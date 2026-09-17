import json
import hashlib
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT / "tests"))
import handsoff_agent
import handsoff_lib as lib
import handsoff_dashboard
from test_handsoff_supervisor import HandsoffTestCase, run


class HostRoleTests(HandsoffTestCase):
    def set_host(self, role):
        path = self.tmp / "handsoff.toml"
        text = path.read_text().replace(f'{role} = "auto"', f'{role} = "host"')
        path.write_text(text)

    def test_config_profiles_and_launch_refusal(self):
        self.set_host("supervisor")
        cfg = lib.load_config(self.tmp)
        self.assertEqual(lib.agent_profiles(cfg)["supervisor"], {"adapter": "host", "model": None})
        with mock.patch.object(lib, "validate_runtime_integrity"):
            with self.assertRaisesRegex(lib.HandsoffError, "supervisor is host-driven"):
                handsoff_agent.build_launch_spec(self.tmp, "supervisor", "task", which=lambda _: None)
        for role in ("implementer", "reviewer"):
            path = self.tmp / "handsoff.toml"
            path.write_text(path.read_text().replace(f'{role} = "auto"', f'{role} = "host"'))
            with self.assertRaisesRegex(lib.HandsoffError, rf"\[agents\]\.{role} cannot be host"):
                lib.load_config(self.tmp)
            path.write_text(path.read_text().replace(f'{role} = "host"', f'{role} = "auto"'))

    def test_handoff_and_recovery_skip_host_role(self):
        self.set_host("supervisor")
        self.init()
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status.update(phase_number=3, phase=lib.PHASES[3], updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        self.assertIsNone(lib.managed_handoff_role(status, cfg))
        self.assertEqual(lib.recovery_assessment(status, cfg)["reason"], "host_role")

    def test_design_propose_records_and_binds(self):
        # Criterion REQ-003: host Architect proposal recording and review binding.
        self.set_host("architect")
        self.init()
        authored = run(["criterion-update", "REQ-001", "--requirement",
                        "Host design proposal is reviewable"], cwd=self.tmp)
        self.assertEqual(authored.returncode, 0, authored.stdout + authored.stderr)
        phase_two = run(["advance", "2", "20", "--status", "in_progress"], cwd=self.tmp)
        self.assertEqual(phase_two.returncode, 0, phase_two.stdout + phase_two.stderr)
        proposal = {
            "summary": "Bounded host design",
            "approach": ["Use the existing workflow boundary"],
            "tradeoffs": [],
            "decisions": ["Keep the change local"],
            "constraints": [],
            "verification": ["Run focused tests"],
        }
        proposal_path = self.tmp / "proposal.json"
        proposal_path.write_text(json.dumps(proposal))
        validated = lib.validate_design_proposal(proposal)
        result = run(["design-propose", "--file", str(proposal_path), "--by", "host-architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected_hash = hashlib.sha256(lib._canonical(validated).encode("utf-8")).hexdigest()
        self.assertTrue(result.stdout.startswith(f"DESIGN_PROPOSAL_RECORDED: {expected_hash}"))
        status = self.read_status()
        self.assertIsNone(status["design_proposal"]["session_id"])
        self.assertEqual(status["design_proposal"]["architect"], "host-architect")
        self.assertEqual(lib.read_events(self.tmp, lib.load_config(self.tmp))[-1]["kind"],
                         "design_proposal_recorded")
        self.assertFalse(any("design_proposal" in error for error in lib.validate_status_schema(status)))
        review = run(["record-design-review", "--approve", "--by", "codex-reviewer",
                      "--architect", "host-architect", "--summary", "ok"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        self.assertEqual(self.read_status()["design_review"]["proposal_hash"], expected_hash)

    def test_design_propose_refused_for_managed_architect(self):
        # Criterion REQ-003: managed Architect cannot use the host proposal path.
        self.init()
        proposal = {
            "summary": "Bounded managed design",
            "approach": ["Use the existing workflow boundary"],
            "tradeoffs": [],
            "decisions": ["Keep the change local"],
            "constraints": [],
            "verification": ["Run focused tests"],
        }
        proposal_path = self.tmp / "proposal.json"
        proposal_path.write_text(json.dumps(proposal))
        result = run(["design-propose", "--file", str(proposal_path), "--by", "host-architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SHIP_FEATURE_BLOCKED: design-propose is only for a host Architect", result.stdout)
        cfg = lib.load_config(self.tmp)
        self.assertFalse(any(event["kind"] == "design_proposal_recorded"
                             for event in lib.read_events(self.tmp, cfg)))

    def test_host_supervisor_hands_off_only_to_reviewer(self):
        # Criterion REQ-004: host Supervisor hands off only to a managed Reviewer.
        self.set_host("supervisor")
        self.init()
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status.update(phase_number=5, phase=lib.PHASES[5], assigned_role="reviewer")
        lib.commit(self.tmp, cfg, status=status, event_kind="test_phase_advance",
                   event_message="test phase setup")
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer-run", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        lib.transition_agent_session(self.tmp, session["session_id"], "running")
        lib.transition_agent_session(self.tmp, session["session_id"], "completed", exit_code=0)
        status = self.read_status()
        status["agent_sessions"][session["session_id"]]["phase_number"] = 5
        self.assertEqual(lib.managed_handoff_role(status, cfg), "reviewer")
        status.update(phase_number=4, phase=lib.PHASES[4], assigned_role="implementer")
        self.assertIsNone(lib.managed_handoff_role(status, cfg))
        status.update(phase_number=6, phase=lib.PHASES[6], assigned_role="supervisor")
        self.assertIsNone(lib.managed_handoff_role(status, cfg))

    def test_dashboard_payload_reports_host_role(self):
        # Criterion REQ-004: dashboard reports host capability and resolved profile.
        self.set_host("supervisor")
        cfg = lib.load_config(self.tmp)
        view = handsoff_dashboard._settings_view(cfg)
        self.assertIn("host", view["allowed_adapters_by_role"]["supervisor"])
        self.assertNotIn("host", view["allowed_adapters_by_role"]["reviewer"])
        self.assertEqual(lib.resolved_agent_profiles(cfg)["supervisor"]["adapter"], "host")
        self.assertIsNone(lib.resolved_agent_profiles(cfg)["supervisor"]["model"])


if __name__ == "__main__":
    unittest.main()
