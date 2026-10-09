"""Escaped-defect ledger: record, overlap, run-scoped declines, close.

These drive bin/handsoff_defects.py directly. The CLI commands and the
Phase 3 gate that call it are wired in the supervisor and workflow; the
gate's refusal is exercised here through `inherited_regressions`, which is
the list that gate refuses on.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))

import handsoff_defects as defects  # noqa: E402
from handsoff_core import HandsoffError  # noqa: E402
from handsoff_ledger import repository_digest  # noqa: E402


def _fields(**overrides):
    fields = {
        "issue": "#999", "control": "test_selection",
        "summary": "the auth handler accepted an expired token",
        "regression": "test_expired_token_refused", "paths": ["src/auth/*.py"], "by": "pilot",
    }
    fields.update(overrides)
    return fields


def _acceptance(*criteria):
    return {"criteria": [dict(c) for c in criteria]}


def _criterion(cid, requirement="unrelated work", paths=None):
    criterion = {"id": cid, "requirement": requirement}
    if paths is not None:
        criterion["paths"] = list(paths)
    return criterion


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-defects-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_run(self, phase, acceptance):
        (self.root / "handsoff-status.json").write_text(json.dumps({"phase_number": phase}))
        (self.root / "handsoff-acceptance.json").write_text(json.dumps(acceptance))


class RecordAndList(LedgerCase):
    def test_record_appends_an_open_defect_and_list_returns_it(self):
        defect = defects.record_defect(self.root, _fields())
        self.assertTrue(defect["id"].startswith("DEF-"))
        self.assertEqual(defect["state"], "open")
        self.assertEqual(defect["control"], "test_selection")
        self.assertEqual(defect["recorded_by"], "pilot")
        self.assertEqual(defect["paths"], ["src/auth/*.py"])
        listed = defects.load_defects(self.root)
        self.assertEqual([d["id"] for d in listed], [defect["id"]])
        lines = (self.root / defects.DEFECTS_FILE).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["kind"], "record")

    def test_ledger_is_append_only(self):
        first = defects.record_defect(self.root, _fields())
        before = (self.root / defects.DEFECTS_FILE).read_text()
        defects.record_defect(self.root, _fields(issue="#1000", paths=[]))
        defects.decide_defect(self.root, first["id"], "decline", "out of scope", "pilot", run_id="run-a")
        after = (self.root / defects.DEFECTS_FILE).read_text()
        self.assertTrue(after.startswith(before), "earlier lines must never be rewritten")
        self.assertEqual(len(after.splitlines()), 3)

    def test_every_control_value_is_accepted(self):
        for control in defects.CONTROLS:
            self.assertEqual(defects.record_defect(self.root, _fields(control=control))["control"], control)
        self.assertEqual(set(defects.CONTROLS), {
            "requirement", "implementation", "test_selection", "environment",
            "reviewer_visibility", "live_verification", "deployment", "handsoff_integrity"})

    def test_invalid_records_are_refused_and_nothing_is_written(self):
        bad = [
            _fields(control="vibes"), _fields(control=None), _fields(summary="  "),
            _fields(regression=None), _fields(issue=""), _fields(by=""),
            _fields(paths="src/*.py"), _fields(paths=[""]), _fields(extra="x"),
        ]
        for fields in bad:
            with self.subTest(fields=fields), self.assertRaises(HandsoffError):
                defects.record_defect(self.root, fields)
        self.assertFalse((self.root / defects.DEFECTS_FILE).exists())

    def test_a_corrupt_ledger_is_refused_rather_than_appended_to(self):
        (self.root / defects.DEFECTS_FILE).write_text("{not json\n")
        with self.assertRaises(HandsoffError):
            defects.record_defect(self.root, _fields())
        self.assertEqual((self.root / defects.DEFECTS_FILE).read_text(), "{not json\n")


class Overlap(unittest.TestCase):
    def test_intersecting_globs_whose_strings_differ_overlap(self):
        self.assertTrue(defects.globs_overlap("src/*/handler.py", "src/auth/*.py"))
        self.assertTrue(defects.globs_overlap("src/auth/*.py", "src/*/handler.py"))
        self.assertTrue(defects.globs_overlap("**/*.py", "docs/index.md"))
        self.assertTrue(defects.globs_overlap("src/auth/login.py", "src/auth/login.py"))
        self.assertTrue(defects.globs_overlap("./src/a?.py", "src/ab.py"))

    def test_provably_disjoint_prefixes_do_not_overlap(self):
        self.assertFalse(defects.globs_overlap("src/auth/*.py", "docs/*.md"))
        self.assertFalse(defects.globs_overlap("src/auth/*.py", "src/billing/*.py"))
        self.assertFalse(defects.globs_overlap("bin/a.py", "bin/b.py"))

    def test_run_overlap(self):
        defect = {"paths": ["src/auth/*.py"]}
        self.assertTrue(defects.defect_overlaps_run(
            defect, _acceptance(_criterion("REQ-001", paths=["src/*/handler.py"]))))
        self.assertFalse(defects.defect_overlaps_run(
            defect, _acceptance(_criterion("REQ-001", paths=["docs/*.md"]))))

    def test_missing_paths_on_either_side_overlap_everything(self):
        self.assertTrue(defects.defect_overlaps_run(
            {"paths": []}, _acceptance(_criterion("REQ-001", paths=["docs/*.md"]))))
        self.assertTrue(defects.defect_overlaps_run(
            {"paths": ["src/auth/*.py"]},
            _acceptance(_criterion("REQ-001", paths=["docs/*.md"]), _criterion("REQ-002"))))


class Inheritance(LedgerCase):
    def test_overlapping_open_defect_is_inherited_and_a_disjoint_one_is_not(self):
        hit = defects.record_defect(self.root, _fields(paths=["src/auth/*.py"]))
        defects.record_defect(self.root, _fields(paths=["docs/*.md"]))
        run = _acceptance(_criterion("REQ-001", paths=["src/*/handler.py"]))
        self.assertEqual([d["id"] for d in defects.inherited_regressions(self.root, run, "run-a")],
                         [hit["id"]])

    def test_adoption_by_citation_clears_the_inheritance(self):
        defect = defects.record_defect(self.root, _fields())
        blocked = _acceptance(_criterion("REQ-001", paths=["src/auth/*.py"]))
        self.assertEqual(len(defects.inherited_regressions(self.root, blocked, "run-a")), 1)
        adopted = _acceptance(
            _criterion("REQ-001", paths=["src/auth/*.py"]),
            _criterion("REQ-002", requirement=f"Fix {defect['id']}: expired tokens are refused",
                       paths=["src/auth/*.py"]))
        self.assertEqual(defects.inherited_regressions(self.root, adopted, "run-a"), [])
        # A longer id that merely starts with this one is not a citation.
        lookalike = _acceptance(_criterion("REQ-001", requirement=f"see {defect['id']}0",
                                           paths=["src/auth/*.py"]))
        self.assertEqual(len(defects.inherited_regressions(self.root, lookalike, "run-a")), 1)

    def test_a_reasoned_decline_clears_this_run_only_and_the_next_run_inherits_it_again(self):
        defect = defects.record_defect(self.root, _fields())
        run = _acceptance(_criterion("REQ-001", paths=["src/auth/*.py"]))
        declined = defects.decide_defect(self.root, defect["id"], "decline",
                                         "this run only touches logging", "pilot", run_id="run-a")
        self.assertEqual(declined["state"], "open")
        self.assertEqual(declined["declined_runs"], ["run-a"])
        self.assertEqual(declined["decisions"][0]["reason"], "this run only touches logging")
        self.assertEqual(defects.inherited_regressions(self.root, run, "run-a"), [])
        self.assertEqual([d["id"] for d in defects.inherited_regressions(self.root, run, "run-b")],
                         [defect["id"]])

    def test_a_decline_needs_a_reason_and_a_run(self):
        defect = defects.record_defect(self.root, _fields())
        with self.assertRaises(HandsoffError):
            defects.decide_defect(self.root, defect["id"], "decline", "  ", "pilot", run_id="run-a")
        with self.assertRaises(HandsoffError):
            defects.decide_defect(self.root, defect["id"], "decline", "why", "pilot", run_id=None)
        with self.assertRaises(HandsoffError):
            defects.decide_defect(self.root, defect["id"], "approve", "why", "pilot", run_id="run-a")
        with self.assertRaises(HandsoffError):
            defects.decide_defect(self.root, "DEF-missing", "decline", "why", "pilot", run_id="run-a")
        self.assertEqual(defects.load_defects(self.root)[0]["decisions"], [])


class Close(LedgerCase):
    def test_close_is_refused_until_an_adopting_run_reaches_phase_8(self):
        defect = defects.record_defect(self.root, _fields())
        citing = _acceptance(_criterion("REQ-001", requirement=f"Adopt {defect['id']}",
                                        paths=["src/auth/*.py"]))
        with self.assertRaises(HandsoffError):  # no run at all
            defects.decide_defect(self.root, defect["id"], "close", None, "pilot")
        self.write_run(7, citing)
        with self.assertRaises(HandsoffError) as refused:
            defects.decide_defect(self.root, defect["id"], "close", None, "pilot")
        self.assertIn("Phase 7", str(refused.exception))
        self.write_run(8, _acceptance(_criterion("REQ-001", paths=["src/auth/*.py"])))
        with self.assertRaises(HandsoffError):  # Phase 8, but this run did not adopt it
            defects.decide_defect(self.root, defect["id"], "close", None, "pilot")
        self.assertEqual(defects.load_defects(self.root)[0]["state"], "open")
        self.write_run(8, citing)
        closed = defects.decide_defect(self.root, defect["id"], "close", None, "pilot", run_id="run-a")
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["decisions"][-1]["adopted_by"], ["REQ-001"])

    def test_a_closed_defect_is_never_inherited_and_takes_no_more_decisions(self):
        defect = defects.record_defect(self.root, _fields(paths=[]))
        self.write_run(8, _acceptance(_criterion("REQ-001", requirement=f"Adopt {defect['id']}")))
        defects.decide_defect(self.root, defect["id"], "close", None, "pilot")
        later = _acceptance(_criterion("REQ-001"))
        self.assertEqual(defects.inherited_regressions(self.root, later, "run-b"), [])
        for action in ("close", "decline"):
            with self.assertRaises(HandsoffError):
                defects.decide_defect(self.root, defect["id"], action, "why", "pilot", run_id="run-b")


class Digest(LedgerCase):
    def test_recording_a_defect_does_not_change_the_repository_digest(self):
        (self.root / "app.py").write_text("print('hi')\n")
        before = repository_digest(self.root)
        defect = defects.record_defect(self.root, _fields())
        defects.decide_defect(self.root, defect["id"], "decline", "later", "pilot", run_id="run-a")
        self.assertEqual(repository_digest(self.root), before)

    def test_a_tracked_ledger_does_not_change_the_digest_in_a_git_checkout(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        (self.root / "app.py").write_text("print('hi')\n")
        defects.record_defect(self.root, _fields())
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], cwd=self.root, check=True)
        subprocess.run(git + ["add", "-A"], cwd=self.root, check=True)
        subprocess.run(git + ["commit", "-q", "-m", "seed"], cwd=self.root, check=True)
        before = repository_digest(self.root)
        defects.record_defect(self.root, _fields(issue="#1001"))
        self.assertEqual(repository_digest(self.root), before)
        (self.root / "app.py").write_text("print('changed')\n")
        self.assertNotEqual(repository_digest(self.root), before, "the digest still sees real edits")


if __name__ == "__main__":
    unittest.main()


class DefectCommandEndToEnd(unittest.TestCase):
    """E4 live proof: the CLI wiring and the ledger module agreed only in
    their own tests; drive the real `defect record` command through to the
    inherited regression and the phase-3 gate."""

    def setUp(self):
        from tests.test_handsoff_supervisor import HandsoffTestCase  # noqa: F401  (fixture helpers)
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-defects-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy2(Path(BIN).parent / "handsoff.toml", self.root / "handsoff.toml")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def cli(self, *argv):
        return subprocess.run([sys.executable, str(Path(BIN) / "handsoff_supervisor.py"), "--root", str(self.root),
                               *argv], capture_output=True, text=True, cwd=self.root)

    def test_record_then_a_new_run_inherits_it(self):
        recorded = self.cli("defect", "record", "--issue", "#8", "--control", "test_selection",
                            "--summary", "escaped", "--regression", "keep the docstring",
                            "--path", "stats/**", "--by", "pilot")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        defect = json.loads(recorded.stdout)["recorded"]
        self.assertEqual(defect["recorded_by"], "pilot")
        self.assertEqual([d["id"] for d in defects.load_defects(self.root)], [defect["id"]])
