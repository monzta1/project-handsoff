"""#317: one rule decides what an archive is, and readers are registered.

Three readers of the same files disagreed. The Miner's `classify_archive`
was correct: an explicit `run_kind` wins, and without one the repo name, or
the file name when the repo is absent, decides by prefix. Run over the 100
archives written before #49 added the field, it calls 81 test and 19
product.

`lib.tokens_per_ticket` excluded only an exact `run_kind` of `"test"`, so
all 100 passed as product, the 81 fixtures included, and those figures ride
on every Fleet card. A strict classifier written for an earlier lane went
the other way and excluded all 100, the 19 real runs included.

The rule is now shared. The harder half is that it stays shared: a reader
that classifies without registering itself fails this suite rather than
quietly becoming a fourth answer.
"""
import ast
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_analyzer as analyzer  # noqa: E402

ROOT = BIN.parent


class TheOracleIsStatedPerCase(unittest.TestCase):
    """REQ-002. Every shape the real archives contain, with its answer."""

    def test_an_explicit_kind_wins(self):
        self.assertEqual(lib.classify_archive_record({"run_kind": "product"}), "product")
        self.assertEqual(lib.classify_archive_record({"run_kind": "test"}), "test")

    def test_an_explicit_kind_beats_a_contradicting_name(self):
        """A run that declares itself is believed over its own repo name."""
        self.assertEqual(
            lib.classify_archive_record({"run_kind": "product", "repo": "handsoff-test-abc"}),
            "product")

    def test_an_absent_kind_falls_back_to_a_fixture_shaped_repo(self):
        self.assertEqual(lib.classify_archive_record({"repo": "handsoff-test-0e13kpp3"}), "test")

    def test_an_absent_kind_falls_back_to_a_product_repo(self):
        self.assertEqual(lib.classify_archive_record({"repo": "fm9-tone-109-device-handle"}), "product")

    def test_with_no_repo_at_all_the_file_name_decides(self):
        self.assertEqual(lib.classify_archive_record({}, "handsoff-selfcheck-x.json"), "test")
        self.assertEqual(lib.classify_archive_record({}, "sentinel-lane-3.json"), "product")

    def test_an_unexpected_value_is_not_honoured(self):
        """Trusting it would let a typo or a hand-edited archive declare
        itself product. Falling through to the name keeps the decision on
        evidence the archive cannot fake about itself."""
        for kind in ("", "PRODUCT", "prod", 123, None, ["product"]):
            self.assertEqual(
                lib.classify_archive_record({"run_kind": kind, "repo": "handsoff-test-z"}), "test",
                f"{kind!r} was honoured instead of falling through")
            self.assertEqual(
                lib.classify_archive_record({"run_kind": kind, "repo": "fm9-tone"}), "product")

    def test_a_record_that_is_not_a_mapping_still_classifies(self):
        self.assertEqual(lib.classify_archive_record(None, "handsoff-test-x.json"), "test")
        self.assertEqual(lib.classify_archive_record("nonsense", "fm9-tone.json"), "product")

    def test_every_fixture_prefix_is_honoured(self):
        for prefix in lib.FIXTURE_ROOT_PREFIXES:
            self.assertEqual(lib.classify_archive_record({"repo": prefix + "whatever"}), "test", prefix)


class TheLegacyArchivesAreClassifiedAsTheMinerDoes(unittest.TestCase):
    """REQ-002: pinned to the real data, not to invented cases.

    The 100 archives from 12 to 14 September carry no `run_kind`. The Miner
    calls 81 of them test and 19 product; the shared rule must agree.
    """

    def test_a_legacy_fixture_archive_is_test(self):
        self.assertEqual(
            lib.classify_archive_record(
                {"repo": "handsoff-test-0e13kpp3", "status": {"status": "complete"}},
                "handsoff-test-0e13kpp3-20260913-101010-x.json"),
            "test")

    def test_a_legacy_product_archive_is_product(self):
        self.assertEqual(
            lib.classify_archive_record(
                {"repo": "fm9-tone-109-device-handle", "status": {"status": "complete"}},
                "fm9-tone-109-device-handle-20260914-025803-t.json"),
            "product")


