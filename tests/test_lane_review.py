"""Review-lane adoption and its preflight gates."""

import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handsoff_supervisor import HandsoffTestCase, run
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_analyzer
import handsoff_lib as lib


class ReviewLaneTests(HandsoffTestCase):
    def git_commit(self, author="Commit Author <author@example.com>"):
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", author.split(" <")[0]], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", author.split("<")[1][:-1]], cwd=self.tmp, check=True)
        (self.tmp / "implementation.txt").write_text("implemented\n")
        subprocess.run(["git", "add", "implementation.txt"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-qm", "implementation"], cwd=self.tmp, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.tmp, text=True).strip()

    def unusable_author_commit(self):
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        tree = subprocess.check_output(["git", "mktree"], cwd=self.tmp, input="", text=True).strip()
        raw = f"tree {tree}\nauthor  <> 0 +0000\ncommitter Committer <committer@example.com> 0 +0000\n\nunusable author\n"
        return subprocess.check_output(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                                       cwd=self.tmp, input=raw, text=True).strip()

    def prepared(self, author="Commit Author <author@example.com>"):
        self.init("Review adoption")
        for name in ("handsoff-status.json", "handsoff-events.jsonl", "handsoff-verifications.jsonl",
                     ".handsoff-event-head.json"):
            (self.tmp / name).unlink(missing_ok=True)
        return self.git_commit(author)

    def phase_five_review(self, *, second=False):
        ref = self.prepared("Adopted Author <author@example.com>")
        result = run(["init", "Review gates", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if second:
            added = run(["criterion-add", "REQ-002", "--type", "supporting",
                         "--requirement", "review supporting criterion",
                         "--verification", "automated", "--test", "true"], self.tmp)
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        updated = run(["criterion-update", "REQ-001", "--test", "true"], self.tmp)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        verified = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        status = self.read_status()
        acceptance = self.read_acceptance()
        status.update(phase_number=5, phase=lib.PHASES[5], phases_run=[5, 6])
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, acceptance=acceptance,
                   event_kind="test_review_phase_five", event_message="Restore review phase")
        return ref

    def record_approved_review(self):
        status = self.read_status()
        acceptance = self.read_acceptance()
        cfg = lib.load_config(self.tmp)
        lib.open_review_attempt(status, acceptance, cfg, by="independent-reviewer", reviewer="independent-reviewer")
        now = datetime.now(timezone.utc).isoformat()
        attempt = status["review_attempts"][-1]
        attempt.update({"closed_at": now, "reviewer": "independent-reviewer", "disposition": "approved",
                        "tests_executed": "yes", "findings": []})
        profile = lib.audited_agent_profile(cfg, "reviewer")
        status["review"] = {"decision": "approved", "by": "independent-reviewer", "at": now,
                             "acceptance_hash": lib.acceptance_hash(acceptance["criteria"]),
                             "config_hash": lib.config_hash(cfg),
                             "scope_hash": lib.work_item_scope_hash(acceptance.get("work_items", []), acceptance["criteria"]),
                             "implementer_profile": profile, "reviewer_profile": profile,
                             "tests_executed": "yes", "profiles_distinct": True,
                             **lib.rules_binding(self.tmp, cfg),
                             "checklist": {"symptom_reproduced": "not_applicable", "symptom_resolved": "yes",
                                           "all_criteria_verified": "yes", "evidence_attached": "yes"}}
        status["reviewed_by"] = "independent-reviewer"
        lib.commit(self.tmp, cfg, status=status, event_kind="test_review_approved",
                   event_message="Approve review fixture")

    def test_review_adoption_records_lane_waivers_and_provenance(self):
        ref = self.prepared()
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = json.loads((self.tmp / "handsoff-status.json").read_text())
        self.assertEqual(status["lane"], "review")
        self.assertEqual(status["phase_number"], 5)
        self.assertEqual(status["phases_run"], [5, 6])
        self.assertEqual(status["phases_waived"], [1, 2, 3, 4, 7, 8])
        adopted = status["implementation_adopted"]
        self.assertEqual(adopted["ref"], ref)
        self.assertEqual(adopted["adopting_actor"], "pilot")

    def test_review_lane_without_criteria_writes_nothing(self):
        ref = self.git_commit() if False else None
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", "HEAD", "--by", "pilot"], self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review lane", result.stdout.lower())
        self.assertFalse((self.tmp / "handsoff-status.json").exists())

    def test_review_adoption_refuses_unusable_author_before_writing(self):
        self.init("Review adoption")
        acceptance = self.tmp / "handsoff-acceptance.json"
        acceptance_before = acceptance.read_bytes()
        for name in ("handsoff-status.json", "handsoff-events.jsonl", "handsoff-verifications.jsonl",
                     ".handsoff-event-head.json"):
            (self.tmp / name).unlink(missing_ok=True)
        ref = self.unusable_author_commit()
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit has no usable author identity", result.stdout + result.stderr)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())
        self.assertEqual(acceptance.read_bytes(), acceptance_before)
        self.assertFalse((self.tmp / "handsoff-events.jsonl").exists())
        self.assertFalse((self.tmp / "handsoff-verifications.jsonl").exists())
        self.assertFalse((self.tmp / ".handsoff-event-head.json").exists())

    def test_author_and_adopter_are_recorded_for_independence_check(self):
        ref = self.prepared("Adopted Author <author@example.com>")
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        adopted = json.loads((self.tmp / "handsoff-status.json").read_text())["implementation_adopted"]
        self.assertEqual(adopted["commit_author"], "Adopted Author <author@example.com>")

    def test_reviewer_equal_to_adopting_actor_is_refused(self):
        ref = self.prepared("Commit Author <author@example.com>")
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        refused = run(["record-review", "--by", "pilot"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("adopting actor", refused.stdout + refused.stderr)

    def test_review_lane_gates_and_independence_refusals_preserve_ledgers(self):
        ref = self.prepared("Adopted Author <author@example.com>")
        result = run(["init", "Review adoption", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        names = ("handsoff-status.json", "handsoff-acceptance.json",
                 "handsoff-verifications.jsonl", "handsoff-events.jsonl")
        snapshot = lambda: {name: ((self.tmp / name).read_bytes() if (self.tmp / name).exists() else None)
                            for name in names}
        before = snapshot()
        for command in (("advance", "4", "40"), ("deployment-gate", "--approve", "--by", "pilot"),
                        ("verify-live", "--by", "pilot"), ("advance", "6", "60"),
                        ("record-review", "--by", "Adopted Author <author@example.com>")):
            refused = run(list(command), self.tmp)
            self.assertNotEqual(refused.returncode, 0, command)
            self.assertIn("review", (refused.stdout + refused.stderr).lower())
            self.assertEqual(before, snapshot(), command)

    def test_review_lane_phase_six_writes_complete_review_report(self):
        ref = self.prepared("Adopted Author <author@example.com>")
        result = run(["init", "Review report", "--lane", "review", "--adopt", ref, "--by", "pilot"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(run(["criterion-update", "REQ-001", "--baseline", "not_applicable",
                              "--baseline-reason", "review adopts an existing implementation"], self.tmp).returncode, 0)
        self.assertEqual(run(["criterion-update", "REQ-001", "--verification", "manual"], self.tmp).returncode, 0)
        self.assertEqual(run(["record-evidence", "REQ-001", "--kind", "manual",
                              "--description", "reviewed adopted implementation", "--by", "pilot"], self.tmp).returncode, 0)
        acceptance = self.read_acceptance()
        acceptance.pop("work_items", None)
        acceptance["criteria"][0]["state"] = "passing"
        status = self.read_status()
        status["requirement_coverage"] = lib.coverage_for(acceptance["criteria"], False)
        status.update(phase_number=5, phase=lib.PHASES[5], phases_run=[5, 6])
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, acceptance=acceptance,
                   event_kind="test_review_fixture_restored", event_message="Restore review phase")
        cfg = lib.load_config(self.tmp)
        lib.open_review_attempt(status, self.read_acceptance(), cfg,
                                by="independent-reviewer", reviewer="independent-reviewer")
        now = datetime.now(timezone.utc).isoformat()
        attempt = status["review_attempts"][-1]
        attempt.update({"closed_at": now, "reviewer": "independent-reviewer",
                        "disposition": "approved", "tests_executed": "yes", "findings": [
            {"code": "incorrect_implementation", "summary": "first finding"},
            {"code": "acceptance_not_met", "summary": "second finding"},
        ]})
        profile = lib.audited_agent_profile(cfg, "reviewer")
        status["review"] = {"decision": "approved", "by": "independent-reviewer", "at": now,
                             "acceptance_hash": lib.acceptance_hash(self.read_acceptance()["criteria"]),
                             "config_hash": lib.config_hash(cfg),
                             "scope_hash": lib.work_item_scope_hash([], self.read_acceptance()["criteria"]),
                             "implementer_profile": profile, "reviewer_profile": profile,
                             "tests_executed": "yes", "profiles_distinct": True,
                             **lib.rules_binding(self.tmp, cfg),
                             "checklist": {"symptom_reproduced": "not_applicable", "symptom_resolved": "yes",
                                           "all_criteria_verified": "yes", "evidence_attached": "yes"}}
        status["reviewed_by"] = "independent-reviewer"
        lib.commit(self.tmp, cfg, status=status,
                   event_kind="test_review_recorded", event_message="Test review recorded")
        advanced = run(["advance", "6", "100"], self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        final = self.read_status()
        self.assertEqual(final["status"], "review_complete")
        self.assertEqual(final["review_report"], "review_report.md")
        report = (self.tmp / "review_report.md").read_text()
        for text in ("approved", "independent-reviewer", ref,
                     final["implementation_adopted"]["sha"], "yes",
                     "REQ-001", "state:", "incorrect_implementation", "first finding",
                     "acceptance_not_met", "second finding"):
            self.assertIn(text, report)

    def test_review_phase_six_evidence_gate_refusal_is_transactional(self):
        self.phase_five_review(second=True)
        names = ("handsoff-status.json", "handsoff-acceptance.json",
                 "handsoff-verifications.jsonl", "handsoff-events.jsonl")
        before = {name: (self.tmp / name).read_bytes() for name in names}
        refused = run(["advance", "6", "60"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("evidence", refused.stdout + refused.stderr)
        self.assertEqual(before, {name: (self.tmp / name).read_bytes() for name in names})

    def test_review_phase_six_requires_complete_anchor(self):
        self.phase_five_review()
        status = self.read_status()
        status["implementation_adopted"].pop("commit_author")
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="test_incomplete_anchor", event_message="Remove anchor author")
        refused = run(["advance", "6", "60"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("review anchor gate", refused.stdout + refused.stderr)

    def test_review_progress_gate_requires_required_work_item(self):
        self.phase_five_review()
        self.record_approved_review()
        acceptance = self.read_acceptance()
        acceptance["work_items"] = [{"id": "issue-999", "kind": "issue", "number": 999,
                                      "title": "unfinished", "url": "", "required": True,
                                      "github_state": None, "github_checked_at": None,
                                      "created_at": "2026-01-01T00:00:00+00:00",
                                      "updated_at": "2026-01-01T00:00:00+00:00", "notes": ""}]
        acceptance["criteria"][0]["requirement"] = "[#999] review criterion"
        status = self.read_status()
        status["requirement_coverage"] = lib.coverage_for(acceptance["criteria"], False)
        status["progress"] = 95
        status["review"]["scope_hash"] = lib.work_item_scope_hash(acceptance["work_items"], acceptance["criteria"])
        status["review"]["acceptance_hash"] = lib.acceptance_hash(acceptance["criteria"])
        status["work_item_delivery"] = lib.new_work_item_delivery(acceptance["work_items"], "full")
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, acceptance=acceptance,
                   event_kind="test_unfinished_item", event_message="Restore unfinished item")
        refused = run(["advance", "6", "95"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("progress gate", refused.stdout + refused.stderr)

    def test_review_complete_anchor_advances_and_terminalizes(self):
        self.phase_five_review()
        self.record_approved_review()
        advanced = run(["advance", "6", "60"], self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["status"], "review_complete")

    def test_archive_analysis_applies_rules_only_to_their_lane_and_is_report_only(self):
        archive = self.tmp / "archive"
        archive.mkdir()
        design = archive / "design.json"
        design.write_text(json.dumps({"status": {"lane": "design", "phases_run": [1, 2, 3]}}))
        review = archive / "review.json"
        review.write_text(json.dumps({"status": {"lane": "review", "phases_run": [5, 6],
                                                    "review": {"by": "reviewer"}}}))
        before = {path: path.read_bytes() for path in archive.glob("*.json")}
        findings = [{"rule": "R3-design", "title": "design rule"},
                    {"rule": "R4", "title": "implementation review rule"},
                    {"rule": "R10-review", "title": "review rule"}]
        design_findings = handsoff_analyzer.applicable_findings(json.loads(design.read_text()), findings)
        self.assertNotIn("R4", {item["rule"] for item in design_findings})
        self.assertNotIn("R10-review", {item["rule"] for item in design_findings})
        self.assertEqual(handsoff_analyzer.applicable_findings(
                         json.loads(review.read_text()), findings), [{"rule": "R4", "title": "implementation review rule"}])
        missing = archive / "missing.json"
        missing.write_text(json.dumps({"status": {"lane": "review", "phases_run": [5, 6]}}))
        missing_findings = handsoff_analyzer.scan(archive)
        self.assertTrue(any(item["rule"] == "R10" and "no review record" in item["text"]
                            for item in missing_findings))
        self.assertEqual(before, {path: path.read_bytes() for path in archive.glob("*.json") if path != missing})


if __name__ == "__main__":
    unittest.main()
