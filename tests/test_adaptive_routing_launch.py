"""REQ-001/REQ-004: default adaptive routing reaches real launch/session paths."""
import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import ROOT, HandsoffTestCase, run

sys.path.insert(0, str(ROOT / "bin"))
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def routing_record(risk_class="routine"):
    selected = lib.route_adaptive_profile(risk_class=risk_class, deterministic_checks_complete=True)
    profile = selected["profile"]
    return {
        "risk_class": risk_class, "tier": selected["tier"],
        "adapter": profile["adapter"], "model": profile["model"],
        "profile": profile, "reason": selected["reason"],
        "reviewer_required": selected["reviewer_required"],
        "human_gate_required": selected["human_gate_required"],
    }


class AdaptiveRoutingLaunchTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _phase_four(self):
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="phase four")

    def _spec(self):
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="task"), \
                mock.patch.object(agent, "applicable_design_review_packet", return_value=None):
            return agent.build_launch_spec(self.tmp, "implementer", "task",
                                           which=lambda name: f"/opt/test/{name}", skip_preflight=True)

    def test_init_records_explicit_risk_and_omission_defaults_to_routine(self):
        result = run(["init", "Classified", "--risk-class", "elevated"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read_status()["risk_class"], "elevated")

        other = self.tmp.parent / (self.tmp.name + "-legacy")
        shutil.copytree(self.tmp, other)
        try:
            for name in ("handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl",
                         ".handsoff-event-head.json"):
                (other / name).unlink(missing_ok=True)
            result = run(["init", "Defaulted"], cwd=other)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((other / "handsoff-status.json").read_text())
            self.assertEqual(status["risk_class"], "routine")
            self.assertFalse(any("adaptive_routing" in session for session in (status.get("agent_sessions") or {}).values()))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_legacy_normal_launch_routes_and_atomically_persists_routine(self):
        self.init("Legacy")
        self._phase_four()
        status = self.read_status()
        status.pop("risk_class")
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="legacy status without risk class")
        self.assertNotIn("risk_class", self.read_status())

        routed = self._spec()
        self.assertEqual((routed.adapter, routed.model, routed.resolution_source),
                         ("claude", "claude-haiku-4-5-20251001", "adaptive"))
        self.assertEqual(routed.adaptive_routing["tier"], "FAST")
        self.assertNotIn("risk_class", self.read_status(), "building a spec is read-only")

        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="test-implementer",
            adapter=routed.adapter, requested_model=routed.model,
            resolution_source=routed.resolution_source,
            adaptive_routing=routed.adaptive_routing,
        )
        self.assertEqual(self.read_status()["risk_class"], "routine")
        self.assertEqual(session["adaptive_routing"]["tier"], "FAST")
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["kind"] == "adaptive_risk_defaulted" for event in events))

    def test_operator_surfaces_document_routine_as_the_default(self):
        result = run(["init", "--help"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("set adaptive routing risk (default: routine)", result.stdout)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        self.assertIn("Every new run uses adaptive model routing", readme)
        self.assertIn("Adaptive routing is the default for every new run", reference)
        self.assertNotIn("Adaptive routing is opt-in per run", reference)

    def test_session_commit_rechecks_budget_and_refusal_mutates_no_ledger(self):
        result = run(["init", "Budget", "--risk-class", "irreversible"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with (self.tmp / "handsoff.toml").open("a", encoding="utf-8") as handle:
            handle.write("\n[routing_budgets.per_mission]\npremium_calls = 0\n")
        paths = [self.tmp / name for name in ("handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl")]
        before = {path.name: path.read_bytes() for path in paths}
        with self.assertRaisesRegex(lib.HandsoffError, "premium_calls_exhausted"):
            lib.create_agent_session(
                self.tmp, role="implementer", actor="test-implementer", adapter="claude",
                requested_model="claude-opus-5", resolution_source="adaptive",
                adaptive_routing=routing_record("irreversible"),
            )
        self.assertEqual(before, {path.name: path.read_bytes() for path in paths})

    def test_classified_session_persists_the_exact_agent_model_assignment(self):
        result = run(["init", "Recorded", "--risk-class", "routine"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = routing_record()
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="test-implementer", adapter=route["adapter"],
            requested_model=route["model"], resolution_source="adaptive", adaptive_routing=route,
        )
        self.assertEqual(session["adaptive_routing"]["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(lib.adaptive_usage(self.read_status())["total_calls"], 1)

    def test_recovery_profile_is_exact_and_never_adaptively_replaced(self):
        result = run(["init", "Recovery", "--risk-class", "irreversible"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="task"), \
                mock.patch.object(agent.lib, "launch_preflight", return_value={"state": "ready"}) as preflight, \
                mock.patch.dict(os.environ, {"HANDSOFF_SKIP_PREFLIGHT": ""}):
            spec = agent.build_profile_launch_spec(
                self.tmp, "implementer", "task", {"adapter": "codex", "model": "reserved-model"},
                which=lambda name: f"/opt/test/{name}",
            )
        preflight.assert_called_once()
        self.assertEqual((spec.adapter, spec.model, spec.resolution_source),
                         ("codex", "reserved-model", "fallback"))
        self.assertIsNone(spec.adaptive_routing)

    def test_undersized_fallback_packet_refuses_before_preflight(self):
        self.init("Fallback packet")
        config_path = self.tmp / "handsoff.toml"
        config_path.write_text(
            config_path.read_text().replace("reviewer = 80000", "reviewer = 20000"),
            encoding="utf-8",
        )
        preflight = mock.Mock(return_value={"state": "ready"})
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent.lib, "launch_preflight", preflight):
            with self.assertRaisesRegex(lib.HandsoffError, "fallback launch refused before reservation"):
                agent.build_profile_launch_spec(
                    self.tmp, "reviewer", "x" * 60_000,
                    {"adapter": "codex", "model": "reserved-model"},
                    which=lambda name: f"/opt/test/{name}",
                )
        preflight.assert_not_called()

    def test_explicit_role_profile_is_not_replaced_by_adaptive_routing(self):
        result = run(["init", "Explicit", "--risk-class", "shared_infrastructure"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._phase_four()
        config = (self.tmp / "handsoff.toml").read_text(encoding="utf-8")
        config = config.replace('implementer = "auto"', 'implementer = "codex"', 1)
        config = config.replace('implementer = "default"', 'implementer = "gpt-6-astra"', 1)
        (self.tmp / "handsoff.toml").write_text(config, encoding="utf-8")
        spec = self._spec()
        self.assertEqual((spec.adapter, spec.model, spec.resolution_source),
                         ("codex", "gpt-6-astra", "configured"))
        self.assertIsNone(spec.adaptive_routing)

    def test_undersized_rendered_packet_refuses_without_creating_a_session(self):
        result = run(["init", "Packet preflight", "--risk-class", "routine"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._phase_four()
        before = self.read_status()
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="x" * 300_000), \
                mock.patch.object(agent, "applicable_design_review_packet", return_value=None):
            with self.assertRaisesRegex(lib.HandsoffError, "refused before session creation"):
                agent.build_launch_spec(self.tmp, "implementer", "task",
                                        which=lambda name: f"/opt/test/{name}", skip_preflight=True)
        self.assertEqual(self.read_status(), before)

    def test_phase_five_reviewer_can_launch_when_routing_selects_the_same_profile(self):
        result = run(["init", "Review", "--risk-class", "routine"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._phase_four()
        route = routing_record()
        implementation = lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer", adapter=route["adapter"],
            requested_model=route["model"], resolution_source="adaptive", adaptive_routing=route,
        )
        lib.transition_agent_session(self.tmp, implementation["session_id"], "running")
        lib.transition_agent_session(self.tmp, implementation["session_id"], "completed", exit_code=0)
        status = self.read_status()
        status.update(phase_number=5, phase=lib.PHASES[5], progress=60,
                      original_symptom_evidence_id="vr-" + "a" * 32)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="phase five")
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="task"), \
                mock.patch.object(agent, "applicable_design_review_packet", return_value=None), \
                mock.patch.object(agent.lib, "reviewer_launch_evidence_gaps", return_value=[]):
            review = agent.build_launch_spec(
                self.tmp, "reviewer", "task", which=lambda name: f"/opt/test/{name}",
                skip_preflight=True,
            )
        self.assertEqual((review.adapter, review.model), (route["adapter"], route["model"]))
        self.assertEqual(review.resolution_source, "adaptive")


if __name__ == "__main__":
    import unittest
    unittest.main()
