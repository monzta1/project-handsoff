#!/usr/bin/env python3
"""Focused contracts for evidence refresh, durable monitors, and run SLOs."""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_runtime_control as control  # noqa: E402


def digest(label: str) -> str:
    return control.content_hash({"label": label})


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def dependency_hashes() -> dict[str, str]:
    return {name: digest(name) for name in control.DEPENDENCY_CLASSES}


def subject(
    subject_id: str,
    kind: str,
    *,
    product: str,
    refreshable: list[str] | None = None,
    generated: list[dict] | None = None,
) -> dict:
    value = {
        "subject_id": subject_id,
        "subject_kind": kind,
        "input_hash": digest(f"input:{subject_id}"),
        "dependency_hashes": dependency_hashes(),
        "path_dependencies": {
            "product_source": [product],
            "test_source": [f"tests/test_{subject_id.lower()}.py"],
            "criteria_specs": ["handsoff-acceptance.json"],
            "governance_policy": ["rules/model-policy.json"],
            "deployment_target": ["deploy/target.json"],
            "runtime_bytes": ["bin/runtime.py"],
        },
        "refreshable_paths": refreshable or [],
        "generated_outputs": generated or [],
    }
    if kind == "decision":
        value.update(actor="reviewer-1", reason="all gates passed", verdict_hash=digest("approved"))
    return value


def dependency_map() -> dict:
    generated = {
        "path": "dist/release-manifest.json",
        "producer": "release-manifest",
        "dependency_hash": digest("runtime-inputs"),
        "output_hash": digest("old-manifest"),
        "deterministic": True,
        "allow_auto_refresh": True,
    }
    return {
        "schema": "handsoff.evidence_dependencies",
        "version": 1,
        "framework_state_paths": [".handsoff-version", "dist/checksums.txt"],
        "unrelated_paths": ["notes/local.txt"],
        "subjects": [
            subject(
                "REQ-278",
                "evidence",
                product="src/release.py",
                refreshable=[".handsoff-version"],
                generated=[generated],
            ),
            subject("review-278", "decision", product="src/review.py", refreshable=["dist/checksums.txt"]),
            subject("REQ-other", "evidence", product="src/other.py"),
        ],
    }


def monitor_row(run_id: str, root: str, state: str, cursor: int, at: datetime = NOW) -> dict:
    return {
        "run_id": run_id,
        "root": root,
        "state": state,
        "cursor": cursor,
        "updated_at": at.isoformat(),
    }


def snapshot(**updates) -> dict:
    value = {
        "schema": "handsoff.run_snapshot",
        "version": 1,
        "run_id": "run-279",
        "state": "in_progress",
        "percent": 40,
        "verified_complete": False,
        "gate": "none",
        "worker_state": "healthy",
        "failure_category": None,
        "recovery_attempts": 0,
        "escalation_recorded": False,
        "result_adoptable": False,
    }
    value.update(updates)
    return value


POLICY = {
    "same_task_retry_cap": 2,
    "equivalent_quota_fallback": True,
    "safe_result_adoption": True,
}


def in_flight(operation_id: str, *, remote: bool, cancellable: bool, bounded: bool = True) -> dict:
    return {
        "operation_id": operation_id,
        "kind": "external_call" if remote else "test",
        "location": "remote" if remote else "local",
        "cancellable": cancellable,
        "bounded": bounded,
        "state": "running",
    }


