"""#448: a regression run no longer loads the machine.

The runner rewrote ~5.5 MB with fsync and .bak copies after every test, and
the dashboard's "cheap" SSE signature re-parsed the 1.8 MB progress file
five times a second per client. These tests pin the I/O budget: throttled
progress writes with a trailing flush under one lock, lean running files,
a stat-keyed parse cache whose freshness still follows the clock, one shared
SSE signature per tick, and niced shard workers.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_progress as progress  # noqa: E402
import handsoff_regress as regress  # noqa: E402
from tests.engine_patch import patch_engine  # noqa: E402
from tests.test_handsoff_supervisor import HandsoffTestCase, run  # noqa: E402


def _fixture_root(prefix: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    (tmp / "tests").mkdir()
    (tmp / "tests" / "__init__.py").write_text("")
    for name in ("handsoff.toml", "handsoff-runtime.json"):
        shutil.copyfile(ROOT / name, tmp / name)
    return tmp


def _many_tests(tmp: Path, modules: int = 3, per_module: int = 50) -> int:
    for module in range(modules):
        body = "\n".join(f"    def test_{index:03d}(self):\n        pass\n" for index in range(per_module))
        (tmp / "tests" / f"test_many_{module}.py").write_text(
            f"import unittest\n\n\nclass Many{module}(unittest.TestCase):\n{body}"
        )
    return modules * per_module


class _Recorder:
    """Wraps transient_replace and records every payload written to `names`."""

    def __init__(self, names: tuple[str, ...]):
        self.names = names
        self.writes: list[tuple[str, bytes]] = []
        self.lock = threading.Lock()
        self.real = progress.transient_replace

    def __call__(self, path, payload):
        if Path(path).name in self.names:
            with self.lock:
                self.writes.append((Path(path).name, bytes(payload)))
        return self.real(path, payload)

    def payloads(self, name: str) -> list[dict]:
        return [json.loads(raw) for written, raw in self.writes if written == name]


class ThrottledProgressWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = _fixture_root("handsoff-io-throttle-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_150_tests_write_the_progress_file_fewer_than_40_times_with_exact_totals(self):
        count = _many_tests(self.tmp)
        recorder = _Recorder((regress.PROGRESS_FILE,))
        with mock.patch.object(regress.test_progress, "transient_replace", recorder):
            results = regress.run_battery_results(
                self.tmp, "many", ["python3 -m unittest tests.test_many_0 tests.test_many_1 tests.test_many_2"],
                timeout=120, request_id="rg-many", command_sha256="a" * 64, max_shards=5,
            )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        writes = recorder.payloads(regress.PROGRESS_FILE)
        self.assertLess(len(writes), 40, f"{len(writes)} progress writes for {count} tests")
        final = json.loads((self.tmp / regress.PROGRESS_FILE).read_text())
        self.assertEqual(final["totals"], {"total": count, "done": count, "passed": count,
                                           "failed": 0, "errors": 0, "skipped": 0})
        shards = final["commands"][0]["shards"]
        self.assertEqual([shard["done"] for shard in shards], [shard["test_count"] for shard in shards])
        self.assertEqual(sum(len(shard["completed_test_ids"]) for shard in shards), count)

    def test_a_count_parsed_before_a_long_silence_lands_within_two_seconds(self):
        marker = self.tmp.parent / f"{self.tmp.name}-marker"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        (self.tmp / "tests" / "test_quiet.py").write_text(textwrap.dedent(f'''
            import time, unittest
            from pathlib import Path

            class Quiet(unittest.TestCase):
                def test_a_quick(self):
                    Path({str(marker)!r}).write_text(repr(time.time()))

                def test_b_silent(self):
                    time.sleep(4)
        '''))
        observed: list[float] = []
        stop = threading.Event()

        def watch():
            path = self.tmp / regress.PROGRESS_FILE
            while not stop.is_set():
                try:
                    state = json.loads(path.read_text())
                    if not state.get("finished_at") and (state.get("totals") or {}).get("done") == 1:
                        observed.append(time.time())
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            regress.run_battery_results(self.tmp, "quiet", ["python3 -m unittest tests.test_quiet"],
                                        timeout=120, request_id="rg-quiet", command_sha256="b" * 64,
                                        max_shards=1)
        finally:
            stop.set()
            watcher.join(timeout=2)
        self.assertTrue(observed, "the first count never reached the progress file while the run was live")
        finished_test = float(marker.read_text())
        self.assertLess(observed[0] - finished_test, 2.0)

    def test_a_superseded_execution_trailing_flush_writes_nothing(self):
        old = progress.start(self.tmp, run_id="run", source="regression", label="old", units=["w"])
        state = {"execution_id": old["execution_id"], "commands": [], "planned_commands": [],
                 "totals": {}, "finished_at": None, "exit_code": None}
        writer = regress._ProgressWriter(self.tmp)
        writer.write(state)
        state["totals"] = {"total": 1, "done": 1}
        writer.write(state, throttle=True)  # inside the interval: deferred, dirty
        self.assertTrue(writer.dirty)
        before = (self.tmp / regress.PROGRESS_FILE).read_bytes()
        new = progress.start(self.tmp, run_id="run", source="regression", label="retry", units=["w"])
        writer.last_flush = float("-inf")  # the interval has passed: the trailing flush is due
        with self.assertRaises(regress.StaleRegressionExecution):
            writer.flush_if_dirty(state)
        self.assertEqual((self.tmp / regress.PROGRESS_FILE).read_bytes(), before)
        self.assertEqual(progress.read(self.tmp)["execution_id"], new["execution_id"])
        with self.assertRaises(regress.StaleRegressionExecution):
            writer.write(state)
        writer.flush_if_dirty(state)  # a stale writer's flush is a no-op, never a write
        self.assertEqual((self.tmp / regress.PROGRESS_FILE).read_bytes(), before)

    def test_flushes_racing_per_test_writes_always_serialize_a_consistent_snapshot(self):
        normalized = progress.start(self.tmp, run_id="run", source="regression", label="race", units=["w"])
        shards = [regress._new_shard(index, None) for index in range(1, 6)]
        entry = {"command": "c", "total": None, "done": 0, "passed": 0, "failed": 0, "errors": 0,
                 "skipped": 0, "current": None, "failures": [], "shards": shards}
        state = {"execution_id": normalized["execution_id"], "commands": [entry], "planned_commands": ["c"],
                 "totals": {}, "finished_at": None, "exit_code": None}
        writer = regress._ProgressWriter(self.tmp)
        recorder = _Recorder((regress.PROGRESS_FILE,))
        stop = threading.Event()

        def report(shard):
            for index in range(60):
                with writer.lock:
                    regress.count_event(shard, {"name": f"t{shard['index']}.{index}", "result": "ok"})
                    regress._recount_entry(entry)
                    state["totals"] = regress._totals(state)
                    writer.write(state, throttle=True)
                time.sleep(0.001)

        def flush():
            while not stop.is_set():
                writer.flush_if_dirty(state)
                time.sleep(0.001)

        with mock.patch.object(regress, "PROGRESS_INTERVAL_SECONDS", 0.002), \
                mock.patch.object(regress.test_progress, "transient_replace", recorder):
            flusher = threading.Thread(target=flush)
            flusher.start()
            workers = [threading.Thread(target=report, args=(shard,)) for shard in shards]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            stop.set()
            flusher.join()
            writer.flush_if_dirty(state)
        snapshots = recorder.payloads(regress.PROGRESS_FILE)
        self.assertGreater(len(snapshots), 5)
        for snapshot in snapshots:
            row = snapshot["commands"][0]
            self.assertEqual(row["done"], sum(shard["done"] for shard in row["shards"]))
            self.assertEqual(snapshot["totals"]["done"], row["done"])
        self.assertEqual(json.loads((self.tmp / regress.PROGRESS_FILE).read_text())["totals"]["done"], 300)

    def test_trailing_flushes_never_add_a_second_write_inside_one_interval(self):
        normalized = progress.start(self.tmp, run_id="run", source="regression", label="gap", units=["w"])
        state = {"execution_id": normalized["execution_id"], "commands": [], "planned_commands": [],
                 "totals": {"done": 0}, "finished_at": None, "exit_code": None}
        writer = regress._ProgressWriter(self.tmp)
        stamps = []
        real = progress.transient_replace

        def stamped(path, payload):
            if Path(path).name == regress.PROGRESS_FILE:
                stamps.append(time.monotonic())
            return real(path, payload)

        interval = 0.1
        stop = threading.Event()

        def flush():
            while not stop.is_set():
                writer.flush_if_dirty(state)
                time.sleep(interval / 4)

        with mock.patch.object(regress, "PROGRESS_INTERVAL_SECONDS", interval), \
                mock.patch.object(regress.test_progress, "transient_replace", stamped):
            flusher = threading.Thread(target=flush)
            flusher.start()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with writer.lock:
                    state["totals"]["done"] += 1
                    writer.write(state, throttle=True)
                time.sleep(0.003)
            stop.set()
            flusher.join()
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        self.assertGreater(len(stamps), 5)
        self.assertGreaterEqual(min(gaps), interval * 0.95, gaps)
        # and the trailing flush still keeps up: no gap exceeds 1.25 intervals by much
        self.assertLess(max(gaps), interval * 2, gaps)

    def test_every_writer_in_one_execution_is_the_same_lock(self):
        seen = []
        real = regress._run_worker

        def spy(*args, **kwargs):
            seen.append(kwargs["writer"])
            return real(*args, **kwargs)

        _many_tests(self.tmp, modules=1, per_module=10)
        with mock.patch.object(regress, "_run_worker", spy):
            regress.run_battery_results(self.tmp, "one", ["python3 -m unittest tests.test_many_0"],
                                        timeout=120, request_id="rg-one", command_sha256="c" * 64)
        self.assertEqual(len(seen), 5)
        self.assertEqual(len({id(writer) for writer in seen}), 1)
        self.assertIsInstance(seen[0].lock, type(threading.RLock()))


class LeanProgressFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = _fixture_root("handsoff-io-lean-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_running_files_carry_counts_not_id_lists_and_the_terminal_write_is_full(self):
        count = _many_tests(self.tmp, modules=2, per_module=20)
        recorder = _Recorder((regress.PROGRESS_FILE, progress.PROGRESS_FILE))
        inventory_writes = []
        real_atomic = regress.lib.atomic_write_json
        durable = []
        real_durable = regress.lib.durable_replace

        def atomic(path, data):
            if Path(path).name == regress.INVENTORY_FILE:
                inventory_writes.append(path)
            return real_atomic(path, data)

        def durable_spy(path, *args, **kwargs):
            durable.append(Path(path).name)
            return real_durable(path, *args, **kwargs)

        with mock.patch.object(regress.test_progress, "transient_replace", recorder), \
                patch_engine("atomic_write_json", atomic), \
                patch_engine("durable_replace", durable_spy), \
                mock.patch.object(regress, "PROGRESS_INTERVAL_SECONDS", 0.0):
            results = regress.run_battery_results(
                self.tmp, "lean", ["python3 -m unittest tests.test_many_0 tests.test_many_1"],
                timeout=120, request_id="rg-lean", command_sha256="d" * 64,
            )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        legacy = recorder.payloads(regress.PROGRESS_FILE)
        running = [item for item in legacy if not item.get("finished_at")]
        self.assertGreater(len(running), count // 2, "the interval was patched to 0: every test writes")
        for item in running:
            self.assertNotIn("inventory", item)
            for row in item["commands"]:
                self.assertNotIn("test_ids", row)
                for shard in row["shards"]:
                    self.assertNotIn("test_ids", shard)
                    self.assertNotIn("completed_test_ids", shard)
                    self.assertIn("done", shard)
        terminal = legacy[-1]
        self.assertTrue(terminal["finished_at"])
        self.assertEqual(terminal["inventory"]["test_count"], count)
        observed = sorted(test for shard in terminal["commands"][0]["shards"]
                          for test in shard["completed_test_ids"])
        self.assertEqual(observed, terminal["inventory"]["test_ids"])
        for snapshot in recorder.payloads(progress.PROGRESS_FILE):
            inventory = snapshot.get("inventory")
            if inventory is None:
                continue
            self.assertEqual(set(inventory), {"schema_version", "inventory_id", "group", "state",
                                              "test_count", "shard_count", "test_ids_sha256"})
            self.assertEqual(snapshot["inventory_test_count"], count)
        self.assertEqual(len(inventory_writes), 1, "the inventory is written once per execution")
        self.assertNotIn(regress.PROGRESS_FILE, durable)
        self.assertNotIn(progress.PROGRESS_FILE, durable)
        leftovers = [path.name for path in self.tmp.iterdir()
                     if path.name.startswith((regress.PROGRESS_FILE, progress.PROGRESS_FILE))
                     and path.name not in (regress.PROGRESS_FILE, progress.PROGRESS_FILE)]
        self.assertEqual(leftovers, [], "progress files leave no .bak or temp copies")

    def test_transient_replace_never_fsyncs(self):
        target = self.tmp / "side.json"
        with mock.patch.object(os, "fsync", side_effect=AssertionError("fsync called")):
            progress.transient_replace(target, b"{}\n")
            progress.transient_replace(target, b"{\"a\": 1}\n")
        self.assertEqual(target.read_bytes(), b"{\"a\": 1}\n")
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir() if "side" in path.name), ["side.json"])

    def test_the_inventory_is_rewritten_only_when_it_changes(self):
        ids = [f"tests.test_x.Case.test_{index}" for index in range(4)]
        inventory = regress.make_inventory("g", [{"command": "c", "collection_state": "collected",
                                                  "fallback_reason": None, "test_count": 4, "test_ids": ids}])
        normalized = progress.start(self.tmp, run_id="run", source="regression", label="inv", units=[])
        state = {"execution_id": normalized["execution_id"], "commands": [], "planned_commands": [],
                 "totals": {}, "finished_at": None, "exit_code": None, "inventory": inventory}
        writer = regress._ProgressWriter(self.tmp)
        calls = []
        real = regress.lib.atomic_write_json
        with patch_engine("atomic_write_json",
                          lambda path, data: (calls.append(Path(path).name), real(path, data))):
            writer.write(state)
            writer.write(state)
            state["inventory"] = regress.make_inventory(
                "g", [{"command": "c", "collection_state": "collected", "fallback_reason": None,
                       "test_count": 3, "test_ids": ids[:3]}])
            writer.write(state)
        self.assertEqual(calls, [regress.INVENTORY_FILE, regress.INVENTORY_FILE])
        self.assertEqual(regress.read_inventory(self.tmp, view="dashboard")["test_count"], 3)


class CheapDashboardSignature(unittest.TestCase):
    def setUp(self):
        self.tmp = _fixture_root("handsoff-io-signature-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dashboard._JSON_CACHE.clear()
        self.addCleanup(dashboard._JSON_CACHE.clear)
        self.loads = []
        real = json.loads

        def counting(text, *args, **kwargs):
            if isinstance(text, str) and ("tx-io-marker" in text or "rg-io-marker" in text):
                self.loads.append("normalized" if "tx-io-marker" in text else "legacy")
            return real(text, *args, **kwargs)

        patcher = mock.patch.object(dashboard.json, "loads", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_files(self, now: datetime, *, terminal: bool) -> None:
        stamp = now.isoformat()
        normalized = {"schema_version": 1, "run_id": "r", "execution_id": "tx-io-marker",
                      "state": "passed" if terminal else "running", "started_at": stamp,
                      "heartbeat_at": stamp, "finished_at": stamp if terminal else None,
                      "units": [], "totals": {}}
        legacy = {"request_id": "rg-io-marker", "command_sha256": "e" * 64, "started_at": stamp,
                  "finished_at": stamp if terminal else None, "exit_code": 0 if terminal else None,
                  "commands": [], "totals": {}}
        (self.tmp / progress.PROGRESS_FILE).write_text(json.dumps(normalized))
        legacy_path = self.tmp / regress.PROGRESS_FILE
        legacy_path.write_text(json.dumps(legacy))
        os.utime(legacy_path, (now.timestamp(), now.timestamp()))

    def fresh(self, at: datetime) -> dict:
        with mock.patch.object(dashboard, "_utcnow", return_value=at):
            signature = dashboard._artifact_signature(self.tmp)
        return {name: value for name, value, _ in signature if name.startswith("__")}

    def stats(self):
        return [((self.tmp / name).stat().st_mtime_ns, (self.tmp / name).stat().st_size, (self.tmp / name).read_bytes())
                for name in (progress.PROGRESS_FILE, regress.PROGRESS_FILE)]

    def test_an_unchanged_file_is_parsed_once_however_often_the_signature_runs(self):
        now = datetime.now(timezone.utc)
        self.write_files(now, terminal=False)
        for _ in range(10):
            self.fresh(now)
        self.assertEqual(sorted(self.loads), ["legacy", "normalized"])

    def test_running_freshness_flips_at_30_seconds_with_the_files_untouched(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.write_files(now, terminal=False)
        before = self.stats()
        self.assertEqual(self.fresh(now + timedelta(seconds=29)),
                         {"__test_progress_fresh__": 1, "__legacy_regression_fresh__": 1})
        self.assertEqual(self.fresh(now + timedelta(seconds=31)),
                         {"__test_progress_fresh__": 0, "__legacy_regression_fresh__": 0})
        self.assertEqual(self.stats(), before)
        self.assertEqual(sorted(self.loads), ["legacy", "normalized"])

    def test_terminal_freshness_flips_at_600_seconds_with_the_files_untouched(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.write_files(now, terminal=True)
        before = self.stats()
        self.assertEqual(self.fresh(now + timedelta(seconds=599)),
                         {"__test_progress_fresh__": 1, "__legacy_regression_fresh__": 1})
        self.assertEqual(self.fresh(now + timedelta(seconds=601)),
                         {"__test_progress_fresh__": 0, "__legacy_regression_fresh__": 0})
        self.assertEqual(self.stats(), before)
        self.assertEqual(sorted(self.loads), ["legacy", "normalized"])

    def test_a_changed_file_is_parsed_again(self):
        now = datetime.now(timezone.utc)
        self.write_files(now, terminal=False)
        self.fresh(now)
        later = now + timedelta(seconds=2)
        self.write_files(later, terminal=True)
        self.assertEqual(self.fresh(later)["__test_progress_fresh__"], 1)
        self.assertEqual(sorted(self.loads), ["legacy", "legacy", "normalized", "normalized"])

    def test_concurrent_readers_of_an_unchanged_file_parse_it_once(self):
        now = datetime.now(timezone.utc)
        self.write_files(now, terminal=False)
        real = dashboard.json.loads  # the counting wrapper from setUp

        def slow(text, *args, **kwargs):
            time.sleep(0.05)
            return real(text, *args, **kwargs)

        with mock.patch.object(dashboard.json, "loads", slow):
            threads = [threading.Thread(target=dashboard._json_by_stat, args=(self.tmp / regress.PROGRESS_FILE,))
                       for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(self.loads, ["legacy"])

    def test_api_regression_reads_the_same_cache_and_cannot_corrupt_it(self):
        now = datetime.now(timezone.utc)
        self.write_files(now, terminal=False)
        self.fresh(now)
        request = {"request_id": "rg-io-marker", "command_sha256": "e" * 64}
        first = dashboard._regression_progress(self.tmp, request)
        first["totals"] = "mutated by a caller"
        second = dashboard._regression_progress(self.tmp, request)
        self.assertEqual(second["totals"], {})
        self.assertEqual(self.loads.count("legacy"), 1)


class SharedSseSignature(HandsoffTestCase):
    def test_three_clients_in_one_tick_cost_one_signature(self):
        calls = []

        def slow(root):
            # SSE handlers left by another test's server share this module
            # function; count only this fixture's root.
            if root == self.tmp:
                calls.append(root)
            time.sleep(0.05)
            return (("x", len(calls), 0),)

        shared = dashboard._SharedSignature(self.tmp)
        with mock.patch.object(dashboard, "_artifact_signature", slow):
            threads = [threading.Thread(target=shared.get) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(calls), 1)
            time.sleep(dashboard.SSE_POLL_SECONDS)
            shared.get()
            self.assertEqual(len(calls), 2)
        self.assertEqual(dashboard.SSE_POLL_SECONDS, 1.0)

    def test_a_change_reaches_every_client_as_one_invalidate(self):
        self.init("Shared SSE signature fixture")
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Runs first: lets this server's SSE handler loops end with the test.
        self.addCleanup(setattr, server, "stopping", True)
        host, port = server.server_address[:2]
        calls = []
        real = dashboard._artifact_signature

        def counting(root):
            if root == self.tmp:
                calls.append(time.monotonic())
            return real(root)

        streams = []
        with mock.patch.object(dashboard, "_artifact_signature", counting):
            for _ in range(2):
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request("GET", "/api/events")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                while response.readline() not in (b"\n", b"\r\n", b""):
                    pass
                streams.append((connection, response))
            changed = run(["background-wait-start", "--by", "sse-test", "--note", "changed"], cwd=self.tmp)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            for connection, response in streams:
                event = ""
                for _ in range(12):
                    line = response.readline().decode("utf-8")
                    if line.startswith("event:"):
                        event = line.strip()
                    if not line.strip() and event == "event: invalidate":
                        break
                self.assertEqual(event, "event: invalidate")
            window_start = time.monotonic()
            time.sleep(3.0)
            in_window = [at for at in calls if at >= window_start]
        for connection, _ in streams:
            connection.close()
        # Two clients over three seconds: one shared computation per tick,
        # not one per client every 0.2 s (which would be about 30).
        self.assertLessEqual(len(in_window), 4)


class ShardPriority(unittest.TestCase):
    def setUp(self):
        self.tmp = _fixture_root("handsoff-io-priority-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.report = self.tmp.parent / f"{self.tmp.name}-priority.json"
        self.addCleanup(lambda: self.report.unlink(missing_ok=True))
        (self.tmp / "tests" / "test_priority.py").write_text(textwrap.dedent(f'''
            import json, os, subprocess, sys, unittest
            from pathlib import Path

            class Priority(unittest.TestCase):
                def test_reports(self):
                    child = subprocess.run(
                        [sys.executable, "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"],
                        capture_output=True, text=True, check=True)
                    Path({str(self.report)!r}).write_text(json.dumps({{
                        "self": os.getpriority(os.PRIO_PROCESS, 0), "child": int(child.stdout)}}))
        '''))

    def test_workers_and_their_children_start_niced(self):
        runner = os.getpriority(os.PRIO_PROCESS, 0)
        results = regress.run_battery_results(self.tmp, "nice", ["python3 -m unittest tests.test_priority"],
                                              timeout=120, request_id="rg-nice", command_sha256="f" * 64)
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        observed = json.loads(self.report.read_text())
        expected = min(runner + regress.SHARD_NICENESS, 19)
        self.assertEqual(observed, {"self": expected, "child": expected})
        ran = [shard for shard in results[0]["shards"] if shard["test_count"]]
        self.assertEqual([shard["priority"] for shard in ran], [regress.SHARD_NICENESS])

    def test_a_shell_command_is_niced_through_sh(self):
        spec, shell, priority = regress._niced("echo hi", True)
        self.assertFalse(shell)
        self.assertEqual(spec[1:], ["-n", str(regress.SHARD_NICENESS), "/bin/sh", "-c", "echo hi"])
        self.assertEqual(priority, regress.SHARD_NICENESS)

    def test_without_a_nice_executable_the_shard_runs_unchanged(self):
        with mock.patch.object(regress.shutil, "which", return_value=None):
            self.assertEqual(regress._niced(["python3", "-V"], False), (["python3", "-V"], False, None))
            results = regress.run_battery_results(self.tmp, "plain", ["python3 -m unittest tests.test_priority"],
                                                  timeout=120, request_id="rg-plain", command_sha256="0" * 64)
        self.assertEqual(results[0]["exit_code"], 0, results[0]["output_tail"])
        ran = [shard for shard in results[0]["shards"] if shard["test_count"]]
        self.assertEqual([shard["priority"] for shard in ran], [None])
        self.assertEqual(json.loads(self.report.read_text())["self"], os.getpriority(os.PRIO_PROCESS, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
