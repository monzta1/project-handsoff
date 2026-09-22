"""REQ-006: adaptive PREMIUM and escalation budgets pause safely."""
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class AdaptiveRoutingBudgetTests(unittest.TestCase):
    def cfg(self, **overrides):
        budgets = {"per_mission": {}, "fleet": {}}
        for scope, values in overrides.items():
            budgets[scope] = values
        return {"adaptive_routing_budgets": budgets}

    def test_per_mission_premium_budget_is_distinct(self):
        result = lib.evaluate_adaptive_budget(
            self.cfg(per_mission={"premium_calls": 2}),
            mission_usage={"premium_calls": 2}, deterministic_checks_complete=True)
        self.assertEqual(result["reason"], "per_mission_premium_calls_exhausted")
        self.assertEqual(result["scope"], "per_mission")

    def test_fleet_repair_total_and_concurrency_budgets_are_enforced(self):
        for field in ("repair_rounds", "total_calls", "concurrent_premium_agents"):
            result = lib.evaluate_adaptive_budget(
                self.cfg(fleet={field: 1}), fleet_usage={field: 1},
                deterministic_checks_complete=True)
            self.assertEqual(result["reason"], f"fleet_{field}_exhausted")

    def test_checks_in_flight_pause_before_reporting_budget_exhaustion(self):
        result = lib.evaluate_adaptive_budget(
            self.cfg(per_mission={"premium_calls": 0}),
            mission_usage={"premium_calls": 0}, deterministic_checks_complete=False)
        self.assertEqual(result["reason"], "deterministic_checks_in_flight")

    def test_route_returns_distinct_safe_pause(self):
        result = lib.route_adaptive_profile(
            self.cfg(per_mission={"total_calls": 1}),
            mission_usage={"total_calls": 1}, deterministic_checks_complete=True)
        self.assertEqual((result["state"], result["reason"]),
                         ("paused", "per_mission_total_calls_exhausted"))
        self.assertIsNone(result["tier"])

    def test_invalid_budget_is_refused(self):
        with self.assertRaises(lib.HandsoffError):
            lib.validate_adaptive_routing_budgets({"per_mission": {"premium_calls": -1}})

    def test_mission_and_fleet_counters_are_derived_from_committed_session_ledgers(self):
        profile = lib.route_adaptive_profile(risk_class="irreversible")["profile"]
        route = {"risk_class": "irreversible", "tier": "PREMIUM", "adapter": profile["adapter"],
                 "model": profile["model"], "profile": profile, "reason": "qualified_profile",
                 "reviewer_required": True, "human_gate_required": True}
        status = {"agent_sessions": {
            "hs-" + "1" * 32: {"state": "running", "adaptive_routing": route},
        }, "review_attempts": [{"disposition": "changes_requested"}]}
        self.assertEqual(lib.adaptive_usage(status), {
            "premium_calls": 1, "repair_rounds": 1, "total_calls": 1,
            "concurrent_premium_agents": 1,
        })
        base = Path(tempfile.mkdtemp(prefix="handsoff-fleet-budget-"))
        try:
            root = base / "run"
            root.mkdir()
            (root / "handsoff.toml").write_text("")
            (root / "handsoff-status.json").write_text(json.dumps(status))
            registry = base / "projects.json"
            registry.write_text(json.dumps({"schema": 1, "projects": [
                {"root": str(root), "registered_at": "2026-01-01T00:00:00+00:00"},
            ]}))
            self.assertEqual(lib.adaptive_fleet_usage(root, registry=registry)["premium_calls"], 1)
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
