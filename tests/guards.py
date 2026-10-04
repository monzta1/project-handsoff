#!/usr/bin/env python3
"""#371: guards, the test cases that read engine source as text.

A guard asserts something about the text of a bin/ file: a count, a
registry entry, a call that must or must not appear. Those cases pass or
fail on the source alone, so they are the cheap ones to run after the last
edit and before every push, where a red CI round is the expensive way to
learn a count went stale.

    python3 -m tests.guards                      run every marked case in tests/
    python3 -m tests.guards --ids-out FILE       also write the executed ids as JSON

`guard` marks a case (or every test method of a class). The runner imports
every tests/test_*.py module, runs only the marked cases, and fails, writing
no record, on an import or discovery error, when no guard executes, when a
guard is skipped (a skipped guard did not run), or when the executed ids
differ from the marker set. `scan` is the static half: it reports every case
that reads a bin/ file without the marker, so a new source-reading test
cannot quietly stay out of the guard run.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

MARKER = "__handsoff_guard__"
REPO = Path(__file__).resolve().parents[1]
RECORD = Path(".handsoff") / "guards-record.json"
RECORD_SCHEMA = 1


def guard(target):
    """Mark a test method, or every test method a class defines, as a guard."""
    if isinstance(target, type):
        prefix = unittest.TestLoader.testMethodPrefix
        for name, value in list(vars(target).items()):
            if name.startswith(prefix) and callable(value):
                setattr(value, MARKER, True)
        return target
    setattr(target, MARKER, True)
    return target


def is_guard(test: unittest.TestCase) -> bool:
    return bool(getattr(getattr(test, test._testMethodName, None), MARKER, False))


# ---------------------------------------------------------------------------
# The scanner: which cases read a bin/ file as text.
#
# Path expressions are evaluated to an abstract kind: the tests file or
# directory (from __file__), the repository root, the bin/ directory, a bin/
# file, or the text of a bin/ file. Names take their kind from assignments in
# the function, the class, the module, or a tests module they import from.
# A read is read_text(), read_bytes(), open() on a bin/ file, or
# inspect.getsource() of an engine object (a handsoff_* module or anything
# under bin/). A call into a tests helper binds its receiver, positional and
# keyword arguments to the helper's parameters by name.
# ---------------------------------------------------------------------------

TESTS_FILE, TESTS_DIR, ROOT, BIN_DIR, BIN_FILE, SOURCE = (
    "tests_file", "tests_dir", "root", "bin_dir", "bin_file", "source")
#: Implementation review attempt 2: an engine object (a handsoff module or a
#: name taken from one) keeps its provenance through assignments, so
#: `target = lib.load_config; inspect.getsource(target)` is still a read.
ENGINE_OBJECT = "engine_object"
_PARENT = {TESTS_FILE: TESTS_DIR, TESTS_DIR: ROOT, BIN_FILE: BIN_DIR, BIN_DIR: ROOT}
_PATHY = {BIN_DIR, BIN_FILE}
_PASS_THROUGH = {"resolve", "absolute", "expanduser", "as_posix", "strip", "rstrip",
                 "fspath", "abspath", "realpath", "normpath", "sorted", "list", "tuple",
                 "set", "reversed", "str", "Path", "PurePath", "PosixPath"}
_SOURCE_READERS = {"getsource", "getsourcelines", "findsource"}
_OPENERS = {"open"}


def _contains_toplevel_query(node) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == "--show-toplevel" for n in ast.walk(node))


def _join(kind, part) -> str | None:
    if kind in _PATHY:
        return BIN_FILE
    if kind == ROOT and isinstance(part, ast.Constant) and isinstance(part.value, str):
        text = part.value.lstrip("./") if part.value.startswith("./") else part.value
        if text.rstrip("/") == "bin":
            return BIN_DIR
        if text.startswith("bin/"):
            return BIN_FILE
    return None


def _module_statements(body):
    """Module-level statements, including those inside if/try/with blocks."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for block in ("body", "orelse", "finalbody"):
                yield from _module_statements(getattr(node, block, []))
            for handler in getattr(node, "handlers", []):
                yield from _module_statements(handler.body)


