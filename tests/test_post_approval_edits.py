"""Focused REQ-001 checks for approval edits and verification bindings."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402
from tests.fixture_state import write_version_pin


class PostApprovalEditTests(unittest.TestCase):
    def criterion(self, test="python3 -m unittest tests.test_post_approval_edits -v"):
        return {"id": "REQ-001", "type": "primary_fix", "requirement": "fix the symptom",
                "verification": "automated", "tests": [test], "evidence": [], "state": "not_tested"}

    def test_post_approval_update_guard_has_pointer_text(self):
        status = {"design_approved": {"by": "pilot"}}
        self.assertTrue(supervisor._approval_edit_guard(status, False))

    def test_revoke_invalidation_names_and_recomputes_phase_two(self):
        status = {"phase_number": 4, "review": {"x": 1}, "reviewed_by": "r",
                  "deployment_approved": {"x": 1}, "live_verification_id": "vr-1",
                  "design_approved": {"x": 1}, "design_review": {"x": 1},
                  "design_proposal": {"x": 1}, "requires_design_approval": True}
        revoked = supervisor._invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        self.assertEqual(set(revoked), {"review", "deployment_approved", "live_verification_id",
                                        "design_approved", "design_review", "design_proposal"})
        self.assertEqual(status["phase_number"], 2)
        self.assertEqual(status["next_action"], "Architect revises the design and requests review again")

    def test_criterion_add_guard_is_declined_without_revoke(self):
        self.assertTrue(supervisor._approval_edit_guard({"design_approved": {"at": "now"}}, False))

    def test_live_commands_do_not_change_decision_hashes(self):
        cfg = {key: None for key in lib.GOVERNANCE_CONFIG_KEYS}
        cfg.update(check_commands=["true"], live_check_commands=["live-a"], regressions=[])
        first = lib.config_hash(cfg), lib.verification_config_hash(cfg)
        cfg["live_check_commands"] = ["live-b"]
        self.assertEqual(first, (lib.config_hash(cfg), lib.verification_config_hash(cfg)))

    def test_live_record_carries_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = {"verification_log": "verifications.jsonl"}
            record = lib.append_verification(root, cfg, kind="live", ok=True, by="pilot",
                                             criteria=[self.criterion()], commands=["live-a"], results=[])
            self.assertEqual(record["commands"], ["live-a"])
            stored = json.loads((root / "verifications.jsonl").read_text())
            self.assertEqual(stored["commands"], ["live-a"])

    def test_unverifiable_test_warning_does_not_refuse(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            supervisor._print_unverifiable_test_warnings({"criteria": [self.criterion("pytest test_x.py::test_y")]},
                                                         {"check_commands": []})
        self.assertIn("WARNING: criterion REQ-001 test 'pytest test_x.py::test_y' matches no [checks].commands entry; verify will refuse it", output.getvalue())


if __name__ == "__main__":
    unittest.main()


from tests.test_handsoff_supervisor import HandsoffTestCase, approve_design_review, run  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


class PostApprovalCliTests(HandsoffTestCase):
    """REQ-001 end to end through the CLI: the refusal, the deliberate
    revoke, and a live_commands-only change after deployment approval."""

    def _approved_design(self):
        self.init("Approval survives bookkeeping")
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "A reviewed requirement"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp).returncode, 0)
        approved = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                        "--summary", "fixture"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

    def test_criterion_update_is_refused_after_approval_and_nothing_changes(self):
        self._approved_design()
        before = self.read_status()
        result = run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("amendment-open", result.stdout)
        self.assertIn("--revoke-approval", result.stdout)
        after = self.read_status()
        self.assertEqual(after["design_approved"], before["design_approved"])
        self.assertEqual(after["phase_number"], before["phase_number"])

    def test_revoke_approval_flag_proceeds_and_names_what_it_revoked(self):
        self._approved_design()
        result = run(["criterion-update", "REQ-001", "--test", "true", "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        self.assertIsNone(status["design_approved"])
        self.assertEqual(status["phase_number"], 2)
        self.assertIn("revises", status["next_action"])
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        last = events[-1]
        self.assertEqual(last["kind"], "criterion_updated")
        self.assertIn("design_approved", last["decisions_revoked"])
        self.assertIn("design_review", last["decisions_revoked"])
    def test_criteria_apply_with_revoke_completes_and_names_revoked_decisions(self):
        # Regression: cmd_criteria_apply referenced decisions_revoked without
        # assigning it, so every non-dry-run transaction raised NameError.
        self._approved_design()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        tx = self.tmp / "tx.json"
        tx.write_text(json.dumps({"operations": [{"op": "update", "id": "REQ-001", "fields": {"tests": ["true"]}}]}))
        result = run(["criteria-apply", "--file", str(tx), "--by", "supervisor", "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertIn("design_approved", json.dumps(events[-1]))


    def test_live_commands_change_after_deployment_approval_keeps_the_approval(self):
        self.init("Config edits are not scope")
        write_version_pin(self.tmp)  # the pin counts as content; write it before evidence
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        gate = run(["deployment-gate", "--approve", "--by", "pilot"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('live_commands = []', 'live_commands = ["true"]'))
        status = self.read_status()
        self.assertIsNotNone(status["deployment_approved"])
        self.assertIsNotNone(status["review"])
        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        shown = run(["status"], cwd=self.tmp)
        self.assertEqual(shown.returncode, 0, shown.stdout[-600:] + shown.stderr[-300:])
        self.assertEqual(json.loads(shown.stdout)["evidence_drift"]["stale"], [])

    def test_rollback_after_drift_rewrites_next_action(self):
        # #110: a Phase 7 run knocked back to Phase 5 by fresh evidence must
        # read the Phase 5 default, not the deployment text it carried.
        self.init("Rollback rewrites next_action")
        write_version_pin(self.tmp)
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        gate = run(["deployment-gate", "--approve", "--by", "pilot"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertEqual(self.read_status()["phase_number"], 7)
        (self.tmp / "product.txt").write_text("changed after approval\n")
        verified = run(["verify", "--criterion", "REQ-001", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 5)
        self.assertIsNone(status["deployment_approved"])
        self.assertEqual(status["next_action"], lib.NEXT_ACTION_DEFAULTS[5])

    def test_changing_check_commands_after_evidence_is_drift(self):
        # The counterpart: configuration that changes what a check proves
        # does invalidate evidence, through the verification config hash.
        self.init("Check commands are bound")
        write_version_pin(self.tmp)
        self.set_criterion_state("passing", resolved=True)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('commands = ["true"]', 'commands = ["true", "false"]'))
        shown = run(["status"], cwd=self.tmp)
        self.assertEqual(json.loads(shown.stdout)["evidence_drift"]["stale"], ["REQ-001"])
