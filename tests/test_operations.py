"""REQ-001 and REQ-002: bounded external operation telemetry."""
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import HandsoffTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_agent as runtime
import handsoff_lib as lib
from tests.fixture_state import write_version_pin


def record(**overrides):
    value = {"operation_id": "op-abcd", "dependency": "github_api", "operation": "fetch_issues",
             "state": "started", "attempt": 1, "timeout_seconds": 60}
    value.update(overrides)
    return value


class TestOperations(HandsoffTestCase):
    def test_durable_replace_fault_boundaries_never_leave_malformed_json(self):
        target = self.tmp / "durable.json"
        old = {"generation": 1}
        new = {"generation": 2}
        lib.atomic_write_json(target, old)
        for stage in ("before_flush", "after_flush", "after_replace", "during_directory_sync"):
            lib.atomic_write_json(target, old)
            def interrupt(observed, expected=stage):
                if observed == expected:
                    raise RuntimeError(expected)
            with self.assertRaisesRegex(RuntimeError, stage):
                lib.durable_replace(target, (json.dumps(new) + "\n").encode(), fault=interrupt)
            self.assertIn(json.loads(target.read_text()), (old, new))

    def test_durable_replace_retains_and_restores_one_valid_backup(self):
        target = self.tmp / "state.json"
        lib.atomic_write_json(target, {"generation": 1})
        lib.atomic_write_json(target, {"generation": 2})
        backup = lib.durable_backup_path(target)
        self.assertEqual(json.loads(backup.read_text()), {"generation": 1})
        target.write_text("not-json")
        restored = lib.restore_durable_backup(target)
        self.assertTrue(restored["restored"])
        self.assertEqual(json.loads(target.read_text()), {"generation": 1})

    def test_durability_capability_is_honest_and_bounded(self):
        capability = lib.durability_capability(self.tmp / "status.json")
        self.assertIn(capability["level"], {"full", "best_effort"})
        self.assertTrue(capability["file_fsync"])
        self.assertEqual(capability["directory_fsync"], capability["level"] == "full")

    def test_current_operation_prefers_live_record(self):
        now = datetime.now(timezone.utc)
        lib.record_operation(self.tmp, "sess-operations", "implementer", record(operation_id="op-done", state="succeeded"), now)
        lib.record_operation(self.tmp, "sess-operations", "implementer", record(operation_id="op-live"), now)
        self.assertEqual(lib.current_operation(self.tmp, "sess-operations")["operation_id"], "op-live")

    def test_current_operation_falls_back_to_newest_terminal(self):
        now = datetime.now(timezone.utc)
        lib.record_operation(self.tmp, "sess-operations", "implementer", record(operation_id="op-done", state="succeeded"), now)
        self.assertEqual(lib.current_operation(self.tmp, "sess-operations")["operation_id"], "op-done")

    def test_succeeded_operation_ids_preserve_order(self):
        now = datetime.now(timezone.utc)
        for suffix in ("a", "b", "c"):
            lib.record_operation(self.tmp, "sess-operations", "implementer", record(operation_id="op-" + suffix, state="succeeded"), now)
        self.assertEqual(lib.succeeded_operation_ids(self.tmp, "sess-operations"), ["op-a", "op-b", "op-c"])

    def test_external_timeout_is_recoverable(self):
        self.assertIn("external_timeout", lib.RECOVERABLE_FAILURE_CATEGORIES)

    def test_dispatch_failed_is_not_recoverable(self):
        self.assertIn("dispatch_failed", lib.FAILURE_CATEGORIES)
        self.assertNotIn("dispatch_failed", lib.RECOVERABLE_FAILURE_CATEGORIES)

    def test_operation_grace_default_is_120(self):
        self.assertEqual(lib.load_config(self.tmp)["recovery"]["operation_grace_seconds"], 120)

    def test_proposal_length_error_names_field_and_length(self):
        with self.assertRaisesRegex(lib.HandsoffError, r"approach item length 600"):
            lib.validate_design_proposal({"summary": "x", "approach": ["x" * 600], "tradeoffs": [], "decisions": ["x"], "constraints": [], "verification": ["x"]})

    def test_valid_record(self):
        self.assertEqual(lib.validate_operation_line(record()), record())

    def test_unknown_key_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(reason="secret")))

    def test_prose_dependency_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(dependency="fetch the issue list")))

    def test_overlong_identifier_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(operation="a" * 65)))

    def test_invalid_state_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(state="waiting")))

    def test_boolean_attempt_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(attempt=True)))

    def test_timeout_range_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(timeout_seconds=86401)))

    def test_invalid_category_rejected(self):
        self.assertIsNone(lib.validate_operation_line(record(category="made_up")))

    def test_repeated_operation_updates_in_place_and_ends(self):
        first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lib.record_operation(self.tmp, "s1", "implementer", record(), first)
        saved = lib.record_operation(self.tmp, "s1", "implementer", record(state="succeeded", attempt=2), first + timedelta(seconds=2))
        self.assertEqual(len(lib.read_operations(self.tmp)["sessions"]["s1"]["operations"]), 1)
        self.assertEqual(saved["ended_at"], (first + timedelta(seconds=2)).isoformat())

    def test_operation_and_session_bounds(self):
        now = datetime.now(timezone.utc)
        for n in range(70):
            lib.record_operation(self.tmp, "s1", "implementer", record(operation_id=f"op-{n:04x}"), now)
        for n in range(10):
            lib.record_operation(self.tmp, f"s{n}", "implementer", record(operation_id=f"op-x{n:03d}"), now)
        value = lib.read_operations(self.tmp)
        self.assertLessEqual(len(value["sessions"]), 8)
        self.assertLessEqual(len(value["sessions"]["s9"]["operations"]), 64)

    def test_warning_counter(self):
        lib.count_operation_warning(self.tmp, "s1", "implementer", "protocol_warnings")
        lib.count_operation_warning(self.tmp, "s1", "implementer", "late_telemetry")
        self.assertEqual(lib.read_operations(self.tmp)["sessions"]["s1"]["protocol_warnings"], 1)
        self.assertEqual(lib.read_operations(self.tmp)["sessions"]["s1"]["late_telemetry"], 1)

    def test_assessment_states_and_precedence(self):
        now = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
        base = {**record(), "started_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:01:59+00:00"}
        self.assertEqual(lib.operation_assessment(base, now, 10), "timed_out")
        for state in ("succeeded", "failed", "timed_out", "cancelled"):
            self.assertEqual(lib.operation_assessment({**base, "state": state}, now, 10), state)

    def test_dependency_class_table_unknown_and_missing(self):
        expected = {"auth_failure": "authentication", "rate_limit": "agent_provider",
                    "context_exhaustion": "agent_provider", "token_budget_exhaustion": "agent_provider",
                    "timeout": "network", "network": "network", "target_service": "target_service"}
        for category, family in expected.items():
            self.assertEqual(lib.dependency_class({"category": category}), family)
        self.assertEqual(lib.dependency_class({"category": "unknown"}), "engine")
        self.assertEqual(lib.dependency_class({}), "engine")

    def _operation_fixture(self):
        self.init("operation view")
        session = lib.create_agent_session(self.tmp, role="implementer", actor="codex-implementer",
                                           adapter="codex", requested_model="default",
                                           resolution_source="configured")
        return session, lib.load_unique_json(lib.status_path(self.tmp, lib.load_config(self.tmp)))

    def test_operation_view_waiting_elapsed_and_retry_count(self):
        session, status = self._operation_fixture()
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lib.record_operation(self.tmp, session["session_id"], "implementer", record(attempt=2), started)
        view = lib.operation_view(status, self.tmp, now=started + timedelta(seconds=7), quiet_seconds=60)
        self.assertEqual(view["assessment"], "waiting")
        self.assertEqual(view["elapsed_seconds"], 7)
        self.assertEqual(view["retry_count"], 1)

    def test_operation_view_quiet_wait_is_stale(self):
        session, status = self._operation_fixture()
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lib.record_operation(self.tmp, session["session_id"], "implementer", record(), started)
        self.assertEqual(lib.operation_view(status, self.tmp, now=started + timedelta(seconds=11), quiet_seconds=10)["assessment"], "stale")

    def test_operation_view_terminal_states(self):
        session, status = self._operation_fixture()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lib.record_operation(self.tmp, session["session_id"], "implementer", record(state="timed_out"), now)
        self.assertEqual(lib.operation_view(status, self.tmp, now=now)["assessment"], "timed_out")
        cancelled = lib.create_agent_session(self.tmp, role="reviewer", actor="codex-reviewer",
                                              adapter="codex", requested_model="default",
                                              resolution_source="configured")
        status = lib.load_unique_json(lib.status_path(self.tmp, lib.load_config(self.tmp)))
        lib.record_operation(self.tmp, cancelled["session_id"], "reviewer", record(state="cancelled"), now)
        self.assertEqual(lib.operation_view(status, self.tmp, now=now)["assessment"], "cancelled")

    def test_operation_view_retry_and_last_success(self):
        session, status = self._operation_fixture()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lib.record_operation(self.tmp, session["session_id"], "implementer",
                            record(operation_id="op-done1", state="succeeded"), now)
        lib.record_operation(self.tmp, session["session_id"], "implementer",
                            record(operation_id="op-open2", attempt=3), now + timedelta(seconds=2))
        view = lib.operation_view(status, self.tmp, now=now + timedelta(seconds=3))
        self.assertEqual(view["retry_count"], 2)
        self.assertEqual(view["last_success_at"], now.isoformat())

    def test_operation_view_without_telemetry_is_unavailable(self):
        _session, status = self._operation_fixture()
        view = lib.operation_view(status, self.tmp)
        self.assertEqual(view["availability"], "unavailable")
        self.assertEqual(view["current"], None)
        self.assertIn("no operation telemetry", view["assessment"])

    def test_operation_view_exposes_warning_counters(self):
        session, status = self._operation_fixture()
        lib.count_operation_warning(self.tmp, session["session_id"], "implementer", "late_telemetry")
        view = lib.operation_view(status, self.tmp)
        self.assertEqual(view["late_telemetry"], 1)


