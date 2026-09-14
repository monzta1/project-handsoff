#!/usr/bin/env python3
"""Local dashboard for Project Handsoff.

Most workflow artifacts remain read-only. Narrow same-origin endpoints exist
for allowlisted Agent Settings and the guarded design/deployment approval
commands exposed by the supervisor CLI.
"""
from __future__ import annotations

import argparse
import hmac
import json
import re
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


ASSET_ROOT = Path(__file__).resolve().parent.parent / "dashboard"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/lib/dashboard-logic.js": ("lib/dashboard-logic.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_SETTINGS_BODY = 16 * 1024
# #48: sixteen answers of up to 1024 characters each, plus envelope.
MAX_QUESTION_BATCH_BODY = 64 * 1024


def _settings_view(cfg: dict) -> dict:
    effective_profiles = lib.resolved_agent_profiles(cfg)
    return {
        "agents": dict(cfg.get("agents", {})),
        "profiles": lib.agent_profiles(cfg),
        "profile_sources": lib.profile_sources(cfg),
        "recommended_crew": {role: dict(profile) for role, profile in lib.RECOMMENDED_CREW.items()},
        "crew": lib.crew_view(cfg),
        "effective_profiles": effective_profiles,
        "fallbacks": lib.fallback_profiles(cfg),
        "max_failovers_per_role": cfg.get(
            "max_failovers_per_role", lib.DEFAULT_MAX_FAILOVERS_PER_ROLE,
        ),
        # #37: read-only here; the settings dialog never writes these keys.
        "reviewer_followup": lib.followup_reviewer_profile(cfg),
        "max_fallback_profiles": lib.MAX_FALLBACK_PROFILES,
        "default_adapter": lib.default_agent_adapter(),
        "default_order": list(lib.DEFAULT_AGENT_PREFERENCE),
        "allowed_adapters": list(lib.AGENT_SETTING_ADAPTERS),
        "availability": lib.adapter_availability(),
        "availability_scope": (
            "Executable discovery only; it does not prove authentication, account entitlement, "
            "network access, or model validity."
        ),
        "providers": lib.provider_status(),
        "providers_scope": (
            "Detection only, shown for information: CLI providers are checked for an executable on "
            "PATH, credentialed providers for the presence of an environment variable name. Handsoff "
            "never reads, displays, or stores credential values -- you supply them yourself."
        ),
    }


