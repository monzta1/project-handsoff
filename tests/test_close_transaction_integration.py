"""Focused integration coverage for the real close and PR-watch paths."""
from __future__ import annotations

import json
import sys
import unittest

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


class PullRequestIntegrationTests(unittest.TestCase):
    def test_ci_watch_boundary_refuses_auto_close_text(self):
        class Result:
            returncode = 0
            stdout = json.dumps({"title": "Fixes #271", "body": "implementation"})

        with self.assertRaisesRegex(lib.HandsoffError, "use Refs"):
            supervisor._enforce_refs_only_pr(BIN.parent, 88, runner=lambda *args, **kwargs: Result())


if __name__ == "__main__":
    unittest.main()
