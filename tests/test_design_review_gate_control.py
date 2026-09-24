"""#319 and #320: the gate the Pilot can clear, and the limit that fires it.

`_input_request` computed `budget_exhausted`, used it to set `kind`, and
then threw it away, because the return applies `kind if required else
None` and `budget_exhausted` was never a term in `required`. Two paths
broke together and neither could be worked around from the other: the
action builder never offered "Authorize one review", and
`POST /api/design-review-authorize` refused because it checks
`input_required.kind`. Only the CLI still worked.

docs/REFERENCE.md already documented the intended behaviour: "Mission
Control shows Authorize one review whenever the Phase 2 budget is
exhausted and the run is not closed, without depending on a separately
recorded hold." The code stopped keeping that promise, which is why it
was reported twice from the operator's side.

The limit itself is the other half. At 2 the gate interrupted 27% of
runs and saved nothing, because a blocked run was authorized and then ran
the attempt anyway.
"""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class TheLimitIsTheMeasuredPercentile(unittest.TestCase):
    """REQ-002. The number is pinned to the measurement that chose it.

    100 product runs in the archive, classified with the shared rule from
    #317, distribute their design-review attempts:

        1 -> 24    2 -> 49    3 -> 17    4 -> 6
        5 -> 1     6 -> 1     8 -> 2

    Cumulatively a limit of 2 finishes 73% of runs inside it and stops 27%
    for a Pilot click; a limit of 3 finishes 90% and stops 10%. Three is
    the nearest-rank p90, the convention `tokens_per_ticket` already uses.
    """

    MEASURED = {1: 24, 2: 49, 3: 17, 4: 6, 5: 1, 6: 1, 8: 2}

    def test_the_default_is_three(self):
        self.assertEqual(lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS, 3)
        self.assertEqual(lib.DEFAULT_CONFIG["max_autonomous_design_reviews"], 3)

    def test_three_is_the_nearest_rank_p90_of_the_measured_runs(self):
        """Derived, not asserted: if the measurement changes, so does the
        number this test demands."""
        runs = sorted(attempts for attempts, count in self.MEASURED.items()
                      for _ in range(count))
        self.assertEqual(len(runs), 100)
        p90 = runs[-(-90 * len(runs) // 100) - 1]
        self.assertEqual(p90, lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS)

    def test_the_old_default_interrupted_more_than_a_quarter_of_runs(self):
        total = sum(self.MEASURED.values())
        past_two = sum(c for a, c in self.MEASURED.items() if a > 2)
        past_three = sum(c for a, c in self.MEASURED.items() if a > 3)
        self.assertEqual(past_two, 27)
        self.assertEqual(past_three, 10)
        self.assertLess(past_three / total, past_two / total)

    def test_the_gate_is_kept_because_the_tail_is_real(self):
        """Four runs needed five to eight attempts. An unbounded limit
        would let a pathological run spend reviewer sessions with no stop."""
        tail = sum(c for a, c in self.MEASURED.items()
                   if a > lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS + 1)
        self.assertEqual(tail, 4)
        self.assertGreater(lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS, 0)

    def test_it_remains_governance_bound(self):
        self.assertIn("max_autonomous_design_reviews", lib.GOVERNANCE_CONFIG_KEYS)


class TheExhaustedBudgetReachesThePilot(HandsoffTestCase):
    """REQ-001. The state that produced both reported occurrences."""

    def _exhausted_run(self, *, run_closed=False):
        """Phase 2, budget spent, and no `authorization_hold` recorded.

        That combination is what an approved final attempt leaves behind
        when its design approval is later revoked, for example by a
        criterion update. `cmd_design_review` only writes the hold on the
        `changes_requested` branch, so nothing else in `required` is true.
        """
        self.init("Exhausted budget")
        status = self.read_status()
        status["phase_number"] = 2
        status["status"] = "in_progress"
        status["design_review_attempts"] = lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS
        status.pop("authorization_hold", None)
        status["requires_design_approval"] = False
        if run_closed:
            status["run_closed"] = {"at": "2026-09-24T00:00:00+00:00", "outcome": "closed"}
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2))
        return status

    def _snapshot(self):
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        import handsoff_dashboard as dashboard
        return dashboard.build_snapshot(self.tmp)

    def test_the_run_reads_as_input_required(self):
        self._exhausted_run()
        request = self._snapshot().get("input_required") or {}
        self.assertTrue(request.get("required"),
                        "an exhausted budget is a Pilot decision, so the banner must arm")
        self.assertEqual(request.get("kind"), "design_review_budget")

    def test_mission_control_offers_the_authorize_control(self):
        self._exhausted_run()
        offered = {item.get("kind") for item in self._snapshot().get("operator_actions") or []}
        self.assertIn("design_review_authorize", offered,
                      "the Pilot had no way to clear this gate from Mission Control")

    def test_it_also_offers_the_escalate_and_hold_controls(self):
        self._exhausted_run()
        offered = {item.get("kind") for item in self._snapshot().get("operator_actions") or []}
        self.assertIn("design_review_escalate", offered)
        self.assertIn("pause", offered)

    def test_a_run_with_budget_remaining_offers_no_authorize_control(self):
        """The term must not fire indiscriminately."""
        self.init("Budget remaining")
        status = self.read_status()
        status["phase_number"] = 2
        status["design_review_attempts"] = 0
        status["requires_design_approval"] = False
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2))
        snapshot = self._snapshot()
        offered = {item.get("kind") for item in snapshot.get("operator_actions") or []}
        self.assertNotIn("design_review_authorize", offered)
        self.assertNotEqual((snapshot.get("input_required") or {}).get("kind"),
                            "design_review_budget")

    def test_a_closed_run_offers_no_authorize_control(self):
        self._exhausted_run(run_closed=True)
        snapshot = self._snapshot()
        offered = {item.get("kind") for item in snapshot.get("operator_actions") or []}
        self.assertNotIn("design_review_authorize", offered)


class TheComputedValueIsConsumed(unittest.TestCase):
    """REQ-001, derived from the source.

    The defect was not a wrong value, it was a correct value nothing read.
    A future edit that recomputes `budget_exhausted` and again leaves it
    out of `required` fails here rather than silently stranding the Pilot.
    """

    def test_budget_exhausted_is_a_term_in_required(self):
        import ast
        source = (BIN / "handsoff_dashboard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "_input_request")
        assignments = [node for node in ast.walk(function)
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "required" for t in node.targets)]
        self.assertTrue(assignments, "_input_request no longer assigns `required`")
        names = {n.id for a in assignments for n in ast.walk(a.value) if isinstance(n, ast.Name)}
        self.assertIn("budget_exhausted", names,
                      "budget_exhausted is computed but not consumed by `required`; "
                      "the Pilot's control is discarded by `kind if required else None`")


if __name__ == "__main__":
    unittest.main()
