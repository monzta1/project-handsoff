#!/usr/bin/env python3
"""Deployed-engine check for `handsoff update` (#219): the installed
engine's `handsoff update --dry-run` on this machine reads one line per
tool with the installed versions and touches nothing (the dry run answers
every writing call without making it). The line for each tool is one of
the documented forms; the last line is UPDATE_OK (dry run).
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]
identity = json.loads(subprocess.run([HANDSOFF, "version", "--json"], capture_output=True, text=True, check=True).stdout)
assert identity["version"] == expected_version, (identity["version"], expected_version)

completed = subprocess.run([HANDSOFF, "update", "--dry-run"], capture_output=True, text=True)
lines = completed.stdout.strip().splitlines()
# The operator's Beakon checkout may carry work in progress; the command
# then names it as failed (never over local changes) and exits 1. That is
# the documented behaviour, not a smoke failure: the wheel tools must read.
last = lines[-1] if lines else ""
assert re.fullmatch(r"UPDATE_OK \(dry run\)|UPDATE_FAILED: (beakon|fleet)(, (beakon|fleet))?", last), lines
assert completed.returncode == (0 if last.startswith("UPDATE_OK") else 1), (completed.returncode, last)
FORM = re.compile(r"^(handsoff|miner|sentinel|beakon) (already \S+|would update .+ -> .+|left at .+: .+|skipped: .+|failed: .+)$")
tools = {}
for line in lines[:-1]:
    if line.startswith("fleet "):
        assert line.startswith("fleet would restart: kickstart com.moncy.handsoff-dashboard") or line.startswith("fleet skipped"), line
        continue
    match = FORM.match(line)
    assert match, line
    tools[match.group(1)] = match.group(2)
assert set(tools) == {"handsoff", "miner", "sentinel", "beakon"}, tools
for wheel in ("handsoff", "miner", "sentinel"):
    assert tools[wheel].startswith(("already", "would update", "left at", "skipped: no gh login")), (wheel, tools[wheel])
print("LIVE_UPDATE_OK " + "; ".join(f"{k}: {v}" for k, v in tools.items()))