class _Module:
    def __init__(self, name: str, path: Path, tree: ast.Module):
        self.name, self.path, self.tree = name, path, tree
        self.env: dict[str, str | None] = {}
        self.functions: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.imports: dict[str, tuple[str, str | None]] = {}  # local -> (module, attr)
        self.class_attrs: dict[str, dict[str, str | None]] = {}
        # names bound to an engine module anywhere in the file: `import handsoff_agent as agent`
        self.engine_modules = {alias.asname or alias.name for node in ast.walk(tree)
                               if isinstance(node, ast.Import) for alias in node.names
                               if alias.name.startswith("handsoff")}
        # ... and names taken from one: `from handsoff_lib import load_config`
        self.engine_names = self.engine_modules | {
            alias.asname or alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and not node.level and node.module
            and (node.module.startswith("handsoff") or node.module.split(".")[0] == "bin")
            for alias in node.names}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node


class Scanner:
    """Static reach analysis over one tests directory (a package)."""

    def __init__(self, tests_dir: Path):
        self.tests_dir = Path(tests_dir)
        self.package = self.tests_dir.name
        self.modules: dict[str, _Module] = {}
        self._memo: dict[tuple, object] = {}
        for path in sorted(self.tests_dir.glob("*.py")):
            name = f"{self.package}.{path.stem}"
            self.modules[name] = _Module(name, path, ast.parse(path.read_text(encoding="utf-8")))
        for module in self.modules.values():
            self._collect_imports(module)
        for _ in range(3):  # constants may name constants defined further down or imported
            for module in self.modules.values():
                self._module_env(module)
            self._memo.clear()  # results computed against a partial environment

    # -- names ---------------------------------------------------------------

    def _resolve_module(self, module: _Module, name: str | None, level: int) -> str | None:
        if level:
            base = module.name.rsplit(".", level)[0]
            name = f"{base}.{name}" if name else base
        return name

    def _collect_imports(self, module: _Module):
        for node in module.tree.body:
            if isinstance(node, ast.ImportFrom):
                source = self._resolve_module(module, node.module, node.level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if f"{source}.{alias.name}" in self.modules:
                        module.imports[local] = (f"{source}.{alias.name}", None)
                    elif source in self.modules:
                        module.imports[local] = (source, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.modules:
                        module.imports[alias.asname or alias.name] = (alias.name, None)

    def _module_env(self, module: _Module):
        env = module.env
        for local, (source, attr) in module.imports.items():
            if attr is not None:
                env[local] = self.modules[source].env.get(attr) or env.get(local)
        for node in _module_statements(module.tree.body):
            self._bind_statement(node, env, module, None)
        for cls in module.classes.values():
            attrs = module.class_attrs.setdefault(cls.name, {})
            for node in cls.body:
                self._bind_statement(node, {**env, **attrs}, module, None, into=attrs)
            for func in cls.body:
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local = self._local_env(func, module, cls, ())
                    for node in ast.walk(func):
                        if isinstance(node, ast.Assign):
                            for target in node.targets:
                                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                                        and target.value.id in ("self", "cls")):
                                    kind = self.kind(node.value, local, module, cls)
                                    if kind:
                                        attrs[target.attr] = kind

    def _bind_statement(self, node, env, module, cls, into=None):
        into = env if into is None else into
        if isinstance(node, ast.Assign):
            kind = self.kind(node.value, env, module, cls)
            for target in node.targets:
                self._bind_target(target, kind, into)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self._bind_target(node.target, self.kind(node.value, env, module, cls), into)

    @staticmethod
    def _bind_target(target, kind, env):
        if isinstance(target, ast.Name):
            if kind or target.id not in env:
                env[target.id] = kind
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                Scanner._bind_target(element, kind, env)

    def _local_env(self, func, module, cls, bound) -> dict:
        assigned = {n.id for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        env = {name: kind for name, kind in module.env.items() if name not in assigned}
        args = func.args
        params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        params += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
        given = dict(bound)
        for name in params:
            env[name] = given.get(name)
        for _ in range(2):
            for node in ast.walk(func):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    self._bind_statement(node, env, module, cls)
                elif isinstance(node, ast.NamedExpr):
                    self._bind_target(node.target, self.kind(node.value, env, module, cls), env)
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                    self._bind_target(node.target, self.kind(node.iter, env, module, cls), env)
                elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                    self._bind_target(node.optional_vars, self.kind(node.context_expr, env, module, cls), env)
        return env

    # -- classes -------------------------------------------------------------

    def _class_ref(self, module: _Module, node) -> tuple[_Module, ast.ClassDef] | None:
        if isinstance(node, ast.Name):
            if node.id in module.classes:
                return module, module.classes[node.id]
            if node.id in module.imports:
                source, attr = module.imports[node.id]
                target = self.modules[source]
                if attr in target.classes:
                    return target, target.classes[attr]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            ref = module.imports.get(node.value.id)
            if ref and ref[1] is None and node.attr in self.modules[ref[0]].classes:
                target = self.modules[ref[0]]
                return target, target.classes[node.attr]
        return None

    def mro(self, module: _Module, cls: ast.ClassDef, seen=None) -> list[tuple[_Module, ast.ClassDef]]:
        key = ("mro", module.name, cls.name)
        if seen is None and key in self._memo:
            return self._memo[key]
        top = seen is None
        seen = set() if seen is None else seen
        if key in seen:
            return []
        seen.add(key)
        order = [(module, cls)]
        for base in cls.bases:
            ref = self._class_ref(module, base)
            if ref:
                order += self.mro(*ref, seen=seen)
        if top:
            self._memo[key] = order
        return order

    def _class_attrs(self, module, cls) -> dict:
        merged: dict = {}
        for owner_module, owner in reversed(self.mro(module, cls)):
            merged.update(owner_module.class_attrs.get(owner.name, {}))
        return merged

    def is_test_case(self, module: _Module, cls: ast.ClassDef) -> bool:
        for owner_module, owner in self.mro(module, cls):
            for base in owner.bases:
                text = ast.unparse(base)
                if text.endswith("TestCase") and self._class_ref(owner_module, base) is None:
                    return True
        return False

    @staticmethod
    def _method(cls: ast.ClassDef, name: str):
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def find_method(self, module, cls, name):
        for owner_module, owner in self.mro(module, cls):
            method = self._method(owner, name)
            if method is not None:
                return owner_module, owner, method
        return None

    # -- kinds ---------------------------------------------------------------

    def kind(self, node, env, module, cls) -> str | None:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return TESTS_FILE
            if node.id in module.engine_names:
                return ENGINE_OBJECT
            return env.get(node.id)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value.startswith("bin/"):
                return BIN_FILE
            return None
        if isinstance(node, (ast.Call, ast.Attribute)) and _contains_toplevel_query(node):
            return ROOT
        if isinstance(node, ast.Attribute):
            if node.attr == "parent":
                return _PARENT.get(self.kind(node.value, env, module, cls))
            if node.attr == "__file__" and isinstance(node.value, ast.Name) \
                    and node.value.id in module.engine_modules:
                return BIN_FILE
            if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                return self._class_attrs(module, cls).get(node.attr) if cls is not None else None
            if isinstance(node.value, ast.Name) and node.value.id in module.imports:
                source, attr = module.imports[node.value.id]
                if attr is None:
                    return self.modules[source].env.get(node.attr)
            # implementation review attempt 2: an attribute of an engine
            # object (lib.load_config) is one too, after every path rule
            # above; a CALL's result (lib.load_config(root)) is data, not source
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and (base.id in module.engine_names
                                               or base.id in module.engine_modules
                                               or env.get(base.id) == ENGINE_OBJECT):
                return ENGINE_OBJECT
            return None
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "parents":
                kind = self.kind(value.value, env, module, cls)
                index = node.slice.value if isinstance(node.slice, ast.Constant) else None
                if isinstance(index, int):
                    for _ in range(index + 1):
                        kind = _PARENT.get(kind)
                    return kind
                return None
            return self.kind(value, env, module, cls)
        if isinstance(node, ast.BinOp):
            left = self.kind(node.left, env, module, cls)
            if isinstance(node.op, ast.Div):
                return _join(left, node.right)
            if isinstance(node.op, ast.Add) and left in _PATHY:
                return BIN_FILE
            return None
        if isinstance(node, ast.JoinedStr):
            if node.values and isinstance(node.values[0], ast.FormattedValue):
                head = self.kind(node.values[0].value, env, module, cls)
                if head in _PATHY:
                    return BIN_FILE
                if head == ROOT and len(node.values) > 1 and isinstance(node.values[1], ast.Constant) \
                        and str(node.values[1].value).startswith("/bin"):
                    return BIN_DIR if str(node.values[1].value).rstrip("/") == "/bin" else BIN_FILE
            return None
        if isinstance(node, ast.IfExp):
            return self.kind(node.body, env, module, cls) or self.kind(node.orelse, env, module, cls)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                kind = self.kind(element, env, module, cls)
                if kind:
                    return kind
            return None
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            local = dict(env)
            for generator in node.generators:
                self._bind_target(generator.target, self.kind(generator.iter, local, module, cls), local)
            return self.kind(node.elt, local, module, cls)
        if isinstance(node, ast.Call):
            return self._call_kind(node, env, module, cls)
        return None

    def _call_kind(self, node, env, module, cls):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if self._is_read(node, env, module, cls):
            return SOURCE
        if isinstance(func, ast.Attribute):
            receiver = self.kind(func.value, env, module, cls)
            if receiver in _PATHY and name in ("glob", "rglob", "iterdir", "with_name", "with_suffix"):
                return BIN_FILE
            if receiver and name == "joinpath":
                kind = receiver
                for arg in node.args:
                    kind = _join(kind, arg)
                return kind
            if receiver and name in _PASS_THROUGH:
                return receiver
            if name == "dirname" and node.args:
                return _PARENT.get(self.kind(node.args[0], env, module, cls))
            if name == "join" and isinstance(func.value, ast.Attribute) and func.value.attr == "path" and node.args:
                kind = self.kind(node.args[0], env, module, cls)
                for arg in node.args[1:]:
                    kind = _join(kind, arg)
                return kind
        if name in _PASS_THROUGH and node.args:
            kind = self.kind(node.args[0], env, module, cls)
            for arg in node.args[1:]:
                kind = _join(kind, arg)
            return kind
        target = self._callee(func, module, cls)
        if target is not None:
            *where, receiver = target
            return self._return_kind(*where, self._bind_call(where[2], receiver, node, env, module, cls))
        return None

    def _bind_call(self, func, receiver, node, env, module, cls) -> tuple:
        """The kinds a call passes, keyed by the callee's parameter names.

        A call through an instance or class (self.helper(x), Class(x),
        Class.classmethod(x)) binds the receiver to the first parameter, so
        the first argument lands on the second; keywords bind by name."""
        args = func.args
        positional = [a.arg for a in args.posonlyargs + args.args]
        if receiver and positional and not any(
                isinstance(d, ast.Name) and d.id == "staticmethod" for d in func.decorator_list):
            positional = positional[1:]
        bound = {}
        for name, arg in zip(positional, node.args):
            if isinstance(arg, ast.Starred):
                break
            bound[name] = self.kind(arg, env, module, cls)
        named = set(positional) | {a.arg for a in args.kwonlyargs}
        for keyword in node.keywords:
            if keyword.arg in named:
                bound[keyword.arg] = self.kind(keyword.value, env, module, cls)
        return tuple(sorted(bound.items(), key=lambda item: item[0]))

    def _return_kind(self, module, cls, func, bound):
        key = ("ret", module.name, getattr(cls, "name", None), func.name, tuple(bound))
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None
        env = self._local_env(func, module, cls, bound)
        result = None
        for node in ast.walk(func):
            if isinstance(node, ast.Return):
                result = result or self.kind(node.value, env, module, cls)
        self._memo[key] = result
        return result

    # -- reads and calls -----------------------------------------------------

    def _is_engine_object(self, node, env, module, cls) -> bool:
        """Whether an inspect target is engine code: a handsoff_* module or anything under bin/."""
        for part in ast.walk(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str) \
                    and (part.value.startswith("handsoff") or part.value.startswith("bin/")):
                return True  # importlib.import_module("handsoff_lib"), a module loaded from bin/
        while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
            node = node.func if isinstance(node, ast.Call) else node.value
        if isinstance(node, ast.Name):
            return node.id in module.engine_names or env.get(node.id) in (BIN_DIR, BIN_FILE, SOURCE, ENGINE_OBJECT)
        return False

    def _is_read(self, node, env, module, cls) -> bool:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in _SOURCE_READERS:
            return bool(node.args) and self._is_engine_object(node.args[0], env, module, cls)
        if isinstance(func, ast.Attribute) and name in ("read_text", "read_bytes", "open"):
            if self.kind(func.value, env, module, cls) == BIN_FILE:
                return True
        if name in _OPENERS and node.args:
            return self.kind(node.args[0], env, module, cls) == BIN_FILE
        return False

    @staticmethod
    def _with_receiver(found, receiver):
        return None if found is None else (*found, receiver)

    def _callee(self, func, module, cls):
        """(module, class, function, receiver) for a call that resolves inside the tests.

        `receiver` is True when the call binds the callee's first parameter
        itself: an instance or class call, or a constructor."""
        if isinstance(func, ast.Name):
            if func.id in module.functions:
                return module, None, module.functions[func.id], False
            if func.id in module.classes:
                return self._with_receiver(self.find_method(module, module.classes[func.id], "__init__"), True)
            ref = module.imports.get(func.id)
            if ref and ref[1] is not None:
                target = self.modules[ref[0]]
                if ref[1] in target.functions:
                    return target, None, target.functions[ref[1]], False
                if ref[1] in target.classes:
                    return self._with_receiver(self.find_method(target, target.classes[ref[1]], "__init__"), True)
            return None
        if not isinstance(func, ast.Attribute):
            return None
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in ("self", "cls") and cls is not None:
            return self._with_receiver(self.find_method(module, cls, func.attr), True)
        if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name) and owner.func.id == "super" \
                and cls is not None:
            for base_module, base in self.mro(module, cls)[1:]:
                method = self._method(base, func.attr)
                if method is not None:
                    return base_module, base, method, True
            return None
        ref = self._class_ref(module, owner)
        if ref:
            found = self.find_method(*ref, func.attr)
            # Class.method(x) passes the instance explicitly unless the method is a classmethod
            classmethod_ = found is not None and any(
                isinstance(d, ast.Name) and d.id == "classmethod" for d in found[2].decorator_list)
            return self._with_receiver(found, classmethod_)
        if isinstance(owner, ast.Name) and owner.id in module.imports:
            source, attr = module.imports[owner.id]
            if attr is None and func.attr in self.modules[source].functions:
                return self.modules[source], None, self.modules[source].functions[func.attr], False
        return None

    def reads(self, module, cls, func, bound=()) -> str | None:
        """The first engine read reachable from `func`, as `where: what`, or None."""
        key = ("reads", module.name, getattr(cls, "name", None), func.name, tuple(bound))
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None
        env = self._local_env(func, module, cls, bound)
        where = f"{module.name}.{cls.name + '.' if cls is not None else ''}{func.name}"
        found = None
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and self._is_read(node, env, module, cls):
                found = f"{where} line {node.lineno}"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and env.get(node.id) == SOURCE \
                    and module.env.get(node.id) == SOURCE:
                found = f"{where} line {node.lineno} (source constant {node.id})"
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in ("self", "cls") and cls is not None \
                    and self._class_attrs(module, cls).get(node.attr) == SOURCE:
                found = f"{where} line {node.lineno} (source attribute {node.attr})"
            elif isinstance(node, ast.Call):
                target = self._callee(node.func, module, cls)
                if target is not None:
                    *where, receiver = target
                    hop = self.reads(*where, self._bind_call(where[2], receiver, node, env, module, cls))
                    if hop:
                        found = f"{where} -> {hop}"
            if found:
                break
        self._memo[key] = found
        return found

    # -- cases ---------------------------------------------------------------

    @staticmethod
    def _decorated(node) -> bool:
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (isinstance(target, ast.Name) and target.id == "guard") or \
                    (isinstance(target, ast.Attribute) and target.attr == "guard"):
                return True
        return False

    def cases(self):
        """Yield (id, marked, reason) for every test case, reason None when it reads nothing."""
        prefix = unittest.TestLoader.testMethodPrefix
        for module in self.modules.values():
            if not module.path.name.startswith("test_"):
                continue
            module_setup = module.functions.get("setUpModule")
            for cls in module.classes.values():
                if not self.is_test_case(module, cls):
                    continue
                chain = self.mro(module, cls)
                fixture_reason = None
                for owner_module, owner in chain:
                    for hook in ("setUp", "setUpClass"):
                        method = self._method(owner, hook)
                        hop = method is not None and self.reads(owner_module, owner, method)
                        if hop and not fixture_reason:
                            fixture_reason = f"via {hook}: {hop}"
                if module_setup is not None and not fixture_reason:
                    hop = self.reads(module, None, module_setup)
                    fixture_reason = hop and f"via setUpModule: {hop}"
                seen = set()
                for owner_module, owner in chain:
                    for method in owner.body:
                        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                or not method.name.startswith(prefix) or method.name in seen:
                            continue
                        seen.add(method.name)
                        # unittest runs an inherited method under this class too
                        marked = self._decorated(method) or self._decorated(owner)
                        reason = self.reads(owner_module, owner, method) or fixture_reason
                        yield f"{module.name}.{cls.name}.{method.name}", marked, reason


def scan(tests_dir: Path | str = REPO / "tests") -> dict[str, str]:
    """Every case that reads engine source without the guard marker, id -> why."""
    return {case: reason for case, marked, reason in Scanner(Path(tests_dir)).cases()
            if reason and not marked}


def marked_cases(tests_dir: Path | str = REPO / "tests") -> set[str]:
    """Every case the source marks as a guard, read statically."""
    return {case for case, marked, _ in Scanner(Path(tests_dir)).cases() if marked}


# ---------------------------------------------------------------------------
# The runner.
# ---------------------------------------------------------------------------

class GuardResult(unittest.TextTestResult):
    """Records what actually executed: an id enters `executed` in startTest."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed: list[str] = []
        self.not_run: list[tuple[str, str]] = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)

    def addSkip(self, test, reason):
        self.not_run.append((test.id(), f"skipped: {reason}"))
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.not_run.append((test.id(), "expected failure"))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.not_run.append((test.id(), "unexpected success"))
        super().addUnexpectedSuccess(test)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def marked_in(module) -> set[str]:
    """The guard ids a module defines, read from its objects, not from discovery.

    A load_tests hook decides what discovery returns, so the marker set comes
    from the module's own TestCase classes and functions instead; a guard the
    hook leaves out is then missing from the run rather than from the set."""
    prefix = unittest.TestLoader.testMethodPrefix
    found = set()
    for value in list(vars(module).values()):
        if isinstance(value, type) and issubclass(value, unittest.TestCase):
            for name in dir(value):
                if name.startswith(prefix) and getattr(getattr(value, name, None), MARKER, False):
                    found.add(f"{value.__module__}.{value.__qualname__}.{name}")
        elif callable(value) and getattr(value, MARKER, False) \
                and getattr(value, "__module__", None) == module.__name__:
            found.add(f"{module.__name__}.{getattr(value, '__qualname__', value.__name__)}")
    return found


def collect(root: Path, package: str) -> tuple[list[unittest.TestCase], set[str], list[str]]:
    """Import every <package>/test_*.py under root; return (guards, marked ids, errors)."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    errors: list[str] = []
    guards: dict[str, unittest.TestCase] = {}
    marked: set[str] = set()
    for path in sorted((root / package).glob("test_*.py")):
        name = f"{package}.{path.stem}"
        try:
            module = importlib.import_module(name)
        except BaseException as exc:  # a SystemExit at import is a failure too
            if isinstance(exc, KeyboardInterrupt):
                raise
            errors.append(f"{name}: import failed: {type(exc).__name__}: {exc}")
            continue
        marked |= marked_in(module)
        before = len(loader.errors)
        suite = loader.loadTestsFromModule(module)
        errors += [f"{name}: discovery failed: {e.strip().splitlines()[-1]}" for e in loader.errors[before:]]
        for test in _flatten(suite):
            if type(test).__name__ in ("_FailedTest", "ModuleImportFailure"):
                errors.append(f"{name}: discovery failed: {test.id()}")
            elif is_guard(test):
                guards.setdefault(test.id(), test)
    return list(guards.values()), marked, errors


def write_record(root: Path, digest_before: str, executed: list[str], duration_ms: int) -> Path:
    """The record step, reached only by a passing run whose tree did not move.

    The record binds the run to `digest_before`, the repository digest taken
    before any test module was imported. ci-watch refuses to start unless
    that digest is still the current one, so any edit after the run, docs
    included, asks for another guard run.
    """
    import hashlib
    path = root / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": RECORD_SCHEMA,
        "repository_digest": digest_before,
        "executed": len(executed),
        "guard_ids_sha256": hashlib.sha256("\n".join(sorted(executed)).encode("utf-8")).hexdigest(),
        "duration_ms": duration_ms,
        "passed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def remove_record(root: Path) -> None:
    (root / RECORD).unlink(missing_ok=True)


def _digest(root: Path) -> str:
    """The digest verify binds evidence to, under the project's own config.

    It skips every `.handsoff*` path component, so the record never counts
    against the tree it describes."""
    if str(REPO / "bin") not in sys.path:
        sys.path.insert(0, str(REPO / "bin"))
    import handsoff_ledger
    return handsoff_ledger.repository_digest(root, handsoff_ledger.load_config(root))


def run(root: Path, package: str = "tests", ids_out: Path | None = None, stream=None) -> int:
    stream = stream or sys.stderr
    started = time.monotonic()
    digest = _digest(root)  # before any test module is imported or run
    os.environ.setdefault("HANDSOFF_SKIP_PREFLIGHT", "1")

    def fail(lines: list[str]) -> int:
        remove_record(root)
        for line in lines:
            print(f"guards FAILED: {line}", file=stream)
        return 1

    guards, marked, errors = collect(root, package)
    if errors:
        return fail(errors)
    marked = sorted(marked)
    runner = unittest.TextTestRunner(stream=stream, verbosity=1, resultclass=GuardResult)
    result = runner.run(unittest.TestSuite(guards))
    executed = sorted(set(result.executed))
    if ids_out is not None:
        Path(ids_out).write_text(json.dumps({"executed": executed, "marked": marked}), encoding="utf-8")
    problems = [f"{test.id()} failed" for test, _ in result.failures]
    problems += [f"{test.id()} errored" for test, _ in result.errors]
    problems += [f"{case} not run ({why})" for case, why in result.not_run]
    if not executed:
        problems.append("no guard executed")
    if executed != marked:
        missing = sorted(set(marked) - set(executed))
        extra = sorted(set(executed) - set(marked))
        problems.append(f"executed ids differ from the marker set: missing {missing}, unmarked {extra}")
    if problems:
        return fail(problems)
    after = _digest(root)
    if after != digest:
        return fail([f"the tree changed during the run (digest {digest[:12]} before, {after[:12]} after); "
                     f"run python3 -m tests.guards again after the last edit"])
    duration_ms = int((time.monotonic() - started) * 1000)
    write_record(root, digest, executed, duration_ms)
    print(f"guards: {len(executed)} executed in {duration_ms / 1000:.1f}s", file=stream)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tests.guards", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=REPO, help="repository root (default: this checkout)")
    parser.add_argument("--package", default="tests", help="the tests package under the root")
    parser.add_argument("--ids-out", type=Path, help="write the executed and marked ids as JSON")
    args = parser.parse_args(argv)
    return run(args.root.resolve(), args.package, args.ids_out)


if __name__ == "__main__":
    sys.exit(main())
