"""REQ-003: closed adaptive-routing risk policy and routing gates."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class AdaptiveRoutingRiskTests(unittest.TestCase):
    def test_policy_contains_the_six_exact_mappings(self):
        self.assertEqual(lib.adaptive_risk_policy(), {
            "routine": {"min_tier": "FAST", "reviewer_required": False, "human_gate_required": False, "irreversible": False},
            "elevated": {"min_tier": "STANDARD", "reviewer_required": True, "human_gate_required": False, "irreversible": False},
            "security_sensitive": {"min_tier": "PREMIUM", "reviewer_required": True, "human_gate_required": True, "irreversible": False},
            "persistence_migration": {"min_tier": "PREMIUM", "reviewer_required": True, "human_gate_required": True, "irreversible": False},
            "shared_infrastructure": {"min_tier": "PREMIUM", "reviewer_required": True, "human_gate_required": True, "irreversible": False},
            "irreversible": {"min_tier": "PREMIUM", "reviewer_required": True, "human_gate_required": True, "irreversible": True},
        })

    def test_risk_floor_and_gates_are_enforced(self):
        result = lib.route_adaptive_profile(risk_class="elevated")
        self.assertEqual(result["reason"], "reviewer_required")
        result = lib.route_adaptive_profile(risk_class="elevated", reviewer_approved=True)
        self.assertEqual(result["tier"], "STANDARD")
        result = lib.route_adaptive_profile(risk_class="security_sensitive", reviewer_approved=True)
        self.assertEqual(result["reason"], "human_gate_required")
        result = lib.route_adaptive_profile(risk_class="security_sensitive", reviewer_approved=True,
                                             human_gate_approved=True, available_tiers=["STANDARD", "PREMIUM"])
        self.assertEqual(result["tier"], "PREMIUM")

    def test_classification_is_closed(self):
        for risk_class in lib.ADAPTIVE_RISK_CLASSES:
            self.assertEqual(lib.classify_adaptive_risk(risk_class), risk_class)
        with self.assertRaises(lib.HandsoffError):
            lib.classify_adaptive_risk("low")


if __name__ == "__main__":
    unittest.main()
