"""#167: rules distilled from run history. Launch rules refuse before any
adapter is resolved; packet rules refuse a reviewer result at the boundary
and keep it adoptable; --propose-rules drafts, never enables."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402

GOOD = {"kind": "design", "decision": "approved", "summary": "fine", "findings": [],
        "structural_blocker": False, "symptom_reproduced": "not_applicable"}


class LaunchRuleTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.which_calls = []

    def which(self, name):
        self.which_calls.append(name)
        return "/usr/local/bin/codex" if name == "codex" else None

    def _switch_off(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + "\n[features]\nlaunch_rules = false\n")

    def _phase(self, number):
        status = self.read_status()
        status["phase_number"] = number
        status["phase"] = lib.PHASES[number]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="fixture_phase", event_message="fixture phase", actor="test")

    def test_the_shipped_rules_load_and_validate(self):
        rules = {r["id"]: r for r in lib.load_launch_rules(self.tmp)}
        self.assertEqual(set(rules), {"reviewer-launch-phase-1", "reviewer-packet-tests-executed", "reviewer-packet-finding-length"})
        self.assertEqual((rules["reviewer-packet-finding-length"]["max_chars"], rules["reviewer-packet-finding-length"]["recover"]), (512, "truncate"))
        self.assertEqual(rules["reviewer-launch-phase-1"]["when"], {"command": "launch", "role": "reviewer", "phase_in": [1], "amendment": False})
        self.assertEqual(rules["reviewer-packet-tests-executed"]["allowed"], ["yes", "no", "unknown"])
        for rule in rules.values():
            self.assertIn(rule["cause"]["at"], ("2026-09-19", "2026-09-20"))
            self.assertTrue(rule["cause"]["root"])
        # a project's own rules join the engine's; a malformed one is an error, a duplicate id too
        mine = self.tmp / "handsoff-rules"
        mine.mkdir()
        (mine / "no-implementer-at-2.json").write_text(json.dumps({
            "id": "no-implementer-at-2", "cause": {"event": "x", "at": "2026-01-01"},
            "when": {"command": "launch", "role": "implementer", "phase_in": [2]}, "refuse": "not yet"}))
        self.assertIn("no-implementer-at-2", {r["id"] for r in lib.load_launch_rules(self.tmp)})
        (mine / "bad.json").write_text(json.dumps({"id": "bad", "cause": {"event": "x", "at": "y"},
                                                    "when": {"command": "launch", "phase_in": [9]}, "refuse": "z"}))
        with self.assertRaisesRegex(lib.HandsoffError, "phase_in"):
            lib.load_launch_rules(self.tmp)
        (mine / "bad.json").write_text(json.dumps({"id": "reviewer-launch-phase-1", "cause": {"event": "x", "at": "y"},
                                                    "when": {"command": "launch"}, "refuse": "z"}))
        with self.assertRaisesRegex(lib.HandsoffError, "duplicate rule id"):
            lib.load_launch_rules(self.tmp)
        (mine / "bad.json").unlink()
        # proposed drafts are never loaded
        proposed = Path(lib.engine_resource_path("rules")) / "proposed"
        self.assertFalse(any("proposed" in r["source"] for r in lib.load_launch_rules(self.tmp)))
        self.assertTrue(proposed.parent.is_dir())

    def test_reviewer_launch_at_phase_1_is_refused_before_any_adapter_is_resolved(self):
        self.init()
        with self.assertRaises(lib.HandsoffError) as ctx:
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=self.which)
        message = str(ctx.exception)
        self.assertIn("reviewer launch refused: the run is at Phase 1", message)
        self.assertIn("rule reviewer-launch-phase-1", message)
        self.assertIn("cause: agent_session_failed on ir-command 2026-09-19", message)
        self.assertEqual(self.which_calls, [], "no adapter was looked up")
        status = self.read_status()
        self.assertEqual(status.get("agent_sessions") or {}, {}, "no session was reserved")
        # the same launch for an open amendment is not what the rule is about
        self.assertIsNone(lib.evaluate_launch_rules(self.tmp, lib.load_config(self.tmp), role="reviewer", phase=1, amendment=True))
        # at Phase 2 the rule steps aside (the design-proposal check speaks next)
        self._phase(2)
        with self.assertRaises(lib.HandsoffError) as ctx:
            runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=self.which)
        self.assertIn("no design proposal", str(ctx.exception))

    def test_the_switch_off_skips_evaluation_and_the_launch_event_says_so(self):
        self.init()
        self._switch_off()
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "Review it.", which=self.which)
        self.assertEqual(spec.role, "reviewer", "with the switch off the Phase 1 launch is not refused")
        cfg = lib.load_config(self.tmp)
        self.assertIsNone(lib.evaluate_launch_rules(self.tmp, cfg, role="reviewer", phase=1, amendment=False))
        session = lib.create_agent_session(self.tmp, role="implementer", actor="codex-implementer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        launching = [e for e in events if e["kind"] == "agent_session_launching" and e["session_id"] == session["session_id"]]
        self.assertEqual(launching[-1]["launch_rules"], "disabled")

    def test_packet_rule_refuses_a_bad_tests_executed_naming_the_field_and_keeps_the_verdict(self):
        self.init()
        bad = json.dumps({**GOOD, "tests_executed": "pytest -q tests ran green"})
        with self.assertRaises(lib.PacketRuleViolation) as ctx:
            broker.parse_reviewer_result(bad, root=self.tmp)
        exc = ctx.exception
        self.assertEqual(exc.field, "tests_executed")
        self.assertIn("tests_executed must be the bare string yes, no or unknown", str(exc))
        self.assertIn('got "pytest -q tests ran green"', str(exc))
        self.assertIn("rule reviewer-packet-tests-executed", str(exc))
        self.assertEqual(exc.recovered["tests_executed"], "unknown")
        self.assertEqual(exc.recovered["decision"], "approved")
        # a good packet passes the rule and the floor
        self.assertEqual(broker.parse_reviewer_result(json.dumps({**GOOD, "tests_executed": "no"}), root=self.tmp)["tests_executed"], "no")
        # without root (no project) the built-in floor still refuses
        with self.assertRaisesRegex(lib.HandsoffError, "tests_executed is invalid"):
            broker.parse_reviewer_result(bad)
        # switch off: the floor refuses, without a rule name
        self._switch_off()
        with self.assertRaises(lib.HandsoffError) as ctx:
            broker.parse_reviewer_result(bad, root=self.tmp)
        self.assertNotIsInstance(ctx.exception, lib.PacketRuleViolation)

    def test_a_finding_over_the_bound_is_refused_and_recovered_by_truncation(self):
        self.init()
        long = json.dumps({**GOOD, "decision": "changes_requested", "findings": ["incorrect_implementation: " + "x" * 700],
                           "tests_executed": "yes"})
        with self.assertRaises(lib.PacketRuleViolation) as ctx:
            broker.parse_reviewer_result(long, root=self.tmp)
        exc = ctx.exception
        self.assertEqual(exc.field, "findings")
        self.assertIn("each finding must be 512 characters or fewer; 1 value(s) over 512", str(exc))
        self.assertIn("rule reviewer-packet-finding-length", str(exc))
        self.assertEqual(exc.recovered["decision"], "changes_requested")
        self.assertTrue(exc.recovered["findings"][0].endswith("..."))
        self.assertLessEqual(len(exc.recovered["findings"][0]), 512 + len("other: "))
        # a finding within the bound passes untouched
        ok = broker.parse_reviewer_result(json.dumps({**GOOD, "decision": "changes_requested", "findings": ["other: fine"]}), root=self.tmp)
        self.assertEqual(ok["findings"], ["other: fine"])
        # the floor without a project still refuses, without recovery
        with self.assertRaisesRegex(lib.HandsoffError, "findings are invalid"):
            broker.parse_reviewer_result(long)

    def test_a_refused_packet_is_persisted_on_the_session_for_adoption(self):
        self.init()
        session = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
                                           requested_model="default", resolution_source="configured")
        results, errors, recovered = [], [], []
        line = "HANDSOFF_REVIEW_RESULT: " + json.dumps({**GOOD, "tests_executed": "yes, ran them"})
        runtime._parse_reviewer_line(line, results, errors, recovered, self.tmp)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(recovered[0]["payload"]["tests_executed"], "unknown")
        self.assertEqual(recovered[0]["text"], line[len("HANDSOFF_REVIEW_RESULT: "):])
        lib.record_session_result(self.tmp, session["session_id"], "review", recovered[-1]["payload"],
                                  recovered_from_rule=True, refused_text=recovered[-1]["text"])
        stored = self.read_status()["agent_sessions"][session["session_id"]]["result"]
        self.assertEqual(stored["kind"], "review")
        self.assertEqual(stored["payload"]["tests_executed"], "unknown")
        self.assertTrue(stored["recovered_from_rule"])
        self.assertEqual(json.loads(stored["refused_text"])["tests_executed"], "yes, ran them", "the raw packet is kept as written")
        # adoption re-parses the preserved text and repairs only the named field
        self._phase(2)
        r = run(["criterion-update", "REQ-001", "--requirement", "a real criterion", "--test", "true"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('architect = "auto"', 'architect = "host"', 1))
        proposal = self.tmp / "proposal.json"
        proposal.write_text(json.dumps({"summary": "s", "approach": ["a"], "tradeoffs": ["t"],
                                        "decisions": ["d"], "constraints": ["c"], "verification": ["v"]}))
        r = run(["design-propose", "--file", str(proposal), "--by", "claude-architect"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["session-result-adopt", "--session", session["session_id"], "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(self.read_status().get("design_review"), "the preserved verdict landed as the design review")
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        recorded = [e for e in events if e["kind"] == "agent_session_result_recorded"][-1]
        self.assertTrue(recorded["recovered_from_rule"])
        self.assertIn("recovered from a refused packet", recorded["message"])


class ProposeRulesTests(HandsoffTestCase):
    """#167 drafting moved to the Miner with #174 (its test_propose_rules
    covers the shapes); the engine keeps the CLI flag as a shim that calls
    `miner propose-rules` and reads its reply."""

    def test_the_cli_flag_calls_the_miner_and_evaluates_nothing(self):
        self.init()
        archives = self.tmp / "archive"
        archives.mkdir()
        shim_dir = Path(tempfile.mkdtemp(prefix="handsoff-miner-shim-"))
        log = shim_dir / "argv.json"
        fake = shim_dir / "miner"
        fake.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                        "print(json.dumps({'written': [], 'skipped': [], 'proposed_dir': 'rules/proposed'}))\n")
        fake.chmod(0o755)
        try:
            with mock.patch.dict(os.environ, {"HANDSOFF_MINER": str(fake)}):
                r = run(["analyze-archives", "--propose-rules", "--archive-dir", str(archives)], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("HANDSOFF_RULES_PROPOSED: 0 draft(s) in rules/proposed", r.stdout)
            self.assertEqual(json.loads(log.read_text()),
                             ["propose-rules", "--root", str(self.tmp.resolve()), "--json", "--archive-dir", str(archives)])
            self.assertFalse((self.tmp / "rules" / "proposed").exists())
        finally:
            shutil.rmtree(shim_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
