"""Focused integration coverage for the real close and PR-watch paths."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


class CloseCommandIntegrationTests(HandsoffTestCase):
    def test_run_close_persists_and_completes_the_ordered_transaction(self):
        self.init("Transactional close")
        result = run(["run-close", "--by", "tester", "--reason", "done"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        record = json.loads((self.tmp / payload["transaction"]["path"]).read_text())
        self.assertEqual(record["state"], "complete")
        self.assertEqual(set(record["steps"]), {
            "prepare", "final_report_post", "archive", "fleet_unregister",
            "dashboard_shutdown", "config_restore", "optional_analysis",
        })
        self.assertEqual(record["steps"]["final_report_post"]["state"], "skipped")
        self.assertEqual(record["steps"]["optional_analysis"]["state"], "skipped")

    def test_reopen_creates_a_new_close_episode(self):
        self.init("Repeat close")
        self.assertEqual(run(["run-close", "--by", "tester", "--reason", "one"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["run-reopen", "--by", "tester", "--reason", "retry"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["run-close", "--by", "tester", "--reason", "two"], cwd=self.tmp).returncode, 0)
        records = list((self.tmp / ".handsoff-archive" / "close-transactions").glob("*.json"))
        self.assertEqual(len(records), 2)


class ConfigOverrideRestoreTests(HandsoffTestCase):
    """REQ-002 (#382): config-override writes one value for this run only and
    run-close's config_restore step restores, adopts or preserves it."""

    KEY = "workflow.stall_minutes"

    def setUp(self):
        super().setUp()
        self.init("Run-scoped config")
        self.toml = self.tmp / "handsoff.toml"

    def override(self, key, value):
        return run(["config-override", "--key", key, "--value", value, "--by", "tester"], cwd=self.tmp)

    def close(self):
        result = run(["run-close", "--by", "tester", "--reason", "done"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.splitlines()[-1])
        record = json.loads(Path(payload["transaction"]["path"]).read_text())
        self.assertEqual(record["steps"]["config_restore"]["state"], "complete")
        return record.get("config_restore") or {}

    def test_the_run_value_is_restored_and_a_second_override_keeps_the_first_original(self):
        before = self.toml.read_bytes()
        original = lib.read_config_value(self.tmp, self.KEY)
        self.assertIsInstance(original, int)
        for value in ("25", "30"):
            result = self.override(self.KEY, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CONFIG_OVERRIDE_RECORDED", result.stdout)
        self.assertEqual(lib.read_config_value(self.tmp, self.KEY), 30)
        self.assertEqual(lib.load_config(self.tmp)["stall_minutes"], 30)
        [entry] = self.read_status()["config_overrides"]
        self.assertEqual({k: entry[k] for k in ("path", "key", "original", "run_written", "by")},
                         {"path": "handsoff.toml", "key": self.KEY, "original": original,
                          "run_written": 30, "by": "tester"})
        decisions = self.close()
        self.assertEqual(decisions[self.KEY]["decision"], "restore")
        self.assertEqual(self.toml.read_bytes(), before)

    def test_a_file_already_back_at_the_original_is_adopted(self):
        original = lib.read_config_value(self.tmp, self.KEY)
        self.assertEqual(self.override(self.KEY, "25").returncode, 0)
        text = self.toml.read_text()
        self.toml.write_text(text.replace("stall_minutes = 25", f"stall_minutes = {original}"))
        reverted = self.toml.read_bytes()
        decisions = self.close()
        self.assertEqual(decisions[self.KEY]["decision"], "adopt")
        self.assertEqual(self.toml.read_bytes(), reverted)

    def test_a_human_edit_since_the_override_is_preserved(self):
        self.assertEqual(self.override(self.KEY, "25").returncode, 0)
        text = self.toml.read_text()
        self.assertIn("stall_minutes = 25", text)
        self.toml.write_text(text.replace("stall_minutes = 25", "stall_minutes = 40"))
        decisions = self.close()
        self.assertEqual(decisions[self.KEY]["decision"], "preserve")
        self.assertEqual(lib.read_config_value(self.tmp, self.KEY), 40)

    def test_an_absent_original_is_removed_again(self):
        key = "workflow.max_autonomous_design_reviews"
        before = self.toml.read_bytes()
        self.assertIsNone(lib.read_config_value(self.tmp, key))
        result = self.override(key, "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lib.read_config_value(self.tmp, key), 2)
        self.assertIsNone(self.read_status()["config_overrides"][0]["original"])
        decisions = self.close()
        self.assertEqual(decisions[key]["decision"], "restore")
        self.assertIsNone(lib.read_config_value(self.tmp, key))
        self.assertEqual(self.toml.read_bytes(), before)

    def test_a_key_the_config_schema_does_not_know_is_refused(self):
        before = self.toml.read_bytes()
        for key, value in (("workflow.no_such_setting", "1"), ("project.name", "x"), ("stall_minutes", "5")):
            result = self.override(key, value)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("CONFIG_OVERRIDE_BLOCKED", result.stdout)
            self.assertIn("is not one the config schema knows", result.stdout)
        result = self.override(self.KEY, "soon")
        self.assertEqual(result.returncode, 1)
        self.assertIn("takes int values, not 'soon'", result.stdout)
        self.assertEqual(self.toml.read_bytes(), before)
        self.assertNotIn("config_overrides", self.read_status())
        self.assertEqual(self.close(), {})


class CloseArchiveAndDashboardTests(HandsoffTestCase):
    """REQ-024 (#381): the archive step and the dashboard release."""

    def _close(self):
        result = run(["run-close", "--by", "tester", "--reason", "done"], cwd=self.tmp)
        # the release lines print before the result object
        return result, (json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else None)

    def _reopen_archive_step(self, transaction_path: Path) -> None:
        """Make the close resume at its archive step, as after a crash there."""
        record = json.loads(transaction_path.read_text())
        record["steps"]["archive"] = {"state": "pending", "attempts": 0, "intent_at": None,
                                      "completed_at": None, "last_observed": None, "last_error": None}
        record["state"], record["completed_at"] = "open", None
        transaction_path.write_text(json.dumps(record))

    def test_the_archive_is_written_once_and_a_second_close_leaves_it_byte_identical(self):
        self.init("Archive once")
        result, payload = self._close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = Path(payload["archive"]["path"])
        transaction_path = Path(payload["transaction"]["path"])
        first = archive.read_bytes()
        written = json.loads(first)
        self.assertEqual(written["run_token"], transaction_path.stem)
        self.assertEqual(written["run_closed"]["by"], "tester")
        record = json.loads(transaction_path.read_text())
        self.assertEqual(record["steps"]["archive"]["completion"], "read_back")

        self._reopen_archive_step(transaction_path)
        again, _payload = self._close()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(archive.read_bytes(), first)
        record = json.loads(transaction_path.read_text())
        self.assertEqual((record["state"], record["steps"]["archive"]["completion"]), ("complete", "adopted"))

    def test_a_differing_archive_is_refused_and_never_overwritten(self):
        self.init("Archive conflict")
        result, payload = self._close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = Path(payload["archive"]["path"])
        archive.write_text('{"tampered": true}\n')
        self._reopen_archive_step(Path(payload["transaction"]["path"]))
        again, _payload = self._close()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("archive incomplete", again.stdout)
        self.assertIn("CloseConflict", again.stdout)
        self.assertEqual(archive.read_text(), '{"tampered": true}\n')

    def _owner(self, *, root_sha256: str, pid: int) -> Path:
        path = lib.dashboard_owner_path(self.tmp)
        path.write_text(json.dumps({"pid": pid, "host": "127.0.0.1", "port": 9, "owner": lib.DASHBOARD_OWNER,
                                    "run_token": "t" * 32, "root_sha256": root_sha256, "feature": "x"}))
        return path

    def test_a_dashboard_another_run_owns_is_not_released(self):
        self.init("Foreign dashboard")
        other = lib.dashboard_root_sha256(self.tmp / "another-run")
        owner = self._owner(root_sha256=other, pid=os.getpid())
        before = owner.read_bytes()
        result, payload = self._close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(owner.read_bytes(), before, "the owner record is left alone")
        step = json.loads(Path(payload["transaction"]["path"]).read_text())["steps"]["dashboard_shutdown"]
        self.assertEqual(step["last_observed"]["state"], "foreign")
        self.assertEqual(step["completion"], "adopted")
        self.assertNotIn("HANDSOFF_DASHBOARD", result.stdout)

    def test_a_stale_owner_record_of_this_root_is_removed_without_touching_a_process(self):
        self.init("Stale dashboard")
        exited = subprocess.Popen(["true"])
        exited.wait()
        owner = self._owner(root_sha256=lib.dashboard_root_sha256(self.tmp), pid=exited.pid)
        decided = supervisor._dashboard_ownership(self.tmp)
        self.assertEqual(decided["state"], "stale", decided)
        result, payload = self._close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(owner.exists())
        self.assertIn("HANDSOFF_DASHBOARD_RELEASE_SKIPPED: stale ownership metadata removed", result.stdout)
        step = json.loads(Path(payload["transaction"]["path"]).read_text())["steps"]["dashboard_shutdown"]
        self.assertEqual((step["last_observed"]["state"], step["completion"]), ("absent", "read_back"))


class PullRequestIntegrationTests(unittest.TestCase):
    def test_ci_watch_boundary_refuses_auto_close_text(self):
        class Result:
            returncode = 0
            stdout = json.dumps({"title": "Fixes #271", "body": "implementation"})

        with self.assertRaisesRegex(lib.HandsoffError, "use Refs"):
            supervisor._enforce_refs_only_pr(BIN.parent, 88, runner=lambda *args, **kwargs: Result())


if __name__ == "__main__":
    unittest.main()
