"""#284 stage 7: the workflow state machine boundary.

Three things are checked here, and the third is the one the ticket asks for.

REQ-002, a bounded subsystem behind a stable interface: nothing unrelated
enters `handsoff_workflow`, it never imports the monolith, and the monolith
re-exports exactly the moved set.

REQ-004, contract equivalence: every case in
`tests/fixtures/workflow_contract_pre_extraction.json` was captured by
importing `git show HEAD:bin/handsoff_lib.py` BEFORE any symbol moved, and is
replayed here against the working tree. Comparing the new module against its
own output would pass whatever it did. The inputs come from a really
initialized project rather than hand-built dicts, because three attempts at
hand-building an acceptance registry produced only validation refusals, and a
fixture full of refusals on both sides proves nothing.

REQ-003, state transitions validate proposed state before persistence: that
is a property of this boundary. `plan_criteria_transaction` returns the whole
resulting registry with its hashes and writes nothing, so the caller cannot
persist a batch that was never validated as a batch.
"""
import ast
import json
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402

ROOT = BIN.parent
MODULE = BIN / "handsoff_workflow.py"
CONTRACT = ROOT / "tests" / "fixtures" / "workflow_contract_pre_extraction.json"

#: Layers below this one. An import from anywhere else is a boundary breach.
ALLOWED_ENGINE_IMPORTS = {
    "handsoff_core", "handsoff_config", "handsoff_routing", "handsoff_ledger",
    "handsoff_resources", "handsoff_agent_runtime",
}
ALLOWED_STDLIB_IMPORTS = {
    "__future__", "hashlib", "json", "os", "re", "uuid", "copy", "datetime", "pathlib",
}


