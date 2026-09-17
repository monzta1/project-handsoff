#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-recovery-"))
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        result = subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root),
             "init", "Recovery #30"], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def read_status(self):
        return json.loads((self.root / "handsoff-status.json").read_text())

    def commit_status(self, status, kind="test_setup"):
        cfg = lib.load_config(self.root)
        with lib.project_lock(self.root):
            lib.commit(self.root, cfg, status=status, event_kind=kind, event_message=kind)

    def session(self, sid, role, state, when):
        terminal = state not in lib.AGENT_SESSION_LIVE_STATES
        return {
            "session_id": sid, "role": role, "actor": f"codex-{role}", "adapter": "codex",
            "requested_model": "default", "reported_model": None,
            "resolution_source": "configured", "started_at": when,
            "running_at": when, "ended_at": when if terminal else None,
            "state": state, "exit_code": 1 if terminal else None,
        }

    def test_assigned_worker_cannot_be_masked_by_unrelated_activity(self):
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(minutes=30)).isoformat()
        fresh = now.isoformat()
        assigned = "hs-" + "1" * 32
        unrelated = "hs-" + "2" * 32
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], updated_at=fresh,
                      last_heartbeat_at=fresh,
                      agent_sessions={assigned: self.session(assigned, "implementer", "running", stale),
                                      unrelated: self.session(unrelated, "reviewer", "running", fresh)},
                      current_agent_sessions={"implementer": assigned, "reviewer": unrelated})
        cfg = lib.load_config(self.root)
        assessment = lib.recovery_assessment(status, cfg, {assigned: stale, unrelated: fresh}, [], now)
        self.assertEqual(assessment["state"], "worker_silent")
        status["agent_sessions"][assigned] = self.session(assigned, "implementer", "failed", stale)
        assessment = lib.recovery_assessment(status, cfg, {unrelated: fresh}, [], now)
        self.assertEqual(assessment["state"], "worker_terminal")

    def test_managed_completion_hands_off_but_manual_and_same_role_runs_do_not(self):
        now = datetime.now(timezone.utc)
        earlier = (now - timedelta(minutes=2)).isoformat()
        later = (now - timedelta(minutes=1)).isoformat()
        architect_id = "hs-" + "a" * 32
        reviewer_id = "hs-" + "b" * 32
        status = self.read_status()
        status.update(phase_number=2, phase=lib.PHASES[2], status="in_progress",
                      design_review=None, agent_sessions={}, current_agent_sessions={})
        self.assertIsNone(lib.managed_handoff_role(status))
        architect = self.session(architect_id, "architect", "completed", earlier)
        architect["phase_number"] = 1
        status["agent_sessions"] = {architect_id: architect}
        status["current_agent_sessions"] = {"architect": architect_id}
        self.assertEqual(lib.managed_handoff_role(status), "reviewer")
        reviewer = self.session(reviewer_id, "reviewer", "completed", later)
        reviewer["phase_number"] = 2
        status["agent_sessions"][reviewer_id] = reviewer
        status["current_agent_sessions"]["reviewer"] = reviewer_id
        status["design_review"] = {"decision": "changes_requested"}
        self.assertEqual(lib.managed_handoff_role(status), "architect")
        architect["phase_number"] = 2
        architect["ended_at"] = now.isoformat()
        self.assertIsNone(lib.managed_handoff_role(status))

    def test_watchdog_does_not_retry_nonrecoverable_budget_exhaustion(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        sid = "hs-" + "c" * 32
        status = self.read_status()
        failed = self.session(sid, "implementer", "failed", old)
        failed["phase_number"] = 4
        status.update(phase_number=4, phase=lib.PHASES[4], status="in_progress",
                      agent_sessions={sid: failed}, current_agent_sessions={"implementer": sid},
                      agent_failures={sid: {"category": "token_budget_exhaustion",
                                            "reason": "managed role exhausted its token budget",
                                            "tail_sha256": "0" * 64, "at": old}})
        assessment = lib.recovery_assessment(status, lib.load_config(self.root), {}, [], now)
        self.assertEqual((assessment["state"], assessment["reason"]),
                         ("not_applicable", "non_recoverable_failure"))

    def test_approved_design_advances_without_a_supervisor_agent(self):
        cfg = lib.load_config(self.root)
        status = self.read_status()
        acceptance = lib.load_unique_json(lib.acceptance_path(self.root, cfg))
        digest = lib.design_hash(acceptance["criteria"])
        scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0])
        decision = {
            "at": datetime.now(timezone.utc).isoformat(),
            "architect": "codex-architect", "design_hash": digest,
            "config_hash": lib.config_hash(cfg), "scope_hash": scope,
            "summary": "Focused approved design.",
        }
        status.update(
            phase_number=2, phase=lib.PHASES[2], progress=20, status="in_progress",
            design_review={**decision, "by": "codex-reviewer", "decision": "approved"},
            design_approved={**decision, "by": "Mission Control Pilot"},
        )
        self.commit_status(status)
        self.assertIsNone(lib.assigned_role(self.read_status()))
        self.assertTrue(supervisor.advance_approved_design(self.root))
        advanced = self.read_status()
        self.assertEqual((advanced["phase_number"], advanced["status"]),
                         (3, "in_progress"))
        self.assertGreaterEqual(advanced["progress"], status["progress"])
        self.assertFalse(supervisor.advance_approved_design(self.root))

    def test_recovery_attempt_numbers_restart_for_each_episode(self):
        status = self.read_status()
        now = datetime.now(timezone.utc).isoformat()
        def attempt(rid, source, target):
            return {
                "recovery_id": rid, "role": "supervisor", "trigger": "worker_terminal",
                "from_session_id": source, "to_session_id": target,
                "attempt": 1, "cap": 3, "holder": "watchdog", "state": "failed",
                "reason": "HandsoffError", "at": now, "launched_at": now,
                "ended_at": now,
            }
        status["recovery_attempts"] = [
            attempt("hv-" + "1" * 32, "hs-" + "1" * 32, "hs-" + "2" * 32),
            attempt("hv-" + "2" * 32, "hs-" + "3" * 32, "hs-" + "4" * 32),
        ]
        errors = lib.validate_status_schema(status)
        self.assertFalse([error for error in errors if "recovery_attempts" in error], errors)

    def test_terminal_role_from_prior_phase_does_not_block_new_assignment(self):
        now = datetime.now(timezone.utc)
        reviewer_id = "hs-" + "5" * 32
        old_supervisor_id = "hs-" + "6" * 32
        status = self.read_status()
        reviewer = self.session(
            reviewer_id, "reviewer", "completed",
            (now - timedelta(minutes=2)).isoformat(),
        )
        reviewer["phase_number"] = 2
        old_supervisor = self.session(
            old_supervisor_id, "supervisor", "failed",
            (now - timedelta(minutes=1)).isoformat(),
        )
        old_supervisor["phase_number"] = 2
        status.update(
            phase_number=3, phase=lib.PHASES[3], status="in_progress",
            agent_sessions={reviewer_id: reviewer, old_supervisor_id: old_supervisor},
            current_agent_sessions={
                "reviewer": reviewer_id, "supervisor": old_supervisor_id,
            },
        )
        self.assertEqual(lib.managed_handoff_role(status), "supervisor")

    def test_exhausted_design_review_budget_does_not_spin_handoff(self):
        status = self.read_status()
        now = datetime.now(timezone.utc).isoformat()
        architect_id = "hs-" + "7" * 32
        architect = self.session(architect_id, "architect", "completed", now)
        architect["phase_number"] = 2
        status.update(
            phase_number=2, phase=lib.PHASES[2], status="in_progress",
            design_review_attempts=2, design_review=None,
            design_proposal={"based_on_review_attempt": 2},
            agent_sessions={architect_id: architect},
            current_agent_sessions={"architect": architect_id},
        )
        self.assertIsNone(lib.managed_handoff_role(status, lib.load_config(self.root)))

    def test_bounded_architect_proposal_hands_revision_to_reviewer(self):
        now = datetime.now(timezone.utc)
        sid = "hs-" + "d" * 32
        status = self.read_status()
        status.update(phase_number=2, phase=lib.PHASES[2], status="in_progress",
                      design_review_attempts=1,
                      design_review={"decision": "changes_requested", "findings": [
                          {"id": "F1.1", "text": "Specify the timeout boundary"}]},
                      agent_sessions={sid: self.session(sid, "architect", "running", now.isoformat())},
                      current_agent_sessions={"architect": sid})
        self.commit_status(status)
        proposal = lib.record_design_proposal(self.root, sid, {
            "summary": "Bound external calls and surface safe telemetry.",
            "approach": ["Wrap each external operation in one bounded lifecycle."],
            "tradeoffs": ["Timeouts favor bounded completion over indefinite compatibility waits."],
            "decisions": ["Persist only content-free operation metadata."],
            "constraints": ["Preserve existing successful caller behavior."],
            "verification": ["Run focused timeout and dashboard state tests."],
        })
        final = self.read_status()
        self.assertEqual(final["design_proposal"]["proposal_hash"], proposal["proposal_hash"])
        self.assertIsNone(final["design_review"])
        self.assertEqual(lib.assigned_role(final), "reviewer")
        context = lib.managed_design_context(self.root, "reviewer")
        self.assertEqual(context["design_proposal"]["proposal_hash"], proposal["proposal_hash"])
        self.assertEqual(context["criteria"][0]["id"], "REQ-001")

    def test_liveness_updates_are_locked_and_atomic(self):
        first = "hs-" + "3" * 32
        second = "hs-" + "4" * 32
        threads = [threading.Thread(target=lib.update_session_liveness, args=(self.root, sid))
                   for sid in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        mapping = lib.read_session_liveness(self.root)
        self.assertEqual(set(mapping), {first, second})
        self.assertFalse(any(path.name.startswith("..handsoff-session-liveness")
                             for path in self.root.iterdir()))

    def test_stale_lease_closes_before_a_new_bounded_recovery(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40, updated_at=old,
                      last_heartbeat_at=None)
        expired_id = "hv-" + "5" * 32
        status["recovery_attempts"] = [{
            "recovery_id": expired_id, "role": "implementer", "trigger": "silent_run",
            "from_session_id": None, "to_session_id": None, "attempt": 1, "cap": 3,
            "holder": "old-watchdog", "state": "reserved", "reason": "stale",
            "at": old, "launched_at": None, "ended_at": None,
        }]
        status["recovery_lease"] = {
            "lease_id": "hl-" + "6" * 32, "holder": "old-watchdog", "acquired_at": old,
            "expires_at": old, "recovery_id": expired_id,
        }
        self.commit_status(status)
        result = lib.recover_run(self.root, actor="watchdog", launcher=lambda role: 0, now=now)
        self.assertEqual(result["action"], "recovered")
        final = self.read_status()
        self.assertEqual([item["state"] for item in final["recovery_attempts"]], ["failed", "recovered"])
        self.assertEqual(final["recovery_attempts"][0]["reason"], "lease_expired")
        self.assertIsNone(final["recovery_lease"])
        self.assertEqual(lib.verify_event_log(self.root, lib.load_config(self.root)), [])

    def test_exhaustion_blocks_with_visible_escalation(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40, updated_at=old,
                      last_heartbeat_at=None)
        attempts = []
        for index in range(3):
            attempts.append({
                "recovery_id": "hv-" + str(index + 1) * 32, "role": "implementer",
                "trigger": "silent_run", "from_session_id": None, "to_session_id": None,
                "attempt": index + 1, "cap": 3, "holder": "watchdog", "state": "failed",
                "reason": "failed", "at": old, "launched_at": old, "ended_at": old,
            })
        status["recovery_attempts"] = attempts
        self.commit_status(status)
        result = lib.recover_run(self.root, actor="watchdog", launcher=lambda role: 0, now=now)
        self.assertEqual(result["action"], "escalated")
        final = self.read_status()
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["escalation"]["kind"], "recovery_exhausted")


