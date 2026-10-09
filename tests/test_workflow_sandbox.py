#!/usr/bin/env python3
"""P1.4: a CI workflow change needs a sandbox run, not just checks.

A criterion whose paths include a .github/workflows/ file, or that declares
evidence_classes workflow, requires evidence of kind workflow. Only
`workflow-check --criterion ID --by ACTOR` writes it: it runs
[checks].workflow_commands (plain argv, for example a disposable-repository
harness) and binds the record to the digest of the criterion's workflow
files, so a later change to those files stales it. With no
workflow_commands configured, the clearing command names the key.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402

WORKFLOW_FILE = ".github/workflows/ci.yml"


class WorkflowFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.scripts = Path(tempfile.mkdtemp(prefix="handsoff-workflow-scripts-"))
        self.addCleanup(shutil.rmtree, self.scripts, True)
        check = self.scripts / "check.sh"
        check.write_text("exit 0\n")
        self.command = f"sh {check}"
        # the harness exits with the code in harness-exit (0 unless a test says otherwise)
        # and appends a line per run, so a test can tell it really ran
        self.harness_log = self.scripts / "harness.log"
        self.harness_exit = self.scripts / "harness-exit"
        self.harness_exit.write_text("0")
        harness = self.scripts / "harness.sh"
        harness.write_text(f'echo ran >> "{self.harness_log}"\nexit "$(cat "{self.harness_exit}")"\n')
        self.harness = f"sh {harness}"
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", f"commands = {json.dumps([self.command])}", 1))
        (self.tmp / ".github" / "workflows").mkdir(parents=True)
        (self.tmp / WORKFLOW_FILE).write_text("on: push\njobs: {}\n")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "app.py").write_text("print('v1')\n")
        self.init("P1.4 workflow sandbox")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "P1.4 primary", "--test", self.command],
                     cwd=self.tmp))
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.4 the CI workflow",
                      "--verification", "automated", "--test", self.command, "--path", WORKFLOW_FILE],
                     cwd=self.tmp))

    def configure_harness(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "live_commands = []", f"live_commands = []\nworkflow_commands = {json.dumps([self.harness])}", 1))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _criterion(self, cid):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def _verifications(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines()
                if line.strip()]

    def _workflow_check(self, cid="REQ-002"):
        return run(["workflow-check", "--criterion", cid, "--by", "test-implementer"], cwd=self.tmp)

    def _drift(self):
        cfg = lib.load_config(self.tmp)
        records, _ = lib.load_verifications(self.tmp, cfg)
        return lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), records)


class TestWorkflowEvidenceIsRequired(WorkflowFixture):
    def test_a_workflow_path_criterion_stays_not_passing_on_checks_alone(self):
        criterion = self._criterion("REQ-002")
        self.assertEqual(lib.required_evidence_kinds(criterion), {"checks", "workflow"})
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")
        records = self._verifications()
        self.assertFalse(lib.criterion_fully_evidenced(self._criterion("REQ-002"), records))
        # record-evidence can never write the kind: its --kind choices exclude it
        refused = run(["record-evidence", "REQ-002", "--kind", "workflow", "--description", "x",
                       "--by", "test-implementer"], cwd=self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")

    def test_a_passing_criterion_with_only_checks_fails_the_evidence_gate_naming_workflow_check(self):
        criterion = dict(self._criterion("REQ-002"), state="passing")
        checks = {"ok": True, "kind": "checks", "criteria": ["REQ-002"],
                  "criterion_hashes": {"REQ-002": lib.criterion_spec_hash(criterion)}}
        errors = workflow._evidence_errors([criterion], [checks])
        self.assertEqual(errors, ["evidence gate: passing criterion REQ-002 lacks valid workflow evidence"])
        configured = {"workflow_commands": [self.harness]}
        gates = supervisor.advance_gates(errors, 6, {}, configured)
        self.assertEqual(gates[0][2], "handsoff_supervisor.py workflow-check --criterion REQ-002 --by ACTOR")
        self.assertEqual(lib.reviewer_launch_evidence_gaps([criterion], [checks]),
                         ["REQ-002: run handsoff_supervisor.py workflow-check --criterion REQ-002 --by ACTOR"])

    def test_with_no_workflow_commands_the_clearing_command_names_the_key(self):
        errors = ["evidence gate: passing criterion REQ-002 lacks valid workflow evidence"]
        command = supervisor.advance_gates(errors, 6, {}, lib.load_config(self.tmp))[0][2]
        self.assertTrue(command.startswith("set [checks].workflow_commands in handsoff.toml"), command)
        self.assertIn("workflow-check --criterion REQ-002", command)
        result = self._workflow_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHIP_FEATURE_NO_WORKFLOW_COMMANDS_CONFIGURED: set [checks].workflow_commands", result.stdout)
        self.assertFalse(self.harness_log.exists())

    def test_declaring_evidence_class_workflow_requires_it_without_a_workflow_path(self):
        self._ok(run(["criterion-add", "REQ-003", "--type", "supporting", "--requirement", "P1.4 declared",
                      "--verification", "automated", "--test", self.command, "--evidence-class", "workflow"],
                     cwd=self.tmp))
        criterion = self._criterion("REQ-003")
        self.assertEqual(lib.required_evidence_kinds(criterion), {"checks", "workflow"})
        self.assertEqual(lib.criterion_workflow_paths(criterion), [".github/workflows"])

    def test_criteria_without_workflow_paths_are_unchanged(self):
        for paths in (None, ["src/app.py"], [".github/dependabot.yml"], ["docs/workflows/x.md"]):
            criterion = {"id": "REQ-9", "verification": "automated", **({"paths": paths} if paths else {})}
            self.assertEqual(lib.required_evidence_kinds(criterion), {"checks"}, paths)
        for policy, kinds in lib.VERIFICATION_REQUIREMENTS.items():
            self.assertEqual(lib.required_evidence_kinds({"verification": policy}), kinds)
        for spelled in (".github/workflows/*.yml", "./.github/workflows/ci.yml", ".github/workflows"):
            self.assertIn("workflow", lib.required_evidence_kinds(
                {"verification": "automated", "paths": [spelled]}), spelled)
        self.configure_harness()
        self._ok(run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp))
        self.assertEqual(self._criterion("REQ-001")["state"], "passing")
        refused = self._workflow_check("REQ-001")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("needs no workflow evidence", refused.stdout)
        self.assertFalse(self.harness_log.exists())


class TestWorkflowCheck(WorkflowFixture):
    def test_workflow_check_records_and_clears_the_criterion(self):
        self.configure_harness()
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self.assertEqual(self._criterion("REQ-002")["state"], "not_tested")
        payload = json.loads(self._ok(self._workflow_check()).stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["criterion_state"], "passing")
        self.assertEqual(self.harness_log.read_text().count("ran"), 1, "the configured harness really ran")
        record = self._verifications()[-1]
        self.assertEqual((record["kind"], record["ok"], record["criteria"], record["commands"]),
                         ("workflow", True, ["REQ-002"], [self.harness]))
        entries = lib.repository_digest_entries(self.tmp, lib.load_config(self.tmp))
        self.assertEqual(record["scope_digests"]["REQ-002"],
                         lib.workflow_digest(self._criterion("REQ-002"), entries))
        self.assertIn(record["run_id"], self._criterion("REQ-002")["evidence"])
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")
        self.assertEqual(workflow._evidence_errors([self._criterion("REQ-002")], self._verifications()), [])
        events = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"workflow_checked"', events)

    def test_a_failing_harness_records_a_failure_and_never_passes(self):
        self.configure_harness()
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self.harness_exit.write_text("3")
        result = self._workflow_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(self._criterion("REQ-002")["state"], "failing")
        self.assertFalse(lib.criterion_fully_evidenced(self._criterion("REQ-002"), self._verifications()))

    def test_a_later_workflow_file_change_invalidates_it(self):
        self.configure_harness()
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self._ok(self._workflow_check())
        self.assertEqual(self._drift()["workflow_stale"], [])
        # a change outside the workflow files keeps it current
        (self.tmp / "src" / "app.py").write_text("print('v2')\n")
        self.assertEqual(self._drift()["workflow_stale"], [])
        (self.tmp / WORKFLOW_FILE).write_text("on: pull_request\njobs: {}\n")
        drift = self._drift()
        self.assertEqual(drift["workflow_stale"], ["REQ-002"])
        self.assertIn({"criterion": "REQ-002", "changed_paths": [WORKFLOW_FILE], "reason": "workflow files changed"},
                      drift["invalidated"])
        self.assertIn("handsoff_supervisor.py workflow-check --criterion REQ-002 --by ACTOR",
                      drift["refresh_commands"])
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        status = dict(self.read_status(), phase_number=5, phase=lib.PHASES[5])
        errors = lib.compute_errors(status, self.read_acceptance(), cfg, verifications=records,
                                    verification_problems=problems, root=self.tmp)
        line = next(e for e in errors if "workflow evidence was recorded on different workflow files" in e)
        self.assertIn(f"changed paths: {WORKFLOW_FILE}", line)
        self.assertEqual(supervisor.advance_gates([line], 6, status, cfg)[0][2],
                         "handsoff_supervisor.py workflow-check --criterion REQ-002 --by ACTOR")
        # running the harness again on the new files clears it
        self._ok(self._workflow_check())
        self.assertEqual(self._drift()["workflow_stale"], [])

    def test_workflow_commands_must_be_plain_argv(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "live_commands = []", 'live_commands = []\nworkflow_commands = ["sh harness.sh && echo ok"]', 1))
        with self.assertRaises(lib.HandsoffError) as raised:
            lib.load_config(self.tmp)
        self.assertIn("checks.workflow_commands[0]", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
