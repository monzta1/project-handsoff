"""#345: a managed reviewer that prints one valid verdict and then ends
failed has the verdict adopted through the same path session-result-adopt
uses, unless the reviewer touched the tree, the verdict is a #167 refused
packet, or the record command refuses it. A question asked before a budget
error still reaches the Pilot."""
from __future__ import annotations

import io
import json
import shutil
import sys
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _FakeProcess:
    pid = 4242

    def __init__(self, stdout="", stderr="", returncode=0, side_effect=None):
        if side_effect:
            side_effect()
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
        return runtime.LaunchSpec(
            role, adapter, "default", (f"/bin/{adapter}", "exec", "-"),
            str(self.tmp), "bounded prompt", token_budget=40_000,
            project_root=str(self.tmp.resolve()),
        )

    def _launch(self, spec=None, **process):
        side_effect = process.pop("side_effect", None)
        factory = mock.Mock(side_effect=lambda *a, **k: _FakeProcess(side_effect=side_effect, **process))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            try:
                code, error = runtime.execute_launch(spec or self._spec(), popen_factory=factory,
                                                     beacon_interval=0.01), None
            except runtime.AgentLaunchError as exc:
                code, error = None, exc
        return code, error, out.getvalue()

    def _session(self, role="reviewer"):
        status = self.read_status()
        sid = status["current_agent_sessions"][role]
        return status, sid, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)

    def _events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def _assert_adopted(self, ending):
        code, error, out = self._launch(**ENDINGS[ending](APPROVED))
        self.assertIsNone(error, f"{ending}: {error}\n{out}")
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        # the session stays failed, with a launcher category, marked adopted
        self.assertEqual(session["state"], "failed")
        self.assertNotEqual(failure["category"], "orchestration_noop")
        self.assertTrue(failure.get("adopted"))
        self.assertIsNotNone(session["result"]["adopted_at"])
        self.assertTrue(session["result"]["adopted_automatically"])
        # the verdict went through record-review, attributed to the reviewer
        self.assertIsNotNone(status["review"])
        self.assertEqual(status["review"]["adopted_session"], sid)
        self.assertEqual(status["review"]["by"], session["actor"])
        adopted = self._events("session_result_adopted")
        self.assertEqual([(e["session_id"], e["automatic"]) for e in adopted], [(sid, True)])
        self.assertIn("adopted automatically", out)
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

    # REQ-005: both endings adopt
    def test_one_valid_verdict_is_adopted_after_a_non_zero_exit(self):
        self._phase5()
        self._assert_adopted("non_zero_exit")

    def test_one_valid_verdict_is_adopted_after_a_protocol_error_on_another_line(self):
        self._phase5()
        self._assert_adopted("protocol_error")
        _, _, _, failure = self._session()
        self.assertEqual(failure["category"], "non_zero_exit")

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
        sid = status["current_agent_sessions"]["architect"]
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, assistant?", sid)])

    def test_a_raw_question_before_budget_exhaustion_is_recorded_once(self):
        # Codex can route its last message to stderr on a budget error
        self.init("Raw question before budget")
        question = self._question("Which path, raw?")
        self._launch(self._spec("architect"), stdout="", stderr=question + "\n" + BUDGET, returncode=1)
        status, questions = self._open_questions()
        sid = status["current_agent_sessions"]["architect"]
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, raw?", sid)])
        # the same question on both streams is one record for the launch
        self._fresh()
        self.init("Raw question on both streams")
        self._launch(self._spec("architect"), stdout=question + "\n", stderr=question + "\n" + BUDGET,
                     returncode=1)
        status, questions = self._open_questions()
        sid = status["current_agent_sessions"]["architect"]
        self.assertEqual([(q["text"], q["session_id"]) for q in questions], [("Which path, raw?", sid)])

    # REQ-007
    def test_the_snapshot_and_reference_mark_an_auto_adopted_verdict(self):
        self._phase5()
        sid = self._assert_adopted("non_zero_exit")
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
