#!/usr/bin/env python3
"""Deployed-engine check for external operation telemetry (#67, #68).

Runs the installed `handsoff` command against a fresh thin project with a
fake `codex` on PATH that emits HANDSOFF_OPERATION lines around a design
proposal. Proves the installed runner persists the telemetry, the installed
dashboard exposes it as runtime.operation with a terminal assessment, and
the shipped prompts carry a parseable example.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]

FAKE_CODEX = """#!/bin/sh
cat >/dev/null
echo 'HANDSOFF_OPERATION: {"operation_id":"op-gh01","dependency":"github_api","operation":"create_issue_comment","state":"started","attempt":1,"timeout_seconds":60}'
echo 'HANDSOFF_OPERATION: {"operation_id":"op-gh01","dependency":"github_api","operation":"create_issue_comment","state":"succeeded","attempt":1,"timeout_seconds":60}'
echo 'HANDSOFF_DESIGN_PROPOSAL: {"summary":"live smoke","approach":["one"],"tradeoffs":[],"decisions":["one"],"constraints":[],"verification":["one"]}'
exit 0
"""


def run(*args, env=None, check=True):
    return subprocess.run([HANDSOFF, *args], capture_output=True, text=True, check=check, env=env)


identity = json.loads(run("version", "--json").stdout)
assert identity["source"] == "installed-engine" and identity["version"] == expected_version, identity

with tempfile.TemporaryDirectory(prefix="handsoff-live-ops-") as tmp:
    shim = Path(tmp) / "shim"
    shim.mkdir()
    (shim / "codex").write_text(FAKE_CODEX, encoding="utf-8")
    (shim / "codex").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{os.environ.get('PATH', '')}")

    project = Path(tmp) / "thin-project"
    run("init", str(project))
    toml = project / "handsoff.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[agents]\nsupervisor = "host"\narchitect = "codex"\n'
                    'implementer = "codex"\nreviewer = "codex"\n', encoding="utf-8")
    run("supervisor", "--root", str(project), "init", "Live operations smoke", "--item", "#1")
    run("supervisor", "--root", str(project), "advance", "2", "10")
    run("supervisor", "--root", str(project), "criterion-update", "REQ-001", "--type", "primary_fix",
        "--verification", "manual", "--requirement", "[#1] Tagged to the registered issue")
    launched = run("agent", "--root", str(project), "launch", "architect", "--by", "fake-architect",
                   "--task", "live smoke", env=env)
    assert "DESIGN_PROPOSAL" in launched.stdout + launched.stderr or launched.returncode == 0, launched

    operations = json.loads((project / ".handsoff-operations.json").read_text(encoding="utf-8"))
    sessions = operations["sessions"]
    assert len(sessions) == 1, sessions
    records = next(iter(sessions.values()))["operations"]
    assert [r["operation_id"] for r in records] == ["op-gh01"], records
    assert records[0]["state"] == "succeeded" and records[0]["ended_at"], records[0]
    assert set(records[0]) <= {"operation_id", "dependency", "operation", "state", "attempt", "timeout_seconds",
                               "category", "started_at", "updated_at", "ended_at", "session_id", "role"}, records[0]

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen([HANDSOFF, "dashboard", "--root", str(project), "--port", str(port), "--no-open"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        payload = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.25)
        assert payload is not None, "installed dashboard did not answer"
        operation = payload["runtime"]["operation"]
        assert operation["availability"] == "available", operation
        assert operation["current"]["operation_id"] == "op-gh01", operation
        assert operation["assessment"] == "succeeded" and operation["dependency_class"], operation
        assert operation["retry_count"] == 0 and operation["last_success_at"], operation
    finally:
        server.terminate()
        server.wait(timeout=10)

    prompts_root = Path(identity["source_root"]) / "prompts"
    for name in ("implementer", "reviewer", "architect", "supervisor"):
        text = (prompts_root / f"{name}.md").read_text(encoding="utf-8")
        examples = [m for m in re.findall(r"HANDSOFF_OPERATION: (\{[^`\n]*\})", text) if "op-gh01" in m]
        assert examples, name
        json.loads(examples[0])

print(f"LIVE_OPERATIONS_OK installed={identity['version']} telemetry_persisted=yes dashboard_operation=yes prompts=yes")
