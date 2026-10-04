"""#371: the guard run, the cheap check between the last edit and a push.

A guard is a test case that reads engine source as text. `python3 -m
tests.guards` runs exactly the marked cases, so a stale count or a missing
registry entry turns red locally instead of on a CI round. Two things keep
that honest. The runner fails closed: an import or discovery error, an
empty run, or a skipped guard is a failure that writes no record, and the
ids it reports are the ones that actually started, compared with the marker
set. The scanner fails any source-reading case that is not marked, so the
guard run cannot silently shrink.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import guards
from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import HandsoffTestCase, run

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "guards"
RUNNER_FIXTURES = FIXTURES / "runner"
SCANNER_TESTS = FIXTURES / "scanner" / "tests"

POSITIVE = {
    "tests.test_positive." + case for case in (
        "DirectRead.test_direct_read",
        "SetUpRead.test_uses_the_setup_text",
        "SetUpRead.test_ignores_the_setup_text",
        "SetUpClassRead.test_uses_the_class_text",
        "InheritedSetUp.test_inherits_the_setup_read",
        "InheritedSetUp.test_uses_the_setup_text",
        "InheritedSetUp.test_ignores_the_setup_text",
        "HelperReads.test_two_hop_helper",
        "HelperReads.test_helper_imported_from_another_tests_module",
        "HelperReads.test_helper_through_a_module_attribute",
        "HelperReads.test_parameter_bound_helper",
        "HelperReads.test_method_helper",
        "HelperReads.test_keyword_bound_helper",
        "HelperReads.test_instance_method_bound_helper",
        "ConstantReads.test_constant_naming_a_bin_file",
        "ConstantReads.test_path_alias",
        "ConstantReads.test_module_level_source_constant",
        "ConstantReads.test_constant_from_another_tests_module",
        "ConstantReads.test_git_toplevel_root",
        "ConstantReads.test_local_alias",
        "ConstantReads.test_glob_of_bin",
        "ConstantReads.test_os_path_join",
        "ConstantReads.test_inspect_getsource",
        "ConstantReads.test_engine_module_file",
        "ConstantReads.test_literal_bin_path",
    )
}


def run_guards(root: Path, package: str = "tests", ids_out: Path | None = None):
    argv = [sys.executable, "-m", "tests.guards", "--root", str(root), "--package", package]
    if ids_out is not None:
        argv += ["--ids-out", str(ids_out)]
    started = time.monotonic()
    result = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=600,
                            env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"})
    return result, time.monotonic() - started


_REPO_RUN = {}


def run_guards_on_this_repository():
    """One real guard run on this checkout, shared by the tests that need it."""
    if not _REPO_RUN:
        with tempfile.TemporaryDirectory() as scratch:
            ids = Path(scratch) / "ids.json"
            result, seconds = run_guards(REPO, "tests", ids)
            report = json.loads(ids.read_text()) if ids.exists() else None
        _REPO_RUN.update(result=result, seconds=seconds, report=report)
    return _REPO_RUN["result"], _REPO_RUN["seconds"], _REPO_RUN["report"]


class FixtureRun(unittest.TestCase):
    """Copies one runner fixture to a scratch root and runs the guard command on it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-guards-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_fixture(self, name: str):
        root = self.tmp / name
        shutil.copytree(RUNNER_FIXTURES / name, root)
        ids = self.tmp / f"{name}-ids.json"
        result, _ = run_guards(root, "gtests", ids)
        output = result.stdout + result.stderr
        executed = json.loads(ids.read_text())["executed"] if ids.exists() else None
        return result.returncode, output, executed, root / guards.RECORD

    def assert_failed_without_record(self, name: str, *named: str):
        code, output, _, record = self.run_fixture(name)
        self.assertNotEqual(code, 0, output)
        self.assertIn("guards FAILED", output)
        for text in named:
            self.assertIn(text, output)
        self.assertFalse(record.exists(), f"{name}: a failing run wrote {record}")
        return output


class TheRunnerExecutesExactlyTheMarkedCases(FixtureRun):
    def test_a_passing_run_executes_only_the_marked_cases_and_writes_the_record(self):
        code, output, executed, record = self.run_fixture("passing")
        self.assertEqual(code, 0, output)
        self.assertEqual(executed, ["gtests.test_pass.Guards.test_the_engine_defines_a_handler",
                                    "gtests.test_pass.MarkedClass.test_the_engine_is_not_empty"])
        self.assertTrue(record.exists(), "a passing run writes the record, so its absence elsewhere means something")
        self.assertEqual(json.loads(record.read_text())["executed"], 2)

    def test_executed_ids_on_this_repository_equal_the_marker_set(self):
        result, seconds, report = run_guards_on_this_repository()
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
        executed = set(report["executed"])
        print(f"\nguards on this repository: {len(executed)} cases in {seconds:.1f}s", file=sys.stderr)
        self.assertTrue(executed, "no guard executed")
        self.assertEqual(executed, guards.marked_cases(REPO / "tests"),
                         "the executed ids must be the cases the source marks, read statically")
        self.assertEqual(executed, set(report["marked"]))


