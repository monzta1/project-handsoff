#!/usr/bin/env python3
"""Standalone status validator, kept for anything already invoking this
script by name. It is a thin wrapper now: all gate logic lives in
handsoff_lib.py, shared with handsoff_supervisor.py, so the two can no
longer drift into checking different rules. Prefer
`handsoff_supervisor.py validate` in new usage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("status", nargs="?", default=None, help="path to handsoff-status.json (default: resolved from handsoff.toml)")
    p.add_argument("acceptance", nargs="?", default=None, help="path to handsoff-acceptance.json (default: resolved from handsoff.toml)")
    p.add_argument("--root", default=None)
    args = p.parse_args()

    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    status_file = Path(args.status) if args.status else lib.status_path(root, cfg)
    acceptance_file = Path(args.acceptance) if args.acceptance else lib.acceptance_path(root, cfg)

    try:
        with lib.project_lock(root):
            status = lib.load_unique_json(status_file)
            acceptance = lib.load_unique_json(acceptance_file)
            verifications, verification_problems = lib.load_verifications(root, cfg)
            errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                        verification_problems=verification_problems)
            errors += [f"event log: {problem}" for problem in lib.verify_event_log(root, cfg)]
    except lib.HandsoffError as e:
        print("SHIP_FEATURE_STATUS_INVALID")
        print(f"- {e}")
        return 1
    except Exception as e:  # last-resort: a clean refusal beats a raw traceback
        print("SHIP_FEATURE_STATUS_INVALID")
        print(f"- unexpected error: {type(e).__name__}: {e}")
        return 1
    if errors:
        print("SHIP_FEATURE_STATUS_INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("SHIP_FEATURE_STATUS_VALID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
