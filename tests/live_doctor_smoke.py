#!/usr/bin/env python3
"""Deployed-engine check for doctor's path classification and documentation audit.

Proves two things about the installed engine, not the source tree: the
stable `handsoff` command serves the release this tree declares, and
`handsoff doctor` on a fresh thin project reports provenance-based
runtime path classification (#70) and the read-only documentation audit
(#71). A project-owned `dashboard/` directory must not be labelled a
copied runtime, and a stale copied-runtime command in the project's
README must be reported without the file being rewritten.
"""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
expected_version = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]


def run(*args: str) -> dict:
    completed = subprocess.run([HANDSOFF, *args], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


identity = run("version", "--json")
assert identity["source"] == "installed-engine", identity
assert identity["version"] == expected_version, (identity["version"], expected_version)

with tempfile.TemporaryDirectory(prefix="handsoff-live-doctor-") as tmp:
    project = Path(tmp) / "thin-project"
    subprocess.run([HANDSOFF, "init", str(project)], capture_output=True, text=True, check=True)
    (project / "dashboard").mkdir()
    (project / "dashboard" / "app.js").write_text("console.log('project owned');\n", encoding="utf-8")
    readme = project / "README.md"
    readme.write_text("Run `python3 bin/handsoff_supervisor.py status` to check.\n", encoding="utf-8")
    before = readme.read_bytes()

    report = run("doctor", str(project))
    assert report["ok"] is True, report
    assert report["engine"]["version"] == expected_version, report["engine"]
    classified = {Path(entry["path"]).name: entry["classification"] for entry in report["runtime_paths"]}
    assert classified.get("dashboard") == "project-owned", classified
    assert not report["legacy_runtime_paths"], report["legacy_runtime_paths"]
    documentation = report["documentation"]
    assert documentation["stale"] is True, documentation
    codes = {(Path(item["path"]).name, item["code"]) for item in documentation["diagnostics"]}
    assert ("README.md", "obsolete-command-path") in codes, codes
    assert documentation["installed_engine"] == expected_version, documentation
    assert readme.read_bytes() == before, "documentation audit rewrote README.md"

    docs_only = subprocess.run([HANDSOFF, "doctor", str(project), "--docs-only"], capture_output=True, text=True)
    assert docs_only.returncode == 1 and "README.md" in docs_only.stdout and ":obsolete-command-path:" in docs_only.stdout, docs_only
    readme.write_text("Run `handsoff supervisor status` to check.\n", encoding="utf-8")
    clean = subprocess.run([HANDSOFF, "doctor", str(project), "--docs-only"], capture_output=True, text=True)
    assert clean.returncode == 0 and "DOCUMENTATION_OK" in clean.stdout, clean
    commands = subprocess.run([HANDSOFF, "commands"], capture_output=True, text=True, check=True).stdout
    assert "## doctor" in commands and "--docs-only" in commands and "## verify" in commands, commands[:400]

print(f"LIVE_DOCTOR_OK installed={identity['version']} project_owned_dashboard=yes documentation_audit=yes docs_only=yes commands_reference=yes")
