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
import json
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_analyzer as analyzer  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_tranche as tranche  # noqa: E402

# One inventory for the command line, broker, and Mission Control. A command
# may only be exposed to the Pilot when it is explicitly classified here;
# agent protocol writes never become browser mutations by accident.
OPERATION_REGISTRY = {
    "init": {"class": "operator-facing", "surface": "mission-init-form"},
    "status": {"class": "diagnostic", "surface": "dashboard"},
    "validate": {"class": "diagnostic", "surface": "audit-state"},
    "verify-log": {"class": "diagnostic", "surface": "audit-state"},
    "doctor": {"class": "operator-facing", "surface": "audit-state"},
    "dashboard": {"class": "diagnostic", "surface": "dashboard"},
    "verify": {"class": "operator-facing", "surface": "verification-list"},
    "release-plan": {"class": "operator-facing", "surface": "regression-alert"},
    "regression-request": {"class": "operator-facing", "surface": "regression-alert"},
    "regression-decide": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "regression-run": {"class": "automatic", "surface": "regression-alert"},
    "regression-cancel": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "regression-finalize": {"class": "automatic", "surface": "regression-alert"},
    "work-items-sync": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-activate": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-update": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-remove": {"class": "agent-only", "surface": "ticket-panel"},
    "lane-request": {"class": "agent-only", "surface": "ticket-panel"},
    "lane-confirm": {"class": "operator-facing", "surface": "ticket-panel"},
    "plan-tranche": {"class": "diagnostic", "surface": "tranche-panel"},
    "tranche-approve": {"class": "operator-facing", "surface": "tranche-panel"},
    "record-evidence": {"class": "agent-only", "surface": "verification-list"},
    "record-symptom-resolved": {"class": "agent-only", "surface": "acceptance-score"},
    "design-approve": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-reject": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "record-design-review": {"class": "agent-only", "surface": "review-attempts-panel"},
    "design-review-packet": {"class": "automatic", "surface": "design-review-packet"},
    "design-propose": {"class": "automatic", "surface": "design-review-packet"},
    "design-review-authorize": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-review-escalate": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "record-review": {"class": "agent-only", "surface": "review-attempts-panel"},
    "review-attempt-start": {"class": "agent-only", "surface": "review-attempts-panel"},
    "record-review-findings": {"class": "agent-only", "surface": "review-attempts-panel"},
    "review-cap-override": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "recover": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "watch": {"class": "automatic", "surface": "live-status"},
    "recovery-acknowledge": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "verify-live": {"class": "operator-facing", "surface": "verification-list"},
    "heartbeat": {"class": "automatic", "surface": "live-status"},
    "background-wait-start": {"class": "automatic", "surface": "live-status"},
    "background-wait-end": {"class": "automatic", "surface": "live-status"},
    "human-pause-start": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "human-pause-end": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-timing": {"class": "diagnostic", "surface": "metrics-panel"},
    "design-evidence": {"class": "agent-only", "surface": "design-evidence-panel"},
    "criterion-update": {"class": "agent-only", "surface": "criteria-list"},
    "criterion-add": {"class": "agent-only", "surface": "criteria-list"},
    "criterion-remove": {"class": "agent-only", "surface": "criteria-list"},
    "criteria-apply": {"class": "agent-only", "surface": "criteria-list"},
    "amendment-open": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-revise": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-review": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-approve": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "amendment-escalate": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "question-raise": {"class": "agent-only", "surface": "questions-panel"},
    "question-answer": {"class": "operator-facing", "surface": "questions-panel"},
    "analyze-archives": {"class": "automatic", "surface": "flight-log"},
    "pilot-note": {"class": "operator-facing", "surface": "pilot-note-form"},
    "run-close": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "run-reopen": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "advance": {"class": "agent-only", "surface": "phase-rail"},
    "deployment-gate": {"class": "operator-facing", "surface": "operator-actions-panel"},
}


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


def _invalidate_decisions(status: dict, *, rollback_to: int = 5, invalidate_design: bool = False) -> list[str]:
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
    revoked = []
    for key in ("review", "deployment_approved", "live_verification_id"):
        if status.get(key) is not None:
            revoked.append(key)
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
        for key in ("design_approved", "design_review", "design_proposal"):
            if status.get(key) is not None:
                revoked.append(key)
        if "design_review" in status or status.get("requires_design_review"):
            status["design_review"] = None
        status["design_approved"] = None
        status["design_proposal"] = None
        if (status.get("requires_design_approval") or status.get("requires_design_review")) \
                and status.get("phase_number", 1) >= 3:
            status["phase_number"] = 2
            status["phase"] = lib.PHASES[2]
            status["status"] = "in_progress"
            status["progress"] = min(status.get("progress", 0), 20)
    if status.get("phase_number") == 2:
        status["next_action"] = "Architect revises the design and requests review again"
    return revoked


def _approval_edit_guard(status: dict, revoke: bool) -> bool:
    """Require an explicit opt-in before registry edits destroy an approved design.

    The guard runs before any acceptance mutation, preserving the exact
    refusal symptom and making a declined edit observably no-op.
    """
    if status.get("design_approved") and not revoke:
        print("SHIP_FEATURE_BLOCKED: design is approved; this edit would revoke design approval, the independent review, and the proposal. Use amendment-open for a reviewed change, or pass --revoke-approval to proceed deliberately")
        return True
    return False


