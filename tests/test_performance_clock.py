"""#295: the active-run ceiling is a deadline, not an opportunistic check.

A run for #281, #282, #283, #286 and #287 stayed active for more than four
hours on 2026-09-23 without pausing. The machinery from #280 was correct;
nothing called it. `transition_performance` is a pure function, and its
only production callers were the supervisor CLI dispatch and an HTTP
request to an open dashboard.

The durable record from that run shows the shape exactly. Episode 1 paused
on time. Episode 2, opened by the resume, did not:

    episode-2  started_at 20:01:36Z  warning_at 21:31:38Z  paused_at null

Its 120-minute deadline was 22:01:36Z and the record's last write was
21:57:57Z, three minutes and thirty-nine seconds earlier. Closeout had
moved to git, gh and CI by then, so no supervisor command ran again and
the deadline passed unobserved.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_runtime_control as runtime_control  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

#: The real episode 2 from run-a5a4a20f097beeb60bfce8777ca1d8c9.
EPISODE_2_START = datetime(2026, 9, 23, 20, 1, 36, tzinfo=timezone.utc)
EPISODE_2_DEADLINE = EPISODE_2_START + timedelta(minutes=120)
#: The last moment anything wrote the durable record.
LAST_OBSERVATION = datetime(2026, 9, 23, 21, 57, 57, tzinfo=timezone.utc)


class TheClockIsOwnedByTheRun(HandsoffTestCase):
    """REQ-004."""

    def _history(self, started):
        return runtime_control.new_performance_history("run-test", "episode-1", now=started)

    def test_a_deadline_that_passes_with_no_command_still_pauses(self):
        """The exact shape that failed: no supervisor command runs across
        the deadline, and the clock evaluates it anyway."""
        history = self._history(EPISODE_2_START)
        # Nothing at all happens between the last observation and the
        # deadline; the next thing to run is the run's own clock.
        self.assertLess(LAST_OBSERVATION, EPISODE_2_DEADLINE)
        updated, decision = runtime_control.transition_performance(
            history, now=EPISODE_2_DEADLINE + timedelta(seconds=1))
        self.assertEqual(decision["action"], "pause_for_performance_review")
        self.assertEqual(updated["episodes"][-1]["state"], "paused_for_performance_review")
        self.assertTrue(decision["block_new_work"])

    def test_the_last_observation_before_the_deadline_does_not_pause(self):
        """Proving the gap is real: at 21:57:57Z the run was legitimately
        still inside its ceiling, which is why nothing was wrong yet."""
        history = self._history(EPISODE_2_START)
        _updated, decision = runtime_control.transition_performance(history, now=LAST_OBSERVATION)
        self.assertNotEqual(decision["action"], "pause_for_performance_review")
        self.assertFalse(decision["block_new_work"])

    def test_the_ninety_minute_warning_still_fires_first(self):
        history = self._history(EPISODE_2_START)
        _updated, decision = runtime_control.transition_performance(
            history, now=EPISODE_2_START + timedelta(minutes=90))
        self.assertEqual(decision["action"], "deadline_warning")

    def test_a_tick_interval_bounds_the_overshoot(self):
        """The clock cannot pause at the exact second, so the guarantee is
        that it pauses within one tick of the deadline."""
        self.assertLessEqual(supervisor.PERFORMANCE_TICK_SECONDS, 120)
        history = self._history(EPISODE_2_START)
        _updated, decision = runtime_control.transition_performance(
            history, now=EPISODE_2_DEADLINE + timedelta(seconds=supervisor.PERFORMANCE_TICK_SECONDS))
        self.assertEqual(decision["action"], "pause_for_performance_review")


class TheClockRunsWithoutACommand(HandsoffTestCase):
    """REQ-004, at the command surface."""

    def test_a_single_tick_evaluates_and_persists(self):
        self.init("Clock")
        view = supervisor.performance_tick(self.tmp)
        self.assertIn("state", view)
        self.assertIn(view["state"], ("active", "warning", "paused_for_performance_review"))

    def test_a_tick_at_a_past_deadline_pauses_the_run(self):
        self.init("Clock")
        view = supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=121))
        self.assertEqual(view["state"], "paused_for_performance_review")
        self.assertTrue(view["block_new_work"])

    def test_the_watch_command_is_a_reader_so_a_pause_cannot_silence_it(self):
        self.assertIn("performance-watch", supervisor.PERFORMANCE_READ_ONLY_COMMANDS)

    def test_the_dashboard_carries_the_clock(self):
        import handsoff_dashboard
        self.assertTrue(hasattr(handsoff_dashboard.DashboardServer, "_performance_clock_loop"),
                        "the run-owned dashboard must tick the clock with no browser attached")


class NothingStartsAfterThePause(HandsoffTestCase):
    """REQ-005."""

    def _pause(self):
        self.init("Paused run")
        supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=121))

    def test_a_mutating_supervisor_command_is_refused(self):
        self._pause()
        self.assertIsNotNone(supervisor.performance_mutation_refusal(self.tmp, "advance"))

    def test_the_refusal_names_the_resume_that_clears_it(self):
        self._pause()
        self.assertIn("performance-resume", supervisor.performance_mutation_refusal(self.tmp, "advance"))

    def test_an_agent_launch_is_refused(self):
        """This entry point had no performance gate at all: the supervisor
        CLI and the dashboard both refused, while `handsoff agent launch`,
        the path a host actually uses, did not."""
        self._pause()
        self.assertIsNotNone(runtime._performance_refusal(self.tmp, "launch_reviewer"))

    def test_a_release_or_cleanup_command_is_refused(self):
        self._pause()
        for operation in ("release-plan", "release-reconcile", "work-items-sync"):
            self.assertIsNotNone(supervisor.performance_mutation_refusal(self.tmp, operation),
                                 f"{operation} may not start during a pause")

    def test_reading_the_state_is_always_allowed(self):
        self._pause()
        for operation in ("status", "validate", "performance-status", "performance-watch"):
            self.assertIsNone(supervisor.performance_mutation_refusal(self.tmp, operation))

    def test_the_resume_itself_is_allowed(self):
        self._pause()
        for operation in ("performance-resume", "run-close", "regression-cancel"):
            self.assertIsNone(supervisor.performance_mutation_refusal(self.tmp, operation))

    def test_a_run_with_no_pause_refuses_nothing(self):
        self.init("Healthy run")
        self.assertIsNone(supervisor.performance_mutation_refusal(self.tmp, "advance"))

    def test_an_agent_launch_in_a_healthy_run_is_not_refused(self):
        self.init("Healthy run")
        self.assertIsNone(runtime._performance_refusal(self.tmp, "launch_reviewer"))


class TheTimerCountsTheRightTime(HandsoffTestCase):
    """REQ-005: retries, provider latency, CI waits, regression time and
    release work all stay inside the budget. Only measured sleep and
    persisted holds come out."""

    def test_elapsed_wall_time_counts_by_default(self):
        history = runtime_control.new_performance_history("run-test", "episode-1", now=EPISODE_2_START)
        seconds = runtime_control.episode_active_seconds(
            history["episodes"][0], now=EPISODE_2_START + timedelta(minutes=45))
        self.assertAlmostEqual(seconds, 45 * 60, delta=1)

    def test_a_persisted_hold_is_subtracted(self):
        history = runtime_control.new_performance_history("run-test", "episode-1", now=EPISODE_2_START)
        episode = history["episodes"][0]
        episode["holds"] = [{"hold_id": "hold-1", "kind": "pilot",
                             "started_at": (EPISODE_2_START + timedelta(minutes=10)).isoformat(),
                             "ended_at": (EPISODE_2_START + timedelta(minutes=20)).isoformat(),
                             "evidence_hash": "0" * 64}]
        seconds = runtime_control.episode_active_seconds(
            episode, now=EPISODE_2_START + timedelta(minutes=45))
        self.assertAlmostEqual(seconds, 35 * 60, delta=1)


if __name__ == "__main__":
    unittest.main()
