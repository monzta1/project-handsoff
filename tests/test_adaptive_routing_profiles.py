"""Focused coverage for REQ-001 and REQ-002 adaptive routing profiles."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class AdaptiveRoutingProfileTests(unittest.TestCase):
    def test_profiles_have_explicit_metadata_and_route_by_capability(self):
        profiles = lib.adaptive_routing_profiles()
        self.assertEqual(set(profiles), {"FAST", "STANDARD", "PREMIUM"})
        for profile in profiles.values():
            self.assertTrue(profile["capabilities"])
            self.assertTrue(profile["limits"])
            self.assertIn("latency_ms", profile)
            self.assertIn("estimated_cost", profile)
        result = lib.route_adaptive_profile(required_capabilities=["tool_use"])
        self.assertEqual(result["state"], "selected")
        self.assertEqual(result["tier"], "STANDARD")

    def test_custom_profiles_are_configurable_without_provider_branching(self):
        cfg = {"adaptive_routing_profiles": {
            "FAST": {"model": "small", "capabilities": ["text"],
                     "limits": {"context_tokens": 100}, "latency_ms": 1, "estimated_cost": 0},
            "STANDARD": {"model": "medium", "capabilities": ["text", "code"],
                         "limits": {"context_tokens": 200}, "latency_ms": 2, "estimated_cost": 1},
            "PREMIUM": {"model": "large", "capabilities": ["text", "code", "reasoning"],
                        "limits": {"context_tokens": 300}, "latency_ms": 3, "estimated_cost": 2},
        }}
        result = lib.route_adaptive_profile(cfg, required_capabilities=["reasoning"])
        self.assertEqual(result["tier"], "PREMIUM")
        self.assertEqual(result["profile"]["model"], "large")

    def test_unavailable_requirement_pauses_with_auditable_reason(self):
        result = lib.route_adaptive_profile(required_capabilities=["tool_use"], available_tiers=["FAST"])
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


if __name__ == "__main__":
    unittest.main()
