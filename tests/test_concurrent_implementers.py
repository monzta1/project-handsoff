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

from tests.engine_patch import patch_engine
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin
from tests.guards import guard

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


#: #399: a safety ceiling only, so a hang cannot block CI forever. Nothing
#: waits on it in a passing run: a gate holds until the test releases it,
#: and a launch is awaited by its own start signal.
CEILING_SECONDS = 300


class _Process:
    """A fake adapter child: `work` runs in its working directory when it
    starts, and it stays running until `gate` is set. `started` is set when
    the launcher waits on it: the session is running and the gate blocks."""
    pid = 4242

    def __init__(self, cwd, work=None, gate=None, started=None):
        if work:
            work(Path(cwd))
        self._gate = gate
        self._started = started
        self.returncode = None if gate else 0
        self.stdin = _InputPipe()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        if self._started is not None:
            self._started.set()
        if self._gate is not None and not self._gate.wait(CEILING_SECONDS):
            self.returncode = 1  # a gate nobody released fails the launch, never passes it
            return 1
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


class _SlowRunner:
    """#399: a loaded CI runner, simulated. Every sleep it is asked for
    advances its clock and takes only a sliver of real time, so a launch can
    start minutes late without the test taking minutes."""

    def __init__(self):
        self.now = 0.0
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            return self.now

    def sleep(self, seconds):
        with self._lock:
            self.now += seconds
        time.sleep(0.01)


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

    def _launch(self, spec, work=None, gate=None, seen=None, started=None, delay=None):
        def factory(argv, **kwargs):
            if seen is not None:
                seen.append(kwargs["cwd"])
            if delay is not None:
                delay()
            return _Process(kwargs["cwd"], work=work, gate=gate, started=started)
        try:
            return runtime.execute_launch(spec, popen_factory=factory, beacon_interval=0.01), None
        except runtime.AgentLaunchError as exc:
            return None, exc

    def _launch_thread(self, name, results, **kwargs):
        """A launch on its own thread whose outcome, whatever it is, lands in
        `results[name]`, so a waiter can report it rather than time out."""
        def target():
            try:
                results[name] = self._launch(**kwargs)
            except BaseException as exc:  # noqa: BLE001 - reported to the waiter, not swallowed
                results[name] = (None, exc)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def _wait_started(self, name, started, thread, results, clock=time):
        """#399: wait for the launch's own start signal while its thread is
        alive, never a fixed wall-clock budget: a thread that ends first
        fails the test at once with the launch's result or error."""
        begun = clock.monotonic()
        while not started.wait(0.02):
            if not thread.is_alive() and not started.is_set():
                code, error = results.get(name, (None, "no result"))
                self.fail(f"launch {name} ended before its process started: exit {code}, error {error!r}")
            if clock.monotonic() - begun > CEILING_SECONDS:
                self.fail(f"launch {name} did not start within the {CEILING_SECONDS} s safety ceiling")

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
        # #407: refused first because the live session declared ownership
        with self.assertRaisesRegex(lib.HandsoffError,
                                    f"implementer session {live['session_id']} declared owned paths"):
            self._hold()
        self.assertEqual(self._status_bytes(), before)
        # and a declared launch beside an undeclared live one is refused too
        self._fresh()
        unowned = self._hold()
        with self.assertRaisesRegex(lib.HandsoffError, f"{unowned['session_id']}.*no declared ownership"):
            self._hold("b.txt")

    @guard
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
        sid = lib.role_session_ids(status)["implementer"]  # #420: an ended session leaves the pointer
        self.assertEqual(status["agent_sessions"][sid]["state"], "failed")
        self.assertEqual(status["agent_sessions"][sid]["apply"], {"state": "refused", "paths": ["b.txt"]})
        failure = status["agent_failures"][sid]
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertEqual(failure["changed_paths"], ["b.txt"])
        self.assertIn("b.txt", failure["reason"])

    def test_disjoint_concurrent_writers_both_apply_in_either_order(self):
        # #399: the third case is a loaded runner: B's launch starts 45
        # simulated seconds late and every status read costs 5, past the
        # fixed 30 s deadline that failed on CI run 37401038853.
        for order, slow in ((("A", "B"), False), (("B", "A"), False), (("A", "B"), True)):
            with self.subTest(order=order, slow=slow):
                if order == ("B", "A") or slow:
                    self._fresh()
                clock = _SlowRunner() if slow else time
                self._concurrent_writers(order, clock, launch_delay={"B": 45} if slow else {},
                                         read_delay=5 if slow else 0)
                if slow:
                    self.assertGreater(clock.monotonic(), 30, "the simulated start outlasted the old deadline")

    def _concurrent_writers(self, order, clock, launch_delay, read_delay):
        self._hold("c.txt")
        gates = {"A": threading.Event(), "B": threading.Event()}
        started = {"A": threading.Event(), "B": threading.Event()}
        work = {
            "A": lambda cwd: (cwd / "a.txt").write_text("a by A\n"),
            "B": lambda cwd: ((cwd / "src" / "x.py").write_text("x = 2\n"),
                              (cwd / "src" / "new.py").write_text("new = True\n")),
        }
        owns = {"A": ("a.txt",), "B": ("src",)}
        results, threads = {}, {}

        def read_status():
            if read_delay:
                clock.sleep(read_delay)
            return self.read_status()
        try:
            with mock.patch("sys.stdout", io.StringIO()):
                for name in ("A", "B"):
                    delay = (lambda s=launch_delay[name]: clock.sleep(s)) if name in launch_delay else None
                    threads[name] = self._launch_thread(name, results, spec=self._spec(*owns[name]),
                                                        work=work[name], gate=gates[name],
                                                        started=started[name], delay=delay)
                    self._wait_started(name, started[name], threads[name], results, clock)
                running = [s for s in read_status()["agent_sessions"].values() if s["state"] == "running"]
                self.assertEqual(len(running), 2)
                # A has been blocked at its gate the whole time B was starting
                self.assertTrue(threads["A"].is_alive())
                self.assertNotIn("A", results)
                for name in order:
                    gates[name].set()
                    threads[name].join(CEILING_SECONDS)
                    self.assertEqual(results[name], (0, None), name)
        finally:
            for gate in gates.values():
                gate.set()
            for thread in threads.values():
                thread.join(CEILING_SECONDS)
        self.assertEqual(self._read("a.txt"), "a by A\n")
        self.assertEqual(self._read("src/x.py"), "x = 2\n")
        self.assertEqual(self._read("src/new.py"), "new = True\n")
        applied = sorted((s["owned_paths"], s["apply"])
                         for s in read_status()["agent_sessions"].values() if "apply" in s)
        self.assertEqual(applied, [
            (["a.txt"], {"state": "applied", "paths": ["a.txt"]}),
            (["src"], {"state": "applied", "paths": ["src/new.py", "src/x.py"]}),
        ])
        self.assertEqual(list(self.tmp.glob(".handsoff-live-hs-*.json")), [],
                         "each session's own beacon is removed when it ends")

    def test_a_launch_that_fails_before_starting_fails_the_wait_at_once(self):
        """#399: the waiter reports the launch's own error, not a timeout."""
        self._hold("c.txt")
        started, gate, results = threading.Event(), threading.Event(), {}

        def refuse():
            raise OSError("adapter executable vanished")  # the launch names the class, not the text
        begun = time.monotonic()
        try:
            with mock.patch("sys.stdout", io.StringIO()):
                thread = self._launch_thread("A", results, spec=self._spec("a.txt"), gate=gate,
                                             started=started, delay=refuse)
                with self.assertRaises(AssertionError) as caught:
                    self._wait_started("A", started, thread, results)
        finally:
            gate.set()
            thread.join(CEILING_SECONDS)
        self.assertLess(time.monotonic() - begun, 30, "failed at once, not at a deadline")
        self.assertFalse(started.is_set())
        self.assertIn("launch A ended before its process started", str(caught.exception))
        self.assertIn("codex process failed to start: OSError", str(caught.exception))

    def test_a_gate_that_is_never_released_fails_the_launch(self):
        """#399: a gate used to auto-complete after 30 s and report success."""
        gate = threading.Event()
        with mock.patch.object(threading.Event, "wait", return_value=False):
            process = _Process(self.tmp, gate=gate)
            self.assertEqual(process.wait(), 1)
        self.assertEqual(process.returncode, 1)

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
        sid = lib.role_session_ids(status)["implementer"]  # #420: an ended session leaves the pointer
        failure = status["agent_failures"][sid]
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertEqual(failure["changed_paths"], ["a.txt"])
        self.assertIn("host changed owned paths since launch: a.txt", failure["reason"])

    def _tree(self):
        """Every project file's bytes and mode, the git and Handsoff state aside."""
        tree = {}
        for path in sorted(self.tmp.rglob("*")):
            relative = path.relative_to(self.tmp).as_posix()
            if relative.split("/")[0] == ".git" or relative.startswith(("handsoff-", ".handsoff")):
                continue
            kind = "dir" if path.is_dir() and not path.is_symlink() else "file"
            tree[relative] = (kind, path.stat().st_mode,
                              path.read_bytes() if kind == "file" else None)
        return tree

    def test_a_file_replaced_by_a_directory_is_refused_and_nothing_is_applied(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (cwd / "b.txt").unlink()
            (cwd / "b.txt").mkdir()
            (cwd / "b.txt" / "inner").write_text("inner\n")
        before = self._tree()
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt", "b.txt"), work=work)
        self.assertIsNotNone(error)
        self.assertEqual(self._tree(), before, "a refused apply leaves the project byte-identical")
        status = self.read_status()
        failure = status["agent_failures"][lib.role_session_ids(status)["implementer"]]  # #420
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertIn("file into a directory or back: b.txt", failure["reason"])

    def _three_file_work(self, cwd):
        for name in ("a.txt", "b.txt", "c.txt"):
            (cwd / name).write_text(f"{name} by agent\n")
        (cwd / "src" / "deep").mkdir()
        (cwd / "src" / "deep" / "new.py").write_text("new\n")

    def test_a_disk_error_while_staging_touches_no_target(self):
        # review attempt 2: persistent ENOSPC on the second write; every
        # copy now goes to staging first, so no target is touched at all
        real = shutil.copy2
        calls = []

        def copy2(src, dst, **kwargs):
            if ".handsoff-apply-" in str(dst):
                calls.append(dst)
                if len(calls) >= 2:
                    raise OSError(28, "No space left on device")
            return real(src, dst, **kwargs)
        before = self._tree()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(agent_runtime.shutil, "copy2", copy2):
            code, error = self._launch(self._spec("a.txt", "b.txt", "c.txt", "src"), work=self._three_file_work)
        self.assertIsNotNone(error)
        self.assertIn("No space left", str(error))
        self.assertEqual(self._tree(), before, "a staging failure leaves the project untouched")
        self.assertEqual(list(self.tmp.glob(".handsoff-apply-*")), [], "staging removed")

    def test_a_rename_failure_mid_swap_rolls_every_target_back(self):
        real = os.rename
        swaps = []

        def rename(src, dst):
            if "/staged/" in str(src) and ".handsoff-apply-" in str(src):
                swaps.append(dst)
                if len(swaps) == 3:
                    raise OSError(5, "I/O error")
            return real(src, dst)
        before = self._tree()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(agent_runtime.os, "rename", rename):
            code, error = self._launch(self._spec("a.txt", "b.txt", "c.txt", "src"), work=self._three_file_work)
        self.assertIsNotNone(error)
        self.assertEqual(self._tree(), before, "every target is restored, created directories removed")
        self.assertEqual(list(self.tmp.glob(".handsoff-apply-*")), [], "staging removed after a clean rollback")

    def test_a_failed_rollback_keeps_the_originals_and_names_them(self):
        real = os.rename
        state = {"swaps": 0}

        def rename(src, dst):
            text = str(src)
            if "/staged/" in text and ".handsoff-apply-" in text:
                state["swaps"] += 1
                if state["swaps"] == 2:
                    raise OSError(5, "I/O error")
            if "/aside/" in text and ".handsoff-apply-" in text:
                raise OSError(5, "rollback I/O error")
            return real(src, dst)
        original_a = self._read("a.txt")
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(agent_runtime.os, "rename", rename):
            code, error = self._launch(self._spec("a.txt", "b.txt", "c.txt", "src"), work=self._three_file_work)
        self.assertIsNotNone(error)
        self.assertIn("apply rollback incomplete", str(error))
        kept = list(self.tmp.glob(".handsoff-apply-*/aside/*"))
        self.assertTrue(kept, "the moved-aside originals are kept, never deleted")
        self.assertIn(original_a, [p.read_text() for p in kept])
        self.assertIn(kept[0].parent.parent.name, str(error), "the error names where the originals are")

    def test_a_host_chmod_to_0600_is_refused_and_the_mode_kept(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (self.tmp / "a.txt").chmod(0o600)  # not the executable bit
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertIsNotNone(error)
        self.assertEqual((self.tmp / "a.txt").stat().st_mode & 0o7777, 0o600)
        self.assertNotEqual(self._read("a.txt"), "a by agent\n")

    def test_a_host_edit_during_seeding_refuses_the_launch_and_is_kept(self):
        # implementation review attempt 3: an edit between seeding and the
        # baseline snapshot became the baseline and was overwritten at apply
        real = agent_runtime._changed_since
        edited = []

        def changed_since(cwd, commit_sha, pathspecs=None):
            if not edited and Path(cwd).resolve() == self.tmp.resolve():
                (self.tmp / "a.txt").write_text("host edit during seeding\n")
                edited.append(True)
            return real(cwd, commit_sha, pathspecs)

        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
        with mock.patch("sys.stdout", io.StringIO()), \
                mock.patch.object(agent_runtime, "_changed_since", changed_since):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertTrue(edited, "the edit was injected while seeding")
        self.assertIsNotNone(error)
        self.assertIn("changed in the project while the implementer workspace was being seeded", str(error))
        self.assertEqual(self._read("a.txt"), "host edit during seeding\n", "the host edit survives")

    def test_an_untouched_0600_file_is_applied_and_keeps_its_mode(self):
        # the baseline is the project at launch, not git's 0644 checkout
        (self.tmp / "a.txt").chmod(0o600)

        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertIsNone(error, error)
        self.assertEqual(self._read("a.txt"), "a by agent\n")
        self.assertEqual((self.tmp / "a.txt").stat().st_mode & 0o7777, 0o600)

    def test_a_host_chmod_of_an_owned_file_is_refused_and_the_mode_kept(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (self.tmp / "a.txt").chmod(0o755)  # the host changes only the mode
        with mock.patch("sys.stdout", io.StringIO()):
            code, error = self._launch(self._spec("a.txt"), work=work)
        self.assertIsNotNone(error)
        self.assertEqual(self._read("a.txt"), ORIGINAL["a.txt"])
        self.assertEqual((self.tmp / "a.txt").stat().st_mode & 0o777, 0o755)
        status = self.read_status()
        failure = status["agent_failures"][lib.role_session_ids(status)["implementer"]]  # #420
        self.assertIn("host changed owned paths since launch: a.txt", failure["reason"])

    def _with_workspace(self, session_id):
        session = self.read_status()["agent_sessions"][session_id]
        lib.create_implementer_workspace(self.tmp, session)
        path = Path(session["workspace"]["path"])
        seed = Path(session["workspace"]["path"] + ".seed.json")
        self.assertTrue(path.is_dir() and seed.is_file())
        return path, seed

    def _worktrees(self):
        listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=self.tmp,
                                check=True, capture_output=True, text=True).stdout
        return [Path(line[len("worktree "):]).resolve() for line in listed.splitlines()
                if line.startswith("worktree ")]

    def test_run_close_removes_every_cancelled_implementers_worktree_and_seed(self):
        first, second = self._hold("a.txt"), self._hold("b.txt")
        resources = []
        for session in (first, second):
            lib.transition_agent_session(self.tmp, session["session_id"], "running")
            resources.append(self._with_workspace(session["session_id"]))
        # no launcher survives to run its own cleanup
        lib.close_run(self.tmp, by="test-pilot", reason="closing both", cancel_active=True,
                      release_dashboard=False,
                      terminate_process=lambda root, session: {"session_id": session["session_id"]})
        for path, seed in resources:
            self.assertFalse(path.exists(), path)
            self.assertFalse(seed.exists(), seed)
            self.assertNotIn(path.resolve(), self._worktrees())

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
        with patch_engine("load_config", return_value=cfg):
            lib.recover_run(self.tmp, actor="watchdog",
                            launcher=lambda role, **kwargs: launched.append((role, kwargs)) or True)
        self.assertEqual(launched, [("implementer", {"owned_paths": ["a.txt"]})])
        sessions = self.read_status()["agent_sessions"]
        self.assertEqual(sessions[lost["session_id"]]["state"], "failed")
        self.assertEqual(sessions[alive["session_id"]]["state"], "running")

    def test_recovery_removes_the_lost_implementers_worktree_and_seed(self):
        lost, alive = self._hold("a.txt"), self._hold("b.txt")
        lost_path, lost_seed = self._with_workspace(lost["session_id"])
        alive_path, alive_seed = self._with_workspace(alive["session_id"])
        stale = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4])
        for sid in (lost["session_id"], alive["session_id"]):
            status["agent_sessions"][sid].update(state="running", started_at=stale, running_at=stale,
                                                 phase_number=4)
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="test_setup", event_message="test_setup")
        lib.write_live_beacon(self.tmp, session_id=alive["session_id"], role="implementer",
                              state="running", pid=os.getpid())
        cfg["agents"]["implementer"] = "codex"
        with patch_engine("load_config", return_value=cfg):
            lib.recover_run(self.tmp, actor="watchdog", launcher=lambda role, **kwargs: True)
        self.assertEqual(self.read_status()["agent_sessions"][lost["session_id"]]["state"], "failed")
        self.assertFalse(lost_path.exists())
        self.assertFalse(lost_seed.exists())
        self.assertNotIn(lost_path.resolve(), self._worktrees())
        # the live session's worktree is its own launcher's to remove
        self.assertTrue(alive_path.is_dir() and alive_seed.is_file())

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
