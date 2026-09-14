#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


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
