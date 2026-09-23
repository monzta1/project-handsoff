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
import handsoff_dashboard as dashboard  # noqa: E402


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
        result = self.cli("release-plan", "--version", "v1.0.0", "--by", "Pilot")
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
            lib.run_checks(cfg, self.root, commands=["python3 tests/shard.py --all"])
        self.assertEqual(
            lib.normalized_test_footprint("python ./tests/test_handsoff_supervisor.py -v", self.root),
            frozenset({"tests/test_handsoff_supervisor.py"}),
        )
        for bypass in (
            "python3 $(printf tests/test_handsoff_supervisor.py)",
            "python3 `printf tests/test_handsoff_supervisor.py`",
            "python3 tests/test_regression_gate.py; python3 tests/test_handsoff_supervisor.py",
        ):
            with self.subTest(bypass=bypass), self.assertRaisesRegex(
                    lib.HandsoffError, "shell expansion or control operators"):
                lib.normalized_test_footprint(bypass, self.root)

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
        with mock.patch.object(supervisor.regress, "run_battery_results", return_value=result) as run:
            self.assertEqual(supervisor.cmd_regression_run(args), 0)
        run.assert_called_once_with(
            self.root.resolve(), item["group"], item["commands"],
            timeout=item["timeout_seconds"],
            request_id=item["request_id"], command_sha256=item["command_sha256"],
            max_shards=supervisor.regress.DEFAULT_SHARDS,
        )
        final = self.status()["regression_requests"][-1]
        self.assertEqual(final["state"], "completed")
        self.assertRegex(final["launch_nonce_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("output_tail", final["results"][0])

    def test_regression_timeout_overrides_the_focused_check_timeout(self):
        cfg = lib.load_config(self.root)
        group = lib.regression_group(cfg, "python-full")
        self.assertEqual(cfg["check_timeout_seconds"], 600)
        self.assertEqual(group["timeout_seconds"], 1800)
        item = self.request()
        self.assertEqual(item["timeout_seconds"], 1800)

        path = self.root / "handsoff.toml"
        path.write_text(path.read_text().replace("timeout_seconds = 1800", "timeout_seconds = 0"))
        with self.assertRaisesRegex(lib.HandsoffError, "timeout_seconds must be a positive integer"):
            lib.load_config(self.root)

    def test_repository_change_invalidates_decision(self):
        item = self.request()
        (self.root / "handsoff.toml").write_text(
            (self.root / "handsoff.toml").read_text() + "\n# changed after request\n"
        )
        result = self.cli("regression-decide", "--request-id", item["request_id"],
                          "--accept", "--by", "Mission-Control-Pilot")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()["regression_requests"][-1]["state"], "invalidated")

    def test_configuration_change_during_run_invalidates_completion(self):
        item = self.request()
        accepted = self.cli("regression-decide", "--request-id", item["request_id"],
                            "--accept", "--by", "Mission-Control-Pilot")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        args = argparse.Namespace(root=str(self.root), request_id=item["request_id"], by="runner")

        def mutate_policy(*_args, **_kwargs):
            path = self.root / "handsoff.toml"
            path.write_text(path.read_text().replace("launch_window_minutes = 10",
                                                     "launch_window_minutes = 11"))
            return [{"command": item["commands"][0], "exit_code": 0,
                     "output_sha256": "0" * 64, "duration_s": 0.01, "output_tail": "ok"}]

        with mock.patch.object(supervisor.regress, "run_battery_results", side_effect=mutate_policy):
            self.assertEqual(supervisor.cmd_regression_run(args), 1)
        self.assertEqual(self.status()["regression_requests"][-1]["state"], "invalidated")

    def test_progress_telemetry_must_match_request_and_command_hash(self):
        request = {"request_id": "rg-current", "command_sha256": "a" * 64}
        path = self.root / ".handsoff-regression.json"
        payload = {**request, "totals": {"total": 8, "done": 3}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(dashboard._regression_progress(self.root, request), payload)
        self.assertIsNone(dashboard._regression_progress(
            self.root, {**request, "request_id": "rg-new"}))
        self.assertIsNone(dashboard._regression_progress(
            self.root, {**request, "command_sha256": "b" * 64}))

    def test_repository_snapshot_uses_default_branch_merge_base(self):
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "switch", "-qc", "feature-test"], cwd=self.root, check=True)
        (self.root / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "feature"], cwd=self.root, check=True)
        snapshot = lib.repository_snapshot(self.root)
        self.assertEqual(snapshot["commit_pair"]["before"], base)


if __name__ == "__main__":
    unittest.main()
