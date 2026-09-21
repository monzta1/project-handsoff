#!/usr/bin/env python3
"""Installed Handsoff command: thin projects, version control, and dispatch."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
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
    prompt_overrides = lib.prompt_override_diagnosis(root)
    run_triage = lib.run_triage(root, lib.load_config(root))
    plan = {"action": action, "root": str(root), "from": current, "to": target,
            "installed": identity["version"], "compatible": compatible, "dry_run": dry_run,
            "prompt_overrides": prompt_overrides, "run_triage": run_triage}
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
    stale = next((item for item in prompt_overrides
                  if item["state"] == "declared_stale_protocol"), None)
    if stale:
        raise lib.HandsoffError(f"cannot activate upgrade: {stale['path']} is missing {stale['expected_prefix']}")
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
    runtime_paths, _ = _runtime_path_diagnosis(root, identity)
    parts = [Path(item["path"]).name for item in runtime_paths
             if item["classification"] == "copied-runtime"]
    prompt_overrides = {}
    prompts = root / "prompts"
    if prompts.is_dir():
        signatures = _runtime_signatures()
        prompt_entry = _classify_runtime_path(root, "prompts", signatures, identity)
        if prompt_entry["classification"] == "copied-runtime" and "prompts" not in parts:
            parts.append("prompts")
        else:
            # Once the runtime manifest is moved, every project prompt left
            # behind is an override, even when it happened to match the old
            # bundled prompt. Binding every retained prompt preserves mixed
            # project-owned directories without creating undeclared files.
            allowed_prompts = {f"{role}.md" for role in lib.SELECTABLE_AGENT_ROLES}
            for path in sorted(prompts.glob("*.md")):
                if path.name not in allowed_prompts:
                    continue
                relative = f"prompts/{path.name}"
                prompt_overrides[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
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


def _runtime_signatures() -> dict[str, str]:
    manifest_path = lib.engine_root() / lib.RUNTIME_MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        signatures = manifest.get("files", {})
    except (OSError, ValueError):
        return {}
    return signatures if isinstance(signatures, dict) else {}


def _runtime_payload_files(path: Path, root: Path) -> set[str]:
    ignored_names = {".DS_Store"}
    files = set()
    for candidate in path.rglob("*"):
        if not candidate.is_file() or candidate.name in ignored_names \
                or "__pycache__" in candidate.parts or candidate.suffix == ".pyc":
            continue
        files.add(candidate.relative_to(root).as_posix())
    return files


def _classify_runtime_path(root: Path, part: str, signatures: dict[str, str],
                           identity: dict) -> dict:
    path = root / part
    if path.is_file():
        engine_manifest = lib.engine_root() / lib.RUNTIME_MANIFEST_FILE
        try:
            same_manifest = hashlib.sha256(path.read_bytes()).digest() \
                == hashlib.sha256(engine_manifest.read_bytes()).digest()
        except OSError:
            same_manifest = False
        copied = part == lib.RUNTIME_MANIFEST_FILE \
            and identity.get("source") == "project-drop-in" and same_manifest
        return {
            "path": str(path),
            "classification": "copied-runtime" if copied else "project-owned",
            "source": "validated-runtime-identity" if copied else "project-content",
            "reason": "validated project drop-in manifest" if copied
                      else "file is not the validated runtime manifest",
        }
    expected = {relative: digest for relative, digest in signatures.items()
                if relative.startswith(part + "/")}
    actual = _runtime_payload_files(path, root) if path.is_dir() else set()
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    changed = []
    for relative in sorted(set(expected) & actual):
        try:
            digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest != expected[relative]:
            changed.append(relative)
    copied = bool(expected) and not missing and not extra and not changed
    reason = "all runtime files match the manifest exactly" if copied else \
        f"project content differs from runtime manifest (missing={len(missing)}, changed={len(changed)}, extra={len(extra)})"
    return {
        "path": str(path),
        "classification": "copied-runtime" if copied else "project-owned",
        "source": "runtime-manifest" if copied else "project-content",
        "reason": reason,
    }


def _runtime_path_diagnosis(root: Path, identity: dict) -> tuple[list[dict], list[str]]:
    """Classify paths only when exact manifest evidence proves ownership."""
    signatures = _runtime_signatures()
    found, legacy = [], []
    for part in (*LEGACY_PARTS, "prompts"):
        path = root / part
        if not path.exists():
            continue
        entry = _classify_runtime_path(root, part, signatures, identity)
        found.append(entry)
        if entry["classification"] == "copied-runtime":
            legacy.append(str(path))
    return found, legacy


def _canonical_executable() -> str:
    return shutil.which("handsoff") or "handsoff"


def _documentation_files(root: Path, cfg: dict) -> list[Path]:
    """Select documentation deterministically, respecting project-owned scope."""
    configured = cfg.get("documentation", {})
    if configured.get("files"):
        candidates = [root / relative for relative in configured["files"]]
    else:
        candidates = [path for path in root.iterdir() if path.is_file()]
        docs = root / "docs"
        if docs.is_dir():
            candidates.extend(docs.rglob("*"))
    excludes = configured.get("exclude", [])
    return sorted({path for path in candidates if path.is_file()
                   and path.suffix.lower() in {".md", ".txt"}
                   and not any(fnmatch.fnmatch(path.relative_to(root).as_posix(), pattern)
                               for pattern in excludes)},
                   key=lambda path: path.relative_to(root).as_posix())


def _documentation_diagnosis(root: Path, identity: dict, cfg: dict | None = None) -> dict:
    """Read-only documentation check based on the active installation identity."""
    executable = _canonical_executable()
    installed = str(identity["version"])
    diagnostics, suppressed = [], []
    old_version = re.compile(
        r"(?:(?:releases?/)?download/v|releases?/v|Handsoff\s+v|pin\s+v)([0-9]+\.[0-9]+\.[0-9]+)",
        re.IGNORECASE,
    )
    old_command = re.compile(
        r"(?:python\d*(?:\.\d+)?\s+)?(?:\S*/)?bin/handsoff_(?:supervisor|dashboard|agent|cli|fleet)\.py"
    )
    for path in _documentation_files(root, cfg or lib.load_config(root)):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        intentional = set()
        for index, line in enumerate(lines[:-1]):
            if line.strip() in {"handsoff-doc: intentional", "<!-- handsoff-doc: intentional -->"}:
                intentional.add(index + 1)
        for match in old_version.finditer(text):
            line_number = text.count("\n", 0, match.start())
            target = {"path": str(path), "code": "obsolete-release-reference",
                      "detail": f"references v{match.group(1)}; installed engine is {installed}"}
            if line_number in intentional:
                suppressed.append(target)
                continue
            if "v" + match.group(1) != installed and match.group(1) != installed.lstrip("v"):
                diagnostics.append(target)
        contradiction_patterns = [
            (re.compile(r"\b(?:no|without|does not have|has no) handsoff\.toml\b", re.I),
             "project has no handsoff.toml", "handsoff.toml" if (root / "handsoff.toml").is_file() else None),
        ]
        if (root / "handsoff.toml").is_file():
            for pattern, claim, resolved in contradiction_patterns:
                match = pattern.search(text)
                if match:
                    target = {"path": str(path), "code": "config-claim-contradiction",
                              "detail": f"{match.group(0)}; resolved: {resolved}"}
                    line_number = text.count("\n", 0, match.start())
                    (suppressed if line_number in intentional else diagnostics).append(target)
        profiles = lib.resolved_agent_profiles(cfg or lib.load_config(root))
        roles = "|".join(map(re.escape, lib.SELECTABLE_AGENT_ROLES))
        adapters = r"codex|claude(?:\s+code)?|host"
        assignment = re.compile(rf"(?i)(?P<role>{roles})\s*(?:=|is|:)\s*(?P<adapter>{adapters})|(?P<adapter2>{adapters})\s+as\s+(?:the\s+)?(?P<role2>{roles})")
        for match in assignment.finditer(text):
            role = (match.group("role") or match.group("role2")).lower()
            adapter = (match.group("adapter") or match.group("adapter2")).lower()
            adapter = "claude" if adapter.startswith("claude") else adapter
            resolved = profiles[role]["adapter"]
            if adapter != resolved:
                target = {"path": str(path), "code": "config-claim-contradiction",
                          "detail": f"{match.group(0)}; resolved: {resolved}"}
                line_number = text.count("\n", 0, match.start())
                (suppressed if line_number in intentional else diagnostics).append(target)
        command_match = old_command.search(text)
        if command_match or "version-specific-venv" in text:
            target = {"path": str(path), "code": "obsolete-command-path",
                      "detail": f"use the installed executable {executable}"}
            command_line = text.count("\n", 0, command_match.start()) if command_match else -1
            if command_line in intentional:
                suppressed.append(target)
            else:
                diagnostics.append(target)
    return {"stale": bool(diagnostics), "diagnostics": diagnostics, "suppressed": suppressed,
            "installed_engine": installed, "canonical_executable": executable,
            "supported_project_pin": identity.get("compatibility")}


def doctor(root: Path, *, skip_preflight: bool = False) -> dict:
    identity = lib.validate_runtime_integrity(root)
    cfg = lib.load_config(root)
    runtime_paths, legacy_runtime_paths = _runtime_path_diagnosis(root, identity)
    documentation = _documentation_diagnosis(root, identity, cfg)
    prompt_overrides = lib.prompt_override_diagnosis(root)
    run_triage = lib.run_triage(root, cfg)
    permissions = lib.implementer_allowed_tools(cfg, root)
    warnings = [f"check command cannot be expressed: {command!r}" for command in cfg["check_commands"] if not command.strip() or "\n" in command or "\r" in command]
    for item in prompt_overrides:
        if item["state"] == "declared_stale_protocol":
            warnings.append(f"override-protocol-stale: {item['path']} missing {item['expected_prefix']}")
        elif item["state"] == "undeclared":
            warnings.append(f"override-undeclared: {item['path']}")
        elif item["state"] == "declared_hash_mismatch":
            warnings.append(f"override-hash-mismatch: {item['path']}")
    # #170: a review certifies the rules set it ran under; say when it moved.
    rules_note = None
    try:
        status_file = lib.status_path(root, cfg)
        if status_file.is_file():
            status = lib.load_unique_json(status_file)
            review = status.get("review")
            if isinstance(review, dict) and review.get("rules_hash"):
                changed = lib.rules_set_diff(root, review.get("rules_entries")) \
                    if review["rules_hash"] != lib.rules_set_hash(root, cfg) else []
                if changed:
                    rules_note = "rules set changed since the last review: " + ", ".join(changed)
                    warnings.append("rules-set-changed: " + ", ".join(changed))
    except (lib.HandsoffError, OSError, ValueError):
        rules_note = None
    return {"ok": not any(item["state"] == "declared_stale_protocol" for item in prompt_overrides), "root": str(root), "engine": identity,
            "rules_set": rules_note,
            "config": str(root / "handsoff.toml"),
            "adapters": lib.adapter_availability(cfg),
            "preflight": None if skip_preflight else lib.adapter_preflight(cfg, root),
            "state_present": lib.status_path(root, cfg).exists(),
            "console_executable": _canonical_executable(),
            "legacy_runtime_paths": legacy_runtime_paths,
            "runtime_paths": runtime_paths,
            "documentation": documentation,
            "prompt_overrides": prompt_overrides, "run_triage": run_triage,
            "implementer_permissions": permissions,
            "warnings": warnings,
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


def _passthrough(argv: list[str]) -> int | None:
    """Passthrough commands own their entire remaining argv, including
    options such as --root, so they are dispatched before argparse and no
    `--` separator is ever required. Returns the exit code, or None when
    argv is not a passthrough. Kept out of build_parser on purpose: that
    function must stay pure so `handsoff commands` can render the parser
    and v0.3.14's regression (an exit code handed to parse_args) cannot
    recur."""
    if len(argv) > 1 and argv[1] in {"supervisor", "agent", "fleet"}:
        try:
            if argv[1] == "supervisor":
                return _dispatch_supervisor(argv[2:])
            if argv[1] == "agent":
                return _dispatch(handsoff_agent.main, argv[2:])
            return _dispatch(handsoff_fleet.main, argv[2:])
        except lib.HandsoffError as exc:
            print(f"HANDSOFF_BLOCKED: {exc}", file=sys.stderr)
            return 1
    return None


def build_parser() -> argparse.ArgumentParser:
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
    check.add_argument("--docs-only", action="store_true", help="run only the documentation audit")
    check.add_argument("--skip-preflight", action="store_true")
    sub.add_parser("commands", help="print the argparse command reference")
    playbook = sub.add_parser("playbook", help="#208: print the engine's lane playbook (the index, or one topic)")
    playbook.add_argument("topic", nargs="?", default=None, help="lanes, landing, reviewers, lessons")
    update = sub.add_parser("update", help="#219: bring handsoff, miner, sentinel and beakon to their latest releases "
                                           "on this machine, restart what runs here, print one line per tool")
    update.add_argument("--dry-run", action="store_true", help="print what would change; call nothing that writes")
    update.add_argument("--only", default=None, help="comma-separated subset, e.g. handsoff,miner")
    update.add_argument("--config", default=None, help="update.toml (default ~/.handsoff/update.toml)")
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
    # #72: a run-owned dashboard is what Fleet links to and what releases its
    # port on completion; the stable command must be able to start one.
    dash.add_argument("--owned-by-run", action="store_true")
    return parser


def _commands_reference() -> str:
    """Render both parser trees so the reference cannot drift from argparse."""
    parsers = [("handsoff", build_parser()), ("supervisor", handsoff_supervisor.build_parser())]
    output = []
    for prefix, parser in parsers:
        actions = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        for name, command in actions.choices.items():
            output.append(f"## {name}")
            output.append(command.format_help().strip())
            for action in command._actions:
                if action.dest == "help":
                    continue
                names = ", ".join(action.option_strings or [action.dest])
                details = []
                if action.choices:
                    details.append("choices: " + ", ".join(map(str, action.choices)))
                if action.default not in (None, argparse.SUPPRESS, False, []):
                    details.append(f"default: {action.default}")
                if action.help:
                    details.append(action.help)
                output.append(f"- {names}: {'; '.join(details)}")
            output.append("")
    return "\n".join(output)


def main() -> int:
    passthrough = _passthrough(sys.argv)
    if passthrough is not None:
        return passthrough
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "version":
            value = _current_identity()
            print(json.dumps(value, indent=2) if args.json else value["version"])
            return 0
        if args.command == "init":
            result = init_project(_root(args.root), args.pin, dry_run=args.dry_run)
        elif args.command == "doctor":
            root = _root(args.root)
            if args.docs_only:
                identity = lib.validate_runtime_integrity(root)
                result = _documentation_diagnosis(root, identity, lib.load_config(root))
                for item in result["diagnostics"]:
                    print(f'{item["path"]}:{item["code"]}:{item["detail"]}')
                if not result["diagnostics"]:
                    print("DOCUMENTATION_OK")
                return 1 if result["diagnostics"] else 0
            result = doctor(root, skip_preflight=args.skip_preflight)
        elif args.command == "commands":
            print(_commands_reference(), end="")
            return 0
        elif args.command == "playbook":
            print(lib.playbook_text(args.topic), end="")
            return 0
        elif args.command == "update":
            import handsoff_update
            try:
                cfg = handsoff_update.load_config(args.config)
                outcome = handsoff_update.update(cfg, only=[t.strip() for t in args.only.split(",") if t.strip()] if args.only else None,
                                                 dry_run=args.dry_run)
            except handsoff_update.UpdateError as exc:
                print(f"SHIP_FEATURE_BLOCKED: {exc}")
                return 1
            return outcome["exit_code"]
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
            if args.owned_by_run:
                call.append("--owned-by-run")
            return _dispatch_supervisor(call)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except lib.HandsoffError as exc:
        print(f"HANDSOFF_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
