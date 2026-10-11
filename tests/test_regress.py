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
from copy import deepcopy
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
        self.assertEqual(regress.DEFAULT_SHARDS, 5)
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
        self.assertEqual(len(command["shards"]), 5)
        self.assertEqual([shard["test_count"] for shard in command["shards"]], [1, 1, 1, 1, 0])
        self.assertEqual([shard["done"] for shard in command["shards"]], [1, 1, 1, 1, 0])
        self.assertTrue(all(shard["finished_at"] for shard in command["shards"]))
        self.assertTrue(all(Path(shard["log"]).is_file() for shard in command["shards"]))
        self.assertTrue(all(shard["cleanup"] == "removed" for shard in command["shards"] if shard["test_count"]))
        self.assertEqual(command["worker_count"], 5)
        self.assertTrue(all(shard["isolation"]["service_id"] for shard in command["shards"] if shard["test_count"]))
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

    def test_shards_use_unique_state_cache_temp_service_and_port_namespaces(self):
        (self.tmp / ".handsoff-ci.json").write_text("{}\n")
        fixture_runtime = self.tmp / ".handsoff-fixture"
        fixture_runtime.mkdir()
        (fixture_runtime / "criteria.json").write_text("{}\n")
        (self.tmp / "tests" / "test_isolation.py").write_text(textwrap.dedent('''
            import os, pathlib, socket, subprocess, unittest

            class TestIsolation(unittest.TestCase):
                def _exercise(self):
                    for key in ("TMPDIR", "XDG_CACHE_HOME", "XDG_STATE_HOME", "HANDSOFF_SERVICE_NAMESPACE"):
                        self.assertTrue(os.environ[key])
                    marker = pathlib.Path(os.environ["XDG_STATE_HOME"]) / "shared-name"
                    marker.write_text(os.environ["HANDSOFF_SHARD_ID"])
                    tracked = subprocess.run(
                        ["git", "ls-files", "tests/test_isolation.py"],
                        capture_output=True, text=True, check=True,
                    ).stdout.splitlines()
                    self.assertEqual(tracked, ["tests/test_isolation.py"])
                    self.assertFalse(pathlib.Path(".handsoff-ci.json").exists())
                    self.assertFalse(pathlib.Path(".handsoff-fixture").exists())
                    sock = socket.socket()
                    try:
                        sock.bind(("127.0.0.1", int(os.environ["HANDSOFF_PORT_BASE"])))
                    finally:
                        sock.close()
                test_a = _exercise
                test_b = _exercise
                test_c = _exercise
                test_d = _exercise
                test_e = _exercise
        '''))
        results = regress.run_battery_results(
            self.tmp, "isolated", ["python3 -m unittest tests.test_isolation"], timeout=120,
            request_id="rg-isolated", command_sha256="c" * 64, max_shards=5,
        )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        self.assertEqual(len(results[0]["shards"]), 5)
        self.assertEqual(len({row["isolation"]["service_id"] for row in results[0]["shards"]}), 5)
        self.assertTrue(all(row["cleanup"] == "removed" for row in results[0]["shards"]))
        self.assertFalse((self.tmp / "shared-name").exists())

    def test_serial_and_five_worker_inventory_and_outcome_are_equivalent(self):
        command = "python3 -m unittest tests.test_fixture"
        serial = regress.run_battery_results(self.tmp, "serial", [command], timeout=120,
                                             request_id="rg-serial", command_sha256="d" * 64,
                                             max_shards=1)[0]
        sharded = regress.run_battery_results(self.tmp, "sharded", [command], timeout=120,
                                              request_id="rg-sharded", command_sha256="e" * 64,
                                              max_shards=5)[0]
        self.assertEqual(serial["exit_code"], sharded["exit_code"])
        serial_ids = [test for row in serial["shards"] for test in row["completed_test_ids"]]
        shard_ids = [test for row in sharded["shards"] for test in row["completed_test_ids"]]
        self.assertEqual(sorted(serial_ids), sorted(shard_ids))

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
        self.assertTrue(entry["degraded"])
        self.assertEqual(entry["fallback_reason"], "discovery import error")
        self.assertEqual(len(entry["shards"]), 1)
        self.assertEqual(entry["done"], 4)

    def test_collection_failure_is_terminal_and_never_serial_fallback(self):
        (self.tmp / "tests" / "test_broken.py").write_text("raise RuntimeError('broken collection')\n")
        results = regress.run_battery_results(
            self.tmp, "broken", ["python3 -m unittest tests.test_broken"], timeout=120,
            request_id="rg-broken", command_sha256="b" * 64,
        )
        self.assertEqual(results[0]["exit_code"], 2)
        state = json.loads((self.tmp / regress.PROGRESS_FILE).read_text())
        self.assertEqual(state["exit_code"], 2)
        self.assertIn("tests.test_broken", state["collection_error"])
        self.assertEqual(state["commands"], [])
        normalized = regress.test_progress.read(self.tmp, execution_id=state["execution_id"])
        self.assertEqual(normalized["state"], "failed")
        self.assertEqual(normalized["totals"]["done"], 0)

    def test_python_full_preflight_names_an_extra_ci_only_module(self):
        (self.tmp / "tests" / "test_ci_only.py").write_text(textwrap.dedent('''
            import unittest

            class TestCIOnly(unittest.TestCase):
                def test_extra_target(self):
                    pass
        '''))
        with self.assertRaisesRegex(
                regress.RegressionInventoryError,
                r"missing: tests\.test_ci_only\.TestCIOnly\.test_extra_target"):
            regress.collect_inventory(
                self.tmp, "python-full", ["python3 -m unittest tests.test_fixture"]
            )

    def test_unknown_group_is_refused(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "handsoff_regress.py"), "--root", str(self.tmp), "--group", "nope"],
            capture_output=True, text=True, timeout=60, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("HANDSOFF_REGRESS_BLOCKED", proc.stderr)


