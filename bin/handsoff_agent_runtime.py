#!/usr/bin/env python3
"""Handsoff agent runtime: managed sessions, their ledger and their budgets.

#284 stage 5. The largest extraction so far: the session lifecycle, the
actor and model validators, the telemetry integrity checks and the token
budget planner.

Layer: core -> routing -> config -> ledger -> here. Every import is module
level; nothing is deferred.

Several constants this needed had already left the monolith in earlier
stages: the adapter and role vocabularies went to `handsoff_config` and the
verification requirements to `handsoff_ledger`. They read as unresolved
when this boundary was first measured, which is what a symbol looks like
after the layer beneath it has claimed it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import (
    HandsoffError,
    _canonical,
    acceptance_hash,
    acceptance_path,
    design_hash,
    load_unique_json,
    project_lock,
    status_path,
)
from handsoff_routing import (
    adaptive_fleet_usage,
    adaptive_usage,
    route_adaptive_profile,
    validate_session_adaptive_routing,
    validate_session_routing_contract,
)
from handsoff_config import (
    DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    DEFAULT_MODEL_POLICY,
    HOST_AGENT_ADAPTER,
    LAUNCHABLE_AGENT_ADAPTERS,
    SELECTABLE_AGENT_ADAPTERS,
    SELECTABLE_AGENT_ROLES,
    adapter_serves_role,
    load_config,
    model_policy_allows,
    validate_agent_model,
    validate_model_policy,
)
from handsoff_ledger import (
    config_hash,
    feature_enabled,
    _digest_excluded,
    _file_sha256,
    commit,
    event_head_path,
    event_log_path,
    open_amendment,
    verification_log_path,
)
from handsoff_resources import RUNTIME_MANIFEST_FILE, engine_root


from handsoff_schema import (  # noqa: E402,F401
    AGENT_REPLACEMENT_STATES,
    AGENT_REPLACEMENT_TRIGGERS,
    AGENT_SESSION_FIELDS,
    AGENT_SESSION_ID_PATTERN,
    AGENT_SESSION_LIVE_STATES,
    AGENT_SESSION_OPTIONAL_FIELDS,
    AGENT_SESSION_RESOLUTION_SOURCES,
    AGENT_SESSION_STATES,
    AGENT_SESSION_TERMINAL_STATES,
    AMENDMENT_CLASSIFICATIONS,
    AMENDMENT_FIELDS,
    AMENDMENT_ID_PATTERN,
    AMENDMENT_PILOT_APPROVAL_FIELDS,
    AMENDMENT_REVIEW_DECISIONS,
    AMENDMENT_REVIEW_FIELDS,
    AMENDMENT_REVIEW_OPTIONAL_FIELDS,
    AMENDMENT_STATES,
    BUDGET_FAILURE_CAUSES,
    CRITERIA_TRANSACTION_OPS,
    DESIGN_REVIEWER_ESCALATION_FIELDS,
    DESIGN_REVIEWER_PROFILE_FIELDS,
    DESIGN_REVIEWER_SELECTION_REASONS,
    DESIGN_REVIEWER_TIERS,
    DESIGN_REVIEW_DISPOSITIONS,
    DESIGN_REVIEW_FINDING_ID_PATTERN,
    DESIGN_REVIEW_HISTORY_FIELDS,
    DESIGN_REVIEW_PACKET_ID_PATTERN,
    ESCALATION_KINDS,
    FAILURE_CATEGORIES,
    MAX_AGENT_ACTOR_LENGTH,
    MAX_AGENT_REPLACEMENTS,
    MAX_AGENT_SESSIONS,
    MAX_AGENT_SESSION_ID_LENGTH,
    MAX_AMENDMENT_HISTORY,
    MAX_AMENDMENT_LIST,
    MAX_AMENDMENT_REVIEW_FINDINGS,
    MAX_DESIGN_REVIEW_FINDINGS,
    MAX_DESIGN_REVIEW_FINDING_LENGTH,
    MAX_DESIGN_REVIEW_HISTORY,
    MAX_PENDING_QUESTIONS,
    MAX_PROGRESS_NOTE,
    MAX_PROGRESS_RECORDS,
    MAX_QUALITY_FINDINGS,
    MAX_QUESTION_OPTIONS,
    MAX_QUESTION_OPTION_TEXT,
    MAX_QUESTION_TEXT,
    MAX_RECOVERY_ATTEMPTS,
    MAX_REGRESSION_REQUESTS,
    MAX_REVIEW_ATTEMPTS,
    MAX_REVIEW_CAP_OVERRIDES,
    MAX_REVIEW_SESSION_IDS,
    OPERATION_IDENTIFIER_PATTERN,
    PHASES,
    PROFILE_SOURCES,
    PROGRESS_CRITERION_PATTERN,
    PROGRESS_STATES,
    QUALITY_FINDING_CODES,
    QUALITY_FINDING_ID_PATTERN,
    QUESTION_ANSWER_FIELDS,
    QUESTION_FIELDS,
    QUESTION_FORM_ERRORS,
    QUESTION_FORM_FIELDS,
    QUESTION_ID_PATTERN,
    QUESTION_LEGACY_FIELDS,
    RECOVERY_ID_PATTERN,
    RECOVERY_LEASE_ID_PATTERN,
    RECOVERY_STATES,
    RECOVERY_TRIGGERS,
    REGRESSION_REQUEST_ID_PATTERN,
    REGRESSION_STATES,
    RELEASE_VERSION_PATTERN,
    REPLACEMENT_ID_PATTERN,
    REQUIRED_COVERAGE_FIELDS,
    REQUIRED_STATUS_FIELDS,
    REVIEW_ATTEMPT_DISPOSITIONS,
    REVIEW_ATTEMPT_ID_PATTERN,
    REVIEW_ATTEMPT_TRIGGERS,
    REVIEW_FINDING_CODES,
    REVIEW_OVERRIDE_ID_PATTERN,
    ROLE_BUDGET_FLOORS,
    RUN_LANES,
    STATUS_VALUES,
    USAGE_SOURCES,
    WORK_ITEM_ID_PATTERN,
    WORK_ITEM_KINDS,
    WORK_ITEM_LANES,
    _FAILURE_REASON_LABELS,
    _HEX64,
    _amendment_record_errors,
    _design_review_findings_errors,
    _design_review_history_errors,
    _design_review_packet_errors,
    _design_reviewer_escalation_errors,
    _design_reviewer_profile_errors,
    _is_number,
    _question_form_errors,
    _validate_failure_classification,
    amendment_status_errors,
    classify_release_version,
    question_status_errors,
    validate_acceptance_schema,
    validate_agent_actor,
    validate_owned_paths,
    validate_progress_line,
    validate_release_plan,
    validate_reviewer_isolation_contract,
    validate_session_budget_decision,
    validate_status_schema,
    validate_usage,
)


DESIGN_REVIEW_AUTHORIZATION_COMMAND = "handsoff_supervisor.py design-review-authorize --by <pilot>"


# #58: portable, bounded managed-agent output. This generated side file is
# deliberately separate from every audit ledger and is never consulted by a
# gate. It contains only host-redacted child output and session metadata.
AGENT_OUTPUT_FILE = ".handsoff-agent-output.json"


AGENT_OUTPUT_LOCK_FILE = ".handsoff-agent-output.lock"


AGENT_OUTPUT_FLUSH_INTERVAL_SECONDS = 0.25


AGENT_OUTPUT_FLUSH_MAX_ENTRIES = 20


AGENT_OUTPUT_FLUSH_MAX_BYTES = 32768


DEFAULT_AGENT_PREFERENCE = SELECTABLE_AGENT_ADAPTERS


VERSION_PIN_FILE = ".handsoff-version"


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


#: "baseline" (#165): a criterion's own commands run BEFORE the feature,
#: recorded as ok=True when every one of them failed (a valid red) and
#: ok=False when any passed (baseline_invalid). It never satisfies the
#: checks requirement; it is what the failing-first gate asks for behind
#: the later green run.
# #349: "mutation" is evidence that the criterion's own test FAILS when the
# behaviour it names is removed. Every other kind proves something happened;
# this one proves the test would notice if it stopped happening.
VERIFICATION_KINDS = {"checks", "manual", "browser", "live", "baseline", "mutation"}


def agent_profiles(cfg: dict) -> dict:
    return {
        role: {"adapter": cfg["agents"][role],
               "model": None if cfg["agents"][role] == HOST_AGENT_ADAPTER else cfg["models"][role]}
        for role in SELECTABLE_AGENT_ROLES
    }


def default_agent_adapter(*, which=None) -> str | None:
    """Return the first installed runnable adapter in documented order."""
    lookup = which or shutil.which
    return next((adapter for adapter in DEFAULT_AGENT_PREFERENCE if lookup(adapter)), None)


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
    """Bound status growth while retaining every role's current snapshot
    (#420: its latest ended session too, now that ending clears the pointer)."""
    protected = {value for value in current.values() if isinstance(value, str)}
    protected |= {value for value in role_session_ids(
        {"agent_sessions": sessions, "current_agent_sessions": current}).values() if value}
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
    implementer = sessions.get(role_session_ids(status).get("implementer"))
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


def live_implementer_sessions(status: dict) -> list[dict]:
    """#359: every live implementer session, by session id. The
    current_agent_sessions pointer names only the latest launch, so a reader
    that must see concurrent implementers iterates agent_sessions instead."""
    return [session for session in (status.get("agent_sessions") or {}).values()
            if isinstance(session, dict) and session.get("role") == "implementer"
            and session.get("state") in AGENT_SESSION_LIVE_STATES]


def live_agent_sessions(status: dict) -> list[dict]:
    """#359: every live session a lifecycle reader must stop, close or show:
    each role's current live session plus every live implementer, once each,
    by session id."""
    pointers = status.get("current_agent_sessions") if isinstance(status, dict) else None
    sessions = status.get("agent_sessions") if isinstance(status, dict) else None
    found = {}
    for session_id in (pointers or {}).values():
        session = (sessions or {}).get(session_id) if isinstance(session_id, str) else None
        if isinstance(session, dict) and session.get("state") in AGENT_SESSION_LIVE_STATES:
            found[session_id] = deepcopy(session)
    for session in live_implementer_sessions(status):
        found.setdefault(session.get("session_id"), deepcopy(session))
    return list(found.values())


def implementer_admission(root: Path, live: list[dict], owned_paths: list[str] | None,
                          *, preferred_id: str | None = None) -> str | None:
    """#359: admit an implementer beside the live ones, or refuse it before
    anything is written. Returns the launch commit of its worktree when it
    declared ownership, else None. Every implementer that declares ownership
    runs in its own worktree, the first one included, so none writes the
    project tree."""
    # The single-live-implementer rule (one live session per role) existed
    # to stop two implementers racing on the same files. Ownership keeps
    # exactly that guarantee: a second implementer is admitted only when
    # every live one declared its paths and none overlaps this launch's
    # (equal, or one a directory prefix of the other).
    if live and owned_paths is None:
        first = next((item for item in live if item.get("session_id") == preferred_id), live[0])
        raise HandsoffError(
            f"role implementer already has live agent session {first['session_id']} ({first.get('state')})")
    for item in live:
        theirs = item.get("owned_paths")
        if not theirs:
            raise HandsoffError(
                f"role implementer already has live agent session {item['session_id']} "
                f"({item.get('state')}) with no declared ownership")
        overlapping = sorted({path for mine in owned_paths for other in theirs
                              if owned_paths_overlap(mine, other) for path in (mine, other)})
        if overlapping:
            raise HandsoffError(
                f"implementer launch refused: owned paths {', '.join(overlapping)} overlap "
                f"live implementer session {item['session_id']}")
    if owned_paths is None:
        return None
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
        raise HandsoffError("implementer launch with --owns refused: the project has no git HEAD to branch a worktree from")
    return head.stdout.strip()


def owned_paths_overlap(first: str, second: str) -> bool:
    """Equal, or one a directory prefix of the other."""
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def normalize_owned_paths(root: Path, paths) -> list[str]:
    """#359: each --owns PATH as a normalized project-relative POSIX path
    inside the root. Refuses the root itself and any path that escapes it."""
    root = Path(root).resolve()
    normalized = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise HandsoffError("--owns needs a non-empty path")
        candidate = Path(raw) if os.path.isabs(raw) else root / raw
        # realpath, not normpath: the root is resolved, so an absolute path
        # through a symlink (/var -> /private/var on macOS) must be too.
        resolved = Path(os.path.realpath(str(candidate)))
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            raise HandsoffError(f"--owns {raw} is outside the project root") from None
        text = relative.as_posix()
        if text in {"", "."}:
            raise HandsoffError("--owns cannot claim the whole project root")
        if text not in normalized:
            normalized.append(text)
    return validate_owned_paths(normalized)


def implementer_workspace_dir(root: Path) -> Path:
    """#359: the run scratch directory holding concurrent implementers'
    worktrees, one per session id. Always outside the project root."""
    root = Path(root).resolve()
    scratch = Path(tempfile.gettempdir()).resolve() / "handsoff-workspaces" \
        / hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    if scratch == root or root in scratch.parents:
        raise HandsoffError("implementer workspace directory would be inside the project root")
    return scratch


def _git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)


