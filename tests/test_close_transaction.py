"""Focused REQ-001/REQ-007 close transaction semantics."""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

import sys
sys.path.insert(0, str(BIN))
import handsoff_close_transaction as close  # noqa: E402


def operation(remote, key, value=True, *, actions=None, optional=False, unavailable_reason=None):
    def observe():
        return remote.get(key)

    def act():
        if actions is not None:
            actions.append(key)
        remote[key] = value

    return close.Operation(observe, lambda found: found == value, act,
                           optional=optional, unavailable_reason=unavailable_reason)


class PullRequestTextTests(unittest.TestCase):
    def test_generated_text_uses_refs_only_and_deduplicates_multiple_items(self):
        body = close.refs_only_pr_body("Implementation details.", [271, "#277", 271])
        self.assertEqual(body, "Implementation details.\n\nRefs #271, #277\n")
        self.assertEqual(close.auto_close_findings("Ship teardown", body), [])

    def test_auto_close_keywords_are_found_in_squash_title_and_body(self):
        findings = close.auto_close_findings(
            "Fixes #271 in the squash commit",
            "Resolves monzta1/project-handsoff#277\nClosed: https://github.com/o/r/issues/8",
        )
        self.assertEqual([(row["field"], row["reference"]) for row in findings], [
            ("title", "#271"),
            ("body", "monzta1/project-handsoff#277"),
            ("body", "https://github.com/o/r/issues/8"),
        ])
        with self.assertRaisesRegex(close.CloseTransactionError, "use Refs"):
            close.enforce_refs_only("Fixes #271", "")
        self.assertEqual(len(close.enforce_refs_only("Fixes #271", "", policy="warn")), 1)
        self.assertEqual(close.auto_close_findings(body="This fixes flaky tests; Refs #271"), [])


class IssueStateTests(unittest.TestCase):
    RUN_PRS = [{"number": 88, "merged": True, "merged_commit": "abc123"}]

    def snapshot(self, state="closed", **closure):
        return {"state": state, "checked_at": "2026-09-23T12:00:00+00:00", "closure": closure}

    def test_only_the_runs_exact_merged_pr_is_attributable(self):
        attributable = self.snapshot(kind="pull_request", pr_number=88, merged=True, merged_commit="abc123")
        wrong_commit = self.snapshot(kind="pull_request", pr_number=88, merged=True, merged_commit="different")
        human = self.snapshot(kind="human", actor="maintainer")
        self.assertEqual(close.issue_state_decision(attributable, self.RUN_PRS, run_token="run-a"), "reopen")
        self.assertEqual(close.issue_state_decision(wrong_commit, self.RUN_PRS, run_token="run-a"), "pause")
        self.assertEqual(close.issue_state_decision(human, self.RUN_PRS, run_token="run-a"), "pause")
        self.assertEqual(close.issue_state_decision(self.snapshot(state="open"), self.RUN_PRS,
                                                    run_token="run-a"), "continue")

    def test_own_close_is_adopted_but_another_run_is_not(self):
        own = self.snapshot(kind="handsoff_run", run_token="run-a")
        foreign = self.snapshot(kind="handsoff_run", run_token="run-b")
        self.assertEqual(close.issue_state_decision(own, [], run_token="run-a"), "already_closed")
        self.assertEqual(close.issue_state_decision(foreign, [], run_token="run-a"), "pause")

    def test_each_required_observation_is_durably_checkpointed_before_decision(self):
        item = {}
        persisted = []
        for checkpoint in close.ISSUE_CHECKPOINTS:
            result = close.checkpoint_issue_state(
                item, checkpoint, self.snapshot(state="open"), self.RUN_PRS,
                run_token="run-a", persist=lambda: persisted.append(deepcopy(item)),
            )
            self.assertEqual(result, "continue")
        self.assertEqual(list(item["issue_states"]), list(close.ISSUE_CHECKPOINTS))
        self.assertEqual(len(persisted), 4)

        with self.assertRaises(close.IssueClosureBlocked):
            close.checkpoint_issue_state(
                item, "pre_close", self.snapshot(kind="human", actor="moncy"), self.RUN_PRS,
                run_token="run-a", persist=lambda: persisted.append(deepcopy(item)),
            )
        self.assertEqual(persisted[-1]["issue_states"]["pre_close"]["closure"]["kind"], "human")


