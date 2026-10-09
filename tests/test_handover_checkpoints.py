"""P1.6: durable handover checkpoints. A managed Implementer prints
HANDSOFF_CHECKPOINT lines; the runtime writes one at launch and one when the
session ends on a failure; a replacement resumes from the latest, with its
unfinished criteria taken from the current acceptance, never from the
checkpoint's own completed_criteria claim."""
import io
import json
import shutil
import sys
import unittest
from unittest import mock

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_checkpoint as checkpoints  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_schema as schema  # noqa: E402
from tests.engine_patch import patch_engine  # noqa: E402
from tests.fixture_state import write_version_pin  # noqa: E402


class _InputPipe:
    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)

    def close(self):
        return None


class _FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, pid=4243):
        self.pid = pid
        self.returncode = returncode
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


GOOD = {"files_changed": ["bin/a.py", "tests/test_a.py"], "completed_criteria": ["REQ-001", "REQ-002"],
        "commands_run": [{"command": "python3 -m unittest tests.test_a -v", "exit_code": 0}],
        "remaining": ["REQ-003 not started"], "blockers": [], "provider_state": "turn 12"}
CHILD = ("HANDSOFF_CHECKPOINT: " + json.dumps(GOOD) + "\n"
         'HANDSOFF_CHECKPOINT: {"files_changed": ["a"], "secret": "x"}\n'
         "HANDSOFF_CHECKPOINT: " + json.dumps({"files_changed": [f"f{i}" for i in range(65)]}) + "\n"
         "HANDSOFF_CHECKPOINT: not json\n"
         'HANDSOFF_PROGRESS: {"criterion": "REQ-001", "state": "done", "test": "true", "note": ""}\n')


class HandoverCheckpointTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Checkpoint fixture")
        self.set_criterion_state("passing", resolved=True)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace(
            'commands = ["true"]',
            'commands = ["true", "python3 -m unittest tests.test_b -v", "python3 -m unittest tests.test_c -v"]', 1))
        tx = {"operations": [
            {"op": "add", "criterion": {"id": "REQ-002", "type": "supporting", "requirement": "two",
                                        "verification": "automated", "tests": ["python3 -m unittest tests.test_b -v"]}},
            {"op": "add", "criterion": {"id": "REQ-003", "type": "supporting", "requirement": "three",
                                        "verification": "automated", "tests": ["python3 -m unittest tests.test_c -v"]}},
        ]}
        path = self.tmp / "tx.json"
        path.write_text(json.dumps(tx))
        r = run(["criteria-apply", "--file", str(path), "--by", "host"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reached = self.advance_to(4, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _spec(self, task="bounded prompt"):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"), str(self.tmp),
                                  task, token_budget=40_000, project_root=str(self.tmp.resolve()))

    def _events(self, kind):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
                if line.strip() and json.loads(line).get("kind") == kind]

    def _fail_once(self, stdout=CHILD):
        factory = mock.Mock(return_value=_FakeProcess(stdout, stderr="token budget exhausted\n", returncode=1))
        with self.assertRaises(runtime.AgentLaunchError):
            runtime.execute_launch(self._spec(), popen_factory=factory, beacon_interval=0.01)
        status = self.read_status()
        sid = lib.role_session_ids(status)["implementer"]
        return sid, status

    def test_a_printed_checkpoint_is_stored_and_malformed_lines_are_refused(self):
        """[P1.6] the valid line is stored with its event; three malformed lines are warnings only."""
        sid, status = self._fail_once()
        history = status["agent_sessions"][sid]["checkpoints"]
        self.assertEqual([item["source"] for item in history], ["failure", "agent", "launch"])
        agent = history[1]
        self.assertEqual({k: agent[k] for k in GOOD}, GOOD)
        self.assertNotIn("secret", json.dumps(history))
        self.assertFalse(any(len(item["files_changed"]) > 64 for item in history))
        self.assertEqual(lib.read_operations(self.tmp)["sessions"][sid]["protocol_warnings"], 3)
        self.assertEqual([e["source"] for e in self._events("checkpoint_recorded") if e["session_id"] == sid],
                         ["launch", "agent", "failure"])
        self.assertEqual(run(["verify-log"], cwd=self.tmp).returncode, 0)

    def test_a_claude_stream_json_event_carries_the_checkpoint_and_progress(self):
        """E3 live proof: a Claude implementer prints protocol lines inside a
        stream-json text event; both are stored, once each."""
        session = lib.create_agent_session(self.tmp, role="implementer", actor="impl",
                                           adapter="claude", requested_model="default",
                                           resolution_source="configured")
        sid = session["session_id"]
        checkpoint = 'HANDSOFF_CHECKPOINT: {"files_changed": ["a.txt"], "remaining": ["finish"]}'
        progress = 'HANDSOFF_PROGRESS: {"criterion": "REQ-001", "state": "partial", "test": "true", "note": "half"}'
        event = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": checkpoint + "\n" + progress}]}})
        self.assertIsNotNone(runtime._parse_checkpoint_line(event, self.tmp, sid, "implementer"))
        self.assertIsNotNone(runtime._parse_progress_line(event, self.tmp, sid, "implementer"))
        result = json.dumps({"type": "result", "result": checkpoint})
        runtime._parse_checkpoint_line(result, self.tmp, sid, "implementer")  # the final result echo is not re-stored
        stored = self.read_status()["agent_sessions"][sid]
        agent = [c for c in stored["checkpoints"] if c["source"] == "agent"]
        self.assertEqual(len(agent), 1)
        self.assertEqual(agent[0]["files_changed"], ["a.txt"])
        self.assertEqual([p["criterion"] for p in stored["progress"]], ["REQ-001"])
        # a reviewer's identical event is ignored
        self.assertIsNone(runtime._parse_checkpoint_line(event, self.tmp, sid, "reviewer"))

    def test_the_history_is_bounded_latest_first(self):
        session = lib.create_agent_session(self.tmp, role="implementer", actor="codex-implementer", adapter="codex",
                                           requested_model="m", resolution_source="configured")
        for index in range(20):
            checkpoints.record_session_checkpoint(self.tmp, session["session_id"],
                                                  {**GOOD, "provider_state": f"step {index}"})
        history = self.read_status()["agent_sessions"][session["session_id"]]["checkpoints"]
        self.assertEqual(len(history), 16)
        self.assertEqual(history[0]["provider_state"], "step 19")
        self.assertEqual(checkpoints.latest_checkpoint(self.read_status(), session["session_id"])["provider_state"],
                         "step 19")
        self.assertIsNone(checkpoints.latest_checkpoint(self.read_status(), "hs-missing"))

    def test_the_validator_bounds(self):
        self.assertIsNone(checkpoints.parse_checkpoint_line("HANDSOFF_CHECKPOINT: {}"))
        validate = schema.validate_checkpoint_line
        self.assertIsNone(validate({"commands_run": [{"command": "x"}]}))
        self.assertIsNone(validate({"commands_run": [{"command": "x", "exit_code": True}]}))
        self.assertIsNone(validate({"commands_run": [{"command": "x", "exit_code": 0}] * 33}))
        self.assertIsNone(validate({"remaining": ["x"] * 9}))
        self.assertIsNone(validate({"blockers": ["x" * 201]}))
        self.assertIsNone(validate({"provider_state": "x" * 201}))
        self.assertIsNone(validate({"completed_criteria": ["../etc"]}))
        self.assertEqual(validate({"files_changed": ["f"] * 64})["files_changed"], ["f"] * 64)
        self.assertEqual(checkpoints.parse_checkpoint_line('HANDSOFF_CHECKPOINT: {"remaining": ["r"]}'),
                         {"files_changed": [], "completed_criteria": [], "commands_run": [], "remaining": ["r"],
                          "blockers": [], "provider_state": ""})

    def test_launch_and_budget_failure_checkpoints_are_written(self):
        """[P1.6] launch: owned paths, items, criteria; failure: progress and changed paths."""
        sid, status = self._fail_once("")
        launch = status["agent_sessions"][sid]["checkpoints"][-1]
        self.assertEqual(launch["source"], "launch")
        self.assertEqual(launch["criteria"], ["REQ-001", "REQ-002", "REQ-003"])
        self.assertEqual((launch["owned_paths"], launch["work_items"]), ([], []))
        failure = status["agent_sessions"][sid]["checkpoints"][0]
        self.assertEqual(failure["source"], "failure")
        self.assertTrue(failure["provider_state"].startswith("token_budget_exhaustion"))
        self.assertEqual(failure["remaining"], ["REQ-001", "REQ-002", "REQ-003"])
        # the failure checkpoint carries the progress claims and the agent's own checkpoint forward
        sid, status = self._fail_once()
        failure = status["agent_sessions"][sid]["checkpoints"][0]
        self.assertEqual(failure["files_changed"], GOOD["files_changed"])
        self.assertEqual(failure["completed_criteria"], ["REQ-001", "REQ-002"])
        self.assertIn({"command": "true", "exit_code": 0}, failure["commands_run"])
        # an explicit launch binding is recorded as given
        acceptance = {"criteria": [{"id": "REQ-001", "requirement": "[#7] one"},
                                   {"id": "REQ-002", "requirement": "[#8] two"}],
                      "work_items": [{"id": "issue-7"}, {"id": "issue-8"}]}
        record = checkpoints.launch_checkpoint(acceptance, ["bin/a.py"], ["issue-8"])
        self.assertEqual((record["owned_paths"], record["work_items"], record["criteria"]),
                         (["bin/a.py"], ["issue-8"], ["REQ-002"]))
        # a reviewer session gets no checkpoint
        reviewer = lib.create_agent_session(self.tmp, role="reviewer", actor="claude-reviewer", adapter="claude",
                                            requested_model="m", resolution_source="configured")
        self.assertNotIn("checkpoints", reviewer)

    def test_a_failure_without_an_agent_checkpoint_records_the_files_it_changed(self):
        """E3 review: a budget failure names no paths, so the failure
        checkpoint reads the session's actual changes from the tree."""
        import subprocess
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        if not (self.tmp / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.tmp, check=True, capture_output=True)
        subprocess.run([*git, "commit", "-qm", "base", "--allow-empty"], cwd=self.tmp, check=True, capture_output=True)

        class _Writes(_FakeProcess):
            def __init__(inner, root):
                (root / "changed-before-budget.txt").write_text("partial work\n")
                super().__init__("", stderr="token budget exhausted\n", returncode=1)
        factory = mock.Mock(side_effect=lambda *a, **k: _Writes(self.tmp))
        with self.assertRaises(runtime.AgentLaunchError):
            runtime.execute_launch(self._spec(), popen_factory=factory, beacon_interval=0.01)
        status = self.read_status()
        sid = lib.role_session_ids(status)["implementer"]
        failure = status["agent_sessions"][sid]["checkpoints"][0]
        self.assertEqual(failure["source"], "failure")
        self.assertIn("changed-before-budget.txt", failure["files_changed"])
        self.assertFalse(any(path.startswith(".handsoff") or path.startswith("handsoff-")
                             for path in failure["files_changed"]), failure["files_changed"])

    def test_a_false_claim_and_invalidated_evidence_both_stay_unfinished(self):
        """[P1.6] completion authority is the current acceptance, never the checkpoint."""
        sid, _ = self._fail_once()
        handoff = {"from_session_id": sid}
        # REQ-001 passes on the ledger; REQ-002 is only claimed complete by the checkpoint
        section = checkpoints.replacement_resume_section(self.tmp, handoff)
        unfinished = section.split("Unfinished criteria", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("REQ-001", unfinished)
        self.assertIn("- REQ-002 (not_tested)", unfinished)
        self.assertIn("- REQ-003 (not_tested)", unfinished)
        self.assertIn("Criteria the ledger shows passing: REQ-001. Do not redo them.", section)
        self.assertIn("claimed complete (its claim, not evidence): REQ-001, REQ-002", section)
        # REQ-001's evidence is invalidated after the checkpoint: it is unfinished again
        changed = run(["criterion-update", "REQ-001", "--requirement", "A changed fixture requirement",
                       "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        section = checkpoints.replacement_resume_section(self.tmp, handoff)
        unfinished = section.split("Unfinished criteria", 1)[1].split("\n\n", 1)[0]
        self.assertIn("- REQ-001 (not_tested)", unfinished)
        self.assertIn("Criteria the ledger shows passing: none.", section)
        # with no checkpoint the handoff is as today
        self.assertEqual(checkpoints.resume_section(None, {"criteria": []}, None), "")
        self.assertEqual(checkpoints.replacement_resume_section(self.tmp, {"from_session_id": sid, "checkpoint": None}), "")

    def test_a_replacement_stdin_carries_the_checkpoint_section(self):
        """[P1.6] end to end: the fallback launch's input lists only unfinished criteria."""
        cfg = lib.load_config(self.tmp)
        payload = {"profiles": lib.agent_profiles(cfg), "fallbacks": lib.fallback_profiles(cfg),
                   "max_failovers_per_role": 2}
        payload["fallbacks"]["implementer"] = [{"adapter": "codex", "model": "second"}]
        lib.update_agent_settings(self.tmp, payload)
        first = _FakeProcess(CHILD, stderr="runner crashed\n", returncode=1)
        second = _FakeProcess('HANDSOFF_PROGRESS: {"criterion": "REQ-002", "state": "done", "test": "true"}\n')
        launches = [first, second]
        # the runtime manifest is regenerated by the host after the last edit
        with mock.patch.object(lib, "launch_preflight", return_value={"state": "ready"}), \
                patch_engine("validate_runtime_integrity", return_value=None):
            code = runtime.execute_with_recovery(
                self._spec("original task"), popen_factory=lambda *a, **k: launches.pop(0),
                which=lambda adapter: f"/usr/local/bin/{adapter}",
                snapshotter=lambda root: {"head": "a" * 40, "branch": "main", "dirty": True, "status_sha256": "b" * 64})
        self.assertEqual(code, 0)
        stdin = "".join(second.stdin.written)
        self.assertIn("# Resume from checkpoint", stdin)
        resume = stdin.split("# Resume from checkpoint", 1)[1]
        unfinished = resume.split("Unfinished criteria", 1)[1].split("\n\n", 1)[0]
        self.assertEqual([line for line in unfinished.splitlines() if line.startswith("- ")],
                         ["- REQ-002 (not_tested)", "- REQ-003 (not_tested)"])
        self.assertIn("- bin/a.py", resume)
        self.assertIn("- python3 -m unittest tests.test_a -v (exit 0)", resume)
        self.assertNotIn("# Resume from checkpoint", "".join(first.stdin.written))

    def test_the_prompt_documents_the_line(self):
        prompt = (ROOT / "prompts" / "implementer.md").read_text()
        self.assertIn("HANDSOFF_CHECKPOINT:", prompt)
        self.assertIn("only as your claim", prompt)
        self.assertIn("workflow", lib.VERIFICATION_KINDS)


if __name__ == "__main__":
    unittest.main()
