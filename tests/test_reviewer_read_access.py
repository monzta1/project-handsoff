"""#343: the managed Claude reviewer can read the project it reviews.

The reviewer runs from an external scratch directory, by design, and Claude
Code refuses any read outside its working directory. With no `--add-dir` the
reviewer could not open one file of the project under review: every read came
back "Path is outside allowed working directories", the first design review was
packet-only, and it still consumed one of the two autonomous attempts. The
operator's workaround was to add the project to
`permissions.additionalDirectories` in their own `~/.claude/settings.json`,
which changes every Claude session on that machine.

Three criteria, and the third is the interesting one. REQ-008 exists because
the obvious implementation is wrong: `launch_preflight` takes no role and is
shared by every role and adapter, and architect and supervisor run with the
same empty allowlist the reviewer had. A probe that globally required reading a
project file would refuse launches #343 never asked to change, so the tests
below assert those two roles still pass, not merely that the reviewer does.
"""
import pathlib
import shutil
import sys
import tempfile
import unittest

from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def claude_only(name):
    return f"/usr/local/bin/{name}" if name == "claude" else None


class TheReviewerArgvCarriesReadAccess(unittest.TestCase):
    """REQ-003, the argv half."""

    def test_the_reviewer_is_given_the_project_root(self):
        argv = lib.claude_argv("/x/claude", "reviewer", list(lib.REVIEWER_READ_ONLY_TOOLS),
                               project_root="/some/project")
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1],
                         str(pathlib.Path("/some/project").resolve()))

    def test_the_path_is_absolute_so_the_scratch_cwd_cannot_change_its_meaning(self):
        """The reviewer's cwd is a temporary directory elsewhere, so a relative
        path would resolve against the scratch root and grant nothing."""
        argv = lib.claude_argv("/x/claude", "reviewer", [], project_root=".")
        given = argv[argv.index("--add-dir") + 1]
        self.assertTrue(pathlib.Path(given).is_absolute(), given)

    def test_no_write_capable_tool_is_in_the_reviewer_allowlist(self):
        """The actual claim of REQ-003, asserted against the write tools rather
        than by restating the allowlist, which would pass whatever it said."""
        allowed = set(lib.REVIEWER_READ_ONLY_TOOLS)
        self.assertEqual(allowed & set(lib.WRITE_CAPABLE_TOOLS), set())
        self.assertIn("Read", allowed, "a reviewer that cannot read is the bug being fixed")

    def test_the_allowlist_reaches_the_argv(self):
        argv = lib.claude_argv("/x/claude", "reviewer", list(lib.REVIEWER_READ_ONLY_TOOLS))
        tools = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertEqual(tools, list(lib.REVIEWER_READ_ONLY_TOOLS))
        for tool in lib.WRITE_CAPABLE_TOOLS:
            self.assertNotIn(tool, tools)

    def test_the_reviewer_permission_mode_is_unchanged(self):
        argv = lib.claude_argv("/x/claude", "reviewer", list(lib.REVIEWER_READ_ONLY_TOOLS))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default",
                         "read access must not have arrived as acceptEdits")

    def test_a_non_reviewer_role_argv_is_unchanged(self):
        """The second half of REQ-003. Passing a project root to another role
        must add nothing, because widening their launch is out of scope."""
        for role in ("supervisor", "architect", "implementer"):
            with self.subTest(role=role):
                without = lib.claude_argv("/x/claude", role, [])
                with_root = lib.claude_argv("/x/claude", role, [], project_root="/some/project")
                self.assertEqual(with_root, without)
                self.assertNotIn("--add-dir", with_root)


class BothLaunchBuildersGiveItTheSameAccess(unittest.TestCase):
    """REQ-003 names both builders because they had their own copy of the
    allowlist expression, and #347 had just shipped with one of its two sites
    threaded and the other not. The failover builder is the one that regresses
    silently: it runs only after a session has already failed."""

    def setUp(self):
        self.source = (BIN / "handsoff_agent.py").read_text(encoding="utf-8")

    def test_both_builders_pass_the_project_root(self):
        self.assertEqual(self.source.count("project_root=root"), 2,
                         "both lib.claude_argv call sites must pass the project root; "
                         "build_profile_launch_spec is the failover path")

    def test_neither_builder_keeps_its_own_copy_of_the_allowlist(self):
        self.assertEqual(self.source.count("_claude_allowed_tools(cfg, root"), 2)
        self.assertNotIn('[] if role in {"reviewer", "supervisor", "architect"}', self.source,
                         "an inline allowlist expression is how the two builders drifted")

    def test_every_claude_argv_call_in_the_launcher_passes_the_root(self):
        calls = self.source.count("lib.claude_argv(")
        self.assertEqual(calls, self.source.count("project_root=root"),
                         f"{calls} claude_argv calls but not all pass project_root, so a third "
                         "launch builder added later would silently lose read access")

    def test_the_shared_helper_gives_the_reviewer_the_read_only_set(self):
        self.assertEqual(runtime._claude_allowed_tools({}, pathlib.Path("."), "reviewer"),
                         list(lib.REVIEWER_READ_ONLY_TOOLS))

    def test_the_shared_helper_leaves_the_other_gate_roles_empty(self):
        for role in ("supervisor", "architect"):
            with self.subTest(role=role):
                self.assertEqual(runtime._claude_allowed_tools({}, pathlib.Path("."), role), [],
                                 "architect and supervisor must keep the empty allowlist they had")


