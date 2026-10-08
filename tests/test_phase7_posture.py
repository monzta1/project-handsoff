#!/usr/bin/env python3
"""#434: a Phase 7 run whose deployment approval is not required says so.

With adaptive_deployment_approval_required false, the Phase 7 next_action
says approval is not required for this project and names the next step
(land, then verify-live). A run that requires approval keeps the approval
text. The decision is the one adaptive_deployment_approval_required makes,
so a risk class whose policy needs a human gate still asks for approval.
"""
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class TestPhase7NextAction(unittest.TestCase):
    def test_not_required_names_the_next_step(self):
        text = lib.phase_next_action(7, {}, {"deployment_requires_explicit_approval": False})
        self.assertEqual(text, lib.PHASE7_NO_APPROVAL_NEXT_ACTION)
        self.assertIn("not required for this project", text)
        self.assertIn("land", text)
        self.assertIn("verify-live", text)

    def test_required_keeps_todays_text(self):
        self.assertEqual(lib.phase_next_action(7, {}, {"deployment_requires_explicit_approval": True}),
                         lib.NEXT_ACTION_DEFAULTS[7])
        self.assertEqual(lib.phase_next_action(7, {}, {}), lib.NEXT_ACTION_DEFAULTS[7])

    def test_follows_the_adaptive_decision(self):
        cfg = {"deployment_requires_explicit_approval": False}
        seen = set()
        for risk_class in (*lib.ADAPTIVE_RISK_CLASSES, None):
            status = {"risk_class": risk_class} if risk_class else {}
            required = lib.adaptive_deployment_approval_required(status, cfg)
            seen.add(required)
            expected = lib.NEXT_ACTION_DEFAULTS[7] if required else lib.PHASE7_NO_APPROVAL_NEXT_ACTION
            self.assertEqual(lib.phase_next_action(7, status, cfg), expected, risk_class)
        self.assertEqual(seen, {True, False}, "the default risk policy gates some classes and not others")

    def test_other_phases_are_unchanged(self):
        cfg = {"deployment_requires_explicit_approval": False}
        for phase, text in lib.NEXT_ACTION_DEFAULTS.items():
            if phase != 7:
                self.assertEqual(lib.phase_next_action(phase, {}, cfg), text)
        self.assertEqual(lib.phase_next_action(99, {}, cfg, "fallback"), "fallback")


class TestPhase7AdvanceWritesThePosture(HandsoffTestCase):
    def _reach_phase_7(self, approval_required):
        self.init("#434 Phase 7 posture")
        if not approval_required:
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace("deployment_requires_explicit_approval = true",
                                                     "deployment_requires_explicit_approval = false"))
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        self.assertEqual(self.read_status()["phase_number"], 7)

    def test_advance_to_phase_7_without_required_approval_states_the_plain_step(self):
        self._reach_phase_7(approval_required=False)
        self.assertEqual(self.read_status()["next_action"], lib.PHASE7_NO_APPROVAL_NEXT_ACTION)

    def test_advance_to_phase_7_with_required_approval_keeps_the_approval_step(self):
        self._reach_phase_7(approval_required=True)
        self.assertEqual(self.read_status()["next_action"], lib.NEXT_ACTION_DEFAULTS[7])
        self.assertIn("deployment approval", self.read_status()["next_action"])


if __name__ == "__main__":
    unittest.main()
