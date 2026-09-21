"""#181: CI as a step of the run. ci-watch records what the run waits for,
ci_view mirrors the PR's checks onto the snapshot at most once a minute and
records the terminal event once, a red check refuses the Phase 7
transition, and nothing token-like ever reaches the ledger, the side file
or the snapshot. No test reaches the real gh: a fake runner answers, and a
PATH shim marks any real invocation."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402

T0 = datetime(2026, 9, 21, 1, 35, 25, tzinfo=timezone.utc)
SECRET = "ghp_SECRETTOKEN4242ABCDEF"


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class FakeGh:
    """Answers gh pr view, gh pr checks and gh run list from canned data
    and counts the calls. Its environment carries a token so the grep in
    the last test means something."""

    def __init__(self, checks, runs=None, head="abc123def4567890", url="https://github.com/monzta1/x/pull/7"):
        self.checks = checks
        self.runs = runs if runs is not None else {}
        self.head = head
        self.url = url
        self.calls = []
        self.environment = {"GH_TOKEN": SECRET}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert argv[0] == "gh" and kwargs.get("shell") is False
        sub = tuple(argv[1:3])

        class Result:
            returncode = 0
            stderr = ""
            stdout = "null"
        result = Result()
        if sub == ("pr", "view"):
            result.stdout = json.dumps({"number": int(argv[3]), "url": self.url, "headRefOid": self.head})
        elif sub == ("pr", "checks"):
            result.stdout = json.dumps(self.checks)
        elif sub == ("run", "list"):
            name = argv[argv.index("--workflow") + 1]
            result.stdout = json.dumps(self.runs.get(name, []))
        else:
            result.returncode = 1
            result.stderr = "unknown gh call"
        return result


def _check(name, state, workflow="CI", started=None, completed=None, link=None):
    return {"name": name, "state": state, "workflow": workflow,
            "startedAt": _iso(started) if started else None, "completedAt": _iso(completed) if completed else None,
            "link": link or f"https://github.com/monzta1/x/actions/runs/1/job/{abs(hash(name)) % 1000}"}


def shard_checks(done=0, failed=None):
    """The real shape from #180: six python shards, dashboard, tests."""
    names = [f"python (shard {i} of 6)" for i in range(6)] + ["dashboard", "tests"]
    checks = []
    for i, name in enumerate(names):
        if name == failed:
            checks.append(_check(name, "FAILURE", started=T0, completed=T0 + timedelta(seconds=70)))
        elif i < done:
            checks.append(_check(name, "SUCCESS", started=T0, completed=T0 + timedelta(seconds=70 + i)))
        else:
            checks.append(_check(name, "IN_PROGRESS", started=T0))
    return checks


PREVIOUS_RUN = {"CI": [{"createdAt": "2026-09-21T01:16:07Z", "updatedAt": "2026-09-21T01:18:11Z",
                        "url": "https://github.com/monzta1/x/actions/runs/35550347105"}]}


class CiWatchTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        # any real gh invocation leaves a marker
        self.shim_dir = Path(tempfile.mkdtemp(prefix="handsoff-gh-shim-"))
        self.gh_marker = self.shim_dir / "gh-was-called"
        shim = self.shim_dir / "gh"
        shim.write_text(f"#!/bin/sh\ntouch '{self.gh_marker}'\nexit 1\n")
        shim.chmod(0o755)
        self._path_before = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.shim_dir}{os.pathsep}{self._path_before}"
        self.init("CI as a step of the run")
        self.cfg = lib.load_config(self.tmp)
        self.which = lambda name: "/usr/local/bin/gh" if name == "gh" else None

    def tearDown(self):
        os.environ["PATH"] = self._path_before
        self.assertFalse(self.gh_marker.exists(), "a test invoked the real gh executable")
        shutil.rmtree(self.shim_dir, ignore_errors=True)
        super().tearDown()

    def _events(self, kind=None):
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if line.strip()]
        return [e for e in events if kind is None or e["kind"] == kind]

    def _start(self, gh, at=T0):
        return lib.ci_watch_start(self.tmp, self.cfg, pr=7, by="claude-host", runner=gh, which=self.which, now=at)

    def _view(self, gh, at, **kw):
        status = self.read_status()
        return lib.ci_view(status, self.tmp, self.cfg, now=at, runner=gh, which=self.which, **kw)

    # -- REQ-001 -----------------------------------------------------------

    def test_watch_records_the_event_with_head_checks_and_expected_from_the_previous_run(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        watch = self._start(gh)
        self.assertEqual((watch["pr"], watch["head"], watch["state"]), (7, "abc123def4567890", "running"))
        self.assertEqual(watch["expected_seconds"], 124.0)
        self.assertEqual(watch["expected_source"], PREVIOUS_RUN["CI"][0]["url"])
        event = self._events("ci_watch_started")[-1]
        self.assertEqual(event["pr"], 7)
        self.assertEqual(event["head"], "abc123def4567890")
        self.assertEqual(event["url"], "https://github.com/monzta1/x/pull/7")
        self.assertEqual(len(event["checks"]), 8)
        self.assertEqual(event["expected_seconds"], 124.0)
        self.assertEqual(self.read_status()["ci"]["watched_by"], "claude-host")
        side = json.loads((self.tmp / lib.CI_FILE).read_text())
        self.assertEqual(side["head"], "abc123def4567890")
        self.assertEqual(len(side["checks"]), 8)
        # the side file is Handsoff state, never repository content
        self.assertTrue(lib._digest_excluded(lib.CI_FILE, set()))

    def test_expected_is_the_slowest_of_several_workflows_sorted(self):
        checks = [_check("tests", "IN_PROGRESS", workflow="CI"), _check("bundle", "IN_PROGRESS", workflow="Bundle")]
        runs = {"CI": PREVIOUS_RUN["CI"],
                "Bundle": [{"createdAt": "2026-09-21T01:00:00Z", "updatedAt": "2026-09-21T01:09:30Z",
                            "url": "https://github.com/monzta1/x/actions/runs/2"}]}
        gh = FakeGh(checks, runs=runs)
        watch = self._start(gh)
        self.assertEqual(watch["expected_seconds"], 570.0)
        self.assertEqual(watch["expected_source"], "https://github.com/monzta1/x/actions/runs/2")
        asked = [c[c.index("--workflow") + 1] for c in gh.calls if c[1:3] == ["run", "list"]]
        self.assertEqual(asked, ["Bundle", "CI"], "one lookup per workflow, in sorted order")

    def test_no_history_means_null_expected_and_the_note(self):
        gh = FakeGh(shard_checks())
        watch = self._start(gh)
        self.assertIsNone(watch["expected_seconds"])
        self.assertIsNone(watch["expected_source"])
        view = self._view(gh, T0 + timedelta(seconds=30))
        self.assertIsNone(view["progress"])
        self.assertEqual(view["note"], "no previous run to compare")

    def test_view_refreshes_through_gh_at_most_once_a_minute(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        self._start(gh)
        checks_calls = lambda: sum(1 for c in gh.calls if c[1:3] == ["pr", "checks"])
        self.assertEqual(checks_calls(), 1)
        view = self._view(gh, T0 + timedelta(seconds=20))
        self.assertEqual(checks_calls(), 1, "20 s old: served from the side file")
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["elapsed_seconds"], 20.0)
        self.assertAlmostEqual(view["progress"], 20 / 124, places=4)
        self.assertEqual(len(view["checks"]), 8)
        gh.checks = shard_checks(done=3)
        view = self._view(gh, T0 + timedelta(seconds=61))
        self.assertEqual(checks_calls(), 2, "61 s old: refreshed")
        self.assertEqual(sum(1 for c in view["checks"] if c["state"] == "SUCCESS"), 3)
        self.assertEqual(view["note"], None)
        # the cells carry names, states, elapsed and links: six shards plus two
        names = [c["name"] for c in view["checks"]]
        self.assertEqual(sorted(names), sorted([f"python (shard {i} of 6)" for i in range(6)] + ["dashboard", "tests"]))
        done = next(c for c in view["checks"] if c["state"] == "SUCCESS")
        self.assertGreaterEqual(done["elapsed_seconds"], 70)
        self.assertTrue(done["link"].startswith("https://"))
        view = self._view(gh, T0 + timedelta(seconds=70), force=True)
        self.assertEqual(checks_calls(), 3, "--poll forces a refresh")

    def test_snapshot_carries_the_view_and_a_gh_error_becomes_a_note(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        # build_snapshot uses the real runner and the wall clock: a side file
        # written just now is under 60 s old, so gh is not asked (the PATH
        # shim would mark it)
        self._start(gh, at=datetime.now(timezone.utc))
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["ci"]["pr"], 7)
        self.assertEqual(len(snapshot["ci"]["checks"]), 8)
        self.assertEqual(snapshot["ci"]["state"], "running")
        # no watch: no row
        status = self.read_status()
        status.pop("ci")
        self.assertIsNone(lib.ci_view(status, self.tmp, self.cfg, now=T0, runner=gh, which=self.which))

    # -- REQ-002 -----------------------------------------------------------

    def test_all_green_records_ci_passed_exactly_once(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        self._start(gh)
        gh.checks = shard_checks(done=8)
        at = T0 + timedelta(seconds=120)
        view = self._view(gh, at)
        self.assertEqual(view["state"], "passed")
        self.assertEqual(view["progress"], 1.0)
        self.assertEqual(view["elapsed_seconds"], 120.0)
        self.assertIsNone(view["failed_check"])
        self.assertEqual(len(self._events("ci_passed")), 1)
        calls_before = len(gh.calls)
        for offset in (130, 300, 9000):
            again = self._view(gh, T0 + timedelta(seconds=offset))
            self.assertEqual(again["state"], "passed")
            self.assertEqual(again["elapsed_seconds"], 120.0, "elapsed is frozen at the terminal time")
        self.assertEqual(len(self._events("ci_passed")), 1, "terminal event written once")
        self.assertEqual(len(gh.calls), calls_before, "a terminal watch never asks gh again")
        self.assertEqual(lib.ci_gate_errors(self.read_status()), [])

    def test_a_red_check_records_ci_failed_once_and_blocks_phase_7_until_a_new_head(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        self._start(gh)
        gh.checks = shard_checks(done=8, failed="python (shard 4 of 6)")
        view = self._view(gh, T0 + timedelta(seconds=90))
        self.assertEqual(view["state"], "failed")
        self.assertEqual(view["failed_check"], "python (shard 4 of 6)")
        self.assertEqual(len(self._events("ci_failed")), 1)
        self.assertEqual(self._events("ci_failed")[0]["failed_check"], "python (shard 4 of 6)")
        self._view(gh, T0 + timedelta(seconds=200))
        self.assertEqual(len(self._events("ci_failed")), 1)
        # the gate refuses the 6 to 7 transition on the proposed status
        status = self.read_status()
        proposed = {**status, "phase_number": 7, "phase": lib.PHASES[7]}
        errors = lib.ci_gate_errors(proposed)
        self.assertEqual(len(errors), 1)
        self.assertIn("python (shard 4 of 6) failed on PR #7", errors[0])
        self.assertIn("https://github.com/monzta1/x/pull/7", errors[0])
        self.assertEqual(lib.ci_gate_errors({**status, "phase_number": 6}), errors, "the gate reads the watch, the phase filter is compute_errors'")
        acceptance = self.read_acceptance()
        full = lib.compute_errors(proposed, acceptance, self.cfg, now=T0 + timedelta(seconds=200))
        self.assertTrue(any(e.startswith("CI: python (shard 4 of 6) failed") for e in full), full)
        at_six = lib.compute_errors({**status, "phase_number": 6, "phase": lib.PHASES[6]}, acceptance, self.cfg, now=T0)
        self.assertFalse(any(e.startswith("CI:") for e in at_six), "a failed watch does not touch Phase 6 itself")
        # a fix is pushed: a watch on the new head replaces the failed one
        gh.head = "fedcba9876543210"
        gh.checks = shard_checks()
        watch = self._start(gh, at=T0 + timedelta(seconds=400))
        self.assertEqual((watch["state"], watch["head"]), ("running", "fedcba9876543210"))
        self.assertEqual(lib.ci_gate_errors({**self.read_status(), "phase_number": 7}), [])
        self.assertEqual(len(self._events("ci_watch_started")), 2)

    def test_nothing_token_like_reaches_the_ledger_the_side_file_or_the_snapshot(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        os.environ["GH_TOKEN"] = SECRET
        try:
            self._start(gh)
            gh.checks = shard_checks(done=8)
            self._view(gh, T0 + timedelta(seconds=120))
            # the snapshot's own refresh must not reach the real gh either: the
            # watch is terminal, so it is served from the side file
            snapshot = json.dumps(dashboard.build_snapshot(self.tmp))
        finally:
            os.environ.pop("GH_TOKEN", None)
        for name, text in (("events", (self.tmp / "handsoff-events.jsonl").read_text()),
                           ("status", (self.tmp / "handsoff-status.json").read_text()),
                           ("side", (self.tmp / lib.CI_FILE).read_text()),
                           ("snapshot", snapshot)):
            self.assertNotIn(SECRET, text, name)
            self.assertNotIn("GH_TOKEN", text, name)
            self.assertNotIn("ghp_", text, name)

    # -- the command -------------------------------------------------------

    def test_the_command_refuses_without_gh_and_without_a_pr_and_prints_the_view_on_poll(self):
        r = run(["ci-watch"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CI_WATCH_BLOCKED: give --pr N", r.stdout)
        # the PATH shim is the only gh: the real command refuses with the shim's failure, never a traceback
        r = run(["ci-watch", "--pr", "7", "--by", "claude-host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("CI_WATCH_BLOCKED: gh pr view failed", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.gh_marker.unlink()
        r = run(["ci-watch", "--poll"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CI_WATCH_NONE", r.stdout)
        with self.assertRaisesRegex(lib.HandsoffError, "needs the gh CLI"):
            lib.ci_watch_start(self.tmp, self.cfg, pr=7, by="x", runner=FakeGh([]), which=lambda name: None)
        with self.assertRaisesRegex(lib.HandsoffError, "--pr must be a pull request number"):
            lib.ci_watch_start(self.tmp, self.cfg, pr="seven", by="x", runner=FakeGh([]), which=self.which)


if __name__ == "__main__":
    unittest.main()
