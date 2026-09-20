"""#170: a review certifies the rules it ran under. The rules set (config,
hooks, the reviewer prompt, the rule files, the engine version) is hashed
onto reviews and approvals; a change afterwards is named and refused;
decisions recorded before the field existed stay valid."""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_cli as cli  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class RulesSetTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        import shutil
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        # the dogfood config waives the Pilot's design click (#159); this
        # fixture wants the approval recorded so its binding can be checked
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("require_design_approval = false", "require_design_approval = true")
                        .replace("deployment_requires_explicit_approval = false", "deployment_requires_explicit_approval = true"))

    def _reviewed(self):
        self.init("Rules binding fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(6, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _errors(self):
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        return lib.compute_errors(self.read_status(), self.read_acceptance(), cfg, verifications=records,
                                  verification_problems=problems, root=self.tmp)

    def test_the_set_covers_the_documented_files_and_never_env(self):
        entries = lib.rules_set_entries(self.tmp)
        for key in lib.RULES_SET_PROJECT_FILES:
            self.assertIn(key, entries)
        self.assertIsNotNone(entries["handsoff.toml"])
        self.assertIsNone(entries[".claude/settings.json"], "absent files hash as absent")
        self.assertIn("engine:prompts/reviewer.md", entries)
        self.assertIn("engine:reviewer-launch-phase-1.json", entries)
        self.assertEqual(entries["engine:version"], "v0.3.48")
        self.assertFalse(any(".env" in key for key in entries))
        (self.tmp / ".env").write_text("SECRET=abc\n")
        before = lib.rules_set_hash(self.tmp)
        (self.tmp / ".env").write_text("SECRET=xyz\n")
        self.assertEqual(lib.rules_set_hash(self.tmp), before, ".env is never read")
        for value in entries.values():
            self.assertNotIn("SECRET", str(value))

    def test_a_hook_edit_after_review_refuses_naming_the_file_and_an_unrelated_edit_does_not(self):
        self._reviewed()
        review = self.read_status()["review"]
        self.assertEqual(review["rules_hash"], lib.rules_set_hash(self.tmp))
        self.assertIn("handsoff.toml", review["rules_entries"])
        self.assertFalse([e for e in self._errors() if "rules set" in e])
        (self.tmp / "notes.md").write_text("unrelated\n")
        self.assertFalse([e for e in self._errors() if "rules set" in e])
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(rm:*)"]}}')
        errors = [e for e in self._errors() if "rules set" in e]
        self.assertIn("review gate: the rules set changed since it was recorded (.claude/settings.json); record it again", errors)
        self.assertFalse([e for e in errors if e.startswith("design gate")], "the design approval records, the review compares")
        self.assertFalse([e for e in errors if "notes.md" in e])
        # doctor names it too
        report = cli.doctor(self.tmp, skip_preflight=True)
        self.assertIn("rules set changed since the last review: .claude/settings.json", report["rules_set"])
        self.assertIn("rules-set-changed: .claude/settings.json", report["warnings"])
        # a fresh review re-binds and clears it (evidence re-verified on the changed tree first)
        r = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["record-review", "--by", "reviewer-2"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([e for e in self._errors() if "rules set" in e])
        self.assertIsNone(cli.doctor(self.tmp, skip_preflight=True)["rules_set"])

    def test_deployment_approval_and_design_approval_bind_too_and_the_switch_off_records_only(self):
        self.init("Rules binding gates")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "Mission Control Pilot"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        status = self.read_status()
        self.assertEqual(status["deployment_approved"]["rules_hash"], lib.rules_set_hash(self.tmp))
        self.assertEqual(status["design_approved"]["rules_hash"], lib.rules_set_hash(self.tmp))
        (self.tmp / "AGENTS.md").write_text("# new rules for agents\n")
        labels = {e.split(":")[0] for e in self._errors() if "rules set changed" in e}
        self.assertEqual(labels, {"review gate"})
        self.assertIn("rules_hash", self.read_status()["design_approved"], "recorded on the design approval, compared at the review")
        # the deployment gate speaks at Phase 8
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        proposed = self.read_status()
        proposed["phase_number"], proposed["phase"], proposed["progress"], proposed["status"] = 8, lib.PHASES[8], 100, "complete"
        errors = lib.compute_errors(proposed, self.read_acceptance(), cfg, verifications=records,
                                    verification_problems=problems, root=self.tmp)
        self.assertIn("deployment gate: the rules set changed since it was recorded (AGENTS.md); record it again", errors)
        # switch off: recorded, never refused
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + "\n[features]\nreview_binds_rules = false\n")
        self.assertFalse([e for e in self._errors() if "rules set changed" in e])

    def test_a_decision_recorded_before_the_field_existed_stays_valid(self):
        self._reviewed()
        status = self.read_status()
        for decision in (status["review"], status.get("design_approved") or {}):
            decision.pop("rules_hash", None)
            decision.pop("rules_entries", None)
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_legacy",
                   event_message="a review from an older engine", actor="test")
        (self.tmp / "CLAUDE.md").write_text("changed after the legacy review\n")
        self.assertFalse([e for e in self._errors() if "rules set" in e], "no field, no comparison")
        self.assertIsNone(cli.doctor(self.tmp, skip_preflight=True)["rules_set"])


if __name__ == "__main__":
    unittest.main()
