"""#412: a reviewer cannot leave files in the checkout, and untracked scratch drift is named."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

CODEX = lambda name: "/usr/local/bin/codex" if name == "codex" else None  # noqa: E731


def _outside(root: Path, cwd: str) -> bool:
    path = Path(cwd).resolve()
    root = root.resolve()
    return path != root and root not in path.parents


class ReviewerWorkingDirectoryTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        # the drop-in check (build_launch_spec) needs the prompts in the copy
        if not (self.tmp / "prompts").exists():
            shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts", copy_function=shutil.copyfile)
        write_version_pin(self.tmp)

    def test_every_reviewer_scratch_is_outside_the_root(self):
        made = runtime._reviewer_scratch(self.tmp, "codex", "reviewer")
        self.addCleanup(shutil.rmtree, made, True)
        self.assertTrue(made.is_dir())
        self.assertTrue(_outside(self.tmp, str(made)))
        inspected = runtime._reviewer_scratch(self.tmp, "codex", "reviewer", create=False)
        self.assertTrue(_outside(self.tmp, str(inspected)))
        self.assertIsNone(runtime._reviewer_scratch(self.tmp, "codex", "implementer"))

    def test_a_reviewer_launch_runs_outside_the_root_with_read_access_to_it(self):
        for build in (lambda: runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=CODEX),
                      # a fallback is handed the original launch's full input (recover_launch's
                      # original_input = spec.stdin), so it is built the same way here
                      lambda: runtime.build_profile_launch_spec(
                          self.tmp, "reviewer", runtime.build_role_input(self.tmp, "reviewer", "Review it."),
                          {"adapter": "codex", "model": "gpt-test"}, which=CODEX)):
            spec = build()
            self.addCleanup(shutil.rmtree, spec.cwd, True)
            self.assertTrue(_outside(self.tmp, spec.cwd), spec.cwd)
            self.assertEqual(spec.env_overrides["TMPDIR"], spec.cwd)
            # read access unchanged: the packet names the project root and how to read it
            self.assertIn(f"git -C {self.tmp.resolve()}", spec.stdin)
            self.assertEqual(Path(spec.project_root).resolve(), self.tmp.resolve())


class UntrackedScratchDriftTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)
        shutil.copy(BIN.parent / ".gitignore", self.tmp / ".gitignore")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "product.py").write_text("x = 1\n")
        for args in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.com"],
                     ["git", "config", "user.name", "Scratch Test"], ["git", "add", "."],
                     ["git", "commit", "-qm", "fixture"]):
            subprocess.run(args, cwd=self.tmp, check=True, capture_output=True)
        self.init("Scratch fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def _drift(self):
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), records)
        errors = lib.compute_errors(self.read_status(), self.read_acceptance(), cfg, verifications=records,
                                    verification_problems=problems, root=self.tmp)
        message = next(error for error in errors if error.startswith("evidence drift: REQ-001"))
        return drift, message

    def test_drift_with_only_untracked_paths_is_named_scratch_with_the_removal_command(self):
        probe = self.tmp / "tests" / "_probe_reviewer_tmp.py"
        probe.parent.mkdir(exist_ok=True)
        probe.write_text("print('a reviewer probe')\n")
        drift, message = self._drift()
        self.assertEqual(drift["stale"], ["REQ-001"])
        self.assertTrue(drift["untracked_scratch"])
        self.assertEqual(drift["untracked_paths"], ["tests/_probe_reviewer_tmp.py"])
        self.assertEqual(drift["clean_command"], "git clean -f -- tests/_probe_reviewer_tmp.py")
        self.assertIn("changed paths: tests/_probe_reviewer_tmp.py", message)
        self.assertIn("untracked scratch, not tracked work", message)
        self.assertIn("`git clean -f -- tests/_probe_reviewer_tmp.py`", message)
        # the command really clears the drift
        subprocess.run(["git", "clean", "-f", "--", "tests/_probe_reviewer_tmp.py"], cwd=self.tmp,
                       check=True, capture_output=True)
        cfg = lib.load_config(self.tmp)
        records, _ = lib.load_verifications(self.tmp, cfg)
        self.assertEqual(lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), records)["stale"], [])

    def test_a_tracked_change_is_drift_as_today(self):
        (self.tmp / "src" / "product.py").write_text("x = 2\n")
        drift, message = self._drift()
        self.assertFalse(drift["untracked_scratch"])
        self.assertIsNone(drift["clean_command"])
        self.assertIn("changed paths: src/product.py", message)
        self.assertNotIn("untracked scratch", message)

    def test_a_new_untracked_file_under_a_source_path_is_still_drift(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("[checks]\n", '[checks]\nsource_paths = ["src"]\n', 1))
        (self.tmp / "src" / "new_module.py").write_text("y = 1\n")
        drift, message = self._drift()
        self.assertFalse(drift["untracked_scratch"])
        self.assertIn("changed paths: src/new_module.py", message)
        self.assertNotIn("untracked scratch", message)
        self.assertNotIn("git clean", message)
