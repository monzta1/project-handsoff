"""#345 #405: a managed reviewer's one valid verdict is delivered whatever
the process exit (0, non-zero, or a timeout): dispatched while the session
is live, and when that dispatch raises, adopted through the same path
session-result-adopt uses before the session ends. Never when the reviewer
touched the tree or the verdict is a #167 refused packet; when the record
command refuses it too the session ends dispatch_failed with the result
kept. A question asked before a budget error still reaches the Pilot."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin
from tests.engine_patch import patch_engine

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker  # noqa: E402,F401  (bound, so patch_engine reaches dispatch_reviewer_result)
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _FakeProcess:
    pid = 4242

    def __init__(self, stdout="", stderr="", returncode=0, side_effect=None, timed_out=False):
        if side_effect:
            side_effect()
        self.returncode = returncode
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        # #405: the runner's own wait times out once; the stop that follows
        # returns. No pid, so no real process group is ever signalled.
        self._timeouts = 1 if timed_out else 0
        if timed_out:
            self.pid = None

    def wait(self, timeout=None):
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired("fake-reviewer", timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


def reviewer_spec(case, role="reviewer", adapter="codex"):
    return runtime.LaunchSpec(
        role, adapter, "default", (f"/bin/{adapter}", "exec", "-"),
        str(case.tmp), "bounded prompt", token_budget=40_000,
        project_root=str(case.tmp.resolve()),
    )


def launch(case, spec=None, **process):
    """execute_launch with a fake child; returns (code, AgentLaunchError or None, stdout and stderr)."""
    side_effect = process.pop("side_effect", None)
    factory = mock.Mock(side_effect=lambda *a, **k: _FakeProcess(side_effect=side_effect, **process))
    out = io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
        try:
            code, error = runtime.execute_launch(spec or reviewer_spec(case), popen_factory=factory,
                                                 beacon_interval=0.01), None
        except runtime.AgentLaunchError as exc:
            code, error = None, exc
    return code, error, out.getvalue()


def dispatch_raises(reason="host bridge unavailable"):
    """#405: the direct dispatch fails, so the adoption path is what records."""
    return patch_engine("dispatch_reviewer_result", side_effect=lib.HandsoffError(reason))


def _verdict(**overrides):
    packet = {"kind": "implementation", "decision": "approved", "summary": "fine", "findings": [],
              "structural_blocker": False, "symptom_reproduced": "yes", "tests_executed": "yes"}
    packet.update(overrides)
    return "HANDSOFF_REVIEW_RESULT: " + json.dumps(packet) + "\n"


APPROVED = _verdict()
MALFORMED = "HANDSOFF_REVIEW_RESULT: {not json\n"
RULE_REFUSED = _verdict(tests_executed="yes, ran them")  # #167 packet rule
CRASH = "fatal: adapter connection reset\n"
BUDGET = "ERROR: shared rollout token budget exhausted\n"

# The two failed endings REQ-005 names: a non-zero exit that is not the
# budget-trailing OK, and a protocol error on another line (exit 0).
ENDINGS = {
    "non_zero_exit": lambda out: {"stdout": out, "stderr": CRASH, "returncode": 1},
    "protocol_error": lambda out: {"stdout": out + MALFORMED, "stderr": "", "returncode": 0},
}
# #405: a clean exit and a timeout deliver too; kept out of ENDINGS, whose
# refusal tests each name the failure category their ending produces.
DELIVERY_ENDINGS = {
    **ENDINGS,
    "clean_exit": lambda out: {"stdout": out, "stderr": "", "returncode": 0},
    "timeout": lambda out: {"stdout": out, "stderr": "", "returncode": -15, "timed_out": True},
}


class SessionResultAutoAdoptTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _fresh(self):
        self.tearDown()
        self.setUp()

    def _phase5(self):
        self.init("Auto-adopt fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _spec(self, role="reviewer", adapter="codex"):
        return reviewer_spec(self, role, adapter)

    def _launch(self, spec=None, **process):
        return launch(self, spec, **process)

    def _session(self, role="reviewer"):
        status = self.read_status()
        sid = lib.role_session_ids(status)[role]  # #420: an ended session leaves the pointer
        return status, sid, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)

    def _events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def _assert_adopted(self, ending, *, through_adoption=False):
        """#405: the verdict is recorded and the session ends completed with
        the process exit kept; through_adoption makes the direct dispatch
        raise, so the session-result-adopt replay is what records it."""
        process = DELIVERY_ENDINGS[ending](APPROVED)
        if through_adoption:
            with dispatch_raises():
                code, error, out = self._launch(**process)
        else:
            code, error, out = self._launch(**process)
        self.assertIsNone(error, f"{ending}: {error}\n{out}")
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["exit_code"], 124 if ending == "timeout" else process["returncode"])
        self.assertIsNone(failure)
        # the verdict went through record-review, attributed to the reviewer
        self.assertIsNotNone(status["review"])
        self.assertEqual(status["review"]["by"], session["actor"])
        adopted = self._events("session_result_adopted")
        if through_adoption:
            self.assertIsNotNone(session["result"]["adopted_at"])
            self.assertTrue(session["result"]["adopted_automatically"])
            self.assertEqual(status["review"]["adopted_session"], sid)
            self.assertEqual([(e["session_id"], e["automatic"]) for e in adopted], [(sid, True)])
            self.assertIn("adopted automatically", out)
        else:
            self.assertIsNone(session["result"]["adopted_at"], "dispatched directly, never replayed")
            self.assertEqual(adopted, [])
        # the reservation is released: no live reviewer, and recovery is not paused on it
        self.assertIn(session["state"], lib.AGENT_SESSION_TERMINAL_STATES)
        assessment = lib.recovery_assessment(status, lib.load_config(self.tmp), {}, [], root=self.tmp)
        self.assertNotEqual(assessment["reason"], "non_recoverable_failure")
        return sid

    def _assert_not_adopted(self, error):
        self.assertIsInstance(error, runtime.AgentLaunchError)
        status, sid, session, failure = self._session()
        self.assertEqual(session["state"], "failed")
        self.assertIsNone(status["review"])
        self.assertFalse((failure or {}).get("adopted"))
        if isinstance(session.get("result"), dict):
            self.assertIsNone(session["result"]["adopted_at"])
        self.assertEqual(self._events("session_result_adopted"), [])
        return status, sid, session, failure

    # #405 REQ-001: exit 0, 1 and a timeout (124) all deliver the verdict,
    # directly and through the adoption replay when the dispatch raises
    def test_a_valid_review_is_adopted_after_exit_0_1_and_124(self):
        for ending in ("clean_exit", "non_zero_exit", "timeout"):
            for through_adoption in (False, True):
                with self.subTest(ending=ending, through_adoption=through_adoption):
                    self._phase5()
                    self._assert_adopted(ending, through_adoption=through_adoption)
                    self._fresh()

    def test_one_valid_verdict_is_adopted_after_a_protocol_error_on_another_line(self):
        self._phase5()
        self._assert_adopted("protocol_error")
        _, _, session, _ = self._session()
        # the malformed line is kept on the transcript, never on the result
        self.assertEqual(session["result"]["payload"]["decision"], "approved")

    def test_a_malformed_result_alone_is_kept_and_never_adopted(self):
        for ending in ("clean_exit", "non_zero_exit", "timeout"):
            with self.subTest(ending=ending):
                self._phase5()
                process = DELIVERY_ENDINGS[ending](MALFORMED)
                code, error, out = self._launch(**process)
                self.assertIsInstance(error, runtime.AgentLaunchError)
                status, sid, session, failure = self._session()
                self.assertIsNone(status["review"])
                self.assertIsNone(session.get("result"))
                self.assertEqual(self._events("session_result_adopted"), [])
                # nothing to adopt by hand either
                adopted = run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)
                self.assertNotEqual(adopted.returncode, 0)
                self.assertIn("no persisted result", adopted.stdout)
                self._fresh()

    def test_a_second_adoption_changes_nothing(self):
        self._phase5()
        sid = self._assert_adopted("non_zero_exit", through_adoption=True)
        before_status = self.read_status()
        before_events = (self.tmp / "handsoff-events.jsonl").read_text()
        again = run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("result is already adopted", again.stdout)
        self.assertEqual(self.read_status(), before_status)
        self.assertEqual((self.tmp / "handsoff-events.jsonl").read_text(), before_events)

    def test_dispatch_and_adoption_both_failing_ends_dispatch_failed_with_the_result_kept(self):
        # #341 makes record-review refuse tests_executed "no"; the dispatch is
        # made to raise too, so both reasons are named
        self._phase5()
        children = []

        def factory(*_args, **_kwargs):
            children.append(_FakeProcess(**ENDINGS["non_zero_exit"](_verdict(tests_executed="no"))))
            return children[-1]
        out = io.StringIO()
        with dispatch_raises("host bridge unavailable"), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", out), self.assertRaises(lib.HandsoffError) as paused:
            runtime.execute_with_recovery(self._spec(), popen_factory=factory,
                                          snapshotter=lambda root: {"head": None, "branch": None, "dirty": False})
        # no automatic replacement: one child, and the pause names the exact adoption command
        self.assertEqual(len(children), 1, "dispatch_failed launched a replacement")
        status, sid, session, failure = self._assert_not_adopted(runtime.AgentLaunchError("x", None))
        self.assertEqual(failure["category"], "dispatch_failed")
        self.assertTrue(failure["result_available"])
        self.assertIn("dispatch: host bridge unavailable", failure["reason"])
        self.assertIn("adoption:", failure["reason"])
        self.assertEqual(session["result"]["payload"]["tests_executed"], "no")
        self.assertIn("agent replacement paused", str(paused.exception))
        self.assertIn(runtime.session_result_adopt_command(sid), str(paused.exception))
        self.assertIn(runtime.session_result_adopt_command(sid), out.getvalue())

    def test_an_approval_at_phase_7_after_engine_or_mechanics_drift_is_adopted_as_a_reaffirmation(self):
        # the 2026-10-07 sentinel-sandbox#4 case: the run sits at Phase 7 and
        # the review was recorded by an older engine (engine:version entry)
        self._phase7()
        self._legacy_review(engine_version="v0.5.8")
        toml = self.tmp / "handsoff.toml"
        code, error, out = self._launch(**DELIVERY_ENDINGS["clean_exit"](APPROVED))
        self.assertIsNone(error, out)
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        self.assertEqual(session["state"], "completed")
        self.assertIsNotNone(session["result"]["adopted_at"])
        self.assertEqual(status["review"]["rules_hash"], lib.rules_set_hash(self.tmp))
        self.assertEqual(status["phase_number"], 7)
        reaffirmed = self._events("review_reaffirmed")
        self.assertEqual(reaffirmed[-1]["rules_changed"], ["engine:version"])
        # and a later mechanics edit ([models], [agent_budget]) changes nothing a review binds
        toml.write_text(toml.read_text().replace("reviewer = 200000", "reviewer = 150000"))
        self.assertEqual(lib.rules_binding_errors(self.tmp, lib.load_config(self.tmp), status["review"],
                                                  "review gate"), [])

    def test_an_approval_at_phase_7_after_a_policy_change_is_not_adopted(self):
        for edit in ("AGENTS.md", "checks.commands"):
            with self.subTest(edit=edit):
                self._phase7()
                if edit == "AGENTS.md":
                    (self.tmp / "AGENTS.md").write_text("# new rules for agents\n")
                    named = "AGENTS.md"
                else:
                    toml = self.tmp / "handsoff.toml"
                    toml.write_text(toml.read_text().replace('commands = ["true"]', 'commands = ["true", "false"]'))
                    named = "handsoff.toml#policy"
                code, error, out = self._launch(**DELIVERY_ENDINGS["clean_exit"](APPROVED))
                self.assertIsInstance(error, runtime.AgentLaunchError)
                status, sid, session, failure = self._session()
                self.assertEqual(failure["category"], "dispatch_failed")
                self.assertIn("requires Phase 5", failure["reason"])
                self.assertIn("SESSION_RESULT_AUTOADOPT_REFUSED: the rules set changed in policy entries", out)
                self.assertIn(named, out)
                self.assertIn("a fresh review is required", out)
                self.assertIsNone(session["result"]["adopted_at"])
                self._fresh()

    # fix round 1: verify clears the recorded review, so the readoption path
    # must judge the rules the reviewer session ran under, not status.review
    def _adopt_edit_verify(self, edit):
        self._phase5()
        sid = self._assert_adopted("non_zero_exit", through_adoption=True)
        self.assertIsInstance(self._session()[2]["rules_entries"], dict)
        edit()
        verified = run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIsNone(self.read_status()["review"], "verify clears the recorded review")
        return sid, run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)

    def test_readopting_an_approval_after_an_agents_md_edit_and_verify_is_refused(self):
        sid, again = self._adopt_edit_verify(
            lambda: (self.tmp / "AGENTS.md").write_text("# new rules for agents\n"))
        self.assertNotEqual(again.returncode, 0, again.stdout)
        self.assertIn("SESSION_RESULT_ADOPT_REFUSED: the rules set changed in policy entries (AGENTS.md)",
                      again.stdout)
        self.assertIn("a fresh review is required", again.stdout)
        status = self.read_status()
        self.assertIsNone(status["review"])
        self.assertNotIn("readoptions", status["agent_sessions"][sid]["result"])

    def test_readopting_an_approval_after_a_mechanics_edit_and_verify_still_adopts(self):
        toml = self.tmp / "handsoff.toml"
        sid, again = self._adopt_edit_verify(
            lambda: toml.write_text(toml.read_text().replace("reviewer = 200000", "reviewer = 150000")))
        self.assertIn("reviewer = 150000", toml.read_text(), "the mechanics edit applied")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("SESSION_RESULT_ADOPTED", again.stdout)
        status = self.read_status()
        self.assertIsNotNone(status["review"])
        self.assertEqual(len(status["agent_sessions"][sid]["result"]["readoptions"]), 1)

    def test_a_reviewer_session_without_a_recorded_rules_set_fails_closed(self):
        self._phase5()
        sid = self._assert_adopted("non_zero_exit", through_adoption=True)
        status = self.read_status()
        del status["agent_sessions"][sid]["rules_entries"]
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_legacy",
                   event_message="a reviewer session launched before rules_entries", actor="test")
        run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp)
        again = run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("recorded no rules set at launch", again.stdout)

    def _phase7(self):
        self.init("Auto-adopt at Phase 7")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="test-implementer", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _legacy_review(self, *, engine_version, handsoff_toml_hash=None):
        """Rewrite the recorded review's binding as a pre-#406 engine wrote
        it: the whole-file handsoff.toml hash and an engine:version entry."""
        import hashlib
        status = self.read_status()
        entries = {k: v for k, v in status["review"]["rules_entries"].items()
                   if k not in (lib.RULES_POLICY_ENTRY, lib.RULES_MECHANICS_ENTRY)}
        entries["handsoff.toml"] = handsoff_toml_hash or hashlib.sha256(
            (self.tmp / "handsoff.toml").read_bytes()).hexdigest()
        entries["engine:version"] = engine_version
        status["review"]["rules_entries"] = entries
        status["review"]["rules_hash"] = hashlib.sha256(json.dumps(
            entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status, event_kind="fixture_legacy",
                   event_message="a review recorded by an older engine", actor="test")

    # REQ-005 refusals, each under both endings, each beside a control that adopts
    def test_a_reviewer_that_modified_the_tree_is_failed_and_never_adopted(self):
        for ending in ENDINGS:
            with self.subTest(ending=ending):
                self._phase5()
                probe = self.tmp / "PROBE.py"
                code, error, _ = self._launch(**ENDINGS[ending](APPROVED),
                                              side_effect=lambda: probe.write_text("print('reviewer edit')\n"))
                _, _, _, failure = self._assert_not_adopted(error)
                self.assertEqual(failure["category"], "reviewer_modified_project")
                self.assertIn("PROBE.py", failure["changed_paths"])
                self._fresh()
                self._phase5()
                self._assert_adopted(ending)
                self._fresh()

    def test_a_rule_refused_packet_is_never_adopted_automatically(self):
        for ending in ENDINGS:
            for output in (RULE_REFUSED, APPROVED + RULE_REFUSED):
                with self.subTest(ending=ending, beside_a_valid_verdict=output != RULE_REFUSED):
                    self._phase5()
                    code, error, _ = self._launch(**ENDINGS[ending](output))
                    _, sid, session, _ = self._assert_not_adopted(error)
                    self._fresh()
            self._phase5()
            self._assert_adopted(ending)
            self._fresh()

    def test_a_rule_refused_packet_on_stderr_blocks_a_stdout_approval(self):
        # a valid stdout verdict must not hide a #167 packet on the other stream
        self._phase5()
        code, error, out = self._launch(stdout=APPROVED, stderr=RULE_REFUSED + CRASH, returncode=1)
        self._assert_not_adopted(error)
        self.assertEqual(self._events("session_result_adopted"), [])

    def test_a_rule_refused_stderr_packet_blocks_adoption_on_a_clean_exit(self):
        # attempt-2 finding: exit 0, approval plus a malformed line on stdout,
        # a #167 packet on stderr; the packet must block adoption here too
        self._phase5()
        code, error, out = self._launch(stdout=APPROVED + MALFORMED, stderr=RULE_REFUSED, returncode=0)
        self._assert_not_adopted(error)
        self.assertEqual(self._events("session_result_adopted"), [])

    def test_a_valid_stderr_verdict_is_adopted_on_a_clean_exit(self):
        # attempt-2 finding: exit 0, only a malformed line on stdout, the one
        # valid verdict on stderr; that verdict is adopted
        self._phase5()
        code, error, out = self._launch(stdout=MALFORMED, stderr=APPROVED, returncode=0)
        self.assertIsNone(error, out)
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        # #405: delivered by the direct dispatch, so recorded with the session bound
        self.assertEqual(session["state"], "completed")
        self.assertEqual(status["review"]["by"], session["actor"])

    def test_an_identical_verdict_on_both_streams_is_one_verdict(self):
        # #114: Codex can repeat its final message on stderr; the copy is
        # the same verdict, so the session still has exactly one to adopt
        self._phase5()
        code, error, out = self._launch(stdout=APPROVED, stderr=APPROVED + CRASH, returncode=1)
        self.assertIsNone(error, out)
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        self.assertEqual(session["state"], "completed")
        self.assertEqual(status["review"]["by"], session["actor"])
        self.assertEqual(len(self._events("review_approved")), 1)

    def test_a_verdict_the_record_command_refuses_leaves_the_failed_session(self):
        # #341: tests_executed no is refused while a criterion requires checks
        for ending in ENDINGS:
            with self.subTest(ending=ending):
                self._phase5()
                code, error, out = self._launch(**ENDINGS[ending](_verdict(tests_executed="no")))
                status, sid, session, failure = self._assert_not_adopted(error)
                self.assertEqual(session["result"]["payload"]["tests_executed"], "no")
                self.assertNotEqual(failure["category"], "orchestration_noop")
                self.assertIn("SESSION_RESULT_AUTOADOPT_REFUSED:", out)
                refused = self._events("session_result_autoadopt_refused")
                self.assertEqual([e["session_id"] for e in refused], [sid])
                self.assertLessEqual(len(refused[0]["reason"]), 200)
                self._fresh()

    # REQ-006
    def _question(self, text):
        return "HANDSOFF_QUESTION: " + json.dumps({"text": text, "options": ["Concise", "Full"],
                                                   "recommended": "Concise"})

    def _open_questions(self):
        status = self.read_status()
        return status, lib.open_questions(status)

    def test_a_question_inside_an_assistant_event_survives_budget_exhaustion(self):
        self.init("Question before budget")
        event = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Before I go on:\n" + self._question("Which path, assistant?")}]}}
        self._launch(self._spec("architect", "claude"), stdout=json.dumps(event) + "\n",
                     stderr=BUDGET, returncode=1)
        status, questions = self._open_questions()
        sid = lib.role_session_ids(status)["architect"]  # #420
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, assistant?", sid)])

    def test_a_raw_question_before_budget_exhaustion_is_recorded_once(self):
        # Codex can route its last message to stderr on a budget error
        self.init("Raw question before budget")
        question = self._question("Which path, raw?")
        self._launch(self._spec("architect"), stdout="", stderr=question + "\n" + BUDGET, returncode=1)
        status, questions = self._open_questions()
        sid = lib.role_session_ids(status)["architect"]  # #420
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, raw?", sid)])
        # the same question on both streams is one record for the launch
        self._fresh()
        self.init("Raw question on both streams")
        self._launch(self._spec("architect"), stdout=question + "\n", stderr=question + "\n" + BUDGET,
                     returncode=1)
        status, questions = self._open_questions()
        sid = lib.role_session_ids(status)["architect"]  # #420
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, raw?", sid)])

    def test_a_question_before_long_stderr_diagnostics_is_recorded_once(self):
        # 700+ diagnostic lines push the question far out of the 8192-char tail
        self.init("Question before diagnostics")
        question = self._question("Which path, early?")
        diagnostics = "".join(f"diagnostic line {n}: retrying adapter call\n" for n in range(750))
        self._launch(self._spec("architect"), stdout="", stderr=question + "\n" + diagnostics + BUDGET,
                     returncode=1)
        status, questions = self._open_questions()
        sid = lib.role_session_ids(status)["architect"]  # #420
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, early?", sid)])

    # REQ-007
    def test_the_snapshot_and_reference_mark_an_auto_adopted_verdict(self):
        self._phase5()
        sid = self._assert_adopted("non_zero_exit", through_adoption=True)
        snapshot = dashboard.build_snapshot(self.tmp)
        reviewer = next(entry for entry in snapshot["crew"] if entry["key"] == "reviewer")
        marked = reviewer["session"]["result_adopted"]
        self.assertEqual((marked["session_id"], marked["automatic"]), (sid, True))
        self.assertEqual(marked["at"], self.read_status()["agent_sessions"][sid]["result"]["adopted_at"])
        self.assertEqual(reviewer["session"]["state"], "adopted")
        from tests.test_snapshot_contract import SCHEMA, validate
        self.assertEqual(validate(snapshot["crew"], SCHEMA["properties"]["crew"], "$.crew"), [])
        self.assertNotEqual(validate([{"session": {"result_adopted": {"session_id": sid}}}],
                                     SCHEMA["properties"]["crew"]), [])
        reference = " ".join((BIN.parent / "docs" / "REFERENCE.md").read_text().split())
        self.assertIn("an automatically adopted verdict satisfies its gate while marked adopted", reference)


if __name__ == "__main__":
    import unittest
    unittest.main()
