"""#284 stage 1: the core layer, and the contracts a move can silently drop.

`bin/handsoff_core.py` holds the thirteen primitives with zero outbound
dependencies on the engine: the error type (121 inbound callers), canonical
JSON form, atomic and durable writes, the project lock, the status and
acceptance paths, and the three content hashes that bind evidence.

It exists so the layers above it can import at module level. The first
extraction (model routing) had to defer its monolith imports to avoid a
cycle; primitives the core owns are ordinary imports now.

The fcntl test below is here because extracting a guarded import is the
easy way to break a documented degradation. The monolith wraps `import
fcntl` in try/except so `project_lock` becomes a no-op where it is absent
(docs/REFERENCE.md, "Known limitations"). The first draft of the core used
a bare import, which would have made every module that imports the core
die on import instead, while leaving the `if fcntl is None` branch as dead
code claiming a graceful degradation it no longer performed.
"""
import ast
import builtins
import pathlib
import sys
import tempfile
import unittest

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_core as core  # noqa: E402
import handsoff_lib as lib  # noqa: E402

CORE_SOURCE = BIN / "handsoff_core.py"


class TheCoreDependsOnNothingInTheEngine(unittest.TestCase):
    """That property is what lets everything above import it at module level."""

    def setUp(self):
        self.tree = ast.parse(CORE_SOURCE.read_text(encoding="utf-8"))

    def test_it_imports_no_handsoff_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("handsoff"):
                self.fail(f"core imports {node.module}; it must depend on nothing in the engine")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("handsoff"),
                                     f"core imports {alias.name}")

    def test_it_imports_standalone_without_the_monolith(self):
        """Proved by importing it in a subprocess that never loads the lib."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import handsoff_core as c; "
             "assert 'handsoff_lib' not in sys.modules; print(c.HandsoffError.__name__)" % str(BIN)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HandsoffError", result.stdout)

    def test_the_monolith_reexports_every_core_symbol(self):
        exported = set()
        tree = ast.parse((BIN / "handsoff_lib.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_core":
                exported |= {a.asname or a.name for a in node.names}
        self.assertGreaterEqual(len(exported), 13)
        for name in sorted(exported):
            self.assertTrue(hasattr(lib, name), f"handsoff_lib.{name} no longer resolves")
            self.assertTrue(hasattr(core, name), f"handsoff_core.{name} no longer resolves")


class TheGuardedImportSurvivedTheMove(unittest.TestCase):
    """The contract a bare import would have dropped."""

    def test_fcntl_is_imported_under_a_guard(self):
        guarded = [
            alias.name
            for node in ast.walk(ast.parse(CORE_SOURCE.read_text(encoding="utf-8")))
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
            for stmt in node.body if isinstance(stmt, (ast.Import, ast.ImportFrom))
            for alias in stmt.names
        ]
        self.assertIn("fcntl", guarded,
                      "a bare fcntl import makes every importer of the core die on "
                      "a platform without it, instead of degrading project_lock")

    def test_the_core_imports_and_locks_as_a_no_op_without_fcntl(self):
        """Simulates the platform the guard exists for, rather than trusting it."""
        real_import = builtins.__import__

        def without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("simulated non-POSIX platform")
            return real_import(name, *args, **kwargs)

        saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith("handsoff")}
        builtins.__import__ = without_fcntl
        try:
            for name in list(sys.modules):
                if name.startswith("handsoff"):
                    del sys.modules[name]
            import handsoff_core as reloaded
            self.assertIsNone(reloaded.fcntl, "the guard did not take effect")
            root = pathlib.Path(tempfile.mkdtemp())
            with reloaded.project_lock(root):
                pass  # a no-op lock, not an exception
        finally:
            builtins.__import__ = real_import
            for name in list(sys.modules):
                if name.startswith("handsoff"):
                    del sys.modules[name]
            sys.modules.update(saved)

    def test_project_lock_still_documents_the_limitation(self):
        doc = core.project_lock.__doc__ or ""
        self.assertIn("no-op", doc)
        self.assertTrue("fcntl" in doc or "POSIX" in doc)


class TheCoreIsRegisteredWhereItMustBe(unittest.TestCase):
    """#325: the file set lives in more than one registry."""

    def test_the_runtime_manifest_covers_it(self):
        self.assertIn("bin/handsoff_core.py",
                      (BIN / "handsoff_manifest.py").read_text(encoding="utf-8"))

    def test_the_wheel_packages_it(self):
        self.assertIn("handsoff_core",
                      (BIN.parent / "pyproject.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
