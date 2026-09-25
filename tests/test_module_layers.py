"""#284: every extracted module resolves, layers downward, and is registered.

One test per extraction does not scale, and the per-module suites each
check a different subset. This checks the properties that must hold for
*all* of them, derived from the source so a new module is covered the
moment it appears in `LAYERS`.

The unresolved-name check exists because stage 4 shipped a `NameError`.
The extractor reported `PREFLIGHT_FILE` as unresolved, I wrote imports for
the other names in that list and skipped it, and 28 suites failed on a
module that imported cleanly and only broke when the function was called.
A tool that reports a problem is not a check that the problem is fixed.
"""
import ast
import builtins
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))

#: Extracted modules, lowest layer first. A module may import any layer
#: BELOW it and nothing above, which is what keeps the graph acyclic.
LAYERS = [
    "handsoff_core",
    "handsoff_routing",
    "handsoff_config",
    "handsoff_ledger",
    "handsoff_resources",
]

STDLIB_OK = {
    "__future__", "json", "os", "re", "copy", "datetime", "pathlib", "hashlib",
    "uuid", "fcntl", "math", "shlex", "tomllib", "subprocess", "fnmatch",
    "sysconfig", "contextlib",
}


def _tree(module):
    return ast.parse((BIN / f"{module}.py").read_text(encoding="utf-8"))


def _bound_names(tree):
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            target = node.target
            if isinstance(target, ast.Name):
                bound.add(target.id)
            elif isinstance(target, ast.Tuple):
                bound |= {e.id for e in target.elts if isinstance(e, ast.Name)}
        elif isinstance(node, ast.withitem) and isinstance(getattr(node, "optional_vars", None), ast.Name):
            bound.add(node.optional_vars.id)
        elif isinstance(node, ast.Tuple) and isinstance(getattr(node, "ctx", None), ast.Store):
            bound |= {e.id for e in node.elts if isinstance(e, ast.Name)}
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


class EveryExtractedModuleResolves(unittest.TestCase):
    """The check stage 4 needed and did not have."""

    def test_no_module_references_an_unbound_name(self):
        for module in LAYERS:
            tree = _tree(module)
            bound = _bound_names(tree)
            used = {n.id for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            unresolved = sorted(used - bound)
            self.assertEqual(unresolved, [],
                             f"{module} references names nothing binds: {unresolved}. "
                             "This is the NameError shape that only appears when the "
                             "function is called, not when the module imports.")

    def test_every_module_imports_in_isolation(self):
        """Imported in a subprocess so an already-loaded monolith cannot mask
        a missing dependency."""
        for module in LAYERS:
            result = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {str(BIN)!r}); import {module}"],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{module}: {result.stderr[-400:]}")


class TheLayersOnlyPointDownward(unittest.TestCase):
    """No module may import a layer above it; that is what keeps it acyclic."""

    def test_each_module_imports_only_lower_layers(self):
        """Module level only. Routing still reaches the monolith inside
        function bodies: that is declared migration coupling from the first
        extraction, governed by tests/test_routing_boundary.py, and it is
        what stage 1 began removing. Forbidding it here would fail on a
        state the plan records as intentional."""
        for index, module in enumerate(LAYERS):
            allowed = set(LAYERS[:index])
            for node in _tree(module).body:
                names = []
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("handsoff"):
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names if a.name.startswith("handsoff")]
                for name in names:
                    self.assertIn(name, allowed,
                                  f"{module} imports {name}, which is not below it "
                                  f"(allowed: {sorted(allowed) or 'nothing'})")

    def test_no_module_imports_the_monolith_at_module_level(self):
        """A module-level monolith import closes the re-export cycle and
        makes the package unimportable. Deferred ones inside functions are
        the documented workaround, counted and bounded elsewhere."""
        for module in LAYERS:
            for node in _tree(module).body:
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "handsoff_lib",
                                        f"{module} imports the monolith, closing a cycle")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "handsoff_lib", module)

    def test_deferred_monolith_imports_are_only_where_declared(self):
        """The workaround must stay confined and shrinking.

        Only model routing, the first extraction, still needs it. A second
        module acquiring deferred monolith imports would mean the layering
        stopped being enough, which is a finding rather than a detail.
        """
        allowed = {"handsoff_routing"}
        for module in LAYERS:
            deferred = 0
            for node in _tree(module).body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.ImportFrom) and inner.module == "handsoff_lib":
                        deferred += 1
            if module in allowed:
                continue
            self.assertEqual(deferred, 0,
                             f"{module} defers {deferred} monolith import(s); only "
                             f"{sorted(allowed)} is permitted to, and that is temporary")

    def test_module_level_third_party_imports_are_stdlib_only(self):
        for module in LAYERS:
            for node in _tree(module).body:
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = [node.module.split(".")[0]]
                for root in roots:
                    if root.startswith("handsoff"):
                        continue
                    self.assertIn(root, STDLIB_OK, f"{module} imports {root}")


class EveryModuleIsRegisteredWhereItMustBe(unittest.TestCase):
    """#325: the file set lives in more than one registry."""

    def test_the_runtime_manifest_covers_every_module(self):
        manifest = (BIN / "handsoff_manifest.py").read_text(encoding="utf-8")
        for module in LAYERS:
            self.assertIn(f"bin/{module}.py", manifest,
                          f"{module} can change without invalidating evidence")

    def test_the_wheel_packages_every_module(self):
        pyproject = (BIN.parent / "pyproject.toml").read_text(encoding="utf-8")
        for module in LAYERS:
            self.assertIn(module, pyproject, f"the wheel would install without {module}")

    def test_the_monolith_reexports_every_module(self):
        tree = ast.parse((BIN / "handsoff_lib.py").read_text(encoding="utf-8"))
        reexported = {node.module for node in ast.walk(tree)
                      if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("handsoff")}
        for module in LAYERS:
            self.assertIn(module, reexported,
                          f"handsoff_lib does not re-export {module}; callers would break")


if __name__ == "__main__":
    unittest.main()
