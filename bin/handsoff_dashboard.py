#!/usr/bin/env python3
"""Local dashboard for Project Handsoff.

Most workflow artifacts remain read-only. Narrow same-origin endpoints exist
for allowlisted Agent Settings and the guarded design/deployment approval
commands exposed by the supervisor CLI.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import hmac
import io
import json
import re
import os
import subprocess
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
import handsoff_progress as test_progress  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402
import handsoff_tranche as tranche  # noqa: E402


ASSET_ROOT = lib.engine_root() / "dashboard"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/regression": ("regression.html", "text/html; charset=utf-8"),
    "/regression.js": ("regression.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/lib/dashboard-logic.js": ("lib/dashboard-logic.js", "text/javascript; charset=utf-8"),
    "/lib/run-vocabulary.js": ("lib/run-vocabulary.js", "text/javascript; charset=utf-8"),  # #218
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
}
MAX_SETTINGS_BODY = 16 * 1024
# #48: sixteen answers of up to 1024 characters each, plus envelope.
MAX_QUESTION_BATCH_BODY = 64 * 1024

# Dashboard requests are intentionally tracked in memory as well as by the
# CLI's lock files.  The map lets a running Pilot operation be displayed
# without exposing subprocess output or creating a second evidence channel.
_VERIFY_RUNS: dict[str, dict] = {}
_VERIFY_RUNS_LOCK = threading.Lock()


def _live_verification_in_flight(root: Path, cfg: dict) -> dict | None:
    """#148: the transient record verify-live keeps while it runs, or None.
    A record older than the checks timeout times its command count is a
    leftover from a crashed run and is ignored."""
    path = root / lib.LIVE_INFLIGHT_FILE
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        total = int(payload.get("total") or 0)
        done = int(payload.get("done") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or age > int(cfg.get("check_timeout_seconds", 600) or 600) * total:
        return None
    current = payload.get("current")
    return {"done": min(done, total), "total": total,
            "current": current if isinstance(current, str) else None,
            "started_at": payload.get("started_at") if isinstance(payload.get("started_at"), str) else None,
            "by": payload.get("by") if isinstance(payload.get("by"), str) else None}


def _live_verification_last_failure(records: list[dict]) -> dict | None:
    """#148: the failing command of the newest live record, until a later
    live record passes. Tails are the ledger's redacted, bounded tails."""
    for record in reversed(records):
        if record.get("kind") != "live":
            continue
        if record.get("ok") is True:
            return None
        failed = [item for item in (record.get("results") or [])
                  if isinstance(item, dict) and item.get("exit_code") not in (0, None)]
        if not failed:
            return {"command": None, "exit_code": None, "output_tail": "", "at": record.get("at"),
                    "run_id": record.get("run_id")}
        item = failed[0]
        return {"command": item.get("command"), "exit_code": item.get("exit_code"),
                "output_tail": str(item.get("output_tail") or "")[-lib.CHECK_OUTPUT_TAIL_CHARS:],
                "at": record.get("at"), "run_id": record.get("run_id")}
    return None


def _verification_view(root: Path, cfg: dict, records: list[dict]) -> dict:
    """Expose only bounded verification state, never check output."""
    inflight = list(lib.verify_inflight_bindings(root))
    with _VERIFY_RUNS_LOCK:
        for item in _VERIFY_RUNS.values():
            if item.get("root") == str(root):
                inflight.append(item["command"])
    latest = {}
    for record in records:
        if record.get("kind") != "checks":
            continue
        for criterion in record.get("criteria", []):
            latest.setdefault(criterion, {"ok": record.get("ok"), "at": record.get("at"),
                                          "run_id": record.get("run_id")})
    newest = next((r for r in reversed(records) if r.get("kind") == "live"), None)
    live = {"ok": newest.get("ok") if newest else None, "at": newest.get("at") if newest else None,
            "run_id": newest.get("run_id") if newest else None,
            "in_flight": _live_verification_in_flight(root, cfg),
            "last_failure": _live_verification_last_failure(records)}
    return {"in_flight": sorted(set(inflight)), "latest": latest, "live": live}


UNKNOWN_ENGINE = {"version": "unknown", "source": "unknown", "source_root": None,
                  "compatibility": None, "manifest_sha256": None}


def _engine_identity(root: Path) -> tuple[dict, str | None]:
    """#185: the engine identity for a read path. A missing or mismatched
    version pin is one line of data (the badge reads UNKNOWN and the audit
    strip carries the reason), never a failed snapshot: the pin is a side
    file the operator owns, and a page that dies on it hides every other
    fact about the run."""
    try:
        return lib.runtime_identity(root), None
    except (lib.HandsoffError, OSError) as exc:
        return {**UNKNOWN_ENGINE, "reason": str(exc)}, str(exc)


def _engine_view(root: Path) -> dict:
    """Render safe CLI forms and catch preview failures as data."""
    identity, engine_error = _engine_identity(root)
    version = identity["version"]
    # #184: the run page keeps the version line and the command list; the
    # upgrade and migrate previews and the permanent "execution is not
    # offered" line said nothing about the run.
    root_text = str(root)
    commands = {
        "install": f"handsoff init {root_text}",
        "upgrade_preview": f"handsoff upgrade {root_text} --to {version} --dry-run",
        "upgrade": f"handsoff upgrade {root_text} --to {version}",
        "rollback_preview": f"handsoff rollback {root_text} --dry-run",
        "rollback": f"handsoff rollback {root_text}",
        "migrate_preview": f"handsoff migrate {root_text} --dry-run",
        "migrate": f"handsoff migrate {root_text}",
        "doctor": f"handsoff doctor {root_text}",
    }
    return {**{key: identity.get(key) for key in ("version", "source", "source_root", "compatibility")},
            "pin": identity.get("compatibility"), "reason": engine_error, "commands": commands,
            "execution": "unavailable",
            "execution_reason": "execution is not offered while a dashboard is serving this root"}


def _start_verification(root: Path, kind: str, criteria: list[str] | None = None) -> None:
    """Run the canonical supervisor command asynchronously and retain its exit state."""
    started_at = datetime.now(timezone.utc).isoformat()
    command = [sys.executable, str(Path(__file__).with_name("handsoff_supervisor.py")),
               "--root", str(root)]
    if kind == "verify":
        command += ["verify"]
        for criterion in criteria or []:
            command += ["--criterion", criterion]
    else:
        command += ["verify-live"]
    command += ["--by", "Mission Control Pilot"]
    key = started_at + ":" + kind
    with _VERIFY_RUNS_LOCK:
        _VERIFY_RUNS[key] = {"root": str(root), "command": " ".join(command), "state": "running"}
    def worker():
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    check=False)
            exit_code = result.returncode
        except OSError:
            exit_code = 1
        with _VERIFY_RUNS_LOCK:
            if key in _VERIFY_RUNS:
                _VERIFY_RUNS[key]["state"] = f"finished exit {exit_code}"
    threading.Thread(target=worker, daemon=True).start()