class EvidenceDependencyTests(unittest.TestCase):
    def test_v1_schema_is_closed_bounded_and_complete(self):
        mapping = dependency_map()
        control.validate_dependency_map(mapping)
        broken = copy.deepcopy(mapping)
        broken["surprise"] = True
        with self.assertRaisesRegex(control.SchemaError, "unknown fields"):
            control.validate_dependency_map(broken)
        broken = copy.deepcopy(mapping)
        del broken["subjects"][0]["dependency_hashes"]["runtime_bytes"]
        with self.assertRaisesRegex(control.SchemaError, "missing fields"):
            control.validate_dependency_map(broken)
        broken = copy.deepcopy(mapping)
        broken["subjects"] = broken["subjects"] * 100
        with self.assertRaisesRegex(control.SchemaError, "at most"):
            control.validate_dependency_map(broken)

    def test_allowlisted_generated_output_can_refresh_only_its_subject(self):
        mapping = dependency_map()
        observations = {
            "dist/release-manifest.json": {
                "output_hash": digest("new-manifest"),
                "regenerated_hash": digest("new-manifest"),
                "dependency_hash": digest("runtime-inputs"),
            }
        }
        result = control.assess_dependency_drift(
            mapping,
            "REQ-278",
            [{"path": "dist/release-manifest.json"}],
            dependency_hashes(),
            current_input_hash=digest("input:REQ-278"),
            regenerated_outputs=observations,
        )
        self.assertEqual(result["action"], "auto_refresh")
        unaffected = control.assess_dependency_drift(
            mapping,
            "REQ-other",
            [{"path": ".handsoff-version"}],
            dependency_hashes(),
            current_input_hash=digest("input:REQ-other"),
        )
        self.assertEqual(unaffected["action"], "current")

    def test_generated_tampering_and_changed_runtime_dependencies_invalidate(self):
        mapping = dependency_map()
        tampered = control.assess_dependency_drift(
            mapping,
            "REQ-278",
            [{"path": "dist/release-manifest.json"}],
            dependency_hashes(),
            current_input_hash=digest("input:REQ-278"),
            regenerated_outputs={
                "dist/release-manifest.json": {
                    "output_hash": digest("tampered"),
                    "regenerated_hash": digest("expected"),
                    "dependency_hash": digest("runtime-inputs"),
                }
            },
        )
        self.assertEqual(tampered["action"], "invalidate")
        changed = dependency_hashes()
        changed["runtime_bytes"] = digest("changed-runtime")
        runtime = control.assess_dependency_drift(
            mapping,
            "REQ-278",
            [{"path": "dist/release-manifest.json"}],
            changed,
            current_input_hash=digest("input:REQ-278"),
            regenerated_outputs={
                "dist/release-manifest.json": {
                    "output_hash": digest("new"),
                    "regenerated_hash": digest("new"),
                    "dependency_hash": digest("changed-runtime-inputs"),
                }
            },
        )
        self.assertEqual(runtime["action"], "invalidate")
        self.assertIn("runtime_bytes", runtime["dependency_mismatches"])

    def test_mixed_product_drift_unknown_paths_and_renames_are_never_refreshed(self):
        mapping = dependency_map()
        for changes in (
            [{"path": "src/release.py"}, {"path": ".handsoff-version"}],
            [{"path": "mystery.dat"}],
            [{"old_path": "dist/release-manifest.json", "new_path": "dist/manifest-v2.json"}],
        ):
            with self.subTest(changes=changes):
                result = control.assess_dependency_drift(
                    mapping,
                    "REQ-278",
                    changes,
                    dependency_hashes(),
                    current_input_hash=digest("input:REQ-278"),
                )
                self.assertEqual(result["action"], "invalidate")

    def test_decision_reaffirm_requires_identical_complete_inputs_and_preserves_provenance(self):
        mapping = dependency_map()
        result = control.assess_dependency_drift(
            mapping,
            "review-278",
            [{"path": "dist/checksums.txt"}],
            dependency_hashes(),
            current_input_hash=digest("input:review-278"),
        )
        self.assertEqual(result["action"], "auto_reaffirm")
        self.assertEqual((result["original_actor"], result["original_reason"]), ("reviewer-1", "all gates passed"))
        event = control.dependency_audit_event(result, at=NOW)
        self.assertEqual(event["kind"], "evidence_dependency_auto_reaffirm")
        changed = control.assess_dependency_drift(
            mapping,
            "review-278",
            [{"path": "dist/checksums.txt"}],
            dependency_hashes(),
            current_input_hash=digest("different-inputs"),
        )
        self.assertEqual(changed["action"], "invalidate")
        self.assertIn("complete decision input hash changed", changed["reasons"])


