"""#171: the final report posts itself. The report is fixed wording from an
allowlist of ledger fields; posting goes through a fake gh on PATH that
records every call; a second post is a no-op; a credential shape or a
missing gh login means nothing leaves."""
import json
import os
import re
import stat
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_GH_LOG"])
state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"issues": {}, "auth": True}
args = sys.argv[1:]
with log.open("a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:2] == ["auth", "status"]:
    sys.exit(0 if state.get("auth", True) else 1)
if args[:2] == ["issue", "view"] and args[-1] == "state" and state.get("fail_close_readback"):
    sys.exit(1)
if args[:2] == ["issue", "view"]:
    number = args[2]
    issue = state["issues"].setdefault(number, {"body": "", "comments": [], "url": f"https://example.test/issues/{number}", "closed": False})
    if "--jq" in args:
        print(json.dumps(issue["comments"]))
    else:
        print(json.dumps({"body": issue["body"], "url": issue["url"], "state": "CLOSED" if issue.get("closed") else "OPEN",
                          "comments": [{"body": c} for c in issue["comments"]]}))
    sys.exit(0)
if args[:2] == ["issue", "close"] and state.get("fail_close"):
    sys.exit(1)
if args[:2] == ["issue", "edit"] and state.get("fail_edit"):
    sys.exit(1)
if args[:2] == ["issue", "comment"]:
    number = args[2]
    body = args[args.index("--body") + 1]
    issue = state["issues"].setdefault(number, {"body": "", "comments": [], "url": f"https://example.test/issues/{number}", "closed": False})
    issue["comments"].append(body)
    state_path.write_text(json.dumps(state))
    print(f"https://example.test/issues/{number}#issuecomment-{len(issue['comments'])}")
    sys.exit(0)
if args[:2] == ["issue", "close"]:
    state["issues"].setdefault(args[2], {"body": "", "comments": [], "url": "", "closed": False})["closed"] = True
    state_path.write_text(json.dumps(state))
    sys.exit(0)
if args[:2] == ["issue", "edit"]:
    state["issues"].setdefault(args[2], {"body": "", "comments": [], "url": "", "closed": False})["body"] = args[args.index("--body") + 1]
    state_path.write_text(json.dumps(state))
    sys.exit(0)
sys.exit(2)
'''


class ReportPostingTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "gh.log"
        self.state = self.tmp / "gh-state.json"
        self._env_before = {k: os.environ.get(k) for k in ("PATH", "FAKE_GH_LOG", "FAKE_GH_STATE")}
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        os.environ["FAKE_GH_LOG"] = str(self.log)
        os.environ["FAKE_GH_STATE"] = str(self.state)
        self._gh_state({"auth": True, "issues": {
            "40": {"body": "Parent: #9\n\nthe story", "comments": [], "url": "https://example.test/issues/40", "closed": False},
            "41": {"body": "no parent here", "comments": [], "url": "https://example.test/issues/41", "closed": False},
            "9": {"body": "Epic\n- [ ] #40 first story\n- [ ] #41 second story\n", "comments": [], "url": "https://example.test/issues/9", "closed": False},
        }})

    def tearDown(self):
        for key, value in self._env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def _gh_state(self, value=None):
        if value is not None:
            self.state.write_text(json.dumps(value))
        return json.loads(self.state.read_text())

    def _calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def _run(self):
        r = run(["init", "Ship the story", "--item", "#40 first story", "--item", "#41 second story"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        run(["criterion-update", "REQ-001", "--requirement", "[#40] the story works end to end and " + "x" * 200,
             "--test", "true"], cwd=self.tmp)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        run(["verify", "--criterion", "REQ-001", "--by", "impl"], cwd=self.tmp)

    def _loaded(self):
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        events = lib.read_events(self.tmp, cfg)
        records, _ = lib.load_verifications(self.tmp, cfg)
        return cfg, status, acceptance, events, records

    def test_the_report_is_fixed_wording_from_ledger_fields_and_matches_the_golden_file(self):
        self._run()
        cfg, status, acceptance, events, records = self._loaded()

        def runner(args, **kw):
            class P:
                returncode = 0
                stdout = "abc1234 Ship the story\n" if "log" in args else "origin/main\n"
                stderr = ""
            return P()
        text = lib.render_final_report(self.tmp, cfg, status, acceptance, events, records, ["SHIP_FEATURE_VALID"], runner=runner)
        # volatile fields are normalised before comparing with the golden file
        normalised = re.sub(r"vr-[0-9a-f]{32}", "vr-RUN", text)
        normalised = re.sub(r"\d{4}-\d{2}-\d{2}T[0-9:.+]+", "TIME", normalised)
        golden = Path(__file__).parent / "fixtures" / "final_report.md"
        if os.environ.get("HANDSOFF_UPDATE_GOLDEN") == "1":
            golden.write_text(normalised)
        self.assertEqual(normalised, golden.read_text())
        # the allowlist: a long requirement is cut, the home path is a tilde
        self.assertIn("x" * 60, text)
        self.assertNotIn("x" * 150, text)
        self.assertNotIn(str(Path.home()), text)
        self.assertIn("### Validate", text)
        self.assertIn("SHIP_FEATURE_VALID", text)

    def test_post_posts_once_per_item_closes_and_ticks_the_epic_and_a_second_post_is_a_no_op(self):
        self._run()
        r = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HANDSOFF_REPORT_POSTED: #40, #41", r.stdout)
        state = self._gh_state()
        self.assertEqual(len(state["issues"]["40"]["comments"]), 1)
        self.assertEqual(len(state["issues"]["41"]["comments"]), 1)
        comment = state["issues"]["40"]["comments"][0]
        self.assertTrue(comment.startswith("<!-- handsoff-report "))
        self.assertIn("## Handsoff report: Ship the story", comment)
        self.assertTrue(state["issues"]["40"]["closed"] and state["issues"]["41"]["closed"])
        self.assertIn("- [x] #40 first story", state["issues"]["9"]["body"])
        self.assertIn("- [ ] #41 second story", state["issues"]["9"]["body"], "41 has no parent line")
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        posted = [e for e in events if e["kind"] == "report_posted"]
        self.assertEqual(len(posted), 1)
        self.assertEqual([p["number"] for p in posted[0]["posted"]], [40, 41])
        self.assertTrue(posted[0]["posted"][0]["url"].startswith("https://example.test/issues/40#issuecomment-"))
        self.assertTrue(posted[0]["posted"][0]["ticked"])
        # nothing in the comment came from the pilot note or agent output
        self.assertNotIn("pilot", comment.lower())
        # a second post finds the marker and posts nothing more
        r = run(["run-reopen", "--by", "moncy", "--reason", "again"], cwd=self.tmp)
        r = run(["run-close", "--by", "moncy", "--reason", "shipped again", "--post"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = self._gh_state()
        self.assertEqual(len(state["issues"]["40"]["comments"]), 1)
        self.assertIn("skipped: #40, #41", r.stdout)

    def test_a_failed_close_or_tick_is_retried_on_the_next_post_without_a_second_comment(self):
        self._run()
        self._gh_state({**self._gh_state(), "fail_close": True, "fail_edit": True})
        r = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("final_report_post incomplete", r.stdout)
        state = self._gh_state()
        self.assertEqual(len(state["issues"]["40"]["comments"]), 1)
        self.assertFalse(state["issues"]["40"]["closed"])
        self.assertIn("- [ ] #40", state["issues"]["9"]["body"])
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        first = [e for e in events if e["kind"] == "report_posted"][-1]["posted"]
        self.assertEqual((first[0]["closed"], first[0]["ticked"], first[0]["comment"]), (False, False, "posted"))
        # GitHub recovers: the next post closes and ticks, and never comments again
        self._gh_state({**self._gh_state(), "fail_close": False, "fail_edit": False})
        r = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = self._gh_state()
        self.assertEqual(len(state["issues"]["40"]["comments"]), 1, "no second comment")
        self.assertTrue(state["issues"]["40"]["closed"] and state["issues"]["41"]["closed"])
        self.assertIn("- [x] #40", state["issues"]["9"]["body"])
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        second = [e for e in events if e["kind"] == "report_posted"][-1]["posted"]
        self.assertEqual((second[0]["closed"], second[0]["ticked"], second[0]["comment"]), (True, True, "skipped"))
        # and once everything is done, a third post touches nothing
        run(["run-reopen", "--by", "moncy", "--reason", "again"], cwd=self.tmp)
        before = len(self._calls())
        r = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        mutations = [c for c in self._calls()[before:] if c[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "edit"])]
        self.assertEqual(mutations, [])

    def test_an_unknown_closed_issue_pauses_before_any_github_mutation(self):
        self._run()
        state = self._gh_state()
        state["issues"]["40"]["closed"] = True
        self._gh_state(state)
        before = len(self._calls())
        result = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("closed without attributable Handsoff ownership", result.stdout)
        mutations = [call for call in self._calls()[before:]
                     if call[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "edit"])]
        self.assertEqual(mutations, [])

    def test_a_lost_close_readback_is_attributed_and_reconciled_on_retry(self):
        self._run()
        self._gh_state({**self._gh_state(), "fail_close_readback": True})
        first = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(self._gh_state()["issues"]["40"]["closed"])
        records = list((self.tmp / ".handsoff-archive" / "close-transactions").glob("*.json"))
        record = json.loads(records[0].read_text())
        self.assertTrue(record["items"]["40"]["closed_intent"])
        self.assertTrue(record["items"]["40"]["closed_dispatched"])

        self._gh_state({**self._gh_state(), "fail_close_readback": False})
        retry = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertNotIn("closed without attributable Handsoff ownership", retry.stdout)

    def test_failed_close_intent_does_not_attribute_a_later_human_closure(self):
        self._run()
        self._gh_state({**self._gh_state(), "fail_close": True, "fail_edit": True})
        first = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertNotEqual(first.returncode, 0)
        records = list((self.tmp / ".handsoff-archive" / "close-transactions").glob("*.json"))
        record = json.loads(records[0].read_text())
        self.assertTrue(record["items"]["40"]["closed_intent"])
        self.assertNotIn("closed_dispatched", record["items"]["40"])

        state = self._gh_state()
        state["issues"]["40"]["closed"] = True
        state["fail_close"] = False
        state["fail_edit"] = False
        self._gh_state(state)
        before = len(self._calls())
        retry = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("closed without attributable Handsoff ownership", retry.stdout)
        mutations = [call for call in self._calls()[before:]
                     if call[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "edit"])]
        self.assertEqual(mutations, [])

    def test_without_post_nothing_leaves_and_no_gh_auth_records_not_posted(self):
        self._run()
        r = run(["run-close", "--by", "moncy", "--reason", "quiet"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._calls(), [], "no gh call without --post")
        run(["run-reopen", "--by", "moncy", "--reason", "again"], cwd=self.tmp)
        self._gh_state({**self._gh_state(), "auth": False})
        r = run(["run-close", "--by", "moncy", "--reason", "shipped", "--post"], cwd=self.tmp)
        self.assertIn("HANDSOFF_REPORT_NOT_POSTED: gh is not authenticated", r.stdout)
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual([e["reason"] for e in events if e["kind"] == "report_not_posted"], ["gh_auth"])
        self.assertEqual([c for c in self._calls() if c[:2] == ["issue", "comment"]], [])

    def test_a_credential_shape_in_a_criterion_or_gate_message_never_leaves(self):
        self._run()
        run(["criterion-add", "REQ-002", "--type", "supporting", "--verification", "automated", "--test", "true",
             "--requirement", "[#41] rotate the key ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab before release"], cwd=self.tmp)
        cfg, status, acceptance, events, records = self._loaded()
        text = lib.render_final_report(self.tmp, cfg, status, acceptance, events, records, ["SHIP_FEATURE_VALID"])
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", text, "the output redactor caught it")
        self.assertIn("[REDACTED]", text)
        # a shape the redactor does not know still refuses the post
        validate = ["SHIP_FEATURE_BLOCKED", "- live gate: token xoxb-123456789012-abcdefghijkl leaked into a message"]
        outcome = lib.post_final_report(self.tmp, cfg, status, acceptance, events, records, validate, by="moncy")
        self.assertEqual(outcome["reason"], "redaction")
        self.assertEqual(outcome["posted"], [])
        self.assertEqual([c for c in self._calls() if c[:2] == ["issue", "comment"]], [])

    def test_the_switch_posts_on_completion_and_off_stays_quiet(self):
        js = (BIN.parent / "bin" / "handsoff_supervisor.py").read_text()
        self.assertIn('lib.feature_enabled(cfg, "report_posting")', js)
        self.assertIn("_post_report(root, cfg, by=", js)
        self.assertFalse(lib.FEATURES["report_posting"][0], "off by default: nothing leaves the machine")


if __name__ == "__main__":
    unittest.main()
