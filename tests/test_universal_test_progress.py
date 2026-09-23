"""Contract tests for identity-bound, app-neutral test progress."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_progress as progress  # noqa: E402
import handsoff_regress as regress  # noqa: E402


class UniversalProgressContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="handsoff-progress-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_overflow_keeps_all_aggregate_counts_but_only_sixteen_rows(self):
        snapshot = progress.start(self.root, run_id="run-1", source="verify", label="Checks",
                                  units=[f"check {index}" for index in range(20)])
        all_units = [
            {"index": index, "label": f"check {index}", "state": "passed", "total": 1,
             "done": 1, "progress": 1.0, "elapsed_seconds": 1.0, "result": "exit 0"}
            for index in range(1, 21)
        ]
        snapshot["totals"] = progress.aggregate_units(all_units)
        snapshot["units"] = all_units
        self.assertTrue(progress.write(self.root, snapshot, expected_execution_id=snapshot["execution_id"]))
        view = progress.read(self.root, run_id="run-1", execution_id=snapshot["execution_id"])
        self.assertEqual(view["totals"]["unit_total"], 20)
        self.assertEqual(view["totals"]["passed"], 20)
        self.assertEqual(view["visible_unit_count"], 16)
        self.assertEqual(len(view["units"]), 16)

    def test_unknown_total_propagates_and_terminal_precedence_is_deterministic(self):
        units = [
            {"state": "passed", "total": 3, "done": 3},
            {"state": "cancelled", "total": None, "done": 0},
            {"state": "timed_out", "total": 2, "done": 1},
            {"state": "failed", "total": 1, "done": 1},
        ]
        totals = progress.aggregate_units(units)
        self.assertIsNone(totals["total"])
        self.assertEqual(totals["done"], 5)
        self.assertEqual(totals["state"], "failed")

    def test_retry_supersedes_old_execution_and_rejects_late_writer(self):
        old = progress.start(self.root, run_id="run-1", source="verify", label="Old", units=["a"])
        new = progress.start(self.root, run_id="run-1", source="verify", label="New", units=["a"])
        old["state"] = "failed"
        self.assertFalse(progress.write(self.root, old, expected_execution_id=old["execution_id"]))
        self.assertIsNone(progress.read(self.root, execution_id=old["execution_id"]))
        self.assertEqual(progress.read(self.root, execution_id=new["execution_id"])["label"], "New")

    def test_nonterminal_expires_after_thirty_seconds_and_terminal_after_ten_minutes(self):
        began = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = progress.start(self.root, run_id="run-1", source="verify", label="Checks",
                                  units=["a"], now=began)
        self.assertIsNotNone(progress.read(self.root, now=began + timedelta(seconds=30)))
        self.assertIsNone(progress.read(self.root, now=began + timedelta(seconds=31)))
        snapshot["heartbeat_at"] = began.isoformat()
        snapshot["finished_at"] = began.isoformat()
        snapshot["state"] = "passed"
        Path(self.root / progress.PROGRESS_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertIsNotNone(progress.read(self.root, now=began + timedelta(seconds=600)))
        self.assertIsNone(progress.read(self.root, now=began + timedelta(seconds=601)))

    def test_managed_check_invocation_publishes_fresh_identity_and_result(self):
        cfg = {"check_timeout_seconds": 30, "check_commands": [], "regressions": []}
        first = lib.run_checks(cfg, self.root, commands=[f"{sys.executable} -c 'pass'"])
        one = progress.read(self.root)
        second = lib.run_checks(cfg, self.root, commands=[f"{sys.executable} -c 'pass'"])
        two = progress.read(self.root)
        self.assertEqual(first[0]["exit_code"], 0)
        self.assertEqual(second[0]["exit_code"], 0)
        self.assertNotEqual(one["execution_id"], two["execution_id"])
        self.assertEqual(two["source"], "verify")
        self.assertEqual(two["state"], "passed")
        self.assertEqual(two["totals"]["unit_done"], 1)

    def test_ci_mapping_uses_the_normalized_vocabulary(self):
        view = progress.from_ci("run-1", {
            "pr": 7, "head": "abc", "state": "running", "started_at": "2026-01-01T00:00:00+00:00",
            "fetched_at": "2026-01-01T00:00:03+00:00",
            "checks": [{"name": "linux", "state": "SUCCESS", "elapsed_seconds": 2},
                       {"name": "windows", "state": "IN_PROGRESS", "elapsed_seconds": 3}],
        })
        self.assertEqual(view["source"], "ci")
        self.assertEqual(view["execution_id"], "ci-abc")
        self.assertEqual([item["state"] for item in view["units"]], ["passed", "running"])
        self.assertEqual(view["totals"]["state"], "running")

    def test_regression_snapshot_carries_the_same_versioned_dashboard_inventory(self):
        ids = [f"tests.test_x.Case.test_{index}" for index in range(7)]
        inventory = regress.make_inventory("python-full", [{
            "command": "python3 -m unittest tests.test_x",
            "collection_state": "collected", "fallback_reason": None,
            "test_count": len(ids), "test_ids": ids,
        }])
        normalized = progress.start(
            self.root, run_id="run-1", source="regression", label="Full", units=[]
        )
        state = {
            "execution_id": normalized["execution_id"], "commands": [], "planned_commands": [],
            "totals": {"total": 7, "done": 0, "passed": 0, "failed": 0,
                       "errors": 0, "skipped": 0},
            "finished_at": None, "exit_code": None, "inventory": inventory,
        }
        regress._write(self.root, state)
        view = progress.read(self.root, execution_id=normalized["execution_id"])
        self.assertEqual(view["inventory"]["schema_version"], 1)
        self.assertEqual(view["inventory_id"], inventory["inventory_id"])
        self.assertEqual(view["inventory_test_count"], 7)
        dashboard_inventory = regress.read_inventory(self.root, view="dashboard")
        self.assertEqual(dashboard_inventory["test_ids"], ids)

    def test_persisted_labels_are_bounded_and_secret_safe(self):
        snapshot = progress.start(
            self.root, run_id="run-1", source="verify", label="Checks",
            units=["runner API_TOKEN=super-secret " + ("x" * 400)],
        )
        raw = (self.root / progress.PROGRESS_FILE).read_text(encoding="utf-8")
        self.assertNotIn("super-secret", raw)
        self.assertIn("[REDACTED]", raw)
        self.assertLessEqual(len(snapshot["units"][0]["label"]), 240)

    def test_dashboard_reader_requires_current_run_and_regression_identity(self):
        status = {"feature": "x"}
        events = [{"at": "2026-01-01T00:00:00+00:00"}]
        run_id = lib.feature_hash(status, events)
        snapshot = progress.start(
            self.root, run_id=run_id, source="regression", label="Full",
            units=["Command 1"], request_id="rg-1", command_hash="a" * 64,
        )
        request = {"request_id": "rg-1", "command_sha256": "a" * 64}
        self.assertEqual(dashboard._test_progress(self.root, status, events, request, None)["execution_id"],
                         snapshot["execution_id"])
        self.assertIsNone(dashboard._test_progress(
            self.root, status, events,
            {"request_id": "rg-2", "command_sha256": "a" * 64}, None,
        ))
        self.assertIsNone(dashboard._test_progress(self.root, {"feature": "other"}, events, request, None))

    def test_legacy_regression_maps_during_an_engine_upgrade(self):
        now = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
        payload = {
            "request_id": "rg-1", "command_sha256": "b" * 64,
            "label": "python-full", "started_at": "2026-01-01T00:00:00+00:00",
            "heartbeat_at": "2026-01-01T00:00:04+00:00",
            "finished_at": None, "exit_code": None,
            "totals": {"total": 10, "done": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
            "commands": [{"shards": [
                {"index": 1, "label": "Worker 1", "test_count": 5, "done": 4,
                 "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
                 "exit_code": None, "timed_out": False},
                {"index": 2, "label": "Worker 2", "test_count": 5, "done": 0,
                 "started_at": None, "finished_at": None, "exit_code": None, "timed_out": False},
            ]}],
        }
        view = progress.from_legacy_regression("run-1", payload, now=now)
        self.assertEqual(view["source"], "regression")
        self.assertTrue(view["execution_id"].startswith("legacy-"))
        self.assertEqual(view["totals"]["total"], 10)
        self.assertEqual([unit["state"] for unit in view["units"]], ["running", "queued"])
        self.assertIsNone(progress.from_legacy_regression(
            "run-1", payload, now=now + timedelta(seconds=31),
        ))

    def test_legacy_terminal_expiry_and_failed_before_timeout_precedence(self):
        finished = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = {
            "request_id": "rg-2", "command_sha256": "c" * 64, "label": "mixed",
            "started_at": finished.isoformat(), "heartbeat_at": finished.isoformat(),
            "finished_at": finished.isoformat(), "exit_code": 1,
            "totals": {"total": 2, "done": 2, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
            "commands": [{"shards": [
                {"label": "Worker 1", "test_count": 1, "done": 1, "started_at": finished.isoformat(),
                 "finished_at": finished.isoformat(), "exit_code": 1, "timed_out": False},
                {"label": "Worker 2", "test_count": 1, "done": 1, "started_at": finished.isoformat(),
                 "finished_at": finished.isoformat(), "exit_code": 124, "timed_out": True},
            ]}],
        }
        view = progress.from_legacy_regression("run-1", payload, now=finished + timedelta(seconds=600))
        self.assertEqual(view["state"], "failed")
        self.assertIsNone(progress.from_legacy_regression(
            "run-1", payload, now=finished + timedelta(seconds=601),
        ))

    def test_sse_signature_recomputes_legacy_expiry_without_a_payload_write(self):
        path = self.root / ".handsoff-regression.json"
        path.write_text(json.dumps({
            "request_id": "rg-3", "command_sha256": "d" * 64,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None, "commands": [], "totals": {},
        }), encoding="utf-8")
        self.assertTrue(dashboard._legacy_regression_fresh(self.root))
        stale = datetime.now(timezone.utc).timestamp() - 31
        os.utime(path, (stale, stale))
        self.assertFalse(dashboard._legacy_regression_fresh(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
