#!/usr/bin/env python3
"""Run a regression battery and stream per-test progress for Mission Control.

    handsoff_regress.py --root R --group python-full
    handsoff_regress.py --root R --command "python3 -m unittest tests.test_x -v"

Progress is written atomically to `<root>/.handsoff-regression.json` after
every test line, so the dashboard's /regression page (and /api/regression)
can show a battery while it runs: totals, pass/fail/error/skip counts, the
test in flight, recent failures, elapsed time, and the final exit code. The
file is Handsoff side state (never part of the repository digest).

unittest is run with -v so each test prints one line; node --test output
(`ok N - name` / `not ok N - name`) is parsed too. The test total is counted
up front by unittest discovery when the command is a `python3 -m unittest`
or `python3 tests/<module>.py` invocation, else it grows as tests report.
"""
from __future__ import annotations

import argparse
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


def progress_path(root: Path) -> Path:
    return Path(root) / PROGRESS_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(root: Path, state: dict) -> None:
    lib.atomic_write_json(progress_path(root), state)
    _publish_normalized(root, state)


def _publish_normalized(root: Path, state: dict) -> None:
    execution_id = state.get("execution_id")
    if not isinstance(execution_id, str):
        return
    snapshot = test_progress.read(root, execution_id=execution_id)
    if snapshot is None:
        return
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
    snapshot["state"] = counts["state"]
    snapshot["totals"] = counts
    snapshot["unit_count"] = len(units)
    snapshot["units"] = units[:test_progress.MAX_VISIBLE_UNITS]
    if state.get("finished_at"):
        snapshot["finished_at"] = state["finished_at"]
        snapshot["result"] = f"exit {state.get('exit_code')}"
    test_progress.write(root, snapshot, expected_execution_id=execution_id)


def _unittest_names(argv: list[str]) -> list[str]:
    """Return unittest load names only for the two command shapes we can shard."""
    if len(argv) >= 3 and argv[0].startswith("python") and argv[1:3] == ["-m", "unittest"]:
        return [arg for arg in argv[3:] if not arg.startswith("-")]
    if len(argv) >= 2 and argv[0].startswith("python") and argv[1].startswith("tests/") \
            and argv[1].endswith(".py"):
        return [argv[1][:-3].replace("/", ".")]
    return []


