#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
from tests.fixture_state import force_acceptance


class ReviewAttemptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-review-attempts-"))
        shutil.copy(ROOT / "handsoff.toml", self.root / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        result = self.run_cli("init", "Review #31")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=20,
        )

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def commit_status(self, status, kind="test_setup"):
        cfg = lib.load_config(self.root)
        with lib.project_lock(self.root):
            lib.commit(self.root, cfg, status=status, event_kind=kind, event_message=kind)

    def test_reviewer_session_automatically_opens_structured_attempt(self):
        status = self.read("handsoff-status.json")
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        self.commit_status(status)
        session = lib.create_agent_session(
            self.root, role="reviewer", actor="codex-reviewer-r1", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        status = self.read("handsoff-status.json")
        self.assertEqual(status["review_round"], 1)
        self.assertEqual(status["legacy_review_round_offset"], 0)
        self.assertEqual(len(status["review_attempts"]), 1)
        attempt = status["review_attempts"][0]
        self.assertEqual(attempt["session_ids"], [session["session_id"]])
        self.assertEqual(attempt["disposition"], "open")
        self.assertEqual(attempt["acceptance_hash"], lib.acceptance_hash(self.read("handsoff-acceptance.json")["criteria"]))
        self.assertEqual(lib.validate_status_schema(status), [])

    def test_cap_refuses_fourth_and_human_override_permits_one(self):
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        cfg = lib.load_config(self.root)
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        for _ in range(3):
            attempt = lib.open_review_attempt(status, acceptance, cfg, by="reviewer")
            attempt["disposition"] = "changes_requested"
            attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        self.commit_status(status)
        refused = self.run_cli("review-attempt-start", "--by", "reviewer")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("REVIEW_ATTEMPT_REFUSED", refused.stdout)
        blocked = self.read("handsoff-status.json")
        self.assertEqual(blocked["escalation"]["kind"], "review_cap_exhausted")
        self.assertEqual(blocked["review_round"], 3)
        override = self.run_cli("review-cap-override", "--by", "moncy", "--reason", "one final review")
        self.assertEqual(override.returncode, 0, override.stdout + override.stderr)
        opened = self.run_cli("review-attempt-start", "--by", "reviewer")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        self.assertEqual(self.read("handsoff-status.json")["review_round"], 4)

    def test_legacy_offset_and_stale_acceptance_are_fail_closed(self):
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        cfg = lib.load_config(self.root)
        status.pop("review_attempts")
        status.pop("review_cap_overrides")
        status.pop("legacy_review_round_offset")
        status.pop("escalation")
        status["review_round"] = 100
        lib.migrate_review_ledger(status)
        self.assertEqual(status["legacy_review_round_offset"], 100)
        self.assertEqual(lib.effective_review_cap(status, cfg), 100)
        status["review_cap_overrides"].append({
            "override_id": "ho-" + "1" * 32, "by": "moncy",
            "at": datetime.now(timezone.utc).isoformat(), "reason": "one more",
            "config_hash": lib.config_hash(cfg), "review_round_at_grant": 100,
        })
        attempt = lib.open_review_attempt(status, acceptance, cfg, by="reviewer")
        self.assertEqual(attempt["attempt"], 101)
        acceptance["criteria"][0]["requirement"] = "changed"
        self.assertTrue(lib.abandon_stale_review_attempt(status, acceptance))
        self.assertEqual(attempt["disposition"], "abandoned")
        self.assertEqual(lib.validate_status_schema(status), [])

    def test_managed_cap_refusal_does_not_create_a_ghost_session(self):
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        cfg = lib.load_config(self.root)
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        for _ in range(3):
            attempt = lib.open_review_attempt(status, acceptance, cfg, by="reviewer")
            attempt["disposition"] = "changes_requested"
            attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        self.commit_status(status)

        with self.assertRaisesRegex(lib.HandsoffError, "budget exhausted"):
            lib.create_agent_session(
                self.root, role="reviewer", actor="codex-reviewer-r4", adapter="codex",
                requested_model="default", resolution_source="configured",
            )
        refused = self.read("handsoff-status.json")
        self.assertFalse(any(session.get("state") == "launching"
                             for session in (refused.get("agent_sessions") or {}).values()))
        self.assertNotIn("reviewer", refused.get("current_agent_sessions") or {})

        override = self.run_cli("review-cap-override", "--by", "moncy", "--reason", "one final review")
        self.assertEqual(override.returncode, 0, override.stdout + override.stderr)
        session = lib.create_agent_session(
            self.root, role="reviewer", actor="codex-reviewer-r4", adapter="codex",
            requested_model="default", resolution_source="configured",
        )
        self.assertEqual(session["state"], "launching")

    def test_evidence_during_review_refreshes_attempt_and_remains_closable(self):
        acceptance = self.read("handsoff-acceptance.json")
        acceptance["criteria"][0].update(
            verification="manual", tests=[], evidence=[], state="not_tested",
        )
        cfg = lib.load_config(self.root)
        # A manual criterion with neither tests nor evidence is the starting
        # state this test needs and one the engine refuses to write, so the
        # registry is placed on disk rather than committed.
        force_acceptance(self.root, cfg, acceptance)
        status = self.read("handsoff-status.json")
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        with lib.project_lock(self.root):
            attempt = lib.open_review_attempt(
                status, acceptance, cfg, by="reviewer", reviewer="reviewer",
            )
            lib.commit(self.root, cfg, status=status, event_kind="review_attempt_opened",
                       event_message="Test review opened", attempt_id=attempt["attempt_id"])
        before = self.read("handsoff-status.json")["review_attempts"][-1]

        evidence = self.run_cli(
            "record-evidence", "REQ-001", "--kind", "manual",
            "--description", "Reviewer observed the focused behavior", "--by", "observer",
        )
        self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        attempt = status["review_attempts"][-1]
        self.assertEqual(attempt["attempt_id"], before["attempt_id"])
        self.assertEqual(attempt["disposition"], "open")
        self.assertEqual(attempt["acceptance_hash"], lib.acceptance_hash(acceptance["criteria"]))

        closed = self.run_cli(
            "record-review-findings", "--by", "reviewer",
            "--finding", "other: focused follow-up requested",
        )
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual(self.read("handsoff-status.json")["review_attempts"][-1]["disposition"],
                         "changes_requested")

    def test_negative_review_can_close_a_stale_attempt_without_granting_approval(self):
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40,
                      requires_design_review=False, requires_design_approval=False)
        lib.open_review_attempt(status, acceptance, lib.load_config(self.root),
                                by="reviewer", reviewer="reviewer")
        self.commit_status(status)
        acceptance["criteria"][0]["state"] = "not_tested"
        cfg = lib.load_config(self.root)
        status = self.read("handsoff-status.json")
        lib.sync_coverage(status, acceptance)
        with lib.project_lock(self.root):
            lib.commit(self.root, cfg, status=status, acceptance=acceptance,
                       event_kind="evidence_fixture", event_message="Evidence-only fixture")

        closed = self.run_cli(
            "record-review-findings", "--by", "reviewer",
            "--finding", "incorrect_implementation: stale evidence state needs follow-up",
        )
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        final = self.read("handsoff-status.json")
        self.assertEqual(final["review_attempts"][-1]["disposition"], "changes_requested")
        self.assertIsNone(final.get("reviewed_by"))

    def test_review_findings_cannot_bypass_phase_or_proposed_state_gates(self):
        finding = (
            "record-review-findings", "--by", "reviewer",
            "--finding", "incorrect_implementation: must remain fail closed",
        )
        early = self.run_cli(*finding)
        self.assertNotEqual(early.returncode, 0)
        self.assertIn("Phase 4 or later", early.stdout)
        self.assertEqual(self.read("handsoff-status.json")["review_round"], 0)

        status = self.read("handsoff-status.json")
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40)
        self.commit_status(status)
        invalid = self.run_cli(*finding)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("design", invalid.stdout)
        final = self.read("handsoff-status.json")
        self.assertEqual(final["review_round"], 0)
        self.assertEqual(final["phase_number"], 4)


if __name__ == "__main__":
    unittest.main()
