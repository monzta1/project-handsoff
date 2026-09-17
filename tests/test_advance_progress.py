"""REQ-003 focused checks for monotonic progress and bounded phase changes."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_handsoff_supervisor import HandsoffTestCase, run  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
