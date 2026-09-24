"""#296: a run reports success only once the installed artifact is verified.

On 2026-09-23 the v0.3.80 release was published and issues #281, #282,
#283, #286 and #287 were closed by hand while the Handsoff run was still at
Phase 7. The released wheel was never installed and `verify-live` never
ran. Nothing refused, and nothing on the ledger recorded that the claim and
the evidence had come apart.

A run may always be abandoned. It may not call an abandonment a delivery.
"""
import shutil
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class VerifiedPhase8IsEvidenceNotAClaim(unittest.TestCase):
    """`phase_number` and `progress` are what the run says about itself;
    `live_verification_id` is the evidence behind it."""

    def test_a_verified_run_at_phase_eight_is_verified(self):
        self.assertTrue(lib.run_is_live_verified(
            {"phase_number": 8, "progress": 100, "live_verification_id": "ver-1"}))

    def test_the_v0_3_80_shape_is_not_verified(self):
        """Published at Phase 7, nothing installed, nothing verified."""
        self.assertFalse(lib.run_is_live_verified(
            {"phase_number": 7, "progress": 70, "release_published": True}))

    def test_phase_eight_without_a_verification_id_is_not_verified(self):
        self.assertFalse(lib.run_is_live_verified({"phase_number": 8, "progress": 100}))

    def test_a_verification_id_without_phase_eight_is_not_verified(self):
        self.assertFalse(lib.run_is_live_verified(
            {"phase_number": 7, "progress": 70, "live_verification_id": "ver-1"}))

    def test_progress_short_of_one_hundred_is_not_verified(self):
        self.assertFalse(lib.run_is_live_verified(
            {"phase_number": 8, "progress": 95, "live_verification_id": "ver-1"}))

    def test_a_project_that_waives_live_verification_still_needs_phase_eight(self):
        cfg = {"require_live_verification": False}
        self.assertTrue(lib.run_is_live_verified({"phase_number": 8, "progress": 100}, cfg))
        self.assertFalse(lib.run_is_live_verified({"phase_number": 7, "progress": 70}, cfg))

    def test_junk_input_is_not_verified(self):
        for value in (None, {}, "complete", {"phase_number": "eight", "progress": "100"}):
            self.assertFalse(lib.run_is_live_verified(value))


class AnEarlyCloseNamesWhatItIs(unittest.TestCase):
    """REQ-006: aborted or released_unverified, never a clean close."""

    def test_the_outcomes_exist(self):
        for outcome in ("aborted", "released_unverified"):
            self.assertIn(outcome, lib.RUN_OUTCOMES)

    def test_the_legacy_outcomes_are_preserved(self):
        for outcome in ("closed", "not_planned"):
            self.assertIn(outcome, lib.RUN_OUTCOMES)

    def test_complete_is_not_an_outcome_a_close_can_record(self):
        self.assertNotIn("complete", lib.RUN_OUTCOMES)

    def test_a_run_that_published_nothing_is_aborted(self):
        self.assertEqual(lib.unverified_close_outcome({"phase_number": 4}), "aborted")

    def test_a_run_that_published_a_release_is_released_unverified(self):
        self.assertEqual(
            lib.unverified_close_outcome({"phase_number": 7, "release_published": True}),
            "released_unverified")

    def test_deployment_approval_counts_as_published(self):
        """The v0.3.80 run had an approved deployment and a published
        release while sitting at Phase 7."""
        self.assertEqual(
            lib.unverified_close_outcome({"phase_number": 7, "deployment_approved": {"by": "moncy"}}),
            "released_unverified")

    def test_the_unverified_outcomes_are_listed_together(self):
        self.assertEqual(set(lib.UNVERIFIED_RUN_OUTCOMES), {"aborted", "released_unverified"})


class PostingSuccessRequiresVerifiedPhase8(HandsoffTestCase):
    """REQ-006, the regression: publication at Phase 7 followed by an
    attempted successful close."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")

    def _phase7_published(self):
        self.init("Published but unverified")
        status = lib.load_unique_json(self.tmp / "handsoff-status.json")
        status["phase_number"] = 7
        status["progress"] = 70
        status["release_published"] = True
        return status

    def test_run_close_post_refuses_before_verified_phase_eight(self):
        status = self._phase7_published()
        cfg = {"require_live_verification": True}
        self.assertFalse(lib.run_is_live_verified(status, cfg))
        # The refusal message must name the phase, the evidence and the repair.
        outcome = lib.unverified_close_outcome(status)
        self.assertEqual(outcome, "released_unverified")

    def test_the_close_that_is_permitted_is_the_honest_one(self):
        status = self._phase7_published()
        self.assertEqual(lib.unverified_close_outcome(status), "released_unverified")
        self.assertIn(lib.unverified_close_outcome(status), lib.UNVERIFIED_RUN_OUTCOMES)

    def test_a_verified_run_closes_cleanly(self):
        self.init("Verified")
        status = lib.load_unique_json(self.tmp / "handsoff-status.json")
        status.update({"phase_number": 8, "progress": 100, "live_verification_id": "ver-1"})
        self.assertTrue(lib.run_is_live_verified(status))

    def test_close_run_refuses_an_outcome_outside_the_set(self):
        self.init("Outcome set")
        with self.assertRaisesRegex(lib.HandsoffError, "run outcome must be one of"):
            lib.close_run(self.tmp, by="moncy", reason="bad outcome", outcome="complete")


class WorkItemsStayOpenWhenVerificationIsIncomplete(HandsoffTestCase):
    """REQ-006: closing the issues is the claim that the work landed."""

    def test_an_unverified_run_has_no_business_closing_its_issues(self):
        self.init("Unverified")
        status = lib.load_unique_json(self.tmp / "handsoff-status.json")
        status.update({"phase_number": 7, "progress": 70, "release_published": True})
        self.assertFalse(lib.run_is_live_verified(status),
                         "issue closure is gated on this being true")


if __name__ == "__main__":
    unittest.main()
