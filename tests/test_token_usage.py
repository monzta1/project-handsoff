"""#168: what a ticket cost. Usage comes from the adapter's own printed
usage, watched per streamed line, recorded on the session and its terminal
event, summed by role and phase, and shown per closed ticket. Nothing is
ever estimated."""
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.test_session_artifacts import APPROVED, _FakeProcess

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class UsageWatcherTests(unittest.TestCase):
    def test_codex_tokens_used_on_two_lines_or_one(self):
        w = lib.UsageWatcher("codex")
        for line in ["codex", "some output", "tokens used", "59,142"]:
            w.feed(line)
        self.assertEqual(w.result(), {"tokens_in": None, "tokens_out": None, "tokens_total": 59142, "source": "adapter"})
        w = lib.UsageWatcher("codex")
        w.feed("tokens used: 1,234")
        self.assertEqual(w.result()["tokens_total"], 1234)
        # the last usage wins; a 'tokens used' not followed by a number is ignored
        w.feed("tokens used")
        w.feed("not a number")
        self.assertEqual(w.result()["tokens_total"], 1234)
        w.feed("tokens used")
        w.feed("2,000")
        self.assertEqual(w.result()["tokens_total"], 2000)

    def test_claude_stream_json_usage_in_and_out(self):
        w = lib.UsageWatcher("claude")
        w.feed(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 20}}}))
        w.feed(json.dumps({"type": "result", "usage": {"input_tokens": 1500, "output_tokens": 300, "cache_read_input_tokens": 9}}))
        self.assertEqual(w.result(), {"tokens_in": 1500, "tokens_out": 300, "tokens_total": 1800, "source": "adapter"})
        w.feed("{not json")
        self.assertEqual(w.result()["tokens_total"], 1800)

    def test_nothing_printed_is_not_reported_and_the_switch_off_is_disabled(self):
        w = lib.UsageWatcher("codex")
        w.feed("just text")
        self.assertEqual(w.result(), {"tokens_in": None, "tokens_out": None, "tokens_total": None, "source": "not reported"})
        w.feed("tokens used")
        w.feed("77")
        self.assertEqual(w.result(enabled=False)["source"], "disabled")
        self.assertIsNone(w.result(enabled=False)["tokens_total"])

    def test_validate_usage_refuses_shapes_that_are_not_the_record(self):
        for bad in ({"tokens_total": 1}, {"tokens_in": None, "tokens_out": None, "tokens_total": -1, "source": "adapter"},
                    {"tokens_in": None, "tokens_out": None, "tokens_total": None, "source": "guessed"}, "x"):
            with self.assertRaises(lib.HandsoffError):
                lib.validate_usage(bad)


class UsageOnSessionTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Usage fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _spec(self):
        return runtime.LaunchSpec("reviewer", "codex", "default", ("/bin/codex", "exec", "-"), str(self.tmp),
                                  "bounded prompt", token_budget=40_000, project_root=str(self.tmp.resolve()))

    def _session(self):
        status = self.read_status()
        sid = status["current_agent_sessions"]["reviewer"]
        return sid, status["agent_sessions"][sid]

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def test_usage_after_more_than_a_tail_of_output_is_still_recorded_on_session_and_event(self):
        filler = "".join(f"line {i} " + "x" * 80 + "\n" for i in range(300))  # far more than 8192 chars
        output = APPROVED + "tokens used\n44,988\n" + filler
        with mock.patch.object(broker, "dispatch_reviewer_result", return_value=0):
            runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(output)), beacon_interval=0.01)
        sid, session = self._session()
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["usage"], {"tokens_in": None, "tokens_out": None, "tokens_total": 44988, "source": "adapter"})
        terminal = [e for e in self._events() if e["kind"] == "agent_session_completed" and e["session_id"] == sid][-1]
        self.assertEqual(terminal["usage"]["tokens_total"], 44988)
        metrics = lib.build_run_metrics(self.read_status(), self._events(), [])
        row = next(s for s in metrics["sessions"] if s["session_id"] == sid)
        self.assertEqual((row["total_tokens"], row["usage_source"]), (44988, "adapter"))

    def test_usage_on_stderr_and_on_a_failed_session_and_nothing_printed(self):
        class Stderr(_FakeProcess):
            def __init__(self, stdout="", **kw):
                super().__init__(stdout, **kw)
                self.stderr = io.StringIO("tokens used\n1,500\n")
        with mock.patch.object(broker, "dispatch_reviewer_result", side_effect=lib.HandsoffError("gate says no")):
            with self.assertRaises(runtime.AgentLaunchError):
                runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=Stderr(APPROVED)), beacon_interval=0.01)
        sid, session = self._session()
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["usage"]["tokens_total"], 1500)
        totals = lib.usage_totals(self.read_status())
        self.assertEqual(totals["tokens_total"], 1500)
        self.assertEqual(totals["by_role"], {"reviewer": 1500})
        self.assertEqual(totals["by_phase"], {"5": 1500})
        self.assertEqual((totals["sessions_reported"], totals["sessions_not_reported"]), (1, 0))
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(json.loads(r.stdout)["usage"]["tokens_total"], 1500)

    def test_the_switch_off_records_disabled(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + "\n[features]\ntoken_accounting = false\n")
        with mock.patch.object(broker, "dispatch_reviewer_result", return_value=0):
            runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(APPROVED + "tokens used\n9\n")),
                                   beacon_interval=0.01)
        _, session = self._session()
        self.assertEqual(session["usage"], {"tokens_in": None, "tokens_out": None, "tokens_total": None, "source": "disabled"})
        self.assertEqual(lib.usage_totals(self.read_status())["sessions_not_reported"], 1)


class TokensPerTicketTests(unittest.TestCase):
    def test_archived_usage_is_attributed_to_each_closed_issue_item_and_never_zero(self):
        import tempfile
        base = Path(tempfile.mkdtemp(prefix="handsoff-usage-"))
        try:
            def archive(name, root, items, usage, kind="product"):
                (base / f"{name}.json").write_text(json.dumps({
                    "root": root, "repo": "p", "run_kind": kind, "completed_at": "2026-09-20T00:00:00+00:00",
                    "acceptance": {"work_items": [{"id": f"issue-{n}", "kind": "issue", "number": n} for n in items]},
                    "usage": usage}))
            archive("a", "/p/x", [165, 166], {"tokens_total": 9000, "sessions_reported": 3, "sessions_not_reported": 0})
            archive("b", "/p/x", [172], {"tokens_total": 0, "sessions_reported": 0, "sessions_not_reported": 2})
            archive("c", "/p/x", [1], {"tokens_total": 5, "sessions_reported": 1, "sessions_not_reported": 0}, kind="test")
            per = lib.tokens_per_ticket(base)
            tickets = per["/p/x"]["tickets"]
            self.assertEqual(tickets["165"]["tokens_total"], 9000)
            self.assertEqual(tickets["165"]["shared_with"], 1)
            self.assertEqual(tickets["166"]["tokens_total"], 9000)
            self.assertEqual((tickets["172"]["tokens_total"], tickets["172"]["reported"]), (None, False))
            self.assertNotIn("1", tickets, "test runs are never mined")
            js = (BIN.parent / "fleet" / "metrics.js").read_text()
            self.assertIn('"TOKENS"', js)
            self.assertIn("not reported", js)
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
