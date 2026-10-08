"""#408: managed roles keep their work in the foreground. The reviewer,
implementer and architect prompts say so; a session that exits cleanly with
no protocol result while background work it started is still outstanding
is background_abandoned: recoverable, and no review round is spent. A
timeout, crash or protocol error keeps its own class whatever ran in the
background; background work that finished first, or none at all, leaves a
no-result exit no_artifact."""
from __future__ import annotations

import json
import shutil
import sys

from tests.guards import guard
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase
from tests.test_session_result_autoadopt import APPROVED, MALFORMED, launch, reviewer_spec
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


def _event(kind, block):
    return json.dumps({"type": kind, "message": {"role": kind, "content": [block]}}) + "\n"


def _tool_use(call_id, name, **arguments):
    return _event("assistant", {"type": "tool_use", "id": call_id, "name": name, "input": arguments})


def _tool_result(call_id, text):
    return _event("user", {"type": "tool_result", "tool_use_id": call_id, "content": text})


BACKGROUND_STARTED = (_tool_use("toolu_1", "Bash", command="python3 probe.py", run_in_background=True)
                      + _tool_result("toolu_1", "Command running in background with ID: bash_1"))
BACKGROUND_FINISHED = (_tool_use("toolu_2", "BashOutput", bash_id="bash_1")
                       + _tool_result("toolu_2", "<status>completed</status>\n<exit_code>0</exit_code>"))
DETACHED = _tool_use("toolu_3", "Bash", command="nohup python3 probe.py > probe.log 2>&1 &")
FOREGROUND = _tool_use("toolu_4", "Bash", command="python3 -m unittest tests.test_x 2>&1 && echo done")


class BackgroundRuleTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Background rule #408")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _end(self, **process):
        """One reviewer launch; returns (category or None, review_round, session state)."""
        code, error, out = launch(self, reviewer_spec(self, "reviewer", "claude"), **process)
        status = self.read_status()
        sid = lib.role_session_ids(status)["reviewer"]
        failure = (status.get("agent_failures") or {}).get(sid) or {}
        return failure.get("category"), status.get("review_round"), status["agent_sessions"][sid]["state"]

    @guard
    def test_each_managed_role_prompt_carries_the_foreground_rule(self):
        for role in ("reviewer", "implementer", "architect"):
            with self.subTest(role=role):
                text = " ".join((BIN.parent / "prompts" / f"{role}.md").read_text(encoding="utf-8").split())
                self.assertIn("Run every command in the foreground", text)
                self.assertIn("The session ends when the turn ends", text)

    def test_a_clean_exit_with_outstanding_background_work_and_no_result_is_background_abandoned(self):
        for started in (BACKGROUND_STARTED, DETACHED):
            with self.subTest(detached=started is DETACHED):
                category, rounds, state = self._end(stdout=started, returncode=0)
                self.assertEqual((category, state), ("background_abandoned", "failed"))
                self.assertIn(category, lib.RECOVERABLE_FAILURE_CATEGORIES)
                self.assertIn(category, lib.FAILURE_CATEGORIES)
                status = self.read_status()
                assessment = lib.recovery_assessment(status, lib.load_config(self.tmp), {}, [], root=self.tmp)
                self.assertNotEqual(assessment["reason"], "non_recoverable_failure")
                # the relaunch reuses the open attempt: the round count is unchanged
                category, again, state = self._end(stdout=APPROVED, returncode=0)
                self.assertEqual((category, state), (None, "completed"))
                self.assertEqual(again, rounds, "background_abandoned spent a review round")
                self.tearDown()
                self.setUp()

    def test_timeout_crash_and_protocol_error_after_a_background_call_keep_their_classes_and_count(self):
        endings = {
            "timeout": ({"returncode": -15, "timed_out": True}, "timeout"),
            "crash": ({"returncode": -9}, "process_crash"),
            "protocol_error": ({"returncode": 0, "extra": MALFORMED}, "orchestration_noop"),
        }
        for name, (process, expected) in endings.items():
            with self.subTest(ending=name):
                results = []
                for stdout in ("", BACKGROUND_STARTED):
                    child = dict(process)
                    extra = child.pop("extra", "")
                    results.append(self._end(stdout=stdout + extra, **child))
                    self.tearDown()
                    self.setUp()
                without, with_background = results
                self.assertEqual(with_background[0], expected, results)
                self.assertEqual(with_background, without, "the background call changed the class or the count")

    def test_completed_background_work_then_a_clean_no_result_exit_is_no_artifact(self):
        category, _, state = self._end(stdout=BACKGROUND_STARTED + BACKGROUND_FINISHED, returncode=0)
        self.assertEqual((category, state), ("no_artifact", "failed"))

    def test_a_killed_background_call_no_longer_counts_as_outstanding(self):
        killed = (_tool_use("toolu_5", "KillShell", shell_id="bash_1")
                  + _tool_result("toolu_5", "Successfully killed shell: bash_1"))
        category, _, _ = self._end(stdout=BACKGROUND_STARTED + killed, returncode=0)
        self.assertEqual(category, "no_artifact")

    def test_a_quoted_ampersand_and_a_collected_job_are_not_outstanding(self):
        for command in ("printf 'a & b'", 'echo "x &"', "echo a \\& b", "sleep 0 & wait",
                        "sleep 0 & sleep 0 & wait", "bash -lc 'sleep 0 & wait'"):
            with self.subTest(command=command):
                work = runtime._BackgroundWork()
                work.feed(_tool_use("toolu_6", "Bash", command=command))
                self.assertEqual(work.outstanding, [])
                category, _, _ = self._end(stdout=_tool_use("toolu_6", "Bash", command=command), returncode=0)
                self.assertEqual(category, "no_artifact")
                self.tearDown()
                self.setUp()
        for command in ("sleep 9 &", "sleep 0 & wait; sleep 9 &", "sleep 9 & disown; wait",
                        "bash -lc 'nohup sleep 9 > log 2>&1 &'", "setsid -f sleep 9"):
            with self.subTest(command=command):
                work = runtime._BackgroundWork()
                work.feed(_tool_use("toolu_7", "Bash", command=command))
                self.assertEqual(work.outstanding, ["toolu_7"])

    def test_jobs_are_tracked_through_wait_disown_comments_and_quotes(self):
        for command, outstanding in (
                ("sleep 90 & sleep 0 & wait $!", True),  # wait $! collects only sleep 0
                ("printf done # run tests & review", False),  # a comment starts nothing
                ("sleep 90 & wait", False),
                ("sleep 90 & disown; wait", True),
                ("a & b & wait %1", True),  # b is never collected
                ("a & b & wait %1 %2", False),
                ("sleep 90 & disown %1\nwait", True),
                ("sleep 90 &\nwait", False),
                ("nohup sleep 90 & wait", False),  # nohup's child is still the shell's; wait collects it
                ("nohup sleep 0 & wait", False),
                ("nohup sleep 90 &", True),
                ("sleep 90 & wait '$!'; true", True),  # a quoted '$!' is literal: wait fails, sleep runs on
                ('sleep 90 & wait "$!"', False),  # double quotes still expand $!
                ("sleep 90 & wait \\$!; true", True),  # an escaped \\$! is literal too
                ('bash -c "sleep 90 & wait \\$!"', False),  # inside double quotes the nested shell gets $!
                ("bash -c 'sleep 90 & wait \\$!'", True),  # inside single quotes the nested shell gets \\$!
                ("echo \"#x\" &", True),
                ("echo a#b", False),
                ("echo 'unbalanced &", False),  # unparseable: never background
                ("bash -lc 'sleep 90 & sleep 0 & wait $!'", True)):
            with self.subTest(command=command):
                self.assertEqual(runtime._detaches(command), outstanding)
                work = runtime._BackgroundWork()
                work.feed(_tool_use("toolu_9", "Bash", command=command))
                self.assertEqual(work.outstanding, ["toolu_9"] if outstanding else [])

    def test_a_run_in_background_task_is_outstanding_until_the_stream_reports_it_finished(self):
        notified = _event("user", {"type": "text", "text": (
            "<task-notification>\n<task-id>bash_1</task-id>\n<status>completed</status>\n"
            "<summary>Background command completed</summary>\n</task-notification>")})
        system = json.dumps({"type": "system", "subtype": "task_notification",
                             "task_id": "bash_1", "status": "completed"}) + "\n"
        still_running = (_tool_use("toolu_8", "BashOutput", bash_id="bash_1")
                         + _tool_result("toolu_8", "<status>running</status>"))
        for name, stdout, outstanding in (
                ("still running at the last turn", BACKGROUND_STARTED + still_running, ["toolu_1"]),
                ("task notification", BACKGROUND_STARTED + notified, []),
                ("system notification", BACKGROUND_STARTED + system, []),
                ("poll reports completed", BACKGROUND_STARTED + BACKGROUND_FINISHED, [])):
            with self.subTest(name):
                work = runtime._BackgroundWork()
                for line in stdout.splitlines():
                    work.feed(line)
                self.assertEqual(work.outstanding, outstanding)
                category, _, _ = self._end(stdout=stdout, returncode=0)
                self.assertEqual(category, "background_abandoned" if outstanding else "no_artifact")
                self.tearDown()
                self.setUp()

    def test_no_background_work_and_no_result_keeps_no_artifact(self):
        for stdout in ("", FOREGROUND):
            with self.subTest(foreground_command=bool(stdout)):
                category, _, state = self._end(stdout=stdout, returncode=0)
                self.assertEqual((category, state), ("no_artifact", "failed"))

    def test_a_valid_verdict_after_background_work_is_still_delivered(self):
        category, _, state = self._end(stdout=BACKGROUND_STARTED + APPROVED, returncode=0)
        self.assertEqual((category, state), (None, "completed"))
        self.assertIsNotNone(self.read_status()["review"])