if __name__ == "__main__":
    unittest.main()


class _InputPipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _FakeProcess:
    """A completed child for runner tests; `pid` is what the beacon reports."""
    returncode = 0

    def __init__(self, stdout="", pid=4242):
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


class TestBoundedCancellationAndHygiene(HandsoffTestCase):
    """REQ-003 (#67) and REQ-007 (#84): the runner signals only a proven
    process group, resumes without repeating succeeded work, refuses a
    reviewer launch with no proposal, and names dispatch failures."""

    def setUp(self):
        super().setUp()
        import shutil
        shutil.copytree(Path(__file__).resolve().parent.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _host_architect(self):
        """Point the fixture's [agents].architect at host whatever the copied
        toml said (normalize_fixture_config may have rewritten it to auto)."""
        import re
        toml = self.tmp / "handsoff.toml"
        toml.write_text(re.sub(r'^architect = "[a-z-]+"$', 'architect = "host"',
                               toml.read_text(), count=1, flags=re.M))

    def _live_reviewer(self, pid=4242):
        self.init("Cancellation fixture")
        session = lib.create_agent_session(
            self.tmp, role="reviewer", actor="codex-reviewer", adapter="codex",
            requested_model="default", resolution_source="configured")
        lib.write_live_beacon(self.tmp, session_id=session["session_id"], role="reviewer",
                             state="running", pid=pid)
        return session["session_id"]

    def _operation(self, session_id, *, timeout_seconds, age_seconds, state="started"):
        started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        lib.record_operation(self.tmp, session_id, "reviewer",
                             record(timeout_seconds=timeout_seconds, state=state), started)

    def test_timed_out_operation_terminates_only_the_proven_group(self):
        session_id = self._live_reviewer(pid=4242)
        self._operation(session_id, timeout_seconds=10, age_seconds=400)
        cfg = lib.load_config(self.tmp)
        process = _FakeProcess(pid=4242)
        with mock.patch("os.getpgid", return_value=4242), mock.patch("os.killpg") as killpg:
            verdict = runtime._check_operation_timeout(
                self.tmp, cfg, session_id, process, datetime.now(timezone.utc))
        self.assertEqual(verdict, "terminated")
        import signal
        self.assertEqual(killpg.call_args_list[0].args, (4242, signal.SIGTERM))
        self.assertTrue(all(call.args[0] == 4242 for call in killpg.call_args_list))

    def test_waiting_operation_is_left_alone(self):
        session_id = self._live_reviewer()
        self._operation(session_id, timeout_seconds=1000, age_seconds=5)
        cfg = lib.load_config(self.tmp)
        with mock.patch("os.getpgid", return_value=4242), mock.patch("os.killpg") as killpg:
            verdict = runtime._check_operation_timeout(
                self.tmp, cfg, session_id, _FakeProcess(), datetime.now(timezone.utc))
        self.assertEqual(verdict, "unverified")
        killpg.assert_not_called()

    def test_pid_that_is_not_group_leader_is_never_signalled(self):
        session_id = self._live_reviewer()
        self._operation(session_id, timeout_seconds=10, age_seconds=400)
        cfg = lib.load_config(self.tmp)
        with mock.patch("os.getpgid", return_value=4243), mock.patch("os.killpg") as killpg:
            verdict = runtime._check_operation_timeout(
                self.tmp, cfg, session_id, _FakeProcess(), datetime.now(timezone.utc))
        self.assertEqual(verdict, "unverified")
        killpg.assert_not_called()

    def test_resume_packet_lists_succeeded_operations_only_when_present(self):
        session_id = self._live_reviewer()
        self.assertEqual(runtime._completed_operations_section(self.tmp, session_id), "")
        now = datetime.now(timezone.utc)
        lib.record_operation(self.tmp, session_id, "reviewer",
                             record(operation_id="op-done1", state="succeeded"), now)
        lib.record_operation(self.tmp, session_id, "reviewer",
                             record(operation_id="op-open2", state="started"), now)
        section = runtime._completed_operations_section(self.tmp, session_id)
        self.assertIn("# Completed external operations", section)
        self.assertIn("- op-done1", section)
        self.assertNotIn("op-open2", section)
        self.assertIn("do not repeat them", section)

    def test_reviewer_launch_refused_without_a_proposal(self):
        self._host_architect()
        self.init("Launch refusal fixture")
        from tests.test_handsoff_supervisor import run
        self.assertEqual(run(["advance", "2", "10"], cwd=self.tmp).returncode, 0)
        which = lambda name: "/usr/local/bin/codex" if name == "codex" else None
        with self.assertRaisesRegex(lib.HandsoffError, "no design proposal is recorded"):
            runtime.build_launch_spec(self.tmp, "reviewer", "critique", which=which)
        lib.record_design_proposal(self.tmp, None, {
            "summary": "s", "approach": ["a"], "tradeoffs": [], "decisions": ["d"],
            "constraints": [], "verification": ["v"]}, architect_actor="host-architect")
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "critique", which=which)
        self.assertEqual(spec.role, "reviewer")

    def test_dispatch_failure_is_named_not_non_zero_exit(self):
        self.init("Dispatch failure fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = ('HANDSOFF_REVIEW_RESULT: {"kind":"implementation","decision":"approved",'
                    '"summary":"ok","findings":[],"structural_blocker":false,'
                    '"symptom_reproduced":"yes","tests_executed":"yes"}\n')
        spec = runtime.LaunchSpec(
            "reviewer", "codex", "default", ("/bin/codex", "exec", "-"),
            str(self.tmp), "bounded reviewer prompt", token_budget=40_000,
            project_root=str(self.tmp.resolve()),
        )
        import handsoff_broker
        with mock.patch.object(handsoff_broker, "dispatch_reviewer_result",
                               side_effect=lib.HandsoffError("no managed Architect identity")):
            with self.assertRaisesRegex(runtime.AgentLaunchError, "no managed Architect identity"):
                runtime.execute_launch(spec, popen_factory=mock.Mock(return_value=_FakeProcess(approved)),
                                       beacon_interval=0.01)
        status = self.read_status()
        session = status["agent_sessions"][status["current_agent_sessions"]["reviewer"]]
        failure = status["agent_failures"][session["session_id"]]
        self.assertEqual(failure["category"], "dispatch_failed")
        self.assertIn("no managed Architect identity", failure["reason"])

    def test_design_propose_cli_names_field_and_length(self):
        self._host_architect()
        self.init("Length message fixture")
        proposal = self.tmp / "proposal.json"
        proposal.write_text(json.dumps({"summary": "s", "approach": ["x" * 600], "tradeoffs": [],
                                        "decisions": ["d"], "constraints": [], "verification": ["v"]}))
        from tests.test_handsoff_supervisor import run
        result = run(["design-propose", "--file", str(proposal), "--by", "host-architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", result.stdout)
        self.assertIn("approach", result.stdout)
        self.assertIn("600", result.stdout)


class TestPromptsAndLateTelemetry(HandsoffTestCase):
    """REQ-006: every role prompt carries a parseable example; REQ-004: a
    line from a session that is no longer current is dropped and counted."""

    def setUp(self):
        super().setUp()
        import shutil
        shutil.copytree(Path(__file__).resolve().parent.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def test_every_role_prompt_documents_a_parseable_example(self):
        import re
        for name in ("implementer", "reviewer", "architect", "supervisor"):
            text = (self.tmp / "prompts" / f"{name}.md").read_text()
            self.assertIn("HANDSOFF_OPERATION:", text, name)
            self.assertIn("^[a-z][a-z0-9_.-]{0,63}$", text, name)
            examples = [m for m in re.findall(r"HANDSOFF_OPERATION: (\{[^`\n]*\})", text) if "op-gh01" in m]
            self.assertTrue(examples, name)
            self.assertIsNotNone(lib.validate_operation_line(json.loads(examples[0])), name)

    def test_late_telemetry_from_a_superseded_session_is_dropped(self):
        self.init("Late telemetry fixture")
        older = lib.create_agent_session(
            self.tmp, role="reviewer", actor="reviewer-old", adapter="codex",
            requested_model="default", resolution_source="configured")
        lib.transition_agent_session(self.tmp, older["session_id"], "running")
        lib.transition_agent_session(self.tmp, older["session_id"], "completed", exit_code=0)
        newer = lib.create_agent_session(
            self.tmp, role="reviewer", actor="reviewer-new", adapter="codex",
            requested_model="default", resolution_source="configured")
        runtime._parse_operation_line("HANDSOFF_OPERATION: " + json.dumps(record()),
                                       self.tmp, older["session_id"], "reviewer")
        data = lib.read_operations(self.tmp)
        self.assertNotIn(older["session_id"], {sid for sid, s in data["sessions"].items() if s.get("operations")})
        self.assertEqual(data["sessions"].get(older["session_id"], {}).get("late_telemetry", 0), 1)
        self.assertNotIn(newer["session_id"], data["sessions"])


class TestAutoReconfirmation(HandsoffTestCase):
    """#102: --auto re-applies what a person already decided for an
    identical acceptance hash, and authorizes exactly one engine retry."""

    def _write_status(self, status):
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.atomic_write_json(lib.status_path(self.tmp, cfg), status)
            lib.append_event(self.tmp, cfg, "test_setup", "test harness adjusted status directly")

    def _approved_at_seven(self):
        from tests.test_handsoff_supervisor import run
        self.init("Auto reconfirmation")
        write_version_pin(self.tmp)
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        gate = run(["deployment-gate", "--approve", "--by", "pilot"], cwd=self.tmp)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        return run

    def test_auto_reapplies_a_prior_approval_after_drift_and_re_review(self):
        run = self._approved_at_seven()
        (self.tmp / "product.txt").write_text("changed after approval\n")
        verified = run(["verify", "--criterion", "REQ-001", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(self.read_status()["phase_number"], 5)
        self.assertIsNone(self.read_status()["deployment_approved"])
        review = run(["record-review", "--by", "reviewer-2", "--symptom-reproduced", "yes", "--tests-executed", "yes"], cwd=self.tmp)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        self.assertEqual(run(["advance", "6"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "7"], cwd=self.tmp).returncode, 0)
        auto = run(["deployment-gate", "--approve", "--auto", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
        self.assertIn("DEPLOYMENT_APPROVAL_AUTO_CONFIRMED", auto.stdout)
        status = self.read_status()
        self.assertEqual(status["deployment_approved"]["auto_confirmed_from"]["by"], "pilot")
        kinds = [json.loads(line)["kind"] for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertIn("deployment_approval_auto_confirmed", kinds)

    def test_auto_refuses_a_changed_acceptance_hash(self):
        run = self._approved_at_seven()
        changed = run(["criterion-update", "REQ-001", "--requirement", "A different requirement", "--revoke-approval"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-2")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        auto = run(["deployment-gate", "--approve", "--auto", "--by", "supervisor"], cwd=self.tmp)
        self.assertEqual(auto.returncode, 1, auto.stdout)
        self.assertIn("no prior approval for this design hash", auto.stdout)
        self.assertIsNone(self.read_status()["deployment_approved"])

    def test_auto_retry_is_authorized_exactly_once(self):
        from tests.test_handsoff_supervisor import run
        self.init("Auto retry")
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        status = self.read_status()
        sid = "hs-" + "4" * 32
        now = datetime.now(timezone.utc).isoformat()
        status["phase_number"] = 4; status["phase"] = lib.PHASES[4]
        status["agent_sessions"] = {sid: {"session_id": sid, "role": "implementer", "actor": "codex-implementer",
            "adapter": "codex", "requested_model": "default", "reported_model": None, "resolution_source": "configured",
            "started_at": now, "running_at": now, "ended_at": now, "state": "failed", "exit_code": 1, "phase_number": 4}}
        status["current_agent_sessions"] = {"implementer": sid}
        status["agent_failures"] = {sid: {"session_id": sid, "category": "token_budget_exhaustion",
            "reason": "managed role exhausted its token budget", "at": now,
            "tail_sha256": "0" * 64}}
        self._write_status(status)
        cfg = lib.load_config(self.tmp)
        before = lib.recovery_assessment(self.read_status(), cfg, {}, [], root=self.tmp)
        self.assertEqual(before["reason"], "non_recoverable_failure")
        first = run(["recover", "--auto", "--by", "watchdog", "--dry-run"], cwd=self.tmp)
        # --dry-run only reports; the authorization path runs without it.
        first = run(["recover", "--auto", "--by", "watchdog", "--timeout", "1"], cwd=self.tmp)
        self.assertIn("RECOVERY_AUTO_CONFIRMED", first.stdout, first.stdout + first.stderr)
        after = lib.recovery_assessment(self.read_status(), cfg, {}, [], root=self.tmp)
        self.assertNotEqual(after["reason"], "non_recoverable_failure")
        # The marked failure record must still pass the status schema, or
        # every later command on the run is refused (field proof finding).
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])
        second = run(["recover", "--auto", "--by", "watchdog", "--timeout", "1"], cwd=self.tmp)
        self.assertEqual(second.returncode, 1, second.stdout)
        self.assertIn("automatic retry", second.stdout)
