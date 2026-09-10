#!/usr/bin/env python3
"""Regression tests for Project Handsoff itself.

Every test here maps to either a bug found by hand (run the tool the way
the README says to, on a fresh copy) or a guarantee this rewrite adds.
Stdlib unittest only, no pytest dependency, so these run anywhere Python 3
is available, matching the framework's own "copy a few files into a
project" ethos.

Run: python3 tests/test_handsoff_supervisor.py -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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
            changed = run(["criterion-update", "REQ-001", "--state", state], cwd=self.tmp)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)

    def advance_to(self, phase, **extra):
        """Step one phase at a time up to `phase`, as the one-step rule requires."""
        current = self.read_status()["phase_number"]
        last = None
        reviewed_by = extra.pop("reviewed_by", None)
        for n in range(current + 1, phase + 1):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
