"""#321: no test can write the real archive, and the guard names what did.

Three suites reached an archive write without redirecting
`HANDSOFF_ARCHIVE_DIR` and wrote nine files per run into the operator's
real `~/Documents/Handsoff-Archive`, which is the Miner's input:
`test_report_posting` (7), `test_offline_and_closed` (1),
`test_snapshot_contract` (1). Measured by redirecting each suite to its
own directory and counting what landed.

All three already inherit `HandsoffTestCase`, so the redirect belongs in
that base rather than in each suite: fixing the three would leave the
fourth to be written later.

The second half is attribution. `TestVerificationCache` always redirected
its own archive, so it was never the writer, yet its guard reported the
failure, because a count taken before and after one test also catches a
write from a parallel shard or from a subprocess an earlier test left
running. That is why a single-process run and a sharded run disagreed
about how many tests failed, and why the named test moved between runs.
"""
import os
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

REAL_ARCHIVE = Path.home() / "Documents" / "Handsoff-Archive"


class TheRedirectIsStructural(HandsoffTestCase):
    """REQ-003. Inheriting the base is enough; remembering is not required."""

    def test_the_archive_is_redirected_away_from_documents(self):
        self.assertEqual(os.environ.get("HANDSOFF_ARCHIVE_DIR"),
                         str(self.base_archive_dir))

    def test_archive_dir_never_resolves_under_the_real_documents_path(self):
        """The assertion that matters: not that a variable is set, but that
        the function every writer calls answers somewhere safe."""
        resolved = lib.archive_dir().resolve()
        self.assertNotEqual(resolved, REAL_ARCHIVE.resolve())
        self.assertFalse(
            str(resolved).startswith(str((Path.home() / "Documents").resolve())),
            f"archive_dir() resolved under the operator's Documents: {resolved}")

    def test_the_redirect_points_at_a_directory_this_test_owns(self):
        self.assertTrue(self.base_archive_dir.is_dir())
        self.assertNotEqual(self.base_archive_dir.resolve(), REAL_ARCHIVE.resolve())

    def test_a_write_through_the_public_path_lands_in_the_temp_directory(self):
        """Proves the redirect is load-bearing rather than cosmetic."""
        (lib.archive_dir() / "probe.json").write_text("{}", encoding="utf-8")
        self.assertTrue((self.base_archive_dir / "probe.json").is_file())


class TheRedirectIsRestored(unittest.TestCase):
    """REQ-003. A base that leaks its own redirect is its own defect."""

    def test_an_unset_variable_is_restored_to_unset(self):
        before = os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
        try:
            case = TheRedirectIsStructural("test_the_archive_is_redirected_away_from_documents")
            case.setUp()
            self.assertIn("HANDSOFF_ARCHIVE_DIR", os.environ)
            case.tearDown()
            self.assertNotIn("HANDSOFF_ARCHIVE_DIR", os.environ,
                             "a previously unset variable must be removed, not left set")
        finally:
            if before is not None:
                os.environ["HANDSOFF_ARCHIVE_DIR"] = before

    def test_an_existing_value_is_restored_exactly(self):
        before = os.environ.get("HANDSOFF_ARCHIVE_DIR")
        os.environ["HANDSOFF_ARCHIVE_DIR"] = "/tmp/some-operator-choice"
        try:
            case = TheRedirectIsStructural("test_the_archive_is_redirected_away_from_documents")
            case.setUp()
            case.tearDown()
            self.assertEqual(os.environ.get("HANDSOFF_ARCHIVE_DIR"), "/tmp/some-operator-choice")
        finally:
            if before is None:
                os.environ.pop("HANDSOFF_ARCHIVE_DIR", None)
            else:
                os.environ["HANDSOFF_ARCHIVE_DIR"] = before


class TheGuardNamesWhatAppeared(HandsoffTestCase):
    """REQ-004. A count says something appeared; it cannot say what."""

    def test_the_failure_message_names_the_file(self):
        before = frozenset({"already-there.json"})

        class Fake:
            pass

        # Drive the shared assertion against a directory we control, so the
        # test proves the message without writing the operator's archive.
        self.documents_archive = self.base_archive_dir
        (self.base_archive_dir / "leaked-run.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(AssertionError) as caught:
            self.assertRealArchiveUnwritten(before)
        message = str(caught.exception)
        self.assertIn("leaked-run.json", message,
                      "the guard must name the file that appeared")

    def test_the_message_says_the_writer_may_be_another_process(self):
        self.documents_archive = self.base_archive_dir
        (self.base_archive_dir / "from-a-parallel-shard.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(AssertionError) as caught:
            self.assertRealArchiveUnwritten(frozenset())
        self.assertIn("another process", str(caught.exception))

    def test_an_unchanged_directory_passes(self):
        self.documents_archive = self.base_archive_dir
        self.assertRealArchiveUnwritten(self._documents_archive_names())

    def test_a_file_that_was_already_there_is_not_reported(self):
        self.documents_archive = self.base_archive_dir
        (self.base_archive_dir / "pre-existing.json").write_text("{}", encoding="utf-8")
        self.assertRealArchiveUnwritten(self._documents_archive_names())


class TheMeasuredLeakersAreCovered(unittest.TestCase):
    """REQ-003: the three measured suites inherit the base that redirects."""

    def test_each_measured_leaking_suite_derives_from_the_base(self):
        import importlib
        for name in ("test_report_posting", "test_offline_and_closed", "test_snapshot_contract"):
            module = importlib.import_module(f"tests.{name}")
            cases = [obj for obj in vars(module).values()
                     if isinstance(obj, type) and issubclass(obj, unittest.TestCase)
                     and obj.__module__ == module.__name__]
            self.assertTrue(cases, f"{name} declares no test cases")
            for case in cases:
                self.assertTrue(
                    issubclass(case, HandsoffTestCase),
                    f"{name}.{case.__name__} writes archives but does not inherit the redirect")


if __name__ == "__main__":
    unittest.main()
