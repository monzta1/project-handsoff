"""#359: two managed implementers may run at once when their declared owned
paths are disjoint. The second runs in its own git worktree and only its
owned-path changes come back. #365: no pre-flight status call reads stdin."""
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from datetime import datetime, timedelta, timezone

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_agent_runtime as agent_runtime  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class LaunchPreflightStdinTests(unittest.TestCase):
    """#365: the no-prompt login status call must not inherit the launcher's stdin."""

    def test_login_status_never_reads_an_open_stdin_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "state").mkdir()
            adapter = root / "codex"
            # The status command reads stdin to EOF, as a CLI that prompts would.
            adapter.write_text('#!/bin/sh\nif [ "$2" = "status" ]; then cat >/dev/null; exit 0; fi\n'
                               'cat >/dev/null; echo OK\n')
            adapter.chmod(0o755)
            calls = []

            def runner(argv, **kwargs):
                calls.append((list(argv), kwargs))
                return subprocess.run(argv, **kwargs)

            read_end, write_end = os.pipe()  # write end stays open: EOF never arrives
            saved = os.dup(0)
            os.dup2(read_end, 0)
            try:
                started = time.monotonic()
                result = lib.launch_preflight(
                    root, adapter="codex", model="gpt-6-astra", executable=str(adapter),
                    argv=[str(adapter), "exec", "--model", "gpt-6-astra"], cwd=str(root),
                    runner=runner, timeout=5, state_dir=root / "state",
                )
                elapsed = time.monotonic() - started
            finally:
                os.dup2(saved, 0)
                for fd in (saved, read_end, write_end):
                    os.close(fd)
            self.assertEqual(result["state"], "ready", result)
            self.assertLess(elapsed, 5)
            login = [kwargs for argv, kwargs in calls if argv[1:] == ["login", "status"]]
            self.assertEqual(len(login), 1)
            self.assertIs(login[0].get("stdin"), subprocess.DEVNULL)


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _Process:
    """A fake adapter child: `work` runs in its working directory when it
    starts, and it stays running until `gate` is set."""
    pid = 4242

    def __init__(self, cwd, work=None, gate=None):
        if work:
            work(Path(cwd))
        self._gate = gate
        self.returncode = None if gate else 0
        self.stdin = _InputPipe()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        if self._gate is not None:
            self._gate.wait(30)
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


ORIGINAL = {"a.txt": "a original\n", "b.txt": "b original\n", "c.txt": "c original\n",
            "src/x.py": "x = 1\n"}


class ConcurrentImplementerTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Concurrent implementers")
        for name, text in ORIGINAL.items():
            (self.tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / name).write_text(text)
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp, check=True,
                                   capture_output=True, text=True).stdout.strip()
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)

    def _fresh(self):
        self.doCleanups()
        self.tearDown()
        self.setUp()

    def _hold(self, *owned):
        """A live implementer that stays launching: the session the next launch runs beside."""
        return lib.create_agent_session(self.tmp, role="implementer", actor="held-implementer",
                                        adapter="codex", requested_model="default",
                                        resolution_source="configured",
                                        owned_paths=list(owned) if owned else None)

    def _spec(self, *owned):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=tuple(owned) or None)

    def _launch(self, spec, work=None, gate=None, seen=None):
        def factory(argv, **kwargs):
            if seen is not None:
                seen.append(kwargs["cwd"])
            return _Process(kwargs["cwd"], work=work, gate=gate)
        try:
            return runtime.execute_launch(spec, popen_factory=factory, beacon_interval=0.01), None
        except runtime.AgentLaunchError as exc:
            return None, exc

    def _status_bytes(self):
        return ((self.tmp / "handsoff-status.json").read_bytes(),
                (self.tmp / "handsoff-events.jsonl").read_bytes())

    def _events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def _read(self, name):
        return (self.tmp / name).read_text()

    # REQ-001
    def test_two_disjoint_implementers_are_live_at_the_same_time(self):
        first = self._hold("a.txt")
        second = self._hold("src", "b.txt")
        status = self.read_status()
        live = {sid: s for sid, s in status["agent_sessions"].items()
                if s["role"] == "implementer" and s["state"] in lib.AGENT_SESSION_LIVE_STATES}
        self.assertEqual(set(live), {first["session_id"], second["session_id"]})
        self.assertEqual(live[first["session_id"]]["owned_paths"], ["a.txt"])
        self.assertEqual(live[second["session_id"]]["owned_paths"], ["src", "b.txt"])
        # each runs in its own worktree of HEAD, the first one included
        for session in (first, second):
            workspace = live[session["session_id"]]["workspace"]
            self.assertEqual(workspace["launch_commit"], self.head)
            self.assertTrue(workspace["path"].endswith(session["session_id"]))
        # owned_paths rode the very commit that created each session
        launched = {e["session_id"]: e for e in self._events("agent_session_launching")}
        self.assertEqual(launched[first["session_id"]]["owned_paths"], ["a.txt"])
        self.assertEqual(launched[second["session_id"]]["owned_paths"], ["src", "b.txt"])
        self.assertEqual(lib.validate_status_schema(status), [])

    def test_a_sole_implementer_without_owns_keeps_todays_session(self):
        session = self._hold()
        for field in ("owned_paths", "workspace", "apply"):
            self.assertNotIn(field, session)

    # REQ-002 and REQ-004
    def test_overlapping_ownership_is_refused_before_anything_is_written(self):
        live = self._hold("src", "a.txt")
        for owned, overlapping in ((["a.txt"], "a.txt"), (["src/x.py", "c.txt"], "src, src/x.py"),
                                   (["src", "b.txt"], "src"), (["src/new", "a.txt"], "a.txt, src, src/new")):
            before = self._status_bytes()
            with self.assertRaises(lib.HandsoffError) as caught:
                self._hold(*owned)
            message = str(caught.exception)
            self.assertIn(f"owned paths {overlapping} overlap", message)
            self.assertIn(live["session_id"], message)
            self.assertEqual(self._status_bytes(), before, owned)
        # a disjoint declaration beside the same live session is admitted
        self.assertEqual(self._hold("b.txt")["owned_paths"], ["b.txt"])

    def test_an_undeclared_launch_beside_a_live_implementer_is_refused_as_today(self):
        live = self._hold("a.txt")
        before = self._status_bytes()
        with self.assertRaisesRegex(lib.HandsoffError,
                                    f"role implementer already has live agent session {live['session_id']}"):
            self._hold()
        self.assertEqual(self._status_bytes(), before)
        # and a declared launch beside an undeclared live one is refused too
        self._fresh()
        unowned = self._hold()
        with self.assertRaisesRegex(lib.HandsoffError, f"{unowned['session_id']}.*no declared ownership"):
            self._hold("b.txt")

    def test_the_ownership_check_is_what_closes_the_race(self):
        source = " ".join(inspect.getsource(agent_runtime.implementer_admission).split())
        self.assertIn("# The single-live-implementer rule (one live session per role) existed # "
                      "to stop two implementers racing on the same files.", source)
        self.assertIn("implementer_admission(", inspect.getsource(agent_runtime.create_agent_session))
        self._hold("a.txt")
        # without the overlap test the same-file launch would be admitted: the race
        with mock.patch.object(agent_runtime, "owned_paths_overlap", return_value=False):
            self.assertEqual(self._hold("a.txt")["owned_paths"], ["a.txt"])

    # REQ-003
    def test_a_concurrent_implementer_writing_another_sessions_file_is_refused(self):
        self._hold("b.txt")
        seen = []

        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (cwd / "b.txt").write_text("b clobbered\n")
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work, seen=seen)
        self.assertIsNotNone(error)
        self.assertIn("b.txt", str(error))
        cwd = Path(seen[0]).resolve()
        self.assertNotEqual(cwd, self.tmp.resolve())
        self.assertNotIn(self.tmp.resolve(), cwd.parents)
        self.assertFalse(cwd.exists(), "the worktree is removed when the session ends")
        # nothing applied: neither B's file nor A's own
        self.assertEqual(self._read("b.txt"), ORIGINAL["b.txt"])
        self.assertEqual(self._read("a.txt"), ORIGINAL["a.txt"])
        status = self.read_status()
        sid = status["current_agent_sessions"]["implementer"]
        self.assertEqual(status["agent_sessions"][sid]["state"], "failed")
        self.assertEqual(status["agent_sessions"][sid]["apply"], {"state": "refused", "paths": ["b.txt"]})
        failure = status["agent_failures"][sid]
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertEqual(failure["changed_paths"], ["b.txt"])
        self.assertIn("b.txt", failure["reason"])

    def test_disjoint_concurrent_writers_both_apply_in_either_order(self):
        for order in (("A", "B"), ("B", "A")):
            with self.subTest(order=order):
                if order == ("B", "A"):
                    self._fresh()
                self._hold("c.txt")
                gates = {"A": threading.Event(), "B": threading.Event()}
                work = {
                    "A": lambda cwd: (cwd / "a.txt").write_text("a by A\n"),
                    "B": lambda cwd: ((cwd / "src" / "x.py").write_text("x = 2\n"),
                                      (cwd / "src" / "new.py").write_text("new = True\n")),
                }
                owns = {"A": ("a.txt",), "B": ("src",)}
                results, threads = {}, {}
                with mock.patch("sys.stdout", io.StringIO()):
                    for name in ("A", "B"):
                        threads[name] = threading.Thread(target=lambda n=name: results.__setitem__(
                            n, self._launch(self._spec(*owns[n]), work=work[n], gate=gates[n])))
                        threads[name].start()
                        self._wait_running(len(threads))
                    status = self.read_status()
                    running = [s for s in status["agent_sessions"].values() if s["state"] == "running"]
                    self.assertEqual(len(running), 2)
                    for name in order:
                        gates[name].set()
                        threads[name].join(30)
                        self.assertEqual(results[name], (0, None), name)
                self.assertEqual(self._read("a.txt"), "a by A\n")
                self.assertEqual(self._read("src/x.py"), "x = 2\n")
                self.assertEqual(self._read("src/new.py"), "new = True\n")
                applied = sorted((s["owned_paths"], s["apply"])
                                 for s in self.read_status()["agent_sessions"].values() if "apply" in s)
                self.assertEqual(applied, [
                    (["a.txt"], {"state": "applied", "paths": ["a.txt"]}),
                    (["src"], {"state": "applied", "paths": ["src/new.py", "src/x.py"]}),
                ])
                self.assertEqual(list(self.tmp.glob(".handsoff-live-hs-*.json")), [],
                                 "each session's own beacon is removed when it ends")

    def _wait_running(self, count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            sessions = self.read_status()["agent_sessions"].values()
            if sum(s["state"] == "running" for s in sessions) >= count:
                return
            time.sleep(0.02)
        self.fail(f"{count} sessions never reached running")

    def test_a_host_edit_to_an_owned_path_is_refused_not_overwritten(self):
        self._hold("c.txt")

        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (self.tmp / "a.txt").write_text("a by host\n")  # the host edits while it runs
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertIsNotNone(error)
        self.assertEqual(self._read("a.txt"), "a by host\n")
        status = self.read_status()
        sid = status["current_agent_sessions"]["implementer"]
        failure = status["agent_failures"][sid]
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertEqual(failure["changed_paths"], ["a.txt"])
        self.assertIn("host changed owned paths since launch: a.txt", failure["reason"])

    def test_owns_is_normalized_and_scopes_the_claude_edit_allowlist(self):
        claude_only = lambda name: "/usr/local/bin/claude" if name == "claude" else None
        with mock.patch.object(runtime.lib, "validate_runtime_integrity"), \
                mock.patch.object(runtime, "build_role_input", return_value="task"), \
                mock.patch.object(runtime, "applicable_design_review_packet", return_value=None):
            scoped = runtime.build_launch_spec(self.tmp, "implementer", "task", which=claude_only,
                                               owned_paths=["./a.txt", str(self.tmp / "src") + "/"])
            plain = runtime.build_launch_spec(self.tmp, "implementer", "task", which=claude_only)
        self.assertEqual(scoped.adapter, "claude")
        self.assertEqual(scoped.owned_paths, ("a.txt", "src"))
        tools = scoped.argv[scoped.argv.index("--allowedTools") + 1].split(",")
        for rule in ("Edit(a.txt)", "Edit(a.txt/**)", "Write(src)", "Write(src/**)"):
            self.assertIn(rule, tools)
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertEqual(scoped.argv[scoped.argv.index("--permission-mode") + 1], "default")
        plain_tools = plain.argv[plain.argv.index("--allowedTools") + 1].split(",")
        self.assertIn("Edit", plain_tools)
        self.assertEqual(plain.argv[plain.argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIsNone(plain.owned_paths)
        for bad in ("../outside", ".", "/etc/passwd"):
            with self.assertRaises(lib.HandsoffError, msg=bad):
                lib.normalize_owned_paths(self.tmp, [bad])

    # Host decisions on REQ-003's worktree
    def test_state_files_written_in_the_worktree_are_neither_attributed_nor_applied(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            # what a session running verify in its worktree leaves behind
            with (cwd / "handsoff-verifications.jsonl").open("a") as handle:
                handle.write('{"run_id": "from-the-worktree"}\n')
            (cwd / ".handsoff-verify-inflight").mkdir(exist_ok=True)
            (cwd / ".handsoff-verify-inflight" / "run.json").write_text("{}\n")
        before = (self.tmp / "handsoff-verifications.jsonl").read_bytes() \
            if (self.tmp / "handsoff-verifications.jsonl").exists() else None
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertEqual((code, error), (0, None))
        self.assertEqual(self._read("a.txt"), "a by agent\n")
        after = (self.tmp / "handsoff-verifications.jsonl").read_bytes() \
            if (self.tmp / "handsoff-verifications.jsonl").exists() else None
        self.assertEqual(after, before, "the worktree's ledger is never applied")
        self.assertFalse((self.tmp / ".handsoff-verify-inflight" / "run.json").exists())
        applied = [s["apply"] for s in self.read_status()["agent_sessions"].values() if "apply" in s]
        self.assertEqual(applied, [{"state": "applied", "paths": ["a.txt"]}])

    def test_the_worktree_is_seeded_with_uncommitted_content(self):
        (self.tmp / "a.txt").write_text("a dirty\n")       # owned, uncommitted
        (self.tmp / "b.txt").write_text("b dirty\n")       # not owned, uncommitted
        (self.tmp / "notes.txt").write_text("untracked\n")  # untracked
        seen = {}

        def work(cwd):
            seen.update({name: (cwd / name).read_text() for name in ("a.txt", "b.txt", "notes.txt")})
            (cwd / "a.txt").write_text(seen["a.txt"] + "more\n")
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertEqual(seen, {"a.txt": "a dirty\n", "b.txt": "b dirty\n", "notes.txt": "untracked\n"})
        self.assertEqual((code, error), (0, None), "an uncommitted owned path is not a host edit")
        self.assertEqual(self._read("a.txt"), "a dirty\nmore\n")
        self.assertEqual(self._read("b.txt"), "b dirty\n")

    def test_every_owns_launch_runs_in_its_own_worktree_even_alone(self):
        seen = []
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"),
                                       work=lambda cwd: (cwd / "a.txt").write_text("a alone\n"), seen=seen)
        self.assertEqual((code, error), (0, None))
        cwd = Path(seen[0]).resolve()
        self.assertNotEqual(cwd, self.tmp.resolve())
        self.assertNotIn(self.tmp.resolve(), cwd.parents)
        self.assertEqual(self._read("a.txt"), "a alone\n")
        session = next(s for s in self.read_status()["agent_sessions"].values() if s.get("owned_paths"))
        self.assertEqual(session["workspace"]["launch_commit"], self.head)
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["a.txt"]})

    def test_a_failover_replacement_keeps_ownership_and_worktree_mode(self):
        source = self._hold("a.txt")
        self._hold("b.txt")  # launched later: the source is no longer the role's current session
        lib.transition_agent_session(self.tmp, source["session_id"], "running")
        lib.transition_agent_session(self.tmp, source["session_id"], "failed", exit_code=1,
                                     failure=lib.classify_runtime_failure(exit_code=1,
                                                                          stderr_tail="rate limit exceeded"))
        cfg = lib.load_config(self.tmp)
        fallbacks = lib.fallback_profiles(cfg)
        fallbacks["implementer"] = [{"adapter": "claude", "model": "sonnet"}]
        lib.update_agent_settings(self.tmp, {"profiles": lib.agent_profiles(cfg), "fallbacks": fallbacks,
                                             "max_failovers_per_role": 2})
        record = lib.reserve_agent_replacement(
            self.tmp, from_session_id=source["session_id"], which=lambda name: f"/usr/local/bin/{name}",
            snapshotter=lambda root: {"head": "a" * 40, "branch": "main", "dirty": False,
                                      "status_sha256": "b" * 64})
        self.assertEqual(record["action"], "launch", record)
        replacement = self.read_status()["agent_sessions"][record["to_session_id"]]
        self.assertEqual(replacement["owned_paths"], ["a.txt"])
        self.assertEqual(replacement["workspace"]["launch_commit"], self.head)
        self.assertTrue(replacement["workspace"]["path"].endswith(record["to_session_id"]))
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])

    # REQ-009
    def test_the_earlier_sessions_transitions_are_not_refused_as_stale(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        self.assertEqual(self.read_status()["current_agent_sessions"]["implementer"], second["session_id"])
        lib.transition_agent_session(self.tmp, first["session_id"], "running")
        lib.transition_agent_session(self.tmp, first["session_id"], "completed", exit_code=0)
        lib.transition_agent_session(self.tmp, second["session_id"], "running")
        sessions = self.read_status()["agent_sessions"]
        self.assertEqual(sessions[first["session_id"]]["state"], "completed")
        self.assertEqual(sessions[second["session_id"]]["state"], "running")

    def test_run_close_stops_and_closes_every_live_implementer(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        for session in (first, second):
            lib.transition_agent_session(self.tmp, session["session_id"], "running")
        stopped = []
        result = lib.close_run(self.tmp, by="test-pilot", reason="closing both", cancel_active=True,
                               release_dashboard=False,
                               terminate_process=lambda root, session: stopped.append(session["session_id"])
                               or {"session_id": session["session_id"]})
        both = {first["session_id"], second["session_id"]}
        self.assertEqual(set(stopped), both)
        self.assertEqual(set(result["run_closed"]["session_ids"]), both)
        sessions = self.read_status()["agent_sessions"]
        self.assertEqual({sessions[sid]["state"] for sid in both}, {"cancelled"})

    def test_each_concurrent_process_is_proven_by_its_own_beacon(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        child = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        reaper = threading.Thread(target=child.wait, daemon=True)  # no zombie outlives the signal
        reaper.start()
        lib.write_live_beacon(self.tmp, session_id=first["session_id"], role="implementer",
                              state="running", pid=child.pid, per_session=True)
        # the second session's beat overwrites the shared project beacon
        lib.write_live_beacon(self.tmp, session_id=second["session_id"], role="implementer",
                              state="running", pid=os.getpid(), per_session=True)
        stopped = lib._terminate_owned_session_process(self.tmp, first, wait=5)
        self.assertEqual(stopped["session_id"], first["session_id"])
        self.assertEqual(stopped["signal"], "SIGTERM")
        reaper.join(5)
        self.assertIsNotNone(child.returncode)

    def test_recovery_assesses_every_live_implementer_and_keeps_ownership(self):
        lost, alive = self._hold("a.txt"), self._hold("b.txt")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4])
        for sid in (lost["session_id"], alive["session_id"]):
            status["agent_sessions"][sid].update(state="running", started_at=stale, running_at=stale,
                                                 phase_number=4)
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="test_setup", event_message="test_setup")
        # the current implementer is silent too, but its process is alive
        lib.write_live_beacon(self.tmp, session_id=alive["session_id"], role="implementer",
                              state="running", pid=os.getpid())
        cfg["agents"]["implementer"] = "codex"
        assessment = lib.recovery_assessment(self.read_status(), cfg, {}, [], root=self.tmp)
        self.assertEqual(assessment["state"], "worker_silent", assessment)
        self.assertEqual(assessment["lost_session_id"], lost["session_id"])
        launched = []
        with mock.patch.object(lib, "load_config", return_value=cfg):
            lib.recover_run(self.tmp, actor="watchdog",
                            launcher=lambda role, **kwargs: launched.append((role, kwargs)) or True)
        self.assertEqual(launched, [("implementer", {"owned_paths": ["a.txt"]})])
        sessions = self.read_status()["agent_sessions"]
        self.assertEqual(sessions[lost["session_id"]]["state"], "failed")
        self.assertEqual(sessions[alive["session_id"]]["state"], "running")

    def test_questions_from_either_session_reach_status_with_their_session_id(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        lib.raise_question(self.tmp, role="implementer", text="First asks?", session_id=first["session_id"])
        lib.raise_question(self.tmp, role="implementer", text="Second asks?", session_id=second["session_id"])
        questions = {q["session_id"]: q for q in self.read_status()["pending_questions"]}
        self.assertEqual(set(questions), {first["session_id"], second["session_id"]})
        self.assertEqual(questions[first["session_id"]]["text"], "First asks?")
        self.assertTrue(questions[first["session_id"]]["blocking"],
                        "the earlier live implementer's question holds the run like the current one's")
        self.assertTrue(questions[second["session_id"]]["blocking"])

    def test_status_and_the_dashboard_snapshot_list_both_live_sessions(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        both = {first["session_id"]: ["a.txt"], second["session_id"]: ["b.txt"]}
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual({s["session_id"]: s["owned_paths"] for s in payload["live_sessions"]}, both)
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual({s["session_id"]: s["owned_paths"] for s in snapshot["runtime"]["live_sessions"]},
                         both)
        close = next(item for item in snapshot["operator_actions"] if item["kind"] == "run_close")
        self.assertIn("Cancels the owned live session", close["consequence"])


if __name__ == "__main__":
    unittest.main()
