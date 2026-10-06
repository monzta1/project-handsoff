"""#397: implementation reviews get an engine-built, bounded packet, and a
review that cannot fit its budget is refused before it starts.

The #381-#393 run's first implementation review spent 199K of its 200K
tokens re-deriving every criterion's results from a 30-file diff and
verbose tests, and ended with no verdict. The engine already holds those
results; the packet hands them over, and the size check refuses the launch
that would repeat that run before anything is reserved.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.engine_patch import patch_engine
from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import HandsoffTestCase, run

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def _record(run_id, criteria, results=(), kind="checks", description=""):
    return {"run_id": run_id, "kind": kind, "criteria": list(criteria), "results": list(results),
            "description": description}


def _result(command, exit_code, tail):
    return {"command": command, "exit_code": exit_code, "output_tail": tail}


NO_DIFF = {"base": None, "bytes": 0, "files": []}


class ThePacketCarriesEachCommandsOwnResult(unittest.TestCase):
    """REQ-001: per criterion, per test command, the latest ledger result
    for that exact command, or 'missing'."""

    CRITERION = {"id": "REQ-001", "verification": "automated",
                 "tests": ["python3 -m unittest tests.a", "python3 -m unittest tests.b", "python3 -m unittest tests.c"]}

    def test_a_multi_command_criterion_reports_one_failing_and_one_passing_command(self):
        records = [
            _record("vr-old", ["REQ-001"], [_result("python3 -m unittest tests.a", 0, "OK old")]),
            _record("vr-new", ["REQ-001"], [_result("python3 -m unittest tests.a", 1, "FAILED (failures=1)"),
                                            _result("python3 -m unittest tests.b", 0, "OK")]),
            # Another criterion's run of the same command is not this criterion's evidence.
            _record("vr-other", ["REQ-009"], [_result("python3 -m unittest tests.a", 0, "OK elsewhere")]),
        ]
        packet = lib.build_implementation_review_packet([self.CRITERION], records, NO_DIFF)
        commands = packet["criteria"][0]["commands"]
        self.assertEqual(commands, [
            {"command": "python3 -m unittest tests.a", "run_id": "vr-new", "exit_code": 1,
             "output_tail": "FAILED (failures=1)"},
            {"command": "python3 -m unittest tests.b", "run_id": "vr-new", "exit_code": 0, "output_tail": "OK"},
            {"command": "python3 -m unittest tests.c", "result": "missing"},
        ])
        self.assertEqual(packet["criteria"][0]["verification"], "automated")

    def test_a_tail_is_at_most_one_kibibyte(self):
        records = [_record("vr-1", ["REQ-001"], [_result("python3 -m unittest tests.a", 0, "x" * 5000 + "END")])]
        tail = lib.build_implementation_review_packet([self.CRITERION], records, NO_DIFF)["criteria"][0]["commands"][0]["output_tail"]
        self.assertEqual(len(tail.encode("utf-8")), 1024)
        self.assertTrue(tail.endswith("END"))

    def test_a_manual_criterion_carries_its_bounded_evidence_and_run_id(self):
        criterion = {"id": "REQ-004", "verification": "manual", "tests": ["live: something"]}
        records = [_record("vr-live", ["REQ-004"], kind="manual", description="d" * 900)]
        entry = lib.build_implementation_review_packet([criterion], records, NO_DIFF)["criteria"][0]
        self.assertEqual(entry["evidence"], {"run_id": "vr-live", "description": "d" * 600})
        self.assertNotIn("commands", entry)
        missing = lib.build_implementation_review_packet([criterion], [], NO_DIFF)["criteria"][0]
        self.assertEqual(missing["evidence"], "missing")

    def test_the_packet_tells_the_reviewer_how_to_use_it(self):
        full = lib.build_implementation_review_packet([self.CRITERION], [], NO_DIFF)
        self.assertIn("Open only the hunks you doubt", full["instructions"])
        self.assertIn("Rerun the tests once, quietly", full["instructions"])
        compact = lib.build_implementation_review_packet([self.CRITERION], [], NO_DIFF, compact=True)
        self.assertIn("Do not run tests; judge from the recorded results", compact["instructions"])


def _files(count, hunks):
    return {"base": "0" * 40, "bytes": 0,
            "files": [{"path": f"src/module_{n:04d}.py", "hunks": [f"-{h},2 +{h},3" for h in range(1, hunks + 1)]}
                      for n in range(count)]}


def _many_criteria(count, tail_bytes):
    criteria = [{"id": f"REQ-{n:03d}", "verification": "automated", "tests": [f"python3 -m unittest tests.t{n}"]}
                for n in range(count)]
    records = [_record(f"vr-{n}", [f"REQ-{n:03d}"], [_result(f"python3 -m unittest tests.t{n}", 0, "x" * tail_bytes)])
               for n in range(count)]
    return criteria, records


class OversizedInputIsTrimmedDeterministically(unittest.TestCase):
    """REQ-001: criterion data first; hunk ranges collapse to counts, then the
    files beyond the cap become a count; `omitted` and `truncated` say so."""

    CRITERIA = [{"id": "REQ-001", "verification": "automated", "tests": ["true"]}]
    RECORDS = [_record("vr-1", ["REQ-001"], [_result("true", 0, "ok")])]

    def _bytes(self, packet):
        return lib.implementation_review_packet_bytes(packet)

    def test_a_small_packet_keeps_every_hunk_range(self):
        packet = lib.build_implementation_review_packet(self.CRITERIA, self.RECORDS, _files(3, 2))
        self.assertEqual(packet["changed_files"][0], {"path": "src/module_0000.py", "hunks": ["-1,2 +1,3", "-2,2 +2,3"]})
        self.assertEqual((packet["omitted"], packet["truncated"], packet["files_omitted"]), ([], [], 0))

    def test_hunk_ranges_collapse_to_counts_first(self):
        packet = lib.build_implementation_review_packet(self.CRITERIA, self.RECORDS, _files(500, 30))
        self.assertLessEqual(self._bytes(packet), lib.MAX_IMPLEMENTATION_REVIEW_PACKET_BYTES)
        self.assertEqual(packet["omitted"], ["hunk_ranges"])
        self.assertEqual(len(packet["changed_files"]), 500)
        self.assertEqual(packet["changed_files"][0], {"path": "src/module_0000.py", "hunk_count": 30})

    def test_then_the_files_beyond_the_cap_become_a_count(self):
        diff = _files(4000, 1)
        packet = lib.build_implementation_review_packet(self.CRITERIA, self.RECORDS, diff)
        self.assertLessEqual(self._bytes(packet), lib.MAX_IMPLEMENTATION_REVIEW_PACKET_BYTES)
        self.assertEqual(packet["omitted"], ["hunk_ranges", "files_beyond_cap"])
        self.assertEqual(len(packet["changed_files"]) + packet["files_omitted"], 4000)
        self.assertGreater(packet["files_omitted"], 0)
        self.assertEqual(packet["criteria"][0]["commands"][0]["output_tail"], "ok")
        self.assertEqual(packet, lib.build_implementation_review_packet(self.CRITERIA, self.RECORDS, diff),
                         "trimming must be deterministic")

    def test_criterion_data_over_the_cap_cuts_tails_and_descriptions_and_drops_nothing(self):
        criteria, records = _many_criteria(90, 1024)
        criteria.append({"id": "REQ-MAN", "verification": "manual", "tests": ["live: x"]})
        records.append(_record("vr-man", ["REQ-MAN"], kind="manual", description="m" * 600))
        packet = lib.build_implementation_review_packet(criteria, records, _files(10, 2))
        self.assertLessEqual(self._bytes(packet), lib.MAX_IMPLEMENTATION_REVIEW_PACKET_BYTES)
        self.assertEqual(packet["truncated"], ["output_tail:256", "description:300"])
        self.assertEqual(len(packet["criteria"]), 91)
        for entry in packet["criteria"][:90]:
            self.assertEqual(len(entry["commands"]), 1)
            self.assertEqual(len(entry["commands"][0]["output_tail"]), 256)
        self.assertEqual(len(packet["criteria"][90]["evidence"]["description"]), 300)

    def test_criterion_data_that_still_cannot_fit_is_refused(self):
        criteria, records = _many_criteria(400, 1024)
        with self.assertRaisesRegex(lib.HandsoffError, r"packet is \d+ bytes of criterion data, over the 65536-byte cap"):
            lib.build_implementation_review_packet(criteria, records, NO_DIFF)


class TheSizeCheckEstimate(unittest.TestCase):
    """REQ-003: 1.5 times the diff's tokens plus the packet's tokens."""

    def test_the_estimate_formula(self):
        self.assertEqual(lib.implementation_review_estimate(diff_bytes=4000, packet_bytes=400), 1600)

    def test_under_the_budget_is_allowed_and_over_it_names_the_remedies(self):
        self.assertIsNone(lib.implementation_review_size_refusal(
            diff_bytes=4000, packet_bytes=400, budget=1600, compact=False))
        refusal = lib.implementation_review_size_refusal(
            diff_bytes=4000, packet_bytes=400, budget=1599, compact=False)
        for text in ("estimated at 1600 tokens", "[agent_budget].reviewer 1599",
                     "a review per implementer lane", "--compact-scope", "larger [agent_budget] reviewer budget"):
            self.assertIn(text, refusal)


def claude_only(name):
    return "/usr/local/bin/claude" if name == "claude" else None


class APhaseFiveReviewerLaunch(HandsoffTestCase):
    """REQ-001..REQ-003 through the real launch builder and the real CLI, on a
    git project whose ledger was written by `verify`."""

    SCOPE = [{"path": "src/mod.py", "start": 2, "end": 4}]

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        (self.tmp / "handsoff-overrides.json").write_text(json.dumps({"schema": 1, "files": {
            f"prompts/{role}.md": hashlib.sha256((self.tmp / "prompts" / f"{role}.md").read_bytes()).hexdigest()
            for role in ("architect", "implementer", "reviewer")}}))
        # REQ-001 runs one passing and one failing command on purpose, so the
        # packet is seen to carry both; the reviewer launch's evidence gate
        # (which a failing command trips) is another feature's and is stood
        # down here.
        gaps = patch_engine("reviewer_launch_evidence_gaps", return_value=[])
        gaps.__enter__()
        self.addCleanup(gaps.__exit__, None, None, None)
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        text = text.replace('reviewer = "auto"', 'reviewer = "claude"', 1)
        text = text.replace("compatibility_mode = false", "compatibility_mode = true")
        text = text.replace("compatibility_approved = false", "compatibility_approved = true")
        text = text.replace("commands = []", 'commands = ["true", "false"]', 1)
        toml.write_text(text)
        (self.tmp / ".gitignore").write_text("handsoff-*\n.handsoff*\n")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "mod.py").write_text("".join(f"line {n}\n" for n in range(1, 11)))
        (self.tmp / "src" / "other.py").write_text("x = 1\n")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "Handsoff Fixture")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "baseline")
        self._git("checkout", "-q", "-b", "lane")
        (self.tmp / "src" / "mod.py").write_text("".join(f"line {n}\n" for n in range(1, 11)).replace("line 3", "LINE 3"))
        self._git("commit", "-q", "-am", "change line 3")
        (self.tmp / "src" / "other.py").write_text("x = 2\n")
        (self.tmp / "src" / "new.py").write_text("A = 1\nB = 2\n")
        self.init("Implementation review packet")
        updated = run(["criterion-update", "REQ-001", "--test", "true", "--test", "false"], cwd=self.tmp)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        records, _problems = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        self.assertTrue(records, "verify must have written the ledger")
        self._set_phase(5, original_symptom_evidence_id=records[-1]["run_id"])

    def test_handsoff_state_files_are_not_changed_files_even_when_not_gitignored(self):
        """REQ-004's live proof: a project whose .gitignore does not cover
        Handsoff's own state listed 21 .handsoff* files and ledger backups as
        changed files and counted them in the size estimate."""
        (self.tmp / ".gitignore").write_text("")
        before = lib.implementation_review_diff(self.tmp)
        for name in (".handsoff-live.json", ".handsoff-preflight.json", "handsoff-status.json.bak",
                     ".handsoff-runtime-control/performance.json"):
            path = self.tmp / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("{}" * 5000)
        diff = lib.implementation_review_diff(self.tmp)
        paths = [entry["path"] for entry in diff["files"]]
        created = {".handsoff-live.json", ".handsoff-preflight.json", "handsoff-status.json.bak",
                   ".handsoff-runtime-control/performance.json"}
        self.assertFalse(created & set(paths), paths)
        self.assertIn("src/mod.py", paths)
        self.assertEqual(diff["bytes"], before["bytes"])

    def _set_phase(self, number, **extra):
        path = self.tmp / "handsoff-status.json"
        status = json.loads(path.read_text())
        status["phase_number"] = number
        status.update(extra)
        path.write_text(json.dumps(status, indent=2))

    def _spec(self, **kwargs):
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only,
                                         skip_preflight=True, **kwargs)
        self.addCleanup(shutil.rmtree, spec.cwd, True)
        return spec

    def _packet_from(self, stdin):
        heading = runtime.IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING + "\n\n"
        self.assertIn(heading, stdin)
        return json.loads(stdin.split(heading, 1)[1].split("\n", 1)[0])

    def _digest(self, *names):
        return {name: hashlib.sha256((self.tmp / name).read_bytes()).hexdigest()
                for name in names if (self.tmp / name).exists()}

    def test_the_reviewer_receives_the_packet_ahead_of_its_task(self):
        stdin = self._spec().stdin
        packet = self._packet_from(stdin)
        self.assertLess(stdin.index(runtime.IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING), stdin.index("# Assigned task"))
        commands = {item["command"]: item for item in packet["criteria"][0]["commands"]}
        self.assertEqual(commands["true"]["exit_code"], 0)
        self.assertEqual(commands["false"]["exit_code"], 1)
        self.assertTrue(commands["true"]["run_id"].startswith("vr-"))
        files = {item["path"]: item["hunks"] for item in packet["changed_files"]}
        self.assertEqual(files["src/mod.py"], ["-3 +3"])
        self.assertEqual(files, {"src/mod.py": ["-3 +3"], "src/other.py": ["-1 +1"], "src/new.py": ["-0,0 +1,2"]},
                         "a committed change, an uncommitted edit and an untracked file, from the merge base")
        self.assertLessEqual(len(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()),
                             lib.MAX_IMPLEMENTATION_REVIEW_PACKET_BYTES)

    def test_another_phase_or_role_gets_no_packet(self):
        self._set_phase(4)
        self.assertNotIn(runtime.IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING,
                         runtime.build_role_input(self.tmp, "reviewer", "Review it."))
        self._set_phase(5)
        self.assertNotIn(runtime.IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING,
                         runtime.build_role_input(self.tmp, "implementer", "Do it."))

    def test_a_follow_up_attempt_still_carries_the_delta_packet(self):
        self._set_phase(5, review_attempts=[{"attempt": 1, "disposition": "changes_requested",
                                             "closed_at": "2026-01-01T00:00:00+00:00",
                                             "findings": [{"code": "other", "summary": "a gap"}]}])
        stdin = runtime.build_role_input(self.tmp, "reviewer", "Review it.")
        self.assertIn(runtime.IMPLEMENTATION_REVIEW_PACKET_HEADING + "\n\n", stdin)
        self._packet_from(stdin)

    def test_a_phase_five_compact_launch_gets_the_packet_told_not_to_run_tests(self):
        packet = self._packet_from(self._spec(compact_scope=self.SCOPE).stdin)
        self.assertIn("Do not run tests; judge from the recorded results", packet["instructions"])
        self.assertEqual({item["command"] for item in packet["criteria"][0]["commands"]}, {"true", "false"})

    def test_criterion_data_too_large_is_refused_before_any_reservation(self):
        before = self._digest("handsoff-status.json", "handsoff-verifications.jsonl", "handsoff-events.jsonl")
        with patch_engine("MAX_IMPLEMENTATION_REVIEW_PACKET_BYTES", 200), \
                mock.patch.object(runtime, "_reviewer_scratch", side_effect=AssertionError("scratch reserved")), \
                self.assertRaisesRegex(lib.HandsoffError, r"packet is \d+ bytes of criterion data, over the 200-byte cap"):
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only, skip_preflight=True)
        self.assertEqual(before, self._digest("handsoff-status.json", "handsoff-verifications.jsonl",
                                              "handsoff-events.jsonl"))
        self.assertEqual(self.read_status().get("agent_sessions") or {}, {})

    def _write_large_diff(self):
        budget = lib.load_config(self.tmp)["agent_token_budgets"]["reviewer"]
        (self.tmp / "src" / "large.py").write_text("X = 1\n" * (budget * 4 // 6 + 1))
        return budget

    def test_a_diff_under_the_bound_launches(self):
        self.assertEqual(self._spec().role, "reviewer")

    def test_a_diff_over_the_bound_is_refused_with_nothing_reserved(self):
        budget = self._write_large_diff()
        before = self._digest("handsoff-status.json", "handsoff-verifications.jsonl", "handsoff-events.jsonl")
        with mock.patch.object(runtime, "_reviewer_scratch", side_effect=AssertionError("scratch reserved")), \
                self.assertRaises(lib.HandsoffError) as refused:
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=claude_only, skip_preflight=True)
        message = str(refused.exception)
        for text in ("refused before session creation", "estimated at", f"[agent_budget].reviewer {budget}",
                     "a review per implementer lane", "--compact-scope"):
            self.assertIn(text, message)
        self.assertEqual(before, self._digest("handsoff-status.json", "handsoff-verifications.jsonl",
                                              "handsoff-events.jsonl"))
        self.assertEqual(self.read_status().get("agent_sessions") or {}, {})

    def test_a_compact_launch_over_the_same_diff_is_allowed(self):
        self._write_large_diff()
        spec = self._spec(compact_scope=self.SCOPE)
        self.assertEqual(spec.compact_scope, tuple(self.SCOPE))

    def test_the_command_prints_the_packet_and_leaves_the_ledger_byte_identical(self):
        names = ("handsoff-verifications.jsonl", "handsoff-events.jsonl", "handsoff-status.json",
                 "handsoff-acceptance.json")
        before = self._digest(*names)
        result = run(["implementation-review-packet"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual(printed, lib.implementation_review_packet(self.tmp, lib.load_config(self.tmp)))
        self.assertEqual({item["command"]: item["exit_code"] for item in printed["criteria"][0]["commands"]},
                         {"true": 0, "false": 1})
        self.assertEqual(before, self._digest(*names))
        self.assertIn("handsoff-verifications.jsonl", before)


if __name__ == "__main__":
    unittest.main()
