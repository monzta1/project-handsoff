#!/usr/bin/env python3
"""Regression tests for Project Handsoff itself.

Every test here maps to either a bug found by hand (run the tool the way
the README says to, on a fresh copy) or a guarantee this rewrite adds.
Stdlib unittest only, no pytest dependency, so these run anywhere Python 3
is available, matching the framework's own "copy a few files into a
project" ethos.

Run: python3 tests/test_handsoff_supervisor.py -v
"""
import io
import http.client
import json
import os
import shutil
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

ISSUE24_TARGETED_COMMANDS = [
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_config_round_trip_preserves_order_and_legacy_defaults",
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_selection_orders_and_skips_unavailable_profiles_with_audit",
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_invalid_profiles_are_skipped_with_safe_audit_reasons",
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_recovery_categories_caps_and_exhaustion_pause",
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_reviewer_independence_survives_fallback",
    "python3 -m unittest tests.test_handsoff_supervisor.TestFallbackPolicy.test_dashboard_edits_fallbacks_without_mutating_current_session",
]


def set_fixture_check_commands(path, commands):
    """Give copied fixtures their own checks without inheriting dogfood checks."""
    text, count = re.subn(
        r"^commands\s*=\s*\[.*\]$", f"commands = {json.dumps(commands)}",
        path.read_text(), count=1, flags=re.MULTILINE,
    )
    if count != 1:
        raise AssertionError("could not locate [checks].commands in fixture")
    path.write_text(text)


def setUpModule():
    """Keep issue #24 dogfood checks exact; fixtures are normalized below."""
    text = (ROOT / "handsoff.toml").read_text()
    expected = f"commands = {json.dumps(ISSUE24_TARGETED_COMMANDS)}"
    if expected not in text:
        raise SystemExit(
            "This repo's handsoff.toml must list exactly the six issue #24 targeted checks."
        )
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
        set_fixture_check_commands(self.tmp / "handsoff.toml", [])
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
        styles = (ROOT / "dashboard" / "styles.css").read_text()
        for role in ("architect", "supervisor", "implementer", "reviewer"):
            self.assertIn(f'id="agent-{role}-model"', html)
            self.assertIn(f'id="agent-{role}-run"', html)
            self.assertIn(f'id="agent-{role}-effective"', html)
        self.assertIn("Executable detection does not prove", html)
        self.assertIn("default</code> sends no model flag", html)
        self.assertIn("exact model ID to pin it", html)
        self.assertIn("JSON.stringify(payload)", script)
        self.assertIn("THIS RUN:", script)
        self.assertIn("NEXT LAUNCH:", script)
        self.assertIn("state.actors?.implemented_by", script)
        self.assertIn("snapshot.runtime?.current_sessions", script)
        self.assertIn("model not recorded", script)
        self.assertIn("exact model not exposed", script)
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
        self.assertEqual(view["effective_profiles"], lib.agent_profiles(lib.load_config(self.tmp)))
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

    def test_missing_role_selections_resolve_to_first_available_adapter(self):
        import handsoff_lib as lib

        config_path = self.tmp / "handsoff.toml"
        text = config_path.read_text()
        for role in lib.SELECTABLE_AGENT_ROLES:
            text = text.replace(f'{role} = "auto"\n', f'# {role} intentionally omitted\n', 1)
        config_path.write_text(text)
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
        self.assertEqual(html.count('<option value="auto">Auto-detect</option>'), 4)
        self.assertIn("Auto-detect (currently", script)
        self.assertIn("effective_profiles", script)
        self.assertIn('profile.adapter === "configure-me" ? "auto"', script)

        legacy_path = self.tmp / "handsoff.toml"
        legacy_path.write_text(legacy_path.read_text().replace('reviewer = "auto"',
                                                               'reviewer = "configure-me"'))
        with mock.patch.object(lib.shutil, "which",
                               side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            legacy_view = dashboard._settings_view(lib.load_config(self.tmp))
        self.assertEqual(legacy_view["profiles"]["reviewer"]["adapter"], "configure-me")
        self.assertEqual(legacy_view["effective_profiles"]["reviewer"]["adapter"], "codex")

        with self.assertRaisesRegex(lib.HandsoffError, "no supported agent adapter"):
            lib.resolved_agent_profiles(lib.load_config(self.tmp), which=lambda _name: None,
                                        require_available=True)


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
        self.assertEqual(review["implementer_profile"], {
            "adapter": "claude", "model": "implementer-model", "effective_adapter": "claude",
        })
        self.assertEqual(review["reviewer_profile"], {
            "adapter": "codex", "model": "reviewer-model", "effective_adapter": "codex",
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
        })
        script = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn("snapshot.runtime?.current_sessions", script)
        self.assertIn("THIS RUN:", script)
        self.assertIn("profile not recorded", script)
        self.assertIn("exact model not reported", script)
        self.assertIn("NEXT LAUNCH:", script)

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
        set_fixture_check_commands(root / "handsoff.toml", [])
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
        self.assertIn('data-role="architect"', (ROOT / "dashboard" / "index.html").read_text())
        self.assertIn('["DESIGN REVIEWER", actors.design_reviewed_by]',
                      (ROOT / "dashboard" / "app.js").read_text())

        status = self.read_status()
        status["design_review"]["by"] = ""
        self._write_status(status)
        invalid = run(["validate"], cwd=self.tmp)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("design_review.by", invalid.stdout)
        self.assertNotIn("Traceback", invalid.stdout + invalid.stderr)


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
        set_fixture_check_commands(root / "handsoff.toml", [])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
