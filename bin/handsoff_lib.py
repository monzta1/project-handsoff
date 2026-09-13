#!/usr/bin/env python3
"""Shared engine for Project Handsoff. Both CLI scripts import this; there is
exactly one copy of the gate logic, one copy of the file I/O, one copy of
the JSON loading.

Every path this module touches is resolved against a project ROOT, never
against this file's own directory: `resolve_root()` walks up from the
current directory looking for `handsoff.toml`, or honors `--root` /
`HANDSOFF_ROOT` when given. That is the fix for the original bug: the
supervisor used to resolve `handsoff-status.json` next to itself in `bin/`,
so the quick-start's own instructions (create the files at the project
root) crashed on a fresh copy.

The other structural rule this module exists to enforce: every gate is
checked against the state a call is ABOUT to write, never the state
already on disk. `advance()` builds the proposed status in memory first,
validates that, and only then writes it. Checking the state you are
leaving instead of the state you are entering is exactly how the original
tool let an ungated transition into Phase 6 succeed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.request
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

PHASES = {
    1: "Orient",
    2: "Design debate",
    3: "Design approved",
    4: "Implementation",
    5: "Independent review",
    6: "Checks & documentation",
    7: "Awaiting deployment approval",
    8: "Live verified",
}

#: Default next_action per phase, used whenever `advance` (or `init`) isn't
#: given an explicit --next-action. Without this, next_action was set once
#: at init and never touched again: a completed Phase 8 project still read
#: "reproduce the original symptom", a self-contradictory status.
NEXT_ACTION_DEFAULTS = {
    1: "Read the project rules and reproduce the original symptom.",
    2: "Debate the design until the reviewer answers DESIGN_APPROVED, then get human design-approve.",
    3: "Begin implementation against the approved design.",
    4: "Implement the change and run `verify` against each criterion's tests.",
    5: "Get an independent reviewer to run `record-review`.",
    6: "Finish documentation and homework, then advance to Phase 7.",
    7: "Get explicit deployment approval, then run `verify-live` before advancing to Phase 8.",
    8: "Workflow complete; no further action required.",
}

#: The literal placeholder criterion `init` seeds a fresh run with. Shared
#: between cmd_init (which writes it) and design-approve (which refuses to
#: bless a registry that still contains it untouched), so the two can never
#: drift apart into checking different text.
PLACEHOLDER_REQUIREMENT = "State the exact observable outcome."
PLACEHOLDER_TESTS = ["name_or_path_of_test"]

DEFAULT_CONFIG = {
    "status_file": "handsoff-status.json",
    "acceptance_file": "handsoff-acceptance.json",
    "event_log": "handsoff-events.jsonl",
    "verification_log": "handsoff-verifications.jsonl",
    "max_design_rounds": 3,
    "max_review_rounds": 3,
    "stall_minutes": 10,
    "require_live_verification": True,
    "deployment_requires_explicit_approval": True,
    "check_commands": [],
    "live_check_commands": [],
    "check_timeout_seconds": 600,
    "agents": {
        "architect": "auto",
        "supervisor": "auto",
        "implementer": "auto",
        "reviewer": "auto",
    },
    "models": {
        "architect": "default",
        "supervisor": "default",
        "implementer": "default",
        "reviewer": "default",
    },
}

AGENT_ROLES = ("architect", "supervisor", "implementer", "reviewer")
SELECTABLE_AGENT_ROLES = AGENT_ROLES
LEGACY_AGENT_ROLES = ("architect", "implementer", "reviewer")
SELECTABLE_AGENT_ADAPTERS = ("codex", "claude")
AUTO_AGENT_ADAPTER = "auto"
LEGACY_UNCONFIGURED_AGENT_ADAPTER = "configure-me"
AGENT_SETTING_ADAPTERS = (AUTO_AGENT_ADAPTER, *SELECTABLE_AGENT_ADAPTERS)
DEFAULT_AGENT_PREFERENCE = SELECTABLE_AGENT_ADAPTERS
DEFAULT_AGENT_MODEL = "default"
MAX_AGENT_MODEL_LENGTH = 128
MAX_AGENT_ACTOR_LENGTH = 128
MAX_AGENT_SESSION_ID_LENGTH = 64
MAX_AGENT_SESSIONS = 64
AGENT_SESSION_ID_PATTERN = re.compile(r"^hs-[0-9a-f]{32}$")
AGENT_SESSION_LIVE_STATES = {"launching", "running"}
AGENT_SESSION_TERMINAL_STATES = {
    "completed", "failed", "timed_out", "cancelled", "failed_to_start",
}
AGENT_SESSION_STATES = AGENT_SESSION_LIVE_STATES | AGENT_SESSION_TERMINAL_STATES
AGENT_SESSION_RESOLUTION_SOURCES = {"configured", "auto_detected", "legacy_auto_detected"}
AGENT_SESSION_FIELDS = {
    "session_id", "role", "actor", "adapter", "requested_model", "reported_model",
    "resolution_source", "started_at", "running_at", "ended_at", "state", "exit_code",
}

REQUIRED_STATUS_FIELDS = (
    "feature", "phase_number", "phase", "progress", "status", "updated_at",
    "next_action", "events", "requirement_coverage", "verification_head",
)
REQUIRED_COVERAGE_FIELDS = (
    "passing", "failing", "not_tested", "blocked", "original_symptom_resolved",
)

STATUS_VALUES = {"in_progress", "blocked", "ready_to_deploy", "awaiting_approval", "complete"}
VERIFICATION_KINDS = {"checks", "manual", "browser", "live"}
VERIFICATION_REQUIREMENTS = {
    "automated": {"checks"},
    "manual": {"manual"},
    "browser": {"browser"},
    "automated_and_browser": {"checks", "browser"},
}
CHECKLIST_VALUES = {
    "symptom_reproduced": {"yes", "not_applicable"},
    "symptom_resolved": {"yes"},
    "all_criteria_verified": {"yes"},
    "evidence_attached": {"yes"},
}


class HandsoffError(Exception):
    """A config or state file problem that stops us before any gate logic
    runs, distinct from a gate simply refusing a transition."""


# --------------------------------------------------------------------------
# root and config resolution
# --------------------------------------------------------------------------

def resolve_root(explicit: str | None = None) -> Path:
    """The project root, in order: --root, $HANDSOFF_ROOT, the nearest
    ancestor of the current directory that has a handsoff.toml, then the
    current directory itself. Never the script's own directory: that was
    the original bug."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("HANDSOFF_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "handsoff.toml").is_file():
            return candidate
    return here


def load_config(root: Path) -> dict:
    """handsoff.toml, actually read this time. Missing keys fall back to
    DEFAULT_CONFIG rather than erroring, since a fresh project may not have
    customised every field yet."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["agents"] = dict(DEFAULT_CONFIG["agents"])
    cfg["models"] = dict(DEFAULT_CONFIG["models"])
    path = root / "handsoff.toml"
    if not path.is_file():
        return cfg
    if tomllib is None:
        raise HandsoffError("handsoff.toml present but no TOML parser available (need Python 3.11+)")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"cannot load {path}: {exc}") from exc
    project = raw.get("project", {})
    workflow = raw.get("workflow", {})
    agents = raw.get("agents", {})
    models = raw.get("models", {})
    checks = raw.get("checks", {})
    if not all(isinstance(section, dict) for section in (project, workflow, agents, models, checks)):
        raise HandsoffError("handsoff.toml: project, workflow, agents, models, and checks must be tables")
    cfg["status_file"] = project.get("status_file", cfg["status_file"])
    cfg["acceptance_file"] = project.get("acceptance_file", cfg["acceptance_file"])
    cfg["event_log"] = project.get("event_log", cfg["event_log"])
    cfg["verification_log"] = project.get("verification_log", cfg["verification_log"])
    for key in ("max_design_rounds", "max_review_rounds", "stall_minutes"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be an integer")
        cfg[key] = value
    for key in ("require_live_verification", "deployment_requires_explicit_approval"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be boolean")
        cfg[key] = value
    for role in AGENT_ROLES:
        value = agents.get(role, cfg["agents"][role])
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"handsoff.toml: agents.{role} must be a non-empty string")
        cfg["agents"][role] = value.strip()
    for role in SELECTABLE_AGENT_ROLES:
        value = models.get(role, cfg["models"][role])
        cfg["models"][role] = validate_agent_model(value)
    for config_key, toml_key in (("check_commands", "commands"), ("live_check_commands", "live_commands")):
        value = checks.get(toml_key, cfg[config_key])
        if not isinstance(value, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in value):
            raise HandsoffError(f"handsoff.toml: checks.{toml_key} must be an array of non-empty command strings")
        cfg[config_key] = list(value)
    timeout_value = checks.get("timeout_seconds", cfg["check_timeout_seconds"])
    if not isinstance(timeout_value, int) or isinstance(timeout_value, bool) or timeout_value <= 0:
        raise HandsoffError("handsoff.toml: checks.timeout_seconds must be a positive integer")
    cfg["check_timeout_seconds"] = timeout_value
    resolved_root = root.resolve()
    for key in ("status_file", "acceptance_file", "event_log", "verification_log"):
        value = cfg[key]
        if not isinstance(value, str) or not value.strip() or Path(value).is_absolute() or ".." in Path(value).parts:
            raise HandsoffError(f"handsoff.toml: project.{key} must be a safe relative path")
        # The string-only check above rejects ".." and absolute paths, but
        # a symlinked PARENT DIRECTORY defeats it just as completely: the
        # string never says ".." while the real file still lands outside
        # the project. Resolve the full path and confirm it is still a
        # descendant of the root after that resolution, not just lexically.
        try:
            (root / value).resolve().relative_to(resolved_root)
        except ValueError:
            raise HandsoffError(
                f"handsoff.toml: project.{key} resolves outside the project root "
                f"(a parent directory may be a symlink)") from None
    for key in ("max_design_rounds", "max_review_rounds", "stall_minutes"):
        if cfg[key] < 0:
            raise HandsoffError(f"handsoff.toml: {key} must not be negative")
    return cfg


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text file and clean up on failure."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, path.stat().st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def validate_agent_model(value: object) -> str:
    """Validate a literal runner model ID without interpreting it.

    The value is passed as one argv element only after validation. Leading
    dashes are rejected so a model can never be confused for another CLI
    option even if a runner changes how it parses option values.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise HandsoffError("agent model must be a non-empty string without surrounding whitespace")
    if len(value) > MAX_AGENT_MODEL_LENGTH:
        raise HandsoffError(f"agent model must be at most {MAX_AGENT_MODEL_LENGTH} characters")
    if value.startswith("-"):
        raise HandsoffError("agent model must not begin with '-'")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HandsoffError("agent model must not contain control characters")
    return value


