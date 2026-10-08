"""#420: ended sessions never stay current. A terminal transition clears
its role's current_agent_sessions entry in the same commit; recovery reads
each role's latest unsuperseded terminal session from agent_sessions; a
live-state session whose recorded process is gone is reconciled to failed
(process_gone) on the next status read; and a session that never recorded a
process, older than the stall window, is cancelled by run-close
--cancel-active."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

OLD = "hs-" + "a" * 32
NEW = "hs-" + "b" * 32


def session(sid, role, state, started, ended=None):
    terminal = state not in lib.AGENT_SESSION_LIVE_STATES
    return {"session_id": sid, "role": role, "actor": f"codex-{role}", "adapter": "codex",
            "requested_model": "default", "reported_model": None, "resolution_source": "configured",
            "started_at": started, "running_at": started if state != "launching" else None,
            "ended_at": (ended or started) if terminal else None,
            "state": state, "exit_code": 1 if terminal else None}


def failure(sid, category):
    return {"session_id": sid, "category": category, "reason": lib._FAILURE_REASON_LABELS[category],
            "tail_sha256": "0" * 64, "at": datetime.now(timezone.utc).isoformat()}


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class SessionTerminalTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.init("Session terminal #420")
        self.now = datetime.now(timezone.utc)
        self.ago = lambda minutes: (self.now - timedelta(minutes=minutes)).isoformat()

    def commit(self, phase, sessions, pointers, failures=None):
        status = self.read_status()
        status.update(phase_number=phase, phase=lib.PHASES[phase],
                      agent_sessions={item["session_id"]: item for item in sessions},
                      current_agent_sessions=pointers)
        status.pop("agent_failures", None)
        if failures:
            status["agent_failures"] = {item["session_id"]: item for item in failures}
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="test_setup", event_message="fixture")
        return status

    def events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def test_every_terminal_transition_clears_the_role_entry_in_the_same_commit(self):
        for state in ("completed", "failed", "timed_out", "cancelled"):
            with self.subTest(state=state):
                self.commit(4, [session(OLD, "implementer", "running", self.ago(1))], {"implementer": OLD})
                kw = {"failure": lib.classify_runtime_failure(exit_code=1)} if state == "failed" else {}
                lib.transition_agent_session(self.tmp, OLD, state, exit_code=0 if state == "completed" else 1, **kw)
                status = self.read_status()
                self.assertNotIn("implementer", status["current_agent_sessions"])
                self.assertEqual(status["agent_sessions"][OLD]["state"], state)
                # one commit: the event that records the ending is the one that saw the cleared pointer
                [ended] = self.events(f"agent_session_{state}")[-1:]
                self.assertEqual(ended["status_sha256"],
                                 __import__("hashlib").sha256((self.tmp / "handsoff-status.json").read_bytes()).hexdigest())
                # readers of the ended session still find it
                self.assertEqual(lib.role_session_ids(status)["implementer"], OLD)
                self.assertEqual(lib.current_agent_sessions(status)["implementer"]["session_id"], OLD)
        self.commit(4, [session(OLD, "implementer", "launching", self.ago(1))], {"implementer": OLD})
        lib.transition_agent_session(self.tmp, OLD, "failed_to_start", failure=lib.classify_runtime_failure(exit_code=-1))
        self.assertEqual(self.read_status()["current_agent_sessions"], {})

    def test_after_cleanup_a_recoverable_failed_reviewer_is_still_found_and_replaced(self):
        self.commit(5, [session(OLD, "reviewer", "failed", self.ago(40), self.ago(30))], {},
                    [failure(OLD, "non_zero_exit")])
        status = self.read_status()
        self.assertEqual(status["current_agent_sessions"], {})
        assessment = lib.recovery_assessment(status, lib.load_config(self.tmp), {}, [], self.now, root=self.tmp)
        self.assertEqual((assessment["state"], assessment["lost_session_id"]), ("worker_terminal", OLD))
        launched = []
        result = lib.recover_run(self.tmp, actor="watchdog", now=self.now,
                                 launcher=lambda role: launched.append(role) or True)
        self.assertEqual(launched, ["reviewer"], result)

    def test_a_non_recoverable_failure_still_blocks_recovery_after_cleanup(self):
        self.commit(5, [session(OLD, "reviewer", "failed", self.ago(40), self.ago(30))], {},
                    [failure(OLD, "token_budget_exhaustion")])
        assessment = lib.recovery_assessment(self.read_status(), lib.load_config(self.tmp), {}, [], self.now,
                                             root=self.tmp)
        self.assertEqual((assessment["reason"], assessment["lost_session_id"]), ("non_recoverable_failure", OLD))
        launched = []
        lib.recover_run(self.tmp, actor="watchdog", now=self.now, launcher=lambda role: launched.append(role) or True)
        self.assertEqual(launched, [])

    def test_a_superseded_terminal_session_is_skipped(self):
        # OLD failed and was replaced by NEW, which completed: OLD is not the reviewer's session
        status = self.commit(5, [session(OLD, "reviewer", "failed", self.ago(40), self.ago(30)),
                                 session(NEW, "reviewer", "completed", self.ago(20), self.ago(25))], {},
                             [failure(OLD, "token_budget_exhaustion")])
        self.assertEqual(lib.role_session_ids(status)["reviewer"], NEW)
        assessment = lib.recovery_assessment(status, lib.load_config(self.tmp), {}, [], self.now, root=self.tmp)
        self.assertNotEqual(assessment["reason"], "non_recoverable_failure", assessment)

    def test_a_live_session_whose_process_is_gone_is_reconciled_on_the_next_status_read(self):
        self.commit(4, [session(OLD, "implementer", "running", self.ago(5))], {"implementer": OLD})
        lib.write_live_beacon(self.tmp, session_id=OLD, role="implementer", state="running", pid=dead_pid(),
                              now=self.now - timedelta(minutes=2))
        before = self.read_status()["agent_sessions"][OLD]["state"]
        run(["status"], cwd=self.tmp)  # the read reports the fixture's gate errors; the reconcile is what matters
        status = self.read_status()
        self.assertEqual(before, "running")
        self.assertEqual(status["agent_sessions"][OLD]["state"], "failed")
        self.assertEqual(status["agent_failures"][OLD]["category"], "process_gone")
        self.assertNotIn("implementer", status["current_agent_sessions"])

    def test_a_live_process_or_a_fresh_beacon_is_never_reconciled(self):
        for pid, beacon_at in ((os.getpid(), self.now - timedelta(minutes=2)), (dead_pid(), self.now)):
            with self.subTest(alive=pid == os.getpid()):
                self.commit(4, [session(OLD, "implementer", "running", self.ago(5))], {"implementer": OLD})
                lib.write_live_beacon(self.tmp, session_id=OLD, role="implementer", state="running", pid=pid,
                                      now=beacon_at)
                self.assertEqual(lib.reconcile_gone_sessions(self.tmp), [])
                self.assertEqual(self.read_status()["agent_sessions"][OLD]["state"], "running")

    def test_run_close_cancels_a_session_that_never_recorded_a_process(self):
        # the 2026-10-07 reviewer stayed running with no pid; one still launching ends failed_to_start
        for state, ended in (("running", "cancelled"), ("launching", "failed_to_start")):
            with self.subTest(state=state):
                self.commit(5, [session(OLD, "reviewer", state, self.ago(15))], {"reviewer": OLD})
                closed = run(["run-close", "--by", "test-pilot", "--reason", "stray launch", "--cancel-active"],
                             cwd=self.tmp)
                self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
                status = self.read_status()
                self.assertEqual(status["agent_sessions"][OLD]["state"], ended)
                self.assertEqual(status["run_closed"]["session_ids"], [OLD])
                self.assertNotIn("reviewer", status["current_agent_sessions"])
                self.tearDown()
                self.setUp()

    def test_a_fresh_beacon_live_session_still_requires_the_existing_proof(self):
        # a beacon names a process (this test's own pid, not a group leader): run-close must prove it
        self.commit(5, [session(OLD, "reviewer", "running", self.ago(15))], {"reviewer": OLD})
        lib.write_live_beacon(self.tmp, session_id=OLD, role="reviewer", state="running", pid=os.getpid(),
                              now=datetime.now(timezone.utc))
        closed = run(["run-close", "--by", "test-pilot", "--reason", "stray launch", "--cancel-active"], cwd=self.tmp)
        self.assertNotEqual(closed.returncode, 0, closed.stdout)
        self.assertIn("process-group leader", closed.stdout + closed.stderr)
        self.assertEqual(self.read_status()["agent_sessions"][OLD]["state"], "running")

    def test_a_young_session_without_a_process_is_not_cancelled_as_never_started(self):
        self.commit(5, [session(OLD, "reviewer", "launching", self.ago(1))], {"reviewer": OLD})
        closed = run(["run-close", "--by", "test-pilot", "--reason", "too soon", "--cancel-active"], cwd=self.tmp)
        self.assertNotEqual(closed.returncode, 0, closed.stdout)
        self.assertIn("cannot prove process ownership", closed.stdout + closed.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()