def _durable_results(results: list[dict]) -> list[dict]:
    """Persist redacted tails only for failed checks, never for successes.

    Failed checks need bounded diagnosis, but successful output needs no
    diagnosis and may echo secrets, so successful records stay tail-less.
    """
    durable = []
    for result in results:
        item = {k: v for k, v in result.items() if k != "output_tail"}
        if result.get("exit_code", 0) != 0 or result.get("timed_out"):
            tail = result.get("output_tail", "")
            item["output_tail"] = lib.redact_output_text(tail)[-lib.CHECK_OUTPUT_TAIL_CHARS:]
        durable.append(item)
    return durable


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
            "last_heartbeat_owner": None, "background_wait": None,
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
            "design_review_attempts": 0, "design_review_authorization": None,
            "amendment": None, "amendments": [], "pending_questions": [],
            "review": None, "live_verification_id": None, "original_symptom_evidence_id": None,
            "verification_head": "GENESIS",
            "requirement_coverage": {"passing": 0, "failing": 1, "not_tested": 0, "blocked": 0,
                                      "original_symptom_resolved": False},
            "reviewer_checklist": {"symptom_reproduced": "not_verifiable", "symptom_resolved": "not_verifiable",
                                   "all_criteria_verified": "no", "evidence_attached": "no"},
            "events": [],
        }
        status["work_item_delivery"] = lib.new_work_item_delivery(
            acceptance["work_items"], getattr(args, "lane", "full"),
        )
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
                                    verification_problems=verification_problems, root=root)
        # #41: the same activity reading the dashboard snapshot carries.
        liveness = lib.liveness_view(status, root, cfg)
        warning = liveness["stall_warning"]
        lib.record_stall_transition(root, cfg, warning)
        activity = None
        live = lib.live_status(status, cfg, root)
        budget = lib.design_review_budget(status, cfg)
        reviewer_selection = lib.design_reviewer_selection_view(cfg, status, acceptance)
        log_problems = lib.verify_event_log(root, cfg)
    print(__import__("json").dumps({
        "root": str(root), "feature": status.get("feature"), "phase": status.get("phase"),
        "engine": lib.runtime_identity(root),
        "phase_number": status.get("phase_number"), "progress": status.get("progress"),
        "status": status.get("status"), "next_action": status.get("next_action"),
        "design_round": status.get("design_round"), "review_round": status.get("review_round"),
        "review_attempts": [{k: item.get(k) for k in ("attempt", "attempt_id", "trigger", "disposition", "reviewer")}
                            for item in (status.get("review_attempts") or [])],
        "effective_max_review_rounds": lib.effective_review_cap(status, cfg)
        if "review_attempts" in status else cfg.get("max_review_rounds"),
        "escalation": status.get("escalation"),
        "design_review": status.get("design_review"),
        "design_review_attempts": budget["attempts"], "design_review_budget": budget,
        "design_reviewer_selection": reviewer_selection,
        "consistency_errors": reviewer_selection.get("consistency_errors", []),
        "amendment": lib.amendment_view(status, acceptance, cfg, verifications),
        "reviewed_by": status.get("reviewed_by"),
        "live_verification_id": status.get("live_verification_id"),
        "verification_runs": len(verifications),
        "validation": "blocked" if errors or log_problems else "valid", "errors": errors,
        "evidence_drift": lib.evidence_drift(root, cfg, acceptance, verifications),
        "stall_warning": warning, "activity_note": activity, "activity": liveness, "live": live,
        "process_signal": liveness["process_signal"],
        "questions": lib.questions_view(status),
        "unattributed_criteria": lib.derive_work_items(status, acceptance, cfg)["unattributed_criteria"],
        "crew": lib.crew_view(cfg),
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
                                    verification_problems=verification_problems, root=root)
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
        small_fix_jump = (
            current == 1 and args.phase == 4
            and not lib.full_design_required(status, acceptance, cfg)
        )
        if args.phase < current or (args.phase > current + 1 and not small_fix_jump):
            print(f"phase transition blocked: current={current}, requested={args.phase} (one step at a time)")
            return 1
        current_progress = status.get("progress", 0)
        progress = current_progress if args.progress is None else args.progress
        if args.progress is not None and args.progress < current_progress:
            progress = current_progress

        # Build the PROPOSED status and validate THAT, before writing
        # anything. This is the fix for the original bug: validating the
        # status already on disk can never catch the transition about to
        # happen, because the phase-6+ checks only fire once phase_number
        # already reads 6+, which is one write too late.
        proposed = deepcopy(status)
        proposed["phase_number"] = args.phase
        proposed["phase"] = lib.PHASES[args.phase]
        proposed["progress"] = progress
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
            active = proposed.get("active_work_item")
            delivery = proposed.get("work_item_delivery") or {}
            if active in delivery:
                delivery[active]["implemented_by"] = args.implemented_by
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

        if "work_item_delivery" in proposed:
            # The command's monotonic progress value is the operator's
            # explicit transition contract. Item-level progress remains a
            # dashboard view and must not silently rewrite an omitted value.
            proposed["progress"] = progress
        proposed["_preserve_progress"] = True

        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
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
        progress_was_clamped = args.progress is not None and args.progress < current_progress

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
                      phase_number=args.phase, progress=progress, **new_design_round_event)
        else:
            lib.commit(root, cfg, status=proposed,
                      event_kind="phase_advanced", event_message=f"Advanced to {lib.PHASES[args.phase]}",
                      phase_number=args.phase, progress=progress)

        archived = False
        if args.phase == 8 and proposed.get("status") == "complete":
            # The phase transition above already committed successfully; an
            # archive failure (e.g. an unwritable Documents folder) must not
            # be reported as if the run itself failed.
            try:
                fresh_verifications, _ = lib.load_verifications(root, cfg)
                archive_path = lib.archive_run(root, cfg, proposed, acceptance,
                                               fresh_verifications, lib.read_events(root, cfg))
                print(f"HANDSOFF_ARCHIVED: {archive_path}")
                archived = True
            except OSError as exc:
                print(f"HANDSOFF_ARCHIVE_FAILED (run still completed successfully): {exc}")
    if args.phase == 8 and proposed.get("status") == "complete":
        # #49: outside the lock like the dashboard release below (the scan
        # may talk to GitHub); the completion event takes the lock itself.
        if archived:
            _analyze_after_archive(root, cfg)
        # #40: outside the project lock on purpose. The dashboard's own
        # event stream takes that lock every 0.2 s, so holding it here
        # would keep the server from ever noticing the stop request.
        _release_run_dashboard(root, cfg)
    if args.progress is not None and args.progress < progress:
        print(f"NOTE: progress kept at {current_progress} (requested {args.progress} is lower)")
    print("SHIP_FEATURE_ADVANCED")
    return 0


def advance_approved_design(root: Path) -> bool:
    """Advance the completed design gate without spending an agent turn.

    The check is intentionally narrow: only a Phase-2 run whose current
    design review and human approval both pass their hash-bound gates is
    eligible. ``cmd_advance`` remains the single transition implementation
    and revalidates the state under its own lock, so a concurrent mutation
    cannot bypass normal workflow checks.
    """
    root = root.resolve()
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
        if int(status.get("phase_number", 0) or 0) != 2:
            return False
        if lib._design_errors(status, acceptance, cfg) \
                or lib._design_review_errors(status, acceptance, cfg):
            return False
        progress = max(30, int(status.get("progress", 0) or 0))
    args = argparse.Namespace(
        root=str(root), phase=3, progress=progress, status="in_progress",
        implemented_by=None, design_round=None, new_design_round=False,
        design_round_reason=None, review_round=None, dry_run=False,
        next_action=None, authorization_hold=None,
    )
    result = cmd_advance(args)
    if result != 0:
        raise lib.HandsoffError("approved design could not advance to Phase 3")
    return True


def _analyze_after_archive(root, cfg) -> None:
    """#49: scan the archive (the record just written included) right after
    the Phase 8 archive write, when [analysis] enabled is true, and record
    archive_scan_completed on this run's live ledger. Any failure at all is
    reported and never fails the advance: the transition and the archive
    are already committed."""
    if not (cfg.get("analysis") or {}).get("enabled", True):
        return
    try:
        report = analyzer.scan(root, cfg)
        with lib.project_lock(root):
            lib.commit(root, cfg, event_kind="archive_scan_completed",
                      event_message=f"Archive scan completed: {len(report['findings'])} finding(s), "
                                    f"{len(report['filed'])} filed",
                      **analyzer.scan_event_fields(report))
        print(f"HANDSOFF_ANALYSIS_REPORT: {report['report_path']}")
    except Exception as exc:  # noqa: BLE001 - a scan must never fail a completed run
        print(f"HANDSOFF_ANALYSIS_FAILED (run still completed successfully): {type(exc).__name__}: {exc}")


def cmd_analyze_archives(args) -> int:
    """#49: the same scan the Phase 8 trigger runs, on demand. Prints the
    report path. --dry-run files nothing; --archive-dir overrides the
    archive location for this scan only."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    report = analyzer.scan(root, cfg, archive_directory=args.archive_dir, dry_run=args.dry_run)
    print(f"HANDSOFF_ANALYSIS_REPORT: {report['report_path']}")
    print(json.dumps({
        "report_path": report["report_path"],
        "filing": report["filing"],
        "findings": [{"rule": f["rule"], "title": f["title"], "run_ids": f["run_ids"], "numbers": f["numbers"],
                      "excluded": f["excluded"]} for f in report["findings"]],
        "filed": report["filed"],
        "suppressed": report["suppressed"],
        "not_filed": report["not_filed"],
        "runs": {key: len(value) for key, value in report["runs"].items()},
    }, indent=2))
    return 0


def cmd_pilot_note(args) -> int:
    """#49: record a pilot_note event on the current run. The next archive
    scan lists each distinct note as an R7 finding with its run id."""
    root = lib.resolve_root(args.root)
    record = lib.record_pilot_note(root, by=args.by, text=args.text)
    print(f"PILOT_NOTE_RECORDED: {len(record['text'])} characters by {record['by']}")
    return 0


def _release_run_dashboard(root, cfg) -> None:
    """Release the dashboard this run owns (#40), if any, and log what
    happened. Like the archive step, a failure here is reported and never
    turns an already-committed completion into a failed advance."""
    try:
        release = lib.release_run_dashboard(root)
        with lib.project_lock(root):
            if release.get("released"):
                lib.commit(root, cfg, event_kind="dashboard_released",
                          event_message=f"Run-owned dashboard on port {release.get('port')} released",
                          port=release.get("port"), pid=release.get("pid"), reason=release.get("reason"))
                print(f"HANDSOFF_DASHBOARD_RELEASED: port {release.get('port')}")
            else:
                lib.commit(root, cfg, event_kind="dashboard_release_skipped",
                          event_message=f"Run-owned dashboard release skipped: {release.get('reason')}",
                          reason=release.get("reason"))
                print(f"HANDSOFF_DASHBOARD_RELEASE_SKIPPED: {release.get('reason')}")
    except (lib.HandsoffError, OSError) as exc:
        print(f"HANDSOFF_DASHBOARD_RELEASE_FAILED (run still completed successfully): {exc}")


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
        if args.revoke:
            phase = int(status.get("phase_number", 0) or 0)
            if not isinstance(status.get("deployment_approved"), dict):
                print("DEPLOYMENT_REVOKE_BLOCKED\n- deployment approval has not been recorded")
                return 1
            if phase not in {7, 8} or status.get("live_verification_id"):
                print("DEPLOYMENT_REVOKE_BLOCKED\n- revoke requires Phase 7 or 8 without a live verification id")
                return 1
            proposed = dict(status)
            proposed["deployment_approved"] = None
            proposed["phase_number"] = 7
            proposed["phase"] = lib.PHASES[7]
            proposed["status"] = "awaiting_approval"
            proposed["next_action"] = "Await explicit deployment approval."
            proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.commit(root, cfg, status=proposed, event_kind="deployment_revoked",
                       event_message=args.reason, by=args.by, reason=args.reason)
            print("DEPLOYMENT_REVOKED")
            return 0
        refusal = lib.amendment_decision_refusal(status, "deployment approval")
        if refusal:
            print(f"DEPLOYMENT_BLOCKED\n- {refusal}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
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
            verification_problems=verification_problems, root=root,
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


def cmd_design_reject(args) -> int:
    """Pilot rejects the currently reviewed design and returns it for revision."""
    actor = lib.validate_agent_actor(args.by)
    reason = str(args.reason or "").strip()
    if not reason:
        print("SHIP_FEATURE_BLOCKED: design rejection requires --reason")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        review = status.get("design_review")
        if status.get("phase_number") != 2 or not isinstance(review, dict) \
                or review.get("decision") != "approved" or status.get("design_approved"):
            print("SHIP_FEATURE_BLOCKED: no reviewed, unapproved design is awaiting the Pilot")
            return 1
        rejected_hash = review.get("design_hash")
        status["design_review"] = None
        status["design_approved"] = None
        status["status"] = "in_progress"
        status["next_action"] = f"Pilot requested design revision: {reason}"
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="design_rejected",
                   event_message=f"Pilot rejected the reviewed design: {reason}",
                   by=actor, reason=reason, design_hash=rejected_hash)
    print("DESIGN_REJECTED")
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
        "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
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
    if lib.verify_event_log(root, cfg):
        return False
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
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        before_items, _ = lib.effective_work_items(acceptance, cfg)
        before_scope = lib.work_item_scope_hash(before_items, acceptance.get("criteria", []))
        derived = lib.derive_work_item_registry(acceptance, cfg, explicit_items=args.item)
        existing = acceptance.get("work_items")
        if isinstance(existing, list):
            by_id = {item["id"]: item for item in existing}
            criterion_ids = {lib.criterion_work_item_id(criterion)
                             for criterion in acceptance.get("criteria", [])}
            criterion_ids.discard(None)
            for item in derived:
                current = by_id.get(item["id"])
                if current is None:
                    if not args.item and any(candidate.get("kind") == "issue" for candidate in existing) \
                            and item.get("kind") == "ask" and item["id"] not in criterion_ids:
                        print(f"WORK_ITEM_SYNC_SKIPPED: {item['id']} (feature-title ask not added beside explicit issue items; pass --item to add it deliberately)")
                        continue
                    existing.append(item)
                elif args.from_tickets:
                    current["title"], current["url"] = item["title"], item["url"]
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
            persisted = existing
        else:
            persisted = derived
        acceptance["work_items"] = persisted
        after_scope = lib.work_item_scope_hash(persisted, acceptance.get("criteria", []))
        if isinstance(status.get("deployment_approved"), dict) and before_scope != after_scope:
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
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
        if not isinstance(status.get("work_item_delivery"), dict):
            status["work_item_delivery"] = lib.new_work_item_delivery(persisted, "full")
            for record in status["work_item_delivery"].values():
                record["implemented_by"] = status.get("implemented_by")
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
        if status.get("tranche_approval"):
            rows = lib.derive_work_items(status, acceptance, cfg)["items"]
            next_item = next((item["id"] for item in rows
                              if item.get("required", True) and item.get("status") != "done"), None)
            if next_item and args.item != next_item:
                print(f"SHIP_FEATURE_BLOCKED: approved tranche order requires {next_item} next")
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
        before_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        deployment_approved = status.get("deployment_approved")
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
        if getattr(args, "implemented_by", None) is not None:
            delivery = status.get("work_item_delivery") or {}
            if args.item not in delivery:
                print(f"SHIP_FEATURE_BLOCKED: work item {args.item} has no delivery record")
                return 1
            delivery[args.item]["implemented_by"] = lib.validate_agent_actor(args.implemented_by)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        after_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        if isinstance(deployment_approved, dict) and before_scope != after_scope:
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
        if isinstance(deployment_approved, dict) and before_scope != after_scope:
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


def cmd_work_item_remove(args) -> int:
    """Remove a zero-criteria item while preserving the approval audit trail."""
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items = acceptance.get("work_items")
        if not isinstance(items, list):
            print("SHIP_FEATURE_BLOCKED: synchronize work items before removing them")
            return 1
        item = next((candidate for candidate in items if candidate.get("id") == args.item), None)
        if item is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        row = next(row for row in lib.derive_work_items(status, acceptance, cfg)["items"]
                   if row["id"] == args.item)
        if row.get("criteria"):
            criteria = ", ".join(row["criteria"])
            print(f"SHIP_FEATURE_BLOCKED: work item {args.item} maps to criteria {criteria}; retag or remove them first")
            return 1
        post_approval = isinstance(status.get("deployment_approved"), dict)
        if post_approval and row.get("criteria"):
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
        # A removable item carries no criteria, so by construction it is
        # outside the reviewed scope: the digest is unchanged and no
        # decision is invalidated (#82). The assertion keeps that invariant
        # honest if the scope definition ever drifts.
        before_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        acceptance["work_items"] = [candidate for candidate in items if candidate.get("id") != args.item]
        delivery = status.get("work_item_delivery")
        if isinstance(delivery, dict):
            delivery.pop(args.item, None)
        after_scope = lib.work_item_scope_hash(acceptance["work_items"], acceptance.get("criteria", []))
        if not post_approval and before_scope != after_scope:
            print(f"SHIP_FEATURE_BLOCKED: removing {args.item} would change the reviewed scope; retag its criteria first")
            return 1
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_item_removed", event_message=f"Removed work item {args.item}",
                   by=actor, work_item=args.item, scope_changed=False, post_approval=post_approval)
    print(f"WORK_ITEM_REMOVED: {args.item}")
    return 0


def cmd_lane_request(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        delivery = status.get("work_item_delivery") or {}
        if args.item not in delivery:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
        if not facts["eligible"]:
            print("SMALL_FIX_REFUSED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
            return 1
        record = delivery[args.item]
        record.update(requested_lane="small-fix", facts=facts,
                      baseline_head=lib.repository_snapshot(root)["head"], escalation_reason=None)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="work_item_lane_requested",
                   event_message=f"Requested small-fix lane for {args.item}",
                   by=actor, work_item=args.item, facts=facts)
    print(f"SMALL_FIX_REQUESTED: {args.item}")
    return 0


def cmd_lane_confirm(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        delivery = status.get("work_item_delivery") or {}
        record = delivery.get(args.item)
        if not isinstance(record, dict) or record.get("requested_lane") != "small-fix":
            print(f"SHIP_FEATURE_BLOCKED: {args.item} has no eligible small-fix request")
            return 1
        facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
        if not facts["eligible"]:
            print("SMALL_FIX_REFUSED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
            return 1
        now = datetime.now(timezone.utc).isoformat()
        record.update(lane="small-fix", confirmed_by=actor, confirmed_at=now,
                      facts=facts, escalation_reason=None)
        status.update(updated_at=now,
                      next_action="Implement the confirmed small fix and attach targeted evidence.")
        lib.commit(root, cfg, status=status, event_kind="work_item_lane_confirmed",
                   event_message=f"Pilot confirmed small-fix lane for {args.item}",
                   by=actor, work_item=args.item, facts=facts)
    print(f"SMALL_FIX_CONFIRMED: {args.item}")
    return 0


def cmd_plan_tranche(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        if args.issues_file:
            issues = json.loads(Path(args.issues_file).read_text(encoding="utf-8"))
            if not isinstance(issues, list):
                raise lib.HandsoffError("issues file must contain a JSON array")
        else:
            issues = tranche.fetch_issues(args.repo)
        archive_directory = Path(args.archive_dir).expanduser() if args.archive_dir else lib.archive_dir()
        proposal = tranche.build_proposal(issues, tranche.load_archives(archive_directory),
                                          args.repo, cfg, limit=args.limit)
        path = root / tranche.PROPOSAL_FILE
        lib._atomic_write_text(path, json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, lib.HandsoffError) as exc:
        print(f"TRANCHE_PLAN_BLOCKED: {exc}")
        return 1
    print(json.dumps({"proposal_path": str(path), **proposal}, indent=2))
    return 0


def cmd_tranche_approve(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        if int(status.get("phase_number", 1) or 1) != 1:
            print("TRANCHE_APPROVAL_BLOCKED: backlog tranches can only be approved in Phase 1")
            return 1
        path = root / tranche.PROPOSAL_FILE
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("TRANCHE_APPROVAL_BLOCKED: proposal artifact is missing or invalid")
            return 1
        if args.proposal_hash != proposal.get("proposal_hash") \
                or tranche.proposal_hash(proposal) != proposal.get("proposal_hash"):
            print("TRANCHE_APPROVAL_BLOCKED: proposal hash is stale or invalid")
            return 1
        proposed = list(proposal.get("proposed_order") or [])
        order = list(args.item or proposed)
        drops = list(args.drop or [])
        by_id = {f"issue-{row['number']}": row for row in proposal.get("issues", [])
                 if isinstance(row, dict) and isinstance(row.get("number"), int)}
        if len(proposed) != len(set(proposed)) or set(proposed) != set(by_id):
            print("TRANCHE_APPROVAL_BLOCKED: proposal item inventory is invalid")
            return 1
        if len(set(order)) != len(order) or len(set(drops)) != len(drops) \
                or set(order) & set(drops) or set(order) | set(drops) != set(proposed):
            print("TRANCHE_APPROVAL_BLOCKED: retained order and explicit drops must partition the proposal once")
            return 1
        existing = {item["id"]: item for item in acceptance.get("work_items", [])}
        now = datetime.now(timezone.utc).isoformat()
        persisted = []
        for item_id in order:
            source = by_id[item_id]
            previous = existing.get(item_id, {})
            persisted.append({
                "id": item_id, "kind": "issue", "number": source["number"],
                "title": source["title"], "url": source.get("url", ""), "required": True,
                "github_state": "open", "github_checked_at": now,
                "created_at": previous.get("created_at", now), "updated_at": now,
                "notes": previous.get("notes", ""),
            })
        proposed_acceptance = deepcopy(acceptance)
        proposed_acceptance["work_items"] = persisted
        errors = lib.validate_acceptance_schema(proposed_acceptance)
        if errors:
            print("TRANCHE_APPROVAL_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        proposed_status = deepcopy(status)
        old_delivery = proposed_status.get("work_item_delivery") or {}
        proposed_status["work_item_delivery"] = {
            item_id: old_delivery.get(item_id, lib.new_work_item_delivery([{"id": item_id}])[item_id])
            for item_id in order
        }
        proposed_status["tranche_approval"] = {
            "proposal_hash": args.proposal_hash, "proposed_order": proposed,
            "approved_order": order, "drops": drops, "by": actor, "at": now,
            "labels": {item_id: by_id[item_id].get("labels", []) for item_id in order},
        }
        proposed_status["active_work_item"] = order[0] if order else None
        proposed_status["updated_at"] = now
        lib.commit(root, cfg, status=proposed_status, acceptance=proposed_acceptance,
                   event_kind="tranche_approved", event_message="Pilot approved backlog tranche",
                   proposal_hash=args.proposal_hash, proposed_order=proposed,
                   approved_order=order, drops=drops, by=actor)
    print(f"TRANCHE_APPROVED: {', '.join(order) or 'empty'}")
    return 0


def cmd_release_plan(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    plan = lib.release_plan_payload(
        cfg, args.version, actor, override_reason=args.full_regression_override_reason,
    )
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        active = lib.active_regression_request(status)
        if active and active.get("state") in {"awaiting_approval", "accepted"}:
            active["state"] = "invalidated"
            active["completed_at"] = plan["planned_at"]
        status["release_plan"] = plan
        status["updated_at"] = plan["planned_at"]
        lib.commit(
            root, cfg, status=status, event_kind="release_planned",
            event_message=f"{plan['release_class'].capitalize()} release {plan['version']} planned",
            version=plan["version"], release_class=plan["release_class"],
            full_regression_eligible=plan["full_regression_eligible"], by=actor,
        )
    print(json.dumps(plan, indent=2))
    return 0


def cmd_regression_request(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    group = lib.regression_group(cfg, args.group)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        plan = status.get("release_plan")
        if cfg["regression_gate"].get("full_regression_major_only", True):
            if not isinstance(plan, dict):
                print("REGRESSION_BLOCKED: record a semantic release-plan before requesting a full regression")
                return 1
            if not plan.get("full_regression_eligible"):
                print(f"REGRESSION_BLOCKED: {plan.get('release_class')} releases use targeted tests; "
                      "record an explicit full-regression override reason to proceed")
                return 1
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
            "release_version": plan.get("version") if isinstance(plan, dict) else None,
            "release_class": plan.get("release_class") if isinstance(plan, dict) else None,
            "policy_override_reason": plan.get("full_regression_override_reason")
            if isinstance(plan, dict) else None,
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
    once per invocation, for efficiency when criteria share a test.

    #43: each needed command is bound to the exact state it proves
    something about (lib.verification_binding). A command whose binding
    already has an eligible executed, passing record in this run is not
    launched again unless --no-cache is passed; its criterion gets a
    record marked executed false with reused_from naming the source. The
    binding locks under .handsoff-verify-inflight/ serialize concurrent
    verifies of one binding so the second re-reads the ledger and reuses
    instead of launching a duplicate."""
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
        # #43: bind before launching, so the binding describes the state
        # the command actually ran against, not whatever it left behind.
        run_hash = lib.feature_hash(status, lib.read_events(root, cfg))
        repo_digest = lib.repository_digest(root, cfg)
        config_digest = lib.verification_config_hash(cfg)
        bindings = {
            cmd: lib.verification_binding(cmd, repo_digest, config_digest,
                                          [before[c["id"]] for c in criteria if cmd in c.get("tests", [])])
            for cmd in needed
        }
    use_cache = not getattr(args, "no_cache", False)
    with lib.verify_inflight_lock(root, list(bindings.values())):
        reused: dict[str, dict] = {}
        if use_cache:
            with lib.project_lock(root):
                # Re-read under the binding lock: a concurrent verify of the
                # same binding that held it before us has appended by now.
                ledger_records, _ = lib.load_verifications(root, cfg)
            for cmd in needed:
                source = lib.reusable_check_record(ledger_records, cmd, bindings[cmd], run_hash)
                if source is not None:
                    reused[cmd] = source
        launched = [cmd for cmd in needed if cmd not in reused]
        results = lib.run_checks(cfg, root, commands=launched) if launched else []
        results_by_command = {r["command"]: r for r in results}
        for cmd, source in reused.items():
            copied = next(r for r in source["results"] if r.get("command") == cmd)
            results_by_command[cmd] = {**copied, "reused_from": source["run_id"]}
        results = [results_by_command[cmd] for cmd in needed]
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
                own_tests = list(criterion.get("tests", []))
                own_results = [results_by_command[t] for t in own_tests]
                own_ok = bool(own_results) and all(r["exit_code"] == 0 for r in own_results)
                own_sources = {reused[t]["run_id"] for t in own_tests if t in reused}
                # A record is executed only when every one of its commands
                # was launched here; a partially reused multi-command record
                # is not a reuse source, and names its source only when all
                # of its reused results came from the same record (each
                # copied result carries its own reused_from regardless).
                executed = not own_sources
                record_digest = repo_digest if executed else next(
                    (reused[t].get("repository_digest") for t in own_tests if t in reused), None)
                reused_from = next(iter(own_sources)) if len(own_sources) == 1 else None
                record = lib.append_verification(
                    root, cfg, kind="checks", ok=own_ok, by=args.by,
                                         criteria=[criterion], results=_durable_results(own_results),
                    commands=own_tests,
                    binding={t: bindings[t] for t in own_tests}, executed=executed,
                    reused_from=reused_from, feature_hash=run_hash,
                    repository_digest=record_digest, config_digest=config_digest)
                status["verification_head"] = record["hash"]
                if record["run_id"] not in criterion["evidence"]:
                    criterion["evidence"].append(record["run_id"])
                if not own_ok:
                    criterion["state"] = "failing"
                elif lib.criterion_fully_evidenced(criterion, existing_records + [record]):
                    criterion["state"] = "passing"
                else:
                    criterion["state"] = "not_tested"
                per_criterion[criterion["id"]] = {"run_id": record["run_id"], "ok": own_ok,
                                                  "executed": executed, "reused_from": reused_from}
            lib.sync_coverage(status, acceptance)
            _invalidate_decisions(status)
            review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.commit(root, cfg, status=status, acceptance=acceptance,
                      event_kind="checks_run", event_message="Ran and attached configured checks",
                      criteria=per_criterion,
                      review_attempt_refreshed=review_refresh,
                      launched_count=len(launched), reused_count=len(reused),
                      results=[{"command": r["command"], "exit_code": r["exit_code"],
                                "output_sha256": r["output_sha256"]} for r in results])
    overall_ok = all(v["ok"] for v in per_criterion.values())
    print(__import__("json").dumps({
        "ok": overall_ok, "criteria": per_criterion, "results": results,
        "launched": launched, "reused": {cmd: source["run_id"] for cmd, source in reused.items()},
    }, indent=2))
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
        review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="evidence_recorded", event_message="Attached criterion evidence",
                  run_id=record["run_id"], criterion=args.criterion, by=args.by,
                  review_attempt_refreshed=review_refresh)
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
        rows = lib.derive_work_items(status, acceptance, cfg)["items"]
        missing = [row["id"] for row in rows if row.get("required", True) and not row.get("criteria")]
        if missing:
            print("SHIP_FEATURE_BLOCKED: required work items have no criteria: "
                  f"{', '.join(missing)}; tag a criterion [#N] or work-item-remove them")
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
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "redesigns_settled_work": args.redesigns_settled_work,
            "proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
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
    _print_unverifiable_test_warnings(acceptance, cfg)
    return 0


def _reviewer_session_error(status: dict, session_id: str | None, reviewer: str) -> str | None:
    """Bind a host-recorded verdict to the exact current live Reviewer."""
    if session_id is None:
        return None
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    session = sessions.get(session_id)
    if not isinstance(session, dict) or session.get("role") != "reviewer" \
            or current.get("reviewer") != session_id \
            or session.get("state") not in lib.AGENT_SESSION_LIVE_STATES:
        return "--session must name the current live managed Reviewer session"
    if str(session.get("actor") or "").casefold() != reviewer.strip().casefold():
        return "--by must match the managed Reviewer session actor"
    return None


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
    structural_blocker = bool(getattr(args, "structural_blocker", False))
    if structural_blocker and not args.request_changes:
        print("SHIP_FEATURE_BLOCKED: --structural-blocker is only valid with --request-changes")
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
        session_error = _reviewer_session_error(status, getattr(args, "session", None), args.by)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
        provenance = (status.get("design_proposal") or {}).get("provenance")
        if isinstance(provenance, dict):
            if args.by.strip().casefold() == str(provenance.get("actor") or "").casefold():
                print("SHIP_FEATURE_BLOCKED: design reviewer must differ from the proposal architect actor")
                return 1
            launch_id = getattr(args, "session", None)
            launch_session = (status.get("agent_sessions") or {}).get(launch_id)
            if launch_session and provenance.get("host_session_id") and \
                    launch_session.get("host_session_id") == provenance.get("host_session_id"):
                print("SHIP_FEATURE_BLOCKED: design reviewer must use an independent host session")
                return 1
        criteria = acceptance.get("criteria", [])
        if any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT and c.get("tests") == lib.PLACEHOLDER_TESTS
               for c in criteria):
            print("SHIP_FEATURE_BLOCKED: acceptance registry still contains init's untouched placeholder "
                  "criterion; author a real criterion before design review")
            return 1

        # #35: every record, approve or request-changes, is one attempt
        # against the autonomous budget; past it the Pilot authorizes one
        # attempt at a time. The refusal names the exact command.
        budget = lib.design_review_budget(status, cfg)
        if budget["exhausted"]:
            print(f"SHIP_FEATURE_BLOCKED: {lib.design_review_budget_exhausted_message(budget)}")
            return 1

        # #36: bounded findings carry ids F<attempt>.<n>; the record also
        # binds the commit it was made at so a later packet can tell
        # whether the repository moved underneath the prior review.
        try:
            findings = lib.validate_design_review_findings(args.finding, budget["next_attempt"])
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        try:
            head = lib.repository_snapshot(root)["head"]
        except lib.HandsoffError:
            head = None

        # #37: which reviewer tier this attempt was made under, selected by
        # the same pure precedence a managed launch uses (attempt number,
        # follow-up config, open escalation, last structural blocker,
        # criteria structure), from the state as it stands right now.
        tier, tier_reason = lib.select_design_reviewer_tier(cfg, status, acceptance)
        try:
            tier_profile = lib.design_reviewer_tier_profile(cfg, tier, require_available=False)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        reviewer_profile = {"adapter": tier_profile["adapter"], "model": tier_profile["model"],
                            "tier": tier, "reason": tier_reason}

        now = datetime.now(timezone.utc).isoformat()
        decision = "approved" if args.approve else "changes_requested"
        status["design_review"] = {
            "at": now,
            "by": args.by.strip(),
            "architect": args.architect.strip(),
            "decision": decision,
            "summary": args.summary.strip(),
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "attempt": budget["next_attempt"],
            "head": head,
            "findings": findings,
            "structural_blocker": structural_blocker,
            "reviewer_profile": reviewer_profile,
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
        }
        lib.append_design_review_history(
            status, lib.design_review_history_entry(status["design_review"], criteria,
                                                    structural_blocker=structural_blocker))
        status.pop("authorization_hold", None)
        status["updated_at"] = now
        status["design_review_attempts"] = budget["next_attempt"]
        # An unconsumed authorization is spent by this record whether a
        # managed session reserved it (launch_session_id set, kept as the
        # audit trail of which session performed the attempt) or a human
        # recorded the review directly (launch_session_id null).
        authorized = budget["authorized"]
        if authorized:
            status["design_review_authorization"]["consumed_at"] = now
        # #37: a Pilot escalation buys exactly one primary-tier attempt; this
        # record is that attempt, so the escalation is spent here.
        escalation = status.get("design_reviewer_escalation")
        escalation_consumed = isinstance(escalation, dict) and escalation.get("consumed_at") is None
        if escalation_consumed:
            status["design_reviewer_escalation"]["consumed_at"] = now
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
        review_event = {
            "kind": event_kind, "message": message,
            "by": args.by.strip(), "architect": args.architect.strip(), "decision": decision,
            "design_hash": status["design_review"]["design_hash"],
            "design_review_attempt": budget["next_attempt"],
            "design_review_limit": budget["limit"],
            "authorized": authorized,
            "head": head,
            "findings": len(findings),
            "structural_blocker": structural_blocker,
            "reviewer_tier": tier,
            "reviewer_selection_reason": tier_reason,
            "reviewer_profile": {"adapter": reviewer_profile["adapter"], "model": reviewer_profile["model"]},
            "escalation_consumed": escalation_consumed,
        }
        after = lib.design_review_budget(status, cfg)
        if decision == "changes_requested" and after["exhausted"]:
            # The run cannot request another review on its own. Phase 3
            # stays closed (the gate still sees changes_requested); the
            # Pilot's command is the next action, verbatim, so status and
            # the Mission Control banner both carry it.
            status["status"] = "blocked"
            status["authorization_hold"] = "design_review"
            status["next_action"] = lib.design_review_budget_exhausted_message(after)
            lib.commit(root, cfg, status=status, extra_events=[review_event],
                      event_kind="design_review_budget_exhausted",
                      event_message=f"Design review budget exhausted ({after['attempts']}/{after['limit']}); "
                                    "Pilot authorization required for any further attempt",
                      design_review_attempts=after["attempts"], design_review_limit=after["limit"],
                      reviewer_session_id=getattr(args, "session", None))
        else:
            fields = {k: v for k, v in review_event.items() if k not in ("kind", "message")}
            lib.commit(root, cfg, status=status, event_kind=event_kind, event_message=message,
                       reviewer_session_id=getattr(args, "session", None), **fields)
    print("DESIGN_REVIEW_APPROVED" if args.approve else "DESIGN_CHANGES_REQUESTED")
    return 0