def validate_agent_actor(value: object) -> str:
    """Validate a display/audit identity without interpreting it."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise HandsoffError("agent actor must be a non-empty string without surrounding whitespace")
    if len(value) > MAX_AGENT_ACTOR_LENGTH:
        raise HandsoffError(f"agent actor must be at most {MAX_AGENT_ACTOR_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HandsoffError("agent actor must not contain control characters")
    return value


def agent_profiles(cfg: dict) -> dict:
    return {
        role: {"adapter": cfg["agents"][role], "model": cfg["models"][role]}
        for role in SELECTABLE_AGENT_ROLES
    }


def default_agent_adapter(*, which=None) -> str | None:
    """Return the first installed runnable adapter in documented order."""
    lookup = which or shutil.which
    return next((adapter for adapter in DEFAULT_AGENT_PREFERENCE if lookup(adapter)), None)


def resolved_agent_profiles(cfg: dict, *, which=None, require_available: bool = False) -> dict:
    """Resolve auto/unconfigured roles without changing explicit selections."""
    configured = agent_profiles(cfg)
    automatic = default_agent_adapter(which=which)
    resolved = {}
    for role, profile in configured.items():
        adapter = profile["adapter"]
        if adapter in {AUTO_AGENT_ADAPTER, LEGACY_UNCONFIGURED_AGENT_ADAPTER}:
            if automatic is None and require_available:
                raise HandsoffError(
                    "no supported agent adapter is available on PATH; install Codex or Claude Code, "
                    "or choose an explicit installed adapter"
                )
            adapter = automatic
        resolved[role] = {"adapter": adapter, "model": profile["model"]}
    return resolved


def audited_agent_profile(cfg: dict, role: str) -> dict:
    """Snapshot configured and currently effective role selection for audit records."""
    configured = agent_profiles(cfg)[role]
    effective = resolved_agent_profiles(cfg)[role]
    return {
        "adapter": configured["adapter"],
        "model": configured["model"],
        "effective_adapter": effective["adapter"],
    }


def adapter_availability() -> dict:
    """Executable discovery only; callers must not imply runtime readiness."""
    result = {}
    for adapter in SELECTABLE_AGENT_ADAPTERS:
        discovered = shutil.which(adapter)
        executable = str(Path(discovered).resolve()) if discovered else None
        result[adapter] = {"available": executable is not None, "executable": executable}
    return result


#: Providers surfaced read-only in the Agent Settings UI so a user can see
#: what's actually usable before picking an adapter, distinct from
#: SELECTABLE_AGENT_ADAPTERS (the values `update_agent_config` accepts).
#: Each entry is "cli" (detected by executable presence on PATH),
#: "credential" (detected by presence of an environment variable name only
#: -- never its value), or "endpoint" (detected by presence of either an
#: endpoint URL or credential env var name, since a self-hosted
#: OpenAI-compatible server commonly needs no real credential).
PROVIDER_SPECS = (
    {"id": "codex", "label": "Codex CLI", "kind": "cli", "executable": "codex"},
    {"id": "claude", "label": "Claude Code", "kind": "cli", "executable": "claude"},
    {"id": "ollama", "label": "Ollama (local models)", "kind": "cli", "executable": "ollama", "list_models": True},
    {"id": "grok", "label": "Grok", "kind": "credential", "credential_env_var": "XAI_API_KEY"},
    {
        "id": "openai_compatible", "label": "OpenAI-compatible endpoint", "kind": "endpoint",
        "endpoint_env_var": "OPENAI_BASE_URL", "credential_env_var": "OPENAI_API_KEY",
    },
)

#: Ollama's own default local server address; Handsoff does not manage the
#: Ollama service, it only asks this fixed, well-known loopback address
#: whether it's running, the same way `ollama list` would.
OLLAMA_API_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_API_TIMEOUT_SECONDS = 1.5


def _detect_ollama_models() -> list[str]:
    """Best-effort local model listing via Ollama's own local REST API.

    Returns an empty list whenever the server isn't reachable (Ollama
    installed but not running) or answers unexpectedly -- absence of
    models is not an error, since Handsoff never starts or manages the
    Ollama service itself.
    """
    try:
        with urllib.request.urlopen(OLLAMA_API_URL, timeout=OLLAMA_API_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []
    return sorted({
        entry["name"] for entry in payload["models"]
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]
    })


def provider_status() -> dict:
    """Read-only provider discovery for the Agent Settings UI.

    A CLI-kind provider is "detected" if its executable is on PATH, else
    "unavailable" (nothing to configure -- it just isn't installed). A
    credential- or endpoint-kind provider is "detected" if the relevant
    environment variable NAME is present, else "requires_setup", since the
    user can make it work by supplying their own configuration. Only a
    variable's presence is ever checked -- its value is never read,
    displayed, stored, or otherwise touched.
    """
    result = {}
    for spec in PROVIDER_SPECS:
        if spec["kind"] == "cli":
            discovered = shutil.which(spec["executable"])
            executable = str(Path(discovered).resolve()) if discovered else None
            entry = {
                "label": spec["label"],
                "state": "detected" if executable else "unavailable",
                "executable": executable,
            }
            if spec.get("list_models"):
                entry["models"] = _detect_ollama_models() if executable else []
            result[spec["id"]] = entry
        elif spec["kind"] == "credential":
            configured = spec["credential_env_var"] in os.environ
            result[spec["id"]] = {
                "label": spec["label"],
                "state": "detected" if configured else "requires_setup",
                "credential_env_var": spec["credential_env_var"],
            }
        else:  # "endpoint"
            endpoint_configured = spec["endpoint_env_var"] in os.environ
            credential_configured = spec["credential_env_var"] in os.environ
            result[spec["id"]] = {
                "label": spec["label"],
                "state": "detected" if (endpoint_configured or credential_configured) else "requires_setup",
                "endpoint_env_var": spec["endpoint_env_var"],
                "credential_env_var": spec["credential_env_var"],
            }
    return result


def _patch_toml_role_table(lines: list[str], parsed: dict, table: str,
                           assignments: dict[str, str]) -> list[str]:
    section_pattern = re.compile(rf"^\s*\[{re.escape(table)}\]\s*(?:#.*)?(?:\r?\n)?$")
    section_starts = [i for i, line in enumerate(lines)
                      if re.match(r"^\s*\[[^\]]+\]\s*(?:#.*)?(?:\r?\n)?$", line)]
    headers = [i for i in section_starts if section_pattern.match(lines[i])]
    if len(headers) > 1:
        raise HandsoffError(f"handsoff.toml: ambiguous duplicate [{table}] tables")
    if headers:
        start = headers[0]
        end = next((i for i in section_starts if i > start), len(lines))
    elif table in parsed:
        raise HandsoffError(f"handsoff.toml: unsupported [{table}] table syntax")
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        start = len(lines)
        lines.append(f"[{table}]\n")
        end = len(lines)

    patterns = {
        role: re.compile(
            rf"^(\s*{role}\s*=\s*)(?:\"(?:[^\"\\]|\\.)*\"|'[^']*')(\s*(?:#.*)?)(\r?\n)?$"
        ) for role in assignments
    }
    found: set[str] = set()
    for index in range(start + 1, end):
        for role, pattern in patterns.items():
            match = pattern.match(lines[index])
            if not match:
                continue
            if role in found:
                raise HandsoffError(f"handsoff.toml: duplicate {table}.{role} assignment")
            found.add(role)
            newline = match.group(3) or ""
            lines[index] = f"{match.group(1)}{json.dumps(assignments[role])}{match.group(2)}{newline}"

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = [
        f"{role} = {json.dumps(value)}\n" for role, value in assignments.items() if role not in found
    ]
    return lines


def update_agent_config(root: Path, assignments: dict) -> dict:
    """Patch dashboard-selectable role profiles in TOML.

    Legacy `{role: adapter}` calls still update only `[agents]`. New
    `{role: {adapter, model}}` calls atomically update `[agents]` and
    `[models]`. Unknown TOML remains byte-for-byte unchanged.
    """
    submitted = frozenset(assignments) if isinstance(assignments, dict) else frozenset()
    current_shape = frozenset(SELECTABLE_AGENT_ROLES)
    legacy_shape = frozenset(LEGACY_AGENT_ROLES)
    if not isinstance(assignments, dict) or submitted not in {frozenset(current_shape), frozenset(legacy_shape)}:
        raise HandsoffError(
            "agent settings must contain all four roles, or the legacy architect, implementer, and reviewer set"
        )
    legacy = all(isinstance(value, str) for value in assignments.values())
    profiles = all(isinstance(value, dict) for value in assignments.values())
    if not legacy and not profiles:
        raise HandsoffError("agent settings must use either all adapter strings or all role profiles")
    if legacy:
        adapters = assignments
        # An older client knows nothing about model IDs. Reset to the runner
        # default rather than silently retaining a custom model that may be
        # incompatible with the newly selected adapter.
        models = {role: DEFAULT_AGENT_MODEL for role in assignments}
    else:
        if any(set(value) != {"adapter", "model"} for value in assignments.values()):
            raise HandsoffError("each agent profile must contain exactly adapter and model")
        adapters = {role: value["adapter"] for role, value in assignments.items()}
        models = {role: validate_agent_model(value["model"]) for role, value in assignments.items()}
    if any(value not in AGENT_SETTING_ADAPTERS for value in adapters.values()):
        raise HandsoffError("agent adapters must be exactly 'auto', 'codex', or 'claude'")
    path = root / "handsoff.toml"
    if tomllib is None:
        raise HandsoffError("agent settings require Python 3.11+ TOML support")

    with project_lock(root):
        try:
            original = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(original)
        except (OSError, ValueError) as exc:
            raise HandsoffError(f"cannot update {path}: {exc}") from exc
        if "agents" in parsed and not isinstance(parsed["agents"], dict):
            raise HandsoffError("handsoff.toml: agents must be a table")
        if legacy and submitted == legacy_shape:
            raw_models = parsed.get("models", {})
            supervisor_model = raw_models.get("supervisor", DEFAULT_AGENT_MODEL)
            models["supervisor"] = validate_agent_model(supervisor_model)

        lines = _patch_toml_role_table(original.splitlines(keepends=True), parsed, "agents", adapters)
        lines = _patch_toml_role_table(lines, parsed, "models", models)
        proposed = "".join(lines)
        try:
            proposed_raw = tomllib.loads(proposed)
        except ValueError as exc:
            raise HandsoffError(f"refusing invalid proposed handsoff.toml: {exc}") from exc
        effective_agents = proposed_raw.get("agents", {})
        if any(effective_agents.get(role) != adapters[role] for role in adapters):
            raise HandsoffError("refusing ambiguous agent settings update")
        effective_models = proposed_raw.get("models", {})
        if any(effective_models.get(role) != models[role] for role in SELECTABLE_AGENT_ROLES):
            raise HandsoffError("refusing ambiguous agent model update")
        _atomic_write_text(path, proposed)
    if legacy:
        return {role: adapters[role] for role in assignments}
    return {role: {"adapter": adapters[role], "model": models[role]}
            for role in SELECTABLE_AGENT_ROLES}


def status_path(root: Path, cfg: dict) -> Path:
    return root / cfg["status_file"]


def acceptance_path(root: Path, cfg: dict) -> Path:
    return root / cfg["acceptance_file"]


def event_log_path(root: Path, cfg: dict) -> Path:
    return root / cfg["event_log"]


def verification_log_path(root: Path, cfg: dict) -> Path:
    return root / cfg["verification_log"]


def lock_path(root: Path) -> Path:
    return root / ".handsoff.lock"


def event_head_path(root: Path) -> Path:
    return root / ".handsoff-event-head.json"


def write_ahead_path(root: Path) -> Path:
    return root / ".handsoff-writeahead.json"


@contextmanager
def project_lock(root: Path):
    """Advisory single-writer lock around a read-modify-write. Best effort:
    on a platform without fcntl this is a no-op, which is a known
    limitation (see README), not a silent claim of safety it cannot keep."""
    if fcntl is None:
        yield
        return
    lp = lock_path(root)
    lp.touch(exist_ok=True)
    with lp.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# JSON I/O: duplicate-key detection, atomic writes
# --------------------------------------------------------------------------

def load_unique_json(path: Path) -> dict:
    """Parse JSON, rejecting a duplicate top-level-or-nested key rather than
    silently keeping the last one, the way plain json.loads would."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise HandsoffError(f"duplicate JSON key '{key}' in {path}")
            out[key] = value
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HandsoffError(f"cannot read {path}: {e}") from e
    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as e:
        raise HandsoffError(f"invalid JSON in {path}: {e}") from e


def atomic_write_json(path: Path, data: dict) -> None:
    """Write a whole new file, or not at all. A process killed mid-write
    leaves either the old file or the new one, never a truncated one: the
    write lands in a sibling temp file first and os.replace is atomic on
    the same filesystem. A normal exception during the write cleans up its
    temp file; a SIGKILL or power loss between the write and the rename
    can still leave a stray .tmp<pid> file behind (harmless, the real file
    is untouched; see README Known limitations)."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# write-ahead journal: lets `doctor` prove a crash, not launder a hand edit
# --------------------------------------------------------------------------

def _serialized_digest(data: dict) -> str:
    """The sha256 of the exact bytes atomic_write_json would produce for
    this value, in the same format, so a later comparison against the
    real file on disk (hashed the same way _file_sha256 does) can never
    mismatch on serialization alone."""
    return hashlib.sha256((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")).hexdigest()


def write_ahead(root: Path, *, status: dict | None = None, acceptance: dict | None = None) -> None:
    """Record, BEFORE any target file is touched, exactly which file(s)
    this in-flight write intends to produce and their exact resulting
    content. This is what lets `doctor` tell a genuinely interrupted
    write apart from an unrelated hand edit that merely happens to still
    validate: doctor will only re-anchor the event log to a state that
    exactly matches a journal entry naming it as the intended result of
    a real command, never to any state that simply passes the gates.
    Caller must hold project_lock and call clear_write_ahead once the
    matching commit (through its append_event) has completed; see
    commit()."""
    entry: dict = {"at": datetime.now(timezone.utc).isoformat()}
    if status is not None:
        entry["status_sha256"] = _serialized_digest(status)
    if acceptance is not None:
        entry["acceptance_sha256"] = _serialized_digest(acceptance)
    atomic_write_json(write_ahead_path(root), entry)


def clear_write_ahead(root: Path) -> None:
    try:
        write_ahead_path(root).unlink()
    except OSError:
        pass


def read_write_ahead(root: Path) -> dict | None:
    path = write_ahead_path(root)
    if not path.exists():
        return None
    try:
        return load_unique_json(path)
    except HandsoffError:
        return None


def commit(root: Path, cfg: dict, *, status: dict | None = None, acceptance: dict | None = None,
          event_kind: str, event_message: str, extra_events: list[dict] | None = None,
          **event_extra) -> str:
    """Write status and/or acceptance, and append the event(s) describing
    them, as one write-ahead-journaled unit. Every mutating command uses
    this instead of calling atomic_write_json/append_event directly, so
    every real write leaves the journal `doctor` needs to recover it
    safely. Caller must hold project_lock for the entire surrounding
    read-validate-write, not just this call.

    `extra_events`, if given, are appended (in list order) BEFORE the
    primary event_kind/event_message, so a single state transition that is
    really two consecutive facts (a design round ending because the next
    one just started, say) can log both without a second write-ahead cycle
    or a second caller of commit(). Each entry is {"kind": ..., "message":
    ..., **extra}. Returns the hash of the PRIMARY event only; callers that
    need an extra event's own hash should read it back from the log."""
    write_ahead(root, status=status, acceptance=acceptance)
    if acceptance is not None:
        atomic_write_json(acceptance_path(root, cfg), acceptance)
    if status is not None:
        atomic_write_json(status_path(root, cfg), status)
    for extra in extra_events or ():
        rest = {k: v for k, v in extra.items() if k not in ("kind", "message")}
        append_event(root, cfg, extra["kind"], extra["message"], **rest)
    event_hash = append_event(root, cfg, event_kind, event_message, **event_extra)
    clear_write_ahead(root)
    return event_hash


# --------------------------------------------------------------------------
# managed-agent runtime telemetry
# --------------------------------------------------------------------------

def default_agent_actor(adapter: str, role: str) -> str:
    """Stable documented identity used when ``handsoff_agent launch`` omits --by."""
    if adapter not in SELECTABLE_AGENT_ADAPTERS or role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("cannot derive an actor for an unsupported agent adapter or role")
    return f"{adapter}-{role}"


def _new_agent_session_id(sessions: dict, *, id_factory=None) -> str:
    factory = id_factory or (lambda: f"hs-{uuid.uuid4().hex}")
    for _attempt in range(16):
        candidate = factory()
        if not isinstance(candidate, str) or len(candidate) > MAX_AGENT_SESSION_ID_LENGTH \
                or not AGENT_SESSION_ID_PATTERN.fullmatch(candidate):
            raise HandsoffError("generated agent session id is invalid")
        if candidate not in sessions:
            return candidate
    raise HandsoffError("could not allocate a collision-free agent session id")


def _prune_agent_sessions(sessions: dict, current: dict) -> None:
    """Bound status growth while retaining every role's current snapshot."""
    protected = {value for value in current.values() if isinstance(value, str)}
    removable = sorted(
        (session for sid, session in sessions.items()
         if sid not in protected and session.get("state") in AGENT_SESSION_TERMINAL_STATES),
        key=lambda session: (session.get("started_at", ""), session.get("session_id", "")),
    )
    while len(sessions) >= MAX_AGENT_SESSIONS and removable:
        sessions.pop(removable.pop(0)["session_id"], None)
    if len(sessions) >= MAX_AGENT_SESSIONS:
        raise HandsoffError("agent session history is full; no terminal session can be retired safely")


def _assert_agent_telemetry_integrity(root: Path, cfg: dict, status: dict) -> None:
    """Refuse telemetry writes over stale or unauthenticated workflow state."""
    problems = [f"event log: {problem}" for problem in verify_event_log(root, cfg)]
    records, verification_problems = load_verifications(root, cfg)
    problems.extend(f"verification ledger: {problem}" for problem in verification_problems)
    actual_head = records[-1].get("hash") if records else "GENESIS"
    if status.get("verification_head") != actual_head:
        problems.append("verification ledger: tail does not match the anchored head")
    if problems:
        raise HandsoffError(problems[0])


def create_agent_session(root: Path, *, role: str, actor: str, adapter: str,
                         requested_model: str, resolution_source: str,
                         id_factory=None) -> dict:
    """Commit the immutable launch snapshot before a managed child starts.

    The task/prompt, environment, runner output, credentials, and token data
    are deliberately not accepted by this API, so callers cannot accidentally
    persist them as telemetry.
    """
    root = root.resolve()
    if role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("agent session role must be architect, supervisor, implementer, or reviewer")
    actor = validate_agent_actor(actor)
    if adapter not in SELECTABLE_AGENT_ADAPTERS:
        raise HandsoffError("agent session adapter must be codex or claude")
    requested_model = validate_agent_model(requested_model)
    if resolution_source not in AGENT_SESSION_RESOLUTION_SOURCES:
        raise HandsoffError("agent session resolution source is invalid")
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        schema_errors = validate_status_schema(status)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        sessions = deepcopy(status.get("agent_sessions") or {})
        current = deepcopy(status.get("current_agent_sessions") or {})
        active_id = current.get(role)
        active = sessions.get(active_id) if isinstance(active_id, str) else None
        if active and active.get("state") in AGENT_SESSION_LIVE_STATES:
            raise HandsoffError(
                f"role {role} already has live agent session {active_id} ({active.get('state')})"
            )
        _prune_agent_sessions(sessions, current)
        session_id = _new_agent_session_id(sessions, id_factory=id_factory)
        now = datetime.now(timezone.utc).isoformat()
        session = {
            "session_id": session_id,
            "role": role,
            "actor": actor,
            "adapter": adapter,
            "requested_model": requested_model,
            "reported_model": None,
            "resolution_source": resolution_source,
            "started_at": now,
            "running_at": None,
            "ended_at": None,
            "state": "launching",
            "exit_code": None,
        }
        sessions[session_id] = session
        current[role] = session_id
        proposed = deepcopy(status)
        proposed["agent_sessions"] = sessions
        proposed["current_agent_sessions"] = current
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        commit(
            root, cfg, status=proposed,
            event_kind="agent_session_launching",
            event_message=f"Managed {role} agent session is launching",
            session_id=session_id, role=role, actor=actor, adapter=adapter,
            requested_model=requested_model, reported_model=None,
            resolution_source=resolution_source, state="launching",
        )
        return deepcopy(session)


def transition_agent_session(root: Path, session_id: str, state: str,
                             *, exit_code: int | None = None) -> dict:
    """Apply a session-ID-matched lifecycle-only update under the lock."""
    if not isinstance(session_id, str) or not AGENT_SESSION_ID_PATTERN.fullmatch(session_id):
        raise HandsoffError("agent session id is invalid")
    if state not in AGENT_SESSION_STATES - {"launching"}:
        raise HandsoffError("agent session lifecycle state is invalid")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise HandsoffError("agent session exit code must be an integer or null")
    terminal = state in AGENT_SESSION_TERMINAL_STATES
    if not terminal and exit_code is not None:
        raise HandsoffError("a non-terminal agent session cannot have an exit code")
    with project_lock(root.resolve()):
        root = root.resolve()
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        schema_errors = validate_status_schema(status)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        sessions = status.get("agent_sessions")
        current = status.get("current_agent_sessions")
        if not isinstance(sessions, dict) or not isinstance(current, dict):
            raise HandsoffError("agent session telemetry is not present")
        existing = sessions.get(session_id)
        if not isinstance(existing, dict):
            raise HandsoffError(f"agent session {session_id} was not found")
        role = existing.get("role")
        if current.get(role) != session_id:
            raise HandsoffError(f"stale agent session {session_id} is no longer current for role {role}")
        old_state = existing.get("state")
        allowed = {
            "launching": {"running", "failed_to_start"},
            "running": {"completed", "failed", "timed_out", "cancelled"},
        }
        if state not in allowed.get(old_state, set()):
            raise HandsoffError(f"agent session cannot transition from {old_state} to {state}")
        proposed = deepcopy(status)
        updated = proposed["agent_sessions"][session_id]
        now = datetime.now(timezone.utc).isoformat()
        updated["state"] = state
        if state == "running":
            updated["running_at"] = now
        else:
            updated["ended_at"] = now
            updated["exit_code"] = exit_code
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        event_kind = f"agent_session_{state}"
        commit(
            root, cfg, status=proposed,
            event_kind=event_kind,
            event_message=f"Managed {role} agent session is {state.replace('_', ' ')}",
            session_id=session_id, role=role, state=state, exit_code=exit_code,
        )
        return deepcopy(updated)


def current_agent_sessions(status: dict) -> dict:
    """Return each role's recorded current session, without inference."""
    sessions = status.get("agent_sessions") if isinstance(status, dict) else None
    pointers = status.get("current_agent_sessions") if isinstance(status, dict) else None
    if not isinstance(sessions, dict) or not isinstance(pointers, dict):
        return {role: None for role in SELECTABLE_AGENT_ROLES}
    return {
        role: deepcopy(sessions.get(pointers.get(role)))
        if isinstance(sessions.get(pointers.get(role)), dict) else None
        for role in SELECTABLE_AGENT_ROLES
    }


# --------------------------------------------------------------------------
# tamper-evident event log
# --------------------------------------------------------------------------

def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return "GENESIS"
    last = ""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return "GENESIS"
    return json.loads(last)["hash"]


def append_event(root: Path, cfg: dict, kind: str, message: str, **extra) -> str:
    path = event_log_path(root, cfg)
    prev_hash = _last_hash(path)
    body = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "message": message,
            "prev_hash": prev_hash,
            "status_sha256": _file_sha256(status_path(root, cfg)),
            "acceptance_sha256": _file_sha256(acceptance_path(root, cfg)), **extra}
    body["hash"] = hashlib.sha256((_canonical(body) + prev_hash).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(body) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    atomic_write_json(event_head_path(root), {"hash": body["hash"]})
    return body["hash"]


def event_log_chain_errors(root: Path, cfg: dict) -> tuple[list[str], dict | None]:
    """Walk the chain and check only its own internal integrity: hash
    linkage, tail anchor, parseable JSON. Deliberately excludes the
    status/acceptance freshness cross-check (see verify_event_log) so a
    caller like `doctor` can tell tampering (unrecoverable) apart from a
    merely-stale anchor (recoverable). Returns (problems, last_record)."""
    path = event_log_path(root, cfg)
    head_path = event_head_path(root)
    if not path.exists():
        problems = ["event log is missing but its chain head exists"] if head_path.exists() else []
        return problems, None
    problems = []
    prev_hash = "GENESIS"
    last_record = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {lineno}: not valid JSON")
            continue
        claimed_hash = record.get("hash")
        last_record = record
        recomputed_body = {k: v for k, v in record.items() if k != "hash"}
        if record.get("prev_hash") != prev_hash:
            problems.append(f"line {lineno}: prev_hash does not match the previous record, log was edited or reordered")
        expected = hashlib.sha256((_canonical(recomputed_body) + recomputed_body.get("prev_hash", "")).encode("utf-8")).hexdigest()
        if claimed_hash != expected:
            problems.append(f"line {lineno}: hash does not match its own content, record was edited in place")
        prev_hash = claimed_hash or prev_hash
    if head_path.exists():
        try:
            anchored = load_unique_json(head_path).get("hash")
            if anchored != prev_hash:
                problems.append("event log tail does not match its anchored head; records were deleted or an append was interrupted")
        except HandsoffError as exc:
            problems.append(str(exc))
    else:
        problems.append("event log chain head is missing")
    return problems, last_record


def verify_event_log(root: Path, cfg: dict) -> list[str]:
    """Walk the chain; return a list of problems, empty if it is intact.
    Combines chain integrity with the status/acceptance freshness check:
    the latest event must describe the files as they currently are."""
    problems, last_record = event_log_chain_errors(root, cfg)
    if last_record is not None:
        if last_record.get("status_sha256") != _file_sha256(status_path(root, cfg)):
            problems.append("status file does not match the state recorded by the latest event")
        if last_record.get("acceptance_sha256") != _file_sha256(acceptance_path(root, cfg)):
            problems.append("acceptance file does not match the state recorded by the latest event")
    return problems


# --------------------------------------------------------------------------
# immutable verification ledger
# --------------------------------------------------------------------------

def criterion_spec_hash(criterion: dict) -> str:
    """Hash the claim being verified, excluding mutable outcome fields and
    authored_by -- the last is provenance metadata about who proposed the
    criterion, not part of the claim being verified, so stamping it at
    design-approve time can never change this hash, invalidate an
    already-recorded evidence binding, or mismatch a freshly recomputed
    design_hash (which is built from this same hash per criterion)."""
    spec = {k: v for k, v in criterion.items() if k not in {"state", "evidence", "authored_by"}}
    return hashlib.sha256(_canonical(spec).encode("utf-8")).hexdigest()


def append_verification(root: Path, cfg: dict, *, kind: str, ok: bool,
                        by: str, criteria: list[dict], results: list[dict] | None = None,
                        description: str | None = None,
                        acceptance_digest: str | None = None,
                        config_digest: str | None = None) -> dict:
    """Append a hash-chained evidence record. Caller must hold project_lock.

    The chain proves a record was not altered AFTER it was written; it says
    nothing about whether the record was meaningful WHEN it was written.
    That is what this validates: an empty actor, an unknown kind, or a
    record naming zero criteria would hash and chain just as cleanly as a
    real one, so those are rejected here, structurally, before anything
    is appended."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("verification record: 'by' must be a non-empty string")
    if kind not in VERIFICATION_KINDS:
        raise HandsoffError(f"verification record: 'kind' must be one of {sorted(VERIFICATION_KINDS)}")
    if not criteria or not all(isinstance(c, dict) and c.get("id") for c in criteria):
        raise HandsoffError("verification record: 'criteria' must be a non-empty list of criteria with ids")
    path = verification_log_path(root, cfg)
    prev_hash = _last_hash(path)
    record = {
        "run_id": f"vr-{uuid.uuid4().hex}",
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "ok": bool(ok),
        "by": by,
        "criteria": [c["id"] for c in criteria],
        "criterion_hashes": {c["id"]: criterion_spec_hash(c) for c in criteria},
        "results": results or [],
        "description": description or "",
        "acceptance_hash": acceptance_digest,
        "config_hash": config_digest,
        "prev_hash": prev_hash,
    }
    record["hash"] = hashlib.sha256((_canonical(record) + prev_hash).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def load_verifications(root: Path, cfg: dict) -> tuple[list[dict], list[str]]:
    """Read and authenticate the verification ledger."""
    path = verification_log_path(root, cfg)
    if not path.exists():
        return [], []
    records: list[dict] = []
    problems: list[str] = []
    prev_hash = "GENESIS"
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            problems.append(f"verification line {lineno}: not valid JSON")
            continue
        if not isinstance(record, dict):
            problems.append(f"verification line {lineno}: record must be an object")
            continue
        claimed_hash = record.get("hash")
        body = {k: v for k, v in record.items() if k != "hash"}
        if body.get("prev_hash") != prev_hash:
            problems.append(f"verification line {lineno}: prev_hash mismatch")
        expected = hashlib.sha256((_canonical(body) + str(body.get("prev_hash", ""))).encode("utf-8")).hexdigest()
        if claimed_hash != expected:
            problems.append(f"verification line {lineno}: content hash mismatch")
        # A hand-crafted record can chain and hash perfectly while still
        # being empty or meaningless: the hash proves nothing was altered
        # AFTER it was written, not that it was ever real evidence. Every
        # loaded record is re-checked against the same structural rules
        # append_verification enforces on the way in.
        by = record.get("by")
        if not isinstance(by, str) or not by.strip():
            problems.append(f"verification line {lineno}: 'by' must be a non-empty string")
        if record.get("kind") not in VERIFICATION_KINDS:
            problems.append(f"verification line {lineno}: 'kind' must be one of {sorted(VERIFICATION_KINDS)}")
        criteria_ids = record.get("criteria")
        if not criteria_ids or not isinstance(criteria_ids, list) or not all(
                isinstance(c, str) and c for c in criteria_ids):
            problems.append(f"verification line {lineno}: 'criteria' must be a non-empty list of criterion ids")
        if not isinstance(record.get("ok"), bool):
            problems.append(f"verification line {lineno}: 'ok' must be a boolean")
        records.append(record)
        prev_hash = claimed_hash or prev_hash
    return records, problems


# --------------------------------------------------------------------------
# schema: minimal, stdlib-only (no jsonschema dependency, matching the
# project's own "portable, no assumptions" stance)
# --------------------------------------------------------------------------

def validate_acceptance_schema(acceptance: dict) -> list[str]:
    errors = []
    if not isinstance(acceptance, dict):
        return ["acceptance: top-level value must be an object"]
    if "feature" not in acceptance:
        errors.append("acceptance: missing 'feature'")
    elif not isinstance(acceptance["feature"], str) or not acceptance["feature"].strip():
        errors.append("acceptance: 'feature' must be a non-empty string")
    criteria = acceptance.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("acceptance: 'criteria' must be a non-empty array")
        return errors
    seen_ids = set()
    for c in criteria:
        if not isinstance(c, dict):
            errors.append("acceptance: every criterion must be an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid.strip():
            errors.append("acceptance: a criterion is missing 'id'")
        elif cid in seen_ids:
            errors.append(f"acceptance: duplicate criterion id '{cid}'")
        else:
            seen_ids.add(cid)
        if c.get("type") not in ("primary_fix", "supporting"):
            errors.append(f"acceptance: criterion {cid} has an invalid 'type'")
        if c.get("state") not in ("failing", "passing", "not_tested", "blocked"):
            errors.append(f"acceptance: criterion {cid} has an invalid 'state'")
        for field in ("requirement", "verification"):
            if not isinstance(c.get(field), str) or not c.get(field, "").strip():
                errors.append(f"acceptance: criterion {cid} missing '{field}'")
        if c.get("verification") not in VERIFICATION_REQUIREMENTS:
            errors.append(f"acceptance: criterion {cid} has an invalid 'verification' policy")
        tests = c.get("tests")
        evidence = c.get("evidence")
        if not isinstance(tests, list) or not all(isinstance(x, str) and x.strip() for x in tests):
            errors.append(f"acceptance: criterion {cid} 'tests' must be an array of non-empty strings")
        if not isinstance(evidence, list) or not all(isinstance(x, str) and x.strip() for x in evidence):
            errors.append(f"acceptance: criterion {cid} 'evidence' must be an array of verification run ids")
        if not tests and not evidence:
            errors.append(f"acceptance: criterion {cid} has no linked tests or evidence")
        if "authored_by" in c and c["authored_by"] is not None:
            if not isinstance(c["authored_by"], str) or not c["authored_by"].strip():
                errors.append(f"acceptance: criterion {cid} 'authored_by' must be a non-empty string or null")
    if not any(isinstance(c, dict) and c.get("type") == "primary_fix" for c in criteria):
        errors.append("acceptance: at least one primary_fix criterion is required")
    return errors


def _is_number(value) -> bool:
    """True only for a finite, real number. json.loads accepts NaN and
    Infinity as an extension, and both pass isinstance(x, float) while
    still crashing int()/float() arithmetic downstream (ValueError for
    NaN, OverflowError for Infinity) -- round 3 finding: this used to
    let a hand-edited "design_round": NaN through the schema check clean,
    then crash inside compute_errors."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def validate_status_schema(status: dict) -> list[str]:
    """Every field compute_errors later casts with int()/float() is
    type-checked HERE first. Skipping this and letting a bad cast raise
    was a real bug: a hand-edited status.json with e.g.
    "design_round": "not-a-number" crashed the CLI with a raw traceback
    instead of a clean SHIP_FEATURE_BLOCKED."""
    if not isinstance(status, dict):
        return ["status: top-level value must be an object"]
    errors = [f"status: missing required field '{f}'" for f in REQUIRED_STATUS_FIELDS if f not in status]
    coverage = status.get("requirement_coverage", {})
    if not isinstance(coverage, dict):
        errors.append("status: 'requirement_coverage' must be an object")
        coverage = {}
    errors += [f"status: requirement_coverage missing '{f}'" for f in REQUIRED_COVERAGE_FIELDS if f not in coverage]
    if "phase_number" in status and (not isinstance(status["phase_number"], int)
                                      or isinstance(status["phase_number"], bool)
                                      or status["phase_number"] not in PHASES):
        errors.append(f"status: phase_number {status['phase_number']} is not one of {sorted(PHASES)}")
    elif "phase_number" in status and status.get("phase") != PHASES[status["phase_number"]]:
        errors.append("status: 'phase' does not match 'phase_number'")
    if "progress" in status and (not _is_number(status["progress"]) or not 0 <= status["progress"] <= 100):
        errors.append(f"status: 'progress' must be a finite number from 0 to 100, got {status['progress']!r}")
    for field in ("design_round", "review_round", "retry_count"):
        if field in status and (not isinstance(status[field], int) or isinstance(status[field], bool) or status[field] < 0):
            errors.append(f"status: '{field}' must be a non-negative integer, got {status[field]!r}")
    for field in ("feature", "phase", "status", "updated_at", "next_action"):
        if field in status and (not isinstance(status[field], str) or not status[field].strip()):
            errors.append(f"status: '{field}' must be a non-empty string")
    if "status" in status and status["status"] not in STATUS_VALUES:
        errors.append(f"status: invalid status value {status['status']!r}")
    if "updated_at" in status and isinstance(status["updated_at"], str):
        try:
            parsed = datetime.fromisoformat(status["updated_at"])
            if parsed.tzinfo is None:
                errors.append("status: 'updated_at' must include a timezone")
        except ValueError:
            errors.append("status: 'updated_at' must be an ISO-8601 timestamp")
    # 'last_heartbeat_at' is optional and nullable (absent entirely on any
    # status.json written before this field existed, in this repo or any
    # other project already running Handsoff) but when PRESENT it is held
    # to the same timestamp discipline as 'updated_at', so a malformed
    # heartbeat is refused as a schema error rather than silently read as
    # fresh by stall_warning()/activity_note().
    if "last_heartbeat_at" in status and status["last_heartbeat_at"] is not None:
        if not isinstance(status["last_heartbeat_at"], str) or not status["last_heartbeat_at"].strip():
            errors.append("status: 'last_heartbeat_at' must be a non-empty string or null")
        else:
            try:
                parsed = datetime.fromisoformat(status["last_heartbeat_at"])
                if parsed.tzinfo is None:
                    errors.append("status: 'last_heartbeat_at' must include a timezone")
            except ValueError:
                errors.append("status: 'last_heartbeat_at' must be an ISO-8601 timestamp")
    if "events" in status and not isinstance(status["events"], list):
        errors.append("status: 'events' must be an array")
    for field in ("passing", "failing", "not_tested", "blocked"):
        if field in coverage and (not isinstance(coverage[field], int) or isinstance(coverage[field], bool) or coverage[field] < 0):
            errors.append(f"status: requirement_coverage.{field} must be a non-negative integer")
    if "original_symptom_resolved" in coverage and not isinstance(coverage["original_symptom_resolved"], bool):
        errors.append("status: requirement_coverage.original_symptom_resolved must be boolean")
    for field in ("implemented_by", "reviewed_by", "original_symptom_evidence_id", "live_verification_id"):
        if field in status and status[field] is not None and (not isinstance(status[field], str) or not status[field].strip()):
            errors.append(f"status: '{field}' must be a non-empty string or null")
    for field in ("requires_design_approval", "requires_design_review"):
        if field in status and not isinstance(status[field], bool):
            errors.append(f"status: '{field}' must be a boolean")
    for field, record_name in (("review", "review"), ("deployment_approved", "deployment_approved")):
        record = status.get(field)
        if record is None:
            continue
        if not isinstance(record, dict):
            errors.append(f"status: '{field}' must be an object or null")
            continue
        for sub in ("by", "at", "acceptance_hash"):
            if sub not in record or not isinstance(record[sub], str) or not record[sub].strip():
                errors.append(f"status: '{record_name}.{sub}' must be a non-empty string")
        if "at" in record and isinstance(record["at"], str):
            try:
                parsed = datetime.fromisoformat(record["at"])
                if parsed.tzinfo is None:
                    errors.append(f"status: '{record_name}.at' must include a timezone")
            except ValueError:
                errors.append(f"status: '{record_name}.at' must be an ISO-8601 timestamp")
    design_approved = status.get("design_approved")
    if design_approved is not None:
        if not isinstance(design_approved, dict):
            errors.append("status: 'design_approved' must be an object or null")
        else:
            for sub in ("by", "architect", "at", "design_hash"):
                if sub not in design_approved or not isinstance(design_approved[sub], str) \
                        or not design_approved[sub].strip():
                    errors.append(f"status: 'design_approved.{sub}' must be a non-empty string")
            if "at" in design_approved and isinstance(design_approved["at"], str):
                try:
                    parsed = datetime.fromisoformat(design_approved["at"])
                    if parsed.tzinfo is None:
                        errors.append("status: 'design_approved.at' must include a timezone")
                except ValueError:
                    errors.append("status: 'design_approved.at' must be an ISO-8601 timestamp")
            if "redesigns_settled_work" in design_approved and design_approved["redesigns_settled_work"] is not None \
                    and (not isinstance(design_approved["redesigns_settled_work"], str)
                         or not design_approved["redesigns_settled_work"].strip()):
                errors.append("status: 'design_approved.redesigns_settled_work' must be a non-empty string or null")
            # Optional, like redesigns_settled_work: a run approved before
            # this field existed (or a hand-crafted fixture) has none, and
            # that stays valid -- only a present-but-malformed value refuses.
            if "summary" in design_approved and design_approved["summary"] is not None \
                    and (not isinstance(design_approved["summary"], str) or not design_approved["summary"].strip()):
                errors.append("status: 'design_approved.summary' must be a non-empty string or null")
    design_review = status.get("design_review")
    if design_review is not None:
        if not isinstance(design_review, dict):
            errors.append("status: 'design_review' must be an object or null")
        else:
            for sub in ("by", "architect", "at", "decision", "summary", "design_hash", "config_hash"):
                if sub not in design_review or not isinstance(design_review[sub], str) \
                        or not design_review[sub].strip():
                    errors.append(f"status: 'design_review.{sub}' must be a non-empty string")
            if design_review.get("decision") not in {"approved", "changes_requested"}:
                errors.append("status: 'design_review.decision' must be 'approved' or 'changes_requested'")
            if "at" in design_review and isinstance(design_review["at"], str):
                try:
                    parsed = datetime.fromisoformat(design_review["at"])
                    if parsed.tzinfo is None:
                        errors.append("status: 'design_review.at' must include a timezone")
                except ValueError:
                    errors.append("status: 'design_review.at' must be an ISO-8601 timestamp")
    review = status.get("review")
    if isinstance(review, dict) and "checklist" in review and not isinstance(review["checklist"], dict):
        errors.append("status: 'review.checklist' must be an object")
    if isinstance(review, dict):
        for field in ("implementer_profile", "reviewer_profile"):
            profile = review.get(field)
            if profile is None:
                continue
            if not isinstance(profile, dict):
                errors.append(f"status: 'review.{field}' must be an object")
                continue
            for key in ("adapter", "model"):
                if not isinstance(profile.get(key), str) or not profile[key].strip():
                    errors.append(f"status: 'review.{field}.{key}' must be a non-empty string")
            effective = profile.get("effective_adapter")
            if effective is not None and (not isinstance(effective, str) or not effective.strip()):
                errors.append(f"status: 'review.{field}.effective_adapter' must be a non-empty string or null")
        if "profiles_distinct" in review and not isinstance(review["profiles_distinct"], bool):
            errors.append("status: 'review.profiles_distinct' must be boolean")
    sessions = status.get("agent_sessions")
    pointers = status.get("current_agent_sessions")
    if sessions is not None:
        if not isinstance(sessions, dict):
            errors.append("status: 'agent_sessions' must be an object")
            sessions = {}
        elif len(sessions) > MAX_AGENT_SESSIONS:
            errors.append(f"status: 'agent_sessions' must contain at most {MAX_AGENT_SESSIONS} sessions")
        for session_id, session in sessions.items():
            label = f"status: agent session {session_id!r}"
            if not isinstance(session_id, str) or len(session_id) > MAX_AGENT_SESSION_ID_LENGTH \
                    or not AGENT_SESSION_ID_PATTERN.fullmatch(session_id):
                errors.append(f"{label} has an invalid session id")
            if not isinstance(session, dict):
                errors.append(f"{label} must be an object")
                continue
            missing = AGENT_SESSION_FIELDS - set(session)
            if missing:
                errors.append(f"{label} is missing fields: {', '.join(sorted(missing))}")
            unexpected = set(session) - AGENT_SESSION_FIELDS
            if unexpected:
                errors.append(f"{label} contains unsupported fields: {', '.join(sorted(unexpected))}")
            if session.get("session_id") != session_id:
                errors.append(f"{label} session_id must match its object key")
            if session.get("role") not in SELECTABLE_AGENT_ROLES:
                errors.append(f"{label} has an invalid role")
            try:
                validate_agent_actor(session.get("actor"))
            except HandsoffError as exc:
                errors.append(f"{label}: {exc}")
            if session.get("adapter") not in SELECTABLE_AGENT_ADAPTERS:
                errors.append(f"{label} has an invalid adapter")
            try:
                validate_agent_model(session.get("requested_model"))
            except HandsoffError as exc:
                errors.append(f"{label}: requested_model: {exc}")
            reported_model = session.get("reported_model")
            if reported_model is not None:
                try:
                    validate_agent_model(reported_model)
                except HandsoffError as exc:
                    errors.append(f"{label}: reported_model: {exc}")
            if session.get("resolution_source") not in AGENT_SESSION_RESOLUTION_SOURCES:
                errors.append(f"{label} has an invalid resolution_source")
            session_state = session.get("state")
            if session_state not in AGENT_SESSION_STATES:
                errors.append(f"{label} has an invalid state")
            for time_field in ("started_at", "running_at", "ended_at"):
                value = session.get(time_field)
                if value is None and time_field != "started_at":
                    continue
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{label}.{time_field} must be an ISO-8601 timestamp or null")
                    continue
                try:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        errors.append(f"{label}.{time_field} must include a timezone")
                except ValueError:
                    errors.append(f"{label}.{time_field} must be an ISO-8601 timestamp or null")
            exit_code = session.get("exit_code")
            if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
                errors.append(f"{label}.exit_code must be an integer or null")
            if session_state in AGENT_SESSION_LIVE_STATES and session.get("ended_at") is not None:
                errors.append(f"{label} cannot have ended_at while live")
            if session_state in AGENT_SESSION_TERMINAL_STATES and session.get("ended_at") is None:
                errors.append(f"{label} terminal state requires ended_at")
            if session_state == "launching" and session.get("running_at") is not None:
                errors.append(f"{label} launching state cannot have running_at")
            if session_state not in {"launching", "failed_to_start"} and session.get("running_at") is None:
                errors.append(f"{label} state {session_state!r} requires running_at")
    if pointers is not None:
        if not isinstance(pointers, dict):
            errors.append("status: 'current_agent_sessions' must be an object")
        else:
            for role, session_id in pointers.items():
                if role not in SELECTABLE_AGENT_ROLES:
                    errors.append(f"status: current_agent_sessions has invalid role {role!r}")
                    continue
                if not isinstance(session_id, str) or not AGENT_SESSION_ID_PATTERN.fullmatch(session_id):
                    errors.append(f"status: current_agent_sessions.{role} must be a valid session id")
                elif not isinstance(sessions, dict) or session_id not in sessions:
                    errors.append(f"status: current_agent_sessions.{role} points to a missing session")
                elif not isinstance(sessions[session_id], dict) or sessions[session_id].get("role") != role:
                    errors.append(f"status: current_agent_sessions.{role} points to a different role")
    if (sessions is None) != (pointers is None):
        errors.append("status: agent_sessions and current_agent_sessions must be present together")
    return errors


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

GOVERNANCE_CONFIG_KEYS = (
    "deployment_requires_explicit_approval", "require_live_verification",
    "max_design_rounds", "max_review_rounds", "stall_minutes",
)


def config_hash(cfg: dict) -> str:
    """Binds a review, a deployment approval, or a live verification to the
    governance policy in force when it was recorded. Without this, someone
    could flip deployment_requires_explicit_approval or
    require_live_verification off AFTER a review, silently downgrading what
    the workflow requires without invalidating anything already granted."""
    return hashlib.sha256(_canonical({k: cfg.get(k) for k in GOVERNANCE_CONFIG_KEYS}).encode("utf-8")).hexdigest()


def acceptance_hash(criteria: list[dict]) -> str:
    """Binds a deployment approval to the exact criteria states it was
    given against. If the registry changes afterward (a criterion flips,
    one is added or removed), this changes too, and the Phase 8 gate
    refuses the now-stale approval rather than honoring it blindly.

    Sorted by id first: `_canonical` sorts each dict's own keys but not
    list order, so a harmless reordering of the criteria array (a
    re-save, a merge) would otherwise change the hash and falsely
    invalidate a still-valid approval. Sorting fails safe either way,
    over-blocking rather than under-blocking, but there is no reason to
    pay for it when the content genuinely has not changed."""
    ordered = sorted(criteria, key=lambda c: c.get("id") or "")
    return hashlib.sha256(_canonical({"criteria": ordered}).encode("utf-8")).hexdigest()


def design_hash(criteria: list[dict]) -> str:
    """Binds a design approval to the SPEC of each criterion (id, type,
    requirement, verification, tests), never its evidence state --
    unlike acceptance_hash, which deliberately includes state/evidence so
    a deployment/review approval notices new or changed evidence. A
    design approval is given before implementation exists; if it were
    bound to acceptance_hash, the very first `verify` call (which flips
    a criterion's state) would invalidate it, forcing re-approval for
    every criterion the moment it is first evidenced. Adding, removing,
    or respecifying a criterion still invalidates it; recording evidence
    about one that already exists does not."""
    ordered = sorted(criteria, key=lambda c: c.get("id") or "")
    return hashlib.sha256(_canonical(
        {"criteria": [{"id": c.get("id"), "spec": criterion_spec_hash(c)} for c in ordered]}
    ).encode("utf-8")).hexdigest()


def _is_green(criteria: list[dict]) -> bool:
    return bool(criteria) and all(c.get("state") == "passing" for c in criteria)


def coverage_for(criteria: list[dict], resolved: bool = False) -> dict:
    counts = {"passing": 0, "failing": 0, "not_tested": 0, "blocked": 0}
    for criterion in criteria:
        state = criterion.get("state")
        if state in counts:
            counts[state] += 1
    return {**counts, "original_symptom_resolved": bool(resolved)}


def sync_coverage(status: dict, acceptance: dict) -> None:
    resolved = status.get("requirement_coverage", {}).get("original_symptom_resolved") is True
    status["requirement_coverage"] = coverage_for(acceptance.get("criteria", []), resolved)


def valid_evidence_kinds(criterion: dict, verifications: list[dict]) -> set[str]:
    """Which required evidence kinds this criterion actually has a valid,
    spec-matching, successful record for right now. `verifications` may be
    a list of already-loaded records, an in-memory list a caller just
    appended to, or any combination; only ok=True, criterion-id-matching,
    current-spec-hash-matching records count."""
    cid = criterion.get("id")
    spec = criterion_spec_hash(criterion)
    kinds: set[str] = set()
    for record in verifications:
        if not isinstance(record, dict):
            continue
        if (record.get("ok") is True and cid in record.get("criteria", [])
                and record.get("criterion_hashes", {}).get(cid) == spec):
            kinds.add(record.get("kind"))
    return kinds


def criterion_fully_evidenced(criterion: dict, verifications: list[dict]) -> bool:
    """True only once EVERY evidence kind the criterion's policy requires
    has a valid record. A combined automated_and_browser criterion with
    only its automated half run is NOT fully evidenced: it must not read
    as 'passing' until the browser half lands too."""
    required = VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set())
    return bool(required) and required <= valid_evidence_kinds(criterion, verifications)


