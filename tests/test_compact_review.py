"""#290/REQ-003: a compact review is a constraint, not a polite request.

On 2026-09-23 a reviewer was told in prose to review only the final delta,
run no tests, and emit an immediate verdict. It consumed 70,796 tokens
grepping unrelated repository history and failed without a verdict. A
replacement did the same.

Prose cannot bound exploration. A working directory can: a reviewer given
nothing but the scoped slices has nothing else to find and no suite to run.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests.engine_patch import patch_engine
from tests.fixture_state import write_version_pin
from tests.guards import guard
from tests.test_handsoff_supervisor import HandsoffTestCase

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = BIN.parent
sys.path.insert(0, str(BIN))

import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_schema as schema  # noqa: E402


class TheScopeIsValidated(unittest.TestCase):

    def test_a_well_formed_scope_is_accepted_and_ordered(self):
        scope = lib.validate_compact_review_scope([
            {"path": "b.py", "start": 10, "end": 20},
            {"path": "a.py", "start": 5, "end": 6},
            {"path": "a.py", "start": 1, "end": 2},
        ])
        self.assertEqual([(item["path"], item["start"]) for item in scope],
                         [("a.py", 1), ("a.py", 5), ("b.py", 10)])

    def test_an_empty_scope_is_refused(self):
        for value in ([], None, "bin/handsoff_lib.py"):
            with self.assertRaises(lib.HandsoffError):
                lib.validate_compact_review_scope(value)

    def test_an_absolute_path_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "repository-relative"):
            lib.validate_compact_review_scope([{"path": "/etc/passwd", "start": 1, "end": 2}])

    def test_a_path_that_escapes_the_repository_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "escape the repository"):
            lib.validate_compact_review_scope([{"path": "../secrets", "start": 1, "end": 2}])

    def test_an_inverted_range_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "precedes its start"):
            lib.validate_compact_review_scope([{"path": "a.py", "start": 20, "end": 10}])

    def test_a_range_too_large_to_be_compact_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "at most"):
            lib.validate_compact_review_scope(
                [{"path": "a.py", "start": 1, "end": lib.MAX_COMPACT_SCOPE_LINES + 1}])

    def test_too_many_ranges_is_a_full_review_not_a_compact_one(self):
        entries = [{"path": f"f{i}.py", "start": 1, "end": 2}
                   for i in range(lib.MAX_COMPACT_SCOPE_ENTRIES + 1)]
        with self.assertRaisesRegex(lib.HandsoffError, "full review"):
            lib.validate_compact_review_scope(entries)

    def test_a_malformed_entry_is_refused(self):
        for entry in ({"path": "a.py", "start": 1}, {"path": "a.py", "start": 1, "end": 2, "x": 1},
                      {"path": "", "start": 1, "end": 2}, {"path": "a.py", "start": 0, "end": 2},
                      {"path": "a.py", "start": True, "end": 2}):
            with self.assertRaises(lib.HandsoffError):
                lib.validate_compact_review_scope([entry])


class TheScopeIsMaterialized(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.scratch = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def _materialize(self, entries):
        scope = lib.validate_compact_review_scope(entries)
        return lib.materialize_compact_scope(ROOT, scope, self.scratch)

    def test_only_the_scoped_slices_exist_in_the_scratch(self):
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666}])
        names = sorted(p.name for p in self.scratch.iterdir())
        self.assertEqual(names, ["SCOPE.json", "bin__handsoff_lib.py.1653-1666.slice"])
        self.assertEqual(len(manifest["entries"]), 1)

    def test_the_repository_is_not_reachable_from_the_scratch(self):
        """The whole point: there is nothing else to discover."""
        self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 5}])
        for forbidden in ("tests", "bin", ".git", "pyproject.toml"):
            self.assertFalse((self.scratch / forbidden).exists(),
                             f"{forbidden} is reachable from a compact review scratch")

    def test_no_test_suite_is_materialized_so_none_can_run(self):
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 5}])
        self.assertFalse(manifest["tests_executable"])
        self.assertFalse(manifest["repository_visible"])
        self.assertEqual(list(self.scratch.glob("**/test_*.py")), [])

    def test_a_slice_carries_its_real_coordinates(self):
        """A finding must still be able to cite file and line even though
        the whole file is absent."""
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666}])
        text = (self.scratch / manifest["entries"][0]["slice"]).read_text()
        self.assertTrue(text.startswith("# bin/handsoff_lib.py lines 1653-1666"))

    @guard
    def test_the_slice_content_matches_the_source_range(self):
        source = (ROOT / "bin" / "handsoff_lib.py").read_text().splitlines()
        manifest = self._materialize([{"path": "bin/handsoff_lib.py", "start": 10, "end": 14}])
        body = (self.scratch / manifest["entries"][0]["slice"]).read_text().splitlines()[1:]
        self.assertEqual(body, source[9:14])

    def test_the_manifest_totals_the_lines_actually_written(self):
        manifest = self._materialize([
            {"path": "bin/handsoff_lib.py", "start": 1, "end": 10},
            {"path": "bin/handsoff_manifest.py", "start": 1, "end": 5},
        ])
        self.assertEqual(manifest["total_lines"], 15)
        self.assertEqual(sum(item["lines"] for item in manifest["entries"]), 15)

    def test_the_manifest_is_written_for_the_reviewer_to_read(self):
        self._materialize([{"path": "bin/handsoff_lib.py", "start": 1, "end": 3}])
        manifest = json.loads((self.scratch / "SCOPE.json").read_text())
        self.assertEqual(manifest["schema"], "handsoff.compact_review_scope")

    def test_a_missing_file_is_refused_rather_than_silently_empty(self):
        with self.assertRaisesRegex(lib.HandsoffError, "cannot read"):
            self._materialize([{"path": "bin/not_a_file.py", "start": 1, "end": 2}])

    def test_a_range_past_the_end_of_the_file_yields_what_exists(self):
        manifest = self._materialize([{"path": "bin/handsoff_manifest.py", "start": 1, "end": 400}])
        self.assertGreater(manifest["total_lines"], 0)
        self.assertLess(manifest["total_lines"], 400)


class TheBoundIsStructuralNotProse(unittest.TestCase):
    """The distinction REQ-003 turns on."""

    def test_the_packet_shrinks_to_the_scope(self):
        """The failing review carried a 27,954-byte packet. A compact scope
        of the same question is a small fraction of that."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope = lib.validate_compact_review_scope([
            {"path": "bin/handsoff_lib.py", "start": 1653, "end": 1666},
            {"path": "bin/handsoff_lib.py", "start": 1718, "end": 1745},
        ])
        manifest = lib.materialize_compact_scope(ROOT, scope, Path(directory.name))
        written = sum((Path(directory.name) / item["slice"]).stat().st_size
                      for item in manifest["entries"])
        self.assertLess(written, 27_954,
                        "a compact scope that is not smaller than the full packet is not compact")

    def test_the_limits_are_named_constants_a_reviewer_cannot_talk_past(self):
        self.assertIsInstance(lib.MAX_COMPACT_SCOPE_LINES, int)
        self.assertIsInstance(lib.MAX_COMPACT_SCOPE_ENTRIES, int)
        self.assertLessEqual(lib.MAX_COMPACT_SCOPE_ENTRIES * lib.MAX_COMPACT_SCOPE_LINES, 10_000,
                             "the worst-case compact review must still be compact")


