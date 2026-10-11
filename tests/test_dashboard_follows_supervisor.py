"""#466: the dashboard follows the supervisor at the deployment gate.

Found closing #444 (Sentinel lane, 2026-10-11). The run sat at Phase 7
`ready_to_deploy` (this project does not require deployment approval) and
Sentinel waited for a recorded approval. The view-only dashboard showed a
performance pause whose banner printed `handsoff performance-resume --by
<pilot> ...`, which the stable CLI rejects (no `supervisor` prefix, no
`--root`). After a CLI performance-resume the page still offered no
Authorize deployment, while `deployment-gate --approve` worked at once: the
dashboard offered the approval only when the project required it.

Now a performance pause is the request (Resume, nothing the gate refuses),
an unrecorded approval at Phase 7 is offered whenever the gate would take
it, and every command printed for a human parses with the stable CLI.
"""
import contextlib
import http.client
import io
import json
import shlex
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone

from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_cli as cli  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

#: the 2026-10-11 banner command, verbatim with its placeholders filled
OLD_RESUME_COMMAND = 'handsoff performance-resume --by Pilot --reason "reevaluated"'
PLACEHOLDERS = {"<pilot>": "Pilot", "<why>": "reevaluated", "ACTOR": "Pilot", "TEXT": "reevaluated"}


def kinds(snapshot):
    return [action["kind"] for action in snapshot["operator_actions"]]


def action(snapshot, kind):
    return next(item for item in snapshot["operator_actions"] if item["kind"] == kind)


def parse_stable(command: str):
    """Parse `command` exactly as the installed `handsoff` console routes
    it (handsoff_cli.main): `supervisor` passes its argv to the supervisor
    parser before argparse, anything else meets build_parser. Returns the
    supervisor namespace; raises SystemExit on a refusal."""
    for placeholder, value in PLACEHOLDERS.items():
        command = command.replace(placeholder, value)
    argv = shlex.split(command)
    if argv[0] != "handsoff":
        raise SystemExit(f"not a handsoff command: {command}")
    with contextlib.redirect_stderr(io.StringIO()):
        if argv[1:2] == ["supervisor"]:
            return supervisor.build_parser(prog="handsoff supervisor").parse_args(argv[2:])
        cli.build_parser().parse_args(argv[1:])
    raise SystemExit(f"not a supervisor command: {command}")


class DeploymentGateCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        super().tearDown()

    def phase7(self, *, approval_required, host=None):
        args = ["init", "Dashboard follows supervisor #466"] + (["--by", host] if host else [])
        initialized = run(args, cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        if not approval_required:
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace("deployment_requires_explicit_approval = true",
                                                     "deployment_requires_explicit_approval = false"))
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def pause(self):
        view = supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=121))
        self.assertEqual(view["state"], "paused_for_performance_review")

    def cli_resume(self):
        resumed = run(["performance-resume", "--by", "Pilot", "--reason", "reevaluated"], cwd=self.tmp)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("PERFORMANCE_RESUMED", resumed.stdout)

    def snapshot(self):
        return dashboard.build_snapshot(self.tmp)

    def serve(self, *, view_only):
        kwargs = ({"run_token": lib.new_dashboard_run_token(),
                   "root_sha256": lib.dashboard_root_sha256(self.tmp), "view_only": True}
                  if view_only else {})
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp, **kwargs)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._servers.append(server)
        return server.server_address[1]

    def post(self, port, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("POST", path, body=json.dumps(payload), headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body


class ReproductionOf20261011(DeploymentGateCase):
    def test_sentinel_run_at_ready_to_deploy_offers_authorize_deployment_after_resume(self):
        """The 2026-10-11 state: a Sentinel-host run at Phase 7
        ready_to_deploy, paused for performance review, then resumed through
        the supervisor CLI. Before #466 the snapshot offered no
        deployment_approve, though the gate took the approval at once."""
        self.phase7(approval_required=False, host="Sentinel")
        status = self.read_status()
        self.assertEqual((status["phase_number"], status["status"]), (7, "ready_to_deploy"))
        self.assertIsNone(status.get("deployment_approved"))
        self.pause()
        self.cli_resume()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["performance"]["state"], "active")
        self.assertIn("deployment_approve", kinds(snapshot),
                      "the supervisor accepts deployment-gate --approve here; the dashboard must offer it")
        self.assertTrue(snapshot["input_required"]["deployment_approval_offered"])
        # optional for this project: offered, never held on
        self.assertFalse(snapshot["input_required"]["required"])
        gate = run(["deployment-gate", "--approve", "--by", "Pilot"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertNotIn("deployment_approve", kinds(self.snapshot()))


class PausedSnapshot(DeploymentGateCase):
    def assert_paused_offers_resume_only(self):
        snapshot = self.snapshot()
        request = snapshot["input_required"]
        self.assertTrue(request["required"])
        self.assertEqual(request["kind"], "performance_pause")
        self.assertEqual(request["turn"], "pilot")
        self.assertFalse(request["deployment_approval_offered"])
        self.assertIn("performance_resume", kinds(snapshot))
        self.assertNotIn("deployment_approve", kinds(snapshot))
        self.assertNotIn("deployment_hold", kinds(snapshot))
        resume = action(snapshot, "performance_resume")
        self.assertEqual(resume["operation"], "performance-resume")
        self.assertTrue(resume["requires_reason"])
        command = snapshot["performance"]["pause"]["resume_command"]
        self.assertIn(command, request["message"])
        self.assertEqual(parse_stable(command).root, str(self.tmp.resolve()))

    def test_required_approval_while_paused_offers_resume_and_no_approval(self):
        self.phase7(approval_required=True)
        self.pause()
        self.assert_paused_offers_resume_only()

    def test_optional_approval_while_paused_offers_resume_and_no_approval(self):
        self.phase7(approval_required=False, host="Sentinel")
        self.pause()
        self.assert_paused_offers_resume_only()

    def test_after_resume_the_required_approval_is_offered(self):
        self.phase7(approval_required=True)
        self.pause()
        self.cli_resume()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["input_required"]["kind"], "deployment_approval")
        self.assertIn("deployment_approve", kinds(snapshot))
        self.assertNotIn("performance_resume", kinds(snapshot))

    def test_the_resume_action_is_bound_to_the_paused_episode(self):
        self.phase7(approval_required=True)
        self.pause()
        first = action(self.snapshot(), "performance_resume")["action_id"]
        self.cli_resume()
        self.pause()
        self.assertNotEqual(action(self.snapshot(), "performance_resume")["action_id"], first)


class ThroughTheServer(DeploymentGateCase):
    def resume_then_approve(self, *, view_only, approval_required):
        self.phase7(approval_required=approval_required, host="Sentinel")
        self.pause()
        port = self.serve(view_only=view_only)
        # nothing the gate would refuse: the approval endpoint names the pause
        code, body = self.post(port, "/api/deployment-approval", {})
        self.assertEqual(code, 409, body)
        self.assertIn("Paused for performance review", body["error"])
        resume = action(self.snapshot(), "performance_resume")
        code, body = self.post(port, "/api/operator-action", {"action_id": resume["action_id"], "reason": ""})
        self.assertEqual(code, 400, body)
        code, body = self.post(port, "/api/operator-action",
                               {"action_id": resume["action_id"], "reason": "reevaluated in Mission Control"})
        self.assertEqual(code, 200, body)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["performance"]["state"], "active")
        approve = action(snapshot, "deployment_approve")
        code, body = self.post(port, "/api/operator-action", {"action_id": approve["action_id"], "reason": None})
        self.assertEqual(code, 200, body)
        approved = self.read_status()["deployment_approved"]
        self.assertEqual(approved["by"], "Mission Control Pilot")

    def test_view_only_server_resumes_then_records_the_optional_approval(self):
        self.resume_then_approve(view_only=True, approval_required=False)

    def test_view_only_server_resumes_then_records_the_required_approval(self):
        self.resume_then_approve(view_only=True, approval_required=True)

    def test_owned_server_resumes_then_records_the_optional_approval(self):
        self.resume_then_approve(view_only=False, approval_required=False)

    def test_view_only_deployment_approval_endpoint_takes_the_optional_approval(self):
        self.phase7(approval_required=False, host="Sentinel")
        port = self.serve(view_only=True)
        code, body = self.post(port, "/api/deployment-approval", {})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["deployment_approved"]["by"], "Mission Control Pilot")


