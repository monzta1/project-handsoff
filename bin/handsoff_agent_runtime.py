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
import math
import os
import re
import shutil
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
    classify_adaptive_risk,
    route_adaptive_profile,
    validate_session_adaptive_routing,
)
from handsoff_config import (
    DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    DEFAULT_MODEL_POLICY,
    HOST_AGENT_ADAPTER,
    SELECTABLE_AGENT_ADAPTERS,
    SELECTABLE_AGENT_ROLES,
    load_config,
    model_policy_allows,
    validate_agent_model,
    validate_model_policy,
)
from handsoff_ledger import (
    MAX_WORK_ITEMS,
    config_hash,
    feature_enabled,
    VERIFICATION_REQUIREMENTS,
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
VERIFICATION_KINDS = {"checks", "manual", "browser", "live", "baseline"}






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


def create_agent_session(root: Path, *, role: str, actor: str, adapter: str,
                         requested_model: str, resolution_source: str,
                         id_factory=None, packet_id: str | None = None,
                         design_hash: str | None = None, tier: str | None = None,
                         tier_reason: str | None = None,
                         amendment_id: str | None = None,
                         adaptive_routing: dict | None = None,
                         budget_decision: dict | None = None,
                         reviewer_isolation: dict | None = None) -> dict:
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
    adaptive_routing = (validate_session_adaptive_routing(adaptive_routing)
                        if adaptive_routing is not None else None)
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
        if reviewer_isolation is not None:
            session["reviewer_isolation"] = deepcopy(reviewer_isolation)
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
        )
        return deepcopy(session)








def transition_agent_session(root: Path, session_id: str, state: str,
                             *, exit_code: int | None = None,
                             failure: dict | None = None, usage: dict | None = None,
                             reported_model: str | None = None) -> dict:
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
            if reported_model is not None:
                updated["reported_model"] = reported_model
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
        out[last.get(criterion, "untouched")].append(criterion)
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
