class TestRegressionInventory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-inventory-"))
        self.ids = [f"tests.test_x.Case.test_{index:02d}" for index in range(11)]
        self.command = {
            "command": "python3 -m unittest tests.test_x",
            "collection_state": "collected",
            "fallback_reason": None,
            "test_count": len(self.ids),
            "test_ids": list(reversed(self.ids)),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def inventory(self):
        return regress.make_inventory("python-full", [deepcopy(self.command)])

    def test_one_versioned_inventory_has_equal_independent_views_and_five_balanced_shards(self):
        inventory = self.inventory()
        regress.lib.atomic_write_json(regress.inventory_path(self.tmp), inventory)
        views = {view: regress.read_inventory(self.tmp, view=view)
                 for view in regress.INVENTORY_VIEWS}
        self.assertEqual({item["inventory_id"] for item in views.values()}, {inventory["inventory_id"]})
        self.assertEqual({tuple(item["test_ids"]) for item in views.values()}, {tuple(sorted(self.ids))})
        self.assertEqual(inventory["schema_version"], 1)
        self.assertEqual(inventory["shard_count"], 5)
        sizes = [shard["test_count"] for shard in inventory["shards"]]
        self.assertEqual(sizes, [3, 2, 2, 2, 2])
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_duplicates_and_missing_shards_fail_closed(self):
        duplicate = deepcopy(self.command)
        duplicate["test_ids"].append(duplicate["test_ids"][0])
        with self.assertRaisesRegex(regress.RegressionInventoryError, "duplicate"):
            regress.make_inventory("python-full", [duplicate])
        missing = self.inventory()
        missing["shards"].pop()
        with self.assertRaisesRegex(regress.RegressionInventoryError, "missing shard"):
            regress.validate_inventory(missing)

    def test_exact_view_equality_names_omission_and_requires_audited_exclusion(self):
        reference = self.inventory()
        candidate_command = deepcopy(self.command)
        omitted = self.ids[-1]
        candidate_command["test_ids"].remove(omitted)
        candidate_command["test_count"] -= 1
        candidate = regress.make_inventory("python-full", [candidate_command])
        with self.assertRaisesRegex(regress.RegressionInventoryError, omitted):
            regress.require_equal_inventory(reference, candidate,
                                            reference_view="local", candidate_view="ci")
        regress.require_equal_inventory(
            reference, candidate, reference_view="local", candidate_view="ci",
            exclusions=[{"test_id": omitted, "view": "ci", "platform": "windows",
                         "reason": "POSIX-only fixture", "audit_id": "issue-276"}],
        )
        with self.assertRaisesRegex(regress.RegressionInventoryError, "not auditable"):
            regress.require_equal_inventory(
                reference, candidate, reference_view="local", candidate_view="ci",
                exclusions=[{"test_id": omitted, "view": "ci", "reason": "not here"}],
            )

    def test_completed_shards_reject_omission(self):
        shards = []
        for plan in self.inventory()["shards"]:
            shard = regress._new_shard(plan["index"], plan["test_ids"])
            shard["completed_test_ids"] = list(plan["test_ids"])
            shard["finished_at"] = "2026-01-01T00:00:00+00:00"
            shards.append(shard)
        shards[2]["completed_test_ids"].pop()
        with self.assertRaisesRegex(regress.RegressionInventoryError, "missing"):
            regress._validate_completed_shards({"shards": shards}, sorted(self.ids), 5)

    def test_superseded_retry_cannot_publish_false_completion(self):
        old = regress.test_progress.start(
            self.tmp, run_id="run", source="regression", label="old", units=["worker"]
        )
        state = {
            "execution_id": old["execution_id"], "commands": [], "planned_commands": [],
            "totals": {}, "finished_at": "2026-01-01T00:00:00+00:00", "exit_code": 0,
        }
        new = regress.test_progress.start(
            self.tmp, run_id="run", source="regression", label="retry", units=["worker"]
        )
        with self.assertRaises(regress.StaleRegressionExecution):
            regress._write(self.tmp, state)
        self.assertEqual(regress.test_progress.read(self.tmp)["execution_id"], new["execution_id"])
        self.assertFalse((self.tmp / regress.PROGRESS_FILE).exists())


class TestCompleteShardHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-all-shards-"))
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "__init__.py").write_text("")
        shutil.copy(ROOT / "tests" / "shard.py", self.tmp / "tests" / "shard.py")
        for suffix in ("alpha", "beta"):
            (self.tmp / "tests" / f"test_{suffix}.py").write_text(textwrap.dedent(f'''
                import builtins, unittest

                if hasattr(builtins, "_handsoff_incompatible_module"):
                    raise RuntimeError("incompatible modules shared an interpreter")
                builtins._handsoff_incompatible_module = "{suffix}"

                class Test{suffix.title()}(unittest.TestCase):
                    def test_runs(self):
                        pass
            '''))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_complete_inventory_runs_once_in_five_shards_with_module_isolation(self):
        results = regress.run_battery_results(
            self.tmp, "python-full", ["python3 tests/shard.py --all"], timeout=120,
        )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        state = json.loads((self.tmp / regress.PROGRESS_FILE).read_text())
        inventory = state["inventory"]
        self.assertEqual(inventory["test_count"], 2)
        self.assertEqual([item["test_count"] for item in inventory["shards"]], [1, 1, 0, 0, 0])
        command = state["commands"][0]
        observed = [test_id for shard in command["shards"]
                    for test_id in shard["completed_test_ids"]]
        self.assertEqual(sorted(observed), inventory["test_ids"])
        self.assertEqual(len(observed), len(set(observed)))


