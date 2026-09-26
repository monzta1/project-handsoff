"""#300: the routing evidence projection and its outcome taxonomy.

The epic's ordering principle is that measurement precedes activation, so this
ticket produces evidence and changes no gate. What the tests below hold to is
the three rules the epic states, because each one is a way the projection could
look right and be useless:

- process completion is not success;
- missing usage stays UNKNOWN, never zero and never estimated;
- a correction is a new projection, never a rewrite of prior evidence.

The fixtures are built from the shapes real archives carry, including the one
that matters most: `{"source": "adapter", "tokens_in": null, "tokens_out":
null, "tokens_total": 16928}`. A projection that defaulted those nulls to 0
would report a session that spent 16,928 tokens as having spent none on input
and output, and every tokens-per-outcome aggregate in #301 would be wrong.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_evidence as evidence  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def session(session_id="hs-" + "1" * 32, **overrides):
    record = {
        "session_id": session_id, "role": "reviewer", "actor": "codex-reviewer",
        "adapter": "codex", "requested_model": "default", "reported_model": "gpt-5.6-luna",
        "resolution_source": "configured", "phase_number": 5, "tier": "STANDARD",
        "started_at": "2026-09-24T10:00:00+00:00", "running_at": "2026-09-24T10:00:30+00:00",
        "ended_at": "2026-09-24T10:20:30+00:00", "state": "completed", "exit_code": 0,
        "usage": {"source": "adapter", "tokens_in": 1000, "tokens_out": 500,
                  "tokens_total": 1500},
        "result": {"kind": "review", "adopted_at": "2026-09-24T10:21:00+00:00",
                   "adopted_by": "host",
                   "payload": {"kind": "review", "decision": "approved", "findings": [],
                               "tests_executed": "yes"}},
    }
    record.update(overrides)
    return record


def archive(status_overrides=None, sessions=None, **overrides):
    status = {
        "feature": "A feature", "phase_number": 8, "phase": "Live verified", "progress": 100,
        "status": "complete", "risk_class": "routine",
        "agent_sessions": {s["session_id"]: s for s in (sessions or [session()])},
    }
    status.update(status_overrides or {})
    record = {"repo": "widget", "root": "/tmp/widget", "run_kind": "product",
              "feature": status["feature"], "status": status, "acceptance": {"criteria": []},
              "verifications": [], "events": [], "metrics": {}, "usage": {}}
    record.update(overrides)
    return record


def project(record):
    return evidence.project_archive(record, source_sha256="0" * 64, source_name="widget.json")


class MissingUsageStaysUnknown(unittest.TestCase):
    """The rule that would be easiest to break by writing `or 0`."""

    def test_a_total_without_a_split_reports_unknown_for_the_split(self):
        """The real shape: a total of 16,928 with both halves null."""
        record = project(archive(sessions=[session(usage={
            "source": "adapter", "tokens_in": None, "tokens_out": None,
            "tokens_total": 16928})]))[0]
        self.assertEqual(record["tokens"]["tokens_total"], 16928)
        self.assertEqual(record["tokens"]["tokens_in"], evidence.UNKNOWN)
        self.assertEqual(record["tokens"]["tokens_out"], evidence.UNKNOWN)
        self.assertIn("usage_partial", record["quality_flags"])

    def test_no_usage_at_all_is_unknown_not_zero(self):
        record = project(archive(sessions=[session(usage=None)]))[0]
        for field in ("tokens_in", "tokens_out", "tokens_total"):
            self.assertEqual(record["tokens"][field], evidence.UNKNOWN, field)
        self.assertIn("usage_not_reported", record["quality_flags"])

    def test_usage_the_adapter_did_not_report_is_not_trusted(self):
        """A source other than the adapter is an estimate by definition."""
        record = project(archive(sessions=[session(usage={
            "source": "estimated", "tokens_in": 10, "tokens_out": 10, "tokens_total": 20})]))[0]
        self.assertEqual(record["tokens"]["tokens_total"], evidence.UNKNOWN)

    def test_a_real_zero_survives(self):
        """UNKNOWN must not swallow a genuine zero, or the sentinel is useless."""
        record = project(archive(sessions=[session(usage={
            "source": "adapter", "tokens_in": 0, "tokens_out": 0, "tokens_total": 0})]))[0]
        self.assertEqual(record["tokens"]["tokens_total"], 0)
        self.assertNotIn("usage_not_reported", record["quality_flags"])

    def test_a_boolean_is_not_an_integer(self):
        """`True` is an int in Python, and would read as 1 token."""
        record = project(archive(sessions=[session(usage={
            "source": "adapter", "tokens_in": True, "tokens_out": 5,
            "tokens_total": 5})]))[0]
        self.assertEqual(record["tokens"]["tokens_in"], evidence.UNKNOWN)


class ProcessCompletionIsNotSuccess(unittest.TestCase):
    """#290 is the reference case: both reviewers completed and produced nothing."""

    def test_a_completed_session_with_no_result_is_negative_evidence(self):
        record = project(archive(sessions=[session(result=None, state="completed",
                                                   exit_code=0)]))[0]
        self.assertEqual(record["outcome"], "protocol_no_result")
        self.assertTrue(record["negative"],
                        "a process that exits 0 having produced no verdict is a cost with no result")

    def test_an_approval_from_a_reviewer_that_ran_no_tests_is_unsupported(self):
        record = project(archive(sessions=[session(result={
            "kind": "review", "payload": {"decision": "approved", "tests_executed": "no"}})]))[0]
        self.assertEqual(record["outcome"], "review_unsupported")
        self.assertTrue(record["negative"])

    def test_changes_requested_is_negative_not_a_failure(self):
        record = project(archive(sessions=[session(result={
            "kind": "review",
            "payload": {"decision": "changes_requested", "tests_executed": "yes"}})]))[0]
        self.assertEqual(record["outcome"], "review_changes_requested")
        self.assertTrue(record["negative"])

    def test_budget_exhaustion_outranks_the_exit_code(self):
        record = project(archive(
            status_overrides={"agent_failures": {"hs-" + "1" * 32: {
                "category": "token_budget_exhaustion", "reason": "x", "tail_sha256": "y"}}},
            sessions=[session(result=None)]))[0]
        self.assertEqual(record["outcome"], "budget_exhausted")

    def test_only_a_run_at_phase_8_reads_verified(self):
        verified = project(archive())[0]
        self.assertEqual(verified["outcome"], "verified_phase8")
        self.assertFalse(verified["negative"])
        unverified = project(archive(status_overrides={"phase_number": 5,
                                                       "status": "in_progress"}))[0]
        self.assertEqual(unverified["outcome"], "adopted_not_verified")
        self.assertIn("run_incomplete", unverified["quality_flags"])

    def test_every_outcome_is_in_the_closed_set(self):
        """A taxonomy with an escape hatch is not a taxonomy."""
        for outcome in evidence.ROUTING_OUTCOMES:
            self.assertIsInstance(outcome, str)
        self.assertEqual(evidence.NEGATIVE_OUTCOMES - set(evidence.ROUTING_OUTCOMES), set(),
                         "a negative outcome that is not an outcome")
        self.assertNotIn("verified_phase8", evidence.NEGATIVE_OUTCOMES)
        self.assertNotIn(evidence.UNKNOWN, evidence.NEGATIVE_OUTCOMES,
                         "absence of evidence is not negative evidence")


