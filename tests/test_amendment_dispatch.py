"""#146: a reviewer session launched for an open amendment dispatches as an
amendment review whatever kind the reviewer wrote on the verdict, with its
findings, through live dispatch and through session-result-adopt. Real
ledgers throughout: a Phase 4 run, an open scoped amendment, managed
reviewer sessions bound to it, and the broker's real workflow subprocess."""
import json
import sys
import unittest

from tests import test_handsoff_supervisor as _supervisor_tests
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402

class _AmendmentFixture(HandsoffTestCase):
    """The TestAmendmentLane fixture (a Phase 4 run with two derived work
    items and an amendment transaction helper) without inheriting its tests."""
    # Helpers are borrowed by attribute so the lane's own tests are not
    # re-registered in this module.
    STATE_FILES = _supervisor_tests.TestAmendmentLane.STATE_FILES
    ARCHITECT = _supervisor_tests.TestAmendmentLane.ARCHITECT
    REVIEWER = _supervisor_tests.TestAmendmentLane.REVIEWER
    PILOT = _supervisor_tests.TestAmendmentLane.PILOT
    _phase_4_run = _supervisor_tests.TestAmendmentLane._phase_4_run
    _open = _supervisor_tests.TestAmendmentLane._open
    _revise = _supervisor_tests.TestAmendmentLane._revise
    _review = _supervisor_tests.TestAmendmentLane._review
    _write_tx = _supervisor_tests.TestAmendmentLane._write_tx
    _update = _supervisor_tests.TestAmendmentLane._update
    _ok = _supervisor_tests.TestAmendmentLane._ok
    _events = _supervisor_tests.TestAmendmentLane._events
    _kinds = _supervisor_tests.TestAmendmentLane._kinds
    _criteria = _supervisor_tests.TestAmendmentLane._criteria

    def setUp(self):
        super().setUp()
        import handsoff_dashboard
        self.lib = lib
        self.broker = broker
        self.dashboard = handsoff_dashboard
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1)
                        .replace("live_commands = []", 'live_commands = ["true"]', 1))


