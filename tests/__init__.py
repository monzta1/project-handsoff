"""Test-wide guards.

#166: `init` registers the run in the fleet register and takes the ticket
lock through it. Every test module is imported as `tests.<name>`, so this
runs first and points HANDSOFF_FLEET_REGISTRY at a per-process temp file;
subprocesses inherit it. A test that sets its own value keeps it. The
operator's ~/.handsoff/projects.json is never touched by a test run.
"""
import os
import tempfile

if not os.environ.get("HANDSOFF_FLEET_REGISTRY"):
    os.environ["HANDSOFF_FLEET_REGISTRY"] = os.path.join(
        tempfile.mkdtemp(prefix="handsoff-test-registry-"), "projects.json")
