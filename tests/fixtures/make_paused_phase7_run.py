#!/usr/bin/env python3
"""#466: build a scratch run for the browser click-through of REQ-003.

    python3 tests/fixtures/make_paused_phase7_run.py ROOT

ROOT (absent or empty) becomes a Sentinel-host run at Phase 7
`ready_to_deploy`, paused for performance review, with no deployment
approval, on a project that does not require one (as project-handsoff
does not). It is built by the same helpers tests.test_dashboard_follows_supervisor
uses, so the page shows what those tests assert: Resume with a runnable
command, then, once resumed, Authorize deployment. Serve it with the
branch's dashboard and click through both.

The fleet register and run archive this writes stay in temporary
directories, never the operator's own; nothing is cleaned up, since the
scratch run is the point.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests.test_dashboard_follows_supervisor import DeploymentGateCase  # noqa: E402


class _ScratchRun(DeploymentGateCase):
    def runTest(self):  # unittest needs a method name; nothing runs here
        pass


def build(root: Path) -> None:
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; name a fresh scratch directory")
    root.mkdir(parents=True, exist_ok=True)
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        # the run's own directory is ROOT; every other temp dir stays temp
        return str(root) if kwargs.get("prefix") == "handsoff-test-" else real_mkdtemp(*args, **kwargs)

    case = _ScratchRun()
    with mock.patch("tempfile.mkdtemp", side_effect=mkdtemp):
        case.setUp()  # never tearDown: it would delete ROOT
    case.phase7(approval_required=False, host="Sentinel")
    case.pause()
    status = case.read_status()
    print(f"Scratch run: {root}")
    print(f"Phase {status['phase_number']} {status['status']}, deployment approval: "
          f"{status.get('deployment_approved')}, paused for performance review")
    print(f"Fleet register (temporary): {os.environ.get('HANDSOFF_FLEET_REGISTRY')}")
    print(f"Serve it: python3 {REPO / 'bin' / 'handsoff_dashboard.py'} --root {root} --port <free> --no-open")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_paused_phase7_run.py ROOT")
    build(Path(sys.argv[1]))