def _enumerate_unittest_ids(root: Path, argv: list[str]) -> tuple[list[str] | None, str | None]:
    """Enumerate exact IDs in a clean process, or explain why sharding is unsafe.

    Any import/discovery ambiguity fails closed to the approved command's
    sequential path. A duplicate ID is also unsafe: a shard plan must prove
    every discovered case appears exactly once.
    """
    try:
        names = _unittest_names(argv)
        if not names:
            return None, None
        code = (
            "import json, sys, unittest; sys.path.insert(0, '.'); "
            "loader=unittest.TestLoader(); suites=[loader.loadTestsFromName(n) for n in sys.argv[1:]]; "
            "walk=lambda s: [t for x in s for t in (walk(x) if isinstance(x, unittest.TestSuite) else [x])]; "
            "tests=[t for s in suites for t in walk(s)]; "
            "bad=[t.id() for t in tests if t.__class__.__name__ == '_FailedTest']; "
            "ids=[t.id() for t in tests]; "
            "print(json.dumps({'ok': not bad and bool(ids) and len(ids)==len(set(ids)), "
            "'ids': ids, 'bad': bad}))"
        )
        out = subprocess.run([argv[0], "-c", code, *names], cwd=str(root), capture_output=True,
                             text=True, timeout=120, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"})
        if out.returncode != 0:
            return None, f"enumeration exited {out.returncode}"
        payload = json.loads(out.stdout)
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if not payload.get("ok") or not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
            reason = "discovery import error" if payload.get("bad") else "empty or duplicate test inventory"
            return None, reason
        return sorted(ids), None
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return None, f"enumeration failed: {type(exc).__name__}"


def _count_unittest_total(root: Path, argv: list[str]) -> int | None:
    ids, _ = _enumerate_unittest_ids(root, argv)
    return len(ids) if ids is not None else None


def _verbose_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "unittest" and "-v" not in argv and "--verbose" not in argv:
        argv.append("-v")
    elif len(argv) >= 2 and argv[0].startswith("python") and argv[1].startswith("tests/") and argv[1].endswith(".py") \
            and "-v" not in argv:
        argv.append("-v")
    return argv


def _recount_entry(entry: dict) -> None:
    for key in ("done", "passed", "failed", "errors", "skipped"):
        entry[key] = sum(int(shard.get(key, 0) or 0) for shard in entry["shards"])
    failures = []
    for shard in sorted(entry["shards"], key=lambda item: item["index"]):
        failures.extend({**failure, "shard": shard["index"]} for failure in shard.get("failures", []))
    entry["failures"] = failures[-MAX_RECENT:]
    active = [shard for shard in entry["shards"] if not shard.get("finished_at") and shard.get("current")]
    entry["current"] = active[0]["current"] if active else None


def _new_shard(index: int, test_count: int | None) -> dict:
    return {"index": index, "label": f"Worker {index}", "test_count": test_count,
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
            "index", "label", "test_count", "done", "passed", "failed", "errors", "skipped",
            "exit_code", "timed_out", "output_sha256", "output_bytes"
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
                max_shards: int = DEFAULT_SHARDS) -> dict:
    started = time.monotonic()
    argv = _verbose_argv(command)
    eligible = bool(_unittest_names(argv))
    ids, enumeration_error = _enumerate_unittest_ids(root, argv) if eligible else (None, None)
    use_shards = ids is not None and len(ids) > 1 and max_shards > 1
    partitions = []
    if use_shards:
        worker_count = min(max_shards, len(ids))
        partitions = [ids[index::worker_count] for index in range(worker_count)]
    entry = {"command": command, "started_at": _now(), "finished_at": None, "exit_code": None,
             "total": len(ids) if ids is not None else None, "done": 0, "passed": 0, "failed": 0,
             "errors": 0, "skipped": 0, "current": None, "failures": [], "shards": [],
             "mode": "sharded" if use_shards else "sequential",
             "fallback_reason": enumeration_error if eligible and ids is None else None}
    specs = []
    if use_shards:
        for index, partition in enumerate(partitions, 1):
            shard = _new_shard(index, len(partition))
            entry["shards"].append(shard)
            specs.append(([argv[0], "-m", "unittest", "-v", *partition], False, shard))
    else:
        shard = _new_shard(1, len(ids) if ids is not None else None)
        entry["shards"].append(shard)
        sequential = shlex.join(argv) if eligible else command
        specs.append((sequential, True, shard))
    state["commands"].append(entry)
    state["current_command"] = command
    _write(root, state)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix="handsoff-regression") as pool:
        futures = [pool.submit(_run_worker, root, spec, shell=shell, state=state, entry=entry, shard=shard,
                               timeout=timeout, command_index=command_index, lock=lock)
                   for spec, shell, shard in specs]
        for future in futures:
            future.result()
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
             "planned_commands": list(commands), "worker_limit": max_shards}
    heartbeat_stop = threading.Event()

    def keep_alive() -> None:
        while not heartbeat_stop.wait(test_progress.HEARTBEAT_SECONDS):
            test_progress.heartbeat(root, normalized["execution_id"])

    heartbeat_thread = threading.Thread(target=keep_alive, name="handsoff-regression-heartbeat", daemon=True)
    heartbeat_thread.start()
    _write(root, state)
    try:
        results = [run_command(root, command, state, timeout=timeout, command_index=index,
                               max_shards=max_shards) for index, command in enumerate(commands, 1)]
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
    code = run_battery(root, label, commands, timeout=args.timeout, max_shards=args.shards)
    state = json.loads(progress_path(root).read_text(encoding="utf-8"))
    totals = state["totals"]
    print(f"HANDSOFF_REGRESS_{'OK' if code == 0 else 'FAILED'}: {totals.get('passed', 0)} passed, "
          f"{totals.get('failed', 0)} failed, {totals.get('errors', 0)} errors, {totals.get('skipped', 0)} skipped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
