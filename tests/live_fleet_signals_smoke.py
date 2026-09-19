"""#152 live smoke: the installed Fleet on :8765 serves the GitHub and Beakon
signals for real registered projects.

The subject is the deployed engine, like every other live smoke: it proves
that the release carrying this change is the one installed and served, then
that the signals are real. Three checks, in order:

1. `handsoff version --json` reports the version this checkout's
   pyproject.toml declares (the release was installed).
2. http://127.0.0.1:8765/app.js contains `signalsStrip` (the served Fleet
   page is the new one, not a stale asset).
3. Every project in /api/fleet carries `github` and `beakon` keys, and the
   project whose origin is monzta1/project-handsoff reports the same latest
   release tag as `gh release view` does (the collector read GitHub for real).
   The refresh thread starts with the server, so a just-restarted Fleet gets
   up to 90 seconds for its first pass to land.

Set HANDSOFF_FLEET_URL to point the smoke at another Fleet port.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
FLEET = os.environ.get("HANDSOFF_FLEET_URL", "http://127.0.0.1:8765").rstrip("/")
REPO = "monzta1/project-handsoff"


def fail(message: str) -> int:
    print(f"LIVE_FLEET_SIGNALS_FAILED: {message}")
    return 1


def get(path: str, timeout: float = 10):
    with urllib.request.urlopen(f"{FLEET}{path}", timeout=timeout) as response:
        return response.read().decode("utf-8")


def main() -> int:
    expected = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    version = subprocess.run([HANDSOFF, "version", "--json"], capture_output=True, text=True, timeout=30)
    if version.returncode != 0:
        return fail(f"handsoff version failed: {version.stderr.strip()[:200]}")
    installed = json.loads(version.stdout).get("version", "").lstrip("v")
    if installed != expected:
        return fail(f"installed engine is {installed}, this checkout is {expected}: install the release first")
    print(f"installed engine v{installed} matches pyproject")

    try:
        app = get("/app.js")
    except OSError as exc:
        return fail(f"Fleet at {FLEET} is not answering: {exc}")
    if "signalsStrip" not in app:
        return fail("the served fleet/app.js has no signalsStrip: a stale asset is being served")
    print("served Fleet page carries the signals strip")

    tag = subprocess.run(["gh", "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"],
                         capture_output=True, text=True, timeout=30)
    if tag.returncode != 0:
        return fail(f"gh release view failed: {tag.stderr.strip()[:200]}")
    expected_tag = tag.stdout.strip()

    deadline = time.time() + 90
    last = None
    while time.time() < deadline:
        fleet = json.loads(get("/api/fleet"))
        projects = fleet.get("projects") or []
        if not projects:
            return fail("no project is registered with this Fleet")
        missing = [p.get("name") for p in projects if "github" not in p or "beakon" not in p]
        if missing:
            return fail(f"projects without both signal keys: {missing}")
        target = next((p for p in projects if (p.get("github") or {}).get("repo") == REPO), None)
        if target and target["github"].get("error") is None:
            got = (target["github"].get("latest_release") or {}).get("tag")
            if got != expected_tag:
                return fail(f"{REPO} latest release reads {got}, gh says {expected_tag}")
            beakon = target.get("beakon")
            print(f"{len(projects)} projects carry github and beakon; {target['name']}: "
                  f"{target['github']['open_issues']} issues, {target['github']['open_prs']} PRs, "
                  f"release {got}, beakon {'none (no worker)' if beakon is None else json.dumps(beakon)}")
            print("LIVE_FLEET_SIGNALS_OK")
            return 0
        last = target["github"] if target else f"no project with origin {REPO} among {[p.get('name') for p in projects]}"
        time.sleep(3)
    return fail(f"the GitHub signal for {REPO} did not land within 90 s: {last}")


if __name__ == "__main__":
    sys.exit(main())
