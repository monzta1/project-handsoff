#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


class RegressionGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-regression-gate-"))
        for name in ("handsoff.toml", ".gitignore"):
            shutil.copy(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Gate Test"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)
        result = self.cli("init", "Regression gate #28")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=20,
        )

    def status(self):
        return json.loads((self.root / "handsoff-status.json").read_text())

    def request(self):
        result = self.cli("regression-request", "--group", "python-full", "--by", "codex-supervisor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.status()["regression_requests"][-1]

    def test_equivalent_full_suite_is_blocked_outside_gate(self):
        cfg = lib.load_config(self.root)
        with self.assertRaisesRegex(lib.HandsoffError, "full regression blocked"):
            lib.run_checks(cfg, self.root, commands=["python -m unittest tests.test_handsoff_supervisor"])
        self.assertEqual(
            lib.normalized_test_footprint("python ./tests/test_handsoff_supervisor.py -v", self.root),
            frozenset({"tests/test_handsoff_supervisor.py"}),
        )

    def test_request_can_be_declined_without_launch(self):
        item = self.request()
        result = self.cli("regression-decide", "--request-id", item["request_id"],
                          "--decline", "--by", "Mission-Control-Pilot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        final = self.status()["regression_requests"][-1]
        self.assertEqual(final["state"], "declined")
        self.assertIsNone(final["launch_nonce_sha256"])
        self.assertEqual(final["results"], [])

    def test_only_accepted_bound_request_can_launch(self):
        item = self.request()
        blocked = self.cli("regression-run", "--request-id", item["request_id"], "--by", "runner")
        self.assertNotEqual(blocked.returncode, 0)
        accepted = self.cli("regression-decide", "--request-id", item["request_id"],
                            "--accept", "--by", "Mission-Control-Pilot")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        args = argparse.Namespace(root=str(self.root), request_id=item["request_id"], by="runner")
        result = [{"command": item["commands"][0], "exit_code": 0, "output_sha256": "0" * 64,
                   "duration_s": 0.01, "output_tail": "ok"}]
        with mock.patch.object(lib, "run_checks", return_value=result) as run:
            self.assertEqual(supervisor.cmd_regression_run(args), 0)
        run.assert_called_once_with(lib.load_config(self.root), self.root.resolve(),
                                    commands=item["commands"], allow_regression=True)
        final = self.status()["regression_requests"][-1]
        self.assertEqual(final["state"], "completed")
        self.assertRegex(final["launch_nonce_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("output_tail", final["results"][0])

    def test_repository_change_invalidates_decision(self):
        item = self.request()
        (self.root / "handsoff.toml").write_text(
            (self.root / "handsoff.toml").read_text() + "\n# changed after request\n"
        )
        result = self.cli("regression-decide", "--request-id", item["request_id"],
                          "--accept", "--by", "Mission-Control-Pilot")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()["regression_requests"][-1]["state"], "invalidated")


if __name__ == "__main__":
    unittest.main()
