"""#284 stage 3: the evidence ledger, and the bytes it must not change.

`bin/handsoff_ledger.py` holds the append-only event log, the verification
log, the repository digest and the evidence-drift check. Seventy-six
symbols in the monolith call into it, so it is the first extraction where
re-export carries real weight rather than hiding a rename.

**Why the fixture comes from git rather than from a capture taken here.**
The values in `tests/fixtures/ledger_contract_pre_extraction.json` were
produced by importing `bin/handsoff_lib.py` as it exists on `origin/main`,
before any of these symbols moved. Recording them from the current tree
would compare the new module against itself and pass whatever it did.

This module exists because the ledger's docstring claimed it before it was
written. That claim was a safety net asserted and not built, which is the
same defect this project keeps finding in its own criteria: a statement
wider than anything that checks it.
"""
import ast
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_ledger as ledger  # noqa: E402
import handsoff_lib as lib  # noqa: E402

LEDGER_SOURCE = BIN / "handsoff_ledger.py"
CONTRACT = pathlib.Path(__file__).parent / "fixtures" / "ledger_contract_pre_extraction.json"

#: The layers the ledger may import. Each sits below it, so none can cycle.
ALLOWED_ENGINE_IMPORTS = {"handsoff_core", "handsoff_routing", "handsoff_config"}


class ThePersistedShapeIsUnchanged(unittest.TestCase):
    """Criterion 4 of #284, checked against the code that was replaced."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def _now(self):
        root = pathlib.Path(tempfile.mkdtemp())
        crit = [{"id": "REQ-001", "requirement": "x", "verification": "automated",
                 "tests": ["t"], "type": "primary_fix"},
                {"id": "REQ-002", "requirement": "y", "verification": "manual",
                 "tests": [], "type": "supporting"}]
        out = {
            "repository_digest_empty": lib.repository_digest(root, lib.DEFAULT_CONFIG),
            "event_log_name": lib.event_log_path(root, lib.DEFAULT_CONFIG).name,
            "verification_log_name": lib.verification_log_path(root, lib.DEFAULT_CONFIG).name,
            "acceptance_hash_empty": lib.acceptance_hash([]),
            "acceptance_hash_two": lib.acceptance_hash(crit),
            "criterion_spec_hash": lib.criterion_spec_hash(crit[0]),
            "design_hash_two": lib.design_hash(crit),
            "design_hash_empty": lib.design_hash([]),
        }
        (root / "a.txt").write_text("hello\n")
        out["repository_digest_one_file"] = lib.repository_digest(root, lib.DEFAULT_CONFIG)
        return out

    def test_the_fixture_was_taken_before_the_extraction(self):
        """A fixture regenerated after the move would prove nothing, so this
        checks it against the file git still holds."""
        self.assertTrue(CONTRACT.is_file())
        self.assertGreaterEqual(len(self.baseline), 9)
        result = subprocess.run(["git", "show", "origin/main:bin/handsoff_lib.py"],
                                cwd=BIN.parent, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("origin/main unavailable")
        for name in ("repository_digest", "acceptance_hash", "design_hash"):
            self.assertIn(f"def {name}", result.stdout,
                          f"{name} was not in the pre-extraction module; the fixture is not a baseline")

    def test_every_hash_and_digest_is_byte_identical(self):
        now = json.loads(json.dumps(self._now(), sort_keys=True, default=str))
        differing = {k: (self.baseline[k], now.get(k))
                     for k in self.baseline if self.baseline[k] != now.get(k)}
        self.assertEqual(differing, {},
                         "the extraction changed a persisted value (case: before, after)")

    def test_the_digest_still_distinguishes_trees(self):
        """Guards against a baseline where both sides recorded the same error."""
        self.assertNotEqual(self.baseline["repository_digest_empty"],
                            self.baseline["repository_digest_one_file"])

    def test_the_hashes_still_distinguish_criteria(self):
        self.assertNotEqual(self.baseline["acceptance_hash_empty"],
                            self.baseline["acceptance_hash_two"])
        self.assertNotEqual(self.baseline["design_hash_empty"],
                            self.baseline["design_hash_two"])


class TheLedgerSitsOnTheLayersBelowIt(unittest.TestCase):
    """The layering, derived from the source."""

    def setUp(self):
        self.tree = ast.parse(LEDGER_SOURCE.read_text(encoding="utf-8"))

    def test_it_imports_only_layers_beneath_it(self):
        offenders = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("handsoff"):
                if node.module not in ALLOWED_ENGINE_IMPORTS:
                    offenders.append(node.module)
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names
                              if a.name.startswith("handsoff") and a.name not in ALLOWED_ENGINE_IMPORTS]
        self.assertEqual(sorted(set(offenders)), [],
                         "the ledger may import only the layers below it")

    def test_it_never_imports_the_monolith(self):
        """That would close a cycle: handsoff_lib re-exports this module."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "handsoff_lib")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "handsoff_lib")

    def test_nothing_is_deferred_into_a_function_body(self):
        """Stage 1 removed the need for that workaround; it must not return."""
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    name = getattr(inner, "module", None) or inner.names[0].name
                    self.assertFalse(str(name).startswith("handsoff"),
                                     f"{node.name} defers an engine import; the layers below "
                                     "make that unnecessary")

    def test_the_monolith_reexports_the_whole_set(self):
        exported = set()
        tree = ast.parse((BIN / "handsoff_lib.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_ledger":
                exported |= {a.asname or a.name for a in node.names}
        self.assertGreaterEqual(len(exported), 50, "the re-export surface shrank")
        for name in sorted(exported):
            self.assertTrue(hasattr(lib, name), f"handsoff_lib.{name} no longer resolves")
            self.assertTrue(hasattr(ledger, name), f"handsoff_ledger.{name} no longer resolves")


class TheModuleIsRegisteredWhereItMustBe(unittest.TestCase):
    def test_the_runtime_manifest_covers_it(self):
        self.assertIn("bin/handsoff_ledger.py",
                      (BIN / "handsoff_manifest.py").read_text(encoding="utf-8"))

    def test_the_wheel_packages_it(self):
        self.assertIn("handsoff_ledger",
                      (BIN.parent / "pyproject.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
