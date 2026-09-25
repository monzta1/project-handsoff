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
    "bin/handsoff_cli.py",
    "bin/handsoff_close_transaction.py",
    "bin/handsoff_dashboard.py",
    "bin/handsoff_fleet.py",
    "bin/handsoff_fleet_signals.py",
    "bin/handsoff_lib.py",
    "bin/handsoff_manifest.py",
    "bin/handsoff_observability.py",
    "bin/handsoff_preflight.py",
    "bin/handsoff_progress.py",
    "bin/handsoff_config.py",
    "bin/handsoff_core.py",
    "bin/handsoff_resources.py",
    "bin/handsoff_ledger.py",
    "bin/handsoff_routing.py",
    "bin/handsoff_regress.py",
    "bin/handsoff_release_runtime.py",
    "bin/handsoff_release_transaction.py",
    "bin/handsoff_runtime_control.py",
    "bin/handsoff_supervisor.py",
    "bin/handsoff_tranche.py",
    "bin/handsoff_update.py",
    "bin/validate_handsoff_status.py",
    "dashboard/app.js",
    "dashboard/index.html",
    "dashboard/lib/dashboard-logic.js",
    "dashboard/lib/run-vocabulary.js",
    "dashboard/logo.png",
    "dashboard/regression.html",
    "dashboard/regression.js",
    "dashboard/styles.css",
    "fleet/app.js",
    "fleet/index.html",
    "fleet/metrics.html",
    "fleet/metrics.js",
    "fleet/styles.css",
    "playbook/INDEX.md",
    "playbook/index.json",
    "playbook/landing.md",
    "playbook/lanes.md",
    "playbook/lessons-agents.md",
    "playbook/lessons-evidence.md",
    "playbook/lessons-lane.md",
    "playbook/protocol.md",
    "playbook/reviewers.md",
    "prompts/architect.md",
    "prompts/implementer.md",
    "prompts/reviewer.md",
    "prompts/supervisor.md",
    "rules/README.md",
    "rules/reviewer-launch-phase-1.json",
    "rules/reviewer-packet-finding-length.json",
    "rules/reviewer-packet-tests-executed.json",
    "schemas/acceptance.schema.json",
    "schemas/status.schema.json",
    "schemas/snapshot.schema.json",
    "templates/handsoff.toml",
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
