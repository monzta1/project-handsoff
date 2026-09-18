"""Lane G: a Pilot can run a fully managed crew from Mission Control alone.

Every test here is a code path the 2026-09-18 field proof on fm9-tone walked
by hand: #117 #118 #119 #120 #121 #122 #123 #125.
"""
import json
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT / "tests"))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402
from test_handsoff_supervisor import HandsoffTestCase, approve_design_review, run  # noqa: E402


def _commit(root, status):
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        lib.commit(root, cfg, status=status, event_kind="test_setup", event_message="test setup")


class ManagedCrewTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        import shutil
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _phase(self, number, **fields):
        status = self.read_status()
        status.update(phase_number=number, phase=lib.PHASES[number], status="in_progress", **fields)
        _commit(self.tmp, status)
        return status

    def _session(self, role, state, actor=None, phase=None, minutes_ago=0):
        session = lib.create_agent_session(self.tmp, role=role, actor=actor or f"codex-{role}", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        status = self.read_status()
        record = status["agent_sessions"][session["session_id"]]
        now = datetime.now(timezone.utc).isoformat()
        record["state"] = state
        if phase is not None:
            record["phase_number"] = phase
        if state not in lib.AGENT_SESSION_LIVE_STATES:
            record["ended_at"] = now
            record["running_at"] = record["started_at"]
            record["exit_code"] = 0 if state == "completed" else 1
            if state == "failed":
                failure = lib.classify_runtime_failure(orchestration_noop=True)
                status.setdefault("agent_failures", {})[session["session_id"]] = {
                    **failure, "session_id": session["session_id"], "at": now}
        _commit(self.tmp, status)
        return session["session_id"]

    def _inventory(self):
        return {item["kind"]: item for item in dashboard.build_snapshot(self.tmp)["operations"]["inventory"]}

    # REQ-001 (#119) ------------------------------------------------------
    def test_launch_offers_the_managed_architect_at_phase_one(self):
        self.init("First launch")
        item = self._inventory()["launch_role"]
        self.assertEqual(item["availability"], "actionable", item)
        self.assertEqual(item["launchable_roles"], ["architect"])
        self.assertRegex(item["action_id"], r"^launch_role:")

    def test_init_on_an_owned_dashboard_launches_the_architect_once(self):
        self.init("Auto start")
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp, run_token="t" * 32, root_sha256="a" * 64)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        try:
            with mock.patch.object(server, "_launch_managed_role") as launch:
                server.request_first_launch("Fix the widget (#7)")
                server._orchestrate_once()
                server._orchestrate_once()
            self.assertEqual(launch.call_count, 1)
            role, task = launch.call_args.args
            self.assertEqual(role, "architect")
            self.assertIn("Mission objective: Fix the widget (#7)", task)
            self.assertIn("HANDSOFF_DESIGN_PROPOSAL", task)
        finally:
            server.server_close()

    def test_init_endpoint_requests_the_first_launch_only_when_owned(self):
        source = (BIN / "handsoff_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("if self.server.owned_by_run:\n                    self.server.request_first_launch(requested[\"feature\"])", source)

    # REQ-002 (#120) ------------------------------------------------------
    def test_answered_question_relaunches_the_asker(self):
        self.init("Question")
        sid = self._session("architect", "completed", phase=1)
        status = self.read_status()
        status["pending_questions"] = [{"question_id": "qn-" + "1" * 32, "role": "architect", "session_id": sid,
                                        "text": "Which path?", "answer": "Concise", "answered_by": "pilot",
                                        "answered_at": datetime.now(timezone.utc).isoformat(), "delivered_at": None}]
        cfg = lib.load_config(self.tmp)
        self.assertEqual(lib.managed_handoff_role(status, cfg), "architect")
        status["pending_questions"][0]["delivered_at"] = datetime.now(timezone.utc).isoformat()
        self.assertIsNone(lib.managed_handoff_role(status, cfg))
        status["pending_questions"][0]["delivered_at"] = None
        status["agent_sessions"][sid]["state"] = "running"
        self.assertIsNone(lib.managed_handoff_role(status, cfg))

    def test_prompts_say_the_assigned_scope_needs_no_permission(self):
        for name in ("architect", "implementer", "reviewer", "supervisor"):
            text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn("The assigned scope needs no permission", text, name)

    # REQ-003 (#121) ------------------------------------------------------
    def test_architect_criteria_request_is_parsed_and_applied_on_the_host(self):
        self.init("Host records")
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1))
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        request = broker.parse_architect_request(json.dumps({
            "actor": "architect", "project_root": str(self.tmp.resolve()), "action": "criteria", "by": "codex-architect",
            "operations": [{"op": "update", "id": "REQ-001", "fields": {
                "type": "primary_fix", "requirement": "[#7] The widget stops leaking",
                "verification": "automated", "tests": ["true"]}}]}))
        code = broker.execute_architect_criteria(self.tmp, request, capability=broker._SUPERVISOR_HOST_CAPABILITY)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_acceptance()["criteria"][0]["requirement"], "[#7] The widget stops leaking")
        for bad in ('{"actor":"supervisor","project_root":"x","action":"criteria","operations":[{}],"by":"a"}',
                    '{"actor":"architect","project_root":"x","action":"launch_role","operations":[{}],"by":"a"}',
                    '{"actor":"architect","project_root":"x","action":"criteria","operations":[],"by":"a"}'):
            with self.assertRaises(lib.HandsoffError):
                broker.parse_architect_request(bad)

    def test_runner_applies_the_criteria_request_before_the_proposal(self):
        source = (BIN / "handsoff_agent.py").read_text(encoding="utf-8")
        self.assertIn("_parse_architect_request_line(line, architect_requests, protocol_errors)", source)
        self.assertLess(source.index("if architect_requests:"), source.index("if architect_results:\n        try:\n            lib.record_design_proposal"))

    def test_sandboxed_roles_are_told_the_host_records_for_them(self):
        self.init("Sandbox note")
        for role in ("architect", "reviewer"):
            text = runtime.build_role_input(self.tmp, role, "task")
            self.assertIn("# Sandbox", text, role)
            self.assertIn("Do not run `handsoff supervisor` commands", text)
        self.assertNotIn("# Sandbox", runtime.build_role_input(self.tmp, "implementer", "task"))

    def test_orchestration_tasks_are_role_specific(self):
        status = {"next_action": "Get an independent reviewer to run `record-review`."}
        reviewer = lib.orchestration_task("reviewer", status)
        self.assertNotIn("record-review", reviewer)
        self.assertIn("HANDSOFF_REVIEW_RESULT", reviewer)
        self.assertIn("Get an independent reviewer", lib.orchestration_task("supervisor", status))
        self.assertIn("Mission objective: X", lib.orchestration_task("architect", status, objective="X"))

    def test_approval_with_structural_blocker_is_refused_as_a_contradiction(self):
        payload = json.dumps({"kind": "design", "decision": "approved", "summary": "ok", "findings": [],
                              "structural_blocker": True, "symptom_reproduced": "not_applicable"})
        with self.assertRaisesRegex(lib.HandsoffError, "contradicts"):
            broker.parse_reviewer_result(payload)
        payload = json.dumps({"kind": "implementation", "decision": "approved", "summary": "ok", "findings": [],
                              "structural_blocker": True, "symptom_reproduced": "yes"})
        with self.assertRaisesRegex(lib.HandsoffError, "never a structural blocker"):
            broker.parse_reviewer_result(payload)

    # REQ-004 (#122) ------------------------------------------------------
    def test_full_evidence_at_phase_four_assigns_the_supervisor(self):
        self.init("Hand off")
        self.set_criterion_state("passing", resolved=True)
        status = self._phase(4)
        self.assertTrue(lib.implementation_evidence_complete(status))
        self.assertEqual(lib.assigned_role(status), "supervisor")
        self._session("implementer", "completed", phase=4)
        cfg = lib.load_config(self.tmp)
        self.assertEqual(lib.managed_handoff_role(self.read_status(), cfg), "supervisor")

    def test_partial_evidence_keeps_the_implementer(self):
        self.init("Still building")
        status = self._phase(4)
        self.assertFalse(lib.implementation_evidence_complete(status))
        self.assertEqual(lib.assigned_role(status), "implementer")

    def test_advance_five_defaults_implemented_by_to_the_completed_implementer(self):
        self.init("Implementer actor")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(4)
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        self._session("implementer", "completed", actor="codex-implementer-9", phase=4)
        advanced = run(["advance", "5"], cwd=self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["implemented_by"], "codex-implementer-9")

    # REQ-005 (#123) ------------------------------------------------------
    def test_failed_reviewer_does_not_block_relaunch_and_pause_is_acknowledgeable(self):
        self.init("Dead end")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="impl-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        sid = self._session("reviewer", "failed", phase=5)
        inventory = self._inventory()
        self.assertEqual(inventory["launch_role"]["availability"], "actionable", inventory["launch_role"])
        self.assertEqual(inventory["launch_role"]["launchable_roles"], ["reviewer"])
        ack = inventory["recovery_acknowledge"]
        self.assertEqual(ack["availability"], "actionable", ack)
        self.assertIn("orchestration_noop", ack["consequence"])
        self.assertEqual(inventory["recover"]["availability"], "unavailable")
        cleared = run(["recovery-acknowledge", "--by", "pilot", "--reason", "relaunching"], cwd=self.tmp)
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        status = self.read_status()
        self.assertTrue(status["agent_failures"][sid]["acknowledged"])
        self.assertEqual(lib.validate_status_schema(status), [])
        cfg = lib.load_config(self.tmp)
        self.assertNotEqual(lib.recovery_assessment(status, cfg, {}, [], root=self.tmp)["reason"], "non_recoverable_failure")
        self.assertEqual(self._inventory()["recovery_acknowledge"]["availability"], "unavailable")

    # REQ-006 (#118) ------------------------------------------------------
    def test_init_retires_a_complete_run_and_refuses_an_in_progress_one(self):
        self.init("First mission")
        status = self.read_status()
        status.update(phase_number=8, phase=lib.PHASES[8], status="complete", progress=100)
        _commit(self.tmp, status)
        again = run(["init", "Second mission"], cwd=self.tmp)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("HANDSOFF_RETIRED:", again.stdout)
        archived = sorted((self.tmp / ".handsoff-archive").iterdir())
        self.assertEqual(len(archived), 1)
        self.assertTrue((archived[0] / "handsoff-status.json").is_file())
        self.assertTrue((archived[0] / "handsoff-events.jsonl").is_file())
        self.assertEqual(self.read_status()["feature"], "Second mission")
        self.assertEqual(self.read_status()["phase_number"], 1)
        refused = run(["init", "Third mission"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("HANDSOFF_INIT_SKIPPED", refused.stdout)
        self.assertEqual(self.read_status()["feature"], "Second mission")
        # A run at Phase 8 that has not recorded completion is still in progress.
        status = self.read_status()
        status.update(phase_number=8, phase=lib.PHASES[8], status="in_progress")
        _commit(self.tmp, status)
        refused = run(["init", "Fourth mission"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertEqual(self.read_status()["feature"], "Second mission")
        self.assertEqual(len(list((self.tmp / ".handsoff-archive").iterdir())), 1)

    # REQ-007 (#117) ------------------------------------------------------
    def test_engine_identity_is_ledgered_and_reported(self):
        self.init("Engine on the ledger")
        events = lib.read_events(self.tmp, lib.load_config(self.tmp))
        first = events[0]
        self.assertEqual(first["kind"], "initialized")
        self.assertEqual(set(first["engine"]), {"version", "source", "manifest_sha256"})
        self._session("architect", "completed", phase=1)
        events = lib.read_events(self.tmp, lib.load_config(self.tmp))
        launch = next(e for e in events if e["kind"] == "agent_session_launching")
        self.assertEqual(launch["engine"]["version"], first["engine"]["version"])
        shown = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(len(shown["engine_history"]), 1)
        self.assertEqual(shown["engine_history"][0]["version"], first["engine"]["version"])
        printed = subprocess.run([sys.executable, str(ROOT / "tools" / "run_evidence.py"), str(self.tmp)],
                                 capture_output=True, text=True)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn(first["engine"]["version"], printed.stdout)
        self.assertNotIn("not ledgered", printed.stdout)

    # REQ-009 (#125) ------------------------------------------------------
    def test_record_review_rewrites_next_action_and_orchestration_never_re_reviews(self):
        self.init("Once reviewed")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="impl-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        self._session("reviewer", "completed", phase=5)
        review = run(["record-review", "--by", "reviewer-1", "--symptom-reproduced", "yes", "--tests-executed", "yes"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        status = self.read_status()
        self.assertIn("advance 6", status["next_action"])
        self.assertIn("Supervisor advances to Phase 6", status["next_action"])
        cfg = lib.load_config(self.tmp)
        self.assertEqual(lib.managed_handoff_role(status, cfg), "supervisor")
        status["review"] = status["review"]  # unchanged: a second reviewer is never selected
        self.assertNotEqual(lib.managed_handoff_role(status, cfg), "reviewer")


if __name__ == "__main__":
    unittest.main()
