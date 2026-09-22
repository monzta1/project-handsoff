"""REQ-001/REQ-004: canonical snapshot and per-agent assignment telemetry."""
import json
import sys
from pathlib import Path

from tests.test_adaptive_routing_launch import routing_record
from tests.test_handsoff_supervisor import ROOT, HandsoffTestCase, run
from tests.test_snapshot_contract import validate

sys.path.insert(0, str(ROOT / "bin"))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class AdaptiveRoutingSnapshotTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def test_legacy_snapshot_is_explicitly_not_used(self):
        self.init("Legacy snapshot")
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="configured-implementer", adapter="codex",
            requested_model="configured-model", resolution_source="configured",
        )
        snapshot = dashboard.build_snapshot(self.tmp)["adaptive_routing"]
        self.assertFalse(snapshot["used"])
        self.assertIsNone(snapshot["tier"])
        self.assertEqual(snapshot["calls_by_tier"], {"FAST": 0, "STANDARD": 0, "PREMIUM": 0})
        self.assertEqual(snapshot["selections"], [{
            "session_id": session["session_id"], "role": "implementer", "actor": "configured-implementer",
            "phase_number": 1, "adaptive": False, "tier": None, "adapter": "codex",
            "model": "configured-model", "reason": "configured", "state": "launching",
        }])

    def test_snapshot_names_which_agent_used_which_model_for_which_phase(self):
        result = run(["init", "Routed snapshot", "--risk-class", "routine"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = routing_record()
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer", adapter=route["adapter"],
            requested_model=route["model"], resolution_source="adaptive", adaptive_routing=route,
        )
        lib.transition_agent_session(self.tmp, session["session_id"], "running")
        lib.transition_agent_session(
            self.tmp, session["session_id"], "completed", exit_code=0,
            usage={"tokens_in": 1000, "tokens_out": 500, "tokens_total": 1500, "source": "adapter"},
        )
        snapshot = dashboard.build_snapshot(self.tmp)
        route_view = snapshot["adaptive_routing"]
        self.assertTrue(route_view["used"])
        self.assertEqual(route_view["estimated_cost"], 0.0035)
        self.assertEqual(route_view["token_usage"]["total"], 1500)
        self.assertEqual(route_view["selections"], [{
            "session_id": session["session_id"], "role": "implementer", "actor": "codex-implementer",
            "phase_number": 1, "adaptive": True, "tier": "FAST", "adapter": "claude",
            "model": "claude-haiku-4-5-20251001", "reason": "qualified_profile", "state": "completed",
        }])
        schema = json.loads((ROOT / "schemas" / "snapshot.schema.json").read_text())
        self.assertEqual(validate(snapshot, schema), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
