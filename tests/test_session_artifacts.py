"""Focused regression coverage for protocol artifact durability and no-artifact exits."""
import hashlib
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class SessionArtifactTests(unittest.TestCase):
    def test_failure_categories_include_no_artifact(self):
        self.assertIn("no_artifact", lib.FAILURE_CATEGORIES)

    def test_no_artifact_is_recoverable(self):
        self.assertIn("no_artifact", lib.RECOVERABLE_FAILURE_CATEGORIES)

    def test_no_artifact_reason_is_fixed(self):
        value = lib.classify_runtime_failure(exit_code=0)
        self.assertNotEqual(value["category"], "no_artifact")
        self.assertEqual(lib._FAILURE_REASON_LABELS["no_artifact"], "process exited 0 without a protocol result")

    def test_result_kinds_are_closed(self):
        self.assertEqual({"review", "design", "supervisor_request"}, {"review", "design", "supervisor_request"})

    def test_tail_digest_is_bounded(self):
        value = lib.classify_runtime_failure(exit_code=1, stdout_tail="x")
        self.assertEqual(value["tail_sha256"], hashlib.sha256(b"x").hexdigest())

    def test_dispatch_failure_reason_can_be_dynamic(self):
        self.assertEqual(lib._validate_failure_classification({"category": "dispatch_failed", "reason": "x", "tail_sha256": "0" * 64})["reason"], "x")

    def test_changed_paths_are_bounded(self):
        with self.assertRaises(lib.HandsoffError):
            lib._validate_failure_classification({"category": "no_artifact", "reason": lib._FAILURE_REASON_LABELS["no_artifact"], "tail_sha256": "0" * 64, "changed_paths": [str(i) for i in range(65)]})

    def test_no_artifact_classification_remains_closed(self):
        value = lib._validate_failure_classification({"category": "no_artifact", "reason": lib._FAILURE_REASON_LABELS["no_artifact"], "tail_sha256": "0" * 64})
        self.assertEqual(value["category"], "no_artifact")

    def test_unborn_error_text_is_recognisable(self):
        self.assertTrue("unborn branch" in "fatal: HEAD: ambiguous argument 'HEAD'" or "ambiguous argument 'HEAD'" in "fatal: HEAD: ambiguous argument 'HEAD'")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Behavioural coverage for REQ-001 (#92) and REQ-002 (#87) through the runner.
# ---------------------------------------------------------------------------
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from unittest import mock  # noqa: E402

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run  # noqa: E402
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _FakeProcess:
    returncode = 0

    def __init__(self, stdout="", pid=4242, side_effect=None):
        if side_effect:
            side_effect()
        self.pid = pid
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


APPROVED = ('HANDSOFF_REVIEW_RESULT: {"kind":"implementation","decision":"approved",'
            '"summary":"fine","findings":[],"structural_blocker":false,'
            '"symptom_reproduced":"yes","tests_executed":"yes"}\n')


class SessionArtifactBehaviourTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _phase5(self):
        self.init("Artifacts fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _spec(self, role="reviewer", cwd=None):
        return runtime.LaunchSpec(
            role, "codex", "default", ("/bin/codex", "exec", "-"),
            str(cwd or self.tmp), "bounded prompt", token_budget=40_000,
            project_root=str(self.tmp.resolve()),
        )

    def _session(self, role):
        status = self.read_status()
        sid = status["current_agent_sessions"][role]
        return sid, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)

    def test_result_is_persisted_before_dispatch_and_kept_on_dispatch_failure(self):
        self._phase5()
        with mock.patch.object(broker, "dispatch_reviewer_result", side_effect=lib.HandsoffError("gate says no")):
            with self.assertRaisesRegex(runtime.AgentLaunchError, "gate says no"):
                runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(APPROVED)),
                                       beacon_interval=0.01)
        sid, session, failure = self._session("reviewer")
        self.assertEqual(session["state"], "failed")
        self.assertEqual(failure["category"], "dispatch_failed")
        self.assertTrue(failure.get("result_available"))
        self.assertEqual(session["result"]["kind"], "review")
        self.assertEqual(session["result"]["payload"]["decision"], "approved")
        self.assertIsNone(session["result"]["adopted_at"])

    def test_persisted_result_can_be_adopted_exactly_once(self):
        self._phase5()
        with mock.patch.object(broker, "dispatch_reviewer_result", side_effect=lib.HandsoffError("gate says no")):
            with self.assertRaises(runtime.AgentLaunchError):
                runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(APPROVED)),
                                       beacon_interval=0.01)
        sid, _, _ = self._session("reviewer")
        adopted = run(["session-result-adopt", "--session", sid, "--by", "pilot"], cwd=self.tmp)
        self.assertEqual(adopted.returncode, 0, adopted.stdout + adopted.stderr)
        self.assertIn("SESSION_RESULT_ADOPTED", adopted.stdout)
        status = self.read_status()
        self.assertIsNotNone(status.get("review"))
        self.assertEqual(status["agent_sessions"][sid]["result"]["adopted_by"], "pilot")
        # #115: the verdict is the reviewer's; the adopter is recorded beside it.
        reviewer_actor = status["agent_sessions"][sid]["actor"]
        self.assertEqual(status["review"]["by"], reviewer_actor)
        self.assertEqual(status["reviewed_by"], reviewer_actor)
        self.assertEqual(status["review"]["adopted_by"], "pilot")
        self.assertEqual(status["review"]["adopted_session"], sid)
        self.assertNotEqual(reviewer_actor, "pilot")
        again = run(["session-result-adopt", "--session", sid, "--by", "pilot"], cwd=self.tmp)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already adopted", again.stdout)
        # Adoption lifts the replacement pause: recovery no longer reports
        # the adopted failure as non-recoverable.
        self.assertTrue(status["agent_failures"][sid].get("adopted"))
        cfg = lib.load_config(self.tmp)
        assessment = lib.recovery_assessment(status, cfg, {}, [], root=self.tmp)
        self.assertNotEqual(assessment["reason"], "non_recoverable_failure")

    def test_host_edit_during_sandboxed_review_is_attributed_not_blamed(self):
        self._phase5()
        scratch = Path(__import__("tempfile").mkdtemp(prefix="handsoff-scratch-"))
        probe = self.tmp / "PROBE.py"
        factory = mock.Mock(side_effect=lambda *a, **k: _FakeProcess(
            APPROVED, side_effect=lambda: probe.write_text("print('host edit')\n")))
        # The host edit makes the tree drift, so the canonical record-review
        # refuses (#77); what #92 guarantees is that the reviewer is not
        # blamed, the edit is attributed, and the verdict survives for
        # adoption once evidence is refreshed.
        with self.assertRaisesRegex(runtime.AgentLaunchError, "dispatch failed"):
            runtime.execute_launch(self._spec(cwd=scratch), popen_factory=factory, beacon_interval=0.01)
        sid, session, failure = self._session("reviewer")
        self.assertEqual(failure["category"], "dispatch_failed")
        self.assertTrue(failure.get("result_available"))
        self.assertEqual(session["result"]["payload"]["decision"], "approved")
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        edited = [e for e in events if e.get("kind") == "host_edited_during_review"]
        self.assertTrue(edited)
        self.assertIn("PROBE.py", json.dumps(edited[-1]))

    def test_in_tree_reviewer_that_writes_is_still_failed(self):
        self._phase5()
        probe = self.tmp / "PROBE.py"
        factory = mock.Mock(side_effect=lambda *a, **k: _FakeProcess(
            APPROVED, side_effect=lambda: probe.write_text("print('reviewer edit')\n")))
        with self.assertRaisesRegex(runtime.AgentLaunchError, "modified the project tree: PROBE.py appeared"):
            runtime.execute_launch(self._spec(cwd=self.tmp), popen_factory=factory, beacon_interval=0.01)
        _, _, failure = self._session("reviewer")
        self.assertEqual(failure["category"], "reviewer_modified_project")
        self.assertIn("PROBE.py", failure["changed_paths"])
        # #203: the record says what was seen, so a sighting can be read
        change = next(c for c in failure["changes"] if c["path"] == "PROBE.py")
        self.assertEqual(change["kind"], "appeared")
        self.assertIsNotNone(change["mtime"])
        self.assertGreaterEqual(change["seconds_after_session_start"], -1.0)

    def test_phase5_launch_refused_until_symptom_and_evidence_exist(self):
        self.init("Launch refusal fixture")
        self.set_criterion_state("passing", resolved=False)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        which = lambda name: "/usr/local/bin/codex" if name == "codex" else None
        with self.assertRaisesRegex(lib.HandsoffError, "record-symptom-resolved --evidence"):
            runtime.build_launch_spec(self.tmp, "reviewer", "review", which=which)
        records = [json.loads(line) for line in (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines()]
        resolved = run(["record-symptom-resolved", "--evidence", records[-1]["run_id"], "--by", "test-implementer"],
                       cwd=self.tmp)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "review", which=which)
        self.assertEqual(spec.role, "reviewer")

    def test_unborn_head_is_a_non_git_root(self):
        self.init("Unborn fixture")
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        # An unborn HEAD must behave exactly like a root with no .git at all.
        with self.assertRaisesRegex(lib.HandsoffError, "non-git root"):
            lib.repository_snapshot(self.tmp)

    def test_exit_zero_without_artifact_is_no_artifact(self):
        for stdout in ("", "I looked at the code and it seems fine.\n"):
            self._phase5()
            with self.assertRaisesRegex(runtime.AgentLaunchError, "without a protocol result"):
                runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(stdout)),
                                       beacon_interval=0.01)
            _, session, failure = self._session("reviewer")
            self.assertEqual((session["state"], failure["category"]), ("failed", "no_artifact"))
            self.tearDown(); self.setUp()

    def test_valid_line_completes(self):
        self._phase5()
        self.assertEqual(runtime.execute_launch(self._spec(), popen_factory=mock.Mock(return_value=_FakeProcess(APPROVED)),
                                                beacon_interval=0.01), 0)
        _, session, _ = self._session("reviewer")
        self.assertEqual(session["state"], "completed")
