"""bin/handsoff_regress.py: the Regression Console's runner. The parser is
exercised line by line, then the tool runs a real fixture module so the
progress file, totals and exit code are checked end to end."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_regress as regress  # noqa: E402


def feed(lines: list[str]) -> list[dict]:
    events, pending = [], None
    for line in lines:
        event, pending = regress.parse_line(line, pending)
        if event:
            events.append(event)
    return events


class TestParseLine(unittest.TestCase):
    def test_single_line_results(self):
        events = feed([
            "test_a (tests.test_x.TestOne.test_a) ... ok",
            "test_b (tests.test_x.TestOne.test_b) ... FAIL",
            "test_c (tests.test_x.TestOne.test_c) ... ERROR",
            "test_d (tests.test_x.TestOne.test_d) ... skipped 'no gh'",
            "test_e (tests.test_x.TestOne.test_e) [sub=1] ... ok",
        ])
        self.assertEqual([(e["name"], e["result"]) for e in events], [
            ("tests.test_x.TestOne.test_a", "ok"), ("tests.test_x.TestOne.test_b", "FAIL"),
            ("tests.test_x.TestOne.test_c", "ERROR"), ("tests.test_x.TestOne.test_d", "skipped 'no gh'"),
            ("tests.test_x.TestOne.test_e", "ok"),
        ])

    def test_python_311_shape_without_the_method_in_the_id(self):
        events = feed(["test_a (tests.test_x.TestOne) ... ok"])
        self.assertEqual(events, [{"name": "tests.test_x.TestOne.test_a", "result": "ok"}])

    def test_docstring_two_line_result(self):
        events = feed([
            "test_doc (tests.test_x.TestOne.test_doc)",
            "First docstring line explains the case ... ok",
        ])
        self.assertEqual(events, [{"name": "tests.test_x.TestOne.test_doc", "result": "ok"}])

    def test_leaked_child_output_then_result_on_its_own_line(self):
        events = feed([
            "test_leak (tests.test_x.TestOne.test_leak) ... HANDSOFF_BROKER_REQUEST: {\"action\": \"workflow\"}",
            "DESIGN_APPROVAL_RECORDED",
            "----------------------------------------",
            "ok",
            "test_next (tests.test_x.TestOne.test_next) ... FAIL",
        ])
        self.assertEqual([(e["name"], e["result"]) for e in events], [
            ("tests.test_x.TestOne.test_leak", "ok"), ("tests.test_x.TestOne.test_next", "FAIL"),
        ])

    def test_leaked_line_merely_ending_in_a_result_word_is_not_a_verdict(self):
        events = feed([
            "test_leak (tests.test_x.TestOne.test_leak) ... status: looks ok",
            "everything ok",
            "FAIL",
        ])
        self.assertEqual(events, [{"name": "tests.test_x.TestOne.test_leak", "result": "FAIL"}])

    def test_result_word_with_nothing_pending_is_ignored(self):
        self.assertEqual(feed(["ok", "FAIL", "random ok"]), [])

    def test_node_lines(self):
        events = feed([
            "ok 1 - renders the clock",
            "not ok 2 - approve button posts",
            "ok 3 - skipped one # SKIP no browser",
        ])
        self.assertEqual([(e["name"], e["node"]) for e in events], [
            ("renders the clock", "ok"), ("approve button posts", "not ok"), ("skipped one", "skip"),
        ])

    def test_count_event_totals_and_bounded_failures(self):
        entry = {"done": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failures": [], "current": None}
        for event in feed([
            "test_a (m.T.test_a) ... ok", "test_b (m.T.test_b) ... FAIL", "test_c (m.T.test_c) ... ERROR",
            "test_d (m.T.test_d) ... skipped 'x'", "test_e (m.T.test_e) ... expected failure",
            "test_f (m.T.test_f) ... unexpected success", "not ok 1 - node case",
        ]):
            regress.count_event(entry, event)
        self.assertEqual({k: entry[k] for k in ("done", "passed", "failed", "errors", "skipped")},
                         {"done": 7, "passed": 2, "failed": 3, "errors": 1, "skipped": 1})
        self.assertEqual([f["name"] for f in entry["failures"]],
                         ["m.T.test_b", "m.T.test_c", "m.T.test_f", "node case"])
        self.assertEqual(entry["current"], "node case")


class TestRunBattery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-regress-"))
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "__init__.py").write_text("")
        (self.tmp / "tests" / "test_fixture.py").write_text(textwrap.dedent('''
            import sys, unittest

            class TestFixture(unittest.TestCase):
                def test_passes(self):
                    pass

                def test_leaks_then_passes(self):
                    """Leaks a line that ends in the word ok before finishing."""
                    sys.stdout.write("child says: looks ok\\n")
                    sys.stdout.flush()

                def test_fails(self):
                    self.assertEqual(1, 2)

                @unittest.skip("fixture skip")
                def test_skipped(self):
                    pass
        '''))
        for name in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / name, self.tmp / name)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_progress_file_totals_and_exit_code(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "handsoff_regress.py"), "--root", str(self.tmp),
             "--command", "python3 -m unittest tests.test_fixture"],
            capture_output=True, text=True, timeout=120, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("HANDSOFF_REGRESS_FAILED: 2 passed, 1 failed, 0 errors, 1 skipped", proc.stdout)
        state = json.loads((self.tmp / regress.PROGRESS_FILE).read_text())
        self.assertEqual(state["label"], "ad hoc")
        self.assertIsNotNone(state["finished_at"])
        self.assertEqual(state["exit_code"], 1)
        self.assertEqual(state["totals"], {"total": 4, "done": 4, "passed": 2, "failed": 1, "errors": 0, "skipped": 1})
        command = state["commands"][0]
        self.assertEqual(command["exit_code"], 1)
        self.assertEqual(command["mode"], "sharded")
        self.assertEqual([f["name"] for f in command["failures"]], ["tests.test_fixture.TestFixture.test_fails"])
        self.assertIsNone(command["current"])
        self.assertEqual(len(command["shards"]), 4)
        self.assertEqual([shard["test_count"] for shard in command["shards"]], [1, 1, 1, 1])
        self.assertTrue(all(shard["done"] == 1 for shard in command["shards"]))
        self.assertTrue(all(Path(shard["log"]).is_file() for shard in command["shards"]))
        seen = set()
        for shard in command["shards"]:
            pending = None
            for line in Path(shard["log"]).read_text(encoding="utf-8").splitlines():
                event, pending = regress.parse_line(line, pending)
                if event:
                    seen.add(event["name"])
        self.assertEqual(seen, {
            "tests.test_fixture.TestFixture.test_fails",
            "tests.test_fixture.TestFixture.test_leaks_then_passes",
            "tests.test_fixture.TestFixture.test_passes",
            "tests.test_fixture.TestFixture.test_skipped",
        })

    def test_enumeration_ambiguity_falls_back_to_one_sequential_worker(self):
        command = "python3 -m unittest tests.test_fixture"
        with mock.patch.object(regress, "_enumerate_unittest_ids",
                               return_value=(None, "discovery import error")):
            results = regress.run_battery_results(
                self.tmp, "fallback", [command], timeout=120,
                request_id="rg-fallback", command_sha256="a" * 64,
            )
        self.assertEqual(results[0]["exit_code"], 1)
        self.assertEqual(len(results[0]["shards"]), 1)
        state = json.loads((self.tmp / regress.PROGRESS_FILE).read_text())
        self.assertEqual(state["request_id"], "rg-fallback")
        self.assertEqual(state["command_sha256"], "a" * 64)
        entry = state["commands"][0]
        self.assertEqual(entry["mode"], "sequential")
        self.assertEqual(entry["fallback_reason"], "discovery import error")
        self.assertEqual(len(entry["shards"]), 1)
        self.assertEqual(entry["done"], 4)

    def test_unknown_group_is_refused(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "handsoff_regress.py"), "--root", str(self.tmp), "--group", "nope"],
            capture_output=True, text=True, timeout=60, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("HANDSOFF_REGRESS_BLOCKED", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
