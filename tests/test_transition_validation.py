"""#284 criterion 3: state transitions validate proposed state before persistence.

**This criterion is now met structurally, and this module asserts the
property.** It previously recorded a ratchet instead, because the criterion
did not hold: measured across `handsoff_lib`, `handsoff_supervisor` and
`handsoff_ledger`, 71 functions committed a status, 55 validated the proposed
state first and 16 did not. Validating at 16 more call sites would have made
the count zero without making the property true, because nothing stopped a
seventeenth.

What made it structural was a layering change, not a rule. The schema
validators sat in `handsoff_agent_runtime`, ABOVE the ledger, so only a caller
could reach them. Their transitive closure turned out to be 108 symbols with
no reference back into the runtime and only two dependencies on the ledger,
both plain constants. Moving those constants to `handsoff_config` and the
closure to `handsoff_schema`, below the ledger, let `commit` call them itself.

The risk was measured before the change rather than argued about: a temporary
probe inside `commit` reported every status it was about to write that failed
validation, across the whole suite. Twenty-one, all from fixtures constructing
a malformed document on purpose. Those fixtures now write the file directly,
which is also how a corrupt file really arrives -- never through a recorded
transition.

A first attempt at the original measurement looked only for
`validate_status_schema` and reported 56 unvalidated, which would have been a
false alarm: `cmd_advance` validates through `compute_errors` and the gate
helpers, not the schema validator. A narrow query is evidence about the query.
"""
import ast
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


