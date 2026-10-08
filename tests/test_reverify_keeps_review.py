"""#411: re-verifying the reviewed tree keeps the review, the phase and the progress."""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin


class ReverifyKeepsReviewTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.outside = Path(tempfile.mkdtemp(prefix="handsoff-reverify-outside-"))
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.marker = self.outside / "marker"
        # passes until the marker (outside the root, so outside the digest) exists
        self.flaky = f"test ! -e {self.marker}"
        # edits a project file while it runs once the marker exists
        script = self.outside / "edit.sh"
        script.write_text(f'if [ -e "{self.marker}" ]; then echo x >> edited.txt; fi\nexit 0\n')
        self.editor = f"sh {script}"

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def _reach_phase_6(self, command=None):
        self.init("Reverify fixture")
        self.set_criterion_state("passing", resolved=True)
        if command:
            toml = self.tmp / "handsoff.toml"
            toml.write_text(re.sub(r'(?m)^commands = \["true"\]',
                                   lambda _: "commands = " + json.dumps(["true", command]), toml.read_text()))
            updated = run(["criterion-update", "REQ-001", "--test", command], cwd=self.tmp)
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement",
                     "A manually checked fixture behaviour", "--verification", "manual",
                     "--test", "look at it"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        manual = run(["record-evidence", "REQ-002", "--kind", "manual", "--description", "seen",
                      "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(manual.returncode, 0, manual.stdout + manual.stderr)
        verified = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        review = run(["record-review", "--by", "test-reviewer", "--tests-executed", "yes"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        advanced = self.advance_to(6, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 6)
        return status

    def _assert_retained(self, before):
        after = self.read_status()
        self.assertIsNotNone(after["review"])
        self.assertEqual(after["phase_number"], 6)
        self.assertEqual(after["progress"], before["progress"])
        self.assertEqual(after["reviewed_by"], "test-reviewer")
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.assertEqual(after["review"]["acceptance_hash"],
                         lib.acceptance_hash(self.read_acceptance()["criteria"]))
        self.assertNotEqual(after["review"]["acceptance_hash"], before["review"]["acceptance_hash"])
        self.assertEqual(after["review"]["reviewed_binding"], before["review"]["reviewed_binding"])
        retained = [e for e in self._events() if e.get("kind") == "review_retained"]
        self.assertTrue(retained)
        self.assertEqual(retained[-1]["digest"], before["review"]["reviewed_binding"]["digest"])
        self.assertEqual(retained[-1]["acceptance_hash"], after["review"]["acceptance_hash"])
        # the rebound review still clears every gate: Phase 7 is reachable
        advanced = self.advance_to(7, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def _assert_rolled_back(self, *, naming):
        status = self.read_status()
        self.assertIsNone(status["review"])
        self.assertEqual(status["phase_number"], 5)
        self.assertNotIn("review_retained", [e.get("kind") for e in self._events()])
        checks = [e for e in self._events() if e.get("kind") in {"checks_run", "evidence_recorded"}][-1]
        self.assertFalse(checks["review_retention"]["retained"])
        self.assertIn(naming, checks["review_retention"]["reason"])
        return status

    def test_review_records_its_binding(self):
        status = self._reach_phase_6()
        binding = status["review"]["reviewed_binding"]
        self.assertEqual(set(binding), {"digest", "specs_hash", "design_hash"})
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.assertEqual(binding["digest"], lib.repository_digest(self.tmp, lib.load_config(self.tmp)))
        self.assertEqual(binding["specs_hash"], lib.design_hash(self.read_acceptance()["criteria"]))

    def test_verify_on_identical_tree_keeps_review(self):
        before = self._reach_phase_6()
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["review"]["retained"])
        self._assert_retained(before)

    def test_verify_all_on_identical_tree_keeps_review(self):
        before = self._reach_phase_6()
        result = run(["verify", "--all", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_retained(before)

    def test_record_evidence_on_identical_tree_keeps_review(self):
        before = self._reach_phase_6()
        result = run(["record-evidence", "REQ-002", "--kind", "manual", "--description", "seen again",
                      "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REVIEW_RETAINED", result.stdout)
        self._assert_retained(before)

    def _reach_live(self):
        toml = self.tmp / "handsoff.toml"
        self._reach_phase_6()
        toml.write_text(re.sub(r"(?m)^live_commands = \[\]", 'live_commands = ["true"]', toml.read_text()))
        advanced = self.advance_to(7, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        live = run(["verify-live", "--by", "test-monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        status = self.read_status()
        self.assertTrue(status["live_verification_id"])
        return status

    def _reach_phase_8(self):
        self._reach_live()
        completed = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        status = self.read_status()
        self.assertEqual((status["phase_number"], status["progress"], status["status"]), (8, 100, "complete"))
        return status

    def test_verify_after_verify_live_keeps_review_phase_approval_and_live_run(self):
        before = self._reach_live()
        live_id = before["live_verification_id"]
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["review"]["retained"])
        after = self.read_status()
        self.assertEqual(after["phase_number"], 7)
        self.assertEqual(after["reviewed_by"], "test-reviewer")
        self.assertEqual(after["review"]["reviewed_binding"], before["review"]["reviewed_binding"])
        self.assertIsNotNone(after["deployment_approved"])
        self.assertEqual(after["deployment_approved"]["acceptance_hash"], after["review"]["acceptance_hash"])
        # the live run is bound to the specs, the tree and the env, none of
        # which moved: it stands, and Phase 8 needs no second live run
        self.assertEqual(after["live_verification_id"], live_id)
        retained = [e for e in self._events() if e.get("kind") == "review_retained"]
        self.assertIsNone(retained[-1]["live_verification_cleared"])
        completed = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_phase_8_recheck_on_identical_tree_keeps_phase_8(self):
        before = self._reach_phase_8()
        result = run(["verify", "--all", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = self.read_status()
        self.assertEqual((after["phase_number"], after["progress"], after["status"]), (8, 100, "complete"))
        self.assertEqual(after["live_verification_id"], before["live_verification_id"])
        self.assertEqual(after["reviewed_by"], "test-reviewer")
        self.assertNotEqual(after["review"]["acceptance_hash"], before["review"]["acceptance_hash"])
        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_phase_8_tracked_edit_rolls_back(self):
        self._reach_phase_8()
        (self.tmp / "changed.py").write_text("x = 1\n")
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self._assert_rolled_back(naming="changed.py")
        self.assertIsNone(status["live_verification_id"])

    def test_live_record_from_another_tree_is_cleared_on_retention(self):
        before = self._reach_live()
        sys.path.insert(0, str(BIN))
        import handsoff_supervisor as supervisor
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        digest = before["review"]["reviewed_binding"]["digest"]
        live_id = before["live_verification_id"]
        self.assertTrue(supervisor._live_binding_holds(self.tmp, cfg, live_id, acceptance, digest))
        self.assertFalse(supervisor._live_binding_holds(self.tmp, cfg, live_id, acceptance, "0" * 64))
        acceptance["criteria"][0]["requirement"] = "A respecified requirement"
        self.assertFalse(supervisor._live_binding_holds(self.tmp, cfg, live_id, acceptance, digest))
        self.assertFalse(lib.live_record_specs_current(
            next(r for r in lib.load_verifications(self.tmp, cfg)[0] if r.get("run_id") == live_id),
            acceptance["criteria"]))

    def test_failed_recheck_rolls_back(self):
        self._reach_phase_6(command=self.flaky)
        self.marker.write_text("fail now")
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self._assert_rolled_back(naming="REQ-001")

    def test_command_editing_a_file_during_verification_rolls_back_naming_it(self):
        self._reach_phase_6(command=self.editor)
        self.marker.write_text("edit now")
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_rolled_back(naming="edited.txt")
        printed = json.loads(result.stdout)["review"]
        self.assertFalse(printed["retained"])
        self.assertIn("a command edited the tree while it ran: edited.txt", printed["reason"])

    def test_tree_change_rolls_back_naming_the_path(self):
        self._reach_phase_6()
        (self.tmp / "changed.py").write_text("x = 1\n")
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_rolled_back(naming="changed.py")

    def test_acceptance_spec_edit_rolls_back(self):
        status = self._reach_phase_6()
        sys.path.insert(0, str(BIN))
        import handsoff_supervisor as supervisor
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        digest = status["review"]["reviewed_binding"]["digest"]
        self.assertIsNone(supervisor._review_retention_refusal(
            self.tmp, cfg, status, acceptance, digest_before=digest, digest_after=digest,
            rechecked=acceptance["criteria"]))
        acceptance["criteria"][0]["requirement"] = "A respecified requirement"
        self.assertEqual(supervisor._review_retention_refusal(
            self.tmp, cfg, status, acceptance, digest_before=digest, digest_after=digest,
            rechecked=acceptance["criteria"]), "the criterion specifications changed since the review")
        changed_design = dict(status, design_proposal={"design_hash": "0" * 64})
        self.assertEqual(supervisor._review_retention_refusal(
            self.tmp, cfg, changed_design, self.read_acceptance(), digest_before=digest, digest_after=digest,
            rechecked=[]), "the design changed since the review")
        # through the CLI: a spec edit rolls the run back and verify cannot restore it
        edited = run(["criterion-update", "REQ-001", "--requirement", "A genuinely different requirement",
                      "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = self.read_status()
        self.assertIsNone(after["review"])
        self.assertLess(after["phase_number"], 6)

    def test_review_without_binding_rolls_back(self):
        status = self._reach_phase_6()
        status["review"].pop("reviewed_binding")
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="fixture_legacy_review",
                       event_message="fixture: a review recorded before #411", by="test-supervisor")
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_rolled_back(naming="no reviewed binding")
