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


class GovernanceCrossTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-cross-"))
        for name in ("handsoff.toml", ".gitignore"):
            shutil.copy(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Cross Test"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)
        result = self.cli("init", "Cross governance #31 and #28")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=20)

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def request(self):
        result = self.cli("regression-request", "--group", "python-full", "--by", "codex-supervisor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.read("handsoff-status.json")["regression_requests"][-1]

    def test_recovery_skips_pending_regression_gate(self):
        self.request()
        status = self.read("handsoff-status.json")
        cfg = lib.load_config(self.root)
        assessment = lib.recovery_assessment(status, cfg, {}, lib.read_events(self.root, cfg))
        self.assertEqual(assessment["state"], "not_applicable")
        self.assertEqual(assessment["reason"], "regression_pending")

    def test_new_managed_session_invalidates_accepted_regression(self):
        item = self.request()
        accepted = self.cli("regression-decide", "--request-id", item["request_id"],
                            "--accept", "--by", "Mission-Control-Pilot")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        lib.create_agent_session(self.root, role="implementer", actor="codex-implementer-r2",
                                 adapter="codex", requested_model="default", resolution_source="configured")
        final = self.read("handsoff-status.json")["regression_requests"][-1]
        self.assertEqual(final["state"], "invalidated")
        self.assertIsNotNone(final["completed_at"])

    def test_table_global_states_and_audit_integrity(self):
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        cfg = lib.load_config(self.root)
        status["status"] = "blocked"
        status["next_action"] = "Operator acknowledgement required"
        rows = lib.derive_work_items(status, acceptance, cfg)
        self.assertEqual({row["status"] for row in rows["items"]}, {"blocked"})
        self.assertEqual(lib.verify_event_log(self.root, cfg), [])

    def test_init_repeatable_items_and_criterion_mutations_keep_registry_in_sync(self):
        other = Path(tempfile.mkdtemp(prefix="handsoff-items-init-"))
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copy(ROOT / "handsoff.toml", other / "handsoff.toml")
        shutil.copytree(ROOT / "schemas", other / "schemas")
        init = subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(other),
             "init", "ignored #99", "--item", "#31 Review convergence", "--item", "improve docs"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        initialized = json.loads((other / "handsoff-acceptance.json").read_text())
        self.assertEqual([item["id"] for item in initialized["work_items"]],
                         ["issue-31", "ask-improve-docs"])

        untagged = self.cli("criterion-add", "REQ-UNTAGGED", "--type", "supporting",
                            "--requirement", "No owner", "--verification", "automated",
                            "--test", "python3 tests/test_work_items.py")
        self.assertEqual(untagged.returncode, 0, untagged.stdout + untagged.stderr)
        self.assertIn("WORK_ITEM_WARNING", untagged.stdout)
        tagged = self.cli("criterion-add", "REQ-99", "--type", "supporting",
                          "--requirement", "[#99] New promise", "--verification", "automated",
                          "--test", "python3 tests/test_work_items.py")
        self.assertEqual(tagged.returncode, 0, tagged.stdout + tagged.stderr)
        self.assertIn("issue-99", {item["id"] for item in self.read("handsoff-acceptance.json")["work_items"]})

    def test_scope_bootstrap_clears_decisions_without_authenticated_approval(self):
        cfg = lib.load_config(self.root)
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        acceptance.pop("work_items")
        digest = lib.design_hash(acceptance["criteria"])
        config_digest = lib.config_hash(cfg)
        now = datetime.now(timezone.utc).isoformat()
        status["design_review"] = {
            "at": now, "by": "reviewer", "architect": "architect", "summary": "approved",
            "decision": "approved", "design_hash": digest, "config_hash": config_digest,
        }
        status["design_approved"] = {
            "at": now, "by": "pilot", "architect": "architect", "summary": "approved",
            "design_hash": digest, "config_hash": config_digest, "redesigns_settled_work": None,
        }
        with lib.project_lock(self.root):
            lib.commit(self.root, cfg, status=status, acceptance=acceptance,
                       event_kind="test_scope_legacy", event_message="legacy scope fixture")
        synced = self.cli("work-items-sync", "--by", "supervisor")
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        final = self.read("handsoff-status.json")
        self.assertIsNone(final["design_review"])
        self.assertIsNone(final["design_approved"])

    def _legacy_scope_fixture(self):
        cfg = lib.load_config(self.root)
        status = self.read("handsoff-status.json")
        acceptance = self.read("handsoff-acceptance.json")
        acceptance.pop("work_items")
        acceptance["criteria"][0]["requirement"] = "[#31] Review convergence"
        acceptance["criteria"].append({
            "id": "REQ-028", "type": "supporting", "requirement": "[#28] Regression gate",
            "verification": "automated", "tests": ["python3 tests/test_governance_cross.py"],
            "evidence": [], "state": "failing",
        })
        digest = lib.design_hash(acceptance["criteria"])
        config_digest = lib.config_hash(cfg)
        now = datetime.now(timezone.utc).isoformat()
        status["design_review"] = {
            "at": now, "by": "reviewer", "architect": "architect", "summary": "approved",
            "decision": "approved", "design_hash": digest, "config_hash": config_digest,
        }
        status["design_approved"] = {
            "at": now, "by": "pilot", "architect": "architect", "summary": "approved",
            "design_hash": digest, "config_hash": config_digest, "redesigns_settled_work": None,
        }
        with lib.project_lock(self.root):
            lib.commit(self.root, cfg, status=status, acceptance=acceptance,
                       event_kind="design_approved", event_message="Authenticated legacy approval")
        return cfg

    def test_scope_bootstrap_accepts_an_intact_authenticated_legacy_approval(self):
        self._legacy_scope_fixture()
        synced = self.cli("work-items-sync", "--by", "supervisor")
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        final = self.read("handsoff-status.json")
        expected = lib.work_item_scope_hash(self.read("handsoff-acceptance.json")["work_items"])
        self.assertEqual(final["design_review"]["scope_hash"], expected)
        self.assertEqual(final["design_approved"]["scope_hash"], expected)

    def test_scope_bootstrap_refuses_tampered_status_or_event_chain(self):
        cfg = self._legacy_scope_fixture()
        status_path = self.root / "handsoff-status.json"
        status = self.read("handsoff-status.json")
        status["next_action"] = "unlogged edit"
        status_path.write_text(json.dumps(status))
        tampered_status = self.cli("work-items-sync", "--by", "supervisor")
        self.assertNotEqual(tampered_status.returncode, 0)
        self.assertIn("event log", tampered_status.stdout)

        # Recreate the fixture in a separate project so no repair path can
        # accidentally hide the first tamper from this second assertion.
        other = Path(tempfile.mkdtemp(prefix="handsoff-cross-events-"))
        self.addCleanup(shutil.rmtree, other, True)
        for name in ("handsoff.toml", ".gitignore"):
            shutil.copy(ROOT / name, other / name)
        shutil.copytree(ROOT / "schemas", other / "schemas")
        init = subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(other),
             "init", "Cross governance #31 and #28"], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        events = other / "handsoff-events.jsonl"
        events.write_text(events.read_text().replace('"kind":"initialized"', '"kind":"tampered"', 1))
        rejected = subprocess.run(
            [sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(other),
             "work-items-sync", "--by", "supervisor"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("event log", rejected.stdout)

    def test_scope_sync_refuses_missing_event_log_and_head(self):
        cfg = lib.load_config(self.root)
        lib.event_log_path(self.root, cfg).unlink()
        lib.event_head_path(self.root).unlink()
        rejected = self.cli("work-items-sync", "--by", "supervisor")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("event log is missing or empty", rejected.stdout)


if __name__ == "__main__":
    unittest.main()