def claude_only(name):
    return "/usr/local/bin/claude" if name == "claude" else None


def session_fields_accepted(*names):
    """The session schema belongs to another lane of this run (#383/#388 add
    `compact` and `quarantined_result`). Where it has not landed yet, accept
    the named optional fields so this lane's behaviour is tested on its own;
    once it has, this patches nothing."""
    stack = contextlib.ExitStack()
    missing = set(names) - schema.AGENT_SESSION_FIELDS
    if missing:
        original = schema.validate_status_schema
        markers = tuple(f"'.{name}" for name in missing) + tuple(f".{name} " for name in missing)

        def accepting(status):
            return [error for error in original(status)
                    if not any(marker in error for marker in markers)
                    and not ("unsupported fields" in error
                             and set(error.rsplit(": ", 1)[-1].split(", ")) <= missing)]
        stack.enter_context(patch_engine("validate_status_schema", side_effect=accepting))
    return stack


class TheFlagIsParsed(unittest.TestCase):
    """#388: `--compact-scope PATH:START-END`, repeatable."""

    def test_repeated_ranges_parse_into_a_validated_scope(self):
        self.assertEqual(runtime.parse_compact_scope(["b.py:3-9", "a.py:1-2"]),
                         [{"path": "a.py", "start": 1, "end": 2}, {"path": "b.py", "start": 3, "end": 9}])

    def test_no_flag_is_no_scope(self):
        self.assertIsNone(runtime.parse_compact_scope(None))

    def test_a_malformed_range_is_refused(self):
        for value in ("a.py", "a.py:3", "a.py:x-4", ":1-2", "/etc/passwd:1-2"):
            with self.subTest(value=value), self.assertRaises(lib.HandsoffError):
                runtime.parse_compact_scope([value])