def _function(module, name):
    src = (BIN / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return src, fn


class TheLedgerValidatesBeforeItPersists(unittest.TestCase):
    """The property, at the one place every transition goes through."""

    def setUp(self):
        self.src, self.fn = _function("handsoff_ledger", "commit")

    def _line_of(self, *needles):
        """First line inside commit calling any of `needles`."""
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                if name in needles:
                    return node.lineno
        return None

    def test_commit_validates_the_status_and_the_acceptance_registry(self):
        body = ast.get_source_segment(self.src, self.fn) or ""
        self.assertIn("validate_status_schema", body)
        self.assertIn("validate_acceptance_schema", body)

    def test_it_validates_before_it_writes_anything(self):
        """Order is the whole claim. Validating after the write would leave
        the invalid document on disk and the journal describing it."""
        validated = self._line_of("validate_status_schema", "validate_acceptance_schema")
        wrote = self._line_of("write_ahead", "atomic_write_json", "append_event")
        self.assertIsNotNone(validated, "commit does not validate at all")
        self.assertIsNotNone(wrote, "commit does not write at all; the matcher is broken")
        self.assertLess(validated, wrote,
                        "commit validates after it has already written")

    def test_the_validators_live_below_the_ledger(self):
        """The layering is what makes this possible; an import from above
        would be a cycle and the property would have to go back to callers."""
        imported_from = {}
        for node in ast.parse(self.src).body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_from[alias.name] = node.module
        for name in ("validate_status_schema", "validate_acceptance_schema"):
            self.assertEqual(imported_from.get(name), "handsoff_schema", name)


class NoTransitionCanPersistAnUncheckedStatus(HandsoffTestCase):
    """Behaviour, not source shape."""

    def test_an_invalid_status_is_refused_and_nothing_is_written(self):
        self.init("Transition validation")
        cfg = lib.load_config(self.tmp)
        path = lib.status_path(self.tmp, cfg)
        before = path.read_text(encoding="utf-8")
        broken = json.loads(before)
        broken.pop("feature")
        with self.assertRaises(lib.HandsoffError) as caught:
            with lib.project_lock(self.tmp):
                lib.commit(self.tmp, cfg, status=broken,
                           event_kind="fixture", event_message="should not land")
        self.assertIn("refusing to persist an invalid status", str(caught.exception))
        self.assertIn("feature", str(caught.exception),
                      "the refusal must name the field so it is actionable")
        self.assertEqual(path.read_text(encoding="utf-8"), before,
                         "the status file changed despite the refusal")

    def test_an_invalid_acceptance_registry_is_refused(self):
        self.init("Transition validation")
        cfg = lib.load_config(self.tmp)
        path = lib.acceptance_path(self.tmp, cfg)
        before = path.read_text(encoding="utf-8")
        broken = json.loads(before)
        broken["criteria"] = "not a list"
        with self.assertRaises(lib.HandsoffError) as caught:
            with lib.project_lock(self.tmp):
                lib.commit(self.tmp, cfg, acceptance=broken,
                           event_kind="fixture", event_message="should not land")
        self.assertIn("refusing to persist an invalid acceptance registry", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_valid_status_still_commits(self):
        """Guards against a gate that refuses everything, which would pass
        both tests above and break the engine."""
        self.init("Transition validation")
        cfg = lib.load_config(self.tmp)
        status = json.loads(lib.status_path(self.tmp, cfg).read_text(encoding="utf-8"))
        status["next_action"] = "carry on"
        with lib.project_lock(self.tmp):
            event = lib.commit(self.tmp, cfg, status=status,
                               event_kind="fixture", event_message="valid write")
        self.assertTrue(event)
        written = json.loads(lib.status_path(self.tmp, cfg).read_text(encoding="utf-8"))
        self.assertEqual(written["next_action"], "carry on")


class CommitIsTheOnlyWayAStatusReachesTheDisk(unittest.TestCase):
    """Validation at `commit` only covers every transition if `commit` is the
    only writer. A second path would reopen the gap silently.

    Matching `atomic_write_json(status_path(...))` literally would miss a
    write through a local variable, so the whole inventory of JSON writes is
    pinned instead: every call site with the expression it writes to. A new
    one, however it spells its target, has to be added here and looked at.
    """

    #: module.function -> the first argument's source, for every
    #: atomic_write_json call in bin/. Only the two in `commit` name the run's
    #: status and acceptance documents; the rest write registries, journals,
    #: caches and in-flight records that are not the run state.
    ATOMIC_WRITE_TARGETS = {
        ("handsoff_cli.change_pin", "_history_path(root)"),
        ("handsoff_cli.migrate_project", "root / lib.OVERRIDES_FILE"),
        ("handsoff_fleet.save_registry", "path"),
        ("handsoff_fleet_signals._persist", "self.path"),
        ("handsoff_ledger.append_event", "event_head_path(root)"),
        ("handsoff_ledger.commit", "acceptance_path(root, cfg)"),
        ("handsoff_ledger.commit", "status_path(root, cfg)"),
        ("handsoff_ledger.write_ahead", "write_ahead_path(root)"),
        ("handsoff_lib._write_ci_side", "ci_side_path(root)"),
        ("handsoff_lib.launch_preflight", "path"),
        ("handsoff_lib.run_design_evidence", "design_evidence_path(root)"),
        ("handsoff_lib.write_dashboard_owner", "path"),
        ("handsoff_regress._write", "inventory_path(root)"),
        ("handsoff_regress._write", "progress_path(root)"),
        ("handsoff_regress.main", "inventory_path(root)"),
        ("handsoff_supervisor._load_close_transaction", "path"),
        ("handsoff_supervisor._runtime_write", "path"),
        ("handsoff_supervisor.cmd_verify", "snapshot"),
        ("handsoff_supervisor.cmd_verify_live", "inflight_path"),
        ("handsoff_supervisor.on_progress", "inflight_path"),
        ("handsoff_supervisor.persist", "path"),
    }

    def _measure(self):
        found = set()
        for path in sorted(BIN.glob("handsoff_*.py")):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    name = getattr(node.func, "id", getattr(node.func, "attr", None))
                    if name != "atomic_write_json":
                        continue
                    target = ast.get_source_segment(src, node.args[0]) or "?"
                    found.add((f"{path.stem}.{fn.name}", target))
        return found

    def test_the_inventory_of_json_writes_is_unchanged(self):
        found = self._measure()
        self.assertEqual(
            sorted(found - self.ATOMIC_WRITE_TARGETS), [],
            "a new JSON write appeared; if it writes the run's status or acceptance it must "
            "go through commit, which validates, and otherwise add it here")
        self.assertEqual(
            sorted(self.ATOMIC_WRITE_TARGETS - found), [],
            "these are recorded but no longer exist; a stale entry hides the next one")

    def test_only_commit_names_the_status_and_acceptance_documents(self):
        writers = {fn for fn, target in self.ATOMIC_WRITE_TARGETS
                   if "status_path" in target or "acceptance_path" in target}
        self.assertEqual(writers, {"handsoff_ledger.commit"},
                         "something other than commit writes the run's own documents")

    def test_the_measurement_is_not_vacuous(self):
        """A matcher that found nothing would pass both tests above."""
        self.assertGreaterEqual(len(self._measure()), 20,
                                "the matcher stopped finding JSON writes")


if __name__ == "__main__":
    unittest.main()
