#!/usr/bin/env python3
"""P2.1: owner acceptance, opt-in.

[workflow] owner_acceptance = off (default) or high_risk, with [workflow]
owners naming human identities. When high_risk and the run is high risk
(an elevated criterion, or a risk class that requires a human gate),
advance 8 refuses until owner-accept is recorded for every elevated
criterion (or the primary criterion when only the run is high risk) by a
listed owner, bound to the current acceptance hash. owner-accept is
human-only: the broker refuses it and a managed role's session refuses it.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402

FULL = {dimension: f"test_{dimension}" for dimension in workflow.NEGATIVE_PATH_DIMENSIONS}
MANAGED = "HANDSOFF_MANAGED_SESSION_ROLE"


class OwnerFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        # this suite may itself run inside a managed session; the CLI it drives
        # stands for a human at a terminal, so the marker is removed for it
        self._managed = os.environ.pop(MANAGED, None)
        self.addCleanup(self._restore_managed)
        self.scratch = Path(tempfile.mkdtemp(prefix="handsoff-owner-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def _restore_managed(self):
        if self._managed is not None:
            os.environ[MANAGED] = self._managed

    def configure(self, mode="high_risk", owners=("moncy",)):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace("live_commands = []", 'live_commands = ["true"]', 1)
        if mode is not None:
            text = text.replace("[workflow]\n", f"[workflow]\nowner_acceptance = {json.dumps(mode)}\n"
                                f"owners = {json.dumps(list(owners))}\n", 1)
        toml.write_text(text)

    def reach_live(self, *, elevated=True):
        self.init("P2.1 owner acceptance")
        self.set_criterion_state("passing", resolved=True)
        if elevated:
            args = ["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P2.1 elevated",
                    "--verification", "automated", "--test", "true", "--risk", "elevated"]
            for dimension, text in FULL.items():
                args += ["--negative-path", f"{dimension}={text}"]
            self._ok(run(args, cwd=self.tmp))
            self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self._ok(self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1"))
        self._ok(run(["deployment-gate", "--approve", "--by", "pilot"], cwd=self.tmp))
        self._ok(run(["verify-live", "--by", "monitor"], cwd=self.tmp))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def accept(self, criterion, by="moncy", **extra):
        args = ["owner-accept", "--criterion", criterion, "--environment", "production",
                "--path", "Checkout > Pay", "--input", "card 4242", "--expected", "a receipt",
                "--observed", "a receipt with the order number", "--by", by]
        for key, value in extra.items():
            args += [f"--{key}", value]
        return run(args, cwd=self.tmp)

    def advance_8(self):
        return run(["advance", "8", "100", "--status", "complete"], cwd=self.tmp)


class TestOffByDefault(OwnerFixture):
    def test_off_changes_nothing(self):
        self.configure(mode=None)
        cfg = lib.load_config(self.tmp)
        self.assertEqual((cfg["owner_acceptance"], cfg["owners"]), ("off", []))
        self.reach_live()
        self.assertEqual(lib.owner_acceptance_criteria(self.read_status(), self.read_acceptance(), cfg), [])
        refused = self.accept("REQ-002")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("owner acceptance is off", refused.stdout)
        self._ok(self.advance_8())

    def test_the_settings_are_validated_and_hashed_only_when_set(self):
        self.configure(mode=None)
        cfg = lib.load_config(self.tmp)
        baseline = lib.config_hash(cfg)
        self.assertEqual(lib.config_hash({**cfg, "owner_acceptance": "off", "owners": []}), baseline)
        self.assertNotEqual(lib.config_hash({**cfg, "owner_acceptance": "high_risk", "owners": ["moncy"]}), baseline)
        toml = self.tmp / "handsoff.toml"
        original = toml.read_text()
        for block, needle in (('owner_acceptance = "always"', "workflow.owner_acceptance must be one of"),
                              ('owner_acceptance = "high_risk"', "needs workflow.owners"),
                              ('owners = ["a", "A"]', "workflow.owners must be a list of distinct"),
                              ('owners = [""]', "workflow.owners must be a list of distinct")):
            with self.subTest(block=block):
                toml.write_text(original.replace("[workflow]\n", f"[workflow]\n{block}\n", 1))
                with self.assertRaisesRegex(lib.HandsoffError, needle):
                    lib.load_config(self.tmp)
        toml.write_text(original)


class TestHighRisk(OwnerFixture):
    def test_without_records_advance_8_is_refused(self):
        self.configure()
        self.reach_live()
        refused = self.advance_8()
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("owner acceptance gate: Phase 8 requires owner acceptance of REQ-002", refused.stdout)
        self.assertIn("clears with: handsoff_supervisor.py owner-accept --criterion REQ-002", refused.stdout)
        # only the elevated criterion needs it
        self.assertNotIn("acceptance of REQ-001", refused.stdout)
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(payload["owner_acceptance_required"], ["REQ-002"])

    def test_a_non_owner_is_refused(self):
        self.configure()
        self.reach_live()
        refused = self.accept("REQ-002", by="intruder")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("intruder is not one of [workflow] owners", refused.stdout)
        self.assertNotIn("owner_acceptance", self.read_status())
        # a record written by hand for a non-owner does not satisfy the gate either
        cfg = lib.load_config(self.tmp)
        status = dict(self.read_status(), owner_acceptance={"REQ-002": {
            "by": "intruder", "acceptance_hash": lib.acceptance_hash(self.read_acceptance()["criteria"])}})
        errors = lib.owner_acceptance_errors(status, self.read_acceptance(), cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn("who is not in [workflow] owners", errors[0])

    def test_a_managed_role_is_refused_through_the_broker_and_in_its_session(self):
        self.configure()
        self.reach_live()
        self.assertIn("owner-accept", broker.HUMAN_ONLY_COMMANDS)
        request = {"command": "owner-accept", "project_root": str(self.tmp), "actor": "supervisor",
                   "action": "workflow", "criterion": "REQ-002", "by": "moncy"}
        with self.assertRaisesRegex(lib.HandsoffError, "broker refuses human-only command: owner-accept"):
            broker._workflow_argv(self.tmp, request)
        with mock.patch.dict(os.environ, {MANAGED: "implementer"}):
            refused = self.accept("REQ-002")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("owner-accept is human-only", refused.stdout)
        self.assertNotIn("owner_acceptance", self.read_status())

    def test_complete_records_clear_the_gate(self):
        self.configure(owners=("moncy", "dana"))
        self.reach_live()
        # outside the project: an artifact is evidence about the deployment, not repository content
        artifact = self.scratch / "receipt.png"
        artifact.write_bytes(b"png")
        self._ok(self.accept("REQ-002", by="Dana", artifact=str(artifact)))
        record = self.read_status()["owner_acceptance"]["REQ-002"]
        self.assertEqual((record["by"], record["environment"], record["artifact"]), ("Dana", "production", str(artifact)))
        self.assertEqual(record["acceptance_hash"], lib.acceptance_hash(self.read_acceptance()["criteria"]))
        for field in ("path", "input", "expected", "observed", "at"):
            self.assertTrue(record[field], field)
        self._ok(self.advance_8())

    def test_an_acceptance_change_invalidates_the_records(self):
        self.configure()
        self.reach_live()
        self._ok(self.accept("REQ-002"))
        cfg = lib.load_config(self.tmp)
        status = dict(self.read_status(), phase_number=8, phase=lib.PHASES[8], progress=100, status="complete")
        acceptance = self.read_acceptance()
        self.assertEqual(lib.owner_acceptance_errors(status, acceptance, cfg), [])
        acceptance["criteria"][0]["evidence"] = []
        acceptance["criteria"][0]["state"] = "not_tested"
        errors = lib.owner_acceptance_errors(status, acceptance, cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn("the acceptance registry changed since it was accepted", errors[0])

    def test_a_run_high_risk_only_by_its_class_needs_the_primary_criterion(self):
        self.configure()
        cfg = lib.load_config(self.tmp)
        acceptance = {"criteria": [{"id": "REQ-001", "type": "primary_fix"}, {"id": "REQ-002", "type": "supporting"}]}
        self.assertEqual(lib.owner_acceptance_criteria({"risk_class": "routine"}, acceptance, cfg), [])
        self.assertEqual(lib.owner_acceptance_criteria({"risk_class": "security_sensitive"}, acceptance, cfg),
                         ["REQ-001"])
        self.assertEqual(lib.owner_acceptance_criteria({}, acceptance, cfg), [])
        errors = lib.owner_acceptance_errors({"risk_class": "security_sensitive"}, acceptance, cfg)
        self.assertIn("owner acceptance of REQ-001", errors[0])

    def test_a_run_that_is_not_high_risk_is_not_gated(self):
        self.configure()
        self.reach_live(elevated=False)
        self.assertEqual(lib.owner_acceptance_criteria(self.read_status(), self.read_acceptance(),
                                                       lib.load_config(self.tmp)), [])
        self._ok(self.advance_8())

    def test_bad_inputs_are_refused(self):
        self.configure()
        self.reach_live()
        self.assertIn("unknown criterion REQ-404", self.accept("REQ-404").stdout)
        self.assertIn("--artifact missing.png is not a file", self.accept("REQ-002", artifact="missing.png").stdout)
        blank = run(["owner-accept", "--criterion", "REQ-002", "--environment", " ", "--path", "p", "--input", "i",
                     "--expected", "e", "--observed", "o", "--by", "moncy"], cwd=self.tmp)
        self.assertIn("--environment must be non-empty", blank.stdout)


if __name__ == "__main__":
    unittest.main()
