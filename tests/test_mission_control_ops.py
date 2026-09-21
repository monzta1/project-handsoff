"""REQ-001 and REQ-008 focused checks for the operations inventory."""
import hashlib
import http.client
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, approve_design_review, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


KINDS = ["design_approve", "design_reject", "deployment_approve", "deployment_revoke", "deployment_hold",
         "design_review_authorize", "design_review_escalate", "review_cap_override",
         "recovery_acknowledge", "recover", "pause", "resume", "run_close", "run_reopen",
         "regression_accept", "regression_decline", "regression_cancel", "launch_role",
         "verify_criterion", "verify_live"]


class MissionControlOpsTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def inventory(self):
        return dashboard.build_snapshot(self.tmp)["operations"]["inventory"]

    def test_fresh_inventory_is_complete_and_ordered(self):
        self.init()
        items = self.inventory()
        self.assertEqual([x["kind"] for x in items], KINDS)
        self.assertEqual({x["availability"] for x in items}, {"actionable", "unavailable"})  # #183: no read_only rows
        self.assertIn("design review", next(x["reason"] for x in items if x["kind"] == "design_approve"))

    def test_reviewed_design_has_bound_action(self):
        self.init(); run(["criterion-update", "REQ-001", "--requirement", "A reviewed design is required"], cwd=self.tmp); run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(approve_design_review(self.tmp).returncode, 0)
        snapshot = dashboard.build_snapshot(self.tmp)
        item = next(x for x in snapshot["operations"]["inventory"] if x["kind"] == "design_approve")
        legacy = next(x for x in snapshot["operator_actions"] if x["kind"] == "design_approve")
        self.assertEqual(item["availability"], "actionable")
        self.assertTrue(item["action_id"].endswith(legacy["binding"]))

    def test_closed_run_reports_closed_gates(self):
        self.init(); lib.close_run(self.tmp, by="test", reason="fixture")
        for item in self.inventory():
            if item["kind"] == "run_reopen": continue
            if item["availability"] == "actionable": self.fail(item)
        self.assertIn("closed", next(x["reason"] for x in self.inventory() if x["kind"] == "run_close"))

    def test_completed_run_has_no_actionable_entries(self):
        self.init(); status = self.read_status(); status.update(status="complete", phase_number=8)
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, acceptance=self.read_acceptance(), event_kind="fixture", event_message="complete", actor="test")
        self.assertFalse([x for x in self.inventory() if x["availability"] == "actionable"])

    def test_engine_operations_are_not_in_the_inventory(self):
        # #183: they are CLI commands and could never act from a served
        # dashboard; three permanent READ ONLY rows said nothing about the run.
        self.init()
        kinds = {x["kind"] for x in self.inventory()}
        self.assertFalse(kinds & {"engine_upgrade", "engine_rollback", "engine_migrate"}, kinds)
        self.assertFalse([x for x in self.inventory() if x["availability"] == "read_only"])

    def test_dashboard_http_contains_inventory_and_legacy_list(self):
        self.init()
        try: server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError: self.skipTest("managed test environment disallows loopback binds")
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            conn = http.client.HTTPConnection(*server.server_address, timeout=3)
            conn.request("GET", "/api/dashboard", headers={"Origin": "http://127.0.0.1"})
            response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
            self.assertEqual(response.status, 200); self.assertIn("operations", payload)
            self.assertIsInstance(payload["operator_actions"], list)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_auto_handoff_config_is_boolean(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(__import__("re").sub(r"^auto_handoff = .*$", "", toml.read_text(), flags=__import__("re").M))
        self.assertTrue(lib.load_config(self.tmp)["auto_handoff"], "default is true when the key is absent")
        path = self.tmp / "handsoff.toml"
        path.write_text(path.read_text().replace("[workflow]", "[workflow]\nauto_handoff = false", 1))
        self.assertFalse(lib.load_config(self.tmp)["auto_handoff"])
        path.write_text(path.read_text().replace("auto_handoff = false", 'auto_handoff = "no"', 1))
        with self.assertRaisesRegex(lib.HandsoffError, "workflow.auto_handoff"):
            lib.load_config(self.tmp)

    def test_auto_handoff_false_never_launches(self):
        self.init(); path = self.tmp / "handsoff.toml"; path.write_text(path.read_text().replace("auto_handoff = true", "auto_handoff = false", 1))
        try: server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError: self.skipTest("managed test environment disallows loopback binds")
        try:
            with mock.patch.object(lib, "managed_handoff_role", return_value="reviewer"), mock.patch.object(server, "_launch_managed_role") as launch:
                server._orchestrate_once()
                launch.assert_not_called()
        finally: server.server_close()

    def _phase(self, number):
        phases = {1: "Orient", 2: "Design debate", 3: "Design approved", 4: "Implementation",
                  5: "Independent review", 6: "Checks & documentation", 7: "Awaiting deployment approval",
                  8: "Live verified"}
        status = self.read_status(); status["phase_number"] = number; status["phase"] = phases[number]; status["status"] = "in_progress"
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="fixture_phase", event_message="fixture phase", actor="test")

    def test_phase_four_implementer_launch_is_actionable_with_profile(self):
        self.init(); self._phase(4)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        self.assertEqual(item["availability"], "actionable")
        self.assertEqual(item["launchable_roles"], ["implementer"])
        self.assertEqual(item["role"], "implementer")
        # the consequence names the adapter that would launch; on a runner
        # with neither codex nor claude installed it reads None, which is
        # the truth there, so the assertion is on the shape (#179)
        self.assertRegex(item["consequence"], r"launches a managed implementer: (codex|claude|None) \(default\), budget 120000 tokens")

    def test_live_implementer_makes_launch_unavailable(self):
        self.init(); self._phase(4)
        lib.create_agent_session(self.tmp, role="implementer", actor="test-agent",
                                 adapter="codex", requested_model="default",
                                 resolution_source="configured")
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        self.assertEqual(item["availability"], "unavailable")
        self.assertIn("live implementer", item["reason"])

    def test_phase_two_reviewer_requires_proposal_then_allows_launch(self):
        self.init(); self._phase(2)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        self.assertIn("design proposal", item["reason"])
        status = self.read_status(); status["design_proposal"] = {"based_on_review_attempt": 0}
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="fixture_proposal", event_message="fixture proposal", actor="test")
        self.assertEqual(next(x for x in self.inventory() if x["kind"] == "launch_role")["availability"], "actionable")

    def test_launch_role_action_id_is_state_bound(self):
        self.init(); self._phase(4)
        self.assertRegex(next(x for x in self.inventory() if x["kind"] == "launch_role")["action_id"], r"^launch_role:")

    def test_launch_role_offers_the_managed_supervisor_at_phase_three(self):
        # #119: every managed role is launchable from Mission Control.
        self.init(); self._phase(3)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        self.assertEqual(item["availability"], "actionable", item)
        self.assertEqual(item["launchable_roles"], ["supervisor"])

    def test_launch_role_unavailable_for_a_host_supervisor(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        import re
        toml.write_text(re.sub(r'^supervisor = "[a-z]+"$', 'supervisor = "host"', toml.read_text(), count=1, flags=re.M))
        self._phase(3)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        self.assertEqual(item["availability"], "unavailable")
        self.assertIn("host-driven", item["reason"])

    def test_dashboard_payload_does_not_expose_environment_secrets(self):
        self.init(); os.environ["MISSION_TEST_TOKEN"] = "never-include-this-value"
        try:
            self.assertNotIn(os.environ["MISSION_TEST_TOKEN"], json.dumps(dashboard.build_snapshot(self.tmp)))
        finally:
            os.environ.pop("MISSION_TEST_TOKEN", None)

    def test_verify_criterion_is_unavailable_before_phase_four(self):
        self.init()
        item = next(x for x in self.inventory() if x["kind"] == "verify_criterion")
        self.assertEqual(item["availability"], "unavailable")
        self.assertIn("phase", item["reason"])

    def test_verify_criterion_lists_automated_criteria_in_phase_four(self):
        self.init(); self._phase(4)
        item = next(x for x in self.inventory() if x["kind"] == "verify_criterion")
        self.assertEqual(item["availability"], "actionable")
        self.assertTrue(item["criteria"])

    def test_verify_criterion_explains_inflight_lock(self):
        # A held flock is in flight; a leftover lock file from a finished
        # verify is not (lock files are never unlinked).
        import fcntl
        self.init(); self._phase(4)
        lock_dir = self.tmp / lib.VERIFY_INFLIGHT_DIR; lock_dir.mkdir()
        stale = lock_dir / "stale.lock"; stale.touch()
        item = next(x for x in self.inventory() if x["kind"] == "verify_criterion")
        self.assertEqual(item["availability"], "actionable", item)
        self.assertEqual(lib.verify_inflight_bindings(self.tmp), [])
        held = lock_dir / "held.lock"; held.touch()
        with held.open("r+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            item = next(x for x in self.inventory() if x["kind"] == "verify_criterion")
            self.assertIn("in flight", item["reason"])
            self.assertEqual(lib.verify_inflight_bindings(self.tmp), ["held"])
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_verify_live_is_unavailable_before_phase_seven(self):
        self.init(); self._phase(4)
        item = next(x for x in self.inventory() if x["kind"] == "verify_live")
        self.assertEqual(item["availability"], "unavailable")
        self.assertIn("Phase 7", item["reason"])

    def test_verify_live_is_actionable_at_phase_seven(self):
        self.init(); self._phase(7)
        status = self.read_status(); status["deployment_approved"] = {"by": "test"}
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="fixture_deployment", event_message="fixture deployment", actor="test")
        self.assertEqual(next(x for x in self.inventory() if x["kind"] == "verify_live")["availability"], "actionable")

    def test_verification_payload_has_bounded_shape(self):
        self.init()
        value = dashboard.build_snapshot(self.tmp)["operations"]["verification"]
        self.assertEqual(set(value), {"in_flight", "latest", "live"})

    def test_engine_payload_has_all_stable_commands(self):
        self.init()
        engine = dashboard.build_snapshot(self.tmp)["operations"]["engine"]
        self.assertEqual(set(engine["commands"]), {"install", "upgrade_preview", "upgrade", "rollback_preview",
                                                     "rollback", "migrate_preview", "migrate", "doctor"})
        self.assertTrue(all(str(self.tmp) in command for command in engine["commands"].values()))

    def test_engine_execution_is_unavailable_and_the_run_page_carries_no_previews(self):
        self.init()
        engine = dashboard.build_snapshot(self.tmp)["operations"]["engine"]
        self.assertEqual(engine["execution"], "unavailable")
        self.assertIn("dashboard is serving this root", engine["execution_reason"])
        # #184: the version line and the command list stay; the previews went
        self.assertNotIn("previews", engine)
        self.assertEqual(len(engine["commands"]), 8)


class LaunchRoleHttpTests(HandsoffTestCase):
    """REQ-002 over HTTP: accepted launches are audited by hash only, stale
    and over-long requests are refused with distinct reasons and audited
    as refused. These bind loopback sockets, which the managed Codex
    sandbox cannot, so the host runs them."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def inventory(self):
        return dashboard.build_snapshot(self.tmp)["operations"]["inventory"]

    def _phase(self, phase):
        return MissionControlOpsTests._phase(self, phase)

    def _serve(self):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def _post(self, server, path, body):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("POST", path, body=json.dumps(body),
                           headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def _launch_events(self):
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        return [e for e in events if e.get("kind") == "pilot_launch_requested"]

    def test_accepted_launch_is_audited_by_hash_and_started(self):
        self.init(); self._phase(4)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        task = "Implement REQ-001 exactly as specified; SECRET-LIKE-TASK-TEXT"
        server = self._serve()
        try:
            with mock.patch.object(dashboard.DashboardServer, "_launch_managed_role", return_value=0) as launch:
                status, payload = self._post(server, "/api/launch-role",
                                             {"action_id": item["action_id"], "role": "implementer", "task": task})
                for _ in range(50):
                    if launch.called:
                        break
                    threading.Event().wait(0.05)
        finally:
            server.server_close()
        self.assertEqual((status, payload["ok"], payload["role"]), (200, True, "implementer"))
        self.assertTrue(launch.called)
        self.assertEqual(launch.call_args.args[:2], ("implementer", task))
        self.assertEqual(launch.call_args.args[2], "Mission Control Pilot")
        audited = self._launch_events()
        self.assertEqual(len(audited), 1)
        self.assertTrue(audited[0]["accepted"])
        self.assertEqual(audited[0]["task_sha256"], hashlib.sha256(task.encode("utf-8")).hexdigest())
        self.assertNotIn("SECRET-LIKE-TASK-TEXT", (self.tmp / "handsoff-events.jsonl").read_text())
        self.assertNotIn("SECRET-LIKE-TASK-TEXT", (self.tmp / "handsoff-status.json").read_text())

    def test_stale_and_overlong_and_wrong_role_are_refused_and_audited(self):
        self.init(); self._phase(4)
        item = next(x for x in self.inventory() if x["kind"] == "launch_role")
        server = self._serve()
        try:
            with mock.patch.object(dashboard.DashboardServer, "_launch_managed_role", return_value=0) as launch:
                stale = self._post(server, "/api/launch-role",
                                   {"action_id": "launch_role:0000000000000000", "role": "implementer", "task": "x"})
                overlong = self._post(server, "/api/launch-role",
                                      {"action_id": item["action_id"], "role": "implementer", "task": "x" * 4001})
                wrong = self._post(server, "/api/launch-role",
                                   {"action_id": item["action_id"], "role": "reviewer", "task": "x"})
        finally:
            server.server_close()
        self.assertFalse(launch.called)
        self.assertEqual([stale[0], overlong[0], wrong[0]], [409, 409, 409])
        reasons = {stale[1]["error"], overlong[1]["error"], wrong[1]["error"]}
        self.assertEqual(len(reasons), 3, reasons)
        self.assertIn("stale", stale[1]["error"])
        self.assertIn("4000", overlong[1]["error"])
        audited = self._launch_events()
        self.assertEqual(len(audited), 3)
        self.assertTrue(all(e["accepted"] is False and e["reason"] for e in audited))


class LaunchActorAttributionTests(HandsoffTestCase):
    """Only a Pilot-initiated launch carries the Pilot's name; watchdog and
    orchestration launches keep the runtime's adapter-role identity."""

    def test_default_launch_actor_is_not_the_pilot(self):
        import handsoff_agent
        server = dashboard.DashboardServer.__new__(dashboard.DashboardServer)
        server.project_root = self.tmp
        with mock.patch.object(handsoff_agent, "build_launch_spec", return_value="spec"), \
                mock.patch.object(handsoff_agent, "execute_with_recovery", return_value=0) as run_it:
            dashboard.DashboardServer._launch_managed_role(server, "reviewer", "resume")
            dashboard.DashboardServer._launch_managed_role(server, "reviewer", "pilot task", "Mission Control Pilot")
        self.assertEqual([c.kwargs.get("actor") for c in run_it.call_args_list], [None, "Mission Control Pilot"])


class DeploymentApprovalHttpTests(LaunchRoleHttpTests):
    """The deployment approval buttons must reach the real gate, not a
    mocked one: v0.3.17 to v0.3.19 crashed on a Namespace missing the
    --revoke flag (field proof finding, #108)."""

    def _ready_for_deployment(self):
        self.init("Approve from Mission Control")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def test_legacy_endpoint_records_a_real_approval(self):
        self._ready_for_deployment()
        server = self._serve()
        try:
            status, payload = self._post(server, "/api/deployment-approval", {})
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_status()["deployment_approved"]["by"], "Mission Control Pilot")

    def test_operator_action_records_a_real_approval(self):
        self._ready_for_deployment()
        action = next(a for a in dashboard.build_snapshot(self.tmp)["operator_actions"] if a["kind"] == "deployment_approve")
        server = self._serve()
        try:
            status, payload = self._post(server, "/api/operator-action", {"action_id": action["action_id"], "reason": None})
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_status()["deployment_approved"]["by"], "Mission Control Pilot")


