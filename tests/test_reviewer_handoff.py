#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_handsoff_supervisor import normalize_fixture_config
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class ReviewerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-reviewer-handoff-"))
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        # #179: a fixture starts from the engine defaults, not the dogfood
        # repo's own preferences (the waived clicks, its [checks] and groups)
        normalize_fixture_config(self.root / "handsoff.toml")
        shutil.copy(ROOT / "handsoff-runtime.json", self.root / "handsoff-runtime.json")
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        shutil.copytree(ROOT / "prompts", self.root / "prompts")
        shutil.copytree(ROOT / "bin", self.root / "bin")
        shutil.copytree(ROOT / "dashboard", self.root / "dashboard")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.root, check=True)
        self.cli("init", "Reviewer host handoff")
        self.cli("criterion-update", "REQ-001", "--requirement", "A real design requirement",
                 "--verification", "manual", "--test", "manual: inspect")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def status(self):
        return json.loads((self.root / "handsoff-status.json").read_text())

    def test_read_only_reviewer_result_is_recorded_by_host_and_session_bound(self):
        architect = lib.create_agent_session(
            self.root, role="architect", actor="architect-one", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        lib.transition_agent_session(self.root, architect["session_id"], "running")
        lib.transition_agent_session(self.root, architect["session_id"], "completed", exit_code=0)
        self.cli("advance", "2", "20", "--status", "in_progress")
        reviewer = lib.create_agent_session(
            self.root, role="reviewer", actor="reviewer-one", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        lib.transition_agent_session(self.root, reviewer["session_id"], "running")
        result = broker.parse_reviewer_result(json.dumps({
            "kind": "design", "decision": "approved", "summary": "Design is executable",
            "findings": [], "structural_blocker": False, "symptom_reproduced": "not_applicable",
        }))
        self.assertEqual(broker.dispatch_reviewer_result(self.root, reviewer["session_id"], result), 0)
        recorded = self.status()["design_review"]
        self.assertEqual((recorded["by"], recorded["architect"], recorded["decision"]),
                         ("reviewer-one", "architect-one", "approved"))
        event = json.loads((self.root / "handsoff-events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["reviewer_session_id"], reviewer["session_id"])

        other = "hs-" + "f" * 32
        with self.assertRaisesRegex(lib.HandsoffError, "current live Reviewer"):
            broker.dispatch_reviewer_result(self.root, other, result)

    def test_nested_managed_launch_is_refused_before_build_or_spawn(self):
        with mock.patch.dict(os.environ, {agent.MANAGED_ROLE_ENV: "architect",
                                          agent.MANAGED_SESSION_ENV: "hs-" + "a" * 32}), \
                mock.patch.object(agent, "build_launch_spec") as build, \
                mock.patch.object(sys, "argv", [
                    "handsoff_agent.py", "--root", str(self.root), "launch", "reviewer", "--task", "Review",
                ]):
            self.assertEqual(agent.main(), 1)
        build.assert_not_called()

    def test_runtime_initialization_failure_has_a_distinct_classification(self):
        failure = lib.classify_runtime_failure(
            exit_code=1, stderr_tail="failed to open state DB: attempt to write a readonly database",
        )
        self.assertEqual(failure["category"], "runtime_environment")
        self.assertEqual(failure["reason"], "managed runtime initialization failed")

    def test_mixed_runtime_is_refused_before_launch_reservation(self):
        prompt = self.root / "prompts" / "reviewer.md"
        prompt.write_text(prompt.read_text() + "\nstale local edit\n")
        before = self.status()
        with self.assertRaisesRegex(lib.HandsoffError, "prompts/reviewer.md"):
            agent.build_launch_spec(self.root, "reviewer", "Review", which=lambda _name: "/bin/codex")
        self.assertEqual(self.status(), before)

    def _failed_phase2_reviewer_after_prior_changes(self):
        architect = lib.create_agent_session(
            self.root, role="architect", actor="architect-one", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        lib.transition_agent_session(self.root, architect["session_id"], "running")
        lib.transition_agent_session(self.root, architect["session_id"], "completed", exit_code=0)
        self.cli("advance", "2", "20", "--status", "in_progress")
        self.cli("record-design-review", "--by", "reviewer-prior", "--architect", "architect-one",
                 "--request-changes", "--summary", "One revision", "--finding", "Revise the boundary")
        reviewer = lib.create_agent_session(
            self.root, role="reviewer", actor="reviewer-current", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        lib.transition_agent_session(self.root, reviewer["session_id"], "running")
        lib.transition_agent_session(
            self.root, reviewer["session_id"], "failed", exit_code=1,
            failure=lib.classify_runtime_failure(exit_code=1),
        )
        return architect["session_id"], reviewer["session_id"]

    def test_recovery_uses_exact_failed_reviewer_not_phase_inferred_architect(self):
        architect_id, reviewer_id = self._failed_phase2_reviewer_after_prior_changes()
        cfg = lib.load_config(self.root)
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        status = self.status()
        self.assertEqual(lib.assigned_role(status), "architect")
        assessment = lib.recovery_assessment(
            status, cfg, {}, lib.read_events(self.root, cfg), future,
        )
        self.assertEqual((assessment["assigned_role"], assessment["lost_session_id"], assessment["state"]),
                         ("reviewer", reviewer_id, "worker_terminal"))
        self.assertNotEqual(assessment["lost_session_id"], architect_id)
        launched = []
        result = lib.recover_run(
            self.root, actor="watchdog", launcher=lambda role: launched.append(role) or 0, now=future,
        )
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(launched, ["reviewer"])

    def test_acknowledged_failed_session_does_not_rearm_the_same_hold(self):
        _, reviewer_id = self._failed_phase2_reviewer_after_prior_changes()
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        for offset in range(3):
            result = lib.recover_run(
                self.root, actor="watchdog",
                launcher=lambda _role: (_ for _ in ()).throw(RuntimeError("fixture launch failure")),
                now=future + timedelta(seconds=offset),
            )
            self.assertEqual(result["action"], "failed")
            self.assertEqual(result["attempt"], offset + 1)
        escalated = lib.recover_run(
            self.root, actor="watchdog", launcher=lambda _role: 0,
            now=future + timedelta(seconds=4),
        )
        self.assertEqual(escalated["action"], "escalated")
        self.cli("recovery-acknowledge", "--by", "pilot", "--reason", "Failure inspected")
        cfg = lib.load_config(self.root)
        assessment = lib.recovery_assessment(
            self.status(), cfg, {}, lib.read_events(self.root, cfg), future + timedelta(seconds=5),
        )
        self.assertEqual(assessment["state"], "not_applicable")
        self.assertEqual(assessment["reason"], "assigned session is terminal but not recoverable")
        self.assertNotEqual(assessment.get("lost_session_id"), reviewer_id)


if __name__ == "__main__":
    unittest.main()
