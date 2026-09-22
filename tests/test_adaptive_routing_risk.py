"""REQ-003: closed adaptive-routing risk policy and routing gates."""
import sys
import tempfile
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

    def test_risk_floor_selects_and_records_native_phase_obligations(self):
        result = lib.route_adaptive_profile(risk_class="elevated", deterministic_checks_complete=True)
        self.assertEqual(result["tier"], "STANDARD")
        self.assertTrue(result["reviewer_required"])
        self.assertFalse(result["human_gate_required"])
        result = lib.route_adaptive_profile(risk_class="security_sensitive",
                                             available_tiers=["STANDARD", "PREMIUM"],
                                             deterministic_checks_complete=True)
        self.assertEqual(result["tier"], "PREMIUM")
        self.assertTrue(result["human_gate_required"])

    def test_classification_is_closed(self):
        for risk_class in lib.ADAPTIVE_RISK_CLASSES:
            self.assertEqual(lib.classify_adaptive_risk(risk_class), risk_class)
        with self.assertRaises(lib.HandsoffError):
            lib.classify_adaptive_risk("low")

    def test_project_risk_policy_is_loaded_and_validated(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-risk-policy-"))
        try:
            rows = []
            for name, policy in lib.ADAPTIVE_DEFAULT_RISK_POLICY.items():
                rows.extend([
                    f"[risk_policy.{name}]",
                    f'min_tier = "{policy["min_tier"]}"',
                    f'reviewer_required = {str(policy["reviewer_required"]).lower()}',
                    f'human_gate_required = {str(policy["human_gate_required"]).lower()}',
                    f'irreversible = {str(policy["irreversible"]).lower()}',
                ])
            (root / "handsoff.toml").write_text("\n".join(rows) + "\n")
            self.assertEqual(lib.load_config(root)["risk_policy"], lib.ADAPTIVE_DEFAULT_RISK_POLICY)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_human_gate_requires_deployment_approval_even_when_project_waives_it(self):
        cfg = {"deployment_requires_explicit_approval": False,
               "risk_policy": lib.ADAPTIVE_DEFAULT_RISK_POLICY}
        self.assertFalse(lib.adaptive_deployment_approval_required({"risk_class": "routine"}, cfg))
        self.assertTrue(lib.adaptive_deployment_approval_required({"risk_class": "irreversible"}, cfg))


if __name__ == "__main__":
    unittest.main()
