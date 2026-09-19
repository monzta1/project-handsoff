import json
import socket
import tempfile
import unittest
from pathlib import Path

import sys

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class OfflineAndClosedTest(HandsoffTestCase):
    def test_closed_snapshot_and_dead_owner_are_rendered(self):
        result = run(["init", "Closed mission", "--item", "#151"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = json.loads((self.tmp / "handsoff-status.json").read_text())
        result = run(["run-close", "--by", "pilot", "--reason", "finished safely",
                      "--expected-updated-at", status["updated_at"]], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snap = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snap["status"]["status"], "closed")
        self.assertEqual(snap["status"]["phase"], "Run closed")
        self.assertEqual(snap["supervisor"]["label"], "Mission closed")
        self.assertIn("finished safely", snap["supervisor"]["headline"])
        self.assertIsNone(snap["actors"]["active_role"])
        current = status["phase_number"]
        self.assertEqual(snap["phases"][current - 1]["state"], "closed")
        self.assertTrue(all(p["state"] == "complete" for p in snap["phases"][:current - 1]))

        # A dead owner on a closed run stays closed: offline is only for a run
        # that is still moving.
        self._dead_owner()
        self.assertEqual(fleet.project_view({"root": str(self.tmp), "registered_at": "now"})["state"], "closed")

    def _dead_owner(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        owner = {"port": port, "host": "127.0.0.1", "run_token": "dead", "pid": 1,
                 "root_sha256": lib.dashboard_root_sha256(self.tmp), "started_at": "2026-09-19T00:00:00+00:00",
                 "feature": "x"}
        (self.tmp / ".handsoff-dashboard-owner.json").write_text(json.dumps(owner))
        return port

    def test_dead_owner_on_a_moving_run_reads_offline_and_is_counted(self):
        result = run(["init", "Moving mission", "--item", "#151"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._dead_owner()
        registry = self.tmp / ".handsoff-fixture" / "fleet.json"
        registry.parent.mkdir(exist_ok=True)
        fleet.register_project(self.tmp, registry)
        payload = fleet.build_fleet(registry)
        project = payload["projects"][0]
        self.assertEqual(project["state"], "offline")
        self.assertIsNone(project["dashboard_url"])
        self.assertIn("not responding", project["dashboard_note"])
        self.assertEqual(payload["counts"]["offline"], 1)
        self.assertEqual(payload["counts"]["quiet"], 0)
        # Without an owner record the same run is simply quiet.
        (self.tmp / ".handsoff-dashboard-owner.json").unlink()
        self.assertEqual(fleet.build_fleet(registry)["projects"][0]["state"], "quiet")

    def test_complete_run_with_a_dead_owner_stays_complete(self):
        self.init("Complete mission")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "runner"], cwd=self.tmp).returncode, 0)
        done = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self._dead_owner()
        self.assertEqual(fleet.project_view({"root": str(self.tmp), "registered_at": "now"})["state"], "complete")
        snap = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snap["phases"][-1]["state"], "complete")
        self.assertFalse(any(p["state"] in {"active", "closed"} for p in snap["phases"]))


if __name__ == "__main__":
    unittest.main()