def _evidence_errors(criteria: list[dict], verifications: list[dict]) -> list[str]:
    errors: list[str] = []
    for criterion in criteria:
        if criterion.get("state") != "passing":
            continue
        cid = criterion.get("id")
        required = VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set())
        missing = required - valid_evidence_kinds(criterion, verifications)
        if missing:
            errors.append(f"evidence gate: passing criterion {cid} lacks valid {', '.join(sorted(missing))} evidence")
    return errors


def _review_errors(status: dict, acceptance: dict, cfg: dict) -> list[str]:
    errors: list[str] = []
    review = status.get("review")
    if not isinstance(review, dict):
        return ["review gate: Phase 6+ requires a recorded independent review"]
    if review.get("acceptance_hash") != acceptance_hash(acceptance.get("criteria", [])):
        errors.append("review gate: acceptance changed since review; record a new review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("review gate: workflow policy changed since review; record a new review")
    reviewer = review.get("by")
    if not reviewer:
        errors.append("review gate: review must identify its reviewer")
    implementer = status.get("implemented_by")
    if reviewer and implementer \
            and reviewer.strip().casefold() == implementer.strip().casefold():
        errors.append("review gate: reviewer must differ from implementer, no self-approval")
    checklist = review.get("checklist", {})
    for field, allowed in CHECKLIST_VALUES.items():
        if checklist.get(field) not in allowed:
            errors.append(f"review gate: checklist field '{field}' is incomplete")
    return errors


def _design_errors(status: dict, acceptance: dict, cfg: dict) -> list[str]:
    """The Architect gate: Phase 3+ requires a recorded human design
    approval, for any run `requires_design_approval` (a run a NEW init
    created). Absent for any status.json that predates this field --
    those runs are simply never subject to this check, so an
    already-in-progress run elsewhere is unaffected by upgrading bin/."""
    if not status.get("requires_design_approval"):
        return []
    errors: list[str] = []
    approval = status.get("design_approved")
    if not isinstance(approval, dict):
        return ["design gate: Phase 3+ requires a recorded human design approval"]
    if approval.get("design_hash") != design_hash(acceptance.get("criteria", [])):
        errors.append("design gate: criteria were added, removed, or respecified since design approval; record a new approval")
    if approval.get("config_hash") != config_hash(cfg):
        errors.append("design gate: workflow policy changed since design approval; record a new approval")
    approver = approval.get("by")
    architect = approval.get("architect")
    if not approver:
        errors.append("design gate: design approval must identify the human approver")
    if not architect:
        errors.append("design gate: design approval must identify the architect")
    if approver and architect and approver.strip().casefold() == architect.strip().casefold():
        errors.append("design gate: approver must differ from the architect, no self-approval")
    return errors


def _design_review_errors(status: dict, acceptance: dict, cfg: dict) -> list[str]:
    """AR7 gate: new runs cannot leave Phase 2 until an independent
    reviewer approved the exact current design. The opt-in status flag is
    absent from pre-AR7 runs, preserving their in-flight behavior."""
    if not status.get("requires_design_review"):
        return []
    review = status.get("design_review")
    if not isinstance(review, dict):
        return ["design review gate: Phase 3+ requires an approved independent design review"]
    errors: list[str] = []
    if review.get("decision") != "approved":
        errors.append("design review gate: the current design review requested changes")
    if review.get("design_hash") != design_hash(acceptance.get("criteria", [])):
        errors.append("design review gate: criteria changed since design review; record a new design review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("design review gate: workflow policy changed since design review; record a new design review")
    reviewer = review.get("by")
    architect = review.get("architect")
    if not reviewer:
        errors.append("design review gate: design review must identify its reviewer")
    if not architect:
        errors.append("design review gate: design review must identify the architect")
    if reviewer and architect and reviewer.strip().casefold() == architect.strip().casefold():
        errors.append("design review gate: reviewer must differ from the architect, no self-review")
    approval = status.get("design_approved")
    approved_architect = approval.get("architect") if isinstance(approval, dict) else None
    if approved_architect and architect \
            and approved_architect.strip().casefold() != architect.strip().casefold():
        errors.append("design review gate: reviewed architect differs from the architect named in human approval")
    return errors


def _valid_symptom_record(status: dict, criteria: list[dict], verifications: list[dict]) -> dict | None:
    evidence_id = status.get("original_symptom_evidence_id")
    primary = {c["id"]: c for c in criteria if c.get("type") == "primary_fix"}
    for record in verifications:
        if record.get("run_id") != evidence_id or record.get("ok") is not True:
            continue
        for cid in set(record.get("criteria", [])) & set(primary):
            if record.get("criterion_hashes", {}).get(cid) == criterion_spec_hash(primary[cid]):
                return record
    return None


def compute_errors(status: dict, acceptance: dict, cfg: dict, *, now: datetime | None = None,
                   verifications: list[dict] | None = None,
                   verification_problems: list[str] | None = None) -> list[str]:
    """Every rule a transition must satisfy, evaluated against WHATEVER
    status dict is passed in. Callers that want to gate a transition must
    pass the PROPOSED status, the one they are about to write, not the one
    already on disk: this function has no way to know which you meant, and
    checking the wrong one is exactly how the original tool let an
    unguarded write through."""
    now = now or datetime.now(timezone.utc)
    errors = validate_status_schema(status)
    errors += validate_acceptance_schema(acceptance)
    errors += [f"verification ledger: {p}" for p in (verification_problems or [])]
    records = verifications or []
    actual_verification_head = records[-1].get("hash") if records else "GENESIS"
    if isinstance(status, dict) and status.get("verification_head") != actual_verification_head:
        errors.append("verification ledger: tail does not match the anchored head; evidence was deleted or an append was interrupted")
    if errors:
        return errors  # a malformed shape makes every gate below meaningless

    criteria = acceptance.get("criteria", [])
    coverage = status.get("requirement_coverage", {})
    green = _is_green(criteria)
    resolved = coverage.get("original_symptom_resolved") is True
    phase = int(status.get("phase_number", 0) or 0)
    progress = float(status.get("progress", 0) or 0)
    evidence_errors = _evidence_errors(criteria, verifications or [])
    expected_coverage = coverage_for(criteria, resolved)
    symptom_record = _valid_symptom_record(status, criteria, verifications or [])

    if status.get("feature") != acceptance.get("feature"):
        errors.append("state gate: status and acceptance describe different features")
    if coverage != expected_coverage:
        errors.append("state gate: requirement_coverage does not match the acceptance registry")
    if phase == 7 and status.get("status") not in {"awaiting_approval", "ready_to_deploy"}:
        errors.append("state gate: Phase 7 requires status 'awaiting_approval' or 'ready_to_deploy'")

    if phase >= 3:
        errors.extend(_design_review_errors(status, acceptance, cfg))
        errors.extend(_design_errors(status, acceptance, cfg))

    if phase >= 6 and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("phase gate: every criterion and the original symptom must have verified evidence before Phase 6+")
        if resolved and not symptom_record:
            errors.append("symptom gate: resolved original symptom must reference a successful verification run")
        errors.extend(evidence_errors)
    if progress >= 95 and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("progress gate: 95%+ requires verified acceptance and a resolved original symptom")
    if status.get("status") in ("ready_to_deploy", "awaiting_approval", "complete") and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("status gate: acceptance registry is not fully green")

    if phase >= 6:
        implemented_by = status.get("implemented_by")
        if not implemented_by:
            errors.append("review gate: Phase 6+ requires 'implemented_by' to be recorded")
        errors.extend(_review_errors(status, acceptance, cfg))

    if cfg.get("deployment_requires_explicit_approval", True) and phase >= 8:
        approval = status.get("deployment_approved")
        if not approval or not approval.get("at"):
            errors.append("deployment gate: Phase 8 requires a recorded deployment approval")
        elif approval.get("acceptance_hash") != acceptance_hash(criteria):
            errors.append("deployment gate: the acceptance registry changed since approval was given, re-approve")
        elif approval.get("config_hash") != config_hash(cfg):
            errors.append("deployment gate: workflow policy changed since approval was given, re-approve")

    if phase >= 8:
        if progress != 100 or status.get("status") != "complete":
            errors.append("live gate: Phase 8 requires progress 100 and status 'complete'")
        if cfg.get("require_live_verification", True):
            live_id = status.get("live_verification_id")
            record = next((r for r in (verifications or []) if r.get("run_id") == live_id), None)
            approval = status.get("deployment_approved") or {}
            if not record or record.get("kind") != "live" or record.get("ok") is not True:
                errors.append("live gate: Phase 8 requires a successful live verification run")
            elif record.get("acceptance_hash") != acceptance_hash(criteria):
                errors.append("live gate: acceptance changed since live verification")
            elif record.get("config_hash") != config_hash(cfg):
                errors.append("live gate: workflow policy changed since live verification; run it again")
            elif approval.get("at") and record.get("at", "") <= approval.get("at", ""):
                errors.append("live gate: live verification must occur after deployment approval")

    design_round = int(status.get("design_round", 0) or 0)
    review_round = int(status.get("review_round", 0) or 0)
    max_design = int(cfg.get("max_design_rounds", 3))
    max_review = int(cfg.get("max_review_rounds", 3))
    if design_round > max_design:
        errors.append(f"round cap: design_round {design_round} exceeds max_design_rounds {max_design}, escalate to the user")
    if review_round > max_review:
        errors.append(f"round cap: review_round {review_round} exceeds max_review_rounds {max_review}, escalate to the user")

    return errors


def _minutes_since(timestamp: str | None, now: datetime) -> float | None:
    """Age of an ISO-8601 timestamp in minutes, or None if it is missing or
    unparsable -- callers treat None as 'no signal', never as 'fresh'."""
    if not timestamp:
        return None
    try:
        last = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() / 60


def stall_warning(status: dict, cfg: dict, *, now: datetime | None = None) -> str | None:
    """Advisory only, never blocks a call: a stalled run should surface for
    escalation, not lock the operator out of even reading status.

    Reads the FRESHEST of two signals, not `updated_at` alone: `updated_at`
    (bumped by any progress-making call: advance, verify, record-evidence,
    ...) and `last_heartbeat_at` (bumped only by the `heartbeat` command, a
    pure liveness ping for a run doing legitimate long background work that
    has no progress to report yet). A run flagged stalled here has NEITHER
    signal current -- genuinely no activity, not merely no status-file
    write. `last_heartbeat_at` may be entirely absent (any status.json
    written before this field existed); that reads as 'no heartbeat', the
    same as if it were missing today, never as an error."""
    now = now or datetime.now(timezone.utc)
    if status.get("status") not in ("in_progress",):
        return None
    updated_minutes = _minutes_since(status.get("updated_at"), now)
    if updated_minutes is None:
        return None
    heartbeat_minutes = _minutes_since(status.get("last_heartbeat_at"), now)
    limit = float(cfg.get("stall_minutes", 10))
    freshest = min(m for m in (updated_minutes, heartbeat_minutes) if m is not None)
    if freshest > limit:
        return f"no update in {updated_minutes:.0f} minutes (limit {limit:.0f}), consider escalating"
    return None


def activity_note(status: dict, cfg: dict, *, now: datetime | None = None) -> str | None:
    """The other half of the same signal: a run that is alive but not
    currently progressing. Fires only in the specific case that would
    otherwise look ambiguous -- `updated_at` is stale past `stall_minutes`,
    but `last_heartbeat_at` is fresh -- so a caller (the dashboard, `status`)
    can say 'busy on a long background task' instead of leaving the
    operator to guess between 'stalled' and 'on course'. Mutually exclusive
    with `stall_warning`: whenever this returns non-None, `stall_warning`
    is guaranteed None, since a fresh heartbeat is exactly what suppresses
    it."""
    now = now or datetime.now(timezone.utc)
    if status.get("status") not in ("in_progress",):
        return None
    updated_minutes = _minutes_since(status.get("updated_at"), now)
    heartbeat_minutes = _minutes_since(status.get("last_heartbeat_at"), now)
    if updated_minutes is None or heartbeat_minutes is None:
        return None
    limit = float(cfg.get("stall_minutes", 10))
    if updated_minutes > limit and heartbeat_minutes <= limit:
        return (f"background task active (heartbeat {heartbeat_minutes:.0f} min ago); "
                f"no status update in {updated_minutes:.0f} minutes, but the run is alive")
    return None


# --------------------------------------------------------------------------
# check execution: close the loop between a claimed state and reality
# --------------------------------------------------------------------------

def run_checks(cfg: dict, root: Path, commands: list[str] | None = None,
               timeout: int | None = None) -> list[dict]:
    """Actually execute the given commands (default: [checks].commands), in
    the project root. Each result is real evidence a criterion's evidence
    list can reference, not a sentence someone typed. Timeout comes from
    handsoff.toml's checks.timeout_seconds (default 600s) unless overridden."""
    import subprocess
    timeout = timeout if timeout is not None else cfg.get("check_timeout_seconds", 600)
    results = []
    for cmd in (commands if commands is not None else cfg.get("check_commands", [])):
        started = time.time()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
            returncode = proc.returncode
            output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output = stdout + stderr + f"\nHANDSOFF: command timed out after {timeout} seconds"
        output_hash = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
        results.append({
            "command": cmd, "exit_code": returncode, "output_sha256": output_hash,
            "duration_s": round(time.time() - started, 2),
            "output_tail": output[-2000:],
        })
    return results


# --------------------------------------------------------------------------
# run archive: one self-contained JSON snapshot per completed run, written
# outside any project repo so what Handsoff learns about ITSELF survives a
# repo's own archive/cleanup and can be read across every project it has
# ever run in, not just the one it just finished.
# --------------------------------------------------------------------------

def archive_dir() -> Path:
    override = os.environ.get("HANDSOFF_ARCHIVE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "Handsoff-Archive"


def read_events(root: Path, cfg: dict) -> list[dict]:
    """Every event for this run, oldest first. Tolerant of a corrupt line the
    way the dashboard already is: a bad line is skipped, not fatal, because
    archiving a finished run must never be the thing that fails a run."""
    path = event_log_path(root, cfg)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


# --------------------------------------------------------------------------
# AR8: design-phase timing (read-back over the event log, not a new store)
# --------------------------------------------------------------------------

DESIGN_DEBATE_PHASE = 2

_WAIT_STARTS = {"background_wait_started": "background_wait", "human_pause_started": "human_wait"}
_WAIT_ENDS = {"background_wait_ended": "active", "human_pause_ended": "active"}


def _parse_event_time(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def summarize_design_timing(events: list[dict], *, now: datetime | None = None) -> dict:
    """Walk a run's event log (oldest first, as `read_events` returns it) and
    account design-phase wall-clock time by category (active / background_wait
    / human_wait) per round, so an archived run can answer 'where did design
    time actually go', not just 'how long did design take total'.

    A design-phase EPISODE opens at the first event carrying phase_number==2
    (either a plain phase_advanced -- pre-round-tracking design work counts
    as round 0 -- or a design_round_advanced that transitions straight from
    Phase 1 into round 1 in one call) and closes at design_approved. Most
    runs have exactly one episode; a criterion mutation that invalidates an
    already-approved design and rolls back to Phase 2 (see
    _invalidate_decisions) can reopen a second one, handled the same way.

    design_round_advanced is the round boundary, per AR8's contract: it both
    closes whatever round preceded it (a safety net -- design_round_ended
    already closes it explicitly, see cmd_advance/cmd_design_approve) and
    opens the new one. Time defaults to 'active'; background_wait_started/
    human_pause_started/design_approval_requested switch the running
    category until their matching end (or the episode/round boundary that
    supersedes them). Events this function does not recognize never stop
    the clock -- they simply accrue to whatever category is already
    running, the same as any other quiet stretch of real work.

    An episode with no closing design_approved yet (the run is still mid-
    design) is included with status 'in_progress', its still-open category
    flushed up to `now` (default: real now) so a live read gives a sane
    answer, not a truncated one."""
    now = now or datetime.now(timezone.utc)
    episode: dict | None = None
    episodes: list[dict] = []

    def empty_totals() -> dict:
        return {"active": 0.0, "background_wait": 0.0, "human_wait": 0.0}

    def new_round(round_number, ts: str) -> dict:
        return {"round": round_number, "start": ts, "end": None, "seconds": empty_totals()}

    def flush(ts: str) -> None:
        elapsed = max(0.0, (_parse_event_time(ts) - _parse_event_time(episode["_cat_start"])).total_seconds())
        episode["rounds"][episode["_current_round"]]["seconds"][episode["_category"]] += elapsed
        episode["_cat_start"] = ts

    def switch_category(category: str, ts: str) -> None:
        flush(ts)
        episode["_category"] = category

    def open_episode(ts: str, round_number) -> None:
        nonlocal episode
        episode = {
            "start": ts, "end": None, "status": "in_progress",
            "rounds": {round_number: new_round(round_number, ts)},
            "_current_round": round_number, "_category": "active", "_cat_start": ts,
        }

    def start_round(round_number, ts: str) -> None:
        flush(ts)
        prev = episode["rounds"][episode["_current_round"]]
        prev["end"] = prev["end"] or ts
        episode["rounds"][round_number] = new_round(round_number, ts)
        episode["_current_round"] = round_number
        episode["_category"] = "active"
        episode["_cat_start"] = ts

    def close_episode(ts: str) -> None:
        nonlocal episode
        flush(ts)
        cur = episode["rounds"][episode["_current_round"]]
        cur["end"] = cur["end"] or ts
        episode["end"] = ts
        episode["status"] = "approved"
        for key in ("_current_round", "_category", "_cat_start"):
            del episode[key]
        episodes.append(episode)
        episode = None

    for ev in events:
        kind, ts = ev.get("kind"), ev.get("at")
        if not ts:
            continue
        if kind == "design_round_advanced":
            if episode is None:
                open_episode(ts, ev.get("design_round"))
            else:
                start_round(ev.get("design_round"), ts)
            continue
        if episode is None:
            if kind == "phase_advanced" and ev.get("phase_number") == DESIGN_DEBATE_PHASE:
                open_episode(ts, 0)
            continue
        if kind == "design_round_ended":
            flush(ts)
            target = episode["rounds"].get(ev.get("design_round"), episode["rounds"][episode["_current_round"]])
            target["end"] = target["end"] or ts
        elif kind == "design_approval_requested":
            switch_category("human_wait", ts)
        elif kind in _WAIT_STARTS:
            switch_category(_WAIT_STARTS[kind], ts)
        elif kind in _WAIT_ENDS:
            switch_category(_WAIT_ENDS[kind], ts)
        elif kind == "design_approved":
            close_episode(ts)

    if episode is not None:
        flush(now.isoformat())
        for key in ("_current_round", "_category", "_cat_start"):
            del episode[key]
        episodes.append(episode)

    totals = empty_totals()
    for ep in episodes:
        for r in ep["rounds"].values():
            for key in totals:
                totals[key] += r["seconds"][key]

    return {"episodes": episodes, "total_seconds": totals}


def _archive_slug(text: str, max_len: int = 60) -> str:
    out, prev_dash = [], False
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return (slug or "run")[:max_len]


def archive_run(root: Path, cfg: dict, status: dict, acceptance: dict,
                verifications: list[dict], events: list[dict]) -> Path:
    """Write one self-contained JSON record of a just-completed run to the
    centralized archive (~/Documents/Handsoff-Archive by default, override
    with HANDSOFF_ARCHIVE_DIR). Called automatically by `advance` when a
    transition lands Phase 8 with status complete, so no Supervisor session
    has to remember a separate step -- the same failure mode that let the
    dashboard's blocked-alert protocol go unused until Moncy actually hit it.

    The record is a full copy of status + acceptance + verifications +
    events, not a hand-picked summary: which fields turn out to matter for
    improving Handsoff is exactly the open question this archive exists to
    answer, and a summary decided today could not answer a question nobody
    has thought to ask yet.
    """
    record = {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "repo": root.name,
        "root": str(root),
        "feature": status.get("feature"),
        "started_at": events[0]["at"] if events else status.get("updated_at"),
        "completed_at": status.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "acceptance": acceptance,
        "verifications": verifications,
        "events": events,
    }
    out_dir = archive_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{_archive_slug(root.name)}-{stamp}-{_archive_slug(status.get('feature'))}.json"
    out_path = out_dir / filename
    out_path.write_text(json.dumps(record, indent=1, sort_keys=True))
    return out_path
