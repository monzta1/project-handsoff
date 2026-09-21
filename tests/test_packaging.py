import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import ROOT, BIN, run, docs_text

sys.path.insert(0, str(BIN))
import handsoff_cli as cli
import handsoff_dashboard as dashboard
import handsoff_lib as lib

CURRENT_VERSION = json.loads((ROOT / "handsoff-runtime.json").read_text())["version"]


class VersionedRuntimeTests(unittest.TestCase):
    def setUp(self):
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
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
        result = cli.init_project(root, None)
        self.assertEqual(result["pin"], "0.3.*")
        self.assertTrue((root / "handsoff.toml").is_file())
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "0.3.*")
        for copied in ("bin", "dashboard", "fleet", "prompts", "schemas", "handsoff-runtime.json"):
            self.assertFalse((root / copied).exists(), copied)
        identity = lib.validate_runtime_integrity(root)
        self.assertEqual(identity["version"], CURRENT_VERSION)
        self.assertEqual(identity["source"], "installed-engine")
        self.assertEqual(identity["compatibility"], "0.3.*")
        self.assertNotIn("manifest", identity)
        self.assertRegex(identity["manifest_sha256"], r"^[0-9a-f]{64}$")
        diagnosis = cli.doctor(root)
        self.assertTrue(diagnosis["ok"])
        self.assertFalse(diagnosis["migration_required"])
        self.assertEqual(diagnosis["legacy_runtime_paths"], [])
        self.assertTrue(diagnosis["console_executable"])

    def test_incompatible_pin_refuses_before_any_agent_launch(self):
        root = self.base / "wrong-pin"
        cli.init_project(root, CURRENT_VERSION)
        (root / lib.VERSION_PIN_FILE).write_text("v9.0.0\n")
        with self.assertRaisesRegex(lib.HandsoffError, "requires Handsoff v9.0.0"):
            lib.validate_runtime_integrity(root)

    def test_upgrade_preview_change_and_rollback_preserve_prior_pin(self):
        root = self.base / "upgrade"
        cli.init_project(root, CURRENT_VERSION)
        preview = cli.change_pin(root, "0.3.*", dry_run=True, action="upgrade")
        self.assertEqual(preview["from"], CURRENT_VERSION)
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), CURRENT_VERSION)
        cli.change_pin(root, "0.3.*", dry_run=False, action="upgrade")
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), "0.3.*")
        rollback = cli.rollback_pin(root, dry_run=False)
        self.assertEqual(rollback["to"], CURRENT_VERSION)
        self.assertEqual((root / lib.VERSION_PIN_FILE).read_text().strip(), CURRENT_VERSION)

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
        diagnosis = cli.doctor(root)
        self.assertTrue(diagnosis["migration_required"])
        self.assertIn(str(root / "bin"), diagnosis["legacy_runtime_paths"])
        self.assertTrue((root / "bin").is_dir())
        self.assertIn("bin", preview["move"])
        applied = cli.migrate_project(root, dry_run=False)
        self.assertFalse((root / "bin").exists())
        self.assertTrue((Path(applied["backup"]) / "bin").is_dir())
        self.assertEqual((root / "handsoff-events.jsonl").read_bytes(), ledger)
        self.assertEqual(lib.validate_runtime_integrity(root)["source"], "installed-engine")
        self.assertEqual(dashboard.build_snapshot(root)["project"]["feature"], "Preserve this run")

    def test_doctor_preserves_project_owned_runtime_named_directory(self):
        root = self._drop_in("mixed-runtime")
        customer_file = root / "bin" / "customer-build-tool.py"
        customer_file.write_text("print('owned by project')\n", encoding="utf-8")
        custom_prompt = root / "prompts" / "project-specialist.md"
        custom_prompt.write_text("Project-owned specialist prompt.\n", encoding="utf-8")
        diagnosis = cli.doctor(root)
        bin_entry = next(item for item in diagnosis["runtime_paths"]
                         if item["path"] == str(root / "bin"))
        prompt_entry = next(item for item in diagnosis["runtime_paths"]
                            if item["path"] == str(root / "prompts"))
        self.assertEqual(bin_entry["classification"], "project-owned")
        self.assertEqual(prompt_entry["classification"], "project-owned")
        self.assertNotIn(str(root / "bin"), diagnosis["legacy_runtime_paths"])
        self.assertNotIn(str(root / "prompts"), diagnosis["legacy_runtime_paths"])
        preview = cli.migrate_project(root, dry_run=True)
        self.assertNotIn("bin", preview["move"])
        self.assertNotIn("prompts", preview["move"])
        applied = cli.migrate_project(root, dry_run=False)
        self.assertTrue(customer_file.is_file())
        self.assertTrue(custom_prompt.is_file())
        self.assertFalse((Path(applied["backup"]) / "bin").exists())
        self.assertFalse((Path(applied["backup"]) / "prompts").exists())
        overrides = json.loads((root / lib.OVERRIDES_FILE).read_text(encoding="utf-8"))
        self.assertNotIn("prompts/project-specialist.md", overrides["files"])
        self.assertIn("prompts/architect.md", overrides["files"])

    def test_doctor_documentation_audit_is_read_only_and_identity_driven(self):
        root = self.base / "documentation-audit"
        cli.init_project(root, None)
        readme = root / "README.md"
        readme.write_text(
            "Run python3 bin/handsoff_supervisor.py and download/v0.1.0.\n",
            encoding="utf-8",
        )
        before = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in root.rglob("*") if path.is_file()}
        with mock.patch.object(cli.shutil, "which", return_value="/opt/tools/handsoff"):
            diagnosis = cli.doctor(root)
        after = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(diagnosis["documentation"]["stale"])
        self.assertEqual(diagnosis["documentation"]["canonical_executable"], "/opt/tools/handsoff")
        self.assertEqual(diagnosis["documentation"]["installed_engine"], CURRENT_VERSION)
        self.assertEqual(diagnosis["documentation"]["supported_project_pin"], "0.3.*")
        self.assertEqual(
            [item["code"] for item in diagnosis["documentation"]["diagnostics"]],
            ["obsolete-release-reference", "obsolete-command-path"],
        )

    def test_doctor_clean_documentation_is_not_stale(self):
        root = self.base / "clean-documentation"
        cli.init_project(root, None)
        (root / "README.md").write_text("Run handsoff doctor .\n", encoding="utf-8")
        diagnosis = cli.doctor(root)
        self.assertFalse(diagnosis["documentation"]["stale"])
        self.assertEqual(diagnosis["documentation"]["diagnostics"], [])

    def test_doctor_flags_instruction_files_that_contradict_the_config(self):
        # #106: a SKILL.md claiming no toml and a different reviewer provider
        # than the resolved [agents] table is a contradiction, with the claim
        # and the resolved value named.
        root = self.base / "contradicting-docs"
        cli.init_project(root, None)
        # init leaves [agents] unset, so the reviewer resolves to the
        # recommended default, codex.
        self.assertEqual(lib.resolved_agent_profiles(lib.load_config(root))["reviewer"]["adapter"], "codex")
        (root / "SKILL.md").write_text(
            "This project has no handsoff.toml.\nThe reviewer is claude here.\n", encoding="utf-8")
        diagnostics = [d for d in cli.doctor(root)["documentation"]["diagnostics"]
                       if d["code"] == "config-claim-contradiction"]
        self.assertEqual(len(diagnostics), 2, diagnostics)
        details = " | ".join(d["detail"] for d in diagnostics)
        self.assertIn("no handsoff.toml; resolved: handsoff.toml", details)
        self.assertIn("reviewer is claude; resolved: codex", details)
        self.assertTrue(all(d["path"].endswith("SKILL.md") for d in diagnostics))

    def test_doctor_accepts_instruction_files_that_agree_with_the_config(self):
        root = self.base / "agreeing-docs"
        cli.init_project(root, None)
        (root / "SKILL.md").write_text(
            "This project uses handsoff.toml at the root.\nThe reviewer is codex.\n", encoding="utf-8")
        diagnostics = [d for d in cli.doctor(root)["documentation"]["diagnostics"]
                       if d["code"] == "config-claim-contradiction"]
        self.assertEqual(diagnostics, [])

    def test_documentation_files_limit_scan(self):
        root = self.base / "configured-files"
        cli.init_project(root, None)
        (root / "one.md").write_text("download/v0.1.0\n")
        (root / "two.md").write_text("download/v0.1.0\n")
        (root / "handsoff.toml").write_text("[documentation]\nfiles = ['one.md']\n")
        cfg = lib.load_config(root)
        self.assertEqual([p.name for p in cli._documentation_files(root, cfg)], ["one.md"])

    def test_documentation_exclude_glob_drops_matching_file(self):
        root = self.base / "configured-exclude"
        cli.init_project(root, None)
        (root / "skip.md").write_text("download/v0.1.0\n")
        (root / "keep.md").write_text("download/v0.1.0\n")
        (root / "handsoff.toml").write_text("[documentation]\nexclude = ['skip.md']\n")
        paths = cli._documentation_files(root, lib.load_config(root))
        self.assertEqual([p.name for p in paths], ["keep.md"])

    def test_intentional_marker_moves_finding_to_suppressed(self):
        root = self.base / "marker"
        cli.init_project(root, None)
        (root / "README.md").write_text("<!-- handsoff-doc: intentional -->\ndownload/v0.1.0\n")
        result = cli.doctor(root)["documentation"]
        self.assertFalse(result["stale"])
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["suppressed"][0]["code"], "obsolete-release-reference")

    def test_install_rollback_reference_is_suppressed(self):
        result = cli._documentation_diagnosis(ROOT, {"version": CURRENT_VERSION}, lib.load_config(ROOT))
        install = [item for item in result["diagnostics"] if item["path"] == str(ROOT / "INSTALL.md")]
        self.assertNotIn("obsolete-release-reference", [item["code"] for item in install])

    def test_repository_documentation_carries_no_obsolete_release_reference(self):
        """v0.3.25 field-note defect 3: every root and docs/ file must point at
        the release this tree declares (the INSTALL.md rollback example is
        suppressed by its intentional marker)."""
        result = cli._documentation_diagnosis(ROOT, {"version": CURRENT_VERSION}, lib.load_config(ROOT))
        obsolete = [(item["path"], item["detail"]) for item in result["diagnostics"]
                    if item["code"] == "obsolete-release-reference"]
        self.assertEqual(obsolete, [])
        self.assertIn(CURRENT_VERSION.lstrip("v"), (ROOT / "pyproject.toml").read_text())

    def test_release_identity_is_one_version_and_live_checked(self):
        """v0.3.25 field notes, REQ-006: the tree declares one release version in
        pyproject.toml, handsoff-runtime.json, INSTALL.md and README.md, and the
        published/installed identity is proved by a configured live check."""
        version = CURRENT_VERSION.lstrip("v")
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertRegex(pyproject, rf'(?m)^version = "{re.escape(version)}"$')
        wheel = f"releases/download/v{version}/project_handsoff-{version}-py3-none-any.whl"
        for name in ("INSTALL.md", "README.md"):
            text = (ROOT / name).read_text()
            self.assertIn(wheel, text, name)
            unmarked = [line for previous, line in zip([""] + text.splitlines(), text.splitlines())
                        if "releases/download/v" in line and wheel not in line
                        and previous.strip() not in {"handsoff-doc: intentional", "<!-- handsoff-doc: intentional -->"}]
            self.assertEqual(unmarked, [], f"{name} references another release without an intentional marker")
        live = lib.load_config(ROOT)["live_check_commands"]
        self.assertIn("python3 tests/live_release_smoke.py", live)
        self.assertIn("python3 tests/live_doctor_smoke.py", live)
        smoke = (ROOT / "tests" / "live_release_smoke.py").read_text()
        for check in ("ls-remote", "merge-base", "releases/tags/", "sha256", "version\", \"--json\"",
                      "zipfile", "manifest_sha256", "installed.read_bytes() == wheel.read(name)"):
            self.assertIn(check, smoke)
        self.assertNotIn('"fetch"', smoke, "the live release check must not mutate the checkout")

    def test_upgrade_docs_show_the_compatible_pin_as_the_default_path(self):
        """v0.3.25 field-note defects 2 and 3: the runbooks show the compatible
        line, the exact bump is the exception, and the README documents the
        field notes, the instruction-file guidance, and the release procedure."""
        install = (ROOT / "INSTALL.md").read_text()
        patch = install.split("## Clean patch upgrade", 1)[1].split("## After an upgrade", 1)[0]
        self.assertIn("--to 0.3.*", patch)
        self.assertLess(patch.index("--to 0.3.*"), patch.index("--to v"), "compatible pin must come first")
        self.assertIn("strict reproducibility", patch)
        self.assertIn("[digest] ignore", patch)
        for name in ("AGENTS.md", "SKILL.md"):
            self.assertIn(name, patch)
        proof = (ROOT / "docs" / "FIELD-PROOF.md").read_text()
        self.assertIn("--to X.Y.*", proof)
        self.assertNotIn("handsoff upgrade /abs/path/to/project --to vX.Y.Z", proof)
        readme = docs_text()
        self.assertIn("### v0.3.25 field notes", readme)
        self.assertIn("### Cutting a release", readme)
        release = readme.split("### Cutting a release", 1)[1].split("\n## ", 1)[0]
        for step in ("handsoff_manifest.py --version vX.Y.Z", "git tag -a vX.Y.Z", "git push origin main",
                     "git push origin vX.Y.Z", "python3 -m pip wheel --no-deps -w dist .", "gh release create vX.Y.Z"):
            self.assertIn(step, release)
        self.assertIn("cosmetic-only change", release)
        self.assertIn("PREFLIGHT_TOKEN_BUDGET", readme)

    def test_doctor_docs_only_returns_one_for_stale_and_zero_for_clean(self):
        root = self.base / "docs-only"
        cli.init_project(root, None)
        (root / "README.md").write_text("download/v0.1.0\n")
        with mock.patch.object(sys, "argv", ["handsoff", "doctor", str(root), "--docs-only"]), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as output:
            self.assertEqual(cli.main(), 1)
        self.assertRegex(output.getvalue().strip(), r"^.+:obsolete-release-reference:.+$")
        (root / "README.md").write_text("Run handsoff doctor .\n")
        with mock.patch.object(sys, "argv", ["handsoff", "doctor", str(root), "--docs-only"]), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as output:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(output.getvalue().strip(), "DOCUMENTATION_OK")

    def test_commands_reference_comes_from_both_argparse_trees(self):
        with mock.patch.object(sys, "argv", ["handsoff", "commands"]), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as output:
            self.assertEqual(cli.main(), 0)
        rendered = output.getvalue()
        self.assertIn("## doctor", rendered)
        self.assertIn("## advance", rendered)
        self.assertIn("--docs-only", rendered)

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


