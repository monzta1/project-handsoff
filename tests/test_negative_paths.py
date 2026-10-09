#!/usr/bin/env python3
"""P2.2: negative-path matrices for elevated criteria.

A criterion may declare `risk` (normal or elevated, absent is normal) and a
`negative_paths` matrix from each of the seven dimensions to a test
description or 'not_applicable: REASON'. Both are spec, validated by the one
criterion validator and bound by the spec hash. Advance 3 refuses while an
elevated criterion lacks a dimension or marks one not_applicable without a
reason. A normal criterion is never judged by the gate.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run
from tests.guards import guard

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402

FULL = {
    "unauthorized": "test_rejects_a_caller_without_the_role",
    "malformed_input": "test_refuses_a_truncated_payload",
    "duplicate": "test_second_submit_is_idempotent",
    "timeout_retry": "test_retry_after_upstream_timeout",
    "stale_data": "test_refuses_a_stale_etag",
    "partial_failure": "test_half_written_batch_rolls_forward",
    "rollback": "not_applicable: the change writes no persistent state",
}


def matrix_args(matrix):
    args = []
    for dimension, text in matrix.items():
        args += ["--negative-path", f"{dimension}={text}"]
    return args


class NegativePathFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(tempfile.mkdtemp(prefix="handsoff-negative-paths-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        script = self.scratch / "check.sh"
        script.write_text("exit 0\n")
        self.command = f"sh {script}"
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", f"commands = {json.dumps([self.command])}", 1))
        self.init("P2.2 negative-path matrices")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "P2.2 primary outcome",
                      "--test", self.command], cwd=self.tmp))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _refused(self, result, needle):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(needle, result.stdout + result.stderr)
        return result

    def _criterion(self, cid):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def _add(self, cid, *extra):
        return run(["criterion-add", cid, "--type", "supporting", "--requirement", f"P2.2 {cid}",
                    "--verification", "automated", "--test", self.command, *extra], cwd=self.tmp)


class TestElevatedMatrixGate(NegativePathFixture):
    def test_elevated_with_a_full_matrix_passes_advance_3(self):
        self._ok(self._add("REQ-002", "--risk", "elevated", *matrix_args(FULL)))
        criterion = self._criterion("REQ-002")
        self.assertEqual(criterion["risk"], "elevated")
        self.assertEqual(criterion["negative_paths"], FULL)
        self._ok(self.advance_to(3))
        self.assertEqual(self.read_status()["phase_number"], 3)

    def test_a_missing_dimension_refuses_advance_3_until_it_is_added(self):
        partial = {k: v for k, v in FULL.items() if k not in ("rollback", "stale_data")}
        self._ok(self._add("REQ-002", "--risk", "elevated", *matrix_args(partial)))
        refused = self._refused(self.advance_to(3), "negative-path gate: REQ-002 is risk elevated")
        self.assertIn("lacks stale_data rollback", refused.stdout)
        # the refusal names its clearing command (#421)
        self.assertIn("clears with: handsoff_supervisor.py criterion-update REQ-002 --negative-path", refused.stdout)
        self.assertEqual(self.read_status()["phase_number"], 2)
        # one dimension at a time: entries merge into the stored matrix
        self._ok(run(["criterion-update", "REQ-002", "--negative-path", "stale_data=test_refuses_a_stale_etag",
                      "--revoke-approval"], cwd=self.tmp))
        self._refused(self.advance_to(3), "lacks rollback")
        self._ok(run(["criterion-update", "REQ-002", "--negative-path",
                      "rollback=not_applicable: nothing is persisted", "--revoke-approval"], cwd=self.tmp))
        self.assertEqual(set(self._criterion("REQ-002")["negative_paths"]), set(workflow.NEGATIVE_PATH_DIMENSIONS))
        self._ok(self.advance_to(3))

    def test_a_not_applicable_entry_without_a_reason_refuses_advance_3(self):
        for reasonless in ("not_applicable", "not_applicable:", "not_applicable:   "):
            with self.subTest(entry=reasonless):
                self.assertTrue(workflow.negative_path_reason_missing(reasonless))
        self.assertFalse(workflow.negative_path_reason_missing("not_applicable: read-only path"))
        self.assertFalse(workflow.negative_path_reason_missing("test_rollback_restores_the_row"))
        self._ok(self._add("REQ-002", "--risk", "elevated",
                           *matrix_args({**FULL, "duplicate": "not_applicable"})))
        refused = self._refused(self.advance_to(3), "negative-path gate: REQ-002 marks duplicate not_applicable "
                                                    "without a reason")
        self.assertNotIn("lacks", refused.stdout)
        self._ok(run(["criterion-update", "REQ-002", "--negative-path",
                      "duplicate=not_applicable: the endpoint is a pure read", "--revoke-approval"],
                     cwd=self.tmp))
        self._ok(self.advance_to(3))

    def test_elevated_with_no_matrix_lists_every_dimension(self):
        self._ok(self._add("REQ-002", "--risk", "elevated"))
        refused = self._refused(self.advance_to(3), "negative-path gate")
        self.assertIn("lacks " + " ".join(workflow.NEGATIVE_PATH_DIMENSIONS), refused.stdout)

    def test_the_status_payload_shows_risk_and_the_matrix(self):
        self._ok(self._add("REQ-002", "--risk", "elevated", *matrix_args(FULL)))
        payload = json.loads(run(["status"], cwd=self.tmp).stdout)
        rows = {row["id"]: row for row in payload["criteria"]}
        self.assertEqual((rows["REQ-002"]["risk"], rows["REQ-002"]["negative_paths"]), ("elevated", FULL))
        self.assertEqual((rows["REQ-001"]["risk"], rows["REQ-001"]["negative_paths"]), ("normal", {}))


class TestValidation(NegativePathFixture):
    def test_invalid_values_are_refused_by_the_shared_validator(self):
        cases = [
            ({"risk": "high"}, "'risk' must be one of normal, elevated"),
            ({"negative_paths": ["unauthorized"]}, "'negative_paths' must be a non-empty object"),
            ({"negative_paths": {}}, "'negative_paths' must be a non-empty object"),
            ({"negative_paths": {"sql_injection": "x"}}, "unknown dimension(s) sql_injection"),
            ({"negative_paths": {"duplicate": ""}}, "'negative_paths.duplicate' must be a non-empty string"),
            ({"negative_paths": {"duplicate": 7}}, "'negative_paths.duplicate' must be a non-empty string"),
            ({"negative_paths": {"duplicate": "x" * 513}}, "at most 512 characters"),
        ]
        for fields, needle in cases:
            with self.subTest(fields=str(fields)[:60]):
                problems = lib.validate_criterion_fields(fields)
                self.assertTrue(any(needle in problem for problem in problems), problems)
        self.assertEqual(lib.validate_criterion_fields({"risk": "elevated", "negative_paths": FULL}), [])
        self.assertEqual(lib.validate_criterion_fields({"risk": None, "negative_paths": None}), [])

    def test_invalid_values_are_refused_on_every_write_path(self):
        self._refused(self._add("REQ-002", "--risk", "elevated", "--negative-path", "sql_injection=x"),
                      "unknown dimension(s) sql_injection")
        self._refused(self._add("REQ-002", "--negative-path", "no-equals-sign"), "must be DIMENSION=TEXT")
        self._refused(self._add("REQ-002", "--negative-path", "duplicate=a", "--negative-path", "duplicate=b"),
                      "repeats dimension duplicate")
        self._refused(self._add("REQ-002", "--negative-path", "duplicate="),
                      "'negative_paths.duplicate' must be a non-empty string")
        self._refused(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "x",
                           "--verification", "automated", "--test", self.command, "--risk", "high"], cwd=self.tmp),
                      "invalid choice")
        self.assertNotIn("REQ-002", [c["id"] for c in self.read_acceptance()["criteria"]])
        self._refused(run(["criterion-update", "REQ-001", "--negative-path", "rollback=x",
                           "--no-negative-paths"], cwd=self.tmp), "not both")
        cfg = lib.load_config(self.tmp)
        bad = [{"op": "add", "criterion": {"id": "REQ-009", "type": "supporting", "requirement": "x",
                                           "verification": "automated", "tests": [self.command],
                                           "risk": "elevated", "negative_paths": {"bogus": "x"}}}]
        with self.assertRaisesRegex(lib.CriteriaTransactionError, "unknown dimension"):
            lib.plan_criteria_transaction(self.read_acceptance(), cfg, bad, root=self.tmp)

    def test_the_transaction_path_stores_and_clears_both_fields(self):
        cfg = lib.load_config(self.tmp)
        add = [{"op": "add", "criterion": {"id": "REQ-009", "type": "supporting", "requirement": "x",
                                           "verification": "automated", "tests": [self.command],
                                           "risk": "elevated", "negative_paths": dict(FULL)}}]
        plan = lib.plan_criteria_transaction(self.read_acceptance(), cfg, add, root=self.tmp)
        added = next(c for c in plan["criteria_after"] if c["id"] == "REQ-009")
        self.assertEqual((added["risk"], added["negative_paths"]), ("elevated", FULL))
        acceptance = self.read_acceptance()
        acceptance["criteria"].append({**added})
        clear = [{"op": "update", "id": "REQ-009", "fields": {"risk": None, "negative_paths": None}}]
        plan = lib.plan_criteria_transaction(acceptance, cfg, clear, root=self.tmp)
        cleared = next(c for c in plan["criteria_after"] if c["id"] == "REQ-009")
        self.assertNotIn("risk", cleared)
        self.assertNotIn("negative_paths", cleared)

    def test_risk_and_the_matrix_are_part_of_the_spec_hash(self):
        base = {"id": "REQ-002", "type": "supporting", "requirement": "x", "verification": "automated",
                "tests": ["true"], "evidence": [], "state": "not_tested"}
        elevated = {**base, "risk": "elevated", "negative_paths": dict(FULL)}
        changed = {**elevated, "negative_paths": {**FULL, "duplicate": "test_a_different_case"}}
        hashes = {lib.criterion_spec_hash(c) for c in (base, elevated, changed)}
        self.assertEqual(len(hashes), 3)
        # a matrix change is a respecification: evidence resets
        self._ok(self._add("REQ-002", "--risk", "elevated", *matrix_args(FULL)))
        self._ok(run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp))
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")
        self._ok(run(["criterion-update", "REQ-002", "--negative-path", "duplicate=test_a_different_case"],
                     cwd=self.tmp))
        self.assertEqual((self._criterion("REQ-002")["state"], self._criterion("REQ-002")["evidence"]),
                         ("not_tested", []))


class TestLegacyCriteriaUnchanged(NegativePathFixture):
    def test_a_criterion_without_the_fields_stores_nothing_and_passes_the_gate(self):
        self._ok(self._add("REQ-002"))
        criterion = self._criterion("REQ-002")
        self.assertNotIn("risk", criterion)
        self.assertNotIn("negative_paths", criterion)
        # risk normal is the default and is never stored
        self._ok(self._add("REQ-003", "--risk", "normal"))
        self.assertNotIn("risk", self._criterion("REQ-003"))
        self.assertEqual(lib.negative_path_errors(self.read_acceptance()), [])
        self._ok(self.advance_to(3))

    def test_a_normal_criterion_with_a_partial_matrix_is_not_gated(self):
        self._ok(self._add("REQ-002", "--negative-path", "duplicate=not_applicable"))
        self.assertEqual(lib.negative_path_errors(self.read_acceptance()), [])
        self._ok(self.advance_to(3))

    def test_update_risk_normal_clears_elevated(self):
        self._ok(self._add("REQ-002", "--risk", "elevated"))
        self._ok(run(["criterion-update", "REQ-002", "--risk", "normal"], cwd=self.tmp))
        self.assertNotIn("risk", self._criterion("REQ-002"))
        self._ok(self.advance_to(3))


class TestDashboardShowsTheMatrix(NegativePathFixture):
    def test_the_snapshot_carries_the_matrix(self):
        import handsoff_dashboard as dashboard
        self._ok(self._add("REQ-002", "--risk", "elevated", *matrix_args(FULL)))
        snapshot = dashboard.build_snapshot(self.tmp)
        row = next(c for c in snapshot["acceptance"]["criteria"] if c["id"] == "REQ-002")
        self.assertEqual((row["risk"], row["negative_paths"]), ("elevated", FULL))

    @guard
    def test_the_board_renders_every_dimension(self):
        app = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("${negativePathMatrix(criterion)}", app)
        for dimension in workflow.NEGATIVE_PATH_DIMENSIONS:
            self.assertIn(f'"{dimension}"', app)

    @guard
    def test_the_json_schema_matches_the_validator(self):
        schema = json.loads((ROOT / "schemas" / "acceptance.schema.json").read_text())
        properties = schema["properties"]["criteria"]["items"]["properties"]
        self.assertEqual(properties["risk"]["enum"], list(workflow.CRITERION_RISKS))
        self.assertEqual(list(properties["negative_paths"]["properties"]), list(workflow.NEGATIVE_PATH_DIMENSIONS))
        for dimension in workflow.NEGATIVE_PATH_DIMENSIONS:
            self.assertEqual(properties["negative_paths"]["properties"][dimension]["maxLength"],
                             workflow.MAX_NEGATIVE_PATH_CHARS)


class StubLedger:
    """The fixed handsoff_defects interface, held in memory, so the
    supervisor's REQ-001 wiring is exercised on its own; the ledger itself
    is tests.test_escaped_defects' subject."""

    def __init__(self):
        self.open = [{"id": "D-0001", "issue": "#431", "summary": "lost retry", "control": "test_selection",
                      "paths": ["bin/*.py"], "state": "open", "decisions": []}]
        self.calls = []

    def inherited_regressions(self, root, acceptance, run_id):
        self.calls.append(("inherited", run_id))
        cited = " ".join(str(c.get("requirement")) for c in acceptance.get("criteria", []))
        return [d for d in self.open if d["id"] not in cited
                and not any(x.get("run_id") == run_id for x in d["decisions"])]

    def decide_defect(self, root, defect_id, action, reason, by, run_id=None):
        self.calls.append((action, defect_id, reason, by, run_id))
        defect = next(d for d in self.open if d["id"] == defect_id)
        defect["decisions"].append({"action": action, "run_id": run_id, "reason": reason, "by": by})
        return defect

    def record_defect(self, root, fields):
        self.calls.append(("record", fields))
        return {"id": "D-0002", **fields}

    def load_defects(self, root):
        return list(self.open)