class IsolatedWorkerHead(unittest.TestCase):
    """#449: a worker's source snapshot has one commit recording its index."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-head-"))
        self.root = self.tmp / "project"
        (self.root / "sub").mkdir(parents=True)
        (self.root / ".gitignore").write_text("*.log\n")
        (self.root / "a.txt").write_text("a\n")
        (self.root / "sub" / "b.py").write_text("B = 1\n")
        (self.root / "ignored.log").write_text("runtime\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)

    def _worker(self) -> Path:
        source, _isolation, _env = regress._isolated_worker(self.root, 1, 1)
        self.addCleanup(shutil.rmtree, source.parent, True)
        return source

    def _assert_snapshot(self, source: Path, expected: list[str]) -> str:
        head = self._git(source, "rev-parse", "HEAD")
        self.assertEqual(head.returncode, 0, head.stderr)
        tracked = self._git(source, "ls-files").stdout.splitlines()
        committed = self._git(source, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertEqual(tracked, committed)
        self.assertEqual(sorted(committed), sorted(expected))
        self.assertEqual(self._git(source, "status", "--porcelain").stdout, "")
        return head.stdout.strip()

    def test_snapshot_head_records_the_index_and_is_clean(self):
        source = self._worker()
        self._assert_snapshot(source, [".gitignore", "a.txt", "sub/b.py"])

    def test_snapshot_ignores_hostile_caller_git_config(self):
        home = self.tmp / "home"
        hooks = home / "hooks"
        hooks.mkdir(parents=True)
        for name in ("pre-commit", "commit-msg"):
            hook = hooks / name
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
        (home / ".gitconfig").write_text(
            f"[commit]\n\tgpgsign = true\n[core]\n\thooksPath = {hooks}\n")
        clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")},
                             clear=False):
            for key in [key for key in os.environ if key.startswith("GIT_")]:
                del os.environ[key]
            # The hostile configuration does break an ordinary commit.
            probe = self.tmp / "probe"
            probe.mkdir()
            env = {**clean, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")}
            subprocess.run(["git", "init", "--quiet"], cwd=str(probe), env=env, check=True,
                           capture_output=True)
            plain = subprocess.run(["git", "commit", "--allow-empty", "-m", "x"], cwd=str(probe),
                                   env=env, capture_output=True, text=True)
            self.assertNotEqual(plain.returncode, 0)
            source = self._worker()
        self._assert_snapshot(source, [".gitignore", "a.txt", "sub/b.py"])

    def test_snapshot_ignores_inherited_invalid_identity_and_date(self):
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "", "GIT_AUTHOR_DATE": "invalid"}):
            source = self._worker()
        self._assert_snapshot(source, [".gitignore", "a.txt", "sub/b.py"])

    def test_snapshot_of_a_tree_with_no_committable_files(self):
        shutil.rmtree(self.root)
        self.root.mkdir()
        source = self._worker()
        self._assert_snapshot(source, [])

    def test_snapshot_leaves_an_inherited_repository_untouched(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "kept.txt").write_text("kept\n")
        for args in (("init", "--quiet"), ("add", "kept.txt"),
                     ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                      "commit", "--quiet", "--no-verify", "-m", "other")):
            done = self._git(other, *args)
            self.assertEqual(done.returncode, 0, done.stderr)
        head_before = self._git(other, "rev-parse", "HEAD").stdout
        index = other / ".git" / "index"
        index_before = index.read_bytes()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                          "GIT_INDEX_FILE": str(index)}):
            source = self._worker()
        self.assertEqual(self._git(other, "rev-parse", "HEAD").stdout, head_before)
        self.assertEqual(index.read_bytes(), index_before)
        self.assertNotEqual(self._assert_snapshot(source, [".gitignore", "a.txt", "sub/b.py"]),
                            head_before.strip())

    def test_five_shards_can_clone_and_check_out_their_head(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_head.py").write_text(textwrap.dedent('''
            import os, subprocess, tempfile, unittest

            class TestHead(unittest.TestCase):
                def _exercise(self):
                    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                         text=True, check=True).stdout.strip()
                    with tempfile.TemporaryDirectory() as clone:
                        subprocess.run(["git", "clone", "--quiet", os.getcwd(), clone],
                                       capture_output=True, check=True)
                        subprocess.run(["git", "checkout", "--quiet", "--detach", sha],
                                       cwd=clone, capture_output=True, check=True)
                test_a = _exercise
                test_b = _exercise
                test_c = _exercise
                test_d = _exercise
                test_e = _exercise
        '''))
        for name in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / name, self.root / name)
        results = regress.run_battery_results(
            self.root, "head", ["python3 -m unittest tests.test_head"], timeout=120,
            request_id="rg-head", command_sha256="f" * 64, max_shards=5,
        )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        self.assertEqual(len(results[0]["shards"]), 5)
        self.assertTrue(all(row["cleanup"] == "removed" for row in results[0]["shards"]))


BROKEN_CLASS = "tests.test_alpha.TestBroken"
BROKEN_IDS = [f"{BROKEN_CLASS}.test_one", f"{BROKEN_CLASS}.test_two"]


def run_all_fixture(modules: dict[str, str]) -> tuple[list[dict], dict]:
    """#450: run `python3 tests/shard.py --all` over a scratch project holding
    `modules` and return the results and the terminal progress record."""
    root = Path(tempfile.mkdtemp(prefix="handsoff-fixture-error-"))
    try:
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("")
        shutil.copy(ROOT / "tests" / "shard.py", root / "tests" / "shard.py")
        for name, body in modules.items():
            (root / "tests" / f"{name}.py").write_text(textwrap.dedent(body))
        results = regress.run_battery_results(
            root, "python-full", ["python3 tests/shard.py --all"], timeout=120,
        )
        return results, json.loads((root / regress.PROGRESS_FILE).read_text())
    finally:
        shutil.rmtree(root, ignore_errors=True)


SETUP_CLASS_ERROR = {"test_alpha": '''
    import unittest

    class TestBroken(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("fixture exploded")

        def test_one(self):
            pass

        def test_two(self):
            pass

    class TestFine(unittest.TestCase):
        def test_ok(self):
            pass
'''}


class FixtureErrorReporting(unittest.TestCase):
    """#450: a setUpClass/setUpModule ERROR is counted and listed with its traceback."""

    @classmethod
    def setUpClass(cls):
        cls.results, cls.state = run_all_fixture(SETUP_CLASS_ERROR)
        cls.command = cls.state["commands"][0]

    def test_fixture_lines_parse_in_both_python_shapes(self):
        for line, fixture, scope, result in (
            ("setUpClass (tests.test_release_wheel.ReproducibleBuildTest) ... ERROR",
             "setUpClass", "tests.test_release_wheel.ReproducibleBuildTest", "ERROR"),
            ("setUpModule (tests.test_release_wheel) ... ERROR", "setUpModule", "tests.test_release_wheel", "ERROR"),
            ("setUpClass (pkg.Class) ... FAIL", "setUpClass", "pkg.Class", "FAIL"),
            ("tearDownClass (pkg.Class) ... ERROR", "tearDownClass", "pkg.Class", "ERROR"),
            ("setUpClass (pkg.Class) ... skipped 'no network'", "setUpClass", "pkg.Class", "skipped 'no network'"),
        ):
            event, pending = regress.parse_line(line, "pkg.Other.test_waiting")
            self.assertEqual(event, {"name": f"{fixture} ({scope})", "result": result,
                                     "fixture": fixture, "scope": scope})
            self.assertEqual(pending, "pkg.Other.test_waiting")

    def test_count_event_counts_one_error_and_accounts_only_its_own_scope(self):
        shard = regress._new_shard(1, ["pkg.Class.test_a", "pkg.Class.test_b", "pkg.ClassB.test_c"])
        shard["completed_test_ids"].append("pkg.Class.test_a")
        event, _ = regress.parse_line("setUpClass (pkg.Class) ... ERROR", None)
        regress.count_event(shard, event, shard["test_ids"])
        self.assertEqual(shard["errors"], 1)
        self.assertEqual(shard["blocked_test_ids"], ["pkg.Class.test_b"])
        self.assertEqual(shard["done"], 1)
        self.assertEqual(shard["failures"], [{"name": "setUpClass (pkg.Class)", "kind": "ERROR",
                                              "fixture": "setUpClass", "scope": "pkg.Class", "excerpt": None}])
        teardown, _ = regress.parse_line("tearDownClass (pkg.ClassB) ... ERROR", None)
        regress.count_event(shard, teardown, shard["test_ids"])
        self.assertEqual((shard["errors"], shard["done"], shard["blocked_test_ids"]), (2, 1, ["pkg.Class.test_b"]))

    def test_shard_command_and_totals_count_the_error(self):
        self.assertNotEqual(self.results[0]["exit_code"], 0)
        self.assertGreaterEqual(self.state["totals"]["errors"], 1)
        self.assertGreaterEqual(self.command["errors"], 1)
        errored = [shard for shard in self.command["shards"] if shard["errors"]]
        self.assertTrue(errored)
        self.assertEqual(sum(shard["errors"] for shard in self.command["shards"]), self.command["errors"])

    def test_failure_names_the_fixture_with_a_traceback_excerpt(self):
        fixtures = [f for f in self.command["failures"] if f.get("fixture")]
        self.assertTrue(fixtures, self.command["failures"])
        for failure in fixtures:
            self.assertEqual(failure["kind"], "ERROR")
            self.assertEqual(failure["name"], f"setUpClass ({BROKEN_CLASS})")
            self.assertEqual(failure["scope"], BROKEN_CLASS)
            self.assertIn("RuntimeError: fixture exploded", failure["excerpt"])
            self.assertIn("Traceback", failure["excerpt"])
            self.assertLessEqual(len(failure["excerpt"]), regress.EXCERPT_MAX_CHARS)

    def test_excerpt_is_bounded(self):
        body = "\n".join(f"  frame {n}" for n in range(200))
        log = ("=" * 70 + "\nERROR: setUpClass (pkg.Class)\n" + "-" * 70
               + f"\nTraceback (most recent call last):\n{body}\nRuntimeError: boom\n\n" + "-" * 70 + "\nRan 0 tests\n")
        excerpt = regress._fixture_excerpt(log, "setUpClass (pkg.Class)")
        self.assertEqual(len(excerpt.splitlines()), regress.EXCERPT_MAX_LINES)
        self.assertTrue(excerpt.endswith("RuntimeError: boom"))
        self.assertIsNone(regress._fixture_excerpt(log, "setUpClass (pkg.Other)"))