def _settings_view(cfg: dict) -> dict:
    effective_profiles = lib.resolved_agent_profiles(cfg)
    return {
        # #165 #167 #166: the workflow switches, effective values with defaults.
        "features": lib.features_view(cfg),
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
        "allowed_adapters_by_role": {
            role: list(lib.AGENT_SETTING_ADAPTERS) + ([lib.HOST_AGENT_ADAPTER] if role in lib.HOST_CAPABLE_ROLES else [])
            for role in lib.SELECTABLE_AGENT_ROLES
        },
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


def _regression_progress(root: Path, request: dict | None) -> dict | None:
    """Return side telemetry only when it belongs to the displayed request.

    The progress file is generated state, never authorization or evidence.
    Matching both identities here prevents a prior run from appearing under
    a newer accepted request even if a browser refresh races file creation.
    """
    if not isinstance(request, dict):
        return None
    path = root / ".handsoff-regression.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) \
            or payload.get("request_id") != request.get("request_id") \
            or payload.get("command_sha256") != request.get("command_sha256"):
        return None
    return payload


def _test_progress(root: Path, status: dict, events: list[dict], request: dict | None,
                   ci: dict | None, legacy_regression: dict | None = None) -> dict | None:
    """Choose the newest exact live execution across local checks and CI."""
    run_id = lib.feature_hash(status, events)
    local = test_progress.read(root, run_id=run_id)
    if local and local.get("source") == "regression":
        if not isinstance(request, dict) \
                or local.get("request_id") != request.get("request_id") \
                or local.get("command_sha256") != request.get("command_sha256"):
            local = None
    external = test_progress.from_ci(run_id, ci) if isinstance(ci, dict) else None
    legacy = None
    if isinstance(legacy_regression, dict):
        legacy_payload = dict(legacy_regression)
        if not legacy_payload.get("heartbeat_at"):
            try:
                legacy_payload["heartbeat_at"] = datetime.fromtimestamp(
                    (root / ".handsoff-regression.json").stat().st_mtime,
                    timezone.utc,
                ).isoformat()
            except OSError:
                pass
        legacy = test_progress.from_legacy_regression(run_id, legacy_payload)
    candidates = [item for item in (local, legacy, external) if item]
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item.get("started_at") or ""))


def _legacy_regression_fresh(root: Path) -> bool:
    path = root / ".handsoff-regression.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["heartbeat_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError):
        return False
    return test_progress.from_legacy_regression("sse-freshness", payload) is not None


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
                # #58: every bounded log append invalidates Mission Control.
                lib.agent_output_path(root),
                lib.operations_path(root),
                root / lib.PREFLIGHT_FILE,
                root / ".handsoff-regression.json",
                root / test_progress.PROGRESS_FILE,
                root / tranche.PROPOSAL_FILE,
            ]
        except lib.HandsoffError:
            paths = [root / "handsoff.toml", lib.live_beacon_path(root),
                     lib.output_liveness_path(root), lib.agent_output_path(root), lib.operations_path(root)]
        signature = []
        for path in paths:
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(path), -1, -1))
        # Freshness itself is observable state. This flips when a silent
        # execution reaches 30 seconds (or a terminal summary reaches ten
        # minutes), so SSE invalidates without requiring another file write.
        signature.append(("__test_progress_fresh__", 1 if test_progress.read(root) else 0, 0))
        signature.append(("__legacy_regression_fresh__", 1 if _legacy_regression_fresh(root) else 0, 0))
        return tuple(signature)


def _phase_view(current: int, run_complete: bool, current_name: str | None = None, closed: bool = False,
                lane: str | None = None, phases_run: list[int] | None = None,
                phases_waived: list[int] | None = None) -> list[dict]:
    """The current phase renders "active" (the pulsing in-progress bar) only
    while the run is still moving. Once status is complete, phase 8 being
    "current" no longer means "in progress", so it renders solid-complete
    like every phase before it instead of blinking forever.
    """
    # Snapshot contract choice (a): every snapshot carries all eight phases.
    # Lane phases are marked below, so waived work remains visible instead of
    # being silently dropped from the strip.
    waived = set(phases_waived or [])
    return [
        {
            "number": number,
            "name": current_name if number == current and current_name else name,
            "lane_status": ("waived" if lane and lane != "full" and number in waived else "run"),
            "state": ("closed" if closed and number == current
                      else "complete" if number < current or (number == current and run_complete)
                      else "active" if number == current else "upcoming"),
        }
        for number, name in lib.PHASES.items()
    ]


def _display_phase_name(status: dict, verification_live: dict | None = None) -> str:
    """Never describe an accepted deployment authorization as still awaiting
    it, and (#148) never call a run ready to ship while its live
    verification is running or after the newest one failed."""
    phase_number = int(status.get("phase_number", 1) or 1)
    live = verification_live or {}
    if phase_number == 7 and live.get("in_flight"):
        flight = live["in_flight"]
        return f"LIVE VERIFICATION RUNNING · {flight.get('done', 0)}/{flight.get('total', 0)}"
    if phase_number == 7 and live.get("last_failure"):
        failure = live["last_failure"]
        return f"LIVE VERIFICATION FAILED · {failure.get('command')} exit {failure.get('exit_code')}"
    if phase_number == 7 and status.get("deployment_approved"):
        return "Deployment authorized · ready to ship"
    return status.get("phase") or lib.PHASES.get(phase_number, "Unknown phase")


_PHASE_PROGRESS_FLOORS = {1: 0, 2: 20, 3: 30, 4: 40, 5: 50, 6: 75, 7: 90, 8: 95}


def _display_progress(status: dict) -> int:
    """Show mission advancement without mislabeling it as evidence coverage.

    The persisted ``progress`` value is a delivery/evidence score and can
    correctly remain zero during design. Mission Control's primary gauge is
    phase progress, so it receives a phase floor while the raw score remains
    available as ``verification_progress``.
    """
    if status.get("status") == "complete":
        return 100
    phase_number = int(status.get("phase_number", 1) or 1)
    raw = int(float(status.get("progress", 0) or 0))
    return min(100, max(raw, _PHASE_PROGRESS_FLOORS.get(phase_number, 0)))


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


#: #147: whose turn a paused run waits on, by the kind of pause. The Pilot's
#: turn is the only one that may interrupt them.
DECISION_TURNS = {
    "design_approval": "pilot", "deployment_approval": "pilot", "amendment_approval": "pilot",
    "regression_approval": "pilot", "design_review_budget": "pilot", "question": "pilot",
    "escalation": "pilot",
    "amendment_review": "reviewer",
    "amendment_revision": "architect",
    "evidence_drift": "supervisor", "blocked": "supervisor", "decision": "supervisor",
}
PREAUTHORIZATION_PATTERN = re.compile(r"pre-?authori[sz]", re.IGNORECASE)
PREAUTHORIZATION_EXCERPT_CHARS = 160


def _decision_turn(kind: str | None) -> str | None:
    return DECISION_TURNS.get(kind or "", "supervisor") if kind else None


def _standing_preauthorization(root: Path | None, cfg: dict) -> dict | None:
    """#147: the newest pilot_note by a human on this run whose text states
    a standing pre-authorization; None otherwise. Managed roles (actor
    names carrying a role name) cannot pre-authorize anything."""
    if root is None:
        return None
    try:
        events = _read_events(root, cfg)
    except (OSError, lib.HandsoffError):
        return None
    for event in reversed(events):
        if event.get("kind") != "pilot_note":
            continue
        by = str(event.get("by") or "").strip()
        text = str(event.get("text") or "")
        lowered = by.casefold()
        if not by or any(role in lowered for role in lib.SELECTABLE_AGENT_ROLES) or lowered == "supervisor":
            continue
        if not PREAUTHORIZATION_PATTERN.search(text):
            continue
        return {"by": by, "at": event.get("at"), "excerpt": " ".join(text.split())[:PREAUTHORIZATION_EXCERPT_CHARS]}
    return None


