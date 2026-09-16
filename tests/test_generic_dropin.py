#!/usr/bin/env python3
"""Proves the README's own Quick Start claim, literally: copy handsoff.toml,
schemas/, prompts/, dashboard/, and everything in bin/ into a target
project, and it just works there -- with zero assumption baked in about
this repo's own name or path, or about any project (fm9-tone, tonecommand,
or otherwise) it was developed and tested against.

Existing tests in test_handsoff_supervisor.py exercise the CLI's own logic
against a scratch root, but always invoke THIS repo's bin/handsoff_supervisor.py
directly; they don't prove that physically relocating bin/, dashboard/,
schemas/, and prompts/ elsewhere and running the COPY still works. This
script does exactly that.

It also doubles as the Phase 8 live-verification check for this
repository's own ship-feature run: point --source at a fresh `git clone`
of the pushed GitHub repo (not this working copy) to prove the published
artifact itself behaves generically, not just the local tree.

Run: python3 tests/test_generic_dropin.py
     python3 tests/test_generic_dropin.py --source /path/to/fresh/clone
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DROPIN_ITEMS = ("handsoff.toml", "handsoff-runtime.json", "schemas", "prompts", "dashboard", "fleet", "templates", "bin")
FORBIDDEN_STRINGS = ("fm9-tone", "tonecommand", "shieldbearer")
SCANNED_SUFFIXES = (".py", ".md", ".toml", ".json", ".css", ".js", ".html")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"DROPIN_CHECK_FAILED: {message}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent.parent),
                     help="repo root to copy the drop-in files from (defaults to this working copy)")
    args = ap.parse_args()
    source = Path(args.source).resolve()

    target = Path(tempfile.mkdtemp(prefix="handsoff-dropin-")).resolve()
    try:
        for name in DROPIN_ITEMS:
            src = source / name
            dst = target / name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy(src, dst)

        combined = "\n".join(
            p.read_text(errors="ignore")
            for p in target.rglob("*")
            if p.is_file() and p.suffix in SCANNED_SUFFIXES
        ).lower()
        for bad in FORBIDDEN_STRINGS:
            check(bad not in combined, f"found a project-specific reference ({bad!r}) in the drop-in copy")

        py = sys.executable
        supervisor = target / "bin" / "handsoff_supervisor.py"

        def run(cmd_args):
            return subprocess.run([py, str(supervisor), *cmd_args], cwd=target,
                                   capture_output=True, text=True, timeout=30)

        # The README's own quick-start init command, verbatim (the
        # criterion-update below substitutes a throwaway "true" test since
        # a bare copy has no tests/test_fix.py of its own to point at).
        r = run(["init", "Fix the thing that is broken"])
        check(r.returncode == 0, f"init failed: {r.stdout}{r.stderr}")

        # Regex, not a literal "commands = []" replace: this drop-in copy's
        # own handsoff.toml is whatever the source repo happens to ship
        # (which the source repo's own tests separately hold to "[]" as
        # its pristine template state) -- this script shouldn't assume that
        # and break if the source ever legitimately configures commands.
        toml = target / "handsoff.toml"
        # DOTALL with a non-greedy body: the source repo's own self-hosting
        # config spans the array over several lines, one command per line.
        toml_text, n = re.subn(r"^commands\s*=\s*\[.*?\]$", 'commands = ["true"]',
                                toml.read_text(), count=1, flags=re.MULTILINE | re.DOTALL)
        check(n == 1, "could not locate a commands = [...] line in the drop-in copy's handsoff.toml")
        # The source repo also gates its own full suites behind
        # [[regressions]] (#28). Those groups name this repo's tests, which
        # an unrelated project does not have, and the gate reads the
        # throwaway "true" check as broad enough to capture any group, so
        # the self-hosting regression tables are dropped from the copy the
        # same way the self-hosting commands array is replaced above.
        toml_text = re.sub(r"^\[regression_gate\]\n(?:(?!\[).*\n?)*", "", toml_text, flags=re.MULTILINE)
        toml_text = re.sub(r"^\[\[regressions\]\]\n(?:(?!\[).*\n?)*", "", toml_text, flags=re.MULTILINE)
        toml.write_text(toml_text)

        r = run(["criterion-update", "REQ-001", "--requirement", "Exact observable outcome",
                 "--verification", "automated", "--test", "true"])
        check(r.returncode == 0, f"criterion-update failed: {r.stdout}{r.stderr}")

        r = run(["verify", "--criterion", "REQ-001", "--by", "dropin-tester"])
        check(r.returncode == 0, f"verify failed: {r.stdout}{r.stderr}")
        run_id = json.loads(r.stdout)["criteria"]["REQ-001"]["run_id"]

        r = run(["record-symptom-resolved", "--evidence", run_id, "--by", "dropin-tester"])
        check(r.returncode == 0, f"record-symptom-resolved failed: {r.stdout}{r.stderr}")

        r = run(["status"])
        check(r.returncode == 0, f"status failed: {r.stdout}{r.stderr}")
        status_payload = json.loads(r.stdout)
        check(status_payload["phase_number"] == 1, "status did not reflect a fresh, generic run")
        for bad in FORBIDDEN_STRINGS:
            check(bad not in r.stdout.lower(), f"status output mentions {bad!r}")

        port = free_port()
        proc = subprocess.Popen(
            [py, str(supervisor), "dashboard", "--no-open", "--port", str(port)],
            cwd=target, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            payload = None
            for _ in range(30):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=1) as resp:
                        payload = json.loads(resp.read())
                    break
                except (urllib.error.URLError, ConnectionRefusedError):
                    time.sleep(0.2)
            check(payload is not None, "dashboard never responded on a dropped-in project")
            check(payload.get("root") == str(target),
                  "dashboard reported the wrong root for a dropped-in project")
            check(payload.get("project", {}).get("feature") == "Fix the thing that is broken",
                  "dashboard did not reflect the generic target project's own feature")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        print(f"DROPIN_CHECK_OK: {source} behaves generically when dropped into an unrelated project at {target}")
        return 0
    finally:
        shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
