"""#297: one canonical release preflight, run before anything expensive.

The v0.3.80 release changed runtime files without regenerating
`handsoff-runtime.json`. The existing integrity tests caught it, but only
after a five-shard matrix had already started, costing a whole CI cycle.
These tests pin the cheap gate that catches it first and names the repair.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = BIN.parent
sys.path.insert(0, str(BIN))

import handsoff_manifest  # noqa: E402
import handsoff_preflight as preflight  # noqa: E402


class PreflightFixture(unittest.TestCase):
    """A real engine checkout, copied so an edit cannot touch the repository."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "engine"
        self.root.mkdir()
        for relative in ("bin", "dashboard", "fleet", "playbook", "prompts",
                         "rules", "schemas", "templates"):
            shutil.copytree(ROOT / relative, self.root / relative)
        shutil.copy(ROOT / "pyproject.toml", self.root / "pyproject.toml")
        shutil.copy(ROOT / "handsoff-runtime.json", self.root / "handsoff-runtime.json")
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        self.addCleanup(self.dir.cleanup)

    def version(self) -> str:
        return (self.root / "pyproject.toml").read_text().split('version = "')[1].split('"')[0]

    def regenerate(self):
        subprocess.run([sys.executable, str(self.root / "bin" / "handsoff_manifest.py"),
                        "--root", str(self.root), "--version", f"v{self.version()}"],
                       check=True, capture_output=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.root / "bin" / "handsoff_preflight.py"),
                               "--root", str(self.root), *args],
                              capture_output=True, text=True)


class AFreshCheckoutPasses(PreflightFixture):

    def test_a_regenerated_manifest_passes_every_check(self):
        self.regenerate()
        report = preflight.preflight(self.root)
        self.assertTrue(report["ok"], report)
        self.assertEqual([item["name"] for item in report["checks"]],
                         ["runtime_manifest", "release_version", "schemas", "project_config"])

    def test_the_cli_exits_zero_and_says_so(self):
        self.regenerate()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_PREFLIGHT_OK", result.stdout)


class AnEditedRuntimeFileFails(PreflightFixture):

    def test_an_edited_runtime_file_is_named_as_stale(self):
        self.regenerate()
        target = self.root / "dashboard" / "app.js"
        target.write_text(target.read_text() + "\n// edited after the manifest\n")
        report = preflight.preflight(self.root)
        self.assertFalse(report["ok"])
        manifest = report["checks"][0]
        self.assertEqual(manifest["stale"], ["dashboard/app.js"])
        self.assertIn("stale", manifest["detail"])

    def test_the_cli_exits_non_zero_and_prints_the_exact_repair(self):
        self.regenerate()
        target = self.root / "bin" / "handsoff_lib.py"
        target.write_text(target.read_text() + "\n# edited after the manifest\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("bin/handsoff_lib.py", result.stdout)
        self.assertIn(f"python3 bin/handsoff_manifest.py --version v{self.version()}", result.stdout)
        self.assertIn("HANDSOFF_PREFLIGHT_FAILED", result.stdout)

    def test_a_playbook_edit_is_caught_although_it_reads_as_a_docs_only_change(self):
        """playbook/*.md are covered runtime files, but `changes` classifies
        any *.md as docs-only. The preflight is the only gate that sees it."""
        self.regenerate()
        target = self.root / "playbook" / "lessons.md"
        target.write_text(target.read_text() + "\n- a lesson added after the manifest\n")
        report = preflight.preflight(self.root)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["stale"], ["playbook/lessons.md"])

    def test_a_covered_file_that_is_absent_is_reported_separately_from_a_changed_one(self):
        self.regenerate()
        (self.root / "rules" / "reviewer-launch-phase-1.json").unlink()
        manifest = preflight.check_runtime_manifest(self.root)
        self.assertEqual(manifest["missing"], ["rules/reviewer-launch-phase-1.json"])
        self.assertEqual(manifest["stale"], [])

    def test_a_recorded_file_no_longer_covered_is_drift_too(self):
        self.regenerate()
        path = self.root / "handsoff-runtime.json"
        payload = json.loads(path.read_text())
        payload["files"]["bin/retired_module.py"] = "0" * 64
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        manifest = preflight.check_runtime_manifest(self.root)
        self.assertFalse(manifest["ok"])
        self.assertEqual(manifest["uncovered"], ["bin/retired_module.py"])


class TheOtherChecks(PreflightFixture):

    def test_a_version_the_manifest_disagrees_with_fails(self):
        self.regenerate()
        path = self.root / "handsoff-runtime.json"
        payload = json.loads(path.read_text())
        payload["version"] = "v0.0.1"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        check = preflight.check_release_version(self.root)
        self.assertFalse(check["ok"])
        self.assertIn("v0.0.1", check["detail"])
        self.assertIn(self.version(), check["detail"])

    def test_the_tag_form_and_the_bare_form_agree(self):
        self.regenerate()
        self.assertTrue(preflight.check_release_version(self.root)["ok"])

    def test_an_unparseable_schema_fails_with_its_path(self):
        (self.root / "schemas" / "status.schema.json").write_text("{not json")
        check = preflight.check_schemas(self.root)
        self.assertFalse(check["ok"])
        self.assertTrue(any("schemas/status.schema.json" in item for item in check["invalid"]))

    def test_an_unparseable_project_config_fails(self):
        (self.root / "handsoff.toml").write_text("[project\nname = broken")
        check = preflight.check_project_config(self.root)
        self.assertFalse(check["ok"])
        self.assertIn("not valid TOML", check["detail"])

    def test_an_absent_project_config_is_legal(self):
        (self.root / "handsoff.toml").unlink()
        self.assertTrue(preflight.check_project_config(self.root)["ok"])

    def test_a_missing_manifest_is_reported_rather_than_raising(self):
        (self.root / "handsoff-runtime.json").unlink()
        check = preflight.check_runtime_manifest(self.root)
        self.assertFalse(check["ok"])
        self.assertIn("missing", check["detail"])


class TheReportIsMachineReadable(PreflightFixture):

    def test_json_output_carries_every_check(self):
        self.regenerate()
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 4)

    def test_the_preflight_covers_itself(self):
        """A release tool outside the covered set could change unnoticed."""
        self.assertIn("bin/handsoff_preflight.py", handsoff_manifest.RUNTIME_FILES)


if __name__ == "__main__":
    unittest.main()
