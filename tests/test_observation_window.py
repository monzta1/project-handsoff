#!/usr/bin/env python3
"""P2.3: the post-deployment observation window.

With [checks].observation_minutes above 0, the first successful verify-live
opens a window in status (opened_at, closes_at) bound to the acceptance
hash, the config hash and that live record. Advance 8 refuses until a second
successful verify-live under the same bindings is recorded at or after
closes_at, naming the time left. A stale window never satisfies the gate; a
failure inside the window closes it as failed with a rollback note, and a
later success opens a fresh one. With 0 nothing changes.

The window is real time: the fixture sets a few seconds of window
(fractional minutes) and waits for it to close.
"""
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

#: three seconds
WINDOW_MINUTES = 0.05


class ObservationFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(tempfile.mkdtemp(prefix="handsoff-observation-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        # the live check passes while the flag file is absent
        self.flag = self.scratch / "broken"
        script = self.scratch / "live.sh"
        script.write_text(f'test ! -e "{self.flag}"\n')
        self.live = f"sh {script}"

    def configure(self, minutes):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace("live_commands = []", f"live_commands = {json.dumps([self.live])}", 1)
        if minutes is not None:
            text = text.replace("[checks]\n", f"[checks]\nobservation_minutes = {minutes}\n", 1)
        toml.write_text(text)

    def reach_phase_7(self, minutes=WINDOW_MINUTES):
        self.configure(minutes)
        self.init("P2.3 observation window")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)

    def live_run(self, expect=0):
        result = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def advance_8(self):
        return run(["advance", "8", "100", "--status", "complete"], cwd=self.tmp)

    def window(self):
        return self.read_status().get("observation")

    def wait_until_closed(self, window):
        closes = datetime.fromisoformat(window["closes_at"])
        delay = (closes - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay + 0.2)

    def events(self, kind):
        path = self.tmp / "handsoff-events.jsonl"
        return [e for e in map(json.loads, path.read_text().splitlines()) if e.get("kind") == kind]


class TestTheWindow(ObservationFixture):
    def test_the_first_success_opens_a_bound_window(self):
        self.reach_phase_7()
        payload = json.loads(self.live_run().stdout)
        window = self.window()
        self.assertEqual(window["state"], "open")
        self.assertEqual(window["live_id"], payload["run_id"])
        opened = datetime.fromisoformat(window["opened_at"])
        closes = datetime.fromisoformat(window["closes_at"])
        self.assertEqual(closes - opened, timedelta(minutes=WINDOW_MINUTES))
        cfg = lib.load_config(self.tmp)
        self.assertEqual(window["acceptance_hash"], lib.acceptance_hash(self.read_acceptance()["criteria"]))
        self.assertEqual(window["config_hash"], lib.config_hash(cfg))
        self.assertEqual(len(self.events("observation_window_opened")), 1)
        status_payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(status_payload["observation"]["closes_at"], window["closes_at"])

    def test_an_early_advance_is_refused_naming_the_time_left_and_a_later_run_clears_it(self):
        self.reach_phase_7()
        self.live_run()
        window = self.window()
        refused = self.advance_8()
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn(f"observation gate: the observation window closes at {window['closes_at']}", refused.stdout)
        self.assertRegex(refused.stdout, r"\(0 min [0-9] s left\)")
        self.assertIn("clears with: handsoff_supervisor.py verify-live --by ACTOR", refused.stdout)
        # a second success INSIDE the window does not confirm it
        self.live_run()
        self.assertEqual(self.window()["live_id"], window["live_id"])
        self.assertNotEqual(self.advance_8().returncode, 0)
        self.wait_until_closed(window)
        refused = self.advance_8()
        self.assertIn("observation window closed at", refused.stdout)
        self.live_run()
        landed = self.advance_8()
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)

    def test_a_changed_spec_makes_the_window_stale_and_a_fresh_one_is_required(self):
        self.reach_phase_7()
        self.live_run()
        first = self.window()
        # a policy change after the window opened: bindings no longer match
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(f"observation_minutes = {WINDOW_MINUTES}",
                                                 "observation_minutes = 0.06", 1))
        cfg = lib.load_config(self.tmp)
        self.assertFalse(lib.observation_window_current(first, self.read_acceptance()["criteria"], cfg))
        status = dict(self.read_status(), phase_number=8, phase=lib.PHASES[8], progress=100, status="complete")
        records, problems = lib.load_verifications(self.tmp, cfg)
        errors = lib.observation_gate_errors(status, self.read_acceptance(), cfg, records,
                                             datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(len(errors), 1)
        self.assertIn("the observation window is stale", errors[0])
        # the next success under the new bindings opens a fresh window, which
        # must close before it is confirmed
        import handsoff_supervisor as supervisor
        record = {"ok": True, "run_id": "vr-fresh", "at": datetime.now(timezone.utc).isoformat(),
                  "acceptance_hash": lib.acceptance_hash(self.read_acceptance()["criteria"]),
                  "config_hash": lib.config_hash(cfg)}
        event = supervisor._advance_observation_window(status, self.read_acceptance(), cfg, record)
        self.assertEqual(event["kind"], "observation_window_opened")
        fresh = status["observation"]
        self.assertEqual((fresh["live_id"], fresh["config_hash"], fresh["minutes"]),
                         ("vr-fresh", lib.config_hash(cfg), 0.06))
        self.assertNotEqual(fresh["config_hash"], first["config_hash"])
        early = lib.observation_gate_errors(status, self.read_acceptance(), cfg, records + [record],
                                            datetime.now(timezone.utc))
        self.assertIn("closes at", early[0])

    def test_an_acceptance_change_makes_the_window_stale_in_the_gate(self):
        self.reach_phase_7()
        self.live_run()
        window = self.window()
        self.wait_until_closed(window)
        self.live_run()
        cfg = lib.load_config(self.tmp)
        records, _ = lib.load_verifications(self.tmp, cfg)
        acceptance = self.read_acceptance()
        status = dict(self.read_status(), phase_number=8, phase=lib.PHASES[8], progress=100, status="complete")
        now = datetime.now(timezone.utc)
        self.assertEqual(lib.observation_gate_errors(status, acceptance, cfg, records, now), [])
        acceptance["criteria"][0]["requirement"] = "respecified after the window opened"
        errors = lib.observation_gate_errors(status, acceptance, cfg, records, now)
        self.assertEqual(len(errors), 1)
        self.assertIn("stale", errors[0])
        # the next success opens a fresh window bound to the new acceptance
        stale = dict(window, acceptance_hash="0" * 64)
        fresh_status = dict(self.read_status(), observation=stale)
        record = dict(records[-1], ok=True)
        import handsoff_supervisor as supervisor
        event = supervisor._advance_observation_window(fresh_status, self.read_acceptance(), cfg, record)
        self.assertEqual(event["kind"], "observation_window_opened")
        self.assertEqual(fresh_status["observation"]["live_id"], record["run_id"])

    def test_a_failure_inside_the_window_blocks_and_a_later_success_restarts_it(self):
        self.reach_phase_7()
        self.live_run()
        first = self.window()
        self.flag.write_text("down")
        self.live_run(expect=1)
        failed = self.window()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("roll back", failed["rollback"]["note"])
        self.assertTrue(failed["rollback"]["run_id"])
        self.assertEqual(len(self.events("observation_window_failed")), 1)
        self.wait_until_closed(first)
        refused = self.advance_8()
        self.assertIn("observation gate: the observation window failed", refused.stdout)
        # recovery: the next success opens a fresh window, which must itself close
        self.flag.unlink()
        self.live_run()
        fresh = self.window()
        self.assertEqual(fresh["state"], "open")
        self.assertNotEqual(fresh["live_id"], first["live_id"])
        self.assertGreater(fresh["opened_at"], first["opened_at"])
        self.assertIn("observation gate: the observation window closes at", self.advance_8().stdout)
        self.wait_until_closed(fresh)
        self.live_run()
        landed = self.advance_8()
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)