class TaskClassComesFromRecordedLabels(unittest.TestCase):
    """The cohort key #301 groups by, and the honest answer when it is absent."""

    def _with_labels(self, labels):
        return project(archive(status_overrides={
            "tranche_approval": {"labels": {"issue-1": labels}}}))[0]

    def test_a_known_label_maps_to_its_class(self):
        self.assertEqual(self._with_labels(["bug"])["task_class"], "bug")
        self.assertEqual(self._with_labels(["enhancement"])["task_class"], "feature")

    def test_the_github_prefixes_are_stripped_like_the_tranche_does(self):
        self.assertEqual(self._with_labels(["type: bug"])["task_class"], "bug")
        self.assertEqual(self._with_labels(["Kind:Enhancement"])["task_class"], "feature")

    def test_several_classes_resolve_by_a_documented_precedence(self):
        """Deterministic, or the same run projects differently on two machines."""
        self.assertEqual(self._with_labels(["docs", "bug"])["task_class"], "bug")
        self.assertEqual(self._with_labels(["bug", "security"])["task_class"], "security")
        self.assertEqual(self._with_labels(["security", "bug"])["task_class"], "security",
                         "order of the labels must not change the answer")

    def test_an_unrecognized_label_contributes_nothing(self):
        """Guessing from an unknown slug is how a cohort compares unlike work."""
        record = self._with_labels(["needs-triage", "P2"])
        self.assertEqual(record["task_class"], evidence.UNKNOWN)
        self.assertIn("task_class_unknown", record["quality_flags"])

    def test_no_labels_is_unknown_and_flagged(self):
        record = project(archive())[0]
        self.assertEqual(record["task_class"], evidence.UNKNOWN)
        self.assertIn("task_class_unknown", record["quality_flags"])

    def test_every_mapped_class_is_in_the_closed_set(self):
        for label, name in evidence.LABEL_TASK_CLASSES.items():
            self.assertIn(name, evidence.TASK_CLASSES, label)


