#!/usr/bin/env python3
"""Project Handsoff supervisor CLI. All gate logic lives in handsoff_lib.py;
this file is the command surface over it.

Paths resolve against the project root (--root, $HANDSOFF_ROOT, or the
nearest ancestor with a handsoff.toml), never against this script's own
directory. Every transition validates the state it is ABOUT to write,
never the state already on disk, and every write is atomic and appended
to a hash-chained event log.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402


def _load(root: Path, cfg: dict):
    status = lib.load_unique_json(lib.status_path(root, cfg))
    acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    return status, acceptance


def _load_all(root: Path, cfg: dict):
    status, acceptance = _load(root, cfg)
    verifications, verification_problems = lib.load_verifications(root, cfg)
    return status, acceptance, verifications, verification_problems


def _criterion(acceptance: dict, criterion_id: str) -> dict | None:
    return next((c for c in acceptance.get("criteria", []) if c.get("id") == criterion_id), None)


def _most_recent_kind(events: list[dict], kinds: set[str]) -> str | None:
    """Scan the event log backwards for the latest event whose kind is one
    of `kinds`, ignoring everything else in between. Used to tell an
    open start/end pair (background-wait, human-pause, a still-pending
    design approval request) apart from a closed one, without a second
    piece of state to keep in sync with the log."""
    for event in reversed(events):
        if event.get("kind") in kinds:
            return event["kind"]
    return None


def _invalidate_decisions(status: dict, *, rollback_to: int = 5, invalidate_design: bool = False) -> None:
    """Any acceptance mutation makes earlier review/deployment/live decisions
    stale. `invalidate_design=True` additionally clears design_approved and,
    for a flagged run (requires_design_approval) that had advanced to phase
    3+, forces it back to phase 2 -- landing at phase 3+ with a cleared
    approval would otherwise be an immediately-blocked state, which no
    other rollback in this function ever produces. Only the three
    criterion-registry-mutating callers (criterion-add/update/remove) pass
    this; the three evidence-recording callers (verify, record-evidence,
    record-symptom-resolved) do not, since they change nothing about WHAT
    is being asked for, only whether it has been proven -- an already-
    approved design must not be forced back into re-approval just because
    evidence was attached to it."""
    status["review"] = None
    status["reviewed_by"] = None
    status["deployment_approved"] = None
    status["live_verification_id"] = None
    if status.get("phase_number", 1) >= 6:
        status["phase_number"] = rollback_to
        status["phase"] = lib.PHASES[rollback_to]
        status["status"] = "in_progress"
        status["progress"] = min(status.get("progress", 0), 40 if rollback_to == 4 else 50)
    if invalidate_design:
        if "design_review" in status or status.get("requires_design_review"):
            status["design_review"] = None
        status["design_approved"] = None
        if (status.get("requires_design_approval") or status.get("requires_design_review")) \
                and status.get("phase_number", 1) >= 3:
            status["phase_number"] = 2
            status["phase"] = lib.PHASES[2]
            status["status"] = "in_progress"
            status["progress"] = min(status.get("progress", 0), 20)


def _durable_results(results: list[dict]) -> list[dict]:
    """Do not persist command output, which may contain secrets; retain hashes and metadata."""
    return [{k: v for k, v in result.items() if k != "output_tail"} for result in results]


def _audit_errors(root: Path, cfg: dict, status: dict, records: list[dict],
                  verification_problems: list[str]) -> list[str]:
    problems = [f"event log: {p}" for p in lib.verify_event_log(root, cfg)]
    problems += [f"verification ledger: {p}" for p in verification_problems]
    actual_head = records[-1].get("hash") if records else "GENESIS"
    if status.get("verification_head") != actual_head:
        problems.append("verification ledger: tail does not match its anchored head")
    return problems


def _print_audit_block(problems: list[str]) -> int:
    print("SHIP_FEATURE_BLOCKED")
    print("\n".join(f"- {problem}" for problem in problems))
    return 1


def cmd_init(args) -> int:
    if not args.feature or not args.feature.strip():
        print("SHIP_FEATURE_BLOCKED: feature must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    sp, ap = lib.status_path(root, cfg), lib.acceptance_path(root, cfg)
    now = datetime.now(timezone.utc).isoformat()
    # Locked for the same reason advance/deployment-gate are: init writes
    # two files and appends an event, and an unlocked append_event racing
    # another process's append_event forks the hash chain (round 2 finding).
    with lib.project_lock(root):
        artifacts = (sp, ap, lib.event_log_path(root, cfg), lib.verification_log_path(root, cfg),
                     lib.event_head_path(root))
        existing = [path.name for path in artifacts if path.exists()]
        if existing:
            print(f"HANDSOFF_INIT_SKIPPED: existing Handsoff artifacts at {root}: {', '.join(existing)}")
            return 1
        acceptance = {
            "feature": args.feature,
            "criteria": [{
                "id": "REQ-001", "type": "primary_fix", "requirement": lib.PLACEHOLDER_REQUIREMENT,
                "verification": "automated", "tests": list(lib.PLACEHOLDER_TESTS), "evidence": [], "state": "failing",
            }],
        }
        acceptance["work_items"] = lib.derive_work_item_registry(
            acceptance, cfg, now=now, explicit_items=args.item,
        )
        status = {
            "feature": args.feature, "phase_number": 1, "phase": lib.PHASES[1], "progress": 0,
            "status": "in_progress", "updated_at": now, "last_heartbeat_at": None,
            "next_action": lib.NEXT_ACTION_DEFAULTS[1],
            "design_round": 0, "review_round": 0, "retry_count": 0, "summary": "", "reassurance": "",
            "legacy_review_round_offset": 0, "review_attempts": [],
            "review_cap_overrides": [], "escalation": None,
            "recovery_attempts": [], "recovery_lease": None,
            "regression_requests": [],
            "active_work_item": None,
            "implemented_by": None, "reviewed_by": None, "deployment_approved": None,
            "requires_design_approval": True, "design_approved": None,
            "requires_design_review": True, "design_review": None,
            "review": None, "live_verification_id": None, "original_symptom_evidence_id": None,
            "verification_head": "GENESIS",
            "requirement_coverage": {"passing": 0, "failing": 1, "not_tested": 0, "blocked": 0,
                                      "original_symptom_resolved": False},
            "reviewer_checklist": {"symptom_reproduced": "not_verifiable", "symptom_resolved": "not_verifiable",
                                   "all_criteria_verified": "no", "evidence_attached": "no"},
            "events": [],
        }
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="initialized", event_message=f"Handsoff initialized for '{args.feature}'",
                  project_root=str(root))
    print(f"HANDSOFF_INITIALIZED: {sp} and {ap}")
    return 0


def cmd_status(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems)
        warning = lib.stall_warning(status, cfg)
        activity = lib.activity_note(status, cfg)
        log_problems = lib.verify_event_log(root, cfg)
    print(__import__("json").dumps({
        "root": str(root), "feature": status.get("feature"), "phase": status.get("phase"),
        "phase_number": status.get("phase_number"), "progress": status.get("progress"),
        "status": status.get("status"), "next_action": status.get("next_action"),
        "design_round": status.get("design_round"), "review_round": status.get("review_round"),
        "review_attempts": [{k: item.get(k) for k in ("attempt", "attempt_id", "trigger", "disposition", "reviewer")}
                            for item in (status.get("review_attempts") or [])],
        "effective_max_review_rounds": lib.effective_review_cap(status, cfg)
        if "review_attempts" in status else cfg.get("max_review_rounds"),
        "escalation": status.get("escalation"),
        "design_review": status.get("design_review"),
        "reviewed_by": status.get("reviewed_by"),
        "live_verification_id": status.get("live_verification_id"),
        "verification_runs": len(verifications),
        "validation": "blocked" if errors or log_problems else "valid", "errors": errors,
        "stall_warning": warning, "activity_note": activity,
        "event_log_intact": not log_problems, "event_log_problems": log_problems,
    }, indent=2))
    return 1 if errors or log_problems else 0


def cmd_validate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems)
        log_problems = lib.verify_event_log(root, cfg)
    if log_problems:
        errors = errors + [f"event log: {p}" for p in log_problems]
    if errors:
        print("SHIP_FEATURE_INVALID")
        print("\n".join(f"- {x}" for x in errors))
        return 1
    print("SHIP_FEATURE_VALID")
    return 0


def cmd_advance(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)

    if args.design_round is not None and args.new_design_round:
        print("SHIP_FEATURE_INVALID: pass either --design-round or --new-design-round, not both")
        return 1
    if args.design_round_reason and not args.new_design_round:
        print("SHIP_FEATURE_INVALID: --design-round-reason only applies together with --new-design-round")
        return 1
    if args.new_design_round and args.phase != 2:
        print("SHIP_FEATURE_INVALID: --new-design-round only applies when advancing to phase 2 (Design debate)")
        return 1

    # The lock covers the ENTIRE read-validate-write sequence, not just the
    # final write. Locking only the write let two concurrent callers both
    # read the same stale state, both validate successfully against it,
    # and then race at the write, silently losing one caller's transition.
    # Re-reading inside the lock means the second caller to arrive always
    # validates against what the first one actually left behind.
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)

        if args.phase not in lib.PHASES:
            print(f"invalid phase {args.phase}, must be one of {sorted(lib.PHASES)}")
            return 1
        current = int(status.get("phase_number", 0) or 0)
        if args.phase < current or args.phase > current + 1:
            print(f"phase transition blocked: current={current}, requested={args.phase} (one step at a time)")
            return 1
        if args.progress < status.get("progress", 0):
            print(f"progress transition blocked: current={status.get('progress')}, requested={args.progress} (progress cannot decrease)")
            return 1

        # Build the PROPOSED status and validate THAT, before writing
        # anything. This is the fix for the original bug: validating the
        # status already on disk can never catch the transition about to
        # happen, because the phase-6+ checks only fire once phase_number
        # already reads 6+, which is one write too late.
        proposed = dict(status)
        proposed["phase_number"] = args.phase
        proposed["phase"] = lib.PHASES[args.phase]
        proposed["progress"] = args.progress
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposed["next_action"] = args.next_action or lib.NEXT_ACTION_DEFAULTS.get(args.phase, proposed.get("next_action"))
        if args.status:
            proposed["status"] = args.status
        elif args.phase == 7:
            proposed["status"] = "awaiting_approval"
        elif args.phase == 8:
            proposed["status"] = "complete"
        if args.implemented_by:
            proposed["implemented_by"] = args.implemented_by
        if args.authorization_hold:
            if args.phase != 2 or proposed.get("status") != "blocked":
                print("SHIP_FEATURE_INVALID: --authorization-hold requires Phase 2 with --status blocked")
                return 1
            proposed["authorization_hold"] = args.authorization_hold
        else:
            # Holds are assertions about THIS exact transition, never sticky
            # state inherited by a later, unrelated Phase-2 block.
            proposed.pop("authorization_hold", None)
        new_design_round_event = None
        if args.design_round is not None:
            proposed["design_round"] = args.design_round
        elif args.new_design_round:
            # Organic tracking: bump from whatever design_round actually is
            # on disk right now, so repeated --new-design-round calls count
            # real rounds one at a time instead of the caller having to
            # compute and pass an absolute number (that's still --design-round,
            # kept for explicit overrides/fixture setup).
            previous_round = int(status.get("design_round", 0) or 0)
            new_round = previous_round + 1
            proposed["design_round"] = new_round
            new_design_round_event = {
                "design_round": new_round,
                "previous_design_round": previous_round,
                "reason": args.design_round_reason,
            }
        if args.review_round is not None:
            lib.migrate_review_ledger(proposed)
            current_review = int(proposed.get("review_round", 0) or 0)
            if args.review_round < current_review:
                print("SHIP_FEATURE_INVALID: review_round is monotonic")
                return 1
            if args.review_round > lib.effective_review_cap(proposed, cfg):
                print(f"SHIP_FEATURE_INVALID: round cap: review_round {args.review_round} exceeds "
                      f"effective max_review_rounds {lib.effective_review_cap(proposed, cfg)}")
                return 1
            now = datetime.now(timezone.utc).isoformat()
            while proposed["review_round"] < args.review_round:
                attempts = proposed["review_attempts"]
                aid = lib._new_bounded_id(
                    "ha", lib.REVIEW_ATTEMPT_ID_PATTERN,
                    {item.get("attempt_id") for item in attempts}, None,
                )
                number = proposed["review_round"] + 1
                attempts.append({
                    "attempt_id": aid, "attempt": number, "opened_at": now, "closed_at": now,
                    "opened_by": "advance", "reviewer": None, "session_ids": [],
                    "trigger": "manual_override", "trigger_detail": "legacy advance --review-round",
                    "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
                    "phase_number": args.phase, "disposition": "unrecorded", "findings": [],
                })
                proposed["review_round"] = number

        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems)
        if errors:
            # A blocked attempt to LEAVE Phase 2 specifically because design
            # approval is missing/stale IS the state machine telling us the
            # run just entered "awaiting design approval" -- log that fact
            # even though the phase transition itself is refused and
            # nothing else is written. Deduped against the log itself (no
            # second field to keep in sync): skip if the most recent
            # design-lifecycle event is already an unresolved request.
            if args.phase == 3 \
                    and any(e.startswith("design gate:") for e in errors) \
                    and not any(e.startswith("design review gate:") for e in errors):
                pending = _most_recent_kind(lib.read_events(root, cfg),
                                            {"design_round_advanced", "design_approval_requested", "design_approved"})
                if pending != "design_approval_requested":
                    lib.commit(root, cfg, event_kind="design_approval_requested",
                              event_message="Design approval requested (advance to Phase 3 blocked pending it)")
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        if args.dry_run:
            print("SHIP_FEATURE_ADVANCE_WOULD_SUCCEED")
            return 0

        if new_design_round_event is not None:
            reason = new_design_round_event["reason"]
            message = (f"Design round {new_design_round_event['previous_design_round']} -> "
                       f"{new_design_round_event['design_round']}")
            if reason:
                message += f": {reason}"
            extra_events = None
            if new_design_round_event["previous_design_round"] >= 1:
                extra_events = [{
                    "kind": "design_round_ended",
                    "message": f"Design round {new_design_round_event['previous_design_round']} ended (next round started)",
                    "design_round": new_design_round_event["previous_design_round"],
                    "trigger": "next_round_started",
                }]
            lib.commit(root, cfg, status=proposed, extra_events=extra_events,
                      event_kind="design_round_advanced", event_message=message,
                      phase_number=args.phase, progress=args.progress, **new_design_round_event)
        else:
            lib.commit(root, cfg, status=proposed,
                      event_kind="phase_advanced", event_message=f"Advanced to {lib.PHASES[args.phase]}",
                      phase_number=args.phase, progress=args.progress)

        if args.phase == 8 and proposed.get("status") == "complete":
            # The phase transition above already committed successfully; an
            # archive failure (e.g. an unwritable Documents folder) must not
            # be reported as if the run itself failed.
            try:
                fresh_verifications, _ = lib.load_verifications(root, cfg)
                archive_path = lib.archive_run(root, cfg, proposed, acceptance,
                                               fresh_verifications, lib.read_events(root, cfg))
                print(f"HANDSOFF_ARCHIVED: {archive_path}")
            except OSError as exc:
                print(f"HANDSOFF_ARCHIVE_FAILED (run still completed successfully): {exc}")
    print("SHIP_FEATURE_ADVANCED")
    return 0


def cmd_deployment_gate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems)
        if errors:
            print("DEPLOYMENT_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        # Approval is only meaningful in Phase 7. Refusing earlier phases
        # prevents premature approval; refusing later phases prevents a
        # delayed/double-click request from mutating a completed run.
        phase = int(status.get("phase_number", 0) or 0)
        if phase != 7:
            print(f"DEPLOYMENT_BLOCKED\n- deployment approval requires Phase 7 (currently Phase {phase})")
            return 1
        if not args.approve:
            print("DEPLOYMENT_AWAITING_EXPLICIT_APPROVAL")
            return 2
        if not args.by:
            print("DEPLOYMENT_BLOCKED\n- --by is required when recording approval")
            return 1
        acceptance_digest = lib.acceptance_hash(acceptance.get("criteria", []))
        config_digest = lib.config_hash(cfg)
        existing = status.get("deployment_approved")
        if isinstance(existing, dict):
            if existing.get("acceptance_hash") == acceptance_digest \
                    and existing.get("config_hash") == config_digest:
                print("DEPLOYMENT_ALREADY_APPROVED")
                return 0
            print("DEPLOYMENT_BLOCKED\n- existing deployment approval is stale; workflow decisions must be refreshed")
            return 1
        # The approval is bound to a hash of the acceptance criteria AT
        # THE MOMENT of approval. If the registry changes afterward (a
        # criterion reopened, added, or removed), the Phase 8 gate
        # recomputes this hash and refuses the now-stale approval.
        proposed = dict(status)
        proposed["deployment_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "acceptance_hash": acceptance_digest,
            "config_hash": config_digest,
        }
        proposed["status"] = "ready_to_deploy"
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposed["next_action"] = "Deploy the reviewed change and run live verification."
        proposed_errors = lib.compute_errors(
            proposed, acceptance, cfg, verifications=verifications,
            verification_problems=verification_problems,
        )
        if proposed_errors:
            print("DEPLOYMENT_BLOCKED")
            print("\n".join(f"- {error}" for error in proposed_errors))
            return 1
        lib.commit(root, cfg, status=proposed,
                  event_kind="deployment_approved", event_message="Explicit deployment approval recorded",
                  by=args.by)
    print("DEPLOYMENT_APPROVED")
    return 0


def _regression_bindings(root: Path, cfg: dict, acceptance: dict, status: dict) -> dict:
    regression_policy = {
        "regressions": cfg.get("regressions", []),
        "regression_gate": cfg.get("regression_gate", {}),
        "check_timeout_seconds": cfg.get("check_timeout_seconds"),
    }
    events = lib.read_events(root, cfg)
    epoch = {
        "sessions": sorted((status.get("agent_sessions") or {}).keys()),
        "replacements": [item.get("replacement_id") for item in status.get("agent_replacements", [])],
        "recoveries": [item.get("recovery_id") for item in status.get("recovery_attempts", [])],
    }
    return {
        "repository": lib.repository_snapshot(root),
        "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
        "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]),
        "run_id": events[0].get("hash") if events else "GENESIS",
        "epoch_sha256": hashlib.sha256(__import__("json").dumps(
            epoch, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "config_hash": hashlib.sha256(__import__("json").dumps(
            regression_policy, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
    }


def _regression_request(status: dict, request_id: str | None = None) -> dict | None:
    requests = status.get("regression_requests") or []
    if request_id:
        return next((item for item in requests if item.get("request_id") == request_id), None)
    return lib.active_regression_request(status)


def _same_regression_bindings(item: dict, root: Path, cfg: dict, acceptance: dict, status: dict) -> bool:
    current = _regression_bindings(root, cfg, acceptance, status)
    return all(item.get(key) == current[key] for key in current)


def _legacy_scope_bootstrap_allowed(root: Path, cfg: dict, status: dict,
                                    acceptance: dict, before_items: list[dict]) -> bool:
    """Trust an old scope-less approval only when its full audit proof is current."""
    review = status.get("design_review")
    approval = status.get("design_approved")
    if not isinstance(review, dict) or not isinstance(approval, dict):
        return False
    if review.get("decision") != "approved":
        return False
    current_design = lib.design_hash(acceptance.get("criteria", []))
    current_config = lib.config_hash(cfg)
    if any(record.get("design_hash") != current_design or record.get("config_hash") != current_config
           for record in (review, approval)):
        return False
    approval_event = next((event for event in reversed(lib.read_events(root, cfg))
                           if event.get("kind") == "design_approved"), None)
    if not approval_event or approval_event.get("acceptance_sha256") != lib._file_sha256(
            lib.acceptance_path(root, cfg)):
        return False
    tagged = {lib.criterion_work_item_id(item) for item in acceptance.get("criteria", [])}
    tagged.discard(None)
    return all(not item.get("required", True) or item.get("id") in tagged for item in before_items)


def cmd_work_items_sync(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        before_items, _ = lib.effective_work_items(acceptance, cfg)
        before_scope = lib.work_item_scope_hash(before_items)
        derived = lib.derive_work_item_registry(acceptance, cfg)
        existing = acceptance.get("work_items")
        if isinstance(existing, list):
            by_id = {item["id"]: item for item in existing}
            for item in derived:
                current = by_id.get(item["id"])
                if current is None:
                    existing.append(item)
                elif args.from_tickets:
                    current["title"], current["url"] = item["title"], item["url"]
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
            persisted = existing
        else:
            persisted = derived
        acceptance["work_items"] = persisted
        after_scope = lib.work_item_scope_hash(persisted)
        bootstrap = False
        decisions = [status.get("design_review"), status.get("design_approved")]
        scope_missing = any(isinstance(record, dict) and not record.get("scope_hash")
                            for record in decisions)
        scope_conflict = any(isinstance(record, dict) and record.get("scope_hash") not in {None, after_scope}
                             for record in decisions)
        if before_scope == after_scope and scope_missing:
            bootstrap = not scope_conflict and all(isinstance(record, dict) and not record.get("scope_hash")
                                                   for record in decisions) \
                and _legacy_scope_bootstrap_allowed(root, cfg, status, acceptance, before_items)
            if bootstrap:
                for field in ("design_review", "design_approved"):
                    status[field]["scope_hash"] = after_scope
            else:
                status["design_review"] = None
                status["design_approved"] = None
        elif before_scope != after_scope or scope_conflict:
            status["design_review"] = None
            status["design_approved"] = None
        if (before_scope != after_scope or scope_conflict or (scope_missing and not bootstrap)) \
                and status.get("phase_number", 1) >= 3:
            status.update(phase_number=2, phase=lib.PHASES[2], progress=min(status.get("progress", 0), 20),
                          status="in_progress", next_action="Review and approve the changed work-item scope.")
        status.setdefault("active_work_item", None)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance) + lib.validate_status_schema(status)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_items_synced", event_message="Canonical work-item registry synchronized",
                   by=actor, count=len(persisted), scope_hash=after_scope,
                   scope_binding_bootstrapped=bootstrap)
    print(f"WORK_ITEMS_SYNCED: {len(persisted)}")
    return 0


def cmd_work_item_activate(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items, _ = lib.effective_work_items(acceptance, cfg)
        if args.item not in {item["id"] for item in items}:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        status["active_work_item"] = args.item
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="work_item_activated",
                   event_message=f"Activated work item {args.item}", by=actor, work_item=args.item)
    print(f"WORK_ITEM_ACTIVATED: {args.item}")
    return 0


def cmd_work_item_update(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items = acceptance.get("work_items")
        if not isinstance(items, list):
            print("SHIP_FEATURE_BLOCKED: synchronize work items before updating them")
            return 1
        item = next((candidate for candidate in items if candidate.get("id") == args.item), None)
        if item is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        before_scope = lib.work_item_scope_hash(items)
        for field in ("title", "url", "github_state", "notes"):
            value = getattr(args, field)
            if value is not None:
                item[field] = value
        if args.required:
            item["required"] = True
        elif args.optional:
            item["required"] = False
        if args.github_state is not None:
            item["github_checked_at"] = datetime.now(timezone.utc).isoformat()
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        after_scope = lib.work_item_scope_hash(items)
        if before_scope != after_scope:
            _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = item["updated_at"]
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_item_updated", event_message=f"Updated work item {args.item}",
                   by=actor, work_item=args.item, scope_changed=before_scope != after_scope)
    print(f"WORK_ITEM_UPDATED: {args.item}")
    return 0


def cmd_regression_request(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    group = lib.regression_group(cfg, args.group)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        if lib.active_regression_request(status):
            print("REGRESSION_BLOCKED: another regression request is already live")
            return 1
        now = datetime.now(timezone.utc)
        requests = list(status.get("regression_requests") or [])
        terminals = [item for item in requests if item.get("state") not in {"awaiting_approval", "accepted", "launched"}]
        while len(requests) >= lib.MAX_REGRESSION_REQUESTS and terminals:
            victim = terminals.pop(0)
            requests.remove(victim)
        if len(requests) >= lib.MAX_REGRESSION_REQUESTS:
            print("REGRESSION_BLOCKED: request history is full")
            return 1
        item = {
            "request_id": f"rg-{uuid.uuid4().hex}", "group": group["name"],
            "commands": list(group["commands"]), "command_sha256": lib.command_sha256(group["commands"]),
            "state": "awaiting_approval", "requested_by": actor, "requested_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=cfg["regression_gate"]["approval_timeout_minutes"])).isoformat(),
            "decided_by": None, "decided_at": None, "launched_at": None, "completed_at": None,
            "launch_nonce_sha256": None, "reason": args.reason.strip(),
            "requester_session_id": next((sid for sid, session in (status.get("agent_sessions") or {}).items()
                                          if session.get("actor") == actor), None),
            **_regression_bindings(root, cfg, acceptance, status), "results": [],
        }
        status["regression_requests"] = [*requests, item]
        status["updated_at"] = now.isoformat()
        lib.commit(root, cfg, status=status, event_kind="regression_requested",
                   event_message=f"Regression group {group['name']} awaits Pilot approval",
                   request_id=item["request_id"], group=group["name"], command_sha256=item["command_sha256"])
    print(f"REGRESSION_AWAITING_APPROVAL: {item['request_id']}")
    return 0


def cmd_regression_decide(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    decision = "accepted" if args.accept else "declined"
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "awaiting_approval":
            print("REGRESSION_BLOCKED: request is not awaiting approval")
            return 1
        now = datetime.now(timezone.utc)
        if now > datetime.fromisoformat(item["expires_at"]):
            item["state"] = "expired"
            item["completed_at"] = now.isoformat()
            decision = "expired"
        elif not _same_regression_bindings(item, root, cfg, acceptance, status):
            item["state"] = "invalidated"
            item["completed_at"] = now.isoformat()
            decision = "invalidated"
        else:
            item["state"] = decision
        item["decided_by"] = actor
        item["decided_at"] = now.isoformat()
        status["updated_at"] = now.isoformat()
        lib.commit(root, cfg, status=status, event_kind=f"regression_{decision}",
                   event_message=f"Regression request {decision}", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_{decision.upper()}: {item['request_id']}")
    return 0 if decision in {"accepted", "declined"} else 1


def cmd_regression_cancel(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") not in {"awaiting_approval", "accepted"}:
            print("REGRESSION_BLOCKED: only a pending or accepted request can be cancelled")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        item["state"] = "cancelled"
        item["completed_at"] = now
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="regression_cancelled",
                   event_message="Regression request cancelled", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_CANCELLED: {item['request_id']}")
    return 0


def cmd_regression_run(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    raw_nonce = uuid.uuid4().hex
    nonce_digest = hashlib.sha256(raw_nonce.encode()).hexdigest()
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "accepted":
            print("REGRESSION_BLOCKED: request has not been accepted")
            return 1
        now = datetime.now(timezone.utc)
        accepted_at = datetime.fromisoformat(item["decided_at"])
        launch_deadline = accepted_at + timedelta(minutes=cfg["regression_gate"]["launch_window_minutes"])
        if now > launch_deadline or not _same_regression_bindings(item, root, cfg, acceptance, status):
            item["state"] = "expired" if now > launch_deadline else "invalidated"
            item["completed_at"] = now.isoformat()
            status["updated_at"] = now.isoformat()
            lib.commit(root, cfg, status=status, event_kind=f"regression_{item['state']}",
                       event_message=f"Regression request {item['state']} before launch", request_id=item["request_id"])
            print(f"REGRESSION_{item['state'].upper()}: {item['request_id']}")
            return 1
        item["state"] = "launched"
        item["launched_at"] = now.isoformat()
        item["launch_nonce_sha256"] = nonce_digest
        status["updated_at"] = now.isoformat()
        commands = list(item["commands"])
        lib.commit(root, cfg, status=status, event_kind="regression_launched",
                   event_message=f"Accepted regression group {item['group']} launched",
                   request_id=item["request_id"], by=actor)
    results = lib.run_checks(cfg, root, commands=commands, allow_regression=True)
    with lib.project_lock(root):
        # Re-read policy after the command returns. Reusing the pre-launch
        # object would let a concurrent policy edit pass its own comparison.
        cfg = lib.load_config(root)
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "launched" or item.get("launch_nonce_sha256") != nonce_digest:
            print("REGRESSION_LATE_RESULT_IGNORED")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        bindings_valid = _same_regression_bindings(item, root, cfg, acceptance, status)
        item["state"] = ("completed" if all(result["exit_code"] == 0 for result in results) else "failed") \
            if bindings_valid else "invalidated"
        item["completed_at"] = now
        item["results"] = _durable_results(results)
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind=f"regression_{item['state']}",
                   event_message=f"Regression request {item['state']}", request_id=item["request_id"])
    print(__import__("json").dumps({"request_id": item["request_id"], "state": item["state"], "results": results}, indent=2))
    return 0 if item["state"] == "completed" else 1


def cmd_regression_finalize(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "launched":
            print("REGRESSION_BLOCKED: request is not launched")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        item["state"] = "failed"
        item["completed_at"] = now
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="regression_failed",
                   event_message="Pilot terminalized a stranded regression launch", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_FAILED: {item['request_id']}")
    return 0


def cmd_verify(args) -> int:
    """Run checks and bind the immutable result to named criteria.

    Each named criterion is judged ONLY by its own configured `tests`, never
    by the outcome of some other command in [checks].commands that happens
    to be configured globally. Two named criteria with different tests get
    two independent verification records: an unrelated criterion's failing
    test must never fail this one, and an unrelated passing command must
    never satisfy it either. The union of needed commands is still run only
    once per invocation, for efficiency when criteria share a test."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not cfg.get("check_commands"):
        print("SHIP_FEATURE_NO_CHECKS_CONFIGURED: set [checks].commands in handsoff.toml")
        return 1
    with lib.project_lock(root):
        status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
        lib.ensure_no_launched_regression(status)
        audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criteria = [_criterion(acceptance, cid) for cid in args.criterion]
        missing = [cid for cid, criterion in zip(args.criterion, criteria) if criterion is None]
        if missing:
            print(f"SHIP_FEATURE_BLOCKED: unknown criteria: {', '.join(missing)}")
            return 1
        non_automated = [c["id"] for c in criteria
                         if "checks" not in lib.VERIFICATION_REQUIREMENTS.get(c.get("verification"), set())]
        if non_automated:
            print(f"SHIP_FEATURE_BLOCKED: verify cannot satisfy non-automated criteria: {', '.join(non_automated)}")
            return 1
        configured = set(cfg["check_commands"])
        unmatched = {c["id"]: [test for test in c.get("tests", []) if test not in configured]
                     for c in criteria}
        unmatched = {cid: tests for cid, tests in unmatched.items() if tests}
        if unmatched:
            print("SHIP_FEATURE_BLOCKED: criterion tests must exactly match configured check commands: "
                  + __import__("json").dumps(unmatched, sort_keys=True))
            return 1
        before = {c["id"]: lib.criterion_spec_hash(c) for c in criteria}
        needed_set = {test for c in criteria for test in c.get("tests", [])}
        needed = [cmd for cmd in cfg["check_commands"] if cmd in needed_set]
    results = lib.run_checks(cfg, root, commands=needed)
    results_by_command = {r["command"]: r for r in results}
    with lib.project_lock(root):
        status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criteria = [_criterion(acceptance, cid) for cid in args.criterion]
        if any(c is None for c in criteria) or any(lib.criterion_spec_hash(c) != before[c["id"]] for c in criteria):
            print("SHIP_FEATURE_BLOCKED: criterion changed while checks were running; run verify again")
            return 1
        per_criterion = {}
        for criterion in criteria:
            own_results = [results_by_command[t] for t in criterion.get("tests", [])]
            own_ok = bool(own_results) and all(r["exit_code"] == 0 for r in own_results)
            record = lib.append_verification(root, cfg, kind="checks", ok=own_ok, by=args.by,
                                             criteria=[criterion], results=_durable_results(own_results))
            status["verification_head"] = record["hash"]
            if record["run_id"] not in criterion["evidence"]:
                criterion["evidence"].append(record["run_id"])
            if not own_ok:
                criterion["state"] = "failing"
            elif lib.criterion_fully_evidenced(criterion, existing_records + [record]):
                criterion["state"] = "passing"
            else:
                criterion["state"] = "not_tested"
            per_criterion[criterion["id"]] = {"run_id": record["run_id"], "ok": own_ok}
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="checks_run", event_message="Ran and attached configured checks",
                  criteria=per_criterion,
                  results=[{"command": r["command"], "exit_code": r["exit_code"],
                            "output_sha256": r["output_sha256"]} for r in results])
    overall_ok = all(v["ok"] for v in per_criterion.values())
    print(__import__("json").dumps({"ok": overall_ok, "criteria": per_criterion, "results": results}, indent=2))
    return 0 if overall_ok else 1


