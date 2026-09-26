#!/usr/bin/env python3
"""Routing evidence: what actually happened the last N times we routed this.

#300, the foundation of the #299 epic. A versioned, content-addressed
PROJECTION over the completed-run archives, session records and event log.
Not a second ledger: a parallel source of truth would add reconciliation and
integrity risk, and the epic forbids it. Nothing here writes engine state, and
nothing here changes a gate.

Three rules the epic states and this module holds to literally:

**Process completion is not success.** A session that consumes its budget and
returns no structured verdict is NEGATIVE evidence even when a host later
repairs the workflow. The #290 reviewers are the reference case: both sessions
"completed" as processes and produced nothing.

**Missing usage stays UNKNOWN.** Never zero, never estimated. Real archives
carry `{"source": "adapter", "tokens_in": null, "tokens_out": null,
"tokens_total": 16928}`, so a projection that defaulted the nulls to 0 would
report a session that used 16,928 tokens as having used none on input and
output, and every tokens-per-outcome aggregate downstream would be wrong.

**A correction is a new projection.** `DERIVATION_VERSION` is part of every
record and every record hash, so fixing a derivation produces new records
beside the old ones rather than rewriting evidence that a decision may already
have been made on.

Layer: core -> config -> here. It reads archives; it never reads a live run.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from handsoff_core import HandsoffError, _canonical
from handsoff_config import classify_archive_record

#: Bumped whenever a derivation below changes meaning. Part of every record and
#: every record hash, so a corrected projection never silently replaces the
#: evidence an earlier decision was made on.
DERIVATION_VERSION = 1

#: The one sentinel for absent data. A string rather than None so it survives
#: JSON, grouping and equality without colliding with a real zero.
UNKNOWN = "UNKNOWN"

#: Normalized task classes. Closed, because #301 keys every cohort by this and
#: an open set means cohorts that cannot be compared.
TASK_CLASSES = ("security", "bug", "performance", "feature", "refactor",
                "test", "docs", "chore", UNKNOWN)

#: GitHub label slug -> task class, against the slugs `_normalize_label`
#: already produces (casefolded, `type:`/`kind:`/`area:` stripped, hyphenated).
LABEL_TASK_CLASSES = {
    "security": "security", "vulnerability": "security",
    "bug": "bug", "defect": "bug", "fix": "bug", "regression": "bug", "crash": "bug",
    "performance": "performance", "perf": "performance",
    "feature": "feature", "enhancement": "feature", "feat": "feature",
    "refactor": "refactor", "refactoring": "refactor", "architecture": "refactor",
    "maintainability": "refactor", "tech-debt": "refactor",
    "test": "test", "testing": "test", "tests": "test",
    "docs": "docs", "documentation": "docs",
    "chore": "chore", "ci": "chore", "build": "chore", "dependencies": "chore",
    "infrastructure": "chore",
}

#: Precedence when a run's labels map to several classes. Deterministic by
#: construction: TASK_CLASSES is ordered by how much the class constrains how
#: the work must be done, most first, so the same label set always resolves the
#: same way on any machine.
TASK_CLASS_PRECEDENCE = {name: index for index, name in enumerate(TASK_CLASSES)}

#: Outcomes, most authoritative first. `verified_phase8` is the only one that
#: means a user-visible result was proven where users meet it.
ROUTING_OUTCOMES = (
    "verified_phase8",
    "adopted_not_verified",
    "review_changes_requested",
    "review_unsupported",
    "regression_failed",
    "verification_failed",
    "budget_exhausted",
    "ceiling_overrun",
    "protocol_no_result",
    "replaced",
    "failed_other",
    UNKNOWN,
)

#: Outcomes that are negative routing evidence, named rather than inferred from
#: position so adding an outcome cannot silently change the sign of an old one.
NEGATIVE_OUTCOMES = frozenset({
    "review_changes_requested", "review_unsupported", "regression_failed",
    "verification_failed", "budget_exhausted", "ceiling_overrun",
    "protocol_no_result", "replaced", "failed_other",
})

#: Why a record may not be trusted at face value. Reported, never dropped.
QUALITY_FLAGS = (
    "usage_not_reported",        # the adapter reported no token usage at all
    "usage_partial",             # a total without the input/output split
    "run_incomplete",            # the run never reached Phase 8 complete
    "task_class_unknown",        # no labels were recorded for the work items
    "risk_class_absent",         # pre-dates risk-class routing
    "duration_unavailable",      # timestamps missing or unparseable
    "legacy_archive",            # written before run_kind existed
)

_LABEL_PREFIX = re.compile(r"^(?:type|kind|area)\s*:\s*")
_SLUG = re.compile(r"[^a-z0-9]+")


def normalize_label(value: object) -> str:
    """The same normalization `handsoff_tranche` applies, reproduced here so a
    projection of an old archive does not depend on the tranche module's
    current behaviour."""
    text = str(value.get("name") if isinstance(value, dict) else value).strip().casefold()
    return _SLUG.sub("-", _LABEL_PREFIX.sub("", text)).strip("-")[:64]


def task_class_for_labels(labels: object) -> str:
    """The most constraining class the labels map to, or UNKNOWN.

    A label the map does not know contributes nothing: guessing from an
    unrecognized slug is how a cohort ends up comparing unlike work.
    """
    if not isinstance(labels, (list, tuple, set)):
        return UNKNOWN
    classes = {LABEL_TASK_CLASSES.get(normalize_label(value)) for value in labels}
    classes.discard(None)
    if not classes:
        return UNKNOWN
    return min(classes, key=lambda name: TASK_CLASS_PRECEDENCE[name])


def recorded_work_item_labels(status: dict) -> list[str]:
    """Every label the run recorded for its work items.

    Two sources, both already in the archive: the per-item labels a tranche
    approval records, and a `github_labels` field on a work item. Neither is
    present in any archive written before #300, which is why `task_class` reads
    UNKNOWN for historical evidence rather than being inferred from the title.
    """
    labels: list[str] = []
    approval = status.get("tranche_approval") if isinstance(status, dict) else None
    if isinstance(approval, dict) and isinstance(approval.get("labels"), dict):
        for values in approval["labels"].values():
            if isinstance(values, (list, tuple)):
                labels += [str(value) for value in values]
    return labels


def _int_or_unknown(value: object) -> int | str:
    """A real integer, or UNKNOWN. Never a substituted zero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return UNKNOWN
    return value


