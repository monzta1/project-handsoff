#!/usr/bin/env python3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402


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

    def test_scope_identity_ignores_display_updates(self):
        acceptance = {"feature": "Ship #31", "criteria": [criterion("REQ-001", "[#31] Track reviews")]}
        items = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        before = lib.work_item_scope_hash(items)
        items[0].update(title="New display title", url="https://new", github_state="closed", notes="observed")
        self.assertEqual(before, lib.work_item_scope_hash(items))
        items[0]["required"] = False
        self.assertNotEqual(before, lib.work_item_scope_hash(items))

    def test_canonical_state_drives_rows_and_global_gates_outrank_done(self):
        acceptance = {"feature": "Ship #31 and #29", "criteria": [
            criterion("REQ-001", "[#31] Track reviews", "passing", ["vr-1"]),
            criterion("REQ-29", "[#29] Render every item"),
        ]}
        acceptance["work_items"] = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        rows = lib.derive_work_items(self.status(), acceptance, self.cfg)
        self.assertEqual({item["id"]: item["status"] for item in rows["items"]},
                         {"issue-29": "not_started", "issue-31": "done"})
        status = self.status()
        status["regression_requests"] = [{"state": "awaiting_approval", "group": "full"}]
        waiting = lib.derive_work_items(status, acceptance, self.cfg)
        self.assertEqual({item["status"] for item in waiting["items"]}, {"awaiting_approval"})

    def test_persisted_registry_exposes_unattributed_required_work(self):
        acceptance = {"feature": "Ship #31 and #29", "criteria": [
            criterion("REQ-001", "[#31] Track reviews", "passing", ["vr-1"]),
            criterion("REQ-U", "No item tag", "passing", ["vr-2"]),
        ]}
        acceptance["work_items"] = lib.derive_work_item_registry(acceptance, self.cfg, now=self.now)
        rows = lib.derive_work_items(self.status(), acceptance, self.cfg)
        unattributed = next(item for item in rows["items"] if item["id"] == "unattributed")
        self.assertTrue(unattributed["required"])
        self.assertEqual(unattributed["status"], "done")
        self.assertTrue(rows["multi"])


if __name__ == "__main__":
    unittest.main()