def _amendment_round(root: Path | None, cfg: dict, amendment: dict) -> int:
    """1 plus the number of revisions recorded for the open amendment."""
    if root is None:
        return 1
    try:
        events = _read_events(root, cfg)
    except (OSError, lib.HandsoffError):
        return 1
    revisions = sum(1 for e in events if e.get("kind") == "amendment_revised"
                    and e.get("amendment_id") == amendment.get("amendment_id"))
    return 1 + revisions


def _decision_headline(input_request: dict) -> tuple[str, str]:
    """#147: the briefing label and headline by whose turn it is."""
    turn = input_request.get("turn")
    preauthorized = input_request.get("preauthorized") if turn == "pilot" else None
    if preauthorized:
        stamp = str(preauthorized.get("at") or "")[11:16]
        return ("Pre-authorized by pilot note",
                f"Pre-authorized by pilot note at {stamp} UTC; the Supervisor records the approval.")
    if turn == "reviewer":
        amendment_id = input_request.get("amendment_id")
        round_number = input_request.get("amendment_round") or 1
        target = f"amendment {amendment_id} (round {round_number})" if amendment_id else "the change"
        return ("Under independent review", f"Under independent review: {target}. Nothing waits on you, Pilot.")
    if turn == "architect":
        return ("Architect revising", "Architect revising the amendment. Nothing waits on you, Pilot.")
    if turn == "supervisor":
        return ("Supervisor working", "The Supervisor is clearing this hold. Nothing waits on you, Pilot.")
    return ("Pilot approval needed", "Holding position. Awaiting your command, Pilot.")


def _input_request(status: dict, cfg: dict, root: Path | None = None,
                   acceptance: dict | None = None,
                   verifications: list[dict] | None = None) -> dict:
    """Translate an explicit workflow pause into a dashboard alert.

    Supervisors record user-dependent pauses as status=blocked with the exact
    request in next_action. Phase 7's explicit approval wait is inherently a
    user pause, so it is surfaced even without a separate blocked transition.
    A narrow phrase check supports older state written before that convention.
    """
    if isinstance(status.get("run_closed"), dict):
        return {"required": False, "kind": None, "message": None, "blockers": [], "request_id": None,
                "amendment_id": None, "amendment_decision": None, "question_id": None,
                "question_cards": [], "turn": None, "preauthorized": None, "amendment_round": None}
    workflow_status = str(status.get("status") or "")
    regression = next((item for item in reversed(status.get("regression_requests") or [])
                       if item.get("state") == "awaiting_approval"), None)
    phase = int(status.get("phase_number", 1) or 1)
    next_action = str(status.get("next_action") or "Pilot authorization is required before the mission can continue.")
    approval_missing = (
        lib.adaptive_deployment_approval_required(status, cfg)
        and phase == 7
        and not status.get("deployment_approved")
        and not isinstance(status.get("human_pause"), dict)
    )
    drift = (lib.evidence_drift(root, cfg, acceptance, verifications)
             if root is not None and acceptance is not None and verifications is not None
             else {"stale": [], "refresh_commands": []})
    evidence_stale = bool(drift["stale"])
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
    budget_exhausted = (phase == 2 and budget.get("exhausted")
                        and not design_approval_missing
                        and not isinstance(status.get("run_closed"), dict))
    required = bool(regression) or workflow_status == "blocked" or approval_missing or design_approval_missing \
        or older_signal or bool(amendment) or bool(questions)
    escalation = status.get("escalation") if isinstance(status.get("escalation"), dict) else None
    blockers: list[str] = []
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
    elif approval_missing and evidence_stale:
        kind = "evidence_drift"
        message = (f"Automated evidence for {', '.join(drift['stale'])} is stale; run "
                   f"{', '.join(drift['refresh_commands'])} before deployment can be authorized.")
    elif approval_missing:
        kind = "deployment_approval"
        message = "Pilot authorization required: grant explicit deployment approval before live verification can continue."
    elif design_approval_missing:
        kind = "design_approval"
        blockers = lib.design_approval_blockers(status, acceptance or {}, cfg) if acceptance is not None else []
        if blockers:
            message = ("Independent design review is approved, but the design gate cannot take the "
                       "authorization yet: " + "; ".join(blockers) + ".")
        else:
            message = "Independent design review is approved. Authorize this exact design to open Phase 3."
    elif workflow_status == "blocked":
        kind = "blocked"
        message = next_action
    else:
        kind = "decision"
        message = next_action
    turn = _decision_turn(kind) if required else None
    # A standing pre-authorization only ever stands in for the Pilot's own
    # turn; a reviewer's or the Architect's pending step is theirs to take.
    preauthorized = _standing_preauthorization(root, cfg) if required and turn == "pilot" else None
    amendment_round = _amendment_round(root, cfg, amendment) if amendment else None
    return {"required": required, "kind": kind if required else None,
            "message": message if required else None,
            "blockers": blockers if required and kind == "design_approval" else [],
            "request_id": regression.get("request_id") if regression else None,
            "amendment_id": amendment.get("amendment_id") if amendment else None,
            "amendment_decision": amendment_decision,
            "question_id": questions[0].get("question_id") if questions else None,
            "question_cards": lib.question_cards(status) if questions else [],
            "turn": turn, "preauthorized": preauthorized, "amendment_round": amendment_round}


class _ThreadCaptureStdout:
    """A stdout proxy that diverts only the registering thread's writes.

    The dashboard is a ThreadingHTTPServer with orchestration and watchdog
    threads that print on their own schedule, so contextlib.redirect_stdout
    (which swaps the process-global sys.stdout) would either swallow their
    lines or, with interleaved enter/exit, leave the server's stdout pointed
    at a discarded buffer. Every other thread keeps writing to the real
    stream; a thread with a registered buffer writes there instead."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def capture(self, buffer) -> None:
        self._local.buffer = buffer

    def release(self) -> None:
        self._local.buffer = None

    def _target(self):
        return getattr(self._local, "buffer", None) or self._real

    def write(self, text):
        return self._target().write(text)

    def writelines(self, lines):
        return self._target().writelines(lines)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_CAPTURE_LOCK = threading.Lock()


def _capturing_stdout() -> _ThreadCaptureStdout:
    """Install the proxy once; later calls reuse it (stack-safe by construction)."""
    with _CAPTURE_LOCK:
        current = sys.stdout
        if isinstance(current, _ThreadCaptureStdout):
            return current
        proxy = _ThreadCaptureStdout(current)
        sys.stdout = proxy
        return proxy


def _run_gate(command_fn, command) -> tuple[int, str | None]:
    """Run a Supervisor command in-process and keep its refusal text. The
    commands explain a refusal on stdout (SHIP_FEATURE_BLOCKED ...); a
    Mission Control button that only said "the gate rejected this" left
    the Pilot pressing it again with no way to learn why. Only this
    thread's output is captured (see _ThreadCaptureStdout)."""
    buffer = io.StringIO()
    proxy = _capturing_stdout()
    proxy.capture(buffer)
    try:
        code = command_fn(command)
    finally:
        proxy.release()
    text = buffer.getvalue()
    if code == 0:
        # A success line (DESIGN_APPROVAL_RECORDED and the like) still
        # belongs in the server log.
        proxy.write(text)
        return 0, None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if line.startswith("SHIP_FEATURE_BLOCKED")), None)
    if start is None:
        return code, None
    reason = lines[start][len("SHIP_FEATURE_BLOCKED"):].lstrip(": ").strip()
    details = [line.lstrip("- ").strip() for line in lines[start + 1:] if line.startswith("-")]
    reason = "; ".join([part for part in [reason, *details] if part])
    return code, reason[:600] or None