class TestDefaultOff(ObservationFixture):
    def test_zero_changes_nothing(self):
        self.reach_phase_7(minutes=None)
        self.assertEqual(lib.load_config(self.tmp)["observation_minutes"], 0)
        self.live_run()
        self.assertNotIn("observation", self.read_status())
        self.assertEqual(self.events("observation_window_opened"), [])
        landed = self.advance_8()
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)

    def test_the_setting_is_governance_hashed_only_when_set(self):
        self.configure(None)
        cfg = lib.load_config(self.tmp)
        baseline = lib.config_hash(cfg)
        self.assertEqual(lib.config_hash({**cfg, "observation_minutes": 0}), baseline)
        self.assertNotEqual(lib.config_hash({**cfg, "observation_minutes": 30}), baseline)

    def test_invalid_values_are_refused_at_load(self):
        for value in ("-1", '"ten"', "true", "20000"):
            with self.subTest(value=value):
                toml = self.tmp / "handsoff.toml"
                original = toml.read_text()
                toml.write_text(original.replace("[checks]\n", f"[checks]\nobservation_minutes = {value}\n", 1))
                with self.assertRaisesRegex(lib.HandsoffError, re.escape("checks.observation_minutes")):
                    lib.load_config(self.tmp)
                toml.write_text(original)


if __name__ == "__main__":
    unittest.main()
