"""#216: the engine install refuses while a registered run has a live
managed session. A test register with fixture roots; no real install, no
network; the update runner's fakes on PATH where update is involved."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_fleet import _FleetFixture
from tests.test_handsoff_supervisor import BIN, ROOT

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_update as update  # noqa: E402


class InstallCheckTests(_FleetFixture):
    def running_session(self, root, role="reviewer", state="running"):
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            sid = "hs-" + "ab" * 16
            now = status["updated_at"]
            status["agent_sessions"] = {sid: {"session_id": sid, "role": role, "actor": f"codex-{role}",
                "adapter": "codex", "requested_model": "default", "reported_model": None,
                "resolution_source": "configured", "state": state, "started_at": now,
                "running_at": now if state == "running" else None, "ended_at": None, "exit_code": None, "tier": None,
                "phase_number": 5, "packet_id": None, "design_hash": None}}
            status["current_agent_sessions"] = {role: sid}
            lib.commit(root, cfg, status=status, event_kind="fixture_session", event_message="fixture")
        return sid

    def complete_session(self, root, sid):
        cfg = lib.load_config(root)
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            status["agent_sessions"][sid].update(state="completed", ended_at=status["updated_at"], exit_code=0)
            status["current_agent_sessions"] = {}
            lib.commit(root, cfg, status=status, event_kind="fixture_session", event_message="fixture")

    def events(self, root):
        return [json.loads(l) for l in (root / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def test_a_running_reviewer_blocks_the_install_naming_it(self):
        """[#216] acceptance: exit non-zero naming root, role and session; zero once completed."""
        root = self.project("live").resolve()
        quiet = self.project("quiet").resolve()
        fleet.register_project(root, self.registry)
        fleet.register_project(quiet, self.registry)
        sid = self.running_session(root)
        lines = []
        result = update.install_check(registry=self.registry, out=lines.append)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(lines[0], f"blocked: {root} reviewer {sid}")
        self.assertTrue(lines[-1].startswith("INSTALL_CHECK_BLOCKED: 1 live managed session"))
        self.assertEqual([s["root"] for s in result["blocked"]], [str(root)])
        self.assertNotIn("engine_install_forced", [e["kind"] for e in self.events(root)])
        # a launching session blocks too
        self.complete_session(root, sid)
        lines = []
        self.assertEqual(update.install_check(registry=self.registry, out=lines.append)["exit_code"], 0)
        self.assertEqual(lines, ["INSTALL_CHECK_OK"])
        self.running_session(root, role="implementer", state="launching")
        self.assertEqual(update.install_check(registry=self.registry, out=lambda _: None)["exit_code"], 1)

    def test_force_needs_by_and_ledgers_the_override_once_per_root(self):
        """[#216] acceptance: --force records engine_install_forced on that run's ledger."""
        root = self.project("live").resolve()
        fleet.register_project(root, self.registry)
        sid = self.running_session(root)
        lines = []
        result = update.install_check(force=True, registry=self.registry, out=lines.append)
        self.assertEqual(result["exit_code"], 2)
        self.assertTrue(lines[-1].startswith("INSTALL_CHECK_REFUSED: --force needs --by"))
        self.assertNotIn("engine_install_forced", [e["kind"] for e in self.events(root)])
        lines = []
        result = update.install_check(force=True, by="moncy", note="releasing v0.3.70 over a stuck reviewer",
                                      registry=self.registry, out=lines.append)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["forced"])
        self.assertEqual(lines[-1], "INSTALL_CHECK_FORCED")
        forced = [e for e in self.events(root) if e["kind"] == "engine_install_forced"]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["by"], "moncy")
        self.assertEqual(forced[0]["note"], "releasing v0.3.70 over a stuck reviewer")
        self.assertEqual(forced[0]["sessions"], [{"role": "reviewer", "session_id": sid}])
        self.assertEqual(subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "verify-log"], cwd=root,
                                        capture_output=True, text=True).returncode, 0)

    def test_a_root_that_is_gone_is_skipped_and_the_badge_reads_the_block(self):
        """[#216] acceptance: the Fleet badge data."""
        root = self.project("live").resolve()
        gone = self.project("gone").resolve()
        fleet.register_project(root, self.registry)
        fleet.register_project(gone, self.registry)
        shutil.rmtree(gone)
        self.assertIsNone(fleet.install_blocked(self.registry))
        self.assertIsNone(fleet.build_fleet(self.registry)["engine"]["install_blocked"])
        sid = self.running_session(root)
        blocked = fleet.build_fleet(self.registry)["engine"]["install_blocked"]
        self.assertEqual(blocked, {"count": 1, "sessions": [{"root": str(root), "role": "reviewer", "session_id": sid}]})

    def test_update_refuses_before_its_first_wheel_install(self):
        """[#216] handsoff update runs the check first and touches nothing when blocked."""
        root = self.project("live").resolve()
        fleet.register_project(root, self.registry)
        self.running_session(root)
        lines = []
        calls = []

        class Recorder(update.Runner):
            def run(self, argv, **kw):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, "", "")

        cfg = update.load_config(self.base / "missing.toml")
        outcome = update.update(cfg, out=lines.append, runner=Recorder(), registry=self.registry, fleet_wait=0)
        self.assertEqual(outcome["exit_code"], 1)
        self.assertEqual(outcome["failed"], ["install-check"])
        self.assertEqual(lines[-1], "UPDATE_FAILED: install blocked")
        self.assertEqual([c for c in calls if c[1:2] == ["release"] or c[1:2] == ["install"]], [])
        # dry run: reports the block and stops
        lines = []
        outcome = update.update(cfg, out=lines.append, runner=Recorder(dry_run=True), registry=self.registry, dry_run=True, fleet_wait=0)
        self.assertEqual(outcome["exit_code"], 1)
        self.assertIn("dry run: the install would be blocked; nothing else is checked", lines)
        # --only beakon (no wheel tool): the check is not in the way
        lines = []
        outcome = update.update(cfg, only=["beakon"], out=lines.append, runner=Recorder(), registry=self.registry, fleet_wait=0)
        self.assertNotIn("install-check", outcome["failed"])

    def test_the_cli_lists_install_check_and_the_docs_name_it(self):
        commands = subprocess.run([sys.executable, str(BIN / "handsoff_cli.py"), "commands"], capture_output=True, text=True).stdout
        self.assertIn("install-check", commands)
        self.assertIn("handsoff install-check", (ROOT / "playbook" / "landing.md").read_text())
        reference = (ROOT / "docs" / "REFERENCE.md").read_text()
        release = reference.split("### Cutting a release", 1)[1].split("\n## ", 1)[0]
        self.assertIn("handsoff install-check", release)
        self.assertNotIn("by hand", release.split("install-check", 1)[1][:400])


if __name__ == "__main__":
    unittest.main()
