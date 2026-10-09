"""#440: a Codex reviewer that prints one schema-valid verdict and then
exits 1 past its token budget has that verdict delivered (#405), never a
`non_zero_exit` failure with the result only adoptable by hand.

Cause: Codex's stderr is a transcript. It repeats the final message (#114,
de-duplicated) and echoes the prompt it was given. Two copies were counted as
a second verdict: a compact reviewer's stdout verdict was rewritten to
`tests_executed: no` only when persisted, so its unchanged stderr copy no
longer compared equal; and a verdict line inside the launch's own prompt (the
playbook's protocol example) was parsed as the reviewer's. With two verdicts
`_one_valid_verdict` was false, so the exit-1 path ended `non_zero_exit`
while the persisted result sat on the session."""
from __future__ import annotations

import dataclasses
import io
import json
import shutil
import subprocess
import sys
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin
from tests.test_session_result_autoadopt import APPROVED, MALFORMED, launch, reviewer_spec

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

BUDGET = "tokens used\n96,431\n"  # what the E4 reviewer printed before its exit 1
PROTOCOL_EXAMPLE = next(line for line in (BIN.parent / "playbook" / "protocol.md").read_text().splitlines()
                        if line.startswith(runtime.REVIEW_RESULT_PREFIX))
SLICE = ({"path": "product.txt", "start": 1, "end": 1},)


def codex_child(verdict: str, newline: bool) -> str:
    """A fake Codex reviewer: the verdict as its final stdout message (with or
    without the trailing newline), the transcript and budget meter on
    stderr, then exit 1."""
    return ("import sys\n"
            f"sys.stderr.write({('codex' + chr(10) + verdict + chr(10))!r})\n"
            f"sys.stdout.write({verdict!r} + {chr(10) if newline else ''!r})\n"
            "sys.stdout.flush()\n"
            f"sys.stderr.write({BUDGET!r})\n"
            "sys.exit(1)\n")


class VerdictBeforeOverrunTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _fresh(self):
        self.tearDown()
        self.setUp()

    def _phase5(self):
        self.init("Verdict before overrun")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _session(self):
        status = self.read_status()
        sid = lib.role_session_ids(status)["reviewer"]
        return status, sid, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)

    def _real_child(self, spec):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
            try:
                return runtime.execute_launch(spec, popen_factory=subprocess.Popen, beacon_interval=0.01), \
                    None, out.getvalue()
            except runtime.AgentLaunchError as exc:
                return None, exc, out.getvalue()

    def _assert_recorded(self, code, error, out, *, exit_code=1):
        self.assertIsNone(error, out)
        self.assertEqual(code, 0)
        status, sid, session, failure = self._session()
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["exit_code"], exit_code)
        self.assertIsNone(failure)
        self.assertEqual(session["result"]["kind"], "review")
        self.assertEqual(session["result"]["payload"]["decision"], "approved")
        self.assertEqual(session["result"]["payload"]["summary"], "fine")
        self.assertIsNotNone(status["review"])
        self.assertEqual(status["review"]["by"], session["actor"])

    def test_a_verdict_then_budget_exit_1_is_recorded_with_and_without_a_trailing_newline(self):
        verdict = APPROVED.rstrip("\n")
        for newline in (True, False):
            with self.subTest(newline=newline):
                self._phase5()
                spec = dataclasses.replace(reviewer_spec(self),
                                           argv=(sys.executable, "-c", codex_child(verdict, newline)))
                self._assert_recorded(*self._real_child(spec))
                self._fresh()

    def test_a_prompt_echo_of_the_protocol_example_is_not_a_second_verdict(self):
        # before the fix: failed, non_zero_exit, result kept but not delivered
        verdict = APPROVED.rstrip("\n")
        for newline in (True, False):
            with self.subTest(newline=newline):
                self._phase5()
                spec = dataclasses.replace(reviewer_spec(self), stdin="## protocol\n\n" + PROTOCOL_EXAMPLE + "\n")
                stderr = "user\n## protocol\n\n" + PROTOCOL_EXAMPLE + "\ncodex\n" + verdict + "\n" + BUDGET
                self._assert_recorded(*launch(self, spec, stdout=verdict + ("\n" if newline else ""),
                                              stderr=stderr, returncode=1))
                self._fresh()

    def test_a_verdict_the_reviewer_wrote_on_stderr_alone_still_counts(self):
        # the echo filter drops only lines the launch itself sent
        self._phase5()
        spec = dataclasses.replace(reviewer_spec(self), stdin="## protocol\n\n" + PROTOCOL_EXAMPLE + "\n")
        self._assert_recorded(*launch(self, spec, stdout="", stderr="codex\n" + APPROVED + BUDGET, returncode=1))

    def test_a_compact_reviewers_verdict_on_both_streams_is_one_verdict(self):
        # before the fix: failed, non_zero_exit (two verdicts); now delivered,
        # and the record command's #341 gate decides (a compact approval is
        # tests_executed no), so the result is kept as dispatch_failed
        verdict = APPROVED.rstrip("\n")
        for newline in (True, False):
            with self.subTest(newline=newline):
                self._phase5()
                spec = dataclasses.replace(reviewer_spec(self), compact_scope=SLICE)
                code, error, out = launch(self, spec, stdout=verdict + ("\n" if newline else ""),
                                          stderr="codex\n" + APPROVED + BUDGET, returncode=1)
                self.assertIsInstance(error, runtime.AgentLaunchError)
                self.assertNotIn("codex exited with status", str(error))
                status, sid, session, failure = self._session()
                self.assertEqual(failure["category"], "dispatch_failed")
                self.assertTrue(failure["result_available"])
                self.assertIn("SESSION_RESULT_AUTOADOPT_REFUSED", out)
                self.assertEqual(session["result"]["payload"]["tests_executed"], "no")
                self._fresh()

    def test_a_malformed_verdict_then_budget_exit_1_fails_without_adoption(self):
        for newline in (True, False):
            with self.subTest(newline=newline):
                self._phase5()
                spec = dataclasses.replace(reviewer_spec(self),
                                           argv=(sys.executable, "-c",
                                                 codex_child(MALFORMED.rstrip("\n"), newline)))
                code, error, out = self._real_child(spec)
                self.assertIsInstance(error, runtime.AgentLaunchError)
                status, sid, session, failure = self._session()
                self.assertEqual(session["state"], "failed")
                self.assertIsNone(session.get("result"))
                self.assertIsNone(status["review"])
                self.assertNotIn("result_available", failure)
                events = [json.loads(line) for line in
                          (self.tmp / "handsoff-events.jsonl").read_text().splitlines() if line.strip()]
                self.assertEqual([e for e in events if e.get("kind") == "session_result_adopted"], [])
                adopted = run(["session-result-adopt", "--session", sid, "--by", "test-pilot"], cwd=self.tmp)
                self.assertIn("no persisted result", adopted.stdout)
                self._fresh()