def cmd_record_evidence(args) -> int:
    """Record named manual/browser evidence in the immutable ledger."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.description or not args.description.strip():
        print("SHIP_FEATURE_BLOCKED: --description must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        required_kinds = lib.VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set())
        if args.kind not in required_kinds:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} policy does not accept {args.kind} evidence")
            return 1
        record = lib.append_verification(root, cfg, kind=args.kind, ok=True, by=args.by,
                                         criteria=[criterion], description=args.description)
        status["verification_head"] = record["hash"]
        criterion["evidence"].append(record["run_id"])
        criterion["state"] = ("passing" if lib.criterion_fully_evidenced(criterion, records + [record])
                              else "not_tested")
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="evidence_recorded", event_message="Attached criterion evidence",
                  run_id=record["run_id"], criterion=args.criterion, by=args.by)
    print(f"EVIDENCE_RECORDED: {record['run_id']}")
    return 0


def cmd_record_symptom(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        record = next((r for r in records if r.get("run_id") == args.evidence and r.get("ok") is True), None)
        if not record:
            print("SHIP_FEATURE_BLOCKED: symptom evidence must reference a successful verification run")
            return 1
        primary = {c["id"]: c for c in acceptance["criteria"] if c.get("type") == "primary_fix"}
        current_primary = any(
            cid in record.get("criteria", [])
            and record.get("criterion_hashes", {}).get(cid) == lib.criterion_spec_hash(criterion)
            for cid, criterion in primary.items()
        )
        if not current_primary:
            print("SHIP_FEATURE_BLOCKED: symptom evidence must verify a primary_fix criterion")
            return 1
        status["requirement_coverage"]["original_symptom_resolved"] = True
        status["original_symptom_evidence_id"] = args.evidence
        _invalidate_decisions(status)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status,
                  event_kind="symptom_resolved", event_message="Original symptom marked resolved",
                  evidence=args.evidence, by=args.by)
    print("ORIGINAL_SYMPTOM_RESOLVED")
    return 0


def cmd_design_approve(args) -> int:
    """Record the Architect's core gate: an explicit human approval of the
    proposed design/criteria, distinct from the Architect identity that
    proposed them. This is what actually unblocks Phase 3+ for a flagged
    run (requires_design_approval); the Architect's own text is never
    self-sufficient, the same way a Reviewer's own text never advances
    the review gate without record-review."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.architect or not args.architect.strip():
        print("SHIP_FEATURE_BLOCKED: --architect must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    if args.by.strip().casefold() == args.architect.strip().casefold():
        print("SHIP_FEATURE_BLOCKED: approver must differ from the architect, no self-approval")
        return 1
    if args.redesigns_settled_work is not None and not args.redesigns_settled_work.strip():
        print("SHIP_FEATURE_BLOCKED: --redesigns-settled-work must be a non-empty string when passed")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criteria = acceptance.get("criteria", [])
        if any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT and c.get("tests") == lib.PLACEHOLDER_TESTS
               for c in criteria):
            print("SHIP_FEATURE_BLOCKED: acceptance registry still contains init's untouched placeholder "
                  "criterion; author a real criterion before requesting design approval")
            return 1
        # Stamp authored_by = the architect on every criterion that does not
        # already carry one (absent key, or explicitly null/empty all count
        # as unstamped) -- an earlier approval's authorship is never
        # reassigned by a later one. Safe because criterion_spec_hash (and
        # therefore design_hash) excludes authored_by entirely: this can
        # never change a criterion's spec hash, invalidate already-recorded
        # evidence, or mismatch a freshly recomputed design_hash.
        for c in criteria:
            if c.get("authored_by") is None:
                c["authored_by"] = args.architect
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "architect": args.architect,
            "summary": args.summary,
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]),
            "redesigns_settled_work": args.redesigns_settled_work,
        }
        # In the natural AR7 flow, record-design-review left the run
        # explicitly blocked on this human decision. Once both current
        # design decisions exist, reflect that the Supervisor may advance
        # instead of leaving Mission Control stuck on a stale approval ask.
        if status.get("phase_number") == 2 and status.get("requires_design_review") \
                and not lib._design_review_errors(status, acceptance, cfg):
            status["status"] = "in_progress"
            status["next_action"] = "Advance the independently reviewed and human-approved design to Phase 3."
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Approval is round_end's other trigger (the first being the next
        # round starting, see cmd_advance): whatever round was open when
        # approval landed just ended, closing the episode a design-timing
        # read-back needs. Only meaningful for a run that ever tracked an
        # organic round (design_round > 0); a run that went straight from
        # init to design-approve at round 0 has no round to close.
        current_round = int(status.get("design_round", 0) or 0)
        extra_events = None
        if current_round >= 1:
            extra_events = [{
                "kind": "design_round_ended",
                "message": f"Design round {current_round} ended (design approved)",
                "design_round": current_round,
                "trigger": "design_approved",
            }]
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra_events,
                  event_kind="design_approved", event_message=args.summary,
                  by=args.by, architect=args.architect,
                  redesigns_settled_work=args.redesigns_settled_work)
    print("DESIGN_APPROVAL_RECORDED")
    return 0


