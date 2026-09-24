#!/usr/bin/env python3
"""The one canonical release preflight (#297).

Every check here is fast, local and deterministic: no network, no test
runner, no managed session.  CI runs it as the first job and the five
Python shards depend on it, so a defect this catches never pays for a
matrix fan-out.  The release playbook runs the same command before a push
for the same reason.

It answers one question per check and names the exact repair.  A stale
runtime manifest prints the regeneration command and every stale path,
because that omission cost a whole CI cycle on v0.3.80 (#297) after the
integrity tests caught it too late to be cheap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

import handsoff_manifest

MANIFEST_NAME = "handsoff-runtime.json"
SCHEMA_DIR = "schemas"
CONFIG_NAME = "handsoff.toml"
PROJECT_NAME = "pyproject.toml"

#: Printed verbatim whenever the manifest is stale, so the fix never has to
#: be reconstructed from memory.
REGENERATE = "python3 bin/handsoff_manifest.py --version v{version}"


class PreflightError(Exception):
    """A check could not run at all, as distinct from a check that failed."""


def _read_project_version(root: Path) -> str:
    path = root / PROJECT_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreflightError(f"{PROJECT_NAME} is missing") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PreflightError(f"{PROJECT_NAME} is not valid TOML: {exc}") from exc
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise PreflightError(f"{PROJECT_NAME} has no [project] version")
    return version.strip()


def check_runtime_manifest(root: Path) -> dict:
    """Compare every covered runtime file against the shipped manifest.

    Missing, changed and uncovered files are reported separately: they have
    different repairs, and collapsing them into one count is what makes a
    stale manifest expensive to diagnose.
    """
    path = root / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"name": "runtime_manifest", "ok": False,
                "detail": f"{MANIFEST_NAME} is missing", "stale": [], "missing": [],
                "uncovered": list(handsoff_manifest.RUNTIME_FILES)}
    except json.JSONDecodeError as exc:
        return {"name": "runtime_manifest", "ok": False,
                "detail": f"{MANIFEST_NAME} is not valid JSON: {exc}",
                "stale": [], "missing": [], "uncovered": []}

    recorded = manifest.get("files")
    if not isinstance(recorded, dict):
        return {"name": "runtime_manifest", "ok": False,
                "detail": f"{MANIFEST_NAME} has no files map", "stale": [],
                "missing": [], "uncovered": list(handsoff_manifest.RUNTIME_FILES)}

    stale, missing = [], []
    for relative in handsoff_manifest.RUNTIME_FILES:
        target = root / relative
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except FileNotFoundError:
            missing.append(relative)
            continue
        if recorded.get(relative) != digest:
            stale.append(relative)
    # A file the manifest records but the covered set no longer names is as
    # much a drift as a changed hash; it means the two lists disagree.
    uncovered = sorted(set(recorded) - set(handsoff_manifest.RUNTIME_FILES))

    ok = not (stale or missing or uncovered)
    detail = "the runtime manifest matches every covered runtime file"
    if not ok:
        parts = []
        if stale:
            parts.append(f"{len(stale)} changed")
        if missing:
            parts.append(f"{len(missing)} missing")
        if uncovered:
            parts.append(f"{len(uncovered)} no longer covered")
        detail = f"the runtime manifest is stale ({', '.join(parts)})"
    return {"name": "runtime_manifest", "ok": ok, "detail": detail,
            "stale": sorted(stale), "missing": sorted(missing), "uncovered": uncovered}


def check_release_version(root: Path) -> dict:
    """The manifest's version must be the version the project declares."""
    try:
        version = _read_project_version(root)
    except PreflightError as exc:
        return {"name": "release_version", "ok": False, "detail": str(exc)}
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return {"name": "release_version", "ok": False,
                "detail": f"{MANIFEST_NAME} could not be read: {exc}"}
    recorded = manifest.get("version")
    # The manifest carries the tag form (v0.3.80); pyproject carries the bare
    # form (0.3.80).  Compare on the bare form so neither has to change.
    bare = recorded[1:] if isinstance(recorded, str) and recorded.startswith("v") else recorded
    if bare != version:
        return {"name": "release_version", "ok": False,
                "detail": f"{PROJECT_NAME} declares {version} but {MANIFEST_NAME} records {recorded}"}
    return {"name": "release_version", "ok": True,
            "detail": f"{PROJECT_NAME} and {MANIFEST_NAME} both declare {version}"}


def check_schemas(root: Path) -> dict:
    """Every shipped schema must parse; an unparseable one fails late and loudly."""
    directory = root / SCHEMA_DIR
    if not directory.is_dir():
        return {"name": "schemas", "ok": False, "detail": f"{SCHEMA_DIR}/ is missing", "invalid": []}
    invalid = []
    for path in sorted(directory.glob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            invalid.append(f"{path.relative_to(root)}: {exc}")
    if invalid:
        return {"name": "schemas", "ok": False,
                "detail": f"{len(invalid)} schema file(s) are not valid JSON", "invalid": invalid}
    return {"name": "schemas", "ok": True, "detail": "every schema parses", "invalid": []}


def check_project_config(root: Path) -> dict:
    """The repository's own handsoff.toml must load as TOML."""
    path = root / CONFIG_NAME
    if not path.exists():
        return {"name": "project_config", "ok": True,
                "detail": f"{CONFIG_NAME} is absent, which is legal for a non-Handsoff repository"}
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return {"name": "project_config", "ok": False,
                "detail": f"{CONFIG_NAME} is not valid TOML: {exc}"}
    return {"name": "project_config", "ok": True, "detail": f"{CONFIG_NAME} parses"}


CHECKS = (check_runtime_manifest, check_release_version, check_schemas, check_project_config)


def preflight(root: Path) -> dict:
    """Run every check and return the whole verdict, failures included."""
    root = Path(root).resolve()
    results = [check(root) for check in CHECKS]
    return {"root": str(root), "ok": all(item["ok"] for item in results), "checks": results}


def render(report: dict, root: Path) -> str:
    """Human-readable output; the stale paths and the repair are always named."""
    lines = []
    for item in report["checks"]:
        lines.append(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}: {item['detail']}")
        for key, label in (("stale", "changed since the manifest"),
                           ("missing", "covered but absent"),
                           ("uncovered", "recorded but no longer covered"),
                           ("invalid", "invalid")):
            for path in item.get(key) or []:
                lines.append(f"        {label}: {path}")
    if report["ok"]:
        lines.append("HANDSOFF_PREFLIGHT_OK")
        return "\n".join(lines)
    manifest = next(item for item in report["checks"] if item["name"] == "runtime_manifest")
    if not manifest["ok"]:
        try:
            version = _read_project_version(Path(root))
        except PreflightError:
            version = "X.Y.Z"
        lines.append("")
        lines.append("Regenerate the runtime manifest, then retry:")
        lines.append(f"    {REGENERATE.format(version=version)}")
    lines.append("HANDSOFF_PREFLIGHT_FAILED")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="The canonical Handsoff release preflight")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report = preflight(root)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report, root))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