def session_usage(session: dict) -> dict:
    """Tokens as reported, with absence preserved."""
    usage = session.get("usage") if isinstance(session, dict) else None
    if not isinstance(usage, dict) or usage.get("source") != "adapter":
        return {"tokens_in": UNKNOWN, "tokens_out": UNKNOWN, "tokens_total": UNKNOWN,
                "source": str(usage.get("source")) if isinstance(usage, dict) else UNKNOWN}
    return {"tokens_in": _int_or_unknown(usage.get("tokens_in")),
            "tokens_out": _int_or_unknown(usage.get("tokens_out")),
            "tokens_total": _int_or_unknown(usage.get("tokens_total")),
            "source": "adapter"}


def _parse(value: object):
    from datetime import datetime
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def session_duration_ms(session: dict) -> dict:
    """Wall clock from start, and active time from the running transition.

    Two numbers because they answer different questions: wall clock includes
    the wait for an adapter to begin, active time does not, and a router
    comparing candidates cares about the second.
    """
    started, running, ended = (_parse(session.get(k)) for k in ("started_at", "running_at", "ended_at"))
    wall = round((ended - started).total_seconds() * 1000) if started and ended else UNKNOWN
    active = round((ended - running).total_seconds() * 1000) if running and ended else UNKNOWN
    return {"wall_clock_ms": wall if wall == UNKNOWN or wall >= 0 else UNKNOWN,
            "active_ms": active if active == UNKNOWN or active >= 0 else UNKNOWN}


