"""#172: a failed session whose verdict was adopted, or whose run has since
advanced, never outranks the ledger: Mission Control reads it as stopped
and Fleet never says FAILED for it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
from tests.fixture_state import force_status, write_version_pin
from tests.guards import guard


class FailedSessionSupersededTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init()

    def _failed_reviewer(self, phase=4):
        status = self.read_status()
        status["phase_number"], status["phase"] = phase, lib.PHASES[phase]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_phase",
                   event_message="fixture", actor="test")
        session = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        sid = session["session_id"]
        lib.transition_agent_session(self.tmp, sid, "running")
        lib.record_session_result(self.tmp, sid, "review", {
            "kind": "implementation", "decision": "approved", "summary": "fine", "findings": [],
            "structural_blocker": False, "symptom_reproduced": "not_applicable", "tests_executed": "yes"})
        lib.transition_agent_session(self.tmp, sid, "failed", exit_code=1, failure={
            "category": "dispatch_failed", "reason": "implementation Reviewer result requires Phase 5",
            "result_available": True, "tail_sha256": "0" * 64})
        lib.write_live_beacon(self.tmp, session_id=sid, role="reviewer", state="failed", pid=None,
                              ended_at="2026-09-20T22:11:43+00:00", exit_code=1)
        return sid

    def _states(self):
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        live = lib.live_status(status, cfg, self.tmp)
        fleet.register_project(self.tmp, self.registry)
        card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        return live, card

    def test_a_genuinely_failed_session_still_reads_failed(self):
        self._failed_reviewer()
        live, card = self._states()
        self.assertEqual(live["state"], "failed")
        self.assertEqual(card["state"], "failed")

    def test_an_adopted_verdict_reads_stopped_and_fleet_never_says_failed(self):
        sid = self._failed_reviewer()
        status = self.read_status()
        status["agent_sessions"][sid]["result"]["adopted_at"] = "2026-09-20T22:12:25+00:00"
        status["agent_sessions"][sid]["result"]["adopted_by"] = "claude-supervisor"
        status.setdefault("agent_failures", {})
        # result={} is the shape an older engine left behind; the claim is that
        # the fleet card reads it as stopped, so it is placed on disk.
        force_status(self.tmp, lib.load_config(self.tmp), status)
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("verdict adopted at 2026-09-20T22:12:25+00:00", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        # #389: the fixture just wrote the ledger, so a host may read as working.
        self.assertIn(card["state"], {"running", "quiet", "host_working", "waiting", "idle"})

    def test_an_adopted_failure_with_a_later_status_update_never_reads_failed(self):
        sid = self._failed_reviewer()
        status = self.read_status()
        status["agent_sessions"][sid]["result"] = {}
        status["agent_failures"][sid]["adopted"] = True
        status["agent_sessions"][sid]["ended_at"] = "2026-09-20T22:11:43+00:00"
        status["updated_at"] = "2026-09-20T22:12:31+00:00"
        # Same shape as above: result={} is what an older engine left behind.
        force_status(self.tmp, lib.load_config(self.tmp), status)
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("status updated at 2026-09-20T22:12:31+00:00", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        self.assertIn(card["state"], {"running", "quiet", "host_working", "waiting", "idle"})

    def test_a_run_that_advanced_past_the_session_reads_stopped(self):
        sid = self._failed_reviewer(phase=4)
        status = self.read_status()
        status["phase_number"], status["phase"] = 6, lib.PHASES[6]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_phase",
                   event_message="fixture", actor="test")
        live, card = self._states()
        self.assertEqual(live["state"], "stopped")
        self.assertIn("session ran at phase 4; the run advanced to phase 6", live["detail"])
        self.assertNotEqual(card["state"], "failed")
        self.assertEqual(self.read_status()["agent_sessions"][sid]["state"], "failed", "the ledger keeps the failure")

    @guard
    def test_adoption_rewrites_the_beacon_as_adopted_and_leaves_another_session_alone(self):
        sid = self._failed_reviewer(phase=5)
        self.assertEqual(lib.read_live_beacon(self.tmp)["state"], "failed")
        self.assertFalse(lib.mark_beacon_adopted(self.tmp, "hs-" + "0" * 32), "another session's beacon is not touched")
        self.assertEqual(lib.read_live_beacon(self.tmp)["state"], "failed")
        self.assertTrue(lib.mark_beacon_adopted(self.tmp, sid))
        beacon = lib.read_live_beacon(self.tmp)
        self.assertEqual((beacon["state"], beacon["session_id"], beacon["exit_code"]), ("adopted", sid, 1))
        source = (BIN / "handsoff_supervisor.py").read_text()
        self.assertIn("lib.mark_beacon_adopted(root, session_id)", source,
                      "adopt_session_result calls it, for the CLI and the launcher (#345)")


class RediscoverTests(HandsoffTestCase):
    """#384 (REQ-005): `handsoff fleet rediscover` prints rediscover_active_runs
    over make_rediscovery_inputs built from the registry, each root's ledger
    and its .handsoff-runtime-control/monitor.json, and writes no file;
    fleet serve runs the same pass at start and serves it on /api/fleet."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init()
        fleet.register_project(self.tmp, self.registry)
        fleet.note_registry_state(self.tmp, None, "open", path=self.registry)
        self.root = str(self.tmp.resolve())
        self.events = lib.read_events(self.tmp, lib.load_config(self.tmp))
        # The run id the performance clock and monitor-poll use (#383 lane A).
        self.run_id = "run-" + next(event["hash"] for event in self.events if isinstance(event.get("hash"), str))[:32]

    def _monitor(self, cursor):
        import handsoff_runtime_control as runtime_control
        from datetime import datetime, timezone
        record = runtime_control.new_monitor(self.run_id, "fleet-test", now=datetime.now(timezone.utc))
        record["cursor"] = cursor
        path = self.tmp / ".handsoff-runtime-control" / "monitor.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record))
        return record

    def _tree(self):
        """Every file under the project and the registry directory, with its bytes."""
        files = {}
        for base in (self.tmp, self.registry.parent):
            for path in sorted(base.rglob("*")):
                if path.is_file() and ".git" not in path.parts:
                    files[str(path)] = path.read_bytes()
        return files

    def _cli(self):
        before = self._tree()
        result = subprocess.run([sys.executable, str(BIN / "handsoff_fleet.py"), "rediscover"], capture_output=True,
                                text=True, timeout=60, env={**os.environ, "HANDSOFF_FLEET_REGISTRY": str(self.registry),
                                                            "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._tree(), before, "rediscover writes no file")
        return json.loads(result.stdout)

    def test_the_command_prints_the_merged_result_from_registry_ledger_and_monitor(self):
        self._monitor(cursor=len(self.events) + 5)
        found = self._cli()
        self.assertEqual(found, [{"run_id": self.run_id, "root": self.root, "cursor": len(self.events) + 5,
                                  "sources": ["fleet", "ledger", "monitor"], "disposition": "resume_monitoring"}])
        import handsoff_runtime_control as runtime_control
        self.assertEqual(found, runtime_control.rediscover_active_runs(fleet.rediscovery_inputs(self.registry)))

    def test_without_a_monitor_record_the_registry_and_ledger_still_find_the_run(self):
        found = self._cli()
        self.assertEqual([(item["run_id"], item["sources"], item["cursor"]) for item in found],
                         [(self.run_id, ["fleet", "ledger"], len(self.events))])

    def test_a_monitor_record_that_does_not_validate_contributes_nothing(self):
        path = self.tmp / ".handsoff-runtime-control" / "monitor.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"schema": "handsoff.monitor", "version": 1, "run_id": self.run_id}))
        self.assertEqual([item["sources"] for item in self._cli()], [["fleet", "ledger"]])

    def test_a_closed_run_is_not_rediscovered(self):
        self._monitor(cursor=1)
        fleet.note_registry_state(self.tmp, None, "closed", path=self.registry)
        status = self.read_status()
        status["run_closed"] = {"at": "2026-10-05T00:00:00+00:00", "by": "test", "reason": "fixture"}
        force_status(self.tmp, lib.load_config(self.tmp), status)
        self.assertEqual(self._cli(), [])

    def test_fleet_serve_runs_the_pass_at_start_and_serves_it_on_api_fleet(self):
        import http.client
        import threading
        self._monitor(cursor=2)
        expected = fleet.rediscover(self.registry)
        self.assertEqual(len(expected), 1)
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals_interval=3600, issues_interval=3600)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            connection.request("GET", "/api/fleet")
            payload = json.loads(connection.getresponse().read())
            connection.close()
        finally:
            server.stopping = True
            server.signals_thread.stop_event.set()
            server.issues_thread.stop_event.set()
            server.shutdown()
            server.server_close()
        self.assertEqual(payload["rediscovered"], expected)
        self.assertIsNone(payload["rediscovery_error"])


if __name__ == "__main__":
    unittest.main()
