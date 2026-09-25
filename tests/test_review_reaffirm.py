"""Field-note defect 3: record-review --reaffirm and session-result-adopt re-adoption."""
from __future__ import annotations

import json
import shutil

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin


class ReviewReaffirmTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _reach_approved_review(self, reviewer="test-reviewer"):
        self.init("Reaffirm fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        review = run(["record-review", "--by", reviewer, "--tests-executed", "yes"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        status = self.read_status()
        self.assertIsNotNone(status["review"])
        self.assertEqual(status["review_round"], 1)
        return status

    def _evidence_refresh(self):
        """verify again on an unchanged spec: the review is revoked, the design is not."""
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        self.assertIsNone(status["review"])
        self.assertIsNotNone(status["design_approved"])
        return status

    def test_reaffirm_rebinds_without_spending_budget(self):
        before = self._reach_approved_review()
        old_hash = before["review"]["acceptance_hash"]
        self._evidence_refresh()
        result = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INDEPENDENT_REVIEW_REAFFIRMED", result.stdout)
        status = self.read_status()
        self.assertEqual(status["reviewed_by"], "test-reviewer")
        self.assertEqual(status["review_round"], 1)
        self.assertEqual(len(status["review_attempts"]), 1)
        attempt = status["review_attempts"][0]
        self.assertEqual(attempt["disposition"], "approved")
        self.assertIsNotNone(attempt["reaffirmed_at"])
        self.assertEqual(attempt["acceptance_hash"], status["review"]["acceptance_hash"])
        self.assertNotEqual(status["review"]["acceptance_hash"], old_hash)
        self.assertEqual(status["review"]["reaffirmed_attempt"], 1)
        self.assertEqual(status["review"]["reaffirmed_from_acceptance_hash"], old_hash)
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        reaffirmed = [e for e in events if e.get("kind") == "review_reaffirmed"]
        self.assertEqual(len(reaffirmed), 1)
        self.assertEqual(reaffirmed[0]["attempt"], 1)
        self.assertEqual(reaffirmed[0]["previous_acceptance_hash"], old_hash)
        # the run can go on: Phase 6 accepts the reaffirmed review
        advanced = self.advance_to(6, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)

    def test_reaffirm_refusals_are_named(self):
        self._reach_approved_review()
        current = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(current.returncode, 1)
        self.assertIn("review is current; nothing to reaffirm", current.stdout)
        self._evidence_refresh()
        other = run(["record-review", "--reaffirm", "--by", "someone-else"], cwd=self.tmp)
        self.assertEqual(other.returncode, 1)
        self.assertIn("reaffirm must come from the reviewer of attempt 1", other.stdout)
        # a real spec change moves the design hash: refused, a fresh review is needed
        changed = run(["criterion-update", "REQ-001", "--requirement", "A genuinely different requirement", "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        for args in (["verify", "--criterion", "REQ-001", "--by", "test-implementer"],):
            result = run(args, cwd=self.tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_id = json.loads(result.stdout)["criteria"]["REQ-001"]["run_id"]
        symptom = run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(symptom.returncode, 0, symptom.stdout + symptom.stderr)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        stale = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("design hash changed", stale.stdout)

    def test_reaffirm_refused_without_approved_attempt_or_with_open_attempt(self):
        self.init("Reaffirm fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        nothing = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(nothing.returncode, 1)
        self.assertIn("no approved review attempt to reaffirm", nothing.stdout)
        opened = run(["review-attempt-start", "--by", "test-supervisor", "--reviewer", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        busy = run(["record-review", "--reaffirm", "--by", "test-reviewer"], cwd=self.tmp)
        self.assertEqual(busy.returncode, 1)
        self.assertIn("a review attempt is open", busy.stdout)

    def test_session_result_readopt_after_evidence_refresh(self):
        """An adopted approved verdict whose review was revoked replays as a reaffirmation."""
        self.init("Readopt fixture")
        self.set_criterion_state("passing", resolved=True)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        status = self.read_status()
        session_id = "hs-" + "ab" * 16
        status.setdefault("agent_sessions", {})[session_id] = {
            "session_id": session_id, "role": "reviewer", "adapter": "codex", "requested_model": "default",
            "reported_model": None, "resolution_source": "configured", "actor": "codex-reviewer", "state": "completed",
            "started_at": "2026-09-18T00:00:00+00:00", "running_at": "2026-09-18T00:00:00+00:00",
            "ended_at": "2026-09-18T00:01:00+00:00", "exit_code": 0, "phase_number": 5, "packet_id": None,
            "design_hash": None, "tier": None,
            "result": {"kind": "review", "recorded_at": "2026-09-18T00:01:00+00:00", "adopted_at": None, "adopted_by": None,
                       "payload": {"decision": "approved", "summary": "ok", "findings": [], "symptom_reproduced": "not_applicable", "tests_executed": "yes"}},
        }
        status["current_agent_sessions"] = {"reviewer": session_id}
        self._write_status_unaudited(status)
        first = run(["session-result-adopt", "--session", session_id, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIsNotNone(self.read_status()["review"])
        again = run(["session-result-adopt", "--session", session_id, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(again.returncode, 1)
        self.assertIn("result is already adopted", again.stdout)
        self._evidence_refresh()
        readopt = run(["session-result-adopt", "--session", session_id, "--by", "test-supervisor"], cwd=self.tmp)
        self.assertEqual(readopt.returncode, 0, readopt.stdout + readopt.stderr)
        status = self.read_status()
        self.assertEqual(status["reviewed_by"], "codex-reviewer")
        self.assertEqual(status["review_round"], 1)
        result = status["agent_sessions"][session_id]["result"]
        self.assertIsNotNone(result["adopted_at"])
        self.assertEqual(len(result["readoptions"]), 1)
        self.assertEqual(result["readoptions"][0]["by"], "test-supervisor")
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(1 for e in events if e.get("kind") == "review_reaffirmed"), 1)

    def _write_status_unaudited(self, status):
        """Fixture only: plant a managed session record the way the launcher would,
        through the library commit so the ledger chain stays intact."""
        import sys
        sys.path.insert(0, str(BIN))
        import handsoff_lib as lib
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="agent_session_started",
                       event_message="fixture reviewer session", by="test-supervisor")