def _operator_actions(status: dict, cfg: dict, input_request: dict) -> list[dict]:
    """Canonical current Pilot actions; every id is bound to displayed state."""
    binding = _operation_binding(status, input_request)
    actions = []

    operation_by_kind = {
        "design_approve": "design-approve", "design_reject": "design-reject",
        "deployment_approve": "deployment-gate", "deployment_hold": "human-pause-start",
        "deployment_revoke": "deployment-gate",
        "design_review_authorize": "design-review-authorize", "design_review_escalate": "design-review-escalate",
        "regression_accept": "regression-decide", "regression_decline": "regression-decide",
        "regression_cancel": "regression-cancel", "amendment_approve": "amendment-approve",
        "amendment_escalate": "amendment-escalate", "amendment_review_approve": "amendment-review",
        "amendment_review_reject": "amendment-review", "recovery_acknowledge": "recovery-acknowledge",
        "recover": "recover", "review_cap_override": "review-cap-override",
        "pause": "human-pause-start", "resume": "human-pause-end",
        "run_close": "run-close", "run_reopen": "run-reopen",
    }

    def add(kind, label, consequence, *, reason=False, tone="primary", confirmation=False):
        operation = operation_by_kind[kind]
        registry = supervisor.OPERATION_REGISTRY.get(operation) or {}
        if registry.get("class") != "operator-facing":
            raise lib.HandsoffError(f"dashboard action {kind} has no operator-facing canonical operation")
        actions.append({
            "action_id": f"{kind}:{binding}", "kind": kind, "label": label,
            "operation": operation, "type": input_request.get("kind") or kind,
            "reason": input_request.get("message"), "requesting_role": _active_role(status, input_request),
            "requesting_session": None,
            "created_at": status.get("updated_at"), "binding": binding,
            "consequence": consequence, "requires_reason": reason, "tone": tone,
            "requires_confirmation": confirmation,
        })

    kind = input_request.get("kind")
    if kind == "design_approval":
        add("design_approve", "Authorize design", "Opens Phase 3 for the exact reviewed design")
        add("design_reject", "Request design revision", "Returns the design to the Architect and Reviewer", reason=True, tone="danger")
    elif kind == "deployment_approval":
        add("deployment_approve", "Authorize deployment", "Allows live verification to continue")
        add("deployment_hold", "Hold deployment", "Pauses the mission until the Pilot resumes it", reason=True, tone="danger")
    elif kind == "deployment_revoke":
        add("deployment_revoke", "Revoke deployment approval", "Returns the mission to Phase 7 approval", reason=True, tone="danger")
    elif kind == "design_review_budget":
        add("design_review_authorize", "Authorize one review", "Permits exactly one additional design review")
        add("design_review_escalate", "Escalate reviewer tier", "Routes the next review to the primary reviewer tier", reason=True)
        add("pause", "Keep mission held", "Records an explicit Pilot pause", reason=True, tone="muted")
    elif kind == "regression_approval":
        add("regression_accept", "Accept regression", "Authorizes the displayed commands once")
        add("regression_decline", "Decline regression", "Closes this request without executing it", tone="danger")
    elif kind == "amendment_approval":
        add("amendment_approve", "Approve amendment", "Resumes the frozen phase with the reviewed delta")
        add("amendment_escalate", "Escalate to redesign", "Closes the amendment and returns to full design", reason=True, tone="danger")
    elif kind == "amendment_review":
        # The amendment review is the independent Reviewer's decision
        # (amendment-review is agent-only in OPERATION_REGISTRY); the Pilot
        # launches the reviewer from the console, never records it here.
        pass
    elif kind == "amendment_revision":
        add("amendment_escalate", "Escalate to redesign", "Closes the amendment and returns to full design", reason=True, tone="danger")
    escalation = status.get("escalation") or {}
    if escalation.get("kind") in {"recovery_exhausted", "recovery_paused"}:
        add("recovery_acknowledge", "Acknowledge recovery hold", "Clears the exhausted recovery hold", reason=True)
        add("recover", "Retry recovery", "Runs one eligible bounded recovery attempt", reason=True)
    if escalation.get("kind") == "review_cap_exhausted":
        add("review_cap_override", "Authorize one review attempt", "Extends the implementation-review cap once", reason=True)
    current_regression = lib.active_regression_request(status)
    if current_regression and current_regression.get("state") in {"awaiting_approval", "accepted"}:
        add("regression_cancel", "Cancel regression request", "Closes the pending regression request", tone="danger")
    if isinstance(status.get("run_closed"), dict):
        actions.clear()
        if status.get("status") != "complete":
            add("run_reopen", "Reopen mission", "Restores this run for continued work", reason=True)
    elif isinstance(status.get("human_pause"), dict):
        add("resume", "Resume mission", "Ends the explicit Pilot pause")
    elif status.get("status") != "complete" and not actions:
        add("pause", "Pause mission", "Records an explicit Pilot pause", reason=True, tone="muted")
    if not isinstance(status.get("run_closed"), dict):
        live = [item for item in lib.current_agent_sessions(status).values()
                if isinstance(item, dict) and item.get("state") in lib.AGENT_SESSION_LIVE_STATES]
        consequence = "Cancels the owned live session and records closure" if live else "Records closure and releases run-owned resources"
        add("run_close", "Cleanly close run", consequence, reason=True, tone="danger", confirmation=True)
    return actions


