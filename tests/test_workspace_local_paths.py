"""#429: [workspace] local_paths reach an owning implementer's worktree as
links and never come back; a failed owning session keeps its workspace the
way a stopped one does."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.test_implementer_scope import _Child

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_config as config  # noqa: E402
import handsoff_lib as lib  # noqa: E402

ORIGINAL = {"a.txt": "a original\n", "b.txt": "b original\n"}
VENV_PYTHON = "#!/bin/sh\necho venv python\n"
KB_INDEX = "# Knowledge base\n"


class _FailingChild(_Child):
    """A child that exits non-zero with an adapter's failure text."""

    def __init__(self, cwd, work=None, code=1, stderr=""):
        super().__init__(cwd, work=work, code=code)
        self.stderr = io.StringIO(stderr)


class LocalPathConfigTests(unittest.TestCase):
    def test_default_is_empty_and_paths_are_normalized(self):
        self.assertEqual(config.DEFAULT_CONFIG["workspace_local_paths"], [])
        self.assertEqual(config.validate_workspace_local_paths({}), [])
        self.assertEqual(config.validate_workspace_local_paths({"local_paths": [".venv/", "kb", "kb"]}),
                         [".venv", "kb"])

    def test_unsafe_paths_and_unknown_keys_are_refused(self):
        self.assertEqual(config.validate_workspace_local_paths({"local_paths": ["./kb", "kb//sub"]}),
                         ["kb", "kb/sub"])
        for bad in ("/abs/path", "../outside", "a/../b", ".git", ".handsoff-x", "", "/"):
            with self.assertRaises(lib.HandsoffError, msg=bad):
                config.validate_workspace_local_paths({"local_paths": [bad]})
        with self.assertRaisesRegex(lib.HandsoffError, "unknown keys"):
            config.validate_workspace_local_paths({"paths": []})
        with self.assertRaisesRegex(lib.HandsoffError, "list of strings"):
            config.validate_workspace_local_paths({"local_paths": ".venv"})


