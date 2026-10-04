"""Scanner fixture: every unmarked case here reads engine source and must be reported.

Never imported; the scanner only parses it.
"""
import ast
import inspect
import os
import subprocess
import unittest
from pathlib import Path

import handsoff_lib as lib
from tests import helpers
from tests.guards import guard
from tests.helpers import HELPER_ENGINE, engine_text, read_path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
ENGINE = BIN / "handsoff_lib.py"
ALIAS = ENGINE
LIB_TEXT = ENGINE.read_text()
GIT_ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               check=True).stdout.strip())


def _tree():
    return ast.parse(ENGINE.read_text())


def _defined():
    return {node.name for node in _tree().body if hasattr(node, "name")}


class DirectRead(unittest.TestCase):
    def test_direct_read(self):
        self.assertIn("def", (BIN / "handsoff_lib.py").read_text())


class SetUpRead(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "bin" / "handsoff_core.py").read_text()

    def test_uses_the_setup_text(self):
        self.assertTrue(self.source)

    def test_ignores_the_setup_text(self):
        self.assertTrue(True)


class SetUpClassRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(BIN / "handsoff_ledger.py") as handle:
            cls.source = handle.read()

    def test_uses_the_class_text(self):
        self.assertTrue(self.source)


class InheritedSetUp(SetUpRead):
    def test_inherits_the_setup_read(self):
        self.assertTrue(self.source)


class HelperReads(unittest.TestCase):
    def test_two_hop_helper(self):
        self.assertTrue(_defined())

    def test_helper_imported_from_another_tests_module(self):
        self.assertTrue(engine_text())

    def test_helper_through_a_module_attribute(self):
        self.assertTrue(helpers.engine_text())

    def test_parameter_bound_helper(self):
        self.assertTrue(read_path(ENGINE))

    def test_method_helper(self):
        self.assertTrue(self._read())

    def test_keyword_bound_helper(self):
        self.assertTrue(read_path(path=ENGINE))

    def test_instance_method_bound_helper(self):
        self.assertTrue(self._read_from(ENGINE))

    def _read_from(self, path):
        return path.read_text()

    def _read(self):
        return self._path().read_bytes()

    def _path(self):
        return BIN / "handsoff_agent.py"


class ConstantReads(unittest.TestCase):
    def test_constant_naming_a_bin_file(self):
        self.assertTrue(ENGINE.read_text())

    def test_path_alias(self):
        self.assertTrue(ALIAS.read_text())

    def test_module_level_source_constant(self):
        self.assertIn("def", LIB_TEXT)

    def test_constant_from_another_tests_module(self):
        self.assertTrue(HELPER_ENGINE.read_text())

    def test_git_toplevel_root(self):
        self.assertTrue((GIT_ROOT / "bin" / "handsoff_lib.py").read_text())

    def test_local_alias(self):
        path = BIN / "handsoff_lib.py"
        self.assertTrue(path.read_text())

    def test_glob_of_bin(self):
        for path in sorted(BIN.glob("*.py")):
            self.assertTrue(path.read_text())

    def test_os_path_join(self):
        with open(os.path.join(ROOT, "bin", "handsoff_lib.py")) as handle:
            self.assertTrue(handle.read())

    def test_inspect_getsource(self):
        self.assertIn("def", inspect.getsource(lib.load_config))

    def test_engine_module_file(self):
        self.assertTrue(Path(lib.__file__).read_text())

    def test_literal_bin_path(self):
        with open("bin/handsoff_lib.py") as handle:
            self.assertTrue(handle.read())


class Marked(unittest.TestCase):
    @guard
    def test_marked_reader(self):
        self.assertTrue(ENGINE.read_text())


@guard
class MarkedClass(unittest.TestCase):
    def setUp(self):
        self.source = ENGINE.read_text()

    def test_class_marked(self):
        self.assertTrue(self.source)
