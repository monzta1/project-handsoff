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
import io
import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.fixture_state import write_version_pin
from tests.test_compact_review import session_fields_accepted
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.guards import guard

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

    @guard
    def test_the_clock_does_not_retire_at_the_first_pause(self):
        """The bug this replaces: the thread returned on the first pause,
        and `performance-resume` opens a NEW episode with its own deadline.
        Every episode after the first would then be unobserved, which is
        the exact v0.3.80 shape: episode 1 paused on time, episode 2 never
        paused at all."""
        import inspect as _inspect
        import handsoff_dashboard
        body = _inspect.getsource(handsoff_dashboard.DashboardServer._performance_clock_loop)
        self.assertNotIn("paused_for_performance_review", body.split('"""')[-1],
                         "the clock loop must not branch on the pause state and stop")

    def test_a_second_episode_is_evaluated_after_a_resume(self):
        """A resumed run gets a fresh episode and a fresh deadline, and the
        clock must pause that one too."""
        first = runtime_control.new_performance_history("run-test", "episode-1", now=EPISODE_2_START)
        paused, decision = runtime_control.transition_performance(
            first, now=EPISODE_2_START + timedelta(minutes=121))
        self.assertEqual(decision["action"], "pause_for_performance_review")

        resumed_at = EPISODE_2_START + timedelta(minutes=130)
        resumed = runtime_control.resume_performance(
            paused,
            {"decision_id": "resume-1", "action": "resume", "actor": "moncy",
             "reason": "reevaluated", "evidence_hash": "0" * 64, "at": resumed_at.isoformat()},
            "episode-2")
        self.assertEqual(resumed["episodes"][-1]["episode_id"], "episode-2")

        # The second episode has its own ceiling, and it must fire.
        _final, second = runtime_control.transition_performance(
            resumed, now=resumed_at + timedelta(minutes=121))
        self.assertEqual(second["action"], "pause_for_performance_review",
                         "episode 2 must reach its own ceiling; this is the v0.3.80 failure")
        self.assertEqual(second["episode_id"], "episode-2")


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


