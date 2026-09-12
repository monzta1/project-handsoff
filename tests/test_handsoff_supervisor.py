#!/usr/bin/env python3
"""Regression tests for Project Handsoff itself.

Every test here maps to either a bug found by hand (run the tool the way
the README says to, on a fresh copy) or a guarantee this rewrite adds.
Stdlib unittest only, no pytest dependency, so these run anywhere Python 3
is available, matching the framework's own "copy a few files into a
project" ethos.

Run: python3 tests/test_handsoff_supervisor.py -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def setUpModule():
    """Most tests below copy this repo's OWN handsoff.toml into a tmp dir
    and string-replace its "commands = []" line to inject a throwaway
    check -- that silently no-ops (not a loud failure) if this repo's own
    template ever legitimately has real commands configured, since real
    users copy this exact file verbatim per the README's Quick Start.
    Fail loudly and explain, rather than let dozens of tests fail
    mysteriously downstream. If you need real check commands to dogfood
    THIS repo, configure a separate, gitignored root instead of the
    shipped template (see "Self-hosting" in README.md).
    """
    text = (ROOT / "handsoff.toml").read_text()
    if "commands = []" not in text:
        raise SystemExit(
            "This repo's own handsoff.toml no longer has 'commands = []'. "
            "It must stay a pristine, generic template -- these tests "
            "depend on that, and so does every real user who copies it "
            "verbatim. Revert it, and track any real dogfooding check "
            "commands in a separate, gitignored root instead."
        )
    # Any test that lands Phase 8 with status complete now triggers a real
    # archive write (see handsoff_lib.archive_run). Sandboxed here at module
    # scope, not just in TestRunArchive, so a test class that reaches
    # completion for an unrelated reason (there are several) can never write
    # into the real ~/Documents/Handsoff-Archive on a developer's machine.
    global _MODULE_ARCHIVE_DIR
    _MODULE_ARCHIVE_DIR = tempfile.mkdtemp(prefix="handsoff-archive-module-")
    os.environ["HANDSOFF_ARCHIVE_DIR"] = _MODULE_ARCHIVE_DIR


def tearDownModule():
    os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
    if _MODULE_ARCHIVE_DIR:
        shutil.rmtree(_MODULE_ARCHIVE_DIR, ignore_errors=True)


_MODULE_ARCHIVE_DIR = None


def run(args, cwd):
    return subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), *args],
                          cwd=cwd, capture_output=True, text=True, timeout=30)


class HandsoffTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-test-"))
        for name in ("handsoff.toml",):
            shutil.copy(ROOT / name, self.tmp / name)
        shutil.copytree(ROOT / "schemas", self.tmp / "schemas")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self, feature="Test feature"):
        r = run(["init", feature], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def read_status(self):
        return json.loads((self.tmp / "handsoff-status.json").read_text())

    def read_acceptance(self):
        return json.loads((self.tmp / "handsoff-acceptance.json").read_text())

    def write_acceptance(self, acceptance):
        (self.tmp / "handsoff-acceptance.json").write_text(json.dumps(acceptance, indent=2))

    def set_criterion_state(self, state, resolved):
        if state == "passing":
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            criterion = run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
            self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
            result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_id = json.loads(result.stdout)["criteria"]["REQ-001"]["run_id"]
            if resolved:
                symptom = run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], cwd=self.tmp)
                self.assertEqual(symptom.returncode, 0, symptom.stdout + symptom.stderr)
        else:
            # Also give it a non-placeholder requirement (state alone
            # would otherwise leave it exactly matching init's default
            # text, which design-approve refuses -- see AR-009/Architect
            # gate); this changes only fixture text, not the state/
            # evidence behavior these tests actually exercise.
            changed = run(["criterion-update", "REQ-001", "--state", state,
                          "--requirement", "A test-fixture criterion under evaluation"], cwd=self.tmp)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)

    def advance_to(self, phase, **extra):
        """Step one phase at a time up to `phase`, as the one-step rule requires."""
        current = self.read_status()["phase_number"]
        last = None
        reviewed_by = extra.pop("reviewed_by", None)
        for n in range(current + 1, phase + 1):
            if n == 3 and self.read_status().get("requires_design_approval") and not self.read_status().get("design_approved"):
                # The Architect gate: every run init flags going forward
                # needs a recorded, non-self human design approval before
                # Phase 3+. Fixture identities here are unrelated to
                # implemented_by/reviewed_by, which the caller controls.
                approval = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                               "--summary", "Test-fixture design approval"], cwd=self.tmp)
                if approval.returncode:
                    return approval
            if n == 6 and reviewed_by:
                review = run(["record-review", "--by", str(reviewed_by)], cwd=self.tmp)
                if review.returncode:
                    return review
            args = ["advance", str(n), str(n * 10)]
            for k, v in extra.items():
                args += [f"--{k.replace('_', '-')}", str(v)]
            last = run(args, cwd=self.tmp)
        return last


class TestQuickStartPaths(HandsoffTestCase):
    """The original bug: the README's own quick-start crashed on a fresh
    copy because paths resolved against bin/, not the project root."""

    def test_status_works_from_project_root_on_a_fresh_copy(self):
        self.init()
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["phase_number"], 1)

    def test_root_resolves_from_a_subdirectory_too(self):
        self.init()
        sub = self.tmp / "some" / "nested" / "cwd"
        sub.mkdir(parents=True)
        r = run(["status"], cwd=sub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestPhaseGateOnProposedState(HandsoffTestCase):
    """The core bug: advancing INTO phase 6 was validated against the
    status BEFORE the write, so the gate could never catch the one
    transition that mattered."""

    def test_advance_into_phase_6_is_blocked_while_criterion_is_failing(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(6, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("SHIP_FEATURE_BLOCKED", r.stdout)
        status = self.read_status()
        self.assertLess(status["phase_number"], 6, "phase must not have advanced")

    def test_advance_into_phase_6_succeeds_once_criterion_is_passing(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        r = self.advance_to(6, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 6)

    def test_dry_run_reports_without_writing(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        self.advance_to(4)
        before = self.read_status()["phase_number"]
        r = run(["advance", "5", "50", "--dry-run"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read_status()["phase_number"], before, "dry run must not write")


class TestSelfApprovalBlocked(HandsoffTestCase):
    def test_same_implementer_and_reviewer_is_blocked_at_phase_6(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="agent-x")
        r = run(["record-review", "--by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("reviewer must differ", r.stdout)

    def test_different_implementer_and_reviewer_is_allowed(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="agent-x")
        review = run(["record-review", "--by", "agent-y"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        r = run(["advance", "6", "80", "--implemented-by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestDeploymentApproval(HandsoffTestCase):
    """The third bug: `advance` to Phase 8 had no idea `deployment-gate`
    existed, so the approval step was skippable outright."""

    def _reach_phase_7(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")

    def test_advance_to_phase_8_blocked_without_approval(self):
        self._reach_phase_7()
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("deployment gate", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 8)

    def test_deployment_gate_refuses_without_explicit_flag(self):
        self._reach_phase_7()
        r = run(["deployment-gate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("AWAITING_EXPLICIT_APPROVAL", r.stdout)

    def test_advance_to_phase_8_succeeds_after_approval(self):
        self._reach_phase_7()
        approve = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)


class TestRoundCaps(HandsoffTestCase):
    def test_review_round_beyond_cap_is_blocked(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="a")
        self.assertEqual(run(["record-review", "--by", "b"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "6", "80", "--implemented-by", "a", "--review-round", "9"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("round cap", r.stdout)


class TestDuplicateKeyDetection(HandsoffTestCase):
    def test_duplicate_key_in_status_file_is_rejected(self):
        self.init()
        raw = (self.tmp / "handsoff-status.json").read_text()
        # inject a real duplicate top-level key
        broken = raw.replace('"progress": 0,', '"progress": 0,\n  "progress": 999,', 1)
        (self.tmp / "handsoff-status.json").write_text(broken)
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", r.stdout)
        self.assertIn("duplicate", r.stdout)


class TestAtomicWrites(HandsoffTestCase):
    def test_no_tmp_file_left_behind_after_a_write(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(3)
        leftovers = list(self.tmp.glob("*.tmp*"))
        self.assertEqual(leftovers, [], f"temp files were not cleaned up: {leftovers}")

    def test_status_file_is_always_valid_json_after_a_write(self):
        self.init()
        self.advance_to(2)
        json.loads((self.tmp / "handsoff-status.json").read_text())  # raises if corrupt


class TestEventLogTamperDetection(HandsoffTestCase):
    def test_intact_log_verifies_clean(self):
        self.init()
        self.advance_to(2)
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("EVENT_LOG_INTACT", r.stdout)

    def test_editing_a_past_event_is_detected(self):
        self.init()
        self.advance_to(2)
        log = self.tmp / "handsoff-events.jsonl"
        lines = log.read_text().splitlines()
        first = json.loads(lines[0])
        first["message"] = "a rewritten history"  # edit in place, do not fix the hash
        lines[0] = json.dumps(first)
        log.write_text("\n".join(lines) + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("TAMPERED", r.stdout)


class TestSchemaValidation(HandsoffTestCase):
    def test_missing_required_status_field_is_caught(self):
        self.init()
        s = self.read_status()
        del s["requirement_coverage"]
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("requirement_coverage", r.stdout)

    def test_criterion_with_no_evidence_or_tests_is_caught(self):
        self.init()
        a = self.read_acceptance()
        a["criteria"][0]["tests"] = []
        a["criteria"][0]["evidence"] = []
        self.write_acceptance(a)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no linked tests or evidence", r.stdout)


class TestOneStepAtATime(HandsoffTestCase):
    def test_cannot_skip_phases(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        r = run(["advance", "5", "50"], cwd=self.tmp)  # from 1 straight to 5
        self.assertEqual(r.returncode, 1)
        self.assertIn("one step at a time", r.stdout)


class TestNoSelfApprovalRequiresBothFields(HandsoffTestCase):
    """Adversarial review finding 1: the original check only fired when
    BOTH fields were set and equal, so leaving implemented_by unset (its
    default) defeated the rule entirely."""

    def test_advance_blocked_when_implemented_by_was_never_set(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5)
        r = run(["advance", "6", "80"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("implemented_by", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 6)

    def test_advance_blocked_when_reviewed_by_was_never_set(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5)
        r = run(["advance", "6", "80", "--implemented-by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("independent review", r.stdout)


class TestDeploymentApprovalCannotBeGrantedEarly(HandsoffTestCase):
    """Adversarial review finding 2: deployment-gate never checked the
    phase, so approval could be granted at Phase 1, before an Implementer
    or Reviewer had touched anything, and would still satisfy Phase 8
    later."""

    def test_approval_refused_before_phase_7(self):
        self.init()
        r = run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Phase 7", r.stdout)
        self.assertIsNone(self.read_status().get("deployment_approved"))

    def test_approval_becomes_stale_if_acceptance_changes_afterward(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        approve = run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout)

        # Mutate the acceptance registry after approval (add a second
        # criterion, still fully green) without going back through the
        # gate. The approval was given against a different registry.
        a = self.read_acceptance()
        a["criteria"][0]["requirement"] = "Changed after approval."
        self.write_acceptance(a)

        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("acceptance file does not match", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 8)

    def test_unchanged_acceptance_keeps_the_approval_valid(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestConcurrentAdvanceDoesNotCorruptState(HandsoffTestCase):
    """Adversarial review finding 3: the lock only wrapped the final
    write, so two concurrent callers could both read the same stale
    state, both validate successfully, and race at the write. This does
    not assert a specific winner (either outcome is legitimate); it
    asserts neither run leaves corrupt JSON or a broken hash chain."""

    def test_two_concurrent_advances_leave_valid_state(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(2)
        design = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                     "--summary", "Test-fixture design approval"], cwd=self.tmp)
        self.assertEqual(design.returncode, 0, design.stdout + design.stderr)
        p1 = subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "advance", "3", "30"],
                              cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "advance", "3", "35"],
                              cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertIn(0, (p1.returncode, p2.returncode), (out1, err1, out2, err2))
        status = self.read_status()  # raises if the JSON is corrupt
        self.assertEqual(status["phase_number"], 3)
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)


class TestConcurrentInitAndVerifyDoNotForkTheEventLog(HandsoffTestCase):
    """Round 2 finding 1: project_lock covered advance/deployment-gate but
    not init or verify, both of which append to the event log. Concurrent
    calls to either forked the hash chain and produced false tamper
    reports on lines nobody had touched."""

    def test_concurrent_init_leaves_one_consistent_project(self):
        procs = [subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "init", f"race-init-{i}"],
                                  cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
        outs = [p.communicate(timeout=30) for p in procs]
        self.assertEqual(sum(1 for p in procs if p.returncode == 0), 1,
                         f"exactly one init should win: {[p.returncode for p in procs]} {outs}")
        status = self.read_status()  # raises if corrupt
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)
        # the event log's feature name must match whichever init actually won
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn(status["feature"], events[0]["message"])

    def test_concurrent_verify_does_not_fork_the_log(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["echo ok"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "echo ok"], cwd=self.tmp).returncode, 0)
        procs = [subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "verify",
                                   "--criterion", "REQ-001", "--by", f"runner-{i}"],
                                  cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(6)]
        for p in procs:
            p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0)
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)


class TestMalformedNumericFieldsRefuseCleanly(HandsoffTestCase):
    """Round 2 finding 2: a non-numeric design_round/review_round/progress
    in a hand-edited status.json crashed the CLI with a raw traceback
    instead of a clean SHIP_FEATURE_BLOCKED."""

    def _corrupt_field(self, field, value):
        s = self.read_status()
        s[field] = value
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))

    def test_non_numeric_design_round_is_refused_cleanly(self):
        self.init()
        self._corrupt_field("design_round", "not-a-number")
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_round", r.stdout)

    def test_non_numeric_progress_is_refused_cleanly_on_status(self):
        self.init()
        self._corrupt_field("progress", "lots")
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class TestNonFiniteNumericFieldsRefuseCleanly(HandsoffTestCase):
    """Round 3 finding: NaN/Infinity are valid JSON-extension floats
    (Python's json.dumps emits them by default, json.loads accepts them
    by default), so isinstance(x, float) alone let them through the
    round-2 numeric-type check, then crashed int()/float() downstream
    (ValueError for NaN, OverflowError for Infinity). Checked through
    BOTH scripts, since the second one had no catch-all and leaked a raw
    traceback. This REPLACES the existing field's value (json.dumps of a
    dict with a real float('nan')/float('inf')) rather than splicing in
    a second copy of a key `init` already writes; appending a duplicate
    key would only exercise the unrelated duplicate-key detector."""

    def _set_field(self, field, value):
        s = self.read_status()
        s[field] = value
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))

    def test_nan_design_round_refused_cleanly_by_supervisor(self):
        self.init()
        self._set_field("design_round", float("nan"))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_round", r.stdout)

    def test_infinity_review_round_refused_cleanly_by_supervisor(self):
        self.init()
        self._set_field("review_round", float("inf"))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("review_round", r.stdout)

    def test_nan_design_round_refused_cleanly_by_second_script(self):
        self.init()
        self._set_field("design_round", float("nan"))
        r = subprocess.run([sys.executable, str(BIN / "validate_handsoff_status.py"), "--root", str(self.tmp)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("SHIP_FEATURE_STATUS_INVALID", r.stdout)
        self.assertIn("design_round", r.stdout)


class TestAcceptanceHashIgnoresCriteriaOrder(HandsoffTestCase):
    """Round 2 finding 3: acceptance_hash was sensitive to array order,
    not just content, so a harmless reordering falsely invalidated a
    valid deployment approval."""

    def test_reordering_two_criteria_does_not_change_the_hash(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        a = [{"id": "REQ-001", "state": "passing"}, {"id": "REQ-002", "state": "passing"}]
        b = [{"id": "REQ-002", "state": "passing"}, {"id": "REQ-001", "state": "passing"}]
        self.assertEqual(lib.acceptance_hash(a), lib.acceptance_hash(b))


class TestSecondValidatorSharesTheSameLogic(HandsoffTestCase):
    """validate_handsoff_status.py must agree with handsoff_supervisor.py
    validate, since both now import the same compute_errors()."""

    def test_both_validators_agree_when_invalid(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        self.advance_to(5)
        s = self.read_status()
        s["phase_number"] = 6  # force an invalid on-disk state directly
        s["phase"] = "Checks & documentation"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r1 = run(["validate"], cwd=self.tmp)
        r2 = subprocess.run([sys.executable, str(BIN / "validate_handsoff_status.py"), "--root", str(self.tmp)],
                            capture_output=True, text=True, timeout=30)
        self.assertEqual(r1.returncode, 1)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("Phase 6", r1.stdout)
        self.assertIn("Phase 6", r2.stdout)


class TestEvidenceAndLiveGates(HandsoffTestCase):
    def test_free_text_cannot_impersonate_verification_evidence(self):
        self.init()
        a = self.read_acceptance()
        a["criteria"][0]["state"] = "passing"
        a["criteria"][0]["evidence"] = ["trust me"]
        self.write_acceptance(a)
        s = self.read_status()
        s["requirement_coverage"] = {"passing": 1, "failing": 0, "not_tested": 0,
                                     "blocked": 0, "original_symptom_resolved": True}
        s["phase_number"] = 6
        s["phase"] = "Checks & documentation"
        s["implemented_by"] = "impl"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("lacks valid checks evidence", r.stdout)

    def test_phase_8_requires_live_run_after_approval(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("successful live verification", r.stdout)

    def test_live_verification_works_when_explicit_approval_is_disabled(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("deployment_requires_explicit_approval = true",
                                                 "deployment_requires_explicit_approval = false"))
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        final = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)

    def test_verification_tail_deletion_is_detected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        (self.tmp / "handsoff-verifications.jsonl").write_text("")
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)

    def test_combined_policy_requires_both_automated_and_browser_evidence(self):
        self.init()
        changed = run(["criterion-update", "REQ-001", "--verification", "automated_and_browser"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        blocked = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("verified evidence", blocked.stdout)
        criterion = self.read_acceptance()["criteria"][0]
        self.assertEqual(criterion["state"], "not_tested",
                          "automated-only evidence must not mark a combined-policy criterion passing")
        browser = run(["record-evidence", "REQ-001", "--kind", "browser",
                       "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(browser.returncode, 0, browser.stdout + browser.stderr)
        approved = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

    def test_command_output_is_not_persisted_in_verification_ledger(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["printf HANDSOFF_%s SECRET"]', 1))
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "printf HANDSOFF_%s SECRET"], cwd=self.tmp).returncode, 0)
        result = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_SECRET", result.stdout)
        self.assertNotIn("HANDSOFF_SECRET", (self.tmp / "handsoff-verifications.jsonl").read_text())

    def test_automated_policy_rejects_manual_attestation(self):
        self.init()
        result = run(["record-evidence", "REQ-001", "--kind", "manual",
                      "--description", "Claimed pass", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not accept manual", result.stdout)

    def test_unrelated_configured_check_cannot_satisfy_a_criterion(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1))
        result = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exactly match", result.stdout)


class TestDerivedAndStrictState(HandsoffTestCase):
    def test_coverage_must_equal_criteria_projection(self):
        self.init()
        s = self.read_status()
        s["requirement_coverage"]["passing"] = 99
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match", r.stdout)

    def test_phase_label_and_progress_range_are_enforced(self):
        self.init()
        s = self.read_status()
        s["phase"] = "Made up"
        s["progress"] = 101
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("phase_number", r.stdout)
        self.assertIn("0 to 100", r.stdout)

    def test_acceptance_mutation_invalidates_review_and_approval(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        changed = run(["criterion-update", "REQ-001", "--requirement", "A newly scoped outcome"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        s = self.read_status()
        self.assertIsNone(s["review"])
        self.assertIsNone(s["deployment_approved"])


class TestAnchoredEventLog(HandsoffTestCase):
    def test_deleting_last_event_is_detected(self):
        self.init()
        self.advance_to(2)
        log = self.tmp / "handsoff-events.jsonl"
        lines = log.read_text().splitlines()
        log.write_text(lines[0] + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)

    def test_unlogged_status_edit_is_detected(self):
        self.init()
        status = self.read_status()
        status["summary"] = "quietly rewritten"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("status file does not match", r.stdout)

    def test_unlogged_acceptance_edit_is_detected(self):
        self.init()
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["requirement"] = "quietly rewritten"
        self.write_acceptance(acceptance)
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("acceptance file does not match", r.stdout)


class TestConfigValidation(HandsoffTestCase):
    def test_state_paths_cannot_escape_project_root(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('status_file = "handsoff-status.json"',
                                                 'status_file = "../escaped.json"'))
        r = run(["init", "unsafe"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("safe relative path", r.stdout)

    def test_commands_must_be_an_array(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = "true"', 1))
        r = run(["init", "bad config"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("array", r.stdout)


class TestVerifyIsolatesUnrelatedCriteria(HandsoffTestCase):
    """The real bug this pass found: `verify` used to run the WHOLE
    [checks].commands list and share one ok/evidence record across every
    named criterion. A criterion's own passing test could be dragged down
    by an unrelated command failing, and (in a batch call) two unrelated
    criteria's outcomes were conflated into one record. Each criterion
    must now be judged strictly by its own configured tests."""

    def _two_criteria(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "commands = []", 'commands = ["true", "false"]', 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp).returncode, 0)
        add = run(["criterion-add", "REQ-002", "--type", "supporting",
                   "--requirement", "A second, unrelated outcome.",
                   "--verification", "automated", "--test", "false"], cwd=self.tmp)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)

    def test_an_unrelated_failing_command_does_not_fail_a_passing_criterion(self):
        self._two_criteria()
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["criteria"]["REQ-001"]["ok"])
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")

    def test_batched_verify_gives_each_criterion_its_own_independent_outcome(self):
        self._two_criteria()
        r = run(["verify", "--criterion", "REQ-001", "--criterion", "REQ-002", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # overall: REQ-002 failed
        payload = json.loads(r.stdout)
        self.assertTrue(payload["criteria"]["REQ-001"]["ok"])
        self.assertFalse(payload["criteria"]["REQ-002"]["ok"])
        self.assertNotEqual(payload["criteria"]["REQ-001"]["run_id"], payload["criteria"]["REQ-002"]["run_id"])
        criteria = {c["id"]: c for c in self.read_acceptance()["criteria"]}
        self.assertEqual(criteria["REQ-001"]["state"], "passing")
        self.assertEqual(criteria["REQ-002"]["state"], "failing")

    def test_an_unrelated_passing_command_does_not_satisfy_a_different_criterion(self):
        self._two_criteria()
        # REQ-002's own test ("false") never gets run or referenced here;
        # verifying REQ-001 alone must not put ANY evidence on REQ-002.
        run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        req002 = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-002")
        self.assertEqual(req002["state"], "not_tested")
        self.assertEqual(req002["evidence"], [])


class TestLiveVerificationOrdering(HandsoffTestCase):
    """The live gate requires the live check to have run AFTER approval,
    not merely that both exist somewhere in the ledger."""

    def test_live_verification_before_approval_does_not_satisfy_phase_8(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 1, live.stdout + live.stderr)
        self.assertIn("Phase 7", live.stdout + live.stderr)


class TestLiveAndApprovalConfigCombinations(HandsoffTestCase):
    """Every combination of require_live_verification and
    deployment_requires_explicit_approval must reach Phase 8; disabling a
    gate must not make the workflow impossible to finish."""

    def _disable(self, *flags):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        for flag in flags:
            text = text.replace(f"{flag} = true", f"{flag} = false")
        toml.write_text(text)

    def test_both_gates_disabled_reaches_phase_8(self):
        self._disable("require_live_verification", "deployment_requires_explicit_approval")
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)

    def test_approval_required_but_live_disabled_reaches_phase_8(self):
        self._disable("require_live_verification")
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestCheckTimeout(HandsoffTestCase):
    def test_a_hanging_check_times_out_cleanly_rather_than_hanging(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["sleep 5"]', 1)
                        .replace("timeout_seconds = 600", "timeout_seconds = 1", 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "sleep 5"], cwd=self.tmp).returncode, 0)
        started = __import__("time").time()
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        elapsed = __import__("time").time() - started
        self.assertLess(elapsed, 10, "the 1-second configured timeout should have fired well before 10s")
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["results"][0]["exit_code"], 124)

    def test_timeout_seconds_must_be_a_positive_integer(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("timeout_seconds = 600", "timeout_seconds = -1", 1))
        r = run(["init", "bad timeout"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timeout_seconds", r.stdout)


class TestPartialVerificationLedgerTailDeletion(HandsoffTestCase):
    """The earlier test only covered truncating the WHOLE verification
    ledger to empty. Deleting just the last record (leaving an internally
    consistent but short chain) must be caught too, the same way a
    deleted-tail event log record is."""

    def test_deleting_the_last_verification_record_is_detected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        log = self.tmp / "handsoff-verifications.jsonl"
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1)
        log.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)


class TestHalfCompletedCrossFileWrite(HandsoffTestCase):
    """Simulates a crash between the acceptance write and the status write
    inside a mutating command: acceptance now shows a passing criterion,
    but status's derived coverage and the event log both still reflect the
    old, pre-write world. This must be reported, not silently accepted."""

    def test_acceptance_ahead_of_status_is_detected(self):
        self.init()
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["state"] = "passing"
        self.write_acceptance(acceptance)  # acceptance.json changed...
        # ...but handsoff-status.json and the event log were NOT touched,
        # exactly the shape of a crash between two of a command's writes.
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(
            any("does not match" in e for e in json.loads(
                subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "status"],
                               cwd=self.tmp, capture_output=True, text=True, timeout=30).stdout
            ).get("errors", [])),
            "expected a coverage or audit mismatch to be reported")


class TestIdentityAndRecordTypeValidation(HandsoffTestCase):
    """Requirement: strictly validate identities, review records, and
    approval records, not just the coverage counters and phase fields."""

    def test_non_string_implemented_by_is_rejected(self):
        self.init()
        s = self.read_status()
        s["implemented_by"] = 12345
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("implemented_by", r.stdout)

    def test_review_record_missing_by_is_rejected(self):
        self.init()
        s = self.read_status()
        s["review"] = {"at": "2026-01-01T00:00:00+00:00", "acceptance_hash": "x"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("review.by", r.stdout)

    def test_deployment_approved_with_naive_timestamp_is_rejected(self):
        self.init()
        s = self.read_status()
        s["deployment_approved"] = {"by": "owner", "at": "2026-01-01T00:00:00", "acceptance_hash": "x"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timezone", r.stdout)


class TestNextActionStaysCurrent(HandsoffTestCase):
    """Found during the end-to-end run: next_action was set once at init
    and never touched again, so a completed Phase 8 project still read
    'reproduce the original symptom', a self-contradictory status."""

    def test_advance_updates_next_action_to_a_phase_appropriate_default(self):
        self.init()
        before = self.read_status()["next_action"]
        self.set_criterion_state("passing", resolved=True)
        run(["advance", "2", "20"], cwd=self.tmp)
        after = self.read_status()["next_action"]
        self.assertNotEqual(before, after)
        self.assertIn("design", after.lower())

    def test_next_action_can_be_overridden_explicitly(self):
        self.init()
        run(["advance", "2", "20", "--next-action", "Custom next step"], cwd=self.tmp)
        self.assertEqual(self.read_status()["next_action"], "Custom next step")

    def test_a_completed_workflow_does_not_still_say_reproduce_the_symptom(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(self.read_status()["phase_number"], 8)
        self.assertNotIn("reproduce the original symptom", self.read_status()["next_action"])


class TestEmptyIdentitiesAndFeaturesRejected(HandsoffTestCase):
    """Round 3 finding 1: an empty string is a valid str, so a naive
    `if not args.by:` style check (or none at all) let '' through as a
    real actor or feature name. init "" also used to write a permanently
    unrecoverable project, since a later real init then found existing
    artifacts and refused."""

    def test_init_with_empty_feature_is_rejected(self):
        r = run(["init", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())
        retry = run(["init", "a real feature"], cwd=self.tmp)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)

    def test_init_with_whitespace_only_feature_is_rejected(self):
        r = run(["init", "   "], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_verify_with_empty_by_is_rejected(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        r = run(["verify", "--criterion", "REQ-001", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_evidence_with_empty_by_is_rejected(self):
        self.init()
        r = run(["record-evidence", "REQ-001", "--kind", "manual", "--description", "checked", "--by", ""],
               cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_evidence_with_empty_description_is_rejected(self):
        self.init()
        r = run(["record-evidence", "REQ-001", "--kind", "manual", "--description", "", "--by", "someone"],
               cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_review_with_empty_by_is_rejected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        r = run(["record-review", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_verify_live_with_empty_by_is_rejected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        r = run(["verify-live", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_hand_crafted_verification_record_with_empty_actor_is_detected(self):
        """A record can chain and hash perfectly while still being
        structurally empty; cryptographic authentication alone would
        wave it through. It must be caught on load, not just on write."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        log = lib.verification_log_path(self.tmp, cfg)
        prev_hash = lib._last_hash(log)
        body = {"run_id": "vr-forged", "at": "2026-01-01T00:00:00+00:00", "kind": "manual",
               "ok": True, "by": "", "criteria": ["REQ-001"],
               "criterion_hashes": {}, "results": [], "description": "forged",
               "acceptance_hash": None, "config_hash": None, "prev_hash": prev_hash}
        body["hash"] = __import__("hashlib").sha256(
            (lib._canonical(body) + prev_hash).encode("utf-8")).hexdigest()
        with log.open("a", encoding="utf-8") as fh:
            fh.write(lib._canonical(body) + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("'by' must be a non-empty string", r.stdout)


class TestConfigurationChainOfTrust(HandsoffTestCase):
    """Round 3 finding 2: handsoff.toml sat outside the chain of trust.
    Review, deployment approval, and live verification are each bound to
    a hash of the governance-relevant config keys at the moment they were
    granted; changing those keys afterward must invalidate the decision,
    the same way changing the acceptance registry already did."""

    def _flip_a_governance_key(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("max_review_rounds = 3", "max_review_rounds = 7"))

    def test_review_is_invalidated_when_governance_config_changes_after_review(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        review = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        ok = run(["advance", "6", "60"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self._flip_a_governance_key()
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed since review", r.stdout)

    def test_deployment_and_live_decisions_are_invalidated_when_governance_config_changes(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        final = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        self._flip_a_governance_key()
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed", r.stdout)

    def test_verify_live_refuses_when_config_changed_before_it_was_even_invoked(self):
        """Config already stale by the time verify-live starts: caught
        by the FIRST comparison, against the approval's own config_hash.
        This does not exercise the mid-run race (see the isolated test
        below); a fresh process reloads handsoff.toml at startup either
        way, so this case was never at risk."""
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]')
                        .replace("live_commands = []", 'live_commands = ["true"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self._flip_a_governance_key()
        r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed since deployment approval", r.stdout)

    def test_verify_live_detects_a_config_change_that_lands_while_the_check_is_running(self):
        """Round 3 review finding: the SECOND comparison used to hash
        the same in-memory `cfg` object against itself, so it could
        never detect a real race no matter what changed on disk. Here
        the live check itself edits handsoff.toml as a side effect,
        landing the change in the window between the two lock
        acquisitions inside a SINGLE verify-live invocation, the one
        shape the old code could not see."""
        flipper = self.tmp / "flip_config.py"
        flipper.write_text(
            "from pathlib import Path\n"
            "p = Path('handsoff.toml')\n"
            "p.write_text(p.read_text().replace('max_review_rounds = 3', 'max_review_rounds = 9'))\n"
        )
        toml = self.tmp / "handsoff.toml"
        # "commands = []" is a substring of "live_commands = []" too, so
        # the first replace must be count-limited or it also clobbers
        # the live_commands line before the second replace ever sees it.
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1)
                        .replace("live_commands = []", f'live_commands = ["{sys.executable} flip_config.py"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("workflow policy changed during live verification", r.stdout)


class TestSymlinkPathContainment(HandsoffTestCase):
    """Round 3 finding 3: the old check only rejected literal '..' and
    absolute paths in a configured state-file path. A relative path
    through a symlinked directory passed that check yet still resolved
    outside the project root."""

    def test_status_file_cannot_escape_root_via_a_symlinked_directory(self):
        outside = Path(tempfile.mkdtemp(prefix="handsoff-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = self.tmp / "escape"
        os.symlink(outside, link, target_is_directory=True)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            'status_file = "handsoff-status.json"', 'status_file = "escape/status.json"'))
        r = run(["init", "symlink escape attempt"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("resolves outside", r.stdout)
        self.assertFalse((outside / "status.json").exists())


class TestCombinedPolicyEvidenceCompletion(HandsoffTestCase):
    """Round 3 finding 4 (medium): a combined automated_and_browser
    criterion used to flip to 'passing' the moment EITHER evidence kind
    landed. Passing must only be derived once every required kind for
    the criterion's policy has a valid, current record."""

    def test_browser_only_evidence_does_not_mark_a_combined_criterion_passing(self):
        self.init()
        self.assertEqual(
            run(["criterion-update", "REQ-001", "--verification", "automated_and_browser"], cwd=self.tmp).returncode, 0)
        r = run(["record-evidence", "REQ-001", "--kind", "browser",
                "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "not_tested")

    def test_automated_only_evidence_does_not_mark_a_combined_criterion_passing(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--verification", "automated_and_browser",
                              "--test", "true"], cwd=self.tmp).returncode, 0)
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "not_tested")

    def test_criterion_becomes_passing_only_once_both_kinds_are_present(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--verification", "automated_and_browser",
                              "--test", "true"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp).returncode, 0)
        r = run(["record-evidence", "REQ-001", "--kind", "browser",
                "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")


class TestConfiguredCheckOrderPreserved(HandsoffTestCase):
    """Smaller improvement 1: verify used to alphabetize the set of
    needed test commands, silently reordering them relative to
    [checks].commands. Configured order carries operator intent (e.g.
    cheap smoke checks before a slow suite) and must be preserved."""

    def test_verify_runs_checks_in_configured_order_not_alphabetical(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "commands = []", 'commands = ["echo zebra", "echo apple"]', 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "echo zebra"], cwd=self.tmp).returncode, 0)
        add = run(["criterion-add", "REQ-002", "--type", "supporting",
                  "--requirement", "A second outcome.", "--verification", "automated",
                  "--test", "echo apple"], cwd=self.tmp)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        r = run(["verify", "--criterion", "REQ-001", "--criterion", "REQ-002", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        commands_run = [result["command"] for result in json.loads(r.stdout)["results"]]
        self.assertEqual(commands_run, ["echo zebra", "echo apple"],
                         "checks must run in [checks].commands order, not alphabetically sorted")


class TestDoctorRecoveryCommand(HandsoffTestCase):
    """Smaller improvement 2: the README documented that an interrupted
    cross-file write fails closed but offered no supported way to
    recover. `doctor` closes that gap for two specific, provably safe
    shapes: a stale verification_head anchor (self-authenticating from
    the ledger), and an event log that has not yet recorded a
    write-ahead-journal-confirmed status/acceptance write. A bare hand
    edit that merely happens to still validate is NOT one of those
    shapes and must be refused, even though it looks identical on disk
    to a real interrupted write (round 3 finding: doctor could otherwise
    launder an untracked edit into the audit trail as if it were a
    crash)."""

    def test_doctor_reports_ok_when_nothing_to_recover(self):
        self.init()
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DOCTOR_OK", r.stdout)

    def test_doctor_recovers_a_verification_head_left_behind_by_a_crash(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        criterion = self.read_acceptance()["criteria"][0]
        with lib.project_lock(self.tmp):
            # Ledger append succeeds and is durable; the status.json write
            # that would re-anchor verification_head to it never happens,
            # exactly the shape of a crash between the two.
            lib.append_verification(self.tmp, cfg, kind="manual", ok=True, by="side-channel",
                                    criteria=[criterion], description="out of band")
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("anchored head", broken.stdout)
        dry = run(["doctor", "--dry-run"], cwd=self.tmp)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("WOULD_RECOVER", dry.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 1, "dry run must not write anything")
        fixed = run(["doctor"], cwd=self.tmp)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn("RECOVERED", fixed.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_doctor_recovers_a_journal_confirmed_write_left_behind_by_a_crash(self):
        """The genuine recoverable shape: a real command's write-ahead
        journal entry proves the current status content was its
        intended, in-flight output; only the append_event step after it
        never happened. This reproduces exactly what commit() does up to
        (not including) that last step."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["next_action"] = "Advanced by a command that crashed after writing files"
        with lib.project_lock(self.tmp):
            lib.write_ahead(self.tmp, status=status)
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("does not match the state recorded by the latest event", broken.stdout)
        fixed = run(["doctor"], cwd=self.tmp)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn("RECOVERED", fixed.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["next_action"],
                         "Advanced by a command that crashed after writing files")

    def test_doctor_refuses_to_recover_an_unproven_hand_edit(self):
        """The critical case: an edit made directly to status.json, with
        no command and no write-ahead journal entry behind it, must not
        be recovered just because it happens to still validate. A crash
        and a hand edit are indistinguishable on disk; only the journal
        tells them apart, and here there isn't one."""
        self.init()
        status = self.read_status()
        status["next_action"] = "Hand-edited directly, no command ever ran"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("does not match the state recorded by the latest event", broken.stdout)
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)
        self.assertIn("write-ahead journal", r.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 1,
                         "a refused recovery must not have written anything")
        self.assertEqual(self.read_status()["next_action"], "Hand-edited directly, no command ever ran")

    def test_doctor_refuses_when_the_event_log_chain_is_broken(self):
        self.init()
        log = self.tmp / "handsoff-events.jsonl"
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        log.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""))
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)

    def test_doctor_refuses_to_paper_over_an_independently_invalid_state(self):
        self.init()
        status = self.read_status()
        status["progress"] = 150  # invalid regardless of ledger freshness
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)

    def test_doctor_refuses_a_journal_confirmed_write_that_still_does_not_validate(self):
        """Even with a genuine write-ahead journal entry proving intent,
        doctor must not anchor the log to a state that fails its own
        gates: the journal proves a write was in flight, not that its
        content was legitimate."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["progress"] = 150
        with lib.project_lock(self.tmp):
            lib.write_ahead(self.tmp, status=status)
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)
        self.assertIn("does not independently validate", r.stdout)


class TestRunArchive(HandsoffTestCase):
    """Landing Phase 8 complete writes one self-contained JSON record to a
    centralized archive outside any project repo, so what Handsoff learns
    about ITSELF survives that repo's own cleanup and can be read across
    every project it has ever run in. Automatic, not a step a Supervisor
    session has to remember."""

    def setUp(self):
        super().setUp()
        self.archive_dir = Path(tempfile.mkdtemp(prefix="handsoff-archive-test-"))
        self._old_env = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.archive_dir)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self._old_env
        shutil.rmtree(self.archive_dir, ignore_errors=True)
        super().tearDown()

    def _complete_a_run(self, feature="Archive test feature"):
        self.init(feature)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_completing_a_run_writes_exactly_one_archive_file(self):
        self._complete_a_run()
        files = list(self.archive_dir.glob("*.json"))
        self.assertEqual(len(files), 1, files)

    def test_the_archive_reports_its_own_path(self):
        r = self._complete_a_run()
        self.assertIn("HANDSOFF_ARCHIVED:", r.stdout)

    def test_the_archived_record_is_a_faithful_self_contained_copy(self):
        self._complete_a_run(feature="Archive content test")
        record = json.loads(next(self.archive_dir.glob("*.json")).read_text())
        self.assertEqual(record["repo"], self.tmp.name)
        self.assertEqual(record["feature"], "Archive content test")
        self.assertEqual(record["status"]["phase_number"], 8)
        self.assertEqual(record["status"]["status"], "complete")
        self.assertTrue(record["acceptance"]["criteria"])
        self.assertTrue(record["verifications"])
        self.assertTrue(record["events"])
        self.assertIn("archived_at", record)

    def test_archiving_only_happens_at_completion_not_every_phase(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(4, implemented_by="impl-1")
        self.assertEqual(list(self.archive_dir.glob("*.json")), [])

    def test_archive_dir_is_created_if_missing(self):
        shutil.rmtree(self.archive_dir)
        self._complete_a_run()
        self.assertTrue(self.archive_dir.is_dir())
        self.assertEqual(len(list(self.archive_dir.glob("*.json"))), 1)

    def test_two_different_repos_archive_to_the_same_place_without_colliding(self):
        self._complete_a_run(feature="First repo run")
        other = Path(tempfile.mkdtemp(prefix="handsoff-test-other-"))
        try:
            for name in ("handsoff.toml",):
                shutil.copy(ROOT / name, other / name)
            shutil.copytree(ROOT / "schemas", other / "schemas")
            real_tmp, self.tmp = self.tmp, other
            self._complete_a_run(feature="Second repo run")
            self.tmp = real_tmp
        finally:
            shutil.rmtree(other, ignore_errors=True)
        self.assertEqual(len(list(self.archive_dir.glob("*.json"))), 2)


class TestActivityAwareStallDetection(HandsoffTestCase):
    """The ir-command B3 dogfooding bug: a run doing hours of legitimate
    background work (a long design review, which had even scheduled its
    own fallback heartbeat) was flagged 'stalled' because stall detection
    read only the status file's updated_at mtime. Fix: stall_warning()
    now reads the FRESHEST of updated_at and a new, optional
    last_heartbeat_at field, written by a new `heartbeat` command; a
    fresh heartbeat suppresses a false stall, while a run with neither
    signal current is still correctly flagged. See README "Known
    limitations"."""

    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _ago(self, minutes, delta=timedelta()):
        return (self.NOW - timedelta(minutes=minutes) - delta).isoformat()

    def _real_ago(self, minutes):
        """For tests that go through the real CLI (which always reads the
        real wall clock, not an injectable `now`): a genuinely past
        timestamp relative to the actual current time, not a sleep."""
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def _write_status(self, status):
        """Write a status dict directly (to simulate time having passed
        without a real advance/heartbeat call) and re-anchor the event log
        to it exactly the way commit() would, so the write reads as
        legitimate rather than tripping the (unrelated) tamper detector --
        this test simulates a normal passage of time, not a corrupted
        file, so it must not exercise that separate guarantee."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted status timestamps directly")

    def test_fresh_heartbeat_suppresses_stall_warning(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = {"stall_minutes": 10}
        status = {"status": "in_progress", "updated_at": self._ago(260), "last_heartbeat_at": self._ago(2)}
        self.assertIsNone(lib.stall_warning(status, cfg, now=self.NOW))
        note = lib.activity_note(status, cfg, now=self.NOW)
        self.assertIsNotNone(note)
        self.assertIn("background task", note)

    def test_no_heartbeat_and_stale_status_still_stalled(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = {"stall_minutes": 10}
        # No heartbeat field at all.
        no_heartbeat = {"status": "in_progress", "updated_at": self._ago(15)}
        self.assertIsNotNone(lib.stall_warning(no_heartbeat, cfg, now=self.NOW))
        self.assertIsNone(lib.activity_note(no_heartbeat, cfg, now=self.NOW))
        # Heartbeat present but itself stale: still a real stall.
        stale_heartbeat = {"status": "in_progress", "updated_at": self._ago(15), "last_heartbeat_at": self._ago(20)}
        self.assertIsNotNone(lib.stall_warning(stale_heartbeat, cfg, now=self.NOW))
        # Boundary: exactly stall_minutes old still counts as fresh (the
        # existing strict '>' comparison, not '>='), so this must NOT stall.
        at_boundary = {"status": "in_progress", "updated_at": self._ago(10)}
        self.assertIsNone(lib.stall_warning(at_boundary, cfg, now=self.NOW))
        # One second past the boundary must stall.
        past_boundary = {"status": "in_progress", "updated_at": self._ago(10, delta=timedelta(seconds=1))}
        self.assertIsNotNone(lib.stall_warning(past_boundary, cfg, now=self.NOW))

    def test_stall_and_busy_state_never_block_advance_or_status(self):
        self.init()
        # Genuinely stalled: no heartbeat, updated_at stale.
        s = self.read_status()
        s["updated_at"] = self._real_ago(20)
        self._write_status(s)
        status_r = run(["status"], cwd=self.tmp)
        self.assertEqual(status_r.returncode, 0, status_r.stdout + status_r.stderr)
        self.assertIsNotNone(json.loads(status_r.stdout)["stall_warning"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0, "a stall warning must not block validate")
        same_state = run(["advance", "1", "0"], cwd=self.tmp)
        self.assertEqual(same_state.returncode, 0, same_state.stdout + same_state.stderr)

        # Busy background task: fresh heartbeat, updated_at still stale.
        s2 = self.read_status()
        s2["updated_at"] = self._real_ago(20)
        s2["last_heartbeat_at"] = self._real_ago(1)
        self._write_status(s2)
        status_r2 = run(["status"], cwd=self.tmp)
        self.assertEqual(status_r2.returncode, 0, status_r2.stdout + status_r2.stderr)
        payload2 = json.loads(status_r2.stdout)
        self.assertIsNone(payload2["stall_warning"])
        self.assertIsNotNone(payload2["activity_note"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0, "a busy state must not block validate")
        self.assertEqual(run(["advance", "1", "0"], cwd=self.tmp).returncode, 0, "a busy state must not block advance")

    def test_heartbeat_command_records_liveness_without_mutating_progress(self):
        self.init()
        before = self.read_status()
        r = run(["heartbeat", "--by", "impl-1", "--note", "long design review running"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HEARTBEAT_RECORDED", r.stdout)
        after = self.read_status()
        self.assertEqual(after["phase_number"], before["phase_number"])
        self.assertEqual(after["progress"], before["progress"])
        self.assertEqual(after["updated_at"], before["updated_at"], "heartbeat must not touch updated_at")
        self.assertIsNotNone(after["last_heartbeat_at"])
        self.assertNotEqual(after["last_heartbeat_at"], before.get("last_heartbeat_at"))
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"heartbeat"', events_text)
        self.assertIn("long design review running", events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)
        missing_by = run(["heartbeat"], cwd=self.tmp)
        self.assertNotEqual(missing_by.returncode, 0)
        self.assertNotIn("Traceback", missing_by.stdout + missing_by.stderr)

    def test_dashboard_shows_distinct_busy_label_and_still_shows_stalled_banner(self):
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dash
        self.init()
        # Busy background task.
        s = self.read_status()
        s["updated_at"] = self._real_ago(20)
        s["last_heartbeat_at"] = self._real_ago(1)
        self._write_status(s)
        snapshot = dash.build_snapshot(self.tmp)
        self.assertTrue(snapshot["initialized"])
        self.assertIsNotNone(snapshot["activity_note"])
        supervisor = snapshot["supervisor"]
        self.assertNotEqual(supervisor["label"], "On course")
        self.assertNotIn("stalled", supervisor["headline"].lower())
        self.assertIn("background", (supervisor["label"] + supervisor["headline"]).lower())

        # Genuinely stalled: no heartbeat at all.
        s2 = self.read_status()
        s2["updated_at"] = self._real_ago(20)
        s2.pop("last_heartbeat_at", None)
        self._write_status(s2)
        snapshot2 = dash.build_snapshot(self.tmp)
        supervisor2 = snapshot2["supervisor"]
        self.assertIsNone(snapshot2["activity_note"])
        self.assertIn("stalled", supervisor2["headline"].lower())
        self.assertTrue(any("no update" in item for item in supervisor2["attention"]),
                        supervisor2["attention"])

    def test_missing_heartbeat_field_is_backward_compatible(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.init()
        s = self.read_status()
        self.assertIn("last_heartbeat_at", s)  # init writes it as None going forward
        del s["last_heartbeat_at"]  # simulate a status.json from BEFORE this fix
        self._write_status(s)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        without_field = dict(s)
        without_field["status"] = "in_progress"
        without_field["updated_at"] = self._ago(20)
        with_null_field = dict(without_field)
        with_null_field["last_heartbeat_at"] = None
        cfg = {"stall_minutes": 10}
        self.assertEqual(lib.stall_warning(without_field, cfg, now=self.NOW),
                         lib.stall_warning(with_null_field, cfg, now=self.NOW))
        self.assertIsNotNone(lib.stall_warning(without_field, cfg, now=self.NOW))

    def test_malformed_heartbeat_field_rejected_by_schema(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.init()
        s = self.read_status()
        s["last_heartbeat_at"] = "not-a-timestamp"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("last_heartbeat_at", r.stdout)

        s2 = self.read_status()
        s2["last_heartbeat_at"] = datetime(2026, 1, 1, 12, 0, 0).isoformat()  # no timezone
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s2))
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1)
        self.assertNotIn("Traceback", r2.stdout + r2.stderr)
        self.assertIn("last_heartbeat_at", r2.stdout)

        cfg = {"stall_minutes": 10}
        bad_status = {"status": "in_progress", "updated_at": self._ago(20), "last_heartbeat_at": "not-a-timestamp"}
        self.assertIsNotNone(lib.stall_warning(bad_status, cfg, now=self.NOW),
                            "a malformed heartbeat must never be read as fresh")


class TestArchitectDesignApprovalGate(HandsoffTestCase):
    """The Architect role's core (AR1-AR3): /ship-feature opens with an
    Architect that collaboratively authors design + testable criteria,
    then a human -- never the Architect itself -- must explicitly
    approve before Phase 3+ opens. `design-approve` records that
    approval; Phase 3+ refuses to advance for any run `init` flagged
    `requires_design_approval` without one bound to the current
    acceptance hash, naming a human approver distinct from the
    architect. A run from before this feature (missing the flag) is
    completely unaffected -- this changes the ENTRY for new runs, not
    the existing phases."""

    def _new_project_root(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-test-architect-"))
        shutil.copy(ROOT / "handsoff.toml", root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", root / "schemas")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _author_real_criterion(self, requirement="A real, specific, observable outcome",
                               test="pytest tests/test_real.py -q", root=None):
        root = root or self.tmp
        r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _write_status(self, status, root=None):
        """Write a status dict directly (to simulate a hand-crafted or
        pre-existing state no real command would produce) and re-anchor
        the event log to it exactly the way commit() would, so the write
        reads as legitimate rather than tripping the unrelated tamper
        detector -- these tests simulate specific states, not corruption."""
        root = root or self.tmp
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            lib.atomic_write_json(lib.status_path(root, cfg), status)
            lib.append_event(root, cfg, "test_backdate", "test harness adjusted status fields directly")

    def test_phase_3_blocked_without_non_self_design_approval(self):
        self.init()
        self.assertTrue(self.read_status()["requires_design_approval"])
        self._author_real_criterion()
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)

        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("design gate", blocked.stdout)
        self.assertEqual(self.read_status()["phase_number"], 2)

        self_approve = run(["design-approve", "--by", "arch-1", "--architect", "arch-1",
                            "--summary", "Approach and tradeoffs"], cwd=self.tmp)
        self.assertEqual(self_approve.returncode, 1)
        self.assertIn("self-approval", self_approve.stdout)
        self.assertIsNone(self.read_status()["design_approved"])

        # Gate-level self-approval refusal, independent of the command's
        # own check: hand-craft a self-approved record directly (bypassing
        # cmd_design_approve entirely) and confirm advance still refuses.
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(), "by": "same-id", "architect": "same-id",
            "design_hash": lib.design_hash(acceptance["criteria"]), "config_hash": lib.config_hash(cfg),
        }
        self._write_status(status)
        bypass = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(bypass.returncode, 1, bypass.stdout + bypass.stderr)
        self.assertIn("design gate", bypass.stdout)
        self.assertIn("self-approval", bypass.stdout)

        approve = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                      "--summary", "Approach and tradeoffs"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        ok = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)

    def test_design_approve_command_records_and_refuses_self_approval(self):
        self.init()
        self._author_real_criterion()
        for args, expect_substr in (
            (["design-approve", "--by", "", "--architect", "a", "--summary", "s"], "--by"),
            (["design-approve", "--by", "h", "--architect", "  ", "--summary", "s"], "--architect"),
            (["design-approve", "--by", "h", "--architect", "a", "--summary", "   "], "--summary"),
            (["design-approve", "--by", "same", "--architect", "same", "--summary", "s"], "self-approval"),
            # Whitespace/case variants of the same identity must not slip
            # past the self-approval check -- the load-bearing guarantee
            # this feature exists for.
            (["design-approve", "--by", "moncy ", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", " moncy", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", "Moncy", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", "MONCY", "--architect", "moncy", "--summary", "s"], "self-approval"),
        ):
            r = run(args, cwd=self.tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(expect_substr, r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIsNone(self.read_status()["design_approved"])

        # The gate-level re-check (independent of the command's own
        # refusal) must also catch a whitespace/case-varied self-approval
        # hand-crafted directly into status.json, not only byte-identical
        # strings.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(), "by": "Moncy ", "architect": "moncy",
            "design_hash": lib.design_hash(acceptance["criteria"]), "config_hash": lib.config_hash(cfg),
        }
        self._write_status(status)
        bypass = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(bypass.returncode, 1, bypass.stdout + bypass.stderr)
        self.assertIn("self-approval", bypass.stdout)

        r = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                "--summary", "Approach: X. Tradeoffs: Y. Decisions: Z."], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESIGN_APPROVAL_RECORDED", r.stdout)
        status = self.read_status()
        record = status["design_approved"]
        self.assertEqual(record["by"], "moncy")
        self.assertEqual(record["architect"], "arch-1")
        self.assertTrue(record["at"])
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        self.assertEqual(record["design_hash"], lib.design_hash(acceptance["criteria"]))
        self.assertEqual(record["config_hash"], lib.config_hash(cfg))
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"design_approved"', events_text)
        self.assertIn("Approach: X. Tradeoffs: Y. Decisions: Z.", events_text)

    def test_criterion_mutation_invalidates_design_approval_and_rolls_back_to_phase_2(self):
        for phase in (3, 4, 5):
            root = self._new_project_root()
            self.assertEqual(run(["init", "Test feature"], cwd=root).returncode, 0)
            toml = root / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            self._author_real_criterion(root=root)
            self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                                  "--summary", "s"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "2", "20"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "3", "30"], cwd=root).returncode, 0)
            if phase >= 4:
                self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=root).returncode, 0)
            if phase >= 5:
                self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=root).returncode, 0)

            status = json.loads((root / "handsoff-status.json").read_text())
            self.assertEqual(status["phase_number"], phase)
            self.assertIsNotNone(status["design_approved"])

            r = run(["criterion-update", "REQ-001", "--requirement", "A changed outcome"], cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = json.loads((root / "handsoff-status.json").read_text())
            self.assertIsNone(after["design_approved"])
            self.assertEqual(after["phase_number"], 2, f"starting phase {phase} should roll back to 2")

    def test_pre_existing_runs_without_the_new_field_are_unaffected(self):
        self.init()
        self._author_real_criterion()
        status = self.read_status()
        del status["requires_design_approval"]
        self._write_status(status)
        self.assertNotIn("requires_design_approval", self.read_status())

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_full_pre_existing_suite_unmodified_and_passing(self):
        """Re-affirms 2 pre-existing pipeline invariants -- self-approval
        blocked at review, deployment approval blocked before Phase 7 --
        hold byte-for-byte for a run that went through the new Architect
        gate. The broader 'nothing else in this file changed' half of
        this guarantee is structural: every pre-existing test class above
        this one is untouched by this feature's diff."""
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(test="true")
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        self_review = run(["record-review", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(self_review.returncode, 1)
        self.assertIn("reviewer must differ", self_review.stdout)

        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        early_deploy = run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        self.assertEqual(early_deploy.returncode, 1)
        self.assertIn("Phase 7", early_deploy.stdout)

        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        live_r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live_r.returncode, 0, live_r.stdout + live_r.stderr)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["status"], "complete")

    def test_end_to_end_architect_flow_through_existing_pipeline(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(requirement="The sample feature does the observable thing.", test="true")

        refused = run(["design-approve", "--by", "arch-1", "--architect", "arch-1",
                       "--summary", "Approach"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1)
        self.assertIsNone(self.read_status()["design_approved"])

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                       "--summary", "Approach: do X. Tradeoffs: Y vs Z. Decision: Y."], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("SHIP_FEATURE_VALID", validated.stdout)

    def test_design_approve_refuses_on_untouched_placeholder_criteria(self):
        self.init()
        r = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("placeholder", r.stdout)
        self.assertIsNone(self.read_status()["design_approved"])

        self._author_real_criterion()
        ok = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_pre_existing_run_rollback_cascade_unaffected_by_new_gate(self):
        for phase in (4, 5):
            root = self._new_project_root()
            self.assertEqual(run(["init", "Test feature"], cwd=root).returncode, 0)
            toml = root / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            self._author_real_criterion(root=root)
            self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                                  "--summary", "s"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "2", "20"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "3", "30"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=root).returncode, 0)
            if phase == 5:
                self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=root).returncode, 0)

            status = json.loads((root / "handsoff-status.json").read_text())
            self.assertEqual(status["phase_number"], phase)
            status.pop("requires_design_approval", None)
            self._write_status(status, root=root)

            r = run(["criterion-update", "REQ-001", "--requirement", "A changed outcome"], cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = json.loads((root / "handsoff-status.json").read_text())
            # Pre-existing behavior: _invalidate_decisions only forces a
            # phase rollback when phase_number >= 6 (rollback_to=4); at
            # phase 4 or 5 it leaves phase_number untouched today. An
            # unflagged run must see that exact same behavior, not the
            # new phase-2 force-rollback this feature adds for flagged runs.
            self.assertEqual(after["phase_number"], phase,
                            f"unflagged run starting at phase {phase} must be left exactly as before this "
                            "feature (unchanged), not force-rolled to phase 2")

    def test_malformed_design_approved_rejected_by_schema(self):
        self.init()
        self._author_real_criterion()
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)

        status = self.read_status()
        status["design_approved"]["architect"] = ""
        self._write_status(status)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_approved.architect", r.stdout)

        status2 = self.read_status()
        status2["design_approved"]["at"] = "not-a-timestamp"
        self._write_status(status2)
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("design_approved.at", r2.stdout)

    def test_evidence_recording_never_invalidates_design_approval(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(test="true")
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        before = self.read_status()
        self.assertIsNotNone(before["design_approved"])
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        after = self.read_status()
        self.assertEqual(after["design_approved"], before["design_approved"])
        self.assertEqual(after["phase_number"], 4)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertIsNotNone(self.read_status()["design_approved"])

        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "80", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        design_before_p6_evidence = self.read_status()["design_approved"]

        # Re-running verify at phase 6 must still re-invalidate review per
        # the pre-existing rollback_to=5 cascade (unchanged regression
        # coverage), while design_approved -- the new field -- survives.
        verify_again = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_again.returncode, 0, verify_again.stdout + verify_again.stderr)
        after_p6 = self.read_status()
        self.assertIsNone(after_p6["review"], "pre-existing rollback_to=5 behavior must still clear review")
        self.assertEqual(after_p6["phase_number"], 5, "pre-existing rollback_to=5 behavior must still fire")
        self.assertEqual(after_p6["design_approved"], design_before_p6_evidence,
                         "design_approved must survive an evidence-recording call even at phase 6+")


