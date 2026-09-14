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
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
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
MAX_FALLBACK_PROFILES = 8
DEFAULT_MAX_FAILOVERS_PER_ROLE = 2
# #35: how many design-review attempts (approve or request-changes, every
# record-design-review counts) a run may consume on its own before the
# Pilot has to authorize each further attempt one at a time.
DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS = 2
DESIGN_REVIEW_AUTHORIZATION_COMMAND = "handsoff_supervisor.py design-review-authorize --by <pilot>"
TICKET_STATES = frozenset({"done", "in_progress", "not_started", "blocked"})
#: #38: cached, hash-bound design evidence. The side file is generated
#: state (gitignored), never a ledger: it holds the bounded output of
#: trusted configured commands, and the event log only ever carries hashes.
DESIGN_EVIDENCE_FILE = "handsoff-design-evidence.json"
DESIGN_EVIDENCE_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
MAX_DESIGN_EVIDENCE_ENTRIES = 16
MAX_DESIGN_EVIDENCE_OUTPUT_BYTES = 8192
DESIGN_EVIDENCE_STATES = ("current", "stale", "failed", "missing")
#: #33: liveness beacon written by `handsoff_agent.execute_launch` while a
#: managed child runs. Generated state (gitignored), never hashed, never
#: read by any gate: identifiers, integers, and timestamps only. The
#: ledger-bound session record stays the authority on lifecycle; the beacon
#: only says whether the process that owns that session is still signalling.
LIVE_BEACON_FILE = ".handsoff-live.json"
LIVE_BEACON_KEYS = ("session_id", "role", "state", "pid", "beacon_at", "ended_at", "exit_code")
LIVE_BEACON_INTERVAL_SECONDS = 5.0
LIVE_BEACON_FRESH_SECONDS = 15.0
LIVE_STATES = ("idle", "started", "running", "waiting", "stalled", "stopped", "failed", "complete")
#: #40: a dashboard launched with `--owned-by-run` writes this pointer file
#: in the project root so run completion can find and release it. It is
#: generated state (gitignored), a pointer and never the authority: the
#: server keeps its own run_token and root_sha256 in memory and answers
#: `/api/ownership` from those, so a stale or hand-edited file can only
#: ever get itself removed, never get a foreign process shut down.
DASHBOARD_OWNER_FILE = ".handsoff-dashboard-owner.json"
DASHBOARD_OWNER = "ship-feature"
DASHBOARD_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# #36: delta review packets. A recorded review may carry bounded findings;
# every record appends a bounded history entry; a packet is the sorted,
# canonical, size-capped delta a follow-up reviewer receives instead of
# the full task.
MAX_DESIGN_REVIEW_FINDINGS = 32
MAX_DESIGN_REVIEW_FINDING_LENGTH = 512
MAX_DESIGN_REVIEW_HISTORY = 8
MAX_DESIGN_REVIEW_PACKET_BYTES = 65536
MAX_DESIGN_REVIEW_PACKET_FILES = 200
DESIGN_REVIEW_PACKET_TRIMMED_TEXT_LENGTH = 256
DESIGN_REVIEW_DISPOSITIONS = ("resolved", "rejected", "unresolved")
DESIGN_REVIEW_FINDING_ID_PATTERN = re.compile(r"^F[1-9][0-9]*\.[1-9][0-9]*$")
DESIGN_REVIEW_PACKET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DESIGN_REVIEW_HISTORY_FIELDS = (
    "attempt", "decision", "by", "design_hash", "head", "criteria_ids", "criterion_hashes",
    "structural_blocker", "findings",
)
DESIGN_REVIEW_PACKET_INSTRUCTIONS = (
    "Verify the revision: changed criteria, unresolved and rejected findings, new material. "
    "Repository access remains available to challenge any omission or stale fact."
)
DESIGN_EVIDENCE_RECORD_FIELDS = (
    "id", "command", "command_sha256", "identity_sha256", "inputs", "input_hash", "matched_files",
    "exit_code", "output", "output_sha256", "output_bytes", "truncated", "at", "by", "head", "branch", "dirty",
)

#: #39: the crew a role gets when handsoff.toml does not name one (or still
#: carries the legacy "configure-me" placeholder). Architect, Supervisor,
#: and Implementer share one premium reasoning profile: design needs the
#: strongest reasoning available, and the operator keeps the same Opus
#: profile for orchestration and implementation so one run has one
#: consistent voice. The Reviewer runs on an independent provider family
#: so the critique never comes from the model being critiqued. An explicit
#: "auto" is NOT part of this table: it keeps the older first-installed
#: auto-detect path (DEFAULT_AGENT_PREFERENCE) on purpose.
RECOMMENDED_CREW = {
    "architect": {"adapter": "claude", "model": "claude-opus-5"},
    "supervisor": {"adapter": "claude", "model": "claude-opus-5"},
    "implementer": {"adapter": "claude", "model": "claude-opus-5"},
    "reviewer": {"adapter": "codex", "model": "default"},
}
#: Where a role's adapter or model came from: named in handsoff.toml
#: ("explicit"), taken from RECOMMENDED_CREW because the key was absent or
#: the placeholder ("recommended"), or the runner default because the
#: adapter was overridden but no model was named, so the recommended model
#: for the other adapter would be the wrong thing to pass ("runner_default").
PROFILE_SOURCES = ("explicit", "recommended", "runner_default")
RECOMMENDED_PROFILE_SOURCE = "recommended"
EXPLICIT_PROFILE_SOURCE = "explicit"
RUNNER_DEFAULT_PROFILE_SOURCE = "runner_default"
#: #37: the optional economical follow-up reviewer profile. Both keys
#: ([agents].reviewer_followup and [models].reviewer_followup) present
#: enables tiering; both absent reproduces the single-profile behavior
#: exactly; exactly one present is a config error. It is a cost knob, not
#: a gate, so it is deliberately NOT in GOVERNANCE_CONFIG_KEYS.
FOLLOWUP_REVIEWER_KEY = "reviewer_followup"
DESIGN_REVIEWER_TIERS = ("primary", "followup")
#: Selection precedence, evaluated in this order; the first match is the
#: reason. Only `delta_check` selects the follow-up tier.
DESIGN_REVIEWER_SELECTION_REASONS = (
    "first_review", "no_followup_configured", "pilot_escalation",
    "structural_blocker", "criteria_structure_changed", "delta_check",
)
DESIGN_REVIEWER_ESCALATION_FIELDS = ("by", "at", "note", "consumed_at")
DESIGN_REVIEWER_PROFILE_FIELDS = ("adapter", "model", "tier", "reason")
#: What crew_view's `available` actually proves, and nothing more: the
#: adapter executable was found on PATH. Authentication, entitlement,
#: network access, and whether the model id is valid for that adapter are
#: never checked offline, so the view names its scope instead of implying it.
CREW_AVAILABILITY_SCOPE = "executable discovery only"
WORK_ITEM_KINDS = {"issue", "ask"}
WORK_ITEM_STATES = {
    "done", "blocked", "in_review", "awaiting_approval", "recovering",
    "in_progress", "not_started",
}
WORK_ITEM_TAG_PATTERN = re.compile(r"^\[(#\d{1,9}|[a-z0-9][a-z0-9-]{0,39})\]\s")
WORK_ITEM_ID_PATTERN = re.compile(r"^(?:issue-[1-9][0-9]{0,8}|ask-[a-z0-9][a-z0-9-]{0,39}|unattributed)$")
MAX_WORK_ITEMS = 64

