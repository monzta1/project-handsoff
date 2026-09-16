import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import ROOT, BIN, normalize_fixture_config, run

import sys
sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard
import handsoff_fleet as fleet
import handsoff_lib as lib


class FleetMissionControlTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-fleet-test-"))
        self.registry = self.base / "fleet.json"
        self.archive = self.base / "archive"
        self.old_archive = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.archive)

    def tearDown(self):
        if self.old_archive is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self.old_archive
        shutil.rmtree(self.base, ignore_errors=True)

    def project(self, name):
        root = self.base / name
        root.mkdir()
        for item in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / item, root / item)
        normalize_fixture_config(root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", root / "schemas")
        shutil.copytree(ROOT / "bin", root / "bin")
        shutil.copytree(ROOT / "dashboard", root / "dashboard")
        result = run(["init", f"Feature for {name}"], root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root

    def test_registry_and_multi_project_snapshot_are_isolated(self):
        first = self.project("alpha")
        second = self.project("beta")
        fleet.register_project(first, self.registry)
        fleet.register_project(second, self.registry)
        fleet.register_project(first, self.registry)
        snapshot = fleet.build_fleet(self.registry)
        self.assertEqual([item["name"] for item in snapshot["projects"]], ["alpha", "beta"])
        self.assertEqual(len(fleet.load_registry(self.registry)), 2)
        self.assertNotEqual(snapshot["projects"][0]["binding"], snapshot["projects"][1]["binding"])
        self.assertEqual(snapshot["projects"][0]["engine_version"], "v0.3.5")

    def test_dashboard_progress_uses_phase_floor_and_preserves_verification_score(self):
        root = self.project("progress")
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            status.update(phase_number=2, phase=lib.PHASES[2], progress=0)
            lib.commit(root, cfg, status=status, event_kind="fixture_phase", event_message="fixture")
        snapshot = dashboard.build_snapshot(root)
        self.assertEqual(snapshot["status"]["progress"], 20)
        self.assertEqual(snapshot["status"]["verification_progress"], 0)

    def test_clean_close_and_reopen_are_audited_idempotent_and_non_destructive(self):
        root = self.project("closure")
        source = root / "valuable-source.txt"
        source.write_text("preserve me")
        before = json.loads((root / "handsoff-status.json").read_text())["updated_at"]
        closed = lib.close_run(root, by="Pilot", reason="Leaving the mission safely",
                               expected_updated_at=before, release_dashboard=False)
        self.assertTrue(closed["closed"])
        self.assertEqual(source.read_text(), "preserve me")
        again = lib.close_run(root, by="Pilot", reason="retry", release_dashboard=False)
        self.assertTrue(again["already_closed"])
        events = [json.loads(line) for line in (root / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(item["kind"] == "run_closed" for item in events), 1)
        updated = json.loads((root / "handsoff-status.json").read_text())["updated_at"]
        self.assertTrue(lib.reopen_run(root, by="Pilot", reason="Continue", expected_updated_at=updated)["reopened"])
        self.assertNotIn("run_closed", json.loads((root / "handsoff-status.json").read_text()))

    def test_active_close_requires_confirmation_and_terminalizes_once(self):
        root = self.project("active")
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            sid = "hs-" + "12" * 16
            now = status["updated_at"]
            status["agent_sessions"] = {sid: {"session_id": sid, "role": "implementer", "actor": "codex-implementer",
                "adapter": "codex", "requested_model": "default", "reported_model": None,
                "resolution_source": "configured", "state": "running", "started_at": now,
                "running_at": now, "ended_at": None, "exit_code": None, "tier": None,
                "phase_number": 4, "packet_id": None, "design_hash": None}}
            status["current_agent_sessions"] = {"implementer": sid}
            lib.commit(root, cfg, status=status, event_kind="fixture_session", event_message="fixture")
        with self.assertRaisesRegex(lib.HandsoffError, "explicit cancel"):
            lib.close_run(root, by="Pilot", reason="stop", cancel_active=False, release_dashboard=False)
        terminated = []
        result = lib.close_run(root, by="Pilot", reason="stop", cancel_active=True,
                               terminate_process=lambda _root, session: terminated.append(session["session_id"]) or {"ok": True},
                               release_dashboard=False)
        self.assertTrue(result["run_closed"]["cancelled_active"])
        self.assertEqual(terminated, [sid])
        status = json.loads((root / "handsoff-status.json").read_text())
        self.assertEqual(status["agent_sessions"][sid]["state"], "cancelled")
        self.assertNotIn("implementer", status["current_agent_sessions"])

    def test_fleet_api_rejects_stale_binding_and_releases_only_through_library(self):
        root = self.project("api")
        fleet.register_project(root, self.registry)
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            def post(body):
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/api/release-port", json.dumps(body),
                                   {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"})
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                return response.status, payload
            with mock.patch.object(lib, "release_run_dashboard", return_value={"released": False, "reason": "none"}) as release:
                code, _ = post({"root": str(root), "binding": "stale", "confirm": True})
                self.assertEqual(code, 409)
                release.assert_not_called()
                binding = fleet.build_fleet(self.registry)["projects"][0]["binding"]
                code, body = post({"root": str(root), "binding": binding, "confirm": True})
                self.assertEqual(code, 200, body)
                release.assert_called_once_with(root.resolve())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_fleet_ui_requires_explicit_confirmation(self):
        html = (ROOT / "fleet" / "index.html").read_text()
        script = (ROOT / "fleet" / "app.js").read_text()
        self.assertIn("Fleet Mission Control", html)
        self.assertIn("CONFIRM EXACT OPERATION", html)
        self.assertIn("/api/release-port", script)
        self.assertIn("/api/close-run", script)
        self.assertIn("/api/reopen-run", script)
        self.assertIn("EventSource", script)


if __name__ == "__main__":
    unittest.main()