class RecordsAreBoundToTheirSource(unittest.TestCase):
    """#300: hash-bound, and not editable through a caller-supplied flag."""

    def test_the_record_hash_covers_every_field(self):
        record = project(archive())[0]
        self.assertEqual(evidence.validate_record(record), [])
        edited = {**record, "outcome": "verified_phase8", "tokens": {**record["tokens"],
                                                                     "tokens_total": 1}}
        self.assertIn("record_hash does not match the record; it was edited after projection",
                      " ".join(evidence.validate_record(edited)))

    def test_the_derivation_version_is_part_of_the_identity(self):
        """A correction produces new records rather than rewriting old ones."""
        record = project(archive())[0]
        bumped = {**record, "derivation_version": record["derivation_version"] + 1}
        self.assertNotEqual(evidence.record_hash(bumped), record["record_hash"])

    def test_the_source_bytes_are_named_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget-run.json"
            payload = json.dumps(archive()).encode("utf-8")
            path.write_bytes(payload)
            record = evidence.project_archive_file(path)[0]
        self.assertEqual(record["source_archive"], "widget-run.json")
        self.assertEqual(record["source_sha256"], hashlib.sha256(payload).hexdigest())

    def test_negative_cannot_disagree_with_the_outcome(self):
        record = project(archive())[0]
        lied = {**record, "negative": True}
        self.assertIn("'negative' disagrees with the outcome taxonomy",
                      " ".join(evidence.validate_record(lied)))

    def test_a_malformed_archive_is_refused_not_guessed(self):
        with self.assertRaises(lib.HandsoffError):
            evidence.project_archive([1, 2, 3], source_sha256="0" * 64, source_name="x.json")

    def test_a_non_dict_session_is_skipped_rather_than_crashing(self):
        record = archive()
        record["status"]["agent_sessions"]["broken"] = "not a session"
        self.assertEqual(len(project(record)), 1)


class TheProjectionIsReproducible(unittest.TestCase):
    """#301 needs byte-identical aggregates from the same sources."""

    def test_projecting_twice_gives_identical_records(self):
        first, second = project(archive()), project(archive())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_one_record_per_managed_session(self):
        """Routing decides per session, so collapsing a run would attribute one
        role's outcome to another."""
        records = project(archive(sessions=[
            session("hs-" + "1" * 32, role="architect", adapter="claude"),
            session("hs-" + "2" * 32, role="reviewer", adapter="codex"),
        ]))
        self.assertEqual({r["role"] for r in records}, {"architect", "reviewer"})
        self.assertEqual({r["adapter"] for r in records}, {"claude", "codex"})

    def test_duration_reports_wall_clock_and_active_time_separately(self):
        record = project(archive())[0]
        self.assertEqual(record["duration"]["wall_clock_ms"], 1230000)
        self.assertEqual(record["duration"]["active_ms"], 1200000,
                         "active time excludes the wait for the adapter to start")

    def test_unparseable_timestamps_are_unknown_and_flagged(self):
        record = project(archive(sessions=[session(started_at="not a date",
                                                    ended_at="also not")]))[0]
        self.assertEqual(record["duration"]["wall_clock_ms"], evidence.UNKNOWN)
        self.assertIn("duration_unavailable", record["quality_flags"])


