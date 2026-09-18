"""#111: test fixtures never depend on files that only exist on one machine.

The version pin (.handsoff-version) is not tracked in the repository; a
fixture that copied it from the checkout root passed on a machine where a
run had been initialised there and failed with FileNotFoundError on a fresh
clone. Every fixture writes the pin itself, and this guard keeps it so."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

FORBIDDEN = (
    re.compile(r'ROOT\s*/\s*"\.handsoff-version"'),
    re.compile(r'BIN\.parent\s*/\s*"\.handsoff-version"'),
    re.compile(r'shutil\.copy\([^)]*"\.handsoff-version"'),
)


class FixtureHygieneTests(unittest.TestCase):
    def test_no_test_reads_the_pin_from_the_repository_root(self):
        offenders = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if any(pattern.search(line) for pattern in FORBIDDEN):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "fixtures must write the pin themselves:\n" + "\n".join(offenders))

    def test_fixtures_that_need_the_pin_write_it(self):
        # The five modules named in #111 each write the literal pin now.
        for name in ("test_design_convergence", "test_design_review_hold", "test_mission_control_ops",
                     "test_reviewer_sandbox", "test_handsoff_supervisor"):
            text = (ROOT / "tests" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn('".handsoff-version").write_text("0.3.*\\n")', text, name)


class RuntimeManifestTests(unittest.TestCase):
    def test_manifest_matches_the_working_tree(self):
        # A commit that edits a runtime file without regenerating the
        # manifest ships an engine that fails its own integrity check (main
        # at 5834701 did exactly that). Regenerate with
        # `python3 bin/handsoff_manifest.py --root . --version vX.Y.Z`.
        import json
        import handsoff_manifest as manifest_module
        manifest = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))
        # The file set must match the builder's RUNTIME_FILES exactly, so a
        # manifest that predates a newly added runtime file is stale too.
        expected = manifest_module.payload(ROOT, manifest["version"])["files"]
        self.assertEqual(sorted(manifest["files"]), sorted(expected),
                         "regenerate handsoff-runtime.json; the file set differs from RUNTIME_FILES")
        stale = sorted(relative for relative in expected if manifest["files"].get(relative) != expected[relative])
        self.assertEqual(stale, [], "regenerate handsoff-runtime.json; stale entries: " + ", ".join(stale))
        # Every runtime file the manifest names exists; the builder raises otherwise.
        for relative in expected:
            self.assertTrue((ROOT / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
