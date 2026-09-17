#!/usr/bin/env python3
"""Deployed-engine check for Mission Control operator coverage (#72).

Starts the installed dashboard, owned by run, on a fresh thin project and
asserts through its HTTP API that every canonical operation is labelled,
that a stale launch request is refused and audited, that the engine section
is preview-only, and that Fleet exposes a verified loopback dashboard_url
for the owned dashboard and none after it is released.
"""
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]


def run(*args, check=True, env=None):
    return subprocess.run([HANDSOFF, *args], capture_output=True, text=True, check=check, env=env)


def get(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def post(url, body, origin):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def fleet_snapshot(env):
    """Fleet's snapshot through the installed engine's own Python, so the
    view under test is the shipped one."""
    python = str(Path.home() / ".local" / "share" / "handsoff" / "venv" / "bin" / "python")
    return json.loads(subprocess.run(
        [python, "-c", "import json, handsoff_fleet as f; print(json.dumps(f.build_fleet()))"],
        capture_output=True, text=True, check=True, env=env).stdout)


identity = json.loads(run("version", "--json").stdout)
assert identity["source"] == "installed-engine" and identity["version"] == expected_version, identity

with tempfile.TemporaryDirectory(prefix="handsoff-live-mc-") as tmp:
    project = (Path(tmp) / "thin-project").resolve()
    project.parent.mkdir(parents=True, exist_ok=True)
    registry = Path(tmp) / "fleet.json"
    env = dict(os.environ, HANDSOFF_FLEET_REGISTRY=str(registry))
    run("init", str(project))
    toml = project / "handsoff.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[agents]\nsupervisor = "host"\narchitect = "host"\n'
                    'implementer = "codex"\nreviewer = "codex"\n', encoding="utf-8")
    run("supervisor", "--root", str(project), "init", "Live Mission Control smoke", "--item", "#1")
    run("fleet", "register", str(project), env=env)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen([HANDSOFF, "dashboard", "--root", str(project), "--port", str(port),
                               "--no-open", "--owned-by-run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        payload = None
        for _ in range(40):
            try:
                payload = get(f"{base}/api/dashboard")
                break
            except Exception:
                time.sleep(0.25)
        assert payload is not None, "installed dashboard did not answer"
        inventory = payload["operations"]["inventory"]
        kinds = [item["kind"] for item in inventory]
        assert len(kinds) == 22 and kinds[0] == "design_approve" and "launch_role" in kinds and "engine_migrate" in kinds, kinds
        assert all(item["availability"] in {"actionable", "unavailable", "read_only"} for item in inventory), inventory
        assert all(item["reason"] or item["consequence"] for item in inventory), inventory
        live_keys = {"seconds_since_activity", "process_signal", "stall_warning", "stall_threshold_minutes", "assessment"}
        assert live_keys <= set(payload["status"].get("activity") or {}), payload["status"].get("activity")
        cli_status = json.loads(run("supervisor", "--root", str(project), "status").stdout)
        assert cli_status["activity"]["stall_warning"] == payload["status"]["activity"]["stall_warning"], (cli_status["activity"], payload["status"]["activity"])
        engine = payload["operations"]["engine"]
        assert engine["version"] == expected_version and engine["execution"] == "unavailable", engine
        assert len(engine["commands"]) == 8 and all(str(project) in c for c in engine["commands"].values()), engine["commands"]

        status, answer = post(f"{base}/api/launch-role",
                              {"action_id": "launch_role:0000000000000000", "role": "implementer", "task": "x"}, base)
        assert status == 409 and "stale" in answer["error"], (status, answer)
        events = [json.loads(line) for line in (project / "handsoff-events.jsonl").read_text(encoding="utf-8").splitlines()]
        audited = [e for e in events if e.get("kind") == "pilot_launch_requested"]
        assert audited and audited[-1]["accepted"] is False, audited

        snapshot = fleet_snapshot(env)
        card = next(p for p in snapshot["projects"] if p["root"] == str(project))
        assert card["dashboard_url"] == f"http://127.0.0.1:{port}/", card
    finally:
        server.terminate()
        server.wait(timeout=10)

    snapshot = fleet_snapshot(env)
    card = next(p for p in snapshot["projects"] if p["root"] == str(project))
    assert card["dashboard_url"] is None and card["dashboard_note"], card

print(f"LIVE_MISSION_CONTROL_OK installed={identity['version']} inventory=22 stale_launch_refused=yes engine_preview_only=yes fleet_url=yes released_url_none=yes")