def _git_paths(cwd: Path | str, *args: str) -> list[str]:
    result = _git(cwd, args[0], "-z", *args[1:])  # -z before any `--` pathspec
    if result.returncode != 0:
        raise HandsoffError(f"git {args[0]} failed: {result.stderr.strip()[:160]}")
    return [item for item in result.stdout.split("\0") if item]


def _changed_since(cwd: Path | str, commit_sha: str, pathspecs: list[str] | None = None) -> list[str]:
    """Tracked paths whose working-tree content differs from commit_sha, plus
    untracked (not ignored) files, optionally limited to pathspecs."""
    tail = ["--", *pathspecs] if pathspecs else []
    changed = _git_paths(cwd, "diff", "--name-only", "--no-renames", commit_sha, *tail)
    changed += _git_paths(cwd, "ls-files", "--others", "--exclude-standard", *tail)
    return sorted(set(changed))


def _workspace_manifest_path(workspace: dict) -> Path:
    """The seed record beside the worktree, outside it and the project."""
    return Path(workspace["path"] + ".seed.json")


def _path_digest(path: Path) -> str | None:
    """File type, every permission bit and content of one path: a host chmod
    with no content change (0644 to 0600 as much as to 0755) is a change."""
    if path.is_symlink():
        return hashlib.sha256(("symlink:" + os.readlink(path)).encode("utf-8", "replace")).hexdigest()
    if not path.is_file():
        return None
    return f"file:{(path.stat().st_mode & 0o7777):04o}:{_file_sha256(path)}"


