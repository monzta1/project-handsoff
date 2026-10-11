#!/usr/bin/env python3
"""#447: a launched regression whose runner died is reported as stranded,
and every refusal on it names regression-finalize."""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_progress as test_progress  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


class RunnerKilled(Exception):
    """Stands in for the reboot: the runner dies after the launched commit."""


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class StrandedFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-stranded-")).resolve()
        for name in ("handsoff.toml", ".gitignore"):
            shutil.copy(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        for args in (["init", "-q"], ["config", "user.email", "test@example.com"],
                     ["config", "user.name", "Stranded Test"], ["add", "."],
                     ["commit", "-qm", "fixture"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        for args in (("init", "Stranded regression #447"),
                     ("release-plan", "--version", "v1.0.0", "--by", "Pilot")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=60,
        )

    def status(self):
        return json.loads((self.root / "handsoff-status.json").read_text())

    def launch(self, *, pid=None):
        """Request, accept and launch a regression whose runner then dies."""
        result = self.cli("regression-request", "--group", "python-full", "--by", "claude-host",
                          "--reason", "full pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rid = self.status()["regression_requests"][-1]["request_id"]
        result = self.cli("regression-decide", "--request-id", rid, "--accept", "--by", "Pilot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = argparse.Namespace(root=str(self.root), request_id=rid, by="claude-host")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(supervisor.regress, "run_battery_results",
                                                  side_effect=RunnerKilled))
            if pid is not None:
                stack.enter_context(mock.patch.object(supervisor.os, "getpid", return_value=pid))
            with self.assertRaises(RunnerKilled):
                supervisor.cmd_regression_run(args)
        item = self.status()["regression_requests"][-1]
        self.assertEqual(item["state"], "launched")
        return rid

    def heartbeat(self, rid, *, age_seconds, execution_request=None):
        """The progress file as the dead runner left it, last heartbeat `age_seconds` ago."""
        test_progress.start(
            self.root, run_id="run", source="regression", label="python-full",
            units=["Command 1"], request_id=execution_request or rid,
            now=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        )

    def regression_events(self):
        cfg = lib.load_config(self.root)
        return [event for event in lib.read_events(self.root, cfg)
                if str(event.get("kind", "")).startswith("regression_")]

    def finalize_line(self, rid):
        return f"regression-finalize --request-id {rid}"


class StrandedStatusTests(StrandedFixture):
    def test_status_reports_a_dead_runner_as_stranded_and_names_finalize(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS + 60)
        before = self.status()
        events_before = self.regression_events()
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        liveness = payload["regression_liveness"]
        self.assertEqual(liveness["request_id"], rid)
        self.assertEqual(liveness["state"], "stranded")
        self.assertEqual(liveness["signal"], "heartbeat")
        self.assertGreater(liveness["silent_seconds"], lib.STRANDED_REGRESSION_SECONDS)
        self.assertIn(self.finalize_line(rid), liveness["required_action"])
        self.assertIn(self.finalize_line(rid), payload["next_action"])
        self.assertEqual(payload["activity"]["assessment"]["reason"], "regression_stranded")
        # the report adds no write: request, stored next_action, regression events unchanged
        after = self.status()
        self.assertEqual(after["regression_requests"], before["regression_requests"])
        self.assertEqual(after["regression_requests"][-1]["state"], "launched")
        self.assertEqual(after["next_action"], before["next_action"])
        self.assertNotIn("regression-finalize", after["next_action"])
        self.assertEqual(self.regression_events(), events_before)

    def test_status_keeps_a_heartbeating_regression_running(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=1)
        payload = json.loads(self.cli("status").stdout)
        liveness = payload["regression_liveness"]
        self.assertEqual(liveness["state"], "running")
        self.assertIsNone(liveness["required_action"])
        self.assertNotIn("regression-finalize", payload["next_action"])
        self.assertEqual(payload["next_action"], self.status()["next_action"])

    def test_status_without_a_launched_regression_has_no_liveness(self):
        payload = json.loads(self.cli("status").stdout)
        self.assertIsNone(payload["regression_liveness"])


class StrandedRecoverTests(StrandedFixture):
    def test_recover_dry_run_names_finalize_for_a_stranded_regression(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS + 60)
        result = self.cli("recover", "--by", "claude-host", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        assessment = json.loads(result.stdout)
        self.assertEqual(assessment["state"], "not_applicable")
        self.assertEqual(assessment["reason"], "regression_stranded")
        self.assertIn(self.finalize_line(rid), assessment["required_action"])

    def test_recover_names_finalize_and_writes_nothing(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS + 60)
        before = self.durable()
        result = self.cli("recover", "--by", "claude-host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RECOVERY_SKIPPED: regression_stranded", result.stdout)
        self.assertIn(self.finalize_line(rid), result.stdout)
        self.assertEqual(self.durable(), before)

    def test_stranded_outranks_the_run_state_exclusions(self):
        # a blocked or ready_to_deploy run is excluded from recovery, but its
        # stranded launch still has to name the way out (status and recover)
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS + 300)
        path = self.root / "handsoff-status.json"
        original = path.read_text()
        for run_state in ("blocked", "ready_to_deploy"):
            with self.subTest(run_state=run_state):
                status = json.loads(original)
                status["status"] = run_state
                path.write_text(json.dumps(status, indent=2))
                payload = json.loads(self.cli("status").stdout)
                self.assertEqual(payload["status"], run_state)
                self.assertEqual(payload["activity"]["assessment"]["reason"], "regression_stranded")
                assessment = json.loads(self.cli("recover", "--by", "claude-host", "--dry-run").stdout)
                self.assertEqual(assessment["reason"], "regression_stranded")
                self.assertIn(self.finalize_line(rid), assessment["required_action"])
                result = self.cli("recover", "--by", "claude-host")
                self.assertIn("RECOVERY_SKIPPED: regression_stranded", result.stdout, result.stderr)
                self.assertIn(self.finalize_line(rid), result.stdout)

    def test_recover_keeps_regression_pending_while_the_runner_heartbeats(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=1)
        assessment = json.loads(self.cli("recover", "--by", "claude-host", "--dry-run").stdout)
        self.assertEqual(assessment["reason"], "regression_pending")
        self.assertNotIn("required_action", assessment)
        before = self.durable()
        result = self.cli("recover", "--by", "claude-host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RECOVERY_SKIPPED: regression_pending", result.stdout)
        self.assertNotIn("regression-finalize", result.stdout)
        self.assertEqual(self.durable(), before)

    def durable(self):
        cfg = lib.load_config(self.root)
        return ((self.root / "handsoff-status.json").read_bytes(),
                lib.event_log_path(self.root, cfg).read_bytes())


class StrandedLivenessTests(StrandedFixture):
    def later(self, seconds):
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def test_regression_run_records_the_runner_pid_and_the_status_validates(self):
        self.launch()
        item = self.status()["regression_requests"][-1]
        self.assertEqual(item["runner_pid"], supervisor.os.getpid())
        result = self.cli("status")
        self.assertEqual(json.loads(result.stdout)["validation"], "valid", result.stdout)

    def test_a_stale_own_heartbeat_is_stranded_even_when_the_pid_is_alive(self):
        # after a reboot the recorded pid can belong to an unrelated process;
        # the runner's own heartbeat thread is the authoritative signal
        rid = self.launch()
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS + 60)
        liveness = lib.regression_liveness(self.root, self.status())
        self.assertTrue(liveness["runner_alive"])
        self.assertEqual(liveness["state"], "stranded")

    def test_threshold_boundary_on_the_heartbeat(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=lib.STRANDED_REGRESSION_SECONDS - 30)
        self.assertEqual(lib.regression_liveness(self.root, self.status())["state"], "running")
        self.assertEqual(lib.regression_liveness(self.root, self.status(), now=self.later(60))["state"],
                         "stranded")

    def test_without_its_own_progress_a_dead_pid_strands_after_the_threshold(self):
        rid = self.launch(pid=dead_pid())
        self.heartbeat(rid, age_seconds=0, execution_request="rg-" + "0" * 32)  # another writer replaced it
        status = self.status()
        early = lib.regression_liveness(self.root, status)
        self.assertEqual((early["state"], early["signal"]), ("running", "launch"))
        late = lib.regression_liveness(self.root, status, now=self.later(lib.STRANDED_REGRESSION_SECONDS + 5))
        self.assertEqual(late["state"], "stranded")
        self.assertIn(self.finalize_line(rid), late["required_action"])
        (self.root / test_progress.PROGRESS_FILE).unlink()
        self.assertEqual(lib.regression_liveness(self.root, status, now=self.later(
            lib.STRANDED_REGRESSION_SECONDS + 5))["state"], "stranded")

    def test_without_its_own_progress_a_live_pid_keeps_it_running(self):
        self.launch()
        late = lib.regression_liveness(self.root, self.status(),
                                       now=self.later(lib.STRANDED_REGRESSION_SECONDS + 5))
        self.assertEqual((late["state"], late["runner_alive"]), ("running", True))

    def test_a_pre_fix_request_without_a_pid_strands_on_time_alone(self):
        rid = self.launch(pid=dead_pid())
        status = self.status()
        del status["regression_requests"][-1]["runner_pid"]
        late = lib.regression_liveness(self.root, status, now=self.later(lib.STRANDED_REGRESSION_SECONDS + 5))
        self.assertEqual((late["state"], late["runner_pid"], late["runner_alive"]), ("stranded", None, False))
        self.assertIn(self.finalize_line(rid), late["required_action"])

    def test_finalize_frees_the_run(self):
        rid = self.launch(pid=dead_pid())
        result = self.cli("regression-finalize", "--request-id", rid, "--by", "Pilot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(lib.regression_liveness(self.root, self.status()))


class StrandedRefusalTests(StrandedFixture):
    def assertNamesFinalize(self, result, rid):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REGRESSION_BLOCKED", result.stdout)
        self.assertIn(self.finalize_line(rid), result.stdout)

    def test_regression_run_refusal_names_finalize(self):
        rid = self.launch(pid=dead_pid())
        self.assertNamesFinalize(self.cli("regression-run", "--request-id", rid, "--by", "claude-host"), rid)

    def test_regression_cancel_refusal_names_finalize(self):
        rid = self.launch(pid=dead_pid())
        self.assertNamesFinalize(self.cli("regression-cancel", "--request-id", rid, "--by", "claude-host"), rid)

    def test_regression_request_refusal_names_finalize(self):
        rid = self.launch(pid=dead_pid())
        self.assertNamesFinalize(self.cli("regression-request", "--group", "python-full",
                                          "--by", "claude-host", "--reason", "retry"), rid)

    def test_workflow_mutation_lock_names_finalize(self):
        rid = self.launch(pid=dead_pid())
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.ensure_no_launched_regression(self.status())
        self.assertIn(self.finalize_line(rid), str(caught.exception))

    def test_pending_requests_keep_their_plain_refusals(self):
        result = self.cli("regression-request", "--group", "python-full", "--by", "claude-host",
                          "--reason", "full pass")
        rid = self.status()["regression_requests"][-1]["request_id"]
        run = self.cli("regression-run", "--request-id", rid, "--by", "claude-host")
        self.assertIn("REGRESSION_BLOCKED: request has not been accepted", run.stdout, result.stdout)
        self.assertNotIn("regression-finalize", run.stdout)


if __name__ == "__main__":
    unittest.main()
