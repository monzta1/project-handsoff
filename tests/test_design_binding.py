"""REQ-004 and REQ-005 design binding and provenance coverage."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import handsoff_lib as lib
from test_handsoff_supervisor import HandsoffTestCase, run


class DesignBindingTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.set_host_architect()
        self.init("Design binding")
        run(["criterion-update", "REQ-001", "--requirement", "Bound design"], self.tmp)
        run(["advance", "2", "20"], self.tmp)
        self.proposal = {
            "summary": "Bound proposal", "approach": ["Keep the existing path"],
            "tradeoffs": [], "decisions": ["Bind decisions to the proposal"],
            "constraints": [], "verification": ["Run focused checks"],
        }

    def set_host_architect(self):
        path = self.tmp / "handsoff.toml"
        path.write_text(path.read_text().replace('architect = "auto"', 'architect = "host"'))

    def propose(self, actor="architect-1"):
        path = self.tmp / "proposal.json"
        path.write_text(json.dumps(self.proposal))
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_COMPANION_SESSION_ID": "host-session-1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        result = run(["design-propose", "--file", str(path), "--by", actor], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.read_status()

    def approve_review(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "host-session-reviewer", "CODEX_COMPANION_SESSION_ID": ""}, clear=True):
            result = run(["record-design-review", "--approve", "--by", "reviewer-1",
                          "--architect", "architect-1", "--summary", "independent"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = run(["design-approve", "--by", "pilot", "--architect", "architect-1",
                      "--summary", "approved"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_approval_stores_proposal_hash(self):
        status = self.propose()
        proposal_hash = status["design_proposal"]["proposal_hash"]
        self.approve_review()
        self.assertEqual(self.read_status()["design_approved"]["proposal_hash"], proposal_hash)

    def test_revision_nulls_decisions_and_emits_event(self):
        self.propose()
        self.approve_review()
        old = self.read_status()["design_proposal"]["proposal_hash"]
        self.proposal["summary"] = "Revised proposal"
        self.propose()
        status = self.read_status()
        self.assertIsNone(status["design_approved"])
        self.assertIsNone(status["design_review"])
        self.assertEqual(status["phase_number"], 2)
        self.assertEqual(status["next_action"], "Independent Reviewer evaluates the revised proposal")
        event = lib.read_events(self.tmp, lib.load_config(self.tmp))[-2]
        self.assertEqual(event["kind"], "decisions_invalidated")
        self.assertEqual(event["old_proposal_hash"], old)
        self.assertEqual(event["nulled_decisions"], ["design_approved", "design_review"])

    def test_stale_proposal_error_names_hashes(self):
        status = self.propose()
        status["requires_design_approval"] = True
        status["design_approved"] = {"by": "pilot", "architect": "architect-1", "at": status["updated_at"],
                                      "design_hash": "x", "proposal_hash": "old"}
        errors = lib._design_errors(status, lib.load_unique_json(lib.acceptance_path(self.tmp, lib.load_config(self.tmp))),
                                    lib.load_config(self.tmp))
        self.assertTrue(any("stale proposal: approval binds old, current is" in error for error in errors))

    def test_packet_reports_proposal_changed(self):
        self.propose()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "host-session-reviewer", "CODEX_COMPANION_SESSION_ID": ""}, clear=True):
            run(["record-design-review", "--approve", "--by", "reviewer-1", "--architect", "architect-1",
                 "--summary", "first"], self.tmp)
        self.proposal["summary"] = "Revised proposal"
        self.propose()
        status = self.read_status()
        cfg = lib.load_config(self.tmp)
        packet = lib.build_design_review_packet(self.tmp, cfg, status,
                                                lib.load_unique_json(lib.acceptance_path(self.tmp, cfg)))
        self.assertTrue(packet["proposal_changed"])
        self.assertNotEqual(packet["previous_proposal_hash"], packet["proposal_hash"])

    def test_provenance_records_host_session_id(self):
        status = self.propose("architect-provenance")
        provenance = status["design_proposal"]["provenance"]
        self.assertEqual(provenance["actor"], "architect-provenance")
        self.assertEqual(provenance["host_session_id"], "host-session-1")
        self.assertGreater(provenance["pid"], 0)

    def test_review_refuses_architect_actor_and_host_session(self):
        self.propose("architect-1")
        same_actor = run(["record-design-review", "--approve", "--by", "architect-1",
                          "--architect", "architect-1", "--summary", "no"], self.tmp)
        self.assertEqual(same_actor.returncode, 1)
        self.assertIn("reviewer must differ", same_actor.stdout)

    def _live_reviewer(self, adapter, actor="reviewer-managed"):
        # A reviewer session launched from the Architect's host session.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_COMPANION_SESSION_ID": "host-session-1"}, clear=True):
            session = lib.create_agent_session(self.tmp, role="reviewer", actor=actor, adapter=adapter,
                                               requested_model="default", resolution_source="configured")
        status = self.read_status()
        self.assertEqual(status["agent_sessions"][session["session_id"]]["host_session_id"], "host-session-1")
        return session["session_id"]

    def test_managed_reviewer_from_the_architect_host_session_is_accepted(self):
        # #112: the host Supervisor launches Codex from the same terminal the
        # proposal came from; the reviewer is still an independent process.
        self.propose("architect-1")
        session_id = self._live_reviewer("codex")
        result = run(["record-design-review", "--approve", "--by", "reviewer-managed", "--session", session_id,
                      "--architect", "architect-1", "--summary", "independent process"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read_status()["design_review"]["decision"], "approved")

    def test_host_recorded_review_from_the_architect_host_session_is_refused(self):
        # A verdict recorded by hand (no --session) from the very session that
        # wrote the proposal is a self-review, whatever --by claims.
        self.propose("architect-1")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_COMPANION_SESSION_ID": "host-session-1"}, clear=True):
            result = run(["record-design-review", "--approve", "--by", "reviewer-host",
                          "--architect", "architect-1", "--summary", "same terminal"], self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("independent host session", result.stdout)
        self.assertIsNone(self.read_status().get("design_review"))

    def test_host_recorded_review_from_another_host_session_is_accepted(self):
        self.propose("architect-1")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "host-session-2", "CODEX_COMPANION_SESSION_ID": ""}, clear=True):
            result = run(["record-design-review", "--approve", "--by", "reviewer-host",
                          "--architect", "architect-1", "--summary", "other terminal"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_actor_managed_reviewer_is_still_refused(self):
        self.propose("architect-1")
        session_id = self._live_reviewer("codex", actor="architect-1")
        result = run(["record-design-review", "--approve", "--by", "architect-1", "--session", session_id,
                      "--architect", "architect-1", "--summary", "self review"], self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("reviewer must differ", result.stdout)


if __name__ == "__main__":
    unittest.main()


class DesignReviewerSelectionIsPersisted(HandsoffTestCase):
    """#145: design_reviewer_selection_view checked the newest live Phase 2
    reviewer against status["design_reviewer_selection"], which nothing ever
    wrote, so Mission Control reported "has no selection metadata" during
    every design review. The launch now persists the selection it made."""

    def setUp(self):
        super().setUp()
        self.lib = lib
        self.init("Selection is persisted")
        criterion = run(["criterion-update", "REQ-001", "--requirement",
                         "[#1] a reviewable outcome"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)

    def _view(self):
        cfg = self.lib.load_config(self.tmp)
        status = self.read_status()
        acceptance = self.read_acceptance()
        return self.lib.design_reviewer_selection_view(cfg, status, acceptance, which=lambda name: "/usr/bin/true")

    def _launch(self, sid="hs-" + "a" * 32, actor="codex-reviewer"):
        return self.lib.create_agent_session(
            self.tmp, role="reviewer", actor=actor, adapter="codex",
            requested_model="default", resolution_source="configured",
            id_factory=lambda: sid, tier="primary", tier_reason="first_review")

    def test_launch_persists_the_selection_and_the_view_has_no_fault(self):
        self._launch()
        status = self.read_status()
        current = status["design_reviewer_selection"]["current"]
        self.assertEqual(current["session_id"], "hs-" + "a" * 32)
        self.assertEqual(current["actor"], "codex-reviewer")
        self.assertEqual(current["adapter"], "codex")
        self.assertEqual(current["tier"], "primary")
        self.assertEqual(current["attempt"], 1)
        self.assertTrue(current["selected_at"])
        kinds = [json.loads(l)["kind"] for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn("design_reviewer_selected", kinds)
        self.assertEqual(self._view()["consistency_errors"], [], "live: no phantom fault")
        self.lib.transition_agent_session(self.tmp, "hs-" + "a" * 32, "running")
        self.assertEqual(self._view()["consistency_errors"], [], "running: still none")
        self.lib.transition_agent_session(self.tmp, "hs-" + "a" * 32, "completed")
        self.assertEqual(self._view()["consistency_errors"], [], "completed: still none")

    def test_a_mismatch_still_reports(self):
        self._launch()
        status = self.read_status()
        status["design_reviewer_selection"]["current"]["session_id"] = "hs-" + "b" * 32
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2))
        errors = self._view()["consistency_errors"]
        self.assertTrue(any("selection metadata names" in e for e in errors), errors)
        status = self.read_status()
        status["design_reviewer_selection"]["current"].update(session_id="hs-" + "a" * 32, actor="someone-else")
        (self.tmp / "handsoff-status.json").write_text(json.dumps(status, indent=2))
        errors = self._view()["consistency_errors"]
        self.assertTrue(any("selection metadata names someone-else" in e for e in errors), errors)
