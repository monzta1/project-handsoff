"""#456: `dashboard --view-only`. A view-only server registers as the run's
dashboard (owner file and /api/ownership carry view_only true with the same
token and root binding as an owned one) so Fleet links to it, but it starts
no orchestration loop, no recovery watchdog and no performance clock, and
never launches a role on any path, whatever [recovery] and auto_handoff say.
Human operator actions still work through it. Found live: a plain viewer
dashboard on a Sentinel lane ran the recovery watchdog beside Sentinel's host."""
from __future__ import annotations

import http.client
import json
import re
import sys
import threading
import time
from unittest import mock

from tests.engine_patch import patch_engine
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

LOOPS = ("_watchdog_loop", "_orchestration_loop", "_performance_clock_loop")


class ViewOnlyDashboardCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.init("View-only dashboard #456")
        self._stops = []

    def tearDown(self):
        for stop in reversed(self._stops):
            try:
                stop()
            except Exception:
                pass
        super().tearDown()

    def enable_recovery_and_handoff(self):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        text = re.sub(r"(?m)^auto_handoff = \w+", "auto_handoff = true", text, count=1)
        text = re.sub(r"(?m)^dashboard_watchdog = \w+", "dashboard_watchdog = true", text)
        text = re.sub(r"(?ms)(^\[recovery\]\n(?:[^\[].*?\n)*?)enabled = \w+", r"\1enabled = true", text, count=1)
        toml.write_text(text)
        cfg = lib.load_config(self.tmp)
        self.assertTrue(cfg["recovery"].get("enabled"))
        self.assertTrue(cfg["recovery"].get("dashboard_watchdog"))
        self.assertTrue(cfg.get("auto_handoff", True))

    def server(self, **kwargs):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp, **kwargs)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        self._stops.append(server.server_close)
        return server

    def view_only_server(self):
        return self.server(run_token=lib.new_dashboard_run_token(),
                           root_sha256=lib.dashboard_root_sha256(self.tmp), view_only=True)

    def owned_server(self):
        return self.server(run_token=lib.new_dashboard_run_token(),
                           root_sha256=lib.dashboard_root_sha256(self.tmp))

    def listen(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        self._stops.insert(0, server.shutdown)
        return server.server_address[1]

    def serve(self, **kwargs):
        """The real serve() path in a thread; returns (server, thread, owner record)."""
        created = []
        original = dashboard.DashboardServer

        def factory(*args, **kw):
            created.append(original(*args, **kw))
            return created[-1]

        owner = lib.dashboard_owner_path(self.tmp)
        with mock.patch.object(dashboard, "DashboardServer", factory):
            thread = threading.Thread(target=dashboard.serve, args=(self.tmp,),
                                      kwargs={"port": 0, "open_browser": False, **kwargs}, daemon=True)
            thread.start()
            for _ in range(100):
                if created and owner.exists():
                    break
                time.sleep(0.05)
        self.assertTrue(owner.exists(), "serve() must write the owner file")
        server = created[0]
        self._stops.insert(0, lambda: (server.request_stop(), thread.join(timeout=5)))
        for _ in range(40):  # the owner file may be rewritten once for view_only
            record = json.loads(owner.read_text())
            if not kwargs.get("view_only") or record.get("view_only") is True:
                break
            time.sleep(0.05)
        return server, thread, record

    def get_json(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def post_json(self, port, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", path, body=json.dumps(payload), headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    # -- REQ-001: never launches ---------------------------------------------

    def test_http_launch_role_is_refused_409_naming_view_only(self):
        server = self.view_only_server()
        port = self.listen(server)
        with mock.patch.object(handsoff_agent, "execute_with_recovery") as execute, \
                mock.patch.object(dashboard.DashboardServer, "_launch_managed_role") as launch:
            status, body = self.post_json(port, "/api/launch-role",
                                          {"action_id": "launch_role:x", "role": "architect", "task": "Go"})
        self.assertEqual(status, 409, body)
        self.assertFalse(body["ok"])
        self.assertIn("view-only", body["error"])
        launch.assert_not_called()
        execute.assert_not_called()

    def test_direct_launch_managed_role_refuses(self):
        server = self.view_only_server()
        with mock.patch.object(handsoff_agent, "build_launch_spec") as spec, \
                mock.patch.object(handsoff_agent, "execute_with_recovery") as execute, \
                patch_engine("record_dashboard_launch") as recorded:
            with self.assertRaisesRegex(lib.HandsoffError, "view-only"):
                server._launch_managed_role("reviewer", "Review it", launched_by=("watchdog", "recovery"))
        spec.assert_not_called()
        execute.assert_not_called()
        recorded.assert_not_called()

    def test_view_only_starts_none_of_the_three_threads_with_recovery_and_handoff_on(self):
        self.enable_recovery_and_handoff()
        events = {name: threading.Event() for name in LOOPS}
        patches = [mock.patch.object(dashboard.DashboardServer, name, side_effect=lambda *a, n=name: events[n].set())
                   for name in LOOPS]
        for patcher in patches:
            patcher.start()
        try:
            self.view_only_server()
            time.sleep(0.4)
            self.assertEqual([name for name in LOOPS if events[name].is_set()], [],
                             "a view-only server starts no background thread")
            # The control: an owned server under the same config starts all three.
            self.owned_server()
            for name in LOOPS:
                self.assertTrue(events[name].wait(3), f"an owned server starts {name}")
        finally:
            for patcher in patches:
                patcher.stop()

    def test_forced_orchestration_tick_launches_nothing(self):
        self.enable_recovery_and_handoff()
        server = self.view_only_server()
        server.request_first_launch("Objective")
        with patch_engine("managed_handoff_role", return_value="reviewer"), \
                mock.patch.object(dashboard.supervisor, "advance_approved_design", return_value=False) as advance, \
                mock.patch.object(handsoff_agent, "execute_with_recovery") as execute:
            server._orchestrate_once()
        advance.assert_not_called()
        execute.assert_not_called()

    def test_forced_watchdog_tick_launches_nothing(self):
        self.enable_recovery_and_handoff()
        server = self.view_only_server()
        with mock.patch.object(server._watchdog_stop, "wait", side_effect=[False, True]), \
                patch_engine("recover_run") as recover, \
                mock.patch.object(handsoff_agent, "execute_with_recovery") as execute:
            server._watchdog_loop()
        recover.assert_not_called()
        execute.assert_not_called()

    def test_owned_dashboard_still_launches(self):
        server = self.owned_server()
        with mock.patch.object(dashboard.supervisor, "performance_mutation_refusal", return_value=None), \
                mock.patch.object(handsoff_agent, "build_launch_spec", return_value={"spec": 1}), \
                mock.patch.object(handsoff_agent, "execute_with_recovery", return_value=0) as execute:
            self.assertEqual(server._launch_managed_role("reviewer", "Review it"), 0)
        execute.assert_called_once()
        self.assertFalse(server.view_only)
        self.assertNotIn("view_only", server.ownership_view())

    # -- REQ-001: registration -------------------------------------------------

    def test_owner_file_and_ownership_api_say_view_only_with_the_same_binding(self):
        server, thread, record = self.serve(view_only=True)
        self.assertIs(record.get("view_only"), True)
        self.assertEqual(record["root_sha256"], lib.dashboard_root_sha256(self.tmp))
        status, body = self.get_json(record["port"], "/api/ownership")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"owned": True, "run_token": record["run_token"],
                                "root_sha256": record["root_sha256"], "view_only": True})
        server.request_stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(lib.dashboard_owner_path(self.tmp).exists(), "its own owner file is removed on exit")

    def test_exit_leaves_an_owner_file_carrying_another_token(self):
        server, thread, record = self.serve(view_only=True)
        owner = lib.dashboard_owner_path(self.tmp)
        owner.write_text(json.dumps({**record, "run_token": lib.new_dashboard_run_token()}))
        server.request_stop()
        thread.join(timeout=5)
        self.assertTrue(owner.exists(), "a newer server's owner file is left in place")

    def test_owned_serve_owner_file_is_unchanged(self):
        _server, _thread, record = self.serve(owned_by_run=True)
        self.assertNotIn("view_only", record)
        status, body = self.get_json(record["port"], "/api/ownership")
        self.assertEqual((status, set(body)), (200, {"owned", "run_token", "root_sha256"}))

    def test_approval_action_works_through_the_view_only_server(self):
        server = self.view_only_server()
        port = self.listen(server)
        with mock.patch.object(dashboard, "build_snapshot",
                               return_value={"input_required": {"kind": "deployment_approval"}}), \
                mock.patch.object(dashboard.supervisor, "cmd_deployment_gate", return_value=0) as gate:
            status, body = self.post_json(port, "/api/deployment-approval", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        gate.assert_called_once()
        self.assertTrue(gate.call_args.args[0].approve)
        self.assertEqual(gate.call_args.args[0].by, "Mission Control Pilot")

    # -- REQ-001: the flags are exclusive --------------------------------------

    def test_view_only_and_owned_by_run_are_exclusive(self):
        with self.assertRaisesRegex(lib.HandsoffError, "exclusive"):
            dashboard.serve(self.tmp, port=0, open_browser=False, owned_by_run=True, view_only=True)
        self.assertFalse(lib.dashboard_owner_path(self.tmp).exists())
        both = run(["dashboard", "--no-open", "--port", "0", "--view-only", "--owned-by-run"], cwd=self.tmp)
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)
        self.assertIn("not allowed with", both.stderr)
        with mock.patch.object(sys, "argv", ["handsoff_dashboard.py", "--root", str(self.tmp), "--no-open",
                                             "--view-only", "--owned-by-run"]):
            with self.assertRaises(SystemExit) as exit_:
                dashboard.main()
        self.assertEqual(exit_.exception.code, 2)

    def test_supervisor_dashboard_passes_view_only_through(self):
        args = supervisor.build_parser().parse_args(["--root", str(self.tmp), "dashboard", "--no-open", "--view-only"])
        with mock.patch.object(dashboard, "serve", return_value=0) as serve:
            self.assertEqual(supervisor.cmd_dashboard(args), 0)
        self.assertIs(serve.call_args.kwargs["view_only"], True)
        self.assertIs(serve.call_args.kwargs["owned_by_run"], False)

    # -- REQ-002: Fleet's owner view -------------------------------------------

    def test_fleet_treats_a_verified_view_only_dashboard_as_healthy(self):
        _server, _thread, record = self.serve(view_only=True)
        view = fleet._owner_view(self.tmp)
        self.assertEqual((view["health"], view["port"]), ("healthy", record["port"]))
        self.assertIs(view.get("view_only"), True)

    def test_fleet_owned_dashboard_carries_no_view_only(self):
        _server, _thread, record = self.serve(owned_by_run=True)
        view = fleet._owner_view(self.tmp)
        self.assertEqual((view["health"], view["port"]), ("healthy", record["port"]))
        self.assertNotIn("view_only", view)

    def test_fleet_stale_view_only_owner_is_not_healthy(self):
        _server, _thread, record = self.serve(view_only=True)
        owner = lib.dashboard_owner_path(self.tmp)
        owner.write_text(json.dumps({**record, "run_token": lib.new_dashboard_run_token()}))
        view = fleet._owner_view(self.tmp)
        self.assertEqual(view["health"], "stale")
        self.assertNotIn("view_only", view)


if __name__ == "__main__":
    import unittest
    unittest.main()
