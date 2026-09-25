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
    "handsoff_agent_runtime",
    "handsoff_projection",
]

STDLIB_OK = {
    "__future__", "json", "os", "re", "copy", "datetime", "pathlib", "hashlib",
    "uuid", "fcntl", "math", "shlex", "tomllib", "subprocess", "fnmatch",
    "sysconfig", "contextlib", "shutil", "threading",
}


def _top_level_names(tree):
    """Names the module itself defines at top level: def, class, and assignment.

    Imported names are deliberately excluded -- a re-export is not a definition,
    and treating it as one would say a symbol lives in two places at once.
    """
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _tree(module):
    return ast.parse((BIN / f"{module}.py").read_text(encoding="utf-8"))


def _target_names(target):
    """Every name a binding target introduces, at any nesting depth.

    `for key, (minimum, maximum) in bounds.items():` nests a tuple inside
    the loop target. Unpacking one level only reported those inner names as
    unbound, which is a false alarm about correct code and exactly as
    useless as a missed real one.
    """
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts)) if target.elts else set()
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _scope_bindings(node):
    """Names bound directly in ONE scope, not descending into nested scopes.

    A nested function contributes only its own name here. Its parameters and
    locals belong to its scope, not this one.
    """
    bound = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        bound |= {a.arg for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)}
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    for child in _walk_scope(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)  # the NAME only; its body is its own scope
        elif isinstance(child, ast.Assign):
            for t in child.targets:
                bound |= _target_names(t)
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign)) and isinstance(child.target, ast.Name):
            bound.add(child.target.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
            bound |= _target_names(child.target)
        elif isinstance(child, ast.withitem) and getattr(child, "optional_vars", None) is not None:
            bound |= _target_names(child.optional_vars)
        elif isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            bound.add(child.target.id)
        elif isinstance(child, ast.Global):
            bound |= set(child.names)
    return bound


def _nested_scopes(node):
    """The scopes defined directly inside this one.

    A class body is a scope too. Treating it as part of the enclosing one
    let class attributes leak outwards, so `class C: token = 1` satisfied a
    bare `token` elsewhere in the module. That is the same shared pool this
    walker exists to prevent, relocated to classes.
    """
    return [c for c in _walk_scope(node)
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))]


def _walk_scope(node):
    """Every node inside `node`, stopping at each nested function or lambda.

    Pooling nested scopes together is the bug this file exists to catch: it
    is what let a parameter in one function satisfy an unbound reference in
    a sibling. The boundary has to be respected while walking, not patched
    afterwards.
    """
    out = []
    bodies = []
    if isinstance(node, ast.Module):
        bodies = [node.body]
    elif isinstance(node, ast.Lambda):
        bodies = [[node.body]]
    else:
        bodies = [getattr(node, "body", [])]
        for extra in ("orelse", "finalbody", "handlers", "decorator_list"):
            bodies.append(getattr(node, extra, []) or [])
    stack = [n for body in bodies for n in body]
    while stack:
        current = stack.pop()
        out.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue  # a new scope: its insides are not ours
        for child in ast.iter_child_nodes(current):
            stack.append(child)
    return out


