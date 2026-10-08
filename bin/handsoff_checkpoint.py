#!/usr/bin/env python3
"""P1.6: durable handover checkpoints for a managed implementer.

A checkpoint is what a session hands over: the files it changed, the
commands it ran, what it says it finished, what remains and what blocks
it. The implementer prints `HANDSOFF_CHECKPOINT:` lines; the runtime
writes one at launch (owned paths, work items, criteria) and one when the
session ends on a budget, timeout or runtime failure. A session keeps its
checkpoints latest first, at most MAX_CHECKPOINT_HISTORY.

A checkpoint is the previous session's account, never evidence: the
replacement's unfinished criteria come from the current acceptance, and
`completed_criteria` is shown only as a claim.

Layer: core -> config -> schema -> ledger -> here. handsoff_lib imports
this module, so nothing here imports handsoff_lib.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from handsoff_config import load_config
from handsoff_core import HandsoffError, acceptance_path, load_unique_json, project_lock, status_path
from handsoff_ledger import commit, item_criteria
from handsoff_schema import (
    CHECKPOINT_SOURCES,
    MAX_CHECKPOINT_COMMANDS,
    MAX_CHECKPOINT_CRITERIA,
    MAX_CHECKPOINT_FILES,
    MAX_CHECKPOINT_HISTORY,
    MAX_CHECKPOINT_NOTES,
    MAX_CHECKPOINT_PATH,
    MAX_CHECKPOINT_TEXT,
    PROGRESS_CRITERION_PATTERN,
    validate_checkpoint_line,
    validate_status_schema,
)

CHECKPOINT_PREFIX = "HANDSOFF_CHECKPOINT:"
RESUME_HEADING = "# Resume from checkpoint"


def parse_checkpoint_line(line: str) -> dict | None:
    """The validated checkpoint a `HANDSOFF_CHECKPOINT:` line carries, or
    None for any other line and for a malformed one."""
    if not isinstance(line, str) or not line.startswith(CHECKPOINT_PREFIX):
        return None
    try:
        return validate_checkpoint_line(json.loads(line[len(CHECKPOINT_PREFIX):].strip()))
    except (ValueError, TypeError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object) -> str:
    return str(value or "").replace("\x00", "")[:MAX_CHECKPOINT_TEXT]


def push_checkpoint(session: dict, checkpoint: dict, source: str, at: str | None = None) -> dict:
    """Put a checkpoint first on SESSION (in place), keeping the history
    bounded; returns the stored record."""
    if source not in CHECKPOINT_SOURCES:
        raise HandsoffError("checkpoint source is invalid")
    record = {**deepcopy(checkpoint), "source": source, "at": at or _now()}
    history = [record, *(session.get("checkpoints") or [])]
    session["checkpoints"] = history[:MAX_CHECKPOINT_HISTORY]
    return record


def checkpoint_event(session_id: str, record: dict) -> dict:
    """The `checkpoint_recorded` event for an extra_events list."""
    return {"kind": "checkpoint_recorded",
            "message": f"Implementer checkpoint recorded ({record['source']})",
            "session_id": session_id, "source": record["source"],
            "files_changed": len(record["files_changed"]),
            "completed_criteria": list(record["completed_criteria"])}


def record_session_checkpoint(root: Path, session_id: str, checkpoint: dict, *, source: str = "agent") -> dict:
    """Store one validated checkpoint on an implementer session under the
    project lock, with a `checkpoint_recorded` event; returns the record."""
    root = Path(root).resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict) or session.get("role") != "implementer":
            raise HandsoffError("checkpoint session is not an implementer session")
        proposed = deepcopy(status)
        record = push_checkpoint(proposed["agent_sessions"][session_id], checkpoint, source)
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        event = checkpoint_event(session_id, record)
        commit(root, cfg, status=proposed, event_kind=event.pop("kind"), event_message=event.pop("message"),
               **event)
        return deepcopy(record)


def session_criteria(acceptance: dict, work_items: list[str] | None) -> list[str]:
    """The criterion ids a session works on: those of its work items, or
    every criterion when it was launched for none."""
    criteria = acceptance.get("criteria", []) if isinstance(acceptance, dict) else []
    if work_items:
        own = [c for item_id in work_items for c in item_criteria(acceptance, item_id)]
    else:
        own = list(criteria)
    ids = []
    for criterion in own:
        cid = criterion.get("id") if isinstance(criterion, dict) else None
        if isinstance(cid, str) and cid not in ids:
            ids.append(cid)
    return ids


def launch_checkpoint(acceptance: dict, owned_paths: list[str] | None, work_items: list[str] | None) -> dict:
    """The checkpoint the runtime writes at launch."""
    criteria = [cid for cid in session_criteria(acceptance, work_items)
                if PROGRESS_CRITERION_PATTERN.fullmatch(cid)][:MAX_CHECKPOINT_CRITERIA]
    return {"files_changed": [], "completed_criteria": [], "commands_run": [],
            "remaining": [], "blockers": [], "provider_state": "",
            "owned_paths": list(owned_paths or [])[:MAX_CHECKPOINT_FILES],
            "work_items": list(work_items or []), "criteria": criteria}


def _unique(values) -> list:
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def failure_checkpoint(session: dict, failure: dict, summary: dict | None) -> dict:
    """The checkpoint the runtime writes when a session ends on a failure:
    its progress records and the paths it changed, carrying forward what
    the session's own latest checkpoint already said."""
    prior = next((item for item in session.get("checkpoints") or []
                  if isinstance(item, dict) and item.get("source") == "agent"), {})
    done, partial, untouched = ((summary or {}).get(key, []) for key in ("done", "partial", "untouched"))
    progress = [item for item in session.get("progress") or [] if isinstance(item, dict)]
    blockers = [*prior.get("blockers", []),
                *(_text(f"{item.get('criterion')}: {item.get('note') or item.get('test') or 'scope exception'}")
                  for item in progress if item.get("state") == "scope_exception")]
    commands = list(prior.get("commands_run", []))
    for item in progress:
        test = item.get("test")
        if item.get("state") == "done" and isinstance(test, str) and test \
                and all(c["command"] != test[:MAX_CHECKPOINT_PATH] for c in commands):
            commands.append({"command": test[:MAX_CHECKPOINT_PATH], "exit_code": 0})  # a done claim names a green test
    paths = [p for p in (failure.get("changed_paths") or [])
             if isinstance(p, str) and 0 < len(p) <= MAX_CHECKPOINT_PATH]
    completed = [c for c in _unique([*prior.get("completed_criteria", []), *done])
                 if PROGRESS_CRITERION_PATTERN.fullmatch(str(c))]
    remaining = [_text(c) for c in [*partial, *untouched]] or list(prior.get("remaining", []))
    return {"files_changed": _unique([*paths, *prior.get("files_changed", [])])[:MAX_CHECKPOINT_FILES],
            "completed_criteria": completed[:MAX_CHECKPOINT_CRITERIA],
            "commands_run": commands[:MAX_CHECKPOINT_COMMANDS],
            "remaining": remaining[:MAX_CHECKPOINT_NOTES],
            "blockers": _unique(blockers)[:MAX_CHECKPOINT_NOTES],
            "provider_state": _text(f"{failure.get('category')}: {failure.get('reason') or ''}".strip())}


