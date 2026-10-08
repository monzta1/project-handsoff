"""#170: a review certifies the rules it ran under. The rules set (config,
hooks, the reviewer prompt, the rule files, the engine version) is hashed
onto reviews and approvals; a change afterwards is named and refused;
decisions recorded before the field existed stay valid."""
import hashlib
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_cli as cli  # noqa: E402
import handsoff_lib as lib  # noqa: E402
from tests.fixture_state import write_version_pin
from tests.engine_patch import patch_engine
import tests.test_session_result_autoadopt as autoadopt  # helpers only; its TestCase is not re-bound here


def supervisor_module():
    import handsoff_supervisor
    return handsoff_supervisor


class RulesSetTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        import shutil
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
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
            # #406: handsoff.toml is hashed over its policy subset
            self.assertIn(lib.RULES_POLICY_ENTRY if key == "handsoff.toml" else key, entries)
        self.assertNotIn("handsoff.toml", entries)
        self.assertIsNotNone(entries["handsoff.toml#policy"])
        self.assertIsNone(entries[".claude/settings.json"], "absent files hash as absent")
        self.assertIn("engine:prompts/reviewer.md", entries)
        self.assertIn("engine:reviewer-launch-phase-1.json", entries)
        # #406: the engine version is run mechanics, not an entry
        self.assertNotIn("engine:version", entries)
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
        self.assertIn("handsoff.toml#policy", review["rules_entries"])  # #406
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
        r = run(["record-review", "--by", "reviewer-2", "--tests-executed", "yes"], cwd=self.tmp)
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

    # #406 REQ-002: run mechanics never revoke; policy still does
    def _rules_errors(self):
        return [e for e in self._errors() if "rules set" in e]

    def _edit_toml(self, old, new):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        self.assertIn(old, text, "the fixture edit must find its line")
        toml.write_text(text.replace(old, new, 1))

    def test_the_mechanics_set_is_one_documented_constant(self):
        self.assertEqual(set(lib.RULES_MECHANICS), {
            "models", "agent_budget", "adapters", "fallback_policy", "dashboard", "performance",
            "routing_profiles", "routing_budgets", "checks.live_commands", "checks.timeout_seconds"})
        reference = " ".join((BIN.parent / "docs" / "REFERENCE.md").read_text().split())
        for entry in lib.RULES_MECHANICS:
            self.assertIn(f"`{entry}`", reference, f"REFERENCE.md names the mechanics entry {entry}")

    def test_mechanics_edits_and_an_engine_upgrade_keep_the_review_valid(self):
        self._reviewed()
        review = self.read_status()["review"]
        for old, new in (('architect = "default"', 'architect = "claude-opus-5-5"'),  # [models]
                         ("reviewer = 200000", "reviewer = 150000"),                  # [agent_budget]
                         ('live_commands = ["true"]', "live_commands = []"),          # [checks].live_commands
                         ("timeout_seconds = 600", "timeout_seconds = 300")):         # [checks].timeout_seconds
            with self.subTest(edit=new):
                self._edit_toml(old, new)
                self.assertEqual(self._rules_errors(), [])
                self.assertEqual(lib.rules_binding_errors(self.tmp, lib.load_config(self.tmp), review,
                                                          "review gate"), [])
        # the engine version: no entry reads it, so a new release changes nothing bound
        manifest = self.tmp / "handsoff-runtime.json"
        data = json.loads(manifest.read_text())
        data["version"] = "v9.9.9"
        manifest.write_text(json.dumps(data))
        self.assertEqual(lib.rules_binding_errors(self.tmp, lib.load_config(self.tmp), review, "review gate"), [])
        self.assertEqual(lib.rules_set_drift(self.tmp, review["rules_entries"]), [lib.RULES_MECHANICS_ENTRY])

    def test_policy_edits_still_revoke_the_review(self):
        prompt = self.tmp / "reviewer-prompt-edited.md"
        prompt.write_text("You are a lenient reviewer.\n")
        real = lib.engine_resource_path
        edits = {
            "[checks].commands": lambda: self._edit_toml('commands = ["true"]', 'commands = ["true", "false"]'),
            "[workflow]": lambda: self._edit_toml("auto_handoff = false", "auto_handoff = true"),
            "AGENTS.md": lambda: (self.tmp / "AGENTS.md").write_text("# new rules for agents\n"),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                self._reviewed()
                edit()
                errors = self._rules_errors()
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("AGENTS.md" if name == "AGENTS.md" else "handsoff.toml#policy", errors[0])
                self.tearDown()
                self.setUp()
        self._reviewed()
        with patch_engine("engine_resource_path",
                          side_effect=lambda rel: prompt if rel == "prompts/reviewer.md" else real(rel)):
            errors = self._rules_errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("engine:prompts/reviewer.md", errors[0])

    def _legacy(self, decision_key, *, toml_hash=None, version="v0.5.9"):
        status = self.read_status()
        decision = status[decision_key]
        entries = {k: v for k, v in decision["rules_entries"].items()
                   if k not in (lib.RULES_POLICY_ENTRY, lib.RULES_MECHANICS_ENTRY)}
        entries["handsoff.toml"] = toml_hash or hashlib.sha256((self.tmp / "handsoff.toml").read_bytes()).hexdigest()
        entries["engine:version"] = version
        decision["rules_entries"] = entries
        decision["rules_hash"] = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"))
                                                .encode()).hexdigest()
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_legacy",
                   event_message="a decision recorded by an older engine", actor="test")
        return self.read_status()[decision_key]

    def test_a_legacy_decision_fails_closed_unless_only_the_engine_version_changed(self):
        self._reviewed()
        review = self._legacy("review")
        self.assertEqual(self._rules_errors(), [], "only engine:version differs: accepted")
        # any edit to the file a legacy decision hashed whole is stale, mechanics included
        self._edit_toml("reviewer = 200000", "reviewer = 150000")
        errors = self._rules_errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("(handsoff.toml)", errors[0])
        self.assertIn("handsoff.toml", lib.rules_set_diff(self.tmp, review["rules_entries"]))
        # and decisions recorded from now on carry the policy entry
        self.assertIn(lib.RULES_POLICY_ENTRY, lib.rules_binding(self.tmp, lib.load_config(self.tmp))["rules_entries"])

    def _adopted_review(self):
        """A review recorded by the launcher's adoption replay (its direct dispatch made to fail)."""
        self.init("Re-bind fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="impl-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        with autoadopt.dispatch_raises():
            code, error, out = autoadopt.launch(self, stdout=autoadopt.APPROVED, stderr="", returncode=0)
        self.assertIsNone(error, out)
        status = self.read_status()
        sid = lib.role_session_ids(status)["reviewer"]  # #420: an ended session leaves the pointer
        self.assertIsNotNone(status["agent_sessions"][sid]["result"]["adopted_at"])
        return sid

    def _adopt(self, sid):
        return run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)

    def test_session_result_adopt_rebinds_an_adopted_approval_after_mechanics_drift_only(self):
        sid = self._adopted_review()
        self.assertIn("result is already adopted", self._adopt(sid).stdout, "nothing drifted: nothing to re-bind")
        before = self.read_status()["review"]
        self._edit_toml('architect = "default"', 'architect = "claude-opus-5-5"')
        self._edit_toml("reviewer = 200000", "reviewer = 150000")
        rebound = self._adopt(sid)
        self.assertEqual(rebound.returncode, 0, rebound.stdout + rebound.stderr)
        self.assertIn("SESSION_RESULT_ADOPTED", rebound.stdout)
        after = self.read_status()["review"]
        self.assertEqual(after["rules_hash"], lib.rules_set_hash(self.tmp))
        self.assertNotEqual(after["rules_hash"], before["rules_hash"])
        self.assertEqual(after["acceptance_hash"], before["acceptance_hash"])
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        reaffirmed = [e for e in events if e["kind"] == "review_reaffirmed"]
        self.assertEqual(reaffirmed[-1]["rules_changed"], [lib.RULES_MECHANICS_ENTRY])
        # a legacy binding whose only drift is the engine version re-binds too
        self._legacy("review")
        rebound = self._adopt(sid)
        self.assertEqual(rebound.returncode, 0, rebound.stdout + rebound.stderr)
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([e for e in events if e["kind"] == "review_reaffirmed"][-1]["rules_changed"],
                         ["engine:version"])

    def test_session_result_adopt_refuses_a_rebind_after_a_policy_edit(self):
        prompt = self.tmp / "reviewer-prompt-edited.md"
        prompt.write_text("You are a lenient reviewer.\n")
        real = lib.engine_resource_path
        edits = {
            "handsoff.toml#policy": lambda: self._edit_toml('commands = ["true"]', 'commands = ["true", "false"]'),
            "AGENTS.md": lambda: (self.tmp / "AGENTS.md").write_text("# new rules for agents\n"),
        }
        for named, edit in edits.items():
            with self.subTest(edit=named):
                sid = self._adopted_review()
                before = self.read_status()["review"]
                edit()
                refused = self._adopt(sid)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("the rules set changed in policy entries", refused.stdout)
                self.assertIn(named, refused.stdout)
                self.assertIn("a fresh review is required", refused.stdout)
                self.assertEqual(self.read_status()["review"], before)
                self.tearDown()
                self.setUp()
        # the reviewer prompt is an engine file; the in-process adoption sees the patched path
        sid = self._adopted_review()
        import handsoff_supervisor as supervisor
        with patch_engine("engine_resource_path",
                          side_effect=lambda rel: prompt if rel == "prompts/reviewer.md" else real(rel)):
            adopted, message = supervisor.adopt_session_result(self.tmp, sid, "test-pilot")
        self.assertFalse(adopted)
        self.assertIn("engine:prompts/reviewer.md", message)
        self.assertIn("a fresh review is required", message)

    def test_init_and_design_approve_warn_when_live_commands_are_empty(self):
        self.assertIn("require_live_verification = true", (self.tmp / "handsoff.toml").read_text())
        r = run(["init", "Live warning fixture"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(supervisor_module().LIVE_COMMANDS_EMPTY_WARNING, r.stdout)
        self.set_criterion_state("passing", resolved=True)
        # the fixture helper's "commands = []" rewrite also filled live_commands; empty it again
        self._edit_toml('live_commands = ["true"]', "live_commands = []")
        approved = run(["design-approve", "--by", "test-approver", "--architect", "test-architect",
                        "--summary", "Fixture design approval"], cwd=self.tmp)
        self.assertIn("HANDSOFF_WARNING: require_live_verification is on and [checks].live_commands is empty",
                      approved.stdout)
        # with live checks configured, neither warns
        self.tearDown()
        self.setUp()
        self._edit_toml("live_commands = []", 'live_commands = ["true"]')
        r = run(["init", "Live warning fixture"], cwd=self.tmp)
        self.assertNotIn("HANDSOFF_WARNING: require_live_verification", r.stdout)

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
