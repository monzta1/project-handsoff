#!/usr/bin/env python3
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402


class RunMetricsTests(unittest.TestCase):
    def fixture(self):
        status = {
            "status": "complete", "updated_at": "2026-01-01T01:00:00+00:00",
            "agent_sessions": {
                "hs-" + "1" * 32: {
                    "session_id": "hs-" + "1" * 32, "role": "architect", "adapter": "codex",
                    "requested_model": "gpt-test", "reported_model": None, "phase_number": 2,
                    "state": "completed", "started_at": "2026-01-01T00:05:00+00:00",
                    "running_at": "2026-01-01T00:06:00+00:00", "ended_at": "2026-01-01T00:16:00+00:00",
                },
                "hs-" + "2" * 32: {
                    "session_id": "hs-" + "2" * 32, "role": "implementer", "adapter": "claude",
                    "requested_model": "opus", "reported_model": "opus-exact", "phase_number": 4,
                    "state": "failed", "started_at": "2026-01-01T00:25:00+00:00",
                    "running_at": "2026-01-01T00:26:00+00:00", "ended_at": "2026-01-01T00:46:00+00:00",
                },
            },
            "agent_replacements": [{"replacement_id": "one"}],
            "recovery_attempts": [{"recovery_id": "one"}],
            "design_review_attempts": 2,
            "review_attempts": [{"attempt_id": "one"}],
        }
        events = [
            {"kind": "initialized", "at": "2026-01-01T00:00:00+00:00"},
            {"kind": "phase_advanced", "phase_number": 2, "at": "2026-01-01T00:10:00+00:00"},
            {"kind": "human_pause_started", "at": "2026-01-01T00:12:00+00:00"},
            {"kind": "human_pause_ended", "at": "2026-01-01T00:17:00+00:00"},
            {"kind": "phase_advanced", "phase_number": 4, "at": "2026-01-01T00:30:00+00:00"},
            {"kind": "phase_advanced", "phase_number": 8, "at": "2026-01-01T00:55:00+00:00"},
        ]
        verifications = [{"results": [{"duration_s": 4.5}, {"duration_s": 1.5}]}]
        return status, events, verifications

    def test_metrics_are_truthful_content_free_and_never_estimate_tokens(self):
        status, events, verifications = self.fixture()
        metrics = lib.build_run_metrics(
            status, events, verifications,
            now=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(metrics["elapsed_seconds"], 3600)
        self.assertEqual(metrics["phase_seconds"], {
            "1": 600.0, "2": 1200.0, "3": 0.0, "4": 1500.0,
            "5": 0.0, "6": 0.0, "7": 0.0, "8": 300.0,
        })
        self.assertEqual(metrics["pilot_wait_seconds"], 300)
        self.assertEqual(metrics["verification_seconds"], 6)
        self.assertEqual((metrics["managed_sessions"], metrics["failed_sessions"]), (2, 1))
        self.assertEqual((metrics["replacement_count"], metrics["recovery_attempts"]), (1, 1))
        self.assertEqual(metrics["tokens"], {
            "input": None, "output": None, "cached": None, "total": None, "coverage": "0/2 sessions",
        })
        self.assertEqual(metrics["largest_sessions"][0]["role"], "implementer")
        self.assertEqual(metrics["baseline"]["state"], "unavailable")
        serialized = json.dumps(metrics)
        self.assertNotIn("prompt", serialized.casefold())
        self.assertNotIn("response", serialized.casefold())

    def test_completed_archive_carries_the_bounded_metrics_summary(self):
        status, events, verifications = self.fixture()
        root = Path(tempfile.mkdtemp(prefix="handsoff-metrics-root-"))
        archive = Path(tempfile.mkdtemp(prefix="handsoff-metrics-archive-"))
        try:
            with mock.patch.dict(os.environ, {"HANDSOFF_ARCHIVE_DIR": str(archive)}):
                path = lib.archive_run(root, {}, status, {"criteria": []}, verifications, events)
            record = json.loads(path.read_text())
            self.assertEqual(record["metrics"]["managed_sessions"], 2)
            self.assertIsNone(record["metrics"]["tokens"]["total"])
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(archive, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
