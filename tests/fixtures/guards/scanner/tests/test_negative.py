"""Scanner fixture: nothing here reads engine source, so nothing may be reported.

Never imported; the scanner only parses it.
"""
import ast
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import harmless

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
DOCS = ROOT / "docs" / "REFERENCE.md"
HANDSOFF = Path.home() / ".local" / "bin" / "handsoff"
FIXTURE_TEXT = "import os\nopen('bin/handsoff_lib.py').read()\n"


def _local_helper():
    return "a helper defined in the tests"


class NonEngineReads(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_reads_a_copy_in_a_temporary_directory(self):
        target = self.tmp / "bin" / "handsoff_lib.py"
        target.parent.mkdir()
        target.write_text("x = 1\n")
        self.assertEqual((self.tmp / "bin" / "handsoff_lib.py").read_text(), "x = 1\n")

    def test_a_local_name_shadows_the_root(self):
        ROOT = Path(tempfile.mkdtemp())
        (ROOT / "bin").mkdir()
        (ROOT / "bin" / "x.py").write_text("")
        self.assertEqual((ROOT / "bin" / "x.py").read_text(), "")

    def test_a_constant_naming_docs(self):
        self.assertTrue(DOCS.read_text())

    def test_a_root_file_outside_bin(self):
        self.assertTrue((ROOT / "pyproject.toml").read_text())

    def test_a_bin_directory_outside_the_repository(self):
        self.assertFalse(HANDSOFF.read_text() is None)

    def test_runs_the_engine_without_reading_it(self):
        subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "--help"], capture_output=True)

    def test_a_standard_library_module_file(self):
        self.assertTrue(Path(json.__file__).read_text())

    def test_calls_a_harmless_helper(self):
        self.assertTrue(harmless())


class ParsingFixtureText(unittest.TestCase):
    def test_parses_inline_fixture_text(self):
        tree = ast.parse(FIXTURE_TEXT)
        self.assertTrue(tree.body)

    def test_parses_a_literal(self):
        self.assertTrue(ast.parse("open('bin/handsoff_lib.py').read()").body)

    def test_getsource_of_a_tests_helper(self):
        self.assertIn("helper", inspect.getsource(_local_helper))
