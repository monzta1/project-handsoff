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
            "purpose": "Build and verification", "phase_number": 1, "adaptive": False,
            "tier": None, "adapter": "codex", "model": "configured-model",
            "requested_model": "configured-model", "model_source": "exact_request",
            "model_consistency": "not_applicable",
            "reason": "configured", "state": "launching",
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
            "purpose": "Build and verification", "phase_number": 1, "adaptive": True,
            "tier": "FAST", "adapter": "claude", "model": "claude-haiku-4-5-20251001",
            "requested_model": "claude-haiku-4-5-20251001", "model_source": "adaptive_selection",
            "model_consistency": "pending_verification",
            "reason": "qualified_profile", "state": "completed",
        }])
        schema = json.loads((ROOT / "schemas" / "snapshot.schema.json").read_text())
        self.assertEqual(validate(snapshot, schema), [])

    def test_provider_default_is_not_presented_as_an_exact_model(self):
        self.init("Resolved model telemetry")
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="claude-implementer", adapter="claude",
            requested_model="default", resolution_source="configured",
        )
        unresolved = dashboard.build_snapshot(self.tmp)["adaptive_routing"]["selections"][0]
        self.assertIsNone(unresolved["model"])
        self.assertEqual(unresolved["model_source"], "not_reported")
        lib.transition_agent_session(self.tmp, session["session_id"], "running")
        lib.transition_agent_session(
            self.tmp, session["session_id"], "completed", exit_code=0,
            reported_model="claude-opus-5",
        )
        resolved = dashboard.build_snapshot(self.tmp)["adaptive_routing"]["selections"][0]
        self.assertEqual(resolved["model"], "claude-opus-5")
        self.assertEqual(resolved["model_source"], "adapter_reported")

    def test_provider_model_mismatch_is_visible_and_charged_conservatively(self):
        result = run(["init", "Mismatch", "--risk-class", "routine"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = routing_record()
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="claude-implementer", adapter=route["adapter"],
            requested_model=route["model"], resolution_source="adaptive", adaptive_routing=route,
        )
        lib.transition_agent_session(self.tmp, session["session_id"], "running")
        lib.transition_agent_session(
            self.tmp, session["session_id"], "completed", exit_code=0,
            reported_model="claude-opus-5[1m]",
            usage={"tokens_in": 1000, "tokens_out": 500, "tokens_total": 1500, "source": "adapter"},
        )
        status = self.read_status()
        selection = dashboard.build_snapshot(self.tmp)["adaptive_routing"]["selections"][0]
        self.assertEqual(selection["model_consistency"], "mismatch")
        self.assertEqual(selection["model"], "claude-opus-5[1m]")
        self.assertEqual(lib.adaptive_usage(status)["premium_calls"], 1)

    def test_historical_session_without_phase_remains_schema_valid(self):
        self.init("Historical")
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="old-implementer", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        status = self.read_status()
        status["agent_sessions"][session["session_id"]]["phase_number"] = None
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="historical null phase")
        snapshot = dashboard.build_snapshot(self.tmp)
        schema = json.loads((ROOT / "schemas" / "snapshot.schema.json").read_text())
        self.assertEqual(validate(snapshot, schema), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
