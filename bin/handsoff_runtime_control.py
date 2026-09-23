#!/usr/bin/env python3
"""Pure runtime-control contracts for evidence, monitoring, and performance.

The integration layer owns persistence and side effects.  This module owns the
closed v1 records and deterministic decisions used before those side effects.
It deliberately has no dependency on an agent, model, dashboard, or
``handsoff_lib`` so polling and restart recovery remain model-free.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence


MAX_SUBJECTS = 256
MAX_PATHS = 512
MAX_EVENTS = 4096
MAX_EPISODES = 64
MAX_IN_FLIGHT = 256
MAX_TEXT = 512
MAX_REASON = 2048

DEPENDENCY_CLASSES = (
    "product_source",
    "test_source",
    "criteria_specs",
    "governance_policy",
    "deployment_target",
    "runtime_bytes",
)
DRIFT_CLASSES = DEPENDENCY_CLASSES + (
    "framework_state",
    "generated_release_metadata",
    "unrelated",
    "unknown",
    "mixed",
)
BLOCKING_GATES = frozenset(
    {"regression", "deployment", "performance_pause", "new_authority"}
)
PERFORMANCE_PAUSE_ALLOWED_OPERATIONS = frozenset(
    {"inspect", "reconcile", "explicit_resume", "cancel", "safe_close"}
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class RuntimeControlError(ValueError):
    """Base class for a refused runtime-control operation."""


class SchemaError(RuntimeControlError):
    """A record does not satisfy its closed bounded schema."""


class CASConflict(RuntimeControlError):
    """A monitor compare-and-swap precondition or lease fence failed."""


class LeaseHeld(CASConflict):
    """Another live monitor owner holds the lease."""


class MutationRefused(RuntimeControlError):
    """A read-only, gated, or unauthenticated record cannot be mutated."""


def _closed(
    value: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str] = (),
    *,
    where: str,
) -> None:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{where} must be an object")
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing:
        raise SchemaError(f"{where} missing fields: {', '.join(missing)}")
    if extra:
        raise SchemaError(f"{where} has unknown fields: {', '.join(extra)}")


def _text(value: Any, *, where: str, maximum: int = MAX_TEXT, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum:
        raise SchemaError(f"{where} must be a bounded string")
    if any(ord(char) < 32 for char in value):
        raise SchemaError(f"{where} contains a control character")
    return value


def _identifier(value: Any, *, where: str) -> str:
    value = _text(value, where=where, maximum=128)
    if not ID_RE.fullmatch(value):
        raise SchemaError(f"{where} is not a valid identifier")
    return value


def _sha(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SchemaError(f"{where} must be a lowercase SHA-256")
    return value


def _integer(value: Any, *, where: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SchemaError(f"{where} must be an integer from {minimum} through {maximum}")
    return value


def _number(value: Any, *, where: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise SchemaError(f"{where} must be a number >= {minimum}")
    return float(value)


def _time(value: Any, *, where: str) -> datetime:
    if not isinstance(value, str):
        raise SchemaError(f"{where} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchemaError(f"{where} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise SchemaError(f"{where} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _now(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise SchemaError("now must include a timezone")
        return value.astimezone(timezone.utc)
    return _time(value, where="now")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _path(value: Any, *, where: str) -> str:
    value = _text(value, where=where, maximum=512)
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."} or ".." in path.parts or value != path.as_posix():
        raise SchemaError(f"{where} must be a normalized repository-relative path")
    return value


def _bounded_list(value: Any, *, where: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SchemaError(f"{where} must be a list with at most {maximum} entries")
    return value


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical bytes used by hashes and legacy authentication."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_paths(value: Any, *, where: str) -> list[str]:
    result = [_path(item, where=f"{where}[]") for item in _bounded_list(value, where=where, maximum=MAX_PATHS)]
    if len(result) != len(set(result)):
        raise SchemaError(f"{where} contains duplicate paths")
    return result


# ---------------------------------------------------------------------------
# Evidence dependency map and conservative drift decisions


def validate_dependency_map(record: Mapping[str, Any]) -> None:
    """Validate the closed ``handsoff.evidence_dependencies`` v1 schema."""

    _closed(
        record,
        {"schema", "version", "framework_state_paths", "unrelated_paths", "subjects"},
        where="dependency map",
    )
    if record["schema"] != "handsoff.evidence_dependencies" or record["version"] != 1:
        raise SchemaError("dependency map must be handsoff.evidence_dependencies v1")
    framework = _validate_paths(record["framework_state_paths"], where="framework_state_paths")
    unrelated = _validate_paths(record["unrelated_paths"], where="unrelated_paths")
    if set(framework) & set(unrelated):
        raise SchemaError("framework and unrelated path allowlists overlap")
    subjects = _bounded_list(record["subjects"], where="subjects", maximum=MAX_SUBJECTS)
    ids: set[str] = set()
    for index, subject in enumerate(subjects):
        where = f"subjects[{index}]"
        _closed(
            subject,
            {
                "subject_id",
                "subject_kind",
                "input_hash",
                "dependency_hashes",
                "path_dependencies",
                "refreshable_paths",
                "generated_outputs",
            },
            {"actor", "reason", "verdict_hash"},
            where=where,
        )
        subject_id = _identifier(subject["subject_id"], where=f"{where}.subject_id")
        if subject_id in ids:
            raise SchemaError(f"duplicate dependency subject {subject_id}")
        ids.add(subject_id)
        if subject["subject_kind"] not in {"evidence", "decision"}:
            raise SchemaError(f"{where}.subject_kind must be evidence or decision")
        _sha(subject["input_hash"], where=f"{where}.input_hash")
        if subject["subject_kind"] == "decision":
            _text(subject.get("actor"), where=f"{where}.actor")
            _text(subject.get("reason"), where=f"{where}.reason", maximum=MAX_REASON)
            _sha(subject.get("verdict_hash"), where=f"{where}.verdict_hash")
        elif set(subject) & {"actor", "reason", "verdict_hash"}:
            raise SchemaError(f"{where} evidence cannot carry decision provenance")
        hashes = subject["dependency_hashes"]
        _closed(hashes, DEPENDENCY_CLASSES, where=f"{where}.dependency_hashes")
        for name in DEPENDENCY_CLASSES:
            _sha(hashes[name], where=f"{where}.dependency_hashes.{name}")
        path_dependencies = subject["path_dependencies"]
        _closed(path_dependencies, DEPENDENCY_CLASSES, where=f"{where}.path_dependencies")
        seen_paths: set[str] = set()
        for name in DEPENDENCY_CLASSES:
            paths = _validate_paths(path_dependencies[name], where=f"{where}.path_dependencies.{name}")
            overlap = seen_paths & set(paths)
            if overlap:
                raise SchemaError(f"{where} maps paths to multiple dependency classes: {sorted(overlap)}")
            seen_paths.update(paths)
        refreshable = _validate_paths(subject["refreshable_paths"], where=f"{where}.refreshable_paths")
        if not set(refreshable) <= set(framework):
            raise SchemaError(f"{where}.refreshable_paths must be framework-state allowlisted")
        outputs = _bounded_list(subject["generated_outputs"], where=f"{where}.generated_outputs", maximum=MAX_PATHS)
        output_paths: set[str] = set()
        for output_index, output in enumerate(outputs):
            output_where = f"{where}.generated_outputs[{output_index}]"
            _closed(
                output,
                {"path", "producer", "dependency_hash", "output_hash", "deterministic", "allow_auto_refresh"},
                where=output_where,
            )
            output_path = _path(output["path"], where=f"{output_where}.path")
            if output_path in output_paths or output_path in seen_paths:
                raise SchemaError(f"{output_where}.path is duplicated or ambiguously classified")
            output_paths.add(output_path)
            _identifier(output["producer"], where=f"{output_where}.producer")
            _sha(output["dependency_hash"], where=f"{output_where}.dependency_hash")
            _sha(output["output_hash"], where=f"{output_where}.output_hash")
            if not isinstance(output["deterministic"], bool) or not isinstance(output["allow_auto_refresh"], bool):
                raise SchemaError(f"{output_where} flags must be booleans")


def dependency_subject(dependency_map: Mapping[str, Any], subject_id: str) -> Mapping[str, Any]:
    validate_dependency_map(dependency_map)
    matches = [subject for subject in dependency_map["subjects"] if subject["subject_id"] == subject_id]
    if not matches:
        raise SchemaError(f"unknown dependency subject {subject_id}")
    return matches[0]


def _classify_path(path: str, subject: Mapping[str, Any], dependency_map: Mapping[str, Any]) -> str:
    matches: list[str] = []
    for name in DEPENDENCY_CLASSES:
        if path in subject["path_dependencies"][name]:
            matches.append(name)
    if any(path == output["path"] for output in subject["generated_outputs"]):
        matches.append("generated_release_metadata")
    if path in dependency_map["framework_state_paths"]:
        matches.append("framework_state")
    if path in dependency_map["unrelated_paths"]:
        matches.append("unrelated")
    unique = set(matches)
    if not unique:
        return "unknown"
    if len(unique) > 1:
        return "mixed"
    return matches[0]


def classify_dependency_changes(
    dependency_map: Mapping[str, Any], subject_id: str, changes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Classify paths for one subject; renames remain explicit and conservative."""

    subject = dependency_subject(dependency_map, subject_id)
    bounded = _bounded_list(list(changes), where="changes", maximum=MAX_PATHS)
    result: list[dict[str, Any]] = []
    for index, change in enumerate(bounded):
        _closed(change, set(), {"path", "old_path", "new_path"}, where=f"changes[{index}]")
        if "path" in change:
            if set(change) != {"path"}:
                raise SchemaError(f"changes[{index}] path form cannot include rename fields")
            path = _path(change["path"], where=f"changes[{index}].path")
            result.append({"path": path, "classification": _classify_path(path, subject, dependency_map), "renamed": False})
            continue
        if set(change) != {"old_path", "new_path"}:
            raise SchemaError(f"changes[{index}] must contain path or old_path/new_path")
        old_path = _path(change["old_path"], where=f"changes[{index}].old_path")
        new_path = _path(change["new_path"], where=f"changes[{index}].new_path")
        old_class = _classify_path(old_path, subject, dependency_map)
        new_class = _classify_path(new_path, subject, dependency_map)
        classification = old_class if old_class == new_class == "unrelated" else "mixed"
        result.append(
            {
                "old_path": old_path,
                "new_path": new_path,
                "classification": classification,
                "renamed": True,
            }
        )
    return result


