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
import ast
import contextlib
import http.client
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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


#: a human's choices in a printed command, token by token
TOKEN_PLACEHOLDERS = {"ACTOR": "Pilot", "PILOT": "Pilot", "REVIEWER": "reviewer-2", "ARCHITECT": "architect-1",
                      "TEXT": "reevaluated", "RUN_ID": "vr-1", "N": "7", "FEATURE": "feature", "REF": "HEAD",
                      "...": "reevaluated", "<pilot>": "Pilot", "<why>": "reevaluated", "<you>": "Pilot",
                      "<run_id>": "vr-1", "OWNER": "owner-1", "ENV": "production", "PATH": "/",
                      "FILE": "bin/x.py", "FUNCTION": "f", "DIMENSION=TEXT": "input=empty"}
#: where a command printed inside a sentence ends
COMMAND_END = re.compile(r"[;,`)\n]| \(| or | before | then | to | and | if | on the |(?<!\.)\.(?!\.)(?=\s|$)|$")
PRINTED = re.compile(r"handsoff supervisor --root ")
ROOT_TEMPLATE = "handsoff supervisor --root {root} "


def fill_placeholders(command: str) -> str:
    return shlex.join(TOKEN_PLACEHOLDERS.get(token, token) for token in shlex.split(command))


def printed_commands(text: str) -> list[str]:
    """Every `handsoff supervisor --root ...` command in printed text."""
    commands = []
    for match in PRINTED.finditer(text):
        rest = text[match.start():]
        commands.append(rest[:COMMAND_END.search(rest, len(match.group(0))).start()].strip())
    return commands


