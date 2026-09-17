#!/usr/bin/env python3
import sys
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from test_handsoff_supervisor import HandsoffTestCase, run, approve_design_review  # noqa: E402


def criterion(cid, requirement, state="not_tested", evidence=None):
    return {"id": cid, "type": "primary_fix" if cid == "REQ-001" else "supporting",
            "requirement": requirement, "verification": "automated", "tests": ["focused"],
            "evidence": evidence or [], "state": state}


class WorkItemTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).isoformat()
        self.cfg = {"tickets": [
            {"number": 31, "title": "Review convergence", "url": "https://example/31", "status": "not_started"},
            {"number": 29, "title": "Automatic table", "url": "https://example/29", "status": "not_started"},
        ]}

    def status(self):
        return {"updated_at": self.now, "phase_number": 4, "phase": "Implementation",
                "status": "in_progress", "next_action": "Implement", "review_attempts": [],
                "recovery_attempts": [], "regression_requests": [], "active_work_item": None,
                "escalation": None}

    def test_issue_and_ask_identities_derive_without_authored_ticket_status(self):
        acceptance = {"feature": "Ship #31 and #29", "criteria": [
            criterion("REQ-001", "[#31] Track reviews"),
            criterion("REQ-29", "[#29] Render every item"),
            criterion("REQ-X", "[cross] Preserve interactions"),
        ]}
        items = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        self.assertEqual([item["id"] for item in items], ["issue-29", "issue-31", "ask-cross"])
        self.assertEqual(next(item for item in items if item["id"] == "issue-31")["title"], "Review convergence")
        self.assertTrue(all("status" not in item for item in items))

    def test_plain_asks_split_only_on_explicit_separators(self):
        one = lib.derive_work_item_registry({"feature": "fix parser and improve copy", "criteria": []}, {"tickets": []}, now=self.now)
        self.assertEqual(len(one), 1)
        two = lib.derive_work_item_registry({"feature": "fix parser; improve copy", "criteria": []}, {"tickets": []}, now=self.now)
        self.assertEqual(len(two), 2)
        mixed = lib.derive_work_item_registry(
            {"feature": "#31 review convergence; improve docs", "criteria": []},
            self.cfg, now=self.now,
        )
        self.assertEqual([item["id"] for item in mixed], ["issue-31", "ask-improve-docs"])

    def test_explicit_items_override_feature_detection_and_support_mixed_kinds(self):
        items = lib.derive_work_item_registry(
            {"feature": "ignore #29", "criteria": []}, self.cfg, now=self.now,
            explicit_items=["#31 Review convergence", "improve docs"],
        )
        self.assertEqual([item["id"] for item in items], ["issue-31", "ask-improve-docs"])
        self.assertEqual(items[0]["title"], "Review convergence")

    def test_scope_identity_ignores_display_updates(self):
        acceptance = {"feature": "Ship #31", "criteria": [criterion("REQ-001", "[#31] Track reviews")]}
        items = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        before = lib.work_item_scope_hash(items)
        items[0].update(title="New display title", url="https://new", github_state="closed", notes="observed")
        self.assertEqual(before, lib.work_item_scope_hash(items))
        items[0]["required"] = False
        self.assertEqual(before, lib.work_item_scope_hash(items))

    def test_scope_hash_matches_legacy_required_digest(self):
        items = [{"id": "issue-31", "kind": "issue", "number": 31, "required": True}]
        self.assertTrue(lib.scope_hash_matches(
            lib.work_item_scope_hashes(items)["legacy"], items))

    def test_canonical_state_drives_rows_and_global_gates_outrank_done(self):
        acceptance = {"feature": "Ship #31 and #29", "criteria": [
            criterion("REQ-001", "[#31] Track reviews", "passing", ["vr-1"]),
            criterion("REQ-29", "[#29] Render every item"),
        ]}
        acceptance["work_items"] = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        rows = lib.derive_work_items(self.status(), acceptance, self.cfg)
        self.assertEqual({item["id"]: item["status"] for item in rows["items"]},
                         {"issue-29": "not_started", "issue-31": "in_progress"})
        status = self.status()
        status["regression_requests"] = [{"state": "awaiting_approval", "group": "full"}]
        waiting = lib.derive_work_items(status, acceptance, self.cfg)
        self.assertEqual({item["status"] for item in waiting["items"]}, {"awaiting_approval"})
        status = self.status()
        status.update(phase_number=5, phase="Independent review", review_attempts=[{
            "disposition": "open",
        }])
        reviewed = lib.derive_work_items(status, acceptance, self.cfg)
        self.assertEqual({item["status"] for item in reviewed["items"]}, {"in_review"})

    def test_single_item_registry_exposes_unknown_tag_as_unattributed(self):
        acceptance = {"feature": "Ship #31", "criteria": [
            criterion("REQ-001", "[#31] Track reviews", "passing", ["vr-1"]),
            criterion("REQ-U", "[#123] Unknown issue", "passing", ["vr-2"]),
        ]}
        acceptance["work_items"] = [next(item for item in lib.derive_work_item_registry(
            acceptance, self.cfg, now=self.now) if item["id"] == "issue-31")]
        rows = lib.derive_work_items(self.status(), acceptance, self.cfg)
        unattributed = next(item for item in rows["items"] if item["id"] == "unattributed")
        self.assertTrue(unattributed["required"])
        self.assertEqual(unattributed["criteria"], ["REQ-U"])
        self.assertEqual(rows["unattributed_criteria"], ["REQ-U"])


