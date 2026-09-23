#!/usr/bin/env python3
"""App-neutral, identity-bound live progress for checks, tests, and CI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROGRESS_FILE = ".handsoff-test-progress.json"
MAX_VISIBLE_UNITS = 16
HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
TERMINAL_RETENTION_SECONDS = 600
TERMINAL_STATES = {"passed", "failed", "timed_out", "cancelled"}
STATES = TERMINAL_STATES | {"queued", "running"}
STATE_PRECEDENCE = ("failed", "timed_out", "cancelled", "running", "queued", "passed")
_PROCESS_LOCK = threading.RLock()
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret|credential|authorization)\s*[=:]\s*)[^\s,;]+"
)


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def command_sha256(commands: list[str] | tuple[str, ...] | str) -> str:
    values = [commands] if isinstance(commands, str) else list(commands)
    raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def progress_path(root: Path) -> Path:
    return Path(root) / PROGRESS_FILE


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


@contextmanager
def _write_lock(root: Path):
    """Cross-process lock so a superseded writer cannot win a check/write race."""
    lock_dir = Path(root) / ".handsoff-test-progress.lock"
    deadline = time.monotonic() + 5
    with _PROCESS_LOCK:
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                try:
                    if time.time() - lock_dir.stat().st_mtime > STALE_SECONDS:
                        lock_dir.rmdir()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("test progress writer lock timed out")
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                lock_dir.rmdir()
            except OSError:
                pass


def _safe_text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    # Telemetry labels are display hints, never command output or environment.
    clean = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", clean)[:limit]


def _unit(index: int, label: str, *, state: str = "queued", total: int | None = None) -> dict:
    return {
        "index": index,
        "label": _safe_text(label) or f"Unit {index}",
        "state": state if state in STATES else "queued",
        "total": total if isinstance(total, int) and total >= 0 else None,
        "done": 0,
        "progress": 0.0 if isinstance(total, int) and total > 0 else None,
        "elapsed_seconds": 0.0,
        "result": None,
    }


def aggregate_units(units: list[dict]) -> dict:
    """Count every unit and choose the deterministic aggregate state."""
    counts = {state: 0 for state in STATES}
    total_work: int | None = 0
    done_work = 0
    for item in units:
        state = item.get("state") if item.get("state") in STATES else "queued"
        counts[state] += 1
        value = item.get("total")
        if value is None or total_work is None:
            total_work = None
        else:
            total_work += max(int(value), 0)
        done_work += max(int(item.get("done") or 0), 0)
    state = next((name for name in STATE_PRECEDENCE if counts[name]), "passed")
    terminal = sum(counts[name] for name in TERMINAL_STATES)
    return {
        "unit_total": len(units),
        "unit_done": terminal,
        "total": total_work,
        "done": done_work,
        **counts,
        "state": state,
    }


def start(root: Path, *, run_id: str, source: str, label: str, units: list[str],
          request_id: str | None = None, command_hash: str | None = None,
          totals: dict | None = None, now: datetime | None = None) -> dict:
    timestamp = _now(now)
    all_units = [_unit(index, item) for index, item in enumerate(units, 1)]
    snapshot = {
        "schema_version": 1,
        "run_id": str(run_id),
        "request_id": str(request_id) if request_id else None,
        "command_sha256": command_hash or command_sha256(units),
        "execution_id": f"tx-{uuid.uuid4().hex}",
        "source": _safe_text(source, 40) or "checks",
        "label": _safe_text(label) or "Test execution",
        "state": "queued",
        "started_at": timestamp,
        "heartbeat_at": timestamp,
        "finished_at": None,
        "result": None,
        "totals": totals or aggregate_units(all_units),
        "unit_count": len(all_units),
        "units": all_units[:MAX_VISIBLE_UNITS],
    }
    with _write_lock(root):
        _atomic_write(progress_path(root), snapshot)
    return snapshot


def write(root: Path, snapshot: dict, *, expected_execution_id: str) -> bool:
    """Atomically update only the execution that still owns the progress file."""
    path = progress_path(root)
    with _write_lock(root):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if current.get("execution_id") != expected_execution_id \
                or snapshot.get("execution_id") != expected_execution_id:
            return False
        snapshot["heartbeat_at"] = _now()
        snapshot["units"] = list(snapshot.get("units") or [])[:MAX_VISIBLE_UNITS]
        _atomic_write(path, snapshot)
        return True


def heartbeat(root: Path, execution_id: str) -> bool:
    path = progress_path(root)
    with _write_lock(root):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if snapshot.get("execution_id") != execution_id or snapshot.get("state") in TERMINAL_STATES:
            return False
        snapshot["heartbeat_at"] = _now()
        _atomic_write(path, snapshot)
        return True


def finish(root: Path, snapshot: dict, *, state: str, result: str | None = None) -> bool:
    if state not in TERMINAL_STATES:
        raise ValueError("terminal progress state required")
    snapshot["state"] = state
    snapshot["finished_at"] = _now()
    snapshot["result"] = _safe_text(result)
    return write(root, snapshot, expected_execution_id=str(snapshot.get("execution_id")))


def read(root: Path, *, run_id: str | None = None, request_id: str | None = None,
         command_hash: str | None = None, execution_id: str | None = None,
         now: datetime | None = None) -> dict | None:
    """Return only an exact, fresh execution; recompute expiry without writes."""
    try:
        payload = json.loads(progress_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {"run_id": run_id, "request_id": request_id,
                "command_sha256": command_hash, "execution_id": execution_id}
    if any(value is not None and payload.get(key) != value for key, value in expected.items()):
        return None
    if payload.get("state") not in STATES or not isinstance(payload.get("execution_id"), str):
        return None
    reference = _parse(payload.get("finished_at") if payload.get("state") in TERMINAL_STATES
                       else payload.get("heartbeat_at"))
    if reference is None:
        return None
    age = ((now or datetime.now(timezone.utc)) - reference).total_seconds()
    ceiling = TERMINAL_RETENTION_SECONDS if payload.get("state") in TERMINAL_STATES else STALE_SECONDS
    if age > ceiling:
        return None
    payload["age_seconds"] = max(round(age, 3), 0)
    payload["units"] = list(payload.get("units") or [])[:MAX_VISIBLE_UNITS]
    payload["visible_unit_count"] = len(payload["units"])
    return payload


def from_ci(run_id: str, ci: dict, *, now: datetime | None = None) -> dict | None:
    """Map Handsoff's external-CI view into the same read-only vocabulary."""
    if not isinstance(ci, dict) or not ci.get("head"):
        return None
    state_map = {"running": "running", "passed": "passed", "failed": "failed"}
    units = []
    for index, check in enumerate(ci.get("checks") or [], 1):
        raw = str(check.get("state") or "").upper()
        if raw == "SUCCESS":
            state = "passed"
        elif raw == "TIMED_OUT":
            state = "timed_out"
        elif raw in {"FAILURE", "ACTION_REQUIRED", "STALE"}:
            state = "failed"
        elif raw == "CANCELLED":
            state = "cancelled"
        elif check.get("queued"):
            state = "queued"
        else:
            state = "running"
        item = _unit(index, check.get("name") or f"CI check {index}", state=state)
        item["elapsed_seconds"] = check.get("elapsed_seconds")
        item["result"] = raw or None
        units.append(item)
    aggregate = aggregate_units(units)
    state = state_map.get(ci.get("state"), aggregate["state"])
    started = ci.get("started_at") or _now(now)
    finished = ci.get("ended_at") if state in TERMINAL_STATES else None
    return {
        "schema_version": 1,
        "run_id": run_id,
        "request_id": None,
        "command_sha256": command_sha256([str(ci.get("head")), *[u["label"] for u in units]]),
        "execution_id": f"ci-{ci.get('head')}",
        "source": "ci",
        "label": f"PR #{ci.get('pr')} CI",
        "state": state,
        "started_at": started,
        "heartbeat_at": ci.get("fetched_at") or started,
        "finished_at": finished,
        "result": state if finished else None,
        "totals": {**aggregate, "state": state},
        "unit_count": len(units),
        "visible_unit_count": min(len(units), MAX_VISIBLE_UNITS),
        "units": units[:MAX_VISIBLE_UNITS],
        "age_seconds": 0,
    }


