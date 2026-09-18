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


class PreflightUsesLaunchArgvTests(unittest.TestCase):
    """Field-note defect 1: pre-flight passed with a hand-written text-mode argv
    while the launch argv (stream-json) was refused by the CLI. The probe now
    uses the launcher's own argv helpers, so such a flag fails pre-flight."""

    def setUp(self):
        import os
        self._skip = os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None)

    def tearDown(self):
        import os
        if self._skip is not None:
            os.environ["HANDSOFF_SKIP_PREFLIGHT"] = self._skip

    def test_probe_argv_matches_launch_shape(self):
        seen = {}

        class Completed:
            returncode = 0
            stderr = ""

        def runner(argv, **kwargs):
            seen[Path(argv[0]).name] = list(argv)
            return Completed()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            claude = root / "claude"; claude.write_text("#!/bin/sh\nexit 0\n"); claude.chmod(0o755)
            codex = root / "codex"; codex.write_text("#!/bin/sh\nexit 0\n"); codex.chmod(0o755)
            lib.adapter_preflight({"adapters": {"codex": str(codex), "claude": str(claude)}}, root, lambda _: None, runner=runner)
        claude_argv = seen["claude"]
        self.assertEqual(claude_argv[1:], lib.claude_argv(str(claude), "reviewer", [], lib.DEFAULT_AGENT_MODEL)[1:])
        i = claude_argv.index("--output-format")
        self.assertEqual(claude_argv[i - 1], "--verbose")
        self.assertEqual(claude_argv[i + 1], "stream-json")
        self.assertNotIn("--model", claude_argv)
        codex_argv = seen["codex"]
        self.assertEqual(codex_argv[1:], lib.codex_argv(str(codex), "reviewer", lib.DEFAULT_AGENT_MODEL, lib.MIN_AGENT_TOKEN_BUDGET)[1:])
        self.assertIn("read-only", codex_argv)
        self.assertEqual(codex_argv[-1], "-")

    def test_flag_the_cli_refuses_fails_preflight(self):
        """A stand-in CLI that rejects --verbose+stream-json the way the real one
        rejected stream-json without it: the probe must report unreachable."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            claude = root / "claude"
            claude.write_text('#!/bin/sh\ncase "$*" in *"--output-format stream-json"*) echo "Error: refused flag" >&2; exit 1;; esac\nexit 0\n')
            claude.chmod(0o755)
            item = lib.adapter_preflight({"adapters": {"claude": str(claude)}}, root, lambda _: None)["claude"]
        self.assertEqual(item["state"], "unreachable")
        self.assertIn("refused flag", item["reason"])