class DurableMonitorTests(unittest.TestCase):
    def test_owner_epoch_cursor_cas_fences_old_and_concurrent_owners(self):
        first = control.new_monitor("run-279", "host-a", now=NOW, lease_seconds=30)
        with self.assertRaises(control.LeaseHeld):
            control.claim_monitor(
                first,
                "host-b",
                now=NOW + timedelta(seconds=10),
                lease_seconds=30,
                expected_epoch=1,
                expected_cursor=0,
            )
        renewed = control.claim_monitor(
            first,
            "host-a",
            now=NOW + timedelta(seconds=10),
            lease_seconds=30,
            expected_epoch=1,
            expected_cursor=0,
        )
        self.assertEqual(renewed["lease_epoch"], 2)
        with self.assertRaises(control.CASConflict):
            control.advance_monitor_cursor(
                renewed,
                owner_instance="host-a",
                lease_epoch=1,
                expected_cursor=0,
                new_cursor=1,
                now=NOW + timedelta(seconds=11),
            )
        advanced = control.advance_monitor_cursor(
            renewed,
            owner_instance="host-a",
            lease_epoch=2,
            expected_cursor=0,
            new_cursor=4,
            now=NOW + timedelta(seconds=11),
        )
        self.assertEqual(advanced["cursor"], 4)
        with self.assertRaises(control.CASConflict):
            control.advance_monitor_cursor(
                advanced,
                owner_instance="host-a",
                lease_epoch=2,
                expected_cursor=0,
                new_cursor=5,
                now=NOW + timedelta(seconds=12),
            )
        takeover = control.claim_monitor(
            advanced,
            "host-b",
            now=NOW + timedelta(seconds=41),
            lease_seconds=30,
            expected_epoch=2,
            expected_cursor=4,
        )
        self.assertEqual((takeover["owner_instance"], takeover["lease_epoch"]), ("host-b", 3))

    def test_restart_rediscovery_unions_sources_and_terminal_ledger_truth_wins(self):
        monitor = control.new_monitor("run-active", "old-host", now=NOW, lease_seconds=30)
        inputs = control.make_rediscovery_inputs(
            [
                monitor_row("run-active", "/repo/a", "active", 3),
                monitor_row("run-done", "/repo/b", "active", 2),
            ],
            [
                monitor_row("run-active", "/repo/a", "in_progress", 7),
                monitor_row("run-ledger", "/repo/c", "paused", 8),
                monitor_row("run-done", "/repo/b", "completed", 9, NOW + timedelta(seconds=1)),
            ],
            [monitor],
        )
        discovered = control.rediscover_active_runs(inputs)
        self.assertEqual([item["run_id"] for item in discovered], ["run-active", "run-ledger"])
        self.assertEqual(discovered[0]["cursor"], 7)
        self.assertEqual(discovered[0]["sources"], ["fleet", "ledger", "monitor"])

    def test_conflicting_roots_block_restart_instead_of_guessing(self):
        inputs = control.make_rediscovery_inputs(
            [monitor_row("run-1", "/repo/a", "active", 1)],
            [monitor_row("run-1", "/repo/b", "in_progress", 2)],
            [],
        )
        self.assertEqual(control.rediscover_active_runs(inputs)[0]["disposition"], "blocked_conflict")

    def test_polling_is_model_free_and_preserves_every_gate(self):
        for gate in sorted(control.BLOCKING_GATES):
            with self.subTest(gate=gate):
                decision = control.decide_monitor_action(
                    snapshot(gate=gate, worker_state="stalled", recovery_attempts=0), POLICY
                )
                self.assertEqual(decision["action"], "wait_at_gate")
                self.assertFalse(decision["mutating"])
                self.assertEqual(decision["model_calls"], 0)
        quiet = control.decide_monitor_action(snapshot(worker_state="quiet"), POLICY)
        self.assertEqual(quiet["action"], "poll")
        complete = control.decide_monitor_action(
            snapshot(state="completed", percent=100, verified_complete=True), POLICY
        )
        self.assertEqual(complete["action"], "complete_monitor")

    def test_only_bounded_recovery_equivalent_fallback_and_one_escalation_are_automatic(self):
        retry = control.decide_monitor_action(snapshot(worker_state="stalled", recovery_attempts=1), POLICY)
        self.assertEqual(retry["action"], "retry_same_task")
        quota = control.decide_monitor_action(snapshot(worker_state="quota_exhausted"), POLICY)
        self.assertEqual(quota["action"], "fallback_equivalent_quota")
        exhausted = control.decide_monitor_action(
            snapshot(worker_state="failed", recovery_attempts=2), POLICY
        )
        self.assertEqual(exhausted["action"], "record_escalation")
        already = control.decide_monitor_action(
            snapshot(worker_state="failed", recovery_attempts=2, escalation_recorded=True), POLICY
        )
        self.assertEqual(already["action"], "report_blocked")


