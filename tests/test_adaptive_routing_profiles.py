"""Focused coverage for REQ-001 and REQ-002 adaptive routing profiles."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class AdaptiveRoutingProfileTests(unittest.TestCase):
    def test_profiles_are_distinct_source_cited_real_models(self):
        profiles = lib.adaptive_routing_profiles()
        self.assertEqual(set(profiles), {"FAST", "STANDARD", "PREMIUM"})
        self.assertEqual(len({profile["model"] for profile in profiles.values()}), 3)
        contexts = []
        outputs = []
        for profile in profiles.values():
            self.assertTrue(profile["capabilities"])
            self.assertTrue(profile["limits"])
            self.assertEqual(profile["source"], lib.ADAPTIVE_MODEL_CATALOG_SOURCE)
            self.assertEqual(set(profile["pricing"]), {"input_per_mtok", "output_per_mtok"})
            contexts.append(profile["limits"]["context_tokens"])
            outputs.append(profile["limits"]["output_tokens"])
        self.assertEqual(contexts, sorted(contexts))
        self.assertEqual(outputs, sorted(outputs))
        result = lib.route_adaptive_profile(required_capabilities=["tool_use"])
        self.assertEqual(result["state"], "selected")
        self.assertEqual(result["tier"], "FAST")

    def test_fast_and_premium_route_to_different_model_ids(self):
        fast = lib.route_adaptive_profile(risk_class="routine")
        premium = lib.route_adaptive_profile(risk_class="irreversible")
        self.assertEqual((fast["tier"], premium["tier"]), ("FAST", "PREMIUM"))
        self.assertNotEqual(fast["profile"]["model"], premium["profile"]["model"])

    def test_default_deferral_cannot_claim_invented_metadata(self):
        with self.assertRaisesRegex(lib.HandsoffError, "default deferral"):
            lib.validate_adaptive_routing_profiles({
                "FAST": {"adapter": "claude", "model": "default", "capabilities": ["tool_use"]},
            })

    def test_unavailable_requirement_pauses_with_auditable_reason(self):
        result = lib.route_adaptive_profile(required_capabilities=["extended_thinking"], available_tiers=["FAST"])
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["reason"], "required_capability_unavailable")
        self.assertIsNone(result["profile"])

    def test_explicitly_empty_available_tiers_pauses_without_selecting(self):
        result = lib.route_adaptive_profile(available_tiers=[])
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["reason"], "required_capability_unavailable")
        self.assertIsNone(result["tier"])
        self.assertIsNone(result["profile"])

    def test_unavailable_required_tier_never_uses_lower_unqualified_profile(self):
        result = lib.route_adaptive_profile(required_capabilities=["text"], minimum_tier="PREMIUM",
                                            available_tiers=["FAST", "STANDARD"])
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["reason"], "required_tier_unavailable")
        self.assertIsNone(result["tier"])

    def test_defaults_are_deep_copied_for_every_caller(self):
        first = lib.adaptive_routing_profiles()
        first["FAST"]["capabilities"].append("invented")
        first["FAST"]["limits"]["context_tokens"] = 1
        second = lib.adaptive_routing_profiles()
        self.assertNotIn("invented", second["FAST"]["capabilities"])
        self.assertEqual(second["FAST"]["limits"]["context_tokens"], 200000)
        budgets = lib.adaptive_routing_budgets()
        budgets["per_mission"]["premium_calls"] = 0
        self.assertIsNone(lib.adaptive_routing_budgets()["per_mission"]["premium_calls"])
        source = (Path(__file__).resolve().parents[1] / "bin" / "handsoff_lib.py").read_text()
        self.assertNotIn('if "deepcopy" in globals()', source)


if __name__ == "__main__":
    unittest.main()
