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
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402

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


def progress_path(root: Path) -> Path:
    return Path(root) / PROGRESS_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(root: Path, state: dict) -> None:
    lib.atomic_write_json(progress_path(root), state)


def _count_unittest_total(root: Path, argv: list[str]) -> int | None:
    """Best effort: count tests by discovery for the module the command names."""
    try:
        names: list[str] = []
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "unittest":
            names = [a for a in argv[3:] if not a.startswith("-")]
        elif len(argv) >= 2 and argv[1].startswith("tests/") and argv[1].endswith(".py"):
            names = [argv[1][:-3].replace("/", ".")]
        if not names:
            return None
        code = ("import sys, unittest; sys.path.insert(0, '.'); "
                "loader = unittest.TestLoader(); "
                "print(sum(loader.loadTestsFromName(n).countTestCases() for n in sys.argv[1:]))")
        out = subprocess.run([sys.executable, "-c", code, *names], cwd=str(root), capture_output=True,
                             text=True, timeout=120, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"})
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _verbose_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "unittest" and "-v" not in argv and "--verbose" not in argv:
        argv.append("-v")
    elif len(argv) >= 2 and argv[0].startswith("python") and argv[1].startswith("tests/") and argv[1].endswith(".py") \
            and "-v" not in argv:
        argv.append("-v")
    return argv


def run_command(root: Path, command: str, state: dict, *, timeout: int) -> int:
    argv = _verbose_argv(command)
    total = _count_unittest_total(root, argv)
    entry = {"command": command, "started_at": _now(), "finished_at": None, "exit_code": None,
             "total": total, "done": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
             "current": None, "failures": []}
    state["commands"].append(entry)
    state["current_command"] = command
    _write(root, state)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "HANDSOFF_SKIP_PREFLIGHT": os.environ.get("HANDSOFF_SKIP_PREFLIGHT", "1")}
    process = subprocess.Popen(argv, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env, start_new_session=True)
    deadline = time.monotonic() + timeout
    log_path = Path(tempfile.gettempdir()) / f"handsoff-regress-{os.getpid()}.log"
    with log_path.open("a", encoding="utf-8") as log:
        assert process.stdout is not None
        pending_name = None
        for line in process.stdout:
            log.write(line)
            event, pending_name = parse_line(line.rstrip("\n"), pending_name)
            if event is None:
                continue
            count_event(entry, event)
            state["totals"] = _totals(state)
            _write(root, state)
            if time.monotonic() > deadline:
                process.kill()
                break
    code = process.wait()
    entry["finished_at"] = _now()
    entry["exit_code"] = code
    entry["current"] = None
    entry["log"] = str(log_path)
    state["totals"] = _totals(state)
    _write(root, state)
    return code


def _totals(state: dict) -> dict:
    keys = ("total", "done", "passed", "failed", "errors", "skipped")
    totals = {key: 0 for key in keys}
    for entry in state["commands"]:
        for key in keys:
            value = entry.get(key)
            if key == "total":
                if value is None:
                    totals["total"] = None if totals["total"] is None else totals["total"]
                    continue
            totals[key] = (totals[key] or 0) + (value or 0)
    return totals


def run_battery(root: Path, label: str, commands: list[str], *, timeout: int) -> int:
    state = {"label": label, "started_at": _now(), "finished_at": None, "exit_code": None,
             "commands": [], "current_command": None, "totals": {}, "root": str(root)}
    _write(root, state)
    worst = 0
    for command in commands:
        code = run_command(root, command, state, timeout=timeout)
        worst = worst or code
    state["finished_at"] = _now()
    state["exit_code"] = worst
    state["current_command"] = None
    _write(root, state)
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a regression battery with live progress for Mission Control")
    parser.add_argument("--root", default=None)
    parser.add_argument("--group", default=None, help="a [[regressions]] group name from handsoff.toml")
    parser.add_argument("--command", action="append", default=[], help="an explicit command (repeatable)")
    parser.add_argument("--timeout", type=int, default=3600, help="per-command timeout in seconds")
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
    code = run_battery(root, label, commands, timeout=args.timeout)
    state = json.loads(progress_path(root).read_text(encoding="utf-8"))
    totals = state["totals"]
    print(f"HANDSOFF_REGRESS_{'OK' if code == 0 else 'FAILED'}: {totals.get('passed', 0)} passed, "
          f"{totals.get('failed', 0)} failed, {totals.get('errors', 0)} errors, {totals.get('skipped', 0)} skipped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
