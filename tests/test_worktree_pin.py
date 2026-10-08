"""#416: an implementer's worktree carries the run's version pin and its
effective configuration. .handsoff-version (never in git) and the root's
handsoff.toml (a skip-worktree local edit included) are copied in before
launch and recorded as seeded, so the apply treats them as seeded content
and ledger verification runs inside the worktree. With no [[regressions]]
group configured a default group named full exists."""
from __future__ import annotations

import shutil
import subprocess
import sys

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.test_concurrent_implementers import _Process
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

#: the engine stays out of git, as a thin project's does, so the worktree is
#: not a runtime drop-in and must find the pin on its own
IGNORED = [".handsoff-version", "bin/", "handsoff-runtime.json", "schemas/", "dashboard/", "fleet/",
           "templates/", "rules/", "playbook/", "prompts/"]
LOCAL_EDIT = "\n# lane-local override, kept out of git by skip-worktree\n"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


class WorktreePinTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        self.pin = write_version_pin(self.tmp).read_text()
        self.init("Worktree pin #416")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "x.py").write_text("x = 1\n")
        (self.tmp / ".gitignore").write_text("\n".join(IGNORED) + "\n")
        git(self.tmp, "init", "-q")
        git(self.tmp, "add", "-A")
        git(self.tmp, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)

    def _local_override(self):
        toml = self.tmp / "handsoff.toml"
        git(self.tmp, "update-index", "--skip-worktree", "handsoff.toml")
        toml.write_text(toml.read_text() + LOCAL_EDIT)
        self.assertEqual(git(self.tmp, "status", "--porcelain", "--", "handsoff.toml"), "",
                         "git reports a skip-worktree edit; the fixture would not reproduce #416")
        return toml.read_text()

    def _launch(self, work):
        spec = runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=("src",))
        try:
            code = runtime.execute_launch(spec, popen_factory=lambda argv, **kw: _Process(kw["cwd"], work=work),
                                          beacon_interval=0.01)
            error = None
        except runtime.AgentLaunchError as exc:
            code, error = None, exc
        status = self.read_status()
        sid = lib.role_session_ids(status)["implementer"]
        return code, error, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)

    def test_the_worktree_has_the_pin_and_the_root_config_and_verification_finds_the_pin(self):
        config = self._local_override()
        seen = {}

        def work(cwd):
            seen["pin"] = (cwd / ".handsoff-version").read_text()
            seen["config"] = (cwd / "handsoff.toml").read_text()
            try:
                seen["identity"] = lib.runtime_identity(cwd)["compatibility"]
            except lib.HandsoffError as exc:
                seen["identity"] = str(exc)
            (cwd / "src" / "x.py").write_text("x = 2\n")
        code, error, session, failure = self._launch(work)
        self.assertIsNone(error, failure)
        self.assertEqual((seen["pin"], seen["config"]), (self.pin, config))
        self.assertEqual(seen["identity"], self.pin.strip(), "verification in the worktree found no pin")
        # the apply is a no-op for the seeded config: only the owned change came back
        self.assertEqual(session["apply"], {"state": "applied", "paths": ["src/x.py"]})
        self.assertEqual((self.tmp / "handsoff.toml").read_text(), config)
        self.assertEqual((self.tmp / "src" / "x.py").read_text(), "x = 2\n")

    def test_without_the_seeded_pin_verification_in_the_worktree_reports_it_missing(self):
        # the control: the same read fails exactly as #416 reported once the pin is gone
        seen = {}

        def work(cwd):
            (cwd / ".handsoff-version").unlink()
            try:
                lib.runtime_identity(cwd)
            except lib.HandsoffError as exc:
                seen["error"] = str(exc)
        self._launch(work)
        self.assertIn("version pin is missing", seen.get("error", ""))

    def test_an_implementer_edit_to_handsoff_toml_is_still_refused_as_outside_ownership(self):
        self._local_override()

        def work(cwd):
            toml = cwd / "handsoff.toml"
            toml.write_text(toml.read_text() + "\n# implementer edit\n")
        code, error, session, failure = self._launch(work)
        self.assertIsInstance(error, runtime.AgentLaunchError, (session, failure))
        self.assertEqual(failure["category"], "ownership_violation")
        self.assertIn("handsoff.toml", failure["changed_paths"])
        self.assertNotIn("# implementer edit", (self.tmp / "handsoff.toml").read_text())

    def test_the_seed_manifest_records_the_pin_and_the_config(self):
        self._local_override()
        captured = {}

        def work(cwd):
            import json
            sid = lib.role_session_ids(self.read_status())["implementer"]
            workspace = self.read_status()["agent_sessions"][sid]["workspace"]
            captured.update(json.loads(open(workspace["path"] + ".seed.json").read())["seeded"])
        self._launch(work)
        self.assertIn(".handsoff-version", captured)
        self.assertIn("handsoff.toml", captured)


class DefaultRegressionGroupTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.init("Default regression group #416")
        git(self.tmp, "init", "-q")
        git(self.tmp, "add", "-A")
        git(self.tmp, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")

    def test_regression_request_full_is_accepted_with_no_configured_group(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true", "echo ok"]', 1))
        cfg = lib.load_config(self.tmp)
        planned = run(["release-plan", "--version", "v1.0.0", "--by", "test-pilot"], cwd=self.tmp)
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        self.assertEqual(cfg["regressions"], [], "the default group must not enter the configured list")
        self.assertEqual(lib.regression_group(cfg, "full"),
                         {"name": "full", "commands": ["true", "echo ok"]})
        requested = run(["regression-request", "--group", "full", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(requested.returncode, 0, requested.stdout + requested.stderr)
        self.assertIn("REGRESSION_AWAITING_APPROVAL", requested.stdout)
        item = self.read_status()["regression_requests"][-1]
        self.assertEqual((item["group"], item["commands"]), ("full", ["true", "echo ok"]))
        # and the focused checks stay ungated: the default group is no gated group
        self.assertEqual(lib.configured_regression_commands(cfg), set())

    def test_a_configured_group_list_is_unchanged(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + '\n[[regressions]]\nname = "suite"\ncommands = ["python3 -m unittest -q"]\n')
        cfg = lib.load_config(self.tmp)
        self.assertEqual(lib.effective_regression_groups(cfg), cfg["regressions"])
        self.assertEqual([group["name"] for group in cfg["regressions"]], ["suite"])
        with self.assertRaises(lib.HandsoffError):
            lib.regression_group(cfg, "full")


if __name__ == "__main__":
    import unittest
    unittest.main()
