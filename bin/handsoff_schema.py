#!/usr/bin/env python3
"""Handsoff schema validation: is this state well formed at all.

#284 criterion 3. One hundred and eight symbols, lifted out of the agent
runtime and placed BELOW the ledger so that `commit` can call them. That is
the whole point of the move: while the validators sat above the ledger, only a
caller could validate, and criterion 3 -- state transitions validate proposed
state before persistence -- was a habit that 16 of 71 status-committing
functions did not have. Below the ledger it is structural, and there is no
17th.

Nothing here reads or writes the tree. Every function takes a proposed
document and returns a list of human-readable errors, empty when it is well
formed, so a caller can report all of them at once rather than the first.

Layer: core -> routing -> config -> here -> ledger. Every import is module
level.
"""
from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from datetime import datetime

from handsoff_core import HandsoffError, _canonical
from handsoff_config import (
    MAX_WORK_ITEMS, SELECTABLE_AGENT_ADAPTERS, SELECTABLE_AGENT_ROLES,
    VERIFICATION_REQUIREMENTS, validate_agent_model, validate_model_policy,
)
from handsoff_routing import classify_adaptive_risk, validate_session_adaptive_routing



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


RUN_LANES = {"full", "design", "review"}


OPERATION_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


# #36: delta review packets. A recorded review may carry bounded findings;
# every record appends a bounded history entry; a packet is the sorted,
# canonical, size-capped delta a follow-up reviewer receives instead of
# the full task.
MAX_DESIGN_REVIEW_FINDINGS = 32


MAX_DESIGN_REVIEW_FINDING_LENGTH = 512


MAX_DESIGN_REVIEW_HISTORY = 8


DESIGN_REVIEW_DISPOSITIONS = ("resolved", "rejected", "unresolved")


DESIGN_REVIEW_FINDING_ID_PATTERN = re.compile(r"^F[1-9][0-9]*\.[1-9][0-9]*$")


DESIGN_REVIEW_PACKET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


DESIGN_REVIEW_HISTORY_FIELDS = (
    "attempt", "decision", "by", "design_hash", "head", "criteria_ids", "criterion_hashes",
    "structural_blocker", "findings",
)


#: Where a role's adapter or model came from: named in handsoff.toml
#: ("explicit"), taken from RECOMMENDED_CREW because the key was absent or
#: the placeholder ("recommended"), or the runner default because the
#: adapter was overridden but no model was named, so the recommended model
#: for the other adapter would be the wrong thing to pass ("runner_default").
PROFILE_SOURCES = ("explicit", "recommended", "runner_default")


DESIGN_REVIEWER_TIERS = ("primary", "followup")


#: Selection precedence, evaluated in this order; the first match is the
#: reason. Only `delta_check` selects the follow-up tier.
DESIGN_REVIEWER_SELECTION_REASONS = (
    "first_review", "no_followup_configured", "pilot_escalation",
    "structural_blocker", "criteria_structure_changed", "delta_check",
)


DESIGN_REVIEWER_ESCALATION_FIELDS = ("by", "at", "note", "consumed_at")


DESIGN_REVIEWER_PROFILE_FIELDS = ("adapter", "model", "tier", "reason")


WORK_ITEM_KINDS = {"issue", "ask"}


WORK_ITEM_ID_PATTERN = re.compile(r"^(?:issue-[1-9][0-9]{0,8}|ask-[a-z0-9][a-z0-9-]{0,39}|unattributed)$")


WORK_ITEM_LANES = ("full", "small-fix", "escalated")


#: #290: why a bounded session ended, kept distinct because the repairs
#: differ. The reviewer failures on 2026-09-23 were recorded only as
#: "token_budget_exhaustion", which cannot tell a model that ignored its
#: scope from a wrapper that stopped it.
BUDGET_FAILURE_CAUSES = ("model_noncompliance", "wrapper_overrun",
                         "shared_budget_exhaustion", "missing_protocol")


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
    "configured", "recommended", "auto_detected", "legacy_auto_detected", "fallback", "adaptive",
}


