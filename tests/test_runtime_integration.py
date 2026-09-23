#!/usr/bin/env python3
"""Focused integration checks for runtime-control Supervisor/dashboard hooks."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_supervisor as supervisor  # noqa: E402


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-runtime-integration-"))
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        self.status = {"status": "in_progress", "progress": 40, "agent_sessions": {},
                       "agent_failures": {}, "recovery_attempts": [], "regression_requests": []}
        self.events = [{"kind": "initialized", "at": self.now.isoformat(), "hash": "a" * 64}]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_90_warning_and_120_pause_are_persisted_and_block_mutation(self):
        warning = supervisor.refresh_performance_state(
            self.root, status=self.status, events=self.events, now=self.now + timedelta(minutes=90),
        )
        self.assertEqual((warning["state"], warning["transition"]), ("warning", "deadline_warning"))
        paused = supervisor.refresh_performance_state(
            self.root, status=self.status, events=self.events, now=self.now + timedelta(minutes=120),
        )
        self.assertTrue(paused["block_new_work"])
        self.assertEqual(paused["transition"], "pause_for_performance_review")
        with mock.patch.object(supervisor, "refresh_performance_state", return_value=paused):
            self.assertIn("blocked", supervisor.performance_mutation_refusal(self.root, "advance"))
            self.assertIsNone(supervisor.performance_mutation_refusal(self.root, "status"))
            self.assertIsNone(supervisor.performance_mutation_refusal(self.root, "performance-resume"))

    def test_explicit_resume_opens_a_new_episode(self):
        supervisor.refresh_performance_state(
            self.root, status=self.status, events=self.events, now=self.now + timedelta(minutes=120),
        )
        args = mock.Mock(root=str(self.root), by="supervisor", reason="scope reduced after reevaluation",
                         evidence_hash=hashlib.sha256(b"review").hexdigest())
        with mock.patch.object(supervisor, "datetime") as clock:
            clock.now.return_value = self.now + timedelta(minutes=121)
            clock.side_effect = lambda *values, **kwargs: datetime(*values, **kwargs)
            self.assertEqual(supervisor.cmd_performance_resume(args), 0)
        record = json.loads((self.root / supervisor.RUNTIME_CONTROL_DIR / supervisor.PERFORMANCE_RECORD).read_text())
        self.assertEqual(record["episodes"][-1]["state"], "active")
        self.assertEqual(len(record["episodes"]), 2)

    def test_a_persisted_open_human_hold_is_closed_by_the_later_end_event(self):
        open_events = self.events + [{
            "kind": "human_pause_started", "at": (self.now + timedelta(minutes=10)).isoformat(),
            "hash": "b" * 64,
        }]
        supervisor.refresh_performance_state(
            self.root, status=self.status, events=open_events,
            now=self.now + timedelta(minutes=15),
        )
        ended_events = open_events + [{
            "kind": "human_pause_ended", "at": (self.now + timedelta(minutes=20)).isoformat(),
            "hash": "c" * 64,
        }]
        view = supervisor.refresh_performance_state(
            self.root, status=self.status, events=ended_events,
            now=self.now + timedelta(minutes=180),
        )
        self.assertEqual(view["state"], "paused_for_performance_review")
        record = json.loads((self.root / supervisor.RUNTIME_CONTROL_DIR /
                             supervisor.PERFORMANCE_RECORD).read_text())
        self.assertEqual(record["episodes"][0]["holds"][0]["ended_at"],
                         (self.now + timedelta(minutes=20)).isoformat())

    def test_cli_and_dashboard_expose_runtime_controls_and_telemetry(self):
        parser = supervisor.build_parser()
        self.assertEqual(parser.parse_args(["monitor-poll", "--owner", "host-a"]).command, "monitor-poll")
        self.assertEqual(parser.parse_args(["performance-status"]).command, "performance-status")
        dashboard = (ROOT / "bin" / "handsoff_dashboard.py").read_text()
        app = (ROOT / "dashboard" / "app.js").read_text()
        html = (ROOT / "dashboard" / "index.html").read_text()
        self.assertIn('"performance": performance', dashboard)
        for field in ("overall_percent", "forecast_remaining_seconds", "bottleneck", "forecast_variance_seconds"):
            self.assertIn(field, app)
        self.assertIn('id="metrics-performance-state"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
