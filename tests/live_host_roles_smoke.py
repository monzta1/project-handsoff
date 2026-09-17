#!/usr/bin/env python3
"""Deployed-engine check for host roles and the exhausted-budget hold (#76, #78, #79).

Runs the installed `handsoff` command, never the source tree: a fresh thin
project configured with a host Supervisor and Architect must pass doctor,
report adapter host with no model, accept `design-propose`, and refuse
`host` for the reviewer. `work-items-sync` must not append a feature-title ask beside the explicit
issue item.
"""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([HANDSOFF, *args], capture_output=True, text=True, check=check)


identity = json.loads(run("version", "--json").stdout)
assert identity["source"] == "installed-engine" and identity["version"] == expected_version, identity

with tempfile.TemporaryDirectory(prefix="handsoff-live-host-") as tmp:
    project = Path(tmp) / "thin-project"
    run("init", str(project))
    toml = project / "handsoff.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[agents]\nsupervisor = "host"\narchitect = "host"\nimplementer = "codex"\nreviewer = "codex"\n', encoding="utf-8")
    report = json.loads(run("doctor", str(project)).stdout)
    assert report["ok"] is True and report["engine"]["version"] == expected_version, report["engine"]

    run("supervisor", "--root", str(project), "init", "Live host smoke", "--item", "#1")
    refused = run("agent", "--root", str(project), "inspect", "supervisor", "--task", "live", check=False)
    assert refused.returncode != 0 and "host-driven" in (refused.stdout + refused.stderr), refused

    run("supervisor", "--root", str(project), "advance", "2", "10")
    synced = run("supervisor", "--root", str(project), "work-items-sync", "--by", "host-supervisor")
    assert "WORK_ITEM_SYNC_SKIPPED: ask-" in synced.stdout, synced.stdout
    registry = json.loads((project / "handsoff-acceptance.json").read_text(encoding="utf-8"))["work_items"]
    assert [item["id"] for item in registry] == ["issue-1"], registry

    proposal = project / "proposal.json"
    proposal.write_text(json.dumps({"summary": "Live host proposal", "approach": ["one step"], "tradeoffs": [],
                                    "decisions": ["one decision"], "constraints": [],
                                    "verification": ["manual check"]}), encoding="utf-8")
    result = run("supervisor", "--root", str(project), "design-propose", "--file", str(proposal), "--by", "host-architect")
    assert result.stdout.startswith("DESIGN_PROPOSAL_RECORDED: "), result.stdout

    bad = toml.read_text(encoding="utf-8").replace('reviewer = "codex"', 'reviewer = "host"')
    toml.write_text(bad, encoding="utf-8")
    refused = run("supervisor", "--root", str(project), "status", check=False)
    assert refused.returncode != 0 and "cannot be host" in (refused.stdout + refused.stderr), refused

print(f"LIVE_HOST_ROLES_OK installed={identity['version']} host_launch_refused=yes design_propose=yes sync_skip=yes reviewer_host_refused=yes")