def cmd_record_design_review(args) -> int:
    """Record an independent Phase-2 critique of the current design.

    The record is bound to design_hash, so any later criteria mutation
    makes it unusable even before the mutation command clears it. An
    approval opens the human-approval step; changes requested keep the run
    in Phase 2 and invalidate any human approval that arrived too early.
    """
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.architect or not args.architect.strip():
        print("SHIP_FEATURE_BLOCKED: --architect must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    if args.by.strip().casefold() == args.architect.strip().casefold():
        print("SHIP_FEATURE_BLOCKED: design reviewer must differ from the architect, no self-review")
        return 1

    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, records, problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: independent design review can only be recorded in Phase 2")
            return 1
        criteria = acceptance.get("criteria", [])
        if any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT and c.get("tests") == lib.PLACEHOLDER_TESTS
               for c in criteria):
            print("SHIP_FEATURE_BLOCKED: acceptance registry still contains init's untouched placeholder "
                  "criterion; author a real criterion before design review")
            return 1

        decision = "approved" if args.approve else "changes_requested"
        status["design_review"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by.strip(),
            "architect": args.architect.strip(),
            "decision": decision,
            "summary": args.summary.strip(),
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]),
        }
        status.pop("authorization_hold", None)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        if decision == "approved":
            if not lib._design_errors(status, acceptance, cfg):
                status["status"] = "in_progress"
                status["next_action"] = "Advance the independently reviewed and human-approved design to Phase 3."
            else:
                status["status"] = "blocked"
                status["next_action"] = "Get explicit human approval of the independently reviewed design."
            event_kind = "design_review_approved"
            message = f"Independent design review approved: {args.summary.strip()}"
        else:
            status["design_approved"] = None
            status["status"] = "in_progress"
            status["next_action"] = "Architect revises the design, starts a new design round, and requests review again."
            event_kind = "design_review_changes_requested"
            message = f"Independent design review requested changes: {args.summary.strip()}"
        lib.commit(root, cfg, status=status, event_kind=event_kind, event_message=message,
                  by=args.by.strip(), architect=args.architect.strip(), decision=decision,
                  design_hash=status["design_review"]["design_hash"])
    print("DESIGN_REVIEW_APPROVED" if args.approve else "DESIGN_CHANGES_REQUESTED")
    return 0