class DesignApprovalGateReasonTests(LaunchRoleHttpTests):
    """A Pilot who presses AUTHORIZE DESIGN while the gate cannot take it
    must learn why: the card names the blockers before the click, the
    button is parked, and the endpoint relays the gate's own refusal text
    (field report 2026-09-18: two buttons on screen, neither "worked",
    the reason lived only in the server's stdout)."""

    def _reviewed_with_unscoped_item(self):
        started = run(["init", "Approve with an unscoped item", "--item", "#1 Scoped item"], cwd=self.tmp)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        authored = run(["criterion-update", "REQ-001", "--requirement",
                        "[#1] Dashboard approval fixture has a real acceptance criterion"], cwd=self.tmp)
        self.assertEqual(authored.returncode, 0, authored.stdout + authored.stderr)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#94 Unscoped follow-up"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        reviewed = approve_design_review(self.tmp, architect="arch-ui", reviewer="reviewer-ui")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)

    def test_blockers_are_named_on_the_card_and_the_endpoint_relays_the_gate_reason(self):
        self._reviewed_with_unscoped_item()
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot["input_required"]
        self.assertEqual(request["kind"], "design_approval")
        self.assertEqual(len(request["blockers"]), 1)
        self.assertIn("issue-94", request["blockers"][0])
        self.assertIn("cannot take the authorization yet", request["message"])
        self.assertIn("issue-94", request["message"])
        action = next(a for a in snapshot["operator_actions"] if a["kind"] == "design_approve")
        server = self._serve()
        try:
            legacy_status, legacy = self._post(server, "/api/design-approval", {})
            action_status, via_action = self._post(server, "/api/operator-action",
                                                   {"action_id": action["action_id"], "reason": None})
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(legacy_status, 409, legacy)
        self.assertIn("required work items have no criteria: issue-94", legacy["error"])
        self.assertEqual(action_status, 409, via_action)
        self.assertIn("required work items have no criteria: issue-94", via_action["error"])
        self.assertIsNone(self.read_status()["design_approved"])

    def test_clearing_the_blocker_clears_the_card_and_the_button_approves(self):
        self._reviewed_with_unscoped_item()
        removed = run(["work-item-remove", "issue-94", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        request = dashboard.build_snapshot(self.tmp)["input_required"]
        self.assertEqual((request["kind"], request["blockers"]), ("design_approval", []))
        self.assertNotIn("cannot take", request["message"])
        server = self._serve()
        try:
            status, payload = self._post(server, "/api/design-approval", {})
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_status()["design_approved"]["by"], "Mission Control Pilot")

    def test_design_approval_blockers_helper(self):
        cfg = lib.load_config(self.tmp)
        status = {"work_item_delivery": {}}
        acceptance = {"criteria": [{"id": "REQ-001", "requirement": lib.PLACEHOLDER_REQUIREMENT,
                                    "tests": list(lib.PLACEHOLDER_TESTS), "state": "failing"}],
                      "work_items": []}
        blockers = lib.design_approval_blockers(status, acceptance, cfg)
        self.assertEqual(len(blockers), 1)
        self.assertIn("placeholder", blockers[0])
        self.assertEqual(lib.design_approval_blockers(status, {"criteria": [{"id": "REQ-001", "requirement": "[#1] real", "tests": ["x"]}], "work_items": []}, cfg), [])


class GateCaptureThreadSafetyTests(HandsoffTestCase):
    """_run_gate must capture only its own thread's output. The dashboard's
    orchestration and watchdog threads print concurrently with gate calls,
    so a process-global redirect could swallow the refusal reason or leave
    sys.stdout pointed at a discarded buffer."""

    def test_concurrent_printing_threads_keep_the_real_stdout_and_the_reason(self):
        import io
        import threading
        import time
        real = io.StringIO()
        saved = sys.stdout
        sys.stdout = real
        try:
            stop = threading.Event()
            noise_lines = []

            def chatter():
                n = 0
                while not stop.is_set():
                    line = f"orchestration tick {n}"
                    noise_lines.append(line)
                    print(line)
                    n += 1
                    time.sleep(0.001)

            def gate(_command):
                print("SHIP_FEATURE_BLOCKED: required work items have no criteria: issue-94")
                time.sleep(0.05)
                print("- detail line")
                return 1

            noisy = threading.Thread(target=chatter, daemon=True)
            noisy.start()
            results = []
            gates = [threading.Thread(target=lambda: results.append(dashboard._run_gate(gate, None))) for _ in range(4)]
            for thread in gates:
                thread.start()
            for thread in gates:
                thread.join(timeout=5)
            stop.set()
            noisy.join(timeout=5)
            # Every gate call saw its own reason, never a tick and never another call's lines.
            self.assertEqual(results, [(1, "required work items have no criteria: issue-94; detail line")] * 4)
            output = real.getvalue()
            self.assertNotIn("SHIP_FEATURE_BLOCKED", output)
            self.assertNotIn("detail line", output)
            for line in noise_lines:
                self.assertIn(line, output)
            # The proxy stays installed and keeps forwarding, and a later
            # gate call on the main thread still captures.
            print("after the gates")
            self.assertIn("after the gates", real.getvalue())
            self.assertEqual(dashboard._run_gate(gate, None), (1, "required work items have no criteria: issue-94; detail line"))
            self.assertEqual(dashboard._run_gate(lambda _c: print("DESIGN_APPROVAL_RECORDED") or 0, None), (0, None))
            # A success line still reaches the server log.
            self.assertIn("DESIGN_APPROVAL_RECORDED", real.getvalue())
        finally:
            sys.stdout = saved
