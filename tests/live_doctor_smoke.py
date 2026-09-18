#!/usr/bin/env python3
"""Deployed-engine check for doctor's path classification and documentation audit.

Proves two things about the installed engine, not the source tree: the
stable `handsoff` command serves the release this tree declares, and
`handsoff doctor` on a fresh thin project reports provenance-based
runtime path classification (#70) and the read-only documentation audit
(#71). A project-owned `dashboard/` directory must not be labelled a
copied runtime, and a stale copied-runtime command in the project's
README must be reported without the file being rewritten.

It also proves the v0.3.25 field-note fix where it was observed: the
installed engine's Codex pre-flight reports `reachable` from a fresh thin
project and from this real repository root (the probe used to borrow the
8,000-token floor and run inside the project, so a working Codex read as
unreachable). The executable under test is the dedicated-venv installation
INSTALL.md creates, asserted through `version --json`, never a source-tree
or unrelated PATH copy.
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


VENV = Path.home() / ".local" / "share" / "handsoff" / "venv"

identity = run("version", "--json")
assert identity["source"] == "installed-engine", identity
assert identity["version"] == expected_version, (identity["version"], expected_version)
source_root = Path(identity["source_root"]).resolve()
assert VENV.resolve() in source_root.parents, (source_root, VENV)
assert Path(HANDSOFF).resolve().is_relative_to(VENV.resolve()), Path(HANDSOFF).resolve()


def codex_preflight(report: dict, where: str) -> str:
    """Codex reachable wherever the executable exists; 'absent' when it is not installed."""
    if not report["adapters"]["codex"]["available"]:
        return "absent"
    state = report["preflight"]["codex"]["state"]
    assert state == "reachable", (where, report["preflight"]["codex"])
    return state

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
    thin_codex = codex_preflight(report, "thin project")
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

# The field note's own reproduction: doctor from a real project root, where the
# reviewer-shaped prompt plus the project's context exhausted the old budget.
root_report = run("doctor", str(ROOT))
assert root_report["engine"]["version"] == expected_version, root_report["engine"]
root_codex = codex_preflight(root_report, "repository root")

print(f"LIVE_DOCTOR_OK installed={identity['version']} executable={Path(HANDSOFF).resolve()} "
      f"project_owned_dashboard=yes documentation_audit=yes docs_only=yes commands_reference=yes "
      f"codex_preflight_thin={thin_codex} codex_preflight_root={root_codex}")