def _operation_binding(status: dict, input_request: dict) -> str:
    """Use the legacy state binding for both action lists and inventory."""
    seed = {
        "updated_at": status.get("updated_at"), "kind": input_request.get("kind"),
        "escalation": status.get("escalation"),
        "amendment_hash": (status.get("amendment") or {}).get("amendment_hash"),
        "regression": (lib.active_regression_request(status) or {}).get("command_sha256"),
    }
    return hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _duration_words(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "unknown"
    total = int(seconds)
    if total < 3600:
        return f"{total // 60} min"
    return f"{total // 3600} h {(total % 3600) // 60:02d} m"


def _supervisor_briefing(status: dict, criteria: list[dict], errors: list[str],
                         audit_errors: list[str], stall: str | None, activity: str | None,
                         latest_event: dict | None, input_request: dict, host_wait: dict | None = None) -> dict:
    phase_number = int(status.get("phase_number", 1) or 1)
    phase = status.get("phase") or lib.PHASES.get(phase_number, "Unknown phase")
    progress = status.get("progress", 0)
    passing = sum(c.get("state") == "passing" for c in criteria)
    total = len(criteria)
    resolved = status.get("requirement_coverage", {}).get("original_symptom_resolved") is True
    all_errors = [*audit_errors, *errors]
    blocked = [c for c in criteria if c.get("state") == "blocked"]
    failing = [c for c in criteria if c.get("state") == "failing"]

    if isinstance(status.get("run_closed"), dict):
        closed = status["run_closed"]
        tone = "steady"
        if closed.get("outcome") == "not_planned":  # #177
            label = "Not planned"
            headline = f"Not planned, declined by {closed.get('by')}: {closed.get('reason')}"
        else:
            label = "Mission closed"
            headline = f"Mission closed by {closed.get('by')}: {closed.get('reason')}"
        summary = status.get("next_action") or "The run is closed."
    elif input_request["required"]:
        pilot_turn = input_request.get("turn") == "pilot" and not input_request.get("preauthorized")
        tone = "critical" if pilot_turn else "warning"
        label, headline = _decision_headline(input_request)
        summary = input_request["message"]
    elif all_errors:
        tone = "critical"
        label = "Safety interlock"
        headline = "Tactical advance suspended, Pilot."
        summary = (f"The feature is at Phase {phase_number}, {phase}, with {progress}% reported progress. "
                   f"I found {len(all_errors)} condition{'s' if len(all_errors) != 1 else ''} that must be resolved before advancement.")
    elif host_wait:
        # #194: the ball is with the host; say which host, what it owes,
        # and for how long, and point the Pilot at the button that moves it.
        tone = "warning"
        label = "Waiting on the host"
        since = str(host_wait.get("since") or "")[11:16]
        headline = (f"Waiting on the host ({host_wait.get('family')}) since {since}Z: "
                    f"{host_wait.get('action')} ({_duration_words(host_wait.get('silent_seconds'))})")
        summary = (f"The host has written nothing to the ledger for {_duration_words(host_wait.get('silent_seconds'))}. "
                   f"Work remains at Phase {phase_number}, {phase}, with {passing} of {total} acceptance criteria verified.")
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
        "next_action": (f"Launch the {host_wait['launch_role']} from the Pilot console (LAUNCH ROLE), or wait for the host"
                        if host_wait and host_wait.get("launch_role") else
                        input_request["message"] or status.get("next_action") or lib.NEXT_ACTION_DEFAULTS.get(phase_number, "Review the current state.")),
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
            "operator_actions": [{
                "action_id": "init:new", "kind": "init", "label": "Initialize mission",
                "consequence": "Creates a new Handsoff mission in this project",
                "requires_reason": False, "tone": "primary",
            }],
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
            liveness = lib.liveness_view(status, root, cfg)
            stall = liveness["stall_warning"]
            lib.record_stall_transition(root, cfg, stall)
            activity_view = liveness
            activity = liveness.get("activity_note")
            # #33: the live session view, from structured state plus the beacon.
            live = lib.live_status(status, cfg, root)
            agent_output = lib.agent_output_view(status, root)
            operation = lib.operation_view(status, root)
            current_regression = lib.active_regression_request(status)
            last_regression = next((item for item in reversed(status.get("regression_requests") or [])
                                    if item.get("state") not in {"awaiting_approval", "accepted", "launched"}), None)
            regression_progress = _regression_progress(root, current_regression or last_regression)
            # #38: states and hashes only; the bounded output stays in the side file.
            try:
                design_evidence = lib.design_evidence_view(root, cfg)
            except lib.HandsoffError as exc:
                design_evidence = [{"id": entry["id"], "state": "missing", "reasons": [str(exc)],
                                    "input_hash": None, "matched_files": None, "output_sha256": None,
                                    "at": None, "by": None, "head": None, "commit_matches_head": False,
                                    "truncated": False, "exit_code": None}
                                   for entry in cfg.get("design_evidence", [])]
            recovery_assessment = liveness["assessment"]
            try:
                tranche_proposal = json.loads((root / tranche.PROPOSAL_FILE).read_text(encoding="utf-8"))
                if tranche.proposal_hash(tranche_proposal) != tranche_proposal.get("proposal_hash"):
                    tranche_proposal = None
            except (OSError, ValueError):
                tranche_proposal = None
    except (lib.HandsoffError, OSError) as exc:
        return {"initialized": False, "generated_at": generated_at, "root": str(root), "error": str(exc)}

    criteria = [dict(c) for c in acceptance.get("criteria", [])]
    for criterion in criteria:
        # #165: what the failing-first gate sees: the newest valid baseline
        # (RED before GREEN) or the declaration that none can exist.
        baseline = lib.criterion_baseline(criterion, verifications)
        # #169: the repeat count, from the newest checks record's attempts
        if criterion.get("repeat"):
            newest = next((r for r in reversed(verifications) if isinstance(r, dict) and r.get("kind") == "checks"
                           and criterion.get("id") in r.get("criteria", []) and r.get("attempts") is not None), None)
            attempts = newest.get("attempts") if newest else None
            failed = next((a for a in (attempts or []) if not a.get("ok")), None)
            criterion["repeat_view"] = ({"kind": "failed", "attempt": failed["attempt"], "repeat": criterion["repeat"], "seed": failed.get("seed")}
                                        if failed else {"kind": "passed", "attempts": len(attempts or []), "repeat": criterion["repeat"]}
                                        if attempts else {"kind": "pending", "repeat": criterion["repeat"]})
        criterion["baseline_view"] = (
            {"kind": "not_applicable", "reason": criterion.get("baseline_reason") or ""}
            if criterion.get("baseline") == lib.BASELINE_NOT_APPLICABLE
            else {"kind": "recorded", "at": baseline.get("at"), "run_id": baseline.get("run_id")}
            if baseline else {"kind": "none"})
    work_items = lib.derive_work_items(status, acceptance, cfg)
    latest_event = events[-1] if events else None
    coverage = status.get("requirement_coverage", {})
    design_review_budget = lib.design_review_budget(status, cfg)
    # #37: current (the latest recorded review's tier) and next (what the
    # next launch selects, with any refusal named) reviewer profile.
    design_reviewer_selection = lib.design_reviewer_selection_view(cfg, status, acceptance)
    audit_healthy = not gate_errors and not audit_errors
    input_request = _input_request(status, cfg, root, acceptance, verifications)
    operator_actions = _operator_actions(status, cfg, input_request)
    operations_inventory = lib.operation_inventory(status, acceptance, cfg, root,
                                                    _operation_binding(status, input_request))
    display_status = dict(status)
    display_status["verification_progress"] = status.get("progress", 0)
    display_status["progress"] = _display_progress(status)
    if isinstance(status.get("run_closed"), dict):
        display_status["status"] = "closed"
        display_status["phase"] = "Run closed"
    verification_view = _verification_view(root, cfg, verifications)
    if not isinstance(status.get("run_closed"), dict):
        display_status["phase"] = _display_phase_name(status, verification_view.get("live"))
    display_status["stall_warning"] = liveness["stall_warning"]
    display_status["live"] = live
    display_status["activity"] = liveness
    display_status["process_signal"] = liveness["process_signal"]
    display_status["consistency_errors"] = design_reviewer_selection.get("consistency_errors", [])
    actors = {
        "architect": ((status.get("design_review") or {}).get("architect")
                      or (status.get("design_approved") or {}).get("architect")),
        "design_reviewed_by": (status.get("design_review") or {}).get("by"),
        "implemented_by": status.get("implemented_by"),
        "reviewed_by": status.get("reviewed_by"),
        "approved_by": (status.get("deployment_approved") or {}).get("by"),
        "active_role": None if isinstance(status.get("run_closed"), dict) else _active_role(status, input_request),
    }
    sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}

    def session_view(session):
        if not isinstance(session, dict):
            return None
        fields = ("session_id", "role", "actor", "adapter", "requested_model",
                  "reported_model", "resolution_source", "state", "started_at",
                  "running_at", "ended_at", "exit_code", "tier")
        view = {field: session.get(field) for field in fields}
        if isinstance(session.get("result"), dict) and session["result"].get("adopted_at"):
            view["state"] = "adopted"
        return view

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
    metrics = lib.build_run_metrics(status, events, verifications)
    try:
        performance = supervisor.refresh_performance_state(
            root, status=status, events=events, metrics=metrics,
        )
    except (lib.HandsoffError, OSError, ValueError) as exc:
        performance = {"state": "unavailable", "block_new_work": False, "error": str(exc)}
    engine_identity, engine_error = _engine_identity(root)  # #185
    host = lib.host_identity(status, events)  # #186
    host_wait = lib.host_wait_view(status, events, cfg, pilot_input_required=bool(input_request.get("required")))  # #194
    # #181: the CI row. ci_view refreshes through gh at most once a minute
    # and commits the terminal event once; a gh hiccup becomes the row's
    # note, never a failed snapshot.
    try:
        ci = lib.ci_view(status, root, cfg)
    except (lib.HandsoffError, OSError) as exc:
        ci = {**(status.get("ci") or {}), "checks": [], "progress": None, "elapsed_seconds": None,
              "note": f"CI view unavailable: {exc}"} if isinstance(status.get("ci"), dict) else None
    universal_progress = _test_progress(root, status, events, current_regression or last_regression,
                                        ci, regression_progress)
    return {
        "initialized": True,
        "ci": ci,
        "test_progress": universal_progress,
        "generated_at": generated_at,
        "root": str(root),
        "engine": engine_identity,
        "host": host,
        "project": {"name": root.name, "feature": status.get("feature", acceptance.get("feature", "Untitled feature")),
                    "logo_url": "/project-logo" if lib.project_logo(root, cfg) else None},
        "status": display_status,
        # Snapshot contract choice (a): these keys are always emitted, with
        # empty/null defaults for legacy full runs rather than being optional.
        "lane": status.get("lane"),
        "phases_run": deepcopy(status.get("phases_run", [])),
        "phases_waived": deepcopy(status.get("phases_waived", [])),
        "design_document": status.get("design_document"),
        "phases": _phase_view(
            int(status.get("phase_number", 1) or 1),
            status.get("status") == "complete",
            display_status["phase"],
            closed=isinstance(status.get("run_closed"), dict),
            lane=status.get("lane"),
            phases_run=status.get("phases_run"),
            phases_waived=status.get("phases_waived"),
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
        "tranche": tranche_proposal,
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
            "agent_output": agent_output,
            "operation": operation,
        },
        "metrics": metrics,
        "performance": performance,
        "adaptive_routing": lib.adaptive_routing_snapshot(status, cfg, host=host, events=events),
        "model_policy": deepcopy(status.get("model_policy", cfg.get("model_policy", lib.DEFAULT_MODEL_POLICY))),
        "launch_preflight": lib.launch_preflight_snapshot(root),
        "audit": {
            "healthy": audit_healthy,
            "gate_errors": gate_errors,
            "chain_errors": audit_errors,
            "engine_error": engine_error,
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
            "explicit_approval": lib.adaptive_deployment_approval_required(status, cfg),
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
                "tests_executed": item.get("tests_executed", "unknown"),
            } for item in (status.get("review_attempts") or [])],
        },
        "recovery": {
            "implementer_progress": lib.latest_failed_implementer_progress(status),  # #215
            "assessment": recovery_assessment,
            "lease": status.get("recovery_lease"),
            "attempts": list((status.get("recovery_attempts") or [])[-8:]),
            "cap": cfg.get("recovery", {}).get("max_attempts", 0),
            "watchdog_enabled": cfg.get("recovery", {}).get("dashboard_watchdog", False),
        },
        "regression": {
            "release_plan": status.get("release_plan"),
            "pending": next((item for item in reversed(status.get("regression_requests") or [])
                             if item.get("state") == "awaiting_approval"), None),
            "current": current_regression,
            "last": last_regression,
            "progress": regression_progress,
            "history": list(reversed((status.get("regression_requests") or [])[-8:])),
        },
        "escalation": status.get("escalation"),
        "settings": _settings_view(cfg),
        "input_required": input_request,
        "operator_actions": operator_actions,
        "operations": {"inventory": operations_inventory,
                        "verification": verification_view,
                        "engine": _engine_view(root)},
        "verification": verification_view,
        "activity_note": activity,
        "activity": activity_view,
        "live": live,
        "supervisor": _supervisor_briefing(display_status, criteria, gate_errors, audit_errors, stall, activity,
                                            latest_event, input_request, host_wait),
        "host_wait": host_wait,
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
            if self.owned_by_run:
                threading.Thread(target=self._orchestration_loop, daemon=True).start()

    def _launch_managed_role(self, role: str, task: str, actor: str | None = None) -> int:
        """Launch a managed role. `actor` names who asked for the launch and
        becomes the session's recorded identity, so only a Pilot-initiated
        /api/launch-role passes "Mission Control Pilot"; watchdog and
        orchestration launches keep the runtime default (adapter-role) so
        a machine reviewer is never recorded under the Pilot's name."""
        refusal = supervisor.performance_mutation_refusal(self.project_root, "launch_agent")
        if refusal:
            raise lib.HandsoffError(refusal)
        import handsoff_agent
        spec = handsoff_agent.build_launch_spec(self.project_root, role, task)
        return handsoff_agent.execute_with_recovery(spec, actor=actor)

    def _orchestration_loop(self):
        while not self._watchdog_stop.wait(1.0):
            try:
                self._orchestrate_once()
            except Exception as exc:
                print(f"HANDSOFF_ORCHESTRATION_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)

    def request_first_launch(self, objective: str) -> None:
        """#119: remember the mission objective so the orchestration thread
        launches the managed Architect once after init."""
        with self._stop_lock:
            self._first_launch_objective = str(objective)[:512]

    def _take_first_launch(self) -> str | None:
        with self._stop_lock:
            objective = getattr(self, "_first_launch_objective", None)
            self._first_launch_objective = None
        return objective

    def _orchestrate_once(self):
        """Advance approved design state, then optionally hand off one role."""
        cfg = lib.load_config(self.project_root)
        with lib.project_lock(self.project_root):
            status = lib.load_unique_json(lib.status_path(self.project_root, cfg))
        objective = self._take_first_launch()
        if objective is not None:
            profile = lib.resolved_agent_profiles(cfg).get("architect") or {}
            if profile.get("adapter") != lib.HOST_AGENT_ADAPTER and lib.assigned_role(status) == "architect" \
                    and not any(isinstance(item, dict) and item.get("state") in lib.AGENT_SESSION_LIVE_STATES
                                for item in (status.get("agent_sessions") or {}).values()):
                self._launch_managed_role("architect", lib.orchestration_task("architect", status, objective=objective))
                return
        if supervisor.advance_approved_design(self.project_root):
            return
        if not cfg.get("auto_handoff", True):
            return
        role = lib.managed_handoff_role(status, cfg)
        if role is None:
            return
        self._launch_managed_role(role, lib.orchestration_task(role, status))

    def _watchdog_loop(self):
        cfg = lib.load_config(self.project_root)
        interval = cfg.get("recovery", {}).get("poll_seconds", 30)
        while not self._watchdog_stop.wait(interval):
            try:
                def launcher(role):
                    task = (f"Resume trusted Handsoff state as {role}; read status, acceptance, and event log "
                            "and continue without repeating evidenced work.")
                    return self._launch_managed_role(role, task)
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
        # #124: loopback always; otherwise one configured [dashboard]
        # public_origins entry, compared exactly.
        try:
            public = lib.load_config(self.server.project_root).get("public_origins", [])
        except lib.HandsoffError:
            public = []
        return lib.origin_allowed(self.headers.get("Origin"), self.server.server_port, public)

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
        if path == "/api/regression":
            try:
                root = self.server.project_root
                with lib.project_lock(root):
                    cfg = lib.load_config(root)
                    status = lib.load_unique_json(lib.status_path(root, cfg))
                    current = lib.active_regression_request(status)
                    last = next((item for item in reversed(status.get("regression_requests") or [])
                                 if item.get("state") not in {"awaiting_approval", "accepted", "launched"}), None)
                    payload = _regression_progress(root, current or last)
            except (lib.HandsoffError, OSError, ValueError):
                payload = None
            self._json_response(HTTPStatus.OK, {"regression": payload, "generated_at": datetime.now(timezone.utc).isoformat()})
            return
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
        if path == "/project-logo":
            # The project's own artwork ([project] logo, or a conventional
            # path), served from inside the project only.
            try:
                found = lib.project_logo(self.server.project_root, lib.load_config(self.server.project_root))
                payload = found[0].read_bytes() if found else None
            except (lib.HandsoffError, OSError):
                found, payload = None, None
            if payload is None:
                payload = b"No project logo"
                self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            self._headers(HTTPStatus.OK, found[1], len(payload))
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
        if path not in {"/api/settings/agents", "/api/settings/features", "/api/design-approval", "/api/deployment-approval",
                        "/api/init", "/api/operator-action", "/api/launch-role", "/api/verify", "/api/verify-live",
                        "/api/lane-confirm", "/api/tranche-approval",
                        "/api/regression-decision", "/api/question-answer", "/api/question-answers",
                        "/api/design-review-authorize", "/api/pilot-note", "/api/amendment-approval"}:
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
            if path in {"/api/verify", "/api/verify-live"}:
                expected = {"action_id", "criteria"} if path == "/api/verify" else {"action_id"}
                if set(requested) != expected or not isinstance(requested.get("action_id"), str) \
                        or path == "/api/verify" and (not isinstance(requested.get("criteria"), list)
                        or not all(isinstance(value, str) for value in requested["criteria"])
                        or not requested["criteria"]):
                    raise lib.HandsoffError("verification request has an invalid shape")
                snapshot = build_snapshot(self.server.project_root)
                kind = "verify" if path == "/api/verify" else "verify_live"
                item_kind = "verify_criterion" if kind == "verify" else "verify_live"
                item = next((x for x in snapshot["operations"]["inventory"] if x["kind"] == item_kind), None)
                reason = None
                if not item or item.get("action_id") != requested["action_id"]:
                    reason = "The displayed verification action is stale"
                elif item.get("availability") != "actionable":
                    reason = item.get("reason") or "verification is unavailable"
                elif kind == "verify":
                    allowed = set(item.get("criteria") or [])
                    invalid = [value for value in requested["criteria"] if value not in allowed]
                    if invalid:
                        reason = f"unknown or non-automated criteria: {', '.join(invalid)}"
                if reason is None and lib.verify_inflight_bindings(self.server.project_root):
                    reason = "verification already in flight"
                criteria = requested.get("criteria", [])
                cfg = lib.load_config(self.server.project_root)
                lib.commit(self.server.project_root, cfg, event_kind="pilot_verification_requested",
                           event_message="Mission Control Pilot requested verification", kind=kind,
                           criteria=criteria, accepted=reason is None, reason=reason,
                           by="Mission Control Pilot")
                if reason:
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": reason})
                    return
                _start_verification(self.server.project_root, kind, criteria)
                self._json_response(HTTPStatus.OK, {"ok": True, "kind": kind, "criteria": criteria})
                return
            if path == "/api/init":
                if set(requested) != {"feature", "issue", "lane"} \
                        or not isinstance(requested.get("feature"), str) \
                        or requested.get("issue") is not None and not isinstance(requested.get("issue"), str) \
                        or requested.get("lane") not in {"full", "small-fix"}:
                    raise lib.HandsoffError("mission initialization requires feature, optional issue, and lane")
                item = [requested["issue"].strip()] if requested.get("issue") and requested["issue"].strip() else []
                command = argparse.Namespace(root=str(self.server.project_root), feature=requested["feature"],
                                             item=item, lane=requested["lane"])
                if supervisor.cmd_init(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "Mission initialization was rejected"})
                    return
                # #119: on an owned dashboard with a managed Architect, the
                # crew starts itself; the orchestration thread launches it
                # exactly once (the pending task is consumed on pickup).
                if self.server.owned_by_run:
                    self.server.request_first_launch(requested["feature"])
                self._json_response(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/operator-action":
                if set(requested) != {"action_id", "reason"} \
                        or not isinstance(requested.get("action_id"), str) \
                        or requested.get("reason") is not None and not isinstance(requested.get("reason"), str):
                    raise lib.HandsoffError("operator action requires action_id and optional reason")
                snapshot = build_snapshot(self.server.project_root)
                action = next((item for item in snapshot.get("operator_actions") or []
                               if item.get("action_id") == requested["action_id"]), None)
                if not action:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The displayed operator action is stale"})
                    return
                reason = (requested.get("reason") or "").strip()
                if action.get("requires_reason") and not reason:
                    raise lib.HandsoffError("this operator action requires a reason")
                root_arg = str(self.server.project_root)
                pilot = "Mission Control Pilot"
                status = snapshot.get("status") or {}
                kind = action["kind"]
                gate_reason = None
                if kind == "design_approve":
                    review = status.get("design_review") or {}
                    command = argparse.Namespace(root=root_arg, by=pilot, architect=review.get("architect"),
                                                 summary="Pilot authorized the reviewed design in Mission Control.",
                                                 redesigns_settled_work=None)
                    code, gate_reason = _run_gate(supervisor.cmd_design_approve, command)
                elif kind == "design_reject":
                    code, gate_reason = _run_gate(supervisor.cmd_design_reject, argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason))
                elif kind == "deployment_approve":
                    code, gate_reason = _run_gate(supervisor.cmd_deployment_gate, argparse.Namespace(
                        root=root_arg, approve=True, revoke=False, auto=False, by=pilot, reason=None))
                elif kind in {"deployment_hold", "pause"}:
                    code = supervisor.cmd_human_pause_start(argparse.Namespace(
                        root=root_arg, by=pilot, note=reason or "Pilot held the mission in Mission Control"))
                elif kind == "resume":
                    code = supervisor.cmd_human_pause_end(argparse.Namespace(
                        root=root_arg, by=pilot, note="Pilot resumed the mission in Mission Control"))
                elif kind == "design_review_authorize":
                    code = supervisor.cmd_design_review_authorize(argparse.Namespace(
                        root=root_arg, by=pilot, note="Authorized from Mission Control"))
                elif kind == "design_review_escalate":
                    code = supervisor.cmd_design_review_escalate(argparse.Namespace(
                        root=root_arg, by=pilot, note=reason))
                elif kind == "recovery_acknowledge":
                    code = supervisor.cmd_recovery_acknowledge(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason))
                elif kind == "recover":
                    code = supervisor.cmd_recover(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason, dry_run=False, timeout=3600))
                elif kind == "review_cap_override":
                    code = supervisor.cmd_review_cap_override(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason))
                elif kind == "amendment_approve":
                    code = supervisor.cmd_amendment_approve(argparse.Namespace(root=root_arg, by=pilot))
                elif kind == "amendment_escalate":
                    code = supervisor.cmd_amendment_escalate(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason))
                elif kind in {"amendment_review_approve", "amendment_review_reject"}:
                    code = supervisor.cmd_amendment_review(argparse.Namespace(
                        root=root_arg, by=pilot, summary=reason,
                        approve=kind == "amendment_review_approve",
                        request_changes=kind == "amendment_review_reject"))
                elif kind in {"regression_accept", "regression_decline"}:
                    item = (snapshot.get("regression") or {}).get("pending") or {}
                    code = supervisor.cmd_regression_decide(argparse.Namespace(
                        root=root_arg, request_id=item.get("request_id"), by=pilot,
                        accept=kind == "regression_accept", decline=kind == "regression_decline"))
                elif kind == "regression_cancel":
                    item = (snapshot.get("regression") or {}).get("current") or {}
                    code = supervisor.cmd_regression_cancel(argparse.Namespace(
                        root=root_arg, request_id=item.get("request_id"), by=pilot))
                elif kind == "run_close":
                    code = supervisor.cmd_run_close(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason,
                        expected_updated_at=status.get("updated_at"), cancel_active=True,
                        release_dashboard=False))
                elif kind == "run_reopen":
                    code = supervisor.cmd_run_reopen(argparse.Namespace(
                        root=root_arg, by=pilot, reason=reason,
                        expected_updated_at=status.get("updated_at")))
                else:
                    raise lib.HandsoffError("unsupported operator action")
                if code != 0:
                    self._json_response(HTTPStatus.CONFLICT, {
                        "ok": False,
                        "error": gate_reason or "The workflow gate rejected this action",
                    })
                    return
                self._json_response(HTTPStatus.OK, {"ok": True, "kind": kind})
                return
            if path == "/api/launch-role":
                if set(requested) != {"action_id", "role", "task"} or not all(isinstance(requested.get(k), str) for k in requested):
                    raise lib.HandsoffError("launch role requires action_id, role, and task")
                task = requested["task"]
                task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()
                pilot = "Mission Control Pilot"
                reason = None
                # build_snapshot and lib.commit each take the project lock
                # themselves (flock is not reentrant); the action_id binding
                # carries the staleness guarantee across the two calls.
                snapshot = build_snapshot(self.server.project_root)
                item = next((x for x in snapshot["operations"]["inventory"] if x["kind"] == "launch_role"), None)
                if not item or item.get("action_id") != requested["action_id"]:
                    reason = "The displayed launch role action is stale"
                elif requested["role"] != item.get("role"):
                    reason = "role is not the assigned launch role"
                elif requested["role"] == "host":
                    reason = "host roles cannot be launched as managed sessions"
                elif item.get("availability") != "actionable":
                    reason = item.get("reason") or "role is not launchable"
                elif not task.strip():
                    reason = "task must be a non-empty string"
                elif len(task) > 4000:
                    reason = "task exceeds 4000 characters"
                cfg = lib.load_config(self.server.project_root)
                lib.commit(self.server.project_root, cfg, event_kind="pilot_launch_requested",
                           event_message="Mission Control Pilot requested a managed role launch",
                           role=requested["role"], accepted=reason is None,
                           reason=reason, task_sha256=task_sha256, by=pilot)
                if reason:
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": reason})
                    return
                threading.Thread(target=self.server._launch_managed_role,
                                 args=(requested["role"], task, pilot), daemon=True).start()
                self._json_response(HTTPStatus.OK, {"ok": True, "role": requested["role"], "session_launching": True})
                return
            if path == "/api/lane-confirm":
                if set(requested) != {"item"} or not isinstance(requested.get("item"), str):
                    raise lib.HandsoffError("lane confirmation requires one work-item id")
                command = argparse.Namespace(root=str(self.server.project_root),
                                             item=requested["item"], by="Mission Control Pilot")
                if supervisor.cmd_lane_confirm(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The small-fix lane gate rejected this item"})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True, "item": requested["item"]})
                return
            if path == "/api/tranche-approval":
                required = {"proposal_hash", "order", "drops"}
                if set(requested) != required or not isinstance(requested.get("proposal_hash"), str) \
                        or not isinstance(requested.get("order"), list) \
                        or not isinstance(requested.get("drops"), list) \
                        or not all(isinstance(value, str) for value in requested["order"] + requested["drops"]):
                    raise lib.HandsoffError("tranche approval requires proposal_hash, order, and drops")
                command = argparse.Namespace(root=str(self.server.project_root),
                                             proposal_hash=requested["proposal_hash"],
                                             item=requested["order"], drop=requested["drops"],
                                             by="Mission Control Pilot")
                if supervisor.cmd_tranche_approve(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The tranche gate rejected this decision"})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True})
                return
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
            if path == "/api/pilot-note":
                # #49: the header note box; same audited path as the CLI,
                # actor is the dashboard's Pilot identity.
                if set(requested) != {"text"} or not isinstance(requested.get("text"), str):
                    raise lib.HandsoffError("pilot note requires text")
                record = lib.record_pilot_note(self.server.project_root, by="Mission Control Pilot",
                                               text=requested["text"])
                self._json_response(HTTPStatus.OK, {"ok": True, "text": record["text"]})
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
                code, gate_reason = _run_gate(supervisor.cmd_design_approve, command)
                if code != 0:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": gate_reason or "The design approval gate rejected this authorization"},
                    )
                    return
                approved = lib.load_unique_json(lib.status_path(
                    self.server.project_root, lib.load_config(self.server.project_root)
                )).get("design_approved")
                self._json_response(HTTPStatus.OK, {"ok": True, "design_approved": approved})
                return
            if path == "/api/amendment-approval":
                if requested:
                    raise lib.HandsoffError("amendment approval payload must be empty")
                snapshot = build_snapshot(self.server.project_root)
                if (snapshot.get("input_required") or {}).get("kind") != "amendment_approval":
                    self._json_response(HTTPStatus.CONFLICT, {
                        "ok": False, "error": "The current mission is not waiting on an amendment approval"})
                    return
                command = argparse.Namespace(root=str(self.server.project_root), by="Mission Control Pilot")
                if supervisor.cmd_amendment_approve(command) != 0:
                    self._json_response(HTTPStatus.CONFLICT,
                                        {"ok": False, "error": "The amendment gate rejected this approval"})
                    return
                self._json_response(HTTPStatus.OK, {"ok": True})
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
                    root=str(self.server.project_root), approve=True, revoke=False, auto=False,
                    by="Mission Control Pilot", reason=None,
                )
                code, gate_reason = _run_gate(supervisor.cmd_deployment_gate, command)
                if code != 0:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": gate_reason or "The deployment gate rejected this authorization"},
                    )
                    return
                approved = lib.load_unique_json(lib.status_path(
                    self.server.project_root, lib.load_config(self.server.project_root)
                )).get("deployment_approved")
                self._json_response(HTTPStatus.OK, {"ok": True, "deployment_approved": approved})
                return
            if path == "/api/settings/features":
                # #165 #167 #166: the switches, through the same lock and
                # validation as the agent matrix; the change is an audited
                # event so a flipped switch is visible in the activity feed.
                saved = lib.update_feature_settings(self.server.project_root, requested)
                effective_cfg = lib.load_config(self.server.project_root)
                status_file = lib.status_path(self.server.project_root, effective_cfg)
                if status_file.is_file():
                    lib.append_event(self.server.project_root, effective_cfg, "features_updated",
                                     "Workflow features changed from Mission Control", features=saved["features"])
                self._json_response(HTTPStatus.OK, {"ok": True, "features": lib.features_view(effective_cfg)})
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
        threading.Timer(0.25, lambda: lib.open_dashboard_url(url)).start()
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
