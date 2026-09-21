"""#204: a stale runtime manifest refuses with the fix named. An engine
checkout whose bin/, prompts/ or dashboard/ changed after the manifest was
written says 'regenerate the manifest' from every path that reads it; a
thin project keeps its own wording."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

ROOT = BIN.parent


class StaleManifestTests(HandsoffTestCase):
    """The fixture is a runtime drop-in (bin/, schemas/, dashboard/ copied
    in); adding bin/handsoff_manifest.py and pyproject.toml makes it look
    like an engine checkout to the rule."""

    def _checkout(self):
        # the base fixture copies bin/, schemas/, dashboard/, rules/; an
        # engine checkout also has prompts/ and the project's pyproject
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        shutil.copy(ROOT / "pyproject.toml", self.tmp / "pyproject.toml")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def test_an_edited_runtime_file_refuses_every_reader_with_the_command(self):
        self._checkout()
        self.init("Stale manifest")
        self.assertIsNone(lib.stale_manifest_refusal(self.tmp), "in step: nothing to say")
        self.set_criterion_state("passing", resolved=True)
        target = self.tmp / "dashboard" / "app.js"
        target.write_text(target.read_text() + "\n// edited after the manifest\n")
        version = json.loads((self.tmp / "handsoff-runtime.json").read_text())["version"]
        pyproject = (ROOT / "pyproject.toml").read_text().split('version = "')[1].split('"')[0]
        line = lib.stale_manifest_refusal(self.tmp)
        self.assertEqual(line, f"the runtime manifest is stale (dashboard/app.js changed after it was written): "
                               f"run python3 bin/handsoff_manifest.py --version v{pyproject}, then retry")
        with self.assertRaisesRegex(lib.HandsoffError, "the runtime manifest is stale"):
            lib.validate_runtime_integrity(self.tmp)
        with self.assertRaisesRegex(lib.HandsoffError, "the runtime manifest is stale"):
            lib.project_resource_path(self.tmp, "prompts/reviewer.md")
        # the commands say it too: verify and the reviewer launch
        r = run(["verify", "--criterion", "REQ-001", "--by", "claude-host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("the runtime manifest is stale (dashboard/app.js changed", r.stdout + r.stderr)
        self.assertNotIn("reinstall the Handsoff engine", r.stdout + r.stderr)
        self.assertNotIn("is not declared in", r.stdout + r.stderr)
        launch = subprocess.run([sys.executable, str(BIN / "handsoff_agent.py"), "--root", str(self.tmp), "launch", "reviewer",
                                 "--by", "codex-reviewer", "--task", "t"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(launch.returncode, 0)
        self.assertIn("the runtime manifest is stale", launch.stdout + launch.stderr)
        # more than six changed files are counted, not listed
        for name in ("a", "b", "c", "d", "e", "f", "g"):
            (self.tmp / "bin" / f"{name}.py").write_text("x")
        manifest = json.loads((self.tmp / "handsoff-runtime.json").read_text())
        for name in ("a", "b", "c", "d", "e", "f", "g"):
            manifest["files"][f"bin/{name}.py"] = "0" * 64
        (self.tmp / "handsoff-runtime.json").write_text(json.dumps(manifest))
        self.assertIn("(+2 more)", lib.stale_manifest_refusal(self.tmp))
        # regenerating clears it (the generator writes the manifest for this root)
        gen = subprocess.run([sys.executable, str(self.tmp / "bin" / "handsoff_manifest.py"), "--version", version],
                             cwd=str(self.tmp), capture_output=True, text=True, timeout=60)
        self.assertEqual(gen.returncode, 0, gen.stdout + gen.stderr)
        self.assertIsNone(lib.stale_manifest_refusal(self.tmp))
        lib.validate_runtime_integrity(self.tmp)
        r = run(["verify", "--criterion", "REQ-001", "--by", "claude-host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_thin_project_keeps_its_own_wording(self):
        # no bin/handsoff_manifest.py: a drop-in, not an engine checkout
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Thin project")
        self.assertIsNone(lib.stale_manifest_refusal(self.tmp))
        target = self.tmp / "dashboard" / "app.js"
        target.write_text(target.read_text() + "\n// edited\n")
        self.assertIsNone(lib.stale_manifest_refusal(self.tmp), "not an engine checkout: not this rule's business")
        with self.assertRaisesRegex(lib.HandsoffError, "refresh the complete release drop-in"):
            lib.validate_runtime_integrity(self.tmp)


if __name__ == "__main__":
    unittest.main()
