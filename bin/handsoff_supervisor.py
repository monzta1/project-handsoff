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


def cmd_init(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    sp, ap = lib.status_path(root, cfg), lib.acceptance_path(root, cfg)
    now = datetime.now(timezone.utc).isoformat()
    # Locked for the same reason advance/deployment-gate are: init writes
    # two files and appends an event, and an unlocked append_event racing
    # another process's append_event forks the hash chain (round 2 finding).
    with lib.project_lock(root):
        if sp.exists() or ap.exists():
            print(f"HANDSOFF_INIT_SKIPPED: {sp.name} or {ap.name} already exists at {root}")
            return 1
        lib.atomic_write_json(ap, {
            "feature": args.feature,
            "criteria": [{
                "id": "REQ-001", "type": "primary_fix", "requirement": "State the exact observable outcome.",
                "verification": "unit_and_browser", "tests": ["name_or_path_of_test"], "evidence": [], "state": "failing",
            }],
        })
        lib.atomic_write_json(sp, {
            "feature": args.feature, "phase_number": 1, "phase": lib.PHASES[1], "progress": 0,
            "status": "in_progress", "updated_at": now,
            "next_action": "Read the project rules and reproduce the original symptom.",
            "design_round": 0, "review_round": 0, "retry_count": 0, "summary": "", "reassurance": "",
            "implemented_by": None, "reviewed_by": None, "deployment_approved": None,
            "requirement_coverage": {"passing": 0, "failing": 1, "not_tested": 0, "blocked": 0,
                                      "original_symptom_resolved": False},
            "reviewer_checklist": {"symptom_reproduced": "not_verifiable", "symptom_resolved": "not_verifiable",
                                   "all_criteria_verified": "no", "evidence_attached": "no"},
            "events": [],
        })
        lib.append_event(root, cfg, "initialized", f"Handsoff initialized for '{args.feature}'", project_root=str(root))
    print(f"HANDSOFF_INITIALIZED: {sp} and {ap}")
    return 0


def cmd_status(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        status, acceptance = _load(root, cfg)
    except lib.HandsoffError as e:
        print(f"SHIP_FEATURE_BLOCKED: {e}")
        return 1
    errors = lib.compute_errors(status, acceptance, cfg)
    warning = lib.stall_warning(status, cfg)
    log_problems = lib.verify_event_log(root, cfg)
    print(__import__("json").dumps({
        "root": str(root), "feature": status.get("feature"), "phase": status.get("phase"),
        "phase_number": status.get("phase_number"), "progress": status.get("progress"),
        "status": status.get("status"), "next_action": status.get("next_action"),
        "design_round": status.get("design_round"), "review_round": status.get("review_round"),
        "validation": "blocked" if errors else "valid", "errors": errors,
        "stall_warning": warning, "event_log_intact": not log_problems, "event_log_problems": log_problems,
    }, indent=2))
    return 1 if errors else 0


def cmd_validate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        status, acceptance = _load(root, cfg)
    except lib.HandsoffError as e:
        print(f"SHIP_FEATURE_BLOCKED: {e}")
        return 1
    errors = lib.compute_errors(status, acceptance, cfg)
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
            status, acceptance = _load(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1

        if args.phase not in lib.PHASES:
            print(f"invalid phase {args.phase}, must be one of {sorted(lib.PHASES)}")
            return 1
        current = int(status.get("phase_number", 0) or 0)
        if args.phase < current or args.phase > current + 1:
            print(f"phase transition blocked: current={current}, requested={args.phase} (one step at a time)")
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
        if args.status:
            proposed["status"] = args.status
        if args.implemented_by:
            proposed["implemented_by"] = args.implemented_by
        if args.reviewed_by:
            proposed["reviewed_by"] = args.reviewed_by
        if args.design_round is not None:
            proposed["design_round"] = args.design_round
        if args.review_round is not None:
            proposed["review_round"] = args.review_round

        errors = lib.compute_errors(proposed, acceptance, cfg)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        if args.dry_run:
            print("SHIP_FEATURE_ADVANCE_WOULD_SUCCEED")
            return 0

        lib.atomic_write_json(lib.status_path(root, cfg), proposed)
        lib.append_event(root, cfg, "phase_advanced", f"Advanced to {lib.PHASES[args.phase]}",
                         phase_number=args.phase, progress=args.progress)
    print("SHIP_FEATURE_ADVANCED")
    return 0


def cmd_deployment_gate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance = _load(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg)
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
        # The approval is bound to a hash of the acceptance criteria AT
        # THE MOMENT of approval. If the registry changes afterward (a
        # criterion reopened, added, or removed), the Phase 8 gate
        # recomputes this hash and refuses the now-stale approval.
        status["deployment_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by or "unspecified",
            "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
        }
        lib.atomic_write_json(lib.status_path(root, cfg), status)
        lib.append_event(root, cfg, "deployment_approved", "Explicit deployment approval recorded", by=args.by or "unspecified")
    print("DEPLOYMENT_APPROVED")
    return 0


def cmd_verify(args) -> int:
    """Run the configured checks for real and print their results, so an
    acceptance criterion's evidence can point at something that actually
    executed instead of a claim someone typed."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not cfg.get("check_commands"):
        print("SHIP_FEATURE_NO_CHECKS_CONFIGURED: set [checks].commands in handsoff.toml")
        return 1
    results = lib.run_checks(cfg, root)
    ok = all(r["exit_code"] == 0 for r in results)
    with lib.project_lock(root):
        lib.append_event(root, cfg, "checks_run", "Ran configured checks", ok=ok,
                         results=[{"command": r["command"], "exit_code": r["exit_code"]} for r in results])
    print(__import__("json").dumps({"ok": ok, "results": results}, indent=2))
    return 0 if ok else 1


def cmd_verify_log(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    problems = lib.verify_event_log(root, cfg)
    if problems:
        print("EVENT_LOG_TAMPERED_OR_CORRUPT")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    print("EVENT_LOG_INTACT")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Project Handsoff supervisor and gatekeeper")
    p.add_argument("--root", default=None, help="project root (default: nearest ancestor with handsoff.toml, else cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("feature")

    sub.add_parser("status")
    sub.add_parser("validate")
    sub.add_parser("verify-log")

    verify = sub.add_parser("verify")

    adv = sub.add_parser("advance")
    adv.add_argument("phase", type=int)
    adv.add_argument("progress", type=int)
    adv.add_argument("--status", default=None)
    adv.add_argument("--implemented-by", default=None)
    adv.add_argument("--reviewed-by", default=None)
    adv.add_argument("--design-round", type=int, default=None)
    adv.add_argument("--review-round", type=int, default=None)
    adv.add_argument("--dry-run", action="store_true")

    gate = sub.add_parser("deployment-gate")
    gate.add_argument("--approve", action="store_true")
    gate.add_argument("--by", default=None)

    args = p.parse_args()
    handlers = {
        "init": cmd_init, "status": cmd_status, "validate": cmd_validate,
        "advance": cmd_advance, "deployment-gate": cmd_deployment_gate,
        "verify": cmd_verify, "verify-log": cmd_verify_log,
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
