"""#419: verify records the original symptom resolved when every
primary_fix criterion passes on automated evidence, bound to the run and
the actor. A manual primary_fix still needs record-symptom-resolved, and
then next_action and the reviewer-launch refusal name the exact run id."""
import json
import shutil
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin
from tests.engine_patch import patch_engine

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class SymptomAutoTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1))
        self.init("Symptom fixture")
        updated = run(["criterion-update", "REQ-001", "--requirement", "The symptom is gone",
                       "--test", "true"], cwd=self.tmp)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "A supporting check",
                     "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

    def _verify(self, *criteria, by="test-implementer"):
        args = ["verify", "--by", by] + (["--all"] if not criteria else
                                         [a for cid in criteria for a in ("--criterion", cid)])
        r = run(args, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r, json.loads(r.stdout)

    def _symptom_events(self):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == "symptom_resolved"]

    def _assert_recorded(self, r, payload, by="test-implementer"):
        run_id = payload["criteria"]["REQ-001"]["run_id"]
        status = self.read_status()
        self.assertTrue(status["requirement_coverage"]["original_symptom_resolved"])
        self.assertEqual(status["original_symptom_evidence_id"], run_id)
        self.assertEqual(payload["original_symptom_resolved"], run_id)
        self.assertIn(f"ORIGINAL_SYMPTOM_RESOLVED: {run_id}", r.stderr)
        event = self._symptom_events()[-1]
        self.assertEqual((event["evidence"], event["by"], event["automatic"]), (run_id, by, True))
        return run_id

    def test_verify_all_records_the_symptom(self):
        r, payload = self._verify(by="impl-7")
        self._assert_recorded(r, payload, by="impl-7")

    def test_a_single_criterion_verify_of_the_primary_fix_records_it(self):
        r, payload = self._verify("REQ-001")
        self._assert_recorded(r, payload)

    def test_a_verify_that_leaves_a_primary_unverified_records_nothing(self):
        r, payload = self._verify("REQ-002")
        self.assertFalse(self.read_status()["requirement_coverage"]["original_symptom_resolved"])
        self.assertNotIn("original_symptom_resolved", payload)
        self.assertNotIn("ORIGINAL_SYMPTOM_RESOLVED", r.stderr)
        self.assertEqual(self._symptom_events(), [])

    def test_a_manual_primary_fix_leaves_it_to_the_command_and_names_the_run_id(self):
        updated = run(["criterion-update", "REQ-001", "--verification", "manual"], cwd=self.tmp)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        evidence = run(["record-evidence", "REQ-001", "--kind", "manual", "--description", "seen fixed",
                        "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
        manual_run = evidence.stdout.split("EVIDENCE_RECORDED:")[1].strip()
        r, payload = self._verify("REQ-002")
        status = self.read_status()
        self.assertFalse(status["requirement_coverage"]["original_symptom_resolved"])
        self.assertEqual(self._symptom_events(), [])
        step = f"handsoff_supervisor.py record-symptom-resolved --evidence {manual_run} --by ACTOR"
        self.assertIn(step, status["next_action"])
        # the reviewer-launch refusal names the same run id (the runtime
        # integrity check is the manifest's concern, not this refusal's)
        status["phase_number"], status["phase"] = 5, lib.PHASES[5]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_phase",
                   event_message="fixture at Phase 5", actor="test")
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        with patch_engine("validate_runtime_integrity", return_value=None), \
                self.assertRaises(lib.HandsoffError) as refused:
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=lambda name: f"/bin/{name}")
        self.assertIn(f"reviewer launch refused: run {step} first", str(refused.exception))
        # and the command it names is accepted
        recorded = run(["record-symptom-resolved", "--evidence", manual_run, "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

    def test_after_a_rollback_clears_it_the_next_passing_verify_records_it_again(self):
        _, first = self._verify()
        first_run = first["criteria"]["REQ-001"]["run_id"]
        changed = run(["criterion-update", "REQ-001", "--requirement", "The symptom is gone, restated"],
                      cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        status = self.read_status()
        self.assertFalse(status["requirement_coverage"]["original_symptom_resolved"])
        self.assertIsNone(status["original_symptom_evidence_id"])
        r, payload = self._verify("REQ-001")
        second_run = self._assert_recorded(r, payload)
        self.assertNotEqual(second_run, first_run)
        self.assertEqual(len(self._symptom_events()), 2)


if __name__ == "__main__":
    unittest.main()