def session_outcome(status: dict, session: dict) -> str:
    """What this session actually produced, by the epic's taxonomy.

    Read in order of authority. A structured result that was adopted and whose
    run reached Phase 8 is the only `verified_phase8`; a session that spent its
    budget and returned nothing is `protocol_no_result` whatever its exit code,
    because a process that exits 0 having produced no verdict is a cost with no
    result.
    """
    sid = session.get("session_id")
    failures = status.get("agent_failures") if isinstance(status.get("agent_failures"), dict) else {}
    failure = failures.get(sid) if isinstance(failures.get(sid), dict) else {}
    category = failure.get("category")
    result = session.get("result") if isinstance(session.get("result"), dict) else None
    payload = result.get("payload") if isinstance(result, dict) and isinstance(result.get("payload"), dict) else {}

    replacements = status.get("agent_replacements")
    if isinstance(replacements, list) and any(
            isinstance(item, dict) and item.get("from_session_id") == sid for item in replacements):
        return "replaced"
    if category in {"token_budget_exhaustion"}:
        return "budget_exhausted"
    if isinstance(failure.get("ceiling_overshoot_tokens"), int) and failure["ceiling_overshoot_tokens"] > 0:
        return "ceiling_overrun"
    if category == "orchestration_noop" or (session.get("state") == "completed" and result is None):
        return "protocol_no_result"
    if category and category not in {"still_running"}:
        return "failed_other"
    decision = payload.get("decision")
    if decision == "changes_requested":
        return "review_changes_requested"
    if payload.get("tests_executed") == "no" and decision == "approved":
        # An approval whose reviewer ran no tests is not a supported verdict.
        return "review_unsupported"
    if decision in {"approved", "accepted"} or result is not None:
        complete = status.get("status") == "complete" and int(status.get("phase_number", 0) or 0) >= 8
        return "verified_phase8" if complete else "adopted_not_verified"
    if session.get("state") in {"failed", "cancelled"}:
        return "failed_other"
    return UNKNOWN


def project_archive(archive: dict, *, source_sha256: str, source_name: str) -> list[dict]:
    """One evidence record per managed session in one archived run.

    The session is the unit because routing decides per session: a run mixing a
    premium architect with a fast reviewer carries evidence about both, and
    collapsing it to the run would attribute one's outcome to the other.
    """
    if not isinstance(archive, dict):
        raise HandsoffError("routing evidence: an archive record must be an object")
    status = archive.get("status") if isinstance(archive.get("status"), dict) else {}
    sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}
    labels = recorded_work_item_labels(status)
    task_class = task_class_for_labels(labels)
    risk_class = status.get("risk_class")
    complete = status.get("status") == "complete" and int(status.get("phase_number", 0) or 0) >= 8
    engine = (status.get("engine") or {}).get("version") if isinstance(status.get("engine"), dict) else None

    records = []
    for sid, session in sorted(sessions.items()):
        if not isinstance(session, dict):
            continue
        usage = session_usage(session)
        duration = session_duration_ms(session)
        routing = session.get("adaptive_routing") if isinstance(session.get("adaptive_routing"), dict) else {}
        flags = []
        if usage["tokens_total"] == UNKNOWN:
            flags.append("usage_not_reported")
        elif usage["tokens_in"] == UNKNOWN or usage["tokens_out"] == UNKNOWN:
            flags.append("usage_partial")
        if not complete:
            flags.append("run_incomplete")
        if task_class == UNKNOWN:
            flags.append("task_class_unknown")
        if not risk_class:
            flags.append("risk_class_absent")
        if duration["wall_clock_ms"] == UNKNOWN:
            flags.append("duration_unavailable")
        if archive.get("run_kind") not in ("test", "product"):
            flags.append("legacy_archive")

        record = {
            "derivation_version": DERIVATION_VERSION,
            "source_archive": source_name,
            "source_sha256": source_sha256,
            "repository": archive.get("repo") or UNKNOWN,
            "engine_version": engine or UNKNOWN,
            "session_id": sid,
            "role": session.get("role") or UNKNOWN,
            "task_class": task_class,
            "risk_class": risk_class or UNKNOWN,
            "phase": _int_or_unknown(session.get("phase_number")),
            "adapter": session.get("adapter") or UNKNOWN,
            "requested_model": session.get("requested_model") or UNKNOWN,
            "reported_model": session.get("reported_model") or UNKNOWN,
            "tier": routing.get("tier") or session.get("tier") or UNKNOWN,
            "routed": bool(routing),
            "tokens": usage,
            "duration": duration,
            "retries": _retry_count(status, sid),
            "replacements": _replacement_count(status, sid),
            "outcome": session_outcome(status, session),
            "policy_version": (status.get("model_policy") or {}).get("version", UNKNOWN)
                              if isinstance(status.get("model_policy"), dict) else UNKNOWN,
            "quality_flags": sorted(set(flags)),
        }
        record["negative"] = record["outcome"] in NEGATIVE_OUTCOMES
        record["record_hash"] = record_hash(record)
        records.append(record)
    return records


