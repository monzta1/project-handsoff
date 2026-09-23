import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = code, stdout, stderr


class Runner:
    def __init__(self, login=Result(), probe=Result(stdout="OK")):
        self.login, self.probe, self.calls = login, probe, []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self.login if "status" in argv else self.probe


class LaunchPreflightTests(unittest.TestCase):
    def call(self, root, runner, **kwargs):
        executable = root / "codex"
        if not executable.exists():
            executable.write_text("fake")
        return lib.launch_preflight(
            root, adapter="codex", model="gpt-6-astra", executable=str(executable),
            argv=[str(executable), "exec", "--model", "gpt-6-astra"], cwd=str(root),
            runner=runner, state_dir=kwargs.pop("state_dir", root / "state"), **kwargs,
        )

    def test_unwritable_runtime_shape_blocks_before_any_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root / "state"; state.write_text("not a directory")
            runner = Runner()
            result = self.call(root, runner, state_dir=state)
            self.assertEqual((result["state"], result["category"]),
                             ("blocked", "runtime_environment"))
            self.assertEqual(runner.calls, [])

    def test_auth_failure_never_reaches_provider_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "state").mkdir(); runner = Runner(Result(1, stderr="401 unauthorized"))
            result = self.call(root, runner)
            self.assertEqual(result["category"], "auth_failure")
            self.assertEqual(len(runner.calls), 1)

    def test_equivalent_failure_is_cached_and_coalesced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "state").mkdir()
            runner = Runner(probe=Result(1, stderr="model is not supported"))
            now = datetime.now(timezone.utc)
            first = self.call(root, runner, now=now)
            second = self.call(root, runner, now=now + timedelta(seconds=1))
            self.assertFalse(first["cached"]); self.assertTrue(second["cached"])
            self.assertEqual(len(runner.calls), 2)
            view = lib.launch_preflight_snapshot(root)
            self.assertEqual((view["state"], view["avoided_retries"]), ("blocked", 1))
            self.assertEqual(view["incident"]["category"], "model_unavailable")

    def test_corrected_fingerprint_runs_and_clears_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "state").mkdir(); now = datetime.now(timezone.utc)
            failed = Runner(probe=Result(1, stderr="model unavailable token=secret"))
            self.call(root, failed, now=now)
            executable = root / "codex"; executable.write_text("changed executable")
            ready = Runner()
            result = lib.launch_preflight(
                root, adapter="codex", model="gpt-6-astra", executable=str(executable),
                argv=[str(executable), "exec", "--model", "gpt-6-astra"], cwd=str(root),
                runner=ready, state_dir=root / "state", now=now + timedelta(seconds=2),
            )
            self.assertEqual(result["state"], "ready")
            self.assertIsNone(lib.launch_preflight_snapshot(root)["incident"])


if __name__ == "__main__":
    unittest.main()
