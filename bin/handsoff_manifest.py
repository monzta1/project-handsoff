#!/usr/bin/env python3
"""Generate or verify the shipped Handsoff runtime manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

RUNTIME_FILES = (
    "bin/handsoff_agent.py",
    "bin/handsoff_broker.py",
    "bin/handsoff_dashboard.py",
    "bin/handsoff_lib.py",
    "bin/handsoff_supervisor.py",
    "bin/handsoff_tranche.py",
    "bin/validate_handsoff_status.py",
    "dashboard/app.js",
    "dashboard/index.html",
    "dashboard/lib/dashboard-logic.js",
    "dashboard/styles.css",
    "prompts/architect.md",
    "prompts/implementer.md",
    "prompts/reviewer.md",
    "prompts/supervisor.md",
    "schemas/acceptance.schema.json",
    "schemas/status.schema.json",
)


def payload(root: Path, version: str) -> dict:
    files = {}
    for relative in RUNTIME_FILES:
        data = (root / relative).read_bytes()
        files[relative] = hashlib.sha256(data).hexdigest()
    return {"schema": 1, "version": version, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    path = root / "handsoff-runtime.json"
    path.write_text(json.dumps(payload(root, args.version), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
