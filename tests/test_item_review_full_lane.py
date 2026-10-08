"""#360: record-review --item on a full-lane item before Phase 5 is advice only."""
import contextlib
import io
import json
import sys
import unittest

import tests.test_handsoff_supervisor as base
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import seed_placeholder

sys.path.insert(0, str(BIN))
import handsoff_lib as lib
import handsoff_supervisor as supervisor


class ItemReviewFullLaneTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        r = run(["init", "Full lane item review", "--item", "#47"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        seed_placeholder(self.tmp)  # #418
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(4, implemented_by="impl-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        self.assertEqual(self.read_status()["work_item_delivery"]["issue-47"]["lane"], "full")

    def events(self):
        path = lib.event_log_path(self.tmp, lib.load_config(self.tmp))
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_advisory_event_names_reviewer_item_and_hash_and_nothing_else(self):  # REQ-005
        before_status, before_events = self.read_status(), self.events()
        r = run(["record-review", "--item", "issue-47", "--by", "rev-1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ADVISORY_ITEM_REVIEW_RECORDED: issue-47", r.stdout)
        added = self.events()[len(before_events):]
        self.assertEqual([e["kind"] for e in added], ["work_item_review_advisory"])
        event = added[0]
        self.assertEqual(event["reviewer"], "rev-1")
        self.assertEqual(event["item_id"], "issue-47")
        self.assertEqual(event["acceptance_hash"],
                         lib.item_acceptance_hash(self.read_acceptance(), "issue-47"))
        after = self.read_status()
        record = after["work_item_delivery"]["issue-47"]
        self.assertEqual(record, before_status["work_item_delivery"]["issue-47"])
        self.assertEqual((record["lane"], record["confirmed_by"], record["reviewed_by"]),
                         ("full", None, before_status["work_item_delivery"]["issue-47"]["reviewed_by"]))
        self.assertNotIn("work_item_review_approved", [e["kind"] for e in self.events()])
        self.assertNotIn("work_item_lane_confirmed", [e["kind"] for e in added])

    def test_advisory_reviews_never_count_as_the_phase_5_review(self):  # REQ-006
        before = self.read_status()
        for reviewer in ("rev-1", "rev-2", "rev-3"):
            r = run(["record-review", "--item", "issue-47", "--by", reviewer], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.read_status()
        self.assertEqual(after.get("review_round"), before.get("review_round"))
        self.assertEqual(after.get("review_attempts"), before.get("review_attempts"))
        self.assertIsNone(after.get("review"))
        cfg = lib.load_config(self.tmp)
        verifications, problems = lib.load_verifications(self.tmp, cfg)
        proposed = dict(after, phase_number=6, phase=lib.PHASES[6])
        errors = lib.compute_errors(proposed, self.read_acceptance(), cfg, verifications=verifications,
                                    verification_problems=problems, root=self.tmp)
        self.assertIn("review gate: Phase 6+ requires a recorded independent review", errors)

    def test_implementer_as_reviewer_is_refused_naming_both(self):  # REQ-007
        before = self.events()
        r = run(["record-review", "--item", "issue-47", "--by", "IMPL-1"], cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reviewer IMPL-1 must differ from implementer impl-1", r.stdout)
        self.assertEqual(len(self.events()), len(before))

    def test_run_implementer_is_used_when_the_item_has_none(self):  # REQ-007
        status, acceptance = self.read_status(), self.read_acceptance()
        record = dict(status["work_item_delivery"]["issue-47"], implemented_by=None)
        status["implemented_by"] = "run-impl"
        before, out = self.events(), io.StringIO()
        with contextlib.redirect_stdout(out):
            code = supervisor._record_item_review_advisory(
                self.tmp, lib.load_config(self.tmp), status, acceptance, record, "issue-47", "run-impl")
        self.assertEqual(code, 1)
        self.assertIn("reviewer run-impl must differ from implementer run-impl", out.getvalue())
        self.assertEqual(len(self.events()), len(before))



class SmallFixItemReviewUnchanged(HandsoffTestCase):
    # REQ-007: the small-fix --item path keeps its behaviour and its test.
    test_small_fix_item_review_unchanged = \
        base.TestSmallFixLane.test_small_fix_keeps_implementation_evidence_review_and_audit_gates


if __name__ == "__main__":
    unittest.main()
