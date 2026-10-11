"""#444: a run-owned dashboard that dies leaves its owner file behind and
nothing noticed: the run sat waiting on regression-decide, a decision only
that dashboard serves. status, recover and watch now check the file's pid
and endpoint from outside, ledger one owned_dashboard_down event naming it,
and tell the Pilot the command that restarts it; Fleet's card says the same."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, _harness_env, run

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OwnedDashboardCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.init("Owned dashboard liveness #444")
        self.elsewhere = Path(tempfile.mkdtemp(prefix="handsoff-elsewhere-"))
        self._children = []

    def tearDown(self):
        for child in self._children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        super().tearDown()

    def spawn(self, argv):
        """A real dashboard process, started from another directory."""
        child = subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), *argv],
                                 cwd=self.elsewhere, env=_harness_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(child)
        owner = lib.dashboard_owner_path(self.tmp)
        for _ in range(300):
            if owner.exists():
                record = json.loads(owner.read_text())
                state = lib.owned_dashboard_state(self.tmp, self.read_status())
                if record.get("pid") == child.pid and (state or {}).get("state") == "live":
                    return child, record
            time.sleep(0.05)
        self.fail("the dashboard never came up as this run's owner")

    def start_dashboard(self, mode="--owned-by-run", port=0):
        return self.spawn(["--root", str(self.tmp), "dashboard", mode, "--port", str(port), "--no-open"])

    def kill(self, child):
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)

    def write_owner(self, **fields):
        record = {"pid": os.getpid(), "host": "127.0.0.1", "port": closed_port(),
                  "started_at": "2026-10-10T00:00:00+00:00", "owner": "ship-feature",
                  "run_token": lib.new_dashboard_run_token(),
                  "root_sha256": lib.dashboard_root_sha256(self.tmp), "feature": "f", **fields}
        record = {k: v for k, v in record.items() if v is not None}
        lib.dashboard_owner_path(self.tmp).write_text(json.dumps(record))
        return record

    def status_json(self, *, check=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = supervisor.cmd_status(argparse.Namespace(root=str(self.tmp)))
        if check:
            self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def events(self, kind):
        return [e for e in lib.read_events(self.tmp, lib.load_config(self.tmp)) if e.get("kind") == kind]

    def restart_argv(self, line):
        self.assertIn("restart it: ", line)
        argv = shlex.split(line.split("restart it: ", 1)[1])
        self.assertEqual(argv[:2], ["handsoff", "supervisor"])
        return argv[2:]


class StatusReportsDeadDashboard(OwnedDashboardCase):
    def test_killing_the_owned_dashboard_is_ledgered_once_and_names_the_restart(self):
        child, record = self.start_dashboard()
        self.kill(child)
        shown = self.status_json()
        down = self.events("owned_dashboard_down")
        self.assertEqual(len(down), 1)
        self.assertEqual((down[0]["pid"], down[0]["host"], down[0]["port"], down[0]["reason"]),
                         (record["pid"], "127.0.0.1", record["port"], "pid_gone"))
        self.assertIn(f"pid {record['pid']}", down[0]["message"])
        self.assertIn(f"127.0.0.1:{record['port']}", down[0]["message"])
        line = shown["next_action"]
        self.assertEqual(line, down[0]["required_action"])
        self.assertEqual(self.restart_argv(line), ["--root", str(self.tmp.resolve()), "dashboard",
                                                  "--owned-by-run", "--port", str(record["port"]), "--no-open"])
        self.assertEqual(shown["owned_dashboard"]["state"], "down")
        self.assertEqual(self.read_status()["dashboard_down_reported"]["pid"], record["pid"])
        self.assertNotIn("restart it", self.read_status()["next_action"], "the override is never stored")
        self.status_json()
        self.assertEqual(len(self.events("owned_dashboard_down")), 1, "a repeat status adds no event")

    def test_the_printed_command_restarts_this_run_from_another_directory(self):
        child, record = self.start_dashboard()
        self.kill(child)
        argv = self.restart_argv(self.status_json()["next_action"])
        restarted, again = self.spawn(argv)
        self.assertEqual(again["port"], record["port"])
        self.assertEqual(again["root_sha256"], lib.dashboard_root_sha256(self.tmp))
        self.assertNotEqual(again["pid"], record["pid"])

    def test_a_live_pid_whose_port_does_not_answer_is_down(self):
        record = self.write_owner()
        shown = self.status_json()
        down = self.events("owned_dashboard_down")
        self.assertEqual([(e["pid"], e["port"], e["reason"]) for e in down],
                         [(record["pid"], record["port"], "not_responding")])
        self.assertIn(f"--port {record['port']}", shown["next_action"])

    def test_a_view_only_record_restarts_view_only(self):
        self.write_owner(pid=dead_pid(), view_only=True)
        argv = self.restart_argv(self.status_json()["next_action"])
        self.assertIn("--view-only", argv)
        self.assertNotIn("--owned-by-run", argv)

    def test_a_stranded_regression_line_wins_then_the_dashboard_line(self):
        self.write_owner(pid=dead_pid())
        stranded = {"state": "stranded", "required_action": "Run regression-finalize for rg-1"}
        with patch_engine("regression_liveness", return_value=stranded):
            self.assertEqual(self.status_json()["next_action"], stranded["required_action"])
        self.assertEqual(len(self.events("owned_dashboard_down")), 1, "still ledgered under the regression")
        self.assertIn("restart it", self.status_json()["next_action"])

    def test_a_second_dashboard_that_dies_is_reported_again(self):
        first = self.write_owner(pid=dead_pid())
        self.status_json()
        second = self.write_owner(pid=dead_pid())
        self.status_json()
        self.assertEqual([e["pid"] for e in self.events("owned_dashboard_down")], [first["pid"], second["pid"]])


class RecoverReportsDeadDashboard(OwnedDashboardCase):
    def recover(self, *extra):
        r = run(["recover", "--by", "test-supervisor", *extra], cwd=self.tmp)
        return r

    def test_recover_ledgers_the_dead_dashboard_once_and_prints_the_restart(self):
        child, record = self.start_dashboard(mode="--view-only")
        self.kill(child)
        first = self.recover()
        self.assertIn(f"pid {record['pid']}", first.stdout)
        self.assertIn("--view-only", first.stdout)
        self.assertIn(f"--root {shlex.quote(str(self.tmp.resolve()))}", first.stdout)
        self.recover()
        down = self.events("owned_dashboard_down")
        self.assertEqual([(e["pid"], e["port"]) for e in down], [(record["pid"], record["port"])])

    def test_watch_once_ledgers_and_prints_it(self):
        record = self.write_owner(pid=dead_pid())
        r = run(["watch", "--once", "--by", "test-supervisor"], cwd=self.tmp)
        self.assertIn("restart it:", r.stdout, r.stdout + r.stderr)
        self.assertEqual([e["pid"] for e in self.events("owned_dashboard_down")], [record["pid"]])

    def test_recover_dry_run_writes_nothing(self):
        self.write_owner(pid=dead_pid())
        r = self.recover("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.events("owned_dashboard_down"), [])


class HealthyOrOutOfScopeIsQuiet(OwnedDashboardCase):
    def assert_quiet(self):
        stored = self.read_status()["next_action"]
        shown = self.status_json()
        self.assertEqual(shown["next_action"], stored)
        self.assertEqual(self.events("owned_dashboard_down"), [])

    def test_a_live_answering_dashboard_is_quiet(self):
        self.start_dashboard()
        self.assert_quiet()
        self.assertEqual(self.status_json()["owned_dashboard"]["state"], "live")

    def test_no_owner_record_is_quiet(self):
        self.assert_quiet()
        self.assertIsNone(self.status_json()["owned_dashboard"])

    def test_a_record_bound_to_another_root_is_quiet(self):
        self.write_owner(pid=dead_pid(), root_sha256=lib.dashboard_root_sha256(self.elsewhere))
        self.assert_quiet()

    def test_a_complete_or_closed_run_is_quiet(self):
        self.write_owner(pid=dead_pid())
        for change in ({"status": "complete"}, {"run_closed": {"outcome": "closed"}}):
            status = lib.load_unique_json(self.tmp / "handsoff-status.json")
            self.assertIsNone(lib.owned_dashboard_state(self.tmp, {**status, **change}))

    def test_a_run_closed_between_probe_and_lock_gets_no_event(self):
        self.write_owner(pid=dead_pid())
        cfg = lib.load_config(self.tmp)
        state = lib.owned_dashboard_state(self.tmp, self.read_status())
        self.assertEqual(state["state"], "down")
        path = self.tmp / "handsoff-status.json"
        status = json.loads(path.read_text())
        path.write_text(json.dumps({**status, "status": "complete"}))
        with lib.project_lock(self.tmp):
            self.assertIsNone(lib.record_dashboard_down_transition(self.tmp, cfg, state))
        self.assertEqual(self.events("owned_dashboard_down"), [])

    def test_status_across_that_race_shows_the_stored_step(self):
        self.write_owner(pid=dead_pid())
        path = self.tmp / "handsoff-status.json"
        real_probe = supervisor._owned_dashboard_probe

        def probe_then_close(root, cfg):
            state = real_probe(root, cfg)
            self.assertEqual(state["state"], "down")
            with lib.project_lock(root):  # a ledgered close, so status still validates
                lib.commit(root, cfg, status={**json.loads(path.read_text()), "status": "complete"},
                           event_kind="test_run_completed", event_message="closed between probe and lock")
            return state

        stored = self.read_status()["next_action"]
        with mock.patch.object(supervisor, "_owned_dashboard_probe", probe_then_close):
            shown = self.status_json(check=False)  # a complete fixture run with no evidence is "blocked"
        self.assertEqual(shown["next_action"], stored)
        self.assertIsNone(shown["owned_dashboard"])
        self.assertEqual(self.events("owned_dashboard_down"), [])

    def test_a_restart_is_ledgered_once_as_restored_and_the_stored_step_returns(self):
        child, record = self.start_dashboard()
        self.kill(child)
        self.restart_argv(self.status_json()["next_action"])
        self.start_dashboard(port=record["port"])
        shown = self.status_json()
        self.status_json()
        restored = self.events("owned_dashboard_restored")
        self.assertEqual(len(restored), 1)
        self.assertEqual(shown["next_action"], self.read_status()["next_action"])
        self.assertNotIn("restart it", shown["next_action"])
        self.assertIsNone(self.read_status()["dashboard_down_reported"])

    def test_after_restore_a_stranded_regression_line_still_wins(self):
        child, record = self.start_dashboard()
        self.kill(child)
        self.status_json()
        self.start_dashboard(port=record["port"])
        stranded = {"state": "stranded", "required_action": "Run regression-finalize for rg-1"}
        with patch_engine("regression_liveness", return_value=stranded):
            self.assertEqual(self.status_json()["next_action"], stranded["required_action"])
        self.assertEqual(len(self.events("owned_dashboard_restored")), 1)

    def test_the_status_schema_holds_the_field(self):
        good = {"pid": 5, "host": "127.0.0.1", "port": 8790, "reported_at": "2026-10-10T00:00:00+00:00"}
        base = self.read_status()
        self.assertEqual([e for e in lib.validate_status_schema({**base, "dashboard_down_reported": good})
                          if "dashboard_down_reported" in e], [])
        for bad in ({**good, "port": 0}, {**good, "reported_at": "2026-10-10T00:00:00"}, {"pid": 5}, "x"):
            self.assertTrue([e for e in lib.validate_status_schema({**base, "dashboard_down_reported": bad})
                             if "dashboard_down_reported" in e], bad)


class FleetCardRestartLine(OwnedDashboardCase):
    def card(self):
        entry = {"root": str(self.tmp), "registered_at": "2026-10-10T00:00:00+00:00"}
        return fleet.project_view(entry)

    def test_a_dead_owned_dashboard_card_shows_owned_by_run_restart(self):
        child, record = self.start_dashboard()
        self.kill(child)
        card = self.card()
        self.assertEqual(card["state"], "offline")
        self.assertEqual(self.restart_argv(card["next_action"]), [
            "--root", str(self.tmp.resolve()), "dashboard", "--owned-by-run", "--port", str(record["port"]),
            "--no-open"])

    def test_a_dead_view_only_dashboard_card_shows_view_only_restart(self):
        child, record = self.start_dashboard(mode="--view-only")
        self.kill(child)
        view = fleet._owner_view(self.tmp)
        self.assertEqual(view["reason"], fleet.OWNER_PID_GONE)
        self.assertNotIn("view_only", view, "an unverified server is never labelled view-only")
        argv = self.restart_argv(self.card()["next_action"])
        self.assertIn("--view-only", argv)
        self.assertNotIn("--owned-by-run", argv)

    def test_a_live_pid_not_answering_is_offline_with_the_restart_line(self):
        record = self.write_owner()
        card = self.card()
        self.assertEqual(fleet._owner_view(self.tmp)["reason"], fleet.OWNER_NOT_RESPONDING)
        self.assertEqual(card["state"], "offline")
        self.assertIn(f"--port {record['port']}", card["next_action"])

    def test_a_gone_pid_whose_port_another_server_answers_shows_the_restart_line(self):
        _other, serving = self.start_dashboard(mode="--view-only")
        record = self.write_owner(pid=dead_pid(), port=serving["port"])  # a new token: not that server
        self.assertEqual(fleet._owner_view(self.tmp)["reason"], fleet.OWNER_PID_GONE)
        card = self.card()
        self.assertEqual(card["state"], "offline")
        self.assertIn(f"pid {record['pid']}", card["next_action"])
        self.assertIn(f"--port {serving['port']}", card["next_action"])

    def test_a_gone_pid_whose_port_answers_as_this_owner_is_still_offline(self):
        _other, serving = self.start_dashboard()
        # the recorded token and root match the answering server; only the pid is gone
        record = self.write_owner(pid=dead_pid(), port=serving["port"], run_token=serving["run_token"])
        view = fleet._owner_view(self.tmp)
        self.assertEqual(view["health"], "stale")
        self.assertEqual(view["reason"], fleet.OWNER_PID_GONE)
        card = self.card()
        self.assertEqual(card["state"], "offline")
        self.assertIn(f"pid {record['pid']}", card["next_action"])
        self.assertEqual(lib.owned_dashboard_state(self.tmp, self.read_status())["reason"], "pid_gone")

    def test_a_healthy_dashboard_card_keeps_the_stored_step(self):
        self.start_dashboard()
        self.assertEqual(self.card()["next_action"], self.read_status()["next_action"])

    def test_a_foreign_root_record_keeps_the_stored_step(self):
        self.write_owner(pid=dead_pid(), root_sha256=lib.dashboard_root_sha256(self.elsewhere))
        self.assertEqual(self.card()["next_action"], self.read_status()["next_action"])


if __name__ == "__main__":
    import unittest
    unittest.main()