def _strict_json_object(payload: bytes) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise lib.HandsoffError(f"invalid settings JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise lib.HandsoffError("settings payload must be a JSON object")
    return value


def _read_events(root: Path, cfg: dict) -> list[dict]:
    path = lib.event_log_path(root, cfg)
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


def _artifact_signature(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap, lock-consistent fingerprint of dashboard inputs.

    SSE carries only an invalidation signal. `/api/dashboard` remains the
    authoritative snapshot, which keeps event delivery small and avoids
    duplicating the gate calculations in two code paths.
    """
    with lib.project_lock(root):
        try:
            cfg = lib.load_config(root)
            paths = [
                root / "handsoff.toml",
                lib.status_path(root, cfg),
                lib.acceptance_path(root, cfg),
                lib.event_log_path(root, cfg),
                lib.verification_log_path(root, cfg),
                lib.design_evidence_path(root),
                # #33: every beacon write invalidates the snapshot so the
                # live strip follows the managed child in near real time.
                lib.live_beacon_path(root),
                # #41: the output record, at most one write per second.
                lib.output_liveness_path(root),
            ]
        except lib.HandsoffError:
            paths = [root / "handsoff.toml", lib.live_beacon_path(root), lib.output_liveness_path(root)]
        signature = []
        for path in paths:
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(path), -1, -1))
        return tuple(signature)


def _phase_view(current: int, run_complete: bool, current_name: str | None = None) -> list[dict]:
    """The current phase renders "active" (the pulsing in-progress bar) only
    while the run is still moving. Once status is complete, phase 8 being
    "current" no longer means "in progress", so it renders solid-complete
    like every phase before it instead of blinking forever.
    """
    return [
        {
            "number": number,
            "name": current_name if number == current and current_name else name,
            "state": ("complete" if number < current or (number == current and run_complete)
                      else "active" if number == current else "upcoming"),
        }
        for number, name in lib.PHASES.items()
    ]


def _display_phase_name(status: dict) -> str:
    """Never describe an accepted deployment authorization as still awaiting it."""
    phase_number = int(status.get("phase_number", 1) or 1)
    if phase_number == 7 and status.get("deployment_approved"):
        return "Deployment authorized · ready to ship"
    return status.get("phase") or lib.PHASES.get(phase_number, "Unknown phase")


#: Which crew role is doing the work during each phase, for the dashboard's
#: chiclet row. Phase 2 is resolved dynamically below because a design
#: finding hands work back from the reviewer to the Architect.
ACTIVE_ROLE_BY_PHASE = {
    1: "architect", 3: "supervisor",
    4: "implementer", 5: "reviewer", 6: "implementer",
    7: "supervisor", 8: "supervisor",
}


def _live_managed_role(status: dict) -> str | None:
    """The role whose current managed agent session is launching or running."""
    current = lib.current_agent_sessions(status)
    for role in ("architect", "implementer", "reviewer", "supervisor"):
        session = current.get(role)
        if isinstance(session, dict) and session.get("state") in lib.AGENT_SESSION_LIVE_STATES:
            return role
    return None


def _active_role(status: dict, input_request: dict) -> str | None:
    """The crew role currently doing the work, or None when nobody is: the
    run is complete, or it is paused waiting on a human decision (the
    input-required banner already covers that case, so the chiclets go
    dark rather than falsely claiming the Supervisor is mid-task).
    """
    if status.get("status") == "complete":
        return None
    if input_request.get("required"):
        return None
    # #34/#33: an open human pause means nobody is mid-task either, whether
    # or not the run was also marked blocked.
    if isinstance(status.get("human_pause"), dict):
        return None
    # A live managed session is the ground truth for who is working right
    # now: a Phase-2 Architect drafting the design must light up as the
    # Architect, not be assumed to be the reviewer.
    live = _live_managed_role(status)
    if live:
        return live
    phase_number = int(status.get("phase_number", 1) or 1)
    if phase_number == 2:
        review = status.get("design_review") or {}
        return "architect" if review.get("decision") == "changes_requested" else "reviewer"
    return ACTIVE_ROLE_BY_PHASE.get(phase_number)


def _input_request(status: dict, cfg: dict) -> dict:
    """Translate an explicit workflow pause into a dashboard alert.

    Supervisors record user-dependent pauses as status=blocked with the exact
    request in next_action. Phase 7's explicit approval wait is inherently a
    user pause, so it is surfaced even without a separate blocked transition.
    A narrow phrase check supports older state written before that convention.
    """
    workflow_status = str(status.get("status") or "")
    regression = next((item for item in reversed(status.get("regression_requests") or [])
                       if item.get("state") == "awaiting_approval"), None)
    phase = int(status.get("phase_number", 1) or 1)
    next_action = str(status.get("next_action") or "Pilot authorization is required before the mission can continue.")
    approval_missing = (
        cfg.get("deployment_requires_explicit_approval", True)
        and phase == 7
        and not status.get("deployment_approved")
    )
    design_review = status.get("design_review") or {}
    design_approval_missing = (
        status.get("requires_design_approval") is True
        and phase == 2
        and not status.get("design_approved")
        and design_review.get("decision") == "approved"
    )
    # Whole-word phrases only: "Launch the authorized attempt" is progress,
    # not a request, and must not re-arm the banner after the Pilot acts.
    older_signal = status.get("status") != "in_progress" and any(
        re.search(r"\b" + re.escape(phrase) + r"\b", next_action.lower()) for phrase in (
            "waiting for user", "waiting on user", "your input", "need your decision",
            "need your approval", "provide credentials", "grant permission", "authorize",
        ))
    # #42: an open amendment always waits on a named decision (review,
    # revision, or the Pilot's approval); the banner names which.
    amendment = lib.open_amendment(status)
    amendment_decision = lib.amendment_pending_decision(amendment)
    # #46: a blocking role question is a named decision the Pilot answers.
    questions = lib.open_questions(status, blocking_only=True)
    # #35 follow-up: an exhausted design-review budget is a Pilot decision
    # with its own control, like the design and deployment gates.
    budget = lib.design_review_budget(status, cfg)
    budget_exhausted = phase == 2 and budget.get("exhausted") and status.get("authorization_hold") == "design_review"
    required = bool(regression) or workflow_status == "blocked" or approval_missing or design_approval_missing \
        or older_signal or bool(amendment) or bool(questions)
    escalation = status.get("escalation") if isinstance(status.get("escalation"), dict) else None
    if regression:
        kind = "regression_approval"
        message = (f"Full regression '{regression.get('group')}' requested. Review its exact commands "
                   "and choose Accept or Decline; it will not run until accepted.")
    elif escalation:
        kind = "escalation"
        message = f"{escalation.get('reason')}. {escalation.get('required_action')}"
    elif amendment:
        amendment_id = amendment.get("amendment_id")
        if amendment_decision == "pilot_approval":
            kind = "amendment_approval"
            message = (f"Amendment {amendment_id} is reviewed and approved. Pilot authorization required: "
                       "record amendment-approve to resume the frozen phase, or escalate it.")
        elif amendment_decision == "revision":
            kind = "amendment_revision"
            message = (f"Amendment {amendment_id} review requested changes. Pending decision: the Architect "
                       "revises it (amendment-revise) or escalates it to a full redesign.")
        else:
            kind = "amendment_review"
            message = (f"Amendment {amendment_id} is open and frozen. Pending decision: independent "
                       "amendment review (amendment-review), then Pilot approval.")
    elif budget_exhausted:
        kind = "design_review_budget"
        message = (f"Design review budget exhausted ({budget['attempts']} of {budget['limit']} attempts). "
                   "Authorize exactly one more design-review attempt, or leave the run held.")
    elif questions:
        # #48: one card per role naming the count, never one alert per
        # question; a lone question still shows its text on the banner.
        kind = "question"
        cards = lib.question_cards(status)
        message = " · ".join(f"{card['role'].capitalize()}: {card['count']} "
                             f"question{'' if card['count'] == 1 else 's'} waiting" for card in cards)
        if len(questions) == 1:
            message = f"{message}: {questions[0].get('text')}"
    elif approval_missing:
        kind = "deployment_approval"
        message = "Pilot authorization required: grant explicit deployment approval before live verification can continue."
    elif design_approval_missing:
        kind = "design_approval"
        message = "Independent design review is approved. Authorize this exact design to open Phase 3."
    elif workflow_status == "blocked":
        kind = "blocked"
        message = next_action
    else:
        kind = "decision"
        message = next_action
    return {"required": required, "kind": kind if required else None,
            "message": message if required else None,
            "request_id": regression.get("request_id") if regression else None,
            "amendment_id": amendment.get("amendment_id") if amendment else None,
            "amendment_decision": amendment_decision,
            "question_id": questions[0].get("question_id") if questions else None,
            "question_cards": lib.question_cards(status) if questions else []}


def _supervisor_briefing(status: dict, criteria: list[dict], errors: list[str],
                         audit_errors: list[str], stall: str | None, activity: str | None,
                         latest_event: dict | None, input_request: dict) -> dict:
    phase_number = int(status.get("phase_number", 1) or 1)
    phase = status.get("phase") or lib.PHASES.get(phase_number, "Unknown phase")
    progress = status.get("progress", 0)
    passing = sum(c.get("state") == "passing" for c in criteria)
    total = len(criteria)
    resolved = status.get("requirement_coverage", {}).get("original_symptom_resolved") is True
    all_errors = [*audit_errors, *errors]
    blocked = [c for c in criteria if c.get("state") == "blocked"]
    failing = [c for c in criteria if c.get("state") == "failing"]

    if input_request["required"]:
        tone = "critical"
        label = "Pilot authorization required"
        headline = "Holding position. Awaiting your command, Pilot."
        summary = input_request["message"]
    elif all_errors:
        tone = "critical"
        label = "Safety interlock"
        headline = "Tactical advance suspended, Pilot."
        summary = (f"The feature is at Phase {phase_number}, {phase}, with {progress}% reported progress. "
                   f"I found {len(all_errors)} condition{'s' if len(all_errors) != 1 else ''} that must be resolved before advancement.")
    elif stall:
        tone = "warning"
        label = "Telemetry interruption"
        headline = "Mission telemetry stalled. Signal has gone silent, Pilot."
        summary = (f"No unsafe transition has occurred. Work remains at Phase {phase_number}, {phase}, "
                   f"with {passing} of {total} acceptance criteria verified.")
    elif activity:
        tone = "steady"
        label = "Background operation"
        headline = "Systems active. Background sequence in progress, Pilot."
        summary = (f"{activity}. Work remains at Phase {phase_number}, {phase}, "
                   f"with {passing} of {total} acceptance criteria verified.")
    elif blocked or failing:
        tone = "warning"
        label = "Objectives unresolved"
        headline = "Holding trajectory until Mission Objectives are verified, Pilot."
        summary = (f"We are in Phase {phase_number}, {phase}, at {progress}%. "
                   f"{passing} of {total} criteria are passing; {len(failing)} are failing and {len(blocked)} are blocked.")
    elif phase_number == 8 and status.get("status") == "complete":
        tone = "success"
        label = "Mission complete"
        headline = "Objectives neutralized. Mission complete, Pilot."
        summary = (f"All {total} acceptance criteria are evidenced, the original symptom is resolved, "
                   "and the live verification gate has passed.")
    else:
        tone = "steady"
        label = "Trajectory stable"
        headline = f"System online. {phase} sequence active, Pilot."
        summary = (f"Progress is {progress}% with {passing} of {total} acceptance criteria verified. "
                   f"The original symptom is {'confirmed resolved' if resolved else 'not yet confirmed resolved'}.")

    attention = []
    if stall:
        attention.append(stall)
    attention.extend(all_errors[:4])
    for criterion in [*blocked, *failing]:
        if len(attention) >= 6:
            break
        attention.append(f"{criterion.get('id', 'Criterion')}: {criterion.get('requirement', 'Needs attention')}")
    if not attention and not resolved:
        attention.append("Original symptom still needs a successful evidence record.")

    completed = []
    if passing:
        completed.append(f"{passing} acceptance criterion{'s' if passing != 1 else ''} verified")
    if resolved:
        completed.append("Original symptom verified as resolved")
    if status.get("review"):
        completed.append("Independent review recorded")
    if status.get("deployment_approved"):
        completed.append("Deployment approval recorded")
    if status.get("live_verification_id"):
        completed.append("Live verification passed")

    return {
        "tone": tone,
        "label": label,
        "headline": headline,
        "summary": status.get("summary") or summary,
        "reassurance": status.get("reassurance") or "Reactor core stable. Safety interlocks active. I will advance only when every required gate is satisfied.",
        "next_action": input_request["message"] or status.get("next_action") or lib.NEXT_ACTION_DEFAULTS.get(phase_number, "Review the current state."),
        "attention": attention,
        "completed": completed,
        "latest_event": ({"kind": latest_event.get("kind"), "message": latest_event.get("message"),
                          "at": latest_event.get("at")} if latest_event else None),
    }


def build_snapshot(root: Path) -> dict:
    """Build one coherent dashboard snapshot while holding the project lock."""
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        cfg = lib.load_config(root)
    except lib.HandsoffError as exc:
        return {"initialized": False, "generated_at": generated_at, "root": str(root), "error": str(exc)}

    status_file = lib.status_path(root, cfg)
    acceptance_file = lib.acceptance_path(root, cfg)
    if not status_file.exists() or not acceptance_file.exists():
        return {
            "initialized": False,
            "generated_at": generated_at,
            "root": str(root),
            "error": "System online. No active Mission Objective was found in this project, Pilot.",
            "settings": _settings_view(cfg),
        }

    try:
        with lib.project_lock(root):
            status = lib.load_unique_json(status_file)
            acceptance = lib.load_unique_json(acceptance_file)
            verifications, verification_problems = lib.load_verifications(root, cfg)
            events = _read_events(root, cfg)
            gate_errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                             verification_problems=verification_problems)
            event_errors = lib.verify_event_log(root, cfg)
            actual_head = verifications[-1].get("hash") if verifications else "GENESIS"
            audit_errors = [*event_errors]
            if status.get("verification_head") != actual_head:
                audit_errors.append("Verification ledger tail does not match its anchored head.")
            # #41: one activity reading (output record read once) feeds the
            # stall warning, the activity note, and the live view, the same
            # function `status` prints, so CLI and dashboard cannot disagree.
            activity_view = lib.activity_view(status, cfg, root)
            stall = activity_view["stall_warning"]
            activity = activity_view["activity_note"]
            # #33: the live session view, from structured state plus the beacon.
            live = lib.live_status(status, cfg, root)
            # #38: states and hashes only; the bounded output stays in the side file.
            try:
                design_evidence = lib.design_evidence_view(root, cfg)
            except lib.HandsoffError as exc:
                design_evidence = [{"id": entry["id"], "state": "missing", "reasons": [str(exc)],
                                    "input_hash": None, "matched_files": None, "output_sha256": None,
                                    "at": None, "by": None, "head": None, "commit_matches_head": False,
                                    "truncated": False, "exit_code": None}
                                   for entry in cfg.get("design_evidence", [])]
            recovery_assessment = lib.recovery_assessment(
                status, cfg, lib.read_session_liveness(root), events,
            )
    except (lib.HandsoffError, OSError) as exc:
        return {"initialized": False, "generated_at": generated_at, "root": str(root), "error": str(exc)}

    criteria = acceptance.get("criteria", [])
    work_items = lib.derive_work_items(status, acceptance, cfg)
    latest_event = events[-1] if events else None
    coverage = status.get("requirement_coverage", {})
    design_review_budget = lib.design_review_budget(status, cfg)
    # #37: current (the latest recorded review's tier) and next (what the
    # next launch selects, with any refusal named) reviewer profile.
    design_reviewer_selection = lib.design_reviewer_selection_view(cfg, status, acceptance)
    audit_healthy = not gate_errors and not audit_errors
    input_request = _input_request(status, cfg)
    display_status = dict(status)
    display_status["phase"] = _display_phase_name(status)
    actors = {
        "architect": ((status.get("design_review") or {}).get("architect")
                      or (status.get("design_approved") or {}).get("architect")),
        "design_reviewed_by": (status.get("design_review") or {}).get("by"),
        "implemented_by": status.get("implemented_by"),
        "reviewed_by": status.get("reviewed_by"),
        "approved_by": (status.get("deployment_approved") or {}).get("by"),
        "active_role": _active_role(status, input_request),
    }
    sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}

    def session_view(session):
        if not isinstance(session, dict):
            return None
        fields = ("session_id", "role", "actor", "adapter", "requested_model",
                  "reported_model", "resolution_source", "state", "started_at",
                  "running_at", "ended_at", "exit_code", "tier")
        return {field: session.get(field) for field in fields}

    def session_for_actor(actor):
        if not isinstance(actor, str) or not actor.strip():
            return None
        identity = actor.strip().casefold()
        matches = [session for session in sessions.values()
                   if isinstance(session, dict)
                   and isinstance(session.get("actor"), str)
                   and session["actor"].strip().casefold() == identity]
        return session_view(matches[-1]) if matches else None

    current_sessions = lib.current_agent_sessions(status)
    supervisor_session = current_sessions.get("supervisor")

    def crew_entry(key, label, actor, live_session=None):
        # A recorded decision names the actor once it lands; until then the
        # role's current managed session (an Architect mid-design, an
        # Implementer mid-build) is the truthful occupant of the station.
        session = session_for_actor(actor)
        if actor is None and isinstance(live_session, dict):
            actor = live_session.get("actor")
            session = session_view(live_session)
        return {"key": key, "label": label, "actor": actor, "session": session}

    # One managed "reviewer" role serves two stations: in Phase 2 a live
    # reviewer session is critiquing the design, from Phase 5 on it is
    # reviewing the implementation. Route it to the matching row only.
    phase_number = int(status.get("phase_number", 1) or 1)
    live_reviewer = current_sessions.get("reviewer")
    design_reviewer_live = live_reviewer if phase_number <= 2 else None
    implementation_reviewer_live = live_reviewer if phase_number >= 5 else None
    crew = [
        crew_entry("architect", "ARCHITECT", actors["architect"], current_sessions.get("architect")),
        crew_entry("design_reviewer", "DESIGN REVIEWER", actors["design_reviewed_by"], design_reviewer_live),
        {"key": "supervisor", "label": "SUPERVISOR",
         "actor": supervisor_session.get("actor") if isinstance(supervisor_session, dict) else None,
         "session": session_view(supervisor_session)},
        crew_entry("implementer", "IMPLEMENTER", actors["implemented_by"], current_sessions.get("implementer")),
        crew_entry("reviewer", "REVIEWER", actors["reviewed_by"], implementation_reviewer_live),
        {"key": "approver", "label": "APPROVER", "actor": actors["approved_by"], "session": None},
    ]
    replacements = []
    for replacement in (status.get("agent_replacements") or [])[-8:]:
        if not isinstance(replacement, dict):
            continue
        fields = ("replacement_id", "role", "from_session_id", "to_session_id", "trigger",
                  "category", "reason", "attempt", "cap", "action", "planner_reason",
                  "selected_profile", "state", "at", "ended_at")
        item = {field: replacement.get(field) for field in fields}
        item["from_profile"] = session_view(sessions.get(replacement.get("from_session_id")))
        item["to_profile"] = session_view(sessions.get(replacement.get("to_session_id")))
        replacements.append(item)
    return {
        "initialized": True,
        "generated_at": generated_at,
        "root": str(root),
        "project": {"name": root.name, "feature": status.get("feature", acceptance.get("feature", "Untitled feature"))},
        "status": display_status,
        "phases": _phase_view(
            int(status.get("phase_number", 1) or 1),
            status.get("status") == "complete",
            display_status["phase"],
        ),
        "acceptance": {
            "criteria": criteria,
            "passing": coverage.get("passing", 0),
            "failing": coverage.get("failing", 0),
            "not_tested": coverage.get("not_tested", 0),
            "blocked": coverage.get("blocked", 0),
            "total": len(criteria),
            "original_symptom_resolved": coverage.get("original_symptom_resolved") is True,
        },
        "tickets": [],
        "work_items": work_items,
        "design_evidence": design_evidence,
        # #36: counts and flags of the latest delta packet, never finding text.
        "design_review_packet": lib.design_review_packet_summary(status),
        # #42: the open amendment (ids, hashes, decisions), never criterion text.
        "amendment": lib.amendment_view(status, acceptance, cfg, verifications),
        "questions": lib.questions_view(status),
        "actors": actors,
        "crew": crew,
        "runtime": {
            "current_sessions": current_sessions,
            "replacements": replacements,
        },
        "audit": {
            "healthy": audit_healthy,
            "gate_errors": gate_errors,
            "chain_errors": audit_errors,
            "verification_runs": len(verifications),
            "event_count": len(events),
            "verification_head": status.get("verification_head"),
        },
        "policy": {
            "review_round": status.get("review_round", 0),
            "max_review_rounds": cfg.get("max_review_rounds"),
            "effective_max_review_rounds": lib.effective_review_cap(status, cfg)
            if "review_attempts" in status else cfg.get("max_review_rounds"),
            "review_cap_overrides": len(status.get("review_cap_overrides") or []),
            "design_round": status.get("design_round", 0),
            "max_design_rounds": cfg.get("max_design_rounds"),
            "design_review_attempts": design_review_budget["attempts"],
            "max_autonomous_design_reviews": design_review_budget["limit"],
            "design_review_authorization": status.get("design_review_authorization"),
            "design_reviewer_selection": design_reviewer_selection,
            "design_reviewer_escalation": status.get("design_reviewer_escalation"),
            "explicit_approval": cfg.get("deployment_requires_explicit_approval", True),
            "live_verification": cfg.get("require_live_verification", True),
            "configured_checks": len(cfg.get("check_commands", [])),
            "configured_live_checks": len(cfg.get("live_check_commands", [])),
        },
        "review": {
            "attempts": [{
                "attempt": item.get("attempt"), "attempt_id": item.get("attempt_id"),
                "trigger": item.get("trigger"), "disposition": item.get("disposition"),
                "reviewer": item.get("reviewer"), "opened_at": item.get("opened_at"),
                "closed_at": item.get("closed_at"), "findings_count": len(item.get("findings") or []),
            } for item in (status.get("review_attempts") or [])],
        },
        "recovery": {
            "assessment": recovery_assessment,
            "lease": status.get("recovery_lease"),
            "attempts": list((status.get("recovery_attempts") or [])[-8:]),
            "cap": cfg.get("recovery", {}).get("max_attempts", 0),
            "watchdog_enabled": cfg.get("recovery", {}).get("dashboard_watchdog", False),
        },
        "regression": {
            "pending": next((item for item in reversed(status.get("regression_requests") or [])
                             if item.get("state") == "awaiting_approval"), None),
            "current": lib.active_regression_request(status),
            "last": next((item for item in reversed(status.get("regression_requests") or [])
                          if item.get("state") not in {"awaiting_approval", "accepted", "launched"}), None),
            "history": list(reversed((status.get("regression_requests") or [])[-8:])),
        },
        "escalation": status.get("escalation"),
        "settings": _settings_view(cfg),
        "input_required": input_request,
        "activity_note": activity,
        "activity": activity_view,
        "live": live,
        "supervisor": _supervisor_briefing(display_status, criteria, gate_errors, audit_errors, stall, activity,
                                            latest_event, input_request),
        "events": list(reversed(events[-12:])),
        # #43: every entry carries executed/reused_from, null on a legacy
        # record written before the verification cache existed.
        "verifications": [{**record, "executed": record.get("executed"), "reused_from": record.get("reused_from")}
                          for record in reversed(verifications[-8:])],
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path, *,
                 run_token: str | None = None, root_sha256: str | None = None):
        self.project_root = root
        # #40: an owned server keeps its own ownership values in memory
        # from launch; the pointer file on disk is never consulted to
        # answer /api/ownership or to authorize /api/shutdown.
        self.run_token = run_token
        self.root_sha256 = root_sha256
        self.stopping = False
        self._stop_lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        super().__init__(address, DashboardHandler)
        cfg = lib.load_config(root)
        recovery = cfg.get("recovery", {})
        if recovery.get("enabled") and recovery.get("dashboard_watchdog"):
            threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def _watchdog_loop(self):
        cfg = lib.load_config(self.project_root)
        interval = cfg.get("recovery", {}).get("poll_seconds", 30)
        while not self._watchdog_stop.wait(interval):
            try:
                def launcher(role):
                    import handsoff_agent
                    task = (f"Resume trusted Handsoff state as {role}; read status, acceptance, and event log "
                            "and continue without repeating evidenced work.")
                    spec = handsoff_agent.build_launch_spec(self.project_root, role, task)
                    return handsoff_agent.execute_with_recovery(spec)
                lib.recover_run(self.project_root, actor="Mission Control Watchdog", launcher=launcher)
            except Exception as exc:
                print(f"HANDSOFF_WATCHDOG_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)

    def server_close(self):
        self._watchdog_stop.set()
        super().server_close()

    @property
    def owned_by_run(self) -> bool:
        return bool(self.run_token) and bool(self.root_sha256)

    def ownership_view(self) -> dict:
        if not self.owned_by_run:
            return {"owned": False}
        return {"owned": True, "run_token": self.run_token, "root_sha256": self.root_sha256}

    def shutdown_matches(self, run_token, root_sha256) -> bool:
        """Both bindings must match the in-memory values; constant-time
        comparison so a wrong token learns nothing from timing."""
        if not self.owned_by_run:
            return False
        if not isinstance(run_token, str) or not isinstance(root_sha256, str):
            return False
        return (hmac.compare_digest(run_token.encode("utf-8"), self.run_token.encode("utf-8"))
                and hmac.compare_digest(root_sha256.encode("utf-8"), self.root_sha256.encode("utf-8")))

    def request_stop(self) -> None:
        """Flag every SSE loop to exit, then run shutdown() off the handler
        thread (shutdown() blocks until serve_forever returns, and the
        handler thread is one of the things serve_forever is waiting on).
        Idempotent: a second request while stopping starts nothing new."""
        with self._stop_lock:
            if self.stopping:
                return
            self.stopping = True
        threading.Thread(target=self.shutdown, name="handsoff-dashboard-stop", daemon=True).start()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        self.end_headers()

    def _sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        self.end_headers()

    def _json_response(self, status: HTTPStatus, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _same_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and port == self.server.server_port
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )

    def _send_event(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    def _serve_events(self) -> None:
        self._sse_headers()
        signature = _artifact_signature(self.server.project_root)
        last_keepalive = time.monotonic()
        self.wfile.write(b"retry: 1000\n")
        self._send_event("ready", {"connected": True})
        while not self.server.stopping:
            time.sleep(0.2)
            if self.server.stopping:
                break
            current = _artifact_signature(self.server.project_root)
            if current != signature:
                signature = current
                self._send_event("invalidate", {"changed": True})
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 15:
                self.wfile.write(b": telemetry keepalive\n\n")
                self.wfile.flush()
                last_keepalive = time.monotonic()
        # #40: the stream has no length, so the client only learns it is
        # over when the connection closes; make handle() drop it instead
        # of waiting for a next request that will never come.
        self.close_connection = True

    def _serve_shutdown(self) -> None:
        """`POST /api/shutdown` for the CLI release path (#40). Not a
        browser endpoint, so no same-origin header check: the run_token
        and root_sha256 in the body are its authentication, and both are
        compared against the server's own in-memory values."""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 1:
            self._json_response(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "A shutdown request body is required"})
            return
        if length > MAX_SETTINGS_BODY:
            self._json_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Shutdown request is too large"})
            return
        try:
            requested = _strict_json_object(self.rfile.read(length))
        except lib.HandsoffError as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if not self.server.owned_by_run:
            self._json_response(HTTPStatus.FORBIDDEN, {"ok": False, "error": "This dashboard is not run-owned"})
            return
        if not self.server.shutdown_matches(requested.get("run_token"), requested.get("root_sha256")):
            self._json_response(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Ownership mismatch"})
            return
        self.server.request_stop()
        self._json_response(HTTPStatus.OK, {"ok": True, "stopping": True, "port": self.server.server_port})

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/dashboard":
            payload = json.dumps(build_snapshot(self.server.project_root), separators=(",", ":")).encode("utf-8")
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if path == "/api/events":
            try:
                self._serve_events()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/api/ownership":
            self._json_response(HTTPStatus.OK, self.server.ownership_view())
            return
        if path == "/healthz":
            payload = b'{"ok":true}'
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        asset = ASSETS.get(path)
        if asset:
            asset_path = ASSET_ROOT / asset[0]
            try:
                payload = asset_path.read_bytes()
            except OSError:
                payload = b"Dashboard assets are missing. Copy the dashboard/ directory beside bin/."
                self._headers(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            self._headers(HTTPStatus.OK, asset[1], len(payload))
            self.wfile.write(payload)
            return
        payload = b"Not found"
        self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/shutdown":
            self._serve_shutdown()
            return
        if path not in {"/api/settings/agents", "/api/design-approval", "/api/deployment-approval",
                        "/api/regression-decision", "/api/question-answer", "/api/question-answers",
                        "/api/design-review-authorize"}:
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if not self._same_origin_allowed():
            self._json_response(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Same-origin dashboard request required"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 1:
            self._json_response(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "A settings request body is required"})
            return
        body_cap = MAX_QUESTION_BATCH_BODY if path == "/api/question-answers" else MAX_SETTINGS_BODY
        if length > body_cap:
            self._json_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Settings request is too large"})
            return
        try:
            requested = _strict_json_object(self.rfile.read(length))
            if path == "/api/regression-decision":
                if set(requested) != {"request_id", "decision", "command_hash"} \
                        or requested.get("decision") not in {"accept", "decline"} \
                        or not isinstance(requested.get("request_id"), str) \
                        or not isinstance(requested.get("command_hash"), str):
                    raise lib.HandsoffError("regression decision requires request_id, command_hash, and accept/decline")
                snapshot = build_snapshot(self.server.project_root)
                pending = (snapshot.get("regression") or {}).get("pending") or {}
                if pending.get("request_id") != requested["request_id"] \
                        or pending.get("command_sha256") != requested["command_hash"]:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The displayed regression request is stale"})
                    return
                command = argparse.Namespace(
                    root=str(self.server.project_root), request_id=requested["request_id"],
                    by="Mission Control Pilot", accept=requested["decision"] == "accept",
                    decline=requested["decision"] == "decline",
                )
                if supervisor.cmd_regression_decide(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The regression gate rejected this decision"})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True, "decision": requested["decision"]})
                return
            if path == "/api/question-answer":
                # #46: the same lock-protected, audited path as the CLI; the
                # actor is the dashboard's Pilot identity, like Authorize.
                if set(requested) != {"question_id", "text"} \
                        or not isinstance(requested.get("question_id"), str) \
                        or not isinstance(requested.get("text"), str):
                    raise lib.HandsoffError("question answer requires question_id and text")
                record = lib.answer_question(self.server.project_root, question_id=requested["question_id"],
                                             by="Mission Control Pilot", text=requested["text"])
                self._json_response(HTTPStatus.OK, {"ok": True, "question_id": record["question_id"]})
                return
            if path == "/api/question-answers":
                # #48: one form per role sends its answers as one batch; a
                # shape error is 400, a state conflict (unknown, answered,
                # choice not offered) is 409, and either writes nothing.
                try:
                    records = lib.answer_questions_batch(
                        self.server.project_root, answers=lib.question_answers_from_payload(requested),
                        by="Mission Control Pilot")
                except lib.QuestionAnswerConflict as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True, "question_ids": [r["question_id"] for r in records]})
                return
            if path == "/api/design-approval":
                if requested:
                    raise lib.HandsoffError("design approval payload must be empty")
                snapshot = build_snapshot(self.server.project_root)
                input_request = snapshot.get("input_required") or {}
                review = (snapshot.get("status") or {}).get("design_review") or {}
                architect = review.get("architect")
                if input_request.get("kind") != "design_approval" or not architect:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The current mission is not awaiting design authorization"},
                    )
                    return
                command = argparse.Namespace(
                    root=str(self.server.project_root),
                    by="Mission Control Pilot",
                    architect=architect,
                    summary="Pilot authorized the independently reviewed design in Mission Control.",
                    redesigns_settled_work=None,
                )
                if supervisor.cmd_design_approve(command) != 0:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The design approval gate rejected this authorization"},
                    )
                    return
                approved = lib.load_unique_json(lib.status_path(
                    self.server.project_root, lib.load_config(self.server.project_root)
                )).get("design_approved")
                self._json_response(HTTPStatus.OK, {"ok": True, "design_approved": approved})
                return
            if path == "/api/design-review-authorize":
                if requested:
                    raise lib.HandsoffError("design review authorization payload must be empty")
                snapshot = build_snapshot(self.server.project_root)
                if (snapshot.get("input_required") or {}).get("kind") != "design_review_budget":
                    self._json_response(HTTPStatus.CONFLICT, {
                        "ok": False, "error": "The current mission is not waiting on a design-review authorization"})
                    return
                command = argparse.Namespace(root=str(self.server.project_root), by="Mission Control Pilot",
                                             note="Authorized from Mission Control")
                if supervisor.cmd_design_review_authorize(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The design-review budget rejected this authorization"})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/deployment-approval":
                if requested:
                    raise lib.HandsoffError("deployment approval payload must be empty")
                snapshot = build_snapshot(self.server.project_root)
                input_request = snapshot.get("input_required") or {}
                if input_request.get("kind") != "deployment_approval":
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The current mission is not awaiting deployment authorization"},
                    )
                    return
                command = argparse.Namespace(
                    root=str(self.server.project_root), approve=True, by="Mission Control Pilot",
                )
                if supervisor.cmd_deployment_gate(command) != 0:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The deployment gate rejected this authorization"},
                    )
                    return
                approved = lib.load_unique_json(lib.status_path(
                    self.server.project_root, lib.load_config(self.server.project_root)
                )).get("deployment_approved")
                self._json_response(HTTPStatus.OK, {"ok": True, "deployment_approved": approved})
                return
            wrapped = set(requested) == {"profiles", "fallbacks", "max_failovers_per_role"}
            if wrapped:
                lib.update_agent_settings(self.server.project_root, requested)
            else:
                lib.update_agent_config(self.server.project_root, requested)
            effective_cfg = lib.load_config(self.server.project_root)
        except lib.HandsoffError as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except OSError:
            self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Agent settings could not be saved"})
            return
        response = _settings_view(effective_cfg)
        if not wrapped:
            response["agents"] = {role: effective_cfg["agents"][role] for role in requested}
        self._json_response(HTTPStatus.OK, {"ok": True, **response})