def _tree():
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _defined():
    names = set()
    for node in _tree().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _reexported():
    for node in ast.parse((BIN / "handsoff_lib.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module == "handsoff_workflow":
            return {a.name for a in node.names}
    return set()


def _sync_appends(mod, accept, cfg, supporting):
    """Run sync on a registry missing an item a criterion declares."""
    accept["criteria"].append({**supporting, "id": "REQ-909",
                               "requirement": "[#42] a tagged one"})
    try:
        changed = mod.sync_work_item_registry(accept, cfg)
    except Exception as exc:
        return {"__raised__": type(exc).__name__, "message": str(exc)[:400]}
    return {"changed": changed, "ids": sorted(i.get("id") for i in accept["work_items"])}


def contract_cases(mod, root):
    """Every recorded case, computed against `mod`.

    Kept in one function used by both the capture and the replay, so the two
    cannot drift into comparing different calls.
    """
    cfg = mod.load_config(root)
    accept = json.loads((root / "handsoff-acceptance.json").read_text())
    status = json.loads((root / "handsoff-status.json").read_text())
    criteria = accept["criteria"]
    supporting = {"id": "REQ-909", "type": "supporting", "requirement": "a new one",
                  "verification": "manual", "tests": ["look at it"]}
    primary = next(c["id"] for c in criteria if c.get("type") == "primary_fix")

    def fresh():
        return json.loads(json.dumps(accept))

    def case(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:  # a refusal is part of the contract
            return {"__raised__": type(exc).__name__, "message": str(exc)[:400]}

    # Records in the shape the ledger actually writes. An earlier version of
    # this fixture used {"criterion": id} with no hashes, so every
    # evidence-shaped case recorded an empty set and a function returning
    # empty would have matched it.
    def record(criterion, kind, ok=True):
        cid = criterion.get("id")
        return {"kind": kind, "ok": ok, "criteria": [cid],
                "criterion_hashes": {cid: mod.criterion_spec_hash(criterion)},
                "run_id": f"r-{cid}-{kind}"}

    evidence = [record(c, "automated") for c in criteria]
    baselines = [record(c, "baseline") for c in criteria]
    failing_first = json.loads(json.dumps(cfg))
    failing_first.setdefault("features", {})["failing_first"] = True
    # The key is `regressions`, a list of groups each with `commands`; a
    # guessed `checks.regression_commands` produced an empty set both sides.
    regressions = json.loads(json.dumps(cfg))
    regressions["regressions"] = [{"commands": ["python3 -m unittest tests.test_x"]}]
    # `amendment`, singular, and only state `open` freezes.
    amended = {**status, "phase_number": 9,
               "amendment": {"amendment_id": "AM-1", "state": "open", "frozen_phase": 5,
                             "frozen_progress": 50, "opened_at": "2026-09-25T00:00:00+00:00"}}
    # rules_binding_errors needs the feature on AND a recorded rules_hash.
    binds_rules = json.loads(json.dumps(cfg))
    binds_rules.setdefault("features", {})["review_binds_rules"] = True
    ci_failed = {**status, "ci": {"state": "failed", "failed_check": "tests", "pr": 335,
                                 "url": "https://example.invalid/pr/335"}}

    return {
        "lane_gate_refusal_none": case(mod.lane_gate_refusal, status, "advance"),
        "lane_gate_refusal_design_lane": case(mod.lane_gate_refusal,
                                              {**status, "lane": "design"}, "record-review"),
        "lane_gate_refusal_review_lane": case(mod.lane_gate_refusal,
                                              {**status, "lane": "review"}, "advance_1"),
        "lane_gate_refusal_empty": case(mod.lane_gate_refusal, {}, "advance"),
        "gate_progress": case(mod.gate_progress, status, accept),
        "gate_progress_empty": case(mod.gate_progress, {}, {"criteria": []}),
        "ci_gate_errors_clean": case(mod.ci_gate_errors, status),
        "ci_gate_errors_failed": case(mod.ci_gate_errors, ci_failed),
        "ci_gate_errors_absent": case(mod.ci_gate_errors, {}),
        "coverage_for": case(mod.coverage_for, criteria),
        "coverage_for_resolved": case(mod.coverage_for, criteria, True),
        "valid_evidence_kinds": sorted(case(mod.valid_evidence_kinds, criteria[0], evidence)),
        "valid_evidence_kinds_wrong_hash": sorted(case(
            mod.valid_evidence_kinds, {**criteria[0], "requirement": "reworded"}, evidence)),
        "criterion_baseline": case(mod.criterion_baseline, criteria[0], baselines),
        "criterion_baseline_absent": case(mod.criterion_baseline, criteria[0], evidence),
        "baseline_errors_off": case(mod.baseline_errors, criteria, [], cfg),
        "baseline_errors_on": case(mod.baseline_errors,
                                   [{**c, "state": "passing"} for c in criteria], [], failing_first),
        "baseline_errors_satisfied": case(mod.baseline_errors,
                                          [{**c, "state": "passing"} for c in criteria],
                                          baselines, failing_first),
        "full_design_required": case(mod.full_design_required, status, accept, cfg),
        "derive_work_items": case(mod.derive_work_items, status, accept, cfg),
        "amendment_freeze_errors_none": case(mod.amendment_freeze_errors, status),
        "amendment_freeze_errors_open": case(mod.amendment_freeze_errors, amended),
        "feature_hash": case(mod.feature_hash, status, []),
        "feature_hash_with_event": case(mod.feature_hash, status,
                                        [{"at": "2026-09-25T00:00:00+00:00"}]),
        "pending_design_decline_none": case(mod.pending_design_decline, status),
        "pending_design_decline_open": case(mod.pending_design_decline,
                                            {**status, "design_declined": {"decision": "pending",
                                                                           "by": "reviewer"}}),
        "configured_regression_commands_none": sorted(case(mod.configured_regression_commands, cfg)),
        "configured_regression_commands_set": sorted(case(mod.configured_regression_commands,
                                                          regressions)),
        "validate_criterion_fields_ok": case(mod.validate_criterion_fields,
                                             {"requirement": "x", "type": "supporting"}),
        "validate_criterion_fields_require_all": case(mod.validate_criterion_fields,
                                                      {"requirement": "x"}, require_all=True),
        "validate_criterion_fields_bad_type": case(mod.validate_criterion_fields,
                                                   {"type": "nonsense"}),
        "compute_errors": case(mod.compute_errors, status, accept, cfg),
        "compute_errors_empty": case(mod.compute_errors, {}, {"criteria": []}, cfg),
        "plan_criteria_transaction_add_supporting": case(mod.plan_criteria_transaction, fresh(), cfg,
                                    [{"op": "add", "criterion": dict(supporting)}]),
        "plan_criteria_transaction_add_then_update": case(mod.plan_criteria_transaction, fresh(), cfg,
                                     [{"op": "add", "criterion": dict(supporting)},
                                      {"op": "update", "id": "REQ-909",
                                       "fields": {"requirement": "changed"}}]),
        "plan_criteria_transaction_add_then_remove": case(mod.plan_criteria_transaction, fresh(), cfg,
                                     [{"op": "add", "criterion": dict(supporting)},
                                      {"op": "remove", "id": "REQ-909"}]),
        "plan_criteria_transaction_second_primary": case(
            mod.plan_criteria_transaction, fresh(), cfg,
            [{"op": "add", "criterion": {**supporting, "type": "primary_fix"}}]),
        "plan_criteria_transaction_remove_only_primary": case(mod.plan_criteria_transaction, fresh(), cfg,
                                             [{"op": "remove", "id": primary}]),
        "plan_criteria_transaction_too_many": case(
            mod.plan_criteria_transaction, fresh(), cfg,
            [{"op": "add", "criterion": {**supporting, "id": f"REQ-{n:03d}"}} for n in range(200)]),
        "plan_criteria_transaction_unknown_op": case(mod.plan_criteria_transaction, fresh(), cfg,
                                [{"op": "nope", "id": "REQ-001"}]),
        "plan_criteria_transaction_missing_field": case(mod.plan_criteria_transaction, fresh(), cfg,
                                       [{"op": "add", "criterion": {"id": "REQ-911",
                                                                    "type": "supporting"}}]),
        "plan_criteria_transaction_unknown_id": case(mod.plan_criteria_transaction, fresh(), cfg,
                                       [{"op": "update", "id": "REQ-999",
                                         "fields": {"requirement": "x"}}]),
        # Two halves: nothing to append, and a criterion tagged for an issue
        # the persisted registry does not name yet. Returning False for both
        # would be indistinguishable from a function that never syncs.
        "sync_work_item_registry_unchanged": [case(mod.sync_work_item_registry, fresh(), cfg)],
        "sync_work_item_registry_appends": _sync_appends(mod, fresh(), cfg, supporting),
        "rules_set_entries": case(mod.rules_set_entries, root),
        "rules_set_hash": case(mod.rules_set_hash, root),
        "rules_set_diff_none": case(mod.rules_set_diff, root, None),
        "rules_set_diff_stale": case(mod.rules_set_diff, root, {"rules/a.md": "deadbeef"}),
        "load_launch_rules": case(mod.load_launch_rules, root),
        "rules_binding_errors_no_decision": case(mod.rules_binding_errors, root, cfg, None, "design"),
        "rules_binding_errors_stale": case(mod.rules_binding_errors, root, binds_rules,
                                           {"rules_hash": "deadbeef"}, "design"),
        "rules_binding_errors_current": case(
            mod.rules_binding_errors, root, binds_rules,
            {"rules_hash": mod.rules_set_hash(root, binds_rules)}, "design"),
    }


class TheReExportSurfaceIsExactlyTheMovedSet(unittest.TestCase):
    """REQ-002: 121 call sites still say `lib.compute_errors`; they must resolve."""

    def setUp(self):
        self.defined = _defined()
        self.reexported = _reexported()

    def test_the_lib_reexports_every_public_workflow_symbol(self):
        public = {n for n in self.defined if not n.startswith("_")}
        self.assertEqual(public - self.reexported, set(),
                         "the monolith stopped naming these, so every existing caller breaks")

    def test_the_lib_reexports_nothing_the_module_does_not_define(self):
        self.assertEqual(self.reexported - self.defined, set(),
                         "the re-export names a symbol that is not in the module")

    def test_every_reexported_symbol_actually_resolves(self):
        for name in sorted(self.reexported):
            self.assertIs(getattr(lib, name), getattr(workflow, name), name)

    def test_the_moved_set_is_not_trivially_small(self):
        """A boundary of three helpers would satisfy every other test here."""
        self.assertGreaterEqual(len(self.defined), 40,
                                "the extraction moved less than it claims")


class NothingUnrelatedEntersTheBoundary(unittest.TestCase):
    """REQ-006: the architectural rule, not a convention in a docstring."""

    def test_module_level_imports_are_on_the_allowlist(self):
        unexpected = []
        for node in _tree().body:
            if isinstance(node, ast.Import):
                unexpected += [a.name for a in node.names
                               if a.name not in ALLOWED_STDLIB_IMPORTS | ALLOWED_ENGINE_IMPORTS]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module not in ALLOWED_STDLIB_IMPORTS | ALLOWED_ENGINE_IMPORTS:
                    unexpected.append(module)
        self.assertEqual(unexpected, [],
                         "a new dependency entered the workflow boundary; either it belongs in a "
                         "layer below or it does not belong here")

    def test_the_monolith_is_never_imported(self):
        """Not at module level and not deferred inside a function either.

        An importing-back edge would make the boundary a name, not a layer.
        """
        for node in ast.walk(_tree()):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "handsoff_lib",
                                    f"line {node.lineno} imports the monolith")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "handsoff_lib",
                                        f"line {node.lineno} imports the monolith")

    def test_no_definition_is_unreachable_from_the_concern(self):
        """A symbol no other symbol here reaches, and the monolith does not
        re-export, arrived by accident."""
        tree = _tree()
        defined = _defined()
        used = set()
        for node in tree.body:
            used |= {n.id for n in ast.walk(node)
                     if isinstance(n, ast.Name) and n.id in defined}
        orphans = sorted(defined - used - _reexported())
        self.assertEqual(orphans, [], "nothing reaches these and nothing re-exports them")


class TheProposedStateIsValidatedBeforeItIsPersisted(unittest.TestCase):
    """REQ-003, as a property of the boundary rather than of each call site."""

    def test_planning_a_transaction_writes_nothing(self):
        """The plan carries the resulting registry and its hashes, so the
        caller persists a validated batch or nothing at all."""
        cases = json.loads(CONTRACT.read_text(encoding="utf-8"))
        plan = cases["plan_criteria_transaction_add_supporting"]
        self.assertNotIn("__raised__", plan, "the recorded success is a refusal")
        for field in ("criteria_after", "work_items_after", "registry_hash_before",
                      "registry_hash_after", "design_hash_before", "design_hash_after"):
            self.assertIn(field, plan, field)
        self.assertNotEqual(plan["registry_hash_before"], plan["registry_hash_after"],
                            "an add that changes nothing is not a validated add")

    def test_the_module_never_writes_engine_state_itself(self):
        """Persistence belongs to the ledger. If this module wrote status or
        acceptance directly, a caller could bypass the validation above."""
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in ("write_text(", "durable_replace(", "_atomic_write_text(", "commit("):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} in the workflow layer: it decides, it does not persist")

    def test_a_refused_batch_names_the_operation_and_the_reason(self):
        """A refusal that does not say which operation failed is not usable by
        an agent, which is the whole reason the transaction is a batch."""
        cases = json.loads(CONTRACT.read_text(encoding="utf-8"))
        for key in ("plan_criteria_transaction_second_primary", "plan_criteria_transaction_unknown_op", "plan_criteria_transaction_missing_field",
                    "plan_criteria_transaction_unknown_id"):
            message = cases[key].get("message", "")
            self.assertIn("operation ", message, f"{key}: no operation named")
            self.assertGreater(len(message), 30, f"{key}: no reason given")