def cmd_review_attempt_start(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number", 0) < 4:
            print("SHIP_FEATURE_BLOCKED: review attempts require Phase 4 or later")
            return 1
        proposed = deepcopy(status)
        try:
            attempt = lib.open_review_attempt(
                proposed, acceptance, cfg, by=args.by, trigger=args.trigger,
                detail=args.note or "", reviewer=args.reviewer,
            )
        except lib.HandsoffError as exc:
            if isinstance(proposed.get("escalation"), dict) \
                    and proposed["escalation"].get("kind") == "review_cap_exhausted":
                lib.commit(root, cfg, status=proposed, event_kind="review_attempt_refused",
                           event_message=str(exc), by=args.by,
                           review_round=proposed.get("review_round"),
                           effective_max_review_rounds=lib.effective_review_cap(proposed, cfg))
            print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
            return 1
        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {x}" for x in errors))
            return 1
        lib.commit(root, cfg, status=proposed, event_kind="review_attempt_opened",
                   event_message=f"Implementation review attempt {attempt['attempt']} opened",
                   by=args.by, attempt_id=attempt["attempt_id"], attempt=attempt["attempt"],
                   trigger=attempt["trigger"], reviewer=attempt.get("reviewer"))
    print(f"REVIEW_ATTEMPT_OPENED: {attempt['attempt_id']} "
          f"(attempt {attempt['attempt']} of {lib.effective_review_cap(proposed, cfg)})")
    return 0


def _parse_review_findings(values: list[str]) -> list[dict]:
    if not values or len(values) > 16:
        raise lib.HandsoffError("record-review-findings requires 1 to 16 findings")
    findings = []
    for raw in values:
        code, sep, summary = raw.partition(":")
        code, summary = code.strip(), summary.strip()
        if not sep or code not in lib.REVIEW_FINDING_CODES or not summary or len(summary) > 512:
            raise lib.HandsoffError("findings must use 'CODE: summary' with a supported code")
        findings.append({"code": code, "summary": summary})
    if len({(item["code"], item["summary"]) for item in findings}) != len(findings):
        raise lib.HandsoffError("duplicate review findings are not allowed")
    return findings


def cmd_record_review_findings(args) -> int:
    reviewer = lib.validate_agent_actor(args.by)
    findings = _parse_review_findings(args.finding)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        implementer = status.get("implemented_by")
        if implementer and reviewer.casefold() == implementer.strip().casefold():
            print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
            return 1
        proposed = deepcopy(status)
        attempt = lib.current_review_attempt(proposed)
        if attempt is None:
            try:
                attempt = lib.open_review_attempt(proposed, acceptance, cfg, by=reviewer, reviewer=reviewer)
            except lib.HandsoffError as exc:
                print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
                return 1
        if attempt.get("acceptance_hash") != lib.acceptance_hash(acceptance.get("criteria", [])):
            print("SHIP_FEATURE_BLOCKED: acceptance changed since this review attempt opened")
            return 1
        attempt["reviewer"] = reviewer
        attempt["findings"] = findings
        attempt["disposition"] = "changes_requested"
        attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        if attempt["attempt"] >= lib.effective_review_cap(proposed, cfg):
            lib._review_cap_escalation(proposed, cfg)
            extra = [{"kind": "review_cap_escalated", "message": proposed["escalation"]["reason"],
                      "attempt_id": attempt["attempt_id"], "attempt": attempt["attempt"]}]
        else:
            proposed["phase_number"] = 4
            proposed["phase"] = lib.PHASES[4]
            proposed["progress"] = min(float(proposed.get("progress", 0)), 40)
            proposed["status"] = "in_progress"
            proposed["next_action"] = (
                f"Implementer addresses {len(findings)} findings from attempt {attempt['attempt']}; "
                f"Supervisor then starts attempt {attempt['attempt'] + 1} of {lib.effective_review_cap(proposed, cfg)}"
            )
            extra = None
        proposed["review"] = None
        proposed["reviewed_by"] = None
        lib.commit(root, cfg, status=proposed, extra_events=extra,
                   event_kind="review_attempt_closed",
                   event_message=f"Review attempt {attempt['attempt']} requested changes",
                   by=reviewer, attempt_id=attempt["attempt_id"], attempt=attempt["attempt"],
                   disposition="changes_requested", findings=findings)
    print("REVIEW_CHANGES_RECORDED")
    return 0


def cmd_review_cap_override(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be non-empty")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        proposed = deepcopy(status)
        lib.migrate_review_ledger(proposed)
        overrides = proposed["review_cap_overrides"]
        if len(overrides) >= lib.MAX_REVIEW_CAP_OVERRIDES:
            print("SHIP_FEATURE_BLOCKED: review cap override history is full")
            return 1
        oid = lib._new_bounded_id(
            "ho", lib.REVIEW_OVERRIDE_ID_PATTERN,
            {item.get("override_id") for item in overrides}, None,
        )
        overrides.append({
            "override_id": oid, "by": actor, "at": datetime.now(timezone.utc).isoformat(),
            "reason": args.reason.strip(), "config_hash": lib.config_hash(cfg),
            "review_round_at_grant": int(proposed.get("review_round", 0) or 0),
        })
        if isinstance(proposed.get("escalation"), dict) \
                and proposed["escalation"].get("kind") == "review_cap_exhausted":
            proposed["escalation"] = None
            proposed["status"] = "in_progress"
        proposed["next_action"] = (
            f"Start review attempt {int(proposed.get('review_round', 0)) + 1} "
            f"of {lib.effective_review_cap(proposed, cfg)}"
        )
        lib.commit(root, cfg, status=proposed, event_kind="review_cap_override_recorded",
                   event_message="Human granted one additional review attempt",
                   by=actor, reason=args.reason.strip(), override_id=oid,
                   effective_max_review_rounds=lib.effective_review_cap(proposed, cfg))
    print(f"REVIEW_CAP_OVERRIDE_RECORDED: {oid}")
    return 0


def cmd_record_review(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    reviewer_id = args.by.strip()
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number", 0) < 5:
            print("SHIP_FEATURE_BLOCKED: independent review can only be recorded in Phase 5 or later")
            return 1
        implementer = status.get("implemented_by")
        if implementer and reviewer_id.casefold() == implementer.strip().casefold():
            print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
            return 1
        lib.migrate_review_ledger(status)
        attempt = lib.current_review_attempt(status)
        if attempt is None:
            try:
                attempt = lib.open_review_attempt(
                    status, acceptance, cfg, by=reviewer_id, reviewer=reviewer_id,
                )
            except lib.HandsoffError as exc:
                if isinstance(status.get("escalation"), dict) \
                        and status["escalation"].get("kind") == "review_cap_exhausted":
                    lib.commit(root, cfg, status=status, event_kind="review_attempt_refused",
                               event_message=str(exc), by=reviewer_id)
                print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
                return 1
        if attempt.get("acceptance_hash") != lib.acceptance_hash(acceptance.get("criteria", [])):
            print("SHIP_FEATURE_BLOCKED: acceptance changed since this review attempt opened")
            return 1
        preflight = dict(status)
        preflight["phase_number"] = 6
        preflight["phase"] = lib.PHASES[6]
        implementer_profile = lib.audited_agent_profile(cfg, "implementer")
        reviewer_profile = lib.audited_agent_profile(cfg, "reviewer")
        profiles_distinct = (
            implementer_profile["effective_adapter"], implementer_profile["model"]
        ) != (
            reviewer_profile["effective_adapter"], reviewer_profile["model"]
        )
        preflight["review"] = {
            "by": reviewer_id, "at": datetime.now(timezone.utc).isoformat(),
            "acceptance_hash": lib.acceptance_hash(acceptance["criteria"]),
            "config_hash": lib.config_hash(cfg),
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]),
            "implementer_profile": implementer_profile,
            "reviewer_profile": reviewer_profile,
            "profiles_distinct": profiles_distinct,
            "checklist": {"symptom_reproduced": args.symptom_reproduced,
                          "symptom_resolved": "yes", "all_criteria_verified": "yes",
                          "evidence_attached": "yes"},
        }
        preflight["reviewed_by"] = reviewer_id
        errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        status["review"] = preflight["review"]
        status["reviewed_by"] = reviewer_id
        status["reviewer_checklist"] = preflight["review"]["checklist"]
        attempt["reviewer"] = reviewer_id
        attempt["disposition"] = "approved"
        attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, extra_events=[{
                      "kind": "review_attempt_closed",
                      "message": f"Review attempt {attempt['attempt']} approved",
                      "attempt_id": attempt["attempt_id"], "attempt": attempt["attempt"],
                      "disposition": "approved", "reviewer": reviewer_id,
                  }],
                  event_kind="review_approved", event_message="Independent review approved current acceptance",
                  by=reviewer_id, acceptance_hash=status["review"]["acceptance_hash"],
                  implementer_profile=implementer_profile, reviewer_profile=reviewer_profile,
                  profiles_distinct=profiles_distinct)
    print("INDEPENDENT_REVIEW_RECORDED")
    return 0