def _path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    if path.is_file():
        return "file"
    return "absent" if not os.path.lexists(path) else "other"


def _apply_refusal(root: Path, workspace: Path, changed: list[str]) -> list[str]:
    """Every changed path whose apply would change a file into a directory
    or back, or meet anything that is not a regular file or symlink. Checked
    before anything is written, so such an apply never starts."""
    refused = []
    for path in changed:
        source, target = _path_kind(workspace / path), _path_kind(root / path)
        if source in {"dir", "other"} or target in {"dir", "other"}:
            refused.append(path)
            continue
        parent = Path(path).parent
        while str(parent) not in {"", "."}:
            if _path_kind(root / parent) not in {"dir", "absent"}:
                refused.append(path)
                break
            parent = parent.parent
    return refused


def _apply_changes(root: Path, workspace: Path, changed: list[str]) -> None:
    """Write every changed path, or none.

    Every source is first copied into a staging directory inside the
    project (a `.handsoff*` component, so the repository digest ignores it),
    before any target is touched: a full disk or any copy error fails here
    with the project unchanged. Targets are then swapped with renames on the
    same filesystem, each existing target moved aside first. Any failure
    rolls back with renames in reverse. If a rollback step itself fails the
    staging directory, holding every moved-aside original, is KEPT and the
    error names it; it is removed only after a clean apply or rollback.
    An overwritten file keeps the project's permission bits except the
    executable bit, which follows the session (#359 review attempt 2)."""
    staging = Path(tempfile.mkdtemp(prefix=".handsoff-apply-", dir=root))
    staged, aside = staging / "staged", staging / "aside"
    staged.mkdir()
    aside.mkdir()
    try:
        for index, path in enumerate(changed):
            source = workspace / path
            if source.is_symlink() or source.exists():
                shutil.copy2(source, staged / str(index), follow_symlinks=False)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    undo = []
    try:
        for index, path in enumerate(changed):
            target, new = root / path, staged / str(index)
            mode = None
            if target.is_symlink() or target.is_file():
                if not target.is_symlink():
                    mode = (target.stat().st_mode & 0o7777)
                os.rename(target, aside / str(index))
                undo.append(lambda target=target, saved=aside / str(index): os.rename(saved, target))
            if not (new.is_symlink() or new.exists()):
                continue
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                undo.append(lambda directory=directory: directory.rmdir())
            if mode is not None and not new.is_symlink():
                os.chmod(new, (mode & ~0o111) | ((new.stat().st_mode & 0o7777) & 0o111))
            os.rename(new, target)
            undo.append(lambda target=target, new=new: os.rename(target, new))
    except BaseException:
        rollback_errors = []
        for step in reversed(undo):
            try:
                step()
            except OSError as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise HandsoffError(
                f"apply rollback incomplete; originals kept in {aside.relative_to(root)} "
                f"under the project root ({rollback_errors[0][:80]})")
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)