class PerformanceEpisodeTests(unittest.TestCase):
    def test_active_wall_time_counts_concurrency_once_and_unions_sleep_with_holds(self):
        history = control.new_performance_history("run-280", "episode-1", now=NOW)
        episode = history["episodes"][0]
        episode["holds"].append(
            {
                "hold_id": "pilot-1",
                "kind": "pilot",
                "started_at": (NOW + timedelta(minutes=20)).isoformat(),
                "ended_at": (NOW + timedelta(minutes=40)).isoformat(),
                "evidence_hash": digest("pilot hold"),
            }
        )
        episode["sleeps"].append(
            {
                "started_at": (NOW + timedelta(minutes=30)).isoformat(),
                "ended_at": (NOW + timedelta(minutes=50)).isoformat(),
                "measured_seconds": 20 * 60,
            }
        )
        # One 100-minute wall interval, not the sum of concurrent tasks; the
        # overlapping exclusions form one 30-minute interval.
        self.assertEqual(
            control.episode_active_seconds(episode, now=NOW + timedelta(minutes=100)),
            70 * 60,
        )

    def test_exact_90_warning_and_exact_120_atomic_pause(self):
        history = control.new_performance_history("run-280", "episode-1", now=NOW)
        before, decision = control.transition_performance(
            history, now=NOW + timedelta(minutes=90) - timedelta(microseconds=1)
        )
        self.assertEqual(decision["action"], "none")
        warned, decision = control.transition_performance(history, now=NOW + timedelta(minutes=90))
        self.assertEqual(decision["action"], "deadline_warning")
        self.assertEqual(decision["active_seconds"], 90 * 60)
        paused, decision = control.transition_performance(
            warned,
            now=NOW + timedelta(minutes=120),
            in_flight=[
                in_flight("local-test", remote=False, cancellable=True),
                in_flight("remote-ci", remote=True, cancellable=False, bounded=False),
            ],
        )
        self.assertEqual(decision["action"], "pause_for_performance_review")
        self.assertEqual(paused["episodes"][0]["state"], "paused_for_performance_review")
        self.assertEqual(paused["breaches"], 1)
        self.assertEqual(
            [item["action"] for item in decision["in_flight_dispositions"]],
            ["terminate", "detach_read_only_reconcile"],
        )
        self.assertTrue(all(item["late_result"] == "quarantine" for item in decision["in_flight_dispositions"]))

    def test_pause_blocks_mutation_quarantines_late_results_and_requires_new_episode(self):
        history = control.new_performance_history("run-280", "episode-1", now=NOW)
        paused, _ = control.transition_performance(history, now=NOW + timedelta(minutes=120))
        self.assertFalse(control.performance_operation_allowed(paused, "launch_agent"))
        self.assertTrue(control.performance_operation_allowed(paused, "reconcile"))
        with self.assertRaises(control.MutationRefused):
            control.require_performance_operation(paused, "release")
        self.assertEqual(
            control.late_result_disposition(paused, "episode-1", "late-agent"),
            "quarantine",
        )
        decision = {
            "decision_id": "resume-1",
            "action": "resume",
            "actor": "pilot",
            "reason": "reevaluation packet approved",
            "evidence_hash": digest("reevaluation"),
            "at": (NOW + timedelta(minutes=125)).isoformat(),
        }
        resumed = control.resume_performance(paused, decision, "episode-2")
        self.assertEqual(resumed["episodes"][-1]["state"], "active")
        self.assertEqual(resumed["breaches"], 1)
        # #383: the resumed episode leaves the pause, so its late results are adoptable.
        self.assertEqual(resumed["episodes"][0]["state"], "completed")
        self.assertEqual(resumed["episodes"][0]["paused_at"], paused["episodes"][0]["paused_at"])
        self.assertEqual(
            control.late_result_disposition(resumed, "episode-1", "late-agent"),
            "eligible_for_normal_validation",
        )
        with self.assertRaises(control.SchemaError):
            control.resume_performance(paused, {**decision, "action": "acknowledge"}, "episode-2")

    def test_restart_reconstructs_active_time_from_persisted_events(self):
        events = [
            {"kind": "episode_started", "at": NOW.isoformat(), "episode_id": "episode-1", "sequence": 1},
            {
                "kind": "hold_started",
                "at": (NOW + timedelta(minutes=10)).isoformat(),
                "episode_id": "episode-1",
                "hold_id": "hold-1",
                "hold_kind": "external_blocker",
                "evidence_hash": digest("outage"),
            },
            {
                "kind": "hold_ended",
                "at": (NOW + timedelta(minutes=20)).isoformat(),
                "episode_id": "episode-1",
                "hold_id": "hold-1",
            },
            {
                "kind": "sleep_measured",
                "at": (NOW + timedelta(minutes=35)).isoformat(),
                "episode_id": "episode-1",
                "started_at": (NOW + timedelta(minutes=30)).isoformat(),
                "measured_seconds": 5 * 60,
            },
        ]
        rebuilt = control.reconstruct_performance_history(
            "run-280", events, now=NOW + timedelta(minutes=100)
        )
        self.assertEqual(rebuilt["cumulative_active_seconds"], 85 * 60)
        warned, decision = control.transition_performance(
            rebuilt, now=NOW + timedelta(minutes=105)
        )
        self.assertEqual(decision["action"], "deadline_warning")
        self.assertEqual(control.total_active_seconds(warned, now=NOW + timedelta(minutes=105)), 90 * 60)

    def test_the_timeline_replays_to_the_same_clock(self):
        """#385: performance_timeline emits exactly the kinds the rebuild reads."""
        history = control.new_performance_history("run-385", "episode-1", now=NOW)
        episode = history["episodes"][0]
        episode["holds"].append({"hold_id": "hold-1", "kind": "pilot",
                                 "started_at": (NOW + timedelta(minutes=10)).isoformat(),
                                 "ended_at": (NOW + timedelta(minutes=20)).isoformat(),
                                 "evidence_hash": digest("pilot")})
        episode["sleeps"].append({"started_at": (NOW + timedelta(minutes=30)).isoformat(),
                                  "ended_at": (NOW + timedelta(minutes=35)).isoformat(),
                                  "measured_seconds": 300.0})
        paused, _ = control.transition_performance(history, now=NOW + timedelta(minutes=140))
        decision = {"decision_id": "resume-1", "action": "resume", "actor": "pilot",
                    "reason": "reevaluated", "evidence_hash": digest("resume"),
                    "at": (NOW + timedelta(minutes=150)).isoformat()}
        resumed = control.resume_performance(paused, decision, "episode-2")
        resumed["episodes"][1]["holds"].append({"hold_id": "hold-2", "kind": "pilot",
                                                "started_at": (NOW + timedelta(minutes=160)).isoformat(),
                                                "ended_at": None, "evidence_hash": digest("open")})
        timeline = control.performance_timeline(resumed)
        self.assertEqual([event["kind"] for event in timeline], [
            "episode_started", "hold_started", "hold_ended", "sleep_started", "sleep_ended",
            "performance_paused", "resumed", "hold_started",
        ])
        keys = [control.performance_timeline_key(event) for event in timeline]
        self.assertEqual(len(keys), len(set(keys)))
        later = NOW + timedelta(minutes=170)
        rebuilt = control.reconstruct_performance_history("run-385", timeline, now=later)
        self.assertEqual(control.total_active_seconds(rebuilt, now=later),
                         control.total_active_seconds(resumed, now=later))
        self.assertEqual([item["state"] for item in rebuilt["episodes"]], ["completed", "active"])
        self.assertEqual(rebuilt["resume_decisions"], [decision])
        self.assertEqual(rebuilt["breaches"], 1)

    def test_a_resumed_event_cannot_open_the_timeline(self):
        with self.assertRaisesRegex(control.SchemaError, "begin with episode_started"):
            control.reconstruct_performance_history(
                "run-385", [{"kind": "resumed", "at": NOW.isoformat(), "episode_id": "episode-2"}], now=NOW)


