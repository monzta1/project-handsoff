"""#172: a failed session whose verdict was adopted, or whose run has since
advanced, never outranks the ledger: Mission Control reads it as stopped
and Fleet never says FAILED for it."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class FailedSessionSupersededTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init()

    def _failed_reviewer(self, phase=4):
        status = self.read_status()
        status["phase_number"], status["phase"] = phase, lib.PHASES[phase]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_phase",
                   event_message="fixture", actor="test")
        session = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        sid = session["session_id"]
        lib.transition_agent_session(self.tmp, sid, "running")
        lib.record_session_result(self.tmp, sid, "review", {
            "kind": "implementation", "decision": "approved", "summary": "fine", "findings": [],
            "structural_blocker": False, "symptom_reproduced": "not_applicable", "tests_executed": "yes"})
        lib.transition_agent_session(self.tmp, sid, "failed", exit_code=1, failure={
            "category": "dispatch_failed", "reason": "implementation Reviewer result requires Phase 5",
            "result_available": True, "tail_sha256": "0" * 64})
        lib.write_live_beacon(self.tmp, session_id=sid, role="reviewer", state="failed", pid=None,
                              ended_at="2026-09-20T22:11:43+00:00", exit_code=1)
        return sid

    def _states(self):
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        live = lib.live_status(status, cfg, self.tmp)
        fleet.register_project(self.tmp, self.registry)
        card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        return live, card

    def test_a_genuinely_failed_session_still_reads_failed(self):
        self._failed_reviewer()
        live, card = self._states()
        self.assertEqual(live["state"], "failed")
        self.assertEqual(card["state"], "failed")

    def test_an_adopted_verdict_reads_stopped_and_fleet_never_says_failed(self):
        sid = self._failed_reviewer()
        status = self.read_status()
        status["agent_sessions"][sid]["result"]["adopted_at"] = "2026-09-20T22:12:25+00:00"
        status["agent_sessions"][sid]["result"]["adopted_by"] = "claude-supervisor"
        status.setdefault("agent_failures", {})
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_adopt",
                   event_message="fixture", actor="test")
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("verdict adopted at 2026-09-20T22:12:25+00:00", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        self.assertIn(card["state"], {"running", "quiet", "waiting", "idle"})

    def test_an_adopted_failure_with_a_later_status_update_never_reads_failed(self):
        sid = self._failed_reviewer()
        status = self.read_status()
        status["agent_sessions"][sid]["result"] = {}
        status["agent_failures"][sid]["adopted"] = True
        status["agent_sessions"][sid]["ended_at"] = "2026-09-20T22:11:43+00:00"
        status["updated_at"] = "2026-09-20T22:12:31+00:00"
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_adopt",
                   event_message="fixture", actor="test")
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("status updated at 2026-09-20T22:12:31+00:00", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        self.assertIn(card["state"], {"running", "quiet", "waiting", "idle"})

    def test_a_run_that_advanced_past_the_session_reads_stopped(self):
        sid = self._failed_reviewer(phase=4)
        status = self.read_status()
        status["phase_number"], status["phase"] = 6, lib.PHASES[6]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_phase",
                   event_message="fixture", actor="test")
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("session ran at phase 4; the run advanced to phase 6", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        self.assertEqual(self.read_status()["agent_sessions"][sid]["state"], "failed", "the ledger keeps the failure")

    def test_adoption_rewrites_the_beacon_as_adopted_and_leaves_another_session_alone(self):
        sid = self._failed_reviewer(phase=5)
        self.assertEqual(lib.read_live_beacon(self.tmp)["state"], "failed")
        self.assertFalse(lib.mark_beacon_adopted(self.tmp, "hs-" + "0" * 32), "another session's beacon is not touched")
        self.assertEqual(lib.read_live_beacon(self.tmp)["state"], "failed")
        self.assertTrue(lib.mark_beacon_adopted(self.tmp, sid))
        beacon = lib.read_live_beacon(self.tmp)
        self.assertEqual((beacon["state"], beacon["session_id"], beacon["exit_code"]), ("adopted", sid, 1))
        source = (BIN / "handsoff_supervisor.py").read_text()
        self.assertIn("lib.mark_beacon_adopted(root, args.session)", source, "session-result-adopt calls it")


if __name__ == "__main__":
    unittest.main()
