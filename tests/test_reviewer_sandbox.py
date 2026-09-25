"""Focused REQ-003, REQ-004, and REQ-007 reviewer boundary checks."""
from __future__ import annotations

import shutil
import sys
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
from tests.fixture_state import write_version_pin


class ReviewerSandboxTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def test_reviewer_spec_isolated_and_supervisor_stays_root(self):
        reviewer = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.",
            which=lambda name: "/usr/local/bin/codex" if name == "codex" else None)
        self.assertNotEqual(reviewer.cwd, str(self.tmp.resolve()))
        self.assertTrue(__import__("pathlib").Path(reviewer.cwd).is_dir())
        self.assertEqual(reviewer.env_overrides["TMPDIR"], reviewer.cwd)
        self.assertIn("--skip-git-repo-check", reviewer.argv)
        self.assertIn("--sandbox", reviewer.argv)
        self.assertIn("workspace-write", reviewer.argv)
        self.assertIn(str(self.tmp.resolve()), reviewer.stdin)
        self.assertIn("git -C", reviewer.stdin)
        supervisor = runtime.build_launch_spec(self.tmp, "supervisor", "Review it.",
            which=lambda name: "/usr/local/bin/codex" if name == "codex" else None)
        self.assertEqual(supervisor.cwd, str(self.tmp.resolve()))
        self.assertIn("read-only", supervisor.argv)

    def test_reviewer_network_is_denied_while_implementer_network_is_declared(self):
        import handsoff_lib as lib
        which = lambda name: "/usr/local/bin/codex" if name == "codex" else None  # noqa: E731
        reviewer = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=which)
        argv = list(reviewer.argv)
        self.assertNotIn(lib.CODEX_WORKSPACE_NETWORK_FLAG, argv)
        self.assertIn("workspace-write", argv)
        self.assertEqual(reviewer.reviewer_isolation["network_policy"], "denied")
        implementer = lib.codex_argv("/usr/local/bin/codex", "implementer", "default", 80000)
        self.assertIn(lib.CODEX_WORKSPACE_NETWORK_FLAG, implementer)
        for role in ("architect", "supervisor"):
            read_only = lib.codex_argv("/usr/local/bin/codex", role, "default", 40000)
            self.assertIn("read-only", read_only)
            self.assertNotIn(lib.CODEX_WORKSPACE_NETWORK_FLAG, read_only)
        supervisor = runtime.build_launch_spec(self.tmp, "supervisor", "Review it.", which=which)
        self.assertNotIn(lib.CODEX_WORKSPACE_NETWORK_FLAG, supervisor.argv)

    def test_claude_reviewer_is_refused_before_scratch_or_session(self):
        import handsoff_lib as lib
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace('reviewer = "auto"', 'reviewer = "claude"', 1)
        text = text.replace("compatibility_mode = true", "compatibility_mode = false")
        text = text.replace("compatibility_approved = true", "compatibility_approved = false")
        toml.write_text(text)
        before = set(__import__("pathlib").Path("/tmp").glob("handsoff-reviewer-*"))
        with self.assertRaisesRegex(lib.HandsoffError, "before session reservation"):
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.",
                                      which=lambda name: f"/usr/local/bin/{name}")
        after = set(__import__("pathlib").Path("/tmp").glob("handsoff-reviewer-*"))
        self.assertEqual(after, before)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())

    def test_compatibility_contract_requires_explicit_approval_and_detects_tampering(self):
        import handsoff_lib as lib
        waiting = lib.reviewer_isolation_contract("claude", {
            "reviewer_isolation": {"compatibility_mode": True, "compatibility_approved": False}})
        self.assertEqual(waiting["decision"], "approval_required")
        approved = lib.reviewer_isolation_contract("claude", {
            "reviewer_isolation": {"compatibility_mode": True, "compatibility_approved": True}})
        self.assertEqual((approved["enforcement"], approved["decision"]),
                         ("approved_compatibility", "enforce"))
        approved["network_policy"] = "denied"
        with self.assertRaisesRegex(lib.HandsoffError, "digest"):
            lib.validate_reviewer_isolation_contract(approved)

    def test_reviewer_environment_removes_credentials_and_keeps_external_scratch(self):
        cleaned = runtime._reviewer_environment({
            "PATH": "/bin", "API_TOKEN": "secret-value", "PASSWORD": "hidden",
            "ORDINARY": "visible",
        }, {"TMPDIR": "/tmp/reviewer-scratch"})
        self.assertEqual(cleaned["PATH"], "/bin")
        self.assertEqual(cleaned["ORDINARY"], "visible")
        self.assertEqual(cleaned["TMPDIR"], "/tmp/reviewer-scratch")
        self.assertNotIn("API_TOKEN", cleaned)
        self.assertNotIn("PASSWORD", cleaned)

    def test_unsupported_finding_is_other_and_tests_default_is_unknown(self):
        result = broker.parse_reviewer_result('{"kind":"implementation","decision":"changes_requested",'
            '"summary":"x","findings":["EVIDENCE: x"],"structural_blocker":false,'
            '"symptom_reproduced":"yes"}')
        self.assertEqual(result["findings"], ["other: EVIDENCE: x"])
        self.assertEqual(result["tests_executed"], "unknown")


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _FakeReviewerProcess:
    """A completed Codex child whose stdout is one review result line; an
    optional side effect runs before the runner reads stdout, standing in
    for a reviewer that wrote into the project tree."""
    pid = None
    returncode = 0

    def __init__(self, stdout, side_effect=None):
        import io
        if side_effect:
            side_effect()
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


class ReviewerGuardAndBrokerTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _reviewer_spec(self):
        return runtime.LaunchSpec(
            "reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded reviewer prompt", token_budget=40_000,
            project_root=str(self.tmp.resolve()),
        )

    def _reach_phase_5(self):
        self.init("Reviewer guard fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def test_reviewer_that_writes_into_project_is_failed_and_result_discarded(self):
        self._reach_phase_5()
        approved = ('HANDSOFF_REVIEW_RESULT: {"kind":"implementation","decision":"approved",'
                    '"summary":"looks fine","findings":[],"structural_blocker":false,'
                    '"symptom_reproduced":"yes","tests_executed":"yes"}\n')
        probe = self.tmp / "PROBE.py"
        factory = mock.Mock(side_effect=lambda *a, **k: _FakeReviewerProcess(
            approved, side_effect=lambda: probe.write_text("print('written by reviewer')\n")))
        with self.assertRaisesRegex(runtime.AgentLaunchError, "modified the project tree"):
            runtime.execute_launch(self._reviewer_spec(), popen_factory=factory, beacon_interval=0.01)
        status = self.read_status()
        session = status["agent_sessions"][status["current_agent_sessions"]["reviewer"]]
        self.assertEqual(session["state"], "failed")
        self.assertEqual(status["agent_failures"][session["session_id"]]["category"],
                         "reviewer_modified_project")
        self.assertIsNone(status.get("review"))

    def test_tests_executed_lands_on_review_and_attempt(self):
        self._reach_phase_5()
        approved = ('HANDSOFF_REVIEW_RESULT: {"kind":"implementation","decision":"approved",'
                    '"summary":"ledger only","findings":[],"structural_blocker":false,'
                    '"symptom_reproduced":"not_applicable","tests_executed":"no"}\n')
        factory = mock.Mock(return_value=_FakeReviewerProcess(approved))
        self.assertEqual(runtime.execute_launch(self._reviewer_spec(), popen_factory=factory,
                                                beacon_interval=0.01), 0)
        status = self.read_status()
        self.assertEqual(status["review"]["tests_executed"], "no")
        closed = [item for item in status["review_attempts"] if item.get("disposition") == "approved"]
        self.assertEqual(closed[-1]["tests_executed"], "no")

    def test_broker_binds_host_architect_from_proposal(self):
        import handsoff_lib as lib
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('architect = "codex"', 'architect = "host"')
                        .replace('architect = "claude"', 'architect = "host"'))
        self.init("Host architect broker fixture")
        advanced = run(["advance", "2", "10"], cwd=self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        lib.record_design_proposal(self.tmp, None, {
            "summary": "host design", "approach": ["one"], "tradeoffs": [], "decisions": ["one"],
            "constraints": [], "verification": ["one"]}, architect_actor="host-architect")
        session = lib.create_agent_session(
            self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
            requested_model="default", resolution_source="configured")
        result = broker.parse_reviewer_result(
            '{"kind":"design","decision":"approved","summary":"ok","findings":[],'
            '"structural_blocker":false,"symptom_reproduced":"not_applicable"}')
        request = broker._reviewer_result_request(self.tmp, session["session_id"], result)
        self.assertEqual(request["architect"], "host-architect")
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["design_proposal"] = None
        lib.commit(self.tmp, cfg, status=status, event_kind="test_proposal_cleared",
                   event_message="test cleared the proposal")
        with self.assertRaisesRegex(lib.HandsoffError, "no managed Architect identity"):
            broker._reviewer_result_request(self.tmp, session["session_id"], result)
