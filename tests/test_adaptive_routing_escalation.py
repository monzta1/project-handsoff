"""REQ-004/REQ-005: evidence-first escalation and bounded convergence."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib
from tests.fixture_state import force_acceptance


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


class ReviewAttemptAdaptiveRecordTests(unittest.TestCase):
    """#387: a closed review attempt on a run with a risk_class carries the
    deterministic check records and the claim, evidence and question records,
    all bound to the acceptance hash, on its review_attempt_closed event."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-adaptive-records-"))
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        result = self.run_cli("init", "Adaptive records #387", "--risk-class", "elevated")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.cfg = lib.load_config(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=30,
        )

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def prepare(self, criteria, *, risk_class="elevated", results=None):
        """Place the registry, optionally one checks run, and a Phase 4 status
        anchored to the verification ledger's head. Returns the run or None."""
        criteria[0]["type"] = "primary_fix"
        acceptance = self.read("handsoff-acceptance.json")
        acceptance["criteria"] = criteria
        force_acceptance(self.root, self.cfg, acceptance, anchor=True)
        run = None
        if results is not None:
            with lib.project_lock(self.root):
                run = lib.append_verification(self.root, self.cfg, kind="checks", ok=False, by="tester",
                                              criteria=criteria, results=results)
        status = self.read("handsoff-status.json")
        if run is not None:
            status["verification_head"] = run["hash"]
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        if risk_class is None:
            status.pop("risk_class", None)
        else:
            status["risk_class"] = risk_class
        lib.sync_coverage(status, acceptance)
        with lib.project_lock(self.root):
            lib.commit(self.root, self.cfg, status=status, event_kind="test_setup", event_message="setup")
        return run

    def criterion(self, cid, tests, verification="automated"):
        return {"id": cid, "requirement": f"{cid} requirement", "type": "supporting",
                "verification": verification, "tests": tests, "evidence": [], "state": "not_tested"}

    def close_with_findings(self):
        return self.run_cli("record-review-findings", "--by", "reviewer",
                            "--finding", "incorrect_implementation: the check still fails")

    def closed_events(self):
        return [e for e in lib.read_events(self.root, self.cfg) if e.get("kind") == "review_attempt_closed"]

    def test_records_are_present_and_bound_to_the_acceptance_hash(self):
        run = self.prepare([self.criterion("REQ-001", ["python3 -c pass"]),
                            self.criterion("REQ-002", ["python3 -c 'raise SystemExit(3)'"])],
                           results=[{"command": "python3 -c pass", "exit_code": 0},
                                    {"command": "python3 -c 'raise SystemExit(3)'", "exit_code": 3}])
        closed = self.close_with_findings()
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        records = self.closed_events()[-1]["adaptive_records"]
        expected_hash = lib.acceptance_hash(self.read("handsoff-acceptance.json")["criteria"])
        self.assertTrue(all(r["acceptance_hash"] == expected_hash and r["mission_id"] for r in records))
        checks = [r for r in records if r["record_type"] == "deterministic_check"]
        self.assertEqual([(c["check_id"], c["outcome"], c["evidence"]) for c in checks],
                         [("REQ-001:1", "pass", [run["run_id"]]), ("REQ-002:1", "fail", [run["run_id"]])])
        self.assertIn("exit 3", checks[1]["detail"])
        kinds = [r["record_type"] for r in records[len(checks):]]
        self.assertEqual(kinds, ["implementer_claim", "reviewer_claim", "supporting_evidence"])
        reviewer = records[len(checks) + 1]
        self.assertEqual((reviewer["actor"], reviewer["decision"]), ("reviewer", "repair"))
        self.assertEqual(records[-1]["evidence_id"], run["run_id"])
        self.assertLessEqual(len(records), 42)
        self.assertEqual(lib.validate_status_schema(self.read("handsoff-status.json")), [])

    def test_manual_and_unexecuted_checks_are_not_run(self):
        self.prepare([self.criterion("REQ-001", ["live: the installed engine shows it"], "manual"),
                      self.criterion("REQ-002", ["python3 -c pass"])])
        closed = self.close_with_findings()
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        checks = [r for r in self.closed_events()[-1]["adaptive_records"]
                  if r["record_type"] == "deterministic_check"]
        self.assertEqual([(c["outcome"], c["evidence"], c["detail"]) for c in checks],
                         [("not_run", [], "manual criterion"), ("not_run", [], "never executed")])

    def test_a_33_check_plan_is_refused_before_anything_is_recorded(self):
        self.prepare([self.criterion("REQ-001", [f"python3 -c 'print({n})'" for n in range(33)])])
        status_before = (self.root / "handsoff-status.json").read_bytes()
        refused = self.close_with_findings()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("33 checks", refused.stdout)
        self.assertIn("bound is 32", refused.stdout)
        self.assertEqual((self.root / "handsoff-status.json").read_bytes(), status_before)
        # The performance clock's own transition log (#385) may append; no
        # review event or attempt is recorded.
        kinds = {e.get("kind") for e in lib.read_events(self.root, self.cfg)}
        self.assertFalse(kinds & {"review_attempt_closed", "review_attempt_opened", "review_attempt_refused"})

    def test_no_records_without_a_risk_class(self):
        self.prepare([self.criterion("REQ-001", ["python3 -c pass"])], risk_class=None)
        closed = self.close_with_findings()
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertNotIn("adaptive_records", self.closed_events()[-1])


if __name__ == "__main__":
    unittest.main()
