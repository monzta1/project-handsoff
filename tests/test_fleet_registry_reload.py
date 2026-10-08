"""#434 (REQ-001): Fleet shows what is true without a restart. The running
Fleet server recomputes its rediscovered runs whenever the registry file's
signature changes, so a root another process unregistered or removed drops
off the next /api/fleet; and a Phase 7 run whose deployment approval is not
required gets a plain next step, on its next_action and its Fleet card,
while a run that requires approval keeps today's text and decision."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from tests.fixture_state import force_status, write_version_pin
from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, normalize_fixture_config, run

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def make_run(feature: str) -> Path:
    """A second project laid out as HandsoffTestCase lays out its own."""
    root = Path(tempfile.mkdtemp(prefix="handsoff-test-"))
    for name in ("handsoff.toml", "handsoff-runtime.json"):
        shutil.copyfile(ROOT / name, root / name)
    normalize_fixture_config(root / "handsoff.toml")
    for directory in ("schemas", "dashboard", "fleet", "templates", "bin", "rules", "playbook", "prompts"):
        shutil.copytree(ROOT / directory, root / directory, copy_function=shutil.copyfile)
    write_version_pin(root)
    result = run(["init", feature], cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


class RegistryReloadTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init("Registry reload")
        self.other = make_run("Second run")
        self.addCleanup(shutil.rmtree, self.other, ignore_errors=True)
        for root in (self.tmp, self.other):
            fleet.register_project(root, self.registry)
            fleet.note_registry_state(root, None, "open", path=self.registry)
        self.server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals_interval=3600, issues_interval=3600)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.stopping = True
        self.server.signals_thread.stop_event.set()
        self.server.issues_thread.stop_event.set()
        self.server.shutdown()
        self.server.server_close()

    def fleet_payload(self) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        try:
            connection.request("GET", "/api/fleet")
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    def roots(self, payload):
        return ({item["root"] for item in payload["projects"]}, {item["root"] for item in payload["rediscovered"]})

    def test_a_root_unregistered_by_another_process_leaves_the_next_fleet_build(self):
        mine, other = str(self.tmp.resolve()), str(self.other.resolve())
        projects, rediscovered = self.roots(self.fleet_payload())
        self.assertEqual((projects, rediscovered), ({mine, other}, {mine, other}))
        script = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import handsoff_fleet as f; "
                  "print(f.unregister_project(Path(sys.argv[2]), Path(sys.argv[3])))")
        result = subprocess.run([sys.executable, "-c", script, str(BIN), other, str(self.registry)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "True"), result.stderr)
        payload = self.fleet_payload()
        self.assertEqual(self.roots(payload), ({mine}, {mine}), "no restart: the next build omits it everywhere")
        self.assertIsNone(payload["rediscovery_error"])

    def test_a_removed_root_leaves_rediscovered_on_the_next_fleet_build(self):
        mine, other = str(self.tmp.resolve()), str(self.other.resolve())
        self.assertEqual(self.roots(self.fleet_payload())[1], {mine, other})
        shutil.rmtree(self.other)
        projects, rediscovered = self.roots(self.fleet_payload())
        self.assertEqual(rediscovered, {mine}, "the registry write that marked it missing recomputed rediscovered")
        self.assertIn(other, projects, "the card stays ORPHANED for one pass (#207)")

    def test_an_unchanged_registry_reuses_the_rediscovered_runs(self):
        self.fleet_payload()
        calls = []
        original = fleet.rediscover
        fleet.rediscover = lambda path=None: calls.append(path) or original(path)
        try:
            self.fleet_payload()
            self.fleet_payload()
        finally:
            fleet.rediscover = original
        self.assertEqual(calls, [], "no registry change, no recompute")
        self.assertEqual(fleet.registry_signature(self.registry), self.server.rediscovery_signature)


class NoApprovalPhase7Tests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init("Phase 7 approval")

    def phase7(self, *, required: bool):
        toml = self.tmp / "handsoff.toml"
        if not required:
            toml.write_text(toml.read_text()
                            .replace("deployment_requires_explicit_approval = true",
                                     "deployment_requires_explicit_approval = false")
                            .replace('profile = "safe"', 'profile = "dogfood"'))
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status.update(phase_number=7, phase=lib.PHASES[7], status="in_progress", deployment_approved=None,
                      next_action=lib.NEXT_ACTION_DEFAULTS[7])
        force_status(self.tmp, cfg, status)
        return cfg

    def card(self):
        entry = {"root": str(self.tmp.resolve()), "registered_at": "2026-10-08T00:00:00+00:00"}
        return fleet.project_view(entry, None, facts=fleet.project_facts(self.tmp), owner=None)

    def test_not_required_the_card_names_the_plain_next_step_and_offers_no_authorization(self):
        cfg = self.phase7(required=False)
        self.assertFalse(lib.adaptive_deployment_approval_required(self.read_status(), cfg))
        card = self.card()
        self.assertEqual(card["next_action"], fleet.NO_APPROVAL_PHASE7_LINE)
        self.assertIn("not required", card["next_action"])
        self.assertIn("verify-live", card["next_action"])
        self.assertNotIn("deployment_approve", [item.get("kind") for item in card["decisions"]])
        self.assertNotEqual(card["state"], "waiting")

    def test_not_required_the_run_own_plain_next_action_is_kept_word_for_word(self):
        cfg = self.phase7(required=False)
        status = self.read_status()
        status["next_action"] = "Deployment approval is not required for this project: land it, then run verify-live."
        self.assertEqual(fleet.card_next_action(status, cfg), status["next_action"])

    def test_required_the_card_keeps_today_text_and_the_authorize_deployment_decision(self):
        cfg = self.phase7(required=True)
        self.assertTrue(lib.adaptive_deployment_approval_required(self.read_status(), cfg))
        card = self.card()
        self.assertEqual(card["next_action"], lib.NEXT_ACTION_DEFAULTS[7])
        self.assertIn("deployment_approve", [item.get("kind") for item in card["decisions"]])
        self.assertEqual(card["state"], "waiting")


if __name__ == "__main__":
    import unittest
    unittest.main()