def cmd_verify_live(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    commands = cfg.get("live_check_commands", [])
    if not commands:
        print("SHIP_FEATURE_NO_LIVE_CHECKS_CONFIGURED: set [checks].live_commands in handsoff.toml")
        return 1
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        approval_required = cfg.get("deployment_requires_explicit_approval", True)
        if status.get("phase_number") != 7 or (approval_required and not status.get("deployment_approved")):
            print("SHIP_FEATURE_BLOCKED: live verification requires Phase 7"
                  + (" and deployment approval" if approval_required else ""))
            return 1
        digest = lib.acceptance_hash(acceptance["criteria"])
        config_digest = lib.config_hash(cfg)
        if status.get("deployment_approved") and status["deployment_approved"].get("acceptance_hash") != digest:
            print("SHIP_FEATURE_BLOCKED: acceptance changed since deployment approval")
            return 1
        if status.get("deployment_approved") and status["deployment_approved"].get("config_hash") != config_digest:
            print("SHIP_FEATURE_BLOCKED: workflow policy changed since deployment approval")
            return 1
    results = lib.run_checks(cfg, root, commands)
    ok = all(r["exit_code"] == 0 for r in results)
    with lib.project_lock(root):
        # Re-read handsoff.toml from disk here, not the `cfg` captured
        # before run_checks: comparing config_hash(cfg) to itself can
        # never detect a change, since it is the same in-memory object
        # both times. Only a fresh load can see an edit that landed
        # while the (possibly slow) live checks were executing.
        cfg = lib.load_config(root)
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if lib.acceptance_hash(acceptance["criteria"]) != digest:
            print("SHIP_FEATURE_BLOCKED: acceptance changed during live verification; run it again")
            return 1
        if lib.config_hash(cfg) != config_digest:
            print("SHIP_FEATURE_BLOCKED: workflow policy changed during live verification; run it again")
            return 1
        record = lib.append_verification(root, cfg, kind="live", ok=ok, by=args.by,
                                         criteria=acceptance["criteria"], results=_durable_results(results),
                                         acceptance_digest=digest, config_digest=config_digest)
        status["verification_head"] = record["hash"]
        if ok:
            status["live_verification_id"] = record["run_id"]
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status,
                  event_kind="live_checks_run", event_message="Ran configured live checks",
                  ok=ok, run_id=record["run_id"], by=args.by)
    print(__import__("json").dumps({"ok": ok, "run_id": record["run_id"], "results": results}, indent=2))
    return 0 if ok else 1


