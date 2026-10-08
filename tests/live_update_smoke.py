#!/usr/bin/env python3
"""Deployed-engine check for `handsoff update` (#219): the installed
engine's `handsoff update --dry-run` on this machine reads one line per
tool with the installed versions and touches nothing (the dry run answers
every writing call without making it). The line for each tool is one of
the documented forms; the last line is UPDATE_OK (dry run).

#426: a dry run blocked by another live managed session is a valid
outcome too. The install check then names each live session on its own
`blocked: <root> <role> <session_id>` line, reports their count on the
INSTALL_CHECK_BLOCKED line, and the last line says the install would be
blocked; nothing else is checked, so no tool lines follow.

The parsing lives in classify_dry_run so a unit test can drive it with
recorded outputs; importing this module runs nothing.
"""
import json
import re
import subprocess
from pathlib import Path

BLOCKED_LAST_LINE = "dry run: the install would be blocked; nothing else is checked"
BLOCKED_CHECK = re.compile(r"^INSTALL_CHECK_BLOCKED: (\d+) live managed session\(s\); .+$")
BLOCKED_SESSION = re.compile(r"^blocked: (\S.*) (\S+) (\S+)$")
FORM = re.compile(r"^(handsoff|miner|sentinel|beakon) (already \S+|would update .+ -> .+|left at .+: .+|skipped: .+|failed: .+)$")
LAST = re.compile(r"UPDATE_OK \(dry run\)|UPDATE_FAILED: (beakon|fleet)(, (beakon|fleet))?")


def classify_dry_run(output: str, returncode: int | None = None) -> str:
    """Classify the stdout of `handsoff update --dry-run` as "ok", "failed"
    or "blocked", raising AssertionError (with the offending lines) for any
    output the documented forms do not allow. When returncode is given it
    must agree with the outcome: 0 for ok, 1 for failed or blocked."""
    lines = output.strip().splitlines()
    last = lines[-1] if lines else ""
    if last == BLOCKED_LAST_LINE:
        checks = [line for line in lines[:-1] if line.startswith("INSTALL_CHECK_")]
        assert len(checks) == 1, lines
        match = BLOCKED_CHECK.match(checks[0])
        assert match, checks[0]
        sessions = [line for line in lines[:-1] if line.startswith("blocked: ")]
        for line in sessions:
            assert BLOCKED_SESSION.match(line), line
        # The check names every live session: one line each, and the count it
        # reports is the number of sessions named.
        assert sessions and int(match.group(1)) == len(sessions), (match.group(1), sessions)
        unexpected = [line for line in lines[:-1] if line not in sessions and line != checks[0]]
        assert not unexpected, unexpected
        if returncode is not None:
            assert returncode == 1, (returncode, last)
        return "blocked"
    # The operator's Beakon checkout may carry work in progress; the command
    # then names it as failed (never over local changes) and exits 1. That is
    # the documented behaviour, not a smoke failure: the wheel tools must read.
    assert LAST.fullmatch(last), lines
    outcome = "ok" if last.startswith("UPDATE_OK") else "failed"
    if returncode is not None:
        assert returncode == (0 if outcome == "ok" else 1), (returncode, last)
    tools = {}
    for line in lines[:-1]:
        if line.startswith("INSTALL_CHECK_"):
            # #216: the install check speaks first; a dry run reads OK on a quiet register
            assert line == "INSTALL_CHECK_OK", line
            continue
        if line.startswith("fleet "):
            assert line.startswith("fleet would restart: kickstart com.moncy.handsoff-dashboard") or line.startswith("fleet skipped"), line
            continue
        match = FORM.match(line)
        assert match, line
        tools[match.group(1)] = match.group(2)
    assert set(tools) == {"handsoff", "miner", "sentinel", "beakon"}, tools
    for wheel in ("handsoff", "miner", "sentinel"):
        assert tools[wheel].startswith(("already", "would update", "left at", "skipped: no gh login")), (wheel, tools[wheel])
    return outcome


def tool_lines(output: str) -> dict:
    """The per-tool readings of an ok or failed dry run, by tool name."""
    return {m.group(1): m.group(2) for m in map(FORM.match, output.strip().splitlines()[:-1]) if m}


def main() -> None:
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               check=True).stdout.strip())
    handsoff = str(Path.home() / ".local" / "bin" / "handsoff")
    expected_version = json.loads((root / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]
    identity = json.loads(subprocess.run([handsoff, "version", "--json"], capture_output=True, text=True,
                                         check=True).stdout)
    assert identity["version"] == expected_version, (identity["version"], expected_version)

    completed = subprocess.run([handsoff, "update", "--dry-run"], capture_output=True, text=True)
    outcome = classify_dry_run(completed.stdout, completed.returncode)
    if outcome == "blocked":
        print("LIVE_UPDATE_OK blocked by live managed sessions: "
              + next(line for line in completed.stdout.splitlines() if line.startswith("INSTALL_CHECK_BLOCKED")))
        return
    tools = tool_lines(completed.stdout)
    print("LIVE_UPDATE_OK " + "; ".join(f"{k}: {v}" for k, v in tools.items()))


if __name__ == "__main__":
    main()
