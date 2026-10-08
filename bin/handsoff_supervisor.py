#!/usr/bin/env python3
"""Project Handsoff supervisor CLI. All gate logic lives in handsoff_lib.py;
this file is the command surface over it.

Paths resolve against the project root (--root, $HANDSOFF_ROOT, or the
nearest ancestor with a handsoff.toml), never against this script's own
directory. Every transition validates the state it is ABOUT to write,
never the state already on disk, and every write is atomic and appended
to a hash-chained event log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402
import handsoff_mutation as mutation  # noqa: E402
import handsoff_projection as projection  # noqa: E402
import handsoff_close_transaction as close_transaction  # noqa: E402
import handsoff_regress as regress  # noqa: E402
import handsoff_release_runtime as release_runtime  # noqa: E402
import handsoff_runtime_control as runtime_control  # noqa: E402
import handsoff_tranche as tranche  # noqa: E402

# One inventory for the command line, broker, and Mission Control. A command
# may only be exposed to the Pilot when it is explicitly classified here;
# agent protocol writes never become browser mutations by accident.
OPERATION_REGISTRY = {
    "init": {"class": "operator-facing", "surface": "mission-init-form"},
    "status": {"class": "diagnostic", "surface": "dashboard"},
    "validate": {"class": "diagnostic", "surface": "audit-state"},
    "verify-log": {"class": "diagnostic", "surface": "audit-state"},
    "doctor": {"class": "operator-facing", "surface": "audit-state"},
    "dashboard": {"class": "diagnostic", "surface": "dashboard"},
    "verify": {"class": "operator-facing", "surface": "verification-list"},
    "release-plan": {"class": "operator-facing", "surface": "regression-alert"},
    "release-reconcile": {"class": "automatic", "surface": "release-readiness"},
    "regression-request": {"class": "operator-facing", "surface": "regression-alert"},
    "regression-decide": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "regression-run": {"class": "automatic", "surface": "regression-alert"},
    "regression-cancel": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "regression-finalize": {"class": "automatic", "surface": "regression-alert"},
    "work-items-sync": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-activate": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-update": {"class": "agent-only", "surface": "ticket-panel"},
    "work-item-remove": {"class": "agent-only", "surface": "ticket-panel"},
    "lane-request": {"class": "agent-only", "surface": "ticket-panel"},
    "lane-confirm": {"class": "operator-facing", "surface": "ticket-panel"},
    "plan-tranche": {"class": "diagnostic", "surface": "tranche-panel"},
    "tranche-approve": {"class": "operator-facing", "surface": "tranche-panel"},
    "record-evidence": {"class": "agent-only", "surface": "verification-list"},
    "mutation-proof": {"class": "agent-only", "surface": "verification-list"},
    "workflow-check": {"class": "agent-only", "surface": "verification-list"},  # P1.4
    "qa-report": {"class": "agent-only", "surface": "verification-list"},  # P1.5
    "record-symptom-resolved": {"class": "agent-only", "surface": "acceptance-score"},
    "design-approve": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-reject": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "record-design-review": {"class": "agent-only", "surface": "review-attempts-panel"},
    "design-review-packet": {"class": "automatic", "surface": "design-review-packet"},
    "design-propose": {"class": "automatic", "surface": "design-review-packet"},
    "design-review-authorize": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-review-escalate": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "record-review": {"class": "agent-only", "surface": "review-attempts-panel"},
    "review-attempt-start": {"class": "agent-only", "surface": "review-attempts-panel"},
    "record-review-findings": {"class": "agent-only", "surface": "review-attempts-panel"},
    "session-result-adopt": {"class": "operator-facing", "surface": "review-attempts-panel"},
    "implementer-apply": {"class": "agent-only", "surface": "live-status"},  # #413
    "implementer-discard": {"class": "destructive", "surface": "live-status"},  # #413
    "review-cap-override": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "recover": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "watch": {"class": "automatic", "surface": "live-status"},
    "recovery-acknowledge": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "verify-live": {"class": "operator-facing", "surface": "verification-list"},
    "heartbeat": {"class": "automatic", "surface": "live-status"},
    "background-wait-start": {"class": "automatic", "surface": "live-status"},
    "background-wait-end": {"class": "automatic", "surface": "live-status"},
    "human-pause-start": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "human-pause-end": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "design-timing": {"class": "diagnostic", "surface": "metrics-panel"},
    # #397: prints what a Phase-5 reviewer will be handed; writes nothing.
    "implementation-review-packet": {"class": "diagnostic", "surface": "review-attempts-panel"},
    "design-evidence": {"class": "agent-only", "surface": "design-evidence-panel"},
    "criterion-update": {"class": "agent-only", "surface": "criteria-list"},
    "criterion-add": {"class": "agent-only", "surface": "criteria-list"},
    "criterion-remove": {"class": "agent-only", "surface": "criteria-list"},
    "criteria-apply": {"class": "agent-only", "surface": "criteria-list"},
    "amendment-open": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-revise": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-review": {"class": "agent-only", "surface": "amendment-panel"},
    "amendment-approve": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "amendment-escalate": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "question-raise": {"class": "agent-only", "surface": "questions-panel"},
    "question-answer": {"class": "operator-facing", "surface": "questions-panel"},
    "analyze-archives": {"class": "automatic", "surface": "flight-log"},
    "pilot-note": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "ci-watch": {"class": "agent-only", "surface": "ci-status"},
    "design-decline": {"class": "agent-only", "surface": "phase-rail"},
    "run-close": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "config-override": {"class": "agent-only", "surface": "flight-log"},
    "run-reopen": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "advance": {"class": "agent-only", "surface": "phase-rail"},
    "deployment-gate": {"class": "operator-facing", "surface": "operator-actions-panel"},
    "monitor-poll": {"class": "automatic", "surface": "live-status"},
    "evidence-refresh-plan": {"class": "diagnostic", "surface": "verification-list"},
    "performance-status": {"class": "diagnostic", "surface": "metrics-panel"},
    # #295: the run's own clock. Automatic, not operator-facing: a host or
    # the run-owned dashboard runs it, and it only reads and transitions.
    "performance-watch": {"class": "automatic", "surface": "metrics-panel"},
    "performance-resume": {"class": "operator-facing", "surface": "metrics-panel"},
    # #414: the Pilot's standing auto-resume decision.
    "performance-auto-resume": {"class": "operator-facing", "surface": "metrics-panel"},
    # #302: applies one shadow recommendation, only with a Mission Control approval.
    "shadow-apply": {"class": "operator-facing", "surface": "metrics-panel"},
    # #379: reads the report and the routed choice; writes nothing.
    "shadow-route": {"class": "diagnostic", "surface": "metrics-panel"},
    # #303: activates evidence-assisted routing, only with a #302 finding's approval.
    "evidence-routing-activate": {"class": "operator-facing", "surface": "metrics-panel"},
}

RUNTIME_CONTROL_DIR = ".handsoff-runtime-control"
MONITOR_RECORD = "monitor.json"
PERFORMANCE_RECORD = "performance.json"
#: #385: the append-only, hash-chained performance timeline journal.
PERFORMANCE_TIMELINE_JOURNAL = "performance-timeline.jsonl"
EVIDENCE_AUDIT_RECORD = "evidence-audit.json"
PERFORMANCE_READ_ONLY_COMMANDS = frozenset({
    "status", "validate", "verify-log", "doctor", "dashboard", "design-timing",
    "implementation-review-packet", "monitor-poll", "evidence-refresh-plan", "performance-status",
    # #295: the clock is the thing that detects the pause; refusing it
    # during a pause would make the pause un-observable from its own watcher.
    "performance-watch",
})
#: #414: commands that only write the ledger; none of them launches a role.
PERFORMANCE_BOOKKEEPING_COMMANDS = frozenset({
    "advance", "pilot-note", "work-item-update", "record-evidence", "record-symptom-resolved",
    "ci-watch", "performance-auto-resume",
})
#: #383: every supervisor command decides a pause through
#: runtime_control.require_performance_operation, under the operation it
#: performs. A command not named here is mutating and is judged under its own
#: name, which no pause allows.
PERFORMANCE_COMMAND_OPERATIONS = {
    **{command: "inspect" for command in PERFORMANCE_READ_ONLY_COMMANDS},
    "monitor-poll": "reconcile",
    "performance-resume": "explicit_resume",
    "regression-cancel": "cancel",
    "run-close": "safe_close",
    # #414: ledger bookkeeping that starts no new work stays allowed while
    # paused; a role launch and any other new work stay blocked.
    **{command: "bookkeeping" for command in PERFORMANCE_BOOKKEEPING_COMMANDS},
    # #414: verify binds cached results only; cmd_verify refuses a miss.
    "verify": "verify_cached",
}


def performance_operation(command: str) -> str:
    """The runtime-control operation a supervisor command or dashboard action performs."""
    return PERFORMANCE_COMMAND_OPERATIONS.get(command, command)


def _load(root: Path, cfg: dict):
    status = lib.load_unique_json(lib.status_path(root, cfg))
    acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    return status, acceptance


def _load_all(root: Path, cfg: dict):
    # #204: an engine checkout whose runtime files changed after the
    # manifest was written refuses every ledger command with the fix named,
    # before the command can produce evidence against a tree the manifest
    # does not describe. Thin projects are untouched.
    stale = lib.stale_manifest_refusal(root)
    if stale:
        raise lib.HandsoffError(stale)
    status, acceptance = _load(root, cfg)
    verifications, verification_problems = lib.load_verifications(root, cfg)
    return status, acceptance, verifications, verification_problems


def _runtime_path(root: Path, name: str) -> Path:
    return root / RUNTIME_CONTROL_DIR / name


def _runtime_write(root: Path, name: str, payload: dict) -> None:
    path = _runtime_path(root, name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lib.atomic_write_json(path, payload)


def _runtime_append_line(root: Path, name: str, payload: dict) -> None:
    """Durably append one JSON line to a runtime-control journal."""
    path = _runtime_path(root, name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _runtime_read(root: Path, name: str, schema: str) -> dict | None:
    path = _runtime_path(root, name)
    if not path.exists():
        return None
    access = runtime_control.load_versioned_record(
        lib.load_unique_json(path), expected_schema=schema, for_mutation=True,
    )
    if access.migrated:
        _runtime_write(root, name, access.record)
    return access.record


def _runtime_run_id(root: Path, events: list[dict]) -> str:
    seed = next((event.get("hash") for event in events if isinstance(event.get("hash"), str)), None)
    seed = seed or hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    return f"run-{seed[:32]}"


def _event_datetime(value: object, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return fallback


def _sync_performance_holds(history: dict, events: list[dict]) -> dict:
    """Import persisted human pauses once; concurrent background work still counts."""
    updated = deepcopy(history)
    episode = updated["episodes"][-1]
    episode_start = _event_datetime(episode["started_at"], datetime.now(timezone.utc))
    existing = {item["hold_id"]: item for item in episode["holds"]}
    open_holds: list[tuple[str, datetime, str]] = []
    completed: list[dict] = []
    for index, event in enumerate(events):
        kind = event.get("kind")
        at = _event_datetime(event.get("at"), episode_start)
        if at < episode_start:
            continue
        if kind == "human_pause_started":
            hold_id = f"pilot-{str(event.get('hash') or index)[:64]}"
            open_holds.append((hold_id, at, runtime_control.content_hash({
                "kind": kind, "at": at.isoformat(), "by": event.get("by"),
            })))
        elif kind == "human_pause_ended" and open_holds:
            hold_id, started, evidence_hash = open_holds.pop(0)
            completed.append({"hold_id": hold_id, "kind": "pilot", "started_at": started.isoformat(),
                              "ended_at": at.isoformat(), "evidence_hash": evidence_hash})
    for hold_id, started, evidence_hash in open_holds:
        completed.append({"hold_id": hold_id, "kind": "pilot", "started_at": started.isoformat(),
                          "ended_at": None, "evidence_hash": evidence_hash})
    for item in completed:
        prior = existing.get(item["hold_id"])
        if prior is None:
            episode["holds"].append(item)
            existing[item["hold_id"]] = item
        elif prior.get("ended_at") is None and item.get("ended_at") is not None:
            # A prior refresh persisted the open hold.  Reconcile the later
            # end event into that same durable row instead of ignoring it as
            # a duplicate and excluding the rest of the run forever.
            prior["ended_at"] = item["ended_at"]
    runtime_control.validate_performance_history(updated)
    return updated


def _performance_journal(root: Path, run_id: str) -> tuple[list[dict], str | None]:
    """#385: this run's timeline entries and the chain head, from the journal.

    The timeline lives in its own append-only, hash-chained journal, never
    in the run's event ledger, so refreshing the clock (which every command
    and every dashboard snapshot does) leaves the ledger untouched.
    """
    path = _runtime_path(root, PERFORMANCE_TIMELINE_JOURNAL)
    if not path.exists():
        return [], None
    lines = path.read_text(encoding="utf-8").splitlines()
    timeline, head, _refused = runtime_control.accepted_timeline(lines, run_id)
    return timeline, head


def _read_performance_record(root: Path) -> dict | None:
    """The durable clock, or None when it is missing or fails its integrity check.

    #385: an unparsable or invalid record, or a legacy record carrying a
    signature that does not verify, is rebuilt from the timeline journal. An
    unsigned legacy record and an unknown version keep their read-only
    refusal and are never overwritten.
    """
    path = _runtime_path(root, PERFORMANCE_RECORD)
    if not path.exists():
        return None
    try:
        raw = lib.load_unique_json(path)
    except lib.HandsoffError:
        return None
    try:
        access = runtime_control.load_versioned_record(
            raw, expected_schema="handsoff.performance_history", for_mutation=True,
        )
    except runtime_control.MutationRefused:
        if isinstance(raw, dict) and raw.get("schema") is None and raw.get("version") is None and "_auth" in raw:
            return None
        raise
    except runtime_control.SchemaError:
        return None
    if access.migrated:
        _runtime_write(root, PERFORMANCE_RECORD, access.record)
    return access.record


def _rebuild_performance_history(root: Path, run_id: str, events: list[dict], status: dict,
                                 now: datetime) -> dict:
    """#385: replay the timeline journal, so a lost record keeps its elapsed time."""
    timeline, _head = _performance_journal(root, run_id)
    if timeline:
        try:
            return runtime_control.reconstruct_performance_history(run_id, timeline, now=now)
        except runtime_control.SchemaError:
            pass
    started = _event_datetime(events[0].get("at") if events else status.get("updated_at"), now)
    return runtime_control.new_performance_history(run_id, "episode-1", now=min(started, now))


def _append_performance_timeline(root: Path, history: dict, *, lock_held: bool) -> None:
    """#385: journal each transition, hold and sleep once, keyed by episode, kind and hold id."""
    key = runtime_control.performance_timeline_key
    run_id = history["run_id"]
    entries = runtime_control.performance_timeline(history)
    timeline, _head = _performance_journal(root, run_id)
    if {key(entry) for entry in entries} <= {key(entry) for entry in timeline}:
        return

    def write() -> None:
        current, head = _performance_journal(root, run_id)
        recorded = {key(entry) for entry in current}
        for entry in entries:
            if key(entry) not in recorded:
                recorded.add(key(entry))
                line = runtime_control.chain_timeline_entry(run_id, head, entry)
                _runtime_append_line(root, PERFORMANCE_TIMELINE_JOURNAL, line)
                head = line["hash"]

    if lock_held:
        write()
    else:
        with lib.project_lock(root):
            write()


def refresh_performance_state(
    root: Path,
    *,
    status: dict | None = None,
    events: list[dict] | None = None,
    metrics: dict | None = None,
    now: datetime | None = None,
    persist: bool = True,
    lock_held: bool = False,
    cfg: dict | None = None,
) -> dict:
    """Refresh durable 90/120-minute state and return content-free telemetry.

    `lock_held` says the caller already holds project_lock, which the
    timeline append otherwise takes for itself.
    """
    root = Path(root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = cfg if cfg is not None else lib.load_config(root)  # #393: Fleet passes the config it parsed
    status = status or lib.load_unique_json(lib.status_path(root, cfg))
    events = events if events is not None else lib.read_events(root, cfg)
    run_id = _runtime_run_id(root, events)
    history = _read_performance_record(root)
    if history is None or history.get("run_id") != run_id:
        history = _rebuild_performance_history(root, run_id, events, status, now)
    history = _sync_performance_holds(history, events)
    live_sessions = [
        {"operation_id": session_id, "kind": "agent", "location": "local",
         "cancellable": True, "bounded": True, "state": "running"}
        for session_id, session in (status.get("agent_sessions") or {}).items()
        if isinstance(session, dict) and session.get("state") in getattr(lib, "AGENT_SESSION_LIVE_STATES", set())
    ]
    history, decision = runtime_control.transition_performance(history, now=now, in_flight=live_sessions)
    auto_resumed = None
    if persist:
        _runtime_write(root, PERFORMANCE_RECORD, history)
        _append_performance_timeline(root, history, lock_held=lock_held)
        history, auto_resumed = _auto_resume_performance(root, cfg, history, events, now, lock_held=lock_held)
        if auto_resumed is not None:
            decision = {**decision, "action": "performance_auto_resumed", "block_new_work": False}
    episode = history["episodes"][-1]
    active_seconds = runtime_control.episode_active_seconds(episode, now=now)
    progress = max(0, min(100, int(float(status.get("progress", 0) or 0))))
    forecast_total = (active_seconds * 100 / progress) if progress else None
    forecast_remaining = max(0.0, forecast_total - active_seconds) if forecast_total is not None else None
    phase_seconds = (metrics or {}).get("phase_seconds") or {}
    bottleneck = None
    if phase_seconds:
        phase, seconds = max(phase_seconds.items(), key=lambda item: float(item[1] or 0))
        bottleneck = {"phase": str(phase), "seconds": float(seconds or 0)}
    return {
        "schema": "handsoff.performance_view", "version": 1,
        "run_id": run_id, "episode_id": episode["episode_id"], "state": episode["state"],
        "overall_percent": progress, "active_seconds": active_seconds,
        "warning_seconds": 90 * 60, "pause_seconds": 120 * 60,
        "warning_at": episode["warning_at"], "paused_at": episode["paused_at"],
        "block_new_work": decision["block_new_work"], "transition": decision["action"],
        "forecast_total_seconds": forecast_total, "forecast_remaining_seconds": forecast_remaining,
        "forecast_variance_seconds": (forecast_total - 120 * 60) if forecast_total is not None else None,
        "bottleneck": bottleneck, "breaches": history["breaches"],
        "pause": performance_pause_view(episode, active_seconds),
        "auto_resume": _auto_resume_view(validated_auto_resume_decision(root, cfg)),
        "auto_resumed": auto_resumed,
    }


#: #414: the ledger event carrying the Pilot's standing auto-resume decision
PERFORMANCE_AUTO_RESUME_EVENT = "performance_auto_resume_decided"
PERFORMANCE_RESUME_COMMAND = 'handsoff performance-resume --by <pilot> --reason "<why>"'


def performance_auto_resume_decision(events: list[dict]) -> dict | None:
    """#414: the standing decision, read from the hash-chained ledger only
    (never from status, so editing state cannot grant it): the last
    performance_auto_resume_decided event when it turned auto-resume on."""
    for event in reversed(events or []):
        if isinstance(event, dict) and event.get("kind") == PERFORMANCE_AUTO_RESUME_EVENT:
            return dict(event) if event.get("auto_resume") is True else None
    return None


def validated_auto_resume_decision(root: Path, cfg: dict) -> dict | None:
    """#414 review: the standing decision only from a ledger whose hash chain
    validates; a break, an unchained line or a missing head reads as none,
    for the clock and for what status and Mission Control show alike."""
    chain_problems, _ = lib.event_log_chain_errors(root, cfg)
    if chain_problems:
        return None
    return performance_auto_resume_decision(lib.read_events(root, cfg))


def _auto_resume_view(standing: dict | None) -> dict | None:
    if standing is None:
        return None
    return {"decision_id": standing.get("decision_id"), "by": standing.get("by"), "at": standing.get("at")}


def performance_pause_view(episode: dict, active_seconds: float) -> dict | None:
    """#414: what status and Mission Control show while paused: since when,
    the active time that tripped it, and the command that resumes it."""
    if episode.get("state") != "paused_for_performance_review":
        return None
    try:  # the active time at the pause, which is what tripped it
        active_seconds = runtime_control.episode_active_seconds(
            episode, now=_event_datetime(episode.get("paused_at"), datetime.now(timezone.utc)))
    except runtime_control.RuntimeControlError:
        pass
    return {"episode_id": episode["episode_id"], "since": episode.get("paused_at"),
            "active_seconds": active_seconds, "resume_command": PERFORMANCE_RESUME_COMMAND}


def _auto_resume_performance(root: Path, cfg: dict, history: dict, events: list[dict], now: datetime,
                             *, lock_held: bool) -> tuple[dict, dict | None]:
    """#414: resume a pause that began after the Pilot's standing decision.

    The resume is the same runtime-control transition performance-resume
    makes, bound to the paused episode's hash, and the ledger records a
    performance_auto_resumed event naming the decision. A pause that began
    before the decision is left for an explicit performance-resume."""
    episode = history["episodes"][-1]
    if episode["state"] != "paused_for_performance_review":
        return history, None
    # the decision counts only from a ledger whose hash chain validates: a
    # break, an unchained line or a missing head fails closed (no resume),
    # and the events read are the validated file's, not a caller's copy
    standing = validated_auto_resume_decision(root, cfg)
    if standing is None or not isinstance(standing.get("decision_id"), str):
        return history, None
    paused_at = _event_datetime(episode.get("paused_at"), now)
    if _event_datetime(standing.get("at"), now) > paused_at:
        return history, None
    resume = {
        "decision_id": f"auto-{episode['episode_id']}", "action": "resume",
        "actor": str(standing.get("by") or "pilot"),
        "reason": f"standing decision {standing['decision_id']}",
        "evidence_hash": runtime_control.content_hash(episode), "at": now.isoformat(),
    }
    try:
        resumed = runtime_control.resume_performance(
            history, resume, f"episode-{len(history['episodes']) + 1}")
    except runtime_control.RuntimeControlError:
        return history, None
    record = {"decision_id": standing["decision_id"], "episode_id": episode["episode_id"],
              "new_episode_id": resumed["episodes"][-1]["episode_id"]}

    def write() -> bool:
        # a concurrent refresh may have resumed this pause first
        current = _read_performance_record(root)
        if current is not None and current["episodes"][-1]["episode_id"] != episode["episode_id"]:
            return False
        _runtime_write(root, PERFORMANCE_RECORD, resumed)
        _append_performance_timeline(root, resumed, lock_held=True)
        lib.commit(root, cfg, event_kind="performance_auto_resumed",
                   event_message=(f"Performance pause {episode['episode_id']} resumed by the Pilot's "
                                  f"standing decision {standing['decision_id']}"),
                   **record)
        return True

    if lock_held:
        written = write()
    else:
        with lib.project_lock(root):
            written = write()
    if not written:
        current = _read_performance_record(root)
        return (current if current is not None else history), None
    return resumed, record


def performance_mutation_refusal(root: Path, operation: str) -> str | None:
    """Return the deterministic pause refusal used by CLI and dashboard launchers."""
    try:
        view = refresh_performance_state(root)
    except (lib.HandsoffError, OSError):
        return None
    if not view["block_new_work"]:
        return None
    try:
        history = _runtime_read(root, PERFORMANCE_RECORD, "handsoff.performance_history")
        if history is None:
            return None
        runtime_control.require_performance_operation(history, performance_operation(operation))
    except runtime_control.MutationRefused:
        return (f"{operation} is blocked: active run time reached 120 minutes and the run is "
                "paused_for_performance_review; record an explicit performance-resume decision")
    except (runtime_control.RuntimeControlError, lib.HandsoffError, OSError):
        return None
    return None


def paused_verify_refusal(root: Path, launched: list[str], use_cache: bool) -> str | None:
    """#414: while paused, verify may bind results whose cache key matches
    and run nothing. None when the run is not paused."""
    try:
        view = refresh_performance_state(root)
    except (lib.HandsoffError, OSError):
        return None
    if not view["block_new_work"]:
        return None
    reason = ("this verify runs every command (--no-cache, a baseline or a repeat)" if not use_cache
              else f"{len(launched)} command(s) have no cached result: {', '.join(launched)}")
    return (f"verify is blocked: the run is paused_for_performance_review and {reason}; "
            "only a cache-only verify binds while paused. Record an explicit performance-resume "
            "decision to run checks")


def _runtime_snapshot(status: dict, events: list[dict], performance: dict) -> dict:
    terminal = status.get("status") == "complete" and int(status.get("progress", 0) or 0) >= 100
    live = [item for item in (status.get("agent_sessions") or {}).values()
            if isinstance(item, dict) and item.get("state") in getattr(lib, "AGENT_SESSION_LIVE_STATES", set())]
    failures = [item for item in (status.get("agent_failures") or {}).values() if isinstance(item, dict)]
    regressions = status.get("regression_requests") or []
    gate = "performance_pause" if performance["block_new_work"] else (
        "regression" if any(item.get("state") in {"accepted", "launched"} for item in regressions if isinstance(item, dict)) else "none"
    )
    worker_state = "healthy" if live else ("failed" if failures else "quiet")
    if terminal:
        worker_state = "none"
    return {
        "schema": "handsoff.run_snapshot", "version": 1, "run_id": performance["run_id"],
        "state": "completed" if terminal else ("blocked" if status.get("status") == "blocked" else "in_progress"),
        "percent": performance["overall_percent"], "verified_complete": terminal, "gate": gate,
        "worker_state": worker_state, "failure_category": failures[-1].get("category") if failures else None,
        "recovery_attempts": len(status.get("recovery_attempts") or []),
        "escalation_recorded": bool(status.get("escalation")),
        "result_adoptable": any(isinstance(item.get("result"), dict) for item in failures),
    }


def _criterion(acceptance: dict, criterion_id: str) -> dict | None:
    return next((c for c in acceptance.get("criteria", []) if c.get("id") == criterion_id), None)


def _most_recent_kind(events: list[dict], kinds: set[str]) -> str | None:
    """Scan the event log backwards for the latest event whose kind is one
    of `kinds`, ignoring everything else in between. Used to tell an
    open start/end pair (background-wait, human-pause, a still-pending
    design approval request) apart from a closed one, without a second
    piece of state to keep in sync with the log."""
    for event in reversed(events):
        if event.get("kind") in kinds:
            return event["kind"]
    return None


def _invalidate_decisions(status: dict, *, rollback_to: int = 5, invalidate_design: bool = False) -> list[str]:
    """Any acceptance mutation makes earlier review/deployment/live decisions
    stale. `invalidate_design=True` additionally clears design_approved and,
    for a flagged run (requires_design_approval) that had advanced to phase
    3+, forces it back to phase 2 -- landing at phase 3+ with a cleared
    approval would otherwise be an immediately-blocked state, which no
    other rollback in this function ever produces. Only the three
    criterion-registry-mutating callers (criterion-add/update/remove) pass
    this; the three evidence-recording callers (verify, record-evidence,
    record-symptom-resolved) do not, since they change nothing about WHAT
    is being asked for, only whether it has been proven -- an already-
    approved design must not be forced back into re-approval just because
    evidence was attached to it."""
    revoked = []
    for key in ("review", "deployment_approved", "live_verification_id"):
        if status.get(key) is not None:
            revoked.append(key)
    status["review"] = None
    status["reviewed_by"] = None
    status["deployment_approved"] = None
    status["live_verification_id"] = None
    if status.get("phase_number", 1) >= 6:
        status["phase_number"] = rollback_to
        status["phase"] = lib.PHASES[rollback_to]
        status["status"] = "in_progress"
        status["progress"] = min(status.get("progress", 0), 40 if rollback_to == 4 else 50)
        # #110: a rolled-back run must not keep telling the Pilot to deploy.
        status["next_action"] = lib.NEXT_ACTION_DEFAULTS[rollback_to]
    if invalidate_design:
        for key in ("design_approved", "design_review", "design_proposal"):
            if status.get(key) is not None:
                revoked.append(key)
        if "design_review" in status or status.get("requires_design_review"):
            status["design_review"] = None
        status["design_approved"] = None
        status["design_proposal"] = None
        if (status.get("requires_design_approval") or status.get("requires_design_review")) \
                and status.get("phase_number", 1) >= 3:
            status["phase_number"] = 2
            status["phase"] = lib.PHASES[2]
            status["status"] = "in_progress"
            status["progress"] = min(status.get("progress", 0), 20)
    if status.get("phase_number") == 2:
        status["next_action"] = "Architect revises the design and requests review again"
    return revoked


def _reviewed_design_hash(status: dict) -> str | None:
    """#411: the design a review judged: the recorded proposal's hash, else
    the approved design's, else None (a run that never had a design)."""
    for key in ("design_proposal", "design_approved"):
        value = status.get(key)
        if isinstance(value, dict) and value.get("design_hash"):
            return value["design_hash"]
    return None


def _reviewed_binding(root: Path, cfg: dict, status: dict, acceptance: dict) -> dict:
    """#411: what a review certified: the repository digest of the tree it
    reviewed, the criterion specifications hash (lib.design_hash: specs
    only, never evidence ids) and the design hash. The digest's per-path
    snapshot is kept so a later rollback can name the changed paths."""
    digest = lib.repository_digest(root, cfg)
    _write_digest_snapshot(root, cfg, digest)
    return {"digest": digest, "specs_hash": lib.design_hash(acceptance.get("criteria", [])),
            "design_hash": _reviewed_design_hash(status)}


def _write_digest_snapshot(root: Path, cfg: dict, digest: str, *, replace: bool = False) -> None:
    digest_dir = root / ".handsoff-digests"
    digest_dir.mkdir(parents=True, exist_ok=True)
    snapshot = digest_dir / f"{digest}.json"
    if replace or not snapshot.exists():
        lib.atomic_write_json(snapshot, {"digest": digest, "entries": lib.repository_digest_entries(root, cfg)})
    snapshots = sorted(digest_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snapshots[16:]:
        old.unlink()


def _changed_paths_since(root: Path, cfg: dict, digest: str | None) -> list[str] | None:
    """Paths whose content differs from the snapshot of `digest`, or None
    when no snapshot was kept."""
    snapshot = root / ".handsoff-digests" / f"{digest}.json"
    if not digest or not snapshot.is_file():
        return None
    try:
        old = lib.load_unique_json(snapshot).get("entries", {})
    except (OSError, lib.HandsoffError, ValueError):
        return None
    current = lib.repository_digest_entries(root, cfg)
    return sorted(path for path in {*old, *current} if old.get(path, "<absent>") != current.get(path, "<absent>"))


def _review_retention_refusal(root: Path, cfg: dict, status: dict, acceptance: dict, *,
                              digest_before: str, digest_after: str, rechecked: list[dict]) -> str | None:
    """#411: why the recorded review cannot stand after new evidence, or
    None when it can: the completion-time digest, the specs hash and the
    design hash all equal the reviewed binding, no command edited the tree
    while it ran, and every re-checked criterion still passes."""
    review = status.get("review")
    binding = review.get("reviewed_binding") if isinstance(review, dict) else None
    if not isinstance(binding, dict) or not binding.get("digest") or not binding.get("specs_hash"):
        return "the review records no reviewed binding"
    if digest_before != digest_after:
        paths = _changed_paths_since(root, cfg, digest_before)
        return ("a command edited the tree while it ran"
                + (f": {', '.join(paths[:16])}" if paths else ""))
    if digest_after != binding["digest"]:
        paths = _changed_paths_since(root, cfg, binding["digest"])
        return ("the tree changed since the review"
                + (f": {', '.join(paths[:16])}" if paths else " (no snapshot to name the paths)"))
    if lib.design_hash(acceptance.get("criteria", [])) != binding["specs_hash"]:
        return "the criterion specifications changed since the review"
    if _reviewed_design_hash(status) != binding.get("design_hash"):
        return "the design changed since the review"
    failed = [c.get("id") for c in rechecked if c.get("state") != "passing"]
    if failed:
        return f"a re-checked criterion is not passing: {', '.join(failed)}"
    return None


def _live_binding_holds(root: Path, cfg: dict, live_id: str, acceptance: dict, digest: str) -> bool:
    """#411: the recorded live run still stands for this tree: it carries
    the completion digest it ran on, equal to `digest`, its criterion
    specifications, configuration and [checks].env are current."""
    records, _problems = lib.load_verifications(root, cfg)
    record = next((r for r in records if isinstance(r, dict) and r.get("run_id") == live_id), None)
    return (isinstance(record, dict) and record.get("kind") == "live" and record.get("ok") is True
            and bool(record.get("repository_digest")) and record.get("repository_digest") == digest
            and lib.live_record_specs_current(record, acceptance.get("criteria", []))
            and record.get("config_hash") == lib.config_hash(cfg)
            and (record.get("env") or {}) == lib.recorded_check_env(cfg))


def _retain_or_invalidate(root: Path, cfg: dict, status: dict, acceptance: dict, *,
                          digest_before: str, digest_after: str, rechecked: list[dict]) -> dict | None:
    """#411: after verify or record-evidence, keep the review, the phase,
    the progress and the deployment approval when the reviewed binding
    still holds, rebinding their acceptance hashes to the new evidence in
    the same commit; otherwise roll back as _invalidate_decisions always
    has. Returns the outcome to record (None when there was no review)."""
    review = status.get("review")
    if not isinstance(review, dict):
        _invalidate_decisions(status)
        return None
    reason = _review_retention_refusal(root, cfg, status, acceptance, digest_before=digest_before,
                                       digest_after=digest_after, rechecked=rechecked)
    if reason is not None:
        # named in the event and the command's output; next_action stays the
        # rolled-back phase's own default (#110)
        _invalidate_decisions(status)
        return {"retained": False, "reason": reason}
    binding = review["reviewed_binding"]
    previous = review.get("acceptance_hash")
    current = lib.acceptance_hash(acceptance.get("criteria", []))
    review["acceptance_hash"] = current
    deployment = status.get("deployment_approved")
    if isinstance(deployment, dict):
        deployment["acceptance_hash"] = current
    closed = [a for a in status.get("review_attempts") or [] if isinstance(a, dict) and a.get("closed_at")]
    if closed and closed[-1].get("disposition") == "approved" and closed[-1].get("acceptance_hash") == previous:
        closed[-1]["acceptance_hash"] = current
    # A live verification is evidence with its own binding (the criterion
    # specifications, the tree digest and the [checks].env it ran under),
    # never the evidence-bearing acceptance hash: re-verifying the same tree
    # keeps it; when that binding moved, the live run alone is stale and is
    # re-run with verify-live.
    live_cleared = None
    if status.get("live_verification_id") and not _live_binding_holds(
            root, cfg, status["live_verification_id"], acceptance, digest_after):
        live_cleared = status["live_verification_id"]
        status["live_verification_id"] = None
        if int(status.get("phase_number") or 0) >= 8:
            # Phase 8 stands on the live run; the run waits at Phase 7 for it
            status["phase_number"] = 7
            status["phase"] = lib.PHASES[7]
            status["status"] = "in_progress"
            status["progress"] = min(status.get("progress", 0), 90)
            status["next_action"] = lib.phase_next_action(7, status, cfg)  # #434
    return {"retained": True, "digest": binding["digest"], "specs_hash": binding["specs_hash"],
            "design_hash": binding.get("design_hash"), "previous_acceptance_hash": previous,
            "acceptance_hash": current, "live_verification_cleared": live_cleared}


def _retention_events(outcome: dict | None) -> list[dict]:
    if not outcome or not outcome.get("retained"):
        return []
    return [{"kind": "review_retained",
             "message": "Re-verification of the reviewed tree kept the review",
             **{key: outcome[key] for key in ("digest", "specs_hash", "design_hash",
                                              "previous_acceptance_hash", "acceptance_hash",
                                              "live_verification_cleared")}}]


def _approval_edit_guard(status: dict, revoke: bool) -> bool:
    """Require an explicit opt-in before registry edits destroy an approved design.

    The guard runs before any acceptance mutation, preserving the exact
    refusal symptom and making a declined edit observably no-op.
    """
    if status.get("design_approved") and not revoke:
        print("SHIP_FEATURE_BLOCKED: design is approved; this edit would revoke design approval, the independent review, and the proposal. Use amendment-open for a reviewed change, or pass --revoke-approval to proceed deliberately")
        return True
    return False


def _durable_results(results: list[dict]) -> list[dict]:
    """Persist redacted tails only for failed checks, never for successes.

    Failed checks need bounded diagnosis, but successful output needs no
    diagnosis and may echo secrets, so successful records stay tail-less.
    """
    durable = []
    for result in results:
        item = {k: v for k, v in result.items() if k != "output_tail"}
        if result.get("exit_code", 0) != 0 or result.get("timed_out"):
            tail = result.get("output_tail", "")
            item["output_tail"] = lib.redact_output_text(tail)[-lib.CHECK_OUTPUT_TAIL_CHARS:]
        durable.append(item)
    return durable


def _audit_errors(root: Path, cfg: dict, status: dict, records: list[dict],
                  verification_problems: list[str]) -> list[str]:
    problems = [f"event log: {p}" for p in lib.verify_event_log(root, cfg)]
    problems += [f"verification ledger: {p}" for p in verification_problems]
    actual_head = records[-1].get("hash") if records else "GENESIS"
    if status.get("verification_head") != actual_head:
        problems.append("verification ledger: tail does not match its anchored head")
    return problems


def _print_audit_block(problems: list[str]) -> int:
    print("SHIP_FEATURE_BLOCKED")
    print("\n".join(f"- {problem}" for problem in problems))
    return 1


def _write_missing_pin(root) -> str | None:
    """#185: a root served by the installed engine needs .handsoff-version,
    and every read of the engine identity fails without it (a fresh git
    worktree has no untracked files, so it has no pin). init is the one
    command that can put it there: when the root is not a runtime drop-in
    and no pin exists, write the running engine's compatible line. A root
    with a pin, or one carrying its own engine, is untouched."""
    pin_path = root / lib.VERSION_PIN_FILE
    if pin_path.exists() or lib._looks_like_runtime_drop_in(root):
        return None
    version = lib.engine_manifest_version()
    match = re.match(r"v?(\d+)\.(\d+)\.", version or "")
    if not match:
        return None
    pin = f"{match.group(1)}.{match.group(2)}.*"
    lib._atomic_write_text(pin_path, pin + "\n")
    print(f"HANDSOFF_PIN_WRITTEN: {pin} ({pin_path.name} was missing; engine {version})")
    return pin


def cmd_init(args) -> int:
    if not args.feature or not args.feature.strip():
        print("SHIP_FEATURE_BLOCKED: feature must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        risk_class = lib.classify_adaptive_risk(args.risk_class or "routine")
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    review_adoption = None
    if getattr(args, "lane", "full") == "review":
        ref = args.adopt if isinstance(args.adopt, str) else None
        if not ref:
            print("SHIP_FEATURE_BLOCKED: review lane requires --adopt REF")
            return 1
        acceptance_file = lib.acceptance_path(root, cfg)
        try:
            existing_acceptance = lib.load_unique_json(acceptance_file)
        except lib.HandsoffError:
            print("SHIP_FEATURE_BLOCKED: review lane requires criteria already on file")
            return 1
        if not existing_acceptance.get("criteria"):
            print("SHIP_FEATURE_BLOCKED: review lane requires criteria already on file")
            return 1
        try:
            resolved = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                                     cwd=str(root), text=True, capture_output=True, check=False, timeout=60)
            if resolved.returncode != 0 or not resolved.stdout.strip():
                raise ValueError("ref does not resolve to a commit")
            sha = resolved.stdout.strip()
            author = subprocess.run(["git", "show", "-s", "--format=%an <%ae>", sha],
                                    cwd=str(root), text=True, capture_output=True, check=False, timeout=60)
            commit_author = author.stdout.strip() if author.returncode == 0 else ""
            if not commit_author or commit_author == "<>" or not re.search(r"\S", commit_author):
                raise ValueError("commit has no usable author identity")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"SHIP_FEATURE_BLOCKED: review lane adoption refused: {exc}")
            return 1
        review_adoption = {"ref": ref, "sha": sha, "commit_author": commit_author,
                           "adopting_actor": (args.by or "").strip()}
        if not review_adoption["adopting_actor"]:
            print("SHIP_FEATURE_BLOCKED: review lane adoption requires --by")
            return 1
    source_design = None
    if getattr(args, "from_design", None):
        try:
            source_design = _read_design_take_up(Path(args.from_design).resolve())
        except lib.HandsoffError as exc:
            print(f"SHIP_FEATURE_BLOCKED: {exc}")
            return 1
    # #400: a qualified owner/name#N (or issue URL) of the run's own
    # repository is its issue-N; another repository's ticket is refused,
    # since a run's issue items belong to its own repository. The title is
    # checked as well as every --item.
    import handsoff_fleet_signals
    own_repo = handsoff_fleet_signals.origin_repo(root)
    foreign = lib.foreign_issue_refs([args.feature, *(args.item or [])], own_repo)
    if foreign:
        print(f"SHIP_FEATURE_BLOCKED: {foreign[0]} is an issue of {foreign[0].split('#')[0]}, not of this "
              f"run's repository {own_repo}; a run's issue items belong to its own repository")
        return 1
    sp, ap = lib.status_path(root, cfg), lib.acceptance_path(root, cfg)
    now = datetime.now(timezone.utc).isoformat()
    # Locked for the same reason advance/deployment-gate are: init writes
    # two files and appends an event, and an unlocked append_event racing
    # another process's append_event forks the hash chain (round 2 finding).
    with lib.project_lock(root):
        artifacts = (sp, ap, lib.event_log_path(root, cfg), lib.verification_log_path(root, cfg),
                     lib.event_head_path(root))
        existing = [path.name for path in artifacts if path.exists()]
        if review_adoption and existing == [lib.acceptance_path(root, cfg).name]:
            existing = []
        if existing:
            retired = lib.retire_finished_run(root, cfg)
            if retired is None:
                print(f"HANDSOFF_INIT_SKIPPED: existing Handsoff artifacts at {root}: {', '.join(existing)}")
                return 1
            print(f"HANDSOFF_RETIRED: {retired}")
        pin_written = _write_missing_pin(root)  # #185
        # #418: --item names the scope, so the registry starts empty and
        # design-propose / advance 2 refuse until criteria are added; without
        # --item the placeholder still marks where the first one goes.
        acceptance = (deepcopy(existing_acceptance) if review_adoption else {
            "feature": args.feature,
            "criteria": [] if args.item else [{
                "id": "REQ-001", "type": "primary_fix", "requirement": lib.PLACEHOLDER_REQUIREMENT,
                "verification": "automated", "tests": list(lib.PLACEHOLDER_TESTS), "evidence": [], "state": "failing",
            }],
        })
        if review_adoption:
            acceptance["feature"] = args.feature
        if source_design is not None:
            acceptance["criteria"] = source_design["criteria"]
            if source_design.get("items") is not None:
                acceptance["work_items"] = source_design["items"]
        # A carried review stays valid only while the policy and scope it was bound to still hold;
        # a take-up that changes either earns a new review rather than inheriting the old one.
        if args.item:
            # #400: the scope was named; the title never derives items later.
            acceptance["work_items_explicit"] = True
        if source_design is None or args.item or not acceptance.get("work_items"):
            acceptance["work_items"] = lib.derive_work_item_registry(
                acceptance, cfg, now=now, explicit_items=args.item, repo=own_repo,
            )
        # #400: the repository the ticket claim below is made in, recorded on
        # the run's own registry (never the shared register) so a later
        # origin change cannot release the claim. None when there is no origin.
        acceptance["repository"] = own_repo
        numbers = {item["number"] for item in acceptance["work_items"]
                   if item.get("kind") == "issue" and isinstance(item.get("number"), int)}
        adopted = {"adopted_from": []}
        if numbers and lib.feature_enabled(cfg, "ticket_lock"):
            # #166: one ticket, one run. The check and the registration are
            # one transaction under the register lock; a refusal leaves no
            # status file behind.
            import handsoff_fleet as fleet
            try:
                adopted = fleet.claim_tickets(root, numbers, adopt=bool(getattr(args, "adopt", False)),
                                              repo=own_repo)
            except lib.HandsoffError as exc:
                print(f"SHIP_FEATURE_BLOCKED: {exc}")
                return 1
        status = {
            "feature": args.feature, "phase_number": 1, "phase": lib.PHASES[1], "progress": 0,
            "status": "in_progress", "updated_at": now, "last_heartbeat_at": None,
            "last_heartbeat_owner": None, "background_wait": None,
            "next_action": lib.NEXT_ACTION_DEFAULTS[1],
            "design_round": 0, "review_round": 0, "retry_count": 0, "summary": "", "reassurance": "",
            "legacy_review_round_offset": 0, "review_attempts": [],
            "review_cap_overrides": [], "escalation": None,
            "recovery_attempts": [], "recovery_lease": None,
            "regression_requests": [],
            "active_work_item": None,
            "implemented_by": None, "reviewed_by": None, "deployment_approved": None,
            "approval_posture": {
                "profile": cfg.get("execution_profile", "safe"),
                "require_design_approval": bool(cfg.get("require_design_approval", True)),
                "require_deployment_approval": bool(cfg.get("deployment_requires_explicit_approval", True)),
                "waivers_active": (not bool(cfg.get("require_design_approval", True))
                                   or not bool(cfg.get("deployment_requires_explicit_approval", True))),
            },
            # #159: a project may waive the Pilot's design click in handsoff.toml;
            # the independent design review below is required regardless.
            "requires_design_approval": bool(cfg.get("require_design_approval", True)), "design_approved": None,
            "requires_design_review": True, "design_review": None,
            "design_review_attempts": 0, "design_review_authorization": None,
            "amendment": None, "amendments": [], "pending_questions": [],
            "review": None, "live_verification_id": None, "original_symptom_evidence_id": None,
            "verification_head": "GENESIS",
            "requirement_coverage": {"passing": 0, "failing": len(acceptance["criteria"]), "not_tested": 0, "blocked": 0,
                                      "original_symptom_resolved": False},
            "reviewer_checklist": {"symptom_reproduced": "not_verifiable", "symptom_resolved": "not_verifiable",
                                   "all_criteria_verified": "no", "evidence_attached": "no"},
            "events": [],
        }
        status["risk_class"] = risk_class
        # Routing constraints are a run fact, not a mutable preference. A
        # later settings edit cannot silently re-authorize a denied vendor
        # or model for this mission.
        status["model_policy"] = deepcopy(cfg.get("model_policy", lib.DEFAULT_MODEL_POLICY))
        if source_design is not None:
            status["design_review"] = source_design.get("design_review")
            status["design_approved"] = source_design.get("design_approved")
            status["design_proposal"] = source_design.get("design_proposal")
            # A take-up is a new run: its --lane (defaulting to full) owns
            # execution.  The source lane's waiver is provenance only; it
            # must never make the new run unable to build the design.
            inherited_waived = source_design.get("phases_waived", [])
            if not isinstance(inherited_waived, list) or any(
                    not isinstance(phase, int) for phase in inherited_waived):
                inherited_waived = []
            phase3 = not lib._design_review_errors(status, acceptance, cfg) \
                and not lib._design_errors(status, acceptance, cfg, root)
            phase = 3 if phase3 else 2
            status.update(phase_number=phase, phase=lib.PHASES[phase],
                          progress=30 if phase3 else 20,
                          design_document=Path(args.from_design).name,
                          next_action=lib.NEXT_ACTION_DEFAULTS[phase],
                          taken_up_from={
                              "path": str(Path(args.from_design).resolve()),
                              "sha256": lib._file_sha256(Path(args.from_design).resolve()),
                              "source_design_hash": source_design["design_hash"],
                              "inherited_waived": sorted(inherited_waived),
                          })
        if args.lane == "design":
            status.update(lane="design", phases_run=[1, 2, 3], phases_waived=[4, 5, 6, 7, 8])
        if review_adoption:
            status.update(lane="review", phase_number=5, phase=lib.PHASES[5], progress=50,
                          phases_run=[5, 6], phases_waived=[1, 2, 3, 4, 7, 8],
                          implemented_by=review_adoption["commit_author"],
                          implementation_adopted=review_adoption)
        status["work_item_delivery"] = lib.new_work_item_delivery(
            acceptance["work_items"], "small-fix" if getattr(args, "lane", "full") == "small-fix" else "full",
        )
        waived = [] if status["requires_design_approval"] else [{
            "kind": "design_approval_waived",
            "message": "Pilot design approval waived by handsoff.toml [workflow] require_design_approval = false; "
                       "the independent design review remains required",
                       "config_key": "require_design_approval",
        }]
        posture_event = {
            "kind": "approval_posture_recorded",
            "message": (f"Execution profile {status['approval_posture']['profile']}: "
                        f"design approval {'required' if status['approval_posture']['require_design_approval'] else 'waived'}, "
                        f"deployment approval {'required' if status['approval_posture']['require_deployment_approval'] else 'waived'}"),
            **status["approval_posture"],
        }
        if args.lane == "design":
            lane_actor = args.by.strip() if args.by and args.by.strip() else "unknown"
            waived.append({"kind": "lane_selected", "message": "Design lane selected",
                           "lane": "design", "actor": lane_actor, "by": lane_actor})
        if review_adoption:
            waived.append({"kind": "implementation_adopted", "message": "Implementation adopted for review lane",
                           **review_adoption})
        for previous in adopted["adopted_from"]:
            waived.append({"kind": "work_item_adopted",
                           "message": f"Adopted #{', #'.join(str(n) for n in previous['numbers'])} from a dead run at {previous['root']}",
                           "previous_root": previous["root"], "numbers": previous["numbers"],
                           "previous_phase": previous.get("phase")})
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="initialized", event_message=f"Handsoff initialized for '{args.feature}'",
                  extra_events=waived + ([{"kind": "design_taken_up",
                                          "message": "Design document taken up",
                                          **status["taken_up_from"]}] if source_design is not None else []),
                  project_root=str(root), engine=lib.ledger_engine_identity(root),
                  ticket_lock="evaluated" if lib.feature_enabled(cfg, "ticket_lock") else "disabled",
                  pin_written=pin_written, by=(args.by.strip() if isinstance(args.by, str) and args.by.strip() else None))
        posture_extra = {key: value for key, value in posture_event.items()
                         if key not in {"kind", "message"}}
        lib.commit(root, cfg, event_kind=posture_event["kind"],
                   event_message=posture_event["message"], **posture_extra)
    print(f"HANDSOFF_INITIALIZED: {sp} and {ap}")
    for previous in adopted["adopted_from"]:
        print(f"WORK_ITEM_ADOPTED: #{', #'.join(str(n) for n in previous['numbers'])} from {previous['root']}")
    _warn_live_commands_empty(cfg)
    return 0


LIVE_COMMANDS_EMPTY_WARNING = (
    "HANDSOFF_WARNING: require_live_verification is on and [checks].live_commands is empty; "
    "add the live checks now (a run-mechanics setting, so adding it later never revokes a review)")


def _warn_live_commands_empty(cfg: dict) -> None:
    """#406: say early that verify-live will have nothing to run."""
    if cfg.get("require_live_verification", True) and not cfg.get("live_check_commands"):
        print(LIVE_COMMANDS_EMPTY_WARNING)


def _read_design_take_up(path: Path) -> dict:
    """Read exactly one bounded JSON design block, never page prose."""
    if not path.is_file():
        raise lib.HandsoffError("--from-design file does not exist")
    if path.stat().st_size > 256 * 1024:
        raise lib.HandsoffError("design document exceeds 256 KiB")
    raw = path.read_bytes()
    marker = b'<script type="application/json" id="handsoff-design">'
    starts = [i for i in range(len(raw)) if raw.startswith(marker, i)]
    if not starts:
        raise lib.HandsoffError("design document has no handsoff-design block")
    if len(starts) > 1:
        raise lib.HandsoffError("design document has more than one handsoff-design block")
    begin = starts[0] + len(marker)
    finish = raw.find(b"</script>", begin)
    if finish < 0:
        raise lib.HandsoffError("handsoff-design block is not closed")
    try:
        block = json.loads(raw[begin:finish].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise lib.HandsoffError(f"handsoff-design block has invalid JSON: {exc}") from exc
    if not isinstance(block, dict):
        raise lib.HandsoffError("handsoff-design block must be a JSON object")
    if block.get("schema") != 1:
        raise lib.HandsoffError("handsoff-design block has unknown schema version")
    candidate = {"feature": "taken-up design", "criteria": block.get("criteria")}
    if block.get("items"):
        candidate["work_items"] = block["items"]
    errors = lib.validate_acceptance_schema(candidate)
    if errors:
        raise lib.HandsoffError("design criteria fail acceptance schema: " + "; ".join(errors))
    actual = lib.design_hash(block["criteria"])
    if not isinstance(block.get("design_hash"), str) or block["design_hash"] != actual:
        hashes = block.get("criterion_hashes")
        ids = [c["id"] for c in block["criteria"]
               if isinstance(hashes, dict) and hashes.get(c.get("id")) != lib.criterion_spec_hash(c)]
        if not ids:
            ids = [c["id"] for c in block["criteria"]]
        raise lib.HandsoffError("design_hash mismatch for criteria: " + ", ".join(ids))
    return block


def cmd_status(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        lib.reconcile_gone_sessions(root)  # #420: a gone process never stays live
    except (lib.HandsoffError, OSError, ValueError):
        pass  # the read below reports what is wrong
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
        # #41: the same activity reading the dashboard snapshot carries.
        liveness = lib.liveness_view(status, root, cfg)
        # P1.10: the stall warning is derived from the one session-state
        # projection, as the dashboard does, before its transition is recorded,
        # so a fresh session heartbeat clears it here too.
        session_states = status_session_projection(root, cfg, status)
        if session_states is not None:
            liveness = {**liveness, "stall_warning": projection.projected_stall_warning(
                session_states, liveness["stall_warning"])}
        warning = liveness["stall_warning"]
        lib.record_stall_transition(root, cfg, warning)
        activity = liveness.get("activity_note")
        live = lib.live_status(status, cfg, root)
        budget = lib.design_review_budget(status, cfg)
        reviewer_selection = lib.design_reviewer_selection_view(cfg, status, acceptance)
        log_problems = lib.verify_event_log(root, cfg)
        # #414: the performance pause, shown up front with its resume command
        try:
            performance = refresh_performance_state(root, status=status, cfg=cfg, lock_held=True)
        except (lib.HandsoffError, OSError, ValueError, runtime_control.RuntimeControlError):
            performance = {}
    progress_shown = lib.progress_view(status, acceptance, cfg)  # #415
    print(__import__("json").dumps({
        "performance_pause": performance.get("pause"),
        "performance_auto_resume": performance.get("auto_resume"),
        "root": str(root), "feature": status.get("feature"), "phase": status.get("phase"),
        "engine": lib.runtime_identity(root),
        "phase_number": status.get("phase_number"), "progress": status.get("progress"),
        "gate_progress": lib.gate_progress(status, acceptance),
        "engine_history": lib.engine_history(lib.read_events(root, cfg)),
        "work_item_completion": lib.work_item_completion_lines(
            lib.work_item_checkpoints(status, acceptance, None, verifications, cfg)),
        "status": status.get("status"), "next_action": status.get("next_action"),
        # P2.4: the close outcome with its reason and known risk
        "run_closed": status.get("run_closed"),
        "design_round": status.get("design_round"), "review_round": status.get("review_round"),
        "review_attempts": [{k: item.get(k) for k in ("attempt", "attempt_id", "trigger", "disposition", "reviewer")}
                            for item in (status.get("review_attempts") or [])],
        "effective_max_review_rounds": lib.effective_review_cap(status, cfg)
        if "review_attempts" in status else cfg.get("max_review_rounds"),
        "escalation": status.get("escalation"),
        "usage": lib.usage_totals(status),  # #168
        "design_review": status.get("design_review"),
        "design_review_attempts": budget["attempts"], "design_review_budget": budget,
        "design_reviewer_selection": reviewer_selection,
        "consistency_errors": reviewer_selection.get("consistency_errors", []),
        "amendment": lib.amendment_view(status, acceptance, cfg, verifications),
        "reviewed_by": status.get("reviewed_by"),
        "live_verification_id": status.get("live_verification_id"),
        "verification_runs": len(verifications),
        "validation": "blocked" if errors or log_problems else "valid", "errors": errors,
        "evidence_drift": lib.evidence_drift(root, cfg, acceptance, verifications),
        "criteria": status_criteria(acceptance),  # P1.1
        "session_projection": session_states,  # P1.10
        "stall_warning": warning, "activity_note": activity, "activity": liveness, "live": live,
        "process_signal": liveness["process_signal"],
        "questions": lib.questions_view(status),
        # P1.5: untrusted QA reports no reviewer has mapped; they count for nothing
        "qa_reports_pending": pending_qa_reports(status),
        # #359: every live session by its own id; two implementers may be live
        "live_sessions": [{field: session.get(field) for field in
                           ("session_id", "role", "actor", "state", "started_at", "owned_paths")}
                          for session in lib.live_agent_sessions(status)],
        # #413: a stopped implementer's kept workspace and what it changed
        "kept_workspaces": _kept_workspaces(cfg, status),
        "unattributed_criteria": lib.derive_work_items(status, acceptance, cfg)["unattributed_criteria"],
        "crew": lib.crew_view(cfg),
        "event_log_intact": not log_problems, "event_log_problems": log_problems,
        # #308: the gate state a host polls during an unattended run. Every
        # field a gate path consults is reachable from this payload, declared
        # in bin/handsoff_observability.py and enforced by a test that derives
        # the consulted set from the source. Before this, a host that polled
        # `status` could not tell a deliberate derived view from an omission.
        "review": status.get("review"),
        "risk_class": status.get("risk_class"),
        "updated_at": status.get("updated_at"),
        "regression_requests": status.get("regression_requests") or [],
        "recovery_lease": status.get("recovery_lease"),
        "deployment_approved": status.get("deployment_approved"),
        "design_approved": status.get("design_approved"),
        "design_review_authorization": status.get("design_review_authorization"),
        "implemented_by": status.get("implemented_by"),
        "lane": status.get("lane"),
        "model_policy": status.get("model_policy"),
        "original_symptom_evidence_id": status.get("original_symptom_evidence_id"),
        "requirement_coverage": status.get("requirement_coverage"),
        "verification_head": status.get("verification_head"),
        "work_item_delivery": status.get("work_item_delivery"),
        # #422: an open human pause names itself; a caller that polls status
        # (Sentinel's resume) must not conclude that none is open.
        "human_pause": human_pause_view(status),
        # #415: the progress status, the board and the first HTML all show
        # (lib.progress_view), whether it was set in this phase or derived,
        # and every work item with how it becomes implemented and done
        "display_progress": progress_shown["progress"],
        "progress_source": progress_shown["source"],
        "progress_set_phase": status.get("progress_set_phase"),
        "progress_view": progress_shown,
        "work_item_done_condition": lib.WORK_ITEM_DONE_CONDITION,
        "work_items": [{field: item.get(field) for field in
                        ("id", "status", "required", "criteria", "unmet_criteria",
                         "implemented_at", "implemented_by", "done_when")}
                       for item in lib.derive_work_items(status, acceptance, cfg)["items"]],
    }, indent=2))
    return 1 if errors or log_problems else 0


def status_session_projection(root: Path, cfg: dict, status: dict) -> dict | None:
    """P1.10: the one session-state projection (handsoff_projection.
    session_projection) status reports, or None when it cannot be built, so
    a projection fault never takes `status` down with it."""
    project = getattr(projection, "session_projection", None)
    if project is None:
        return None
    try:
        return project(root, cfg, status, datetime.now(timezone.utc))
    except (lib.HandsoffError, OSError, ValueError, KeyError, TypeError):
        return None


def status_criteria(acceptance: dict) -> list[dict]:
    """P1.1: each criterion's declared outcome, evidence classes and paths,
    and the evidence kinds its passing requires (policy plus classes)."""
    return [{"id": c.get("id"), "state": c.get("state"), "verification": c.get("verification"),
             "outcome": c.get("outcome"), "evidence_classes": list(c.get("evidence_classes") or []),
             "paths": list(c.get("paths") or []),
             "required_evidence": sorted(lib.required_evidence_kinds(c))}
            for c in acceptance.get("criteria", []) if isinstance(c, dict)]


def human_pause_view(status: dict) -> dict | None:
    """#422: who opened the human pause, when, and why; None when none is open."""
    pause = status.get("human_pause")
    if not isinstance(pause, dict):
        return None
    return {"by": pause.get("by"), "at": pause.get("since"), "note": pause.get("note")}


def cmd_validate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
        log_problems = lib.verify_event_log(root, cfg)
    if log_problems:
        errors = errors + [f"event log: {p}" for p in log_problems]
    if errors:
        print("SHIP_FEATURE_INVALID")
        print("\n".join(f"- {x}" for x in errors))
        return 1
    print("SHIP_FEATURE_VALID")
    return 0


_SUPERVISOR_CMD = "handsoff_supervisor.py"
_EMBEDDED_COMMAND = re.compile(r"handsoff_supervisor\.py (.+?)(?= on the tree| or |;|,|$)")


def _workflow_check_command(cid: str, cfg: dict | None) -> str:
    """P1.4: workflow-check for `cid`, or, with no [checks].workflow_commands
    configured, the key to configure first."""
    command = f"{_SUPERVISOR_CMD} workflow-check --criterion {cid} --by ACTOR"
    if cfg is not None and not cfg.get("workflow_commands"):
        return f"set [checks].workflow_commands in handsoff.toml, then {command}"
    return command


def _gate_clearing_command(message: str, phase: int, status: dict, cfg: dict | None = None) -> str | None:
    """#421: the command that clears one unmet gate line. Every gate
    compute_errors emits maps to one (None only for a line no gate wrote).
    The gate text itself is never changed."""
    cmd = f"{_SUPERVISOR_CMD} "
    drift = re.match(r"evidence drift: (\S+) was verified", message)
    if drift:
        return cmd + f"verify --criterion {drift.group(1)} --by ACTOR"
    workflow_drift = re.match(r"evidence drift: (\S+) workflow evidence", message)
    if workflow_drift:
        return _workflow_check_command(workflow_drift.group(1), cfg)
    evidence = re.match(r"evidence gate: passing criterion (\S+) lacks valid (.+) evidence", message)
    if evidence:
        cid, kinds = evidence.group(1), evidence.group(2)
        if "checks" in kinds:
            return cmd + f"verify --criterion {cid} --by ACTOR"
        if "mutation" in kinds:
            return cmd + f"mutation-proof {cid} --by ACTOR"
        if "workflow" in kinds:
            return _workflow_check_command(cid, cfg)
        kind = "browser" if "browser" in kinds else "manual"
        return cmd + f"record-evidence {cid} --kind {kind} --description TEXT --by ACTOR"
    unreasoned = re.match(r"baseline gate: (\S+) says baseline not_applicable without a reason", message)
    if unreasoned:
        return cmd + f"criterion-update {unreasoned.group(1)} --baseline-reason TEXT"
    escalated = re.search(r"until the escalation is cleared by (?:Run )?(\S.*)$", message)
    if message.startswith("escalation gate:") and escalated:
        return cmd + escalated.group(1).strip()
    embedded = _EMBEDDED_COMMAND.search(message)
    if embedded:
        return cmd + embedded.group(1).strip()
    if message.startswith("state gate: Phase 7 requires status"):
        return cmd + "advance 7 --status awaiting_approval"
    if message.startswith("state gate: requirement_coverage"):
        return cmd + "verify --all --by ACTOR"
    if message.startswith(("state gate: status and acceptance describe different features",
                           "verification ledger:")):
        return cmd + "doctor"
    if message.startswith("live gate: Phase 8 requires progress 100"):
        return cmd + "advance 8 100"
    if message.startswith("live gate:"):
        # no successful run, acceptance, policy, [checks].env or rules
        # changed since it, or it preceded the deployment approval
        return cmd + "verify-live --by ACTOR"
    if message.startswith("design gate: a decline is pending"):
        return cmd + "record-design-review --by REVIEWER --architect ARCHITECT --summary TEXT --approve"
    if message.startswith("design gate:"):
        return cmd + "design-approve --by PILOT --architect ARCHITECT --summary TEXT"
    if message.startswith(("design review gate:", "stale proposal:")):
        return cmd + "record-design-review --by REVIEWER --architect ARCHITECT --summary TEXT --approve"
    if message.startswith("amendment gate:"):
        return (cmd + "amendment-review --by REVIEWER --approve --summary TEXT, then "
                + cmd + "amendment-approve --by PILOT")
    if message.startswith("round cap: design_round"):
        return cmd + "config-override --key workflow.max_design_rounds --value N --by PILOT"
    if message.startswith("round cap: review_round"):
        return cmd + "review-cap-override --by PILOT --reason TEXT"
    if message.startswith("review attempt gate:"):
        return cmd + "record-review --by REVIEWER --tests-executed yes"
    if message.startswith("CI:"):
        pr = re.search(r"ci-watch --pr (\S+)", message)
        return cmd + f"ci-watch --pr {pr.group(1) if pr else 'N'} --by ACTOR"
    if message.startswith("symptom gate:"):
        return cmd + "record-symptom-resolved --evidence RUN_ID --by ACTOR"
    if message.startswith(("phase gate:", "progress gate: 95%+", "status gate:")):
        resolved = (status.get("requirement_coverage") or {}).get("original_symptom_resolved") is True
        return cmd + "verify --all --by ACTOR" + (
            "" if resolved else f", then {cmd}record-symptom-resolved --evidence RUN_ID --by ACTOR")
    if message.startswith(("progress gate:", "work items gate:")) and "work item" in message:
        return cmd + "verify --all --by ACTOR"
    if message.startswith("review gate: Phase 6+ requires 'implemented_by'"):
        return cmd + f"advance {phase} --implemented-by ACTOR"
    if message.startswith("review gate:"):
        return cmd + "record-review --by REVIEWER --tests-executed yes"
    if message.startswith("review anchor gate:"):
        # the anchor is written only by a review-lane init that adopts a commit
        return cmd + 'init "FEATURE" --lane review --adopt REF --by ACTOR'
    if message.startswith("deployment gate:"):
        return cmd + "deployment-gate --approve --by PILOT"
    if message.startswith("live gate: Phase 8 requires a successful live") \
            or message.startswith("live gate:") and "run it again" in message:
        return cmd + "verify-live --by ACTOR"
    return None


def advance_gates(errors: list[str], phase: int, status: dict,
                  cfg: dict | None = None) -> list[tuple[str, str, str | None]]:
    """#421: every unmet gate of an advance to `phase`, as (gate, message,
    clearing command), in the order compute_errors found them. Aggregation
    only: which gates exist and what they say is compute_errors' alone."""
    gates = []
    for message in errors:
        gate = message.split(":", 1)[0].strip() if ":" in message else "gate"
        gates.append((gate, message, _gate_clearing_command(message, phase, status, cfg)))
    return gates


def _gate_lines(gates: list[tuple[str, str, str | None]]) -> str:
    lines = []
    for _gate, message, command in gates:
        lines.append(f"- {message}")
        if command:
            lines.append(f"  clears with: {command}")
    return "\n".join(lines)


def _advance_preview_errors(root: Path, cfg: dict, status: dict, acceptance: dict,
                            verifications: list[dict], verification_problems: list[str],
                            phase: int, requested_status: str | None) -> list[str]:
    """#421: the gates an advance to `phase` would meet right now, for a
    refusal that names them. Mirrors cmd_advance's proposed status for the
    fields the gates read; never writes."""
    proposed = deepcopy(status)
    proposed["phase_number"] = phase
    proposed["phase"] = lib.PHASES[phase]
    proposed["progress"] = max(status.get("progress", 0) or 0,
                               lib.gate_progress(status, acceptance)["percent"])
    if requested_status:
        proposed["status"] = requested_status
    elif phase == 7:
        proposed["status"] = ("awaiting_approval" if lib.adaptive_deployment_approval_required(proposed, cfg)
                              else "ready_to_deploy")
    elif phase == 8:
        proposed["status"] = "complete"
    if phase == 3 and status.get("lane") == "design":
        proposed["status"] = "design_complete"
    if phase == 6 and proposed.get("lane") == "review":
        proposed["status"] = "review_complete"
    proposed["_preserve_progress"] = True
    return lib.compute_errors(proposed, acceptance, cfg, verifications=verifications,
                              verification_problems=verification_problems, root=root)


def _multi_step_refusal(root: Path, cfg: dict, status: dict, acceptance: dict,
                        verifications: list[dict], verification_problems: list[str],
                        current: int, requested: int, requested_status: str | None) -> str:
    """#421: a jump of more than one phase is still refused, and the refusal
    names what holds the next phase and what the requested one adds."""
    nxt = current + 1
    lines = [f"phase transition blocked: current={current}, requested={requested} "
             f"(one step at a time); advance {nxt} first"]
    try:
        held = _advance_preview_errors(root, cfg, status, acceptance, verifications,
                                       verification_problems, nxt, None)
        target = _advance_preview_errors(root, cfg, status, acceptance, verifications,
                                         verification_problems, requested, requested_status)
    except lib.HandsoffError as exc:
        lines.append(f"gates not evaluated: {exc}")
        return "\n".join(lines)
    if held:
        lines.append(f"Phase {nxt} is held by {len(held)} unmet gate(s):")
        lines.append(_gate_lines(advance_gates(held, nxt, status, cfg)))
    else:
        lines.append(f"Phase {nxt} has no unmet gate; clears with: {_SUPERVISOR_CMD} advance {nxt}")
    extra = [e for e in target if e not in held]
    if extra and requested != nxt:
        lines.append(f"Phase {requested} also needs:")
        lines.append(_gate_lines(advance_gates(extra, requested, status, cfg)))
    return "\n".join(lines)


def cmd_advance(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)

    if args.design_round is not None and args.new_design_round:
        print("SHIP_FEATURE_INVALID: pass either --design-round or --new-design-round, not both")
        return 1
    if args.design_round_reason and not args.new_design_round:
        print("SHIP_FEATURE_INVALID: --design-round-reason only applies together with --new-design-round")
        return 1
    if args.new_design_round and args.phase != 2:
        print("SHIP_FEATURE_INVALID: --new-design-round only applies when advancing to phase 2 (Design debate)")
        return 1

    # The lock covers the ENTIRE read-validate-write sequence, not just the
    # final write. Locking only the write let two concurrent callers both
    # read the same stale state, both validate successfully against it,
    # and then race at the write, silently losing one caller's transition.
    # Re-reading inside the lock means the second caller to arrive always
    # validates against what the first one actually left behind.
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)

        if args.phase == 8:
            release_error = release_runtime.completion_error(root, status.get("release_plan"))
            if release_error:
                print(f"SHIP_FEATURE_BLOCKED: release gate: {release_error}")
                return 1

        refusal = lib.lane_gate_refusal(status, f"advance_{args.phase}")
        if refusal:
            print(f"SHIP_FEATURE_BLOCKED: {refusal}")
            return 1

        if args.phase not in lib.PHASES:
            print(f"invalid phase {args.phase}, must be one of {sorted(lib.PHASES)}")
            return 1
        current = int(status.get("phase_number", 0) or 0)
        small_fix_jump = (
            current == 1 and args.phase == 4
            and not lib.full_design_required(status, acceptance, cfg)
        )
        if args.phase > current + 1 and not small_fix_jump:
            # #421: still one step at a time, but name what holds the next phase
            print(_multi_step_refusal(root, cfg, status, acceptance, verifications, verification_problems,
                                      current, args.phase, args.status))
            return 1
        if args.phase < current:
            print(f"phase transition blocked: current={current}, requested={args.phase} (one step at a time)")
            return 1
        if args.phase >= 2 and not acceptance.get("criteria"):
            print(f"SHIP_FEATURE_BLOCKED: {lib.EMPTY_REGISTRY_REFUSAL}")  # #418
            return 1
        current_progress = status.get("progress", 0)
        if args.progress is None:
            # #102: no explicit value means "what the gates say", never
            # lower than what the operator already recorded (#100).
            progress = max(current_progress, lib.gate_progress(status, acceptance)["percent"])
        else:
            progress = args.progress
            if args.progress < current_progress:
                progress = current_progress

        # Build the PROPOSED status and validate THAT, before writing
        # anything. This is the fix for the original bug: validating the
        # status already on disk can never catch the transition about to
        # happen, because the phase-6+ checks only fire once phase_number
        # already reads 6+, which is one write too late.
        proposed = deepcopy(status)
        proposed["phase_number"] = args.phase
        proposed["phase"] = lib.PHASES[args.phase]
        proposed["progress"] = progress
        if args.progress is not None and getattr(args, "progress_explicit", True):
            # #415: an explicit value is 'set' for this phase only; a later
            # phase inherits it as 'derived' (lib.progress_view)
            proposed["progress_set_phase"] = args.phase
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposed["next_action"] = args.next_action or lib.phase_next_action(  # #434
            args.phase, proposed, cfg, proposed.get("next_action"))
        if args.status:
            proposed["status"] = args.status
        elif args.phase == 7:
            proposed["status"] = (
                "awaiting_approval"
                if lib.adaptive_deployment_approval_required(proposed, cfg)
                else "ready_to_deploy"
            )
        elif args.phase == 8:
            proposed["status"] = "complete"
        implemented_by = args.implemented_by
        if not implemented_by and args.phase == 5 and not proposed.get("implemented_by"):
            # #122: the completed managed Implementer is the implementer.
            done = [item for item in (proposed.get("agent_sessions") or {}).values()
                    if isinstance(item, dict) and item.get("role") == "implementer"
                    and item.get("state") == "completed" and item.get("actor")]
            if done:
                implemented_by = max(done, key=lambda item: item.get("ended_at") or "")["actor"]
        if implemented_by:
            proposed["implemented_by"] = implemented_by
            # #176: the run-level implementer is the default per-item
            # implementer. Every required item's delivery record that has
            # none takes it (a record is created for an item that has none,
            # the tag-derived kind), so one run with one item and one run
            # with three behave the same at the Phase 8 gate; a host can
            # still name another actor per item with work-item-update
            # before Phase 8, and an item added after Phase 5 is refused
            # there as before.
            delivery = proposed.get("work_item_delivery")
            if not isinstance(delivery, dict):
                delivery = proposed["work_item_delivery"] = {}
            registry = lib.effective_work_items(acceptance, cfg)[0] if isinstance(acceptance, dict) else []
            for item_id, record in lib.new_work_item_delivery(registry, "full").items():
                delivery.setdefault(item_id, record)
            for item_id, record in delivery.items():
                if isinstance(record, dict) and not record.get("implemented_by"):
                    record["implemented_by"] = implemented_by
        if args.authorization_hold:
            if args.phase != 2 or proposed.get("status") != "blocked":
                print("SHIP_FEATURE_INVALID: --authorization-hold requires Phase 2 with --status blocked")
                return 1
            proposed["authorization_hold"] = args.authorization_hold
        else:
            # Holds are assertions about THIS exact transition, never sticky
            # state inherited by a later, unrelated Phase-2 block.
            proposed.pop("authorization_hold", None)
        new_design_round_event = None
        if args.design_round is not None:
            proposed["design_round"] = args.design_round
        elif args.new_design_round:
            # Organic tracking: bump from whatever design_round actually is
            # on disk right now, so repeated --new-design-round calls count
            # real rounds one at a time instead of the caller having to
            # compute and pass an absolute number (that's still --design-round,
            # kept for explicit overrides/fixture setup).
            previous_round = int(status.get("design_round", 0) or 0)
            new_round = previous_round + 1
            proposed["design_round"] = new_round
            new_design_round_event = {
                "design_round": new_round,
                "previous_design_round": previous_round,
                "reason": args.design_round_reason,
            }
        if args.review_round is not None:
            lib.migrate_review_ledger(proposed)
            current_review = int(proposed.get("review_round", 0) or 0)
            if args.review_round < current_review:
                print("SHIP_FEATURE_INVALID: review_round is monotonic")
                return 1
            if args.review_round > lib.effective_review_cap(proposed, cfg):
                print(f"SHIP_FEATURE_INVALID: round cap: review_round {args.review_round} exceeds "
                      f"effective max_review_rounds {lib.effective_review_cap(proposed, cfg)}")
                return 1
            now = datetime.now(timezone.utc).isoformat()
            while proposed["review_round"] < args.review_round:
                attempts = proposed["review_attempts"]
                aid = lib._new_bounded_id(
                    "ha", lib.REVIEW_ATTEMPT_ID_PATTERN,
                    {item.get("attempt_id") for item in attempts}, None,
                )
                number = proposed["review_round"] + 1
                attempts.append({
                    "attempt_id": aid, "attempt": number, "opened_at": now, "closed_at": now,
                    "opened_by": "advance", "reviewer": None, "session_ids": [],
                    "trigger": "manual_override", "trigger_detail": "legacy advance --review-round",
                    "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
                    "phase_number": args.phase, "disposition": "unrecorded", "findings": [],
                })
                proposed["review_round"] = number

        if "work_item_delivery" in proposed:
            # The command's monotonic progress value is the operator's
            # explicit transition contract. Item-level progress remains a
            # dashboard view and must not silently rewrite an omitted value.
            proposed["progress"] = progress
        proposed["_preserve_progress"] = True

        design_document = None
        if args.phase == 3 and status.get("lane") == "design":
            proposed["status"] = "design_complete"
            proposed["design_document"] = "design.html"
        if args.phase == 6 and proposed.get("lane") == "review":
            proposed["status"] = "review_complete"

        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
        if errors:
            # A blocked attempt to LEAVE Phase 2 specifically because design
            # approval is missing/stale IS the state machine telling us the
            # run just entered "awaiting design approval" -- log that fact
            # even though the phase transition itself is refused and
            # nothing else is written. Deduped against the log itself (no
            # second field to keep in sync): skip if the most recent
            # design-lifecycle event is already an unresolved request.
            if args.phase == 3 \
                    and any(e.startswith("design gate:") for e in errors) \
                    and not any(e.startswith("design review gate:") for e in errors):
                pending = _most_recent_kind(lib.read_events(root, cfg),
                                            {"design_round_advanced", "design_approval_requested", "design_approved"})
                if pending != "design_approval_requested":
                    lib.commit(root, cfg, event_kind="design_approval_requested",
                              event_message="Design approval requested (advance to Phase 3 blocked pending it)")
            print("SHIP_FEATURE_BLOCKED")
            print(_gate_lines(advance_gates(errors, args.phase, proposed, cfg)))  # #421: every gate, each with its command
            return 1
        if args.phase == 3 and status.get("lane") == "design":
            design_document = lib.render_design_document(root, cfg, proposed, acceptance)
        if args.dry_run:
            print("SHIP_FEATURE_ADVANCE_WOULD_SUCCEED")
            return 0

        review_report = None
        if args.phase == 6 and proposed.get("lane") == "review":
            proposed["review_report"] = "review_report.md"
            review_report = lib.render_review_report(proposed, acceptance)

        if new_design_round_event is not None:
            reason = new_design_round_event["reason"]
            message = (f"Design round {new_design_round_event['previous_design_round']} -> "
                       f"{new_design_round_event['design_round']}")
            if reason:
                message += f": {reason}"
            extra_events = None
            if new_design_round_event["previous_design_round"] >= 1:
                extra_events = [{
                    "kind": "design_round_ended",
                    "message": f"Design round {new_design_round_event['previous_design_round']} ended (next round started)",
                    "design_round": new_design_round_event["previous_design_round"],
                    "trigger": "next_round_started",
                }]
            lib.commit(root, cfg, status=proposed, extra_events=extra_events,
                      event_kind="design_round_advanced", event_message=message,
                      phase_number=args.phase, progress=progress, **new_design_round_event)
        else:
            lib.commit(root, cfg, status=proposed,
                      event_kind="phase_advanced", event_message=f"Advanced to {lib.PHASES[args.phase]}",
                      phase_number=args.phase, progress=progress)
        if review_report is not None:
            lib._atomic_write_text(root / proposed["review_report"], review_report)
        if design_document is not None:
            lib._atomic_write_text(root / "design.html", design_document)

        archived = False
        if args.phase == 8 and proposed.get("status") == "complete":
            # The phase transition above already committed successfully; an
            # archive failure (e.g. an unwritable Documents folder) must not
            # be reported as if the run itself failed.
            try:
                fresh_verifications, _ = lib.load_verifications(root, cfg)
                archive_path = lib.archive_run(root, cfg, proposed, acceptance,
                                               fresh_verifications, lib.read_events(root, cfg))
                print(f"HANDSOFF_ARCHIVED: {archive_path}")
                archived = True
            except OSError as exc:
                print(f"HANDSOFF_ARCHIVE_FAILED (run still completed successfully): {exc}")
    if args.phase == 8 and proposed.get("status") == "complete":
        # #49: outside the lock like the dashboard release below (the scan
        # may talk to GitHub); the completion event takes the lock itself.
        if archived:
            _analyze_after_archive(root, cfg)
        _evidence_rollback_after_complete(root)  # #303
        # #40: outside the project lock on purpose. The dashboard's own
        # event stream takes that lock every 0.2 s, so holding it here
        # would keep the server from ever noticing the stop request.
        _release_run_dashboard(root, cfg)
        _release_tickets(root, "complete")  # #166
        if lib.feature_enabled(cfg, "report_posting"):
            _post_report(root, cfg, by=args.implemented_by or "supervisor")  # #171
    if args.progress is not None and args.progress < progress:
        print(f"NOTE: progress kept at {current_progress} (requested {args.progress} is lower)")
    print("SHIP_FEATURE_ADVANCED")
    return 0


def advance_approved_design(root: Path) -> bool:
    """Advance the completed design gate without spending an agent turn.

    The check is intentionally narrow: only a Phase-2 run whose current
    design review and human approval both pass their hash-bound gates is
    eligible. ``cmd_advance`` remains the single transition implementation
    and revalidates the state under its own lock, so a concurrent mutation
    cannot bypass normal workflow checks.
    """
    root = root.resolve()
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
        if int(status.get("phase_number", 0) or 0) != 2:
            return False
        if lib._design_errors(status, acceptance, cfg) \
                or lib._design_review_errors(status, acceptance, cfg):
            return False
        progress = max(30, int(status.get("progress", 0) or 0))
    args = argparse.Namespace(
        root=str(root), phase=3, progress=progress, status="in_progress",
        implemented_by=None, design_round=None, new_design_round=False,
        design_round_reason=None, review_round=None, dry_run=False,
        next_action=None, authorization_hold=None,
        progress_explicit=False,  # #415: the engine's own step, not a recorded value
    )
    result = cmd_advance(args)
    if result != 0:
        raise lib.HandsoffError("approved design could not advance to Phase 3")
    return True


# #174: the archive scan is the Miner's (monzta1/miner), installed beside
# the engine. Handsoff keeps the trigger, the ledger record and the command
# as a shim for one release; the rules, the drafts and the filing live there.
MINER_RELEASE = "v0.2.0"
MINER_INSTALL_HINT = (f"install the Miner {MINER_RELEASE}: gh release download {MINER_RELEASE} --repo monzta1/miner "
                      f"--pattern 'miner-*.whl' --dir /tmp && python3 -m pip install /tmp/miner-*.whl "
                      "(into the engine's own environment), or set HANDSOFF_MINER to its executable")


def _miner_argv() -> list[str] | None:
    """The installed Miner: HANDSOFF_MINER (an executable), else `miner` on
    PATH, else `miner` in this interpreter's environment (sys.prefix/bin,
    where pip puts it in the engine's own venv); None when there is none."""
    override = os.environ.get("HANDSOFF_MINER")
    if override:
        return [override]
    found = shutil.which("miner")
    if found:
        return [found]
    # The engine's own environment: sys.prefix is the venv root (on macOS a
    # framework build reports the framework binary as sys.executable, so
    # the executable's directory is not where pip put the scripts).
    for candidate in (Path(sys.prefix) / "bin" / "miner", Path(sys.executable).resolve().parent / "miner"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return None


def _miner_run(subcommand: str, root, *, archive_dir=None, dry_run: bool = False) -> dict:
    """Run one Miner command with --json and return its report. Refuses with
    the install hint when no Miner is installed; a non-zero exit or a reply
    that is not JSON is a HandsoffError naming the Miner's own words."""
    argv = _miner_argv()
    if argv is None:
        raise lib.HandsoffError(f"no Miner is installed; {MINER_INSTALL_HINT}")
    command = argv + [subcommand, "--root", str(root), "--json"]
    if archive_dir:
        command += ["--archive-dir", str(archive_dir)]
    if dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except OSError as exc:
        raise lib.HandsoffError(f"the Miner could not be started ({command[0]}): {exc.strerror or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise lib.HandsoffError("the Miner did not finish within 600 seconds") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise lib.HandsoffError(f"the Miner exited {completed.returncode}: {detail[-1][:200] if detail else 'no output'}")
    try:
        report = json.loads(completed.stdout)
    except ValueError as exc:
        raise lib.HandsoffError("the Miner's reply was not JSON") from exc
    if not isinstance(report, dict):
        raise lib.HandsoffError("the Miner's reply was not a report")
    if subcommand == "scan" and not dry_run:
        # Engine-owned lane guard: the installed Miner remains the source of
        # existing rules, while this small report-only check supplies the
        # review-lane gate that older Miners do not know about.
        try:
            import handsoff_analyzer
            local_findings = handsoff_analyzer.scan(Path(archive_dir) if archive_dir else lib.archive_dir())
            report.setdefault("findings", []).extend(local_findings)
        except (OSError, ValueError, TypeError):
            pass
    if subcommand == "scan":
        _lane_filter_findings(report, Path(archive_dir) if archive_dir else lib.archive_dir())
    return report


def _archive_record(archive_dir: Path, run_id: object) -> dict | None:
    """The archive JSON a finding's run id names, or None when it cannot be read."""
    if not isinstance(run_id, str):
        return None
    try:
        record = json.loads((archive_dir / run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _lane_filter_findings(report: dict, archive_dir: Path) -> None:
    """#380: keep a finding's run id only when that archive's lane still applies
    the finding's rule, and drop a finding left with no run id. Findings that
    never named run ids pass through. The Miner's own filing is not touched;
    only the findings this engine prints and counts are trimmed."""
    import handsoff_analyzer
    if not isinstance(report.get("findings"), list):
        return
    kept = []
    for finding in report["findings"]:
        run_ids = finding.get("run_ids") if isinstance(finding, dict) else None
        if not isinstance(run_ids, list) or not run_ids:
            kept.append(finding)
            continue
        survivors = []
        for run_id in run_ids:
            record = _archive_record(archive_dir, run_id)
            if record is not None and any(
                    item.get("rule") == finding.get("rule")
                    for item in handsoff_analyzer.applicable_findings(record, [finding])):
                survivors.append(run_id)
        if survivors:
            kept.append({**finding, "run_ids": survivors})
    report["findings"] = kept


def _scan_event_fields(report: dict) -> dict:
    """The counters `archive_scan_completed` records on the live ledger,
    read from the Miner's report (the shape the engine's own scan wrote)."""
    runs = report.get("runs") or {}
    return {
        "findings": len(report.get("findings") or []),
        "filed": len(report.get("filed") or []),
        "suppressed": len(report.get("suppressed") or []),
        "excluded": len(report.get("excluded") or []),
        "skipped_fixtures": len(runs.get("skipped_fixtures") or []),
        "unreadable": len(runs.get("unreadable") or []),
        "report_path": str(report.get("report_path") or ""),
    }


def _analyze_after_archive(root, cfg) -> None:
    """#49: scan the archive (the record just written included) right after
    the Phase 8 archive write, when [analysis] enabled is true, and record
    archive_scan_completed on this run's live ledger. #174: the scan is the
    installed Miner's; without one the step is skipped with the install
    hint. Any failure at all is reported and never fails the advance: the
    transition and the archive are already committed."""
    if not (cfg.get("analysis") or {}).get("enabled", True):
        return
    if _miner_argv() is None:
        print(f"HANDSOFF_ANALYSIS_SKIPPED (run still completed successfully): {MINER_INSTALL_HINT}")
        return
    try:
        report = _miner_run("scan", root)
        fields = _scan_event_fields(report)
        with lib.project_lock(root):
            lib.commit(root, cfg, event_kind="archive_scan_completed",
                      event_message=f"Archive scan completed: {fields['findings']} finding(s), "
                                    f"{fields['filed']} filed",
                      **fields)
        print(f"HANDSOFF_ANALYSIS_REPORT: {fields['report_path']}")
    except Exception as exc:  # noqa: BLE001 - a scan must never fail a completed run
        print(f"HANDSOFF_ANALYSIS_FAILED (run still completed successfully): {type(exc).__name__}: {exc}")


def _evidence_rollback_after_complete(root) -> None:
    """#303: the rollback monitor, after the Phase 8 archive write and outside
    the lock (it takes the lock itself). Any failure is reported and never
    fails the advance: the transition is already committed."""
    import handsoff_evidence_routing as evidence_routing
    try:
        result = evidence_routing.rollback_monitor(root)
    except Exception as exc:  # noqa: BLE001 - the monitor must never fail a completed run
        print(f"HANDSOFF_EVIDENCE_ROLLBACK_FAILED (run still completed successfully): {type(exc).__name__}: {exc}")
        return
    if result["state"] == "rolled_back":
        print(f"EVIDENCE_ROUTING_ROLLED_BACK: {result['metric']} (assisted "
              f"{result['rates'][result['metric']]['assisted']:.3f}, baseline "
              f"{result['rates'][result['metric']]['baseline']:.3f}); off until a fresh approved activation")
    elif result["state"] == "no_decision":
        print(f"EVIDENCE_ROUTING_NO_DECISION: {result['reason']} {result['counts']}")


def cmd_analyze_archives(args) -> int:
    """#49: the same scan the Phase 8 trigger runs, on demand. Prints the
    report path. --dry-run files nothing; --archive-dir overrides the
    archive location for this scan only. #174: a shim over the installed
    Miner (`miner scan`, `miner propose-rules`) for one release; without a
    Miner it refuses with the install hint."""
    root = lib.resolve_root(args.root)
    lib.load_config(root)
    try:
        if getattr(args, "propose_rules", False):
            # #167: drafts only, never evaluated until a human moves one up.
            result = _miner_run("propose-rules", root, archive_dir=args.archive_dir)
            written = result.get("written") or result.get("proposed") or []
            print(f"HANDSOFF_RULES_PROPOSED: {len(written)} draft(s) in {result.get('proposed_dir') or result.get('rules_dir')}")
            print(json.dumps(result, indent=2, default=str))
            return 0
        report = _miner_run("scan", root, archive_dir=args.archive_dir, dry_run=args.dry_run)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(f"HANDSOFF_ANALYSIS_REPORT: {report.get('report_path')}")
    runs = report.get("runs") or {}
    print(json.dumps({
        "report_path": report.get("report_path"),
        "filing": report.get("filing"),
        "findings": [{"rule": f.get("rule"), "title": f.get("title"), "run_ids": f.get("run_ids"),
                      "numbers": f.get("numbers"), "excluded": f.get("excluded")} for f in report.get("findings") or []],
        "filed": report.get("filed"),
        "suppressed": report.get("suppressed"),
        "not_filed": report.get("not_filed"),
        "runs": {key: len(value) for key, value in runs.items() if isinstance(value, list)},
    }, indent=2))
    return 0


def cmd_pilot_note(args) -> int:
    """#49: record a pilot_note event on the current run. The next archive
    scan lists each distinct note as an R7 finding with its run id."""
    root = lib.resolve_root(args.root)
    record = lib.record_pilot_note(root, by=args.by, text=args.text)
    print(f"PILOT_NOTE_RECORDED: {len(record['text'])} characters by {record['by']}")
    return 0


def cmd_shadow_apply(args) -> int:
    """#302: apply one shadow routing recommendation. There is no actor
    flag: the only authority is a Pilot approval Mission Control's endpoint
    recorded for exactly this finding and change, used once."""
    import handsoff_shadow as shadow
    root = lib.resolve_root(args.root)
    report = lib.load_unique_json(Path(args.report).expanduser().resolve())
    result = shadow.apply_finding(root, report, finding=args.finding, approval=args.approval)
    print(f"SHADOW_APPLIED: {result['finding_id']} with {result['approval_id']}: "
          f"{result['change']['role']} -> {result['change']['to']['adapter']}/{result['change']['to']['model']}")
    return 0


def cmd_shadow_route(args) -> int:
    """#379: read-only shadow mode. Prints the live routed choice beside what
    the frozen policy in the report would pick for one cohort; handsoff.toml
    is hashed before and after and never written."""
    import handsoff_shadow as shadow
    root = lib.resolve_root(args.root)
    try:
        report = lib.load_unique_json(Path(args.report).expanduser().resolve())
    except (OSError, ValueError, lib.HandsoffError) as exc:
        print(f"SHADOW_REFUSED: cannot read the report {args.report}: {exc}")
        return 1
    try:
        view = shadow.shadow_route(root, report, role=args.role, repository=args.repository,
                                   task_class=args.task_class, variant=args.variant)
    except lib.HandsoffError as exc:
        print(f"SHADOW_REFUSED: {exc}")
        return 1
    print(json.dumps(view, sort_keys=True))
    return 0


def cmd_evidence_routing_activate(args) -> int:
    """#303: activate evidence-assisted routing. Refused unless a #302 finding
    and the Mission Control approval recorded for it are named; the approval
    is consumed and the activation bound to the scoring policy version."""
    import handsoff_evidence_routing as evidence_routing
    root = lib.resolve_root(args.root)
    record = evidence_routing.activate(root, finding=args.finding, approval=args.approval, by=args.by)
    print(f"EVIDENCE_ROUTING_ACTIVATED: {record['activation_id']} policy {record['policy_version']} "
          f"from {record['finding_id']} with {record['approval_id']}")
    return 0


def cmd_design_decline(args) -> int:
    """#177: the Architect declines the change. Recorded like a proposal,
    hash-bound to the criteria, and the run closes as not_planned. The
    host posts the reason on the issue in maintainer voice; the printed
    lines are that text."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        record = lib.record_design_decline(root, cfg, by=args.by, reason=args.reason,
                                           evidence=args.evidence, alternative=args.alternative)
    except lib.HandsoffError as exc:
        print(f"DESIGN_DECLINE_BLOCKED: {exc}")
        return 1
    print(f"DESIGN_DECLINED: pending the independent reviewer's word (by {record['by']})")
    print(f"Not planned, proposed: {record['reason']}")
    for item in record["evidence"]:
        print(f"- {item}")
    if record["alternative"]:
        print(f"Instead: {record['alternative']}")
    return 0


GUARDS_RECORD = Path(".handsoff") / "guards-record.json"
GUARDS_COMMAND = "python3 -m tests.guards"
GUARDS_MODULE = Path("tests") / "guards.py"
_GUARDS_RECORD_KEYS = {"schema", "repository_digest", "executed", "guard_ids_sha256", "duration_ms", "passed_at"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _guards_record_digest(root: Path) -> str | None:
    """#371: the digest a passing guard run bound itself to, or None when the
    record is absent or not exactly the shape `python3 -m tests.guards` writes."""
    path = root / GUARDS_RECORD
    try:
        if path.stat().st_size > 4096:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != _GUARDS_RECORD_KEYS:
        return None
    ints = {key: record[key] for key in ("schema", "executed", "duration_ms")}
    if any(type(value) is not int for value in ints.values()):
        return None
    if ints["schema"] != 1 or not 1 <= ints["executed"] <= 10000 or not 0 <= ints["duration_ms"] <= 600000:
        return None
    strings = [record[key] for key in ("repository_digest", "guard_ids_sha256", "passed_at")]
    if not all(isinstance(value, str) for value in strings):
        return None
    if not (_HEX64.fullmatch(strings[0]) and _HEX64.fullmatch(strings[1]) and _UTC_SECOND.fullmatch(strings[2])):
        return None
    return strings[0]


def _require_guards_record(root: Path, cfg: dict) -> None:
    """#371: a watch starts only on a tree the guard run passed on, so a
    stale count turns red locally instead of on a CI round."""
    recorded = _guards_record_digest(root)
    if recorded is None:
        raise lib.HandsoffError(f"no passing guard run is recorded for this tree; run `{GUARDS_COMMAND}` "
                                "after the last edit, then push and start the watch")
    if recorded != lib.repository_digest(root, cfg):
        raise lib.HandsoffError(f"the tree changed since the last passing guard run; run `{GUARDS_COMMAND}` "
                                "after the last edit, then push and start the watch")


def cmd_ci_watch(args) -> int:
    """#181: record that the run is waiting on a pull request's checks
    (--pr), or refresh and print the CI view (--poll). The host runs
    `ci-watch --pr N` right after `gh pr create`; Mission Control then
    shows the CI row until every check has completed. #371: starting a
    watch needs a guard record bound to the current tree. #392: only where
    the project has the module GUARDS_COMMAND runs; elsewhere nothing can
    write the record, so the watch starts and says guards are not configured."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.pr is None and not args.poll:
        print("CI_WATCH_BLOCKED: give --pr N to start a watch or --poll to refresh the current one")
        return 1
    try:
        if args.pr is not None:
            guards_configured = (root / GUARDS_MODULE).is_file()
            if guards_configured:
                _require_guards_record(root, cfg)
            _enforce_refs_only_pr(root, args.pr)
            watch = lib.ci_watch_start(root, cfg, pr=args.pr, by=args.by)
            expected = f"{watch['expected_seconds']:.0f} s expected" if watch.get("expected_seconds") else lib.CI_NO_HISTORY_NOTE
            note = "" if guards_configured else f"; guards not configured (no {GUARDS_MODULE.as_posix()})"
            print(f"CI_WATCH_STARTED: PR #{watch['pr']} head {watch['head'][:12]}; {expected}{note}")
        if args.poll:
            with lib.project_lock(root):
                status = lib.load_unique_json(lib.status_path(root, cfg))
            view = lib.ci_view(status, root, cfg, force=True)
            if view is None:
                print("CI_WATCH_NONE: no watch is recorded on this run")
                return 1
            # the state printed is the one the ledger holds after the poll
            with lib.project_lock(root):
                after = lib.load_unique_json(lib.status_path(root, cfg)).get("ci") or {}
            view = {**view, "state": after.get("state", view["state"]), "failed_check": after.get("failed_check"),
                    "ended_at": after.get("ended_at")}
            print(json.dumps(view, indent=1, sort_keys=True))
    except lib.HandsoffError as exc:
        print(f"CI_WATCH_BLOCKED: {exc}")
        return 1
    return 0


def _enforce_refs_only_pr(root: Path, number: int, *, runner=subprocess.run) -> None:
    """Refuse a watched PR whose title/body can auto-close a work item."""
    try:
        result = runner(
            ["gh", "pr", "view", str(number), "--json", "title,body"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    try:
        close_transaction.enforce_refs_only(payload.get("title") or "", payload.get("body") or "")
    except close_transaction.CloseTransactionError as exc:
        raise lib.HandsoffError(f"ci-watch: {exc}") from exc


def _release_run_dashboard(root, cfg) -> None:
    """Release the dashboard this run owns (#40), if any, and log what
    happened. Like the archive step, a failure here is reported and never
    turns an already-committed completion into a failed advance."""
    try:
        release = lib.release_run_dashboard(root)
        with lib.project_lock(root):
            if release.get("released"):
                lib.commit(root, cfg, event_kind="dashboard_released",
                          event_message=f"Run-owned dashboard on port {release.get('port')} released",
                          port=release.get("port"), pid=release.get("pid"), reason=release.get("reason"))
                print(f"HANDSOFF_DASHBOARD_RELEASED: port {release.get('port')}")
            else:
                lib.commit(root, cfg, event_kind="dashboard_release_skipped",
                          event_message=f"Run-owned dashboard release skipped: {release.get('reason')}",
                          reason=release.get("reason"))
                print(f"HANDSOFF_DASHBOARD_RELEASE_SKIPPED: {release.get('reason')}")
    except (lib.HandsoffError, OSError) as exc:
        print(f"HANDSOFF_DASHBOARD_RELEASE_FAILED (run still completed successfully): {exc}")


def cmd_deployment_gate(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        refusal = lib.lane_gate_refusal(status, "deployment-gate")
        if refusal:
            print(f"DEPLOYMENT_BLOCKED\n- {refusal}")
            return 1
        if getattr(args, "revoke", False):
            phase = int(status.get("phase_number", 0) or 0)
            if not isinstance(status.get("deployment_approved"), dict):
                print("DEPLOYMENT_REVOKE_BLOCKED\n- deployment approval has not been recorded")
                return 1
            if phase not in {7, 8} or status.get("live_verification_id"):
                print("DEPLOYMENT_REVOKE_BLOCKED\n- revoke requires Phase 7 or 8 without a live verification id")
                return 1
            proposed = dict(status)
            proposed["deployment_approved"] = None
            proposed["phase_number"] = 7
            proposed["phase"] = lib.PHASES[7]
            proposed["status"] = "awaiting_approval"
            proposed["next_action"] = "Await explicit deployment approval."
            proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.commit(root, cfg, status=proposed, event_kind="deployment_revoked",
                       event_message=args.reason, by=args.by, reason=args.reason)
            print("DEPLOYMENT_REVOKED")
            return 0
        refusal = lib.amendment_decision_refusal(status, "deployment approval")
        if refusal:
            print(f"DEPLOYMENT_BLOCKED\n- {refusal}")
            return 1
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=verification_problems, root=root)
        if errors:
            print("DEPLOYMENT_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        # Approval is only meaningful in Phase 7. Refusing earlier phases
        # prevents premature approval; refusing later phases prevents a
        # delayed/double-click request from mutating a completed run.
        phase = int(status.get("phase_number", 0) or 0)
        if phase != 7:
            print(f"DEPLOYMENT_BLOCKED\n- deployment approval requires Phase 7 (currently Phase {phase})")
            return 1
        if not args.approve:
            print("DEPLOYMENT_AWAITING_EXPLICIT_APPROVAL")
            return 2
        if not args.by:
            print("DEPLOYMENT_BLOCKED\n- --by is required when recording approval")
            return 1
        acceptance_digest = lib.acceptance_hash(acceptance.get("criteria", []))
        config_digest = lib.config_hash(cfg)
        existing = status.get("deployment_approved")
        if isinstance(existing, dict):
            if existing.get("acceptance_hash") == acceptance_digest \
                    and existing.get("config_hash") == config_digest:
                print("DEPLOYMENT_ALREADY_APPROVED")
                return 0
            print("DEPLOYMENT_BLOCKED\n- existing deployment approval is stale; workflow decisions must be refreshed")
            return 1
        # The approval is bound to a hash of the acceptance criteria AT
        # THE MOMENT of approval. If the registry changes afterward (a
        # criterion reopened, added, or removed), the Phase 8 gate
        # recomputes this hash and refuses the now-stale approval.
        auto_from = None
        design_digest = lib.design_hash(acceptance.get("criteria", []))
        if getattr(args, "auto", False):
            # #102: --auto re-applies only what a person already approved
            # for this exact design hash (what was decided), while the
            # current review is fresh against the current evidence. A
            # drift-and-re-review cycle revokes the approval without
            # changing what was approved; a changed criterion does.
            prior = next((event for event in reversed(lib.read_events(root, cfg))
                          if event.get("kind") == "deployment_approved"
                          and event.get("design_hash") == design_digest), None)
            review = status.get("review") if isinstance(status.get("review"), dict) else None
            if prior is None or review is None or review.get("acceptance_hash") != acceptance_digest:
                print("DEPLOYMENT_BLOCKED\n- no prior approval for this design hash")
                return 1
            auto_from = {"by": prior.get("by"), "at": prior.get("at")}
        proposed = dict(status)
        proposed["deployment_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "acceptance_hash": acceptance_digest,
            "config_hash": config_digest,
            **lib.rules_binding(root, cfg),  # #170
            **({"auto_confirmed_from": auto_from} if auto_from else {}),
        }
        proposed["status"] = "ready_to_deploy"
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposed["next_action"] = "Deploy the reviewed change and run live verification."
        proposed_errors = lib.compute_errors(
            proposed, acceptance, cfg, verifications=verifications,
            verification_problems=verification_problems, root=root,
        )
        if proposed_errors:
            print("DEPLOYMENT_BLOCKED")
            print("\n".join(f"- {error}" for error in proposed_errors))
            return 1
        if auto_from:
            lib.commit(root, cfg, status=proposed,
                      event_kind="deployment_approval_auto_confirmed",
                      event_message="Deployment approval re-applied for an unchanged acceptance hash",
                      by=args.by, acceptance_hash=acceptance_digest, design_hash=design_digest,
                      prior_by=auto_from["by"], prior_at=auto_from["at"])
            print("DEPLOYMENT_APPROVAL_AUTO_CONFIRMED")
            return 0
        lib.commit(root, cfg, status=proposed,
                  event_kind="deployment_approved", event_message="Explicit deployment approval recorded",
                  by=args.by, acceptance_hash=acceptance_digest, design_hash=design_digest)
    print("DEPLOYMENT_APPROVED")
    return 0


def cmd_design_reject(args) -> int:
    """Pilot rejects the currently reviewed design and returns it for revision."""
    actor = lib.validate_agent_actor(args.by)
    reason = str(args.reason or "").strip()
    if not reason:
        print("SHIP_FEATURE_BLOCKED: design rejection requires --reason")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        review = status.get("design_review")
        if status.get("phase_number") != 2 or not isinstance(review, dict) \
                or review.get("decision") != "approved" or status.get("design_approved"):
            print("SHIP_FEATURE_BLOCKED: no reviewed, unapproved design is awaiting the Pilot")
            return 1
        rejected_hash = review.get("design_hash")
        status["design_review"] = None
        status["design_approved"] = None
        status["status"] = "in_progress"
        status["next_action"] = f"Pilot requested design revision: {reason}"
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="design_rejected",
                   event_message=f"Pilot rejected the reviewed design: {reason}",
                   by=actor, reason=reason, design_hash=rejected_hash)
    print("DESIGN_REJECTED")
    return 0


def _regression_bindings(root: Path, cfg: dict, acceptance: dict, status: dict) -> dict:
    regression_policy = {
        "regressions": cfg.get("regressions", []),
        "regression_gate": cfg.get("regression_gate", {}),
        "check_timeout_seconds": cfg.get("check_timeout_seconds"),
    }
    if cfg.get("check_env"):
        # #410: a regression run under other [checks].env values is another run
        regression_policy["check_env"] = lib.recorded_check_env(cfg)
    events = lib.read_events(root, cfg)
    epoch = {
        "sessions": sorted((status.get("agent_sessions") or {}).keys()),
        "replacements": [item.get("replacement_id") for item in status.get("agent_replacements", [])],
        "recoveries": [item.get("recovery_id") for item in status.get("recovery_attempts", [])],
    }
    return {
        "repository": lib.repository_snapshot(root),
        "acceptance_hash": lib.acceptance_hash(acceptance.get("criteria", [])),
        "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
        "run_id": events[0].get("hash") if events else "GENESIS",
        "epoch_sha256": hashlib.sha256(__import__("json").dumps(
            epoch, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "config_hash": hashlib.sha256(__import__("json").dumps(
            regression_policy, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
    }


def _regression_request(status: dict, request_id: str | None = None) -> dict | None:
    requests = status.get("regression_requests") or []
    if request_id:
        return next((item for item in requests if item.get("request_id") == request_id), None)
    return lib.active_regression_request(status)


def _same_regression_bindings(item: dict, root: Path, cfg: dict, acceptance: dict, status: dict) -> bool:
    current = _regression_bindings(root, cfg, acceptance, status)
    return all(item.get(key) == current[key] for key in current)


def _legacy_scope_bootstrap_allowed(root: Path, cfg: dict, status: dict,
                                    acceptance: dict, before_items: list[dict]) -> bool:
    """Trust an old scope-less approval only when its full audit proof is current."""
    if lib.verify_event_log(root, cfg):
        return False
    review = status.get("design_review")
    approval = status.get("design_approved")
    if not isinstance(review, dict) or not isinstance(approval, dict):
        return False
    if review.get("decision") != "approved":
        return False
    current_design = lib.design_hash(acceptance.get("criteria", []))
    current_config = lib.config_hash(cfg)
    if any(record.get("design_hash") != current_design or record.get("config_hash") != current_config
           for record in (review, approval)):
        return False
    approval_event = next((event for event in reversed(lib.read_events(root, cfg))
                           if event.get("kind") == "design_approved"), None)
    if not approval_event or approval_event.get("acceptance_sha256") != lib._file_sha256(
            lib.acceptance_path(root, cfg)):
        return False
    tagged = {lib.criterion_work_item_id(item) for item in acceptance.get("criteria", [])}
    tagged.discard(None)
    return all(not item.get("required", True) or item.get("id") in tagged for item in before_items)


def cmd_work_items_sync(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        before_items, _ = lib.effective_work_items(acceptance, cfg)
        before_scope = lib.work_item_scope_hash(before_items, acceptance.get("criteria", []))
        # #141: an explicit --item is the deliberate way back for a removed
        # item; its tombstone goes in this same commit.
        own_repo = None
        if args.item:
            import handsoff_fleet_signals
            own_repo = handsoff_fleet_signals.origin_repo(root)
            foreign = lib.foreign_issue_refs(args.item, own_repo)
            if foreign:  # #400: as at init
                print(f"SHIP_FEATURE_BLOCKED: {foreign[0]} is an issue of {foreign[0].split('#')[0]}, not of this "
                      f"run's repository {own_repo}; a run's issue items belong to its own repository")
                return 1
            lib.clear_work_item_tombstones(
                acceptance, {row["id"] for row in lib.derive_work_item_registry(
                    {"feature": "", "criteria": []}, cfg, explicit_items=args.item, repo=own_repo)})
        derived = lib.derive_work_item_registry(acceptance, cfg, explicit_items=args.item, repo=own_repo)
        existing = acceptance.get("work_items")
        if isinstance(existing, list):
            by_id = {item["id"]: item for item in existing}
            criterion_ids = {lib.criterion_work_item_id(criterion)
                             for criterion in acceptance.get("criteria", [])}
            criterion_ids.discard(None)
            for item in derived:
                current = by_id.get(item["id"])
                if current is None:
                    if not args.item and (acceptance.get("work_items_explicit") is True
                                          or any(candidate.get("kind") == "issue" for candidate in existing)) \
                            and item.get("kind") == "ask" and item["id"] not in criterion_ids:
                        print(f"WORK_ITEM_SYNC_SKIPPED: {item['id']} (feature-title ask not added beside explicit issue items; pass --item to add it deliberately)")
                        continue
                    existing.append(item)
                elif args.from_tickets:
                    current["title"], current["url"] = item["title"], item["url"]
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
            persisted = existing
        else:
            persisted = derived
        acceptance["work_items"] = persisted
        after_scope = lib.work_item_scope_hash(persisted, acceptance.get("criteria", []))
        if isinstance(status.get("deployment_approved"), dict) and before_scope != after_scope:
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
        bootstrap = False
        decisions = [status.get("design_review"), status.get("design_approved")]
        scope_missing = any(isinstance(record, dict) and not record.get("scope_hash")
                            for record in decisions)
        scope_conflict = any(isinstance(record, dict) and record.get("scope_hash") not in {None, after_scope}
                             for record in decisions)
        if before_scope == after_scope and scope_missing:
            bootstrap = not scope_conflict and all(isinstance(record, dict) and not record.get("scope_hash")
                                                   for record in decisions) \
                and _legacy_scope_bootstrap_allowed(root, cfg, status, acceptance, before_items)
            if bootstrap:
                for field in ("design_review", "design_approved"):
                    status[field]["scope_hash"] = after_scope
            else:
                status["design_review"] = None
                status["design_approved"] = None
        elif before_scope != after_scope or scope_conflict:
            status["design_review"] = None
            status["design_approved"] = None
        if (before_scope != after_scope or scope_conflict or (scope_missing and not bootstrap)) \
                and status.get("phase_number", 1) >= 3:
            status.update(phase_number=2, phase=lib.PHASES[2], progress=min(status.get("progress", 0), 20),
                          status="in_progress", next_action="Review and approve the changed work-item scope.")
        status.setdefault("active_work_item", None)
        if not isinstance(status.get("work_item_delivery"), dict):
            status["work_item_delivery"] = lib.new_work_item_delivery(persisted, "full")
            for record in status["work_item_delivery"].values():
                record["implemented_by"] = status.get("implemented_by")
        else:
            # #116: an item added mid-run gets the same delivery record
            # init --item creates, so work-item-update accepts it.
            for item_id, record in lib.new_work_item_delivery(persisted, "full").items():
                status["work_item_delivery"].setdefault(item_id, record)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance) + lib.validate_status_schema(status)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_items_synced", event_message="Canonical work-item registry synchronized",
                   by=actor, count=len(persisted), scope_hash=after_scope,
                   scope_binding_bootstrapped=bootstrap)
    print(f"WORK_ITEMS_SYNCED: {len(persisted)}")
    return 0


def cmd_work_item_activate(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items, _ = lib.effective_work_items(acceptance, cfg)
        if args.item not in {item["id"] for item in items}:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        if status.get("tranche_approval"):
            rows = lib.derive_work_items(status, acceptance, cfg)["items"]
            next_item = next((item["id"] for item in rows
                              if item.get("required", True) and item.get("status") != "done"), None)
            if next_item and args.item != next_item:
                print(f"SHIP_FEATURE_BLOCKED: approved tranche order requires {next_item} next")
                return 1
        status["active_work_item"] = args.item
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="work_item_activated",
                   event_message=f"Activated work item {args.item}", by=actor, work_item=args.item)
    print(f"WORK_ITEM_ACTIVATED: {args.item}")
    return 0


def cmd_work_item_update(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items = acceptance.get("work_items")
        if not isinstance(items, list):
            print("SHIP_FEATURE_BLOCKED: synchronize work items before updating them")
            return 1
        item = next((candidate for candidate in items if candidate.get("id") == args.item), None)
        if item is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        before_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        deployment_approved = status.get("deployment_approved")
        for field in ("title", "url", "github_state", "notes"):
            value = getattr(args, field)
            if value is not None:
                item[field] = value
        if args.required:
            item["required"] = True
        elif args.optional:
            item["required"] = False
        if args.github_state is not None:
            item["github_checked_at"] = datetime.now(timezone.utc).isoformat()
        if getattr(args, "implemented_by", None) is not None:
            delivery = status.get("work_item_delivery") or {}
            if args.item not in delivery:
                print(f"SHIP_FEATURE_BLOCKED: work item {args.item} has no delivery record")
                return 1
            delivery[args.item]["implemented_by"] = lib.validate_agent_actor(args.implemented_by)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        after_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        if isinstance(deployment_approved, dict) and before_scope != after_scope:
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
        if isinstance(deployment_approved, dict) and before_scope != after_scope:
            _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = item["updated_at"]
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_item_updated", event_message=f"Updated work item {args.item}",
                   by=actor, work_item=args.item, scope_changed=before_scope != after_scope)
    print(f"WORK_ITEM_UPDATED: {args.item}")
    return 0


def cmd_work_item_remove(args) -> int:
    """Remove a zero-criteria item while preserving the approval audit trail."""
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        items = acceptance.get("work_items")
        if not isinstance(items, list):
            print("SHIP_FEATURE_BLOCKED: synchronize work items before removing them")
            return 1
        item = next((candidate for candidate in items if candidate.get("id") == args.item), None)
        if item is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        row = next(row for row in lib.derive_work_items(status, acceptance, cfg)["items"]
                   if row["id"] == args.item)
        if row.get("criteria"):
            criteria = ", ".join(row["criteria"])
            print(f"SHIP_FEATURE_BLOCKED: work item {args.item} maps to criteria {criteria}; retag or remove them first")
            return 1
        post_approval = isinstance(status.get("deployment_approved"), dict)
        if post_approval and row.get("criteria"):
            print("SHIP_FEATURE_BLOCKED: work-item scope is frozen after deployment approval; revoke the approval or archive the run before changing the item set")
            return 1
        # A removable item carries no criteria, so by construction it is
        # outside the reviewed scope: the digest is unchanged and no
        # decision is invalidated (#82). The assertion keeps that invariant
        # honest if the scope definition ever drifts.
        before_scope = lib.work_item_scope_hash(items, acceptance.get("criteria", []))
        acceptance["work_items"] = [candidate for candidate in items if candidate.get("id") != args.item]
        lib.add_work_item_tombstone(acceptance, args.item, actor, datetime.now(timezone.utc).isoformat())
        delivery = status.get("work_item_delivery")
        if isinstance(delivery, dict):
            delivery.pop(args.item, None)
        after_scope = lib.work_item_scope_hash(acceptance["work_items"], acceptance.get("criteria", []))
        if not post_approval and before_scope != after_scope:
            print(f"SHIP_FEATURE_BLOCKED: removing {args.item} would change the reviewed scope; retag its criteria first")
            return 1
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="work_item_removed", event_message=f"Removed work item {args.item}",
                   by=actor, work_item=args.item, scope_changed=False, post_approval=post_approval)
    print(f"WORK_ITEM_REMOVED: {args.item}")
    return 0


def cmd_lane_request(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        delivery = status.get("work_item_delivery") or {}
        if args.item not in delivery:
            print(f"SHIP_FEATURE_BLOCKED: unknown work item {args.item}")
            return 1
        facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
        if not facts["eligible"]:
            print("SMALL_FIX_REFUSED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
            return 1
        record = delivery[args.item]
        record.update(requested_lane="small-fix", facts=facts,
                      baseline_head=lib.repository_snapshot(root)["head"], escalation_reason=None)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="work_item_lane_requested",
                   event_message=f"Requested small-fix lane for {args.item}",
                   by=actor, work_item=args.item, facts=facts)
    print(f"SMALL_FIX_REQUESTED: {args.item}")
    return 0


def cmd_lane_confirm(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        delivery = status.get("work_item_delivery") or {}
        record = delivery.get(args.item)
        if not isinstance(record, dict) or record.get("requested_lane") != "small-fix":
            print(f"SHIP_FEATURE_BLOCKED: {args.item} has no eligible small-fix request")
            return 1
        facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
        if not facts["eligible"]:
            print("SMALL_FIX_REFUSED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
            return 1
        now = datetime.now(timezone.utc).isoformat()
        record.update(lane="small-fix", confirmed_by=actor, confirmed_at=now,
                      facts=facts, escalation_reason=None)
        status.update(updated_at=now,
                      next_action="Implement the confirmed small fix and attach targeted evidence.")
        lib.commit(root, cfg, status=status, event_kind="work_item_lane_confirmed",
                   event_message=f"Pilot confirmed small-fix lane for {args.item}",
                   by=actor, work_item=args.item, facts=facts)
    print(f"SMALL_FIX_CONFIRMED: {args.item}")
    return 0


def cmd_plan_tranche(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        if args.issues_file:
            issues = json.loads(Path(args.issues_file).read_text(encoding="utf-8"))
            if not isinstance(issues, list):
                raise lib.HandsoffError("issues file must contain a JSON array")
        else:
            issues = tranche.fetch_issues(args.repo)
        archive_directory = Path(args.archive_dir).expanduser() if args.archive_dir else lib.archive_dir()
        proposal = tranche.build_proposal(issues, tranche.load_archives(archive_directory),
                                          args.repo, cfg, limit=args.limit)
        path = root / tranche.PROPOSAL_FILE
        lib._atomic_write_text(path, json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, lib.HandsoffError) as exc:
        print(f"TRANCHE_PLAN_BLOCKED: {exc}")
        return 1
    print(json.dumps({"proposal_path": str(path), **proposal}, indent=2))
    return 0


def cmd_tranche_approve(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        if int(status.get("phase_number", 1) or 1) != 1:
            print("TRANCHE_APPROVAL_BLOCKED: backlog tranches can only be approved in Phase 1")
            return 1
        path = root / tranche.PROPOSAL_FILE
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("TRANCHE_APPROVAL_BLOCKED: proposal artifact is missing or invalid")
            return 1
        if args.proposal_hash != proposal.get("proposal_hash") \
                or tranche.proposal_hash(proposal) != proposal.get("proposal_hash"):
            print("TRANCHE_APPROVAL_BLOCKED: proposal hash is stale or invalid")
            return 1
        proposed = list(proposal.get("proposed_order") or [])
        order = list(args.item or proposed)
        drops = list(args.drop or [])
        by_id = {f"issue-{row['number']}": row for row in proposal.get("issues", [])
                 if isinstance(row, dict) and isinstance(row.get("number"), int)}
        if len(proposed) != len(set(proposed)) or set(proposed) != set(by_id):
            print("TRANCHE_APPROVAL_BLOCKED: proposal item inventory is invalid")
            return 1
        if len(set(order)) != len(order) or len(set(drops)) != len(drops) \
                or set(order) & set(drops) or set(order) | set(drops) != set(proposed):
            print("TRANCHE_APPROVAL_BLOCKED: retained order and explicit drops must partition the proposal once")
            return 1
        existing = {item["id"]: item for item in acceptance.get("work_items", [])}
        now = datetime.now(timezone.utc).isoformat()
        persisted = []
        for item_id in order:
            source = by_id[item_id]
            previous = existing.get(item_id, {})
            persisted.append({
                "id": item_id, "kind": "issue", "number": source["number"],
                "title": source["title"], "url": source.get("url", ""), "required": True,
                "github_state": "open", "github_checked_at": now,
                "created_at": previous.get("created_at", now), "updated_at": now,
                "notes": previous.get("notes", ""),
            })
        proposed_acceptance = deepcopy(acceptance)
        proposed_acceptance["work_items"] = persisted
        errors = lib.validate_acceptance_schema(proposed_acceptance)
        if errors:
            print("TRANCHE_APPROVAL_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        proposed_status = deepcopy(status)
        old_delivery = proposed_status.get("work_item_delivery") or {}
        proposed_status["work_item_delivery"] = {
            item_id: old_delivery.get(item_id, lib.new_work_item_delivery([{"id": item_id}])[item_id])
            for item_id in order
        }
        proposed_status["tranche_approval"] = {
            "proposal_hash": args.proposal_hash, "proposed_order": proposed,
            "approved_order": order, "drops": drops, "by": actor, "at": now,
            "labels": {item_id: by_id[item_id].get("labels", []) for item_id in order},
        }
        proposed_status["active_work_item"] = order[0] if order else None
        proposed_status["updated_at"] = now
        lib.commit(root, cfg, status=proposed_status, acceptance=proposed_acceptance,
                   event_kind="tranche_approved", event_message="Pilot approved backlog tranche",
                   proposal_hash=args.proposal_hash, proposed_order=proposed,
                   approved_order=order, drops=drops, by=actor)
    print(f"TRANCHE_APPROVED: {', '.join(order) or 'empty'}")
    return 0


def cmd_release_plan(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    plan = lib.release_plan_payload(
        cfg, args.version, actor, override_reason=args.full_regression_override_reason,
    )
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        lib.ensure_no_launched_regression(status)
        active = lib.active_regression_request(status)
        if active and active.get("state") in {"awaiting_approval", "accepted"}:
            active["state"] = "invalidated"
            active["completed_at"] = plan["planned_at"]
        status["release_plan"] = plan
        status["updated_at"] = plan["planned_at"]
        lib.commit(
            root, cfg, status=status, event_kind="release_planned",
            event_message=f"{plan['release_class'].capitalize()} release {plan['version']} planned",
            version=plan["version"], release_class=plan["release_class"],
            full_regression_eligible=plan["full_regression_eligible"], by=actor,
        )
    print(json.dumps(plan, indent=2))
    return 0


def cmd_release_reconcile(args) -> int:
    """Run or resume the Phase-7 release transaction. #378: --inspect only
    prints the record's state; it takes no plan, lock or ledger and never
    writes the record."""
    if args.inspect:
        root = lib.resolve_root(args.root)
        store = release_runtime.tx.JsonFileRecordStore(root / release_runtime.RECORD_NAME)
        try:
            inspection = release_runtime.tx.ReleaseTransaction(None, store, None).inspect()
        except release_runtime.tx.ReleaseTransactionError as exc:
            print(f"RELEASE_BLOCKED: {exc}")
            return 1
        print(json.dumps(inspection, sort_keys=True))
        return 0
    if not args.artifact or not args.by:
        print("RELEASE_BLOCKED: release-reconcile needs --artifact and --by (only --inspect runs without them)")
        return 1
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, _acceptance, records, problems = _load_all(root, cfg)
        except lib.HandsoffError as exc:
            print(f"RELEASE_BLOCKED: {exc}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 7:
            print("RELEASE_BLOCKED: release reconciliation requires Phase 7")
            return 1
        if lib.adaptive_deployment_approval_required(status, cfg) and not status.get("deployment_approved"):
            print("RELEASE_BLOCKED: deployment approval is required before publishing")
            return 1
        plan = deepcopy(status.get("release_plan"))
        if not isinstance(plan, dict):
            print("RELEASE_BLOCKED: record release-plan before publishing")
            return 1
        plan_binding = json.dumps(plan, sort_keys=True, separators=(",", ":"))

    def rooted(value, default=None):
        path = Path(value) if value else default
        if path is not None and not path.is_absolute():
            path = root / path
        return path

    try:
        evidence = release_runtime.reconcile_release(
            root, plan, rooted(args.artifact), repository=args.repository, commit=args.commit,
            manifest=rooted(args.manifest, root / "handsoff-runtime.json"),
            notes_file=rooted(args.notes_file),
        )
    except (release_runtime.ReleaseProviderError,
            release_runtime.tx.ReleaseTransactionError, ValueError) as exc:
        print(f"RELEASE_BLOCKED: {exc}")
        return 1
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        current = status.get("release_plan")
        if status.get("phase_number") != 7 or not isinstance(current, dict) \
                or json.dumps(current, sort_keys=True, separators=(",", ":")) != plan_binding:
            print("RELEASE_BLOCKED: workflow state or release plan changed during reconciliation")
            return 1
        lib.commit(root, cfg, status=status, event_kind="release_reconciled",
                   event_message=f"Release {plan['version']} published, installed and manifest-verified",
                   by=actor, release=evidence)
    print(json.dumps(evidence, indent=2))
    return 0


def cmd_regression_request(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    group = lib.regression_group(cfg, args.group)
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        plan = status.get("release_plan")
        if cfg["regression_gate"].get("full_regression_major_only", True):
            if not isinstance(plan, dict):
                print("REGRESSION_BLOCKED: record a semantic release-plan before requesting a full regression")
                return 1
            if not plan.get("full_regression_eligible"):
                print(f"REGRESSION_BLOCKED: {plan.get('release_class')} releases use targeted tests; "
                      "record an explicit full-regression override reason to proceed")
                return 1
        if lib.active_regression_request(status):
            print("REGRESSION_BLOCKED: another regression request is already live")
            return 1
        now = datetime.now(timezone.utc)
        requests = list(status.get("regression_requests") or [])
        terminals = [item for item in requests if item.get("state") not in {"awaiting_approval", "accepted", "launched"}]
        while len(requests) >= lib.MAX_REGRESSION_REQUESTS and terminals:
            victim = terminals.pop(0)
            requests.remove(victim)
        if len(requests) >= lib.MAX_REGRESSION_REQUESTS:
            print("REGRESSION_BLOCKED: request history is full")
            return 1
        item = {
            "request_id": f"rg-{uuid.uuid4().hex}", "group": group["name"],
            "commands": list(group["commands"]), "command_sha256": lib.command_sha256(group["commands"]),
            "timeout_seconds": group.get("timeout_seconds", cfg.get("check_timeout_seconds", 600)),
            "state": "awaiting_approval", "requested_by": actor, "requested_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=cfg["regression_gate"]["approval_timeout_minutes"])).isoformat(),
            "decided_by": None, "decided_at": None, "launched_at": None, "completed_at": None,
            "launch_nonce_sha256": None, "reason": args.reason.strip(),
            "requester_session_id": next((sid for sid, session in (status.get("agent_sessions") or {}).items()
                                          if session.get("actor") == actor), None),
            "release_version": plan.get("version") if isinstance(plan, dict) else None,
            "release_class": plan.get("release_class") if isinstance(plan, dict) else None,
            "policy_override_reason": plan.get("full_regression_override_reason")
            if isinstance(plan, dict) else None,
            **_regression_bindings(root, cfg, acceptance, status), "results": [],
        }
        status["regression_requests"] = [*requests, item]
        status["updated_at"] = now.isoformat()
        lib.commit(root, cfg, status=status, event_kind="regression_requested",
                   event_message=f"Regression group {group['name']} awaits Pilot approval",
                   request_id=item["request_id"], group=group["name"], command_sha256=item["command_sha256"])
    print(f"REGRESSION_AWAITING_APPROVAL: {item['request_id']}")
    return 0


def cmd_regression_decide(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    decision = "accepted" if args.accept else "declined"
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "awaiting_approval":
            print("REGRESSION_BLOCKED: request is not awaiting approval")
            return 1
        now = datetime.now(timezone.utc)
        if now > datetime.fromisoformat(item["expires_at"]):
            item["state"] = "expired"
            item["completed_at"] = now.isoformat()
            decision = "expired"
        elif not _same_regression_bindings(item, root, cfg, acceptance, status):
            item["state"] = "invalidated"
            item["completed_at"] = now.isoformat()
            decision = "invalidated"
        else:
            item["state"] = decision
        item["decided_by"] = actor
        item["decided_at"] = now.isoformat()
        status["updated_at"] = now.isoformat()
        lib.commit(root, cfg, status=status, event_kind=f"regression_{decision}",
                   event_message=f"Regression request {decision}", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_{decision.upper()}: {item['request_id']}")
    return 0 if decision in {"accepted", "declined"} else 1


def cmd_regression_cancel(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") not in {"awaiting_approval", "accepted"}:
            print("REGRESSION_BLOCKED: only a pending or accepted request can be cancelled")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        item["state"] = "cancelled"
        item["completed_at"] = now
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="regression_cancelled",
                   event_message="Regression request cancelled", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_CANCELLED: {item['request_id']}")
    return 0


def cmd_regression_run(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    raw_nonce = uuid.uuid4().hex
    nonce_digest = hashlib.sha256(raw_nonce.encode()).hexdigest()
    with lib.project_lock(root):
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "accepted":
            print("REGRESSION_BLOCKED: request has not been accepted")
            return 1
        now = datetime.now(timezone.utc)
        accepted_at = datetime.fromisoformat(item["decided_at"])
        launch_deadline = accepted_at + timedelta(minutes=cfg["regression_gate"]["launch_window_minutes"])
        if now > launch_deadline or not _same_regression_bindings(item, root, cfg, acceptance, status):
            item["state"] = "expired" if now > launch_deadline else "invalidated"
            item["completed_at"] = now.isoformat()
            status["updated_at"] = now.isoformat()
            lib.commit(root, cfg, status=status, event_kind=f"regression_{item['state']}",
                       event_message=f"Regression request {item['state']} before launch", request_id=item["request_id"])
            print(f"REGRESSION_{item['state'].upper()}: {item['request_id']}")
            return 1
        item["state"] = "launched"
        item["launched_at"] = now.isoformat()
        item["launch_nonce_sha256"] = nonce_digest
        status["updated_at"] = now.isoformat()
        commands = list(item["commands"])
        lib.commit(root, cfg, status=status, event_kind="regression_launched",
                   event_message=f"Accepted regression group {item['group']} launched",
                   request_id=item["request_id"], by=actor)
    # Full regressions have their own progress-aware execution path. Targeted
    # verification deliberately remains on lib.run_checks so sharding cannot
    # alter evidence-cache semantics outside a Pilot-accepted gate.
    results = regress.run_battery_results(
        root, item["group"], commands,
        timeout=item.get("timeout_seconds", cfg.get("check_timeout_seconds", 600)),
        request_id=item["request_id"], command_sha256=item["command_sha256"],
        max_shards=regress.DEFAULT_SHARDS,
    )
    with lib.project_lock(root):
        # Re-read policy after the command returns. Reusing the pre-launch
        # object would let a concurrent policy edit pass its own comparison.
        cfg = lib.load_config(root)
        status, acceptance = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "launched" or item.get("launch_nonce_sha256") != nonce_digest:
            print("REGRESSION_LATE_RESULT_IGNORED")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        bindings_valid = _same_regression_bindings(item, root, cfg, acceptance, status)
        item["state"] = ("completed" if all(result["exit_code"] == 0 for result in results) else "failed") \
            if bindings_valid else "invalidated"
        item["completed_at"] = now
        item["results"] = _durable_results(results)
        item["env"] = lib.recorded_check_env(cfg)  # #410: bound through _regression_bindings
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind=f"regression_{item['state']}",
                   event_message=f"Regression request {item['state']}", request_id=item["request_id"])
    print(__import__("json").dumps({"request_id": item["request_id"], "state": item["state"], "results": results}, indent=2))
    return 0 if item["state"] == "completed" else 1


def cmd_regression_finalize(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        item = _regression_request(status, args.request_id)
        if not item or item.get("state") != "launched":
            print("REGRESSION_BLOCKED: request is not launched")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        item["state"] = "failed"
        item["completed_at"] = now
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="regression_failed",
                   event_message="Pilot terminalized a stranded regression launch", request_id=item["request_id"], by=actor)
    print(f"REGRESSION_FAILED: {item['request_id']}")
    return 0


def cmd_verify(args) -> int:
    """Run checks and bind the immutable result to named criteria.

    Each named criterion is judged ONLY by its own configured `tests`, never
    by the outcome of some other command in [checks].commands that happens
    to be configured globally. Two named criteria with different tests get
    two independent verification records: an unrelated criterion's failing
    test must never fail this one, and an unrelated passing command must
    never satisfy it either. The union of needed commands is still run only
    once per invocation, for efficiency when criteria share a test.

    #43: each needed command is bound to the exact state it proves
    something about (lib.verification_binding). A command whose binding
    already has an eligible executed, passing record in this run is not
    launched again unless --no-cache is passed; its criterion gets a
    record marked executed false with reused_from naming the source. The
    binding locks under .handsoff-verify-inflight/ serialize concurrent
    verifies of one binding so the second re-reads the ledger and reuses
    instead of launching a duplicate."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not cfg.get("check_commands"):
        print("SHIP_FEATURE_NO_CHECKS_CONFIGURED: set [checks].commands in handsoff.toml")
        return 1
    with lib.project_lock(root):
        status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
        lib.ensure_no_launched_regression(status)
        audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if getattr(args, "all", False):
            # #363: every criterion whose policy needs checks, in registry
            # order, through the same union path as a named list
            if getattr(args, "expect_fail", False):
                print("SHIP_FEATURE_BLOCKED: --all does not apply to --expect-fail; record each baseline with --criterion")
                return 1
            args.criterion = [c["id"] for c in acceptance.get("criteria", [])
                              if "checks" in lib.required_evidence_kinds(c)]
            if not args.criterion:
                print("SHIP_FEATURE_BLOCKED: --all found no automated criteria in the registry")
                return 1
        criteria = [_criterion(acceptance, cid) for cid in args.criterion]
        missing =[cid for cid, criterion in zip(args.criterion, criteria) if criterion is None]
        if missing:
            print(f"SHIP_FEATURE_BLOCKED: unknown criteria: {', '.join(missing)}")
            return 1
        non_automated = [c["id"] for c in criteria
                         if "checks" not in lib.required_evidence_kinds(c)]
        if non_automated:
            print(f"SHIP_FEATURE_BLOCKED: verify cannot satisfy non-automated criteria: {', '.join(non_automated)}")
            return 1
        configured = set(cfg["check_commands"])
        unmatched = {c["id"]: [test for test in c.get("tests", []) if test not in configured]
                     for c in criteria}
        unmatched = {cid: tests for cid, tests in unmatched.items() if tests}
        if unmatched:
            print("SHIP_FEATURE_BLOCKED: criterion tests must exactly match configured check commands: "
                  + __import__("json").dumps(unmatched, sort_keys=True))
            return 1
        before = {c["id"]: lib.criterion_spec_hash(c) for c in criteria}
        needed_set = {test for c in criteria for test in c.get("tests", [])}
        needed = [cmd for cmd in cfg["check_commands"] if cmd in needed_set]
        # #43: bind before launching, so the binding describes the state
        # the command actually ran against, not whatever it left behind.
        run_hash = lib.feature_hash(status, lib.read_events(root, cfg))
        repo_digest = lib.repository_digest(root, cfg)
        # #411: the pre-run snapshot, so a command that edits the tree while
        # it runs can be named against what it started from
        _write_digest_snapshot(root, cfg, repo_digest)
        config_digest = lib.verification_config_hash(cfg)
        # P1.2: a criterion that declares paths binds to its scoped digest
        scope_digests = (lib.criterion_scope_digests(criteria, lib.repository_digest_entries(root, cfg))
                         if any(c.get("paths") for c in criteria) else {})

        def scoped_pairs(cmd: str) -> list[tuple[str, str]] | None:
            covered = [c for c in criteria if cmd in c.get("tests", [])]
            if not any(c["id"] in scope_digests for c in covered):
                return None
            return [(before[c["id"]], scope_digests.get(c["id"], repo_digest)) for c in covered]

        bindings = {
            cmd: lib.verification_binding(cmd, repo_digest, config_digest,
                                          [before[c["id"]] for c in criteria if cmd in c.get("tests", [])],
                                          scoped=scoped_pairs(cmd))
            for cmd in needed
        }
    expect_fail = bool(getattr(args, "expect_fail", False))
    # #169: a repeat criterion runs its own commands N times, no cache.
    repeated = {c["id"]: c for c in criteria if c.get("repeat")}
    if repeated and expect_fail:
        print("SHIP_FEATURE_BLOCKED: --expect-fail does not apply to a repeat criterion; record its baseline separately")
        return 1
    # #165: a baseline is always executed against THIS tree; a cached
    # passing record from an earlier binding would be the opposite claim.
    use_cache = not getattr(args, "no_cache", False) and not expect_fail and not repeated
    with lib.verify_inflight_lock(root, list(bindings.values())):
        reused: dict[str, dict] = {}
        if use_cache:
            with lib.project_lock(root):
                # Re-read under the binding lock: a concurrent verify of the
                # same binding that held it before us has appended by now.
                ledger_records, _ = lib.load_verifications(root, cfg)
            for cmd in needed:
                source = lib.reusable_check_record(ledger_records, cmd, bindings[cmd], run_hash)
                if source is not None:
                    reused[cmd] = source
        launched = [cmd for cmd in needed if cmd not in reused]
        if launched or not use_cache:
            # #414: a paused run binds cached results only; running a
            # command is new work, so a cache miss or --no-cache is refused
            refusal = paused_verify_refusal(root, launched, use_cache)
            if refusal:
                print(f"SHIP_FEATURE_BLOCKED: {refusal}")
                return 1
        repeat_attempts: dict[str, list[dict]] = {}
        results_by_command: dict[str, dict] = {}
        for cmd, source in reused.items():
            copied = next(r for r in source["results"] if r.get("command") == cmd)
            results_by_command[cmd] = {**copied, "reused_from": source["run_id"]}
        check_env = lib.recorded_check_env(cfg)  # #410: the table only
        concurrency = int(cfg.get("check_concurrency", 1) or 1)
        written: dict[str, dict] = {}

        def write_record(status: dict, criterion: dict, records: list[dict]) -> tuple[dict, dict]:
            """Append one criterion's record and set its state. The caller
            holds the project lock and commits `status` and the acceptance."""
            own_tests = list(criterion.get("tests", []))
            own_results = [results_by_command[t] for t in own_tests]
            # #410: passing only when every one of its commands passed
            own_ok = bool(own_results) and all(r["exit_code"] == 0 for r in own_results)
            attempts = repeat_attempts.get(criterion["id"])
            if attempts is not None:
                # #169: every attempt must pass; the record names the one that did not
                own_ok = bool(attempts) and all(a["ok"] for a in attempts) and len(attempts) == int(criterion["repeat"])
            own_sources = {reused[t]["run_id"] for t in own_tests if t in reused}
            # A record is executed only when every one of its commands
            # was launched here; a partially reused multi-command record
            # is not a reuse source, and names its source only when all
            # of its reused results came from the same record (each
            # copied result carries its own reused_from regardless).
            executed = not own_sources
            record_digest = repo_digest if executed else next(
                (reused[t].get("repository_digest") for t in own_tests if t in reused), None)
            reused_from = next(iter(own_sources)) if len(own_sources) == 1 else None
            failed_attempt = next((a for a in (attempts or []) if not a["ok"]), None)
            record = lib.append_verification(
                root, cfg, kind="checks", ok=own_ok, by=args.by,
                criteria=[criterion], results=_durable_results(own_results),
                commands=own_tests,
                binding={t: bindings[t] for t in own_tests}, executed=executed,
                reused_from=reused_from, feature_hash=run_hash,
                repository_digest=record_digest, config_digest=config_digest,
                attempts=attempts, env=check_env, concurrency=concurrency,
                scope_digests={criterion["id"]: scope_digests[criterion["id"]]}
                if criterion["id"] in scope_digests else None,
                description=(f"repeat {criterion['repeat']}: failed at attempt {failed_attempt['attempt']}"
                             + (f" (seed {failed_attempt['seed']})" if failed_attempt.get("seed") else "")
                             if failed_attempt else f"repeat {criterion['repeat']}: {len(attempts)}/{criterion['repeat']} passed")
                if attempts is not None else None)
            if record_digest:
                # the executed digest's snapshot was taken before the run (#411)
                _write_digest_snapshot(root, cfg, record_digest)
            status["verification_head"] = record["hash"]
            if record["run_id"] not in criterion["evidence"]:
                criterion["evidence"].append(record["run_id"])
            if not own_ok:
                criterion["state"] = "failing"
            elif lib.criterion_fully_evidenced(criterion, records + [record]):
                criterion["state"] = "passing"
            else:
                criterion["state"] = "not_tested"
            return record, {"run_id": record["run_id"], "ok": own_ok,
                            "executed": executed, "reused_from": reused_from,
                            **({"attempts": len(attempts), "repeat": int(criterion["repeat"])}
                               if attempts is not None else {})}

        def record_completed(cmd: str, result: dict) -> None:
            """#410: as each concurrent command finishes, every criterion
            whose commands are now all done is written in one single-writer
            transaction under the project lock. A criterion that changed or
            an audit failure is left to the final pass, which refuses it."""
            results_by_command[cmd] = result
            ready = [cid for cid in args.criterion if cid not in written and cid not in repeated
                     and all(t in results_by_command for t in tests_by_criterion[cid])]
            if not ready:
                return
            with lib.project_lock(root):
                status, acceptance, records, problems = _load_all(root, cfg)
                if _audit_errors(root, cfg, status, records, problems):
                    return
                fresh = {cid: _criterion(acceptance, cid) for cid in ready}
                if any(c is None or lib.criterion_spec_hash(c) != before[cid] for cid, c in fresh.items()):
                    return
                entries = {}
                for cid in ready:
                    record, entries[cid] = write_record(status, fresh[cid], records)
                    records.append(record)
                lib.sync_coverage(status, acceptance)
                status["updated_at"] = datetime.now(timezone.utc).isoformat()
                lib.commit(root, cfg, status=status, acceptance=acceptance,
                           event_kind="criterion_checks_recorded",
                           event_message=f"Recorded checks for {', '.join(ready)} as their commands finished",
                           criteria=entries, command=cmd, concurrency=concurrency)
                written.update(entries)

        tests_by_criterion = {c["id"]: list(c.get("tests", [])) for c in criteria}
        concurrent = concurrency > 1 and not expect_fail and not repeated and len(launched) > 1
        if repeated:
            # each repeat criterion's commands run N times on their own; a
            # command shared with a plain criterion is run for that one too
            for criterion in repeated.values():
                own = list(criterion.get("tests", []))
                last, attempts = lib.run_repeated_checks(cfg, root, own, int(criterion["repeat"]),
                                                         criterion.get("seed_env"), run_hash)
                repeat_attempts[criterion["id"]] = attempts
                for r in last:
                    results_by_command.setdefault(r["command"], r)
            plain_needed = [cmd for cmd in launched if cmd not in results_by_command]
            results = (lib.run_checks(cfg, root, commands=plain_needed) if plain_needed else []) \
                + [results_by_command[cmd] for cmd in launched if cmd in results_by_command]
        elif concurrent:
            results = lib.run_checks(cfg, root, commands=launched, concurrency=concurrency,
                                     on_result=record_completed)
        else:
            results = lib.run_checks(cfg, root, commands=launched) if launched else []
        results_by_command.update({r["command"]: r for r in results})
        results = [results_by_command[cmd] for cmd in needed]
        # #411: the digest when the commands completed, never the pre-run one
        completion_digest = lib.repository_digest(root, cfg)
        with lib.project_lock(root):
            status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
            audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
            if audit_errors:
                return _print_audit_block(audit_errors)
            criteria = [_criterion(acceptance, cid) for cid in args.criterion]
            if any(c is None for c in criteria) or any(lib.criterion_spec_hash(c) != before[c["id"]] for c in criteria):
                print("SHIP_FEATURE_BLOCKED: criterion changed while checks were running; run verify again")
                return 1
            per_criterion = {}
            if expect_fail:
                # #165: one baseline record per criterion. Valid (ok) when
                # every own command failed, invalid when any passed; either
                # way the criterion's state is untouched, only its evidence
                # list grows, and the later green run is judged against it.
                for criterion in criteria:
                    own_tests = list(criterion.get("tests", []))
                    own_results = [results_by_command[t] for t in own_tests]
                    valid = bool(own_results) and all(r["exit_code"] != 0 for r in own_results)
                    record = lib.append_verification(
                        root, cfg, kind="baseline", ok=valid, by=args.by,
                        criteria=[criterion], results=_durable_results(own_results),
                        commands=own_tests, binding={t: bindings[t] for t in own_tests},
                        executed=True, feature_hash=run_hash, repository_digest=repo_digest,
                        config_digest=config_digest, env=check_env,
                        description="baseline: commands expected to fail before the feature"
                        if valid else "baseline_invalid: a command passed before the feature")
                    status["verification_head"] = record["hash"]
                    if record["run_id"] not in criterion["evidence"]:
                        criterion["evidence"].append(record["run_id"])
                    per_criterion[criterion["id"]] = {"run_id": record["run_id"], "ok": valid,
                                                      "baseline": valid, "baseline_invalid": not valid,
                                                      "executed": True, "reused_from": None}
                status["updated_at"] = datetime.now(timezone.utc).isoformat()
                lib.commit(root, cfg, status=status, acceptance=acceptance,
                          event_kind="baseline_recorded",
                          event_message="Ran the criteria's own checks expecting failure (failing-first baseline)",
                          criteria=per_criterion, launched_count=len(launched),
                          results=[{"command": r["command"], "exit_code": r["exit_code"],
                                    "output_sha256": r["output_sha256"]} for r in results])
                overall_ok = all(v["ok"] for v in per_criterion.values())
                print(__import__("json").dumps({"ok": overall_ok, "expected": "fail", "criteria": per_criterion,
                                                "results": results, "launched": launched, "reused": {}}, indent=2))
                return 0 if overall_ok else 1
            new_records: list[dict] = []
            for criterion in criteria:
                if criterion["id"] in written:
                    # #410: recorded under the lock as its last command finished
                    per_criterion[criterion["id"]] = written[criterion["id"]]
                    continue
                record, per_criterion[criterion["id"]] = write_record(
                    status, criterion, existing_records + new_records)
                new_records.append(record)
            symptom_run = _automatic_symptom_run(status, acceptance, per_criterion, existing_records)
            symptom_events = []
            if symptom_run:
                # #419: every primary_fix passes on automated evidence, so the
                # symptom is recorded here, bound to this run and this actor.
                status["requirement_coverage"]["original_symptom_resolved"] = True
                status["original_symptom_evidence_id"] = symptom_run
                symptom_events = [{"kind": "symptom_resolved", "message": "Original symptom marked resolved by verify",
                                   "evidence": symptom_run, "by": args.by, "automatic": True}]
            lib.sync_coverage(status, acceptance)
            # #411: after every evidence mutation, so the rebound hash is final
            retention = _retain_or_invalidate(root, cfg, status, acceptance, digest_before=repo_digest,
                                              digest_after=completion_digest, rechecked=criteria)
            _name_symptom_step(status, acceptance, existing_records + [
                {"run_id": v["run_id"], "ok": v["ok"], "criteria": [cid],
                 "criterion_hashes": {cid: before[cid]}} for cid, v in per_criterion.items()])
            review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.commit(root, cfg, status=status, acceptance=acceptance,
                      event_kind="checks_run", event_message="Ran and attached configured checks",
                      extra_events=(symptom_events + _retention_events(retention)) or None,
                      review_retention=retention,
                      criteria=per_criterion,
                      review_attempt_refreshed=review_refresh,
                      launched_count=len(launched), reused_count=len(reused),
                      results=[{"command": r["command"], "exit_code": r["exit_code"],
                                "output_sha256": r["output_sha256"]} for r in results])
    overall_ok = all(v["ok"] for v in per_criterion.values())
    print(__import__("json").dumps({
        "ok": overall_ok, "criteria": per_criterion, "results": results,
        "launched": launched, "reused": {cmd: source["run_id"] for cmd, source in reused.items()},
        **({"original_symptom_resolved": symptom_run} if symptom_run else {}),
        **({"review": retention} if retention else {}),
    }, indent=2))
    if symptom_run:
        # #419: stdout stays one JSON document; the marker is on stderr
        print(f"ORIGINAL_SYMPTOM_RESOLVED: {symptom_run}", file=sys.stderr)
    return 0 if overall_ok else 1


AUTOMATED_EVIDENCE_KINDS = {"checks", "mutation"}


def _automatic_symptom_run(status: dict, acceptance: dict, per_criterion: dict, records: list[dict]) -> str | None:
    """#419: the run id verify records the original symptom against, or None.
    Only when every primary_fix criterion is passing on automated evidence
    (a manual primary still needs record-symptom-resolved), this run
    verified one of them green, and no valid symptom record already stands."""
    primary = [c for c in acceptance.get("criteria", []) if c.get("type") == "primary_fix"]
    if not primary or status.get("lane") == "review":
        return None
    for criterion in primary:
        kinds = lib.required_evidence_kinds(criterion)
        if not kinds or not kinds <= AUTOMATED_EVIDENCE_KINDS or criterion.get("state") != "passing":
            return None
    if (status.get("requirement_coverage") or {}).get("original_symptom_resolved") is True \
            and lib._valid_symptom_record(status, primary, records):
        return None
    return next((per_criterion[c["id"]]["run_id"] for c in primary
                 if (per_criterion.get(c["id"]) or {}).get("ok")), None)


def _name_symptom_step(status: dict, acceptance: dict, records: list[dict]) -> None:
    """#419: when every primary_fix passes but the symptom still waits on
    record-symptom-resolved (manual primary evidence), next_action names
    the exact command with the candidate run id."""
    if (status.get("requirement_coverage") or {}).get("original_symptom_resolved") is True:
        return
    primary = [c for c in acceptance.get("criteria", []) if c.get("type") == "primary_fix"]
    if not primary or any(c.get("state") != "passing" for c in primary):
        return
    step = lib.symptom_resolution_step(acceptance, records)
    if step:
        status["next_action"] = f"Record the original symptom resolved: {step}"


def cmd_record_evidence(args) -> int:
    """Record named manual/browser evidence in the immutable ledger."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.description or not args.description.strip():
        print("SHIP_FEATURE_BLOCKED: --description must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        required_kinds = lib.required_evidence_kinds(criterion)
        if args.kind not in required_kinds:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} policy does not accept {args.kind} evidence")
            return 1
        record = lib.append_verification(root, cfg, kind=args.kind, ok=True, by=args.by,
                                         criteria=[criterion], description=args.description)
        status["verification_head"] = record["hash"]
        criterion["evidence"].append(record["run_id"])
        criterion["state"] = ("passing" if lib.criterion_fully_evidenced(criterion, records + [record])
                              else "not_tested")
        lib.sync_coverage(status, acceptance)
        digest = lib.repository_digest(root, cfg)  # #411: nothing runs, so one digest is both ends
        retention = _retain_or_invalidate(root, cfg, status, acceptance, digest_before=digest,
                                          digest_after=digest, rechecked=[criterion])
        _name_symptom_step(status, acceptance, records + [record])  # #419
        review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                  event_kind="evidence_recorded", event_message="Attached criterion evidence",
                  extra_events=_retention_events(retention) or None, review_retention=retention,
                  run_id=record["run_id"], criterion=args.criterion, by=args.by,
                  review_attempt_refreshed=review_refresh)
    print(f"EVIDENCE_RECORDED: {record['run_id']}")
    if retention:
        print("REVIEW_RETAINED" if retention["retained"] else f"REVIEW_REVOKED: {retention['reason']}")
    return 0


def _attach_evidence(root: Path, cfg: dict, status: dict, acceptance: dict, records: list[dict],
                     criterion: dict, record: dict) -> tuple[dict | None, bool]:
    """Bind one appended record to its criterion as record-evidence does:
    the head, the evidence list, the state, coverage, review retention.
    Returns (retention, review refreshed). The caller holds the lock and commits."""
    status["verification_head"] = record["hash"]
    if record["run_id"] not in criterion["evidence"]:
        criterion["evidence"].append(record["run_id"])
    if not record["ok"]:
        criterion["state"] = "failing"
    else:
        criterion["state"] = ("passing" if lib.criterion_fully_evidenced(criterion, records + [record])
                              else "not_tested")
    lib.sync_coverage(status, acceptance)
    digest = lib.repository_digest(root, cfg)
    retention = _retain_or_invalidate(root, cfg, status, acceptance, digest_before=digest,
                                      digest_after=digest, rechecked=[criterion])
    _name_symptom_step(status, acceptance, records + [record])  # #419
    review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    return retention, review_refresh


def cmd_workflow_check(args) -> int:
    """P1.4: run [checks].workflow_commands (a disposable-repository
    harness) and record kind workflow for one criterion, bound to the digest
    of its workflow files. The only writer of workflow evidence: checks
    never satisfy a criterion that changes .github/workflows/."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    commands = list(cfg.get("workflow_commands") or [])
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if "workflow" not in lib.required_evidence_kinds(criterion):
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} needs no workflow evidence: its paths "
                  "name no .github/workflows/ file and it declares no evidence_classes workflow")
            return 1
        if not commands:
            print("SHIP_FEATURE_NO_WORKFLOW_COMMANDS_CONFIGURED: set [checks].workflow_commands in handsoff.toml")
            return 1
        spec_before = lib.criterion_spec_hash(criterion)
        files_before = lib.workflow_digest(criterion, lib.repository_digest_entries(root, cfg))
        repo_digest = lib.repository_digest(root, cfg)
        _write_digest_snapshot(root, cfg, repo_digest)  # names the files a later change staled
    ran_env = lib.recorded_check_env(cfg)
    results = lib.run_checks(cfg, root, commands, progress_source="workflow-check")
    ok = all(r["exit_code"] == 0 for r in results)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None or lib.criterion_spec_hash(criterion) != spec_before:
            print("SHIP_FEATURE_BLOCKED: criterion changed while the workflow harness ran; run workflow-check again")
            return 1
        if lib.workflow_digest(criterion, lib.repository_digest_entries(root, cfg)) != files_before:
            print("SHIP_FEATURE_BLOCKED: the workflow files changed while the harness ran; run workflow-check again")
            return 1
        record = lib.append_verification(
            root, cfg, kind="workflow", ok=ok, by=args.by, criteria=[criterion],
            results=_durable_results(results), commands=commands,
            config_digest=lib.verification_config_hash(cfg), repository_digest=repo_digest,
            env=ran_env, scope_digests={criterion["id"]: files_before},
            description="workflow harness on " + ", ".join(lib.criterion_workflow_paths(criterion)))
        retention, review_refresh = _attach_evidence(root, cfg, status, acceptance, records, criterion, record)
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="workflow_checked", event_message="Ran the workflow harness for a criterion",
                   extra_events=_retention_events(retention) or None, review_retention=retention,
                   run_id=record["run_id"], criterion=args.criterion, by=args.by, ok=ok,
                   review_attempt_refreshed=review_refresh)
    print(json.dumps({"ok": ok, "run_id": record["run_id"], "criterion_state": criterion["state"],
                      "workflow_digest": files_before, "results": results}, indent=2))
    return 0 if ok else 1


QA_DIR = ".handsoff-qa"
QA_LIST_FIELDS = ("steps", "findings", "artifacts", "generated_tests")
MAX_QA_LIST_ENTRIES = 256


def _qa_origin(target: str) -> str | None:
    """P1.5: the canonical scheme://host[:port] of a QA target URL."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(target.strip())
        return lib.normalize_public_origins([f"{parts.scheme}://{parts.netloc}"], "target")[0]
    except (lib.HandsoffError, ValueError):
        return None


def _qa_report_view(entry: dict) -> dict:
    return {key: entry.get(key) for key in ("id", "author", "target", "added_at", "findings", "mapped")}


def pending_qa_reports(status: dict) -> list[dict]:
    """P1.5: QA reports no reviewer has mapped to a criterion yet. They
    count toward no gate."""
    return [_qa_report_view(entry) for entry in status.get("qa_reports") or []
            if isinstance(entry, dict) and not entry.get("mapped")]


def cmd_qa_report_add(args) -> int:
    """P1.5: store a QA agent's report as an untrusted side record. It is
    never evidence; only qa-report map by another actor makes it so."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    origin = _qa_origin(args.target)
    if origin is None or origin not in cfg.get("qa_targets", []):
        allowed = ", ".join(cfg.get("qa_targets", [])) or "none configured"
        print(f"SHIP_FEATURE_BLOCKED: QA target {args.target} is not an allowlisted [qa].targets origin "
              f"({allowed})")
        return 1
    try:
        report = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"SHIP_FEATURE_BLOCKED: cannot read QA report {args.file}: {exc}")
        return 1
    if not isinstance(report, dict):
        print("SHIP_FEATURE_BLOCKED: a QA report must be a JSON object")
        return 1
    for field in QA_LIST_FIELDS:
        value = report.get(field, [])
        if not isinstance(value, list) or len(value) > MAX_QA_LIST_ENTRIES:
            print(f"SHIP_FEATURE_BLOCKED: QA report '{field}' must be a list of at most {MAX_QA_LIST_ENTRIES} entries")
            return 1
    with lib.project_lock(root):
        status, _acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        report_id = f"qa-{uuid.uuid4().hex[:12]}"
        stored = {"id": report_id, "author": args.by.strip(), "target": args.target.strip(), "origin": origin,
                  "trusted": False, "added_at": datetime.now(timezone.utc).isoformat(),
                  **{field: report.get(field, []) for field in QA_LIST_FIELDS}}
        body = json.dumps(stored, indent=2, sort_keys=True)
        (root / QA_DIR).mkdir(exist_ok=True)
        # created once, never rewritten: map re-hashes it against report_sha256
        with (root / QA_DIR / f"{report_id}.json").open("x", encoding="utf-8") as fh:
            fh.write(body)
        status.setdefault("qa_reports", []).append({
            "id": report_id, "author": stored["author"], "target": stored["target"],
            "added_at": stored["added_at"], "findings": len(stored["findings"]),
            "report_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "mapped": []})
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="qa_report_added",
                   event_message="Stored an untrusted QA report", report=report_id,
                   target=stored["target"], by=stored["author"])
    print(f"QA_REPORT_STORED: {report_id} (untrusted; map it to a criterion with qa-report map)")
    return 0


def cmd_qa_report_map(args) -> int:
    """P1.5: a reviewer other than the report's author records browser
    evidence for one criterion, citing the report."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        entry = next((item for item in status.get("qa_reports") or []
                      if isinstance(item, dict) and item.get("id") == args.report), None)
        if entry is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown QA report {args.report}")
            return 1
        if args.by.strip().casefold() == str(entry.get("author", "")).strip().casefold():
            print(f"SHIP_FEATURE_BLOCKED: {args.by} wrote QA report {args.report}; "
                  "a different actor must map it to a criterion")
            return 1
        try:
            body = (root / QA_DIR / f"{args.report}.json").read_text(encoding="utf-8")
        except OSError as exc:
            print(f"SHIP_FEATURE_BLOCKED: cannot read QA report {args.report}: {exc}")
            return 1
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != entry.get("report_sha256"):
            print(f"SHIP_FEATURE_BLOCKED: QA report {args.report} changed since it was stored")
            return 1
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if "browser" not in lib.required_evidence_kinds(criterion):
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} policy does not accept browser evidence")
            return 1
        record = lib.append_verification(
            root, cfg, kind="browser", ok=True, by=args.by.strip(), criteria=[criterion],
            results=[{"qa_report": args.report, "report_sha256": entry["report_sha256"],
                      "target": entry.get("target"), "author": entry.get("author")}],
            description=f"QA report {args.report} on {entry.get('target')} by {entry.get('author')}, "
                        f"mapped by {args.by.strip()}")
        retention, review_refresh = _attach_evidence(root, cfg, status, acceptance, records, criterion, record)
        entry["mapped"].append({"criterion": args.criterion, "by": args.by.strip(), "run_id": record["run_id"],
                                "at": record["at"]})
        lib.commit(root, cfg, status=status, acceptance=acceptance,
                   event_kind="qa_report_mapped", event_message="Mapped a QA report to criterion evidence",
                   extra_events=_retention_events(retention) or None, review_retention=retention,
                   report=args.report, run_id=record["run_id"], criterion=args.criterion, by=args.by.strip(),
                   review_attempt_refreshed=review_refresh)
    print(f"EVIDENCE_RECORDED: {record['run_id']}")
    return 0


def cmd_qa_report(args) -> int:
    return cmd_qa_report_add(args) if args.action == "add" else cmd_qa_report_map(args)


def cmd_mutation_proof(args) -> int:
    """#349: prove the criterion's own test FAILS when the behaviour is removed.

    Every other evidence path in this engine asks whether a command exited
    zero. None of them asks whether that command would still exit zero with
    the implementation gutted, and the answer is often yes: stubbing one
    refusal in `validate_status_schema` to `return []` passed all 1,727
    tests in this repository.

    So the engine applies the mutation itself. An author who reports having
    mutation-tested their work is making exactly the unverified claim this
    exists to catch, which is why `--by` names who asked and the surgery is
    done by this process, in a throwaway copy, against the criterion's own
    configured command.

    A failed proof records NOTHING. A ledger entry saying "the test does not
    detect this" would be a durable artifact that looks like evidence while
    meaning its opposite, and `criterion_fully_evidenced` would have to learn
    to distinguish them. Refusing at the door keeps the ledger a record of
    proofs rather than of attempts; the refusal goes to stdout, where the
    author reads it, and the criterion stays `not_tested`.
    """
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    # The lock is taken TWICE, with the expensive part outside it.
    #
    # A review found the whole proof running inside one `with project_lock`:
    # three full runs of the criterion's suite, each bounded by --timeout
    # (900s by default), so up to 45 minutes holding a blocking fcntl lock with
    # no timeout. `heartbeat` takes the same lock, so a slow suite did not just
    # make its own command slow, it silently blocked the liveness signal the
    # watchdog reads, which is the exact failure the watchdog exists to catch.
    #
    # `mutation.prove` writes no engine state (it is a layer above core that
    # reads a tree and runs a command), so nothing requires the lock to be held
    # while it runs. What the lock protected is re-established on reacquisition
    # instead: the criterion must still exist with the SAME spec hash, and the
    # source must still have the digest the proof ran against. Both are checked
    # below, so a tree or a claim that changed during the proof is refused
    # rather than recorded against the wrong thing.
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        required_kinds = lib.required_evidence_kinds(criterion)
        if "mutation" not in required_kinds:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} has verification "
                  f"{criterion.get('verification')!r}, which does not require mutation evidence. "
                  "Set it to a policy that does (automated_and_mutation) before proving it, so "
                  "the proof is part of what makes the criterion pass rather than a note beside it.")
            return 1
        # The criterion's OWN commands, never one passed on the command line.
        # A proof of some other command says nothing about this criterion, and
        # letting the author choose the command is letting the author choose a
        # command that fails for an unrelated reason.
        #
        # Joined with `&&`, which gives exactly the semantics the proof needs
        # over a criterion carrying several tests: the baseline half passes
        # only if EVERY test passes, and the mutated half fails if ANY test
        # notices. The claim is that the criterion's tests detect it, not that
        # one chosen test does.
        tests = [t for t in (criterion.get("tests") or []) if str(t).strip()]
        if not tests:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} names no test command to prove")
            return 1
        command = " && ".join(tests)
        # And the criterion's OWN target and symbol. There is deliberately no
        # --target/--symbol: a review reproduced an irrelevant symbol looking
        # proved, and showed that a proof recorded against one stayed valid
        # evidence for the criterion forever, because nothing bound the two.
        # Declared on the criterion, they are inside `criterion_spec_hash`, so
        # the design review sees the claim and changing it invalidates the proof.
        target = criterion.get("mutation_target")
        symbol = criterion.get("mutation_symbol")
        if not target or not symbol:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} declares no mutation target. "
                  f"Set it on the criterion, so the claim is reviewed and hash-bound rather than "
                  f"chosen now: criterion-update {args.criterion} --mutation-target FILE "
                  "--mutation-symbol FUNCTION")
            return 1
        spec_before = lib.criterion_spec_hash(criterion)

    # --- the lock is released here, for the duration of the proof only ---
    try:
        record = mutation.prove(root, command=command, target=target,
                                symbol=symbol, timeout=args.timeout,
                                total_timeout=args.total_timeout)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    if not record["ok"]:
        print(json.dumps({"ok": False, "recorded": False, **record}, indent=2))
        print(f"SHIP_FEATURE_BLOCKED: mutation proof failed, nothing recorded. "
              f"{record.get('refusal', '')}")
        return 1

    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} was removed while the "
                  "proof ran; nothing recorded")
            return 1
        if lib.criterion_spec_hash(criterion) != spec_before:
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} changed while the proof "
                  "ran, so the proof is evidence for a claim that no longer exists; nothing "
                  "recorded. Run it again.")
            return 1
        if mutation.source_digest(root, target) != record["source_digest"]:
            print("SHIP_FEATURE_BLOCKED: the source changed while the proof ran, so the proof "
                  "describes a tree that is no longer here; nothing recorded. Run it again.")
            return 1
        verification = lib.append_verification(
            root, cfg, kind="mutation", ok=True, by=args.by, criteria=[criterion],
            commands=list(tests),
            description=(f"neutralising {symbol} in {target} made "
                         f"{command!r} fail (exit {record['baseline_exit_code']} -> "
                         f"{record['mutated_exit_code']})"),
            results=[record])
        status["verification_head"] = verification["hash"]
        if verification["run_id"] not in criterion["evidence"]:
            criterion["evidence"].append(verification["run_id"])
        criterion["state"] = ("passing"
                             if lib.criterion_fully_evidenced(criterion, records + [verification])
                             else "not_tested")
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        review_refresh = lib.refresh_review_attempt_after_evidence(status, acceptance)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            lib.commit(root, cfg, status=status, acceptance=acceptance,
                       event_kind="mutation_proved",
                       event_message=f"{symbol} neutralised; the criterion's test failed",
                       run_id=verification["run_id"], criterion=args.criterion, by=args.by,
                       target=target, symbol=symbol,
                       review_attempt_refreshed=review_refresh)
        except lib.HandsoffError as exc:
            # The ledger append above is durable the moment it returns, and the
            # commit is what attaches it. A review pointed out that a commit
            # failure then reported SHIP_FEATURE_BLOCKED, which reads as "the
            # proof failed", while a valid record for an expensive proof sat
            # unconsumed. Say which actually happened and name the record, so
            # the next step is obvious instead of a rerun in the dark.
            print(f"SHIP_FEATURE_BLOCKED: the mutation proof SUCCEEDED and its record "
                  f"{verification['run_id']} is in the ledger, but attaching it failed: {exc}. "
                  "The record is valid and bound to this criterion's current spec, so `doctor` "
                  "or a rerun will pick it up; the proof itself does not need repeating.")
            return 1
    print(json.dumps({"ok": True, "recorded": True, "run_id": verification["run_id"],
                      "criterion_state": criterion["state"], **record}, indent=2))
    return 0


def cmd_record_symptom(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        record = next((r for r in records if r.get("run_id") == args.evidence and r.get("ok") is True), None)
        if not record:
            print("SHIP_FEATURE_BLOCKED: symptom evidence must reference a successful verification run")
            return 1
        primary = {c["id"]: c for c in acceptance["criteria"] if c.get("type") == "primary_fix"}
        current_primary = any(
            cid in record.get("criteria", [])
            and record.get("criterion_hashes", {}).get(cid) == lib.criterion_spec_hash(criterion)
            for cid, criterion in primary.items()
        )
        if not current_primary:
            print("SHIP_FEATURE_BLOCKED: symptom evidence must verify a primary_fix criterion")
            return 1
        status["requirement_coverage"]["original_symptom_resolved"] = True
        status["original_symptom_evidence_id"] = args.evidence
        _invalidate_decisions(status)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status,
                  event_kind="symptom_resolved", event_message="Original symptom marked resolved",
                  evidence=args.evidence, by=args.by)
    print("ORIGINAL_SYMPTOM_RESOLVED")
    return 0


def cmd_design_approve(args) -> int:
    """Record the Architect's core gate: an explicit human approval of the
    proposed design/criteria, distinct from the Architect identity that
    proposed them. This is what actually unblocks Phase 3+ for a flagged
    run (requires_design_approval); the Architect's own text is never
    self-sufficient, the same way a Reviewer's own text never advances
    the review gate without record-review."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.architect or not args.architect.strip():
        print("SHIP_FEATURE_BLOCKED: --architect must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    if args.by.strip().casefold() == args.architect.strip().casefold():
        print("SHIP_FEATURE_BLOCKED: approver must differ from the architect, no self-approval")
        return 1
    if args.redesigns_settled_work is not None and not args.redesigns_settled_work.strip():
        print("SHIP_FEATURE_BLOCKED: --redesigns-settled-work must be a non-empty string when passed")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        criteria = acceptance.get("criteria", [])
        if any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT and c.get("tests") == lib.PLACEHOLDER_TESTS
               for c in criteria):
            print("SHIP_FEATURE_BLOCKED: acceptance registry still contains init's untouched placeholder "
                  "criterion; author a real criterion before requesting design approval")
            return 1
        rows = lib.derive_work_items(status, acceptance, cfg)["items"]
        missing = [row["id"] for row in rows if row.get("required", True) and not row.get("criteria")]
        if missing:
            print("SHIP_FEATURE_BLOCKED: required work items have no criteria: "
                  f"{', '.join(missing)}; tag a criterion [#N] or work-item-remove them")
            return 1
        # Stamp authored_by = the architect on every criterion that does not
        # already carry one (absent key, or explicitly null/empty all count
        # as unstamped) -- an earlier approval's authorship is never
        # reassigned by a later one. Safe because criterion_spec_hash (and
        # therefore design_hash) excludes authored_by entirely: this can
        # never change a criterion's spec hash, invalidate already-recorded
        # evidence, or mismatch a freshly recomputed design_hash.
        for c in criteria:
            if c.get("authored_by") is None:
                c["authored_by"] = args.architect
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        status["design_approved"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": args.by,
            "architect": args.architect,
            "summary": args.summary,
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "redesigns_settled_work": args.redesigns_settled_work,
            "proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
            **lib.rules_binding(root, cfg),  # #170
        }
        # In the natural AR7 flow, record-design-review left the run
        # explicitly blocked on this human decision. Once both current
        # design decisions exist, reflect that the Supervisor may advance
        # instead of leaving Mission Control stuck on a stale approval ask.
        if status.get("phase_number") == 2 and status.get("requires_design_review") \
                and not lib._design_review_errors(status, acceptance, cfg):
            status["status"] = "in_progress"
            status["next_action"] = "Advance the independently reviewed and human-approved design to Phase 3."
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Approval is round_end's other trigger (the first being the next
        # round starting, see cmd_advance): whatever round was open when
        # approval landed just ended, closing the episode a design-timing
        # read-back needs. Only meaningful for a run that ever tracked an
        # organic round (design_round > 0); a run that went straight from
        # init to design-approve at round 0 has no round to close.
        current_round = int(status.get("design_round", 0) or 0)
        extra_events = None
        if current_round >= 1:
            extra_events = [{
                "kind": "design_round_ended",
                "message": f"Design round {current_round} ended (design approved)",
                "design_round": current_round,
                "trigger": "design_approved",
            }]
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra_events,
                  event_kind="design_approved", event_message=args.summary,
                  by=args.by, architect=args.architect,
                  redesigns_settled_work=args.redesigns_settled_work)
    print("DESIGN_APPROVAL_RECORDED")
    _print_unverifiable_test_warnings(acceptance, cfg)
    _warn_live_commands_empty(cfg)  # #406
    return 0


#: #125: what record-review leaves for the Supervisor.
REVIEW_APPROVED_NEXT_ACTION = "Supervisor advances to Phase 6: checks and documentation (advance 6)."

# #102: failure categories the engine itself classified as environmental,
# for which one automatic retry is offered before the Pilot is asked.
AUTO_RETRY_CATEGORIES = {"runtime_environment", "token_budget_exhaustion"}


def _adoption_error(status: dict, args) -> str | None:
    """#115: --adopted-session and --adopted-by come together and name a
    terminal managed session whose persisted result is being adopted; the
    record's --by must be that session's actor so the verdict is
    attributed to the reviewer, not to whoever pressed adopt."""
    session_id = getattr(args, "adopted_session", None)
    adopter = getattr(args, "adopted_by", None)
    if session_id is None and adopter is None:
        return None
    if not session_id or not adopter or not str(adopter).strip():
        return "--adopted-session and --adopted-by must be given together"
    session = (status.get("agent_sessions") or {}).get(session_id)
    if not isinstance(session, dict) or not isinstance(session.get("result"), dict):
        return "--adopted-session must name a managed session with a persisted result"
    if str(session.get("actor") or "").casefold() != args.by.strip().casefold():
        return "--by must match the adopted session's actor"
    return None


def _adoption_fields(args) -> dict:
    session_id = getattr(args, "adopted_session", None)
    if not session_id:
        return {}
    return {"adopted_by": str(args.adopted_by).strip(), "adopted_session": session_id}


def _reviewer_session_error(status: dict, session_id: str | None, reviewer: str) -> str | None:
    """Bind a host-recorded verdict to the exact current live Reviewer."""
    if session_id is None:
        return None
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    session = sessions.get(session_id)
    if not isinstance(session, dict) or session.get("role") != "reviewer" \
            or current.get("reviewer") != session_id \
            or session.get("state") not in lib.AGENT_SESSION_LIVE_STATES:
        return "--session must name the current live managed Reviewer session"
    if str(session.get("actor") or "").casefold() != reviewer.strip().casefold():
        return "--by must match the managed Reviewer session actor"
    return None


def _review_pending_decline(root, cfg, args) -> int | None:
    """#177: when the Architect's decline is pending, record-design-review
    judges the decline. Approve closes the run as not_planned (close_run
    takes its own lock, so the decision is made under a short lock and the
    closure happens outside it); request-changes sends it back with the
    findings. None when no decline is pending."""
    decision = "approved" if args.approve else "changes_requested"
    with lib.project_lock(root):
        try:
            status, acceptance, records, problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        pending = lib.pending_design_decline(status)
        if pending is None:
            return None
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: a decline is reviewed in Phase 2")
            return 1
        if args.by.strip().casefold() == str(pending.get("by") or "").casefold():
            print("SHIP_FEATURE_BLOCKED: the decline's reviewer must differ from the architect who declined")
            return 1
        criteria = acceptance.get("criteria", [])
        if pending.get("design_hash") != lib.design_hash(criteria):
            print("SHIP_FEATURE_BLOCKED: the criteria changed since the decline was recorded; decline again")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), args.by) \
            or _adoption_error(status, args)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
        try:
            findings = lib.validate_design_review_findings(args.finding, 1)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        now_iso = datetime.now(timezone.utc).isoformat()
        resolved = {**pending, "decision": decision, "reviewed_by": args.by.strip(), "reviewed_at": now_iso,
                    "findings": [f["text"] if isinstance(f, dict) else str(f) for f in findings]}
        if decision != "approved":
            status["design_declined"] = resolved
            status["status"] = "in_progress"
            status["next_action"] = "Architect answers the reviewer's findings with a new proposal or a new decline."
            lib.commit(root, cfg, status=status, event_kind="design_decline_changes_requested",
                       event_message=f"Independent reviewer sent the decline back: {args.summary.strip()}",
                       by=args.by.strip(), architect=pending.get("by"),
                       design_hash=pending.get("design_hash"), findings=len(findings),
                       reviewer_session_id=getattr(args, "session", None))
            print("DESIGN_DECLINE_CHANGES_REQUESTED")
            return 0
        expected_updated_at = status.get("updated_at")
    decline_event = {"kind": "design_decline_approved",
                     "message": f"Independent reviewer approved the decline: {args.summary.strip()}",
                     "by": args.by.strip(), "architect": pending.get("by"),
                     "design_hash": pending.get("design_hash"), "reason": pending.get("reason"),
                     "reviewer_session_id": getattr(args, "session", None)}
    try:
        lib.close_run(root, by=args.by.strip(), reason=pending["reason"], outcome="not_planned",
                      expected_updated_at=expected_updated_at,
                      status_patch={"design_declined": resolved}, extra_events=[decline_event])
    except lib.HandsoffError as e:
        print(f"SHIP_FEATURE_BLOCKED: {e}")
        return 1
    print("DESIGN_DECLINE_APPROVED: run closed as not_planned")
    return 0


def cmd_record_design_review(args) -> int:
    """Record an independent Phase-2 critique of the current design.

    The record is bound to design_hash, so any later criteria mutation
    makes it unusable even before the mutation command clears it. An
    approval opens the human-approval step; changes requested keep the run
    in Phase 2 and invalidate any human approval that arrived too early.
    """
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.architect or not args.architect.strip():
        print("SHIP_FEATURE_BLOCKED: --architect must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    if args.by.strip().casefold() == args.architect.strip().casefold():
        print("SHIP_FEATURE_BLOCKED: design reviewer must differ from the architect, no self-review")
        return 1
    structural_blocker = bool(getattr(args, "structural_blocker", False))
    if structural_blocker and not args.request_changes:
        print("SHIP_FEATURE_BLOCKED: --structural-blocker is only valid with --request-changes")
        return 1

    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    decline_outcome = _review_pending_decline(root, cfg, args)
    if decline_outcome is not None:
        return decline_outcome
    with lib.project_lock(root):
        try:
            status, acceptance, records, problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: independent design review can only be recorded in Phase 2")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), args.by) \
            or _adoption_error(status, args)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
        provenance = (status.get("design_proposal") or {}).get("provenance")
        if isinstance(provenance, dict):
            if args.by.strip().casefold() == str(provenance.get("actor") or "").casefold():
                print("SHIP_FEATURE_BLOCKED: design reviewer must differ from the proposal architect actor")
                return 1
            launch_id = getattr(args, "session", None) or getattr(args, "adopted_session", None)
            launch_session = (status.get("agent_sessions") or {}).get(launch_id)
            # #112: a managed reviewer (codex or claude, named by --session) is
            # a separate process, provider and actor even when the host
            # Supervisor launched it from the Architect's terminal. Only a
            # host-recorded verdict (no --session) shares a host session in
            # any meaningful sense: the recording process must not be the
            # Claude Code or Codex session that wrote the proposal.
            recording_host = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_COMPANION_SESSION_ID")
            if launch_session is None and provenance.get("host_session_id") \
                    and recording_host == provenance.get("host_session_id"):
                print("SHIP_FEATURE_BLOCKED: design reviewer must use an independent host session")
                return 1
        criteria = acceptance.get("criteria", [])
        if any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT and c.get("tests") == lib.PLACEHOLDER_TESTS
               for c in criteria):
            print("SHIP_FEATURE_BLOCKED: acceptance registry still contains init's untouched placeholder "
                  "criterion; author a real criterion before design review")
            return 1

        # #35: every record, approve or request-changes, is one attempt
        # against the autonomous budget; past it the Pilot authorizes one
        # attempt at a time. The refusal names the exact command.
        budget = lib.design_review_budget(status, cfg)
        if budget["exhausted"]:
            print(f"SHIP_FEATURE_BLOCKED: {lib.design_review_budget_exhausted_message(budget)}")
            return 1

        # #36: bounded findings carry ids F<attempt>.<n>; the record also
        # binds the commit it was made at so a later packet can tell
        # whether the repository moved underneath the prior review.
        try:
            findings = lib.validate_design_review_findings(args.finding, budget["next_attempt"])
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        try:
            head = lib.repository_snapshot(root)["head"]
        except lib.HandsoffError:
            head = None

        # #37: which reviewer tier this attempt was made under, selected by
        # the same pure precedence a managed launch uses (attempt number,
        # follow-up config, open escalation, last structural blocker,
        # criteria structure), from the state as it stands right now.
        tier, tier_reason = lib.select_design_reviewer_tier(cfg, status, acceptance)
        try:
            tier_profile = lib.design_reviewer_tier_profile(cfg, tier, require_available=False)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        reviewer_profile = {"adapter": tier_profile["adapter"], "model": tier_profile["model"],
                            "tier": tier, "reason": tier_reason}

        now = datetime.now(timezone.utc).isoformat()
        decision = "approved" if args.approve else "changes_requested"
        status["design_review"] = {
            "at": now,
            "by": args.by.strip(),
            "architect": args.architect.strip(),
            "decision": decision,
            "summary": args.summary.strip(),
            "design_hash": lib.design_hash(criteria),
            "config_hash": lib.config_hash(cfg),
            "attempt": budget["next_attempt"],
            "head": head,
            "findings": findings,
            "structural_blocker": structural_blocker,
            "reviewer_profile": reviewer_profile,
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "proposal_hash": (status.get("design_proposal") or {}).get("proposal_hash"),
            **_adoption_fields(args),
        }
        lib.append_design_review_history(
            status, lib.design_review_history_entry(status["design_review"], criteria,
                                                    structural_blocker=structural_blocker))
        status.pop("authorization_hold", None)
        status["updated_at"] = now
        status["design_review_attempts"] = budget["next_attempt"]
        # An unconsumed authorization is spent by this record whether a
        # managed session reserved it (launch_session_id set, kept as the
        # audit trail of which session performed the attempt) or a human
        # recorded the review directly (launch_session_id null).
        authorized = budget["authorized"]
        if authorized:
            # #417: one attempt spends one authorized round; a multi-round
            # grant stays open for the next attempt until its last round.
            lib.spend_design_review_authorization(status, now)
        # #37: a Pilot escalation buys exactly one primary-tier attempt; this
        # record is that attempt, so the escalation is spent here.
        escalation = status.get("design_reviewer_escalation")
        escalation_consumed = isinstance(escalation, dict) and escalation.get("consumed_at") is None
        if escalation_consumed:
            status["design_reviewer_escalation"]["consumed_at"] = now
        if decision == "approved":
            if not lib._design_errors(status, acceptance, cfg):
                status["status"] = "in_progress"
                status["next_action"] = ("Advance the independently reviewed design to Phase 3 (Pilot design approval waived by config)."
                                         if not status.get("requires_design_approval")
                                         else "Advance the independently reviewed and human-approved design to Phase 3.")
            else:
                status["status"] = "blocked"
                status["next_action"] = "Get explicit human approval of the independently reviewed design."
            event_kind = "design_review_approved"
            message = f"Independent design review approved: {args.summary.strip()}"
        else:
            status["design_approved"] = None
            status["status"] = "in_progress"
            status["next_action"] = "Architect revises the design, starts a new design round, and requests review again."
            event_kind = "design_review_changes_requested"
            message = f"Independent design review requested changes: {args.summary.strip()}"
        review_event = {
            "kind": event_kind, "message": message,
            "by": args.by.strip(), "architect": args.architect.strip(), "decision": decision,
            "design_hash": status["design_review"]["design_hash"],
            "design_review_attempt": budget["next_attempt"],
            "design_review_limit": budget["limit"],
            "authorized": authorized,
            "head": head,
            "findings": len(findings),
            "structural_blocker": structural_blocker,
            "reviewer_tier": tier,
            "reviewer_selection_reason": tier_reason,
            "reviewer_profile": {"adapter": reviewer_profile["adapter"], "model": reviewer_profile["model"]},
            "escalation_consumed": escalation_consumed,
        }
        after = lib.design_review_budget(status, cfg)
        auto = (lib.design_round_auto_authorization(status, cfg)
                if decision == "changes_requested" else None)
        if auto is not None:
            # #417: [workflow] design_rounds_on_convergence grants one more
            # round because this round recorded strictly fewer findings than
            # the one before it; the cumulative per-run count is ledgered.
            status["design_review_authorization"] = {
                "by": lib.DESIGN_ROUNDS_ON_CONVERGENCE_ACTOR, "at": now,
                "note": (f"findings fell from {auto['previous_findings']} to {auto['findings']}; "
                         f"automatic round {auto['auto_used']} of {auto['allowance']}"),
                "attempt_permitted": auto["round"], "launch_session_id": None, "consumed_at": None,
                "rounds_remaining": 1,
            }
            status["design_rounds_auto_used"] = auto["auto_used"]
            status["status"] = "in_progress"
            status.pop("authorization_hold", None)
            status["next_action"] = f"Launch the automatically authorized design-review attempt {auto['round']}"
            lib.commit(root, cfg, status=status, extra_events=[review_event],
                      event_kind="design_review_auto_authorized",
                      event_message=(f"Design-review attempt {auto['round']} authorized by policy: findings fell "
                                     f"from {auto['previous_findings']} to {auto['findings']} "
                                     f"({auto['auto_used']}/{auto['allowance']} automatic rounds used)"),
                      round=auto["round"], findings=auto["findings"],
                      previous_findings=auto["previous_findings"],
                      design_rounds_auto_used=auto["auto_used"],
                      design_rounds_on_convergence=auto["allowance"],
                      reviewer_session_id=getattr(args, "session", None))
        elif decision == "changes_requested" and after["exhausted"]:
            # The run cannot request another review on its own. Phase 3
            # stays closed (the gate still sees changes_requested); the
            # Pilot's command is the next action, verbatim, so status and
            # the Mission Control banner both carry it.
            status["status"] = "blocked"
            status["authorization_hold"] = "design_review"
            status["next_action"] = lib.design_review_budget_exhausted_message(after)
            lib.commit(root, cfg, status=status, extra_events=[review_event],
                      event_kind="design_review_budget_exhausted",
                      event_message=f"Design review budget exhausted ({after['attempts']}/{after['limit']}); "
                                    "Pilot authorization required for any further attempt",
                      design_review_attempts=after["attempts"], design_review_limit=after["limit"],
                      reviewer_session_id=getattr(args, "session", None))
        else:
            fields = {k: v for k, v in review_event.items() if k not in ("kind", "message")}
            lib.commit(root, cfg, status=status, event_kind=event_kind, event_message=message,
                       reviewer_session_id=getattr(args, "session", None), **fields)
    print("DESIGN_REVIEW_APPROVED" if args.approve else "DESIGN_CHANGES_REQUESTED")
    return 0


def cmd_design_propose(args) -> int:
    """Record a JSON proposal supplied by the host-driven Architect."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if cfg["agents"].get("architect") != lib.HOST_AGENT_ADAPTER:
        print('SHIP_FEATURE_BLOCKED: design-propose is only for a host Architect; set [agents].architect = "host"')
        return 1
    try:
        actor = lib.validate_agent_actor(args.by)
        value = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError, lib.HandsoffError) as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    status = lib.load_unique_json(lib.status_path(root, cfg))
    if int(status.get("phase_number", 1) or 1) not in {1, 2}:
        print("SHIP_FEATURE_BLOCKED: design proposal can only be recorded in Phase 1 or Phase 2")
        return 1
    try:
        recorded = lib.record_design_proposal(root, None, value, architect_actor=actor)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    print(f"DESIGN_PROPOSAL_RECORDED: {recorded['proposal_hash']}")
    _print_unverifiable_test_warnings(acceptance, cfg)
    return 0


def cmd_design_review_packet(args) -> int:
    """#36: generate the delta review packet for the next design-review
    attempt from the most recent recorded review. Refused before any review
    (the first review gets the full task) and outside Phase 2. Every
    disposition refusal names the offending value and writes nothing."""
    json_module = __import__("json")
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, records, problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: a delta review packet can only be generated in Phase 2")
            return 1
        budget = lib.design_review_budget(status, cfg)
        if budget["attempts"] == 0:
            print("SHIP_FEATURE_BLOCKED: no design review has been recorded yet; the first review "
                  "receives the full task, not a delta packet")
            return 1
        if not status.get("design_review_history"):
            print("SHIP_FEATURE_BLOCKED: no design review history is recorded; record-design-review "
                  "must run on this version before a delta packet can be built")
            return 1
        try:
            dispositions = lib.parse_design_review_dispositions(
                args.disposition, lib.latest_design_review_findings(status))
            packet = lib.build_design_review_packet(root, cfg, status, acceptance, dispositions)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        size = lib.design_review_packet_bytes(packet)
        status["design_review_packet"] = packet
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        delta = packet["criteria_delta"]
        lib.commit(
            root, cfg, status=status, event_kind="design_review_packet_generated",
            event_message=f"Delta review packet {packet['packet_id']} generated for design-review "
                          f"attempt {packet['attempt']}",
            by=args.by.strip(), packet_id=packet["packet_id"], attempt=packet["attempt"],
            previous_attempt=packet["previous_attempt"], design_hash=packet["design_hash"],
            previous_design_hash=packet["previous_design_hash"], head=packet["repository"]["head"],
            previous_head=packet["previous_head"], bytes=size, stale=packet["stale"],
            truncated=bool(packet.get("truncated")),
            counts={
                "findings": len(packet["findings"]),
                **{name: len(ids) for name, ids in packet["dispositions"].items()},
                "new_findings": len(packet["new_findings_since"]),
                "criteria_added": len(delta["added"]), "criteria_removed": len(delta["removed"]),
                "criteria_changed": len(delta["changed"]),
                "criteria_unchanged": (len(delta["unchanged"]) if "unchanged" in delta
                                       else delta.get("unchanged_count", 0)),
                "evidence": len(packet["evidence"]),
                "files_changed": (len(packet["files_changed_since_previous"])
                                  if packet["files_changed_since_previous"] is not None else None),
            },
        )
    print(f"DESIGN_REVIEW_PACKET_GENERATED: {packet['packet_id']} attempt {packet['attempt']} ({size} bytes)")
    print(json_module.dumps(packet, indent=2, sort_keys=True))
    return 0


def cmd_design_review_authorize(args) -> int:
    """#35: the Pilot permits one more design-review attempt past the
    autonomous budget (#417: or up to five with --rounds N, consumed one
    per recorded attempt). Human-only (the broker refuses it). Refuses
    while the budget is not exhausted (nothing to authorize) and while an
    earlier authorization is still unconsumed (one at a time). Never
    touches design_review_attempts: only a recorded review counts."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    # #417: one ledgered grant may cover up to MAX rounds; without --rounds
    # it covers exactly one, as before.
    rounds = getattr(args, "rounds", None)
    rounds = 1 if rounds is None else rounds
    if (not isinstance(rounds, int) or isinstance(rounds, bool)
            or not 1 <= rounds <= lib.MAX_DESIGN_REVIEW_AUTHORIZED_ROUNDS):
        print(f"SHIP_FEATURE_BLOCKED: --rounds must be an integer from 1 to "
              f"{lib.MAX_DESIGN_REVIEW_AUTHORIZED_ROUNDS}, got {rounds!r}")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if isinstance(status.get("run_closed"), dict):
            print("SHIP_FEATURE_BLOCKED: run is closed; reopen it before authorizing a design review")
            return 1
        budget = lib.design_review_budget(status, cfg)
        if budget["authorized"]:
            print("SHIP_FEATURE_BLOCKED: an unconsumed design-review authorization already exists "
                  f"(attempt {status['design_review_authorization']['attempt_permitted']}); "
                  "record-design-review must consume it before another can be granted")
            return 1
        if budget["attempts"] < budget["limit"]:
            print(f"SHIP_FEATURE_BLOCKED: design review budget is not exhausted "
                  f"({budget['attempts']}/{budget['limit']}); nothing to authorize")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        note = args.note.strip() if args.note and args.note.strip() else None
        status["design_review_authorization"] = {
            "by": args.by.strip(), "at": now, "note": note,
            "attempt_permitted": budget["next_attempt"],
            "launch_session_id": None, "consumed_at": None,
            "rounds_remaining": rounds,
        }
        last_attempt = budget["next_attempt"] + rounds - 1
        span = (f"attempt {budget['next_attempt']}" if rounds == 1
                else f"attempts {budget['next_attempt']} to {last_attempt}")
        status["status"] = "in_progress"
        status.pop("authorization_hold", None)
        status["next_action"] = f"Launch the authorized design-review {span}"
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="design_review_attempt_authorized",
                  event_message=note or f"Pilot authorized design-review {span}",
                  by=args.by.strip(), attempt_permitted=budget["next_attempt"], rounds=rounds,
                  design_review_attempts=budget["attempts"], design_review_limit=budget["limit"])
    suffix = "" if rounds == 1 else f" ({rounds} rounds, through attempt {last_attempt})"
    print(f"DESIGN_REVIEW_ATTEMPT_AUTHORIZED: {budget['next_attempt']}{suffix}")
    return 0


def cmd_design_review_escalate(args) -> int:
    """#37: the Pilot forces the NEXT design-review attempt onto the primary
    reviewer tier regardless of what the delta check would select. Human-only
    (the broker refuses it). Refused outside Phase 2 and while an earlier
    escalation is still unconsumed (one at a time); consumed by the next
    record-design-review. Never touches design_review_attempts."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number") != 2:
            print("SHIP_FEATURE_BLOCKED: a design reviewer escalation can only be recorded in Phase 2")
            return 1
        escalation = status.get("design_reviewer_escalation")
        if isinstance(escalation, dict) and escalation.get("consumed_at") is None:
            print("SHIP_FEATURE_BLOCKED: an unconsumed design reviewer escalation already exists "
                  f"(by {escalation['by']} at {escalation['at']}); record-design-review must consume "
                  "it before another can be recorded")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        note = args.note.strip() if args.note and args.note.strip() else None
        status["design_reviewer_escalation"] = {
            "by": args.by.strip(), "at": now, "note": note, "consumed_at": None,
        }
        status["updated_at"] = now
        budget = lib.design_review_budget(status, cfg)
        lib.commit(root, cfg, status=status, event_kind="design_reviewer_escalated",
                  event_message=note or f"Pilot escalated design-review attempt {budget['next_attempt']} "
                                        "to the primary reviewer tier",
                  by=args.by.strip(), tier="primary", reason="pilot_escalation",
                  design_review_attempt=budget["next_attempt"],
                  design_review_attempts=budget["attempts"])
    print(f"DESIGN_REVIEWER_ESCALATED: attempt {budget['next_attempt']} will use the primary reviewer tier")
    return 0


def cmd_review_attempt_start(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number", 0) < 4:
            print("SHIP_FEATURE_BLOCKED: review attempts require Phase 4 or later")
            return 1
        proposed = deepcopy(status)
        try:
            attempt = lib.open_review_attempt(
                proposed, acceptance, cfg, by=args.by, trigger=args.trigger,
                detail=args.note or "", reviewer=args.reviewer,
            )
        except lib.HandsoffError as exc:
            if isinstance(proposed.get("escalation"), dict) \
                    and proposed["escalation"].get("kind") == "review_cap_exhausted":
                lib.commit(root, cfg, status=proposed, event_kind="review_attempt_refused",
                           event_message=str(exc), by=args.by,
                           review_round=proposed.get("review_round"),
                           effective_max_review_rounds=lib.effective_review_cap(proposed, cfg))
            print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
            return 1
        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {x}" for x in errors))
            return 1
        lib.commit(root, cfg, status=proposed, event_kind="review_attempt_opened",
                   event_message=f"Implementation review attempt {attempt['attempt']} opened",
                   by=args.by, attempt_id=attempt["attempt_id"], attempt=attempt["attempt"],
                   trigger=attempt["trigger"], reviewer=attempt.get("reviewer"))
    print(f"REVIEW_ATTEMPT_OPENED: {attempt['attempt_id']} "
          f"(attempt {attempt['attempt']} of {lib.effective_review_cap(proposed, cfg)})")
    return 0


ADAPTIVE_RECORD_EVIDENCE_LIMIT = 7


def _adaptive_review_records(root: Path, cfg: dict, status: dict, acceptance: dict,
                             records: list[dict], *, attempt: dict, reviewer: str,
                             disposition: str, findings: list[dict] | None = None) -> list[dict] | None:
    """#387: the audit records a closed review attempt carries on a run with a
    risk_class. The deterministic check plan is every criterion's test
    command, validated (at most 32 checks) before anything is recorded; each
    check's outcome is the latest verification-ledger run of that command,
    or not_run for a manual criterion or a command never executed. The
    records ride on the review_attempt_closed event because
    status.adaptive_escalation is a closed schema. None without a risk_class;
    a plan over the bound raises HandsoffError naming it."""
    if not status.get("risk_class"):
        return None
    criteria = acceptance.get("criteria") or []
    acceptance_hash = lib.acceptance_hash(criteria)
    mission_id = _runtime_run_id(root, lib.read_events(root, cfg))
    plan = []
    for criterion in criteria:
        for index, command in enumerate(criterion.get("tests") or []):
            plan.append({"check_id": f"{criterion.get('id')}:{index + 1}", "command": command,
                         "applies_to": str(criterion.get("id")), "order": len(plan)})
    if len(plan) > 32:
        raise lib.HandsoffError(f"the deterministic check plan has {len(plan)} checks; "
                                "the bound is 32 checks per review attempt")
    checks = lib.validate_adaptive_check_plan(plan, mission_id=mission_id, acceptance_hash=acceptance_hash)
    manual = {str(c.get("id")) for c in criteria if c.get("verification") != "automated"}
    result, runs = [], []
    for check in checks:
        latest = None
        if check["applies_to"] not in manual:
            for record in reversed(records):
                item = next((r for r in record.get("results") or []
                             if isinstance(r, dict) and r.get("command") == check["command"]), None)
                if item is not None and isinstance(record.get("run_id"), str):
                    latest = (record, item)
                    break
        if latest is None:
            reason = "manual criterion" if check["applies_to"] in manual else "never executed"
            result.append(lib.record_adaptive_check(check, outcome="not_run", detail=reason))
            continue
        record, item = latest
        code = item.get("exit_code")
        outcome = "error" if item.get("timed_out") or not isinstance(code, int) \
            else ("pass" if code == 0 else "fail")
        result.append(lib.record_adaptive_check(check, outcome=outcome, evidence=[record["run_id"]],
                                                detail=f"exit {code}, run {record['run_id']}"))
        if record["run_id"] not in runs:
            runs.append(record["run_id"])
    if disposition == "approved":
        reviewer_claim = {"decision": "accept", "summary": "Review approved the current acceptance"}
    else:
        summary = "; ".join(f"{f['code']}: {f['summary']}" for f in findings or [])
        reviewer_claim = {"decision": "repair",
                          "summary": (summary[:509] + "...") if len(summary) > 512 else summary}
    question = next(iter(lib.open_questions(status)), None)
    result += lib.adaptive_escalation_records(
        mission_id=mission_id, acceptance_hash=acceptance_hash,
        implementer={"claim_id": f"{attempt['attempt_id']}:implementer",
                     "actor": str(status.get("implemented_by") or "unrecorded")[:128],
                     "decision": "accept",
                     "summary": f"Implementation submitted for review attempt {attempt['attempt']}"},
        reviewer={"claim_id": f"{attempt['attempt_id']}:reviewer", "actor": reviewer, **reviewer_claim},
        supporting_evidence=[{"evidence_id": run_id, "kind": "verification_run",
                              "detail": f"verification ledger run {run_id}"}
                             for run_id in runs[-ADAPTIVE_RECORD_EVIDENCE_LIMIT:]],
        unresolved_question=None if question is None else {
            "question_id": question["question_id"],
            "question": str(question.get("text") or "")[:512].strip() or "(empty question)"},
    )
    return result


def _parse_review_findings(values: list[str]) -> list[dict]:
    if not values or len(values) > 16:
        raise lib.HandsoffError("record-review-findings requires 1 to 16 findings")
    findings = []
    for raw in values:
        code, sep, summary = raw.partition(":")
        code, summary = code.strip(), summary.strip()
        if not sep or code not in lib.REVIEW_FINDING_CODES or not summary or len(summary) > 512:
            raise lib.HandsoffError("findings must use 'CODE: summary' with a supported code")
        findings.append({"code": code, "summary": summary})
    if len({(item["code"], item["summary"]) for item in findings}) != len(findings):
        raise lib.HandsoffError("duplicate review findings are not allowed")
    return findings


def cmd_record_review_findings(args) -> int:
    reviewer = lib.validate_agent_actor(args.by)
    findings = _parse_review_findings(args.finding)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if status.get("phase_number", 0) < 4:
            print("SHIP_FEATURE_BLOCKED: review findings require Phase 4 or later")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), reviewer) \
            or _adoption_error(status, args)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
        implementer = status.get("implemented_by")
        if implementer and reviewer.casefold() == implementer.strip().casefold():
            print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
            return 1
        proposed = deepcopy(status)
        attempt = lib.current_review_attempt(proposed)
        if attempt is None:
            try:
                attempt = lib.open_review_attempt(proposed, acceptance, cfg, by=reviewer, reviewer=reviewer)
            except lib.HandsoffError as exc:
                print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
                return 1
        current_acceptance_hash = lib.acceptance_hash(acceptance.get("criteria", []))
        acceptance_changed = attempt.get("acceptance_hash") != current_acceptance_hash
        attempt["reviewer"] = reviewer
        attempt["findings"] = findings
        attempt["tests_executed"] = args.tests_executed
        attempt["disposition"] = "changes_requested"
        attempt.update(_adoption_fields(args))
        attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        if attempt["attempt"] >= lib.effective_review_cap(proposed, cfg):
            lib._review_cap_escalation(proposed, cfg)
            extra = [{"kind": "review_cap_escalated", "message": proposed["escalation"]["reason"],
                      "attempt_id": attempt["attempt_id"], "attempt": attempt["attempt"]}]
        else:
            proposed["phase_number"] = 4
            proposed["phase"] = lib.PHASES[4]
            proposed["progress"] = min(float(proposed.get("progress", 0)), 40)
            proposed["status"] = "in_progress"
            proposed["next_action"] = (
                f"Implementer addresses {len(findings)} findings from attempt {attempt['attempt']}; "
                f"Supervisor then starts attempt {attempt['attempt'] + 1} of {lib.effective_review_cap(proposed, cfg)}"
            )
            extra = None
        proposed["review"] = None
        proposed["reviewed_by"] = None
        if proposed.get("risk_class"):
            repairs = sum(1 for item in proposed.get("review_attempts", [])
                          if item.get("disposition") == "changes_requested")
            proposed["adaptive_escalation"] = lib.bound_adaptive_escalation(
                disagreement_rounds=repairs, repair_rounds=repairs,
            )
        try:
            adaptive = _adaptive_review_records(root, cfg, proposed, acceptance, records, attempt=attempt,
                                                reviewer=reviewer, disposition="changes_requested",
                                                findings=findings)
        except lib.HandsoffError as exc:
            print(f"SHIP_FEATURE_BLOCKED: {exc}")
            return 1
        errors = lib.compute_errors(proposed, acceptance, cfg, verifications=records,
                                    verification_problems=problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {error}" for error in errors))
            return 1
        lib.commit(root, cfg, status=proposed, extra_events=extra,
                   event_kind="review_attempt_closed",
                   event_message=f"Review attempt {attempt['attempt']} requested changes",
                   by=reviewer, attempt_id=attempt["attempt_id"], attempt=attempt["attempt"],
                   disposition="changes_requested", findings=findings,
                   reviewer_session_id=getattr(args, "session", None),
                   reviewed_acceptance_hash=attempt.get("acceptance_hash"),
                   current_acceptance_hash=current_acceptance_hash,
                   acceptance_changed=acceptance_changed,
                   **({} if adaptive is None else {"adaptive_records": adaptive}))
    print("REVIEW_CHANGES_RECORDED")
    return 0


def cmd_review_cap_override(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be non-empty")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        proposed = deepcopy(status)
        lib.migrate_review_ledger(proposed)
        overrides = proposed["review_cap_overrides"]
        if len(overrides) >= lib.MAX_REVIEW_CAP_OVERRIDES:
            print("SHIP_FEATURE_BLOCKED: review cap override history is full")
            return 1
        oid = lib._new_bounded_id(
            "ho", lib.REVIEW_OVERRIDE_ID_PATTERN,
            {item.get("override_id") for item in overrides}, None,
        )
        overrides.append({
            "override_id": oid, "by": actor, "at": datetime.now(timezone.utc).isoformat(),
            "reason": args.reason.strip(), "config_hash": lib.config_hash(cfg),
            "review_round_at_grant": int(proposed.get("review_round", 0) or 0),
        })
        if isinstance(proposed.get("escalation"), dict) \
                and proposed["escalation"].get("kind") == "review_cap_exhausted":
            proposed["escalation"] = None
            proposed["status"] = "in_progress"
        proposed["next_action"] = (
            f"Start review attempt {int(proposed.get('review_round', 0)) + 1} "
            f"of {lib.effective_review_cap(proposed, cfg)}"
        )
        lib.commit(root, cfg, status=proposed, event_kind="review_cap_override_recorded",
                   event_message="Human granted one additional review attempt",
                   by=actor, reason=args.reason.strip(), override_id=oid,
                   effective_max_review_rounds=lib.effective_review_cap(proposed, cfg))
    print(f"REVIEW_CAP_OVERRIDE_RECORDED: {oid}")
    return 0


def _record_item_review_advisory(root, cfg, status, acceptance, record, item_id, reviewer_id) -> int:
    """#360: a per-item review of a full-lane item before Phase 5 is advice
    only. It appends one event and changes nothing else: no review_round, no
    review_attempts, no item approval, no lane confirmation, so compute_errors
    still requires the Phase 5 independent review."""
    if not lib.item_criteria(acceptance, item_id):
        print(f"SHIP_FEATURE_BLOCKED: {item_id} has no criteria to review")
        return 1
    implementer = (record.get("implemented_by") or status.get("implemented_by") or "").strip()
    if implementer and reviewer_id.casefold() == implementer.casefold():
        print(f"SHIP_FEATURE_BLOCKED: reviewer {reviewer_id} must differ from implementer {implementer}")
        return 1
    item_hash = lib.item_acceptance_hash(acceptance, item_id)
    lib.commit(root, cfg, event_kind="work_item_review_advisory",
               event_message=f"Advisory review of {item_id} before Phase 5",
               by=reviewer_id, reviewer=reviewer_id, item_id=item_id, acceptance_hash=item_hash)
    print(f"ADVISORY_ITEM_REVIEW_RECORDED: {item_id}")
    return 0


def cmd_record_review(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    reviewer_id = args.by.strip()
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        refusal = lib.lane_gate_refusal(status, "record-review")
        if refusal:
            print(f"SHIP_FEATURE_BLOCKED: {refusal}")
            return 1
        if _refuse_if_amendment_open(status, action="independent review"):
            return 1
        if getattr(args, "item", None):
            delivery = status.get("work_item_delivery") or {}
            record = delivery.get(args.item)
            if isinstance(record, dict) and record.get("lane") in ("full", "escalated") \
                    and status.get("phase_number", 0) < 5:
                return _record_item_review_advisory(root, cfg, status, acceptance, record,
                                                    args.item, reviewer_id)
            if not isinstance(record, dict) or record.get("lane") != "small-fix" \
                    or not record.get("confirmed_by"):
                print(f"SHIP_FEATURE_BLOCKED: {args.item} is not a confirmed small-fix lane")
                return 1
            facts = lib.small_fix_facts(root, acceptance, args.item, cfg)
            if not facts["eligible"]:
                record.update(lane="escalated", facts=facts,
                              escalation_reason="; ".join(facts["reasons"]),
                              reviewed_by=None, review_hash=None)
                status.update(phase_number=2, phase=lib.PHASES[2], status="blocked",
                              updated_at=datetime.now(timezone.utc).isoformat(),
                              next_action=f"Design the escalated work item {args.item} in the full lane.")
                lib.commit(root, cfg, status=status, event_kind="work_item_lane_escalated",
                           event_message=f"Small-fix lane escalated for {args.item}",
                           by=reviewer_id, work_item=args.item, reasons=facts["reasons"])
                print("SMALL_FIX_ESCALATED\n" + "\n".join(f"- {reason}" for reason in facts["reasons"]))
                return 1
            own = lib.item_criteria(acceptance, args.item)
            if not own or any(c.get("state") != "passing" or not c.get("evidence") for c in own):
                print(f"SHIP_FEATURE_BLOCKED: {args.item} criteria require passing evidence")
                return 1
            implementer = record.get("implemented_by")
            if not implementer:
                print(f"SHIP_FEATURE_BLOCKED: {args.item} has no recorded implementer")
                return 1
            if reviewer_id.casefold() == implementer.strip().casefold():
                print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
                return 1
            item_hash = lib.item_acceptance_hash(acceptance, args.item)
            if record.get("reviewed_by") and record.get("review_hash") == item_hash:
                print(f"INDEPENDENT_ITEM_REVIEW_ALREADY_RECORDED: {args.item}")
                return 0
            now = datetime.now(timezone.utc).isoformat()
            record.update(reviewed_by=reviewer_id, review_hash=item_hash, facts=facts)
            status["updated_at"] = now
            lib.commit(root, cfg, status=status, event_kind="work_item_review_approved",
                       event_message=f"Independent review approved {args.item}",
                       by=reviewer_id, implementer=implementer, work_item=args.item,
                       acceptance_hash=item_hash)
            print(f"INDEPENDENT_ITEM_REVIEW_RECORDED: {args.item}")
            return 0
        if status.get("phase_number", 0) < 5:
            print("SHIP_FEATURE_BLOCKED: independent review can only be recorded in Phase 5 or later")
            return 1
        session_error = _reviewer_session_error(status, getattr(args, "session", None), reviewer_id) \
            or _adoption_error(status, args)
        if session_error:
            print(f"SHIP_FEATURE_BLOCKED: {session_error}")
            return 1
        implementer = status.get("implemented_by")
        if implementer and reviewer_id.casefold() == implementer.strip().casefold():
            print("SHIP_FEATURE_BLOCKED: reviewer must differ from implementer")
            return 1
        lib.migrate_review_ledger(status)
        if getattr(args, "reaffirm", False):
            return _record_review_reaffirm(root, cfg, status, acceptance, records, problems, reviewer_id, args)
        tests_errors = lib.review_tests_executed_errors(args.tests_executed, acceptance)  # #341
        if tests_errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in tests_errors))
            return 1
        attempt = lib.current_review_attempt(status)
        if attempt is None:
            try:
                attempt = lib.open_review_attempt(
                    status, acceptance, cfg, by=reviewer_id, reviewer=reviewer_id,
                )
            except lib.HandsoffError as exc:
                if isinstance(status.get("escalation"), dict) \
                        and status["escalation"].get("kind") == "review_cap_exhausted":
                    lib.commit(root, cfg, status=status, event_kind="review_attempt_refused",
                               event_message=str(exc), by=reviewer_id)
                print(f"REVIEW_ATTEMPT_REFUSED: {exc}")
                return 1
        if attempt.get("acceptance_hash") != lib.acceptance_hash(acceptance.get("criteria", [])):
            print("SHIP_FEATURE_BLOCKED: acceptance changed since this review attempt opened")
            return 1
        preflight = dict(status)
        preflight["phase_number"] = 6
        preflight["phase"] = lib.PHASES[6]
        implementer_profile = lib.audited_agent_profile(cfg, "implementer")
        reviewer_profile = lib.audited_agent_profile(cfg, "reviewer")
        profiles_distinct = (
            implementer_profile["effective_adapter"], implementer_profile["model"]
        ) != (
            reviewer_profile["effective_adapter"], reviewer_profile["model"]
        )
        preflight["review"] = {
            "by": reviewer_id, "at": datetime.now(timezone.utc).isoformat(),
            "acceptance_hash": lib.acceptance_hash(acceptance["criteria"]),
            "config_hash": lib.config_hash(cfg),
            "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
            "implementer_profile": implementer_profile,
            "reviewer_profile": reviewer_profile,
            "tests_executed": args.tests_executed,
            **lib.review_tests_executed_waiver(args.tests_executed),  # #341
            "profiles_distinct": profiles_distinct,
            **lib.rules_binding(root, cfg),  # #170
            "reviewed_binding": _reviewed_binding(root, cfg, status, acceptance),  # #411
            "checklist": {"symptom_reproduced": args.symptom_reproduced,
                          "symptom_resolved": "yes", "all_criteria_verified": "yes",
                          "evidence_attached": "yes"},
            **_adoption_fields(args),
        }
        preflight["reviewed_by"] = reviewer_id
        errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                    verification_problems=problems, root=root)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        try:
            adaptive = _adaptive_review_records(root, cfg, status, acceptance, records, attempt=attempt,
                                                reviewer=reviewer_id, disposition="approved")
        except lib.HandsoffError as exc:
            print(f"SHIP_FEATURE_BLOCKED: {exc}")
            return 1
        status["review"] = preflight["review"]
        status["reviewed_by"] = reviewer_id
        status["reviewer_checklist"] = preflight["review"]["checklist"]
        if status.get("risk_class"):
            repairs = sum(1 for item in status.get("review_attempts", [])
                          if item.get("disposition") == "changes_requested")
            status["adaptive_escalation"] = lib.bound_adaptive_escalation(
                disagreement_rounds=repairs, repair_rounds=repairs, human_decision="accepted",
            )
        attempt["reviewer"] = reviewer_id
        attempt["disposition"] = "approved"
        attempt["closed_at"] = datetime.now(timezone.utc).isoformat()
        attempt["tests_executed"] = args.tests_executed
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        # #125: the review is done; the next move is the Supervisor's, and
        # a managed Supervisor reads this text as its task.
        status["next_action"] = REVIEW_APPROVED_NEXT_ACTION
        lib.commit(root, cfg, status=status, extra_events=[{
                      "kind": "review_attempt_closed",
                      "message": f"Review attempt {attempt['attempt']} approved",
                      "attempt_id": attempt["attempt_id"], "attempt": attempt["attempt"],
                      "disposition": "approved", "reviewer": reviewer_id,
                      **({} if adaptive is None else {"adaptive_records": adaptive}),
                  }],
                  event_kind="review_approved", event_message="Independent review approved current acceptance",
                  by=reviewer_id, acceptance_hash=status["review"]["acceptance_hash"],
                  reviewer_session_id=getattr(args, "session", None),
                  implementer_profile=implementer_profile, reviewer_profile=reviewer_profile,
                  profiles_distinct=profiles_distinct)
    print("INDEPENDENT_REVIEW_RECORDED")
    return 0


def _record_review_reaffirm(root, cfg, status, acceptance, records, problems, reviewer_id, args) -> int:
    """Field-note defect 3 (the safe half): an evidence-only refresh (verify or
    record-evidence on unchanged criterion specs) revokes the review; the same
    reviewer re-binds the latest approved attempt to the new acceptance hash
    without opening an attempt or spending budget. Every review gate still
    runs, and a changed design hash (a real spec change) is refused."""
    # #179 (Lane A): a current review whose rules binding went stale (the
    # engine:version entry after a release install, the reviewer prompt or
    # a rule file) is reaffirmed too: the same reviewer re-binds it to the
    # current set and the ledger names what changed. A current review whose
    # set is unchanged has nothing to reaffirm.
    stale_rules = lib.rules_set_diff(root, (status.get("review") or {}).get("rules_entries")) \
        if isinstance(status.get("review"), dict) and lib.rules_binding_errors(root, cfg, status.get("review"), "review gate") else []
    if status.get("review") is not None and not stale_rules:
        print("SHIP_FEATURE_BLOCKED: review is current; nothing to reaffirm")
        return 1
    if lib.current_review_attempt(status) is not None:
        print("SHIP_FEATURE_BLOCKED: a review attempt is open; close it before reaffirming")
        return 1
    closed = [a for a in status.get("review_attempts") or [] if isinstance(a, dict) and a.get("closed_at")]
    latest = closed[-1] if closed else None
    if latest is None or latest.get("disposition") != "approved":
        print("SHIP_FEATURE_BLOCKED: no approved review attempt to reaffirm")
        return 1
    if str(latest.get("reviewer") or "").casefold() != reviewer_id.casefold():
        print(f"SHIP_FEATURE_BLOCKED: reaffirm must come from the reviewer of attempt {latest.get('attempt')} ({latest.get('reviewer')})")
        return 1
    current_design = lib.design_hash(acceptance.get("criteria", []))
    bound_design = latest.get("design_hash")
    if not bound_design:
        print("SHIP_FEATURE_BLOCKED: the approved attempt predates design binding; a fresh review is needed")
        return 1
    if bound_design != current_design:
        print("SHIP_FEATURE_BLOCKED: design hash changed since the approved attempt; a real change needs a fresh review")
        return 1
    previous_hash = latest.get("acceptance_hash")
    tests_executed = latest.get("tests_executed", args.tests_executed)
    tests_errors = lib.review_tests_executed_errors(tests_executed, acceptance)  # #341
    if tests_errors:
        print("SHIP_FEATURE_BLOCKED")
        print("\n".join(f"- {x}" for x in tests_errors))
        return 1
    preflight = dict(status)
    preflight["phase_number"] = 6
    preflight["phase"] = lib.PHASES[6]
    implementer_profile = lib.audited_agent_profile(cfg, "implementer")
    reviewer_profile = lib.audited_agent_profile(cfg, "reviewer")
    profiles_distinct = (
        implementer_profile["effective_adapter"], implementer_profile["model"]
    ) != (reviewer_profile["effective_adapter"], reviewer_profile["model"])
    now = datetime.now(timezone.utc).isoformat()
    preflight["review"] = {
        "by": reviewer_id, "at": now,
        "acceptance_hash": lib.acceptance_hash(acceptance["criteria"]),
        "config_hash": lib.config_hash(cfg),
        "scope_hash": lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])),
        "implementer_profile": implementer_profile,
        "reviewer_profile": reviewer_profile,
        "tests_executed": tests_executed,
        **lib.review_tests_executed_waiver(tests_executed),  # #341
        "profiles_distinct": profiles_distinct,
        "reaffirmed_attempt": latest.get("attempt"),
        "reaffirmed_from_acceptance_hash": previous_hash,
        **lib.rules_binding(root, cfg),  # #170: the reaffirmed review certifies the current set
        "reviewed_binding": _reviewed_binding(root, cfg, status, acceptance),  # #411
        "checklist": {"symptom_reproduced": args.symptom_reproduced,
                      "symptom_resolved": "yes", "all_criteria_verified": "yes",
                      "evidence_attached": "yes"},
        **_adoption_fields(args),
    }
    preflight["reviewed_by"] = reviewer_id
    errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                verification_problems=problems, root=root)
    if errors:
        print("SHIP_FEATURE_BLOCKED")
        print("\n".join(f"- {x}" for x in errors))
        return 1
    status["review"] = preflight["review"]
    status["reviewed_by"] = reviewer_id
    status["reviewer_checklist"] = preflight["review"]["checklist"]
    latest["acceptance_hash"] = preflight["review"]["acceptance_hash"]
    latest["design_hash"] = current_design
    latest["reaffirmed_at"] = now
    status["updated_at"] = now
    status["next_action"] = REVIEW_APPROVED_NEXT_ACTION
    reason = (f"after the rules set changed ({', '.join(stale_rules[:8])})" if stale_rules
              else "after an evidence-only refresh")
    lib.commit(root, cfg, status=status, event_kind="review_reaffirmed",
               event_message=f"Review attempt {latest.get('attempt')} reaffirmed {reason}",
               by=reviewer_id, attempt=latest.get("attempt"), attempt_id=latest.get("attempt_id"),
               acceptance_hash=preflight["review"]["acceptance_hash"], previous_acceptance_hash=previous_hash,
               design_hash=current_design, reviewer_session_id=getattr(args, "session", None),
               rules_changed=stale_rules)
    print(f"INDEPENDENT_REVIEW_REAFFIRMED{': rules set changed (' + ', '.join(stale_rules[:8]) + ')' if stale_rules else ''}")
    return 0


def _kept_workspace_refusal(status: dict, session_id: str) -> tuple[dict | None, str | None]:
    """#413: the kept workspace a disposition may act on, or why not: the
    session must be an implementer's, terminal, not quarantined, and kept
    (workspace_disposition pending)."""
    session = (status.get("agent_sessions") or {}).get(session_id)
    if not isinstance(session, dict) or session.get("role") != "implementer" \
            or not isinstance(session.get("workspace"), dict):
        return None, f"{session_id} is not an implementer session with a workspace"
    if session.get("state") not in lib.AGENT_SESSION_TERMINAL_STATES:
        return None, f"{session_id} is still live; stop it first"
    quarantined = session.get("quarantined_result")
    if isinstance(quarantined, dict) and not quarantined.get("adopted_at"):
        return None, f"{session_id} holds a quarantined result; session-result-adopt decides it"
    if session.get("workspace_disposition") != "pending":
        return None, (f"{session_id} has no kept workspace awaiting a disposition "
                      f"({session.get('workspace_disposition') or 'none'})")
    return session, None


def _kept_workspaces(cfg: dict, status: dict) -> list[dict]:
    """#413: every stopped implementer's kept workspace with its changed paths."""
    kept = []
    for session in (status.get("agent_sessions") or {}).values():
        if not isinstance(session, dict) or session.get("workspace_disposition") != "pending":
            continue
        try:
            changed = lib.implementer_workspace_changes(cfg, session)
        except (lib.HandsoffError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            changed = None
        kept.append({"session_id": session.get("session_id"), "state": session.get("state"),
                     "owned_paths": session.get("owned_paths"), "changed_paths": changed,
                     "apply": f"implementer-apply --session {session.get('session_id')} --by ACTOR",
                     "discard": f"implementer-discard --session {session.get('session_id')} --by ACTOR"})
    return kept


def cmd_implementer_apply(args) -> int:
    """#413: apply a stopped implementer's owned-path changes, with every
    check a normal apply makes (ownership, host edits, type transitions)."""
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        _session, refusal = _kept_workspace_refusal(status, args.session)
    if refusal:
        print(f"SHIP_FEATURE_BLOCKED: {refusal}")
        return 1
    # apply_implementer_workspace takes the project lock itself
    try:
        applied = lib.apply_implementer_workspace(root, args.session)
    except (lib.HandsoffError, OSError, subprocess.SubprocessError) as exc:
        applied = {"state": "refused", "reason": "apply_failed", "paths": [], "detail": str(exc)[:120]}
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        session, refusal = _kept_workspace_refusal(status, args.session)
        if refusal:
            print(f"SHIP_FEATURE_BLOCKED: {refusal}")
            return 1
        if applied["state"] != "applied":
            print(f"IMPLEMENTER_APPLY_REFUSED: {applied['reason']}: "
                  + (", ".join(applied.get("paths") or []) or applied.get("detail", "")))
            lib.commit(root, cfg, event_kind="implementer_workspace_apply_refused",
                       event_message="Kept implementer workspace apply refused; it stays pending",
                       session_id=args.session, by=actor, reason=applied["reason"],
                       paths=applied.get("paths") or [])
            return 1
        session["apply"] = {"state": "applied", "paths": applied["paths"]}
        session["workspace_disposition"] = "applied"
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="implementer_workspace_applied",
                   event_message="Stopped implementer's owned-path changes applied",
                   session_id=args.session, by=actor, paths=applied["paths"])
    lib.remove_implementer_workspace(root, session)
    print(f"IMPLEMENTER_APPLIED: {', '.join(applied['paths']) or 'no changes'}")
    return 0


def cmd_implementer_discard(args) -> int:
    """#413: remove a stopped implementer's kept workspace; nothing is applied."""
    actor = lib.validate_agent_actor(args.by)
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, _ = _load(root, cfg)
        session, refusal = _kept_workspace_refusal(status, args.session)
        if refusal:
            print(f"SHIP_FEATURE_BLOCKED: {refusal}")
            return 1
        session["workspace_disposition"] = "discarded"
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        lib.commit(root, cfg, status=status, event_kind="implementer_workspace_discarded",
                   event_message="Stopped implementer's kept workspace discarded",
                   session_id=args.session, by=actor)
    lib.remove_implementer_workspace(root, session)
    print(f"IMPLEMENTER_DISCARDED: {args.session}")
    return 0


def cmd_session_result_adopt(args) -> int:
    root = lib.resolve_root(args.root)
    actor = lib.validate_agent_actor(args.by)
    adopted, message = adopt_session_result(root, args.session, actor)
    print(message)
    return 0 if adopted else 1


def adopt_session_result(root, session_id: str, actor: str, *,
                         automatic: bool = False) -> tuple[bool, str]:
    """Replay a persisted protocol result through the canonical record
    command (#92). Three rules keep it honest: the request is not bound to
    the dead session (the broker would refuse a non-live session), the
    replay runs outside the project lock (it is a supervisor subprocess
    that takes the lock itself), and the adoption mark is written on a
    status re-read after the replay, so the replayed decision is not
    overwritten by a stale copy. #345: the launcher calls this with
    automatic=True for a failed reviewer's one valid verdict, so the CLI
    and the launcher share every refusal by sharing this path. #383: a
    quarantined late result is adopted once, only after its launch episode
    was resumed: a verdict through this same replay, a workspace through the
    normal workspace application."""
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        held = session.get("quarantined_result") if isinstance(session, dict) else None
        refusal = _quarantine_adoption_refusal(root, session_id, held) if isinstance(held, dict) else None
    if refusal:
        return False, f"SESSION_RESULT_ADOPT_REFUSED: {refusal}"
    if isinstance(held, dict) and held["kind"] == "implementer_workspace":
        return _adopt_quarantined_workspace(root, cfg, session_id, actor)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        session = (status.get("agent_sessions") or {}).get(session_id)
        result = session.get("result") if isinstance(session, dict) else None
        if not isinstance(result, dict):
            return False, "SESSION_RESULT_ADOPT_REFUSED: no persisted result"
        kind, payload = result["kind"], deepcopy(result["payload"])
        if isinstance(result.get("refused_text"), str):
            # #167: adopt from the packet as the reviewer wrote it, repairing
            # only the field the rule names; the copy made at refusal time
            # is never the source.
            import handsoff_broker as broker
            try:
                payload = broker.parse_reviewer_result(result["refused_text"], root=root)
            except lib.PacketRuleViolation as exc:
                if not isinstance(exc.recovered, dict):
                    return False, f"SESSION_RESULT_ADOPT_REFUSED: the preserved packet cannot be repaired: {exc}"
                payload = exc.recovered
            except lib.HandsoffError as exc:
                return False, f"SESSION_RESULT_ADOPT_REFUSED: the preserved packet is invalid: {exc}"
        readopt = False
        approval = kind == "review" and payload.get("kind") != "design" and payload.get("decision") == "approved" \
            and not (isinstance(session, dict) and session.get("amendment_id"))
        if approval:
            # #405 #406 fix round 1: the verdict judged the rules in force
            # when its reviewer session ran; a later policy edit needs a fresh
            # review, whatever the recorded review says (verify clears it).
            refusal = _session_rules_refusal(root, session)
            if refusal:
                return False, refusal
        if approval and isinstance(status.get("review"), dict):
            # #405 #406: a review is already recorded (the run sits past
            # Phase 5), so this approval reaffirms it; the re-bind decides.
            rebind = True
        elif result.get("adopted_at") is not None:
            # Field-note defect 3: an evidence-only refresh revoked the review
            # this verdict already backs; replay it as a reaffirmation.
            if kind == "review" and payload.get("decision") == "approved" and status.get("review") is None:
                readopt = True
                rebind = False
            else:
                return False, "SESSION_RESULT_ADOPT_REFUSED: result is already adopted"
        else:
            rebind = False
        if rebind:
            return _rebind_review(root, cfg, status, acceptance, records, problems, session_id, actor,
                                  automatic=automatic)
        architect = (status.get("design_proposal") or {}).get("architect")
        session_actor = str(session.get("actor") or "").strip()
    import handsoff_broker as broker
    # #115: the verdict belongs to the reviewer session that produced it;
    # the adopter is recorded alongside, never in its place.
    base = {"actor": "supervisor", "project_root": str(root), "action": "workflow",
            "by": session_actor or actor, "adopted_session": session_id, "adopted_by": actor}
    if isinstance(session, dict) and session.get("amendment_id"):
        # #146: a verdict persisted by a reviewer launched for an amendment
        # adopts as an amendment review, findings included, whatever kind
        # the reviewer wrote.
        amendment_result = {**payload, "kind": payload.get("kind", kind)}
        request = broker._amendment_review_request(root, cfg, base, status, session, amendment_result)
    elif kind == "review" and payload.get("kind") != "design":
        if payload.get("decision") == "approved":
            request = {**base, "command": "record-review",
                       "symptom_reproduced": payload.get("symptom_reproduced", "not_applicable"),
                       "tests_executed": payload.get("tests_executed", "unknown")}
            if readopt:
                request["reaffirm"] = True
        else:
            request = {**base, "command": "record-review-findings", "findings": payload.get("findings") or [],
                       "tests_executed": payload.get("tests_executed", "unknown")}
    elif kind == "design" or payload.get("kind") == "design":
        # The runner persists every reviewer result as kind "review"; the
        # packet's own kind says whether it judged a design (#167).
        if not architect:
            return False, "SESSION_RESULT_ADOPT_REFUSED: no architect recorded on the design proposal"
        request = {**base, "command": "record-design-review", "architect": architect,
                   "decision": "approve" if payload.get("decision") == "approved" else "request-changes",
                   "summary": payload.get("summary") or "adopted"}
        if payload.get("findings"):
            request["findings"] = payload["findings"]
        if payload.get("structural_blocker"):
            request["structural_blocker"] = True
    else:
        request = dict(payload)
        request.update(base)
    try:
        broker.execute_request(root, request, capability=broker._SUPERVISOR_HOST_CAPABILITY)
    except lib.HandsoffError as exc:
        return False, f"SESSION_RESULT_ADOPT_REFUSED: {exc}"
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        adopted = status["agent_sessions"][session_id]["result"]
        if readopt:
            adopted.setdefault("readoptions", []).append({"at": datetime.now(timezone.utc).isoformat(), "by": actor})
        else:
            adopted["adopted_at"] = datetime.now(timezone.utc).isoformat()
            adopted["adopted_by"] = actor
            adopted["adopted_automatically"] = automatic
            held = status["agent_sessions"][session_id].get("quarantined_result")
            if isinstance(held, dict):
                held["adopted_at"], held["adopted_by"] = adopted["adopted_at"], actor
        failure = (status.get("agent_failures") or {}).get(session_id)
        if isinstance(failure, dict):
            # The replacement pause is derived from this record's category;
            # marking it adopted is what lifts the pause (recovery skips it).
            failure["adopted"] = True
        lib.commit(root, cfg, status=status, event_kind="session_result_adopted",
                   event_message="Persisted session result adopted", session_id=session_id, by=actor,
                   automatic=automatic)
        lib.mark_beacon_adopted(root, session_id)  # #172
    return True, "SESSION_RESULT_ADOPTED"


def _session_rules_refusal(root: Path, session: dict) -> str | None:
    """#405 #406: why a stored approval may not be adopted under the current
    rules set, or None. Compared with the entries recorded when the reviewer
    session launched; only policy entries count (rules_set_diff). A session
    with none recorded fails closed: what it judged cannot be known."""
    entries = session.get("rules_entries")
    if not isinstance(entries, dict):
        return ("SESSION_RESULT_ADOPT_REFUSED: the reviewer session recorded no rules set at launch, "
                "so the rules its verdict judged are unknown; a fresh review is required")
    policy = lib.rules_set_diff(root, entries)
    if not policy:
        return None
    shown = ", ".join(policy[:8]) + (f" (+{len(policy) - 8} more)" if len(policy) > 8 else "")
    return (f"SESSION_RESULT_ADOPT_REFUSED: the rules set changed in policy entries ({shown}) "
            "since the reviewer session ran; a fresh review is required")


def _rebind_review(root: Path, cfg: dict, status: dict, acceptance: dict, records, problems,
                   session_id: str, actor: str, *, automatic: bool) -> tuple[bool, str]:
    """#405 #406: an approval delivered while a review is already recorded
    reaffirms that review, re-binding it to the current rules set. Allowed
    only when no policy entry changed (rules_set_diff: the engine version
    and the RULES_MECHANICS settings never count), the acceptance is the
    one reviewed, and every gate still passes on the current tree; any
    policy change refuses, naming the entries, and needs a fresh review.
    An already adopted result whose review is bound to the current set has
    nothing to re-bind, so a second adoption changes nothing. The caller
    holds the project lock."""
    review = status["review"]
    result = status["agent_sessions"][session_id]["result"]
    first = result.get("adopted_at") is None
    current_hash = lib.rules_set_hash(root, cfg)
    if not first and review.get("rules_hash") == current_hash:
        return False, "SESSION_RESULT_ADOPT_REFUSED: result is already adopted"
    policy = lib.rules_set_diff(root, review.get("rules_entries")) \
        if review.get("rules_hash") and review.get("rules_hash") != current_hash else []
    if policy:
        shown = ", ".join(policy[:8]) + (f" (+{len(policy) - 8} more)" if len(policy) > 8 else "")
        return False, (f"SESSION_RESULT_ADOPT_REFUSED: the rules set changed in policy entries ({shown}) "
                       "since the review; a fresh review is required")
    if review.get("acceptance_hash") != lib.acceptance_hash(acceptance.get("criteria", [])):
        return False, ("SESSION_RESULT_ADOPT_REFUSED: the acceptance criteria changed since the review; "
                       "a fresh review is required")
    changed = lib.rules_set_drift(root, review.get("rules_entries")) \
        if review.get("rules_hash") != current_hash else []
    now = datetime.now(timezone.utc).isoformat()
    preflight = dict(status)
    preflight["review"] = {**review, **lib.rules_binding(root, cfg),
                           "rules_rebound": {"at": now, "by": actor, "session_id": session_id,
                                             "from_rules_hash": review.get("rules_hash"), "changed": changed}}
    errors = lib.compute_errors(preflight, acceptance, cfg, verifications=records,
                                verification_problems=problems, root=root)
    if errors:
        return False, ("SESSION_RESULT_ADOPT_REFUSED: the review cannot be reaffirmed on the current tree: "
                       + "; ".join(errors[:3]))
    status["review"] = preflight["review"]
    status["updated_at"] = now
    if first:
        result["adopted_at"], result["adopted_by"], result["adopted_automatically"] = now, actor, automatic
    else:
        result.setdefault("readoptions", []).append({"at": now, "by": actor})
    failure = (status.get("agent_failures") or {}).get(session_id)
    if isinstance(failure, dict):
        failure["adopted"] = True
    reason = f"only engine or run-mechanics entries changed ({', '.join(changed)})" if changed else \
        "the rules set was re-recorded" if review.get("rules_hash") != current_hash else \
        "a further approval of the recorded review"
    lib.commit(root, cfg, status=status, extra_events=[{
                   "kind": "session_result_adopted", "message": "Persisted session result adopted as a reaffirmation",
                   "session_id": session_id, "by": actor, "automatic": automatic}],
               event_kind="review_reaffirmed",
               event_message=f"Review re-bound to the current rules set after {reason}",
               by=actor, session_id=session_id, rules_changed=changed,
               previous_rules_hash=review.get("rules_hash"), rules_hash=current_hash)
    lib.mark_beacon_adopted(root, session_id)  # #172
    return True, "SESSION_RESULT_ADOPTED: review reaffirmed and re-bound to the current rules set"


def _quarantine_adoption_refusal(root: Path, session_id: str, held: dict) -> str | None:
    """#383: why a quarantined result may not be adopted yet, or None."""
    if held.get("adopted_at") is not None:
        return "the quarantined result is already adopted"
    try:
        history = _runtime_read(root, PERFORMANCE_RECORD, "handsoff.performance_history")
        if history is None:
            return "no performance record holds the quarantining episode"
        disposition = runtime_control.late_result_disposition(history, held["episode_id"], session_id)
    except (runtime_control.RuntimeControlError, lib.HandsoffError, OSError) as exc:
        return f"the quarantining episode cannot be read: {exc}"
    if disposition == "quarantine":
        return (f"its launch episode {held['episode_id']} is still paused_for_performance_review; "
                "record performance-resume first")
    return None


def _adopt_quarantined_workspace(root: Path, cfg: dict, session_id: str, actor: str) -> tuple[bool, str]:
    """#383: apply a quarantined implementer workspace through the normal
    #359 application, ownership and host-edit checks included, then mark it
    adopted. The apply takes the project lock itself, so it runs outside it."""
    try:
        applied = lib.apply_implementer_workspace(root, session_id)
    except (lib.HandsoffError, OSError, subprocess.SubprocessError) as exc:
        return False, f"SESSION_RESULT_ADOPT_REFUSED: workspace apply failed: {str(exc)[:160]}"
    if applied["state"] != "applied":
        return False, (f"SESSION_RESULT_ADOPT_REFUSED: workspace apply refused ({applied['reason']}): "
                       f"{', '.join(applied['paths'])[:200]}")
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        session = status["agent_sessions"][session_id]
        held = session["quarantined_result"]
        if held.get("adopted_at") is not None:
            return False, "SESSION_RESULT_ADOPT_REFUSED: the quarantined result is already adopted"
        session["apply"] = {"state": "applied", "paths": applied["paths"]}
        held["adopted_at"], held["adopted_by"] = datetime.now(timezone.utc).isoformat(), actor
        # #415: the completed, now applied, bound session credits its items
        acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
        credited = lib.credit_session_work_items(status, acceptance, session_id, held["adopted_at"])
        lib.commit(root, cfg, status=status, acceptance=acceptance if credited else None,
                   event_kind="session_result_adopted",
                   event_message="Quarantined implementer workspace adopted", session_id=session_id, by=actor,
                   automatic=False, **({"work_items_implemented": credited} if credited else {}))
    lib.remove_implementer_workspace(root, session)
    return True, "SESSION_RESULT_ADOPTED"


def cmd_verify_live(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    commands = cfg.get("live_check_commands", [])
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        refusal = lib.lane_gate_refusal(status, "verify-live")
        if refusal:
            print(f"SHIP_FEATURE_BLOCKED: {refusal}")
            return 1
        if not commands:
            print("SHIP_FEATURE_NO_LIVE_CHECKS_CONFIGURED: set [checks].live_commands in handsoff.toml")
            return 1
        if _refuse_if_amendment_open(status, action="live verification"):
            return 1
        approval_required = lib.adaptive_deployment_approval_required(status, cfg)
        if status.get("phase_number") != 7 or (approval_required and not status.get("deployment_approved")):
            print("SHIP_FEATURE_BLOCKED: live verification requires Phase 7"
                  + (" and deployment approval" if approval_required else ""))
            return 1
        digest = lib.acceptance_hash(acceptance["criteria"])
        config_digest = lib.config_hash(cfg)
        if status.get("deployment_approved") and status["deployment_approved"].get("acceptance_hash") != digest:
            print("SHIP_FEATURE_BLOCKED: acceptance changed since deployment approval")
            return 1
        if status.get("deployment_approved") and status["deployment_approved"].get("config_hash") != config_digest:
            print("SHIP_FEATURE_BLOCKED: workflow policy changed since deployment approval")
            return 1
    # #148: a transient in-flight record so Mission Control can show the
    # live verification while it runs; it is display only, never evidence,
    # and is removed on every exit from this point on.
    inflight_path = root / lib.LIVE_INFLIGHT_FILE
    started_at = datetime.now(timezone.utc).isoformat()
    progress = {"started_at": started_at, "by": args.by, "total": len(commands), "done": 0,
                "current": None, "results": []}

    def on_progress(index, total, command, result):
        if result is None:
            progress["current"] = command
        else:
            progress["current"] = None
            progress["done"] = index
            progress["results"].append({"command": command, "exit_code": result["exit_code"]})
        lib.atomic_write_json(inflight_path, progress)

    try:
        lib.atomic_write_json(inflight_path, progress)
        ran_env = lib.recorded_check_env(cfg)  # #410
        results = lib.run_checks(cfg, root, commands, on_progress=on_progress,
                                 progress_source="verify-live")
        ok = all(r["exit_code"] == 0 for r in results)
        with lib.project_lock(root):
            # Re-read handsoff.toml from disk here, not the `cfg` captured
            # before run_checks: comparing config_hash(cfg) to itself can
            # never detect a change, since it is the same in-memory object
            # both times. Only a fresh load can see an edit that landed
            # while the (possibly slow) live checks were executing.
            cfg = lib.load_config(root)
            status, acceptance, records, verification_problems = _load_all(root, cfg)
            audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
            if audit_errors:
                return _print_audit_block(audit_errors)
            if lib.acceptance_hash(acceptance["criteria"]) != digest:
                print("SHIP_FEATURE_BLOCKED: acceptance changed during live verification; run it again")
                return 1
            if lib.config_hash(cfg) != config_digest:
                print("SHIP_FEATURE_BLOCKED: workflow policy changed during live verification; run it again")
                return 1
            if lib.recorded_check_env(cfg) != ran_env:
                print("SHIP_FEATURE_BLOCKED: [checks].env changed during live verification; run it again")
                return 1
            record = lib.append_verification(root, cfg, kind="live", ok=ok, by=args.by,
                                             criteria=acceptance["criteria"], results=_durable_results(results),
                                             commands=commands,
                                             acceptance_digest=digest, config_digest=config_digest,
                                             # #411: taken when the commands completed, so a
                                             # later re-verify of the same tree keeps the run
                                             repository_digest=lib.repository_digest(root, cfg),
                                             rules=lib.rules_binding(root, cfg), env=ran_env)
            status["verification_head"] = record["hash"]
            if ok:
                status["live_verification_id"] = record["run_id"]
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.commit(root, cfg, status=status,
                      event_kind="live_checks_run", event_message="Ran configured live checks",
                      ok=ok, run_id=record["run_id"], by=args.by)
    finally:
        try:
            inflight_path.unlink()
        except FileNotFoundError:
            pass
    print(__import__("json").dumps({"ok": ok, "run_id": record["run_id"], "results": results}, indent=2))
    return 0 if ok else 1


def _sync_registry_from_criteria(acceptance: dict, cfg: dict) -> bool:
    """Append newly declared criterion identities without deleting stable promises."""
    return lib.sync_work_item_registry(acceptance, cfg)


def _criterion_needs_item_warning(criterion: dict, acceptance: dict, cfg: dict) -> bool:
    items, _ = lib.effective_work_items(acceptance, cfg)
    return len(items) > 1 and lib.criterion_work_item_id(criterion) is None


def _print_unverifiable_test_warnings(acceptance: dict, cfg: dict) -> None:
    """Warn when design gates name automated tests verify cannot run."""
    configured = set(cfg.get("check_commands", []))
    for criterion in acceptance.get("criteria", []):
        if "checks" not in lib.required_evidence_kinds(criterion):
            continue
        for test in criterion.get("tests", []):
            if test not in configured:
                print(f"WARNING: criterion {criterion.get('id')} test {test!r} matches no [checks].commands entry; verify will refuse it")


def cmd_criterion_update(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        criterion = _criterion(acceptance, args.criterion)
        if criterion is None:
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if all(getattr(args, field, None) is None for field in ("requirement", "verification", "type", "test", "state", "baseline", "repeat", "seed_env", "mutation_target", "mutation_symbol", "outcome", "evidence_class", "path")) \
                and not getattr(args, "no_evidence_classes", False) and not getattr(args, "no_paths", False):
            print("SHIP_FEATURE_BLOCKED: criterion-update requires at least one change")
            return 1
        # P1.1: outcome, evidence_classes and paths; an empty outcome or a
        # --no-* flag clears the field (stored as absent, never null)
        scope_fields: dict = {}
        if getattr(args, "outcome", None) is not None:
            scope_fields["outcome"] = args.outcome or None
        if getattr(args, "no_evidence_classes", False) and getattr(args, "evidence_class", None):
            print("SHIP_FEATURE_BLOCKED: pass either --evidence-class or --no-evidence-classes, not both")
            return 1
        if getattr(args, "no_paths", False) and getattr(args, "path", None):
            print("SHIP_FEATURE_BLOCKED: pass either --path or --no-paths, not both")
            return 1
        if getattr(args, "evidence_class", None) is not None or getattr(args, "no_evidence_classes", False):
            scope_fields["evidence_classes"] = getattr(args, "evidence_class", None) or None
        if getattr(args, "path", None) is not None or getattr(args, "no_paths", False):
            scope_fields["paths"] = getattr(args, "path", None) or None
        fields = {field: getattr(args, field) for field in ("requirement", "verification", "type", "state")
                  if getattr(args, field) is not None}
        if args.test is not None:
            fields["tests"] = args.test
        if args.baseline is not None:
            # #165: "none" clears the declaration; not_applicable needs its reason.
            fields["baseline"] = None if args.baseline == "none" else args.baseline
            if args.baseline_reason is not None:
                fields["baseline_reason"] = args.baseline_reason
        if args.repeat is not None:
            fields["repeat"] = None if args.repeat == 1 else args.repeat
        if args.seed_env is not None:
            fields["seed_env"] = args.seed_env or None
            fields.setdefault("repeat", criterion.get("repeat"))
        if args.mutation_target is not None or args.mutation_symbol is not None:
            # Both are read, not only the one given, so the pair is validated as
            # it will exist on the criterion rather than as the edit alone.
            fields["mutation_target"] = (args.mutation_target if args.mutation_target is not None
                                         else criterion.get("mutation_target")) or None
            fields["mutation_symbol"] = (args.mutation_symbol if args.mutation_symbol is not None
                                         else criterion.get("mutation_symbol")) or None
        # The policy's requirement is about the criterion's resulting shape, so
        # it is checked against the merge, not against the fields being changed:
        # switching an existing criterion TO automated_and_mutation without a
        # declared symbol would otherwise be accepted and only fail at proof time.
        fields.update(scope_fields)
        merged = {**{k: v for k, v in criterion.items() if k not in ("state", "evidence")}, **fields}
        problems = list(lib.validate_criterion_fields(fields))
        problems += [problem for problem in lib.validate_criterion_fields(
                         merged, check_commands=list(cfg.get("check_commands", [])))
                     if ("automated_and_mutation" in problem or "evidence class checks" in problem)
                     and problem not in problems]
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
        was_primary = criterion.get("type") == "primary_fix"
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
        spec_changed = False
        for field in ("requirement", "verification", "type"):
            value = getattr(args, field)
            if value is not None and criterion.get(field) != value:
                criterion[field] = value
                spec_changed = True
        if args.test is not None:
            criterion["tests"] = args.test
            spec_changed = True
        if args.repeat is not None or args.seed_env is not None:
            # #169: part of the claim, so the spec hash changes
            if args.repeat is not None:
                if args.repeat == 1:
                    criterion.pop("repeat", None)
                    criterion.pop("seed_env", None)
                else:
                    criterion["repeat"] = args.repeat
            if args.seed_env is not None and criterion.get("repeat"):
                if args.seed_env:
                    criterion["seed_env"] = args.seed_env
                else:
                    criterion.pop("seed_env", None)
            spec_changed = True
        if args.baseline is not None:
            # part of the claim: a baseline declaration changes the spec hash
            if args.baseline == "none":
                criterion.pop("baseline", None)
                criterion.pop("baseline_reason", None)
            else:
                criterion["baseline"] = args.baseline
                criterion["baseline_reason"] = args.baseline_reason
            spec_changed = True
        if args.mutation_target is not None or args.mutation_symbol is not None:
            # #349: part of the claim, so the spec hash changes and any proof
            # recorded against the previous symbol stops counting. That is the
            # point: a proof is evidence for the symbol it neutralised, and a
            # criterion that now names a different one has not been proved.
            for field, value in (("mutation_target", args.mutation_target),
                                 ("mutation_symbol", args.mutation_symbol)):
                if value is None:
                    continue
                if value:
                    criterion[field] = value
                else:
                    criterion.pop(field, None)
            spec_changed = True
        for field, value in scope_fields.items():
            # P1.1: part of the claim, so the spec hash changes and earlier
            # evidence stops satisfying the criterion
            if value:
                criterion[field] = list(value) if isinstance(value, list) else value
            else:
                criterion.pop(field, None)
            spec_changed = True
        if args.state is not None:
            criterion["state"] = args.state
        if spec_changed or args.state is not None:
            criterion["evidence"] = []
            if args.state is None or args.state == "passing":
                criterion["state"] = "not_tested"
        if spec_changed and (was_primary or criterion.get("type") == "primary_fix"):
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        registry_changed = _sync_registry_from_criteria(acceptance, cfg)
        warning = _criterion_needs_item_warning(criterion, acceptance, cfg)
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_updated", event_message="Acceptance criterion updated",
                  criterion=args.criterion, decisions_revoked=decisions_revoked, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])))
    if warning:
        print("WORK_ITEM_WARNING: untagged criterion in a multi-item run")
    print("CRITERION_UPDATED")
    return 0


def cmd_criterion_add(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        if _criterion(acceptance, args.criterion):
            print(f"SHIP_FEATURE_BLOCKED: criterion {args.criterion} already exists")
            return 1
        fields = {
            "id": args.criterion, "type": args.type, "requirement": args.requirement,
            "verification": args.verification, "tests": args.test or [],
        }
        if args.baseline is not None:
            fields["baseline"] = args.baseline
            fields["baseline_reason"] = args.baseline_reason
        if args.repeat is not None and args.repeat != 1:
            fields["repeat"] = args.repeat
        if args.seed_env:
            fields["seed_env"] = args.seed_env
        if args.mutation_target or args.mutation_symbol:
            fields["mutation_target"] = args.mutation_target
            fields["mutation_symbol"] = args.mutation_symbol
        # P1.1: only when given, so a legacy criterion's spec hash is unchanged
        for field, value in (("outcome", args.outcome), ("evidence_classes", args.evidence_class),
                             ("paths", args.path)):
            if value is not None:
                fields[field] = value
        problems = lib.validate_criterion_fields(fields, require_all=True,
                                                 check_commands=list(cfg.get("check_commands", [])))
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
        before_scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
        acceptance["criteria"].append({
            **{k: v for k, v in fields.items() if k != "id"}, "id": args.criterion,
            "evidence": [], "state": "not_tested",
        })
        if args.type == "primary_fix":
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        criterion = acceptance["criteria"][-1]
        registry_changed = _sync_registry_from_criteria(acceptance, cfg)
        warning = _criterion_needs_item_warning(criterion, acceptance, cfg)
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_added", event_message="Acceptance criterion added",
                  criterion=args.criterion, decisions_revoked=decisions_revoked, work_item_scope_changed=registry_changed or
                  before_scope != lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", [])))
    if warning:
        print("WORK_ITEM_WARNING: untagged criterion in a multi-item run")
    print("CRITERION_ADDED")
    return 0


def cmd_criterion_remove(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        if _refuse_if_amendment_open(status):
            return 1
        problems = lib.validate_criterion_fields({"id": args.criterion})
        if problems:
            print("SHIP_FEATURE_BLOCKED: " + "; ".join(problems))
            return 1
        if not _criterion(acceptance, args.criterion):
            print(f"SHIP_FEATURE_BLOCKED: unknown criterion {args.criterion}")
            return 1
        if len(acceptance["criteria"]) == 1:
            print("SHIP_FEATURE_BLOCKED: acceptance registry must retain at least one criterion")
            return 1
        acceptance["criteria"] = [c for c in acceptance["criteria"] if c["id"] != args.criterion]
        errors = lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criterion_removed", event_message="Acceptance criterion removed",
                  criterion=args.criterion)
    print("CRITERION_REMOVED")
    return 0


def cmd_criteria_apply(args) -> int:
    """#44: apply a whole list of add/update/remove operations as one
    lock-protected commit, or refuse the whole list with nothing written.
    The planner (lib.plan_criteria_transaction) does every check on a deep
    copy; only a complete plan reaches the single commit() below, which
    carries status, acceptance, and one criteria_transaction_applied event.
    Decisions are invalidated exactly once, the same way one
    criterion-update does it (design cleared, flagged Phase 3+ run back to
    Phase 2 in the same status write, no phase_advanced event)."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        if _refuse_if_amendment_open(status):
            return 1
        if _approval_edit_guard(status, args.revoke_approval):
            return 1
        plan = lib.plan_criteria_transaction(acceptance, cfg, operations, root=root)
        if args.dry_run:
            print(__import__("json").dumps(lib.criteria_plan_preview(plan, status), indent=2, sort_keys=True))
            return 0
        lib.apply_criteria_plan(acceptance, plan)
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        untagged = [record["id"] for record in plan["operations"] if record["op"] != "remove"
                    and _criterion_needs_item_warning(_criterion(acceptance, record["id"]), acceptance, cfg)]
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        decisions_revoked = _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="criteria_transaction_applied",
                  event_message=f"Acceptance criteria transaction applied ({plan['operation_count']} operations)",
                  by=args.by, operations=plan["operations"],
                  registry_hash_before=plan["registry_hash_before"],
                  registry_hash_after=plan["registry_hash_after"],
                  design_hash_after=plan["design_hash_after"],
                  operation_count=plan["operation_count"],
                  decisions_revoked=decisions_revoked,
                  work_item_scope_changed=plan["work_item_scope_changed"])
    if untagged:
        print(f"WORK_ITEM_WARNING: untagged criteria in a multi-item run: {', '.join(untagged)}")
    print(f"CRITERIA_TRANSACTION_APPLIED: {plan['operation_count']} operations, "
          f"registry {plan['registry_hash_after'][:12]}")
    return 0


def _refuse_if_amendment_open(status: dict, *, action: str | None = None) -> int | None:
    """#42: the freeze. Criterion mutations (no `action`) and the decision
    commands (`action` named) are refused while an amendment is open;
    verify and record-evidence are deliberately not routed through here."""
    message = lib.amendment_decision_refusal(status, action) if action else lib.amendment_mutation_refusal(status)
    if message is None:
        return None
    print(f"SHIP_FEATURE_BLOCKED: {message}")
    return 1


def _amendment_event_fields(record: dict) -> dict:
    """What every amendment event carries: ids, hashes, classification,
    never criterion text or test commands."""
    return {
        "amendment_id": record["amendment_id"], "amendment_hash": record["amendment_hash"],
        "base_design_hash": record["base_design_hash"], "resulting_design_hash": record["resulting_design_hash"],
        "changed_ids": list(record["changed_ids"]), "dependent_ids": list(record["dependent_ids"]),
        "affected_work_items": list(record["affected_work_items"]),
        "classification": record["classification"], "classification_reasons": list(record["classification_reasons"]),
        "operation_count": len(record["operations"]),
    }


def _print_full_redesign_refusal(record: dict, *, revise: bool) -> int:
    print("AMENDMENT_REFUSED: full_redesign")
    print("\n".join(f"- {reason}" for reason in record["classification_reasons"]))
    if revise:
        print("This follow-up widens the open amendment beyond a scoped correction. Run "
              f"`amendment-escalate --by ACTOR --reason TEXT` on {record['amendment_id']}, which takes the full "
              "path (design decisions cleared, Phase 2), then apply the change with criteria-apply.")
    else:
        print("A scoped amendment cannot carry this change. Use the ordinary path instead: "
              "`criteria-apply --file TX.json --by ACTOR` (or criterion-update), which invalidates the design "
              "approval and returns the run to Phase 2 for a full redesign.")
    return 1


def _close_amendment(status: dict, record: dict, *, state: str, now: str) -> None:
    record["state"] = state
    record["closed_at"] = now
    history = [item for item in (status.get("amendments") or []) if isinstance(item, dict)]
    history.append(record)
    status["amendments"] = history[-lib.MAX_AMENDMENT_HISTORY:]
    status["amendment"] = None


def cmd_amendment_open(args) -> int:
    """#42: open a scoped post-approval amendment. The #44 planner builds
    the delta, `lib.classify_amendment` decides scoped or full_redesign
    (the caller cannot choose), and only a scoped delta is applied: the
    changed criteria reset to not_tested with evidence cleared, review/
    deployment/live decisions invalidated (design decisions kept on their
    base hash), the phase frozen, one commit, one `amendment_opened`."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        planned = lib.plan_amendment(status, acceptance, cfg, operations, by=args.by, root=root,
                                     request_full_redesign=bool(args.request_full_redesign))
        record, plan = planned["record"], planned["plan"]
        if record["classification"] != "scoped":
            return _print_full_redesign_refusal(record, revise=False)
        lib.apply_criteria_plan(acceptance, plan)
        lib.reset_amended_criteria(acceptance, record["changed_ids"])
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        record["frozen_phase"] = int(status.get("phase_number", 0) or 0)
        record["frozen_progress"] = status.get("progress", 0)
        status["amendment"] = record
        status.setdefault("amendments", [])
        status["next_action"] = (f"Amendment {record['amendment_id']} is open: record amendment-review, then "
                                 "the Pilot records amendment-approve (or escalate it).")
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_status_schema(status) + lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        summary = args.summary.strip() if args.summary and args.summary.strip() else None
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="amendment_opened",
                  event_message=summary or f"Scoped amendment {record['amendment_id']} opened "
                                           f"({len(record['changed_ids'])} criteria changed)",
                  by=record["by"], frozen_phase=record["frozen_phase"], frozen_progress=record["frozen_progress"],
                  registry_hash_before=plan["registry_hash_before"], registry_hash_after=plan["registry_hash_after"],
                  **_amendment_event_fields(record))
    print(f"AMENDMENT_OPENED: {record['amendment_id']} scoped, {len(record['changed_ids'])} criteria changed, "
          f"frozen at Phase {record['frozen_phase']}")
    return 0


def cmd_amendment_revise(args) -> int:
    """#42: a follow-up transaction on the open amendment after the reviewer
    requested changes (or before any review). Same classification over the
    cumulative delta; an expansion to full_redesign refuses and points at
    amendment-escalate. Recomputes the amendment hash and clears the stale
    review in the same commit."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    operations = lib.load_criteria_transaction(Path(args.file))
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        if lib.open_amendment(status) is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open; use amendment-open")
            return 1
        planned = lib.plan_amendment_revision(status, acceptance, cfg, operations, root=root)
        record, plan = planned["record"], planned["plan"]
        if record["classification"] != "scoped":
            return _print_full_redesign_refusal(record, revise=True)
        lib.apply_criteria_plan(acceptance, plan)
        lib.reset_amended_criteria(acceptance, record["changed_ids"])
        if plan["resets_original_symptom"]:
            status["requirement_coverage"]["original_symptom_resolved"] = False
            status["original_symptom_evidence_id"] = None
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        lib.sync_coverage(status, acceptance)
        _invalidate_decisions(status)
        status["amendment"] = record
        status["next_action"] = (f"Amendment {record['amendment_id']} was revised: record amendment-review again, "
                                 "then the Pilot records amendment-approve (or escalate it).")
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        errors = lib.validate_status_schema(status) + lib.validate_acceptance_schema(acceptance)
        if errors:
            print("SHIP_FEATURE_BLOCKED\n" + "\n".join(f"- {e}" for e in errors))
            return 1
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, acceptance=acceptance, extra_events=extra,
                  event_kind="amendment_revised",
                  event_message=f"Amendment {record['amendment_id']} revised "
                                f"({plan['operation_count']} more operations; review cleared)",
                  by=args.by.strip(),
                  registry_hash_before=plan["registry_hash_before"], registry_hash_after=plan["registry_hash_after"],
                  **_amendment_event_fields(record))
    print(f"AMENDMENT_REVISED: {record['amendment_id']} now {len(record['changed_ids'])} criteria changed; "
          "review cleared")
    return 0


def cmd_amendment_review(args) -> int:
    """#42: the independent review of the open amendment, bound to the
    amendment hash recomputed from the registry on disk. The reviewer must
    differ from the amendment's author and from the architect on record."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.summary or not args.summary.strip():
        print("SHIP_FEATURE_BLOCKED: --summary must be a non-empty string")
        return 1
    reviewer = args.by.strip()
    findings = [f.strip() for f in (getattr(args, "finding", None) or []) if isinstance(f, str) and f.strip()]
    if len(findings) > lib.MAX_AMENDMENT_REVIEW_FINDINGS or any(len(f) > 512 for f in findings):
        print(f"SHIP_FEATURE_BLOCKED: at most {lib.MAX_AMENDMENT_REVIEW_FINDINGS} findings of at most 512 characters")
        return 1
    if args.approve and findings:
        print("SHIP_FEATURE_BLOCKED: an approved amendment review carries no findings")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        adoption_problem = _adoption_error(status, args)
        if adoption_problem:
            print(f"SHIP_FEATURE_BLOCKED: {adoption_problem}")
            return 1
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        architects = {amendment.get("by")}
        for field in ("design_approved", "design_review"):
            record = status.get(field)
            if isinstance(record, dict) and record.get("architect"):
                architects.add(record["architect"])
        if any(isinstance(a, str) and a.strip().casefold() == reviewer.casefold() for a in architects):
            print("SHIP_FEATURE_BLOCKED: amendment reviewer must differ from the amendment's author and the "
                  "architect on record, no self-review")
            return 1
        recomputed, problems = lib.recompute_amendment_hash(amendment, acceptance.get("criteria", []))
        if recomputed is None:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- amendment hash: {p}" for p in problems))
            return 1
        now = datetime.now(timezone.utc).isoformat()
        decision = "approved" if args.approve else "changes_requested"
        amendment["review"] = {"by": reviewer, "at": now, "decision": decision,
                               "summary": args.summary.strip(), "amendment_hash": recomputed}
        if findings:
            amendment["review"]["findings"] = findings
        if getattr(args, "adopted_session", None):
            amendment["review"]["adopted_session"] = args.adopted_session
            amendment["review"]["adopted_by"] = args.adopted_by
        if decision == "approved":
            status["next_action"] = (f"Amendment {amendment['amendment_id']} review approved: the Pilot records "
                                     "amendment-approve to resume the frozen phase.")
        else:
            status["next_action"] = (f"Amendment {amendment['amendment_id']} review requested changes: the "
                                     "Architect applies amendment-revise or escalates with amendment-escalate.")
        status["updated_at"] = now
        lib.commit(root, cfg, status=status, event_kind="amendment_reviewed",
                  event_message=f"Amendment {amendment['amendment_id']} review {decision.replace('_', ' ')}: "
                                f"{args.summary.strip()}",
                  by=reviewer, decision=decision, amendment_id=amendment["amendment_id"],
                  amendment_hash=recomputed, findings=findings,
                  adopted_session=getattr(args, "adopted_session", None),
                  adopted_by=getattr(args, "adopted_by", None))
    print("AMENDMENT_REVIEW_APPROVED" if args.approve else "AMENDMENT_CHANGES_REQUESTED")
    return 0


def cmd_amendment_approve(args) -> int:
    """#42: the Pilot's approval of the reviewed amendment (human-only; the
    broker refuses it). Requires an approved review bound to the hash
    recomputed from the registry on disk right now; on success the design
    decisions are rewritten to the resulting design hash with the
    amendment id appended to their `amended_by` trail, the amendment
    closes into the history, and the run resumes the frozen phase and
    progress with no phase_advanced event."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    pilot = args.by.strip()
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        if isinstance(amendment.get("by"), str) and amendment["by"].strip().casefold() == pilot.casefold():
            print("SHIP_FEATURE_BLOCKED: approver must differ from the amendment's author, no self-approval")
            return 1
        review = amendment.get("review")
        if not isinstance(review, dict):
            print("SHIP_FEATURE_BLOCKED: the amendment has no review yet; record amendment-review first")
            return 1
        if review.get("decision") != "approved":
            print("SHIP_FEATURE_BLOCKED: the amendment review requested changes; revise (amendment-revise) "
                  "or escalate (amendment-escalate)")
            return 1
        recomputed, problems = lib.recompute_amendment_hash(amendment, acceptance.get("criteria", []))
        if recomputed is None or recomputed != review.get("amendment_hash"):
            problems = problems or ["the reviewed amendment hash does not match the recomputed hash"]
            return _print_audit_block([f"amendment hash: {p}" for p in problems]
                                      + ["amendment approval refused; record a new amendment-review against "
                                         "the registry on disk, or escalate"])
        now = datetime.now(timezone.utc).isoformat()
        scope = lib.work_item_scope_hash(lib.effective_work_items(acceptance, cfg)[0], acceptance.get("criteria", []))
        rewritten = []
        for field in ("design_approved", "design_review"):
            record = status.get(field)
            if not isinstance(record, dict):
                continue
            record["design_hash"] = amendment["resulting_design_hash"]
            if "scope_hash" in record:
                record["scope_hash"] = scope
            trail = list(record.get("amended_by") or [])
            trail.append(amendment["amendment_id"])
            record["amended_by"] = trail[-lib.MAX_AMENDMENT_HISTORY:]
            rewritten.append(field)
        amendment["pilot_approval"] = {"by": pilot, "at": now, "amendment_hash": recomputed}
        closed = deepcopy(amendment)
        _close_amendment(status, closed, state="approved", now=now)
        frozen_phase = int(closed["frozen_phase"])
        status["phase_number"] = frozen_phase
        status["phase"] = lib.PHASES[frozen_phase]
        status["progress"] = closed["frozen_progress"]
        status["next_action"] = lib.phase_next_action(frozen_phase, status, cfg, status.get("next_action"))
        status["updated_at"] = now
        errors = lib.compute_errors(status, acceptance, cfg, verifications=records,
                                    verification_problems=verification_problems)
        if errors:
            print("SHIP_FEATURE_BLOCKED")
            print("\n".join(f"- {x}" for x in errors))
            return 1
        lib.commit(root, cfg, status=status, event_kind="amendment_approved",
                  event_message=f"Amendment {closed['amendment_id']} approved by the Pilot; design decisions "
                                f"rewritten to the amended design, Phase {frozen_phase} resumed",
                  by=pilot, reviewer=review.get("by"), rewritten=rewritten,
                  frozen_phase=frozen_phase, frozen_progress=closed["frozen_progress"],
                  **_amendment_event_fields(closed))
    print(f"AMENDMENT_APPROVED: {closed['amendment_id']} closed; Phase {frozen_phase} resumed")
    return 0


def cmd_question_raise(args) -> int:
    """#46: a role (or the Supervisor on its behalf) raises a question for
    the Pilot. From the role's current live managed session it blocks the
    run with the question as next_action; otherwise it is recorded and shown
    without blocking. Managed children normally use the
    `HANDSOFF_QUESTION: <text>` stdout line instead of this command."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    try:
        record = lib.raise_question(root, role=args.role, text=args.text,
                                    session_id=args.session, by=args.by.strip())
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(f"QUESTION_RAISED: {record['question_id']} ({'blocking' if record['blocking'] else 'non-blocking'})")
    return 0


def cmd_question_answer(args) -> int:
    """#46: the Pilot answers; the block lifts once no blocking question is
    open, and the answer reaches the role on its next launch. #48: `--batch
    FILE` records several answers ({"answers": [{"question_id", "choice"} |
    {"question_id", "other"}]}) in one commit; any bad entry refuses the
    whole file and writes nothing."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    batch = getattr(args, "batch", None)
    single = args.id is not None or args.text is not None
    if batch is None and (args.id is None or args.text is None):
        print("SHIP_FEATURE_BLOCKED: question-answer needs --id and --text, or --batch FILE")
        return 1
    if batch is not None and single:
        print("SHIP_FEATURE_BLOCKED: --batch cannot be combined with --id or --text")
        return 1
    root = lib.resolve_root(args.root)
    try:
        if batch is not None:
            try:
                payload = json.loads(Path(batch).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise lib.HandsoffError(f"batch file could not be read as JSON: {exc}") from exc
            records = lib.answer_questions_batch(root, answers=lib.question_answers_from_payload(payload),
                                                 by=args.by.strip())
            print(f"QUESTIONS_ANSWERED: {' '.join(r['question_id'] for r in records)}")
            return 0
        record = lib.answer_question(root, question_id=args.id, by=args.by.strip(), text=args.text)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(f"QUESTION_ANSWERED: {record['question_id']}")
    return 0


def cmd_amendment_escalate(args) -> int:
    """#42: give up on the scoped lane. The amendment closes as
    `escalated` and the run takes the full path: design decisions cleared
    and, for a flagged run, Phase 2 (the same rollback a criterion
    mutation performs). The amended registry stays as it is."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        lib.ensure_no_launched_regression(status)
        amendment = lib.open_amendment(status)
        if amendment is None:
            print("SHIP_FEATURE_BLOCKED: no amendment is open")
            return 1
        now = datetime.now(timezone.utc).isoformat()
        closed = deepcopy(amendment)
        _close_amendment(status, closed, state="escalated", now=now)
        lib.migrate_review_ledger(status)
        abandoned = lib.abandon_stale_review_attempt(status, acceptance)
        _invalidate_decisions(status, rollback_to=4, invalidate_design=True)
        status["next_action"] = ("Amendment escalated to a full redesign: revise the design, request a new "
                                 "independent design review and human approval, then advance to Phase 3.")
        status["updated_at"] = now
        extra = [{"kind": "review_attempt_closed", "message": "Stale review attempt abandoned",
                  "disposition": "abandoned", "reason": "acceptance_changed"}] if abandoned else None
        lib.commit(root, cfg, status=status, extra_events=extra, event_kind="amendment_escalated",
                  event_message=f"Amendment {closed['amendment_id']} escalated to a full redesign: "
                                f"{args.reason.strip()}",
                  by=args.by.strip(), reason=args.reason.strip(),
                  phase_number=status["phase_number"], **_amendment_event_fields(closed))
    print(f"AMENDMENT_ESCALATED: {closed['amendment_id']} closed; run at Phase {status['phase_number']}")
    return 0


def cmd_recover(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.dry_run:
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            assessment = lib.recovery_assessment(
                status, cfg, lib.read_session_liveness(root), lib.read_events(root, cfg), root=root,
            )
        print(__import__("json").dumps(assessment, indent=2))
        return 0

    def launcher(role, owned_paths=None):
        import handsoff_agent
        task = (f"Resume Phase from trusted Handsoff state as {role}: read handsoff-status.json, "
                "handsoff-acceptance.json and the event log; do not repeat evidenced work.")
        spec = handsoff_agent.build_launch_spec(root, role, task, owned_paths=owned_paths)  # #359
        return handsoff_agent.execute_with_recovery(spec, timeout=args.timeout)

    if getattr(args, "auto", False):
        # #102: one automatic retry after an engine-classified failure. The
        # authorization is ledgered and marked on the failure record, which
        # is what recovery_assessment consults; a second --auto for the
        # same session is refused so a broken environment cannot loop.
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, cfg))
            events = lib.read_events(root, cfg)
            assessment = lib.recovery_assessment(status, cfg, lib.read_session_liveness(root), events, root=root)
            used = {e.get("session_id") for e in events if e.get("kind") == "recovery_auto_confirmed"}
            current_ids = [v for v in lib.role_session_ids(status).values() if isinstance(v, str)]  # #420
            already = next((cid for cid in current_ids if cid in used), None)
            if already and (assessment.get("lost_session_id") in (None, already)):
                print(f"SHIP_FEATURE_BLOCKED: automatic retry already used for {already}")
                return 1
            sid = assessment.get("lost_session_id")
            failure = (status.get("agent_failures") or {}).get(sid) if sid else None
            if assessment.get("reason") != "non_recoverable_failure" or not isinstance(failure, dict):
                print("SHIP_FEATURE_BLOCKED: --auto applies only to a run paused on an engine-classified failure")
                return 1
            if failure.get("category") not in AUTO_RETRY_CATEGORIES:
                print(f"SHIP_FEATURE_BLOCKED: automatic retry is not offered for {failure.get('category')}")
                return 1
            if any(e.get("kind") == "recovery_auto_confirmed" and e.get("session_id") == sid for e in events):
                print(f"SHIP_FEATURE_BLOCKED: automatic retry already used for {sid}")
                return 1
            proposed = deepcopy(status)
            proposed["agent_failures"][sid]["auto_retry_authorized"] = True
            lib.commit(root, cfg, status=proposed, event_kind="recovery_auto_confirmed",
                       event_message="One automatic retry authorized after an engine-classified failure",
                       by=args.by, session_id=sid, category=failure.get("category"))
        print(f"RECOVERY_AUTO_CONFIRMED: {sid}")
    result = lib.recover_run(root, actor=args.by, launcher=launcher)
    label = {
        "skipped": "RECOVERY_SKIPPED", "recovered": "RECOVERY_RECOVERED",
        "failed": "RECOVERY_FAILED", "escalated": "RECOVERY_ESCALATED",
    }[result["action"]]
    print(f"{label}: {result.get('recovery_id') or result['assessment']['reason']}")
    return 1 if result["action"] in {"failed", "escalated"} else 0


def cmd_watch(args) -> int:
    import time
    while True:
        code = cmd_recover(args)
        if code and args.once:
            return code
        if args.once:
            return 0
        # as cmd_performance_watch: 0 would spin on the lock, a negative raises
        time.sleep(max(1, int(args.interval)))


def cmd_recovery_acknowledge(args) -> int:
    actor = lib.validate_agent_actor(args.by)
    if not args.reason or not args.reason.strip():
        print("SHIP_FEATURE_BLOCKED: --reason must be non-empty")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        status, acceptance, records, problems = _load_all(root, cfg)
        escalation = status.get("escalation")
        paused_failure = None
        if not isinstance(escalation, dict) or escalation.get("kind") not in {
                "recovery_exhausted", "recovery_paused"}:
            # #123: a replacement pause on a non-recoverable failure has no
            # escalation record; acknowledging marks that failure so the
            # assessment stops reporting it and a relaunch is possible.
            assessment = lib.recovery_assessment(status, cfg, lib.read_session_liveness(root),
                                                 lib.read_events(root, cfg), root=root)
            paused_failure = (status.get("agent_failures") or {}).get(assessment.get("lost_session_id")) \
                if assessment.get("reason") == "non_recoverable_failure" else None
            if not isinstance(paused_failure, dict):
                print("SHIP_FEATURE_BLOCKED: no recovery escalation is awaiting acknowledgement")
                return 1
            paused_failure["acknowledged"] = True
            escalation = {"source": None}
        status["escalation"] = None
        status["status"] = "in_progress"
        status["next_action"] = lib.phase_next_action(
            int(status.get("phase_number", 1) or 1), status, cfg, "Resume the current mission phase."
        )
        source = next((item for item in reversed(status.get("recovery_attempts") or [])
                       if item.get("recovery_id") == escalation.get("source")), None)
        acknowledged_session_id = None
        if isinstance(source, dict):
            acknowledged_session_id = source.get("to_session_id") or source.get("from_session_id")
        if isinstance(paused_failure, dict):
            acknowledged_session_id = paused_failure.get("session_id")
        lib.commit(root, cfg, status=status, event_kind="recovery_acknowledged",
                   event_message=args.reason.strip(), by=actor,
                   session_id=acknowledged_session_id,
                   role=source.get("role") if isinstance(source, dict) else None)
    print("RECOVERY_ACKNOWLEDGED")
    return 0


def _record_heartbeat(status: dict, owner: str) -> None:
    """The exact liveness update `heartbeat` performs, factored out so
    background-wait-start/end can feed the SAME signal stall_warning/
    activity_note already read, instead of growing a second, parallel
    stall mechanism just for background waits."""
    status["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    status["last_heartbeat_owner"] = owner


def cmd_heartbeat(args) -> int:
    """Record a pure liveness signal for a run doing long background work
    (a slow check, an async agent, a scheduled self-wakeup) that has no
    progress to report yet. Deliberately does not touch phase_number,
    progress, or updated_at -- those stay reserved for calls that actually
    advance the work; last_heartbeat_at is reserved for 'still alive'."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        session_id = args.session
        current = lib.current_agent_sessions(status)
        session = next((item for item in current.values()
                        if isinstance(item, dict) and item.get("session_id") == session_id), None)
        if not isinstance(session, dict) or session.get("state") not in lib.AGENT_SESSION_LIVE_STATES:
            print("SHIP_FEATURE_BLOCKED: --session must name the current live managed session")
            return 1
        _record_heartbeat(status, session_id)
        message = args.note.strip() if args.note and args.note.strip() else "Heartbeat: run is active"
        lib.commit(root, cfg, status=status,
                  event_kind="heartbeat", event_message=message, by=args.by)
    print("HEARTBEAT_RECORDED")
    return 0


def cmd_background_wait_start(args) -> int:
    """Mark the start of a stretch where the run is waiting on a
    background task (an async agent, a slow check, a scheduled
    self-wakeup) rather than idle or waiting on a human. Feeds
    last_heartbeat_at through the exact same path `heartbeat` uses --
    a declared background wait IS a liveness signal, not a second,
    parallel stall mechanism."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"background_wait_started", "background_wait_ended"}) == "background_wait_started":
            print("SHIP_FEATURE_BLOCKED: a background wait is already open; call background-wait-end first")
            return 1
        message = args.note.strip() if args.note and args.note.strip() else "Background task wait started"
        if getattr(args, "resume_after_authorization", False):
            if status.get("phase_number") != 2:
                print("SHIP_FEATURE_BLOCKED: --resume-after-authorization is only valid for a Phase-2 design review")
                return 1
            if status.get("status") != "blocked" or status.get("design_review") \
                    or status.get("authorization_hold") != "design_review":
                print("SHIP_FEATURE_BLOCKED: --resume-after-authorization requires a blocked Phase-2 run "
                      "with authorization_hold=design_review before any review has been recorded")
                return 1
            status["status"] = "in_progress"
            status["next_action"] = message
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            status.pop("authorization_hold", None)
        status["background_wait"] = {
            "by": args.by.strip(), "since": datetime.now(timezone.utc).isoformat(),
            "note": message,
        }
        _record_heartbeat(status, "background_wait")
        lib.commit(root, cfg, status=status,
                  event_kind="background_wait_started", event_message=message, by=args.by)
    print("BACKGROUND_WAIT_STARTED")
    return 0


def cmd_background_wait_end(args) -> int:
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"background_wait_started", "background_wait_ended"}) != "background_wait_started":
            print("SHIP_FEATURE_BLOCKED: no open background wait to end")
            return 1
        status["background_wait"] = None
        _record_heartbeat(status, "background_wait")
        message = args.note.strip() if args.note and args.note.strip() else "Background task wait ended"
        lib.commit(root, cfg, status=status,
                  event_kind="background_wait_ended", event_message=message, by=args.by)
    print("BACKGROUND_WAIT_ENDED")
    return 0


def cmd_human_pause_start(args) -> int:
    """Mark the start of a stretch where the run is waiting on a human for
    something other than the design-approval gate (design_approval_
    requested/design_approved already cover that pair automatically from
    the state machine). Deliberately does not touch last_heartbeat_at:
    nothing is actively running here, so there is nothing alive to
    declare -- that is the whole point of distinguishing this from a
    background wait. The pause is persisted as a durable `human_pause`
    record on status instead (a heartbeat would expire after
    stall_minutes and the pause would read as abandoned again), which is
    what suppresses stall_warning and feeds activity_note while it is
    open. updated_at is left alone so the pause length stays visible."""
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"human_pause_started", "human_pause_ended"}) == "human_pause_started":
            print("SHIP_FEATURE_BLOCKED: a human pause is already open; call human-pause-end first")
            return 1
        note = args.note.strip() if args.note and args.note.strip() else None
        message = note or "Human input pause started"
        status["human_pause"] = {
            "by": args.by.strip(), "since": datetime.now(timezone.utc).isoformat(), "note": note,
        }
        lib.commit(root, cfg, status=status,
                  event_kind="human_pause_started", event_message=message, by=args.by)
    print("HUMAN_PAUSE_STARTED")
    return 0


def cmd_human_pause_end(args) -> int:
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        try:
            status, acceptance, verifications, verification_problems = _load_all(root, cfg)
        except lib.HandsoffError as e:
            print(f"SHIP_FEATURE_BLOCKED: {e}")
            return 1
        audit_errors = _audit_errors(root, cfg, status, verifications, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
        events = lib.read_events(root, cfg)
        if _most_recent_kind(events, {"human_pause_started", "human_pause_ended"}) != "human_pause_started":
            print("SHIP_FEATURE_BLOCKED: no open human pause to end")
            return 1
        status["human_pause"] = None
        message = args.note.strip() if args.note and args.note.strip() else "Human input pause ended"
        lib.commit(root, cfg, status=status,
                  event_kind="human_pause_ended", event_message=message, by=args.by)
    print("HUMAN_PAUSE_ENDED")
    return 0


def cmd_design_timing(args) -> int:
    """Read-back over the event log: report design-phase wall-clock time
    by category (active/background_wait/human_wait), per round -- the
    piece that makes 'did the Architect make design faster' answerable
    from a finished or in-flight run, not just 'how long did design take
    total'. Defaults to the live project's own event log; --events-file
    points it at any handsoff-events.jsonl, including an archived one
    under .handsoff-archive/<run>/, so a past run can be re-examined
    exactly like a current one."""
    json_module = __import__("json")
    if args.events_file:
        path = Path(args.events_file)
        if not path.exists():
            print(f"SHIP_FEATURE_BLOCKED: no such events file: {path}")
            return 1
        events = [json_module.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        root = lib.resolve_root(args.root)
        cfg = lib.load_config(root)
        events = lib.read_events(root, cfg)
    summary = lib.summarize_design_timing(events)
    print(json_module.dumps(summary, indent=2))
    return 0


def cmd_implementation_review_packet(args) -> int:
    """#397: print the packet a reviewer launched at Phase 5 receives, as
    JSON, so the host sees exactly what the reviewer will get. Read-only:
    the ledger, status and acceptance files are untouched."""
    json_module = __import__("json")
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    packet = lib.implementation_review_packet(root, cfg, compact=args.compact)
    print(json_module.dumps(packet, indent=2, sort_keys=True))
    return 0


def cmd_design_evidence(args) -> int:
    """#38: `design-evidence run` executes the configured [[design_evidence]]
    measurements that are missing, stale, or failed (or all of them with
    --force) and caches their bounded output in handsoff-design-evidence.json;
    `design-evidence show` prints the state view the dashboard and the role
    prompts consume. Neither touches status or acceptance; `run` appends a
    hashes-only `design_evidence_recorded` event per executed command."""
    json_module = __import__("json")
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if args.action == "show":
        print(json_module.dumps({"artifacts": lib.design_evidence_view(root, cfg)}, indent=2))
        return 0
    if not args.by or not args.by.strip():
        print("SHIP_FEATURE_BLOCKED: --by must be a non-empty string")
        return 1
    if not cfg.get("design_evidence"):
        print("SHIP_FEATURE_NO_DESIGN_EVIDENCE_CONFIGURED: add [[design_evidence]] tables to handsoff.toml")
        return 1
    with lib.project_lock(root):
        status, acceptance, existing_records, verification_problems = _load_all(root, cfg)
        audit_errors = _audit_errors(root, cfg, status, existing_records, verification_problems)
        if audit_errors:
            return _print_audit_block(audit_errors)
    # The commands run outside the lock (they may be slow); run_design_evidence
    # takes the lock itself around the store write and the event commit.
    records = lib.run_design_evidence(root, cfg, ids=args.id or None, by=args.by, force=args.force)
    summary = [{
        "id": r["id"], "reused": r["reused"], "exit_code": r["exit_code"], "input_hash": r["input_hash"],
        "output_sha256": r["output_sha256"], "output_bytes": r["output_bytes"], "truncated": r["truncated"],
        "matched_files": r["matched_files"], "head": r["head"], "at": r["at"],
    } for r in records]
    ok = all(r["exit_code"] == 0 for r in records)
    print(json_module.dumps({"ok": ok, "artifacts": summary}, indent=2))
    return 0 if ok else 1


def cmd_verify_log(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    with lib.project_lock(root):
        problems = lib.verify_event_log(root, cfg)
        records, verification_problems = lib.load_verifications(root, cfg)
        problems.extend(verification_problems)
        try:
            status = lib.load_unique_json(lib.status_path(root, cfg))
            actual_head = records[-1].get("hash") if records else "GENESIS"
            if status.get("verification_head") != actual_head:
                problems.append("verification ledger tail does not match its anchored head")
        except lib.HandsoffError as exc:
            problems.append(str(exc))
    if problems:
        print("EVENT_LOG_TAMPERED_OR_CORRUPT")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    print("EVENT_LOG_INTACT")
    return 0


def cmd_doctor(args) -> int:
    """Diagnose an interrupted cross-file write and, only when it is
    provably safe, recover from it.

    The two writes that can be caught mid-commit are: (a) status and/or
    acceptance were written but the describing event never got
    appended, and (b) a verification record was appended to the ledger
    but the status.json update that re-anchors verification_head to it
    never landed. Both leave a chain-head mismatch that blocks delivery,
    as documented in the README's known limitations.

    Gap (a) is NOT safe to recover just because the current status/
    acceptance content happens to pass every gate: an arbitrary hand
    edit (change a free-text field, tweak a counter nothing validates
    strictly) can also happen to still validate, and blessing that would
    launder an untracked edit into the audit trail as if it were a real
    crash. So gap (a) is only recovered when the CURRENT file content
    exactly matches a write-ahead journal entry (see commit() in
    handsoff_lib.py) proving it was the intended, in-flight output of a
    real command, not merely a state that passes validation.

    Gap (b) needs no such proof: it only ever patches verification_head
    to a value doctor computes itself from the authenticated ledger, and
    only when status has NOT drifted from the last logged event (i.e.
    gap (a) does not also apply) -- so there is no unproven content
    riding along with the fix.

    Beyond that, this command refuses to touch anything unless: the
    event log's own hash chain and tail anchor are intact, the
    verification ledger's own hash chain is intact, AND the corrected
    state it proposes to write independently passes the same validation
    `advance` would run against it. Any sign of tampering, reordering,
    or an invalid resulting state is reported and left for the operator
    to restore from version control or backup; doctor never guesses its
    way past real damage."""
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    if getattr(args, "prompts", False) or getattr(args, "probe", None):
        # P0.4 / P1.9: read-only reports; neither takes the project lock nor
        # writes anything, and the probe runs only when named here
        report, code = lib.doctor_prompt_probe_report(root, cfg, prompts=args.prompts, probe=args.probe)
        print(json.dumps(report, indent=2, sort_keys=True))
        return code
    with lib.project_lock(root):
        chain_problems, last_event = lib.event_log_chain_errors(root, cfg)
        records, ledger_problems = lib.load_verifications(root, cfg)
        if chain_problems or ledger_problems:
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: ledger integrity is broken; "
                  "restore the affected log and its anchor from version control or backup.")
            for p in chain_problems:
                print(f"- event log: {p}")
            for p in ledger_problems:
                print(f"- verification ledger: {p}")
            return 1
        status_file = lib.status_path(root, cfg)
        acceptance_file = lib.acceptance_path(root, cfg)
        if not status_file.exists() or not acceptance_file.exists():
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: status or acceptance file is missing; nothing to re-anchor.")
            return 1
        status = lib.load_unique_json(status_file)
        acceptance = lib.load_unique_json(acceptance_file)

        status_sha = lib._file_sha256(status_file)
        acceptance_sha = lib._file_sha256(acceptance_file)
        status_stale = last_event is None or last_event.get("status_sha256") != status_sha
        acceptance_stale = last_event is None or last_event.get("acceptance_sha256") != acceptance_sha
        actual_verification_head = records[-1].get("hash") if records else "GENESIS"
        head_stale = status.get("verification_head") != actual_verification_head

        if not status_stale and not acceptance_stale and not head_stale:
            lib.clear_write_ahead(root)  # prune any orphaned journal from a since-completed write
            print("SHIP_FEATURE_DOCTOR_OK: event log and verification ledger are already anchored "
                  "to the current state; nothing to recover.")
            return 0

        journal = lib.read_write_ahead(root)

        def _proven(stale: bool, key: str, current_sha: str | None) -> bool:
            if journal and key in journal:
                return journal[key] == current_sha
            return not stale

        if not _proven(status_stale, "status_sha256", status_sha) or not _proven(acceptance_stale, "acceptance_sha256", acceptance_sha):
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: the event log has not recorded the current "
                  "status/acceptance state, and no write-ahead journal entry proves this exact "
                  "content was the intended output of a real, interrupted command, or the journal "
                  "names a partial or mismatched transaction that never fully landed on disk. "
                  "Recovering without that proof could launder an untracked hand edit, or bless a "
                  "half-applied write, as a crash instead of fixing one. Restore from version "
                  "control or backup, or redo the command that should have produced this state.")
            return 1

        # Journal-confirmed (or absent) staleness is now proven safe. The
        # verification_head patch is the one piece of content doctor is
        # allowed to originate itself, since it is derived, not chosen.
        proposed_status = dict(status)
        if head_stale:
            proposed_status["verification_head"] = actual_verification_head
        errors = lib.compute_errors(proposed_status, acceptance, cfg,
                                    verifications=records, verification_problems=[])
        if errors:
            print("SHIP_FEATURE_DOCTOR_CANNOT_RECOVER: the recoverable state does not independently "
                  "validate, so recovering would paper over a real problem instead of fixing one. "
                  "Resolve these first, or restore from backup:")
            for e in errors:
                print(f"- {e}")
            return 1

        gaps = []
        if status_stale or acceptance_stale:
            gaps.append("event log had not recorded the current, write-ahead-confirmed status/acceptance state")
        if head_stale:
            gaps.append("status.verification_head lagged the verification ledger's actual tail")

        if args.dry_run:
            print("SHIP_FEATURE_DOCTOR_WOULD_RECOVER: " + "; ".join(gaps) + ". "
                  "The resulting state validates cleanly. Re-run without --dry-run to fix.")
            return 0

        # If only the event log lagged, status/acceptance on disk are
        # already exactly right (that is what the journal just proved);
        # re-appending the event (which re-hashes whatever is currently
        # on disk) closes the gap with no file rewrite needed.
        lib.commit(root, cfg, status=proposed_status if head_stale else None,
                  event_kind="recovered",
                  event_message="Doctor re-anchored the ledgers after an interrupted write: " + "; ".join(gaps))
    print("SHIP_FEATURE_DOCTOR_RECOVERED: " + "; ".join(gaps) + ".")
    return 0


def cmd_dashboard(args) -> int:
    """Launch the local, read-only Mission Control dashboard."""
    root = lib.resolve_root(args.root)
    from handsoff_dashboard import serve
    return serve(root, host=args.host, port=args.port, open_browser=not args.no_open,
                 owned_by_run=bool(getattr(args, "owned_by_run", False)))


def _close_transaction_identity(root: Path, cfg: dict) -> tuple[str, Path]:
    """Stable identity/path for the current initialized run."""
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        events = lib.read_events(root, cfg)
        problems = lib.verify_event_log(root, cfg)
    if problems:
        raise lib.HandsoffError("run-close: event log authentication failed: " + problems[0])
    # A local run-reopen starts a new close episode.  It retains the run's
    # feature identity while preventing a completed prior close transaction
    # from suppressing the new report/close reconciliation pass.
    reopen_count = sum(event.get("kind") == "run_reopened" for event in events)
    token = hashlib.sha256(
        f"{lib.feature_hash(status, events)}:{reopen_count}".encode("utf-8")
    ).hexdigest()
    path = root / ".handsoff-archive" / "close-transactions" / f"{token}.json"
    return token, path


def _load_close_transaction(root: Path, cfg: dict) -> tuple[close_transaction.CloseTransaction, Path]:
    token, path = _close_transaction_identity(root, cfg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError) as exc:
        raise lib.HandsoffError(f"run-close: close transaction is unreadable: {exc}") from exc
    try:
        record = close_transaction.migrate_transaction(
            raw, root=root, run_token=token, authenticated=True,
        )
    except close_transaction.CloseTransactionError as exc:
        raise lib.HandsoffError(f"run-close: {exc}") from exc

    def persist(value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_json(path, value)

    transaction = close_transaction.CloseTransaction(record, persist=persist)
    persist(record)
    return transaction, path


def _fleet_run_state(root: Path) -> str | None:
    import handsoff_fleet as fleet
    entry = next((row for row in fleet.load_registry() if row.get("root") == str(root.resolve())), None)
    return entry.get("state") if isinstance(entry, dict) else None


REPORT_ITEM_OPERATIONS = ("commented", "closed", "ticked")
UNATTRIBUTED_CLOSURE = "closed without attributable Handsoff ownership; left alone"


def _report_item_complete(row: dict) -> bool:
    operations = row.get("operations") if isinstance(row.get("operations"), dict) else {}
    return all(isinstance(operations.get(name), dict)
               and operations[name].get("state") in close_transaction.TERMINAL_OPERATION_STATES
               for name in REPORT_ITEM_OPERATIONS)


def _handsoff_closed_issues(root: Path, cfg: dict) -> set[int]:
    """Issues an earlier close episode of this run closed, from the
    authenticated ledger. A deliberate run-reopen may reconcile them
    without treating an arbitrary pre-existing closed issue as ours."""
    closed = set()
    for event in lib.read_events(root, cfg):
        if event.get("kind") != "report_posted":
            continue
        for posted in event.get("posted") or []:
            if isinstance(posted, dict) and isinstance(posted.get("number"), int) \
                    and posted.get("closed") is True:
                closed.add(posted["number"])
    return closed


def _issue_closure_snapshot(root: Path, row: dict, number: int, issue: dict, *, run_token: str,
                            known_closed: set[int], run_closures: dict) -> tuple[dict, list[dict]]:
    """The checkpoint_issue_state snapshot of one read issue, and the run's
    merged pull requests it may be attributed to."""
    state = str(issue.get("state") or "").lower()
    if state != "closed":
        return {"state": state}, []
    closed_row = (row.get("operations") or {}).get("closed") or {}
    legacy = row.get("legacy") or {}
    # Handsoff dispatched this close (a lost read-back), finished it, or an
    # earlier episode or a pre-#381 row says so and the report is there.
    ours = (bool(closed_row.get("acted_at")) or closed_row.get("state") == "complete"
            or lib.issue_closed_by_report(issue)
            or ((number in known_closed or legacy.get("closed") is True
                 or legacy.get("closed_dispatched") is True) and lib.issue_has_report(issue)))
    if ours:
        return {"state": "closed", "closure": {"kind": "handsoff_run", "run_token": run_token}}, []
    if "map" not in run_closures:
        run_closures["map"] = lib.run_pull_request_closures(root)
    pr_number = run_closures["map"].get(number)
    if pr_number is None:
        return {"state": "closed", "closure": {"kind": "unknown"}}, []
    return ({"state": "closed", "closure": {"kind": "pull_request", "pr_number": pr_number, "merged": True}},
            [{"number": pr_number, "merged": True}])


def _report_item_operations(root: Path, number: int, issue: dict, *, body: str, head: str,
                            seen: dict) -> dict:
    """#381: the commented, closed and ticked Operations of one issue. The
    first observation of each is the read just checkpointed; read-back asks
    GitHub again."""
    def read(fields: str) -> dict:
        found = lib.read_report_issue(root, number, fields=fields)
        if found is None:
            raise lib.HandsoffError(f"issue #{number} could not be read")
        return found

    def first_then(fields: str):
        cached = [issue]
        return lambda: cached.pop() if cached else read(fields)

    comment_view = first_then("comments")
    state_view = first_then("state")

    def comment() -> None:
        posted = lib._gh(["issue", "comment", str(number), "--body", body], cwd=root)
        if posted.returncode != 0:
            raise lib.HandsoffError("comment failed")
        seen["commented_now"] = True
        seen["url"] = posted.stdout.strip().splitlines()[-1] if posted.stdout.strip() else None

    def close() -> None:
        closed = lib._gh(["issue", "close", str(number), "-c",
                          f"Closed by the Handsoff run report ({head[:12]})."], cwd=root)
        if closed.returncode != 0:
            raise lib.HandsoffError("close failed")

    parent = lib.report_issue_parent(issue)
    seen["parent"] = parent

    def parent_body() -> str:
        view = lib._gh(["issue", "view", str(parent), "--json", "body"], cwd=root)
        try:
            found = json.loads(view.stdout) if view.returncode == 0 else None
        except ValueError:
            found = None
        if not isinstance(found, dict):
            raise lib.HandsoffError(f"parent #{parent} could not be read")
        return str(found.get("body") or "")

    def tick_state() -> dict:
        if parent is None:
            return {"parent": None, "ticked": True}
        return {"parent": parent, "ticked": lib.parent_box_ticked(parent_body(), number)}

    def tick() -> None:
        new_body = lib.tick_parent_box(parent_body(), number)
        if new_body is None:
            raise lib.HandsoffError(f"parent #{parent} has no box for #{number}")
        edited = lib._gh(["issue", "edit", str(parent), "--body", new_body], cwd=root)
        if edited.returncode != 0:
            raise lib.HandsoffError("tick failed")

    return {
        "commented": close_transaction.Operation(
            lambda: {"reported": lib.issue_has_report(comment_view())},
            lambda value: value["reported"], comment),
        "closed": close_transaction.Operation(
            lambda: {"state": str(state_view().get("state") or "").upper()},
            lambda value: value["state"] == "CLOSED", close),
        "ticked": close_transaction.Operation(tick_state, lambda value: value["ticked"], tick),
    }


def _reconcile_report_items(root: Path, cfg: dict, transaction, *, by: str, revalidate) -> dict:
    """#381: post the final report one issue at a time through
    CloseTransaction.reconcile_item. Each issue's state is checkpointed
    first; a closure that is not the run's is recorded unpostable and the
    rest still post."""
    with lib.project_lock(root):
        status, acceptance, verifications, _problems = _load_all(root, cfg)
        events = lib.read_events(root, cfg)
    prepared = lib.prepare_final_report(root, cfg, status, acceptance, events, verifications,
                                        _validate_lines(root, cfg))
    if prepared["reason"]:
        return _record_report(root, cfg, {"posted": [], "skipped": [], "reason": prepared["reason"],
                                          "detail": prepared["detail"]}, by=by)
    token = transaction.record["run_token"]
    rows = transaction.record.setdefault("items", {})
    known_closed = _handsoff_closed_issues(root, cfg)
    run_closures: dict = {}
    posted, skipped = [], []
    for item in lib.report_issue_items(status, acceptance, cfg):
        number = item["number"]
        row = rows.setdefault(str(number), {"operations": {}})
        issue = lib.read_report_issue(root, number)
        if issue is None:
            # Not unpostable: a read can succeed on the next post.
            skipped.append({"number": number, "reason": "state could not be read"})
            continue
        snapshot, run_prs = _issue_closure_snapshot(root, row, number, issue, run_token=token,
                                                    known_closed=known_closed, run_closures=run_closures)
        try:
            close_transaction.checkpoint_issue_state(row, "pre_report", snapshot, run_prs,
                                                     run_token=token, persist=transaction._persist)
        except close_transaction.IssueClosureBlocked:
            row["unpostable"] = UNATTRIBUTED_CLOSURE
            transaction._persist()
            skipped.append({"number": number, "unpostable": True, "reason": UNATTRIBUTED_CLOSURE})
            continue
        if row.pop("unpostable", None) is not None:
            transaction._persist()
        seen: dict = {"commented_now": False, "url": None}
        operations = _report_item_operations(root, number, issue, body=prepared["body"],
                                             head=prepared["head"], seen=seen)
        try:
            transaction.reconcile_item(str(number), operations, revalidate=revalidate)
        except close_transaction.ReconciliationError:
            pass  # the row keeps its error; the other items still post
        state = {name: (row.get("operations") or {}).get(name, {}).get("state")
                 for name in REPORT_ITEM_OPERATIONS}
        if state["commented"] != "complete":
            skipped.append({"number": number, "reason": "comment failed"})
            continue
        if not seen["commented_now"]:
            skipped.append({"number": number, "reason": "already posted"})
        posted.append({"number": number, "url": seen["url"], "closed": state["closed"] == "complete",
                       "parent": seen["parent"],
                       "ticked": seen["parent"] is not None and state["ticked"] == "complete",
                       "comment": "posted" if seen["commented_now"] else "skipped"})
    return _record_report(root, cfg, {"posted": posted, "skipped": skipped, "reason": None,
                                      "detail": None, "head": prepared["head"]}, by=by)


def _owner_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _dashboard_ownership(root: Path) -> dict:
    """#381: who owns the run's dashboard, decided by
    close_transaction.resource_ownership. The owner record binds its root
    only as root_sha256, so this root stands in for a matching hash and no
    root for any other; the live endpoint is asked only for this root."""
    path = lib.dashboard_owner_path(root)
    expected_sha = lib.dashboard_root_sha256(root)
    metadata = endpoint = None
    record: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        record = loaded if isinstance(loaded, dict) else {}
        own_root = record.get("root_sha256") == expected_sha
        metadata = {"root": str(root) if own_root else None,
                    "run_token": record.get("run_token"), "pid": record.get("pid")}
        port = record.get("port")
        if own_root and isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            host = record.get("host") if record.get("host") in lib.DASHBOARD_LOOPBACK_HOSTS else "127.0.0.1"
            try:
                answer = lib._dashboard_json_request(
                    f"{lib._dashboard_base_url(host, port)}/api/ownership", timeout=2.0)
            except (OSError, ValueError):
                answer = None
            if isinstance(answer, dict) and answer.get("owned") is True:
                endpoint = {"root": str(root) if answer.get("root_sha256") == expected_sha else None,
                            "run_token": answer.get("run_token"), "pid": record.get("pid")}
    expected = {"root": str(root), "run_token": record.get("run_token"), "pid": record.get("pid")}
    return close_transaction.resource_ownership(expected, metadata, endpoint, pid_alive=_owner_pid_alive)


def _release_owned_dashboard(root: Path, cfg: dict) -> None:
    """Release only what _dashboard_ownership proves is this run's: an owned
    server is shut down, a stale pointer file is removed, nothing else."""
    ownership = _dashboard_ownership(root)
    if ownership["state"] == "owned":
        _release_run_dashboard(root, cfg)
    elif ownership["state"] == "stale":
        lib.dashboard_owner_path(root).unlink(missing_ok=True)
        with lib.project_lock(root):
            lib.commit(root, cfg, event_kind="dashboard_release_skipped",
                       event_message=f"Run-owned dashboard release skipped: {ownership['reason']}",
                       reason=ownership["reason"])
        print(f"HANDSOFF_DASHBOARD_RELEASE_SKIPPED: stale ownership metadata removed: {ownership['reason']}")


def _run_close_transaction(args, root: Path, cfg: dict) -> dict:
    """Execute the product close path as an ordered, resumable transaction."""
    transaction, transaction_path = _load_close_transaction(root, cfg)
    result: dict = {}

    def current_status() -> dict:
        with lib.project_lock(root):
            return lib.load_unique_json(lib.status_path(root, cfg))

    def revalidate() -> None:
        token, path = _close_transaction_identity(root, cfg)
        if token != transaction.record.get("run_token") or path != transaction_path:
            raise close_transaction.CloseConflict("run identity changed during close")

    def close_state() -> dict:
        status = current_status()
        return {"closed": isinstance(status.get("run_closed"), dict),
                "run_closed": status.get("run_closed")}

    def close_action() -> None:
        # #296: a run that never reached verified Phase 8 is aborted or
        # released-unverified, never a clean close. The operator may still
        # override with an explicit --outcome.
        outcome = getattr(args, "outcome", None) or (
            "closed" if lib.run_is_live_verified(close_status, cfg)
            else lib.unverified_close_outcome(close_status))
        known_risk = getattr(args, "known_risk", None)
        # P2.4: the outcome and its text ride on the close transaction too
        transaction.record["close_outcome"] = {"outcome": outcome, "reason": args.reason,
                                               "known_risk": known_risk}
        transaction._persist()
        result.update(lib.close_run(
            root, by=args.by, reason=args.reason,
            expected_updated_at=getattr(args, "expected_updated_at", None),
            cancel_active=bool(getattr(args, "cancel_active", False)),
            release_dashboard=False, outcome=outcome, known_risk=known_risk,
        ))

    started_at = transaction.record.get("created_at") or ""

    with lib.project_lock(root):
        close_status, close_acceptance = _load(root, cfg)
    # #296: posting the report is the claim that the work was delivered, and
    # it closes the issues. On 2026-09-23 that happened while the run sat at
    # Phase 7, with the released wheel never installed and `verify-live`
    # never run. The claim is only true once the INSTALLED artifact has been
    # verified. The run may still be closed, as aborted or
    # released_unverified; what is refused is the claim, so the work items
    # stay open, which is exactly what REQ-006 asks for.
    posting_requested = bool(getattr(args, "post", False))
    delivery_unverified = posting_requested and not lib.run_is_live_verified(close_status, cfg)
    unverified_reason = None
    if delivery_unverified:
        unverified_reason = (
            "reporting the run as delivered requires verified installed-artifact Phase 8; this run "
            f"is at phase {close_status.get('phase_number')} and progress {close_status.get('progress')} "
            f"with live verification {'recorded' if close_status.get('live_verification_id') else 'absent'}. "
            "Run verify-live and advance 8 100, then close again to post; the work items stay open."
        )
        print(f"HANDSOFF_REPORT_NOT_POSTED: {unverified_reason}")
    expected_issue_numbers = {
        item["number"] for item in lib.derive_work_items(close_status, close_acceptance, cfg).get("items", [])
        if item.get("kind") == "issue" and isinstance(item.get("number"), int)
    }

    def report_state() -> dict:
        matching = [event for event in lib.read_events(root, cfg)
                    if event.get("kind") in {"report_posted", "report_not_posted"}
                    and str(event.get("at") or "") >= started_at]
        rows = transaction.record.get("items") or {}
        # #374: an item skipped as unpostable (closed by someone else) is
        # settled and left alone; it does not hold the close open. #381:
        # every other item is complete only on its operations rows.
        complete = all(
            bool(rows.get(str(number), {}).get("unpostable"))
            or _report_item_complete(rows.get(str(number), {}))
            for number in expected_issue_numbers
        )
        kind = matching[-1].get("kind") if matching else None
        return {"recorded": bool(matching), "kind": kind,
                "items_complete": complete and kind == "report_posted"}

    def report_action() -> None:
        _reconcile_report_items(root, cfg, transaction, by=args.by, revalidate=revalidate)

    def fleet_action() -> None:
        import handsoff_fleet as fleet
        fleet.note_registry_state(root, None, "closed")

    # #381: the close archive is written once; a resumed close adopts it only
    # when it is byte-identical, and a differing file is a conflict.
    archive_path = (root / ".handsoff-archive" / "close-archives"
                    / f"{transaction.record['run_token']}.json")
    archive_intended: list[bytes] = []

    def archive_bytes() -> bytes:
        if not archive_intended:
            archive_intended.append(close_transaction.archive_bytes({
                "schema": 1, "root": transaction.record.get("root"),
                "run_token": transaction.record.get("run_token"),
                "run_closed": current_status().get("run_closed"),
                "items": transaction.record.get("items") or {},
            }))
        return archive_intended[0]

    def archive_state() -> dict:
        intended = hashlib.sha256(archive_bytes()).hexdigest()
        exists = archive_path.is_file()
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest() if exists else None
        return {"path": str(archive_path), "exists": exists, "matches": actual == intended}

    def config_overrides() -> list[dict]:
        return [entry for entry in current_status().get("config_overrides") or [] if isinstance(entry, dict)]

    def config_restore_state() -> dict:
        decided = transaction.record.get("config_restore") or {}
        return {"pending": [entry["key"] for entry in config_overrides() if entry["key"] not in decided],
                "decisions": deepcopy(decided)}

    def config_restore_action() -> None:
        decisions = transaction.record.setdefault("config_restore", {})
        for entry in config_overrides():
            key = entry["key"]
            if key in decisions:
                continue
            with lib.project_lock(root):
                outcome = close_transaction.compare_and_restore_config(
                    read=lambda: lib.read_config_value(root, key),
                    write=lambda value: lib.write_config_value(root, key, value),
                    original=entry["original"], run_written=entry["run_written"],
                )
            decisions[key] = {**outcome, "original": entry["original"], "run_written": entry["run_written"]}
            transaction._persist()
            print(f"HANDSOFF_CONFIG_RESTORE: {key} {outcome['decision']}: {outcome['reason']}")

    operations = {
        "prepare": close_transaction.Operation(close_state, lambda value: value["closed"], close_action),
        "final_report_post": close_transaction.Operation(
            report_state, lambda value: value["items_complete"], report_action,
            optional=not posting_requested or delivery_unverified,
            unavailable_reason=(unverified_reason if delivery_unverified
                                else (None if posting_requested else "--post was not requested")),
        ),
        # The existing retire_finished_run path moves the full ledgers at init.
        "archive": close_transaction.Operation(
            archive_state, lambda value: value["matches"],
            lambda: close_transaction.write_archive_once(archive_path, archive_bytes()),
        ),
        "fleet_unregister": close_transaction.Operation(
            lambda: {"state": _fleet_run_state(root)},
            lambda value: value["state"] == "closed", fleet_action,
        ),
        # #381: a dashboard another run owns is left alone, not released.
        "dashboard_shutdown": close_transaction.Operation(
            lambda: _dashboard_ownership(root),
            lambda value: value["state"] in {"absent", "foreign"},
            lambda: _release_owned_dashboard(root, cfg),
        ),
        # #382: each override config-override recorded is restored, adopted
        # or preserved once, and the decision is kept on the transaction.
        "config_restore": close_transaction.Operation(
            config_restore_state, lambda value: not value["pending"], config_restore_action,
        ),
        "optional_analysis": close_transaction.Operation(
            lambda: {"applicable": False}, lambda _value: False, lambda: None,
            optional=True, unavailable_reason="analysis runs after a completed Phase 8 archive",
        ),
    }
    try:
        transaction.run(operations, revalidate=revalidate)
    except close_transaction.CloseTransactionError as exc:
        raise lib.HandsoffError(f"run-close: {exc}") from exc
    if not result:
        closed = close_state()["run_closed"]
        result = {"closed": True, "already_closed": True, "run_closed": closed,
                  "dashboard": {"released": False, "reason": "closure transaction resumed"}}
    result["transaction"] = {"state": transaction.record["state"], "path": str(transaction_path)}
    result["archive"] = {"path": str(archive_path)}
    return result


def cmd_config_override(args) -> int:
    """#382: the only run-scoped writer of handsoff.toml."""
    root = lib.resolve_root(args.root)
    try:
        entry = lib.record_config_override(root, key=args.key, value=args.value, by=args.by)
    except lib.HandsoffError as exc:
        print(f"CONFIG_OVERRIDE_BLOCKED: {exc}")
        return 1
    print(f"CONFIG_OVERRIDE_RECORDED: {entry['key']} = {json.dumps(entry['run_written'])} "
          f"(original {json.dumps(entry['original'])}); run-close restores it")
    return 0


def cmd_run_close(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    try:
        if args.outcome or args.known_risk is not None:
            # P2.4: refused before a close transaction exists
            with lib.project_lock(root):
                status = lib.load_unique_json(lib.status_path(root, cfg))
            if not isinstance(status.get("run_closed"), dict):
                lib.validate_close_outcome(status, cfg, args.outcome or "closed", args.reason, args.known_risk)
        result = _run_close_transaction(args, root, cfg)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _validate_lines(root, cfg) -> list[str]:
    with lib.project_lock(root):
        status, acceptance, verifications, problems = _load_all(root, cfg)
        errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                    verification_problems=problems, root=root)
    return ["SHIP_FEATURE_VALID"] if not errors else ["SHIP_FEATURE_BLOCKED"] + [f"- {e}" for e in errors]


def _post_report(root, cfg, *, by: str, known_handsoff_closed: set[int] | None = None,
                 checkpoint=None) -> dict:
    """#171: render from the ledger, post once per ticket, record the outcome."""
    with lib.project_lock(root):
        status, acceptance, verifications, _problems = _load_all(root, cfg)
        events = lib.read_events(root, cfg)
    validate = _validate_lines(root, cfg)
    outcome = lib.post_final_report(
        root, cfg, status, acceptance, events, verifications, validate, by=by,
        known_handsoff_closed=known_handsoff_closed,
        checkpoint=checkpoint,
    )
    return _record_report(root, cfg, outcome, by=by)


def _record_report(root, cfg, outcome: dict, *, by: str) -> dict:
    with lib.project_lock(root):
        lib.record_report_outcome(root, cfg, outcome, by=by)
    if outcome.get("reason"):
        print(f"HANDSOFF_REPORT_NOT_POSTED: {outcome['detail']}")
    else:
        print("HANDSOFF_REPORT_POSTED: " + ", ".join(f"#{p['number']}" for p in outcome["posted"])
              + (" (skipped: " + ", ".join(f"#{x['number']} {x['reason']}" for x in outcome["skipped"]) + ")"
                 if outcome["skipped"] else ""))
    return outcome


def _release_tickets(root, state: str) -> None:
    """#166: a closed or completed run holds no ticket. The register entry
    says so; the lock also reads the run's own status, so this is the
    visible half, never the only half."""
    try:
        import handsoff_fleet as fleet
        fleet.note_registry_state(root, None, state)
    except (lib.HandsoffError, OSError):
        pass


def cmd_run_reopen(args) -> int:
    result = lib.reopen_run(
        lib.resolve_root(args.root), by=args.by, reason=args.reason,
        expected_updated_at=getattr(args, "expected_updated_at", None),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_performance_status(args) -> int:
    root = lib.resolve_root(args.root)
    with lib.project_lock(root):
        print(json.dumps(refresh_performance_state(root, lock_held=True), indent=2, sort_keys=True))
    return 0


#: #295: how often the run-owned clock re-evaluates its own deadline. The
#: 90- and 120-minute boundaries are the thing being protected, so a minute
#: of granularity bounds the overshoot at a minute while costing one cheap
#: local read per tick.
PERFORMANCE_TICK_SECONDS = 60


def performance_tick(root: Path, *, now: datetime | None = None) -> dict:
    """Evaluate the deadline once, from the run's own clock.

    `transition_performance` is pure and only decides when something calls
    it. Until #295 the only callers were the supervisor CLI dispatch and an
    open dashboard, so during the v0.3.80 closeout the run spent an hour
    past its ceiling with nothing evaluating it: the durable record's last
    write was 21:57:57Z and the deadline was 22:01:36Z. This is the caller
    that does not depend on anyone typing a command.
    """
    with lib.project_lock(root):
        return refresh_performance_state(root, now=now, lock_held=True)


def cmd_performance_watch(args) -> int:
    """Run the clock for the life of the run, across every episode.

    It does NOT retire at the first pause. `performance-resume` opens a new
    episode with its own deadlines, so a clock that stopped at the first
    pause would leave every later episode unobserved: exactly the v0.3.80
    shape where episode 1 paused on time and episode 2 never did.
    """
    root = lib.resolve_root(args.root)
    interval = max(1, int(args.interval))
    deadline_ticks = None if args.once else args.max_ticks
    ticks = 0
    announced: set[str] = set()
    while True:
        try:
            view = performance_tick(root)
        except (lib.HandsoffError, OSError) as exc:
            # A run that has been closed or archived stops the watch rather
            # than spinning on a file that will not come back.
            print(f"HANDSOFF_PERFORMANCE_WATCH_STOPPED: {exc}")
            return 0
        ticks += 1
        if view["transition"] != "none":
            print(json.dumps({"transition": view["transition"], "state": view["state"],
                              "episode_id": view["episode_id"],
                              "active_seconds": view["active_seconds"]}, sort_keys=True))
        # Announced once per episode, so a held pause does not spam, and the
        # next episode's pause is still reported.
        if view["state"] == "paused_for_performance_review" and view["episode_id"] not in announced:
            announced.add(view["episode_id"])
            print(f"HANDSOFF_PERFORMANCE_PAUSED: {view['episode_id']} reached the active-run ceiling")
        if args.once or (deadline_ticks is not None and ticks >= deadline_ticks):
            return 0
        time.sleep(interval)


def cmd_performance_resume(args) -> int:
    root = lib.resolve_root(args.root)
    with lib.project_lock(root):
        history = _runtime_read(root, PERFORMANCE_RECORD, "handsoff.performance_history")
        if history is None:
            raise lib.HandsoffError("no performance episode exists to resume")
        # #350: the resume is bound to the paused episode it reopens. The
        # engine computes the hash; a supplied value must match it, so a
        # resume prepared against an earlier pause cannot clear a later one.
        expected = runtime_control.content_hash(history["episodes"][-1])
        supplied = getattr(args, "evidence_hash", None)
        if supplied is not None and supplied != expected:
            raise lib.HandsoffError(
                f"performance-resume refused: --evidence-hash {supplied} does not match the "
                f"paused episode {history['episodes'][-1]['episode_id']}, whose hash is {expected}; "
                "omit the flag to bind to the current pause")
        at = datetime.now(timezone.utc)
        decision = {
            "decision_id": f"resume-{uuid.uuid4().hex}", "action": "resume", "actor": args.by,
            "reason": args.reason, "evidence_hash": expected, "at": at.isoformat(),
        }
        resumed = runtime_control.resume_performance(
            history, decision, f"episode-{len(history['episodes']) + 1}",
        )
        _runtime_write(root, PERFORMANCE_RECORD, resumed)
        _append_performance_timeline(root, resumed, lock_held=True)
    print(f"PERFORMANCE_RESUMED: bound to paused episode hash {expected}")
    return 0


def cmd_performance_auto_resume(args) -> int:
    """#414: the Pilot's standing decision, [performance] auto_resume = true,
    recorded as a ledger event. Each pause that begins after it resumes by
    itself with a performance_auto_resumed event naming it; --off withdraws
    it. Human-only: it is not in the broker's table, so no managed role can send it."""
    for name in ("by", "reason"):
        if not isinstance(getattr(args, name), str) or not getattr(args, name).strip():
            raise lib.HandsoffError(f"performance-auto-resume: --{name} must be a non-empty string")
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    enabled = not getattr(args, "off", False)
    decision_id = f"auto-resume-{uuid.uuid4().hex[:16]}"
    with lib.project_lock(root):
        if not lib.status_path(root, cfg).exists():
            raise lib.HandsoffError("performance-auto-resume: no current run (run init first)")
        lib.commit(root, cfg, event_kind=PERFORMANCE_AUTO_RESUME_EVENT,
                   event_message=(f"Pilot {args.by.strip()} recorded [performance] auto_resume = "
                                  f"{'true' if enabled else 'false'} ({decision_id})"),
                   decision_id=decision_id, auto_resume=enabled, by=args.by.strip(),
                   reason=" ".join(args.reason.split())[:400])
    print(f"PERFORMANCE_AUTO_RESUME_RECORDED: {decision_id} auto_resume={'true' if enabled else 'false'}"
          + ("; each later performance pause resumes by itself" if enabled else ""))
    return 0


def cmd_monitor_poll(args) -> int:
    root = lib.resolve_root(args.root)
    cfg = lib.load_config(root)
    now = datetime.now(timezone.utc)
    with lib.project_lock(root):
        status = lib.load_unique_json(lib.status_path(root, cfg))
        events = lib.read_events(root, cfg)
        performance = refresh_performance_state(root, status=status, events=events, now=now,
                                                lock_held=True)
        record = _runtime_read(root, MONITOR_RECORD, "handsoff.monitor")
        if record is None or record.get("run_id") != performance["run_id"]:
            record = runtime_control.new_monitor(performance["run_id"], args.owner, now=now,
                                                 lease_seconds=args.lease_seconds)
        else:
            record = runtime_control.claim_monitor(
                record, args.owner, now=now, lease_seconds=args.lease_seconds,
                expected_epoch=record["lease_epoch"], expected_cursor=record["cursor"],
            )
        cursor = len(events)
        if cursor > record["cursor"]:
            record = runtime_control.advance_monitor_cursor(
                record, owner_instance=args.owner, lease_epoch=record["lease_epoch"],
                expected_cursor=record["cursor"], new_cursor=cursor, now=now,
            )
        snapshot = _runtime_snapshot(status, events, performance)
        policy = {"same_task_retry_cap": 2, "equivalent_quota_fallback": True,
                  "safe_result_adoption": True}
        decision = runtime_control.decide_monitor_action(snapshot, policy)
        if decision["action"] == "complete_monitor":
            record = runtime_control.finish_monitor(
                record, owner_instance=args.owner, lease_epoch=record["lease_epoch"],
                expected_cursor=record["cursor"], now=now,
            )
        _runtime_write(root, MONITOR_RECORD, record)
    print(json.dumps({"monitor": record, "decision": decision, "performance": performance}, indent=2, sort_keys=True))
    return 0


def cmd_evidence_refresh_plan(args) -> int:
    root = lib.resolve_root(args.root)
    def read_bounded(path_text: str) -> dict | list:
        path = Path(path_text)
        if not path.is_absolute():
            path = root / path
        if path.stat().st_size > 1_000_000:
            raise lib.HandsoffError(f"runtime-control input is too large: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    mapping = read_bounded(args.map)
    changes = read_bounded(args.changes)
    hashes = read_bounded(args.hashes)
    regenerated = read_bounded(args.regenerated) if args.regenerated else None
    assessment = runtime_control.assess_dependency_drift(
        mapping, args.subject, changes, hashes, current_input_hash=args.input_hash,
        regenerated_outputs=regenerated,
    )
    event = runtime_control.dependency_audit_event(assessment, at=datetime.now(timezone.utc))
    with lib.project_lock(root):
        path = _runtime_path(root, EVIDENCE_AUDIT_RECORD)
        audit = lib.load_unique_json(path) if path.exists() else {"schema": 1, "events": []}
        audit["events"] = (audit.get("events") or [])[-4095:] + [event]
        _runtime_write(root, EVIDENCE_AUDIT_RECORD, audit)
    print(json.dumps(assessment, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Project Handsoff supervisor and gatekeeper")
    p.add_argument("--root", default=None, help="project root (default: nearest ancestor with handsoff.toml, else cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--adopt", nargs="?", const=True, default=False,
                      help="#166: take over a ticket whose registered owner is dead or closed; a live owner is never adopted")
    init.add_argument("feature")
    init.add_argument("--by", default=None, help="#186: the host actor driving this run (claude-host, codex-implementer); "
                      "recorded on initialized so the page can name the host family")
    init.add_argument("--item", action="append", default=[],
                      help="declare one issue (#31 or #31 Title) or plain ask; repeat for multiple items")
    init.add_argument("--lane", choices=("full", "design", "review", "small-fix"), default="full",
                      help="initial run or delivery lane")
    init.add_argument("--from-design", default=None, metavar="PATH",
                      help="take up a completed design.html document")
    init.add_argument("--risk-class", choices=lib.ADAPTIVE_RISK_CLASSES, default="routine",
                      help="set adaptive routing risk (default: routine)")

    sub.add_parser("status", help="print run state as JSON, including the requested crew per role "
                                  "with its source (explicit or recommended) and adapter availability")
    sub.add_parser("validate")
    sub.add_parser("verify-log")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--dry-run", action="store_true")
    doctor.add_argument("--prompts", action="store_true",
                        help="P0.4: report every role's effective prompt, its source and verdict, without launching")
    doctor.add_argument("--probe", metavar="ADAPTER", default=None,
                        help="P1.9: run a bounded real protocol exchange with ADAPTER (codex, claude or all); "
                             "spends tokens, records nothing in the run ledger")

    dashboard = sub.add_parser("dashboard", help="open the local read-only Mission Control dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    dashboard.add_argument("--owned-by-run", action="store_true",
                           help="mark this server as owned by the current run: it writes "
                                ".handsoff-dashboard-owner.json and `advance 8` with status complete "
                                "shuts it down so the port is free for the next run (the persistent "
                                "LaunchAgent and manual launches leave this off)")

    verify = sub.add_parser("verify")
    verify_scope = verify.add_mutually_exclusive_group(required=True)
    verify_scope.add_argument("--criterion", action="append",
                              help="criterion id to bind this run to; repeat for more than one")
    verify_scope.add_argument("--all", action="store_true",
                              help="#363: verify every automated criterion in the registry in one run; "
                                   "each distinct command is launched once")
    verify.add_argument("--by", required=True)
    verify.add_argument("--no-cache", action="store_true",
                        help="launch every needed command even when an eligible record for its binding exists")
    verify.add_argument("--expect-fail", action="store_true",
                        help="#165: record a baseline: the criterion's own commands are expected to FAIL on this tree "
                             "(the feature is not built yet); a command that passes makes the baseline invalid")

    adopt = sub.add_parser("session-result-adopt")
    adopt.add_argument("--session", required=True)
    adopt.add_argument("--by", required=True)

    implementer_apply = sub.add_parser("implementer-apply",
                                       help="#413: apply a stopped implementer's owned-path changes")
    implementer_discard = sub.add_parser("implementer-discard",
                                         help="#413: remove a stopped implementer's kept workspace")
    for disposition in (implementer_apply, implementer_discard):
        disposition.add_argument("--session", required=True)
        disposition.add_argument("--by", required=True)

    release_plan = sub.add_parser("release-plan", help="record semantic release class and verification policy")
    release_plan.add_argument("--version", required=True)
    release_plan.add_argument("--by", required=True)
    release_plan.add_argument("--full-regression-override-reason")

    release_reconcile = sub.add_parser(
        "release-reconcile", help="publish, install and verify the planned release transaction")
    # Required for a release; --inspect is read-only and needs neither.
    release_reconcile.add_argument("--artifact")
    release_reconcile.add_argument("--by")
    release_reconcile.add_argument("--repository")
    release_reconcile.add_argument("--commit")
    release_reconcile.add_argument("--manifest")
    release_reconcile.add_argument("--notes-file")
    release_reconcile.add_argument("--inspect", action="store_true",
                                   help="#378: print the release transaction record's state; read-only")

    regression_request = sub.add_parser("regression-request")
    regression_request.add_argument("--group", required=True)
    regression_request.add_argument("--by", required=True)
    regression_request.add_argument("--reason", default="Release confidence requested")

    regression_decide = sub.add_parser("regression-decide")
    regression_decide.add_argument("--request-id", required=True)
    regression_decide.add_argument("--by", required=True)
    regression_choice = regression_decide.add_mutually_exclusive_group(required=True)
    regression_choice.add_argument("--accept", action="store_true")
    regression_choice.add_argument("--decline", action="store_true")

    regression_run = sub.add_parser("regression-run")
    regression_run.add_argument("--request-id", required=True)
    regression_run.add_argument("--by", required=True)

    regression_cancel = sub.add_parser("regression-cancel")
    regression_cancel.add_argument("--request-id", required=True)
    regression_cancel.add_argument("--by", required=True)

    regression_finalize = sub.add_parser("regression-finalize")
    regression_finalize.add_argument("--request-id", required=True)
    regression_finalize.add_argument("--by", required=True)

    work_sync = sub.add_parser("work-items-sync")
    work_sync.add_argument("--by", required=True)
    work_sync.add_argument("--from-tickets", action="store_true")
    work_sync.add_argument("--item", action="append", default=[],
                           help="explicit work item to add, repeatable")

    work_activate = sub.add_parser("work-item-activate")
    work_activate.add_argument("item")
    work_activate.add_argument("--by", required=True)

    work_update = sub.add_parser("work-item-update")
    work_update.add_argument("item")
    work_update.add_argument("--by", required=True)
    work_update.add_argument("--title")
    work_update.add_argument("--url")
    work_update.add_argument("--github-state", choices=("open", "closed"))
    work_update.add_argument("--notes")
    work_update.add_argument("--implemented-by")
    required_choice = work_update.add_mutually_exclusive_group()
    required_choice.add_argument("--required", action="store_true")
    required_choice.add_argument("--optional", action="store_true")

    work_remove = sub.add_parser("work-item-remove")
    work_remove.add_argument("item")
    work_remove.add_argument("--by", required=True)

    lane_request = sub.add_parser("lane-request", help="request the measured small-fix lane for one item")
    lane_request.add_argument("item")
    lane_request.add_argument("--by", required=True)

    lane_confirm = sub.add_parser("lane-confirm", help="Pilot confirms an eligible small-fix lane")
    lane_confirm.add_argument("item")
    lane_confirm.add_argument("--by", required=True)

    plan_tranche = sub.add_parser("plan-tranche", help="build a deterministic read-only backlog proposal")
    plan_tranche.add_argument("--repo", required=True, help="GitHub owner/repository")
    plan_tranche.add_argument("--issues-file", default=None, help="offline JSON issue inventory")
    plan_tranche.add_argument("--archive-dir", default=None)
    plan_tranche.add_argument("--limit", type=int, default=5)

    tranche_approve = sub.add_parser("tranche-approve", help="Pilot atomically approves a proposal")
    tranche_approve.add_argument("--proposal-hash", required=True)
    tranche_approve.add_argument("--by", required=True)
    tranche_approve.add_argument("--item", action="append", default=None,
                                 help="retained item id in approved order; repeat")
    tranche_approve.add_argument("--drop", action="append", default=None,
                                 help="explicitly dropped item id; repeat")

    evidence = sub.add_parser("record-evidence")
    evidence.add_argument("criterion")
    evidence.add_argument("--kind", choices=("manual", "browser"), required=True)
    evidence.add_argument("--description", required=True)
    evidence.add_argument("--by", required=True)

    workflow_check = sub.add_parser(
        "workflow-check",
        help="P1.4: run [checks].workflow_commands and record workflow evidence for a criterion")
    workflow_check.add_argument("--criterion", required=True)
    workflow_check.add_argument("--by", required=True)

    qa_report = sub.add_parser("qa-report", help="P1.5: untrusted browser QA reports")
    qa_report_actions = qa_report.add_subparsers(dest="action", required=True)
    qa_report_add = qa_report_actions.add_parser(
        "add", help="store a QA report for an allowlisted [qa].targets origin; never evidence by itself")
    qa_report_add.add_argument("--file", required=True)
    qa_report_add.add_argument("--target", required=True)
    qa_report_add.add_argument("--by", required=True)
    qa_report_map = qa_report_actions.add_parser(
        "map", help="record browser evidence citing a QA report; refused for the report's author")
    qa_report_map.add_argument("--report", required=True)
    qa_report_map.add_argument("--criterion", required=True)
    qa_report_map.add_argument("--by", required=True)

    mutation_proof = sub.add_parser(
        "mutation-proof",
        help="prove the criterion's configured test fails when a named function is neutralised")
    mutation_proof.add_argument("criterion")
    mutation_proof.add_argument("--timeout", type=int, default=mutation.DEFAULT_TIMEOUT,
                                help="seconds for ONE run of the criterion's command")
    mutation_proof.add_argument("--total-timeout", type=int, default=mutation.DEFAULT_TOTAL_TIMEOUT,
                                help="seconds for the whole proof, across every run; a proof "
                                     "that exceeds it is reported as out of budget, not as a "
                                     "failed proof")
    mutation_proof.add_argument("--by", required=True)

    symptom = sub.add_parser("record-symptom-resolved")
    symptom.add_argument("--evidence", required=True, help="successful verification run id")
    symptom.add_argument("--by", required=True)

    design_approve = sub.add_parser("design-approve", help="record explicit human approval of the Architect's "
                                    "proposed design/criteria; required before Phase 3+ for any run init flagged")
    design_approve.add_argument("--by", required=True, help="the human approver's identity")
    design_approve.add_argument("--architect", required=True,
                                help="the identity that proposed the design (must differ from --by)")
    design_approve.add_argument("--summary", required=True,
                                help="non-empty design summary (approach/tradeoffs/decisions), recorded as the event message")
    design_approve.add_argument("--redesigns-settled-work", default=None,
                                help="omit for a design that only fits around existing/shipped work; pass a "
                                "non-empty description ONLY when the human explicitly asked to redesign "
                                "already-settled work, making that exception visible in the audit trail")

    design_reject = sub.add_parser("design-reject", help="Pilot rejects the currently reviewed design")
    design_reject.add_argument("--by", required=True)
    design_reject.add_argument("--reason", required=True)

    design_review = sub.add_parser("record-design-review",
                                   help="record an independent Phase-2 review of the Architect's design")
    design_review.add_argument("--by", required=True, help="independent design reviewer's identity")
    design_review.add_argument("--architect", required=True, help="identity of the Architect being reviewed")
    design_review.add_argument("--summary", required=True, help="review findings or approval rationale")
    design_review.add_argument("--session", default=None,
                               help="host-only exact managed Reviewer session binding")
    design_review.add_argument("--adopted-session", default=None,
                        help="#115: the terminal managed session whose persisted result this record adopts")
    design_review.add_argument("--adopted-by", default=None,
                        help="#115: the Pilot or Supervisor who adopted the persisted result")
    design_review_decision = design_review.add_mutually_exclusive_group(required=True)
    design_review_decision.add_argument("--approve", action="store_true")
    design_review_decision.add_argument("--request-changes", action="store_true")
    design_review.add_argument("--finding", action="append", default=None,
                               help="one concrete finding (at most 512 characters); repeat for more, at most 32; "
                               "each gets the id F<attempt>.<n> that a later design-review-packet "
                               "disposition refers to")
    design_review.add_argument("--structural-blocker", action="store_true",
                               help="only with --request-changes: the design needs structural rework, so the "
                               "next attempt goes to the primary reviewer tier even when a follow-up "
                               "reviewer profile is configured (#37)")

    design_propose = sub.add_parser("design-propose", help="record a proposal: summary 1 to 512 characters; each list at most 8 items of at most 512 characters")
    design_propose.add_argument("--file", required=True, help="JSON proposal file")
    design_propose.add_argument("--by", required=True, help="host Architect identity")

    design_review_packet = sub.add_parser(
        "design-review-packet",
        help="generate the delta review packet for the next design-review attempt from the most recent "
             "recorded review (#36): criteria delta, dispositioned prior findings, evidence states, and "
             "repository identity; refused before the first review, which gets the full task")
    design_review_packet.add_argument("--by", required=True, help="identity generating the packet")
    design_review_packet.add_argument(
        "--disposition", action="append", default=None, metavar="ID=resolved|rejected|unresolved[:note]",
        help="disposition of one prior finding by id; rejected requires a note; an omitted finding "
             "defaults to unresolved")

    design_review_authorize = sub.add_parser(
        "design-review-authorize",
        help="Pilot-only: permit one more design-review attempt (or --rounds N) once the autonomous "
             "budget ([workflow] max_autonomous_design_reviews) is exhausted; each round is consumed by "
             "a record-design-review, whether a managed reviewer session or a human recorded it")
    design_review_authorize.add_argument("--by", required=True, help="the Pilot's identity")
    design_review_authorize.add_argument("--note", default=None,
                                         help="optional one-line reason, recorded as the event message")
    design_review_authorize.add_argument("--rounds", type=int, default=1,
                                         help="#417: how many more attempts this one ledgered grant permits "
                                              "(1 to 5, default 1), consumed one per recorded attempt")

    design_review_escalate = sub.add_parser(
        "design-review-escalate",
        help="Pilot-only: force the next design-review attempt onto the primary reviewer tier instead of "
             "the configured follow-up profile (#37); consumed by the next record-design-review")
    design_review_escalate.add_argument("--by", required=True, help="the Pilot's identity")
    design_review_escalate.add_argument("--note", default=None,
                                        help="optional one-line reason, recorded as the event message")

    review = sub.add_parser("record-review")
    review.add_argument("--by", required=True)
    review.add_argument("--session", default=None,
                        help="host-only exact managed Reviewer session binding")
    review.add_argument("--adopted-session", default=None,
                        help="#115: the terminal managed session whose persisted result this record adopts")
    review.add_argument("--adopted-by", default=None,
                        help="#115: the Pilot or Supervisor who adopted the persisted result")
    review.add_argument("--item", default=None,
                        help="record an item-scoped independent review for a confirmed small-fix lane")
    review.add_argument("--symptom-reproduced", choices=("yes", "not_applicable"), default="yes")
    review.add_argument("--tests-executed", choices=("yes", "no", "unknown"), default="unknown",
                        help=lib.REVIEW_TESTS_EXECUTED_RULE)
    review.add_argument("--reaffirm", action="store_true",
                        help="re-bind this reviewer's latest approved attempt after an evidence-only refresh; "
                             "opens no attempt and spends no review budget")

    review_start = sub.add_parser("review-attempt-start")
    review_start.add_argument("--by", required=True)
    review_start.add_argument("--reviewer", default=None)
    review_start.add_argument("--trigger", choices=tuple(sorted(lib.REVIEW_ATTEMPT_TRIGGERS)), default=None)
    review_start.add_argument("--note", default=None)

    review_findings = sub.add_parser("record-review-findings")
    review_findings.add_argument("--by", required=True)
    review_findings.add_argument("--session", default=None,
                                 help="host-only exact managed Reviewer session binding")
    review_findings.add_argument("--adopted-session", default=None,
                        help="#115: the terminal managed session whose persisted result this record adopts")
    review_findings.add_argument("--adopted-by", default=None,
                        help="#115: the Pilot or Supervisor who adopted the persisted result")
    review_findings.add_argument("--finding", action="append", required=True)
    review_findings.add_argument("--tests-executed", choices=("yes", "no", "unknown"), default="unknown")

    review_override = sub.add_parser("review-cap-override")
    review_override.add_argument("--by", required=True)
    review_override.add_argument("--reason", required=True)

    recover = sub.add_parser("recover")
    recover.add_argument("--by", required=True)
    recover.add_argument("--auto", action="store_true",
                         help="#102: authorize exactly one automatic retry after an engine-classified failure")
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument("--timeout", type=int, default=3600)

    watch = sub.add_parser("watch")
    watch.add_argument("--by", required=True)
    watch.add_argument("--interval", type=int, default=30)
    watch.add_argument("--timeout", type=int, default=3600)
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--once", action="store_true")

    recovery_ack = sub.add_parser("recovery-acknowledge")
    recovery_ack.add_argument("--by", required=True)
    recovery_ack.add_argument("--reason", required=True)

    live = sub.add_parser("verify-live")
    live.add_argument("--by", required=True)

    heartbeat = sub.add_parser("heartbeat", help="record a liveness signal for a run doing long "
                               "background work, without advancing phase or progress")
    heartbeat.add_argument("--by", required=True)
    heartbeat.add_argument("--session", required=True,
                           help="current live managed session id that owns this heartbeat")
    heartbeat.add_argument("--note", default=None, help="optional one-line description of the background activity")

    bg_start = sub.add_parser("background-wait-start", help="mark the start of a wait on a background "
                              "task (an async agent, a slow check); also records a heartbeat")
    bg_start.add_argument("--by", required=True)
    bg_start.add_argument("--note", default=None, help="optional one-line description of the background task")
    bg_start.add_argument(
        "--resume-after-authorization", action="store_true",
        help="atomically clear a Phase-2 human authorization hold as the approved background review starts",
    )

    bg_end = sub.add_parser("background-wait-end", help="mark the end of the currently open background-task wait")
    bg_end.add_argument("--by", required=True)
    bg_end.add_argument("--note", default=None)

    hp_start = sub.add_parser("human-pause-start", help="mark the start of a wait on a human, other than "
                              "the design-approval gate (which is tracked automatically)")
    hp_start.add_argument("--by", required=True)
    hp_start.add_argument("--note", default=None, help="optional one-line description of what is being waited on")

    hp_end = sub.add_parser("human-pause-end", help="mark the end of the currently open human-input pause")
    hp_end.add_argument("--by", required=True)
    hp_end.add_argument("--note", default=None)

    timing = sub.add_parser("design-timing", help="report design-phase wall-clock time by category "
                            "(active/background_wait/human_wait) and by round")
    timing.add_argument("--events-file", default=None,
                        help="path to a handsoff-events.jsonl to summarize instead of the live project's own "
                        "(e.g. an archived run under .handsoff-archive/<run>/handsoff-events.jsonl)")

    review_packet = sub.add_parser(
        "implementation-review-packet",
        help="#397: print, read-only, the packet a Phase-5 reviewer launch receives")
    review_packet.add_argument("--compact", action="store_true",
                               help="the compact-scope variant: judge from the recorded results, run no tests")

    design_evidence = sub.add_parser(
        "design-evidence",
        help="run or show the cached, hash-bound [[design_evidence]] measurements (#38): a "
             "configured command runs once and is reused until its command or declared inputs change",
    )
    design_evidence_actions = design_evidence.add_subparsers(dest="action", required=True)
    design_evidence_run = design_evidence_actions.add_parser(
        "run", help="execute every missing, stale, or failed measurement and cache its bounded output")
    design_evidence_run.add_argument("--id", action="append", default=None,
                                     help="artifact id to run; repeat for more than one (default: all)")
    design_evidence_run.add_argument("--by", required=True)
    design_evidence_run.add_argument("--force", action="store_true",
                                     help="rerun even when the cached record is current (the reviewer's challenge path)")
    design_evidence_actions.add_parser("show", help="print every artifact's state as JSON, never its output")

    criterion = sub.add_parser("criterion-update")
    criterion.add_argument("criterion")
    criterion.add_argument("--type", choices=("primary_fix", "supporting"))
    criterion.add_argument("--requirement")
    criterion.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS))
    criterion.add_argument("--test", action="append")
    criterion.add_argument("--state", choices=("failing", "not_tested", "blocked"))
    criterion.add_argument("--baseline", choices=(lib.BASELINE_NOT_APPLICABLE, "none"),
                           help="#165: not_applicable declares no failing run can exist for this criterion (with --baseline-reason); none clears it")
    criterion.add_argument("--baseline-reason")
    criterion.add_argument("--repeat", type=int, help="#169: the command must pass this many times in a row (1 to 50; 1 clears)")
    criterion.add_argument("--seed-env", help="#169: environment variable that receives a distinct seed per attempt")
    criterion.add_argument("--mutation-target",
                           help="#349: the project-relative file defining the function this "
                                "criterion's tests must detect the loss of")
    criterion.add_argument("--mutation-symbol",
                           help="#349: that function's name; required with automated_and_mutation")
    criterion.add_argument("--outcome", help="P1.1: the observable outcome (at most 512 characters; '' clears)")
    criterion.add_argument("--evidence-class", action="append", dest="evidence_class",
                           help="P1.1: an evidence class required on top of the policy "
                                "(checks, manual, browser, workflow); repeatable, replaces the list")
    criterion.add_argument("--no-evidence-classes", action="store_true",
                           help="P1.1: clear the declared evidence classes")
    criterion.add_argument("--path", action="append", dest="path",
                           help="P1.2: a project-relative glob the evidence depends on; repeatable, replaces the list")
    criterion.add_argument("--no-paths", action="store_true", help="P1.2: clear the declared paths")
    criterion.add_argument("--revoke-approval", action="store_true")

    criterion_add = sub.add_parser("criterion-add")
    criterion_add.add_argument("criterion")
    criterion_add.add_argument("--type", choices=("primary_fix", "supporting"), required=True)
    criterion_add.add_argument("--requirement", required=True)
    criterion_add.add_argument("--verification", choices=tuple(lib.VERIFICATION_REQUIREMENTS), required=True)
    criterion_add.add_argument("--test", action="append", required=True)
    criterion_add.add_argument("--baseline", choices=(lib.BASELINE_NOT_APPLICABLE,),
                               help="#165: declare that no failing run can exist for this criterion (with --baseline-reason)")
    criterion_add.add_argument("--baseline-reason")
    criterion_add.add_argument("--repeat", type=int, help="#169: the command must pass this many times in a row (1 to 50)")
    criterion_add.add_argument("--seed-env", help="#169: environment variable that receives a distinct seed per attempt")
    criterion_add.add_argument("--mutation-target",
                               help="#349: the project-relative file defining the function this "
                                    "criterion's tests must detect the loss of")
    criterion_add.add_argument("--mutation-symbol",
                               help="#349: that function's name; required with automated_and_mutation")
    criterion_add.add_argument("--outcome", help="P1.1: the observable outcome (at most 512 characters)")
    criterion_add.add_argument("--evidence-class", action="append", dest="evidence_class",
                               help="P1.1: an evidence class required on top of the policy "
                                    "(checks, manual, browser, workflow); repeatable")
    criterion_add.add_argument("--path", action="append", dest="path",
                               help="P1.2: a project-relative glob the evidence depends on; repeatable")
    criterion_add.add_argument("--revoke-approval", action="store_true")

    criterion_remove = sub.add_parser("criterion-remove")
    criterion_remove.add_argument("criterion")

    criteria_apply = sub.add_parser("criteria-apply", help="apply a list of add/update/remove criterion "
                                    "operations from a JSON file as one all-or-nothing commit (#44)")
    criteria_apply.add_argument("--file", required=True, help="TX.json: {\"operations\": [...]}")
    criteria_apply.add_argument("--by", required=True)
    criteria_apply.add_argument("--dry-run", action="store_true",
                                help="validate and print the plan as JSON; write nothing")
    criteria_apply.add_argument("--revoke-approval", action="store_true")

    amendment_open = sub.add_parser("amendment-open", help="open a scoped post-approval amendment from a "
                                    "criteria transaction file; the planner classifies it and refuses a "
                                    "full redesign (#42)")
    amendment_open.add_argument("--file", required=True, help="TX.json: {\"operations\": [...]}")
    amendment_open.add_argument("--by", required=True, help="the Architect opening the amendment")
    amendment_open.add_argument("--summary", default=None)
    amendment_open.add_argument("--request-full-redesign", action="store_true",
                                help="force the full_redesign classification (the command then refuses to open)")

    amendment_revise = sub.add_parser("amendment-revise", help="apply a follow-up transaction to the open "
                                      "amendment; recomputes its hash and clears the stale review (#42)")
    amendment_revise.add_argument("--file", required=True)
    amendment_revise.add_argument("--by", required=True)

    amendment_review = sub.add_parser("amendment-review", help="record the independent review of the open "
                                      "amendment, bound to its hash (#42)")
    amendment_review.add_argument("--by", required=True)
    amendment_review_decision = amendment_review.add_mutually_exclusive_group(required=True)
    amendment_review_decision.add_argument("--approve", action="store_true")
    amendment_review_decision.add_argument("--request-changes", action="store_true")
    amendment_review.add_argument("--summary", required=True)
    amendment_review.add_argument("--finding", action="append", default=[],
                                  help="a reviewer finding carried on a request-changes review (#146)")
    amendment_review.add_argument("--adopted-session", default=None,
                                  help="#146: the terminal reviewer session whose persisted verdict this records")
    amendment_review.add_argument("--adopted-by", default=None)

    amendment_approve = sub.add_parser("amendment-approve", help="Pilot approval of the reviewed amendment: "
                                       "rewrites the design hashes and resumes the frozen phase (#42, human-only)")
    amendment_approve.add_argument("--by", required=True)

    amendment_escalate = sub.add_parser("amendment-escalate", help="close the open amendment as escalated and "
                                        "take the full redesign path (#42)")
    amendment_escalate.add_argument("--by", required=True)
    amendment_escalate.add_argument("--reason", required=True)

    question_raise = sub.add_parser("question-raise", help="record a role's question for the Pilot (#46); a "
                                    "managed child prints HANDSOFF_QUESTION: <text> instead")
    question_raise.add_argument("--role", required=True, choices=lib.SELECTABLE_AGENT_ROLES)
    question_raise.add_argument("--text", required=True)
    question_raise.add_argument("--by", required=True)
    question_raise.add_argument("--session", default=None, help="managed session id the question belongs to")

    question_answer = sub.add_parser("question-answer", help="Pilot answer to an open question (#46, human-only); "
                                     "--batch FILE answers several at once (#48)")
    question_answer.add_argument("--id", default=None)
    question_answer.add_argument("--text", default=None)
    question_answer.add_argument("--batch", default=None, metavar="FILE",
                                 help='JSON file {"answers": [{"question_id", "choice"} | {"question_id", "other"}]}')
    question_answer.add_argument("--by", required=True)

    analyze = sub.add_parser("analyze-archives", help="scan the completed-run archive for recurring patterns "
                             "and file evidenced improvement tickets (#49); prints the report path")
    analyze.add_argument("--dry-run", action="store_true", help="evaluate and report, file nothing")
    analyze.add_argument("--propose-rules", action="store_true",
                         help="#167: write one draft launch rule per failure shape that repeats across runs "
                              "into rules/proposed/ (never evaluated until a human moves it up and commits it)")
    analyze.add_argument("--archive-dir", default=None, help="archive directory for this scan only "
                         "(default: [analysis].archive_dir, else HANDSOFF_ARCHIVE_DIR, else ~/Documents/Handsoff-Archive)")

    pilot_note = sub.add_parser("pilot-note", help="record a Pilot observation on the current run (#49); "
                                "the next archive scan lists it as an R7 finding")
    pilot_note.add_argument("--by", required=True)
    pilot_note.add_argument("--text", required=True, help="1 to 512 characters")

    shadow_apply = sub.add_parser("shadow-apply", help="#302: apply one shadow routing recommendation; "
                                  "refused without a Mission Control approval for that finding and change")
    shadow_apply.add_argument("--report", required=True, help="the handsoff_shadow report JSON")
    shadow_apply.add_argument("--finding", required=True, help="the finding id (shf-...)")
    shadow_apply.add_argument("--approval", required=True, help="the approval id Mission Control recorded (apr-...)")

    shadow_route = sub.add_parser("shadow-route", help="#379: read-only shadow mode; the routed choice beside "
                                  "the frozen policy's pick for one cohort, with handsoff.toml unchanged")
    shadow_route.add_argument("--report", required=True, help="the handsoff_shadow report JSON")
    shadow_route.add_argument("--role", required=True)
    shadow_route.add_argument("--repository", required=True, help="OWNER/NAME")
    shadow_route.add_argument("--task-class", required=True)
    shadow_route.add_argument("--variant", default="all_evidence",
                              choices=["all_evidence", "excluding_low_quality"])

    evidence_activate = sub.add_parser("evidence-routing-activate",
                                       help="#303: activate evidence-assisted routing; refused without a "
                                       "#302 finding and its Mission Control approval")
    evidence_activate.add_argument("--finding", required=True, help="the #302 finding id (shf-...)")
    evidence_activate.add_argument("--approval", required=True,
                                   help="the approval id Mission Control recorded for it (apr-...)")
    evidence_activate.add_argument("--by", required=True)

    decline = sub.add_parser("design-decline", help="#177: the Architect declines the change at Phase 1 or 2; "
                             "recorded hash-bound to the criteria, the run closes as not_planned")
    decline.add_argument("--by", required=True, help="the Architect actor")
    decline.add_argument("--reason", required=True, help="1 to 512 characters: why the change is not needed")
    decline.add_argument("--evidence", action="append", default=[], help="what shows it (repeatable, at most 8)")
    decline.add_argument("--alternative", default=None, help="what to do instead, if anything")

    ci_watch = sub.add_parser("ci-watch", help="#181: watch a pull request's checks as a step of the run; "
                              "Mission Control shows the CI row until they complete")
    ci_watch.add_argument("--pr", type=int, default=None, help="pull request number to watch (starts a watch)")
    ci_watch.add_argument("--by", default="host", help="who started the watch")
    ci_watch.add_argument("--poll", action="store_true", help="refresh now and print the CI view as JSON")

    config_override = sub.add_parser("config-override", help="#382: set one handsoff.toml value for this run "
                                     "only; run-close restores it unless a human edited it since")
    config_override.add_argument("--key", required=True, help="dotted table.key the config schema knows")
    config_override.add_argument("--value", required=True)
    config_override.add_argument("--by", required=True)

    run_close = sub.add_parser("run-close", help="cleanly close a run and release owned resources")
    run_close.add_argument("--by", required=True)
    run_close.add_argument("--reason", required=True)
    run_close.add_argument("--expected-updated-at")
    run_close.add_argument("--cancel-active", action="store_true")
    run_close.add_argument("--post", action="store_true",
                           help="#171: post the ledger's report to each issue work item, close it and tick its epic box before closing")
    run_close.add_argument("--outcome", choices=lib.RUN_OUTCOMES, default=None,
                           help="#296: name the outcome explicitly; without it a run that has "
                                "not passed verified Phase 8 records aborted or released_unverified")
    run_close.add_argument("--known-risk", default=None, metavar="TEXT",
                           help="P2.4: the risk a verified_with_known_risk close accepts (required with it)")

    run_reopen = sub.add_parser("run-reopen", help="reopen a non-complete cleanly closed run")
    run_reopen.add_argument("--by", required=True)
    run_reopen.add_argument("--reason", required=True)
    run_reopen.add_argument("--expected-updated-at")

    adv = sub.add_parser("advance")
    adv.add_argument("phase", type=int)
    adv.add_argument("progress", type=int, nargs="?")
    adv.add_argument("--status", default=None)
    adv.add_argument("--implemented-by", default=None)
    adv.add_argument("--design-round", type=int, default=None,
                     help="explicitly set design_round to this value (override; use --new-design-round "
                     "for normal organic tracking)")
    adv.add_argument("--new-design-round", action="store_true",
                     help="increment design_round by 1 from its current on-disk value and record a "
                     "distinct design_round_advanced event; only valid when advancing to phase 2 "
                     "(Design debate); mutually exclusive with --design-round")
    adv.add_argument("--design-round-reason", default=None,
                     help="optional short reason/what triggered the new design round; only valid "
                     "together with --new-design-round")
    adv.add_argument("--review-round", type=int, default=None)
    adv.add_argument("--dry-run", action="store_true")
    adv.add_argument("--next-action", default=None,
                     help="override the phase-appropriate default next_action message")
    adv.add_argument("--authorization-hold", choices=("design_review",), default=None,
                     help="tag an explicit Phase-2 blocked wait for reviewer authorization")

    gate = sub.add_parser("deployment-gate")
    gate.add_argument("--approve", action="store_true")
    gate.add_argument("--auto", action="store_true",
                      help="#102: with --approve, re-apply a prior approval recorded for this exact acceptance hash")
    gate.add_argument("--revoke", action="store_true")
    gate.add_argument("--by", default=None)
    gate.add_argument("--reason", default=None)

    monitor = sub.add_parser("monitor-poll", help="model-free durable monitor poll")
    monitor.add_argument("--owner", required=True)
    monitor.add_argument("--lease-seconds", type=int, default=30)

    evidence_refresh = sub.add_parser("evidence-refresh-plan", help="conservatively assess evidence drift")
    evidence_refresh.add_argument("--map", required=True)
    evidence_refresh.add_argument("--subject", required=True)
    evidence_refresh.add_argument("--changes", required=True)
    evidence_refresh.add_argument("--hashes", required=True)
    evidence_refresh.add_argument("--input-hash", required=True)
    evidence_refresh.add_argument("--regenerated")

    sub.add_parser("performance-status", help="refresh and print the 90/120-minute run state")
    performance_watch = sub.add_parser(
        "performance-watch",
        help="run the clock that evaluates the 90/120-minute deadline without a CLI call")
    performance_watch.add_argument("--interval", type=int, default=PERFORMANCE_TICK_SECONDS)
    performance_watch.add_argument("--once", action="store_true",
                                   help="evaluate exactly once and exit")
    performance_watch.add_argument("--max-ticks", type=int, default=None,
                                   help="stop after this many evaluations")
    performance_resume = sub.add_parser("performance-resume", help="open a new episode after explicit reevaluation")
    performance_resume.add_argument("--by", required=True)
    performance_resume.add_argument("--reason", required=True)
    auto_resume = sub.add_parser(
        "performance-auto-resume",
        help="#414: record the Pilot's standing decision ([performance] auto_resume) that each later "
             "performance pause resumes by itself; --off withdraws it")
    auto_resume.add_argument("--by", required=True)
    auto_resume.add_argument("--reason", required=True)
    auto_resume.add_argument("--off", action="store_true",
                             help="withdraw the standing decision; later pauses wait for performance-resume")
    performance_resume.add_argument(
        "--evidence-hash", default=None,
        help="optional: sha256 of the paused episode this resume answers; the engine "
             "computes it, and a value that does not match is refused with the expected one")

    return p


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "init": cmd_init, "status": cmd_status, "validate": cmd_validate,
        "advance": cmd_advance, "deployment-gate": cmd_deployment_gate,
        "verify": cmd_verify, "verify-log": cmd_verify_log, "doctor": cmd_doctor,
        "release-plan": cmd_release_plan,
        "release-reconcile": cmd_release_reconcile,
        "regression-request": cmd_regression_request,
        "regression-decide": cmd_regression_decide,
        "regression-run": cmd_regression_run,
        "regression-cancel": cmd_regression_cancel,
        "regression-finalize": cmd_regression_finalize,
        "work-items-sync": cmd_work_items_sync,
        "work-item-activate": cmd_work_item_activate,
        "work-item-update": cmd_work_item_update,
        "work-item-remove": cmd_work_item_remove,
        "lane-request": cmd_lane_request,
        "lane-confirm": cmd_lane_confirm,
        "plan-tranche": cmd_plan_tranche,
        "tranche-approve": cmd_tranche_approve,
        "dashboard": cmd_dashboard,
        "record-evidence": cmd_record_evidence,
        "mutation-proof": cmd_mutation_proof,
        "workflow-check": cmd_workflow_check,
        "qa-report": cmd_qa_report,
        "record-symptom-resolved": cmd_record_symptom,
        "design-approve": cmd_design_approve,
        "design-reject": cmd_design_reject,
        "record-design-review": cmd_record_design_review,
        "design-review-packet": cmd_design_review_packet,
        "design-propose": cmd_design_propose,
        "design-review-authorize": cmd_design_review_authorize,
        "design-review-escalate": cmd_design_review_escalate,
        "record-review": cmd_record_review,
        "review-attempt-start": cmd_review_attempt_start,
        "record-review-findings": cmd_record_review_findings,
        "session-result-adopt": cmd_session_result_adopt,
        "implementer-apply": cmd_implementer_apply,
        "implementer-discard": cmd_implementer_discard,
        "review-cap-override": cmd_review_cap_override,
        "recover": cmd_recover,
        "watch": cmd_watch,
        "recovery-acknowledge": cmd_recovery_acknowledge,
        "verify-live": cmd_verify_live,
        "heartbeat": cmd_heartbeat,
        "background-wait-start": cmd_background_wait_start,
        "background-wait-end": cmd_background_wait_end,
        "human-pause-start": cmd_human_pause_start,
        "human-pause-end": cmd_human_pause_end,
        "design-timing": cmd_design_timing,
        "design-evidence": cmd_design_evidence,
        "criterion-update": cmd_criterion_update,
        "criterion-add": cmd_criterion_add,
        "criterion-remove": cmd_criterion_remove,
        "criteria-apply": cmd_criteria_apply,
        "amendment-open": cmd_amendment_open,
        "amendment-revise": cmd_amendment_revise,
        "amendment-review": cmd_amendment_review,
        "amendment-approve": cmd_amendment_approve,
        "amendment-escalate": cmd_amendment_escalate,
        "question-raise": cmd_question_raise,
        "question-answer": cmd_question_answer,
        "analyze-archives": cmd_analyze_archives,
        "pilot-note": cmd_pilot_note,
        "shadow-apply": cmd_shadow_apply,
        "shadow-route": cmd_shadow_route,
        "evidence-routing-activate": cmd_evidence_routing_activate,
        "ci-watch": cmd_ci_watch,
        "design-decline": cmd_design_decline,
        "run-close": cmd_run_close,
        "config-override": cmd_config_override,
        "run-reopen": cmd_run_reopen,
        "monitor-poll": cmd_monitor_poll,
        "evidence-refresh-plan": cmd_evidence_refresh_plan,
        "implementation-review-packet": cmd_implementation_review_packet,
        "performance-status": cmd_performance_status,
        "performance-watch": cmd_performance_watch,
        "performance-resume": cmd_performance_resume,
        "performance-auto-resume": cmd_performance_auto_resume,
    }
    # #378/#379: pure readers of a record or report never touch the run, so
    # they skip the performance gate, whose clock refresh writes state and
    # which refuses work once a run is paused.
    pure_reader = args.command in {"shadow-route", "implementation-review-packet"} or (
        args.command == "release-reconcile" and getattr(args, "inspect", False))
    try:
        if args.command != "init" and not pure_reader:
            root = lib.resolve_root(args.root)
            refusal = performance_mutation_refusal(root, args.command)
            if refusal:
                raise lib.HandsoffError(refusal)
        return handlers[args.command](args)
    except lib.HandsoffError as e:
        print(f"SHIP_FEATURE_BLOCKED: {e}")
        return 1
    except Exception as e:  # last-resort: a clean refusal beats a raw traceback
        print(f"SHIP_FEATURE_BLOCKED: unexpected error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
