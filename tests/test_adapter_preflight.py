"""Focused pre-flight coverage, using fake executables so no provider is contacted."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import handsoff_lib as lib


class AdapterPreflightTests(unittest.TestCase):
    """These tests exercise pre-flight itself with fake executables, so the
    fixture-wide skip must be lifted here and restored afterwards."""

    def setUp(self):
        import os
        self._skip = os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None)

    def tearDown(self):
        import os
        if self._skip is not None:
            os.environ["HANDSOFF_SKIP_PREFLIGHT"] = self._skip

    def fake(self, root, code):
        path = root / "fake"
        path.write_text("#!/bin/sh\n" + code)
        path.chmod(0o755)
        return str(path)

    def test_reachable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); exe = self.fake(root, "exit 0\n")
            self.assertEqual(lib.adapter_preflight({"adapters": {"codex": exe}}, root, lambda _: None)["codex"]["state"], "reachable")

    def test_unreachable_reason_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); exe = self.fake(root, "echo 'Permission denied' >&2; exit 3\n")
            item = lib.adapter_preflight({"adapters": {"codex": exe}}, root, lambda _: None)["codex"]
            self.assertIn("exit code 3", item["reason"])

    def test_missing_is_not_checked(self):
        with tempfile.TemporaryDirectory() as d:
            item = lib.adapter_preflight({"adapters": {}}, Path(d), lambda _: None)["claude"]
            self.assertEqual(item["state"], "not_checked")

    def test_environment_failure_classification(self):
        self.assertEqual(lib.classify_runtime_failure(exit_code=1, stderr_tail="SSL certificate verification failed")["category"], "runtime_environment")

    def test_environment_failure_skips_all_fallbacks(self):
        result = lib.plan_agent_fallback("implementer", "runtime_environment", [{"adapter": "codex", "model": "gpt-5"}], {"codex": True, "claude": True}, [], 0, 2)
        self.assertEqual(result["skipped"][0]["reason"], "environment_failure")

    def test_preflight_file_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); lib.adapter_preflight({"adapters": {}}, root, lambda _: None)
            self.assertTrue((root / lib.PREFLIGHT_FILE).is_file())


if __name__ == "__main__":
    unittest.main()
