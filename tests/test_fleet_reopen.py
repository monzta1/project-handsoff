"""#432: a reopened run reads open in Fleet.

run-close writes `state: closed` on the run's register entry and run-reopen
leaves the register alone, so before #432 `fleet register` kept answering
closed for a run whose status was in progress again. Fleet now derives the
record's state from the run's own status whenever it registers or builds.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, _harness_env, run

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402


def fleet_cli(*args):
    return subprocess.run([sys.executable, str(BIN / "handsoff_fleet.py"), *args],
                          capture_output=True, text=True, timeout=60, env=_harness_env())


class FleetReopenTests(HandsoffTestCase):
    def registry_state(self):
        entries = {entry["root"]: entry for entry in fleet.load_registry()}
        return entries[str(self.tmp.resolve())].get("state")

    def close(self):
        result = run(["run-close", "--by", "tester", "--reason", "done"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def reopen(self):
        result = run(["run-reopen", "--by", "tester", "--reason", "again"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_close_then_reopen_then_register_reads_open(self):
        self.init("Reopen and register")
        self.close()
        self.assertEqual(self.registry_state(), "closed")
        self.reopen()
        self.assertEqual(self.registry_state(), "closed", "run-reopen itself does not touch the register")
        result = fleet_cli("register", str(self.tmp))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "open")
        self.assertEqual(self.registry_state(), "open")

    def test_a_reopened_run_reads_open_on_the_next_fleet_refresh_without_registering(self):
        self.init("Reopen and refresh")
        self.close()
        self.reopen()
        self.assertEqual(self.registry_state(), "closed")
        fleet._build_fleet(cache=fleet.ProjectViewCache())
        self.assertEqual(self.registry_state(), "open")

    def test_a_closed_run_still_reads_closed(self):
        self.init("Stays closed")
        self.close()
        self.assertEqual(json.loads(fleet_cli("register", str(self.tmp)).stdout)["state"], "closed")
        fleet._build_fleet(cache=fleet.ProjectViewCache())
        self.assertEqual(self.registry_state(), "closed")

    def test_a_complete_run_still_reads_complete(self):
        self.init("Complete run")
        status_file = self.tmp / "handsoff-status.json"
        status = json.loads(status_file.read_text())
        status["status"], status["run_closed"] = "complete", None
        status_file.write_text(json.dumps(status))
        fleet.note_registry_state(self.tmp, None, "complete")
        self.assertEqual(json.loads(fleet_cli("register", str(self.tmp)).stdout)["state"], "complete")
        fleet._build_fleet(cache=fleet.ProjectViewCache())
        self.assertEqual(self.registry_state(), "complete")

    def test_a_root_without_a_run_keeps_its_stored_claim(self):
        # The claim grace (#166): a register entry written before init's
        # status file exists must not be overwritten by 'none'.
        fleet.note_registry_state(self.tmp, {7}, "open")
        self.assertFalse((self.tmp / "handsoff-status.json").exists())
        self.assertEqual(fleet.register_project(self.tmp)["state"], "open")
        self.assertEqual(fleet.sync_registry_states({str(self.tmp.resolve()): {"state": "none"}}), [])


if __name__ == "__main__":
    unittest.main()