# #36: packet_id and design_hash are written on every new session (null
# unless a Phase-2 reviewer was launched with a delta packet) but stay
# OPTIONAL on read, so a status.json written before they existed is still
# valid; when present they must be null or a non-empty string. #37 adds
# `tier` the same way: null unless a Phase-2 reviewer was launched through
# the tiered selection, otherwise exactly "primary" or "followup".
AGENT_SESSION_OPTIONAL_FIELDS = {"packet_id", "design_hash", "tier", "phase_number", "result", "host_session_id", "usage",
                                 "adaptive_routing",
                                 "budget_decision", "reviewer_isolation", "amendment_id", "progress"}  # #215: per-criterion progress the Implementer reported


#: #215: one HANDSOFF_PROGRESS line per criterion the Implementer finished or abandoned
PROGRESS_STATES = ("done", "partial", "untouched")


PROGRESS_CRITERION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


MAX_PROGRESS_RECORDS = 64


MAX_PROGRESS_NOTE = 200


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


#: #284: `protocol_silent` was missing until commit started validating.
#: recover_run writes `trigger: assessment["state"]`, and protocol_silent is
#: a real assessment state, so every protocol-silence recovery persisted a
#: recovery_attempts entry outside this set and nothing noticed.
RECOVERY_TRIGGERS = {"worker_terminal", "worker_silent", "silent_run", "protocol_silent"}


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


STATUS_VALUES = {"in_progress", "blocked", "ready_to_deploy", "awaiting_approval", "complete", "design_complete", "review_complete"}


def validate_reviewer_isolation_contract(value: object) -> dict:
    fields = {"adapter", "enforcement", "project_access", "scratch_root", "subprocess_policy",
              "credentials", "network_policy", "decision", "reason", "contract_digest"}
    if not isinstance(value, dict) or set(value) != fields:
        raise HandsoffError("reviewer_isolation has invalid fields")
    if value.get("adapter") not in SELECTABLE_AGENT_ADAPTERS \
            or value.get("enforcement") not in {"native", "os_wrapper", "unavailable", "approved_compatibility"} \
            or value.get("project_access") != "read_only" or value.get("scratch_root") != "external" \
            or value.get("subprocess_policy") != "bounded" or value.get("credentials") != "sanitized" \
            or value.get("network_policy") not in {"denied", "loopback", "declared"} \
            or value.get("decision") not in {"enforce", "refuse", "approval_required"} \
            or not isinstance(value.get("reason"), str) or not value["reason"]:
        raise HandsoffError("reviewer_isolation values are invalid")
    unsigned = {key: value[key] for key in fields - {"contract_digest"}}
    expected = hashlib.sha256(_canonical(unsigned).encode()).hexdigest()
    if value.get("contract_digest") != expected:
        raise HandsoffError("reviewer_isolation contract digest is invalid")
    return deepcopy(value)