class TheClockIsRebuiltFromTheJournal(HandsoffTestCase):
    """#385/REQ-006: the timeline is written once per transition to its own
    hash-chained journal, never the event ledger, and a lost or damaged
    record is rebuilt from it with the same elapsed time."""

    def setUp(self):
        super().setUp()
        self.init("Clock")
        self.cfg = lib.load_config(self.tmp)
        self.start = datetime.now(timezone.utc)
        self.record = self.tmp / supervisor.RUNTIME_CONTROL_DIR / supervisor.PERFORMANCE_RECORD
        self.journal = self.tmp / supervisor.RUNTIME_CONTROL_DIR / supervisor.PERFORMANCE_TIMELINE_JOURNAL

    def _timeline(self):
        run_id = supervisor._runtime_run_id(self.tmp, lib.read_events(self.tmp, self.cfg))
        return supervisor._performance_journal(self.tmp, run_id)[0]

    def test_no_timeline_entry_is_written_to_the_event_ledger(self):
        self._refresh(30)
        supervisor.performance_tick(self.tmp, now=self.start + timedelta(minutes=135))
        self.assertTrue(self._timeline())
        kinds = {event.get("kind") for event in lib.read_events(self.tmp, self.cfg)}
        self.assertNotIn("performance_timeline", kinds)

    def test_read_only_commands_leave_the_event_ledger_byte_identical(self):
        ledger = lib.event_log_path(self.tmp, self.cfg)
        before = ledger.read_bytes()
        for argv in (["status"], ["validate"], ["doctor", "--dry-run"]):
            with self.subTest(argv[0]):
                run(argv, cwd=self.tmp)
                self.assertEqual(ledger.read_bytes(), before)
        self.assertTrue(self.journal.exists())

    def test_a_line_failing_its_chain_is_refused_not_used(self):
        self._refresh(30)
        lines = self.journal.read_text(encoding="utf-8").splitlines()
        hold = next(index for index, line in enumerate(lines) if '"hold_started"' in line)
        forged = json.loads(lines[hold])
        forged["timeline"]["at"] = (self.start + timedelta(minutes=1)).isoformat()
        lines[hold] = json.dumps(forged)
        self.journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _timeline, _head, refused = runtime_control.accepted_timeline(lines, forged["run_id"])
        self.assertGreaterEqual(refused, 1)
        self.assertNotIn(forged["timeline"], self._timeline())

    def _events_with_hold(self):
        """The run's ledger plus a ten-minute Pilot hold starting at +10."""
        return [*lib.read_events(self.tmp, self.cfg),
                {"kind": "human_pause_started", "at": (self.start + timedelta(minutes=10)).isoformat(),
                 "by": "pilot", "hash": "a" * 64},
                {"kind": "human_pause_ended", "at": (self.start + timedelta(minutes=20)).isoformat(),
                 "by": "pilot", "hash": "b" * 64}]

    def _refresh(self, minutes):
        return supervisor.refresh_performance_state(
            self.tmp, events=self._events_with_hold(), now=self.start + timedelta(minutes=minutes))

    def _refresh_from_ledger(self, minutes):
        """The hold reaches this refresh only through the recorded timeline."""
        return supervisor.refresh_performance_state(self.tmp, now=self.start + timedelta(minutes=minutes))

    def test_each_transition_is_written_once(self):
        supervisor.refresh_performance_state(self.tmp, now=self.start)
        supervisor.refresh_performance_state(self.tmp, now=self.start + timedelta(minutes=1))
        self.assertEqual([entry["kind"] for entry in self._timeline()], ["episode_started"])
        self._refresh(30)
        self._refresh(31)
        self.assertEqual([entry["kind"] for entry in self._timeline()],
                         ["episode_started", "hold_started", "hold_ended"])
        supervisor.performance_tick(self.tmp, now=self.start + timedelta(minutes=135))
        supervisor.performance_tick(self.tmp, now=self.start + timedelta(minutes=136))
        kinds = [entry["kind"] for entry in self._timeline()]
        self.assertEqual(kinds, ["episode_started", "hold_started", "hold_ended", "performance_paused"])
        keys = [runtime_control.performance_timeline_key(entry) for entry in self._timeline()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(lib.verify_event_log(self.tmp, self.cfg), [])

    def test_a_missing_record_is_rebuilt_with_the_same_elapsed_time(self):
        before = self._refresh(50)
        self.assertAlmostEqual(before["active_seconds"], 40 * 60, delta=1)
        self.record.unlink()
        after = self._refresh_from_ledger(50)
        self.assertEqual(after["episode_id"], before["episode_id"])
        self.assertAlmostEqual(after["active_seconds"], before["active_seconds"], delta=0.001)
        self.assertTrue(self.record.exists())

    def test_a_rebuilt_paused_run_stays_paused(self):
        supervisor.performance_tick(self.tmp, now=self.start + timedelta(minutes=125))
        self.record.unlink()
        view = supervisor.refresh_performance_state(self.tmp, now=self.start + timedelta(minutes=126))
        self.assertEqual(view["state"], "paused_for_performance_review")
        self.assertTrue(view["block_new_work"])

    def test_a_tampered_record_is_rebuilt_with_the_same_elapsed_time(self):
        before = self._refresh(50)
        valid = json.loads(self.record.read_text(encoding="utf-8"))
        invalid = {**valid, "breaches": "many"}
        signed_legacy = {"run_id": valid["run_id"], "episode_id": "episode-1",
                         "started_at": self.start.isoformat(), "state": "active",
                         "_auth": {"algorithm": "hmac-sha256", "key_id": "forged", "signature": "c" * 64}}
        for label, text in (("unparsable", "{not json"), ("invalid", json.dumps(invalid)),
                            ("bad signature", json.dumps(signed_legacy))):
            with self.subTest(label):
                self.record.write_text(text, encoding="utf-8")
                after = self._refresh_from_ledger(50)
                self.assertAlmostEqual(after["active_seconds"], before["active_seconds"], delta=0.001)
                rebuilt = json.loads(self.record.read_text(encoding="utf-8"))
                runtime_control.validate_performance_history(rebuilt)
                self.assertEqual(rebuilt["episodes"][0]["holds"], valid["episodes"][0]["holds"])

    def test_an_unsigned_legacy_record_stays_read_only(self):
        supervisor.refresh_performance_state(self.tmp, now=self.start)
        legacy = json.dumps({"run_id": "run-legacy", "episode_id": "episode-1",
                             "started_at": self.start.isoformat(), "state": "active"})
        self.record.write_text(legacy, encoding="utf-8")
        with self.assertRaisesRegex(runtime_control.MutationRefused, "not authenticated"):
            supervisor.refresh_performance_state(self.tmp, now=self.start + timedelta(minutes=5))
        self.assertEqual(self.record.read_text(encoding="utf-8"), legacy)


class _Pipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _Process:
    """A fake adapter child: `work` runs in its working directory, then it
    prints `stdout` and exits 0."""
    pid = 4244

    def __init__(self, cwd, stdout="", work=None):
        if work:
            work(Path(cwd))
        self.returncode = 0
        self.stdin = _Pipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


APPROVED = "HANDSOFF_REVIEW_RESULT: " + json.dumps({
    "kind": "implementation", "decision": "approved", "summary": "fine", "findings": [],
    "structural_blocker": False, "symptom_reproduced": "not_applicable", "tests_executed": "yes"}) + "\n"


class TheLaunchEpisodeDecides(unittest.TestCase):
    """#383/REQ-004: the episode a session belongs to is the last one started
    at or before its started_at, not whichever episode is current now."""

    HISTORY = {"episodes": [
        {"episode_id": "episode-1", "started_at": "2026-09-23T18:00:00+00:00",
         "state": "paused_for_performance_review"},
        {"episode_id": "episode-2", "started_at": "2026-09-23T20:01:36+00:00", "state": "active"},
    ]}

    def test_a_session_started_before_the_resume_belongs_to_the_paused_episode(self):
        episode = runtime._launch_episode(self.HISTORY, "2026-09-23T19:59:00+00:00")
        self.assertEqual(episode["episode_id"], "episode-1")

    def test_a_session_started_after_the_resume_belongs_to_the_new_episode(self):
        episode = runtime._launch_episode(self.HISTORY, "2026-09-23T20:05:00Z")
        self.assertEqual(episode["episode_id"], "episode-2")

    def test_a_session_older_than_every_episode_has_none(self):
        self.assertIsNone(runtime._launch_episode(self.HISTORY, "2026-09-23T17:00:00+00:00"))
        self.assertIsNone(runtime._launch_episode(self.HISTORY, None))


class ALateResultIsQuarantined(HandsoffTestCase):
    """#383/REQ-004, the quarantine half: a reviewer verdict or an implementer
    workspace finishing in a paused launch episode is neither dispatched nor
    applied, and is kept on the session for adoption after the resume."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Late result")
        self.enterContext(session_fields_accepted("quarantined_result"))

    def _pause(self, lose_record=False):
        supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=121))
        if lose_record:
            # #385: only the timeline journal says the episode is paused.
            supervisor._runtime_path(self.tmp, supervisor.PERFORMANCE_RECORD).unlink()
            self.assertTrue(supervisor._runtime_path(self.tmp, supervisor.PERFORMANCE_TIMELINE_JOURNAL).exists())

    def _launch(self, spec, stdout="", work=None):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = runtime.execute_launch(
                spec, beacon_interval=0.01,
                popen_factory=lambda argv, **kwargs: _Process(kwargs["cwd"], stdout, work))
        return code, out.getvalue()

    def _reviewer(self):
        return runtime.LaunchSpec("reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded prompt", token_budget=40_000,
                                  project_root=str(self.tmp.resolve()))

    def _events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def _session(self, role):
        status = self.read_status()
        sid = lib.role_session_ids(status)[role]  # #420: an ended session leaves the pointer
        return status, sid, status["agent_sessions"][sid]

    def test_a_reviewer_verdict_in_a_paused_episode_is_held_not_dispatched(self, lose_record=False):
        self._pause(lose_record)
        code, out = self._launch(self._reviewer(), APPROVED)
        self.assertEqual(code, 0)
        status, sid, session = self._session("reviewer")
        self.assertIsNone(status.get("review"), "a quarantined verdict must not be recorded")
        held = session["quarantined_result"]
        self.assertEqual((held["kind"], held["role"], held["episode_id"]), ("review", "reviewer", "episode-1"))
        self.assertEqual(held["result"]["decision"], "approved")
        self.assertIsNone(held["adopted_at"])
        self.assertIsNone(session["result"]["adopted_at"], "the persisted result stays unadopted")
        self.assertEqual([(e["session_id"], e["episode_id"], e["role"])
                          for e in self._events("late_result_quarantined")],
                         [(sid, "episode-1", "reviewer")])
        self.assertIn("LATE_RESULT_QUARANTINED", out)

    def test_an_implementer_workspace_in_a_paused_episode_is_left_unapplied(self, lose_record=False):
        (self.tmp / "a.txt").write_text("original\n")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)
        self._pause(lose_record)
        spec = runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=("a.txt",))
        code, out = self._launch(spec, work=lambda cwd: (cwd / "a.txt").write_text("by agent\n"))
        self.assertEqual(code, 0)
        _status, sid, session = self._session("implementer")
        self.assertEqual((self.tmp / "a.txt").read_text(), "original\n", "a quarantined workspace was applied")
        self.assertNotIn("apply", session)
        self.assertEqual(Path(session["workspace"]["path"]).joinpath("a.txt").read_text(), "by agent\n",
                         "the workspace is the result and must survive for adoption")
        held = session["quarantined_result"]
        self.assertEqual((held["kind"], held["role"], held["episode_id"]),
                         ("implementer_workspace", "implementer", "episode-1"))
        self.assertEqual([(e["session_id"], e["episode_id"], e["role"])
                          for e in self._events("late_result_quarantined")],
                         [(sid, "episode-1", "implementer")])
        self.assertIn("LATE_RESULT_QUARANTINED", out)

    def test_a_reviewer_verdict_is_held_when_only_the_journal_records_the_pause(self):
        self.test_a_reviewer_verdict_in_a_paused_episode_is_held_not_dispatched(lose_record=True)

    def test_an_implementer_workspace_is_held_when_only_the_journal_records_the_pause(self):
        self.test_an_implementer_workspace_in_a_paused_episode_is_left_unapplied(lose_record=True)

    def test_a_result_in_a_healthy_episode_proceeds(self):
        supervisor.performance_tick(self.tmp)
        session = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        self.assertIsNone(runtime._quarantine_late_result(self.tmp, session["session_id"], "reviewer",
                                                          "review", {"decision": "approved"}))
        self.assertEqual(self._events("late_result_quarantined"), [])

    def test_a_run_never_on_the_clock_is_not_put_on_it(self):
        session = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        self.assertIsNone(runtime._quarantine_late_result(self.tmp, session["session_id"], "reviewer",
                                                          "review", {"decision": "approved"}))
        self.assertFalse(supervisor._runtime_path(self.tmp, supervisor.PERFORMANCE_RECORD).exists())


class AQuarantinedResultIsAdoptedOnce(HandsoffTestCase):
    """#383/REQ-004, the adoption half: performance-resume moves the launch
    episode out of the pause, then session-result-adopt applies the held
    result once through the normal path and refuses a second adopt."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Late result")

    _pause = ALateResultIsQuarantined._pause
    _launch = ALateResultIsQuarantined._launch
    _reviewer = ALateResultIsQuarantined._reviewer
    _events = ALateResultIsQuarantined._events
    _session = ALateResultIsQuarantined._session

    def _resume(self):
        args = type("Args", (), {"root": str(self.tmp), "by": "pilot", "reason": "reevaluated",
                                 "evidence_hash": None})()
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(supervisor.cmd_performance_resume(args), 0)
        history = supervisor._runtime_read(self.tmp, supervisor.PERFORMANCE_RECORD,
                                           "handsoff.performance_history")
        self.assertEqual(history["episodes"][0]["state"], "completed",
                         "the resume must move the launch episode out of the pause")
        self.assertEqual(history["episodes"][-1]["state"], "active")

    def test_a_quarantined_verdict_is_replayed_once_after_the_resume(self):
        self._pause()
        self._launch(self._reviewer(), APPROVED)
        _status, sid, session = self._session("reviewer")
        self.assertIsNone(session["quarantined_result"]["adopted_at"])
        import handsoff_broker as broker
        with mock.patch.object(broker, "execute_request") as replay:
            adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
            self.assertFalse(adopted)
            self.assertIn("still paused_for_performance_review", message)
            self.assertIn("performance-resume", message)
            replay.assert_not_called()

            self._resume()
            adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
            self.assertEqual((adopted, message), (True, "SESSION_RESULT_ADOPTED"))
            self.assertEqual(replay.call_count, 1)
            request = replay.call_args.args[1]
            self.assertEqual((request["command"], request["adopted_session"], request["adopted_by"]),
                             ("record-review", sid, "pilot"))

            adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
            self.assertFalse(adopted)
            self.assertIn("already adopted", message)
            self.assertEqual(replay.call_count, 1, "a second adopt must not replay the verdict again")
        _status, _sid, session = self._session("reviewer")
        self.assertEqual(session["quarantined_result"]["adopted_by"], "pilot")
        self.assertIsNotNone(session["quarantined_result"]["adopted_at"])
        self.assertEqual(session["result"]["adopted_by"], "pilot")
        self.assertEqual(len(self._events("session_result_adopted")), 1)

    def test_a_quarantined_workspace_is_applied_once_after_the_resume(self):
        (self.tmp / "a.txt").write_text("original\n")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)
        self._pause()
        spec = runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=("a.txt",))
        self._launch(spec, work=lambda cwd: (cwd / "a.txt").write_text("by agent\n"))
        _status, sid, session = self._session("implementer")
        workspace = Path(session["workspace"]["path"])

        adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
        self.assertFalse(adopted)
        self.assertIn("still paused_for_performance_review", message)
        self.assertEqual((self.tmp / "a.txt").read_text(), "original\n")

        self._resume()
        adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
        self.assertEqual((adopted, message), (True, "SESSION_RESULT_ADOPTED"))
        self.assertEqual((self.tmp / "a.txt").read_text(), "by agent\n")
        _status, _sid, session = self._session("implementer")
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["a.txt"]})
        self.assertEqual(session["quarantined_result"]["adopted_by"], "pilot")
        self.assertFalse(workspace.exists(), "an adopted workspace is removed like an applied one")

        (self.tmp / "a.txt").write_text("host edit\n")
        adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
        self.assertFalse(adopted)
        self.assertIn("already adopted", message)
        self.assertEqual((self.tmp / "a.txt").read_text(), "host edit\n")
        self.assertEqual(len(self._events("session_result_adopted")), 1)

    def test_a_workspace_the_host_changed_meanwhile_is_refused_by_the_normal_checks(self):
        (self.tmp / "a.txt").write_text("original\n")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)
        self._pause()
        spec = runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=("a.txt",))
        self._launch(spec, work=lambda cwd: (cwd / "a.txt").write_text("by agent\n"))
        _status, sid, _session = self._session("implementer")
        self._resume()
        (self.tmp / "a.txt").write_text("host edit\n")
        adopted, message = supervisor.adopt_session_result(self.tmp, sid, "pilot")
        self.assertFalse(adopted)
        self.assertIn("host_edit", message)
        self.assertEqual((self.tmp / "a.txt").read_text(), "host edit\n")
        _status, _sid, session = self._session("implementer")
        self.assertIsNone(session["quarantined_result"]["adopted_at"])


if __name__ == "__main__":
    unittest.main()
