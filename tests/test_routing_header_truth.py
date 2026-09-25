"""#333: the routing panel's header must agree with its own rows.

Observed on lane `claude/lane-284`, engine v0.3.86: the header claimed
`model: claude-haiku-4-5-20251001`, `used: true`, `outcome: accepted`, while
its own `selections` rows showed both sessions ran `codex` on `gpt-5.6-luna`.
The run's recorded state agreed with the rows: `status.adaptive_routing` was
None and every session's `adaptive_routing` was None.

Nothing had been routed. `[agents] reviewer = "codex"` is an explicit adapter
pin, which `build_launch_spec` treats as a mission constraint that skips
adaptive routing, so no session carried a decision and the snapshot builder
computed one at read time -- a reasonable thing to show as what routing WOULD
choose, presented instead as what was chosen.

The panel is the only place a Pilot sees which model is spending their budget.
It named a 1 dollar per Mtok model while the work ran on a premium one and
said the choice was accepted.

Same shape as #319: a value computed at read time, surfaced as recorded state.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib  # noqa: E402

#: The pinned-reviewer run from the report: two codex sessions, no routing.
PINNED_REVIEWER_RUN = {
    "phase_number": 5, "status": "in_progress", "risk_class": "routine",
    "adaptive_escalation": {"outcome": "accepted", "reason": "within budget"},
    "agent_sessions": {
        "hs-" + "1" * 32: {
            "session_id": "hs-" + "1" * 32, "role": "reviewer", "state": "completed",
            "actor": "codex-reviewer", "adapter": "codex", "requested_model": "default",
            "reported_model": "gpt-5.6-luna", "resolution_source": "configured",
            "started_at": "2026-09-24T10:00:00+00:00",
            "running_at": "2026-09-24T10:00:00+00:00",
            "ended_at": "2026-09-24T10:20:00+00:00", "exit_code": 0,
        },
        "hs-" + "2" * 32: {
            "session_id": "hs-" + "2" * 32, "role": "reviewer", "state": "completed",
            "actor": "codex-reviewer", "adapter": "codex", "requested_model": "default",
            "reported_model": "gpt-5.6-luna", "resolution_source": "configured",
            "started_at": "2026-09-24T11:00:00+00:00",
            "running_at": "2026-09-24T11:00:00+00:00",
            "ended_at": "2026-09-24T11:20:00+00:00", "exit_code": 0,
        },
    },
}


class TheHeaderAgreesWithTheRows(unittest.TestCase):
    """The claim the report makes, as a test."""

    def setUp(self):
        self.view = lib.adaptive_routing_snapshot(PINNED_REVIEWER_RUN, {})

    def _row_models(self):
        return {row.get("model") for row in self.view["selections"]
                if isinstance(row.get("model"), str) and row["model"].strip()}

    def test_the_named_model_is_one_a_session_reported(self):
        models = self._row_models()
        self.assertTrue(models, "the fixture has no row with a model; the test proves nothing")
        self.assertIn(self.view["model"], models,
                      "the header names a model no row reports, which is the #333 defect: "
                      f"header {self.view['model']!r} against rows {sorted(models)}")

    def test_it_does_not_name_the_model_routing_would_have_chosen(self):
        """The exact symptom: a FAST-tier model surfaced on a run that used a
        premium one. Pinned to the values from the report."""
        self.assertNotEqual(self.view["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(self.view["model"], "gpt-5.6-luna")
        self.assertEqual(self.view["adapter"], "codex")

    def test_the_header_says_where_its_model_came_from(self):
        self.assertEqual(self.view["header_source"], "reported",
                         "nothing was routed, so the header is a reported model")

    def test_no_outcome_is_claimed_when_nothing_was_routed(self):
        """`outcome: accepted` says a routing choice was made and approved.
        The recorded escalation is still in the status; what is refused is
        presenting it as the outcome of a decision that never happened."""
        self.assertIsNone(self.view["outcome"])
        self.assertEqual(PINNED_REVIEWER_RUN["adaptive_escalation"]["outcome"], "accepted",
                         "the fixture's recorded escalation is untouched")

    def test_the_read_time_projection_is_kept_under_its_own_name(self):
        """The computed choice is useful. It is not the header."""
        projection = self.view["would_route_to"]
        self.assertIsInstance(projection, dict)
        self.assertEqual(projection["tier"], "FAST")
        self.assertNotEqual(projection["model"], self.view["model"],
                            "the fixture no longer distinguishes projection from header")

    def test_the_tier_is_not_borrowed_from_the_projection(self):
        """A FAST tier beside a premium model is how the report read."""
        self.assertNotEqual(self.view["tier"], projection_tier(self.view),
                            "the header took its tier from the projection")


def projection_tier(view):
    return (view.get("would_route_to") or {}).get("tier")


class ARoutedRunStillReportsItsDecision(unittest.TestCase):
    """The fix must not blank the panel on a run that really was routed."""

    def setUp(self):
        session = dict(PINNED_REVIEWER_RUN["agent_sessions"]["hs-" + "1" * 32])
        session["adaptive_routing"] = {"tier": "PREMIUM", "adapter": "codex",
                                       "model": "gpt-6-astra"}
        session["reported_model"] = "gpt-6-astra"
        self.status = {**PINNED_REVIEWER_RUN,
                       "agent_sessions": {session["session_id"]: session}}
        self.view = lib.adaptive_routing_snapshot(self.status, {})

    def test_the_recorded_decision_is_the_header(self):
        self.assertEqual((self.view["tier"], self.view["adapter"], self.view["model"]),
                         ("PREMIUM", "codex", "gpt-6-astra"))
        self.assertEqual(self.view["header_source"], "routed")

    def test_the_recorded_outcome_is_reported(self):
        self.assertEqual(self.view["outcome"], "accepted",
                         "a run that WAS routed must still show its escalation outcome")

    def test_no_projection_is_offered_once_a_decision_exists(self):
        self.assertIsNone(self.view["would_route_to"],
                          "a routed run has no need of what routing would choose")


class AFreshRunShowsTheProjectionAndClaimsNothing(unittest.TestCase):
    """No sessions at all: there is nothing to report, and saying so is right."""

    def setUp(self):
        self.view = lib.adaptive_routing_snapshot(
            {"phase_number": 1, "status": "in_progress", "risk_class": "routine",
             "agent_sessions": {}}, {})

    def test_the_header_names_no_model(self):
        self.assertIsNone(self.view["model"])
        self.assertIsNone(self.view["tier"])
        self.assertEqual(self.view["header_source"], "projection")

    def test_the_projection_is_still_offered(self):
        self.assertIsInstance(self.view["would_route_to"], dict)
        self.assertEqual(self.view["would_route_to"]["tier"], "FAST")

    def test_the_run_still_reads_as_governed_by_routing(self):
        """`used` means the run is governed by adaptive routing, which a
        risk_class makes true before any call. The panel stays open; what
        changed is that it no longer invents a model for the header."""
        self.assertTrue(self.view["used"])


if __name__ == "__main__":
    unittest.main()