class TestRunsNeverEnterTheProjection(unittest.TestCase):
    """#300: test fixtures are excluded from every cohort."""

    def _corpus(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "product.json").write_text(json.dumps(archive()))
        (directory / "fixture.json").write_text(json.dumps(archive(run_kind="test")))
        (directory / "legacy.json").write_text(json.dumps(
            {k: v for k, v in archive().items() if k != "run_kind"}))
        (directory / "broken.json").write_text("{not json")
        (directory / "nosessions.json").write_text(json.dumps(
            archive(status_overrides={"agent_sessions": {}})))
        return directory

    def test_a_test_run_is_skipped_and_counted(self):
        out = evidence.project_archive_dir(self._corpus())
        self.assertEqual(out["skipped"]["test_run"], 1)
        self.assertNotIn("fixture.json", {r["source_archive"] for r in out["records"]})

    def test_a_legacy_archive_is_still_read_and_flagged(self):
        """Existing archives remain readable; that they pre-date run_kind is
        recorded rather than used to drop them."""
        out = evidence.project_archive_dir(self._corpus())
        legacy = [r for r in out["records"] if r["source_archive"] == "legacy.json"]
        self.assertEqual(len(legacy), 1)
        self.assertIn("legacy_archive", legacy[0]["quality_flags"])

    def test_an_unreadable_archive_is_reported_not_silently_dropped(self):
        out = evidence.project_archive_dir(self._corpus())
        self.assertEqual(len(out["unreadable"]), 1)
        self.assertIn("broken.json", out["unreadable"][0])

    def test_a_run_with_no_managed_sessions_is_counted(self):
        out = evidence.project_archive_dir(self._corpus())
        self.assertEqual(out["skipped"]["no_sessions"], 1)

    def test_including_test_runs_is_explicit_and_off_by_default(self):
        directory = self._corpus()
        default = evidence.project_archive_dir(directory)
        widened = evidence.project_archive_dir(directory, include_test_runs=True)
        self.assertGreater(len(widened["records"]), len(default["records"]))


class ItChangesNoGate(unittest.TestCase):
    """The epic's non-goal, asserted rather than trusted."""

    def test_the_module_never_writes_engine_state(self):
        source = (BIN / "handsoff_evidence.py").read_text(encoding="utf-8")
        for forbidden in ("commit(", "write_text(", "atomic_write_json(", "append_event("):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} in the evidence projection: it reads, it does not write")

    def test_it_imports_no_layer_that_could_mutate_a_run(self):
        import ast
        tree = ast.parse((BIN / "handsoff_evidence.py").read_text(encoding="utf-8"))
        engine = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("handsoff"):
                engine.add(node.module)
            elif isinstance(node, ast.Import):
                engine |= {a.name for a in node.names if a.name.startswith("handsoff")}
        self.assertEqual(engine, {"handsoff_core", "handsoff_config"},
                         "the projection needs the core and the shared archive rule; "
                         "anything else could mutate a run")


class ARealCompletedRunProducesARecord(HandsoffTestCase):
    """#300's first acceptance criterion, end to end through archive_run.

    The fixtures above build archive shapes by hand, which proves the
    derivation but not that the engine actually writes what the derivation
    reads. This drives the real writer.
    """

    def test_the_archive_a_completed_run_writes_projects_and_validates(self):
        self.init("Routing evidence end to end")
        cfg = lib.load_config(self.tmp)
        status = json.loads(lib.status_path(self.tmp, cfg).read_text(encoding="utf-8"))
        acceptance = json.loads(lib.acceptance_path(self.tmp, cfg).read_text(encoding="utf-8"))
        created = lib.create_agent_session(
            self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
            requested_model="default", resolution_source="configured")
        lib.transition_agent_session(self.tmp, created["session_id"], "running")
        lib.transition_agent_session(
            self.tmp, created["session_id"], "completed", exit_code=0,
            usage={"tokens_in": 900, "tokens_out": 100, "tokens_total": 1000,
                   "source": "adapter"})
        status = json.loads(lib.status_path(self.tmp, cfg).read_text(encoding="utf-8"))

        path = lib.archive_run(self.tmp, cfg, status, acceptance, [], [])
        records = evidence.project_archive_file(path)

        self.assertEqual(len(records), 1, "the archived run carried one managed session")
        record = records[0]
        self.assertEqual(evidence.validate_record(record), [],
                         "the engine wrote an archive the projection cannot validate")
        self.assertEqual(record["adapter"], "codex")
        self.assertEqual(record["role"], "reviewer")
        self.assertEqual(record["tokens"]["tokens_total"], 1000)
        self.assertEqual(record["tokens"]["tokens_in"], 900)
        self.assertEqual(record["source_sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())

    def test_the_run_that_produced_it_is_unchanged(self):
        """The projection reads. #300's non-goal is that no gate moves."""
        self.init("Routing evidence reads only")
        cfg = lib.load_config(self.tmp)
        status_file = lib.status_path(self.tmp, cfg)
        before = status_file.read_text(encoding="utf-8")
        status = json.loads(before)
        acceptance = json.loads(lib.acceptance_path(self.tmp, cfg).read_text(encoding="utf-8"))
        path = lib.archive_run(self.tmp, cfg, status, acceptance, [], [])
        evidence.project_archive_file(path)
        self.assertEqual(status_file.read_text(encoding="utf-8"), before,
                         "projecting evidence changed the run it read")


if __name__ == "__main__":
    unittest.main()
