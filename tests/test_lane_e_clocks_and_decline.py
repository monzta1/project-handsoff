"""Lane E (#193, #177): clocks that know the Mac slept, and a decline the
runner can carry and the reviewer resolves."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402

NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
LOG = """2026-09-21 03:20:00 -0400 Sleep               \tEntering Sleep state due to 'Idle Sleep'
2026-09-21 06:45:53 -0400 DarkWake            \tDarkWake from Deep Idle [CDNP] : due to maintenance
2026-09-21 06:46:38 -0400 Sleep               \tEntering Sleep state due to 'Maintenance Sleep'
2026-09-21 10:25:00 -0400 Wake                \tWake from Deep Idle [CDNVA] : due to keyboard
"""


class SleepLogTests(unittest.TestCase):
    def test_the_parser_reads_offsets_keeps_a_darkwake_asleep_and_closes_on_wake(self):
        intervals = lib.parse_sleep_log(LOG, now=NOW)
        self.assertEqual([(a.isoformat(), b.isoformat()) for a, b in intervals],
                         [("2026-09-21T07:20:00+00:00", "2026-09-21T14:25:00+00:00")])
        # a DST fall-back: the offset changes between lines and nothing is ambiguous
        dst = ("2026-11-01 01:30:00 -0400 Sleep\tEntering Sleep state\n"
               "2026-11-01 01:15:00 -0500 Wake\tWake from Normal Sleep\n")
        (slept, woke), = lib.parse_sleep_log(dst, now=NOW)
        self.assertEqual((slept.isoformat(), woke.isoformat()), ("2026-11-01T05:30:00+00:00", "2026-11-01T06:15:00+00:00"))
        # an open interval closes at now; a wake before any sleep is ignored
        open_log = "2026-09-21 11:00:00 -0400 Wake\tx\n2026-09-21 11:30:00 -0400 Sleep\tEntering Sleep state\n"
        (slept, woke), = lib.parse_sleep_log(open_log, now=NOW)
        self.assertEqual((slept.isoformat(), woke), ("2026-09-21T15:30:00+00:00", NOW))
        self.assertEqual(lib.parse_sleep_log("garbage\n2026-09-21 25:00:00 -0400 Sleep\tx\n", now=NOW), [])

    def test_awake_and_asleep_seconds_never_go_negative_or_double_count(self):
        intervals = lib.parse_sleep_log(LOG, now=NOW)
        start, end = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc), datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(lib.awake_seconds(start, end, intervals), 3300.0)
        self.assertEqual(lib.asleep_seconds(start, end, intervals), 25500.0)
        inside = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(lib.awake_seconds(*inside, intervals), 0.0)
        self.assertEqual(lib.asleep_seconds(*inside, intervals), 3600.0)
        self.assertEqual(lib.awake_seconds(end, start, intervals), 0.0)
        self.assertEqual(lib.asleep_seconds(end, start, intervals), 0.0)
        self.assertIsNone(lib.awake_seconds(None, end, intervals))
        # overlapping intervals merge, so a span is never subtracted twice
        doubled = intervals + intervals
        self.assertEqual(lib.asleep_seconds(start, end, lib.parse_sleep_log(LOG + LOG, now=NOW)), 25500.0)
        self.assertEqual(lib.awake_seconds(start, end, []), 28800.0)

    def test_without_pmset_every_number_is_the_wall_clock_and_a_request_never_waits_for_the_log(self):
        lib._SLEEP_LOG_CACHE["at"] = None; lib._SLEEP_LOG_CACHE["intervals"] = []; lib._SLEEP_LOG_CACHE["thread"] = None
        with mock.patch.object(lib.shutil, "which", return_value=None):
            self.assertEqual(lib.machine_sleep_intervals(now=NOW, wait=True), [])
        self.assertEqual(lib.machine_sleep_intervals(now=NOW, log_reader=lambda: None), [])
        self.assertEqual(len(lib.machine_sleep_intervals(now=NOW, log_reader=lambda: LOG)), 1)
        # the log takes seconds on a real Mac (33,000 lines): a request gets
        # the cache (empty before the first read lands) and never blocks
        lib._SLEEP_LOG_CACHE["at"] = None; lib._SLEEP_LOG_CACHE["intervals"] = []; lib._SLEEP_LOG_CACHE["thread"] = None
        import time
        def slow():
            time.sleep(0.5)
            return LOG
        with mock.patch.object(lib, "_read_pmset_log", side_effect=slow):
            started = time.time()
            first = lib.machine_sleep_intervals(now=NOW)
            self.assertLess(time.time() - started, 0.2)
            self.assertEqual(first, [])
            self.assertEqual(len(lib.machine_sleep_intervals(now=NOW, wait=True)), 1)
        lib._SLEEP_LOG_CACHE["at"] = None; lib._SLEEP_LOG_CACHE["intervals"] = []; lib._SLEEP_LOG_CACHE["thread"] = None


class SleepAwareBoardTests(HandsoffTestCase):
    def _reset(self):
        lib._SLEEP_LOG_CACHE["at"] = None
        lib._SLEEP_LOG_CACHE["intervals"] = []
        lib._SLEEP_LOG_CACHE["thread"] = None

    def _prime(self):
        """The log is read on a background thread and never on a request;
        the test waits for that read so the snapshot sees the intervals."""
        self._reset()
        lib.machine_sleep_intervals(wait=True)

    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init("Lane E clocks")
        self.cfg = lib.load_config(self.tmp)

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def test_a_phase_that_spans_a_sleep_reads_awake_time_with_the_sleep_beside_it(self):
        # Phase 2 from 03:00Z to 11:00Z with the machine asleep 03:20Z to 10:25Z
        sleep = lib.parse_sleep_log(
            "2026-09-21 03:20:00 +0000 Sleep\tx\n2026-09-21 06:45:53 +0000 DarkWake\tx\n2026-09-21 10:25:00 +0000 Wake\tx\n",
            now=datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc))
        events = [{"kind": "initialized", "at": "2026-09-21T02:00:00+00:00", "phase_number": 1},
                  {"kind": "phase_advanced", "at": "2026-09-21T03:00:00+00:00", "phase_number": 2}]
        status = {**self.read_status(), "phase_number": 2, "phase": lib.PHASES[2], "updated_at": "2026-09-21T03:00:00+00:00",
                  "status": "in_progress"}
        now = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)
        metrics = lib.build_run_metrics(status, events, [], now=now, sleep=sleep)
        self.assertEqual(metrics["phase_seconds"]["2"], 3300.0, "55 minutes awake")
        self.assertEqual(metrics["phase_asleep_seconds"]["2"], 25500.0, "7 h 05 m asleep")
        self.assertEqual(metrics["phase_seconds"]["1"], 3600.0)
        self.assertEqual(metrics["elapsed_seconds"], 3600.0 + 3300.0)
        self.assertEqual(metrics["asleep_seconds"], 25500.0)
        # the same numbers without sleep are the wall clock
        plain = lib.build_run_metrics(status, events, [], now=now, sleep=[])
        self.assertEqual((plain["phase_seconds"]["2"], plain["asleep_seconds"]), (28800.0, 0.0))

    def test_the_snapshot_the_card_and_the_stall_use_the_machines_sleep(self):
        # the machine slept from 30 minutes ago until 2 minutes ago; the run
        # wrote 31 minutes ago, so awake silence is about 3 minutes: no stall
        now = datetime.now(timezone.utc)
        log = (f"{(now - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')} +0000 Sleep\tx\n"
               f"{(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')} +0000 Wake\tx\n")
        status = self.read_status()
        status["updated_at"] = (now - timedelta(minutes=31)).isoformat()
        lib.commit(self.tmp, self.cfg, status=status, event_kind="fixture", event_message="then silence")
        with mock.patch.object(lib, "_read_pmset_log", return_value=log):
            self._prime()
            snapshot = dashboard.build_snapshot(self.tmp)
            fleet.register_project(self.tmp, self.registry)
            card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        self._reset()
        # the run itself started seconds ago (the ledger is hash-chained, so
        # its events are fresh); the silence since updated_at is what slept
        activity = snapshot["activity"]
        self.assertGreater(activity["asleep_seconds"], 27 * 60)
        self.assertLess(activity["seconds_since_activity"], 5 * 60, "31 minutes of silence, 28 of them asleep")
        self.assertIsNone(activity["stall_warning"], "a closed lid is not a stall")
        self.assertIn("asleep_seconds", card, "the card carries the run's sleep")
        self.assertIn("phase_asleep_seconds", card)
        # without the sleep the same silence is a stall
        with mock.patch.object(lib, "_read_pmset_log", return_value=""):
            self._prime()
            plain = dashboard.build_snapshot(self.tmp)
        self._reset()
        self.assertIsNotNone(plain["activity"]["stall_warning"], "without the sleep the same silence is a stall")
        # the host-wait line (#194) reads awake silence too: 3 minutes is nobody waiting
        with mock.patch.object(lib, "_read_pmset_log", return_value=log):
            lib._SLEEP_LOG_CACHE["at"] = None
            self.assertIsNone(lib.host_wait_view(self.read_status(), self._events(), self.cfg))
        # and with a ledger whose newest write is 31 minutes old (the events
        # are hash-chained, so this is a pure call with old timestamps)
        then = (now - timedelta(minutes=31)).isoformat()
        old_status = {**self.read_status(), "updated_at": then}
        old_events = [{"kind": "initialized", "at": then, "by": "claude-host"}]
        with mock.patch.object(lib, "_read_pmset_log", return_value=log):
            self._prime()
            self.assertIsNone(lib.host_wait_view(old_status, old_events, self.cfg, now=now), "28 of the 31 minutes were asleep")
        with mock.patch.object(lib, "_read_pmset_log", return_value=""):
            self._prime()
            waiting = lib.host_wait_view(old_status, old_events, self.cfg, now=now)
        self._reset()
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting["asleep_seconds"], 0.0)
        self.assertGreaterEqual(waiting["silent_seconds"], 30 * 60)


class RunnerDeclineTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Lane E decline dispatch")
        r = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)

    def _events(self, kind=None):
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        return [e for e in events if kind is None or e["kind"] == kind]

    def _spec(self, code):
        return agent.LaunchSpec("architect", "codex", "default", (sys.executable, "-c", code), str(self.tmp),
                                "private task text", "configured")

    def test_a_managed_architects_decline_line_is_recorded_pending(self):
        line = ('HANDSOFF_DESIGN_DECLINE: {"reason": "the harm is already prevented", '
                '"evidence": ["compute_errors refuses it", "lane A ledger"], "alternative": null}')
        rc = agent.execute_launch(self._spec(f"print({line!r})"), actor="codex-architect",
                                  session_id_factory=lambda: "hs-" + "7" * 32, beacon_interval=0.05)
        self.assertEqual(rc, 0)
        status = self.read_status()
        self.assertEqual(status["design_declined"]["decision"], "pending")
        self.assertEqual(status["design_declined"]["by"], "codex-architect")
        self.assertEqual(status["design_declined"]["evidence"], ["compute_errors refuses it", "lane A ledger"])
        self.assertIsNone(status.get("run_closed"))
        self.assertEqual(status["agent_sessions"]["hs-" + "7" * 32]["state"], "completed")
        self.assertEqual(len(self._events("design_declined")), 1)

    def test_a_proposal_and_a_decline_in_one_turn_is_refused(self):
        both = ('print("HANDSOFF_DESIGN_DECLINE: {\\"reason\\": \\"x\\"}"); '
                'print("HANDSOFF_DESIGN_PROPOSAL: {\\"summary\\": \\"s\\", \\"approach\\": [\\"a\\"], \\"tradeoffs\\": [], '
                '\\"decisions\\": [\\"d\\"], \\"constraints\\": [], \\"verification\\": [\\"v\\"]}")')
        with self.assertRaisesRegex(lib.HandsoffError, "more than one structured outcome"):
            agent.execute_launch(self._spec(both), actor="codex-architect",
                                 session_id_factory=lambda: "hs-" + "8" * 32, beacon_interval=0.05)
        self.assertNotIn("design_declined", self.read_status())
        self.assertEqual(self.read_status()["agent_sessions"]["hs-" + "8" * 32]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
