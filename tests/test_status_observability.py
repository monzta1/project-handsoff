"""#308: what `status` must show, and why the rule is derived rather than listed.

A host running unattended is told to read state rather than remember it. The
status projection is hand-built, so a field a gate consults could be absent
from it, and nothing distinguished "not shown because it is surfaced
elsewhere" from "not shown because nobody added it".

This suite enforces the rule and, as importantly, refuses to pass vacuously:
the consulted set is parsed out of the named gate functions, a computed field
name is an error rather than a skip, and a run that finds too few fields is
treated as a broken matcher rather than clean code.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_observability as obs  # noqa: E402

ROOT = BIN.parent


class TheConsultedSetIsDerivedFromSource(unittest.TestCase):
    """REQ-001. A hand-written list rots; this one cannot."""

    def setUp(self):
        self.found = obs.consulted_fields(BIN)

    def test_the_analysis_finds_the_real_gate_reads(self):
        self.assertGreaterEqual(len(self.found), obs.MINIMUM_CONSULTED_FIELDS)

    def test_it_covers_every_named_gate_function(self):
        covered = {(c.module, c.function) for c in self.found}
        for module, functions in obs.GATE_FUNCTIONS.items():
            for function in functions:
                self.assertIn((module, function), covered,
                              f"{module}:{function} consulted no status field; either it stopped "
                              "being a gate or the matcher missed it")

    def test_a_delegating_gate_names_a_delegate_that_is_analysed(self):
        """`performance_mutation_refusal` reads no status field itself; it
        calls `refresh_performance_state`, which does. Declaring the chain
        keeps it a gate in the contract instead of dropping it silently."""
        analysed = {f for functions in obs.GATE_FUNCTIONS.values() for f in functions}
        for gate, delegate in obs.DELEGATING_GATES.items():
            self.assertIn(delegate, analysed,
                          f"{gate} delegates to {delegate}, which nothing analyses")

    def test_a_gate_function_that_no_longer_exists_is_an_error(self):
        original = dict(obs.GATE_FUNCTIONS)
        obs.GATE_FUNCTIONS["handsoff_lib.py"] = ("compute_errors", "no_such_gate_function")
        try:
            with self.assertRaisesRegex(obs.ObservabilityError, "drifted"):
                obs.consulted_fields(BIN)
        finally:
            obs.GATE_FUNCTIONS.clear()
            obs.GATE_FUNCTIONS.update(original)

    def test_it_recognises_the_three_declared_access_forms(self):
        import ast
        src = ("def gate(status):\n"
               "    a = status.get('alpha')\n"
               "    b = status.get('beta', 1)\n"
               "    c = status['gamma']\n")
        node = ast.parse(src).body[0]
        visitor = obs._StatusReads("gate", "fake.py")
        visitor.visit(node)
        self.assertEqual({c.field for c in visitor.found}, {"alpha", "beta", "gamma"})
        self.assertEqual(visitor.computed, [])

    def test_a_computed_field_name_is_refused_not_skipped(self):
        """Skipping it would let a field escape the rule silently, which is
        the exact failure this module exists to prevent."""
        import ast
        node = ast.parse("def gate(status, key):\n    return status.get(key)\n").body[0]
        visitor = obs._StatusReads("gate", "fake.py")
        visitor.visit(node)
        self.assertEqual(visitor.found, set())
        self.assertEqual(visitor.computed, ["fake.py:gate"])

    def test_the_floor_stops_a_broken_matcher_passing(self):
        original = obs.MINIMUM_CONSULTED_FIELDS
        obs.MINIMUM_CONSULTED_FIELDS = 10_000
        try:
            with self.assertRaisesRegex(obs.ObservabilityError, "matcher is broken"):
                obs.consulted_fields(BIN)
        finally:
            obs.MINIMUM_CONSULTED_FIELDS = original


class EveryConsultedFieldIsDeclared(unittest.TestCase):
    """REQ-001: a field absent from the map is a violation, not a default."""

    def test_no_consulted_field_is_undeclared(self):
        names = {c.field for c in obs.consulted_fields(BIN)}
        undeclared = sorted(names - set(obs.STATUS_OBSERVABILITY))
        self.assertEqual(undeclared, [],
                         "a gate consults these without declaring how they are surfaced")

    def test_the_map_declares_nothing_a_gate_does_not_consult(self):
        names = {c.field for c in obs.consulted_fields(BIN)}
        stale = sorted(set(obs.STATUS_OBSERVABILITY) - names)
        self.assertEqual(stale, [], "these are declared but no gate reads them any more")

    def test_the_declarations_are_well_formed(self):
        obs.validate_declarations(obs.STATUS_OBSERVABILITY)


class NotSurfacedIsBoundedNotAnEscapeValve(unittest.TestCase):
    """REQ-005. The design reviewer refused an unbounded version of this."""

    def test_exactly_the_expected_fields_are_not_surfaced(self):
        """Asserted against an explicit set, so a fourth is a visible change
        to this test rather than a silent widening."""
        declared = {f for f, e in obs.STATUS_OBSERVABILITY.items()
                    if e["kind"] == obs.NOT_SURFACED}
        self.assertEqual(declared, {"agent_sessions", "design_proposal", "design_review_history"})

    def test_each_reason_is_its_own_words(self):
        reasons = [e["reason"] for e in obs.STATUS_OBSERVABILITY.values()
                   if e["kind"] == obs.NOT_SURFACED]
        self.assertEqual(len(reasons), len(set(reasons)))
        for reason in reasons:
            self.assertGreaterEqual(len(reason), 20)

    def test_a_shared_reason_is_refused(self):
        shared = "the same blanket excuse repeated for two different fields entirely"
        with self.assertRaisesRegex(obs.ObservabilityError, "blanket exemption"):
            obs.validate_declarations({
                "one": {"kind": obs.NOT_SURFACED, "reason": shared},
                "two": {"kind": obs.NOT_SURFACED, "reason": shared},
            })

    def test_a_pattern_is_refused(self):
        with self.assertRaisesRegex(obs.ObservabilityError, "never a pattern"):
            obs.validate_declarations({"design_*": {"kind": obs.OWN_KEY}})

    def test_a_reason_too_short_to_be_a_reason_is_refused(self):
        with self.assertRaisesRegex(obs.ObservabilityError, "reason of its own"):
            obs.validate_declarations({"x": {"kind": obs.NOT_SURFACED, "reason": "big"}})

    def test_a_derived_declaration_needs_its_path(self):
        with self.assertRaisesRegex(obs.ObservabilityError, "needs the path"):
            obs.validate_declarations({"x": {"kind": obs.DERIVED}})


class TheRuleHoldsAgainstARenderedPayload(HandsoffTestCase):
    """REQ-001 and REQ-002, against the real command output."""

    def _payload(self):
        self.init("Observability")
        result = run(["status"], cwd=self.tmp)
        return json.loads(result.stdout)

    def test_every_own_key_field_is_present(self):
        payload = self._payload()
        missing = [f for f, e in obs.STATUS_OBSERVABILITY.items()
                   if e["kind"] == obs.OWN_KEY and not obs.reachable_in(payload, f)]
        self.assertEqual(missing, [], "declared surfaced but absent from the payload")

    def test_every_derived_path_resolves(self):
        """A declared mapping cannot name a path that does not exist."""
        payload = self._payload()
        broken = [f for f, e in obs.STATUS_OBSERVABILITY.items()
                  if e["kind"] == obs.DERIVED and not obs.reachable_in(payload, e["path"])]
        self.assertEqual(broken, [])

    def test_a_field_declared_absent_is_actually_absent(self):
        payload = self._payload()
        contradictory = [f for f, e in obs.STATUS_OBSERVABILITY.items()
                         if e["kind"] == obs.NOT_SURFACED and f in payload]
        self.assertEqual(contradictory, [],
                         "declared not_surfaced while present; the declaration is a lie")

    def test_the_five_host_polled_paths_are_present(self):
        payload = self._payload()
        for path in obs.HOST_POLLED_PATHS:
            self.assertTrue(obs.reachable_in(payload, path), f"{path} is not in the payload")

    def test_the_authorization_a_host_waited_on_is_visible(self):
        """The 2026-09-24 exchange: the grant was reachable only as
        design_review_budget.authorized, so a host polling by name saw None.
        Both are present now."""
        payload = self._payload()
        self.assertIn("design_review_authorization", payload)
        self.assertIn("authorized", payload["design_review_budget"])

    def test_existing_consumers_keep_their_fields(self):
        """Fields are added, never renamed or removed."""
        payload = self._payload()
        for field in ("feature", "phase", "phase_number", "progress", "status",
                      "gate_progress", "next_action", "errors", "validation"):
            self.assertIn(field, payload)


if __name__ == "__main__":
    unittest.main()