def _sync_registry_from_criteria(acceptance: dict, cfg: dict) -> bool:
    """Append newly declared criterion identities without deleting stable promises."""
    existing = acceptance.get("work_items")
    if not isinstance(existing, list):
        return False
    known = {item.get("id") for item in existing}
    changed = False
    for item in lib.derive_work_item_registry(acceptance, cfg):
        if item["id"] not in known:
            existing.append(item)
            known.add(item["id"])
            changed = True
    existing.sort(key=lambda item: (item["kind"] != "issue", item.get("number") or 0, item["id"]))
    return changed


def _criterion_needs_item_warning(criterion: dict, acceptance: dict, cfg: dict) -> bool:
    items, _ = lib.effective_work_items(acceptance, cfg)
    return len(items) > 1 and lib.criterion_work_item_id(criterion) is None


def cmd_criterion_update(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if all(getattr(args, field) is None for field in ("requirement", "verification", "type", "test", "state")):
            print("SHIP_FEATURE_BLOCKED: criterion-update requires at least one change")
            return 1
        was_primary = criterion.get("type") == "primary_fix"
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0])
        spec_changed = False
        for field in ("requirement", "verification", "type"):
            value = getattr(args, field)
            if value is not None and criterion.get(field) != value:
                criterion[field] = value
                spec_changed = True
        if args.test is not None:
            criterion["tests"] = args.test
            spec_changed = True
        if args.state is not None:
            criterion["state"] = args.state
        if spec_changed or args.state is not None:
            criterion["evidence"] = []
            if args.state is None or args.state == "passing":
                criterion["state"] = "not_tested"
        if spec_changed and (was_primary or criterion.get("type") == "primary_fix"):
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        registry_changed = _sync_registry_from_criteria(acceptance, cfg)
        warning = _criterion_needs_item_warning(criterion, acceptance, cfg)
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_updated", event_message="Acceptance criterion updated",
                  criterion=args.criterion, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]))
    if warning:
        print("WORK_ITEM_WARNING: untagged criterion in a multi-item run")
    print("CRITERION_UPDATED")
    return 0


def cmd_criterion_add(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if _criterion(acceptance, args.criterion):
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} already exists")
            return 1
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0])
        acceptance["criteria"].append({
            "id": args.criterion, "type": args.type, "requirement": args.requirement,
            "verification": args.verification, "tests": args.test or [],
            "evidence": [], "state": "not_tested",
        })
        if args.type == "primary_fix":
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        criterion = acceptance["criteria"][-1]
        registry_changed = _sync_registry_from_criteria(acceptance, cfg)
        warning = _criterion_needs_item_warning(criterion, acceptance, cfg)
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_added", event_message="Acceptance criterion added",
                  criterion=args.criterion, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0]))
    if warning:
        print("WORK_ITEM_WARNING: untagged criterion in a multi-item run")
    print("CRITERION_ADDED")
    return 0


def cmd_criterion_remove(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if not _criterion(acceptance, args.criterion):
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if len(acceptance["criteria"]) == 1:
            print("SHIP_FEATURE_BLOCKED: acceptance registry must retain at least one criterion")
            return 1
        acceptance["criteria"] = [c for c in acceptance["criteria"] if c["id"] != args.criterion]
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_removed", event_message="Acceptance criterion removed",
                  criterion=args.criterion)
    print("CRITERION_REMOVED")
    return 0


