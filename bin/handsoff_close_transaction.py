#!/usr/bin/env python3
"""Crash-safe policy and reconciliation primitives for ``run-close``.

This module deliberately knows nothing about the supervisor's storage lock or
about GitHub/Fleet/dashboard transports.  Callers supply small observe, act,
and persist functions.  That keeps the close protocol independently testable
and gives every external side effect the same write-ahead/read-back shape.

The current record schema is version 1.  Legacy records are retained as
evidence but are migrated with every operation pending: only reconciliation
against the real resource may mark an operation complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping


SCHEMA_VERSION = 1
ISSUE_CHECKPOINTS = ("pre_merge", "post_merge", "pre_report", "pre_close")
ITEM_OPERATIONS = ("prepared", "commented", "closed", "ticked")
TEARDOWN_STEPS = (
    "prepare",
    "final_report_post",
    "archive",
    "fleet_unregister",
    "dashboard_shutdown",
    "config_restore",
    "optional_analysis",
)
TERMINAL_OPERATION_STATES = frozenset({"complete", "skipped"})


class CloseTransactionError(RuntimeError):
    """Base class for a refused or incomplete close transaction."""


class ReconciliationError(CloseTransactionError):
    """A side effect could not be proven complete by read-back."""


class CloseConflict(CloseTransactionError):
    """A named resource exists but does not contain the intended value."""


class ReadOnlyTransactionError(CloseTransactionError):
    """A record may be inspected, but its schema is unsafe to mutate."""


class IssueClosureBlocked(CloseTransactionError):
    """A human or unattributable closure requires operator review."""


class OptionalStepUnavailable(CloseTransactionError):
    """An optional close step is unavailable and should be visibly skipped."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_root(root: str | os.PathLike[str]) -> str:
    return str(Path(root).expanduser().resolve())


def _clean_token(token: object) -> str:
    if not isinstance(token, str) or not token.strip() or len(token) > 256:
        raise CloseTransactionError("run_token must be a non-empty string of at most 256 characters")
    return token.strip()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# REQ-001: PR text and issue-state policy

# GitHub's closing keywords are deliberately enumerated.  A finding requires
# a keyword immediately followed by an issue reference, avoiding false
# positives such as "this fixes flaky tests".
_CLOSE_KEYWORD = r"close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?"
_ISSUE_REFERENCE = (
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]{0,9}"
    r"|https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]{0,9}"
)
AUTO_CLOSE_RE = re.compile(
    rf"(?i)(?P<keyword>\b(?:{_CLOSE_KEYWORD})\b)\s*:?[ \t]*(?P<reference>{_ISSUE_REFERENCE})"
)


def auto_close_findings(title: str = "", body: str = "") -> list[dict[str, str]]:
    """Return auto-close keyword findings from both squash title and body."""
    findings: list[dict[str, str]] = []
    for field, text in (("title", title), ("body", body)):
        for match in AUTO_CLOSE_RE.finditer(str(text or "")):
            findings.append({
                "field": field,
                "keyword": match.group("keyword"),
                "reference": match.group("reference"),
                "text": match.group(0),
            })
    return findings


def enforce_refs_only(title: str = "", body: str = "", *, policy: str = "block") -> list[dict[str, str]]:
    """Warn about or reject PR text that could close an issue on merge.

    ``policy='warn'`` returns findings for a caller to display.  ``block``
    raises, and ``allow`` only performs detection.  Generated text should use
    :func:`refs_line` and call this function with the default policy.
    """
    if policy not in {"allow", "warn", "block"}:
        raise ValueError("auto-close policy must be allow, warn, or block")
    findings = auto_close_findings(title, body)
    if findings and policy == "block":
        summary = ", ".join(f"{row['field']}:{row['text']}" for row in findings)
        raise CloseTransactionError(f"PR text contains GitHub auto-close keyword(s): {summary}; use Refs")
    return findings