class TestArchitectRespectsSettledDesigns(HandsoffTestCase):
    """AR9: the Architect treats existing/shipped work as settled context
    to build around, proposing a change to it only on the human's
    explicit request (`design-approve --redesigns-settled-work`). The
    behavioral half of this guarantee (does the Architect actually fit
    vs. re-architect, and ask when ambiguous) is a prompt-governed LLM
    behavior, not deterministic code, and is verified separately by a
    recorded actor/judge agent scenario (manual evidence, see
    REQ-001/AR9-003/AR9-007 in the acceptance registry) -- not by a test
    in this class. This class covers the two MECHANICALLY testable
    guarantees: the new optional CLI flag, and that the AR1-3 core is
    unregressed."""

    def _author_real_criterion(self, requirement="A real, specific, observable outcome",
                               test="pytest tests/test_real.py -q"):
        r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_redesigns_settled_work_flag_optional_and_auditable(self):
        self.init()
        self._author_real_criterion()

        # Omitted (the default): no claim is made about touching settled work.
        r1 = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        status1 = self.read_status()
        self.assertIn("redesigns_settled_work", status1["design_approved"])
        self.assertIsNone(status1["design_approved"]["redesigns_settled_work"])

        # Explicitly passed but empty/whitespace-only: refused cleanly.
        r2 = run(["design-approve", "--by", "moncy", "--architect", "arch-2", "--summary", "s",
                 "--redesigns-settled-work", "   "], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn("--redesigns-settled-work", r2.stdout)
        self.assertNotIn("Traceback", r2.stdout + r2.stderr)

        # Explicit, non-empty value: recorded on status.design_approved
        # AND in the hash-chained event, auditable either way.
        note = "Changing the payment retry policy, per explicit human request"
        r3 = run(["design-approve", "--by", "moncy", "--architect", "arch-3", "--summary", "s2",
                 "--redesigns-settled-work", note], cwd=self.tmp)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        status3 = self.read_status()
        self.assertEqual(status3["design_approved"]["redesigns_settled_work"], note)
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn(note, events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)

    def test_ar1_ar3_core_unregressed(self):
        """Programmatically loads and RUNS the real, pre-existing
        TestArchitectDesignApprovalGate class (not a re-assertion of its
        behavior): a renamed or deleted class fails to resolve here, and
        a net shrinkage in its test methods fails the exact-count check,
        so this criterion is bound to the actual class, not a copy of
        what it once asserted."""
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestArchitectDesignApprovalGate)
        self.assertEqual(suite.countTestCases(), 10,
                        "TestArchitectDesignApprovalGate must still have exactly its known 10 test methods")
        runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(),
                       f"AR1-3 core regressed: {len(result.failures)} failures, {len(result.errors)} errors "
                       f"({[f[0].id() for f in result.failures] + [e[0].id() for e in result.errors]})")


