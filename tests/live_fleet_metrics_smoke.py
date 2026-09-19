"""#153 live smoke: the installed Fleet on :8765 serves the Metrics tab and
its data for real registered projects.

Three checks against the deployed engine, in order:

1. `handsoff version --json` reports this checkout's pyproject version.
2. /metrics and /metrics.js are served (the tab strip is in the page, the
   series code is in the script), so the served assets are the new ones.
3. /api/metrics reports a non-null refreshed_at not earlier than its
   started_at, waiting up to 120 s for a just-started server's first pass
   (the running installed collector refreshed, not a disk-loaded cache),
   and the entry whose repo is monzta1/project-handsoff has an issue count
   equal to gh's is:issue open plus closed totals read in the same minute.

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
    print(f"LIVE_FLEET_METRICS_FAILED: {message}")
    return 1


def get(path: str, timeout: float = 10):
    with urllib.request.urlopen(f"{FLEET}{path}", timeout=timeout) as response:
        return response.read().decode("utf-8")


def gh_count(query: str) -> int:
    proc = subprocess.run(["gh", "api", f"search/issues?q={query}&per_page=1", "--jq", ".total_count"],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    return int(proc.stdout.strip())


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
        page = get("/metrics")
        script = get("/metrics.js")
    except OSError as exc:
        return fail(f"Fleet at {FLEET} is not answering: {exc}")
    if 'href="/metrics" aria-current="page"' not in page:
        return fail("the served /metrics page has no METRICS tab: a stale asset is being served")
    if "function computeSeries" not in script:
        return fail("the served /metrics.js has no computeSeries: a stale asset is being served")
    print("served Metrics page and script are the new ones")

    deadline = time.time() + 120
    payload = None
    while time.time() < deadline:
        payload = json.loads(get("/api/metrics"))
        refreshed, started = payload.get("refreshed_at"), payload.get("started_at")
        if refreshed and started and refreshed >= started:
            break
        time.sleep(3)
    else:
        return fail(f"refreshed_at did not reach started_at within 120 s: {payload and {k: payload.get(k) for k in ('started_at', 'refreshed_at')}}")
    print(f"running collector refreshed at {payload['refreshed_at']} (server started {payload['started_at']})")

    target = next((p for p in payload["projects"] if p.get("repo") == REPO), None)
    if target is None:
        return fail(f"no project with origin {REPO} among {[p.get('name') for p in payload['projects']]}")
    if target.get("error"):
        return fail(f"{REPO} entry carries an error: {target['error']}")
    served = len(target["issues"])
    try:
        expected_count = gh_count(f"repo:{REPO}+is:issue+is:open") + gh_count(f"repo:{REPO}+is:issue+is:closed")
    except RuntimeError as exc:
        return fail(f"gh search failed: {exc}")
    if served != expected_count:
        return fail(f"{REPO} serves {served} issues, gh counts {expected_count} open plus closed")
    print(f"{REPO}: {served} issues match gh; {len(target.get('commits') or [])} commits since {target.get('commits_since')}; "
          f"{len(target.get('releases') or [])} releases")
    print("LIVE_FLEET_METRICS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
