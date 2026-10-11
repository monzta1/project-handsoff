#!/usr/bin/env python3
"""#442: an accepted regression carries the launch deadline regression-run
enforces, every status reader shows it, and a launch before it is honoured."""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_schema as schema  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402

REAL_DATETIME = supervisor.datetime


def frozen_clock(moment):
    class Clock(REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return moment
    return mock.patch.object(supervisor, "datetime", Clock)


class AcceptedRegressionFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-accept-deadline-"))
        for name in ("handsoff.toml", ".gitignore"):
            shutil.copy(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.com"],
                    ["git", "config", "user.name", "Deadline Test"], ["git", "add", "."],
                    ["git", "commit", "-qm", "fixture"]):
            subprocess.run(cmd, cwd=self.root, check=True)
        for args in (("init", "Accept deadline"), ("release-plan", "--version", "v1.0.0", "--by", "Pilot")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=30,
        )

    def status(self):
        return json.loads((self.root / "handsoff-status.json").read_text())

    def accept(self):
        result = self.cli("regression-request", "--group", "python-full", "--by", "codex-supervisor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        request_id = self.status()["regression_requests"][-1]["request_id"]
        self.accepted_output = self.cli("regression-decide", "--request-id", request_id,
                                        "--accept", "--by", "Mission-Control-Pilot")
        self.assertEqual(self.accepted_output.returncode, 0,
                         self.accepted_output.stdout + self.accepted_output.stderr)
        return self.status()["regression_requests"][-1]

    def launch_at(self, item, moment):
        args = argparse.Namespace(root=str(self.root), request_id=item["request_id"], by="runner")
        result = [{"command": item["commands"][0], "exit_code": 0, "output_sha256": "0" * 64,
                   "duration_s": 0.01, "output_tail": "ok"}]
        with frozen_clock(moment), \
                mock.patch.object(supervisor.regress, "run_battery_results", return_value=result) as run, \
                mock.patch("builtins.print") as printed:
            code = supervisor.cmd_regression_run(args)
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        return code, run.called, output


class AcceptDeadlinePersisted(AcceptedRegressionFixture):
    def test_status_file_carries_the_enforced_deadline(self):
        item = self.accept()
        window = lib.load_config(self.root)["regression_gate"]["launch_window_minutes"]
        self.assertEqual(window, 10)
        self.assertEqual(datetime.fromisoformat(item["accept_expires_at"]),
                         datetime.fromisoformat(item["decided_at"]) + timedelta(minutes=window))
        self.assertLess(datetime.fromisoformat(item["accept_expires_at"]),
                        datetime.fromisoformat(item["expires_at"]))
        self.assertIn(f"(launch by {item['accept_expires_at']})", self.accepted_output.stdout)
        self.assertEqual(schema.validate_status_schema(self.status()), [])

    def test_status_cli_shows_the_deadline(self):
        item = self.accept()
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shown = json.loads(result.stdout)["regression_requests"][-1]
        self.assertEqual(shown["accept_expires_at"], item["accept_expires_at"])

    def test_dashboard_snapshot_shows_the_deadline(self):
        item = self.accept()
        snapshot = dashboard.build_snapshot(self.root.resolve())
        self.assertEqual(snapshot["regression"]["current"]["accept_expires_at"], item["accept_expires_at"])
        self.assertEqual(snapshot["regression"]["history"][0]["accept_expires_at"], item["accept_expires_at"])

    def test_decline_sets_no_launch_deadline(self):
        result = self.cli("regression-request", "--group", "python-full", "--by", "codex-supervisor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        request_id = self.status()["regression_requests"][-1]["request_id"]
        result = self.cli("regression-decide", "--request-id", request_id, "--decline", "--by", "Pilot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("accept_expires_at", self.status()["regression_requests"][-1])


class LaunchHonoursPersistedDeadline(AcceptedRegressionFixture):
    def test_launch_one_second_before_the_deadline_runs(self):
        item = self.accept()
        moment = datetime.fromisoformat(item["accept_expires_at"]) - timedelta(seconds=1)
        code, ran, output = self.launch_at(item, moment)
        self.assertNotIn("REGRESSION_EXPIRED", output)
        self.assertTrue(ran)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.status()["regression_requests"][-1]["state"], "completed")

    def test_launch_one_second_after_the_deadline_expires(self):
        item = self.accept()
        moment = datetime.fromisoformat(item["accept_expires_at"]) + timedelta(seconds=1)
        code, ran, output = self.launch_at(item, moment)
        self.assertEqual(code, 1)
        self.assertFalse(ran)
        self.assertIn(f"REGRESSION_EXPIRED: {item['request_id']}", output)
        self.assertEqual(self.status()["regression_requests"][-1]["state"], "expired")
        events = [json.loads(line) for line in (self.root / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["kind"], "regression_expired")

    def _store_deadline(self, deadline):
        """Rewrite the stored deadline through the chained commit, as an engine write would."""
        cfg = lib.load_config(self.root)
        with lib.project_lock(self.root):
            status = self.status()
            item = status["regression_requests"][-1]
            if deadline is None:
                del item["accept_expires_at"]
            else:
                item["accept_expires_at"] = deadline
            lib.commit(self.root, cfg, status=status, event_kind="regression_accepted",
                       event_message="test fixture: stored launch deadline", request_id=item["request_id"])
        return self.status()["regression_requests"][-1]

    def test_the_stored_deadline_not_a_recomputation_is_enforced(self):
        item = self.accept()
        decided = datetime.fromisoformat(item["decided_at"])
        later = self._store_deadline((decided + timedelta(minutes=20)).isoformat())
        code, ran, output = self.launch_at(later, decided + timedelta(minutes=11))
        self.assertNotIn("REGRESSION_EXPIRED", output)
        self.assertTrue(ran)
        self.assertEqual(code, 0, output)

    def test_an_earlier_stored_deadline_is_enforced(self):
        item = self.accept()
        decided = datetime.fromisoformat(item["decided_at"])
        earlier = self._store_deadline((decided + timedelta(minutes=2)).isoformat())
        code, ran, output = self.launch_at(earlier, decided + timedelta(minutes=3))
        self.assertEqual(code, 1)
        self.assertFalse(ran)
        self.assertIn("REGRESSION_EXPIRED", output)

    def test_legacy_request_without_the_field_keeps_the_old_window(self):
        item = self.accept()
        legacy = self._store_deadline(None)
        self.assertNotIn("accept_expires_at", legacy)
        decided = datetime.fromisoformat(item["decided_at"])
        code, ran, output = self.launch_at(legacy, decided + timedelta(minutes=11))
        self.assertEqual(code, 1)
        self.assertFalse(ran)
        self.assertIn("REGRESSION_EXPIRED", output)
        legacy = self.status()["regression_requests"][-1]
        self.assertEqual(legacy["state"], "expired")


class AcceptDeadlineSchema(AcceptedRegressionFixture):
    def test_aware_naive_non_string_and_absent_values(self):
        self.accept()
        base = self.status()
        self.assertEqual(schema.validate_status_schema(base), [])
        for bad in ("2026-10-10T21:26:37", "not a time", 1760131597, None, ""):
            with self.subTest(value=bad):
                status = copy.deepcopy(base)
                status["regression_requests"][-1]["accept_expires_at"] = bad
                errors = schema.validate_status_schema(status)
                self.assertTrue(any("accept_expires_at" in error for error in errors), errors)
        legacy = copy.deepcopy(base)
        del legacy["regression_requests"][-1]["accept_expires_at"]
        self.assertEqual(schema.validate_status_schema(legacy), [])

    def test_json_schema_declares_the_optional_property(self):
        document = json.loads((ROOT / "schemas" / "status.schema.json").read_text())
        items = document["properties"]["regression_requests"]["items"]
        self.assertEqual(items["properties"]["accept_expires_at"], {"type": "string", "format": "date-time"})
        self.assertNotIn("accept_expires_at", items["required"])
        self.assertFalse(items["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