class PrintedCommandsRun(DeploymentGateCase):
    """REQ-002: the closed set of command templates the dashboard and the
    supervisor print for a human, each filled and parsed by the stable CLI."""

    def templates(self):
        root = self.tmp
        restart = lib.dashboard_restart_line(root, 4242, "127.0.0.1", 8765, True)
        triage = lib.run_triage(root, lib.load_config(root)) or {}
        return {
            "performance-resume": supervisor.human_command(supervisor.PERFORMANCE_RESUME_COMMAND, root),
            "human-pause-end": supervisor.human_command(dashboard.HUMAN_PAUSE_END_COMMAND, root),
            "dashboard": restart.split("restart it: ", 1)[1],
            **{option["command"].split()[4]: option["command"] for option in triage.get("options", [])},
        }

    def test_every_template_parses_with_the_stable_cli_and_names_the_root(self):
        self.phase7(approval_required=True)
        templates = self.templates()
        self.assertEqual(set(templates), {"performance-resume", "human-pause-end", "dashboard",
                                          "recover", "run-close", "run-reopen"})
        for verb, command in templates.items():
            with self.subTest(verb=verb):
                parsed = parse_stable(command)
                self.assertEqual(parsed.command, verb, command)
                self.assertEqual(parsed.root, str(self.tmp.resolve()), command)

    def test_templates_carry_the_supervisor_prefix_and_the_root_slot(self):
        for template in (supervisor.PERFORMANCE_RESUME_COMMAND, dashboard.HUMAN_PAUSE_END_COMMAND):
            with self.subTest(template=template):
                self.assertTrue(template.startswith("handsoff supervisor --root {root} "), template)
                for placeholder in ("<", ">"):
                    self.assertTrue(all(token in {"<pilot>", '"<why>"'} for token in template.split()
                                        if placeholder in token), template)

    def test_the_2026_10_11_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            parse_stable(OLD_RESUME_COMMAND)

    def test_the_commands_on_the_page_parse_too(self):
        self.phase7(approval_required=True)
        self.pause()
        snapshot = self.snapshot()
        resume = snapshot["performance"]["pause"]["resume_command"]
        self.assertEqual(parse_stable(resume).command, "performance-resume")
        self.cli_resume()
        paused = run(["human-pause-start", "--by", "Pilot", "--note", "checking"], cwd=self.tmp)
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        message = self.snapshot()["input_required"]["message"]
        command = message.split("End it with ", 1)[1].split(", then", 1)[0]
        parsed = parse_stable(command)
        self.assertEqual((parsed.command, parsed.root), ("human-pause-end", str(self.tmp.resolve())))


if __name__ == "__main__":
    import unittest
    unittest.main()