def _normalise_reference(value: int | str) -> str:
    if isinstance(value, bool):
        raise ValueError("issue reference must not be boolean")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("issue number must be positive")
        return f"#{value}"
    text = str(value).strip()
    if re.fullmatch(_ISSUE_REFERENCE, text, re.IGNORECASE):
        return text
    raise ValueError(f"invalid issue reference: {value!r}")


def refs_line(references: Iterable[int | str]) -> str:
    """Render a stable, duplicate-free non-closing PR reference line."""
    unique: list[str] = []
    for value in references:
        reference = _normalise_reference(value)
        if reference not in unique:
            unique.append(reference)
    if not unique:
        raise ValueError("at least one issue reference is required")
    return "Refs " + ", ".join(unique)


def refs_only_pr_body(body: str, references: Iterable[int | str]) -> str:
    """Append a generated ``Refs`` line after refusing unsafe supplied text."""
    enforce_refs_only(body=body)
    text = str(body or "").rstrip()
    return f"{text}\n\n{refs_line(references)}\n" if text else refs_line(references) + "\n"


def _normalise_issue_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    state = str(snapshot.get("state") or "unknown").lower()
    if state not in {"open", "closed", "unknown"}:
        state = "unknown"
    closure = snapshot.get("closure") if isinstance(snapshot.get("closure"), Mapping) else {}
    clean = {
        "state": state,
        "checked_at": str(snapshot.get("checked_at") or _now()),
        "closure": {
            "kind": str(closure.get("kind") or "unknown"),
            "pr_number": closure.get("pr_number") if isinstance(closure.get("pr_number"), int) else None,
            "merged": closure.get("merged") is True,
            "merged_commit": closure.get("merged_commit") if isinstance(closure.get("merged_commit"), str) else None,
            "run_token": closure.get("run_token") if isinstance(closure.get("run_token"), str) else None,
            "actor": closure.get("actor") if isinstance(closure.get("actor"), str) else None,
        },
    }
    return clean


def attributable_auto_close(snapshot: Mapping[str, Any], run_pull_requests: Iterable[Mapping[str, Any]]) -> bool:
    """True only for a merged PR whose identity belongs to this run."""
    issue = _normalise_issue_snapshot(snapshot)
    closure = issue["closure"]
    if issue["state"] != "closed" or closure["kind"] != "pull_request" or not closure["merged"]:
        return False
    for candidate in run_pull_requests:
        if candidate.get("number") != closure["pr_number"] or candidate.get("merged") is not True:
            continue
        expected_commit = candidate.get("merged_commit")
        if expected_commit and expected_commit != closure["merged_commit"]:
            continue
        return True
    return False


def issue_state_decision(snapshot: Mapping[str, Any], run_pull_requests: Iterable[Mapping[str, Any]],
                         *, run_token: str) -> str:
    """Return ``continue``, ``reopen``, ``already_closed``, or ``pause``.

    A close attributed to this transaction's token is the only closed state
    that can satisfy final read-back.  A merged run PR is safe to reopen.
    Every other closure is treated as human/unknown and is never mutated.
    """
    issue = _normalise_issue_snapshot(snapshot)
    if issue["state"] == "open":
        return "continue"
    if issue["state"] != "closed":
        return "pause"
    closure = issue["closure"]
    if closure["kind"] == "handsoff_run" and closure["run_token"] == run_token:
        return "already_closed"
    if attributable_auto_close(issue, run_pull_requests):
        return "reopen"
    return "pause"


def checkpoint_issue_state(item_record: MutableMapping[str, Any], checkpoint: str,
                           snapshot: Mapping[str, Any], run_pull_requests: Iterable[Mapping[str, Any]],
                           *, run_token: str, persist: Callable[[], None]) -> str:
    """Persist an issue observation before returning its required action."""
    if checkpoint not in ISSUE_CHECKPOINTS:
        raise ValueError(f"unknown issue checkpoint: {checkpoint}")
    states = item_record.setdefault("issue_states", {})
    states[checkpoint] = _normalise_issue_snapshot(snapshot)
    persist()
    decision = issue_state_decision(states[checkpoint], run_pull_requests, run_token=run_token)
    if decision == "pause":
        raise IssueClosureBlocked(f"issue is closed at {checkpoint} without attributable run auto-close")
    return decision


