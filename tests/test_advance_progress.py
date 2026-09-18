"""REQ-003 focused checks for monotonic progress and bounded phase changes."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_handsoff_supervisor import HandsoffTestCase, approve_design_review, run  # noqa: E402
import json  # noqa: E402


class AdvanceProgressTests(HandsoffTestCase):
    def _start(self):
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "Progress criterion"], cwd=self.tmp).returncode, 0)

    def test_omitted_progress_keeps_value(self):
        self._start()
        self.assertEqual(run(["advance", "2", "40"], cwd=self.tmp).returncode, 0)
        result = run(["advance", "2"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.read_status()["progress"], 40)

    def test_lower_progress_is_clamped_with_note(self):
        self._start()
        run(["advance", "2", "40"], cwd=self.tmp)
        result = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("SHIP_FEATURE_ADVANCED", result.stdout)
        self.assertEqual(self.read_status()["progress"], 40)

    def test_new_design_round_does_not_lower_progress(self):
        self._start()
        run(["advance", "2", "60"], cwd=self.tmp)
        result = run(["advance", "2", "--new-design-round"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.read_status()["progress"], 60)

    def test_phase_rule_remains_one_step(self):
        self._start()
        result = run(["advance", "4", "20"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("one step at a time", result.stdout)

    # #102: progress follows cleared gates when no value is given.
    def test_approved_design_reads_25_not_the_phase_weight(self):
        self._start()
        self.assertEqual(run(["advance", "2"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["progress"], 5)
        self.assertEqual(approve_design_review(self.tmp).returncode, 0)
        approved = run(["design-approve", "--by", "pilot", "--architect", "test-architect", "--summary", "ok"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertEqual(run(["advance", "3"], cwd=self.tmp).returncode, 0)
        status = self.read_status()
        self.assertEqual(status["progress"], 25)
        shown = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(shown["gate_progress"]["percent"], 25)
        self.assertEqual(shown["gate_progress"]["cleared"], ["initialized", "design_reviewed", "design_approved"])

    def test_evidenced_run_reads_at_least_50(self):
        self.init("Gates")
        self.set_criterion_state("passing", resolved=True)
        shown = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertGreaterEqual(shown["gate_progress"]["percent"], 50)
        self.assertIn("evidence", shown["gate_progress"]["cleared"])
        self.assertIn("symptom", shown["gate_progress"]["cleared"])

    def test_explicit_progress_is_still_honoured(self):
        self._start()
        self.assertEqual(run(["advance", "2", "70"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["progress"], 70)
        self.assertEqual(run(["advance", "2"], cwd=self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["progress"], 70)


if __name__ == "__main__":
    unittest.main()
