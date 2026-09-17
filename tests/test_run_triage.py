import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import handsoff_cli as cli
import handsoff_lib as lib


class RunTriageTests(unittest.TestCase):
    def setUp(self):
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-triage-"))
        cli.init_project(self.root, None)
        self.cfg = lib.load_config(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_blocked_run_has_three_rooted_options(self):
        (self.root / self.cfg["status_file"]).write_text(json.dumps({
            "phase_number": 4, "status": "blocked", "escalation": {"kind": "pilot_escalation"}}))
        result = lib.run_triage(self.root, self.cfg)
        self.assertEqual([x["action"] for x in result["options"]], ["recover", "close", "reopen"])
        for option in result["options"]:
            self.assertIn(str(self.root.resolve()), option["command"])

    def test_complete_run_is_not_triaged(self):
        (self.root / self.cfg["status_file"]).write_text(json.dumps({"phase_number": 8, "status": "complete"}))
        self.assertIsNone(lib.run_triage(self.root, self.cfg))
        self.assertIsNone(cli.doctor(self.root)["run_triage"])


if __name__ == "__main__":
    unittest.main()