# ---------------------------------------------------------------------------
# REQ-007: generic intent/action/read-back transaction

@dataclass(frozen=True)
class Operation:
    """One externally observable idempotent operation."""

    observe: Callable[[], Any]
    satisfied: Callable[[Any], bool]
    act: Callable[[], Any]
    optional: bool = False
    unavailable_reason: str | Callable[[], str] | None = None


def new_transaction(root: str | os.PathLike[str], run_token: str) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "root": _clean_root(root),
        "run_token": _clean_token(run_token),
        "state": "open",
        "steps": {},
        "items": {},
        "created_at": _now(),
        "completed_at": None,
    }


def migrate_transaction(raw: Mapping[str, Any] | None, *, root: str | os.PathLike[str], run_token: str,
                        authenticated: bool) -> dict[str, Any]:
    """Validate v1, lazily migrate authenticated legacy, or refuse unknown.

    Legacy truthy booleans are *not* completion proof.  They are retained
    under ``legacy`` and all current steps remain pending for read-back.
    """
    expected_root = _clean_root(root)
    expected_token = _clean_token(run_token)
    if raw is None:
        return new_transaction(expected_root, expected_token)
    if not isinstance(raw, Mapping):
        raise ReadOnlyTransactionError("close transaction record is not an object")
    version = raw.get("version")
    if version is None:
        if not authenticated:
            raise ReadOnlyTransactionError("unauthenticated legacy close transaction cannot be migrated")
        migrated = new_transaction(expected_root, expected_token)
        migrated["migrated_from"] = "legacy"
        migrated["legacy"] = deepcopy(dict(raw))
        return migrated
    if version != SCHEMA_VERSION:
        raise ReadOnlyTransactionError(f"unsupported close transaction version {version!r}; mutation refused")
    record = deepcopy(dict(raw))
    if record.get("root") != expected_root or record.get("run_token") != expected_token:
        raise ReadOnlyTransactionError("close transaction root/run_token ownership mismatch")
    if not isinstance(record.get("steps"), dict) or not isinstance(record.get("items"), dict):
        raise ReadOnlyTransactionError("close transaction steps/items are malformed")
    if record.get("state") not in {"open", "complete"}:
        raise ReadOnlyTransactionError("close transaction state is malformed")
    return record


def _error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:512]