def _retry_count(status: dict, session_id: object) -> int:
    attempts = status.get("review_attempts")
    if not isinstance(attempts, list):
        return 0
    return sum(1 for item in attempts
               if isinstance(item, dict) and session_id in (item.get("session_ids") or []))


def _replacement_count(status: dict, session_id: object) -> int:
    replacements = status.get("agent_replacements")
    if not isinstance(replacements, list):
        return 0
    return sum(1 for item in replacements
               if isinstance(item, dict) and item.get("from_session_id") == session_id)


def record_hash(record: dict) -> str:
    """Content address over everything but the hash field itself, so a record
    cannot be edited and keep its identity, and no caller-supplied flag can
    make an altered record look original."""
    body = {k: v for k, v in record.items() if k != "record_hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def validate_record(record: object) -> list[str]:
    """Every error at once, so a malformed projection reports all of them."""
    if not isinstance(record, dict):
        return ["evidence record: top-level value must be an object"]
    errors = []
    required = {"derivation_version", "source_archive", "source_sha256", "repository",
                "engine_version", "session_id", "role", "task_class", "risk_class", "phase",
                "adapter", "requested_model", "reported_model", "tier", "routed", "tokens",
                "duration", "retries", "replacements", "outcome", "policy_version",
                "quality_flags", "negative", "record_hash"}
    missing = sorted(required - set(record))
    errors += [f"evidence record: missing '{name}'" for name in missing]
    if record.get("task_class") not in TASK_CLASSES:
        errors.append(f"evidence record: task_class {record.get('task_class')!r} is not in the closed set")
    if record.get("outcome") not in ROUTING_OUTCOMES:
        errors.append(f"evidence record: outcome {record.get('outcome')!r} is not in the closed set")
    for flag in record.get("quality_flags") or []:
        if flag not in QUALITY_FLAGS:
            errors.append(f"evidence record: quality flag {flag!r} is not in the closed set")
    if not missing and record.get("record_hash") != record_hash(record):
        errors.append("evidence record: record_hash does not match the record; it was edited after projection")
    if record.get("negative") is not (record.get("outcome") in NEGATIVE_OUTCOMES):
        errors.append("evidence record: 'negative' disagrees with the outcome taxonomy")
    return errors


def project_archive_file(path: Path) -> list[dict]:
    """Project one archive file, binding every record to its bytes."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        archive = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HandsoffError(f"routing evidence: {path.name} is not valid JSON: {exc}") from exc
    return project_archive(archive, source_sha256=hashlib.sha256(raw).hexdigest(),
                           source_name=path.name)


def project_archive_dir(directory: Path, *, include_test_runs: bool = False) -> dict:
    """Project every archive in a directory.

    Test runs are excluded by `run_kind`, the field `archive_run` writes for
    exactly this purpose, and the count of what was skipped is returned rather
    than discarded: a cohort that silently dropped half its sources is not a
    cohort anyone can reason about.
    """
    directory = Path(directory)
    records, skipped, unreadable = [], {"test_run": 0, "no_sessions": 0}, []
    for path in sorted(directory.glob("*.json")):
        try:
            archive = json.loads(path.read_bytes())
        except (json.JSONDecodeError, OSError) as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        if not isinstance(archive, dict):
            unreadable.append(f"{path.name}: top-level value is not an object")
            continue
        # The ONE rule (#317), not a private `run_kind == "test"`: that
        # comparison misses every archive written before run_kind existed,
        # whose kind is decided by the repo-name prefix instead.
        if classify_archive_record(archive, path.name) == "test" and not include_test_runs:
            skipped["test_run"] += 1
            continue
        produced = project_archive_file(path)
        if not produced:
            skipped["no_sessions"] += 1
        records += produced
    return {"derivation_version": DERIVATION_VERSION, "records": records,
            "skipped": skipped, "unreadable": unreadable}
