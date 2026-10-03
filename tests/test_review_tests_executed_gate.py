"""#341: record-review refuses an approval that did not run the tests while a
criterion's verification policy requires checks."""
from __future__ import annotations

import json
import shutil

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin


class ReviewTestsExecutedGateTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _automated_run_at_phase_5(self):
        self.init("Tests-executed gate fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def _manual_run_at_phase_5(self):
        self.init("Tests-executed gate manual fixture")
        changed = run(["criterion-update", "REQ-001", "--verification", "manual",
                       "--requirement", "A manually inspected fixture criterion"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        evidence = run(["record-evidence", "REQ-001", "--kind", "manual",
                        "--description", "Inspected by hand", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
        records = [json.loads(line) for line in
                   (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines() if line.strip()]
        run_id = next(r["run_id"] for r in reversed(records) if r.get("kind") == "manual")
        symptom = run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(symptom.returncode, 0, symptom.stdout + symptom.stderr)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["verification"], "manual")

    def _review(self, value, *extra):
        return run(["record-review", "--by", "test-reviewer", "--tests-executed", value, *extra], cwd=self.tmp)

    # REQ-001
    def test_no_and_unknown_refused_when_a_criterion_requires_checks(self):
        self._automated_run_at_phase_5()
        before = self.read_status()
        for value in ("no", "unknown"):
            with self.subTest(value=value):
                refused = self._review(value)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn("tests_executed", refused.stdout)
                self.assertIn(f"tests_executed is {value}", refused.stdout)
                self.assertIn("REQ-001", refused.stdout)
                after = self.read_status()
                self.assertIsNone(after["review"])
                self.assertEqual(after["review"], before["review"])
                self.assertEqual(after.get("review_attempts"), before.get("review_attempts"))

    def test_refusal_names_every_criterion_requiring_checks(self):
        self._automated_run_at_phase_5()
        import sys
        sys.path.insert(0, str(BIN))
        import handsoff_workflow
        acceptance = {"criteria": [
            {"id": "REQ-001", "verification": "automated"},
            {"id": "REQ-002", "verification": "manual"},
            {"id": "REQ-003", "verification": "automated_and_mutation"},
            {"id": "REQ-004", "verification": "automated_and_browser"},
            {"id": "REQ-005", "verification": "browser"},
        ]}
        errors = handsoff_workflow.review_tests_executed_errors("unknown", acceptance)
        self.assertEqual(len(errors), 1)
        self.assertIn("tests_executed is unknown", errors[0])
        self.assertIn("REQ-001, REQ-003, REQ-004", errors[0])
        self.assertNotIn("REQ-002", errors[0])
        self.assertNotIn("REQ-005", errors[0])
        self.assertEqual(handsoff_workflow.review_tests_executed_errors("yes", acceptance), [])

    # REQ-002
    def test_yes_accepted_on_a_checks_run_without_waiver(self):
        self._automated_run_at_phase_5()
        self.assertEqual(self._review("no").returncode, 1)
        accepted = self._review("yes")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        review = self.read_status()["review"]
        self.assertEqual(review["tests_executed"], "yes")
        self.assertNotIn("tests_executed_waiver", review)

    def _assert_waived(self, value):
        self._manual_run_at_phase_5()
        accepted = self._review(value)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        review = self.read_status()["review"]
        self.assertEqual(review["tests_executed"], value)
        self.assertIn("no criterion requires checks", review["tests_executed_waiver"])
        self.assertIn(f"tests_executed {value}", review["tests_executed_waiver"])

    def test_no_accepted_with_waiver_when_no_criterion_requires_checks(self):
        self._assert_waived("no")

    def test_unknown_accepted_with_waiver_when_no_criterion_requires_checks(self):
        self._assert_waived("unknown")

    def test_yes_accepted_on_a_manual_run_without_waiver(self):
        self._manual_run_at_phase_5()
        accepted = self._review("yes")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        review = self.read_status()["review"]
        self.assertEqual(review["tests_executed"], "yes")
        self.assertNotIn("tests_executed_waiver", review)
        import sys
        sys.path.insert(0, str(BIN))
        import handsoff_workflow
        self.assertEqual(handsoff_workflow.review_tests_executed_waiver("yes"), {})
        self.assertIn("tests_executed_waiver", handsoff_workflow.review_tests_executed_waiver("no"))

    # REQ-003
    def _evidence_refresh(self):
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.read_status()["review"])

    def _plant_attempt_tests_executed(self, value):
        """Fixture only: an approved attempt recorded before #341 carried
        tests_executed no; plant it through the library commit."""
        import sys
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["review_attempts"][-1]["tests_executed"] = value
        if isinstance(status.get("review"), dict):
            status["review"]["tests_executed"] = value
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="review_attempt_closed",
                       event_message="fixture: pre-#341 approval", by="test-supervisor")

    def test_reaffirm_refuses_rebinding_no_when_a_criterion_requires_checks(self):
        self._automated_run_at_phase_5()
        self.assertEqual(self._review("yes").returncode, 0)
        self._plant_attempt_tests_executed("no")
        self._evidence_refresh()
        before = self.read_status()
        refused = run(["record-review", "--reaffirm", "--by", "test-reviewer",
                       "--tests-executed", "yes"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("tests_executed is no", refused.stdout)
        self.assertIn("REQ-001", refused.stdout)
        after = self.read_status()
        self.assertIsNone(after["review"])
        self.assertEqual(after["review_attempts"], before["review_attempts"])
        # the same reaffirm re-binding yes goes through
        self._plant_attempt_tests_executed("yes")
        reaffirmed = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(reaffirmed.returncode, 0, reaffirmed.stdout + reaffirmed.stderr)
        self.assertEqual(self.read_status()["review"]["tests_executed"], "yes")

    def test_validate_passes_on_a_recorded_review_carrying_no(self):
        self._automated_run_at_phase_5()
        # enforced when recorded ...
        self.assertEqual(self._review("no").returncode, 1)
        self.assertEqual(self._review("yes").returncode, 0)
        # ... never retroactively
        self._plant_attempt_tests_executed("no")
        self.assertEqual(self.read_status()["review"]["tests_executed"], "no")
        valid = run(["validate"], cwd=self.tmp)
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        advanced = self.advance_to(6, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    # REQ-004
    def test_reference_and_help_state_the_rule(self):
        import sys
        sys.path.insert(0, str(BIN))
        import handsoff_workflow
        rule = handsoff_workflow.REVIEW_TESTS_EXECUTED_RULE
        for phrase in ("yes: the reviewer ran the tests; always accepted",
                       "no: refused while any criterion requires checks",
                       "unknown is treated as no when a criterion requires checks"):
            self.assertIn(phrase, rule)
        reference = (BIN.parent / "docs" / "REFERENCE.md").read_text()
        self.assertIn(rule, reference)
        self.assertEqual(reference.count("unknown is treated as no when a criterion requires checks"), 1)
        help_text = run(["record-review", "--help"], cwd=self.tmp)
        self.assertEqual(help_text.returncode, 0, help_text.stdout + help_text.stderr)
        flattened = " ".join(help_text.stdout.split())
        self.assertIn("--tests-executed", flattened)
        self.assertIn("unknown is treated as no when a criterion requires checks", flattened)
        self.assertIn("no: refused while any criterion requires checks", flattened)


if __name__ == "__main__":
    import unittest
    unittest.main()