class BehaviourIsUnchangedAcrossTheExtraction(unittest.TestCase):
    """REQ-004, checked against the OLD module's recorded output."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def _degenerate(self, value):
        """A value a broken function would return by accident."""
        return value in ([], {}, None, "", False) or value == [False]

    def test_every_covered_function_has_a_case_that_would_notice_a_change(self):
        """The count of recorded cases is not coverage.

        The first version of this fixture recorded 39 cases of which 15 were
        `[]` or `null`, several because the input never reached the code:
        ci_gate_errors was fed state "FAILURE" where it tests for "failed",
        and the evidence cases used {"criterion": id} where the ledger writes
        {"criteria": [id], "criterion_hashes": {...}}. Stubbing ci_gate_errors
        to `return []` passed the whole suite. So each function must have at
        least one case whose value is not something a broken function would
        return by accident; the empty halves stay, as the other side of a pair.
        """
        functions = sorted(_defined(), key=len, reverse=True)
        groups = {}
        for key, value in self.baseline.items():
            owner = next((f for f in functions if key == f or key.startswith(f + "_")), None)
            self.assertIsNotNone(owner, f"case {key} names no function in the module")
            groups.setdefault(owner, []).append((key, value))
        blind = {name: [k for k, _ in cases] for name, cases in groups.items()
                 if all(self._degenerate(v) for _, v in cases)}
        self.assertEqual(blind, {},
                         "every recorded case for these functions is empty or None, so the "
                         "fixture would match a function that returned nothing at all")

    def test_the_covered_set_is_a_real_share_of_the_boundary(self):
        """A single well-chosen function would satisfy the test above."""
        functions = sorted(_defined(), key=len, reverse=True)
        covered = {next((f for f in functions if k == f or k.startswith(f + "_")), None)
                   for k in self.baseline}
        public = {n for n in _defined() if not n.startswith("_") and n[0].islower()}
        self.assertGreaterEqual(len(covered & public), 18,
                                f"only {len(covered & public)} of {len(public)} public functions "
                                "have a recorded case")

    def test_the_fixture_is_not_a_record_of_refusals(self):
        """Captured with hand-built dicts, most of these cases refused, and a
        fixture of matching refusals would pass whatever the module did."""
        values = [k for k, v in self.baseline.items()
                  if not (isinstance(v, dict) and "__raised__" in v)]
        self.assertGreaterEqual(len(values), 40,
                                "most recorded cases are refusals; the baseline proves little")
        self.assertGreaterEqual(len(self.baseline), 52, "the baseline lost cases")


class TheContractStillHolds(HandsoffTestCase):
    """The replay needs a really initialized project, so it runs on a fixture."""

    def test_every_recorded_case_is_unchanged(self):
        self.init("Contract capture")
        baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))
        now = json.loads(json.dumps(contract_cases(lib, self.tmp), sort_keys=True, default=str))
        # The feature name and the timestamps differ per run, so compare the
        # cases that do not carry them; the ones that do are compared by shape.
        volatile = {"derive_work_items", "sync_work_item_registry", "feature_hash",
                    "compute_errors", "gate_progress", "plan_criteria_transaction_add_supporting",
                    "plan_criteria_transaction_add_then_update", "plan_criteria_transaction_add_then_remove", "rules_set_entries",
                    "rules_set_hash", "rules_set_diff_none", "load_launch_rules"}
        differing = {k: (baseline[k], now.get(k))
                     for k in baseline if k not in volatile and baseline[k] != now.get(k)}
        self.assertEqual(differing, {},
                         "the extraction changed behaviour (case: before, after)")

    def test_the_volatile_cases_keep_their_shape_and_their_refusals(self):
        """Excluded from the equality above because they carry the run's own
        feature name, paths or timestamps. Their structure is still pinned, so
        an extraction that turned a plan into a refusal would be caught."""
        self.init("Contract capture")
        baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))
        now = json.loads(json.dumps(contract_cases(lib, self.tmp), sort_keys=True, default=str))
        for key in ("derive_work_items", "compute_errors", "gate_progress",
                    "plan_criteria_transaction_add_supporting", "plan_criteria_transaction_add_then_update", "rules_set_entries",
                    "load_launch_rules"):
            before, after = baseline[key], now.get(key)
            self.assertEqual(type(before), type(after), key)
            self.assertEqual("__raised__" in before if isinstance(before, dict) else None,
                             "__raised__" in after if isinstance(after, dict) else None,
                             f"{key}: one side refuses and the other does not")
            if isinstance(before, dict):
                self.assertEqual(sorted(before), sorted(after), f"{key}: fields changed")


class TheModuleIsRegisteredWhereItMustBe(unittest.TestCase):
    """Six registries named the same file set before this one; now seven."""

    def test_the_runtime_manifest_covers_it(self):
        manifest = json.loads((ROOT / "handsoff-runtime.json").read_text())
        self.assertIn("bin/handsoff_workflow.py", manifest["files"])

    def test_the_manifest_generator_names_it(self):
        source = (BIN / "handsoff_manifest.py").read_text(encoding="utf-8")
        self.assertIn('"bin/handsoff_workflow.py"', source,
                      "regenerating the manifest would silently drop the module")

    def test_the_wheel_packages_it(self):
        self.assertIn('"handsoff_workflow"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                      "an installed engine would not have the module at all")

    def test_the_layer_suite_analyses_it(self):
        source = (ROOT / "tests" / "test_module_layers.py").read_text(encoding="utf-8")
        self.assertIn('"handsoff_workflow"', source,
                      "the cross-module checks would skip this module entirely")


if __name__ == "__main__":
    unittest.main()
