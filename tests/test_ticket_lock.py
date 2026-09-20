"""#166: one ticket, one run. init refuses a ticket a live registered run
owns; the check and the registration are one locked transaction; --adopt
takes over only a dead or closed owner; close and completion release."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, normalize_fixture_config, run

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402

ROOT = BIN.parent


class TicketLockTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-lock-"))
        self.registry = self.base / "projects.json"
        os.environ["HANDSOFF_FLEET_REGISTRY"] = str(self.registry)
        self.first = self._project("first")

    def tearDown(self):
        os.environ.pop("HANDSOFF_FLEET_REGISTRY", None)
        shutil.rmtree(self.base, ignore_errors=True)
        super().tearDown()

    def _project(self, name):
        root = self.base / name
        root.mkdir()
        for file in ("handsoff.toml", "handsoff-runtime.json"):
            shutil.copy(ROOT / file, root / file)
        normalize_fixture_config(root / "handsoff.toml")
        for directory in ("schemas", "dashboard", "fleet", "templates", "bin", "rules"):
            shutil.copytree(ROOT / directory, root / directory)
        return root

    def _make_alive(self, root):
        """A running managed session with a fresh beacon naming a live pid
        (this test process): the Fleet liveness rules read it as alive."""
        session = lib.create_agent_session(root, role="implementer", actor="codex-implementer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        lib.transition_agent_session(root, session["session_id"], "running")
        lib.write_live_beacon(root, session_id=session["session_id"], role="implementer", state="running", pid=os.getpid())
        self.assertTrue(fleet.owner_alive(root))
        return session["session_id"]

    def _make_dead(self, root):
        """The beacon is gone and the session has ended: nothing vouches
        for the run any more."""
        beacon = lib.live_beacon_path(root)
        if beacon.exists():
            beacon.unlink()
        status = json.loads((root / "handsoff-status.json").read_text())
        for session_id, session in (status.get("agent_sessions") or {}).items():
            if session.get("state") in lib.AGENT_SESSION_LIVE_STATES:
                lib.transition_agent_session(root, session_id, "completed", exit_code=0)
        self.assertFalse(fleet.owner_alive(root))

    def _init(self, root, *items, adopt=False, feature="Feature"):
        args = ["init", feature]
        for item in items:
            args += ["--item", item]
        if adopt:
            args.append("--adopt")
        return run(args, cwd=root)

    def test_second_init_on_a_live_owned_ticket_is_refused_naming_the_owner(self):
        r = self._init(self.first, "#146 the capture search")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entry = next(e for e in fleet.load_registry(self.registry) if e["root"] == str(self.first.resolve()))
        self.assertEqual(entry["work_items"], [146])
        self.assertEqual(entry["state"], "open")
        events = [json.loads(l) for l in (self.first / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(next(e for e in events if e["kind"] == "initialized")["ticket_lock"], "evaluated")
        second = self._project("second")
        self._make_alive(self.first)
        r = self._init(second, "#146 the same ticket")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ticket lock: #146 is owned by", r.stdout)
        self.assertIn(str(self.first.resolve()), r.stdout)
        self.assertIn("phase 1", r.stdout)
        self.assertIn("last event", r.stdout)
        self.assertFalse((second / "handsoff-status.json").exists(), "a refusal leaves no run behind")
        self.assertNotIn(str(second.resolve()), {e["root"] for e in fleet.load_registry(self.registry)})
        # a different ticket, or the same root re-initialising, is fine
        r = self._init(second, "#147 another ticket")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_adopt_takes_over_a_dead_or_closed_owner_never_a_live_one(self):
        self._init(self.first, "#146 x")
        second = self._project("second")
        self._make_alive(self.first)
        r = self._init(second, "#146 x", adopt=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("owned by a LIVE run", r.stdout)
        self.assertIn("run-close it first", r.stdout)
        # dead owner (no beacon, no owned dashboard): adopted, with an event
        self._make_dead(self.first)
        r = self._init(second, "#146 x", adopt=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WORK_ITEM_ADOPTED: #146 from " + str(self.first.resolve()), r.stdout)
        events = [json.loads(l) for l in (second / "handsoff-events.jsonl").read_text().splitlines()]
        adopted = next(e for e in events if e["kind"] == "work_item_adopted")
        self.assertEqual(adopted["previous_root"], str(self.first.resolve()))
        self.assertEqual(adopted["numbers"], [146])
        # closed owners: plain init works, no adopt flag needed (the dead
        # first run is still open on disk, so it is closed here too)
        third = self._project("third")
        run(["run-close", "--by", "tester", "--reason", "superseded"], cwd=second)
        r = run(["run-close", "--by", "tester", "--reason", "abandoned"], cwd=self.first)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entry = next(e for e in fleet.load_registry(self.registry) if e["root"] == str(second.resolve()))
        self.assertEqual(entry["state"], "closed")
        r = self._init(third, "#146 x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_completion_releases_and_the_switch_off_skips_the_lock(self):
        self._init(self.first, "#146 x")
        status = json.loads((self.first / "handsoff-status.json").read_text())
        status["phase_number"], status["phase"], status["progress"], status["status"] = 8, lib.PHASES[8], 100, "complete"
        lib.commit(self.first, lib.load_config(self.first), status=status, event_kind="fixture_complete",
                   event_message="fixture", actor="test")
        self.assertEqual(fleet._run_state(self.first)["state"], "complete")
        self.assertEqual(fleet.ticket_owners({146}, path=self.registry), [])
        second = self._project("second")
        self.assertEqual(self._init(second, "#146 x").returncode, 0)
        # switch off on a third project: no lock, no registry write, event says disabled
        third = self._project("third")
        toml = third / "handsoff.toml"
        toml.write_text(toml.read_text() + "\n[features]\nticket_lock = false\n")
        self._make_alive(second)
        r = self._init(third, "#146 x")
        self.assertEqual(r.returncode, 0, r.stdout)
        events = [json.loads(l) for l in (third / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(next(e for e in events if e["kind"] == "initialized")["ticket_lock"], "disabled")
        self.assertNotIn(str(third.resolve()), {e["root"] for e in fleet.load_registry(self.registry)})

    def test_two_concurrent_inits_on_one_ticket_leave_exactly_one_owner(self):
        second = self._project("second")
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, str(BIN / "handsoff_supervisor.py"), "init", "Race",
                                   "--item", "#146 race"], cwd=root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for root in (self.first, second)]
        outs = [p.communicate(timeout=60) for p in procs]
        codes = [p.returncode for p in procs]
        self.assertEqual(sorted(codes), [0, 1], outs)
        winner = [root for root, code in zip((self.first, second), codes) if code == 0]
        self.assertEqual(len(winner), 1)
        owners = [e for e in fleet.load_registry(self.registry) if e.get("work_items") == [146] and e.get("state") == "open"]
        self.assertEqual([o["root"] for o in owners], [str(winner[0].resolve())])
        loser_out = next(out for out, code in zip(outs, codes) if code == 1)[0]
        self.assertIn("ticket lock: #146 is owned by", loser_out)

    def test_claimed_twice_is_shown_on_both_fleet_cards(self):
        self._init(self.first, "#146 x")
        second = self._project("second")
        toml = second / "handsoff.toml"
        toml.write_text(toml.read_text() + "\n[features]\nticket_lock = false\n")
        self._init(second, "#146 x")
        fleet.register_project(second, self.registry)
        twice = fleet.claimed_twice(self.registry)
        self.assertEqual(twice, {str(self.first.resolve()): [146], str(second.resolve()): [146]})
        snapshot = fleet.build_fleet(self.registry)
        marks = {p["root"]: p["claimed_twice"] for p in snapshot["projects"]}
        self.assertEqual(marks[str(self.first.resolve())], [146])
        self.assertEqual(marks[str(second.resolve())], [146])
        js = (ROOT / "fleet" / "app.js").read_text()
        self.assertIn("claimed-twice", js)
        self.assertIn(".claimed-twice", (ROOT / "fleet" / "styles.css").read_text())


if __name__ == "__main__":
    unittest.main()
