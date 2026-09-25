import http.client
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine

from tests.test_handsoff_supervisor import ROOT, BIN, normalize_fixture_config, run

CURRENT_VERSION = json.loads((ROOT / "handsoff-runtime.json").read_text())["version"]

import sys
sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard
import handsoff_fleet as fleet
import handsoff_lib as lib


def _register_in_process(root, registry):
    fleet.register_project(Path(root), Path(registry))


def _lock_in_process(registry, queue):
    try:
        with fleet.registry_lock(Path(registry), timeout=0.1):
            pass
    except Exception as exc:
        queue.put(str(exc))


class _FleetFixture(unittest.TestCase):
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



class FleetMissionControlTests(_FleetFixture):
    def test_process_concurrent_register_preserves_every_project(self):
        roots = [self.project(f"concurrent-{index}") for index in range(4)]
        context = multiprocessing.get_context("fork")
        workers = [context.Process(target=_register_in_process,
                                   args=(str(root), str(self.registry))) for root in roots]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        saved = fleet.load_registry(self.registry)
        self.assertEqual({item["root"] for item in saved}, {str(root.resolve()) for root in roots})
        self.assertEqual(json.loads(self.registry.read_text())["schema"], 1)

    def test_registry_lock_contention_fails_actionably_without_mutation(self):
        root = self.project("lock-timeout")
        fleet.register_project(root, self.registry)
        before = self.registry.read_bytes()
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        with fleet.registry_lock(self.registry):
            worker = context.Process(target=_lock_in_process, args=(str(self.registry), queue))
            worker.start()
            worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        self.assertIn("lock timed out", queue.get(timeout=2))
        self.assertEqual(self.registry.read_bytes(), before)

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
        self.assertEqual(snapshot["projects"][0]["engine_version"], CURRENT_VERSION)

    def test_dashboard_progress_uses_phase_floor_and_preserves_verification_score(self):
        root = self.project("progress")
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            status.update(phase_number=2, phase=lib.PHASES[2], progress=0)
            lib.commit(root, cfg, status=status, event_kind="fixture_phase", event_message="fixture")
        snapshot = dashboard.build_snapshot(root)
        self.assertEqual(snapshot["status"]["progress"], 20)
        # The work-item recompute keeps the "initialized" gate (5) as the
        # verification floor; nothing is verified yet.
        self.assertEqual(snapshot["status"]["verification_progress"], 5)

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

    def test_fleet_card_shows_when_a_run_started_and_finished(self):
        """#143: the frozen elapsed clock said how long; it never said when."""
        script = (ROOT / "fleet" / "app.js").read_text()
        self.assertIn("function stamp(value)", script)
        self.assertIn("STARTED ${esc(stamp(project.started_at))}", script)
        self.assertIn("FINISHED ${esc(stamp(project.ended_at))}", script)
        # FINISHED is conditional on ended_at; STARTED on started_at
        self.assertIn('project.ended_at\n    ? `<span class="stamp stamp-finished"', script)
        self.assertIn('if (!project.started_at) return "";\n  const started', script)
        # the date rides along only when the stamp is not today
        self.assertIn("sameDay ? time :", script)
        self.assertIn("${timing(project)}", script)
        self.assertIn(".stamp-finished", (ROOT / "fleet" / "styles.css").read_text())

    def owned_dashboard(self, root, host="127.0.0.1", token="fleet-run-token"):
        server = dashboard.DashboardServer(("127.0.0.1", 0), root,
                                           run_token=token,
                                           root_sha256=lib.dashboard_root_sha256(root))
        lib.write_dashboard_owner(root, pid=os.getpid(), host=host,
                                  port=server.server_address[1], run_token=token,
                                  root_sha256=lib.dashboard_root_sha256(root), feature="Fleet")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_project_view_exposes_verified_loopback_dashboard_url(self):
        root = self.project("healthy-nav")
        fleet.register_project(root, self.registry)
        server, thread = self.owned_dashboard(root)
        try:
            view = fleet.build_fleet(self.registry)["projects"][0]
            self.assertEqual(view["dashboard_url"], f"http://127.0.0.1:{server.server_port}/")
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_project_view_missing_owner_explains_no_dashboard(self):
        root = self.project("missing-nav")
        fleet.register_project(root, self.registry)
        view = fleet.build_fleet(self.registry)["projects"][0]
        self.assertIsNone(view["dashboard_url"])
        self.assertEqual(view["dashboard_note"], "no run-owned dashboard")

    def test_project_view_stale_token_explains_stale_ownership(self):
        root = self.project("stale-nav")
        fleet.register_project(root, self.registry)
        server, thread = self.owned_dashboard(root, token="actual-token")
        try:
            lib.write_dashboard_owner(root, pid=os.getpid(), host="127.0.0.1",
                                      port=server.server_port, run_token="wrong-token",
                                      root_sha256=lib.dashboard_root_sha256(root), feature="Fleet")
            view = fleet.build_fleet(self.registry)["projects"][0]
            self.assertIsNone(view["dashboard_url"])
            self.assertIn("stale", view["dashboard_note"])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_project_view_foreign_root_is_stale(self):
        root = self.project("foreign-nav")
        other = self.project("other-root")
        fleet.register_project(root, self.registry)
        server, thread = self.owned_dashboard(root)
        try:
            lib.write_dashboard_owner(root, pid=os.getpid(), host="127.0.0.1",
                                      port=server.server_port, run_token="fleet-run-token",
                                      root_sha256=lib.dashboard_root_sha256(other), feature="Fleet")
            view = fleet.build_fleet(self.registry)["projects"][0]
            self.assertIsNone(view["dashboard_url"])
            self.assertIn("stale", view["dashboard_note"])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_project_view_ignores_foreign_owner_host_when_verified(self):
        root = self.project("host-nav")
        fleet.register_project(root, self.registry)
        server, thread = self.owned_dashboard(root, host="evil.example")
        try:
            view = fleet.build_fleet(self.registry)["projects"][0]
            self.assertEqual(view["dashboard_url"], f"http://127.0.0.1:{server.server_port}/")
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_project_view_loses_url_after_owned_server_release(self):
        root = self.project("released-nav")
        fleet.register_project(root, self.registry)
        server, thread = self.owned_dashboard(root)
        self.assertIsNotNone(fleet.build_fleet(self.registry)["projects"][0]["dashboard_url"])
        server.shutdown(); server.server_close(); thread.join(2)
        lib.remove_dashboard_owner_if_token(root, "fleet-run-token")
        view = fleet.build_fleet(self.registry)["projects"][0]
        self.assertIsNone(view["dashboard_url"])


