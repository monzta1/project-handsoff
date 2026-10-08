#!/usr/bin/env python3
"""P1.1: criteria as outcomes with evidence classes.

A criterion may state its observable `outcome` and declare
`evidence_classes` (checks, manual, browser) required on top of its
verification policy, plus the `paths` its evidence depends on (P1.2). The
required kinds are the policy's kinds plus the classes, wherever passing is
decided: verify, record-evidence and the phase gate. A criterion without
the fields behaves exactly as before.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402


class OutcomeFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.counters = Path(tempfile.mkdtemp(prefix="handsoff-outcomes-counters-"))
        self.addCleanup(shutil.rmtree, self.counters, True)
        script = self.counters / "check.sh"
        script.write_text("exit 0\n")
        self.command = f"sh {script}"
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", f"commands = {json.dumps([self.command])}", 1))
        self.init("P1.1 criteria as outcomes")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "P1.1 primary outcome",
                      "--test", self.command], cwd=self.tmp))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _refused(self, result, needle):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(needle, result.stdout + result.stderr)
        return result

    def _criterion(self, cid):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def _verify(self, cid):
        return self._ok(run(["verify", "--criterion", cid, "--by", "test-implementer"], cwd=self.tmp))

    def _record(self, cid, kind):
        return self._ok(run(["record-evidence", cid, "--kind", kind, "--description",
                             f"{kind} walk-through", "--by", "test-implementer"], cwd=self.tmp))


class TestEvidenceClassesEndToEnd(OutcomeFixture):
    def test_manual_criterion_declaring_checks_needs_verify_and_manual_evidence(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 manual plus checks",
                      "--verification", "manual", "--test", self.command, "--evidence-class", "checks",
                      "--outcome", "the operator sees the plain line"], cwd=self.tmp))
        criterion = self._criterion("REQ-002")
        self.assertEqual(criterion["evidence_classes"], ["checks"])
        self.assertEqual(criterion["outcome"], "the operator sees the plain line")
        self.assertEqual(lib.required_evidence_kinds(criterion), {"manual", "checks"})
        # verify now runs it (checks is required), but manual is still missing
        payload = json.loads(self._verify("REQ-002").stdout)
        self.assertTrue(payload["criteria"]["REQ-002"]["ok"])
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")
        self._record("REQ-002", "manual")
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")

    def test_manual_evidence_first_then_verify_also_passes_only_with_both(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 manual plus checks",
                      "--verification", "manual", "--test", self.command, "--evidence-class", "checks"],
                     cwd=self.tmp))
        self._record("REQ-002", "manual")
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")
        self._verify("REQ-002")
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")

    def test_automated_criterion_declaring_browser_stays_not_passing_until_browser_evidence(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 automated plus browser",
                      "--verification", "automated", "--test", self.command, "--evidence-class", "browser"],
                     cwd=self.tmp))
        self._verify("REQ-002")
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")
        self._record("REQ-002", "browser")
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")

    def test_record_evidence_refuses_a_kind_neither_policy_nor_class_requires(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 automated only",
                      "--verification", "automated", "--test", self.command], cwd=self.tmp))
        self._refused(run(["record-evidence", "REQ-002", "--kind", "browser", "--description", "x",
                           "--by", "test-implementer"], cwd=self.tmp), "does not accept browser evidence")

    def test_phase_gate_refuses_a_passing_criterion_missing_a_declared_class(self):
        criterion = {"id": "REQ-009", "type": "supporting", "requirement": "x", "verification": "automated",
                     "tests": ["t"], "evidence": [], "state": "passing", "evidence_classes": ["browser"]}
        checks = {"ok": True, "kind": "checks", "criteria": ["REQ-009"],
                  "criterion_hashes": {"REQ-009": lib.criterion_spec_hash(criterion)}}
        errors = workflow._evidence_errors([criterion], [checks])
        self.assertEqual(len(errors), 1)
        self.assertIn("lacks valid browser evidence", errors[0])
        self.assertFalse(lib.criterion_fully_evidenced(criterion, [checks]))
        browser = dict(checks, kind="browser")
        self.assertEqual(workflow._evidence_errors([criterion], [checks, browser]), [])
        self.assertTrue(lib.criterion_fully_evidenced(criterion, [checks, browser]))

    def test_status_shows_outcome_classes_and_required_evidence(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 status view",
                      "--verification", "automated", "--test", self.command, "--evidence-class", "manual",
                      "--outcome", "visible outcome", "--path", "src/a"], cwd=self.tmp))
        result = run(["status"], cwd=self.tmp)
        payload = json.loads(result.stdout)
        row = next(c for c in payload["criteria"] if c["id"] == "REQ-002")
        self.assertEqual(row["outcome"], "visible outcome")
        self.assertEqual(row["evidence_classes"], ["manual"])
        self.assertEqual(row["paths"], ["src/a"])
        self.assertEqual(row["required_evidence"], ["checks", "manual"])
        self.assertIn("session_projection", payload)


class TestCriterionFieldValidation(OutcomeFixture):
    def test_invalid_fields_are_refused_by_criterion_add(self):
        base = ["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 invalid",
                "--verification", "automated", "--test", self.command]
        self._refused(run(base + ["--outcome", "x" * 513], cwd=self.tmp), "'outcome'")
        self._refused(run(base + ["--evidence-class", "mutation"], cwd=self.tmp), "policy-only")
        self._refused(run(base + ["--evidence-class", "bogus"], cwd=self.tmp), "'evidence_classes'")
        self._refused(run(base + ["--path", "/etc/passwd"], cwd=self.tmp), "project-relative")
        self._refused(run(base + ["--path", "../outside"], cwd=self.tmp), "project-relative")
        many = [arg for index in range(33) for arg in ("--path", f"src/{index}")]
        self._refused(run(base + many, cwd=self.tmp), "1 to 32")
        self.assertNotIn("REQ-002", [c["id"] for c in self.read_acceptance()["criteria"]])

    def test_checks_class_requires_tests_that_are_check_commands(self):
        self._refused(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 unrunnable",
                           "--verification", "manual", "--test", "walk through it", "--evidence-class", "checks"],
                          cwd=self.tmp), "evidence class checks")
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 manual",
                      "--verification", "manual", "--test", "walk through it"], cwd=self.tmp))
        self._refused(run(["criterion-update", "REQ-002", "--evidence-class", "checks"], cwd=self.tmp),
                      "evidence class checks")

    def test_invalid_fields_are_refused_by_criteria_apply(self):
        bad = {"operations": [{"op": "add", "criterion": {
            "id": "REQ-002", "type": "supporting", "requirement": "P1.1 tx", "verification": "automated",
            "tests": [self.command], "evidence_classes": ["mutation"]}}]}
        tx = self.tmp.parent / f"{self.tmp.name}-tx.json"
        self.addCleanup(lambda: tx.unlink(missing_ok=True))
        tx.write_text(json.dumps(bad))
        self._refused(run(["criteria-apply", "--file", str(tx), "--by", "test-implementer"], cwd=self.tmp),
                      "policy-only")

    def test_criteria_apply_adds_and_clears_the_fields(self):
        tx = self.tmp.parent / f"{self.tmp.name}-tx.json"
        self.addCleanup(lambda: tx.unlink(missing_ok=True))
        tx.write_text(json.dumps({"operations": [{"op": "add", "criterion": {
            "id": "REQ-002", "type": "supporting", "requirement": "P1.1 tx", "verification": "automated",
            "tests": [self.command], "outcome": "tx outcome", "evidence_classes": ["browser"],
            "paths": ["src/a/*"]}}]}))
        self._ok(run(["criteria-apply", "--file", str(tx), "--by", "test-implementer"], cwd=self.tmp))
        criterion = self._criterion("REQ-002")
        self.assertEqual((criterion["outcome"], criterion["evidence_classes"], criterion["paths"]),
                         ("tx outcome", ["browser"], ["src/a/*"]))
        tx.write_text(json.dumps({"operations": [{"op": "update", "id": "REQ-002", "fields": {
            "outcome": None, "evidence_classes": None, "paths": None}}]}))
        self._ok(run(["criteria-apply", "--file", str(tx), "--by", "test-implementer"], cwd=self.tmp))
        criterion = self._criterion("REQ-002")
        for field in ("outcome", "evidence_classes", "paths"):
            self.assertNotIn(field, criterion)

    def test_validator_unit_rules(self):
        self.assertEqual(lib.validate_criterion_fields({"outcome": "ok", "evidence_classes": ["manual", "browser"],
                                                        "paths": ["src/**", "docs/x.md"]}), [])
        self.assertTrue(lib.validate_criterion_fields({"evidence_classes": []}))
        self.assertTrue(lib.validate_criterion_fields({"evidence_classes": ["manual", "manual"]}))
        self.assertTrue(lib.validate_criterion_fields({"paths": ["src", "src"]}))
        self.assertTrue(lib.validate_criterion_fields({"outcome": "   "}))
        self.assertEqual(lib.validate_criterion_fields({"outcome": None, "paths": None}), [])


class TestSpecHashAndLegacy(OutcomeFixture):
    def test_fields_are_part_of_the_spec_hash(self):
        legacy = {"id": "REQ-1", "type": "supporting", "requirement": "r", "verification": "automated",
                  "tests": ["t"], "evidence": [], "state": "not_tested"}
        hashes = {lib.criterion_spec_hash(legacy)}
        for field, value in (("outcome", "o"), ("evidence_classes", ["manual"]), ("paths", ["src"])):
            hashes.add(lib.criterion_spec_hash({**legacy, field: value}))
        self.assertEqual(len(hashes), 4)

    def test_changing_the_outcome_resets_evidence(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 reset",
                      "--verification", "automated", "--test", self.command], cwd=self.tmp))
        self._verify("REQ-002")
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")
        self._ok(run(["criterion-update", "REQ-002", "--outcome", "a new outcome"], cwd=self.tmp))
        criterion = self._criterion("REQ-002")
        self.assertEqual((criterion["state"], criterion["evidence"]), ("not_tested", []))

    def test_legacy_criterion_is_unchanged(self):
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.1 legacy",
                      "--verification", "automated", "--test", self.command], cwd=self.tmp))
        criterion = self._criterion("REQ-002")
        for field in ("outcome", "evidence_classes", "paths"):
            self.assertNotIn(field, criterion)
        self.assertEqual(lib.required_evidence_kinds(criterion), lib.VERIFICATION_REQUIREMENTS["automated"])
        for policy, kinds in lib.VERIFICATION_REQUIREMENTS.items():
            self.assertEqual(lib.required_evidence_kinds({"verification": policy}), kinds)
        self._verify("REQ-002")
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")


class TestAcceptanceSchemaAligned(unittest.TestCase):
    def test_json_schema_matches_the_validator(self):
        schema = json.loads((ROOT / "schemas" / "acceptance.schema.json").read_text())
        properties = schema["properties"]["criteria"]["items"]["properties"]
        self.assertEqual(set(properties["verification"]["enum"]), set(lib.VERIFICATION_REQUIREMENTS))
        self.assertEqual(properties["evidence_classes"]["items"]["enum"], list(lib.EVIDENCE_CLASSES))
        self.assertNotIn("mutation", lib.EVIDENCE_CLASSES)
        self.assertEqual(properties["outcome"]["maxLength"], workflow.MAX_OUTCOME_CHARS)
        self.assertEqual(properties["paths"]["maxItems"], workflow.MAX_CRITERION_PATHS)
        for field in workflow.CRITERION_UPDATE_FIELDS:
            if field != "state":
                self.assertIn(field, properties, field)


if __name__ == "__main__":
    unittest.main()