class TestArchitectHandoffAndAuthorship(HandsoffTestCase):
    """AR5+AR6: the acceptance registry -- written exclusively via
    criterion-add/criterion-update, exactly as AR1-3 already required --
    hands off to the existing, unmodified Implementer/Reviewer/gate
    pipeline with no special-casing (AR5-002); design-approve's own
    --summary is retrievable straight from status.design_approved.summary,
    not only the event log (AR5-003); and every criterion present at a
    successful design-approve is stamped authored_by = the architect
    identity, an existing non-null stamp never reassigned by a later
    approval (AR6-004). authored_by is deliberately excluded from
    criterion_spec_hash (and therefore design_hash), the same way
    state/evidence already are: provenance about who proposed a criterion,
    not part of the claim being verified, so stamping it can never
    invalidate an already-recorded evidence binding or mismatch a freshly
    recomputed design_hash (AR6-005)."""

    def _enable_true_command(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))

    def _author_real_criterion(self, criterion_id="REQ-001",
                               requirement="A real, specific, observable outcome",
                               test="true", type_="primary_fix"):
        if criterion_id == "REQ-001":
            r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=self.tmp)
        else:
            r = run(["criterion-add", criterion_id, "--type", type_, "--requirement", requirement,
                    "--verification", "automated", "--test", test], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _write_acceptance(self, acceptance):
        """Write acceptance.json directly (simulating a hand-edited or
        legacy registry) and re-anchor the event log to it, the same way
        TestArchitectDesignApprovalGate._write_status re-anchors a
        hand-crafted status -- so the write reads as a deliberate test
        fixture, not as tamper the unrelated chain-freshness check would
        otherwise (correctly) flag."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.acceptance_path(self.tmp, cfg), acceptance)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted acceptance directly")

    def test_audit_trail_shows_four_distinct_identities(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion()

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-ar5",
                       "--summary", "Approach: X."], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-ar5"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-ar5"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-ar5"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-ar5"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-ar5"], cwd=self.tmp).returncode, 0)

        status = self.read_status()
        acceptance = self.read_acceptance()
        architect = status["design_approved"]["architect"]
        approver = status["design_approved"]["by"]
        implementer = status["implemented_by"]
        reviewer = status["reviewed_by"]
        identities = {architect, approver, implementer, reviewer}
        self.assertEqual(len(identities), 4, f"expected 4 distinct identities, got {identities}")
        self.assertEqual(architect, "arch-ar5")
        self.assertEqual(approver, "moncy")
        self.assertEqual(implementer, "impl-ar5")
        self.assertEqual(reviewer, "rev-ar5")

        # criterion.authored_by is retrievable straight from the acceptance
        # file too, not only from status.design_approved.architect.
        req001 = next(c for c in acceptance["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(req001["authored_by"], "arch-ar5")

        # The design-authoring event is in the same tamper-evident chain as
        # every other event, not a side channel.
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"design_approved"', events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)

    def test_registry_handoff_drives_existing_pipeline_unchanged(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="The primary observable outcome happens.")
        self._author_real_criterion(criterion_id="SUP-001", type_="supporting",
                                    requirement="A supporting observable outcome happens.")

        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-h",
                             "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)

        verify_r = run(["verify", "--criterion", "REQ-001", "--criterion", "SUP-001", "--by", "impl-h"],
                       cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-h"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("SHIP_FEATURE_VALID", validated.stdout)
        self.assertEqual(self.read_status()["status"], "complete")

        # Both registry-written criteria (one via criterion-update, one via
        # criterion-add) were carried through unmodified pipeline machinery,
        # and both got authored_by stamped at approval.
        acceptance = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in acceptance["criteria"] if x["id"] == cid)
            self.assertEqual(c["state"], "passing")
            self.assertEqual(c["authored_by"], "arch-h")

    def test_design_summary_stored_on_status_for_retrieval(self):
        self.init()
        self._author_real_criterion()
        summary = "Approach: clean registry handoff. Tradeoffs: none new. Decision: proceed."
        r = run(["design-approve", "--by", "moncy", "--architect", "arch-s",
                "--summary", summary], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        status = self.read_status()
        self.assertEqual(status["design_approved"]["summary"], summary,
                        "design summary must be retrievable directly from status.json, not only the event log")

        # Present in the hash-chained event too -- this adds a second,
        # directly-retrievable location, it does not replace the existing one.
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn(summary, events_text)

        # Survives a fresh reload from disk, not just held in memory.
        reread = json.loads((self.tmp / "handsoff-status.json").read_text())
        self.assertEqual(reread["design_approved"]["summary"], summary)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_authored_by_stamped_at_approval_and_never_reassigned(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="Primary observable outcome.")
        self._author_real_criterion(criterion_id="SUP-001", type_="supporting",
                                    requirement="Supporting observable outcome.")

        approve1 = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                       "--summary", "s1"], cwd=self.tmp)
        self.assertEqual(approve1.returncode, 0, approve1.stdout + approve1.stderr)

        acceptance = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in acceptance["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1")

        # Persists through a fresh reload from disk.
        reread = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in reread["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1")

        # Adding a criterion post-approval forces the pre-existing Phase-2
        # rollback (AR-003) and the new criterion starts unstamped.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        added = run(["criterion-add", "SUP-002", "--type", "supporting",
                    "--requirement", "A criterion added after approval.",
                    "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIsNone(self.read_status()["design_approved"])
        after_add = self.read_acceptance()
        sup002 = next(x for x in after_add["criteria"] if x["id"] == "SUP-002")
        self.assertNotIn("authored_by", sup002)
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in after_add["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1", "an earlier approval's authorship must never be reassigned")

        # A different architect approves the re-opened design: only the
        # newly-added, unstamped criterion picks up the new architect; the
        # two already-authored criteria keep their original authorship.
        approve2 = run(["design-approve", "--by", "moncy", "--architect", "arch-2",
                       "--summary", "s2"], cwd=self.tmp)
        self.assertEqual(approve2.returncode, 0, approve2.stdout + approve2.stderr)
        after_approve2 = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in after_approve2["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1", "re-approval must never reassign existing authorship")
        sup002_after = next(x for x in after_approve2["criteria"] if x["id"] == "SUP-002")
        self.assertEqual(sup002_after["authored_by"], "arch-2")

        # authored_by explicitly null is treated exactly like an absent key:
        # eligible for stamping.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        explicit_null = run(["criterion-add", "SUP-003", "--type", "supporting",
                            "--requirement", "A criterion with authored_by forced null.",
                            "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(explicit_null.returncode, 0, explicit_null.stdout + explicit_null.stderr)
        acc = self.read_acceptance()
        for c in acc["criteria"]:
            if c["id"] == "SUP-003":
                c["authored_by"] = None
        self._write_acceptance(acc)
        approve3 = run(["design-approve", "--by", "moncy", "--architect", "arch-3",
                       "--summary", "s3"], cwd=self.tmp)
        self.assertEqual(approve3.returncode, 0, approve3.stdout + approve3.stderr)
        final = self.read_acceptance()
        sup003 = next(x for x in final["criteria"] if x["id"] == "SUP-003")
        self.assertEqual(sup003["authored_by"], "arch-3")
        for cid in ("REQ-001", "SUP-001", "SUP-002"):
            c = next(x for x in final["criteria"] if x["id"] == cid)
            self.assertNotEqual(c["authored_by"], "arch-3", "re-approval must never reassign existing authorship")

        # An explicit empty string is NON-null (unlike an absent key or an
        # explicit null), so the stamping guard must not treat it as
        # eligible for stamping -- but "" is also schema-invalid, so the
        # only way it could ever reach the registry is a hand-edit (already
        # out of contract per AR5-002). design-approve's own post-stamp
        # schema check catches that and refuses the whole approval rather
        # than silently overwriting or silently accepting the invalid
        # value. Round-1 review finding: the stamping guard originally used
        # a falsy check (`not c.get("authored_by")`), which would have
        # silently overwritten "" with the new architect instead of
        # refusing -- this is the regression test for that.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        acc2 = self.read_acceptance()
        for c in acc2["criteria"]:
            if c["id"] == "SUP-003":
                c["authored_by"] = ""
        self._write_acceptance(acc2)
        approve4 = run(["design-approve", "--by", "moncy", "--architect", "arch-4",
                       "--summary", "s4"], cwd=self.tmp)
        self.assertEqual(approve4.returncode, 1, approve4.stdout + approve4.stderr)
        self.assertIn("authored_by", approve4.stdout)
        final2 = self.read_acceptance()
        sup003_after = next(x for x in final2["criteria"] if x["id"] == "SUP-003")
        self.assertEqual(sup003_after["authored_by"], "",
                        "a refused design-approve must not have touched the on-disk registry")

    def test_authored_by_schema_validated_and_stamping_is_hash_safe(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="Primary observable outcome.")

        # -- Schema: optional, nullable, non-empty string when present.
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["authored_by"] = ""
        self._write_acceptance(acceptance)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("authored_by", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

        acceptance2 = self.read_acceptance()
        acceptance2["criteria"][0]["authored_by"] = 42
        self._write_acceptance(acceptance2)
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn("authored_by", r2.stdout)

        acceptance3 = self.read_acceptance()
        acceptance3["criteria"][0]["authored_by"] = None
        self._write_acceptance(acceptance3)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        acceptance4 = self.read_acceptance()
        del acceptance4["criteria"][0]["authored_by"]
        self._write_acceptance(acceptance4)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        acceptance5 = self.read_acceptance()
        acceptance5["criteria"][0]["authored_by"] = "arch-real"
        self._write_acceptance(acceptance5)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        # -- Hash safety: real evidence recorded BEFORE authorship is
        # stamped must remain valid/passing AFTER stamping, and a fresh
        # Phase-3+ gate check right after approval must not see a stale
        # design-hash mismatch.
        acceptance6 = self.read_acceptance()
        del acceptance6["criteria"][0]["authored_by"]
        self._write_acceptance(acceptance6)

        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-hs"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-hs",
                       "--summary", "s"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        stamped = self.read_acceptance()
        self.assertEqual(stamped["criteria"][0]["authored_by"], "arch-hs")
        self.assertEqual(stamped["criteria"][0]["state"], "passing",
                        "stamping authored_by must not invalidate already-recorded evidence")

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0,
                        "stamping must not trip the evidence gate: " + validated.stdout + validated.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        gate = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertNotIn("design gate", gate.stdout)

    def test_pre_existing_architect_and_settled_design_features_unregressed(self):
        """AR9 guardrail: build on AR1-3's design-approve, don't
        re-architect it. Programmatically runs the real, pre-existing
        TestArchitectDesignApprovalGate (AR1-3) and
        TestArchitectRespectsSettledDesigns (AR9) classes, bound to their
        actual method counts so a renamed/deleted/shrunk class fails here."""
        loader = unittest.TestLoader()
        for cls, expected_count in ((TestArchitectDesignApprovalGate, 10),
                                    (TestArchitectRespectsSettledDesigns, 2)):
            suite = loader.loadTestsFromTestCase(cls)
            self.assertEqual(suite.countTestCases(), expected_count,
                            f"{cls.__name__} must still have exactly its known {expected_count} test methods")
            runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
            result = runner.run(suite)
            self.assertTrue(result.wasSuccessful(),
                           f"{cls.__name__} regressed: {len(result.failures)} failures, {len(result.errors)} errors "
                           f"({[f[0].id() for f in result.failures] + [e[0].id() for e in result.errors]})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
