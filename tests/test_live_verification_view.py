"""#148: a live verification is visible while it runs and after it fails.
verify-live keeps a transient in-flight record through run_checks'
on_progress hook; the dashboard names the running and the failed state
instead of "ready to ship"; Fleet's state follows. Real Phase 7 run with
the deployment gate approved and a live command that fails."""
import json
import os
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class LiveVerificationViewTests(HandsoffTestCase):
    def _phase_7(self, live_commands):
        self.init("Live verification visibility")
        self.set_criterion_state("passing", resolved=True)
        toml = self.tmp / "handsoff.toml"
        rendered = json.dumps(live_commands)
        text = toml.read_text()
        self.assertIn("live_commands = [", text)
        start = text.index("live_commands = [")
        end = text.index("]", start) + 1
        toml.write_text(text[:start] + f"live_commands = {rendered}" + text[end:])
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

    def _script(self, name, body):
        """A fixture command as a script under .handsoff-fixture (side state,
        outside the digest); commands may not carry shell operators."""
        folder = self.tmp / ".handsoff-fixture"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text(body)
        return f"python3 {path}"

    def _set_live_commands(self, commands):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        start = text.index("live_commands = [")
        end = text.index("]", start) + 1
        toml.write_text(text[:start] + f"live_commands = {json.dumps(commands)}" + text[end:])

    def _fleet_state(self):
        registry = self.tmp / ".handsoff-fixture" / "fleet.json"
        registry.parent.mkdir(exist_ok=True)
        fleet.register_project(self.tmp, registry)
        return fleet.build_fleet(registry)["projects"][0]["state"]

    def test_run_checks_reports_progress_before_and_after_each_command(self):
        seen = []
        results = lib.run_checks({}, self.tmp, ["printf one", "printf two"],
                                 on_progress=lambda index, total, command, result: seen.append((index, total, command, result is None)))
        self.assertEqual([r["exit_code"] for r in results], [0, 0])
        self.assertEqual(seen, [(1, 2, "printf one", True), (1, 2, "printf one", False),
                                (2, 2, "printf two", True), (2, 2, "printf two", False)])

    def test_in_flight_record_then_failure_detail_then_recovery_on_a_later_pass(self):
        marker = self.tmp / ".handsoff-fixture" / "seen-inflight.json"
        marker.parent.mkdir(exist_ok=True)
        # The second live command copies the in-flight record while it is
        # the running command, then fails; the third never runs after it?
        # No: run_checks runs every command, so the third passes and the
        # record's per-command results carry the failure.
        copy_cmd = self._script("copy_then_fail.py", (
            "import shutil, sys\n"
            f"shutil.copy({str(self.tmp / lib.LIVE_INFLIGHT_FILE)!r}, {str(marker)!r})\n"
            "print('smoke tail line')\n"
            "sys.exit(3)\n"))
        self._phase_7(["true", copy_cmd, "true"])
        before = dashboard.build_snapshot(self.tmp)
        self.assertEqual(before["status"]["phase"], "Deployment authorized · ready to ship")
        self.assertIsNone(before["verification"]["live"]["in_flight"])
        self.assertIsNone(before["verification"]["live"]["last_failure"])
        ran = run(["verify-live", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        # The in-flight record seen mid-run named the running command and the results so far.
        seen = json.loads(marker.read_text())
        self.assertEqual((seen["total"], seen["done"], seen["by"]), (3, 1, "runner"))
        self.assertEqual(seen["current"], copy_cmd)
        self.assertEqual(seen["results"], [{"command": "true", "exit_code": 0}])
        self.assertTrue(seen["started_at"].startswith("20"))
        # It is gone once the record is written; the ledger carries the failure.
        self.assertFalse((self.tmp / lib.LIVE_INFLIGHT_FILE).exists())
        after = dashboard.build_snapshot(self.tmp)
        live = after["verification"]["live"]
        self.assertIsNone(live["in_flight"])
        self.assertEqual(live["ok"], False)
        self.assertEqual(live["last_failure"]["command"], copy_cmd)
        self.assertEqual(live["last_failure"]["exit_code"], 3)
        self.assertIn("smoke tail line", live["last_failure"]["output_tail"])
        self.assertEqual(live["last_failure"]["run_id"], live["run_id"])
        self.assertTrue(after["status"]["phase"].startswith("LIVE VERIFICATION FAILED · "))
        self.assertIn("exit 3", after["status"]["phase"])
        self.assertEqual(next(p["name"] for p in after["phases"] if p["number"] == 7), after["status"]["phase"])
        self.assertEqual(self._fleet_state(), "failed")
        # The record is digest side state: validate still passes, nothing reads as drift.
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)
        # A later passing run clears the failure and the run reads ready again.
        self._set_live_commands(["true"])
        ran = run(["verify-live", "--by", "runner"], cwd=self.tmp)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        passed = dashboard.build_snapshot(self.tmp)
        self.assertIsNone(passed["verification"]["live"]["last_failure"])
        self.assertEqual(passed["verification"]["live"]["ok"], True)
        self.assertNotEqual(self._fleet_state(), "failed")

    def test_running_state_is_visible_while_the_command_runs_and_stale_records_are_ignored(self):
        gate = self.tmp / ".handsoff-fixture" / "release"
        gate.parent.mkdir(exist_ok=True)
        wait_cmd = self._script("wait_for_release.py", (
            "import os, time\n"
            f"while not os.path.exists({str(gate)!r}):\n    time.sleep(0.1)\n"))
        self._phase_7([wait_cmd])
        worker = threading.Thread(target=lambda: run(["verify-live", "--by", "runner"], cwd=self.tmp), daemon=True)
        worker.start()
        deadline = time.monotonic() + 15
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = dashboard.build_snapshot(self.tmp)
            if snapshot["verification"]["live"]["in_flight"]:
                break
            time.sleep(0.1)
        flight = snapshot["verification"]["live"]["in_flight"]
        self.assertIsNotNone(flight, "the in-flight record never became visible")
        self.assertEqual((flight["done"], flight["total"], flight["current"]), (0, 1, wait_cmd))
        self.assertEqual(snapshot["status"]["phase"], "LIVE VERIFICATION RUNNING · 0/1")
        self.assertEqual(self._fleet_state(), "running")
        gate.write_text("go")
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertFalse((self.tmp / lib.LIVE_INFLIGHT_FILE).exists())
        # A leftover record older than the checks timeout times its size is ignored.
        stale = self.tmp / lib.LIVE_INFLIGHT_FILE
        stale.write_text(json.dumps({"started_at": "2026-01-01T00:00:00+00:00", "by": "x", "total": 1, "done": 0,
                                     "current": "old", "results": []}))
        old = time.time() - 24 * 3600
        os.utime(stale, (old, old))
        self.assertIsNone(dashboard.build_snapshot(self.tmp)["verification"]["live"]["in_flight"])
        stale.unlink()

    def test_verify_live_removes_the_record_when_blocked_after_the_checks(self):
        self._phase_7(["true"])
        # Change the acceptance mid-run: the post-check gate refuses and the record is still removed.
        hold = self.tmp / ".handsoff-fixture" / "hold"
        hold.parent.mkdir(exist_ok=True)
        wait_cmd = self._script("wait_for_hold.py", (
            "import os, time\n"
            f"while not os.path.exists({str(hold)!r}):\n    time.sleep(0.1)\n"))
        self._set_live_commands([wait_cmd])
        toml = self.tmp / "handsoff.toml"
        # The review certifies its rules set and handsoff.toml is part of it
        # (#170): the edit above staled the review, so the same reviewer
        # re-binds it (#179, Lane A) before the deployment approval, which
        # binds the config hash, is given again.
        reaffirmed = run(["record-review", "--by", "reviewer-1", "--reaffirm", "--tests-executed", "yes",
                          "--symptom-reproduced", "not_applicable"], cwd=self.tmp)
        self.assertEqual(reaffirmed.returncode, 0, reaffirmed.stdout + reaffirmed.stderr)
        self.assertIn("rules set changed (handsoff.toml)", reaffirmed.stdout)
        reapproved = run(["deployment-gate", "--approve", "--by", "owner"], cwd=self.tmp)
        self.assertEqual(reapproved.returncode, 0, reapproved.stdout + reapproved.stderr)
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.setdefault("r", run(["verify-live", "--by", "runner"], cwd=self.tmp)), daemon=True)
        worker.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.tmp / lib.LIVE_INFLIGHT_FILE).exists():
            time.sleep(0.1)
        self.assertTrue((self.tmp / lib.LIVE_INFLIGHT_FILE).exists())
        # Policy changes while the checks run: the gate refuses the record.
        toml.write_text(toml.read_text().replace("stall_minutes = ", "stall_minutes = 9", 1))
        hold.write_text("go")
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["r"].returncode, 1)
        self.assertIn("changed during live verification", outcome["r"].stdout)
        self.assertFalse((self.tmp / lib.LIVE_INFLIGHT_FILE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