class ReconciliationTests(unittest.TestCase):
    def transaction(self):
        self.persisted = []
        record = close.new_transaction(".", "run-a")
        return close.CloseTransaction(record, persist=lambda state: self.persisted.append(state),
                                      clock=lambda: f"t{len(self.persisted):03d}")

    def test_item_intent_precedes_action_and_lost_response_is_adopted(self):
        transaction = self.transaction()
        remote = {name: False for name in close.ITEM_OPERATIONS}
        action_snapshots = []

        def lost_comment_response():
            action_snapshots.append(deepcopy(transaction.record))
            remote["commented"] = True
            raise TimeoutError("response was lost")

        operations = {
            name: operation(remote, name)
            for name in close.ITEM_OPERATIONS
        }
        operations["commented"] = close.Operation(
            lambda: remote["commented"], bool, lost_comment_response,
        )
        result = transaction.reconcile_item("issue-271", operations)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["operations"]["commented"]["completion"], "read_back")
        self.assertEqual(action_snapshots[0]["items"]["issue-271"]["operations"]["commented"]["state"],
                         "intent")
        for name in close.ITEM_OPERATIONS:
            self.assertEqual(result["operations"][name]["state"], "complete")

    def test_partial_multi_item_failure_resumes_only_unfinished_operation(self):
        transaction = self.transaction()
        remote = {"one-comment": False, "one-close": False, "two-comment": False, "two-close": False}
        actions = []
        one = {
            "commented": operation(remote, "one-comment", actions=actions),
            "closed": operation(remote, "one-close", actions=actions),
        }
        transaction.reconcile_item("issue-271", one)

        failures = {"remaining": 1}

        def flaky_close():
            actions.append("two-close")
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise ConnectionError("API unavailable")
            remote["two-close"] = True

        two = {
            "commented": operation(remote, "two-comment", actions=actions),
            "closed": close.Operation(lambda: remote["two-close"], bool, flaky_close),
        }
        with self.assertRaises(close.ReconciliationError):
            transaction.reconcile_item("issue-277", two)
        self.assertTrue(remote["two-comment"])
        transaction.reconcile_item("issue-277", two)
        transaction.reconcile_item("issue-271", one)
        self.assertEqual(actions.count("one-comment"), 1)
        self.assertEqual(actions.count("one-close"), 1)
        self.assertEqual(actions.count("two-comment"), 1)
        self.assertEqual(actions.count("two-close"), 2)

    def test_every_unfinished_item_operation_revalidates_current_gates(self):
        transaction = self.transaction()
        remote = {}
        checks = []

        def gate():
            checks.append("gate")
            if len(checks) == 3:
                raise close.CloseTransactionError("live verification became stale")

        operations = {name: operation(remote, name) for name in close.ITEM_OPERATIONS}
        with self.assertRaisesRegex(close.CloseTransactionError, "became stale"):
            transaction.reconcile_item("issue-271", operations, revalidate=gate)
        self.assertEqual(transaction.record["items"]["issue-271"]["operations"]["prepared"]["state"],
                         "complete")
        self.assertEqual(transaction.record["items"]["issue-271"]["operations"]["commented"]["state"],
                         "complete")
        self.assertEqual(transaction.record["items"]["issue-271"]["operations"]["closed"]["state"],
                         "pending")
        self.assertEqual(transaction.record["items"]["issue-271"]["operations"]["closed"]["attempts"], 0)

    def test_ordered_teardown_stops_then_resumes_and_is_idempotent(self):
        transaction = self.transaction()
        remote = {name: False for name in close.TEARDOWN_STEPS}
        actions = []
        failed = {"once": True}

        operations = {name: operation(remote, name, actions=actions) for name in close.TEARDOWN_STEPS}

        def fail_archive_once():
            actions.append("archive")
            if failed["once"]:
                failed["once"] = False
                raise PermissionError("archive unavailable")
            remote["archive"] = True

        operations["archive"] = close.Operation(lambda: remote["archive"], bool, fail_archive_once)
        operations["optional_analysis"] = close.Operation(
            lambda: False, bool, lambda: None, optional=True,
            unavailable_reason="analyzer is not installed",
        )

        with self.assertRaises(close.ReconciliationError):
            transaction.run(operations)
        self.assertEqual(actions, ["prepare", "final_report_post", "archive"])
        result = transaction.run(operations)
        self.assertEqual(actions, [
            "prepare", "final_report_post", "archive", "archive", "fleet_unregister",
            "dashboard_shutdown", "config_restore",
        ])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["steps"]["optional_analysis"]["state"], "skipped")
        self.assertEqual(result["steps"]["optional_analysis"]["skip_reason"],
                         "analyzer is not installed")
        before = list(actions)
        transaction.run(operations)
        self.assertEqual(actions, before)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.root = str(Path(".").resolve())
        self.expected = {"root": self.root, "run_token": "run-a", "pid": 123, "endpoint": "127.0.0.1:8796"}
        self.metadata = dict(self.expected)
        self.endpoint = dict(self.expected)

    def classify(self, metadata=None, endpoint=None, live=True):
        return close.resource_ownership(
            self.expected,
            self.metadata if metadata is None else metadata,
            self.endpoint if endpoint is None else endpoint,
            pid_alive=lambda _pid: live,
        )["state"]

    def test_ownership_requires_root_token_live_pid_and_endpoint(self):
        self.assertEqual(self.classify(), "owned")
        self.assertEqual(self.classify(live=False), "stale")
        reused = {**self.endpoint, "run_token": "another-run"}
        self.assertEqual(self.classify(endpoint=reused), "foreign")
        reused = {**self.endpoint, "endpoint": "127.0.0.1:9999"}
        self.assertEqual(self.classify(endpoint=reused), "foreign")
        concurrent = {**self.metadata, "root": str(Path("../other").resolve())}
        self.assertEqual(self.classify(metadata=concurrent), "foreign")

    def test_absent_resource_is_an_idempotent_success_state(self):
        result = close.resource_ownership(self.expected, None, None, pid_alive=lambda _pid: False)
        self.assertEqual(result["state"], "absent")


class ArchiveAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="handsoff-close-")
        self.path = Path(self.temp.name) / "archive" / "run.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_collision_adopts_exact_content_and_refuses_difference(self):
        first = close.write_archive_once(self.path, {"run": "a", "value": 1})
        second = close.write_archive_once(self.path, {"value": 1, "run": "a"})
        self.assertEqual(first["state"], "created")
        self.assertEqual(second["state"], "adopted")
        self.assertEqual(first["sha256"], second["sha256"])
        with self.assertRaisesRegex(close.CloseConflict, "differs"):
            close.write_archive_once(self.path, {"run": "b"})
        self.assertIn(b'"run":"a"', self.path.read_bytes())

    def test_config_restore_is_compare_and_restore_and_preserves_human_edits(self):
        cell = {"value": "temporary"}
        restored = close.compare_and_restore_config(
            read=lambda: cell["value"], write=lambda value: cell.update(value=value),
            original="original", run_written="temporary",
        )
        self.assertEqual((restored["decision"], cell["value"]), ("restore", "original"))

        cell["value"] = "human edit"
        preserved = close.compare_and_restore_config(
            read=lambda: cell["value"], write=lambda value: cell.update(value=value),
            original="original", run_written="temporary",
        )
        self.assertEqual((preserved["decision"], cell["value"]), ("preserve", "human edit"))


class MigrationTests(unittest.TestCase):
    def test_authenticated_legacy_migration_never_synthesizes_completion(self):
        legacy = {"report_posted": True, "archived": True, "items": {"issue-271": {"closed": True}}}
        migrated = close.migrate_transaction(legacy, root=".", run_token="run-a", authenticated=True)
        self.assertEqual(migrated["migrated_from"], "legacy")
        self.assertEqual(migrated["legacy"], legacy)
        self.assertEqual(migrated["steps"], {})
        self.assertEqual(migrated["items"], {})
        self.assertEqual(migrated["state"], "open")

    def test_legacy_requires_authentication_and_unknown_versions_are_read_only(self):
        with self.assertRaisesRegex(close.ReadOnlyTransactionError, "unauthenticated legacy"):
            close.migrate_transaction({"archived": True}, root=".", run_token="run-a", authenticated=False)
        with self.assertRaisesRegex(close.ReadOnlyTransactionError, "unsupported.*99"):
            close.migrate_transaction({"version": 99}, root=".", run_token="run-a", authenticated=True)

    def test_v1_identity_mismatch_refuses_mutation(self):
        record = close.new_transaction(".", "run-a")
        with self.assertRaisesRegex(close.ReadOnlyTransactionError, "ownership mismatch"):
            close.migrate_transaction(record, root=".", run_token="run-b", authenticated=True)


if __name__ == "__main__":
    unittest.main()