class TheRunnerFailsClosed(FixtureRun):
    def test_a_stale_count_fails_naming_the_guard(self):
        self.assert_failed_without_record(
            "stale_count", "gtests.test_count.HandlerCount.test_the_engine_has_two_handlers failed")

    def test_a_missing_registry_entry_fails_naming_the_guard(self):
        output = self.assert_failed_without_record(
            "missing_registry", "gtests.test_registry.Registry.test_every_engine_module_is_registered failed")
        self.assertIn("bin/routing.py has no registry entry", output)

    def test_an_undeclared_json_write_fails_naming_the_guard(self):
        output = self.assert_failed_without_record(
            "undeclared_write", "gtests.test_writes.JsonWrites.test_every_json_write_is_declared failed")
        self.assertIn("cache.json is written but not declared", output)

    def test_an_import_error_fails_even_when_every_guard_passes(self):
        self.assert_failed_without_record("import_error", "gtests.test_broken: import failed")

    def test_a_discovery_error_fails_even_when_every_guard_passes(self):
        self.assert_failed_without_record("discovery_error", "gtests.test_load: discovery failed")

    def test_a_guard_load_tests_leaves_out_still_counts_as_marked(self):
        self.assert_failed_without_record(
            "omitted_guard", "executed ids differ from the marker set",
            "gtests.test_omit.Guards.test_omitted_guard")

    def test_zero_executed_guards_fails(self):
        self.assert_failed_without_record("no_guards", "no guard executed")

    def test_a_skipped_guard_counts_as_not_run(self):
        self.assert_failed_without_record(
            "skipped", "gtests.test_skip.Guards.test_skipped_guard not run (skipped: not on this platform)")


class TheRecordIsBoundToTheTreeTheRunSaw(FixtureRun):
    """REQ-003: the digest is taken before the run and compared after it."""

    def test_an_edit_during_the_run_leaves_no_record_and_removes_an_old_one(self):
        root = self.tmp / "edit_during_run"
        shutil.copytree(RUNNER_FIXTURES / "edit_during_run", root)
        old = guards.write_record(root, guards._digest(root), ["gtests.earlier.run"], 1)
        result, _ = run_guards(root, "gtests")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("guards FAILED: the tree changed during the run", output)
        self.assertNotIn("test_the_engine_defines_a_handler_and_then_changes failed", output,
                         "the guard itself passes; only the drift may fail this run")
        self.assertFalse(old.exists(), "a run that saw the tree move left a record behind")


GH_SHIM = """#!/bin/sh
echo "$*" >> '{log}'
case "$1 $2" in
  "pr view") echo '{{"number": 7, "url": "https://example.invalid/pull/7", "headRefOid": "abc123def4567890", "title": "Guard record", "body": ""}}' ;;
  *) echo '[]' ;;
esac
"""


class CiWatchStartsOnlyOnTheTreeTheGuardsPassedOn(HandsoffTestCase):
    """REQ-003: ci-watch, through the CLI, against a guard run on a fixture project."""

    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        self.shim_dir = Path(tempfile.mkdtemp(prefix="handsoff-gh-shim-"))
        self.addCleanup(shutil.rmtree, self.shim_dir, True)
        self.gh_log = self.shim_dir / "gh-calls.log"
        shim = self.shim_dir / "gh"
        shim.write_text(GH_SHIM.format(log=self.gh_log))
        shim.chmod(0o755)
        path_before = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.shim_dir}{os.pathsep}{path_before}"
        self.addCleanup(os.environ.__setitem__, "PATH", path_before)
        self.init("ci-watch needs a guard record")
        for part in ("gtests", "bin"):
            shutil.copytree(RUNNER_FIXTURES / "passing" / part, self.tmp / part, dirs_exist_ok=True)

    def ci_watch(self):
        r = run(["ci-watch", "--pr", "7", "--by", "claude-host"], cwd=self.tmp)
        return r.returncode, r.stdout + r.stderr

    def test_a_passing_run_lets_ci_watch_start_and_a_later_edit_makes_it_refuse(self):
        code, output = self.ci_watch()
        self.assertEqual(code, 1, output)
        self.assertIn("CI_WATCH_BLOCKED: no passing guard run is recorded for this tree", output)
        self.assertIn("python3 -m tests.guards", output)
        self.assertFalse(self.gh_log.exists(), "ci-watch reached gh before the guard gate")

        result, _ = run_guards(self.tmp, "gtests")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.tmp / guards.RECORD).exists())
        code, output = self.ci_watch()
        self.assertEqual(code, 0, output)
        self.assertIn("CI_WATCH_STARTED: PR #7", output)
        # starting the watch writes only Handsoff state, so the record still holds
        code, output = self.ci_watch()
        self.assertEqual(code, 0, output)

        engine = self.tmp / "bin" / "engine.py"
        engine.write_text(engine.read_text() + "\ndef handle_two():\n    return 2\n")
        code, output = self.ci_watch()
        self.assertEqual(code, 1, output)
        self.assertIn("CI_WATCH_BLOCKED: the tree changed since the last passing guard run", output)
        self.assertIn("python3 -m tests.guards", output)

    def test_a_record_not_in_the_written_shape_counts_as_absent(self):
        result, _ = run_guards(self.tmp, "gtests")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.tmp / guards.RECORD
        record = json.loads(path.read_text())
        for bad in ({**record, "extra": 1}, {**record, "schema": 2}, {**record, "executed": 0},
                    {**record, "repository_digest": record["repository_digest"].upper()}):
            path.write_text(json.dumps(bad))
            code, output = self.ci_watch()
            self.assertEqual(code, 1, output)
            self.assertIn("no passing guard run is recorded for this tree", output)
        path.write_text(json.dumps(record))
        code, output = self.ci_watch()
        self.assertEqual(code, 0, output)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