def _owned_snapshot(cwd: Path, owned: list[str]) -> dict:
    """Content digest of every tracked or untracked (not ignored) file under
    the owned paths of one tree."""
    if not owned:
        return {}
    listed = _git_paths(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "--", *owned)
    return {path: _path_digest(Path(cwd) / path) for path in sorted(set(listed))}


def create_implementer_workspace(root: Path, session: dict) -> Path:
    """#359: the detached worktree an ownership-declaring implementer runs in.

    The worktree starts at the launch commit and is then seeded with the
    project's working-tree content for every path that differs from it
    (dirty tracked files and untracked ones), so the session sees the tree
    the host sees. The seed record keeps what was seeded and the digest of
    every owned file as seeded: the apply compares against that content,
    never against the launch commit."""
    root = Path(root).resolve()
    workspace = session["workspace"]
    path = Path(workspace["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Review attempt 3: the host baseline is taken BEFORE seeding and checked
    # again after, so a host edit that lands while the worktree is being
    # seeded can never become the baseline and be overwritten at apply.
    owned = session.get("owned_paths") or []
    baseline = _owned_snapshot(root, owned)
    result = _git(root, "worktree", "add", "--detach", str(path), workspace["launch_commit"])
    if result.returncode != 0:
        raise HandsoffError(f"git worktree add failed: {result.stderr.strip()[:160]}")
    seeded = {}
    for relative in _changed_since(root, workspace["launch_commit"]):
        if any(part.startswith(".handsoff") for part in relative.split("/")):
            continue  # Handsoff's own side state (beacons, locks, temp files), never project content
        source, target = root / relative, path / relative
        if source.is_dir() and not source.is_symlink():
            continue  # a nested repository; git names it, not its files
        if target.is_symlink() or target.is_file():
            target.unlink()
        try:
            if source.is_symlink() or source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed while the workspace was seeded (an atomic write's temp file)
        seeded[relative] = _path_digest(target)
    # #416: the run's version pin (never in git) and its effective config (a
    # skip-worktree local edit git does not report) are seeded as well, so
    # ledger verification runs in the worktree and apply treats them as seeded.
    for relative in (VERSION_PIN_FILE, "handsoff.toml"):
        source, target = root / relative, path / relative
        if source.is_file() and not source.is_symlink():
            if target.is_symlink() or target.is_file():
                target.unlink()
            shutil.copy2(source, target)
            seeded[relative] = _path_digest(target)
    linked, skipped = _link_local_paths(root, path, load_config(root).get("workspace_local_paths") or [])
    # The host-edit baseline is the PROJECT's owned files at launch, not the
    # worktree's: git checks files out 0644/0755, so a host file at 0600
    # would otherwise read as a host edit at apply time.
    if _owned_snapshot(root, owned) != baseline:
        _unlink_local_paths(path, linked)
        _git(root, "worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)
        raise HandsoffError("an owned path changed in the project while the implementer workspace was "
                            "being seeded; nothing was launched, retry the launch")
    manifest = {"seeded": seeded, "owned": baseline, "local_paths": linked, "local_paths_skipped": skipped}
    _workspace_manifest_path(workspace).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def _link_local_paths(root: Path, workspace: Path, local_paths: list[str]) -> tuple[list[str], list[dict]]:
    """#429: symlink each [workspace].local_paths entry from the project into
    the worktree. A path must exist, resolve inside the root and be
    gitignored; anything else is skipped and named, never linked."""
    linked, skipped = [], []
    for relative in local_paths:
        source, target = root / relative, workspace / relative
        if not os.path.lexists(source):
            skipped.append({"path": relative, "reason": "absent"})
            continue
        try:
            source.resolve().relative_to(root)
        except ValueError:
            skipped.append({"path": relative, "reason": "outside the project root"})
            continue
        if _git(root, "check-ignore", "-q", "--", relative).returncode != 0:
            skipped.append({"path": relative, "reason": "not gitignored"})
            continue
        if os.path.lexists(target):
            skipped.append({"path": relative, "reason": "present in the worktree"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, target, target_is_directory=source.is_dir())
        linked.append(relative)
    return linked, skipped


def _unlink_local_paths(workspace: Path, linked: list[str]) -> None:
    """#429: the links go first, so removing a worktree never reaches the
    project's own .venv or knowledge base through them."""
    for relative in linked:
        target = Path(workspace) / relative
        if target.is_symlink():
            target.unlink()


def _is_local_path(path: str, local_paths: list[str]) -> bool:
    return any(path == item or path.startswith(item + "/") for item in local_paths)


def workspace_local_path_report(session: dict) -> dict:
    """#429: what the launch linked and skipped, from the seed record."""
    try:
        manifest = json.loads(_workspace_manifest_path(session["workspace"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return {"linked": [], "skipped": []}
    return {"linked": manifest.get("local_paths") or [], "skipped": manifest.get("local_paths_skipped") or []}


def remove_implementer_workspace(root: Path, session: dict) -> None:
    workspace = session.get("workspace") if isinstance(session, dict) else None
    if not isinstance(workspace, dict):
        return
    try:
        manifest = json.loads(_workspace_manifest_path(workspace).read_text(encoding="utf-8"))
        _unlink_local_paths(Path(workspace["path"]), manifest.get("local_paths") or [])
    except (OSError, ValueError):
        pass
    _git(root, "worktree", "remove", "--force", workspace["path"])
    shutil.rmtree(workspace["path"], ignore_errors=True)
    _workspace_manifest_path(workspace).unlink(missing_ok=True)
    _git(root, "worktree", "prune")


def implementer_workspace_changes(cfg: dict, session: dict, manifest: dict | None = None) -> list[str]:
    """#359 #413: the paths a workspace session changed, against what it was
    seeded with: what an apply would copy back, and what `status` lists for a
    stopped session's kept workspace. Handsoff's own state files are neither."""
    workspace = Path(session["workspace"]["path"])
    if manifest is None:
        manifest = json.loads(_workspace_manifest_path(session["workspace"]).read_text(encoding="utf-8"))
    seeded = manifest["seeded"]
    local_paths = manifest.get("local_paths") or []
    state_files = {cfg["status_file"], cfg["acceptance_file"], cfg["event_log"], cfg["verification_log"]}
    # #416: handsoff.toml is out of the evidence digest but seeded here, so
    # an unchanged copy is a no-op and an implementer's edit is attributed
    # (and refused as outside its ownership), never silently dropped.
    # #429: a linked local path is never in the change set, so it is never
    # applied back nor checked against ownership.
    return sorted(
        path for path in set(_changed_since(workspace, session["workspace"]["launch_commit"])) | set(seeded)
        if (path == "handsoff.toml" or not _digest_excluded(path, state_files))
        and not _is_local_path(path, local_paths)
        and (path not in seeded or _path_digest(workspace / path) != seeded[path]))


def workspace_kept_on_stop(session: object) -> bool:
    """#413: an implementer workspace is kept for an explicit disposition when
    its session was stopped (cancelled, timed out, or its child killed by a
    signal) and nothing was applied. #429: a failed session (any failure
    category, token_budget_exhaustion and ownership_violation included) is
    kept the same way, so its partial edits can still be applied or
    discarded. A normal exit keeps today's automatic apply."""
    if not isinstance(session, dict) or not isinstance(session.get("workspace"), dict):
        return False
    if (session.get("apply") or {}).get("state") == "applied":
        return False
    return session.get("state") in {"cancelled", "timed_out", "failed"}


def apply_implementer_workspace(root: Path, session_id: str) -> dict:
    """#359: copy an ownership-declaring implementer's changes back.

    Attribution is the session's OWN worktree diffed against what it was
    seeded with, never the project tree, so another session finishing first
    never enters this diff. Handsoff's own state files (the repository
    digest's structural exclusion: `.handsoff*` components and the ledgers)
    are neither attributed nor applied, so a session that ran verify in its
    worktree is not refused for them. All or nothing: a changed path outside
    owned_paths, an owned file the host changed (content, type or executable
    bit) in the project since it was seeded, or a file/directory type
    transition refuses the whole apply and nothing is copied; a failure
    while copying restores every target before it propagates.
    Returns {state, paths}."""
    root = Path(root).resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict) or not isinstance(session.get("workspace"), dict):
            raise HandsoffError(f"agent session {session_id} has no implementer workspace")
        owned = session.get("owned_paths") or []
        workspace = Path(session["workspace"]["path"])
        manifest = json.loads(_workspace_manifest_path(session["workspace"]).read_text(encoding="utf-8"))
        changed = implementer_workspace_changes(cfg, session, manifest)
        outside = [path for path in changed
                   if not any(path == mine or path.startswith(mine + "/") for mine in owned)]
        if outside:
            return {"state": "refused", "reason": "outside_ownership", "paths": outside[:64]}
        before, now = manifest["owned"], _owned_snapshot(root, owned)
        collided = sorted(path for path in set(before) | set(now) if before.get(path) != now.get(path))
        if collided:
            return {"state": "refused", "reason": "host_edit", "paths": collided[:64]}
        transitions = _apply_refusal(root, workspace, changed)
        if transitions:
            return {"state": "refused", "reason": "type_transition", "paths": transitions[:64]}
        _apply_changes(root, workspace, changed)
        return {"state": "applied", "paths": changed[:64]}


def create_agent_session(root: Path, *, role: str, actor: str, adapter: str,
                         requested_model: str, resolution_source: str,
                         id_factory=None, packet_id: str | None = None,
                         design_hash: str | None = None, tier: str | None = None,
                         tier_reason: str | None = None,
                         amendment_id: str | None = None,
                         adaptive_routing: dict | None = None,
                         budget_decision: dict | None = None,
                         reviewer_isolation: dict | None = None,
                         reasoning_effort: str | None = None,
                         routing_contract: dict | None = None,
                         owned_paths: list[str] | None = None,
                         work_items: list[str] | None = None) -> dict:
    """Commit the immutable launch snapshot before a managed child starts.

    The task/prompt, environment, runner output, credentials, and token data
    are deliberately not accepted by this API, so callers cannot accidentally
    persist them as telemetry. `packet_id`/`design_hash` (#36) record which
    delta review packet, if any, a Phase-2 reviewer was launched with.
    `tier`/`tier_reason` (#37) record which reviewer tier the selection
    chose and why; the session keeps `tier`, and a `design_reviewer_selected`
    event carrying both is committed with the launch.
    `owned_paths` (#359) is an implementer's declared ownership; with it a
    second implementer may run beside a live one in its own worktree.
    """
    root = root.resolve()
    if role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("agent session role must be architect, supervisor, implementer, or reviewer")
    if owned_paths is not None:
        if role != "implementer":
            raise HandsoffError("--owns applies to implementer sessions only")
        owned_paths = validate_owned_paths(list(owned_paths))
    if work_items is not None and role != "implementer":
        raise HandsoffError("--item applies to implementer sessions only")
    actor = validate_agent_actor(actor)
    if adapter not in LAUNCHABLE_AGENT_ADAPTERS:
        raise HandsoffError("agent session adapter must be codex, claude or a registered contract adapter")
    if not adapter_serves_role(adapter, role):
        # #305: refused before any id, reservation or event exists.
        raise HandsoffError(f"agent session refused: {adapter} does not serve the {role} role")
    requested_model = validate_agent_model(requested_model)
    if resolution_source not in AGENT_SESSION_RESOLUTION_SOURCES:
        raise HandsoffError("agent session resolution source is invalid")
    adaptive_routing = (validate_session_adaptive_routing(adaptive_routing)
                        if adaptive_routing is not None else None)
    if adaptive_routing is not None and "evidence" in adaptive_routing:
        import handsoff_evidence_routing  # #303: imports routing's own layers
        adaptive_routing["evidence"] = handsoff_evidence_routing.validate_evidence_record(
            adaptive_routing["evidence"])
    routing_contract = (validate_session_routing_contract(
        routing_contract, {"adapter": adapter, "model": requested_model})
        if routing_contract is not None else None)
    budget_decision = (validate_session_budget_decision(budget_decision)
                       if budget_decision is not None else None)
    reviewer_isolation = (validate_reviewer_isolation_contract(reviewer_isolation)
                          if reviewer_isolation is not None else None)
    # Direct API callers may load/create legacy fixture sessions without the
    # additive field; every managed reviewer launch supplies it below.
    if role != "reviewer" and reviewer_isolation is not None:
        raise HandsoffError("reviewer_isolation applies only to reviewer sessions")
    if reviewer_isolation is not None and (reviewer_isolation["adapter"] != adapter
                                            or reviewer_isolation["decision"] != "enforce"):
        raise HandsoffError("reviewer isolation contract does not authorize this adapter launch")
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
        if work_items is not None:
            from handsoff_workflow import validate_bound_work_items
            work_items = validate_bound_work_items(acceptance, work_items)  # #415
        legacy_risk_defaulted = False
        if adaptive_routing is not None:
            policy = status.get("model_policy", cfg.get("model_policy", DEFAULT_MODEL_POLICY))
            if not model_policy_allows(policy, adapter, requested_model):
                raise HandsoffError("adaptive launch refused by the mission model policy")
            route_cfg = {**cfg, "model_policy": validate_model_policy(policy)}
            status_risk_class = status.get("risk_class")
            if status_risk_class is None and adaptive_routing["risk_class"] == "routine":
                legacy_risk_defaulted = True
            elif status_risk_class != adaptive_routing["risk_class"]:
                raise HandsoffError("adaptive routing selection is stale for the run risk class")
            refreshed = route_adaptive_profile(
                route_cfg, risk_class=adaptive_routing["risk_class"],
                available_tiers=[adaptive_routing["tier"]],
                available_adapters=[adaptive_routing["adapter"]],
                mission_usage=adaptive_usage(status),
                fleet_usage=adaptive_fleet_usage(root, current_status=status),
                # Initial launches are outside the repair/escalation drain;
                # this lock-protected call exists to recheck usage budgets.
                deterministic_checks_complete=True,
            )
            if refreshed.get("state") != "selected":
                raise HandsoffError(f"adaptive launch refused before mutation: {refreshed.get('reason')}")
            pair = (refreshed["tier"], refreshed["profile"]["adapter"], refreshed["profile"]["model"])
            expected = (adaptive_routing["tier"], adaptive_routing["adapter"], adaptive_routing["model"])
            if pair != expected:
                raise HandsoffError("adaptive routing selection changed before session commit; rebuild the launch spec")
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
        launch_commit = None
        if role == "implementer":
            # #359: the ownership check (implementer_admission) replaces the
            # one-live-session rule for implementers. Refused there, before
            # any id, reservation or event exists.
            beside = live_implementer_sessions(status)
            refusal = implementer_ownership_refusal(status, owned_paths)  # #407
            if refusal:
                raise HandsoffError(refusal)
            launch_commit = implementer_admission(root, beside, owned_paths, preferred_id=active_id)
        elif active and active.get("state") in AGENT_SESSION_LIVE_STATES:
            raise HandsoffError(
                f"role {role} already has live agent session {active_id} ({active.get('state')})"
            )
        removed_session_ids = _prune_agent_sessions(sessions, current)
        session_id = _new_agent_session_id(sessions, id_factory=id_factory)
        now = datetime.now(timezone.utc).isoformat()
        if reservation is not None:
            reservation["launch_session_id"] = session_id
        proposed = deepcopy(status)
        if legacy_risk_defaulted:
            proposed["risk_class"] = "routine"
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
        if adaptive_routing is not None:
            session["adaptive_routing"] = deepcopy(adaptive_routing)
        if budget_decision is not None:
            session["budget_decision"] = deepcopy(budget_decision)
        if reasoning_effort is not None:
            session["reasoning_effort"] = reasoning_effort
        if routing_contract is not None:
            session["routing_contract"] = deepcopy(routing_contract)
        if reviewer_isolation is not None:
            session["reviewer_isolation"] = deepcopy(reviewer_isolation)
        if role == "reviewer":
            # #405 #406: the rules this verdict will judge; adoption compares
            # them with the rules in force then.
            from handsoff_workflow import rules_set_entries
            session["rules_entries"] = rules_set_entries(root)
        if owned_paths is not None:
            session["owned_paths"] = list(owned_paths)
        if work_items:
            session["work_items"] = list(work_items)  # #415: the explicit binding
        if launch_commit is not None:
            session["workspace"] = {"path": str(implementer_workspace_dir(root) / session_id),
                                    "launch_commit": launch_commit}
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
        if legacy_risk_defaulted:
            extra_events.append({
                "kind": "adaptive_risk_defaulted",
                "message": "Legacy run defaulted to routine adaptive routing at managed launch",
                "risk_class": "routine", "session_id": session_id,
            })
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
            **({"owned_paths": list(owned_paths), "concurrent": bool(beside)}
               if owned_paths is not None else {}),
        )
        return deepcopy(session)


def transition_agent_session(root: Path, session_id: str, state: str,
                             *, exit_code: int | None = None,
                             failure: dict | None = None, usage: dict | None = None,
                             reported_model: str | None = None,
                             apply: dict | None = None,
                             halfway_at: str | None = None) -> dict:
    """Apply a session-ID-matched lifecycle-only update under the lock.
    `apply` (#359) records a concurrent implementer's apply-back outcome.
    `halfway_at` (#409) rides the transition to running, in the same commit."""
    if halfway_at is not None and (state != "running" or not isinstance(halfway_at, str)):
        raise HandsoffError("halfway_at is recorded on the transition to running only")
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
    if reported_model is not None:
        reported_model = validate_agent_model(reported_model)
        if not terminal:
            raise HandsoffError("reported model is recorded on the terminal transition only")
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
        # #359: a concurrent implementer launched earlier is no longer the
        # role's current pointer but is still live and still its own session.
        if current.get(role) != session_id and not (
                role == "implementer"
                and any(item.get("session_id") == session_id for item in live_implementer_sessions(status))):
            raise HandsoffError(f"stale agent session {session_id} is no longer current for role {role}")
        if apply is not None and (not terminal or not isinstance(existing.get("workspace"), dict)):
            raise HandsoffError("apply is recorded on the terminal transition of a workspace session only")
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
            if halfway_at is not None:
                updated["halfway_at"] = halfway_at
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
            if proposed["current_agent_sessions"].get(role) == session_id:
                # #420: an ended session never stays current
                proposed["current_agent_sessions"].pop(role)
            if reported_model is not None:
                updated["reported_model"] = reported_model
            if usage is not None:
                updated["usage"] = usage  # #168
            if apply is not None:
                updated["apply"] = {"state": apply["state"], "paths": list(apply["paths"])}
            if replacement is not None:
                replacement_state = "recovered" if state == "completed" else "failed"
                replacement["state"] = replacement_state
                replacement["ended_at"] = now
                replacement["handoff"]["state"] = replacement_state
                replacement["handoff"]["ended_at"] = now
            if failure is not None:
                failures = proposed.setdefault("agent_failures", {})
                if role == "implementer" and "progress_summary" not in failure:
                    # #215: the account of what was finished, from the ledger
                    try:
                        acceptance = load_unique_json(acceptance_path(root, cfg))
                    except (HandsoffError, OSError, ValueError):
                        acceptance = {}
                    failure = {**failure, "progress_summary": progress_summary(updated.get("progress"), acceptance)}
                failures[session_id] = {"session_id": session_id, **failure, "at": now}
            if role == "reviewer":
                warnings = proposed.setdefault("warnings", [])
                warnings[:] = [item for item in warnings
                                if item != "a reviewer is live; do not edit the tree"]
        schema_errors = validate_status_schema(proposed)
        if schema_errors:
            raise HandsoffError(schema_errors[0])
        # #415: a completed session bound to work items (launch --item)
        # credits exactly those items, once its workspace (if any) applied
        credited_acceptance, credited = None, []
        if state == "completed" and updated.get("work_items"):
            from handsoff_workflow import credit_session_work_items
            credited_acceptance = load_unique_json(acceptance_path(root, cfg))
            credited = credit_session_work_items(proposed, credited_acceptance, session_id, now)
            if not credited:
                credited_acceptance = None
        event_kind = f"agent_session_{state}"
        commit(
            root, cfg, status=proposed, acceptance=credited_acceptance,
            **({"work_items_implemented": credited} if credited else {}),
            event_kind=event_kind,
            event_message=f"Managed {role} agent session is {state.replace('_', ' ')}",
            session_id=session_id, role=role, state=state, exit_code=exit_code,
            failure_category=failure["category"] if failure else None,
            failure_dependency=failure.get("dependency") if failure else None,
            failure_operation=failure.get("operation") if failure else None,
            replacement_id=replacement.get("replacement_id") if replacement else None,
            replacement_state=replacement.get("state") if replacement else None,
            usage=usage,
            reported_model=reported_model,
        )
        result = deepcopy(updated)
    if terminal:
        update_session_liveness(root, session_id, remove=True)
    return result


def _new_bounded_id(prefix: str, pattern: re.Pattern, existing: set[str], id_factory=None) -> str:
    factory = id_factory or (lambda: f"{prefix}-{uuid.uuid4().hex}")
    for _ in range(16):
        candidate = factory()
        if not isinstance(candidate, str) or not pattern.fullmatch(candidate):
            raise HandsoffError(f"generated {prefix} id is invalid")
        if candidate not in existing:
            return candidate
    raise HandsoffError(f"could not allocate a collision-free {prefix} id")


def claim_precreated_agent_session(root: Path, session_id: str, *, role: str,
                                   adapter: str, requested_model: str,
                                   routing_contract: dict | None = None) -> dict:
    """One-way pre-spawn claim of the exact reserved session/profile.

    #304: a recovery replacement is created by the reservation, not by
    create_agent_session, so the claim is where its routing contract is
    persisted, validated against the reserved adapter and model."""
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
        if routing_contract is not None:
            routing_contract = validate_session_routing_contract(
                routing_contract, {"adapter": adapter, "model": requested_model})
        proposed = deepcopy(status)
        if routing_contract is not None:
            proposed["agent_sessions"][session_id]["routing_contract"] = deepcopy(routing_contract)
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
        return deepcopy(proposed["agent_sessions"][session_id])


def latest_terminal_session_id(sessions: dict, role: str) -> str | None:
    """#420: the role's latest unsuperseded terminal session, by end time.
    A session is superseded once a later launch of the role exists."""
    own = [item for item in (sessions or {}).values()
           if isinstance(item, dict) and item.get("role") == role]
    ended = [item for item in own if item.get("state") in AGENT_SESSION_TERMINAL_STATES
             and not any(str(other.get("started_at") or "") > str(item.get("started_at") or "")
                         for other in own)]
    if not ended:
        return None
    latest = max(ended, key=lambda item: (str(item.get("ended_at") or ""), str(item.get("started_at") or "")))
    return latest.get("session_id")


def role_session_ids(status: dict) -> dict:
    """#420: each role's session id: the live pointer while one is current,
    else the latest unsuperseded terminal session. A terminal transition
    clears the pointer, so a reader of an ended session reads it here."""
    sessions = status.get("agent_sessions") if isinstance(status, dict) else None
    pointers = status.get("current_agent_sessions") if isinstance(status, dict) else None
    if not isinstance(sessions, dict):
        return {role: None for role in SELECTABLE_AGENT_ROLES}
    found = {}
    for role in SELECTABLE_AGENT_ROLES:
        pointed = pointers.get(role) if isinstance(pointers, dict) else None
        if isinstance(pointed, str) and isinstance(sessions.get(pointed), dict):
            found[role] = pointed
        else:
            found[role] = latest_terminal_session_id(sessions, role)
    return found


def current_agent_sessions(status: dict) -> dict:
    """Return each role's session (#420: role_session_ids), a copy."""
    sessions = status.get("agent_sessions") if isinstance(status, dict) else None
    if not isinstance(sessions, dict):
        return {role: None for role in SELECTABLE_AGENT_ROLES}
    return {role: deepcopy(sessions[sid]) if sid else None
            for role, sid in role_session_ids(status).items()}


def implementer_ownership_refusal(status: dict, owned_paths, *, except_session_id: str | None = None) -> str | None:
    """#407: once any implementer session in the run, live or historical,
    declared owned paths, every implementer launch must declare its own."""
    if owned_paths:
        return None
    declared = next((item for sid, item in sorted((status.get("agent_sessions") or {}).items())
                     if sid != except_session_id and isinstance(item, dict)
                     and item.get("role") == "implementer" and item.get("owned_paths")), None)
    if declared is None:
        return None
    return (f"implementer launch refused: implementer session {declared.get('session_id')} declared "
            "owned paths, so this launch must declare its own with --owns")


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
    # #417: a grant may cover several rounds (design-review-authorize
    # --rounds N); each recorded attempt past the limit spends one. A grant
    # written before the field existed covers exactly one.
    rounds_remaining = int(authorization.get("rounds_remaining", 1) or 0) if authorized else 0
    return {
        "attempts": attempts,
        "limit": limit,
        "authorized": authorized,
        "rounds_remaining": rounds_remaining,
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


#: #417: the most rounds one design-review-authorize may grant.
MAX_DESIGN_REVIEW_AUTHORIZED_ROUNDS = 5
#: #417: who an engine-granted round is recorded as granted by.
DESIGN_ROUNDS_ON_CONVERGENCE_ACTOR = "policy:design_rounds_on_convergence"


def spend_design_review_authorization(status: dict, now: str) -> int:
    """#417: one recorded attempt spends one authorized round. The grant
    stays open (attempt_permitted moves to the following attempt and the
    launch reservation is released) while rounds remain; the last round
    sets consumed_at exactly as a single-round grant always has. Returns
    the rounds still remaining afterwards."""
    authorization = status["design_review_authorization"]
    remaining = max(int(authorization.get("rounds_remaining", 1) or 0) - 1, 0)
    if "rounds_remaining" in authorization or remaining:
        authorization["rounds_remaining"] = remaining
    if remaining:
        authorization["attempt_permitted"] = int(authorization["attempt_permitted"]) + 1
        authorization["launch_session_id"] = None
    else:
        authorization["consumed_at"] = now
    return remaining


def design_review_finding_counts(status: dict) -> tuple[int, int] | None:
    """#417: (latest, previous) finding counts of the last two recorded
    design-review rounds, or None with fewer than two. Recorded findings
    carry no severity, so only the count is compared; a legacy entry's
    findings list is counted the same way, whatever its items hold."""
    history = [h for h in (status.get("design_review_history") or []) if isinstance(h, dict)]
    if len(history) < 2:
        return None

    def count(entry: dict) -> int:
        findings = entry.get("findings")
        return len(findings) if isinstance(findings, list) else 0

    return count(history[-1]), count(history[-2])


def design_round_auto_authorization(status: dict, cfg: dict) -> dict | None:
    """#417: whether [workflow] design_rounds_on_convergence grants one more
    round right now: the budget is exhausted, the cumulative per-run
    allowance (status.design_rounds_auto_used, never reset by a proposal or
    a restart) is not spent, and the latest round recorded strictly fewer
    findings than the one before it. Returns the event fields, or None."""
    allowance = int(cfg.get("design_rounds_on_convergence", 0) or 0)
    used = int(status.get("design_rounds_auto_used", 0) or 0)
    if allowance <= 0 or used >= allowance:
        return None
    budget = design_review_budget(status, cfg)
    if not budget["exhausted"]:
        return None
    counts = design_review_finding_counts(status)
    if counts is None or not counts[0] < counts[1]:
        return None
    return {"round": budget["next_attempt"], "findings": counts[0], "previous_findings": counts[1],
            "auto_used": used + 1, "allowance": allowance}


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


def progress_summary(progress: list | None, acceptance: dict | None) -> dict:
    """#215: {done, partial, untouched} from a session's progress list and
    the acceptance registry: the last state per reported criterion wins;
    every automated criterion never reported is untouched. Computed from
    the ledger, never trusted from the child."""
    last: dict[str, str] = {}
    for item in progress or []:
        if isinstance(item, dict) and isinstance(item.get("criterion"), str) and item.get("state") in PROGRESS_STATES:
            last[item["criterion"]] = item["state"]
    automated = [c["id"] for c in (acceptance or {}).get("criteria") or []
                 if isinstance(c, dict) and c.get("verification") == "automated" and isinstance(c.get("id"), str)]
    order = list(dict.fromkeys(automated + sorted(last)))
    out = {"done": [], "partial": [], "untouched": []}
    for criterion in order:
        # #413: present only when an implementer reported one
        out.setdefault(last.get(criterion, "untouched"), []).append(criterion)
    return out


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


