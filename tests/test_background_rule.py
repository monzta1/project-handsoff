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
        killed = _tool_use("toolu_5", "KillShell", shell_id="bash_1")
        category, _, _ = self._end(stdout=BACKGROUND_STARTED + killed, returncode=0)
        self.assertEqual(category, "no_artifact")

    def test_no_background_work_and_no_result_keeps_no_artifact(self):
        for stdout in ("", FOREGROUND):
            with self.subTest(foreground_command=bool(stdout)):
                category, _, state = self._end(stdout=stdout, returncode=0)
                self.assertEqual((category, state), ("no_artifact", "failed"))

    def test_a_valid_verdict_after_background_work_is_still_delivered(self):
        category, _, state = self._end(stdout=BACKGROUND_STARTED + APPROVED, returncode=0)
        self.assertEqual((category, state), (None, "completed"))
        self.assertIsNotNone(self.read_status()["review"])


if __name__ == "__main__":
    import unittest
    unittest.main()
