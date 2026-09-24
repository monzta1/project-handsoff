"""#309: a running session names its model, not only a dead one.

`UsageWatcher` parses the provider's model from the adapter banner within
the first second. Until now the only writer was `end_session`, which runs on
a terminal transition, so a session reported no model for its whole life and
named it only once nothing could be done about it.

The write is deliberately narrow, because a session record is evidence: it
touches `reported_model` and nothing else, refuses a session that has already
ended, and leaves a disagreement between two announcements to
`_adaptive_model_reconciliation` rather than resolving it silently.
"""
import copy
import shutil
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

#: The banner Codex prints, verbatim from session
#: hs-d26f19a8a3754328ae26f3a156740692 on 2026-09-23.
BANNER = [
    "2026-09-24T02:57:26Z ERROR codex_models_manager::manager: failed to refresh",
    "OpenAI Codex v0.153.4",
    "--------",
    "workdir: /private/var/folders/q4/T/handsoff-reviewer-t8oyewc5",
    "model: gpt-5.6-luna",
]


class TheObservationBoundaryIsNamed(unittest.TestCase):
    """REQ-003: the first feed that sets reported_model, before the next
    line is fed. Not 'eventually', not 'at the end'."""

    def test_the_model_is_known_after_exactly_the_banner_lines(self):
        watcher = lib.UsageWatcher("codex")
        for line in BANNER:
            watcher.feed(line)
        self.assertEqual(watcher.reported_model, "gpt-5.6-luna")

    def test_it_is_not_known_one_line_earlier(self):
        """Pins the boundary: the line before the model line must not
        already have it, or the assertion above proves nothing."""
        watcher = lib.UsageWatcher("codex")
        for line in BANNER[:-1]:
            watcher.feed(line)
        self.assertIsNone(watcher.reported_model)

    def test_nothing_after_the_banner_is_needed(self):
        watcher = lib.UsageWatcher("codex")
        for line in BANNER:
            watcher.feed(line)
        before = watcher.reported_model
        for line in ["user", "Please review the packet.", "tokens used", "18,107"]:
            watcher.feed(line)
        self.assertEqual(watcher.reported_model, before)