def _loads_in_scope(node):
    return {n.id for n in _walk_scope(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _unresolved(tree):
    """Every name used in a scope that nothing in its chain binds."""
    builtin = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    unresolved = set()

    def visit(scope, inherited):
        own = _scope_bindings(scope)
        visible = inherited | own
        unresolved.update(_loads_in_scope(scope) - visible)
        for nested in _nested_scopes(scope):
            if isinstance(scope, ast.ClassDef):
                # Python does not give a method the class body's names: a
                # method reading a class attribute by bare name raises
                # NameError. So a scope inside a class inherits what
                # encloses the CLASS, not the class body.
                visit(nested, inherited)
            else:
                visit(nested, visible)

    visit(tree, builtin)
    return sorted(unresolved)


class EveryExtractedModuleResolves(unittest.TestCase):
    """The check stage 4 needed and did not have."""

    def test_no_module_references_an_unbound_name(self):
        """Scope by scope, because a shared pool cannot answer this.

        Two earlier versions of this check were wrong in the same way. The
        first pooled every parameter in the module, so a parameter named
        `design_hash` in `create_agent_session` satisfied an unbound
        `design_hash(...)` in `open_review_attempt` and shipped a NameError.
        The second respected the module boundary but still merged nested
        sibling closures, so two closures inside one function shared a pool
        and the same masking returned one level down. A scope sees its
        enclosing chain and its own bindings; never its siblings'.
        """
        for module in LAYERS:
            unresolved = _unresolved(_tree(module))
            self.assertEqual(unresolved, [],
                             f"{module} references names nothing binds: {unresolved}. "
                             "This is the NameError shape that only appears when the "
                             "function is called, not when the module imports.")

    def test_the_checker_catches_a_sibling_closure_masking_a_name(self):
        """The checker is the thing most likely to be wrong here, so it is
        tested against the pattern it exists to find, which already occurs
        in handsoff_lib.py: sibling closures with same-named parameters."""
        source = (
            "def outer():\n"
            "    def first(token):\n"
            "        return token\n"
            "    def second():\n"
            "        return token\n"  # unbound: `token` belongs to first()
            "    return first, second\n"
        )
        self.assertIn("token", _unresolved(ast.parse(source)),
                      "a sibling closure's parameter is masking an unbound name")

    def test_the_checker_accepts_a_genuine_enclosing_binding(self):
        """The mirror case: a nested scope may read its enclosing scope."""
        source = (
            "def outer(token):\n"
            "    def inner():\n"
            "        return token\n"
            "    return inner\n"
        )
        self.assertEqual(_unresolved(ast.parse(source)), [])

    def test_a_class_attribute_does_not_leak_into_the_enclosing_scope(self):
        """`class C: token = 1` must not satisfy a bare `token` elsewhere.

        Treating a class body as part of its enclosing scope was the same
        shared pool as the sibling-closure bug, relocated to classes.
        """
        source = (
            "class C:\n"
            "    token = 1\n"
            "def f():\n"
            "    return token\n"
        )
        self.assertIn("token", _unresolved(ast.parse(source)))

    def test_a_method_does_not_inherit_the_class_body(self):
        """Verified against the interpreter: a method reading a class
        attribute by bare name raises NameError, so the checker must not
        pretend that name is visible."""
        source = (
            "class C:\n"
            "    token = 1\n"
            "    def m(self):\n"
            "        return token\n"
        )
        self.assertIn("token", _unresolved(ast.parse(source)))

    def test_a_class_body_may_still_read_its_enclosing_scope(self):
        source = (
            "LIMIT = 3\n"
            "class C:\n"
            "    cap = LIMIT\n"
        )
        self.assertEqual(_unresolved(ast.parse(source)), [])

    def test_the_checker_unpacks_nested_binding_targets(self):
        """`for key, (low, high) in pairs:` binds three names, not one."""
        source = (
            "def f(pairs):\n"
            "    for key, (low, high) in pairs:\n"
            "        print(key, low, high)\n"
        )
        self.assertEqual(_unresolved(ast.parse(source)), [])

    def test_the_checker_binds_lambda_and_comprehension_targets(self):
        source = (
            "PAIRS = {k: v for k, v in ()}\n"
            "KEY = sorted([], key=lambda row: row.id)\n"
        )
        self.assertEqual(_unresolved(ast.parse(source)), [])

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


class NoTestPatchesAMovedSymbolOnTheMonolith(unittest.TestCase):
    """#284: re-export keeps callers working; it does not redirect patching.

    An extracted module does `from handsoff_ledger import commit`, which
    binds the name at import time. `mock.patch.object(handsoff_lib, "commit")`
    then patches something nothing calls, and the test passes while
    exercising the real code path it meant to intercept.

    That happened twice. `test_telemetry_is_optional_non_gating_and_failure_safe`
    patched `lib.commit` to prove a write failure propagates, and it only
    failed because it asserted a raise. The sleep-accounting tests patched
    `lib._read_pmset_log` and silently read the machine's real pmset log,
    returning 281 intervals where the fixture had 1. The second kind is
    worse: a patch that quietly does nothing and still reports green.

    This finds them mechanically, before either shape can ship.
    """

    def _symbols_that_left_the_monolith(self):
        """name -> the extracted module that now defines it."""
        moved = {}
        lib_own = _top_level_names(_tree("handsoff_lib"))
        for module in LAYERS:
            for name in _top_level_names(_tree(module)) - lib_own:
                moved.setdefault(name, module)
        return moved

    def test_the_moved_set_is_derived_not_guessed(self):
        """Pins the three symbols whose bindings caused real silent failures,
        so a later extraction cannot quietly drop them out of the guard."""
        moved = self._symbols_that_left_the_monolith()
        self.assertEqual(moved.get("commit"), "handsoff_ledger")
        self.assertEqual(moved.get("_read_pmset_log"), "handsoff_projection")
        self.assertEqual(moved.get("create_agent_session"), "handsoff_agent_runtime")
        self.assertNotIn("playbook_section", moved, "still defined in the monolith")

    def test_every_patch_of_a_moved_symbol_goes_through_patch_engine(self):
        """mock.patch.object(lib, "X") replaces one binding of X.

        Once X lives in another module, each importer holds its own binding,
        and whether the monolith's is the one the test drives depends on the
        call path -- which no static check can decide. Two tests got it wrong
        and stayed green: one patched lib.commit while the commit ran inside
        handsoff_agent_runtime, and the sleep tests patched lib._read_pmset_log
        while handsoff_projection read the operator's real pmset log. So the
        rule is not "reason about the call path" but "patch every binding":
        tests.engine_patch.patch_engine does that, and makes an inert patch
        unexpressible rather than merely detectable.
        """
        moved = self._symbols_that_left_the_monolith()
        offenders = []
        for path in sorted((BIN.parent / "tests").glob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                if getattr(node.func, "attr", None) != "object":
                    continue
                target, name = node.args[0], node.args[1]
                if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                    continue
                if getattr(target, "id", getattr(target, "attr", None)) not in {"lib", "handsoff_lib"}:
                    continue
                if name.value in moved:
                    offenders.append(f"{path.name}:{node.lineno} patches handsoff_lib."
                                     f"{name.value}, which now lives in {moved[name.value]}")
        self.assertEqual(offenders, [],
                         "patch these with tests.engine_patch.patch_engine(\"<name>\", ...), which "
                         "replaces the name on every module that binds it; patching the monolith "
                         "alone leaves the other bindings pointing at the real implementation")


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