class FixtureInventoryAccounting(unittest.TestCase):
    """#450: a recorded fixture outcome accounts for its tests; INVENTORY
    fires only for tests missing for another reason, and names them."""

    def test_setupclass_error_accounts_for_its_tests(self):
        results, state = run_all_fixture(SETUP_CLASS_ERROR)
        command = state["commands"][0]
        self.assertEqual([f for f in command["failures"] if f["kind"] == "INVENTORY"], [])
        self.assertNotIn("inventory_error", command)
        self.assertTrue(all(shard["inventory_error"] is None for shard in command["shards"]))
        self.assertTrue(all(shard["inventory_error"] is None for shard in results[0]["shards"]))
        blocked = sorted(test_id for shard in command["shards"] for test_id in shard["blocked_test_ids"])
        self.assertEqual(blocked, BROKEN_IDS)
        self.assertEqual(state["totals"]["done"], state["totals"]["total"])

    def test_setupclass_skip_accounts_for_its_tests(self):
        results, state = run_all_fixture({"test_alpha": '''
            import unittest

            class TestBroken(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("not here")

                def test_one(self):
                    pass

                def test_two(self):
                    pass
        '''})
        command = state["commands"][0]
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        self.assertEqual(command["failures"], [])
        self.assertEqual((command["skipped"], command["errors"]), (2, 0))
        self.assertTrue(all(shard["inventory_error"] is None for shard in command["shards"]))

    def test_tests_missing_without_a_fixture_outcome_still_record_inventory(self):
        results, state = run_all_fixture({"test_alpha": '''
            import os, unittest

            class TestExit(unittest.TestCase):
                def test_a(self):
                    pass

                def test_b(self):
                    os._exit(0)

                def test_c(self):
                    pass
        '''})
        command = state["commands"][0]
        missing = "tests.test_alpha.TestExit.test_b"
        inventory = [f for f in command["failures"] if f["kind"] == "INVENTORY"]
        self.assertEqual(len(inventory), 1, command["failures"])
        self.assertEqual(inventory[0]["missing"], [missing])
        self.assertEqual(inventory[0]["missing_count"], 1)
        self.assertIn(missing, inventory[0]["detail"])
        shard = next(s for s in command["shards"] if s["index"] == inventory[0]["shard"])
        self.assertEqual(shard["inventory_error"], inventory[0]["detail"])
        self.assertNotEqual(results[0]["exit_code"], 0)

    def test_inventory_missing_list_is_bounded(self):
        with self.assertRaises(regress.RegressionInventoryError) as caught:
            regress._exact_ids([f"t.{n:02d}" for n in range(30)], [], "shard 1")
        self.assertEqual(len(caught.exception.missing), 30)
        shard = regress._new_shard(1, [f"t.{n:02d}" for n in range(30)])
        self.assertEqual(regress._accounted_ids(shard), [])