class ARunningSessionNamesItsModel(HandsoffTestCase):
    """REQ-003, against a real session record."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        self.init("Live reported model")

    def _running_session(self):
        session = lib.create_agent_session(
            self.tmp, role="reviewer", adapter="codex", requested_model="default",
            actor="codex-reviewer", resolution_source="configured")
        return session["session_id"]

    def test_the_model_is_recorded_while_the_session_is_still_running(self):
        session_id = self._running_session()
        result = lib.record_reported_model(self.tmp, session_id, "gpt-5.6-luna")
        self.assertTrue(result["written"])
        status = self.read_status()
        session = status["agent_sessions"][session_id]
        self.assertEqual(session["reported_model"], "gpt-5.6-luna")
        self.assertIn(session["state"], lib.AGENT_SESSION_LIVE_STATES,
                      "the point of the fix: still running, and already named")

    def test_before_the_fix_shape_a_running_session_named_nothing(self):
        """The regression: a fresh session has no model until something
        writes one, which used only to happen at the end."""
        session_id = self._running_session()
        self.assertIsNone(self.read_status()["agent_sessions"][session_id]["reported_model"])

    def test_an_unknown_session_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "unknown"):
            lib.record_reported_model(self.tmp, "hs-" + "0" * 32, "gpt-5.6-luna")

    def test_a_malformed_session_id_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "session id is invalid"):
            lib.record_reported_model(self.tmp, "not-a-session", "gpt-5.6-luna")

    def test_a_model_that_is_not_a_model_is_refused(self):
        session_id = self._running_session()
        with self.assertRaises(lib.HandsoffError):
            lib.record_reported_model(self.tmp, session_id, "-rf")


class TheWriteIsBounded(HandsoffTestCase):
    """REQ-004. A session record is evidence; this writer may not damage it."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        self.init("Bounded write")
        self.session_id = lib.create_agent_session(
            self.tmp, role="reviewer", adapter="codex", requested_model="default",
            actor="codex-reviewer", resolution_source="configured")["session_id"]

    def _session(self):
        return self.read_status()["agent_sessions"][self.session_id]

    def test_it_changes_reported_model_and_nothing_else(self):
        before = copy.deepcopy(self._session())
        lib.record_reported_model(self.tmp, self.session_id, "gpt-5.6-luna")
        after = copy.deepcopy(self._session())
        self.assertEqual(after.pop("reported_model"), "gpt-5.6-luna")
        before.pop("reported_model")
        self.assertEqual(after, before, "the write touched something other than the model")

    def test_a_repeated_announcement_performs_no_second_write(self):
        lib.record_reported_model(self.tmp, self.session_id, "gpt-5.6-luna")
        again = lib.record_reported_model(self.tmp, self.session_id, "gpt-5.6-luna")
        self.assertFalse(again["written"])
        self.assertEqual(again["reported_model"], "gpt-5.6-luna")

    def test_a_later_different_model_does_not_overwrite_the_first(self):
        """The disagreement is for reconciliation to judge, not for this
        writer to resolve by silently preferring the newer value."""
        lib.record_reported_model(self.tmp, self.session_id, "gpt-5.6-luna")
        result = lib.record_reported_model(self.tmp, self.session_id, "something-else")
        self.assertFalse(result["written"])
        self.assertEqual(self._session()["reported_model"], "gpt-5.6-luna")

    def test_a_terminal_session_refuses_and_is_unchanged(self):
        lib.transition_agent_session(self.tmp, self.session_id, "running")
        lib.transition_agent_session(self.tmp, self.session_id, "completed", exit_code=0)
        before = copy.deepcopy(self._session())
        with self.assertRaisesRegex(lib.HandsoffError, "already ended"):
            lib.record_reported_model(self.tmp, self.session_id, "gpt-5.6-luna")
        self.assertEqual(self._session(), before, "a refused write still changed the record")

    def test_every_terminal_state_refuses(self):
        """One role holds one live session at a time, so each per-state
        session is retired before the next is created."""
        lib.transition_agent_session(self.tmp, self.session_id, "running")
        lib.transition_agent_session(self.tmp, self.session_id, "cancelled")
        checked = []
        for state in sorted(lib.AGENT_SESSION_TERMINAL_STATES):
            session_id = lib.create_agent_session(
                self.tmp, role="reviewer", adapter="codex", requested_model="default",
                actor="codex-reviewer", resolution_source="configured")["session_id"]
            reached = False
            for path in (("running", state), (state,)):
                try:
                    for step in path:
                        kwargs = {"exit_code": 0} if step in {"completed", "failed", "timed_out"} else {}
                        lib.transition_agent_session(self.tmp, session_id, step, **kwargs)
                    reached = True
                    break
                except lib.HandsoffError:
                    continue
            if not reached:
                # Free the role and skip a state this fixture cannot reach.
                try:
                    lib.transition_agent_session(self.tmp, session_id, "cancelled")
                except lib.HandsoffError:
                    pass
                continue
            with self.assertRaises(lib.HandsoffError, msg=state):
                lib.record_reported_model(self.tmp, session_id, "gpt-5.6-luna")
            checked.append(state)
        self.assertGreaterEqual(len(checked), 3, f"only reached {checked}")

    def test_a_session_that_announced_nothing_records_null(self):
        """Distinguishable from a model not yet observed: both are null, and
        neither is ever invented."""
        self.assertIsNone(self._session()["reported_model"])
        lib.transition_agent_session(self.tmp, self.session_id, "running")
        lib.transition_agent_session(self.tmp, self.session_id, "completed", exit_code=0)
        self.assertIsNone(self._session()["reported_model"])


class ReconciliationJudgesTheMismatch(unittest.TestCase):
    """REQ-004: the writer defers, `_adaptive_model_reconciliation` decides."""

    def session(self, reported):
        return {"adapter": "codex", "reported_model": reported,
                "adaptive_routing": {"model": "gpt-5.6-luna", "tier": "PREMIUM",
                                     "profile": {"adapter": "codex", "model": "gpt-5.6-luna"}}}

    def test_a_match_reconciles(self):
        self.assertEqual(
            lib._adaptive_model_reconciliation(self.session("gpt-5.6-luna"))["consistency"],
            "matched")

    def test_an_early_write_gives_reconciliation_something_to_judge(self):
        """Before #309 a running session was always pending_verification,
        because nothing had written a model yet."""
        self.assertEqual(
            lib._adaptive_model_reconciliation(self.session(None))["consistency"],
            "pending_verification")
        self.assertNotEqual(
            lib._adaptive_model_reconciliation(self.session("gpt-5.6-luna"))["consistency"],
            "pending_verification")


if __name__ == "__main__":
    unittest.main()