def latest_checkpoint(status: dict, session_id: str) -> dict | None:
    """The session's most recent checkpoint, or None."""
    session = ((status or {}).get("agent_sessions") or {}).get(session_id) if isinstance(status, dict) else None
    history = session.get("checkpoints") if isinstance(session, dict) else None
    if not isinstance(history, list) or not history or not isinstance(history[0], dict):
        return None
    return deepcopy(history[0])


def unfinished_criteria(acceptance: dict, work_items: list[str] | None) -> list[tuple[str, str]]:
    """(id, state) of every criterion of the session's items that is not
    passing in ACCEPTANCE now. Only the ledger-backed acceptance decides;
    a checkpoint's completed_criteria never does."""
    states = {c.get("id"): c.get("state") for c in acceptance.get("criteria", []) if isinstance(c, dict)}
    return [(cid, str(states.get(cid))) for cid in session_criteria(acceptance, work_items)
            if states.get(cid) != "passing"]


def resume_section(checkpoint: dict | None, acceptance: dict, work_items: list[str] | None,
                   from_session_id: str | None = None) -> str:
    """The replacement's `# Resume from checkpoint` section; empty without
    a checkpoint."""
    if not isinstance(checkpoint, dict):
        return ""
    unfinished = unfinished_criteria(acceptance, work_items)
    passing = [cid for cid in session_criteria(acceptance, work_items)
               if cid not in {item[0] for item in unfinished}]
    who = f"session {from_session_id}" if from_session_id else "session"
    lines = [RESUME_HEADING, "",
             f"The previous {who} left a checkpoint (source: {checkpoint.get('source')}) at {checkpoint.get('at')}.",
             "", "Unfinished criteria (from the current acceptance, not from the checkpoint):"]
    lines += [f"- {cid} ({state})" for cid, state in unfinished] or ["- none"]
    lines += ["", "Criteria the ledger shows passing: " + (", ".join(passing) or "none")
              + ". Do not redo them."]
    claimed = checkpoint.get("completed_criteria") or []
    if claimed:
        lines.append("The previous session claimed complete (its claim, not evidence): " + ", ".join(claimed) + ".")
    files = checkpoint.get("files_changed") or []
    if files:
        lines += ["", "Files already changed:"] + [f"- {path}" for path in files]
    commands = checkpoint.get("commands_run") or []
    if commands:
        lines += ["", "Commands already run:"] + [f"- {item['command']} (exit {item['exit_code']})"
                                                  for item in commands]
    for label, key in (("Remaining, as it said", "remaining"), ("Blockers", "blockers")):
        if checkpoint.get(key):
            lines += ["", f"{label}:"] + [f"- {item}" for item in checkpoint[key]]
    if checkpoint.get("provider_state"):
        lines += ["", f"Provider state: {checkpoint['provider_state']}"]
    return "\n".join(lines)


def replacement_resume_section(root: Path, handoff: dict) -> str:
    """The section for a replacement launch: the handoff's checkpoint (or the
    source session's latest when the handoff predates the field), judged
    against the current acceptance and the source session's work items."""
    root = Path(root)
    cfg = load_config(root)
    status = load_unique_json(status_path(root, cfg))
    acceptance = load_unique_json(acceptance_path(root, cfg))
    source_id = handoff.get("from_session_id")
    checkpoint = handoff["checkpoint"] if "checkpoint" in handoff else latest_checkpoint(status, source_id)
    session = (status.get("agent_sessions") or {}).get(source_id) or {}
    return resume_section(checkpoint, acceptance, session.get("work_items"), source_id)