def cmd_recover(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.dry_run:
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            assessment = lib.recovery_assessment(
                status, cfg, lib.read_session_liveness(root), lib.read_events(root, cfg),
            )
        print(__import__("json").dumps(assessment, indent=2))
        return 0

    def launcher(role):
        import handsoff_agent
        task = (f"Resume Phase from trusted Handsoff state as {role}: read handsoff-status.json, "
                "handsoff-acceptance.json and the event log; do not repeat evidenced work.")
        spec = handsoff_agent.build_launch_spec(root, role, task)
        return handsoff_agent.execute_with_recovery(spec, timeout=args.timeout)

    result = lib.recover_run(root, actor=args.by, launcher=launcher)
    label = {
        "skipped": "RECOVERY_SKIPPED", "recovered": "RECOVERY_RECOVERED",
        "failed": "RECOVERY_FAILED", "escalated": "RECOVERY_ESCALATED",
    }[result["action"]]
    print(f"{label}: {result.get('recovery_id') or result['assessment']['reason']}")
    return 1 if result["action"] in {"failed", "escalated"} else 0


def cmd_watch(args) -> int:
    import time
    while True:
        code = cmd_recover(args)
        if code and args.once:
            return code
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_recovery_acknowledge(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be non-empty")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        escalation = status.get("escalation")
        if not isinstance(escalation, dict) or escalation.get("kind") not in {
                "recovery_exhausted", "recovery_paused"}:
            print("SHIP_FEATURE_BLOCKED: no recovery escalation is awaiting acknowledgement")
            return 1
        status["escalation"] = None
        status["status"] = "in_progress"
        status["next_action"] = "Relaunch the assigned role manually or allow the watchdog to reassess."
        lib.commit(root, cfg, status=status, event_kind="recovery_acknowledged",
                   event_message=args.reason.strip(), by=actor)
    print("RECOVERY_ACKNOWLEDGED")
    return 0


def _record_heartbeat(status: dict) -> None:
    """The exact liveness update `heartbeat` performs, factored out so
    background-wait-start/end can feed the SAME signal stall_warning/
    activity_note already read, instead of growing a second, parallel
    stall mechanism just for background waits."""
    status["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()


def cmd_heartbeat(args) -> int:
    """Record a pure liveness signal for a run doing long background work
    (a slow check, an async agent, a scheduled self-wakeup) that has no
    progress to report yet. Deliberately does not touch phase_number,
    progress, or updated_at -- those stay reserved for calls that actually
    advance the work; last_heartbeat_at is reserved for 'still alive'."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        _record_heartbeat(status)
        message = args.note.strip() if args.note and args.note.strip() else "Heartbeat: run is active"
        lib.commit(root, cfg, status=status,
                  event_kind="heartbeat", event_message=message, by=args.by)
    print("HEARTBEAT_RECORDED")
    return 0


def cmd_background_wait_start(args) -> int:
    """Mark the start of a stretch where the run is waiting on a
    background task (an async agent, a slow check, a scheduled
    self-wakeup) rather than idle or waiting on a human. Feeds
    last_heartbeat_at through the exact same path `heartbeat` uses --
    a declared background wait IS a liveness signal, not a second,
    parallel stall mechanism."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"background_wait_started", "background_wait_ended"}) == "background_wait_started":
            print("SHIP_FEATURE_BLOCKED: a background wait is already open; call background-wait-end first")
            return 1
        message = args.note.strip() if args.note and args.note.strip() else "Background task wait started"
        if getattr(args, "resume_after_authorization", False):
            if status.get("phase_number") != 2:
                print("SHIP_FEATURE_BLOCKED: --resume-after-authorization is only valid for a Phase-2 design review")
                return 1
            if status.get("status") != "blocked" or status.get("design_review") \
                    or status.get("authorization_hold") != "design_review":
                print("SHIP_FEATURE_BLOCKED: --resume-after-authorization requires a blocked Phase-2 run "
                      "with authorization_hold=design_review before any review has been recorded")
                return 1
            status["status"] = "in_progress"
            status["next_action"] = message
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            status.pop("authorization_hold", None)
        _record_heartbeat(status)
        lib.commit(root, cfg, status=status,
                  event_kind="background_wait_started", event_message=message, by=args.by)
    print("BACKGROUND_WAIT_STARTED")
    return 0


def cmd_background_wait_end(args) -> int:
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"background_wait_started", "background_wait_ended"}) != "background_wait_started":
            print("SHIP_FEATURE_BLOCKED: no open background wait to end")
            return 1
        _record_heartbeat(status)
        message = args.note.strip() if args.note and args.note.strip() else "Background task wait ended"
        lib.commit(root, cfg, status=status,
                  event_kind="background_wait_ended", event_message=message, by=args.by)
    print("BACKGROUND_WAIT_ENDED")
    return 0


def cmd_human_pause_start(args) -> int:
    """Mark the start of a stretch where the run is waiting on a human for
    something other than the design-approval gate (design_approval_
    requested/design_approved already cover that pair automatically from
    the state machine). Deliberately does not touch last_heartbeat_at:
    nothing is actively running here, so there is nothing alive to
    declare -- that is the whole point of distinguishing this from a
    background wait."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"human_pause_started", "human_pause_ended"}) == "human_pause_started":
            print("SHIP_FEATURE_BLOCKED: a human pause is already open; call human-pause-end first")
            return 1
        message = args.note.strip() if args.note and args.note.strip() else "Human input pause started"
        lib.commit(root, cfg, event_kind="human_pause_started", event_message=message, by=args.by)
    print("HUMAN_PAUSE_STARTED")
    return 0


def cmd_human_pause_end(args) -> int:
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"human_pause_started", "human_pause_ended"}) != "human_pause_started":
            print("SHIP_FEATURE_BLOCKED: no open human pause to end")
            return 1
        message = args.note.strip() if args.note and args.note.strip() else "Human input pause ended"
        lib.commit(root, cfg, event_kind="human_pause_ended", event_message=message, by=args.by)
    print("HUMAN_PAUSE_ENDED")
    return 0


def cmd_design_timing(args) -> int:
    """Read-back over the event log: report design-phase wall-clock time
    by category (active/background_wait/human_wait), per round -- the
    piece that makes 'did the Architect make design faster' answerable
    from a finished or in-flight run, not just 'how long did design take
    total'. Defaults to the live project's own event log; --events-file
    points it at any handsoff-events.jsonl, including an archived one
    under .handsoff-archive/<run>/, so a past run can be re-examined
    exactly like a current one."""
    json_module = __import__("json")
    if args.events_file:
        path = Path(args.events_file)
        if not path.exists():
            print(f"SHIP_FEATURE_BLOCKED: no such events file: {path}")
            return 1
        events = [json_module.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        root = lib.resolve_root(args.root)
        cfg = lib.load_config(root)
        events = lib.read_events(root, cfg)
    summary = lib.summarize_design_timing(events)
    print(json_module.dumps(summary, indent=2))
    return 0


def cmd_verify_log(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        problems = lib.verify_event_log(root, cfg)
        records, verification_problems = lib.load_verifications(root, cfg)
        problems.extend(verification_problems)
        try:
            status = lib.load_unique_json(lib.status_path(root, cfg))
            actual_head = records[-1].get("hash") if records else "GENESIS"
            if status.get("verification_head") != actual_head:
                problems.append("verification ledger tail does not match its anchored head")
        except lib.HandsoffError as exc:
            problems.append(str(exc))
    if problems:
        print("EVENT_LOG_TAMPERED_OR_CORRUPT")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    print("EVENT_LOG_INTACT")
    return 0


def cmd_doctor(args) -> int:
    """Diagnose an interrupted cross-file write and, only when it is
    provably safe, recover from it.

    The two writes that can be caught mid-commit are: (a) status and/or
    acceptance were written but the describing event never got
    appended, and (b) a verification record was appended to the ledger
    but the status.json update that re-anchors verification_head to it
    never landed. Both leave a chain-head mismatch that blocks delivery,
    as documented in the README's known limitations.

    Gap (a) is NOT safe to recover just because the current status/
    acceptance content happens to pass every gate: an arbitrary hand
    edit (change a free-text field, tweak a counter nothing validates
    strictly) can also happen to still validate, and blessing that would
    launder an untracked edit into the audit trail as if it were a real
    crash. So gap (a) is only recovered when the CURRENT file content
    exactly matches a write-ahead journal entry (see commit() in
    handsoff_lib.py) proving it was the intended, in-flight output of a
    real command, not merely a state that passes validation.

    Gap (b) needs no such proof: it only ever patches verification_head
    to a value doctor computes itself from the authenticated ledger, and
    only when status has NOT drifted from the last logged event (i.e.
    gap (a) does not also apply) -- so there is no unproven content
    riding along with the fix.

    Beyond that, this command refuses to touch anything unless: the
    event log's own hash chain and tail anchor are intact, the
    verification ledger's own hash chain is intact, AND the corrected
    state it proposes to write independently passes the same validation
    `advance` would run against it. Any sign of tampering, reordering,
    or an invalid resulting state is reported and left for the operator
    to restore from version control or backup; doctor never guesses its
    way past real damage."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        chain_problems, last_event = lib.event_log_chain_errors(root, cfg)
        records, ledger_problems = lib.load_verifications(root, cfg)
        if chain_problems or ledger_problems:
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: ledger integrity is broken; "
                  "restore the affected log and its anchor from version control or backup.")
            for p in chain_problems:
                print(f"- event log: {p}")
            for p in ledger_problems:
                print(f"- verification ledger: {p}")
            return 1
        status_file = lib.status_path(root, cfg)
        acceptance_file = lib.acceptance_path(root, cfg)
        if not status_file.exists() or not acceptance_file.exists():
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: status or acceptance file is missing; nothing to re-anchor.")
            return 1
        status = lib.load_unique_json(status_file)
        acceptance = lib.load_unique_json(acceptance_file)

        status_sha = lib._file_sha256(status_file)
        acceptance_sha = lib._file_sha256(acceptance_file)
        status_stale = last_event is None or last_event.get("status_sha256") != status_sha
        acceptance_stale = last_event is None or last_event.get("acceptance_sha256") != acceptance_sha
        actual_verification_head = records[-1].get("hash") if records else "GENESIS"
        head_stale = status.get("verification_head") != actual_verification_head

        if not status_stale and not acceptance_stale and not head_stale:
            lib.clear_write_ahead(root)  # prune any orphaned journal from a since-completed write
            print("SHIP_FEATURE_DOCTOR_OK: event log and verification ledger are already anchored "
                  "to the current state; nothing to recover.")
            return 0

        journal = lib.read_write_ahead(root)

        def _proven(stale: bool, key: str, current_sha: str | None) -> bool:
            if journal and key in journal:
                return journal[key] == current_sha
            return not stale

        if not _proven(status_stale, "status_sha256", status_sha) or not _proven(acceptance_stale, "acceptance_sha256", acceptance_sha):
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: the event log has not recorded the current "
                  "status/acceptance state, and no write-ahead journal entry proves this exact "
                  "content was the intended output of a real, interrupted command, or the journal "
                  "names a partial or mismatched transaction that never fully landed on disk. "
                  "Recovering without that proof could launder an untracked hand edit, or bless a "
                  "half-applied write, as a crash instead of fixing one. Restore from version "
                  "control or backup, or redo the command that should have produced this state.")
            return 1

        # Journal-confirmed (or absent) staleness is now proven safe. The
        # verification_head patch is the one piece of content doctor is
        # allowed to originate itself, since it is derived, not chosen.
        proposed_status = dict(status)
        if head_stale:
            proposed_status["verification_head"] = actual_verification_head
        errors = lib.compute_errors(proposed_status, acceptance, cfg,
                                    verifications=records, verification_problems=[])
        if errors:
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: the recoverable state does not independently "
                  "validate, so recovering would paper over a real problem instead of fixing one. "
                  "Resolve these first, or restore from backup:")
            for e in errors:
                print(f"- {e}")
            return 1

        gaps = []
        if status_stale or acceptance_stale:
            gaps.append("event log had not recorded the current, write-ahead-confirmed status/acceptance state")
        if head_stale:
            gaps.append("status.verification_head lagged the verification ledger's actual tail")

        if args.dry_run:
            print("SHIP_FEATURE_DOCTOR_WOULD_RECOVER: " + "; ".join(gaps) + ". "
                  "The resulting state validates cleanly. Re-run without --dry-run to fix.")
            return 0

        # If only the event log lagged, status/acceptance on disk are
        # already exactly right (that is what the journal just proved);
        # re-appending the event (which re-hashes whatever is currently
        # on disk) closes the gap with no file rewrite needed.
        lib.commit(root, cfg, status=proposed_status if head_stale else None,
                  event_kind="recovered",
                  event_message="Doctor re-anchored the ledgers after an interrupted write: " + "; ".join(gaps))
    print("SHIP_FEATURE_DOCTOR_RECOVERED: " + "; ".join(gaps) + ".")
    return 0


def cmd_dashboard(args) -> int:
    """Launch the local, read-only Mission Control dashboard."""
    root = lib.resolve_root(args.root)
    from handsoff_dashboard import serve
    return serve(root, host=args.host, port=args.port, open_browser=not args.no_open)


