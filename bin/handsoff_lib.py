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
import fnmatch
import json
import math
import os
import re
import secrets
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import sysconfig
import webbrowser
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_OUTPUT_TOKEN_PATTERNS = (
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)
_OUTPUT_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)([\"']?[A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTH|COOKIE|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]*[\"']?\s*[=:]\s*[\"']?)[^\s,}\"']+"
)


def redact_output_text(text: str) -> str:
    """Redact credential-shaped values before diagnostic output is persisted.

    Verification failures need a bounded tail for diagnosis, while successful
    checks deliberately remain tail-less because successful output needs no
    diagnosis and may echo secrets. This pure function is shared with the
    portable agent output path so both persistence paths use the same policy.
    """
    try:
        redacted = _OUTPUT_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", text)
        for pattern in _OUTPUT_TOKEN_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    except Exception:
        return "[OUTPUT REDACTION FAILED]"

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
    2: "Debate the design until the reviewer answers DESIGN_APPROVED, then get human design-approve (unless waived by config).",
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
# Hard ceilings for one managed Codex session.  These are deliberately
# conservative: a role that cannot finish inside its allowance must return a
# bounded handoff or stop for the Pilot, never silently consume an unlimited
# rollout.  Projects may lower or raise an individual role under
# [agent_budget], but may not disable the ceiling.
DEFAULT_AGENT_TOKEN_BUDGETS = {
    "architect": 40_000,
    "supervisor": 24_000,
    "implementer": 80_000,
    "reviewer": 80_000,
}
PREFLIGHT_FILE = ".handsoff-preflight.json"
MIN_AGENT_TOKEN_BUDGET = 8_000
#: The pre-flight probe's own rollout ceiling. It used to borrow the 8,000
#: token floor, and inside a real project the reviewer-shaped prompt alone
#: cost 13,008 tokens at prefill weight 1.0, so a working Codex reported
#: `unreachable` (v0.3.25 field-note defect 1). Three times the floor bounds
#: one "Reply with OK" turn without depending on any project's context.
PREFLIGHT_TOKEN_BUDGET = 24_000
MAX_AGENT_TOKEN_BUDGET = 500_000
TICKET_STATES = frozenset({"done", "in_progress", "not_started", "blocked"})
#: #38: cached, hash-bound design evidence. The side file is generated
#: state (gitignored), never a ledger: it holds the bounded output of
#: trusted configured commands, and the event log only ever carries hashes.
DESIGN_EVIDENCE_FILE = "handsoff-design-evidence.json"
DESIGN_EVIDENCE_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
MAX_DESIGN_EVIDENCE_ENTRIES = 16
MAX_DESIGN_EVIDENCE_OUTPUT_BYTES = 8192
# #175: managed-session knowledge briefings are bounded so a project cannot
# turn an indexed file into an unbounded launch prompt.
MAX_BRIEFING_FILE_BYTES = 64 * 1024
MAX_BRIEFING_TOTAL_BYTES = 256 * 1024
BRIEFING_CONFIG_KEYS = frozenset({"index", "root"})
DESIGN_EVIDENCE_STATES = ("current", "stale", "failed", "missing")
#: #33: liveness beacon written by `handsoff_agent.execute_launch` while a
#: managed child runs. Generated state (gitignored), never hashed, never
#: read by any gate: identifiers, integers, and timestamps only. The
#: ledger-bound session record stays the authority on lifecycle; the beacon
#: only says whether the process that owns that session is still signalling.
LIVE_BEACON_FILE = ".handsoff-live.json"
LIVE_INFLIGHT_FILE = ".handsoff-live-inflight.json"
LIVE_BEACON_KEYS = ("session_id", "role", "state", "pid", "beacon_at", "ended_at", "exit_code")
LIVE_BEACON_INTERVAL_SECONDS = 5.0
LIVE_BEACON_FRESH_SECONDS = 15.0
LIVE_STATES = ("idle", "started", "running", "waiting", "stalled", "stopped", "failed", "complete")
#: #41: output liveness. Every stdout/stderr chunk a managed child writes
#: bumps this file (gitignored, never hashed, never logged, never read by
#: a gate): identifiers, one timestamp, and two counters, never content.
#: It is what lets `stall_warning` see a child that is streaming output
#: while the workflow and heartbeat timestamps sit idle. Writes are rate
#: limited to one per second per session; chunks in between only bump the
#: counters. The file only counts while bound to the current session for
#: its role in a live state, so a process exit expires the signal at once.
OUTPUT_LIVENESS_FILE = ".handsoff-output-liveness.json"
OUTPUT_LIVENESS_KEYS = ("session_id", "role", "output_at", "chunks", "bytes")
OUTPUT_LIVENESS_WRITE_INTERVAL_SECONDS = 1.0
# #58: portable, bounded managed-agent output. This generated side file is
# deliberately separate from every audit ledger and is never consulted by a
# gate. It contains only host-redacted child output and session metadata.
AGENT_OUTPUT_FILE = ".handsoff-agent-output.json"
AGENT_OUTPUT_LOCK_FILE = ".handsoff-agent-output.lock"
OPERATION_STATES = ("started", "succeeded", "failed", "timed_out", "cancelled")
OPERATION_ID_PATTERN = re.compile(r"^op-[a-z0-9]{4,32}$")
OPERATION_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
OPERATIONS_FILE = ".handsoff-operations.json"
MAX_AGENT_OUTPUT_SESSIONS = 8
MAX_AGENT_OUTPUT_ENTRIES = 160
MAX_AGENT_OUTPUT_LINE_CHARS = 2048
AGENT_OUTPUT_FLUSH_INTERVAL_SECONDS = 0.25
AGENT_OUTPUT_FLUSH_MAX_ENTRIES = 20
AGENT_OUTPUT_FLUSH_MAX_BYTES = 32768
ACTIVITY_SOURCES = ("workflow", "heartbeat", "output", "beacon", "session", "pause")
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
    "in_progress", "not_started", "unscoped",
}
WORK_ITEM_TAG_PATTERN = re.compile(r"^\[(#\d{1,9}|[a-z0-9][a-z0-9-]{0,39})\]\s")
WORK_ITEM_ID_PATTERN = re.compile(r"^(?:issue-[1-9][0-9]{0,8}|ask-[a-z0-9][a-z0-9-]{0,39}|unattributed)$")
MAX_WORK_ITEMS = 64
WORK_ITEM_LANES = ("full", "small-fix", "escalated")
DEFAULT_SMALL_FIX_MAX_CRITERIA = 3
DEFAULT_SMALL_FIX_MAX_CHANGED_LINES = 200
DEFAULT_SMALL_FIX_MAX_FILES = 6

DEFAULT_CONFIG = {
    "logo": None,
    "status_file": "handsoff-status.json",
    "acceptance_file": "handsoff-acceptance.json",
    "event_log": "handsoff-events.jsonl",
    "verification_log": "handsoff-verifications.jsonl",
    "max_design_rounds": 3,
    "auto_handoff": True,
    "max_review_rounds": 3,
    "stall_minutes": 10,
    "max_autonomous_design_reviews": DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    "small_fix_max_criteria": DEFAULT_SMALL_FIX_MAX_CRITERIA,
    "small_fix_max_changed_lines": DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    "small_fix_max_files": DEFAULT_SMALL_FIX_MAX_FILES,
    "require_live_verification": True,
    "deployment_requires_explicit_approval": True,
    # #159: false lets a project waive the Pilot's design click; the
    # independent design review stays mandatory either way.
    "require_design_approval": True,
    # #165 #167 #166: workflow features, each a switch under [features] that
    # Mission Control's settings dialog edits. Defaults live in FEATURES.
    "features": {},
    "check_commands": [],
    "briefing": None,
    "digest_ignore": [],
    "implementer_commands": [],
    "documentation": {"files": [], "exclude": []},
    # #124: exact browser origins (scheme://host[:port]) that may drive the
    # run dashboard through a tunnel or private network; loopback is always
    # accepted. Fleet reads HANDSOFF_PUBLIC_ORIGINS instead (no project).
    "public_origins": [],
    "live_check_commands": [],
    "check_timeout_seconds": 600,
    "tickets": [],
    "design_evidence": [],
    "regressions": [],
    "regression_gate": {
        "approval_timeout_minutes": 30, "launch_window_minutes": 10,
        "full_regression_major_only": True,
    },
    "agents": {role: profile["adapter"] for role, profile in RECOMMENDED_CREW.items()},
    "models": {role: profile["model"] for role, profile in RECOMMENDED_CREW.items()},
    "fallbacks": {
        "architect": [],
        "supervisor": [],
        "implementer": [],
        "reviewer": [],
    },
    "max_failovers_per_role": DEFAULT_MAX_FAILOVERS_PER_ROLE,
    "agent_token_budgets": dict(DEFAULT_AGENT_TOKEN_BUDGETS),
    "adapters": {},
    "followup_design_token_budget": None,
    "reviewer_followup": None,
    "recovery": {
        "enabled": True, "max_attempts": 3, "lease_minutes": 15,
        "worker_loss_grace_minutes": 2, "live_session_silence_minutes": 10,
        "protocol_silence_minutes": {"architect": 0, "supervisor": 0, "implementer": 0, "reviewer": 0},
        "liveness_seconds": 60, "dashboard_watchdog": True, "poll_seconds": 30,
        "operation_grace_seconds": 120,
    },
    # #49: the archive analyzer. `filing` is "gh" (file through the gh CLI)
    # or "report_only" (never construct a GitHub client; the report still
    # lists every draft). archive_dir None means HANDSOFF_ARCHIVE_DIR or
    # the default Documents archive.
    "analysis": {
        "enabled": True, "max_tickets_per_scan": 5, "dedupe_days": 30,
        "design_phase_hours_threshold": 1.0, "archive_dir": None, "filing": "gh",
        "framework_repo": "monzta1/project-handsoff",
    },
}
ANALYSIS_FILING_MODES = ("gh", "report_only")
ANALYSIS_DIR = ".handsoff-analysis"
# #49: a run whose root name starts with one of these is a test fixture,
# a self-check, a drop-in or a benchmark run, never a product run.
FIXTURE_ROOT_PREFIXES = (
    "handsoff-test-", "handsoff-selfcheck", "handsoff-dropin", "handsoff-benchmark", "handsoff-fixture",
)
RUN_KINDS = ("test", "product")
MAX_PILOT_NOTE_LENGTH = 512

AGENT_ROLES = ("architect", "supervisor", "implementer", "reviewer")
SELECTABLE_AGENT_ROLES = AGENT_ROLES
LEGACY_AGENT_ROLES = ("architect", "implementer", "reviewer")
SELECTABLE_AGENT_ADAPTERS = ("codex", "claude")
HOST_AGENT_ADAPTER = "host"
HOST_CAPABLE_ROLES = ("supervisor", "architect")
AUTO_AGENT_ADAPTER = "auto"
LEGACY_UNCONFIGURED_AGENT_ADAPTER = "configure-me"
AGENT_SETTING_ADAPTERS = (AUTO_AGENT_ADAPTER, *SELECTABLE_AGENT_ADAPTERS)
DEFAULT_AGENT_PREFERENCE = SELECTABLE_AGENT_ADAPTERS
DEFAULT_AGENT_MODEL = "default"
MAX_AGENT_MODEL_LENGTH = 128
MAX_AGENT_ACTOR_LENGTH = 128
MAX_AGENT_SESSION_ID_LENGTH = 64
MAX_AGENT_SESSIONS = 64
RUNTIME_MANIFEST_FILE = "handsoff-runtime.json"
VERSION_PIN_FILE = ".handsoff-version"
OVERRIDES_FILE = "handsoff-overrides.json"
ROLE_PROTOCOL_PREFIXES = {"reviewer": "HANDSOFF_REVIEW_RESULT:",
                          "architect": "HANDSOFF_DESIGN_PROPOSAL:",
                          "supervisor": "HANDSOFF_BROKER_REQUEST:"}
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
AGENT_SESSION_OPTIONAL_FIELDS = {"packet_id", "design_hash", "tier", "phase_number", "result", "host_session_id", "usage",
                                 "amendment_id"}
AGENT_SESSION_FIELDS = {
    "session_id", "role", "actor", "adapter", "requested_model", "reported_model",
    "resolution_source", "started_at", "running_at", "ended_at", "state", "exit_code",
    *AGENT_SESSION_OPTIONAL_FIELDS,
}


def normalize_public_origins(value, label: str) -> list[str]:
    """#124: an origin is scheme://host[:port], nothing else. Each entry is
    canonicalised (lowercase scheme and host, explicit port dropped only
    when it is the scheme default) so comparison is exact, never a prefix."""
    from urllib.parse import urlsplit
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise HandsoffError(f"{label} must be a list of non-empty origin strings")
    result = []
    for item in value:
        parts = urlsplit(item.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"} \
                or parts.query or parts.fragment or parts.username or parts.password:
            raise HandsoffError(f"{label} entry {item!r} must be scheme://host[:port] with no path")
        port = parts.port
        default = 443 if parts.scheme == "https" else 80
        host = parts.hostname.lower()
        canonical = f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")
        if canonical not in result:
            result.append(canonical)
    return result


def origin_allowed(origin: str | None, server_port: int, public_origins: list[str]) -> bool:
    """Loopback on the server's own port is always allowed; otherwise the
    origin must equal one configured public origin exactly."""
    from urllib.parse import urlsplit
    if not origin:
        return False
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return False
    if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
        return False
    if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"} and port == server_port:
        return True
    try:
        canonical = normalize_public_origins([origin], "origin")[0]
    except HandsoffError:
        return False
    return canonical in public_origins


def fleet_public_origins() -> list[str]:
    raw = os.environ.get("HANDSOFF_PUBLIC_ORIGINS", "")
    entries = [item for item in raw.split(",") if item.strip()]
    return normalize_public_origins(entries, "HANDSOFF_PUBLIC_ORIGINS") if entries else []


CHROME_APP = Path("/Applications/Google Chrome.app")
_OPEN_DASHBOARD_SCRIPT = """
on run argv
  set target to item 1 of argv
  tell application "Google Chrome"
    repeat with w in windows
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        if URL of t starts with target then
          set active tab index of w to i
          set index of w to 1
          activate
          return "found"
        end if
      end repeat
    end repeat
    open location target
    activate
    return "opened"
  end tell
end run
"""


def open_dashboard_url(url: str, *, platform: str | None = None, chrome: Path | None = None,
                       run=subprocess.run, opener=None) -> str:
    """#162: open `url` in the operator's browser exactly once. On macOS with
    Google Chrome installed, one AppleScript activates a tab already on the
    URL (a previous run on the same port) or opens the location, and says
    which on its last line: found or opened. webbrowser.open runs only when
    osascript could not start or answered neither word, which can only
    happen before any tab was opened; a non-zero exit after `opened` is
    still `opened`. Returns found, opened or fallback."""
    platform = platform or sys.platform
    chrome = CHROME_APP if chrome is None else chrome
    opener = opener or webbrowser.open
    if platform == "darwin" and chrome.exists():
        try:
            proc = run(["osascript", "-", url], input=_OPEN_DASHBOARD_SCRIPT, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None:
            answer = (proc.stdout.strip().splitlines() or [""])[-1].strip().lower()
            if answer in {"found", "opened"}:
                return answer
    opener(url)
    return "fallback"


def engine_root() -> Path:
    """Locate this engine's immutable resources in a checkout or installation."""
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / RUNTIME_MANIFEST_FILE).is_file() and (checkout / "dashboard").is_dir():
        return checkout
    return Path(sysconfig.get_path("data")) / "share" / "handsoff"


def engine_resource_path(relative: str) -> Path:
    if not isinstance(relative, str) or relative.startswith(("/", "../")):
        raise HandsoffError("engine resource path is invalid")
    root = engine_root()
    if relative.startswith("bin/") and not (root / relative).exists():
        return Path(__file__).resolve().parent / Path(relative).name
    return root / relative


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)", str(value).strip())
    if not match:
        raise HandsoffError(f"invalid Handsoff version: {value}")
    return tuple(map(int, match.groups()))


def version_satisfies(version: str, pin: str) -> bool:
    actual = _version_tuple(version)
    pin = str(pin).strip()
    wildcard = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.\*", pin)
    if wildcard:
        return actual[:2] == tuple(map(int, wildcard.groups()))
    if pin.startswith("=="):
        pin = pin[2:]
    return actual == _version_tuple(pin)


def _runtime_identity_with_manifest(root: Path) -> dict:
    """Exact engine source, pin, manifest identity, and compatibility."""
    root = Path(root).resolve()
    drop_in = _looks_like_runtime_drop_in(root)
    source = "project-drop-in" if drop_in else "installed-engine"
    path = root / RUNTIME_MANIFEST_FILE if drop_in else engine_root() / RUNTIME_MANIFEST_FILE
    # #204: an engine checkout whose listed files changed after the manifest
    # was written says so on every read of the identity, the dashboard's
    # engine badge and the Fleet card included (they render the reason as
    # ENGINE UNKNOWN, #185), not only when the pin is missing.
    stale = stale_manifest_refusal(root)
    if stale:
        raise HandsoffError(stale)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HandsoffError(
            f"Handsoff runtime manifest is missing: {path}; reinstall the complete Handsoff engine"
        ) from exc
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"Handsoff runtime manifest is unreadable: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "version", "files"} \
            or manifest.get("schema") != 1 or not isinstance(manifest.get("version"), str) \
            or not manifest["version"].strip() or not isinstance(manifest.get("files"), dict) \
            or not manifest["files"]:
        raise HandsoffError("Handsoff runtime manifest is invalid; reinstall the complete Handsoff engine")
    pin_path = root / VERSION_PIN_FILE
    pin = manifest["version"] if drop_in else None
    if not drop_in:
        try:
            pin = pin_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise HandsoffError(f"Handsoff engine version pin is missing: {pin_path}; run `handsoff init {root}`") from exc
        if not version_satisfies(manifest["version"], pin):
            raise HandsoffError(
                f"project requires Handsoff {pin}, but installed engine is {manifest['version']}; "
                "install the compatible engine or update the pin deliberately"
            )
    return {
        "version": manifest["version"], "source": source, "source_root": str(path.parent),
        "compatibility": pin, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "manifest": manifest,
    }


def _looks_like_runtime_drop_in(root: Path) -> bool:
    """Recognize a copied engine from its signed runtime, never its names.

    A project is allowed to have directories called ``bin`` or ``schemas``.
    Those names alone are not evidence that an engine was copied into it.
    """
    manifest_path = Path(root) / RUNTIME_MANIFEST_FILE
    library = Path(root) / "bin" / "handsoff_lib.py"
    if not manifest_path.is_file() or not library.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("files", {}).get("bin/handsoff_lib.py")
        return isinstance(expected, str) and hashlib.sha256(library.read_bytes()).hexdigest() == expected
    except (OSError, ValueError, AttributeError):
        return False


def runtime_identity(root: Path) -> dict:
    """Public content-free engine identity; never return the manifest body."""
    identity = _runtime_identity_with_manifest(root)
    identity.pop("manifest", None)
    return identity


def engine_manifest_version() -> str | None:
    """The version of the engine running this code, from its own manifest;
    None when it cannot be read."""
    try:
        return json.loads((engine_root() / RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def ledger_engine_identity(root: Path) -> dict | None:
    """#117: the three identity fields an event carries so an archive can
    say which engine ran it; None when the identity cannot be read (the
    event is still written, the field is just absent)."""
    try:
        identity = runtime_identity(Path(root))
    except (HandsoffError, OSError, ValueError):
        return None
    return {"version": identity.get("version"), "source": identity.get("source"),
            "manifest_sha256": identity.get("manifest_sha256")}


def retire_finished_run(root: Path, cfg: dict) -> Path | None:
    """#118: move a complete or closed run's ledgers into
    .handsoff-archive/<UTC date>-<slug>/ so the next mission can start;
    None (and nothing moved) when the run is still in progress or the
    status cannot be read."""
    root = Path(root)
    try:
        status = load_unique_json(status_path(root, cfg))
    except (HandsoffError, OSError, ValueError):
        return None
    # Only a recorded completion or closure counts; a run at Phase 8 whose
    # status is still in_progress has not finished (review finding).
    finished = status.get("status") in {"complete", "closed"} \
        or isinstance(status.get("run_closed"), dict)
    if not finished:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(status.get("feature") or "run").lower()).strip("-")[:48] or "run"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    target = root / ".handsoff-archive" / f"{stamp}-{slug}"
    target.mkdir(parents=True, exist_ok=False)
    moved = [status_path(root, cfg), acceptance_path(root, cfg), event_log_path(root, cfg),
             verification_log_path(root, cfg), event_head_path(root), root / ".handsoff-digests"]
    for path in moved:
        if path.exists():
            path.rename(target / path.name)
    return target


def engine_history(events: list[dict]) -> list[dict]:
    """Distinct engine identities in ledger order (#117)."""
    seen = []
    for event in events or []:
        engine = event.get("engine") if isinstance(event, dict) else None
        if isinstance(engine, dict) and engine not in seen:
            seen.append(engine)
    return seen


def stale_manifest_refusal(root: Path) -> str | None:
    """#204: an engine checkout (bin/handsoff_manifest.py beside
    handsoff-runtime.json) whose runtime files changed after the manifest
    was written. Returns the one line that names the changed files and
    the exact command; None for a thin project or a checkout in step.
    Three hosts in one day edited bin/ or prompts/, launched a reviewer or
    ran verify, and read 'override not declared' or 'runtime files do not
    match; reinstall the engine', both of which point at the wrong place."""
    root = Path(root).resolve()
    manifest_path = root / RUNTIME_MANIFEST_FILE
    generator = root / "bin" / "handsoff_manifest.py"
    pyproject = root / "pyproject.toml"
    if not manifest_path.is_file() or not generator.is_file() or not pyproject.is_file():
        return None
    try:
        if 'name = "project-handsoff"' not in pyproject.read_text(encoding="utf-8"):
            return None  # a thin project that happens to carry the generator
    except OSError:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files") or {}
    except (OSError, ValueError, AttributeError):
        return None
    changed = []
    for relative, expected in sorted(files.items()):
        if not isinstance(relative, str) or relative.startswith(("/", "../")):
            continue
        target = root / relative
        try:
            actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        except OSError:
            actual = None
        if actual != expected:
            changed.append(relative)
    if not changed:
        return None
    version = None
    try:
        match = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
        version = match.group(1) if match else None
    except OSError:
        pass
    shown = ", ".join(changed[:6]) + (f" (+{len(changed) - 6} more)" if len(changed) > 6 else "")
    tag = f"v{version}" if version else "vX.Y.Z"
    return (f"the runtime manifest is stale ({shown} changed after it was written): "
            f"run python3 bin/handsoff_manifest.py --version {tag}, then retry")


def validate_runtime_integrity(root: Path) -> dict:
    """Refuse stale drop-ins, incompatible pins, or a corrupted installed engine."""
    stale = stale_manifest_refusal(root)
    if stale:
        raise HandsoffError(stale)
    root = Path(root).resolve()
    identity = _runtime_identity_with_manifest(root)
    manifest = identity.pop("manifest")
    drop_in = identity["source"] == "project-drop-in"
    mismatches = []
    for relative, expected in sorted(manifest["files"].items()):
        if not isinstance(relative, str) or relative.startswith(("/", "../")) \
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise HandsoffError("Handsoff runtime manifest contains an invalid entry")
        target = (root / relative).resolve() if drop_in else engine_resource_path(relative).resolve()
        try:
            if drop_in:
                target.relative_to(root)
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, ValueError):
            actual = None
        if actual != expected:
            mismatches.append(relative)
    if mismatches:
        shown = ", ".join(mismatches[:8])
        suffix = f" (+{len(mismatches) - 8} more)" if len(mismatches) > 8 else ""
        raise HandsoffError(
            f"Handsoff runtime files do not match release {manifest['version']}: {shown}{suffix}; "
            f"{'refresh the complete release drop-in' if drop_in else 'reinstall the Handsoff engine'}"
        )
    if not drop_in and (root / OVERRIDES_FILE).exists():
        try:
            overrides = json.loads((root / OVERRIDES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HandsoffError(f"{OVERRIDES_FILE} is unreadable") from exc
        files = overrides.get("files") if isinstance(overrides, dict) and overrides.get("schema") == 1 else None
        if not isinstance(files, dict) or set(overrides) != {"schema", "files"}:
            raise HandsoffError(f"{OVERRIDES_FILE} is invalid")
        allowed = {f"prompts/{role}.md" for role in SELECTABLE_AGENT_ROLES}
        unknown = set(files) - allowed
        if unknown:
            raise HandsoffError(f"trusted core runtime overrides are forbidden: {', '.join(sorted(unknown))}")
        for relative in files:
            project_resource_path(root, relative)
    return {**identity, "files": len(manifest["files"]), "state": "verified"}


def project_resource_path(root: Path, relative: str) -> Path:
    """Resolve an optional thin-project override only when hash-declared."""
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    stale = stale_manifest_refusal(root)  # #204: an engine checkout out of step says so first
    if stale:
        raise HandsoffError(stale)
    drop_in = _looks_like_runtime_drop_in(root)
    if drop_in:
        return candidate
    if candidate.is_file():
        try:
            overrides = json.loads((root / OVERRIDES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HandsoffError(f"project override {relative} is not declared in {OVERRIDES_FILE}") from exc
        files = overrides.get("files") if isinstance(overrides, dict) else None
        expected = files.get(relative) if isinstance(files, dict) else None
        if expected is None:
            raise HandsoffError(f"project override {relative} is not declared in {OVERRIDES_FILE}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if expected != actual:
            raise HandsoffError(f"project override hash mismatch: {relative}")
        return candidate
    return engine_resource_path(relative)


def briefing_section(root: Path, cfg: dict, topic: str | None = None) -> str:
    """Resolve the bounded knowledge briefing for one managed launch.

    A missing [briefing] block is deliberately a no-op. When configured, the
    index is authoritative: always_load files are included first, followed by
    files whose declared topics match the optional one-launch topic. Every
    selected path must remain inside the project root and exist as a regular
    file before a managed session can be reserved.
    """
    briefing = cfg.get("briefing")
    if briefing is None:
        if topic is not None:
            raise HandsoffError("--topic requires a [briefing] block in handsoff.toml")
        return ""
    if topic is not None and (not isinstance(topic, str) or not topic.strip()):
        raise HandsoffError("--topic must be a non-empty topic name")
    root = Path(root).resolve()
    index_path = (root / briefing["index"]).resolve()
    if not index_path.is_file():
        raise HandsoffError(f"briefing index is missing: {index_path}")
    kb_root = (root / briefing.get("root", "")).resolve() if briefing.get("root") else index_path.parent
    try:
        index_path.relative_to(root)
        kb_root.relative_to(root)
    except ValueError as exc:
        raise HandsoffError("briefing paths must remain inside the project root") from exc
    try:
        manifest = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"cannot read briefing index {index_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise HandsoffError("briefing index must be a version 1 object")
    topics = manifest.get("topics")
    always_load = manifest.get("always_load")
    files = manifest.get("files")
    if not isinstance(topics, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in topics.items()):
        raise HandsoffError("briefing index topics must map strings to strings")
    if not isinstance(always_load, list) or not all(isinstance(item, str) and item.strip() for item in always_load):
        raise HandsoffError("briefing index always_load must be a list of file names")
    if not isinstance(files, list):
        raise HandsoffError("briefing index files must be a list")
    declared = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"].strip():
            raise HandsoffError("briefing index files must contain a file name")
        item_topics = item.get("topics", [])
        if not isinstance(item_topics, list) or not all(isinstance(value, str) and value in topics for value in item_topics):
            raise HandsoffError(f"briefing index topics are invalid for {item['file']}")
        declared[item["file"]] = tuple(item_topics)
    selected = list(always_load)
    if topic is not None:
        topic = topic.strip()
        if topic not in topics:
            raise HandsoffError(f"briefing topic is not declared in the index: {topic}")
        selected.extend(name for name, item_topics in declared.items() if topic in item_topics)
    unique = []
    for name in selected:
        if name not in unique:
            unique.append(name)
    sections = []
    total = 0
    for name in unique:
        relative = Path(name)
        if relative.is_absolute():
            raise HandsoffError(f"briefing file must be relative: {name}")
        path = (kb_root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HandsoffError(f"briefing file escapes the project root: {name}") from exc
        if not path.is_file():
            raise HandsoffError(f"briefing file is missing: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise HandsoffError(f"cannot read briefing file {path}: {exc}") from exc
        if len(data) > MAX_BRIEFING_FILE_BYTES:
            raise HandsoffError(f"briefing file is larger than {MAX_BRIEFING_FILE_BYTES} bytes: {path}")
        total += len(data)
        if total > MAX_BRIEFING_TOTAL_BYTES:
            raise HandsoffError(f"briefing exceeds {MAX_BRIEFING_TOTAL_BYTES} bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandsoffError(f"briefing file is not UTF-8: {path}") from exc
        sections.append(f"## {name}\n\n{text.rstrip()}")
    if not sections:
        raise HandsoffError("briefing index selected no files")
    return "# Knowledge base briefing\n\n" + "\n\n".join(sections)


def prompt_override_diagnosis(root: Path) -> list[dict]:
    """Classify project prompt overrides against the installed engine contract.

    A runtime drop-in (the engine checkout itself) owns its prompts outright,
    so it has nothing to declare and reports an empty list."""
    root = Path(root).resolve()
    if _looks_like_runtime_drop_in(root):
        return []
    override_path = root / OVERRIDES_FILE
    declared = {}
    if override_path.is_file():
        try:
            value = json.loads(override_path.read_text(encoding="utf-8"))
            declared = value.get("files", {}) if isinstance(value, dict) else {}
        except (OSError, ValueError):
            declared = {}
    prompt_dir = root / "prompts"
    paths = {f"prompts/{role}.md": prompt_dir / f"{role}.md" for role in SELECTABLE_AGENT_ROLES}
    paths.update({key: root / key for key in declared if isinstance(key, str) and key.startswith("prompts/")})
    result = []
    for relative in sorted(paths):
        role = Path(relative).stem
        if role not in SELECTABLE_AGENT_ROLES:
            continue
        path = paths[relative]
        present = path.is_file()
        expected_prefix = ROLE_PROTOCOL_PREFIXES.get(role)
        if not present:
            if relative in declared:
                result.append({"role": role, "path": relative, "state": "declared_missing",
                               "declared": True, "expected_prefix": expected_prefix,
                               "detail": "declared override file is missing"})
            continue
        if relative not in declared:
            result.append({"role": role, "path": relative, "state": "undeclared",
                           "declared": False, "expected_prefix": expected_prefix,
                           "detail": "project prompt is not declared"})
            continue
        text = path.read_text(encoding="utf-8")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if declared[relative] != actual:
            state, detail = "declared_hash_mismatch", "declared hash differs"
        elif expected_prefix and expected_prefix not in text:
            state, detail = "declared_stale_protocol", f"missing {expected_prefix}"
        else:
            state, detail = "declared_current", "override matches the engine contract"
        result.append({"role": role, "path": relative, "state": state, "declared": True,
                       "expected_prefix": expected_prefix, "detail": detail})
    return result


def run_triage(root: Path, cfg: dict) -> dict | None:
    path = status_path(Path(root), cfg)
    if not path.is_file():
        return None
    status = load_unique_json(path)
    if status.get("status") == "complete" or int(status.get("phase_number", 0) or 0) >= 8:
        return None
    escalation = status.get("escalation")
    kind = escalation.get("kind") if isinstance(escalation, dict) else None
    root_text = str(Path(root).resolve())
    return {"phase_number": status.get("phase_number"), "status": status.get("status"),
            "escalation_kind": kind, "blocked": status.get("status") == "blocked" or bool(escalation),
            "options": [
                {"action": "recover", "command": f"handsoff supervisor --root {root_text} recover --by ACTOR",
                 "preserves": "Recover keeps the run and its ledgers and retries the assigned role once."},
                {"action": "close", "command": f"handsoff supervisor --root {root_text} run-close --by ACTOR --reason TEXT",
                 "preserves": "Close keeps the ledgers, releases the dashboard and marks the run closed."},
                {"action": "reopen", "command": f"handsoff supervisor --root {root_text} run-reopen --by ACTOR --reason TEXT",
                 "preserves": "Reopen restores a closed run at its recorded phase."},
            ]}
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
#: "baseline" (#165): a criterion's own commands run BEFORE the feature,
#: recorded as ok=True when every one of them failed (a valid red) and
#: ok=False when any passed (baseline_invalid). It never satisfies the
#: checks requirement; it is what the failing-first gate asks for behind
#: the later green run.
VERIFICATION_KINDS = {"checks", "manual", "browser", "live", "baseline"}
BASELINE_NOT_APPLICABLE = "not_applicable"
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
    cfg["agent_token_budgets"] = dict(DEFAULT_AGENT_TOKEN_BUDGETS)
    cfg["adapters"] = {}
    cfg["design_evidence"] = []
    cfg["profile_sources"] = {
        role: {"adapter": RECOMMENDED_PROFILE_SOURCE, "model": RECOMMENDED_PROFILE_SOURCE}
        for role in AGENT_ROLES
    }
    cfg["recovery"] = dict(DEFAULT_CONFIG["recovery"])
    cfg["recovery"]["protocol_silence_minutes"] = dict(DEFAULT_CONFIG["recovery"]["protocol_silence_minutes"])
    cfg["regression_gate"] = dict(DEFAULT_CONFIG["regression_gate"])
    cfg["analysis"] = dict(DEFAULT_CONFIG["analysis"])
    cfg["documentation"] = {key: list(value) for key, value in DEFAULT_CONFIG["documentation"].items()}
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
    agent_budget = raw.get("agent_budget", {})
    adapters = raw.get("adapters", {})
    checks = raw.get("checks", {})
    implementer = raw.get("implementer", {})
    documentation = raw.get("documentation", {})
    dashboard_table = raw.get("dashboard", {})
    recovery = raw.get("recovery", {})
    regression_gate = raw.get("regression_gate", {})
    analysis = raw.get("analysis", {})
    digest = raw.get("digest", {})
    briefing = raw.get("briefing")
    regressions = raw.get("regressions", [])
    tickets = raw.get("tickets", [])
    if not isinstance(digest, dict):
        raise HandsoffError("handsoff.toml: digest must be a table")
    if not all(isinstance(section, dict) for section in (project, workflow, agents, models, fallback_policy, agent_budget, adapters, checks, implementer, documentation, recovery, regression_gate, analysis)):
        raise HandsoffError(
            "handsoff.toml: project, workflow, agents, models, fallback_policy, agent_budget, checks, implementer, documentation, recovery, regression_gate, and analysis must be tables"
        )
    if briefing is not None:
        if not isinstance(briefing, dict):
            raise HandsoffError("handsoff.toml: briefing must be a table")
        unknown_briefing = set(briefing) - BRIEFING_CONFIG_KEYS
        if unknown_briefing:
            raise HandsoffError(
                "handsoff.toml: briefing has unknown keys: " + ", ".join(sorted(unknown_briefing))
            )
        index = briefing.get("index")
        if not isinstance(index, str) or not index.strip() or Path(index).is_absolute() or ".." in Path(index).parts:
            raise HandsoffError("handsoff.toml: briefing.index must be a safe relative path")
        kb_root = briefing.get("root", "")
        if not isinstance(kb_root, str) or (kb_root and (Path(kb_root).is_absolute() or ".." in Path(kb_root).parts)):
            raise HandsoffError("handsoff.toml: briefing.root must be a safe relative path when set")
        cfg["briefing"] = {"index": index.strip(), "root": kb_root.strip()}
    cfg["status_file"] = project.get("status_file", cfg["status_file"])
    cfg["acceptance_file"] = project.get("acceptance_file", cfg["acceptance_file"])
    cfg["event_log"] = project.get("event_log", cfg["event_log"])
    cfg["verification_log"] = project.get("verification_log", cfg["verification_log"])
    logo = project.get("logo")
    if logo is not None and (not isinstance(logo, str) or not logo.strip()):
        raise HandsoffError("handsoff.toml: project.logo must be a non-empty relative path when set")
    cfg["logo"] = logo
    ignore = digest.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(item, str) for item in ignore):
        raise HandsoffError("handsoff.toml: digest.ignore must be a list of strings")
    cfg["digest_ignore"] = list(ignore)
    if set(adapters) - set(SELECTABLE_AGENT_ADAPTERS):
        raise HandsoffError("handsoff.toml: adapters may only contain codex and claude")
    for adapter, value in adapters.items():
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"handsoff.toml: adapters.{adapter} must name an existing file")
        path_value = Path(value).expanduser()
        if not path_value.is_absolute(): path_value = root / path_value
        if not path_value.is_file(): raise HandsoffError(f"handsoff.toml: adapters.{adapter} must name an existing file")
        cfg["adapters"][adapter] = str(path_value.resolve())
    for key in ("max_design_rounds", "max_review_rounds", "stall_minutes", "max_autonomous_design_reviews",
                "small_fix_max_criteria", "small_fix_max_changed_lines", "small_fix_max_files"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be an integer")
        cfg[key] = value
    for key in ("auto_handoff", "require_live_verification", "deployment_requires_explicit_approval",
                "require_design_approval"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be boolean")
        cfg[key] = value
    features = raw.get("features", {})
    if not isinstance(features, dict):
        raise HandsoffError("handsoff.toml: [features] must be a table")
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise HandsoffError("handsoff.toml: unknown [features] key(s): " + ", ".join(unknown)
                            + "; known: " + ", ".join(FEATURES))
    for key, value in features.items():
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: features.{key} must be boolean")
    cfg["features"] = {name: bool(features.get(name, default)) for name, (default, _text) in FEATURES.items()}
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
        if value not in AGENT_SETTING_ADAPTERS and value != HOST_AGENT_ADAPTER:
            raise HandsoffError(f"handsoff.toml: agents.{role} must be exactly 'auto', 'codex', 'claude', or 'host'")
        cfg["agents"][role] = value
        if value == HOST_AGENT_ADAPTER and role not in HOST_CAPABLE_ROLES:
            raise HandsoffError(f"[agents].{role} cannot be host: only supervisor and architect may be host-driven")
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
    unknown_budget_roles = set(agent_budget) - {*SELECTABLE_AGENT_ROLES, "followup_design"}
    if unknown_budget_roles:
        raise HandsoffError(
            "handsoff.toml: agent_budget has unknown keys: "
            + ", ".join(sorted(unknown_budget_roles))
        )
    for role in SELECTABLE_AGENT_ROLES:
        value = agent_budget.get(role, DEFAULT_AGENT_TOKEN_BUDGETS[role])
        if not isinstance(value, int) or isinstance(value, bool) \
                or not MIN_AGENT_TOKEN_BUDGET <= value <= MAX_AGENT_TOKEN_BUDGET:
            raise HandsoffError(
                f"handsoff.toml: agent_budget.{role} must be an integer from "
                f"{MIN_AGENT_TOKEN_BUDGET} to {MAX_AGENT_TOKEN_BUDGET}"
            )
        cfg["agent_token_budgets"][role] = value
    followup_budget = agent_budget.get("followup_design")
    if followup_budget is not None:
        if not isinstance(followup_budget, int) or isinstance(followup_budget, bool) or followup_budget < MIN_AGENT_TOKEN_BUDGET:
            raise HandsoffError("handsoff.toml: agent_budget.followup_design must be a positive integer")
        cfg["followup_design_token_budget"] = followup_budget
    for config_key, toml_key in (("check_commands", "commands"), ("live_check_commands", "live_commands")):
        value = checks.get(toml_key, cfg[config_key])
        if not isinstance(value, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in value):
            raise HandsoffError(f"handsoff.toml: checks.{toml_key} must be an array of non-empty command strings")
        cfg[config_key] = list(value)
    # Field-note defect 5: a live command with shell operators used to pass
    # validate, status and doctor and fail only at verify-live, after
    # deployment approval. Refuse it at load with verify-live's own message.
    for index, command in enumerate(cfg["live_check_commands"]):
        try:
            assert_plain_command(command)
        except HandsoffError as exc:
            raise HandsoffError(f"handsoff.toml: checks.live_commands[{index}]: {exc}") from exc
    implementer_commands = implementer.get("commands", [])
    if not isinstance(implementer_commands, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in implementer_commands):
        raise HandsoffError("handsoff.toml: implementer.commands must be an array of non-empty command strings")
    cfg["implementer_commands"] = list(implementer_commands)
    timeout_value = checks.get("timeout_seconds", cfg["check_timeout_seconds"])
    if not isinstance(timeout_value, int) or isinstance(timeout_value, bool) or timeout_value <= 0:
        raise HandsoffError("handsoff.toml: checks.timeout_seconds must be a positive integer")
    cfg["check_timeout_seconds"] = timeout_value
    for key in ("files", "exclude"):
        value = documentation.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise HandsoffError(f"handsoff.toml: documentation.{key} must be a list of non-empty strings")
        cfg["documentation"][key] = list(value)
    if not isinstance(dashboard_table, dict):
        raise HandsoffError("handsoff.toml: dashboard must be a table")
    cfg["public_origins"] = normalize_public_origins(dashboard_table.get("public_origins", []), "handsoff.toml: dashboard.public_origins")
    unknown_gate = set(regression_gate) - set(DEFAULT_CONFIG["regression_gate"])
    if unknown_gate:
        raise HandsoffError(f"handsoff.toml: regression_gate has unknown keys: {', '.join(sorted(unknown_gate))}")
    for key in ("approval_timeout_minutes", "launch_window_minutes"):
        value = regression_gate.get(key, cfg["regression_gate"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440:
            raise HandsoffError(f"handsoff.toml: regression_gate.{key} must be an integer from 1 to 1440")
        cfg["regression_gate"][key] = value
    major_only = regression_gate.get(
        "full_regression_major_only", cfg["regression_gate"]["full_regression_major_only"],
    )
    if not isinstance(major_only, bool):
        raise HandsoffError("handsoff.toml: regression_gate.full_regression_major_only must be boolean")
    cfg["regression_gate"]["full_regression_major_only"] = major_only
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
        "operation_grace_seconds": (0, 3600),
    }
    for key, (minimum, maximum) in bounds.items():
        value = recovery.get(key, cfg["recovery"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise HandsoffError(
                f"handsoff.toml: recovery.{key} must be an integer from {minimum} to {maximum}"
            )
        cfg["recovery"][key] = value
    protocol_limits = recovery.get("protocol_silence_minutes", cfg["recovery"]["protocol_silence_minutes"])
    if not isinstance(protocol_limits, dict):
        raise HandsoffError("handsoff.toml: recovery.protocol_silence_minutes must be a per-role table")
    unknown_roles = set(protocol_limits) - set(AGENT_ROLES)
    if unknown_roles:
        raise HandsoffError("handsoff.toml: recovery.protocol_silence_minutes has unknown roles: " + ", ".join(sorted(unknown_roles)))
    for role_name in AGENT_ROLES:
        value = protocol_limits.get(role_name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1440:
            raise HandsoffError(f"handsoff.toml: recovery.protocol_silence_minutes.{role_name} must be an integer from 0 to 1440")
        cfg["recovery"]["protocol_silence_minutes"][role_name] = value
    cfg["analysis"] = _validate_analysis_config(analysis)
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
    for key in ("status_file", "acceptance_file", "event_log", "verification_log", "logo"):
        value = cfg[key]
        if key == "logo" and value is None:
            continue
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
    for key in ("max_design_rounds", "stall_minutes", "max_autonomous_design_reviews",
                "small_fix_max_criteria", "small_fix_max_changed_lines", "small_fix_max_files"):
        if cfg[key] < 0:
            raise HandsoffError(f"handsoff.toml: {key} must not be negative")
    if not 1 <= cfg["max_review_rounds"] <= 56:
        raise HandsoffError("handsoff.toml: workflow.max_review_rounds must be an integer from 1 to 56")
    if not 1 <= cfg["small_fix_max_criteria"] <= 64:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_criteria must be an integer from 1 to 64")
    if not 1 <= cfg["small_fix_max_changed_lines"] <= 100000:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_changed_lines must be an integer from 1 to 100000")
    if not 1 <= cfg["small_fix_max_files"] <= 1000:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_files must be an integer from 1 to 1000")
    ensure_regression_config_is_disjoint(cfg, root)
    return cfg


#: The two supervisor operations an implementer may run, in every form the
#: project can reach them by. Everything else stays with the host.
IMPLEMENTER_SUPERVISOR_OPERATIONS = ("verify", "record-symptom-resolved")

#: Codex sandbox flags shared by the launcher and the pre-flight probe.
CODEX_DISABLED_FEATURES = (
    "plugins", "apps", "skill_search", "multi_agent", "goals",
    "browser_use", "computer_use", "image_generation",
)
#: Field-note defect 8: tests that bind a loopback listener are common;
#: a workspace-write Codex sandbox gets network access so they can run.
CODEX_WORKSPACE_NETWORK_FLAG = "sandbox_workspace_write.network_access=true"


def claude_argv(executable: str, role: str, allowed_tools: list[str] | None, model: str = DEFAULT_AGENT_MODEL) -> list[str]:
    """The one Claude Code argv shape Handsoff launches (and pre-flights).

    `--verbose` sits next to `--output-format stream-json`: the Claude CLI
    refuses stream-json under --print without it (field-note defect 1), so
    every managed Claude role used to exit 1 at launch.
    """
    mode = "default" if role in {"reviewer", "supervisor", "architect"} else "acceptEdits"
    argv = [str(executable), "-p", "--verbose", "--output-format", "stream-json", "--permission-mode", mode]
    argv.extend(["--allowedTools", ",".join(allowed_tools or [])])
    if model != DEFAULT_AGENT_MODEL:
        argv.extend(["--model", model])
    return argv


def codex_argv(executable: str, role: str, model: str, token_budget: int, *, reviewer_sandbox: bool = False) -> list[str]:
    """The one Codex argv shape Handsoff launches (and pre-flights)."""
    workspace_write = reviewer_sandbox or role == "implementer"
    sandbox = "workspace-write" if workspace_write else "read-only"
    argv = [str(executable), "exec", "--ephemeral", "--sandbox", sandbox]
    if reviewer_sandbox:
        argv.append("--skip-git-repo-check")
    for feature in CODEX_DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    # Codex's native shared rollout meter stops the complete agent loop,
    # including repeated tool/model turns.  Count both prefill and sampling
    # tokens at full weight so cached or repeated context is not free from
    # Handsoff's safety ceiling.
    argv.extend([
        "-c",
        ("features.rollout_budget={enabled=true,"
         f"limit_tokens={token_budget},reminder_at_remaining_tokens=[],"
         "sampling_token_weight=1.0,prefill_token_weight=1.0}"),
    ])
    if workspace_write:
        argv.extend(["-c", CODEX_WORKSPACE_NETWORK_FLAG])
    if model != DEFAULT_AGENT_MODEL:
        argv.extend(["--model", model])
    argv.append("-")
    return argv


def implementer_supervisor_forms(root: Path | None, which=shutil.which) -> list[str]:
    """The supervisor command prefixes that exist for this project: the
    drop-in script when bin/handsoff_supervisor.py is in the tree, else the
    installed console (`handsoff supervisor ...`) plus its resolved absolute
    path so an allowlist matches however the implementer spells it."""
    if root is not None and (Path(root) / "bin" / "handsoff_supervisor.py").is_file():
        return ["python3 bin/handsoff_supervisor.py"]
    forms = ["handsoff supervisor"]
    console = which("handsoff")
    if console:
        forms.append(f"{Path(console).resolve()} supervisor")
    return forms


def implementer_allowed_tools(cfg: dict, root: Path | None = None, which=shutil.which) -> list[str]:
    """Build Claude's exact implementation tool surface from governed commands.

    The command prefixes are intentionally explicit: Claude may edit the
    workspace, but it can run only configured checks and the two evidence
    operations needed to bind its work to the acceptance ledger, in the
    command forms that exist for this project (field-note defect 2: an
    installed-engine project has no bin/).
    """
    tools = [f"Bash({command})" for command in cfg.get("check_commands", [])]
    for form in implementer_supervisor_forms(root, which):
        for operation in IMPLEMENTER_SUPERVISOR_OPERATIONS:
            tools.append(f"Bash({form} {operation}*)")
    tools.extend(cfg.get("implementer_commands", []))
    tools.extend(["Read", "Edit", "Write", "Glob", "Grep"])
    return tools


def implementer_permissions_section(root: Path | None, which=shutil.which) -> str:
    """The role-input paragraph that tells a Claude implementer exactly which
    supervisor command forms are permitted."""
    forms = implementer_supervisor_forms(root, which)
    lines = ["# Permitted supervisor commands", "",
             "Run verify and record-symptom-resolved only in these exact forms (the launcher allows nothing else; "
             "--root is unnecessary when run from the project root):"]
    for form in forms:
        for operation in IMPLEMENTER_SUPERVISOR_OPERATIONS:
            lines.append(f"- `{form} {operation} ...`")
    return "\n".join(lines)


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


def _validate_analysis_config(analysis: dict) -> dict:
    """#49: the [analysis] table. Every key is optional; an invalid value is
    a load error like every other section."""
    cfg = dict(DEFAULT_CONFIG["analysis"])
    unknown = set(analysis) - set(cfg)
    if unknown:
        raise HandsoffError(f"handsoff.toml: analysis has unknown keys: {', '.join(sorted(unknown))}")
    enabled = analysis.get("enabled", cfg["enabled"])
    if not isinstance(enabled, bool):
        raise HandsoffError("handsoff.toml: analysis.enabled must be boolean")
    cfg["enabled"] = enabled
    for key, (minimum, maximum) in (("max_tickets_per_scan", (0, 50)), ("dedupe_days", (0, 365))):
        value = analysis.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise HandsoffError(f"handsoff.toml: analysis.{key} must be an integer from {minimum} to {maximum}")
        cfg[key] = value
    threshold = analysis.get("design_phase_hours_threshold", cfg["design_phase_hours_threshold"])
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
            or not math.isfinite(threshold) or threshold <= 0:
        raise HandsoffError("handsoff.toml: analysis.design_phase_hours_threshold must be a number greater than 0")
    cfg["design_phase_hours_threshold"] = float(threshold)
    archive = analysis.get("archive_dir", cfg["archive_dir"])
    if archive is not None and (not isinstance(archive, str) or not archive.strip()):
        raise HandsoffError("handsoff.toml: analysis.archive_dir must be a non-empty string when set")
    cfg["archive_dir"] = archive.strip() if isinstance(archive, str) else None
    framework_repo = analysis.get("framework_repo", cfg["framework_repo"])
    if not isinstance(framework_repo, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", framework_repo.strip()):
        raise HandsoffError("handsoff.toml: analysis.framework_repo must be an owner/repository slug")
    cfg["framework_repo"] = framework_repo.strip()
    filing = analysis.get("filing", cfg["filing"])
    if filing not in ANALYSIS_FILING_MODES:
        raise HandsoffError("handsoff.toml: analysis.filing must be exactly 'gh' or 'report_only'")
    cfg["filing"] = filing
    return cfg


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
        role: {"adapter": cfg["agents"][role],
               "model": None if cfg["agents"][role] == HOST_AGENT_ADAPTER else cfg["models"][role]}
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
        if adapter == HOST_AGENT_ADAPTER:
            resolved[role] = {"adapter": adapter, "model": None, "source": dict(sources[role])}
            continue
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
        discovered = cfg.get("adapters", {}).get(adapter) or (lookup(adapter) if adapter in SELECTABLE_AGENT_ADAPTERS else None)
        executable = str(Path(discovered).resolve()) if discovered else None
        view[role] = {
            "adapter": adapter,
            "model": profile["model"],
            "adapter_source": profile["source"]["adapter"],
            "model_source": profile["source"]["model"],
            "available": executable is not None,
            "executable": executable,
            "availability_scope": CREW_AVAILABILITY_SCOPE,
            "budget_warning": budget_warning(cfg, role),
        }
    return view

def budget_warning(cfg: dict, role: str) -> str | None:
    """Explain the four-turn safety floor so low configured budgets are visible."""
    value = cfg.get("agent_token_budgets", {}).get(role)
    return f"budget for {role} ({value}) is below 20000 tokens (5000 per turn times 4 turns)" if isinstance(value, int) and value < 20000 else None


def adapter_availability(cfg: dict | None = None, *, which=None) -> dict:
    """Executable discovery only; callers must not imply runtime readiness."""
    result = {}
    for adapter in SELECTABLE_AGENT_ADAPTERS:
        override = (cfg or {}).get("adapters", {}).get(adapter)
        discovered = override or (which or shutil.which)(adapter)
        executable = str(Path(discovered).resolve()) if discovered else None
        result[adapter] = {"available": executable is not None, "executable": executable}
    return result

def adapter_preflight(cfg: dict, root: Path, which=shutil.which, runner=subprocess.run, timeout=60) -> dict:
    # Tests and CI set HANDSOFF_SKIP_PREFLIGHT=1: a unit test must never
    # make a real model call, and doctor is called by many fixtures.
    if os.environ.get("HANDSOFF_SKIP_PREFLIGHT") == "1":
        return {adapter: {"state": "not_checked", "reason": "HANDSOFF_SKIP_PREFLIGHT=1",
                          "checked_at": None, "executable": None}
                for adapter in SELECTABLE_AGENT_ADAPTERS}
    """Probe each configured CLI once with a tiny prompt, recording bounded diagnostic state.

    The probe runs from a throwaway scratch directory, never the project
    root: a Codex probe run inside a real project read the tree, spent the
    borrowed 8,000-token floor on that context and answered `OK` followed
    by a rollout-budget error, which `doctor` reported as unreachable and a
    launch within 24 hours refused (v0.3.25 field-note defect 1). The Codex
    argv is the managed Reviewer's exact launch shape (scratch sandbox,
    `--skip-git-repo-check`) with its own PREFLIGHT_TOKEN_BUDGET, and an
    `OK` reply before a trailing budget error counts as reachable, the same
    acceptance the launcher applies to a complete protocol line (#114).
    """
    result = {}
    scratch = Path(tempfile.mkdtemp(prefix="handsoff-preflight-")).resolve()
    try:
        for adapter in SELECTABLE_AGENT_ADAPTERS:
            executable = cfg.get("adapters", {}).get(adapter) or which(adapter)
            item = {"state": "not_checked", "reason": "executable missing", "checked_at": datetime.now(timezone.utc).isoformat(), "executable": str(executable) if executable else None}
            if executable:
                # Field-note defect 1 (v0.3.22): probe with the launch argv
                # shape (no model override) so a flag the CLI refuses fails here.
                if adapter == "codex":
                    argv = codex_argv(str(executable), "reviewer", DEFAULT_AGENT_MODEL, PREFLIGHT_TOKEN_BUDGET, reviewer_sandbox=True)
                else:
                    argv = claude_argv(str(executable), "reviewer", [], DEFAULT_AGENT_MODEL)
                try:
                    completed = runner(argv, input="Reply with OK", text=True, capture_output=True, timeout=timeout, cwd=str(scratch))
                    item.update(_preflight_outcome(completed))
                except subprocess.TimeoutExpired:
                    item.update(state="unreachable", reason="timeout")
            result[adapter] = item
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    (root / PREFLIGHT_FILE).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


PREFLIGHT_OK_BEFORE_BUDGET_REASON = "OK before trailing token-budget exhaustion"


def _preflight_outcome(completed) -> dict:
    """Classify one probe run. A non-zero exit whose stdout carries the `OK`
    reply and whose tail is a rollout-budget error is the CLI working and
    the budget meter closing the turn afterwards, so it is reachable; any
    other non-zero exit keeps the bounded, redacted stderr tail."""
    if completed.returncode == 0:
        return {"state": "reachable", "reason": None}
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    failure = classify_runtime_failure(exit_code=completed.returncode, stderr_tail=stderr[-2000:], stdout_tail=stdout[-2000:])
    if failure["category"] == "token_budget_exhaustion" and re.search(r"\bOK\b", stdout):
        return {"state": "reachable", "reason": PREFLIGHT_OK_BEFORE_BUDGET_REASON}
    tail = re.sub(r"(?i)(api[_ -]?key|token|password)\s*[:=]\s*\S+", r"\1=[redacted]", stderr[:200])
    return {"state": "unreachable", "reason": f"exit code {completed.returncode}: {tail}"}


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
                           assignments: dict[str, str], *, booleans: bool = False) -> list[str]:
    """Rewrite `key = value` lines of one table in place, keeping comments
    and order, adding the table or missing keys at the end. String values by
    default; `booleans=True` matches and writes bare true/false (#165 #167
    #166, the [features] switches)."""
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

    value_pattern = r"(?:true|false)" if booleans else r"(?:\"(?:[^\"\\]|\\.)*\"|'[^']*')"
    patterns = {
        role: re.compile(
            rf"^(\s*{role}\s*=\s*){value_pattern}(\s*(?:#.*)?)(\r?\n)?$"
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
    if any(value not in AGENT_SETTING_ADAPTERS and value != HOST_AGENT_ADAPTER for value in adapters.values()):
        raise HandsoffError("agent adapters must be exactly 'auto', 'codex', 'claude', or 'host'")
    for role, adapter in adapters.items():
        if adapter == HOST_AGENT_ADAPTER and role not in HOST_CAPABLE_ROLES:
            raise HandsoffError(f"[agents].{role} cannot be host: only supervisor and architect may be host-driven")
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
        if adapter not in AGENT_SETTING_ADAPTERS and adapter != HOST_AGENT_ADAPTER:
            raise HandsoffError(f"profiles.{role}.adapter must be auto, codex, claude, or host")
        if adapter == HOST_AGENT_ADAPTER and role not in HOST_CAPABLE_ROLES:
            raise HandsoffError(f"[agents].{role} cannot be host: only supervisor and architect may be host-driven")
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


# --------------------------------------------------------------------------
# #167: launch rules, distilled from run history
# --------------------------------------------------------------------------

RULE_COMMANDS = ("launch", "packet")
RULE_WHEN_KEYS = {"command", "role", "phase_in", "amendment", "field"}
RULES_DIR = "rules"
PROJECT_RULES_DIR = "handsoff-rules"
MAX_RULE_BYTES = 16 * 1024


class PacketRuleViolation(HandsoffError):
    """A reviewer packet broke a packet rule. `recovered` is the packet with
    the offending field set to the rule's recover_as value (or None when
    the rule names none), so the verdict can be adopted deliberately."""

    def __init__(self, message: str, *, rule_id: str, field: str, value, recovered: dict | None):
        super().__init__(message)
        self.rule_id, self.field, self.value, self.recovered = rule_id, field, value, recovered


def _validate_rule(rule: object, source: str) -> dict:
    if not isinstance(rule, dict):
        raise HandsoffError(f"rule {source}: not an object")
    for key in ("id", "cause", "when", "refuse"):
        if key not in rule:
            raise HandsoffError(f"rule {source}: missing {key}")
    if not isinstance(rule["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", rule["id"]):
        raise HandsoffError(f"rule {source}: id must be a short lowercase slug")
    if not isinstance(rule["refuse"], str) or not rule["refuse"].strip() or len(rule["refuse"]) > 512:
        raise HandsoffError(f"rule {source}: refuse must be 1 to 512 characters")
    cause = rule["cause"]
    if not isinstance(cause, dict) or not isinstance(cause.get("event"), str) or not isinstance(cause.get("at"), str):
        raise HandsoffError(f"rule {source}: cause needs at least event and at")
    when = rule["when"]
    if not isinstance(when, dict) or set(when) - RULE_WHEN_KEYS or when.get("command") not in RULE_COMMANDS:
        raise HandsoffError(f"rule {source}: when.command must be launch or packet and keys limited to "
                            + ", ".join(sorted(RULE_WHEN_KEYS)))
    if "role" in when and when["role"] not in AGENT_ROLES:
        raise HandsoffError(f"rule {source}: when.role must be a managed role")
    if when["command"] == "launch":
        phases = when.get("phase_in")
        if phases is not None and (not isinstance(phases, list) or not phases
                                   or not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 8 for p in phases)):
            raise HandsoffError(f"rule {source}: when.phase_in must be a non-empty list of phase numbers")
        if "amendment" in when and not isinstance(when["amendment"], bool):
            raise HandsoffError(f"rule {source}: when.amendment must be boolean")
        if "field" in when:
            raise HandsoffError(f"rule {source}: when.field belongs to packet rules")
    else:
        if not isinstance(when.get("field"), str) or not when["field"].strip():
            raise HandsoffError(f"rule {source}: a packet rule needs when.field")
        allowed = rule.get("allowed")
        max_chars = rule.get("max_chars")
        if allowed is None and max_chars is None:
            raise HandsoffError(f"rule {source}: a packet rule needs allowed (exact values) or max_chars")
        if allowed is not None and (not isinstance(allowed, list) or not allowed or not all(isinstance(a, str) for a in allowed)):
            raise HandsoffError(f"rule {source}: allowed must be a non-empty list of strings")
        if max_chars is not None and (not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1):
            raise HandsoffError(f"rule {source}: max_chars must be a positive integer")
        if "recover_as" in rule and (allowed is None or rule["recover_as"] not in allowed):
            raise HandsoffError(f"rule {source}: recover_as must be one of allowed")
        if "recover" in rule and rule["recover"] != "truncate":
            raise HandsoffError(f"rule {source}: recover may only be truncate")
        if rule.get("recover") == "truncate" and max_chars is None:
            raise HandsoffError(f"rule {source}: recover truncate needs max_chars")
        for key in ("phase_in", "amendment"):
            if key in when:
                raise HandsoffError(f"rule {source}: when.{key} belongs to launch rules")
    return rule


def load_launch_rules(root: Path | None = None) -> list[dict]:
    """Every rule the engine ships (rules/*.json in the runtime manifest)
    plus a project's own handsoff-rules/*.json. Ids are unique across both;
    rules/proposed/ is never read. A malformed file is an error, never a
    silently skipped rule."""
    rules: list[dict] = []
    seen: set[str] = set()
    directories = [engine_resource_path(RULES_DIR)]
    if root is not None:
        directories.append(Path(root) / PROJECT_RULES_DIR)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stat().st_size > MAX_RULE_BYTES:
                raise HandsoffError(f"rule {path.name}: larger than {MAX_RULE_BYTES} bytes")
            try:
                rule = _validate_rule(json.loads(path.read_text(encoding="utf-8")), path.name)
            except ValueError as exc:
                raise HandsoffError(f"rule {path.name}: invalid JSON ({exc})") from exc
            if rule["id"] in seen:
                raise HandsoffError(f"rule {path.name}: duplicate rule id {rule['id']}")
            seen.add(rule["id"])
            rules.append({**rule, "source": str(path)})
    return rules


def rule_refusal(rule: dict) -> str:
    cause = rule.get("cause", {})
    return f"{rule['refuse']} (rule {rule['id']}, cause: {cause.get('event')} on {cause.get('root', 'a run')} {cause.get('at')})"


def evaluate_launch_rules(root: Path, cfg: dict, *, role: str, phase: int | None, amendment: bool) -> dict | None:
    """The first launch rule matching this launch, or None. With
    features.launch_rules off nothing is evaluated."""
    if not feature_enabled(cfg, "launch_rules"):
        return None
    for rule in load_launch_rules(root):
        when = rule["when"]
        if when["command"] != "launch":
            continue
        if "role" in when and when["role"] != role:
            continue
        if "phase_in" in when and (phase is None or phase not in when["phase_in"]):
            continue
        if "amendment" in when and when["amendment"] != amendment:
            continue
        return rule
    return None


def evaluate_packet_rules(root: Path | None, cfg: dict | None, value: dict, *, role: str = "reviewer") -> None:
    """Raise PacketRuleViolation for the first packet rule the value breaks.
    With features.launch_rules off nothing is evaluated (the caller's own
    validation stays the floor)."""
    if cfg is not None and not feature_enabled(cfg, "launch_rules"):
        return
    for rule in load_launch_rules(root):
        when = rule["when"]
        if when["command"] != "packet" or when.get("role", role) != role:
            continue
        field = when["field"]
        if field not in value:
            continue
        seen = value[field]
        if "max_chars" in rule and rule.get("allowed") is None:
            # a length rule: strings, or every string in a list
            limit = rule["max_chars"]
            items = seen if isinstance(seen, list) else [seen]
            too_long = [i for i in items if isinstance(i, str) and len(i) > limit]
            if not too_long:
                continue
            recovered = None
            if rule.get("recover") == "truncate":
                cut = [(i[:limit - 3] + "...") if isinstance(i, str) and len(i) > limit else i for i in items]
                recovered = {**value, field: cut if isinstance(seen, list) else cut[0]}
            raise PacketRuleViolation(
                f"{rule['refuse']}; {len(too_long)} value(s) over {limit} characters (rule {rule['id']}, cause: "
                f"{rule['cause'].get('event')} on {rule['cause'].get('root', 'a run')} {rule['cause'].get('at')})",
                rule_id=rule["id"], field=field, value=f"{len(too_long)} over {limit}", recovered=recovered)
        if isinstance(seen, str) and seen in rule["allowed"]:
            continue
        recovered = None
        if "recover_as" in rule:
            recovered = {**value, field: rule["recover_as"]}
        shown = seen if isinstance(seen, str) else type(seen).__name__
        raise PacketRuleViolation(
            f"{rule['refuse']}; got {json.dumps(shown)[:120]} (rule {rule['id']}, cause: "
            f"{rule['cause'].get('event')} on {rule['cause'].get('root', 'a run')} {rule['cause'].get('at')})",
            rule_id=rule["id"], field=field, value=seen, recovered=recovered)


# --------------------------------------------------------------------------
# #170: the rules set a review ran under
# --------------------------------------------------------------------------

#: Project files that decide what a reviewer saw and could run. Contents
#: are hashed, never stored; .env and credential files are never in the set.
RULES_SET_PROJECT_FILES = ("handsoff.toml", ".claude/settings.json", ".claude/settings.local.json",
                           ".codex/config.toml", "AGENTS.md", "CLAUDE.md")


def rules_set_entries(root: Path) -> dict[str, str | None]:
    """Path -> sha256 of contents, or None when absent. Engine entries
    (reviewer prompt, rules/*.json, manifest version) are keyed 'engine:'."""
    root = Path(root).resolve()
    entries: dict[str, str | None] = {}
    for relative in RULES_SET_PROJECT_FILES:
        path = root / relative
        try:
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            entries[relative] = None
    prompt = engine_resource_path("prompts/reviewer.md")
    try:
        entries["engine:prompts/reviewer.md"] = hashlib.sha256(prompt.read_bytes()).hexdigest() if prompt.is_file() else None
    except OSError:
        entries["engine:prompts/reviewer.md"] = None
    for rule in load_launch_rules(root):
        try:
            entries[f"engine:{Path(rule['source']).name}" if "/rules/" in rule["source"].replace(str(root), "")
                    else f"project:{Path(rule['source']).name}"] = hashlib.sha256(Path(rule["source"]).read_bytes()).hexdigest()
        except OSError:
            continue
    try:
        entries["engine:version"] = json.loads((engine_root() / RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        entries["engine:version"] = None
    return entries


def rules_set_hash(root: Path, cfg: dict | None = None) -> str:
    return hashlib.sha256(_canonical(rules_set_entries(root)).encode("utf-8")).hexdigest()


def rules_set_diff(root: Path, recorded_entries: dict | None) -> list[str]:
    """Which entries differ from a recorded snapshot; every entry when no
    snapshot was recorded (an older decision carries the hash only)."""
    current = rules_set_entries(root)
    if not isinstance(recorded_entries, dict):
        return sorted(current)
    changed = [key for key in sorted(set(current) | set(recorded_entries))
               if current.get(key) != recorded_entries.get(key)]
    return changed


def rules_binding(root: Path, cfg: dict) -> dict:
    """What a decision records: the hash and the entries behind it."""
    entries = rules_set_entries(root)
    return {"rules_hash": hashlib.sha256(_canonical(entries).encode("utf-8")).hexdigest(),
            "rules_entries": entries}


def rules_binding_errors(root: Path | None, cfg: dict, decision: dict | None, label: str) -> list[str]:
    """#170: refuse when the rules set changed since `decision` was recorded.
    A decision without rules_hash (recorded before the field existed) is
    accepted as it stands. With the switch off nothing is checked."""
    if root is None or not isinstance(decision, dict) or not feature_enabled(cfg, "review_binds_rules"):
        return []
    recorded = decision.get("rules_hash")
    if not recorded:
        return []
    if recorded == rules_set_hash(root, cfg):
        return []
    changed = rules_set_diff(root, decision.get("rules_entries"))
    shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
    return [f"{label}: the rules set changed since it was recorded ({shown}); record it again"]


def update_feature_settings(root: Path, payload: object) -> dict:
    """Atomically persist the [features] switches (#165 #167 #166). The
    payload is exactly {name: bool} for every known feature; nothing else
    is written and the proposed file is re-parsed before it replaces the
    old one."""
    if not isinstance(payload, dict) or set(payload) != set(FEATURES):
        raise HandsoffError("feature settings must contain exactly: " + ", ".join(FEATURES))
    for name, value in payload.items():
        if not isinstance(value, bool):
            raise HandsoffError(f"features.{name} must be a literal boolean")
    root = root.resolve()
    path = root / "handsoff.toml"
    if tomllib is None:
        raise HandsoffError("feature settings require Python 3.11+ TOML support")
    with project_lock(root):
        try:
            original = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(original)
            load_config(root)
        except (OSError, ValueError) as exc:
            raise HandsoffError(f"cannot update {path}: {exc}") from exc
        lines = _patch_toml_role_table(original.splitlines(keepends=True), parsed, "features",
                                       dict(payload), booleans=True)
        proposed = "".join(lines)
        try:
            proposed_raw = tomllib.loads(proposed)
        except ValueError as exc:
            raise HandsoffError(f"refusing invalid proposed handsoff.toml: {exc}") from exc
        if proposed_raw.get("features") != payload:
            raise HandsoffError("refusing ambiguous feature settings update")
        _atomic_write_text(path, proposed)
    return {"features": dict(payload)}


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
    except Exception:
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
    # A scoped amendment deliberately freezes both phase and progress.
    # Recomputing item progress while its changed criteria are temporarily
    # reset would make the just-written amendment contradict its own frozen
    # snapshot and render an otherwise valid run invalid.
    preserve_progress = bool(status is not None and status.pop("_preserve_progress", False))
    if status is not None and "work_item_delivery" in status and open_amendment(status) is None and not preserve_progress:
        progress_acceptance = acceptance
        if progress_acceptance is None and acceptance_path(root, cfg).is_file():
            progress_acceptance = load_unique_json(acceptance_path(root, cfg))
        if isinstance(progress_acceptance, dict):
            # The derived value only ever raises progress here: an operator
            # value written by advance (#100) is never fought by bookkeeping,
            # and rollbacks set their own lower value explicitly.
            derived = overall_item_progress(status, progress_acceptance, cfg)
            status["progress"] = max(int(status.get("progress", 0) or 0), derived)
    if status is not None and preserve_progress and "progress" in event_extra:
        status["progress"] = event_extra["progress"]
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
        # the criterion specs this attempt judged; record-review --reaffirm
        # re-binds only while these are unchanged
        "design_hash": design_hash(acceptance.get("criteria", [])),
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
                         tier_reason: str | None = None,
                         amendment_id: str | None = None) -> dict:
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
        if amendment_id is not None:
            # #142: a reviewer launched FOR an open amendment reviews the
            # amendment, not the phase. It opens no review attempt and
            # spends no budget; the broker dispatches its verdict as
            # amendment-review. The id must name the amendment that is open
            # right now, or the launch is refused before a session exists.
            if role != "reviewer":
                raise HandsoffError("--amendment applies to reviewer sessions only")
            open_record = open_amendment(status)
            if not open_record or open_record.get("amendment_id") != amendment_id:
                raise HandsoffError(
                    f"reviewer launch refused: no open amendment {amendment_id}")
        if role == "reviewer" and amendment_id is None \
                and int(proposed.get("phase_number", 0) or 0) >= 4:
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
            "amendment_id": amendment_id,
            "tier": tier,
            "phase_number": int(status.get("phase_number", 1) or 1),
            "host_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_COMPANION_SESSION_ID"),
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
        if tier is not None:
            # #145: the selection is also PERSISTED on status, in this same
            # commit, so design_reviewer_selection_view has a record to check
            # the live session against. Before this the view read a key that
            # nothing wrote and reported "no selection metadata" on every
            # live design review.
            proposed["design_reviewer_selection"] = {"current": {
                "actor": actor, "session_id": session_id, "adapter": adapter,
                "model": requested_model, "tier": tier, "reason": tier_reason,
                "attempt": budget["next_attempt"] if budget else None,
                "selected_at": now,
            }}
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
            engine=ledger_engine_identity(root),
            session_id=session_id, role=role, actor=actor, adapter=adapter,
            requested_model=requested_model, reported_model=None,
            resolution_source=resolution_source, state="launching",
            implementer_binding={"adapter": binding["adapter"], "model": binding["model"]}
            if binding else None,
            design_review_attempt=budget["next_attempt"] if budget else None,
            design_review_authorization_reserved=reservation is not None,
            packet_id=packet_id, design_hash=design_hash, tier=tier,
            # #167: whether the launch rules stood between this launch and
            # the process; "disabled" is the project's own switch.
            launch_rules="evaluated" if feature_enabled(cfg, "launch_rules") else "disabled",
        )
        return deepcopy(session)


def _validate_failure_classification(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= {"category", "reason", "tail_sha256", "dependency", "operation", "changed_paths", "result_available", "adopted"} \
            or not {"category", "reason", "tail_sha256"} <= set(value):
        raise HandsoffError("agent failure classification is invalid")
    category = value.get("category")
    if category not in FAILURE_CATEGORIES or (category != "dispatch_failed" and value.get("reason") != _FAILURE_REASON_LABELS.get(category)):
        raise HandsoffError("agent failure classification is not from the closed set")
    if category == "dispatch_failed" and (not isinstance(value.get("reason"), str) or not value["reason"].strip() or len(value["reason"]) > 200):
        raise HandsoffError("dispatch failure reason must be 1 to 200 characters")
    digest = value.get("tail_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HandsoffError("agent failure classification digest is invalid")
    result = {"category": category, "reason": value["reason"], "tail_sha256": digest}
    for key in ("dependency", "operation"):
        if key in value:
            if not isinstance(value[key], str) or not OPERATION_IDENTIFIER_PATTERN.fullmatch(value[key]):
                raise HandsoffError("agent failure operation identifier is invalid")
            result[key] = value[key]
    if "changed_paths" in value:
        if not isinstance(value["changed_paths"], list) or len(value["changed_paths"]) > 64 or not all(isinstance(item, str) for item in value["changed_paths"]):
            raise HandsoffError("agent failure changed paths are invalid")
        result["changed_paths"] = list(value["changed_paths"])
    if "adopted" in value:
        if value["adopted"] is not True:
            raise HandsoffError("agent failure adopted flag is invalid")
        result["adopted"] = True
    if "result_available" in value:
        if value["result_available"] is not True:
            raise HandsoffError("agent failure result_available is invalid")
        result["result_available"] = True
    return result


# --------------------------------------------------------------------------
# #168: usage, from the adapter's own words
# --------------------------------------------------------------------------

USAGE_SOURCES = ("adapter", "not reported", "disabled")
_CODEX_TOKENS_LINE = re.compile(r"^\s*tokens used\s*:?\s*([0-9][0-9,]*)?\s*$", re.IGNORECASE)
_NUMBER_LINE = re.compile(r"^\s*([0-9][0-9,]*)\s*$")


class UsageWatcher:
    """Watches every streamed output line (stdout and stderr alike) and keeps
    the LAST usage the adapter printed, independent of the bounded tails.
    Codex prints 'tokens used' and the number on the next line (or the
    same line); Claude's stream-json carries usage.input_tokens and
    usage.output_tokens on its events. Nothing is estimated."""

    def __init__(self, adapter: str | None = None):
        self.adapter = adapter
        self.usage: dict | None = None
        self._awaiting_number = False

    def feed(self, line: str) -> None:
        text = line.rstrip("\r\n")
        if self._awaiting_number:
            self._awaiting_number = False
            number = _NUMBER_LINE.match(text)
            if number:
                self._set(total=int(number.group(1).replace(",", "")))
                return
        match = _CODEX_TOKENS_LINE.match(text)
        if match:
            if match.group(1):
                self._set(total=int(match.group(1).replace(",", "")))
            else:
                self._awaiting_number = True
            return
        if text.startswith("{"):
            try:
                event = json.loads(text)
            except ValueError:
                return
            usage = _find_usage(event)
            if usage:
                self._set(tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"))

    def _set(self, *, total: int | None = None, tokens_in: int | None = None, tokens_out: int | None = None) -> None:
        if total is None and tokens_in is None and tokens_out is None:
            return
        if total is None and (tokens_in is not None or tokens_out is not None):
            total = int(tokens_in or 0) + int(tokens_out or 0)
        self.usage = {"tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_total": total, "source": "adapter"}

    def result(self, enabled: bool = True) -> dict:
        if not enabled:
            return {"tokens_in": None, "tokens_out": None, "tokens_total": None, "source": "disabled"}
        return dict(self.usage) if self.usage else {"tokens_in": None, "tokens_out": None, "tokens_total": None, "source": "not reported"}


def _find_usage(value) -> dict | None:
    """The deepest usage object with integer input/output token counts."""
    found = None
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict) and any(isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)
                                           for k in ("input_tokens", "output_tokens")):
            found = {k: usage.get(k) for k in ("input_tokens", "output_tokens")
                     if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)}
        for child in value.values():
            nested = _find_usage(child)
            if nested:
                found = nested
    elif isinstance(value, list):
        for child in value:
            nested = _find_usage(child)
            if nested:
                found = nested
    return found


def validate_usage(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"tokens_in", "tokens_out", "tokens_total", "source"}:
        raise HandsoffError("session usage must carry tokens_in, tokens_out, tokens_total and source")
    if value["source"] not in USAGE_SOURCES:
        raise HandsoffError("session usage source is invalid")
    for key in ("tokens_in", "tokens_out", "tokens_total"):
        number = value[key]
        if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 0):
            raise HandsoffError(f"session usage {key} must be a non-negative integer or null")
    return dict(value)


def usage_totals(status: dict) -> dict:
    """#168: what the run cost so far, from recorded session usage only."""
    by_role: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    total = 0
    reported = not_reported = 0
    for session in (status.get("agent_sessions") or {}).values():
        if not isinstance(session, dict):
            continue
        usage = session.get("usage")
        if not isinstance(usage, dict) or usage.get("source") != "adapter" or not isinstance(usage.get("tokens_total"), int):
            if session.get("state") in AGENT_SESSION_TERMINAL_STATES:
                not_reported += 1
            continue
        reported += 1
        total += usage["tokens_total"]
        role = str(session.get("role") or "unknown")
        phase = str(session.get("phase_number") or "unknown")
        by_role[role] = by_role.get(role, 0) + usage["tokens_total"]
        by_phase[phase] = by_phase.get(phase, 0) + usage["tokens_total"]
    return {"tokens_total": total, "by_role": by_role, "by_phase": by_phase,
            "sessions_reported": reported, "sessions_not_reported": not_reported}


def transition_agent_session(root: Path, session_id: str, state: str,
                             *, exit_code: int | None = None,
                             failure: dict | None = None, usage: dict | None = None) -> dict:
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
    if usage is not None:
        usage = validate_usage(usage)
        if not terminal:
            raise HandsoffError("session usage is recorded on the terminal transition only")
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
            if role == "reviewer":
                warnings = proposed.setdefault("warnings", [])
                if "a reviewer is live; do not edit the tree" not in warnings:
                    warnings.append("a reviewer is live; do not edit the tree")
            if replacement is not None:
                replacement["state"] = "running"
                replacement["running_at"] = now
                replacement["handoff"]["state"] = "running"
                replacement["handoff"]["running_at"] = now
        else:
            updated["ended_at"] = now
            updated["exit_code"] = exit_code
            if usage is not None:
                updated["usage"] = usage  # #168
            if replacement is not None:
                replacement_state = "recovered" if state == "completed" else "failed"
                replacement["state"] = replacement_state
                replacement["ended_at"] = now
                replacement["handoff"]["state"] = replacement_state
                replacement["handoff"]["ended_at"] = now
            if failure is not None:
                failures = proposed.setdefault("agent_failures", {})
                failures[session_id] = {"session_id": session_id, **failure, "at": now}
            if role == "reviewer":
                warnings = proposed.setdefault("warnings", [])
                warnings[:] = [item for item in warnings
                                if item != "a reviewer is live; do not edit the tree"]
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
            failure_dependency=failure.get("dependency") if failure else None,
            failure_operation=failure.get("operation") if failure else None,
            replacement_id=replacement.get("replacement_id") if replacement else None,
            replacement_state=replacement.get("state") if replacement else None,
            usage=usage,
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
            stderr = getattr(exc, "stderr", "") or ""
            if "unborn branch" in stderr.lower() or "ambiguous argument 'head'" in stderr.lower():
                raise HandsoffError("non-git root") from None
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
            "phase_number": int(status.get("phase_number", 1) or 1),
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
                        commands: list[str] | None = None,
                        description: str | None = None,
                        acceptance_digest: str | None = None,
                        config_digest: str | None = None,
                        binding: dict | None = None, executed: bool = True,
                        reused_from: str | None = None,
                        feature_hash: str | None = None,
                        repository_digest: str | None = None,
                        attempts: list[dict] | None = None,
                        rules: dict | None = None) -> dict:
    """Append a hash-chained evidence record. Caller must hold project_lock.

    #43: `binding` (command to verification_binding hash), `executed`,
    `reused_from`, and `feature_hash` are part of the hashed record so a
    reused record cannot later be passed off as an executed one. Legacy
    records written before these fields existed simply lack them and are
    never reuse sources.

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
    if binding is not None and not isinstance(binding, dict):
        raise HandsoffError("verification record: 'binding' must be an object or null")
    if not isinstance(executed, bool):
        raise HandsoffError("verification record: 'executed' must be a boolean")
    if reused_from is not None and (not isinstance(reused_from, str) or not reused_from.strip()):
        raise HandsoffError("verification record: 'reused_from' must be a run id or null")
    if executed and reused_from is not None:
        raise HandsoffError("verification record: an executed record cannot name a reuse source")
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
        "commands": list(commands or []),
        "description": description or "",
        "acceptance_hash": acceptance_digest,
        "config_hash": config_digest,
        "binding": binding,
        "executed": executed,
        "reused_from": reused_from,
        "feature_hash": feature_hash,
        "repository_digest": repository_digest,
        "prev_hash": prev_hash,
    }
    if attempts is not None:
        record["attempts"] = attempts  # #169: only a repeat record carries it
    if rules is not None:
        record["rules_hash"] = rules["rules_hash"]  # #170: a live record binds its rules set
        record["rules_entries"] = rules["rules_entries"]
    record["hash"] = hashlib.sha256((_canonical(record) + prev_hash).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def evidence_drift(root: Path, cfg: dict, acceptance: dict,
                   verifications: list[dict]) -> dict:
    """Classify newest valid automated evidence against one cached digest.

    Legacy records remain unknown rather than stale, so adding this integrity
    check cannot unexpectedly invalidate an existing run.
    """
    current_digest = repository_digest(root, cfg)
    current_config = verification_config_hash(cfg)
    result = {"current_digest": current_digest, "current": [], "stale": [],
              "unknown": [], "refresh_commands": [], "changed_paths": [],
              "changed_paths_truncated": False, "changed_paths_note": None}
    current_entries = repository_digest_entries(root, cfg)
    for criterion in acceptance.get("criteria", []):
        cid = criterion.get("id")
        if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
            continue
        record = next((candidate for candidate in reversed(verifications)
                       if candidate.get("kind") == "checks"
                       and candidate.get("ok") is True
                       and cid in candidate.get("criteria", [])
                       and candidate.get("criterion_hashes", {}).get(cid) == criterion_spec_hash(criterion)), None)
        if record is None:
            continue
        digest = record.get("repository_digest")
        recorded_config = record.get("config_hash")
        if digest is None:
            result["unknown"].append(cid)
            result["changed_paths"] = None
            result["changed_paths_note"] = "snapshot not recorded"
        elif digest == current_digest and (recorded_config is None or recorded_config == current_config):
            result["current"].append(cid)
        else:
            result["stale"].append(cid)
            snapshot_path = root / ".handsoff-digests" / f"{digest}.json"
            if snapshot_path.is_file():
                try:
                    old_entries = load_unique_json(snapshot_path).get("entries", {})
                    changed = sorted({*old_entries, *current_entries} - {
                        path for path in set(old_entries) & set(current_entries)
                        if old_entries[path] == current_entries[path]
                    })
                    if len(changed) > 32:
                        result["changed_paths_truncated"] = True
                    result["changed_paths"] = changed[:32]
                except (OSError, HandsoffError, ValueError):
                    result["changed_paths_note"] = "snapshot not recorded"
            else:
                result["changed_paths_note"] = "snapshot not recorded"
            result["refresh_commands"].append(
                f"handsoff_supervisor.py verify --criterion {cid} --by ACTOR")
    return result


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
        # #43 fields: absent on legacy records (tolerated, never reusable),
        # typed when present so a hand-edited "executed": "yes" or a reuse
        # source on an executed record is refused rather than half-trusted.
        if "executed" in record and not isinstance(record.get("executed"), bool):
            problems.append(f"verification line {lineno}: 'executed' must be a boolean")
        if record.get("binding") is not None and not isinstance(record.get("binding"), dict):
            problems.append(f"verification line {lineno}: 'binding' must be an object or null")
        reused_from = record.get("reused_from")
        if reused_from is not None and (not isinstance(reused_from, str) or not reused_from.strip()):
            problems.append(f"verification line {lineno}: 'reused_from' must be a run id or null")
        if record.get("executed") is True and reused_from is not None:
            problems.append(f"verification line {lineno}: an executed record cannot name a reuse source")
        if record.get("feature_hash") is not None and not isinstance(record.get("feature_hash"), str):
            problems.append(f"verification line {lineno}: 'feature_hash' must be a string or null")
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
        if set(entry) - set(DESIGN_REVIEW_HISTORY_FIELDS) - {"proposal_hash"} or \
                set(DESIGN_REVIEW_HISTORY_FIELDS) - set(entry):
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
    heartbeat_owner = status.get("last_heartbeat_owner")
    if heartbeat_owner is not None and (not isinstance(heartbeat_owner, str) or not heartbeat_owner.strip()):
        errors.append("status: 'last_heartbeat_owner' must be a non-empty string or null")
    background_wait = status.get("background_wait")
    if background_wait is not None and (not isinstance(background_wait, dict)
                                        or not isinstance(background_wait.get("since"), str)
                                        or not isinstance(background_wait.get("by"), str)):
        errors.append("status: 'background_wait' must be an owned wait record or null")
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
            if "proposal_hash" in design_approved and design_approved["proposal_hash"] is not None and (not isinstance(design_approved["proposal_hash"], str)
                                                                                                          or not design_approved["proposal_hash"].strip()):
                errors.append("status: 'design_approved.proposal_hash' must be a non-empty string")
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
            if "proposal_hash" in design_review and design_review["proposal_hash"] is not None and (not isinstance(design_review["proposal_hash"], str)
                                                                                                      or not design_review["proposal_hash"].strip()):
                errors.append("status: 'design_review.proposal_hash' must be a non-empty string")
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
    proposal = status.get("design_proposal")
    if isinstance(proposal, dict) and "provenance" in proposal:
        provenance = proposal["provenance"]
        expected = {"actor", "pid", "executable", "host_session_id", "recorded_at"}
        if not isinstance(provenance, dict) or set(provenance) != expected:
            errors.append("status: 'design_proposal.provenance' has invalid fields")
        else:
            if not isinstance(provenance["actor"], str) or not provenance["actor"].strip():
                errors.append("status: 'design_proposal.provenance.actor' must be a non-empty string")
            if not isinstance(provenance["pid"], int) or isinstance(provenance["pid"], bool) or provenance["pid"] < 1:
                errors.append("status: 'design_proposal.provenance.pid' must be a positive integer")
            if not isinstance(provenance["executable"], str) or not provenance["executable"].strip():
                errors.append("status: 'design_proposal.provenance.executable' must be a non-empty string")
            if provenance["host_session_id"] is not None and (not isinstance(provenance["host_session_id"], str)
                                                               or not provenance["host_session_id"].strip()):
                errors.append("status: 'design_proposal.provenance.host_session_id' must be a string or null")
            try:
                if datetime.fromisoformat(provenance["recorded_at"]).tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("status: 'design_proposal.provenance.recorded_at' must be timezone-aware")
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
            if not isinstance(attempt, dict) or not required.issubset(attempt) \
                    or set(attempt) - required - {"tests_executed", "adopted_by", "adopted_session", "design_hash", "reaffirmed_at"}:
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
            if attempt.get("tests_executed", "unknown") not in {"yes", "no", "unknown"}:
                errors.append(f"{label}.tests_executed is invalid")
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
                    or not isinstance(item.get("cap"), int) \
                    or not isinstance(item.get("attempt"), int) \
                    or isinstance(item.get("attempt"), bool) \
                    or not 1 <= item.get("attempt") <= item.get("cap") \
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
    errors.extend(validate_release_plan(status.get("release_plan")))
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
            optional = {"release_version", "release_class", "policy_override_reason"}
            if not isinstance(item, dict) or not required <= set(item) or set(item) - required - optional:
                errors.append(f"{label} has invalid fields")
                continue
            if "release_version" in item:
                try:
                    normalized, release_class = classify_release_version(item.get("release_version"))
                    if normalized != item.get("release_version") or release_class != item.get("release_class"):
                        errors.append(f"{label} release version/class mismatch")
                except HandsoffError:
                    errors.append(f"{label}.release_version is invalid")
                override = item.get("policy_override_reason")
                if override is not None and (not isinstance(override, str) or not override.strip()):
                    errors.append(f"{label}.policy_override_reason is invalid")
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
                if optional_field == "result":
                    continue
                if optional_field == "usage":
                    if value is not None:
                        try:
                            validate_usage(value)
                        except HandsoffError as exc:
                            errors.append(f"{label}.usage: {exc}")
                    continue
                if optional_field == "phase_number":
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                              or value not in PHASES):
                        errors.append(f"{label}.phase_number must be null or an integer from 1 through 8")
                    continue
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
            result = session.get("result")
            if result is not None:
                required = {"kind", "payload", "recorded_at", "adopted_at", "adopted_by"}
                # #167: a packet a rule refused keeps its raw text and the mark
                optional = {"readoptions", "refused_text", "recovered_from_rule"}
                if not isinstance(result, dict) or set(result) - optional != required or result.get("kind") not in {"review", "design", "supervisor_request"} or not isinstance(result.get("payload"), dict) or ((result.get("adopted_at") is None) != (result.get("adopted_by") is None)) \
                        or ("readoptions" in result and (not isinstance(result["readoptions"], list) or any(not isinstance(r, dict) or set(r) != {"at", "by"} for r in result["readoptions"]))) \
                        or ("refused_text" in result and (not isinstance(result["refused_text"], str) or len(result["refused_text"]) > 65536)) \
                        or ("recovered_from_rule" in result and not isinstance(result["recovered_from_rule"], bool)):
                    errors.append(f"{label}.result is invalid")
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
                        key: failure.get(key) for key in ("category", "reason", "tail_sha256", "dependency", "operation")
                        if isinstance(failure, dict) and key in failure
                    }) if isinstance(failure, dict) else None
                except HandsoffError as exc:
                    errors.append(f"status: agent failure {session_id!r}: {exc}")
                    normalized = None
                allowed_fields = {"session_id", "category", "reason", "tail_sha256", "at", "dependency", "operation", "changed_paths",
                                  "result_available", "adopted", "scratch_path", "auto_retry_authorized", "acknowledged"}
                if isinstance(failure, dict) and "auto_retry_authorized" in failure and failure["auto_retry_authorized"] is not True:
                    errors.append(f"status: agent failure {session_id!r} auto_retry_authorized must be true when present")
                if not isinstance(failure, dict) or not {"session_id", "category", "reason", "tail_sha256", "at"} <= set(failure) \
                        or set(failure) - allowed_fields \
                        or failure.get("session_id") != session_id:
                    errors.append(f"status: agent failure {session_id!r} has invalid fields")
                if isinstance(failure, dict) and (not isinstance(failure.get("changed_paths", []), list) or len(failure.get("changed_paths", [])) > 64):
                    errors.append(f"{label} changed_paths is invalid")
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
    delivery = status.get("work_item_delivery")
    if delivery is not None:
        fields = {"lane", "requested_lane", "confirmed_by", "confirmed_at", "facts",
                  "escalation_reason", "implemented_by", "reviewed_by", "review_hash",
                  "baseline_head"}
        if not isinstance(delivery, dict) or len(delivery) > MAX_WORK_ITEMS:
            errors.append("status: 'work_item_delivery' must be an object of at most 64 items")
        else:
            for item_id, record in delivery.items():
                if not WORK_ITEM_ID_PATTERN.fullmatch(str(item_id)) or not isinstance(record, dict) \
                        or set(record) != fields:
                    errors.append(f"status: work item delivery {item_id!r} has invalid fields")
                    continue
                if record.get("lane") not in WORK_ITEM_LANES \
                        or record.get("requested_lane") not in {"full", "small-fix"}:
                    errors.append(f"status: work item delivery {item_id!r} has invalid lane")
                for field in ("confirmed_by", "confirmed_at", "escalation_reason", "implemented_by",
                              "reviewed_by", "review_hash", "baseline_head"):
                    if record.get(field) is not None and not isinstance(record.get(field), str):
                        errors.append(f"status: work item delivery {item_id!r}.{field} must be a string or null")
                if record.get("facts") is not None and not isinstance(record.get("facts"), dict):
                    errors.append(f"status: work item delivery {item_id!r}.facts must be an object or null")
    tranche_approval = status.get("tranche_approval")
    if tranche_approval is not None:
        required = {"proposal_hash", "proposed_order", "approved_order", "drops", "labels", "by", "at"}
        if not isinstance(tranche_approval, dict) or set(tranche_approval) != required:
            errors.append("status: 'tranche_approval' has invalid fields")
        else:
            proposed = tranche_approval.get("proposed_order")
            approved = tranche_approval.get("approved_order")
            drops = tranche_approval.get("drops")
            labels = tranche_approval.get("labels")
            if not re.fullmatch(r"[0-9a-f]{64}", str(tranche_approval.get("proposal_hash", ""))):
                errors.append("status: tranche_approval.proposal_hash must be sha256")
            if not all(isinstance(values, list) and len(values) <= MAX_WORK_ITEMS
                       and len(values) == len(set(values))
                       and all(WORK_ITEM_ID_PATTERN.fullmatch(str(value)) for value in values)
                       for values in (proposed, approved, drops)):
                errors.append("status: tranche approval item lists are invalid")
            elif set(approved) & set(drops) or set(approved) | set(drops) != set(proposed):
                errors.append("status: tranche approval order and drops must partition the proposal")
            try:
                validate_agent_actor(tranche_approval.get("by"))
                datetime.fromisoformat(tranche_approval.get("at"))
            except (HandsoffError, TypeError, ValueError):
                errors.append("status: tranche approval actor or timestamp is invalid")
            if not isinstance(labels, dict) or set(labels) != set(approved) \
                    or any(not isinstance(value, list) or len(value) > 32
                           or not all(isinstance(label, str) and len(label) <= 64 for label in value)
                           for value in labels.values()):
                errors.append("status: tranche approval labels are invalid")
    # #42: the open amendment and its closed history, plus the amended_by
    # trail on the design decisions it rewrote.
    errors.extend(amendment_status_errors(status))
    errors.extend(question_status_errors(status))
    return errors


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

#: [features] switches: name -> (default, one line for the settings dialog).
#: failing_first is off by default because turning it on refuses every
#: project's next Phase 6 until baselines exist; the other two only refuse
#: what was already a mistake.
FEATURES = {
    "failing_first": (False, "A criterion's test must be seen to fail before the feature; Phase 6 and Phase 8 refuse a pass with no recorded failing run behind it (#165)."),
    "launch_rules": (True, "Rules distilled from run history are evaluated before a managed launch and at the reviewer packet boundary; a match refuses with the rule's cause (#167)."),
    "ticket_lock": (True, "init refuses a ticket that another live registered run already owns; --adopt takes over only a dead or closed owner (#166)."),
    "token_accounting": (True, "The usage each adapter prints is recorded on its session and summed by role, phase and ticket; nothing is estimated (#168)."),
    "review_binds_rules": (True, "A review, design approval and deployment approval carry the hash of the rules set they ran under; a changed hook or config revokes them (#170)."),
    "report_posting": (False, "Phase 8 completion posts the ledger's report to each ticket and closes it; off, nothing leaves the machine unless run-close --post says so (#171)."),
}


def feature_enabled(cfg: dict, name: str) -> bool:
    if name not in FEATURES:
        raise HandsoffError(f"unknown workflow feature: {name}")
    return bool((cfg or {}).get("features", {}).get(name, FEATURES[name][0]))


def features_view(cfg: dict) -> dict:
    """Effective switches with their defaults and descriptions, for the
    status snapshot and the settings dialog."""
    return {name: {"enabled": feature_enabled(cfg, name), "default": default, "description": text}
            for name, (default, text) in FEATURES.items()}


GOVERNANCE_CONFIG_KEYS = (
    "deployment_requires_explicit_approval", "require_live_verification",
    "max_design_rounds", "max_review_rounds", "stall_minutes",
    "max_autonomous_design_reviews", "small_fix_max_criteria",
    "small_fix_max_changed_lines", "small_fix_max_files",
    "require_design_approval",
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
    "small_fix_max_criteria": DEFAULT_SMALL_FIX_MAX_CRITERIA,
    "small_fix_max_changed_lines": DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    "small_fix_max_files": DEFAULT_SMALL_FIX_MAX_FILES,
    "require_design_approval": True,  # #159
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
    # [features] switches bind the same way: a switch at its default keeps
    # every recorded hash byte for byte; a flipped one revokes what was
    # granted under the other setting.
    for name, (default, _text) in FEATURES.items():
        if feature_enabled(cfg, name) != default:
            bound["features." + name] = feature_enabled(cfg, name)
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


def reviewer_launch_evidence_gaps(criteria: list[dict], verifications: list[dict]) -> list[str]:
    """Field-note defect 4: the Phase 5 reviewer launch pre-check names every
    evidence kind a criterion's policy still lacks, read from the ledger, in
    the exact command that supplies it. An automated_and_browser criterion
    with only its checks half is a gap; a fully evidenced registry is none."""
    gaps: list[str] = []
    for criterion in criteria:
        cid = criterion.get("id")
        required = VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set())
        present = valid_evidence_kinds(criterion, verifications)
        for kind in sorted(required - present):
            if kind == "checks":
                gaps.append(f"{cid}: run handsoff_supervisor.py verify --criterion {cid} --by ACTOR")
            else:
                gaps.append(f"{cid}: run handsoff_supervisor.py record-evidence {cid} --kind {kind} --description ... --by ACTOR")
    return gaps


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


def criterion_baseline(criterion: dict, verifications: list[dict]) -> dict | None:
    """#165: the newest VALID baseline (kind baseline, ok True) bound to this
    criterion's current spec hash, or None. A baseline recorded against an
    older wording of the criterion does not count: the claim changed."""
    cid = criterion.get("id")
    spec = criterion_spec_hash(criterion)
    found = None
    for record in verifications:
        if (isinstance(record, dict) and record.get("kind") == "baseline" and record.get("ok") is True
                and cid in record.get("criteria", []) and record.get("criterion_hashes", {}).get(cid) == spec):
            found = record
    return found


def baseline_errors(criteria: list[dict], verifications: list[dict], cfg: dict) -> list[str]:
    """#165: with features.failing_first on, every automated criterion that
    reads passing must have a valid failing run behind it, unless it says
    baseline = not_applicable with a reason. Off: nothing is asked."""
    if not feature_enabled(cfg, "failing_first"):
        return []
    errors: list[str] = []
    for criterion in criteria:
        if criterion.get("state") != "passing":
            continue
        if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
            continue
        cid = criterion.get("id")
        if criterion.get("baseline") == BASELINE_NOT_APPLICABLE:
            if not str(criterion.get("baseline_reason") or "").strip():
                errors.append(f"baseline gate: {cid} says baseline not_applicable without a reason")
            continue
        if criterion_baseline(criterion, verifications) is None:
            errors.append(f"baseline gate: {cid} passed without a recorded failing run; run handsoff_supervisor.py "
                          f"verify --criterion {cid} --expect-fail --by ACTOR on the tree before the feature, "
                          f"or mark it --baseline not_applicable --baseline-reason TEXT")
    return errors


def _review_errors(status: dict, acceptance: dict, cfg: dict, root: Path | None = None) -> list[str]:
    errors: list[str] = []
    review = status.get("review")
    if not isinstance(review, dict):
        return ["review gate: Phase 6+ requires a recorded independent review"]
    if review.get("acceptance_hash") != acceptance_hash(acceptance.get("criteria", [])):
        errors.append("review gate: acceptance changed since review; record a new review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("review gate: workflow policy changed since review; record a new review")
    errors.extend(rules_binding_errors(root, cfg, review, "review gate"))  # #170
    if "work_items" in acceptance and not scope_hash_matches(review.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
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


def operation_inventory(status: dict, acceptance: dict, cfg: dict, root: Path,
                        action_binding: str) -> list[dict]:
    """Return every Pilot-facing operation, including blocked previews.

    The dashboard used to show only the next permitted buttons.  That made a
    missing button ambiguous: unavailable, already satisfied, or simply not
    implemented.  This table makes that distinction explicit while keeping
    action ids bound to the same state hash used by the legacy endpoint.
    """
    specs = [
        ("design_approve", "design-approve", "Authorize the reviewed design", "primary", False),
        ("design_reject", "design-reject", "Request a design revision", "danger", True),
        ("deployment_approve", "deployment-gate", "Authorize live verification", "primary", False),
        ("deployment_revoke", "deployment-gate", "Revoke deployment approval", "danger", True),
        ("deployment_hold", "human-pause-start", "Hold deployment", "danger", True),
        ("design_review_authorize", "design-review-authorize", "Permit one more design review", "primary", False),
        ("design_review_escalate", "design-review-escalate", "Escalate the reviewer tier", "primary", True),
        ("review_cap_override", "review-cap-override", "Extend the implementation review cap once", "primary", True),
        ("recovery_acknowledge", "recovery-acknowledge", "Clear the recovery hold", "primary", True),
        ("recover", "recover", "Retry one bounded recovery attempt", "primary", True),
        ("pause", "human-pause-start", "Pause the mission", "muted", True),
        ("resume", "human-pause-end", "Resume the mission", "primary", False),
        ("run_close", "run-close", "Close the run and release resources", "danger", True),
        ("run_reopen", "run-reopen", "Reopen the run", "primary", True),
        ("regression_accept", "regression-decide", "Accept the requested regression", "primary", False),
        ("regression_decline", "regression-decide", "Decline the requested regression", "danger", False),
        ("regression_cancel", "regression-cancel", "Cancel the regression request", "danger", False),
        ("launch_role", "launch-role", "Launch a selected managed role", "primary", False),
        ("verify_criterion", "verify", "Run focused criterion verification", "primary", False),
        ("verify_live", "verify-live", "Run live verification", "primary", False),
        # #183: engine-upgrade, engine-rollback and engine-migrate are CLI
        # commands; they were listed here as permanent READ ONLY rows that
        # could never act from a served dashboard, so they are not listed.
    ]
    closed = isinstance(status.get("run_closed"), dict)
    complete = status.get("status") == "complete" or int(status.get("phase_number", 0) or 0) >= 8
    phase = int(status.get("phase_number", 0) or 0)
    review = status.get("design_review") or {}
    review_ready = review.get("decision") == "approved"
    design_pending = bool(status.get("requires_design_approval")) and review_ready and not status.get("design_approved")
    deployment_pending = phase == 7 and not status.get("deployment_approved")
    deployment_revoke = phase in {7, 8} and isinstance(status.get("deployment_approved"), dict) and not status.get("live_verification_id")
    budget = design_review_budget(status, cfg)
    regression = active_regression_request(status)
    escalation = status.get("escalation") or {}
    recovery_hold = escalation.get("kind") in {"recovery_exhausted", "recovery_paused"}
    # #123: a replacement paused on a non-recoverable failure has no
    # escalation record, yet the Pilot must be able to clear it here.
    replacement_pause = None
    if not recovery_hold and not closed and not complete:
        try:
            assessment = recovery_assessment(status, cfg, {}, [], root=root)
        except (HandsoffError, OSError, ValueError):
            assessment = {}
        if assessment.get("reason") == "non_recoverable_failure":
            replacement_pause = (status.get("agent_failures") or {}).get(assessment.get("lost_session_id")) or {}
            recovery_hold = True
    automated = [c.get("id") for c in acceptance.get("criteria", [])
                 if "checks" in VERIFICATION_REQUIREMENTS.get(c.get("verification"), set())]
    try:
        verification_in_flight = bool(verify_inflight_bindings(root))
    except OSError:
        verification_in_flight = False
    launch_role = assigned_role(status)
    launch_reason = None
    launch_profile = None
    if status.get("status") != "in_progress":
        launch_reason = "run is not in progress"
    elif closed:
        launch_reason = "run is closed"
    elif launch_role not in SELECTABLE_AGENT_ROLES:
        launch_reason = "current phase assigns no managed role"
    else:
        launch_profile = resolved_agent_profiles(cfg).get(launch_role)
        current_session = current_agent_sessions(status).get(launch_role)
        if not launch_profile or launch_profile.get("adapter") == HOST_AGENT_ADAPTER:
            launch_reason = f"{launch_role} is host-driven"
        elif current_session and current_session.get("state") in AGENT_SESSION_LIVE_STATES:
            # #123: only a live session blocks a launch; a failed one is
            # exactly what a relaunch replaces.
            launch_reason = f"a live {launch_role} session already exists"
        elif phase == 2 and launch_role == "reviewer" and not status.get("design_proposal"):
            launch_reason = "reviewer launch requires a design proposal"
    actionable = set()
    if design_pending: actionable |= {"design_approve", "design_reject"}
    if deployment_pending: actionable |= {"deployment_approve", "deployment_hold"}
    if deployment_revoke: actionable.add("deployment_revoke")
    if phase == 2 and budget.get("exhausted"): actionable |= {"design_review_authorize", "design_review_escalate"}
    if recovery_hold: actionable.add("recovery_acknowledge")
    if recovery_hold and not replacement_pause: actionable.add("recover")
    if escalation.get("kind") == "review_cap_exhausted": actionable.add("review_cap_override")
    if regression and regression.get("state") in {"awaiting_approval", "accepted"}: actionable |= {"regression_accept", "regression_decline", "regression_cancel"}
    if not closed and not complete:
        if status.get("human_pause"): actionable.add("resume")
        else: actionable.add("pause")
        actionable.add("run_close")
    elif closed and not complete: actionable.add("run_reopen")
    result = []
    for kind, operation, consequence, tone, requires_reason in specs:
        if kind == "launch_role":
            if launch_reason:
                availability, reason = "unavailable", launch_reason
            else:
                availability, reason = "actionable", ""
                consequence = (f"launches a managed {launch_role}: {launch_profile['adapter']} "
                               f"({launch_profile['model']}), budget {cfg['agent_token_budgets'][launch_role]} tokens")
        elif kind in {"verify_criterion", "verify_live"}:
            if closed:
                availability, reason = "unavailable", "run is closed"
            elif complete:
                availability, reason = "unavailable", "run is complete"
            elif kind == "verify_criterion":
                if phase < 4 or phase > 7:
                    availability, reason = "unavailable", f"verification requires Phase 4 to 7, current phase is {phase}"
                elif verification_in_flight:
                    availability, reason = "unavailable", "verification already in flight"
                elif not automated:
                    availability, reason = "unavailable", "no automated criteria"
                else:
                    availability, reason = "actionable", ""
                    consequence = "runs the configured checks for the selected criteria as Mission Control Pilot and appends ledger records"
            elif phase not in {7, 8}:
                availability, reason = "unavailable", f"live verification requires Phase 7 or 8, current phase is {phase}"
            elif verification_in_flight:
                availability, reason = "unavailable", "verification already in flight"
            else:
                availability, reason = "actionable", ""
                consequence = "runs [checks].live_commands as Mission Control Pilot"
        elif kind in actionable:
            availability, reason = "actionable", ""
        elif closed:
            availability, reason = "unavailable", "run is closed"
        elif complete:
            availability, reason = "unavailable", "run is complete"
        elif kind.startswith("design_") and not review_ready:
            availability, reason = "unavailable", "design review is not approved yet"
        elif kind.startswith("deployment_"):
            availability, reason = "unavailable", "no deployment approval is pending"
        elif kind.startswith("regression_"):
            availability, reason = "unavailable", "no regression request is pending"
        elif kind == "design_review_authorize":
            availability, reason = "unavailable", "budget is not exhausted"
        else:
            availability, reason = "unavailable", "operation is not currently applicable"
        entry = {"kind": kind, "operation": operation, "availability": availability,
                       "reason": reason, "consequence": consequence,
                       "action_id": f"{kind}:{action_binding}" if availability == "actionable" else None,
                       "requires_reason": requires_reason, "tone": tone}
        if kind == "verify_criterion":
            entry["criteria"] = automated if availability == "actionable" else []
        if kind == "launch_role":
            entry["launchable_roles"] = [launch_role] if availability == "actionable" else []
            entry["role"] = launch_role
        if kind == "recovery_acknowledge" and replacement_pause:
            entry["consequence"] = (f"Clears the replacement pause after a {replacement_pause.get('category')} failure "
                                    f"({replacement_pause.get('reason')}); relaunch the role afterwards")
        result.append(entry)
    return result


GATE_PROGRESS_WEIGHTS = (
    ("initialized", 5), ("design_reviewed", 15), ("design_approved", 25), ("evidence", 45),
    ("symptom", 50), ("review", 65), ("deployment", 80), ("live", 95), ("complete", 100),
)


def gate_progress(status: dict, acceptance: dict) -> dict:
    """#102: progress as gates cleared, not phase index. A run with an
    approved design used to read 8 percent because progress was
    phase-weighted; here it reads 25. Gates are cumulative: the percent is
    the weight of the highest gate cleared, `cleared` lists them in order."""
    criteria = acceptance.get("criteria", []) if isinstance(acceptance, dict) else []
    automated = [c for c in criteria if "checks" in VERIFICATION_REQUIREMENTS.get(c.get("verification"), set())]
    review = status.get("design_review") if isinstance(status, dict) else None
    facts = {
        "initialized": bool(status),
        "design_reviewed": isinstance(review, dict) and review.get("decision") == "approved",
        "design_approved": isinstance(status.get("design_approved"), dict),
        "evidence": bool(automated) and all(c.get("state") == "passing" for c in automated),
        "symptom": bool((status.get("requirement_coverage") or {}).get("original_symptom_resolved")
                        or status.get("original_symptom_evidence_id")),
        "review": isinstance(status.get("review"), dict) and bool(status.get("reviewed_by")),
        "deployment": isinstance(status.get("deployment_approved"), dict),
        "live": bool(status.get("live_verification_id")),
        "complete": status.get("status") == "complete" or int(status.get("phase_number", 0) or 0) >= 8,
    }
    cleared = [name for name, _ in GATE_PROGRESS_WEIGHTS if facts[name]]
    percent = max((weight for name, weight in GATE_PROGRESS_WEIGHTS if facts[name]), default=0)
    return {"percent": percent, "cleared": cleared}


def _design_hash_current(recorded: object, status: dict, acceptance: dict) -> bool:
    """A design decision is current when its hash is the registry's design
    hash, or (#42) while a scoped amendment is open: the decision still
    carries the amendment's base hash and the registry is exactly the
    amended one. Approving the amendment rewrites the decision to the
    resulting hash; escalating it clears the decision."""
    current = design_hash(acceptance.get("criteria", []))
    if recorded == current:
        return True
    amendment = open_amendment(status)
    return bool(amendment) and amendment.get("base_design_hash") == recorded \
        and amendment.get("resulting_design_hash") == current


def _design_errors(status: dict, acceptance: dict, cfg: dict, root: Path | None = None) -> list[str]:
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
    if not _design_hash_current(approval.get("design_hash"), status, acceptance):
        errors.append("design gate: criteria were added, removed, or respecified since design approval; record a new approval")
    if approval.get("config_hash") != config_hash(cfg):
        errors.append("design gate: workflow policy changed since design approval; record a new approval")
    # #170: the design approval RECORDS the rules set it was given under;
    # the comparison belongs to the review, deployment and live gates, so
    # a hook edit at Phase 6 asks for a fresh review, not a fresh design.
    if "work_items" in acceptance and not scope_hash_matches(approval.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
        errors.append("design gate: work-item scope changed since design approval; record a new approval")
    proposal = status.get("design_proposal")
    if isinstance(proposal, dict) and approval.get("proposal_hash") != proposal.get("proposal_hash"):
        errors.append(f"stale proposal: approval binds {approval.get('proposal_hash')}, current is {proposal.get('proposal_hash')}")
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
    if not _design_hash_current(review.get("design_hash"), status, acceptance):
        errors.append("design review gate: criteria changed since design review; record a new design review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("design review gate: workflow policy changed since design review; record a new design review")
    if "work_items" in acceptance and not scope_hash_matches(review.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
        errors.append("design review gate: work-item scope changed since design review; record a new design review")
    proposal = status.get("design_proposal")
    if isinstance(proposal, dict) and review.get("proposal_hash") != proposal.get("proposal_hash"):
        errors.append(f"stale proposal: approval binds {review.get('proposal_hash')}, current is {proposal.get('proposal_hash')}")
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
    recorded_reviews = len(status.get("design_review_history") or [])
    attempts = max(int(status.get("design_review_attempts", 0) or 0), recorded_reviews)
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
    sessions = status.get("agent_sessions") if isinstance(status, dict) else {}
    # #113: only a Phase 2 launch is a design reviewer. A Phase 5
    # implementation reviewer never carries selection metadata and must
    # not be read as a consistency fault here.
    live_reviewers = sorted(
        (session for session in (sessions or {}).values()
         if isinstance(session, dict) and session.get("role") == "reviewer"
         and session.get("state") in AGENT_SESSION_LIVE_STATES
         and int(session.get("phase_number") or 0) == 2),
        key=lambda item: item.get("started_at") or "",
    )
    consistency_errors = []
    if live_reviewers:
        newest = live_reviewers[-1]
        current = {"actor": newest.get("actor"), "session_id": newest.get("session_id"),
                   "adapter": newest.get("adapter"),
                   "attempt": max(int(status.get("design_review_attempts", 0) or 0),
                                  len(status.get("design_review_history") or [])) + 1,
                   "state": newest.get("state")}
        selection_metadata = status.get("design_reviewer_selection")
        if not isinstance(selection_metadata, dict):
            consistency_errors.append(f"live reviewer session {newest.get('session_id')} has no selection metadata")
        else:
            recorded = selection_metadata.get("current") if isinstance(selection_metadata.get("current"), dict) else None
            if recorded is None:
                consistency_errors.append(f"live reviewer session {newest.get('session_id')} has no selection metadata")
            elif recorded.get("session_id") not in (None, newest.get("session_id")) \
                    or (recorded.get("actor") and recorded.get("actor") != newest.get("actor")):
                consistency_errors.append(
                    f"selection metadata names {recorded.get('actor')} ({recorded.get('session_id')}) "
                    f"but the newest live reviewer is {newest.get('actor')} ({newest.get('session_id')})")
        if len(live_reviewers) > 1:
            consistency_errors.append("unexpected_live_sessions: " + ", ".join(
                item.get("session_id", "") for item in live_reviewers[:-1]))
    tier, reason = select_design_reviewer_tier(cfg, status, acceptance)
    lookup = which or shutil.which
    try:
        profile = design_reviewer_tier_profile(cfg, tier, which=lookup, require_available=False)
    except HandsoffError as exc:
        return {"current": current, "consistency_errors": consistency_errors,
                "next": {"tier": tier, "reason": reason, "adapter": None, "model": None, "error": str(exc)}}
    error = None
    try:
        select_design_reviewer_profile(cfg, status, acceptance, which=lookup)
    except HandsoffError as exc:
        error = str(exc)
    return {"current": current, "consistency_errors": consistency_errors,
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
                   verification_problems: list[str] | None = None,
                   root: Path | None = None) -> list[str]:
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
    gate_criteria = criteria
    registry = acceptance.get("work_items")
    if isinstance(registry, list):
        required_ids = {item.get("id") for item in registry if item.get("required", True)}
        gate_criteria = [criterion for criterion in criteria
                         if criterion_work_item(criterion, registry) in required_ids
                         or criterion_work_item(criterion, registry) == "unattributed"]
    coverage = status.get("requirement_coverage", {})
    green = _is_green(gate_criteria)
    resolved = coverage.get("original_symptom_resolved") is True
    phase = int(status.get("phase_number", 0) or 0)
    progress = float(status.get("progress", 0) or 0)
    evidence_errors = _evidence_errors(gate_criteria, verifications or [])
    if phase >= 6 or progress >= 95:
        # #165: the failing-first gate rides with the evidence gate, so a
        # green with no red behind it blocks Phase 6+ and 95%+ alike.
        evidence_errors.extend(baseline_errors(gate_criteria, verifications or [], cfg))
    if root is not None and phase >= 5:
        drift = evidence_drift(root, cfg, acceptance, records)
        for cid in drift["stale"]:
            paths = ", ".join(drift.get("changed_paths", []))
            suffix = f"; changed paths: {paths}" if paths else ""
            errors.append(f"evidence drift: {cid} was verified on a different repository digest{suffix}; "
                          f"re-run handsoff_supervisor.py verify --criterion {cid} --by ACTOR")
    expected_coverage = coverage_for(criteria, resolved)
    symptom_record = _valid_symptom_record(status, gate_criteria, verifications or [])

    if status.get("feature") != acceptance.get("feature"):
        errors.append("state gate: status and acceptance describe different features")
    if coverage != expected_coverage:
        errors.append("state gate: requirement_coverage does not match the acceptance registry")
    if phase == 7 and status.get("status") not in {"awaiting_approval", "ready_to_deploy"}:
        errors.append("state gate: Phase 7 requires status 'awaiting_approval' or 'ready_to_deploy'")

    if phase >= 3 and full_design_required(status, acceptance, cfg):
        errors.extend(_design_review_errors(status, acceptance, cfg))
        errors.extend(_design_errors(status, acceptance, cfg, root))
    # #42: an open amendment pins the run to the phase and progress it was
    # opened at, forward and back, until it is approved or escalated.
    errors.extend(amendment_freeze_errors(status))

    if phase >= 6 and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("phase gate: every criterion and the original symptom must have verified evidence before Phase 6+")
        if resolved and not symptom_record:
            errors.append("symptom gate: resolved original symptom must reference a successful verification run")
        errors.extend(evidence_errors)
    if progress >= 95 and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("progress gate: 95%+ requires verified acceptance and a resolved original symptom")
        if phase < 6:
            errors.extend(evidence_errors)  # name the baseline gaps here too (#165)
    if "work_items" in acceptance and progress >= 95:
        unfinished = [item for item in derive_work_items(status, acceptance, cfg)["items"]
                      if item.get("required") and item.get("status") != "done"]
        for item in unfinished:
            if item.get("status") == "unscoped":
                tag = f"#{item['number']}" if item.get("kind") == "issue" else item["id"][4:]
                errors.append(f"progress gate: required work item {item['id']} has no acceptance criteria (unscoped); run handsoff_supervisor.py work-item-remove {item['id']} --by ACTOR or tag a criterion [{tag}]")
            else:
                errors.append(f"progress gate: required work item {item['id']} must be done before 95%+")
    if status.get("status") in ("ready_to_deploy", "awaiting_approval", "complete") and (not green or not resolved or not symptom_record or evidence_errors):
        errors.append("status gate: acceptance registry is not fully green")
    if "work_items" in acceptance and (phase >= 8 or status.get("status") == "complete"):
        unfinished = [item for item in derive_work_items(status, acceptance, cfg)["items"]
                      if item.get("required") and item.get("status") != "done"]
        for item in unfinished:
            if item.get("status") == "unscoped":
                tag = f"#{item['number']}" if item.get("kind") == "issue" else item["id"][4:]
                errors.append(f"work items gate: required work item {item['id']} has no acceptance criteria (unscoped); run handsoff_supervisor.py work-item-remove {item['id']} --by ACTOR or tag a criterion [{tag}]")
            else:
                errors.append(f"work items gate: required work item {item['id']} is {item['status']}; a run cannot complete while it is unfinished")
        # #116: every required item names who implemented it, or the
        # completion audit is silently weaker for items added mid-run.
        delivery = status.get("work_item_delivery")
        if isinstance(delivery, dict):
            for item in derive_work_items(status, acceptance, cfg)["items"]:
                record = delivery.get(item["id"])
                if item.get("required") and item.get("status") != "unscoped" \
                        and (not isinstance(record, dict) or not record.get("implemented_by")):
                    errors.append(f"work items gate: work item {item['id']} has no implemented_by; run handsoff_supervisor.py work-item-update {item['id']} --by ACTOR --implemented-by ACTOR")

    if phase >= 6:
        implemented_by = status.get("implemented_by")
        if not implemented_by:
            errors.append("review gate: Phase 6+ requires 'implemented_by' to be recorded")
        errors.extend(_review_errors(status, acceptance, cfg, root))

    if cfg.get("deployment_requires_explicit_approval", True) and phase >= 8:
        approval = status.get("deployment_approved")
        if not approval or not approval.get("at"):
            errors.append("deployment gate: Phase 8 requires a recorded deployment approval")
        elif approval.get("acceptance_hash") != acceptance_hash(criteria):
            errors.append("deployment gate: the acceptance registry changed since approval was given, re-approve")
        elif approval.get("config_hash") != config_hash(cfg):
            errors.append("deployment gate: workflow policy changed since approval was given, re-approve")
        else:
            errors.extend(rules_binding_errors(root, cfg, approval, "deployment gate"))  # #170

    if phase >= 3 and pending_design_decline(status):
        errors.append("design gate: a decline is pending the independent reviewer's word; approve closes the run as not planned, changes send it back")  # #177
    if phase >= 7:
        errors.extend(ci_gate_errors(status))  # #181

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
            elif rules_binding_errors(root, cfg, record, "live gate"):
                errors.extend(rules_binding_errors(root, cfg, record, "live gate"))  # #170
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


def implementation_evidence_complete(status: dict) -> bool:
    """#122: every automated criterion passing and the original symptom
    resolved, read from the run's own coverage bookkeeping."""
    coverage = status.get("requirement_coverage") if isinstance(status.get("requirement_coverage"), dict) else {}
    symptom = bool(coverage.get("original_symptom_resolved") or status.get("original_symptom_evidence_id"))
    counts = {key: int(coverage.get(key, 0) or 0) for key in ("passing", "failing", "not_tested", "blocked")}
    return symptom and counts["passing"] > 0 and counts["failing"] + counts["not_tested"] + counts["blocked"] == 0


def assigned_role(status: dict) -> str | None:
    if status.get("status") == "complete":
        return None
    phase = int(status.get("phase_number", 1) or 1)
    if phase == 1:
        return "architect"
    if phase == 2:
        # A recorded Pilot approval ends design work even before the
        # phase counter advances.  This transition is deterministic and is
        # performed by the owned dashboard without an LLM; assigning a
        # Supervisor here wastes tokens and lets malformed model protocol
        # turn a valid approval into a recovery hold.
        if isinstance(status.get("design_approved"), dict):
            return None
        review = status.get("design_review") or {}
        proposal = status.get("design_proposal") or {}
        proposal_ready = isinstance(proposal, dict) \
            and proposal.get("based_on_review_attempt") == int(status.get("design_review_attempts", 0) or 0)
        if review.get("decision") == "changes_requested" and not proposal_ready:
            return "architect"
        return "reviewer"
    if phase == 5 and isinstance(status.get("review"), dict):
        # record-review completes the Reviewer's assignment before the
        # Supervisor advances to Phase 6. The watchdog must not recover
        # that deliberately completed reviewer during this interval.
        return "supervisor"
    if phase == 4 and implementation_evidence_complete(status):
        # #122: the Implementer's assignment ends with the last piece of
        # evidence; the Supervisor advances to Phase 5.
        return "supervisor"
    return {3: "supervisor", 4: "implementer", 5: "reviewer", 6: "implementer",
            7: "supervisor", 8: "supervisor"}.get(phase)


# #121: what an orchestration launch tells each role. The Supervisor gets
# the run's next_action (it is the one who acts on it); the sandboxed
# roles get their own instruction and never a Supervisor-facing command.
ORCHESTRATION_TASKS = {
    "architect": ("Act as the Architect for this mission. Your sandbox is read-only and that is expected: "
                  "record acceptance criteria with a HANDSOFF_BROKER_REQUEST criteria transaction and end with your "
                  "HANDSOFF_DESIGN_PROPOSAL; the host records both. Do not run supervisor commands and do not ask "
                  "permission for the assigned scope."),
    "implementer": ("Act as the Implementer against the approved design and the supplied managed design context. "
                    "Implement, run the linked checks with verify, record the resolved symptom, and stop; do not "
                    "repeat evidenced work or run a full regression suite."),
    "reviewer": ("Act as the independent Reviewer. Your sandbox is read-only and that is expected: read the diff and "
                 "the acceptance registry, run the linked checks, and end with exactly one HANDSOFF_REVIEW_RESULT "
                 "line; the host records it. Do not run supervisor commands. structural_blocker is true only when "
                 "the design itself cannot satisfy a criterion; a blocked recording is never a structural blocker."),
}


def orchestration_task(role: str, status: dict, *, objective: str | None = None) -> str:
    if role == "supervisor":
        next_action = str(status.get("next_action") or "Continue the current workflow step.")
        return (f"Continue the managed Handsoff workflow as supervisor. Execute this current next action: "
                f"{next_action} Use the required broker protocol; use the supplied managed design context instead "
                "of rediscovering the repository; do not stop at narration, repeat evidenced work, or run a full "
                "regression suite.")
    text = ORCHESTRATION_TASKS.get(role, f"Continue the managed Handsoff workflow as {role}.")
    if objective:
        text = f"Mission objective: {objective}\n\n{text}"
    return text


def managed_handoff_role(status: dict, cfg: dict | None = None) -> str | None:
    """Return the next role only for an already-managed, decision-free chain.

    Recovery deliberately refuses to manufacture a first session for a
    manually driven run.  Normal orchestration is different: after one
    managed role completes and workflow state assigns a different role, an
    owned Mission Control dashboard should launch that role without waiting
    for a terminal operator.  The latest completed session must be newer than
    the target role's latest session, which prevents completed/no-op roles
    from being relaunched forever.
    """
    if status.get("status") in {"complete", "blocked", "awaiting_approval", "ready_to_deploy", "closed"}:
        return None
    if isinstance(status.get("run_closed"), dict) or status.get("escalation") is not None \
            or status.get("authorization_hold") is not None:
        return None
    if isinstance(status.get("human_pause"), dict) or isinstance(status.get("background_wait"), dict):
        return None
    if any(isinstance(item, dict) and item.get("answer") is None and item.get("state", "pending") == "pending"
           for item in status.get("pending_questions") or []):
        return None
    # #120: a question answered but not yet delivered relaunches the role
    # that asked it, unless that role already has a live session.
    undelivered = [item for item in status.get("pending_questions") or []
                   if isinstance(item, dict) and item.get("answer") is not None and not item.get("delivered_at")]
    if undelivered:
        asker = undelivered[-1].get("role")
        live = any(isinstance(item, dict) and item.get("state") in AGENT_SESSION_LIVE_STATES
                   and item.get("role") == asker for item in (status.get("agent_sessions") or {}).values())
        if asker in SELECTABLE_AGENT_ROLES and not live \
                and not (cfg is not None and cfg.get("agents", {}).get(asker) == HOST_AGENT_ADAPTER):
            return asker
    if any(isinstance(item, dict) and item.get("state") in {"awaiting_approval", "accepted", "launched"}
           for item in status.get("regression_requests") or []):
        return None
    if any(isinstance(item, dict) and item.get("state") in {"reserved", "launched"}
           for item in status.get("recovery_attempts") or []):
        return None
    target = assigned_role(status)
    if target is None:
        return None
    if target == "reviewer" and isinstance(status.get("review"), dict):
        # #125: a recorded review is never re-run by orchestration.
        return None
    if cfg is not None:
        if cfg.get("agents", {}).get(target) == HOST_AGENT_ADAPTER:
            return None
        if cfg.get("agents", {}).get("supervisor") == HOST_AGENT_ADAPTER and target != "reviewer":
            return None
    phase = int(status.get("phase_number", 1) or 1)
    if target == "reviewer" and phase == 2 and cfg is not None:
        budget = design_review_budget(status, cfg)
        if design_review_launch_refusal(budget, status):
            return None
    sessions = [item for item in (status.get("agent_sessions") or {}).values()
                if isinstance(item, dict)]
    if any(item.get("state") in AGENT_SESSION_LIVE_STATES for item in sessions):
        return None

    relevant = [item for item in sessions
                if item.get("state") == "completed"
                and item.get("role") in SELECTABLE_AGENT_ROLES
                and item.get("phase_number") in {phase, phase - 1}
                and isinstance(item.get("ended_at"), str)]
    if not relevant:
        return None
    source = max(relevant, key=lambda item: item["ended_at"])
    if source.get("role") == target:
        return None
    # A terminal assignment from an earlier phase must not suppress the
    # same role's new assignment in this phase. This matters most when a
    # Phase-2 Supervisor request failed before the trusted host advanced an
    # approved design to Phase 3.
    target_sessions = [item for item in sessions
                       if item.get("role") == target
                       and item.get("phase_number") == phase
                       and isinstance(item.get("ended_at"), str)]
    if target_sessions and max(item["ended_at"] for item in target_sessions) >= source["ended_at"]:
        return None
    return target


DESIGN_PROPOSAL_FIELDS = ("summary", "approach", "tradeoffs", "decisions", "constraints", "verification")


def validate_design_proposal(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(DESIGN_PROPOSAL_FIELDS):
        raise HandsoffError("design proposal must contain exactly summary, approach, tradeoffs, decisions, constraints, and verification")
    result = {}
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 512:
        raise HandsoffError("design proposal summary must be 1 to 512 characters")
    result["summary"] = summary.strip()
    for field in DESIGN_PROPOSAL_FIELDS[1:]:
        items = value.get(field)
        minimum = 1 if field in {"approach", "decisions", "verification"} else 0
        if not isinstance(items, list) or not minimum <= len(items) <= 8:
            raise HandsoffError(f"design proposal {field} must contain {minimum} to 8 items")
        cleaned = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 512:
                raise HandsoffError(f"design proposal {field} item length {len(item.strip()) if isinstance(item, str) else 0}; each item must be 1 to 512 characters")
            cleaned.append(item.strip())
        result[field] = cleaned
    return result


def record_design_proposal(root: Path, session_id: str | None, value: object,
                           *, architect_actor: str | None = None) -> dict:
    """Persist one bounded proposal through the same managed and host path.

    The host Architect has no managed session, but must receive identical
    criterion, review-budget, and event bindings so later review cannot tell
    which execution surface produced the proposal.
    """
    proposal = validate_design_proposal(value)
    root = root.resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        sessions = status.get("agent_sessions") or {}
        current = status.get("current_agent_sessions") or {}
        session = sessions.get(session_id) if session_id is not None else None
        if session_id is not None:
            if not isinstance(session, dict) or session.get("role") != "architect" \
                    or current.get("architect") != session_id \
                    or session.get("state") not in AGENT_SESSION_LIVE_STATES:
                raise HandsoffError("design proposal must come from the current live Architect session")
        actor = session.get("actor") if session is not None else validate_agent_actor(architect_actor)
        if int(status.get("phase_number", 1) or 1) not in {1, 2}:
            raise HandsoffError("design proposal can only be recorded in Phase 1 or Phase 2")
        now = datetime.now(timezone.utc).isoformat()
        bound = {
            **proposal,
            "architect": actor,
            "session_id": session_id,
            "at": now,
            "design_hash": design_hash(acceptance.get("criteria", [])),
            "based_on_review_attempt": int(status.get("design_review_attempts", 0) or 0),
            "provenance": {
                "actor": actor,
                "pid": os.getpid(),
                "executable": str(Path(sys.argv[0]).resolve()),
                "host_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_COMPANION_SESSION_ID"),
                "recorded_at": now,
            },
        }
        bound["proposal_hash"] = hashlib.sha256(_canonical(proposal).encode("utf-8")).hexdigest()
        proposed = deepcopy(status)
        proposed["design_proposal"] = bound
        old_review = status.get("design_review")
        old_approval = status.get("design_approved")
        proposed["design_review"] = None
        proposed["design_approved"] = None
        budget = design_review_budget(status, cfg)
        refusal = design_review_launch_refusal(budget, status)
        if refusal is not None:
            proposed["status"] = "blocked"
            proposed["authorization_hold"] = "design_review"
            proposed["next_action"] = "Revised design proposal recorded; " + design_review_budget_exhausted_message(budget)
        else:
            proposed["status"] = "in_progress"
            proposed.pop("authorization_hold", None)
            proposed["next_action"] = "Independent Reviewer evaluates the revised proposal"
        proposed["updated_at"] = now
        invalidated = []
        if isinstance(old_approval, dict):
            invalidated.append("design_approved")
        if isinstance(old_review, dict):
            invalidated.append("design_review")
        extra_events = None
        if invalidated:
            extra_events = [{
                "kind": "decisions_invalidated",
                "message": "Prior design decisions invalidated by revised proposal",
                "old_proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
                "new_proposal_hash": bound["proposal_hash"],
                "nulled_decisions": invalidated,
            }]
        commit(root, cfg, status=proposed, event_kind="design_proposal_recorded",
               event_message="Bounded Architect design proposal recorded",
               extra_events=extra_events,
               architect=actor, session_id=session_id,
               proposal_hash=bound["proposal_hash"], design_hash=bound["design_hash"],
               based_on_review_attempt=bound["based_on_review_attempt"],
               counts={field: len(bound[field]) for field in DESIGN_PROPOSAL_FIELDS[1:]})
        return deepcopy(bound)


def managed_design_context(root: Path, role: str) -> dict | None:
    """Compact, bounded Phase-2 context supplied instead of repo rediscovery."""
    if role not in {"architect", "reviewer"}:
        return None
    cfg = load_config(root)
    status_file = status_path(root, cfg)
    acceptance_file = acceptance_path(root, cfg)
    if not status_file.is_file() or not acceptance_file.is_file():
        return None
    status = load_unique_json(status_file)
    acceptance = load_unique_json(acceptance_file)
    if status.get("phase_number") != 2:
        return None
    latest = status.get("design_review") or {}
    findings = latest.get("findings") or latest_design_review_findings(status)
    bounded_findings = [item for item in findings
                        if isinstance(item, dict) and isinstance(item.get("text"), str)][:32]
    return {
        "feature": status.get("feature") or acceptance.get("feature"),
        "next_action": status.get("next_action"),
        "review_attempts": int(status.get("design_review_attempts", 0) or 0),
        "review_summary": latest.get("summary"),
        "findings": [{"id": item.get("id"), "text": item.get("text")} for item in findings if isinstance(item, dict)][:32],
        "prior_findings": [{"number": number, "text": item["text"][:512]}
                           for number, item in enumerate(bounded_findings, 1)],
        "criteria": [{key: item.get(key) for key in ("id", "type", "requirement", "verification", "tests")}
                     for item in acceptance.get("criteria", [])[:64] if isinstance(item, dict)],
        "design_proposal": status.get("design_proposal"),
        # #177: a pending decline is reviewed in place of a proposal, for its
        # evidence, not for the effort it saves
        "design_decline": pending_design_decline(status),
        "instructions": ("Use this packet first. Do not list or search the whole repository. "
                         "Inspect only files needed to resolve a named finding. "
                         "Judge only prior findings the revision leaves unanswered; a finding "
                         "answered by number with a concrete change is settled."),
    }


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


def bound_heartbeat_at(status: dict) -> str | None:
    """Return a heartbeat only while its declared owner is still current.

    Managed heartbeats are leases owned by one live session. Background
    waits use their explicit persisted wait record. Legacy/unowned pings are
    deliberately ignored so a detached shell cannot keep a dead role green.
    """
    stamped = status.get("last_heartbeat_at")
    owner = status.get("last_heartbeat_owner")
    if not isinstance(stamped, str) or not isinstance(owner, str):
        return None
    if owner == "background_wait":
        return stamped if isinstance(status.get("background_wait"), dict) else None
    return stamped if any(
        isinstance(session, dict) and session.get("session_id") == owner
        and session.get("state") in AGENT_SESSION_LIVE_STATES
        for session in current_agent_sessions(status).values()
    ) else None


def _beacon_process_alive(root: Path | None, session_id: str) -> bool:
    """True when the live beacon names `session_id` and its pid still
    exists. Existence, not identity: pid reuse is bounded by the beacon's
    own session binding, and a stale beacon for another session never
    vouches for this one."""
    if root is None:
        return False
    beacon = read_live_beacon(root)
    if not isinstance(beacon, dict) or beacon.get("session_id") != session_id:
        return False
    pid = beacon.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def recovery_assessment(status: dict, cfg: dict, liveness: dict | None = None,
                        events: list[dict] | None = None,
                        now: datetime | None = None, root: Path | None = None) -> dict:
    """`root` lets the assessment consult the live beacon (#86); callers
    without a root (pure tests, older call sites) keep the timestamp rule."""
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
    if cfg.get("agents", {}).get(role) == HOST_AGENT_ADAPTER:
        result["reason"] = "host_role"
        return result
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    phase = int(status.get("phase_number", 1) or 1)

    def session_signal(item: dict) -> tuple[str, float | None, float, str] | None:
        state = item.get("state")
        if state in {"failed", "timed_out", "failed_to_start"}:
            return ("worker_terminal", _minutes_since(item.get("ended_at"), now),
                    float(recovery["worker_loss_grace_minutes"]), "failed session is terminal")
        if state in AGENT_SESSION_LIVE_STATES:
            sid = item.get("session_id")
            ping = (liveness or {}).get(sid)
            protocol_limit = int((recovery.get("protocol_silence_minutes") or {}).get(role, 0) or 0)
            if protocol_limit > 0 and root is not None:
                store = _read_agent_output_store(root)
                output = (store.get("sessions") or {}).get(sid)
                entries = output.get("entries") if isinstance(output, dict) else []
                newest = entries[-1].get("at") if entries and isinstance(entries[-1], dict) else None
                ping = newest or (output.get("updated_at") if isinstance(output, dict) else None)
                ping = ping or item.get("running_at") or item.get("started_at")
                minutes = _minutes_since(ping, now)
                if minutes is not None and minutes >= protocol_limit:
                    return ("protocol_silent", minutes, float(protocol_limit),
                            f"no protocol output for {int(minutes)} minutes (limit {protocol_limit})")
            ping = ping or item.get("running_at") or item.get("started_at")
            return ("worker_silent", _minutes_since(ping, now),
                    float(recovery["live_session_silence_minutes"]), "session liveness expired")
        return None

    # The in-process fallback planner already refuses these categories.  The
    # host watchdog must honor the same decision or it can spend the budget
    # again after the launcher intentionally paused (# token ceilings).
    for current_role, candidate_id in current.items():
        candidate = sessions.get(candidate_id) if isinstance(candidate_id, str) else None
        failure = (status.get("agent_failures") or {}).get(candidate_id) \
            if isinstance(candidate_id, str) else None
        if isinstance(candidate, dict) and candidate.get("state") in {
                "failed", "timed_out", "failed_to_start", "cancelled"} \
                and isinstance(failure, dict) and not failure.get("adopted") \
                and not failure.get("acknowledged") \
                and not failure.get("auto_retry_authorized") \
                and failure.get("category") not in RECOVERABLE_FAILURE_CATEGORIES:
            result.update(reason="non_recoverable_failure", assigned_role=current_role,
                          lost_session_id=candidate_id)
            return result

    acknowledged = {
        event.get("session_id") for event in (events or [])
        if isinstance(event, dict) and event.get("kind") == "recovery_acknowledged"
    }
    candidates = []
    for current_role, candidate_id in current.items():
        candidate = sessions.get(candidate_id) if isinstance(candidate_id, str) else None
        if not isinstance(candidate, dict) or candidate_id in acknowledged:
            continue
        candidate_phase = candidate.get("phase_number")
        if candidate_phase is not None and candidate_phase != phase:
            continue
        observed = session_signal(candidate)
        if observed is None:
            continue
        state_name, silent, threshold, reason = observed
        if state_name == "worker_silent" and _beacon_process_alive(root, candidate_id):
            # #86: a liveness timestamp can go stale without the worker
            # dying (a sleeping laptop, a paused runner). The beacon names
            # the child's pid; while that pid exists, presuming the
            # session lost would kill real work and pay for a replacement.
            result.update(state="active", reason="process_alive", assigned_role=current_role,
                          lost_session_id=candidate_id, silent_minutes=silent,
                          threshold_minutes=threshold)
            return result
        if silent is not None and silent >= threshold:
            stamp = candidate.get("ended_at") or candidate.get("running_at") \
                or candidate.get("started_at") or ""
            candidates.append((stamp, current_role, candidate_id, state_name, silent, threshold, reason))
    if candidates:
        _, exact_role, exact_id, state_name, silent, threshold, reason = max(candidates)
        result.update(state=state_name, reason=reason, assigned_role=exact_role,
                      lost_session_id=exact_id, silent_minutes=silent,
                      threshold_minutes=threshold)
        return result

    session_id = current.get(role)
    session = sessions.get(session_id) if isinstance(session_id, str) else None
    if not isinstance(session, dict):
        # Recovery may replace a worker that was actually launched and was
        # subsequently lost.  It must never manufacture the first managed
        # session for a role merely because a run has been quiet.  Manual
        # and externally driven runs intentionally have no agent_session_*
        # record for that role; escalating them is a false alarm (#51).
        managed_for_role = any(
            isinstance(event, dict)
            and str(event.get("kind") or "").startswith("agent_session_")
            and event.get("role") == role
            for event in (events or [])
        ) or any(item.get("role") == role for item in status.get("recovery_attempts", []))
        if not managed_for_role:
            result["reason"] = "no_managed_session"
            return result
        timestamps = [status.get("updated_at"), bound_heartbeat_at(status)]
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
    if session.get("state") in AGENT_SESSION_TERMINAL_STATES:
        result["reason"] = "assigned session is terminal but not recoverable"
        return result
    state = session.get("state")
    result["lost_session_id"] = session_id
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


def _recovery_episode_attempts(attempts: list[dict], lost_session_id: str | None,
                               role: str | None) -> list[dict]:
    """Return the contiguous retry chain ending at one exact lost session."""
    if not lost_session_id:
        return [item for item in attempts
                if item.get("role") == role and item.get("from_session_id") is None]
    selected = []
    cursor = lost_session_id
    for item in reversed(attempts):
        if item.get("role") != role:
            if selected:
                break
            continue
        if item.get("to_session_id") == cursor or item.get("from_session_id") == cursor:
            selected.append(item)
            cursor = item.get("from_session_id")
        elif selected:
            break
    return list(reversed(selected))


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
            status, cfg, read_session_liveness(root), read_events(root, cfg), now, root=root,
        )
        if assessment["state"] in {"not_applicable", "active"}:
            return {"action": "skipped", "assessment": assessment}
        attempts = list(status.get("recovery_attempts") or [])
        cap = cfg["recovery"]["max_attempts"]
        episode_attempts = _recovery_episode_attempts(
            attempts, assessment.get("lost_session_id"), assessment.get("assigned_role"),
        )
        if len(episode_attempts) >= cap:
            proposed = deepcopy(status)
            proposed["status"] = "blocked"
            proposed["escalation"] = {
                "kind": "recovery_exhausted", "at": now.isoformat(),
                "reason": f"automatic recovery exhausted ({len(episode_attempts)} of {cap})",
                "required_action": "Run recovery-acknowledge --by OPERATOR --reason TEXT",
                "source": episode_attempts[-1]["recovery_id"] if episode_attempts else "recovery-ledger",
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
        if assessment["state"] in {"worker_silent", "protocol_silent"} and assessment.get("lost_session_id"):
            sid = assessment["lost_session_id"]
            session = (proposed.get("agent_sessions") or {}).get(sid)
            if isinstance(session, dict) and session.get("state") in AGENT_SESSION_LIVE_STATES:
                session["state"] = "failed"
                session["ended_at"] = now.isoformat()
                session["exit_code"] = None if assessment["state"] == "protocol_silent" else -1
                category = "protocol_silence" if assessment["state"] == "protocol_silent" else "presumed_lost"
                proposed.setdefault("agent_failures", {})[sid] = {
                    "session_id": sid, "category": category,
                    "reason": assessment["reason"] if category == "protocol_silence" else _FAILURE_REASON_LABELS["presumed_lost"],
                    "tail_sha256": hashlib.sha256(b"").hexdigest(), "at": now.isoformat(),
                }
        record = {
            "recovery_id": rid, "role": role, "trigger": assessment["state"],
            "from_session_id": assessment.get("lost_session_id"), "to_session_id": None,
            "attempt": len(episode_attempts) + 1, "cap": cap, "holder": actor,
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
        extra_events = None
        if assessment["state"] == "protocol_silent":
            extra_events = [{"kind": "protocol_silence_enforced", "message": assessment["reason"],
                             "role": role, "session_id": assessment["lost_session_id"],
                             "silent_minutes": assessment["silent_minutes"], "threshold": assessment["threshold_minutes"]}]
        commit(root, cfg, status=proposed, extra_events=extra_events,
               event_kind="recovery_recovered" if ok else "recovery_failed",
               event_message=f"Recovery attempt {item['attempt']} {item['state']}",
               by=actor, recovery_id=rid, role=role, state=item["state"])
    return {"action": "recovered" if ok else "failed", "assessment": assessment,
            "recovery_id": rid, "attempt": record["attempt"], "cap": cap}


def stall_warning(status: dict, cfg: dict, *, now: datetime | None = None,
                  output_liveness: dict | None = None) -> str | None:
    """Advisory only, never blocks a call: a stalled run should surface for
    escalation, not lock the operator out of even reading status.

    Reads the FRESHEST of three signals, not `updated_at` alone: `updated_at`
    (bumped by any progress-making call: advance, verify, record-evidence,
    ...), `last_heartbeat_at` (bumped only by the `heartbeat` command, a
    pure liveness ping for a run doing legitimate long background work that
    has no progress to report yet), and, when the caller passes the
    `.handsoff-output-liveness.json` record (#41), the `output_at` of the
    current live managed session, so a child that is streaming output is
    never flagged silent. A run flagged stalled here has NONE of the
    signals current -- genuinely no activity, not merely no status-file
    write. `last_heartbeat_at` may be entirely absent (any status.json
    written before this field existed); that reads as 'no heartbeat', the
    same as if it were missing today, never as an error. `output_liveness`
    omitted (pure callers, legacy callers) or unbound to the current live
    session for its role reads as 'no output', exactly today's behaviour.

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
    heartbeat_minutes = _minutes_since(bound_heartbeat_at(status), now)
    bound = output_liveness_for(status, output_liveness)
    output_minutes = _minutes_since(bound["output_at"], now) if bound else None
    limit = float(cfg.get("stall_minutes", 10))
    freshest = min(m for m in (updated_minutes, heartbeat_minutes, output_minutes) if m is not None)
    if freshest > limit:
        return f"no update in {updated_minutes:.0f} minutes (limit {limit:.0f}), consider escalating"
    return None


def _output_seconds_ago(bound: dict | None, now: datetime) -> int | None:
    """Whole seconds since a bound output record's `output_at`, truncated
    (never rounded, so two readers a few hundred milliseconds apart agree)
    and clamped at zero; None when there is no bound record."""
    seconds = _seconds_since(bound["output_at"], now) if bound else None
    if seconds is None:
        return None
    return max(int(seconds), 0)


def activity_note(status: dict, cfg: dict, *, now: datetime | None = None,
                  output_liveness: dict | None = None) -> str | None:
    """The other half of the same signal: a run that is alive but not
    currently progressing. Fires only in the specific cases that would
    otherwise look ambiguous -- `updated_at` is stale past `stall_minutes`,
    but the current live managed session produced output within
    `stall_minutes` (#41, 'Agent active; latest output N seconds ago'), or
    `last_heartbeat_at` is fresh -- so a caller (the dashboard, `status`)
    can say 'busy on a long background task' instead of leaving the
    operator to guess between 'stalled' and 'on course'. Mutually exclusive
    with `stall_warning`: whenever this returns non-None, `stall_warning`
    is guaranteed None, since fresh output or a fresh heartbeat is exactly
    what suppresses it. The output reading comes before the heartbeat one;
    `output_liveness` omitted or unbound reads as 'no output'.

    An open human pause takes precedence over both readings and renders as
    'waiting on <by> since <n> min ago[: <note>]', the one rendering both
    `status` and the dashboard snapshot show."""
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
    limit = float(cfg.get("stall_minutes", 10))
    bound = output_liveness_for(status, output_liveness)
    output_minutes = _minutes_since(bound["output_at"], now) if bound else None
    if (updated_minutes is not None and updated_minutes > limit
            and output_minutes is not None and output_minutes <= limit):
        return f"Agent active; latest output {_output_seconds_ago(bound, now)} seconds ago"
    heartbeat_minutes = _minutes_since(bound_heartbeat_at(status), now)
    if updated_minutes is None or heartbeat_minutes is None:
        return None
    if updated_minutes > limit and heartbeat_minutes <= limit:
        return (f"background task active (heartbeat {heartbeat_minutes:.0f} min ago); "
                f"no status update in {updated_minutes:.0f} minutes, but the run is alive")
    return None


PLAIN_COMMAND_MESSAGE = "test commands may not contain shell expansion or control operators"


def assert_plain_command(command: str) -> list[str]:
    """Refuse every construct that can manufacture a different command after
    validation; the allowed language is simple argv plus path globs. Shared by
    verify-live and, since the field-note fixes, by config load, so an operator
    in [checks].live_commands is refused at configuration time (defect 5)."""
    if not isinstance(command, str) or not command.strip():
        raise HandsoffError("test command must be a non-empty string")
    if re.search(r"[\$`;&|<>(){}\r\n]", command):
        raise HandsoffError(PLAIN_COMMAND_MESSAGE)
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise HandsoffError(f"invalid test command: {exc}") from exc
    if any(token in {"|", "||", "&&", ";", ">", ">>", "<"} for token in words):
        raise HandsoffError("test commands may not contain shell control operators")
    return words


def normalized_test_footprint(command: str, root: Path) -> frozenset[str]:
    """Return the repository-relative tests a command can execute.

    This is intentionally conservative: a command that names a tests directory,
    wildcard, discovery mode, or an unrecognised test runner is treated as broad.
    Handsoff only needs to distinguish configured focused checks from configured
    regression groups; it is not a general shell parser.
    """
    # Commands use a shell so configured test-path globs continue to work.
    words = assert_plain_command(command)
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


RELEASE_VERSION_PATTERN = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$")
RELEASE_CLASSES = ("patch", "minor", "major")


def classify_release_version(version: str) -> tuple[str, str]:
    """Normalize a semantic version and classify the intended release.

    The class is explicit in the version itself for this policy: any X.0.0
    is major, X.Y.0 is minor, and X.Y.Z is patch. 0.x still follows the
    same mechanical rule so agents cannot reinterpret policy ad hoc.
    """
    if not isinstance(version, str):
        raise HandsoffError("release version must be semantic X.Y.Z")
    match = RELEASE_VERSION_PATTERN.fullmatch(version.strip())
    if not match:
        raise HandsoffError("release version must be semantic X.Y.Z")
    major, minor, patch = (int(value) for value in match.groups())
    normalized = f"v{major}.{minor}.{patch}"
    release_class = "major" if minor == 0 and patch == 0 else "minor" if patch == 0 else "patch"
    return normalized, release_class


def release_plan_payload(cfg: dict, version: str, by: str, *, override_reason: str | None = None,
                         now: datetime | None = None) -> dict:
    normalized, release_class = classify_release_version(version)
    reason = override_reason.strip() if isinstance(override_reason, str) and override_reason.strip() else None
    major_only = cfg.get("regression_gate", {}).get("full_regression_major_only", True)
    eligible = not major_only or release_class == "major" or reason is not None
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    targeted = [{"command": command, "reason": "configured focused check"}
                for command in cfg.get("check_commands", [])]
    return {
        "version": normalized, "release_class": release_class, "planned_by": by,
        "planned_at": stamp, "full_regression_eligible": eligible,
        "full_regression_override_reason": reason,
        "targeted_checks": targeted,
        "regression_groups": [group.get("name") for group in cfg.get("regressions", [])],
    }


def validate_release_plan(plan: object) -> list[str]:
    if plan is None:
        return []
    required = {"version", "release_class", "planned_by", "planned_at",
                "full_regression_eligible", "full_regression_override_reason",
                "targeted_checks", "regression_groups"}
    if not isinstance(plan, dict) or set(plan) != required:
        return ["status: release_plan is invalid"]
    errors = []
    try:
        normalized, release_class = classify_release_version(plan.get("version"))
        if normalized != plan.get("version") or release_class != plan.get("release_class"):
            errors.append("status: release_plan version/class mismatch")
    except HandsoffError:
        errors.append("status: release_plan version is invalid")
    if not all(isinstance(plan.get(key), str) and plan[key].strip()
               for key in ("planned_by", "planned_at")):
        errors.append("status: release_plan identity is invalid")
    if not isinstance(plan.get("full_regression_eligible"), bool):
        errors.append("status: release_plan eligibility is invalid")
    override = plan.get("full_regression_override_reason")
    if override is not None and (not isinstance(override, str) or not override.strip()):
        errors.append("status: release_plan override reason is invalid")
    checks = plan.get("targeted_checks")
    if not isinstance(checks, list) or not all(
            isinstance(item, dict) and set(item) == {"command", "reason"}
            and all(isinstance(item.get(key), str) and item[key].strip() for key in ("command", "reason"))
            for item in checks):
        errors.append("status: release_plan targeted checks are invalid")
    groups = plan.get("regression_groups")
    if not isinstance(groups, list) or not all(isinstance(item, str) and item for item in groups):
        errors.append("status: release_plan regression groups are invalid")
    return errors


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


def removed_work_item_ids(acceptance: dict) -> set[str]:
    """Ids the Pilot removed with work-item-remove (#141 tombstones)."""
    out = set()
    for record in acceptance.get("removed_work_items") or []:
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            out.add(record["id"])
    return out


def add_work_item_tombstone(acceptance: dict, item_id: str, by: str, at: str) -> None:
    records = [r for r in (acceptance.get("removed_work_items") or []) if r.get("id") != item_id]
    records.append({"id": item_id, "by": by, "at": at})
    acceptance["removed_work_items"] = records


def clear_work_item_tombstones(acceptance: dict, item_ids) -> list[str]:
    """Delete the tombstones for `item_ids`; returns the ids that had one.
    Called by every path that re-adds an item on purpose, in the same
    commit, so a removal and a re-add can never both be on file."""
    wanted = set(item_ids)
    before = acceptance.get("removed_work_items") or []
    cleared = [r["id"] for r in before if r.get("id") in wanted]
    if cleared:
        acceptance["removed_work_items"] = [r for r in before if r.get("id") not in wanted]
    return cleared


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
    tagged: set[str] = set()
    for criterion in acceptance.get("criteria", []):
        item_id = criterion_work_item_id(criterion)
        if not item_id:
            continue
        tagged.add(item_id)
        if item_id.startswith("issue-"):
            number = int(item_id[6:])
            title = tickets.get(number, {}).get("title") or f"Issue #{number}"
            identities[item_id] = ("issue", number, title)
        else:
            title = item_id[4:].replace("-", " ").title()
            identities[item_id] = ("ask", None, title)
    # #141: an item the Pilot removed stays removed. The feature title still
    # names it, so title derivation would quietly bring it back on the next
    # transaction; the tombstone says the removal was a decision. A tagged
    # criterion or an explicit --item is the deliberate way back, and the
    # caller clears the tombstone in the same commit (clear_work_item_tombstones).
    explicit_ids = set()
    for item in explicit_items or []:
        issue = re.fullmatch(r"\s*#([1-9][0-9]{0,8})(?:\s+(.+?))?\s*", item)
        explicit_ids.add(f"issue-{int(issue.group(1))}" if issue else f"ask-{_work_item_slug(item)}")
    for item_id in removed_work_item_ids(acceptance):
        if item_id not in tagged and item_id not in explicit_ids:
            identities.pop(item_id, None)
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


def scoped_work_items(items: list[dict], criteria: list[dict] | None) -> list[dict]:
    """The items that actually carry acceptance criteria. Scope is what the
    criteria promise: an item nobody has tagged a criterion to (a spurious
    title-derived ask, an issue registered ahead of its criteria) is not
    part of what a reviewer or the Pilot judged, so adding or removing it
    must not invalidate their decisions (#82). With `criteria` None the
    whole registry counts, which is what pure-registry callers expect.
    Mirrors derive_work_items: untagged criteria attach to a single-item
    registry's only item."""
    if criteria is None:
        return list(items)
    ids = {item.get("id") for item in items}
    mapped: set[str] = set()
    for criterion in criteria:
        item_id = criterion_work_item_id(criterion)
        if item_id is None and len(items) == 1:
            item_id = items[0].get("id")
        if item_id in ids:
            mapped.add(item_id)
    return [item for item in items if item.get("id") in mapped]


def work_item_scope_hash(items: list[dict], criteria: list[dict] | None = None) -> str:
    scope = sorted(({"id": item.get("id"), "kind": item.get("kind"),
                    "number": item.get("number")} for item in scoped_work_items(items, criteria)),
                   key=lambda item: item["id"] or "")
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def work_item_scope_hashes(items: list[dict], criteria: list[dict] | None = None) -> dict[str, str]:
    """Current digest plus every historical formula a recorded decision may
    carry: the pre-v0.3.11 digest over all items with `required`, the same
    with `required` normalized to true, and the v0.3.11 identity-only
    digest over all items. Gates accept any of them so runs recorded by an
    earlier engine keep their approvals."""
    current = work_item_scope_hash(items, criteria)
    all_items = work_item_scope_hash(items)
    legacy_scope = sorted(({
        "id": item.get("id"), "kind": item.get("kind"),
        "number": item.get("number"), "required": item.get("required", True),
    } for item in items), key=lambda item: item["id"] or "")
    legacy = hashlib.sha256(json.dumps(legacy_scope, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    legacy_normalized_scope = sorted(({**item, "required": True} for item in legacy_scope),
                                     key=lambda item: item["id"] or "")
    legacy_normalized = hashlib.sha256(json.dumps(legacy_normalized_scope, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
    return {"current": current, "all_items": all_items, "legacy": legacy,
            "legacy_normalized": legacy_normalized}


def scope_hash_matches(recorded: object, items: list[dict], criteria: list[dict] | None = None) -> bool:
    """Accept any known digest so existing approvals remain valid after migration."""
    return recorded in work_item_scope_hashes(items, criteria).values()


def new_work_item_delivery(items: list[dict], lane: str = "full") -> dict:
    """Item-scoped delivery state for new runs.  Legacy runs omit this
    mapping and continue to use the existing run-level gates."""
    if lane not in ("full", "small-fix"):
        raise HandsoffError("lane must be full or small-fix")
    return {item["id"]: {
        "lane": lane, "requested_lane": lane, "confirmed_by": None,
        "confirmed_at": None, "facts": None, "escalation_reason": None,
        "implemented_by": None, "reviewed_by": None, "review_hash": None,
        "baseline_head": None,
    } for item in items}


def work_item_delivery(status: dict, item_id: str) -> dict:
    record = (status.get("work_item_delivery") or {}).get(item_id)
    if isinstance(record, dict):
        return record
    return {
        "lane": "full", "requested_lane": "full", "confirmed_by": None,
        "confirmed_at": None, "facts": None, "escalation_reason": None,
        "implemented_by": status.get("implemented_by"),
        "reviewed_by": status.get("reviewed_by"), "review_hash": None,
        "baseline_head": None,
    }


def item_criteria(acceptance: dict, item_id: str) -> list[dict]:
    registry = acceptance.get("work_items") or []
    return [criterion for criterion in acceptance.get("criteria", [])
            if criterion_work_item(criterion, registry) == item_id]


def item_acceptance_hash(acceptance: dict, item_id: str) -> str:
    return acceptance_hash(item_criteria(acceptance, item_id))


def repository_change_facts(root: Path) -> dict:
    """Conservative, reproducible review-time diff measurements."""
    changed: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--numstat", "HEAD", "--"], cwd=root,
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                added, removed, path = (line.split("\t", 2) + ["", "", ""])[:3]
                if not path:
                    continue
                changed[path] = (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
            capture_output=True, text=True, timeout=20, check=False,
        )
        if untracked.returncode == 0:
            for name in untracked.stdout.splitlines():
                path = root / name
                try:
                    changed[name] = len(path.read_text(encoding="utf-8").splitlines())
                except (OSError, UnicodeDecodeError):
                    changed[name] = 0
    except (OSError, subprocess.SubprocessError):
        return {"changed_lines": None, "changed_files": None, "paths": [],
                "governance_paths": ["measurement_unavailable"]}
    paths = sorted(changed)
    governance = [path for path in paths if path == "handsoff.toml"
                  or path.startswith("schemas/") or path.startswith("prompts/")
                  or path.startswith("bin/handsoff_")]
    return {"changed_lines": sum(changed.values()), "changed_files": len(paths),
            "paths": paths, "governance_paths": governance}


def small_fix_facts(root: Path, acceptance: dict, item_id: str, cfg: dict,
                    *, change_facts: dict | None = None) -> dict:
    criteria = item_criteria(acceptance, item_id)
    diff = change_facts if change_facts is not None else repository_change_facts(root)
    facts = {
        "criteria": len(criteria),
        "primary_fixes": sum(c.get("type") == "primary_fix" for c in criteria),
        **diff,
        "caps": {
            "criteria": cfg["small_fix_max_criteria"],
            "changed_lines": cfg["small_fix_max_changed_lines"],
            "changed_files": cfg["small_fix_max_files"],
        },
    }
    reasons = []
    if facts["criteria"] > facts["caps"]["criteria"]:
        reasons.append(f"{facts['criteria']} criteria over {facts['caps']['criteria']} cap")
    if facts["primary_fixes"] != 1:
        reasons.append(f"requires exactly one primary_fix (found {facts['primary_fixes']})")
    if facts["changed_lines"] is None:
        reasons.append("diff measurement unavailable")
    elif facts["changed_lines"] > facts["caps"]["changed_lines"]:
        reasons.append(f"{facts['changed_lines']} changed lines over {facts['caps']['changed_lines']} cap")
    if facts["changed_files"] is None:
        reasons.append("file measurement unavailable")
    elif facts["changed_files"] > facts["caps"]["changed_files"]:
        reasons.append(f"{facts['changed_files']} changed files over {facts['caps']['changed_files']} cap")
    if facts["governance_paths"]:
        reasons.append("governance paths changed: " + ", ".join(facts["governance_paths"]))
    facts["eligible"] = not reasons
    facts["reasons"] = reasons
    return facts


def item_progress(status: dict, acceptance: dict, cfg: dict, item_id: str) -> dict:
    own = item_criteria(acceptance, item_id)
    delivery = work_item_delivery(status, item_id)
    passing = sum(c.get("state") == "passing" for c in own)
    criteria_points = 60.0 * passing / len(own) if own else 0.0
    lane_gate = (bool(delivery.get("confirmed_by")) if delivery.get("lane") == "small-fix"
                 else not _design_errors(status, acceptance, cfg)
                 and not _design_review_errors(status, acceptance, cfg))
    implemented_by = delivery.get("implemented_by")
    implemented = bool(implemented_by and any(c.get("evidence") for c in own))
    item_hash = item_acceptance_hash(acceptance, item_id)
    reviewed = bool(delivery.get("reviewed_by") and delivery.get("review_hash") == item_hash)
    global_review = status.get("review") or {}
    if global_review.get("acceptance_hash") == acceptance_hash(acceptance.get("criteria", [])):
        reviewed = True
    approval = status.get("deployment_approved") or {}
    deployed = (not cfg.get("deployment_requires_explicit_approval", True)
                or approval.get("acceptance_hash") == acceptance_hash(acceptance.get("criteria", [])))
    live = not cfg.get("require_live_verification", True) or bool(status.get("live_verification_id"))
    gates = {"lane": lane_gate, "implemented": implemented, "reviewed": reviewed,
             "deployed": deployed, "live": live}
    # #102: gate weights, not phase weights. An approved design reads 25,
    # partial evidence climbs from 25 to 45 with the passing fraction, and
    # each later gate lands on its own step, so a run never reads 8 percent
    # with its design fully approved.
    # A small-fix item has no design review; its lane confirmation is the
    # equivalent step.
    design_reviewed = (lane_gate if delivery.get("lane") == "small-fix"
                       else not _design_review_errors(status, acceptance, cfg))
    fraction = passing / len(own) if own else 0.0
    symptom = bool((status.get("requirement_coverage") or {}).get("original_symptom_resolved")
                   or status.get("original_symptom_evidence_id"))
    phase = int(status.get("phase_number", 0) or 0)
    value = 5
    if design_reviewed:
        value = 15
    if lane_gate:
        value = 25 + int(math.floor(20 * fraction + 0.5))
    if own and passing == len(own) and lane_gate:
        value = 45
        if symptom:
            value = 50
        if reviewed:
            value = 65
        # A gate the project switched off clears with the phase that would
        # have asked for it, never ahead of the run (the 95 percent step is
        # reserved for a work item that is actually done).
        if reviewed and deployed and phase >= 7:
            value = 80
        if reviewed and deployed and live and phase >= 8:
            value = 95
    if status.get("status") == "complete" or phase >= 8 and reviewed and deployed and live:
        value = 100
    value = min(100, max(0, value))
    return {"percent": value, "passing": passing, "total": len(own), "gates": gates,
            "lane": delivery.get("lane", "full"), "facts": delivery.get("facts"),
            "escalation_reason": delivery.get("escalation_reason")}


def overall_item_progress(status: dict, acceptance: dict, cfg: dict) -> int:
    items, _ = effective_work_items(acceptance, cfg)
    values = [item_progress(status, acceptance, cfg, item["id"])["percent"]
              for item in items if item.get("required", True)]
    return int(math.floor(sum(values) / len(values) + 0.5)) if values else 0


def full_design_required(status: dict, acceptance: dict, cfg: dict) -> bool:
    items, _ = effective_work_items(acceptance, cfg)
    for item in items:
        if not item.get("required", True):
            continue
        delivery = work_item_delivery(status, item["id"])
        if delivery.get("lane") != "small-fix" or not delivery.get("confirmed_by"):
            return True
    return False


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
        elif item_id is not None:
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
        progress_view = item_progress(status, acceptance, cfg, item["id"])
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
        elif item.get("required", True) and not own:
            state = "unscoped"
            blocker = "Required work item has no mapped acceptance criteria"
        elif progress_view["percent"] == 100:
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
                         "lane": progress_view["lane"], "progress": progress_view["percent"],
                         "lane_confirmed": bool(work_item_delivery(status, item["id"]).get("confirmed_by")),
                         "lane_facts": progress_view["facts"],
                         "lane_escalation": progress_view["escalation_reason"],
                         "phase_or_next": status.get("next_action") or status.get("phase"),
                         "blocker": blocker, "discrepancy": discrepancy})
    counts = {state: sum(item["status"] == state for item in rendered) for state in WORK_ITEM_STATES}
    unattributed_criteria = [c.get("id") for c in mapping.get("unattributed", [])]
    return {"multi": len(rendered) > 1, "registry": source,
            "tickets_config_deprecated": bool(cfg.get("tickets")) and source == "persisted",
            "unattributed_criteria": unattributed_criteria,
            "items": rendered, "aggregate": {"total": len(rendered), "done": counts["done"],
                                               "counts": counts,
                                               "progress": overall_item_progress(status, acceptance, cfg)}}


def work_item_checkpoints(status: dict, acceptance: dict, events: list[dict] | None = None,
                          verifications: list[dict] | None = None, cfg: dict | None = None) -> dict:
    """#104: durable per-item checkpoints derived from the ledger only,
    never stored, so they cannot drift from the run they describe.

    designed: a design approval is recorded. implemented: implemented_by is
    on the item's delivery record, or every tagged criterion carries
    evidence. verified: every tagged criterion is passing. reviewed: an
    approved review recorded after the newest verification for the item.
    complete: all four."""
    cfg = cfg or DEFAULT_CONFIG
    registry, _ = effective_work_items(acceptance, cfg)
    criteria = acceptance.get("criteria", [])
    delivery = status.get("work_item_delivery") if isinstance(status.get("work_item_delivery"), dict) else {}
    designed = isinstance(status.get("design_approved"), dict)
    review = status.get("review") if isinstance(status.get("review"), dict) else None
    review_at = review.get("at") if review else None
    result = {}
    for item in registry:
        own = [c for c in criteria if criterion_work_item_id(c) == item["id"]
               or (criterion_work_item_id(c) is None and len(registry) == 1)]
        record = delivery.get(item["id"]) if isinstance(delivery, dict) else None
        implemented = bool(isinstance(record, dict) and record.get("implemented_by")) \
            or (bool(own) and all(c.get("evidence") for c in own))
        verified = bool(own) and all(c.get("state") == "passing" for c in own)
        newest_evidence = None
        for record_v in verifications or []:
            if any(cid in (record_v.get("criteria") or []) for cid in (c.get("id") for c in own)):
                newest_evidence = max(newest_evidence or "", record_v.get("at") or "")
        reviewed = bool(review_at) and verified and (newest_evidence is None or review_at >= newest_evidence)
        result[item["id"]] = {
            "designed": designed, "implemented": implemented, "verified": verified, "reviewed": reviewed,
            "complete": designed and implemented and verified and reviewed,
            "criteria": [c.get("id") for c in own],
            "passing": [c.get("id") for c in own if c.get("state") == "passing"],
        }
    return result


def work_item_completion_lines(checkpoints: dict) -> list[str]:
    """One human line per item: 'issue-102 implemented, verified' or
    'issue-104 not started'."""
    lines = []
    for item_id, point in checkpoints.items():
        if point["complete"]:
            lines.append(f"{item_id} complete")
            continue
        stages = [name for name in ("designed", "implemented", "verified", "reviewed") if point[name]]
        lines.append(f"{item_id} {', '.join(stages)}" if stages else f"{item_id} not started")
    return lines


def resume_scope_section(root: Path) -> str:
    """#104: the completed and remaining scope for a relaunched implementer
    or reviewer, so the resumed attempt continues with what is unfinished."""
    cfg = load_config(root)
    status = load_unique_json(status_path(root, cfg))
    acceptance = load_unique_json(acceptance_path(root, cfg))
    try:
        verifications, _ = load_verifications(root, cfg)
    except HandsoffError:
        verifications = []
    points = work_item_checkpoints(status, acceptance, None, verifications, cfg)
    if not points:
        return ""
    done = [f"- {item_id} (criteria passing: {', '.join(p['passing']) or 'none'})" for item_id, p in points.items() if p["complete"] or p["verified"]]
    remaining = [f"- {item_id}: {', '.join(c for c in p['criteria'] if c not in p['passing']) or 'no open criteria'}"
                 for item_id, p in points.items() if not (p["complete"] or p["verified"])]
    return ("# Completed scope\n\nDo not repeat this work; its evidence is on the ledger.\n\n"
            + ("\n".join(done) or "- none yet")
            + "\n\n# Remaining scope\n\n" + ("\n".join(remaining) or "- nothing remains"))


# --------------------------------------------------------------------------
# #44: atomic acceptance-criteria transactions. One validator serves
# criterion-add/update/remove and criteria-apply; the planner applies a
# whole operation list to a deep copy of the registry and either returns a
# complete plan (per-operation spec hashes, registry and design hashes
# before and after, derived work items) or raises with the offending
# operation named, so the caller writes everything or nothing.
# --------------------------------------------------------------------------

CRITERION_TYPES = ("primary_fix", "supporting")
CRITERION_SETTABLE_STATES = ("failing", "not_tested", "blocked")
CRITERION_ADD_FIELDS = ("id", "type", "requirement", "verification", "tests")
CRITERION_UPDATE_FIELDS = ("requirement", "verification", "tests", "type", "state",
                           # #165: a criterion may declare that no failing run can exist
                           # for it (a test born with the feature), with the reason audited
                           "baseline", "baseline_reason",
                           # #169: N green runs in a row, with a seed per attempt
                           "repeat", "seed_env")
MAX_REPEAT = 50
CRITERIA_TRANSACTION_OPS = ("add", "update", "remove")
MAX_CRITERIA_TRANSACTION_OPERATIONS = 64
MAX_CRITERIA_TRANSACTION_BYTES = 256 * 1024


class CriteriaTransactionError(HandsoffError):
    """A refused operation. `str()` yields the documented refusal text
    `operation N (<op> <id>): <reason>`, with N counted from 1 in file
    order, so the supervisor's generic SHIP_FEATURE_BLOCKED prefix
    completes the message without a second formatting path."""

    def __init__(self, index: int, op: object, criterion_id: object, reason: str):
        self.index = index
        self.op = op if isinstance(op, str) and op else "?"
        self.criterion_id = criterion_id if isinstance(criterion_id, str) and criterion_id else "?"
        self.reason = reason
        super().__init__(f"operation {index} ({self.op} {self.criterion_id}): {reason}")


def validate_criterion_fields(fields: dict, *, require_all: bool = False) -> list[str]:
    """The one field validator behind criterion-add, criterion-update, and
    criteria-apply. `require_all` is the add form: id, type, requirement,
    verification, and a non-empty tests list must all be present. Without
    it (the update and remove forms) any subset of the fields is checked,
    `id` included; whether a subset may be empty is the caller's rule.
    Returns a list of problems."""
    if not isinstance(fields, dict):
        return ["criterion fields must be an object"]
    allowed = set(CRITERION_ADD_FIELDS) | set(CRITERION_UPDATE_FIELDS)
    unknown = sorted(set(fields) - allowed)
    if unknown:
        return [f"unknown criterion field(s): {', '.join(unknown)}"]
    errors: list[str] = []
    if require_all:
        missing = [field for field in CRITERION_ADD_FIELDS if field not in fields]
        if missing:
            errors.append(f"missing criterion field(s): {', '.join(missing)}")
        if "state" in fields:
            errors.append("a new criterion may not set 'state'; it starts not_tested")
    if "id" in fields and (not isinstance(fields["id"], str) or not fields["id"].strip()):
        errors.append("'id' must be a non-empty string")
    if "type" in fields and fields["type"] not in CRITERION_TYPES:
        errors.append(f"'type' must be one of {', '.join(CRITERION_TYPES)}")
    if "requirement" in fields and (not isinstance(fields["requirement"], str)
                                    or not fields["requirement"].strip()):
        errors.append("'requirement' must be a non-empty string")
    if "verification" in fields and fields["verification"] not in VERIFICATION_REQUIREMENTS:
        errors.append(f"'verification' must be one of {', '.join(VERIFICATION_REQUIREMENTS)}")
    if "tests" in fields:
        tests = fields["tests"]
        if not isinstance(tests, list) or not tests \
                or not all(isinstance(test, str) and test.strip() for test in tests):
            errors.append("'tests' must be a non-empty list of non-empty strings")
    if "state" in fields and fields["state"] not in CRITERION_SETTABLE_STATES:
        errors.append(f"'state' must be one of {', '.join(CRITERION_SETTABLE_STATES)}")
    if "baseline" in fields and fields["baseline"] not in (BASELINE_NOT_APPLICABLE, None):
        errors.append(f"'baseline' may only be {BASELINE_NOT_APPLICABLE} (or absent)")
    if fields.get("baseline") == BASELINE_NOT_APPLICABLE and not str(fields.get("baseline_reason") or "").strip():
        errors.append("'baseline_reason' is required with baseline not_applicable")
    if "baseline_reason" in fields and fields.get("baseline") != BASELINE_NOT_APPLICABLE:
        errors.append("'baseline_reason' needs baseline not_applicable")
    if "repeat" in fields and fields["repeat"] is not None and (
            not isinstance(fields["repeat"], int) or isinstance(fields["repeat"], bool)
            or not 1 <= fields["repeat"] <= MAX_REPEAT):
        errors.append(f"'repeat' must be an integer from 1 to {MAX_REPEAT}")
    if "seed_env" in fields and fields["seed_env"] is not None and (
            not isinstance(fields["seed_env"], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", fields["seed_env"])):
        errors.append("'seed_env' must be an environment variable name (A-Z, 0-9, _)")
    if fields.get("seed_env") and not fields.get("repeat"):
        errors.append("'seed_env' needs repeat")
    return errors


def repeat_seed(run_hash: str | None, attempt: int) -> str:
    """#169: a distinct, reproducible seed per attempt."""
    return hashlib.sha256(f"{run_hash or ''}:{attempt}".encode("utf-8")).hexdigest()[:8]


def run_repeated_checks(cfg: dict, root: Path, commands: list[str], repeat: int, seed_env: str | None,
                        run_hash: str | None, timeout: int | None = None) -> tuple[list[dict], list[dict]]:
    """#169: run the criterion's commands `repeat` times in sequence. Returns
    (results of the last attempt, attempts) where each attempt carries its
    number, exit codes, output hashes, seed and, for a failing attempt, the
    output tail. Stops at the first failing attempt: the record names it."""
    attempts: list[dict] = []
    last: list[dict] = []
    for number in range(1, repeat + 1):
        seed = repeat_seed(run_hash, number) if seed_env else None
        env = {seed_env: seed} if seed_env else None
        last = run_checks(cfg, root, commands=commands, timeout=timeout, env=env)
        ok = all(r["exit_code"] == 0 for r in last)
        attempts.append({"attempt": number, "ok": ok, "seed": seed,
                         "exit_codes": [r["exit_code"] for r in last],
                         "output_sha256": [r["output_sha256"] for r in last],
                         **({} if ok else {"output_tail": last[-1]["output_tail"][-2000:]})})
        if not ok:
            break
    return last, attempts


def sync_work_item_registry(acceptance: dict, cfg: dict) -> bool:
    """Append newly declared criterion identities to a persisted registry
    without deleting stable promises. A legacy registry (no persisted
    `work_items`) is left alone: its items stay derived on read."""
    existing = acceptance.get("work_items")
    if not isinstance(existing, list):
        return False
    known = {item.get("id") for item in existing}
    criterion_ids = {criterion_work_item_id(criterion)
                     for criterion in acceptance.get("criteria", [])}
    criterion_ids.discard(None)
    has_persisted_issue = any(item.get("kind") == "issue" for item in existing)
    changed = False
    for item in derive_work_item_registry(acceptance, cfg):
        if item["id"] not in known:
            if has_persisted_issue and item.get("kind") == "ask" and item["id"] not in criterion_ids:
                continue
            existing.append(item)
            known.add(item["id"])
            changed = True
    existing.sort(key=lambda item: (item["kind"] != "issue", item.get("number") or 0, item["id"]))
    return changed


def load_criteria_transaction(path: Path) -> list[dict]:
    """Read TX.json: an object with exactly `operations`, 1 to 64 entries,
    at most 256 KiB, duplicate keys refused. Operation shapes are checked
    by the planner, which names the offending operation."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HandsoffError(f"transaction file: cannot read {path}: {exc}") from exc
    if size > MAX_CRITERIA_TRANSACTION_BYTES:
        raise HandsoffError(f"transaction file: {path} exceeds {MAX_CRITERIA_TRANSACTION_BYTES} bytes")
    body = load_unique_json(path)
    if not isinstance(body, dict) or set(body) != {"operations"}:
        raise HandsoffError("transaction file: top-level value must be an object with exactly 'operations'")
    operations = body["operations"]
    if not isinstance(operations, list):
        raise HandsoffError("transaction file: 'operations' must be an array")
    if not 1 <= len(operations) <= MAX_CRITERIA_TRANSACTION_OPERATIONS:
        raise HandsoffError(
            f"transaction file: 'operations' must contain 1 to {MAX_CRITERIA_TRANSACTION_OPERATIONS} entries"
        )
    return operations


def _transaction_test_gate(index: int, op: str, criterion: dict, cfg: dict, root: Path) -> None:
    """For a criterion `verify` will run (its policy requires `checks`):
    every tests entry must be one of [checks].commands, the rule `verify`
    applies at run time, and must pass the #28 footprint gate exactly as
    `run_checks` applies it (no shell control operators, never a command
    that captures a gated regression group), so a transaction can never
    register a test that verification would later refuse. Manual and
    browser criteria carry attestation descriptions, never commands, and
    are not gated here, matching the single commands."""
    if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
        return
    regression_commands = configured_regression_commands(cfg)
    for test in criterion.get("tests", []):
        if test not in cfg.get("check_commands", []):
            raise CriteriaTransactionError(
                index, op, criterion.get("id"),
                f"automated test {test!r} is not one of [checks].commands",
            )
        try:
            footprint = normalized_test_footprint(test, root)
        except HandsoffError as exc:
            raise CriteriaTransactionError(index, op, criterion.get("id"), f"test {test!r}: {exc}") from exc
        for gated in regression_commands:
            gated_footprint = normalized_test_footprint(gated, root)
            if "*" in footprint or ("*" not in gated_footprint and gated_footprint <= set(footprint)):
                raise CriteriaTransactionError(
                    index, op, criterion.get("id"),
                    f"test {test!r} captures gated regression group commands; "
                    "full regressions go through regression-request",
                )


def plan_criteria_transaction(acceptance: dict, cfg: dict, operations: list[dict], *,
                              root: Path | None = None) -> dict:
    """Apply `operations` in order to a deep copy of `acceptance` and return
    the plan, or raise CriteriaTransactionError (HandsoffError) naming the
    first refused operation. Nothing passed in is mutated.

    The plan carries `operations` ([{op, id, previous_hash, resulting_hash}]),
    `operation_count`, `registry_hash_before`/`registry_hash_after`
    (`acceptance_hash`), `design_hash_before`/`design_hash_after`,
    `work_items_after` (effective work item ids), `work_item_scope_changed`,
    `resets_original_symptom` (a primary_fix spec was added or changed),
    and the full `criteria_after` and `work_items_registry_after` that
    `apply_criteria_plan` writes. `root` is where test footprints resolve
    their globs; it defaults to the current directory."""
    root = root if root is not None else Path.cwd()
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_CRITERIA_TRANSACTION_OPERATIONS:
        raise HandsoffError(
            f"transaction: 'operations' must contain 1 to {MAX_CRITERIA_TRANSACTION_OPERATIONS} entries"
        )
    before_criteria = acceptance.get("criteria")
    if not isinstance(before_criteria, list):
        raise HandsoffError("transaction: acceptance registry has no criteria list")
    planned = deepcopy(acceptance)
    criteria: list[dict] = planned["criteria"]
    before_items, _ = effective_work_items(acceptance, cfg)
    before_scope = work_item_scope_hash(before_items, before_criteria)
    seen_ids: set[str] = set()
    records: list[dict] = []
    resets_symptom = False
    last_primary_touch: int | None = None

    def lookup(criterion_id: str) -> dict | None:
        return next((c for c in criteria if c.get("id") == criterion_id), None)

    for position, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise CriteriaTransactionError(position, None, None, "operation must be an object")
        op = operation.get("op")
        if op not in CRITERIA_TRANSACTION_OPS:
            raise CriteriaTransactionError(position, op, operation.get("id"),
                                           f"'op' must be one of {', '.join(CRITERIA_TRANSACTION_OPS)}")
        if op == "add":
            if set(operation) != {"op", "criterion"}:
                raise CriteriaTransactionError(position, op, None,
                                               "an add operation has exactly 'op' and 'criterion'")
            spec = operation["criterion"]
            if not isinstance(spec, dict) or set(spec) != set(CRITERION_ADD_FIELDS):
                raise CriteriaTransactionError(
                    position, op, spec.get("id") if isinstance(spec, dict) else None,
                    "an add criterion has exactly id, type, requirement, verification, tests",
                )
            criterion_id = spec.get("id")
            problems = validate_criterion_fields(spec, require_all=True)
            if problems:
                raise CriteriaTransactionError(position, op, criterion_id, "; ".join(problems))
            if criterion_id in seen_ids:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "criterion id appears in an earlier operation")
            if lookup(criterion_id) is not None:
                raise CriteriaTransactionError(position, op, criterion_id, "criterion already exists")
            seen_ids.add(criterion_id)
            criterion = {
                "id": criterion_id, "type": spec["type"], "requirement": spec["requirement"],
                "verification": spec["verification"], "tests": list(spec["tests"]),
                "evidence": [], "state": "not_tested",
            }
            _transaction_test_gate(position, op, criterion, cfg, root)
            criteria.append(criterion)
            if criterion["type"] == "primary_fix":
                resets_symptom = True
                last_primary_touch = position
            records.append({"op": op, "id": criterion_id, "previous_hash": None,
                            "resulting_hash": criterion_spec_hash(criterion)})
            continue
        criterion_id = operation.get("id")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise CriteriaTransactionError(position, op, criterion_id, "'id' must be a non-empty string")
        if op == "update":
            if set(operation) != {"op", "id", "fields"}:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "an update operation has exactly 'op', 'id' and 'fields'")
        elif set(operation) != {"op", "id"}:
            raise CriteriaTransactionError(position, op, criterion_id,
                                           "a remove operation has exactly 'op' and 'id'")
        if criterion_id in seen_ids:
            raise CriteriaTransactionError(position, op, criterion_id,
                                           "criterion id appears in an earlier operation")
        seen_ids.add(criterion_id)
        criterion = lookup(criterion_id)
        if criterion is None:
            raise CriteriaTransactionError(position, op, criterion_id, "unknown criterion")
        previous_hash = criterion_spec_hash(criterion)
        if op == "remove":
            if len(criteria) == 1:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "acceptance registry must retain at least one criterion")
            if criterion.get("type") == "primary_fix":
                last_primary_touch = position
            criteria[:] = [c for c in criteria if c is not criterion]
            records.append({"op": op, "id": criterion_id, "previous_hash": previous_hash,
                            "resulting_hash": None})
            continue
        fields = operation["fields"]
        if not isinstance(fields, dict) or not fields or set(fields) - set(CRITERION_UPDATE_FIELDS):
            raise CriteriaTransactionError(
                position, op, criterion_id,
                "'fields' must be a non-empty object with keys among requirement, verification, tests, type, state",
            )
        problems = validate_criterion_fields(fields)
        if problems:
            raise CriteriaTransactionError(position, op, criterion_id, "; ".join(problems))
        was_primary = criterion.get("type") == "primary_fix"
        spec_changed = False
        for field in ("requirement", "verification", "type"):
            if field in fields and criterion.get(field) != fields[field]:
                criterion[field] = fields[field]
                spec_changed = True
        if "tests" in fields:
            criterion["tests"] = list(fields["tests"])
            spec_changed = True
        if "state" in fields:
            criterion["state"] = fields["state"]
        if spec_changed or "state" in fields:
            criterion["evidence"] = []
            if "state" not in fields:
                criterion["state"] = "not_tested"
        if "tests" in fields or "verification" in fields:
            _transaction_test_gate(position, op, criterion, cfg, root)
        if spec_changed and (was_primary or criterion.get("type") == "primary_fix"):
            resets_symptom = True
        if was_primary or criterion.get("type") == "primary_fix":
            last_primary_touch = position
        records.append({"op": op, "id": criterion_id, "previous_hash": previous_hash,
                        "resulting_hash": criterion_spec_hash(criterion)})

    primary_count = sum(1 for c in criteria if c.get("type") == "primary_fix")
    if primary_count != 1:
        culprit = last_primary_touch or len(operations)
        culprit_record = records[culprit - 1]
        raise CriteriaTransactionError(
            culprit, culprit_record["op"], culprit_record["id"],
            f"the resulting registry would have {primary_count} primary_fix criteria; exactly one is required",
        )
    errors = validate_acceptance_schema(planned)
    if errors:
        culprit = len(operations)
        for record in reversed(records):
            if any(f"criterion {record['id']} " in error for error in errors):
                culprit = records.index(record) + 1
                break
        culprit_record = records[culprit - 1]
        raise CriteriaTransactionError(culprit, culprit_record["op"], culprit_record["id"],
                                       "resulting registry fails validation: " + "; ".join(errors))
    registry_changed = sync_work_item_registry(planned, cfg)
    after_items, _ = effective_work_items(planned, cfg)
    return {
        "operations": records,
        "operation_count": len(records),
        "registry_hash_before": acceptance_hash(before_criteria),
        "registry_hash_after": acceptance_hash(criteria),
        "design_hash_before": design_hash(before_criteria),
        "design_hash_after": design_hash(criteria),
        "work_items_after": [item["id"] for item in after_items],
        "work_item_scope_changed": before_scope != work_item_scope_hash(after_items, criteria),
        "resets_original_symptom": resets_symptom,
        "criteria_after": criteria,
        "work_items_registry_after": planned.get("work_items") if isinstance(planned.get("work_items"), list) else None,
    }


def criteria_plan_preview(plan: dict, status: dict) -> dict:
    """The dry-run view: the plan's hashes and operations plus what an apply
    would invalidate on this status, never the bulky registry itself."""
    flagged = bool(status.get("requires_design_approval") or status.get("requires_design_review"))
    return {
        "operations": deepcopy(plan["operations"]),
        "operation_count": plan["operation_count"],
        "registry_hash_before": plan["registry_hash_before"],
        "registry_hash_after": plan["registry_hash_after"],
        "design_hash_before": plan["design_hash_before"],
        "design_hash_after": plan["design_hash_after"],
        "work_items_after": list(plan["work_items_after"]),
        "work_item_scope_changed": plan["work_item_scope_changed"],
        "would_invalidate": {
            "design": bool(status.get("design_approved") or status.get("design_review")),
            "review": status.get("review") is not None,
            "deployment": status.get("deployment_approved") is not None,
            "live": status.get("live_verification_id") is not None,
        },
        "would_roll_back_to_phase": 2 if flagged and int(status.get("phase_number", 1) or 1) >= 3 else None,
    }


def apply_criteria_plan(acceptance: dict, plan: dict) -> None:
    """Write a plan's resulting registry into `acceptance` in place. Refuses
    a plan built against a different registry than the one on hand, so a
    stale plan (the file changed between plan and apply) can never land."""
    current = acceptance_hash(acceptance.get("criteria", []))
    if current != plan.get("registry_hash_before"):
        raise HandsoffError("transaction: the acceptance registry changed since the plan was built")
    acceptance["criteria"] = deepcopy(plan["criteria_after"])
    # #141: a criterion tagged [#N] is the deliberate way to bring a removed
    # item back, so its tombstone goes in this same write.
    tagged = {criterion_work_item_id(c) for c in acceptance["criteria"]} - {None}
    clear_work_item_tombstones(acceptance, tagged)
    if plan.get("work_items_registry_after") is not None:
        acceptance["work_items"] = deepcopy(plan["work_items_registry_after"])


# --------------------------------------------------------------------------
# #42: the scoped post-approval amendment lane. After a design is approved
# and the run has left Phase 2, a small correction to already-approved
# criteria no longer has to throw the whole design away. `plan_amendment`
# reuses the #44 planner for the operation delta and then CLASSIFIES it:
# `scoped` (apply, freeze the phase, review, Pilot approval, resume) or
# `full_redesign` (refuse to open; the ordinary criteria-apply path with
# its Phase 2 rollback is the only way through). The planner decides;
# the caller cannot choose the classification.
# --------------------------------------------------------------------------

AMENDMENT_ID_PATTERN = re.compile(r"^am-[0-9a-f]{32}$")
AMENDMENT_STATES = ("open", "approved", "escalated", "rejected")
AMENDMENT_CLASSIFICATIONS = ("scoped", "full_redesign")
AMENDMENT_REVIEW_DECISIONS = ("approved", "changes_requested")
MAX_AMENDMENT_HISTORY = 16
MAX_AMENDMENT_LIST = 1024
AMENDMENT_FIELDS = {
    "amendment_id", "opened_at", "by", "base_design_hash", "base_scope_hash",
    "changed_ids", "dependent_ids", "affected_work_items", "operations",
    "amendment_hash", "resulting_design_hash", "classification", "classification_reasons",
    "frozen_phase", "frozen_progress", "review", "pilot_approval", "state", "closed_at",
}
AMENDMENT_REVIEW_FIELDS = {"by", "at", "decision", "summary", "amendment_hash"}
#: #146: a request-changes review may carry the reviewer's findings, and a
#: verdict adopted from a terminal session names that session and adopter.
AMENDMENT_REVIEW_OPTIONAL_FIELDS = {"findings", "adopted_session", "adopted_by"}
MAX_AMENDMENT_REVIEW_FINDINGS = 32
AMENDMENT_PILOT_APPROVAL_FIELDS = {"by", "at", "amendment_hash"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def open_amendment(status: dict) -> dict | None:
    """The open amendment record on `status`, or None. Only state `open`
    counts; a closed record left in `status["amendment"]` by a hand edit is
    a schema error, never a silent freeze."""
    record = status.get("amendment") if isinstance(status, dict) else None
    return record if isinstance(record, dict) and record.get("state") == "open" else None


def amendment_hash(base_design_hash: str, operations: list[dict]) -> str:
    """sha256 of the base design hash concatenated with the canonical
    operation records ({op, id, previous_hash, resulting_hash}, in order).
    Bound into the review and the Pilot approval, and recomputed at approve
    time, so a delta that drifted after review can never be approved."""
    return hashlib.sha256(
        (str(base_design_hash) + _canonical({"operations": operations})).encode("utf-8")
    ).hexdigest()


def criterion_work_item(criterion: dict, registry: list[dict]) -> str:
    """The work item a criterion belongs to, by the same rule
    derive_work_items renders: its leading tag, else the only item of a
    single-item run, else `unattributed`."""
    item_id = criterion_work_item_id(criterion)
    if item_id is None and len(registry) == 1:
        return registry[0]["id"]
    known = {item.get("id") for item in registry}
    return item_id if item_id in known else "unattributed"


def _amendment_changed_ids(operations: list[dict]) -> list[str]:
    return [record["id"] for record in operations if record.get("op") in ("update", "remove")]


def amendment_dependent_ids(criteria: list[dict], changed_ids: list[str], registry: list[dict]) -> list[str]:
    """Criteria in the same work item as a changed criterion whose
    requirement text names a changed criterion id. A cheap, deterministic
    dependency signal (documented as such): it flags what the Reviewer
    should re-read, it never widens the freeze or the evidence reset."""
    by_id = {c.get("id"): c for c in criteria}
    changed = [cid for cid in changed_ids if cid in by_id]
    changed_items = {criterion_work_item(by_id[cid], registry) for cid in changed}
    dependents = []
    for criterion in criteria:
        cid = criterion.get("id")
        if cid in changed_ids or criterion_work_item(criterion, registry) not in changed_items:
            continue
        text = str(criterion.get("requirement") or "")
        if any(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(other) + r"(?![A-Za-z0-9_-])", text)
               for other in changed):
            dependents.append(cid)
    return dependents


def _verification_downgrade(before: dict, after: dict) -> str | None:
    """Gate weakening, by policy level: `automated_and_browser` to anything
    else, `automated` to `manual` or `browser`, or an automated criterion
    losing tests with none added (a proper subset of the earlier list, or
    an empty list)."""
    old_policy, new_policy = before.get("verification"), after.get("verification")
    if old_policy == "automated_and_browser" and new_policy != old_policy:
        return f"verification downgrade {old_policy} -> {new_policy}"
    if old_policy == "automated" and new_policy in ("manual", "browser"):
        return f"verification downgrade {old_policy} -> {new_policy}"
    old_tests, new_tests = list(before.get("tests") or []), list(after.get("tests") or [])
    if "checks" in VERIFICATION_REQUIREMENTS.get(new_policy, set()) and old_tests \
            and (not new_tests or (set(new_tests) < set(old_tests))):
        return "tests removed without replacement"
    return None


def classify_amendment(acceptance: dict, cfg: dict, plan: dict, *,
                       cumulative_changed_ids: list[str] | None = None,
                       request_full_redesign: bool = False) -> tuple[str, list[str]]:
    """Decide `scoped` or `full_redesign` for a planned delta, and say why.
    `full_redesign` when any of: an add (added scope); an operation
    touching a primary_fix criterion's type or changing which criterion is
    primary; a verification downgrade; the changed criteria spanning more
    than one derived work item (over `cumulative_changed_ids` on a revise,
    so a second transaction cannot smuggle in a second item); the
    work-item scope changing; or `--request-full-redesign`. Otherwise
    `scoped`. Deterministic and pure."""
    before_by_id = {c.get("id"): c for c in acceptance.get("criteria", [])}
    after_by_id = {c.get("id"): c for c in plan["criteria_after"]}
    registry, _ = effective_work_items(acceptance, cfg)
    reasons: list[str] = []
    for position, record in enumerate(plan["operations"], start=1):
        op, cid = record.get("op"), record.get("id")
        label = f"operation {position} ({op} {cid})"
        if op == "add":
            reasons.append(f"{label}: an add operation is added scope")
            continue
        before = before_by_id.get(cid) or {}
        if op == "remove":
            if before.get("type") == "primary_fix":
                reasons.append(f"{label}: removes the primary_fix criterion")
            continue
        after = after_by_id.get(cid) or {}
        if before.get("type") != after.get("type"):
            reasons.append(f"{label}: changes which criterion is the primary_fix "
                           f"({before.get('type')} -> {after.get('type')})")
        downgrade = _verification_downgrade(before, after)
        if downgrade:
            reasons.append(f"{label}: {downgrade}")
    changed = list(dict.fromkeys([*(cumulative_changed_ids or []), *_amendment_changed_ids(plan["operations"])]))
    items = []
    for cid in changed:
        criterion = after_by_id.get(cid) or before_by_id.get(cid)
        if criterion is None:
            continue
        item = criterion_work_item(criterion, registry)
        if item not in items:
            items.append(item)
    if len(items) > 1:
        reasons.append(f"changed criteria span more than one work item ({', '.join(items)})")
    if plan.get("work_item_scope_changed"):
        reasons.append("the work-item scope would change")
    if request_full_redesign:
        reasons.append("--request-full-redesign was passed")
    return ("full_redesign" if reasons else "scoped"), reasons[:MAX_AMENDMENT_LIST]


def amendment_affected_work_items(before_criteria: list[dict], after_criteria: list[dict],
                                  changed_ids: list[str], registry: list[dict]) -> list[str]:
    """Work items of the changed criteria, read from the amended registry
    (a removed criterion is read from the registry it was removed from)."""
    items = []
    after_by_id = {c.get("id"): c for c in after_criteria}
    before_by_id = {c.get("id"): c for c in before_criteria}
    for cid in changed_ids:
        criterion = after_by_id.get(cid) or before_by_id.get(cid)
        if criterion is None:
            continue
        item = criterion_work_item(criterion, registry)
        if item not in items:
            items.append(item)
    return items


def plan_amendment(status: dict, acceptance: dict, cfg: dict, operations: list[dict], *,
                   by: str, root: Path | None = None, request_full_redesign: bool = False,
                   now: str | None = None, id_factory=None) -> dict:
    """Plan a NEW amendment against the current registry and the design
    approval on record. Returns {"plan", "record"}; the record is complete
    (state `open`) but nothing is written. Raises HandsoffError when the
    run is not in a state that can open one, or CriteriaTransactionError
    from the #44 planner. A `full_redesign` classification is returned in
    the record, not raised: the caller refuses to open and prints the
    reasons."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("amendment: --by must be a non-empty string")
    phase = int(status.get("phase_number", 0) or 0)
    if phase < 3:
        raise HandsoffError(f"amendment: the lane opens at Phase 3 or later (currently Phase {phase}); "
                            "before design approval, revise criteria with criteria-apply")
    if open_amendment(status) is not None:
        raise HandsoffError(f"amendment: {open_amendment(status)['amendment_id']} is already open; "
                            "close or escalate it first")
    approval = status.get("design_approved")
    if not isinstance(approval, dict) or not approval.get("design_hash"):
        raise HandsoffError("amendment: no design approval is on record; use criteria-apply")
    problems = _design_errors(status, acceptance, cfg) + _design_review_errors(status, acceptance, cfg)
    if approval.get("design_hash") != design_hash(acceptance.get("criteria", [])):
        problems.append("design gate: the recorded design approval does not match the current criteria")
    if problems:
        raise HandsoffError("amendment: the design approval on record is not valid for the current "
                            "criteria (" + "; ".join(problems) + "); use criteria-apply")
    plan = plan_criteria_transaction(acceptance, cfg, operations, root=root)
    classification, reasons = classify_amendment(acceptance, cfg, plan,
                                                 request_full_redesign=request_full_redesign)
    registry, _ = effective_work_items(acceptance, cfg)
    changed_ids = _amendment_changed_ids(plan["operations"])
    existing = {item.get("amendment_id") for item in (status.get("amendments") or []) if isinstance(item, dict)}
    record = {
        "amendment_id": _new_bounded_id("am", AMENDMENT_ID_PATTERN, existing, id_factory),
        "opened_at": now or datetime.now(timezone.utc).isoformat(),
        "by": by.strip(),
        "base_design_hash": plan["design_hash_before"],
        "base_scope_hash": work_item_scope_hash(registry, acceptance.get("criteria", [])),
        "changed_ids": changed_ids,
        "dependent_ids": amendment_dependent_ids(plan["criteria_after"], changed_ids, registry),
        "affected_work_items": amendment_affected_work_items(
            acceptance.get("criteria", []), plan["criteria_after"], changed_ids, registry),
        "operations": deepcopy(plan["operations"]),
        "amendment_hash": amendment_hash(plan["design_hash_before"], plan["operations"]),
        "resulting_design_hash": plan["design_hash_after"],
        "classification": classification,
        "classification_reasons": reasons,
        "frozen_phase": phase,
        "frozen_progress": status.get("progress", 0),
        "review": None,
        "pilot_approval": None,
        "state": "open",
        "closed_at": None,
    }
    return {"plan": plan, "record": record}


def plan_amendment_revision(status: dict, acceptance: dict, cfg: dict, operations: list[dict], *,
                            root: Path | None = None) -> dict:
    """Plan a follow-up transaction on the OPEN amendment: the same
    classification rules over the cumulative delta (operations appended,
    changed ids unioned, cross-item judged over the union). Returns
    {"plan", "record"} where `record` is the updated open record with the
    hash recomputed and the stale review cleared; nothing is written."""
    current = open_amendment(status)
    if current is None:
        raise HandsoffError("amendment: no amendment is open")
    plan = plan_criteria_transaction(acceptance, cfg, operations, root=root)
    classification, reasons = classify_amendment(
        acceptance, cfg, plan, cumulative_changed_ids=list(current.get("changed_ids") or []))
    registry, _ = effective_work_items(acceptance, cfg)
    cumulative_operations = list(current.get("operations") or []) + deepcopy(plan["operations"])
    changed_ids = list(dict.fromkeys([*(current.get("changed_ids") or []),
                                      *_amendment_changed_ids(plan["operations"])]))
    record = deepcopy(current)
    record.update({
        "changed_ids": changed_ids,
        "dependent_ids": amendment_dependent_ids(plan["criteria_after"], changed_ids, registry),
        "affected_work_items": amendment_affected_work_items(
            acceptance.get("criteria", []), plan["criteria_after"], changed_ids, registry),
        "operations": cumulative_operations,
        "amendment_hash": amendment_hash(current.get("base_design_hash"), cumulative_operations),
        "resulting_design_hash": plan["design_hash_after"],
        "classification": classification,
        "classification_reasons": reasons,
        "review": None,
    })
    return {"plan": plan, "record": record}


def reset_amended_criteria(acceptance: dict, changed_ids: list[str]) -> None:
    """Registry mutation on open and revise: every changed criterion reads
    `not_tested` with `evidence` cleared (its old ledger records stay in
    the ledger but no longer bind, the spec hash changed); every other
    criterion is left byte for byte."""
    for criterion in acceptance.get("criteria", []):
        if criterion.get("id") in changed_ids:
            criterion["state"] = "not_tested"
            criterion["evidence"] = []


def recompute_amendment_hash(amendment: dict, criteria: list[dict]) -> tuple[str | None, list[str]]:
    """Recompute the open amendment's hash from the CURRENT registry: every
    operation's resulting spec hash must match the criterion on disk
    (absent for a remove), the registry's design hash must equal
    `resulting_design_hash`, and the base plus the recorded operations must
    reproduce `amendment_hash`. Returns (hash or None, problems)."""
    by_id = {c.get("id"): c for c in criteria}
    problems = []
    # The operations are the cumulative HISTORY of the delta: a revision
    # that touches a criterion the open transaction already changed appends
    # a second record for the same id. The registry can only match the LAST
    # record per id, so that is the one checked; the full history still
    # feeds amendment_hash below, so a drifted delta is still refused.
    # (Before this, any revised criterion could never pass review: the
    # first record's resulting hash was compared against a registry that
    # had legitimately moved on to the second's.)
    latest: dict = {}
    for record in amendment.get("operations") or []:
        latest[record.get("id")] = record
    for record in latest.values():
        cid = record.get("id")
        current = by_id.get(cid)
        if record.get("op") == "remove":
            if current is not None:
                problems.append(f"criterion {cid} was removed by the amendment but is present again")
            continue
        if current is None:
            problems.append(f"criterion {cid} named by the amendment is missing")
        elif criterion_spec_hash(current) != record.get("resulting_hash"):
            problems.append(f"criterion {cid} no longer matches the reviewed amendment (spec changed)")
    current_design = design_hash(criteria)
    if current_design != amendment.get("resulting_design_hash"):
        problems.append("the registry's design hash differs from the amendment's resulting design hash")
    recomputed = amendment_hash(amendment.get("base_design_hash"), amendment.get("operations") or [])
    if recomputed != amendment.get("amendment_hash"):
        problems.append("the recorded amendment hash does not reproduce from its base and operations")
    return (None if problems else recomputed), problems


def amendment_freeze_errors(status: dict) -> list[str]:
    """The compute_errors rule: while an amendment is open the run is
    pinned to the phase and progress it was opened at (after the review,
    deployment, and live decisions were invalidated). Any other proposed
    phase or progress is refused, forward or back."""
    amendment = open_amendment(status)
    if amendment is None:
        return []
    phase = int(status.get("phase_number", 0) or 0)
    progress = float(status.get("progress", 0) or 0)
    if phase != amendment.get("frozen_phase") or progress != float(amendment.get("frozen_progress", 0) or 0):
        return [f"amendment gate: amendment {amendment.get('amendment_id')} is open; "
                "record its review and Pilot approval first"]
    return []


def amendment_mutation_refusal(status: dict) -> str | None:
    amendment = open_amendment(status)
    if amendment is None:
        return None
    return f"amendment gate: close or escalate amendment {amendment.get('amendment_id')} before mutating criteria"


def amendment_decision_refusal(status: dict, action: str) -> str | None:
    amendment = open_amendment(status)
    if amendment is None:
        return None
    return (f"amendment gate: amendment {amendment.get('amendment_id')} is open; {action} is refused until "
            "its review and Pilot approval are recorded (or it is escalated)")


def amendment_pending_decision(amendment: dict | None) -> str | None:
    """Which decision the open amendment waits on: `review` (no review yet),
    `revision` (the reviewer requested changes; Architect revises or
    escalates), or `pilot_approval` (review approved). None when closed."""
    if not isinstance(amendment, dict) or amendment.get("state") != "open":
        return None
    review = amendment.get("review")
    if not isinstance(review, dict):
        return "review"
    if review.get("decision") == "approved":
        return "pilot_approval"
    return "revision"


def amendment_view(status: dict, acceptance: dict, cfg: dict, verifications: list[dict]) -> dict | None:
    """The dashboard/status view of the open amendment: ids, affected work
    items, the retained evidence count (criteria outside changed_ids that
    still hold valid evidence), the required decisions and which is
    pending, the classification and reasons. Hashes only, never criterion
    text. None when no amendment is open; `history` counts closed ones."""
    history = [item for item in (status.get("amendments") or []) if isinstance(item, dict)]
    amendment = open_amendment(status)
    if amendment is None:
        return None
    criteria = acceptance.get("criteria", [])
    changed = set(amendment.get("changed_ids") or [])
    retained = sum(1 for c in criteria if c.get("id") not in changed and valid_evidence_kinds(c, verifications))
    review = amendment.get("review") if isinstance(amendment.get("review"), dict) else None
    pilot = amendment.get("pilot_approval") if isinstance(amendment.get("pilot_approval"), dict) else None
    return {
        "amendment_id": amendment.get("amendment_id"),
        "state": amendment.get("state"),
        "by": amendment.get("by"),
        "opened_at": amendment.get("opened_at"),
        "classification": amendment.get("classification"),
        "classification_reasons": list(amendment.get("classification_reasons") or []),
        "changed_ids": list(amendment.get("changed_ids") or []),
        "dependent_ids": list(amendment.get("dependent_ids") or []),
        "affected_work_items": list(amendment.get("affected_work_items") or []),
        "operation_count": len(amendment.get("operations") or []),
        "retained_evidence_count": retained,
        "frozen_phase": amendment.get("frozen_phase"),
        "frozen_progress": amendment.get("frozen_progress"),
        "base_design_hash": amendment.get("base_design_hash"),
        "resulting_design_hash": amendment.get("resulting_design_hash"),
        "amendment_hash": amendment.get("amendment_hash"),
        "required_decisions": [
            {"decision": "review",
             "status": (review or {}).get("decision") or "pending",
             "by": (review or {}).get("by"), "at": (review or {}).get("at")},
            {"decision": "pilot_approval",
             "status": "recorded" if pilot else "pending",
             "by": (pilot or {}).get("by"), "at": (pilot or {}).get("at")},
        ],
        "pending_decision": amendment_pending_decision(amendment),
        "history_count": len(history),
    }


def _amendment_record_errors(record: object, label: str, *, must_be_open: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return [f"status: '{label}' must be an object"]
    if set(record) != AMENDMENT_FIELDS:
        return [f"status: '{label}' must have exactly the keys {sorted(AMENDMENT_FIELDS)}"]
    aid = record.get("amendment_id")
    if not isinstance(aid, str) or not AMENDMENT_ID_PATTERN.fullmatch(aid):
        errors.append(f"status: '{label}.amendment_id' must match am-<32 hex>")
    if not isinstance(record.get("by"), str) or not record["by"].strip():
        errors.append(f"status: '{label}.by' must be a non-empty string")
    for sub in ("opened_at", "closed_at"):
        value = record.get(sub)
        if value is None and sub == "closed_at":
            continue
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
            if parsed is None or parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            errors.append(f"status: '{label}.{sub}' must be a timezone-aware ISO-8601 timestamp"
                          + (" or null" if sub == "closed_at" else ""))
    for sub in ("base_design_hash", "base_scope_hash", "amendment_hash", "resulting_design_hash"):
        if not isinstance(record.get(sub), str) or not _HEX64.fullmatch(record[sub]):
            errors.append(f"status: '{label}.{sub}' must be a sha256 hex digest")
    for sub in ("changed_ids", "dependent_ids", "affected_work_items", "classification_reasons"):
        value = record.get(sub)
        if not isinstance(value, list) or len(value) > MAX_AMENDMENT_LIST \
                or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"status: '{label}.{sub}' must be a list of non-empty strings")
    operations = record.get("operations")
    if not isinstance(operations, list) or not operations \
            or len(operations) > MAX_AMENDMENT_LIST \
            or any(not isinstance(op, dict) or set(op) != {"op", "id", "previous_hash", "resulting_hash"}
                   or op.get("op") not in CRITERIA_TRANSACTION_OPS
                   or not isinstance(op.get("id"), str) or not op["id"].strip()
                   or any(op.get(h) is not None and (not isinstance(op.get(h), str) or not _HEX64.fullmatch(op[h]))
                          for h in ("previous_hash", "resulting_hash"))
                   for op in operations):
        errors.append(f"status: '{label}.operations' must be a non-empty list of "
                      "{op, id, previous_hash, resulting_hash} records")
    if record.get("classification") not in AMENDMENT_CLASSIFICATIONS:
        errors.append(f"status: '{label}.classification' must be one of {', '.join(AMENDMENT_CLASSIFICATIONS)}")
    if record.get("frozen_phase") not in PHASES or isinstance(record.get("frozen_phase"), bool):
        errors.append(f"status: '{label}.frozen_phase' must be a phase number")
    progress = record.get("frozen_progress")
    if not _is_number(progress) or not 0 <= progress <= 100:
        errors.append(f"status: '{label}.frozen_progress' must be a number from 0 to 100")
    state = record.get("state")
    if state not in AMENDMENT_STATES:
        errors.append(f"status: '{label}.state' must be one of {', '.join(AMENDMENT_STATES)}")
    elif must_be_open and state != "open":
        errors.append(f"status: '{label}' must be open (a closed amendment belongs in 'amendments')")
    elif not must_be_open and state == "open":
        errors.append(f"status: '{label}' must be closed (an open amendment belongs in 'amendment')")
    if state == "open" and record.get("closed_at") is not None:
        errors.append(f"status: '{label}.closed_at' must be null while open")
    if state != "open" and state in AMENDMENT_STATES and record.get("closed_at") is None:
        errors.append(f"status: '{label}.closed_at' is required once closed")
    review = record.get("review")
    if review is not None:
        if not isinstance(review, dict) or not AMENDMENT_REVIEW_FIELDS <= set(review) \
                or set(review) - AMENDMENT_REVIEW_FIELDS - AMENDMENT_REVIEW_OPTIONAL_FIELDS \
                or review.get("decision") not in AMENDMENT_REVIEW_DECISIONS \
                or not all(isinstance(review.get(k), str) and review[k].strip()
                           for k in ("by", "at", "summary", "amendment_hash")):
            errors.append(f"status: '{label}.review' must be null or {{by, at, decision, summary, amendment_hash}}")
        else:
            findings = review.get("findings")
            if findings is not None and (not isinstance(findings, list) or len(findings) > MAX_AMENDMENT_REVIEW_FINDINGS
                                         or not all(isinstance(f, str) and f.strip() and len(f) <= 512 for f in findings)):
                errors.append(f"status: '{label}.review.findings' must be a list of at most "
                              f"{MAX_AMENDMENT_REVIEW_FINDINGS} non-empty strings of at most 512 characters")
            for key in ("adopted_session", "adopted_by"):
                if key in review and (not isinstance(review[key], str) or not review[key].strip()):
                    errors.append(f"status: '{label}.review.{key}' must be a non-empty string when set")
    pilot = record.get("pilot_approval")
    if pilot is not None:
        if not isinstance(pilot, dict) or set(pilot) != AMENDMENT_PILOT_APPROVAL_FIELDS \
                or not all(isinstance(pilot.get(k), str) and pilot[k].strip()
                           for k in ("by", "at", "amendment_hash")):
            errors.append(f"status: '{label}.pilot_approval' must be null or {{by, at, amendment_hash}}")
    if state == "approved" and (pilot is None or not isinstance(review, dict)
                                or review.get("decision") != "approved"):
        errors.append(f"status: '{label}' approved requires an approved review and a pilot approval")
    return errors


def amendment_status_errors(status: dict) -> list[str]:
    """Schema rules for `amendment` (null or one OPEN record) and
    `amendments` (closed history, at most 16, unique ids). Both are absent
    on any status.json written before the lane existed and stay optional;
    a present-but-malformed value refuses the whole state."""
    errors: list[str] = []
    seen: set[str] = set()
    if "amendment" in status and status["amendment"] is not None:
        errors.extend(_amendment_record_errors(status["amendment"], "amendment", must_be_open=True))
        if isinstance(status["amendment"], dict) and isinstance(status["amendment"].get("amendment_id"), str):
            seen.add(status["amendment"]["amendment_id"])
    if "amendments" in status:
        history = status["amendments"]
        if not isinstance(history, list) or len(history) > MAX_AMENDMENT_HISTORY:
            errors.append(f"status: 'amendments' must be a list of at most {MAX_AMENDMENT_HISTORY} closed amendments")
        else:
            for index, item in enumerate(history):
                errors.extend(_amendment_record_errors(item, f"amendments[{index}]", must_be_open=False))
                aid = item.get("amendment_id") if isinstance(item, dict) else None
                if isinstance(aid, str):
                    if aid in seen:
                        errors.append(f"status: amendment id {aid} appears more than once")
                    seen.add(aid)
    for field in ("design_approved", "design_review"):
        record = status.get(field)
        if isinstance(record, dict) and "amended_by" in record:
            value = record["amended_by"]
            if not isinstance(value, list) or len(value) > MAX_AMENDMENT_HISTORY \
                    or any(not isinstance(aid, str) or not AMENDMENT_ID_PATTERN.fullmatch(aid) for aid in value):
                errors.append(f"status: '{field}.amended_by' must be a list of amendment ids")
    return errors


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


def liveness_view(status: dict, root: Path, cfg: dict,
                  now: datetime | None = None) -> dict:
    """Return the single activity truth used by status and Mission Control.

    A beacon bound to the current live session is authoritative because it is
    written by the managed process itself.  Session liveness is the next
    useful signal, and the workflow timestamp is the final legacy fallback.
    Pure: it never takes the project lock or writes, because status and the
    dashboard snapshot call it while already holding the lock (a nested
    flock deadlocks). Ledgering the stall transition is
    record_stall_transition, which those callers run inside their lock.
    """
    now = now or datetime.now(timezone.utc)
    beacon = read_live_beacon(root)
    session = _live_focus_session(status)
    session_id = session.get("session_id") if session else None
    matching = beacon if beacon and session_id and beacon.get("session_id") == session_id \
        and session.get("state") in AGENT_SESSION_LIVE_STATES else None
    beacon_seconds = _seconds_since(matching.get("beacon_at"), now) if matching else None
    signal = "none"
    if matching:
        signal = "fresh" if beacon_seconds is not None and 0 <= beacon_seconds <= LIVE_BEACON_FRESH_SECONDS else "stale"
    session_seconds = None
    if session:
        session_live = read_session_liveness(root)
        stamp = session_live.get(session_id) if session_id else None
        session_seconds = _seconds_since(stamp, now)
    chosen = beacon_seconds if matching and beacon_seconds is not None else session_seconds
    # #34 and #41 (restored after the #94 rework): a bound heartbeat (a
    # background wait) and bound managed output are liveness too; the
    # freshest signal wins, and updated_at is the final fallback.
    output_record = output_liveness_for(status, read_output_liveness(root))
    output_seconds = _seconds_since(output_record.get("output_at"), now) if output_record else None
    heartbeat_seconds = _seconds_since(bound_heartbeat_at(status), now)
    updated_seconds = _seconds_since(status.get("updated_at"), now)
    candidates = [value for value in (chosen, output_seconds, heartbeat_seconds) if value is not None]
    if not candidates and updated_seconds is not None:
        candidates = [updated_seconds]
    seconds = max(int(min(candidates)), 0) if candidates else None
    # #193: a closed lid is not silence. The freshest signal's age is
    # measured in awake time; the sleep inside it is reported beside it.
    asleep = 0.0
    if seconds is not None:
        sleep = machine_sleep_intervals(now=now)
        since = now - timedelta(seconds=seconds)
        asleep = asleep_seconds(since, now, sleep)
        seconds = max(int(seconds - asleep), 0)
    threshold = int(float(cfg.get("stall_minutes", 10) or 10) * 60)
    warning = None
    if status.get("status") == "in_progress" and seconds is not None and seconds >= threshold \
            and not isinstance(status.get("human_pause"), dict):
        warning = f"no update in {int(seconds / 60)} minutes (limit {int(threshold / 60)})"
    note = activity_note(status, cfg, now=now, output_liveness=read_output_liveness(root))
    assessment = recovery_assessment(status, cfg, read_session_liveness(root),
                                     read_events(root, cfg), now, root=root)
    return {"seconds_since_activity": seconds, "asleep_seconds": round(asleep, 3), "process_signal": signal,
            "stall_warning": warning, "stall_threshold_minutes": int(threshold / 60),
            "activity_note": note, "assessment": assessment}


def record_stall_transition(root: Path, cfg: dict, warning: str | None) -> str | None:
    """Ledger a stall warning's appearance or disappearance exactly once.
    Caller must hold project_lock. Status is re-read from disk here rather
    than trusted from the caller, so two callers that loaded the same
    snapshot before either committed still produce one event: the second
    one sees the bit already flipped. Returns the event kind written."""
    status = load_unique_json(status_path(root, cfg))
    if warning and not status.get("stall_reported"):
        status["stall_reported"] = True
        commit(root, cfg, status=status, event_kind="stall_reported",
               event_message="Stall warning served")
        return "stall_reported"
    if warning is None and status.get("stall_reported"):
        status["stall_reported"] = False
        commit(root, cfg, status=status, event_kind="stall_cleared",
               event_message="Stall warning cleared")
        return "stall_cleared"
    return None


# --------------------------------------------------------------------------
# #41: output liveness. The managed child's stdout/stderr readers note every
# chunk here; nothing below ever records content, appends an event, or
# touches the session record.
# --------------------------------------------------------------------------

def output_liveness_path(root: Path) -> Path:
    return Path(root) / OUTPUT_LIVENESS_FILE


def agent_output_path(root: Path) -> Path:
    return Path(root) / AGENT_OUTPUT_FILE


_AGENT_OUTPUT_LOCK = threading.Lock()


@contextmanager
def agent_output_lock(root: Path):
    """Serialize telemetry writers without contending on workflow state."""
    with _AGENT_OUTPUT_LOCK:
        if fcntl is None:
            yield
            return
        path = Path(root) / AGENT_OUTPUT_LOCK_FILE
        path.touch(exist_ok=True)
        with path.open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_agent_output_store(root: Path) -> dict:
    try:
        value = json.loads(agent_output_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": 1, "order": [], "sessions": {}}
    if not isinstance(value, dict) or value.get("schema") != 1 \
            or not isinstance(value.get("order"), list) \
            or not isinstance(value.get("sessions"), dict):
        return {"schema": 1, "order": [], "sessions": {}}
    return value


def operations_path(root: Path) -> Path:
    """Return the bounded, telemetry-only operation journal for ROOT."""
    return Path(root) / OPERATIONS_FILE


def validate_operation_line(payload: object) -> dict | None:
    """Accept only the small fixed wire schema so arbitrary child text cannot persist."""
    if not isinstance(payload, dict):
        return None
    allowed = {"operation_id", "dependency", "operation", "state", "attempt",
               "timeout_seconds", "category"}
    if set(payload) - allowed:
        return None
    required = ("operation_id", "dependency", "operation", "state", "attempt", "timeout_seconds")
    if any(key not in payload or not isinstance(payload[key], str)
           for key in ("operation_id", "dependency", "operation", "state")):
        return None
    if not OPERATION_ID_PATTERN.fullmatch(payload["operation_id"]):
        return None
    if any(not OPERATION_IDENTIFIER_PATTERN.fullmatch(payload[key]) for key in ("dependency", "operation")):
        return None
    if payload["state"] not in OPERATION_STATES:
        return None
    for key, low, high in (("attempt", 1, 64), ("timeout_seconds", 1, 86400)):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return None
    if "category" in payload and (not isinstance(payload["category"], str)
                                  or payload["category"] not in FAILURE_CATEGORIES):
        return None
    return {key: payload[key] for key in allowed if key in payload}


def _empty_operations() -> dict:
    return {"schema": 1, "sessions": {}}


def read_operations(root: Path) -> dict:
    """Read malformed or absent journals as empty, keeping telemetry non-fatal."""
    try:
        value = json.loads(operations_path(root).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema") == 1 and isinstance(value.get("sessions"), dict):
            return value
    except (OSError, ValueError):
        pass
    return _empty_operations()


def current_operation(root: Path, session_id: str) -> dict | None:
    """Return the newest unfinished operation, falling back to newest history.

    The fallback keeps diagnostics useful when a session has just become
    terminal, while the unfinished preference makes timeout enforcement local
    to the operation that still owns the live child.
    """
    session = read_operations(root).get("sessions", {}).get(session_id)
    operations = session.get("operations", []) if isinstance(session, dict) else []
    if not isinstance(operations, list) or not operations:
        return None
    non_terminal = [item for item in operations if isinstance(item, dict)
                    and item.get("state") not in OPERATION_STATES[1:]]
    return deepcopy((non_terminal or operations)[-1])


def succeeded_operation_ids(root: Path, session_id: str) -> list[str]:
    """Return bounded, journal-order success identifiers for a source session."""
    session = read_operations(root).get("sessions", {}).get(session_id)
    operations = session.get("operations", []) if isinstance(session, dict) else []
    return [item["operation_id"] for item in operations[:64]
            if isinstance(item, dict) and item.get("state") == "succeeded"
            and isinstance(item.get("operation_id"), str)]


def record_operation(root: Path, session_id: str, role: str, record: dict, now: datetime) -> dict:
    """Atomically retain recent operation telemetry, bounded by session and history."""
    stamp = now.isoformat()
    terminal = record["state"] in OPERATION_STATES[1:]
    with agent_output_lock(Path(root)):
        store = read_operations(root)
        sessions = store["sessions"]
        session = sessions.setdefault(session_id, {"role": role, "protocol_warnings": 0,
            "late_telemetry": 0, "operations": []})
        operations = session.setdefault("operations", [])
        existing = next((item for item in operations if item.get("operation_id") == record["operation_id"]), None)
        if existing is None:
            saved = dict(record)
            saved.update({"started_at": stamp, "updated_at": stamp})
            if terminal:
                saved["ended_at"] = stamp
            operations.append(saved)
        else:
            existing.update({key: record[key] for key in ("attempt", "state") if key in record})
            if "category" in record:
                existing["category"] = record["category"]
            else:
                existing.pop("category", None)
            existing["updated_at"] = stamp
            if terminal:
                existing["ended_at"] = stamp
        session["operations"] = operations[-64:]
        while len(sessions) > 8:
            oldest = next(iter(sessions))
            sessions.pop(oldest, None)
        _atomic_write_text(operations_path(root), json.dumps(store, sort_keys=True) + "\n")
    return next(item for item in session["operations"] if item["operation_id"] == record["operation_id"])


def count_operation_warning(root: Path, session_id: str, role: str, kind: str) -> None:
    """Count malformed or late telemetry without changing managed-session outcome."""
    if kind not in {"protocol_warnings", "late_telemetry"}:
        return
    with agent_output_lock(Path(root)):
        store = read_operations(root)
        session = store["sessions"].setdefault(session_id, {"role": role, "protocol_warnings": 0,
            "late_telemetry": 0, "operations": []})
        session[kind] = int(session.get(kind, 0)) + 1
        _atomic_write_text(operations_path(root), json.dumps(store, sort_keys=True) + "\n")


def operation_assessment(record: dict, now: datetime, quiet_seconds: int) -> str:
    """Classify using terminal, timeout, then quiet-heartbeat precedence."""
    if record.get("state") in OPERATION_STATES[1:]:
        return record["state"]
    started = datetime.fromisoformat(record["started_at"])
    updated = datetime.fromisoformat(record["updated_at"])
    if (now - started).total_seconds() >= record["timeout_seconds"]:
        return "timed_out"
    if (now - updated).total_seconds() >= quiet_seconds:
        return "stale"
    return "waiting"


def dependency_class(record: dict) -> str:
    """Map failure categories to stable dependency families for later assessment."""
    table = {"auth_failure": "authentication", "rate_limit": "agent_provider",
             "context_exhaustion": "agent_provider", "token_budget_exhaustion": "agent_provider",
             "timeout": "network", "network": "network", "target_service": "target_service"}
    return table.get(record.get("category"), "engine")


def operation_view(status: dict, root: Path, now: datetime | None = None,
                   quiet_seconds: int | None = None) -> dict:
    """Build the bounded operation panel from the journal and current status.

    This is deliberately read-only: operation telemetry is advisory UI data,
    so it must not participate in gates, hashes, events, or status mutation.
    The same focused-session rule as ``agent_output_view`` keeps both panels
    describing the newest live session, with the newest session as fallback.
    """
    focused = _live_focus_session(status)
    if not isinstance(focused, dict):
        return {"availability": "unavailable", "session_id": None, "role": None,
                "current": None, "assessment": "no operation telemetry reported by this session",
                "dependency_class": None, "elapsed_seconds": None, "timeout_seconds": None,
                "attempt": None, "retry_count": 0, "last_success_at": None, "history": [],
                "protocol_warnings": 0, "late_telemetry": 0}
    session_id = focused.get("session_id")
    store = read_operations(root)
    session = store.get("sessions", {}).get(session_id)
    if not isinstance(session, dict):
        session = {}
    history = [deepcopy(item) for item in session.get("operations", [])
               if isinstance(item, dict)]
    current = deepcopy(current_operation(root, session_id)) if session_id else None
    now = now or datetime.now(timezone.utc)
    if quiet_seconds is None:
        quiet_seconds = int(load_config(root).get("recovery", {}).get(
            "live_session_silence_minutes", 10)) * 60
    assessment = operation_assessment(current, now, quiet_seconds) if current else None
    started_at = current.get("started_at") if current else None
    elapsed = None
    if started_at:
        try:
            elapsed = max(0, int((now - datetime.fromisoformat(started_at)).total_seconds()))
        except (TypeError, ValueError):
            elapsed = None
    successes = [item for item in history if item.get("state") == "succeeded" and item.get("ended_at")]
    last_success_at = successes[-1].get("ended_at") if successes else None
    return {"availability": "available" if current else "unavailable", "session_id": session_id,
            "role": session.get("role") or focused.get("role"), "current": current,
            "assessment": assessment or "no operation telemetry reported by this session",
            "dependency_class": dependency_class(current) if current else None,
            "elapsed_seconds": elapsed, "timeout_seconds": current.get("timeout_seconds") if current else None,
            "attempt": current.get("attempt") if current else None,
            "retry_count": max(0, int(current.get("attempt", 1)) - 1) if current else 0,
            "last_success_at": last_success_at, "history": history,
            "protocol_warnings": int(session.get("protocol_warnings", 0)),
            "late_telemetry": int(session.get("late_telemetry", 0))}


def start_agent_output(root: Path, session_id: str, role: str, adapter: str, *,
                       now: datetime | None = None) -> None:
    """Create a best-effort portable output envelope for one session."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with agent_output_lock(Path(root)):
            store = _read_agent_output_store(root)
            order = [item for item in store["order"] if item != session_id]
            order.append(session_id)
            while len(order) > MAX_AGENT_OUTPUT_SESSIONS:
                store["sessions"].pop(order.pop(0), None)
            store["order"] = order
            store["sessions"][session_id] = {
                "session_id": session_id, "role": role, "adapter": adapter,
                "state": "connected", "started_at": stamp, "updated_at": stamp,
                "ended_at": None, "cursor": 0, "source_bytes": 0,
                "dropped_entries": 0, "entries": [],
            }
            _atomic_write_text(agent_output_path(root), json.dumps(store, sort_keys=True) + "\n")
    except OSError:
        pass


def append_agent_output(root: Path, session_id: str, stream: str, text: str,
                        source_bytes: int, *, now: datetime | None = None) -> bool:
    """Compatibility wrapper for one already-redacted line."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return append_agent_output_batch(
        root, session_id,
        [{"at": stamp, "stream": stream, "text": text}], source_bytes,
    )


def append_agent_output_batch(root: Path, session_id: str, batch: list[dict],
                              source_bytes: int) -> bool:
    """Persist a bounded batch under the telemetry-only lock."""
    if not isinstance(batch, list) or not batch:
        return False
    normalized = []
    for item in batch[:AGENT_OUTPUT_FLUSH_MAX_ENTRIES]:
        if not isinstance(item, dict) or item.get("stream") not in {"stdout", "stderr"} \
                or not isinstance(item.get("text"), str) or not isinstance(item.get("at"), str):
            return False
        normalized.append({
            "at": item["at"], "stream": item["stream"],
            "text": item["text"][:MAX_AGENT_OUTPUT_LINE_CHARS],
        })
    try:
        with agent_output_lock(Path(root)):
            store = _read_agent_output_store(root)
            record = store["sessions"].get(session_id)
            if not isinstance(record, dict):
                return False
            cursor = int(record.get("cursor", 0)) + 1
            entries = list(record.get("entries") or [])
            for offset, item in enumerate(normalized):
                entries.append({"cursor": cursor + offset, **item})
            if len(entries) > MAX_AGENT_OUTPUT_ENTRIES:
                excess = len(entries) - MAX_AGENT_OUTPUT_ENTRIES
                entries = entries[excess:]
                record["dropped_entries"] = int(record.get("dropped_entries", 0)) + excess
            record.update({
                "cursor": cursor + len(normalized) - 1, "updated_at": normalized[-1]["at"],
                "source_bytes": max(int(source_bytes), int(record.get("source_bytes", 0))),
                "entries": entries,
            })
            _atomic_write_text(agent_output_path(root), json.dumps(store, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def finish_agent_output(root: Path, session_id: str, terminal_state: str, *,
                        now: datetime | None = None) -> None:
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with agent_output_lock(Path(root)):
            store = _read_agent_output_store(root)
            record = store["sessions"].get(session_id)
            if not isinstance(record, dict):
                return
            record.update({"state": "closed", "terminal_state": terminal_state,
                           "updated_at": stamp, "ended_at": stamp})
            _atomic_write_text(agent_output_path(root), json.dumps(store, sort_keys=True) + "\n")
    except Exception:
        pass


def agent_output_view(status: dict, root: Path, *, now: datetime | None = None) -> dict | None:
    """Return the focused session's bounded log and explicit transport state."""
    session = _live_focus_session(status)
    if not isinstance(session, dict):
        return None
    session_id = session.get("session_id")
    store = _read_agent_output_store(root)
    record = store.get("sessions", {}).get(session_id)
    now = now or datetime.now(timezone.utc)
    started_at = session.get("started_at")
    elapsed_seconds = _seconds_since(started_at, now)
    beacon = read_live_beacon(root)
    beacon_age = _seconds_since(beacon.get("beacon_at"), now) if beacon and beacon.get("session_id") == session_id else None
    last_output_at = None
    # Normalize the output record before judging state: the precedence
    # below consults `entries`, which used to be assigned only after it.
    entries = []
    if isinstance(record, dict):
        entries = [entry for entry in (record.get("entries") or [])
                   if isinstance(entry, dict) and set(entry) == {"cursor", "at", "stream", "text"}]
    state = "transport_disconnected"
    if session.get("state") in AGENT_SESSION_TERMINAL_STATES:
        # A session that ended badly is "failed", never "transport
        # disconnected": the transport did its job, the process did not.
        state = "completed" if session.get("state") == "completed" else "failed"
    elif _output_bytes_missed(root, session_id, record):
        # The child produced more bytes than the recorder persisted: the
        # output transport dropped, not the process.
        state = "transport_disconnected"
    elif beacon_age is not None and beacon_age > LIVE_BEACON_FRESH_SECONDS:
        state = "stale_heartbeat"
    elif isinstance(record, dict) and entries:
        state = "active_output"
    elif isinstance(record, dict):
        state = "connected_no_output"
    if not isinstance(record, dict):
        return {
            "session_id": session_id, "role": session.get("role"),
            "adapter": session.get("adapter"), "state": "transport_disconnected",
            "started_at": started_at, "elapsed_seconds": elapsed_seconds,
            "last_heartbeat_age_seconds": beacon_age, "last_output_at": None,
            "cursor": 0, "dropped_entries": 0, "entries": [], "updated_at": None,
        }
    last_output_at = entries[-1].get("at") if entries else record.get("updated_at")
    return {
        "session_id": session_id, "role": record.get("role"), "adapter": record.get("adapter"),
        "state": state, "started_at": started_at, "elapsed_seconds": elapsed_seconds,
        "last_heartbeat_age_seconds": beacon_age, "last_output_at": last_output_at,
        "cursor": int(record.get("cursor", 0)),
        "dropped_entries": int(record.get("dropped_entries", 0)),
        "entries": entries, "updated_at": record.get("updated_at"),
    }


def _output_bytes_missed(root: Path, session_id: str | None, record) -> bool:
    """True when the liveness record counted more output bytes for the
    session than the portable output store persisted."""
    if not isinstance(record, dict):
        return False
    liveness = read_output_liveness(root)
    if not isinstance(liveness, dict) or liveness.get("session_id") != session_id:
        return False
    try:
        return int(liveness.get("bytes", 0) or 0) > int(record.get("source_bytes", 0) or 0)
    except (TypeError, ValueError):
        return False


def read_output_liveness(root: Path) -> dict | None:
    """The output record if it exists and is well-formed (exactly the five
    keys: two identifiers, one timestamp, two non-negative counters);
    anything else, including no file, reads as no signal, never as an
    error."""
    path = output_liveness_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != set(OUTPUT_LIVENESS_KEYS):
        return None
    for key in ("session_id", "role", "output_at"):
        if not isinstance(record[key], str) or not record[key]:
            return None
    for key in ("chunks", "bytes"):
        if not isinstance(record[key], int) or isinstance(record[key], bool) or record[key] < 0:
            return None
    if _minutes_since(record["output_at"], datetime.now(timezone.utc)) is None:
        return None
    return record


_OUTPUT_LIVENESS_LOCK = threading.Lock()
_OUTPUT_LIVENESS_COUNTERS: dict[str, dict] = {}
_MAX_OUTPUT_LIVENESS_SESSIONS = 64


def note_output_liveness(root: Path, session_id: str, role: str, nbytes: int, *,
                         now: datetime | None = None, monotonic=time.monotonic) -> bool:
    """Count one output chunk for `session_id` and, at most once per
    OUTPUT_LIVENESS_WRITE_INTERVAL_SECONDS per session, write the five-key
    record. Chunks between writes only bump the in-memory counters, which
    the next write carries. Returns True only when the file was written.
    Best effort throughout: an OSError is swallowed and nothing here can
    change the child's lifecycle, the session record, or any ledger.
    `monotonic` (the rate-limit clock) and `now` (the stamped time) are
    injectable for tests."""
    if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 0:
        nbytes = 0
    with _OUTPUT_LIVENESS_LOCK:
        counters = _OUTPUT_LIVENESS_COUNTERS.get(session_id)
        if counters is None:
            while len(_OUTPUT_LIVENESS_COUNTERS) >= _MAX_OUTPUT_LIVENESS_SESSIONS:
                _OUTPUT_LIVENESS_COUNTERS.pop(next(iter(_OUTPUT_LIVENESS_COUNTERS)))
            counters = _OUTPUT_LIVENESS_COUNTERS[session_id] = {"chunks": 0, "bytes": 0, "written_at": None}
        counters["chunks"] += 1
        counters["bytes"] += nbytes
        tick = monotonic()
        if (counters["written_at"] is not None
                and tick - counters["written_at"] < OUTPUT_LIVENESS_WRITE_INTERVAL_SECONDS):
            return False
        counters["written_at"] = tick
        record = {
            "session_id": session_id, "role": role,
            "output_at": (now or datetime.now(timezone.utc)).isoformat(),
            "chunks": counters["chunks"], "bytes": counters["bytes"],
        }
    try:
        _atomic_write_text(output_liveness_path(root), json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def output_liveness_for(status: dict, liveness: dict | None) -> dict | None:
    """The output record only when it is bound to the run: its `session_id`
    is the recorded current session for its `role` AND that session is in
    a live state (launching, running). Output from a completed, failed,
    replaced, or unknown session, or from an unknown role, reads as None,
    which is how a process exit expires the signal at once."""
    if not isinstance(liveness, dict):
        return None
    current = current_agent_sessions(status).get(liveness.get("role"))
    if not isinstance(current, dict):
        return None
    if current.get("session_id") != liveness.get("session_id"):
        return None
    if current.get("state") not in AGENT_SESSION_LIVE_STATES:
        return None
    return liveness


_READ_OUTPUT_LIVENESS = object()


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


def live_status(status: dict, cfg: dict, root: Path, *, now: datetime | None = None,
                output_liveness=_READ_OUTPUT_LIVENESS) -> dict:
    """Derive one of LIVE_STATES from structured state plus the beacon.

    Precedence: a complete run; a run waiting on a person (blocked, open
    human pause, Phase 7 awaiting deployment approval); the current live
    session (a beacon counts only when its session_id is the current
    session's; fresh means at most LIVE_BEACON_FRESH_SECONDS old); the
    current terminal session, reported from the ledger-bound record
    (`ended_at`, `exit_code`) with the beacon informing `process_signal`
    only; else idle. `last_activity_at` is the freshest of updated_at,
    last_heartbeat_at, the bound output record (#41), the matching beacon,
    the session timestamps, and an open pause's `since`; `activity_source`
    names which of ACTIVITY_SOURCES that was. `output_liveness` defaults to
    reading `.handsoff-output-liveness.json` from `root`; `activity_view`
    passes the record it already read so one reading feeds every field."""
    now = now or datetime.now(timezone.utc)
    beacon = read_live_beacon(root)
    if output_liveness is _READ_OUTPUT_LIVENESS:
        output_liveness = read_output_liveness(root)
    output = output_liveness_for(status, output_liveness)
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

    # Ordered so an exact tie goes to the more specific signal.
    pause = status.get("human_pause")
    candidates = [("pause", pause.get("since") if isinstance(pause, dict) else None)]
    if output:
        candidates.append(("output", output["output_at"]))
    candidates.append(("heartbeat", bound_heartbeat_at(status)))
    if matching:
        candidates.append(("beacon", matching["beacon_at"]))
    if session:
        candidates.extend(("session", session.get(key)) for key in ("started_at", "running_at", "ended_at"))
    candidates.append(("workflow", status.get("updated_at")))
    stamped = [(source, c, _seconds_since(c, now)) for source, c in candidates if isinstance(c, str)]
    stamped = [(source, c, age) for source, c, age in stamped if age is not None]
    freshest = min(stamped, key=lambda item: item[2]) if stamped else None
    activity_source = freshest[0] if freshest else None
    last_activity_at = freshest[1] if freshest else None
    seconds_since_activity = int(round(freshest[2])) if freshest else None

    view = {
        "state": "idle", "role": role, "session_id": session_id,
        "last_activity_at": last_activity_at, "seconds_since_activity": seconds_since_activity,
        "activity_source": activity_source,
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
        superseded = failed_session_superseded(status, session)
        if view["state"] == "failed" and superseded:
            # #172: the failure stays in the ledger; the live reading says
            # what the host did with it, so a stale failure never outranks
            # a run that has moved on.
            view["state"] = "stopped"
            view["detail"] = f"{role} session failed ({exit_text}) at {view['ended_at'] or 'an unknown time'}; {superseded}"
    return view


def mark_beacon_adopted(root: Path, session_id: str) -> bool:
    """#172: after session-result-adopt the last beacon says what the host
    did with the session. Only a beacon naming that session is rewritten;
    another session's beacon is never touched."""
    beacon = read_live_beacon(root)
    if not isinstance(beacon, dict) or beacon.get("session_id") != session_id:
        return False
    return write_live_beacon(root, session_id=session_id, role=beacon.get("role") or "reviewer",
                             state="adopted", pid=None, ended_at=beacon.get("ended_at"),
                             exit_code=beacon.get("exit_code"))


def failed_session_superseded(status: dict, session: dict) -> str | None:
    """#172: why a failed session no longer speaks for the run: its persisted
    result was adopted, its adopted failure record was followed by a newer
    status update, or the run advanced past the phase the session ran in.
    None when neither holds (a genuinely failed run stays failed)."""
    result = session.get("result") if isinstance(session, dict) else None
    if isinstance(result, dict) and result.get("adopted_at"):
        return f"verdict adopted at {result['adopted_at']} by {result.get('adopted_by') or 'the host'}"
    failures = status.get("agent_failures") if isinstance(status.get("agent_failures"), dict) else {}
    record = failures.get(session.get("session_id")) if isinstance(session, dict) else None
    if isinstance(record, dict) and record.get("adopted") is True:
        try:
            ended = datetime.fromisoformat(str(session.get("ended_at")))
            updated = datetime.fromisoformat(str(status.get("updated_at")))
            if updated > ended:
                return f"verdict adopted by the host; status updated at {status['updated_at']}"
        except (TypeError, ValueError):
            pass
        return "verdict adopted by the host"
    try:
        session_phase = int(session.get("phase_number") or 0)
        run_phase = int(status.get("phase_number") or 0)
    except (TypeError, ValueError):
        return None
    if session_phase and run_phase > session_phase:
        return f"session ran at phase {session_phase}; the run advanced to phase {run_phase}"
    return None


def activity_view(status: dict, cfg: dict, root: Path, *, now: datetime | None = None) -> dict:
    """#41: the one activity reading both `status` and the dashboard show,
    so the CLI and Mission Control cannot disagree. Reads the output
    record once and feeds that same reading to `live_status`,
    `stall_warning`, and `activity_note`. Exactly five keys: `source` (one
    of ACTIVITY_SOURCES or None), `at`, `seconds_ago` (whole seconds,
    truncated), `stall_warning`, `activity_note`."""
    now = now or datetime.now(timezone.utc)
    liveness = read_output_liveness(root)
    live = live_status(status, cfg, root, now=now, output_liveness=liveness)
    seconds = _seconds_since(live["last_activity_at"], now)
    return {
        "source": live["activity_source"],
        "at": live["last_activity_at"],
        "seconds_ago": max(int(seconds), 0) if seconds is not None else None,
        "stall_warning": stall_warning(status, cfg, now=now, output_liveness=liveness),
        "activity_note": activity_note(status, cfg, now=now, output_liveness=liveness),
    }


# --------------------------------------------------------------------------
# check execution: close the loop between a claimed state and reality
# --------------------------------------------------------------------------

def run_checks(cfg: dict, root: Path, commands: list[str] | None = None,
               timeout: int | None = None, *, allow_regression: bool = False,
               on_progress=None, env: dict | None = None) -> list[dict]:
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
        index = len(results) + 1
        if on_progress:
            on_progress(index, len(selected), cmd, None)
        started = time.time()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout,
                                  env={**os.environ, **env} if env else None)
            returncode = proc.returncode
            output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output = stdout + stderr + f"\nHANDSOFF: command timed out after {timeout} seconds"
        raw = output.encode("utf-8", "replace")
        output_hash = hashlib.sha256(raw).hexdigest()
        # #43: the three flags the verification cache reads. A timed-out
        # (124) or truncated result is real evidence of what happened but
        # never a reusable proof that the command passes.
        results.append({
            "command": cmd, "exit_code": returncode, "output_sha256": output_hash,
            "duration_s": round(time.time() - started, 2),
            "timed_out": returncode == 124,
            "truncated": len(output) > CHECK_OUTPUT_TAIL_CHARS,
            "output_bytes": len(raw),
            "output_tail": output[-CHECK_OUTPUT_TAIL_CHARS:],
        })
        if on_progress:
            on_progress(index, len(selected), cmd, results[-1])
    return results


# --------------------------------------------------------------------------
# #43: batch and cache hash-bound targeted verification. `verify` already
# runs the union of needed commands once per invocation; this binds every
# launch to the exact state it proved something about (command, repository
# content including dirty state, the verification-relevant configuration,
# and the specs of the criteria verified with it) so an identical later
# request within the same run reuses the ledger record instead of
# launching again. The ledger itself is the cache: no side file, and the
# reused record chains and hashes like any other.
# --------------------------------------------------------------------------

CHECK_OUTPUT_TAIL_CHARS = 2000
VERIFY_INFLIGHT_DIR = ".handsoff-verify-inflight"
# Handsoff's own generated state, never part of the repository digest in
# either mode: the ledgers, anchors, locks, beacons, and the in-flight
# directory would otherwise churn the digest on every evidence write.
HANDSOFF_GENERATED_NAMES = frozenset({
    ".handsoff.lock", ".handsoff-event-head.json", ".handsoff-writeahead.json",
    ".handsoff-session-liveness.json", ".handsoff-dashboard-owner.json",
    # Agent output is gitignored Handsoff runtime state, not repository evidence.
    ".handsoff-agent-output.json",
    LIVE_BEACON_FILE, OUTPUT_LIVENESS_FILE, DESIGN_EVIDENCE_FILE, PREFLIGHT_FILE,
    LIVE_INFLIGHT_FILE,
    ".handsoff-selfcheck", ".handsoff-archive", VERIFY_INFLIGHT_DIR, ANALYSIS_DIR,
    "__pycache__", ".git",
})


def _digest_listing(root: Path, cfg: dict) -> list[str]:
    """List candidate paths once, applying git and configured ignore rules."""
    listed = None
    try:
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(root),
                               capture_output=True, text=True, timeout=10, check=False)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            tracked = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True,
                                     timeout=30, check=True).stdout
            untracked = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                                       cwd=str(root), capture_output=True, timeout=30, check=True).stdout
            listed = [x.decode("utf-8", "replace") for x in (tracked + untracked).split(b"\0") if x]
    except (OSError, subprocess.SubprocessError):
        listed = None
    if listed is None:
        listed = []
        gitignore_rules = []
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in HANDSOFF_GENERATED_NAMES)
            base = Path(directory).relative_to(root).as_posix()
            if base == ".": base = ""
            if ".gitignore" in files:
                for line in (Path(directory) / ".gitignore").read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("!"):
                        gitignore_rules.append((base, line))
            for filename in files:
                if filename == ".gitignore":
                    continue
                relative = filename if not base else f"{base}/{filename}"
                ignored = False
                for rule_base, rule in gitignore_rules:
                    target = relative[len(rule_base) + 1:] if rule_base and relative.startswith(rule_base + "/") else relative
                    pattern = rule.rstrip("/")
                    if rule.endswith("/") and (target == pattern or target.startswith(pattern + "/")):
                        ignored = True
                    elif rule.startswith("/") and fnmatch.fnmatch(target, pattern.lstrip("/")):
                        ignored = True
                    elif fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(Path(target).name, pattern):
                        ignored = True
                if not ignored:
                    listed.append(relative)
    ignores = [item for item in cfg.get("digest_ignore", []) if isinstance(item, str)]
    return sorted({path for path in listed
                   if not any(fnmatch.fnmatch(path, glob) or any(fnmatch.fnmatch(part, glob) for part in path.split("/"))
                              for glob in ignores)})


def _digest_excluded(relative: str, state_files: set[str]) -> bool:
    """Runtime bookkeeping never counts as repository content. Beyond the
    enumerated names, every `.handsoff*` path component is Handsoff side
    state (locks, beacons, output tails, the version pin, future files).
    Without the structural rule a managed session running after `verify`
    would write a side file and flag its own evidence stale on a root that
    is not a git checkout (#77)."""
    parts = relative.split("/")
    if relative in state_files:
        return True
    if any(part in HANDSOFF_GENERATED_NAMES for part in parts):
        return True
    # Every `.handsoff*` path component, the version pin included: the pin
    # is Handsoff configuration, not product source. `upgrade --to` rewrites
    # it on every engine upgrade, which used to stale the evidence of every
    # completed run in the project (v0.3.25 field-note defect 2). The engine
    # identity stays auditable through `engine_history` and the engine
    # recorded on every initialized and agent_session_launching event.
    if any(part.startswith(".handsoff") for part in parts):
        return True
    # handsoff.toml is Handsoff's own configuration, not the product under
    # test: a live_commands line or a budget tweak must not read as source
    # drift (#93). What configuration CAN change the meaning of evidence,
    # the check commands, is bound separately through the verification
    # config hash stored on every executed record (see evidence_drift).
    if relative == "handsoff.toml":
        return True
    return parts[-1].endswith(".pyc")


def _digest_entry(root: Path, relative: str) -> str | None:
    path = root / relative
    if path.is_symlink():
        return hashlib.sha256(("symlink:" + os.readlink(path)).encode("utf-8", "replace")).hexdigest()
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_digest(root: Path, cfg: dict | None = None) -> str:
    """sha256 over the sorted (relative path, file sha256) pairs of every
    tracked file plus every untracked-not-ignored file when `root` is a git
    checkout (dirty state included by construction: the working tree
    content is hashed, not HEAD). A root that is not a git checkout hashes
    every file under it minus Handsoff's generated names. A tracked file
    deleted from the working tree contributes a null hash, so a deletion
    changes the digest too. Gitignored Handsoff state files are omitted by
    git's untracked-file query, preventing runtime bookkeeping from causing
    evidence drift."""
    names = cfg or DEFAULT_CONFIG
    state_files = {names["status_file"], names["acceptance_file"], names["event_log"], names["verification_log"]}
    pairs = []
    for relative in _digest_listing(root, names):
        if _digest_excluded(relative, state_files):
            continue
        pairs.append([relative, _digest_entry(root, relative)])
    return hashlib.sha256(_canonical({"files": pairs}).encode("utf-8")).hexdigest()


def repository_digest_entries(root: Path, cfg: dict | None = None) -> dict[str, str | None]:
    """Return per-path working-tree digests so a sandbox violation can name files.

    Handsoff state is excluded for the same reason as repository_digest: its own
    bookkeeping must not look like an agent edit.
    """
    names = cfg or load_config(root)
    state_files = {names["status_file"], names["acceptance_file"], names["event_log"], names["verification_log"]}
    return {path: _digest_entry(root, path) for path in _digest_listing(root, names)
            if not _digest_excluded(path, state_files)}


def record_session_result(root: Path, session_id: str, kind: str, payload: dict, *,
                          recovered_from_rule: bool = False, refused_text: str | None = None) -> dict:
    """Persist validated protocol state before broker dispatch can lose it.
    #167: `recovered_from_rule` marks a packet a rule refused; `refused_text`
    is that packet exactly as the reviewer wrote it, so session-result-adopt
    re-parses the preserved text (repairing only the field the rule names)
    rather than trusting a copy made at refusal time."""
    if kind not in {"review", "design", "supervisor_request"} or not isinstance(payload, dict):
        raise HandsoffError("session result is invalid")
    if refused_text is not None and (not isinstance(refused_text, str) or len(refused_text) > 65536):
        raise HandsoffError("session result refused_text is invalid")
    with project_lock(root.resolve()):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict):
            raise HandsoffError("agent session was not found")
        result = {"kind": kind, "payload": deepcopy(payload),
                  "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "adopted_at": None, "adopted_by": None}
        if recovered_from_rule:
            result["recovered_from_rule"] = True
        if refused_text is not None:
            result["refused_text"] = refused_text
        proposed = deepcopy(status)
        proposed["agent_sessions"][session_id]["result"] = result
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="agent_session_result_recorded",
               event_message="Managed agent protocol result persisted (recovered from a refused packet)"
               if recovered_from_rule else "Managed agent protocol result persisted",
               session_id=session_id, result_kind=kind, recovered_from_rule=recovered_from_rule)
        return deepcopy(result)


def verification_config_hash(cfg: dict) -> str:
    """The configuration that changes what a targeted check proves: the
    governance keys, [checks].commands, the per-command timeout, and the
    regression groups. Distinct from config_hash (which binds decisions to
    governance policy alone) and deliberately blind to file paths,
    [agents], [models], [fallback_policy], [recovery], [design_evidence]
    and tickets, none of which alter a check's meaning."""
    bound = {key: cfg.get(key) for key in GOVERNANCE_CONFIG_KEYS}
    bound["check_commands"] = list(cfg.get("check_commands", []))
    bound["check_timeout_seconds"] = cfg.get("check_timeout_seconds")
    bound["regressions"] = [{"name": group.get("name"), "commands": list(group.get("commands", []))}
                            for group in cfg.get("regressions", [])]
    return hashlib.sha256(_canonical(bound).encode("utf-8")).hexdigest()


def verification_binding(command: str, repo_digest: str, config_digest: str,
                         criterion_hashes: list[str]) -> str:
    """The cache key for one command: any change to the command, the
    repository content, the verification configuration, or the spec of a
    criterion being verified with it produces a different binding."""
    return hashlib.sha256(_canonical({
        "command": command,
        "repository_digest": repo_digest,
        "verification_config_hash": config_digest,
        "criterion_hashes": sorted(criterion_hashes),
    }).encode("utf-8")).hexdigest()


def feature_hash(status: dict, events: list[dict]) -> str:
    """Identity of the current run: sha256 of the feature text plus the
    timestamp of the run's first event (the `initialized` record), the same
    anchor the regression gate uses for its run id. A record from an
    earlier run of the same feature therefore never satisfies this one."""
    initialized_at = str(events[0].get("at") or "") if events else ""
    return hashlib.sha256((str(status.get("feature") or "") + initialized_at).encode("utf-8")).hexdigest()


def check_result_reusable(result: object) -> bool:
    return (isinstance(result, dict) and result.get("exit_code") == 0
            and result.get("timed_out") is False and result.get("truncated") is False)


def reusable_check_record(records: list[dict], command: str, binding: str,
                          feature_hash: str) -> dict | None:
    """The latest ledger record that proves `command` passed against this
    exact binding within the current run. Eligibility is all of: kind
    checks, ok, executed (a reused record is never itself a source), the
    same feature hash, the record's binding for this command equal, and
    every result in the record exit 0, not timed out, not truncated. One
    bad result anywhere in the record disqualifies the whole record."""
    for record in reversed(records):
        if not isinstance(record, dict) or record.get("kind") != "checks":
            continue
        if record.get("ok") is not True or record.get("executed") is not True:
            continue
        if record.get("feature_hash") != feature_hash:
            continue
        bindings = record.get("binding")
        if not isinstance(bindings, dict) or bindings.get(command) != binding:
            continue
        results = record.get("results")
        if not isinstance(results, list) or not results:
            continue
        if not all(check_result_reusable(result) for result in results):
            continue
        if not any(result.get("command") == command for result in results):
            continue
        return record
    return None


def verify_inflight_lock_path(root: Path, binding: str) -> Path:
    return root / VERIFY_INFLIGHT_DIR / f"{binding}.lock"

def verify_inflight_bindings(root: Path) -> list[str]:
    """Binding ids whose verify lock is held right now. Lock files are
    never unlinked (unlinking a flock file races with the next opener), so
    a file on disk proves nothing; only a lock that refuses LOCK_NB is in
    flight (#72). Without fcntl nothing can be proven, so nothing is
    reported."""
    directory = root / VERIFY_INFLIGHT_DIR
    if fcntl is None or not directory.is_dir():
        return []
    held = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            with path.open("r+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    held.append(path.name[:-5] if path.name.endswith(".lock") else path.name)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            continue
    return held



@contextmanager
def verify_inflight_lock(root: Path, bindings: list[str]):
    """Serialize concurrent launches of the same bindings. Locks are taken
    in sorted order (no two callers can wait on each other) and held until
    the caller has appended its records, so the second verify of a binding
    finds the first one's record when it re-reads the ledger. Best effort
    without fcntl, like project_lock."""
    if fcntl is None or not bindings:
        yield
        return
    directory = root / VERIFY_INFLIGHT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    handles = []
    try:
        for binding in sorted(set(bindings)):
            path = verify_inflight_lock_path(root, binding)
            path.touch(exist_ok=True)
            fh = path.open("r+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            handles.append(fh)
        yield
    finally:
        for fh in reversed(handles):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


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
        "proposal_hash": review.get("proposal_hash"),
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
        "proposal_changed": previous.get("proposal_hash") is not None and \
                            previous.get("proposal_hash") != (status.get("design_proposal") or {}).get("proposal_hash"),
        "previous_proposal_hash": previous.get("proposal_hash"),
        "proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
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
# runtime failure classification: lets the Supervisor distinguish
# a launched agent that is working from one that failed, exhausted quota or
# context, or was cancelled/timed out. The public launcher routes classified
# failures through execute_with_recovery in handsoff_agent.py.
# Deliberately diverges from run_checks() above: that function keeps a raw
# output_tail because check-command output is trusted, first-party text.
# Third-party agent stderr/stdout is not -- it can be credential-bearing --
# so nothing here ever returns raw text, only a digest and a label drawn
# from a fixed, closed set.
# --------------------------------------------------------------------------

FAILURE_CATEGORIES = (
    "cancelled", "timeout", "token_budget_exhaustion", "orchestration_noop",
    "auth_failure", "rate_limit", "context_exhaustion",
    "runtime_environment", "process_crash", "non_zero_exit", "unknown", "still_running", "presumed_lost",
    "reviewer_modified_project",
    "network", "target_service", "external_timeout", "dispatch_failed", "no_artifact",
    "protocol_silence",
)

_FAILURE_REASON_LABELS = {
    "protocol_silence": "session produced no protocol output within the configured limit",
    "cancelled": "run was cancelled",
    "timeout": "runner exceeded its timeout",
    "token_budget_exhaustion": "managed role exhausted its token budget",
    "orchestration_noop": "Supervisor exited without a broker request or Pilot question",
    "auth_failure": "authentication or authorization failed",
    "rate_limit": "rate limit or quota exhausted",
    "context_exhaustion": "context window exhausted",
    "runtime_environment": "managed runtime initialization failed",
    "process_crash": "process was terminated by a signal",
    "non_zero_exit": "process exited with a non-zero status",
    "unknown": "failure signal matched no known category",
    "still_running": "no failure signal reported yet",
    "presumed_lost": "host watchdog found no liveness signal past the threshold",
    "reviewer_modified_project": "managed Reviewer modified the project tree",
    "external_timeout": "external operation exceeded its declared timeout",
    "dispatch_failed": "host dispatch failed",
    "no_artifact": "process exited 0 without a protocol result",
    "network": "network connection to a dependency failed",
    "target_service": "target service reported a failure",
}

# Order matters: tier 3 checks these in sequence and the first match wins,
# even when a tail matches more than one (e.g. an auth error surfaced while
# refreshing a rate-limited token) -- there is no such thing as an
# ambiguous double-classification, only a fixed tie-break.
_TAIL_PATTERNS = (
    ("token_budget_exhaustion", re.compile(
        r"shared rollout token budget exhausted|token budget exhausted", re.IGNORECASE,
    )),
    ("auth_failure", re.compile(r"unauthorized|authentication failed|invalid api key|401", re.IGNORECASE)),
    ("rate_limit", re.compile(r"rate limit|too many requests|quota exceeded|429", re.IGNORECASE)),
    ("context_exhaustion", re.compile(r"context length exceeded|context window|maximum context|prompt is too long", re.IGNORECASE)),
    ("runtime_environment", re.compile(
        r"readonly database|read-only database|failed to initialize.*app-server|"
        r"cannot establish repository identity|operation not permitted|SSL certificate verification failed|permission denied",
        re.IGNORECASE,
    )),
)


def classify_runtime_failure(*, exit_code: int | None = None, timed_out: bool = False,
                              cancelled: bool = False, orchestration_noop: bool = False,
                              stderr_tail: str = "",
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
    elif orchestration_noop:
        category = "orchestration_noop"
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
    "runtime_environment", "process_crash", "non_zero_exit", "presumed_lost", "external_timeout",
    "no_artifact", "protocol_silence",
}
FALLBACK_SKIP_REASONS = {
    "invalid_profile", "adapter_unavailable", "already_attempted", "reviewer_not_independent", "environment_failure",
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
        if failure_category == "runtime_environment":
            skipped.append({"index": index, "reason": "environment_failure"})
            continue
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


def tokens_per_ticket(archives_dir: Path | None = None) -> dict:
    """#168: tokens recorded per closed issue work item, per repository
    root, from archived product runs only. A run's recorded total is
    attributed to each of its issue work items (a run that closed three
    tickets cost that much for the three together; the number is shown
    against each and named as shared). A ticket whose run recorded no
    usage reads not reported, never zero."""
    directory = Path(archives_dir) if archives_dir is not None else archive_dir()
    result: dict[str, dict] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("run_kind") == "test":
            continue
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
        total = usage.get("tokens_total") if usage and usage.get("sessions_reported") else None
        acceptance = record.get("acceptance") if isinstance(record.get("acceptance"), dict) else {}
        items = [item for item in (acceptance.get("work_items") or [])
                 if isinstance(item, dict) and item.get("kind") == "issue" and isinstance(item.get("number"), int)]
        root = str(record.get("root") or "")
        for item in items:
            entry = result.setdefault(root, {"repo": record.get("repo"), "tickets": {}})
            entry["tickets"][str(item["number"])] = {
                "tokens_total": total, "reported": total is not None, "shared_with": len(items) - 1,
                "run": path.name, "completed_at": record.get("completed_at")}
    return result


def run_kind_for(root: Path) -> str:
    """#49: "test" or "product". An explicit HANDSOFF_RUN_KIND of exactly
    test or product wins; otherwise a root whose name starts with one of
    FIXTURE_ROOT_PREFIXES is a test run, and everything else is product.
    Written into every archive so the analyzer never mines fixtures."""
    explicit = os.environ.get("HANDSOFF_RUN_KIND", "").strip()
    if explicit in RUN_KINDS:
        return explicit
    name = Path(root).name
    if any(name.startswith(prefix) for prefix in FIXTURE_ROOT_PREFIXES):
        return "test"
    return "product"


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


def _sum_known(values) -> int | None:
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    return sum(known) if known else None


def build_run_metrics(status: dict, events: list[dict], verifications: list[dict], *,
                      now: datetime | None = None, sleep: list | None = None) -> dict:
    """Derive content-free delivery metrics from already-audited records.

    Token fields stay null unless an adapter eventually supplies structured,
    authoritative usage. Handsoff never estimates usage from text length.
    """
    now = now or datetime.now(timezone.utc)

    def parsed(value):
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    ordered_events = sorted(
        (item for item in events if isinstance(item, dict) and parsed(item.get("at"))),
        key=lambda item: item["at"],
    )
    start = parsed(ordered_events[0]["at"]) if ordered_events else parsed(status.get("updated_at"))
    complete = status.get("status") == "complete"
    end = parsed(status.get("updated_at")) if complete else now
    # #193: every duration is awake time; the sleep beside it is reported
    sleep = machine_sleep_intervals(now=now) if sleep is None else sleep
    elapsed = awake_seconds(start, end, sleep) if start and end else None
    run_asleep = asleep_seconds(start, end, sleep) if start and end else 0.0

    phase_cursor = start
    phase_number = int(status.get("phase_number", 1) or 1) if not ordered_events else 1
    phase_seconds = {str(number): 0.0 for number in PHASES}
    phase_asleep = {str(number): 0.0 for number in PHASES}
    for event in ordered_events:
        if event.get("kind") == "initialized" and isinstance(event.get("phase_number"), int):
            phase_number = event["phase_number"]
            phase_cursor = parsed(event.get("at")) or phase_cursor
            continue
        if event.get("kind") != "phase_advanced" or not isinstance(event.get("phase_number"), int):
            continue
        at = parsed(event.get("at"))
        if phase_cursor and at and at >= phase_cursor:
            phase_seconds[str(phase_number)] += awake_seconds(phase_cursor, at, sleep) or 0.0
            phase_asleep[str(phase_number)] += asleep_seconds(phase_cursor, at, sleep)
        phase_number = event["phase_number"]
        phase_cursor = at or phase_cursor
    if phase_cursor and end and end >= phase_cursor:
        phase_seconds[str(phase_number)] += awake_seconds(phase_cursor, end, sleep) or 0.0
        phase_asleep[str(phase_number)] += asleep_seconds(phase_cursor, end, sleep)

    sessions = []
    for item in (status.get("agent_sessions") or {}).values():
        if not isinstance(item, dict):
            continue
        began = parsed(item.get("running_at") or item.get("started_at"))
        ended = parsed(item.get("ended_at")) or (now if item.get("state") in AGENT_SESSION_LIVE_STATES else None)
        duration = awake_seconds(began, ended, sleep) if began and ended else None
        sessions.append({
            "session_id": item.get("session_id"), "role": item.get("role"),
            "adapter": item.get("adapter"),
            "model": item.get("reported_model") or item.get("requested_model"),
            "phase_number": item.get("phase_number"), "state": item.get("state"),
            "duration_seconds": round(duration, 3) if duration is not None else None,
            "input_tokens": (item.get("usage") or {}).get("tokens_in"),
            "output_tokens": (item.get("usage") or {}).get("tokens_out"),
            "cached_tokens": None,
            "total_tokens": (item.get("usage") or {}).get("tokens_total"),
            # #168: 'adapter' when the adapter printed its usage, else what
            # the session says (not reported / disabled), 'unavailable' for
            # a session recorded before usage existed.
            "usage_source": (item.get("usage") or {}).get("source", "unavailable"),
        })
    sessions.sort(key=lambda item: str(item.get("session_id") or ""))
    failures = sum(item.get("state") in (AGENT_SESSION_TERMINAL_STATES - {"completed"}) for item in sessions)
    verification_seconds = 0.0
    for record in verifications:
        for result in (record.get("results") or []) if isinstance(record, dict) else []:
            value = result.get("duration_s") if isinstance(result, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                verification_seconds += float(value)
    pilot_wait_seconds = 0.0
    background_wait_seconds = 0.0
    open_waits = {"human": None, "background": None}
    wait_events = {
        "human_pause_started": ("human", True), "human_pause_ended": ("human", False),
        "background_wait_started": ("background", True),
        "background_wait_ended": ("background", False),
    }
    for event in ordered_events:
        mapping = wait_events.get(event.get("kind"))
        if not mapping:
            continue
        key, opening = mapping
        at = parsed(event.get("at"))
        if opening:
            open_waits[key] = at
        elif open_waits[key] and at and at >= open_waits[key]:
            seconds = awake_seconds(open_waits[key], at, sleep) or 0.0
            if key == "human":
                pilot_wait_seconds += seconds
            else:
                background_wait_seconds += seconds
            open_waits[key] = None
    for key, began in open_waits.items():
        if began and end and end >= began:
            if key == "human":
                pilot_wait_seconds += awake_seconds(began, end, sleep) or 0.0
            else:
                background_wait_seconds += awake_seconds(began, end, sleep) or 0.0
    known_usage = [item for item in sessions if item["total_tokens"] is not None]
    largest_sessions = sorted(
        sessions, key=lambda item: item["duration_seconds"] if item["duration_seconds"] is not None else -1,
        reverse=True,
    )[:5]
    return {
        "generated_at": now.isoformat(),
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "asleep_seconds": round(run_asleep, 3),  # #193
        "phase_asleep_seconds": {k: round(v, 3) for k, v in phase_asleep.items()},
        # Mission clock anchors: the page ticks from started_at itself and
        # freezes at ended_at once the run is complete.
        "started_at": start.isoformat() if start else None,
        "ended_at": end.isoformat() if complete and end else None,
        "phase_started_at": phase_cursor.isoformat() if phase_cursor else None,
        "phase_seconds": {key: round(value, 3) for key, value in phase_seconds.items()},
        "verification_seconds": round(verification_seconds, 3),
        "pilot_wait_seconds": round(pilot_wait_seconds, 3),
        "background_wait_seconds": round(background_wait_seconds, 3),
        "managed_sessions": len(sessions), "failed_sessions": failures,
        "replacement_count": len(status.get("agent_replacements") or []),
        "recovery_attempts": len(status.get("recovery_attempts") or []),
        "design_review_attempts": int(status.get("design_review_attempts", 0) or 0),
        "implementation_review_attempts": len(status.get("review_attempts") or []),
        "verification_runs": len(verifications),
        "tokens": {
            # #168: a component the adapter did not print stays None (Codex
            # prints one total), never a made-up zero.
            "input": _sum_known(item["input_tokens"] for item in known_usage),
            "output": _sum_known(item["output_tokens"] for item in known_usage),
            "cached": _sum_known(item["cached_tokens"] for item in known_usage),
            "total": _sum_known(item["total_tokens"] for item in known_usage),
            "coverage": f"{len(known_usage)}/{len(sessions)} sessions",
        },
        "sessions": sessions,
        "largest_sessions": largest_sessions,
        "baseline": {"state": "unavailable", "sample_size": 0,
                     "reason": "no compatible historical cohort selected"},
    }


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
        "run_kind": run_kind_for(root),
        "feature": status.get("feature"),
        "started_at": events[0]["at"] if events else status.get("updated_at"),
        "completed_at": status.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "acceptance": acceptance,
        "verifications": verifications,
        "events": events,
        "metrics": build_run_metrics(status, events, verifications),
        "usage": usage_totals(status),  # #168
    }
    out_dir = archive_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{_archive_slug(root.name)}-{stamp}-{_archive_slug(status.get('feature'))}.json"
    out_path = out_dir / filename
    out_path.write_text(json.dumps(record, indent=1, sort_keys=True))
    return out_path


def record_pilot_note(root: Path, *, by: object, text: object) -> dict:
    """#49: append a `pilot_note` event to the CURRENT run's ledger. The
    note changes nothing about phase, progress, or evidence; the next
    archive scan lists each distinct note as an R7 finding, which is how
    the Pilot's own observations become filed improvement tickets."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("pilot note: --by must be a non-empty string")
    if not isinstance(text, str):
        raise HandsoffError("pilot note: text must be a string")
    clean = " ".join(text.split())
    if not 1 <= len(clean) <= MAX_PILOT_NOTE_LENGTH:
        raise HandsoffError(f"pilot note: text must be 1 to {MAX_PILOT_NOTE_LENGTH} characters")
    root = Path(root).resolve()
    with project_lock(root):
        cfg = load_config(root)
        if not status_path(root, cfg).exists():
            raise HandsoffError("pilot note: no current run (run init first)")
        commit(root, cfg, event_kind="pilot_note", event_message=f"Pilot note from {by.strip()}",
               by=by.strip(), text=clean)
    return {"by": by.strip(), "text": clean}


# --------------------------------------------------------------------------
# #193: the machine's sleep. Two runs read "design debate for 7 hours" while
# the Mac had been asleep for most of it. macOS logs every transition with a
# timestamp and its own UTC offset; every duration the board shows is wall
# clock minus the overlap with those intervals, and says how long it slept.
# Nothing in the ledger changes; sleep is subtracted at read time.
# --------------------------------------------------------------------------

SLEEP_LOG_CACHE_SECONDS = 60
_SLEEP_LOG_CACHE: dict = {"at": None, "intervals": [], "thread": None}
_SLEEP_LOG_LOCK = threading.Lock()
_SLEEP_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4}) (Sleep|Wake|DarkWake)\b")


def _read_pmset_log() -> str | None:
    """`pmset -g log` on macOS; None where it does not exist or fails."""
    if shutil.which("pmset") is None:
        return None
    try:
        result = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=20, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_sleep_log(text: str, *, now: datetime) -> list[tuple[datetime, datetime]]:
    """[(slept_at, woke_at)] in UTC from pmset's log. Each line carries its
    own offset ('2026-09-21 06:36:06 -0400 Sleep'), parsed arithmetically,
    never through a zone name, so a DST change is two lines with different
    offsets and nothing is ambiguous. 'Sleep' opens an interval; only 'Wake'
    closes it; 'DarkWake' (maintenance) leaves it open; an open interval
    closes at now. Overlapping or touching intervals merge."""
    intervals: list[tuple[datetime, datetime]] = []
    open_at: datetime | None = None
    for line in text.splitlines():
        match = _SLEEP_LINE.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1) + match.group(2), "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            continue
        kind = match.group(3)
        if kind == "Sleep":
            if open_at is None or stamp < open_at:
                open_at = stamp if open_at is None else open_at
        elif kind == "Wake" and open_at is not None:
            if stamp > open_at:
                intervals.append((open_at, stamp))
            open_at = None
    if open_at is not None and now > open_at:
        intervals.append((open_at, now))
    intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _refresh_sleep_cache(now: datetime) -> None:
    text = _read_pmset_log()
    intervals = parse_sleep_log(text, now=now) if isinstance(text, str) else []
    with _SLEEP_LOG_LOCK:
        _SLEEP_LOG_CACHE["at"] = now
        _SLEEP_LOG_CACHE["intervals"] = intervals
        _SLEEP_LOG_CACHE["thread"] = None


def machine_sleep_intervals(*, now: datetime | None = None, log_reader=None, wait: bool = False) -> list[tuple[datetime, datetime]]:
    """The machine's sleep intervals from a per-process cache refreshed
    every SLEEP_LOG_CACHE_SECONDS. `pmset -g log` is tens of thousands of
    lines and takes seconds, so the refresh runs on a background thread
    and never on a request: a read before the first refresh lands returns
    [] (wall clock) rather than holding the page. `wait=True` blocks for
    the refresh (the CLI, tests). An injected log_reader (tests) bypasses
    the cache and pmset."""
    now = now or datetime.now(timezone.utc)
    if log_reader is not None:
        text = log_reader()
        return parse_sleep_log(text, now=now) if isinstance(text, str) else []
    with _SLEEP_LOG_LOCK:
        cached_at = _SLEEP_LOG_CACHE["at"]
        fresh = cached_at is not None and (now - cached_at).total_seconds() < SLEEP_LOG_CACHE_SECONDS
        running = _SLEEP_LOG_CACHE["thread"]
        if fresh:
            return list(_SLEEP_LOG_CACHE["intervals"])
        if running is None or not running.is_alive():
            running = threading.Thread(target=_refresh_sleep_cache, args=(now,), daemon=True, name="handsoff-sleep-log")
            _SLEEP_LOG_CACHE["thread"] = running
            running.start()
        stale = list(_SLEEP_LOG_CACHE["intervals"])
    if wait:
        running.join(timeout=30)
        with _SLEEP_LOG_LOCK:
            return list(_SLEEP_LOG_CACHE["intervals"])
    return stale


def asleep_seconds(start: datetime | None, end: datetime | None, intervals: list[tuple[datetime, datetime]]) -> float:
    """How much of [start, end] the machine spent asleep; never negative."""
    if start is None or end is None or end <= start:
        return 0.0
    total = 0.0
    for slept, woke in intervals:
        overlap = (min(end, woke) - max(start, slept)).total_seconds()
        if overlap > 0:
            total += overlap
    return min(total, (end - start).total_seconds())


def awake_seconds(start: datetime | None, end: datetime | None, intervals: list[tuple[datetime, datetime]]) -> float | None:
    """Wall clock minus sleep; None without both ends; never negative."""
    if start is None or end is None:
        return None
    wall = max((end - start).total_seconds(), 0.0)
    return max(wall - asleep_seconds(start, end, intervals), 0.0)


# --------------------------------------------------------------------------
# #186: which host drove the run. Two hosts (Claude, Codex) run lanes side
# by side; the page said "host" for both. The family is read from an actor
# prefix (the #164 rule), never inferred from anything else.
# --------------------------------------------------------------------------

ACTOR_FAMILY_PREFIXES = ("claude-", "codex-")
#: events written by the host's own commands (never by a managed session)
HOST_COMMAND_EVENT_KINDS = frozenset({
    "criteria_transaction_applied", "criterion_added", "criterion_updated", "criterion_removed",
    "design_proposal_recorded", "evidence_recorded", "review_attempt_opened", "symptom_resolved",
    "ci_watch_started", "work_item_updated", "pilot_note", "run_closed",
})


def actor_family(actor: object) -> str | None:
    """'claude' or 'codex' from an actor's prefix; None for anything else."""
    if not isinstance(actor, str):
        return None
    for prefix in ACTOR_FAMILY_PREFIXES:
        if actor.startswith(prefix):
            return prefix[:-1]
    return None


def host_identity(status: dict, events: list[dict]) -> dict:
    """{family, actor, source}: the actor `init --by` recorded on
    `initialized` (source initialized); else the newest actor on a
    host-side command event or status.implemented_by (source ledger);
    else unknown with source none. A family is only ever an actor prefix."""
    events = [e for e in events if isinstance(e, dict)]
    for event in events:
        if event.get("kind") == "initialized" and actor_family(event.get("by")):
            return {"family": actor_family(event["by"]), "actor": event["by"], "source": "initialized"}
    for event in reversed(events):
        if event.get("kind") in HOST_COMMAND_EVENT_KINDS and actor_family(event.get("by")):
            return {"family": actor_family(event["by"]), "actor": event["by"], "source": "ledger"}
    implemented = status.get("implemented_by") if isinstance(status, dict) else None
    if actor_family(implemented):
        return {"family": actor_family(implemented), "actor": implemented, "source": "ledger"}
    return {"family": "unknown", "actor": None, "source": "none"}


def host_wait_view(status: dict, events: list[dict], cfg: dict, *, now: datetime | None = None,
                   pilot_input_required: bool = False) -> dict | None:
    """#194: who the run is waiting on, when the ball is with the host.
    None when the run is closed or complete, a managed session is
    launching or running, the Pilot's own input is pending (a decision
    card is on the page), or the ledger has been written to within the
    stall threshold. Ledger silence is measured from the newest of
    status.updated_at and the last event's `at` (design review F1.2): a
    fresh heartbeat or managed-session output is not the host acting.
    Otherwise the host family (an actor prefix, never a guess), when the
    ledger was last written, how long since, and the action the host owes:
    the authorized design-review attempt when one is unconsumed, else the
    run's next_action. Computed at read time; nothing is written."""
    if not isinstance(status, dict):
        return None
    if isinstance(status.get("run_closed"), dict) or status.get("status") == "complete" \
            or int(status.get("phase_number", 0) or 0) >= 8:
        return None
    if pilot_input_required or isinstance(status.get("human_pause"), dict):
        return None
    sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}
    if any(isinstance(s, dict) and s.get("state") in ("launching", "running") for s in sessions.values()):
        return None
    now = now or datetime.now(timezone.utc)
    times = [e.get("at") for e in events if isinstance(e, dict) and isinstance(e.get("at"), str)]
    if isinstance(status.get("updated_at"), str):
        times.append(status["updated_at"])
    since = max(times) if times else None
    silent = _iso_seconds(since, now.isoformat()) if since else None
    # #193: silence is awake time; a closed lid is not the host ignoring the run
    slept = 0.0
    if silent is not None:
        try:
            began = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            slept = asleep_seconds(began, now, machine_sleep_intervals(now=now))
            silent = max(silent - slept, 0.0)
        except (TypeError, ValueError):
            slept = 0.0
    limit = float(cfg.get("stall_minutes", 10) or 10) * 60
    if silent is None or silent < limit:
        return None
    host = host_identity(status, events)
    authorization = status.get("design_review_authorization")
    if isinstance(authorization, dict) and authorization.get("consumed_at") is None \
            and authorization.get("attempt_permitted") is not None:
        action = f"launch design-review attempt {authorization['attempt_permitted']}"
        launch_role = "reviewer"
    else:
        action = str(status.get("next_action") or "the next step")
        launch_role = None
    return {"family": host["family"], "actor": host["actor"], "since": since,
            "silent_seconds": silent, "asleep_seconds": round(slept, 3), "action": action, "launch_role": launch_role}


# --------------------------------------------------------------------------
# #181: CI as a step of the run. Since #178 a lane lands through a pull
# request and waits for the required check; `ci-watch` records what is
# being waited for, `ci_view` mirrors the PR's checks onto the snapshot at
# most once a minute, and a red check refuses the Phase 7 transition until
# a new head is watched. The engine reads gh's output and never merges.
# --------------------------------------------------------------------------

CI_FILE = ".handsoff-ci.json"
CI_REFRESH_SECONDS = 60
CI_STATES = ("running", "passed", "failed")
CI_NO_HISTORY_NOTE = "no previous run to compare"
#: gh check states that mean "finished"; anything else is still running
CI_CHECK_DONE = {"SUCCESS", "FAILURE", "CANCELLED", "SKIPPED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "NEUTRAL"}
CI_CHECK_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}
#: the only keys ever copied out of a gh record into the ledger or the side
#: file; nothing from gh's environment or its other fields is read
CI_CHECK_FIELDS = ("name", "state", "startedAt", "completedAt", "link", "workflow")


def ci_side_path(root: Path) -> Path:
    return Path(root) / CI_FILE


def _gh_json(root: Path, args: list[str], runner=None, which=None) -> object:
    """Run gh with --json arguments and parse its stdout. The runner and
    which are injectable so tests never reach the real gh."""
    runner = runner or subprocess.run
    which = which or shutil.which
    if which("gh") is None:
        raise HandsoffError("ci-watch needs the gh CLI on PATH (https://cli.github.com), signed in")
    try:
        result = runner(["gh", *args], cwd=str(root), shell=False, text=True,
                        capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandsoffError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise HandsoffError(f"gh {' '.join(args[:2])} failed: {tail[-1] if tail else 'no output'}")
    try:
        return json.loads(result.stdout or "null")
    except ValueError as exc:
        raise HandsoffError(f"gh {' '.join(args[:2])} returned something other than JSON") from exc


def _ci_checks(root: Path, pr: int, runner=None, which=None) -> list[dict]:
    raw = _gh_json(root, ["pr", "checks", str(pr), "--json", ",".join(CI_CHECK_FIELDS)], runner, which)
    checks = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        checks.append({
            "name": item["name"],
            "state": str(item.get("state") or "PENDING").upper(),
            "started_at": item.get("startedAt") if isinstance(item.get("startedAt"), str) else None,
            "completed_at": item.get("completedAt") if isinstance(item.get("completedAt"), str) else None,
            "link": item.get("link") if isinstance(item.get("link"), str) else None,
            "workflow": item.get("workflow") if isinstance(item.get("workflow"), str) else None,
        })
    return sorted(checks, key=lambda c: c["name"])


def _iso_seconds(start: object, end: object) -> float | None:
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    seconds = (b - a).total_seconds()
    return seconds if seconds >= 0 else None


CI_EXPECTED_SAMPLE = 5


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def ci_expected_seconds(root: Path, workflows: list[str], runner=None, which=None) -> tuple[float | None, str | None]:
    """The time to expect for this PR's checks. Per workflow named on them
    (sorted, so the choice is deterministic): the last CI_EXPECTED_SAMPLE
    successful runs on main, each measured as its longest job's startedAt
    to completedAt (the work; a run's createdAt to updatedAt would count
    the minutes a job sat queued, which is what made one run read 6 min
    for 1.5 min of work), the median of those. The largest workflow's
    median wins, since the wait is the slowest workflow; expected_source
    names the runs counted. (None, None) with no completed run yet."""
    best: tuple[float, str] | None = None
    for name in sorted({w for w in workflows if isinstance(w, str) and w.strip()}):
        runs = _gh_json(root, ["run", "list", "--workflow", name, "--branch", "main", "--status", "success",
                               "--limit", str(CI_EXPECTED_SAMPLE), "--json", "databaseId,url"], runner, which)
        samples: list[float] = []
        urls: list[str] = []
        for run in runs if isinstance(runs, list) else []:
            if not isinstance(run, dict) or run.get("databaseId") is None:
                continue
            detail = _gh_json(root, ["run", "view", str(run["databaseId"]), "--json", "jobs"], runner, which)
            jobs = detail.get("jobs") if isinstance(detail, dict) else None
            longest = None
            for job in jobs if isinstance(jobs, list) else []:
                seconds = _iso_seconds(job.get("startedAt"), job.get("completedAt")) if isinstance(job, dict) else None
                if seconds is not None and (longest is None or seconds > longest):
                    longest = seconds
            if longest is not None:
                samples.append(longest)
                if isinstance(run.get("url"), str):
                    urls.append(run["url"])
        median = _median(samples)
        if median is not None and (best is None or median > best[0]):
            best = (median, ", ".join(urls))
    if best is None:
        return None, None
    return best[0], best[1] or None


def _write_ci_side(root: Path, head: str, checks: list[dict], fetched_at: str) -> dict:
    record = {"head": head, "fetched_at": fetched_at, "checks": checks}
    atomic_write_json(ci_side_path(root), record)
    return record


def _read_ci_side(root: Path) -> dict | None:
    path = ci_side_path(root)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("checks"), list) else None


def ci_watch_start(root: Path, cfg: dict, *, pr: object, by: object, runner=None, which=None,
                   now: datetime | None = None) -> dict:
    """Record that the run is waiting on pull request `pr`: its head, URL,
    the checks it carries and the time the last successful run of the same
    workflow(s) took. A watch on a new head replaces the previous one, which
    is how a red watch is cleared after a fix is pushed. Caller does not
    hold project_lock; this takes it."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("ci-watch: --by must be a non-empty string")
    try:
        number = int(pr)
    except (TypeError, ValueError):
        raise HandsoffError("ci-watch: --pr must be a pull request number") from None
    if number < 1:
        raise HandsoffError("ci-watch: --pr must be a pull request number")
    root = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    view = _gh_json(root, ["pr", "view", str(number), "--json", "number,url,headRefOid"], runner, which)
    if not isinstance(view, dict) or not isinstance(view.get("headRefOid"), str):
        raise HandsoffError(f"ci-watch: gh pr view {number} did not return a head commit")
    head = view["headRefOid"]
    url = view.get("url") if isinstance(view.get("url"), str) else None
    checks = _ci_checks(root, number, runner, which)
    expected, source = ci_expected_seconds(root, [c["workflow"] for c in checks], runner, which)
    with project_lock(root):
        if not status_path(root, cfg).exists():
            raise HandsoffError("ci-watch: no current run (run init first)")
        status = load_unique_json(status_path(root, cfg))
        status["ci"] = {
            "pr": number, "head": head, "url": url, "state": "running",
            "started_at": now.isoformat(), "expected_seconds": expected, "expected_source": source,
            "failed_check": None, "watched_by": by.strip(),
        }
        _write_ci_side(root, head, checks, now.isoformat())
        commit(root, cfg, status=status, event_kind="ci_watch_started",
               event_message=f"Watching CI on PR #{number} ({len(checks)} checks)",
               by=by.strip(), pr=number, head=head, url=url, checks=[c["name"] for c in checks],
               expected_seconds=expected, expected_source=source)
    return dict(status["ci"])


def _ci_terminal(checks: list[dict]) -> tuple[str | None, str | None]:
    """("passed"|"failed"|None, failing check name). None while any check is
    still running or the PR carries no checks yet."""
    if not checks or any(c["state"] not in CI_CHECK_DONE for c in checks):
        return None, None
    red = next((c for c in checks if c["state"] in CI_CHECK_RED), None)
    return ("failed", red["name"]) if red else ("passed", None)


def ci_view(status: dict, root: Path, cfg: dict, *, now: datetime | None = None,
            runner=None, which=None, refresh_seconds: int = CI_REFRESH_SECONDS,
            force: bool = False) -> dict | None:
    """The CI block for the snapshot, or None when no watch is recorded.
    While the watch is running the checks are refreshed through gh when the
    side file is older than `refresh_seconds` (the #158 conditional
    pattern); the first time every check has completed the terminal event
    is committed once (guarded by status.ci.state, re-read under the lock
    like record_stall_transition). A terminal watch is served from the side
    file and never asks gh again."""
    watch = status.get("ci") if isinstance(status, dict) else None
    if not isinstance(watch, dict) or watch.get("state") not in CI_STATES:
        return None
    root = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    side = _read_ci_side(root)
    checks = list(side["checks"]) if side and side.get("head") == watch.get("head") else []
    fetched_at = side.get("fetched_at") if side and side.get("head") == watch.get("head") else None
    note = None
    # A red watch is refreshed too: a rerun of the failed job greens the
    # same head without a new commit, and the row must see it (#181).
    if watch.get("state") in ("running", "failed"):
        age = _iso_seconds(fetched_at, now.isoformat()) if fetched_at else None
        if force or age is None or age >= refresh_seconds:
            try:
                checks = _ci_checks(root, int(watch["pr"]), runner, which)
                fetched_at = now.isoformat()
                _write_ci_side(root, str(watch.get("head")), checks, fetched_at)
            except HandsoffError as exc:
                note = str(exc)
        terminal, failing = _ci_terminal(checks)
        # commit the terminal state once: running to passed or failed, and
        # failed to passed after a rerun; never failed to failed again
        if terminal and (watch.get("state") == "running" or terminal == "passed"):
            with project_lock(root):
                current = load_unique_json(status_path(root, cfg))
                live = current.get("ci") if isinstance(current.get("ci"), dict) else None
                if live and live.get("head") == watch.get("head") and live.get("state") in ("running", "failed") \
                        and live.get("state") != terminal:
                    live["state"] = terminal
                    live["failed_check"] = failing
                    live["ended_at"] = now.isoformat()
                    current["ci"] = live
                    if terminal == "passed":
                        rerun = watch.get("state") == "failed"
                        commit(root, cfg, status=current, event_kind="ci_passed",
                               event_message=f"CI passed on PR #{live['pr']} ({len(checks)} checks{', after a rerun' if rerun else ''})",
                               pr=live["pr"], head=live["head"], checks=len(checks), after_rerun=rerun)
                    else:
                        commit(root, cfg, status=current, event_kind="ci_failed",
                               event_message=f"CI failed on PR #{live['pr']}: {failing}",
                               pr=live["pr"], head=live["head"], failed_check=failing)
                    watch = dict(live)
    started = watch.get("started_at")
    ended = watch.get("ended_at")
    elapsed = _iso_seconds(started, ended or now.isoformat()) if started else None
    # #193: the row's elapsed is awake time; the sleep inside it is reported
    ci_asleep = 0.0
    if elapsed is not None:
        try:
            began = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            finished = datetime.fromisoformat(str(ended).replace("Z", "+00:00")) if ended else now
            ci_asleep = asleep_seconds(began, finished, machine_sleep_intervals(now=now))
            elapsed = max(elapsed - ci_asleep, 0.0)
        except (TypeError, ValueError):
            ci_asleep = 0.0
    expected = watch.get("expected_seconds")
    progress = None
    if isinstance(expected, (int, float)) and expected > 0 and elapsed is not None:
        progress = min(elapsed / float(expected), 1.0)
    if watch.get("state") != "running":
        progress = 1.0
    if not isinstance(expected, (int, float)):
        note = note or CI_NO_HISTORY_NOTE
    elif watch.get("state") == "running" and elapsed is not None and elapsed > expected:
        note = note or "over the last run's time"
    cells = []
    for check in checks:
        cell_elapsed = _iso_seconds(check.get("started_at"), check.get("completed_at") or now.isoformat()) \
            if check.get("started_at") else None
        cells.append({"name": check["name"], "state": check["state"], "elapsed_seconds": cell_elapsed,
                      "queued": check.get("started_at") is None and check["state"] not in CI_CHECK_DONE,
                      "link": check.get("link"), "workflow": check.get("workflow")})
    return {
        "pr": watch.get("pr"), "head": watch.get("head"), "url": watch.get("url"),
        "state": watch.get("state"), "started_at": started, "ended_at": ended,
        "elapsed_seconds": elapsed, "asleep_seconds": round(ci_asleep, 3),
        "expected_seconds": expected, "expected_source": watch.get("expected_source"),
        "progress": progress, "failed_check": watch.get("failed_check"), "watched_by": watch.get("watched_by"),
        "fetched_at": fetched_at, "checks": cells, "note": note,
    }


def ci_gate_errors(status: dict) -> list[str]:
    """One line while the watched head has a failed check. Evaluated on the
    proposed status like every gate, so it refuses the 6 to 7 transition."""
    watch = status.get("ci") if isinstance(status, dict) else None
    if not isinstance(watch, dict) or watch.get("state") != "failed":
        return []
    check = watch.get("failed_check") or "a check"
    link = f" ({watch['url']})" if isinstance(watch.get("url"), str) else ""
    return [f"CI: {check} failed on PR #{watch.get('pr')}{link}; rerun or push, then ci-watch --pr {watch.get('pr')} again"]


# --------------------------------------------------------------------------
# #177: the Architect can say "do not build this". A decline is recorded
# like a proposal (bounded, hash-bound to the criteria) and closes the run
# as not_planned; the reason goes on the ledger and, by the host, on the
# issue. Only at Phase 1 or 2, before any design is approved.
# --------------------------------------------------------------------------

MAX_DECLINE_EVIDENCE = 8


def record_design_decline(root: Path, cfg: dict, *, by: object, reason: object, evidence: object = None,
                          alternative: object = None, except_session_id: str | None = None) -> dict:
    """Record the Architect's decline as a PENDING state the design
    reviewer resolves (#177, Lane E): approve closes the run as
    not_planned; request-changes sends it back. Only at Phase 2 with no
    approved design, no live managed session and an open run; bounded
    like a proposal; hash-bound to the criteria. Nothing closes here."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("design-decline: --by must be a non-empty string")
    if not isinstance(reason, str) or not 1 <= len(" ".join(reason.split())) <= 512:
        raise HandsoffError("design-decline: --reason must be 1 to 512 characters")
    items = list(evidence or [])
    if len(items) > MAX_DECLINE_EVIDENCE or any(not isinstance(e, str) or not 1 <= len(e.strip()) <= 512 for e in items):
        raise HandsoffError(f"design-decline: --evidence takes at most {MAX_DECLINE_EVIDENCE} items of 1 to 512 characters")
    if alternative is not None and (not isinstance(alternative, str) or not 1 <= len(alternative.strip()) <= 512):
        raise HandsoffError("design-decline: --alternative must be 1 to 512 characters when given")
    root = Path(root).resolve()
    with project_lock(root):
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        phase = int(status.get("phase_number", 0) or 0)
        if phase != 2:
            raise HandsoffError(f"design-decline: only at Phase 2, the design debate (the run is at Phase {phase})")
        if isinstance(status.get("design_approved"), dict) or (status.get("design_review") or {}).get("decision") == "approved":
            raise HandsoffError("design-decline: the design is already approved; a change of mind after approval is an amendment or a run-close, not a decline")
        if isinstance(status.get("run_closed"), dict):
            raise HandsoffError("design-decline: the run is already closed")
        pending = status.get("design_declined")
        if isinstance(pending, dict) and pending.get("decision") == "pending":
            raise HandsoffError("design-decline: a decline is already pending the reviewer's word")
        # the Architect session dispatching its own decline is still live
        # at this point, like the proposal path; it is not "another session"
        live = [sid for sid, session in (status.get("agent_sessions") or {}).items()
                if isinstance(session, dict) and session.get("state") in ("launching", "running")
                and sid != except_session_id]
        if live:
            raise HandsoffError(f"design-decline: a managed session is live ({', '.join(live)}); let it finish or run-close first")
        record = {
            "by": by.strip(), "at": datetime.now(timezone.utc).isoformat(),
            "reason": " ".join(reason.split()), "evidence": [e.strip() for e in items],
            "alternative": alternative.strip() if isinstance(alternative, str) else None,
            "design_hash": design_hash(acceptance.get("criteria", [])),
            "acceptance_hash": acceptance_hash(acceptance.get("criteria", [])),
            "session_id": except_session_id,
            "decision": "pending", "reviewed_by": None, "reviewed_at": None, "findings": [],
        }
        status["design_declined"] = record
        status["status"] = "in_progress"
        status["next_action"] = "Independent reviewer judges the decline: approve closes the run as not planned, changes send it back."
        commit(root, cfg, status=status, event_kind="design_declined",
               event_message=f"Architect declined the change, pending review: {record['reason']}",
               by=record["by"], reason=record["reason"], evidence=record["evidence"],
               alternative=record["alternative"], design_hash=record["design_hash"],
               acceptance_hash=record["acceptance_hash"])
    return record


def pending_design_decline(status: dict) -> dict | None:
    declined = status.get("design_declined") if isinstance(status, dict) else None
    return declined if isinstance(declined, dict) and declined.get("decision") == "pending" else None




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


def _terminate_owned_session_process(root: Path, session: dict, *, wait: float = 2.0) -> dict:
    """Stop only the process group proven by the current fresh session beacon."""
    beacon = read_live_beacon(root)
    session_id = session.get("session_id")
    if not beacon or beacon.get("session_id") != session_id or beacon.get("state") not in {"started", "running"}:
        raise HandsoffError(f"cannot prove process ownership for live session {session_id}")
    age = _seconds_since(beacon.get("beacon_at"), datetime.now(timezone.utc))
    pid = beacon.get("pid")
    if age is None or age < 0 or age > LIVE_BEACON_FRESH_SECONDS or not isinstance(pid, int) or pid <= 1:
        raise HandsoffError(f"cannot prove a fresh owned process for live session {session_id}")
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        # Restart-safe closure: a prior attempt may have stopped the exact
        # beacon-bound child and crashed before recording the terminal state.
        return {"session_id": session_id, "pid": pid, "signal": "already-stopped"}
    except (OSError, AttributeError) as exc:
        raise HandsoffError(f"owned process for session {session_id} cannot be verified") from exc
    if pgid != pid:
        raise HandsoffError(f"process {pid} is not the owned process-group leader for session {session_id}")
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(wait, 0.0)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"session_id": session_id, "pid": pid, "signal": "SIGTERM"}
        time.sleep(0.05)
    os.killpg(pgid, signal.SIGKILL)
    return {"session_id": session_id, "pid": pid, "signal": "SIGKILL"}


RUN_OUTCOMES = ("closed", "not_planned")


def close_run(root: Path, *, by: str, reason: str, expected_updated_at: str | None = None,
              cancel_active: bool = False, terminate_process=_terminate_owned_session_process,
              release_dashboard: bool = True, outcome: str = "closed",
              status_patch: dict | None = None, extra_events: list[dict] | None = None) -> dict:
    """Audit and resource-close one run without deleting any durable artifact.

    Live processes are cancelled only when a fresh beacon, the current
    session pointer, PID, and process-group leader all agree. The state
    transition is idempotent and bound to the dashboard state the Pilot saw.
    """
    root = Path(root).resolve()
    actor = validate_agent_actor(by)
    why = " ".join(str(reason or "").split())
    if not why:
        raise HandsoffError("clean run closure requires a reason")
    if len(why) > 512:
        raise HandsoffError("clean run closure reason must be at most 512 characters")
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        acceptance = load_unique_json(acceptance_path(root, cfg))
        existing = status.get("run_closed")
        if isinstance(existing, dict):
            return {"closed": True, "already_closed": True, "run_closed": existing,
                    "dashboard": {"released": False, "reason": "closure was already recorded"}}
        if expected_updated_at is not None and status.get("updated_at") != expected_updated_at:
            raise HandsoffError("run closure is stale; mission state changed")
        current = current_agent_sessions(status)
        live = [item for item in current.values()
                if isinstance(item, dict) and item.get("state") in AGENT_SESSION_LIVE_STATES]
        if live and not cancel_active:
            raise HandsoffError("active managed sessions require explicit cancel confirmation")
        stopped = []
        for session in live:
            stopped.append(terminate_process(root, session))

        proposed = deepcopy(status)
        now = datetime.now(timezone.utc).isoformat()
        for session in live:
            sid = session["session_id"]
            target = proposed["agent_sessions"][sid]
            if target.get("state") in AGENT_SESSION_LIVE_STATES:
                target["state"] = "cancelled"
                target["ended_at"] = now
                target["exit_code"] = 130
                proposed.get("current_agent_sessions", {}).pop(target.get("role"), None)
            for replacement in proposed.get("agent_replacements") or []:
                if replacement.get("to_session_id") == sid and replacement.get("state") in {"claimed", "running"}:
                    replacement["state"] = "failed"
                    replacement["ended_at"] = now
        proposed["background_wait"] = None
        proposed["human_pause"] = None
        proposed["recovery_lease"] = None
        if outcome not in RUN_OUTCOMES:
            raise HandsoffError(f"run outcome must be one of {', '.join(RUN_OUTCOMES)}")
        # #177: a decline's record and its closure are one write-ahead unit
        for key, value in (status_patch or {}).items():
            proposed[key] = value
        proposed["run_closed"] = {
            "by": actor, "at": now, "reason": why, "outcome": outcome,
            "cancelled_active": bool(live), "session_ids": [item["session_id"] for item in live],
        }
        proposed["next_action"] = ("Not planned: the Architect declined this change; the reason is on the ledger and the issue."
                                   if outcome == "not_planned" else
                                   "Run closed by the Pilot. Reopen it from Mission Control to continue.")
        proposed["updated_at"] = now
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="run_closed",
               event_message=(f"Run closed as not planned: {why}" if outcome == "not_planned"
                              else f"Pilot cleanly closed the run: {why}"), by=actor, outcome=outcome,
               extra_events=list(extra_events or []),
               cancelled_active=bool(live), session_ids=[item["session_id"] for item in live])
    dashboard = release_run_dashboard(root) if release_dashboard else {
        "released": False, "reason": "dashboard release delegated to caller",
    }
    return {"closed": True, "already_closed": False, "run_closed": proposed["run_closed"],
            "stopped": stopped, "dashboard": dashboard}


def reopen_run(root: Path, *, by: str, reason: str, expected_updated_at: str | None = None) -> dict:
    """Reopen a non-complete cleanly closed run; durable history is preserved."""
    root = Path(root).resolve()
    actor = validate_agent_actor(by)
    why = " ".join(str(reason or "").split())
    if not why:
        raise HandsoffError("reopen requires a reason")
    if len(why) > 512:
        raise HandsoffError("reopen reason must be at most 512 characters")
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        closed = status.get("run_closed")
        if not isinstance(closed, dict):
            raise HandsoffError("run is not closed")
        if status.get("status") == "complete":
            raise HandsoffError("a completed archived run cannot be reopened")
        if expected_updated_at is not None and status.get("updated_at") != expected_updated_at:
            raise HandsoffError("run reopen is stale; mission state changed")
        proposed = deepcopy(status)
        proposed.pop("run_closed", None)
        proposed["status"] = "in_progress"
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposed["next_action"] = f"Pilot reopened the run: {why}"
        commit(root, cfg, status=proposed, event_kind="run_reopened",
               event_message=f"Pilot reopened the run: {why}", by=actor)
    return {"reopened": True}


# --------------------------------------------------------------------------
# #46: role questions that reach Mission Control without Supervisor relay
# --------------------------------------------------------------------------
# A managed role prints one line `HANDSOFF_QUESTION: <text>`; the host
# (handsoff_agent) records it here. The record carries bounded operator-
# facing text, the role, the session id and timestamps, never prompt,
# output, or environment content. A question from the CURRENT live session
# of its role blocks the run (status `blocked`, next_action = the question)
# so the existing input-required alert fires; a question from any other
# session is recorded but never blocks. `question-answer` clears the block
# and the answer reaches the role on its next launch (build_role_input).

QUESTION_PREFIX = "HANDSOFF_QUESTION:"
QUESTION_ID_PATTERN = re.compile(r"^qn-[0-9a-f]{32}$")
MAX_PENDING_QUESTIONS = 16
MAX_QUESTION_TEXT = 1024
# #48: structured questions. A candidate that begins with '{' is parsed as a
# JSON form (text + options + optional recommended); anything else after the
# prefix is plain text exactly as in #46. A form that fails any rule is kept
# verbatim as plain text with `form_error` naming the rule, so nothing a
# role asked is ever lost.
MAX_QUESTION_OPTIONS = 6
MAX_QUESTION_OPTION_TEXT = 120
MAX_QUESTION_BATCH = 16
QUESTION_FORM_REQUIRED_KEYS = {"text", "options"}
QUESTION_FORM_OPTIONAL_KEYS = {"recommended"}
QUESTION_FORM_ERRORS = (
    "malformed_json", "not_object", "unknown_keys", "missing_keys", "text_bounds",
    "options_bounds", "duplicate_options", "recommended_not_offered",
)
QUESTION_LEGACY_FIELDS = {
    "question_id", "role", "session_id", "text", "truncated", "asked_at", "blocking",
    "answer", "answered_by", "answered_at", "delivered_at", "previous_next_action",
}
QUESTION_FORM_FIELDS = {"options", "recommended", "form_error"}
QUESTION_ANSWER_FIELDS = {"chosen_option", "other_text"}
QUESTION_FIELDS = QUESTION_LEGACY_FIELDS | QUESTION_FORM_FIELDS | QUESTION_ANSWER_FIELDS


class QuestionAnswerConflict(HandsoffError):
    """A batch answer that is well-formed but does not fit the board's
    current state (unknown or already-answered id, choice not offered)."""


def _question_text(value: object) -> tuple[str, bool]:
    """One line of bounded, control-free text; longer input is cut, not refused,
    so a role that asked a long question still gets its question on the board."""
    if not isinstance(value, str):
        raise HandsoffError("question text must be a string")
    text = " ".join(value.split())
    if not text:
        raise HandsoffError("question text must not be empty")
    truncated = len(text) > MAX_QUESTION_TEXT
    return text[:MAX_QUESTION_TEXT], truncated


def _question_form_failure(candidate: str, code: str) -> dict:
    text, truncated = _question_text(candidate)
    return {"text": text, "truncated": truncated, "options": [], "recommended": None, "form_error": code}


def parse_question_candidate(raw: object) -> dict:
    """#48: the text after `HANDSOFF_QUESTION:` becomes {text, truncated,
    options, recommended, form_error}. Only a candidate whose first
    non-blank character is '{' is tried as a form; everything else is
    plain text with no form_error, exactly as #46 stored it."""
    if not isinstance(raw, str):
        raise HandsoffError("question text must be a string")
    candidate = raw.strip()
    if not candidate.startswith("{"):
        text, truncated = _question_text(raw)
        return {"text": text, "truncated": truncated, "options": [], "recommended": None, "form_error": None}
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return _question_form_failure(candidate, "malformed_json")
    if not isinstance(parsed, dict):
        return _question_form_failure(candidate, "not_object")
    keys = set(parsed)
    if keys - QUESTION_FORM_REQUIRED_KEYS - QUESTION_FORM_OPTIONAL_KEYS:
        return _question_form_failure(candidate, "unknown_keys")
    if QUESTION_FORM_REQUIRED_KEYS - keys:
        return _question_form_failure(candidate, "missing_keys")
    text_value = parsed["text"]
    if not isinstance(text_value, str):
        return _question_form_failure(candidate, "text_bounds")
    text = " ".join(text_value.split())
    if not text or len(text) > MAX_QUESTION_TEXT:
        return _question_form_failure(candidate, "text_bounds")
    raw_options = parsed["options"]
    if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= MAX_QUESTION_OPTIONS:
        return _question_form_failure(candidate, "options_bounds")
    options: list[str] = []
    for option in raw_options:
        if not isinstance(option, str):
            return _question_form_failure(candidate, "options_bounds")
        clean = " ".join(option.split())
        if not clean or len(clean) > MAX_QUESTION_OPTION_TEXT:
            return _question_form_failure(candidate, "options_bounds")
        options.append(clean)
    if len(set(options)) != len(options):
        return _question_form_failure(candidate, "duplicate_options")
    recommended = None
    if "recommended" in parsed:
        value = parsed["recommended"]
        recommended = " ".join(value.split()) if isinstance(value, str) else None
        if recommended not in options:
            return _question_form_failure(candidate, "recommended_not_offered")
    return {"text": text, "truncated": False, "options": options, "recommended": recommended, "form_error": None}


PROJECT_LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".webp": "image/webp", ".svg": "image/svg+xml"}
PROJECT_LOGO_CANDIDATES = ("logo.png", "logo.svg", "docs/img/logo.png", "ui/logo.png", "assets/logo.png",
                           "static/logo.png", "site/logo.png")
MAX_PROJECT_LOGO_BYTES = 4 * 1024 * 1024


def project_logo(root: Path, cfg: dict) -> tuple[Path, str] | None:
    """The project's own artwork for Mission Control and Fleet: `[project]
    logo` in handsoff.toml, else the first conventional path that exists.
    The file must resolve inside the project, be a regular image of a known
    type and stay under 4 MiB; otherwise there is no logo (never an error:
    branding must not block a run)."""
    root = Path(root).resolve()
    declared = (cfg or {}).get("logo")
    candidates = [declared] if declared else list(PROJECT_LOGO_CANDIDATES)
    for relative in candidates:
        try:
            path = (root / relative).resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            continue
        content_type = PROJECT_LOGO_TYPES.get(path.suffix.lower())
        if content_type is None or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_PROJECT_LOGO_BYTES:
                continue
            # The engine's own brand mark is already in the topbar; a project
            # whose artwork IS that file (Handsoff itself) would show it twice.
            if _same_bytes(path, engine_resource_path("dashboard/logo.png")):
                continue
        except OSError:
            continue
        return path, content_type
    return None


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
    except OSError:
        return False


def design_approval_blockers(status: dict, acceptance: dict, cfg: dict) -> list[str]:
    """The deterministic reasons `design-approve` would refuse right now,
    in the gate's own words, so Mission Control can say them before the
    Pilot presses the button instead of after. Ledger audits are left to
    the gate itself (they need the files); these read the registry only."""
    blockers: list[str] = []
    criteria = acceptance.get("criteria", []) if isinstance(acceptance, dict) else []
    if any(c.get("requirement") == PLACEHOLDER_REQUIREMENT and c.get("tests") == PLACEHOLDER_TESTS for c in criteria):
        blockers.append("the acceptance registry still contains init's untouched placeholder criterion; "
                        "author a real criterion first")
    try:
        rows = derive_work_items(status, acceptance, cfg)["items"]
    except Exception:
        rows = []
    missing = [row["id"] for row in rows if row.get("required", True) and not row.get("criteria")]
    if missing:
        blockers.append(f"required work items have no criteria: {', '.join(missing)}; "
                        "tag a criterion [#N] or work-item-remove them")
    return blockers


def open_questions(status: dict, *, blocking_only: bool = False) -> list[dict]:
    items = status.get("pending_questions") if isinstance(status, dict) else None
    if not isinstance(items, list):
        return []
    return [q for q in items if isinstance(q, dict) and q.get("answer") is None
            and (q.get("blocking") or not blocking_only)]


def question_blocks_run(status: dict, session_id: str | None, role: str) -> bool:
    """Only the current live session of its role may hold the run."""
    if not isinstance(session_id, str):
        return False
    current = current_agent_sessions(status).get(role)
    return isinstance(current, dict) and current.get("session_id") == session_id \
        and current.get("state") in AGENT_SESSION_LIVE_STATES


def raise_question(root: Path, *, role: str, text: object, session_id: str | None = None,
                   by: str | None = None) -> dict:
    """Record a role's question; block the run when it comes from the role's
    current live session. Never touches phase, progress, or evidence."""
    if role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("question role must be architect, supervisor, implementer, or reviewer")
    if session_id is not None and not AGENT_SESSION_ID_PATTERN.fullmatch(str(session_id)):
        raise HandsoffError("question session id is invalid")
    parsed = parse_question_candidate(text)
    clean, truncated = parsed["text"], parsed["truncated"]
    root = root.resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        schema_errors = validate_status_schema(status)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        questions = [dict(q) for q in (status.get("pending_questions") or [])]
        if len(open_questions(status)) >= MAX_PENDING_QUESTIONS:
            raise HandsoffError(f"at most {MAX_PENDING_QUESTIONS} questions may be open; answer one first")
        existing = {q["question_id"] for q in questions}
        question_id = _new_bounded_id("qn", QUESTION_ID_PATTERN, existing)
        now = datetime.now(timezone.utc).isoformat()
        held_by_question = bool(open_questions(status, blocking_only=True))
        # A manual question (no session) blocks when the run is free to be
        # held or is already held by a question; a managed one blocks only
        # from its role's current live session.
        blocking = question_blocks_run(status, session_id, role) or (
            session_id is None and by is not None
            and (status.get("status") == "in_progress" or held_by_question))
        record = {
            "question_id": question_id, "role": role, "session_id": session_id, "text": clean,
            "truncated": truncated, "asked_at": now, "blocking": blocking,
            "options": list(parsed["options"]), "recommended": parsed["recommended"],
            "form_error": parsed["form_error"],
            "answer": None, "answered_by": None, "answered_at": None, "delivered_at": None,
            "chosen_option": None, "other_text": None, "previous_next_action": None,
        }
        proposed = deepcopy(status)
        if blocking and proposed.get("status") == "in_progress":
            record["previous_next_action"] = proposed.get("next_action")
            proposed["status"] = "blocked"
            proposed["next_action"] = f"{role.capitalize()} asks: {clean}"
            proposed["updated_at"] = now
        elif blocking and held_by_question:
            # A second question joins the hold; the banner keeps the first
            # question's wording and the panel lists both.
            pass
        elif blocking:
            # Already blocked or waiting on another gate: keep that gate's
            # wording; the question still shows in the questions panel.
            record["blocking"] = False
        questions = questions[-(MAX_PENDING_QUESTIONS * 4 - 1):] + [record]
        proposed["pending_questions"] = questions
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        commit(root, cfg, status=proposed, event_kind="question_raised",
               event_message=f"{role} asked a question", question_id=question_id, role=role,
               session_id=session_id, blocking=record["blocking"], truncated=truncated,
               text=clean, options=list(parsed["options"]), recommended=parsed["recommended"],
               form_error=parsed["form_error"], by=by)
        return deepcopy(record)


def answer_question(root: Path, *, question_id: str, by: str, text: object) -> dict:
    """Record the Pilot's answer; lift the block once no blocking question is open."""
    if not isinstance(question_id, str) or not QUESTION_ID_PATTERN.fullmatch(question_id):
        raise HandsoffError("question id is invalid")
    actor = validate_agent_actor(by)
    clean, truncated = _question_text(text)
    root = root.resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        schema_errors = validate_status_schema(status)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        proposed = deepcopy(status)
        questions = proposed.get("pending_questions") or []
        record = next((q for q in questions if q.get("question_id") == question_id), None)
        if record is None:
            raise HandsoffError(f"question {question_id} was not found")
        if record.get("answer") is not None:
            raise HandsoffError(f"question {question_id} is already answered")
        now = datetime.now(timezone.utc).isoformat()
        # #48: a single answer that names an offered option is that choice;
        # anything else is free text. Legacy records gain the two fields.
        chosen = clean if clean in (record.get("options") or []) else None
        record.update({"answer": clean, "answered_by": actor, "answered_at": now,
                       "chosen_option": chosen, "other_text": None if chosen is not None else clean})
        _upgrade_question_record(record)
        if record.get("truncated") is False and truncated:
            record["truncated"] = True
        still_blocking = open_questions(proposed, blocking_only=True)
        lifted = _lift_question_hold(proposed, [record], still_blocking)
        proposed["updated_at"] = now
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        commit(root, cfg, status=proposed, event_kind="question_answered",
               event_message=f"Pilot answered {record.get('role')} question {question_id}",
               question_id=question_id, role=record.get("role"), session_id=record.get("session_id"),
               by=actor, answer=clean, chosen_option=record["chosen_option"],
               other_text=record["other_text"], block_lifted=lifted,
               open_blocking_questions=len(still_blocking))
        return deepcopy(record)


def _upgrade_question_record(record: dict) -> None:
    """A #46 record touched by an answer path gains the #48 fields so the
    schema sees one complete shape; absent fields mean plain text."""
    for key, default in (("options", []), ("recommended", None), ("form_error", None),
                         ("chosen_option", None), ("other_text", None)):
        record.setdefault(key, default)


def _lift_question_hold(proposed: dict, answered: list[dict], still_blocking: list[dict]) -> bool:
    """Lift the hold exactly once: only when no blocking question remains
    and at least one of the answers just given was holding the run."""
    if still_blocking or proposed.get("status") != "blocked":
        return False
    holding = [q for q in answered if q.get("blocking")]
    if not holding:
        return False
    first = holding[0]
    proposed["status"] = "in_progress"
    proposed["next_action"] = (first.get("previous_next_action")
                               or f"Resume with the Pilot's answer to {first.get('question_id')}")
    return True


def question_answers_from_payload(payload: object) -> list:
    """The batch envelope shared by `question-answer --batch` and
    `POST /api/question-answers`: exactly {"answers": [...]}."""
    if not isinstance(payload, dict) or set(payload) != {"answers"}:
        raise HandsoffError('question batch must be an object with exactly the key "answers"')
    return payload["answers"]


def _batch_entry(index: int, entry: object) -> tuple[str, str, str]:
    label = f"answers[{index}]"
    if not isinstance(entry, dict):
        raise HandsoffError(f"{label} must be an object")
    keys = set(entry)
    if keys == {"question_id", "choice"}:
        mode = "choice"
    elif keys == {"question_id", "other"}:
        mode = "other"
    else:
        raise HandsoffError(f'{label} must have exactly the keys question_id and choice, or question_id and other')
    question_id = entry["question_id"]
    if not isinstance(question_id, str) or not QUESTION_ID_PATTERN.fullmatch(question_id):
        raise HandsoffError(f"{label}.question_id is invalid")
    value = entry[mode]
    if not isinstance(value, str):
        raise HandsoffError(f"{label}.{mode} must be a string")
    clean = " ".join(value.split())
    if not clean:
        raise HandsoffError(f"{label}.{mode} must not be empty")
    if len(clean) > MAX_QUESTION_TEXT:
        raise HandsoffError(f"{label}.{mode} must be at most {MAX_QUESTION_TEXT} characters")
    return question_id, mode, clean


def answer_questions_batch(root: Path, *, answers: object, by: str) -> list[dict]:
    """#48: record several Pilot answers in one lock-protected commit with
    one `question_answers_recorded` event. Any bad entry refuses the whole
    batch, naming its index, and writes nothing."""
    actor = validate_agent_actor(by)
    if not isinstance(answers, list) or not 1 <= len(answers) <= MAX_QUESTION_BATCH:
        raise HandsoffError(f"answers must be a list of 1 to {MAX_QUESTION_BATCH} entries")
    entries = [_batch_entry(index, entry) for index, entry in enumerate(answers)]
    seen: set[str] = set()
    for index, (question_id, _mode, _clean) in enumerate(entries):
        if question_id in seen:
            raise HandsoffError(f"answers[{index}].question_id {question_id} is listed twice")
        seen.add(question_id)
    root = root.resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        schema_errors = validate_status_schema(status)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        proposed = deepcopy(status)
        by_id = {q.get("question_id"): q for q in (proposed.get("pending_questions") or [])
                 if isinstance(q, dict)}
        now = datetime.now(timezone.utc).isoformat()
        answered: list[dict] = []
        listed: list[dict] = []
        for index, (question_id, mode, clean) in enumerate(entries):
            label = f"answers[{index}]"
            record = by_id.get(question_id)
            if record is None:
                raise QuestionAnswerConflict(f"{label}: question {question_id} was not found")
            if record.get("answer") is not None:
                raise QuestionAnswerConflict(f"{label}: question {question_id} is already answered")
            options = record.get("options") or []
            if mode == "choice":
                if not options:
                    raise QuestionAnswerConflict(f"{label}: question {question_id} is plain text and accepts only other")
                if clean not in options:
                    raise QuestionAnswerConflict(f"{label}: choice {clean!r} is not offered by question {question_id}")
            _upgrade_question_record(record)
            record.update({"answer": clean, "answered_by": actor, "answered_at": now,
                           "chosen_option": clean if mode == "choice" else None,
                           "other_text": clean if mode == "other" else None})
            answered.append(record)
            listed.append({"question_id": question_id, mode: clean, "answered_by": actor})
        still_blocking = open_questions(proposed, blocking_only=True)
        lifted = _lift_question_hold(proposed, answered, still_blocking)
        proposed["updated_at"] = now
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        commit(root, cfg, status=proposed, event_kind="question_answers_recorded",
               event_message=f"Pilot answered {len(answered)} question(s)",
               answers=listed, by=actor, block_lifted=lifted,
               open_blocking_questions=len(still_blocking))
        return [deepcopy(q) for q in answered]


def questions_prompt_section(root: Path, role: str) -> str:
    """Answered questions for `role` not yet delivered, rendered for the next
    launch; marks them delivered in the same commit so an answer is handed
    over exactly once and the hand-over is audited."""
    root = root.resolve()
    with project_lock(root):
        cfg = load_config(root)
        try:
            status = load_unique_json(status_path(root, cfg))
        except (HandsoffError, OSError):
            return ""
        due = [q for q in (status.get("pending_questions") or [])
               if isinstance(q, dict) and q.get("role") == role and q.get("answer") is not None
               and q.get("delivered_at") is None]
        due.sort(key=lambda q: str(q.get("asked_at") or ""))
        if not due:
            return ""
        proposed = deepcopy(status)
        now = datetime.now(timezone.utc).isoformat()
        for q in proposed["pending_questions"]:
            if q.get("question_id") in {d["question_id"] for d in due}:
                q["delivered_at"] = now
        commit(root, cfg, status=proposed, event_kind="question_answers_delivered",
               event_message=f"Delivered {len(due)} Pilot answer(s) to the {role}",
               role=role, question_ids=[d["question_id"] for d in due])
    lines = ["# Pilot answers to your earlier questions", ""]
    for q in due:
        lines.append(f"- Q ({q['question_id']}): {q['text']}")
        if q.get("options"):
            lines.append(f"  Options offered: {', '.join(q['options'])}")
        chosen = " (chosen from the offered options)" if q.get("chosen_option") is not None else ""
        lines.append(f"  A ({q['answered_by']}): {q['answer']}{chosen}")
    return "\n".join(lines)


def questions_view(status: dict) -> dict:
    items = [q for q in (status.get("pending_questions") or []) if isinstance(q, dict)]
    open_items = [q for q in items if q.get("answer") is None]
    by_role: dict[str, dict] = {}
    for q in open_items:
        role = str(q.get("role") or "role")
        entry = by_role.setdefault(role, {"role": role, "count": 0, "blocking": 0, "question_ids": []})
        entry["count"] += 1
        entry["blocking"] += 1 if q.get("blocking") else 0
        entry["question_ids"].append(q.get("question_id"))
    return {
        "open": open_items,
        "blocking": [q for q in open_items if q.get("blocking")],
        "answered": [q for q in items if q.get("answer") is not None][-8:],
        "by_role": list(by_role.values()),
        "total": len(items),
    }


def question_cards(status: dict) -> list[dict]:
    """#48: one banner card per role with an open blocking question."""
    cards: dict[str, int] = {}
    for q in open_questions(status, blocking_only=True):
        role = str(q.get("role") or "role")
        cards[role] = cards.get(role, 0) + 1
    return [{"role": role, "count": count} for role, count in cards.items()]


def question_status_errors(status: dict) -> list[str]:
    """Schema half of the trust boundary: a hand-edited record refuses cleanly."""
    errors: list[str] = []
    if "pending_questions" not in status or status["pending_questions"] is None:
        return errors
    items = status["pending_questions"]
    if not isinstance(items, list):
        return ["status: 'pending_questions' must be a list"]
    if len(items) > MAX_PENDING_QUESTIONS * 4:
        errors.append(f"status: 'pending_questions' must contain at most {MAX_PENDING_QUESTIONS * 4} entries")
    seen = set()
    for index, q in enumerate(items):
        label = f"status: pending_questions[{index}]"
        if not isinstance(q, dict):
            errors.append(f"{label} must be an object")
            continue
        # #48 records carry the form and answer fields; a #46 record written
        # before them is read as plain text (absent means empty list / null).
        # Any other key set is a hand edit.
        if set(q) not in (QUESTION_FIELDS, QUESTION_LEGACY_FIELDS):
            errors.append(f"{label} must have exactly the keys {sorted(QUESTION_FIELDS)}")
            continue
        qid = q.get("question_id")
        if not isinstance(qid, str) or not QUESTION_ID_PATTERN.fullmatch(qid) or qid in seen:
            errors.append(f"{label}.question_id is invalid or duplicated")
        seen.add(qid)
        if q.get("role") not in SELECTABLE_AGENT_ROLES:
            errors.append(f"{label}.role is invalid")
        sid = q.get("session_id")
        if sid is not None and (not isinstance(sid, str) or not AGENT_SESSION_ID_PATTERN.fullmatch(sid)):
            errors.append(f"{label}.session_id is invalid")
        if not isinstance(q.get("text"), str) or not q["text"].strip() or len(q["text"]) > MAX_QUESTION_TEXT:
            errors.append(f"{label}.text must be a non-empty string of at most {MAX_QUESTION_TEXT} characters")
        for flag in ("truncated", "blocking"):
            if not isinstance(q.get(flag), bool):
                errors.append(f"{label}.{flag} must be boolean")
        for key in ("asked_at", "answered_at", "delivered_at"):
            value = q.get(key)
            if value is None and key != "asked_at":
                continue
            try:
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{label}.{key} must be a timezone-aware timestamp")
        answered = q.get("answer") is not None
        if answered and (not isinstance(q.get("answer"), str) or not q["answer"].strip()
                         or not isinstance(q.get("answered_by"), str) or q.get("answered_at") is None):
            errors.append(f"{label} answer must carry text, answered_by and answered_at together")
        if not answered and (q.get("answered_by") is not None or q.get("answered_at") is not None
                             or q.get("delivered_at") is not None):
            errors.append(f"{label} unanswered question cannot carry answer metadata")
        previous = q.get("previous_next_action")
        if previous is not None and not isinstance(previous, str):
            errors.append(f"{label}.previous_next_action must be a string or null")
        errors.extend(_question_form_errors(label, q, answered))
    return errors


def _question_form_errors(label: str, q: dict, answered: bool) -> list[str]:
    """#48 half of the schema check: options, recommended, form_error and
    the chosen_option / other_text pair must agree with each other."""
    errors: list[str] = []
    options = q.get("options", [])
    options_ok = isinstance(options, list) and len(options) <= MAX_QUESTION_OPTIONS and all(
        isinstance(o, str) and 1 <= len(o) <= MAX_QUESTION_OPTION_TEXT and " ".join(o.split()) == o
        for o in options)
    if not options_ok:
        errors.append(f"{label}.options must be a list of at most {MAX_QUESTION_OPTIONS} normalized strings "
                      f"of 1 to {MAX_QUESTION_OPTION_TEXT} characters")
        options = []
    elif len(set(options)) != len(options):
        errors.append(f"{label}.options must be unique")
    recommended = q.get("recommended")
    if recommended is not None and (not isinstance(recommended, str) or recommended not in options):
        errors.append(f"{label}.recommended must be null or one of the options")
    form_error = q.get("form_error")
    if form_error is not None:
        if form_error not in QUESTION_FORM_ERRORS:
            errors.append(f"{label}.form_error must be null or one of {list(QUESTION_FORM_ERRORS)}")
        if options or recommended is not None:
            errors.append(f"{label} with a form_error must carry no options or recommended")
    chosen = q.get("chosen_option")
    other = q.get("other_text")
    if chosen is not None and (not isinstance(chosen, str) or chosen not in options or chosen != q.get("answer")):
        errors.append(f"{label}.chosen_option must be null or the offered option that is the answer")
    if other is not None and (not isinstance(other, str) or other != q.get("answer")):
        errors.append(f"{label}.other_text must be null or equal to the answer")
    if chosen is not None and other is not None:
        errors.append(f"{label} cannot carry both chosen_option and other_text")
    if not answered and (chosen is not None or other is not None):
        errors.append(f"{label} unanswered question cannot carry chosen_option or other_text")
    return errors


# --------------------------------------------------------------------------
# #171: the final report, from the ledger, posted once per ticket
# --------------------------------------------------------------------------

REPORT_MARKER = "<!-- handsoff-report {head} -->"
REPORT_REQUIREMENT_CHARS = 120
_CREDENTIAL_SHAPES = (
    re.compile(r"\b(?:sk|ghp|github_pat|gho|ghs|ghu|xox[abpr])[_-][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _home_to_tilde(text: str) -> str:
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def report_commits(root: Path, events: list[dict], *, runner=subprocess.run) -> list[dict]:
    """Commits made during the run, oldest first: hash and subject only,
    from git itself, between the first event's time and now. 'not recorded'
    when git cannot answer."""
    started = events[0].get("at") if events else None
    if not started:
        return []
    try:
        proc = runner(["git", "-C", str(root), "log", f"--since={started}", "--reverse", "--format=%h %s"],
                      capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, subject = line.partition(" ")
        out.append({"hash": digest, "subject": subject[:120]})
    return out[:64]


def report_push_target(root: Path, *, runner=subprocess.run) -> str:
    try:
        proc = runner(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "@{upstream}"],
                      capture_output=True, text=True, timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "not recorded"


def render_final_report(root: Path, cfg: dict, status: dict, acceptance: dict, events: list[dict],
                        verifications: list[dict], validate_lines: list[str], *, runner=subprocess.run) -> str:
    """The Phase 8 report in fixed maintainer wording, built from an
    allowlist of ledger fields only. Never pilot notes, chat text or agent
    output. Home paths become '~' and the text passes the output redactor;
    whether anything credential-shaped survived is post_final_report's call."""
    by_run = {r.get("run_id"): r for r in verifications if isinstance(r, dict)}
    lines = [f"## Handsoff report: {str(status.get('feature') or 'run')[:200]}", ""]
    lines.append(f"Phase {status.get('phase_number')} ({status.get('phase')}), status {status.get('status')}, "
                 f"progress {status.get('progress')}.")
    commits = report_commits(root, events, runner=runner)
    lines += ["", "### Commits"]
    if commits:
        lines += [f"- `{c['hash']}` {c['subject']}" for c in commits]
        lines.append(f"- pushed to: {report_push_target(root, runner=runner)}")
    else:
        lines.append("- not recorded")
    lines += ["", "### Acceptance"]
    for criterion in acceptance.get("criteria", []):
        text = str(criterion.get("requirement") or "")
        short = text[:REPORT_REQUIREMENT_CHARS] + ("..." if len(text) > REPORT_REQUIREMENT_CHARS else "")
        evidence = criterion.get("evidence") or []
        last = evidence[-1] if evidence else None
        record = by_run.get(last) or {}
        detail = f"evidence {last}" if last else "no evidence"
        if record.get("kind"):
            detail += f" ({record['kind']}, {'ok' if record.get('ok') else 'not ok'})"
        lines.append(f"- {criterion.get('id')} [{criterion.get('verification')}] {criterion.get('state')}: {short} ({detail})")
    review = status.get("review") if isinstance(status.get("review"), dict) else None
    lines += ["", "### Review"]
    if review:
        lines.append(f"- by {review.get('by')} at {review.get('at')}")
        lines.append(f"- acceptance hash {review.get('acceptance_hash')}")
        if review.get("rules_hash"):
            lines.append(f"- rules set {review['rules_hash']}")
    else:
        lines.append("- not recorded")
    approval = status.get("deployment_approved") if isinstance(status.get("deployment_approved"), dict) else None
    lines += ["", "### Deployment"]
    lines.append(f"- approved by {approval.get('by')} at {approval.get('at')}" if approval else "- no deployment approval recorded")
    live = by_run.get(status.get("live_verification_id")) or {}
    lines.append(f"- live verification {'ok' if live.get('ok') else 'not ok'} at {live.get('at')}" if live else "- no live verification recorded")
    totals = usage_totals(status)
    lines += ["", "### Usage"]
    if totals["sessions_reported"]:
        lines.append(f"- {totals['tokens_total']:,} tokens over {totals['sessions_reported']} managed session(s)"
                     + (f"; {totals['sessions_not_reported']} not reported" if totals["sessions_not_reported"] else ""))
        for role, value in sorted(totals["by_role"].items()):
            lines.append(f"- {role}: {value:,}")
    else:
        lines.append("- not reported")
    items = derive_work_items(status, acceptance, cfg).get("items", []) if isinstance(acceptance, dict) else []
    lines += ["", "### Work items"]
    lines += [f"- {item.get('id')}: {item.get('status')}" for item in items] or ["- none"]
    lines += ["", "### Validate", "```"] + [str(v) for v in (validate_lines or ["not run"])] + ["```"]
    text = "\n".join(lines) + "\n"
    return redact_output_text(_home_to_tilde(text))


def report_credential_lines(text: str) -> list[str]:
    """Lines that still look like a credential after redaction."""
    return [line for line in text.splitlines() if any(p.search(line) for p in _CREDENTIAL_SHAPES)]


def _gh(args: list[str], *, runner=subprocess.run, cwd: Path | None = None):
    return runner(["gh", *args], capture_output=True, text=True, timeout=60, cwd=str(cwd) if cwd else None)


def post_final_report(root: Path, cfg: dict, status: dict, acceptance: dict, events: list[dict],
                      verifications: list[dict], validate_lines: list[str], *, by: str,
                      runner=subprocess.run) -> dict:
    """#171: one comment per issue work item, marked with the ledger head so
    a second post finds it and does nothing; the item is closed and its box
    ticked in a parent epic. Nothing is posted when a credential shape
    survives redaction or when gh cannot authenticate."""
    head = _last_hash(event_log_path(root, cfg))
    text = render_final_report(root, cfg, status, acceptance, events, verifications, validate_lines, runner=runner)
    marker = REPORT_MARKER.format(head=head)
    leaked = report_credential_lines(text)
    if leaked:
        return {"posted": [], "skipped": [], "reason": "redaction",
                "detail": f"{len(leaked)} line(s) still credential-shaped; nothing posted"}
    auth = _gh(["auth", "status"], runner=runner, cwd=root)
    if auth.returncode != 0:
        return {"posted": [], "skipped": [], "reason": "gh_auth", "detail": "gh is not authenticated; nothing posted"}
    items = [item for item in derive_work_items(status, acceptance, cfg).get("items", [])
             if item.get("kind") == "issue" and isinstance(item.get("number"), int)]
    posted, skipped = [], []
    body = marker + "\n" + text
    for item in items:
        number = item["number"]
        view = _gh(["issue", "view", str(number), "--json", "body,url,state,comments"], runner=runner, cwd=root)
        try:
            issue = json.loads(view.stdout) if view.returncode == 0 else {}
        except ValueError:
            issue = {}
        existing = [c.get("body") if isinstance(c, dict) else c for c in (issue.get("comments") or [])]
        # any earlier report on this ticket counts: the head moves with every
        # event, the marker's prefix does not. The comment is the only
        # part that is never repeated; close and tick are retried below
        # until each has actually happened (review F1, tranche 2).
        already = any(isinstance(c, str) and c.lstrip().startswith(REPORT_MARKER.split("{head}")[0]) for c in existing)
        url = None
        if already:
            skipped.append({"number": number, "reason": "already posted"})
        else:
            comment = _gh(["issue", "comment", str(number), "--body", body], runner=runner, cwd=root)
            if comment.returncode != 0:
                skipped.append({"number": number, "reason": "comment failed"})
                continue
            url = comment.stdout.strip().splitlines()[-1] if comment.stdout.strip() else None
        closed = str(issue.get("state") or "").upper() == "CLOSED"
        did_close = did_tick = False
        if not closed:
            closed = _gh(["issue", "close", str(number), "-c", f"Closed by the Handsoff run report ({head[:12]})."],
                         runner=runner, cwd=root).returncode == 0
            did_close = closed
        parent_match = re.search(r"(?im)^\s*parent:\s*#([1-9][0-9]{0,8})\b", str(issue.get("body") or ""))
        parent = int(parent_match.group(1)) if parent_match else None
        ticked = False
        if parent:
            parent_view = _gh(["issue", "view", str(parent), "--json", "body"], runner=runner, cwd=root)
            try:
                parent_body = json.loads(parent_view.stdout).get("body") or "" if parent_view.returncode == 0 else ""
            except ValueError:
                parent_body = ""
            unticked = re.compile(rf"^(\s*- \[) (\] #{number}\b)", re.MULTILINE)
            if unticked.search(parent_body):
                new_body = unticked.sub(r"\1x\2", parent_body, count=1)
                ticked = _gh(["issue", "edit", str(parent), "--body", new_body], runner=runner, cwd=root).returncode == 0
                did_tick = ticked
            elif re.search(rf"^\s*- \[x\] #{number}\b", parent_body, re.MULTILINE | re.IGNORECASE):
                ticked = True
        if already and not did_close and not did_tick and closed and (ticked or not parent):
            continue  # everything was done on an earlier post; nothing touched now
        posted.append({"number": number, "url": url, "closed": closed, "parent": parent, "ticked": ticked,
                       "comment": "skipped" if already else "posted"})
    return {"posted": posted, "skipped": skipped, "reason": None, "detail": None, "head": head}


def record_report_outcome(root: Path, cfg: dict, outcome: dict, *, by: str) -> None:
    if outcome.get("reason"):
        append_event(root, cfg, "report_not_posted", f"Final report not posted: {outcome['detail']}",
                     by=by, reason=outcome["reason"])
    else:
        append_event(root, cfg, "report_posted", "Final report posted to the work items",
                     by=by, posted=outcome["posted"], skipped=outcome["skipped"], head=outcome.get("head"))