class CloseTransaction:
    """Persisted ordered reconciliation for one run token and root."""

    def __init__(self, record: MutableMapping[str, Any], *, persist: Callable[[dict[str, Any]], None],
                 clock: Callable[[], str] = _now):
        self.record = record
        self._persist_callback = persist
        self._clock = clock

    def _persist(self) -> None:
        self._persist_callback(deepcopy(dict(self.record)))

    def _operation_record(self, container: MutableMapping[str, Any], name: str) -> MutableMapping[str, Any]:
        operations = container.setdefault("operations", {})
        row = operations.setdefault(name, {
            "state": "pending", "attempts": 0, "intent_at": None,
            "completed_at": None, "last_observed": None, "last_error": None,
        })
        if not isinstance(row, MutableMapping) or row.get("state") not in {
                "pending", "intent", "complete", "skipped"}:
            raise ReadOnlyTransactionError(f"operation {name} has malformed state")
        return row

    def _skip(self, row: MutableMapping[str, Any], reason: str) -> None:
        row.update(state="skipped", completed_at=self._clock(), last_error=None,
                   skip_reason=(" ".join(str(reason).split()) or "unavailable")[:512])
        self._persist()

    def reconcile(self, container: MutableMapping[str, Any], name: str, operation: Operation,
                  *, revalidate: Callable[[], None] | None = None) -> dict[str, Any]:
        """Reconcile one operation, persisting intent before any side effect."""
        row = self._operation_record(container, name)
        if row["state"] in TERMINAL_OPERATION_STATES:
            return dict(row)
        if revalidate is not None:
            revalidate()
        if operation.unavailable_reason is not None:
            reason = (operation.unavailable_reason() if callable(operation.unavailable_reason)
                      else operation.unavailable_reason)
            if reason:
                if operation.optional:
                    self._skip(row, str(reason))
                    return dict(row)
                raise ReconciliationError(str(reason))

        row.update(state="intent", attempts=int(row.get("attempts") or 0) + 1,
                   intent_at=self._clock(), last_error=None)
        self._persist()

        try:
            observed = operation.observe()
            row["last_observed"] = deepcopy(observed)
            self._persist()
        except OptionalStepUnavailable as exc:
            if operation.optional:
                self._skip(row, str(exc))
                return dict(row)
            row.update(state="pending", last_error=_error_text(exc))
            self._persist()
            raise ReconciliationError(row["last_error"]) from exc
        except Exception as exc:
            row.update(state="pending", last_error=_error_text(exc))
            self._persist()
            raise ReconciliationError(f"{name} observation failed: {row['last_error']}") from exc

        if operation.satisfied(observed):
            row.update(state="complete", completed_at=self._clock(), last_error=None,
                       completion="adopted")
            self._persist()
            return dict(row)

        action_error: BaseException | None = None
        try:
            operation.act()
            row["acted_at"] = self._clock()
            self._persist()
        except OptionalStepUnavailable as exc:
            if operation.optional:
                self._skip(row, str(exc))
                return dict(row)
            action_error = exc
        except Exception as exc:  # a lost response may still have succeeded
            action_error = exc

        try:
            observed = operation.observe()
            row["last_observed"] = deepcopy(observed)
        except Exception as exc:
            row.update(state="pending", last_error=_error_text(action_error or exc))
            self._persist()
            raise ReconciliationError(f"{name} read-back failed: {row['last_error']}") from (action_error or exc)

        if operation.satisfied(observed):
            row.update(state="complete", completed_at=self._clock(), last_error=None,
                       completion="read_back")
            self._persist()
            return dict(row)
        message = _error_text(action_error) if action_error else "read-back did not match intent"
        row.update(state="pending", last_error=message)
        self._persist()
        raise ReconciliationError(f"{name} incomplete: {message}") from action_error

    def reconcile_item(self, item_id: str, operations: Mapping[str, Operation], *,
                       revalidate: Callable[[], None] | None = None) -> dict[str, Any]:
        """Reconcile prepared/commented/closed/ticked in canonical order."""
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128:
            raise ValueError("item_id must be a non-empty bounded string")
        unknown = set(operations) - set(ITEM_OPERATIONS)
        if unknown:
            raise ValueError(f"unknown item operation(s): {', '.join(sorted(unknown))}")
        item = self.record.setdefault("items", {}).setdefault(item_id, {"operations": {}})
        for name in ITEM_OPERATIONS:
            operation = operations.get(name)
            if operation is not None:
                self.reconcile(item, name, operation, revalidate=revalidate)
        item["state"] = "complete" if all(
            self._operation_record(item, name)["state"] in TERMINAL_OPERATION_STATES
            for name in operations
        ) else "open"
        self._persist()
        return deepcopy(item)

    def run(self, operations: Mapping[str, Operation], *,
            revalidate: Callable[[], None] | None = None) -> dict[str, Any]:
        """Run complete teardown in order, resuming only unfinished steps."""
        unknown = set(operations) - set(TEARDOWN_STEPS)
        missing = set(TEARDOWN_STEPS) - set(operations)
        if unknown or missing:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(sorted(missing)))
            if unknown:
                detail.append("unknown " + ", ".join(sorted(unknown)))
            raise ValueError("invalid teardown operations: " + "; ".join(detail))
        if self.record.get("state") == "complete":
            return deepcopy(dict(self.record))
        step_container = {"operations": self.record.setdefault("steps", {})}
        for name in TEARDOWN_STEPS:
            self.reconcile(step_container, name, operations[name], revalidate=revalidate)
        self.record["state"] = "complete"
        self.record["completed_at"] = self._clock()
        self._persist()
        return deepcopy(dict(self.record))