def cmd_design_propose(args) -> int:
    """Record a JSON proposal supplied by the host-driven Architect."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if cfg["agents"].get("architect") != lib.HOST_AGENT_ADAPTER:
        print('SHIP_FEATURE_BLOCKED: design-propose is only for a host Architect; set [agents].architect = "host"')
        return 1
    try:
        actor = lib.validate_agent_actor(args.by)
        value = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError, lib.HandsoffError) as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    status = lib.load_unique_json(lib.status_path(root, cfg))
    if int(status.get("phase_number", 1) or 1) not in {1, 2}:
        print("SHIP_FEATURE_BLOCKED: design proposal can only be recorded in Phase 1 or Phase 2")
        return 1
    try:
        recorded = lib.record_design_proposal(root, None, value, architect_actor=actor)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    print(f"DESIGN_PROPOSAL_RECORDED: {recorded['proposal_hash']}")
    _print_unverifiable_test_warnings(acceptance, cfg)
    return 0


def cmd_design_review_packet(args) -> int:
    """#36: generate the delta review packet for the next design-review
    attempt from the most recent recorded review. Refused before any review
    (the first review gets the full task) and outside Phase 2. Every
    disposition refusal names the offending value and writes nothing."""
    json_module = __import__("json")
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
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
            print("SHIP_FEATURE_BLOCKED: a delta review packet can only be generated in Phase 2")
            return 1
        budget = lib.design_review_budget(status, cfg)
        if budget["attempts"] == 0:
            print("SHIP_FEATURE_BLOCKED: no design review has been recorded yet; the first review "
                  "receives the full task, not a delta packet")
            return 1
        if not status.get("design_review_history"):
            print("SHIP_FEATURE_BLOCKED: no design review history is recorded; record-design-review "
                  "must run on this version before a delta packet can be built")
            return 1
        try:
            dispositions = lib.parse_design_review_dispositions(
                args.disposition, lib.latest_design_review_findings(status))
            packet = lib.build_design_review_packet(root, cfg, status, acceptance, dispositions)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        size = lib.design_review_packet_bytes(packet)
        status["design_review_packet"] = packet
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        delta = packet["criteria_delta"]
        lib.commit(
            root, cfg, status=status, event_kind="design_review_packet_generated",
            event_message=f"Delta review packet {packet['packet_id']} generated for design-review "
                          f"attempt {packet['attempt']}",
            by=args.by.strip(), packet_id=packet["packet_id"], attempt=packet["attempt"],
            previous_attempt=packet["previous_attempt"], design_hash=packet["design_hash"],
            previous_design_hash=packet["previous_design_hash"], head=packet["repository"]["head"],
            previous_head=packet["previous_head"], bytes=size, stale=packet["stale"],
            truncated=bool(packet.get("truncated")),
            counts={
                "findings": len(packet["findings"]),
                **{name: len(ids) for name, ids in packet["dispositions"].items()},
                "new_findings": len(packet["new_findings_since"]),
                "criteria_added": len(delta["added"]), "criteria_removed": len(delta["removed"]),
                "criteria_changed": len(delta["changed"]),
                "criteria_unchanged": (len(delta["unchanged"]) if "unchanged" in delta
                                       else delta.get("unchanged_count", 0)),
                "evidence": len(packet["evidence"]),
                "files_changed": (len(packet["files_changed_since_previous"])
                                  if packet["files_changed_since_previous"] is not None else None),
            },
        )
    print(f"DESIGN_REVIEW_PACKET_GENERATED: {packet['packet_id']} attempt {packet['attempt']} ({size} bytes)")
    print(json_module.dumps(packet, indent=2, sort_keys=True))
    return 0


def cmd_design_review_authorize(args) -> int:
    """#35: the Pilot permits exactly one more design-review attempt past
    the autonomous budget. Human-only (the broker refuses it). Refuses
    while the budget is not exhausted (nothing to authorize) and while an
    earlier authorization is still unconsumed (one at a time). Never
    touches design_review_attempts: only a recorded review counts."""
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
        if isinstance(status.get("run_closed"), dict):
            print("SHIP_FEATURE_BLOCKED: run is closed; reopen it before authorizing a design review")
            return 1
        budget = lib.design_review_budget(status, cfg)
        if budget["authorized"]:
            print("SHIP_FEATURE_BLOCKED: an unconsumed design-review authorization already exists "
                  f"(attempt {status['design_review_authorization']['attempt_permitted']}); "
                  "record-design-review must consume it before another can be granted")
            return 1
        if budget["attempts"] < budget["limit"]:
            print(f"SHIP_FEATURE_BLOCKED: design review budget is not exhausted "
                  f"({budget['attempts']}/{budget['limit']}); nothing to authorize")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        note = args.note.strip() if args.note and args.note.strip() else None
        status["design_review_authorization"] = {
            "by": args.by.strip(), "at": now, "note": note,
            "attempt_permitted": budget["next_attempt"],
            "launch_session_id": None, "consumed_at": None,
        }
        status["status"] = "in_progress"
        status.pop("authorization_hold", None)
        status["next_action"] = f"Launch the authorized design-review attempt {budget['next_attempt']}"
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="design_review_attempt_authorized",
                  event_message=note or f"Pilot authorized design-review attempt {budget['next_attempt']}",
                  by=args.by.strip(), attempt_permitted=budget["next_attempt"],
                  design_review_attempts=budget["attempts"], design_review_limit=budget["limit"])
    print(f"DESIGN_REVIEW_ATTEMPT_AUTHORIZED: {budget['next_attempt']}")
    return 0


def cmd_design_review_escalate(args) -> int:
    """#37: the Pilot forces the NEXT design-review attempt onto the primary
    reviewer tier regardless of what the delta check would select. Human-only
    (the broker refuses it). Refused outside Phase 2 and while an earlier
    escalation is still unconsumed (one at a time); consumed by the next
    record-design-review. Never touches design_review_attempts."""
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
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: a design reviewer escalation can only be recorded in Phase 2")
            return 1
        escalation = status.get("design_reviewer_escalation")
        if isinstance(escalation, dict) and escalation.get("consumed_at") is None:
            print("SHIP_FEATURE_BLOCKED: an unconsumed design reviewer escalation already exists "
                  f"(by {escalation['by']} at {escalation['at']}); record-design-review must consume "
                  "it before another can be recorded")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        note = args.note.strip() if args.note and args.note.strip() else None
        status["design_reviewer_escalation"] = {
            "by": args.by.strip(), "at": now, "note": note, "consumed_at": None,
        }
        status["updated_at"] = now
        budget = lib.design_review_budget(status, cfg)
        lib.commit(root, cfg, status=status, event_kind="design_reviewer_escalated",
                  event_message=note or f"Pilot escalated design-review attempt {budget['next_attempt']} "
                                        "to the primary reviewer tier",
                  by=args.by.strip(), tier="primary", reason="pilot_escalation",
                  design_review_attempt=budget["next_attempt"],
                  design_review_attempts=budget["attempts"])
    print(f"DESIGN_REVIEWER_ESCALATED: attempt {budget['next_attempt']} will use the primary reviewer tier")
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
        if status.get("phase_number", 0) < 4:
            print("SHIP_FEATURE_BLOCKED: review findings require Phase 4 or later")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), reviewer)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
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
        current_acceptance_hash = lib.acceptance_hash(acceptance.get("criteria", []))
        acceptance_changed = attempt.get("acceptance_hash") != current_acceptance_hash
        attempt["reviewer"] = reviewer
        attempt["findings"] = findings
        attempt["tests_executed"] = args.tests_executed
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
        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=proposed, extra_events=extra,
                   event_kind="review_attempt_closed",
                   event_message=f"Review attempt {attempt['attempt']} requested changes",
                   by=reviewer, attempt_id=attempt["attempt_id"], attempt=attempt["attempt"],
                   disposition="changes_requested", findings=findings,
                   reviewer_session_id=getattr(args, "session", None),
                   reviewed_acceptance_hash=attempt.get("acceptance_hash"),
                   current_acceptance_hash=current_acceptance_hash,
                   acceptance_changed=acceptance_changed)
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
        if _refuse_if_amendment_open(status, action="independent review"):
            return 1
        if getattr(args, "item", None):
            delivery = status.get("work_item_delivery") or {}
            record = delivery.get(args.item)
            if not isinstance(record, dict) or record.get("lane") != "small-fix" \
                    or not record.get("confirmed_by"):
                print(f"SHIP_FEATURE_BLOCKED: {args.item} is not a confirmed small-fix lane")
                return 1
            facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
            if not facts["eligible"]:
                record.update(lane="escalated", facts=facts,
                              escalation_reason="; ".join(facts["reasons"]),
                              reviewed_by=None, review_hash=None)
                status.update(phase_number=2, phase=lib.PHASES[2], status="blocked",
                              updated_at=datetime.now(timezone.utc).isoformat(),
                              next_action=f"Design the escalated work item {args.item} in the full lane.")
                lib.commit(root, cfg, status=status, event_kind="work_item_lane_escalated",
                           event_message=f"Small-fix lane escalated for {args.item}",
                           by=reviewer_id, work_item=args.item, reasons=facts["reasons"])
                print("SMALL_FIX_ESCALATED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
                return 1
            own = lib.item_criteria(acceptance, args.item)
            if not own or any(c.get("state") != "passing" or not c.get("evidence") for c in own):
                print(f"SHIP_FEATURE_BLOCKED: {args.item} criteria require passing evidence")
                return 1
            implementer = record.get("implemented_by")
            if not implementer:
                print(f"SHIP_FEATURE_BLOCKED: {args.item} has no recorded implementer")
                return 1
            if reviewer_id.casefold() == implementer.strip().casefold():
                print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
                return 1
            item_hash = lib.item_acceptance_hash(acceptance, args.item)
            if record.get("reviewed_by") and record.get("review_hash") == item_hash:
                print(f"INDEPENDENT_ITEM_REVIEW_ALREADY_RECORDED: {args.item}")
                return 0
            now = datetime.now(timezone.utc).isoformat()
            record.update(reviewed_by=reviewer_id, review_hash=item_hash, facts=facts)
            status["updated_at"] = now
            lib.commit(root, cfg, status=status, event_kind="work_item_review_approved",
                       event_message=f"Independent review approved {args.item}",
                       by=reviewer_id, implementer=implementer, work_item=args.item,
                       acceptance_hash=item_hash)
            print(f"INDEPENDENT_ITEM_REVIEW_RECORDED: {args.item}")
            return 0
        if status.get("phase_number", 0) < 5:
            print("SHIP_FEATURE_BLOCKED: independent review can only be recorded in Phase 5 or later")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), reviewer_id)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
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
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "implementer_profile": implementer_profile,
            "reviewer_profile": reviewer_profile,
            "tests_executed": args.tests_executed,
            "profiles_distinct": profiles_distinct,
            "checklist": {"symptom_reproduced": args.symptom_reproduced,
                          "symptom_resolved": "yes", "all_criteria_verified": "yes",
                          "evidence_attached": "yes"},
        }
        preflight["reviewed_by"] = reviewer_id
        errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                    verification_problems=problems, root=root)
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
        attempt["tests_executed"] = args.tests_executed
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, extra_events=[{
                      "kind": "review_attempt_closed",
                      "message": f"Review attempt {attempt['attempt']} approved",
                      "attempt_id": attempt["attempt_id"], "attempt": attempt["attempt"],
                      "disposition": "approved", "reviewer": reviewer_id,
                  }],
                  event_kind="review_approved", event_message="Independent review approved current acceptance",
                  by=reviewer_id, acceptance_hash=status["review"]["acceptance_hash"],
                  reviewer_session_id=getattr(args, "session", None),
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
        if _refuse_if_amendment_open(status, action="live verification"):
            return 1
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
                                         commands=commands,
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
    return lib.sync_work_item_registry(acceptance, cfg)


def _criterion_needs_item_warning(criterion: dict, acceptance: dict, cfg: dict) -> bool:
    items, _ = lib.effective_work_items(acceptance, cfg)
    return len(items) > 1 and lib.criterion_work_item_id(criterion) is None


def _print_unverifiable_test_warnings(acceptance: dict, cfg: dict) -> None:
    """Warn when design gates name automated tests verify cannot run."""
    configured = set(cfg.get("check_commands", []))
    for criterion in acceptance.get("criteria", []):
        if "checks" not in lib.VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
            continue
        for test in criterion.get("tests", []):
            if test not in configured:
                print(f"WARNING: criterion {criterion.get('id')} test {test!r} matches no [checks].commands entry; verify will refuse it")


def cmd_criterion_update(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if all(getattr(args, field) is None for field in ("requirement", "verification", "type", "test", "state")):
            print("SHIP_FEATURE_BLOCKED: criterion-update requires at least one change")
            return 1
        fields = {field: getattr(args, field) for field in ("requirement", "verification", "type", "state")
                  if getattr(args, field) is not None}
        if args.test is not None:
            fields["tests"] = args.test
        problems = lib.validate_criterion_fields(fields)
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
        was_primary = criterion.get("type") == "primary_fix"
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
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
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_updated", event_message="Acceptance criterion updated",
                  criterion=args.criterion, decisions_revoked=decisions_revoked, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])))
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
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        if _criterion(acceptance, args.criterion):
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} already exists")
            return 1
        problems = lib.validate_criterion_fields({
            "id": args.criterion, "type": args.type, "requirement": args.requirement,
            "verification": args.verification, "tests": args.test or [],
        }, require_all=True)
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
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
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_added", event_message="Acceptance criterion added",
                  criterion=args.criterion, decisions_revoked=decisions_revoked, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])))
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
        if _refuse_if_amendment_open(status):
            return 1
        problems = lib.validate_criterion_fields({"id": args.criterion})
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
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
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_removed", event_message="Acceptance criterion removed",
                  criterion=args.criterion)
    print("CRITERION_REMOVED")
    return 0


def cmd_criteria_apply(args) -> int:
    """#44: apply a whole list of add/update/remove operations as one
    lock-protected commit, or refuse the whole list with nothing written.
    The planner (lib.plan_criteria_transaction) does every check on a deep
    copy; only a complete plan reaches the single commit() below, which
    carries status, acceptance, and one criteria_transaction_applied event.
    Decisions are invalidated exactly once, the same way one
    criterion-update does it (design cleared, flagged Phase 3+ run back to
    Phase 2 in the same status write, no phase_advanced event)."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        plan = lib.plan_criteria_transaction(acceptance, cfg, operations, root=root)
        if args.dry_run:
            print(__import__("json").dumps(lib.criteria_plan_preview(plan, status), indent=2, sort_keys=True))
            return 0
        lib.apply_criteria_plan(acceptance, plan)
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        untagged = [record["id"] for record in plan["operations"] if record["op"] != "remove"
                    and _criterion_needs_item_warning(_criterion(acceptance, record["id"]), acceptance, cfg)]
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criteria_transaction_applied",
                  event_message=f"Acceptance criteria transaction applied ({plan['operation_count']} operations)",
                  by=args.by, operations=plan["operations"],
                  registry_hash_before=plan["registry_hash_before"],
                  registry_hash_after=plan["registry_hash_after"],
                  design_hash_after=plan["design_hash_after"],
                  operation_count=plan["operation_count"],
                  decisions_revoked=decisions_revoked,
                  work_item_scope_changed=plan["work_item_scope_changed"])
    if untagged:
        print(f"WORK_ITEM_WARNING: untagged criteria in a multi-item run: {', '.join(untagged)}")
    print(f"CRITERIA_TRANSACTION_APPLIED: {plan['operation_count']} operations, "
          f"registry {plan['registry_hash_after'][:12]}")
    return 0