if __name__ == "__main__":
    unittest.main()


class BeaconProcessAliveTests(RecoveryTests):
    """#86: a stale liveness timestamp never presumes a session lost while
    the live beacon names it and its pid exists."""

    def _silent_run(self):
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(minutes=30)).isoformat()
        sid = "hs-" + "3" * 32
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], updated_at=now.isoformat(),
                      last_heartbeat_at=now.isoformat(),
                      agent_sessions={sid: self.session(sid, "implementer", "running", stale)},
                      current_agent_sessions={"implementer": sid})
        return status, sid, stale, now

    def test_live_beacon_pid_keeps_a_silent_session(self):
        status, sid, stale, now = self._silent_run()
        lib.write_live_beacon(self.root, session_id=sid, role="implementer", state="running", pid=4242)
        with mock.patch("os.kill", return_value=None) as kill:
            assessment = lib.recovery_assessment(status, lib.load_config(self.root), {sid: stale}, [], now,
                                                 root=self.root)
        self.assertEqual((assessment["state"], assessment["reason"]), ("active", "process_alive"))
        self.assertEqual(kill.call_args.args, (4242, 0))
        self.commit_status(status)
        with mock.patch("os.kill", return_value=None):
            outcome = lib.recover_run(self.root, actor="watchdog", launcher=lambda role: 0, now=now)
        self.assertNotEqual(outcome.get("action"), "launched", outcome)
        self.assertEqual(self.read_status()["agent_sessions"][sid]["state"], "running")

    def test_dead_beacon_pid_is_still_lost(self):
        status, sid, stale, now = self._silent_run()
        lib.write_live_beacon(self.root, session_id=sid, role="implementer", state="running", pid=4242)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            assessment = lib.recovery_assessment(status, lib.load_config(self.root), {sid: stale}, [], now,
                                                 root=self.root)
        self.assertEqual(assessment["state"], "worker_silent")

    def test_beacon_for_another_session_does_not_vouch(self):
        status, sid, stale, now = self._silent_run()
        lib.write_live_beacon(self.root, session_id="hs-" + "4" * 32, role="reviewer", state="running", pid=4242)
        with mock.patch("os.kill", return_value=None):
            assessment = lib.recovery_assessment(status, lib.load_config(self.root), {sid: stale}, [], now,
                                                 root=self.root)
        self.assertEqual(assessment["state"], "worker_silent")

    def test_fresh_timestamp_is_active_without_a_beacon(self):
        status, sid, stale, now = self._silent_run()
        assessment = lib.recovery_assessment(status, lib.load_config(self.root), {sid: now.isoformat()}, [], now,
                                             root=self.root)
        self.assertEqual(assessment["state"], "active")