class ReadersAreRegisteredNotRemembered(unittest.TestCase):
    """REQ-001. The half that stops the rule fragmenting again."""

    def test_the_registry_names_the_known_readers(self):
        self.assertIn("handsoff_lib.tokens_per_ticket", lib.ARCHIVE_CLASSIFICATION_READERS)
        self.assertIn("miner.analyzer.classify_archive", lib.ARCHIVE_CLASSIFICATION_READERS)

    def test_the_writer_is_declared_with_its_reason(self):
        self.assertIn("handsoff_lib.run_kind_for", lib.ARCHIVE_CLASSIFICATION_WRITERS)
        for name, reason in lib.ARCHIVE_CLASSIFICATION_WRITERS.items():
            self.assertGreaterEqual(len(reason), 20, f"{name} has no stated reason")

    def test_every_registered_reader_names_the_shared_callable(self):
        for reader, callable_name in lib.ARCHIVE_CLASSIFICATION_READERS.items():
            self.assertEqual(callable_name, "classify_archive_record", reader)
            self.assertTrue(hasattr(lib, callable_name))

    def test_tokens_per_ticket_uses_the_shared_rule_rather_than_its_own(self):
        """Derived from the source: the old filter compared run_kind to the
        literal "test", which is what counted 81 fixture runs as product."""
        source = (BIN / "handsoff_lib.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "tokens_per_ticket")
        body = ast.get_source_segment(source, function) or ""
        self.assertIn("classify_archive_record", body,
                      "tokens_per_ticket does not use the shared rule")
        self.assertNotIn('run_kind") == "test"', body,
                         "tokens_per_ticket still carries its own lenient filter")

    def test_the_analyzer_uses_the_shared_rule_rather_than_its_own(self):
        """The analyzer carried the identical lenient filter: an exact
        run_kind of "test". Its findings feed the Miner, so 81 fixture
        archives were being scored by lane rules and filed as tickets."""
        source = (BIN / "handsoff_analyzer.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "scan")
        body = ast.get_source_segment(source, function) or ""
        self.assertIn("classify_archive_record", body,
                      "handsoff_analyzer.scan does not use the shared rule")
        self.assertNotIn('run_kind") == "test"', body,
                         "handsoff_analyzer.scan still carries its own lenient filter")

    def test_every_function_touching_run_kind_is_declared(self):
        """Closed set, both directions.

        The first version of this test required a function to mention
        FIXTURE_ROOT_PREFIXES before it counted, which only caught readers
        already applying the prefix rule. The bug's actual shape is the
        opposite: read run_kind, ignore the prefix rule. `handsoff_analyzer
        .scan` had exactly that and passed. The signal is touching
        run_kind at all, not touching it a particular way.
        """
        declared = {}
        for name, callable_name in lib.ARCHIVE_CLASSIFICATION_READERS.items():
            declared[name] = f"reader via {callable_name}"
        for name, reason in lib.ARCHIVE_CLASSIFICATION_WRITERS.items():
            declared[name] = f"writer: {reason}"
        # #300: the rule moved down to handsoff_config so handsoff_evidence,
        # which sits above config and below the monolith, can use it instead
        # of a private `run_kind == "test"` comparison.
        declared["handsoff_config.classify_archive_record"] = "the shared rule itself"

        modules = {path.stem for path in BIN.glob("handsoff_*.py")}
        expected = {name for name in declared if name.split(".")[0] in modules}

        # Either route into the decision counts. Naming run_kind is a
        # function deciding for itself; calling the shared rule is one
        # deciding correctly. Both are participants, and a participant that
        # switches route must not silently leave the registry -- which is
        # what the run_kind-only version of this check allowed the moment
        # the analyzer was fixed.
        found = set()
        for path in sorted(BIN.glob("handsoff_*.py")):
            source = path.read_text(encoding="utf-8")
            if "run_kind" not in source and "classify_archive_record" not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                if "run_kind" in segment or "classify_archive_record" in segment:
                    found.add(f"{path.stem}.{node.name}")

        self.assertEqual(
            found, expected,
            "every function that touches run_kind must be a registered reader "
            "or a declared writer; undeclared: "
            f"{sorted(found - expected)}; declared but absent: {sorted(expected - found)}")

    def test_the_registry_covers_the_analyzer(self):
        self.assertIn("handsoff_analyzer.scan", lib.ARCHIVE_CLASSIFICATION_READERS)


class TheAnalyzerSurvivesAMalformedArchive(unittest.TestCase):
    """REQ-001 regression.

    Swapping the shared rule into `scan` replaced the whole condition and
    dropped its `not isinstance(record, dict)` guard. classify_archive_record
    deliberately tolerates a non-dict record, so the guard is not redundant:
    without it a list-shaped archive whose name reads product falls through
    to `record.get("status")` and raises AttributeError, taking down the
    Miner's whole scan over one malformed file.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_list_shaped_product_archive_does_not_crash_the_scan(self):
        (self.tmp / "fm9-tone-run.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(analyzer.scan(self.tmp), [])

    def test_a_scalar_product_archive_does_not_crash_the_scan(self):
        (self.tmp / "sentinel-run.json").write_text('"a string"', encoding="utf-8")
        self.assertEqual(analyzer.scan(self.tmp), [])

    def test_a_malformed_archive_does_not_hide_a_real_finding_beside_it(self):
        """The guard skips the bad file, it does not abandon the scan."""
        (self.tmp / "fm9-tone-bad.json").write_text("[1, 2, 3]", encoding="utf-8")
        (self.tmp / "fm9-tone-good.json").write_text(json.dumps({
            "repo": "fm9-tone", "run_kind": "product",
            "status": {"lane": "review", "phases_run": [6]},
        }), encoding="utf-8")
        findings = analyzer.scan(self.tmp)
        self.assertEqual([f["rule"] for f in findings], ["R10"])


class TheFiguresMove(unittest.TestCase):
    """REQ-004's measured basis: the correction is real and bounded."""

    def test_a_fixture_archive_no_longer_counts(self):
        """The shape of the 81: no run_kind, a fixture-shaped repo."""
        self.assertEqual(
            lib.classify_archive_record({"repo": "handsoff-test-abc"}, "handsoff-test-abc.json"),
            "test")

    def test_the_old_lenient_rule_would_have_counted_it(self):
        """Pins what changed: the previous filter excluded only an exact
        "test", so this archive passed as product."""
        record = {"repo": "handsoff-test-abc"}
        self.assertNotEqual(record.get("run_kind"), "test")
        self.assertEqual(lib.classify_archive_record(record, "handsoff-test-abc.json"), "test")


if __name__ == "__main__":
    unittest.main()
