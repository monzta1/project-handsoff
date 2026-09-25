"""Focused regression coverage for the REQ-001 and REQ-002 review hold."""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handsoff_supervisor import HandsoffTestCase, run

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_dashboard  # noqa: E402
import handsoff_lib  # noqa: E402
from tests.fixture_state import write_version_pin


class TestDesignReviewHold(HandsoffTestCase):
    """The exhausted budget must remain an explicit authorization hold."""

    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)

    def _exhaust(self):
        self.init("Design review hold")
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "A criterion"], self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "10"], self.tmp).returncode, 0)
        # #320: derived from the constant, not the number 2, so raising
        # the default again does not silently stop exhausting the budget
        # and leave these tests asserting against a run that is not held.
        for number in range(1, handsoff_lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS + 1):
            result = run(["record-design-review", "--by", f"reviewer-{number}",
                          "--architect", "architect-1", "--request-changes",
                          "--summary", f"Changes {number}"], self.tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_exhaustion_records_hold_and_exact_message(self):
        self._exhaust()
        status = self.read_status()
        self.assertEqual(status["authorization_hold"], "design_review")
        self.assertEqual(status["status"], "blocked")
        limit = handsoff_lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS
        self.assertEqual(status["next_action"], f"design review budget exhausted ({limit}/{limit}); "
                         "Pilot must run handsoff_supervisor.py design-review-authorize --by <pilot> "
                         "to permit one more attempt")

    def test_revised_proposal_stays_held_and_dashboard_hold_is_not_required(self):
        self._exhaust()
        cfg = handsoff_lib.load_config(self.tmp)
        session = handsoff_lib.create_agent_session(
            self.tmp, role="architect", actor="architect-live", adapter="codex",
            requested_model="default", resolution_source="configured")
        proposal = {field: (["item"] if field in {"approach", "decisions", "verification"} else [])
                    for field in handsoff_lib.DESIGN_PROPOSAL_FIELDS}
        proposal["summary"] = "Revised proposal"
        handsoff_lib.record_design_proposal(self.tmp, session["session_id"], proposal)
        status = self.read_status()
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["authorization_hold"], "design_review")
        self.assertTrue(status["next_action"].startswith("Revised design proposal recorded;"))
        status.pop("authorization_hold")
        request = handsoff_dashboard._input_request(status, cfg)
        self.assertEqual(request["kind"], "design_review_budget")

    def test_authorization_clears_hold_and_controls_managed_handoff(self):
        self._exhaust()
        result = run(["design-review-authorize", "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        cfg = handsoff_lib.load_config(self.tmp)
        self.assertNotIn("authorization_hold", status)
        self.assertEqual(status["status"], "in_progress")
        architect = handsoff_lib.create_agent_session(
            self.tmp, role="architect", actor="architect-live", adapter="codex",
            requested_model="default", resolution_source="configured")
        completed = self.read_status()
        completed["agent_sessions"][architect["session_id"]]["state"] = "completed"
        completed["agent_sessions"][architect["session_id"]]["running_at"] = completed["agent_sessions"][architect["session_id"]]["started_at"]
        completed["agent_sessions"][architect["session_id"]]["ended_at"] = datetime.now(timezone.utc).isoformat()
        completed["current_agent_sessions"].pop("architect", None)
        completed["design_proposal"] = {"based_on_review_attempt": completed["design_review_attempts"]}
        handsoff_lib.commit(self.tmp, cfg, status=completed, event_kind="test_session_completed",
                            event_message="test session completed")
        status = self.read_status()
        self.assertEqual(handsoff_lib.managed_handoff_role(status, cfg), "reviewer")
        session = handsoff_lib.create_agent_session(
            self.tmp, role="reviewer", actor="reviewer-live", adapter="codex",
            requested_model="default", resolution_source="configured")
        self.assertEqual(handsoff_lib.managed_handoff_role(self.read_status(), cfg), None)
        self.assertEqual(session["role"], "reviewer")

    def test_approved_last_attempt_requests_design_approval_not_budget(self):
        """REQ-002: approval is the remaining human gate after budget exhaustion."""
        self._exhaust()
        self.assertEqual(run(["design-review-authorize", "--by", "pilot"], self.tmp).returncode, 0)
        approved = run(["record-design-review", "--by", "reviewer-final", "--architect", "architect-1",
                        "--approve", "--summary", "Final approved design"], self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        status = self.read_status()
        cfg = handsoff_lib.load_config(self.tmp)
        request = handsoff_dashboard._input_request(status, cfg)
        self.assertEqual(request["kind"], "design_approval")
        self.assertNotEqual(request["kind"], "design_review_budget")

    def test_authorization_is_refused_after_close(self):
        self._exhaust()
        cfg = handsoff_lib.load_config(self.tmp)
        handsoff_lib.close_run(self.tmp, by="pilot", reason="closed for test")
        result = run(["design-review-authorize", "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "SHIP_FEATURE_BLOCKED: run is closed; reopen it before authorizing a design review")
        self.assertIsNone(handsoff_lib.managed_handoff_role(self.read_status(), cfg))
