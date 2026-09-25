"""Lane A (#185, #190, #179): a lane cannot trip on the floor. A missing
pin degrades the engine badge instead of killing the page and init writes
the pin; the live smoke names what it lacks; a current review whose rules
set changed is reaffirmed, not refused twice; the engine version is read
from its file, never restated."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

ROOT = BIN.parent


class MissingPinTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Lane A: the floor")

    def test_a_pin_error_serves_a_full_snapshot_with_an_unknown_engine_and_the_reason(self):
        reason = f"Handsoff engine version pin is missing: {self.tmp / '.handsoff-version'}; run `handsoff init {self.tmp}`"
        with patch_engine("runtime_identity", side_effect=lib.HandsoffError(reason)):
            snapshot = dashboard.build_snapshot(self.tmp)
        self.assertTrue(snapshot["initialized"], snapshot.get("error"))
        self.assertEqual(snapshot["status"]["phase_number"], 1)
        self.assertEqual(snapshot["engine"]["version"], "unknown")
        self.assertEqual(snapshot["engine"]["source"], "unknown")
        self.assertEqual(snapshot["engine"]["reason"], reason)
        self.assertEqual(snapshot["audit"]["engine_error"], reason)
        self.assertEqual(snapshot["operations"]["engine"]["version"], "unknown")
        self.assertEqual(snapshot["operations"]["engine"]["reason"], reason)
        # the page renders ENGINE UNKNOWN for that version (#161) and shows the reason on the audit strip
        app = (ROOT / "dashboard" / "app.js").read_text()
        self.assertIn('badge.textContent = `ENGINE ${version && version !== "unknown" ? version : "UNKNOWN"}`;', app)
        self.assertIn("engine: ${snapshot.audit.engine_error}", app)
        # a healthy root carries no engine error
        healthy = dashboard.build_snapshot(self.tmp)
        self.assertIsNone(healthy["audit"]["engine_error"])
        self.assertNotEqual(healthy["engine"]["version"], "unknown")

    def test_the_api_answers_200_with_the_degraded_snapshot(self):
        import threading
        reason = "Handsoff engine version pin is missing: x"
        with patch_engine("runtime_identity", side_effect=lib.HandsoffError(reason)):
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                import urllib.request
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read())
                self.assertTrue(payload["initialized"])
                self.assertEqual(payload["engine"]["version"], "unknown")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class InitWritesThePinTests(HandsoffTestCase):
    def test_init_writes_the_pin_on_a_non_drop_in_root_without_one(self):
        # the fixture copies the engine in (a drop-in); make it not one
        (self.tmp / "bin" / "handsoff_lib.py").unlink()
        self.assertFalse(lib._looks_like_runtime_drop_in(self.tmp))
        self.assertFalse((self.tmp / ".handsoff-version").exists())
        r = run(["init", "Lane A: pin"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        version = json.loads((ROOT / "handsoff-runtime.json").read_text())["version"]
        major, minor = version.lstrip("v").split(".")[:2]
        self.assertIn(f"HANDSOFF_PIN_WRITTEN: {major}.{minor}.*", r.stdout)
        self.assertEqual((self.tmp / ".handsoff-version").read_text(), f"{major}.{minor}.*\n")
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        initialized = next(e for e in events if e["kind"] == "initialized")
        self.assertEqual(initialized["pin_written"], f"{major}.{minor}.*")
        # the identity now reads under the installed-engine path
        self.assertTrue(lib.version_satisfies(version, f"{major}.{minor}.*"))

    def test_a_drop_in_root_and_a_pinned_root_are_untouched(self):
        r = run(["init", "Lane A: drop-in"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("HANDSOFF_PIN_WRITTEN", r.stdout)
        self.assertFalse((self.tmp / ".handsoff-version").exists())
        (self.tmp / "bin" / "handsoff_lib.py").unlink()
        (self.tmp / ".handsoff-version").write_text("9.9.*\n")
        for name in ("handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl",
                     "handsoff-verifications.jsonl", ".handsoff-event-head.json"):
            (self.tmp / name).unlink(missing_ok=True)
        r = run(["init", "Lane A: pinned"], cwd=self.tmp)
        self.assertNotIn("HANDSOFF_PIN_WRITTEN", r.stdout)
        self.assertEqual((self.tmp / ".handsoff-version").read_text(), "9.9.*\n", "an existing pin is never rewritten")


class LiveSmokeNamesItsPreconditionTests(unittest.TestCase):
    def test_the_smoke_names_the_interpreter_that_lacks_websockets(self):
        shadow = Path(tempfile.mkdtemp(prefix="handsoff-no-websockets-"))
        (shadow / "websockets").mkdir()
        (shadow / "websockets" / "__init__.py").write_text("raise ImportError('shadowed for the test')\n")
        env = {**os.environ, "PYTHONPATH": str(shadow)}
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "live_offline_smoke.py")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"LIVE_OFFLINE_BLOCKED: websockets is not importable by {sys.executable}", r.stdout)
        self.assertIn("pip install websockets", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_the_live_extra_is_declared_and_installed_by_the_environment_step(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover
            import tomli as tomllib  # type: ignore
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["optional-dependencies"]["live"], ["websockets>=12"])
        self.assertIn('-m pip install "websockets>=12"', (ROOT / "INSTALL.md").read_text())
        smoke = (ROOT / "tests" / "live_offline_smoke.py").read_text()
        self.assertIn("LIVE_OFFLINE_BLOCKED: Chrome not found at", smoke)
        self.assertNotIn('print("LIVE_OFFLINE_BLOCKED")', smoke)


class ReaffirmAfterARulesChangeTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.init("Lane A: reaffirm")
        self.cfg = lib.load_config(self.tmp)

    def _record_review(self):
        # the same path tests/test_rules_binding.py takes to a recorded review
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(6, implemented_by="impl-1", reviewed_by="codex-reviewer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        return self.read_status()

    def _args(self):
        import argparse
        return argparse.Namespace(root=str(self.tmp), by="codex-reviewer", session=None, adopted_session=None,
                                  adopted_by=None, item=None, symptom_reproduced="not_applicable",
                                  tests_executed="yes", reaffirm=True)

    def test_a_current_review_whose_rules_set_changed_is_reaffirmed_and_the_ledger_names_the_change(self):
        status = self._record_review()
        self.assertIsNotNone(status.get("review"))
        recorded = status["review"]["rules_hash"]
        real_entries = lib.rules_set_entries
        bumped = {**real_entries(self.tmp), "engine:version": "v9.9.9"}
        with patch_engine("rules_set_entries", return_value=bumped):
            errors = lib.rules_binding_errors(self.tmp, self.cfg, status["review"], "review gate")
            self.assertEqual(len(errors), 1)
            self.assertIn("engine:version", errors[0])
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = supervisor.cmd_record_review(self._args())
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("INDEPENDENT_REVIEW_REAFFIRMED: rules set changed (engine:version)", out.getvalue())
            after = self.read_status()
            self.assertNotEqual(after["review"]["rules_hash"], recorded)
            self.assertEqual(after["review"]["rules_entries"]["engine:version"], "v9.9.9")
            self.assertEqual(lib.rules_binding_errors(self.tmp, self.cfg, after["review"], "review gate"), [])
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        reaffirmed = [e for e in events if e["kind"] == "review_reaffirmed"]
        self.assertEqual(len(reaffirmed), 1)
        self.assertEqual(reaffirmed[0]["rules_changed"], ["engine:version"])
        self.assertIn("after the rules set changed (engine:version)", reaffirmed[0]["message"])

    def test_a_current_review_with_an_unchanged_set_still_has_nothing_to_reaffirm(self):
        self._record_review()
        r = run(["record-review", "--by", "codex-reviewer", "--reaffirm", "--tests-executed", "yes",
                 "--symptom-reproduced", "not_applicable"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("review is current; nothing to reaffirm", r.stdout)


class TheVersionLivesInOneFileTests(unittest.TestCase):
    def test_no_test_restates_the_engine_version(self):
        import re
        version = json.loads((ROOT / "handsoff-runtime.json").read_text())["version"]
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = path.read_text()
            self.assertNotRegex(text, r'assertEqual\([^)]*"v0\.3\.\d+"\)',
                                f"{path.name} asserts a literal engine version; read handsoff-runtime.json instead")
        self.assertRegex(version, r"^v\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