if __name__ == "__main__":
    unittest.main()


class ProjectLogoTests(_FleetFixture):
    """A project's own artwork ([project] logo, or a conventional path)
    appears on its Fleet card and in its run dashboard header; the Fleet
    header carries the Handsoff mark. Nothing outside the project can be
    served, and a missing or oversized logo simply means no logo."""

    PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da6364f8cf"
                        "c000000301010018dd8db00000000049454e44ae426082")

    def _get(self, server, path):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), body

    def test_logo_helper_declared_conventional_and_refusals(self):
        root = self.project("gamma")
        cfg = lib.load_config(root)
        self.assertIsNone(lib.project_logo(root, cfg))
        (root / "docs" / "img").mkdir(parents=True)
        (root / "docs" / "img" / "logo.png").write_bytes(self.PNG)
        self.assertEqual(lib.project_logo(root, cfg), ((root / "docs" / "img" / "logo.png").resolve(), "image/png"))
        (root / "art").mkdir()
        (root / "art" / "mark.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        self.assertEqual(lib.project_logo(root, {**cfg, "logo": "art/mark.svg"}), ((root / "art" / "mark.svg").resolve(), "image/svg+xml"))
        # Declared but missing, wrong type, or escaping the root: no logo.
        self.assertIsNone(lib.project_logo(root, {**cfg, "logo": "art/missing.png"}))
        (root / "art" / "notes.txt").write_text("x")
        self.assertIsNone(lib.project_logo(root, {**cfg, "logo": "art/notes.txt"}))
        outside = self.base / "outside.png"
        outside.write_bytes(self.PNG)
        (root / "art" / "link.png").symlink_to(outside)
        self.assertIsNone(lib.project_logo(root, {**cfg, "logo": "art/link.png"}))
        big = root / "art" / "big.png"
        big.write_bytes(b"\0" * (lib.MAX_PROJECT_LOGO_BYTES + 1))
        self.assertIsNone(lib.project_logo(root, {**cfg, "logo": "art/big.png"}))
        # A project whose artwork is the engine's own brand mark (Handsoff
        # itself) shows no second logo: the topbar already carries that one.
        (root / "art" / "same.png").write_bytes(lib.engine_resource_path("dashboard/logo.png").read_bytes())
        self.assertIsNone(lib.project_logo(root, {**cfg, "logo": "art/same.png"}))
        # The config key itself is validated as a safe relative path.
        toml = root / "handsoff.toml"
        toml.write_text(toml.read_text().replace("[project]\n", '[project]\nlogo = "../escape.png"\n', 1))
        with self.assertRaisesRegex(lib.HandsoffError, "project.logo"):
            lib.load_config(root)

    def test_fleet_and_dashboard_serve_the_declared_logo(self):
        root = self.project("delta")
        (root / "ui").mkdir()
        (root / "ui" / "logo.png").write_bytes(self.PNG)
        toml = root / "handsoff.toml"
        toml.write_text(toml.read_text().replace("[project]\n", '[project]\nlogo = "ui/logo.png"\n', 1))
        plain = self.project("epsilon")
        fleet.register_project(root, self.registry)
        fleet.register_project(plain, self.registry)
        snapshot = fleet.build_fleet(self.registry)
        by_name = {item["name"]: item for item in snapshot["projects"]}
        self.assertEqual(by_name["delta"]["logo_url"], f"/project-logo/{fleet.logo_key(root)}")
        self.assertIsNone(by_name["epsilon"]["logo_url"])
        self.assertNotIn(str(root), by_name["delta"]["logo_url"])
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, content_type, body = self._get(server, by_name["delta"]["logo_url"])
            self.assertEqual((status, content_type, body), (200, "image/png", self.PNG))
            self.assertEqual(self._get(server, f"/project-logo/{fleet.logo_key(plain)}")[0], 404)
            self.assertEqual(self._get(server, "/project-logo/not-a-key")[0], 404)
            status, content_type, body = self._get(server, "/logo.png")
            self.assertEqual((status, content_type), (200, "image/png"))
            self.assertEqual(body, (ROOT / "dashboard" / "logo.png").read_bytes())
            self.assertIn('<img class="brand-mark" src="/logo.png"', self._get(server, "/")[2].decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        run_snapshot = dashboard.build_snapshot(root)
        self.assertEqual(run_snapshot["project"]["logo_url"], "/project-logo")
        self.assertIsNone(dashboard.build_snapshot(plain)["project"]["logo_url"])
        run_server = dashboard.DashboardServer(("127.0.0.1", 0), root)
        thread = threading.Thread(target=run_server.serve_forever, daemon=True)
        thread.start()
        try:
            status, content_type, body = self._get(run_server, "/project-logo")
            self.assertEqual((status, content_type, body), (200, "image/png", self.PNG))
        finally:
            run_server.shutdown()
            run_server.server_close()
            thread.join(timeout=2)
        plain_server = dashboard.DashboardServer(("127.0.0.1", 0), plain)
        thread = threading.Thread(target=plain_server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(self._get(plain_server, "/project-logo")[0], 404)
        finally:
            plain_server.shutdown()
            plain_server.server_close()
            thread.join(timeout=2)


class IdleProjectTests(_FleetFixture):
    """#154: a registered project with no run reads idle (filed with the
    finished runs on the page); quiet stays an initialized run with nothing
    moving; an orphaned root stays orphaned."""

    def test_no_run_is_idle_and_counted_apart_from_quiet(self):
        idle_root = self.base / "idle"
        idle_root.mkdir()
        for item in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / item, idle_root / item)
        normalize_fixture_config(idle_root / "handsoff.toml")
        quiet_root = self.project("quiet")
        gone = self.base / "gone"
        gone.mkdir()
        for item in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / item, gone / item)
        fleet.register_project(idle_root, self.registry)
        fleet.register_project(quiet_root, self.registry)
        fleet.register_project(gone, self.registry)
        shutil.rmtree(gone)
        payload = fleet.build_fleet(self.registry)
        states = {item["name"]: item["state"] for item in payload["projects"]}
        self.assertEqual(states, {"idle": "idle", "quiet": "quiet", "gone": "orphaned"})
        self.assertEqual((payload["counts"]["idle"], payload["counts"]["quiet"], payload["counts"]["orphaned"]), (1, 1, 1))
        self.assertFalse(next(item for item in payload["projects"] if item["name"] == "idle")["initialized"])


class EngineBadgeTests(_FleetFixture):
    """#161: /api/fleet names the engine the Fleet server runs."""

    def test_build_fleet_carries_the_servers_engine(self):
        snapshot = fleet.build_fleet(self.registry)
        self.assertEqual(snapshot["engine"]["version"], CURRENT_VERSION)
        self.assertIn(snapshot["engine"]["source"], {"project-drop-in", "installed-engine"})
        self.assertEqual({**fleet.fleet_engine_identity(), "install_blocked": None}, snapshot["engine"])  # #216

    def test_an_unreadable_manifest_reads_unknown(self):
        with patch_engine("engine_root", return_value=self.base / "nowhere"):
            self.assertEqual(fleet.fleet_engine_identity(), {"version": "unknown", "source": "unknown"})

    def test_the_route_and_the_stream_serve_it(self):
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals_interval=3600, issues_interval=3600)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", "/api/fleet")
            payload = json.loads(conn.getresponse().read())
            conn.close()
            self.assertEqual(payload["engine"]["version"], CURRENT_VERSION)
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", "/api/events")
            response = conn.getresponse()
            first = b""
            while b"\n\n" not in first:
                first += response.read1(4096) if hasattr(response, "read1") else response.fp.read1(4096)
            conn.close()
            self.assertIn(b'"engine":{"version":"' + CURRENT_VERSION.encode() + b'"', first)
        finally:
            server.stopping = True
            server.signals_thread.stop_event.set()
            server.issues_thread.stop_event.set()
            server.shutdown()
            server.server_close()


