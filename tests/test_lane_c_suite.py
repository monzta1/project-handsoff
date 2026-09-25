"""Lane C (#179, #176, #177): the suite means the whole tree. The runner
survives a child that exits early; the five red modules run green; the
module plan is a complete, disjoint partition and the workflow gathers it;
Phase 5 fills every required item's implementer; the Architect can decline
and the run closes as not planned."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402

ROOT = BIN.parent
sys.path.insert(0, str(ROOT / "tests"))
import shard  # noqa: E402

RED_ON_MAIN = ("test_governance_cross", "test_live_verification_view", "test_packaging",
               "test_reviewer_handoff", "test_claude_adapter")


class RunnerAndModulesTests(unittest.TestCase):
    def test_the_runner_treats_a_broken_pipe_as_the_childs_exit(self):
        agent = (BIN / "handsoff_agent.py").read_text()
        self.assertIn("except BrokenPipeError:\n                pass\n            try:\n                process.stdin.close()", agent)
        suite = (ROOT / "tests" / "test_handsoff_supervisor.py").read_text()
        self.assertIn("def test_a_child_that_exits_before_reading_its_task_reports_its_own_exit_not_a_broken_pipe", suite)
        self.assertIn('"private task text " * 100000', suite, "the task is larger than a pipe buffer, so EPIPE is deterministic")

    def test_the_five_modules_red_on_main_run_green(self):
        for name in RED_ON_MAIN:
            with self.subTest(module=name):
                # the modules launch fixtures of their own; a host session's
                # environment must not make them look nested
                env = {k: v for k, v in os.environ.items()
                       if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_COMPANION_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
                result = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"],
                                        cwd=str(ROOT), capture_output=True, text=True, timeout=600, env=env)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])

    def test_the_shared_inventory_is_the_whole_tree_and_the_workflow_gathers_it(self):
        modules = shard.modules()
        on_disk = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
        self.assertEqual(set(on_disk) - set(modules), {"test_handsoff_supervisor", *shard.MODULE_SCRIPTS})
        for script in shard.MODULE_SCRIPTS:
            text = (ROOT / "tests" / f"{script}.py").read_text()
            self.assertIn("def main(", text, f"{script} is listed as a script; it must be one")
            self.assertNotIn("unittest.TestCase", text)
        fixture_ids = [f"tests.test_x.Case.test_{index}" for index in range(13)]
        plan = shard.all_plan(5, fixture_ids)
        ids = [test_id for part in plan for test_id in part]
        self.assertEqual(sorted(ids), sorted(fixture_ids), "complete")
        self.assertEqual(len(ids), len(set(ids)), "disjoint")
        self.assertLessEqual(max(map(len, plan)) - min(map(len, plan)), 1)
        self.assertEqual(plan[0], sorted(fixture_ids)[:3], "contiguous module-friendly slices")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("python3 tests/shard.py --all --total 5 --index ${{ matrix.shard }}", workflow)
        # #227 added the docs path; #297/#298 put the fast preflight ahead of
        # every costly job, so the required check gathers it too.
        self.assertIn("needs: [changes, preflight, python, dashboard, docs]", workflow)
        self.assertNotIn("needs.modules", workflow)
        helper = (ROOT / "tests" / "shard.py").read_text()
        self.assertIn('"-m", "unittest", "-v", *selected', helper)
        config = (ROOT / "handsoff.toml").read_text()
        self.assertIn('commands = ["python3 tests/shard.py --all"]', config)


class ImplementerFillTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _events(self):
        return [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]

    def test_phase_5_fills_every_required_item_and_a_late_item_is_still_refused_at_8(self):
        r = run(["init", "Lane C items", "--item", "#176 the gate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(["criterion-update", "REQ-001", "--requirement", "[#176] the gate fires the same for every run"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # a second item arrives by tag only, with no delivery record of its own
        r = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "[#9] tag-derived item",
                 "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="codex-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        delivery = self.read_status()["work_item_delivery"]
        self.assertEqual({k: v["implemented_by"] for k, v in delivery.items()},
                         {"issue-176": "codex-implementer", "issue-9": "codex-implementer"})
        # a host can still name another actor per item
        r = run(["work-item-update", "issue-9", "--by", "claude-host", "--implemented-by", "claude-host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read_status()["work_item_delivery"]["issue-9"]["implemented_by"], "claude-host")
        # an item that appears after Phase 5 (an amendment's, say) has no
        # delivery record and no implementer: the Phase 8 gate names it and
        # nothing else, exactly as before this lane.
        acceptance = self.read_acceptance()
        acceptance["work_items"].append({**acceptance["work_items"][0], "id": "issue-10", "number": 10, "title": "late item"})
        acceptance["criteria"].append({**acceptance["criteria"][0], "id": "REQ-003", "requirement": "[#10] late item",
                                       "evidence": list(acceptance["criteria"][0].get("evidence", []))})
        status = self.read_status()
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        errors = lib.compute_errors({**status, "phase_number": 8, "phase": lib.PHASES[8], "progress": 100, "status": "complete"},
                                    acceptance, cfg, verifications=records, verification_problems=problems)
        self.assertTrue(any("work item issue-10 has no implemented_by" in e for e in errors), errors)
        self.assertFalse(any("issue-176 has no implemented_by" in e or "issue-9 has no implemented_by" in e for e in errors), errors)


class DeclineTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        self.registry = Path(os.environ["HANDSOFF_FLEET_REGISTRY"])
        self.init("Lane C decline: the #176 shape")

    def _events(self, kind=None):
        events = [json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if l.strip()]
        return [e for e in events if kind is None or e["kind"] == kind]

    def test_a_decline_at_phase_2_is_pending_until_the_reviewer_approves_it_then_closes_not_planned(self):
        # Lane E (#177): a decline is reviewed like a proposal
        r = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = run(["design-decline", "--by", "claude-architect", "--reason", "the Phase 8 gate already refuses an item without implemented_by",
                 "--evidence", "lane A refused issue-179 at advance 8 on 2026-09-21", "--evidence", "compute_errors line 5767",
                 "--alternative", "fill every required item at Phase 5 and keep the gate"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESIGN_DECLINED: pending the independent reviewer's word (by claude-architect)", r.stdout)
        status = self.read_status()
        declined = status["design_declined"]
        self.assertEqual((declined["by"], declined["decision"], len(declined["evidence"])), ("claude-architect", "pending", 2))
        self.assertEqual(declined["design_hash"], lib.design_hash(self.read_acceptance()["criteria"]))
        self.assertIsNone(status.get("run_closed"), "nothing closes until the reviewer speaks")
        self.assertEqual(self._events("design_declined")[0]["by"], "claude-architect")
        # Phase 3 is refused while the decline is pending; a second decline too
        r = run(["advance", "3", "30"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("a decline is pending the independent reviewer's word", r.stdout)
        r = run(["design-decline", "--by", "claude-architect", "--reason", "again"], cwd=self.tmp)
        self.assertIn("a decline is already pending", r.stdout)
        # the reviewer packet carries the decline
        context = lib.managed_design_context(self.tmp, "reviewer")
        self.assertEqual(context["design_decline"]["reason"], declined["reason"])
        # the architect cannot approve its own decline; the reviewer can
        r = run(["record-design-review", "--by", "claude-architect", "--architect", "claude-architect", "--approve", "--summary", "self"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        r = run(["record-design-review", "--by", "codex-reviewer", "--architect", "claude-architect", "--approve",
                 "--summary", "the gate exists; the evidence is the ledger"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESIGN_DECLINE_APPROVED: run closed as not_planned", r.stdout)
        status = self.read_status()
        self.assertEqual(status["design_declined"]["decision"], "approved")
        self.assertEqual(status["design_declined"]["reviewed_by"], "codex-reviewer")
        self.assertEqual(status["run_closed"]["outcome"], "not_planned")
        kinds = [e["kind"] for e in self._events()]
        self.assertEqual(kinds[kinds.index("design_decline_approved") + 1], "run_closed", "one write-ahead unit")
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["supervisor"]["label"], "Not planned")
        self.assertTrue(snapshot["supervisor"]["headline"].startswith("Not planned, declined by codex-reviewer:"))
        fleet.register_project(self.tmp, self.registry)
        card = next(p for p in fleet.build_fleet(self.registry)["projects"] if p["root"] == str(self.tmp.resolve()))
        self.assertEqual(card["state"], "closed")
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_the_reviewer_can_send_a_decline_back(self):
        r = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = run(["design-decline", "--by", "claude-architect", "--reason", "not needed"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = run(["record-design-review", "--by", "codex-reviewer", "--architect", "claude-architect", "--request-changes",
                 "--summary", "the harm is real", "--finding", "the ledger in .handsoff-archive shows the gate not firing on 2026-09-20"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESIGN_DECLINE_CHANGES_REQUESTED", r.stdout)
        status = self.read_status()
        self.assertEqual(status["design_declined"]["decision"], "changes_requested")
        self.assertEqual(len(status["design_declined"]["findings"]), 1)
        self.assertIsNone(status.get("run_closed"))
        self.assertEqual(status["phase_number"], 2)
        self.assertIsNone(lib.pending_design_decline(status))
        self.assertEqual(self._events("design_decline_changes_requested")[0]["findings"], 1)
        # the Architect may now decline again or propose
        r = run(["design-decline", "--by", "claude-architect", "--reason", "with the evidence this time", "--evidence", "e1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_decline_refuses_at_phase_3_and_after_an_approval_and_with_bad_fields(self):
        r = run(["design-decline", "--by", "claude-architect", "--reason", "x" * 513], cwd=self.tmp)
        self.assertIn("--reason must be 1 to 512 characters", r.stdout)
        r = run(["design-decline", "--by", "claude-architect", "--reason", "fine", *sum((["--evidence", f"e{i}"] for i in range(9)), [])], cwd=self.tmp)
        self.assertIn("--evidence takes at most 8 items", r.stdout)
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(3, implemented_by="impl-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        r = run(["design-decline", "--by", "claude-architect", "--reason", "too late"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("only at Phase 2, the design debate (the run is at Phase 3)", r.stdout)
        # a live managed session refuses the decline before anything is written (F1.1)
        status = self.read_status()
        status["phase_number"], status["phase"], status["design_approved"], status["design_review"] = 2, lib.PHASES[2], None, None
        # A session record the engine would really write: commit validates the
        # closed field set now, and a three-field stub is not a state it can
        # reach. The claim under test is that a LIVE session refuses a decline.
        sid = "hs-" + "1" * 32
        status["agent_sessions"] = {sid: {
            "session_id": sid, "role": "architect", "state": "running",
            "actor": "claude-architect", "adapter": "claude", "requested_model": "default",
            "reported_model": None, "resolution_source": "configured",
            "started_at": "2026-09-25T00:00:00+00:00", "running_at": "2026-09-25T00:00:00+00:00",
            "ended_at": None, "exit_code": None}}
        status["current_agent_sessions"] = {"architect": sid}
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture", event_message="live session")
        r = run(["design-decline", "--by", "claude-architect", "--reason", "while live"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("a managed session is live", r.stdout)
        self.assertNotIn("design_declined", self.read_status())
        self.assertEqual(self._events("design_declined"), [])
        # and at Phase 1 the decline waits for the debate
        fresh = self.read_status()
        fresh["phase_number"], fresh["phase"], fresh["progress"] = 1, lib.PHASES[1], 10
        fresh["agent_sessions"] = {}
        fresh["current_agent_sessions"] = {}
        lib.commit(self.tmp, lib.load_config(self.tmp), status=fresh, event_kind="fixture", event_message="back to 1")
        r = run(["design-decline", "--by", "claude-architect", "--reason", "early"], cwd=self.tmp)
        self.assertIn("only at Phase 2", r.stdout)
        self.assertNotIn("design_declined", self.read_status())
        self.assertIsNone(self.read_status().get("run_closed"))
        prompt = (ROOT / "prompts" / "architect.md").read_text()
        self.assertIn("HANDSOFF_DESIGN_DECLINE:", prompt)
        self.assertIn("whose absence causes no observed harm", prompt)


if __name__ == "__main__":
    unittest.main()