class ACompactLaunchSeesOnlyItsSlices(HandsoffTestCase):
    """#388: argv, scratch contents, the absolute-path deny, on both builders."""

    SCOPE = [{"path": "src/mod.py", "start": 2, "end": 4}]

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        text = text.replace('reviewer = "auto"', 'reviewer = "claude"', 1)
        text = text.replace("compatibility_mode = false", "compatibility_mode = true")
        text = text.replace("compatibility_approved = false", "compatibility_approved = true")
        toml.write_text(text)
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "mod.py").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    def _spec(self, **kwargs):
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "Review the slice.", which=claude_only,
                                         skip_preflight=True, compact_scope=self.SCOPE, **kwargs)
        self.addCleanup(shutil.rmtree, spec.cwd, True)
        return spec

    def _failover_spec(self):
        spec = runtime.build_profile_launch_spec(
            self.tmp, "reviewer", "Review the slice.", {"adapter": "claude", "model": "default"},
            which=claude_only, skip_preflight=True, compact_scope=tuple(self.SCOPE))
        self.addCleanup(shutil.rmtree, spec.cwd, True)
        return spec

    def _flag(self, argv, name):
        return argv[argv.index(name) + 1]

    def _assert_compact(self, spec):
        argv = list(spec.argv)
        root = str(self.tmp.resolve())
        self.assertNotIn("--add-dir", argv, "a compact reviewer must not be given the project")
        self.assertNotIn(root, argv)
        self.assertNotIn("Bash", self._flag(argv, "--allowedTools").split(","))
        denied = self._flag(argv, "--disallowedTools").split(",")
        self.assertEqual(denied, ["Bash", f"Read(/{root}/**)", f"Grep(/{root}/**)", f"Glob(/{root}/**)"])
        scratch = Path(spec.cwd)
        self.assertNotEqual(scratch.resolve(), self.tmp.resolve())
        self.assertNotIn(self.tmp.resolve(), scratch.resolve().parents)
        self.assertEqual(sorted(p.name for p in scratch.iterdir()),
                         ["SCOPE.json", "src__mod.py.2-4.slice"])
        self.assertEqual(spec.compact_scope, tuple(self.SCOPE))

    def test_the_primary_builder_launches_from_the_slices_only(self):
        spec = self._spec()
        self._assert_compact(spec)
        self.assertEqual((Path(spec.cwd) / "src__mod.py.2-4.slice").read_text(),
                         "# src/mod.py lines 2-4\nline 2\nline 3\nline 4\n")

    def test_the_failover_builder_gets_the_same(self):
        self._assert_compact(self._failover_spec())

    def test_the_stdin_names_only_the_slices(self):
        stdin = self._spec().stdin
        self.assertIn("- src/mod.py lines 2-4", stdin)
        self.assertIn("tests_executed no", stdin)
        self.assertNotIn(str(self.tmp.resolve()), stdin, "the reduced packet must not point at the project")
        self.assertNotIn("# Project root", stdin)
        self.assertLess(len(stdin), len(runtime.build_role_input(self.tmp, "reviewer", "Review the slice.")))

    def test_the_read_probe_is_not_run(self):
        """A compact reviewer reads nothing of the project, so an unreadable
        project is no reason to refuse it."""
        with patch_engine("reviewer_read_access", side_effect=AssertionError("probe ran")):
            self._spec()

    def test_the_turn_bound_is_told_the_scope_is_compact(self):
        with patch_engine("turn_bound_refusal", return_value=None) as bound:
            self._spec()
        self.assertTrue(bound.call_args.kwargs["compact_scope"])

    def test_a_full_reviewer_launch_is_unchanged(self):
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only,
                                         skip_preflight=True)
        self.addCleanup(shutil.rmtree, spec.cwd, True)
        self.assertIn("--add-dir", spec.argv)
        self.assertNotIn("--disallowedTools", spec.argv)
        self.assertIsNone(spec.compact_scope)

    def test_only_a_reviewer_takes_a_compact_scope(self):
        with self.assertRaisesRegex(lib.HandsoffError, "reviewer launches only"):
            runtime.build_launch_spec(self.tmp, "implementer", "Do it.", skip_preflight=True,
                                      which=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
                                      compact_scope=self.SCOPE)


class _Pipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _Process:
    pid = 4243

    def __init__(self, stdout):
        self.returncode = 0
        self.stdin = _Pipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


class ACompactSessionRecordsNoTests(HandsoffTestCase):
    """#388: the session is marked compact and its verdict carries
    tests_executed no, whatever the reviewer wrote."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Compact session")

    def test_the_session_is_compact_and_its_result_says_no_tests_ran(self):
        verdict = {"kind": "implementation", "decision": "changes_requested", "summary": "one gap",
                   "findings": ["other: a gap"], "structural_blocker": False,
                   "symptom_reproduced": "not_applicable", "tests_executed": "yes"}
        spec = runtime.LaunchSpec("reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded prompt", token_budget=40_000,
                                  project_root=str(self.tmp.resolve()),
                                  compact_scope=({"path": "a.py", "start": 1, "end": 2},))
        factory = mock.Mock(side_effect=lambda *a, **k: _Process(
            "HANDSOFF_REVIEW_RESULT: " + json.dumps(verdict) + "\n"))
        error = None
        with session_fields_accepted("compact", "quarantined_result"), mock.patch("sys.stdout", io.StringIO()):
            try:
                runtime.execute_launch(spec, popen_factory=factory, beacon_interval=0.01)
            except runtime.AgentLaunchError as exc:
                error = exc  # the dispatch outcome is record-review's to decide, not this test's
        status = self.read_status()
        session = status["agent_sessions"][lib.role_session_ids(status)["reviewer"]]  # #420
        self.assertIs(session.get("compact"), True, f"launch ended with: {error}")
        self.assertEqual(session["result"]["payload"]["tests_executed"], "no")


if __name__ == "__main__":
    unittest.main()