class TestPhase3DefectGateWiring(NegativePathFixture):
    """REQ-001 wiring: advance 3 refuses an unresolved inherited regression
    until it is adopted or declined for this run; status lists it; close is
    refused before an adopting run reaches Phase 8; the ledger file is
    outside the repository digest."""

    def _call(self, argv):
        import contextlib
        import io
        import handsoff_supervisor as supervisor
        args = supervisor.build_parser().parse_args(["--root", str(self.tmp), *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = {"advance": supervisor.cmd_advance, "defect": supervisor.cmd_defect,
                    "status": supervisor.cmd_status}[argv[0]](args)
        return code, out.getvalue()

    def _to_phase_2_approved(self):
        self._ok(self.advance_to(2))
        self._ok(run(["record-design-review", "--by", "test-design-reviewer", "--architect", "test-architect",
                      "--approve", "--summary", "Test-fixture independent design review"], cwd=self.tmp))
        self._ok(run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                      "--summary", "Test-fixture design approval"], cwd=self.tmp))

    def test_advance_3_refuses_then_clears_by_a_reasoned_decline(self):
        from unittest import mock
        import handsoff_supervisor as supervisor
        self._to_phase_2_approved()
        stub = StubLedger()
        with mock.patch.object(supervisor, "defects", stub):
            code, out = self._call(["advance", "3", "30"])
            self.assertEqual(code, 1, out)
            self.assertIn("defect gate: inherited regression D-0001 (#431: lost retry) overlaps this run", out)
            self.assertIn("clears with: handsoff_supervisor.py defect decline --id D-0001 --reason TEXT --by ACTOR",
                          out)
            code, out = self._call(["status"])
            self.assertEqual([d["id"] for d in json.loads(out)["inherited_regressions"]], ["D-0001"])
            code, out = self._call(["defect", "decline", "--id", "D-0001", "--reason", "out of scope", "--by", "pilot"])
            self.assertEqual(code, 0, out)
            status = self.read_status()
            events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
            run_id = "run-" + lib.feature_hash(status, events)[:16]
            self.assertIn(("decline", "D-0001", "out of scope", "pilot", run_id), stub.calls)
            self.assertIn(("inherited", run_id), stub.calls)
            self.assertEqual(events[-1]["kind"], "defect_declined")
            code, out = self._call(["advance", "3", "30"])
            self.assertEqual(code, 0, out)

    def test_advance_3_clears_by_adoption_and_close_waits_for_phase_8(self):
        from unittest import mock
        import handsoff_supervisor as supervisor
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement",
                      "Adopts D-0001: the retry is restored", "--verification", "automated",
                      "--test", self.command], cwd=self.tmp))
        self._to_phase_2_approved()
        stub = StubLedger()
        with mock.patch.object(supervisor, "defects", stub):
            code, out = self._call(["advance", "3", "30"])
            self.assertEqual(code, 0, out)
            self.assertTrue(supervisor.defect_cited(self.read_acceptance(), "D-0001"))
            self.assertFalse(supervisor.defect_cited(self.read_acceptance(), "D-000"))
            code, out = self._call(["defect", "close", "--id", "D-0001", "--by", "pilot"])
            self.assertEqual(code, 1, out)
            self.assertIn("closes only once a run that adopted it", out)
            self.assertFalse([call for call in stub.calls if call[0] == "close"])

    def test_record_passes_the_fields_and_an_absent_module_refuses(self):
        from unittest import mock
        import handsoff_supervisor as supervisor
        stub = StubLedger()
        with mock.patch.object(supervisor, "defects", stub):
            code, out = self._call(["defect", "record", "--issue", "#432", "--control", "implementation",
                                    "--summary", "s", "--regression", "r", "--path", "src/*.py", "--by", "host"])
            self.assertEqual(code, 0, out)
        self.assertEqual(stub.calls[-1], ("record", {"issue": "#432", "control": "implementation", "summary": "s",
                                                    "regression": "r", "paths": ["src/*.py"],
                                                    "recorded_by": "host"}))
        with mock.patch.object(supervisor, "defects", None):
            code, out = self._call(["defect", "list"])
            self.assertEqual(code, 1)
            self.assertIn("not installed", out)
            self.assertEqual(supervisor.defect_gate_errors(self.tmp, lib.load_config(self.tmp),
                                                           self.read_status(), self.read_acceptance()), [])

    def test_the_ledger_file_is_outside_the_repository_digest(self):
        cfg = lib.load_config(self.tmp)
        before = lib.repository_digest(self.tmp, cfg)
        (self.tmp / "handsoff-defects.jsonl").write_text('{"id": "D-0001"}\n')
        self.assertEqual(lib.repository_digest(self.tmp, cfg), before)
        self.assertIn("handsoff-defects.jsonl", lib.HANDSOFF_GENERATED_NAMES)


if __name__ == "__main__":
    unittest.main()
