#!/usr/bin/env python3
"""Deployed-engine check for the Miner shim (#174).

Proves, against the installed engine and the Miner installed beside it in
the dedicated environment, that `handsoff supervisor analyze-archives
--dry-run` is a shim over `miner scan`: it runs from a thin project against
a temporary archive holding one product run, prints the report path the
Miner wrote under the project's `.handsoff-analysis/`, and its summary
counts that run. Nothing is filed (dry run), nothing touches the operator's
Documents archive, and the real gh is never called (a PATH shim records any
attempt). Then, with the Miner hidden (HANDSOFF_MINER pointing nowhere is
not enough; PATH and the interpreter's bin are what the engine reads, so
HANDSOFF_MINER is set to a file that does not exist and the refusal names
the Miner's exit), the shim refuses rather than scanning on its own.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
VENV = Path.home() / ".local" / "share" / "handsoff" / "venv"
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]

identity = json.loads(subprocess.run([HANDSOFF, "version", "--json"], capture_output=True, text=True, check=True).stdout)
assert identity["source"] == "installed-engine", identity
assert identity["version"] == expected_version, (identity["version"], expected_version)
miner = VENV / "bin" / "miner"
assert miner.is_file(), f"the Miner is not installed beside the engine: {miner}"
# the installed Miner is the release the engine names (MINER_RELEASE)
import sys
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_supervisor  # noqa: E402
installed = subprocess.run([str(VENV / "bin" / "pip"), "show", "miner"], capture_output=True, text=True, check=True).stdout
version_line = next(line for line in installed.splitlines() if line.startswith("Version:"))
assert "v" + version_line.split(":", 1)[1].strip() == handsoff_supervisor.MINER_RELEASE, (version_line, handsoff_supervisor.MINER_RELEASE)

documents = Path.home() / "Documents" / "Handsoff-Archive"
documents_before = sorted(p.name for p in documents.iterdir()) if documents.exists() else []

with tempfile.TemporaryDirectory(prefix="handsoff-miner-shim-smoke-") as tmp:
    base = Path(tmp)
    project = base / "project"
    project.mkdir()
    subprocess.run([HANDSOFF, "init", str(project)], capture_output=True, text=True, check=True)
    archive = base / "archive"
    archive.mkdir()
    (archive / "one.json").write_text(json.dumps({
        "repo": "smoke", "root": "/tmp/smoke", "feature": "Smoke", "run_kind": "product",
        "started_at": "2026-09-01T10:00:00+00:00", "archived_at": "2026-09-01T11:00:00+00:00",
        "completed_at": "2026-09-01T11:00:00+00:00",
        "status": {"status": "complete", "phase_number": 8}, "acceptance": {"criteria": []},
        "verifications": [], "events": [{"kind": "initialized", "at": "2026-09-01T10:00:00+00:00", "message": "x"}],
        "metrics": {}}))
    shim = base / "shim"
    shim.mkdir()
    marker = shim / "gh-was-called"
    (shim / "gh").write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    (shim / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}", "HANDSOFF_ARCHIVE_DIR": str(archive)}
    env.pop("HANDSOFF_MINER", None)
    completed = subprocess.run([HANDSOFF, "supervisor", "--root", str(project), "analyze-archives", "--dry-run",
                                "--archive-dir", str(archive)], capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    first, rest = completed.stdout.split("\n", 1)
    assert first.startswith("HANDSOFF_ANALYSIS_REPORT: "), completed.stdout
    report_path = Path(first.split(": ", 1)[1].strip())
    assert report_path.is_file(), report_path
    assert report_path.parent.resolve() == (project / ".handsoff-analysis").resolve(), report_path
    summary = json.loads(rest)
    assert summary["runs"]["product"] == 1, summary
    assert summary["filing"]["mode"] == "dry_run", summary
    assert summary["filed"] == [], summary
    assert not marker.exists(), "the shim let the Miner call the real gh"
    # the refusal: an unusable Miner is named, the engine never scans on its own
    env["HANDSOFF_MINER"] = str(base / "no-such-miner")
    refused = subprocess.run([HANDSOFF, "supervisor", "--root", str(project), "analyze-archives", "--dry-run"],
                             capture_output=True, text=True, env=env)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "SHIP_FEATURE_BLOCKED:" in refused.stdout, refused.stdout
    reports_after = sorted(p.resolve() for p in (project / ".handsoff-analysis").glob("*.json"))
    assert reports_after == [report_path.resolve()], reports_after

documents_after = sorted(p.name for p in documents.iterdir()) if documents.exists() else []
assert documents_after == documents_before, "the smoke wrote into the operator's Documents archive"
print(f"LIVE_MINER_SHIM_OK: {expected_version} scans through {miner} ({handsoff_supervisor.MINER_RELEASE})")