class WorkItemCliTests(HandsoffTestCase):
    def test_metadata_update_survives_decisions(self):
        # REQ-006: metadata edits do not invalidate decisions or scope.
        initialized = run(["init", "Ship issue work", "--item", "#70", "--item", "#71"], cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        criterion = run(["criterion-update", "REQ-001", "--requirement", "[#70] Keep issue work"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        phase_two = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase_two.returncode, 0, phase_two.stdout + phase_two.stderr)
        review = approve_design_review(self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        self.assertIsNotNone(self.advance_to(4))
        before = self.read_status()
        updated = run(["work-item-update", "issue-71", "--optional", "--by", "supervisor",
                       "--notes", "n", "--title", "t"], cwd=self.tmp)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        after = self.read_status()
        self.assertIsNotNone(after["design_approved"])
        self.assertEqual(after["phase_number"], before["phase_number"])
        event = json.loads((self.tmp / "handsoff-events.jsonl").read_text().splitlines()[-1])
        self.assertFalse(event["scope_changed"])

    def test_sync_skips_title_ask_beside_issue_items(self):
        # REQ-005: feature-title asks are skipped beside persisted issue items.
        initialized = run(["init", "Feature without issue refs", "--item", "#70"], cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        synced = run(["work-items-sync", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        self.assertIn("WORK_ITEM_SYNC_SKIPPED: ask-", synced.stdout)
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], ["issue-70"])

    def test_sync_item_appends_explicit_issue(self):
        # REQ-005: an explicit --item deliberately adds the issue.
        initialized = run(["init", "Feature without issue refs", "--item", "#70"], cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#90"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        self.assertIn("issue-90", [item["id"] for item in self.read_acceptance()["work_items"]])

    def test_scope_frozen_after_deployment_approval(self):
        # REQ-006: deployment approval freezes scope-changing syncs.
        initialized = run(["init", "Feature without issue refs", "--item", "#70"], cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["deployment_approved"] = {"by": "pilot", "at": datetime.now(timezone.utc).isoformat()}
        lib.commit(self.tmp, cfg, status=status, event_kind="test_deployment_approved", event_message="test")
        before = [item["id"] for item in self.read_acceptance()["work_items"]]
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#91"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 1)
        self.assertIn("work-item scope is frozen after deployment approval", synced.stdout)
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], before)

    # REQ-005 / REQ-006 (#82): the criteria-transaction path, removal, and the
    # scope definition that makes zero-criteria items bookkeeping only.

    def _init_with_issue(self, feature="Feature without issue refs"):
        initialized = run(["init", feature, "--item", "#70"], cwd=self.tmp)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)

    def test_criterion_transaction_never_appends_title_ask_but_registers_tags(self):
        self._init_with_issue()
        first = run(["criterion-update", "REQ-001", "--requirement", "[#70] Primary work"], cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], ["issue-70"])
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--verification", "manual",
                     "--test", "manual inspection", "--requirement", "[#71] Tagged follow-up"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        ids = [item["id"] for item in self.read_acceptance()["work_items"]]
        self.assertIn("issue-71", ids)
        self.assertFalse([item for item in ids if item.startswith("ask-")], ids)

    def test_remove_zero_criteria_item_keeps_decisions(self):
        self._init_with_issue("Ship issue work")
        criterion = run(["criterion-update", "REQ-001", "--requirement", "[#70] Keep issue work"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#90"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        phase_two = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(phase_two.returncode, 0, phase_two.stdout + phase_two.stderr)
        review = approve_design_review(self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        reached = self.advance_to(4)
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        removed = run(["work-item-remove", "issue-90", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(removed.stdout.strip(), "WORK_ITEM_REMOVED: issue-90")
        status = self.read_status()
        self.assertIsNotNone(status["design_approved"])
        self.assertEqual(status["phase_number"], 4)
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], ["issue-70"])
        event = json.loads((self.tmp / "handsoff-events.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["kind"], event["scope_changed"]), ("work_item_removed", False))
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)

    def test_remove_refused_for_item_with_criteria(self):
        self._init_with_issue()
        criterion = run(["criterion-update", "REQ-001", "--requirement", "[#70] Primary work"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        removed = run(["work-item-remove", "issue-70", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 1)
        self.assertIn("maps to criteria REQ-001", removed.stdout)
        self.assertEqual([item["id"] for item in self.read_acceptance()["work_items"]], ["issue-70"])

    def test_remove_refused_after_deployment_approval(self):
        self._init_with_issue()
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#90"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        cfg = lib.load_config(self.tmp)
        status = self.read_status()
        status["deployment_approved"] = {"by": "pilot", "at": datetime.now(timezone.utc).isoformat()}
        lib.commit(self.tmp, cfg, status=status, event_kind="test_deployment_approved", event_message="test")
        removed = run(["work-item-remove", "issue-90", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(removed.returncode, 1)
        self.assertIn("work-item scope is frozen after deployment approval", removed.stdout)
        self.assertIn("issue-90", [item["id"] for item in self.read_acceptance()["work_items"]])

    def test_scope_hash_accepts_normalized_legacy_digest_and_ignores_unscoped_items(self):
        items = [
            {"id": "issue-70", "kind": "issue", "number": 70, "required": True},
            {"id": "issue-90", "kind": "issue", "number": 90, "required": False},
        ]
        criteria = [{"id": "REQ-001", "requirement": "[#70] Primary work"}]
        legacy_all_required = lib.work_item_scope_hashes(
            [{**item, "required": True} for item in items])["legacy"]
        self.assertTrue(lib.scope_hash_matches(legacy_all_required, items, criteria))
        with_unscoped = lib.work_item_scope_hash(items, criteria)
        without = lib.work_item_scope_hash(items[:1], criteria)
        self.assertEqual(with_unscoped, without)
        self.assertNotEqual(lib.work_item_scope_hash(items), lib.work_item_scope_hash(items[:1]))

    def test_unscoped_required_item_named_in_gate_message(self):
        self._init_with_issue()
        criterion = run(["criterion-update", "REQ-001", "--requirement", "[#70] Primary work"], cwd=self.tmp)
        self.assertEqual(criterion.returncode, 0, criterion.stdout + criterion.stderr)
        synced = run(["work-items-sync", "--by", "supervisor", "--item", "#90"], cwd=self.tmp)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        cfg = lib.load_config(self.tmp)
        rows = lib.derive_work_items(self.read_status(), self.read_acceptance(), cfg)
        row = next(item for item in rows["items"] if item["id"] == "issue-90")
        self.assertEqual(row["status"], "unscoped")
        status = self.read_status()
        status.update(phase_number=8, phase=lib.PHASES[8], status="complete", progress=100)
        errors = lib.compute_errors(status, self.read_acceptance(), cfg)
        messages = [error for error in errors if "issue-90" in error]
        self.assertTrue(messages, errors)
        for message in messages:
            self.assertIn("work-item-remove issue-90", message)
            self.assertIn("unscoped", message)


if __name__ == "__main__":
    unittest.main()
