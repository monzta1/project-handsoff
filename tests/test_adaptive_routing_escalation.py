"""REQ-004/REQ-005: evidence-first escalation and bounded convergence."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class AdaptiveEscalationTests(unittest.TestCase):
    def binding(self):
        return {"mission_id": "mission-1", "acceptance_hash": "acceptance-1"}

    def test_checks_are_applicable_ordered_and_bound_before_escalation(self):
        checks = lib.validate_adaptive_check_plan([
            {"check_id": "z", "command": "check-z", "applies_to": "review", "order": 2},
            {"check_id": "a", "command": "check-a", "applies_to": "all", "order": 1},
        ], **self.binding())
        self.assertEqual([check["check_id"] for check in checks], ["a", "z"])
        result = lib.record_adaptive_check(checks[0], outcome="fail", evidence=["ev-1"], detail="reproduced")
        self.assertEqual(result["record_type"], "deterministic_check")
        self.assertEqual(result["mission_id"], "mission-1")

    def test_claims_evidence_and_question_are_separate_mission_bound_records(self):
        records = lib.adaptive_escalation_records(
            **self.binding(),
            implementer={"claim_id": "ic-1", "actor": "impl", "decision": "repair", "summary": "patch is incomplete"},
            reviewer={"claim_id": "rc-1", "actor": "review", "decision": "reject", "summary": "check still fails"},
            supporting_evidence=[{"evidence_id": "ev-1", "kind": "test", "detail": "failure output"}],
            unresolved_question={"question_id": "q-1", "question": "Should the migration be retried?"},
        )
        self.assertEqual([record["record_type"] for record in records],
                         ["implementer_claim", "reviewer_claim", "supporting_evidence", "unresolved_question"])
        self.assertTrue(all(record["mission_id"] == "mission-1" and record["acceptance_hash"] == "acceptance-1"
                             for record in records))

    def test_limits_end_disagreement_or_repair_with_human_pause(self):
        disagreement = lib.bound_adaptive_escalation(disagreement_rounds=2, repair_rounds=0)
        self.assertEqual((disagreement["state"], disagreement["outcome"], disagreement["reason"]),
                         ("terminal", "human_pause", "disagreement_limit"))
        repair = lib.bound_adaptive_escalation(disagreement_rounds=0, repair_rounds=2)
        self.assertEqual(repair["reason"], "repair_limit")
        self.assertEqual(lib.bound_adaptive_escalation(human_decision="accepted")["outcome"], "accepted")

    def test_status_schema_closes_the_adaptive_escalation_contract(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "status.schema.json"
        escalation = json.loads(schema_path.read_text())["properties"]["adaptive_escalation"]
        self.assertIs(escalation["additionalProperties"], False)
        self.assertEqual(set(escalation["required"]), set(lib.bound_adaptive_escalation()))
        self.assertEqual(escalation["properties"]["max_repair_rounds"]["minimum"], 1)

    def test_invalid_cross_mission_or_unbounded_records_are_refused(self):
        with self.assertRaises(lib.HandsoffError):
            lib.validate_adaptive_check_plan([{"check_id": "x", "command": "true"}],
                                             mission_id="", acceptance_hash="hash")
        with self.assertRaises(lib.HandsoffError):
            lib.adaptive_escalation_records(
                **self.binding(),
                implementer={"claim_id": "i", "actor": "a", "decision": "accept", "summary": "ok"},
                reviewer={"claim_id": "r", "actor": "b", "decision": "accept", "summary": "ok"},
                unresolved_question={"question_id": "q", "question": "?", "state": "unknown"},
            )


if __name__ == "__main__":
    unittest.main()