def validate_agent_actor(value: object) -> str:
    """Validate a display/audit identity without interpreting it."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise HandsoffError("agent actor must be a non-empty string without surrounding whitespace")
    if len(value) > MAX_AGENT_ACTOR_LENGTH:
        raise HandsoffError(f"agent actor must be at most {MAX_AGENT_ACTOR_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HandsoffError("agent actor must not contain control characters")
    return value


ROLE_BUDGET_FLOORS = {"architect": 20_000, "supervisor": 12_000,
                      "implementer": 24_000, "reviewer": 20_000}


def validate_session_budget_decision(value: object) -> dict:
    legacy = {"ceiling", "configured_ceiling", "floor", "risk_class", "role",
                "packet_bytes", "criteria_count", "changed_files", "followup", "basis"}
    sizing = {"estimator", "estimated_input_tokens", "input_guard_tokens",
              "protocol_overhead_tokens", "response_reserve_tokens", "safe_minimum"}
    # #290: sessions recorded before the protocol reserve existed keep their
    # two older shapes, so archives stay readable.
    reserve = {"reserved_protocol_tokens", "provider_limit"}
    if not isinstance(value, dict) or set(value) not in {
            frozenset(legacy), frozenset(legacy | sizing), frozenset(legacy | sizing | reserve)}:
        raise HandsoffError("budget_decision has invalid fields")
    if value.get("role") not in ROLE_BUDGET_FLOORS:
        raise HandsoffError("budget_decision role is invalid")
    if value.get("risk_class") is not None:
        classify_adaptive_risk(value["risk_class"])
    for field in ("ceiling", "configured_ceiling", "floor", "packet_bytes", "criteria_count", "changed_files"):
        number = value.get(field)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise HandsoffError(f"budget_decision {field} must be a non-negative integer")
    if value["ceiling"] <= 0 or value["ceiling"] > value["configured_ceiling"]:
        raise HandsoffError("budget_decision ceiling must be positive and capped by configuration")
    if not isinstance(value.get("followup"), bool) or value.get("basis") not in {
            "legacy_configured_ceiling", "risk_role_packet_scope"}:
        raise HandsoffError("budget_decision followup or basis is invalid")
    if sizing <= set(value):
        if value.get("estimator") != "ceil_utf8_bytes_over_3":
            raise HandsoffError("budget_decision estimator is invalid")
        for field in sizing - {"estimator"}:
            number = value.get(field)
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise HandsoffError(f"budget_decision {field} must be a non-negative integer")
    return deepcopy(value)


def _validate_failure_classification(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= {"category", "reason", "tail_sha256", "dependency", "operation", "changed_paths", "changes", "result_available", "adopted", "progress_summary", "budget_cause", "ceiling_overshoot_tokens"} \
            or not {"category", "reason", "tail_sha256"} <= set(value):
        raise HandsoffError("agent failure classification is invalid")
    category = value.get("category")
    # dispatch_failed and protocol_silence carry their own reason rather than the
    # canonical label: the first names the dispatch that failed, the second the
    # measured silence and the configured limit ("no protocol output in 30
    # minutes (limit 15)"), which is the whole value of the record. Every other
    # category must match its label exactly so a category cannot be relabelled.
    FREE_REASON = {"dispatch_failed", "protocol_silence"}
    if category not in FAILURE_CATEGORIES or (category not in FREE_REASON and value.get("reason") != _FAILURE_REASON_LABELS.get(category)):
        raise HandsoffError("agent failure classification is not from the closed set")
    if category == "dispatch_failed" and (not isinstance(value.get("reason"), str) or not value["reason"].strip() or len(value["reason"]) > 200):
        raise HandsoffError("dispatch failure reason must be 1 to 200 characters")
    digest = value.get("tail_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HandsoffError("agent failure classification digest is invalid")
    result = {"category": category, "reason": value["reason"], "tail_sha256": digest}
    if "budget_cause" in value:
        # #290: "the session ran out" is not a diagnosis. A model that
        # ignored its scope, a wrapper that stopped it, a shared meter that
        # stopped it, and a session that simply never spoke all need
        # different repairs and were previously one label.
        if value["budget_cause"] not in BUDGET_FAILURE_CAUSES:
            raise HandsoffError("agent failure budget cause is not from the closed set")
        result["budget_cause"] = value["budget_cause"]
    if "ceiling_overshoot_tokens" in value:
        # #290: how far past its limit the session actually went. Null means
        # unmeasured, which is not the same as zero.
        overshoot = value["ceiling_overshoot_tokens"]
        if overshoot is not None and (not isinstance(overshoot, int)
                                      or isinstance(overshoot, bool) or overshoot < 0):
            raise HandsoffError("agent failure ceiling overshoot must be a non-negative integer or null")
        result["ceiling_overshoot_tokens"] = overshoot
    for key in ("dependency", "operation"):
        if key in value:
            if not isinstance(value[key], str) or not OPERATION_IDENTIFIER_PATTERN.fullmatch(value[key]):
                raise HandsoffError("agent failure operation identifier is invalid")
            result[key] = value[key]
    if "changed_paths" in value:
        if not isinstance(value["changed_paths"], list) or len(value["changed_paths"]) > 64 or not all(isinstance(item, str) for item in value["changed_paths"]):
            raise HandsoffError("agent failure changed paths are invalid")
        result["changed_paths"] = list(value["changed_paths"])
    if "progress_summary" in value:
        # #215: what the Implementer said it finished before the session ended
        summary = value["progress_summary"]
        if not isinstance(summary, dict) or set(summary) != {"done", "partial", "untouched"} \
                or not all(isinstance(summary[k], list) and all(isinstance(c, str) for c in summary[k]) for k in summary):
            raise HandsoffError("agent failure progress summary is invalid")
        result["progress_summary"] = {k: list(summary[k]) for k in ("done", "partial", "untouched")}
    if "changes" in value:
        # #203: what the tree looked like when the reviewer was blamed
        changes = value["changes"]
        if not isinstance(changes, list) or len(changes) > 16:
            raise HandsoffError("agent failure changes are invalid")
        kept = []
        for item in changes:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) \
                    or item.get("kind") not in {"appeared", "vanished", "changed"} \
                    or not (item.get("mtime") is None or isinstance(item["mtime"], str)) \
                    or not (item.get("seconds_after_session_start") is None
                            or isinstance(item["seconds_after_session_start"], (int, float))):
                raise HandsoffError("agent failure changes are invalid")
            kept.append({"path": item["path"], "kind": item["kind"], "mtime": item.get("mtime"),
                         "seconds_after_session_start": item.get("seconds_after_session_start")})
        result["changes"] = kept
    if "adopted" in value:
        if value["adopted"] is not True:
            raise HandsoffError("agent failure adopted flag is invalid")
        result["adopted"] = True
    if "result_available" in value:
        if value["result_available"] is not True:
            raise HandsoffError("agent failure result_available is invalid")
        result["result_available"] = True
    return result


USAGE_SOURCES = ("adapter", "not reported", "disabled")


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
    if "risk_class" in status:
        try:
            classify_adaptive_risk(status["risk_class"])
        except HandsoffError as exc:
            errors.append(f"status: {exc}")
    if "model_policy" in status:
        try:
            validate_model_policy(status["model_policy"])
        except HandsoffError as exc:
            errors.append(f"status: {exc}")
    if "approval_posture" in status:
        posture = status["approval_posture"]
        expected = {"profile", "require_design_approval", "require_deployment_approval", "waivers_active"}
        if not isinstance(posture, dict) or set(posture) != expected \
                or posture.get("profile") not in {"safe", "dogfood", "unattended", "shared", "production"} \
                or any(not isinstance(posture.get(key), bool) for key in expected - {"profile"}) \
                or posture.get("waivers_active") != (not posture.get("require_design_approval")
                                                     or not posture.get("require_deployment_approval")):
            errors.append("status: 'approval_posture' is invalid")
    if "lane" in status:
        if status["lane"] not in RUN_LANES:
            errors.append("status: 'lane' must be one of full, design, review")
        for field in ("phases_run", "phases_waived"):
            values = status.get(field)
            if not isinstance(values, list) or any(not isinstance(value, int) or isinstance(value, bool)
                                                   or value not in PHASES for value in values):
                errors.append(f"status: '{field}' must be a sorted list of phase numbers")
            elif values != sorted(set(values)):
                errors.append(f"status: '{field}' must be sorted and unique")
        if isinstance(status.get("phases_run"), list) and isinstance(status.get("phases_waived"), list) \
                and set(status["phases_run"]) & set(status["phases_waived"]):
            errors.append("status: phases_run and phases_waived must be disjoint")
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
            optional = {"release_version", "release_class", "policy_override_reason", "timeout_seconds"}
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
            if "timeout_seconds" in item and (not isinstance(item["timeout_seconds"], int)
                                               or isinstance(item["timeout_seconds"], bool)
                                               or item["timeout_seconds"] <= 0):
                errors.append(f"{label}.timeout_seconds is invalid")
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
                if optional_field == "adaptive_routing":
                    if value is not None:
                        try:
                            validate_session_adaptive_routing(value)
                        except HandsoffError as exc:
                            errors.append(f"{label}.adaptive_routing: {exc}")
                    continue
                if optional_field == "budget_decision":
                    if value is not None:
                        try:
                            validate_session_budget_decision(value)
                        except HandsoffError as exc:
                            errors.append(f"{label}.budget_decision: {exc}")
                    continue
                if optional_field == "reviewer_isolation":
                    if value is not None:
                        try:
                            validate_reviewer_isolation_contract(value)
                        except HandsoffError as exc:
                            errors.append(f"{label}.reviewer_isolation: {exc}")
                    if session.get("role") == "reviewer" and value is None:
                        # Legacy reviewer sessions predate this field and remain readable.
                        pass
                    elif session.get("role") != "reviewer" and value is not None:
                        errors.append(f"{label}.reviewer_isolation applies only to reviewer sessions")
                    continue
                if optional_field == "phase_number":
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                              or value not in PHASES):
                        errors.append(f"{label}.phase_number must be null or an integer from 1 through 8")
                    continue
                if optional_field == "progress":
                    # #215: the Implementer's per-criterion claims, validated one by one
                    if value is not None and (not isinstance(value, list) or len(value) > MAX_PROGRESS_RECORDS or not all(
                            isinstance(item, dict) and set(item) == {"criterion", "state", "test", "note", "at"}
                            and validate_progress_line({k: item[k] for k in ("criterion", "state", "test", "note")}) is not None
                            and isinstance(item["at"], str) for item in value)):
                        errors.append(f"{label}.progress must be a list of validated progress records")
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
                        key: failure.get(key) for key in ("category", "reason", "tail_sha256", "dependency", "operation", "changes", "progress_summary", "budget_cause", "ceiling_overshoot_tokens")
                        if isinstance(failure, dict) and key in failure
                    }) if isinstance(failure, dict) else None
                except HandsoffError as exc:
                    errors.append(f"status: agent failure {session_id!r}: {exc}")
                    normalized = None
                allowed_fields = {"session_id", "category", "reason", "tail_sha256", "at", "dependency", "operation", "changed_paths",
                                  "changes", "result_available", "adopted", "scratch_path", "auto_retry_authorized", "acknowledged",
                                  "progress_summary", "budget_cause", "ceiling_overshoot_tokens"}
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


RELEASE_VERSION_PATTERN = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$")


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


CRITERIA_TRANSACTION_OPS = ("add", "update", "remove")


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


def validate_progress_line(payload: object) -> dict | None:
    """#215: {criterion, state, test, note}; the criterion id pattern, the
    state enum, strings bounded; anything else is None (a protocol warning)."""
    if not isinstance(payload, dict) or set(payload) - {"criterion", "state", "test", "note"} \
            or not {"criterion", "state"} <= set(payload):
        return None
    criterion = payload.get("criterion")
    if not isinstance(criterion, str) or not PROGRESS_CRITERION_PATTERN.fullmatch(criterion):
        return None
    if payload.get("state") not in PROGRESS_STATES:
        return None
    test = payload.get("test", "")
    note = payload.get("note", "")
    if not isinstance(test, str) or not isinstance(note, str) or len(test) > 512 or len(note) > MAX_PROGRESS_NOTE:
        return None
    return {"criterion": criterion, "state": payload["state"], "test": test, "note": note}


FAILURE_CATEGORIES = (
    "cancelled", "timeout", "token_budget_exhaustion", "orchestration_noop",
    "auth_failure", "rate_limit", "context_exhaustion",
    "runtime_environment", "process_crash", "non_zero_exit", "unknown", "still_running", "presumed_lost",
    "reviewer_modified_project",
    "network", "target_service", "external_timeout", "dispatch_failed", "no_artifact",
    "protocol_silence", "model_identity_mismatch",
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
    "model_identity_mismatch": "provider reported a different model than requested",
}


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