# ---------------------------------------------------------------------------
# Concrete, transport-independent close decisions

def resource_ownership(expected: Mapping[str, Any], metadata: Mapping[str, Any] | None,
                       endpoint: Mapping[str, Any] | None, *, pid_alive: Callable[[int], bool]) -> dict[str, str]:
    """Classify a process/endpoint without ever claiming a foreign resource.

    Owned requires matching resolved root, run token and PID in both durable
    metadata and the live endpoint, plus a live PID.  Missing/dead endpoint
    identity is stale; any live mismatch is foreign and must be untouched.
    """
    if metadata is None and endpoint is None:
        return {"state": "absent", "reason": "no ownership metadata or endpoint"}
    required = ("root", "run_token", "pid")
    if not isinstance(metadata, Mapping) or any(key not in metadata for key in required):
        return {"state": "foreign", "reason": "ownership metadata is incomplete"}
    try:
        root_matches = _clean_root(metadata["root"]) == _clean_root(expected["root"])
    except (KeyError, TypeError, ValueError):
        root_matches = False
    token_matches = metadata.get("run_token") == expected.get("run_token")
    pid = metadata.get("pid")
    pid_matches = isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and pid == expected.get("pid")
    if not (root_matches and token_matches and pid_matches):
        return {"state": "foreign", "reason": "root/run_token/PID metadata mismatch"}
    if not pid_alive(pid):
        return {"state": "stale", "reason": "owned PID is not live"}
    if not isinstance(endpoint, Mapping):
        return {"state": "stale", "reason": "live endpoint identity is unavailable"}
    try:
        endpoint_root = _clean_root(endpoint.get("root"))
    except (TypeError, ValueError):
        endpoint_root = ""
    if (endpoint_root, endpoint.get("run_token"), endpoint.get("pid")) != (
            _clean_root(expected["root"]), expected.get("run_token"), expected.get("pid")):
        return {"state": "foreign", "reason": "live endpoint identity mismatch"}
    expected_endpoint = expected.get("endpoint")
    if expected_endpoint is not None and endpoint.get("endpoint") != expected_endpoint:
        return {"state": "foreign", "reason": "endpoint address was reused"}
    return {"state": "owned", "reason": "root/run_token/PID/endpoint identity match"}


def write_archive_once(path: str | os.PathLike[str], payload: bytes | str | Mapping[str, Any]) -> dict[str, str]:
    """Create one archive without overwrite; adopt only byte-identical collision."""
    target = Path(path)
    if isinstance(payload, Mapping):
        intended = _canonical_json(payload)
    elif isinstance(payload, str):
        intended = payload.encode("utf-8")
    else:
        intended = bytes(payload)
    digest = hashlib.sha256(intended).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise CloseConflict(f"archive collision cannot be read: {target}") from exc
        if existing != intended:
            raise CloseConflict(f"archive collision differs from intended content: {target}")
        return {"state": "adopted", "path": str(target), "sha256": digest}
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(intended)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    if target.read_bytes() != intended:
        raise ReconciliationError(f"archive read-back mismatch: {target}")
    return {"state": "created", "path": str(target), "sha256": digest}


def config_restore_decision(current: Any, *, original: Any, run_written: Any) -> str:
    """Return restore/adopt/preserve for a compare-and-restore override."""
    if current == original:
        return "adopt"
    if current == run_written:
        return "restore"
    return "preserve"


def compare_and_restore_config(*, read: Callable[[], Any], write: Callable[[Any], None],
                               original: Any, run_written: Any) -> dict[str, str]:
    """Restore only while the current value still equals this run's write."""
    decision = config_restore_decision(read(), original=original, run_written=run_written)
    if decision == "restore":
        write(deepcopy(original))
        if read() != original:
            raise ReconciliationError("config restore read-back mismatch")
    return {
        "state": "complete",
        "decision": decision,
        "reason": ("run-written value restored" if decision == "restore" else
                   "original value already present" if decision == "adopt" else
                   "current value differs; preserving user edit"),
    }
