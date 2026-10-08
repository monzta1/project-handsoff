"""P1.10 (REQ-004): one status projection. session_projection gives every
managed session one state by fixed rules in a fixed precedence, and the run
one state; status, the run dashboard API (after reconcile_gone_sessions) and
the Fleet cards all read it, and the stall warning is derived from it."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from tests.engine_patch import patch_engine
from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_projection as projection  # noqa: E402

A = "hs-" + "a" * 32
B = "hs-" + "b" * 32


def session(sid, state, started, *, role="implementer", ended=None, **extra):
    terminal = state not in lib.AGENT_SESSION_LIVE_STATES
    record = {"session_id": sid, "role": role, "actor": f"codex-{role}-{sid[3:7]}", "adapter": "codex",
              "requested_model": "default", "reported_model": None, "resolution_source": "configured",
              "started_at": started, "running_at": started if state != "launching" else None,
              "ended_at": (ended or started) if terminal else None,
              "state": state, "exit_code": (0 if state == "completed" else 1) if terminal else None,
              "phase_number": 4}
    record.update(extra)
    return record


def failure(sid, category):
    return {"session_id": sid, "category": category, "reason": lib._FAILURE_REASON_LABELS[category],
            "tail_sha256": "0" * 64, "at": datetime.now(timezone.utc).isoformat()}


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class StatusProjectionTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Status projection P1.10")
        self.now = datetime.now(timezone.utc)
        self.ago = lambda minutes: (self.now - timedelta(minutes=minutes)).isoformat()

    # ---- fixtures -------------------------------------------------------

    def clear_signals(self):
        for path in self.tmp.glob(".handsoff-live*.json"):
            path.unlink()
        for path in (lib.session_liveness_path(self.tmp), lib.output_liveness_path(self.tmp),
                     lib.agent_output_path(self.tmp)):
            path.unlink(missing_ok=True)

    def heartbeat(self, sid, at):
        lib.update_session_liveness(self.tmp, sid, at=at)

    def output(self, sid, at, role="implementer"):
        lib.output_liveness_path(self.tmp).write_text(json.dumps(
            {"session_id": sid, "role": role, "output_at": at, "chunks": 3, "bytes": 120}))

    def beacon(self, sid, pid, at=None, role="implementer"):
        lib.write_live_beacon(self.tmp, session_id=sid, role=role, state="running", pid=pid,
                              now=at or datetime.now(timezone.utc), per_session=True)

    def status_with(self, sessions, pointers=None, failures=None, **fields):
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4],
                      agent_sessions={item["session_id"]: item for item in sessions},
                      current_agent_sessions=pointers or {})
        status.pop("agent_failures", None)
        if failures:
            status["agent_failures"] = {item["session_id"]: item for item in failures}
        status.update(fields)
        return status

    def commit(self, *args, **kwargs):
        status = self.status_with(*args, **kwargs)
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="test_setup", event_message="fixture")
        return status

    def project(self, status, sleep=()):
        with patch_engine("machine_sleep_intervals", return_value=list(sleep)):
            return projection.session_projection(self.tmp, lib.load_config(self.tmp), status, self.now)

    # ---- the three readers ----------------------------------------------

    def status_reader(self):
        result = run(["status"], cwd=self.tmp)
        return json.loads(result.stdout)["session_projection"]  # group A's cmd_status field

    def dashboard_reader(self):
        with patch_engine("machine_sleep_intervals", return_value=[]):
            return dashboard.build_snapshot(self.tmp)

    def fleet_reader(self):
        entry = {"root": str(self.tmp.resolve()), "registered_at": self.now.isoformat()}
        with patch_engine("machine_sleep_intervals", return_value=[]):
            return fleet.project_view(entry, None, facts=fleet.project_facts(self.tmp), owner=None)

    def three_readers(self):
        """(status, dashboard, fleet), each as (run state, {session id: state}).
        status runs first, in its own process, as an operator would."""
        shown = self.status_reader()
        snap = self.dashboard_reader()
        card = self.fleet_reader()
        by_id = lambda sessions: {item["session_id"]: item["state"] for item in sessions}  # noqa: E731
        return ((shown["state"], by_id(shown["sessions"])),
                (snap["status"]["session_projection"]["state"],
                 by_id(snap["status"]["session_projection"]["sessions"])),
                (card["session_state"], by_id(card["session_projection"])))

    # ---- one fixture per rule -------------------------------------------

    def test_each_rule_has_its_own_fixture(self):
        cases = [
            ("failed_recoverable", [session(A, "failed", self.ago(20), ended=self.ago(5))],
             [failure(A, "non_zero_exit")], {}, None),
            ("failed", [session(A, "failed", self.ago(20), ended=self.ago(5))],
             [failure(A, "token_budget_exhaustion")], {}, None),
            ("completed_with_artifact", [session(A, "completed", self.ago(20), ended=self.ago(5), result={
                "kind": "review", "payload": {}, "recorded_at": self.ago(6), "adopted_at": None,
                "adopted_by": None})], None, {}, None),
            ("completed_with_artifact", [session(A, "completed", self.ago(20), ended=self.ago(5),
                                                 apply={"state": "applied", "paths": ["a.py"]})], None, {}, None),
            ("completed_without_artifact", [session(A, "completed", self.ago(20), ended=self.ago(5))],
             None, {}, None),
            ("transport_disconnected", [session(A, "running", self.ago(20))], None, {},
             lambda: (self.heartbeat(A, self.ago(0)), self.beacon(A, dead_pid()))),
            ("waiting_for_background_work", [session(A, "running", self.ago(20))], None,
             {"background_wait": {"by": f"codex-implementer-{A[3:7]}", "since": self.ago(15), "note": "suite"}},
             lambda: self.heartbeat(A, self.ago(15))),
            ("stale_heartbeat", [session(A, "running", self.ago(40))], None, {},
             lambda: self.heartbeat(A, self.ago(30))),
            ("active_output", [session(A, "running", self.ago(40))], None, {},
             lambda: (self.heartbeat(A, self.ago(30)), self.output(A, self.ago(1)))),
            ("connected_no_output", [session(A, "running", self.ago(40))], None, {},
             lambda: self.heartbeat(A, self.ago(1))),
            ("idle", [], None, {}, None),
        ]
        for expected, sessions, failures, fields, signals in cases:
            with self.subTest(expected=expected):
                self.clear_signals()
                if signals:
                    signals()
                view = self.project(self.status_with(sessions, failures=failures, **fields))
                self.assertEqual(view["state"], expected)
                for item in view["sessions"]:
                    self.assertEqual(item["state"], expected)
                    self.assertEqual(set(item), set(projection.SESSION_PROJECTION_FIELDS))
                    self.assertEqual((item["role"], item["provider"], item["session_id"]),
                                     ("implementer", "codex", A))
                    self.assertIsInstance(item["next_action"], str)

    def test_the_record_carries_heartbeat_output_timeout_and_replacements(self):
        self.heartbeat(B, self.ago(2))
        self.output(B, self.ago(1))
        running = session(B, "running", self.ago(30), owned_paths=["bin/x.py"],
                          halfway_at=(self.now - timedelta(minutes=30) + timedelta(seconds=3600)).isoformat())
        status = self.status_with(
            [session(A, "failed", self.ago(50), ended=self.ago(31)), running], {"implementer": B},
            [failure(A, "non_zero_exit")],
            agent_replacements=[{"replacement_id": "hr-1", "action": "launch", "from_session_id": A,
                                 "to_session_id": B}])
        [item] = [s for s in self.project(status)["sessions"] if s["session_id"] == B]
        self.assertEqual(item["state"], "active_output")
        self.assertEqual((item["last_heartbeat_at"], item["last_output_at"]), (self.ago(2), self.ago(1)))
        self.assertEqual((item["owned_paths"], item["timeout_seconds"], item["replacement_count"]),
                         (["bin/x.py"], 7200, 1))
        self.assertEqual(item["elapsed_seconds"], 1800)
        self.assertEqual(item["actor"], f"codex-implementer-{B[3:7]}")

    def test_the_stall_threshold_is_sleep_adjusted(self):
        self.heartbeat(A, self.ago(30))
        status = self.status_with([session(A, "running", self.ago(40))])
        self.assertEqual(self.project(status)["state"], "stale_heartbeat")
        slept = [(self.now - timedelta(minutes=28), self.now - timedelta(minutes=3))]
        self.assertEqual(self.project(status, sleep=slept)["state"], "connected_no_output",
                         "25 of the 30 silent minutes were asleep")

    def test_the_run_state_is_the_most_severe_live_session_else_the_newest_terminal(self):
        self.heartbeat(A, self.ago(30))
        self.output(B, self.ago(0))
        both = self.status_with([session(A, "running", self.ago(40)), session(B, "running", self.ago(5))])
        self.assertEqual(self.project(both)["state"], "stale_heartbeat")
        terminal = self.status_with([session(A, "completed", self.ago(40), ended=self.ago(30)),
                                     session(B, "failed", self.ago(20), ended=self.ago(10))],
                                    failures=[failure(B, "token_budget_exhaustion")])
        self.assertEqual(self.project(terminal)["state"], "failed", "the newest terminal session speaks")

    # ---- conflicting signals, read by all three readers -----------------

    def test_a_fresh_heartbeat_with_a_dead_pid_is_transport_disconnected_everywhere(self):
        self.commit([session(A, "running", self.ago(20))], {"implementer": A})
        self.heartbeat(A, datetime.now(timezone.utc).isoformat())
        self.beacon(A, dead_pid())  # fresh, so reconcile leaves it for the projection to name
        readers = self.three_readers()
        for reader in readers:
            self.assertEqual(reader, ("transport_disconnected", {A: "transport_disconnected"}))

    def test_an_old_heartbeat_with_an_open_background_wait_is_waiting_everywhere(self):
        self.commit([session(A, "running", self.ago(40))], {"implementer": A},
                    background_wait={"by": f"codex-implementer-{A[3:7]}", "since": self.ago(30), "note": "suite"},
                    last_heartbeat_at=self.ago(30), last_heartbeat_owner="background_wait")
        self.heartbeat(A, self.ago(30))
        for reader in self.three_readers():
            self.assertEqual(reader, ("waiting_for_background_work", {A: "waiting_for_background_work"}))

    def test_two_parallel_sessions_in_different_states_read_alike_everywhere(self):
        self.commit([session(A, "running", self.ago(20), owned_paths=["bin/a.py"]),
                     session(B, "running", self.ago(20), owned_paths=["bin/b.py"])], {"implementer": A})
        stamp = datetime.now(timezone.utc).isoformat()
        self.heartbeat(A, stamp)
        self.output(A, stamp)
        self.heartbeat(B, stamp)
        self.beacon(B, dead_pid())
        for reader in self.three_readers():
            self.assertEqual(reader, ("transport_disconnected",
                                      {A: "active_output", B: "transport_disconnected"}))

    def test_a_terminal_failure_reads_alike_everywhere(self):
        self.commit([session(A, "failed", self.ago(20), ended=self.ago(5))], {},
                    [failure(A, "non_zero_exit")])
        for reader in self.three_readers():
            self.assertEqual(reader, ("failed_recoverable", {A: "failed_recoverable"}))

    def test_the_run_dashboard_reconciles_a_gone_process_before_it_projects(self):
        self.commit([session(A, "running", self.ago(20))], {"implementer": A})
        self.beacon(A, dead_pid(), at=datetime.now(timezone.utc) - timedelta(minutes=2))
        snap = self.dashboard_reader()  # no status read first: the dashboard reconciles on its own
        self.assertEqual(self.read_status()["agent_sessions"][A]["state"], "failed")
        self.assertEqual(snap["status"]["session_projection"]["state"], "failed_recoverable")
        for reader in self.three_readers():
            self.assertEqual(reader, ("failed_recoverable", {A: "failed_recoverable"}))

    # ---- the stall warning is derived from the projection ---------------

    def test_a_fresh_heartbeat_clears_the_stall_warning(self):
        self.commit([session(A, "running", self.ago(40))], {"implementer": A})
        self.heartbeat(A, self.ago(30))
        snap = self.dashboard_reader()
        self.assertEqual(snap["status"]["session_projection"]["state"], "stale_heartbeat")
        self.assertTrue(snap["status"]["stall_warning"])
        self.heartbeat(A, datetime.now(timezone.utc).isoformat())
        snap = self.dashboard_reader()
        self.assertEqual(snap["status"]["session_projection"]["state"], "connected_no_output")
        self.assertIsNone(snap["status"]["stall_warning"])
        self.assertIsNone(snap["activity"]["stall_warning"])

    def test_the_derivation_keeps_the_run_reading_only_without_live_sessions(self):
        fresh = {"state": "active_output", "sessions": [{"state": "active_output", "role": "implementer",
                                                         "session_id": A, "next_action": "working"}]}
        stale = {"state": "stale_heartbeat", "sessions": [{"state": "stale_heartbeat", "role": "implementer",
                                                           "session_id": A, "next_action": "check it"}]}
        idle = {"state": "idle", "sessions": []}
        self.assertIsNone(projection.projected_stall_warning(fresh, "no update in 30 minutes (limit 10)"))
        self.assertEqual(projection.projected_stall_warning(stale, "no update in 30 minutes (limit 10)"),
                         "no update in 30 minutes (limit 10)")
        self.assertIn("stale_heartbeat", projection.projected_stall_warning(stale, None))
        self.assertEqual(projection.projected_stall_warning(idle, "legacy"), "legacy")
        self.assertIsNone(projection.projected_stall_warning(idle, None))


if __name__ == "__main__":
    import unittest
    unittest.main()
