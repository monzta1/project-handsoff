import hashlib
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
from tests.fixture_state import compatible_pin


class PromptOverrideTests(unittest.TestCase):
    def setUp(self):
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-overrides-"))
        cli.init_project(self.root, None)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def declare(self, path):
        lib.atomic_write_json(self.root / lib.OVERRIDES_FILE, {"schema": 1, "files": {
            path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()}})

    def test_stale_protocol_flips_doctor_and_upgrade_refuses(self):
        path = "prompts/reviewer.md"
        (self.root / path).parent.mkdir()
        (self.root / path).write_text("custom reviewer prompt\n")
        self.declare(path)
        diagnosis = cli.doctor(self.root)
        self.assertFalse(diagnosis["ok"])
        self.assertIn("HANDSOFF_REVIEW_RESULT:", diagnosis["warnings"][0])
        old = (self.root / lib.VERSION_PIN_FILE).read_text()
        self.assertIn("prompt_overrides", cli.change_pin(self.root, compatible_pin(), dry_run=True, action="upgrade"))
        with self.assertRaises(lib.HandsoffError):
            cli.change_pin(self.root, compatible_pin(), dry_run=False, action="upgrade")
        self.assertEqual((self.root / lib.VERSION_PIN_FILE).read_text(), old)

    def test_current_and_undeclared_states(self):
        path = "prompts/reviewer.md"
        (self.root / path).parent.mkdir()
        (self.root / path).write_text("HANDSOFF_REVIEW_RESULT: custom\n")
        self.declare(path)
        self.assertEqual(lib.prompt_override_diagnosis(self.root)[0]["state"], "declared_current")
        other = self.root / "prompts" / "implementer.md"
        other.write_text("custom\n")
        self.assertTrue(any(x["state"] == "undeclared" for x in lib.prompt_override_diagnosis(self.root)))
        with self.assertRaisesRegex(lib.HandsoffError, "not declared"):
            lib.project_resource_path(self.root, "prompts/implementer.md")

    def test_drop_in_root_has_nothing_to_declare(self):
        # The engine checkout ships its own prompts; they are not overrides.
        self.assertEqual(lib.prompt_override_diagnosis(BIN.parent), [])

    def test_current_override_keeps_doctor_ok(self):
        target = self.root / "prompts" / "reviewer.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text(lib.engine_resource_path("prompts/reviewer.md").read_text(encoding="utf-8"), encoding="utf-8")
        self.declare("prompts/reviewer.md")
        diagnosis = cli.doctor(self.root)
        self.assertTrue(diagnosis["ok"])
        self.assertEqual([w for w in diagnosis["warnings"] if w.startswith("override-")], [])


if __name__ == "__main__":
    unittest.main()