WHEN = ("Run `python3 -m tests.guards` after the last edit and before every push, "
        "in the same step as the bump and docs before the first verify")


class TheDocsPutTheGuardRunBeforeEveryPush(unittest.TestCase):
    """REQ-004: both texts say when guards runs, and a run fits in two minutes."""

    def test_lanes_says_it_in_the_before_pushing_step(self):
        text = _flat(REPO / "playbook" / "lanes.md")
        start = text.index("**Before pushing.**")
        paragraph = text[start:text.index("**", start + len("**Before pushing.**"))]
        self.assertIn(WHEN, paragraph)
        self.assertIn("`ci-watch` refuses without its record", paragraph)

    def test_the_reference_says_it(self):
        text = _flat(REPO / "docs" / "REFERENCE.md")
        self.assertIn("#### The guard run before every push (#371)", text)
        self.assertIn(WHEN, text)
        self.assertIn("`ci-watch --pr N` refuses to start unless that record's digest equals the "
                      "current repository digest", text)

    def test_a_guard_run_on_this_repository_takes_under_120_seconds(self):
        result, seconds, report = run_guards_on_this_repository()
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
        print(f"\nguards runtime on this repository: {seconds:.1f}s "
              f"({len(report['executed'])} guards)", file=sys.stderr)
        self.assertLess(seconds, 120)
        record = json.loads((REPO / guards.RECORD).read_text())
        self.assertLess(record["duration_ms"], 120000)


class TheScannerFindsEverySourceReader(unittest.TestCase):
    def test_every_positive_fixture_is_reported_and_nothing_else(self):
        found = guards.scan(SCANNER_TESTS)
        self.assertEqual(set(found), POSITIVE,
                         "\n".join(f"{case}: {why}" for case, why in sorted(found.items())))

    def test_marked_readers_are_not_reported(self):
        marked = guards.marked_cases(SCANNER_TESTS)
        self.assertEqual(marked, {"tests.test_positive.Marked.test_marked_reader",
                                  "tests.test_positive.MarkedClass.test_class_marked"})
        self.assertFalse(marked & set(guards.scan(SCANNER_TESTS)))

    def test_negative_fixtures_are_not_flagged(self):
        flagged = [case for case in guards.scan(SCANNER_TESTS) if ".test_negative." in case]
        self.assertEqual(flagged, [])
        cases = [case for case, _, _ in guards.Scanner(SCANNER_TESTS).cases() if ".test_negative." in case]
        self.assertGreaterEqual(len(cases), 10, "the negative fixtures were not enumerated at all")

    def test_every_source_reading_case_in_this_repository_is_marked(self):
        unmarked = guards.scan(REPO / "tests")
        self.assertEqual(unmarked, {}, "source-reading cases without @guard:\n"
                         + "\n".join(f"  {case}: {why}" for case, why in sorted(unmarked.items())))

    def test_the_layer_suites_setup_and_helper_readers_are_marked(self):
        marked = guards.marked_cases(REPO / "tests")
        for case in ("tests.test_core_layer.TheCoreDependsOnNothingInTheEngine.test_it_imports_no_handsoff_module",
                     "tests.test_ledger_layer.TheLedgerSitsOnTheLayersBelowIt.test_it_imports_only_layers_beneath_it",
                     "tests.test_workflow_layer.NothingUnrelatedEntersTheBoundary.test_the_monolith_is_never_imported",
                     "tests.test_workflow_layer.TheReExportSurfaceIsExactlyTheMovedSet."
                     "test_the_lib_reexports_every_public_workflow_symbol"):
            self.assertIn(case, marked)


if __name__ == "__main__":
    unittest.main()
