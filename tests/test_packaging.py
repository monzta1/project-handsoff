import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import ROOT, BIN, run

sys.path.insert(0, str(BIN))
import handsoff_cli as cli
import handsoff_dashboard as dashboard
import handsoff_lib as lib


class VersionedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-package-test-"))
        self.old_archive = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = str(self.base / "archive")

    def tearDown(self):
        if self.old_archive is None:
            os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        else:
            os.environ["HANDSOFF_ARCHIVE_DIR"] = self.old_archive
        shutil.rmtree(self.base, ignore_errors=True)

    def test_thin_init_has_only_config_and_pin_and_reports_exact_engine(self):
        root = self.base / "thin-one"
        result = cli.init_project(root, "0.3.*")
        self.assertEqual(result["pin"], "0.3.*")
        self.assertTrue((root / "handsoff.toml").is_file())
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "0.3.*")
        for copied in ("bin", "dashboard", "fleet", "prompts", "schemas", "handsoff-runtime.json"):
            self.assertFalse((root / copied).exists(), copied)
        identity = lib.validate_runtime_integrity(root)
        self.assertEqual(identity["version"], "v0.3.5")
        self.assertEqual(identity["source"], "installed-engine")
        self.assertEqual(identity["compatibility"], "0.3.*")
        self.assertNotIn("manifest", identity)
        self.assertRegex(identity["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(cli.doctor(root)["ok"])

    def test_incompatible_pin_refuses_before_any_agent_launch(self):
        root = self.base / "wrong-pin"
        cli.init_project(root, "v0.3.5")
        (root / lib.VERSION_PIN_FILE).write_text("v9.0.0\n")
        with self.assertRaisesRegex(lib.HandsoffError, "requires Handsoff v9.0.0"):
            lib.validate_runtime_integrity(root)

    def test_upgrade_preview_change_and_rollback_preserve_prior_pin(self):
        root = self.base / "upgrade"
        cli.init_project(root, "v0.3.5")
        preview = cli.change_pin(root, "0.3.*", dry_run=True, action="upgrade")
        self.assertEqual(preview["from"], "v0.3.5")
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "v0.3.5")
        cli.change_pin(root, "0.3.*", dry_run=False, action="upgrade")
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "0.3.*")
        rollback = cli.rollback_pin(root, dry_run=False)
        self.assertEqual(rollback["to"], "v0.3.5")
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "v0.3.5")

    def _drop_in(self, name="legacy"):
        root = self.base / name
        manifest = json.loads((ROOT / "handsoff-runtime.json").read_text())
        for relative in manifest["files"]:
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        shutil.copy2(ROOT / "handsoff-runtime.json", root / "handsoff-runtime.json")
        shutil.copy2(ROOT / "handsoff.toml", root / "handsoff.toml")
        return root

    def test_migration_preview_and_execution_preserve_ledgers_and_backup_runtime(self):
        root = self._drop_in()
        initialized = run(["init", "Preserve this run"], root)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        ledger = (root / "handsoff-events.jsonl").read_bytes()
        preview = cli.migrate_project(root, dry_run=True)
        self.assertTrue((root / "bin").is_dir())
        self.assertIn("bin", preview["move"])
        applied = cli.migrate_project(root, dry_run=False)
        self.assertFalse((root / "bin").exists())
        self.assertTrue((Path(applied["backup"]) / "bin").is_dir())
        self.assertEqual((root / "handsoff-events.jsonl").read_bytes(), ledger)
        self.assertEqual(lib.validate_runtime_integrity(root)["source"], "installed-engine")
        self.assertEqual(dashboard.build_snapshot(root)["project"]["feature"], "Preserve this run")

    def test_project_prompt_override_is_explicit_and_hash_bound(self):
        root = self.base / "override"
        cli.init_project(root, None)
        prompt = root / "prompts" / "architect.md"
        prompt.parent.mkdir()
        prompt.write_text("Custom architect instructions\n")
        with self.assertRaisesRegex(lib.HandsoffError, "not declared"):
            lib.project_resource_path(root, "prompts/architect.md")
        digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
        lib.atomic_write_json(root / lib.OVERRIDES_FILE,
                              {"schema": 1, "files": {"prompts/architect.md": digest}})
        self.assertEqual(lib.project_resource_path(root, "prompts/architect.md"), prompt.resolve())
        prompt.write_text("silently changed\n")
        with self.assertRaisesRegex(lib.HandsoffError, "hash mismatch"):
            lib.project_resource_path(root, "prompts/architect.md")

    def test_two_thin_projects_keep_independent_mutable_state(self):
        first, second = self.base / "first", self.base / "second"
        cli.init_project(first, None)
        cli.init_project(second, None)
        self.assertEqual(run(["init", "First feature"], first).returncode, 0)
        self.assertEqual(run(["init", "Second feature"], second).returncode, 0)
        one = json.loads((first / "handsoff-status.json").read_text())
        two = json.loads((second / "handsoff-status.json").read_text())
        self.assertEqual(one["feature"], "First feature")
        self.assertEqual(two["feature"], "Second feature")
        self.assertNotEqual((first / "handsoff-events.jsonl").read_bytes(),
                            (second / "handsoff-events.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()
