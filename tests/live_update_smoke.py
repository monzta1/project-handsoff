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
assert completed.returncode == 0, completed.stdout + completed.stderr
lines = completed.stdout.strip().splitlines()
assert lines[-1] == "UPDATE_OK (dry run)", lines
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
assert tools["handsoff"].startswith(("already", "would update", "left at")), tools["handsoff"]
print("LIVE_UPDATE_OK " + "; ".join(f"{k}: {v}" for k, v in tools.items()))