class SupervisorPauseRuleTests(unittest.TestCase):
    """#383/REQ-003: every supervisor command decides a pause through
    require_performance_operation, under the operation it performs."""

    EXPECTED = {
        "status": "inspect", "validate": "inspect", "verify-log": "inspect", "doctor": "inspect",
        "dashboard": "inspect", "design-timing": "inspect", "evidence-refresh-plan": "inspect",
        "implementation-review-packet": "inspect",
        "performance-status": "inspect", "performance-watch": "inspect",
        "monitor-poll": "reconcile", "performance-resume": "explicit_resume",
        "regression-cancel": "cancel", "run-close": "safe_close",
        # #414: ledger bookkeeping and the cache-only verify
        **{command: "bookkeeping" for command in (
            "advance", "pilot-note", "work-item-update", "record-evidence", "record-symptom-resolved",
            "ci-watch", "performance-auto-resume")},
        "verify": "verify_cached",
    }

    @classmethod
    def setUpClass(cls):
        import handsoff_supervisor
        cls.supervisor = handsoff_supervisor
        parser = handsoff_supervisor.build_parser()
        action = next(item for item in parser._actions if item.dest == "command")
        cls.commands = sorted(action.choices)
        history = control.new_performance_history("run-383", "episode-1", now=NOW)
        cls.paused, _ = control.transition_performance(history, now=NOW + timedelta(minutes=120))
        cls.healthy = history

    def test_every_supervisor_command_maps_to_one_operation(self):
        self.assertGreater(len(self.commands), 50)
        for command in self.commands:
            operation = self.supervisor.performance_operation(command)
            with self.subTest(command=command):
                if command in self.EXPECTED:
                    self.assertEqual(operation, self.EXPECTED[command])
                else:
                    # mutating: judged under its own name, which no pause allows
                    self.assertEqual(operation, command)
                    self.assertNotIn(operation, control.PERFORMANCE_PAUSE_ALLOWED_OPERATIONS)

    def test_the_map_names_only_real_commands(self):
        self.assertLessEqual(set(self.supervisor.PERFORMANCE_COMMAND_OPERATIONS), set(self.commands))
        self.assertEqual(self.supervisor.PERFORMANCE_COMMAND_OPERATIONS, self.EXPECTED)

    def test_while_paused_only_the_allowed_operations_pass(self):
        for command in self.commands:
            operation = self.supervisor.performance_operation(command)
            with self.subTest(command=command):
                if command in self.EXPECTED:
                    control.require_performance_operation(self.paused, operation)
                else:
                    with self.assertRaises(control.MutationRefused):
                        control.require_performance_operation(self.paused, operation)
                control.require_performance_operation(self.healthy, operation)

    def test_dashboard_and_launcher_operations_are_mutating(self):
        for operation in ("launch_agent", "launch_reviewer", "launch_implementer"):
            with self.assertRaises(control.MutationRefused):
                control.require_performance_operation(self.paused, self.supervisor.performance_operation(operation))

    def test_the_duplicate_command_set_is_gone(self):
        self.assertFalse(hasattr(self.supervisor, "PERFORMANCE_PAUSE_COMMANDS"))

    def test_the_refusal_decides_through_require_performance_operation(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = {"block_new_work": True}
            with mock.patch.object(self.supervisor, "refresh_performance_state", return_value=view), \
                    mock.patch.object(self.supervisor, "_runtime_read", return_value=self.paused), \
                    mock.patch.object(control, "require_performance_operation",
                                      wraps=control.require_performance_operation) as rule:
                self.assertIn("paused_for_performance_review",
                              self.supervisor.performance_mutation_refusal(root, "release-plan"))
                self.assertIsNone(self.supervisor.performance_mutation_refusal(root, "run-close"))
                self.assertIsNone(self.supervisor.performance_mutation_refusal(root, "monitor-poll"))
            self.assertEqual([call.args[1] for call in rule.call_args_list],
                             ["release-plan", "safe_close", "reconcile"])


class LegacyVersionTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"0123456789abcdef-runtime-control"

    def test_the_legacy_signer_is_gone(self):
        """#385 (Pilot): no shipped version wrote signed legacy records."""
        self.assertFalse(hasattr(control, "sign_legacy_record"))

    def test_tampered_or_unauthenticated_legacy_is_read_only_and_refuses_mutation(self):
        unsigned = {"run_id": "run-legacy", "cursor": 1, "state": "active", "updated_at": NOW.isoformat()}
        forged = {**unsigned, "_auth": {"algorithm": "hmac-sha256", "key_id": "migration-key",
                                        "signature": "0" * 64}}
        for legacy in (unsigned, forged):
            access = control.load_versioned_record(
                legacy,
                expected_schema="handsoff.monitor",
                secrets={"migration-key": self.secret},
            )
            self.assertTrue(access.read_only)
            with self.assertRaisesRegex(control.MutationRefused, "not authenticated"):
                control.load_versioned_record(
                    legacy,
                    expected_schema="handsoff.monitor",
                    secrets={"migration-key": self.secret},
                    for_mutation=True,
                )

    def test_unknown_version_is_inspectable_but_never_mutable(self):
        future = {"schema": "handsoff.monitor", "version": 2, "opaque": {"future": True}}
        access = control.load_versioned_record(future, expected_schema="handsoff.monitor")
        self.assertTrue(access.read_only)
        self.assertFalse(access.migrated)
        with self.assertRaisesRegex(control.MutationRefused, "unknown"):
            control.load_versioned_record(
                future, expected_schema="handsoff.monitor", for_mutation=True
            )


if __name__ == "__main__":
    unittest.main()
