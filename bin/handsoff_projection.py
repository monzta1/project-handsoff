#!/usr/bin/env python3
"""Handsoff run projection: what the board reads, derived from the ledger.

#284 stage 6. Forty-four symbols: the live status view, the operation
inventory, the host-wait derivation and the sleep accounting behind it.
This is the read side. It computes views; it does not decide gates.

Layer: core -> routing -> config -> ledger -> resources -> agent_runtime
-> here. Every import is module level.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

from handsoff_core import HandsoffError
from handsoff_routing import adaptive_deployment_approval_required
from handsoff_config import (
    AUTO_AGENT_ADAPTER,
    DEFAULT_CONFIG,
    EXPLICIT_PROFILE_SOURCE,
    HOST_AGENT_ADAPTER,
    LEGACY_UNCONFIGURED_AGENT_ADAPTER,
    SELECTABLE_AGENT_ROLES,
)
from handsoff_ledger import (
    LIVE_BEACON_FILE,
    OUTPUT_LIVENESS_FILE,
    VERIFICATION_REQUIREMENTS,
    VERIFY_INFLIGHT_DIR,
)
from handsoff_agent_runtime import (
    AGENT_OUTPUT_FILE,
    AGENT_SESSION_LIVE_STATES,
    AGENT_SESSION_TERMINAL_STATES,
    active_regression_request,
    agent_profiles,
    current_agent_sessions,
    default_agent_adapter,
    design_review_budget,
)



LIVE_BEACON_KEYS = ("session_id", "role", "state", "pid", "beacon_at", "ended_at", "exit_code")


LIVE_BEACON_FRESH_SECONDS = 15.0


OUTPUT_LIVENESS_KEYS = ("session_id", "role", "output_at", "chunks", "bytes")


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
    deployment_pending = (phase == 7 and adaptive_deployment_approval_required(status, cfg)
                          and not status.get("deployment_approved"))
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


def live_beacon_path(root: Path) -> Path:
    return Path(root) / LIVE_BEACON_FILE


def _seconds_since(timestamp: str | None, now: datetime) -> float | None:
    minutes = _minutes_since(timestamp, now)
    return None if minutes is None else minutes * 60


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


def output_liveness_path(root: Path) -> Path:
    return Path(root) / OUTPUT_LIVENESS_FILE


def agent_output_path(root: Path) -> Path:
    return Path(root) / AGENT_OUTPUT_FILE


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
        adaptive_deployment_approval_required(status, cfg)
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


RECOVERABLE_FAILURE_CATEGORIES = {
    "auth_failure", "rate_limit", "context_exhaustion", "timeout",
    "runtime_environment", "process_crash", "non_zero_exit", "presumed_lost", "external_timeout",
    "no_artifact", "protocol_silence",
}


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
    # The host already knows the exact model serving the supervising session.
    # Prefer the plainly named variable; retain the old CLASS spelling so a
    # dashboard started by an older host keeps its identity after an upgrade.
    model_class = str(
        os.environ.get("HANDSOFF_HOST_MODEL")
        or os.environ.get("HANDSOFF_HOST_MODEL_CLASS")
        or ""
    ).strip()[:64] or None

    def identified(family, actor, source):
        value = {"family": family, "actor": actor, "source": source}
        # Preserve the established three-key API when the runtime does not
        # expose an exact model; an unavailable value is absence, not a new
        # nullable field forced on every existing consumer.
        if model_class is not None:
            value["model_class"] = model_class
        return value

    for event in events:
        if event.get("kind") == "initialized" and actor_family(event.get("by")):
            return identified(actor_family(event["by"]), event["by"], "initialized")
    for event in reversed(events):
        if event.get("kind") in HOST_COMMAND_EVENT_KINDS and actor_family(event.get("by")):
            return identified(actor_family(event["by"]), event["by"], "ledger")
    implemented = status.get("implemented_by") if isinstance(status, dict) else None
    if actor_family(implemented):
        return identified(actor_family(implemented), implemented, "ledger")
    return identified("unknown", None, "none")


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


def _iso_seconds(start: object, end: object) -> float | None:
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    seconds = (b - a).total_seconds()
    return seconds if seconds >= 0 else None
