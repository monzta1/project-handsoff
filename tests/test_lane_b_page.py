"""Lane B (#186, #183, #184, #181 follow-ups): the page says what matters.
The host family from an actor prefix and never a guess; the console
without the three engine rows; the engine view without previews; the CI
row's estimate from job time, its queued cells, a rerun that greens a red
watch, the gate line, and --poll printing the ledger's state."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.test_ci_watch import FakeGh, PREVIOUS_RUN, T0, _run, shard_checks

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
from tests.fixture_state import write_version_pin


class HostIdentityTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def test_init_by_records_the_actor_and_the_page_reads_the_family_everywhere(self):
        r = run(["init", "Lane B host", "--by", "codex-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        initialized = next(e for e in self._events() if e["kind"] == "initialized")
        self.assertEqual(initialized["by"], "codex-implementer")
        host = lib.host_identity(self.read_status(), self._events())
        self.assertEqual(host, {"family": "codex", "actor": "codex-implementer", "source": "initialized"})
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["host"], host)
        fleet.register_project(self.tmp, self.registry)
        card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        self.assertEqual(card["host"], "codex")

    def test_without_init_by_the_newest_host_command_actor_names_the_family(self):
        self.init("Lane B ledger host")
        self.assertEqual(lib.host_identity(self.read_status(), self._events()),
                         {"family": "unknown", "actor": None, "source": "none"})
        r = run(["pilot-note", "--by", "claude-host", "--text", "a note"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        host = lib.host_identity(self.read_status(), self._events())
        self.assertEqual((host["family"], host["actor"], host["source"]), ("claude", "claude-host", "ledger"))
        # a managed reviewer's verdict is not a host command: it never names the host
        events = self._events() + [{"kind": "review_approved", "by": "codex-reviewer"}]
        self.assertEqual(lib.host_identity(self.read_status(), events)["family"], "claude")
        # an actor without a family prefix is never guessed
        self.assertEqual(lib.host_identity({}, [{"kind": "initialized", "by": "moncy"},
                                               {"kind": "pilot_note", "by": "moncy"}]),
                         {"family": "unknown", "actor": None, "source": "none"})
        self.assertEqual(lib.host_identity({"implemented_by": "codex-implementer"}, [])["source"], "ledger")


class ConsoleTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        self.init("Lane B console")

    def test_the_three_engine_rows_are_gone_and_the_engine_view_has_no_previews(self):
        snapshot = dashboard.build_snapshot(self.tmp)
        kinds = [item["kind"] for item in snapshot["operations"]["inventory"]]
        self.assertFalse(set(kinds) & {"engine_upgrade", "engine_rollback", "engine_migrate"})
        self.assertFalse([i for i in snapshot["operations"]["inventory"] if i["availability"] == "read_only"])
        engine = snapshot["operations"]["engine"]
        self.assertNotIn("previews", engine)
        self.assertIsNone(engine["reason"])
        self.assertEqual(sorted(engine["commands"]), sorted(["install", "upgrade_preview", "upgrade", "rollback_preview",
                                                             "rollback", "migrate_preview", "migrate", "doctor"]))
        # the page no longer renders the fixed copy or the previews
        app = (BIN.parent / "dashboard" / "app.js").read_text()
        html = (BIN.parent / "dashboard" / "index.html").read_text()
        self.assertNotIn("supervisor-reassurance", app)
        self.assertNotIn("supervisor-reassurance", html)
        self.assertNotIn("engine.previews", app)
        self.assertNotIn("engine.execution_reason", app)


class CiRowTruthTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        self.shim_dir = Path(tempfile.mkdtemp(prefix="handsoff-gh-shim-"))
        self.gh_marker = self.shim_dir / "gh-was-called"
        shim = self.shim_dir / "gh"
        shim.write_text(f"#!/bin/sh\ntouch '{self.gh_marker}'\nexit 1\n")
        shim.chmod(0o755)
        self._path_before = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.shim_dir}{os.pathsep}{self._path_before}"
        self.init("Lane B CI row")
        self.cfg = lib.load_config(self.tmp)
        self.which = lambda name: "/usr/local/bin/gh" if name == "gh" else None

    def tearDown(self):
        os.environ["PATH"] = self._path_before
        self.assertFalse(self.gh_marker.exists(), "a test invoked the real gh executable")
        shutil.rmtree(self.shim_dir, ignore_errors=True)
        super().tearDown()

    def _events(self, kind):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if l.strip() and json.loads(l)["kind"] == kind]

    def test_expected_is_the_median_of_five_runs_job_time_not_the_queue(self):
        runs = {"CI": [
            _run(1, "https://x/runs/1", 130, 6),
            _run(2, "https://x/runs/2", 90, 6, queued=270),   # the 6-minute run: 4.5 min queued, 1.5 min of work
            _run(3, "https://x/runs/3", 125, 6),
            _run(4, "https://x/runs/4", 400),
            _run(5, "https://x/runs/5", 110),
        ]}
        gh = FakeGh(shard_checks(), runs=runs)
        watch = lib.ci_watch_start(self.tmp, self.cfg, pr=7, by="claude-host", runner=gh, which=self.which, now=T0)
        self.assertEqual(watch["expected_seconds"], 125.0, "median of 130, 90, 125, 400, 110; the queue never counts")
        self.assertEqual(watch["expected_source"].split(", "), [f"https://x/runs/{i}" for i in range(1, 6)])
        views = [c for c in gh.calls if c[1:3] == ["run", "view"]]
        self.assertEqual(len(views), 5, "one jobs lookup per sampled run, once, at watch start")
        before = len(gh.calls)
        lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=61), runner=gh, which=self.which)
        self.assertEqual([c for c in gh.calls[before:] if c[1] == "run"], [], "the refresh path never asks about runs")
        # an even sample takes the mean of the middle two
        self.assertEqual(lib._median([1.0, 2.0, 3.0, 4.0]), 2.5)
        self.assertIsNone(lib._median([]))

    def test_a_queued_check_reads_queued(self):
        checks = shard_checks()
        checks[3]["startedAt"] = None
        checks[3]["state"] = "QUEUED"
        gh = FakeGh(checks, runs=PREVIOUS_RUN)
        lib.ci_watch_start(self.tmp, self.cfg, pr=7, by="claude-host", runner=gh, which=self.which, now=T0)
        view = lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=5), runner=gh, which=self.which)
        queued = [c for c in view["checks"] if c["queued"]]
        self.assertEqual([c["name"] for c in queued], ["python (shard 3 of 6)"])
        self.assertIsNone(queued[0]["elapsed_seconds"])
        self.assertFalse(any(c["queued"] for c in view["checks"] if c["state"] == "IN_PROGRESS"))

    def test_a_rerun_greens_a_red_watch_on_the_same_head_and_the_gate_line_says_rerun(self):
        gh = FakeGh(shard_checks(), runs=PREVIOUS_RUN)
        lib.ci_watch_start(self.tmp, self.cfg, pr=7, by="claude-host", runner=gh, which=self.which, now=T0)
        gh.checks = shard_checks(done=8, failed="python (shard 2 of 6)")
        view = lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=90), runner=gh, which=self.which)
        self.assertEqual(view["state"], "failed")
        errors = lib.ci_gate_errors({**self.read_status(), "phase_number": 7})
        self.assertEqual(errors, ["CI: python (shard 2 of 6) failed on PR #7 (https://github.com/monzta1/x/pull/7); "
                                  "rerun or push, then ci-watch --pr 7 again"])
        # still red on the next refresh: no second ci_failed, no gh call inside the window
        calls = len(gh.calls)
        lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=100), runner=gh, which=self.which)
        self.assertEqual(len(gh.calls), calls, "inside the refresh window a red watch is served from the side file")
        self.assertEqual(len(self._events("ci_failed")), 1)
        # the operator reruns the failed job on GitHub; the same head goes green
        gh.checks = shard_checks(done=8)
        view = lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=200), runner=gh, which=self.which)
        self.assertEqual(view["state"], "passed")
        self.assertIsNone(view["failed_check"])
        passed = self._events("ci_passed")
        self.assertEqual(len(passed), 1)
        self.assertTrue(passed[0]["after_rerun"])
        self.assertIn("after a rerun", passed[0]["message"])
        self.assertEqual(lib.ci_gate_errors({**self.read_status(), "phase_number": 7}), [])
        self.assertEqual(len(self._events("ci_failed")), 1, "a rerun that stays red writes nothing new")
        # a terminal passed watch never asks gh again
        calls = len(gh.calls)
        lib.ci_view(self.read_status(), self.tmp, self.cfg, now=T0 + timedelta(seconds=900), runner=gh, which=self.which, force=True)
        self.assertEqual(len(gh.calls), calls)

    def test_poll_prints_the_state_the_ledger_holds_after_the_poll(self):
        # the command uses the real runner; the PATH shim answers, so this checks the CLI's own shape
        source = (BIN / "handsoff_supervisor.py").read_text()
        self.assertIn('view = {**view, "state": after.get("state", view["state"])', source)
        r = run(["ci-watch", "--poll"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CI_WATCH_NONE", r.stdout)


if __name__ == "__main__":
    unittest.main()