class StableDashboardCommandTests(unittest.TestCase):
    """#72: the stable `handsoff dashboard` command forwards --owned-by-run so
    Fleet can link to dashboards started through the installed CLI."""

    def test_owned_by_run_is_forwarded_to_the_supervisor(self):
        forwarded = []
        with mock.patch.object(cli, "_dispatch_supervisor", side_effect=lambda call: forwarded.append(call) or 0), \
                mock.patch.object(sys, "argv", ["handsoff", "dashboard", "--root", "/tmp/p", "--port", "8801",
                                                "--no-open", "--owned-by-run"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(forwarded, [["--root", "/tmp/p", "dashboard", "--host", "127.0.0.1", "--port", "8801",
                                      "--no-open", "--owned-by-run"]])


class PassthroughCommandTests(unittest.TestCase):
    """v0.3.14 regression: the supervisor/agent/fleet passthrough must return
    the child's exit code from main() and never reach argparse."""

    def test_supervisor_passthrough_returns_exit_code_from_main(self):
        with mock.patch.object(cli, "_dispatch_supervisor", return_value=3) as dispatch, \
                mock.patch.object(sys, "argv", ["handsoff", "supervisor", "--root", "/tmp/p", "status"]):
            self.assertEqual(cli.main(), 3)
        dispatch.assert_called_once_with(["--root", "/tmp/p", "status"])

    def test_build_parser_is_pure_even_with_passthrough_argv(self):
        with mock.patch.object(sys, "argv", ["handsoff", "supervisor", "status"]):
            parser = cli.build_parser()
        self.assertTrue(hasattr(parser, "parse_args"))


class WheelShipsEveryRuntimeFile(unittest.TestCase):
    """The manifest's RUNTIME_FILES and pyproject's shipped files are two
    hand-maintained lists. A runtime file added to one but not the other
    installs an engine whose doctor refuses every project ("runtime files
    do not match release"), which is exactly what v0.3.21's first wheel
    did with the Regression Console."""

    def test_pyproject_ships_every_manifest_runtime_file(self):
        import tomllib
        sys.path.insert(0, str(BIN))
        import handsoff_manifest as manifest
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        tool = pyproject["tool"]["setuptools"]
        shipped = {f"bin/{module}.py" for module in tool["py-modules"]}
        for files in tool["data-files"].values():
            shipped.update(files)
        missing = sorted(set(manifest.RUNTIME_FILES) - shipped)
        self.assertEqual(missing, [], f"runtime files the wheel would not ship: {missing}")
        unlisted = sorted(path for path in shipped if path.startswith("bin/") and path not in manifest.RUNTIME_FILES)
        self.assertEqual(unlisted, [], f"shipped modules the manifest does not guard: {unlisted}")
