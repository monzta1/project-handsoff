"""REQ-004 (#421): an advance refusal names every unmet gate with its command."""
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import HandsoffTestCase, run

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_lib as lib  # noqa: E402


class AdvanceGatesTests(HandsoffTestCase):
    def verify(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "true"], self.tmp).returncode, 0)
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        import json
        return json.loads(result.stdout)["criteria"]["REQ-001"]["run_id"]

    def test_advance_7_from_phase_5_names_phase_6_gates(self):
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "Gate criterion"],
                             self.tmp).returncode, 0)
        reached = self.advance_to(5)
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        before = self.read_status()

        refused = run(["advance", "7", "--status", "awaiting_approval"], self.tmp)
        out = refused.stdout + refused.stderr
        self.assertEqual(refused.returncode, 1, out)
        self.assertIn("one step at a time", out)
        self.assertIn("advance 6 first", out)
        self.assertIn("Phase 6 is held by", out)
        # the gates that hold Phase 6, each with the command that clears it
        self.assertIn("- phase gate: every criterion and the original symptom must have verified evidence", out)
        self.assertIn("clears with: handsoff_supervisor.py verify --all --by ACTOR, then "
                      "handsoff_supervisor.py record-symptom-resolved --evidence RUN_ID --by ACTOR", out)
        self.assertIn("- review gate: Phase 6+ requires a recorded independent review", out)
        self.assertIn("clears with: handsoff_supervisor.py record-review --by REVIEWER --tests-executed yes", out)
        self.assertIn("- review gate: Phase 6+ requires 'implemented_by' to be recorded", out)
        self.assertIn("clears with: handsoff_supervisor.py advance 6 --implemented-by ACTOR", out)
        # a gate shown under Phase 6 is not repeated under Phase 7
        self.assertEqual(out.count("- review gate: Phase 6+ requires a recorded independent review"), 1)
        # the refusal writes nothing
        self.assertEqual(self.read_status()["phase_number"], before["phase_number"])
        self.assertEqual(self.read_status()["updated_at"], before["updated_at"])

    def test_phase_6_to_7_lists_drift_ci_and_status_together(self):
        run_id = self.verify()
        resolved = run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], self.tmp)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        reached = self.advance_to(6, implemented_by="test-implementer", reviewed_by="test-reviewer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["ci"] = {"state": "failed", "failed_check": "tests", "pr": 7}
        lib.commit(self.tmp, cfg, status=status, event_kind="test-ci",
                   event_message="Fixture: the watched PR has a failed check")
        source = self.tmp / "bin" / "handsoff_lib.py"
        source.write_text(source.read_text() + "\n# advance gates drift\n")

        refused = run(["advance", "7", "--status", "in_progress"], self.tmp)
        out = refused.stdout + refused.stderr
        self.assertEqual(refused.returncode, 1, out)
        self.assertTrue(refused.stdout.startswith("SHIP_FEATURE_BLOCKED\n"), out)
        # all three in one refusal, each followed by the command that clears it
        self.assertIn("- evidence drift: REQ-001 was verified on a different repository digest", out)
        self.assertIn("clears with: handsoff_supervisor.py verify --criterion REQ-001 --by ACTOR", out)
        self.assertIn("- CI: tests failed on PR #7", out)
        self.assertIn("clears with: handsoff_supervisor.py ci-watch --pr 7 --by ACTOR", out)
        self.assertIn("- state gate: Phase 7 requires status 'awaiting_approval' or 'ready_to_deploy'", out)
        self.assertIn("clears with: handsoff_supervisor.py advance 7 --status awaiting_approval", out)
        self.assertEqual(self.read_status()["phase_number"], 6)

    def test_every_gate_line_maps_to_a_command(self):
        status = {"requirement_coverage": {"original_symptom_resolved": True}}
        gates = __import__("handsoff_supervisor").advance_gates([
            "evidence drift: REQ-002 was verified on a different repository digest; re-run x",
            "evidence gate: passing criterion REQ-003 lacks valid manual evidence",
            "symptom gate: resolved original symptom must reference a successful verification run",
            "progress gate: required work item WI-1 must be done before 95%+",
            "status gate: acceptance registry is not fully green",
        ], 7, status)
        self.assertEqual([g[0] for g in gates], ["evidence drift", "evidence gate", "symptom gate",
                                                 "progress gate", "status gate"])
        self.assertEqual([g[2] for g in gates], [
            "handsoff_supervisor.py verify --criterion REQ-002 --by ACTOR",
            "handsoff_supervisor.py record-evidence REQ-003 --kind manual --description TEXT --by ACTOR",
            "handsoff_supervisor.py record-symptom-resolved --evidence RUN_ID --by ACTOR",
            "handsoff_supervisor.py verify --all --by ACTOR",
            "handsoff_supervisor.py verify --all --by ACTOR",
        ])


if __name__ == "__main__":
    unittest.main()
