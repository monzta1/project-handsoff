"""#413: implementers stop at their scope; a stopped implementer's workspace waits for a disposition."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

ORIGINAL = {"a.txt": "a original\n", "b.txt": "b original\n"}


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _Child:
    """A fake implementer child: `work` runs in its worktree, then it ends with
    `code` (a negative code is a child killed by a signal, the supervisor's
    stop), or raises KeyboardInterrupt in the launcher (a cancel)."""
    pid = None

    def __init__(self, cwd, work=None, code=0, cancel=False):
        if work:
            work(Path(cwd))
        self.code, self.cancel = code, cancel
        self.returncode = None
        self.stdin = _InputPipe()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        if self.cancel:
            self.cancel = False  # the launcher's own stop then waits normally
            self.returncode = -2
            raise KeyboardInterrupt
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class PromptAndTaskTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        # the drop-in check (build_launch_spec) needs the prompts in the copy
        if not (self.tmp / "prompts").exists():
            shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts", copy_function=shutil.copyfile)

    def test_the_prompt_and_the_task_carry_the_scope_rule(self):
        prompt = (BIN.parent / "prompts" / "implementer.md").read_text(encoding="utf-8")
        self.assertIn("Failures in paths you do not own are expected", prompt)
        self.assertIn("`scope_exception`", prompt)
        self.assertIn("and stop", prompt)
        write_version_pin(self.tmp)
        self.init("Scope rule fixture")
        task = runtime.build_role_input(self.tmp, "implementer", "Build the fixture.")
        self.assertIn(runtime.IMPLEMENTER_SCOPE_RULE, task)
        self.assertIn("Failures in paths you do not own are expected", task)
        self.assertIn('"state": "scope_exception"', task)
        self.assertNotIn(runtime.IMPLEMENTER_SCOPE_RULE,
                         runtime.build_role_input(self.tmp, "reviewer", "Review the fixture."))
        line = lib.validate_progress_line({"criterion": "REQ-001", "state": "scope_exception",
                                           "test": "python3 -m unittest tests.test_other -v",
                                           "note": "tests/test_other.py: two failures I do not own"})
        self.assertIsNotNone(line)
        summary = lib.progress_summary([{"criterion": "REQ-001", "state": "scope_exception"}],
                                       {"criteria": [{"id": "REQ-001", "verification": "automated"}]})
        self.assertEqual(summary["scope_exception"], ["REQ-001"])
        self.assertEqual(lib.progress_summary([], {"criteria": []}),
                         {"done": [], "partial": [], "untouched": []})


class StoppedWorkspaceTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        self.init("Stopped implementer")
        for name, text in ORIGINAL.items():
            (self.tmp / name).write_text(text)
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)

    def _spec(self, *owned):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=tuple(owned))

    def _launch(self, work, code=0, cancel=False, owned=("a.txt",)):
        seen = []

        def factory(argv, **kwargs):
            seen.append(kwargs["cwd"])
            return _Child(kwargs["cwd"], work=work, code=code, cancel=cancel)
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            try:
                result = runtime.execute_launch(self._spec(*owned), popen_factory=factory, beacon_interval=0.01)
                error = None
            except runtime.AgentLaunchError as exc:
                result, error = None, exc
        status = self.read_status()
        sid = next(sid for sid, s in status["agent_sessions"].items() if s.get("workspace"))
        return sid, Path(seen[0]), result, error

    def _session(self, sid):
        return self.read_status()["agent_sessions"][sid]

    def _stopped(self, work=None, **kwargs):
        sid, cwd, _result, error = self._launch(
            work or (lambda cwd: (cwd / "a.txt").write_text("a by agent\n")), **kwargs)
        self.assertIsNotNone(error)
        return sid, cwd

    def test_a_stopped_session_keeps_its_workspace_and_status_lists_its_changes(self):
        sid, cwd = self._stopped(code=-15)
        session = self._session(sid)
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["exit_code"], -15)
        self.assertEqual(session["workspace_disposition"], "pending")
        self.assertTrue(cwd.is_dir(), "the stopped session's worktree is kept")
        self.assertEqual((cwd / "a.txt").read_text(), "a by agent\n")
        self.assertEqual((self.tmp / "a.txt").read_text(), ORIGINAL["a.txt"], "nothing applied yet")
        shown = run(["status"], cwd=self.tmp)
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        kept = json.loads(shown.stdout)["kept_workspaces"]
        self.assertEqual([item["session_id"] for item in kept], [sid])
        self.assertEqual(kept[0]["changed_paths"], ["a.txt"])
        self.assertIn(f"implementer-apply --session {sid}", kept[0]["apply"])
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        kept_events = [e for e in events if e.get("kind") == "implementer_workspace_kept"]
        self.assertEqual(kept_events[-1]["changed_paths"], ["a.txt"])

    def test_a_cancelled_session_keeps_its_workspace(self):
        sid, cwd = self._stopped(cancel=True)
        self.assertEqual(self._session(sid)["state"], "cancelled")
        self.assertEqual(self._session(sid)["workspace_disposition"], "pending")
        self.assertTrue(cwd.is_dir())

    def test_implementer_apply_applies_the_owned_paths(self):
        sid, cwd = self._stopped(code=-15)
        applied = run(["implementer-apply", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("IMPLEMENTER_APPLIED: a.txt", applied.stdout)
        self.assertEqual((self.tmp / "a.txt").read_text(), "a by agent\n")
        session = self._session(sid)
        self.assertEqual(session["workspace_disposition"], "applied")
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["a.txt"]})
        self.assertFalse(cwd.exists(), "an applied workspace is removed")
        again = run(["implementer-apply", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(again.returncode, 1)
        self.assertIn("no kept workspace awaiting a disposition (applied)", again.stdout)

    def test_implementer_apply_refuses_an_unowned_path(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (cwd / "b.txt").write_text("b clobbered\n")
        sid, cwd = self._stopped(work=work, code=-15)
        refused = run(["implementer-apply", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("IMPLEMENTER_APPLY_REFUSED: outside_ownership: b.txt", refused.stdout)
        self.assertEqual((self.tmp / "a.txt").read_text(), ORIGINAL["a.txt"])
        self.assertEqual((self.tmp / "b.txt").read_text(), ORIGINAL["b.txt"])
        self.assertEqual(self._session(sid)["workspace_disposition"], "pending")
        self.assertTrue(cwd.is_dir())

    def test_implementer_apply_refuses_a_host_edited_path(self):
        sid, cwd = self._stopped(code=-15)
        (self.tmp / "a.txt").write_text("a edited by the host\n")
        refused = run(["implementer-apply", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("IMPLEMENTER_APPLY_REFUSED: host_edit: a.txt", refused.stdout)
        self.assertEqual((self.tmp / "a.txt").read_text(), "a edited by the host\n")
        self.assertEqual(self._session(sid)["workspace_disposition"], "pending")

    def test_implementer_apply_refuses_a_live_session(self):
        live = lib.create_agent_session(self.tmp, role="implementer", actor="live-implementer",
                                        adapter="codex", requested_model="default",
                                        resolution_source="configured", owned_paths=["a.txt"])
        for command in ("implementer-apply", "implementer-discard"):
            refused = run([command, "--session", live["session_id"], "--by", "test-supervisor"], cwd=self.tmp)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("is still live; stop it first", refused.stdout)

    def test_implementer_discard_removes_the_workspace(self):
        sid, cwd = self._stopped(code=-15)
        discarded = run(["implementer-discard", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(discarded.returncode, 0, discarded.stdout + discarded.stderr)
        self.assertIn(f"IMPLEMENTER_DISCARDED: {sid}", discarded.stdout)
        self.assertFalse(cwd.exists())
        self.assertEqual((self.tmp / "a.txt").read_text(), ORIGINAL["a.txt"])
        self.assertEqual(self._session(sid)["workspace_disposition"], "discarded")
        shown = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(shown["kept_workspaces"], [])

    def test_a_normal_exit_still_auto_applies(self):
        sid, cwd, result, error = self._launch(lambda cwd: (cwd / "a.txt").write_text("a by agent\n"), code=0)
        self.assertIsNone(error)
        self.assertEqual(result, 0)
        self.assertEqual((self.tmp / "a.txt").read_text(), "a by agent\n")
        session = self._session(sid)
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["a.txt"]})
        self.assertNotIn("workspace_disposition", session)
        self.assertFalse(cwd.exists(), "the worktree is removed when the session ends")
