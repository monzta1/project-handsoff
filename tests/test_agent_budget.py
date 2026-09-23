#!/usr/bin/env python3
"""Focused guarantees for bounded managed-agent token use."""
from __future__ import annotations

import io
import json
import shutil
import sys
from datetime import datetime, timezone
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _CompletedProcess:
    pid = None
    returncode = 0

    def __init__(self, stdout=""):
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


class TestAgentTokenBudget(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")

    def test_codex_launch_has_role_ceiling_and_lean_tool_surface(self):
        spec = runtime.build_launch_spec(
            self.tmp, "supervisor", "Dispatch the next governed action.",
            which=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
            skip_preflight=True,
        )
        self.assertEqual(spec.token_budget, 24_000)
        budget_arg = spec.argv[spec.argv.index("-c") + 1]
        self.assertIn("features.rollout_budget={enabled=true", budget_arg)
        self.assertIn("limit_tokens=24000", budget_arg)
        self.assertIn("sampling_token_weight=1.0", budget_arg)
        self.assertIn("prefill_token_weight=1.0", budget_arg)
        disabled = [spec.argv[i + 1] for i, value in enumerate(spec.argv[:-1]) if value == "--disable"]
        self.assertEqual(tuple(disabled), runtime.CODEX_DISABLED_FEATURES)
        self.assertEqual(spec.argv[-1], "-")

    def test_implementation_delta_drives_scope_and_followup_budget_facts(self):
        self.init("Budget the actual implementation delta")
        delta = {
            "schema": 1, "previous_attempt": 1,
            "changed_files": ["bin/a.py", "dashboard/a.js", "tests/test_a.py"],
            "unresolved_findings": [], "affected_criteria": ["REQ-001"],
            "new_evidence": ["vr-example"],
        }
        with mock.patch.object(runtime, "build_role_input", return_value="bounded packet"), \
                mock.patch.object(runtime, "implementation_review_delta_packet", return_value=delta), \
                mock.patch.object(lib, "evaluate_launch_rules", return_value=None):
            spec = runtime.build_launch_spec(
                self.tmp, "reviewer", "Review the delta.",
                which=lambda name: f"/usr/local/bin/{name}", skip_preflight=True,
            )
        self.assertTrue(spec.budget_decision["followup"])
        self.assertEqual(spec.budget_decision["changed_files"], 3)

    def test_implementation_delta_is_hash_bound(self):
        self.init("Hash the implementation delta")
        status = self.read_status()
        status.update(phase_number=5, phase=lib.PHASES[5], review_attempts=[{
            "attempt": 1, "disposition": "changes_requested",
            "closed_at": "2026-01-01T00:00:00+00:00",
            "findings": [{"code": "F1", "summary": "bounded correction"}],
        }])
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        packet = runtime.implementation_review_delta_packet(self.tmp, lib.load_config(self.tmp), "reviewer")
        self.assertRegex(packet["packet_hash"], r"^[0-9a-f]{64}$")
        body = dict(packet)
        observed = body.pop("packet_hash")
        expected = __import__("hashlib").sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(observed, expected)

    def test_implementer_receives_the_exact_reviewer_approved_contract(self):
        self.init("Reviewer first implementation contract")
        acceptance = self.read_acceptance()
        acceptance["criteria"][0].update({
            "requirement": "Render the exact accepted outcome",
            "verification": "automated",
            "tests": ["python3 -m unittest tests.test_agent_budget -v"],
        })
        status = self.read_status()
        status["phase_number"] = 3
        status["phase"] = lib.PHASES[3]
        status["design_review"] = {
            "decision": "approved", "by": "independent-reviewer", "attempt": 1,
            "design_hash": lib.design_hash(acceptance["criteria"]),
        }
        self.write_acceptance(acceptance)
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))

        text = runtime.build_role_input(self.tmp, "implementer", "Build the contract.")
        self.assertIn("# Reviewer-approved implementation contract", text)
        contract_text = text.split(
            "# Reviewer-approved implementation contract\n\n", 1,
        )[1].split("\n\n", 1)[0]
        contract = json.loads(contract_text)
        self.assertEqual(contract["issued_by"], "independent-reviewer")
        self.assertEqual(contract["criteria"][0]["requirement"],
                         "Render the exact accepted outcome")
        self.assertEqual(contract["criteria"][0]["tests"],
                         ["python3 -m unittest tests.test_agent_budget -v"])
        self.assertRegex(contract["contract_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(contract["criterion_hashes"]), 1)
        repeated = runtime.build_role_input(self.tmp, "implementer", "Build the contract.")
        self.assertEqual(text, repeated)

        acceptance["criteria"][0]["requirement"] = "Unreviewed replacement"
        self.write_acceptance(acceptance)
        stale = runtime.build_role_input(self.tmp, "implementer", "Build the contract.")
        self.assertNotIn('\"issued_by\":\"independent-reviewer\"', stale)

    def test_config_is_bounded_and_tasks_cannot_become_context_dumps(self):
        config = self.tmp / "handsoff.toml"
        text = config.read_text().replace("supervisor = 24000", "supervisor = 12000", 1)
        config.write_text(text)
        self.assertEqual(lib.load_config(self.tmp)["agent_token_budgets"]["supervisor"], 12_000)

        config.write_text(text.replace("supervisor = 12000", "supervisor = 0", 1))
        with self.assertRaisesRegex(lib.HandsoffError, "agent_budget.supervisor"):
            lib.load_config(self.tmp)

        config.write_text(text)
        with self.assertRaisesRegex(lib.HandsoffError, "task exceeds"):
            runtime.build_role_input(self.tmp, "architect", "x" * (runtime.MAX_AGENT_TASK_BYTES + 1))

        self.assertEqual(runtime._effective_token_budget(40_000, "architect", {"review_attempts": 1}), 16_000)
        self.assertEqual(runtime._effective_token_budget(40_000, "reviewer", {"review_attempts": 2}), 16_000)
        # #114: with no packet but a derived follow-up budget, the derived
        # value wins over the 16k constant (still capped by the role ceiling).
        self.assertEqual(runtime._effective_token_budget(80_000, "reviewer",
                         {"review_attempts": 2, "followup_design_token_budget": 52_000}), 52_000)
        self.assertEqual(runtime._effective_token_budget(40_000, "reviewer",
                         {"review_attempts": 2, "followup_design_token_budget": 52_000}), 40_000)
        self.assertEqual(runtime._effective_token_budget(40_000, "architect", {"review_attempts": 0}), 40_000)

    def test_supervisor_narration_without_protocol_is_a_failed_session(self):
        self.init("Fail fast on no-op orchestration")
        spec = runtime.LaunchSpec(
            "supervisor", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded supervisor prompt", token_budget=24_000,
        )
        with self.assertRaisesRegex(runtime.AgentLaunchError, "without a broker request"):
            runtime.execute_launch(
                spec, popen_factory=mock.Mock(return_value=_CompletedProcess("I will do that.\n")),
                beacon_interval=0.01,
            )
        status = self.read_status()
        session = status["agent_sessions"][status["current_agent_sessions"]["supervisor"]]
        self.assertEqual(session["state"], "failed")
        failure = status["agent_failures"][session["session_id"]]
        self.assertEqual(failure["category"], "orchestration_noop")

    def test_invalid_supervisor_protocol_is_nonrecoverable(self):
        self.init("Reject invented Supervisor actions without retries")
        output = (
            'HANDSOFF_BROKER_REQUEST: {"actor":"supervisor","project_root":'
            f'{json.dumps(str(self.tmp.resolve()))},"action":"advance_phase","phase":3}}\n'
        )
        spec = runtime.LaunchSpec(
            "supervisor", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded supervisor prompt", token_budget=24_000,
        )
        with self.assertRaisesRegex(runtime.AgentLaunchError, "broker request rejected"):
            runtime.execute_launch(
                spec, popen_factory=mock.Mock(return_value=_CompletedProcess(output)),
                beacon_interval=0.01,
            )
        status = self.read_status()
        session = status["agent_sessions"][status["current_agent_sessions"]["supervisor"]]
        failure = status["agent_failures"][session["session_id"]]
        self.assertEqual(failure["category"], "orchestration_noop")
        assessment = lib.recovery_assessment(
            status, lib.load_config(self.tmp), {}, [], datetime.now(timezone.utc),
        )
        self.assertEqual((assessment["state"], assessment["reason"]),
                         ("not_applicable", "non_recoverable_failure"))

    def test_architect_protocol_persists_bounded_proposal_and_narration_fails(self):
        self.init("Bounded Architect proposal")
        payload = (
            'HANDSOFF_DESIGN_PROPOSAL: {"summary":"Small design","approach":["Change one boundary"],'
            '"tradeoffs":[],"decisions":["Keep compatibility"],"constraints":[],'
            '"verification":["Run the focused test"]}\n'
        )
        spec = runtime.LaunchSpec(
            "architect", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded architect prompt", token_budget=40_000,
        )
        self.assertEqual(runtime.execute_launch(
            spec, popen_factory=mock.Mock(return_value=_CompletedProcess(payload)),
            beacon_interval=0.01,
        ), 0)
        status = self.read_status()
        self.assertEqual(status["design_proposal"]["summary"], "Small design")
        session = status["agent_sessions"][status["current_agent_sessions"]["architect"]]
        self.assertEqual(session["state"], "completed")

        with self.assertRaisesRegex(runtime.AgentLaunchError, "without a structured design proposal"):
            runtime.execute_launch(
                spec, popen_factory=mock.Mock(return_value=_CompletedProcess("Design complete.\n")),
                beacon_interval=0.01,
            )

    def test_complete_protocol_survives_trailing_budget_exit(self):
        self.init("Keep a complete result despite trailing budget accounting")
        payload = (
            'HANDSOFF_DESIGN_PROPOSAL: {"summary":"Complete design",'
            '"approach":["Change one boundary"],"tradeoffs":[],'
            '"decisions":["Keep compatibility"],"constraints":[],'
            '"verification":["Run the focused test"]}\n'
        )
        process = _CompletedProcess(payload)
        process.returncode = 1
        process.stderr = io.StringIO("ERROR: shared rollout token budget exhausted\n")
        spec = runtime.LaunchSpec(
            "architect", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded architect prompt", token_budget=16_000,
        )
        self.assertEqual(runtime.execute_launch(
            spec, popen_factory=mock.Mock(return_value=process), beacon_interval=0.01,
        ), 0)
        status = self.read_status()
        session = status["agent_sessions"][status["current_agent_sessions"]["architect"]]
        self.assertEqual(session["state"], "completed")
        self.assertEqual(status["design_proposal"]["summary"], "Complete design")

    def test_reviewer_verdict_on_stderr_before_budget_error_is_kept(self):
        # #114: Codex wrote the verdict and the budget error to stderr
        # together; the verdict is complete and must be persisted.
        self.init("Keep a stderr verdict")
        run(["criterion-update", "REQ-001", "--requirement", "A reviewed requirement"], cwd=self.tmp)
        run(["advance", "2", "20"], cwd=self.tmp)
        verdict = ('HANDSOFF_REVIEW_RESULT: {"kind":"design","decision":"approved","summary":"fine",'
                   '"findings":[],"structural_blocker":false,"symptom_reproduced":"not_applicable","tests_executed":"no"}\n')
        process = _CompletedProcess("")
        process.returncode = 1
        process.stderr = io.StringIO(verdict + "ERROR: shared rollout token budget exhausted\n")
        spec = runtime.LaunchSpec(
            "reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded reviewer prompt", token_budget=16_000,
        )
        # Dispatch may still refuse (this fixture has no proposal to bind to);
        # what matters is that the parsed verdict survives for adoption.
        try:
            runtime.execute_launch(spec, popen_factory=mock.Mock(return_value=process), beacon_interval=0.01)
        except (runtime.AgentLaunchError, lib.HandsoffError):
            pass
        status = self.read_status()
        session = next(v for v in status["agent_sessions"].values() if v["role"] == "reviewer")
        self.assertIsNotNone(session.get("result"), session)
        self.assertEqual(session["result"]["kind"], "review")
        self.assertEqual(session["result"]["payload"]["decision"], "approved")

    def test_budget_exhaustion_never_spends_again_on_a_fallback(self):
        failure = lib.classify_runtime_failure(
            exit_code=1, stderr_tail="shared rollout token budget exhausted",
        )
        self.assertEqual(failure["category"], "token_budget_exhaustion")
        decision = lib.plan_agent_fallback(
            "architect", failure["category"],
            [{"adapter": "claude", "model": "default"}],
            {"codex": True, "claude": True}, [], 0, 2,
        )
        self.assertEqual((decision["action"], decision["reason"]),
                         ("pilot_pause", "non_recoverable_failure"))


if __name__ == "__main__":
    import unittest
    unittest.main()