class LocalPathWorkspaceTests(HandsoffTestCase):
    LOCAL_PATHS = '[".venv", "kb", "missing-cache"]'

    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        with (self.tmp / "handsoff.toml").open("a", encoding="utf-8") as handle:
            handle.write(f"\n[workspace]\nlocal_paths = {self.LOCAL_PATHS}\n")
        self.init("Local paths")
        for name, text in ORIGINAL.items():
            (self.tmp / name).write_text(text)
        (self.tmp / ".gitignore").write_text(".venv/\nkb/\nmissing-cache/\n")
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        (self.tmp / ".venv" / "bin" / "python").write_text(VENV_PYTHON)
        (self.tmp / "kb").mkdir()
        (self.tmp / "kb" / "index.md").write_text(KB_INDEX)
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)

    def _spec(self, *owned):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=tuple(owned))

    def _launch(self, work, *, code=0, stderr="", owned=("a.txt",)):
        seen = []

        def factory(argv, **kwargs):
            seen.append(kwargs["cwd"])
            if code < 0 or not stderr:
                return _Child(kwargs["cwd"], work=work, code=code)
            return _FailingChild(kwargs["cwd"], work=work, code=code, stderr=stderr)
        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            try:
                result = runtime.execute_launch(self._spec(*owned), popen_factory=factory, beacon_interval=0.01)
                error = None
            except runtime.AgentLaunchError as exc:
                result, error = None, exc
        status = self.read_status()
        sid = next(sid for sid, s in status["agent_sessions"].items() if s.get("workspace"))
        return sid, Path(seen[0]), result, error, err.getvalue()

    def _session(self, sid):
        return self.read_status()["agent_sessions"][sid]

    def _root_local_paths_intact(self):
        self.assertTrue((self.tmp / ".venv").is_dir() and not (self.tmp / ".venv").is_symlink())
        self.assertEqual((self.tmp / ".venv" / "bin" / "python").read_text(), VENV_PYTHON)
        self.assertEqual((self.tmp / "kb" / "index.md").read_text(), KB_INDEX)

    def test_listed_venv_and_kb_are_visible_and_absent_from_the_change_set(self):
        observed = {}

        def work(cwd):
            observed["venv_link"] = (cwd / ".venv").is_symlink()
            observed["kb_link"] = (cwd / "kb").is_symlink()
            observed["python"] = (cwd / ".venv" / "bin" / "python").read_text()
            observed["kb"] = (cwd / "kb" / "index.md").read_text()
            (cwd / "a.txt").write_text("a by agent\n")
        sid, cwd, result, error, _err = self._launch(work)
        self.assertIsNone(error)
        self.assertEqual(result, 0)
        self.assertEqual(observed, {"venv_link": True, "kb_link": True, "python": VENV_PYTHON,
                                    "kb": KB_INDEX})
        session = self._session(sid)
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["a.txt"]})
        self.assertEqual((self.tmp / "a.txt").read_text(), "a by agent\n")
        self._root_local_paths_intact()
        self.assertFalse(cwd.exists(), "the worktree is removed; the project's own paths are not")

    def test_the_change_set_never_names_a_linked_path(self):
        session = lib.create_agent_session(self.tmp, role="implementer", actor="local-paths",
                                           adapter="codex", requested_model="default",
                                           resolution_source="configured", owned_paths=["a.txt"])
        session = self.read_status()["agent_sessions"][session["session_id"]]
        path = lib.create_implementer_workspace(self.tmp, session)
        try:
            self.assertTrue((path / ".venv").is_symlink())
            self.assertEqual((path / ".venv").resolve(), (self.tmp / ".venv").resolve())
            cfg = lib.load_config(self.tmp)
            self.assertEqual(lib.implementer_workspace_changes(cfg, session), [])
            (path / "a.txt").write_text("a by agent\n")
            self.assertEqual(lib.implementer_workspace_changes(cfg, session), ["a.txt"])
            report = lib.workspace_local_path_report(session)
            self.assertEqual(report["linked"], [".venv", "kb"])
            self.assertEqual(report["skipped"], [{"path": "missing-cache", "reason": "absent"}])
        finally:
            lib.remove_implementer_workspace(self.tmp, session)
        self.assertFalse(path.exists())
        self._root_local_paths_intact()

    def test_an_in_root_link_to_an_outside_environment_is_linked(self):
        """E1 live proof: a lane's .venv is usually a symlink to the main
        checkout's environment, outside the lane root. The entry lies inside
        the root, so it is linked; where it points is the project's choice."""
        import shutil
        import tempfile
        outside = Path(tempfile.mkdtemp(prefix="hs-outside-venv-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "bin").mkdir()
        (outside / "bin" / "python").write_text(VENV_PYTHON)
        shutil.rmtree(self.tmp / ".venv")
        (self.tmp / ".venv").symlink_to(outside, target_is_directory=True)
        # git sees a link as a file, so `.venv/` alone does not ignore it;
        # real projects list `.venv` as well (ToneCommand does)
        (self.tmp / ".gitignore").write_text(".venv/\n.venv\nkb/\nmissing-cache/\n")
        session = lib.create_agent_session(self.tmp, role="implementer", actor="local-paths",
                                           adapter="codex", requested_model="default",
                                           resolution_source="configured", owned_paths=["a.txt"])
        session = self.read_status()["agent_sessions"][session["session_id"]]
        path = lib.create_implementer_workspace(self.tmp, session)
        try:
            self.assertTrue((path / ".venv").is_symlink())
            self.assertEqual((path / ".venv" / "bin" / "python").read_text(), VENV_PYTHON)
            self.assertIn(".venv", lib.workspace_local_path_report(session)["linked"])
        finally:
            lib.remove_implementer_workspace(self.tmp, session)
        self.assertEqual((outside / "bin" / "python").read_text(), VENV_PYTHON)

    def test_an_entry_under_a_symlinked_parent_that_escapes_the_root_is_skipped(self):
        import shutil
        import tempfile
        outside = Path(tempfile.mkdtemp(prefix="hs-outside-parent-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "cache").mkdir()
        (self.tmp / "linked-dir").symlink_to(outside, target_is_directory=True)
        workspace = Path(tempfile.mkdtemp(prefix="hs-ws-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        import handsoff_agent_runtime
        linked, skipped = handsoff_agent_runtime._link_local_paths(self.tmp, workspace, ["linked-dir/cache"])
        self.assertEqual(linked, [])
        self.assertEqual(skipped, [{"path": "linked-dir/cache", "reason": "outside the project root"}])

    def test_an_absent_path_is_skipped_and_named_in_the_launch_output(self):
        sid, _cwd, result, error, err = self._launch(lambda cwd: (cwd / "a.txt").write_text("a by agent\n"))
        self.assertIsNone(error)
        self.assertIn("HANDSOFF_WORKSPACE_LOCAL_PATH_SKIPPED: missing-cache (absent)", err)
        self.assertNotIn("HANDSOFF_WORKSPACE_LOCAL_PATH_SKIPPED: .venv", err)

    def test_a_path_that_is_not_gitignored_is_not_linked(self):
        (self.tmp / ".gitignore").write_text("kb/\nmissing-cache/\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "venv"],
                       cwd=self.tmp, check=True, capture_output=True)
        _sid, _cwd, _result, error, err = self._launch(lambda cwd: (cwd / "a.txt").write_text("x\n"))
        self.assertIsNone(error)
        self.assertIn("HANDSOFF_WORKSPACE_LOCAL_PATH_SKIPPED: .venv (not gitignored)", err)

    def test_a_failed_session_keeps_its_workspace_and_it_applies(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a partial\n")
        sid, cwd, _result, error, _err = self._launch(
            work, code=1, stderr="Error: shared rollout token budget exhausted\n")
        self.assertIsNotNone(error)
        session = self._session(sid)
        self.assertEqual(session["state"], "failed")
        self.assertEqual(self.read_status()["agent_failures"][sid]["category"], "token_budget_exhaustion")
        self.assertEqual(session["workspace_disposition"], "pending")
        self.assertTrue(cwd.is_dir() and (cwd / ".venv").is_symlink())
        shown = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual([(item["session_id"], item["changed_paths"]) for item in shown["kept_workspaces"]],
                         [(sid, ["a.txt"])])
        applied = run(["implementer-apply", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("IMPLEMENTER_APPLIED: a.txt", applied.stdout)
        self.assertEqual((self.tmp / "a.txt").read_text(), "a partial\n")
        self.assertFalse(cwd.exists())
        self._root_local_paths_intact()

    def test_an_ownership_violation_keeps_its_workspace_and_it_discards(self):
        def work(cwd):
            (cwd / "a.txt").write_text("a by agent\n")
            (cwd / "b.txt").write_text("b clobbered\n")
        sid, cwd, _result, error, _err = self._launch(work)
        self.assertIsNotNone(error)
        self.assertEqual(self.read_status()["agent_failures"][sid]["category"], "ownership_violation")
        self.assertEqual(self._session(sid)["workspace_disposition"], "pending")
        self.assertTrue(cwd.is_dir())
        self.assertEqual((self.tmp / "b.txt").read_text(), ORIGINAL["b.txt"])
        discarded = run(["implementer-discard", "--session", sid, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(discarded.returncode, 0, discarded.stdout + discarded.stderr)
        self.assertFalse(cwd.exists())
        self.assertEqual(self._session(sid)["workspace_disposition"], "discarded")
        self._root_local_paths_intact()

    def test_a_stopped_session_is_unchanged(self):
        sid, cwd, _result, error, _err = self._launch(lambda cwd: (cwd / "a.txt").write_text("a\n"), code=-15)
        self.assertIsNotNone(error)
        session = self._session(sid)
        self.assertEqual((session["state"], session["workspace_disposition"]), ("failed", "pending"))
        self.assertTrue(cwd.is_dir())

    def test_kept_on_stop_covers_failed_but_not_applied_or_completed(self):
        workspace = {"path": "/tmp/x", "launch_commit": "0" * 40}
        for state in ("failed", "cancelled", "timed_out"):
            self.assertTrue(lib.workspace_kept_on_stop({"state": state, "workspace": workspace, "exit_code": 1}))
        self.assertFalse(lib.workspace_kept_on_stop({"state": "completed", "workspace": workspace}))
        self.assertFalse(lib.workspace_kept_on_stop({"state": "failed", "workspace": workspace,
                                                     "apply": {"state": "applied", "paths": []}}))
        self.assertFalse(lib.workspace_kept_on_stop({"state": "failed"}))


if __name__ == "__main__":
    unittest.main()
