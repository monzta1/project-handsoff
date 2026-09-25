import json
from copy import deepcopy
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib
from tests.fixture_state import force_status


class StatusTruthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-status-truth-"))
        self.cfg = {"stall_minutes": 10, "status_file": "handsoff-status.json",
                    "acceptance_file": "handsoff-acceptance.json", "event_log": "handsoff-events.jsonl",
                    "recovery": {"enabled": False}}
        (self.root / "handsoff-acceptance.json").write_text("{}")
        self.now = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
        self.sid = "hs-" + "1" * 32
        # A complete status, because record_stall_transition is real engine
        # code: it reads this file and commits, and commit validates. The
        # liveness fields under test are the ones set below.
        self.status = {"feature": "Status truth", "phase_number": 2,
                       "phase": lib.PHASES[2], "progress": 20,
                       "status": "in_progress", "updated_at": "2026-01-01T00:00:00+00:00",
                       "next_action": "fixture",
                       "requirement_coverage": {"total": 0, "passing": 0, "failing": 0,
                                                "not_tested": 0, "blocked": 0,
                                                "original_symptom_resolved": False},
                       "verification_head": None, "events": [],
                       "agent_sessions": {self.sid: {
                           "session_id": self.sid, "role": "implementer", "state": "running",
                           "actor": "fixture", "adapter": "codex", "requested_model": "default",
                           "reported_model": None, "resolution_source": "configured",
                           "started_at": "2026-01-01T00:00:00+00:00",
                           "running_at": "2026-01-01T00:00:00+00:00",
                           "ended_at": None, "exit_code": None}},
                       "current_agent_sessions": {"implementer": self.sid}}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def beacon(self, at):
        lib.write_live_beacon(self.root, session_id=self.sid, role="implementer",
                              state="running", pid=2, now=at)

    def test_fresh_beacon_wins_over_stale_session_liveness(self):
        lib.update_session_liveness(self.root, self.sid, at="2025-12-31T23:00:00+00:00")
        self.beacon(self.now - timedelta(seconds=5))
        view = lib.liveness_view(self.status, self.root, self.cfg, self.now)
        self.assertIsNone(view["stall_warning"])
        self.assertEqual(view["process_signal"], "fresh")

    def test_stale_beacon_has_bounded_warning_text(self):
        self.beacon(self.now - timedelta(minutes=12))
        view = lib.liveness_view(self.status, self.root, self.cfg, self.now)
        self.assertEqual(view["stall_warning"], "no update in 12 minutes (limit 10)")

    def test_two_readers_clear_once(self):
        # The reported-stall bit lives on disk; each reader re-reads it under
        # the lock, so a fresh beacon seen by two concurrent readers is
        # ledgered as one stall_cleared, never two.
        self.status["stall_reported"] = True
        # A partial status by design: this isolates liveness, so it is placed
        # on disk rather than committed, which would now refuse it.
        force_status(self.root, self.cfg, self.status)
        self.beacon(self.now - timedelta(seconds=2))

        def reader():
            view = lib.liveness_view(self.status, self.root, self.cfg, self.now)
            with lib.project_lock(self.root):
                lib.record_stall_transition(self.root, self.cfg, view["stall_warning"])

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        events = lib.read_events(self.root, self.cfg)
        self.assertEqual(sum(event.get("kind") == "stall_cleared" for event in events), 1)

    def test_status_and_snapshot_do_not_deadlock_on_the_project_lock(self):
        # Regression: liveness_view used to take the project lock while its
        # callers already held it, hanging status and the dashboard.
        import subprocess, sys
        force_status(self.root, self.cfg, self.status)
        (self.root / ".handsoff-version").write_text("0.3.*\n")
        result = subprocess.run([sys.executable, str(ROOT / "bin" / "handsoff_supervisor.py"), "--root",
                                 str(self.root), "status"], capture_output=True, text=True, timeout=30)
        # The hand-built fixture is not a valid run, so status may exit 1
        # with its JSON; what matters is that it answered at all.
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        self.assertIn("activity", json.loads(result.stdout))
        import handsoff_dashboard as dashboard
        snapshot = dashboard.build_snapshot(self.root)
        self.assertEqual(snapshot["activity"]["stall_warning"], json.loads(result.stdout)["activity"]["stall_warning"])

    def test_phase_seconds_follow_advanced_events(self):
        status = {"status": "complete", "updated_at": "2026-01-01T00:30:00+00:00"}
        events = [{"kind": "initialized", "phase_number": 1, "at": "2026-01-01T00:00:00+00:00"},
                  {"kind": "phase_advanced", "phase_number": 2, "at": "2026-01-01T00:10:00+00:00"},
                  {"kind": "phase_advanced", "phase_number": 3, "at": "2026-01-01T00:20:00+00:00"}]
        metrics = lib.build_run_metrics(status, events, [], now=self.now)
        self.assertEqual(metrics["phase_seconds"]["1"], 600.0)
        self.assertEqual(metrics["phase_seconds"]["2"], 600.0)
        self.assertEqual(metrics["phase_seconds"]["3"], 600.0)

    def test_liveness_view_has_shared_contract(self):
        self.beacon(self.now - timedelta(seconds=1))
        view = lib.liveness_view(self.status, self.root, self.cfg, self.now)
        # #193: asleep_seconds joined the contract (the age is awake time)
        self.assertEqual(set(view), {"seconds_since_activity", "asleep_seconds", "process_signal", "stall_warning",
                                     "stall_threshold_minutes", "assessment", "activity_note"})

    def test_agent_output_states_have_timing_contract(self):
        self.status["agent_sessions"][self.sid].update({"role": "implementer", "adapter": "codex"})
        view = lib.agent_output_view(self.status, self.root, now=self.now)
        self.assertEqual(view["state"], "transport_disconnected")
        self.assertIn("last_heartbeat_age_seconds", view)

    def test_live_reviewer_selection_rebuilds_from_session(self):
        sid = "hs-" + "2" * 32
        self.status["agent_sessions"][sid] = {"session_id": sid, "role": "reviewer", "actor": "pilot",
            "adapter": "codex", "state": "running", "phase_number": 2, "started_at": "2026-01-01T00:30:00+00:00"}
        view = lib.design_reviewer_selection_view(deepcopy(lib.DEFAULT_CONFIG), self.status, {})
        self.assertEqual(view["current"]["session_id"], sid)
        self.assertEqual(view["current"]["attempt"], 1)

    def test_design_attempts_never_below_history(self):
        self.status["design_review_attempts"] = 1
        self.status["design_review_history"] = [{}, {}, {}]
        self.assertEqual(lib.design_review_budget(self.status, {})["attempts"], 3)

    def test_second_live_reviewer_is_consistency_error(self):
        for n in (2, 3):
            sid = "hs-" + str(n) * 32
            self.status["agent_sessions"][sid] = {"session_id": sid, "role": "reviewer", "state": "running",
                "phase_number": 2, "started_at": f"2026-01-01T00:{n}:00+00:00"}
        view = lib.design_reviewer_selection_view(deepcopy(lib.DEFAULT_CONFIG), self.status, {})
        self.assertTrue(any("unexpected_live_sessions" in item for item in view["consistency_errors"]))

    def test_selection_metadata_naming_another_reviewer_is_a_consistency_error(self):
        sid = "hs-" + "2" * 32
        self.status["agent_sessions"][sid] = {"session_id": sid, "role": "reviewer", "actor": "codex-reviewer",
            "adapter": "codex", "state": "running", "phase_number": 2, "started_at": "2026-01-01T00:30:00+00:00"}
        self.status["design_reviewer_selection"] = {"current": {"actor": "old-reviewer", "session_id": "hs-" + "9" * 32}}
        view = lib.design_reviewer_selection_view(deepcopy(lib.DEFAULT_CONFIG), self.status, {})
        self.assertEqual(view["current"]["session_id"], sid)
        self.assertTrue(any("old-reviewer" in item and "codex-reviewer" in item for item in view["consistency_errors"]),
                        view["consistency_errors"])

    def test_phase_five_reviewer_is_not_a_design_selection_fault(self):
        # #113: the implementation reviewer never has selection metadata.
        sid = "hs-" + "5" * 32
        self.status["phase_number"] = 5
        self.status["design_review"] = {"decision": "approved", "reviewer_profile": {
            "tier": "primary", "adapter": "codex", "model": "default", "reason": "first review"}}
        self.status["agent_sessions"][sid] = {"session_id": sid, "role": "reviewer", "actor": "codex-reviewer",
            "adapter": "codex", "state": "running", "phase_number": 5, "started_at": "2026-01-01T00:40:00+00:00"}
        view = lib.design_reviewer_selection_view(deepcopy(lib.DEFAULT_CONFIG), self.status, {})
        self.assertEqual(view["consistency_errors"], [])
        self.assertEqual(view["current"]["adapter"], "codex")
        self.assertNotIn("session_id", view["current"])

    def test_phase_two_reviewer_without_metadata_still_faults(self):
        sid = "hs-" + "6" * 32
        self.status["agent_sessions"][sid] = {"session_id": sid, "role": "reviewer", "actor": "codex-reviewer",
            "adapter": "codex", "state": "running", "phase_number": 2, "started_at": "2026-01-01T00:40:00+00:00"}
        view = lib.design_reviewer_selection_view(deepcopy(lib.DEFAULT_CONFIG), self.status, {})
        self.assertTrue(any("no selection metadata" in item for item in view["consistency_errors"]), view)

    def test_agent_output_states_with_an_output_record(self):
        # Regression: entries were read before assignment whenever a record
        # existed, so no live session could render a state.
        self.status["agent_sessions"][self.sid].update({"role": "implementer", "adapter": "codex", "state": "running"})
        record = {"session_id": self.sid, "role": "implementer", "adapter": "codex", "cursor": 0,
                  "dropped_entries": 0, "entries": [], "updated_at": self.now.isoformat()}
        (self.root / lib.OUTPUT_RECORD_FILE if hasattr(lib, "OUTPUT_RECORD_FILE") else lib.agent_output_path(self.root)).write_text(
            json.dumps({"schema": 1, "order": [self.sid], "sessions": {self.sid: record}}))
        self.beacon(self.now - timedelta(seconds=1))
        view = lib.agent_output_view(self.status, self.root, now=self.now)
        self.assertEqual(view["state"], "connected_no_output", view)
        record["entries"] = [{"cursor": 1, "at": self.now.isoformat(), "stream": "stdout", "text": "hello"}]
        lib.agent_output_path(self.root).write_text(json.dumps({"schema": 1, "order": [self.sid], "sessions": {self.sid: record}}))
        view = lib.agent_output_view(self.status, self.root, now=self.now)
        self.assertEqual(view["state"], "active_output", view)
        self.assertEqual(view["last_output_at"], self.now.isoformat())
        self.beacon(self.now - timedelta(seconds=120))
        view = lib.agent_output_view(self.status, self.root, now=self.now)
        self.assertEqual(view["state"], "stale_heartbeat", view)


if __name__ == "__main__":
    unittest.main(verbosity=2)