def _refuse_if_amendment_open(status: dict, *, action: str | None = None) -> int | None:
    """#42: the freeze. Criterion mutations (no `action`) and the decision
    commands (`action` named) are refused while an amendment is open;
    verify and record-evidence are deliberately not routed through here."""
    message = lib.amendment_decision_refusal(status, action) if action else lib.amendment_mutation_refusal(status)
    if message is None:
        return None
    print(f"SHIP_FEATURE_BLOCKED: {message}")
    return 1


def _amendment_event_fields(record: dict) -> dict:
    """What every amendment event carries: ids, hashes, classification,
    never criterion text or test commands."""
    return {
        "amendment_id": record["amendment_id"], "amendment_hash": record["amendment_hash"],
        "base_design_hash": record["base_design_hash"], "resulting_design_hash": record["resulting_design_hash"],
        "changed_ids": list(record["changed_ids"]), "dependent_ids": list(record["dependent_ids"]),
        "affected_work_items": list(record["affected_work_items"]),
        "classification": record["classification"], "classification_reasons": list(record["classification_reasons"]),
        "operation_count": len(record["operations"]),
    }


def _print_full_redesign_refusal(record: dict, *, revise: bool) -> int:
    print("AMENDMENT_REFUSED: full_redesign")
    print("\n".join(f"- {reason}" for reason in record["classification_reasons"]))
    if revise:
        print("This follow-up widens the open amendment beyond a scoped correction. Run "
              f"`amendment-escalate --by ACTOR --reason TEXT` on {record['amendment_id']}, which takes the full "
              "path (design decisions cleared, Phase 2), then apply the change with criteria-apply.")
    else:
        print("A scoped amendment cannot carry this change. Use the ordinary path instead: "
              "`criteria-apply --file TX.json --by ACTOR` (or criterion-update), which invalidates the design "
              "approval and returns the run to Phase 2 for a full redesign.")
    return 1


