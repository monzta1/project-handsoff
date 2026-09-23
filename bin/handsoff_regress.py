#!/usr/bin/env python3
"""Run a regression battery and stream per-test progress for Mission Control.

    handsoff_regress.py --root R --group python-full
    handsoff_regress.py --root R --command "python3 -m unittest tests.test_x -v"

Progress is written atomically to `<root>/.handsoff-regression.json` after
every test line, so the dashboard's /regression page (and /api/regression)
can show a battery while it runs: totals, pass/fail/error/skip counts, the
test in flight, recent failures, elapsed time, and the final exit code. The
file is Handsoff side state (never part of the repository digest).

Supported unittest commands are collected once into a schema-versioned
inventory.  That exact inventory is the contract read by local, CI, release,
and dashboard callers and is divided into five deterministic, count-balanced
shards by default.  Unsupported collection shapes visibly fall back to one
serial worker; a broken collection never does.

unittest is run with -v so each test prints one line; node --test output
(`ok N - name` / `not ok N - name`) is parsed too. The test total is counted
up front by unittest discovery when the command is a `python3 -m unittest`
or `python3 tests/<module>.py` invocation, else it grows as tests report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402
import handsoff_progress as test_progress  # noqa: E402

PROGRESS_FILE = ".handsoff-regression.json"
INVENTORY_FILE = ".handsoff-regression-inventory.json"
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_VIEWS = ("local", "ci", "release", "dashboard")
UNITTEST_LINE = re.compile(r"^(?P<test>test\w*) \((?P<where>[\w.]+)\)(?: \[[^\]]*\])? \.\.\. (?P<result>ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
# A test with a docstring prints its name on one line and "<first docstring
# line> ... result" on the next.
UNITTEST_HEAD = re.compile(r"^(?P<test>test\w*) \((?P<where>[\w.]+)\)(?: \[[^\]]*\])?$")
UNITTEST_TAIL = re.compile(r"^.* \.\.\. (?P<result>ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
# A test that leaks child output to the terminal prints "name (where) ... "
# followed by the leaked text; unittest then writes the result token on a
# line of its own. Only an exact result line closes such a test, so a
# leaked line that merely ends in "ok" is never mistaken for a verdict.
UNITTEST_OPEN = re.compile(r"^(?P<test>test\w*) \((?P<where>[\w.]+)\)(?: \[[^\]]*\])? \.\.\. (?P<rest>.*)$")
UNITTEST_LATE = re.compile(r"^(?P<result>ok|FAIL|ERROR|skipped(?: .*)?|expected failure|unexpected success)$")
RESULT_KINDS = {"ok": "passed", "expected failure": "passed", "FAIL": "failed", "unexpected success": "failed",
                "ERROR": "errors"}


def _case_name(where: str, test: str) -> str:
    """Python 3.12 prints `test_a (pkg.Class.test_a)`, 3.11 `test_a (pkg.Class)`."""
    return where if where.endswith(f".{test}") or where == test else f"{where}.{test}"


def parse_line(text: str, pending_name: str | None) -> tuple[dict | None, str | None]:
    """Classify one runner line. Returns (event, pending_name): the event is
    {"name", "result"} for a finished unittest case (result is the raw
    unittest token) or {"name", "node": "ok"|"not ok"|"skip"} for a node
    test, else None. `pending_name` carries a unittest case whose result is
    still to come (docstring second line, or leaked child output)."""
    match = UNITTEST_LINE.match(text)
    if match:
        return {"name": _case_name(match.group("where"), match.group("test")), "result": match.group("result")}, None
    node = NODE_LINE.match(text)
    if node:
        kind = "skip" if node.group("directive") == "SKIP" else ("not ok" if node.group("not") else "ok")
        return {"name": node.group("name"), "node": kind}, pending_name
    head = UNITTEST_HEAD.match(text)
    if head:
        return None, _case_name(head.group("where"), head.group("test"))
    opened = UNITTEST_OPEN.match(text)
    if opened:
        name = _case_name(opened.group("where"), opened.group("test"))
        late = UNITTEST_LATE.match(opened.group("rest"))
        if late:
            return {"name": name, "result": late.group("result")}, None
        return None, name
    if pending_name:
        tail = UNITTEST_TAIL.match(text) or UNITTEST_LATE.match(text)
        if tail:
            return {"name": pending_name, "result": tail.group("result")}, None
    return None, pending_name


def count_event(entry: dict, event: dict) -> None:
    """Fold one parse_line event into a command entry."""
    entry["done"] += 1
    if "node" in event:
        kind = {"ok": "passed", "not ok": "failed", "skip": "skipped"}[event["node"]]
    else:
        kind = RESULT_KINDS.get(event["result"], "skipped")
    entry[kind] += 1
    if kind == "failed":
        entry["failures"].append({"name": event["name"], "kind": "FAIL"})
    elif kind == "errors":
        entry["failures"].append({"name": event["name"], "kind": "ERROR"})
    entry["failures"] = entry["failures"][-MAX_RECENT:]
    entry["current"] = event["name"]
NODE_LINE = re.compile(r"^(?P<not>not )?ok (?P<num>\d+) - (?P<name>.*?)(?: # (?P<directive>SKIP|TODO).*)?$")
MAX_RECENT = 64
DEFAULT_SHARDS = 5


class RegressionInventoryError(RuntimeError):
    """The collected inventory cannot safely describe an execution."""


class CollectionFailure(RegressionInventoryError):
    """A supported collector ran but failed or returned invalid results."""


class StaleRegressionExecution(RegressionInventoryError):
    """A retry superseded this execution while it was still publishing."""


def inventory_path(root: Path) -> Path:
    return Path(root) / INVENTORY_FILE


def _inventory_hash(test_ids: list[str]) -> str:
    raw = json.dumps(test_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def balanced_shards(test_ids: list[str], shard_count: int = DEFAULT_SHARDS) -> list[list[str]]:
    """Return deterministic shards whose test-count spread is at most one.

    Empty shards are intentional.  They make a five-worker plan auditable
    even when a small fixture contains fewer than five cases.
    """
    if shard_count < 1:
        raise RegressionInventoryError("shard_count must be at least 1")
    counts = Counter(test_ids)
    duplicates = sorted(name for name, count in counts.items() if count != 1)
    if duplicates:
        raise RegressionInventoryError(f"duplicate test id(s): {', '.join(duplicates)}")
    ordered = sorted(test_ids)
    # Contiguous count-balanced slices keep tests from the same module (and
    # usually the same TestCase) together. Round-robin-by-ID was numerically
    # balanced but forced nearly every module to start in every worker: the
    # first real 1,251-test run launched roughly 440 interpreters and hit the
    # 15-minute timeout. Contiguous boundaries preserve the <=1 count spread
    # while duplicating at most four boundary modules across five workers.
    base, remainder = divmod(len(ordered), shard_count)
    shards = []
    offset = 0
    for index in range(shard_count):
        size = base + (1 if index < remainder else 0)
        shards.append(ordered[offset:offset + size])
        offset += size
    return shards


def _exact_ids(expected: list[str], actual: list[str], context: str) -> None:
    duplicates = sorted(name for name, count in Counter(actual).items() if count > 1)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if not duplicates and not missing and not extra and len(expected) == len(actual):
        return
    details = []
    if duplicates:
        details.append(f"duplicate: {', '.join(duplicates)}")
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if extra:
        details.append(f"unexpected: {', '.join(extra)}")
    raise RegressionInventoryError(f"{context} inventory mismatch ({'; '.join(details)})")


def _inventory_identity(payload: dict) -> str:
    keys = (
        "schema_version", "revision", "group", "state", "commands", "test_count", "test_ids",
        "test_ids_sha256", "shard_count", "shards",
    )
    identity = {key: payload.get(key) for key in keys}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def make_inventory(label: str, commands: list[dict], *, shard_count: int = DEFAULT_SHARDS,
                   revision: str = "unknown") -> dict:
    """Build the one machine-readable inventory shared by every view."""
    normalized_commands = []
    for source in commands:
        item = dict(source)
        values = item.get("test_ids")
        if not isinstance(values, list):
            raise RegressionInventoryError("command inventory test_ids are invalid")
        _exact_ids(sorted(set(values)), values, f"command {item.get('command')!r}")
        if item.get("collection_state") == "collected" and item.get("test_count") != len(values):
            raise RegressionInventoryError("command inventory count mismatch")
        item["test_ids"] = sorted(values)
        normalized_commands.append(item)
    collected = [item for item in normalized_commands if item.get("collection_state") == "collected"]
    ambiguous = [item for item in normalized_commands if item.get("collection_state") == "ambiguous"]
    test_ids = [test_id for item in collected for test_id in item.get("test_ids", [])]
    _exact_ids(sorted(set(test_ids)), test_ids, f"regression group {label!r}")
    planned = balanced_shards(test_ids, shard_count) if test_ids and not ambiguous else []
    payload = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "revision": revision,
        "group": label,
        "state": "ambiguous" if ambiguous else "collected",
        "consumers": list(INVENTORY_VIEWS),
        "commands": normalized_commands,
        "test_count": None if ambiguous else len(test_ids),
        "test_ids": sorted(test_ids),
        "test_ids_sha256": _inventory_hash(sorted(test_ids)),
        "shard_count": 1 if ambiguous else shard_count,
        "shards": [
            {"index": index, "test_count": len(ids), "test_ids": ids}
            for index, ids in enumerate(planned, 1)
        ],
    }
    payload["inventory_id"] = _inventory_identity(payload)
    return validate_inventory(payload)


def validate_inventory(payload: dict) -> dict:
    """Fail closed on malformed versions, duplicates, omissions, or shards."""
    if not isinstance(payload, dict) or payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise RegressionInventoryError("unsupported regression inventory schema")
    consumers = payload.get("consumers")
    if consumers != list(INVENTORY_VIEWS):
        raise RegressionInventoryError("regression inventory consumers are incomplete")
    ids = payload.get("test_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        raise RegressionInventoryError("regression inventory test_ids are invalid")
    if ids != sorted(ids):
        raise RegressionInventoryError("regression inventory test_ids are not deterministic")
    _exact_ids(sorted(set(ids)), ids, "regression")
    if payload.get("test_ids_sha256") != _inventory_hash(ids):
        raise RegressionInventoryError("regression inventory digest mismatch")
    if not isinstance(payload.get("revision"), str) or not payload["revision"]:
        raise RegressionInventoryError("regression inventory revision is missing")
    commands = payload.get("commands")
    if not isinstance(commands, list):
        raise RegressionInventoryError("regression inventory commands are invalid")
    command_ids = []
    has_ambiguous = False
    for item in commands:
        if not isinstance(item, dict) or item.get("collection_state") not in {"collected", "ambiguous"}:
            raise RegressionInventoryError("regression command collection state is invalid")
        values = item.get("test_ids")
        if not isinstance(values, list) or values != sorted(values):
            raise RegressionInventoryError("regression command test_ids are invalid")
        _exact_ids(sorted(set(values)), values, f"command {item.get('command')!r}")
        if item["collection_state"] == "collected":
            if item.get("test_count") != len(values):
                raise RegressionInventoryError("regression command count mismatch")
            command_ids.extend(values)
        else:
            has_ambiguous = True
            if values or item.get("test_count") is not None or not item.get("fallback_reason"):
                raise RegressionInventoryError("ambiguous command inventory is invalid")
    _exact_ids(ids, command_ids, "regression commands")
    state = payload.get("state")
    if state == "ambiguous":
        if not has_ambiguous or payload.get("test_count") is not None \
                or payload.get("shard_count") != 1 or payload.get("shards"):
            raise RegressionInventoryError("ambiguous collection must use one serial fallback")
        if payload.get("inventory_id") != _inventory_identity(payload):
            raise RegressionInventoryError("regression inventory identity mismatch")
        return payload
    if state != "collected" or has_ambiguous or payload.get("test_count") != len(ids):
        raise RegressionInventoryError("regression inventory count mismatch")
    shard_count = payload.get("shard_count")
    shards = payload.get("shards")
    if not isinstance(shard_count, int) or shard_count < 1 or not isinstance(shards, list):
        raise RegressionInventoryError("regression inventory shard plan is invalid")
    indexes = [item.get("index") for item in shards if isinstance(item, dict)]
    if indexes != list(range(1, shard_count + 1)):
        raise RegressionInventoryError("regression inventory has a missing shard")
    shard_ids = []
    sizes = []
    for shard in shards:
        values = shard.get("test_ids")
        if not isinstance(values, list) or shard.get("test_count") != len(values):
            raise RegressionInventoryError(f"regression inventory shard {shard.get('index')} count mismatch")
        shard_ids.extend(values)
        sizes.append(len(values))
    _exact_ids(ids, shard_ids, "shard plan")
    if sizes and max(sizes) - min(sizes) > 1:
        raise RegressionInventoryError("regression inventory shard count spread exceeds one")
    if payload.get("inventory_id") != _inventory_identity(payload):
        raise RegressionInventoryError("regression inventory identity mismatch")
    return payload


def read_inventory(root_or_path: Path, *, view: str) -> dict:
    """Independently read and validate the inventory for one named consumer."""
    if view not in INVENTORY_VIEWS:
        raise RegressionInventoryError(f"unknown regression inventory view {view!r}")
    path = Path(root_or_path)
    if path.is_dir():
        path = inventory_path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegressionInventoryError(f"{view} regression inventory is unavailable: {exc}") from exc
    payload = validate_inventory(payload)
    if view not in payload["consumers"]:
        raise RegressionInventoryError(f"{view} is absent from regression inventory consumers")
    return payload


def require_equal_inventory(reference: dict, candidate: dict, *, reference_view: str,
                            candidate_view: str, exclusions: list[dict] | None = None) -> None:
    """Require exact view equality, allowing only explicit audited exclusions."""
    left = validate_inventory(reference)
    right = validate_inventory(candidate)
    if left["revision"] != right["revision"]:
        raise RegressionInventoryError(
            f"{candidate_view} revision {right['revision']} does not match "
            f"{reference_view} revision {left['revision']}"
        )
    if left["group"] != right["group"]:
        raise RegressionInventoryError(
            f"{candidate_view} group {right['group']!r} does not match {reference_view} group {left['group']!r}"
        )
    left_ids = list(left["test_ids"])
    right_ids = list(right["test_ids"])
    allowed_missing: set[str] = set()
    for item in exclusions or []:
        if not isinstance(item, dict) or item.get("view") != candidate_view:
            continue
        if not all(isinstance(item.get(key), str) and item[key].strip()
                   for key in ("test_id", "platform", "reason", "audit_id")):
            raise RegressionInventoryError("platform exclusion is not auditable")
        allowed_missing.add(item["test_id"])
    missing = sorted(set(left_ids) - set(right_ids) - allowed_missing)
    extra = sorted(set(right_ids) - set(left_ids))
    duplicates = sorted(name for name, count in Counter(right_ids).items() if count > 1)
    if missing or extra or duplicates:
        detail = []
        if missing:
            detail.append(f"missing from {candidate_view}: {', '.join(missing)}")
        if extra:
            detail.append(f"absent from {reference_view}: {', '.join(extra)}")
        if duplicates:
            detail.append(f"duplicate in {candidate_view}: {', '.join(duplicates)}")
        raise RegressionInventoryError("; ".join(detail))


def progress_path(root: Path) -> Path:
    return Path(root) / PROGRESS_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(root: Path, state: dict) -> None:
    # Publish through the identity-fenced normalized file first.  A retry may
    # have replaced this execution; in that case this writer must not restore
    # stale legacy state or manufacture a terminal success.
    if not _publish_normalized(root, state):
        raise StaleRegressionExecution(
            f"regression execution {state.get('execution_id')!r} was superseded"
        )
    if isinstance(state.get("inventory"), dict):
        lib.atomic_write_json(inventory_path(root), state["inventory"])
    lib.atomic_write_json(progress_path(root), state)


def _publish_normalized(root: Path, state: dict) -> bool:
    execution_id = state.get("execution_id")
    if not isinstance(execution_id, str):
        return True
    snapshot = test_progress.read(root, execution_id=execution_id)
    if snapshot is None:
        return False
    units = []
    for command in state.get("commands") or []:
        for shard in command.get("shards") or []:
            if shard.get("finished_at"):
                code = shard.get("exit_code")
                unit_state = "timed_out" if shard.get("timed_out") else ("passed" if code == 0 else "failed")
            elif shard.get("started_at"):
                unit_state = "running"
            else:
                unit_state = "queued"
            total = shard.get("test_count") if isinstance(shard.get("test_count"), int) else None
            done = max(int(shard.get("done") or 0), 0)
            try:
                began = datetime.fromisoformat(str(shard.get("started_at")).replace("Z", "+00:00"))
                ended = datetime.fromisoformat(str(shard.get("finished_at") or _now()).replace("Z", "+00:00"))
                elapsed = max(round((ended - began).total_seconds(), 3), 0)
            except (TypeError, ValueError):
                elapsed = None
            units.append({
                "index": len(units) + 1,
                "label": str(shard.get("label") or f"Worker {len(units) + 1}")[:240],
                "state": unit_state,
                "total": total,
                "done": done,
                "progress": min(done / total, 1.0) if total else None,
                "elapsed_seconds": elapsed,
                "result": None if not shard.get("finished_at") else f"exit {shard.get('exit_code')}",
            })
    planned = list(state.get("planned_commands") or [])
    for command in planned[len(state.get("commands") or []):]:
        units.append({"index": len(units) + 1, "label": str(command)[:240], "state": "queued",
                      "total": None, "done": 0, "progress": None,
                      "elapsed_seconds": 0.0, "result": None})
    counts = test_progress.aggregate_units(units)
    legacy = state.get("totals") or {}
    counts.update({
        "total": legacy.get("total") if len(state.get("commands") or []) == len(planned) else None,
        "done": int(legacy.get("done") or 0),
        "passed_tests": int(legacy.get("passed") or 0),
        "failed_tests": int(legacy.get("failed") or 0),
        "error_tests": int(legacy.get("errors") or 0),
        "skipped_tests": int(legacy.get("skipped") or 0),
    })
    if state.get("finished_at"):
        counts["state"] = "timed_out" if any(u["state"] == "timed_out" for u in units) \
            else ("failed" if state.get("exit_code") else "passed")
    else:
        # A failed shard is terminal for that worker, not for the execution:
        # the remaining shards continue and retain ownership of this snapshot.
        counts["state"] = "running" if state.get("commands") else "queued"
    snapshot["state"] = counts["state"]
    snapshot["totals"] = counts
    snapshot["unit_count"] = len(units)
    snapshot["units"] = units[:test_progress.MAX_VISIBLE_UNITS]
    if isinstance(state.get("inventory"), dict):
        snapshot["inventory"] = state["inventory"]
        snapshot["inventory_id"] = state["inventory"].get("inventory_id")
        snapshot["inventory_test_count"] = state["inventory"].get("test_count")
    if state.get("finished_at"):
        snapshot["finished_at"] = state["finished_at"]
        snapshot["result"] = f"exit {state.get('exit_code')}"
    return test_progress.write(root, snapshot, expected_execution_id=execution_id)


def _unittest_names(argv: list[str]) -> list[str]:
    """Return unittest load names only for the two command shapes we can shard."""
    is_python = bool(argv) and Path(argv[0]).name.startswith("python")
    if len(argv) >= 3 and is_python and argv[1:3] == ["-m", "unittest"]:
        return [arg for arg in argv[3:] if not arg.startswith("-")]
    if len(argv) >= 2 and is_python and argv[1].startswith("tests/") \
            and argv[1].endswith(".py"):
        return [argv[1][:-3].replace("/", ".")]
    return []


def _all_tests_command(argv: list[str]) -> bool:
    return len(argv) >= 3 and Path(argv[0]).name.startswith("python") \
        and Path(argv[1]).as_posix().endswith("tests/shard.py") and "--all" in argv[2:]


def _collect_all_test_ids(root: Path, argv: list[str] | None = None) -> list[str]:
    """Read tests/shard.py's complete, module-isolated unittest inventory."""
    script = (argv or [sys.executable, "tests/shard.py"])[1]
    python = (argv or [sys.executable])[0]
    result = subprocess.run(
        [python, script, "--all", "--inventory"], cwd=str(root), capture_output=True,
        text=True, timeout=300, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise CollectionFailure(
            f"complete unittest collection failed: {detail[-1][:240] if detail else 'no output'}"
        )
    marker = "HANDSOFF_INVENTORY_JSON="
    line = next((item for item in reversed(result.stdout.splitlines()) if item.startswith(marker)), None)
    if line is None:
        raise CollectionFailure("complete unittest collection returned no inventory")
    try:
        payload = json.loads(line[len(marker):])
    except json.JSONDecodeError as exc:
        raise CollectionFailure("complete unittest collection returned invalid JSON") from exc
    ids = payload.get("test_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
        raise CollectionFailure("complete unittest collection returned invalid test identifiers")
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise CollectionFailure(f"complete unittest collection returned duplicate test id(s): {', '.join(duplicates)}")
    return sorted(ids)


def _discover_complete_test_ids(root: Path) -> list[str]:
    """Collect the CI unittest universe, one clean process per module."""
    helper = Path(root) / "tests" / "shard.py"
    if helper.is_file():
        return _collect_all_test_ids(root, [sys.executable, "tests/shard.py", "--all"])
    excluded = {"test_generic_dropin"}
    ids = []
    for path in sorted((Path(root) / "tests").glob("test_*.py")):
        if path.stem in excluded:
            continue
        module_ids, reason = _enumerate_unittest_ids(
            root, [sys.executable, "-m", "unittest", f"tests.{path.stem}"]
        )
        if module_ids is None:
            raise CollectionFailure(f"collection is ambiguous for tests.{path.stem}: {reason}")
        ids.extend(module_ids)
    _exact_ids(sorted(set(ids)), ids, "complete unittest collection")
    if not ids:
        raise CollectionFailure("complete unittest collection returned no tests")
    return sorted(ids)


def _enumerate_unittest_ids(root: Path, argv: list[str]) -> tuple[list[str] | None, str | None]:
    """Enumerate exact IDs in a clean process.

    ``(None, reason)`` is reserved for a command shape this collector cannot
    interpret safely; callers may expose that as a degraded serial fallback.
    Once a supported collector starts, every error is a hard failure.
    """
    try:
        if _all_tests_command(argv):
            return _collect_all_test_ids(root, argv), None
        names = _unittest_names(argv)
        if not names:
            return None, "collection is ambiguous for this command shape"
        code = (
            "import json, sys, unittest; sys.path.insert(0, '.'); "
            "loader=unittest.TestLoader(); suites=[loader.loadTestsFromName(n) for n in sys.argv[1:]]; "
            "walk=lambda s: [t for x in s for t in (walk(x) if isinstance(x, unittest.TestSuite) else [x])]; "
            "tests=[t for s in suites for t in walk(s)]; "
            "bad=[t.id() for t in tests if t.__class__.__name__ == '_FailedTest']; "
            "ids=[t.id() for t in tests]; "
            "print('HANDSOFF_INVENTORY_JSON='+json.dumps({'ids': ids, 'bad': bad}))"
        )
        out = subprocess.run([argv[0], "-c", code, *names], cwd=str(root), capture_output=True,
                             text=True, timeout=120, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"})
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip().splitlines()
            suffix = f": {detail[-1][:240]}" if detail else ""
            raise CollectionFailure(
                f"collection of {', '.join(names)} exited {out.returncode}{suffix}"
            )
        line = next((item for item in reversed(out.stdout.splitlines())
                     if item.startswith("HANDSOFF_INVENTORY_JSON=")), None)
        if line is None:
            raise CollectionFailure("collection returned no inventory")
        payload = json.loads(line.split("=", 1)[1])
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if payload.get("bad"):
            raise CollectionFailure(f"collection failed: {', '.join(payload['bad'])}")
        if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
            raise CollectionFailure("collection returned invalid test identifiers")
        if not ids:
            raise CollectionFailure("collection returned no tests")
        duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise CollectionFailure(f"collection returned duplicate test id(s): {', '.join(duplicates)}")
        return sorted(ids), None
    except CollectionFailure:
        raise
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise CollectionFailure(f"collection failed: {type(exc).__name__}: {exc}") from exc


def _count_unittest_total(root: Path, argv: list[str]) -> int | None:
    try:
        ids, _ = _enumerate_unittest_ids(root, argv)
    except CollectionFailure:
        return None
    return len(ids) if ids is not None else None


def _verbose_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "unittest" and "-v" not in argv and "--verbose" not in argv:
        argv.append("-v")
    elif len(argv) >= 2 and argv[0].startswith("python") and argv[1].startswith("tests/") and argv[1].endswith(".py") \
            and "-v" not in argv:
        argv.append("-v")
    return argv


def collect_inventory(root: Path, label: str, commands: list[str], *,
                      shard_count: int = DEFAULT_SHARDS) -> dict:
    """Collect each command once and return the shared schema-v1 inventory."""
    command_inventories = []
    for command in commands:
        argv = _verbose_argv(command)
        ids, reason = _enumerate_unittest_ids(root, argv)
        if ids is None:
            command_inventories.append({
                "command": command,
                "collection_state": "ambiguous",
                "fallback_reason": reason or "collection is ambiguous",
                "test_count": None,
                "test_ids": [],
            })
        else:
            command_inventories.append({
                "command": command,
                "collection_state": "collected",
                "fallback_reason": None,
                "test_count": len(ids),
                "test_ids": ids,
            })
    try:
        revision_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
        )
        revision = revision_result.stdout.strip() if revision_result.returncode == 0 else "unversioned"
    except OSError:
        revision = "unversioned"
    inventory = make_inventory(
        label, command_inventories, shard_count=shard_count, revision=revision,
    )
    if label == "python-full" and inventory["state"] == "collected" \
            and not any(_all_tests_command(_verbose_argv(command)) for command in commands):
        complete_ids = _discover_complete_test_ids(root)
        _exact_ids(complete_ids, inventory["test_ids"], "python-full local/CI/release")
    return inventory


def _recount_entry(entry: dict) -> None:
    for key in ("done", "passed", "failed", "errors", "skipped"):
        entry[key] = sum(int(shard.get(key, 0) or 0) for shard in entry["shards"])
    failures = []
    for shard in sorted(entry["shards"], key=lambda item: item["index"]):
        failures.extend({**failure, "shard": shard["index"]} for failure in shard.get("failures", []))
    entry["failures"] = failures[-MAX_RECENT:]
    active = [shard for shard in entry["shards"] if not shard.get("finished_at") and shard.get("current")]
    entry["current"] = active[0]["current"] if active else None


def _new_shard(index: int, test_ids: list[str] | None) -> dict:
    return {"index": index, "label": f"Worker {index}",
            "test_count": len(test_ids) if test_ids is not None else None,
            "test_ids": list(test_ids) if test_ids is not None else None,
            "completed_test_ids": [], "inventory_error": None,
            "started_at": None, "finished_at": None, "exit_code": None, "timed_out": False,
            "done": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "current": None, "failures": [], "log": None, "output_sha256": None, "output_bytes": 0}


def _run_worker(root: Path, run_spec, *, shell: bool, state: dict, entry: dict, shard: dict,
                timeout: int, command_index: int, lock: threading.Lock) -> None:
    log_path = Path(tempfile.gettempdir()) / (
        f"handsoff-regress-{os.getpid()}-{command_index}-{shard['index']}.log"
    )
    env = {**os.environ, "PYTHONUNBUFFERED": "1",
           "HANDSOFF_SKIP_PREFLIGHT": os.environ.get("HANDSOFF_SKIP_PREFLIGHT", "1")}
    timed_out = threading.Event()
    process = None

    def expire() -> None:
        if process is None or process.poll() is not None:
            return
        timed_out.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    shard["started_at"] = _now()
    try:
        process = subprocess.Popen(run_spec, shell=shell, cwd=str(root), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        with log_path.open("w", encoding="utf-8") as log, process.stdout:
            assert process.stdout is not None
            pending_name = None
            for line in process.stdout:
                log.write(line)
                event, pending_name = parse_line(line.rstrip("\n"), pending_name)
                if event is None:
                    continue
                with lock:
                    shard["completed_test_ids"].append(event["name"])
                    count_event(shard, event)
                    _recount_entry(entry)
                    state["totals"] = _totals(state)
                    _write(root, state)
        code = process.wait()
        timer.cancel()
        code = 124 if timed_out.is_set() else code
    except OSError as exc:
        code = 127
        log_path.write_text(f"HANDSOFF: worker launch failed: {exc}\n", encoding="utf-8")
    raw = log_path.read_bytes() if log_path.is_file() else b""
    with lock:
        if shard.get("test_ids") is not None:
            try:
                _exact_ids(shard["test_ids"], shard["completed_test_ids"],
                           f"shard {shard['index']}")
            except RegressionInventoryError as exc:
                shard["inventory_error"] = str(exc)
                shard["failures"].append({"name": "regression inventory", "kind": "INVENTORY"})
                code = code or 2
        shard["finished_at"] = _now()
        shard["exit_code"] = code
        shard["timed_out"] = code == 124
        shard["current"] = None
        shard["log"] = str(log_path)
        shard["output_sha256"] = hashlib.sha256(raw).hexdigest()
        shard["output_bytes"] = len(raw)
        _recount_entry(entry)
        state["totals"] = _totals(state)
        _write(root, state)


def _complete_empty_shard(root: Path, *, state: dict, entry: dict, shard: dict,
                          command_index: int, lock: threading.Lock) -> None:
    """Record an explicit zero-test worker without invoking unittest discover."""
    log_path = Path(tempfile.gettempdir()) / (
        f"handsoff-regress-{os.getpid()}-{command_index}-{shard['index']}.log"
    )
    log_path.write_text("HANDSOFF: empty inventory shard\n", encoding="utf-8")
    raw = log_path.read_bytes()
    with lock:
        shard.update({
            "started_at": _now(), "finished_at": _now(), "exit_code": 0,
            "log": str(log_path), "output_sha256": hashlib.sha256(raw).hexdigest(),
            "output_bytes": len(raw),
        })
        _recount_entry(entry)
        state["totals"] = _totals(state)
        _write(root, state)


def _validate_completed_shards(entry: dict, expected_ids: list[str], expected_count: int) -> None:
    shards = entry.get("shards") or []
    indexes = [shard.get("index") for shard in shards]
    if indexes != list(range(1, expected_count + 1)):
        raise RegressionInventoryError("completed regression has a missing shard")
    if any(not shard.get("finished_at") for shard in shards):
        raise RegressionInventoryError("completed regression has an unfinished shard")
    observed = [test_id for shard in shards for test_id in shard.get("completed_test_ids", [])]
    _exact_ids(expected_ids, observed, "completed regression")


def _merged_result(command: str, entry: dict, started: float) -> dict:
    chunks = []
    summaries = []
    ordered = sorted(entry["shards"], key=lambda item: item["index"])
    for shard in ordered:
        header = f"\n===== shard {shard['index']} of {len(ordered)} =====\n".encode()
        try:
            raw = Path(shard["log"]).read_bytes()
        except (OSError, TypeError):
            raw = b""
        chunks.extend((header, raw))
        summaries.append({key: shard.get(key) for key in (
            "index", "label", "test_count", "test_ids", "completed_test_ids", "inventory_error",
            "done", "passed", "failed", "errors", "skipped", "exit_code", "timed_out",
            "output_sha256", "output_bytes"
        )})
    merged = b"".join(chunks)
    decoded = merged.decode("utf-8", "replace")
    exit_code = 124 if any(shard.get("timed_out") for shard in ordered) else next(
        (int(shard["exit_code"]) for shard in ordered if shard.get("exit_code") != 0), 0
    )
    return {"command": command, "exit_code": exit_code, "output_sha256": hashlib.sha256(merged).hexdigest(),
            "duration_s": round(time.monotonic() - started, 2), "timed_out": exit_code == 124,
            "truncated": len(decoded) > lib.CHECK_OUTPUT_TAIL_CHARS, "output_bytes": len(merged),
            "output_tail": decoded[-lib.CHECK_OUTPUT_TAIL_CHARS:], "shards": summaries}


def run_command(root: Path, command: str, state: dict, *, timeout: int, command_index: int = 1,
                max_shards: int = DEFAULT_SHARDS, command_inventory: dict | None = None) -> dict:
    started = time.monotonic()
    argv = _verbose_argv(command)
    all_tests = _all_tests_command(argv)
    eligible = bool(_unittest_names(argv)) or all_tests
    if command_inventory is None:
        ids, enumeration_error = _enumerate_unittest_ids(root, argv)
    elif command_inventory.get("collection_state") == "collected":
        ids = list(command_inventory.get("test_ids") or [])
        enumeration_error = None
    else:
        ids = None
        enumeration_error = command_inventory.get("fallback_reason") or "collection is ambiguous"
    use_shards = ids is not None and max_shards > 1
    partitions = balanced_shards(ids, max_shards) if use_shards else []
    entry = {"command": command, "started_at": _now(), "finished_at": None, "exit_code": None,
             "total": len(ids) if ids is not None else None, "done": 0, "passed": 0, "failed": 0,
             "errors": 0, "skipped": 0, "current": None, "failures": [], "shards": [],
             "mode": "sharded" if use_shards else "sequential",
             "degraded": ids is None,
             "collection_state": "collected" if ids is not None else "ambiguous",
             "test_ids": ids,
             "test_ids_sha256": _inventory_hash(ids or []),
             "fallback_reason": enumeration_error if ids is None else None}
    specs = []
    if use_shards:
        for index, partition in enumerate(partitions, 1):
            shard = _new_shard(index, partition)
            entry["shards"].append(shard)
            if partition:
                if all_tests:
                    specs.append(([
                        argv[0], argv[1], "--all", "--total", str(max_shards),
                        "--index", str(index - 1), "--run-test-ids", *partition,
                    ], False, shard))
                else:
                    specs.append(([argv[0], "-m", "unittest", "-v", *partition], False, shard))
    else:
        shard = _new_shard(1, ids)
        entry["shards"].append(shard)
        if all_tests and ids is not None:
            specs.append(([argv[0], argv[1], "--all", "--total", "1", "--index", "0",
                           "--run-test-ids", *ids], False, shard))
        else:
            sequential = shlex.join(argv) if eligible else command
            specs.append((sequential, True, shard))
    state["commands"].append(entry)
    state["current_command"] = command
    _write(root, state)
    lock = threading.Lock()
    for shard in entry["shards"]:
        if shard.get("test_ids") == []:
            _complete_empty_shard(root, state=state, entry=entry, shard=shard,
                                  command_index=command_index, lock=lock)
    if specs:
        with ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix="handsoff-regression") as pool:
            futures = [pool.submit(_run_worker, root, spec, shell=shell, state=state, entry=entry, shard=shard,
                                   timeout=timeout, command_index=command_index, lock=lock)
                       for spec, shell, shard in specs]
            for future in futures:
                future.result()
    if ids is not None:
        try:
            _validate_completed_shards(entry, ids, max_shards if use_shards else 1)
        except RegressionInventoryError as exc:
            entry["inventory_error"] = str(exc)
            # Keep all worker outcomes, but make the command terminal failure.
            if entry["shards"]:
                entry["shards"][0]["exit_code"] = entry["shards"][0].get("exit_code") or 2
                entry["shards"][0]["inventory_error"] = str(exc)
    result = _merged_result(command, entry, started)
    entry["finished_at"] = _now()
    entry["exit_code"] = result["exit_code"]
    entry["current"] = None
    state["totals"] = _totals(state)
    _write(root, state)
    return result


def _totals(state: dict) -> dict:
    keys = ("total", "done", "passed", "failed", "errors", "skipped")
    totals = {key: 0 for key in keys}
    for entry in state["commands"]:
        for key in keys:
            value = entry.get(key)
            if key == "total" and (value is None or totals["total"] is None):
                totals["total"] = None
                continue
            totals[key] = (totals[key] or 0) + (value or 0)
    return totals


def run_battery_results(root: Path, label: str, commands: list[str], *, timeout: int,
                        request_id: str | None = None, command_sha256: str | None = None,
                        max_shards: int = DEFAULT_SHARDS) -> list[dict]:
    try:
        cfg = lib.load_config(root)
        run_id = lib.feature_hash(lib.load_unique_json(lib.status_path(root, cfg)), lib.read_events(root, cfg))
    except (lib.HandsoffError, OSError):
        run_id = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    approved_hash = command_sha256 or lib.command_sha256(commands)
    normalized = test_progress.start(
        root, run_id=run_id, source="regression", label=label,
        units=[f"Command {index}" for index in range(1, len(commands) + 1)],
        request_id=request_id, command_hash=approved_hash,
    )
    state = {"label": label, "started_at": _now(), "finished_at": None, "exit_code": None,
             "commands": [], "current_command": None, "totals": {}, "root": str(root),
             "request_id": request_id, "command_sha256": approved_hash,
             "execution_id": normalized["execution_id"], "run_id": run_id,
             "planned_commands": list(commands), "worker_limit": max_shards,
             "inventory": None, "collection_error": None}
    heartbeat_stop = threading.Event()

    def keep_alive() -> None:
        while not heartbeat_stop.wait(test_progress.HEARTBEAT_SECONDS):
            test_progress.heartbeat(root, normalized["execution_id"])

    heartbeat_thread = threading.Thread(target=keep_alive, name="handsoff-regression-heartbeat", daemon=True)
    heartbeat_thread.start()
    _write(root, state)
    try:
        try:
            state["inventory"] = collect_inventory(root, label, commands, shard_count=max_shards)
            _write(root, state)
        except RegressionInventoryError as exc:
            state["collection_error"] = str(exc)
            state["finished_at"] = _now()
            state["exit_code"] = 2
            state["current_command"] = None
            state["totals"] = {"total": None, "done": 0, "passed": 0, "failed": 0,
                               "errors": 1, "skipped": 0}
            _write(root, state)
            return [{"command": "inventory collection", "exit_code": 2, "timed_out": False,
                     "duration_s": 0.0, "truncated": False, "output_bytes": 0,
                     "output_sha256": hashlib.sha256(b"").hexdigest(),
                     "output_tail": str(exc), "shards": []}]
        command_plans = state["inventory"]["commands"]
        results = [run_command(root, command, state, timeout=timeout, command_index=index,
                               max_shards=max_shards, command_inventory=command_plans[index - 1])
                   for index, command in enumerate(commands, 1)]
        worst = next((result["exit_code"] for result in results if result["exit_code"] != 0), 0)
        state["finished_at"] = _now()
        state["exit_code"] = worst
        state["current_command"] = None
        _write(root, state)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    return results


def run_battery(root: Path, label: str, commands: list[str], *, timeout: int,
                max_shards: int = DEFAULT_SHARDS) -> int:
    results = run_battery_results(root, label, commands, timeout=timeout, max_shards=max_shards,
                                  command_sha256=lib.command_sha256(commands))
    return next((result["exit_code"] for result in results if result["exit_code"] != 0), 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a regression battery with live progress for Mission Control")
    parser.add_argument("--root", default=None)
    parser.add_argument("--group", default=None, help="a [[regressions]] group name from handsoff.toml")
    parser.add_argument("--command", action="append", default=[], help="an explicit command (repeatable)")
    parser.add_argument("--timeout", type=int, default=3600, help="per-command timeout in seconds")
    parser.add_argument("--shards", type=int, default=DEFAULT_SHARDS,
                        help=f"maximum Python unittest workers (default: {DEFAULT_SHARDS})")
    parser.add_argument("--inventory-only", action="store_true",
                        help="collect, persist, and print the shared inventory without running tests")
    parser.add_argument("--inventory-view", choices=INVENTORY_VIEWS, default="local",
                        help="consumer identity for --inventory-only (default: local)")
    args = parser.parse_args()
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    commands = list(args.command)
    label = "ad hoc"
    if args.group:
        group = next((g for g in cfg.get("regressions", []) if g.get("name") == args.group), None)
        if group is None:
            print(f"HANDSOFF_REGRESS_BLOCKED: no [[regressions]] group named {args.group!r}", file=sys.stderr)
            return 2
        commands = list(group.get("commands", [])) + commands
        label = args.group
    if not commands:
        print("HANDSOFF_REGRESS_BLOCKED: give --group or at least one --command", file=sys.stderr)
        return 2
    print(f"HANDSOFF_REGRESS_STARTED: {label} ({len(commands)} command(s)); progress in {progress_path(root)}")
    if not 1 <= args.shards <= 16:
        print("HANDSOFF_REGRESS_BLOCKED: --shards must be from 1 to 16", file=sys.stderr)
        return 2
    if args.inventory_only:
        try:
            inventory = collect_inventory(root, label, commands, shard_count=args.shards)
            lib.atomic_write_json(inventory_path(root), inventory)
            # Read through the requested consumer path; do not trust the value
            # merely because this process just wrote it.
            print(json.dumps(read_inventory(root, view=args.inventory_view), sort_keys=True))
            return 0
        except RegressionInventoryError as exc:
            print(f"HANDSOFF_REGRESS_BLOCKED: {exc}", file=sys.stderr)
            return 2
    code = run_battery(root, label, commands, timeout=args.timeout, max_shards=args.shards)
    state = json.loads(progress_path(root).read_text(encoding="utf-8"))
    totals = state["totals"]
    print(f"HANDSOFF_REGRESS_{'OK' if code == 0 else 'FAILED'}: {totals.get('passed', 0)} passed, "
          f"{totals.get('failed', 0)} failed, {totals.get('errors', 0)} errors, {totals.get('skipped', 0)} skipped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
