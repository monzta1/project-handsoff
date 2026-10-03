"""#361: a managed Architect whose one design proposal is refused by
validation ends failed as protocol_refused, with the refused field, its
index and the limit in the recorded reason and on the launcher's stderr.
An Architect that produces nothing names the Architect; a Supervisor that
produces nothing keeps its own sentence."""
from __future__ import annotations

import io
import json
import shutil
import sys
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase
from tests.fixture_state import write_version_pin
from tests.test_session_result_autoadopt import _FakeProcess

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

SUPERVISOR_NOOP = "Supervisor exited without a broker request or Pilot question"
ARCHITECT_NOOP = "Architect exited without a structured design proposal, decline or Pilot question"


def _proposal(**overrides):
    proposal = {
        "summary": "Refusal fixture.",
        "approach": ["one", "two", "x" * 513, "Data shape: none."],
        "tradeoffs": [], "decisions": ["d"], "constraints": [], "verification": ["v"],
    }
    proposal.update(overrides)
    return "HANDSOFF_DESIGN_PROPOSAL: " + json.dumps(proposal) + "\n"


class ArchitectRefusalReasonTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Architect refusal fixture")

    def _spec(self, role):
        return runtime.LaunchSpec(
            role, "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded prompt", token_budget=40_000,
            project_root=str(self.tmp.resolve()),
        )

    def _launch(self, role, stdout, returncode=0):
        factory = mock.Mock(side_effect=lambda *a, **k: _FakeProcess(stdout=stdout, returncode=returncode))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with self.assertRaises(runtime.AgentLaunchError) as raised:
                runtime.execute_launch(self._spec(role), popen_factory=factory, beacon_interval=0.01)
        status = self.read_status()
        sid = status["current_agent_sessions"][role]
        return status["agent_sessions"][sid], status["agent_failures"][sid], str(raised.exception), err.getvalue()

    # REQ-004
    def test_the_validator_names_the_item_index_and_the_limit(self):
        with self.assertRaisesRegex(lib.HandsoffError,
                                    r"approach item length 513 at approach\[2\]; each item must be 1 to 512"):
            lib.validate_design_proposal(json.loads(_proposal()[len("HANDSOFF_DESIGN_PROPOSAL: "):]))

    def test_a_refused_proposal_fails_as_protocol_refused_naming_the_field(self):
        session, failure, message, stderr = self._launch("architect", _proposal())
        self.assertEqual(session["state"], "failed")
        self.assertEqual(failure["category"], "protocol_refused")
        self.assertNotEqual(failure["category"], "orchestration_noop")
        for text in (failure["reason"], message, stderr):
            self.assertIn("approach[2]", text)
            self.assertIn("512", text)
        self.assertIn("HANDSOFF_AGENT_REFUSED:", stderr)
        self.assertLessEqual(len(failure["reason"]), 200)
        # deterministic: recovery must not spend a fallback on the same refusal
        self.assertNotIn("protocol_refused", lib.RECOVERABLE_FAILURE_CATEGORIES)

    def test_a_refused_proposal_then_a_non_zero_exit_is_still_named(self):
        # implementation review attempt 1: the non-zero-exit branch ran first
        # and recorded non_zero_exit with a generic reason
        session, failure, message, stderr = self._launch("architect", _proposal(), returncode=1)
        self.assertEqual(failure["category"], "protocol_refused")
        for text in (failure["reason"], stderr):
            self.assertIn("approach[2]", text)
            self.assertIn("512", text)

    def test_protocol_refused_carries_a_bounded_free_reason(self):
        digest = "0" * 64
        record = lib._validate_failure_classification(
            {"category": "protocol_refused", "reason": "approach[2] over 512", "tail_sha256": digest})
        self.assertEqual(record["reason"], "approach[2] over 512")
        for reason in ("", " ", "x" * 201):
            with self.assertRaises(lib.HandsoffError):
                lib._validate_failure_classification(
                    {"category": "protocol_refused", "reason": reason, "tail_sha256": digest})

    # REQ-005
    def test_a_silent_architect_names_the_architect(self):
        session, failure, _, _ = self._launch("architect", "")
        self.assertEqual(failure["category"], "orchestration_noop")
        self.assertEqual(failure["reason"], ARCHITECT_NOOP)
        self.assertNotIn("Supervisor", failure["reason"])

    def test_a_silent_supervisor_keeps_its_sentence(self):
        session, failure, _, _ = self._launch("supervisor", "")
        self.assertEqual(failure["category"], "orchestration_noop")
        self.assertEqual(failure["reason"], SUPERVISOR_NOOP)

    def test_classification_and_validation_bind_the_noop_reason_to_the_role(self):
        self.assertEqual(lib.classify_runtime_failure(orchestration_noop=True, role="architect")["reason"],
                         ARCHITECT_NOOP)
        self.assertEqual(lib.classify_runtime_failure(orchestration_noop=True, role="supervisor")["reason"],
                         SUPERVISOR_NOOP)
        self.assertEqual(lib.classify_runtime_failure(orchestration_noop=True)["reason"], SUPERVISOR_NOOP)
        digest = "0" * 64
        for reason in (SUPERVISOR_NOOP, ARCHITECT_NOOP):
            lib._validate_failure_classification(
                {"category": "orchestration_noop", "reason": reason, "tail_sha256": digest})
        with self.assertRaises(lib.HandsoffError):
            lib._validate_failure_classification(
                {"category": "orchestration_noop", "reason": "anything else", "tail_sha256": digest})


if __name__ == "__main__":
    import unittest
    unittest.main()