def _close_amendment(status: dict, record: dict, *, state: str, now: str) -> None:
    record["state"] = state
    record["closed_at"] = now
    history = [item for item in (status.get("amendments") or []) if isinstance(item, dict)]
    history.append(record)
    status["amendments"] = history[-lib.MAX_AMENDMENT_HISTORY:]
    status["amendment"] = None


def cmd_amendment_open(args) -> int:
    """#42: open a scoped post-approval amendment. The #44 planner builds
    the delta, `lib.classify_amendment` decides scoped or full_redesign
    (the caller cannot choose), and only a scoped delta is applied: the
    changed criteria reset to not_tested with evidence cleared, review/
    deployment/live decisions invalidated (design decisions kept on their
    base hash), the phase frozen, one commit, one `amendment_opened`."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        planned = lib.plan_amendment(status, acceptance, cfg, operations, by=args.by, root=root,
                                     request_full_redesign=bool(args.request_full_redesign))
        record, plan = planned["record"], planned["plan"]
        if record["classification"] != "scoped":
            return _print_full_redesign_refusal(record, revise=False)
        lib.apply_criteria_plan(acceptance, plan)
        lib.reset_amended_criteria(acceptance, record["changed_ids"])
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        record["frozen_phase"] = int(status.get("phase_number", 0) or 0)
        record["frozen_progress"] = status.get("progress", 0)
        status["amendment"] = record
        status.setdefault("amendments", [])
        status["next_action"] = (f"Amendment {record['amendment_id']} is open: record amendment-review, then "
                                 "the Pilot records amendment-approve (or escalate it).")
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_status_schema(status) + lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        summary = args.summary.strip() if args.summary and args.summary.strip() else None
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="amendment_opened",
                  event_message=summary or f"Scoped amendment {record['amendment_id']} opened "
                                           f"({len(record['changed_ids'])} criteria changed)",
                  by=record["by"], frozen_phase=record["frozen_phase"], frozen_progress=record["frozen_progress"],
                  registry_hash_before=plan["registry_hash_before"], registry_hash_after=plan["registry_hash_after"],
                  **_amendment_event_fields(record))
    print(f"AMENDMENT_OPENED: {record['amendment_id']} scoped, {len(record['changed_ids'])} criteria changed, "
          f"frozen at Phase {record['frozen_phase']}")
    return 0


def cmd_amendment_revise(args) -> int:
    """#42: a follow-up transaction on the open amendment after the reviewer
    requested changes (or before any review). Same classification over the
    cumulative delta; an expansion to full_redesign refuses and points at
    amendment-escalate. Recomputes the amendment hash and clears the stale
    review in the same commit."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        if lib.open_amendment(status) is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open; use amendment-open")
            return 1
        planned = lib.plan_amendment_revision(status, acceptance, cfg, operations, root=root)
        record, plan = planned["record"], planned["plan"]
        if record["classification"] != "scoped":
            return _print_full_redesign_refusal(record, revise=True)
        lib.apply_criteria_plan(acceptance, plan)
        lib.reset_amended_criteria(acceptance, record["changed_ids"])
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        status["amendment"] = record
        status["next_action"] = (f"Amendment {record['amendment_id']} was revised: record amendment-review again, "
                                 "then the Pilot records amendment-approve (or escalate it).")
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_status_schema(status) + lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="amendment_revised",
                  event_message=f"Amendment {record['amendment_id']} revised "
                                f"({plan['operation_count']} more operations; review cleared)",
                  by=args.by.strip(),
                  registry_hash_before=plan["registry_hash_before"], registry_hash_after=plan["registry_hash_after"],
                  **_amendment_event_fields(record))
    print(f"AMENDMENT_REVISED: {record['amendment_id']} now {len(record['changed_ids'])} criteria changed; "
          "review cleared")
    return 0


