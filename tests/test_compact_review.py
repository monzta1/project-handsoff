"""#290/REQ-003: a compact review is a constraint, not a polite request.

On 2026-09-23 a reviewer was told in prose to review only the final delta,
run no tests, and emit an immediate verdict. It consumed 70,796 tokens
grepping unrelated repository history and failed without a verdict. A
replacement did the same.

Prose cannot bound exploration. A working directory can: a reviewer given
nothing but the scoped slices has nothing else to find and no suite to run.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = BIN.parent
sys.path.insert(0, str(BIN))

import handsoff_lib as lib  # noqa: E402


class TheScopeIsValidated(unittest.TestCase):

    def test_a_well_formed_scope_is_accepted_and_ordered(self):
        scope = lib.validate_compact_review_scope([
            {"path": "b.py", "start": 10, "end": 20},
            {"path": "a.py", "start": 5, "end": 6},
            {"path": "a.py", "start": 1, "end": 2},
        ])
        self.assertEqual([(item["path"], item["start"]) for item in scope],
                         [("a.py", 1), ("a.py", 5), ("b.py", 10)])

    def test_an_empty_scope_is_refused(self):
        for value in ([], None, "bin/handsoff_lib.py"):
            with self.assertRaises(lib.HandsoffError):
                lib.validate_compact_review_scope(value)

    def test_an_absolute_path_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "repository-relative"):
            lib.validate_compact_review_scope([{"path": "/etc/passwd", "start": 1, "end": 2}])

    def test_a_path_that_escapes_the_repository_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "escape the repository"):
            lib.validate_compact_review_scope([{"path": "../secrets", "start": 1, "end": 2}])

    def test_an_inverted_range_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "precedes its start"):
            lib.validate_compact_review_scope([{"path": "a.py", "start": 20, "end": 10}])

    def test_a_range_too_large_to_be_compact_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "at most"):
            lib.validate_compact_review_scope(
                [{"path": "a.py", "start": 1, "end": lib.MAX_COMPACT_SCOPE_LINES + 1}])

    def test_too_many_ranges_is_a_full_review_not_a_compact_one(self):
        entries = [{"path": f"f{i}.py", "start": 1, "end": 2}
                   for i in range(lib.MAX_COMPACT_SCOPE_ENTRIES + 1)]
        with self.assertRaisesRegex(lib.HandsoffError, "full review"):
            lib.validate_compact_review_scope(entries)

    def test_a_malformed_entry_is_refused(self):
        for entry in ({"path": "a.py", "start": 1}, {"path": "a.py", "start": 1, "end": 2, "x": 1},
                      {"path": "", "start": 1, "end": 2}, {"path": "a.py", "start": 0, "end": 2},
                      {"path": "a.py", "start": True, "end": 2}):
            with self.assertRaises(lib.HandsoffError):
                lib.validate_compact_review_scope([entry])


class TheScopeIsMaterialized(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.scratch = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def _materialize(self, entries):
        scope = lib.validate_compact_review_scope(entries)
        return lib.materialize_compact_scope(ROOT, scope, self.scratch)

    def test_only_the_scoped_slices_exist_in_the_scratch(self):
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666}])
        names = sorted(p.name for p in self.scratch.iterdir())
        self.assertEqual(names, ["SCOPE.json", "bin__handsoff_lib.py.1653-1666.slice"])
        self.assertEqual(len(manifest["entries"]), 1)

    def test_the_repository_is_not_reachable_from_the_scratch(self):
        """The whole point: there is nothing else to discover."""
        self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 5}])
        for forbidden in ("tests", "bin", ".git", "pyproject.toml"):
            self.assertFalse((self.scratch / forbidden).exists(),
                             f"{forbidden} is reachable from a compact review scratch")

    def test_no_test_suite_is_materialized_so_none_can_run(self):
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 5}])
        self.assertFalse(manifest["tests_executable"])
        self.assertFalse(manifest["repository_visible"])
        self.assertEqual(list(self.scratch.glob("**/test_*.py")), [])

    def test_a_slice_carries_its_real_coordinates(self):
        """A finding must still be able to cite file and line even though
        the whole file is absent."""
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666}])
        text = (self.scratch / manifest["entries"][0]["slice"]).read_text()
        self.assertTrue(text.startswith("# bin/handsoff_lib.py lines 1653-1666"))

    def test_the_slice_content_matches_the_source_range(self):
        source = (ROOT / "bin" / "handsoff_lib.py").read_text().splitlines()
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 10, "end": 14}])
        body = (self.scratch / manifest["entries"][0]["slice"]).read_text().splitlines()[1:]
        self.assertEqual(body, source[9:14])

    def test_the_manifest_totals_the_lines_actually_written(self):
        manifest = self._materialize([
            {"path": "bin/handsoff_lib.py", "start": 1, "end": 10},
            {"path": "bin/handsoff_manifest.py", "start": 1, "end": 5},
        ])
        self.assertEqual(manifest["total_lines"], 15)
        self.assertEqual(sum(item["lines"] for item in manifest["entries"]), 15)

    def test_the_manifest_is_written_for_the_reviewer_to_read(self):
        self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 3}])
        manifest = json.loads((self.scratch / "SCOPE.json").read_text())
        self.assertEqual(manifest["schema"], "handsoff.compact_review_scope")

    def test_a_missing_file_is_refused_rather_than_silently_empty(self):
        with self.assertRaisesRegex(lib.HandsoffError, "cannot read"):
            self._materialize([{"path": "bin/not_a_file.py", "start": 1, "end": 2}])

    def test_a_range_past_the_end_of_the_file_yields_what_exists(self):
        manifest = self._materialize([{"path": "bin/handsoff_manifest.py", "start": 1, "end": 400}])
        self.assertGreater(manifest["total_lines"], 0)
        self.assertLess(manifest["total_lines"], 400)


class TheBoundIsStructuralNotProse(unittest.TestCase):
    """The distinction REQ-003 turns on."""

    def test_the_packet_shrinks_to_the_scope(self):
        """The failing review carried a 27,954-byte packet. A compact scope
        of the same question is a small fraction of that."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope = lib.validate_compact_review_scope([
            {"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666},
            {"path": "bin/handsoff_lib.py", "start": 1718, "end": 1745},
        ])
        manifest = lib.materialize_compact_scope(ROOT, scope, Path(directory.name))
        written = sum((Path(directory.name) / item["slice"]).stat().st_size
                      for item in manifest["entries"])
        self.assertLess(written, 27_954,
                        "a compact scope that is not smaller than the full packet is not compact")

    def test_the_limits_are_named_constants_a_reviewer_cannot_talk_past(self):
        self.assertIsInstance(lib.MAX_COMPACT_SCOPE_LINES, int)
        self.assertIsInstance(lib.MAX_COMPACT_SCOPE_ENTRIES, int)
        self.assertLessEqual(lib.MAX_COMPACT_SCOPE_ENTRIES * lib.MAX_COMPACT_SCOPE_LINES, 10_000,
                             "the worst-case compact review must still be compact")


if __name__ == "__main__":
    unittest.main()