class ShardFooterNumber(unittest.TestCase):
    """#450: tests/shard.py --all names the same 1-based shard as the worker label."""

    def test_footer_prints_the_one_based_shard_and_the_index(self):
        import contextlib
        import io
        sys.path.insert(0, str(ROOT / "tests"))
        import shard as shard_cli
        out = io.StringIO()
        with mock.patch.object(shard_cli, "all_modules", return_value=["test_alpha"]), \
                mock.patch.object(shard_cli.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(shard_cli.run_all_shard(["tests.test_alpha.T.test_x"], 3, 5), 0)
        self.assertIn("shard 4 of 5 (--index 3): 1 tests OK", out.getvalue())

    def test_merged_log_header_and_footer_agree_for_every_worker(self):
        results, state = run_all_fixture(SETUP_CLASS_ERROR)
        tail = results[0]["output_tail"]
        for shard in state["commands"][0]["shards"]:
            if not shard["test_count"]:
                continue
            n = shard["index"]
            self.assertEqual(shard["label"], f"Worker {n}")
            section = tail.split(f"===== shard {n} of 5 =====", 1)[1].split("===== shard", 1)[0]
            self.assertIn(f"\nshard {n} of 5 (--index {n - 1}): ", section)


if __name__ == "__main__":
    unittest.main(verbosity=2)