def cmd_amendment_review(args) -> int:
    """#42: the independent review of the open amendment, bound to the
    amendment hash recomputed from the registry on disk. The reviewer must
    differ from the amendment's author and from the architect on record."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    reviewer = args.by.strip()
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        architects = {amendment.get("by")}
        for field in ("design_approved", "design_review"):
            record = status.get(field)
            if isinstance(record, dict) and record.get("architect"):
                architects.add(record["architect"])
        if any(isinstance(a, str) and a.strip().casefold() == reviewer.casefold() for a in architects):
            print("SHIP_FEATURE_BLOCKED: amendment reviewer must differ from the amendment's author and the "
                  "architect on record, no self-review")
            return 1
        recomputed, problems = lib.recompute_amendment_hash(amendment, acceptance.get("criteria", []))
        if recomputed is None:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- amendment hash: {p}" for p in problems))
            return 1
        now = datetime.now(timezone.utc).isoformat()
        decision = "approved" if args.approve else "changes_requested"
        amendment["review"] = {"by": reviewer, "at": now, "decision": decision,
                               "summary": args.summary.strip(), "amendment_hash": recomputed}
        if decision == "approved":
            status["next_action"] = (f"Amendment {amendment['amendment_id']} review approved: the Pilot records "
                                     "amendment-approve to resume the frozen phase.")
        else:
            status["next_action"] = (f"Amendment {amendment['amendment_id']} review requested changes: the "
                                     "Architect applies amendment-revise or escalates with amendment-escalate.")
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="amendment_reviewed",
                  event_message=f"Amendment {amendment['amendment_id']} review {decision.replace('_', ' ')}: "
                                f"{args.summary.strip()}",
                  by=reviewer, decision=decision, amendment_id=amendment["amendment_id"],
                  amendment_hash=recomputed)
    print("AMENDMENT_REVIEW_APPROVED" if args.approve else "AMENDMENT_CHANGES_REQUESTED")
    return 0


def cmd_amendment_approve(args) -> int:
    """#42: the Pilot's approval of the reviewed amendment (human-only; the
    broker refuses it). Requires an approved review bound to the hash
    recomputed from the registry on disk right now; on success the design
    decisions are rewritten to the resulting design hash with the
    amendment id appended to their `amended_by` trail, the amendment
    closes into the history, and the run resumes the frozen phase and
    progress with no phase_advanced event."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    pilot = args.by.strip()
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        if isinstance(amendment.get("by"), str) and amendment["by"].strip().casefold() == pilot.casefold():
            print("SHIP_FEATURE_BLOCKED: approver must differ from the amendment's author, no self-approval")
            return 1
        review = amendment.get("review")
        if not isinstance(review, dict):
            print("SHIP_FEATURE_BLOCKED: the amendment has no review yet; record amendment-review first")
            return 1
        if review.get("decision") != "approved":
            print("SHIP_FEATURE_BLOCKED: the amendment review requested changes; revise (amendment-revise) "
                  "or escalate (amendment-escalate)")
            return 1
        recomputed, problems = lib.recompute_amendment_hash(amendment, acceptance.get("criteria", []))
        if recomputed is None or recomputed != review.get("amendment_hash"):
            problems = problems or ["the reviewed amendment hash does not match the recomputed hash"]
            return _print_audit_block([f"amendment hash: {p}" for p in problems]
                                      + ["amendment approval refused; record a new amendment-review against "
                                         "the registry on disk, or escalate"])
        now = datetime.now(timezone.utc).isoformat()
        scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
        rewritten = []
        for field in ("design_approved", "design_review"):
            record = status.get(field)
            if not isinstance(record, dict):
                continue
            record["design_hash"] = amendment["resulting_design_hash"]
            if "scope_hash" in record:
                record["scope_hash"] = scope
            trail = list(record.get("amended_by") or [])
            trail.append(amendment["amendment_id"])
            record["amended_by"] = trail[-lib.MAX_AMENDMENT_HISTORY:]
            rewritten.append(field)
        amendment["pilot_approval"] = {"by": pilot, "at": now, "amendment_hash": recomputed}
        closed = deepcopy(amendment)
        _close_amendment(status, closed, state="approved", now=now)
        frozen_phase = int(closed["frozen_phase"])
        status["phase_number"] = frozen_phase
        status["phase"] = lib.PHASES[frozen_phase]
        status["progress"] = closed["frozen_progress"]
        status["next_action"] = lib.NEXT_ACTION_DEFAULTS.get(frozen_phase, status.get("next_action"))
        status["updated_at"] = now
        errors = lib.compute_errors(status, acceptance, cfg, verifications=records,
                                    verification_problems=verification_problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        lib.commit(root, cfg, status=status, event_kind="amendment_approved",
                  event_message=f"Amendment {closed['amendment_id']} approved by the Pilot; design decisions "
                                f"rewritten to the amended design, Phase {frozen_phase} resumed",
                  by=pilot, reviewer=review.get("by"), rewritten=rewritten,
                  frozen_phase=frozen_phase, frozen_progress=closed["frozen_progress"],
                  **_amendment_event_fields(closed))
    print(f"AMENDMENT_APPROVED: {closed['amendment_id']} closed; Phase {frozen_phase} resumed")
    return 0


def cmd_question_raise(args) -> int:
    """#46: a role (or the Supervisor on its behalf) raises a question for
    the Pilot. From the role's current live managed session it blocks the
    run with the question as next_action; otherwise it is recorded and shown
    without blocking. Managed children normally use the
    `HANDSOFF_QUESTION: <text>` stdout line instead of this command."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    try:
        record = lib.raise_question(root, role=args.role, text=args.text,
                                    session_id=args.session, by=args.by.strip())
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(f"QUESTION_RAISED: {record['question_id']} ({'blocking' if record['blocking'] else 'non-blocking'})")
    return 0


def cmd_question_answer(args) -> int:
    """#46: the Pilot answers; the block lifts once no blocking question is
    open, and the answer reaches the role on its next launch. #48: `--batch
    FILE` records several answers ({"answers": [{"question_id", "choice"} |
    {"question_id", "other"}]}) in one commit; any bad entry refuses the
    whole file and writes nothing."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    batch = getattr(args, "batch", None)
    single = args.id is not None or args.text is not None
    if batch is None and (args.id is None or args.text is None):
        print("SHIP_FEATURE_BLOCKED: question-answer needs --id and --text, or --batch FILE")
        return 1
    if batch is not None and single:
        print("SHIP_FEATURE_BLOCKED: --batch cannot be combined with --id or --text")
        return 1
    root = lib.resolve_root(args.root)
    try:
        if batch is not None:
            try:
                payload = json.loads(Path(batch).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise lib.HandsoffError(f"batch file could not be read as JSON: {exc}") from exc
            records = lib.answer_questions_batch(root, answers=lib.question_answers_from_payload(payload),
                                                 by=args.by.strip())
            print(f"QUESTIONS_ANSWERED: {' '.join(r['question_id'] for r in records)}")
            return 0
        record = lib.answer_question(root, question_id=args.id, by=args.by.strip(), text=args.text)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(f"QUESTION_ANSWERED: {record['question_id']}")
    return 0


def cmd_amendment_escalate(args) -> int:
    """#42: give up on the scoped lane. The amendment closes as
    `escalated` and the run takes the full path: design decisions cleared
    and, for a flagged run, Phase 2 (the same rollback a criterion
    mutation performs). The amended registry stays as it is."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        closed = deepcopy(amendment)
        _close_amendment(status, closed, state="escalated", now=now)
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["next_action"] = ("Amendment escalated to a full redesign: revise the design, request a new "
                                 "independent design review and human approval, then advance to Phase 3.")
        status["updated_at"] = now
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, extra_events=extra, event_kind="amendment_escalated",
                  event_message=f"Amendment {closed['amendment_id']} escalated to a full redesign: "
                                f"{args.reason.strip()}",
                  by=args.by.strip(), reason=args.reason.strip(),
                  phase_number=status["phase_number"], **_amendment_event_fields(closed))
    print(f"AMENDMENT_ESCALATED: {closed['amendment_id']} closed; run at Phase {status['phase_number']}")
    return 0


def cmd_recover(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.dry_run:
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            assessment = lib.recovery_assessment(
                status, cfg, lib.read_session_liveness(root), lib.read_events(root, cfg), root=root,
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
        status["next_action"] = lib.NEXT_ACTION_DEFAULTS.get(
            int(status.get("phase_number", 1) or 1), "Resume the current mission phase."
        )
        source = next((item for item in reversed(status.get("recovery_attempts") or [])
                       if item.get("recovery_id") == escalation.get("source")), None)
        acknowledged_session_id = None
        if isinstance(source, dict):
            acknowledged_session_id = source.get("to_session_id") or source.get("from_session_id")
        lib.commit(root, cfg, status=status, event_kind="recovery_acknowledged",
                   event_message=args.reason.strip(), by=actor,
                   session_id=acknowledged_session_id,
                   role=source.get("role") if isinstance(source, dict) else None)
    print("RECOVERY_ACKNOWLEDGED")
    return 0


def _record_heartbeat(status: dict, owner: str) -> None:
    """The exact liveness update `heartbeat` performs, factored out so
    background-wait-start/end can feed the SAME signal stall_warning/
    activity_note already read, instead of growing a second, parallel
    stall mechanism just for background waits."""
    status["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    status["last_heartbeat_owner"] = owner


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
        session_id = args.session
        current = lib.current_agent_sessions(status)
        session = next((item for item in current.values()
                        if isinstance(item, dict) and item.get("session_id") == session_id), None)
        if not isinstance(session, dict) or session.get("state") not in lib.AGENT_SESSION_LIVE_STATES:
            print("SHIP_FEATURE_BLOCKED: --session must name the current live managed session")
            return 1
        _record_heartbeat(status, session_id)
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
        status["background_wait"] = {
            "by": args.by.strip(), "since": datetime.now(timezone.utc).isoformat(),
            "note": message,
        }
        _record_heartbeat(status, "background_wait")
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
        status["background_wait"] = None
        _record_heartbeat(status, "background_wait")
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
    background wait. The pause is persisted as a durable `human_pause`
    record on status instead (a heartbeat would expire after
    stall_minutes and the pause would read as abandoned again), which is
    what suppresses stall_warning and feeds activity_note while it is
    open. updated_at is left alone so the pause length stays visible."""
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
        note = args.note.strip() if args.note and args.note.strip() else None
        message = note or "Human input pause started"
        status["human_pause"] = {
            "by": args.by.strip(), "since": datetime.now(timezone.utc).isoformat(), "note": note,
        }
        lib.commit(root, cfg, status=status,
                  event_kind="human_pause_started", event_message=message, by=args.by)
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
        status["human_pause"] = None
        message = args.note.strip() if args.note and args.note.strip() else "Human input pause ended"
        lib.commit(root, cfg, status=status,
                  event_kind="human_pause_ended", event_message=message, by=args.by)
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


def cmd_design_evidence(args) -> int:
    """#38: `design-evidence run` executes the configured [[design_evidence]]
    measurements that are missing, stale, or failed (or all of them with
    --force) and caches their bounded output in handsoff-design-evidence.json;
    `design-evidence show` prints the state view the dashboard and the role
    prompts consume. Neither touches status or acceptance; `run` appends a
    hashes-only `design_evidence_recorded` event per executed command."""
    json_module = __import__("json")
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.action == "show":
        print(json_module.dumps({"artifacts": lib.design_evidence_view(root, cfg)}, indent=2))
        return 0
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not cfg.get("design_evidence"):
        print("SHIP_FEATURE_NO_DESIGN_EVIDENCE_CONFIGURED: add [[design_evidence]] tables to handsoff.toml")
        return 1
    with lib.project_lock(root):
        status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
    # The commands run outside the lock (they may be slow); run_design_evidence
    # takes the lock itself around the store write and the event commit.
    records = lib.run_design_evidence(root, cfg, ids=args.id or None, by=args.by, force=args.force)
    summary = [{
        "id": r["id"], "reused": r["reused"], "exit_code": r["exit_code"], "input_hash": r["input_hash"],
        "output_sha256": r["output_sha256"], "output_bytes": r["output_bytes"], "truncated": r["truncated"],
        "matched_files": r["matched_files"], "head": r["head"], "at": r["at"],
    } for r in records]
    ok = all(r["exit_code"] == 0 for r in records)
    print(json_module.dumps({"ok": ok, "artifacts": summary}, indent=2))
    return 0 if ok else 1


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
    return serve(root, host=args.host, port=args.port, open_browser=not args.no_open,
                 owned_by_run=bool(getattr(args, "owned_by_run", False)))


def cmd_run_close(args) -> int:
    result = lib.close_run(
        lib.resolve_root(args.root), by=args.by, reason=args.reason,
        expected_updated_at=getattr(args, "expected_updated_at", None),
        cancel_active=bool(getattr(args, "cancel_active", False)),
        release_dashboard=bool(getattr(args, "release_dashboard", True)),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_run_reopen(args) -> int:
    result = lib.reopen_run(
        lib.resolve_root(args.root), by=args.by, reason=args.reason,
        expected_updated_at=getattr(args, "expected_updated_at", None),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Project Handsoff supervisor and gatekeeper")
    p.add_argument("--root", default=None, help="project root (default: nearest ancestor with handsoff.toml, else cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("feature")
    init.add_argument("--item", action="append", default=[],
                      help="declare one issue (#31 or #31 Title) or plain ask; repeat for multiple items")
    init.add_argument("--lane", choices=("full", "small-fix"), default="full",
                      help="initial delivery lane; small-fix still requires explicit Pilot confirmation")

    sub.add_parser("status", help="print run state as JSON, including the requested crew per role "
                                  "with its source (explicit or recommended) and adapter availability")
    sub.add_parser("validate")
    sub.add_parser("verify-log")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--dry-run", action="store_true")

    dashboard = sub.add_parser("dashboard", help="open the local read-only Mission Control dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    dashboard.add_argument("--owned-by-run", action="store_true",
                           help="mark this server as owned by the current run: it writes "
                                ".handsoff-dashboard-owner.json and `advance 8` with status complete "
                                "shuts it down so the port is free for the next run (the persistent "
                                "LaunchAgent and manual launches leave this off)")

    verify = sub.add_parser("verify")
    verify.add_argument("--criterion", action="append", required=True,
                        help="criterion id to bind this run to; repeat for more than one")
    verify.add_argument("--by", required=True)
    verify.add_argument("--no-cache", action="store_true",
                        help="launch every needed command even when an eligible record for its binding exists")

    release_plan = sub.add_parser("release-plan", help="record semantic release class and verification policy")
    release_plan.add_argument("--version", required=True)
    release_plan.add_argument("--by", required=True)
    release_plan.add_argument("--full-regression-override-reason")

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
    work_sync.add_argument("--item", action="append", default=[],
                           help="explicit work item to add, repeatable")

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
    work_update.add_argument("--implemented-by")
    required_choice = work_update.add_mutually_exclusive_group()
    required_choice.add_argument("--required", action="store_true")
    required_choice.add_argument("--optional", action="store_true")

    work_remove = sub.add_parser("work-item-remove")
    work_remove.add_argument("item")
    work_remove.add_argument("--by", required=True)

    lane_request = sub.add_parser("lane-request", help="request the measured small-fix lane for one item")
    lane_request.add_argument("item")
    lane_request.add_argument("--by", required=True)

    lane_confirm = sub.add_parser("lane-confirm", help="Pilot confirms an eligible small-fix lane")
    lane_confirm.add_argument("item")
    lane_confirm.add_argument("--by", required=True)

    plan_tranche = sub.add_parser("plan-tranche", help="build a deterministic read-only backlog proposal")
    plan_tranche.add_argument("--repo", required=True, help="GitHub owner/repository")
    plan_tranche.add_argument("--issues-file", default=None, help="offline JSON issue inventory")
    plan_tranche.add_argument("--archive-dir", default=None)
    plan_tranche.add_argument("--limit", type=int, default=5)

    tranche_approve = sub.add_parser("tranche-approve", help="Pilot atomically approves a proposal")
    tranche_approve.add_argument("--proposal-hash", required=True)
    tranche_approve.add_argument("--by", required=True)
    tranche_approve.add_argument("--item", action="append", default=None,
                                 help="retained item id in approved order; repeat")
    tranche_approve.add_argument("--drop", action="append", default=None,
                                 help="explicitly dropped item id; repeat")

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

    design_reject = sub.add_parser("design-reject", help="Pilot rejects the currently reviewed design")
    design_reject.add_argument("--by", required=True)
    design_reject.add_argument("--reason", required=True)

    design_review = sub.add_parser("record-design-review",
                                   help="record an independent Phase-2 review of the Architect's design")
    design_review.add_argument("--by", required=True, help="independent design reviewer's identity")
    design_review.add_argument("--architect", required=True, help="identity of the Architect being reviewed")
    design_review.add_argument("--summary", required=True, help="review findings or approval rationale")
    design_review.add_argument("--session", default=None,
                               help="host-only exact managed Reviewer session binding")
    design_review_decision = design_review.add_mutually_exclusive_group(required=True)
    design_review_decision.add_argument("--approve", action="store_true")
    design_review_decision.add_argument("--request-changes", action="store_true")
    design_review.add_argument("--finding", action="append", default=None,
                               help="one concrete finding (at most 512 characters); repeat for more, at most 32; "
                               "each gets the id F<attempt>.<n> that a later design-review-packet "
                               "disposition refers to")
    design_review.add_argument("--structural-blocker", action="store_true",
                               help="only with --request-changes: the design needs structural rework, so the "
                               "next attempt goes to the primary reviewer tier even when a follow-up "
                               "reviewer profile is configured (#37)")

    design_propose = sub.add_parser("design-propose", help="record a proposal: summary 1 to 512 characters; each list at most 8 items of at most 512 characters")
    design_propose.add_argument("--file", required=True, help="JSON proposal file")
    design_propose.add_argument("--by", required=True, help="host Architect identity")

    design_review_packet = sub.add_parser(
        "design-review-packet",
        help="generate the delta review packet for the next design-review attempt from the most recent "
             "recorded review (#36): criteria delta, dispositioned prior findings, evidence states, and "
             "repository identity; refused before the first review, which gets the full task")
    design_review_packet.add_argument("--by", required=True, help="identity generating the packet")
    design_review_packet.add_argument(
        "--disposition", action="append", default=None, metavar="ID=resolved|rejected|unresolved[:note]",
        help="disposition of one prior finding by id; rejected requires a note; an omitted finding "
             "defaults to unresolved")

    design_review_authorize = sub.add_parser(
        "design-review-authorize",
        help="Pilot-only: permit exactly one more design-review attempt once the autonomous budget "
             "([workflow] max_autonomous_design_reviews) is exhausted; consumed by the next "
             "record-design-review, whether a managed reviewer session or a human recorded it")
    design_review_authorize.add_argument("--by", required=True, help="the Pilot's identity")
    design_review_authorize.add_argument("--note", default=None,
                                         help="optional one-line reason, recorded as the event message")

    design_review_escalate = sub.add_parser(
        "design-review-escalate",
        help="Pilot-only: force the next design-review attempt onto the primary reviewer tier instead of "
             "the configured follow-up profile (#37); consumed by the next record-design-review")
    design_review_escalate.add_argument("--by", required=True, help="the Pilot's identity")
    design_review_escalate.add_argument("--note", default=None,
                                        help="optional one-line reason, recorded as the event message")

    review = sub.add_parser("record-review")
    review.add_argument("--by", required=True)
    review.add_argument("--session", default=None,
                        help="host-only exact managed Reviewer session binding")
    review.add_argument("--item", default=None,
                        help="record an item-scoped independent review for a confirmed small-fix lane")
    review.add_argument("--symptom-reproduced", choices=("yes", "not_applicable"), default="yes")
    review.add_argument("--tests-executed", choices=("yes", "no", "unknown"), default="unknown")

    review_start = sub.add_parser("review-attempt-start")
    review_start.add_argument("--by", required=True)
    review_start.add_argument("--reviewer", default=None)
    review_start.add_argument("--trigger", choices=tuple(sorted(lib.REVIEW_ATTEMPT_TRIGGERS)), default=None)
    review_start.add_argument("--note", default=None)

    review_findings = sub.add_parser("record-review-findings")
    review_findings.add_argument("--by", required=True)
    review_findings.add_argument("--session", default=None,
                                 help="host-only exact managed Reviewer session binding")
    review_findings.add_argument("--finding", action="append", required=True)
    review_findings.add_argument("--tests-executed", choices=("yes", "no", "unknown"), default="unknown")

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
    heartbeat.add_argument("--session", required=True,
                           help="current live managed session id that owns this heartbeat")
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

    design_evidence = sub.add_parser(
        "design-evidence",
        help="run or show the cached, hash-bound [[design_evidence]] measurements (#38): a "
             "configured command runs once and is reused until its command or declared inputs change",
    )
    design_evidence_actions = design_evidence.add_subparsers(dest="action", required=True)
    design_evidence_run = design_evidence_actions.add_parser(
        "run", help="execute every missing, stale, or failed measurement and cache its bounded output")
    design_evidence_run.add_argument("--id", action="append", default=None,
                                     help="artifact id to run; repeat for more than one (default: all)")
    design_evidence_run.add_argument("--by", required=True)
    design_evidence_run.add_argument("--force", action="store_true",
                                     help="rerun even when the cached record is current (the reviewer's challenge path)")
    design_evidence_actions.add_parser("show", help="print every artifact's state as JSON, never its output")

    criterion = sub.add_parser("criterion-update")
    criterion.add_argument("criterion")
    criterion.add_argument("--type", choices=("primary_fix", "supporting"))
    criterion.add_argument("--requirement")
    criterion.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS))
    criterion.add_argument("--test", action="append")
    criterion.add_argument("--state", choices=("failing", "not_tested", "blocked"))
    criterion.add_argument("--revoke-approval", action="store_true")

    criterion_add = sub.add_parser("criterion-add")
    criterion_add.add_argument("criterion")
    criterion_add.add_argument("--type", choices=("primary_fix", "supporting"), required=True)
    criterion_add.add_argument("--requirement", required=True)
    criterion_add.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS), required=True)
    criterion_add.add_argument("--test", action="append", required=True)
    criterion_add.add_argument("--revoke-approval", action="store_true")

    criterion_remove = sub.add_parser("criterion-remove")
    criterion_remove.add_argument("criterion")

    criteria_apply = sub.add_parser("criteria-apply", help="apply a list of add/update/remove criterion "
                                    "operations from a JSON file as one all-or-nothing commit (#44)")
    criteria_apply.add_argument("--file", required=True, help="TX.json: {\"operations\": [...]}")
    criteria_apply.add_argument("--by", required=True)
    criteria_apply.add_argument("--dry-run", action="store_true",
                                help="validate and print the plan as JSON; write nothing")
    criteria_apply.add_argument("--revoke-approval", action="store_true")

    amendment_open = sub.add_parser("amendment-open", help="open a scoped post-approval amendment from a "
                                    "criteria transaction file; the planner classifies it and refuses a "
                                    "full redesign (#42)")
    amendment_open.add_argument("--file", required=True, help="TX.json: {\"operations\": [...]}")
    amendment_open.add_argument("--by", required=True, help="the Architect opening the amendment")
    amendment_open.add_argument("--summary", default=None)
    amendment_open.add_argument("--request-full-redesign", action="store_true",
                                help="force the full_redesign classification (the command then refuses to open)")

    amendment_revise = sub.add_parser("amendment-revise", help="apply a follow-up transaction to the open "
                                      "amendment; recomputes its hash and clears the stale review (#42)")
    amendment_revise.add_argument("--file", required=True)
    amendment_revise.add_argument("--by", required=True)

    amendment_review = sub.add_parser("amendment-review", help="record the independent review of the open "
                                      "amendment, bound to its hash (#42)")
    amendment_review.add_argument("--by", required=True)
    amendment_review_decision = amendment_review.add_mutually_exclusive_group(required=True)
    amendment_review_decision.add_argument("--approve", action="store_true")
    amendment_review_decision.add_argument("--request-changes", action="store_true")
    amendment_review.add_argument("--summary", required=True)

    amendment_approve = sub.add_parser("amendment-approve", help="Pilot approval of the reviewed amendment: "
                                       "rewrites the design hashes and resumes the frozen phase (#42, human-only)")
    amendment_approve.add_argument("--by", required=True)

    amendment_escalate = sub.add_parser("amendment-escalate", help="close the open amendment as escalated and "
                                        "take the full redesign path (#42)")
    amendment_escalate.add_argument("--by", required=True)
    amendment_escalate.add_argument("--reason", required=True)

    question_raise = sub.add_parser("question-raise", help="record a role's question for the Pilot (#46); a "
                                    "managed child prints HANDSOFF_QUESTION: <text> instead")
    question_raise.add_argument("--role", required=True, choices=lib.SELECTABLE_AGENT_ROLES)
    question_raise.add_argument("--text", required=True)
    question_raise.add_argument("--by", required=True)
    question_raise.add_argument("--session", default=None, help="managed session id the question belongs to")

    question_answer = sub.add_parser("question-answer", help="Pilot answer to an open question (#46, human-only); "
                                     "--batch FILE answers several at once (#48)")
    question_answer.add_argument("--id", default=None)
    question_answer.add_argument("--text", default=None)
    question_answer.add_argument("--batch", default=None, metavar="FILE",
                                 help='JSON file {"answers": [{"question_id", "choice"} | {"question_id", "other"}]}')
    question_answer.add_argument("--by", required=True)

    analyze = sub.add_parser("analyze-archives", help="scan the completed-run archive for recurring patterns "
                             "and file evidenced improvement tickets (#49); prints the report path")
    analyze.add_argument("--dry-run", action="store_true", help="evaluate and report, file nothing")
    analyze.add_argument("--archive-dir", default=None, help="archive directory for this scan only "
                         "(default: [analysis].archive_dir, else HANDSOFF_ARCHIVE_DIR, else ~/Documents/Handsoff-Archive)")

    pilot_note = sub.add_parser("pilot-note", help="record a Pilot observation on the current run (#49); "
                                "the next archive scan lists it as an R7 finding")
    pilot_note.add_argument("--by", required=True)
    pilot_note.add_argument("--text", required=True, help="1 to 512 characters")

    run_close = sub.add_parser("run-close", help="cleanly close a run and release owned resources")
    run_close.add_argument("--by", required=True)
    run_close.add_argument("--reason", required=True)
    run_close.add_argument("--expected-updated-at")
    run_close.add_argument("--cancel-active", action="store_true")

    run_reopen = sub.add_parser("run-reopen", help="reopen a non-complete cleanly closed run")
    run_reopen.add_argument("--by", required=True)
    run_reopen.add_argument("--reason", required=True)
    run_reopen.add_argument("--expected-updated-at")

    adv = sub.add_parser("advance")
    adv.add_argument("phase", type=int)
    adv.add_argument("progress", type=int, nargs="?")
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
    gate.add_argument("--revoke", action="store_true")
    gate.add_argument("--by", default=None)
    gate.add_argument("--reason", default=None)

    return p


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "init": cmd_init, "status": cmd_status, "validate": cmd_validate,
        "advance": cmd_advance, "deployment-gate": cmd_deployment_gate,
        "verify": cmd_verify, "verify-log": cmd_verify_log, "doctor": cmd_doctor,
        "release-plan": cmd_release_plan,
        "regression-request": cmd_regression_request,
        "regression-decide": cmd_regression_decide,
        "regression-run": cmd_regression_run,
        "regression-cancel": cmd_regression_cancel,
        "regression-finalize": cmd_regression_finalize,
        "work-items-sync": cmd_work_items_sync,
        "work-item-activate": cmd_work_item_activate,
        "work-item-update": cmd_work_item_update,
        "work-item-remove": cmd_work_item_remove,
        "lane-request": cmd_lane_request,
        "lane-confirm": cmd_lane_confirm,
        "plan-tranche": cmd_plan_tranche,
        "tranche-approve": cmd_tranche_approve,
        "dashboard": cmd_dashboard,
        "record-evidence": cmd_record_evidence,
        "record-symptom-resolved": cmd_record_symptom,
        "design-approve": cmd_design_approve,
        "design-reject": cmd_design_reject,
        "record-design-review": cmd_record_design_review,
        "design-review-packet": cmd_design_review_packet,
        "design-propose": cmd_design_propose,
        "design-review-authorize": cmd_design_review_authorize,
        "design-review-escalate": cmd_design_review_escalate,
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
        "design-evidence": cmd_design_evidence,
        "criterion-update": cmd_criterion_update,
        "criterion-add": cmd_criterion_add,
        "criterion-remove": cmd_criterion_remove,
        "criteria-apply": cmd_criteria_apply,
        "amendment-open": cmd_amendment_open,
        "amendment-revise": cmd_amendment_revise,
        "amendment-review": cmd_amendment_review,
        "amendment-approve": cmd_amendment_approve,
        "amendment-escalate": cmd_amendment_escalate,
        "question-raise": cmd_question_raise,
        "question-answer": cmd_question_answer,
        "analyze-archives": cmd_analyze_archives,
        "pilot-note": cmd_pilot_note,
        "run-close": cmd_run_close,
        "run-reopen": cmd_run_reopen,
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
