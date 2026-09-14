#!/usr/bin/env python3
"""Regression tests for Project Handsoff itself.

Every test here maps to either a bug found by hand (run the tool the way
the README says to, on a fresh copy) or a guarantee this rewrite adds.
Stdlib unittest only, no pytest dependency, so these run anywhere Python 3
is available, matching the framework's own "copy a few files into a
project" ethos.

Run: python3 tests/test_handsoff_supervisor.py -v
"""
import hashlib
import io
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import re
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

def set_fixture_check_commands(path, commands):
    """Give copied fixtures their own checks without inheriting dogfood checks."""
    text, count = re.subn(
        r"^commands\s*=\s*\[.*\]$", f"commands = {json.dumps(commands)}",
        path.read_text(), count=1, flags=re.MULTILINE,
    )
    if count != 1:
        raise AssertionError("could not locate [checks].commands in fixture")
    path.write_text(text)


def normalize_fixture_config(path):
    """Detach test projects from mutable dogfood/runtime configuration."""
    replacements = {
        ("agents", role): f'{role} = "auto"'
        for role in ("architect", "supervisor", "implementer", "reviewer")
    }
    replacements.update({
        ("models", role): f'{role} = "default"'
        for role in ("architect", "supervisor", "implementer", "reviewer")
    })
    replacements.update({
        ("fallback_policy", "max_failovers_per_role"): "max_failovers_per_role = 2",
        **{("fallback_policy", role): f"{role} = []"
           for role in ("architect", "supervisor", "implementer", "reviewer")},
        ("checks", "commands"): "commands = []",
        ("checks", "live_commands"): "live_commands = []",
    })
    # The dogfood repo gates its own full suites behind [[regressions]]
    # (#28). A fixture project must not inherit those groups: the fixture
    # checks are `true`, which the footprint gate reads as broad, so an
    # inherited group would refuse every fixture verify. Both tables are
    # dropped; tests of the gate itself write their own config.
    dropped_sections = {"regression_gate", "regressions"}
    section = None
    normalized = []
    # A replaced key whose value is a multi-line array (the dogfood
    # `commands = [` list spans several lines) must also drop the
    # continuation lines up to the closing bracket, or the fixture toml
    # is left with a dangling array body and refuses to parse. The array
    # ends on the line that starts with the closing bracket, not on any
    # line that merely contains one: a dogfood command string may itself
    # carry a "]" (a Python one-liner indexing a dict, say).
    in_replaced_array = False
    for line in path.read_text().splitlines(keepends=True):
        if in_replaced_array:
            if line.strip().startswith("]"):
                in_replaced_array = False
            continue
        section_match = re.match(r"^\[\[?([^]]+)\]\]?\s*$", line.strip())
        if section_match:
            section = section_match.group(1)
        if section in dropped_sections:
            continue
        key_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        replacement = replacements.get((section, key_match.group(1))) if key_match else None
        if replacement is not None:
            value = line.split("=", 1)[1]
            in_replaced_array = value.count("[") > value.count("]")
        normalized.append(f"{replacement}\n" if replacement is not None else line)
    path.write_text("".join(normalized))


def setUpModule():
    """Keep completion archives inside a temporary test-only directory."""
    # Any test that lands Phase 8 with status complete now triggers a real
    # archive write (see handsoff_lib.archive_run). Sandboxed here at module
    # scope, not just in TestRunArchive, so a test class that reaches
    # completion for an unrelated reason (there are several) can never write
    # into the real ~/Documents/Handsoff-Archive on a developer's machine.
    global _MODULE_ARCHIVE_DIR
    _MODULE_ARCHIVE_DIR = tempfile.mkdtemp(prefix="handsoff-archive-module-")
    os.environ["HANDSOFF_ARCHIVE_DIR"] = _MODULE_ARCHIVE_DIR


def tearDownModule():
    os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
    if _MODULE_ARCHIVE_DIR:
        shutil.rmtree(_MODULE_ARCHIVE_DIR, ignore_errors=True)


_MODULE_ARCHIVE_DIR = None


def run(args, cwd):
    return subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), *args],
                          cwd=cwd, capture_output=True, text=True, timeout=30)


def approve_design_review(cwd, architect="test-architect", reviewer="test-design-reviewer"):
    return run(["record-design-review", "--by", reviewer, "--architect", architect,
                "--approve", "--summary", "Test-fixture independent design review"], cwd=cwd)


class HandsoffTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-test-"))
        for name in ("handsoff.toml",):
            shutil.copy(ROOT / name, self.tmp / name)
        normalize_fixture_config(self.tmp / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", self.tmp / "schemas")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self, feature="Test feature"):
        r = run(["init", feature], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def read_status(self):
        return json.loads((self.tmp / "handsoff-status.json").read_text())

    def read_acceptance(self):
        return json.loads((self.tmp / "handsoff-acceptance.json").read_text())

    def write_acceptance(self, acceptance):
        (self.tmp / "handsoff-acceptance.json").write_text(json.dumps(acceptance, indent=2))

    def set_criterion_state(self, state, resolved):
        if state == "passing":
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            criterion = run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
            self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
            result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_id = json.loads(result.stdout)["criteria"]["REQ-001"]["run_id"]
            if resolved:
                symptom = run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], cwd=self.tmp)
                self.assertEqual(symptom.returncode, 0, symptom.stdout + symptom.stderr)
        else:
            # Also give it a non-placeholder requirement (state alone
            # would otherwise leave it exactly matching init's default
            # text, which design-approve refuses -- see AR-009/Architect
            # gate); this changes only fixture text, not the state/
            # evidence behavior these tests actually exercise.
            changed = run(["criterion-update", "REQ-001", "--state", state,
                          "--requirement", "A test-fixture criterion under evaluation"], cwd=self.tmp)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)

    def advance_to(self, phase, **extra):
        """Step one phase at a time up to `phase`, as the one-step rule requires."""
        current = self.read_status()["phase_number"]
        last = None
        reviewed_by = extra.pop("reviewed_by", None)
        for n in range(current + 1, phase + 1):
            if n == 3 and self.read_status().get("requires_design_review") \
                    and not self.read_status().get("design_review"):
                design_review = approve_design_review(self.tmp)
                if design_review.returncode:
                    return design_review
            if n == 3 and self.read_status().get("requires_design_approval") and not self.read_status().get("design_approved"):
                # The Architect gate: every run init flags going forward
                # needs a recorded, non-self human design approval before
                # Phase 3+. Fixture identities here are unrelated to
                # implemented_by/reviewed_by, which the caller controls.
                approval = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                               "--summary", "Test-fixture design approval"], cwd=self.tmp)
                if approval.returncode:
                    return approval
            if n == 6 and reviewed_by:
                review = run(["record-review", "--by", str(reviewed_by)], cwd=self.tmp)
                if review.returncode:
                    return review
            args = ["advance", str(n), str(n * 10)]
            for k, v in extra.items():
                args += [f"--{k.replace('_', '-')}", str(v)]
            last = run(args, cwd=self.tmp)
        return last


class TestEveMissionControl(HandsoffTestCase):
    """The dashboard speaks like a calm tactical computer without hiding
    real delivery facts, and receives workflow changes without waiting for
    a browser polling interval."""

    def _dashboard(self):
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dashboard
        return dashboard

    def test_static_interface_uses_tactical_vocabulary(self):
        html = (ROOT / "dashboard" / "index.html").read_text()
        for phrase in (
            "E.V.E. MISSION CONTROL",
            "ACTIVE MISSION OBJECTIVE",
            "MISSION TRAJECTORY",
            "MISSION OBJECTIVES",
            "FLIGHT LOG",
            "INCOMING TRANSMISSIONS",
            "SYSTEM DIAGNOSTICS",
            "Welcome back, Pilot",
        ):
            self.assertIn(phrase, html)

    def test_supervisor_briefing_is_calm_concise_and_pilot_focused(self):
        dashboard = self._dashboard()
        base = {"phase_number": 4, "phase": "Implement", "progress": 40,
                "status": "in_progress", "requirement_coverage": {}}
        clear_input = {"required": False, "kind": None, "message": None}
        scenarios = [
            dashboard._supervisor_briefing(base, [], [], [], None, None, None, clear_input),
            dashboard._supervisor_briefing(base, [], ["Gate failed"], [], None, None, None, clear_input),
            dashboard._supervisor_briefing(base, [], [], [], "No update for 12 minutes.", None, None, clear_input),
            dashboard._supervisor_briefing(
                {**base, "phase_number": 8, "phase": "Complete", "progress": 100, "status": "complete",
                 "requirement_coverage": {"original_symptom_resolved": True}},
                [], [], [], None, None, None, clear_input),
        ]
        for briefing in scenarios:
            self.assertIn("Pilot", briefing["headline"])
            self.assertLess(len(briefing["headline"]), 100)
            self.assertLess(len(briefing["summary"]), 260)
            self.assertNotIn("!!!", briefing["headline"] + briefing["summary"])

    def test_approved_deployment_immediately_reads_ready_to_ship(self):
        dashboard = self._dashboard()
        self.init("Immediate deployment authorization display")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "Mission Control Pilot"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["status"]["status"], "ready_to_deploy")
        self.assertEqual(snapshot["status"]["phase"], "Deployment authorized · ready to ship")
        self.assertEqual(snapshot["phases"][6]["name"], "Deployment authorized · ready to ship")
        current_display = json.dumps({
            "status": snapshot["status"],
            "active_phase": snapshot["phases"][6],
            "supervisor": snapshot["supervisor"],
        }).lower()
        self.assertNotIn("awaiting deployment approval", current_display)

    def test_alerts_and_empty_state_remain_actionable(self):
        dashboard = self._dashboard()
        exact_action = "Run the targeted verification for EVE-003."
        request = dashboard._input_request(
            {"status": "blocked", "phase_number": 4, "next_action": exact_action},
            {"deployment_requires_explicit_approval": True},
        )
        self.assertTrue(request["required"])
        self.assertEqual(request["message"], exact_action)
        briefing = dashboard._supervisor_briefing(
            {"phase_number": 4, "phase": "Implement", "progress": 40, "status": "blocked",
             "requirement_coverage": {}}, [], [], [], None, None, None, request)
        self.assertIn("Pilot", briefing["headline"])
        self.assertEqual(briefing["summary"], exact_action)

        deployment = dashboard._input_request(
            {"status": "in_progress", "phase_number": 7, "next_action": "Review deployment."},
            {"deployment_requires_explicit_approval": True},
        )
        self.assertIn("Pilot", deployment["message"])
        self.assertIn("deployment approval", deployment["message"])
        design = dashboard._input_request(
            {"status": "in_progress", "phase_number": 2, "requires_design_approval": True,
             "design_approved": None, "design_review": {"decision": "approved"},
             "next_action": "Background text no longer mentions authorization."},
            {"deployment_requires_explicit_approval": True},
        )
        self.assertTrue(design["required"])
        self.assertEqual(design["kind"], "design_approval")

        html = (ROOT / "dashboard" / "index.html").read_text()
        script = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn("PILOT AUTHORIZATION REQUIRED", html)
        self.assertIn('id="design-approve"', html)
        self.assertIn('id="deployment-approve"', html)
        self.assertIn('fetch("/api/design-approval"', script)
        self.assertIn('fetch("/api/deployment-approval"', script)
        self.assertIn('signature !== state.alertSignature', script)
        self.assertIn("python3 bin/handsoff_supervisor.py init", html)

        self.init("Dashboard design authorization")
        authored = run([
            "criterion-update", "REQ-001", "--requirement",
            "Dashboard approval fixture has a real acceptance criterion",
        ], cwd=self.tmp)
        self.assertEqual(authored.returncode, 0, authored.stdout + authored.stderr)
        phase_two = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase_two.returncode, 0, phase_two.stdout + phase_two.stderr)
        reviewed = approve_design_review(self.tmp, architect="arch-ui", reviewer="reviewer-ui")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        awaiting = dashboard.build_snapshot(self.tmp)
        self.assertEqual(awaiting["input_required"]["kind"], "design_approval")

        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            connection = http.client.HTTPConnection(host, port, timeout=3)
            connection.request(
                "POST", "/api/design-approval", body="{}",
                headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"},
            )
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertEqual(response.status, 200, body)
            approved = self.read_status()["design_approved"]
            self.assertEqual(approved["by"], "Mission Control Pilot")
            self.assertEqual(approved["architect"], "arch-ui")
            self.assertEqual(self.read_status()["status"], "in_progress")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            with mock.patch.object(
                dashboard, "build_snapshot",
                return_value={"input_required": {"kind": "deployment_approval"}},
            ), mock.patch.object(dashboard.supervisor, "cmd_deployment_gate", return_value=0) as approve:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/api/deployment-approval", body="{}",
                    headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"},
                )
                response = connection.getresponse()
                body = response.read()
                connection.close()
                self.assertEqual(response.status, 200, body)
                command = approve.call_args.args[0]
                self.assertTrue(command.approve)
                self.assertEqual(command.by, "Mission Control Pilot")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_technical_facts_and_accessibility_are_preserved(self):
        dashboard = self._dashboard()
        self.init("Exact feature name: E.V.E. telemetry")
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["project"]["feature"], "Exact feature name: E.V.E. telemetry")
        self.assertEqual(snapshot["status"]["phase_number"], 1)
        self.assertEqual(snapshot["acceptance"]["criteria"][0]["id"], "REQ-001")
        self.assertIn("updated_at", snapshot["status"])

        html = (ROOT / "dashboard" / "index.html").read_text()
        self.assertIn('aria-live="polite"', html)
        self.assertIn('role="alert"', html)
        self.assertIn('data-role="architect"', html)
        self.assertIn('data-role="reviewer"', html)

    def test_dashboard_refreshes_immediately_from_server_events(self):
        dashboard = self._dashboard()
        self.init("Real-time telemetry fixture")
        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        stream = http.client.HTTPConnection(host, port, timeout=3)
        response = None
        try:
            stream.request("GET", "/api/events")
            response = stream.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            while response.readline() not in (b"\n", b"\r\n", b""):
                pass

            started = time.monotonic()
            heartbeat = run(["heartbeat", "--by", "telemetry-test", "--note", "state changed"], cwd=self.tmp)
            self.assertEqual(heartbeat.returncode, 0, heartbeat.stdout + heartbeat.stderr)
            event = ""
            for _ in range(12):
                line = response.readline().decode("utf-8")
                if line.startswith("event:"):
                    event = line.strip()
                if not line.strip() and event == "event: invalidate":
                    break
            self.assertEqual(event, "event: invalidate")
            self.assertLess(time.monotonic() - started, 2.0)

            snapshot_conn = http.client.HTTPConnection(host, port, timeout=3)
            snapshot_conn.request("GET", "/api/dashboard")
            snapshot_response = snapshot_conn.getresponse()
            snapshot = json.loads(snapshot_response.read())
            snapshot_conn.close()
            self.assertIsNotNone(snapshot["status"]["last_heartbeat_at"])
            self.assertEqual(snapshot["events"][0]["by"], "telemetry-test")

            script = (ROOT / "dashboard" / "app.js").read_text()
            self.assertIn('new EventSource("/api/events")', script)
            self.assertIn('document.addEventListener("visibilitychange"', script)
            self.assertIn('window.addEventListener("focus"', script)
            self.assertIn('window.addEventListener("pageshow"', script)
        finally:
            if response is not None:
                response.close()
            stream.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_agent_settings_gui_persists_role_assignments_safely(self):
        dashboard = self._dashboard()
        import handsoff_lib as lib

        html = (ROOT / "dashboard" / "index.html").read_text()
        script = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn('id="settings-toggle"', html)
        self.assertIn('id="settings-dialog"', html)
        for role in ("architect", "supervisor", "implementer", "reviewer"):
            self.assertIn(f'<select id="agent-{role}"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('fetch("/api/settings/agents"', script)

        config_path = self.tmp / "handsoff.toml"
        original = config_path.read_text()
        customized = original.replace('architect = "auto"\n', '# architect intentionally omitted\n')
        customized += '\n[adapter.extra]\nnote = "preserve this byte-for-byte"\n'
        config_path.write_text(customized)

        effective = lib.update_agent_config(self.tmp, {
            "architect": "codex", "implementer": "claude", "reviewer": "codex",
        })
        updated = config_path.read_text()
        self.assertEqual(effective, {"architect": "codex", "implementer": "claude", "reviewer": "codex"})
        self.assertIn('# architect intentionally omitted\n', updated)
        self.assertIn('architect = "codex"\n', updated)
        self.assertIn('supervisor = "auto"', updated)
        self.assertIn('[adapter.extra]\nnote = "preserve this byte-for-byte"', updated)
        self.assertEqual(lib.load_config(self.tmp)["agents"]["implementer"], "claude")

        stable = config_path.read_text()
        with self.assertRaises(lib.HandsoffError):
            lib.update_agent_config(self.tmp, {
                "architect": "remote-agent", "implementer": "claude", "reviewer": "codex",
            })
        self.assertEqual(config_path.read_text(), stable)
        with mock.patch.object(lib.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                lib.update_agent_config(self.tmp, {
                    "architect": "claude", "implementer": "codex", "reviewer": "claude",
                })
        self.assertEqual(config_path.read_text(), stable)

        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]

        def post(raw, *, origin=True):
            connection = http.client.HTTPConnection(host, port, timeout=3)
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = f"http://{host}:{port}"
            connection.request("POST", "/api/settings/agents", body=raw, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.status
            connection.close()
            return status, body

        try:
            payload = json.dumps({"architect": "claude", "implementer": "codex", "reviewer": "claude"})
            status, body = post(payload)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["agents"], {
                "architect": "claude", "implementer": "codex", "reviewer": "claude",
            })
            self.assertEqual(lib.load_config(self.tmp)["agents"]["architect"], "claude")

            unchanged = config_path.read_text()
            forbidden, _ = post(payload, origin=False)
            self.assertEqual(forbidden, 403)
            duplicate, _ = post(
                '{"architect":"codex","architect":"claude","implementer":"codex","reviewer":"claude"}'
            )
            self.assertEqual(duplicate, 400)
            self.assertEqual(config_path.read_text(), unchanged)

            snapshot = dashboard.build_snapshot(self.tmp)
            self.assertEqual(snapshot["settings"]["agents"]["reviewer"], "claude")
            self.assertEqual(snapshot["settings"]["allowed_adapters"], ["auto", "codex", "claude"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_agent_settings_include_role_specific_models_and_availability(self):
        dashboard = self._dashboard()
        import handsoff_lib as lib

        html = (ROOT / "dashboard" / "index.html").read_text()
        script = (ROOT / "dashboard" / "app.js").read_text()
        # THIS RUN/model-text formatting and actorForRole's actor lookup live
        # in the shared, DOM-free logic module (dashboard/lib/dashboard-logic.js)
        # since issue #25 extracted it there for node:test coverage.
        logic_script = (ROOT / "dashboard" / "lib" / "dashboard-logic.js").read_text()
        styles = (ROOT / "dashboard" / "styles.css").read_text()
        for role in ("architect", "supervisor", "implementer", "reviewer"):
            self.assertIn(f'id="agent-{role}-model"', html)
            self.assertIn(f'id="agent-{role}-run"', html)
            self.assertIn(f'id="agent-{role}-effective"', html)
        self.assertIn("Executable detection does not prove", html)
        self.assertIn("default</code> sends no model flag", html)
        self.assertIn("exact model ID to pin it", html)
        self.assertIn("JSON.stringify(payload)", script)
        self.assertIn("THIS RUN:", logic_script)
        self.assertIn("NEXT LAUNCH:", script)
        self.assertIn("actors?.implemented_by", logic_script)
        self.assertIn("snapshot.runtime?.current_sessions", script)
        self.assertIn("provider, model, and session not recorded", logic_script)
        self.assertIn("exact model not exposed", logic_script)
        self.assertIn("width: min(900px", styles)
        self.assertIn("minmax(280px", styles)

        self.assertEqual(lib.load_config(self.tmp)["models"], {
            "architect": "default", "supervisor": "default",
            "implementer": "default", "reviewer": "default",
        })
        config_path = self.tmp / "handsoff.toml"
        config_path.write_text(config_path.read_text() + '\n[adapter.extra]\nnote = "preserve-model-update"\n')
        profiles = {
            "architect": {"adapter": "codex", "model": "gpt-5.4"},
            "supervisor": {"adapter": "claude", "model": "opus"},
            "implementer": {"adapter": "claude", "model": "sonnet"},
            "reviewer": {"adapter": "codex", "model": "custom/reviewer-v2"},
        }
        self.assertEqual(lib.update_agent_config(self.tmp, profiles), profiles)
        updated = config_path.read_text()
        self.assertIn('[models]\n', updated)
        self.assertIn('architect = "gpt-5.4"\n', updated)
        self.assertIn('[adapter.extra]\nnote = "preserve-model-update"', updated)
        self.assertEqual(lib.agent_profiles(lib.load_config(self.tmp)), profiles)

        for bad in ("", " model", "model ", "-override", "bad\nmodel", "x" * 129):
            invalid = {role: dict(profile) for role, profile in profiles.items()}
            invalid["reviewer"]["model"] = bad
            with self.assertRaises(lib.HandsoffError):
                lib.update_agent_config(self.tmp, invalid)

        legacy = {"architect": "claude", "implementer": "codex", "reviewer": "claude"}
        self.assertEqual(lib.update_agent_config(self.tmp, legacy), legacy)
        self.assertEqual(lib.load_config(self.tmp)["models"], {
            "architect": "default", "supervisor": "opus",
            "implementer": "default", "reviewer": "default",
        })
        with mock.patch.object(lib.shutil, "which", side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            view = dashboard._settings_view(lib.load_config(self.tmp))
        self.assertTrue(view["availability"]["codex"]["available"])
        self.assertFalse(view["availability"]["claude"]["available"])
        # #39 added a per-role `source` on effective profiles; the adapter and
        # model themselves must still match the configured profiles exactly.
        self.assertEqual(
            {role: {"adapter": p["adapter"], "model": p["model"]} for role, p in view["effective_profiles"].items()},
            lib.agent_profiles(lib.load_config(self.tmp)),
        )
        self.assertIn("does not prove", view["availability_scope"])

        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            connection = http.client.HTTPConnection(host, port, timeout=3)
            connection.request(
                "POST", "/api/settings/agents", body=json.dumps(profiles),
                headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"},
            )
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertEqual(response.status, 200, body)
            payload = json.loads(body)
            self.assertEqual(payload["profiles"], profiles)
            self.assertIn("availability", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_provider_status_detects_cli_and_credential_providers_without_reading_values(self):
        """AG2: provider discovery must distinguish detected / requires-setup /
        unavailable, and must never read a credential's actual value -- only
        whether its environment variable name is present."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib

        def fake_which(name):
            return "/usr/local/bin/codex" if name == "codex" else None

        with mock.patch.object(lib.shutil, "which", side_effect=fake_which), \
             mock.patch.dict(os.environ, {"XAI_API_KEY": "sk-should-never-be-read"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            status = lib.provider_status()

        self.assertEqual(status["codex"]["state"], "detected")
        self.assertIsNotNone(status["codex"]["executable"])
        self.assertEqual(status["claude"]["state"], "unavailable")
        self.assertIsNone(status["claude"]["executable"])
        self.assertEqual(status["ollama"]["state"], "unavailable")
        self.assertEqual(status["ollama"]["models"], [])
        self.assertEqual(status["grok"]["state"], "detected")
        self.assertEqual(status["openai_compatible"]["state"], "requires_setup")
        self.assertIn("endpoint_env_var", status["openai_compatible"])

        # The credential's value must never appear anywhere in the result.
        serialized = json.dumps(status)
        self.assertNotIn("sk-should-never-be-read", serialized)
        self.assertIn("credential_env_var", status["grok"])
        self.assertNotIn("value", status["grok"])

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            cleared = lib.provider_status()
        self.assertEqual(cleared["grok"]["state"], "requires_setup")
        self.assertEqual(cleared["openai_compatible"]["state"], "requires_setup")

        # A self-hosted OpenAI-compatible endpoint commonly has no real
        # credential -- a configured base URL alone must count as detected.
        with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:1234/v1"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            endpoint_only = lib.provider_status()
        self.assertEqual(endpoint_only["openai_compatible"]["state"], "detected")

    def test_ollama_provider_lists_locally_installed_models(self):
        """AG2 fix: Ollama detection must surface actual local model names,
        not just executable presence, and must degrade gracefully (empty
        list, no error) when the local server isn't running."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        import io

        def fake_which(name):
            return "/usr/local/bin/ollama" if name == "ollama" else None

        fake_response = io.BytesIO(json.dumps({
            "models": [{"name": "llama3:8b"}, {"name": "qwen3-coder:30b"}],
        }).encode("utf-8"))

        class _FakeContext:
            def __enter__(self):
                return fake_response

            def __exit__(self, *exc_info):
                return False

        with mock.patch.object(lib.shutil, "which", side_effect=fake_which), \
             mock.patch.object(lib.urllib.request, "urlopen", return_value=_FakeContext()):
            status = lib.provider_status()
        self.assertEqual(status["ollama"]["state"], "detected")
        self.assertEqual(status["ollama"]["models"], ["llama3:8b", "qwen3-coder:30b"])

        with mock.patch.object(lib.shutil, "which", side_effect=fake_which), \
             mock.patch.object(lib.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            unreachable = lib.provider_status()
        self.assertEqual(unreachable["ollama"]["state"], "detected")
        self.assertEqual(unreachable["ollama"]["models"], [])

    def test_agent_settings_view_and_ui_surface_provider_detection(self):
        """AG2: the Agent Settings config surface lists detected providers
        alongside the existing role/adapter/model behavior, unchanged."""
        dashboard = self._dashboard()
        import handsoff_lib as lib

        with mock.patch.object(lib.shutil, "which", return_value=None):
            view = dashboard._settings_view(lib.load_config(self.tmp))
        self.assertIn("providers", view)
        for provider_id in ("codex", "claude", "ollama", "grok", "openai_compatible"):
            self.assertIn(provider_id, view["providers"])
            self.assertIn(view["providers"][provider_id]["state"], ("detected", "unavailable", "requires_setup"))
        self.assertIn("never reads, displays, or stores credential values", view["providers_scope"])

        # Existing role/model settings behavior is untouched by this addition.
        self.assertEqual(view["allowed_adapters"], list(lib.AGENT_SETTING_ADAPTERS))
        self.assertIn("profiles", view)

        html = (ROOT / "dashboard" / "index.html").read_text()
        script = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn('id="provider-status"', html)
        self.assertIn("DETECTED PROVIDERS", html)
        self.assertIn("function renderProviderStatus", script)
        self.assertIn("requires_setup", script)

    def test_agent_settings_note_capability_guidance_for_architect_and_reviewer(self):
        """AG6: the config surface must inform, not restrict -- it names
        Architect and Reviewer as the roles that benefit most from a
        stronger model, while every role stays freely selectable."""
        html = (ROOT / "dashboard" / "index.html").read_text()
        self.assertIn("CAPABILITY GUIDANCE", html)
        self.assertIn("Architect and Reviewer", html)
        self.assertIn("benefit most from a more capable model", html)
        self.assertIn("guidance only", html)

        import re
        for role in ("architect", "reviewer"):
            select_match = re.search(rf'<select id="agent-{role}"[^>]*>(.*?)</select>', html, re.DOTALL)
            self.assertIsNotNone(select_match, f"agent-{role} select not found")
            self.assertNotIn("disabled", select_match.group(0))
            options = re.findall(r'<option value="([^"]*)"', select_match.group(1))
            self.assertEqual(options, ["auto", "codex", "claude"])

            model_input_match = re.search(rf'<input id="agent-{role}-model"[^>]*>', html)
            self.assertIsNotNone(model_input_match, f"agent-{role}-model input not found")
            self.assertNotIn("disabled", model_input_match.group(0))
            self.assertNotIn("readonly", model_input_match.group(0))

        # No specific model is named as objectively "stronger" -- guidance
        # only, never an unverifiable claim baked into shipped UI copy.
        for named_model in ("gpt-5.4", "sonnet", "opus", "haiku", "claude-opus", "gpt-5"):
            self.assertNotIn(named_model, html.split("CAPABILITY GUIDANCE")[1].split("</p>")[0])

    def test_background_review_start_clears_stale_authorization_state(self):
        dashboard = self._dashboard()
        self.init("Instant review transition")
        changed = run(["criterion-update", "REQ-001", "--requirement", "Instant review state is visible"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        moved = run([
            "advance", "2", "20", "--status", "blocked", "--next-action",
            "Credentials are required for an unrelated service.",
        ], cwd=self.tmp)
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        wrong_hold = run([
            "background-wait-start", "--by", "supervisor", "--resume-after-authorization",
            "--note", "This must not clear an unrelated hold.",
        ], cwd=self.tmp)
        self.assertEqual(wrong_hold.returncode, 1)
        self.assertEqual(self.read_status()["status"], "blocked")
        tagged = run([
            "advance", "2", "20", "--status", "blocked", "--authorization-hold", "design_review",
            "--next-action", "Authorize the independent design reviewer.",
        ], cwd=self.tmp)
        self.assertEqual(tagged.returncode, 0, tagged.stdout + tagged.stderr)
        before = self.read_status()

        server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        stream = http.client.HTTPConnection(host, port, timeout=3)
        response = None
        try:
            stream.request("GET", "/api/events")
            response = stream.getresponse()
            while response.readline() not in (b"\n", b"\r\n", b""):
                pass
            started = time.monotonic()
            resumed = run([
                "background-wait-start", "--by", "supervisor", "--resume-after-authorization",
                "--note", "Independent design reviewer is analyzing the design.",
            ], cwd=self.tmp)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            event = ""
            for _ in range(12):
                line = response.readline().decode("utf-8")
                if line.startswith("event:"):
                    event = line.strip()
                if not line.strip() and event == "event: invalidate":
                    break
            self.assertEqual(event, "event: invalidate")
            self.assertLess(time.monotonic() - started, 2.0)

            after = self.read_status()
            self.assertEqual(after["status"], "in_progress")
            self.assertEqual(after["next_action"], "Independent design reviewer is analyzing the design.")
            self.assertGreater(after["updated_at"], before["updated_at"])
            self.assertIsNotNone(after["last_heartbeat_at"])
            snapshot = dashboard.build_snapshot(self.tmp)
            self.assertFalse(snapshot["input_required"]["required"])
            self.assertEqual(snapshot["actors"]["active_role"], "reviewer")
            self.assertEqual(snapshot["events"][0]["kind"], "background_wait_started")

            ended = run(["background-wait-end", "--by", "supervisor"], cwd=self.tmp)
            self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
            reviewed = approve_design_review(self.tmp, architect="arch", reviewer="design-reviewer")
            self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
            refused = run([
                "background-wait-start", "--by", "supervisor", "--resume-after-authorization",
                "--note", "This must not hide the design approval gate.",
            ], cwd=self.tmp)
            self.assertEqual(refused.returncode, 1)
            protected = dashboard.build_snapshot(self.tmp)
            self.assertEqual(protected["status"]["status"], "blocked")
            self.assertEqual(protected["input_required"]["kind"], "design_approval")
        finally:
            if response is not None:
                response.close()
            stream.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TestZeroConfigAgentDefaults(HandsoffTestCase):
    """AG3: fresh projects launch with deterministic, visible defaults."""

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")

    def _runtime(self):
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        return handsoff_agent

    def test_missing_role_selections_resolve_to_the_recommended_crew_and_auto_still_detects(self):
        import handsoff_lib as lib

        # #39: an omitted role key is the recommended crew, not "auto".
        config_path = self.tmp / "handsoff.toml"
        text = config_path.read_text()
        for role in lib.SELECTABLE_AGENT_ROLES:
            text = text.replace(f'{role} = "auto"\n', f'# {role} intentionally omitted\n', 1)
        config_path.write_text(text)
        cfg = lib.load_config(self.tmp)
        self.assertEqual(
            {role: profile["adapter"] for role, profile in lib.agent_profiles(cfg).items()},
            {role: profile["adapter"] for role, profile in lib.RECOMMENDED_CREW.items()},
        )

        # An explicit "auto" (the fixture default) still resolves to the
        # first installed adapter in documented order.
        shutil.copy(ROOT / "handsoff.toml", config_path)
        normalize_fixture_config(config_path)
        cfg = lib.load_config(self.tmp)
        self.assertTrue(all(profile["adapter"] == "auto" for profile in lib.agent_profiles(cfg).values()))
        with mock.patch.object(lib.shutil, "which",
                               side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            resolved = lib.resolved_agent_profiles(cfg, require_available=True)
        self.assertTrue(all(profile["adapter"] == "codex" for profile in resolved.values()))

    def test_auto_profile_builds_every_role_with_available_fallback(self):
        runtime = self._runtime()

        for role in ("architect", "supervisor", "implementer", "reviewer"):
            spec = runtime.build_launch_spec(
                self.tmp, role, "zero-config launch",
                which=lambda name: "/usr/local/bin/claude" if name == "claude" else None,
            )
            self.assertEqual(spec.adapter, "claude")
            self.assertEqual(spec.argv[0], "/usr/local/bin/claude")
            self.assertNotIn("--model", spec.argv)

    def test_explicit_role_override_wins_without_changing_other_auto_roles(self):
        runtime = self._runtime()
        import handsoff_lib as lib

        profiles = {
            role: {"adapter": "claude" if role == "architect" else "auto", "model": "default"}
            for role in lib.SELECTABLE_AGENT_ROLES
        }
        self.assertEqual(lib.update_agent_config(self.tmp, profiles), profiles)
        both_installed = lambda name: f"/usr/local/bin/{name}"
        architect = runtime.build_launch_spec(self.tmp, "architect", "explicit override", which=both_installed)
        implementer = runtime.build_launch_spec(self.tmp, "implementer", "automatic fallback", which=both_installed)
        self.assertEqual(architect.adapter, "claude")
        self.assertEqual(implementer.adapter, "codex")

    def test_dashboard_explains_default_and_missing_adapter_fails_clearly(self):
        import handsoff_dashboard as dashboard
        import handsoff_lib as lib

        with mock.patch.object(lib.shutil, "which",
                               side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            view = dashboard._settings_view(lib.load_config(self.tmp))
        self.assertEqual(view["default_adapter"], "codex")
        self.assertEqual(view["default_order"], ["codex", "claude"])
        self.assertEqual(view["allowed_adapters"], ["auto", "codex", "claude"])
        self.assertTrue(all(profile["adapter"] == "codex" for profile in view["effective_profiles"].values()))

        html = (ROOT / "dashboard" / "index.html").read_text()
        script = (ROOT / "dashboard" / "app.js").read_text()
        # Auto-detect labeling and the configure-me/auto fallback live in the
        # shared, DOM-free logic module (dashboard/lib/dashboard-logic.js)
        # since issue #25 extracted it there for node:test coverage; app.js
        # still owns effective_profiles handling.
        logic_script = (ROOT / "dashboard" / "lib" / "dashboard-logic.js").read_text()
        self.assertEqual(html.count('<option value="auto">Auto-detect</option>'), 4)
        self.assertIn("Auto-detect (currently", logic_script)
        self.assertIn("effective_profiles", script)
        self.assertIn('storedAdapter === "configure-me" ? "auto"', logic_script)

        # #39: the legacy "configure-me" placeholder is an untouched
        # default, so it takes the recommended reviewer profile (codex),
        # not the auto-detect path.
        legacy_path = self.tmp / "handsoff.toml"
        legacy_path.write_text(legacy_path.read_text().replace('reviewer = "auto"',
                                                               'reviewer = "configure-me"'))
        with mock.patch.object(lib.shutil, "which",
                               side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            legacy_view = dashboard._settings_view(lib.load_config(self.tmp))
        self.assertEqual(legacy_view["profiles"]["reviewer"]["adapter"], "codex")
        self.assertEqual(legacy_view["profile_sources"]["reviewer"]["adapter"], "recommended")
        self.assertEqual(legacy_view["effective_profiles"]["reviewer"]["adapter"], "codex")

        with self.assertRaisesRegex(lib.HandsoffError, "no supported agent adapter"):
            lib.resolved_agent_profiles(lib.load_config(self.tmp), which=lambda _name: None,
                                        require_available=True)


class TestRecommendedCrewDefaults(HandsoffTestCase):
    """#39: roles left unset in handsoff.toml take the recommended crew,
    say so everywhere they are reported, and never claim to have run
    when the adapter is not installed."""

    RECOMMENDED = {
        "architect": ("claude", "claude-opus-5"),
        "supervisor": ("claude", "claude-opus-5"),
        "implementer": ("claude", "claude-opus-5"),
        "reviewer": ("codex", "default"),
    }

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib

    def _write_toml(self, agents=None, models=None):
        """A handsoff.toml with only the sections given (None omits the table)."""
        lines = ["[checks]", "commands = []", "live_commands = []", ""]
        for table, values in (("agents", agents), ("models", models)):
            if values is None:
                continue
            lines.append(f"[{table}]")
            lines.extend(f"{role} = {json.dumps(value)}" for role, value in values.items())
            lines.append("")
        (self.tmp / "handsoff.toml").write_text("\n".join(lines))

    def _requested(self, cfg):
        return {role: (profile["adapter"], profile["model"])
                for role, profile in self.lib.agent_profiles(cfg).items()}

    @staticmethod
    def _both_installed(name):
        return f"/usr/local/bin/{name}"

    def test_recommended_crew_constant_matches_the_settled_profiles(self):
        self.assertEqual(
            {role: (p["adapter"], p["model"]) for role, p in self.lib.RECOMMENDED_CREW.items()},
            self.RECOMMENDED,
        )
        self.assertEqual(self.lib.DEFAULT_CONFIG["agents"],
                         {role: p["adapter"] for role, p in self.lib.RECOMMENDED_CREW.items()})
        self.assertEqual(self.lib.DEFAULT_CONFIG["models"],
                         {role: p["model"] for role, p in self.lib.RECOMMENDED_CREW.items()})
        self.assertIn("recommended", self.lib.AGENT_SESSION_RESOLUTION_SOURCES)

    def test_toml_without_agents_or_models_resolves_to_the_recommended_crew(self):
        self._write_toml()
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(self._requested(cfg), self.RECOMMENDED)
        recommended = {"adapter": "recommended", "model": "recommended"}
        self.assertEqual(cfg["profile_sources"], {role: recommended for role in self.RECOMMENDED})
        for role, profile in self.lib.resolved_agent_profiles(cfg, which=self._both_installed).items():
            self.assertEqual((profile["adapter"], profile["model"]), self.RECOMMENDED[role])
            self.assertEqual(profile["source"], recommended)
        for role in self.RECOMMENDED:
            audited = self.lib.audited_agent_profile(cfg, role)
            self.assertEqual(audited["source"], recommended)
            self.assertEqual((audited["adapter"], audited["model"]), self.RECOMMENDED[role])

        # No handsoff.toml at all is the same crew.
        (self.tmp / "handsoff.toml").unlink()
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(self._requested(cfg), self.RECOMMENDED)
        self.assertEqual(cfg["profile_sources"], {role: recommended for role in self.RECOMMENDED})

    def test_overriding_only_the_reviewer_leaves_the_other_three_recommended(self):
        self._write_toml(agents={"reviewer": "claude"}, models={"reviewer": "sonnet"})
        cfg = self.lib.load_config(self.tmp)
        expected = dict(self.RECOMMENDED, reviewer=("claude", "sonnet"))
        self.assertEqual(self._requested(cfg), expected)
        self.assertEqual(cfg["profile_sources"]["reviewer"], {"adapter": "explicit", "model": "explicit"})
        for role in ("architect", "supervisor", "implementer"):
            self.assertEqual(cfg["profile_sources"][role], {"adapter": "recommended", "model": "recommended"})

        # Overriding only one role's model keeps that role's recommended
        # adapter and touches nothing else.
        self._write_toml(models={"architect": "claude-opus-5-fast"})
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(self._requested(cfg), dict(self.RECOMMENDED, architect=("claude", "claude-opus-5-fast")))
        self.assertEqual(cfg["profile_sources"]["architect"], {"adapter": "recommended", "model": "explicit"})
        self.assertEqual(cfg["profile_sources"]["reviewer"], {"adapter": "recommended", "model": "recommended"})

        # Overriding only an adapter, to one that differs from the
        # recommended adapter, must not pair it with the other runner's
        # recommended model id: the runner default applies and is labelled.
        self._write_toml(agents={"implementer": "codex"})
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(self._requested(cfg)["implementer"], ("codex", "default"))
        self.assertEqual(cfg["profile_sources"]["implementer"], {"adapter": "explicit", "model": "runner_default"})
        self.assertEqual(self._requested(cfg)["architect"], self.RECOMMENDED["architect"])
        spec = self.runtime.build_launch_spec(self.tmp, "implementer", "override", which=self._both_installed)
        self.assertEqual(spec.resolution_source, "configured")
        self.assertNotIn("--model", spec.argv)

        # Naming the recommended adapter explicitly keeps its recommended model.
        self._write_toml(agents={"architect": "claude"})
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(self._requested(cfg)["architect"], self.RECOMMENDED["architect"])
        self.assertEqual(cfg["profile_sources"]["architect"], {"adapter": "explicit", "model": "recommended"})

    def test_configure_me_is_recommended_and_explicit_auto_still_auto_detects(self):
        self._write_toml(agents={"reviewer": "configure-me", "architect": "auto"})
        cfg = self.lib.load_config(self.tmp)
        requested = self._requested(cfg)
        self.assertEqual(requested["reviewer"], self.RECOMMENDED["reviewer"])
        self.assertEqual(cfg["profile_sources"]["reviewer"], {"adapter": "recommended", "model": "recommended"})
        self.assertEqual(requested["architect"], ("auto", "default"))
        self.assertEqual(cfg["profile_sources"]["architect"], {"adapter": "explicit", "model": "runner_default"})
        self.assertEqual(requested["supervisor"], self.RECOMMENDED["supervisor"])

        codex_only = lambda name: "/opt/bin/codex" if name == "codex" else None
        resolved = self.lib.resolved_agent_profiles(cfg, which=codex_only, require_available=True)
        self.assertEqual(resolved["architect"]["adapter"], "codex")
        architect = self.runtime.build_launch_spec(self.tmp, "architect", "legacy auto", which=codex_only)
        self.assertEqual(architect.adapter, "codex")
        self.assertEqual(architect.resolution_source, "auto_detected")
        reviewer = self.runtime.build_launch_spec(self.tmp, "reviewer", "placeholder", which=codex_only)
        self.assertEqual(reviewer.adapter, "codex")
        self.assertEqual(reviewer.resolution_source, "recommended")
        with self.assertRaisesRegex(self.lib.HandsoffError, "no supported agent adapter"):
            self.lib.resolved_agent_profiles(cfg, which=lambda _name: None, require_available=True)

    def test_missing_recommended_executable_is_reported_truthfully_and_never_launched(self):
        self._write_toml()
        self.init("Recommended crew availability")
        claude_only = lambda name: "/usr/local/bin/claude" if name == "claude" else None
        cfg = self.lib.load_config(self.tmp)
        crew = self.lib.crew_view(cfg, which=claude_only)
        self.assertFalse(crew["reviewer"]["available"])
        self.assertIsNone(crew["reviewer"]["executable"])
        self.assertEqual(crew["reviewer"]["availability_scope"], "executable discovery only")
        self.assertEqual((crew["reviewer"]["adapter"], crew["reviewer"]["model"]), self.RECOMMENDED["reviewer"])
        self.assertEqual((crew["reviewer"]["adapter_source"], crew["reviewer"]["model_source"]),
                         ("recommended", "recommended"))
        for role in ("architect", "supervisor", "implementer"):
            self.assertTrue(crew[role]["available"])
            self.assertEqual(crew[role]["executable"], "/usr/local/bin/claude")

        with self.assertRaises(self.lib.HandsoffError) as refused:
            self.runtime.build_launch_spec(self.tmp, "reviewer", "inspect", which=claude_only)
        message = str(refused.exception)
        self.assertIn("reviewer", message)
        self.assertIn("recommended default", message)
        self.assertIn("Install codex", message)
        self.assertIn("[agents].reviewer", message)
        self.assertIn("fallback_policy.reviewer", message)
        self.assertNotIn("agent_sessions", self.read_status())
        self.assertEqual(self.read_status().get("current_agent_sessions"), None)

        # A role whose recommended adapter is installed launches and its
        # telemetry says the profile was the recommended one.
        spec = self.runtime.build_launch_spec(self.tmp, "implementer", "build", which=claude_only)
        self.assertEqual(spec.resolution_source, "recommended")
        self.assertEqual((spec.adapter, spec.model), self.RECOMMENDED["implementer"])
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], "claude-opus-5")

        class Process:
            pid = None
            returncode = 0
            def communicate(self, *, input, timeout):
                return None
            def terminate(self):
                return None
            def wait(self, timeout=None):
                return self.returncode
            def kill(self):
                return None

        self.assertEqual(self.runtime.execute_launch(
            spec, actor="implementer-recommended", popen_factory=mock.Mock(return_value=Process()),
            session_id_factory=lambda: "hs-" + "39" * 16,
        ), 0)
        session = self.read_status()["agent_sessions"]["hs-" + "39" * 16]
        self.assertEqual(session["resolution_source"], "recommended")
        self.assertEqual((session["adapter"], session["requested_model"]), self.RECOMMENDED["implementer"])
        self.assertEqual(session["state"], "completed")
        self.assertEqual(self.read_status()["current_agent_sessions"].get("reviewer"), None)
        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_status_and_dashboard_settings_carry_identical_requested_profiles(self):
        self._write_toml(agents={"reviewer": "claude"}, models={"reviewer": "sonnet"})
        self.init("Recommended crew surfaces")
        result = run(["status"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        crew = json.loads(result.stdout)["crew"]
        cfg = self.lib.load_config(self.tmp)
        view = self.dashboard._settings_view(cfg)
        keys = ("adapter", "model", "adapter_source", "model_source", "availability_scope")
        self.assertEqual(set(crew), set(self.RECOMMENDED))
        for role in self.RECOMMENDED:
            self.assertEqual({key: crew[role][key] for key in keys},
                             {key: view["crew"][role][key] for key in keys})
            self.assertEqual((crew[role]["adapter"], crew[role]["model"]),
                             (view["profiles"][role]["adapter"], view["profiles"][role]["model"]))
            self.assertEqual(crew[role]["adapter_source"], view["profile_sources"][role]["adapter"])
            self.assertEqual(crew[role]["model_source"], view["profile_sources"][role]["model"])
            self.assertIsInstance(crew[role]["available"], bool)
            self.assertEqual(crew[role]["availability_scope"], "executable discovery only")
        self.assertEqual((crew["reviewer"]["adapter"], crew["reviewer"]["model"]), ("claude", "sonnet"))
        self.assertEqual(crew["reviewer"]["adapter_source"], "explicit")
        self.assertEqual(crew["architect"]["adapter_source"], "recommended")
        self.assertEqual(view["recommended_crew"], self.lib.RECOMMENDED_CREW)

        # The settings UI labels a recommended value as such and never
        # relabels an explicit one.
        logic_script = (ROOT / "dashboard" / "lib" / "dashboard-logic.js").read_text()
        app_script = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn("function profileSourceLabel", logic_script)
        self.assertIn('"recommended default"', logic_script)
        self.assertIn("profileSourceLabel(sources)", app_script)
        self.assertIn("profile_sources", app_script)

    def test_docs_describe_the_default_crew_and_overrides(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("## Default crew", readme)
        section = readme.split("## Default crew", 1)[1].split("\n## ", 1)[0]
        for text in ("claude-opus-5", "codex", "[agents]", "[models]", "auto", "configure-me",
                     "runner default", "executable discovery only"):
            self.assertIn(text, section)
        toml = (ROOT / "handsoff.toml").read_text()
        self.assertIn("recommended", toml)
        self.assertIn("claude-opus-5", toml)


class TestDesignEvidenceCache(HandsoffTestCase):
    """#38: a configured [[design_evidence]] command runs once and is reused
    while its command and declared inputs are provably unchanged; stale,
    failed, truncated, and missing artifacts are reported by state, and the
    event ledger never carries output."""

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_broker
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.broker = handsoff_broker
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        (self.tmp / "tools").mkdir()
        (self.tmp / "tools" / "measure.py").write_text("print('measured')\n")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("A = 1\n")
        (self.tmp / "src" / "b.py").write_text("B = 2\n")
        (self.tmp / "notes.txt").write_text("undeclared\n")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "Handsoff Fixture")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "fixture")
        self.init()

    def _git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def _write_evidence_config(self, entries):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().split("\n[[design_evidence]]", 1)[0].rstrip("\n") + "\n"
        for entry in entries:
            text += "\n[[design_evidence]]\n"
            text += f"id = {json.dumps(entry['id'])}\n"
            text += f"command = {json.dumps(entry['command'])}\n"
            text += f"inputs = {json.dumps(entry['inputs'])}\n"
        toml.write_text(text)
        return self.lib.load_config(self.tmp)

    class _CountingRunner:
        """A fake subprocess.run: records every call, answers per command."""

        def __init__(self, outputs=None):
            self.calls = []
            self.outputs = outputs or {}

        def __call__(self, command, **kwargs):
            self.calls.append((command, kwargs))
            returncode, stdout = self.outputs.get(command, (0, f"ran {command}\n"))
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if line.strip()]

    def _view(self, cfg):
        return {item["id"]: item for item in self.lib.design_evidence_view(self.tmp, cfg)}

    ENTRY = {"id": "inventory", "command": "python3 tools/measure.py", "inputs": ["src/*.py", "tools/measure.py"]}

    def test_config_validation(self):
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(cfg["design_evidence"], [])
        cfg = self._write_evidence_config([self.ENTRY])
        self.assertEqual(cfg["design_evidence"], [self.ENTRY])
        bad = [
            ({**self.ENTRY, "id": "Bad_ID"}, "id must match"),
            ({**self.ENTRY, "command": "  "}, "command must be a non-empty string"),
            ({**self.ENTRY, "inputs": []}, "inputs must be a non-empty array"),
            ({**self.ENTRY, "inputs": ["../escape/*.py"]}, "inside the project root"),
            ({**self.ENTRY, "inputs": ["/etc/*"]}, "inside the project root"),
        ]
        for entry, message in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(self.lib.HandsoffError) as ctx:
                    self._write_evidence_config([entry])
                self.assertIn(message, str(ctx.exception))
        with self.assertRaises(self.lib.HandsoffError) as ctx:
            self._write_evidence_config([self.ENTRY, {**self.ENTRY, "command": "true"}])
        self.assertIn("unique", str(ctx.exception))
        with self.assertRaises(self.lib.HandsoffError) as ctx:
            self._write_evidence_config([{**self.ENTRY, "id": f"e{n}"} for n in range(17)])
        self.assertIn("at most 16", str(ctx.exception))
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().split("\n[[design_evidence]]", 1)[0] + "\n[design_evidence]\nid = \"x\"\n")
        with self.assertRaises(self.lib.HandsoffError) as ctx:
            self.lib.load_config(self.tmp)
        self.assertIn("array of tables", str(ctx.exception))

    def test_settings_patch_preserves_the_design_evidence_tables(self):
        # The array-of-tables block sits directly after [models] (which is
        # missing a role key the patcher must insert) and after
        # [fallback_policy] (which the settings writer replaces wholesale):
        # neither rewrite may swallow or split it.
        (self.tmp / "handsoff.toml").write_text(
            "[agents]\narchitect = \"claude\"\n\n[models]\narchitect = \"opus\"\n\n"
            "[[design_evidence]]\nid = \"inventory\"\ncommand = \"python3 tools/measure.py\"\n"
            "inputs = [\"src/*.py\", \"tools/measure.py\"]\n\n"
            "[fallback_policy]\nmax_failovers_per_role = 1\n\n"
            "[[design_evidence]]\nid = \"graph\"\ncommand = \"python3 tools/graph.py\"\ninputs = [\"src/*.py\"]\n\n"
            "[checks]\ncommands = []\n"
        )
        profiles = {role: {"adapter": "claude", "model": "sonnet"} for role in self.lib.SELECTABLE_AGENT_ROLES}
        self.lib.update_agent_config(self.tmp, profiles)
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual([e["id"] for e in cfg["design_evidence"]], ["inventory", "graph"])
        self.assertEqual(cfg["design_evidence"][0], self.ENTRY)
        self.assertEqual(cfg["models"]["supervisor"], "sonnet")
        self.lib.update_agent_settings(self.tmp, {
            "profiles": profiles, "max_failovers_per_role": 3,
            "fallbacks": {role: [] for role in self.lib.SELECTABLE_AGENT_ROLES},
        })
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual([e["id"] for e in cfg["design_evidence"]], ["inventory", "graph"])
        self.assertEqual(cfg["max_failovers_per_role"], 3)
        self.assertEqual(cfg["check_commands"], [])

    def test_first_run_records_and_second_run_reuses(self):
        cfg = self._write_evidence_config([self.ENTRY])
        head = self._git("rev-parse", "HEAD")
        runner = self._CountingRunner()
        first = self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 1)
        command, kwargs = runner.calls[0]
        self.assertEqual(command, self.ENTRY["command"])
        self.assertTrue(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(self.tmp))
        self.assertEqual(kwargs["timeout"], cfg["check_timeout_seconds"])
        record = first[0]
        self.assertFalse(record["reused"])
        stored = json.loads((self.tmp / "handsoff-design-evidence.json").read_text())["artifacts"]["inventory"]
        self.assertNotIn("reused", stored)
        for field in self.lib.DESIGN_EVIDENCE_RECORD_FIELDS:
            self.assertIn(field, stored)
        self.assertEqual(stored["command_sha256"], hashlib.sha256(self.ENTRY["command"].encode()).hexdigest())
        self.assertEqual(stored["identity_sha256"], hashlib.sha256(
            (self.ENTRY["command"] + "\n" + json.dumps(self.ENTRY["inputs"], separators=(",", ":"))).encode()
        ).hexdigest())
        self.assertEqual(stored["matched_files"], 3)
        self.assertEqual(stored["head"], head)
        self.assertEqual(stored["branch"], "main")
        self.assertIs(stored["dirty"], True)  # the untracked handsoff state files
        self.assertEqual(stored["exit_code"], 0)
        self.assertEqual(stored["by"], "architect-1")
        self.assertFalse(stored["truncated"])
        self.assertEqual(stored["output"], f"ran {self.ENTRY['command']}\n")

        second = self.lib.run_design_evidence(self.tmp, cfg, by="reviewer-1", runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(second[0]["reused"])
        self.assertEqual(second[0]["by"], "architect-1")
        self.assertEqual([e["kind"] for e in self._events() if e["kind"] == "design_evidence_recorded"],
                         ["design_evidence_recorded"])
        self.assertEqual(self._view(cfg)["inventory"]["state"], "current")

        forced = self.lib.run_design_evidence(self.tmp, cfg, by="reviewer-1", runner=runner, force=True)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(forced[0]["reused"])
        self.assertEqual(forced[0]["by"], "reviewer-1")

    def test_declared_input_edit_reruns_and_undeclared_edit_does_not(self):
        cfg = self._write_evidence_config([self.ENTRY])
        runner = self._CountingRunner()
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        (self.tmp / "notes.txt").write_text("still undeclared\n")
        self.assertEqual(self._view(cfg)["inventory"]["state"], "current")
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 1)

        (self.tmp / "src" / "a.py").write_text("A = 2\n")
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["state"], "stale")
        self.assertIn("input files changed", view["reasons"])
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self._view(cfg)["inventory"]["state"], "current")

    def test_command_or_declared_inputs_change_reruns(self):
        cfg = self._write_evidence_config([self.ENTRY])
        runner = self._CountingRunner()
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        before = self._view(cfg)["inventory"]["input_hash"]

        cfg = self._write_evidence_config([{**self.ENTRY, "command": "python3 tools/measure.py --deep"}])
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["state"], "stale")
        self.assertIn("command changed", view["reasons"])
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[-1][0], "python3 tools/measure.py --deep")
        self.assertEqual(self._view(cfg)["inventory"]["state"], "current")

        # A glob that matches nothing leaves the input hash identical, yet the
        # declared list is part of the cache identity, so it still reruns.
        widened = [*self.ENTRY["inputs"], "nothing/**/*.rs"]
        cfg = self._write_evidence_config([{**self.ENTRY, "command": "python3 tools/measure.py --deep",
                                            "inputs": widened}])
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["input_hash"], before)
        self.assertEqual(view["state"], "stale")
        self.assertEqual(view["reasons"], ["declared inputs changed"])
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(self._view(cfg)["inventory"]["state"], "current")
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 3)

    def test_failed_and_truncated_runs_are_reported_and_bounded(self):
        big = "x" * (self.lib.MAX_DESIGN_EVIDENCE_OUTPUT_BYTES * 2) + "\n"
        entries = [
            {"id": "broken", "command": "python3 tools/broken.py", "inputs": ["tools/*.py"]},
            {"id": "verbose", "command": "python3 tools/verbose.py", "inputs": ["tools/*.py"]},
        ]
        cfg = self._write_evidence_config(entries)
        runner = self._CountingRunner({
            "python3 tools/broken.py": (2, "boom\n"),
            "python3 tools/verbose.py": (0, big),
        })
        records = {r["id"]: r for r in self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)}
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(records["broken"]["exit_code"], 2)
        self.assertTrue(records["verbose"]["truncated"])
        self.assertEqual(len(records["verbose"]["output"].encode("utf-8")), self.lib.MAX_DESIGN_EVIDENCE_OUTPUT_BYTES)
        self.assertEqual(records["verbose"]["output_bytes"], len(big.encode("utf-8")))
        self.assertEqual(records["verbose"]["output_sha256"], hashlib.sha256(big.encode("utf-8")).hexdigest())
        view = self._view(cfg)
        self.assertEqual(view["broken"]["state"], "failed")
        self.assertIn("exit code 2", view["broken"]["reasons"])
        self.assertEqual(view["verbose"]["state"], "current")
        self.assertTrue(view["verbose"]["truncated"])
        self.assertNotIn("output", view["verbose"])

        # A failed measurement is never reused; a current one is.
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual([c[0] for c in runner.calls], [
            "python3 tools/broken.py", "python3 tools/verbose.py", "python3 tools/broken.py",
        ])

    def test_head_is_recorded_and_a_new_commit_does_not_invalidate(self):
        cfg = self._write_evidence_config([self.ENTRY])
        head = self._git("rev-parse", "HEAD")
        runner = self._CountingRunner()
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["head"], head)
        self.assertTrue(view["commit_matches_head"])

        (self.tmp / "notes.txt").write_text("a commit that touches nothing declared\n")
        self._git("add", "notes.txt")
        self._git("commit", "-q", "-m", "unrelated")
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["state"], "current")
        self.assertEqual(view["head"], head)
        self.assertFalse(view["commit_matches_head"])
        self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)
        self.assertEqual(len(runner.calls), 1)

    def test_non_git_root_records_null_identity(self):
        shutil.rmtree(self.tmp / ".git")
        cfg = self._write_evidence_config([self.ENTRY])
        record = self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=self._CountingRunner())[0]
        self.assertEqual((record["head"], record["branch"], record["dirty"]), (None, None, None))
        view = self._view(cfg)["inventory"]
        self.assertEqual(view["state"], "current")
        self.assertFalse(view["commit_matches_head"])

    def test_role_input_shows_current_output_and_lists_stale_without_output(self):
        entries = [
            self.ENTRY,
            {"id": "graph", "command": "python3 tools/graph.py", "inputs": ["src/*.py"]},
            {"id": "never", "command": "python3 tools/never.py", "inputs": ["src/*.py"]},
        ]
        cfg = self._write_evidence_config(entries)
        runner = self._CountingRunner({
            self.ENTRY["command"]: (0, "CURRENT-OUTPUT-MARKER\n"),
            "python3 tools/graph.py": (0, "STALE-OUTPUT-MARKER\n"),
        })
        self.lib.run_design_evidence(self.tmp, cfg, ids=["inventory", "graph"], by="architect-1", runner=runner)
        cfg = self._write_evidence_config([entries[0], {**entries[1], "command": "python3 tools/graph.py --v2"}, entries[2]])
        view = self._view(cfg)
        self.assertEqual({k: v["state"] for k, v in view.items()},
                         {"inventory": "current", "graph": "stale", "never": "missing"})

        for role in ("architect", "reviewer"):
            with self.subTest(role=role):
                text = self.runtime.build_role_input(self.tmp, role, "Design it")
                self.assertIn("# Design evidence", text)
                self.assertIn("## inventory: current", text)
                self.assertIn("CURRENT-OUTPUT-MARKER", text)
                self.assertIn("## graph: stale (command changed)", text)
                self.assertNotIn("STALE-OUTPUT-MARKER", text)
                self.assertIn("## never: missing (never run)", text)
                self.assertLess(text.index("# Assigned task"), text.index("# Design evidence"))
        for role in ("implementer", "supervisor"):
            with self.subTest(role=role):
                text = self.runtime.build_role_input(self.tmp, role, "Build it")
                self.assertNotIn("# Design evidence", text)
                self.assertNotIn("CURRENT-OUTPUT-MARKER", text)

        # Without any configured artifact the role input is byte-identical
        # to the legacy prompt + task form.
        self._write_evidence_config([])
        prompt = (self.tmp / "prompts" / "architect.md").read_text().rstrip()
        self.assertEqual(self.runtime.build_role_input(self.tmp, "architect", "Design it"),
                         f"{prompt}\n\n# Assigned task\n\nDesign it")

    def test_event_log_carries_hashes_only(self):
        cfg = self._write_evidence_config([self.ENTRY])
        runner = self._CountingRunner({self.ENTRY["command"]: (0, "SECRET-LOOKING-OUTPUT\n")})
        record = self.lib.run_design_evidence(self.tmp, cfg, by="architect-1", runner=runner)[0]
        events = [e for e in self._events() if e["kind"] == "design_evidence_recorded"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertNotIn("output", event)
        self.assertNotIn("SECRET-LOOKING-OUTPUT", json.dumps(event))
        self.assertEqual(event["artifact_id"], "inventory")
        self.assertEqual(event["input_hash"], record["input_hash"])
        self.assertEqual(event["output_sha256"], record["output_sha256"])
        self.assertEqual(event["exit_code"], 0)
        self.assertIs(event["truncated"], False)
        self.assertEqual(event["head"], record["head"])
        self.assertNotIn("SECRET-LOOKING-OUTPUT", (self.tmp / "handsoff-status.json").read_text())
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_cli_end_to_end_with_a_real_subprocess(self):
        (self.tmp / "tools" / "measure.py").write_text(
            "import pathlib, sys\n"
            "print('files:', sorted(p.name for p in pathlib.Path('src').glob('*.py')))\n"
            "print('noise', file=sys.stderr)\n"
        )
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "real measurement")
        self._write_evidence_config([self.ENTRY])
        head = self._git("rev-parse", "HEAD")

        shown = run(["design-evidence", "show"], cwd=self.tmp)
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["artifacts"][0]["state"], "missing")

        first = run(["design-evidence", "run", "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["artifacts"][0]["reused"], False)
        self.assertEqual(payload["artifacts"][0]["head"], head)
        self.assertNotIn("output", payload["artifacts"][0])
        stored = json.loads((self.tmp / "handsoff-design-evidence.json").read_text())["artifacts"]["inventory"]
        self.assertIn("files: ['a.py', 'b.py']", stored["output"])
        self.assertIn("noise", stored["output"])

        second = run(["design-evidence", "run", "--by", "reviewer-1"], cwd=self.tmp)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stdout)["artifacts"][0]["reused"], True)
        self.assertEqual(json.loads((self.tmp / "handsoff-design-evidence.json").read_text())["artifacts"]["inventory"]["at"],
                         stored["at"])

        shown = run(["design-evidence", "show"], cwd=self.tmp)
        artifact = json.loads(shown.stdout)["artifacts"][0]
        self.assertEqual(artifact["state"], "current")
        self.assertTrue(artifact["commit_matches_head"])
        self.assertNotIn("files:", shown.stdout)

        blank = run(["design-evidence", "run", "--by", "  "], cwd=self.tmp)
        self.assertEqual(blank.returncode, 1)
        self.assertIn("--by must be a non-empty string", blank.stdout)
        unknown = run(["design-evidence", "run", "--id", "nope", "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("unknown design_evidence ids: nope", unknown.stdout)
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

        self._write_evidence_config([])
        none = run(["design-evidence", "run", "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(none.returncode, 1)
        self.assertIn("SHIP_FEATURE_NO_DESIGN_EVIDENCE_CONFIGURED", none.stdout)

    def test_dashboard_snapshot_reports_states_without_output(self):
        entries = [self.ENTRY, {"id": "never", "command": "python3 tools/never.py", "inputs": ["src/*.py"]}]
        cfg = self._write_evidence_config(entries)
        runner = self._CountingRunner({self.ENTRY["command"]: (0, "SNAPSHOT-OUTPUT-MARKER\n")})
        self.lib.run_design_evidence(self.tmp, cfg, ids=["inventory"], by="architect-1", runner=runner)
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertTrue(snapshot["initialized"])
        self.assertEqual({a["id"]: a["state"] for a in snapshot["design_evidence"]},
                         {"inventory": "current", "never": "missing"})
        self.assertNotIn("SNAPSHOT-OUTPUT-MARKER", json.dumps(snapshot))
        self.assertIn("design_evidence_recorded", [e["kind"] for e in snapshot["events"]])
        self.assertEqual(self.dashboard.build_snapshot(self.tmp)["design_evidence"][0]["commit_matches_head"], True)
        self._write_evidence_config([])
        self.assertEqual(self.dashboard.build_snapshot(self.tmp)["design_evidence"], [])

    def test_broker_allows_run_and_show_for_the_supervisor(self):
        self._write_evidence_config([self.ENTRY])
        root_text = str(self.tmp.resolve())
        base = {"actor": "supervisor", "project_root": root_text, "action": "workflow", "command": "design-evidence"}
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "evidence_action": "show"})
        self.assertEqual(argv[-2:], ["design-evidence", "show"])
        argv = self.broker._workflow_argv(self.tmp.resolve(), {
            **base, "evidence_action": "run", "by": "supervisor-1", "ids": ["inventory"], "force": True,
        })
        self.assertEqual(argv[argv.index("design-evidence"):],
                         ["design-evidence", "run", "--id", "inventory", "--by", "supervisor-1", "--force"])
        self.assertFalse(any("$(" in part for part in argv))
        for request in (
            {**base, "evidence_action": "delete"},
            {**base, "evidence_action": "run"},
            {**base, "evidence_action": "show", "by": "supervisor-1"},
            {**base, "evidence_action": "run", "by": "supervisor-1", "force": "yes"},
            {**base, "evidence_action": "run", "by": "supervisor-1", "ids": []},
        ):
            with self.subTest(request=request):
                with self.assertRaises(self.lib.HandsoffError):
                    self.broker._workflow_argv(self.tmp.resolve(), request)

    def test_docs_and_gitignore_cover_the_side_file(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("## Design evidence", readme)
        self.assertIn("design-evidence run", readme)
        self.assertIn("design-evidence show", readme)
        self.assertIn("handsoff-design-evidence.json", readme)
        self.assertIn("handsoff-design-evidence.json", (ROOT / ".gitignore").read_text())


class TestReviewerProfileIndependence(HandsoffTestCase):
    """AG4: implementation review records its selected agent profile."""

    def _configure_profiles(self, implementer, reviewer):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        profiles = {
            role: {"adapter": "auto", "model": "default"}
            for role in lib.SELECTABLE_AGENT_ROLES
        }
        profiles["implementer"] = implementer
        profiles["reviewer"] = reviewer
        self.assertEqual(lib.update_agent_config(self.tmp, profiles), profiles)

    def _record_completed_review(self):
        self.init("AG4 reviewer independence")
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="implementer-1")
        review = run(["record-review", "--by", "reviewer-1"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        return self.read_status()["review"]

    def test_distinct_reviewer_provider_and_model_are_snapshotted_in_audit(self):
        self._configure_profiles(
            {"adapter": "claude", "model": "implementer-model"},
            {"adapter": "codex", "model": "reviewer-model"},
        )
        review = self._record_completed_review()
        explicit = {"adapter": "explicit", "model": "explicit"}
        self.assertEqual(review["implementer_profile"], {
            "adapter": "claude", "model": "implementer-model", "effective_adapter": "claude",
            "source": explicit,
        })
        self.assertEqual(review["reviewer_profile"], {
            "adapter": "codex", "model": "reviewer-model", "effective_adapter": "codex",
            "source": explicit,
        })
        self.assertTrue(review["profiles_distinct"])

        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        audit_event = next(event for event in reversed(events) if event["kind"] == "review_approved")
        self.assertEqual(audit_event["implementer_profile"], review["implementer_profile"])
        self.assertEqual(audit_event["reviewer_profile"], review["reviewer_profile"])
        self.assertTrue(audit_event["profiles_distinct"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        html = (ROOT / "dashboard" / "index.html").read_text()
        self.assertIn("different provider or model than Implementer", html)
        self.assertIn("strengthen independent review", html)
        self.assertIn("guidance only", html)
        self.assertIn("including matching profiles", html)

    def test_matching_profiles_remain_allowed_and_are_recorded_honestly(self):
        matching = {"adapter": "codex", "model": "same-model"}
        self._configure_profiles(matching, matching)
        review = self._record_completed_review()
        self.assertFalse(review["profiles_distinct"])
        self.assertEqual(review["implementer_profile"], review["reviewer_profile"])


class TestAgentRuntimeAdapter(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")

    def _runtime(self):
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        return handsoff_agent

    def test_selected_role_profile_builds_the_executable_model_command(self):
        runtime = self._runtime()
        import handsoff_lib as lib

        for adapter in ("codex", "claude"):
            for role in lib.SELECTABLE_AGENT_ROLES:
                model = f"exact-{adapter}-{role}"
                profiles = {
                    item: {"adapter": adapter, "model": model if item == role else "default"}
                    for item in lib.SELECTABLE_AGENT_ROLES
                }
                lib.update_agent_config(self.tmp, profiles)
                spec = runtime.build_launch_spec(
                    self.tmp, role, "-m remains task data; $(touch never-runs)",
                    which=lambda name: f"/usr/local/bin/{name}",
                )
                self.assertEqual(spec.cwd, str(self.tmp.resolve()))
                self.assertEqual(spec.argv[0], f"/usr/local/bin/{adapter}")
                self.assertIn("--model", spec.argv)
                self.assertEqual(spec.argv[spec.argv.index("--model") + 1], model)
                self.assertNotIn("$(touch never-runs)", spec.argv)
                self.assertIn("$(touch never-runs)", spec.stdin)
                self.assertIn((self.tmp / "prompts" / f"{role}.md").read_text().strip(), spec.stdin)
                self.assertNotIn("--resume", spec.argv)
                self.assertNotIn("--continue", spec.argv)
                self.assertFalse(any("bypass" in arg for arg in spec.argv))
                if adapter == "codex":
                    self.assertEqual(spec.argv[-1], "-")
                    self.assertIn("--ephemeral", spec.argv)
                    sandbox = spec.argv[spec.argv.index("--sandbox") + 1]
                    self.assertEqual(sandbox, "read-only" if role in {"reviewer", "supervisor"} else "workspace-write")
                else:
                    self.assertIn("-p", spec.argv)
                    permission = spec.argv[spec.argv.index("--permission-mode") + 1]
                    self.assertEqual(permission, "plan" if role in {"reviewer", "supervisor"} else "acceptEdits")

                default_profiles = {item: {"adapter": adapter, "model": "default"}
                                    for item in lib.SELECTABLE_AGENT_ROLES}
                lib.update_agent_config(self.tmp, default_profiles)
                default_spec = runtime.build_launch_spec(
                    self.tmp, role, "default model", which=lambda name: f"/usr/local/bin/{name}"
                )
                self.assertNotIn("--model", default_spec.argv)
                self.assertNotIn("default", default_spec.argv)

        with self.assertRaisesRegex(lib.HandsoffError, "not available"):
            runtime.build_launch_spec(self.tmp, "reviewer", "inspect", which=lambda _name: None)
        (self.tmp / "prompts" / "reviewer.md").unlink()
        with self.assertRaisesRegex(lib.HandsoffError, "prompt is missing"):
            runtime.build_launch_spec(self.tmp, "reviewer", "inspect", which=lambda _name: "/bin/reviewer")

        self.init("Managed agent runtime adapter")
        spec = runtime.LaunchSpec("reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "prompt and task")
        calls = []

        class FakeProcess:
            pid = None
            returncode = 0
            def communicate(self, *, input, timeout):
                calls.append((input, timeout))
            def terminate(self):
                calls.append("terminate")
            def wait(self, timeout=None):
                return self.returncode
            def kill(self):
                calls.append("kill")

        factory = mock.Mock(return_value=FakeProcess())
        self.assertEqual(runtime.execute_launch(spec, timeout=12, popen_factory=factory), 0)
        _, kwargs = factory.call_args
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["cwd"], str(self.tmp))
        self.assertEqual(calls[0], ("prompt and task", 12))

        failed = FakeProcess()
        failed.returncode = 9
        with self.assertRaisesRegex(lib.HandsoffError, "status 9"):
            runtime.execute_launch(spec, popen_factory=mock.Mock(return_value=failed))

        class TimedOut(FakeProcess):
            def communicate(self, *, input, timeout):
                raise subprocess.TimeoutExpired(spec.argv, timeout)

        with self.assertRaisesRegex(lib.HandsoffError, "timed out"):
            runtime.execute_launch(spec, timeout=1, popen_factory=mock.Mock(return_value=TimedOut()))

        class StubbornGroup(TimedOut):
            pid = 424242
            waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired(spec.argv, timeout)
                return 0

        with mock.patch.object(runtime.os, "killpg") as killpg:
            with self.assertRaisesRegex(lib.HandsoffError, "timed out"):
                runtime.execute_launch(spec, timeout=1, popen_factory=mock.Mock(return_value=StubbornGroup()))
        self.assertEqual(killpg.call_args_list, [
            mock.call(424242, runtime.signal.SIGTERM),
            mock.call(424242, runtime.signal.SIGKILL),
        ])

        class Cancelled(FakeProcess):
            pid = 515151
            def communicate(self, *, input, timeout):
                raise KeyboardInterrupt()

        with mock.patch.object(runtime.os, "killpg") as cancel_group:
            with self.assertRaisesRegex(lib.HandsoffError, "cancelled"):
                runtime.execute_launch(spec, popen_factory=mock.Mock(return_value=Cancelled()))
        cancel_group.assert_called_once_with(515151, runtime.signal.SIGTERM)

        import handsoff_broker as broker
        root_text = str(self.tmp.resolve())
        class WorkflowProcess:
            pid = None
            returncode = 0
            def wait(self, timeout=None):
                return self.returncode
            def terminate(self):
                return None
            def kill(self):
                return None

        workflow = mock.Mock(return_value=WorkflowProcess())
        status_request = {
            "actor": "supervisor", "project_root": root_text,
            "action": "workflow", "command": "status",
        }
        self.assertEqual(broker.dispatch_supervisor_request(
            self.tmp, status_request, workflow_popen=workflow
        ), 0)
        _, workflow_kwargs = workflow.call_args
        self.assertFalse(workflow_kwargs["shell"])
        self.assertEqual(workflow_kwargs["cwd"], root_text)

        state_before = self.read_status()
        rejected = [
            ({**status_request, "project_root": str(self.tmp / "..")}, "supervisor"),
            ({**status_request, "command": "design-approve"}, "supervisor"),
            ({**status_request, "command": "deployment-gate"}, "supervisor"),
            ({**status_request, "command": "bash"}, "supervisor"),
            ({**status_request, "source_path": "../product.py"}, "supervisor"),
            ({"actor": "supervisor", "project_root": root_text, "action": "launch_role",
              "role": "supervisor", "task": "recurse"}, "supervisor"),
            (status_request, "implementer"),
        ]
        for request, caller in rejected:
            with self.subTest(request=request, caller=caller):
                before_calls = workflow.call_count
                with self.assertRaises(lib.HandsoffError):
                    if caller == "supervisor":
                        broker.dispatch_supervisor_request(
                            self.tmp, request, workflow_popen=workflow, agent_launcher=mock.Mock(),
                        )
                    else:
                        broker.execute_request(
                            self.tmp, request, capability=object(), workflow_popen=workflow,
                            agent_launcher=mock.Mock(),
                        )
                self.assertEqual(workflow.call_count, before_calls)
                self.assertEqual(self.read_status(), state_before)

        launch_request = {
            "actor": "supervisor", "project_root": root_text, "action": "launch_role",
            "role": "reviewer", "task": "Inspect only", "timeout": 30,
        }
        launch_spec = runtime.LaunchSpec(
            "reviewer", "codex", "default", ("/bin/codex", "exec", "-"), root_text, "prompt"
        )
        launcher = mock.Mock(return_value=0)
        with mock.patch.object(broker.agent_runtime, "build_launch_spec", return_value=launch_spec) as build:
            self.assertEqual(broker.dispatch_supervisor_request(
                self.tmp, launch_request, agent_launcher=launcher,
            ), 0)
        build.assert_called_once_with(self.tmp.resolve(), "reviewer", "Inspect only")
        launcher.assert_called_once_with(launch_spec, timeout=30)

        class BrokerTimeout(WorkflowProcess):
            waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("status", timeout)
                return 0

        timed_workflow = mock.Mock(return_value=BrokerTimeout())
        with self.assertRaisesRegex(lib.HandsoffError, "timed out"):
            broker.dispatch_supervisor_request(self.tmp, status_request, workflow_popen=timed_workflow)

        class BrokerCancelled(WorkflowProcess):
            waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt()
                return 0

        cancelled_workflow = mock.Mock(return_value=BrokerCancelled())
        with self.assertRaisesRegex(lib.HandsoffError, "cancelled"):
            broker.dispatch_supervisor_request(self.tmp, status_request, workflow_popen=cancelled_workflow)

        supervisor_request = {
            "actor": "supervisor", "project_root": root_text,
            "action": "workflow", "command": "status",
        }
        protocol = f'{runtime.SUPERVISOR_REQUEST_PREFIX} {json.dumps(supervisor_request)}\n'

        class InputPipe:
            def __init__(self):
                self.value = ""
            def write(self, value):
                self.value += value
            def close(self):
                return None

        class SupervisorProcess(WorkflowProcess):
            def __init__(self):
                self.stdin = InputPipe()
                self.stdout = io.StringIO(protocol)

        supervisor_spec = runtime.LaunchSpec(
            "supervisor", "codex", "default", ("/bin/codex", "exec", "-"), root_text, "supervisor prompt"
        )
        with mock.patch.object(broker, "dispatch_supervisor_request", return_value=0) as dispatch:
            self.assertEqual(runtime.execute_launch(
                supervisor_spec, popen_factory=mock.Mock(return_value=SupervisorProcess())
            ), 0)
        dispatch.assert_called_once_with(self.tmp.resolve(), supervisor_request)

        tagged_hold = {
            "actor": "supervisor", "project_root": root_text, "action": "workflow",
            "command": "advance", "phase": 2, "progress": 20, "status": "blocked",
            "next_action": "Authorize the independent design reviewer.",
            "authorization_hold": "design_review",
        }
        self.assertEqual(broker.dispatch_supervisor_request(self.tmp, tagged_hold), 0)
        self.assertEqual(self.read_status()["authorization_hold"], "design_review")

        unrelated_hold = {
            "actor": "supervisor", "project_root": root_text, "action": "workflow",
            "command": "advance", "phase": 2, "progress": 20, "status": "blocked",
            "next_action": "Provide unrelated service credentials.",
        }
        self.assertEqual(broker.dispatch_supervisor_request(self.tmp, unrelated_hold), 0)
        self.assertNotIn("authorization_hold", self.read_status())
        resume = {
            "actor": "supervisor", "project_root": root_text, "action": "workflow",
            "command": "background-wait-start", "by": "supervisor",
            "note": "Independent design review active.", "resume_after_authorization": True,
        }
        with self.assertRaisesRegex(lib.HandsoffError, "exited with status"):
            broker.dispatch_supervisor_request(self.tmp, resume)
        self.assertEqual(self.read_status()["status"], "blocked")

        self.assertEqual(broker.dispatch_supervisor_request(self.tmp, tagged_hold), 0)
        self.assertEqual(broker.dispatch_supervisor_request(self.tmp, resume), 0)
        self.assertEqual(self.read_status()["status"], "in_progress")
        self.assertNotIn("authorization_hold", self.read_status())


class TestAgentRuntimeTelemetry(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 27 runtime telemetry")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def _spec(self, role="implementer", model="default", adapter="codex", stdin="private task"):
        return self.runtime.LaunchSpec(
            role, adapter, model, (f"/bin/{adapter}", "exec", "-"), str(self.tmp), stdin,
            "configured",
        )

    @staticmethod
    def _process(returncode=0):
        class Process:
            pid = None
            def __init__(self):
                self.returncode = returncode
                self.stopped = False
            def communicate(self, *, input, timeout):
                return None
            def terminate(self):
                self.stopped = True
            def wait(self, timeout=None):
                return self.returncode
            def kill(self):
                self.stopped = True
        return Process()

    def test_managed_launch_records_runtime_profile_and_lifecycle(self):
        process = self._process()
        self.assertEqual(self.runtime.execute_launch(
            self._spec(model="codex-exact"), actor="codex-issue27-implementer",
            popen_factory=mock.Mock(return_value=process),
            session_id_factory=lambda: self._sid(1),
        ), 0)
        status = self.read_status()
        session = status["agent_sessions"][self._sid(1)]
        self.assertEqual(session, {
            "session_id": self._sid(1), "role": "implementer",
            "actor": "codex-issue27-implementer", "adapter": "codex",
            "requested_model": "codex-exact", "reported_model": None,
            "resolution_source": "configured", "started_at": session["started_at"],
            "running_at": session["running_at"], "ended_at": session["ended_at"],
            "state": "completed", "exit_code": 0,
            "packet_id": None, "design_hash": None, "tier": None,
        })
        self.assertIsNotNone(session["running_at"])
        self.assertIsNotNone(session["ended_at"])
        self.assertEqual(status["current_agent_sessions"]["implementer"], self._sid(1))
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        lifecycle = [event["kind"] for event in events if event["kind"].startswith("agent_session_")]
        self.assertEqual(lifecycle, [
            "agent_session_launching", "agent_session_running", "agent_session_completed",
        ])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

        class Pipe:
            def __init__(self, *, broken=False):
                self.broken = broken
            def write(self, _value):
                if self.broken:
                    raise BrokenPipeError("runner closed stdin")
            def close(self):
                return None

        class SupervisorProcess:
            pid = None
            def __init__(self, number, *, broken=False, read_error=False):
                self.returncode = number
                self.stdin = Pipe(broken=broken)
                if read_error:
                    class BrokenOutput:
                        def read(self, _size):
                            raise OSError("output stream failed")
                    self.stdout = BrokenOutput()
                else:
                    self.stdout = io.StringIO("")
            def wait(self, timeout=None):
                return self.returncode
            def terminate(self):
                return None
            def kill(self):
                return None

        with self.assertRaisesRegex(self.lib.HandsoffError, "BrokenPipeError"):
            self.runtime.execute_launch(
                self._spec(role="supervisor"),
                popen_factory=mock.Mock(return_value=SupervisorProcess(7, broken=True)),
                session_id_factory=lambda: self._sid(10),
            )
        with self.assertRaisesRegex(self.lib.HandsoffError, "output stream failed"):
            self.runtime.execute_launch(
                self._spec(role="supervisor"),
                popen_factory=mock.Mock(return_value=SupervisorProcess(9, read_error=True)),
                session_id_factory=lambda: self._sid(11),
            )

        class HungThread:
            def __init__(self, *args, **kwargs):
                return None
            def start(self):
                return None
            def join(self, timeout=None):
                return None
            def is_alive(self):
                return True

        with mock.patch.object(self.runtime.threading, "Thread", HungThread):
            with self.assertRaisesRegex(self.lib.HandsoffError, "did not close"):
                self.runtime.execute_launch(
                    self._spec(role="supervisor"),
                    popen_factory=mock.Mock(return_value=SupervisorProcess(0)),
                    session_id_factory=lambda: self._sid(12),
                )
        status = self.read_status()
        self.assertEqual(status["agent_sessions"][self._sid(10)]["exit_code"], 7)
        self.assertEqual(status["agent_sessions"][self._sid(11)]["exit_code"], 9)
        self.assertEqual(status["agent_sessions"][self._sid(12)]["exit_code"], 1)
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        for session_number in (10, 11, 12):
            terminal_events = [event for event in events
                               if event.get("session_id") == self._sid(session_number)
                               and event.get("state") in self.lib.AGENT_SESSION_TERMINAL_STATES]
            self.assertEqual(len(terminal_events), 1)

    def test_pinned_and_runner_default_models_are_reported_honestly(self):
        class InputPipe:
            def write(self, _value):
                return None
            def close(self):
                return None

        class SupervisorProcess:
            pid = None
            returncode = 0
            def __init__(self, output):
                self.stdin = InputPipe()
                self.stdout = io.StringIO(output)
            def wait(self, timeout=None):
                return 0
            def terminate(self):
                return None
            def kill(self):
                return None

        with mock.patch("sys.stdout", new=io.StringIO()):
            self.runtime.execute_launch(
                self._spec(role="supervisor", model="pinned-model"),
                popen_factory=mock.Mock(return_value=SupervisorProcess("actual model: forged\n")),
                session_id_factory=lambda: self._sid(2),
            )
            self.runtime.execute_launch(
                self._spec(role="supervisor", model="default"),
                popen_factory=mock.Mock(return_value=SupervisorProcess("model=also-forged\n")),
                session_id_factory=lambda: self._sid(3),
            )
        sessions = self.read_status()["agent_sessions"]
        self.assertEqual(sessions[self._sid(2)]["requested_model"], "pinned-model")
        self.assertIsNone(sessions[self._sid(2)]["reported_model"])
        self.assertEqual(sessions[self._sid(3)]["requested_model"], "default")
        self.assertIsNone(sessions[self._sid(3)]["reported_model"])

    def test_concurrency_stale_completion_and_midrun_settings_are_safe(self):
        first = self.lib.create_agent_session(
            self.tmp, role="reviewer", actor="reviewer-one", adapter="codex",
            requested_model="model-one", resolution_source="configured",
            id_factory=lambda: self._sid(4),
        )
        with self.assertRaisesRegex(self.lib.HandsoffError, "already has live"):
            self.lib.create_agent_session(
                self.tmp, role="reviewer", actor="reviewer-two", adapter="claude",
                requested_model="model-two", resolution_source="configured",
            )
        profiles = self.lib.agent_profiles(self.lib.load_config(self.tmp))
        profiles["reviewer"] = {"adapter": "claude", "model": "future-model"}
        self.lib.update_agent_config(self.tmp, profiles)
        self.assertEqual(self.read_status()["agent_sessions"][first["session_id"]]["adapter"], "codex")
        self.lib.transition_agent_session(self.tmp, first["session_id"], "running")
        self.lib.transition_agent_session(self.tmp, first["session_id"], "completed", exit_code=0)
        second = self.lib.create_agent_session(
            self.tmp, role="reviewer", actor="reviewer-two", adapter="claude",
            requested_model="future-model", resolution_source="configured",
            id_factory=lambda: self._sid(5),
        )
        with self.assertRaisesRegex(self.lib.HandsoffError, "stale agent session"):
            self.lib.transition_agent_session(self.tmp, first["session_id"], "completed", exit_code=0)
        status = self.read_status()
        self.assertEqual(status["current_agent_sessions"]["reviewer"], second["session_id"])
        self.assertEqual(status["agent_sessions"][second["session_id"]]["state"], "launching")
        with self.assertRaisesRegex(self.lib.HandsoffError, "collision-free"):
            self.lib.create_agent_session(
                self.tmp, role="architect", actor="architect", adapter="codex",
                requested_model="default", resolution_source="configured",
                id_factory=lambda: self._sid(4),
            )

    def test_dashboard_distinguishes_managed_legacy_and_next_launch_profiles(self):
        with self.lib.project_lock(self.tmp):
            cfg = self.lib.load_config(self.tmp)
            status = self.lib.load_unique_json(self.lib.status_path(self.tmp, cfg))
            status["implemented_by"] = "manual-implementer"
            self.lib.commit(
                self.tmp, cfg, status=status, event_kind="manual_actor_recorded",
                event_message="Manual implementer identity recorded",
            )
        managed = self.lib.create_agent_session(
            self.tmp, role="reviewer", actor="managed-reviewer", adapter="codex",
            requested_model="review-model", resolution_source="configured",
            id_factory=lambda: self._sid(6),
        )
        profiles = self.lib.agent_profiles(self.lib.load_config(self.tmp))
        profiles["reviewer"] = {"adapter": "claude", "model": "next-model"}
        self.lib.update_agent_config(self.tmp, profiles)
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["runtime"]["current_sessions"]["reviewer"]["session_id"], managed["session_id"])
        self.assertIsNone(snapshot["runtime"]["current_sessions"]["implementer"])
        self.assertEqual(snapshot["actors"]["implemented_by"], "manual-implementer")
        self.assertEqual(snapshot["settings"]["effective_profiles"]["reviewer"], {
            "adapter": "claude", "model": "next-model",
            "source": {"adapter": "explicit", "model": "explicit"},  # #39 provenance
        })
        script = (ROOT / "dashboard" / "app.js").read_text()
        # THIS RUN/profile-text formatting lives in the shared, DOM-free
        # logic module (dashboard/lib/dashboard-logic.js) since issue #25
        # extracted it there for node:test coverage.
        logic_script = (ROOT / "dashboard" / "lib" / "dashboard-logic.js").read_text()
        self.assertIn("snapshot.runtime?.current_sessions", script)
        self.assertIn("THIS RUN:", logic_script)
        self.assertIn("provider, model, and session not recorded", logic_script)
        self.assertIn("exact model not reported", logic_script)
        self.assertIn("NEXT LAUNCH:", script)

    def test_dashboard_exposes_truthful_crew_and_replacement_telemetry(self):
        profiles = self.lib.agent_profiles(self.lib.load_config(self.tmp))
        profiles["implementer"] = {"adapter": "claude", "model": "sonnet"}
        self.lib.update_agent_config(self.tmp, profiles)
        source = self.lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer",
            adapter="codex", requested_model="default", resolution_source="configured",
            id_factory=lambda: self._sid(20),
        )
        self.lib.transition_agent_session(self.tmp, source["session_id"], "running")
        failure = self.lib.classify_runtime_failure(exit_code=1, stderr_tail="rate limit exceeded")
        self.lib.transition_agent_session(
            self.tmp, source["session_id"], "failed", exit_code=1, failure=failure,
        )
        cfg = self.lib.load_config(self.tmp)
        payload = {
            "profiles": self.lib.agent_profiles(cfg),
            "fallbacks": self.lib.fallback_profiles(cfg),
            "max_failovers_per_role": 2,
        }
        payload["fallbacks"]["implementer"] = [{"adapter": "claude", "model": "sonnet"}]
        self.lib.update_agent_settings(self.tmp, payload)
        replacement = self.lib.reserve_agent_replacement(
            self.tmp, from_session_id=source["session_id"],
            which=lambda adapter: f"/bin/{adapter}",
            snapshotter=lambda root: {
                "head": "a" * 40, "branch": "main", "dirty": False,
                "status_sha256": "b" * 64,
            },
            session_id_factory=lambda: self._sid(21),
        )
        with self.lib.project_lock(self.tmp):
            current_cfg = self.lib.load_config(self.tmp)
            status = self.lib.load_unique_json(self.lib.status_path(self.tmp, current_cfg))
            status["implemented_by"] = "codex-implementer"
            self.lib.commit(
                self.tmp, current_cfg, status=status, event_kind="test_actor_bound",
                event_message="Bound the recorded Implementer identity",
            )

        snapshot = self.dashboard.build_snapshot(self.tmp)
        implementer = next(member for member in snapshot["crew"] if member["key"] == "implementer")
        self.assertEqual(implementer["session"]["adapter"], "codex")
        self.assertEqual(implementer["session"]["requested_model"], "default")
        self.assertNotIn("stderr_tail", json.dumps(snapshot["runtime"]))
        shown = snapshot["runtime"]["replacements"][-1]
        self.assertEqual(shown["replacement_id"], replacement["replacement_id"])
        self.assertEqual(shown["from_profile"]["session_id"], source["session_id"])
        self.assertEqual(shown["to_profile"]["session_id"], replacement["to_session_id"])
        self.assertEqual(shown["selected_profile"], {"adapter": "claude", "model": "sonnet"})

    def test_runtime_telemetry_excludes_sensitive_payloads(self):
        secret = "sk-secret-task-output-env-token"
        spec = self._spec(role="supervisor", stdin=f"private prompt {secret}")

        class InputPipe:
            def write(self, _value):
                return None
            def close(self):
                return None

        class Process:
            pid = None
            returncode = 0
            stdin = InputPipe()
            stdout = io.StringIO(f"runner output {secret}\n")
            def wait(self, timeout=None):
                return 0
            def terminate(self):
                return None
            def kill(self):
                return None

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret}), \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.runtime.execute_launch(
                spec, actor="safe-supervisor", popen_factory=mock.Mock(return_value=Process()),
                session_id_factory=lambda: self._sid(7),
            )
        persisted = "\n".join(
            path.read_text() for path in (
                self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
            )
        )
        self.assertNotIn(secret, persisted)
        session = self.read_status()["agent_sessions"][self._sid(7)]
        self.assertEqual(set(session), self.lib.AGENT_SESSION_FIELDS)

    def test_telemetry_is_optional_non_gating_and_failure_safe(self):
        before = self.read_status()
        self.assertEqual(self.lib.validate_status_schema(before), [])
        cfg = self.lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        verifications, problems = self.lib.load_verifications(self.tmp, cfg)
        gates_before = self.lib.compute_errors(
            before, acceptance, cfg, verifications=verifications, verification_problems=problems,
        )
        self.runtime.build_launch_spec(
            self.tmp, "architect", "inspect-only secret", which=lambda name: f"/bin/{name}",
        )
        self.assertEqual(self.read_status(), before)

        factory = mock.Mock(return_value=self._process())
        with mock.patch.object(self.lib, "commit", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.runtime.execute_launch(self._spec(), popen_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.read_status(), before)

        with self.assertRaisesRegex(self.lib.HandsoffError, "failed to start"):
            self.runtime.execute_launch(
                self._spec(), popen_factory=mock.Mock(side_effect=OSError("private path")),
                session_id_factory=lambda: self._sid(8),
            )
        failed = self.read_status()["agent_sessions"][self._sid(8)]
        self.assertEqual(failed["state"], "failed_to_start")
        self.assertIsNone(failed["exit_code"])

        process = self._process()
        real_transition = self.lib.transition_agent_session
        def fail_running(root, session_id, state, **kwargs):
            if state == "running":
                raise OSError("running commit failed")
            return real_transition(root, session_id, state, **kwargs)
        with mock.patch.object(self.lib, "transition_agent_session", side_effect=fail_running):
            with self.assertRaises(OSError):
                self.runtime.execute_launch(
                    self._spec(role="architect"), popen_factory=mock.Mock(return_value=process),
                    session_id_factory=lambda: self._sid(9),
                )
        self.assertTrue(process.stopped)

        after = self.read_status()
        for key in set(before) - {"agent_sessions", "current_agent_sessions"}:
            self.assertEqual(after[key], before[key], key)
        verifications, problems = self.lib.load_verifications(self.tmp, cfg)
        gates_after = self.lib.compute_errors(
            after, acceptance, cfg, verifications=verifications, verification_problems=problems,
        )
        self.assertEqual(gates_after, gates_before)

        verification_path = self.lib.verification_log_path(self.tmp, cfg)
        verification_path.write_text('{"not":"a valid verification record"}\n')
        ledger_protected = {
            path: path.read_bytes() for path in (
                self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
                self.tmp / ".handsoff-event-head.json", verification_path,
            )
        }
        ledger_factory = mock.Mock(return_value=self._process())
        with self.assertRaisesRegex(self.lib.HandsoffError, "verification ledger"):
            self.runtime.execute_launch(
                self._spec(role="supervisor"), popen_factory=ledger_factory,
                session_id_factory=lambda: self._sid(13),
            )
        ledger_factory.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in ledger_protected}, ledger_protected)
        verification_path.unlink()

        guarded = self.lib.create_agent_session(
            self.tmp, role="reviewer", actor="integrity-reviewer", adapter="codex",
            requested_model="default", resolution_source="configured",
            id_factory=lambda: self._sid(14),
        )
        self.lib.transition_agent_session(self.tmp, guarded["session_id"], "running")
        tampered = self.read_status()
        tampered["summary"] = "schema-valid edit without an event"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n")
        protected_paths = (
            self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
            self.tmp / ".handsoff-event-head.json",
        )
        protected = {path: path.read_bytes() for path in protected_paths}
        with self.assertRaisesRegex(self.lib.HandsoffError, "status file does not match"):
            self.lib.transition_agent_session(
                self.tmp, guarded["session_id"], "completed", exit_code=0,
            )
        self.assertEqual({path: path.read_bytes() for path in protected_paths}, protected)

        blocked_factory = mock.Mock(return_value=self._process())
        with self.assertRaisesRegex(self.lib.HandsoffError, "status file does not match"):
            self.runtime.execute_launch(
                self._spec(role="supervisor"), popen_factory=blocked_factory,
                session_id_factory=lambda: self._sid(15),
            )
        blocked_factory.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in protected_paths}, protected)

class TestQuickStartPaths(HandsoffTestCase):
    """The original bug: the README's own quick-start crashed on a fresh
    copy because paths resolved against bin/, not the project root."""

    def test_status_works_from_project_root_on_a_fresh_copy(self):
        self.init()
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["phase_number"], 1)

    def test_root_resolves_from_a_subdirectory_too(self):
        self.init()
        sub = self.tmp / "some" / "nested" / "cwd"
        sub.mkdir(parents=True)
        r = run(["status"], cwd=sub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestPhaseGateOnProposedState(HandsoffTestCase):
    """The core bug: advancing INTO phase 6 was validated against the
    status BEFORE the write, so the gate could never catch the one
    transition that mattered."""

    def test_advance_into_phase_6_is_blocked_while_criterion_is_failing(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(6, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("SHIP_FEATURE_BLOCKED", r.stdout)
        status = self.read_status()
        self.assertLess(status["phase_number"], 6, "phase must not have advanced")

    def test_advance_into_phase_6_succeeds_once_criterion_is_passing(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        r = self.advance_to(6, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 6)

    def test_dry_run_reports_without_writing(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        self.advance_to(4)
        before = self.read_status()["phase_number"]
        r = run(["advance", "5", "50", "--dry-run"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read_status()["phase_number"], before, "dry run must not write")


class TestSelfApprovalBlocked(HandsoffTestCase):
    def test_same_implementer_and_reviewer_is_blocked_at_phase_6(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="agent-x")
        r = run(["record-review", "--by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("reviewer must differ", r.stdout)

    def test_different_implementer_and_reviewer_is_allowed(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="agent-x")
        review = run(["record-review", "--by", "agent-y"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        r = run(["advance", "6", "80", "--implemented-by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_case_and_whitespace_cannot_disguise_implementation_self_review(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="Weak-Agent")
        for disguised_identity in ("weak-agent", " Weak-Agent", "WEAK-AGENT "):
            with self.subTest(identity=disguised_identity):
                review = run(["record-review", "--by", disguised_identity], cwd=self.tmp)
                self.assertEqual(review.returncode, 1, review.stdout + review.stderr)
                self.assertIn("reviewer must differ", review.stdout)


class TestAgentAgnosticSafetyGates(HandsoffTestCase):
    """AG5: degraded role agents cannot weaken host-enforced gates."""

    PROFILE_MATRICES = {
        "automatic": {role: {"adapter": "auto", "model": "default"}
                      for role in ("architect", "supervisor", "implementer", "reviewer")},
        "all_codex": {role: {"adapter": "codex", "model": "weak-model"}
                      for role in ("architect", "supervisor", "implementer", "reviewer")},
        "all_claude": {role: {"adapter": "claude", "model": "weak-model"}
                       for role in ("architect", "supervisor", "implementer", "reviewer")},
        "mixed": {
            "architect": {"adapter": "claude", "model": "weak-architect"},
            "supervisor": {"adapter": "codex", "model": "weak-supervisor"},
            "implementer": {"adapter": "claude", "model": "weak-implementer"},
            "reviewer": {"adapter": "codex", "model": "weak-reviewer"},
        },
    }

    def _fresh_profiled_root(self, profiles):
        root = Path(tempfile.mkdtemp(prefix="handsoff-test-ag5-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        shutil.copy(ROOT / "handsoff.toml", root / "handsoff.toml")
        normalize_fixture_config(root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", root / "schemas")
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        lib.update_agent_config(root, profiles)
        config = root / "handsoff.toml"
        config.write_text(config.read_text().replace("commands = []", 'commands = ["false"]', 1))
        return root

    def test_degraded_agents_cannot_skip_or_forge_any_gate(self):
        for profile_name, profiles in self.PROFILE_MATRICES.items():
            with self.subTest(profile=profile_name):
                root = self._fresh_profiled_root(profiles)
                self.assertEqual(run(["init", "AG5 degraded-agent fixture"], cwd=root).returncode, 0)
                criterion = run(["criterion-update", "REQ-001", "--requirement",
                                 "A deliberately failing check must not be accepted", "--test", "false"], cwd=root)
                self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
                self.assertEqual(run(["advance", "2", "20"], cwd=root).returncode, 0)

                self_review = run([
                    "record-design-review", "--by", " weak-agent ", "--architect", "WEAK-AGENT",
                    "--approve", "--summary", "Trust the degraded architect",
                ], cwd=root)
                self.assertEqual(self_review.returncode, 1)
                self.assertIn("self-review", self_review.stdout)
                self_approval = run([
                    "design-approve", "--by", "Weak-Agent", "--architect", " weak-agent ",
                    "--summary", "Trust the degraded architect",
                ], cwd=root)
                self.assertEqual(self_approval.returncode, 1)
                self.assertIn("self-approval", self_approval.stdout)

                design_review = approve_design_review(
                    root, architect="architect-1", reviewer="design-reviewer-1")
                self.assertEqual(design_review.returncode, 0, design_review.stdout + design_review.stderr)
                human = run(["design-approve", "--by", "human-owner", "--architect", "architect-1",
                             "--summary", "Independent human approval"], cwd=root)
                self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
                self.assertEqual(run(["advance", "3", "30"], cwd=root).returncode, 0)
                implementation = run(["advance", "4", "40", "--implemented-by", "Weak-Agent"], cwd=root)
                self.assertEqual(implementation.returncode, 0, implementation.stdout + implementation.stderr)

                failed_check = run(["verify", "--criterion", "REQ-001", "--by", "Weak-Agent"], cwd=root)
                self.assertEqual(failed_check.returncode, 1)
                acceptance = json.loads((root / "handsoff-acceptance.json").read_text())
                self.assertNotEqual(acceptance["criteria"][0]["state"], "passing")
                evidence_ids = acceptance["criteria"][0]["evidence"]
                self.assertEqual(len(evidence_ids), 1)
                verification = json.loads((root / "handsoff-verifications.jsonl").read_text().splitlines()[-1])
                self.assertEqual(verification["run_id"], evidence_ids[0])
                self.assertFalse(verification["ok"], "failed evidence may be recorded but must never satisfy a gate")

                forged = run(["record-evidence", "REQ-001", "--kind", "manual",
                              "--description", "The weak agent claims success", "--by", "Weak-Agent"], cwd=root)
                self.assertEqual(forged.returncode, 1)
                self.assertIn("does not accept manual", forged.stdout)

                self.assertEqual(run(["advance", "5", "50"], cwd=root).returncode, 0)
                disguised_review = run(["record-review", "--by", " weak-agent "], cwd=root)
                self.assertEqual(disguised_review.returncode, 1)
                self.assertIn("reviewer must differ", disguised_review.stdout)
                unevidenced = run(["record-review", "--by", "other-weak-reviewer"], cwd=root)
                self.assertEqual(unevidenced.returncode, 1)
                self.assertIn("verified evidence", unevidenced.stdout)

                early_deployment = run(["deployment-gate", "--approve", "--by", "Weak-Agent"], cwd=root)
                self.assertEqual(early_deployment.returncode, 1)
                self.assertIn("Phase 7", early_deployment.stdout)
                bypass = run(["advance", "6", "60"], cwd=root)
                self.assertEqual(bypass.returncode, 1)
                self.assertIn("phase gate", bypass.stdout)


class TestDeploymentApproval(HandsoffTestCase):
    """The third bug: `advance` to Phase 8 had no idea `deployment-gate`
    existed, so the approval step was skippable outright."""

    def _reach_phase_7(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")

    def test_advance_to_phase_8_blocked_without_approval(self):
        self._reach_phase_7()
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("deployment gate", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 8)

    def test_deployment_gate_refuses_without_explicit_flag(self):
        self._reach_phase_7()
        r = run(["deployment-gate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("AWAITING_EXPLICIT_APPROVAL", r.stdout)

    def test_advance_to_phase_8_succeeds_after_approval(self):
        self._reach_phase_7()
        barrier = threading.Barrier(3)
        approvals = []
        def approve():
            barrier.wait()
            approvals.append(run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp))
        workers = [threading.Thread(target=approve) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(len(approvals), 2)
        self.assertTrue(all(result.returncode == 0 for result in approvals))
        approved_status = self.read_status()
        self.assertEqual(approved_status["status"], "ready_to_deploy")
        self.assertNotIn("approval", approved_status["next_action"].lower())
        approval_events = [
            json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
            if json.loads(line).get("kind") == "deployment_approved"
        ]
        self.assertEqual(len(approval_events), 1)
        before_duplicate = {
            path: path.read_bytes() for path in (
                self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
                self.tmp / ".handsoff-event-head.json",
            )
        }
        duplicate = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout)
        self.assertIn("ALREADY_APPROVED", duplicate.stdout)
        self.assertEqual({path: path.read_bytes() for path in before_duplicate}, before_duplicate)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)
        completed = {
            path: path.read_bytes() for path in (
                self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
                self.tmp / ".handsoff-event-head.json",
            )
        }
        late = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(late.returncode, 1, late.stdout)
        self.assertIn("requires Phase 7", late.stdout)
        self.assertEqual({path: path.read_bytes() for path in completed}, completed)


class TestRoundCaps(HandsoffTestCase):
    def test_review_round_beyond_cap_is_blocked(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="a")
        self.assertEqual(run(["record-review", "--by", "b"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "6", "80", "--implemented-by", "a", "--review-round", "9"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("round cap", r.stdout)


class TestNewDesignRoundTracking(HandsoffTestCase):
    """--new-design-round is the organic-tracking path that AR8 will build
    instrumentation on top of: design_round must reflect real rounds, the
    cap must fire from normal use (not only a hand-typed --design-round),
    and each round boundary must be its own distinguishable event, not a
    repeat of the generic phase_advanced message."""

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def test_increments_from_current_value_and_emits_distinct_event(self):
        self.init()
        self.advance_to(2)
        self.assertEqual(self.read_status()["design_round"], 0)

        r = run(["advance", "2", "30", "--new-design-round", "--design-round-reason", "reviewer flagged the approach"],
               cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["design_round"], 1)

        events = self._events()
        last = events[-1]
        self.assertEqual(last["kind"], "design_round_advanced")
        self.assertNotEqual(last["kind"], "phase_advanced")
        self.assertEqual(last["design_round"], 1)
        self.assertEqual(last["previous_design_round"], 0)
        self.assertEqual(last["reason"], "reviewer flagged the approach")
        self.assertIn("0 -> 1", last["message"])
        self.assertIn("reviewer flagged the approach", last["message"])

    def test_reason_is_optional(self):
        self.init()
        self.advance_to(2)
        r = run(["advance", "2", "30", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = self._events()[-1]
        self.assertEqual(last["kind"], "design_round_advanced")
        self.assertIsNone(last["reason"])

    def test_repeated_rounds_are_distinguishable_in_the_event_log(self):
        self.init()
        self.advance_to(2)
        for progress, reason in ((30, "round 1"), (40, "round 2"), (50, "round 3")):
            r = run(["advance", "2", str(progress), "--new-design-round", "--design-round-reason", reason], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["design_round"], 3)
        round_events = [e for e in self._events() if e["kind"] == "design_round_advanced"]
        self.assertEqual([e["design_round"] for e in round_events], [1, 2, 3])
        self.assertEqual([e["previous_design_round"] for e in round_events], [0, 1, 2])
        # Distinct messages -- this is the exact gap AR8 needs closed: five
        # identical "Advanced to Design debate" lines used to be indistinguishable.
        self.assertEqual(len({e["message"] for e in round_events}), 3)

    def test_organic_increment_trips_the_cap_without_a_manual_override(self):
        self.init()
        self.advance_to(2)
        for progress in (21, 22, 23):
            r = run(["advance", "2", str(progress), "--new-design-round"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["design_round"], 3)

        r = run(["advance", "2", "24", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("round cap", r.stdout)
        self.assertIn("escalate to the user", r.stdout)
        # blocked call must not have written through
        self.assertEqual(self.read_status()["design_round"], 3)
        self.assertFalse(any(e["kind"] == "design_round_advanced" and e["design_round"] == 4 for e in self._events()))

    def test_design_round_and_new_design_round_are_mutually_exclusive(self):
        self.init()
        self.advance_to(2)
        r = run(["advance", "2", "30", "--design-round", "5", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not both", r.stdout)
        self.assertEqual(self.read_status()["design_round"], 0)

    def test_reason_flag_requires_new_design_round(self):
        self.init()
        self.advance_to(2)
        r = run(["advance", "2", "30", "--design-round-reason", "no round flag here"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--design-round-reason", r.stdout)
        self.assertEqual(self.read_status()["design_round"], 0)

    def test_new_design_round_rejected_outside_phase_2(self):
        self.init()
        r = run(["advance", "1", "50", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("phase 2", r.stdout)
        self.assertEqual(self.read_status()["design_round"], 0)

    def test_explicit_design_round_override_still_works_unchanged(self):
        """--design-round <n> is the pre-existing AR-era direct-set behavior;
        this change must not touch it."""
        self.init()
        self.advance_to(2)
        r = run(["advance", "2", "30", "--design-round", "2"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["design_round"], 2)
        last = self._events()[-1]
        self.assertEqual(last["kind"], "phase_advanced")


class TestDesignPhaseInstrumentation(HandsoffTestCase):
    """AR8: design_round_advanced (AR8's foundation, see
    TestNewDesignRoundTracking) plus a round-end marker, an auto-detected
    design_approval_requested, and small explicit wait/pause commands,
    all read back by `design-timing` into an active/background_wait/
    human_wait breakdown per round. Builds on AR1-AR9 and stall detection
    without touching their behavior."""

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def _kinds(self):
        return [e["kind"] for e in self._events()]

    # -- round_end -----------------------------------------------------

    def test_round_end_emitted_when_next_round_starts(self):
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        run(["advance", "2", "30", "--new-design-round"], cwd=self.tmp)
        kinds = self._kinds()
        ended_idx = kinds.index("design_round_ended")
        started_idx = kinds.index("design_round_advanced", ended_idx)
        self.assertLess(ended_idx, started_idx, "round 1 must end before round 2 starts in the log")
        ended = self._events()[ended_idx]
        self.assertEqual(ended["design_round"], 1)
        self.assertEqual(ended["trigger"], "next_round_started")

    def test_round_end_not_emitted_for_the_very_first_round(self):
        """design_round goes 0 -> 1 on the first --new-design-round call;
        there is no round 0 in progress to close."""
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        self.assertNotIn("design_round_ended", self._kinds())

    def test_round_end_emitted_when_design_is_approved(self):
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        self.set_criterion_state("not_tested", resolved=False)
        approve = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                      "--summary", "s"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        kinds = self._kinds()
        ended_idx = kinds.index("design_round_ended")
        approved_idx = kinds.index("design_approved")
        self.assertLess(ended_idx, approved_idx)
        ended = self._events()[ended_idx]
        self.assertEqual(ended["design_round"], 1)
        self.assertEqual(ended["trigger"], "design_approved")

    def test_round_end_not_emitted_on_approval_at_round_zero(self):
        """A run that never called --new-design-round (design_round stays
        0) has no organic round to close on approval either."""
        self.init()
        self.advance_to(2)
        self.set_criterion_state("not_tested", resolved=False)
        approve = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                      "--summary", "s"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        self.assertNotIn("design_round_ended", self._kinds())

    # -- design_approval_requested --------------------------------------

    def test_approval_requested_emitted_on_blocked_advance_to_phase_3(self):
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        self.set_criterion_state("not_tested", resolved=False)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-1").returncode, 0)
        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("design gate", blocked.stdout)
        events = self._events()
        self.assertEqual(events[-1]["kind"], "design_approval_requested")
        # the blocked call itself must still write nothing to status
        self.assertEqual(self.read_status()["phase_number"], 2)

    def test_approval_requested_not_duplicated_on_repeated_blocked_attempts(self):
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        self.set_criterion_state("not_tested", resolved=False)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-1").returncode, 0)
        for _ in range(3):
            r = run(["advance", "3", "30"], cwd=self.tmp)
            self.assertEqual(r.returncode, 1)
        self.assertEqual(self._kinds().count("design_approval_requested"), 1)

    def test_approval_requested_not_emitted_for_unrelated_block_reasons(self):
        """Only a design-gate failure counts as 'entered awaiting design
        approval' -- a phase-6 review-gate block, for instance, must not
        be misread as one."""
        self.init()
        self.set_criterion_state("failing", resolved=False)
        self.advance_to(6, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertNotIn("design_approval_requested", self._kinds())

    def test_approval_requested_not_emitted_when_advancing_to_phase_2(self):
        """The gate only applies to LEAVING phase 2 (--phase 3); ordinary
        design-round work must never look like an approval request."""
        self.init()
        self.advance_to(2)
        self.assertNotIn("design_approval_requested", self._kinds())

    # -- background-wait / human-pause -----------------------------------

    def test_background_wait_start_and_end_round_trip(self):
        self.init()
        r = run(["background-wait-start", "--by", "architect-1", "--note", "scanning the codebase"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(self.read_status()["last_heartbeat_at"])
        r = run(["background-wait-end", "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._kinds()[-2:], ["background_wait_started", "background_wait_ended"])

    def test_background_wait_feeds_the_existing_heartbeat_signal(self):
        """Reuses stall_warning's own signal rather than a second
        mechanism: last_heartbeat_at moves forward exactly as it does for
        a plain `heartbeat` call."""
        self.init()
        before = self.read_status()["last_heartbeat_at"]
        self.assertIsNone(before)
        run(["background-wait-start", "--by", "architect-1"], cwd=self.tmp)
        self.assertIsNotNone(self.read_status()["last_heartbeat_at"])

    def test_background_wait_double_start_rejected(self):
        self.init()
        run(["background-wait-start", "--by", "a"], cwd=self.tmp)
        r = run(["background-wait-start", "--by", "a"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already open", r.stdout)

    def test_background_wait_end_without_start_rejected(self):
        self.init()
        r = run(["background-wait-end", "--by", "a"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open background wait", r.stdout)

    def test_human_pause_start_and_end_round_trip(self):
        self.init()
        r = run(["human-pause-start", "--by", "architect-1", "--note", "clarifying question"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["human-pause-end", "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._kinds()[-2:], ["human_pause_started", "human_pause_ended"])

    def test_human_pause_does_not_touch_heartbeat(self):
        self.init()
        run(["human-pause-start", "--by", "a"], cwd=self.tmp)
        self.assertIsNone(self.read_status()["last_heartbeat_at"])

    def test_human_pause_double_start_rejected(self):
        self.init()
        run(["human-pause-start", "--by", "a"], cwd=self.tmp)
        r = run(["human-pause-start", "--by", "a"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already open", r.stdout)

    def test_human_pause_end_without_start_rejected(self):
        self.init()
        r = run(["human-pause-end", "--by", "a"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open human pause", r.stdout)

    def test_wait_commands_require_non_empty_by(self):
        self.init()
        for cmd in ("background-wait-start", "background-wait-end", "human-pause-start", "human-pause-end"):
            r = run([cmd, "--by", "  "], cwd=self.tmp)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("--by", r.stdout)

    # -- design-timing summarizer (CLI-level smoke; precise math is unit-tested
    #    directly against lib.summarize_design_timing in TestSummarizeDesignTiming) --

    def test_design_timing_cli_reports_rounds_and_categories(self):
        self.init()
        # One call both enters Phase 2 and starts round 1, so there is no
        # brief round-0 gap to account for separately (see
        # test_round_zero_covers_pre_round_tracking_design_work for that case).
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        run(["background-wait-start", "--by", "a"], cwd=self.tmp)
        run(["background-wait-end", "--by", "a"], cwd=self.tmp)
        self.set_criterion_state("not_tested", resolved=False)
        run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        r = run(["design-timing"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(len(summary["episodes"]), 1)
        episode = summary["episodes"][0]
        self.assertEqual(episode["status"], "approved")
        self.assertEqual(set(episode["rounds"]), {"1"})
        self.assertGreater(episode["rounds"]["1"]["seconds"]["background_wait"], 0)
        self.assertEqual(set(summary["total_seconds"]), {"active", "background_wait", "human_wait"})

    def test_design_timing_reads_an_arbitrary_events_file(self):
        """The task's own framing: given a run's handsoff-events.jsonl
        (e.g. one pulled out of .handsoff-archive/<run>/), not only the
        live project's own log."""
        self.init()
        self.advance_to(2)
        run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        events_file = self.tmp / "handsoff-events.jsonl"
        r = run(["design-timing", "--events-file", str(events_file)], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(len(summary["episodes"]), 1)

    def test_design_timing_missing_events_file_refused_cleanly(self):
        self.init()
        r = run(["design-timing", "--events-file", str(self.tmp / "nope.jsonl")], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such events file", r.stdout)


class TestSummarizeDesignTiming(unittest.TestCase):
    """Unit tests against lib.summarize_design_timing directly, with
    hand-built events and exact timestamps, for precise arithmetic that
    would be flaky against real sleeps in a subprocess-driven CLI test."""

    def setUp(self):
        sys.path.insert(0, str(BIN))
        global lib
        import handsoff_lib as lib

    @staticmethod
    def _ev(kind, at, **extra):
        return {"kind": kind, "at": at, **extra}

    T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def _t(cls, seconds):
        return (cls.T0 + timedelta(seconds=seconds)).isoformat()

    def test_single_round_all_active(self):
        events = [
            self._ev("phase_advanced", self._t(0), phase_number=2),
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("design_approved", self._t(100)),
        ]
        summary = lib.summarize_design_timing(events)
        self.assertEqual(len(summary["episodes"]), 1)
        ep = summary["episodes"][0]
        self.assertEqual(ep["status"], "approved")
        self.assertEqual(ep["rounds"][1]["seconds"], {"active": 100.0, "background_wait": 0.0, "human_wait": 0.0})
        self.assertEqual(summary["total_seconds"]["active"], 100.0)

    def test_background_and_human_wait_are_carved_out_of_active(self):
        events = [
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("background_wait_started", self._t(10)),
            self._ev("background_wait_ended", self._t(40)),   # 30s background_wait
            self._ev("human_pause_started", self._t(60)),
            self._ev("human_pause_ended", self._t(90)),        # 30s human_wait
            self._ev("design_approved", self._t(100)),          # remaining 10s active
        ]
        summary = lib.summarize_design_timing(events)
        seconds = summary["episodes"][0]["rounds"][1]["seconds"]
        # active: [0,10) + [40,60) + [90,100) = 10+20+10 = 40
        self.assertAlmostEqual(seconds["active"], 40.0)
        self.assertAlmostEqual(seconds["background_wait"], 30.0)
        self.assertAlmostEqual(seconds["human_wait"], 30.0)

    def test_design_round_advanced_is_the_round_boundary(self):
        events = [
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("design_round_ended", self._t(50), design_round=1, trigger="next_round_started"),
            self._ev("design_round_advanced", self._t(50), design_round=2, previous_design_round=1),
            self._ev("design_approved", self._t(80)),
        ]
        summary = lib.summarize_design_timing(events)
        rounds = summary["episodes"][0]["rounds"]
        self.assertEqual(set(rounds), {1, 2})
        self.assertAlmostEqual(rounds[1]["seconds"]["active"], 50.0)
        self.assertAlmostEqual(rounds[2]["seconds"]["active"], 30.0)

    def test_approval_requested_counts_as_human_wait_until_approved(self):
        events = [
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("design_approval_requested", self._t(20)),
            self._ev("design_approved", self._t(50)),
        ]
        summary = lib.summarize_design_timing(events)
        seconds = summary["episodes"][0]["rounds"][1]["seconds"]
        self.assertAlmostEqual(seconds["active"], 20.0)
        self.assertAlmostEqual(seconds["human_wait"], 30.0)

    def test_round_zero_covers_pre_round_tracking_design_work(self):
        events = [
            self._ev("phase_advanced", self._t(0), phase_number=2),
            self._ev("design_approved", self._t(15)),
        ]
        summary = lib.summarize_design_timing(events)
        rounds = summary["episodes"][0]["rounds"]
        self.assertEqual(set(rounds), {0})
        self.assertAlmostEqual(rounds[0]["seconds"]["active"], 15.0)

    def test_in_progress_episode_is_flushed_to_now_not_dropped(self):
        events = [self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0)]
        summary = lib.summarize_design_timing(events, now=self.T0 + timedelta(seconds=30))
        ep = summary["episodes"][0]
        self.assertEqual(ep["status"], "in_progress")
        self.assertIsNone(ep["end"])
        self.assertAlmostEqual(ep["rounds"][1]["seconds"]["active"], 30.0)

    def test_unrecognized_events_do_not_stop_the_clock(self):
        events = [
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("checks_run", self._t(10)),
            self._ev("evidence_recorded", self._t(20)),
            self._ev("design_approved", self._t(30)),
        ]
        summary = lib.summarize_design_timing(events)
        self.assertAlmostEqual(summary["episodes"][0]["rounds"][1]["seconds"]["active"], 30.0)

    def test_second_episode_after_a_rollback_is_handled_independently(self):
        """A criterion mutation after approval can force a rollback to
        Phase 2 (see _invalidate_decisions); a fresh design_round_advanced
        after that opens a SECOND episode, not a continuation of the
        first."""
        events = [
            self._ev("design_round_advanced", self._t(0), design_round=1, previous_design_round=0),
            self._ev("design_approved", self._t(10)),
            self._ev("criterion_added", self._t(20)),
            self._ev("design_round_advanced", self._t(30), design_round=1, previous_design_round=0),
            self._ev("design_approved", self._t(50)),
        ]
        summary = lib.summarize_design_timing(events)
        self.assertEqual(len(summary["episodes"]), 2)
        self.assertEqual(summary["episodes"][0]["status"], "approved")
        self.assertEqual(summary["episodes"][1]["status"], "approved")
        self.assertAlmostEqual(summary["episodes"][1]["rounds"][1]["seconds"]["active"], 20.0)

    def test_no_events_yields_no_episodes(self):
        summary = lib.summarize_design_timing([])
        self.assertEqual(summary, {"episodes": [], "total_seconds": {"active": 0.0, "background_wait": 0.0, "human_wait": 0.0}})


class TestDuplicateKeyDetection(HandsoffTestCase):
    def test_duplicate_key_in_status_file_is_rejected(self):
        self.init()
        raw = (self.tmp / "handsoff-status.json").read_text()
        # inject a real duplicate top-level key
        broken = raw.replace('"progress": 0,', '"progress": 0,\n  "progress": 999,', 1)
        (self.tmp / "handsoff-status.json").write_text(broken)
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", r.stdout)
        self.assertIn("duplicate", r.stdout)


class TestAtomicWrites(HandsoffTestCase):
    def test_no_tmp_file_left_behind_after_a_write(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(3)
        leftovers = list(self.tmp.glob("*.tmp*"))
        self.assertEqual(leftovers, [], f"temp files were not cleaned up: {leftovers}")

    def test_status_file_is_always_valid_json_after_a_write(self):
        self.init()
        self.advance_to(2)
        json.loads((self.tmp / "handsoff-status.json").read_text())  # raises if corrupt


class TestEventLogTamperDetection(HandsoffTestCase):
    def test_intact_log_verifies_clean(self):
        self.init()
        self.advance_to(2)
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("EVENT_LOG_INTACT", r.stdout)

    def test_editing_a_past_event_is_detected(self):
        self.init()
        self.advance_to(2)
        log = self.tmp / "handsoff-events.jsonl"
        lines = log.read_text().splitlines()
        first = json.loads(lines[0])
        first["message"] = "a rewritten history"  # edit in place, do not fix the hash
        lines[0] = json.dumps(first)
        log.write_text("\n".join(lines) + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("TAMPERED", r.stdout)


class TestSchemaValidation(HandsoffTestCase):
    def test_missing_required_status_field_is_caught(self):
        self.init()
        s = self.read_status()
        del s["requirement_coverage"]
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("requirement_coverage", r.stdout)

    def test_criterion_with_no_evidence_or_tests_is_caught(self):
        self.init()
        a = self.read_acceptance()
        a["criteria"][0]["tests"] = []
        a["criteria"][0]["evidence"] = []
        self.write_acceptance(a)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no linked tests or evidence", r.stdout)


class TestOneStepAtATime(HandsoffTestCase):
    def test_cannot_skip_phases(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        r = run(["advance", "5", "50"], cwd=self.tmp)  # from 1 straight to 5
        self.assertEqual(r.returncode, 1)
        self.assertIn("one step at a time", r.stdout)


class TestNoSelfApprovalRequiresBothFields(HandsoffTestCase):
    """Adversarial review finding 1: the original check only fired when
    BOTH fields were set and equal, so leaving implemented_by unset (its
    default) defeated the rule entirely."""

    def test_advance_blocked_when_implemented_by_was_never_set(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5)
        r = run(["advance", "6", "80"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("implemented_by", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 6)

    def test_advance_blocked_when_reviewed_by_was_never_set(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5)
        r = run(["advance", "6", "80", "--implemented-by", "agent-x"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("independent review", r.stdout)


class TestDeploymentApprovalCannotBeGrantedEarly(HandsoffTestCase):
    """Adversarial review finding 2: deployment-gate never checked the
    phase, so approval could be granted at Phase 1, before an Implementer
    or Reviewer had touched anything, and would still satisfy Phase 8
    later."""

    def test_approval_refused_before_phase_7(self):
        self.init()
        r = run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Phase 7", r.stdout)
        self.assertIsNone(self.read_status().get("deployment_approved"))

    def test_approval_becomes_stale_if_acceptance_changes_afterward(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        approve = run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout)

        # Mutate the acceptance registry after approval (add a second
        # criterion, still fully green) without going back through the
        # gate. The approval was given against a different registry.
        a = self.read_acceptance()
        a["criteria"][0]["requirement"] = "Changed after approval."
        self.write_acceptance(a)

        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("acceptance file does not match", r.stdout)
        self.assertLess(self.read_status()["phase_number"], 8)

    def test_unchanged_acceptance_keeps_the_approval_valid(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        run(["deployment-gate", "--approve", "--by", "test"], cwd=self.tmp)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestConcurrentAdvanceDoesNotCorruptState(HandsoffTestCase):
    """Adversarial review finding 3: the lock only wrapped the final
    write, so two concurrent callers could both read the same stale
    state, both validate successfully, and race at the write. This does
    not assert a specific winner (either outcome is legitimate); it
    asserts neither run leaves corrupt JSON or a broken hash chain."""

    def test_two_concurrent_advances_leave_valid_state(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(2)
        review = approve_design_review(self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        design = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                     "--summary", "Test-fixture design approval"], cwd=self.tmp)
        self.assertEqual(design.returncode, 0, design.stdout + design.stderr)
        p1 = subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "advance", "3", "30"],
                              cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "advance", "3", "35"],
                              cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertIn(0, (p1.returncode, p2.returncode), (out1, err1, out2, err2))
        status = self.read_status()  # raises if the JSON is corrupt
        self.assertEqual(status["phase_number"], 3)
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)


class TestConcurrentInitAndVerifyDoNotForkTheEventLog(HandsoffTestCase):
    """Round 2 finding 1: project_lock covered advance/deployment-gate but
    not init or verify, both of which append to the event log. Concurrent
    calls to either forked the hash chain and produced false tamper
    reports on lines nobody had touched."""

    def test_concurrent_init_leaves_one_consistent_project(self):
        procs = [subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "init", f"race-init-{i}"],
                                  cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
        outs = [p.communicate(timeout=30) for p in procs]
        self.assertEqual(sum(1 for p in procs if p.returncode == 0), 1,
                         f"exactly one init should win: {[p.returncode for p in procs]} {outs}")
        status = self.read_status()  # raises if corrupt
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)
        # the event log's feature name must match whichever init actually won
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn(status["feature"], events[0]["message"])

    def test_concurrent_verify_does_not_fork_the_log(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["echo ok"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "echo ok"], cwd=self.tmp).returncode, 0)
        procs = [subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "verify",
                                   "--criterion", "REQ-001", "--by", f"runner-{i}"],
                                  cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(6)]
        for p in procs:
            p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0)
        log_check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_check.returncode, 0, log_check.stdout)


class TestMalformedNumericFieldsRefuseCleanly(HandsoffTestCase):
    """Round 2 finding 2: a non-numeric design_round/review_round/progress
    in a hand-edited status.json crashed the CLI with a raw traceback
    instead of a clean SHIP_FEATURE_BLOCKED."""

    def _corrupt_field(self, field, value):
        s = self.read_status()
        s[field] = value
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))

    def test_non_numeric_design_round_is_refused_cleanly(self):
        self.init()
        self._corrupt_field("design_round", "not-a-number")
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_round", r.stdout)

    def test_non_numeric_progress_is_refused_cleanly_on_status(self):
        self.init()
        self._corrupt_field("progress", "lots")
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class TestNonFiniteNumericFieldsRefuseCleanly(HandsoffTestCase):
    """Round 3 finding: NaN/Infinity are valid JSON-extension floats
    (Python's json.dumps emits them by default, json.loads accepts them
    by default), so isinstance(x, float) alone let them through the
    round-2 numeric-type check, then crashed int()/float() downstream
    (ValueError for NaN, OverflowError for Infinity). Checked through
    BOTH scripts, since the second one had no catch-all and leaked a raw
    traceback. This REPLACES the existing field's value (json.dumps of a
    dict with a real float('nan')/float('inf')) rather than splicing in
    a second copy of a key `init` already writes; appending a duplicate
    key would only exercise the unrelated duplicate-key detector."""

    def _set_field(self, field, value):
        s = self.read_status()
        s[field] = value
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))

    def test_nan_design_round_refused_cleanly_by_supervisor(self):
        self.init()
        self._set_field("design_round", float("nan"))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_round", r.stdout)

    def test_infinity_review_round_refused_cleanly_by_supervisor(self):
        self.init()
        self._set_field("review_round", float("inf"))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("review_round", r.stdout)

    def test_nan_design_round_refused_cleanly_by_second_script(self):
        self.init()
        self._set_field("design_round", float("nan"))
        r = subprocess.run([sys.executable, str(BIN / "validate_handsoff_status.py"), "--root", str(self.tmp)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("SHIP_FEATURE_STATUS_INVALID", r.stdout)
        self.assertIn("design_round", r.stdout)


class TestAcceptanceHashIgnoresCriteriaOrder(HandsoffTestCase):
    """Round 2 finding 3: acceptance_hash was sensitive to array order,
    not just content, so a harmless reordering falsely invalidated a
    valid deployment approval."""

    def test_reordering_two_criteria_does_not_change_the_hash(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        a = [{"id": "REQ-001", "state": "passing"}, {"id": "REQ-002", "state": "passing"}]
        b = [{"id": "REQ-002", "state": "passing"}, {"id": "REQ-001", "state": "passing"}]
        self.assertEqual(lib.acceptance_hash(a), lib.acceptance_hash(b))


class TestSecondValidatorSharesTheSameLogic(HandsoffTestCase):
    """validate_handsoff_status.py must agree with handsoff_supervisor.py
    validate, since both now import the same compute_errors()."""

    def test_both_validators_agree_when_invalid(self):
        self.init()
        self.set_criterion_state("failing", resolved=False)
        self.advance_to(5)
        s = self.read_status()
        s["phase_number"] = 6  # force an invalid on-disk state directly
        s["phase"] = "Checks & documentation"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r1 = run(["validate"], cwd=self.tmp)
        r2 = subprocess.run([sys.executable, str(BIN / "validate_handsoff_status.py"), "--root", str(self.tmp)],
                            capture_output=True, text=True, timeout=30)
        self.assertEqual(r1.returncode, 1)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("Phase 6", r1.stdout)
        self.assertIn("Phase 6", r2.stdout)


class TestEvidenceAndLiveGates(HandsoffTestCase):
    def test_free_text_cannot_impersonate_verification_evidence(self):
        self.init()
        a = self.read_acceptance()
        a["criteria"][0]["state"] = "passing"
        a["criteria"][0]["evidence"] = ["trust me"]
        self.write_acceptance(a)
        s = self.read_status()
        s["requirement_coverage"] = {"passing": 1, "failing": 0, "not_tested": 0,
                                     "blocked": 0, "original_symptom_resolved": True}
        s["phase_number"] = 6
        s["phase"] = "Checks & documentation"
        s["implemented_by"] = "impl"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("lacks valid checks evidence", r.stdout)

    def test_phase_8_requires_live_run_after_approval(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("successful live verification", r.stdout)

    def test_live_verification_works_when_explicit_approval_is_disabled(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("deployment_requires_explicit_approval = true",
                                                 "deployment_requires_explicit_approval = false"))
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        final = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)

    def test_verification_tail_deletion_is_detected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        (self.tmp / "handsoff-verifications.jsonl").write_text("")
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)

    def test_combined_policy_requires_both_automated_and_browser_evidence(self):
        self.init()
        changed = run(["criterion-update", "REQ-001", "--verification", "automated_and_browser"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        blocked = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("verified evidence", blocked.stdout)
        criterion = self.read_acceptance()["criteria"][0]
        self.assertEqual(criterion["state"], "not_tested",
                          "automated-only evidence must not mark a combined-policy criterion passing")
        browser = run(["record-evidence", "REQ-001", "--kind", "browser",
                       "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(browser.returncode, 0, browser.stdout + browser.stderr)
        approved = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

    def test_command_output_is_not_persisted_in_verification_ledger(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["printf HANDSOFF_%s SECRET"]', 1))
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "printf HANDSOFF_%s SECRET"], cwd=self.tmp).returncode, 0)
        result = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_SECRET", result.stdout)
        self.assertNotIn("HANDSOFF_SECRET", (self.tmp / "handsoff-verifications.jsonl").read_text())

    def test_automated_policy_rejects_manual_attestation(self):
        self.init()
        result = run(["record-evidence", "REQ-001", "--kind", "manual",
                      "--description", "Claimed pass", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not accept manual", result.stdout)

    def test_unrelated_configured_check_cannot_satisfy_a_criterion(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1))
        result = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exactly match", result.stdout)


class TestDerivedAndStrictState(HandsoffTestCase):
    def test_coverage_must_equal_criteria_projection(self):
        self.init()
        s = self.read_status()
        s["requirement_coverage"]["passing"] = 99
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match", r.stdout)

    def test_phase_label_and_progress_range_are_enforced(self):
        self.init()
        s = self.read_status()
        s["phase"] = "Made up"
        s["progress"] = 101
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("phase_number", r.stdout)
        self.assertIn("0 to 100", r.stdout)

    def test_acceptance_mutation_invalidates_review_and_approval(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        changed = run(["criterion-update", "REQ-001", "--requirement", "A newly scoped outcome"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        s = self.read_status()
        self.assertIsNone(s["review"])
        self.assertIsNone(s["deployment_approved"])


class TestAnchoredEventLog(HandsoffTestCase):
    def test_deleting_last_event_is_detected(self):
        self.init()
        self.advance_to(2)
        log = self.tmp / "handsoff-events.jsonl"
        lines = log.read_text().splitlines()
        log.write_text(lines[0] + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)

    def test_unlogged_status_edit_is_detected(self):
        self.init()
        status = self.read_status()
        status["summary"] = "quietly rewritten"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("status file does not match", r.stdout)

    def test_unlogged_acceptance_edit_is_detected(self):
        self.init()
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["requirement"] = "quietly rewritten"
        self.write_acceptance(acceptance)
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("acceptance file does not match", r.stdout)


class TestConfigValidation(HandsoffTestCase):
    def test_state_paths_cannot_escape_project_root(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('status_file = "handsoff-status.json"',
                                                 'status_file = "../escaped.json"'))
        r = run(["init", "unsafe"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("safe relative path", r.stdout)

    def test_commands_must_be_an_array(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = "true"', 1))
        r = run(["init", "bad config"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("array", r.stdout)


class TestVerifyIsolatesUnrelatedCriteria(HandsoffTestCase):
    """The real bug this pass found: `verify` used to run the WHOLE
    [checks].commands list and share one ok/evidence record across every
    named criterion. A criterion's own passing test could be dragged down
    by an unrelated command failing, and (in a batch call) two unrelated
    criteria's outcomes were conflated into one record. Each criterion
    must now be judged strictly by its own configured tests."""

    def _two_criteria(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "commands = []", 'commands = ["true", "false"]', 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp).returncode, 0)
        add = run(["criterion-add", "REQ-002", "--type", "supporting",
                   "--requirement", "A second, unrelated outcome.",
                   "--verification", "automated", "--test", "false"], cwd=self.tmp)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)

    def test_an_unrelated_failing_command_does_not_fail_a_passing_criterion(self):
        self._two_criteria()
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["criteria"]["REQ-001"]["ok"])
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")

    def test_batched_verify_gives_each_criterion_its_own_independent_outcome(self):
        self._two_criteria()
        r = run(["verify", "--criterion", "REQ-001", "--criterion", "REQ-002", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # overall: REQ-002 failed
        payload = json.loads(r.stdout)
        self.assertTrue(payload["criteria"]["REQ-001"]["ok"])
        self.assertFalse(payload["criteria"]["REQ-002"]["ok"])
        self.assertNotEqual(payload["criteria"]["REQ-001"]["run_id"], payload["criteria"]["REQ-002"]["run_id"])
        criteria = {c["id"]: c for c in self.read_acceptance()["criteria"]}
        self.assertEqual(criteria["REQ-001"]["state"], "passing")
        self.assertEqual(criteria["REQ-002"]["state"], "failing")

    def test_an_unrelated_passing_command_does_not_satisfy_a_different_criterion(self):
        self._two_criteria()
        # REQ-002's own test ("false") never gets run or referenced here;
        # verifying REQ-001 alone must not put ANY evidence on REQ-002.
        run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        req002 = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-002")
        self.assertEqual(req002["state"], "not_tested")
        self.assertEqual(req002["evidence"], [])


class TestLiveVerificationOrdering(HandsoffTestCase):
    """The live gate requires the live check to have run AFTER approval,
    not merely that both exist somewhere in the ledger."""

    def test_live_verification_before_approval_does_not_satisfy_phase_8(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 1, live.stdout + live.stderr)
        self.assertIn("Phase 7", live.stdout + live.stderr)


class TestLiveAndApprovalConfigCombinations(HandsoffTestCase):
    """Every combination of require_live_verification and
    deployment_requires_explicit_approval must reach Phase 8; disabling a
    gate must not make the workflow impossible to finish."""

    def _disable(self, *flags):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        for flag in flags:
            text = text.replace(f"{flag} = true", f"{flag} = false")
        toml.write_text(text)

    def test_both_gates_disabled_reaches_phase_8(self):
        self._disable("require_live_verification", "deployment_requires_explicit_approval")
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)

    def test_approval_required_but_live_disabled_reaches_phase_8(self):
        self._disable("require_live_verification")
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestCheckTimeout(HandsoffTestCase):
    def test_a_hanging_check_times_out_cleanly_rather_than_hanging(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["sleep 5"]', 1)
                        .replace("timeout_seconds = 600", "timeout_seconds = 1", 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "sleep 5"], cwd=self.tmp).returncode, 0)
        started = __import__("time").time()
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        elapsed = __import__("time").time() - started
        self.assertLess(elapsed, 10, "the 1-second configured timeout should have fired well before 10s")
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["results"][0]["exit_code"], 124)

    def test_timeout_seconds_must_be_a_positive_integer(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("timeout_seconds = 600", "timeout_seconds = -1", 1))
        r = run(["init", "bad timeout"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timeout_seconds", r.stdout)


class TestPartialVerificationLedgerTailDeletion(HandsoffTestCase):
    """The earlier test only covered truncating the WHOLE verification
    ledger to empty. Deleting just the last record (leaving an internally
    consistent but short chain) must be caught too, the same way a
    deleted-tail event log record is."""

    def test_deleting_the_last_verification_record_is_detected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        log = self.tmp / "handsoff-verifications.jsonl"
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1)
        log.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("anchored head", r.stdout)


class TestHalfCompletedCrossFileWrite(HandsoffTestCase):
    """Simulates a crash between the acceptance write and the status write
    inside a mutating command: acceptance now shows a passing criterion,
    but status's derived coverage and the event log both still reflect the
    old, pre-write world. This must be reported, not silently accepted."""

    def test_acceptance_ahead_of_status_is_detected(self):
        self.init()
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["state"] = "passing"
        self.write_acceptance(acceptance)  # acceptance.json changed...
        # ...but handsoff-status.json and the event log were NOT touched,
        # exactly the shape of a crash between two of a command's writes.
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(
            any("does not match" in e for e in json.loads(
                subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "status"],
                               cwd=self.tmp, capture_output=True, text=True, timeout=30).stdout
            ).get("errors", [])),
            "expected a coverage or audit mismatch to be reported")


class TestIdentityAndRecordTypeValidation(HandsoffTestCase):
    """Requirement: strictly validate identities, review records, and
    approval records, not just the coverage counters and phase fields."""

    def test_non_string_implemented_by_is_rejected(self):
        self.init()
        s = self.read_status()
        s["implemented_by"] = 12345
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("implemented_by", r.stdout)

    def test_review_record_missing_by_is_rejected(self):
        self.init()
        s = self.read_status()
        s["review"] = {"at": "2026-01-01T00:00:00+00:00", "acceptance_hash": "x"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("review.by", r.stdout)

    def test_deployment_approved_with_naive_timestamp_is_rejected(self):
        self.init()
        s = self.read_status()
        s["deployment_approved"] = {"by": "owner", "at": "2026-01-01T00:00:00", "acceptance_hash": "x"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timezone", r.stdout)


class TestNextActionStaysCurrent(HandsoffTestCase):
    """Found during the end-to-end run: next_action was set once at init
    and never touched again, so a completed Phase 8 project still read
    'reproduce the original symptom', a self-contradictory status."""

    def test_advance_updates_next_action_to_a_phase_appropriate_default(self):
        self.init()
        before = self.read_status()["next_action"]
        self.set_criterion_state("passing", resolved=True)
        run(["advance", "2", "20"], cwd=self.tmp)
        after = self.read_status()["next_action"]
        self.assertNotEqual(before, after)
        self.assertIn("design", after.lower())

    def test_next_action_can_be_overridden_explicitly(self):
        self.init()
        run(["advance", "2", "20", "--next-action", "Custom next step"], cwd=self.tmp)
        self.assertEqual(self.read_status()["next_action"], "Custom next step")

    def test_a_completed_workflow_does_not_still_say_reproduce_the_symptom(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(self.read_status()["phase_number"], 8)
        self.assertNotIn("reproduce the original symptom", self.read_status()["next_action"])


class TestEmptyIdentitiesAndFeaturesRejected(HandsoffTestCase):
    """Round 3 finding 1: an empty string is a valid str, so a naive
    `if not args.by:` style check (or none at all) let '' through as a
    real actor or feature name. init "" also used to write a permanently
    unrecoverable project, since a later real init then found existing
    artifacts and refused."""

    def test_init_with_empty_feature_is_rejected(self):
        r = run(["init", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())
        retry = run(["init", "a real feature"], cwd=self.tmp)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)

    def test_init_with_whitespace_only_feature_is_rejected(self):
        r = run(["init", "   "], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_verify_with_empty_by_is_rejected(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        r = run(["verify", "--criterion", "REQ-001", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_evidence_with_empty_by_is_rejected(self):
        self.init()
        r = run(["record-evidence", "REQ-001", "--kind", "manual", "--description", "checked", "--by", ""],
               cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_evidence_with_empty_description_is_rejected(self):
        self.init()
        r = run(["record-evidence", "REQ-001", "--kind", "manual", "--description", "", "--by", "someone"],
               cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_record_review_with_empty_by_is_rejected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        r = run(["record-review", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_verify_live_with_empty_by_is_rejected(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        r = run(["verify-live", "--by", ""], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-empty", r.stdout)

    def test_hand_crafted_verification_record_with_empty_actor_is_detected(self):
        """A record can chain and hash perfectly while still being
        structurally empty; cryptographic authentication alone would
        wave it through. It must be caught on load, not just on write."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        log = lib.verification_log_path(self.tmp, cfg)
        prev_hash = lib._last_hash(log)
        body = {"run_id": "vr-forged", "at": "2026-01-01T00:00:00+00:00", "kind": "manual",
               "ok": True, "by": "", "criteria": ["REQ-001"],
               "criterion_hashes": {}, "results": [], "description": "forged",
               "acceptance_hash": None, "config_hash": None, "prev_hash": prev_hash}
        body["hash"] = __import__("hashlib").sha256(
            (lib._canonical(body) + prev_hash).encode("utf-8")).hexdigest()
        with log.open("a", encoding="utf-8") as fh:
            fh.write(lib._canonical(body) + "\n")
        r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("'by' must be a non-empty string", r.stdout)


class TestConfigurationChainOfTrust(HandsoffTestCase):
    """Round 3 finding 2: handsoff.toml sat outside the chain of trust.
    Review, deployment approval, and live verification are each bound to
    a hash of the governance-relevant config keys at the moment they were
    granted; changing those keys afterward must invalidate the decision,
    the same way changing the acceptance registry already did."""

    def _flip_a_governance_key(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("max_review_rounds = 3", "max_review_rounds = 7"))

    def test_review_is_invalidated_when_governance_config_changes_after_review(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(5, implemented_by="impl")
        review = run(["record-review", "--by", "reviewer"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        ok = run(["advance", "6", "60"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self._flip_a_governance_key()
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed since review", r.stdout)

    def test_deployment_and_live_decisions_are_invalidated_when_governance_config_changes(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        final = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        self._flip_a_governance_key()
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed", r.stdout)

    def test_verify_live_refuses_when_config_changed_before_it_was_even_invoked(self):
        """Config already stale by the time verify-live starts: caught
        by the FIRST comparison, against the approval's own config_hash.
        This does not exercise the mid-run race (see the isolated test
        below); a fresh process reloads handsoff.toml at startup either
        way, so this case was never at risk."""
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]')
                        .replace("live_commands = []", 'live_commands = ["true"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self._flip_a_governance_key()
        r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("workflow policy changed since deployment approval", r.stdout)

    def test_verify_live_detects_a_config_change_that_lands_while_the_check_is_running(self):
        """Round 3 review finding: the SECOND comparison used to hash
        the same in-memory `cfg` object against itself, so it could
        never detect a real race no matter what changed on disk. Here
        the live check itself edits handsoff.toml as a side effect,
        landing the change in the window between the two lock
        acquisitions inside a SINGLE verify-live invocation, the one
        shape the old code could not see."""
        flipper = self.tmp / "flip_config.py"
        flipper.write_text(
            "from pathlib import Path\n"
            "p = Path('handsoff.toml')\n"
            "p.write_text(p.read_text().replace('max_review_rounds = 3', 'max_review_rounds = 9'))\n"
        )
        toml = self.tmp / "handsoff.toml"
        # "commands = []" is a substring of "live_commands = []" too, so
        # the first replace must be count-limited or it also clobbers
        # the live_commands line before the second replace ever sees it.
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1)
                        .replace("live_commands = []", f'live_commands = ["{sys.executable} flip_config.py"]'))
        self.init()
        run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("workflow policy changed during live verification", r.stdout)


class TestSymlinkPathContainment(HandsoffTestCase):
    """Round 3 finding 3: the old check only rejected literal '..' and
    absolute paths in a configured state-file path. A relative path
    through a symlinked directory passed that check yet still resolved
    outside the project root."""

    def test_status_file_cannot_escape_root_via_a_symlinked_directory(self):
        outside = Path(tempfile.mkdtemp(prefix="handsoff-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = self.tmp / "escape"
        os.symlink(outside, link, target_is_directory=True)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            'status_file = "handsoff-status.json"', 'status_file = "escape/status.json"'))
        r = run(["init", "symlink escape attempt"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("resolves outside", r.stdout)
        self.assertFalse((outside / "status.json").exists())


class TestCombinedPolicyEvidenceCompletion(HandsoffTestCase):
    """Round 3 finding 4 (medium): a combined automated_and_browser
    criterion used to flip to 'passing' the moment EITHER evidence kind
    landed. Passing must only be derived once every required kind for
    the criterion's policy has a valid, current record."""

    def test_browser_only_evidence_does_not_mark_a_combined_criterion_passing(self):
        self.init()
        self.assertEqual(
            run(["criterion-update", "REQ-001", "--verification", "automated_and_browser"], cwd=self.tmp).returncode, 0)
        r = run(["record-evidence", "REQ-001", "--kind", "browser",
                "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "not_tested")

    def test_automated_only_evidence_does_not_mark_a_combined_criterion_passing(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--verification", "automated_and_browser",
                              "--test", "true"], cwd=self.tmp).returncode, 0)
        r = run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "not_tested")

    def test_criterion_becomes_passing_only_once_both_kinds_are_present(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--verification", "automated_and_browser",
                              "--test", "true"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify", "--criterion", "REQ-001", "--by", "runner"], cwd=self.tmp).returncode, 0)
        r = run(["record-evidence", "REQ-001", "--kind", "browser",
                "--description", "Observed corrected UI", "--by", "browser-runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")


class TestConfiguredCheckOrderPreserved(HandsoffTestCase):
    """Smaller improvement 1: verify used to alphabetize the set of
    needed test commands, silently reordering them relative to
    [checks].commands. Configured order carries operator intent (e.g.
    cheap smoke checks before a slow suite) and must be preserved."""

    def test_verify_runs_checks_in_configured_order_not_alphabetical(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "commands = []", 'commands = ["echo zebra", "echo apple"]', 1))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "echo zebra"], cwd=self.tmp).returncode, 0)
        add = run(["criterion-add", "REQ-002", "--type", "supporting",
                  "--requirement", "A second outcome.", "--verification", "automated",
                  "--test", "echo apple"], cwd=self.tmp)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        r = run(["verify", "--criterion", "REQ-001", "--criterion", "REQ-002", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        commands_run = [result["command"] for result in json.loads(r.stdout)["results"]]
        self.assertEqual(commands_run, ["echo zebra", "echo apple"],
                         "checks must run in [checks].commands order, not alphabetically sorted")


class TestDoctorRecoveryCommand(HandsoffTestCase):
    """Smaller improvement 2: the README documented that an interrupted
    cross-file write fails closed but offered no supported way to
    recover. `doctor` closes that gap for two specific, provably safe
    shapes: a stale verification_head anchor (self-authenticating from
    the ledger), and an event log that has not yet recorded a
    write-ahead-journal-confirmed status/acceptance write. A bare hand
    edit that merely happens to still validate is NOT one of those
    shapes and must be refused, even though it looks identical on disk
    to a real interrupted write (round 3 finding: doctor could otherwise
    launder an untracked edit into the audit trail as if it were a
    crash)."""

    def test_doctor_reports_ok_when_nothing_to_recover(self):
        self.init()
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DOCTOR_OK", r.stdout)

    def test_doctor_recovers_a_verification_head_left_behind_by_a_crash(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        criterion = self.read_acceptance()["criteria"][0]
        with lib.project_lock(self.tmp):
            # Ledger append succeeds and is durable; the status.json write
            # that would re-anchor verification_head to it never happens,
            # exactly the shape of a crash between the two.
            lib.append_verification(self.tmp, cfg, kind="manual", ok=True, by="side-channel",
                                    criteria=[criterion], description="out of band")
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("anchored head", broken.stdout)
        dry = run(["doctor", "--dry-run"], cwd=self.tmp)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("WOULD_RECOVER", dry.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 1, "dry run must not write anything")
        fixed = run(["doctor"], cwd=self.tmp)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn("RECOVERED", fixed.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_doctor_recovers_a_journal_confirmed_write_left_behind_by_a_crash(self):
        """The genuine recoverable shape: a real command's write-ahead
        journal entry proves the current status content was its
        intended, in-flight output; only the append_event step after it
        never happened. This reproduces exactly what commit() does up to
        (not including) that last step."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["next_action"] = "Advanced by a command that crashed after writing files"
        with lib.project_lock(self.tmp):
            lib.write_ahead(self.tmp, status=status)
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("does not match the state recorded by the latest event", broken.stdout)
        fixed = run(["doctor"], cwd=self.tmp)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn("RECOVERED", fixed.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["next_action"],
                         "Advanced by a command that crashed after writing files")

    def test_doctor_refuses_to_recover_an_unproven_hand_edit(self):
        """The critical case: an edit made directly to status.json, with
        no command and no write-ahead journal entry behind it, must not
        be recovered just because it happens to still validate. A crash
        and a hand edit are indistinguishable on disk; only the journal
        tells them apart, and here there isn't one."""
        self.init()
        status = self.read_status()
        status["next_action"] = "Hand-edited directly, no command ever ran"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        broken = run(["validate"], cwd=self.tmp)
        self.assertEqual(broken.returncode, 1)
        self.assertIn("does not match the state recorded by the latest event", broken.stdout)
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)
        self.assertIn("write-ahead journal", r.stdout)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 1,
                         "a refused recovery must not have written anything")
        self.assertEqual(self.read_status()["next_action"], "Hand-edited directly, no command ever ran")

    def test_doctor_refuses_when_the_event_log_chain_is_broken(self):
        self.init()
        log = self.tmp / "handsoff-events.jsonl"
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        log.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""))
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)

    def test_doctor_refuses_to_paper_over_an_independently_invalid_state(self):
        self.init()
        status = self.read_status()
        status["progress"] = 150  # invalid regardless of ledger freshness
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)

    def test_doctor_refuses_a_journal_confirmed_write_that_still_does_not_validate(self):
        """Even with a genuine write-ahead journal entry proving intent,
        doctor must not anchor the log to a state that fails its own
        gates: the journal proves a write was in flight, not that its
        content was legitimate."""
        self.init()
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["progress"] = 150
        with lib.project_lock(self.tmp):
            lib.write_ahead(self.tmp, status=status)
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
        r = run(["doctor"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CANNOT_RECOVER", r.stdout)
        self.assertIn("does not independently validate", r.stdout)


class TestRunArchive(HandsoffTestCase):
    """Landing Phase 8 complete writes one self-contained JSON record to a
    centralized archive outside any project repo, so what Handsoff learns
    about ITSELF survives that repo's own cleanup and can be read across
    every project it has ever run in. Automatic, not a step a Supervisor
    session has to remember."""

    def setUp(self):
        super().setUp()
        self.archive_dir = Path(tempfile.mkdtemp(prefix="handsoff-archive-test-"))
        self._old_env = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.archive_dir)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self._old_env
        shutil.rmtree(self.archive_dir, ignore_errors=True)
        super().tearDown()

    def _complete_a_run(self, feature="Archive test feature"):
        self.init(feature)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        r = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_completing_a_run_writes_exactly_one_archive_file(self):
        self._complete_a_run()
        files = list(self.archive_dir.glob("*.json"))
        self.assertEqual(len(files), 1, files)

    def test_the_archive_reports_its_own_path(self):
        r = self._complete_a_run()
        self.assertIn("HANDSOFF_ARCHIVED:", r.stdout)

    def test_the_archived_record_is_a_faithful_self_contained_copy(self):
        self._complete_a_run(feature="Archive content test")
        record = json.loads(next(self.archive_dir.glob("*.json")).read_text())
        self.assertEqual(record["repo"], self.tmp.name)
        self.assertEqual(record["feature"], "Archive content test")
        self.assertEqual(record["status"]["phase_number"], 8)
        self.assertEqual(record["status"]["status"], "complete")
        self.assertTrue(record["acceptance"]["criteria"])
        self.assertTrue(record["verifications"])
        self.assertTrue(record["events"])
        self.assertIn("archived_at", record)

    def test_archiving_only_happens_at_completion_not_every_phase(self):
        self.init()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(4, implemented_by="impl-1")
        self.assertEqual(list(self.archive_dir.glob("*.json")), [])

    def test_archive_dir_is_created_if_missing(self):
        shutil.rmtree(self.archive_dir)
        self._complete_a_run()
        self.assertTrue(self.archive_dir.is_dir())
        self.assertEqual(len(list(self.archive_dir.glob("*.json"))), 1)

    def test_two_different_repos_archive_to_the_same_place_without_colliding(self):
        self._complete_a_run(feature="First repo run")
        other = Path(tempfile.mkdtemp(prefix="handsoff-test-other-"))
        try:
            for name in ("handsoff.toml",):
                shutil.copy(ROOT / name, other / name)
            shutil.copytree(ROOT / "schemas", other / "schemas")
            real_tmp, self.tmp = self.tmp, other
            self._complete_a_run(feature="Second repo run")
            self.tmp = real_tmp
        finally:
            shutil.rmtree(other, ignore_errors=True)
        self.assertEqual(len(list(self.archive_dir.glob("*.json"))), 2)


class TestRunOwnedDashboardRelease(HandsoffTestCase):
    """#40: a dashboard launched with --owned-by-run is released when the
    run lands Phase 8 complete, so the port is free for the next run. The
    pointer file is never the authority: the server proves ownership from
    its own in-memory run_token and root hash, the caller computes the
    root hash from its own resolved root, and no process is ever signalled
    by PID or port. Every server here binds an ephemeral port in a temp
    root; the machine's real dashboard is never touched."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dashboard
        import handsoff_lib as lib
        import handsoff_supervisor as supervisor
        self.dashboard = dashboard
        self.lib = lib
        self.supervisor = supervisor
        self._cleanup = []

    def tearDown(self):
        for action in reversed(self._cleanup):
            try:
                action()
            except Exception:  # noqa: BLE001
                pass
        super().tearDown()

    # -- helpers ---------------------------------------------------------

    def _start_owned(self, root):
        """Run the real serve() path (owner file write, server_close, token
        conditional file removal) in a thread on an ephemeral port."""
        created = []
        original = self.dashboard.DashboardServer

        def factory(*args, **kwargs):
            server = original(*args, **kwargs)
            created.append(server)
            return server

        patcher = mock.patch.object(self.dashboard, "DashboardServer", factory)
        patcher.start()
        thread = threading.Thread(
            target=self.dashboard.serve, args=(root,),
            kwargs={"port": 0, "open_browser": False, "owned_by_run": True}, daemon=True,
        )
        thread.start()
        owner = self.lib.dashboard_owner_path(root)
        for _ in range(100):
            if owner.exists() and created:
                break
            time.sleep(0.05)
        patcher.stop()
        self.assertTrue(owner.exists(), "serve(owned_by_run=True) must write the owner file")
        record = json.loads(owner.read_text())
        server = created[0]
        self._cleanup.append(lambda: (server.shutdown(), server.server_close()))
        return thread, server, record

    def _start_plain(self, root, **kwargs):
        """A server without the flag (a manual launch or the LaunchAgent),
        or an owned one built directly when the test needs its values."""
        server = self.dashboard.DashboardServer(("127.0.0.1", 0), root, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        self._cleanup.append(lambda: (server.shutdown(), server.server_close()))
        return server, thread, server.server_address[1]

    def _write_owner(self, root, port, token, **extra):
        record = {"pid": os.getpid(), "host": "127.0.0.1", "port": port,
                  "started_at": datetime.now(timezone.utc).isoformat(), "owner": "ship-feature",
                  "run_token": token, "root_sha256": self.lib.dashboard_root_sha256(root),
                  "feature": None, **extra}
        self.lib.dashboard_owner_path(root).write_text(json.dumps(record))
        return record

    def _get_json(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def _post_shutdown(self, port, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("POST", "/api/shutdown", body=json.dumps(payload),
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def _port_accepts(self, port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    def _open_sse(self, port):
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        stream.request("GET", "/api/events")
        response = stream.getresponse()
        self.assertEqual(response.status, 200)
        while response.readline() not in (b"\n", b"\r\n", b""):
            pass
        self._cleanup.append(stream.close)
        return response

    def _event_kinds(self, root=None):
        path = (root or self.tmp) / "handsoff-events.jsonl"
        return [json.loads(line)["kind"] for line in path.read_text().splitlines() if line.strip()]

    def _events_of_kind(self, kind):
        path = self.tmp / "handsoff-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip() and json.loads(line).get("kind") == kind]

    def _reach_completion_gate(self):
        """Everything up to the final `advance 8 100`. set_criterion_state
        also configures live_commands (its replace covers both keys)."""
        self.init("Run-owned dashboard release")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)

    # -- completion path -------------------------------------------------

    def test_completion_releases_the_owned_dashboard_and_frees_the_port(self):
        self._reach_completion_gate()
        thread, server, record = self._start_owned(self.tmp)
        port = record["port"]
        self.assertEqual(record["owner"], "ship-feature")
        self.assertEqual(record["root_sha256"], self.lib.dashboard_root_sha256(self.tmp))
        self.assertEqual(record["feature"], "Run-owned dashboard release")
        self.assertEqual(set(record), {"pid", "host", "port", "started_at", "owner",
                                       "run_token", "root_sha256", "feature"})
        stream = self._open_sse(port)

        started = time.monotonic()
        completed = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("SHIP_FEATURE_ADVANCED", completed.stdout)
        self.assertIn(f"HANDSOFF_DASHBOARD_RELEASED: port {port}", completed.stdout)

        # The open SSE client is closed (EOF, not a timeout) inside the
        # shutdown window, and the serve() thread itself finishes.
        self.assertEqual(stream.read(), b"")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "serve() must return once the run releases it")
        self.assertLess(time.monotonic() - started, 20.0)
        self.assertTrue(server.stopping)
        self.assertFalse(self._port_accepts(port), "the listening socket must be closed")

        # A new bind to the same port succeeds immediately, the way the
        # next run's dashboard would bind it.
        rebound = self.dashboard.DashboardServer(("127.0.0.1", port), self.tmp)
        rebound.server_close()

        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())
        released = self._events_of_kind("dashboard_released")
        self.assertEqual(len(released), 1, self._event_kinds())
        self.assertEqual(released[0]["port"], port)
        self.assertEqual(released[0]["pid"], record["pid"])
        self.assertEqual(self.read_status()["status"], "complete")
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_repeated_release_after_completion_is_a_recorded_no_op(self):
        self._reach_completion_gate()
        thread, server, record = self._start_owned(self.tmp)
        completed = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        thread.join(timeout=5)
        self.assertEqual(self._event_kinds().count("dashboard_released"), 1)

        # A repeated completion notification finds no owner file: a no-op
        # with the skipped reason, logged, and still not an error.
        again = self.lib.release_run_dashboard(self.tmp)
        self.assertEqual(again, {"released": False, "reason": "no run-owned dashboard"})
        cfg = self.lib.load_config(self.tmp)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.supervisor._release_run_dashboard(self.tmp, cfg)
        self.assertIn("HANDSOFF_DASHBOARD_RELEASE_SKIPPED: no run-owned dashboard", out.getvalue())
        kinds = self._event_kinds()
        self.assertEqual(kinds.count("dashboard_released"), 1)
        self.assertEqual(kinds.count("dashboard_release_skipped"), 1)
        skipped = self._events_of_kind("dashboard_release_skipped")[0]
        self.assertEqual(skipped["reason"], "no run-owned dashboard")
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0, "event-only commits stay chained")

    def test_completion_without_an_owned_dashboard_records_skipped_and_still_completes(self):
        self._reach_completion_gate()
        server, thread, port = self._start_plain(self.tmp)
        completed = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("HANDSOFF_DASHBOARD_RELEASE_SKIPPED: no run-owned dashboard", completed.stdout)
        self.assertEqual(self.read_status()["status"], "complete")
        self.assertEqual(self._event_kinds().count("dashboard_release_skipped"), 1)
        self.assertNotIn("dashboard_released", self._event_kinds())
        self.assertEqual(self._get_json(port, "/healthz")[0], 200, "a manual dashboard keeps serving")

    # -- manual isolation ------------------------------------------------

    def test_manual_launch_writes_no_owner_file_and_cannot_be_targeted(self):
        created = []
        original = self.dashboard.DashboardServer

        def factory(*args, **kwargs):
            server = original(*args, **kwargs)
            created.append(server)
            return server

        with mock.patch.object(self.dashboard, "DashboardServer", factory):
            thread = threading.Thread(target=self.dashboard.serve, args=(self.tmp,),
                                      kwargs={"port": 0, "open_browser": False}, daemon=True)
            thread.start()
            for _ in range(100):
                if created:
                    break
                time.sleep(0.05)
        server = created[0]
        self._cleanup.append(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]
        time.sleep(0.2)
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())
        self.assertEqual(self._get_json(port, "/api/ownership"), (200, {"owned": False}))
        status, body = self._post_shutdown(port, {"run_token": "x" * 32,
                                                  "root_sha256": self.lib.dashboard_root_sha256(self.tmp)})
        self.assertEqual(status, 403, body)
        self.assertTrue(thread.is_alive())
        self.assertEqual(self._get_json(port, "/healthz")[0], 200)

    def test_unowned_server_is_untouched_and_foreign_token_file_is_removed(self):
        server, thread, port = self._start_plain(self.tmp)
        self._write_owner(self.tmp, port, self.lib.new_dashboard_run_token())
        result = self.lib.release_run_dashboard(self.tmp)
        self.assertFalse(result["released"])
        self.assertEqual(result["reason"], "stale ownership metadata removed: server is not run-owned")
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())
        self.assertFalse(server.stopping)
        self.assertTrue(thread.is_alive())
        self.assertEqual(self._get_json(port, "/healthz")[0], 200)

        # An owned server whose token differs from the file: same outcome.
        token = self.lib.new_dashboard_run_token()
        owned, owned_thread, owned_port = self._start_plain(
            self.tmp, run_token=token, root_sha256=self.lib.dashboard_root_sha256(self.tmp))
        self._write_owner(self.tmp, owned_port, self.lib.new_dashboard_run_token())
        result = self.lib.release_run_dashboard(self.tmp)
        self.assertEqual(result, {"released": False,
                                  "reason": "stale ownership metadata removed: run_token mismatch"})
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())
        self.assertFalse(owned.stopping)
        self.assertTrue(owned_thread.is_alive())
        self.assertEqual(self._get_json(owned_port, "/api/ownership")[1]["run_token"], token)

    def test_hand_edited_file_pointing_at_another_root_leaves_that_server_running(self):
        root_b = Path(tempfile.mkdtemp(prefix="handsoff-test-root-b-"))
        self._cleanup.append(lambda: shutil.rmtree(root_b, ignore_errors=True))
        thread_b, server_b, record_b = self._start_owned(root_b)
        # Root A's file names root B's port and token, and even copies B's
        # root hash: the caller computes its own and never trusts the file.
        self._write_owner(self.tmp, record_b["port"], record_b["run_token"],
                          root_sha256=record_b["root_sha256"])
        result = self.lib.release_run_dashboard(self.tmp)
        self.assertEqual(result, {"released": False,
                                  "reason": "stale ownership metadata removed: root_sha256 mismatch"})
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists(), "only root A's stale file goes")
        self.assertTrue(self.lib.dashboard_owner_path(root_b).exists())
        self.assertFalse(server_b.stopping)
        self.assertTrue(thread_b.is_alive())
        status, ownership = self._get_json(record_b["port"], "/api/ownership")
        self.assertEqual((status, ownership["owned"], ownership["run_token"]), (200, True, record_b["run_token"]))
        # Root B releases its own server normally afterwards.
        self.assertTrue(self.lib.release_run_dashboard(root_b)["released"])
        thread_b.join(timeout=5)
        self.assertFalse(thread_b.is_alive())

    def test_two_roots_release_only_their_own_server(self):
        root_b = Path(tempfile.mkdtemp(prefix="handsoff-test-root-b-"))
        self._cleanup.append(lambda: shutil.rmtree(root_b, ignore_errors=True))
        thread_a, server_a, record_a = self._start_owned(self.tmp)
        thread_b, server_b, record_b = self._start_owned(root_b)
        self.assertNotEqual(record_a["run_token"], record_b["run_token"])
        self.assertNotEqual(record_a["root_sha256"], record_b["root_sha256"])

        result = self.lib.release_run_dashboard(self.tmp)
        self.assertEqual((result["released"], result["port"]), (True, record_a["port"]))
        thread_a.join(timeout=5)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(self._port_accepts(record_a["port"]))
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())

        self.assertTrue(thread_b.is_alive())
        self.assertFalse(server_b.stopping)
        self.assertTrue(self._port_accepts(record_b["port"]))
        self.assertTrue(self.lib.dashboard_owner_path(root_b).exists())
        self.assertEqual(self._get_json(record_b["port"], "/healthz")[0], 200)

        result = self.lib.release_run_dashboard(root_b)
        self.assertEqual((result["released"], result["port"]), (True, record_b["port"]))
        thread_b.join(timeout=5)
        self.assertFalse(thread_b.is_alive())
        self.assertFalse(self.lib.dashboard_owner_path(root_b).exists())

    def test_shutdown_endpoint_requires_both_bindings_and_no_browser_origin(self):
        token = self.lib.new_dashboard_run_token()
        root_hash = self.lib.dashboard_root_sha256(self.tmp)
        server, thread, port = self._start_plain(self.tmp, run_token=token, root_sha256=root_hash)
        self.assertEqual(self._get_json(port, "/api/ownership"),
                         (200, {"owned": True, "run_token": token, "root_sha256": root_hash}))
        for payload in (
            {"run_token": token},
            {"root_sha256": root_hash},
            {"run_token": token, "root_sha256": "0" * 64},
            {"run_token": self.lib.new_dashboard_run_token(), "root_sha256": root_hash},
            {"run_token": None, "root_sha256": root_hash},
        ):
            status, body = self._post_shutdown(port, payload)
            self.assertEqual(status, 403, (payload, body))
            self.assertFalse(server.stopping)
        self.assertTrue(thread.is_alive())
        # The correct pair, with no Origin header at all (a CLI caller).
        status, body = self._post_shutdown(port, {"run_token": token, "root_sha256": root_hash})
        self.assertEqual((status, body["ok"], body["stopping"]), (200, True, True))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.stopping)

    # -- stale metadata and the server's own exit ------------------------

    def test_dead_port_stale_file_is_removed_without_touching_anything(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self._write_owner(self.tmp, dead_port, self.lib.new_dashboard_run_token(), pid=2 ** 22 + 7)
        result = self.lib.release_run_dashboard(self.tmp)
        self.assertFalse(result["released"])
        self.assertTrue(result["reason"].startswith("stale ownership metadata removed: connection failed"),
                        result)
        self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists())

        for malformed in ("not json", "[]", json.dumps({"port": "8765", "run_token": "t"}),
                          json.dumps({"port": dead_port})):
            self.lib.dashboard_owner_path(self.tmp).write_text(malformed)
            result = self.lib.release_run_dashboard(self.tmp)
            self.assertFalse(result["released"], malformed)
            self.assertTrue(result["reason"].startswith("stale ownership metadata removed:"), result)
            self.assertFalse(self.lib.dashboard_owner_path(self.tmp).exists(), malformed)
        self.assertEqual(self.lib.release_run_dashboard(self.tmp),
                         {"released": False, "reason": "no run-owned dashboard"})

    def test_release_never_signals_a_pid(self):
        server, thread, port = self._start_plain(self.tmp)
        self._write_owner(self.tmp, port, self.lib.new_dashboard_run_token(), pid=os.getpid())
        with mock.patch("os.kill") as kill, mock.patch("os.killpg", create=True) as killpg:
            self.assertFalse(self.lib.release_run_dashboard(self.tmp)["released"])
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertTrue(thread.is_alive())
        self.assertEqual(self._get_json(port, "/healthz")[0], 200)

    def test_server_exit_removes_the_owner_file_only_while_it_carries_its_own_token(self):
        thread, server, record = self._start_owned(self.tmp)
        owner = self.lib.dashboard_owner_path(self.tmp)
        # A newer server on the same root has taken over the pointer file.
        newer = dict(record, run_token=self.lib.new_dashboard_run_token(), port=record["port"] + 1)
        owner.write_text(json.dumps(newer))
        status, body = self._post_shutdown(record["port"], {"run_token": record["run_token"],
                                                            "root_sha256": record["root_sha256"]})
        self.assertEqual(status, 200, body)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self._port_accepts(record["port"]))
        self.assertTrue(owner.exists(), "a file with a different token belongs to the newer server")
        self.assertEqual(json.loads(owner.read_text()), newer)

        # And with its own token still on the file, exit does remove it.
        thread, server, record = self._start_owned(self.tmp)
        self.assertEqual(json.loads(owner.read_text())["run_token"], record["run_token"])
        status, body = self._post_shutdown(record["port"], {"run_token": record["run_token"],
                                                            "root_sha256": record["root_sha256"]})
        self.assertEqual(status, 200, body)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(owner.exists())
        self.assertFalse(self.lib.remove_dashboard_owner_if_token(self.tmp, record["run_token"]),
                         "removal is idempotent")


class TestActivityAwareStallDetection(HandsoffTestCase):
    """The ir-command B3 dogfooding bug: a run doing hours of legitimate
    background work (a long design review, which had even scheduled its
    own fallback heartbeat) was flagged 'stalled' because stall detection
    read only the status file's updated_at mtime. Fix: stall_warning()
    now reads the FRESHEST of updated_at and a new, optional
    last_heartbeat_at field, written by a new `heartbeat` command; a
    fresh heartbeat suppresses a false stall, while a run with neither
    signal current is still correctly flagged. See README "Known
    limitations"."""

    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _ago(self, minutes, delta=timedelta()):
        return (self.NOW - timedelta(minutes=minutes) - delta).isoformat()

    def _real_ago(self, minutes):
        """For tests that go through the real CLI (which always reads the
        real wall clock, not an injectable `now`): a genuinely past
        timestamp relative to the actual current time, not a sleep."""
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def _write_status(self, status):
        """Write a status dict directly (to simulate time having passed
        without a real advance/heartbeat call) and re-anchor the event log
        to it exactly the way commit() would, so the write reads as
        legitimate rather than tripping the (unrelated) tamper detector --
        this test simulates a normal passage of time, not a corrupted
        file, so it must not exercise that separate guarantee."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted status timestamps directly")

    def test_fresh_heartbeat_suppresses_stall_warning(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = {"stall_minutes": 10}
        status = {"status": "in_progress", "updated_at": self._ago(260), "last_heartbeat_at": self._ago(2)}
        self.assertIsNone(lib.stall_warning(status, cfg, now=self.NOW))
        note = lib.activity_note(status, cfg, now=self.NOW)
        self.assertIsNotNone(note)
        self.assertIn("background task", note)

    def test_no_heartbeat_and_stale_status_still_stalled(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = {"stall_minutes": 10}
        # No heartbeat field at all.
        no_heartbeat = {"status": "in_progress", "updated_at": self._ago(15)}
        self.assertIsNotNone(lib.stall_warning(no_heartbeat, cfg, now=self.NOW))
        self.assertIsNone(lib.activity_note(no_heartbeat, cfg, now=self.NOW))
        # Heartbeat present but itself stale: still a real stall.
        stale_heartbeat = {"status": "in_progress", "updated_at": self._ago(15), "last_heartbeat_at": self._ago(20)}
        self.assertIsNotNone(lib.stall_warning(stale_heartbeat, cfg, now=self.NOW))
        # Boundary: exactly stall_minutes old still counts as fresh (the
        # existing strict '>' comparison, not '>='), so this must NOT stall.
        at_boundary = {"status": "in_progress", "updated_at": self._ago(10)}
        self.assertIsNone(lib.stall_warning(at_boundary, cfg, now=self.NOW))
        # One second past the boundary must stall.
        past_boundary = {"status": "in_progress", "updated_at": self._ago(10, delta=timedelta(seconds=1))}
        self.assertIsNotNone(lib.stall_warning(past_boundary, cfg, now=self.NOW))

    def test_stall_and_busy_state_never_block_advance_or_status(self):
        self.init()
        # Genuinely stalled: no heartbeat, updated_at stale.
        s = self.read_status()
        s["updated_at"] = self._real_ago(20)
        self._write_status(s)
        status_r = run(["status"], cwd=self.tmp)
        self.assertEqual(status_r.returncode, 0, status_r.stdout + status_r.stderr)
        self.assertIsNotNone(json.loads(status_r.stdout)["stall_warning"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0, "a stall warning must not block validate")
        same_state = run(["advance", "1", "0"], cwd=self.tmp)
        self.assertEqual(same_state.returncode, 0, same_state.stdout + same_state.stderr)

        # Busy background task: fresh heartbeat, updated_at still stale.
        s2 = self.read_status()
        s2["updated_at"] = self._real_ago(20)
        s2["last_heartbeat_at"] = self._real_ago(1)
        self._write_status(s2)
        status_r2 = run(["status"], cwd=self.tmp)
        self.assertEqual(status_r2.returncode, 0, status_r2.stdout + status_r2.stderr)
        payload2 = json.loads(status_r2.stdout)
        self.assertIsNone(payload2["stall_warning"])
        self.assertIsNotNone(payload2["activity_note"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0, "a busy state must not block validate")
        self.assertEqual(run(["advance", "1", "0"], cwd=self.tmp).returncode, 0, "a busy state must not block advance")

    def test_heartbeat_command_records_liveness_without_mutating_progress(self):
        self.init()
        before = self.read_status()
        r = run(["heartbeat", "--by", "impl-1", "--note", "long design review running"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HEARTBEAT_RECORDED", r.stdout)
        after = self.read_status()
        self.assertEqual(after["phase_number"], before["phase_number"])
        self.assertEqual(after["progress"], before["progress"])
        self.assertEqual(after["updated_at"], before["updated_at"], "heartbeat must not touch updated_at")
        self.assertIsNotNone(after["last_heartbeat_at"])
        self.assertNotEqual(after["last_heartbeat_at"], before.get("last_heartbeat_at"))
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"heartbeat"', events_text)
        self.assertIn("long design review running", events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)
        missing_by = run(["heartbeat"], cwd=self.tmp)
        self.assertNotEqual(missing_by.returncode, 0)
        self.assertNotIn("Traceback", missing_by.stdout + missing_by.stderr)

    def test_dashboard_shows_distinct_busy_label_and_still_shows_stalled_banner(self):
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dash
        self.init()
        # Busy background task.
        s = self.read_status()
        s["updated_at"] = self._real_ago(20)
        s["last_heartbeat_at"] = self._real_ago(1)
        self._write_status(s)
        snapshot = dash.build_snapshot(self.tmp)
        self.assertTrue(snapshot["initialized"])
        self.assertIsNotNone(snapshot["activity_note"])
        supervisor = snapshot["supervisor"]
        self.assertNotEqual(supervisor["label"], "On course")
        self.assertNotIn("stalled", supervisor["headline"].lower())
        self.assertIn("background", (supervisor["label"] + supervisor["headline"]).lower())

        # Genuinely stalled: no heartbeat at all.
        s2 = self.read_status()
        s2["updated_at"] = self._real_ago(20)
        s2.pop("last_heartbeat_at", None)
        self._write_status(s2)
        snapshot2 = dash.build_snapshot(self.tmp)
        supervisor2 = snapshot2["supervisor"]
        self.assertIsNone(snapshot2["activity_note"])
        self.assertIn("stalled", supervisor2["headline"].lower())
        self.assertTrue(any("no update" in item for item in supervisor2["attention"]),
                        supervisor2["attention"])

    def test_missing_heartbeat_field_is_backward_compatible(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.init()
        s = self.read_status()
        self.assertIn("last_heartbeat_at", s)  # init writes it as None going forward
        del s["last_heartbeat_at"]  # simulate a status.json from BEFORE this fix
        self._write_status(s)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        without_field = dict(s)
        without_field["status"] = "in_progress"
        without_field["updated_at"] = self._ago(20)
        with_null_field = dict(without_field)
        with_null_field["last_heartbeat_at"] = None
        cfg = {"stall_minutes": 10}
        self.assertEqual(lib.stall_warning(without_field, cfg, now=self.NOW),
                         lib.stall_warning(with_null_field, cfg, now=self.NOW))
        self.assertIsNotNone(lib.stall_warning(without_field, cfg, now=self.NOW))

    def test_malformed_heartbeat_field_rejected_by_schema(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.init()
        s = self.read_status()
        s["last_heartbeat_at"] = "not-a-timestamp"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("last_heartbeat_at", r.stdout)

        s2 = self.read_status()
        s2["last_heartbeat_at"] = datetime(2026, 1, 1, 12, 0, 0).isoformat()  # no timezone
        (self.tmp / "handsoff-status.json").write_text(json.dumps(s2))
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1)
        self.assertNotIn("Traceback", r2.stdout + r2.stderr)
        self.assertIn("last_heartbeat_at", r2.stdout)

        cfg = {"stall_minutes": 10}
        bad_status = {"status": "in_progress", "updated_at": self._ago(20), "last_heartbeat_at": "not-a-timestamp"}
        self.assertIsNotNone(lib.stall_warning(bad_status, cfg, now=self.NOW),
                            "a malformed heartbeat must never be read as fresh")


class TestHumanPauseSuppressesStall(HandsoffTestCase):
    """Issue #34: `human-pause-start` recorded an event and nothing else,
    so a run waiting on a person for longer than stall_minutes was flagged
    stalled anyway, and `activity_note` had nothing to say about it. Fix:
    the pause is persisted as a durable `human_pause` record on status
    (not a heartbeat, which would expire and read as abandoned again ten
    minutes later); stall_warning() is suppressed while it is open,
    activity_note() names who is being waited on, and `human-pause-end`
    clears it so the same stale state warns again. Criteria
    i34-pause-suppresses-stall and i34-pause-activity-note."""

    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _ago(self, minutes):
        return (self.NOW - timedelta(minutes=minutes)).isoformat()

    def _real_ago(self, minutes):
        """A genuinely past timestamp relative to the real wall clock, for
        tests that go through the CLI (which does not take an injectable
        `now`)."""
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def _write_status(self, status):
        """Same pattern as TestActivityAwareStallDetection: write the dict
        directly and re-anchor the event log the way commit() would, so an
        aged updated_at reads as time passing, not as tampering."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted status timestamps directly")

    def _age_updated_at(self, minutes):
        s = self.read_status()
        s["updated_at"] = self._real_ago(minutes)
        self._write_status(s)

    def _status_payload(self):
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_open_pause_suppresses_stall_and_names_who_is_waited_on(self):
        self.init()
        r = run(["human-pause-start", "--by", "moncy", "--note", "need a port decision"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pause = self.read_status()["human_pause"]
        self.assertEqual(pause["by"], "moncy")
        self.assertEqual(pause["note"], "need a port decision")
        self.assertIsNotNone(datetime.fromisoformat(pause["since"]).tzinfo)
        self.assertIsNone(self.read_status()["last_heartbeat_at"], "a pause is not a heartbeat")
        self._age_updated_at(30)
        payload = self._status_payload()
        self.assertIsNone(payload["stall_warning"])
        self.assertTrue(payload["activity_note"].startswith("waiting on moncy since"), payload["activity_note"])
        self.assertIn(": need a port decision", payload["activity_note"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0, "an open pause must not block validate")

    def test_activity_note_rendering_is_exact(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = {"stall_minutes": 10}
        status = {"status": "in_progress", "updated_at": self._ago(30),
                  "human_pause": {"by": "moncy", "since": self._ago(12), "note": "need a port decision"}}
        self.assertIsNone(lib.stall_warning(status, cfg, now=self.NOW))
        self.assertEqual(lib.activity_note(status, cfg, now=self.NOW),
                         "waiting on moncy since 12 min ago: need a port decision")
        # The pause outlasting stall_minutes many times over changes nothing:
        # it is a declared state, not a liveness signal that expires.
        long_pause = dict(status, human_pause={"by": "moncy", "since": self._ago(600), "note": None})
        self.assertIsNone(lib.stall_warning(long_pause, cfg, now=self.NOW))
        self.assertEqual(lib.activity_note(long_pause, cfg, now=self.NOW), "waiting on moncy since 600 min ago")
        # A null or absent field is exactly today's behavior.
        for legacy in (dict(status, human_pause=None), {k: v for k, v in status.items() if k != "human_pause"}):
            self.assertIsNotNone(lib.stall_warning(legacy, cfg, now=self.NOW))
            self.assertIsNone(lib.activity_note(legacy, cfg, now=self.NOW))

    def test_pause_without_note_has_null_note_and_no_trailing_colon(self):
        self.init()
        r = run(["human-pause-start", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.read_status()["human_pause"]["note"])
        self._age_updated_at(30)
        note = self._status_payload()["activity_note"]
        self.assertRegex(note, r"^waiting on moncy since \d+ min ago$")
        self.assertNotIn(":", note)
        # A blank --note is the same as no note, not an empty suffix.
        run(["human-pause-end", "--by", "moncy"], cwd=self.tmp)
        run(["human-pause-start", "--by", "moncy", "--note", "   "], cwd=self.tmp)
        self.assertIsNone(self.read_status()["human_pause"]["note"])

    def test_pause_end_restores_stall_warning_on_the_same_stale_state(self):
        self.init()
        run(["human-pause-start", "--by", "moncy", "--note", "waiting on credentials"], cwd=self.tmp)
        self._age_updated_at(30)
        self.assertIsNone(self._status_payload()["stall_warning"])
        r = run(["human-pause-end", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.read_status()["human_pause"])
        # human-pause-end does not touch updated_at, so the same staleness
        # is still on disk and must warn again now that nobody is waited on.
        payload = self._status_payload()
        self.assertIsNotNone(payload["stall_warning"])
        self.assertIn("no update in", payload["stall_warning"])
        self.assertIsNone(payload["activity_note"])
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"human_pause_started"', events_text)
        self.assertIn('"kind":"human_pause_ended"', events_text)

    def test_dashboard_snapshot_shows_the_same_activity_note(self):
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dash
        self.init()
        run(["human-pause-start", "--by", "moncy", "--note", "need a port decision"], cwd=self.tmp)
        self._age_updated_at(30)
        snapshot = dash.build_snapshot(self.tmp)
        self.assertTrue(snapshot["initialized"], snapshot.get("error"))
        note = snapshot["activity_note"]
        self.assertTrue(note.startswith("waiting on moncy since"), note)
        self.assertIn(": need a port decision", note)
        supervisor = snapshot["supervisor"]
        self.assertNotIn("stalled", supervisor["headline"].lower())
        self.assertFalse(any("no update" in item for item in supervisor["attention"]), supervisor["attention"])

    def test_malformed_human_pause_is_a_schema_error_not_a_crash(self):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        self.init()
        good = {"by": "moncy", "since": self._real_ago(1), "note": None}
        bad_cases = {
            "missing by": {k: v for k, v in good.items() if k != "by"},
            "naive timestamp": dict(good, since=datetime(2026, 1, 1, 12, 0, 0).isoformat()),
            "not a timestamp": dict(good, since="yesterday"),
            "empty note": dict(good, note=""),
            "extra key": dict(good, reason="x"),
            "not an object": "moncy",
        }
        for label, pause in bad_cases.items():
            s = self.read_status()
            s["human_pause"] = pause
            (self.tmp / "handsoff-status.json").write_text(json.dumps(s))
            r = run(["validate"], cwd=self.tmp)
            self.assertEqual(r.returncode, 1, label)
            self.assertNotIn("Traceback", r.stdout + r.stderr, label)
            self.assertIn("human_pause", r.stdout, label)
            status_r = run(["status"], cwd=self.tmp)
            self.assertNotIn("Traceback", status_r.stdout + status_r.stderr, label)
        # The validator itself names the field, and a well-formed record passes.
        errors = lib.validate_status_schema(dict(self.read_status(), human_pause={"by": "", "since": "x", "note": None}))
        self.assertTrue(any("human_pause.by" in e for e in errors), errors)
        self.assertTrue(any("human_pause.since" in e for e in errors), errors)
        self.assertEqual([e for e in lib.validate_status_schema(dict(self.read_status(), human_pause=good))
                          if "human_pause" in e], [])

    def test_background_wait_still_suppresses_via_heartbeat(self):
        """Guard the sibling path: a background wait is a liveness signal
        (last_heartbeat_at), not a human_pause record, and still reads as
        'background task active' rather than as a pause."""
        self.init()
        run(["background-wait-start", "--by", "implementer-1"], cwd=self.tmp)
        s = self.read_status()
        self.assertIsNotNone(s["last_heartbeat_at"])
        self.assertIsNone(s.get("human_pause"))
        s["updated_at"] = self._real_ago(30)
        self._write_status(s)
        payload = self._status_payload()
        self.assertIsNone(payload["stall_warning"])
        self.assertIn("background task active", payload["activity_note"])
        self.assertNotIn("waiting on", payload["activity_note"])


class TestLiveSessionStatus(HandsoffTestCase):
    """Issue #33: Mission Control could not say whether a managed role was
    actually running, waiting, stalled, or dead; the session record only
    changes at lifecycle transitions. Fix: `handsoff_agent.execute_launch`
    beacons `.handsoff-live.json` (seven keys, best effort, never a ledger)
    while the child runs, and `lib.live_status` derives one of eight states
    from structured state plus that beacon, with the ledger-bound session
    record always outranking it. Criteria i33-live-state-derivation,
    i33-stopped-failed-visible, i33-no-sensitive-output, and the automated
    half of i33-dashboard-live-strip."""

    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    BEACON_KEYS = {"session_id", "role", "state", "pid", "beacon_at", "ended_at", "exit_code"}

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 33 live status")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.cfg = self.lib.load_config(self.tmp)

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def _ago(self, seconds):
        return (self.NOW - timedelta(seconds=seconds)).isoformat()

    def _status(self, **overrides):
        """A minimal in-memory status: the derivation is pure over the dict
        and the beacon file, so most transitions need no CLI round trip."""
        status = {"status": "in_progress", "phase_number": 4, "updated_at": self._ago(600),
                  "last_heartbeat_at": None, "next_action": "Implement the feature",
                  "agent_sessions": {}, "current_agent_sessions": {}}
        status.update(overrides)
        return status

    def _with_session(self, number, role, state, *, started=300, running=240, ended=None, exit_code=None):
        sid = self._sid(number)
        session = {"session_id": sid, "role": role, "actor": f"codex-{role}", "adapter": "codex",
                   "requested_model": "default", "reported_model": None,
                   "resolution_source": "configured", "state": state,
                   "started_at": self._ago(started),
                   "running_at": self._ago(running) if state != "launching" and running is not None else None,
                   "ended_at": self._ago(ended) if ended is not None else None,
                   "exit_code": exit_code, "packet_id": None, "design_hash": None, "tier": None}
        return self._status(agent_sessions={sid: session}, current_agent_sessions={role: sid})

    def _beacon(self, number, role="implementer", *, age=2, state="running", pid=4242, ended_at=None, exit_code=None):
        self.assertTrue(self.lib.write_live_beacon(
            self.tmp, session_id=self._sid(number), role=role, state=state, pid=pid,
            ended_at=ended_at, exit_code=exit_code, now=self.NOW - timedelta(seconds=age),
        ))

    def _view(self, status, now=None):
        view = self.lib.live_status(status, self.cfg, self.tmp, now=now or self.NOW)
        self.assertEqual(set(view), {"state", "role", "session_id", "last_activity_at",
                                     "seconds_since_activity", "activity_source",
                                     "process_signal", "detail", "ended_at", "exit_code"})
        self.assertIn(view["state"], self.lib.LIVE_STATES)
        self.assertIn(view["process_signal"], ("fresh", "stale", "none"))
        return view

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def _real_spec(self, role, code):
        return self.runtime.LaunchSpec(
            role, "codex", "default", (sys.executable, "-c", code), str(self.tmp),
            "private task text", "configured",
        )

    # --- derivation over fixtures -------------------------------------------

    def test_no_session_reads_idle(self):
        view = self._view(self._status())
        self.assertEqual(view["state"], "idle")
        self.assertEqual(view["detail"], "no managed process is running")
        self.assertEqual(view["process_signal"], "none")
        self.assertIsNone(view["role"])
        self.assertIsNone(view["session_id"])
        self.assertEqual(view["last_activity_at"], self._ago(600))
        self.assertEqual(view["seconds_since_activity"], 600)
        self.assertIsNone(view["ended_at"])
        self.assertIsNone(view["exit_code"])

    def test_launching_with_fresh_beacon_reads_started(self):
        self._beacon(1, age=3)
        view = self._view(self._with_session(1, "implementer", "launching"))
        self.assertEqual(view["state"], "started")
        self.assertEqual(view["process_signal"], "fresh")
        self.assertEqual(view["role"], "implementer")
        self.assertEqual(view["session_id"], self._sid(1))
        self.assertEqual(view["seconds_since_activity"], 3)
        self.assertEqual(view["last_activity_at"], self._ago(3))
        self.assertIn("pid 4242", view["detail"])

    def test_running_with_fresh_beacon_reads_running(self):
        self._beacon(1, age=5)
        view = self._view(self._with_session(1, "implementer", "running"))
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["process_signal"], "fresh")
        self.assertEqual(view["seconds_since_activity"], 5)
        self.assertEqual(view["detail"], "implementer session running (pid 4242)")
        # exactly the freshness boundary still counts
        self._beacon(1, age=15)
        self.assertEqual(self._view(self._with_session(1, "implementer", "running"))["state"], "running")

    def test_running_with_stale_beacon_reads_stalled(self):
        self._beacon(1, age=40)
        view = self._view(self._with_session(1, "implementer", "running"))
        self.assertEqual(view["state"], "stalled")
        self.assertEqual(view["process_signal"], "stale")
        self.assertEqual(view["detail"], "no process signal for 40 s")
        self.assertEqual(view["seconds_since_activity"], 40)
        self.assertEqual(view["last_activity_at"], self._ago(40))
        # one second past the boundary is already stale
        self._beacon(1, age=16)
        self.assertEqual(self._view(self._with_session(1, "implementer", "running"))["state"], "stalled")

    def test_running_without_any_beacon_reads_stalled(self):
        self.assertFalse(self.lib.live_beacon_path(self.tmp).exists())
        view = self._view(self._with_session(1, "implementer", "running", running=90))
        self.assertEqual(view["state"], "stalled")
        self.assertEqual(view["process_signal"], "none")
        self.assertEqual(view["detail"], "no process signal for 90 s")

    def test_fresh_beacon_from_an_older_session_is_ignored(self):
        self._beacon(1, age=1)
        view = self._view(self._with_session(2, "reviewer", "running", running=30))
        self.assertEqual(view["state"], "stalled")
        self.assertEqual(view["process_signal"], "none")
        self.assertIn(f"beacon belongs to session {self._sid(1)}, not current session {self._sid(2)}", view["detail"])
        self.assertTrue(view["detail"].startswith("no process signal for 30 s"))
        self.assertEqual(view["last_activity_at"], self._ago(30), "a foreign beacon is not this session's activity")
        # the same foreign beacon during launching reads started, signal none
        started = self._view(self._with_session(2, "reviewer", "launching"))
        self.assertEqual(started["state"], "started")
        self.assertEqual(started["process_signal"], "none")

    def test_completed_and_cancelled_read_stopped_from_the_session_record(self):
        view = self._view(self._with_session(1, "implementer", "completed", ended=20, exit_code=0))
        self.assertEqual(view["state"], "stopped")
        self.assertEqual(view["ended_at"], self._ago(20))
        self.assertEqual(view["exit_code"], 0)
        self.assertEqual(view["detail"], f"implementer session completed (exit 0) at {self._ago(20)}")
        self.assertEqual(view["last_activity_at"], self._ago(20))
        self.assertEqual(view["process_signal"], "none")
        cancelled = self._view(self._with_session(1, "supervisor", "cancelled", ended=20, exit_code=130))
        self.assertEqual(cancelled["state"], "stopped")
        self.assertEqual(cancelled["exit_code"], 130)

    def test_failed_states_read_failed_with_the_real_exit_code(self):
        view = self._view(self._with_session(1, "reviewer", "failed", ended=10, exit_code=1))
        self.assertEqual(view["state"], "failed")
        self.assertEqual(view["exit_code"], 1)
        self.assertEqual(view["detail"], f"reviewer session failed (exit 1) at {self._ago(10)}")
        self.assertEqual(self._view(self._with_session(1, "reviewer", "failed", ended=10, exit_code=7))["exit_code"], 7)
        timed_out = self._view(self._with_session(1, "reviewer", "timed_out", ended=10, exit_code=124))
        self.assertEqual((timed_out["state"], timed_out["exit_code"]), ("failed", 124))
        self.assertIn("timed out (exit 124)", timed_out["detail"])
        never = self._view(self._with_session(1, "reviewer", "failed_to_start", running=None, ended=10))
        self.assertEqual(never["state"], "failed")
        self.assertIsNone(never["exit_code"])
        self.assertIn("failed to start (no exit code)", never["detail"])

    def test_session_record_outranks_a_beacon_that_still_says_running(self):
        """A child that died before its final write leaves a running beacon;
        the terminal session record wins and the beacon only informs
        process_signal."""
        self._beacon(1, age=2, state="running")
        view = self._view(self._with_session(1, "implementer", "failed", ended=1, exit_code=3))
        self.assertEqual(view["state"], "failed")
        self.assertEqual(view["exit_code"], 3)
        self.assertEqual(view["process_signal"], "fresh")
        # and the reverse: a terminal beacon never revives a session the record says is running
        self._beacon(1, age=2, state="completed", ended_at=self._ago(2), exit_code=0)
        view = self._view(self._with_session(1, "implementer", "running"))
        self.assertEqual(view["state"], "running")
        self.assertIsNone(view["exit_code"])

    def test_blocked_pause_and_deployment_wait_read_waiting(self):
        self._beacon(1, age=2)
        blocked = self._view(self._with_session(1, "implementer", "running") | {
            "status": "blocked", "next_action": "Need the Pilot to pick a port"})
        self.assertEqual(blocked["state"], "waiting")
        self.assertEqual(blocked["detail"], "Need the Pilot to pick a port")
        self.assertEqual(blocked["role"], "implementer", "the current session is still named")
        paused = self._view(self._status(human_pause={"by": "moncy", "since": self._ago(120), "note": "port decision"}))
        self.assertEqual(paused["state"], "waiting")
        self.assertEqual(paused["detail"], "waiting on moncy since 2 min ago: port decision")
        approval = self._view(self._status(phase_number=7, deployment_approved=None,
                                           next_action="Awaiting deployment approval"))
        self.assertEqual(approval["state"], "waiting")
        self.assertEqual(approval["detail"], "Awaiting deployment approval")
        approved = self._view(self._status(phase_number=7, deployment_approved={"by": "moncy", "at": self._ago(5)}))
        self.assertEqual(approved["state"], "idle")
        no_gate = self.lib.live_status(self._status(phase_number=7), {**self.cfg, "deployment_requires_explicit_approval": False},
                                       self.tmp, now=self.NOW)
        self.assertEqual(no_gate["state"], "idle")

    def test_complete_run_reads_complete(self):
        self._beacon(1, age=2)
        view = self._view(self._with_session(1, "supervisor", "running") | {"status": "complete", "phase_number": 8})
        self.assertEqual(view["state"], "complete")
        self.assertEqual(view["detail"], "run complete")

    def test_last_activity_is_the_freshest_signal(self):
        status = self._with_session(1, "implementer", "running", started=300, running=240)
        status["updated_at"] = self._ago(200)
        status["last_heartbeat_at"] = self._ago(100)
        view = self._view(status)
        self.assertEqual(view["last_activity_at"], self._ago(100))
        self.assertEqual(view["seconds_since_activity"], 100)
        self._beacon(1, age=4)
        view = self._view(status)
        self.assertEqual(view["last_activity_at"], self._ago(4))
        self.assertEqual(view["seconds_since_activity"], 4)
        status["updated_at"] = self._ago(1)
        self.assertEqual(self._view(status)["last_activity_at"], self._ago(1))

    def test_malformed_beacon_reads_as_no_signal(self):
        path = self.lib.live_beacon_path(self.tmp)
        for text in ("not json", "[]", json.dumps({"session_id": self._sid(1)}),
                     json.dumps({**{k: None for k in self.BEACON_KEYS}, "session_id": self._sid(1),
                                 "role": "implementer", "state": "running", "beacon_at": "yesterday"}),
                     json.dumps({**{k: None for k in self.BEACON_KEYS}, "session_id": self._sid(1),
                                 "role": "implementer", "state": "running", "beacon_at": self._ago(1),
                                 "pid": "4242", "extra": 1})):
            path.write_text(text)
            self.assertIsNone(self.lib.read_live_beacon(self.tmp), text)
            view = self._view(self._with_session(1, "implementer", "running", running=30))
            self.assertEqual((view["state"], view["process_signal"]), ("stalled", "none"), text)

    # --- the beacon written by a real managed launch ---------------------------

    def test_fake_child_exiting_3_leaves_a_final_failed_beacon(self):
        with self.assertRaisesRegex(self.lib.HandsoffError, "exited with status 3"):
            self.runtime.execute_launch(
                self._real_spec("reviewer", "import sys; sys.exit(3)"),
                session_id_factory=lambda: self._sid(5), beacon_interval=0.05,
            )
        beacon = json.loads(self.lib.live_beacon_path(self.tmp).read_text())
        self.assertEqual(set(beacon), self.BEACON_KEYS, "exactly the seven allowed keys")
        self.assertEqual(beacon["session_id"], self._sid(5))
        self.assertEqual(beacon["role"], "reviewer")
        self.assertEqual(beacon["state"], "failed")
        self.assertEqual(beacon["exit_code"], 3)
        self.assertIsInstance(beacon["pid"], int)
        self.assertIsNotNone(datetime.fromisoformat(beacon["beacon_at"]).tzinfo)
        session = self.read_status()["agent_sessions"][self._sid(5)]
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["exit_code"], 3)
        self.assertEqual(beacon["ended_at"], session["ended_at"], "the final beacon copies the record's ended_at")
        view = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual(view["state"], "failed")
        self.assertEqual(view["exit_code"], 3)
        self.assertEqual(view["ended_at"], session["ended_at"])
        self.assertEqual(view["role"], "reviewer")
        self.assertEqual(view["detail"], f"reviewer session failed (exit 3) at {session['ended_at']}")
        kinds = [event["kind"] for event in self._events() if event.get("session_id") == self._sid(5)]
        self.assertEqual(kinds, ["agent_session_launching", "agent_session_running", "agent_session_failed"])
        self.assertFalse(any("beacon" in event["kind"] or "live" in event["kind"] for event in self._events()))
        persisted = "\n".join(path.read_text() for path in (
            self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
            self.lib.live_beacon_path(self.tmp)))
        self.assertNotIn("private task text", persisted)
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_fake_child_exiting_0_leaves_a_final_completed_beacon(self):
        self.assertEqual(self.runtime.execute_launch(
            self._real_spec("implementer", "import sys; sys.exit(0)"),
            session_id_factory=lambda: self._sid(6), beacon_interval=0.05,
        ), 0)
        beacon = json.loads(self.lib.live_beacon_path(self.tmp).read_text())
        self.assertEqual(set(beacon), self.BEACON_KEYS)
        self.assertEqual((beacon["state"], beacon["exit_code"]), ("completed", 0))
        view = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual((view["state"], view["exit_code"]), ("stopped", 0))
        self.assertIsNotNone(view["ended_at"])

    def test_live_child_reads_running_with_a_fresh_signal_then_stopped(self):
        """End to end with a real sleeping child: the periodic beacon makes
        the view read running while the child lives, and stopped after."""
        result = {}

        def launch():
            try:
                result["code"] = self.runtime.execute_launch(
                    self._real_spec("implementer", "import time; time.sleep(1.5)"),
                    session_id_factory=lambda: self._sid(8), beacon_interval=0.1,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                result["error"] = exc

        worker = threading.Thread(target=launch)
        worker.start()
        seen = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = json.loads((self.tmp / "handsoff-status.json").read_text())
            view = self.lib.live_status(status, self.cfg, self.tmp)
            if view["state"] == "running" and view["process_signal"] == "fresh":
                seen = view
                break
            time.sleep(0.05)
        worker.join(timeout=10)
        self.assertNotIn("error", result, result.get("error"))
        self.assertIsNotNone(seen, "the view never read running with a fresh beacon while the child lived")
        self.assertEqual(seen["session_id"], self._sid(8))
        self.assertLessEqual(seen["seconds_since_activity"], 2)
        beacon = json.loads(self.lib.live_beacon_path(self.tmp).read_text())
        self.assertIsInstance(beacon["pid"], int)
        self.assertEqual(result.get("code"), 0)
        view = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual((view["state"], view["exit_code"]), ("stopped", 0))

    def test_unwritable_beacon_path_never_touches_the_lifecycle(self):
        self.lib.live_beacon_path(self.tmp).mkdir()
        self.assertEqual(self.runtime.execute_launch(
            self._real_spec("implementer", "import sys; sys.exit(0)"),
            session_id_factory=lambda: self._sid(7), beacon_interval=0.05,
        ), 0)
        self.assertTrue(self.lib.live_beacon_path(self.tmp).is_dir(), "nothing replaced the directory")
        session = self.read_status()["agent_sessions"][self._sid(7)]
        self.assertEqual((session["state"], session["exit_code"]), ("completed", 0))
        view = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual((view["state"], view["exit_code"], view["process_signal"]), ("stopped", 0, "none"))
        self.assertEqual(view["ended_at"], session["ended_at"])
        self.assertFalse(list((self.tmp).glob(".handsoff-live.json.tmp-*")), "no temp file is left behind")
        with self.assertRaisesRegex(self.lib.HandsoffError, "exited with status 2"):
            self.runtime.execute_launch(
                self._real_spec("reviewer", "import sys; sys.exit(2)"),
                session_id_factory=lambda: self._sid(9), beacon_interval=0.05,
            )
        view = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual((view["state"], view["exit_code"], view["role"]), ("failed", 2, "reviewer"))
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_write_live_beacon_is_best_effort_and_bounded(self):
        self.assertTrue(self.lib.write_live_beacon(
            self.tmp, session_id=self._sid(1), role="implementer", state="running", pid=True))
        beacon = self.lib.read_live_beacon(self.tmp)
        self.assertIsNone(beacon["pid"], "a bool is not a pid")
        self.assertEqual(set(beacon), self.BEACON_KEYS)
        self.assertFalse(self.lib.write_live_beacon(
            self.tmp / "missing-dir", session_id=self._sid(1), role="implementer", state="running", pid=1))

    # --- status output and dashboard wiring -------------------------------------

    def test_status_command_and_snapshot_expose_the_view(self):
        self._beacon(1, age=1)
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        live = json.loads(r.stdout)["live"]
        self.assertEqual(live["state"], "idle")
        self.assertEqual(live["detail"], "no managed process is running")
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["live"]["state"], "idle")
        self.assertEqual(set(snapshot["live"]), set(live))
        self.assertEqual(self.runtime.execute_launch(
            self._real_spec("implementer", "import sys; sys.exit(0)"),
            session_id_factory=lambda: self._sid(3), beacon_interval=0.05,
        ), 0)
        live = json.loads(run(["status"], cwd=self.tmp).stdout)["live"]
        self.assertEqual((live["state"], live["exit_code"], live["role"]), ("stopped", 0, "implementer"))
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["live"]["session_id"], self._sid(3))
        self.assertEqual(snapshot["live"]["state"], "stopped")

    def test_artifact_signature_tracks_the_beacon_file(self):
        before = self.dashboard._artifact_signature(self.tmp)
        paths = [entry[0] for entry in before]
        self.assertIn(str(self.lib.live_beacon_path(self.tmp)), paths)
        self._beacon(1, age=1)
        after = self.dashboard._artifact_signature(self.tmp)
        self.assertNotEqual(before, after, "a beacon write invalidates the SSE signature")

    def test_active_role_goes_dark_during_an_open_human_pause(self):
        status = self.read_status()
        status["phase_number"] = 4
        request = self.dashboard._input_request(status, self.cfg)
        self.assertFalse(request["required"])
        self.assertEqual(self.dashboard._active_role(status, request), "implementer")
        status["human_pause"] = {"by": "moncy", "since": self._ago(30), "note": None}
        self.assertIsNone(self.dashboard._active_role(status, self.dashboard._input_request(status, self.cfg)))
        r = run(["human-pause-start", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertIsNone(snapshot["actors"]["active_role"])
        self.assertEqual(snapshot["live"]["state"], "waiting")
        self.assertTrue(snapshot["live"]["detail"].startswith("waiting on moncy since"))

    def test_gitignore_and_docs_cover_the_beacon(self):
        self.assertIn(".handsoff-live.json", (ROOT / ".gitignore").read_text().splitlines())
        readme = (ROOT / "README.md").read_text()
        self.assertIn("(#33)", readme)
        self.assertIn("**Live status.**", readme)
        for key in self.BEACON_KEYS:
            self.assertIn(f"`{key}`", readme)


class TestOutputLiveness(HandsoffTestCase):
    """Issue #41: a managed agent window streaming output was flagged
    "Signal has gone silent" because both liveness signals were timers and
    neither `stall_warning` nor `activity_note` read them. Fix: every
    stdout/stderr chunk of a managed child (stdout now captured for every
    role) notes `.handsoff-output-liveness.json` (five keys, counters, no
    content, one write per second), and the stall/activity readings honour
    that record only while it is bound to the current live session for its
    role. One `activity_view` feeds both `status` and the dashboard.
    Criteria i41-output-suppresses-stall, i41-output-is-liveness-only,
    i41-session-bound-output, i41-cli-dashboard-agree."""

    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    OUTPUT_KEYS = {"session_id", "role", "output_at", "chunks", "bytes"}
    LIFECYCLE_KINDS = {"agent_session_launching", "agent_session_running", "agent_session_completed",
                       "agent_session_failed", "agent_session_timed_out", "agent_session_cancelled",
                       "agent_session_failed_to_start"}

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 41 output liveness")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.cfg = self.lib.load_config(self.tmp)

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def _ago(self, seconds):
        return (self.NOW - timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _real_ago(seconds):
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    def _status(self, **overrides):
        """Phase-4 in-memory status with BOTH timer signals 30 min stale."""
        status = {"status": "in_progress", "phase_number": 4, "updated_at": self._ago(1800),
                  "last_heartbeat_at": self._ago(1800), "next_action": "Implement the feature",
                  "agent_sessions": {}, "current_agent_sessions": {}}
        status.update(overrides)
        return status

    def _session(self, number, role, state, *, ended=None, exit_code=None):
        sid = self._sid(number)
        return {"session_id": sid, "role": role, "actor": f"codex-{role}", "adapter": "codex",
                "requested_model": "default", "reported_model": None,
                "resolution_source": "configured", "state": state,
                "started_at": self._ago(300),
                "running_at": self._ago(240) if state != "launching" else None,
                "ended_at": self._ago(ended) if ended is not None else None,
                "exit_code": exit_code, "packet_id": None, "design_hash": None, "tier": None}

    def _with_session(self, number, role, state, **kwargs):
        session = self._session(number, role, state, **kwargs)
        return self._status(agent_sessions={session["session_id"]: session},
                            current_agent_sessions={role: session["session_id"]})

    def _output(self, number, role="implementer", *, age=20, real=False, chunks=3, nbytes=120):
        """Write the output record directly, `age` seconds old relative to
        NOW (pure readers) or to the real wall clock (the CLI, the server)."""
        record = {"session_id": self._sid(number), "role": role,
                  "output_at": self._real_ago(age) if real else self._ago(age),
                  "chunks": chunks, "bytes": nbytes}
        self.lib.output_liveness_path(self.tmp).write_text(json.dumps(record, sort_keys=True) + "\n")
        return record

    def _write_status(self, status):
        """Same pattern as TestActivityAwareStallDetection: write the dict
        directly and re-anchor the event log the way commit() would, so an
        aged timestamp reads as time passing, not as tampering."""
        with self.lib.project_lock(self.tmp):
            self.lib.atomic_write_json(self.lib.status_path(self.tmp, self.cfg), status)
            self.lib.append_event(self.tmp, self.cfg, "test_backdate",
                                  "test harness adjusted status timestamps directly")

    def _phase4_running_implementer(self, number=1):
        """A real Phase-4 run with a current running implementer session and
        both timer signals 30 min stale, so only output can keep it alive."""
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(4)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        session = self.lib.create_agent_session(
            self.tmp, role="implementer", actor="claude-implementer", adapter="claude",
            requested_model="default", resolution_source="configured",
            id_factory=lambda: self._sid(number),
        )
        self.lib.transition_agent_session(self.tmp, session["session_id"], "running")
        status = self.read_status()
        status["updated_at"] = self._real_ago(1800)
        status["last_heartbeat_at"] = self._real_ago(1800)
        # The session started before the output it is now producing.
        status["agent_sessions"][session["session_id"]]["started_at"] = self._real_ago(600)
        status["agent_sessions"][session["session_id"]]["running_at"] = self._real_ago(590)
        self._write_status(status)
        self.assertEqual(self.read_status()["phase_number"], 4)
        return session["session_id"]

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def _status_payload(self):
        r = run(["status"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def _api_dashboard(self, server):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/api/dashboard")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, 200, body)
        return json.loads(body)

    def _real_spec(self, role, code, adapter="claude"):
        return self.runtime.LaunchSpec(
            role, adapter, "default", (sys.executable, "-c", code), str(self.tmp),
            "private task text", "configured",
        )

    # --- pure readings -----------------------------------------------------------

    def test_fresh_bound_output_suppresses_the_stall_warning(self):
        cfg = {"stall_minutes": 10}
        self._output(1, age=20)
        liveness = self.lib.read_output_liveness(self.tmp)
        self.assertEqual(set(liveness), self.OUTPUT_KEYS)
        status = self._with_session(1, "implementer", "running")
        self.assertIsNone(self.lib.stall_warning(status, cfg, now=self.NOW, output_liveness=liveness))
        self.assertEqual(self.lib.activity_note(status, cfg, now=self.NOW, output_liveness=liveness),
                         "Agent active; latest output 20 seconds ago")
        launching = self._with_session(1, "implementer", "launching")
        self.assertIsNone(self.lib.stall_warning(launching, cfg, now=self.NOW, output_liveness=liveness))
        # Legacy: the same status without the record is exactly today's reading.
        self.assertEqual(self.lib.stall_warning(status, cfg, now=self.NOW),
                         "no update in 30 minutes (limit 10), consider escalating")
        self.assertIsNone(self.lib.activity_note(status, cfg, now=self.NOW))
        # Exactly stall_minutes old still counts (the existing strict '>').
        boundary = self.lib.read_output_liveness(self.tmp) | {"output_at": self._ago(600)}
        self.assertIsNone(self.lib.stall_warning(status, cfg, now=self.NOW, output_liveness=boundary))
        self.assertEqual(self.lib.activity_note(status, cfg, now=self.NOW, output_liveness=boundary),
                         "Agent active; latest output 600 seconds ago")
        # A fresh workflow update means no note at all: nothing is ambiguous.
        progressing = dict(status, updated_at=self._ago(5))
        self.assertIsNone(self.lib.activity_note(progressing, cfg, now=self.NOW, output_liveness=liveness))

    def test_output_stale_past_stall_minutes_still_warns(self):
        cfg = {"stall_minutes": 10}
        self._output(1, age=601)
        liveness = self.lib.read_output_liveness(self.tmp)
        status = self._with_session(1, "implementer", "running")
        self.assertEqual(self.lib.stall_warning(status, cfg, now=self.NOW, output_liveness=liveness),
                         "no update in 30 minutes (limit 10), consider escalating")
        self.assertIsNone(self.lib.activity_note(status, cfg, now=self.NOW, output_liveness=liveness))

    def test_output_from_a_terminal_replaced_or_unknown_session_never_counts(self):
        cfg = {"stall_minutes": 10}
        self._output(1, age=20)
        liveness = self.lib.read_output_liveness(self.tmp)
        cases = {
            "completed": self._with_session(1, "implementer", "completed", ended=10, exit_code=0),
            "failed": self._with_session(1, "implementer", "failed", ended=10, exit_code=1),
            "replaced": self._with_session(2, "implementer", "running"),
            "unknown": self._status(),
            "other role current": self._with_session(1, "reviewer", "running"),
        }
        for label, status in cases.items():
            with self.subTest(label=label):
                self.assertIsNone(self.lib.output_liveness_for(status, liveness))
                self.assertEqual(self.lib.stall_warning(status, cfg, now=self.NOW, output_liveness=liveness),
                                 "no update in 30 minutes (limit 10), consider escalating")
                self.assertIsNone(self.lib.activity_note(status, cfg, now=self.NOW, output_liveness=liveness))
        bound = self._with_session(1, "implementer", "running")
        self.assertEqual(self.lib.output_liveness_for(bound, liveness), liveness)

    def test_process_exit_ends_output_liveness_immediately(self):
        sid = self._phase4_running_implementer()
        self._output(1, real=True, age=2)
        before = self._status_payload()
        self.assertIsNone(before["stall_warning"])
        self.assertEqual(before["activity"]["source"], "output")
        self.lib.transition_agent_session(
            self.tmp, sid, "failed", exit_code=3,
            failure=self.lib.classify_runtime_failure(exit_code=3),
        )
        # The record is untouched and still fresh; only its binding expired.
        self.assertTrue(self.lib.output_liveness_path(self.tmp).exists())
        status = self.read_status()
        status["updated_at"] = self._real_ago(1800)
        self._write_status(status)
        liveness = self.lib.read_output_liveness(self.tmp)
        self.assertIsNone(self.lib.output_liveness_for(self.read_status(), liveness))
        after = self._status_payload()
        self.assertIsNotNone(after["stall_warning"])
        self.assertIn("no update in", after["stall_warning"])
        self.assertNotEqual(after["activity"]["source"], "output")
        live = self.lib.live_status(self.read_status(), self.cfg, self.tmp)
        self.assertEqual((live["state"], live["exit_code"]), ("failed", 3))
        self.assertNotEqual(live["activity_source"], "output")

    def test_explicit_heartbeat_still_suppresses_with_no_output_file(self):
        self.assertFalse(self.lib.output_liveness_path(self.tmp).exists())
        cfg = {"stall_minutes": 10}
        status = self._with_session(1, "implementer", "running") | {"last_heartbeat_at": self._ago(60)}
        self.assertIsNone(self.lib.stall_warning(status, cfg, now=self.NOW))
        self.assertIsNone(self.lib.stall_warning(status, cfg, now=self.NOW, output_liveness=None))
        self.assertIn("background task active", self.lib.activity_note(status, cfg, now=self.NOW))
        r = run(["heartbeat", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        aged = self.read_status()
        aged["updated_at"] = self._real_ago(1800)
        self._write_status(aged)
        payload = self._status_payload()
        self.assertIsNone(payload["stall_warning"])
        self.assertIn("background task active", payload["activity_note"])
        self.assertEqual(payload["activity"]["source"], "heartbeat")
        self.assertEqual(payload["activity"]["at"], aged["last_heartbeat_at"])

    def test_malformed_output_file_reads_as_no_signal(self):
        cfg = {"stall_minutes": 10}
        status = self._with_session(1, "implementer", "running")
        good = self._output(1, age=20)
        path = self.lib.output_liveness_path(self.tmp)
        for text in ("not json", "[]", json.dumps({"session_id": self._sid(1)}),
                     json.dumps(good | {"output_at": "yesterday"}),
                     json.dumps(good | {"chunks": "3"}),
                     json.dumps(good | {"bytes": -1}),
                     json.dumps(good | {"chunks": True}),
                     json.dumps(good | {"extra": 1}),
                     json.dumps({k: v for k, v in good.items() if k != "role"})):
            path.write_text(text)
            self.assertIsNone(self.lib.read_output_liveness(self.tmp), text)
            self.assertIsNotNone(self.lib.stall_warning(
                status, cfg, now=self.NOW, output_liveness=self.lib.read_output_liveness(self.tmp)), text)
        path.unlink()
        self.assertIsNone(self.lib.read_output_liveness(self.tmp))

    # --- the writer ----------------------------------------------------------------

    def test_note_output_liveness_is_rate_limited_counts_only_and_never_logs(self):
        status_before = (self.tmp / "handsoff-status.json").read_bytes()
        events_before = (self.tmp / "handsoff-events.jsonl").read_bytes()
        acceptance_before = (self.tmp / "handsoff-acceptance.json").read_bytes()
        clock = [1000.0]
        stamp = [self.NOW]
        writes = 0
        for n in range(500):
            clock[0] = 1000.0 + n * (0.999 / 499)  # 500 calls inside one second
            if self.lib.note_output_liveness(self.tmp, self._sid(1), "implementer", 7,
                                             now=stamp[0], monotonic=lambda: clock[0]):
                writes += 1
        self.assertLessEqual(writes, 2)
        self.assertGreaterEqual(writes, 1)
        record = json.loads(self.lib.output_liveness_path(self.tmp).read_text())
        self.assertEqual(set(record), self.OUTPUT_KEYS, "exactly the five allowed keys")
        self.assertEqual(record["session_id"], self._sid(1))
        self.assertEqual(record["role"], "implementer")
        self.assertEqual(record["output_at"], self.NOW.isoformat())
        self.assertIsInstance(record["chunks"], int)
        self.assertIsInstance(record["bytes"], int)
        # One second later the next chunk writes again, carrying every
        # chunk counted in between.
        clock[0] = 1001.5
        stamp[0] = self.NOW + timedelta(seconds=1)
        self.assertTrue(self.lib.note_output_liveness(self.tmp, self._sid(1), "implementer", 7,
                                                      now=stamp[0], monotonic=lambda: clock[0]))
        record = self.lib.read_output_liveness(self.tmp)
        self.assertEqual(record["chunks"], 501)
        self.assertEqual(record["bytes"], 501 * 7)
        self.assertEqual(record["output_at"], stamp[0].isoformat())
        self.assertEqual((self.tmp / "handsoff-status.json").read_bytes(), status_before)
        self.assertEqual((self.tmp / "handsoff-events.jsonl").read_bytes(), events_before)
        self.assertEqual((self.tmp / "handsoff-acceptance.json").read_bytes(), acceptance_before)
        self.assertFalse(list(self.tmp.glob(".handsoff-output-liveness.json.tmp-*")))
        # Best effort: an unwritable path returns False and raises nothing.
        clock[0] = 1010.0
        self.assertFalse(self.lib.note_output_liveness(self.tmp / "missing-dir", self._sid(1), "implementer", 1,
                                                       monotonic=lambda: clock[0]))
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    # --- end to end through execute_launch ---------------------------------------------

    def test_execute_launch_captures_every_role_and_moves_output_at_without_touching_the_run(self):
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(4)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status_before = self.read_status()
        acceptance_before = self.read_acceptance()
        events_before = len(self._events())
        popen_kwargs = {}

        def popen_factory(argv, **kwargs):
            popen_kwargs.update(kwargs)
            return subprocess.Popen(argv, **kwargs)

        code = ("import sys, time\n"
                "for i in range(10):\n"
                "    print('tick', i, flush=True)\n"
                "    time.sleep(0.2)\n"
                "print('secret-output-token', file=sys.stderr, flush=True)\n")
        result = {}
        captured = io.StringIO()

        def launch():
            try:
                with mock.patch("sys.stdout", new=captured):
                    result["code"] = self.runtime.execute_launch(
                        self._real_spec("implementer", code, adapter="claude"),
                        session_id_factory=lambda: self._sid(21), popen_factory=popen_factory,
                        beacon_interval=0.1,
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                result["error"] = exc

        worker = threading.Thread(target=launch)
        worker.start()
        seen = []
        deadline = time.monotonic() + 8
        while worker.is_alive() and time.monotonic() < deadline:
            record = self.lib.read_output_liveness(self.tmp)
            if record and (not seen or seen[-1]["output_at"] != record["output_at"]):
                seen.append(record)
            time.sleep(0.05)
        worker.join(timeout=10)
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("code"), 0)
        self.assertEqual(popen_kwargs["stdout"], subprocess.PIPE, "stdout is captured for a non-supervisor role")
        self.assertGreaterEqual(len(seen), 2, "output_at never moved while the child was printing")
        self.assertLess(seen[0]["output_at"], seen[-1]["output_at"])
        for record in seen:
            self.assertEqual(set(record), self.OUTPUT_KEYS)
            self.assertEqual(record["session_id"], self._sid(21))
            self.assertEqual(record["role"], "implementer")
        final = self.lib.read_output_liveness(self.tmp)
        self.assertGreaterEqual(final["chunks"], 1)
        self.assertGreater(final["bytes"], 0)
        self.assertIn("tick 9", captured.getvalue(), "the child's stdout was streamed through unchanged")
        self.assertNotIn("tick", self.lib.output_liveness_path(self.tmp).read_text())
        # The run itself is untouched by output: only the session record moved.
        status_after = self.read_status()
        for key in ("updated_at", "phase_number", "phase", "progress", "status",
                    "requirement_coverage", "last_heartbeat_at"):
            self.assertEqual(status_after[key], status_before[key], key)
        self.assertEqual(self.read_acceptance(), acceptance_before)
        new_events = self._events()[events_before:]
        self.assertEqual([event["kind"] for event in new_events],
                         ["agent_session_launching", "agent_session_running", "agent_session_completed"])
        persisted = "\n".join(path.read_text() for path in (
            self.tmp / "handsoff-status.json", self.tmp / "handsoff-events.jsonl",
            self.lib.output_liveness_path(self.tmp)))
        self.assertNotIn("secret-output-token", persisted)
        self.assertNotIn("private task text", persisted)
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)
        # The session is terminal now, so the fresh record no longer binds.
        self.assertIsNone(self.lib.output_liveness_for(status_after, final))
        view = self.lib.live_status(status_after, self.cfg, self.tmp)
        self.assertEqual((view["state"], view["exit_code"]), ("stopped", 0))

    def test_supervisor_role_is_still_parsed_and_stdout_reads_unchanged(self):
        import handsoff_broker as broker
        request = {"actor": "supervisor", "project_root": str(self.tmp.resolve()),
                   "action": "workflow", "command": "status"}
        code = ("import json, time\n"
                f"print({self.runtime.SUPERVISOR_REQUEST_PREFIX!r} + ' ' + json.dumps({request!r}), flush=True)\n")
        with mock.patch.object(broker, "dispatch_supervisor_request", return_value=0) as dispatch, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(self.runtime.execute_launch(
                self._real_spec("supervisor", code, adapter="codex"),
                session_id_factory=lambda: self._sid(22), beacon_interval=0.1,
            ), 0)
        dispatch.assert_called_once_with(self.tmp, request)
        record = self.lib.read_output_liveness(self.tmp)
        self.assertEqual((record["session_id"], record["role"]), (self._sid(22), "supervisor"))
        self.assertNotIn("workflow", self.lib.output_liveness_path(self.tmp).read_text())

    # --- one activity object for the CLI and the dashboard -------------------------------

    def test_status_cli_and_api_dashboard_carry_the_same_activity_object(self):
        sid = self._phase4_running_implementer()
        server = self.dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            record = self._output(1, real=True, age=20)
            payload = self._status_payload()
            snapshot = self._api_dashboard(server)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(payload["activity"], {
            "source": "output", "at": record["output_at"], "seconds_ago": 20,
            "stall_warning": None,
            "activity_note": "Agent active; latest output 20 seconds ago",
        })
        self.assertEqual(payload["stall_warning"], payload["activity"]["stall_warning"])
        self.assertEqual(payload["activity_note"], payload["activity"]["activity_note"])
        self.assertEqual(payload["live"]["activity_source"], "output")
        self.assertEqual(payload["live"]["session_id"], sid)
        self.assertTrue(snapshot["initialized"], snapshot.get("error"))
        self.assertEqual(snapshot["activity"], payload["activity"])
        self.assertEqual(snapshot["activity_note"], payload["activity"]["activity_note"])
        self.assertEqual(snapshot["live"]["activity_source"], "output")
        supervisor = snapshot["supervisor"]
        briefing = " ".join([supervisor["label"], supervisor["headline"], supervisor["summary"]])
        self.assertNotIn("stalled", briefing.lower())
        self.assertNotIn("silent", briefing.lower())
        self.assertIn("Agent active; latest output 20 seconds ago", supervisor["summary"])
        self.assertFalse(any("no update" in item for item in supervisor["attention"]), supervisor["attention"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        # The same run with the record stale reads the existing warning in both places.
        self._output(1, real=True, age=1200)
        stale_payload = self._status_payload()
        self.assertIn("no update in 30 minutes", stale_payload["stall_warning"])
        self.assertEqual(stale_payload["activity"]["stall_warning"], stale_payload["stall_warning"])
        self.assertIsNone(stale_payload["activity_note"])
        stale_snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertEqual(stale_snapshot["activity"]["stall_warning"], stale_payload["stall_warning"])
        self.assertIn("stalled", stale_snapshot["supervisor"]["headline"].lower())

    def test_activity_view_shape_and_sources(self):
        view = self.lib.activity_view(self._status(last_heartbeat_at=None), self.cfg, self.tmp, now=self.NOW)
        self.assertEqual(set(view), {"source", "at", "seconds_ago", "stall_warning", "activity_note"})
        self.assertEqual((view["source"], view["at"], view["seconds_ago"]), ("workflow", self._ago(1800), 1800))
        heartbeat = self.lib.activity_view(self._status(last_heartbeat_at=self._ago(900)), self.cfg, self.tmp, now=self.NOW)
        self.assertEqual((heartbeat["source"], heartbeat["seconds_ago"]), ("heartbeat", 900))
        self.assertIsNotNone(view["stall_warning"])
        self.assertIn(view["source"], self.lib.ACTIVITY_SOURCES)
        paused = self.lib.activity_view(
            self._status(human_pause={"by": "moncy", "since": self._ago(120), "note": None}),
            self.cfg, self.tmp, now=self.NOW)
        self.assertEqual((paused["source"], paused["seconds_ago"], paused["stall_warning"]), ("pause", 120, None))
        self.assertEqual(paused["activity_note"], "waiting on moncy since 2 min ago")
        self.assertTrue(self.lib.write_live_beacon(
            self.tmp, session_id=self._sid(1), role="implementer", state="running", pid=1,
            now=self.NOW - timedelta(seconds=4)))
        beaconed = self.lib.activity_view(self._with_session(1, "implementer", "running"), self.cfg, self.tmp, now=self.NOW)
        self.assertEqual((beaconed["source"], beaconed["seconds_ago"]), ("beacon", 4))
        self._output(1, age=2)
        output = self.lib.activity_view(self._with_session(1, "implementer", "running"), self.cfg, self.tmp, now=self.NOW)
        self.assertEqual((output["source"], output["at"], output["seconds_ago"]), ("output", self._ago(2), 2))
        self.assertIsNone(output["stall_warning"])
        session_only = self.lib.activity_view(
            self._with_session(2, "reviewer", "completed", ended=30, exit_code=0), self.cfg, self.tmp, now=self.NOW)
        self.assertEqual((session_only["source"], session_only["seconds_ago"]), ("session", 30))

    def test_artifact_signature_tracks_the_output_file(self):
        before = self.dashboard._artifact_signature(self.tmp)
        self.assertIn(str(self.lib.output_liveness_path(self.tmp)), [entry[0] for entry in before])
        self._output(1, age=1)
        self.assertNotEqual(before, self.dashboard._artifact_signature(self.tmp),
                            "an output record write invalidates the SSE signature")

    def test_gitignore_and_docs_cover_the_output_file(self):
        self.assertIn(".handsoff-output-liveness.json", (ROOT / ".gitignore").read_text().splitlines())
        readme = (ROOT / "README.md").read_text()
        self.assertIn("(#41)", readme)
        self.assertIn("Agent active; latest output N seconds ago", readme)
        for key in self.OUTPUT_KEYS:
            self.assertIn(f"`{key}`", readme)


class TestCriteriaTransaction(HandsoffTestCase):
    """Issue #44: `criteria-apply --file TX.json --by ACTOR [--dry-run]`
    applies adds, updates and removes as one lock-protected commit with one
    `criteria_transaction_applied` event, or refuses the whole list with
    the operation named and nothing written. Criteria i44-one-transaction,
    i44-all-or-nothing, i44-single-invalidation, i44-preview-and-compat."""

    STATE_FILES = ("handsoff-status.json", "handsoff-acceptance.json",
                   "handsoff-events.jsonl", "handsoff-verifications.jsonl")

    def setUp(self):
        super().setUp()
        self._extra_dirs = []
        sys.path.insert(0, str(BIN))
        import handsoff_broker
        import handsoff_lib
        self.lib = handsoff_lib
        self.broker = handsoff_broker
        self._configure_checks(self.tmp)

    def tearDown(self):
        for path in self._extra_dirs:
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    # -- fixture helpers ---------------------------------------------------

    def _configure_checks(self, root, commands=("true", "false")):
        toml = root / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", f"commands = {json.dumps(list(commands))}", 1))

    def _second_fixture(self):
        other = Path(tempfile.mkdtemp(prefix="handsoff-test-tx-"))
        self._extra_dirs.append(other)
        shutil.copy(ROOT / "handsoff.toml", other / "handsoff.toml")
        normalize_fixture_config(other / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", other / "schemas")
        self._configure_checks(other)
        return other

    def _write_tx(self, operations, root=None, name="tx.json"):
        path = (root or self.tmp) / name
        path.write_text(json.dumps({"operations": operations}))
        return path

    def _apply(self, operations, *extra, root=None, by="architect-1"):
        path = self._write_tx(operations, root=root)
        return run(["criteria-apply", "--file", str(path), "--by", by, *extra], cwd=root or self.tmp)

    def _events(self, root=None):
        path = (root or self.tmp) / "handsoff-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _snapshot(self, root=None):
        root = root or self.tmp
        return {name: (root / name).read_bytes() if (root / name).exists() else None
                for name in self.STATE_FILES}

    def _criteria_by_id(self, root=None):
        path = (root or self.tmp) / "handsoff-acceptance.json"
        return {c["id"]: c for c in json.loads(path.read_text())["criteria"]}

    def _write_status(self, status):
        cfg = self.lib.load_config(self.tmp)
        with self.lib.project_lock(self.tmp):
            self.lib.atomic_write_json(self.lib.status_path(self.tmp, cfg), status)
            self.lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted status fields directly")

    @staticmethod
    def _add(cid, requirement=None, verification="automated", tests=("true",), ctype="supporting"):
        return {"op": "add", "criterion": {
            "id": cid, "type": ctype, "requirement": requirement or f"#44 criterion {cid}",
            "verification": verification, "tests": list(tests),
        }}

    def _init_with_base(self, root=None, automated=("true", "false")):
        """A fixture with a real primary (REQ-001) and three supporting
        criteria, built by the single commands so both halves of the
        equivalence test start byte-identical."""
        root = root or self.tmp
        r = run(["init", "Issue 44 fixture"], cwd=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["criterion-update", "REQ-001", "--requirement", "#44 the primary outcome",
                 "--test", automated[0]], cwd=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for cid, verification, test in (("REQ-002", "automated", automated[1]), ("REQ-003", "manual", "walk through"),
                                        ("REQ-004", "browser", "open the page")):
            r = run(["criterion-add", cid, "--type", "supporting", "--requirement", f"#44 criterion {cid}",
                     "--verification", verification, "--test", test], cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # -- i44-one-transaction -----------------------------------------------

    def test_mixed_transaction_equals_the_single_command_sequence_with_one_event(self):
        singles = self.tmp
        batched = self._second_fixture()
        self._init_with_base(singles)
        self._init_with_base(batched)
        self.assertEqual(self._criteria_by_id(singles), self._criteria_by_id(batched))

        for args in (
            ["criterion-update", "REQ-001", "--requirement", "#44 the primary outcome, revised"],
            ["criterion-add", "REQ-005", "--type", "supporting", "--requirement", "#44 criterion REQ-005",
             "--verification", "automated", "--test", "true"],
            ["criterion-update", "REQ-002", "--verification", "manual", "--test", "check by hand"],
            ["criterion-remove", "REQ-004"],
            ["criterion-add", "REQ-006", "--type", "supporting", "--requirement", "#44 criterion REQ-006",
             "--verification", "manual", "--test", "observe"],
        ):
            r = run(args, cwd=singles)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        events_before = len(self._events(batched))
        r = self._apply([
            {"op": "update", "id": "REQ-001", "fields": {"requirement": "#44 the primary outcome, revised"}},
            self._add("REQ-005"),
            {"op": "update", "id": "REQ-002", "fields": {"verification": "manual", "tests": ["check by hand"]}},
            {"op": "remove", "id": "REQ-004"},
            self._add("REQ-006", verification="manual", tests=["observe"]),
        ], root=batched)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CRITERIA_TRANSACTION_APPLIED", r.stdout)

        sort = lambda root: json.dumps(sorted(self._criteria_by_id(root).values(), key=lambda c: c["id"]),
                                       sort_keys=True, indent=2).encode()
        self.assertEqual(sort(singles), sort(batched))
        self.assertEqual(sorted(self._criteria_by_id(batched)), ["REQ-001", "REQ-002", "REQ-003", "REQ-005", "REQ-006"])
        new_events = self._events(batched)[events_before:]
        self.assertEqual([e["kind"] for e in new_events], ["criteria_transaction_applied"])
        self.assertEqual(len([e for e in self._events(singles)
                              if e["kind"] in {"criterion_added", "criterion_updated", "criterion_removed"}]), 9)
        for root in (singles, batched):
            check = run(["verify-log"], cwd=root)
            self.assertEqual(check.returncode, 0, check.stdout)

    def test_event_carries_every_hash(self):
        self._init_with_base()
        before = self._criteria_by_id()
        before_registry = self.lib.acceptance_hash(list(before.values()))
        r = self._apply([
            {"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 criterion REQ-002, revised"}},
            self._add("REQ-005"),
            {"op": "remove", "id": "REQ-003"},
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self._criteria_by_id()
        event = self._events()[-1]
        self.assertEqual(event["kind"], "criteria_transaction_applied")
        self.assertEqual(event["by"], "architect-1")
        self.assertEqual(event["operation_count"], 3)
        self.assertEqual(event["registry_hash_before"], before_registry)
        self.assertEqual(event["registry_hash_after"], self.lib.acceptance_hash(list(after.values())))
        self.assertEqual(event["design_hash_after"], self.lib.design_hash(list(after.values())))
        self.assertEqual(event["operations"], [
            {"op": "update", "id": "REQ-002", "previous_hash": self.lib.criterion_spec_hash(before["REQ-002"]),
             "resulting_hash": self.lib.criterion_spec_hash(after["REQ-002"])},
            {"op": "add", "id": "REQ-005", "previous_hash": None,
             "resulting_hash": self.lib.criterion_spec_hash(after["REQ-005"])},
            {"op": "remove", "id": "REQ-003", "previous_hash": self.lib.criterion_spec_hash(before["REQ-003"]),
             "resulting_hash": None},
        ])
        self.assertNotEqual(event["operations"][0]["previous_hash"], event["operations"][0]["resulting_hash"])
        for forbidden in ("requirement", "tests", "output"):
            self.assertNotIn(forbidden, event)
        self.assertEqual(after["REQ-002"]["state"], "not_tested")
        self.assertEqual(after["REQ-002"]["evidence"], [])

    # -- i44-all-or-nothing ------------------------------------------------

    def test_one_bad_operation_among_five_refuses_everything_with_nothing_written(self):
        # Focused checks with narrow footprints plus a regression group, so
        # the footprint gate has something to capture.
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            'commands = ["true", "false"]', 'commands = ["python3 tests/test_a.py", "python3 tests/test_b.py"]', 1)
            + '\n[[regressions]]\nname = "python-full"\ncommands = ["python3 tests/test_everything.py"]\n')
        self._init_with_base(automated=("python3 tests/test_a.py", "python3 tests/test_b.py"))
        good = [
            {"op": "update", "id": "REQ-001", "fields": {"requirement": "#44 the primary outcome, revised"}},
            self._add("REQ-005", tests=["python3 tests/test_a.py"]),
            {"op": "update", "id": "REQ-003", "fields": {"tests": ["walk through twice"]}},
            {"op": "remove", "id": "REQ-004"},
        ]
        cases = {
            "unknown id": ({"op": "update", "id": "REQ-404", "fields": {"requirement": "x"}}, "unknown criterion"),
            "duplicate id on add": (self._add("REQ-002"), "criterion already exists"),
            "id twice in one transaction": ({"op": "update", "id": "REQ-005", "fields": {"requirement": "#44 x"}},
                                            "earlier operation"),
            "unlisted automated test": (self._add("REQ-006", tests=["pytest -q"]), "not one of [checks].commands"),
            "unlisted automated test on a policy change": (
                {"op": "update", "id": "REQ-003", "fields": {"verification": "automated"}},
                "not one of [checks].commands"),
            "whole-suite spelling of a gated group": (
                self._add("REQ-006", tests=["python3 tests/test_everything.py"]), "not one of [checks].commands"),
            "two primary_fix": (self._add("REQ-006", ctype="primary_fix", tests=["python3 tests/test_a.py"]),
                                "2 primary_fix criteria"),
            "malformed operation": ({"op": "update", "id": "REQ-002", "fields": {"requirement": "x"}, "extra": 1},
                                    "exactly 'op', 'id' and 'fields'"),
            "unknown op": ({"op": "rename", "id": "REQ-002"}, "'op' must be one of"),
            "add sets state": ({"op": "add", "criterion": {**self._add("REQ-006", tests=["python3 tests/test_a.py"])["criterion"],
                                                         "state": "failing"}},
                               "exactly id, type, requirement, verification, tests"),
            "bad field value": ({"op": "update", "id": "REQ-002", "fields": {"state": "passing"}},
                                "'state' must be one of"),
        }
        for label, (bad, reason) in cases.items():
            with self.subTest(case=label):
                before = self._snapshot()
                operations = good[:2] + [bad] + good[2:]
                r = self._apply(operations)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertTrue(r.stdout.startswith("SHIP_FEATURE_BLOCKED: operation 3 ("), r.stdout)
                self.assertIn(reason, r.stdout)
                self.assertEqual(self._snapshot(), before)
        # Zero primary_fix: removing the only primary is attributed to that operation.
        before = self._snapshot()
        r = self._apply(good[1:2] + [{"op": "remove", "id": "REQ-001"}] + good[2:])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("SHIP_FEATURE_BLOCKED: operation 2 (remove REQ-001): "), r.stdout)
        self.assertIn("0 primary_fix criteria", r.stdout)
        self.assertEqual(self._snapshot(), before)
        # The good operations alone still apply, so the refusals above were the bad operation's doing.
        r = self._apply(good)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_schema_failure_of_the_resulting_registry_is_refused(self):
        self._init_with_base()
        cfg = self.lib.load_config(self.tmp)
        status, acceptance = self.read_status(), self.read_acceptance()
        # A legacy registry flaw the field validator cannot see on the way
        # in: an untouched criterion with neither tests nor evidence.
        next(c for c in acceptance["criteria"] if c["id"] == "REQ-004")["tests"] = []
        with self.lib.project_lock(self.tmp):
            self.lib.commit(self.tmp, cfg, status=status, acceptance=acceptance,
                            event_kind="test_legacy_flaw", event_message="legacy registry flaw fixture")
        before = self._snapshot()
        r = self._apply([self._add("REQ-005"), {"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 x"}}])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("SHIP_FEATURE_BLOCKED: operation 2 (update REQ-002): "), r.stdout)
        self.assertIn("resulting registry fails validation", r.stdout)
        self.assertIn("REQ-004", r.stdout)
        self.assertEqual(self._snapshot(), before)
        # Repairing the flaw in the same transaction is accepted.
        r = self._apply([self._add("REQ-005"), {"op": "update", "id": "REQ-004", "fields": {"tests": ["open it"]}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_transaction_file_bounds_are_enforced_before_anything_is_written(self):
        self._init_with_base()
        before = self._snapshot()
        path = self.tmp / "tx.json"
        cases = {
            "zero operations": json.dumps({"operations": []}),
            "sixty-five operations": json.dumps({"operations": [self._add(f"REQ-{n:03d}") for n in range(100, 165)]}),
            "extra top-level key": json.dumps({"operations": [self._add("REQ-005")], "note": "x"}),
            "not an object": json.dumps([self._add("REQ-005")]),
            "over 256 KiB": json.dumps({"operations": [self._add("REQ-005", requirement="#44 " + "x" * 300000)]}),
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                path.write_text(text)
                r = run(["criteria-apply", "--file", str(path), "--by", "architect-1"], cwd=self.tmp)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertTrue(r.stdout.startswith("SHIP_FEATURE_BLOCKED: transaction file: "), r.stdout)
                self.assertEqual(self._snapshot(), before)
        r = run(["criteria-apply", "--file", str(path), "--by", " "], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--by must be a non-empty string", r.stdout)
        self.assertEqual(self._snapshot(), before)
        # Sixty-four operations are the documented maximum and apply.
        path.write_text(json.dumps({"operations": [self._add(f"REQ-{n:03d}") for n in range(100, 164)]}))
        r = run(["criteria-apply", "--file", str(path), "--by", "architect-1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self._criteria_by_id()), 68)

    def test_launched_regression_locks_the_transaction_out(self):
        self._init_with_base()
        status = self.read_status()
        status["regression_requests"] = [{"request_id": "rr-test", "state": "launched"}]
        self._write_status(status)
        before = self._snapshot()
        r = self._apply([self._add("REQ-005")])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("SHIP_FEATURE_BLOCKED: regression rr-test is running", r.stdout)
        self.assertEqual(self._snapshot(), before)

    def test_concurrent_readers_never_observe_an_intermediate_registry(self):
        self._init_with_base()
        acceptance_path = self.tmp / "handsoff-acceptance.json"
        before = acceptance_path.read_bytes()
        path = self._write_tx([self._add(f"REQ-{n:03d}", requirement="#44 " + "filler " * 40) for n in range(100, 160)]
                              + [{"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 revised"}}])
        observed = []
        errors = []
        finished = threading.Event()

        def reader():
            # At least 200 reads, and keep reading until the writer has exited,
            # so the window in which the replace lands is always covered.
            while len(observed) < 200 or not finished.is_set():
                try:
                    raw = acceptance_path.read_bytes()
                    json.loads(raw)
                    observed.append(raw)
                except Exception as exc:  # pragma: no cover - the assertion below reports it
                    errors.append(repr(exc))
                time.sleep(0.002)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            r = run(["criteria-apply", "--file", str(path), "--by", "architect-1"], cwd=self.tmp)
        finally:
            finished.set()
        thread.join(timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = acceptance_path.read_bytes()
        self.assertNotEqual(before, after)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(observed), 200)
        self.assertEqual(set(observed) - {before, after}, set())
        self.assertIn(before, observed)
        self.assertIn(after, observed)
        for _ in range(3):
            status = run(["status"], cwd=self.tmp)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(len(self._criteria_by_id()), 64)

    # -- i44-single-invalidation --------------------------------------------

    def test_decisions_are_invalidated_once_and_a_flagged_run_rolls_back_to_phase_2(self):
        self._init_with_base()
        advanced = self.advance_to(3)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 3)
        self.assertIsNotNone(status["design_approved"])
        self.assertIsNotNone(status["design_review"])
        now = datetime.now(timezone.utc).isoformat()
        digest = self.lib.acceptance_hash(self.read_acceptance()["criteria"])
        status["review"] = {"by": "reviewer-1", "at": now, "acceptance_hash": digest}
        status["reviewed_by"] = "reviewer-1"
        status["deployment_approved"] = {"by": "pilot", "at": now, "acceptance_hash": digest}
        status["live_verification_id"] = "vr-fixture"
        self._write_status(status)
        events_before = len(self._events())

        r = self._apply([
            {"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 criterion REQ-002, revised"}},
            self._add("REQ-005"),
            {"op": "remove", "id": "REQ-004"},
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.read_status()
        self.assertIsNone(after["design_approved"])
        self.assertIsNone(after["design_review"])
        self.assertIsNone(after["review"])
        self.assertIsNone(after["reviewed_by"])
        self.assertIsNone(after["deployment_approved"])
        self.assertIsNone(after["live_verification_id"])
        self.assertEqual(after["phase_number"], 2)
        self.assertEqual(after["phase"], self.lib.PHASES[2])
        self.assertEqual(after["status"], "in_progress")
        self.assertLessEqual(after["progress"], 20)
        new_events = self._events()[events_before:]
        self.assertEqual([e["kind"] for e in new_events], ["criteria_transaction_applied"])
        self.assertEqual(new_events[0]["registry_hash_after"],
                         self.lib.acceptance_hash(self.read_acceptance()["criteria"]))
        self.assertEqual(new_events[0]["design_hash_after"],
                         self.lib.design_hash(self.read_acceptance()["criteria"]))
        check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(check.returncode, 0, check.stdout)
        # The rollback landed in the same write: Phase 3 needs a fresh design decision now.
        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)

    def test_a_changed_primary_resets_the_original_symptom_binding(self):
        self._init_with_base()
        self.set_criterion_state("passing", resolved=True)
        self.assertTrue(self.read_status()["requirement_coverage"]["original_symptom_resolved"])
        r = self._apply([{"op": "update", "id": "REQ-003", "fields": {"requirement": "#44 supporting only"}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.read_status()["requirement_coverage"]["original_symptom_resolved"])
        r = self._apply([{"op": "update", "id": "REQ-001", "fields": {"requirement": "#44 primary, respecified"}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status = self.read_status()
        self.assertFalse(status["requirement_coverage"]["original_symptom_resolved"])
        self.assertIsNone(status["original_symptom_evidence_id"])
        self.assertEqual(self._criteria_by_id()["REQ-001"]["state"], "not_tested")

    # -- i44-preview-and-compat ---------------------------------------------

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        self._init_with_base()
        self.advance_to(2)
        review = approve_design_review(self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        operations = [
            {"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 criterion REQ-002, revised"}},
            self._add("REQ-005"),
            {"op": "remove", "id": "REQ-004"},
        ]
        before = self._snapshot()
        preview = self._apply(operations, "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertEqual(self._snapshot(), before)
        plan = json.loads(preview.stdout)
        self.assertEqual(plan["would_invalidate"], {"design": True, "review": False, "deployment": False, "live": False})
        self.assertIsNone(plan["would_roll_back_to_phase"])
        self.assertEqual(plan["operation_count"], 3)
        self.assertEqual(set(plan), {
            "operations", "operation_count", "registry_hash_before", "registry_hash_after",
            "design_hash_before", "design_hash_after", "work_items_after", "work_item_scope_changed",
            "would_invalidate", "would_roll_back_to_phase",
        })
        for key in ("registry_hash_before", "registry_hash_after", "design_hash_before", "design_hash_after"):
            self.assertRegex(plan[key], r"^[0-9a-f]{64}$")
        self.assertEqual(plan["work_items_after"], ["ask-issue-44-fixture"])
        # A refused dry run reads exactly like a refused apply.
        refused = self._apply(operations + [{"op": "remove", "id": "REQ-404"}], "--dry-run")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stdout.startswith("SHIP_FEATURE_BLOCKED: operation 4 (remove REQ-404): "), refused.stdout)
        self.assertEqual(self._snapshot(), before)

        applied = self._apply(operations)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "criteria_transaction_applied")
        for key in ("operations", "registry_hash_before", "registry_hash_after", "design_hash_after", "operation_count"):
            self.assertEqual(event[key], plan[key], key)
        self.assertIsNone(self.read_status()["design_review"])

    def test_added_criteria_take_exact_keys_and_start_not_tested_with_empty_evidence(self):
        self._init_with_base()
        criterion = self._add("REQ-005", verification="automated_and_browser", tests=["true"])["criterion"]
        for extra in ({"state": "failing"}, {"evidence": []}, {"authored_by": "x"}):
            with self.subTest(extra=extra):
                r = self._apply([{"op": "add", "criterion": {**criterion, **extra}}])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("exactly id, type, requirement, verification, tests", r.stdout)
        for missing in ("type", "tests", "verification"):
            with self.subTest(missing=missing):
                r = self._apply([{"op": "add", "criterion": {k: v for k, v in criterion.items() if k != missing}}])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        r = self._apply([{"op": "add", "criterion": {**criterion, "tests": []}}])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("'tests' must be a non-empty list", r.stdout)
        r = self._apply([{"op": "add", "criterion": criterion}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        added = self._criteria_by_id()["REQ-005"]
        self.assertEqual(added, {**criterion, "state": "not_tested", "evidence": []})
        # An update's explicit state is kept, an implicit one resets to not_tested with cleared evidence.
        r = self._apply([{"op": "update", "id": "REQ-005", "fields": {"state": "blocked"}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._criteria_by_id()["REQ-005"]["state"], "blocked")
        r = self._apply([{"op": "update", "id": "REQ-005", "fields": {"requirement": "#44 revised"}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._criteria_by_id()["REQ-005"]["state"], "not_tested")
        r = self._apply([{"op": "update", "id": "REQ-005", "fields": {}}])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("non-empty object", r.stdout)

    def test_single_commands_share_the_validator_and_keep_working(self):
        self._init_with_base()
        problems = self.lib.validate_criterion_fields({"id": "X", "type": "supporting", "requirement": "r",
                                                       "verification": "automated", "tests": ["true"]},
                                                      require_all=True)
        self.assertEqual(problems, [])
        self.assertTrue(self.lib.validate_criterion_fields({"requirement": " "}))
        self.assertTrue(self.lib.validate_criterion_fields({"tests": []}))
        self.assertTrue(self.lib.validate_criterion_fields({"type": "bonus"}))
        self.assertTrue(self.lib.validate_criterion_fields({"state": "passing"}))
        self.assertTrue(self.lib.validate_criterion_fields({"colour": "red"}))
        self.assertTrue(self.lib.validate_criterion_fields({"id": "X", "type": "supporting"}, require_all=True))
        blank = run(["criterion-update", "REQ-002", "--requirement", "  "], cwd=self.tmp)
        self.assertEqual(blank.returncode, 1, blank.stdout + blank.stderr)
        self.assertIn("'requirement' must be a non-empty string", blank.stdout)
        blank = run(["criterion-add", "REQ-005", "--type", "supporting", "--requirement", " ",
                     "--verification", "manual", "--test", "look"], cwd=self.tmp)
        self.assertEqual(blank.returncode, 1, blank.stdout + blank.stderr)
        self.assertIn("'requirement' must be a non-empty string", blank.stdout)
        r = self._apply([self._add("REQ-005"), {"op": "remove", "id": "REQ-004"}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for args, marker in (
            (["criterion-add", "REQ-006", "--type", "supporting", "--requirement", "#44 after the transaction",
              "--verification", "manual", "--test", "look"], "CRITERION_ADDED"),
            (["criterion-update", "REQ-005", "--requirement", "#44 revised after the transaction"], "CRITERION_UPDATED"),
            (["criterion-remove", "REQ-003"], "CRITERION_REMOVED"),
        ):
            r = run(args, cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(marker, r.stdout)
        self.assertEqual(sorted(self._criteria_by_id()), ["REQ-001", "REQ-002", "REQ-005", "REQ-006"])
        check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_legacy_registry_without_work_items_round_trips(self):
        self._init_with_base()
        cfg = self.lib.load_config(self.tmp)
        status, acceptance = self.read_status(), self.read_acceptance()
        acceptance.pop("work_items")
        for criterion in acceptance["criteria"]:
            criterion.pop("authored_by", None)
        for key in ("design_review_attempts", "design_review_authorization", "recovery_attempts",
                    "recovery_lease", "regression_requests", "active_work_item"):
            status.pop(key, None)
        with self.lib.project_lock(self.tmp):
            self.lib.commit(self.tmp, cfg, status=status, acceptance=acceptance,
                            event_kind="test_legacy", event_message="legacy registry fixture")
        r = self._apply([
            {"op": "update", "id": "REQ-001", "fields": {"requirement": "#44 the primary outcome, revised"}},
            self._add("REQ-005"),
            {"op": "remove", "id": "REQ-004"},
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.read_acceptance()
        self.assertNotIn("work_items", after)
        self.assertEqual(sorted(c["id"] for c in after["criteria"]), ["REQ-001", "REQ-002", "REQ-003", "REQ-005"])
        self.assertEqual(self._events()[-1]["kind"], "criteria_transaction_applied")
        check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(check.returncode, 0, check.stdout)
        status = run(["status"], cwd=self.tmp)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        r = run(["criterion-update", "REQ-005", "--requirement", "#44 single command after a legacy transaction"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_thirty_criterion_registry_is_revised_with_one_transaction_and_one_event(self):
        self.init("Issue 44 representative fixture")
        setup = self._apply([{"op": "update", "id": "REQ-001", "fields": {
            "requirement": "#44 the primary outcome", "tests": ["true"]}}]
            + [self._add(f"REQ-{n:03d}", verification=("automated", "manual", "browser")[n % 3],
                         tests=[("true", "walk through", "open the page")[n % 3]]) for n in range(2, 31)])
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        self.assertEqual(len(self._criteria_by_id()), 30)
        before = self._criteria_by_id()
        count_before = sum(e["kind"] == "criteria_transaction_applied" for e in self._events())

        adds = [f"REQ-{n:03d}" for n in range(31, 36)]
        updates = ["REQ-002", "REQ-007", "REQ-013", "REQ-020", "REQ-029"]
        removes = ["REQ-005", "REQ-018"]
        r = self._apply([self._add(cid) for cid in adds]
                        + [{"op": "update", "id": cid, "fields": {"requirement": f"#44 {cid} revised"}} for cid in updates]
                        + [{"op": "remove", "id": cid} for cid in removes])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self._criteria_by_id()
        self.assertEqual(sum(e["kind"] == "criteria_transaction_applied" for e in self._events()), count_before + 1)
        self.assertEqual(len(after), 33)
        self.assertEqual(sorted(after), sorted((set(before) | set(adds)) - set(removes)))
        for cid in updates:
            self.assertEqual(after[cid]["requirement"], f"#44 {cid} revised")
            self.assertEqual(after[cid]["state"], "not_tested")
        for cid in set(before) - set(updates) - set(removes):
            self.assertEqual(after[cid], before[cid])
        event = self._events()[-1]
        self.assertEqual(event["operation_count"], 12)
        self.assertEqual([op["op"] for op in event["operations"]], ["add"] * 5 + ["update"] * 5 + ["remove"] * 2)
        check = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_footprint_gate_mirrors_run_checks_for_automated_tests(self):
        self._init_with_base()
        cfg = self.lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        # A config load_config would refuse (a focused check that captures a
        # gated group, a check with a control operator): the planner still
        # refuses the operation rather than registering a test verify cannot run.
        cfg["check_commands"] = ["true", "python3 tests/test_a.py", "python3 tests/test_a.py; echo done"]
        cfg["regressions"] = [{"name": "python-full", "commands": ["python3 tests/test_a.py"]}]
        for test, reason in (("true", "captures gated regression group"),
                             ("python3 tests/test_a.py", "captures gated regression group"),
                             ("python3 tests/test_a.py; echo done", "control operators"),
                             ("python3 tests/test_b.py", "not one of [checks].commands")):
            with self.subTest(test=test):
                with self.assertRaises(self.lib.CriteriaTransactionError) as refused:
                    self.lib.plan_criteria_transaction(acceptance, cfg, [self._add("REQ-005", tests=[test])], root=self.tmp)
                self.assertTrue(str(refused.exception).startswith("operation 1 (add REQ-005): "), str(refused.exception))
                self.assertIn(reason, str(refused.exception))
        # Manual and browser entries are attestation descriptions, never gated commands.
        plan = self.lib.plan_criteria_transaction(
            acceptance, cfg, [self._add("REQ-005", verification="manual", tests=["walk through; twice"])], root=self.tmp)
        self.assertEqual(plan["operation_count"], 1)

    def test_planner_is_importable_and_pure(self):
        self._init_with_base()
        cfg = self.lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        frozen = json.dumps(acceptance, sort_keys=True)
        operations = [self._add("REQ-005"), {"op": "remove", "id": "REQ-004"}]
        plan = self.lib.plan_criteria_transaction(acceptance, cfg, operations, root=self.tmp)
        self.assertEqual(json.dumps(acceptance, sort_keys=True), frozen)
        self.assertEqual([op["id"] for op in plan["operations"]], ["REQ-005", "REQ-004"])
        self.assertEqual(plan["registry_hash_before"], self.lib.acceptance_hash(acceptance["criteria"]))
        self.assertEqual(plan["registry_hash_after"], self.lib.acceptance_hash(plan["criteria_after"]))
        self.assertEqual(plan["design_hash_after"], self.lib.design_hash(plan["criteria_after"]))
        self.assertFalse(plan["resets_original_symptom"])
        with self.assertRaises(self.lib.CriteriaTransactionError) as refused:
            self.lib.plan_criteria_transaction(acceptance, cfg, [{"op": "remove", "id": "nope"}], root=self.tmp)
        self.assertEqual(str(refused.exception), "operation 1 (remove nope): unknown criterion")
        self.lib.apply_criteria_plan(acceptance, plan)
        self.assertEqual(sorted(c["id"] for c in acceptance["criteria"]), ["REQ-001", "REQ-002", "REQ-003", "REQ-005"])
        with self.assertRaises(self.lib.HandsoffError):
            self.lib.apply_criteria_plan(acceptance, plan)

    def test_broker_allows_criteria_apply_for_the_supervisor(self):
        self._init_with_base()
        root_text = str(self.tmp.resolve())
        base = {"actor": "supervisor", "project_root": root_text, "action": "workflow",
                "command": "criteria-apply", "by": "supervisor-1", "file": "tx.json"}
        argv = self.broker._workflow_argv(self.tmp.resolve(), base)
        self.assertEqual(argv[argv.index("criteria-apply"):],
                         ["criteria-apply", "--file", "tx.json", "--by", "supervisor-1"])
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "dry_run": True})
        self.assertEqual(argv[-1], "--dry-run")
        for request in (
            {k: v for k, v in base.items() if k != "file"},
            {**base, "dry_run": "yes"},
            {**base, "operations": []},
        ):
            with self.subTest(request=request):
                with self.assertRaises(self.lib.HandsoffError):
                    self.broker._workflow_argv(self.tmp.resolve(), request)

    def test_readme_documents_the_transaction(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("(#44)", readme)
        self.assertIn("### Criteria transactions", readme)
        self.assertIn("criteria-apply --file", readme)
        self.assertIn("criteria_transaction_applied", readme)
        section = readme[readme.index("### Criteria transactions"):readme.index("## Work items and the per-item")]
        self.assertIn('{"operations": [', section)
        self.assertNotIn("\u2014", section)


class TestAmendmentLane(HandsoffTestCase):
    """Issue #42: the scoped post-approval amendment lane. `amendment-open`
    plans a criteria transaction with the #44 planner, classifies it
    (scoped or full_redesign; the caller cannot choose), applies a scoped
    delta with the changed criteria reset and the phase frozen, and only
    `amendment-review --approve` plus the Pilot's `amendment-approve` on
    the exact amendment hash rewrites the design hashes and resumes.
    Criteria i42-scoped-amendment, i42-frozen-until-approved,
    i42-full-redesign-escalation, i42-multi-item-isolation,
    i42-fail-closed, i42-dashboard."""

    STATE_FILES = ("handsoff-status.json", "handsoff-acceptance.json",
                   "handsoff-events.jsonl", "handsoff-verifications.jsonl")
    ARCHITECT = "architect-1"
    REVIEWER = "amendment-reviewer-1"
    PILOT = "pilot-moncy"

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(BIN))
        import handsoff_broker
        import handsoff_dashboard
        import handsoff_lib
        self.lib = handsoff_lib
        self.broker = handsoff_broker
        self.dashboard = handsoff_dashboard
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1)
                        .replace("live_commands = []", 'live_commands = ["true"]', 1))

    # -- fixture helpers ---------------------------------------------------

    def _write_tx(self, operations, name="tx.json"):
        path = self.tmp / name
        path.write_text(json.dumps({"operations": operations}))
        return path

    def _events(self):
        path = self.tmp / "handsoff-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _kinds(self):
        return [event["kind"] for event in self._events()]

    def _snapshot(self):
        return {name: (self.tmp / name).read_bytes() if (self.tmp / name).exists() else None
                for name in self.STATE_FILES}

    def _criteria(self):
        return {c["id"]: c for c in self.read_acceptance()["criteria"]}

    def _open(self, operations, *extra, by=None, name="tx.json"):
        path = self._write_tx(operations, name=name)
        return run(["amendment-open", "--file", str(path), "--by", by or self.ARCHITECT, *extra], cwd=self.tmp)

    def _revise(self, operations, by=None):
        path = self._write_tx(operations, name="tx-revise.json")
        return run(["amendment-revise", "--file", str(path), "--by", by or self.ARCHITECT], cwd=self.tmp)

    def _review(self, decision="--approve", by=None, summary="Scoped correction reviewed"):
        return run(["amendment-review", "--by", by or self.REVIEWER, decision, "--summary", summary], cwd=self.tmp)

    def _approve(self, by=None):
        return run(["amendment-approve", "--by", by or self.PILOT], cwd=self.tmp)

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _update(self, cid, **fields):
        return {"op": "update", "id": cid, "fields": fields}

    def _phase_4_run(self, combined_last=False):
        """A Phase-4 run with four criteria across two derived work items
        (`[#101]` REQ-001/REQ-002, `[#102]` REQ-003/REQ-004), every
        criterion evidenced and the original symptom resolved. With
        `combined_last`, REQ-004 is `automated_and_browser` (both halves
        evidenced) so a verification downgrade can be attempted."""
        self.init("Close #101 and #102")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "[#101] the primary outcome",
                      "--test", "true"], cwd=self.tmp))
        for cid, requirement in (("REQ-002", "[#101] REQ-002 builds on REQ-001"),
                                 ("REQ-003", "[#102] the second item's outcome"),
                                 ("REQ-004", "[#102] REQ-004 rounds out the second item")):
            verification = "automated_and_browser" if combined_last and cid == "REQ-004" else "automated"
            self._ok(run(["criterion-add", cid, "--type", "supporting", "--requirement", requirement,
                          "--verification", verification, "--test", "true"], cwd=self.tmp))
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], ["issue-101", "issue-102"])
        self._ok(self.advance_to(4))
        verified = self._ok(run(["verify", "--criterion", "REQ-001", "--criterion", "REQ-002", "--criterion", "REQ-003",
                                 "--criterion", "REQ-004", "--by", "implementer-1"], cwd=self.tmp))
        run_id = json.loads(verified.stdout)["criteria"]["REQ-001"]["run_id"]
        self._ok(run(["record-symptom-resolved", "--evidence", run_id, "--by", "implementer-1"], cwd=self.tmp))
        if combined_last:
            self._ok(run(["record-evidence", "REQ-004", "--kind", "browser", "--by", "implementer-1",
                          "--description", "Opened the page and saw the outcome"], cwd=self.tmp))
        status = self.read_status()
        self.assertEqual(status["phase_number"], 4)
        self.assertTrue(all(c["state"] == "passing" for c in self._criteria().values()))
        self.assertTrue(status["requirement_coverage"]["original_symptom_resolved"])
        self.assertEqual(self._kinds().count("design_approved"), 1)
        return status

    # -- i42-scoped-amendment / i42-frozen-until-approved --------------------

    def test_scoped_amendment_freezes_the_run_until_reviewed_and_approved(self):
        before_status = self._phase_4_run()
        before = self._criteria()
        base_design = self.lib.design_hash(list(before.values()))
        self.assertEqual(before_status["design_approved"]["design_hash"], base_design)

        opened = self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 builds on REQ-001, revised")],
                                     "--summary", "Tighten REQ-002 after the implementer's finding"))
        self.assertIn("AMENDMENT_OPENED", opened.stdout)
        status = self.read_status()
        amendment = status["amendment"]
        self.assertEqual(amendment["state"], "open")
        self.assertEqual(amendment["classification"], "scoped")
        self.assertEqual(amendment["classification_reasons"], [])
        self.assertRegex(amendment["amendment_id"], r"^am-[0-9a-f]{32}$")
        self.assertEqual(amendment["base_design_hash"], base_design)
        self.assertEqual(amendment["changed_ids"], ["REQ-002"])
        self.assertEqual(amendment["dependent_ids"], [])
        self.assertEqual(amendment["affected_work_items"], ["issue-101"])
        self.assertEqual(amendment["frozen_phase"], 4)
        self.assertEqual(amendment["frozen_progress"], 40)
        self.assertEqual(amendment["operations"], [{
            "op": "update", "id": "REQ-002",
            "previous_hash": self.lib.criterion_spec_hash(before["REQ-002"]),
            "resulting_hash": self.lib.criterion_spec_hash(self._criteria()["REQ-002"]),
        }])
        self.assertEqual(amendment["amendment_hash"],
                         self.lib.amendment_hash(base_design, amendment["operations"]))
        after = self._criteria()
        self.assertEqual(amendment["resulting_design_hash"], self.lib.design_hash(list(after.values())))
        self.assertEqual(after["REQ-002"]["state"], "not_tested")
        self.assertEqual(after["REQ-002"]["evidence"], [])
        for cid in ("REQ-001", "REQ-003", "REQ-004"):
            self.assertEqual(json.dumps(after[cid], sort_keys=True), json.dumps(before[cid], sort_keys=True), cid)
            self.assertEqual(after[cid]["state"], "passing")
        # Design decisions stay on file on their base hash; the run still validates.
        self.assertEqual(status["design_approved"]["design_hash"], base_design)
        self.assertEqual(status["design_review"]["design_hash"], base_design)
        self.assertEqual(status["phase_number"], 4)
        self.assertIsNone(status["review"])
        self.assertEqual(self._kinds()[-1], "amendment_opened")
        event = self._events()[-1]
        self.assertEqual(event["classification"], "scoped")
        self.assertEqual(event["changed_ids"], ["REQ-002"])
        for forbidden in ("requirement", "tests", "fields"):
            self.assertNotIn(forbidden, event)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        # Frozen: forward, backward, review, deployment, live, and every mutation refused.
        events_at_open = len(self._events())
        frozen = self._snapshot()
        advanced = run(["advance", "5", "50"], cwd=self.tmp)
        self.assertEqual(advanced.returncode, 1, advanced.stdout)
        self.assertIn(f"amendment gate: amendment {amendment['amendment_id']} is open", advanced.stdout)
        self.assertEqual(run(["advance", "3", "40"], cwd=self.tmp).returncode, 1)
        review = run(["record-review", "--by", "reviewer-1"], cwd=self.tmp)
        self.assertEqual(review.returncode, 1, review.stdout)
        self.assertIn("amendment gate", review.stdout)
        gate = run(["deployment-gate", "--approve", "--by", self.PILOT], cwd=self.tmp)
        self.assertEqual(gate.returncode, 1, gate.stdout)
        self.assertIn("amendment gate", gate.stdout)
        live = run(["verify-live", "--by", "supervisor-1"], cwd=self.tmp)
        self.assertEqual(live.returncode, 1, live.stdout)
        self.assertIn("amendment gate", live.stdout)
        for args in (["criterion-update", "REQ-003", "--requirement", "[#102] changed underneath"],
                     ["criterion-add", "REQ-009", "--type", "supporting", "--requirement", "[#102] more",
                      "--verification", "automated", "--test", "true"],
                     ["criterion-remove", "REQ-004"],
                     ["criteria-apply", "--file", str(self._write_tx([{"op": "remove", "id": "REQ-004"}], "mut.json")),
                      "--by", self.ARCHITECT]):
            with self.subTest(command=args[0]):
                refused = run(args, cwd=self.tmp)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(f"amendment gate: close or escalate amendment {amendment['amendment_id']}", refused.stdout)
        self.assertEqual(self._snapshot(), frozen)

        # The correction can still be evidenced while frozen.
        verified = self._ok(run(["verify", "--criterion", "REQ-002", "--by", "implementer-1"], cwd=self.tmp))
        self.assertTrue(json.loads(verified.stdout)["ok"])
        self.assertEqual(self._criteria()["REQ-002"]["state"], "passing")
        self.assertEqual(self.read_status()["amendment"]["amendment_id"], amendment["amendment_id"])

        # Pilot approval before review is refused; review binds the hash; approval resumes.
        early = self._approve()
        self.assertEqual(early.returncode, 1, early.stdout)
        self.assertIn("no review yet", early.stdout)
        reviewed = self._ok(self._review())
        self.assertIn("AMENDMENT_REVIEW_APPROVED", reviewed.stdout)
        self.assertEqual(self.read_status()["amendment"]["review"]["amendment_hash"], amendment["amendment_hash"])
        approved = self._ok(self._approve())
        self.assertIn("AMENDMENT_APPROVED", approved.stdout)
        status = self.read_status()
        self.assertIsNone(status["amendment"])
        self.assertEqual(len(status["amendments"]), 1)
        closed = status["amendments"][0]
        self.assertEqual(closed["state"], "approved")
        self.assertIsNotNone(closed["closed_at"])
        self.assertEqual(closed["pilot_approval"]["by"], self.PILOT)
        self.assertEqual(closed["pilot_approval"]["amendment_hash"], amendment["amendment_hash"])
        new_design = self.lib.design_hash(self.read_acceptance()["criteria"])
        self.assertNotEqual(new_design, base_design)
        self.assertEqual(status["design_approved"]["design_hash"], new_design)
        self.assertEqual(status["design_review"]["design_hash"], new_design)
        self.assertEqual(status["design_approved"]["amended_by"], [amendment["amendment_id"]])
        self.assertEqual(status["design_review"]["amended_by"], [amendment["amendment_id"]])
        self.assertEqual(status["design_approved"]["scope_hash"],
                         self.lib.work_item_scope_hash(self.read_acceptance()["work_items"]))
        self.assertEqual((status["phase_number"], status["progress"]), (4, 40))
        kinds_since_open = self._kinds()[events_at_open:]
        self.assertNotIn("phase_advanced", kinds_since_open)
        self.assertEqual(kinds_since_open, ["checks_run", "amendment_reviewed", "amendment_approved"])
        self.assertEqual(self._kinds().count("design_approved"), 1)
        self._ok(run(["advance", "5", "50"], cwd=self.tmp))
        self.assertEqual(self._kinds()[-1], "phase_advanced")
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    # -- i42-full-redesign-escalation --------------------------------------

    def test_full_redesign_deltas_refuse_to_open_and_the_ordinary_path_lands_phase_2(self):
        self._phase_4_run(combined_last=True)
        add = {"op": "add", "criterion": {"id": "REQ-009", "type": "supporting",
                                          "requirement": "[#101] a brand new outcome",
                                          "verification": "automated", "tests": ["true"]}}
        cases = {
            "add operation": ([add], "add operation is added scope"),
            "primary_fix type change": ([self._update("REQ-001", type="supporting"),
                                         self._update("REQ-002", type="primary_fix")],
                                        "changes which criterion is the primary_fix"),
            "automated_and_browser to automated": ([self._update("REQ-004", verification="automated")],
                                                   "verification downgrade automated_and_browser -> automated"),
            "automated to manual": ([self._update("REQ-003", verification="manual", tests=["walk through"])],
                                    "verification downgrade automated -> manual"),
            "cross-item change": ([self._update("REQ-002", requirement="[#101] REQ-002 revised"),
                                   self._update("REQ-003", requirement="[#102] REQ-003 revised")],
                                  "span more than one work item (issue-101, issue-102)"),
        }
        before = self._snapshot()
        for label, (operations, reason) in cases.items():
            with self.subTest(case=label):
                refused = self._open(operations)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertTrue(refused.stdout.startswith("AMENDMENT_REFUSED: full_redesign"), refused.stdout)
                self.assertIn(reason, refused.stdout)
                self.assertIn("criteria-apply", refused.stdout)
                self.assertEqual(self._snapshot(), before)
        forced = self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")], "--request-full-redesign")
        self.assertEqual(forced.returncode, 1, forced.stdout)
        self.assertIn("--request-full-redesign was passed", forced.stdout)
        self.assertEqual(self._snapshot(), before)
        self.assertIsNone(self.read_status()["amendment"])
        self.assertNotIn("amendment_opened", self._kinds())

        # The same transaction takes the ordinary path: design cleared, Phase 2.
        applied = self._ok(run(["criteria-apply", "--file", str(self._write_tx(cases["add operation"][0])),
                                "--by", self.ARCHITECT], cwd=self.tmp))
        self.assertIn("CRITERIA_TRANSACTION_APPLIED", applied.stdout)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 2)
        self.assertIsNone(status["design_approved"])
        self.assertIsNone(status["design_review"])
        self.assertIn("REQ-009", self._criteria())

    def test_escalate_takes_the_full_path_with_design_decisions_cleared(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        amendment_id = self.read_status()["amendment"]["amendment_id"]
        self._ok(self._review("--request-changes", summary="The revision needs a second criterion"))
        # A revise that widens the delta into a second work item refuses and names escalation.
        widened = self._revise([self._update("REQ-003", requirement="[#102] REQ-003 revised too")])
        self.assertEqual(widened.returncode, 1, widened.stdout)
        self.assertTrue(widened.stdout.startswith("AMENDMENT_REFUSED: full_redesign"), widened.stdout)
        self.assertIn("amendment-escalate", widened.stdout)
        self.assertEqual(self.read_status()["amendment"]["review"]["decision"], "changes_requested")
        escalated = self._ok(run(["amendment-escalate", "--by", self.REVIEWER,
                                  "--reason", "The correction spans both items"], cwd=self.tmp))
        self.assertIn("AMENDMENT_ESCALATED", escalated.stdout)
        status = self.read_status()
        self.assertIsNone(status["amendment"])
        self.assertEqual(status["amendments"][-1]["amendment_id"], amendment_id)
        self.assertEqual(status["amendments"][-1]["state"], "escalated")
        self.assertIsNotNone(status["amendments"][-1]["closed_at"])
        self.assertEqual(status["phase_number"], 2)
        self.assertEqual(status["phase"], self.lib.PHASES[2])
        self.assertIsNone(status["design_approved"])
        self.assertIsNone(status["design_review"])
        self.assertEqual(self._kinds()[-1], "amendment_escalated")
        self.assertNotIn("phase_advanced", self._kinds()[-4:])
        # The amended registry stays; nothing waits on the closed amendment.
        self.assertEqual(self._criteria()["REQ-002"]["requirement"], "[#101] REQ-002 revised")
        self.assertEqual(run(["amendment-escalate", "--by", self.REVIEWER, "--reason", "again"], cwd=self.tmp).returncode, 1)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    # -- i42-fail-closed -----------------------------------------------------

    def test_identity_order_and_stale_hash_are_refused_without_writing(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        before = self._snapshot()
        for reviewer in (self.ARCHITECT, "test-architect", "ARCHITECT-1"):
            with self.subTest(reviewer=reviewer):
                refused = self._review(by=reviewer)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("no self-review", refused.stdout)
                self.assertEqual(self._snapshot(), before)
        early = self._approve()
        self.assertEqual(early.returncode, 1, early.stdout)
        self.assertIn("no review yet", early.stdout)
        self.assertEqual(self._snapshot(), before)
        self_approve = self._approve(by=self.ARCHITECT)
        self.assertEqual(self_approve.returncode, 1, self_approve.stdout)
        self.assertEqual(self._snapshot(), before)

        self._ok(self._review())
        # The registry moves between review and approval (an audited commit,
        # so the ledger stays intact and only the amendment hash can catch it).
        cfg = self.lib.load_config(self.tmp)
        status, acceptance = self.read_status(), self.read_acceptance()
        next(c for c in acceptance["criteria"] if c["id"] == "REQ-002")["requirement"] = "[#101] REQ-002 moved again"
        with self.lib.project_lock(self.tmp):
            self.lib.commit(self.tmp, cfg, status=status, acceptance=acceptance,
                            event_kind="test_drift", event_message="registry moved after amendment review")
        drifted = self._snapshot()
        stale = self._approve()
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertTrue(stale.stdout.startswith("SHIP_FEATURE_BLOCKED\n"), stale.stdout)
        self.assertIn("amendment hash", stale.stdout)
        self.assertIn("REQ-002 no longer matches the reviewed amendment", stale.stdout)
        self.assertEqual(self._snapshot(), drifted)
        self.assertEqual(self.read_status()["amendment"]["state"], "open")
        self.assertNotIn("amended_by", self.read_status()["design_approved"])

    def test_restart_and_concurrent_mutation_stay_closed_while_verify_stays_open(self):
        self._phase_4_run(combined_last=True)
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        amendment_id = self.read_status()["amendment"]["amendment_id"]
        # Every command is a fresh process: the record on disk is the only memory.
        self.assertEqual(self.lib.open_amendment(self.read_status())["amendment_id"], amendment_id)
        fresh = run(["advance", "5", "50"], cwd=self.tmp)
        self.assertEqual(fresh.returncode, 1, fresh.stdout)
        self.assertIn(f"amendment gate: amendment {amendment_id} is open", fresh.stdout)
        lower = run(["advance", "3", "40"], cwd=self.tmp)
        self.assertEqual(lower.returncode, 1, lower.stdout)
        status_view = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(status_view["amendment"]["amendment_id"], amendment_id)
        self.assertEqual(status_view["amendment"]["pending_decision"], "review")
        self.assertEqual(status_view["errors"], [])
        # A concurrent criterion mutation under the same lock is refused, verify is not.
        before = self._snapshot()
        mutation = run(["criterion-update", "REQ-001", "--requirement", "[#101] primary respecified"], cwd=self.tmp)
        self.assertEqual(mutation.returncode, 1, mutation.stdout)
        self.assertIn("amendment gate: close or escalate", mutation.stdout)
        self.assertEqual(self._snapshot(), before)
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "implementer-1"], cwd=self.tmp))
        self.assertEqual(self.read_status()["amendment"]["amendment_id"], amendment_id)
        self.assertEqual(self._criteria()["REQ-002"]["state"], "passing")
        # record-evidence stays open too (a browser attestation on the combined criterion).
        recorded = self._ok(run(["record-evidence", "REQ-004", "--kind", "browser", "--by", "implementer-1",
                                 "--description", "Opened the page again while the amendment is open"], cwd=self.tmp))
        self.assertIn("EVIDENCE_RECORDED", recorded.stdout)
        self.assertEqual(self.read_status()["amendment"]["amendment_id"], amendment_id)
        self.assertEqual(self._kinds()[-2:], ["checks_run", "evidence_recorded"])

    def test_dependents_are_same_item_criteria_naming_a_changed_id(self):
        self._phase_4_run()
        acceptance = self.read_acceptance()
        registry = acceptance["work_items"]
        criteria = acceptance["criteria"]
        # REQ-002 names REQ-001 in the same item; REQ-004 names nothing changed.
        self.assertEqual(self.lib.amendment_dependent_ids(criteria, ["REQ-001"], registry), ["REQ-002"])
        self.assertEqual(self.lib.amendment_dependent_ids(criteria, ["REQ-002"], registry), [])
        # A same-text mention in the OTHER item is not a dependency, and a changed id is never its own dependent.
        cross = [dict(c, requirement="[#102] mentions REQ-001 from afar") if c["id"] == "REQ-003" else c
                 for c in criteria]
        self.assertEqual(self.lib.amendment_dependent_ids(cross, ["REQ-001"], registry), ["REQ-002"])
        self.assertEqual(self.lib.amendment_dependent_ids(criteria, ["REQ-001", "REQ-002"], registry), [])
        # REQ-0011 is not REQ-001.
        padded = [dict(c, requirement="[#101] see REQ-0011") if c["id"] == "REQ-002" else c for c in criteria]
        self.assertEqual(self.lib.amendment_dependent_ids(padded, ["REQ-001"], registry), [])
        opened = self._ok(self._open([self._update("REQ-001", requirement="[#101] the primary outcome, sharpened")]))
        self.assertIn("AMENDMENT_OPENED", opened.stdout)
        amendment = self.read_status()["amendment"]
        self.assertEqual(amendment["dependent_ids"], ["REQ-002"])
        self.assertEqual(self._criteria()["REQ-002"]["state"], "passing")
        # A primary_fix respecification also unbinds the original-symptom evidence.
        self.assertFalse(self.read_status()["requirement_coverage"]["original_symptom_resolved"])

    def test_malformed_amendment_state_refuses_every_gate(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        good = self.read_status()
        self.assertEqual(self.lib.validate_status_schema(good), [])
        record = good["amendment"]
        cases = {
            "closed record in the open slot": {"amendment": {**record, "state": "approved", "closed_at": record["opened_at"]}},
            "bad id": {"amendment": {**record, "amendment_id": "am-nope"}},
            "missing key": {"amendment": {k: v for k, v in record.items() if k != "frozen_phase"}},
            "extra key": {"amendment": {**record, "note": "x"}},
            "bad classification": {"amendment": {**record, "classification": "partial"}},
            "bad review": {"amendment": {**record, "review": {"by": "r", "decision": "approved"}}},
            "open record in history": {"amendments": [record]},
            "seventeen closed": {"amendments": [{**record, "state": "escalated", "closed_at": record["opened_at"],
                                                 "amendment_id": f"am-{index:032x}"} for index in range(17)]},
            "bad amended_by": {"design_approved": {**good["design_approved"], "amended_by": ["x"]}},
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                errors = self.lib.validate_status_schema({**good, **patch})
                self.assertTrue(errors, label)
                self.assertTrue(all(e.startswith("status: ") for e in errors), errors)
        self.assertEqual(self.lib.validate_status_schema({**good, "amendment": None, "amendments": []}), [])
        legacy = {k: v for k, v in good.items() if k not in ("amendment", "amendments")}
        self.assertEqual(self.lib.validate_status_schema(legacy), [])

    # -- i42-multi-item-isolation --------------------------------------------

    def test_two_items_amend_independently_with_one_design_approval(self):
        self._phase_4_run()
        before = self._criteria()
        item_two_before = {cid: json.dumps(before[cid], sort_keys=True) for cid in ("REQ-003", "REQ-004")}
        design_review_before = self.read_status()["design_review"]

        # Amendment one: #101 only. Item two is untouched byte for byte.
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 builds on REQ-001, revised")]))
        first = self.read_status()["amendment"]["amendment_id"]
        self.assertEqual(self.read_status()["amendment"]["affected_work_items"], ["issue-101"])
        after_open = self._criteria()
        for cid, frozen in item_two_before.items():
            self.assertEqual(json.dumps(after_open[cid], sort_keys=True), frozen)
        view = json.loads(run(["status"], cwd=self.tmp).stdout)["amendment"]
        self.assertEqual(view["retained_evidence_count"], 3)
        self.assertEqual(view["changed_ids"], ["REQ-002"])
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "implementer-1"], cwd=self.tmp))
        self._ok(self._review())
        self._ok(self._approve())

        # Amendment two: #102 only, with a dependent named in the same item.
        self._ok(self._open([self._update("REQ-004", requirement="[#102] REQ-004 rounds out the second item, revised")]))
        second = self.read_status()["amendment"]
        self.assertNotEqual(second["amendment_id"], first)
        self.assertEqual(second["affected_work_items"], ["issue-102"])
        self.assertEqual(second["changed_ids"], ["REQ-004"])
        self.assertEqual(second["dependent_ids"], [])
        item_one = self._criteria()
        self.assertEqual(item_one["REQ-001"]["state"], "passing")
        self.assertEqual(item_one["REQ-002"]["state"], "passing")
        self.assertEqual(item_one["REQ-003"]["state"], "passing")
        self._ok(run(["verify", "--criterion", "REQ-004", "--by", "implementer-1"], cwd=self.tmp))
        self._ok(self._review())
        self._ok(self._approve())

        status = self.read_status()
        self.assertIsNone(status["amendment"])
        self.assertEqual([item["state"] for item in status["amendments"]], ["approved", "approved"])
        self.assertEqual(status["design_approved"]["amended_by"], [first, second["amendment_id"]])
        self.assertEqual(status["design_review"]["amended_by"], [first, second["amendment_id"]])
        self.assertEqual(status["design_review"]["by"], design_review_before["by"])
        self.assertEqual(status["design_approved"]["design_hash"], self.lib.design_hash(self.read_acceptance()["criteria"]))
        kinds = self._kinds()
        self.assertEqual(kinds.count("design_approved"), 1)
        self.assertEqual(kinds.count("design_review_approved"), 1)
        self.assertEqual(kinds.count("amendment_approved"), 2)
        self.assertEqual(kinds.count("amendment_opened"), 2)
        self.assertEqual(kinds.count("phase_advanced"), 3)
        self.assertEqual(status["phase_number"], 4)
        self.assertTrue(all(c["state"] == "passing" for c in self._criteria().values()))
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        # A change the reviewer rejected never closes: approval is refused,
        # and only a revision (re-reviewed) or an escalation can end it.
        self._ok(self._open([self._update("REQ-003", requirement="[#102] the second item's outcome, weakened")]))
        third = self.read_status()["amendment"]["amendment_id"]
        self._ok(self._review("--request-changes", summary="Do not weaken the outcome"))
        rejected = self._approve()
        self.assertEqual(rejected.returncode, 1, rejected.stdout)
        self.assertIn("requested changes", rejected.stdout)
        self.assertEqual(self.read_status()["amendment"]["amendment_id"], third)
        self.assertEqual(self.read_status()["amendment"]["state"], "open")
        self.assertEqual(run(["advance", "5", "50"], cwd=self.tmp).returncode, 1)
        # Revising clears the stale review; approval still needs a fresh one.
        revised = self._ok(self._revise([self._update("REQ-003", requirement="[#102] the second item's outcome, sharpened")]))
        self.assertIn("AMENDMENT_REVISED", revised.stdout)
        reopened = self.read_status()["amendment"]
        self.assertEqual(reopened["amendment_id"], third)
        self.assertIsNone(reopened["review"])
        self.assertEqual(len(reopened["operations"]), 2)
        self.assertEqual(reopened["amendment_hash"],
                         self.lib.amendment_hash(reopened["base_design_hash"], reopened["operations"]))
        self.assertEqual(self._approve().returncode, 1)
        self.assertEqual(len(self.read_status()["amendments"]), 2)
        self.assertEqual(self._kinds().count("amendment_approved"), 2)

    # -- i42-dashboard -------------------------------------------------------

    def test_snapshot_and_banner_name_the_pending_amendment_decision(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        snapshot = self.dashboard.build_snapshot(self.tmp)
        amendment_id = self.read_status()["amendment"]["amendment_id"]
        view = snapshot["amendment"]
        self.assertEqual(view["amendment_id"], amendment_id)
        self.assertEqual(view["changed_ids"], ["REQ-002"])
        self.assertEqual(view["dependent_ids"], [])
        self.assertEqual(view["affected_work_items"], ["issue-101"])
        self.assertEqual(view["retained_evidence_count"], 3)
        self.assertEqual(view["classification"], "scoped")
        self.assertEqual(view["classification_reasons"], [])
        self.assertEqual([d["decision"] for d in view["required_decisions"]], ["review", "pilot_approval"])
        self.assertEqual([d["status"] for d in view["required_decisions"]], ["pending", "pending"])
        self.assertEqual(view["pending_decision"], "review")
        self.assertNotIn("requirement", json.dumps(view))
        banner = snapshot["input_required"]
        self.assertTrue(banner["required"])
        self.assertEqual(banner["kind"], "amendment_review")
        self.assertIn(amendment_id, banner["message"])
        self.assertIn("amendment review", banner["message"])
        self.assertEqual(snapshot["audit"]["gate_errors"], [])

        self._ok(self._review())
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["amendment"]["pending_decision"], "pilot_approval")
        self.assertEqual(snapshot["amendment"]["required_decisions"][0]["status"], "approved")
        self.assertEqual(snapshot["input_required"]["kind"], "amendment_approval")
        self.assertIn("amendment-approve", snapshot["input_required"]["message"])
        self._ok(self._approve())
        snapshot = self.dashboard.build_snapshot(self.tmp)
        self.assertIsNone(snapshot["amendment"])
        self.assertFalse(snapshot["input_required"]["required"])

    def test_broker_routes_the_lane_and_keeps_approval_human_only(self):
        self._phase_4_run()
        root_text = str(self.tmp.resolve())
        base = {"actor": "supervisor", "project_root": root_text, "action": "workflow"}
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-open",
                                                               "by": self.ARCHITECT, "file": "tx.json",
                                                               "summary": "s", "request_full_redesign": False})
        self.assertEqual(argv[argv.index("amendment-open"):],
                         ["amendment-open", "--file", "tx.json", "--by", self.ARCHITECT, "--summary", "s"])
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-open",
                                                               "by": self.ARCHITECT, "file": "tx.json",
                                                               "request_full_redesign": True})
        self.assertEqual(argv[-1], "--request-full-redesign")
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-review",
                                                               "by": self.REVIEWER, "decision": "request-changes",
                                                               "summary": "needs work"})
        self.assertEqual(argv[argv.index("amendment-review"):],
                         ["amendment-review", "--by", self.REVIEWER, "--request-changes", "--summary", "needs work"])
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-revise",
                                                               "by": self.ARCHITECT, "file": "tx2.json"})
        self.assertEqual(argv[-4:], ["--file", "tx2.json", "--by", self.ARCHITECT])
        argv = self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-escalate",
                                                               "by": self.REVIEWER, "reason": "too wide"})
        self.assertEqual(argv[-4:], ["--by", self.REVIEWER, "--reason", "too wide"])
        self.assertIn("amendment-approve", self.broker.HUMAN_ONLY_COMMANDS)
        with self.assertRaisesRegex(self.lib.HandsoffError, "human-only command: amendment-approve"):
            self.broker._workflow_argv(self.tmp.resolve(), {**base, "command": "amendment-approve", "by": self.PILOT})
        for request in (
            {**base, "command": "amendment-open", "by": self.ARCHITECT},
            {**base, "command": "amendment-open", "by": self.ARCHITECT, "file": "tx.json", "request_full_redesign": "yes"},
            {**base, "command": "amendment-review", "by": self.REVIEWER, "decision": "reject", "summary": "x"},
            {**base, "command": "amendment-revise", "by": self.ARCHITECT, "file": "tx.json", "summary": "x"},
            {**base, "command": "amendment-escalate", "by": self.REVIEWER},
        ):
            with self.subTest(request=request):
                with self.assertRaises(self.lib.HandsoffError):
                    self.broker._workflow_argv(self.tmp.resolve(), request)

    def test_lane_refuses_before_design_approval(self):
        self.init("Close #101 and #102")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "[#101] the primary outcome",
                      "--test", "true"], cwd=self.tmp))
        before = self._snapshot()
        early = self._open([self._update("REQ-001", requirement="[#101] the primary outcome, revised")])
        self.assertEqual(early.returncode, 1, early.stdout)
        self.assertIn("Phase 3 or later", early.stdout)
        self.assertEqual(self._snapshot(), before)

    def test_only_one_amendment_is_open_at_a_time(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-002", requirement="[#101] REQ-002 revised")]))
        before = self._snapshot()
        second = self._open([self._update("REQ-001", requirement="[#101] the primary outcome, revised")], name="tx2.json")
        self.assertEqual(second.returncode, 1, second.stdout)
        self.assertIn("is already open", second.stdout)
        self.assertEqual(self._snapshot(), before)

    def test_readme_documents_the_lane(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("(#42)", readme)
        self.assertIn("### Amendment lane", readme)
        section = readme[readme.index("### Amendment lane"):readme.index("## Work items and the per-item")]
        for name in ("amendment-open", "amendment-review", "amendment-approve", "amendment-escalate",
                     "amendment-revise", "full_redesign", "amendment_opened", "amendment_approved"):
            self.assertIn(name, section)
        self.assertNotIn("\u2014", section)


class TestArchitectDesignReview(HandsoffTestCase):
    """AR7: Phase-2 design critique is independently recorded and bound
    to the exact criteria specification before implementation can begin."""

    def _prepare(self):
        self.init("AR7 fixture")
        criterion = run(["criterion-update", "REQ-001", "--requirement",
                         "A real, independently reviewable design criterion"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        phase2 = run(["advance", "2", "20", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(phase2.returncode, 0, phase2.stdout + phase2.stderr)

    def _design_review(self, *decision, by="design-reviewer", architect="architect-1", summary="Design is sound"):
        return run(["record-design-review", "--by", by, "--architect", architect,
                    "--summary", summary, *decision], cwd=self.tmp)

    def _human_approve(self, architect="architect-1"):
        return run(["design-approve", "--by", "moncy", "--architect", architect,
                    "--summary", "Approved AR7 fixture design"], cwd=self.tmp)

    def _write_status(self, status):
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted status fields directly")

    def test_phase_3_requires_current_approved_independent_design_review(self):
        self._prepare()
        self.assertTrue(self.read_status()["requires_design_review"])
        self.assertEqual(self._human_approve().returncode, 0)

        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("design review gate", blocked.stdout)
        self.assertEqual(self.read_status()["phase_number"], 2)

        reviewed = self._design_review("--approve")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        self.assertEqual(self.read_status()["status"], "in_progress",
                         "a current earlier human approval must not leave the run falsely blocked")
        advanced = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def test_design_reviewer_must_differ_from_architect(self):
        self._prepare()
        for reviewer in ("architect-1", " Architect-1 ", "ARCHITECT-1"):
            refused = self._design_review("--approve", by=reviewer)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("self-review", refused.stdout)
        self.assertIsNone(self.read_status()["design_review"])

    def test_changes_requested_sends_design_back_for_revision(self):
        self._prepare()
        self.assertEqual(self._human_approve().returncode, 0)
        finding = self._design_review("--request-changes", summary="Missing failure-mode criterion")
        self.assertEqual(finding.returncode, 0, finding.stdout + finding.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 2)
        self.assertEqual(status["design_review"]["decision"], "changes_requested")
        self.assertIsNone(status["design_approved"])
        self.assertIn("revises", status["next_action"])
        self.assertIn('"kind":"design_review_changes_requested"',
                      (self.tmp / "handsoff-events.jsonl").read_text())

    def test_criteria_mutation_invalidates_design_review(self):
        self._prepare()
        self.assertEqual(self._design_review("--approve").returncode, 0)
        self.assertEqual(self._human_approve().returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)

        changed = run(["criterion-update", "REQ-001", "--requirement",
                       "A revised independently reviewable criterion"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 2)
        self.assertIsNone(status["design_review"])
        self.assertIsNone(status["design_approved"])

    def test_legacy_run_without_review_flag_is_unaffected(self):
        self._prepare()
        status = self.read_status()
        status.pop("requires_design_review")
        status.pop("design_review")
        self._write_status(status)
        self.assertEqual(self._human_approve().returncode, 0)
        advanced = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def test_design_review_is_schema_validated_and_hash_chained(self):
        self._prepare()
        reviewed = self._design_review("--approve", summary="Independent critique completed")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        record = self.read_status()["design_review"]
        self.assertEqual(record["by"], "design-reviewer")
        self.assertEqual(record["architect"], "architect-1")
        self.assertEqual(record["decision"], "approved")
        self.assertTrue(record["design_hash"])
        self.assertTrue(record["config_hash"])
        self.assertIn('"kind":"design_review_approved"',
                      (self.tmp / "handsoff-events.jsonl").read_text())
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dashboard
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["actors"]["architect"], "architect-1")
        self.assertEqual(snapshot["actors"]["design_reviewed_by"], "design-reviewer")
        self.assertEqual(snapshot["crew"][0]["actor"], "architect-1")
        self.assertIsNone(snapshot["crew"][0]["session"])
        self.assertIn('data-role="architect"', (ROOT / "dashboard" / "index.html").read_text())
        design_reviewer_row = next(row for row in snapshot["crew"] if row["key"] == "design_reviewer")
        self.assertEqual(design_reviewer_row["label"], "DESIGN REVIEWER")
        self.assertEqual(design_reviewer_row["actor"], "design-reviewer")

        status = self.read_status()
        status["design_review"]["by"] = ""
        self._write_status(status)
        invalid = run(["validate"], cwd=self.tmp)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("design_review.by", invalid.stdout)
        self.assertNotIn("Traceback", invalid.stdout + invalid.stderr)


class TestDesignReviewAttemptBudget(HandsoffTestCase):
    """Issue #35: design-review attempts bypassed the round caps. One real
    run burned five design reviews while status reported design_round 0,
    because `design_round` only moves when someone passes
    `--new-design-round`, and nothing counted the reviews themselves. Fix:
    every record-design-review increments a cumulative
    `design_review_attempts`; past `[workflow] max_autonomous_design_reviews`
    (default 2) a record and a managed Phase-2 reviewer launch are both
    refused until the Pilot runs `design-review-authorize`, which permits
    exactly one more attempt, reserved atomically by a managed launch under
    the project lock or consumed directly by a human-recorded review.
    Criteria i35-attempt-count (primary_fix), i35-budget-refusal,
    i35-pilot-authorize-one, i35-count-survives-mutation,
    i35-dashboard-round-label (the node test covers the rendering)."""

    AUTHORIZE_COMMAND = "handsoff_supervisor.py design-review-authorize --by <pilot>"

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_broker
        import handsoff_lib
        self.runtime = handsoff_agent
        self.broker = handsoff_broker
        self.lib = handsoff_lib

    def _prepare(self):
        """Phase 2 with a real criterion and design_round still 0: no
        --new-design-round is ever passed in this class, which is exactly
        the shape the original symptom had."""
        self.init("Issue 35 fixture")
        criterion = run(["criterion-update", "REQ-001", "--requirement",
                         "A real, independently reviewable design criterion"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        phase2 = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase2.returncode, 0, phase2.stdout + phase2.stderr)
        self.assertEqual(self.read_status()["design_round"], 0)
        self.assertEqual(self.read_status()["design_review_attempts"], 0)
        self.assertIsNone(self.read_status()["design_review_authorization"])

    def _review(self, decision="--request-changes", summary="Missing failure-mode criterion"):
        return run(["record-design-review", "--by", "design-reviewer", "--architect", "architect-1",
                    "--summary", summary, decision], cwd=self.tmp)

    def _authorize(self, by="moncy", note=None):
        args = ["design-review-authorize", "--by", by]
        if note:
            args += ["--note", note]
        return run(args, cwd=self.tmp)

    def _exhaust(self):
        """Two change requests: the default autonomous budget, spent."""
        self._prepare()
        for expected in (1, 2):
            reviewed = self._review()
            self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
            self.assertEqual(self.read_status()["design_review_attempts"], expected)

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if line.strip()]

    def _files_snapshot(self):
        return {name: (self.tmp / name).read_bytes()
                for name in ("handsoff-status.json", "handsoff-events.jsonl")}

    def _launch(self, sid=None):
        return self.lib.create_agent_session(
            self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
            requested_model="default", resolution_source="configured",
            id_factory=(lambda: sid) if sid else None,
        )

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def test_default_budget_is_configured_and_governance_bound(self):
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(cfg["max_autonomous_design_reviews"], 2)
        self.assertEqual(self.lib.DEFAULT_CONFIG["max_autonomous_design_reviews"], 2)
        self.assertIn("max_autonomous_design_reviews", self.lib.GOVERNANCE_CONFIG_KEYS)
        # Legacy compatibility: an absent (default) key must leave every
        # already-recorded decision's config_hash byte-identical, or
        # upgrading bin/ would invalidate in-flight runs everywhere.
        legacy_keys = tuple(k for k in self.lib.GOVERNANCE_CONFIG_KEYS if k != "max_autonomous_design_reviews")
        legacy_hash = self.lib.hashlib.sha256(
            self.lib._canonical({k: cfg.get(k) for k in legacy_keys}).encode("utf-8")).hexdigest()
        self.assertEqual(self.lib.config_hash(cfg), legacy_hash)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("stall_minutes = 10", "stall_minutes = 10\nmax_autonomous_design_reviews = 1"))
        changed = self.lib.load_config(self.tmp)
        self.assertEqual(changed["max_autonomous_design_reviews"], 1)
        self.assertNotEqual(self.lib.config_hash(changed), legacy_hash)
        toml.write_text(toml.read_text().replace("max_autonomous_design_reviews = 1", "max_autonomous_design_reviews = -1"))
        with self.assertRaisesRegex(self.lib.HandsoffError, "max_autonomous_design_reviews must not be negative"):
            self.lib.load_config(self.tmp)
        toml.write_text(toml.read_text().replace("max_autonomous_design_reviews = -1", 'max_autonomous_design_reviews = "2"'))
        with self.assertRaisesRegex(self.lib.HandsoffError, "workflow.max_autonomous_design_reviews must be an integer"):
            self.lib.load_config(self.tmp)

    def test_changing_the_budget_invalidates_a_recorded_design_review(self):
        self._prepare()
        self.assertEqual(self._review("--approve", summary="Design is sound").returncode, 0)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("stall_minutes = 10", "stall_minutes = 10\nmax_autonomous_design_reviews = 5"))
        approval = run(["design-approve", "--by", "moncy", "--architect", "architect-1",
                        "--summary", "Approved fixture design"], cwd=self.tmp)
        self.assertEqual(approval.returncode, 0, approval.stdout + approval.stderr)
        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("workflow policy changed since design review", blocked.stdout)

    def test_every_record_increments_the_cumulative_attempt_count(self):
        """i35-attempt-count: approve and request-changes both count, the
        event carries the number, and design_round never moved."""
        self._prepare()
        approved = self._review("--approve", summary="Design is sound")
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 1)
        self.assertEqual(status["design_round"], 0)
        changes = self._review()
        self.assertEqual(changes.returncode, 0, changes.stdout + changes.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 2)
        self.assertEqual(status["design_round"], 0)
        review_events = [e for e in self._events()
                         if e["kind"] in {"design_review_approved", "design_review_changes_requested"}]
        self.assertEqual([e["design_review_attempt"] for e in review_events], [1, 2])
        self.assertEqual([e["design_review_limit"] for e in review_events], [2, 2])
        self.assertEqual([e["authorized"] for e in review_events], [False, False])
        self.assertFalse(any(e["kind"] == "design_round_advanced" for e in self._events()))
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)
        # A fresh process reads the same number back.
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(payload["design_review_attempts"], 2)
        self.assertEqual(payload["design_review_budget"]["limit"], 2)
        self.assertTrue(payload["design_review_budget"]["exhausted"])

    def test_third_attempt_is_refused_and_phase_3_stays_closed(self):
        """i35-budget-refusal: exact blocked text, blocked status carrying
        the authorization command, and no auto-approval of the design."""
        self._exhaust()
        status = self.read_status()
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["next_action"],
                         f"design review budget exhausted (2/2); Pilot must run {self.AUTHORIZE_COMMAND} "
                         "to permit one more attempt")
        self.assertEqual(status["design_review"]["decision"], "changes_requested")
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("design_review_budget_exhausted", kinds)
        exhausted = next(e for e in self._events() if e["kind"] == "design_review_budget_exhausted")
        self.assertEqual((exhausted["design_review_attempts"], exhausted["design_review_limit"]), (2, 2))
        before = self._files_snapshot()
        refused = self._review()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertEqual(refused.stdout.strip(),
                         "SHIP_FEATURE_BLOCKED: design review budget exhausted (2/2); Pilot must run "
                         f"{self.AUTHORIZE_COMMAND} to permit one more attempt")
        self.assertEqual(self._files_snapshot(), before)
        self.assertEqual(self.read_status()["design_review_attempts"], 2)
        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("design review gate", blocked.stdout)
        self.assertEqual(self.read_status()["phase_number"], 2)

    def test_approval_at_the_limit_is_not_exhaustion(self):
        """Reaching the budget with an approval leaves the design approved
        and the run waiting on human approval, exactly as before #35."""
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        approved = self._review("--approve", summary="Revised design is sound")
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 2)
        self.assertNotIn("design_review_budget_exhausted", [e["kind"] for e in self._events()])
        self.assertIn("human approval", status["next_action"])

    def test_pilot_authorization_permits_exactly_one_human_recorded_attempt(self):
        """i35-pilot-authorize-one: a direct record consumes the
        authorization; the next record is refused; authorize is refused
        before exhaustion and while one is unconsumed."""
        self._prepare()
        early = self._authorize()
        self.assertEqual(early.returncode, 1, early.stdout + early.stderr)
        self.assertIn("not exhausted (0/2); nothing to authorize", early.stdout)
        self.assertIsNone(self.read_status()["design_review_authorization"])
        for _ in range(2):
            self.assertEqual(self._review().returncode, 0)
        blank = self._authorize(by="   ")
        self.assertEqual(blank.returncode, 1)
        self.assertIn("--by must be a non-empty string", blank.stdout)
        granted = self._authorize(note="one more pass on the failure-mode criterion")
        self.assertEqual(granted.returncode, 0, granted.stdout + granted.stderr)
        self.assertEqual(granted.stdout.strip(), "DESIGN_REVIEW_ATTEMPT_AUTHORIZED: 3")
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 2, "authorize never counts as an attempt")
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(status["next_action"], "Launch the authorized design-review attempt 3")
        authorization = status["design_review_authorization"]
        self.assertEqual(authorization["by"], "moncy")
        self.assertEqual(authorization["attempt_permitted"], 3)
        self.assertEqual(authorization["note"], "one more pass on the failure-mode criterion")
        self.assertIsNone(authorization["launch_session_id"])
        self.assertIsNone(authorization["consumed_at"])
        granted_event = next(e for e in self._events() if e["kind"] == "design_review_attempt_authorized")
        self.assertEqual(granted_event["attempt_permitted"], 3)
        self.assertEqual(granted_event["by"], "moncy")
        again = self._authorize()
        self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
        self.assertIn("unconsumed design-review authorization already exists", again.stdout)
        third = self._review()
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 3)
        self.assertIsNotNone(status["design_review_authorization"]["consumed_at"])
        self.assertIsNone(status["design_review_authorization"]["launch_session_id"])
        third_event = [e for e in self._events() if e["kind"] == "design_review_changes_requested"][-1]
        self.assertEqual(third_event["design_review_attempt"], 3)
        self.assertTrue(third_event["authorized"])
        self.assertEqual(status["status"], "blocked")
        self.assertIn(self.AUTHORIZE_COMMAND, status["next_action"])
        fourth = self._review()
        self.assertEqual(fourth.returncode, 1, fourth.stdout + fourth.stderr)
        self.assertIn("design review budget exhausted (3/2)", fourth.stdout)
        with self.assertRaisesRegex(self.lib.HandsoffError, "design-review-authorize"):
            self._launch()
        self.assertEqual(self.read_status()["design_review_attempts"], 3)
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_managed_launch_reserves_the_authorization_atomically(self):
        """i35-pilot-authorize-one, managed path: the first reviewer launch
        binds launch_session_id, a second launch before any record is
        refused with status, agent_sessions, and the event log byte-
        identical, and the record made after that session ends consumes
        the same authorization."""
        self._exhaust()
        with self.assertRaisesRegex(self.lib.HandsoffError, "design review budget exhausted \\(2/2\\)"):
            self._launch()
        self.assertEqual(self.read_status().get("agent_sessions"), None)
        self.assertEqual(self._authorize().returncode, 0)
        session = self._launch(self._sid(1))
        self.assertEqual(session["session_id"], self._sid(1))
        status = self.read_status()
        self.assertEqual(status["design_review_authorization"]["launch_session_id"], self._sid(1))
        self.assertIsNone(status["design_review_authorization"]["consumed_at"])
        self.assertEqual(status["design_review_attempts"], 2, "a launch is not yet an attempt")
        launching = next(e for e in self._events() if e["kind"] == "agent_session_launching")
        self.assertEqual(launching["design_review_attempt"], 3)
        self.assertTrue(launching["design_review_authorization_reserved"])
        before = self._files_snapshot()
        with self.assertRaisesRegex(self.lib.HandsoffError,
                                    f"already reserved by session {self._sid(1)}"):
            self._launch(self._sid(2))
        self.assertEqual(self._files_snapshot(), before)
        self.assertEqual(list(self.read_status()["agent_sessions"]), [self._sid(1)])
        self.lib.transition_agent_session(self.tmp, self._sid(1), "running")
        self.lib.transition_agent_session(self.tmp, self._sid(1), "completed", exit_code=0)
        recorded = self._review()
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_attempts"], 3)
        self.assertIsNotNone(status["design_review_authorization"]["consumed_at"])
        self.assertEqual(status["design_review_authorization"]["launch_session_id"], self._sid(1),
                         "the audit trail of which session performed the attempt is kept")
        with self.assertRaisesRegex(self.lib.HandsoffError, "design review budget exhausted \\(3/2\\)"):
            self._launch(self._sid(3))
        self.assertEqual(list(self.read_status()["agent_sessions"]), [self._sid(1)])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_two_concurrent_launches_reserve_exactly_once(self):
        self._exhaust()
        self.assertEqual(self._authorize().returncode, 0)
        results = []
        barrier = threading.Barrier(2)

        def launch(number):
            barrier.wait()
            try:
                results.append(("ok", self._launch(self._sid(number))["session_id"]))
            except self.lib.HandsoffError as exc:
                results.append(("refused", str(exc)))

        threads = [threading.Thread(target=launch, args=(n,)) for n in (11, 12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(kind for kind, _ in results), ["ok", "refused"], results)
        winner = next(value for kind, value in results if kind == "ok")
        status = self.read_status()
        self.assertEqual(list(status["agent_sessions"]), [winner])
        self.assertEqual(status["design_review_authorization"]["launch_session_id"], winner)
        self.assertEqual(sum(e["kind"] == "agent_session_launching" for e in self._events()), 1)
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_replacement_session_continues_the_same_reserved_attempt(self):
        self._exhaust()
        self.assertEqual(self._authorize().returncode, 0)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            "reviewer = []", 'reviewer = [{ adapter = "claude", model = "independent" }]', 1))
        # The reviewer fallback planner needs an implementer to stay
        # independent from; a completed implementer session provides it
        # and is itself never budgeted.
        implementer = self.lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer", adapter="codex",
            requested_model="impl", resolution_source="configured", id_factory=lambda: self._sid(9))
        self.lib.transition_agent_session(self.tmp, implementer["session_id"], "running")
        self.lib.transition_agent_session(self.tmp, implementer["session_id"], "completed", exit_code=0)
        self._launch(self._sid(1))
        self.lib.transition_agent_session(self.tmp, self._sid(1), "running")
        failure = self.lib.classify_runtime_failure(exit_code=1)
        self.lib.transition_agent_session(self.tmp, self._sid(1), "failed", exit_code=1, failure=failure)
        record = self.lib.reserve_agent_replacement(
            self.tmp, from_session_id=self._sid(1),
            which=lambda name: f"/bin/{name}",
            snapshotter=lambda root: {"head": None, "branch": None, "dirty": False},
            session_id_factory=lambda: self._sid(2),
        )
        self.assertEqual(record["action"], "launch", record)
        self.assertEqual(self.read_status()["design_review_authorization"]["launch_session_id"], self._sid(1))
        self.lib.claim_precreated_agent_session(
            self.tmp, self._sid(2), role="reviewer", adapter="claude", requested_model="independent")
        status = self.read_status()
        self.assertEqual(status["design_review_authorization"]["launch_session_id"], self._sid(2))
        self.assertIsNone(status["design_review_authorization"]["consumed_at"])
        self.assertEqual(status["design_review_attempts"], 2, "a replacement is never a second attempt")

    def test_count_survives_mutation_override_and_restart(self):
        """i35-count-survives-mutation."""
        self._prepare()
        self.assertEqual(self._review("--approve", summary="Design is sound").returncode, 0)
        self.assertEqual(self.read_status()["design_review_attempts"], 1)
        changed = run(["criterion-update", "REQ-001", "--requirement",
                       "A revised independently reviewable criterion"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        status = self.read_status()
        self.assertIsNone(status["design_review"], "the mutation still clears the review record")
        self.assertEqual(status["design_review_attempts"], 1)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "Another",
                     "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        removed = run(["criterion-remove", "REQ-002"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(self.read_status()["design_review_attempts"], 1)
        override = run(["advance", "2", "25", "--design-round", "0"], cwd=self.tmp)
        self.assertEqual(override.returncode, 0, override.stdout + override.stderr)
        self.assertEqual(self.read_status()["design_review_attempts"], 1)
        self.assertEqual(self.read_status()["design_round"], 0)
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(payload["design_review_attempts"], 1)
        self.assertEqual(payload["design_review_budget"]["next_attempt"], 2)
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard as dashboard
        policy = dashboard.build_snapshot(self.tmp)["policy"]
        self.assertEqual(policy["design_review_attempts"], 1)
        self.assertEqual(policy["max_autonomous_design_reviews"], 2)
        self.assertIsNone(policy["design_review_authorization"])

    def test_hand_edited_decrement_is_refused_with_an_audit_block(self):
        self._exhaust()
        status_file = self.tmp / "handsoff-status.json"
        status = self.read_status()
        status["design_review_attempts"] = 0
        status_file.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
        refused = self._review()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("SHIP_FEATURE_BLOCKED", refused.stdout)
        self.assertIn("status file does not match the state recorded by the latest event", refused.stdout)
        self.assertEqual(self.read_status()["design_review_attempts"], 0, "nothing was written over the edit")
        self.assertNotIn("design_review_attempt\":3", (self.tmp / "handsoff-events.jsonl").read_text())

    def test_malformed_budget_fields_refuse_cleanly(self):
        self._prepare()
        cfg = self.lib.load_config(self.tmp)
        base = self.read_status()
        cases = [
            ({"design_review_attempts": -1}, "design_review_attempts"),
            ({"design_review_attempts": "2"}, "design_review_attempts"),
            ({"design_review_attempts": True}, "design_review_attempts"),
            ({"design_review_authorization": "yes"}, "design_review_authorization"),
            ({"design_review_authorization": {"by": "moncy"}}, "exactly the keys"),
            ({"design_review_authorization": {"by": "", "at": "2026-01-01T00:00:00+00:00", "note": None,
                                              "attempt_permitted": 3, "launch_session_id": None,
                                              "consumed_at": None}}, "design_review_authorization.by"),
            ({"design_review_authorization": {"by": "moncy", "at": "2026-01-01T00:00:00", "note": None,
                                              "attempt_permitted": 3, "launch_session_id": None,
                                              "consumed_at": None}}, "must include a timezone"),
            ({"design_review_authorization": {"by": "moncy", "at": "2026-01-01T00:00:00+00:00", "note": None,
                                              "attempt_permitted": 0, "launch_session_id": None,
                                              "consumed_at": None}}, "attempt_permitted"),
            ({"design_review_authorization": {"by": "moncy", "at": "2026-01-01T00:00:00+00:00", "note": None,
                                              "attempt_permitted": 3, "launch_session_id": "nope",
                                              "consumed_at": None}}, "launch_session_id"),
        ]
        for patch, expected in cases:
            with self.subTest(patch=patch):
                errors = self.lib.validate_status_schema({**base, **patch})
                self.assertTrue(any(expected in e for e in errors), errors)
        self.assertEqual(self.lib.validate_status_schema(base), [])
        # A legacy status without either field is valid and reads as budget 0/2.
        legacy = {k: v for k, v in base.items()
                  if k not in ("design_review_attempts", "design_review_authorization")}
        self.assertEqual(self.lib.validate_status_schema(legacy), [])
        budget = self.lib.design_review_budget(legacy, cfg)
        self.assertEqual((budget["attempts"], budget["limit"], budget["exhausted"]), (0, 2, False))
        status = self.read_status()
        status["design_review_attempts"] = "two"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
        invalid = run(["validate"], cwd=self.tmp)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("design_review_attempts", invalid.stdout)
        self.assertNotIn("Traceback", invalid.stdout + invalid.stderr)

    def test_build_launch_spec_refuses_early_and_writes_nothing(self):
        """i35-budget-refusal, launch side: the refusal happens inside
        build_launch_spec before any session, event, or authorization
        write; after authorization the same call succeeds."""
        self._exhaust()
        which = lambda name: f"/bin/{name}" if name == "codex" else None
        before = self._files_snapshot()
        with self.assertRaisesRegex(self.lib.HandsoffError, "design-review-authorize") as ctx:
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the design", which=which)
        self.assertIn("design review budget exhausted (2/2)", str(ctx.exception))
        self.assertEqual(self._files_snapshot(), before)
        self.assertIsNone(self.read_status().get("agent_sessions"))
        self.assertIsNone(self.read_status()["design_review_authorization"])
        # Other roles, and the reviewer outside Phase 2, are never budgeted.
        spec = self.runtime.build_launch_spec(self.tmp, "implementer", "Implement", which=which)
        self.assertEqual(spec.role, "implementer")
        self.assertEqual(self._authorize().returncode, 0)
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the design", which=which)
        self.assertEqual(spec.role, "reviewer")
        self.assertEqual(self._files_snapshot()["handsoff-events.jsonl"],
                         (self.tmp / "handsoff-events.jsonl").read_bytes(),
                         "building a spec never launches or reserves")
        self.assertIsNone(self.read_status()["design_review_authorization"]["launch_session_id"])
        # Once a managed launch holds the reservation, the pre-check names it too.
        self._launch(self._sid(1))
        with self.assertRaisesRegex(self.lib.HandsoffError, f"already reserved by session {self._sid(1)}"):
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the design", which=which)

    def test_broker_refuses_design_review_authorize(self):
        self._exhaust()
        before = self._files_snapshot()
        workflow = mock.Mock()
        request = {
            "actor": "supervisor", "project_root": str(self.tmp.resolve()),
            "action": "workflow", "command": "design-review-authorize", "by": "supervisor",
        }
        with self.assertRaisesRegex(self.lib.HandsoffError, "human-only command: design-review-authorize"):
            self.broker.dispatch_supervisor_request(
                self.tmp, request, workflow_popen=workflow, agent_launcher=mock.Mock())
        workflow.assert_not_called()
        self.assertEqual(self._files_snapshot(), before)
        self.assertIn("design-review-authorize", self.broker.HUMAN_ONLY_COMMANDS)


class TestDesignReviewPacket(HandsoffTestCase):
    """Issue #36: after the first full design review, a follow-up reviewer
    receives a bounded delta packet (criteria delta, dispositioned prior
    findings, evidence states, repository identity) in front of its role
    prompt instead of re-deriving the whole design. The first review always
    gets the full task; a packet built for another design hash or attempt is
    never injected. Criteria i36-first-review-full, i36-delta-packet,
    i36-stale-context, i36-telemetry."""

    PACKET_HEADING = "# Delta review packet"

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_broker
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.broker = handsoff_broker
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.which = lambda name: f"/bin/{name}" if name == "codex" else None

    def _git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def _git_fixture(self):
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("A = 1\n")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "Handsoff Fixture")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "fixture")

    def _prepare(self):
        """Phase 2 with a real criterion and no review recorded yet."""
        self.init("Issue 36 fixture")
        criterion = run(["criterion-update", "REQ-001", "--requirement",
                         "A real, independently reviewable design criterion"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        phase2 = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase2.returncode, 0, phase2.stdout + phase2.stderr)

    def _review(self, *findings, decision="--request-changes", summary="Design needs work"):
        args = ["record-design-review", "--by", "design-reviewer", "--architect", "architect-1",
                "--summary", summary, decision]
        for finding in findings:
            args += ["--finding", finding]
        return run(args, cwd=self.tmp)

    def _packet(self, *dispositions, by="supervisor"):
        args = ["design-review-packet", "--by", by]
        for disposition in dispositions:
            args += ["--disposition", disposition]
        return run(args, cwd=self.tmp)

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if line.strip()]

    def _files_snapshot(self):
        return {name: (self.tmp / name).read_bytes()
                for name in ("handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl")}

    def _reviewed_with_edit(self):
        """One change request with three findings, then a criterion edit:
        the canonical shape a second attempt starts from."""
        self._prepare()
        reviewed = self._review("No failure-mode criterion", "Tests name no fixture", "Rollback path missing")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        edited = run(["criterion-update", "REQ-001", "--requirement",
                      "A real criterion, revised with a failure mode"], cwd=self.tmp)
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def test_first_review_gets_the_full_task_and_no_packet(self):
        """i36-first-review-full."""
        self._prepare()
        before = self._files_snapshot()
        refused = self._packet()
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("SHIP_FEATURE_BLOCKED: no design review has been recorded yet", refused.stdout)
        self.assertIn("full task", refused.stdout)
        self.assertEqual(self._files_snapshot(), before, "a refusal writes nothing")
        self.assertNotIn("design_review_packet", self.read_status())
        text = self.runtime.build_role_input(self.tmp, "reviewer", "Review the design")
        self.assertNotIn(self.PACKET_HEADING, text)
        self.assertTrue(text.startswith((self.tmp / "prompts" / "reviewer.md").read_text().rstrip()))
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the design", which=self.which)
        self.assertIsNone(spec.packet_id)
        self.assertIsNone(spec.design_hash)
        self.assertNotIn(self.PACKET_HEADING, spec.stdin)
        self.assertEqual(self._files_snapshot(), before, "building a spec writes nothing")

    def test_record_design_review_stores_bounded_findings_and_history(self):
        self._prepare()
        before = self._files_snapshot()
        too_many = self._review(*[f"finding {n}" for n in range(33)])
        self.assertEqual(too_many.returncode, 1, too_many.stdout)
        self.assertIn("at most 32 --finding entries", too_many.stdout)
        too_long = self._review("x" * 513)
        self.assertEqual(too_long.returncode, 1, too_long.stdout)
        self.assertIn("exceeds 512 characters", too_long.stdout)
        empty = self._review("   ")
        self.assertEqual(empty.returncode, 1, empty.stdout)
        self.assertIn("--finding 1 must be a non-empty string", empty.stdout)
        self.assertEqual(self._files_snapshot(), before, "finding refusals write nothing")
        reviewed = self._review("  First finding  ", "Second finding")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        status = self.read_status()
        review = status["design_review"]
        self.assertEqual(review["attempt"], 1)
        self.assertIsNone(review["head"], "not a git repository: head is recorded as null")
        self.assertEqual(review["findings"], [{"id": "F1.1", "text": "First finding"},
                                              {"id": "F1.2", "text": "Second finding"}])
        history = status["design_review_history"]
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(set(entry), set(self.lib.DESIGN_REVIEW_HISTORY_FIELDS))
        self.assertEqual(entry["attempt"], 1)
        self.assertEqual(entry["decision"], "changes_requested")
        self.assertEqual(entry["by"], "design-reviewer")
        self.assertEqual(entry["design_hash"], review["design_hash"])
        self.assertEqual(entry["criteria_ids"], ["REQ-001"])
        self.assertEqual(entry["criterion_hashes"],
                         {"REQ-001": self.lib.criterion_spec_hash(self.read_acceptance()["criteria"][0])})
        self.assertIs(entry["structural_blocker"], False)
        self.assertEqual(entry["findings"], review["findings"])
        event = [e for e in self._events() if e["kind"] == "design_review_changes_requested"][-1]
        self.assertEqual(event["findings"], 2)
        self.assertNotIn("First finding", json.dumps(event), "events carry counts, never finding text")
        self.assertEqual(self.lib.validate_status_schema(status), [])
        # The history is bounded to the last 8 records.
        for n in range(9):
            status = self.read_status()
            status["design_review_history"] = status["design_review_history"] + [
                dict(entry, attempt=entry["attempt"] + n + 1)]
            self.assertEqual(self.lib.validate_status_schema(status),
                             [] if len(status["design_review_history"]) <= 8 else
                             ["status: 'design_review_history' must contain at most 8 entries"])
        packed = self.read_status()
        self.lib.append_design_review_history(packed, dict(entry, attempt=99))
        for n in range(9):
            self.lib.append_design_review_history(packed, dict(entry, attempt=100 + n))
        self.assertEqual(len(packed["design_review_history"]), 8)
        self.assertEqual(packed["design_review_history"][-1]["attempt"], 108)

    def test_delta_packet_after_a_change_request_and_a_criterion_edit(self):
        """i36-delta-packet: attempt 2, changed criterion, the three
        dispositions distinguishable, new findings, deterministic bytes."""
        self._reviewed_with_edit()
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement",
                     "A second criterion added after the first review", "--verification", "automated",
                     "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        generated = self._packet("F1.1=resolved", "F1.2=rejected:  the fixture is named in the test list  ")
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        first_line, _, body = generated.stdout.partition("\n")
        self.assertRegex(first_line, r"^DESIGN_REVIEW_PACKET_GENERATED: [0-9a-f]{32} attempt 2 \(\d+ bytes\)$")
        packet = json.loads(body)
        status = self.read_status()
        self.assertEqual(status["design_review_packet"], packet)
        self.assertEqual(self.lib.validate_status_schema(status), [])
        self.assertEqual(packet["attempt"], 2)
        self.assertEqual(packet["previous_attempt"], 1)
        self.assertEqual(packet["design_hash"], self.lib.design_hash(self.read_acceptance()["criteria"]))
        self.assertEqual(packet["previous_design_hash"], status["design_review_history"][-1]["design_hash"])
        self.assertNotEqual(packet["design_hash"], packet["previous_design_hash"])
        self.assertEqual(packet["criteria_delta"],
                         {"added": ["REQ-002"], "removed": [], "changed": ["REQ-001"], "unchanged": []})
        self.assertEqual(packet["findings"], [
            {"id": "F1.1", "text": "No failure-mode criterion", "disposition": "resolved", "note": None},
            {"id": "F1.2", "text": "Tests name no fixture", "disposition": "rejected",
             "note": "the fixture is named in the test list"},
            {"id": "F1.3", "text": "Rollback path missing", "disposition": "unresolved", "note": None},
        ])
        self.assertEqual(packet["dispositions"],
                         {"resolved": ["F1.1"], "rejected": ["F1.2"], "unresolved": ["F1.3"]})
        self.assertEqual(packet["new_findings_since"], ["F1.1", "F1.2", "F1.3"],
                         "every finding of the only earlier attempt is new")
        self.assertEqual(packet["evidence"], [])
        self.assertEqual(packet["repository"], {"head": None, "branch": None, "dirty": None})
        self.assertIsNone(packet["previous_head"])
        self.assertIsNone(packet["files_changed_since_previous"])
        self.assertTrue(packet["stale"])
        self.assertEqual(packet["stale_reasons"], ["previous review recorded no repository head"])
        self.assertEqual(packet["instructions"], self.lib.DESIGN_REVIEW_PACKET_INSTRUCTIONS)
        self.assertNotIn("truncated", packet)
        self.assertNotIn("at", packet, "no timestamps inside the packet")
        canonical_body = {k: v for k, v in packet.items() if k != "packet_id"}
        self.assertEqual(packet["packet_id"], hashlib.sha256(
            json.dumps(canonical_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32])
        # Determinism: the same inputs give byte-identical output.
        cfg = self.lib.load_config(self.tmp)
        dispositions = self.lib.parse_design_review_dispositions(
            ["F1.1=resolved", "F1.2=rejected:the fixture is named in the test list"],
            self.lib.latest_design_review_findings(status))
        again = self.lib.build_design_review_packet(self.tmp, cfg, status, self.read_acceptance(), dispositions)
        self.assertEqual(self.lib._canonical(again), self.lib._canonical(packet))
        # A removed criterion shows up under removed (history compared by id set).
        widened = json.loads(json.dumps(status))
        widened["design_review_history"][-1]["criteria_ids"] = ["REQ-001", "REQ-009"]
        widened["design_review_history"][-1]["criterion_hashes"]["REQ-009"] = "gone"
        removed = self.lib.build_design_review_packet(self.tmp, cfg, widened, self.read_acceptance(), {})
        self.assertEqual(removed["criteria_delta"]["removed"], ["REQ-009"])
        self.assertEqual(removed["criteria_delta"]["added"], ["REQ-002"])
        # A second review that repeats one earlier finding by text: only the
        # genuinely new finding is new since.
        second = self._review("Rollback path missing", "Migration is irreversible")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        again = self._packet("F2.1=unresolved:still open")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        later = self.read_status()["design_review_packet"]
        self.assertEqual(later["attempt"], 3)
        self.assertEqual(later["previous_attempt"], 2)
        self.assertEqual([f["id"] for f in later["findings"]], ["F2.1", "F2.2"])
        self.assertEqual(later["new_findings_since"], ["F2.2"])
        self.assertEqual(later["findings"][0]["note"], "still open")
        self.assertEqual(later["criteria_delta"]["unchanged"], ["REQ-001", "REQ-002"])
        events = [e for e in self._events() if e["kind"] == "design_review_packet_generated"]
        self.assertEqual([e["attempt"] for e in events], [2, 3])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_disposition_validation_refuses_without_writing(self):
        """i36-delta-packet, validation half."""
        self._reviewed_with_edit()
        before = self._files_snapshot()
        cases = [
            (("F1.9=resolved",), "unknown finding 'F1.9'"),
            (("F1.1=resolved", "F1.1=unresolved"), "repeats finding F1.1"),
            (("F1.1=fixed",), "invalid value 'fixed'"),
            (("F1.1",), "must have the form ID=resolved|rejected|unresolved[:note]"),
            (("F1.1=",), "invalid value ''"),
            (("F1.2=rejected",), "F1.2=rejected requires a note"),
            (("F1.2=rejected:   ",), "F1.2=rejected requires a note"),
        ]
        for dispositions, message in cases:
            with self.subTest(dispositions=dispositions):
                refused = self._packet(*dispositions)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertTrue(refused.stdout.startswith("SHIP_FEATURE_BLOCKED: "), refused.stdout)
                self.assertIn(message, refused.stdout)
        self.assertEqual(self._files_snapshot(), before, "no refusal writes status or an event")
        self.assertFalse(any(e["kind"] == "design_review_packet_generated" for e in self._events()))
        # An omitted finding defaults to unresolved with a null note, so
        # silence never reads as resolved; notes are stripped and capped.
        generated = self._packet("F1.1=resolved:" + "n" * 600)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        packet = self.read_status()["design_review_packet"]
        self.assertEqual(packet["dispositions"], {"resolved": ["F1.1"], "rejected": [], "unresolved": ["F1.2", "F1.3"]})
        self.assertEqual({f["id"]: (f["disposition"], f["note"]) for f in packet["findings"]},
                         {"F1.1": ("resolved", "n" * 512), "F1.2": ("unresolved", None), "F1.3": ("unresolved", None)})
        # --by is required to be non-empty and the command is Phase-2 only.
        blank = self._packet(by="   ")
        self.assertEqual(blank.returncode, 1)
        self.assertIn("--by must be a non-empty string", blank.stdout)

    def test_new_commit_after_the_review_marks_the_packet_stale(self):
        """i36-stale-context, repository half."""
        self._git_fixture()
        self._prepare()
        reviewed = self._review("Needs a rollback path")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        first_head = self._git("rev-parse", "HEAD")
        self.assertEqual(self.read_status()["design_review"]["head"], first_head)
        self.assertEqual(self.read_status()["design_review_history"][-1]["head"], first_head)
        # Same HEAD (only uncommitted state files changed): not stale.
        fresh = self._packet()
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        packet = self.read_status()["design_review_packet"]
        self.assertFalse(packet["stale"])
        self.assertEqual(packet["stale_reasons"], [])
        self.assertEqual(packet["files_changed_since_previous"], [])
        self.assertEqual(packet["previous_head"], first_head)
        self.assertEqual(packet["repository"]["head"], first_head)
        self.assertEqual(packet["repository"]["branch"], "main")
        self.assertTrue(packet["repository"]["dirty"])
        # A new commit moves HEAD: stale, with the reason and the changed file.
        (self.tmp / "src" / "b.py").write_text("B = 2\n")
        self._git("add", "src/b.py")
        self._git("commit", "-q", "-m", "second")
        second_head = self._git("rev-parse", "HEAD")
        moved = self._packet()
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        packet = self.read_status()["design_review_packet"]
        self.assertTrue(packet["stale"])
        self.assertEqual(packet["stale_reasons"],
                         [f"HEAD moved from {first_head[:12]} to {second_head[:12]} since the previous review"])
        self.assertEqual(packet["files_changed_since_previous"], ["src/b.py"])
        self.assertEqual(packet["previous_head"], first_head)
        self.assertEqual(packet["repository"]["head"], second_head)
        event = [e for e in self._events() if e["kind"] == "design_review_packet_generated"][-1]
        self.assertTrue(event["stale"])
        self.assertEqual(event["head"], second_head)
        self.assertEqual(event["previous_head"], first_head)
        self.assertEqual(event["counts"]["files_changed"], 1)
        # Evidence recorded at the earlier commit is flagged for the packet
        # even though the artifact itself is still current.
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + '\n[[design_evidence]]\nid = "inventory"\ncommand = "true"\n'
                        'inputs = ["src/a.py"]\n')
        cfg = self.lib.load_config(self.tmp)
        self.lib.run_design_evidence(self.tmp, cfg, ids=None, by="architect-1")
        current = self._packet()
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        packet = self.read_status()["design_review_packet"]
        self.assertEqual([(e["id"], e["state"], e["stale_for_packet"]) for e in packet["evidence"]],
                         [("inventory", "current", True)])
        self.assertEqual(packet["evidence"][0]["head"], second_head)
        self.assertTrue(packet["evidence"][0]["commit_matches_head"])
        self.assertNotIn("output", packet["evidence"][0], "packets never carry evidence output")

    def test_oversized_packet_is_trimmed_in_the_fixed_order_and_stays_deterministic(self):
        """i36-delta-packet, size half: every trim step fires, the map
        records each one, ids and hashes survive, bytes are identical on
        regeneration."""
        self._git_fixture()
        self._prepare()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + "".join(
            f'\n[[design_evidence]]\nid = "artifact-{n:02d}"\ncommand = "true"\ninputs = ["src/a.py"]\n'
            for n in range(16)))
        cfg = self.lib.load_config(self.tmp)
        reviewed = self._review(*[f"finding {n:02d} " + "t" * 500 for n in range(32)])
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        status = self.read_status()
        small_status = json.loads(json.dumps(status))
        findings = self.lib.latest_design_review_findings(status)
        self.assertEqual(len(findings), 32)
        # Never-trimmed bulk: 1100 changed criteria ids of 44 characters each
        # (about 51 KB canonical), plus 500 unchanged ids, plus 5000 changed
        # files, plus 32 findings with 512-character notes.
        changed_ids = [f"REQ-{n:040d}" for n in range(1100)]
        unchanged_ids = [f"UNC-{n:040d}" for n in range(500)]
        criteria = [{"id": cid, "type": "supporting", "requirement": "changed since", "verification": "automated",
                     "tests": ["true"], "evidence": [], "state": "failing"} for cid in changed_ids]
        criteria += [{"id": cid, "type": "supporting", "requirement": "same", "verification": "automated",
                      "tests": ["true"], "evidence": [], "state": "failing"} for cid in unchanged_ids]
        acceptance = {"feature": "oversized", "criteria": criteria}
        history = status["design_review_history"][-1]
        history["criteria_ids"] = sorted(changed_ids + unchanged_ids)
        history["criterion_hashes"] = {
            **{cid: "old" for cid in changed_ids},
            **{c["id"]: self.lib.criterion_spec_hash(c) for c in criteria if c["id"] in unchanged_ids},
        }
        history["head"] = "a" * 40
        dispositions = {f["id"]: {"disposition": "rejected", "note": "n" * 512} for f in findings}
        files = [f"src/generated/file_{n:05d}.py" for n in range(5000)]

        def runner(command, **kwargs):
            self.assertEqual(command[:3], ["git", "diff", "--name-only"])
            return subprocess.CompletedProcess(command, 0, stdout="\n".join(reversed(files)) + "\n", stderr="")

        packet = self.lib.build_design_review_packet(self.tmp, cfg, status, acceptance, dispositions, runner=runner)
        size = self.lib.design_review_packet_bytes(packet)
        self.assertLessEqual(size, 65536, size)
        self.assertEqual(json.loads(self.lib._canonical(packet)), packet, "valid JSON")
        self.assertEqual(list(packet["truncated"]), [
            "files_changed_since_previous", "criteria_delta.unchanged", "findings.text",
            "evidence.reasons", "findings",
        ], "the fixed trim order, each step recorded")
        self.assertEqual(packet["files_changed_since_previous"], [])
        self.assertEqual(packet["truncated"]["files_changed_since_previous"], 5000)
        self.assertNotIn("unchanged", packet["criteria_delta"])
        self.assertEqual(packet["criteria_delta"]["unchanged_count"], 500)
        self.assertEqual(packet["truncated"]["criteria_delta.unchanged"], 500)
        self.assertEqual(packet["criteria_delta"]["changed"], sorted(changed_ids), "changed ids are never trimmed")
        self.assertEqual(packet["criteria_delta"]["added"], [])
        self.assertEqual(packet["criteria_delta"]["removed"], [])
        remaining = packet["findings"]
        self.assertTrue(0 < len(remaining) < 32)
        self.assertEqual(packet["truncated"]["findings"], 32 - len(remaining))
        self.assertEqual([f["id"] for f in remaining], sorted(f["id"] for f in findings)[:len(remaining)],
                         "findings are cut from the end of the sorted list")
        self.assertTrue(all(len(f["text"]) == 256 and len(f["note"]) == 256 for f in remaining))
        self.assertEqual(packet["truncated"]["findings.text"], 64)
        self.assertEqual(len(packet["evidence"]), 16)
        self.assertTrue(all("reasons" not in e and e["state"] == "missing" for e in packet["evidence"]))
        self.assertEqual(packet["truncated"]["evidence.reasons"], 16)
        self.assertEqual(packet["attempt"], 2)
        self.assertEqual(packet["previous_head"], "a" * 40)
        self.assertEqual(packet["repository"]["head"], self._git("rev-parse", "HEAD"))
        self.assertTrue(packet["stale"])
        self.assertEqual(len(packet["stale_reasons"]), 1)
        self.assertEqual(packet["design_hash"], self.lib.design_hash(criteria))
        self.assertRegex(packet["packet_id"], r"^[0-9a-f]{32}$")
        body = {k: v for k, v in packet.items() if k != "packet_id"}
        self.assertEqual(packet["packet_id"], hashlib.sha256(self.lib._canonical(body).encode()).hexdigest()[:32],
                         "packet_id is computed over the final trimmed body")
        second = self.lib.build_design_review_packet(self.tmp, cfg, status, acceptance, dispositions, runner=runner)
        self.assertEqual(self.lib._canonical(second), self.lib._canonical(packet), "byte-identical on regeneration")
        self.assertEqual(self.lib.validate_status_schema(dict(status, design_review_packet=packet)), [])
        # Without the never-trimmed bulk, only the files step fires and the
        # first 200 paths survive.
        small_status["design_review_history"][-1]["head"] = "a" * 40
        small = self.lib.build_design_review_packet(
            self.tmp, cfg, small_status, {"feature": "small", "criteria": self.read_acceptance()["criteria"]},
            {}, runner=runner)
        self.assertEqual(small["truncated"], {"files_changed_since_previous": 4800})
        self.assertEqual(small["files_changed_since_previous"], files[:200])
        self.assertEqual(len(small["findings"]), 32)
        self.assertLessEqual(self.lib.design_review_packet_bytes(small), 65536)

    def test_reviewer_session_records_the_packet_and_the_event_is_logged(self):
        """i36-telemetry."""
        self._reviewed_with_edit()
        generated = self._packet("F1.1=resolved")
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        packet = self.read_status()["design_review_packet"]
        event = [e for e in self._events() if e["kind"] == "design_review_packet_generated"][-1]
        self.assertEqual(event["packet_id"], packet["packet_id"])
        self.assertEqual(event["attempt"], 2)
        self.assertEqual(event["design_hash"], packet["design_hash"])
        self.assertEqual(event["bytes"], self.lib.design_review_packet_bytes(packet))
        self.assertEqual(event["by"], "supervisor")
        self.assertEqual(event["counts"], {
            "findings": 3, "resolved": 1, "rejected": 0, "unresolved": 2, "new_findings": 3,
            "criteria_added": 0, "criteria_removed": 0, "criteria_changed": 1, "criteria_unchanged": 0,
            "evidence": 0, "files_changed": None,
        })
        self.assertNotIn("No failure-mode criterion", json.dumps(event), "the event never carries finding text")
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the revision", which=self.which)
        self.assertEqual(spec.packet_id, packet["packet_id"])
        self.assertEqual(spec.design_hash, packet["design_hash"])
        self.assertTrue(spec.stdin.startswith(self.PACKET_HEADING + "\n\n"))
        heading, _, rest = spec.stdin.partition("\n\n")
        packet_json, _, rest = rest.partition("\n\n")
        self.assertEqual(json.loads(packet_json), packet)
        self.assertTrue(rest.startswith((self.tmp / "prompts" / "reviewer.md").read_text().rstrip()))
        self.assertIn("# Assigned task\n\nReview the revision", rest)
        self.assertNotIn("private", spec.stdin)
        # Other roles never see a packet, even in Phase 2.
        for role in ("architect", "implementer", "supervisor"):
            self.assertNotIn(self.PACKET_HEADING, self.runtime.build_role_input(self.tmp, role, "Work"))

        class Process:
            pid = None
            returncode = 0
            def communicate(self, *, input, timeout):
                return None
            def terminate(self):
                return None
            def wait(self, timeout=None):
                return 0
            def kill(self):
                return None

        self.assertEqual(self.runtime.execute_launch(
            spec, actor="codex-reviewer", popen_factory=mock.Mock(return_value=Process()),
            session_id_factory=lambda: self._sid(1)), 0)
        status = self.read_status()
        session = status["agent_sessions"][self._sid(1)]
        self.assertEqual(session["packet_id"], packet["packet_id"])
        self.assertEqual(session["design_hash"], packet["design_hash"])
        self.assertEqual(session["role"], "reviewer")
        self.assertEqual(set(session), self.lib.AGENT_SESSION_FIELDS)
        self.assertEqual(self.lib.validate_status_schema(status), [])
        launching = [e for e in self._events() if e["kind"] == "agent_session_launching"][-1]
        self.assertEqual(launching["packet_id"], packet["packet_id"])
        self.assertEqual(launching["design_hash"], packet["design_hash"])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)
        # Non-reviewer sessions and legacy records: null fields, still valid.
        implementer = self.lib.create_agent_session(
            self.tmp, role="implementer", actor="codex-implementer", adapter="codex",
            requested_model="default", resolution_source="configured", id_factory=lambda: self._sid(2))
        self.assertIsNone(implementer["packet_id"])
        self.assertIsNone(implementer["design_hash"])
        legacy = self.read_status()
        for field in ("packet_id", "design_hash"):
            legacy["agent_sessions"][self._sid(2)].pop(field)
        self.assertEqual(self.lib.validate_status_schema(legacy), [], "sessions written before #36 stay valid")
        bad = self.read_status()
        bad["agent_sessions"][self._sid(2)]["packet_id"] = ""
        self.assertEqual(self.lib.validate_status_schema(bad),
                         [f"status: agent session '{self._sid(2)}'.packet_id must be null or a non-empty string"])
        with self.assertRaisesRegex(self.lib.HandsoffError, "packet_id must be null or a non-empty string"):
            self.lib.create_agent_session(
                self.tmp, role="architect", actor="claude-architect", adapter="claude",
                requested_model="default", resolution_source="configured", packet_id="  ")
        # The dashboard snapshot summarizes the packet by counts only.
        snapshot = self.dashboard.build_snapshot(self.tmp)
        summary = snapshot["design_review_packet"]
        self.assertEqual(summary["packet_id"], packet["packet_id"])
        self.assertEqual(summary["attempt"], 2)
        self.assertEqual(summary["previous_attempt"], 1)
        self.assertEqual(summary["findings"], {"total": 3, "resolved": 1, "rejected": 0, "unresolved": 2})
        self.assertEqual(summary["criteria_delta"], {"added": 0, "removed": 0, "changed": 1, "unchanged": 0})
        self.assertTrue(summary["stale"])
        self.assertFalse(summary["truncated"])
        self.assertEqual(summary["bytes"], event["bytes"])
        self.assertNotIn("No failure-mode criterion", json.dumps(summary))
        self.assertIsNone(self.lib.design_review_packet_summary({"design_review_packet": None}))
        self.assertIsNone(self.lib.design_review_packet_summary({}))

    def test_mismatched_packet_is_not_injected(self):
        """i36-stale-context, injection half: a packet whose design_hash no
        longer matches the current design, or whose attempt is not the next
        one, stays on disk but never reaches the reviewer."""
        self._reviewed_with_edit()
        self.assertEqual(self._packet("F1.1=resolved").returncode, 0)
        packet = self.read_status()["design_review_packet"]
        self.assertIn(self.PACKET_HEADING, self.runtime.build_role_input(self.tmp, "reviewer", "Review"))
        edited = run(["criterion-update", "REQ-001", "--requirement",
                      "Edited again after the packet was generated"], cwd=self.tmp)
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_packet"], packet, "the stored packet is untouched")
        self.assertNotEqual(packet["design_hash"], self.lib.design_hash(self.read_acceptance()["criteria"]))
        text = self.runtime.build_role_input(self.tmp, "reviewer", "Review")
        self.assertNotIn(self.PACKET_HEADING, text)
        self.assertTrue(text.startswith((self.tmp / "prompts" / "reviewer.md").read_text().rstrip()))
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.which)
        self.assertIsNone(spec.packet_id)
        self.assertIsNone(spec.design_hash)
        self.assertNotIn(self.PACKET_HEADING, spec.stdin)
        cfg = self.lib.load_config(self.tmp)
        self.assertIsNone(self.lib.applicable_design_review_packet(status, cfg, self.read_acceptance()["criteria"]))
        # Regenerating for the current design makes it applicable again.
        self.assertEqual(self._packet().returncode, 0)
        status = self.read_status()
        self.assertEqual(self.lib.applicable_design_review_packet(status, cfg, self.read_acceptance()["criteria"]),
                         status["design_review_packet"])
        # Recording attempt 2 makes a packet built for attempt 2 stale by attempt number.
        second = self._review("Still missing a rollback path")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        status = self.read_status()
        self.assertEqual(status["design_review_packet"]["attempt"], 2)
        self.assertEqual(status["design_review_attempts"], 2)
        self.assertIsNone(self.lib.applicable_design_review_packet(status, cfg, self.read_acceptance()["criteria"]))
        self.assertNotIn(self.PACKET_HEADING, self.runtime.build_role_input(self.tmp, "reviewer", "Review"))
        # Outside Phase 2 nothing is injected and the command is refused.
        phase2 = dict(status, phase_number=3, phase=self.lib.PHASES[3])
        phase2["design_review_packet"] = dict(status["design_review_packet"], attempt=3)
        self.assertIsNone(self.lib.applicable_design_review_packet(phase2, cfg, self.read_acceptance()["criteria"]))

    def test_broker_routes_findings_and_packet_requests(self):
        self._reviewed_with_edit()
        root = self.tmp.resolve()
        base = {"actor": "supervisor", "project_root": str(root), "action": "workflow"}
        argv = self.broker._workflow_argv(root, {
            **base, "command": "record-design-review", "by": "reviewer", "architect": "architect-1",
            "decision": "request-changes", "summary": "Needs work", "findings": ["one", "two"],
        })
        self.assertEqual(argv[-4:], ["--finding", "one", "--finding", "two"])
        argv = self.broker._workflow_argv(root, {
            **base, "command": "design-review-packet", "by": "supervisor",
            "dispositions": ["F1.1=resolved", "F1.2=rejected:why"],
        })
        self.assertEqual(argv[5:], ["--by", "supervisor", "--disposition", "F1.1=resolved",
                                    "--disposition", "F1.2=rejected:why"])
        self.assertEqual(self.broker._workflow_argv(root, {**base, "command": "design-review-packet", "by": "s"})[5:],
                         ["--by", "s"])
        for bad in ({"findings": []}, {"findings": ["ok", ""]}, {"findings": "one"}):
            with self.assertRaisesRegex(self.lib.HandsoffError, "findings must be a non-empty string array"):
                self.broker._workflow_argv(root, {
                    **base, "command": "record-design-review", "by": "r", "architect": "a",
                    "decision": "approve", "summary": "s", **bad})
        for bad in ({"dispositions": []}, {"dispositions": [1]}, {"dispositions": "F1.1=resolved"}):
            with self.assertRaisesRegex(self.lib.HandsoffError, "dispositions must be a non-empty string array"):
                self.broker._workflow_argv(root, {**base, "command": "design-review-packet", "by": "s", **bad})
        with self.assertRaisesRegex(self.lib.HandsoffError, "unknown fields: note"):
            self.broker._workflow_argv(root, {**base, "command": "design-review-packet", "by": "s", "note": "x"})
        self.assertNotIn("design-review-packet", self.broker.HUMAN_ONLY_COMMANDS)
        # Dispatched through the broker, the packet is really generated.
        quiet = lambda argv, **kwargs: subprocess.Popen(argv, **{**kwargs, "stdout": subprocess.DEVNULL})
        rc = self.broker.dispatch_supervisor_request(self.tmp, {
            **base, "command": "design-review-packet", "by": "supervisor", "dispositions": ["F1.3=resolved"],
        }, workflow_popen=quiet)
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_status()["design_review_packet"]["dispositions"]["resolved"], ["F1.3"])

    def test_hand_edited_history_and_packet_refuse_cleanly(self):
        self._reviewed_with_edit()
        self.assertEqual(self._packet().returncode, 0)
        base = self.read_status()
        self.assertEqual(self.lib.validate_status_schema(base), [])
        entry = base["design_review_history"][0]
        # The criterion edit cleared design_review; a complete record with the
        # #36 fields is what a fresh record-design-review would write.
        review = {"by": "design-reviewer", "architect": "architect-1", "at": base["updated_at"],
                  "decision": "changes_requested", "summary": "Design needs work",
                  "design_hash": entry["design_hash"], "config_hash": "c" * 64,
                  "attempt": 1, "head": None, "findings": entry["findings"]}
        self.assertEqual(self.lib.validate_status_schema({**base, "design_review": review}), [])
        cases = [
            ({"design_review_history": "nope"}, "'design_review_history' must be an array"),
            ({"design_review_history": [dict(entry, extra=1)]}, "must have exactly the keys"),
            ({"design_review_history": [dict(entry, attempt=0)]}, "attempt must be a positive integer"),
            ({"design_review_history": [dict(entry, decision="maybe")]}, "decision must be"),
            ({"design_review_history": [dict(entry, criteria_ids=["b", "a"])]}, "sorted array of criterion ids"),
            ({"design_review_history": [dict(entry, structural_blocker="no")]}, "structural_blocker must be boolean"),
            ({"design_review_history": [dict(entry, findings=[{"id": "F1.1"}])]}, "exactly id and text"),
            ({"design_review_history": [dict(entry, findings=[{"id": "bad", "text": "t"}])]}, "is not a finding id"),
            ({"design_review_history": [dict(entry, findings=[{"id": "F1.1", "text": "t"}] * 2)]}, "repeats finding id F1.1"),
            ({"design_review_packet": "nope"}, "'design_review_packet' must be an object or null"),
            ({"design_review_packet": dict(base["design_review_packet"], packet_id="xyz")}, "32 hex characters"),
            ({"design_review_packet": dict(base["design_review_packet"], attempt=1)}, "at least 2"),
            ({"design_review_packet": dict(base["design_review_packet"], stale="yes")}, "stale' must be boolean"),
            ({"design_review_packet": dict(base["design_review_packet"], truncated={"findings": -1})},
             "dropped counts"),
            ({"design_review": dict(review, findings=[{"id": "F1.1", "text": "x" * 513}])},
             "at most 512 characters"),
            ({"design_review": dict(review, attempt=0)}, "'design_review.attempt' must be a positive integer"),
            ({"design_review": dict(review, head="")}, "'design_review.head' must be a non-empty string or null"),
        ]
        for patch, message in cases:
            with self.subTest(patch=list(patch)):
                errors = self.lib.validate_status_schema({**base, **patch})
                self.assertTrue(errors, patch)
                self.assertTrue(any(message in e for e in errors), errors)
        legacy = {k: v for k, v in base.items() if k not in ("design_review_history", "design_review_packet")}
        legacy["design_review"] = {k: v for k, v in review.items() if k not in ("findings", "head", "attempt")}
        self.assertEqual(self.lib.validate_status_schema(legacy), [], "a pre-#36 status stays valid")
        # A hand edit on disk is refused by the next command through the audit block.
        tampered = self.read_status()
        tampered["design_review_history"][0]["structural_blocker"] = "no"
        (self.tmp / "handsoff-status.json").write_text(json.dumps(tampered, indent=2))
        refused = self._packet()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", refused.stdout)


class TestTieredDesignReviewerProfiles(HandsoffTestCase):
    """Issue #37: every design-review attempt after the first can go to an
    economical follow-up reviewer profile ([agents]/[models]
    reviewer_followup) when nothing structural changed since the last
    review. `lib.select_design_reviewer_profile` applies a fixed precedence
    (first_review, no_followup_configured, pilot_escalation,
    structural_blocker, criteria_structure_changed, delta_check), enforces
    independence and executable availability on whichever tier it picked
    without ever switching tiers, and a config without the keys reproduces
    the single-profile behavior exactly. Criteria i37-tier-selection,
    i37-escalation-to-primary, i37-unavailable-not-silent,
    i37-legacy-unchanged."""

    PRIMARY = {"adapter": "codex", "model": "default"}
    FOLLOWUP = {"adapter": "claude", "model": "claude-haiku"}

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_broker
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.broker = handsoff_broker
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.both = lambda name: f"/bin/{name}" if name in ("codex", "claude") else None
        self.codex_only = lambda name: "/bin/codex" if name == "codex" else None
        self.claude_only = lambda name: "/bin/claude" if name == "claude" else None

    def _write_toml(self, followup=FOLLOWUP, *, implementer=None, architect=None):
        """Explicit, distinct primary profiles (the recommended crew) plus
        the optional follow-up; None omits both follow-up keys."""
        agents = {"architect": "claude", "supervisor": "claude", "implementer": "claude", "reviewer": "codex"}
        models = {"architect": "claude-opus-5", "supervisor": "claude-opus-5",
                  "implementer": "claude-opus-5", "reviewer": "default"}
        for role, profile in (("implementer", implementer), ("architect", architect)):
            if profile:
                agents[role], models[role] = profile["adapter"], profile["model"]
        if followup:
            agents["reviewer_followup"], models["reviewer_followup"] = followup["adapter"], followup["model"]
        lines = ["[checks]", "commands = []", "live_commands = []", "", "[agents]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in agents.items()]
        lines += ["", "[models]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in models.items()]
        (self.tmp / "handsoff.toml").write_text("\n".join(lines) + "\n")

    def _reset(self):
        """A second fresh project in the same test (init refuses to reuse one)."""
        shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        shutil.copytree(ROOT / "schemas", self.tmp / "schemas")
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")

    def _prepare(self, followup=FOLLOWUP, criteria=("REQ-001",)):
        """Phase 2 with real criteria and no review recorded yet."""
        if (self.tmp / "handsoff-status.json").exists():
            self._reset()
        self._write_toml(followup)
        self.init("Issue 37 fixture")
        criterion = run(["criterion-update", "REQ-001", "--requirement",
                         "A real, independently reviewable design criterion"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        for extra in criteria[1:]:
            added = run(["criterion-add", extra, "--type", "supporting", "--requirement",
                         f"Criterion {extra} exists", "--verification", "automated", "--test", "true"],
                        cwd=self.tmp)
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        phase2 = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase2.returncode, 0, phase2.stdout + phase2.stderr)

    def _review(self, *extra, decision="--request-changes", summary="Design needs work"):
        return run(["record-design-review", "--by", "design-reviewer", "--architect", "architect-1",
                    "--summary", summary, decision, *extra], cwd=self.tmp)

    def _escalate(self, by="moncy", note=None):
        args = ["design-review-escalate", "--by", by]
        if note:
            args += ["--note", note]
        return run(args, cwd=self.tmp)

    def _select(self, which=None):
        cfg = self.lib.load_config(self.tmp)
        return self.lib.select_design_reviewer_profile(
            cfg, self.read_status(), self.read_acceptance(), which=which or self.both)

    def _tier(self):
        cfg = self.lib.load_config(self.tmp)
        return self.lib.select_design_reviewer_tier(cfg, self.read_status(), self.read_acceptance())

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if line.strip()]

    def _files_snapshot(self):
        return {name: (self.tmp / name).read_bytes()
                for name in ("handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl")}

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    class _Process:
        pid = None
        returncode = 0

        def communicate(self, *, input, timeout):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def _launch(self, spec, number):
        return self.runtime.execute_launch(
            spec, actor="managed-reviewer", popen_factory=mock.Mock(return_value=self._Process()),
            session_id_factory=lambda: self._sid(number))

    def test_config_requires_both_followup_keys_or_neither(self):
        self._write_toml(None)
        cfg = self.lib.load_config(self.tmp)
        self.assertIsNone(cfg["reviewer_followup"])
        self.assertIsNone(self.lib.followup_reviewer_profile(cfg))
        self.assertIsNone(self.lib.DEFAULT_CONFIG["reviewer_followup"])
        legacy_hash = self.lib.config_hash(cfg)
        self._write_toml(self.FOLLOWUP)
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(cfg["reviewer_followup"], self.FOLLOWUP)
        self.assertEqual(self.lib.followup_reviewer_profile(cfg), self.FOLLOWUP)
        # The four roles are untouched by the extra keys.
        self.assertEqual(self.lib.agent_profiles(cfg)["reviewer"], self.PRIMARY)
        self.assertEqual(set(self.lib.agent_profiles(cfg)), set(self.lib.SELECTABLE_AGENT_ROLES))
        # A cost knob, not a gate: no governance hash changes when it is added.
        self.assertNotIn("reviewer_followup", self.lib.GOVERNANCE_CONFIG_KEYS)
        self.assertEqual(self.lib.config_hash(cfg), legacy_hash)
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        toml.write_text(text.replace('reviewer_followup = "claude-haiku"\n', ""))
        with self.assertRaisesRegex(self.lib.HandsoffError, "must be set together"):
            self.lib.load_config(self.tmp)
        toml.write_text(text.replace('reviewer_followup = "claude"\n', ""))
        with self.assertRaisesRegex(self.lib.HandsoffError, "must be set together"):
            self.lib.load_config(self.tmp)
        toml.write_text(text.replace('reviewer_followup = "claude"', 'reviewer_followup = "auto"'))
        with self.assertRaisesRegex(self.lib.HandsoffError, "agents.reviewer_followup must be exactly 'codex' or 'claude'"):
            self.lib.load_config(self.tmp)
        toml.write_text(text.replace('reviewer_followup = "claude-haiku"', 'reviewer_followup = "-x"'))
        with self.assertRaisesRegex(self.lib.HandsoffError, "models.reviewer_followup"):
            self.lib.load_config(self.tmp)

    def test_first_attempt_primary_then_delta_check_selects_followup(self):
        """i37-tier-selection: attempt 1 primary; attempt 2 with nothing
        structural changed goes to the follow-up; the session and the
        design_reviewer_selected event carry tier and reason."""
        self._prepare()
        self.assertEqual(self._tier(), ("primary", "first_review"))
        first = self._select()
        self.assertEqual(first, {**self.PRIMARY, "tier": "primary", "reason": "first_review",
                                 "resolution_source": "configured"})
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the design", which=self.both)
        self.assertEqual((spec.adapter, spec.model, spec.tier, spec.tier_reason),
                         ("codex", "default", "primary", "first_review"))
        self.assertEqual(self._launch(spec, 1), 0)
        session = self.read_status()["agent_sessions"][self._sid(1)]
        self.assertEqual(session["tier"], "primary")
        self.assertEqual(set(session), self.lib.AGENT_SESSION_FIELDS)
        selected = [e for e in self._events() if e["kind"] == "design_reviewer_selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual((selected[0]["tier"], selected[0]["reason"], selected[0]["session_id"],
                          selected[0]["adapter"], selected[0]["requested_model"],
                          selected[0]["design_review_attempt"]),
                         ("primary", "first_review", self._sid(1), "codex", "default", 1))
        launching = [e for e in self._events() if e["kind"] == "agent_session_launching"][-1]
        self.assertEqual(launching["tier"], "primary")
        kinds = [e["kind"] for e in self._events()]
        self.assertLess(kinds.index("design_reviewer_selected"), kinds.index("agent_session_launching"),
                        "the selection is logged before the launch it explains")

        reviewed = self._review("--finding", "Rollback path missing")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        review = self.read_status()["design_review"]
        self.assertEqual(review["reviewer_profile"],
                         {**self.PRIMARY, "tier": "primary", "reason": "first_review"})
        self.assertFalse(review["structural_blocker"])
        self.assertFalse(self.read_status()["design_review_history"][-1]["structural_blocker"])
        changes = [e for e in self._events() if e["kind"] == "design_review_changes_requested"][-1]
        self.assertEqual((changes["reviewer_tier"], changes["reviewer_selection_reason"],
                          changes["reviewer_profile"], changes["structural_blocker"],
                          changes["escalation_consumed"]),
                         ("primary", "first_review", self.PRIMARY, False, False))

        self.assertEqual(self._tier(), ("followup", "delta_check"))
        second = self._select()
        self.assertEqual(second, {**self.FOLLOWUP, "tier": "followup", "reason": "delta_check",
                                  "resolution_source": "configured"})
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the revision", which=self.both)
        self.assertEqual((spec.adapter, spec.model, spec.tier, spec.tier_reason, spec.resolution_source),
                         ("claude", "claude-haiku", "followup", "delta_check", "configured"))
        self.assertEqual(spec.argv[0], "/bin/claude")
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], "claude-haiku")
        self.assertEqual(self._launch(spec, 2), 0)
        status = self.read_status()
        session = status["agent_sessions"][self._sid(2)]
        self.assertEqual((session["adapter"], session["requested_model"], session["tier"]),
                         ("claude", "claude-haiku", "followup"))
        self.assertEqual(self.lib.validate_status_schema(status), [])
        selected = [e for e in self._events() if e["kind"] == "design_reviewer_selected"][-1]
        self.assertEqual((selected["tier"], selected["reason"], selected["design_review_attempt"]),
                         ("followup", "delta_check", 2))
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)
        # The follow-up review is recorded under its tier, and the same
        # inputs keep giving the same answer (the selection is pure).
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        approved = self._review(decision="--approve", summary="Revision is sound")
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertEqual(self.read_status()["design_review"]["reviewer_profile"],
                         {**self.FOLLOWUP, "tier": "followup", "reason": "delta_check"})
        # Sessions for other roles, and reviewer sessions outside the
        # tiered launch, carry a null tier and stay valid.
        implementer = self.lib.create_agent_session(
            self.tmp, role="implementer", actor="claude-implementer", adapter="claude",
            requested_model="default", resolution_source="configured", id_factory=lambda: self._sid(3))
        self.assertIsNone(implementer["tier"])
        self.assertNotIn("design_reviewer_selected",
                         [e["kind"] for e in self._events()[-2:]], "no selection event without a tier")
        with self.assertRaisesRegex(self.lib.HandsoffError, "tier applies to reviewer sessions only"):
            self.lib.create_agent_session(
                self.tmp, role="architect", actor="claude-architect", adapter="claude",
                requested_model="default", resolution_source="configured", tier="primary",
                tier_reason="first_review")
        with self.assertRaisesRegex(self.lib.HandsoffError, "tier must be null or one of primary, followup"):
            self.lib.create_agent_session(
                self.tmp, role="reviewer", actor="x", adapter="codex", requested_model="default",
                resolution_source="configured", tier="cheap", tier_reason="delta_check")

    def test_added_criterion_since_last_review_selects_primary(self):
        """i37-escalation-to-primary: an added id is a structural change."""
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement",
                     "A failure-mode criterion", "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(self._tier(), ("primary", "criteria_structure_changed"))
        self.assertEqual(self._select()["adapter"], "codex")
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.both)
        self.assertEqual((spec.tier, spec.tier_reason), ("primary", "criteria_structure_changed"))

    def test_removed_criterion_since_last_review_selects_primary(self):
        self._prepare(criteria=("REQ-001", "REQ-002"))
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self.read_status()["design_review_history"][-1]["criteria_ids"], ["REQ-001", "REQ-002"])
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        removed = run(["criterion-remove", "REQ-002"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(self._tier(), ("primary", "criteria_structure_changed"))
        self.assertEqual(self._select()["tier"], "primary")
        # A history predating #36 (no criteria_ids to compare) cannot prove
        # the structure unchanged, so it also gets the primary reviewer.
        cfg = self.lib.load_config(self.tmp)
        legacy = dict(self.read_status(), design_review_history=[])
        self.assertEqual(self.lib.select_design_reviewer_tier(cfg, legacy, self.read_acceptance()),
                         ("primary", "criteria_structure_changed"))

    def test_text_only_edit_stays_delta_check(self):
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        edited = run(["criterion-update", "REQ-001", "--requirement",
                      "A real criterion, revised with a failure mode"], cwd=self.tmp)
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
        self.assertIsNone(self.read_status()["design_review"], "the edit cleared the review record")
        self.assertNotEqual(self.read_status()["design_review_history"][-1]["design_hash"],
                            self.lib.design_hash(self.read_acceptance()["criteria"]))
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        self.assertEqual(self._select()["model"], "claude-haiku")

    def test_structural_blocker_selects_primary(self):
        self._prepare()
        before = self._files_snapshot()
        refused = self._review("--structural-blocker", decision="--approve", summary="Fine")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("--structural-blocker is only valid with --request-changes", refused.stdout)
        self.assertEqual(self._files_snapshot(), before, "a refusal writes nothing")
        flagged = self._review("--structural-blocker", "--finding", "Wrong data model")
        self.assertEqual(flagged.returncode, 0, flagged.stdout + flagged.stderr)
        status = self.read_status()
        self.assertTrue(status["design_review"]["structural_blocker"])
        self.assertTrue(status["design_review_history"][-1]["structural_blocker"])
        self.assertEqual(self.lib.validate_status_schema(status), [])
        event = [e for e in self._events() if e["kind"] == "design_review_changes_requested"][-1]
        self.assertTrue(event["structural_blocker"])
        self.assertEqual(self._tier(), ("primary", "structural_blocker"))
        self.assertEqual(self._select(), {**self.PRIMARY, "tier": "primary", "reason": "structural_blocker",
                                          "resolution_source": "configured"})
        # The broker carries the flag as a boolean only.
        root = self.tmp.resolve()
        base = {"actor": "supervisor", "project_root": str(root), "action": "workflow",
                "command": "record-design-review", "by": "reviewer", "architect": "architect-1",
                "decision": "request-changes", "summary": "Needs rework"}
        argv = self.broker._workflow_argv(root, {**base, "structural_blocker": True})
        self.assertEqual(argv[-1], "--structural-blocker")
        self.assertNotIn("--structural-blocker", self.broker._workflow_argv(root, {**base, "structural_blocker": False}))
        with self.assertRaisesRegex(self.lib.HandsoffError, "structural_blocker must be boolean"):
            self.broker._workflow_argv(root, {**base, "structural_blocker": "yes"})

    def test_pilot_escalation_selects_primary_and_is_consumed(self):
        """i37-escalation-to-primary: design-review-escalate forces the next
        attempt onto the primary tier once, is human-only, one at a time,
        and Phase 2 only."""
        self._write_toml()
        self.init("Issue 37 escalation")
        before = self._files_snapshot()
        early = self._escalate()
        self.assertEqual(early.returncode, 1, early.stdout)
        self.assertIn("can only be recorded in Phase 2", early.stdout)
        self.assertEqual(self._files_snapshot(), before)
        criterion = run(["criterion-update", "REQ-001", "--requirement", "A real criterion"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        escalated = self._escalate(note="Design changed direction, full review please")
        self.assertEqual(escalated.returncode, 0, escalated.stdout + escalated.stderr)
        self.assertIn("DESIGN_REVIEWER_ESCALATED: attempt 2", escalated.stdout)
        status = self.read_status()
        escalation = status["design_reviewer_escalation"]
        self.assertEqual(set(escalation), {"by", "at", "note", "consumed_at"})
        self.assertEqual((escalation["by"], escalation["note"], escalation["consumed_at"]),
                         ("moncy", "Design changed direction, full review please", None))
        self.assertEqual(status["design_review_attempts"], 1, "escalation never counts as an attempt")
        self.assertEqual(self.lib.validate_status_schema(status), [])
        event = [e for e in self._events() if e["kind"] == "design_reviewer_escalated"][-1]
        self.assertEqual((event["by"], event["tier"], event["reason"], event["design_review_attempt"]),
                         ("moncy", "primary", "pilot_escalation", 2))
        self.assertEqual(event["message"], "Design changed direction, full review please")
        self.assertEqual(self._tier(), ("primary", "pilot_escalation"))
        self.assertEqual(self._select()["adapter"], "codex")
        again = self._escalate()
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("unconsumed design reviewer escalation already exists", again.stdout)
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review again", which=self.both)
        self.assertEqual((spec.tier, spec.tier_reason), ("primary", "pilot_escalation"))
        self.assertEqual(self._launch(spec, 1), 0)
        self.assertEqual(self.read_status()["agent_sessions"][self._sid(1)]["tier"], "primary")
        # Recording the review consumes the escalation; the next attempt is a delta check again.
        consumed = self._review(summary="Still one gap")
        self.assertEqual(consumed.returncode, 0, consumed.stdout + consumed.stderr)
        status = self.read_status()
        self.assertIsNotNone(status["design_reviewer_escalation"]["consumed_at"])
        self.assertEqual(status["design_review"]["reviewer_profile"]["reason"], "pilot_escalation")
        changes = [e for e in self._events() if e["kind"] == "design_review_changes_requested"][-1]
        self.assertTrue(changes["escalation_consumed"])
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        self.assertEqual(self._escalate().returncode, 0, "a consumed escalation no longer blocks a new one")
        # Human-only: the broker refuses it before any process starts.
        workflow = mock.Mock()
        request = {"actor": "supervisor", "project_root": str(self.tmp.resolve()),
                   "action": "workflow", "command": "design-review-escalate", "by": "supervisor"}
        with self.assertRaisesRegex(self.lib.HandsoffError, "human-only command: design-review-escalate"):
            self.broker.dispatch_supervisor_request(
                self.tmp, request, workflow_popen=workflow, agent_launcher=mock.Mock())
        workflow.assert_not_called()
        self.assertIn("design-review-escalate", self.broker.HUMAN_ONLY_COMMANDS)

    def test_escalation_beats_criteria_structure_change(self):
        """Rule 3 beats rule 5: both present, the reason is pilot_escalation."""
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self._escalate().returncode, 0)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement",
                     "Another criterion", "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(self._tier(), ("primary", "pilot_escalation"))
        self.assertEqual(self._select()["reason"], "pilot_escalation")
        # And rule 4 beats rule 5 the same way.
        self._prepare()
        self.assertEqual(self._review("--structural-blocker").returncode, 0)
        added = run(["criterion-add", "REQ-003", "--type", "supporting", "--requirement",
                     "Yet another criterion", "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(self._tier(), ("primary", "structural_blocker"))

    def test_structural_blocker_launches_primary_when_followup_executable_is_missing(self):
        """i37-unavailable-not-silent: the follow-up's absence is irrelevant
        when it was not selected."""
        self._prepare()
        self.assertEqual(self._review("--structural-blocker").returncode, 0)
        selection = self._select(which=self.codex_only)
        self.assertEqual((selection["tier"], selection["reason"], selection["adapter"]),
                         ("primary", "structural_blocker", "codex"))
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Full review", which=self.codex_only)
        self.assertEqual((spec.adapter, spec.tier), ("codex", "primary"))
        self.assertEqual(self._launch(spec, 1), 0)
        session = self.read_status()["agent_sessions"][self._sid(1)]
        self.assertEqual((session["adapter"], session["tier"]), ("codex", "primary"))

    def test_missing_followup_executable_raises_and_never_launches_primary(self):
        """i37-unavailable-not-silent: a delta check whose follow-up adapter
        is missing is refused with the tier and the remedies named; nothing
        is launched, no session or event is recorded, and no fallback to
        the primary tier happens."""
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        before = self._files_snapshot()
        expected = ("followup reviewer profile unavailable: claude is not on PATH; install it, "
                    "set fallback_policy.reviewer, or remove reviewer_followup")
        with self.assertRaises(self.lib.HandsoffError) as refused:
            self._select(which=self.codex_only)
        self.assertEqual(str(refused.exception), expected)
        with self.assertRaises(self.lib.HandsoffError) as refused:
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review the revision", which=self.codex_only)
        self.assertEqual(str(refused.exception), expected)
        self.assertEqual(self._files_snapshot(), before)
        self.assertNotIn("agent_sessions", self.read_status())
        self.assertNotIn("design_reviewer_selected", [e["kind"] for e in self._events()])
        # The other direction never falls forward either: a first review
        # with the primary adapter missing is refused as primary, even
        # though the follow-up adapter is installed.
        self._prepare()
        with self.assertRaises(self.lib.HandsoffError) as refused:
            self._select(which=self.claude_only)
        self.assertEqual(str(refused.exception),
                         "primary reviewer profile unavailable: codex is not on PATH; install it, "
                         "set fallback_policy.reviewer, or remove reviewer_followup")
        with self.assertRaises(self.lib.HandsoffError):
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.claude_only)
        self.assertNotIn("agent_sessions", self.read_status())
        # The dashboard names the same refusal instead of hiding it.
        with mock.patch.object(self.lib.shutil, "which", self.claude_only):
            policy = self.dashboard.build_snapshot(self.tmp)["policy"]["design_reviewer_selection"]
        self.assertIsNone(policy["current"])
        self.assertEqual((policy["next"]["tier"], policy["next"]["reason"], policy["next"]["adapter"]),
                         ("primary", "first_review", "codex"))
        self.assertIn("primary reviewer profile unavailable", policy["next"]["error"])

    def test_followup_equal_to_architect_or_implementer_profile_is_refused(self):
        """i37-escalation-to-primary: the follow-up must be independent of
        the profiles it would be reviewing."""
        self._prepare(followup={"adapter": "claude", "model": "claude-opus-5"})
        self.assertEqual(self._review().returncode, 0)
        self.assertEqual(self._tier(), ("followup", "delta_check"))
        before = self._files_snapshot()
        with self.assertRaisesRegex(self.lib.HandsoffError,
                                    "followup reviewer profile claude/claude-opus-5 is the same as the architect profile"):
            self._select()
        with self.assertRaisesRegex(self.lib.HandsoffError, "same as the architect profile"):
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.both)
        self.assertEqual(self._files_snapshot(), before)
        self.assertNotIn("agent_sessions", self.read_status())
        # Same for the implementer, with an architect that differs.
        self._reset()
        self._write_toml({"adapter": "codex", "model": "gpt-5-mini"},
                         implementer={"adapter": "codex", "model": "gpt-5-mini"})
        self.init("Issue 37 implementer clash")
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "A real criterion"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self._review().returncode, 0)
        with self.assertRaisesRegex(self.lib.HandsoffError,
                                    "followup reviewer profile codex/gpt-5-mini is the same as the implementer profile"):
            self._select()
        # The primary tier is held to the same rule once tiering is on.
        self._write_toml(self.FOLLOWUP, architect={"adapter": "codex", "model": "default"})
        self.assertEqual(self._review("--structural-blocker").returncode, 0)
        with self.assertRaisesRegex(self.lib.HandsoffError,
                                    "primary reviewer profile codex/default is the same as the architect profile"):
            self._select()

    def test_legacy_config_without_the_keys_is_primary_on_every_attempt(self):
        """i37-legacy-unchanged."""
        self._prepare(followup=None)
        self.assertEqual(self._tier(), ("primary", "first_review"))
        # A missing primary is refused with the pre-#37 wording, unchanged.
        with self.assertRaisesRegex(self.lib.HandsoffError, "reviewer cannot launch: the configured adapter codex"):
            self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.claude_only)
        self.assertNotIn("agent_sessions", self.read_status())
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.both)
        self.assertEqual((spec.adapter, spec.model, spec.tier, spec.tier_reason),
                         ("codex", "default", "primary", "first_review"))
        self.assertEqual(self._launch(spec, 1), 0)
        self.assertEqual(self.read_status()["agent_sessions"][self._sid(1)]["tier"], "primary")
        for attempt in (1, 2):
            reviewed = self._review()
            self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
            self.assertEqual(self.read_status()["design_review"]["reviewer_profile"]["tier"], "primary")
            self.assertEqual(self._tier(), ("primary", "no_followup_configured"))
            self.assertEqual(self._select(), {**self.PRIMARY, "tier": "primary", "reason": "no_followup_configured",
                                              "resolution_source": "configured"})
        # Neither a structural blocker nor an escalation nor a criteria
        # change can select anything but primary without a follow-up.
        self.assertEqual(self._escalate().returncode, 0)
        self.assertEqual(self._tier(), ("primary", "no_followup_configured"))
        # Independence is guidance only without tiering, exactly as before:
        # the all-"auto" fixture crew (same adapter and model everywhere)
        # still launches its reviewer.
        normalize_fixture_config(self.tmp / "handsoff.toml")
        shutil.copy(ROOT / "handsoff.toml", self.tmp / "handsoff.toml")
        normalize_fixture_config(self.tmp / "handsoff.toml")
        cfg = self.lib.load_config(self.tmp)
        self.assertIsNone(cfg["reviewer_followup"])
        self.assertEqual(self.lib.agent_profiles(cfg)["reviewer"], {"adapter": "auto", "model": "default"})
        selection = self._select(which=self.codex_only)
        self.assertEqual((selection["adapter"], selection["tier"], selection["resolution_source"]),
                         ("codex", "primary", "auto_detected"))
        # An unresolvable "auto" primary is refused with the pre-#37 wording, unchanged.
        with self.assertRaisesRegex(self.lib.HandsoffError, "no supported agent adapter is available on PATH"):
            self._select(which=lambda _name: None)

    def test_dashboard_policy_shows_current_and_next_with_reason(self):
        """i37-legacy-unchanged, dashboard half, plus the tiered shape."""
        self._prepare()
        with mock.patch.object(self.lib.shutil, "which", self.both):
            policy = self.dashboard.build_snapshot(self.tmp)["policy"]["design_reviewer_selection"]
        self.assertEqual(policy, {"current": None,
                                  "next": {"tier": "primary", "reason": "first_review", "adapter": "codex",
                                           "model": "default", "error": None}})
        self.assertEqual(self._review().returncode, 0)
        with mock.patch.object(self.lib.shutil, "which", self.both):
            snapshot = self.dashboard.build_snapshot(self.tmp)
        policy = snapshot["policy"]["design_reviewer_selection"]
        self.assertEqual(policy["current"], {"adapter": "codex", "model": "default", "tier": "primary",
                                             "reason": "first_review"})
        self.assertEqual(policy["next"], {"tier": "followup", "reason": "delta_check", "adapter": "claude",
                                          "model": "claude-haiku", "error": None})
        self.assertIsNone(snapshot["policy"]["design_reviewer_escalation"])
        self.assertEqual(snapshot["settings"]["reviewer_followup"], self.FOLLOWUP)
        # `status` prints the same view.
        with mock.patch.object(self.lib.shutil, "which", self.both):
            status_cmd = run(["status"], cwd=self.tmp)
        printed = json.loads(status_cmd.stdout)["design_reviewer_selection"]
        self.assertEqual(printed["current"]["tier"], "primary")
        self.assertEqual(printed["next"]["tier"], "followup")
        # The rendering hooks exist in the page and the logic module.
        html = (ROOT / "dashboard" / "index.html").read_text()
        app = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn('id="design-reviewer-profile"', html)
        self.assertIn('designReviewerProfileLabel(policy.design_reviewer_selection)', app)
        # A record predating #37 (no reviewer_profile) reads as no current.
        legacy = self.read_status()
        legacy["design_review"].pop("reviewer_profile")
        legacy["design_review"].pop("structural_blocker")
        self.assertEqual(self.lib.validate_status_schema(legacy), [])
        cfg = self.lib.load_config(self.tmp)
        view = self.lib.design_reviewer_selection_view(cfg, legacy, self.read_acceptance(), which=self.both)
        self.assertIsNone(view["current"])
        self.assertEqual(view["next"]["tier"], "followup")

    def test_settings_save_preserves_the_followup_keys(self):
        self._prepare()
        payload = {
            "profiles": {"architect": {"adapter": "claude", "model": "claude-opus-5"},
                         "supervisor": {"adapter": "claude", "model": "claude-opus-5"},
                         "implementer": {"adapter": "claude", "model": "claude-sonnet"},
                         "reviewer": {"adapter": "codex", "model": "gpt-5"}},
            "fallbacks": {role: [] for role in self.lib.SELECTABLE_AGENT_ROLES},
            "max_failovers_per_role": 1,
        }
        saved = self.lib.update_agent_settings(self.tmp, payload)
        self.assertEqual(saved["profiles"]["reviewer"], {"adapter": "codex", "model": "gpt-5"})
        text = (self.tmp / "handsoff.toml").read_text()
        self.assertIn('reviewer_followup = "claude"', text)
        self.assertIn('reviewer_followup = "claude-haiku"', text)
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(cfg["reviewer_followup"], self.FOLLOWUP)
        self.assertEqual(self.lib.agent_profiles(cfg)["reviewer"], {"adapter": "codex", "model": "gpt-5"})
        self.assertEqual(cfg["max_failovers_per_role"], 1)
        # The older unwrapped endpoint tolerates them too.
        profiles = {role: {"adapter": "claude", "model": "claude-opus-5"} for role in self.lib.SELECTABLE_AGENT_ROLES}
        profiles["reviewer"] = {"adapter": "codex", "model": "default"}
        self.assertEqual(self.lib.update_agent_config(self.tmp, profiles), profiles)
        cfg = self.lib.load_config(self.tmp)
        self.assertEqual(cfg["reviewer_followup"], self.FOLLOWUP)
        self.assertEqual(self.lib.agent_profiles(cfg)["reviewer"], {"adapter": "codex", "model": "default"})

    def test_malformed_escalation_tier_and_profile_refuse_cleanly(self):
        self._prepare()
        self.assertEqual(self._review().returncode, 0)
        base = self.read_status()
        self.assertEqual(self.lib.validate_status_schema(base), [])
        bad = dict(base, design_reviewer_escalation="no")
        self.assertEqual(self.lib.validate_status_schema(bad),
                         ["status: 'design_reviewer_escalation' must be an object or null"])
        bad = dict(base, design_reviewer_escalation={"by": "moncy", "at": "2026-01-01T00:00:00", "note": None,
                                                     "consumed_at": None})
        self.assertEqual(self.lib.validate_status_schema(bad),
                         ["status: 'design_reviewer_escalation.at' must include a timezone"])
        bad = dict(base, design_reviewer_escalation={"by": "", "at": "2026-01-01T00:00:00+00:00", "note": "",
                                                     "consumed_at": "soon"})
        self.assertEqual(self.lib.validate_status_schema(bad), [
            "status: 'design_reviewer_escalation.by' must be a non-empty string",
            "status: 'design_reviewer_escalation.consumed_at' must be an ISO-8601 timestamp",
            "status: 'design_reviewer_escalation.note' must be a non-empty string or null",
        ])
        self.assertEqual(self.lib.validate_status_schema(dict(base, design_reviewer_escalation=None)), [])
        bad = json.loads(json.dumps(base))
        bad["design_review"]["reviewer_profile"]["tier"] = "cheap"
        self.assertEqual(self.lib.validate_status_schema(bad),
                         ["status: 'design_review.reviewer_profile.tier' must be one of primary, followup"])
        bad = json.loads(json.dumps(base))
        bad["design_review"]["reviewer_profile"]["reason"] = "because"
        self.assertEqual(len(self.lib.validate_status_schema(bad)), 1)
        bad = json.loads(json.dumps(base))
        bad["design_review"]["structural_blocker"] = "no"
        self.assertEqual(self.lib.validate_status_schema(bad),
                         ["status: 'design_review.structural_blocker' must be boolean"])
        spec = self.runtime.build_launch_spec(self.tmp, "reviewer", "Review", which=self.both)
        self.assertEqual(self._launch(spec, 1), 0)
        bad = self.read_status()
        bad["agent_sessions"][self._sid(1)]["tier"] = "cheap"
        self.assertEqual(self.lib.validate_status_schema(bad),
                         [f"status: agent session '{self._sid(1)}'.tier must be null or one of primary, followup"])
        legacy = self.read_status()
        legacy["agent_sessions"][self._sid(1)].pop("tier")
        self.assertEqual(self.lib.validate_status_schema(legacy), [], "sessions written before #37 stay valid")
        # A hand edit on disk is refused by the next command through the audit block.
        tampered = self.read_status()
        tampered["design_reviewer_escalation"] = {"by": "x", "at": "now", "note": None, "consumed_at": None}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(tampered, indent=2))
        refused = self._escalate()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", refused.stdout)


class TestArchitectDesignApprovalGate(HandsoffTestCase):
    """The Architect role's core (AR1-AR3): /ship-feature opens with an
    Architect that collaboratively authors design + testable criteria,
    then a human -- never the Architect itself -- must explicitly
    approve before Phase 3+ opens. `design-approve` records that
    approval; Phase 3+ refuses to advance for any run `init` flagged
    `requires_design_approval` without one bound to the current
    acceptance hash, naming a human approver distinct from the
    architect. A run from before this feature (missing the flag) is
    completely unaffected -- this changes the ENTRY for new runs, not
    the existing phases."""

    def _new_project_root(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-test-architect-"))
        shutil.copy(ROOT / "handsoff.toml", root / "handsoff.toml")
        normalize_fixture_config(root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", root / "schemas")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _author_real_criterion(self, requirement="A real, specific, observable outcome",
                               test="pytest tests/test_real.py -q", root=None):
        root = root or self.tmp
        r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _write_status(self, status, root=None):
        """Write a status dict directly (to simulate a hand-crafted or
        pre-existing state no real command would produce) and re-anchor
        the event log to it exactly the way commit() would, so the write
        reads as legitimate rather than tripping the unrelated tamper
        detector -- these tests simulate specific states, not corruption."""
        root = root or self.tmp
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            lib.atomic_write_json(lib.status_path(root, cfg), status)
            lib.append_event(root, cfg, "test_backdate", "test harness adjusted status fields directly")

    def test_phase_3_blocked_without_non_self_design_approval(self):
        self.init()
        self.assertTrue(self.read_status()["requires_design_approval"])
        self._author_real_criterion()
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)

        blocked = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("design gate", blocked.stdout)
        self.assertEqual(self.read_status()["phase_number"], 2)

        self_approve = run(["design-approve", "--by", "arch-1", "--architect", "arch-1",
                            "--summary", "Approach and tradeoffs"], cwd=self.tmp)
        self.assertEqual(self_approve.returncode, 1)
        self.assertIn("self-approval", self_approve.stdout)
        self.assertIsNone(self.read_status()["design_approved"])

        # Gate-level self-approval refusal, independent of the command's
        # own check: hand-craft a self-approved record directly (bypassing
        # cmd_design_approve entirely) and confirm advance still refuses.
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(), "by": "same-id", "architect": "same-id",
            "design_hash": lib.design_hash(acceptance["criteria"]), "config_hash": lib.config_hash(cfg),
        }
        self._write_status(status)
        bypass = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(bypass.returncode, 1, bypass.stdout + bypass.stderr)
        self.assertIn("design gate", bypass.stdout)
        self.assertIn("self-approval", bypass.stdout)

        approve = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                      "--summary", "Approach and tradeoffs"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        review = approve_design_review(self.tmp, architect="arch-1")
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        ok = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)

    def test_design_approve_command_records_and_refuses_self_approval(self):
        self.init()
        self._author_real_criterion()
        for args, expect_substr in (
            (["design-approve", "--by", "", "--architect", "a", "--summary", "s"], "--by"),
            (["design-approve", "--by", "h", "--architect", "  ", "--summary", "s"], "--architect"),
            (["design-approve", "--by", "h", "--architect", "a", "--summary", "   "], "--summary"),
            (["design-approve", "--by", "same", "--architect", "same", "--summary", "s"], "self-approval"),
            # Whitespace/case variants of the same identity must not slip
            # past the self-approval check -- the load-bearing guarantee
            # this feature exists for.
            (["design-approve", "--by", "moncy ", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", " moncy", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", "Moncy", "--architect", "moncy", "--summary", "s"], "self-approval"),
            (["design-approve", "--by", "MONCY", "--architect", "moncy", "--summary", "s"], "self-approval"),
        ):
            r = run(args, cwd=self.tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(expect_substr, r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIsNone(self.read_status()["design_approved"])

        # The gate-level re-check (independent of the command's own
        # refusal) must also catch a whitespace/case-varied self-approval
        # hand-crafted directly into status.json, not only byte-identical
        # strings.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(), "by": "Moncy ", "architect": "moncy",
            "design_hash": lib.design_hash(acceptance["criteria"]), "config_hash": lib.config_hash(cfg),
        }
        self._write_status(status)
        bypass = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(bypass.returncode, 1, bypass.stdout + bypass.stderr)
        self.assertIn("self-approval", bypass.stdout)

        r = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                "--summary", "Approach: X. Tradeoffs: Y. Decisions: Z."], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESIGN_APPROVAL_RECORDED", r.stdout)
        status = self.read_status()
        record = status["design_approved"]
        self.assertEqual(record["by"], "moncy")
        self.assertEqual(record["architect"], "arch-1")
        self.assertTrue(record["at"])
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        self.assertEqual(record["design_hash"], lib.design_hash(acceptance["criteria"]))
        self.assertEqual(record["config_hash"], lib.config_hash(cfg))
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"design_approved"', events_text)
        self.assertIn("Approach: X. Tradeoffs: Y. Decisions: Z.", events_text)

    def test_criterion_mutation_invalidates_design_approval_and_rolls_back_to_phase_2(self):
        for phase in (3, 4, 5):
            root = self._new_project_root()
            self.assertEqual(run(["init", "Test feature"], cwd=root).returncode, 0)
            toml = root / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            self._author_real_criterion(root=root)
            self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                                  "--summary", "s"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "2", "20"], cwd=root).returncode, 0)
            self.assertEqual(approve_design_review(root, architect="arch-1").returncode, 0)
            self.assertEqual(run(["advance", "3", "30"], cwd=root).returncode, 0)
            if phase >= 4:
                self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=root).returncode, 0)
            if phase >= 5:
                self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=root).returncode, 0)

            status = json.loads((root / "handsoff-status.json").read_text())
            self.assertEqual(status["phase_number"], phase)
            self.assertIsNotNone(status["design_approved"])

            r = run(["criterion-update", "REQ-001", "--requirement", "A changed outcome"], cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = json.loads((root / "handsoff-status.json").read_text())
            self.assertIsNone(after["design_approved"])
            self.assertEqual(after["phase_number"], 2, f"starting phase {phase} should roll back to 2")

    def test_pre_existing_runs_without_the_new_field_are_unaffected(self):
        self.init()
        self._author_real_criterion()
        status = self.read_status()
        del status["requires_design_approval"]
        del status["requires_design_review"]
        status.pop("design_review", None)
        self._write_status(status)
        self.assertNotIn("requires_design_approval", self.read_status())

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        r = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_full_pre_existing_suite_unmodified_and_passing(self):
        """Re-affirms 2 pre-existing pipeline invariants -- self-approval
        blocked at review, deployment approval blocked before Phase 7 --
        hold byte-for-byte for a run that went through the new Architect
        gate. The broader 'nothing else in this file changed' half of
        this guarantee is structural: every pre-existing test class above
        this one is untouched by this feature's diff."""
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(test="true")
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-1").returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        self_review = run(["record-review", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(self_review.returncode, 1)
        self.assertIn("reviewer must differ", self_review.stdout)

        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        early_deploy = run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        self.assertEqual(early_deploy.returncode, 1)
        self.assertIn("Phase 7", early_deploy.stdout)

        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        live_r = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live_r.returncode, 0, live_r.stdout + live_r.stderr)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["status"], "complete")

    def test_end_to_end_architect_flow_through_existing_pipeline(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(requirement="The sample feature does the observable thing.", test="true")

        refused = run(["design-approve", "--by", "arch-1", "--architect", "arch-1",
                       "--summary", "Approach"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1)
        self.assertIsNone(self.read_status()["design_approved"])

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                       "--summary", "Approach: do X. Tradeoffs: Y vs Z. Decision: Y."], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-1").returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("SHIP_FEATURE_VALID", validated.stdout)

    def test_design_approve_refuses_on_untouched_placeholder_criteria(self):
        self.init()
        r = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("placeholder", r.stdout)
        self.assertIsNone(self.read_status()["design_approved"])

        self._author_real_criterion()
        ok = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_pre_existing_run_rollback_cascade_unaffected_by_new_gate(self):
        for phase in (4, 5):
            root = self._new_project_root()
            self.assertEqual(run(["init", "Test feature"], cwd=root).returncode, 0)
            toml = root / "handsoff.toml"
            toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
            self._author_real_criterion(root=root)
            self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                                  "--summary", "s"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "2", "20"], cwd=root).returncode, 0)
            self.assertEqual(approve_design_review(root, architect="arch-1").returncode, 0)
            self.assertEqual(run(["advance", "3", "30"], cwd=root).returncode, 0)
            self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=root).returncode, 0)
            if phase == 5:
                self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=root).returncode, 0)

            status = json.loads((root / "handsoff-status.json").read_text())
            self.assertEqual(status["phase_number"], phase)
            status.pop("requires_design_approval", None)
            status.pop("requires_design_review", None)
            status.pop("design_review", None)
            self._write_status(status, root=root)

            r = run(["criterion-update", "REQ-001", "--requirement", "A changed outcome"], cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = json.loads((root / "handsoff-status.json").read_text())
            # Pre-existing behavior: _invalidate_decisions only forces a
            # phase rollback when phase_number >= 6 (rollback_to=4); at
            # phase 4 or 5 it leaves phase_number untouched today. An
            # unflagged run must see that exact same behavior, not the
            # new phase-2 force-rollback this feature adds for flagged runs.
            self.assertEqual(after["phase_number"], phase,
                            f"unflagged run starting at phase {phase} must be left exactly as before this "
                            "feature (unchanged), not force-rolled to phase 2")
            self.assertNotIn("design_review", after,
                             "mutating a legacy run must not inject AR7 state into its status shape")

    def test_malformed_design_approved_rejected_by_schema(self):
        self.init()
        self._author_real_criterion()
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)

        status = self.read_status()
        status["design_approved"]["architect"] = ""
        self._write_status(status)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("design_approved.architect", r.stdout)

        status2 = self.read_status()
        status2["design_approved"]["at"] = "not-a-timestamp"
        self._write_status(status2)
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("design_approved.at", r2.stdout)

    def test_evidence_recording_never_invalidates_design_approval(self):
        self.init()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self._author_real_criterion(test="true")
        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                              "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-1").returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)

        before = self.read_status()
        self.assertIsNotNone(before["design_approved"])
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        after = self.read_status()
        self.assertEqual(after["design_approved"], before["design_approved"])
        self.assertEqual(after["phase_number"], 4)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertIsNotNone(self.read_status()["design_approved"])

        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-1"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "80", "--implemented-by", "impl-1"], cwd=self.tmp).returncode, 0)
        design_before_p6_evidence = self.read_status()["design_approved"]

        # Re-running verify at phase 6 must still re-invalidate review per
        # the pre-existing rollback_to=5 cascade (unchanged regression
        # coverage), while design_approved -- the new field -- survives.
        verify_again = run(["verify", "--criterion", "REQ-001", "--by", "impl-1"], cwd=self.tmp)
        self.assertEqual(verify_again.returncode, 0, verify_again.stdout + verify_again.stderr)
        after_p6 = self.read_status()
        self.assertIsNone(after_p6["review"], "pre-existing rollback_to=5 behavior must still clear review")
        self.assertEqual(after_p6["phase_number"], 5, "pre-existing rollback_to=5 behavior must still fire")
        self.assertEqual(after_p6["design_approved"], design_before_p6_evidence,
                         "design_approved must survive an evidence-recording call even at phase 6+")


class TestArchitectScalesDesignDepth(unittest.TestCase):
    """AR4: design rigor stays constant while discussion depth matches the task."""

    def setUp(self):
        self.prompt = (ROOT / "prompts" / "architect.md").read_text().lower()

    def test_architect_proposes_concise_or_full_depth_from_task_complexity(self):
        for phrase in (
            "propose one of these paths",
            "concise path",
            "small, clear, low-risk work",
            "full path",
            "large, ambiguous, high-risk, or cross-cutting work",
            "one-sentence reason",
        ):
            self.assertIn(phrase, self.prompt)

    def test_human_can_adjust_depth_without_bypassing_gates(self):
        for phrase in (
            '"go deeper"',
            '"that\'s enough, proceed"',
            "submit the smallest sufficient design for independent review",
            "it is not itself human design approval or permission to enter phase 3",
            "neither path bypasses independent design review or human approval",
        ):
            self.assertIn(phrase, self.prompt)


class TestArchitectRespectsSettledDesigns(HandsoffTestCase):
    """AR9: the Architect treats existing/shipped work as settled context
    to build around, proposing a change to it only on the human's
    explicit request (`design-approve --redesigns-settled-work`). The
    behavioral half of this guarantee (does the Architect actually fit
    vs. re-architect, and ask when ambiguous) is a prompt-governed LLM
    behavior, not deterministic code, and is verified separately by a
    recorded actor/judge agent scenario (manual evidence, see
    REQ-001/AR9-003/AR9-007 in the acceptance registry) -- not by a test
    in this class. This class covers the two MECHANICALLY testable
    guarantees: the new optional CLI flag, and that the AR1-3 core is
    unregressed."""

    def _author_real_criterion(self, requirement="A real, specific, observable outcome",
                               test="pytest tests/test_real.py -q"):
        r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_redesigns_settled_work_flag_optional_and_auditable(self):
        self.init()
        self._author_real_criterion()

        # Omitted (the default): no claim is made about touching settled work.
        r1 = run(["design-approve", "--by", "moncy", "--architect", "arch-1", "--summary", "s"], cwd=self.tmp)
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        status1 = self.read_status()
        self.assertIn("redesigns_settled_work", status1["design_approved"])
        self.assertIsNone(status1["design_approved"]["redesigns_settled_work"])

        # Explicitly passed but empty/whitespace-only: refused cleanly.
        r2 = run(["design-approve", "--by", "moncy", "--architect", "arch-2", "--summary", "s",
                 "--redesigns-settled-work", "   "], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn("--redesigns-settled-work", r2.stdout)
        self.assertNotIn("Traceback", r2.stdout + r2.stderr)

        # Explicit, non-empty value: recorded on status.design_approved
        # AND in the hash-chained event, auditable either way.
        note = "Changing the payment retry policy, per explicit human request"
        r3 = run(["design-approve", "--by", "moncy", "--architect", "arch-3", "--summary", "s2",
                 "--redesigns-settled-work", note], cwd=self.tmp)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        status3 = self.read_status()
        self.assertEqual(status3["design_approved"]["redesigns_settled_work"], note)
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn(note, events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)

    def test_ar1_ar3_core_unregressed(self):
        """Programmatically loads and RUNS the real, pre-existing
        TestArchitectDesignApprovalGate class (not a re-assertion of its
        behavior): a renamed or deleted class fails to resolve here, and
        a net shrinkage in its test methods fails the exact-count check,
        so this criterion is bound to the actual class, not a copy of
        what it once asserted."""
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestArchitectDesignApprovalGate)
        self.assertEqual(suite.countTestCases(), 10,
                        "TestArchitectDesignApprovalGate must still have exactly its known 10 test methods")
        runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(),
                       f"AR1-3 core regressed: {len(result.failures)} failures, {len(result.errors)} errors "
                       f"({[f[0].id() for f in result.failures] + [e[0].id() for e in result.errors]})")


class TestArchitectHandoffAndAuthorship(HandsoffTestCase):
    """AR5+AR6: the acceptance registry -- written exclusively via
    criterion-add/criterion-update, exactly as AR1-3 already required --
    hands off to the existing, unmodified Implementer/Reviewer/gate
    pipeline with no special-casing (AR5-002); design-approve's own
    --summary is retrievable straight from status.design_approved.summary,
    not only the event log (AR5-003); and every criterion present at a
    successful design-approve is stamped authored_by = the architect
    identity, an existing non-null stamp never reassigned by a later
    approval (AR6-004). authored_by is deliberately excluded from
    criterion_spec_hash (and therefore design_hash), the same way
    state/evidence already are: provenance about who proposed a criterion,
    not part of the claim being verified, so stamping it can never
    invalidate an already-recorded evidence binding or mismatch a freshly
    recomputed design_hash (AR6-005)."""

    def _enable_true_command(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))

    def _author_real_criterion(self, criterion_id="REQ-001",
                               requirement="A real, specific, observable outcome",
                               test="true", type_="primary_fix"):
        if criterion_id == "REQ-001":
            r = run(["criterion-update", "REQ-001", "--requirement", requirement, "--test", test], cwd=self.tmp)
        else:
            r = run(["criterion-add", criterion_id, "--type", type_, "--requirement", requirement,
                    "--verification", "automated", "--test", test], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _write_acceptance(self, acceptance):
        """Write acceptance.json directly (simulating a hand-edited or
        legacy registry) and re-anchor the event log to it, the same way
        TestArchitectDesignApprovalGate._write_status re-anchors a
        hand-crafted status -- so the write reads as a deliberate test
        fixture, not as tamper the unrelated chain-freshness check would
        otherwise (correctly) flag."""
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.acceptance_path(self.tmp, cfg), acceptance)
            lib.append_event(self.tmp, cfg, "test_backdate", "test harness adjusted acceptance directly")

    def test_audit_trail_shows_four_distinct_identities(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion()

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-ar5",
                       "--summary", "Approach: X."], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-ar5").returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-ar5"], cwd=self.tmp).returncode, 0)
        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-ar5"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-ar5"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-ar5"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-ar5"], cwd=self.tmp).returncode, 0)

        status = self.read_status()
        acceptance = self.read_acceptance()
        architect = status["design_approved"]["architect"]
        approver = status["design_approved"]["by"]
        implementer = status["implemented_by"]
        reviewer = status["reviewed_by"]
        design_reviewer = status["design_review"]["by"]
        identities = {architect, design_reviewer, approver, implementer, reviewer}
        self.assertEqual(len(identities), 5, f"expected 5 distinct identities, got {identities}")
        self.assertEqual(architect, "arch-ar5")
        self.assertEqual(design_reviewer, "test-design-reviewer")
        self.assertEqual(approver, "moncy")
        self.assertEqual(implementer, "impl-ar5")
        self.assertEqual(reviewer, "rev-ar5")

        # criterion.authored_by is retrievable straight from the acceptance
        # file too, not only from status.design_approved.architect.
        req001 = next(c for c in acceptance["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(req001["authored_by"], "arch-ar5")

        # The design-authoring event is in the same tamper-evident chain as
        # every other event, not a side channel.
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn('"kind":"design_approved"', events_text)
        log_r = run(["verify-log"], cwd=self.tmp)
        self.assertEqual(log_r.returncode, 0, log_r.stdout + log_r.stderr)
        self.assertIn("EVENT_LOG_INTACT", log_r.stdout)

    def test_registry_handoff_drives_existing_pipeline_unchanged(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="The primary observable outcome happens.")
        self._author_real_criterion(criterion_id="SUP-001", type_="supporting",
                                    requirement="A supporting observable outcome happens.")

        self.assertEqual(run(["design-approve", "--by", "moncy", "--architect", "arch-h",
                             "--summary", "s"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-h").returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "4", "40", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)

        verify_r = run(["verify", "--criterion", "REQ-001", "--criterion", "SUP-001", "--by", "impl-h"],
                       cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        run_id = json.loads(verify_r.stdout)["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(run(["record-symptom-resolved", "--evidence", run_id, "--by", "impl-h"],
                             cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "5", "50", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["record-review", "--by", "rev-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "6", "60", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "7", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "8", "100", "--implemented-by", "impl-h"], cwd=self.tmp).returncode, 0)

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("SHIP_FEATURE_VALID", validated.stdout)
        self.assertEqual(self.read_status()["status"], "complete")

        # Both registry-written criteria (one via criterion-update, one via
        # criterion-add) were carried through unmodified pipeline machinery,
        # and both got authored_by stamped at approval.
        acceptance = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in acceptance["criteria"] if x["id"] == cid)
            self.assertEqual(c["state"], "passing")
            self.assertEqual(c["authored_by"], "arch-h")

    def test_design_summary_stored_on_status_for_retrieval(self):
        self.init()
        self._author_real_criterion()
        summary = "Approach: clean registry handoff. Tradeoffs: none new. Decision: proceed."
        r = run(["design-approve", "--by", "moncy", "--architect", "arch-s",
                "--summary", summary], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        status = self.read_status()
        self.assertEqual(status["design_approved"]["summary"], summary,
                        "design summary must be retrievable directly from status.json, not only the event log")

        # Present in the hash-chained event too -- this adds a second,
        # directly-retrievable location, it does not replace the existing one.
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        self.assertIn(summary, events_text)

        # Survives a fresh reload from disk, not just held in memory.
        reread = json.loads((self.tmp / "handsoff-status.json").read_text())
        self.assertEqual(reread["design_approved"]["summary"], summary)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_authored_by_stamped_at_approval_and_never_reassigned(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="Primary observable outcome.")
        self._author_real_criterion(criterion_id="SUP-001", type_="supporting",
                                    requirement="Supporting observable outcome.")

        approve1 = run(["design-approve", "--by", "moncy", "--architect", "arch-1",
                       "--summary", "s1"], cwd=self.tmp)
        self.assertEqual(approve1.returncode, 0, approve1.stdout + approve1.stderr)

        acceptance = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in acceptance["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1")

        # Persists through a fresh reload from disk.
        reread = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in reread["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1")

        # Adding a criterion post-approval forces the pre-existing Phase-2
        # rollback (AR-003) and the new criterion starts unstamped.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        added = run(["criterion-add", "SUP-002", "--type", "supporting",
                    "--requirement", "A criterion added after approval.",
                    "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIsNone(self.read_status()["design_approved"])
        after_add = self.read_acceptance()
        sup002 = next(x for x in after_add["criteria"] if x["id"] == "SUP-002")
        self.assertNotIn("authored_by", sup002)
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in after_add["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1", "an earlier approval's authorship must never be reassigned")

        # A different architect approves the re-opened design: only the
        # newly-added, unstamped criterion picks up the new architect; the
        # two already-authored criteria keep their original authorship.
        approve2 = run(["design-approve", "--by", "moncy", "--architect", "arch-2",
                       "--summary", "s2"], cwd=self.tmp)
        self.assertEqual(approve2.returncode, 0, approve2.stdout + approve2.stderr)
        after_approve2 = self.read_acceptance()
        for cid in ("REQ-001", "SUP-001"):
            c = next(x for x in after_approve2["criteria"] if x["id"] == cid)
            self.assertEqual(c["authored_by"], "arch-1", "re-approval must never reassign existing authorship")
        sup002_after = next(x for x in after_approve2["criteria"] if x["id"] == "SUP-002")
        self.assertEqual(sup002_after["authored_by"], "arch-2")

        # authored_by explicitly null is treated exactly like an absent key:
        # eligible for stamping.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        explicit_null = run(["criterion-add", "SUP-003", "--type", "supporting",
                            "--requirement", "A criterion with authored_by forced null.",
                            "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(explicit_null.returncode, 0, explicit_null.stdout + explicit_null.stderr)
        acc = self.read_acceptance()
        for c in acc["criteria"]:
            if c["id"] == "SUP-003":
                c["authored_by"] = None
        self._write_acceptance(acc)
        approve3 = run(["design-approve", "--by", "moncy", "--architect", "arch-3",
                       "--summary", "s3"], cwd=self.tmp)
        self.assertEqual(approve3.returncode, 0, approve3.stdout + approve3.stderr)
        final = self.read_acceptance()
        sup003 = next(x for x in final["criteria"] if x["id"] == "SUP-003")
        self.assertEqual(sup003["authored_by"], "arch-3")
        for cid in ("REQ-001", "SUP-001", "SUP-002"):
            c = next(x for x in final["criteria"] if x["id"] == cid)
            self.assertNotEqual(c["authored_by"], "arch-3", "re-approval must never reassign existing authorship")

        # An explicit empty string is NON-null (unlike an absent key or an
        # explicit null), so the stamping guard must not treat it as
        # eligible for stamping -- but "" is also schema-invalid, so the
        # only way it could ever reach the registry is a hand-edit (already
        # out of contract per AR5-002). design-approve's own post-stamp
        # schema check catches that and refuses the whole approval rather
        # than silently overwriting or silently accepting the invalid
        # value. Round-1 review finding: the stamping guard originally used
        # a falsy check (`not c.get("authored_by")`), which would have
        # silently overwritten "" with the new architect instead of
        # refusing -- this is the regression test for that.
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        acc2 = self.read_acceptance()
        for c in acc2["criteria"]:
            if c["id"] == "SUP-003":
                c["authored_by"] = ""
        self._write_acceptance(acc2)
        approve4 = run(["design-approve", "--by", "moncy", "--architect", "arch-4",
                       "--summary", "s4"], cwd=self.tmp)
        self.assertEqual(approve4.returncode, 1, approve4.stdout + approve4.stderr)
        self.assertIn("authored_by", approve4.stdout)
        final2 = self.read_acceptance()
        sup003_after = next(x for x in final2["criteria"] if x["id"] == "SUP-003")
        self.assertEqual(sup003_after["authored_by"], "",
                        "a refused design-approve must not have touched the on-disk registry")

    def test_authored_by_schema_validated_and_stamping_is_hash_safe(self):
        self.init()
        self._enable_true_command()
        self._author_real_criterion(requirement="Primary observable outcome.")

        # -- Schema: optional, nullable, non-empty string when present.
        acceptance = self.read_acceptance()
        acceptance["criteria"][0]["authored_by"] = ""
        self._write_acceptance(acceptance)
        r = run(["validate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("authored_by", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

        acceptance2 = self.read_acceptance()
        acceptance2["criteria"][0]["authored_by"] = 42
        self._write_acceptance(acceptance2)
        r2 = run(["validate"], cwd=self.tmp)
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn("authored_by", r2.stdout)

        acceptance3 = self.read_acceptance()
        acceptance3["criteria"][0]["authored_by"] = None
        self._write_acceptance(acceptance3)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        acceptance4 = self.read_acceptance()
        del acceptance4["criteria"][0]["authored_by"]
        self._write_acceptance(acceptance4)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        acceptance5 = self.read_acceptance()
        acceptance5["criteria"][0]["authored_by"] = "arch-real"
        self._write_acceptance(acceptance5)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

        # -- Hash safety: real evidence recorded BEFORE authorship is
        # stamped must remain valid/passing AFTER stamping, and a fresh
        # Phase-3+ gate check right after approval must not see a stale
        # design-hash mismatch.
        acceptance6 = self.read_acceptance()
        del acceptance6["criteria"][0]["authored_by"]
        self._write_acceptance(acceptance6)

        verify_r = run(["verify", "--criterion", "REQ-001", "--by", "impl-hs"], cwd=self.tmp)
        self.assertEqual(verify_r.returncode, 0, verify_r.stdout + verify_r.stderr)
        self.assertEqual(self.read_acceptance()["criteria"][0]["state"], "passing")

        approved = run(["design-approve", "--by", "moncy", "--architect", "arch-hs",
                       "--summary", "s"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

        stamped = self.read_acceptance()
        self.assertEqual(stamped["criteria"][0]["authored_by"], "arch-hs")
        self.assertEqual(stamped["criteria"][0]["state"], "passing",
                        "stamping authored_by must not invalidate already-recorded evidence")

        validated = run(["validate"], cwd=self.tmp)
        self.assertEqual(validated.returncode, 0,
                        "stamping must not trip the evidence gate: " + validated.stdout + validated.stderr)

        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        self.assertEqual(approve_design_review(self.tmp, architect="arch-hs").returncode, 0)
        gate = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertNotIn("design gate", gate.stdout)

    def test_pre_existing_architect_and_settled_design_features_unregressed(self):
        """AR9 guardrail: build on AR1-3's design-approve, don't
        re-architect it. Programmatically runs the real, pre-existing
        TestArchitectDesignApprovalGate (AR1-3) and
        TestArchitectRespectsSettledDesigns (AR9) classes, bound to their
        actual method counts so a renamed/deleted/shrunk class fails here."""
        loader = unittest.TestLoader()
        for cls, expected_count in ((TestArchitectDesignApprovalGate, 10),
                                    (TestArchitectRespectsSettledDesigns, 2)):
            suite = loader.loadTestsFromTestCase(cls)
            self.assertEqual(suite.countTestCases(), expected_count,
                            f"{cls.__name__} must still have exactly its known {expected_count} test methods")
            runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
            result = runner.run(suite)
            self.assertTrue(result.wasSuccessful(),
                           f"{cls.__name__} regressed: {len(result.failures)} failures, {len(result.errors)} errors "
                           f"({[f[0].id() for f in result.failures] + [e[0].id() for e in result.errors]})")


class TestFallbackPolicy(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 24 fallback policy")
        sys.path.insert(0, str(BIN))
        global lib
        import handsoff_lib as lib

    def _profiles(self):
        return {
            role: {"adapter": "codex", "model": f"primary-{role}"}
            for role in lib.SELECTABLE_AGENT_ROLES
        }

    def _fallbacks(self):
        return {
            "architect": [{"adapter": "claude", "model": "opus"},
                          {"adapter": "codex", "model": "gpt-5.4"}],
            "supervisor": [{"adapter": "claude", "model": "default"}],
            "implementer": [{"adapter": "codex", "model": "fast"}],
            "reviewer": [{"adapter": "claude", "model": "sonnet"}],
        }

    def test_config_round_trip_preserves_order_and_legacy_defaults(self):
        legacy_text = re.sub(
            r"\n\[fallback_policy\]\n.*?(?=\n\[checks\])", "",
            (self.tmp / "handsoff.toml").read_text(), flags=re.DOTALL,
        )
        legacy_root = Path(tempfile.mkdtemp(prefix="handsoff-fallback-legacy-"))
        self.addCleanup(shutil.rmtree, legacy_root, ignore_errors=True)
        (legacy_root / "handsoff.toml").write_text(legacy_text)
        legacy = lib.load_config(legacy_root)
        self.assertEqual(legacy["fallbacks"], {role: [] for role in lib.SELECTABLE_AGENT_ROLES})
        self.assertEqual(legacy["max_failovers_per_role"], 2)

        payload = {"profiles": self._profiles(), "fallbacks": self._fallbacks(),
                   "max_failovers_per_role": 3}
        self.assertEqual(lib.update_agent_settings(self.tmp, payload), payload)
        loaded = lib.load_config(self.tmp)
        self.assertEqual(lib.agent_profiles(loaded), payload["profiles"])
        self.assertEqual(lib.fallback_profiles(loaded), payload["fallbacks"])
        self.assertEqual(loaded["max_failovers_per_role"], 3)
        self.assertEqual(loaded["fallbacks"]["architect"][0]["model"], "opus")

        stable = (self.tmp / "handsoff.toml").read_bytes()
        invalid_payloads = [
            {**payload, "max_failovers_per_role": True},
            {**payload, "max_failovers_per_role": 9},
            {**payload, "fallbacks": {**payload["fallbacks"], "architect": [
                {"adapter": "auto", "model": "default"}]}},
            {**payload, "fallbacks": {**payload["fallbacks"], "architect": [
                {"adapter": "codex", "model": "m"} for _ in range(9)]}},
            {**payload, "profiles": {**payload["profiles"], "reviewer": {
                "adapter": "codex", "model": "-bad"}}},
            {"profiles": payload["profiles"], "fallbacks": payload["fallbacks"]},
        ]
        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid):
                with self.assertRaises(lib.HandsoffError):
                    lib.update_agent_settings(self.tmp, invalid)
                self.assertEqual((self.tmp / "handsoff.toml").read_bytes(), stable)

        duplicate = (self.tmp / "handsoff.toml").read_text() + "\n[fallback_policy]\nmax_failovers_per_role = 1\n"
        (self.tmp / "handsoff.toml").write_text(duplicate)
        with self.assertRaises(lib.HandsoffError):
            lib.load_config(self.tmp)

    def test_selection_orders_and_skips_unavailable_profiles_with_audit(self):
        entries = [
            {"adapter": "claude", "model": "first"},
            {"adapter": "codex", "model": "second"},
            {"adapter": "codex", "model": "third"},
        ]
        with mock.patch.object(lib.shutil, "which", side_effect=AssertionError("planner discovered executables")), \
                mock.patch.object(lib, "load_config", side_effect=AssertionError("planner read settings")):
            decision = lib.plan_agent_fallback(
                "implementer", "rate_limit", entries,
                {"codex": True, "claude": False}, [], 0, 2,
            )
        self.assertEqual(decision, {
            "action": "select", "reason": "eligible_fallback",
            "profile": {"adapter": "codex", "model": "second"},
            "skipped": [{"index": 0, "reason": "adapter_unavailable"}],
        })

    def test_invalid_profiles_are_skipped_with_safe_audit_reasons(self):
        secret = "sk-never-return-this"
        decision = lib.plan_agent_fallback(
            "implementer", "process_crash", [
                {"adapter": "auto", "model": secret},
                {"adapter": "claude", "model": "offline"},
                {"adapter": "codex", "model": "used"},
                {"adapter": "codex", "model": "eligible"},
            ], {"codex": True, "claude": False}, [("codex", "used")], 0, 2,
        )
        self.assertEqual(decision["profile"], {"adapter": "codex", "model": "eligible"})
        self.assertEqual(decision["skipped"], [
            {"index": 0, "reason": "invalid_profile"},
            {"index": 1, "reason": "adapter_unavailable"},
            {"index": 2, "reason": "already_attempted"},
        ])
        serialized = json.dumps(decision)
        self.assertNotIn(secret, serialized)
        self.assertTrue(all(set(item) == {"index", "reason"} for item in decision["skipped"]))
        self.assertTrue(all(item["reason"] in lib.FALLBACK_SKIP_REASONS for item in decision["skipped"]))

    def test_recovery_categories_caps_and_exhaustion_pause(self):
        invalid_inputs = ("bad-role", [], {"codex": True}, "not-a-list", -1, 9)
        self.assertEqual(lib.plan_agent_fallback(*invalid_inputs[:1], "still_running", *invalid_inputs[1:]), {
            "action": "no_action", "reason": "agent_still_running", "profile": None, "skipped": [],
        })
        for category in ("cancelled", "unknown", "future_category"):
            self.assertEqual(lib.plan_agent_fallback(
                "bad-role", category, [], {}, "bad", -1, 9,
            )["reason"], "non_recoverable_failure")
        with self.assertRaises(lib.HandsoffError):
            lib.plan_agent_fallback("bad-role", "timeout", [], {"codex": True, "claude": True}, [], 0, 2)
        for category in lib.RECOVERABLE_FAILURE_CATEGORIES:
            decision = lib.plan_agent_fallback(
                "implementer", category, [], {"codex": True, "claude": True}, [], 2, 2,
            )
            self.assertEqual((decision["action"], decision["reason"]), ("pilot_pause", "cap_exhausted"))
        missing_reference = lib.plan_agent_fallback(
            "reviewer", "timeout", [], {"codex": True, "claude": True}, [], 2, 2, None,
        )
        self.assertEqual(missing_reference["reason"], "missing_independence_reference")
        exhausted = lib.plan_agent_fallback(
            "implementer", "timeout", [], {"codex": True, "claude": True}, [], 0, 2,
        )
        self.assertEqual(exhausted["reason"], "fallback_exhausted")

    def test_reviewer_independence_survives_fallback(self):
        immutable_session = {"adapter": "codex", "requested_model": "impl-model"}
        decision = lib.plan_agent_fallback(
            "reviewer", "context_exhaustion", [
                {"adapter": "codex", "model": "impl-model"},
                {"adapter": "codex", "model": "review-model"},
            ], {"codex": True, "claude": True}, [], 0, 2, immutable_session,
        )
        self.assertEqual(decision["skipped"], [{"index": 0, "reason": "reviewer_not_independent"}])
        self.assertEqual(decision["profile"], {"adapter": "codex", "model": "review-model"})
        audit_reference = {"effective_adapter": "claude", "model": "same"}
        different_adapter = lib.plan_agent_fallback(
            "reviewer", "timeout", [{"adapter": "codex", "model": "same"}],
            {"codex": True, "claude": True}, [], 0, 2, audit_reference,
        )
        self.assertEqual(different_adapter["action"], "select")
        for malformed in (None, {}, {"adapter": "auto", "requested_model": "x"},
                          {"adapter": "codex", "requested_model": "-bad"}):
            self.assertEqual(lib.plan_agent_fallback(
                "reviewer", "timeout", [], {"codex": True, "claude": True}, [], 0, 2, malformed,
            )["reason"], "missing_independence_reference")

    def test_dashboard_edits_fallbacks_without_mutating_current_session(self):
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="impl-live", adapter="codex",
            requested_model="primary-implementer", resolution_source="configured",
        )
        before_status = (self.tmp / "handsoff-status.json").read_bytes()
        payload = {"profiles": self._profiles(), "fallbacks": self._fallbacks(),
                   "max_failovers_per_role": 4}
        import handsoff_dashboard as dashboard
        raw = json.dumps(payload).encode()
        handler = object.__new__(dashboard.DashboardHandler)
        handler.path = "/api/settings/agents"
        handler.server = mock.Mock(project_root=self.tmp)
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler._same_origin_allowed = lambda: True
        responses = []
        handler._json_response = lambda status, value: responses.append((status, value))
        handler.do_POST()
        self.assertEqual(responses[0][0], 200, responses)
        result = responses[0][1]
        self.assertEqual(result["fallbacks"], payload["fallbacks"])
        self.assertEqual(result["max_failovers_per_role"], 4)
        self.assertEqual((self.tmp / "handsoff-status.json").read_bytes(), before_status)
        self.assertEqual(self.read_status()["agent_sessions"][session["session_id"]]["requested_model"],
                         "primary-implementer")

        stable_fallbacks = lib.fallback_profiles(lib.load_config(self.tmp))
        stable_cap = lib.load_config(self.tmp)["max_failovers_per_role"]
        lib.update_agent_config(self.tmp, self._profiles())
        lib.update_agent_config(self.tmp, {
            "architect": "claude", "implementer": "codex", "reviewer": "claude",
        })
        compatible = lib.load_config(self.tmp)
        self.assertEqual(lib.fallback_profiles(compatible), stable_fallbacks)
        self.assertEqual(compatible["max_failovers_per_role"], stable_cap)
        script = (ROOT / "dashboard" / "app.js").read_text()
        html = (ROOT / "dashboard" / "index.html").read_text()
        for phrase in ("+ ADD FALLBACK", "Move ${role} fallback", "Remove ${role} fallback",
                       "max-failovers", "max_failovers_per_role"):
            self.assertIn(phrase, script + html)
        self.assertIn("JSON.stringify(payload)", script)
        self.assertEqual(dashboard.MAX_SETTINGS_BODY, 16 * 1024)


class TestFailureClassification(unittest.TestCase):
    """Unit tests against lib.classify_runtime_failure and
    lib.should_failover_for_quality directly: pure functions, no CLI or
    filesystem involved, matching TestSummarizeDesignTiming's style below.
    Issue #26 -- built and tested standalone; deliberately NOT wired into
    handsoff_agent.py/handsoff_broker.py yet (see CRIT-005; the launcher
    wiring is deferred to a follow-up issue once #27 freezes the
    session/lifecycle schema)."""

    def setUp(self):
        sys.path.insert(0, str(BIN))
        global lib
        import handsoff_lib as lib

    def test_classifies_explicit_runner_outcomes(self):
        # (kwargs, expected_category) -- one clean-signature and one
        # adversarial/near-miss fixture per category, proving each
        # category is reached on its real signal and NOT on a near-miss.
        cases = [
            # cancelled: clean, then a near-miss (tail merely mentions
            # cancellation but the flag itself is false -- must not fire).
            ("cancelled/clean", dict(cancelled=True, exit_code=-15), "cancelled"),
            ("cancelled/near-miss", dict(cancelled=False, timed_out=False, exit_code=None,
                                         stderr_tail="the operator asked to cancel this run"), "unknown"),

            # timeout: clean, then a near-miss (tail says "timed out" but
            # the flag is false -- must not fire; falls through to unknown
            # since nothing else matches and the tail is non-empty).
            ("timeout/clean", dict(timed_out=True, exit_code=-1), "timeout"),
            ("timeout/near-miss", dict(timed_out=False, cancelled=False, exit_code=None,
                                       stdout_tail="the request timed out waiting for a response"), "unknown"),

            # auth_failure: clean signature, then near-miss text that
            # doesn't actually match any known pattern.
            ("auth_failure/clean", dict(exit_code=None,
                                        stderr_tail="401 Unauthorized: invalid api key provided"), "auth_failure"),
            ("auth_failure/near-miss", dict(exit_code=None,
                                            stderr_tail="please double-check your account configuration"), "unknown"),

            # rate_limit: clean signature, then a near-miss.
            ("rate_limit/clean", dict(exit_code=None,
                                      stderr_tail="429: rate limit exceeded, retry after 30s"), "rate_limit"),
            ("rate_limit/near-miss", dict(exit_code=None,
                                          stderr_tail="please slow down and try again later"), "unknown"),

            # context_exhaustion: clean signature, then a near-miss.
            ("context_exhaustion/clean", dict(exit_code=None,
                                              stderr_tail="context length exceeded for this model"), "context_exhaustion"),
            ("context_exhaustion/near-miss", dict(exit_code=None,
                                                  stderr_tail="the response was truncated unexpectedly"), "unknown"),

            # process_crash: clean (negative/signal-terminated exit code),
            # then a near-miss (a positive code that merely LOOKS like a
            # shell-reported "killed" status -- must resolve by sign only).
            ("process_crash/clean", dict(exit_code=-9), "process_crash"),
            ("process_crash/near-miss", dict(exit_code=137), "non_zero_exit"),

            # non_zero_exit: clean (a positive, non-signal exit code),
            # then a near-miss (a negative code -- must resolve as a crash,
            # never conflated with a plain non-zero exit).
            ("non_zero_exit/clean", dict(exit_code=1), "non_zero_exit"),
            ("non_zero_exit/near-miss", dict(exit_code=-2), "process_crash"),

            # unknown: clean (unrecognized non-empty tail, no exit code),
            # then a near-miss that must NOT be unknown -- an empty tail
            # with no exit code is still_running, the still_running/unknown
            # boundary itself.
            ("unknown/clean", dict(exit_code=None,
                                   stderr_tail="a completely unrelated diagnostic message"), "unknown"),
            ("unknown/near-miss (boundary with still_running)", dict(exit_code=None, stderr_tail=""), "still_running"),
        ]
        for label, kwargs, expected in cases:
            with self.subTest(case=label):
                result = lib.classify_runtime_failure(**kwargs)
                self.assertEqual(result["category"], expected)

        # Tie-break fixtures: tier 1 over tier 4.
        with self.subTest(case="tie-break: cancelled beats a signal-terminated exit_code"):
            result = lib.classify_runtime_failure(cancelled=True, exit_code=-9,
                                                   stderr_tail="irrelevant crash text")
            self.assertEqual(result["category"], "cancelled")

        # Tie-break: tier 3 (tail pattern) over tier 4 (exit-code sign).
        with self.subTest(case="tie-break: a rate-limit tail beats a plain non-zero exit code"):
            result = lib.classify_runtime_failure(exit_code=1, stderr_tail="429 rate limit exceeded")
            self.assertEqual(result["category"], "rate_limit")

        # Tie-break: fixed intra-tier-3 order -- auth_failure wins when a
        # tail matches both an auth-failure-shaped and a rate-limit-shaped
        # substring at once.
        with self.subTest(case="tie-break: auth_failure wins over rate_limit in the same tail"):
            result = lib.classify_runtime_failure(
                exit_code=None,
                stderr_tail="401 Unauthorized while refreshing token; rate limit exceeded on the retry")
            self.assertEqual(result["category"], "auth_failure")

    def test_absence_of_signal_returns_still_running_not_failure(self):
        result = lib.classify_runtime_failure(exit_code=None, timed_out=False, cancelled=False,
                                              stderr_tail="", stdout_tail="")
        self.assertEqual(result["category"], "still_running")
        self.assertNotIn(result["category"], (
            "rate_limit", "auth_failure", "context_exhaustion", "timeout",
            "process_crash", "cancelled", "non_zero_exit", "unknown",
        ))

        # The function has no call path to heartbeat/elapsed-time data at
        # all -- it cannot consult stall_warning()/stall_minutes, so it
        # cannot race or duplicate that pre-existing, unmodified authority.
        import inspect
        params = set(inspect.signature(lib.classify_runtime_failure).parameters)
        self.assertEqual(params, {"exit_code", "timed_out", "cancelled", "stderr_tail", "stdout_tail"})
        self.assertTrue(callable(lib.stall_warning))  # still exists, untouched, sole authority on silence

    def test_quality_failover_requires_bounded_retries_and_recorded_finding(self):
        # Boundary pair, finding_id present throughout.
        self.assertFalse(lib.should_failover_for_quality(retry_count=2, retry_limit=3, finding_id="finding-1"))
        self.assertTrue(lib.should_failover_for_quality(retry_count=3, retry_limit=3, finding_id="finding-1"))

        # Retry count past the limit, but no recorded finding -- refused.
        self.assertFalse(lib.should_failover_for_quality(retry_count=5, retry_limit=3, finding_id=None))
        self.assertFalse(lib.should_failover_for_quality(retry_count=5, retry_limit=3, finding_id=""))

        # Whitespace-only finding_id counts as absent -- refused.
        self.assertFalse(lib.should_failover_for_quality(retry_count=5, retry_limit=3, finding_id="   "))

        # A finding alone, with retries still available, is also refused
        # (AND, not OR -- one subjective result never causes failover).
        self.assertFalse(lib.should_failover_for_quality(retry_count=0, retry_limit=3, finding_id="finding-1"))

    def test_classification_never_persists_raw_secret_bearing_output(self):
        secret = "Authorization: Bearer sk-FAKE-SECRET-DO-NOT-LEAK-1234567890"
        result = lib.classify_runtime_failure(exit_code=None, stderr_tail=secret)

        serialized = json.dumps(result)
        self.assertNotIn("sk-FAKE-SECRET-DO-NOT-LEAK-1234567890", serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn(secret, serialized)

        # Only a category (from the closed set), a closed-set reason label,
        # and a digest -- nothing else.
        self.assertEqual(set(result), {"category", "reason", "tail_sha256"})
        self.assertIn(result["category"], lib.FAILURE_CATEGORIES)
        self.assertEqual(len(lib.FAILURE_CATEGORIES), 9)  # 8 from CRIT-001 + still_running from CRIT-002
        closed_set_reasons = {
            "cancelled": "run was cancelled",
            "timeout": "runner exceeded its timeout",
            "auth_failure": "authentication or authorization failed",
            "rate_limit": "rate limit or quota exhausted",
            "context_exhaustion": "context window exhausted",
            "process_crash": "process was terminated by a signal",
            "non_zero_exit": "process exited with a non-zero status",
            "unknown": "failure signal matched no known category",
            "still_running": "no failure signal reported yet",
        }
        self.assertEqual(set(lib.FAILURE_CATEGORIES), set(closed_set_reasons))
        self.assertEqual(result["reason"], closed_set_reasons[result["category"]])
        self.assertRegex(result["tail_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["tail_sha256"], __import__("hashlib").sha256(secret.encode()).hexdigest())


class TestAgentReplacement(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 23 agent replacement")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_lib
        self.runtime = handsoff_agent
        self.lib = handsoff_lib
        self.repo = {
            "head": "a" * 40, "branch": "main", "dirty": True,
            "status_sha256": "b" * 64,
        }

    def _policy(self, role, entries, cap=2):
        cfg = self.lib.load_config(self.tmp)
        payload = {
            "profiles": self.lib.agent_profiles(cfg),
            "fallbacks": self.lib.fallback_profiles(cfg),
            "max_failovers_per_role": cap,
        }
        payload["fallbacks"][role] = entries
        self.lib.update_agent_settings(self.tmp, payload)

    def _session(self, role="implementer", adapter="codex", model="primary", state="failed",
                 category="non_zero_exit"):
        session = self.lib.create_agent_session(
            self.tmp, role=role, actor=f"{adapter}-{role}", adapter=adapter,
            requested_model=model, resolution_source="configured",
        )
        self.lib.transition_agent_session(self.tmp, session["session_id"], "running")
        if state == "completed":
            self.lib.transition_agent_session(self.tmp, session["session_id"], "completed", exit_code=0)
        else:
            kwargs = {"exit_code": -9} if category == "process_crash" else {"exit_code": 1}
            if category == "cancelled":
                kwargs = {"cancelled": True}
            elif category == "timeout":
                kwargs = {"timed_out": True}
            elif category == "unknown":
                kwargs = {"stderr_tail": "unrecognized host diagnostic"}
            failure = self.lib.classify_runtime_failure(**kwargs)
            terminal_state = "cancelled" if category == "cancelled" else "timed_out" if category == "timeout" else "failed"
            self.lib.transition_agent_session(
                self.tmp, session["session_id"], terminal_state,
                exit_code=130 if category == "cancelled" else 124 if category == "timeout" else 1,
                failure=failure,
            )
        return session["session_id"]

    @staticmethod
    def _process(returncode=0, timeout=False):
        fail_timeout = timeout
        class Process:
            pid = None
            def __init__(self):
                self.returncode = returncode
                self.terminated = 0
                self.killed = 0
                self.waits = 0
            def communicate(self, *, input, timeout):
                if fail_timeout:
                    raise subprocess.TimeoutExpired("agent", timeout)
                return None
            def terminate(self):
                self.terminated += 1
            def kill(self):
                self.killed += 1
                self.returncode = -9
            def wait(self, timeout=None):
                self.waits += 1
                if self.terminated and not self.killed:
                    raise subprocess.TimeoutExpired("agent", timeout)
                return self.returncode
        return Process()

    def _reserve(self, session_id, **kwargs):
        return self.lib.reserve_agent_replacement(
            self.tmp, from_session_id=session_id,
            which=lambda adapter: f"/bin/{adapter}", snapshotter=lambda root: dict(self.repo),
            **kwargs,
        )

    def _set_review_round(self, value):
        with self.lib.project_lock(self.tmp):
            cfg = self.lib.load_config(self.tmp)
            status = self.lib.load_unique_json(self.lib.status_path(self.tmp, cfg))
            status["review_round"] = value
            # #31 derives review_round from the structured ledger: it must
            # equal legacy_review_round_offset + len(review_attempts). A
            # trusted-round fixture therefore moves the legacy offset, the
            # same thing migrate_review_ledger does for a pre-#31 status.
            if "review_attempts" in status:
                status["legacy_review_round_offset"] = value - len(status.get("review_attempts") or [])
            self.lib.commit(
                self.tmp, cfg, status=status, event_kind="test_review_round_advanced",
                event_message=f"Trusted review round advanced to {value}", review_round=value,
            )

    def test_successful_running_replacement_launches_fresh_same_phase_session(self):
        self._policy("implementer", [{"adapter": "claude", "model": "sonnet"}])
        source = self._session()
        before = self.read_status()
        record = self._reserve(source)
        self.assertEqual(record["action"], "launch")
        self.assertNotEqual(record["to_session_id"], source)
        self.assertEqual(record["selected_profile"], {"adapter": "claude", "model": "sonnet"})
        fallback = self.runtime.build_profile_launch_spec(
            self.tmp, "implementer", "trusted in-memory task", record["selected_profile"],
            which=lambda adapter: f"/bin/{adapter}",
        )
        claim_seen_before_spawn = []
        def popen_after_claim(*args, **kwargs):
            live = self.read_status()["agent_replacements"][0]
            claim_seen_before_spawn.append((live["state"], live["handoff"]["state"]))
            return self._process()
        self.assertEqual(self.runtime.execute_launch(
            fallback, precreated_session_id=record["to_session_id"],
            popen_factory=popen_after_claim,
        ), 0)
        self.assertEqual(claim_seen_before_spawn, [("claimed", "claimed")])
        after = self.read_status()
        self.assertEqual(after["agent_sessions"][record["to_session_id"]]["state"], "completed")
        self.assertEqual((after["agent_replacements"][0]["state"],
                          after["agent_replacements"][0]["handoff"]["state"]),
                         ("recovered", "recovered"))
        self.assertEqual((after["phase_number"], after["progress"]),
                         (before["phase_number"], before["progress"]))
        replay_spawn = mock.Mock(return_value=self._process())
        with self.assertRaisesRegex(self.lib.HandsoffError, "exact reservation"):
            self.runtime.execute_launch(
                fallback, precreated_session_id=record["to_session_id"],
                popen_factory=replay_spawn,
            )
        replay_spawn.assert_not_called()

    def test_active_stop_escalates_and_terminalizes_once(self):
        process = self._process(timeout=True)
        spec = self.runtime.LaunchSpec(
            "implementer", "codex", "default", ("/bin/codex",), str(self.tmp), "private", "configured",
        )
        with self.assertRaises(self.runtime.AgentLaunchError):
            self.runtime.execute_launch(spec, timeout=1, popen_factory=mock.Mock(return_value=process))
        self.assertEqual((process.terminated, process.killed), (1, 1))
        status = self.read_status()
        session_id = status["current_agent_sessions"]["implementer"]
        self.assertEqual(status["agent_sessions"][session_id]["state"], "timed_out")
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        terminal = [e for e in events if e.get("session_id") == session_id
                    and e.get("state") in self.lib.AGENT_SESSION_TERMINAL_STATES]
        self.assertEqual(len(terminal), 1)

    def test_failed_relaunch_tries_next_fallback_within_cap(self):
        self._policy("implementer", [
            {"adapter": "claude", "model": "first"},
            {"adapter": "codex", "model": "second"},
        ], cap=2)
        spec = self.runtime.LaunchSpec(
            "implementer", "codex", "primary", ("/bin/codex",), str(self.tmp), "secret task", "configured",
        )
        outcomes = [self._process(returncode=1), OSError("missing runner"), self._process()]
        def launch(*args, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        self.assertEqual(self.runtime.execute_with_recovery(
            spec, popen_factory=launch, which=lambda adapter: f"/bin/{adapter}",
            snapshotter=lambda root: dict(self.repo),
        ), 0)
        status = self.read_status()
        self.assertEqual([r["selected_profile"]["model"] for r in status["agent_replacements"]],
                         ["first", "second"])
        self.assertEqual([r["state"] for r in status["agent_replacements"]],
                         ["failed", "recovered"])
        self.assertEqual([r["handoff"]["state"] for r in status["agent_replacements"]],
                         ["failed", "recovered"])
        self.assertEqual(len(status["agent_replacements"]), 2)
        self.assertEqual(status["agent_sessions"][status["current_agent_sessions"]["implementer"]]["state"],
                         "completed")

    def test_nonrecoverable_and_unbounded_quality_requests_are_refused(self):
        self._policy("implementer", [{"adapter": "claude", "model": "fallback"}])
        cancelled = self._session(category="cancelled")
        before_sessions = set(self.read_status()["agent_sessions"])
        paused = self._reserve(cancelled)
        self.assertEqual((paused["action"], paused["reason"]),
                         ("pilot_pause", "non_recoverable_failure"))
        self.assertEqual(set(self.read_status()["agent_sessions"]), before_sessions)
        completed = self._session(state="completed")
        stable = (self.tmp / "handsoff-status.json").read_bytes()
        with self.assertRaisesRegex(self.lib.HandsoffError, "closed set"):
            self.lib.record_quality_finding(
                self.tmp, session_id=completed, finding_code="caller says output was bad",
            )
        self.assertEqual((self.tmp / "handsoff-status.json").read_bytes(), stable)
        import handsoff_broker
        request = {
            "actor": "supervisor", "project_root": str(self.tmp.resolve()),
            "action": "quality_finding", "session_id": completed,
            "finding_code": "acceptance_not_met",
        }
        def broker_launcher(spec, **kwargs):
            self.assertIn((self.tmp / "prompts" / "implementer.md").read_text().strip(), spec.stdin)
            return self.runtime.execute_with_recovery(
                spec, which=lambda adapter: f"/bin/{adapter}",
                snapshotter=lambda root: dict(self.repo), **kwargs,
            )
        with self.assertRaisesRegex(self.lib.HandsoffError, "quality_boundary_not_reached"):
            handsoff_broker.dispatch_supervisor_request(
                self.tmp, request, agent_launcher=broker_launcher,
            )
        finding = self.read_status()["agent_quality_findings"][-1]
        self.assertFalse(finding["eligible"])
        self.assertEqual(finding["review_round"], 0)
        self.assertEqual(self.read_status()["agent_replacements"][-1]["state"], "pilot_pause")
        stable = (self.tmp / "handsoff-status.json").read_bytes()
        with self.assertRaisesRegex(self.lib.HandsoffError, "already exists"):
            handsoff_broker.dispatch_supervisor_request(
                self.tmp, {**request, "finding_code": "incorrect_implementation"},
                agent_launcher=broker_launcher,
            )
        self.assertEqual((self.tmp / "handsoff-status.json").read_bytes(), stable)
        for review_round in (1, 2, 3):
            self._set_review_round(review_round)
            boundary = self.lib.record_quality_finding(
                self.tmp, session_id=completed, finding_code="acceptance_not_met",
            )
        self.assertEqual(boundary["review_round"], 3)
        self.assertTrue(boundary["eligible"])

    def test_handoff_is_derived_bounded_and_secret_free(self):
        secret = "sk-private-caller-task-and-token"
        acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        acceptance["criteria"][0].update({"id": "REQ-001", "state": "passing", "evidence": ["run-safe"]})
        acceptance["criteria"].append({
            **acceptance["criteria"][0], "id": "REQ-002", "state": "not_tested",
            "requirement": secret, "evidence": [],
        })
        with self.lib.project_lock(self.tmp):
            self.lib.commit(
                self.tmp, self.lib.load_config(self.tmp), acceptance=acceptance,
                event_kind="test_acceptance_prepared",
                event_message="Prepared trusted acceptance state for replacement handoff",
            )
        self._policy("implementer", [{"adapter": "claude", "model": "safe"}])
        source = self._session()
        record = self._reserve(source)
        handoff = record["handoff"]
        self.assertEqual(handoff["passing_criterion_ids"], ["REQ-001"])
        self.assertIn("REQ-002", handoff["remaining_criterion_ids"])
        self.assertEqual(handoff["evidence_ids"], ["run-safe"])
        self.assertEqual(handoff["repository"], self.repo)
        serialized = json.dumps(record)
        self.assertNotIn(secret, serialized)
        for forbidden in ("prompt", "output", "environment", "credential", "token"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_reviewer_independence_and_governance_gates_are_preserved(self):
        self._session(role="implementer", adapter="codex", model="impl", state="completed")
        self._policy("reviewer", [
            {"adapter": "codex", "model": "impl"},
            {"adapter": "claude", "model": "independent"},
        ])
        reviewer = self._session(role="reviewer", model="review-primary")
        status_before = self.read_status()
        original_binding = status_before["reviewer_implementer_bindings"][reviewer]
        self.assertEqual((original_binding["adapter"], original_binding["model"]), ("codex", "impl"))
        self._session(role="implementer", adapter="claude", model="independent", state="completed")
        acceptance_before = (self.tmp / "handsoff-acceptance.json").read_bytes()
        record = self._reserve(reviewer)
        self.assertEqual(record["selected_profile"], {"adapter": "claude", "model": "independent"})
        status_after = self.read_status()
        copied_binding = status_after["reviewer_implementer_bindings"][record["to_session_id"]]
        self.assertEqual((copied_binding["adapter"], copied_binding["model"]), ("codex", "impl"))
        governed = ("phase_number", "phase", "progress", "status", "requirement_coverage",
                    "design_review", "design_approved", "deployment_approved", "review")
        self.assertEqual({key: status_after.get(key) for key in governed},
                         {key: status_before.get(key) for key in governed})
        self.assertEqual((self.tmp / "handsoff-acceptance.json").read_bytes(), acceptance_before)


class TestVerificationCache(HandsoffTestCase):
    """Issue #43: `verify` binds every launched command to the exact state
    it proves something about (command, repository digest including dirty
    state, verification_config_hash, the specs of the criteria verified
    with it) and reuses an eligible executed, passing record of the same
    binding within the run instead of launching again. Criteria
    i43-batch-once, i43-cache-hit-and-invalidation,
    i43-never-reuse-failures-live-regression, i43-concurrency-and-fixture."""

    COUNTER = "\n".join([
        "import pathlib, sys, time",
        "log = pathlib.Path(sys.argv[1])",
        "code = int(sys.argv[2]) if len(sys.argv) > 2 else 0",
        "delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0",
        "chars = int(sys.argv[4]) if len(sys.argv) > 4 else 0",
        "with log.open('a') as fh:",
        "    fh.write('launch\\n')",
        "if delay:",
        "    time.sleep(delay)",
        "if chars:",
        "    sys.stdout.write('x' * chars)",
        "sys.exit(code)",
        "",
    ])

    def setUp(self):
        super().setUp()
        # AC8: every archive write this class could trigger lands in a
        # temp dir, and the real Documents archive is counted before and
        # after so an accidental write there would fail the test.
        self.archive_dir = Path(tempfile.mkdtemp(prefix="handsoff-archive-cache-"))
        self._old_env = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.archive_dir)
        self.documents_archive = Path.home() / "Documents" / "Handsoff-Archive"
        self.documents_count_before = self._documents_archive_count()
        # The launch log lives OUTSIDE the project root: a file written into
        # the root would itself change the repository digest and hide what
        # the cache is being tested for.
        self.counter_dir = Path(tempfile.mkdtemp(prefix="handsoff-launches-"))
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "count.py").write_text(self.COUNTER)
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "app.py").write_text("VERSION = 1\n")
        sys.path.insert(0, str(BIN))
        import handsoff_lib
        import handsoff_supervisor
        self.lib = handsoff_lib
        self.supervisor = handsoff_supervisor

    def tearDown(self):
        self.assertEqual(self._documents_archive_count(), self.documents_count_before,
                         "the real ~/Documents/Handsoff-Archive must never be written by a test")
        if self._old_env is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self._old_env
        shutil.rmtree(self.archive_dir, ignore_errors=True)
        shutil.rmtree(self.counter_dir, ignore_errors=True)
        super().tearDown()

    # -- fixture helpers ---------------------------------------------------

    def _documents_archive_count(self):
        if not self.documents_archive.exists():
            return 0
        return sum(1 for _ in self.documents_archive.iterdir())

    def _command(self, name, exit_code=0, delay=0, chars=0):
        parts = ["python3", "tests/count.py", str(self.counter_dir / f"{name}.log")]
        if exit_code or delay or chars:
            parts.append(str(exit_code))
        if delay or chars:
            parts.append(str(delay))
        if chars:
            parts.append(str(chars))
        return " ".join(parts)

    def _launches(self, name):
        log = self.counter_dir / f"{name}.log"
        return log.read_text().count("launch") if log.exists() else 0

    def _configure(self, commands, extra_toml="", timeout=None):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace("commands = []", f"commands = {json.dumps(list(commands))}", 1)
        if timeout is not None:
            text = text.replace("timeout_seconds = 600", f"timeout_seconds = {timeout}", 1)
        toml.write_text(text + extra_toml)

    def _git_init(self):
        shutil.copy(ROOT / ".gitignore", self.tmp / ".gitignore")
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.email", "test@example.com"],
                     ["git", "config", "user.name", "Cache Test"],
                     ["git", "add", "."],
                     ["git", "commit", "-qm", "fixture"]):
            subprocess.run(args, cwd=self.tmp, check=True, capture_output=True)

    def _add(self, cid, test, verification="automated"):
        added = run(["criterion-add", cid, "--type", "supporting",
                     "--requirement", f"#43 fixture outcome {cid}",
                     "--verification", verification, "--test", test], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

    def _bind_first(self, test):
        bound = run(["criterion-update", "REQ-001", "--test", test], cwd=self.tmp)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)

    def _verify(self, *criteria, by="runner", expect=0, extra=()):
        args = ["verify"]
        for cid in criteria:
            args += ["--criterion", cid]
        args += ["--by", by, *extra]
        result = run(args, cwd=self.tmp)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def _records(self):
        records, problems = self.lib.load_verifications(self.tmp, self.lib.load_config(self.tmp))
        self.assertEqual(problems, [])
        return records

    def _last_event(self, kind):
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                  if line.strip()]
        return next(event for event in reversed(events) if event.get("kind") == kind)

    # -- i43-batch-once ----------------------------------------------------

    def test_five_criteria_naming_one_command_launch_it_once(self):
        command = self._command("a")
        self._configure([command])
        self.init("Cache fixture")
        self._bind_first(command)
        ids = ["REQ-001"]
        for n in range(2, 6):
            self._add(f"REQ-00{n}", command)
            ids.append(f"REQ-00{n}")
        payload = self._verify(*ids)
        self.assertEqual(self._launches("a"), 1)
        self.assertEqual(payload["launched"], [command])
        self.assertEqual(payload["reused"], {})
        self.assertEqual(len(payload["criteria"]), 5)
        records = [r for r in self._records() if r["kind"] == "checks"]
        self.assertEqual(len(records), 5)
        self.assertEqual({r["criteria"][0] for r in records}, set(ids))
        for record in records:
            self.assertTrue(record["ok"])
            self.assertIs(record["executed"], True)
            self.assertIsNone(record["reused_from"])
            self.assertEqual(set(record["binding"]), {command})
            self.assertRegex(record["binding"][command], r"^[0-9a-f]{64}$")
            self.assertRegex(record["feature_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual([r["command"] for r in record["results"]], [command])
            self.assertIs(record["results"][0]["timed_out"], False)
            self.assertIs(record["results"][0]["truncated"], False)
            self.assertNotIn("output_tail", record["results"][0])
        self.assertEqual({c["state"] for c in self.read_acceptance()["criteria"]}, {"passing"})
        event = self._last_event("checks_run")
        self.assertEqual((event["launched_count"], event["reused_count"]), (1, 0))
        for cid in ids:
            self.assertIs(event["criteria"][cid]["executed"], True)

    # -- i43-cache-hit-and-invalidation ------------------------------------

    def test_second_verify_against_unchanged_state_launches_nothing(self):
        command = self._command("a")
        self._configure([command])
        self.init("Cache fixture")
        self._bind_first(command)
        self._add("REQ-002", command)
        first = self._verify("REQ-001", "REQ-002")
        original = first["criteria"]["REQ-001"]["run_id"]
        second = self._verify("REQ-001", "REQ-002")
        self.assertEqual(self._launches("a"), 1)
        self.assertEqual(second["launched"], [])
        self.assertEqual(set(second["reused"]), {command})
        self.assertIn(second["reused"][command], {original, first["criteria"]["REQ-002"]["run_id"]})
        for cid in ("REQ-001", "REQ-002"):
            self.assertIs(second["criteria"][cid]["executed"], False)
            self.assertEqual(second["criteria"][cid]["reused_from"], second["reused"][command])
        records = self._records()
        reused = [r for r in records if r.get("executed") is False]
        self.assertEqual(len(reused), 2)
        source = next(r for r in records if r["run_id"] == second["reused"][command])
        for record in reused:
            self.assertEqual(record["reused_from"], source["run_id"])
            self.assertEqual(record["binding"], source["binding"])
            self.assertEqual(
                [{k: v for k, v in r.items() if k != "reused_from"} for r in record["results"]],
                source["results"])
            self.assertEqual(record["results"][0]["reused_from"], source["run_id"])
        event = self._last_event("checks_run")
        self.assertEqual((event["launched_count"], event["reused_count"]), (0, 1))
        self.assertEqual({c["state"] for c in self.read_acceptance()["criteria"]}, {"passing"})
        # --no-cache launches again even though an eligible record exists.
        forced = self._verify("REQ-001", "REQ-002", extra=("--no-cache",))
        self.assertEqual(self._launches("a"), 2)
        self.assertEqual(forced["launched"], [command])
        self.assertIs(forced["criteria"]["REQ-001"]["executed"], True)

    def test_each_bound_input_change_causes_a_fresh_launch(self):
        command = self._command("a")
        self._configure([command])
        self._git_init()
        self.init("Cache fixture")
        self._bind_first(command)
        self._verify("REQ-001")
        self.assertEqual(self._launches("a"), 1)
        expected = 1

        def expect_launch(label, expected):
            payload = self._verify("REQ-001")
            self.assertEqual(self._launches("a"), expected, f"{label}: expected a fresh launch")
            self.assertEqual(payload["launched"], [command], label)
            self.assertIs(payload["criteria"]["REQ-001"]["executed"], True, label)
            # The new state is now cached: an immediate repeat reuses it.
            repeat = self._verify("REQ-001")
            self.assertEqual(self._launches("a"), expected, f"{label}: repeat must reuse")
            self.assertIs(repeat["criteria"]["REQ-001"]["executed"], False, label)

        with self.subTest(change="editing a tracked file"):
            (self.tmp / "src" / "app.py").write_text("VERSION = 2\n")
            expected += 1
            expect_launch("tracked edit", expected)
        with self.subTest(change="creating an untracked file"):
            (self.tmp / "src" / "new_module.py").write_text("NEW = True\n")
            expected += 1
            expect_launch("untracked file", expected)
        with self.subTest(change="changing the command list in toml"):
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace(
                f"commands = {json.dumps([command])}", f"commands = {json.dumps([command, self._command('b')])}", 1))
            expected += 1
            expect_launch("command list", expected)
        with self.subTest(change="changing a governance config value"):
            toml = self.tmp / "handsoff.toml"
            text = toml.read_text()
            self.assertIn("max_review_rounds = ", text)
            toml.write_text(re.sub(r"^max_review_rounds = \d+$", "max_review_rounds = 9", text, count=1, flags=re.M))
            expected += 1
            expect_launch("governance value", expected)
        with self.subTest(change="criterion-update of the bound criterion"):
            changed = run(["criterion-update", "REQ-001", "--requirement", "#43 the bound claim, revised"],
                          cwd=self.tmp)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            expected += 1
            expect_launch("criterion spec", expected)

    def test_digest_and_config_hash_bind_exactly_what_the_design_names(self):
        command = self._command("a")
        self._configure([command])
        self.init("Cache fixture")
        cfg = self.lib.load_config(self.tmp)
        # Non-git root: Handsoff's own generated files never churn the digest.
        before = self.lib.repository_digest(self.tmp, cfg)
        self._bind_first(command)
        self._verify("REQ-001")
        self.assertEqual(self.lib.repository_digest(self.tmp, cfg), before,
                         "evidence writes and the in-flight lock directory must not change the digest")
        (self.tmp / "src" / "app.py").write_text("VERSION = 2\n")
        self.assertNotEqual(self.lib.repository_digest(self.tmp, cfg), before)
        # verification_config_hash: governance keys, check commands, the
        # timeout, and regression groups are bound; agent/model/paths not.
        base = self.lib.verification_config_hash(cfg)
        self.assertNotEqual(base, self.lib.config_hash(cfg))
        for key, value in (("check_commands", cfg["check_commands"] + ["true"]),
                           ("check_timeout_seconds", cfg["check_timeout_seconds"] + 1),
                           ("regressions", [{"name": "full", "commands": ["python3 tests/test_everything.py"]}]),
                           ("max_review_rounds", cfg["max_review_rounds"] + 1)):
            with self.subTest(bound=key):
                self.assertNotEqual(self.lib.verification_config_hash({**cfg, key: value}), base)
        for key, value in (("agents", {"implementer": "codex"}), ("models", {"implementer": "x"}),
                           ("status_file", "elsewhere.json"), ("live_check_commands", ["true"]),
                           ("recovery", {"enabled": False}), ("tickets", [{"number": 1}])):
            with self.subTest(unbound=key):
                self.assertEqual(self.lib.verification_config_hash({**cfg, key: value}), base)
        binding = self.lib.verification_binding(command, "r", "c", ["h2", "h1"])
        self.assertEqual(binding, self.lib.verification_binding(command, "r", "c", ["h1", "h2"]))
        self.assertNotEqual(binding, self.lib.verification_binding(command, "r", "c", ["h1"]))
        self.assertNotEqual(binding, self.lib.verification_binding(command + " ", "r", "c", ["h1", "h2"]))
        status = self.read_status()
        events = self.lib.read_events(self.tmp, cfg)
        self.assertEqual(self.lib.feature_hash(status, events),
                         hashlib.sha256((status["feature"] + events[0]["at"]).encode()).hexdigest())

    # -- i43-never-reuse-failures-live-regression ----------------------------

    def test_failed_timed_out_and_truncated_results_are_never_reused(self):
        failing = self._command("f", exit_code=1)
        hanging = self._command("t", delay=5)
        chatty = self._command("c", chars=self.lib.CHECK_OUTPUT_TAIL_CHARS + 1)
        self._configure([failing, hanging, chatty], timeout=1)
        self.init("Cache fixture")
        self._bind_first(failing)
        self._add("REQ-002", hanging)
        self._add("REQ-003", chatty)
        first = self._verify("REQ-001", expect=1)
        self.assertFalse(first["criteria"]["REQ-001"]["ok"])
        second = self._verify("REQ-001", expect=1)
        self.assertEqual(self._launches("f"), 2, "a failed record is never reused")
        self.assertEqual(second["launched"], [failing])
        (self.tmp / "src" / "app.py").write_text("VERSION = 2\n")
        self._verify("REQ-001", expect=1)
        self.assertEqual(self._launches("f"), 3)
        timed = self._verify("REQ-002", expect=1)
        self.assertEqual(timed["results"][0]["exit_code"], 124)
        self.assertIs(timed["results"][0]["timed_out"], True)
        self._verify("REQ-002", expect=1)
        self.assertEqual(self._launches("t"), 2, "a timed-out (124) record is never reused")
        big = self._verify("REQ-003")
        self.assertIs(big["results"][0]["truncated"], True)
        self.assertEqual(big["results"][0]["exit_code"], 0)
        self._verify("REQ-003")
        self.assertEqual(self._launches("c"), 2, "a truncated record is never reused even when it passed")
        records = self._records()
        self.assertTrue(all(r["executed"] is True for r in records if r["kind"] == "checks"))
        run_hash = records[-1]["feature_hash"]
        for record in records:
            for command in record["binding"]:
                self.assertIsNone(self.lib.reusable_check_record(records, command, record["binding"][command], run_hash))

    def test_reused_and_legacy_records_are_never_reuse_sources(self):
        command = self._command("a")
        self._configure([command])
        self.init("Cache fixture")
        self._bind_first(command)
        self._verify("REQ-001")
        self._verify("REQ-001")
        records = self._records()
        executed, reused = records[-2], records[-1]
        self.assertIs(reused["executed"], False)
        binding = executed["binding"][command]
        run_hash = executed["feature_hash"]
        self.assertEqual(self.lib.reusable_check_record(records, command, binding, run_hash)["run_id"], executed["run_id"])
        self.assertIsNone(self.lib.reusable_check_record([reused], command, binding, run_hash))
        self.assertIsNone(self.lib.reusable_check_record(records, command, binding, "another-run"))
        self.assertIsNone(self.lib.reusable_check_record(records, command, "other-binding", run_hash))
        # A record from before #43 carries none of the four fields: it must
        # load cleanly, count as evidence, and never be a reuse source.
        legacy = {k: v for k, v in executed.items()
                  if k not in {"binding", "executed", "reused_from", "feature_hash", "hash", "prev_hash"}}
        legacy["run_id"] = "vr-" + "0" * 32
        legacy["prev_hash"] = records[-1]["hash"]
        legacy["hash"] = hashlib.sha256(
            (self.lib._canonical(legacy) + legacy["prev_hash"]).encode("utf-8")).hexdigest()
        ledger = self.tmp / "handsoff-verifications.jsonl"
        ledger.write_text(ledger.read_text() + self.lib._canonical(legacy) + "\n")
        loaded, problems = self.lib.load_verifications(self.tmp, self.lib.load_config(self.tmp))
        self.assertEqual(problems, [])
        self.assertEqual(loaded[-1]["run_id"], legacy["run_id"])
        self.assertIsNone(self.lib.reusable_check_record([loaded[-1]], command, binding, run_hash))
        # And a hand-edited "executed" or a source on an executed record is refused.
        tampered = dict(legacy, run_id="vr-" + "1" * 32, executed="yes", prev_hash=legacy["hash"])
        tampered["hash"] = hashlib.sha256(
            (self.lib._canonical(tampered) + tampered["prev_hash"]).encode("utf-8")).hexdigest()
        ledger.write_text(ledger.read_text() + self.lib._canonical(tampered) + "\n")
        _, problems = self.lib.load_verifications(self.tmp, self.lib.load_config(self.tmp))
        self.assertTrue(any("'executed' must be a boolean" in p for p in problems), problems)

    def test_verify_live_launches_every_time(self):
        command = self._command("a")
        live = self._command("live")
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("deployment_requires_explicit_approval = true",
                                                 "deployment_requires_explicit_approval = false")
                        .replace("live_commands = []", f"live_commands = {json.dumps([live])}", 1))
        self._configure([command])
        self.init("Cache fixture")
        self._bind_first(command)
        first = self._verify("REQ-001")
        symptom = run(["record-symptom-resolved", "--evidence", first["criteria"]["REQ-001"]["run_id"],
                       "--by", "runner"], cwd=self.tmp)
        self.assertEqual(symptom.returncode, 0, symptom.stdout + symptom.stderr)
        reached = self.advance_to(7, implemented_by="impl", reviewed_by="reviewer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        for attempt in (1, 2):
            result = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self._launches("live"), attempt, "the live check launches every time")
            payload = json.loads(result.stdout)
            self.assertNotIn("reused", payload)
        live_records = [r for r in self._records() if r["kind"] == "live"]
        self.assertEqual(len(live_records), 2)
        self.assertTrue(all(r["executed"] is True and r["reused_from"] is None for r in live_records))

    def test_cached_targeted_record_cannot_satisfy_a_regression_group(self):
        command = self._command("a")
        self._configure([command], extra_toml=(
            '\n[[regressions]]\nname = "python-full"\ncommands = ["python3 tests/test_everything.py"]\n'))
        self._git_init()
        self.init("Cache fixture")
        self._bind_first(command)
        self._verify("REQ-001")
        self._verify("REQ-001")
        self.assertEqual(self._launches("a"), 1)
        ledger_before = (self.tmp / "handsoff-verifications.jsonl").read_bytes()
        requested = run(["regression-request", "--group", "python-full", "--by", "codex-supervisor"], cwd=self.tmp)
        self.assertEqual(requested.returncode, 0, requested.stdout + requested.stderr)
        item = self.read_status()["regression_requests"][-1]
        accepted = run(["regression-decide", "--request-id", item["request_id"], "--accept",
                        "--by", "Mission-Control-Pilot"], cwd=self.tmp)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        args = __import__("argparse").Namespace(root=str(self.tmp), request_id=item["request_id"], by="runner")
        launched = [{"command": item["commands"][0], "exit_code": 0, "output_sha256": "0" * 64,
                     "duration_s": 0.01, "timed_out": False, "truncated": False, "output_bytes": 2,
                     "output_tail": "ok"}]
        with mock.patch.object(self.lib, "run_checks", return_value=launched) as runner, \
                __import__("contextlib").redirect_stdout(io.StringIO()):
            self.assertEqual(self.supervisor.cmd_regression_run(args), 0)
        runner.assert_called_once_with(self.lib.load_config(self.tmp), self.tmp.resolve(),
                                       commands=item["commands"], allow_regression=True)
        final = self.read_status()["regression_requests"][-1]
        self.assertEqual(final["state"], "completed")
        self.assertNotIn("output_tail", final["results"][0])
        self.assertEqual((self.tmp / "handsoff-verifications.jsonl").read_bytes(), ledger_before,
                         "regression-run neither reads nor writes the targeted verification cache")
        # The regression command itself can never be a targeted check.
        with self.assertRaisesRegex(self.lib.HandsoffError, "full regression blocked"):
            self.lib.run_checks(self.lib.load_config(self.tmp), self.tmp, commands=["python3 tests/test_everything.py"])

    # -- i43-concurrency-and-fixture ---------------------------------------

    def test_two_concurrent_verifies_of_one_binding_launch_once(self):
        command = self._command("a", delay=1.5)
        self._configure([command])
        self.init("Cache fixture")
        self._bind_first(command)
        outcomes = {}

        def worker(actor):
            outcomes[actor] = run(["verify", "--criterion", "REQ-001", "--by", actor], cwd=self.tmp)

        threads = [threading.Thread(target=worker, args=(actor,)) for actor in ("runner-1", "runner-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for actor, result in outcomes.items():
            self.assertEqual(result.returncode, 0, f"{actor}: {result.stdout}{result.stderr}")
        self.assertEqual(self._launches("a"), 1)
        payloads = {actor: json.loads(result.stdout) for actor, result in outcomes.items()}
        executed = [p for p in payloads.values() if p["criteria"]["REQ-001"]["executed"] is True]
        reused = [p for p in payloads.values() if p["criteria"]["REQ-001"]["executed"] is False]
        self.assertEqual((len(executed), len(reused)), (1, 1))
        original = executed[0]["criteria"]["REQ-001"]["run_id"]
        self.assertEqual(reused[0]["criteria"]["REQ-001"]["reused_from"], original)
        self.assertEqual(reused[0]["reused"], {command: original})
        records = [r for r in self._records() if r["kind"] == "checks"]
        self.assertEqual(len(records), 2)
        self.assertEqual({r["reused_from"] for r in records}, {None, original})
        self.assertTrue((self.tmp / self.lib.VERIFY_INFLIGHT_DIR).is_dir())
        self.assertTrue(self.lib.verify_inflight_lock_path(self.tmp, records[0]["binding"][command]).exists())

    def test_ten_criteria_over_three_commands_verified_twice_launch_three_times(self):
        commands = [self._command(name) for name in ("a", "b", "c")]
        self._configure(commands)
        self.init("Cache fixture")
        self._bind_first(commands[0])
        ids = ["REQ-001"]
        for n in range(2, 11):
            cid = f"REQ-{n:03d}"
            self._add(cid, commands[n % 3])
            ids.append(cid)
        self._add("REQ-011", "walk through the fixture", verification="manual")
        first = self._verify(*ids)
        self.assertEqual(first["launched"], commands)
        self.assertEqual(sum(self._launches(name) for name in ("a", "b", "c")), 3)
        # An unrelated evidence write in between: it appends to the ledger
        # and changes acceptance state but not the bound state.
        attested = run(["record-evidence", "REQ-011", "--kind", "manual", "--by", "tester",
                        "--description", "Walked through the fixture"], cwd=self.tmp)
        self.assertEqual(attested.returncode, 0, attested.stdout + attested.stderr)
        second = self._verify(*ids)
        total = sum(self._launches(name) for name in ("a", "b", "c"))
        self.assertEqual(total, 3, "the second verify must launch nothing")
        naive = 2 * len(commands)
        self.assertLessEqual(total, naive * 0.6, "at least 40 percent fewer launches than one per command per verify")
        self.assertEqual(second["launched"], [])
        self.assertEqual(set(second["reused"]), set(commands))
        self.assertEqual({cid: v["ok"] for cid, v in first["criteria"].items()},
                         {cid: v["ok"] for cid, v in second["criteria"].items()})
        self.assertTrue(all(v["executed"] is False for v in second["criteria"].values()))
        self.assertEqual({c["state"] for c in self.read_acceptance()["criteria"]}, {"passing"})
        event = self._last_event("checks_run")
        self.assertEqual((event["launched_count"], event["reused_count"]), (0, 3))
        # Mission Control's snapshot carries executed/reused_from per entry.
        import handsoff_dashboard as dashboard
        snapshot = dashboard.build_snapshot(self.tmp)
        states = {entry["executed"] for entry in snapshot["verifications"]}
        self.assertIn(False, states)
        self.assertTrue(all("reused_from" in entry for entry in snapshot["verifications"]))

    def test_archive_dir_is_the_temp_dir_and_documents_archive_untouched(self):
        self.assertEqual(self.lib.archive_dir(), self.archive_dir)
        self.init("Cache fixture")
        cfg = self.lib.load_config(self.tmp)
        status, acceptance = self.read_status(), self.read_acceptance()
        written = self.lib.archive_run(self.tmp, cfg, status, acceptance, [], self.lib.read_events(self.tmp, cfg))
        self.assertEqual(written.parent, self.archive_dir)
        self.assertEqual(sum(1 for _ in self.archive_dir.iterdir()), 1)
        self.assertEqual(self._documents_archive_count(), self.documents_count_before)


class TestBenchmarkHarness(HandsoffTestCase):
    """Issue #45: `tools/benchmark_design_phase.py --stub` drives the
    baseline and tranche arms against a fixture revision with a
    deterministic stub adapter and writes run.json per arm plus
    summary.json (medians, percent deltas, seeded-defect retention, the
    commands used, mode "stub"), and no real adapter is ever invoked.
    Criterion i45-harness-stub-run; the report (i45-report-and-method) and
    the live measurement (i45-live-measurement) are manual."""

    HARNESS = ROOT / "tools" / "benchmark_design_phase.py"
    TRAP = "#!/bin/sh\necho REAL_ADAPTER_INVOKED >> \"$HANDSOFF_TEST_TRAP_LOG\"\nexit 1\n"

    def setUp(self):
        super().setUp()
        self.archive_dir = Path(tempfile.mkdtemp(prefix="handsoff-archive-bench-"))
        self._old_env = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.archive_dir)
        self.documents_archive = Path.home() / "Documents" / "Handsoff-Archive"
        self.documents_count_before = self._documents_archive_count()
        # A fixture repository of its own: prompts, one source file for the
        # design evidence to measure, one commit. The harness runs THIS
        # checkout's bin/ against a fresh clone of it per arm.
        self.fixture_repo = Path(tempfile.mkdtemp(prefix="handsoff-bench-fixture-"))
        shutil.copytree(ROOT / "prompts", self.fixture_repo / "prompts")
        (self.fixture_repo / "src").mkdir()
        (self.fixture_repo / "src" / "hello.py").write_text('def hello():\n    return "hi"\n')
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=self.fixture_repo, check=True, capture_output=True)
        # "Real" adapters on PATH are traps: any invocation logs and fails.
        self.trap_dir = Path(tempfile.mkdtemp(prefix="handsoff-bench-trap-"))
        self.trap_log = self.trap_dir / "trap.log"
        for name in ("claude", "codex"):
            path = self.trap_dir / name
            path.write_text(self.TRAP)
            path.chmod(0o755)
        self.out_dir = Path(tempfile.mkdtemp(prefix="handsoff-bench-out-"))
        self.fixture_json = self.out_dir / "fixture.json"
        self.task_file = self.out_dir / "task.md"
        self.task_file.write_text("Design the export command.\n")
        sys.path.insert(0, str(ROOT / "tools"))
        import benchmark_design_phase
        self.harness = benchmark_design_phase

    def tearDown(self):
        self.assertEqual(self._documents_archive_count(), self.documents_count_before,
                         "the real ~/Documents/Handsoff-Archive must never be written by the harness")
        if self._old_env is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self._old_env
        for path in (self.archive_dir, self.fixture_repo, self.trap_dir, self.out_dir):
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    def _documents_archive_count(self):
        if not self.documents_archive.exists():
            return 0
        return sum(1 for _ in self.documents_archive.iterdir())

    def _write_fixture(self, **overrides):
        fixture = {
            "feature": "Benchmark harness fixture",
            "profiles": {
                "architect": {"adapter": "claude", "model": "claude-opus-5"},
                "supervisor": {"adapter": "claude", "model": "claude-opus-5"},
                "implementer": {"adapter": "claude", "model": "claude-opus-5"},
                "reviewer": {"adapter": "codex", "model": "default"},
                "reviewer_followup": {"adapter": "claude", "model": "claude-haiku"},
            },
            "criteria_transaction": {"operations": [
                {"op": "update", "id": "REQ-001", "fields": {
                    "requirement": "The export writes one bundle", "verification": "automated", "tests": ["true"]}},
                {"op": "add", "criterion": {"id": "REQ-002", "type": "supporting",
                                            "requirement": "The bundle carries no raw output",
                                            "verification": "automated", "tests": ["true"]}},
            ]},
            "seeded_defects": [
                {"id": "SD-1", "reveals": "read without the lock", "keywords": ["project lock"]},
                {"id": "SD-2", "reveals": "non-atomic write", "keywords": ["atomic"]},
                {"id": "SD-3", "reveals": "raw output in the bundle", "keywords": ["stdout"]},
            ],
            "design_evidence": [{"id": "src-lines", "command": "wc -l src/hello.py", "inputs": ["src/*.py"]}],
        }
        fixture.update(overrides)
        self.fixture_json.write_text(json.dumps(fixture, indent=2))

    def _run(self, *extra, out="run"):
        out_dir = self.out_dir / out
        env = dict(os.environ)
        git_dir = str(Path(shutil.which("git")).resolve().parent)
        env["PATH"] = os.pathsep.join([str(self.trap_dir), git_dir, "/usr/bin", "/bin"])
        env["HANDSOFF_TEST_TRAP_LOG"] = str(self.trap_log)
        env["HANDSOFF_ARCHIVE_DIR"] = str(self.archive_dir)
        proc = subprocess.run(
            [sys.executable, str(self.HARNESS), "--stub", "--fixture-repo", str(self.fixture_repo),
             "--fixture", str(self.fixture_json), "--task-file", str(self.task_file),
             "--out", str(out_dir), "--stub-sleep", "0.05", *extra],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(self.out_dir),
        )
        return proc, out_dir

    def test_stub_run_writes_both_arms_and_a_summary_without_real_adapters(self):
        self._write_fixture()
        started = time.monotonic()
        proc, out_dir = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLess(time.monotonic() - started, 60.0)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual(json.loads(proc.stdout), summary)
        # Schema.
        self.assertEqual(summary["mode"], "stub")
        self.assertFalse(summary["measurement"])
        for key in ("schema_version", "generated_at", "fixture", "options", "arms", "deltas_percent",
                    "thresholds", "defect_retention", "thresholds_met", "commands"):
            self.assertIn(key, summary)
        self.assertEqual(summary["thresholds"]["wall_clock_reduction_percent"], 30.0)
        self.assertEqual(summary["thresholds"]["tokens_reduction_percent"], 40.0)
        self.assertEqual(set(summary["arms"]), {"baseline", "tranche"})
        for arm in ("baseline", "tranche"):
            record = summary["arms"][arm]
            self.assertEqual(record["outcomes"], ["approved"])
            self.assertEqual(record["attempts"], [2])
            self.assertEqual(record["authorized_attempts"], [None])
            self.assertEqual(record["sessions"], 4)
            self.assertIsInstance(record["median_wall_clock_seconds"], float)
            self.assertIsInstance(record["median_design_phase_tokens"], float)
        self.assertIsInstance(summary["deltas_percent"]["wall_clock"], float)
        self.assertIsInstance(summary["deltas_percent"]["tokens"], float)
        self.assertIn(summary["thresholds_met"], (True, False))
        self.assertTrue(any("benchmark_design_phase.py --stub" in c for c in summary["commands"]["reproduce"]))
        # Deterministic defect retention: the stub reviewer names every
        # seeded defect in both arms, so retention holds with nothing missing.
        retention = summary["defect_retention"]
        self.assertEqual(retention["baseline_found"], ["SD-1", "SD-2", "SD-3"])
        self.assertEqual(retention["tranche_found"], ["SD-1", "SD-2", "SD-3"])
        self.assertEqual(retention["missing_in_tranche"], [])
        self.assertTrue(retention["retained"])
        # Per-arm run.json: settings, sessions with the recorded fields, and
        # the tranche's packet, follow-up tier, and design-evidence cache hit.
        baseline = json.loads((out_dir / "baseline" / "run.json").read_text())
        tranche = json.loads((out_dir / "tranche" / "run.json").read_text())
        self.assertEqual(baseline["settings"], {"reviewer_followup": False, "design_evidence": False, "packets": False})
        self.assertEqual(tranche["settings"], {"reviewer_followup": True, "design_evidence": True, "packets": True})
        for arm_record in (baseline, tranche):
            self.assertEqual(arm_record["mode"], "stub")
            repeat = arm_record["repeats"][0]
            self.assertEqual(repeat["outcome"], "approved")
            self.assertTrue(repeat["tokens_complete"])
            self.assertIsInstance(repeat["design_phase_tokens"], int)
            self.assertEqual([s["role"] for s in repeat["sessions"]],
                             ["architect", "reviewer", "architect", "reviewer"])
            for session in repeat["sessions"]:
                for key in ("role", "adapter", "requested_model", "reported_model", "attempt", "input_tokens",
                            "output_tokens", "started_at", "ended_at", "seconds", "design_evidence", "command",
                            "session_id", "state", "tier", "packet_id", "usage_source"):
                    self.assertIn(key, session)
                self.assertEqual(session["state"], "completed")
                self.assertEqual(session["usage_source"], "runner")
                self.assertIsInstance(session["input_tokens"], int)
                self.assertIsInstance(session["output_tokens"], int)
                self.assertIn(session["adapter"], ("claude", "codex"))
                # The adapter came from the harness's stub directory, never
                # from PATH, where the traps sit first.
                self.assertTrue(session["command"][0].startswith(arm_record["adapter_directory"]), session["command"])
                self.assertFalse(session["command"][0].startswith(str(self.trap_dir)))
                if session["adapter"] == "claude":
                    self.assertEqual(session["command"][-2:], ["--output-format", "json"])
                else:
                    self.assertEqual(session["command"][-2:], ["--json", "-"])
            names = [c["command"][3] for c in repeat["commands"]]
            self.assertEqual(names[:3], ["init", "criteria-apply", "advance"])
            self.assertEqual(names.count("record-design-review"), 2)
            self.assertEqual(names.count("design-review-authorize"), 0)
            self.assertEqual([r["decision"] for r in repeat["reviews"]], ["changes_requested", "approved"])
        baseline_names = [c["command"][3] for c in baseline["repeats"][0]["commands"]]
        tranche_names = [c["command"][3] for c in tranche["repeats"][0]["commands"]]
        self.assertNotIn("design-review-packet", baseline_names)
        self.assertNotIn("design-evidence", baseline_names)
        self.assertEqual(tranche_names.count("design-review-packet"), 1)
        self.assertEqual(tranche_names.count("design-evidence"), 2)
        tranche_sessions = tranche["repeats"][0]["sessions"]
        self.assertEqual([s["tier"] for s in tranche_sessions], [None, "primary", None, "followup"])
        self.assertIsNone(tranche_sessions[1]["packet_id"])
        self.assertRegex(tranche_sessions[3]["packet_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(tranche_sessions[3]["adapter"], "claude")
        self.assertEqual(tranche_sessions[3]["requested_model"], "claude-haiku")
        self.assertEqual(tranche_sessions[0]["design_evidence_run"], {"attempt": 1, "executed": 1, "reused": 0})
        self.assertEqual(tranche_sessions[2]["design_evidence_run"], {"attempt": 2, "executed": 0, "reused": 1})
        self.assertEqual(tranche_sessions[2]["design_evidence"]["states"], {"src-lines": "current"})
        self.assertIsNone(baseline["repeats"][0]["sessions"][0]["design_evidence"])
        # No real adapter ran, nothing landed in the archive, and this
        # checkout's own state files were not touched.
        self.assertFalse(self.trap_log.exists(), "a PATH adapter was invoked")
        self.assertEqual(sum(1 for _ in self.archive_dir.iterdir()), 0)
        self.assertFalse((out_dir / "archive").exists())
        self.assertFalse((self.fixture_repo / "handsoff-status.json").exists())

    def test_budget_exhaustion_authorizes_exactly_one_extra_attempt_and_records_it(self):
        self._write_fixture(max_autonomous_design_reviews=1)
        proc, out_dir = self._run("--arms", "baseline", out="budget")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = json.loads((out_dir / "baseline" / "run.json").read_text())["repeats"][0]
        self.assertEqual(run["outcome"], "approved")
        self.assertEqual(run["authorized_attempt"], 2)
        names = [c["command"][3] for c in run["commands"]]
        self.assertEqual(names.count("design-review-authorize"), 1)
        self.assertLess(names.index("design-review-authorize"), len(names) - 1)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual(summary["arms"]["baseline"]["authorized_attempts"], [2])
        self.assertNotIn("tranche", summary["arms"])
        self.assertIsNone(summary["thresholds_met"])
        self.assertIsNone(summary["defect_retention"]["retained"])
        self.assertFalse((out_dir / "tranche").exists())
        self.assertFalse(self.trap_log.exists())

    def test_runner_usage_parser_reads_both_shapes_and_never_estimates(self):
        parse = self.harness.parse_runner_output
        claude = parse(json.dumps({"type": "result", "result": "DESIGN_APPROVED",
                                   "usage": {"input_tokens": 12, "output_tokens": 3},
                                   "modelUsage": {"claude-opus-5": {}}}))
        self.assertEqual((claude["input_tokens"], claude["output_tokens"]), (12, 3))
        self.assertEqual(claude["reported_model"], "claude-opus-5")
        self.assertEqual(claude["text"], "DESIGN_APPROVED")
        self.assertEqual(claude["usage_source"], "runner")
        codex = parse("\n".join([
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINDING: x\nDESIGN_CHANGES_REQUESTED"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 0, "output_tokens": 9}}),
        ]))
        self.assertEqual((codex["input_tokens"], codex["output_tokens"]), (40, 9))
        self.assertIsNone(codex["reported_model"])
        self.assertEqual(self.harness.review_decision(codex["text"]), "changes_requested")
        self.assertEqual(self.harness.extract_findings(codex["text"]), ["x"])
        plain = parse("Some prose with no structured output.\nDESIGN_APPROVED\n")
        self.assertIsNone(plain["input_tokens"])
        self.assertIsNone(plain["output_tokens"])
        self.assertEqual(plain["usage_source"], "none")
        self.assertEqual(self.harness.review_decision(plain["text"]), "approved")
        self.assertIsNone(self.harness.review_decision("no verdict here"))
        pretty = parse(json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}, indent=2))
        self.assertEqual(pretty["input_tokens"], 1)
        # Retention rule: an id or a fixture keyword counts, nothing else.
        defects = [{"id": "SD-1", "keywords": ["project lock"]}, {"id": "SD-2", "keywords": ["atomic"]}]
        self.assertEqual(self.harness.defects_found("reads without the Project Lock", defects), ["SD-1"])
        self.assertEqual(self.harness.defects_found("sd-2 and an atomic replace", defects), ["SD-2"])
        self.assertEqual(self.harness.defects_found("nothing relevant", defects), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRoleQuestions(HandsoffTestCase):
    """#46: a role's `HANDSOFF_QUESTION:` line reaches Mission Control
    without Supervisor relay, blocks only from the current live session,
    and the Pilot's answer is delivered on the next launch."""

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 46 role questions")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(4)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @staticmethod
    def _sid(number):
        return f"hs-{number:032x}"

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def _spec(self, role, code):
        return self.runtime.LaunchSpec(
            role, "claude", "default", (sys.executable, "-c", code), str(self.tmp),
            "private task text", "configured",
        )

    def _launch_asking(self, role, questions, number, sleep=0.6):
        code = ("import time\n"
                + "".join(f"print({q!r}, flush=True)\n" for q in questions)
                + f"time.sleep({sleep})\n")
        result = {}

        def launch():
            try:
                with mock.patch("sys.stdout", new=io.StringIO()):
                    result["code"] = self.runtime.execute_launch(
                        self._spec(role, code), session_id_factory=lambda: self._sid(number),
                        popen_factory=subprocess.Popen, beacon_interval=0.1,
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                result["error"] = exc

        worker = threading.Thread(target=launch)
        worker.start()
        return worker, result

    def _api(self, server, method, path, body=None):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload)

    def test_question_line_from_the_live_session_blocks_the_run_while_it_still_runs(self):
        status_before = self.read_status()
        worker, result = self._launch_asking(
            "architect", ["HANDSOFF_QUESTION: Concise path or full path for this feature?",
                          "ordinary prose that is not a question line"], 31, sleep=1.5)
        seen = None
        deadline = time.monotonic() + 6
        while worker.is_alive() and time.monotonic() < deadline:
            status = self.read_status()
            if status.get("pending_questions"):
                seen = status
                break
            time.sleep(0.05)
        self.assertIsNotNone(seen, "question was not recorded while the child was running")
        self.assertEqual(seen["status"], "blocked")
        self.assertEqual(seen["next_action"], "Architect asks: Concise path or full path for this feature?")
        question = seen["pending_questions"][0]
        self.assertEqual(set(question), self.lib.QUESTION_FIELDS)
        self.assertEqual((question["role"], question["session_id"], question["blocking"]),
                         ("architect", self._sid(31), True))
        self.assertEqual(question["previous_next_action"], status_before["next_action"])
        worker.join(timeout=10)
        self.assertEqual(result.get("code"), 0, result.get("error"))
        after = self.read_status()
        # Liveness metadata only: phase, progress and coverage are untouched.
        for key in ("phase_number", "progress", "requirement_coverage"):
            self.assertEqual(after[key], status_before[key])
        self.assertEqual(after["status"], "blocked")
        raised = [e for e in self._events() if e["kind"] == "question_raised"]
        self.assertEqual(len(raised), 1)
        self.assertEqual(set(raised[0]) & {"text", "role", "session_id", "blocking", "question_id"},
                         {"text", "role", "session_id", "blocking", "question_id"})
        self.assertNotIn("private task text", json.dumps(raised[0]))
        # The board and the CLI both surface it as a named decision.
        r = run(["status"], cwd=self.tmp)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["questions"]["blocking"][0]["question_id"], question["question_id"])
        server = self.dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            code, snapshot = self._api(server, "GET", "/api/dashboard")
            self.assertEqual(code, 200)
            self.assertTrue(snapshot["input_required"]["required"])
            self.assertEqual(snapshot["input_required"]["kind"], "question")
            self.assertEqual(snapshot["input_required"]["question_id"], question["question_id"])
            self.assertIn("Concise path or full path", snapshot["input_required"]["message"])
            self.assertEqual(snapshot["questions"]["blocking"][0]["question_id"], question["question_id"])
            # Answering from the board takes the same audited path and lifts the block.
            code, answered = self._api(server, "POST", "/api/question-answer",
                                       body=json.dumps({"question_id": question["question_id"],
                                                        "text": "Concise path."}))
            self.assertEqual(code, 200, answered)
            code, snapshot = self._api(server, "GET", "/api/dashboard")
            self.assertFalse(snapshot["input_required"]["required"])
        finally:
            server.shutdown()
            server.server_close()
        final = self.read_status()
        self.assertEqual(final["status"], "in_progress")
        self.assertEqual(final["next_action"], status_before["next_action"])
        record = final["pending_questions"][0]
        self.assertEqual((record["answer"], record["answered_by"]), ("Concise path.", "Mission Control Pilot"))
        self.assertEqual([e["kind"] for e in self._events() if e["kind"].startswith("question_")],
                         ["question_raised", "question_answered"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_answer_is_delivered_to_the_role_exactly_once_on_its_next_launch(self):
        record = self.lib.raise_question(self.tmp, role="architect", text="Which region?", by="test-supervisor")
        run(["question-answer", "--id", record["question_id"], "--by", "moncy", "--text", "us-east-1"], cwd=self.tmp)
        first = self.runtime.build_role_input(self.tmp, "architect", "next task")
        self.assertIn("# Pilot answers to your earlier questions", first)
        self.assertIn("Which region?", first)
        self.assertIn("A (moncy): us-east-1", first)
        second = self.runtime.build_role_input(self.tmp, "architect", "next task")
        self.assertNotIn("# Pilot answers to your earlier questions", second)
        other = self.runtime.build_role_input(self.tmp, "reviewer", "review task")
        self.assertNotIn("# Pilot answers to your earlier questions", other)
        self.assertIsNotNone(self.read_status()["pending_questions"][0]["delivered_at"])
        self.assertIn("question_answers_delivered", [e["kind"] for e in self._events()])

    def test_question_from_a_non_current_or_finished_session_is_recorded_without_blocking(self):
        worker, result = self._launch_asking("reviewer", ["HANDSOFF_QUESTION: first"], 41, sleep=0.3)
        worker.join(timeout=10)
        self.assertEqual(result.get("code"), 0, result.get("error"))
        answer = self.read_status()["pending_questions"][0]
        run(["question-answer", "--id", answer["question_id"], "--by", "moncy", "--text", "ok"], cwd=self.tmp)
        self.assertEqual(self.read_status()["status"], "in_progress")
        # The session is now completed: a late question from it cannot hold the run.
        late = self.lib.raise_question(self.tmp, role="reviewer", session_id=self._sid(41), text="late question")
        self.assertFalse(late["blocking"])
        unknown = self.lib.raise_question(self.tmp, role="implementer", session_id=self._sid(99), text="who am I")
        self.assertFalse(unknown["blocking"])
        status = self.read_status()
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(len(status["pending_questions"]), 3)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_multiple_open_questions_hold_until_the_last_blocking_one_is_answered(self):
        first = self.lib.raise_question(self.tmp, role="architect", text="Q1", by="test-supervisor")
        second = self.lib.raise_question(self.tmp, role="architect", text="Q2", by="test-supervisor")
        self.assertTrue(first["blocking"] and second["blocking"])
        self.assertEqual(self.read_status()["status"], "blocked")
        self.lib.answer_question(self.tmp, question_id=first["question_id"], by="moncy", text="A1")
        self.assertEqual(self.read_status()["status"], "blocked")
        self.lib.answer_question(self.tmp, question_id=second["question_id"], by="moncy", text="A2")
        self.assertEqual(self.read_status()["status"], "in_progress")
        with self.assertRaisesRegex(self.lib.HandsoffError, "already answered"):
            self.lib.answer_question(self.tmp, question_id=second["question_id"], by="moncy", text="again")

    def test_text_is_bounded_and_hand_edits_refuse_cleanly(self):
        long_text = "x" * (self.lib.MAX_QUESTION_TEXT + 50)
        record = self.lib.raise_question(self.tmp, role="implementer", text=long_text, by="test-supervisor")
        self.assertEqual(len(record["text"]), self.lib.MAX_QUESTION_TEXT)
        self.assertTrue(record["truncated"])
        with self.assertRaisesRegex(self.lib.HandsoffError, "must not be empty"):
            self.lib.raise_question(self.tmp, role="implementer", text="   ", by="test-supervisor")
        status = self.read_status()
        status["pending_questions"][0]["blocking"] = "yes"
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("blocking must be boolean" in e for e in errors), errors)
        status["pending_questions"][0] = {"question_id": "qn-bad"}
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("exactly the keys" in e for e in errors), errors)

    def test_broker_routes_question_raise_and_refuses_question_answer(self):
        import handsoff_broker
        argv = handsoff_broker._workflow_argv(self.tmp, {
            "actor": "supervisor", "project_root": str(self.tmp), "action": "workflow",
            "command": "question-raise", "by": "supervisor-1", "role": "architect", "text": "why",
        })
        self.assertEqual(argv[-6:], ["--role", "architect", "--text", "why", "--by", "supervisor-1"])
        with self.assertRaisesRegex(self.lib.HandsoffError, "human-only"):
            handsoff_broker._workflow_argv(self.tmp, {
                "actor": "supervisor", "project_root": str(self.tmp), "action": "workflow",
                "command": "question-answer", "id": "qn-x", "by": "supervisor-1", "text": "no",
            })

    def test_prompts_document_the_question_line(self):
        for role in ("architect", "implementer", "reviewer", "supervisor"):
            self.assertIn("HANDSOFF_QUESTION:", (ROOT / "prompts" / f"{role}.md").read_text())


class TestStructuredQuestions(HandsoffTestCase):
    """#48: a `HANDSOFF_QUESTION:` line may carry a JSON form (text, options,
    recommended); malformed forms are kept as plain text with form_error;
    the Pilot answers a role's questions as one batch; the answers reach
    the role in one ordered section on its next launch."""

    FORM = '{"text": "Which path?", "options": ["Concise", "Full"], "recommended": "Concise"}'

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        self.init("Issue 48 structured questions")
        sys.path.insert(0, str(BIN))
        import handsoff_agent
        import handsoff_dashboard
        import handsoff_lib
        self.runtime = handsoff_agent
        self.dashboard = handsoff_dashboard
        self.lib = handsoff_lib
        self.set_criterion_state("failing", resolved=False)
        r = self.advance_to(4)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def _question_events(self):
        return [e for e in self._events() if e["kind"].startswith("question_")]

    def _ask(self, text, role="architect", by="test-supervisor"):
        return self.lib.raise_question(self.tmp, role=role, text=text, by=by)

    def _batch_file(self, answers):
        path = self.tmp / "answers.json"
        path.write_text(json.dumps({"answers": answers}))
        return str(path)

    def _snapshot_files(self):
        return {name: (self.tmp / name).read_bytes()
                for name in ("handsoff-status.json", "handsoff-events.jsonl")}

    def _api(self, server, method, path, body=None):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload)

    # -- i48-structured-parse ------------------------------------------------

    def test_form_after_prefix_is_parsed_with_options_and_recommended(self):
        record = self._ask("   " + self.FORM + "  ")
        self.assertEqual(set(record), self.lib.QUESTION_FIELDS)
        self.assertEqual(record["text"], "Which path?")
        self.assertEqual(record["options"], ["Concise", "Full"])
        self.assertEqual(record["recommended"], "Concise")
        self.assertIsNone(record["form_error"])
        self.assertFalse(record["truncated"])
        self.assertIsNone(record["chosen_option"])
        self.assertIsNone(record["other_text"])
        self.assertEqual(self.read_status()["next_action"], "Architect asks: Which path?")
        raised = self._question_events()[-1]
        self.assertEqual(raised["kind"], "question_raised")
        self.assertEqual((raised["options"], raised["recommended"], raised["form_error"]),
                         (["Concise", "Full"], "Concise", None))
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_form_text_and_options_are_whitespace_normalized(self):
        record = self._ask('{"text": "  Which    path? ", "options": [" A  b ", "C"], "recommended": "A   b"}')
        self.assertEqual(record["text"], "Which path?")
        self.assertEqual(record["options"], ["A b", "C"])
        self.assertEqual(record["recommended"], "A b")
        self.assertIsNone(record["form_error"])

    def test_plain_text_without_a_leading_brace_is_unchanged_from_46(self):
        for text in ("Which path?", '["Concise", "Full"]', '"a scalar"', "42", 'text with {"braces": 1} inside'):
            record = self._ask(text)
            self.assertEqual(record["text"], " ".join(text.split()))
            self.assertEqual(record["options"], [])
            self.assertIsNone(record["recommended"])
            self.assertIsNone(record["form_error"], text)
        with self.assertRaisesRegex(self.lib.HandsoffError, "must not be empty"):
            self._ask("   ")

    def test_every_form_error_code_keeps_the_candidate_as_plain_text(self):
        parse = self.lib.parse_question_candidate
        cases = {
            "malformed_json": '{"text": "Which path?", "options": ["A", "B"',
            "unknown_keys": '{"text": "Q", "options": ["A", "B"], "extra": 1}',
            "missing_keys": '{"text": "Q"}',
            "text_bounds": '{"text": "   ", "options": ["A", "B"]}',
            "options_bounds": '{"text": "Q", "options": []}',
            "duplicate_options": '{"text": "Q", "options": ["A", " A "]}',
            "recommended_not_offered": '{"text": "Q", "options": ["A", "B"], "recommended": "C"}',
        }
        for code, candidate in cases.items():
            record = self._ask("  " + candidate)
            self.assertEqual(record["form_error"], code, candidate)
            self.assertEqual(record["text"], " ".join(candidate.split()), code)
            self.assertEqual(record["options"], [])
            self.assertIsNone(record["recommended"])
            self.assertFalse(record["truncated"])
            self.assertEqual(self._question_events()[-1]["form_error"], code)
        # More limit violations, each named by its rule.
        self.assertEqual(parse('{"text": "' + "x" * 1025 + '", "options": ["A"]}')["form_error"], "text_bounds")
        self.assertEqual(parse('{"text": 5, "options": ["A"]}')["form_error"], "text_bounds")
        self.assertEqual(parse('{"text": "Q", "options": "A"}')["form_error"], "options_bounds")
        self.assertEqual(parse('{"text": "Q", "options": ["A", 1]}')["form_error"], "options_bounds")
        self.assertEqual(parse('{"text": "Q", "options": ["A", "  "]}')["form_error"], "options_bounds")
        self.assertEqual(parse('{"text": "Q", "options": ["' + "y" * 121 + '"]}')["form_error"], "options_bounds")
        seven = json.dumps({"text": "Q", "options": [f"o{i}" for i in range(7)]})
        self.assertEqual(parse(seven)["form_error"], "options_bounds")
        six = json.dumps({"text": "Q", "options": [f"o{i}" for i in range(6)]})
        self.assertIsNone(parse(six)["form_error"])
        self.assertEqual(parse('{"text": "Q", "options": ["A"], "recommended": null}')["form_error"],
                         "recommended_not_offered")
        self.assertEqual(parse('{"options": ["A"]}')["form_error"], "missing_keys")
        self.assertEqual(parse("{}")["form_error"], "missing_keys")
        # not_object is the parser's guard for a candidate that decodes to
        # something other than an object; detection keeps arrays and
        # scalars away from it, so it is exercised on the parser directly.
        with mock.patch.object(self.lib.json, "loads", return_value=["not", "an", "object"]):
            self.assertEqual(parse('{"anything": 1}')["form_error"], "not_object")
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_over_long_malformed_candidate_is_bounded_and_flagged_truncated(self):
        candidate = '{"text": "' + "x" * 1100
        record = self._ask(candidate)
        self.assertEqual(record["form_error"], "malformed_json")
        self.assertEqual(len(record["text"]), self.lib.MAX_QUESTION_TEXT)
        self.assertTrue(record["truncated"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_schema_refuses_hand_edited_form_fields(self):
        self._ask(self.FORM)
        clean = self.read_status()
        self.assertEqual(self.lib.validate_status_schema(clean), [])
        edits = {
            "options": (["A"] * 2, "must be unique"),
            "options too many": ([f"o{i}" for i in range(7)], "at most 6"),
            "options unnormalized": ([" A"], "normalized"),
            "options too long": (["z" * 121], "1 to 120"),
            "recommended": ("Nope", "one of the options"),
            "form_error": ("weird", "form_error must be null or one of"),
            "chosen_option": ("Full", "chosen_option must be null or the offered option"),
            "other_text": ("free", "other_text must be null or equal to the answer"),
        }
        for label, (value, fragment) in edits.items():
            status = json.loads(json.dumps(clean))
            key = label.split(" ")[0]
            status["pending_questions"][0][key] = value
            errors = self.lib.validate_status_schema(status)
            self.assertTrue(any(fragment in e for e in errors), (label, errors))
        status = json.loads(json.dumps(clean))
        status["pending_questions"][0].update({"form_error": "malformed_json"})
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("form_error must carry no options" in e for e in errors), errors)
        status = json.loads(json.dumps(clean))
        del status["pending_questions"][0]["options"]
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("exactly the keys" in e for e in errors), errors)
        # An answered form question whose chosen_option and other_text
        # disagree with the answer is a hand edit too.
        self.lib.answer_questions_batch(
            self.tmp, by="moncy", answers=[{"question_id": clean["pending_questions"][0]["question_id"], "choice": "Full"}])
        answered = self.read_status()
        status = json.loads(json.dumps(answered))
        status["pending_questions"][0]["other_text"] = "Full"
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("cannot carry both" in e for e in errors), errors)
        status = json.loads(json.dumps(answered))
        status["pending_questions"][0]["answer"] = "Concise"
        errors = self.lib.validate_status_schema(status)
        self.assertTrue(any("chosen_option" in e for e in errors), errors)

    def test_legacy_46_records_without_the_new_fields_still_validate_and_answer(self):
        record = self._ask("Old style question")
        status = self.read_status()
        legacy = {k: v for k, v in status["pending_questions"][0].items() if k in self.lib.QUESTION_LEGACY_FIELDS}
        self.assertEqual(set(legacy), self.lib.QUESTION_LEGACY_FIELDS)
        status["pending_questions"][0] = legacy
        self.assertEqual(self.lib.validate_status_schema(status), [])
        self.assertEqual(self.lib.questions_view(status)["by_role"][0]["count"], 1)
        # Written in place (the next commit re-anchors the event chain).
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2))
        # A legacy record cannot take a choice (no options) but takes other.
        with self.assertRaisesRegex(self.lib.QuestionAnswerConflict, "plain text and accepts only other"):
            self.lib.answer_questions_batch(self.tmp, by="moncy",
                                            answers=[{"question_id": record["question_id"], "choice": "x"}])
        answered = self.lib.answer_questions_batch(
            self.tmp, by="moncy", answers=[{"question_id": record["question_id"], "other": "fine"}])[0]
        self.assertEqual(set(answered), self.lib.QUESTION_FIELDS)
        self.assertEqual((answered["answer"], answered["chosen_option"], answered["other_text"]),
                         ("fine", None, "fine"))
        self.assertEqual(self.read_status()["status"], "in_progress")
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_open_question_limit_and_id_uniqueness_hold_for_forms(self):
        ids = set()
        for i in range(self.lib.MAX_PENDING_QUESTIONS):
            record = self._ask(json.dumps({"text": f"Q{i}", "options": ["A", "B"], "recommended": "A"}))
            ids.add(record["question_id"])
        self.assertEqual(len(ids), self.lib.MAX_PENDING_QUESTIONS)
        with self.assertRaisesRegex(self.lib.HandsoffError, "at most 16 questions may be open"):
            self._ask(self.FORM)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    # -- i48-batch-answer ----------------------------------------------------

    def test_batch_records_every_answer_in_one_commit_and_lifts_the_block_once(self):
        first = self._ask(self.FORM)
        second = self._ask('{"text": "Region?", "options": ["us-east-1", "eu-west-1"]}')
        third = self._ask("Anything else?")
        self.assertEqual(self.read_status()["status"], "blocked")
        events_before = len(self._events())
        records = self.lib.answer_questions_batch(self.tmp, by="moncy", answers=[
            {"question_id": first["question_id"], "choice": "Full"},
            {"question_id": second["question_id"], "other": "  ap-south-1  please "},
            {"question_id": third["question_id"], "other": "no"},
        ])
        self.assertEqual([r["question_id"] for r in records],
                         [first["question_id"], second["question_id"], third["question_id"]])
        self.assertEqual((records[0]["answer"], records[0]["chosen_option"], records[0]["other_text"]),
                         ("Full", "Full", None))
        self.assertEqual((records[1]["answer"], records[1]["chosen_option"], records[1]["other_text"]),
                         ("ap-south-1 please", None, "ap-south-1 please"))
        self.assertEqual((records[2]["answer"], records[2]["chosen_option"], records[2]["other_text"]),
                         ("no", None, "no"))
        self.assertTrue(all(r["answered_by"] == "moncy" and r["answered_at"] for r in records))
        events = self._events()
        self.assertEqual(len(events), events_before + 1)
        recorded = events[-1]
        self.assertEqual(recorded["kind"], "question_answers_recorded")
        self.assertEqual(recorded["answers"], [
            {"question_id": first["question_id"], "choice": "Full", "answered_by": "moncy"},
            {"question_id": second["question_id"], "other": "ap-south-1 please", "answered_by": "moncy"},
            {"question_id": third["question_id"], "other": "no", "answered_by": "moncy"},
        ])
        self.assertTrue(recorded["block_lifted"])
        self.assertEqual(recorded["open_blocking_questions"], 0)
        status = self.read_status()
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(status["next_action"], first["previous_next_action"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_block_lifts_only_when_no_blocking_question_remains(self):
        first = self._ask(self.FORM)
        second = self._ask(self.FORM)
        third = self._ask(self.FORM)
        self.lib.answer_questions_batch(self.tmp, by="moncy", answers=[
            {"question_id": first["question_id"], "choice": "Concise"},
            {"question_id": second["question_id"], "choice": "Concise"},
        ])
        self.assertEqual(self.read_status()["status"], "blocked")
        self.assertFalse(self._question_events()[-1]["block_lifted"])
        self.lib.answer_questions_batch(self.tmp, by="moncy", answers=[
            {"question_id": third["question_id"], "other": "neither"}])
        self.assertEqual(self.read_status()["status"], "in_progress")
        lifted = [e for e in self._question_events() if e.get("block_lifted")]
        self.assertEqual(len(lifted), 1)

    def test_each_batch_refusal_names_the_index_and_writes_nothing(self):
        form = self._ask(self.FORM)
        plain = self._ask("Plain?")
        answered = self._ask("Done?")
        self.lib.answer_question(self.tmp, question_id=answered["question_id"], by="moncy", text="yes")
        before = self._snapshot_files()
        unknown = "qn-" + "0" * 32
        cases = [
            ([], "1 to 16 entries", self.lib.HandsoffError),
            ("nope", "1 to 16 entries", self.lib.HandsoffError),
            ([{"question_id": form["question_id"], "choice": "Full"}] * 17, "1 to 16 entries", self.lib.HandsoffError),
            (["x"], r"answers\[0\] must be an object", self.lib.HandsoffError),
            ([{"question_id": form["question_id"]}], r"answers\[0\] must have exactly the keys", self.lib.HandsoffError),
            ([{"question_id": form["question_id"], "choice": "Full", "other": "x"}],
             r"answers\[0\] must have exactly the keys", self.lib.HandsoffError),
            ([{"question_id": form["question_id"], "choice": "Full"}, {"question_id": "bad", "other": "x"}],
             r"answers\[1\]\.question_id is invalid", self.lib.HandsoffError),
            ([{"question_id": form["question_id"], "choice": "Full"},
              {"question_id": form["question_id"], "other": "x"}],
             r"answers\[1\]\.question_id .* listed twice", self.lib.HandsoffError),
            ([{"question_id": plain["question_id"], "other": "   "}], r"answers\[0\]\.other must not be empty",
             self.lib.HandsoffError),
            ([{"question_id": plain["question_id"], "other": "x" * 1025}], r"answers\[0\]\.other must be at most 1024",
             self.lib.HandsoffError),
            ([{"question_id": plain["question_id"], "other": 5}], r"answers\[0\]\.other must be a string",
             self.lib.HandsoffError),
            ([{"question_id": form["question_id"], "choice": "Full"}, {"question_id": unknown, "other": "x"}],
             r"answers\[1\]: question qn-0+ was not found", self.lib.QuestionAnswerConflict),
            ([{"question_id": plain["question_id"], "other": "x"},
              {"question_id": answered["question_id"], "other": "again"}],
             r"answers\[1\]: question .* is already answered", self.lib.QuestionAnswerConflict),
            ([{"question_id": form["question_id"], "choice": "Medium"}],
             r"answers\[0\]: choice 'Medium' is not offered", self.lib.QuestionAnswerConflict),
            ([{"question_id": plain["question_id"], "choice": "Full"}],
             r"answers\[0\]: question .* plain text and accepts only other", self.lib.QuestionAnswerConflict),
        ]
        for answers, pattern, exc_type in cases:
            with self.assertRaisesRegex(exc_type, pattern):
                self.lib.answer_questions_batch(self.tmp, by="moncy", answers=answers)
            self.assertEqual(self._snapshot_files(), before, pattern)
        with self.assertRaisesRegex(self.lib.HandsoffError, 'exactly the key "answers"'):
            self.lib.question_answers_from_payload({"answers": [], "extra": 1})
        with self.assertRaisesRegex(self.lib.HandsoffError, 'exactly the key "answers"'):
            self.lib.question_answers_from_payload([])
        self.assertEqual(self._snapshot_files(), before)
        self.assertEqual(self.read_status()["status"], "blocked")

    def test_cli_batch_answers_and_the_single_command_still_works(self):
        form = self._ask(self.FORM)
        plain = self._ask("Plain?")
        r = run(["question-answer", "--batch", self._batch_file([
            {"question_id": form["question_id"], "choice": "Concise"}]), "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"QUESTIONS_ANSWERED: {form['question_id']}")
        self.assertEqual(self.read_status()["status"], "blocked")
        # Refusals: bad entry, bad envelope, unreadable file, mixed flags.
        before = self._snapshot_files()
        r = run(["question-answer", "--batch", self._batch_file([
            {"question_id": plain["question_id"], "choice": "Concise"}]), "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED: answers[0]: question", r.stdout)
        self.assertIn("accepts only other", r.stdout)
        envelope = self.tmp / "envelope.json"
        envelope.write_text(json.dumps([{"question_id": plain["question_id"], "other": "x"}]))
        r = run(["question-answer", "--batch", str(envelope), "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn('SHIP_FEATURE_BLOCKED: question batch must be an object with exactly the key "answers"', r.stdout)
        envelope.write_text("{not json")
        r = run(["question-answer", "--batch", str(envelope), "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED: batch file could not be read as JSON", r.stdout)
        r = run(["question-answer", "--batch", str(self.tmp / "missing.json"), "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED: batch file could not be read as JSON", r.stdout)
        r = run(["question-answer", "--batch", str(envelope), "--id", plain["question_id"], "--text", "x",
                 "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED: --batch cannot be combined with --id or --text", r.stdout)
        r = run(["question-answer", "--id", plain["question_id"], "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED: question-answer needs --id and --text, or --batch FILE", r.stdout)
        self.assertEqual(self._snapshot_files(), before)
        # The #46 single form keeps working and now fills the answer pair.
        r = run(["question-answer", "--id", plain["question_id"], "--text", "free text", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"QUESTION_ANSWERED: {plain['question_id']}")
        status = self.read_status()
        self.assertEqual(status["status"], "in_progress")
        records = {q["question_id"]: q for q in status["pending_questions"]}
        self.assertEqual((records[plain["question_id"]]["chosen_option"], records[plain["question_id"]]["other_text"]),
                         (None, "free text"))
        self.assertEqual((records[form["question_id"]]["chosen_option"], records[form["question_id"]]["other_text"]),
                         ("Concise", None))
        answered_events = [e for e in self._question_events() if e["kind"] == "question_answered"]
        self.assertEqual(len(answered_events), 1)
        self.assertEqual((answered_events[0]["answer"], answered_events[0]["chosen_option"],
                          answered_events[0]["other_text"]), ("free text", None, "free text"))
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_single_answer_naming_an_offered_option_records_it_as_the_choice(self):
        form = self._ask(self.FORM)
        record = self.lib.answer_question(self.tmp, question_id=form["question_id"], by="moncy", text="Full")
        self.assertEqual((record["answer"], record["chosen_option"], record["other_text"]), ("Full", "Full", None))
        event = self._question_events()[-1]
        self.assertEqual(event["kind"], "question_answered")
        self.assertEqual((event["answer"], event["chosen_option"], event["other_text"]), ("Full", "Full", None))

    def test_api_batch_answers_with_400_for_shape_and_409_for_state(self):
        form = self._ask(self.FORM)
        plain = self._ask("Plain?")
        server = self.dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            code, snapshot = self._api(server, "GET", "/api/dashboard")
            self.assertEqual(code, 200)
            self.assertEqual(snapshot["input_required"]["kind"], "question")
            self.assertEqual(snapshot["input_required"]["message"], "Architect: 2 questions waiting")
            self.assertEqual(snapshot["input_required"]["question_cards"], [{"role": "architect", "count": 2}])
            self.assertEqual(snapshot["questions"]["by_role"], [
                {"role": "architect", "count": 2, "blocking": 2,
                 "question_ids": [form["question_id"], plain["question_id"]]}])
            before = self._snapshot_files()
            code, body = self._api(server, "POST", "/api/question-answers",
                                   body=json.dumps({"answers": [{"question_id": form["question_id"]}]}))
            self.assertEqual(code, 400, body)
            self.assertIn("answers[0] must have exactly the keys", body["error"])
            code, body = self._api(server, "POST", "/api/question-answers",
                                   body=json.dumps({"answers": [{"question_id": form["question_id"], "choice": "Nope"}]}))
            self.assertEqual(code, 409, body)
            self.assertIn("answers[0]: choice 'Nope' is not offered", body["error"])
            code, body = self._api(server, "POST", "/api/question-answers",
                                   body=json.dumps({"nope": []}))
            self.assertEqual(code, 400, body)
            self.assertEqual(self._snapshot_files(), before)
            code, body = self._api(server, "POST", "/api/question-answers", body=json.dumps({"answers": [
                {"question_id": form["question_id"], "choice": "Concise"},
                {"question_id": plain["question_id"], "other": "fine"}]}))
            self.assertEqual(code, 200, body)
            self.assertEqual(body["question_ids"], [form["question_id"], plain["question_id"]])
            code, snapshot = self._api(server, "GET", "/api/dashboard")
            self.assertFalse(snapshot["input_required"]["required"])
            self.assertEqual(snapshot["questions"]["by_role"], [])
            # Same-origin only, like every other Pilot control.
            host, port = server.server_address[:2]
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("POST", "/api/question-answers", body="{}",
                               headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
            forbidden = connection.getresponse()
            forbidden.read()
            self.assertEqual(forbidden.status, 403)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
        recorded = [e for e in self._question_events() if e["kind"] == "question_answers_recorded"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["by"], "Mission Control Pilot")
        self.assertEqual([a["answered_by"] for a in recorded[0]["answers"]], ["Mission Control Pilot"] * 2)
        self.assertEqual(self.read_status()["status"], "in_progress")

    def test_banner_names_one_card_per_role_and_a_lone_question_keeps_its_text(self):
        self._ask(self.FORM)
        request = self.dashboard._input_request(self.read_status(), self.lib.load_config(self.tmp))
        self.assertEqual(request["message"], "Architect: 1 question waiting: Which path?")
        self._ask("Reviewer one?", role="reviewer")
        self._ask("Reviewer two?", role="reviewer")
        request = self.dashboard._input_request(self.read_status(), self.lib.load_config(self.tmp))
        self.assertEqual(request["message"], "Architect: 1 question waiting · Reviewer: 2 questions waiting")
        self.assertEqual(request["question_cards"], [{"role": "architect", "count": 1}, {"role": "reviewer", "count": 2}])
        self.assertNotIn("Reviewer one?", request["message"])

    def test_delivery_is_one_section_ordered_by_asked_at_marked_in_that_launch(self):
        first = self._ask(self.FORM)
        second = self._ask("Region?")
        third = self._ask('{"text": "Depth?", "options": ["Shallow", "Deep"], "recommended": "Deep"}')
        # Answer out of order; delivery still follows asked_at.
        self.lib.answer_questions_batch(self.tmp, by="moncy", answers=[
            {"question_id": third["question_id"], "choice": "Deep"},
            {"question_id": first["question_id"], "other": "Neither, do a spike"},
            {"question_id": second["question_id"], "other": "us-east-1"},
        ])
        events_before = len(self._events())
        text = self.runtime.build_role_input(self.tmp, "architect", "next task")
        self.assertEqual(text.count("# Pilot answers to your earlier questions"), 1)
        section = text[text.index("# Pilot answers to your earlier questions"):]
        positions = [section.index(q["question_id"]) for q in (first, second, third)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Options offered: Concise, Full", section)
        self.assertIn("A (moncy): Neither, do a spike", section)
        self.assertIn("A (moncy): Deep (chosen from the offered options)", section)
        self.assertIn("A (moncy): us-east-1", section)
        events = self._events()
        self.assertEqual(len(events), events_before + 1)
        delivered = events[-1]
        self.assertEqual(delivered["kind"], "question_answers_delivered")
        self.assertEqual(sorted(delivered["question_ids"]),
                         sorted(q["question_id"] for q in (first, second, third)))
        status = self.read_status()
        stamps = {q["delivered_at"] for q in status["pending_questions"]}
        self.assertEqual(len(stamps), 1)
        self.assertIsNotNone(stamps.pop())
        again = self.runtime.build_role_input(self.tmp, "architect", "next task")
        self.assertNotIn("# Pilot answers to your earlier questions", again)
        self.assertEqual(len(self._events()), events_before + 1)

    # -- i48-legacy-and-prompts ----------------------------------------------

    def test_prompts_carry_the_structured_example_and_the_three_question_rule(self):
        example = 'HANDSOFF_QUESTION: {"text": "Which path?", "options": ["Concise", "Full"], "recommended": "Concise"}'
        for role in ("architect", "implementer", "reviewer", "supervisor"):
            prompt = (ROOT / "prompts" / f"{role}.md").read_text()
            self.assertIn(example, prompt, role)
            self.assertIn("Plain text after the prefix still works", prompt, role)
            self.assertIn("at most three questions, each with options and a recommended answer", prompt, role)
            self.assertNotIn("\u2014", prompt, role)

    def test_no_record_or_event_carries_prompt_output_or_environment_content(self):
        secret_env = "HANDSOFF_TEST_SECRET_VALUE_48"
        with mock.patch.dict(os.environ, {"HANDSOFF_TEST_SECRET": secret_env}):
            form = self._ask(self.FORM)
            self.lib.answer_questions_batch(self.tmp, by="moncy", answers=[
                {"question_id": form["question_id"], "choice": "Full"}])
            self.runtime.build_role_input(self.tmp, "architect", "private task text 48")
        status_text = (self.tmp / "handsoff-status.json").read_text()
        events_text = (self.tmp / "handsoff-events.jsonl").read_text()
        prompt_text = (ROOT / "prompts" / "architect.md").read_text().splitlines()[0]
        for haystack in (status_text, events_text):
            self.assertNotIn(secret_env, haystack)
            self.assertNotIn("private task text 48", haystack)
            self.assertNotIn(prompt_text, haystack)
        for record in self.read_status()["pending_questions"]:
            self.assertEqual(set(record), self.lib.QUESTION_FIELDS)
        for event in self._question_events():
            self.assertFalse(set(event) & {"prompt", "output", "environment", "env", "stdout"}, event)


class TestDesignReviewBudgetAuthorizeControl(HandsoffTestCase):
    """#35 follow-up: an exhausted design-review budget shows its own Pilot
    control on Mission Control, and the control records the same
    authorization the CLI does."""

    def setUp(self):
        super().setUp()
        self.init("Budget control")
        sys.path.insert(0, str(BIN))
        import handsoff_dashboard
        self.dashboard = handsoff_dashboard
        run(["criterion-update", "REQ-001", "--requirement", "A real criterion"], cwd=self.tmp)
        run(["advance", "2", "10"], cwd=self.tmp)
        for n in (1, 2):
            r = run(["record-design-review", "--by", f"rev-{n}", "--architect", "arch",
                     "--request-changes", "--summary", f"round {n}"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["advance", "2", "10", "--status", "blocked", "--next-action", "authorize attempt 3",
                 "--authorization-hold", "design_review"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _api(self, server, method, path, body=None):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload)

    def test_exhausted_budget_is_its_own_input_kind_and_the_control_authorizes_once(self):
        server = self.dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            code, snapshot = self._api(server, "GET", "/api/dashboard")
            self.assertEqual(snapshot["input_required"]["kind"], "design_review_budget")
            self.assertIn("2 of 2", snapshot["input_required"]["message"])
            code, result = self._api(server, "POST", "/api/design-review-authorize", body="{}")
            self.assertEqual(code, 200, result)
            authorization = self.read_status()["design_review_authorization"]
            self.assertEqual(authorization["by"], "Mission Control Pilot")
            self.assertEqual(authorization["attempt_permitted"], 3)
            code, again = self._api(server, "POST", "/api/design-review-authorize", body="{}")
            self.assertEqual(code, 409, again)
        finally:
            server.shutdown()
            server.server_close()
        html = (ROOT / "dashboard" / "index.html").read_text()
        app = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn('id="design-review-authorize"', html)
        self.assertIn('state.inputKind !== "design_review_budget"', app)
        self.assertIn('fetch("/api/design-review-authorize"', app)