def from_legacy_regression(run_id: str, payload: dict, *, now: datetime | None = None) -> dict | None:
    """Map a pre-normalization regression side file during engine upgrades."""
    if not isinstance(payload, dict) or not payload.get("request_id") \
            or not payload.get("command_sha256"):
        return None
    finished = payload.get("finished_at")
    reference = _parse(finished or payload.get("heartbeat_at"))
    if reference is None:
        return None
    age = ((now or datetime.now(timezone.utc)) - reference).total_seconds()
    ceiling = TERMINAL_RETENTION_SECONDS if finished else STALE_SECONDS
    if age > ceiling:
        return None
    units = []
    for command in payload.get("commands") or []:
        for shard in command.get("shards") or []:
            if shard.get("finished_at"):
                unit_state = "timed_out" if shard.get("timed_out") \
                    else ("passed" if shard.get("exit_code") == 0 else "failed")
            else:
                unit_state = "running" if shard.get("started_at") else "queued"
            total = shard.get("test_count") if isinstance(shard.get("test_count"), int) else None
            done = max(int(shard.get("done") or 0), 0)
            units.append({
                "index": len(units) + 1,
                "label": _safe_text(shard.get("label")) or f"Worker {len(units) + 1}",
                "state": unit_state,
                "total": total,
                "done": done,
                "progress": min(done / total, 1.0) if total else None,
                "elapsed_seconds": None,
                "result": None if not shard.get("finished_at") else f"exit {shard.get('exit_code')}",
            })
    counts = aggregate_units(units)
    legacy = payload.get("totals") or {}
    counts.update({
        "total": legacy.get("total") if isinstance(legacy.get("total"), int) else None,
        "done": int(legacy.get("done") or 0),
        "passed_tests": int(legacy.get("passed") or 0),
        "failed_tests": int(legacy.get("failed") or 0),
        "error_tests": int(legacy.get("errors") or 0),
        "skipped_tests": int(legacy.get("skipped") or 0),
    })
    if finished:
        if payload.get("exit_code") == 0:
            counts["state"] = "passed"
        elif counts["state"] not in {"failed", "timed_out", "cancelled"}:
            counts["state"] = "failed"
    identity = command_sha256([
        str(payload.get("request_id")), str(payload.get("command_sha256")),
        str(payload.get("started_at")),
    ])
    return {
        "schema_version": 1,
        "run_id": run_id,
        "request_id": payload.get("request_id"),
        "command_sha256": payload.get("command_sha256"),
        "execution_id": f"legacy-{identity[:32]}",
        "source": "regression",
        "label": _safe_text(payload.get("label")) or "Regression",
        "state": counts["state"],
        "started_at": payload.get("started_at"),
        "heartbeat_at": payload.get("heartbeat_at") or payload.get("started_at"),
        "finished_at": finished,
        "result": None if not finished else f"exit {payload.get('exit_code')}",
        "totals": counts,
        "unit_count": len(units),
        "visible_unit_count": min(len(units), MAX_VISIBLE_UNITS),
        "units": units[:MAX_VISIBLE_UNITS],
        "age_seconds": max(round(age, 3), 0),
    }