class TheReadProbeAnswersHonestly(unittest.TestCase):
    """REQ-004, the probe itself."""

    def test_a_real_project_is_readable_and_names_what_it_read(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="handsoff-read-access-"))
        (root / "handsoff.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        verdict = lib.reviewer_read_access(root)
        self.assertTrue(verdict["readable"], verdict)
        self.assertIn("handsoff.toml", verdict["probed"])

    def test_a_missing_root_is_not_readable(self):
        verdict = lib.reviewer_read_access(pathlib.Path(tempfile.gettempdir()) / "handsoff-absent-xyz")
        self.assertFalse(verdict["readable"])
        self.assertIn("not a directory", verdict["reason"])

    def test_a_directory_with_no_project_in_it_is_not_readable(self):
        """Readable in the filesystem sense and useless in every other: a
        reviewer launched here has nothing to review, and reporting success
        would send it to burn an attempt discovering that."""
        root = pathlib.Path(tempfile.mkdtemp(prefix="handsoff-read-access-empty-"))
        verdict = lib.reviewer_read_access(root)
        self.assertFalse(verdict["readable"])
        self.assertIn("nothing to review", verdict["reason"])

    def test_a_listable_directory_whose_file_cannot_be_opened_is_refused(self):
        """The case an `os.access` check on the directory alone passes, and the
        one the reviewer then fails on its first real read."""
        root = pathlib.Path(tempfile.mkdtemp(prefix="handsoff-read-access-perm-"))
        blocked = root / "handsoff.toml"
        blocked.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        blocked.chmod(0o000)
        self.addCleanup(lambda: (blocked.chmod(0o644), shutil.rmtree(root, ignore_errors=True)))
        verdict = lib.reviewer_read_access(root)
        if verdict["readable"]:
            self.skipTest("this process can read a 0o000 file, so the case cannot be staged "
                          "(running as root, or on a filesystem without POSIX modes)")
        self.assertIn("cannot be opened", verdict["reason"])
        self.assertIn("handsoff.toml", verdict["reason"],
                      "the refusal must name the file, or an operator cannot act on it")


class TheRefusalCostsNothing(HandsoffTestCase):
    """REQ-004's real claim: refused BEFORE the session is reserved, leaving no
    review attempt, no session and no scratch directory. Mirrors the assertions
    `test_claude_reviewer_is_refused_before_scratch_or_session` already makes
    for the isolation refusal, because the cost of a wasted attempt is what the
    field report actually paid."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        text = text.replace('reviewer = "auto"', 'reviewer = "claude"', 1)
        # Compatibility must be APPROVED or the isolation contract refuses
        # first and this test proves nothing about the read probe.
        text = text.replace("compatibility_mode = false", "compatibility_mode = true")
        text = text.replace("compatibility_approved = false", "compatibility_approved = true")
        toml.write_text(text)

    def scratch_dirs(self):
        return set(pathlib.Path(tempfile.gettempdir()).glob("handsoff-reviewer-*"))

    def test_an_unreadable_project_refuses_before_anything_is_spent(self):
        before = self.scratch_dirs()
        with mock_read_access({"readable": False, "reason": "staged: the project cannot be read",
                               "probed": []}):
            with self.assertRaisesRegex(lib.HandsoffError, "before session reservation"):
                runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only)
        self.assertEqual(self.scratch_dirs(), before, "a scratch directory survived the refusal")
        self.assertFalse((self.tmp / "handsoff-status.json").exists(),
                         "a status file was written by a launch that was refused")

    def test_the_refusal_names_the_reason_the_probe_gave(self):
        with mock_read_access({"readable": False, "reason": "staged: the project cannot be read",
                               "probed": []}):
            with self.assertRaises(lib.HandsoffError) as caught:
                runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only)
        self.assertIn("staged: the project cannot be read", str(caught.exception))

    def test_a_readable_project_is_not_refused_by_the_probe(self):
        """The other half. A probe that refused everything would pass every
        test above while making the reviewer unlaunchable."""
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only)
        self.assertIn("--add-dir", spec.argv)
        self.assertIn(str(self.tmp.resolve()), spec.argv)
        self.assertNotEqual(spec.cwd, str(self.tmp.resolve()),
                            "the reviewer must still run from its own scratch directory")


class TheProbeAppliesOnlyToAClaudeReviewer(HandsoffTestCase):
    """REQ-008. `launch_preflight` takes no role and is shared by every role
    and adapter, so the obvious global implementation refuses launches #343
    never asked to change."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def test_the_architect_and_supervisor_still_launch_when_the_probe_would_refuse(self):
        for role in ("architect", "supervisor"):
            with self.subTest(role=role):
                with mock_read_access({"readable": False, "reason": "staged refusal", "probed": []}):
                    spec = runtime.build_launch_spec(
                        self.tmp, role, "Do it.",
                        which=lambda name: "/usr/local/bin/codex" if name == "codex" else None)
                self.assertEqual(spec.role, role)

    def test_a_codex_reviewer_is_not_subject_to_the_claude_read_probe(self):
        """Codex reviewers get a native OS sandbox with the project mounted
        read-only, so the Claude-specific read failure cannot arise there."""
        with mock_read_access({"readable": False, "reason": "staged refusal", "probed": []}):
            spec = runtime.build_launch_spec(
                self.tmp, "reviewer", "Review it.",
                which=lambda name: "/usr/local/bin/codex" if name == "codex" else None)
        self.assertEqual(spec.adapter, "codex")

    def test_the_gate_is_written_as_that_exact_pair(self):
        source = (BIN / "handsoff_agent.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('if role == "reviewer" and adapter == "claude":'), 2,
                         "both builders must gate the probe on the role AND the adapter; a "
                         "role-only gate refuses Codex reviewers and an adapter-only gate "
                         "refuses architect and supervisor")


def mock_read_access(verdict):
    from tests.engine_patch import patch_engine
    return patch_engine("reviewer_read_access", return_value=verdict)


if __name__ == "__main__":
    unittest.main()