def _validate_current_hashes(current_hashes: Mapping[str, Any]) -> None:
    _closed(current_hashes, DEPENDENCY_CLASSES, where="current dependency hashes")
    for name in DEPENDENCY_CLASSES:
        _sha(current_hashes[name], where=f"current dependency hashes.{name}")


def assess_dependency_drift(
    dependency_map: Mapping[str, Any],
    subject_id: str,
    changes: Sequence[Mapping[str, Any]],
    current_dependency_hashes: Mapping[str, Any],
    *,
    current_input_hash: str,
    regenerated_outputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``current``, ``auto_refresh``, ``auto_reaffirm``, or ``invalidate``.

    Generated output observations are trusted only when their bytes match a
    deterministic regeneration and their producer dependency hash is unchanged.
    A decision additionally requires its complete input hash to be byte-identical;
    verdict text is never used as a substitute.
    """

    subject = dependency_subject(dependency_map, subject_id)
    _validate_current_hashes(current_dependency_hashes)
    _sha(current_input_hash, where="current_input_hash")
    observations = regenerated_outputs or {}
    if not isinstance(observations, Mapping) or len(observations) > MAX_PATHS:
        raise SchemaError("regenerated_outputs must be a bounded object")
    classified = classify_dependency_changes(dependency_map, subject_id, changes)
    reasons: list[str] = []
    changed_classes = {entry["classification"] for entry in classified}
    dependency_mismatches = [
        name for name in DEPENDENCY_CLASSES
        if current_dependency_hashes[name] != subject["dependency_hashes"][name]
    ]
    if dependency_mismatches:
        reasons.append("changed dependency hashes: " + ", ".join(dependency_mismatches))
    if any(entry["renamed"] and entry["classification"] != "unrelated" for entry in classified):
        reasons.append("renamed dependency or generated path")
    unsafe_classes = changed_classes & (set(DEPENDENCY_CLASSES) | {"unknown", "mixed"})
    if unsafe_classes:
        reasons.append("unsafe drift classes: " + ", ".join(sorted(unsafe_classes)))

    affected_refresh = False
    for entry in classified:
        if entry["classification"] == "framework_state" and entry.get("path") in subject["refreshable_paths"]:
            affected_refresh = True
        if entry["classification"] != "generated_release_metadata" or entry.get("renamed"):
            continue
        affected_refresh = True
        path = entry["path"]
        declaration = next(output for output in subject["generated_outputs"] if output["path"] == path)
        observation = observations.get(path)
        if not declaration["deterministic"] or not declaration["allow_auto_refresh"]:
            reasons.append(f"{path} is not allowlisted deterministic output")
            continue
        if not isinstance(observation, Mapping):
            reasons.append(f"{path} lacks deterministic regeneration proof")
            continue
        try:
            _closed(observation, {"output_hash", "regenerated_hash", "dependency_hash"}, where=f"regenerated_outputs.{path}")
            output_hash = _sha(observation["output_hash"], where=f"regenerated_outputs.{path}.output_hash")
            regenerated_hash = _sha(observation["regenerated_hash"], where=f"regenerated_outputs.{path}.regenerated_hash")
            dependency_hash = _sha(observation["dependency_hash"], where=f"regenerated_outputs.{path}.dependency_hash")
        except SchemaError as error:
            reasons.append(str(error))
            continue
        if output_hash != regenerated_hash:
            reasons.append(f"{path} does not match deterministic regeneration")
        if dependency_hash != declaration["dependency_hash"]:
            reasons.append(f"{path} producer dependencies changed")

    if reasons:
        action = "invalidate"
    elif subject["subject_kind"] == "decision" and current_input_hash != subject["input_hash"]:
        action = "invalidate"
        reasons.append("complete decision input hash changed")
    elif not affected_refresh:
        action = "current"
        reasons.append("no mapped dependency affecting this subject changed")
    elif subject["subject_kind"] == "evidence":
        action = "auto_refresh"
        reasons.append("only allowlisted deterministic/framework output changed")
    else:
        action = "auto_reaffirm"
        reasons.append("complete decision input hash is byte-identical")
    return {
        "subject_id": subject_id,
        "subject_kind": subject["subject_kind"],
        "action": action,
        "classifications": classified,
        "dependency_mismatches": dependency_mismatches,
        "original_actor": subject.get("actor"),
        "original_reason": subject.get("reason"),
        "input_hash": subject["input_hash"],
        "reasons": reasons[:16],
    }


def dependency_audit_event(assessment: Mapping[str, Any], *, at: datetime | str) -> dict[str, Any]:
    """Build a bounded event explaining refresh, reaffirmation, or invalidation."""

    action = assessment.get("action")
    if action not in {"current", "auto_refresh", "auto_reaffirm", "invalidate"}:
        raise SchemaError("assessment has an unknown action")
    return {
        "kind": f"evidence_dependency_{action}",
        "at": _iso(_now(at)),
        "subject_id": _identifier(assessment.get("subject_id"), where="assessment.subject_id"),
        "subject_kind": assessment.get("subject_kind"),
        "input_hash": _sha(assessment.get("input_hash"), where="assessment.input_hash"),
        "original_actor": assessment.get("original_actor"),
        "original_reason": assessment.get("original_reason"),
        "reasons": [_text(reason, where="assessment.reasons[]", maximum=MAX_REASON) for reason in assessment.get("reasons", [])[:16]],
    }


# ---------------------------------------------------------------------------
# Durable monitor lease, restart discovery, and deterministic recovery


MONITOR_STATES = frozenset({"active", "completed", "cancelled", "blocked", "migration_pending"})


def validate_monitor(record: Mapping[str, Any]) -> None:
    _closed(
        record,
        {
            "schema", "version", "run_id", "owner_instance", "lease_epoch",
            "lease_expires_at", "cursor", "state", "updated_at",
        },
        {"migration"},
        where="monitor",
    )
    if record["schema"] != "handsoff.monitor" or record["version"] != 1:
        raise SchemaError("monitor must be handsoff.monitor v1")
    _identifier(record["run_id"], where="monitor.run_id")
    if record["owner_instance"] is not None:
        _identifier(record["owner_instance"], where="monitor.owner_instance")
    _integer(record["lease_epoch"], where="monitor.lease_epoch")
    if record["lease_expires_at"] is not None:
        _time(record["lease_expires_at"], where="monitor.lease_expires_at")
    _integer(record["cursor"], where="monitor.cursor")
    if record["state"] not in MONITOR_STATES:
        raise SchemaError("monitor.state is unknown")
    _time(record["updated_at"], where="monitor.updated_at")
    if record["state"] == "active" and (record["owner_instance"] is None or record["lease_expires_at"] is None):
        raise SchemaError("active monitor requires an owner and lease")
    if "migration" in record:
        migration = record["migration"]
        _closed(migration, {"source_version", "authenticated", "legacy_state", "pending"}, where="monitor.migration")
        _integer(migration["source_version"], where="monitor.migration.source_version")
        if migration["authenticated"] is not True:
            raise SchemaError("monitor migration must be authenticated")
        _text(migration["legacy_state"], where="monitor.migration.legacy_state")
        pending = _bounded_list(migration["pending"], where="monitor.migration.pending", maximum=16)
        for item in pending:
            _identifier(item, where="monitor.migration.pending[]")


def new_monitor(run_id: str, owner_instance: str, *, now: datetime | str, lease_seconds: int = 30) -> dict[str, Any]:
    timestamp = _now(now)
    _identifier(run_id, where="run_id")
    _identifier(owner_instance, where="owner_instance")
    _integer(lease_seconds, where="lease_seconds", minimum=1, maximum=3600)
    record = {
        "schema": "handsoff.monitor",
        "version": 1,
        "run_id": run_id,
        "owner_instance": owner_instance,
        "lease_epoch": 1,
        "lease_expires_at": _iso(timestamp + timedelta(seconds=lease_seconds)),
        "cursor": 0,
        "state": "active",
        "updated_at": _iso(timestamp),
    }
    validate_monitor(record)
    return record


def claim_monitor(
    record: Mapping[str, Any],
    owner_instance: str,
    *,
    now: datetime | str,
    lease_seconds: int,
    expected_epoch: int,
    expected_cursor: int,
) -> dict[str, Any]:
    """Acquire or renew using owner+epoch+cursor CAS and return a new fence."""

    validate_monitor(record)
    owner_instance = _identifier(owner_instance, where="owner_instance")
    timestamp = _now(now)
    _integer(lease_seconds, where="lease_seconds", minimum=1, maximum=3600)
    if (record["lease_epoch"], record["cursor"]) != (expected_epoch, expected_cursor):
        raise CASConflict("monitor epoch/cursor compare-and-swap failed")
    if record["state"] in {"completed", "cancelled"}:
        raise MutationRefused(f"cannot claim terminal monitor {record['state']}")
    expires = _time(record["lease_expires_at"], where="monitor.lease_expires_at") if record["lease_expires_at"] else None
    if expires and expires > timestamp and record["owner_instance"] not in {None, owner_instance}:
        raise LeaseHeld(f"monitor lease is held by {record['owner_instance']}")
    updated = copy.deepcopy(dict(record))
    updated.update(
        owner_instance=owner_instance,
        lease_epoch=record["lease_epoch"] + 1,
        lease_expires_at=_iso(timestamp + timedelta(seconds=lease_seconds)),
        state="active",
        updated_at=_iso(timestamp),
    )
    updated.pop("migration", None)
    validate_monitor(updated)
    return updated


def advance_monitor_cursor(
    record: Mapping[str, Any],
    *,
    owner_instance: str,
    lease_epoch: int,
    expected_cursor: int,
    new_cursor: int,
    now: datetime | str,
) -> dict[str, Any]:
    """Fence a poll/recovery write and monotonically advance its ledger cursor."""

    validate_monitor(record)
    timestamp = _now(now)
    if record["owner_instance"] != owner_instance or record["lease_epoch"] != lease_epoch:
        raise CASConflict("monitor owner/lease epoch fence failed")
    if record["cursor"] != expected_cursor:
        raise CASConflict("monitor cursor compare-and-swap failed")
    if new_cursor <= expected_cursor:
        raise CASConflict("monitor cursor must advance")
    if record["state"] != "active":
        raise MutationRefused("only an active monitor can advance")
    if _time(record["lease_expires_at"], where="monitor.lease_expires_at") <= timestamp:
        raise CASConflict("monitor lease expired")
    updated = copy.deepcopy(dict(record))
    updated["cursor"] = _integer(new_cursor, where="new_cursor")
    updated["updated_at"] = _iso(timestamp)
    validate_monitor(updated)
    return updated


def finish_monitor(
    record: Mapping[str, Any],
    state: str,
    *,
    owner_instance: str,
    lease_epoch: int,
    expected_cursor: int,
    now: datetime | str,
) -> dict[str, Any]:
    validate_monitor(record)
    if state not in {"completed", "cancelled", "blocked"}:
        raise SchemaError("finish state must be completed, cancelled, or blocked")
    timestamp = _now(now)
    if (
        record["owner_instance"] != owner_instance
        or record["lease_epoch"] != lease_epoch
        or record["cursor"] != expected_cursor
    ):
        raise CASConflict("monitor owner/epoch/cursor compare-and-swap failed")
    if record["lease_expires_at"] is None or _time(record["lease_expires_at"], where="monitor.lease_expires_at") <= timestamp:
        raise CASConflict("monitor lease expired")
    updated = copy.deepcopy(dict(record))
    updated.update(state=state, lease_expires_at=None, updated_at=_iso(timestamp))
    validate_monitor(updated)
    return updated


def validate_rediscovery_inputs(record: Mapping[str, Any]) -> None:
    _closed(record, {"schema", "version", "fleet_runs", "ledger_runs", "monitors"}, where="rediscovery inputs")
    if record["schema"] != "handsoff.monitor_rediscovery" or record["version"] != 1:
        raise SchemaError("rediscovery inputs must be handsoff.monitor_rediscovery v1")
    for field in ("fleet_runs", "ledger_runs"):
        rows = _bounded_list(record[field], where=field, maximum=MAX_SUBJECTS)
        for index, row in enumerate(rows):
            _closed(row, {"run_id", "root", "state", "cursor", "updated_at"}, where=f"{field}[{index}]")
            _identifier(row["run_id"], where=f"{field}[{index}].run_id")
            _text(row["root"], where=f"{field}[{index}].root", maximum=1024)
            if row["state"] not in {"active", "in_progress", "paused", "blocked", "completed", "cancelled", "failed"}:
                raise SchemaError(f"{field}[{index}].state is unknown")
            _integer(row["cursor"], where=f"{field}[{index}].cursor")
            _time(row["updated_at"], where=f"{field}[{index}].updated_at")
    monitors = _bounded_list(record["monitors"], where="monitors", maximum=MAX_SUBJECTS)
    for monitor in monitors:
        validate_monitor(monitor)


def make_rediscovery_inputs(
    fleet_runs: Sequence[Mapping[str, Any]],
    ledger_runs: Sequence[Mapping[str, Any]],
    monitors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    record = {
        "schema": "handsoff.monitor_rediscovery",
        "version": 1,
        "fleet_runs": copy.deepcopy(list(fleet_runs)),
        "ledger_runs": copy.deepcopy(list(ledger_runs)),
        "monitors": copy.deepcopy(list(monitors)),
    }
    validate_rediscovery_inputs(record)
    return record


def rediscover_active_runs(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge Fleet, ledger, and durable records; ledger terminal truth wins."""

    validate_rediscovery_inputs(inputs)
    run_ids = sorted(
        {row["run_id"] for row in inputs["fleet_runs"]}
        | {row["run_id"] for row in inputs["ledger_runs"]}
        | {row["run_id"] for row in inputs["monitors"]}
    )
    result: list[dict[str, Any]] = []
    terminal = {"completed", "cancelled", "failed"}
    for run_id in run_ids:
        fleet = [row for row in inputs["fleet_runs"] if row["run_id"] == run_id]
        ledger = [row for row in inputs["ledger_runs"] if row["run_id"] == run_id]
        monitors = [row for row in inputs["monitors"] if row["run_id"] == run_id]
        latest_ledger = max(ledger, key=lambda row: (_time(row["updated_at"], where="ledger.updated_at"), row["cursor"]), default=None)
        if latest_ledger and latest_ledger["state"] in terminal:
            continue
        roots = {row["root"] for row in fleet + ledger}
        if len(roots) > 1:
            result.append({"run_id": run_id, "root": None, "cursor": 0, "sources": [], "disposition": "blocked_conflict"})
            continue
        states = {row["state"] for row in fleet + ledger} | {row["state"] for row in monitors}
        if not (states & {"active", "in_progress", "paused", "blocked", "migration_pending"}):
            continue
        cursors = [row["cursor"] for row in fleet + ledger + monitors]
        sources = [name for name, rows in (("fleet", fleet), ("ledger", ledger), ("monitor", monitors)) if rows]
        result.append(
            {
                "run_id": run_id,
                "root": next(iter(roots), None),
                "cursor": max(cursors, default=0),
                "sources": sources,
                "disposition": "resume_monitoring",
            }
        )
    return result


def decide_monitor_action(snapshot: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Choose a polling/recovery action without a model call or side effect."""

    _closed(
        snapshot,
        {
            "schema", "version", "run_id", "state", "percent", "verified_complete",
            "gate", "worker_state", "failure_category", "recovery_attempts",
            "escalation_recorded", "result_adoptable",
        },
        where="run snapshot",
    )
    if snapshot["schema"] != "handsoff.run_snapshot" or snapshot["version"] != 1:
        raise SchemaError("run snapshot must be handsoff.run_snapshot v1")
    _identifier(snapshot["run_id"], where="run snapshot.run_id")
    if snapshot["state"] not in {"active", "in_progress", "paused", "blocked", "completed", "cancelled", "failed"}:
        raise SchemaError("run snapshot.state is unknown")
    percent = _integer(snapshot["percent"], where="run snapshot.percent", maximum=100)
    if not isinstance(snapshot["verified_complete"], bool):
        raise SchemaError("run snapshot.verified_complete must be boolean")
    if snapshot["gate"] not in BLOCKING_GATES | {"none"}:
        raise SchemaError("run snapshot.gate is unknown")
    if snapshot["worker_state"] not in {"none", "healthy", "quiet", "stalled", "failed", "quota_exhausted", "result_ready"}:
        raise SchemaError("run snapshot.worker_state is unknown")
    if snapshot["failure_category"] is not None:
        _identifier(snapshot["failure_category"], where="run snapshot.failure_category")
    attempts = _integer(snapshot["recovery_attempts"], where="run snapshot.recovery_attempts", maximum=100)
    for name in ("escalation_recorded", "result_adoptable"):
        if not isinstance(snapshot[name], bool):
            raise SchemaError(f"run snapshot.{name} must be boolean")
    _closed(policy, {"same_task_retry_cap", "equivalent_quota_fallback", "safe_result_adoption"}, where="recovery policy")
    cap = _integer(policy["same_task_retry_cap"], where="recovery policy.same_task_retry_cap", maximum=16)
    for name in ("equivalent_quota_fallback", "safe_result_adoption"):
        if not isinstance(policy[name], bool):
            raise SchemaError(f"recovery policy.{name} must be boolean")

    action = "poll"
    reason = "healthy or awaiting deterministic progress"
    mutating = False
    if snapshot["gate"] in BLOCKING_GATES:
        action, reason = "wait_at_gate", f"{snapshot['gate']} gate remains authoritative"
    elif snapshot["state"] == "completed" and percent == 100 and snapshot["verified_complete"]:
        action, reason = "complete_monitor", "verified 100 percent terminal state"
        mutating = True
    elif snapshot["state"] == "cancelled":
        action, reason = "stop_cancelled", "run was explicitly cancelled"
    elif snapshot["state"] in {"blocked", "failed"} and snapshot["worker_state"] == "none":
        action, reason = "report_blocked", "terminal state has no authorized recovery"
    elif snapshot["worker_state"] == "result_ready" and snapshot["result_adoptable"] and policy["safe_result_adoption"]:
        action, reason, mutating = "adopt_safe_result", "bounded result passed deterministic adoption checks", True
    elif snapshot["worker_state"] == "quota_exhausted":
        if policy["equivalent_quota_fallback"] and attempts < cap:
            action, reason, mutating = "fallback_equivalent_quota", "configured equivalent quota fallback is available", True
        elif not snapshot["escalation_recorded"]:
            action, reason, mutating = "record_escalation", "quota recovery exhausted or unavailable", True
        else:
            action, reason = "report_blocked", "quota escalation already recorded"
    elif snapshot["worker_state"] in {"stalled", "failed"}:
        if attempts < cap:
            action, reason, mutating = "retry_same_task", "configured bounded same-task recovery", True
        elif not snapshot["escalation_recorded"]:
            action, reason, mutating = "record_escalation", "bounded recovery exhausted", True
        else:
            action, reason = "report_blocked", "recovery escalation already recorded"
    return {"action": action, "reason": reason, "mutating": mutating, "model_calls": 0}


# ---------------------------------------------------------------------------
# Performance episodes and the exact 90/120 minute transitions


def _validate_hold(hold: Mapping[str, Any], *, where: str) -> None:
    _closed(hold, {"hold_id", "kind", "started_at", "ended_at", "evidence_hash"}, where=where)
    _identifier(hold["hold_id"], where=f"{where}.hold_id")
    if hold["kind"] not in {"pilot", "external_blocker"}:
        raise SchemaError(f"{where}.kind must be pilot or external_blocker")
    started = _time(hold["started_at"], where=f"{where}.started_at")
    if hold["ended_at"] is not None and _time(hold["ended_at"], where=f"{where}.ended_at") < started:
        raise SchemaError(f"{where}.ended_at precedes started_at")
    _sha(hold["evidence_hash"], where=f"{where}.evidence_hash")


def _validate_sleep(sleep: Mapping[str, Any], *, where: str) -> None:
    _closed(sleep, {"started_at", "ended_at", "measured_seconds"}, where=where)
    started = _time(sleep["started_at"], where=f"{where}.started_at")
    ended = _time(sleep["ended_at"], where=f"{where}.ended_at")
    measured = _number(sleep["measured_seconds"], where=f"{where}.measured_seconds")
    if ended < started or abs((ended - started).total_seconds() - measured) > 0.001:
        raise SchemaError(f"{where} measured sleep must equal its bounded interval")


def validate_performance_history(record: Mapping[str, Any]) -> None:
    _closed(
        record,
        {"schema", "version", "run_id", "episodes", "cumulative_active_seconds", "breaches", "resume_decisions"},
        where="performance history",
    )
    if record["schema"] != "handsoff.performance_history" or record["version"] != 1:
        raise SchemaError("performance history must be handsoff.performance_history v1")
    _identifier(record["run_id"], where="performance history.run_id")
    episodes = _bounded_list(record["episodes"], where="performance history.episodes", maximum=MAX_EPISODES)
    if not episodes:
        raise SchemaError("performance history requires an episode")
    ids: set[str] = set()
    for index, episode in enumerate(episodes):
        where = f"performance history.episodes[{index}]"
        _closed(
            episode,
            {
                "episode_id", "sequence", "state", "started_at", "ended_at",
                "warning_at", "paused_at", "holds", "sleeps", "breach",
                "in_flight_dispositions",
            },
            where=where,
        )
        episode_id = _identifier(episode["episode_id"], where=f"{where}.episode_id")
        if episode_id in ids:
            raise SchemaError("performance episode ids must be unique")
        ids.add(episode_id)
        if _integer(episode["sequence"], where=f"{where}.sequence", minimum=1) != index + 1:
            raise SchemaError("performance episode sequence must be contiguous")
        if episode["state"] not in {"active", "warning", "paused_for_performance_review", "completed", "migration_pending"}:
            raise SchemaError(f"{where}.state is unknown")
        started = _time(episode["started_at"], where=f"{where}.started_at")
        for field in ("ended_at", "warning_at", "paused_at"):
            if episode[field] is not None and _time(episode[field], where=f"{where}.{field}") < started:
                raise SchemaError(f"{where}.{field} precedes started_at")
        if not isinstance(episode["breach"], bool):
            raise SchemaError(f"{where}.breach must be boolean")
        for hold_index, hold in enumerate(_bounded_list(episode["holds"], where=f"{where}.holds", maximum=MAX_EVENTS)):
            _validate_hold(hold, where=f"{where}.holds[{hold_index}]")
        for sleep_index, sleep in enumerate(_bounded_list(episode["sleeps"], where=f"{where}.sleeps", maximum=MAX_EVENTS)):
            _validate_sleep(sleep, where=f"{where}.sleeps[{sleep_index}]")
        dispositions = _bounded_list(episode["in_flight_dispositions"], where=f"{where}.in_flight_dispositions", maximum=MAX_IN_FLIGHT)
        for disposition_index, disposition in enumerate(dispositions):
            disposition_where = f"{where}.in_flight_dispositions[{disposition_index}]"
            _closed(disposition, {"operation_id", "action", "checkpoint", "late_result"}, where=disposition_where)
            _identifier(disposition["operation_id"], where=f"{disposition_where}.operation_id")
            if disposition["action"] not in {"terminate", "detach_read_only_reconcile"}:
                raise SchemaError(f"{disposition_where}.action is unknown")
            if disposition["checkpoint"] is not True or disposition["late_result"] != "quarantine":
                raise SchemaError(f"{disposition_where} must checkpoint and quarantine late results")
    _number(record["cumulative_active_seconds"], where="performance history.cumulative_active_seconds")
    _integer(record["breaches"], where="performance history.breaches", maximum=MAX_EPISODES)
    decisions = _bounded_list(record["resume_decisions"], where="performance history.resume_decisions", maximum=MAX_EPISODES)
    for index, decision in enumerate(decisions):
        _validate_resume_decision(decision, where=f"performance history.resume_decisions[{index}]")


def new_performance_history(run_id: str, episode_id: str, *, now: datetime | str) -> dict[str, Any]:
    timestamp = _now(now)
    _identifier(run_id, where="run_id")
    _identifier(episode_id, where="episode_id")
    record = {
        "schema": "handsoff.performance_history",
        "version": 1,
        "run_id": run_id,
        "episodes": [{
            "episode_id": episode_id,
            "sequence": 1,
            "state": "active",
            "started_at": _iso(timestamp),
            "ended_at": None,
            "warning_at": None,
            "paused_at": None,
            "holds": [],
            "sleeps": [],
            "breach": False,
            "in_flight_dispositions": [],
        }],
        "cumulative_active_seconds": 0.0,
        "breaches": 0,
        "resume_decisions": [],
    }
    validate_performance_history(record)
    return record


def _union_seconds(intervals: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    clipped = sorted((max(left, start), min(right, end)) for left, right in intervals if right > start and left < end)
    merged: list[tuple[datetime, datetime]] = []
    for left, right in clipped:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return sum((right - left).total_seconds() for left, right in merged)


def episode_active_seconds(episode: Mapping[str, Any], *, now: datetime | str) -> float:
    """Count wall time once, subtracting only measured sleep and persisted holds."""

    validation_episode = copy.deepcopy(dict(episode))
    validation_episode["sequence"] = 1
    wrapper = {
        "schema": "handsoff.performance_history", "version": 1, "run_id": "validation",
        "episodes": [validation_episode], "cumulative_active_seconds": 0, "breaches": 0,
        "resume_decisions": [],
    }
    validate_performance_history(wrapper)
    timestamp = _now(now)
    start = _time(episode["started_at"], where="episode.started_at")
    end = _time(episode["ended_at"], where="episode.ended_at") if episode["ended_at"] else timestamp
    end = min(end, timestamp)
    if end < start:
        raise SchemaError("now precedes episode start")
    exclusions: list[tuple[datetime, datetime]] = []
    for hold in episode["holds"]:
        hold_start = _time(hold["started_at"], where="hold.started_at")
        hold_end = _time(hold["ended_at"], where="hold.ended_at") if hold["ended_at"] else end
        exclusions.append((hold_start, hold_end))
    for sleep in episode["sleeps"]:
        exclusions.append((_time(sleep["started_at"], where="sleep.started_at"), _time(sleep["ended_at"], where="sleep.ended_at")))
    return max(0.0, (end - start).total_seconds() - _union_seconds(exclusions, start, end))


def total_active_seconds(history: Mapping[str, Any], *, now: datetime | str) -> float:
    validate_performance_history(history)
    return sum(episode_active_seconds(episode, now=now) for episode in history["episodes"])


def _validate_in_flight(item: Mapping[str, Any], *, where: str) -> None:
    _closed(item, {"operation_id", "kind", "location", "cancellable", "bounded", "state"}, where=where)
    _identifier(item["operation_id"], where=f"{where}.operation_id")
    if item["kind"] not in {"agent", "test", "recovery", "merge", "release", "cleanup", "external_call"}:
        raise SchemaError(f"{where}.kind is unknown")
    if item["location"] not in {"local", "remote"}:
        raise SchemaError(f"{where}.location is unknown")
    if not isinstance(item["cancellable"], bool) or not isinstance(item["bounded"], bool):
        raise SchemaError(f"{where} cancellable/bounded flags must be boolean")
    if item["state"] not in {"queued", "running", "detached"}:
        raise SchemaError(f"{where}.state is unknown")


def performance_pause_plan(in_flight: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for index, item in enumerate(_bounded_list(list(in_flight), where="in_flight", maximum=MAX_IN_FLIGHT)):
        _validate_in_flight(item, where=f"in_flight[{index}]")
        if item["state"] == "detached":
            action = "detach_read_only_reconcile"
        elif item["cancellable"] and item["bounded"]:
            action = "terminate"
        else:
            action = "detach_read_only_reconcile"
        plan.append(
            {
                "operation_id": item["operation_id"],
                "action": action,
                "checkpoint": True,
                "late_result": "quarantine",
            }
        )
    return plan


def transition_performance(
    history: Mapping[str, Any],
    *,
    now: datetime | str,
    in_flight: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the exact >=90 warning and >=120 pause transition once."""

    validate_performance_history(history)
    timestamp = _now(now)
    updated = copy.deepcopy(dict(history))
    episode = updated["episodes"][-1]
    active_seconds = episode_active_seconds(episode, now=timestamp)
    action = "none"
    if episode["state"] in {"active", "warning"} and active_seconds >= 120 * 60:
        action = "pause_for_performance_review"
        episode.update(
            state="paused_for_performance_review",
            paused_at=_iso(timestamp),
            ended_at=_iso(timestamp),
            breach=True,
            in_flight_dispositions=performance_pause_plan(in_flight),
        )
        if episode["warning_at"] is None:
            episode["warning_at"] = _iso(timestamp)
        updated["breaches"] += 1
    elif episode["state"] == "active" and active_seconds >= 90 * 60:
        action = "deadline_warning"
        episode["state"] = "warning"
        episode["warning_at"] = _iso(timestamp)
    updated["cumulative_active_seconds"] = total_active_seconds(updated, now=timestamp)
    validate_performance_history(updated)
    return updated, {
        "action": action,
        "episode_id": episode["episode_id"],
        "active_seconds": active_seconds,
        "block_new_work": episode["state"] == "paused_for_performance_review",
        "in_flight_dispositions": copy.deepcopy(episode["in_flight_dispositions"]),
    }


def performance_operation_allowed(history: Mapping[str, Any], operation: str) -> bool:
    validate_performance_history(history)
    _identifier(operation, where="operation")
    if history["episodes"][-1]["state"] != "paused_for_performance_review":
        return True
    return operation in PERFORMANCE_PAUSE_ALLOWED_OPERATIONS


def require_performance_operation(history: Mapping[str, Any], operation: str) -> None:
    if not performance_operation_allowed(history, operation):
        raise MutationRefused(f"{operation} is blocked by paused_for_performance_review")


def late_result_disposition(history: Mapping[str, Any], episode_id: str, operation_id: str) -> str:
    validate_performance_history(history)
    episode = next((item for item in history["episodes"] if item["episode_id"] == episode_id), None)
    if episode is None:
        raise SchemaError("unknown performance episode")
    _identifier(operation_id, where="operation_id")
    if episode["state"] == "paused_for_performance_review":
        return "quarantine"
    return "eligible_for_normal_validation"


def _validate_resume_decision(decision: Mapping[str, Any], *, where: str = "resume decision") -> None:
    _closed(decision, {"decision_id", "action", "actor", "reason", "evidence_hash", "at"}, where=where)
    _identifier(decision["decision_id"], where=f"{where}.decision_id")
    if decision["action"] != "resume":
        raise SchemaError(f"{where}.action must be resume")
    _text(decision["actor"], where=f"{where}.actor")
    _text(decision["reason"], where=f"{where}.reason", maximum=MAX_REASON)
    _sha(decision["evidence_hash"], where=f"{where}.evidence_hash")
    _time(decision["at"], where=f"{where}.at")


def resume_performance(
    history: Mapping[str, Any],
    decision: Mapping[str, Any],
    new_episode_id: str,
) -> dict[str, Any]:
    """Open a new budget episode only after an explicit recorded decision."""

    validate_performance_history(history)
    _validate_resume_decision(decision)
    _identifier(new_episode_id, where="new_episode_id")
    if history["episodes"][-1]["state"] != "paused_for_performance_review":
        raise MutationRefused("performance resume requires a paused episode")
    if any(item["decision_id"] == decision["decision_id"] for item in history["resume_decisions"]):
        raise MutationRefused("resume decision was already used")
    if any(item["episode_id"] == new_episode_id for item in history["episodes"]):
        raise SchemaError("new performance episode id is not unique")
    updated = copy.deepcopy(dict(history))
    updated["resume_decisions"].append(copy.deepcopy(dict(decision)))
    updated["episodes"].append(
        {
            "episode_id": new_episode_id,
            "sequence": len(updated["episodes"]) + 1,
            "state": "active",
            "started_at": _iso(_time(decision["at"], where="resume decision.at")),
            "ended_at": None,
            "warning_at": None,
            "paused_at": None,
            "holds": [],
            "sleeps": [],
            "breach": False,
            "in_flight_dispositions": [],
        }
    )
    validate_performance_history(updated)
    return updated


def reconstruct_performance_history(
    run_id: str,
    events: Sequence[Mapping[str, Any]],
    *,
    now: datetime | str,
) -> dict[str, Any]:
    """Rebuild episode clocks from bounded ledger events after restart."""

    _identifier(run_id, where="run_id")
    bounded = _bounded_list(list(events), where="performance events", maximum=MAX_EVENTS)
    history: dict[str, Any] | None = None
    open_holds: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, event in enumerate(bounded):
        _closed(
            event,
            {"kind", "at", "episode_id"},
            {"sequence", "hold_id", "hold_kind", "evidence_hash", "started_at", "measured_seconds", "decision"},
            where=f"performance events[{index}]",
        )
        at = _time(event["at"], where=f"performance events[{index}].at")
        episode_id = _identifier(event["episode_id"], where=f"performance events[{index}].episode_id")
        kind = event["kind"]
        if kind == "episode_started":
            if history is None:
                history = new_performance_history(run_id, episode_id, now=at)
            else:
                decision = event.get("decision")
                if not isinstance(decision, Mapping):
                    raise SchemaError("resumed episode requires an explicit decision")
                history = resume_performance(history, decision, episode_id)
            if event.get("sequence") != len(history["episodes"]):
                raise SchemaError("episode event sequence is not contiguous")
            continue
        if history is None:
            raise SchemaError("performance timeline must begin with episode_started")
        episode_index = next((i for i, item in enumerate(history["episodes"]) if item["episode_id"] == episode_id), None)
        if episode_index is None:
            raise SchemaError("performance event references an unknown episode")
        episode = history["episodes"][episode_index]
        if kind == "hold_started":
            hold_id = _identifier(event.get("hold_id"), where="hold event.hold_id")
            if hold_id in open_holds:
                raise SchemaError("hold is already open")
            hold = {
                "hold_id": hold_id,
                "kind": event.get("hold_kind"),
                "started_at": _iso(at),
                "ended_at": None,
                "evidence_hash": event.get("evidence_hash"),
            }
            _validate_hold(hold, where="hold event")
            episode["holds"].append(hold)
            open_holds[hold_id] = (episode_index, hold)
        elif kind == "hold_ended":
            hold_id = _identifier(event.get("hold_id"), where="hold event.hold_id")
            if hold_id not in open_holds:
                raise SchemaError("hold end has no matching start")
            owner_index, hold = open_holds.pop(hold_id)
            if owner_index != episode_index:
                raise SchemaError("hold cannot cross performance episodes")
            hold["ended_at"] = _iso(at)
            _validate_hold(hold, where="hold event")
        elif kind == "sleep_measured":
            measured = _number(event.get("measured_seconds"), where="sleep event.measured_seconds")
            started = _time(event.get("started_at"), where="sleep event.started_at")
            sleep = {"started_at": _iso(started), "ended_at": _iso(at), "measured_seconds": measured}
            _validate_sleep(sleep, where="sleep event")
            episode["sleeps"].append(sleep)
        elif kind == "deadline_warning":
            episode.update(state="warning", warning_at=_iso(at))
        elif kind == "performance_paused":
            episode.update(state="paused_for_performance_review", warning_at=episode["warning_at"] or _iso(at), paused_at=_iso(at), ended_at=_iso(at), breach=True)
            history["breaches"] += 1
        elif kind == "episode_completed":
            episode.update(state="completed", ended_at=_iso(at))
        else:
            raise SchemaError(f"unknown performance event kind {kind!r}")
    if history is None:
        raise SchemaError("performance timeline is empty")
    history["cumulative_active_seconds"] = total_active_seconds(history, now=now)
    validate_performance_history(history)
    return history


# ---------------------------------------------------------------------------
# Authenticated lazy migration and unknown-version read-only handling


@dataclass(frozen=True)
class RecordAccess:
    record: Mapping[str, Any]
    read_only: bool
    migrated: bool
    reason: str | None


def sign_legacy_record(record: Mapping[str, Any], *, key_id: str, secret: bytes) -> dict[str, Any]:
    """Return a copy carrying an HMAC over all legacy fields except ``_auth``."""

    if not isinstance(secret, bytes) or len(secret) < 16:
        raise SchemaError("legacy authentication secret must be at least 16 bytes")
    _identifier(key_id, where="key_id")
    unsigned = copy.deepcopy(dict(record))
    unsigned.pop("_auth", None)
    signature = hmac.new(secret, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    unsigned["_auth"] = {"algorithm": "hmac-sha256", "key_id": key_id, "signature": signature}
    return unsigned


def _authenticate_legacy(record: Mapping[str, Any], secrets: Mapping[str, bytes]) -> bool:
    auth = record.get("_auth")
    if not isinstance(auth, Mapping):
        return False
    try:
        _closed(auth, {"algorithm", "key_id", "signature"}, where="legacy authentication")
        if auth["algorithm"] != "hmac-sha256":
            return False
        key_id = _identifier(auth["key_id"], where="legacy authentication.key_id")
        signature = _sha(auth["signature"], where="legacy authentication.signature")
        secret = secrets.get(key_id)
        if not isinstance(secret, bytes) or len(secret) < 16:
            return False
        unsigned = dict(record)
        unsigned.pop("_auth", None)
        expected = hmac.new(secret, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except SchemaError:
        return False


def _migrate_legacy_monitor(record: Mapping[str, Any]) -> dict[str, Any]:
    _closed(record, {"run_id", "cursor", "state", "updated_at", "_auth"}, {"owner"}, where="legacy monitor")
    _identifier(record["run_id"], where="legacy monitor.run_id")
    _integer(record["cursor"], where="legacy monitor.cursor")
    _text(record["state"], where="legacy monitor.state")
    updated_at = _iso(_time(record["updated_at"], where="legacy monitor.updated_at"))
    migrated = {
        "schema": "handsoff.monitor", "version": 1, "run_id": record["run_id"],
        "owner_instance": None, "lease_epoch": 0, "lease_expires_at": None,
        "cursor": record["cursor"], "state": "migration_pending", "updated_at": updated_at,
        "migration": {
            "source_version": 0, "authenticated": True, "legacy_state": record["state"],
            "pending": ["lease", "rediscovery"],
        },
    }
    validate_monitor(migrated)
    return migrated


def _migrate_legacy_performance(record: Mapping[str, Any]) -> dict[str, Any]:
    _closed(record, {"run_id", "episode_id", "started_at", "state", "_auth"}, where="legacy performance")
    started = _iso(_time(record["started_at"], where="legacy performance.started_at"))
    migrated = new_performance_history(record["run_id"], record["episode_id"], now=started)
    migrated["episodes"][0]["state"] = "migration_pending"
    # The legacy state is authenticated but cannot prove holds, sleep, warning,
    # completion, or side effects; none are synthesized.
    validate_performance_history(migrated)
    return migrated


def load_versioned_record(
    record: Mapping[str, Any],
    *,
    expected_schema: str,
    secrets: Mapping[str, bytes] | None = None,
    for_mutation: bool = False,
) -> RecordAccess:
    """Validate v1, lazily migrate authenticated legacy, or expose read-only.

    Unknown versions and unauthenticated legacy records remain inspectable as
    opaque values.  Mutation is refused rather than guessed.
    """

    if not isinstance(record, Mapping):
        raise SchemaError("versioned record must be an object")
    if len(record) > 64 or len(canonical_bytes(record)) > 1_000_000:
        raise SchemaError("versioned record exceeds bounded read limits")
    schema = record.get("schema")
    version = record.get("version")
    if schema is not None or version is not None:
        if schema != expected_schema or version != 1:
            reason = f"unknown {schema!r} version {version!r}"
            if for_mutation:
                raise MutationRefused(reason)
            return RecordAccess(copy.deepcopy(dict(record)), True, False, reason)
        if expected_schema == "handsoff.monitor":
            validate_monitor(record)
        elif expected_schema == "handsoff.performance_history":
            validate_performance_history(record)
        elif expected_schema == "handsoff.evidence_dependencies":
            validate_dependency_map(record)
        else:
            reason = f"unsupported schema {expected_schema!r}"
            if for_mutation:
                raise MutationRefused(reason)
            return RecordAccess(copy.deepcopy(dict(record)), True, False, reason)
        return RecordAccess(copy.deepcopy(dict(record)), False, False, None)

    authenticated = _authenticate_legacy(record, secrets or {})
    if not authenticated:
        reason = "legacy record is not authenticated"
        if for_mutation:
            raise MutationRefused(reason)
        return RecordAccess(copy.deepcopy(dict(record)), True, False, reason)
    if expected_schema == "handsoff.monitor":
        migrated = _migrate_legacy_monitor(record)
    elif expected_schema == "handsoff.performance_history":
        migrated = _migrate_legacy_performance(record)
    else:
        raise MutationRefused(f"no authenticated legacy migration for {expected_schema}")
    return RecordAccess(migrated, False, True, "authenticated legacy migration pending persistence")


# Compatibility aliases kept deliberately small for integration callers.
acquire_monitor_lease = claim_monitor
monitor_cursor_cas = advance_monitor_cursor
classify_drift = classify_dependency_changes
dependency_refresh_decision = assess_dependency_drift
polling_recovery_decision = decide_monitor_action
evaluate_performance_episode = transition_performance