class DashboardOpenTests(unittest.TestCase):
    """#162: opening a dashboard reuses an existing Chrome tab."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-open-test-"))
        self.chrome = self.base / "Google Chrome.app"
        self.chrome.mkdir()
        self.bindir = self.base / "bin"
        self.bindir.mkdir()
        self.log = self.base / "osascript.log"

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def fake_osascript(self, answer: str, exit_code: int = 0):
        script = self.bindir / "osascript"
        script.write_text("#!/bin/sh\n" f"echo \"$@\" >> '{self.log}'\n" "cat > /dev/null\n"
                          + (f"echo '{answer}'\n" if answer else "") + f"exit {exit_code}\n")
        script.chmod(0o755)

    def open(self, **kwargs):
        calls = []
        with mock.patch.dict(os.environ, {"PATH": f"{self.bindir}:/usr/bin:/bin"}):
            result = lib.open_dashboard_url("http://127.0.0.1:8767/", platform="darwin", chrome=self.chrome,
                                            opener=lambda url: calls.append(url), **kwargs)
        return result, calls

    def test_found_and_opened_answers_never_reach_the_fallback(self):
        self.fake_osascript("found")
        self.assertEqual(self.open(), ("found", []))
        self.assertIn("http://127.0.0.1:8767/", self.log.read_text())
        self.fake_osascript("opened")
        self.assertEqual(self.open(), ("opened", []))
        self.fake_osascript("opened", exit_code=1)   # Chrome opened the tab, then the script died
        self.assertEqual(self.open(), ("opened", []))

    def test_a_silent_failure_or_a_missing_osascript_falls_back_exactly_once(self):
        self.fake_osascript("", exit_code=1)
        self.assertEqual(self.open(), ("fallback", ["http://127.0.0.1:8767/"]))
        (self.bindir / "osascript").unlink()
        calls = []
        with mock.patch.dict(os.environ, {"PATH": str(self.bindir)}):
            result = lib.open_dashboard_url("http://127.0.0.1:8767/", platform="darwin", chrome=self.chrome,
                                            opener=lambda url: calls.append(url))
        self.assertEqual((result, calls), ("fallback", ["http://127.0.0.1:8767/"]))

    def test_other_platforms_and_no_chrome_use_the_plain_opener_once(self):
        calls = []
        lib.open_dashboard_url("http://x/", platform="linux", opener=lambda url: calls.append(url))
        lib.open_dashboard_url("http://x/", platform="darwin", chrome=self.base / "missing.app", opener=lambda url: calls.append(url))
        self.assertEqual(calls, ["http://x/", "http://x/"])
        self.assertIn("lib.open_dashboard_url(url)", (ROOT / "bin" / "handsoff_dashboard.py").read_text())
        self.assertIn("lib.open_dashboard_url(url)", (ROOT / "bin" / "handsoff_fleet.py").read_text())