def _owner_feature(root: Path) -> str | None:
    """The feature name for the pointer file, or None when there is no
    readable run yet. Informational only; nothing checks it."""
    try:
        cfg = lib.load_config(root)
        status_file = lib.status_path(root, cfg)
        if not status_file.exists():
            return None
        feature = lib.load_unique_json(status_file).get("feature")
    except (lib.HandsoffError, OSError):
        return None
    return feature if isinstance(feature, str) else None


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          owned_by_run: bool = False) -> int:
    if host not in lib.DASHBOARD_LOOPBACK_HOSTS:
        raise lib.HandsoffError("dashboard binds to localhost only; use an authenticated reverse proxy for remote access")
    if not ASSET_ROOT.is_dir():
        raise lib.HandsoffError(f"dashboard assets are missing at {ASSET_ROOT}")
    # #40: only a server launched with --owned-by-run mints ownership
    # values; without the flag nothing is written and no release call can
    # ever target this server.
    run_token = lib.new_dashboard_run_token() if owned_by_run else None
    root_sha256 = lib.dashboard_root_sha256(root) if owned_by_run else None
    server = DashboardServer((host, port), root, run_token=run_token, root_sha256=root_sha256)
    actual_host, actual_port = server.server_address[:2]
    browser_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    url = f"http://{browser_host}:{actual_port}/"
    print(f"HANDSOFF_DASHBOARD: {url}")
    print(f"PROJECT_ROOT: {root}")
    if owned_by_run:
        owner_path = lib.write_dashboard_owner(
            root, pid=os.getpid(), host=host, port=actual_port, run_token=run_token,
            root_sha256=root_sha256, feature=_owner_feature(root),
        )
        print(f"HANDSOFF_DASHBOARD_OWNER: {owner_path}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nHANDSOFF_DASHBOARD_STOPPED")
    finally:
        server.server_close()
        if owned_by_run:
            # Only while the file still carries this server's token; a
            # different token means a newer server on this root owns it.
            try:
                lib.remove_dashboard_owner_if_token(root, run_token)
            except OSError:
                pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Handsoff local dashboard")
    parser.add_argument("--root", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--owned-by-run", action="store_true",
                        help="mark this server as owned by the current run so completion releases its port")
    args = parser.parse_args()
    try:
        return serve(lib.resolve_root(args.root), args.host, args.port, not args.no_open,
                     owned_by_run=args.owned_by_run)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
