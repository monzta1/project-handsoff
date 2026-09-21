"""Lane D (#194): the board says who it is waiting on. host_wait_view names
the host family, the pending action and the silence when the ball is with
the host; nothing when a session is live, the Pilot's turn is pending, the
run is closed, or the ledger is fresh. The briefing and the Fleet card
carry it."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class HostWaitTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        r = run(["init", "Lane D host wait", "--by", "codex-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.cfg = lib.load_config(self.tmp)

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def _silent(self, minutes=30):
        """A ledger whose newest write is `minutes` old: rewrite updated_at and
        the last event's at, as the stall watchdog reads them."""
        status = self.read_status()
        then = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        status["updated_at"] = then
        lib.atomic_write_json(lib.status_path(self.tmp, self.cfg), status)
        events = self._events()
        for e in events:
            e["at"] = then
        return status, events

    def test_an_unconsumed_authorization_and_a_silent_host_name_the_host_and_the_attempt(self):
        status, events = self._silent(30)
        status["design_review_authorization"] = {"attempt_permitted": 3, "by": "Mission Control Pilot",
                                                 "at": status["updated_at"], "consumed_at": None,
                                                 "launch_session_id": None, "note": "x"}
        now = datetime.now(timezone.utc)
        view = lib.host_wait_view(status, events, self.cfg, now=now)
        self.assertIsNotNone(view)
        self.assertEqual((view["family"], view["actor"]), ("codex", "codex-implementer"))
        self.assertEqual(view["action"], "launch design-review attempt 3")
        self.assertEqual(view["launch_role"], "reviewer")
        self.assertGreaterEqual(view["silent_seconds"], 29 * 60)
        self.assertEqual(view["since"], status["updated_at"])
        # the briefing reads the line and points at the button
        briefing = dashboard._supervisor_briefing(status, [], [], [], "no update in 30 minutes", None, None,
                                                  {"required": False, "message": None}, view)
        self.assertEqual(briefing["label"], "Waiting on the host")
        self.assertEqual(briefing["tone"], "warning")
        self.assertTrue(briefing["headline"].startswith("Waiting on the host (codex) since "), briefing["headline"])
        self.assertIn("launch design-review attempt 3 (30 min)", briefing["headline"])
        self.assertIn("written nothing to the ledger for 30 min", briefing["summary"])

    def test_the_snapshot_and_the_fleet_card_carry_it_and_it_clears(self):
        # make the persisted run silent with an unconsumed authorization
        status = self.read_status()
        then = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        status["design_review_authorization"] = {"attempt_permitted": 2, "by": "Mission Control Pilot",
                                                 "at": then, "consumed_at": None, "launch_session_id": None, "note": "x"}
        lib.commit(self.tmp, self.cfg, status=status, event_kind="fixture", event_message="authorized")
        # the ledger is hash-chained, so silence is made by moving the clock,
        # not by editing timestamps: the view is asked as if 30 minutes passed
        real = lib.host_wait_view
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        later = mock.patch.object(lib, "host_wait_view", lambda *a, **k: real(*a, now=future, **{k2: v for k2, v in k.items() if k2 != "now"}))
        with later:
            snapshot = dashboard.build_snapshot(self.tmp)
        self.assertIsNotNone(snapshot["host_wait"])
        self.assertEqual(snapshot["host_wait"]["family"], "codex")
        self.assertEqual(snapshot["supervisor"]["label"], "Waiting on the host")
        self.assertIn("LAUNCH ROLE", snapshot["supervisor"]["next_action"])
        fleet.register_project(self.tmp, self.registry)
        with later:
            card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        self.assertEqual(card["host_wait"]["family"], "codex")
        self.assertEqual(card["host_wait"]["action"], "launch design-review attempt 2")
        # consuming the authorization: the action falls back to next_action; a live session clears it
        status = self.read_status()
        status["design_review_authorization"]["consumed_at"] = then
        view = lib.host_wait_view(status, self._events(), self.cfg, now=future)
        self.assertEqual(view["action"], status["next_action"])
        self.assertIsNone(view["launch_role"])
        status["agent_sessions"] = {"hs-" + "2" * 32: {"session_id": "hs-" + "2" * 32, "role": "reviewer", "state": "running"}}
        self.assertIsNone(lib.host_wait_view(status, self._events(), self.cfg, now=future))
        # a closed run and a fresh ledger show nothing
        status.pop("agent_sessions")
        status["run_closed"] = {"by": "x", "at": then, "reason": "r"}
        self.assertIsNone(lib.host_wait_view(status, self._events(), self.cfg, now=future))
        self.assertIsNone(lib.host_wait_view(self.read_status(), self._events(), self.cfg), "within the threshold nobody is waiting")
        self.assertIsNone(dashboard.build_snapshot(self.tmp)["host_wait"])

    def test_pilot_input_or_a_fresh_heartbeat_do_not_make_it_the_hosts_turn(self):
        status, events = self._silent(30)
        # F1.1: a decision card on the page means the Pilot's turn, not the host's
        self.assertIsNone(lib.host_wait_view(status, events, self.cfg, pilot_input_required=True))
        status_paused = {**status, "human_pause": {"by": "moncy", "at": status["updated_at"], "reason": "lunch"}}
        self.assertIsNone(lib.host_wait_view(status_paused, events, self.cfg))
        # F1.2: ledger silence is the ledger's own timestamps; a fresh heartbeat
        # or fresh managed-session output is not the host writing
        fresh = datetime.now(timezone.utc).isoformat()
        with_heartbeat = {**status, "last_heartbeat_at": fresh, "last_heartbeat_owner": "hs-" + "3" * 32}
        view = lib.host_wait_view(with_heartbeat, events, self.cfg)
        self.assertIsNotNone(view, "the heartbeat is not a ledger write")
        self.assertGreaterEqual(view["silent_seconds"], 29 * 60)
        # and a fresh event on the ledger is the host acting
        recent = events + [{"kind": "criteria_transaction_applied", "at": fresh, "by": "codex-implementer"}]
        self.assertIsNone(lib.host_wait_view(status, recent, self.cfg))
        # the threshold is the configured stall_minutes
        self.assertIsNone(lib.host_wait_view(status, events, {**self.cfg, "stall_minutes": 60}))

    def test_the_line_never_blames_a_host_it_cannot_name(self):
        r = run(["init", "no by"], cwd=self.tmp)
        self.assertIn("HANDSOFF_INIT_SKIPPED", r.stdout)  # the fixture run exists; the rule is on the view
        status, events = self._silent(20)
        for e in events:
            e.pop("by", None)
        status["implemented_by"] = None
        view = lib.host_wait_view(status, events, self.cfg)
        self.assertEqual(view["family"], "unknown")
        self.assertIsNone(view["actor"])
        briefing = dashboard._supervisor_briefing(status, [], [], [], "stalled", None, None,
                                                  {"required": False, "message": None}, view)
        self.assertIn("Waiting on the host (unknown)", briefing["headline"])


if __name__ == "__main__":
    unittest.main()
