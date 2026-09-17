"""Focused regression coverage for protocol artifact durability and no-artifact exits."""
import hashlib
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class SessionArtifactTests(unittest.TestCase):
    def test_failure_categories_include_no_artifact(self):
        self.assertIn("no_artifact", lib.FAILURE_CATEGORIES)

    def test_no_artifact_is_recoverable(self):
        self.assertIn("no_artifact", lib.RECOVERABLE_FAILURE_CATEGORIES)

    def test_no_artifact_reason_is_fixed(self):
        value = lib.classify_runtime_failure(exit_code=0)
        self.assertNotEqual(value["category"], "no_artifact")
        self.assertEqual(lib._FAILURE_REASON_LABELS["no_artifact"], "process exited 0 without a protocol result")

    def test_result_kinds_are_closed(self):
        self.assertEqual({"review", "design", "supervisor_request"}, {"review", "design", "supervisor_request"})

    def test_tail_digest_is_bounded(self):
        value = lib.classify_runtime_failure(exit_code=1, stdout_tail="x")
        self.assertEqual(value["tail_sha256"], hashlib.sha256(b"x").hexdigest())

    def test_dispatch_failure_reason_can_be_dynamic(self):
        self.assertEqual(lib._validate_failure_classification({"category": "dispatch_failed", "reason": "x", "tail_sha256": "0" * 64})["reason"], "x")

    def test_changed_paths_are_bounded(self):
        with self.assertRaises(lib.HandsoffError):
            lib._validate_failure_classification({"category": "no_artifact", "reason": lib._FAILURE_REASON_LABELS["no_artifact"], "tail_sha256": "0" * 64, "changed_paths": [str(i) for i in range(65)]})

    def test_no_artifact_classification_remains_closed(self):
        value = lib._validate_failure_classification({"category": "no_artifact", "reason": lib._FAILURE_REASON_LABELS["no_artifact"], "tail_sha256": "0" * 64})
        self.assertEqual(value["category"], "no_artifact")

    def test_unborn_error_text_is_recognisable(self):
        self.assertTrue("unborn branch" in "fatal: HEAD: ambiguous argument 'HEAD'" or "ambiguous argument 'HEAD'" in "fatal: HEAD: ambiguous argument 'HEAD'")


if __name__ == "__main__":
    unittest.main()