DONE = ('HANDSOFF_PROGRESS: {"criterion": "REQ-001", "state": "done", '
        '"test": "true", "note": ""}\n')


class ImplementerBackgroundRuleTests(HandsoffTestCase):
    """#408: an Implementer's result is a criterion reported done; a clean
    exit without one while its background work is outstanding is
    background_abandoned, never a completed session."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.init("Implementer background rule #408")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(4, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _end(self, stdout):
        spec = runtime.LaunchSpec("implementer", "claude", "default", ("/bin/claude", "-p"), str(self.tmp),
                                  "bounded prompt", token_budget=40_000, project_root=str(self.tmp.resolve()))
        code, error, out = launch(self, spec, stdout=stdout, returncode=0)
        status = self.read_status()
        sid = lib.role_session_ids(status)["implementer"]
        failure = (status.get("agent_failures") or {}).get(sid) or {}
        return failure.get("category"), status["agent_sessions"][sid]["state"], error

    def test_outstanding_background_work_and_no_done_criterion_is_background_abandoned(self):
        for started in (BACKGROUND_STARTED, DETACHED):
            with self.subTest(detached=started is DETACHED):
                category, state, error = self._end(started)
                self.assertEqual((category, state), ("background_abandoned", "failed"))
                self.assertIsNotNone(error, "the launch returned success")
                self.assertIn(category, lib.RECOVERABLE_FAILURE_CATEGORIES)
                self.tearDown()
                self.setUp()

    def test_a_done_criterion_finished_work_or_none_completes(self):
        for name, stdout in (("done reported", BACKGROUND_STARTED + DONE),
                             ("work finished", BACKGROUND_STARTED + BACKGROUND_FINISHED),
                             ("no background work", FOREGROUND)):
            with self.subTest(name):
                category, state, error = self._end(stdout)
                self.assertEqual((category, state, error), (None, "completed", None))
                self.tearDown()
                self.setUp()


if __name__ == "__main__":
    import unittest
    unittest.main()
