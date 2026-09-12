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
import sys
from datetime import datetime, timezone
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
        status["design_approved"] = None
        if status.get("requires_design_approval") and status.get("phase_number", 1) >= 3:
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
        status = {
            "feature": args.feature, "phase_number": 1, "phase": lib.PHASES[1], "progress": 0,
            "status": "in_progress", "updated_at": now, "last_heartbeat_at": None,
            "next_action": lib.NEXT_ACTION_DEFAULTS[1],
            "design_round": 0, "review_round": 0, "retry_count": 0, "summary": "", "reassurance": "",
            "implemented_by": None, "reviewed_by": None, "deployment_approved": None,
            "requires_design_approval": True, "design_approved": None,
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
        if args.design_round is not None:
            proposed["design_round"] = args.design_round
        if args.review_round is not None:
            proposed["review_round"] = args.review_round

        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        if args.dry_run:
            print("SHIP_FEATURE_ADVANCE_WOULD_SUCCEED")
            return 0

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
        # Approval is only meaningful once every earlier gate is already
        # satisfied: Phase 7 ("Awaiting deployment approval") or later.
        # Without this, approval could be granted at Phase 1, before an
        # Implementer or Reviewer had touched anything, and would still
        # satisfy the Phase 8 gate later.
        phase = int(status.get("phase_number", 0) or 0)
        if phase < 7:
            print(f"DEPLOYMENT_BLOCKED\n- deployment approval requires Phase 7 or later (currently Phase {phase})")
            return 1
        if not args.approve:
            print("DEPLOYMENT_AWAITING_EXPLICIT_APPROVAL")
            return 2
        if not args.by:
            print("DEPLOYMENT_BLOCKED\n- --by is required when recording approval")
            return 1
        # The approval is bound to a hash of the acceptance criteria AT
        # THE MOMENT of approval. If the registry changes afterward (a
        # criterion reopened, added, or removed), the Phase 8 gate
        # recomputes this hash and refuses the now-stale approval.
        status["deployment_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
            "config_hash": lib.config_hash(cfg),
        }
        lib.commit(root, cfg, status=status,
                  event_kind="deployment_approved", event_message="Explicit deployment approval recorded",
                  by=args.by)
    print("DEPLOYMENT_APPROVED")
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
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "architect": args.architect,
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "redesigns_settled_work": args.redesigns_settled_work,
        }
        lib.commit(root, cfg, status=status,
                  event_kind="design_approved", event_message=args.summary,
                  by=args.by, architect=args.architect,
                  redesigns_settled_work=args.redesigns_settled_work)
    print("DESIGN_APPROVAL_RECORDED")
    return 0


def cmd_record_review(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number", 0) < 5:
            print("SHIP_FEATURE_BLOCKED: independent review can only be recorded in Phase 5 or later")
            return 1
        if args.by == status.get("implemented_by"):
            print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
            return 1
        preflight = dict(status)
        preflight["phase_number"] = 6
        preflight["phase"] = lib.PHASES[6]
        preflight["review"] = {
            "by": args.by, "at": datetime.now(timezone.utc).isoformat(),
            "acceptance_hash": lib.acceptance_hash(acceptance["criteria"]),
            "config_hash": lib.config_hash(cfg),
            "checklist": {"symptom_reproduced": args.symptom_reproduced,
                          "symptom_resolved": "yes", "all_criteria_verified": "yes",
                          "evidence_attached": "yes"},
        }
        preflight["reviewed_by"] = args.by
        errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        status["review"] = preflight["review"]
        status["reviewed_by"] = args.by
        status["reviewer_checklist"] = preflight["review"]["checklist"]
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status,
                  event_kind="review_approved", event_message="Independent review approved current acceptance",
                  by=args.by, acceptance_hash=status["review"]["acceptance_hash"])
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
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="criterion_updated", event_message="Acceptance criterion updated",
                  criterion=args.criterion)
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
        acceptance["criteria"].append({
            "id": args.criterion, "type": args.type, "requirement": args.requirement,
            "verification": args.verification, "tests": args.test or [],
            "evidence": [], "state": "not_tested",
        })
        if args.type == "primary_fix":
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="criterion_added", event_message="Acceptance criterion added",
                  criterion=args.criterion)
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
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="criterion_removed", event_message="Acceptance criterion removed",
                  criterion=args.criterion)
    print("CRITERION_REMOVED")
    return 0


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
        status["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        message = args.note.strip() if args.note and args.note.strip() else "Heartbeat: run is active"
        lib.commit(root, cfg, status=status,
                  event_kind="heartbeat", event_message=message, by=args.by)
    print("HEARTBEAT_RECORDED")
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

    review = sub.add_parser("record-review")
    review.add_argument("--by", required=True)
    review.add_argument("--symptom-reproduced", choices=("yes", "not_applicable"), default="yes")

    live = sub.add_parser("verify-live")
    live.add_argument("--by", required=True)

    heartbeat = sub.add_parser("heartbeat", help="record a liveness signal for a run doing long "
                               "background work, without advancing phase or progress")
    heartbeat.add_argument("--by", required=True)
    heartbeat.add_argument("--note", default=None, help="optional one-line description of the background activity")

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
    adv.add_argument("--design-round", type=int, default=None)
    adv.add_argument("--review-round", type=int, default=None)
    adv.add_argument("--dry-run", action="store_true")
    adv.add_argument("--next-action", default=None,
                     help="override the phase-appropriate default next_action message")

    gate = sub.add_parser("deployment-gate")
    gate.add_argument("--approve", action="store_true")
    gate.add_argument("--by", default=None)

    args = p.parse_args()
    handlers = {
        "init": cmd_init, "status": cmd_status, "validate": cmd_validate,
        "advance": cmd_advance, "deployment-gate": cmd_deployment_gate,
        "verify": cmd_verify, "verify-log": cmd_verify_log, "doctor": cmd_doctor,
        "dashboard": cmd_dashboard,
        "record-evidence": cmd_record_evidence,
        "record-symptom-resolved": cmd_record_symptom,
        "design-approve": cmd_design_approve,
        "record-review": cmd_record_review,
        "verify-live": cmd_verify_live,
        "heartbeat": cmd_heartbeat,
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
