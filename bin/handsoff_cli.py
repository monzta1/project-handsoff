#!/usr/bin/env python3
"""Installed Handsoff command: thin projects, version control, and dispatch."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import handsoff_agent
import handsoff_dashboard
import handsoff_fleet
import handsoff_lib as lib
import handsoff_supervisor

PIN_HISTORY = ".handsoff/engine-pins.json"
LEGACY_PARTS = ("bin", "dashboard", "fleet", "schemas", "templates", "handsoff-runtime.json")


def _root(value: str | None) -> Path:
    return Path(value or ".").expanduser().resolve()


def _current_identity() -> dict:
    manifest = json.loads((lib.engine_root() / lib.RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8"))
    path = lib.engine_root() / lib.RUNTIME_MANIFEST_FILE
    return {"version": manifest["version"], "source": "installed-engine",
            "source_root": str(lib.engine_root()),
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def init_project(root: Path, pin: str | None, *, dry_run: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    identity = _current_identity()
    current = lib._version_tuple(identity["version"])
    required = pin or f"{current[0]}.{current[1]}.*"
    if not lib.version_satisfies(identity["version"], required):
        raise lib.HandsoffError(f"installed engine {identity['version']} does not satisfy requested pin {required}")
    config = root / "handsoff.toml"
    plan = {"root": str(root), "config": "preserve" if config.exists() else "create",
            "pin": required, "engine": identity, "dry_run": dry_run}
    if dry_run:
        return plan
    if not config.exists():
        template = lib.engine_resource_path("templates/handsoff.toml")
        if not template.is_file():
            raise lib.HandsoffError("installed Handsoff configuration template is missing")
        rendered = template.read_text(encoding="utf-8").replace(
            'name = "handsoff-project"', f'name = {json.dumps(root.name)}', 1,
        )
        lib._atomic_write_text(config, rendered)
    lib._atomic_write_text(root / lib.VERSION_PIN_FILE, required + "\n")
    return plan


def _history_path(root: Path) -> Path:
    return root / PIN_HISTORY


def _load_history(root: Path) -> list[dict]:
    try:
        value = json.loads(_history_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise lib.HandsoffError("engine pin history is unreadable") from exc
    if not isinstance(value, list):
        raise lib.HandsoffError("engine pin history is invalid")
    return value


def change_pin(root: Path, target: str, *, dry_run: bool, action: str) -> dict:
    identity = _current_identity()
    pin_path = root / lib.VERSION_PIN_FILE
    try:
        current = pin_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise lib.HandsoffError(f"project version pin is missing: {pin_path}") from exc
    compatible = lib.version_satisfies(identity["version"], target)
    plan = {"action": action, "root": str(root), "from": current, "to": target,
            "installed": identity["version"], "compatible": compatible, "dry_run": dry_run}
    if not compatible:
        normalized = target.removeprefix("==").removeprefix("v")
        plan["required_install"] = (
            f"python3 -m pip install --upgrade https://github.com/monzta1/project-handsoff/"
            f"releases/download/v{normalized}/project_handsoff-{normalized}-py3-none-any.whl"
            if "*" not in normalized else f"install a {normalized} engine from the project-handsoff releases page"
        )
        if not dry_run:
            raise lib.HandsoffError(
                f"installed engine {identity['version']} cannot activate pin {target}; "
                f"preview recorded required install command: {plan['required_install']}"
            )
        return plan
    if dry_run:
        return plan
    history = _load_history(root)
    history.append({"from": current, "to": target, "action": action,
                    "at": datetime.now(timezone.utc).isoformat()})
    history = history[-32:]
    _history_path(root).parent.mkdir(parents=True, exist_ok=True)
    lib.atomic_write_json(_history_path(root), history)
    lib._atomic_write_text(pin_path, target + "\n")
    return plan


def rollback_pin(root: Path, *, dry_run: bool) -> dict:
    history = _load_history(root)
    if not history:
        raise lib.HandsoffError("no prior engine pin is available for rollback")
    return change_pin(root, history[-1]["from"], dry_run=dry_run, action="rollback")


def migrate_project(root: Path, *, dry_run: bool) -> dict:
    identity = lib.validate_runtime_integrity(root)
    if identity["source"] != "project-drop-in":
        raise lib.HandsoffError("project already uses the installed thin runtime")
    version = identity["version"]
    backup = root / ".handsoff" / "legacy-runtime" / version.lstrip("v")
    parts = [item for item in LEGACY_PARTS if (root / item).exists()]
    prompt_overrides = {}
    prompts = root / "prompts"
    if prompts.is_dir():
        for path in prompts.glob("*.md"):
            relative = f"prompts/{path.name}"
            engine = lib.engine_resource_path(relative)
            if not engine.is_file() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(engine.read_bytes()).digest():
                prompt_overrides[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not prompt_overrides:
            parts.append("prompts")
    plan = {"root": str(root), "version": version, "backup": str(backup), "move": parts,
            "preserved_state": ["handsoff-status.json", "handsoff-acceptance.json",
                                "handsoff-events.jsonl", "handsoff-verifications.jsonl"],
            "prompt_overrides": sorted(prompt_overrides), "dry_run": dry_run}
    if dry_run:
        return plan
    if backup.exists():
        raise lib.HandsoffError(f"migration backup already exists: {backup}")
    backup.mkdir(parents=True)
    moved = []
    try:
        for item in parts:
            (root / item).rename(backup / item)
            moved.append(item)
        if prompt_overrides:
            lib.atomic_write_json(root / lib.OVERRIDES_FILE, {"schema": 1, "files": prompt_overrides})
        lib._atomic_write_text(root / lib.VERSION_PIN_FILE, version + "\n")
        lib.validate_runtime_integrity(root)
    except Exception:
        for item in reversed(moved):
            if (backup / item).exists() and not (root / item).exists():
                (backup / item).rename(root / item)
        raise
    return plan


def doctor(root: Path) -> dict:
    identity = lib.validate_runtime_integrity(root)
    cfg = lib.load_config(root)
    legacy_runtime_paths = [str(root / item) for item in LEGACY_PARTS if (root / item).exists()]
    return {"ok": True, "root": str(root), "engine": identity,
            "config": str(root / "handsoff.toml"),
            "adapters": lib.adapter_availability(),
            "state_present": lib.status_path(root, cfg).exists(),
            "console_executable": str(Path(sys.argv[0]).expanduser().resolve()),
            "legacy_runtime_paths": legacy_runtime_paths,
            "migration_required": identity["source"] == "project-drop-in",
            "python": ".".join(map(str, sys.version_info[:3]))}


def _dispatch(module_main, args: list[str]) -> int:
    previous = sys.argv
    try:
        sys.argv = [previous[0], *args]
        return int(module_main())
    finally:
        sys.argv = previous


def _dispatch_supervisor(args: list[str]) -> int:
    root_value = None
    if "--root" in args:
        index = args.index("--root")
        if index + 1 >= len(args):
            raise lib.HandsoffError("--root requires a project path")
        root_value = args[index + 1]
    lib.validate_runtime_integrity(lib.resolve_root(root_value))
    return _dispatch(handsoff_supervisor.main, args)


def main() -> int:
    # Passthrough commands own their entire remaining argv, including
    # options such as --root. Dispatch before argparse so no `--` separator
    # is ever required from the global CLI.
    if len(sys.argv) > 1 and sys.argv[1] in {"supervisor", "agent", "fleet"}:
        try:
            if sys.argv[1] == "supervisor":
                return _dispatch_supervisor(sys.argv[2:])
            if sys.argv[1] == "agent":
                return _dispatch(handsoff_agent.main, sys.argv[2:])
            return _dispatch(handsoff_fleet.main, sys.argv[2:])
        except lib.HandsoffError as exc:
            print(f"HANDSOFF_BLOCKED: {exc}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(prog="handsoff", description="Versioned Handsoff engine")
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version")
    version.add_argument("--json", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("root", nargs="?", default=".")
    init.add_argument("--pin")
    init.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("doctor")
    check.add_argument("root", nargs="?", default=".")
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("root", nargs="?", default=".")
    upgrade.add_argument("--to", required=True)
    upgrade.add_argument("--dry-run", action="store_true")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("root", nargs="?", default=".")
    rollback.add_argument("--dry-run", action="store_true")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("root", nargs="?", default=".")
    migrate.add_argument("--dry-run", action="store_true")
    for name in ("supervisor", "agent", "fleet"):
        command = sub.add_parser(name)
        command.add_argument("args", nargs=argparse.REMAINDER)
    dash = sub.add_parser("dashboard")
    dash.add_argument("--root", default=None)
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "version":
            value = _current_identity()
            print(json.dumps(value, indent=2) if args.json else value["version"])
            return 0
        if args.command == "init":
            result = init_project(_root(args.root), args.pin, dry_run=args.dry_run)
        elif args.command == "doctor":
            result = doctor(_root(args.root))
        elif args.command == "upgrade":
            result = change_pin(_root(args.root), args.to, dry_run=args.dry_run, action="upgrade")
        elif args.command == "rollback":
            result = rollback_pin(_root(args.root), dry_run=args.dry_run)
        elif args.command == "migrate":
            result = migrate_project(_root(args.root), dry_run=args.dry_run)
        elif args.command == "supervisor":
            return _dispatch_supervisor(args.args)
        elif args.command == "agent":
            return _dispatch(handsoff_agent.main, args.args)
        elif args.command == "fleet":
            return _dispatch(handsoff_fleet.main, args.args)
        else:
            call = ["--root", args.root] if args.root else []
            call += ["dashboard", "--host", args.host, "--port", str(args.port)]
            if args.no_open:
                call.append("--no-open")
            return _dispatch_supervisor(call)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except lib.HandsoffError as exc:
        print(f"HANDSOFF_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
