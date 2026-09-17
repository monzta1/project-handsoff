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


if __name__ == "__main__":
    unittest.main()