def main() -> int:
    p = argparse.ArgumentParser(description="Project Handsoff supervisor and gatekeeper")
    p.add_argument("--root", default=None, help="project root (default: nearest ancestor with handsoff.toml, else cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("feature")
    init.add_argument("--item", action="append", default=[],
                      help="declare one issue (#31 or #31 Title) or plain ask; repeat for multiple items")

    sub.add_parser("status")
    sub.add_parser("validate")
    sub.add_parser("verify-log")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--dry-run", action="store_true")

    dashboard = sub.add_parser("dashboard", help="open the local read-only Mission Control dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-open", action="store_true", help="serve without opening a browser")

    verify = sub.add_parser("verify")
    verify.add_argument("--criterion", action="append", required=True,
                        help="criterion id to bind this run to; repeat for more than one")
    verify.add_argument("--by", required=True)

    regression_request = sub.add_parser("regression-request")
    regression_request.add_argument("--group", required=True)
    regression_request.add_argument("--by", required=True)
    regression_request.add_argument("--reason", default="Release confidence requested")

    regression_decide = sub.add_parser("regression-decide")
    regression_decide.add_argument("--request-id", required=True)
    regression_decide.add_argument("--by", required=True)
    regression_choice = regression_decide.add_mutually_exclusive_group(required=True)
    regression_choice.add_argument("--accept", action="store_true")
    regression_choice.add_argument("--decline", action="store_true")

    regression_run = sub.add_parser("regression-run")
    regression_run.add_argument("--request-id", required=True)
    regression_run.add_argument("--by", required=True)

    regression_cancel = sub.add_parser("regression-cancel")
    regression_cancel.add_argument("--request-id", required=True)
    regression_cancel.add_argument("--by", required=True)

    regression_finalize = sub.add_parser("regression-finalize")
    regression_finalize.add_argument("--request-id", required=True)
    regression_finalize.add_argument("--by", required=True)

    work_sync = sub.add_parser("work-items-sync")
    work_sync.add_argument("--by", required=True)
    work_sync.add_argument("--from-tickets", action="store_true")

    work_activate = sub.add_parser("work-item-activate")
    work_activate.add_argument("item")
    work_activate.add_argument("--by", required=True)

    work_update = sub.add_parser("work-item-update")
    work_update.add_argument("item")
    work_update.add_argument("--by", required=True)
    work_update.add_argument("--title")
    work_update.add_argument("--url")
    work_update.add_argument("--github-state", choices=("open", "closed"))
    work_update.add_argument("--notes")
    required_choice = work_update.add_mutually_exclusive_group()
    required_choice.add_argument("--required", action="store_true")
    required_choice.add_argument("--optional", action="store_true")

    evidence = sub.add_parser("record-evidence")
    evidence.add_argument("criterion")
    evidence.add_argument("--kind", choices=("manual", "browser"), required=True)
    evidence.add_argument("--description", required=True)
    evidence.add_argument("--by", required=True)

    symptom = sub.add_parser("record-symptom-resolved")
    symptom.add_argument("--evidence", required=True, help="successful verification run id")
    symptom.add_argument("--by", required=True)

    design_approve = sub.add_parser("design-approve", help="record explicit human approval of the Architect's "
                                    "proposed design/criteria; required before Phase 3+ for any run init flagged")
    design_approve.add_argument("--by", required=True, help="the human approver's identity")
    design_approve.add_argument("--architect", required=True,
                                help="the identity that proposed the design (must differ from --by)")
    design_approve.add_argument("--summary", required=True,
                                help="non-empty design summary (approach/tradeoffs/decisions), recorded as the event message")
    design_approve.add_argument("--redesigns-settled-work", default=None,
                                help="omit for a design that only fits around existing/shipped work; pass a "
                                "non-empty description ONLY when the human explicitly asked to redesign "
                                "already-settled work, making that exception visible in the audit trail")

    design_review = sub.add_parser("record-design-review",
                                   help="record an independent Phase-2 review of the Architect's design")
    design_review.add_argument("--by", required=True, help="independent design reviewer's identity")
    design_review.add_argument("--architect", required=True, help="identity of the Architect being reviewed")
    design_review.add_argument("--summary", required=True, help="review findings or approval rationale")
    design_review_decision = design_review.add_mutually_exclusive_group(required=True)
    design_review_decision.add_argument("--approve", action="store_true")
    design_review_decision.add_argument("--request-changes", action="store_true")

    review = sub.add_parser("record-review")
    review.add_argument("--by", required=True)
    review.add_argument("--symptom-reproduced", choices=("yes", "not_applicable"), default="yes")

    review_start = sub.add_parser("review-attempt-start")
    review_start.add_argument("--by", required=True)
    review_start.add_argument("--reviewer", default=None)
    review_start.add_argument("--trigger", choices=tuple(sorted(lib.REVIEW_ATTEMPT_TRIGGERS)), default=None)
    review_start.add_argument("--note", default=None)

    review_findings = sub.add_parser("record-review-findings")
    review_findings.add_argument("--by", required=True)
    review_findings.add_argument("--finding", action="append", required=True)

    review_override = sub.add_parser("review-cap-override")
    review_override.add_argument("--by", required=True)
    review_override.add_argument("--reason", required=True)

    recover = sub.add_parser("recover")
    recover.add_argument("--by", required=True)
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument("--timeout", type=int, default=3600)

    watch = sub.add_parser("watch")
    watch.add_argument("--by", required=True)
    watch.add_argument("--interval", type=int, default=30)
    watch.add_argument("--timeout", type=int, default=3600)
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--once", action="store_true")

    recovery_ack = sub.add_parser("recovery-acknowledge")
    recovery_ack.add_argument("--by", required=True)
    recovery_ack.add_argument("--reason", required=True)

    live = sub.add_parser("verify-live")
    live.add_argument("--by", required=True)

    heartbeat = sub.add_parser("heartbeat", help="record a liveness signal for a run doing long "
                               "background work, without advancing phase or progress")
    heartbeat.add_argument("--by", required=True)
    heartbeat.add_argument("--note", default=None, help="optional one-line description of the background activity")

    bg_start = sub.add_parser("background-wait-start", help="mark the start of a wait on a background "
                              "task (an async agent, a slow check); also records a heartbeat")
    bg_start.add_argument("--by", required=True)
    bg_start.add_argument("--note", default=None, help="optional one-line description of the background task")
    bg_start.add_argument(
        "--resume-after-authorization", action="store_true",
        help="atomically clear a Phase-2 human authorization hold as the approved background review starts",
    )

    bg_end = sub.add_parser("background-wait-end", help="mark the end of the currently open background-task wait")
    bg_end.add_argument("--by", required=True)
    bg_end.add_argument("--note", default=None)

    hp_start = sub.add_parser("human-pause-start", help="mark the start of a wait on a human, other than "
                              "the design-approval gate (which is tracked automatically)")
    hp_start.add_argument("--by", required=True)
    hp_start.add_argument("--note", default=None, help="optional one-line description of what is being waited on")

    hp_end = sub.add_parser("human-pause-end", help="mark the end of the currently open human-input pause")
    hp_end.add_argument("--by", required=True)
    hp_end.add_argument("--note", default=None)

    timing = sub.add_parser("design-timing", help="report design-phase wall-clock time by category "
                            "(active/background_wait/human_wait) and by round")
    timing.add_argument("--events-file", default=None,
                        help="path to a handsoff-events.jsonl to summarize instead of the live project's own "
                        "(e.g. an archived run under .handsoff-archive/<run>/handsoff-events.jsonl)")

    criterion = sub.add_parser("criterion-update")
    criterion.add_argument("criterion")
    criterion.add_argument("--type", choices=("primary_fix", "supporting"))
    criterion.add_argument("--requirement")
    criterion.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS))
    criterion.add_argument("--test", action="append")
    criterion.add_argument("--state", choices=("failing", "not_tested", "blocked"))

    criterion_add = sub.add_parser("criterion-add")
    criterion_add.add_argument("criterion")
    criterion_add.add_argument("--type", choices=("primary_fix", "supporting"), required=True)
    criterion_add.add_argument("--requirement", required=True)
    criterion_add.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS), required=True)
    criterion_add.add_argument("--test", action="append", required=True)

    criterion_remove = sub.add_parser("criterion-remove")
    criterion_remove.add_argument("criterion")

    adv = sub.add_parser("advance")
    adv.add_argument("phase", type=int)
    adv.add_argument("progress", type=int)
    adv.add_argument("--status", default=None)
    adv.add_argument("--implemented-by", default=None)
    adv.add_argument("--design-round", type=int, default=None,
                     help="explicitly set design_round to this value (override; use --new-design-round "
                     "for normal organic tracking)")
    adv.add_argument("--new-design-round", action="store_true",
                     help="increment design_round by 1 from its current on-disk value and record a "
                     "distinct design_round_advanced event; only valid when advancing to phase 2 "
                     "(Design debate); mutually exclusive with --design-round")
    adv.add_argument("--design-round-reason", default=None,
                     help="optional short reason/what triggered the new design round; only valid "
                     "together with --new-design-round")
    adv.add_argument("--review-round", type=int, default=None)
    adv.add_argument("--dry-run", action="store_true")
    adv.add_argument("--next-action", default=None,
                     help="override the phase-appropriate default next_action message")
    adv.add_argument("--authorization-hold", choices=("design_review",), default=None,
                     help="tag an explicit Phase-2 blocked wait for reviewer authorization")

    gate = sub.add_parser("deployment-gate")
    gate.add_argument("--approve", action="store_true")
    gate.add_argument("--by", default=None)

    args = p.parse_args()
    handlers = {
        "init": cmd_init, "status": cmd_status, "validate": cmd_validate,
        "advance": cmd_advance, "deployment-gate": cmd_deployment_gate,
        "verify": cmd_verify, "verify-log": cmd_verify_log, "doctor": cmd_doctor,
        "regression-request": cmd_regression_request,
        "regression-decide": cmd_regression_decide,
        "regression-run": cmd_regression_run,
        "regression-cancel": cmd_regression_cancel,
        "regression-finalize": cmd_regression_finalize,
        "work-items-sync": cmd_work_items_sync,
        "work-item-activate": cmd_work_item_activate,
        "work-item-update": cmd_work_item_update,
        "dashboard": cmd_dashboard,
        "record-evidence": cmd_record_evidence,
        "record-symptom-resolved": cmd_record_symptom,
        "design-approve": cmd_design_approve,
        "record-design-review": cmd_record_design_review,
        "record-review": cmd_record_review,
        "review-attempt-start": cmd_review_attempt_start,
        "record-review-findings": cmd_record_review_findings,
        "review-cap-override": cmd_review_cap_override,
        "recover": cmd_recover,
        "watch": cmd_watch,
        "recovery-acknowledge": cmd_recovery_acknowledge,
        "verify-live": cmd_verify_live,
        "heartbeat": cmd_heartbeat,
        "background-wait-start": cmd_background_wait_start,
        "background-wait-end": cmd_background_wait_end,
        "human-pause-start": cmd_human_pause_start,
        "human-pause-end": cmd_human_pause_end,
        "design-timing": cmd_design_timing,
        "criterion-update": cmd_criterion_update,
        "criterion-add": cmd_criterion_add,
        "criterion-remove": cmd_criterion_remove,
    }
    try:
        return handlers[args.command](args)
    except lib.HandsoffError as e:
        print(f"SHIP_FEATURE_BLOCKED: {e}")
        return 1
    except Exception as e:  # last-resort: a clean refusal beats a raw traceback
        print(f"SHIP_FEATURE_BLOCKED: unexpected error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
