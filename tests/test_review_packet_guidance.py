"""#409: reviewers and implementers are handed recorded verification to reuse."""
from __future__ import annotations

import io
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402

ROOT = BIN.parent
REUSE_RULE = "Do not re-run a reusable command unless a finding needs it"


class ReviewPacketGuidanceTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        # the drop-in check (build_launch_spec) needs the prompts in the copy
        if not (self.tmp / "prompts").exists():
            shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts", copy_function=shutil.copyfile)
        write_version_pin(self.tmp)
        self.outside = Path(tempfile.mkdtemp(prefix="handsoff-verified-"))
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.marker = self.outside / "fail"
        self.command = f"test ! -e {self.marker}"

    def _phase_5(self, env=None):
        self.init("Verified fixture")
        self.set_criterion_state("passing", resolved=True)
        toml = self.tmp / "handsoff.toml"
        text = re.sub(r'(?m)^commands = \["true"\]',
                      lambda _: "commands = " + json.dumps(["true", self.command]), toml.read_text())
        if env:
            text = text.replace("[checks]\n", "[checks]\nenv = { " + ", ".join(
                f"{k} = {json.dumps(v)}" for k, v in env.items()) + " }\n", 1)
        toml.write_text(text)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "Fixture REQ-002",
                     "--verification", "automated", "--test", self.command], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        verified = run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        return json.loads(verified.stdout)

    def _inputs(self):
        return {role: agent.build_role_input(self.tmp, role, "Review or implement the fixture")
                for role in ("reviewer", "implementer")}

    def _line(self, text, command):
        section = text.split(lib.VERIFIED_HEADING, 1)[1]
        return next(line for line in section.splitlines() if f"`{command}`" in line)

    def test_packet_and_task_list_reusable_evidence_with_real_run_ids(self):
        verified = self._phase_5()
        run_id = verified["criteria"]["REQ-002"]["run_id"]
        records = {r["run_id"] for r in lib.load_verifications(self.tmp, lib.load_config(self.tmp))[0]}
        self.assertIn(run_id, records)
        for role, text in self._inputs().items():
            with self.subTest(role=role):
                self.assertIn(lib.VERIFIED_HEADING, text)
                self.assertIn(lib.VERIFIED_GUIDANCE, text)
                line = self._line(text, self.command)
                self.assertTrue(line.startswith("- reusable:"), line)
                self.assertIn(f"run {run_id}", line)
        reviewer = self._inputs()["reviewer"]
        packet_at = reviewer.index(agent.IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING)
        self.assertLess(packet_at, reviewer.index(lib.VERIFIED_HEADING))
        self.assertLess(reviewer.index(lib.VERIFIED_HEADING), reviewer.index("# Assigned task"))

    def test_a_changed_tree_is_not_reusable_in_both(self):
        self._phase_5()
        (self.tmp / "product.py").write_text("x = 2\n")
        for role, text in self._inputs().items():
            with self.subTest(role=role):
                line = self._line(text, self.command)
                self.assertTrue(line.startswith("- not reusable:"), line)
                self.assertIn("stale tree", line)

    def test_a_changed_env_is_not_reusable_in_both(self):
        self._phase_5(env={"HANDSOFF_PROBE": "one"})
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('HANDSOFF_PROBE = "one"', 'HANDSOFF_PROBE = "two"'))
        for role, text in self._inputs().items():
            with self.subTest(role=role):
                line = self._line(text, self.command)
                self.assertTrue(line.startswith("- not reusable:"), line)
                self.assertIn("changed env", line)

    def test_a_pass_then_fail_history_is_not_reusable_in_both(self):
        self._phase_5()
        self.marker.write_text("now it fails")
        failed = run(["verify", "--criterion", "REQ-002", "--by", "test-implementer", "--no-cache"], cwd=self.tmp)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        failed_id = json.loads(failed.stdout)["criteria"]["REQ-002"]["run_id"]
        self.marker.unlink()
        for role, text in self._inputs().items():
            with self.subTest(role=role):
                line = self._line(text, self.command)
                self.assertTrue(line.startswith("- not reusable:"), line)
                self.assertIn(f"later failure ({failed_id})", line)

    def test_the_prompts_and_the_task_carry_the_guidance_and_the_test_budget(self):
        for name in ("reviewer.md", "implementer.md"):
            with self.subTest(prompt=name):
                prompt = (ROOT / "prompts" / name).read_text(encoding="utf-8")
                self.assertIn("# VERIFIED", prompt)
                self.assertIn(REUSE_RULE, prompt)
                self.assertIn("run focused tests instead", prompt)
        self.assertIn("run focused tests instead", lib.VERIFIED_GUIDANCE)
        self._phase_5()
        for text in self._inputs().values():
            self.assertIn("spend at most 20 minutes of wall clock running tests", text)
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("[agent_budget]\n", "[agent_budget]\ntest_wall_clock_minutes = 7\n", 1))
        self.assertEqual(lib.load_config(self.tmp)["test_wall_clock_minutes"], 7)
        for text in self._inputs().values():
            self.assertIn("spend at most 7 minutes of wall clock running tests", text)
        toml.write_text(toml.read_text().replace("test_wall_clock_minutes = 7", "test_wall_clock_minutes = 0"))
        with self.assertRaisesRegex(lib.HandsoffError, "test_wall_clock_minutes"):
            lib.load_config(self.tmp)


class _Running:
    def __init__(self):
        self.exited = False

    def poll(self):
        return 0 if self.exited else None


class HalfTimeoutWarningTests(HandsoffTestCase):
    def test_the_warning_fires_once_at_half_the_timeout(self):
        self.init("Warning fixture")
        stream = io.StringIO()
        process = _Running()
        warning = agent._HalfTimeoutWarning(self.tmp, "hs-" + "0" * 32, "reviewer", process, 0.4, stream=stream)
        warning.start()
        time.sleep(0.1)
        self.assertEqual(stream.getvalue(), "", "nothing before half the timeout")
        time.sleep(0.4)
        warning.warn()  # a second call never warns again
        warning.cancel()
        lines = [line for line in stream.getvalue().splitlines() if line.startswith("HANDSOFF_TIMEOUT_WARNING:")]
        self.assertEqual(len(lines), 1, stream.getvalue())
        self.assertIn("half of its 0.4-second timeout", lines[0])
        self.assertEqual(warning.fired, 1)

    def test_no_warning_for_a_session_that_already_ended(self):
        stream = io.StringIO()
        process = _Running()
        process.exited = True
        warning = agent._HalfTimeoutWarning(self.tmp, "hs-" + "1" * 32, "implementer", process, 0.1, stream=stream)
        warning.start()
        time.sleep(0.3)
        warning.cancel()
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(warning.fired, 0)
