"""#323: a fixture tree is writable whatever the source mode was.

A managed reviewer's sandbox mounts the project read-only.
`shutil.copy` and `shutil.copytree` preserve the source's permission
bits, so on a read-only checkout every fixture copy landed read-only and
`normalize_fixture_config` failed in `setUp` with PermissionError. Every
`HandsoffTestCase` subclass errored there while passing on any developer
checkout, which means a managed reviewer's verdict on a test-bearing
change was never based on a run that happened.

These tests exercise the copy behaviour directly against a read-only
source, because the defect is invisible on a writable one.
"""
import ast
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, _make_writable

ROOT = BIN.parent


def _read_only_tree():
    source = Path(tempfile.mkdtemp(prefix="handsoff-ro-source-"))
    (source / "nested").mkdir()
    (source / "handsoff.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (source / "nested" / "inner.json").write_text("{}", encoding="utf-8")
    for path in [source, *source.rglob("*")]:
        path.chmod(path.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    return source


class ACopyFromAReadOnlySourceIsStillWritable(unittest.TestCase):
    """REQ-005: the behaviour, proved against a genuinely read-only tree."""

    def setUp(self):
        self.source = _read_only_tree()
        self.addCleanup(self._cleanup, self.source)
        self.dest = Path(tempfile.mkdtemp(prefix="handsoff-ro-dest-"))
        self.addCleanup(self._cleanup, self.dest)

    @staticmethod
    def _cleanup(path):
        for entry in [path, *path.rglob("*")]:
            try:
                entry.chmod(entry.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
        shutil.rmtree(path, ignore_errors=True)

    def test_shutil_copy_reproduces_the_defect(self):
        """Pins the cause. If this ever stops holding, the fix below is
        no longer needed and should be removed rather than kept."""
        target = self.dest / "handsoff.toml"
        shutil.copy(self.source / "handsoff.toml", target)
        with self.assertRaises(PermissionError):
            target.write_text("changed", encoding="utf-8")

    def test_copyfile_leaves_the_mode_to_the_umask(self):
        target = self.dest / "from-copyfile.toml"
        shutil.copyfile(self.source / "handsoff.toml", target)
        target.write_text("changed", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed")

    def test_make_writable_recovers_a_read_only_directory(self):
        """copytree still creates directories with the source mode, so a
        file-only fix leaves a directory nothing can write into."""
        shutil.copytree(self.source, self.dest / "tree", copy_function=shutil.copyfile)
        _make_writable(self.dest / "tree")
        (self.dest / "tree" / "nested" / "created-after.json").write_text("{}", encoding="utf-8")
        self.assertTrue((self.dest / "tree" / "nested" / "created-after.json").is_file())

    def test_make_writable_touches_nothing_outside_the_tree(self):
        outside = self.dest / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        outside.chmod(0o444)
        tree = self.dest / "tree"
        shutil.copytree(self.source, tree, copy_function=shutil.copyfile)
        _make_writable(tree)
        self.assertFalse(os.access(outside, os.W_OK),
                         "_make_writable must not widen permissions outside its argument")


class TheBaseUsesTheModeFreeCopy(unittest.TestCase):
    """REQ-005, derived from the source rather than from behaviour.

    A future edit that reaches for the convenient `shutil.copy` puts every
    managed reviewer back where it started, and does so invisibly on a
    writable checkout.
    """

    def _setup_source(self):
        source = (ROOT / "tests" / "test_handsoff_supervisor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and node.name == "HandsoffTestCase")
        fn = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == "setUp")
        return ast.get_source_segment(source, fn) or ""

    def test_setup_does_not_call_the_mode_preserving_copy(self):
        body = self._setup_source()
        self.assertNotIn("shutil.copy(", body,
                         "shutil.copy preserves the source mode and breaks every "
                         "HandsoffTestCase under a read-only project")
        self.assertNotIn("shutil.copytree(ROOT / directory, self.tmp / directory)\n", body,
                         "copytree without copy_function preserves the source mode")

    def test_setup_copies_content_and_then_widens_the_tree(self):
        body = self._setup_source()
        self.assertIn("shutil.copyfile", body)
        self.assertIn("copy_function=shutil.copyfile", body)
        self.assertIn("_make_writable(self.tmp)", body)


class TheFixtureTreeIsWritableInPractice(HandsoffTestCase):
    """REQ-005: the end state a subclass actually depends on."""

    def test_the_copied_config_can_be_rewritten(self):
        (self.tmp / "handsoff.toml").write_text(
            (self.tmp / "handsoff.toml").read_text(encoding="utf-8"), encoding="utf-8")

    def test_a_new_file_can_be_created_in_a_copied_directory(self):
        (self.tmp / "schemas" / "probe.json").write_text("{}", encoding="utf-8")
        self.assertTrue((self.tmp / "schemas" / "probe.json").is_file())


if __name__ == "__main__":
    unittest.main()