def _render(node, cmd_is_supervisor):
    """A string expression's printed text: formatted values as a stand-in
    value, and `cmd` (the clears-with builder's prefix) as the script."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "cmd" and cmd_is_supervisor:
        return "handsoff_supervisor.py "
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            elif isinstance(value.value, ast.Name) and value.value.id == "_SUPERVISOR_CMD":
                parts.append("handsoff_supervisor.py")
            elif isinstance(value.value, ast.Name) and value.value.id == "cmd" and cmd_is_supervisor:
                parts.append("handsoff_supervisor.py ")
            else:  # a value the engine fills; --kind takes one of its choices
                parts.append("manual" if parts and parts[-1].endswith("--kind ") else "7")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left, cmd_is_supervisor), _render(node.right, cmd_is_supervisor)
        return left + right if left is not None and right is not None else None
    return None


def printed_templates() -> list[tuple[str, str]]:
    """(verb, template) for every command the engine prints for a human:
    each `handsoff_supervisor.py <verb>` in a string of bin/handsoff*.py
    (gate messages, hints, the clears-with builder, whose `cmd` is the
    script prefix), and each `handsoff supervisor --root {root}` template.
    Docstrings are not printed and are skipped."""
    found = []
    script = re.compile(r"(?<![\w/.-])handsoff_supervisor\.py (?=[a-z])")
    for path in sorted(BIN.glob("handsoff*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = {id(node.body[0].value) for node in ast.walk(tree)
                   if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                   and node.body and isinstance(node.body[0], ast.Expr)
                   and isinstance(node.body[0].value, ast.Constant)}
        # only whole expressions: a part of an f-string or a concatenation
        # is a fragment of the printed text, read with its whole
        skipped |= {id(child) for node in ast.walk(tree)
                    if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.BinOp)
                                                           and isinstance(node.op, ast.Add))
                    for child in ast.iter_child_nodes(node)}
        scopes = [tree] + [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        seen = set()
        for scope in scopes:
            cmd_is_supervisor = scope is not tree and any(
                isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "cmd" for t in node.targets)
                and "_SUPERVISOR_CMD" in ast.dump(node.value) for node in ast.walk(scope))
            for node in ast.walk(scope):
                if id(node) in skipped or (id(node), cmd_is_supervisor) in seen:
                    continue
                seen.add((id(node), cmd_is_supervisor))
                text = _render(node, cmd_is_supervisor)
                if not text:
                    continue
                starts = [ROOT_TEMPLATE + text[match.end():] for match in script.finditer(text)]
                starts += [text[match.start():] for match in re.finditer(re.escape(ROOT_TEMPLATE), text)]
                found += [rest[:COMMAND_END.search(rest, len(ROOT_TEMPLATE)).start()].strip() for rest in starts]
    unique = sorted(set(found))
    return [(command.split()[4], command) for command in unique]


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


class StaleEvidenceAtTheGate(DeploymentGateCase):
    """Review attempt 1: after evidence drifted at Phase 7 the page still
    offered Authorize deployment, and deployment-gate --approve refused it
    (DEPLOYMENT_BLOCKED). The offer now reads the gate's own predicate."""

    def drift(self):
        # repository content, outside the runtime manifest, so the
        # installed-CLI path below still passes its integrity check
        (self.tmp / "feature_change.py").write_text("# evidence drift #466\n")

    def assert_names_runnable_verify(self, commands):
        parsed = []
        for command in commands:
            try:
                parsed.append(parse_stable(fill_placeholders(command)))
            except SystemExit:
                self.fail(f"not runnable as printed: {command}")
        self.assertTrue(parsed, commands)
        for item in parsed:
            self.assertEqual(item.root, str(self.tmp.resolve()))
        self.assertIn("verify", [item.command for item in parsed], commands)

    def assert_gate_refuses(self):
        gate = run(["deployment-gate", "--approve", "--by", "Pilot"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("DEPLOYMENT_BLOCKED", gate.stdout)
        self.assertIn("clears with:", gate.stdout)
        self.assertIsNone(self.read_status().get("deployment_approved"))

    def test_optional_approval_with_stale_evidence_offers_no_authorize_and_names_the_command(self):
        self.phase7(approval_required=False, host="Sentinel")
        self.assertIn("deployment_approve", kinds(self.snapshot()))
        self.drift()
        snapshot = self.snapshot()
        request = snapshot["input_required"]
        self.assertNotIn("deployment_approve", kinds(snapshot))
        self.assertFalse(request["deployment_approval_offered"])
        refusals = request["deployment_approval_refusals"]
        self.assertTrue(refusals)
        self.assertNotIn("handsoff_supervisor.py", json.dumps(refusals))
        self.assert_names_runnable_verify([item["command"] for item in refusals if item["command"]])
        self.assert_gate_refuses()

    def test_required_approval_with_stale_evidence_names_the_command_and_offers_no_authorize(self):
        self.phase7(approval_required=True)
        self.drift()
        snapshot = self.snapshot()
        request = snapshot["input_required"]
        self.assertTrue(request["required"])
        self.assertIn(request["kind"], {"evidence_drift", "deployment_blocked"})
        self.assertNotIn("deployment_approve", kinds(snapshot))
        self.assertNotIn("handsoff_supervisor.py", request["message"])
        self.assert_names_runnable_verify(printed_commands(request["message"]))
        self.assert_gate_refuses()

    def test_the_approval_endpoint_names_the_refusal_through_a_view_only_server(self):
        self.phase7(approval_required=False, host="Sentinel")
        self.drift()
        port = self.serve(view_only=True)
        code, body = self.post(port, "/api/deployment-approval", {})
        self.assertEqual(code, 409, body)
        self.assertIn("Deployment cannot be authorized yet", body["error"])
        self.assert_names_runnable_verify(printed_commands(body["error"]))
        self.assertIsNone(self.read_status().get("deployment_approved"))

    def test_the_gate_refusal_through_the_installed_cli_prints_runnable_commands(self):
        """End to end through the stable console's dispatch: every command
        in the refusal, the clears-with lines included, runs as printed."""
        self.phase7(approval_required=False, host="Sentinel")
        self.drift()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._dispatch_supervisor(["--root", str(self.tmp), "deployment-gate", "--approve",
                                             "--by", "Pilot"])
        text = out.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn("DEPLOYMENT_BLOCKED", text)
        self.assertNotIn("handsoff_supervisor.py", text)
        clears = [line.split("clears with: ", 1)[1] for line in text.splitlines() if "clears with: " in line]
        self.assertTrue(clears, text)
        self.assert_names_runnable_verify([command for line in clears for command in printed_commands(line)])
        self.assert_names_runnable_verify(printed_commands(text))


class ScratchRunFixture(unittest.TestCase):
    def test_the_fixture_script_builds_the_paused_phase7_run_the_page_needs(self):
        """REQ-003's scratch run: what the browser click-through starts from."""
        from tests.fixtures import make_paused_phase7_run as fixture
        parent = Path(tempfile.mkdtemp(prefix="handsoff-scratch-"))
        self.addCleanup(shutil.rmtree, parent, True)
        root = parent / "run"
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            fixture.build(root)
            status = json.loads((root / "handsoff-status.json").read_text())
            snapshot = dashboard.build_snapshot(root)
        self.assertEqual((status["phase_number"], status["status"]), (7, "ready_to_deploy"))
        self.assertIsNone(status.get("deployment_approved"))
        self.assertEqual(snapshot["input_required"]["kind"], "performance_pause")
        self.assertIn("performance_resume", kinds(snapshot))
        self.assertNotIn("deployment_approve", kinds(snapshot))
        self.assertEqual(parse_stable(snapshot["performance"]["pause"]["resume_command"]).root,
                         str(root.resolve()))


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

    def test_every_printed_command_in_the_engine_source_runs_through_the_stable_cli(self):
        """The closed set, read from the source rather than listed by hand:
        every `handsoff_supervisor.py <verb>` a gate message, hint or the
        clears-with builder prints, and every `{root}` template. Each, as
        human_command prints it, parses with the stable CLI and names the
        run's root."""
        root = str(self.tmp.resolve())
        found = printed_templates()
        verbs = {verb for verb, _template in found}
        self.assertGreaterEqual(len(found), 35, found)
        self.assertTrue({"verify", "record-symptom-resolved", "deployment-gate", "verify-live",
                         "design-review-authorize", "session-result-adopt", "performance-resume",
                         "human-pause-end", "workflow-check", "record-review"} <= verbs, verbs)
        for verb, template in found:
            with self.subTest(template=template):
                printed = supervisor.human_command(template, self.tmp)
                self.assertNotIn("handsoff_supervisor.py", printed)
                parsed = parse_stable(fill_placeholders(printed))
                self.assertEqual((parsed.command, parsed.root), (verb, root), printed)

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
