"""#318: a run-owned dashboard does not outlive the root it serves.

Observed on 2026-09-24: pid 10599 held port 8790 for one day and seventeen
hours serving `/Users/moncyabraham/Projects/project-handsoff-lane-251`, a
worktree that no longer existed, answering `/api/dashboard` with `state`
and `phase` both null because the files it reads had been deleted
underneath it.

A run closed properly already releases its dashboard, and that works: four
lane dashboards released themselves during the session that found this.
The gap is the case where the worktree is removed without `run-close`
first. Fleet forgets the root after one pass and self-heals; nothing did
the same for the process.

The check lives in the clock thread #295 added, which already ticks for
the server's life. That thread's contract is that it must NOT retire at a
pause, so these tests pin the distinction: a paused run keeps its
worktree and must never trigger retirement, while a removed worktree has
no next episode to watch for.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402


class _Probe:
    """The orphan check, exercised without binding a socket.

    `DashboardServer.__init__` starts threads and takes a port; this
    borrows the unbound method so the decision itself is what is tested.
    """

    def __init__(self, root, port=8790):
        self.project_root = root
        self._missing_root_ticks = 0
        self.server_address = ("127.0.0.1", port)
        self.stop_requests = 0

    def request_stop(self):
        self.stop_requests += 1

    def orphaned(self):
        return dashboard.DashboardServer._orphaned_root(self)


class ARemovedRootRetiresTheServer(unittest.TestCase):
    """REQ-001: the reported symptom, and only after real evidence."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-orphan-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.probe = _Probe(self.root)

    def test_one_missing_tick_is_not_enough(self):
        """A single stat is not evidence a directory is gone for good."""
        shutil.rmtree(self.root)
        self.assertFalse(self.probe.orphaned())
        self.assertEqual(self.probe.stop_requests, 0,
                         "a board must not retire on one observation")

    def test_two_consecutive_missing_ticks_retire_it(self):
        shutil.rmtree(self.root)
        self.probe.orphaned()
        self.assertTrue(self.probe.orphaned())
        self.assertEqual(self.probe.stop_requests, 1)

    def test_the_count_resets_when_the_root_comes_back(self):
        """A worktree being replaced must not kill a live board."""
        shutil.rmtree(self.root)
        self.assertFalse(self.probe.orphaned())
        self.root.mkdir()
        self.assertFalse(self.probe.orphaned())
        self.assertEqual(self.probe._missing_root_ticks, 0)
        shutil.rmtree(self.root)
        self.assertFalse(self.probe.orphaned(), "the counter restarted, so one tick again")
        self.assertEqual(self.probe.stop_requests, 0)

    def test_a_present_root_never_retires_it(self):
        for _ in range(10):
            self.assertFalse(self.probe.orphaned())
        self.assertEqual(self.probe.stop_requests, 0)

    def test_retiring_is_idempotent_across_further_ticks(self):
        shutil.rmtree(self.root)
        self.probe.orphaned()
        self.probe.orphaned()
        self.probe.orphaned()
        self.assertGreaterEqual(self.probe.stop_requests, 1)

    def test_the_message_names_the_root_and_the_port(self):
        import io
        import contextlib
        shutil.rmtree(self.root)
        self.probe.orphaned()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.probe.orphaned()
        printed = buffer.getvalue()
        self.assertIn("HANDSOFF_DASHBOARD_ORPHANED", printed)
        self.assertIn(str(self.root), printed)
        self.assertIn("8790", printed)


class ThePauseContractIsPreserved(HandsoffTestCase):
    """REQ-001: #295's reason for the thread must survive this change.

    #295 exists because the clock retired at the first pause and every
    episode after the first went unobserved. The orphan exit must not
    recreate that, so it keys on the root directory and never on run
    state: a paused run keeps its worktree.
    """

    def test_a_paused_run_keeps_its_root_and_is_not_retired(self):
        probe = _Probe(self.tmp)
        self.init("Paused run")
        for _ in range(5):
            self.assertFalse(probe.orphaned())
        self.assertEqual(probe.stop_requests, 0)

    def test_the_check_reads_no_run_state_at_all(self):
        """Derived from the source: if it consulted status, a closed or
        paused run could be mistaken for a departed one."""
        import ast
        source = (BIN / "handsoff_dashboard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "_orphaned_root")
        body = ast.get_source_segment(source, function) or ""
        for forbidden in ("load_unique_json", "status_path", "run_closed", "human_pause",
                          "performance", "load_config"):
            self.assertNotIn(forbidden, body,
                             f"_orphaned_root must not consult run state ({forbidden})")

    def test_a_status_file_missing_from_a_live_root_is_not_an_orphan(self):
        """A run mid-write can lack the file for an instant; the directory
        is the signal, not its contents."""
        probe = _Probe(self.tmp)
        self.init("Mid write")
        (self.tmp / "handsoff-status.json").unlink()
        self.assertFalse(probe.orphaned())
        self.assertFalse(probe.orphaned())
        self.assertEqual(probe.stop_requests, 0)


class TheClockLoopStillGuardsItsOriginalContract(unittest.TestCase):
    """REQ-001, derived from the source."""

    def _loop_source(self):
        import ast
        source = (BIN / "handsoff_dashboard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_performance_clock_loop")
        return ast.get_source_segment(source, function) or ""

    def test_the_loop_still_ticks_the_performance_clock(self):
        self.assertIn("performance_tick", self._loop_source(),
                      "the orphan check must not have displaced the clock")

    def test_the_loop_documents_why_this_early_exit_is_allowed(self):
        body = self._loop_source()
        self.assertIn("#318", body)
        self.assertIn("#295", body,
                      "the pause contract must be restated where the new exit sits")

    def test_the_threshold_is_a_named_constant(self):
        self.assertEqual(dashboard.ORPHAN_ROOT_MISSING_TICKS, 2)


if __name__ == "__main__":
    unittest.main()
