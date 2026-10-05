"""#374: run-close --post reports on every work item.

A lane whose pull request body said `Closes #N` had its items closed by
GitHub at the merge, before `run-close --post` ran, and the post then
refused the whole report. A closure by a merged pull request whose head is
the run's branch is now the run's own; any other closure skips that item
alone. The playbook now tells the host to write `Refs #N`, so GitHub
leaves the item for run-close.
"""
import json
import subprocess
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, run
from tests.test_report_posting import ReportPostingFixture

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

BRANCH = "claude/lane-374"
MUTATIONS = (["issue", "comment"], ["issue", "close"], ["issue", "edit"], ["issue", "reopen"])


class EveryItemIsPosted(ReportPostingFixture):
    """REQ-001: a fake gh drives run-close --post item by item."""

    def setUp(self):
        super().setUp()
        # The run's branch, read the way a worktree's is.
        subprocess.run(["git", "init", "-q", "-b", BRANCH], cwd=self.tmp, check=True, capture_output=True)

    def _post(self):
        before = len(self._calls())
        result = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        mutations = [call[:3] for call in self._calls()[before:] if call[:2] in MUTATIONS]
        return result, mutations

    def _report_events(self):
        return [event for event in lib.read_events(self.tmp, lib.load_config(self.tmp))
                if event.get("kind") == "report_posted"]

    def _transaction_items(self):
        records = list((self.tmp / ".handsoff-archive" / "close-transactions").glob("*.json"))
        self.assertEqual(len(records), 1)
        return json.loads(records[0].read_text())["items"]

    def test_an_item_closed_by_the_runs_own_merged_pull_request_is_posted_and_not_reopened(self):
        self._run()
        state = self._gh_state()
        state["issues"]["40"]["closed"] = True
        state["prs"] = [{"number": 99, "headRefName": BRANCH, "closingIssuesReferences": [{"number": 40}]}]
        self._gh_state(state)
        result, mutations = self._post()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_REPORT_POSTED: #40, #41", result.stdout)
        self.assertNotIn("skipped", result.stdout)
        # every item commented once; only the open item is closed; nothing reopened
        self.assertEqual(mutations, [["issue", "comment", "40"], ["issue", "edit", "9"],
                                     ["issue", "comment", "41"], ["issue", "close", "41"]])
        state = self._gh_state()
        for number in ("40", "41"):
            self.assertEqual(len(state["issues"][number]["comments"]), 1, number)
            self.assertTrue(state["issues"][number]["comments"][0].startswith("<!-- handsoff-report "))
            self.assertTrue(state["issues"][number]["closed"], number)
        self.assertIn("- [x] #40 first story", state["issues"]["9"]["body"])
        posted = self._report_events()[-1]["posted"]
        self.assertEqual([(p["number"], p["comment"], p["closed"]) for p in posted],
                         [(40, "posted", True), (41, "posted", True)])

    def test_an_unpostable_item_is_skipped_and_named_while_the_others_post(self):
        self._run()
        state = self._gh_state()
        state["issues"]["40"]["closed"] = True
        # a merged pull request from another branch closing #40 is not the run's
        state["prs"] = [{"number": 98, "headRefName": "someone-else", "closingIssuesReferences": [{"number": 40}]}]
        self._gh_state(state)
        result, mutations = self._post()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_REPORT_POSTED: #41 (skipped: #40 closed without attributable Handsoff ownership",
                      result.stdout)
        self.assertEqual(mutations, [["issue", "comment", "41"], ["issue", "close", "41"]])
        state = self._gh_state()
        self.assertEqual(state["issues"]["40"]["comments"], [])
        self.assertEqual(len(state["issues"]["41"]["comments"]), 1)
        self.assertTrue(state["issues"]["41"]["closed"])
        event = self._report_events()[-1]
        self.assertEqual([p["number"] for p in event["posted"]], [41])
        self.assertEqual([(s["number"], s.get("unpostable")) for s in event["skipped"]], [(40, True)])
        self.assertTrue(self._transaction_items()["40"]["unpostable"])

    def _already_posted(self):
        self._run()
        state = self._gh_state()
        for number in ("40", "41"):
            state["issues"][number]["comments"] = ["<!-- handsoff-report " + "a" * 64 + " -->\nearlier"]
        return state

    def test_an_existing_comment_with_a_failing_close_stays_incomplete(self):
        self._gh_state({**self._already_posted(), "fail_close": True})
        result, mutations = self._post()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("final_report_post incomplete", result.stdout)
        self.assertNotIn(["issue", "comment", "40"], mutations, "deduplicated, never commented twice")
        self.assertIn(["issue", "close", "40"], mutations, "the close is still attempted")
        items = self._transaction_items()
        self.assertTrue(items["40"]["commented"])
        self.assertFalse(items["40"]["closed"])
        self.assertNotIn("unpostable", items["40"])

    def test_an_existing_comment_with_a_failing_tick_stays_incomplete(self):
        self._gh_state({**self._already_posted(), "fail_edit": True})
        result, mutations = self._post()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("final_report_post incomplete", result.stdout)
        self.assertIn(["issue", "edit", "9"], mutations, "the tick is still attempted")
        items = self._transaction_items()
        self.assertTrue(items["40"]["closed"])
        self.assertFalse(items["40"]["ticked"])
        self.assertIn("- [ ] #40", self._gh_state()["issues"]["9"]["body"])


class TheLandingPlaybookSaysRefsNotCloses(unittest.TestCase):
    """REQ-002: the sentence rides in the landing topic a host reads."""

    def test_landing_says_refs_never_closes_in_the_pull_request_body(self):
        text = " ".join(lib.playbook_section("landing").split())
        self.assertIn("Write `Refs #N`, never `Closes #N`, in the pull request body", text)


if __name__ == "__main__":
    unittest.main()
