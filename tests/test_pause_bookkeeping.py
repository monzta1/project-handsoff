"""#414: the performance pause bounds new work, not the ledger.

The run paused at 2 h and again about 2 h 40 min later. While paused,
`advance` and reviewer launches were refused, so the board stayed at 30%
for over an hour while implementers worked, and after a resume the run-owned
dashboard stopped refreshing ci-watch: the CI record stayed 'running' for
over 20 minutes with every check green.

While paused, bookkeeping that starts no new work stays allowed (advance,
pilot-note, work-item-update, record-evidence, record-symptom-resolved,
ci-watch, status, and a cache-only verify); a verify that would run a
command and a role launch stay refused. A Pilot-recorded standing decision
resumes each later pause by itself. #426's smoke parsing is tested here too.
"""
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

PAUSE_REFUSAL = "paused_for_performance_review"


class PausedRunFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true", "echo two"]', 1))
        r = run(["init", "Paused bookkeeping", "--item", "#414 the pause"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for args in (
            ["criterion-add", "REQ-001", "--type", "primary_fix", "--requirement",
             "[#414] the pause keeps bookkeeping", "--verification", "automated", "--test", "true"],
            ["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "[#414] a second check",
             "--verification", "automated", "--test", "echo two"],
            ["criterion-add", "REQ-003", "--type", "supporting", "--requirement", "[#414] seen by hand",
             "--verification", "manual", "--test", "manual: the pause is seen by hand"],
        ):
            r = run(args, cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # REQ-001's command runs before the pause, so its result is cached
        r = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.req1_run = json.loads(r.stdout)["criteria"]["REQ-001"]["run_id"]

    def pause(self, minutes=121):
        view = supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=minutes))
        self.assertTrue(view["block_new_work"], view)
        return view

    def events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if line.strip()]

    def assert_allowed(self, result):
        self.assertNotIn(PAUSE_REFUSAL, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BookkeepingWhilePaused(PausedRunFixture):
    def test_every_bookkeeping_command_passes_the_pause_gate(self):
        self.pause()
        for command in ("advance", "pilot-note", "work-item-update", "record-evidence",
                        "record-symptom-resolved", "ci-watch", "status", "verify"):
            with self.subTest(command=command):
                self.assertIsNone(supervisor.performance_mutation_refusal(self.tmp, command))

    def test_advance_succeeds_while_paused(self):
        self.pause()
        self.assert_allowed(run(["advance", "2", "20"], cwd=self.tmp))
        self.assertEqual(self.read_status()["phase_number"], 2)

    def test_pilot_note_succeeds_while_paused(self):
        self.pause()
        self.assert_allowed(run(["pilot-note", "--by", "moncy", "--text", "noted while paused"], cwd=self.tmp))

    def test_work_item_update_succeeds_while_paused(self):
        self.pause()
        self.assert_allowed(run(["work-item-update", "issue-414", "--by", "claude-host",
                                 "--notes", "still moving"], cwd=self.tmp))

    def test_record_evidence_succeeds_while_paused(self):
        self.pause()
        self.assert_allowed(run(["record-evidence", "REQ-003", "--kind", "manual", "--description",
                                 "seen by hand", "--by", "test-implementer"], cwd=self.tmp))

    def test_record_symptom_resolved_succeeds_while_paused(self):
        self.pause()
        self.assert_allowed(run(["record-symptom-resolved", "--evidence", self.req1_run,
                                 "--by", "test-implementer"], cwd=self.tmp))

    def test_status_succeeds_and_shows_the_pause(self):
        self.pause()
        r = run(["status"], cwd=self.tmp)
        self.assertNotIn("SHIP_FEATURE_BLOCKED", r.stdout)
        payload = json.loads(r.stdout)
        pause = payload["performance_pause"]
        self.assertIsNotNone(pause)
        self.assertTrue(pause["since"])
        self.assertGreaterEqual(pause["active_seconds"], 120 * 60)
        self.assertIn("performance-resume", pause["resume_command"])
        # first in the payload, so a host reading the top sees it
        self.assertEqual(next(iter(payload)), "performance_pause")

    def test_status_shows_no_pause_on_a_healthy_run(self):
        self.assertIsNone(json.loads(run(["status"], cwd=self.tmp).stdout)["performance_pause"])


class VerifyWhilePaused(PausedRunFixture):
    def test_a_cached_verify_binds(self):
        self.pause()
        r = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assert_allowed(r)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["launched"], [])
        self.assertIn("true", payload["reused"])
        self.assertTrue(payload["criteria"]["REQ-001"]["ok"])

    def test_a_cache_miss_is_refused_naming_performance_resume(self):
        self.pause()
        before = len(self.events())
        r = run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(PAUSE_REFUSAL, r.stdout)
        self.assertIn("performance-resume", r.stdout)
        self.assertIn("echo two", r.stdout)
        self.assertEqual(len(self.events()), before, "a refused verify records nothing")

    def test_no_cache_is_refused(self):
        self.pause()
        r = run(["verify", "--criterion", "REQ-001", "--no-cache", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(PAUSE_REFUSAL, r.stdout)
        self.assertIn("performance-resume", r.stdout)

    def test_a_cache_miss_runs_on_a_healthy_run(self):
        r = run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class NewWorkStaysBlocked(PausedRunFixture):
    def test_a_role_launch_is_refused(self):
        self.pause()
        for operation in ("launch_reviewer", "launch_implementer"):
            refusal = runtime._performance_refusal(self.tmp, operation)
            self.assertIsNotNone(refusal, operation)
            self.assertIn("performance-resume", refusal)
        self.assertIsNotNone(supervisor.performance_mutation_refusal(self.tmp, "launch_agent"))

    def test_other_new_work_is_refused(self):
        self.pause()
        for command in ("release-plan", "regression-request", "work-items-sync"):
            self.assertIsNotNone(supervisor.performance_mutation_refusal(self.tmp, command), command)


class CiWatchPollWhilePaused(PausedRunFixture):
    HEAD = "abc123def4567890"

    def _start_watch(self):
        checks = [{"name": "tests", "state": "IN_PROGRESS", "workflow": "CI"}]

        def gh(root, args, *rest):
            if args[:2] == ["pr", "view"]:
                return {"number": 7, "url": "https://example.invalid/pull/7", "headRefOid": self.HEAD}
            return checks if args[:2] == ["pr", "checks"] else []

        with mock.patch.object(lib, "_gh_json", side_effect=gh):
            lib.ci_watch_start(self.tmp, lib.load_config(self.tmp), pr=7, by="claude-host")
        self.assertEqual(self.read_status()["ci"]["state"], "running")

    def _green(self, root, pr, *args):
        return [{"name": "tests", "state": "SUCCESS", "started_at": None, "completed_at": None,
                 "link": None, "workflow": "CI"}]

    def test_poll_updates_the_run_while_paused(self):
        self._start_watch()
        self.pause()
        argv = ["handsoff_supervisor.py", "--root", str(self.tmp), "ci-watch", "--poll"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(lib, "_ci_checks", side_effect=self._green), \
                contextlib.redirect_stdout(out):
            code = supervisor.main()
        self.assertNotIn(PAUSE_REFUSAL, out.getvalue())
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.read_status()["ci"]["state"], "passed")
        self.assertIn("ci_passed", [event["kind"] for event in self.events()])

    def test_the_run_owned_dashboard_polls_ci_on_its_own_while_paused(self):
        import handsoff_dashboard as dashboard
        self._start_watch()
        self.pause()
        # the last fetch is older than the refresh interval, as it is a tick later
        side = lib.ci_side_path(self.tmp)
        record = json.loads(side.read_text())
        record["fetched_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        side.write_text(json.dumps(record))
        server = dashboard.DashboardServer.__new__(dashboard.DashboardServer)
        server.project_root = self.tmp
        with mock.patch.object(lib, "_ci_checks", side_effect=self._green):
            view = server._poll_ci()
        self.assertIsNotNone(view)
        self.assertEqual(self.read_status()["ci"]["state"], "passed")

    def test_the_clock_loop_polls_ci_for_a_run_owned_dashboard(self):
        import handsoff_dashboard as dashboard
        server = dashboard.DashboardServer.__new__(dashboard.DashboardServer)
        server.project_root = self.tmp
        server.run_token, server.root_sha256 = "token", "0" * 64  # owned_by_run
        server.stopping = False
        server._missing_root_ticks = 0

        class OneTick:
            calls = 0

            def wait(self, _seconds):
                OneTick.calls += 1
                return OneTick.calls > 1

        server._watchdog_stop = OneTick()
        with mock.patch.object(supervisor, "performance_tick"), \
                mock.patch.object(server, "_poll_ci", create=True) as poll:
            server._performance_clock_loop()
        poll.assert_called_once()


class AutoResume(PausedRunFixture):
    def _decide(self, *extra):
        r = run(["performance-auto-resume", "--by", "moncy", "--reason", "keep the run moving", *extra],
                cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return [e for e in self.events() if e["kind"] == supervisor.PERFORMANCE_AUTO_RESUME_EVENT][-1]

    def test_without_the_decision_the_pause_holds(self):
        self.pause()
        view = supervisor.refresh_performance_state(self.tmp)
        self.assertTrue(view["block_new_work"])
        self.assertNotIn("performance_auto_resumed", [e["kind"] for e in self.events()])

    def test_the_decision_resumes_the_next_pause_and_records_it(self):
        decision = self._decide()
        self.assertTrue(decision["auto_resume"])
        later = datetime.now(timezone.utc) + timedelta(minutes=121)
        view = supervisor.performance_tick(self.tmp, now=later)
        self.assertFalse(view["block_new_work"], view)
        self.assertEqual(view["state"], "active")
        self.assertEqual(view["episode_id"], "episode-2")
        self.assertEqual(view["transition"], "performance_auto_resumed")
        resumed = [e for e in self.events() if e["kind"] == "performance_auto_resumed"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["decision_id"], decision["decision_id"])
        self.assertEqual(resumed[0]["episode_id"], "episode-1")
        # a later refresh does not resume twice, and launches are open again
        again = supervisor.refresh_performance_state(self.tmp, now=later + timedelta(minutes=1))
        self.assertFalse(again["block_new_work"])
        self.assertEqual(len([e for e in self.events() if e["kind"] == "performance_auto_resumed"]), 1)
        # the new episode has its own ceiling, which the standing decision also resumes
        third = supervisor.performance_tick(self.tmp, now=later + timedelta(minutes=122))
        self.assertEqual(third["episode_id"], "episode-3")
        self.assertEqual(len([e for e in self.events() if e["kind"] == "performance_auto_resumed"]), 2)

    def test_withdrawing_the_decision_restores_the_manual_pause(self):
        self._decide()
        self._decide("--off")
        view = supervisor.performance_tick(self.tmp, now=datetime.now(timezone.utc) + timedelta(minutes=121))
        self.assertTrue(view["block_new_work"])

    def test_a_pause_that_began_before_the_decision_waits_for_an_explicit_resume(self):
        paused_at = datetime.now(timezone.utc) + timedelta(minutes=121)
        self.pause()  # pauses at about paused_at
        self._decide()
        # the fixture's clock runs ahead of the ledger's, so the decision is
        # placed after the pause the way it would be on a real run
        events = self.events()
        events[-1] = {**events[-1], "at": (paused_at + timedelta(minutes=5)).isoformat()}
        view = supervisor.refresh_performance_state(self.tmp, events=events,
                                                    now=paused_at + timedelta(minutes=6))
        self.assertTrue(view["block_new_work"])
        self.assertNotIn("performance_auto_resumed", [e["kind"] for e in self.events()])

    def test_editing_state_cannot_grant_it(self):
        status = self.read_status()
        status["performance_auto_resume"] = {"auto_resume": True, "decision_id": "forged"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status))
        self.assertIsNone(supervisor.performance_auto_resume_decision(self.events()))

    def test_the_decision_is_human_only(self):
        # the broker dispatches only the commands in its table
        self.assertNotIn("performance-auto-resume", broker.BROKER_REQUEST_FIELDS)
        self.assertNotIn("performance-resume", broker.BROKER_REQUEST_FIELDS)

    def test_status_names_the_standing_decision(self):
        decision = self._decide()
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(payload["performance_auto_resume"]["decision_id"], decision["decision_id"])


class UpdateSmokeParsingTests(unittest.TestCase):
    """#426 REQ-005: tests/live_update_smoke.py's classify_dry_run accepts a
    dry run blocked by another live managed session (the blocked lines name
    each session and INSTALL_CHECK_BLOCKED counts them), and keeps today's
    checks for the ok and failed outcomes."""

    TOOL_LINES = [
        "INSTALL_CHECK_OK",
        "handsoff already 0.5.13",
        "miner already 1.2.0",
        "sentinel would update 0.3.0 -> 0.3.1",
        "beakon already 2.0.0",
        "fleet skipped: no change",
    ]
    BLOCKED_LAST = "dry run: the install would be blocked; nothing else is checked"

    @classmethod
    def setUpClass(cls):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent / "live_update_smoke.py"
        spec = importlib.util.spec_from_file_location("live_update_smoke_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.smoke = module

    @staticmethod
    def _check(count):
        return (f"INSTALL_CHECK_BLOCKED: {count} live managed session(s); "
                "finish or cancel them, or --force --by <you>")

    def test_ok_output_is_ok(self):
        output = "\n".join(self.TOOL_LINES + ["UPDATE_OK (dry run)"]) + "\n"
        self.assertEqual(self.smoke.classify_dry_run(output, 0), "ok")
        self.assertEqual(self.smoke.classify_dry_run(output), "ok")

    def test_failed_output_is_failed(self):
        lines = list(self.TOOL_LINES)
        lines[4] = "beakon failed: local changes in the checkout"
        output = "\n".join(lines + ["UPDATE_FAILED: beakon"])
        self.assertEqual(self.smoke.classify_dry_run(output, 1), "failed")
        with self.assertRaises(AssertionError):
            self.smoke.classify_dry_run(output, 0)

    def test_blocked_output_names_the_live_sessions(self):
        output = "\n".join([
            "blocked: /Users/pilot/Projects/app reviewer 20261008T101500Z-ab12cd",
            "blocked: /Users/pilot/Projects/site implementer 20261008T101700Z-ef34gh",
            self._check(2), self.BLOCKED_LAST,
        ])
        self.assertEqual(self.smoke.classify_dry_run(output, 1), "blocked")
        with self.assertRaises(AssertionError):
            self.smoke.classify_dry_run(output, 0)

    def test_blocked_output_must_name_every_counted_session(self):
        session = "blocked: /Users/pilot/Projects/app reviewer 20261008T101500Z-ab12cd"
        for lines in ([self._check(1), self.BLOCKED_LAST],
                      [session, self._check(3), self.BLOCKED_LAST],
                      [session, self.BLOCKED_LAST]):
            with self.subTest(lines=lines):
                with self.assertRaises(AssertionError):
                    self.smoke.classify_dry_run("\n".join(lines), 1)

    def test_other_last_lines_are_refused(self):
        for output in ("", "UPDATE_FAILED: install blocked",
                       "\n".join(self.TOOL_LINES + ["UPDATE_FAILED: handsoff"])):
            with self.subTest(output=output[-40:]):
                with self.assertRaises(AssertionError):
                    self.smoke.classify_dry_run(output)

    def test_importing_the_smoke_runs_nothing(self):
        self.assertTrue(callable(self.smoke.main))
        self.assertFalse(hasattr(self.smoke, "completed"))


if __name__ == "__main__":
    unittest.main()