class AmendmentDispatchTests(_AmendmentFixture):
    def _sid(self, n):
        return f"hs-{n:032x}"

    def _reviewer_session(self, number, *, amendment_id=None, actor="codex-reviewer"):
        session = self.lib.create_agent_session(
            self.tmp, role="reviewer", actor=actor, adapter="codex", requested_model="default",
            resolution_source="configured", id_factory=lambda: self._sid(number), amendment_id=amendment_id)
        self.lib.transition_agent_session(self.tmp, session["session_id"], "running")
        return session["session_id"]

    def _open_amendment(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-003", requirement="[#102] the second item's outcome, corrected")]))
        return self.read_status()["amendment"]["amendment_id"]

    def _end(self, sid):
        self.lib.transition_agent_session(self.tmp, sid, "completed", exit_code=0)

    def test_design_kind_verdict_from_an_amendment_session_records_the_amendment_review(self):
        amendment_id = self._open_amendment()
        sid = self._reviewer_session(1, amendment_id=amendment_id)
        result = broker.parse_reviewer_result(json.dumps({
            "kind": "design", "decision": "approved", "summary": "delta is sound",
            "findings": [], "structural_blocker": False, "symptom_reproduced": "not_applicable"}))
        request = broker._reviewer_result_request(self.tmp, sid, result)
        self.assertEqual((request["command"], request["decision"]), ("amendment-review", "approve"))
        self.assertNotIn("session", request)
        self.assertEqual(broker.dispatch_reviewer_result(self.tmp, sid, result), 0)
        review = self.read_status()["amendment"]["review"]
        self.assertEqual((review["decision"], review["by"]), ("approved", "codex-reviewer"))
        self.assertNotIn("findings", review)
        self.assertIn("amendment_reviewed", self._kinds())
        # The run's Phase 2 design review is untouched: nothing went down the
        # record-design-review path.
        self.assertEqual(self.read_status()["design_review"]["attempt"], 1)
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_changes_requested_with_findings_lands_on_the_amendment_record(self):
        amendment_id = self._open_amendment()
        sid = self._reviewer_session(2, amendment_id=amendment_id)
        result = broker.parse_reviewer_result(json.dumps({
            "kind": "implementation", "decision": "changes_requested", "summary": "two gaps",
            "findings": ["acceptance_not_met: REQ-003 names no observable outcome",
                         "other: the affected item list omits #101"],
            "structural_blocker": False, "symptom_reproduced": "not_applicable"}))
        request = broker._reviewer_result_request(self.tmp, sid, result)
        self.assertEqual(request["decision"], "request-changes")
        self.assertEqual(len(request["findings"]), 2)
        self.assertEqual(broker.dispatch_reviewer_result(self.tmp, sid, result), 0)
        status = self.read_status()
        review = status["amendment"]["review"]
        self.assertEqual(review["decision"], "changes_requested")
        self.assertEqual(review["findings"], request["findings"])
        self.assertEqual(self.lib.amendment_pending_decision(status["amendment"]), "revision")
        event = [e for e in self._events() if e["kind"] == "amendment_reviewed"][-1]
        self.assertEqual(event["findings"], request["findings"])
        self.assertEqual(self.lib.validate_status_schema(status), [])

    def test_a_persisted_design_kind_verdict_adopts_as_an_amendment_review_with_findings(self):
        amendment_id = self._open_amendment()
        sid = self._reviewer_session(3, amendment_id=amendment_id)
        payload = broker.parse_reviewer_result(json.dumps({
            "kind": "design", "decision": "changes_requested", "summary": "one gap",
            "findings": ["acceptance_not_met: REQ-003 still reads as a task, not an outcome"],
            "structural_blocker": False, "symptom_reproduced": "not_applicable"}))
        self.lib.record_session_result(self.tmp, sid, "design", payload)
        self.lib.transition_agent_session(
            self.tmp, sid, "failed", exit_code=1,
            failure={"category": "dispatch_failed", "reason": "simulated dispatch failure",
                     "tail_sha256": "0" * 64, "result_available": True})
        adopted = run(["session-result-adopt", "--session", sid, "--by", "claude-supervisor"], cwd=self.tmp)
        self.assertEqual(adopted.returncode, 0, adopted.stdout + adopted.stderr)
        status = self.read_status()
        review = status["amendment"]["review"]
        self.assertEqual((review["decision"], review["by"]), ("changes_requested", "codex-reviewer"))
        self.assertEqual(review["findings"], payload["findings"])
        self.assertEqual((review["adopted_session"], review["adopted_by"]), (sid, "claude-supervisor"))
        self.assertIsNotNone(status["agent_sessions"][sid]["result"]["adopted_at"])
        self.assertEqual(self.lib.validate_status_schema(status), [])
        # Adopting the same verdict twice is refused.
        again = run(["session-result-adopt", "--session", sid, "--by", "claude-supervisor"], cwd=self.tmp)
        self.assertNotEqual(again.returncode, 0)

    def test_amendment_binding_is_validated_before_the_kind_switch(self):
        amendment_id = self._open_amendment()
        bound = self._reviewer_session(4, amendment_id=amendment_id)
        result = broker.parse_reviewer_result(json.dumps({
            "kind": "design", "decision": "approved", "summary": "x",
            "findings": [], "structural_blocker": False, "symptom_reproduced": "not_applicable"}))
        # A design-kind verdict from a session with no amendment binding
        # while an amendment is open is refused, not routed to Phase 2.
        self._end(bound)
        unbound = self._reviewer_session(5)
        with self.assertRaisesRegex(self.lib.HandsoffError, "amendment .* is open"):
            broker._reviewer_result_request(self.tmp, unbound, result)
        self._end(unbound)
        # A bound session whose amendment has since closed is refused on the
        # binding, before the kind switch could send it anywhere else.
        rebound = self._reviewer_session(6, amendment_id=amendment_id)
        escalated = run(["amendment-escalate", "--by", self.ARCHITECT, "--reason", "full redesign after all"], cwd=self.tmp)
        self.assertEqual(escalated.returncode, 0, escalated.stdout + escalated.stderr)
        with self.assertRaisesRegex(self.lib.HandsoffError, "no amendment is open"):
            broker._reviewer_result_request(self.tmp, rebound, result)
        self.assertIsNone(self.read_status().get("amendment"))

    def test_phase_2_design_verdict_without_an_amendment_still_records_a_design_review(self):
        self.init("Plain design review")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "[#7] the outcome", "--test", "true"], cwd=self.tmp))
        self._ok(run(["advance", "2", "20"], cwd=self.tmp))
        self.lib.record_design_proposal(self.tmp, None, {
            "summary": "bounded design", "approach": ["step"], "tradeoffs": [], "decisions": ["decision"],
            "constraints": [], "verification": ["targeted check"]}, architect_actor="architect-1")
        sid = self._reviewer_session(5)
        result = broker.parse_reviewer_result(json.dumps({
            "kind": "design", "decision": "approved", "summary": "sound",
            "findings": [], "structural_blocker": False, "symptom_reproduced": "not_applicable"}))
        request = broker._reviewer_result_request(self.tmp, sid, result)
        self.assertEqual(request["command"], "record-design-review")
        self.assertEqual(broker.dispatch_reviewer_result(self.tmp, sid, result), 0)
        self.assertEqual(self.read_status()["design_review"]["decision"], "approved")

    def test_cli_refuses_findings_on_an_approval_and_bounds_them(self):
        self._open_amendment()
        approved = run(["amendment-review", "--by", self.REVIEWER, "--approve", "--summary", "fine",
                        "--finding", "stray"], cwd=self.tmp)
        self.assertNotEqual(approved.returncode, 0)
        self.assertIn("no findings", approved.stdout)
        too_many = run(["amendment-review", "--by", self.REVIEWER, "--request-changes", "--summary", "many",
                        *sum([["--finding", f"finding {n}"] for n in range(lib.MAX_AMENDMENT_REVIEW_FINDINGS + 1)], [])],
                       cwd=self.tmp)
        self.assertNotEqual(too_many.returncode, 0)
        self.assertIsNone(self.read_status()["amendment"]["review"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
