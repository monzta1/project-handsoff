#!/usr/bin/env python3
"""Focused guarantees for bounded managed-agent token use."""
from __future__ import annotations

import io
import json
import shutil
import sys
from datetime import datetime, timezone
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

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
