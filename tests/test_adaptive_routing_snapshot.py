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
        status = self.read_status()
        status.pop("risk_class")
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="historical status without risk class")
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
            "started_at": session["started_at"], "ended_at": None,
            "tier": None, "adapter": "codex", "model": "configured-model",
            "requested_model": "configured-model", "model_source": "exact_request",
            "model_consistency": "not_applicable",
            "reason": "configured", "state": "launching",
        }])

    def test_new_default_run_is_routing_ready_before_its_first_call(self):
        """Ready, and honest about having chosen nothing yet.

        This test used to assert the header carried FAST / claude /
        claude-haiku-4-5-20251001 on a run with no sessions at all, which is
        #333: a model computed at read time presented as the model that ran. A
        Pilot reading it had no way to know routing had not run. The header is
        now empty until something is recorded or reported, and the computed
        choice keeps its own name.
        """
        self.init("Default routing snapshot")
        snapshot = dashboard.build_snapshot(self.tmp)["adaptive_routing"]
        self.assertTrue(snapshot["used"], "a risk_class means the run is governed by routing")
        self.assertEqual(snapshot["risk_class"], "routine")
        self.assertEqual((snapshot["tier"], snapshot["adapter"], snapshot["model"]),
                         (None, None, None),
                         "nothing ran, so the header names nothing")
        self.assertEqual(snapshot["header_source"], "projection")
        self.assertEqual(snapshot["would_route_to"], {
            "tier": "FAST", "adapter": "claude", "model": "claude-haiku-4-5-20251001",
        }, "what routing would choose is still shown, under its own name")
        self.assertEqual(snapshot["calls_by_tier"], {"FAST": 0, "STANDARD": 0, "PREMIUM": 0})
        self.assertEqual(snapshot["selections"], [])

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
        completed = self.read_status()["agent_sessions"][session["session_id"]]
        self.assertTrue(route_view["used"])
        self.assertEqual(route_view["estimated_cost"], 0.0035)
        self.assertEqual(route_view["token_usage"]["total"], 1500)
        self.assertEqual(route_view["selections"], [{
            "session_id": session["session_id"], "role": "implementer", "actor": "codex-implementer",
            "purpose": "Build and verification", "phase_number": 1, "adaptive": True,
            "started_at": completed["started_at"], "ended_at": completed["ended_at"],
            "tier": "FAST", "adapter": "claude", "model": "claude-haiku-4-5-20251001",
            "requested_model": "claude-haiku-4-5-20251001", "model_source": "adaptive_selection",
            "model_consistency": "pending_verification",
            "reason": "qualified_profile", "state": "completed",
            "usage": {"tokens_in": 1000, "tokens_out": 500,
                      "tokens_total": 1500, "source": "adapter"},
        }])
        schema = json.loads((ROOT / "schemas" / "snapshot.schema.json").read_text())
        self.assertEqual(validate(snapshot, schema), [])

    def test_provider_default_is_not_presented_as_an_exact_model(self):
        self.init("Resolved model telemetry")
        status = self.read_status()
        status.pop("risk_class")
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="historical configured session")
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
        status = self.read_status()
        status.pop("risk_class")
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="historical status")
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
