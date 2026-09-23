import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class ModelConstraintTests(unittest.TestCase):
    def test_codex_only_policy_routes_shared_infrastructure_away_from_opus(self):
        cfg = {"model_policy": {"allowed_adapters": ["codex"],
                                "denied_models": ["claude-opus-5"],
                                "quota_substitution": True}}
        route = lib.route_adaptive_profile(
            cfg, risk_class="shared_infrastructure",
            required_capabilities=("text", "tool_use"),
            deterministic_checks_complete=True,
        )
        self.assertEqual(route["state"], "selected")
        self.assertEqual((route["profile"]["adapter"], route["profile"]["model"]),
                         ("codex", "gpt-6-astra"))

    def test_denied_model_never_survives_fallback(self):
        decision = lib.plan_agent_fallback(
            "implementer", "rate_limit",
            [{"adapter": "claude", "model": "claude-opus-5"}],
            {"codex": True, "claude": True}, [("codex", "gpt-6-astra")], 0, 2,
            model_policy={"allowed_adapters": ["codex", "claude"],
                          "denied_models": ["claude-opus-5"], "quota_substitution": True},
            required_tier="PREMIUM",
        )
        self.assertEqual(decision["action"], "pilot_pause")
        self.assertEqual(decision["skipped"][0]["reason"], "model_policy_denied")

    def test_only_quota_can_cross_vendor_at_equivalent_tier(self):
        entries = [{"adapter": "codex", "model": "gpt-6-astra"}]
        availability = {"codex": True, "claude": True}
        policy = {"allowed_adapters": ["codex", "claude"], "denied_models": [],
                  "quota_substitution": True}
        quota = lib.plan_agent_fallback(
            "implementer", "rate_limit", entries, availability,
            [("claude", "claude-opus-5")], 0, 2,
            model_policy=policy, required_tier="PREMIUM",
        )
        self.assertEqual((quota["action"], quota["reason"]), ("select", "quota_substitution"))
        crash = lib.plan_agent_fallback(
            "implementer", "process_crash", entries, availability,
            [("claude", "claude-opus-5")], 0, 2,
            model_policy=policy, required_tier="PREMIUM",
        )
        self.assertEqual(crash["action"], "pilot_pause")
        self.assertEqual(crash["skipped"][0]["reason"], "cross_vendor_not_allowed")

    def test_auth_and_runtime_failures_pause_without_selection(self):
        for category in ("auth_failure", "runtime_environment"):
            decision = lib.plan_agent_fallback(
                "implementer", category,
                [{"adapter": "codex", "model": "gpt-6-astra"}],
                {"codex": True, "claude": True}, [("claude", "claude-opus-5")], 0, 2,
            )
            self.assertEqual((decision["action"], decision["reason"]),
                             ("pilot_pause", "environment_failure"))

    def test_host_implementation_leg_survives_the_phase_handoff(self):
        status = {"phase_number": 5, "status": "in_progress",
                  "implemented_by": "codex-host", "agent_sessions": {}}
        events = [
            {"kind": "phase_advanced", "phase_number": 4, "at": "2026-01-01T00:00:00+00:00"},
            {"kind": "phase_advanced", "phase_number": 5, "at": "2026-01-01T01:00:00+00:00"},
        ]
        view = lib.adaptive_routing_snapshot(
            status, host={"family": "codex", "model_class": "GPT-5.6 Sol"}, events=events,
        )
        leg = view["selections"][0]
        self.assertEqual((leg["role"], leg["model"], leg["state"]),
                         ("implementer", "GPT-5.6 Sol", "completed"))


if __name__ == "__main__":
    unittest.main()