DEFAULT_CONFIG = {
    "status_file": "handsoff-status.json",
    "acceptance_file": "handsoff-acceptance.json",
    "event_log": "handsoff-events.jsonl",
    "verification_log": "handsoff-verifications.jsonl",
    "max_design_rounds": 3,
    "max_review_rounds": 3,
    "stall_minutes": 10,
    "max_autonomous_design_reviews": DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    "require_live_verification": True,
    "deployment_requires_explicit_approval": True,
    "check_commands": [],
    "live_check_commands": [],
    "check_timeout_seconds": 600,
    "tickets": [],
    "design_evidence": [],
    "regressions": [],
    "regression_gate": {"approval_timeout_minutes": 30, "launch_window_minutes": 10},
    "agents": {role: profile["adapter"] for role, profile in RECOMMENDED_CREW.items()},
    "models": {role: profile["model"] for role, profile in RECOMMENDED_CREW.items()},
    "fallbacks": {
        "architect": [],
        "supervisor": [],
        "implementer": [],
        "reviewer": [],
    },
    "max_failovers_per_role": DEFAULT_MAX_FAILOVERS_PER_ROLE,
    "reviewer_followup": None,
    "recovery": {
        "enabled": True, "max_attempts": 3, "lease_minutes": 15,
        "worker_loss_grace_minutes": 2, "live_session_silence_minutes": 10,
        "liveness_seconds": 60, "dashboard_watchdog": True, "poll_seconds": 30,
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
AGENT_SESSION_RESOLUTION_SOURCES = {
    "configured", "recommended", "auto_detected", "legacy_auto_detected", "fallback",
}
# #36: packet_id and design_hash are written on every new session (null
# unless a Phase-2 reviewer was launched with a delta packet) but stay
# OPTIONAL on read, so a status.json written before they existed is still
# valid; when present they must be null or a non-empty string. #37 adds
# `tier` the same way: null unless a Phase-2 reviewer was launched through
# the tiered selection, otherwise exactly "primary" or "followup".
AGENT_SESSION_OPTIONAL_FIELDS = {"packet_id", "design_hash", "tier"}
AGENT_SESSION_FIELDS = {
    "session_id", "role", "actor", "adapter", "requested_model", "reported_model",
    "resolution_source", "started_at", "running_at", "ended_at", "state", "exit_code",
    *AGENT_SESSION_OPTIONAL_FIELDS,
}
MAX_AGENT_REPLACEMENTS = 32
MAX_QUALITY_FINDINGS = 32
AGENT_REPLACEMENT_TRIGGERS = {"runtime_failure", "quality_finding"}
AGENT_REPLACEMENT_STATES = {"reserved", "claimed", "running", "failed", "recovered", "pilot_pause"}
QUALITY_FINDING_CODES = {
    "acceptance_not_met", "incorrect_implementation", "review_changes_requested",
}
REPLACEMENT_ID_PATTERN = re.compile(r"^hr-[0-9a-f]{32}$")
QUALITY_FINDING_ID_PATTERN = re.compile(r"^hq-[0-9a-f]{32}$")
MAX_RECOVERY_ATTEMPTS = 16
RECOVERY_ID_PATTERN = re.compile(r"^hv-[0-9a-f]{32}$")
RECOVERY_LEASE_ID_PATTERN = re.compile(r"^hl-[0-9a-f]{32}$")
RECOVERY_STATES = {"reserved", "launched", "recovered", "failed", "escalated"}
RECOVERY_TRIGGERS = {"worker_terminal", "worker_silent", "silent_run"}
MAX_REVIEW_ATTEMPTS = 64
MAX_REVIEW_SESSION_IDS = 160
MAX_REVIEW_CAP_OVERRIDES = 8
REVIEW_ATTEMPT_ID_PATTERN = re.compile(r"^ha-[0-9a-f]{32}$")
REVIEW_OVERRIDE_ID_PATTERN = re.compile(r"^ho-[0-9a-f]{32}$")
REVIEW_ATTEMPT_TRIGGERS = {
    "initial", "changes_requested", "acceptance_changed",
    "implementation_changed", "supervisor_remediation", "manual_override",
}
REVIEW_ATTEMPT_DISPOSITIONS = {
    "open", "approved", "changes_requested", "abandoned", "unrecorded",
}
REVIEW_FINDING_CODES = {
    "acceptance_not_met", "incorrect_implementation", "review_changes_requested", "other",
}
REGRESSION_STATES = {
    "awaiting_approval", "accepted", "declined", "expired", "invalidated",
    "launched", "completed", "failed", "cancelled",
}
REGRESSION_REQUEST_ID_PATTERN = re.compile(r"^rg-[0-9a-f]{32}$")
MAX_REGRESSION_REQUESTS = 16
ESCALATION_KINDS = {"review_cap_exhausted", "recovery_exhausted", "recovery_paused"}

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
    cfg["fallbacks"] = {role: [] for role in DEFAULT_CONFIG["fallbacks"]}
    cfg["design_evidence"] = []
    cfg["profile_sources"] = {
        role: {"adapter": RECOMMENDED_PROFILE_SOURCE, "model": RECOMMENDED_PROFILE_SOURCE}
        for role in AGENT_ROLES
    }
    cfg["recovery"] = dict(DEFAULT_CONFIG["recovery"])
    cfg["regression_gate"] = dict(DEFAULT_CONFIG["regression_gate"])
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
    fallback_policy = raw.get("fallback_policy", {})
    checks = raw.get("checks", {})
    recovery = raw.get("recovery", {})
    regression_gate = raw.get("regression_gate", {})
    regressions = raw.get("regressions", [])
    tickets = raw.get("tickets", [])
    if not all(isinstance(section, dict) for section in (project, workflow, agents, models, fallback_policy, checks, recovery, regression_gate)):
        raise HandsoffError(
            "handsoff.toml: project, workflow, agents, models, fallback_policy, checks, recovery, and regression_gate must be tables"
        )
    cfg["status_file"] = project.get("status_file", cfg["status_file"])
    cfg["acceptance_file"] = project.get("acceptance_file", cfg["acceptance_file"])
    cfg["event_log"] = project.get("event_log", cfg["event_log"])
    cfg["verification_log"] = project.get("verification_log", cfg["verification_log"])
    for key in ("max_design_rounds", "max_review_rounds", "stall_minutes", "max_autonomous_design_reviews"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be an integer")
        cfg[key] = value
    for key in ("require_live_verification", "deployment_requires_explicit_approval"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be boolean")
        cfg[key] = value
    # #39: a role key absent from the TOML (or still holding the legacy
    # "configure-me" placeholder) takes the recommended crew profile. Any
    # explicit value, "auto" included, is kept as written and only ever
    # affects its own role.
    for role in AGENT_ROLES:
        if role not in agents:
            continue
        value = agents[role]
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"handsoff.toml: agents.{role} must be a non-empty string")
        value = value.strip()
        if value == LEGACY_UNCONFIGURED_AGENT_ADAPTER:
            continue
        cfg["agents"][role] = value
        cfg["profile_sources"][role]["adapter"] = EXPLICIT_PROFILE_SOURCE
    for role in SELECTABLE_AGENT_ROLES:
        if role in models:
            cfg["models"][role] = validate_agent_model(models[role])
            cfg["profile_sources"][role]["model"] = EXPLICIT_PROFILE_SOURCE
        elif cfg["agents"][role] != RECOMMENDED_CREW[role]["adapter"]:
            # The recommended model belongs to the recommended adapter. With
            # the adapter overridden (or set to "auto") and no model named,
            # passing that model id to a different runner would be wrong,
            # so the runner's own default is used and labelled as such.
            cfg["models"][role] = DEFAULT_AGENT_MODEL
            cfg["profile_sources"][role]["model"] = RUNNER_DEFAULT_PROFILE_SOURCE
    # #37: the follow-up reviewer profile is enabled only by BOTH keys.
    # Half a profile is refused rather than guessed, and "auto" is not a
    # profile (the follow-up must be a specific adapter so the independence
    # check against the architect and implementer means something).
    followup_adapter = agents.get(FOLLOWUP_REVIEWER_KEY)
    followup_model = models.get(FOLLOWUP_REVIEWER_KEY)
    if (followup_adapter is None) != (followup_model is None):
        raise HandsoffError(
            f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} and models.{FOLLOWUP_REVIEWER_KEY} "
            "must be set together (both present enables the follow-up reviewer tier; both absent disables it)"
        )
    if followup_adapter is not None:
        if not isinstance(followup_adapter, str) or not followup_adapter.strip():
            raise HandsoffError(f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} must be a non-empty string")
        followup_adapter = followup_adapter.strip()
        if followup_adapter not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError(
                f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} must be exactly 'codex' or 'claude'"
            )
        try:
            followup_model = validate_agent_model(followup_model)
        except HandsoffError as exc:
            raise HandsoffError(f"handsoff.toml: models.{FOLLOWUP_REVIEWER_KEY}: {exc}") from exc
        cfg["reviewer_followup"] = {"adapter": followup_adapter, "model": followup_model}
    else:
        cfg["reviewer_followup"] = None
    allowed_fallback_keys = {*SELECTABLE_AGENT_ROLES, "max_failovers_per_role"}
    unknown_fallback_keys = set(fallback_policy) - allowed_fallback_keys
    if unknown_fallback_keys:
        raise HandsoffError(
            f"handsoff.toml: fallback_policy has unknown keys: {', '.join(sorted(unknown_fallback_keys))}"
        )
    for role in SELECTABLE_AGENT_ROLES:
        cfg["fallbacks"][role] = validate_fallback_entries(
            fallback_policy.get(role, []), field=f"fallback_policy.{role}",
        )
    cfg["max_failovers_per_role"] = validate_max_failovers(
        fallback_policy.get("max_failovers_per_role", DEFAULT_MAX_FAILOVERS_PER_ROLE)
    )
    for config_key, toml_key in (("check_commands", "commands"), ("live_check_commands", "live_commands")):
        value = checks.get(toml_key, cfg[config_key])
        if not isinstance(value, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in value):
            raise HandsoffError(f"handsoff.toml: checks.{toml_key} must be an array of non-empty command strings")
        cfg[config_key] = list(value)
    timeout_value = checks.get("timeout_seconds", cfg["check_timeout_seconds"])
    if not isinstance(timeout_value, int) or isinstance(timeout_value, bool) or timeout_value <= 0:
        raise HandsoffError("handsoff.toml: checks.timeout_seconds must be a positive integer")
    cfg["check_timeout_seconds"] = timeout_value
    unknown_gate = set(regression_gate) - set(DEFAULT_CONFIG["regression_gate"])
    if unknown_gate:
        raise HandsoffError(f"handsoff.toml: regression_gate has unknown keys: {', '.join(sorted(unknown_gate))}")
    for key in DEFAULT_CONFIG["regression_gate"]:
        value = regression_gate.get(key, cfg["regression_gate"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440:
            raise HandsoffError(f"handsoff.toml: regression_gate.{key} must be an integer from 1 to 1440")
        cfg["regression_gate"][key] = value
    if not isinstance(regressions, list):
        raise HandsoffError("handsoff.toml: regressions must be an array of tables")
    normalized_regressions = []
    for index, item in enumerate(regressions):
        if not isinstance(item, dict) or set(item) != {"name", "commands"}:
            raise HandsoffError(f"handsoff.toml: regressions[{index}] must contain exactly name and commands")
        name, commands = item.get("name"), item.get("commands")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise HandsoffError(f"handsoff.toml: regressions[{index}].name is invalid")
        if not isinstance(commands, list) or not commands or not all(isinstance(cmd, str) and cmd.strip() for cmd in commands):
            raise HandsoffError(f"handsoff.toml: regressions[{index}].commands must be non-empty strings")
        normalized_regressions.append({"name": name, "commands": list(commands)})
    if len({item["name"] for item in normalized_regressions}) != len(normalized_regressions):
        raise HandsoffError("handsoff.toml: regression names must be unique")
    cfg["regressions"] = normalized_regressions
    allowed_recovery = set(DEFAULT_CONFIG["recovery"])
    unknown_recovery = set(recovery) - allowed_recovery
    if unknown_recovery:
        raise HandsoffError(
            f"handsoff.toml: recovery has unknown keys: {', '.join(sorted(unknown_recovery))}"
        )
    for key in ("enabled", "dashboard_watchdog"):
        value = recovery.get(key, cfg["recovery"][key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: recovery.{key} must be boolean")
        cfg["recovery"][key] = value
    bounds = {
        "max_attempts": (0, 16), "lease_minutes": (1, 1440),
        "worker_loss_grace_minutes": (1, 1440), "live_session_silence_minutes": (1, 1440),
        "liveness_seconds": (1, 3600), "poll_seconds": (1, 3600),
    }
    for key, (minimum, maximum) in bounds.items():
        value = recovery.get(key, cfg["recovery"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise HandsoffError(
                f"handsoff.toml: recovery.{key} must be an integer from {minimum} to {maximum}"
            )
        cfg["recovery"][key] = value
    if not isinstance(tickets, list):
        raise HandsoffError("handsoff.toml: tickets must be an array of tables")
    normalized_tickets = []
    for index, ticket in enumerate(tickets):
        if not isinstance(ticket, dict):
            raise HandsoffError(f"handsoff.toml: tickets[{index}] must be a table")
        unknown = set(ticket) - {"number", "title", "status", "url"}
        if unknown:
            raise HandsoffError(
                f"handsoff.toml: tickets[{index}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        number, title, state, url = (
            ticket.get("number"), ticket.get("title"), ticket.get("status"), ticket.get("url", "")
        )
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise HandsoffError(f"handsoff.toml: tickets[{index}].number must be a positive integer")
        if not isinstance(title, str) or not title.strip():
            raise HandsoffError(f"handsoff.toml: tickets[{index}].title must be a non-empty string")
        if state not in TICKET_STATES:
            raise HandsoffError(
                f"handsoff.toml: tickets[{index}].status must be one of {', '.join(sorted(TICKET_STATES))}"
            )
        if not isinstance(url, str):
            raise HandsoffError(f"handsoff.toml: tickets[{index}].url must be a string")
        normalized_tickets.append({
            "number": number, "title": title.strip(), "status": state, "url": url.strip(),
        })
    if len({ticket["number"] for ticket in normalized_tickets}) != len(normalized_tickets):
        raise HandsoffError("handsoff.toml: ticket numbers must be unique")
    cfg["tickets"] = normalized_tickets
    cfg["design_evidence"] = _validate_design_evidence_config(raw.get("design_evidence", []))
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
    for key in ("max_design_rounds", "stall_minutes", "max_autonomous_design_reviews"):
        if cfg[key] < 0:
            raise HandsoffError(f"handsoff.toml: {key} must not be negative")
    if not 1 <= cfg["max_review_rounds"] <= 56:
        raise HandsoffError("handsoff.toml: workflow.max_review_rounds must be an integer from 1 to 56")
    ensure_regression_config_is_disjoint(cfg, root)
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


def _validate_design_evidence_config(value: object) -> list[dict]:
    """#38: `[[design_evidence]]` entries, each {id, command, inputs}. An
    absent section is an empty list and changes nothing. Commands are
    trusted configuration at the same level as [checks].commands; the
    input globs are relative to the project root and may not escape it."""
    if not isinstance(value, list):
        raise HandsoffError("handsoff.toml: design_evidence must be an array of tables")
    if len(value) > MAX_DESIGN_EVIDENCE_ENTRIES:
        raise HandsoffError(
            f"handsoff.toml: design_evidence may hold at most {MAX_DESIGN_EVIDENCE_ENTRIES} entries"
        )
    entries = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise HandsoffError(f"handsoff.toml: design_evidence[{index}] must be a table")
        unknown = set(entry) - {"id", "command", "inputs"}
        if unknown:
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        artifact_id, command, inputs = entry.get("id"), entry.get("command"), entry.get("inputs")
        if not isinstance(artifact_id, str) or not DESIGN_EVIDENCE_ID_PATTERN.match(artifact_id):
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}].id must match [a-z0-9-]{{1,64}}"
            )
        if not isinstance(command, str) or not command.strip():
            raise HandsoffError(f"handsoff.toml: design_evidence[{index}].command must be a non-empty string")
        if not isinstance(inputs, list) or not inputs \
                or not all(isinstance(pattern, str) and pattern.strip() for pattern in inputs):
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}].inputs must be a non-empty array of glob strings"
            )
        for pattern in inputs:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise HandsoffError(
                    f"handsoff.toml: design_evidence[{index}].inputs must be relative globs inside the project root"
                )
        entries.append({"id": artifact_id, "command": command.strip(), "inputs": [p.strip() for p in inputs]})
    if len({entry["id"] for entry in entries}) != len(entries):
        raise HandsoffError("handsoff.toml: design_evidence ids must be unique")
    return entries


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


def validate_fallback_entries(value: object, *, field: str = "fallbacks") -> list[dict]:
    """Validate and copy one role's bounded, ordered fallback profiles."""
    if not isinstance(value, list):
        raise HandsoffError(f"{field} must be an array")
    if len(value) > MAX_FALLBACK_PROFILES:
        raise HandsoffError(f"{field} must contain at most {MAX_FALLBACK_PROFILES} profiles")
    result = []
    for index, profile in enumerate(value):
        if not isinstance(profile, dict) or set(profile) != {"adapter", "model"}:
            raise HandsoffError(f"{field}[{index}] must contain exactly adapter and model")
        adapter = profile.get("adapter")
        if adapter not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError(f"{field}[{index}].adapter must be exactly 'codex' or 'claude'")
        result.append({"adapter": adapter, "model": validate_agent_model(profile.get("model"))})
    return result


def validate_max_failovers(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_FALLBACK_PROFILES:
        raise HandsoffError(
            f"fallback_policy.max_failovers_per_role must be an integer from 0 to {MAX_FALLBACK_PROFILES}"
        )
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


def followup_reviewer_profile(cfg: dict) -> dict | None:
    """#37: the configured follow-up reviewer profile, or None when the run
    has no reviewer tiering (both keys absent, or a cfg predating #37)."""
    profile = cfg.get("reviewer_followup") if isinstance(cfg, dict) else None
    if not isinstance(profile, dict):
        return None
    return {"adapter": profile["adapter"], "model": profile["model"]}


def unavailable_adapter_message(role: str, adapter: str, model: str, resolution_source: str) -> str:
    """The refusal for a role whose adapter executable is not on PATH (#39),
    shared by the launcher and the tiered reviewer selection so the legacy
    wording is identical whichever path refuses."""
    origin = "the recommended default" if resolution_source == "recommended" else "the configured"
    return (
        f"{role} cannot launch: {origin} adapter {adapter} ({model}) is not available on PATH. "
        f"Install {adapter}, or set [agents].{role} and [models].{role} in handsoff.toml to an "
        f"installed adapter, or add a fallback_policy.{role} profile. Availability means only "
        "that the executable was found; it does not prove authentication, entitlement, "
        "network access, or model validity"
    )


def profile_sources(cfg: dict) -> dict:
    """Per-role adapter/model provenance (see PROFILE_SOURCES). A cfg built
    without load_config, or one predating #39, reads as fully explicit."""
    recorded = cfg.get("profile_sources") if isinstance(cfg, dict) else None
    result = {}
    for role in SELECTABLE_AGENT_ROLES:
        entry = recorded.get(role) if isinstance(recorded, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        result[role] = {
            "adapter": entry.get("adapter", EXPLICIT_PROFILE_SOURCE),
            "model": entry.get("model", EXPLICIT_PROFILE_SOURCE),
        }
    return result


def fallback_profiles(cfg: dict) -> dict:
    return {role: deepcopy(cfg.get("fallbacks", {}).get(role, [])) for role in SELECTABLE_AGENT_ROLES}


def default_agent_adapter(*, which=None) -> str | None:
    """Return the first installed runnable adapter in documented order."""
    lookup = which or shutil.which
    return next((adapter for adapter in DEFAULT_AGENT_PREFERENCE if lookup(adapter)), None)


def resolved_agent_profiles(cfg: dict, *, which=None, require_available: bool = False) -> dict:
    """Resolve auto/unconfigured roles without changing explicit selections.

    Each entry also carries ``source`` (adapter/model provenance, see
    PROFILE_SOURCES) so a recommended default is never mistaken for a
    choice the operator made.
    """
    configured = agent_profiles(cfg)
    sources = profile_sources(cfg)
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
        resolved[role] = {"adapter": adapter, "model": profile["model"], "source": dict(sources[role])}
    return resolved


def audited_agent_profile(cfg: dict, role: str) -> dict:
    """Snapshot configured and currently effective role selection for audit records."""
    configured = agent_profiles(cfg)[role]
    effective = resolved_agent_profiles(cfg)[role]
    return {
        "adapter": configured["adapter"],
        "model": configured["model"],
        "effective_adapter": effective["adapter"],
        "source": dict(profile_sources(cfg)[role]),
    }


def crew_view(cfg: dict, *, which=None) -> dict:
    """#39: the four requested profiles, their provenance, and whether each
    adapter executable is discoverable right now.

    ``available`` is executable discovery only (CREW_AVAILABILITY_SCOPE).
    Whether the model id is valid for claude or codex cannot be checked
    offline, so the view says what it checked instead of claiming more.
    ``which`` is injectable for tests; production uses shutil.which.
    """
    lookup = which or shutil.which
    resolved = resolved_agent_profiles(cfg, which=lookup)
    view = {}
    for role in SELECTABLE_AGENT_ROLES:
        profile = resolved[role]
        adapter = profile["adapter"]
        discovered = lookup(adapter) if adapter in SELECTABLE_AGENT_ADAPTERS else None
        executable = str(Path(discovered).resolve()) if discovered else None
        view[role] = {
            "adapter": adapter,
            "model": profile["model"],
            "adapter_source": profile["source"]["adapter"],
            "model_source": profile["source"]["model"],
            "available": executable is not None,
            "executable": executable,
            "availability_scope": CREW_AVAILABILITY_SCOPE,
        }
    return view


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
                      if re.match(r"^\s*\[\[?[^\]]+\]\]?\s*(?:#.*)?(?:\r?\n)?$", line)]
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


def _fallback_profile_toml(profile: dict) -> str:
    return f'{{ adapter = {json.dumps(profile["adapter"])}, model = {json.dumps(profile["model"])} }}'


def _replace_fallback_policy_table(lines: list[str], parsed: dict, fallbacks: dict, cap: int) -> list[str]:
    """Replace the framework-owned fallback table with canonical one-line values."""
    header_pattern = re.compile(r"^\s*\[fallback_policy\]\s*(?:#.*)?(?:\r?\n)?$")
    section_starts = [index for index, line in enumerate(lines)
                      if re.match(r"^\s*\[\[?[^\]]+\]\]?\s*(?:#.*)?(?:\r?\n)?$", line)]
    headers = [index for index in section_starts if header_pattern.match(lines[index])]
    if len(headers) > 1:
        raise HandsoffError("handsoff.toml: ambiguous duplicate [fallback_policy] tables")
    body = ["[fallback_policy]\n", f"max_failovers_per_role = {cap}\n"]
    body.extend(
        f"{role} = [{', '.join(_fallback_profile_toml(profile) for profile in fallbacks[role])}]\n"
        for role in SELECTABLE_AGENT_ROLES
    )
    if headers:
        start = headers[0]
        end = next((index for index in section_starts if index > start), len(lines))
        lines[start:end] = body + (["\n"] if end < len(lines) else [])
    elif "fallback_policy" in parsed:
        raise HandsoffError("handsoff.toml: unsupported [fallback_policy] table syntax")
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(body)
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
        load_config(root)  # Refuse any malformed existing fallback policy atomically.
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


def update_agent_settings(root: Path, payload: object) -> dict:
    """Atomically persist wrapped primary profiles, fallbacks, and the cap."""
    if not isinstance(payload, dict) or set(payload) != {
            "profiles", "fallbacks", "max_failovers_per_role"}:
        raise HandsoffError(
            "wrapped agent settings must contain exactly profiles, fallbacks, and max_failovers_per_role"
        )
    profiles = payload["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(SELECTABLE_AGENT_ROLES):
        raise HandsoffError("profiles must contain exactly all four agent roles")
    normalized_profiles = {}
    for role, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) != {"adapter", "model"}:
            raise HandsoffError(f"profiles.{role} must contain exactly adapter and model")
        adapter = profile.get("adapter")
        if adapter not in AGENT_SETTING_ADAPTERS:
            raise HandsoffError(f"profiles.{role}.adapter must be auto, codex, or claude")
        normalized_profiles[role] = {
            "adapter": adapter, "model": validate_agent_model(profile.get("model")),
        }
    fallbacks = payload["fallbacks"]
    if not isinstance(fallbacks, dict) or set(fallbacks) != set(SELECTABLE_AGENT_ROLES):
        raise HandsoffError("fallbacks must contain exactly all four agent roles")
    normalized_fallbacks = {
        role: validate_fallback_entries(fallbacks[role], field=f"fallbacks.{role}")
        for role in SELECTABLE_AGENT_ROLES
    }
    cap = validate_max_failovers(payload["max_failovers_per_role"])
    root = root.resolve()
    path = root / "handsoff.toml"
    if tomllib is None:
        raise HandsoffError("agent settings require Python 3.11+ TOML support")
    with project_lock(root):
        try:
            original = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(original)
            load_config(root)
        except (OSError, ValueError) as exc:
            raise HandsoffError(f"cannot update {path}: {exc}") from exc
        adapters = {role: normalized_profiles[role]["adapter"] for role in SELECTABLE_AGENT_ROLES}
        models = {role: normalized_profiles[role]["model"] for role in SELECTABLE_AGENT_ROLES}
        lines = _patch_toml_role_table(original.splitlines(keepends=True), parsed, "agents", adapters)
        lines = _patch_toml_role_table(lines, parsed, "models", models)
        lines = _replace_fallback_policy_table(lines, parsed, normalized_fallbacks, cap)
        proposed = "".join(lines)
        try:
            proposed_raw = tomllib.loads(proposed)
        except ValueError as exc:
            raise HandsoffError(f"refusing invalid proposed handsoff.toml: {exc}") from exc
        proposed_policy = proposed_raw.get("fallback_policy", {})
        # #37: compare the four roles only, so the optional reviewer_followup
        # keys survive a settings save untouched instead of failing it.
        proposed_agents = proposed_raw.get("agents", {})
        proposed_models = proposed_raw.get("models", {})
        if any(proposed_agents.get(role) != adapters[role] or proposed_models.get(role) != models[role]
               for role in SELECTABLE_AGENT_ROLES):
            raise HandsoffError("refusing ambiguous primary agent profile update")
        if proposed_policy.get("max_failovers_per_role") != cap:
            raise HandsoffError("refusing ambiguous fallback cap update")
        for role in SELECTABLE_AGENT_ROLES:
            if proposed_policy.get(role) != normalized_fallbacks[role]:
                raise HandsoffError("refusing ambiguous fallback profile update")
        _atomic_write_text(path, proposed)
    return {
        "profiles": normalized_profiles,
        "fallbacks": normalized_fallbacks,
        "max_failovers_per_role": cap,
    }


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


def _prune_agent_sessions(sessions: dict, current: dict) -> set[str]:
    """Bound status growth while retaining every role's current snapshot."""
    protected = {value for value in current.values() if isinstance(value, str)}
    removable = sorted(
        (session for sid, session in sessions.items()
         if sid not in protected and session.get("state") in AGENT_SESSION_TERMINAL_STATES),
        key=lambda session: (session.get("started_at", ""), session.get("session_id", "")),
    )
    removed = set()
    while len(sessions) >= MAX_AGENT_SESSIONS and removable:
        session_id = removable.pop(0)["session_id"]
        sessions.pop(session_id, None)
        removed.add(session_id)
    if len(sessions) >= MAX_AGENT_SESSIONS:
        raise HandsoffError("agent session history is full; no terminal session can be retired safely")
    return removed


def _implementer_binding(status: dict, cfg: dict) -> dict | None:
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    implementer = sessions.get(current.get("implementer"))
    identity = _canonical_implementer_identity(implementer)
    implementer_session_id = implementer.get("session_id") if identity and isinstance(implementer, dict) else None
    if identity is None:
        review_profile = (status.get("review") or {}).get("implementer_profile")
        identity = _canonical_implementer_identity(review_profile)
    if identity is None:
        configured = agent_profiles(cfg).get("implementer")
        identity = _canonical_implementer_identity(configured)
    if identity is None:
        return None
    return {
        "implementer_session_id": implementer_session_id,
        "adapter": identity[0], "model": identity[1],
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }


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


def _validate_session_reference(value: object, field: str) -> str | None:
    """#36: a session's packet_id/design_hash is null or a bounded non-empty string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise HandsoffError(f"agent session {field} must be null or a non-empty string of at most 64 characters")
    return value


def migrate_review_ledger(status: dict) -> None:
    """Attach the structured ledger to a pre-#31 status without inventing
    per-attempt facts that the old format never recorded."""
    if "review_attempts" in status:
        status.setdefault("legacy_review_round_offset", 0)
        status.setdefault("review_cap_overrides", [])
        status.setdefault("escalation", None)
        return
    legacy = status.get("review_round", 0)
    if not isinstance(legacy, int) or isinstance(legacy, bool) or legacy < 0:
        raise HandsoffError("trusted review round is invalid")
    status["legacy_review_round_offset"] = legacy
    status["review_attempts"] = []
    status["review_cap_overrides"] = []
    status.setdefault("escalation", None)


def effective_review_cap(status: dict, cfg: dict) -> int:
    offset = int(status.get("legacy_review_round_offset", 0) or 0)
    digest = config_hash(cfg)
    overrides = sum(
        1 for item in (status.get("review_cap_overrides") or [])
        if isinstance(item, dict) and item.get("config_hash") == digest
    )
    return max(int(cfg.get("max_review_rounds", 3)), offset) + overrides


def current_review_attempt(status: dict) -> dict | None:
    attempts = status.get("review_attempts") or []
    return attempts[-1] if attempts and attempts[-1].get("disposition") == "open" else None


def _review_cap_escalation(status: dict, cfg: dict) -> None:
    cap = effective_review_cap(status, cfg)
    used = int(status.get("review_round", 0) or 0)
    source = ((status.get("review_attempts") or [{}])[-1].get("attempt_id")
              if status.get("review_attempts") else "review-ledger")
    status["status"] = "blocked"
    status["escalation"] = {
        "kind": "review_cap_exhausted",
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": f"review budget exhausted ({used} of {cap} attempts used)",
        "required_action": "Run review-cap-override --by OPERATOR --reason TEXT",
        "source": source,
    }
    status["next_action"] = status["escalation"]["required_action"]


def open_review_attempt(status: dict, acceptance: dict, cfg: dict, *, by: str,
                        trigger: str | None = None, detail: str = "",
                        reviewer: str | None = None, session_id: str | None = None,
                        id_factory=None) -> dict:
    migrate_review_ledger(status)
    if status.get("status") == "complete" or status.get("phase_number") == 8:
        raise HandsoffError("review attempt cannot open on a completed run")
    existing = current_review_attempt(status)
    if existing is not None:
        if session_id and session_id not in existing["session_ids"]:
            if len(existing["session_ids"]) >= MAX_REVIEW_SESSION_IDS:
                raise HandsoffError("review attempt session history is full")
            existing["session_ids"].append(session_id)
        if reviewer:
            existing["reviewer"] = reviewer
        return existing
    used = int(status.get("review_round", 0) or 0)
    if used >= effective_review_cap(status, cfg):
        _review_cap_escalation(status, cfg)
        raise HandsoffError(
            f"review budget exhausted ({used} of {effective_review_cap(status, cfg)} attempts used)"
        )
    attempts = status["review_attempts"]
    if len(attempts) >= MAX_REVIEW_ATTEMPTS:
        raise HandsoffError("review attempt history is full")
    previous = attempts[-1] if attempts else None
    digest = acceptance_hash(acceptance.get("criteria", []))
    if trigger is None:
        if not previous and status.get("legacy_review_round_offset", 0) == 0:
            trigger = "initial"
        elif previous and previous.get("disposition") == "changes_requested":
            trigger = "changes_requested"
        elif previous and previous.get("acceptance_hash") != digest:
            trigger = "acceptance_changed"
        else:
            trigger = "implementation_changed"
    if trigger not in REVIEW_ATTEMPT_TRIGGERS:
        raise HandsoffError("review attempt trigger is invalid")
    attempt_id = _new_bounded_id(
        "ha", REVIEW_ATTEMPT_ID_PATTERN,
        {item.get("attempt_id") for item in attempts if isinstance(item, dict)}, id_factory,
    )
    now = datetime.now(timezone.utc).isoformat()
    attempt = {
        "attempt_id": attempt_id, "attempt": used + 1,
        "opened_at": now, "closed_at": None, "opened_by": validate_agent_actor(by),
        "reviewer": validate_agent_actor(reviewer) if reviewer else None,
        "session_ids": [session_id] if session_id else [],
        "trigger": trigger, "trigger_detail": str(detail or "")[:512],
        "acceptance_hash": digest, "phase_number": int(status.get("phase_number", 1)),
        "disposition": "open", "findings": [],
    }
    attempts.append(attempt)
    status["review_round"] = used + 1
    return attempt


def abandon_stale_review_attempt(status: dict, acceptance: dict) -> bool:
    attempt = current_review_attempt(status)
    if not attempt or attempt.get("acceptance_hash") == acceptance_hash(acceptance.get("criteria", [])):
        return False
    attempt["disposition"] = "abandoned"
    attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
    attempt["findings"] = [{"code": "other", "summary": "acceptance_changed"}]
    return True


def refresh_review_attempt_after_evidence(status: dict, acceptance: dict) -> dict | None:
    """Rebind an open review after an audited evidence-only mutation.

    ``acceptance_hash`` deliberately includes outcome state and evidence IDs,
    so attaching evidence while a reviewer is active changes that hash even
    though the criterion specification did not change. Evidence-recording
    commands call this helper in the same locked commit that writes the new
    evidence. Criterion mutations continue to use
    :func:`abandon_stale_review_attempt` instead.
    """
    attempt = current_review_attempt(status)
    if attempt is None:
        return None
    current = acceptance_hash(acceptance.get("criteria", []))
    previous = attempt.get("acceptance_hash")
    if previous == current:
        return None
    attempt["acceptance_hash"] = current
    return {
        "attempt_id": attempt["attempt_id"],
        "previous_acceptance_hash": previous,
        "acceptance_hash": current,
    }


def create_agent_session(root: Path, *, role: str, actor: str, adapter: str,
                         requested_model: str, resolution_source: str,
                         id_factory=None, packet_id: str | None = None,
                         design_hash: str | None = None, tier: str | None = None,
                         tier_reason: str | None = None) -> dict:
    """Commit the immutable launch snapshot before a managed child starts.

    The task/prompt, environment, runner output, credentials, and token data
    are deliberately not accepted by this API, so callers cannot accidentally
    persist them as telemetry. `packet_id`/`design_hash` (#36) record which
    delta review packet, if any, a Phase-2 reviewer was launched with.
    `tier`/`tier_reason` (#37) record which reviewer tier the selection
    chose and why; the session keeps `tier`, and a `design_reviewer_selected`
    event carrying both is committed with the launch.
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
    packet_id = _validate_session_reference(packet_id, "packet_id")
    design_hash = _validate_session_reference(design_hash, "design_hash")
    if tier is not None and tier not in DESIGN_REVIEWER_TIERS:
        raise HandsoffError(f"agent session tier must be null or one of {', '.join(DESIGN_REVIEWER_TIERS)}")
    if tier is not None and role != "reviewer":
        raise HandsoffError("agent session tier applies to reviewer sessions only")
    if tier is not None and tier_reason not in DESIGN_REVIEWER_SELECTION_REASONS:
        raise HandsoffError(
            f"agent session tier_reason must be one of {', '.join(DESIGN_REVIEWER_SELECTION_REASONS)}"
        )
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        ensure_no_launched_regression(status)
        schema_errors = validate_status_schema(status) or validate_acceptance_schema(acceptance)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        # #35: the sole authorization decision for a managed Phase-2
        # reviewer launch, taken here on the status re-read inside the
        # lock (build_launch_spec's pre-check is advisory only). At or
        # past the autonomous budget the launch needs an unconsumed,
        # unreserved Pilot authorization and takes it by writing this
        # session's id into launch_session_id in the same commit() that
        # records the launch, so a second concurrent launch finds the
        # reservation and is refused before any session or event exists.
        reservation = None
        budget = None
        if role == "reviewer" and status.get("phase_number") == 2:
            budget = design_review_budget(status, cfg)
            refusal = design_review_launch_refusal(budget, status)
            if refusal:
                raise HandsoffError(refusal)
            if budget["attempts"] >= budget["limit"]:
                reservation = deepcopy(status["design_review_authorization"])
        sessions = deepcopy(status.get("agent_sessions") or {})
        current = deepcopy(status.get("current_agent_sessions") or {})
        active_id = current.get(role)
        active = sessions.get(active_id) if isinstance(active_id, str) else None
        if active and active.get("state") in AGENT_SESSION_LIVE_STATES:
            raise HandsoffError(
                f"role {role} already has live agent session {active_id} ({active.get('state')})"
            )
        removed_session_ids = _prune_agent_sessions(sessions, current)
        session_id = _new_agent_session_id(sessions, id_factory=id_factory)
        now = datetime.now(timezone.utc).isoformat()
        if reservation is not None:
            reservation["launch_session_id"] = session_id
        proposed = deepcopy(status)
        opened_attempt = None
        # The convergence gate must run before a not-yet-launched session is
        # inserted into canonical state. A refused fourth attempt may persist
        # its escalation, but never a ghost `launching` worker.
        if role == "reviewer" and int(proposed.get("phase_number", 0) or 0) >= 4:
            migrate_review_ledger(proposed)
            before_id = (current_review_attempt(proposed) or {}).get("attempt_id")
            try:
                opened_attempt = open_review_attempt(
                    proposed, acceptance, cfg, by=actor, reviewer=actor, session_id=session_id,
                )
            except HandsoffError as exc:
                if isinstance(proposed.get("escalation"), dict) \
                        and proposed["escalation"].get("kind") == "review_cap_exhausted":
                    commit(
                        root, cfg, status=proposed,
                        event_kind="review_attempt_refused",
                        event_message=str(exc), role=role, actor=actor,
                        review_round=proposed.get("review_round"),
                        effective_max_review_rounds=effective_review_cap(proposed, cfg),
                    )
                raise
            if opened_attempt.get("attempt_id") == before_id:
                opened_attempt = None
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
            "packet_id": packet_id,
            "design_hash": design_hash,
            "tier": tier,
        }
        sessions[session_id] = session
        current[role] = session_id
        accepted_regression = active_regression_request(proposed)
        if accepted_regression and accepted_regression.get("state") == "accepted":
            accepted_regression["state"] = "invalidated"
            accepted_regression["completed_at"] = now
        proposed["agent_sessions"] = sessions
        proposed["current_agent_sessions"] = current
        if reservation is not None:
            proposed["design_review_authorization"] = reservation
        failures = {
            sid: deepcopy(failure) for sid, failure in (status.get("agent_failures") or {}).items()
            if sid not in removed_session_ids
        }
        if failures:
            proposed["agent_failures"] = failures
        else:
            proposed.pop("agent_failures", None)
        bindings = {
            sid: deepcopy(binding) for sid, binding in (status.get("reviewer_implementer_bindings") or {}).items()
            if sid in sessions
        }
        binding = _implementer_binding(status, cfg) if role == "reviewer" else None
        if binding is not None:
            bindings[session_id] = {"reviewer_session_id": session_id, **binding}
        if bindings:
            proposed["reviewer_implementer_bindings"] = bindings
        else:
            proposed.pop("reviewer_implementer_bindings", None)
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        extra_events = []
        if tier is not None:
            # #37: the selection is a fact of its own, logged before the
            # launch event it explains (extra_events precede the primary).
            extra_events.append({
                "kind": "design_reviewer_selected",
                "message": f"Design reviewer tier {tier} selected ({tier_reason})",
                "session_id": session_id, "role": role, "tier": tier, "reason": tier_reason,
                "adapter": adapter, "requested_model": requested_model,
                "design_review_attempt": budget["next_attempt"] if budget else None,
            })
        if opened_attempt:
            extra_events.append({
                "kind": "review_attempt_opened",
                "message": f"Implementation review attempt {opened_attempt['attempt']} opened",
                "attempt_id": opened_attempt["attempt_id"],
                "attempt": opened_attempt["attempt"],
                "trigger": opened_attempt["trigger"],
                "reviewer": opened_attempt["reviewer"],
            })
        commit(
            root, cfg, status=proposed,
            extra_events=extra_events or None,
            event_kind="agent_session_launching",
            event_message=f"Managed {role} agent session is launching",
            session_id=session_id, role=role, actor=actor, adapter=adapter,
            requested_model=requested_model, reported_model=None,
            resolution_source=resolution_source, state="launching",
            implementer_binding={"adapter": binding["adapter"], "model": binding["model"]}
            if binding else None,
            design_review_attempt=budget["next_attempt"] if budget else None,
            design_review_authorization_reserved=reservation is not None,
            packet_id=packet_id, design_hash=design_hash, tier=tier,
        )
        return deepcopy(session)


def _validate_failure_classification(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"category", "reason", "tail_sha256"}:
        raise HandsoffError("agent failure classification is invalid")
    category = value.get("category")
    if category not in FAILURE_CATEGORIES or value.get("reason") != _FAILURE_REASON_LABELS.get(category):
        raise HandsoffError("agent failure classification is not from the closed set")
    digest = value.get("tail_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HandsoffError("agent failure classification digest is invalid")
    return {"category": category, "reason": value["reason"], "tail_sha256": digest}


def transition_agent_session(root: Path, session_id: str, state: str,
                             *, exit_code: int | None = None,
                             failure: dict | None = None) -> dict:
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
    if failure is not None:
        failure = _validate_failure_classification(failure)
        if not terminal or state == "completed" or failure["category"] == "still_running":
            raise HandsoffError("failure classification requires a failed terminal session")
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
        replacement = next((item for item in proposed.get("agent_replacements", [])
                            if item.get("action") == "launch"
                            and item.get("to_session_id") == session_id), None)
        if replacement is not None and replacement.get("state") != (
                "claimed" if state == "running" else "running" if old_state == "running" else "claimed"):
            raise HandsoffError("replacement lifecycle does not match its claimed session")
        now = datetime.now(timezone.utc).isoformat()
        updated["state"] = state
        if state == "running":
            updated["running_at"] = now
            if replacement is not None:
                replacement["state"] = "running"
                replacement["running_at"] = now
                replacement["handoff"]["state"] = "running"
                replacement["handoff"]["running_at"] = now
        else:
            updated["ended_at"] = now
            updated["exit_code"] = exit_code
            if replacement is not None:
                replacement_state = "recovered" if state == "completed" else "failed"
                replacement["state"] = replacement_state
                replacement["ended_at"] = now
                replacement["handoff"]["state"] = replacement_state
                replacement["handoff"]["ended_at"] = now
            if failure is not None:
                failures = proposed.setdefault("agent_failures", {})
                failures[session_id] = {"session_id": session_id, **failure, "at": now}
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        event_kind = f"agent_session_{state}"
        commit(
            root, cfg, status=proposed,
            event_kind=event_kind,
            event_message=f"Managed {role} agent session is {state.replace('_', ' ')}",
            session_id=session_id, role=role, state=state, exit_code=exit_code,
            failure_category=failure["category"] if failure else None,
            replacement_id=replacement.get("replacement_id") if replacement else None,
            replacement_state=replacement.get("state") if replacement else None,
        )
        result = deepcopy(updated)
    if terminal:
        update_session_liveness(root, session_id, remove=True)
    return result


def repository_snapshot(root: Path, *, runner=subprocess.run) -> dict:
    """Return bounded exact git identity without retaining porcelain text."""
    def git(*args: str) -> str:
        try:
            result = runner(
                ["git", *args], cwd=str(root.resolve()), shell=False, text=True,
                capture_output=True, timeout=3, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HandsoffError(f"cannot establish repository identity: {type(exc).__name__}") from exc
        return result.stdout
    head = git("rev-parse", "HEAD").strip()
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").strip().split()
    branch = git("branch", "--show-current").strip() or "(detached)"
    porcelain = git("status", "--porcelain=v1")
    tracked_diff = git("diff", "--binary", "HEAD")
    untracked = [line for line in git("ls-files", "--others", "--exclude-standard").splitlines() if line]
    content = bytearray(tracked_diff.encode("utf-8", "replace"))
    for relative in sorted(untracked):
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
            if path.is_file():
                content.extend(relative.encode("utf-8", "replace") + b"\0" + path.read_bytes())
        except (OSError, ValueError):
            raise HandsoffError("cannot establish repository content identity") from None
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head) or len(branch) > 256:
        raise HandsoffError("cannot establish bounded repository identity")
    # Bind the reviewed change range, not merely HEAD's immediate parent.
    # Prefer the remote's declared default branch, then conventional local
    # names. Repositories without one retain the safe parent fallback.
    base = None
    candidates = []
    try:
        symbolic = runner(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=str(root.resolve()), shell=False, text=True, capture_output=True,
            timeout=3, check=False,
        )
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            candidates.append(symbolic.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.extend(["refs/remotes/origin/main", "main", "master"])
    for candidate in dict.fromkeys(candidates):
        try:
            merged = runner(
                ["git", "merge-base", "HEAD", candidate], cwd=str(root.resolve()),
                shell=False, text=True, capture_output=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if merged.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", merged.stdout.strip()):
            base = merged.stdout.strip().lower()
            break
    return {
        "path": str(root.resolve()), "head": head.lower(), "branch": branch, "dirty": bool(porcelain),
        "status_sha256": hashlib.sha256(porcelain.encode("utf-8", "replace")).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "commit_pair": {"before": base or (parents[1].lower() if len(parents) > 1 else head.lower()),
                        "after": head.lower()},
    }


def _new_bounded_id(prefix: str, pattern: re.Pattern, existing: set[str], id_factory=None) -> str:
    factory = id_factory or (lambda: f"{prefix}-{uuid.uuid4().hex}")
    for _ in range(16):
        candidate = factory()
        if not isinstance(candidate, str) or not pattern.fullmatch(candidate):
            raise HandsoffError(f"generated {prefix} id is invalid")
        if candidate not in existing:
            return candidate
    raise HandsoffError(f"could not allocate a collision-free {prefix} id")


def record_quality_finding(root: Path, *, session_id: str, finding_code: str,
                           id_factory=None) -> dict:
    """Authenticate a closed quality observation for the current completed session."""
    if finding_code not in QUALITY_FINDING_CODES:
        raise HandsoffError("quality finding code is not from the closed set")
    with project_lock(root.resolve()):
        root = root.resolve()
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        errors = validate_status_schema(status)
        if errors:
            raise HandsoffError(errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict) or session.get("state") != "completed" \
                or (status.get("current_agent_sessions") or {}).get(session.get("role")) != session_id:
            raise HandsoffError("quality findings require the current completed session")
        findings = list(status.get("agent_quality_findings") or [])
        if len(findings) >= MAX_QUALITY_FINDINGS:
            raise HandsoffError("quality finding history is full")
        finding_id = _new_bounded_id(
            "hq", QUALITY_FINDING_ID_PATTERN,
            {item.get("finding_id") for item in findings if isinstance(item, dict)}, id_factory,
        )
        trusted_round = status.get("review_round", 0)
        if not isinstance(trusted_round, int) or isinstance(trusted_round, bool) or trusted_round < 0:
            raise HandsoffError("trusted review round is invalid")
        if any(item.get("session_id") == session_id and item.get("review_round") == trusted_round
               for item in findings):
            raise HandsoffError("quality finding already exists for this session and review round")
        count = sum(1 for item in findings if item.get("session_id") == session_id) + 1
        distinct_review_rounds = {
            item.get("review_round") for item in findings
            if item.get("session_id") == session_id
            and isinstance(item.get("review_round"), int) and item.get("review_round") > 0
        }
        if trusted_round > 0:
            distinct_review_rounds.add(trusted_round)
        distinct_round_count = len(distinct_review_rounds)
        now = datetime.now(timezone.utc).isoformat()
        finding = {
            "finding_id": finding_id, "session_id": session_id, "role": session["role"],
            "code": finding_code, "count": count, "limit": cfg["max_review_rounds"],
            "review_round": trusted_round,
            "distinct_round_count": distinct_round_count,
            "eligible": should_failover_for_quality(
                retry_count=distinct_round_count, retry_limit=cfg["max_review_rounds"], finding_id=finding_id,
            ), "at": now,
        }
        proposed = deepcopy(status)
        proposed["agent_quality_findings"] = [*findings, finding]
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="agent_quality_finding",
               event_message="Trusted agent quality finding recorded",
               finding_id=finding_id, session_id=session_id, role=session["role"],
               finding_code=finding_code, count=count, review_round=trusted_round,
               distinct_round_count=distinct_round_count,
               eligible=finding["eligible"])
        return deepcopy(finding)


def _replacement_chain(status: dict, from_session_id: str) -> tuple[list[tuple[str, str]], int]:
    sessions = status.get("agent_sessions") or {}
    replacements = status.get("agent_replacements") or []
    attempted = []
    cursor = from_session_id
    count = 0
    while cursor:
        session = sessions.get(cursor)
        if not isinstance(session, dict):
            break
        identity = (session.get("adapter"), session.get("requested_model"))
        if identity not in attempted:
            attempted.append(identity)
        parent = next((r for r in reversed(replacements)
                       if r.get("action") == "launch" and r.get("to_session_id") == cursor), None)
        if parent is None:
            break
        count += 1
        cursor = parent.get("from_session_id")
    return attempted, count


def _derive_replacement_handoff(status: dict, acceptance: dict, repository: dict,
                                *, role: str, from_session_id: str, to_session_id: str,
                                trigger: str, category: str, reason: str,
                                attempt: int, cap: int, profile: dict) -> dict:
    criteria = acceptance.get("criteria", [])
    passing = [item.get("id") for item in criteria if item.get("state") == "passing"][:128]
    remaining = [item.get("id") for item in criteria if item.get("state") != "passing"][:128]
    evidence = []
    for item in criteria:
        for evidence_id in item.get("evidence", []):
            if evidence_id not in evidence and len(evidence) < 256:
                evidence.append(evidence_id)
    source = (status.get("agent_sessions") or {}).get(from_session_id, {})
    return {
        "role": role, "from_session_id": from_session_id, "to_session_id": to_session_id,
        "trigger": trigger, "category": category, "reason": reason,
        "attempt": attempt, "cap": cap, "phase_number": status["phase_number"],
        "progress": status["progress"], "passing_criterion_ids": passing,
        "remaining_criterion_ids": remaining, "evidence_ids": evidence,
        "repository": repository, "selected_profile": deepcopy(profile),
        "from_state": source.get("state"), "from_started_at": source.get("started_at"),
        "from_running_at": source.get("running_at"), "from_ended_at": source.get("ended_at"),
        "state": "reserved", "created_at": datetime.now(timezone.utc).isoformat(),
    }


def reserve_agent_replacement(root: Path, *, from_session_id: str,
                              trigger: str = "runtime_failure", finding_id: str | None = None,
                              which=shutil.which, snapshotter=repository_snapshot,
                              session_id_factory=None, replacement_id_factory=None) -> dict:
    """Atomically reserve one trusted fallback; caller cannot supply failure category or handoff."""
    if trigger not in AGENT_REPLACEMENT_TRIGGERS:
        raise HandsoffError("replacement trigger is invalid")
    with project_lock(root.resolve()):
        root = root.resolve()
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        ensure_no_launched_regression(status)
        errors = validate_status_schema(status) or validate_acceptance_schema(acceptance)
        if errors:
            raise HandsoffError(errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        sessions = deepcopy(status.get("agent_sessions") or {})
        current = deepcopy(status.get("current_agent_sessions") or {})
        source = sessions.get(from_session_id)
        if not isinstance(source, dict) or current.get(source.get("role")) != from_session_id:
            raise HandsoffError("replacement lost the current-session CAS")
        role = source["role"]
        # Repository identity and availability belong to this exact CAS
        # moment, so derive both while the same lock protects the source.
        repository = snapshotter(root)
        availability = {adapter: bool(which(adapter)) for adapter in SELECTABLE_AGENT_ADAPTERS}
        forced_pause_reason = None
        if source.get("state") in AGENT_SESSION_LIVE_STATES:
            category, reason = "still_running", _FAILURE_REASON_LABELS["still_running"]
        elif trigger == "runtime_failure":
            failure = (status.get("agent_failures") or {}).get(from_session_id)
            if not isinstance(failure, dict):
                raise HandsoffError("runtime replacement requires an authenticated session failure")
            category, reason = failure.get("category"), failure.get("reason")
        else:
            finding = next((item for item in status.get("agent_quality_findings", [])
                            if item.get("finding_id") == finding_id
                            and item.get("session_id") == from_session_id), None)
            if source.get("state") != "completed" or not finding:
                raise HandsoffError("quality replacement requires a trusted finding for a completed session")
            if finding.get("eligible"):
                category, reason = "non_zero_exit", "bounded quality finding reached review limit"
            else:
                category, reason = "unknown", "quality finding has not reached the review limit"
                forced_pause_reason = "quality_boundary_not_reached"
        attempted, failover_count = _replacement_chain(status, from_session_id)
        implementer = None
        if role == "reviewer":
            binding = (status.get("reviewer_implementer_bindings") or {}).get(from_session_id)
            if isinstance(binding, dict):
                implementer = {"adapter": binding.get("adapter"), "requested_model": binding.get("model")}
        decision = _fallback_decision("pilot_pause", forced_pause_reason) if forced_pause_reason else \
            plan_agent_fallback(
                role, category, cfg["fallbacks"][role], availability, attempted,
                failover_count, cfg["max_failovers_per_role"], implementer,
            )
        records = list(status.get("agent_replacements") or [])
        if len(records) >= MAX_AGENT_REPLACEMENTS:
            raise HandsoffError("agent replacement history is full")
        replacement_id = _new_bounded_id(
            "hr", REPLACEMENT_ID_PATTERN,
            {item.get("replacement_id") for item in records if isinstance(item, dict)},
            replacement_id_factory,
        )
        now = datetime.now(timezone.utc).isoformat()
        if decision["action"] != "select":
            record = {
                "replacement_id": replacement_id, "role": role,
                "from_session_id": from_session_id, "to_session_id": None,
                "trigger": trigger, "category": category, "reason": decision["reason"],
                "attempt": failover_count, "cap": cfg["max_failovers_per_role"],
                "action": "pilot_pause", "planner_reason": decision["reason"],
                "skipped": deepcopy(decision["skipped"]),
                "selected_profile": None, "handoff": None, "state": "pilot_pause", "at": now,
            }
            proposed = deepcopy(status)
            proposed["agent_replacements"] = [*records, record]
            errors = validate_status_schema(proposed)
            if errors:
                raise HandsoffError(errors[0])
            commit(root, cfg, status=proposed, event_kind="agent_replacement_pilot_pause",
                   event_message="Agent replacement paused for Pilot review",
                   replacement_id=replacement_id, session_id=from_session_id, role=role,
                   trigger=trigger, category=category, reason=decision["reason"],
                   replacement_state="pilot_pause")
            return deepcopy(record)
        removed_session_ids = _prune_agent_sessions(sessions, current)
        to_session_id = _new_agent_session_id(sessions, id_factory=session_id_factory)
        profile = decision["profile"]
        session = {
            "session_id": to_session_id, "role": role,
            "actor": default_agent_actor(profile["adapter"], role),
            "adapter": profile["adapter"], "requested_model": profile["model"],
            "reported_model": None, "resolution_source": "fallback",
            "started_at": now, "running_at": None, "ended_at": None,
            "state": "launching", "exit_code": None,
            "packet_id": None, "design_hash": None, "tier": None,
        }
        handoff = _derive_replacement_handoff(
            status, acceptance, repository, role=role, from_session_id=from_session_id,
            to_session_id=to_session_id, trigger=trigger, category=category, reason=reason,
            attempt=failover_count + 1, cap=cfg["max_failovers_per_role"], profile=profile,
        )
        record = {
            "replacement_id": replacement_id, "role": role,
            "from_session_id": from_session_id, "to_session_id": to_session_id,
            "trigger": trigger, "category": category, "reason": reason,
            "attempt": failover_count + 1, "cap": cfg["max_failovers_per_role"],
            "action": "launch", "planner_reason": decision["reason"],
            "skipped": deepcopy(decision["skipped"]), "selected_profile": deepcopy(profile),
            "handoff": handoff, "state": "reserved", "at": now,
        }
        proposed = deepcopy(status)
        proposed["agent_sessions"] = sessions
        proposed["agent_sessions"][to_session_id] = session
        proposed["current_agent_sessions"] = current
        proposed["current_agent_sessions"][role] = to_session_id
        proposed["agent_replacements"] = [*records, record]
        failures = {
            sid: deepcopy(failure) for sid, failure in (status.get("agent_failures") or {}).items()
            if sid not in removed_session_ids
        }
        if failures:
            proposed["agent_failures"] = failures
        else:
            proposed.pop("agent_failures", None)
        bindings = deepcopy(status.get("reviewer_implementer_bindings") or {})
        bindings = {sid: item for sid, item in bindings.items() if sid in sessions}
        if role == "reviewer" and isinstance(bindings.get(from_session_id), dict):
            copied = deepcopy(bindings[from_session_id])
            copied["reviewer_session_id"] = to_session_id
            bindings[to_session_id] = copied
        if bindings:
            proposed["reviewer_implementer_bindings"] = bindings
        else:
            proposed.pop("reviewer_implementer_bindings", None)
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="agent_replacement_reserved",
               event_message=f"Managed {role} replacement session is reserved",
               replacement_id=replacement_id, from_session_id=from_session_id,
               to_session_id=to_session_id, role=role, trigger=trigger,
               category=category, reason=reason, attempt=failover_count + 1,
               replacement_state="reserved")
        return deepcopy(record)


def claim_precreated_agent_session(root: Path, session_id: str, *, role: str,
                                   adapter: str, requested_model: str) -> dict:
    """One-way pre-spawn claim of the exact reserved session/profile."""
    with project_lock(root.resolve()):
        root = root.resolve()
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        errors = validate_status_schema(status)
        if errors:
            raise HandsoffError(errors[0])
        _assert_agent_telemetry_integrity(root, cfg, status)
        session = (status.get("agent_sessions") or {}).get(session_id)
        records = status.get("agent_replacements", [])
        reservation_index = next((index for index, item in enumerate(records)
                                  if item.get("action") == "launch"
                                  and item.get("to_session_id") == session_id), None)
        reservation = records[reservation_index] if reservation_index is not None else None
        if not isinstance(session, dict) or session.get("state") != "launching" \
                or (status.get("current_agent_sessions") or {}).get(role) != session_id \
                or not isinstance(reservation, dict) \
                or reservation.get("state") != "reserved" \
                or not isinstance(reservation.get("handoff"), dict) \
                or reservation["handoff"].get("state") != "reserved" \
                or reservation.get("selected_profile") != {
                    "adapter": adapter, "model": requested_model,
                } \
                or (session.get("role"), session.get("adapter"), session.get("requested_model")) \
                != (role, adapter, requested_model):
            raise HandsoffError("precreated replacement session does not match the exact reservation")
        proposed = deepcopy(status)
        claimed = proposed["agent_replacements"][reservation_index]
        now = datetime.now(timezone.utc).isoformat()
        claimed["state"] = "claimed"
        claimed["claimed_at"] = now
        claimed["handoff"]["state"] = "claimed"
        claimed["handoff"]["claimed_at"] = now
        # #35: a runtime-failure replacement continues the SAME authorized
        # design-review attempt, so the reservation follows it to the new
        # session id; it never becomes a second attempt.
        authorization = proposed.get("design_review_authorization")
        if isinstance(authorization, dict) and authorization.get("consumed_at") is None \
                and authorization.get("launch_session_id") == claimed.get("from_session_id"):
            authorization["launch_session_id"] = session_id
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(
            root, cfg, status=proposed,
            event_kind="agent_replacement_claimed",
            event_message=f"Managed {role} replacement reservation was claimed",
            replacement_id=claimed["replacement_id"], from_session_id=claimed["from_session_id"],
            to_session_id=session_id, role=role, state="claimed",
        )
        return deepcopy(session)


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
    if last_record is None:
        if status_path(root, cfg).exists() or acceptance_path(root, cfg).exists():
            problems.append("event log is missing or empty while project state exists")
    else:
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
    work_items = acceptance.get("work_items")
    if work_items is not None:
        if not isinstance(work_items, list) or not work_items or len(work_items) > MAX_WORK_ITEMS:
            errors.append(f"acceptance: 'work_items' must contain 1 to {MAX_WORK_ITEMS} entries")
            work_items = []
        seen_work_items = set()
        required = {"id", "kind", "number", "title", "url", "required", "github_state",
                    "github_checked_at", "created_at", "updated_at", "notes"}
        for index, item in enumerate(work_items):
            label = f"acceptance: work_items[{index}]"
            if not isinstance(item, dict) or set(item) != required:
                errors.append(f"{label} has invalid fields")
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not WORK_ITEM_ID_PATTERN.fullmatch(item_id) or item_id in seen_work_items:
                errors.append(f"{label}.id is invalid or duplicated")
            seen_work_items.add(item_id)
            if item.get("kind") not in WORK_ITEM_KINDS:
                errors.append(f"{label}.kind is invalid")
            number = item.get("number")
            if (item.get("kind") == "issue") != (isinstance(number, int) and not isinstance(number, bool) and number > 0):
                errors.append(f"{label}.number must match its kind")
            if not isinstance(item.get("title"), str) or not item["title"].strip() or len(item["title"]) > 200:
                errors.append(f"{label}.title is invalid")
            if not isinstance(item.get("url"), str) or not isinstance(item.get("notes"), str) or len(item["notes"]) > 512:
                errors.append(f"{label} display fields are invalid")
            if not isinstance(item.get("required"), bool) or item.get("github_state") not in {None, "open", "closed"}:
                errors.append(f"{label} state fields are invalid")
            for field in ("created_at", "updated_at"):
                try:
                    parsed = datetime.fromisoformat(item.get(field))
                    if parsed.tzinfo is None:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{label}.{field} must be a timezone-aware timestamp")
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


def _design_review_findings_errors(findings: object, label: str) -> list[str]:
    """#36: a bounded list of {id, text} findings, ids in the F<attempt>.<n> form."""
    if not isinstance(findings, list):
        return [f"status: '{label}' must be an array"]
    if len(findings) > MAX_DESIGN_REVIEW_FINDINGS:
        return [f"status: '{label}' must contain at most {MAX_DESIGN_REVIEW_FINDINGS} findings"]
    errors: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"id", "text"}:
            errors.append(f"status: '{label}' entries must be objects with exactly id and text")
            continue
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not DESIGN_REVIEW_FINDING_ID_PATTERN.fullmatch(finding_id):
            errors.append(f"status: '{label}' id {finding_id!r} is not a finding id")
        elif finding_id in seen:
            errors.append(f"status: '{label}' repeats finding id {finding_id}")
        else:
            seen.add(finding_id)
        text = finding.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_DESIGN_REVIEW_FINDING_LENGTH:
            errors.append(f"status: '{label}' text must be a non-empty string of at most "
                          f"{MAX_DESIGN_REVIEW_FINDING_LENGTH} characters")
    return errors


def _design_reviewer_profile_errors(profile: object, label: str) -> list[str]:
    """#37: {adapter, model, tier, reason} from the closed vocabularies."""
    if not isinstance(profile, dict) or set(profile) != set(DESIGN_REVIEWER_PROFILE_FIELDS):
        return [f"status: '{label}' must have exactly the keys {sorted(DESIGN_REVIEWER_PROFILE_FIELDS)}"]
    errors: list[str] = []
    for key in ("adapter", "model"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            errors.append(f"status: '{label}.{key}' must be a non-empty string")
    if profile.get("tier") not in DESIGN_REVIEWER_TIERS:
        errors.append(f"status: '{label}.tier' must be one of {', '.join(DESIGN_REVIEWER_TIERS)}")
    if profile.get("reason") not in DESIGN_REVIEWER_SELECTION_REASONS:
        errors.append(f"status: '{label}.reason' must be one of {', '.join(DESIGN_REVIEWER_SELECTION_REASONS)}")
    return errors


def _design_reviewer_escalation_errors(escalation: object) -> list[str]:
    """#37: absent or null when no Pilot escalation is recorded; when present
    a complete {by, at, note, consumed_at} record, so a hand edit refuses
    cleanly instead of being read by the selection as an open escalation."""
    if escalation is None:
        return []
    if not isinstance(escalation, dict):
        return ["status: 'design_reviewer_escalation' must be an object or null"]
    errors: list[str] = []
    if set(escalation) != set(DESIGN_REVIEWER_ESCALATION_FIELDS):
        errors.append("status: 'design_reviewer_escalation' must have exactly the keys "
                      f"{sorted(DESIGN_REVIEWER_ESCALATION_FIELDS)}")
    if not isinstance(escalation.get("by"), str) or not escalation["by"].strip():
        errors.append("status: 'design_reviewer_escalation.by' must be a non-empty string")
    for sub in ("at", "consumed_at"):
        value = escalation.get(sub)
        if value is None and sub == "consumed_at":
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"status: 'design_reviewer_escalation.{sub}' must be a non-empty string"
                          + (" or null" if sub == "consumed_at" else ""))
            continue
        try:
            if datetime.fromisoformat(value).tzinfo is None:
                errors.append(f"status: 'design_reviewer_escalation.{sub}' must include a timezone")
        except ValueError:
            errors.append(f"status: 'design_reviewer_escalation.{sub}' must be an ISO-8601 timestamp")
    note = escalation.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        errors.append("status: 'design_reviewer_escalation.note' must be a non-empty string or null")
    return errors


def _design_review_history_errors(history: object) -> list[str]:
    """#36: absent on a legacy status; when present, at most 8 complete entries."""
    if history is None:
        return []
    if not isinstance(history, list):
        return ["status: 'design_review_history' must be an array"]
    if len(history) > MAX_DESIGN_REVIEW_HISTORY:
        return [f"status: 'design_review_history' must contain at most {MAX_DESIGN_REVIEW_HISTORY} entries"]
    errors: list[str] = []
    for index, entry in enumerate(history):
        label = f"status: design_review_history[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(entry) != set(DESIGN_REVIEW_HISTORY_FIELDS):
            errors.append(f"{label} must have exactly the keys {sorted(DESIGN_REVIEW_HISTORY_FIELDS)}")
            continue
        attempt = entry["attempt"]
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            errors.append(f"{label}.attempt must be a positive integer")
        if entry["decision"] not in {"approved", "changes_requested"}:
            errors.append(f"{label}.decision must be 'approved' or 'changes_requested'")
        for field in ("by", "design_hash"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                errors.append(f"{label}.{field} must be a non-empty string")
        if entry["head"] is not None and (not isinstance(entry["head"], str) or not entry["head"].strip()):
            errors.append(f"{label}.head must be a non-empty string or null")
        ids = entry["criteria_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids) \
                or ids != sorted(ids):
            errors.append(f"{label}.criteria_ids must be a sorted array of criterion ids")
        hashes = entry["criterion_hashes"]
        if not isinstance(hashes, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and v for k, v in hashes.items()):
            errors.append(f"{label}.criterion_hashes must map criterion ids to spec hashes")
        if not isinstance(entry["structural_blocker"], bool):
            errors.append(f"{label}.structural_blocker must be boolean")
        errors.extend(_design_review_findings_errors(entry["findings"], f"design_review_history[{index}].findings"))
    return errors


def _design_review_packet_errors(packet: object) -> list[str]:
    """#36: null or the latest generated packet with its load-bearing fields typed."""
    if packet is None:
        return []
    if not isinstance(packet, dict):
        return ["status: 'design_review_packet' must be an object or null"]
    errors: list[str] = []
    packet_id = packet.get("packet_id")
    if not isinstance(packet_id, str) or not DESIGN_REVIEW_PACKET_ID_PATTERN.fullmatch(packet_id):
        errors.append("status: 'design_review_packet.packet_id' must be 32 hex characters")
    attempt = packet.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 2:
        errors.append("status: 'design_review_packet.attempt' must be an integer of at least 2")
    previous = packet.get("previous_attempt")
    if not isinstance(previous, int) or isinstance(previous, bool) or previous < 1:
        errors.append("status: 'design_review_packet.previous_attempt' must be a positive integer")
    for field in ("design_hash", "previous_design_hash"):
        if not isinstance(packet.get(field), str) or not packet[field].strip():
            errors.append(f"status: 'design_review_packet.{field}' must be a non-empty string")
    if not isinstance(packet.get("stale"), bool):
        errors.append("status: 'design_review_packet.stale' must be boolean")
    reasons = packet.get("stale_reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        errors.append("status: 'design_review_packet.stale_reasons' must be an array of strings")
    if not isinstance(packet.get("criteria_delta"), dict):
        errors.append("status: 'design_review_packet.criteria_delta' must be an object")
    findings = packet.get("findings")
    if not isinstance(findings, list) or not all(
            isinstance(f, dict) and f.get("disposition") in DESIGN_REVIEW_DISPOSITIONS for f in findings):
        errors.append("status: 'design_review_packet.findings' must be an array of dispositioned findings")
    if "truncated" in packet and (not isinstance(packet["truncated"], dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in packet["truncated"].values())):
        errors.append("status: 'design_review_packet.truncated' must map field names to dropped counts")
    return errors


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
    # 'human_pause' is the durable record of an open human-pause-start
    # (#34): absent or null on any status.json written before the field
    # existed, or whenever no pause is open. When PRESENT it must be a
    # complete object so a hand edit refuses cleanly instead of being
    # read by stall_warning()/activity_note() as an open pause.
    if "human_pause" in status and status["human_pause"] is not None:
        pause = status["human_pause"]
        if not isinstance(pause, dict):
            errors.append("status: 'human_pause' must be an object or null")
        else:
            expected = {"by", "since", "note"}
            if set(pause) != expected:
                errors.append(f"status: 'human_pause' must have exactly the keys {sorted(expected)}")
            if not isinstance(pause.get("by"), str) or not pause["by"].strip():
                errors.append("status: 'human_pause.by' must be a non-empty string")
            since = pause.get("since")
            if not isinstance(since, str) or not since.strip():
                errors.append("status: 'human_pause.since' must be a non-empty string")
            else:
                try:
                    parsed = datetime.fromisoformat(since)
                    if parsed.tzinfo is None:
                        errors.append("status: 'human_pause.since' must include a timezone")
                except ValueError:
                    errors.append("status: 'human_pause.since' must be an ISO-8601 timestamp")
            note = pause.get("note")
            if note is not None and (not isinstance(note, str) or not note.strip()):
                errors.append("status: 'human_pause.note' must be a non-empty string or null")
    # #35: the cumulative attempt counter and the Pilot's one-attempt
    # authorization. Both absent on any status.json written before the
    # fields existed (read as zero attempts / no authorization); when
    # PRESENT they are held to a strict shape so a hand edit refuses
    # cleanly instead of being read by design_review_budget() as budget.
    if "design_review_attempts" in status and (
            not isinstance(status["design_review_attempts"], int)
            or isinstance(status["design_review_attempts"], bool)
            or status["design_review_attempts"] < 0):
        errors.append("status: 'design_review_attempts' must be a non-negative integer, "
                      f"got {status['design_review_attempts']!r}")
    if "design_review_authorization" in status and status["design_review_authorization"] is not None:
        authorization = status["design_review_authorization"]
        if not isinstance(authorization, dict):
            errors.append("status: 'design_review_authorization' must be an object or null")
        else:
            expected = {"by", "at", "note", "attempt_permitted", "launch_session_id", "consumed_at"}
            if set(authorization) != expected:
                errors.append("status: 'design_review_authorization' must have exactly the keys "
                              f"{sorted(expected)}")
            if not isinstance(authorization.get("by"), str) or not authorization["by"].strip():
                errors.append("status: 'design_review_authorization.by' must be a non-empty string")
            for sub in ("at", "consumed_at"):
                value = authorization.get(sub)
                if value is None and sub == "consumed_at":
                    continue
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"status: 'design_review_authorization.{sub}' must be a non-empty string"
                                  + (" or null" if sub == "consumed_at" else ""))
                    continue
                try:
                    if datetime.fromisoformat(value).tzinfo is None:
                        errors.append(f"status: 'design_review_authorization.{sub}' must include a timezone")
                except ValueError:
                    errors.append(f"status: 'design_review_authorization.{sub}' must be an ISO-8601 timestamp")
            note = authorization.get("note")
            if note is not None and (not isinstance(note, str) or not note.strip()):
                errors.append("status: 'design_review_authorization.note' must be a non-empty string or null")
            permitted = authorization.get("attempt_permitted")
            if not isinstance(permitted, int) or isinstance(permitted, bool) or permitted < 1:
                errors.append("status: 'design_review_authorization.attempt_permitted' must be a positive integer")
            launch_session_id = authorization.get("launch_session_id")
            if launch_session_id is not None and (
                    not isinstance(launch_session_id, str)
                    or not AGENT_SESSION_ID_PATTERN.fullmatch(launch_session_id)):
                errors.append("status: 'design_review_authorization.launch_session_id' must be an agent "
                              "session id or null")
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
            # #36: optional on a record written before the fields existed.
            if "findings" in design_review:
                errors.extend(_design_review_findings_errors(design_review["findings"], "design_review.findings"))
            if "head" in design_review and design_review["head"] is not None and (
                    not isinstance(design_review["head"], str) or not design_review["head"].strip()):
                errors.append("status: 'design_review.head' must be a non-empty string or null")
            if "attempt" in design_review and (
                    not isinstance(design_review["attempt"], int) or isinstance(design_review["attempt"], bool)
                    or design_review["attempt"] < 1):
                errors.append("status: 'design_review.attempt' must be a positive integer")
            # #37: optional on a record written before the fields existed.
            if "structural_blocker" in design_review and not isinstance(design_review["structural_blocker"], bool):
                errors.append("status: 'design_review.structural_blocker' must be boolean")
            if "reviewer_profile" in design_review and design_review["reviewer_profile"] is not None:
                errors.extend(_design_reviewer_profile_errors(design_review["reviewer_profile"],
                                                              "design_review.reviewer_profile"))
    errors.extend(_design_reviewer_escalation_errors(status.get("design_reviewer_escalation")))
    errors.extend(_design_review_history_errors(status.get("design_review_history")))
    errors.extend(_design_review_packet_errors(status.get("design_review_packet")))
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
            # #39: provenance is optional (records from before it carry
            # none) but, when present, must be exactly adapter/model
            # sources from the closed PROFILE_SOURCES vocabulary.
            source = profile.get("source")
            if source is not None and (
                    not isinstance(source, dict) or set(source) != {"adapter", "model"}
                    or any(value not in PROFILE_SOURCES for value in source.values())):
                errors.append(
                    f"status: 'review.{field}.source' must be an object with adapter and model "
                    f"sources from {', '.join(PROFILE_SOURCES)}"
                )
        if "profiles_distinct" in review and not isinstance(review["profiles_distinct"], bool):
            errors.append("status: 'review.profiles_distinct' must be boolean")
    attempts = status.get("review_attempts")
    if attempts is not None:
        offset = status.get("legacy_review_round_offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            errors.append("status: 'legacy_review_round_offset' must be a non-negative integer")
            offset = 0
        if not isinstance(attempts, list) or len(attempts) > MAX_REVIEW_ATTEMPTS:
            errors.append(f"status: 'review_attempts' must contain at most {MAX_REVIEW_ATTEMPTS} entries")
            attempts = []
        open_count = 0
        ids = set()
        for index, attempt in enumerate(attempts):
            label = f"status: review_attempts[{index}]"
            required = {
                "attempt_id", "attempt", "opened_at", "closed_at", "opened_by", "reviewer",
                "session_ids", "trigger", "trigger_detail", "acceptance_hash", "phase_number",
                "disposition", "findings",
            }
            if not isinstance(attempt, dict) or set(attempt) != required:
                errors.append(f"{label} has invalid fields")
                continue
            aid = attempt.get("attempt_id")
            if not isinstance(aid, str) or not REVIEW_ATTEMPT_ID_PATTERN.fullmatch(aid) or aid in ids:
                errors.append(f"{label}.attempt_id is invalid or duplicated")
            ids.add(aid)
            if attempt.get("attempt") != offset + index + 1:
                errors.append(f"{label}.attempt does not match its monotonic ledger position")
            if attempt.get("trigger") not in REVIEW_ATTEMPT_TRIGGERS:
                errors.append(f"{label}.trigger is invalid")
            disposition = attempt.get("disposition")
            if disposition not in REVIEW_ATTEMPT_DISPOSITIONS:
                errors.append(f"{label}.disposition is invalid")
            if disposition == "open":
                open_count += 1
                if index != len(attempts) - 1 or attempt.get("closed_at") is not None:
                    errors.append(f"{label} open attempt must be last with null closed_at")
            elif attempt.get("closed_at") is None:
                errors.append(f"{label} closed attempt requires closed_at")
            for key in ("opened_at", "closed_at"):
                value = attempt.get(key)
                if value is None and key == "closed_at":
                    continue
                try:
                    parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
                    if parsed is None or parsed.tzinfo is None:
                        raise ValueError
                except ValueError:
                    errors.append(f"{label}.{key} must be a timezone-aware timestamp or null")
            try:
                validate_agent_actor(attempt.get("opened_by"))
                if attempt.get("reviewer") is not None:
                    validate_agent_actor(attempt.get("reviewer"))
            except HandsoffError as exc:
                errors.append(f"{label}: {exc}")
            session_ids = attempt.get("session_ids")
            if not isinstance(session_ids, list) or len(session_ids) > MAX_REVIEW_SESSION_IDS \
                    or len(session_ids) != len(set(session_ids)) \
                    or any(not isinstance(sid, str) or not AGENT_SESSION_ID_PATTERN.fullmatch(sid)
                           for sid in session_ids):
                errors.append(f"{label}.session_ids is invalid")
            if not isinstance(attempt.get("trigger_detail"), str) or len(attempt["trigger_detail"]) > 512:
                errors.append(f"{label}.trigger_detail is invalid")
            if not isinstance(attempt.get("acceptance_hash"), str) \
                    or not re.fullmatch(r"[0-9a-f]{64}", attempt["acceptance_hash"]):
                errors.append(f"{label}.acceptance_hash is invalid")
            if attempt.get("phase_number") not in PHASES:
                errors.append(f"{label}.phase_number is invalid")
            findings_value = attempt.get("findings")
            if not isinstance(findings_value, list) or len(findings_value) > 16 \
                    or any(not isinstance(item, dict) or set(item) != {"code", "summary"}
                           or item.get("code") not in REVIEW_FINDING_CODES
                           or not isinstance(item.get("summary"), str) or not item["summary"].strip()
                           or len(item["summary"]) > 512 for item in findings_value):
                errors.append(f"{label}.findings is invalid")
        if open_count > 1:
            errors.append("status: at most one review attempt may be open")
        if isinstance(status.get("review_round"), int) \
                and status.get("review_round") != offset + len(attempts):
            errors.append("status: review_round must equal legacy offset plus review_attempts length")
    overrides = status.get("review_cap_overrides")
    if overrides is not None:
        if not isinstance(overrides, list) or len(overrides) > MAX_REVIEW_CAP_OVERRIDES:
            errors.append(f"status: 'review_cap_overrides' must contain at most {MAX_REVIEW_CAP_OVERRIDES} entries")
        else:
            seen_overrides = set()
            for item in overrides:
                oid = item.get("override_id") if isinstance(item, dict) else None
                required = {"override_id", "by", "at", "reason", "config_hash", "review_round_at_grant"}
                if not isinstance(item, dict) or set(item) != required \
                        or not isinstance(oid, str) or not REVIEW_OVERRIDE_ID_PATTERN.fullmatch(oid) \
                        or oid in seen_overrides:
                    errors.append("status: review cap override is invalid")
                    continue
                seen_overrides.add(oid)
                if not all(isinstance(item.get(k), str) and item[k].strip()
                           for k in ("by", "at", "reason", "config_hash")) \
                        or not isinstance(item.get("review_round_at_grant"), int):
                    errors.append("status: review cap override fields are invalid")
    escalation = status.get("escalation")
    if escalation is not None:
        required = {"kind", "at", "reason", "required_action", "source"}
        if not isinstance(escalation, dict) or set(escalation) != required \
                or escalation.get("kind") not in ESCALATION_KINDS \
                or not all(isinstance(escalation.get(k), str) and escalation[k].strip()
                           for k in required - {"kind"}):
            errors.append("status: 'escalation' is invalid")
    recovery_attempts = status.get("recovery_attempts")
    if recovery_attempts is not None:
        if not isinstance(recovery_attempts, list) or len(recovery_attempts) > MAX_RECOVERY_ATTEMPTS:
            errors.append(f"status: 'recovery_attempts' must contain at most {MAX_RECOVERY_ATTEMPTS} entries")
            recovery_attempts = []
        recovery_ids = set()
        for index, item in enumerate(recovery_attempts):
            required = {"recovery_id", "role", "trigger", "from_session_id", "to_session_id",
                        "attempt", "cap", "holder", "state", "reason", "at", "launched_at", "ended_at"}
            rid = item.get("recovery_id") if isinstance(item, dict) else None
            if not isinstance(item, dict) or set(item) != required \
                    or not isinstance(rid, str) or not RECOVERY_ID_PATTERN.fullmatch(rid) \
                    or rid in recovery_ids or item.get("role") not in SELECTABLE_AGENT_ROLES \
                    or item.get("trigger") not in RECOVERY_TRIGGERS \
                    or item.get("state") not in RECOVERY_STATES \
                    or item.get("attempt") != index + 1 \
                    or not isinstance(item.get("cap"), int) \
                    or not all(isinstance(item.get(k), str) and item[k].strip()
                               for k in ("holder", "reason", "at")):
                errors.append(f"status: recovery_attempts[{index}] is invalid")
                continue
            recovery_ids.add(rid)
            if item.get("state") in {"reserved", "launched"} and index != len(recovery_attempts) - 1:
                errors.append("status: only the last recovery attempt may be live")
    lease = status.get("recovery_lease")
    if lease is not None:
        required = {"lease_id", "holder", "acquired_at", "expires_at", "recovery_id"}
        if not isinstance(lease, dict) or set(lease) != required \
                or not isinstance(lease.get("lease_id"), str) \
                or not RECOVERY_LEASE_ID_PATTERN.fullmatch(lease["lease_id"]) \
                or not all(isinstance(lease.get(k), str) and lease[k].strip()
                           for k in ("holder", "acquired_at", "expires_at", "recovery_id")):
            errors.append("status: 'recovery_lease' is invalid")
        elif not isinstance(recovery_attempts, list) or not recovery_attempts \
                or recovery_attempts[-1].get("recovery_id") != lease.get("recovery_id") \
                or recovery_attempts[-1].get("state") not in {"reserved", "launched"}:
            errors.append("status: recovery_lease must reference the live final recovery attempt")
    regression_requests = status.get("regression_requests")
    if regression_requests is not None:
        if not isinstance(regression_requests, list) or len(regression_requests) > MAX_REGRESSION_REQUESTS:
            errors.append(f"status: 'regression_requests' must contain at most {MAX_REGRESSION_REQUESTS} entries")
            regression_requests = []
        seen_request_ids = set()
        live_requests = 0
        for index, item in enumerate(regression_requests):
            required = {
                "request_id", "group", "commands", "command_sha256", "state", "requested_by",
                "requested_at", "expires_at", "decided_by", "decided_at", "launched_at",
                "completed_at", "launch_nonce_sha256", "repository", "acceptance_hash",
                "config_hash", "scope_hash", "run_id", "epoch_sha256", "reason",
                "requester_session_id", "results",
            }
            label = f"status: regression_requests[{index}]"
            rid = item.get("request_id") if isinstance(item, dict) else None
            if not isinstance(item, dict) or set(item) != required:
                errors.append(f"{label} has invalid fields")
                continue
            if not isinstance(rid, str) or not REGRESSION_REQUEST_ID_PATTERN.fullmatch(rid) or rid in seen_request_ids:
                errors.append(f"{label}.request_id is invalid or duplicated")
            seen_request_ids.add(rid)
            if item.get("state") not in REGRESSION_STATES:
                errors.append(f"{label}.state is invalid")
            if item.get("state") in {"awaiting_approval", "accepted", "launched"}:
                live_requests += 1
                if index != len(regression_requests) - 1:
                    errors.append(f"{label} live request must be last")
            if not isinstance(item.get("commands"), list) or not item["commands"] \
                    or not all(isinstance(cmd, str) and cmd.strip() for cmd in item["commands"]):
                errors.append(f"{label}.commands is invalid")
            for key in ("command_sha256", "acceptance_hash", "config_hash", "scope_hash", "epoch_sha256"):
                if not isinstance(item.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]):
                    errors.append(f"{label}.{key} is invalid")
            nonce = item.get("launch_nonce_sha256")
            if nonce is not None and (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce)):
                errors.append(f"{label}.launch_nonce_sha256 is invalid")
            if item.get("state") in {"launched", "completed", "failed"} and nonce is None:
                errors.append(f"{label} launched/terminal execution requires a nonce digest")
            if not isinstance(item.get("repository"), dict):
                errors.append(f"{label}.repository is invalid")
            if not isinstance(item.get("run_id"), str) or not item["run_id"].strip() \
                    or not isinstance(item.get("reason"), str) or not item["reason"].strip():
                errors.append(f"{label} identity fields are invalid")
            requester_session = item.get("requester_session_id")
            if requester_session is not None and (not isinstance(requester_session, str)
                                                   or not AGENT_SESSION_ID_PATTERN.fullmatch(requester_session)):
                errors.append(f"{label}.requester_session_id is invalid")
            if not isinstance(item.get("results"), list):
                errors.append(f"{label}.results must be an array")
        if live_requests > 1:
            errors.append("status: at most one regression request may be live")
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
            missing = AGENT_SESSION_FIELDS - AGENT_SESSION_OPTIONAL_FIELDS - set(session)
            if missing:
                errors.append(f"{label} is missing fields: {', '.join(sorted(missing))}")
            unexpected = set(session) - AGENT_SESSION_FIELDS
            if unexpected:
                errors.append(f"{label} contains unsupported fields: {', '.join(sorted(unexpected))}")
            for optional_field in sorted(AGENT_SESSION_OPTIONAL_FIELDS):
                value = session.get(optional_field)
                if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 64):
                    errors.append(f"{label}.{optional_field} must be null or a non-empty string")
                elif optional_field == "tier" and value is not None and value not in DESIGN_REVIEWER_TIERS:
                    errors.append(f"{label}.tier must be null or one of {', '.join(DESIGN_REVIEWER_TIERS)}")
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
    failures = status.get("agent_failures")
    if failures is not None:
        if not isinstance(failures, dict) or len(failures) > MAX_AGENT_SESSIONS:
            errors.append("status: 'agent_failures' must be an object with at most 64 entries")
        else:
            for session_id, failure in failures.items():
                if not isinstance(sessions, dict) or session_id not in sessions:
                    errors.append(f"status: agent failure {session_id!r} has no session")
                    continue
                try:
                    normalized = _validate_failure_classification({
                        key: failure.get(key) for key in ("category", "reason", "tail_sha256")
                    }) if isinstance(failure, dict) else None
                except HandsoffError as exc:
                    errors.append(f"status: agent failure {session_id!r}: {exc}")
                    normalized = None
                if not isinstance(failure, dict) or set(failure) != {
                    "session_id", "category", "reason", "tail_sha256", "at",
                } or failure.get("session_id") != session_id:
                    errors.append(f"status: agent failure {session_id!r} has invalid fields")
                if normalized and sessions[session_id].get("state") not in \
                        AGENT_SESSION_TERMINAL_STATES - {"completed"}:
                    errors.append(f"status: agent failure {session_id!r} is not terminal")
    findings = status.get("agent_quality_findings")
    if findings is not None:
        if not isinstance(findings, list) or len(findings) > MAX_QUALITY_FINDINGS:
            errors.append("status: 'agent_quality_findings' must contain at most 32 entries")
        else:
            for finding in findings:
                required = {"finding_id", "session_id", "role", "code", "count", "limit",
                            "review_round", "distinct_round_count", "eligible", "at"}
                if not isinstance(finding, dict) or set(finding) != required \
                        or not QUALITY_FINDING_ID_PATTERN.fullmatch(str(finding.get("finding_id", ""))) \
                        or finding.get("code") not in QUALITY_FINDING_CODES \
                        or not isinstance(finding.get("eligible"), bool) \
                        or not isinstance(finding.get("review_round"), int) \
                        or isinstance(finding.get("review_round"), bool) \
                        or finding.get("review_round", -1) < 0 \
                        or not isinstance(finding.get("distinct_round_count"), int) \
                        or isinstance(finding.get("distinct_round_count"), bool) \
                        or finding.get("distinct_round_count", -1) < 0:
                    errors.append("status: agent quality finding is invalid")
    bindings = status.get("reviewer_implementer_bindings")
    if bindings is not None:
        if not isinstance(bindings, dict) or len(bindings) > MAX_AGENT_SESSIONS:
            errors.append("status: 'reviewer_implementer_bindings' must have at most 64 entries")
        else:
            for session_id, binding in bindings.items():
                required = {"reviewer_session_id", "implementer_session_id", "adapter", "model", "bound_at"}
                reviewer = sessions.get(session_id) if isinstance(sessions, dict) else None
                if not isinstance(binding, dict) or set(binding) != required \
                        or binding.get("reviewer_session_id") != session_id \
                        or not isinstance(reviewer, dict) or reviewer.get("role") != "reviewer" \
                        or binding.get("adapter") not in SELECTABLE_AGENT_ADAPTERS:
                    errors.append(f"status: reviewer implementer binding {session_id!r} is invalid")
                    continue
                try:
                    validate_agent_model(binding.get("model"))
                except HandsoffError as exc:
                    errors.append(f"status: reviewer implementer binding {session_id!r}: {exc}")
                implementer_id = binding.get("implementer_session_id")
                if implementer_id is not None and (not isinstance(implementer_id, str)
                                                     or not AGENT_SESSION_ID_PATTERN.fullmatch(implementer_id)):
                    errors.append(f"status: reviewer implementer binding {session_id!r} has invalid implementer id")
    replacements = status.get("agent_replacements")
    if replacements is not None:
        if not isinstance(replacements, list) or len(replacements) > MAX_AGENT_REPLACEMENTS:
            errors.append("status: 'agent_replacements' must contain at most 32 entries")
        else:
            for record in replacements:
                if not isinstance(record, dict) or not REPLACEMENT_ID_PATTERN.fullmatch(
                        str(record.get("replacement_id", ""))) \
                        or record.get("trigger") not in AGENT_REPLACEMENT_TRIGGERS \
                        or record.get("action") not in {"launch", "pilot_pause"} \
                        or record.get("category") not in FAILURE_CATEGORIES \
                        or record.get("state") not in AGENT_REPLACEMENT_STATES:
                    errors.append("status: agent replacement record is invalid")
                elif record.get("action") == "launch":
                    handoff = record.get("handoff")
                    if not isinstance(handoff, dict) or handoff.get("state") != record.get("state"):
                        errors.append("status: agent replacement handoff state is invalid")
                elif record.get("state") != "pilot_pause" or record.get("handoff") is not None:
                    errors.append("status: paused agent replacement is invalid")
    return errors


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

GOVERNANCE_CONFIG_KEYS = (
    "deployment_requires_explicit_approval", "require_live_verification",
    "max_design_rounds", "max_review_rounds", "stall_minutes",
    "max_autonomous_design_reviews",
)
# Governance keys added after runs were already in flight. A key in this
# set is hashed only while it holds a non-default value: an absent (or
# explicitly default) key must reproduce the pre-existing hash byte for
# byte, or upgrading bin/ would invalidate every design review, design
# approval, and deployment approval already recorded on every project
# running Handsoff (this repo's own run included). Changing the key to
# anything else still invalidates the decisions bound to the old value,
# which is the whole point of the chain of trust.
_LEGACY_OPTIONAL_GOVERNANCE_KEYS = {
    "max_autonomous_design_reviews": DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
}


def config_hash(cfg: dict) -> str:
    """Binds a review, a deployment approval, or a live verification to the
    governance policy in force when it was recorded. Without this, someone
    could flip deployment_requires_explicit_approval or
    require_live_verification off AFTER a review, silently downgrading what
    the workflow requires without invalidating anything already granted."""
    bound = {}
    for key in GOVERNANCE_CONFIG_KEYS:
        if key in _LEGACY_OPTIONAL_GOVERNANCE_KEYS:
            default = _LEGACY_OPTIONAL_GOVERNANCE_KEYS[key]
            if cfg.get(key, default) == default:
                continue
        bound[key] = cfg.get(key)
    return hashlib.sha256(_canonical(bound).encode("utf-8")).hexdigest()


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
    if "work_items" in acceptance and review.get("scope_hash") != work_item_scope_hash(acceptance["work_items"]):
        errors.append("review gate: work-item scope changed since review; record a new review")
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
    if "work_items" in acceptance and approval.get("scope_hash") != work_item_scope_hash(acceptance["work_items"]):
        errors.append("design gate: work-item scope changed since design approval; record a new approval")
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
    if "work_items" in acceptance and review.get("scope_hash") != work_item_scope_hash(acceptance["work_items"]):
        errors.append("design review gate: work-item scope changed since design review; record a new design review")
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


def design_review_budget(status: dict, cfg: dict) -> dict:
    """#35: the one rule for both kinds of design-review attempt.

    `design_review_attempts` counts every record-design-review ever made
    on the run (approve or request-changes, cumulative, never decremented;
    `design_round` is a separate, human-driven number and was the gap the
    original symptom slipped through). Once `attempts >= limit` the run is
    `exhausted` unless the Pilot has recorded a `design_review_authorization`
    that no record has consumed yet. An authorization permits exactly ONE
    attempt, performed either by a managed reviewer session (which reserves
    it under the project lock by writing `launch_session_id`, see
    create_agent_session) or by a human-recorded review (which consumes it
    directly). `launch_reserved` is that reservation: while it is set and
    the authorization is unconsumed, no second managed launch is permitted.
    Legacy status files without the fields read as zero attempts and no
    authorization, which reproduces the pre-#35 behavior below the limit."""
    attempts = int(status.get("design_review_attempts", 0) or 0)
    limit = int(cfg.get("max_autonomous_design_reviews", DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS))
    authorization = status.get("design_review_authorization")
    authorized = isinstance(authorization, dict) and authorization.get("consumed_at") is None
    launch_reserved = authorized and authorization.get("launch_session_id") is not None
    return {
        "attempts": attempts,
        "limit": limit,
        "authorized": authorized,
        "exhausted": attempts >= limit and not authorized,
        "launch_reserved": launch_reserved,
        "next_attempt": attempts + 1,
        "authorization_command": DESIGN_REVIEW_AUTHORIZATION_COMMAND,
    }


def design_review_budget_exhausted_message(budget: dict) -> str:
    """The exact sentence a refused record, a refused launch, and a blocked
    run's next_action all carry, so the Pilot reads the same command in
    the CLI, in `status`, and on the Mission Control banner."""
    return (f"design review budget exhausted ({budget['attempts']}/{budget['limit']}); "
            f"Pilot must run {budget['authorization_command']} to permit one more attempt")


def design_review_launch_refusal(budget: dict, status: dict) -> str | None:
    """Why a managed reviewer launch in Phase 2 is refused right now, or
    None when it is permitted. Below the limit no authorization is
    involved; at or past it the launch needs an unconsumed, unreserved
    authorization."""
    if budget["attempts"] < budget["limit"]:
        return None
    if budget["exhausted"]:
        return f"managed reviewer launch refused: {design_review_budget_exhausted_message(budget)}"
    if budget["launch_reserved"]:
        reserved_by = (status.get("design_review_authorization") or {}).get("launch_session_id")
        return (f"managed reviewer launch refused: the authorized design-review attempt "
                f"{budget['next_attempt']} is already reserved by session {reserved_by}; "
                f"record-design-review must consume it before any further launch, after which "
                f"the Pilot may run {budget['authorization_command']} again")
    return None


def select_design_reviewer_tier(cfg: dict, status: dict, acceptance: dict) -> tuple[str, str]:
    """#37: which reviewer tier the NEXT design-review attempt gets, and why.

    Pure over (cfg, status, acceptance): the same inputs always give the
    same answer. Deterministic precedence, first match wins:
      1. attempts == 0                          -> primary, first_review
      2. no follow-up configured                -> primary, no_followup_configured
      3. unconsumed design_reviewer_escalation  -> primary, pilot_escalation
      4. last review flagged structural_blocker -> primary, structural_blocker
      5. criterion id set differs from the last review's criteria_ids
         (any id added or removed; a text-only edit does not count; a
         history entry missing entirely also cannot prove the structure
         unchanged)                             -> primary, criteria_structure_changed
      6. otherwise                              -> followup, delta_check
    """
    if design_review_budget(status, cfg)["attempts"] == 0:
        return "primary", "first_review"
    if followup_reviewer_profile(cfg) is None:
        return "primary", "no_followup_configured"
    escalation = status.get("design_reviewer_escalation")
    if isinstance(escalation, dict) and escalation.get("consumed_at") is None:
        return "primary", "pilot_escalation"
    history = [h for h in (status.get("design_review_history") or []) if isinstance(h, dict)]
    last = history[-1] if history else None
    if last is not None and last.get("structural_blocker") is True:
        return "primary", "structural_blocker"
    criteria = acceptance.get("criteria", []) if isinstance(acceptance, dict) else []
    current_ids = sorted(c.get("id") for c in criteria if isinstance(c, dict) and isinstance(c.get("id"), str))
    previous_ids = sorted(i for i in (last.get("criteria_ids") or []) if isinstance(i, str)) if last else None
    if previous_ids is None or current_ids != previous_ids:
        return "primary", "criteria_structure_changed"
    return "followup", "delta_check"


def design_reviewer_tier_profile(cfg: dict, tier: str, *, which=None, require_available: bool = True) -> dict:
    """The (adapter, model, resolution_source) a tier launches with, before
    any post-selection check. With ``require_available`` false (a record
    made by hand, where nothing launches) an unresolvable "auto" primary
    keeps the configured adapter string instead of raising."""
    if tier == "followup":
        followup = followup_reviewer_profile(cfg)
        if followup is None:
            raise HandsoffError("followup reviewer tier selected but no reviewer_followup profile is configured")
        return {**followup, "resolution_source": "configured"}
    configured_adapter = agent_profiles(cfg)["reviewer"]["adapter"]
    profile = resolved_agent_profiles(cfg, which=which, require_available=require_available)["reviewer"]
    if profile["adapter"] is None:
        profile = {**profile, "adapter": configured_adapter}
    if configured_adapter == AUTO_AGENT_ADAPTER:
        resolution_source = "auto_detected"
    elif profile["source"]["adapter"] == RECOMMENDED_PROFILE_SOURCE:
        resolution_source = "recommended"
    else:
        resolution_source = "configured"
    return {"adapter": profile["adapter"], "model": profile["model"], "resolution_source": resolution_source}


def select_design_reviewer_profile(cfg: dict, status: dict, acceptance: dict, *, which=None) -> dict:
    """#37: the profile the next Phase-2 reviewer launch uses.

    Tier and reason come from select_design_reviewer_tier. Two checks are
    then applied to WHICHEVER tier was selected and never switch it:

    - independence (tiering configured only, so a legacy single-profile
      run behaves exactly as before): the selected adapter/model must
      differ from the architect's and the implementer's, or the clash is
      named and the launch refused;
    - availability: the selected adapter executable must be on PATH. A
      missing follow-up never falls back to primary and a missing primary
      never falls forward to follow-up.

    Returns {"adapter", "model", "tier", "reason", "resolution_source"}.
    ``which`` is injectable for tests; production uses shutil.which.
    """
    lookup = which or shutil.which
    tier, reason = select_design_reviewer_tier(cfg, status, acceptance)
    selected = design_reviewer_tier_profile(cfg, tier, which=lookup)
    adapter, model = selected["adapter"], selected["model"]
    tiering = followup_reviewer_profile(cfg) is not None
    if tiering:
        resolved = resolved_agent_profiles(cfg, which=lookup)
        for role in ("architect", "implementer"):
            other = resolved[role]
            if (other["adapter"], other["model"]) == (adapter, model):
                raise HandsoffError(
                    f"{tier} reviewer profile {adapter}/{model} is the same as the {role} profile; "
                    f"an independent design review needs a different adapter or model for the "
                    f"{tier} reviewer tier"
                )
    if adapter not in SELECTABLE_AGENT_ADAPTERS or not lookup(adapter):
        if tiering:
            raise HandsoffError(
                f"{tier} reviewer profile unavailable: {adapter} is not on PATH; install it, "
                f"set fallback_policy.reviewer, or remove {FOLLOWUP_REVIEWER_KEY}"
            )
        raise HandsoffError(unavailable_adapter_message("reviewer", adapter, model, selected["resolution_source"]))
    return {"adapter": adapter, "model": model, "tier": tier, "reason": reason,
            "resolution_source": selected["resolution_source"]}


def design_reviewer_selection_view(cfg: dict, status: dict, acceptance: dict, *, which=None) -> dict:
    """#37 for `status` and Mission Control: ``current`` is the profile the
    latest recorded design review was made under (null before any review,
    or on a record predating #37); ``next`` is what the next launch would
    select, with ``error`` naming a post-selection refusal (independence or
    availability) instead of raising, so the dashboard still shows the
    tier and reason the Pilot needs to act on."""
    review = status.get("design_review") if isinstance(status, dict) else None
    current = None
    if isinstance(review, dict) and isinstance(review.get("reviewer_profile"), dict):
        profile = review["reviewer_profile"]
        current = {field: profile.get(field) for field in DESIGN_REVIEWER_PROFILE_FIELDS}
    tier, reason = select_design_reviewer_tier(cfg, status, acceptance)
    lookup = which or shutil.which
    try:
        profile = design_reviewer_tier_profile(cfg, tier, which=lookup, require_available=False)
    except HandsoffError as exc:
        return {"current": current,
                "next": {"tier": tier, "reason": reason, "adapter": None, "model": None, "error": str(exc)}}
    error = None
    try:
        select_design_reviewer_profile(cfg, status, acceptance, which=lookup)
    except HandsoffError as exc:
        error = str(exc)
    return {"current": current,
            "next": {"tier": tier, "reason": reason, "adapter": profile["adapter"],
                     "model": profile["model"], "error": error}}


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
    if "work_items" in acceptance and (phase >= 8 or status.get("status") == "complete"):
        unfinished = [item for item in derive_work_items(status, acceptance, cfg)["items"]
                      if item.get("required") and item.get("status") != "done"]
        for item in unfinished:
            errors.append(f"work items gate: required work item {item['id']} is {item['status']}; a run cannot complete while it is unfinished")

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
    max_review = effective_review_cap(status, cfg) if "review_attempts" in status else int(cfg.get("max_review_rounds", 3))
    if design_round > max_design:
        errors.append(f"round cap: design_round {design_round} exceeds max_design_rounds {max_design}, escalate to the user")
    if review_round > max_review:
        errors.append(f"round cap: review_round {review_round} exceeds effective max_review_rounds {max_review}, escalate to the user")
    escalation = status.get("escalation")
    if escalation is not None and status.get("status") != "blocked":
        errors.append(
            f"escalation gate: run is escalated ({escalation.get('kind')}); status must stay blocked "
            f"until the escalation is cleared by {escalation.get('required_action')}"
        )
    if current_review_attempt(status) is not None and status.get("status") == "complete":
        errors.append("review attempt gate: an open review attempt exists but status is complete")

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


def assigned_role(status: dict) -> str | None:
    if status.get("status") == "complete":
        return None
    phase = int(status.get("phase_number", 1) or 1)
    if phase == 1:
        return "architect"
    if phase == 2:
        review = status.get("design_review") or {}
        return "architect" if review.get("decision") == "changes_requested" else "reviewer"
    return {3: "supervisor", 4: "implementer", 5: "reviewer", 6: "implementer",
            7: "supervisor", 8: "supervisor"}.get(phase)


def session_liveness_path(root: Path) -> Path:
    return root / ".handsoff-session-liveness.json"


def read_session_liveness(root: Path) -> dict:
    path = session_liveness_path(root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def update_session_liveness(root: Path, session_id: str, *, at: str | None = None,
                            remove: bool = False) -> None:
    if not AGENT_SESSION_ID_PATTERN.fullmatch(str(session_id)):
        raise HandsoffError("agent session id is invalid")
    root = root.resolve()
    with project_lock(root):
        mapping = read_session_liveness(root)
        if remove:
            mapping.pop(session_id, None)
        else:
            mapping[session_id] = at or datetime.now(timezone.utc).isoformat()
        path = session_liveness_path(root)
        text = json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n"
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def _latest_event_kind(events: list[dict], kinds: set[str]) -> str | None:
    for event in reversed(events):
        if event.get("kind") in kinds:
            return event.get("kind")
    return None


def recovery_assessment(status: dict, cfg: dict, liveness: dict | None = None,
                        events: list[dict] | None = None,
                        now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    recovery = cfg.get("recovery") or DEFAULT_CONFIG["recovery"]
    role = assigned_role(status)
    result = {"state": "not_applicable", "reason": "disabled", "assigned_role": role,
              "silent_minutes": None, "threshold_minutes": None, "lost_session_id": None}
    if not recovery.get("enabled", True):
        return result
    open_regression = next((item for item in status.get("regression_requests", [])
                            if item.get("state") in {"awaiting_approval", "accepted", "launched"}), None)
    exclusions = [
        (status.get("status") in {"complete", "blocked", "awaiting_approval", "ready_to_deploy"}, "run_state"),
        (int(status.get("phase_number", 1) or 1) == 8, "complete"),
        (status.get("escalation") is not None, "escalated"),
        (status.get("authorization_hold") is not None, "authorization_hold"),
        (open_regression is not None, "regression_pending"),
        (any(item.get("state") in {"reserved", "launched"}
             for item in status.get("recovery_attempts", [])), "recovery_in_progress"),
        (_latest_event_kind(events or [], {"human_pause_started", "human_pause_ended"}) ==
         "human_pause_started", "human_pause"),
        (_latest_event_kind(events or [], {"background_wait_started", "background_wait_ended"}) ==
         "background_wait_started", "background_wait"),
    ]
    for applies, reason in exclusions:
        if applies:
            result["reason"] = reason
            return result
    if role is None:
        result["reason"] = "no_assigned_role"
        return result
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    session_id = current.get(role)
    session = sessions.get(session_id) if isinstance(session_id, str) else None
    if not isinstance(session, dict):
        timestamps = [status.get("updated_at"), status.get("last_heartbeat_at")]
        ages = [_minutes_since(value, now) for value in timestamps]
        ages = [age for age in ages if age is not None]
        silent = min(ages) if ages else float("inf")
        threshold = float(cfg.get("stall_minutes", 10))
        result.update(silent_minutes=silent, threshold_minutes=threshold)
        if silent >= threshold:
            result.update(state="silent_run", reason="assigned role has no session")
        else:
            result.update(state="active", reason="recent unassigned-run activity")
        return result
    state = session.get("state")
    result["lost_session_id"] = session_id
    if state in AGENT_SESSION_TERMINAL_STATES:
        silent = _minutes_since(session.get("ended_at"), now)
        threshold = float(recovery["worker_loss_grace_minutes"])
        result.update(silent_minutes=silent, threshold_minutes=threshold)
        if silent is not None and silent >= threshold:
            result.update(state="worker_terminal", reason="assigned session is terminal")
        else:
            result.update(state="active", reason="terminal grace period")
        return result
    ping = (liveness or {}).get(session_id) or session.get("running_at") or session.get("started_at")
    silent = _minutes_since(ping, now)
    threshold = float(recovery["live_session_silence_minutes"])
    result.update(silent_minutes=silent, threshold_minutes=threshold)
    if silent is not None and silent >= threshold:
        result.update(state="worker_silent", reason="assigned session liveness expired")
    else:
        result.update(state="active", reason="assigned session is live")
    return result


def _expire_recovery_lease(status: dict, now: datetime) -> bool:
    lease = status.get("recovery_lease")
    if not isinstance(lease, dict):
        return False
    try:
        expired = datetime.fromisoformat(lease["expires_at"]) <= now
    except (KeyError, TypeError, ValueError):
        return False
    if not expired:
        return False
    attempts = status.get("recovery_attempts") or []
    match = next((item for item in attempts if item.get("recovery_id") == lease.get("recovery_id")), None)
    if match and match.get("state") in {"reserved", "launched"}:
        match["state"] = "failed"
        match["reason"] = "lease_expired"
        match["ended_at"] = now.isoformat()
    status["recovery_lease"] = None
    return True


def recover_run(root: Path, *, actor: str, launcher, now: datetime | None = None,
                id_factory=None) -> dict:
    """Perform one lease-protected recovery episode. `launcher(role)` is
    injected so tests can stay process-free; production passes the managed
    Handsoff agent launcher."""
    root = root.resolve()
    actor = validate_agent_actor(actor)
    now = now or datetime.now(timezone.utc)
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        ensure_no_launched_regression(status)
        _assert_agent_telemetry_integrity(root, cfg, status)
        if _expire_recovery_lease(status, now):
            commit(root, cfg, status=status, event_kind="recovery_lease_expired",
                   event_message="Expired recovery lease was closed fail-safe", by=actor)
            status = load_unique_json(status_path(root, cfg))
        assessment = recovery_assessment(
            status, cfg, read_session_liveness(root), read_events(root, cfg), now,
        )
        if assessment["state"] in {"not_applicable", "active"}:
            return {"action": "skipped", "assessment": assessment}
        attempts = list(status.get("recovery_attempts") or [])
        cap = cfg["recovery"]["max_attempts"]
        if len(attempts) >= cap:
            proposed = deepcopy(status)
            proposed["status"] = "blocked"
            proposed["escalation"] = {
                "kind": "recovery_exhausted", "at": now.isoformat(),
                "reason": f"automatic recovery exhausted ({len(attempts)} of {cap})",
                "required_action": "Run recovery-acknowledge --by OPERATOR --reason TEXT",
                "source": attempts[-1]["recovery_id"] if attempts else "recovery-ledger",
            }
            proposed["next_action"] = proposed["escalation"]["required_action"]
            commit(root, cfg, status=proposed, event_kind="recovery_escalated",
                   event_message=proposed["escalation"]["reason"], by=actor)
            return {"action": "escalated", "assessment": assessment}
        role = assessment["assigned_role"]
        if role == "reviewer" and current_review_attempt(status) is None:
            migrated = deepcopy(status)
            migrate_review_ledger(migrated)
            if int(migrated.get("review_round", 0)) >= effective_review_cap(migrated, cfg):
                _review_cap_escalation(migrated, cfg)
                migrated["escalation"]["kind"] = "recovery_paused"
                migrated["escalation"]["reason"] = "review_cap_reached"
                commit(root, cfg, status=migrated, event_kind="recovery_escalated",
                       event_message="Recovery paused at review cap", by=actor)
                return {"action": "escalated", "assessment": assessment}
        rid = _new_bounded_id(
            "hv", RECOVERY_ID_PATTERN,
            {item.get("recovery_id") for item in attempts}, id_factory,
        )
        lid = _new_bounded_id("hl", RECOVERY_LEASE_ID_PATTERN, set(), id_factory)
        from datetime import timedelta
        proposed = deepcopy(status)
        if assessment["state"] == "worker_silent" and assessment.get("lost_session_id"):
            sid = assessment["lost_session_id"]
            session = (proposed.get("agent_sessions") or {}).get(sid)
            if isinstance(session, dict) and session.get("state") in AGENT_SESSION_LIVE_STATES:
                session["state"] = "failed"
                session["ended_at"] = now.isoformat()
                session["exit_code"] = -1
                proposed.setdefault("agent_failures", {})[sid] = {
                    "session_id": sid, "category": "presumed_lost",
                    "reason": _FAILURE_REASON_LABELS["presumed_lost"],
                    "tail_sha256": hashlib.sha256(b"").hexdigest(), "at": now.isoformat(),
                }
        record = {
            "recovery_id": rid, "role": role, "trigger": assessment["state"],
            "from_session_id": assessment.get("lost_session_id"), "to_session_id": None,
            "attempt": len(attempts) + 1, "cap": cap, "holder": actor,
            "state": "reserved", "reason": assessment["reason"], "at": now.isoformat(),
            "launched_at": None, "ended_at": None,
        }
        proposed["recovery_attempts"] = [*attempts, record]
        proposed["recovery_lease"] = {
            "lease_id": lid, "holder": actor, "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=cfg["recovery"]["lease_minutes"])).isoformat(),
            "recovery_id": rid,
        }
        commit(root, cfg, status=proposed, event_kind="recovery_attempt_started",
               event_message=f"Recovery attempt {record['attempt']} reserved for {role}",
               by=actor, recovery_id=rid, role=role, attempt=record["attempt"], cap=cap)
    ok = False
    launch_error = None
    try:
        outcome = launcher(role)
        ok = outcome in (0, True, None)
    except Exception as exc:  # the terminal state records only type, never untrusted output
        launch_error = type(exc).__name__
    ended = datetime.now(timezone.utc)
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        proposed = deepcopy(status)
        item = next((entry for entry in proposed.get("recovery_attempts", [])
                     if entry.get("recovery_id") == rid), None)
        if not item or item.get("state") not in {"reserved", "launched"}:
            raise HandsoffError("recovery attempt lost its terminal compare-and-set")
        current = (proposed.get("current_agent_sessions") or {}).get(role)
        item["to_session_id"] = current if isinstance(current, str) else None
        item["launched_at"] = item.get("launched_at") or now.isoformat()
        item["ended_at"] = ended.isoformat()
        item["state"] = "recovered" if ok else "failed"
        item["reason"] = "managed role completed" if ok else (launch_error or "managed role failed")
        proposed["recovery_lease"] = None
        commit(root, cfg, status=proposed,
               event_kind="recovery_recovered" if ok else "recovery_failed",
               event_message=f"Recovery attempt {item['attempt']} {item['state']}",
               by=actor, recovery_id=rid, role=role, state=item["state"])
    return {"action": "recovered" if ok else "failed", "assessment": assessment,
            "recovery_id": rid, "attempt": record["attempt"], "cap": cap}


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
    same as if it were missing today, never as an error.

    An open human pause (`human_pause` set by `human-pause-start`) also
    suppresses the warning, for as long as it stays open: nothing is
    running, the run is waiting on a person, and `activity_note` says so.
    It is a declared state, not a heartbeat, so it never expires on its
    own and never masquerades as liveness."""
    now = now or datetime.now(timezone.utc)
    if status.get("status") not in ("in_progress",):
        return None
    if isinstance(status.get("human_pause"), dict):
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
    it.

    An open human pause takes precedence over the heartbeat reading and
    renders as 'waiting on <by> since <n> min ago[: <note>]', the one
    rendering both `status` and the dashboard snapshot show."""
    now = now or datetime.now(timezone.utc)
    if status.get("status") not in ("in_progress",):
        return None
    pause = status.get("human_pause")
    if isinstance(pause, dict):
        since_minutes = _minutes_since(pause.get("since"), now)
        since = f"{since_minutes:.0f} min ago" if since_minutes is not None else "an unknown time"
        note = pause.get("note")
        suffix = f": {note}" if isinstance(note, str) else ""
        return f"waiting on {pause.get('by')} since {since}{suffix}"
    updated_minutes = _minutes_since(status.get("updated_at"), now)
    heartbeat_minutes = _minutes_since(status.get("last_heartbeat_at"), now)
    if updated_minutes is None or heartbeat_minutes is None:
        return None
    limit = float(cfg.get("stall_minutes", 10))
    if updated_minutes > limit and heartbeat_minutes <= limit:
        return (f"background task active (heartbeat {heartbeat_minutes:.0f} min ago); "
                f"no status update in {updated_minutes:.0f} minutes, but the run is alive")
    return None


def normalized_test_footprint(command: str, root: Path) -> frozenset[str]:
    """Return the repository-relative tests a command can execute.

    This is intentionally conservative: a command that names a tests directory,
    wildcard, discovery mode, or an unrecognised test runner is treated as broad.
    Handsoff only needs to distinguish configured focused checks from configured
    regression groups; it is not a general shell parser.
    """
    if not isinstance(command, str) or not command.strip():
        raise HandsoffError("test command must be a non-empty string")
    # Commands use a shell so configured test-path globs continue to work.
    # Refuse every construct that can manufacture a different command after
    # validation; the allowed language is simple argv plus path globs.
    if re.search(r"[\$`;&|<>(){}\r\n]", command):
        raise HandsoffError("test commands may not contain shell expansion or control operators")
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise HandsoffError(f"invalid test command: {exc}") from exc
    if any(token in {"|", "||", "&&", ";", ">", ">>", "<"} for token in words):
        raise HandsoffError("test commands may not contain shell control operators")
    lowered = [Path(word).name.lower() for word in words]
    footprint: set[str] = set()
    for word in words:
        candidate = word.split("::", 1)[0]
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate.startswith("tests.") and "/" not in candidate:
            candidate = candidate.replace(".", "/") + ".py"
        if not (candidate.startswith("tests/") or candidate == "tests"):
            continue
        if candidate == "tests" or candidate.endswith("/"):
            return frozenset({"*"})
        if any(ch in candidate for ch in "*?["):
            matches = sorted(root.glob(candidate))
            if not matches:
                return frozenset({"*"})
            footprint.update(path.resolve().relative_to(root.resolve()).as_posix() for path in matches if path.is_file())
        else:
            footprint.add(Path(candidate).as_posix())
    if "unittest" in lowered and "discover" in lowered:
        return frozenset({"*"})
    if not footprint:
        return frozenset({"*"})
    return frozenset(footprint)


def regression_group(cfg: dict, name: str) -> dict:
    item = next((entry for entry in cfg.get("regressions", []) if entry.get("name") == name), None)
    if item is None:
        raise HandsoffError(f"unknown regression group: {name}")
    return item


def configured_regression_commands(cfg: dict) -> set[str]:
    return {command for group in cfg.get("regressions", []) for command in group.get("commands", [])}


def command_sha256(commands: list[str]) -> str:
    return hashlib.sha256(json.dumps(commands, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def ensure_regression_config_is_disjoint(cfg: dict, root: Path) -> None:
    regression_footprints = []
    for group in cfg.get("regressions", []):
        footprint = set().union(*(normalized_test_footprint(cmd, root) for cmd in group["commands"]))
        regression_footprints.append((group["name"], footprint))
    for command in cfg.get("check_commands", []):
        focused = normalized_test_footprint(command, root)
        for name, regression in regression_footprints:
            captures_group = "*" in focused or ("*" not in regression and regression <= set(focused))
            if captures_group:
                raise HandsoffError(
                    f"focused check overlaps gated regression group {name}: {command}"
                )


def active_regression_request(status: dict) -> dict | None:
    requests = status.get("regression_requests") or []
    return next((item for item in reversed(requests)
                 if item.get("state") in {"awaiting_approval", "accepted", "launched"}), None)


def ensure_no_launched_regression(status: dict) -> None:
    item = active_regression_request(status)
    if item and item.get("state") == "launched":
        raise HandsoffError(
            f"regression {item.get('request_id')} is running; workflow mutations are locked until it terminalizes"
        )


def _work_item_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:40].rstrip("-")
    return slug or "item"


def criterion_work_item_id(criterion: dict) -> str | None:
    requirement = criterion.get("requirement") if isinstance(criterion, dict) else None
    match = WORK_ITEM_TAG_PATTERN.match(requirement or "")
    if not match:
        return None
    tag = match.group(1)
    return f"issue-{tag[1:]}" if tag.startswith("#") else f"ask-{tag}"


def derive_work_item_registry(acceptance: dict, cfg: dict, *, now: str | None = None,
                              explicit_items: list[str] | None = None) -> list[dict]:
    """Derive stable scope from criterion tags, feature issue refs and legacy display metadata."""
    now = now or datetime.now(timezone.utc).isoformat()
    tickets = {int(item["number"]): item for item in cfg.get("tickets", [])}
    identities: dict[str, tuple[str, int | None, str]] = {}
    feature = str(acceptance.get("feature") or "")

    def add_text(text: str) -> None:
        issue = re.fullmatch(r"\s*#([1-9][0-9]{0,8})(?:\s+(.+?))?\s*", text)
        if issue:
            number = int(issue.group(1))
            supplied = (issue.group(2) or "").strip()
            title = supplied or tickets.get(number, {}).get("title") or f"Issue #{number}"
            identities[f"issue-{number}"] = ("issue", number, title)
            return
        slug = _work_item_slug(text)
        identities.setdefault(f"ask-{slug}", ("ask", None, text.strip()[:200]))

    if explicit_items:
        for item in explicit_items[:MAX_WORK_ITEMS]:
            add_text(item)
    else:
        # Explicit separators are promises. A segment containing issue refs
        # contributes those issues; a segment without one remains a plain ask.
        parts = [part.strip(" -\t") for part in
                 re.split(r"[;\n]+|(?:^|\s)\d+[.)]\s+", feature)
                 if part.strip(" -\t")]
        for part in parts[:MAX_WORK_ITEMS]:
            numbers = re.findall(r"(?<!\w)#([1-9][0-9]{0,8})\b", part)
            if numbers:
                for number_text in numbers:
                    number = int(number_text)
                    identities[f"issue-{number}"] = (
                        "issue", number,
                        tickets.get(number, {}).get("title") or f"Issue #{number}",
                    )
            else:
                add_text(part)
    for criterion in acceptance.get("criteria", []):
        item_id = criterion_work_item_id(criterion)
        if not item_id:
            continue
        if item_id.startswith("issue-"):
            number = int(item_id[6:])
            title = tickets.get(number, {}).get("title") or f"Issue #{number}"
            identities[item_id] = ("issue", number, title)
        else:
            title = item_id[4:].replace("-", " ").title()
            identities[item_id] = ("ask", None, title)
    items = []
    for item_id, (kind, number, title) in identities.items():
        ticket = tickets.get(number, {}) if number is not None else {}
        items.append({
            "id": item_id, "kind": kind, "number": number, "title": title[:200],
            "url": str(ticket.get("url") or ""), "required": True,
            "github_state": None, "github_checked_at": None,
            "created_at": now, "updated_at": now, "notes": "",
        })
    return sorted(items, key=lambda item: (item["kind"] != "issue", item["number"] or 0, item["id"]))[:MAX_WORK_ITEMS]


def effective_work_items(acceptance: dict, cfg: dict) -> tuple[list[dict], str]:
    persisted = acceptance.get("work_items")
    if isinstance(persisted, list):
        return persisted, "persisted"
    return derive_work_item_registry(acceptance, cfg), "derived"


def work_item_scope_hash(items: list[dict]) -> str:
    scope = sorted(({
        "id": item.get("id"), "kind": item.get("kind"),
        "number": item.get("number"), "required": item.get("required", True),
    } for item in items), key=lambda item: item["id"] or "")
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def derive_work_items(status: dict, acceptance: dict, cfg: dict) -> dict:
    registry, source = effective_work_items(acceptance, cfg)
    criteria = acceptance.get("criteria", [])
    multi = len(registry) > 1
    mapping: dict[str, list[dict]] = {item["id"]: [] for item in registry}
    for criterion in criteria:
        item_id = criterion_work_item_id(criterion)
        if item_id is None and len(registry) == 1:
            item_id = registry[0]["id"]
        if item_id in mapping:
            mapping[item_id].append(criterion)
        elif multi:
            mapping.setdefault("unattributed", []).append(criterion)
    rows = list(registry)
    if "unattributed" in mapping and not any(item["id"] == "unattributed" for item in rows):
        timestamp = status.get("updated_at") or datetime.now(timezone.utc).isoformat()
        rows.append({"id": "unattributed", "kind": "ask", "number": None,
                     "title": "Unattributed acceptance criteria", "url": "", "required": True,
                     "github_state": None, "github_checked_at": None,
                     "created_at": timestamp, "updated_at": timestamp, "notes": ""})
    regression = active_regression_request(status)
    recovering = any(item.get("state") in {"reserved", "launched"}
                     for item in status.get("recovery_attempts", []))
    reviewing = bool(current_review_attempt(status)) or int(status.get("phase_number", 1) or 1) == 5
    escalation = status.get("escalation") if isinstance(status.get("escalation"), dict) else None
    rendered = []
    for item in rows:
        own = mapping.get(item["id"], [])
        blocker = ""
        if escalation or status.get("status") == "blocked":
            state = "blocked"
            blocker = (escalation or {}).get("reason") or status.get("next_action", "Run blocked")
        elif regression and regression.get("state") in {"awaiting_approval", "accepted"}:
            state = "awaiting_approval"
            blocker = f"Regression {regression.get('group')} awaits Pilot action"
        elif recovering:
            state = "recovering"
            blocker = "Assigned worker recovery is active"
        elif reviewing:
            state = "in_review"
        elif own and all(c.get("state") == "passing" for c in own):
            state = "done"
        elif any(c.get("state") == "blocked" for c in own):
            state = "blocked"
            blocker = "A mapped acceptance criterion is blocked"
        elif status.get("active_work_item") == item["id"] or any(c.get("evidence") for c in own):
            state = "in_progress"
        else:
            state = "not_started"
        github_state = item.get("github_state")
        discrepancy = None
        if github_state == "closed" and state != "done":
            discrepancy = "GitHub is closed but Handsoff work is unfinished"
        elif github_state == "open" and state == "done":
            discrepancy = "Handsoff is done but GitHub is still open"
        rendered.append({**item, "status": state, "criteria": [c.get("id") for c in own],
                         "phase_or_next": status.get("next_action") or status.get("phase"),
                         "blocker": blocker, "discrepancy": discrepancy})
    counts = {state: sum(item["status"] == state for item in rendered) for state in WORK_ITEM_STATES}
    return {"multi": len(rendered) > 1, "registry": source,
            "tickets_config_deprecated": bool(cfg.get("tickets")) and source == "persisted",
            "items": rendered, "aggregate": {"total": len(rendered), "done": counts["done"], "counts": counts}}


# --------------------------------------------------------------------------
# #33: live session status. A managed child beacons `.handsoff-live.json`
# every few seconds; `live_status` folds that liveness signal into the
# structured state (run status, human pause, the ledger-bound session
# record) into one small view the dashboard strip and `status` show.
# --------------------------------------------------------------------------

def live_beacon_path(root: Path) -> Path:
    return Path(root) / LIVE_BEACON_FILE


def _seconds_since(timestamp: str | None, now: datetime) -> float | None:
    minutes = _minutes_since(timestamp, now)
    return None if minutes is None else minutes * 60


def write_live_beacon(root: Path, *, session_id: str, role: str, state: str,
                      pid: int | None, ended_at: str | None = None,
                      exit_code: int | None = None, now: datetime | None = None) -> bool:
    """Best-effort atomic write of the seven-key beacon. Returns False
    instead of raising on any OSError: the beacon is a liveness hint, never
    the authority, so a full disk or a bad path must not touch the child's
    lifecycle or the session record."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(pid, int) or isinstance(pid, bool):
        pid = None
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    beacon = {
        "session_id": session_id, "role": role, "state": state, "pid": pid,
        "beacon_at": now.isoformat(), "ended_at": ended_at, "exit_code": exit_code,
    }
    try:
        _atomic_write_text(live_beacon_path(root), json.dumps(beacon, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def read_live_beacon(root: Path) -> dict | None:
    """The beacon if it exists and is well-formed (exactly the seven keys,
    identifiers, integers, and timestamps only); anything else reads as no
    signal, never as an error."""
    path = live_beacon_path(root)
    try:
        beacon = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(beacon, dict) or set(beacon) != set(LIVE_BEACON_KEYS):
        return None
    for key in ("session_id", "role", "state", "beacon_at"):
        if not isinstance(beacon[key], str) or not beacon[key]:
            return None
    for key in ("pid", "exit_code"):
        if beacon[key] is not None and (not isinstance(beacon[key], int) or isinstance(beacon[key], bool)):
            return None
    if beacon["ended_at"] is not None and not isinstance(beacon["ended_at"], str):
        return None
    if _minutes_since(beacon["beacon_at"], datetime.now(timezone.utc)) is None:
        return None
    return beacon


def _live_focus_session(status: dict) -> dict | None:
    """The one session the live view is about: a live (launching/running)
    current session first, else the most recently ended terminal one."""
    sessions = [s for s in current_agent_sessions(status).values() if isinstance(s, dict)]
    live = [s for s in sessions if s.get("state") in AGENT_SESSION_LIVE_STATES]
    if live:
        return max(live, key=lambda s: str(s.get("running_at") or s.get("started_at") or ""))
    terminal = [s for s in sessions if s.get("state") in AGENT_SESSION_TERMINAL_STATES]
    if terminal:
        return max(terminal, key=lambda s: str(s.get("ended_at") or s.get("started_at") or ""))
    return None


def live_status(status: dict, cfg: dict, root: Path, *, now: datetime | None = None) -> dict:
    """Derive one of LIVE_STATES from structured state plus the beacon.

    Precedence: a complete run; a run waiting on a person (blocked, open
    human pause, Phase 7 awaiting deployment approval); the current live
    session (a beacon counts only when its session_id is the current
    session's; fresh means at most LIVE_BEACON_FRESH_SECONDS old); the
    current terminal session, reported from the ledger-bound record
    (`ended_at`, `exit_code`) with the beacon informing `process_signal`
    only; else idle. `last_activity_at` is the freshest of updated_at,
    last_heartbeat_at, the matching beacon, and the session timestamps."""
    now = now or datetime.now(timezone.utc)
    beacon = read_live_beacon(root)
    session = _live_focus_session(status)
    session_id = session.get("session_id") if session else None
    role = session.get("role") if session else None
    matching = beacon if (beacon and session_id and beacon["session_id"] == session_id) else None
    beacon_age = _seconds_since(matching["beacon_at"], now) if matching else None
    if matching is None:
        process_signal = "none"
    elif beacon_age is not None and 0 <= beacon_age <= LIVE_BEACON_FRESH_SECONDS:
        process_signal = "fresh"
    else:
        process_signal = "stale"

    candidates = [status.get("updated_at"), status.get("last_heartbeat_at")]
    if matching:
        candidates.append(matching["beacon_at"])
    if session:
        candidates.extend(session.get(key) for key in ("started_at", "running_at", "ended_at"))
    stamped = [(c, _seconds_since(c, now)) for c in candidates if isinstance(c, str)]
    stamped = [(c, age) for c, age in stamped if age is not None]
    last_activity_at = min(stamped, key=lambda item: item[1])[0] if stamped else None
    seconds_since_activity = int(round(min(age for _, age in stamped))) if stamped else None

    view = {
        "state": "idle", "role": role, "session_id": session_id,
        "last_activity_at": last_activity_at, "seconds_since_activity": seconds_since_activity,
        "process_signal": process_signal, "detail": "no managed process is running",
        "ended_at": None, "exit_code": None,
    }
    session_state = session.get("state") if session else None
    phase = int(status.get("phase_number", 1) or 1)
    awaiting_deployment = (
        cfg.get("deployment_requires_explicit_approval", True)
        and phase == 7 and not status.get("deployment_approved")
        and status.get("status") == "in_progress"
    )
    if status.get("status") == "complete":
        view["state"] = "complete"
        view["detail"] = "run complete"
    elif (status.get("status") == "blocked" or isinstance(status.get("human_pause"), dict)
          or awaiting_deployment):
        view["state"] = "waiting"
        view["detail"] = (activity_note(status, cfg, now=now)
                          or status.get("next_action")
                          or ("awaiting deployment approval" if awaiting_deployment else "waiting on a decision"))
    elif session_state in AGENT_SESSION_LIVE_STATES:
        mismatch = (f"beacon belongs to session {beacon['session_id']}, not current session {session_id}"
                    if beacon and matching is None else None)
        if process_signal == "fresh":
            view["state"] = "started" if session_state == "launching" else "running"
            view["detail"] = f"{role} session {session_state}"
            if matching.get("pid") is not None:
                view["detail"] += f" (pid {matching['pid']})"
        elif session_state == "launching":
            view["state"] = "started"
            view["detail"] = mismatch or f"{role} session launching, no process signal yet"
        else:
            signal_at = matching["beacon_at"] if matching else (session.get("running_at") or session.get("started_at"))
            silent = _seconds_since(signal_at, now)
            silent_text = f"{int(round(silent))} s" if silent is not None else "an unknown time"
            view["state"] = "stalled"
            view["detail"] = f"no process signal for {silent_text}" + (f" ({mismatch})" if mismatch else "")
    elif session_state in AGENT_SESSION_TERMINAL_STATES:
        exit_code = session.get("exit_code")
        view["state"] = "stopped" if session_state in ("completed", "cancelled") else "failed"
        view["ended_at"] = session.get("ended_at")
        view["exit_code"] = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        exit_text = f"exit {view['exit_code']}" if view["exit_code"] is not None else "no exit code"
        view["detail"] = (f"{role} session {session_state.replace('_', ' ')} ({exit_text})"
                          f" at {view['ended_at'] or 'an unknown time'}")
    return view


# --------------------------------------------------------------------------
# check execution: close the loop between a claimed state and reality
# --------------------------------------------------------------------------

def run_checks(cfg: dict, root: Path, commands: list[str] | None = None,
               timeout: int | None = None, *, allow_regression: bool = False) -> list[dict]:
    """Actually execute the given commands (default: [checks].commands), in
    the project root. Each result is real evidence a criterion's evidence
    list can reference, not a sentence someone typed. Timeout comes from
    handsoff.toml's checks.timeout_seconds (default 600s) unless overridden."""
    import subprocess
    timeout = timeout if timeout is not None else cfg.get("check_timeout_seconds", 600)
    results = []
    selected = list(commands if commands is not None else cfg.get("check_commands", []))
    if not allow_regression:
        regression_commands = configured_regression_commands(cfg)
        for command in selected:
            command_footprint = normalized_test_footprint(command, root)
            for gated in regression_commands:
                gated_footprint = normalized_test_footprint(gated, root)
                captures_group = "*" in command_footprint \
                    or ("*" not in gated_footprint and gated_footprint <= set(command_footprint))
                if captures_group:
                    raise HandsoffError("full regression blocked: create and accept a Mission Control regression request")
    for cmd in selected:
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
# #38: cached, hash-bound design evidence. A configured measurement command
# (an AST inventory, a dependency graph, a schema dump) runs once and its
# bounded output is reused for as long as its declared inputs and its own
# command line are unchanged. The store is a side file, not a ledger: the
# event log carries only ids and hashes, never output, so the secret-safety
# boundary of the chained ledgers is untouched.
# --------------------------------------------------------------------------

def design_evidence_path(root: Path) -> Path:
    return root / DESIGN_EVIDENCE_FILE


def load_design_evidence(root: Path) -> dict:
    """The store, `{"artifacts": {id: record}}`; a missing file is empty and
    a malformed one is refused rather than half-trusted."""
    path = design_evidence_path(root)
    if not path.exists():
        return {"artifacts": {}}
    data = load_unique_json(path)
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, dict) or not all(
        isinstance(record, dict) and record.get("id") == artifact_id
        and all(field in record for field in DESIGN_EVIDENCE_RECORD_FIELDS)
        for artifact_id, record in artifacts.items()
    ):
        raise HandsoffError(f"{path.name}: malformed design evidence store; delete it and rerun design-evidence run")
    return {"artifacts": artifacts}


def _design_evidence_matches(root: Path, inputs: list[str]) -> list[Path]:
    """Regular files matched by the declared globs, deduplicated, sorted by
    root-relative path, and confined to the resolved project root so a
    symlink cannot pull an outside file into the hash."""
    resolved_root = root.resolve()
    matches: dict[str, Path] = {}
    for pattern in inputs:
        for candidate in root.glob(pattern):
            if not candidate.is_file():
                continue
            try:
                relative = candidate.resolve().relative_to(resolved_root)
            except ValueError:
                continue
            matches[relative.as_posix()] = candidate
    return [matches[key] for key in sorted(matches)]


def design_evidence_input_hash(root: Path, inputs: list[str]) -> tuple[str, int]:
    """sha256 over the sorted (relative path, file sha256) pairs the globs
    match right now, plus how many files that was. A glob that matches
    nothing still contributes to the identity (see design_evidence_identity)
    but not to this hash."""
    resolved_root = root.resolve()
    pairs = []
    for path in _design_evidence_matches(root, inputs):
        relative = path.resolve().relative_to(resolved_root).as_posix()
        pairs.append([relative, hashlib.sha256(path.read_bytes()).hexdigest()])
    return hashlib.sha256(_canonical({"files": pairs}).encode("utf-8")).hexdigest(), len(pairs)


def design_evidence_identity(command: str, inputs: list[str]) -> str:
    """The cache key: sha256(command + newline + canonical JSON of the
    declared inputs list). Changing the command OR the glob list changes
    the identity even when the matched file set would hash the same."""
    return hashlib.sha256(
        (command + "\n" + json.dumps(list(inputs), sort_keys=True, separators=(",", ":"))).encode("utf-8")
    ).hexdigest()


def _design_evidence_repository(root: Path) -> dict:
    """Exact commit identity when the root is a git checkout; nulls when it
    is not, so a plain directory can still cache evidence."""
    try:
        snapshot = repository_snapshot(root)
    except HandsoffError:
        return {"head": None, "branch": None, "dirty": None}
    return {"head": snapshot["head"], "branch": snapshot["branch"], "dirty": snapshot["dirty"]}


def _bounded_output(output: str) -> tuple[str, str, int, bool]:
    """Keep at most MAX_DESIGN_EVIDENCE_OUTPUT_BYTES of UTF-8 in the store;
    the sha256 and byte count always describe the full output."""
    raw = output.encode("utf-8", "replace")
    digest = hashlib.sha256(raw).hexdigest()
    truncated = len(raw) > MAX_DESIGN_EVIDENCE_OUTPUT_BYTES
    kept = raw[:MAX_DESIGN_EVIDENCE_OUTPUT_BYTES].decode("utf-8", "ignore") if truncated else output
    return kept, digest, len(raw), truncated


def _execute_design_evidence(entry: dict, root: Path, timeout: int, runner) -> tuple[int, str]:
    """Run one measurement the way run_checks runs a check: shell, project
    root, the configured check timeout, exit 124 on a timeout."""
    try:
        proc = runner(entry["command"], shell=True, cwd=str(root), capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout + stderr + f"\nHANDSOFF: command timed out after {timeout} seconds"


def _design_evidence_entries(cfg: dict, ids: list[str] | None) -> list[dict]:
    entries = cfg.get("design_evidence") or []
    if ids is None:
        return list(entries)
    known = {entry["id"]: entry for entry in entries}
    unknown = [artifact_id for artifact_id in ids if artifact_id not in known]
    if unknown:
        raise HandsoffError(f"unknown design_evidence ids: {', '.join(unknown)}")
    seen: list[str] = []
    for artifact_id in ids:
        if artifact_id not in seen:
            seen.append(artifact_id)
    return [known[artifact_id] for artifact_id in seen]


def run_design_evidence(root: Path, cfg: dict, *, ids: list[str] | None = None, by: str,
                        force: bool = False, runner=subprocess.run) -> list[dict]:
    """Run (or reuse) the configured measurements. A stored record is reused,
    with no command executed and no event written, when its identity and
    input hash still match and it exited 0; `force` is the reviewer's
    challenge path and always reruns. Every executed measurement is stored
    under the project lock and logged as `design_evidence_recorded` with
    hashes and metadata only. Returns one record per requested artifact,
    each with a transient `reused` flag that is not persisted."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("--by must be a non-empty string")
    entries = _design_evidence_entries(cfg, ids)
    if not entries:
        return []
    timeout = cfg.get("check_timeout_seconds", DEFAULT_CONFIG["check_timeout_seconds"])
    stored = load_design_evidence(root)["artifacts"]
    results: list[dict] = []
    executed: list[dict] = []
    for entry in entries:
        identity = design_evidence_identity(entry["command"], entry["inputs"])
        input_hash, matched = design_evidence_input_hash(root, entry["inputs"])
        previous = stored.get(entry["id"])
        if (previous is not None and not force and previous.get("identity_sha256") == identity
                and previous.get("input_hash") == input_hash and previous.get("exit_code") == 0):
            results.append({**previous, "reused": True})
            continue
        exit_code, output = _execute_design_evidence(entry, root, timeout, runner)
        kept, digest, size, truncated = _bounded_output(output)
        record = {
            "id": entry["id"], "command": entry["command"],
            "command_sha256": hashlib.sha256(entry["command"].encode("utf-8")).hexdigest(),
            "identity_sha256": identity, "inputs": list(entry["inputs"]),
            "input_hash": input_hash, "matched_files": matched,
            "exit_code": exit_code, "output": kept, "output_sha256": digest, "output_bytes": size,
            "truncated": truncated, "at": datetime.now(timezone.utc).isoformat(), "by": by.strip(),
            **_design_evidence_repository(root),
        }
        executed.append(record)
        results.append({**record, "reused": False})
    if executed:
        with project_lock(root):
            store = load_design_evidence(root)
            for record in executed:
                store["artifacts"][record["id"]] = record
            atomic_write_json(design_evidence_path(root), store)
            events = [{
                "kind": "design_evidence_recorded",
                "message": f"Recorded design evidence {record['id']}",
                "artifact_id": record["id"], "input_hash": record["input_hash"],
                "output_sha256": record["output_sha256"], "exit_code": record["exit_code"],
                "truncated": record["truncated"], "head": record["head"],
            } for record in executed]
            last = events.pop()
            commit(root, cfg, event_kind=last["kind"], event_message=last["message"], extra_events=events,
                   **{k: v for k, v in last.items() if k not in ("kind", "message")})
    return results


def design_evidence_view(root: Path, cfg: dict) -> list[dict]:
    """One entry per configured artifact with its state (current, stale,
    failed, missing) and the hashes behind that judgement. Never includes
    output: this is what the dashboard snapshot, `design-evidence show`,
    and the #36 packet consume. `commit_matches_head` is informational; an
    unrelated commit does not make an artifact stale, but the exact commit
    it was measured at is always visible."""
    stored = load_design_evidence(root)["artifacts"]
    current_head = _design_evidence_repository(root)["head"]
    view = []
    for entry in cfg.get("design_evidence") or []:
        identity = design_evidence_identity(entry["command"], entry["inputs"])
        input_hash, matched = design_evidence_input_hash(root, entry["inputs"])
        record = stored.get(entry["id"])
        reasons: list[str] = []
        if record is None:
            state = "missing"
            reasons.append("never run")
        else:
            if record.get("identity_sha256") != identity:
                if record.get("command") != entry["command"]:
                    reasons.append("command changed")
                if list(record.get("inputs") or []) != list(entry["inputs"]):
                    reasons.append("declared inputs changed")
                if not reasons:
                    reasons.append("cache identity changed")
            if record.get("input_hash") != input_hash:
                reasons.append("input files changed")
            if reasons:
                state = "stale"
            elif record.get("exit_code") != 0:
                state = "failed"
                reasons.append(f"exit code {record.get('exit_code')}")
            else:
                state = "current"
        if matched == 0:
            reasons.append("declared inputs match no files")
        view.append({
            "id": entry["id"], "state": state, "reasons": reasons,
            "input_hash": input_hash, "matched_files": matched,
            "output_sha256": record.get("output_sha256") if record else None,
            "at": record.get("at") if record else None,
            "by": record.get("by") if record else None,
            "head": record.get("head") if record else None,
            "commit_matches_head": bool(record and record.get("head") and record.get("head") == current_head),
            "truncated": bool(record.get("truncated")) if record else False,
            "exit_code": record.get("exit_code") if record else None,
        })
    return view


def design_evidence_prompt_section(root: Path, cfg: dict) -> str:
    """The `# Design evidence` block appended to the Architect's and the
    Reviewer's role input. Only `current` artifacts show their bounded
    output; stale, failed, and missing ones are listed by state alone so
    they are never presented as current truth. Empty when nothing is
    configured, so a project without [[design_evidence]] sees no change."""
    view = design_evidence_view(root, cfg)
    if not view:
        return ""
    stored = load_design_evidence(root)["artifacts"]
    lines = [
        "# Design evidence",
        "",
        "Cached output of the configured [[design_evidence]] measurement commands. Only artifacts marked "
        "`current` show output; a stale, failed, or missing artifact must be refreshed with "
        "`handsoff_supervisor.py design-evidence run --by <you>` (add `--force` to challenge a cached "
        "result) before it is relied on.",
    ]
    for item in view:
        reasons = f" ({'; '.join(item['reasons'])})" if item["reasons"] else ""
        lines.extend(["", f"## {item['id']}: {item['state']}{reasons}"])
        if item["state"] != "current":
            continue
        record = stored[item["id"]]
        head_note = "matches HEAD" if item["commit_matches_head"] else "HEAD has moved since"
        lines.append(f"command: {record['command']}")
        lines.append(f"measured at {record['at']} by {record['by']} on commit {record['head'] or 'n/a'} "
                     f"({head_note}); input_hash {record['input_hash'][:16]}; "
                     f"{'truncated to' if record['truncated'] else 'complete,'} "
                     f"{min(record['output_bytes'], MAX_DESIGN_EVIDENCE_OUTPUT_BYTES)} of {record['output_bytes']} bytes")
        lines.extend(["", "```", record["output"].rstrip("\n"), "```"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# #36 delta review packets: after the first full design review, a follow-up
# reviewer receives the sorted, canonical, size-capped delta (criteria delta,
# dispositioned prior findings, evidence states, repository identity) in
# front of its role prompt instead of re-deriving the whole design.
# --------------------------------------------------------------------------

def validate_design_review_findings(values: object, attempt: int) -> list[dict]:
    """Bounded `--finding` texts become `{"id": "F<attempt>.<n>", "text"}`."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise HandsoffError("design review findings must be a list of strings")
    if len(values) > MAX_DESIGN_REVIEW_FINDINGS:
        raise HandsoffError(f"at most {MAX_DESIGN_REVIEW_FINDINGS} --finding entries are accepted per review")
    findings = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"--finding {index} must be a non-empty string")
        text = value.strip()
        if len(text) > MAX_DESIGN_REVIEW_FINDING_LENGTH:
            raise HandsoffError(f"--finding {index} exceeds {MAX_DESIGN_REVIEW_FINDING_LENGTH} characters")
        findings.append({"id": f"F{attempt}.{index}", "text": text})
    return findings


def design_review_history_entry(review: dict, criteria: list[dict], *, structural_blocker: bool = False) -> dict:
    """The bounded fact a later packet (and #37's tier selection) compares
    against: which criteria existed, their spec hashes, the commit, and the
    findings, at the moment the review was recorded."""
    ordered = sorted(criteria, key=lambda c: c.get("id") or "")
    return {
        "attempt": review["attempt"],
        "decision": review["decision"],
        "by": review["by"],
        "design_hash": review["design_hash"],
        "head": review.get("head"),
        "criteria_ids": sorted(c.get("id") for c in ordered if isinstance(c.get("id"), str)),
        "criterion_hashes": {c["id"]: criterion_spec_hash(c) for c in ordered if isinstance(c.get("id"), str)},
        "structural_blocker": bool(structural_blocker),
        "findings": deepcopy(review.get("findings") or []),
    }


def append_design_review_history(status: dict, entry: dict) -> None:
    history = [h for h in (status.get("design_review_history") or []) if isinstance(h, dict)]
    history.append(entry)
    status["design_review_history"] = history[-MAX_DESIGN_REVIEW_HISTORY:]


def latest_design_review_findings(status: dict) -> list[dict]:
    history = status.get("design_review_history") or []
    if not history or not isinstance(history[-1], dict):
        return []
    return [f for f in (history[-1].get("findings") or []) if isinstance(f, dict)]


def parse_design_review_dispositions(values: object, findings: list[dict]) -> dict:
    """`ID=resolved|rejected|unresolved[:note]` per entry, validated against
    the most recent review's findings. Every refusal names the offending
    value; an omitted finding is filled in by the packet as unresolved."""
    known = {f["id"] for f in findings}
    parsed: dict[str, dict] = {}
    for raw in values or []:
        if not isinstance(raw, str) or "=" not in raw:
            raise HandsoffError(f"--disposition {raw!r} must have the form ID=resolved|rejected|unresolved[:note]")
        finding_id, _, rest = raw.partition("=")
        finding_id = finding_id.strip()
        disposition, _, note = rest.partition(":")
        disposition = disposition.strip()
        if finding_id not in known:
            raise HandsoffError(f"--disposition names unknown finding {finding_id!r}; known: "
                                f"{', '.join(sorted(known)) or 'none'}")
        if finding_id in parsed:
            raise HandsoffError(f"--disposition repeats finding {finding_id}")
        if disposition not in DESIGN_REVIEW_DISPOSITIONS:
            raise HandsoffError(f"--disposition for {finding_id} has invalid value {disposition!r}; "
                                f"expected one of {', '.join(DESIGN_REVIEW_DISPOSITIONS)}")
        note = note.strip()[:MAX_DESIGN_REVIEW_FINDING_LENGTH] or None
        if disposition == "rejected" and note is None:
            raise HandsoffError(f"--disposition {finding_id}=rejected requires a note explaining why")
        parsed[finding_id] = {"disposition": disposition, "note": note}
    return parsed


def _packet_repository(root: Path) -> dict:
    try:
        snapshot = repository_snapshot(root)
    except HandsoffError:
        return {"head": None, "branch": None, "dirty": None}
    return {"head": snapshot["head"], "branch": snapshot["branch"], "dirty": snapshot["dirty"]}


def _files_changed_between(root: Path, previous_head: str, head: str, runner=subprocess.run) -> list[str] | None:
    if not re.fullmatch(r"[0-9a-f]{40,64}", previous_head or "") or not re.fullmatch(r"[0-9a-f]{40,64}", head or ""):
        return None
    try:
        result = runner(
            ["git", "diff", "--name-only", f"{previous_head}..{head}"], cwd=str(root.resolve()),
            shell=False, text=True, capture_output=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _normalized_finding_text(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _packet_size(body: dict, truncated: dict) -> int:
    probe = {**body, "packet_id": "0" * 32}
    if truncated:
        probe["truncated"] = truncated
    return len(_canonical(probe).encode("utf-8"))


def build_design_review_packet(root: Path, cfg: dict, status: dict, acceptance: dict,
                               dispositions: dict | None = None, *, runner=subprocess.run) -> dict:
    """Deterministic (no timestamps, every list sorted) delta packet for the
    next design-review attempt, at most MAX_DESIGN_REVIEW_PACKET_BYTES when
    serialized canonically. Over budget it is trimmed in a fixed order,
    re-measured after each step, and each trim is recorded in `truncated`;
    ids, hashes, attempt numbers, repository identity, and stale_reasons are
    never trimmed. `packet_id` is sha256 of the final trimmed body."""
    root = root.resolve()
    history = [h for h in (status.get("design_review_history") or []) if isinstance(h, dict)]
    if not history:
        raise HandsoffError("no recorded design review to build a delta packet from")
    previous = history[-1]
    attempts = design_review_budget(status, cfg)["attempts"]
    criteria = acceptance.get("criteria", [])
    current_hashes = {c["id"]: criterion_spec_hash(c) for c in criteria if isinstance(c.get("id"), str)}
    previous_hashes = previous.get("criterion_hashes") or {}
    previous_ids = {i for i in (previous.get("criteria_ids") or []) if isinstance(i, str)}
    current_ids = set(current_hashes)
    common = current_ids & previous_ids
    # A criterion whose earlier spec hash is unknown counts as changed:
    # the reviewer verifies more, never less, when the record is thin.
    changed = sorted(i for i in common if previous_hashes.get(i) != current_hashes[i])
    criteria_delta = {
        "added": sorted(current_ids - previous_ids),
        "removed": sorted(previous_ids - current_ids),
        "changed": changed,
        "unchanged": sorted(common - set(changed)),
    }
    dispositions = dispositions or {}
    findings = []
    for finding in sorted(latest_design_review_findings(status), key=lambda f: f.get("id") or ""):
        given = dispositions.get(finding["id"]) or {"disposition": "unresolved", "note": None}
        findings.append({"id": finding["id"], "text": finding["text"],
                         "disposition": given["disposition"], "note": given.get("note")})
    earlier_texts = {
        _normalized_finding_text(f.get("text"))
        for entry in history[:-1] for f in (entry.get("findings") or []) if isinstance(f, dict)
    }
    new_findings_since = sorted(
        f["id"] for f in findings if _normalized_finding_text(f["text"]) not in earlier_texts
    )
    repository = _packet_repository(root)
    previous_head = previous.get("head")
    stale_reasons: list[str] = []
    files_changed: list[str] | None = None
    if previous_head is None:
        stale_reasons.append("previous review recorded no repository head")
    elif repository["head"] is None:
        stale_reasons.append("current repository identity is unavailable")
    elif previous_head != repository["head"]:
        stale_reasons.append(f"HEAD moved from {previous_head[:12]} to {repository['head'][:12]} since the previous review")
        files_changed = _files_changed_between(root, previous_head, repository["head"], runner)
        if files_changed is None:
            stale_reasons.append("files changed since the previous review could not be listed")
    else:
        files_changed = []
    stale = bool(stale_reasons)
    try:
        evidence_view = design_evidence_view(root, cfg)
    except HandsoffError:
        evidence_view = []
    evidence = []
    for item in sorted(evidence_view, key=lambda e: e.get("id") or ""):
        evidence.append({
            "id": item["id"], "state": item["state"], "reasons": sorted(item.get("reasons") or []),
            "input_hash": item.get("input_hash"), "output_sha256": item.get("output_sha256"),
            "head": item.get("head"), "commit_matches_head": bool(item.get("commit_matches_head")),
            "at": item.get("at"), "by": item.get("by"), "truncated": bool(item.get("truncated")),
            "exit_code": item.get("exit_code"),
            "stale_for_packet": stale or item["state"] != "current" or not item.get("commit_matches_head"),
        })
    body = {
        "attempt": attempts + 1,
        "previous_attempt": previous["attempt"],
        "design_hash": design_hash(criteria),
        "previous_design_hash": previous["design_hash"],
        "criteria_delta": criteria_delta,
        "findings": findings,
        "dispositions": {
            name: sorted(f["id"] for f in findings if f["disposition"] == name)
            for name in DESIGN_REVIEW_DISPOSITIONS
        },
        "new_findings_since": new_findings_since,
        "evidence": evidence,
        "repository": repository,
        "previous_head": previous_head,
        "files_changed_since_previous": files_changed,
        "stale": stale,
        "stale_reasons": stale_reasons,
        "instructions": DESIGN_REVIEW_PACKET_INSTRUCTIONS,
    }
    truncated: dict[str, int] = {}
    limit = MAX_DESIGN_REVIEW_PACKET_BYTES

    def fits() -> bool:
        return _packet_size(body, truncated) <= limit

    # (1) files: first 200, then halve until it fits or is empty.
    if files_changed is not None and len(files_changed) > MAX_DESIGN_REVIEW_PACKET_FILES:
        truncated["files_changed_since_previous"] = len(files_changed) - MAX_DESIGN_REVIEW_PACKET_FILES
        files_changed = files_changed[:MAX_DESIGN_REVIEW_PACKET_FILES]
        body["files_changed_since_previous"] = files_changed
    while not fits() and files_changed:
        keep = len(files_changed) // 2
        truncated["files_changed_since_previous"] = (
            truncated.get("files_changed_since_previous", 0) + len(files_changed) - keep)
        files_changed = files_changed[:keep]
        body["files_changed_since_previous"] = files_changed
    # (2) unchanged criteria ids collapse to their count.
    if not fits():
        unchanged = criteria_delta.pop("unchanged")
        criteria_delta["unchanged_count"] = len(unchanged)
        truncated["criteria_delta.unchanged"] = len(unchanged)
    # (3) finding text and note cut to 256 characters.
    if not fits():
        cut = 0
        for finding in findings:
            for field in ("text", "note"):
                value = finding.get(field)
                if isinstance(value, str) and len(value) > DESIGN_REVIEW_PACKET_TRIMMED_TEXT_LENGTH:
                    finding[field] = value[:DESIGN_REVIEW_PACKET_TRIMMED_TEXT_LENGTH]
                    cut += 1
        if cut:
            truncated["findings.text"] = cut
    # (4) evidence reasons dropped, keeping id and state.
    if not fits():
        dropped = 0
        for item in evidence:
            if item.pop("reasons", None):
                dropped += 1
        if dropped:
            truncated["evidence.reasons"] = dropped
    # (5) findings cut from the end.
    while not fits() and findings:
        findings.pop()
        truncated["findings"] = truncated.get("findings", 0) + 1
    if truncated:
        body["truncated"] = truncated
    packet_id = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:32]
    return {"packet_id": packet_id, **body}


def design_review_packet_bytes(packet: dict) -> int:
    return len(_canonical(packet).encode("utf-8"))


def applicable_design_review_packet(status: dict, cfg: dict, criteria: list[dict]) -> dict | None:
    """The stored packet only when it is for the NEXT attempt of the CURRENT
    design in Phase 2; anything else (older attempt, criteria edited since,
    another phase) is ignored so the reviewer gets full context instead."""
    packet = status.get("design_review_packet") if isinstance(status, dict) else None
    if not isinstance(packet, dict) or status.get("phase_number") != 2:
        return None
    if packet.get("attempt") != design_review_budget(status, cfg)["attempts"] + 1:
        return None
    if packet.get("design_hash") != design_hash(criteria):
        return None
    return deepcopy(packet)


def design_review_packet_summary(status: dict) -> dict | None:
    """Counts and flags for the dashboard; never the finding texts."""
    packet = status.get("design_review_packet") if isinstance(status, dict) else None
    if not isinstance(packet, dict):
        return None
    findings = [f for f in (packet.get("findings") or []) if isinstance(f, dict)]
    delta = packet.get("criteria_delta") or {}
    unchanged = delta.get("unchanged")
    return {
        "packet_id": packet.get("packet_id"),
        "attempt": packet.get("attempt"),
        "previous_attempt": packet.get("previous_attempt"),
        "design_hash": packet.get("design_hash"),
        "stale": bool(packet.get("stale")),
        "stale_reasons": list(packet.get("stale_reasons") or []),
        "findings": {
            "total": len(findings),
            **{name: sum(1 for f in findings if f.get("disposition") == name)
               for name in DESIGN_REVIEW_DISPOSITIONS},
        },
        "new_findings": len(packet.get("new_findings_since") or []),
        "criteria_delta": {
            "added": len(delta.get("added") or []),
            "removed": len(delta.get("removed") or []),
            "changed": len(delta.get("changed") or []),
            "unchanged": len(unchanged) if isinstance(unchanged, list) else int(delta.get("unchanged_count") or 0),
        },
        "evidence": len(packet.get("evidence") or []),
        "files_changed": (len(packet["files_changed_since_previous"])
                          if isinstance(packet.get("files_changed_since_previous"), list) else None),
        "truncated": bool(packet.get("truncated")),
        "bytes": design_review_packet_bytes(packet),
    }


# --------------------------------------------------------------------------
# runtime failure classification: lets the Supervisor eventually distinguish
# a launched agent that is working from one that failed, exhausted quota or
# context, or was cancelled/timed out -- WITHOUT wiring into the launcher
# yet (see handsoff_agent.py; that wiring is deferred to a follow-up issue).
# Deliberately diverges from run_checks() above: that function keeps a raw
# output_tail because check-command output is trusted, first-party text.
# Third-party agent stderr/stdout is not -- it can be credential-bearing --
# so nothing here ever returns raw text, only a digest and a label drawn
# from a fixed, closed set.
# --------------------------------------------------------------------------

FAILURE_CATEGORIES = (
    "cancelled", "timeout", "auth_failure", "rate_limit", "context_exhaustion",
    "process_crash", "non_zero_exit", "unknown", "still_running", "presumed_lost",
)

_FAILURE_REASON_LABELS = {
    "cancelled": "run was cancelled",
    "timeout": "runner exceeded its timeout",
    "auth_failure": "authentication or authorization failed",
    "rate_limit": "rate limit or quota exhausted",
    "context_exhaustion": "context window exhausted",
    "process_crash": "process was terminated by a signal",
    "non_zero_exit": "process exited with a non-zero status",
    "unknown": "failure signal matched no known category",
    "still_running": "no failure signal reported yet",
    "presumed_lost": "host watchdog found no liveness signal past the threshold",
}

# Order matters: tier 3 checks these in sequence and the first match wins,
# even when a tail matches more than one (e.g. an auth error surfaced while
# refreshing a rate-limited token) -- there is no such thing as an
# ambiguous double-classification, only a fixed tie-break.
_TAIL_PATTERNS = (
    ("auth_failure", re.compile(r"unauthorized|authentication failed|invalid api key|401", re.IGNORECASE)),
    ("rate_limit", re.compile(r"rate limit|too many requests|quota exceeded|429", re.IGNORECASE)),
    ("context_exhaustion", re.compile(r"context length exceeded|context window|maximum context|prompt is too long", re.IGNORECASE)),
)


def classify_runtime_failure(*, exit_code: int | None = None, timed_out: bool = False,
                              cancelled: bool = False, stderr_tail: str = "",
                              stdout_tail: str = "") -> dict:
    """Turn a launched agent's raw outcome into one of FAILURE_CATEGORIES.
    Never returns the scanned text itself, only a category, a fixed-set
    reason label, and a digest of it -- the tail can be credential-bearing
    third-party output, unlike run_checks()'s trusted check-command output.

    Five-tier precedence, first match wins: cancelled, then timed_out (both
    override exit_code/tail entirely), then a known tail pattern (checked in
    a fixed order so a tail matching more than one is never ambiguous), then
    the exit code's sign (POSIX: a negative/signal-terminated code is a
    crash, any other nonzero code is a plain non-zero exit), then unknown
    (exit_code absent, tail present but unrecognized) or still_running
    (exit_code absent, tail empty -- nothing has happened yet). exit_code is
    never 0 here: a clean exit is not a failure signal in the first place.
    """
    tail = (stderr_tail or "") + (stdout_tail or "")
    tail_sha256 = hashlib.sha256(tail.encode("utf-8", "replace")).hexdigest()
    if cancelled:
        category = "cancelled"
    elif timed_out:
        category = "timeout"
    else:
        category = None
        for name, pattern in _TAIL_PATTERNS:
            if pattern.search(tail):
                category = name
                break
        if category is None:
            if exit_code is not None:
                category = "process_crash" if exit_code < 0 else "non_zero_exit"
            elif tail.strip():
                category = "unknown"
            else:
                category = "still_running"
    return {
        "category": category,
        "reason": _FAILURE_REASON_LABELS[category],
        "tail_sha256": tail_sha256,
    }


def should_failover_for_quality(*, retry_count: int, retry_limit: int, finding_id: str | None) -> bool:
    """Bounds quality-based failover so one subjective judgment can never
    trigger it: both a retry count at or past the configured limit AND a
    non-empty (post-strip) recorded reviewer/Supervisor finding id are
    required. Neither alone is sufficient -- an exhausted retry count with
    no recorded finding, or a finding with retries still available, both
    refuse."""
    has_finding = isinstance(finding_id, str) and finding_id.strip() != ""
    return retry_count >= retry_limit and has_finding


RECOVERABLE_FAILURE_CATEGORIES = {
    "auth_failure", "rate_limit", "context_exhaustion", "timeout",
    "process_crash", "non_zero_exit", "presumed_lost",
}
FALLBACK_SKIP_REASONS = {
    "invalid_profile", "adapter_unavailable", "already_attempted", "reviewer_not_independent",
}


def _fallback_decision(action: str, reason: str, *, profile: dict | None = None,
                       skipped: list[dict] | None = None) -> dict:
    return {"action": action, "reason": reason, "profile": profile, "skipped": skipped or []}


def _canonical_implementer_identity(profile: object) -> tuple[str, str] | None:
    """Accept immutable runtime-session or implementation-review snapshots."""
    if not isinstance(profile, dict):
        return None
    if "requested_model" in profile:
        adapter = profile.get("resolved_adapter", profile.get("adapter"))
        model = profile.get("requested_model")
    else:
        adapter = profile.get("effective_adapter", profile.get("adapter"))
        model = profile.get("model")
    if adapter not in SELECTABLE_AGENT_ADAPTERS:
        return None
    try:
        model = validate_agent_model(model)
    except HandsoffError:
        return None
    return adapter, model


def plan_agent_fallback(role: str, failure_category: str, fallback_entries: object,
                        adapter_availability: object, attempted_identities: object,
                        failover_count: object, max_failovers_per_role: object,
                        implementer_profile: object = None) -> dict:
    """Purely choose the next eligible fallback; never launch or mutate state."""
    if not isinstance(failure_category, str) or failure_category not in FAILURE_CATEGORIES:
        return _fallback_decision("pilot_pause", "non_recoverable_failure")
    if failure_category == "still_running":
        return _fallback_decision("no_action", "agent_still_running")
    if failure_category not in RECOVERABLE_FAILURE_CATEGORIES:
        return _fallback_decision("pilot_pause", "non_recoverable_failure")

    if role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("fallback planner role is invalid")
    if not isinstance(fallback_entries, list) or len(fallback_entries) > MAX_FALLBACK_PROFILES:
        raise HandsoffError(f"fallback planner entries must be an array of at most {MAX_FALLBACK_PROFILES}")
    if not isinstance(adapter_availability, dict) or set(adapter_availability) != set(SELECTABLE_AGENT_ADAPTERS) \
            or not all(isinstance(value, bool) for value in adapter_availability.values()):
        raise HandsoffError("fallback planner availability must map codex and claude to booleans")
    if not isinstance(attempted_identities, (list, tuple)):
        raise HandsoffError("fallback planner attempted identities must be a sequence")
    attempted = set()
    for identity in attempted_identities:
        if not isinstance(identity, (list, tuple)) or len(identity) != 2 \
                or identity[0] not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError("fallback planner attempted identity is invalid")
        try:
            model = validate_agent_model(identity[1])
        except HandsoffError as exc:
            raise HandsoffError("fallback planner attempted identity is invalid") from exc
        attempted.add((identity[0], model))
    cap = validate_max_failovers(max_failovers_per_role)
    if not isinstance(failover_count, int) or isinstance(failover_count, bool) \
            or not 0 <= failover_count <= MAX_FALLBACK_PROFILES:
        raise HandsoffError(f"fallback planner failover_count must be an integer from 0 to {MAX_FALLBACK_PROFILES}")

    implementer_identity = None
    if role == "reviewer":
        implementer_identity = _canonical_implementer_identity(implementer_profile)
        if implementer_identity is None:
            return _fallback_decision("pilot_pause", "missing_independence_reference")
    if failover_count >= cap:
        return _fallback_decision("pilot_pause", "cap_exhausted")

    skipped = []
    for index, raw_profile in enumerate(fallback_entries):
        try:
            profile = validate_fallback_entries([raw_profile], field="fallback planner entry")[0]
        except HandsoffError:
            skipped.append({"index": index, "reason": "invalid_profile"})
            continue
        identity = (profile["adapter"], profile["model"])
        if not adapter_availability[profile["adapter"]]:
            skipped.append({"index": index, "reason": "adapter_unavailable"})
        elif identity in attempted:
            skipped.append({"index": index, "reason": "already_attempted"})
        elif role == "reviewer" and identity == implementer_identity:
            skipped.append({"index": index, "reason": "reviewer_not_independent"})
        else:
            return _fallback_decision("select", "eligible_fallback", profile=profile, skipped=skipped)
    return _fallback_decision("pilot_pause", "fallback_exhausted", skipped=skipped)


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


# --------------------------------------------------------------------------
# #40: run-owned dashboard release
# --------------------------------------------------------------------------
# A dashboard launched with `dashboard --owned-by-run` belongs to one run of
# one root. When that run lands Phase 8 complete, `advance` asks the server
# to stop so the port is free for the next run. Two independent bindings
# decide whether a server may be stopped: the run_token the server minted
# at launch, and the sha256 of the root it serves, which the caller
# computes from its OWN resolved root and never reads from the file. No
# PID is ever signalled; a server that does not answer with both values
# is left alone and only the pointer file is removed.

def dashboard_owner_path(root: Path) -> Path:
    return root / DASHBOARD_OWNER_FILE


def dashboard_root_sha256(root: Path) -> str:
    """The root binding both sides compute independently."""
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def new_dashboard_run_token() -> str:
    return secrets.token_hex(16)


def write_dashboard_owner(root: Path, *, pid: int, host: str, port: int, run_token: str,
                          root_sha256: str, feature: str | None) -> Path:
    """Write the pointer file for a run-owned server. Identifiers, integers,
    and timestamps only; the token is a random handle, not a credential
    for anything beyond stopping this one loopback server."""
    path = dashboard_owner_path(root)
    atomic_write_json(path, {
        "pid": int(pid),
        "host": host,
        "port": int(port),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "owner": DASHBOARD_OWNER,
        "run_token": run_token,
        "root_sha256": root_sha256,
        "feature": feature,
    })
    return path


def remove_dashboard_owner_if_token(root: Path, run_token: str) -> bool:
    """The server's own exit path: remove the pointer file only while it
    still carries this server's token. A file with a different token
    belongs to a newer server on the same root and is left in place.
    Returns True when a file was removed."""
    path = dashboard_owner_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("run_token") != run_token:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _dashboard_base_url(host: str, port: int) -> str:
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def _dashboard_json_request(url: str, *, timeout: float, body: dict | None = None) -> dict:
    """One loopback JSON exchange. Raises OSError for any transport failure
    and ValueError for any answer that is not a JSON object; the caller
    turns either into a "stale metadata" outcome."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            answer = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            answer = {}
        detail = answer.get("error") if isinstance(answer, dict) else None
        raise ValueError(f"HTTP {exc.code}: {detail or 'no detail'}") from exc
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc
    try:
        answer = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("answer is not JSON") from exc
    if not isinstance(answer, dict):
        raise ValueError("answer is not a JSON object")
    return answer


def _dashboard_port_refuses(host: str, port: int, *, wait: float) -> bool:
    """Poll until a fresh TCP connect to the port is refused, up to `wait`
    seconds. Only ECONNREFUSED counts as closed; a connect that succeeds
    (even to a listen backlog nobody accepts from any more) means the
    listening socket is still open."""
    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                pass
        except ConnectionRefusedError:
            return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def release_run_dashboard(root: Path, *, timeout: float = 2.0, wait: float = 5.0) -> dict:
    """Stop the dashboard this root's run owns, if the server behind the
    pointer file proves it is that server.

    1. No pointer file: nothing to do.
    2. The file names a port. `GET /api/ownership` on it must answer
       `owned: true` with the file's run_token AND the root hash this
       caller computes itself from its own resolved root. Only then is
       `POST /api/shutdown` sent (the server re-checks both values), the
       port is polled until it refuses, and the file is removed.
    3. Anything else (connection refused, an unowned server, a foreign
       token, another root, a malformed answer): the metadata is stale
       or the port belongs to someone else. The file is removed and no
       process is touched.
    Never signals a PID. Idempotent: the file is gone after any outcome
    except the no-file case, so a repeated call reports that."""
    path = dashboard_owner_path(root)
    if not path.exists():
        return {"released": False, "reason": "no run-owned dashboard"}

    def stale(which: str) -> dict:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return {"released": False, "reason": f"stale ownership metadata removed: {which}"}

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return stale("owner file is not readable JSON")
    if not isinstance(record, dict):
        return stale("owner file is not a JSON object")
    port = record.get("port")
    token = record.get("run_token")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return stale("owner file has no usable port")
    if not isinstance(token, str) or not token:
        return stale("owner file has no run_token")
    host = record.get("host")
    if host not in DASHBOARD_LOOPBACK_HOSTS:
        host = "127.0.0.1"
    pid = record.get("pid") if isinstance(record.get("pid"), int) else None
    expected_root = dashboard_root_sha256(root)
    base = _dashboard_base_url(host, port)

    try:
        ownership = _dashboard_json_request(f"{base}/api/ownership", timeout=timeout)
    except OSError as exc:
        return stale(f"connection failed ({exc})")
    except ValueError as exc:
        return stale(f"malformed ownership answer ({exc})")
    if ownership.get("owned") is not True:
        return stale("server is not run-owned")
    if ownership.get("run_token") != token:
        return stale("run_token mismatch")
    if ownership.get("root_sha256") != expected_root:
        return stale("root_sha256 mismatch")

    try:
        answer = _dashboard_json_request(f"{base}/api/shutdown", timeout=timeout,
                                         body={"run_token": token, "root_sha256": expected_root})
    except OSError as exc:
        return stale(f"shutdown request failed ({exc})")
    except ValueError as exc:
        return stale(f"shutdown refused ({exc})")
    if answer.get("ok") is not True:
        return stale("shutdown not acknowledged")
    closed = _dashboard_port_refuses(host, port, wait=wait)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if not closed:
        return {"released": False, "pid": pid, "port": port,
                "reason": f"shutdown accepted but port {port} still accepted connections after {wait:g} s"}
    return {"released": True, "pid": pid, "port": port,
            "reason": f"run-owned dashboard on port {port} shut down"}
