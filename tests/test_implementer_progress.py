"""#215: the Implementer reports per-criterion progress through the ledger.
A fake Implementer child prints HANDSOFF_PROGRESS lines and exits on the
budget; the session carries the progress, the failure record the summary,
the snapshot the card, the relaunch input the done list."""
import io
import json
import shutil
import sys
import unittest
from unittest import mock

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _InputPipe:
    def write(self, _):
        return None

    def close(self):
        return None


class _FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, pid=4243):
        self.pid = pid
        self.returncode = returncode
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


PROGRESS = ('HANDSOFF_PROGRESS: {"criterion": "REQ-001", "state": "done", "test": "python3 -m unittest tests.test_a -v", "note": ""}\n'
            'HANDSOFF_PROGRESS: {"criterion": "REQ-002", "state": "done", "test": "python3 -m unittest tests.test_b -v", "note": "wrote the fixture too"}\n'
            'HANDSOFF_PROGRESS: {"criterion": "REQ-002", "state": "partial", "note": "not a valid line: no test field is fine, but state after done"}\n'
            'HANDSOFF_PROGRESS: not json at all\n'
            'HANDSOFF_PROGRESS: {"criterion": "../etc", "state": "done"}\n')


class ImplementerProgressTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Progress fixture")
        self.set_criterion_state("passing", resolved=True)
        # three automated criteria: the fixture's REQ-001 (its test is `true`) plus two
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('commands = ["true"]', 'commands = ["true", "python3 -m unittest tests.test_b -v", "python3 -m unittest tests.test_c -v"]', 1))
        tx = {"operations": [
            {"op": "add", "criterion": {"id": "REQ-002", "type": "supporting", "requirement": "two", "verification": "automated", "tests": ["python3 -m unittest tests.test_b -v"]}},
            {"op": "add", "criterion": {"id": "REQ-003", "type": "supporting", "requirement": "three", "verification": "automated", "tests": ["python3 -m unittest tests.test_c -v"]}},
        ]}
        path = self.tmp / "tx.json"
        path.write_text(json.dumps(tx))
        r = run(["criteria-apply", "--file", str(path), "--by", "host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reached = self.advance_to(4, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _spec(self):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"), str(self.tmp),
                                  "bounded prompt", token_budget=40_000, project_root=str(self.tmp.resolve()))

    def _session(self):
        status = self.read_status()
        sid = status["current_agent_sessions"]["implementer"]
        return sid, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid), status

    def test_two_done_then_the_budget_leaves_the_account_on_the_ledger(self):
        """[#215] acceptance: progress with three entries, the failure names the untouched one."""
        factory = mock.Mock(return_value=_FakeProcess(PROGRESS, stderr="shared rollout token budget exhausted\n", returncode=1))
        with self.assertRaises(runtime.AgentLaunchError):
            runtime.execute_launch(self._spec(), popen_factory=factory, beacon_interval=0.01)
        sid, session, failure, status = self._session()
        self.assertEqual(session["state"], "failed")
        self.assertEqual(failure["category"], "token_budget_exhaustion")
        self.assertEqual([(p["criterion"], p["state"]) for p in session["progress"]],
                         [("REQ-001", "done"), ("REQ-002", "done"), ("REQ-002", "partial")])
        self.assertEqual(session["progress"][0]["test"], "python3 -m unittest tests.test_a -v")  # what the child said, verbatim
        self.assertTrue(all("at" in p for p in session["progress"]))
        # the summary: last state per criterion wins; the never-reported one is untouched
        self.assertEqual(failure["progress_summary"], {"done": ["REQ-001"], "partial": ["REQ-002"], "untouched": ["REQ-003"]})
        # the two bad lines were warnings, never failures
        warnings = lib.read_operations(self.tmp)["sessions"][sid]["protocol_warnings"]
        self.assertEqual(warnings, 2)
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([e["criterion"] for e in events if e["kind"] == "implementer_progress"], ["REQ-001", "REQ-002", "REQ-002"])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_the_recovery_card_lists_the_progress_and_a_relaunch_receives_the_done_list(self):
        """[#215] acceptance: the snapshot's recovery card; the relaunch task text."""
        factory = mock.Mock(return_value=_FakeProcess(PROGRESS, stderr="token budget exhausted\n", returncode=1))
        with self.assertRaises(runtime.AgentLaunchError):
            runtime.execute_launch(self._spec(), popen_factory=factory, beacon_interval=0.01)
        sid = self._session()[0]
        snapshot = dashboard.build_snapshot(self.tmp)
        card = snapshot["recovery"]["implementer_progress"]
        self.assertEqual(card["session_id"], sid)
        self.assertEqual(card["summary"], {"done": ["REQ-001"], "partial": ["REQ-002"], "untouched": ["REQ-003"]})
        self.assertEqual(card["tests"], {"REQ-001": "python3 -m unittest tests.test_a -v", "REQ-002": "python3 -m unittest tests.test_b -v"})
        with mock.patch.object(runtime, "_role_prompt", return_value="ROLE PROMPT"):
            text = runtime.build_role_input(self.tmp, "implementer", "continue the lane")
        self.assertIn("# Progress so far", text)
        self.assertIn(f"session ({sid})", text)
        self.assertIn("- REQ-001: done (test: python3 -m unittest tests.test_a -v)", text)
        self.assertIn("- REQ-002: partial; check the tree before continuing", text)
        self.assertIn("- REQ-003: untouched", text)
        self.assertIn("Do not redo a done criterion", text)
        # a first launch on a run with no failed implementer carries no section
        with mock.patch.object(runtime, "_role_prompt", return_value="ROLE PROMPT"):
            self.assertNotIn("# Progress so far", runtime.build_role_input(self.tmp, "reviewer", "review"))

    def test_a_silent_implementer_leaves_every_criterion_untouched(self):
        factory = mock.Mock(return_value=_FakeProcess("", stderr="token budget exhausted\n", returncode=1))
        with self.assertRaises(runtime.AgentLaunchError):
            runtime.execute_launch(self._spec(), popen_factory=factory, beacon_interval=0.01)
        _, session, failure, _ = self._session()
        self.assertNotIn("progress", session)
        self.assertEqual(failure["progress_summary"], {"done": [], "partial": [], "untouched": ["REQ-001", "REQ-002", "REQ-003"]})
        self.assertIsNone(dashboard.build_snapshot(self.tmp)["recovery"]["implementer_progress"]["summary"].get("nope"))

    def test_the_label_the_validator_and_the_docs(self):
        """[#215] acceptance: the prompt and the lanes playbook carry the rule."""
        self.assertIsNone(lib.validate_progress_line({"criterion": "REQ-001", "state": "finished"}))
        self.assertIsNone(lib.validate_progress_line({"criterion": "REQ-001", "state": "done", "extra": 1}))
        self.assertIsNone(lib.validate_progress_line({"criterion": "REQ-001", "state": "done", "note": "x" * 201}))
        self.assertEqual(lib.validate_progress_line({"criterion": "REQ-001", "state": "done"}),
                         {"criterion": "REQ-001", "state": "done", "test": "", "note": ""})
        self.assertEqual(lib.progress_summary([{"criterion": "REQ-002", "state": "done"}],
                                              {"criteria": [{"id": "REQ-001", "verification": "automated"}, {"id": "REQ-002", "verification": "automated"}, {"id": "REQ-003", "verification": "manual"}]}),
                         {"done": ["REQ-002"], "partial": [], "untouched": ["REQ-001"]})
        prompt = (ROOT / "prompts" / "implementer.md").read_text()
        self.assertIn("HANDSOFF_PROGRESS:", prompt)
        self.assertIn('"state": "done"', prompt)
        lanes = (ROOT / "playbook" / "lanes.md").read_text()
        self.assertIn("One criterion per Implementer launch", lanes)
        self.assertIn("more than three automated criteria", lanes)
        self.assertIn("2026-09-19", lanes)


if __name__ == "__main__":
    unittest.main()
