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
import html
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
from handsoff_agent_runtime import (  # noqa: E402,F401
    AGENT_OUTPUT_FILE,
    AGENT_OUTPUT_FLUSH_INTERVAL_SECONDS,
    AGENT_OUTPUT_FLUSH_MAX_BYTES,
    AGENT_OUTPUT_FLUSH_MAX_ENTRIES,
    AGENT_OUTPUT_LOCK_FILE,
    DEFAULT_AGENT_PREFERENCE,
    DESIGN_REVIEW_AUTHORIZATION_COMMAND,
    VERIFICATION_KINDS,
    VERSION_PIN_FILE,
    _assert_agent_telemetry_integrity,
    _canonical_implementer_identity,
    _implementer_binding,
    _looks_like_runtime_drop_in,
    _new_agent_session_id,
    _new_bounded_id,
    _prune_agent_sessions,
    _review_cap_escalation,
    _runtime_identity_with_manifest,
    _validate_session_reference,
    _version_tuple,
    active_regression_request,
    agent_profiles,
    claim_precreated_agent_session,
    create_agent_session,
    current_agent_sessions,
    current_review_attempt,
    default_agent_adapter,
    design_review_budget,
    design_review_budget_exhausted_message,
    design_review_launch_refusal,
    effective_review_cap,
    ensure_no_launched_regression,
    event_log_chain_errors,
    ledger_engine_identity,
    load_verifications,
    migrate_review_ledger,
    open_review_attempt,
    progress_summary,
    read_session_liveness,
    runtime_identity,
    session_liveness_path,
    stale_manifest_refusal,
    transition_agent_session,
    update_session_liveness,
    verify_event_log,
    version_satisfies,
)



from handsoff_workflow import (  # noqa: E402,F401
    BASELINE_NOT_APPLICABLE,
    CHECKLIST_VALUES,
    CRITERION_ADD_FIELDS,
    CRITERION_SETTABLE_STATES,
    CRITERION_TYPES,
    CRITERION_UPDATE_FIELDS,
    CriteriaTransactionError,
    GATE_PROGRESS_WEIGHTS,
    MAX_CRITERIA_TRANSACTION_OPERATIONS,
    MAX_REPEAT,
    MAX_RULE_BYTES,
    PROJECT_RULES_DIR,
    RULES_DIR,
    RULES_SET_PROJECT_FILES,
    RULE_COMMANDS,
    RULE_WHEN_KEYS,
    WORK_ITEM_STATES,
    _evidence_errors,
    _is_green,
    _review_errors,
    _transaction_test_gate,
    _valid_review_anchor,
    _valid_symptom_record,
    _validate_rule,
    amendment_freeze_errors,
    append_verification,
    baseline_errors,
    ci_gate_errors,
    compute_errors,
    configured_regression_commands,
    coverage_for,
    criterion_baseline,
    derive_work_items,
    feature_hash,
    full_design_required,
    gate_progress,
    lane_gate_refusal,
    load_launch_rules,
    pending_design_decline,
    plan_criteria_transaction,
    rules_binding_errors,
    rules_set_diff,
    rules_set_entries,
    rules_set_hash,
    sync_work_item_registry,
    valid_evidence_kinds,
    validate_criterion_fields,
)

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
from handsoff_config import (  # noqa: E402,F401
    AGENT_ROLES,
    AGENT_SETTING_ADAPTERS,
    ANALYSIS_FILING_MODES,
    AUTO_AGENT_ADAPTER,
    BRIEFING_CONFIG_KEYS,
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_TOKEN_BUDGETS,
    DEFAULT_CONFIG,
    DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    DEFAULT_MAX_FAILOVERS_PER_ROLE,
    DEFAULT_MODEL_POLICY,
    DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    DEFAULT_SMALL_FIX_MAX_CRITERIA,
    DEFAULT_SMALL_FIX_MAX_FILES,
    DESIGN_EVIDENCE_ID_PATTERN,
    EXPLICIT_PROFILE_SOURCE,
    FEATURES,
    FOLLOWUP_REVIEWER_KEY,
    HOST_AGENT_ADAPTER,
    HOST_CAPABLE_ROLES,
    LEGACY_UNCONFIGURED_AGENT_ADAPTER,
    MAX_AGENT_MODEL_LENGTH,
    MAX_AGENT_TOKEN_BUDGET,
    MAX_DESIGN_EVIDENCE_ENTRIES,
    MAX_FALLBACK_PROFILES,
    MIN_AGENT_TOKEN_BUDGET,
    PLAIN_COMMAND_MESSAGE,
    RECOMMENDED_CREW,
    RECOMMENDED_PROFILE_SOURCE,
    RUNNER_DEFAULT_PROFILE_SOURCE,
    SELECTABLE_AGENT_ADAPTERS,
    SELECTABLE_AGENT_ROLES,
    TICKET_STATES,
    _validate_analysis_config,
    _validate_design_evidence_config,
    assert_plain_command,
    ensure_regression_config_is_disjoint,
    load_config,
    model_policy_allows,
    normalize_public_origins,
    normalized_test_footprint,
    validate_agent_model,
    validate_fallback_entries,
    validate_max_failovers,
    validate_model_policy,
)
from handsoff_ledger import (  # noqa: E402,F401
    ANALYSIS_DIR,
    DESIGN_EVIDENCE_FILE,
    GOVERNANCE_CONFIG_KEYS,
    HANDSOFF_GENERATED_NAMES,
    HANDSOFF_TEMP_COMPONENT,
    LIVE_BEACON_FILE,
    LIVE_INFLIGHT_FILE,
    MAX_WORK_ITEMS,
    OUTPUT_LIVENESS_FILE,
    PREFLIGHT_FILE,
    VERIFICATION_REQUIREMENTS,
    VERIFY_INFLIGHT_DIR,
    WORK_ITEM_TAG_PATTERN,
    _LEGACY_OPTIONAL_GOVERNANCE_KEYS,
    _design_errors,
    _design_hash_current,
    _design_review_errors,
    _digest_entry,
    _digest_excluded,
    _digest_listing,
    _file_sha256,
    _last_hash,
    _serialized_digest,
    _work_item_slug,
    append_event,
    atomic_write_json,
    clear_write_ahead,
    commit,
    config_hash,
    criterion_work_item,
    criterion_work_item_id,
    derive_work_item_registry,
    effective_work_items,
    event_head_path,
    event_log_path,
    evidence_drift,
    feature_enabled,
    item_acceptance_hash,
    item_criteria,
    item_progress,
    open_amendment,
    overall_item_progress,
    removed_work_item_ids,
    repository_digest,
    repository_digest_entries,
    scope_hash_matches,
    scoped_work_items,
    verification_config_hash,
    verification_log_path,
    work_item_delivery,
    work_item_scope_hash,
    work_item_scope_hashes,
    write_ahead,
    write_ahead_path,
)
from handsoff_resources import (  # noqa: E402,F401
    MAX_BRIEFING_FILE_BYTES,
    MAX_BRIEFING_TOTAL_BYTES,
    MAX_PREFLIGHT_ENTRIES,
    PLAYBOOK_DIR,
    PLAYBOOK_INDEX,
    PREFLIGHT_SCHEMA,
    RUNTIME_MANIFEST_FILE,
    briefing_section,
    engine_resource_path,
    engine_root,
    launch_preflight_snapshot,
    playbook_index,
    playbook_root,
    repository_snapshot,
)
PREFLIGHT_SUCCESS_TTL_SECONDS = 900
PREFLIGHT_FAILURE_TTL_SECONDS = 60
MAX_PREFLIGHT_REASON = 220
#: The pre-flight probe's own rollout ceiling. It used to borrow the 8,000
#: token floor, and inside a real project the reviewer-shaped prompt alone
#: cost 13,008 tokens at prefill weight 1.0, so a working Codex reported
#: `unreachable` (v0.3.25 field-note defect 1). Three times the floor bounds
#: one "Reply with OK" turn without depending on any project's context.
PREFLIGHT_TOKEN_BUDGET = 24_000
MAX_DESIGN_EVIDENCE_OUTPUT_BYTES = 8192
DESIGN_EVIDENCE_STATES = ("current", "stale", "failed", "missing")
from handsoff_projection import (  # noqa: E402,F401
    ACTOR_FAMILY_PREFIXES,
    HOST_COMMAND_EVENT_KINDS,
    LIVE_BEACON_FRESH_SECONDS,
    LIVE_BEACON_KEYS,
    OUTPUT_LIVENESS_KEYS,
    RECOVERABLE_FAILURE_CATEGORIES,
    SLEEP_LOG_CACHE_SECONDS,
    _READ_OUTPUT_LIVENESS,
    _SLEEP_LINE,
    _SLEEP_LOG_CACHE,
    _SLEEP_LOG_LOCK,
    _beacon_process_alive,
    _iso_seconds,
    _latest_event_kind,
    _live_focus_session,
    _minutes_since,
    _output_seconds_ago,
    _read_agent_output_store,
    _read_pmset_log,
    _refresh_sleep_cache,
    _seconds_since,
    activity_note,
    actor_family,
    agent_output_path,
    asleep_seconds,
    assigned_role,
    bound_heartbeat_at,
    failed_session_superseded,
    host_identity,
    host_wait_view,
    implementation_evidence_complete,
    live_beacon_path,
    live_status,
    machine_sleep_intervals,
    operation_inventory,
    output_liveness_for,
    output_liveness_path,
    parse_sleep_log,
    profile_sources,
    read_live_beacon,
    read_output_liveness,
    recovery_assessment,
    resolved_agent_profiles,
    verify_inflight_bindings,
)
LIVE_BEACON_INTERVAL_SECONDS = 5.0
LIVE_STATES = ("idle", "started", "running", "waiting", "stalled", "stopped", "failed", "complete")
OUTPUT_LIVENESS_WRITE_INTERVAL_SECONDS = 1.0
OPERATION_STATES = ("started", "succeeded", "failed", "timed_out", "cancelled")
OPERATION_ID_PATTERN = re.compile(r"^op-[a-z0-9]{4,32}$")
OPERATIONS_FILE = ".handsoff-operations.json"
MAX_AGENT_OUTPUT_SESSIONS = 8
MAX_AGENT_OUTPUT_ENTRIES = 160
MAX_AGENT_OUTPUT_LINE_CHARS = 2048
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
MAX_DESIGN_REVIEW_PACKET_BYTES = 65536
MAX_DESIGN_REVIEW_PACKET_FILES = 200
DESIGN_REVIEW_PACKET_TRIMMED_TEXT_LENGTH = 256
DESIGN_REVIEW_PACKET_INSTRUCTIONS = (
    "Verify the revision: changed criteria, unresolved and rejected findings, new material. "
    "Repository access remains available to challenge any omission or stale fact."
)
DESIGN_EVIDENCE_RECORD_FIELDS = (
    "id", "command", "command_sha256", "identity_sha256", "inputs", "input_hash", "matched_files",
    "exit_code", "output", "output_sha256", "output_bytes", "truncated", "at", "by", "head", "branch", "dirty",
)

#: What crew_view's `available` actually proves, and nothing more: the
#: adapter executable was found on PATH. Authentication, entitlement,
#: network access, and whether the model id is valid for that adapter are
#: never checked offline, so the view names its scope instead of implying it.
CREW_AVAILABILITY_SCOPE = "executable discovery only"

# #284: adaptive model routing now lives in bin/handsoff_routing.py, the
# first bounded subsystem extracted from this module. Re-exported here so
# no caller changed in that lane; tests/test_routing_boundary.py pins this
# surface to exactly the moved set, so a symbol cannot be dropped from it
# or added to it without the test saying so.
from handsoff_routing import (  # noqa: E402,F401
    ADAPTIVE_BUDGET_FIELDS,
    ADAPTIVE_DEFAULT_BUDGETS,
    ADAPTIVE_DEFAULT_PROFILES,
    ADAPTIVE_DEFAULT_RISK_POLICY,
    ADAPTIVE_ESCALATION_CHECK_OUTCOMES,
    ADAPTIVE_ESCALATION_CLAIM_DECISIONS,
    ADAPTIVE_ESCALATION_QUESTION_STATES,
    ADAPTIVE_ESCALATION_TERMINAL_OUTCOMES,
    ADAPTIVE_MODEL_CATALOG_SOURCE,
    ADAPTIVE_OPENAI_PROFILES,
    ADAPTIVE_PROFILE_FIELDS,
    ADAPTIVE_RISK_CLASSES,
    ADAPTIVE_ROUTING_TIERS,
    OPENAI_MODEL_CATALOG_SOURCE,
    _adaptive_mission_binding,
    _adaptive_model_reconciliation,
    _adaptive_required_text,
    adaptive_catalog_profile,
    adaptive_deployment_approval_required,
    adaptive_escalation_records,
    adaptive_fleet_usage,
    adaptive_risk_policy,
    adaptive_routing_budgets,
    adaptive_routing_profiles,
    adaptive_routing_snapshot,
    adaptive_usage,
    bound_adaptive_escalation,
    classify_adaptive_risk,
    evaluate_adaptive_budget,
    record_adaptive_check,
    route_adaptive_profile,
    validate_adaptive_check_plan,
    validate_adaptive_risk_policy,
    validate_adaptive_routing_budgets,
    validate_adaptive_routing_profiles,
    validate_session_adaptive_routing,
)

# #49: a run whose root name starts with one of these is a test fixture,
# a self-check, a drop-in or a benchmark run, never a product run.
FIXTURE_ROOT_PREFIXES = (
    "handsoff-test-", "handsoff-selfcheck", "handsoff-dropin", "handsoff-benchmark", "handsoff-fixture",
)
RUN_KINDS = ("test", "product")

#: #317: the ONE rule that decides what an archive is. Three readers used to
#: disagree about the same files. The Miner's answer was the correct one and
#: is adopted here: an explicit run_kind wins, and without one the repo name,
#: or the file name when the repo is absent, decides by prefix.
#:
#: The leniency it replaces mattered. `tokens_per_ticket` excluded only an
#: exact run_kind of "test", so the 100 archives written before #49 added the
#: field all passed as product, including the 81 that were fixture runs, and
#: those figures ride on every Fleet card.
#:
#: An unexpected value is NOT honoured. Trusting it would let a typo or a
#: hand-edited archive declare itself product; falling through to the name
#: rule keeps the decision on evidence the archive cannot fake about itself.
def classify_archive_record(record: object, file_name: str = "") -> str:
    """Return "test" or "product" for one archive record."""
    kind = record.get("run_kind") if isinstance(record, dict) else None
    if isinstance(kind, str) and kind in RUN_KINDS:
        return kind
    repo = record.get("repo") if isinstance(record, dict) else None
    name = repo if isinstance(repo, str) and repo else (file_name or "")
    if any(name.startswith(prefix) for prefix in FIXTURE_ROOT_PREFIXES):
        return "test"
    return "product"


#: #317: every reader of an archive's kind, named rather than remembered.
#: A reader that classifies without appearing here fails the build, so a
#: fourth private answer cannot appear the way the third one did.
ARCHIVE_CLASSIFICATION_READERS = {
    "handsoff_lib.tokens_per_ticket": "classify_archive_record",
    "miner.analyzer.classify_archive": "classify_archive_record",
    "handsoff_analyzer.scan": "classify_archive_record",
}

#: #317: the one function that shares the prefix rule without being a
#: reader. `run_kind_for` decides a run's kind as it STARTS and writes it
#: into the archive; the readers above classify an archive that already
#: exists. Declared rather than defaulted, so the exemption is a recorded
#: decision and a second one cannot appear by accident.
ARCHIVE_CLASSIFICATION_WRITERS = {
    "handsoff_lib.run_kind_for": "decides a run's kind at start and writes it into the archive",
    "handsoff_lib.archive_run": "stores run_kind_for(root) in the archive it writes",
}
MAX_PILOT_NOTE_LENGTH = 512

LEGACY_AGENT_ROLES = ("architect", "implementer", "reviewer")
#: #290: how each adapter's recorded ceiling is actually imposed. An adapter
#: absent from this map cannot bound a session and is refused at launch:
#: recording a ceiling nothing enforces reads as a guarantee and is not one.
#: "native_rollout_meter": the provider stops its own agent loop at a limit
#: passed on the argv. "wrapper_enforced": the provider offers no limit, so
#: Handsoff watches the usage it streams and terminates the session at the
#: first observation past the limit. A wrapper bound is a bound, not an
#: exact cap, and the overrun it permits is recorded rather than hidden.
CEILING_ENFORCEMENT = {"codex": "native_rollout_meter", "claude": "wrapper_enforced"}
#: #290: how tightly each bound actually holds, stated rather than implied.
#: "per_turn": the provider evaluates its meter BETWEEN turns, so a session
#: stops within one turn of its limit, and one tool-heavy turn can be large.
#: Measured at 10,535 tokens over on session
#: hs-d26f19a8a3754328ae26f3a156740692, which ran test suites and printed
#: its usage exactly once, at exit, so nothing could observe it sooner.
#: "per_observation": the adapter streams usage as it goes, so the wrapper
#: stops it at the first report past the limit.
CEILING_BOUND_GRANULARITY = {"native_rollout_meter": "per_turn",
                             "wrapper_enforced": "per_observation"}

#: #307: whether an adapter reports usage BEFORE it exits. This is the fact
#: that decides whether a ceiling can be enforced by observation at all.
#: Codex prints `tokens used` exactly once, at exit, so there is nothing to
#: observe while a turn runs and no reserve can shrink a turn already in
#: flight. Claude streams usage, so the wrapper stops it at the first report
#: past the limit. Declared per adapter rather than inferred, because
#: inferring it needs the very observation the adapter does not provide.
ADAPTER_INTERMEDIATE_USAGE = {"codex": False, "claude": True}

#: #307: roles whose turns can be arbitrarily large because they run tools.
#: A read-only turn is small and the existing reserve covers its measured
#: overshoot (764 tokens); a turn that runs a suite is not bounded by
#: anything the reserve can do.
TOOL_RUNNING_ROLES = ("implementer", "reviewer")

#: #307: the worst turn overshoot actually observed, per adapter, with its
#: provenance. It is a floor under the archive-derived number, not a
#: replacement for it: the session that motivated this ticket overshot by
#: 10,535 tokens on 2026-09-23 (hs-d26f19a8a3754328ae26f3a156740692,
#: ceiling 80,000, limit 77,952, used 88,487, no verdict) and its archive
#: predates the ceiling_overshoot_tokens field, so the archive alone
#: reports 560 and would size a ceiling an order of magnitude too small.
#: A measurement whose record is gone is still a measurement.
MEASURED_WORST_TURN_OVERSHOOT = {"codex": 10_535}


def adapter_reports_usage_before_exit(adapter: str) -> bool:
    """Whether this adapter's usage can be observed while it still runs."""
    if adapter not in ADAPTER_INTERMEDIATE_USAGE:
        raise HandsoffError(
            f"adapter {adapter!r} has not declared whether it reports usage before exit; "
            "add it to ADAPTER_INTERMEDIATE_USAGE before selecting it"
        )
    return ADAPTER_INTERMEDIATE_USAGE[adapter]


def worst_recorded_overshoot(adapter: str, role: str, archives_dir: "Path | None" = None) -> dict:
    """The largest recorded ceiling overshoot for this adapter and role.

    Read from the archives rather than guessed, and it reports how many
    samples it had: a bound resting on two observations must not present
    itself as a measured one.
    """
    directory = Path(archives_dir) if archives_dir is not None else archive_dir()
    worst, samples = 0, 0
    if not directory.is_dir():
        return {"tokens": 0, "samples": 0}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        # The overshoot is recorded on the run's agent_failures map, keyed by
        # session id, and the adapter and role live on agent_sessions under
        # the same key. Reading it off the session's own `failure` field
        # finds nothing and reports zero samples, which reads as "no
        # overshoot has ever happened" rather than "this query is wrong".
        status = record.get("status") if isinstance(record.get("status"), dict) else record
        failures = status.get("agent_failures")
        sessions = status.get("agent_sessions")
        if not isinstance(failures, dict) or not isinstance(sessions, dict):
            continue
        for session_id, failure in failures.items():
            if not isinstance(failure, dict):
                continue
            overshoot = failure.get("ceiling_overshoot_tokens")
            if not isinstance(overshoot, int) or overshoot <= 0:
                continue
            session = sessions.get(session_id)
            if not isinstance(session, dict):
                continue
            if session.get("adapter") != adapter or session.get("role") != role:
                continue
            samples += 1
            worst = max(worst, overshoot)
    return {"tokens": worst, "samples": samples}


def turn_bound_refusal(*, adapter: str, role: str, ceiling: int, compact_scope: bool,
                       safe_minimum: int = 0, archives_dir: "Path | None" = None) -> str | None:
    """Refuse a launch the engine cannot bound, naming both remedies.

    A reserve carves headroom from BELOW the ceiling, so it only helps if
    the session stops below the limit. Session
    hs-d26f19a8a3754328ae26f3a156740692 did not: ceiling 80,000, limit
    77,952, used 88,487, no verdict. Where usage arrives only at exit and
    the role runs tools, the launch is bounded instead of the budget.
    """
    if compact_scope or role not in TOOL_RUNNING_ROLES:
        return None
    if adapter_reports_usage_before_exit(adapter):
        return None
    measured = worst_recorded_overshoot(adapter, role, archives_dir)
    floor = MEASURED_WORST_TURN_OVERSHOOT.get(adapter, 0)
    allowance = max(measured["tokens"], floor)
    # The number the message gives must be one that PASSES the check below.
    # Deriving it from the current ceiling understated it whenever the
    # ceiling was under the safe minimum, so the operator raised the budget
    # to the suggested figure and was refused again on the retry.
    needed = safe_minimum + allowance
    if measured["samples"] and measured["tokens"] >= floor:
        basis = (f"worst recorded overshoot {measured['tokens']} tokens over "
                 f"{measured['samples']} recorded overshoot(s)")
    elif measured["samples"]:
        basis = (f"the declared worst observed turn for {adapter}, {floor} tokens; the archive "
                 f"holds only {measured['samples']} overshoot(s), worst {measured['tokens']}, "
                 "because the session that set this figure predates the recorded field")
    else:
        basis = (f"the declared worst observed turn for {adapter}, {floor} tokens; no overshoot "
                 "is recorded yet for this adapter and role")
    # The engine cannot stop a turn it cannot see, so this is not a promise
    # that the ceiling holds. It refuses the ceilings that demonstrably
    # cannot absorb one bad turn on top of a viable session, and leaves the
    # rest to run with the exposure recorded. A ceiling that already
    # accounts for the worst measured turn is the ticket's own second
    # remedy and is allowed through.
    if ceiling >= safe_minimum + allowance:
        return None
    return (
        f"{adapter} reports usage only at exit, so a {role} that runs tools cannot be bounded "
        f"by the {PROTOCOL_RESERVE_TOKENS}-token protocol reserve: a single turn can exceed the "
        f"whole {ceiling}-token ceiling, and this ceiling cannot absorb one such turn on top of "
        f"the {safe_minimum} tokens a viable session needs. Either give the launch a compact "
        f"review scope, so no suite is materialised and turns stay small, or raise "
        f"[agent_budget].{role} to at least {needed} to cover the worst measured turn ({basis})."
    )


def ceiling_overshoot(usage: object, provider_limit: object) -> int | None:
    """Measure how far past its limit a session actually went.

    Returns None when there is nothing to measure, which is different from
    zero: an adapter that reported no usage has not been shown to be
    within its limit, it has simply not been measured.
    """
    if not isinstance(usage, dict) or not isinstance(provider_limit, int) or isinstance(provider_limit, bool):
        return None
    total = usage.get("tokens_total")
    if not isinstance(total, int) or isinstance(total, bool):
        return None
    return max(0, total - provider_limit)


#: #290/REQ-003: the largest slice a single compact-review entry may carry.
#: A compact review exists to make a bounded question cheap; an entry big
#: enough to need discovery is not a compact review.
MAX_COMPACT_SCOPE_LINES = 400
MAX_COMPACT_SCOPE_ENTRIES = 16


def validate_compact_review_scope(entries: object) -> list[dict]:
    """Validate an explicit file-and-range review scope.

    Each entry is {"path": str, "start": int, "end": int} with 1-based,
    inclusive line numbers. Paths are repository-relative and may not
    escape it: a scope is a narrowing, never a way to reach further.
    """
    if not isinstance(entries, (list, tuple)) or not entries:
        raise HandsoffError("a compact review scope needs at least one file range")
    if len(entries) > MAX_COMPACT_SCOPE_ENTRIES:
        raise HandsoffError(
            f"a compact review scope carries at most {MAX_COMPACT_SCOPE_ENTRIES} ranges; "
            f"{len(entries)} were given, which is a full review")
    scope = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "start", "end"}:
            raise HandsoffError("each compact scope entry needs exactly path, start and end")
        path = entry["path"]
        if not isinstance(path, str) or not path.strip() or path.startswith("/"):
            raise HandsoffError("a compact scope path must be repository-relative")
        if ".." in Path(path).parts:
            raise HandsoffError("a compact scope path may not escape the repository")
        start, end = entry["start"], entry["end"]
        for label, value in (("start", start), ("end", end)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise HandsoffError(f"a compact scope {label} must be a line number of 1 or more")
        if end < start:
            raise HandsoffError("a compact scope end precedes its start")
        if end - start + 1 > MAX_COMPACT_SCOPE_LINES:
            raise HandsoffError(
                f"{path}:{start}-{end} is {end - start + 1} lines; a compact review range is at "
                f"most {MAX_COMPACT_SCOPE_LINES}")
        scope.append({"path": path, "start": start, "end": end})
    return sorted(scope, key=lambda item: (item["path"], item["start"]))


def materialize_compact_scope(root: Path, scope: list[dict], destination: Path) -> dict:
    """Write only the scoped slices into the reviewer's scratch directory.

    This is what makes a compact review a constraint rather than a request.
    #290 showed a reviewer told in prose to review only the final delta
    spend its whole budget grepping unrelated test files. A reviewer whose
    working directory contains nothing but the slices cannot do that: there
    is nothing else to find, and no test suite to run.
    """
    root, destination = Path(root).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in scope:
        source = (root / entry["path"]).resolve()
        if root not in source.parents and source != root:
            raise HandsoffError(f"{entry['path']} resolves outside the repository")
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, FileNotFoundError) as exc:
            raise HandsoffError(f"compact scope cannot read {entry['path']}: {exc}") from exc
        slice_lines = lines[entry["start"] - 1:entry["end"]]
        # The slice is labelled with its real coordinates so a finding can
        # still cite file and line without the whole file being present.
        header = f"# {entry['path']} lines {entry['start']}-{entry['end']}\n"
        target = destination / f"{entry['path'].replace('/', '__')}.{entry['start']}-{entry['end']}.slice"
        target.write_text(header + "\n".join(slice_lines) + "\n", encoding="utf-8")
        written.append({"path": entry["path"], "start": entry["start"], "end": entry["end"],
                        "lines": len(slice_lines), "slice": target.name})
    manifest = {"schema": "handsoff.compact_review_scope", "version": 1,
                "entries": written, "total_lines": sum(item["lines"] for item in written),
                # Nothing executable is materialized, so there is no test
                # runner, no suite and no project configuration to invoke.
                "tests_executable": False, "repository_visible": False}
    (destination / "SCOPE.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                            encoding="utf-8")
    return manifest


def adapter_ceiling_enforcement(adapter: str) -> str:
    """Name how this adapter's ceiling is imposed, or refuse the launch."""
    enforcement = CEILING_ENFORCEMENT.get(adapter)
    if enforcement is None:
        raise HandsoffError(
            f"adapter {adapter!r} cannot impose a token ceiling on the wire and Handsoff "
            "cannot bound it by observation; add it to CEILING_ENFORCEMENT before selecting it"
        )
    return enforcement
OVERRIDES_FILE = "handsoff-overrides.json"
ROLE_PROTOCOL_PREFIXES = {"reviewer": "HANDSOFF_REVIEW_RESULT:",
                          "architect": "HANDSOFF_DESIGN_PROPOSAL:",
                          "supervisor": "HANDSOFF_BROKER_REQUEST:"}




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
















def engine_manifest_version() -> str | None:
    """The version of the engine running this code, from its own manifest;
    None when it cannot be read."""
    try:
        return json.loads((engine_root() / RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None




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


#: the playbook part of a launch stays small enough to ride every launch;
#: a topic that would push it past this is refused at launch, never trimmed
MAX_PLAYBOOK_SECTION_BYTES = 16 * 1024  # #217: room for the protocol topic (8 KB) beside the index and the lanes






def playbook_text(topic: str | None = None) -> str:
    """`handsoff playbook [topic]`: the index, or one topic's files."""
    manifest = playbook_index()
    root = playbook_root()
    if topic is None:
        return (root / "INDEX.md").read_text(encoding="utf-8")
    topic = topic.strip()
    if topic not in manifest["topics"]:
        raise HandsoffError(f"playbook topic is not declared: {topic} (topics: {', '.join(sorted(manifest['topics']))})")
    names = [item["file"] for item in manifest.get("files", []) if topic in item.get("topics", [])]
    if not names:
        raise HandsoffError(f"playbook topic has no files: {topic}")
    return "\n\n".join((root / name).read_text(encoding="utf-8").rstrip() for name in names) + "\n"


def playbook_section(topic: str | None = None) -> str:
    """The playbook part of a managed launch's briefing: always_load (the
    index and the lane recipe), plus one topic when asked. Every managed
    session carries the lane rules on any machine, without configuration."""
    manifest = playbook_index()
    root = playbook_root()
    names = list(manifest.get("always_load") or [])
    if topic is not None and topic in manifest["topics"]:
        names.extend(item["file"] for item in manifest.get("files", []) if topic in item.get("topics", []))
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    sections = [f"## playbook/{name}\n\n{(root / name).read_text(encoding='utf-8').rstrip()}" for name in unique]
    text = "# Handsoff playbook\n\n" + "\n\n".join(sections)
    size = len(text.encode("utf-8"))
    if size > MAX_PLAYBOOK_SECTION_BYTES:
        raise HandsoffError(f"the playbook section for a launch is {size} bytes, over {MAX_PLAYBOOK_SECTION_BYTES}"
                            f" (files: {', '.join(unique)}); shorten the playbook")
    return text




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




# #284 stage 1: the core primitives now live in bin/handsoff_core.py, a
# layer with zero outbound dependencies on the rest of the engine.
# Re-exported here so no caller changed.
from handsoff_core import (  # noqa: E402,F401
    HandsoffError,
    _atomic_write_text,
    _canonical,
    acceptance_hash,
    acceptance_path,
    criterion_spec_hash,
    design_hash,
    durable_backup_path,
    durable_replace,
    load_unique_json,
    lock_path,
    project_lock,
    status_path,
)


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


def reviewer_isolation_contract(adapter: str, cfg: dict | None = None) -> dict:
    """Return the provider-neutral, digest-bound pre-launch verdict."""
    settings = (cfg or {}).get("reviewer_isolation", {})
    compatibility = bool(settings.get("compatibility_mode"))
    approved = bool(settings.get("compatibility_approved"))
    if adapter == "codex":
        enforcement, decision, reason = "native", "enforce", "native scratch sandbox"
        network = "denied"
    elif compatibility and approved:
        enforcement, decision = "approved_compatibility", "enforce"
        reason = "explicitly approved adapter compatibility mode"
        network = "declared"
    elif compatibility:
        enforcement, decision = "approved_compatibility", "approval_required"
        reason = "compatibility mode requires explicit reviewer_isolation.compatibility_approved=true"
        network = "declared"
    else:
        enforcement, decision = "unavailable", "refuse"
        reason = "adapter has no enforceable OS-backed read-only project boundary"
        network = "declared"
    contract = {
        "adapter": adapter, "enforcement": enforcement,
        "project_access": "read_only", "scratch_root": "external",
        "subprocess_policy": "bounded", "credentials": "sanitized",
        "network_policy": network, "decision": decision, "reason": reason,
    }
    contract["contract_digest"] = hashlib.sha256(_canonical(contract).encode()).hexdigest()
    return contract




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
    # #290: `token_budget` here is the PROVIDER LIMIT, already reduced by the
    # protocol reserve, so the meter stops with room left to emit a verdict.
    # The whole allowance stays on the session record as `ceiling`.
    argv.extend([
        "-c",
        ("features.rollout_budget={enabled=true,"
         f"limit_tokens={token_budget},reminder_at_remaining_tokens=[],"
         "sampling_token_weight=1.0,prefill_token_weight=1.0}"),
    ])
    if workspace_write and not reviewer_sandbox:
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




def render_review_report(status: dict, acceptance: dict) -> str:
    """Render the stable human-readable report for a completed review lane."""
    review = status.get("review") if isinstance(status.get("review"), dict) else {}
    adopted = status.get("implementation_adopted") if isinstance(status.get("implementation_adopted"), dict) else {}
    verdict = review.get("verdict") or review.get("decision") or "approved"
    lines = ["# Review report", "", f"- Verdict: {verdict}",
             f"- Reviewer: {review.get('by') or status.get('reviewed_by') or 'unknown'}",
             f"- Adopted commit: {adopted.get('sha') or 'unknown'}",
             f"- Tests executed: {review.get('tests_executed') or 'not recorded'}", "", "## Criteria", ""]
    for criterion in acceptance.get("criteria", []):
        if not isinstance(criterion, dict):
            continue
        evidence = criterion.get("evidence")
        evidence_text = json.dumps(evidence, sort_keys=True) if evidence is not None else "none"
        lines.append(f"- `{criterion.get('id', 'unknown')}` — state: {criterion.get('state', 'unknown')}; evidence: {evidence_text}")
    lines.extend(["", "## Findings", ""])
    findings = []
    for attempt in status.get("review_attempts", []):
        if isinstance(attempt, dict):
            findings.extend(item for item in (attempt.get("findings") or []) if isinstance(item, dict))
    if not findings:
        lines.append("- None")
    else:
        for finding in findings:
            lines.append(f"- `{finding.get('code', 'UNKNOWN')}` — {finding.get('summary') or finding.get('text', '')}")
    return "\n".join(lines) + "\n"








































def _canonical_provider_model(model: object) -> str | None:
    """Normalize provider decorations without pretending an alias was reported."""
    if not isinstance(model, str) or not model.strip():
        return None
    return model.strip().split("[", 1)[0]












def _agent_assignment(session: dict) -> dict:
    route = session.get("adaptive_routing") if isinstance(session.get("adaptive_routing"), dict) else None
    requested_model = route.get("model") if route else session.get("requested_model")
    reported_model = session.get("reported_model")
    if reported_model:
        model, model_source = reported_model, "adapter_reported"
    elif route:
        model, model_source = requested_model, "adaptive_selection"
    elif requested_model and requested_model != DEFAULT_AGENT_MODEL:
        model, model_source = requested_model, "exact_request"
    else:
        model, model_source = None, "not_reported"
    role = session.get("role")
    phase = session.get("phase_number")
    reconciliation = _adaptive_model_reconciliation(session)
    if role == "reviewer" and phase in {1, 2}:
        purpose = "Design challenge"
    elif role == "reviewer" and phase == 5:
        purpose = "Implementation audit"
    elif role == "architect":
        purpose = "Solution architecture"
    elif role == "implementer":
        purpose = "Build and verification"
    elif role == "supervisor":
        purpose = "Mission supervision"
    else:
        purpose = PHASES.get(phase, "Managed task")
    budget = session.get("budget_decision") if isinstance(session.get("budget_decision"), dict) else None
    usage = session.get("usage") if isinstance(session.get("usage"), dict) else None
    assignment = {
        "session_id": session.get("session_id"), "role": role,
        "actor": session.get("actor"), "purpose": purpose, "phase_number": phase,
        "started_at": session.get("started_at"), "ended_at": session.get("ended_at"),
        "adaptive": route is not None, "tier": (route or {}).get("tier"),
        "adapter": (route or {}).get("adapter", session.get("adapter")),
        "model": model, "requested_model": requested_model,
        "model_source": model_source, "model_consistency": reconciliation["consistency"],
        "reason": (route or {}).get("reason", session.get("resolution_source")),
        "state": session.get("state"),
    }
    # Preserve the historical snapshot shape for sessions created before
    # budget/usage telemetry existed. New sessions carry both facts.
    if budget is not None:
        assignment["budget_decision"] = deepcopy(budget)
    if usage is not None:
        assignment["usage"] = deepcopy(usage)
    if isinstance(session.get("reviewer_isolation"), dict):
        assignment["reviewer_isolation"] = deepcopy(session["reviewer_isolation"])
    return assignment
















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




def fallback_profiles(cfg: dict) -> dict:
    return {role: deepcopy(cfg.get("fallbacks", {}).get(role, [])) for role in SELECTABLE_AGENT_ROLES}






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


RISK_BUDGET_BASES = {
    "routine": 0, "elevated": 4_000, "security_sensitive": 12_000,
    "persistence_migration": 14_000, "shared_infrastructure": 16_000,
    "irreversible": 20_000,
}
#: #290: the packet allowance below saturates at 16,000 tokens, which it
#: reaches at 16,000 packet bytes. Past that point the allowance stops
#: growing while the packet keeps growing, so sizing decouples from the
#: work. That decoupling, not the risk class, is what exhausts a reviewer.
BROAD_REVIEW_PACKET_BYTES = 16_000
#: Criteria breadth reaches the same decoupling sooner; this threshold is
#: unchanged from the risk-keyed guard it replaces.
BROAD_REVIEW_CRITERIA = 8
#: #290: held back from the limit handed to the provider so a session can
#: still emit its verdict. Sized for the protocol line itself, already
#: costed as protocol_overhead_tokens (1,024), plus the overshoot of a
#: read-only turn, measured at 764 tokens on session
#: hs-978fd700da4d42b3b9ca70545bc0ceb5.
#:
#: It does NOT cover a tool-running turn, and #307 tracks the real fix.
#: Session hs-d26f19a8a3754328ae26f3a156740692 ran test suites and reported
#: 88,487 tokens against a 77,952 limit: 10,535 past the limit and 8,487
#: past the whole 80,000 ceiling. No reserve could have saved that verdict,
#: because a reserve carves headroom from BELOW the ceiling and that
#: session did not stop below it:
#:
#:     reserve  2,048 -> limit 77,952 -> used 88,487 -> unreachable
#:     reserve 12,000 -> limit 68,000 -> used 88,487 -> unreachable
#:     reserve 20,000 -> limit 60,000 -> used 88,487 -> unreachable
#:
#: Raising it would only start the meter earlier while costing every
#: well-behaved session real headroom, so it stays at 2,048 deliberately.
#: The overshoot is a property of TURN SIZE, so the lever is turn size
#: (#307), not reserve size. ceiling_overshoot_tokens is now recorded on
#: every session that exceeds its limit so this can eventually be set from
#: a measured distribution instead of two samples.
PROTOCOL_RESERVE_TOKENS = 2_048


def _apply_protocol_reserve(decision: dict, *, role: str, safe_minimum: int) -> dict:
    """Hold back the protocol reserve, or refuse with the shortfall named.

    #290/REQ-012, the whole invariant in one place so it cannot be applied
    twice or skipped: `ceiling` is the entire session allowance and is what
    usage is measured against; `provider_limit` is that minus the reserve
    and is the only number the provider is told.
    """
    ceiling = decision["ceiling"]
    reserved = PROTOCOL_RESERVE_TOKENS
    if reserved >= ceiling:
        raise HandsoffError(
            f"the {reserved}-token protocol reserve meets or exceeds the {ceiling}-token "
            f"ceiling for {role}; raise [agent_budget].{role} by at least "
            f"{reserved - ceiling + 1} tokens"
        )
    # REQ-012 says a packet that leaves too little is "refused at launch",
    # and the launch sites already own that refusal with their established
    # wording (#290). The planner only records the numbers; raising here as
    # well would pre-empt those messages and break every caller that reads
    # them. Only the arithmetically impossible case stops the planner.
    provider_limit = ceiling - reserved
    decision["reserved_protocol_tokens"] = reserved
    decision["provider_limit"] = provider_limit
    return decision


def plan_role_token_budget(*, configured_ceiling: int, role: str,
                           risk_class: str | None, packet_bytes: int = 0,
                           criteria_count: int = 0, changed_files: int = 0,
                           followup: bool = False) -> dict:
    """Return a deterministic ceiling and its auditable, non-usage basis."""
    if role not in ROLE_BUDGET_FLOORS:
        raise HandsoffError("token budget role is invalid")
    if not isinstance(configured_ceiling, int) or isinstance(configured_ceiling, bool) or configured_ceiling <= 0:
        raise HandsoffError("configured token ceiling must be a positive integer")
    for label, value in (("packet_bytes", packet_bytes), ("criteria_count", criteria_count),
                         ("changed_files", changed_files)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HandsoffError(f"{label} must be a non-negative integer")
    estimated_input_tokens = math.ceil(packet_bytes / 3)
    input_guard_tokens = math.ceil(estimated_input_tokens * 0.10)
    protocol_overhead_tokens = 1_024
    response_reserve_tokens = max(4_096, criteria_count * 256)
    safe_minimum = (estimated_input_tokens + input_guard_tokens
                    + protocol_overhead_tokens + response_reserve_tokens)
    sizing = {
        "estimator": "ceil_utf8_bytes_over_3", "estimated_input_tokens": estimated_input_tokens,
        "input_guard_tokens": input_guard_tokens,
        "protocol_overhead_tokens": protocol_overhead_tokens,
        "response_reserve_tokens": response_reserve_tokens,
        "safe_minimum": safe_minimum,
    }
    if risk_class is None:
        return _apply_protocol_reserve(
            {"ceiling": configured_ceiling, "configured_ceiling": configured_ceiling,
             "floor": ROLE_BUDGET_FLOORS[role], "risk_class": None,
             "role": role, "packet_bytes": packet_bytes,
             "criteria_count": criteria_count, "changed_files": changed_files,
             "followup": bool(followup), "basis": "legacy_configured_ceiling", **sizing},
            role=role, safe_minimum=safe_minimum)
    risk_class = classify_adaptive_risk(risk_class)
    floor = ROLE_BUDGET_FLOORS[role]
    # Packet bytes are converted conservatively: roughly four bytes/token,
    # plus output/tool headroom. Follow-ups receive less discovery headroom
    # because unchanged bulk context is deliberately absent.
    packet_allowance = min(16_000, math.ceil(packet_bytes / 4) + (6_000 if followup else 12_000))
    scope_allowance = min(12_000, criteria_count * 750 + changed_files * 1_000)
    role_allowance = 8_000 if role in {"architect", "implementer"} else 4_000
    calculated = floor + RISK_BUDGET_BASES[risk_class] + role_allowance + packet_allowance + scope_allowance
    # A broad review is the quality gate, not a cheap discovery turn.
    # Reducing it below the configured ceiling caused the reviewer to
    # exhaust after doing the work but before issuing its verdict, three
    # times now. #290: this keyed on risk class until 2026-09-23, when a
    # ten-criterion review of a *routine* change fell through the guard and
    # exhausted at 49,264 tokens against a 48,500 ceiling. Discovery cost
    # tracks breadth, not risk, so breadth alone widens it. Lower-risk
    # follow-up reviews stay packet-sized because a delta packet is small.
    broad_review = role == "reviewer" and (
        criteria_count >= BROAD_REVIEW_CRITERIA
        or packet_bytes >= BROAD_REVIEW_PACKET_BYTES
    )
    if broad_review:
        calculated = configured_ceiling
    ceiling = min(configured_ceiling, max(floor, calculated))
    return _apply_protocol_reserve(
        {"ceiling": ceiling, "configured_ceiling": configured_ceiling,
         "floor": floor, "risk_class": risk_class, "role": role,
         "packet_bytes": packet_bytes, "criteria_count": criteria_count,
         "changed_files": changed_files, "followup": bool(followup),
         "basis": "risk_role_packet_scope", **sizing},
        role=role, safe_minimum=safe_minimum)




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


def _preflight_runtime_dir(adapter: str) -> Path:
    if adapter == "codex":
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _path_can_create(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)


def _launch_preflight_fingerprint(*, adapter: str, model: str, executable: str,
                                  cwd: str, state_dir: Path) -> str:
    facts = {"adapter": adapter, "model": model, "executable": str(Path(executable).resolve()),
             "cwd": str(Path(cwd).resolve()), "state_dir": str(state_dir.resolve())}
    for label, path in (("executable", Path(executable)), ("state_dir", state_dir)):
        try:
            stat = path.stat()
            facts[label + "_stat"] = [stat.st_dev, stat.st_ino, stat.st_mode, stat.st_mtime_ns]
        except OSError:
            facts[label + "_stat"] = None
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bounded_preflight_reason(text: object) -> str:
    value = redact_output_text(str(text or "")).replace("\n", " ").strip()
    return value[:MAX_PREFLIGHT_REASON] or "preflight failed without diagnostic output"


def launch_preflight(root: Path, *, adapter: str, model: str, executable: str,
                     argv: list[str] | tuple[str, ...], cwd: str,
                     runner=subprocess.run, timeout: int = 45,
                     state_dir: Path | None = None,
                     now: datetime | None = None) -> dict:
    """Validate the exact adapter/model before a session or budget exists.

    A fingerprinted success/failure cache coalesces equivalent failures into
    one incident. Local state and login checks run before the launch-shaped
    model probe, so predictable failures make no provider call.
    """
    if adapter not in SELECTABLE_AGENT_ADAPTERS:
        raise HandsoffError("launch preflight adapter must be codex or claude")
    model = validate_agent_model(model)
    root = root.resolve()
    now = now or datetime.now(timezone.utc)
    runtime_dir = Path(state_dir) if state_dir is not None else _preflight_runtime_dir(adapter)
    fingerprint = _launch_preflight_fingerprint(
        adapter=adapter, model=model, executable=executable, cwd=cwd, state_dir=runtime_dir,
    )
    path = root / PREFLIGHT_FILE
    try:
        store = load_unique_json(path)
    except HandsoffError:
        store = {}
    if not isinstance(store, dict) or store.get("schema") != PREFLIGHT_SCHEMA:
        store = {"schema": PREFLIGHT_SCHEMA, "entries": {}, "incident": None}
    entries = store.get("entries") if isinstance(store.get("entries"), dict) else {}
    cached = entries.get(fingerprint)
    if isinstance(cached, dict):
        try:
            fresh = datetime.fromisoformat(cached["expires_at"]) > now
        except (KeyError, TypeError, ValueError):
            fresh = False
        if fresh:
            result = deepcopy(cached)
            result["cached"] = True
            if result.get("state") == "blocked":
                result["coalesced_count"] = min(int(result.get("coalesced_count", 0)) + 1, 1_000_000)
                entries[fingerprint] = {k: v for k, v in result.items() if k != "cached"}
                store["entries"] = entries
                store["incident"] = deepcopy(entries[fingerprint])
                atomic_write_json(path, store)
            return result

    checked_at = now.isoformat()
    category = None
    reason = None
    probe_calls = 0
    if not runtime_dir.exists() and not _path_can_create(runtime_dir):
        category, reason = "runtime_environment", f"runtime state directory is not writable: {runtime_dir}"
    elif runtime_dir.exists() and (not runtime_dir.is_dir() or not os.access(runtime_dir, os.W_OK | os.X_OK)):
        category, reason = "runtime_environment", f"runtime state directory is not writable: {runtime_dir}"
    else:
        login_argv = ([executable, "login", "status"] if adapter == "codex"
                      else [executable, "auth", "status"])
        try:
            login = runner(login_argv, text=True, capture_output=True, timeout=min(timeout, 15), cwd=cwd)
        except subprocess.TimeoutExpired:
            login = None
            category, reason = "auth_failure", "adapter login status timed out"
        if login is not None and login.returncode != 0:
            category = "auth_failure"
            reason = "adapter is not authenticated: " + _bounded_preflight_reason(
                (login.stderr or login.stdout or "login status failed")
            )
        if category is None:
            try:
                probe_calls = 1
                completed = runner(list(argv), input="Reply with OK", text=True,
                                   capture_output=True, timeout=timeout, cwd=cwd)
                outcome = _preflight_outcome(completed)
                if outcome["state"] != "reachable":
                    raw = (completed.stderr or "") + (completed.stdout or "")
                    if re.search(r"model.+(?:not supported|unavailable|not found)|unsupported model", raw, re.I):
                        category = "model_unavailable"
                    else:
                        category = classify_runtime_failure(
                            exit_code=completed.returncode,
                            stderr_tail=(completed.stderr or "")[-2000:],
                            stdout_tail=(completed.stdout or "")[-2000:],
                        )["category"]
                    reason = outcome["reason"]
            except subprocess.TimeoutExpired:
                probe_calls = 1
                category, reason = "timeout", "exact-model reachability probe timed out"

    ttl = PREFLIGHT_FAILURE_TTL_SECONDS if category else PREFLIGHT_SUCCESS_TTL_SECONDS
    entry = {
        "fingerprint": fingerprint, "adapter": adapter, "model": model,
        "state": "blocked" if category else "ready", "category": category,
        "reason": _bounded_preflight_reason(reason) if category else None,
        "checked_at": checked_at, "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "provider_probe_calls": probe_calls, "coalesced_count": 0,
    }
    entries[fingerprint] = entry
    ordered = sorted(entries.values(), key=lambda item: item.get("checked_at", ""), reverse=True)
    store["entries"] = {item["fingerprint"]: item for item in ordered[:MAX_PREFLIGHT_ENTRIES]}
    store["incident"] = deepcopy(entry) if category else None
    atomic_write_json(path, store)
    return {**deepcopy(entry), "cached": False}




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



class PacketRuleViolation(HandsoffError):
    """A reviewer packet broke a packet rule. `recovered` is the packet with
    the offending field set to the rule's recover_as value (or None when
    the rule names none), so the verdict can be adopted deliberately."""

    def __init__(self, message: str, *, rule_id: str, field: str, value, recovered: dict | None):
        super().__init__(message)
        self.rule_id, self.field, self.value, self.recovered = rule_id, field, value, recovered






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









def rules_binding(root: Path, cfg: dict) -> dict:
    """What a decision records: the hash and the entries behind it."""
    entries = rules_set_entries(root)
    return {"rules_hash": hashlib.sha256(_canonical(entries).encode("utf-8")).hexdigest(),
            "rules_entries": entries}




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


















# --------------------------------------------------------------------------
# JSON I/O: duplicate-key detection, atomic writes
# --------------------------------------------------------------------------



def durability_capability(path: Path) -> dict:
    """Describe the guarantee available to replacement writers on this host.

    POSIX platforms normally support both file and directory fsync.  A
    platform that rejects directory fsync still gets flushed content and an
    atomic same-directory replacement, but is reported as best effort.
    """
    parent = Path(path).parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        return {"level": "best_effort", "file_fsync": True, "directory_fsync": False,
                "reason": f"directory fsync unavailable: {type(exc).__name__}"}
    return {"level": "full", "file_fsync": True, "directory_fsync": True, "reason": None}






def restore_durable_backup(path: Path) -> dict:
    """Restore the bounded last-known-good copy after validating JSON."""
    path = Path(path)
    backup = durable_backup_path(path)
    try:
        payload = backup.read_bytes()
        json.loads(payload)
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"durable backup is unavailable or invalid: {backup}") from exc
    capability = durable_replace(path, payload, keep_backup=False)
    return {"path": str(path), "backup": str(backup), "restored": True,
            "durability": capability}




# --------------------------------------------------------------------------
# write-ahead journal: lets `doctor` prove a crash, not launder a hand edit
# --------------------------------------------------------------------------







def read_write_ahead(root: Path) -> dict | None:
    path = write_ahead_path(root)
    if not path.exists():
        return None
    try:
        return load_unique_json(path)
    except HandsoffError:
        return None




# --------------------------------------------------------------------------
# managed-agent runtime telemetry
# --------------------------------------------------------------------------

def default_agent_actor(adapter: str, role: str) -> str:
    """Stable documented identity used when ``handsoff_agent launch`` omits --by."""
    if adapter not in SELECTABLE_AGENT_ADAPTERS or role not in SELECTABLE_AGENT_ROLES:
        raise HandsoffError("cannot derive an actor for an unsupported agent adapter or role")
    return f"{adapter}-{role}"






















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






# --------------------------------------------------------------------------
# #168: usage, from the adapter's own words
# --------------------------------------------------------------------------

_CODEX_TOKENS_LINE = re.compile(r"^\s*tokens used\s*:?\s*([0-9][0-9,]*)?\s*$", re.IGNORECASE)
_NUMBER_LINE = re.compile(r"^\s*([0-9][0-9,]*)\s*$")
#: #306: Codex announces its resolved model in a plain-text banner, not in
#: JSON, so it never reached the stream-json parser and every Codex session
#: recorded reported_model null. The banner is fenced by rules; only a field
#: inside that fence is the adapter's own word, because the packet echoed
#: afterwards can contain the string "model:" in the task text.
_CODEX_BANNER_RULE = re.compile(r"^\s*-{4,}\s*$")
_CODEX_BANNER_MODEL = re.compile(r"^\s*model\s*:\s*(\S+)\s*$", re.IGNORECASE)


class UsageWatcher:
    """Watches every streamed output line (stdout and stderr alike) and keeps
    the LAST usage the adapter printed, independent of the bounded tails.
    Codex prints 'tokens used' and the number on the next line (or the
    same line); Claude's stream-json carries usage.input_tokens and
    usage.output_tokens on its events. Nothing is estimated."""

    def __init__(self, adapter: str | None = None):
        self.adapter = adapter
        self.usage: dict | None = None
        self.reported_model: str | None = None
        self._awaiting_number = False
        #: How many banner rules have gone by. The model field is only
        #: trusted between the first and the second.
        self._banner_rules = 0

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
        if self.adapter == "codex" and self._banner_rules < 2:
            if _CODEX_BANNER_RULE.match(text):
                self._banner_rules += 1
                return
            banner = _CODEX_BANNER_MODEL.match(text) if self._banner_rules == 1 else None
            if banner and self.reported_model is None:
                try:
                    self.reported_model = validate_agent_model(banner.group(1))
                except HandsoffError:
                    self.reported_model = None
                return
        if text.startswith("{"):
            try:
                event = json.loads(text)
            except ValueError:
                return
            usage = _find_usage(event)
            if usage:
                self._set(tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"))
            model = _find_reported_model(event, self.adapter)
            if model:
                self.reported_model = model

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


def _find_reported_model(event: object, adapter: str | None) -> str | None:
    """Return only a model identity explicitly reported by the adapter.

    Claude stream-json names the resolved model in the init event and, on a
    completed call, as the key of modelUsage.  The latter wins because it is
    the provider's final accounting identity.  We intentionally do not walk
    arbitrary nested `model` keys: task payloads may contain model names that
    were discussed but never used.
    """
    # #306: this parser reads Claude's stream-json shapes. Codex reports its
    # model in a plain-text banner, handled in UsageWatcher.feed. An adapter
    # with no known shape reports nothing here, and the absence is recorded
    # as unreported rather than mistaken for a match.
    if adapter != "claude" or not isinstance(event, dict):
        return None
    usage = event.get("modelUsage")
    if isinstance(usage, dict):
        models = [key for key in usage if isinstance(key, str) and key.strip()]
        if len(models) == 1:
            try:
                return validate_agent_model(models[0])
            except HandsoffError:
                return None
    if event.get("type") == "system" and event.get("subtype") == "init":
        try:
            return validate_agent_model(event.get("model"))
        except HandsoffError:
            return None
    return None




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


def record_reported_model(root: Path, session_id: str, model: str) -> dict:
    """#309: persist the provider's model the moment the adapter announces it.

    `UsageWatcher` parses the model from the adapter's banner within the
    first second, but until now the only writer was `end_session`, which
    runs on a terminal transition. A running session therefore always
    reported no model, which is precisely the window in which an operator
    can still act on it: the fact was available exactly when it could no
    longer be used.

    The write is deliberately narrow. It changes `reported_model` and
    nothing else, so a session's state, usage, exit code and timestamps are
    untouched; it refuses a session that has already ended rather than
    reopening a record that is evidence; and it is idempotent, so repeated
    observations of the same model cost one write. A later announcement of
    a DIFFERENT model does not overwrite the first: that disagreement is
    for `_adaptive_model_reconciliation` to judge, not for this writer to
    silently resolve.
    """
    if not isinstance(session_id, str) or not AGENT_SESSION_ID_PATTERN.fullmatch(session_id):
        raise HandsoffError("agent session id is invalid")
    model = validate_agent_model(model)
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict):
            raise HandsoffError(f"agent session {session_id} is unknown")
        if session.get("state") in AGENT_SESSION_TERMINAL_STATES:
            raise HandsoffError(
                f"agent session {session_id} already ended as {session.get('state')}; its record "
                "is evidence and a late announcement does not reopen it")
        existing = session.get("reported_model")
        if existing is not None:
            # Idempotent for a repeat, and silent about a disagreement: the
            # first announcement stands and reconciliation judges the rest.
            return {"session_id": session_id, "reported_model": existing,
                    "written": False, "reason": "already recorded"}
        proposed = deepcopy(status)
        proposed["agent_sessions"][session_id]["reported_model"] = model
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="agent_model_reported",
               event_message=f"{session_id} reported model {model}", by="runtime",
               session_id=session_id, reported_model=model)
    return {"session_id": session_id, "reported_model": model, "written": True, "reason": None}








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
                              session_id_factory=None, replacement_id_factory=None,
                              pre_reservation_check=None) -> dict:
    """Atomically reserve one trusted fallback after its exact launch check.

    ``pre_reservation_check`` receives the complete proposed replacement
    record while the source-session CAS is still protected, but before a
    managed session or replacement record is persisted.  Raising aborts the
    reservation unchanged.  This lets the launcher preflight the exact
    selected profile and handoff without manufacturing a failed session for
    a launch that never became viable.
    """
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
                model_policy=status.get("model_policy", cfg.get("model_policy")),
                required_tier=((source.get("adaptive_routing") or {}).get("tier")
                               if isinstance(source.get("adaptive_routing"), dict) else None),
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
        if pre_reservation_check is not None:
            pre_reservation_check(deepcopy(record))
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






# --------------------------------------------------------------------------
# tamper-evident event log
# --------------------------------------------------------------------------



def render_design_document(root: Path, cfg: dict, status: dict, acceptance: dict) -> str:
    """Build the deterministic, self-contained design-lane terminal document."""
    review = deepcopy(status.get("design_review"))
    try:
        repository = repository_snapshot(root)
    except HandsoffError:
        repository = {"remote": "", "head": ""}
    block = {
        "schema": 1,
        "criteria": deepcopy(acceptance.get("criteria", [])),
        "design_hash": review.get("design_hash") if isinstance(review, dict) else design_hash(acceptance.get("criteria", [])),
        "criterion_hashes": {c.get("id"): criterion_spec_hash(c)
                            for c in acceptance.get("criteria", []) if isinstance(c, dict)},
        "design_review": review,
        "repository": {"remote": repository.get("remote", ""), "head": repository.get("head", "")},
        "items": deepcopy(acceptance.get("work_items", [])),
        "lane": status.get("lane"),
        "phases_run": deepcopy(status.get("phases_run", [])),
        "phases_waived": deepcopy(status.get("phases_waived", [])),
        "engine": runtime_identity(root),
    }
    if status.get("design_approved") is not None:
        block["design_approved"] = deepcopy(status["design_approved"])
    proposal = status.get("design_proposal") or {}
    if proposal:
        block["design_proposal"] = deepcopy(proposal)
    payload = _canonical(block)
    # A JSON script block must not contain a literal closing-tag opener.  These
    # escapes are valid JSON and therefore preserve the value after parsing.
    script_payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    def esc(value):
        return html.escape(str(value), quote=True)
    rows = []
    for criterion in block["criteria"]:
        rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(criterion.get("id", "")), esc(criterion.get("requirement", "")),
            esc(criterion.get("verification", "")), esc(criterion.get("work_item", criterion.get("work_item_tag", "")))))
    findings = review.get("findings", []) if isinstance(review, dict) else []
    findings_html = "".join(f"<li>{esc(item)}</li>" for item in findings)
    sections = f"<h2>summary</h2><p>{esc(proposal.get('summary', ''))}</p>"
    for key in ("approach", "tradeoffs", "decisions", "constraints", "verification"):
        items = proposal.get(key, [])
        sections += f"<h2>{esc(key)}</h2><ul>{''.join(f'<li>{esc(item)}</li>' for item in items)}</ul>"
    readable = (f"<h1>Design lane</h1>{sections}<h2>Criteria</h2><table><tr><th>ID</th><th>Requirement</th>"
                f"<th>Verification policy</th><th>Work item tag</th></tr>{''.join(rows)}</table>"
                f"<h2>Design review</h2><p>Decision: {esc(review.get('decision', '') if isinstance(review, dict) else '')}</p>"
                f"<p>Reviewer: {esc(review.get('by', '') if isinstance(review, dict) else '')}</p><ul>{findings_html}</ul>")
    return "<!doctype html><html><head><meta charset=\"utf-8\"><title>Design lane</title></head><body>" \
        + readable + f'<script type="application/json" id="handsoff-design">{script_payload}</script></body></html>\n'












# --------------------------------------------------------------------------
# immutable verification ledger
# --------------------------------------------------------------------------









# --------------------------------------------------------------------------
# schema: minimal, stdlib-only (no jsonschema dependency, matching the
# project's own "portable, no assumptions" stance)
# --------------------------------------------------------------------------

















# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------





def features_view(cfg: dict) -> dict:
    """Effective switches with their defaults and descriptions, for the
    status snapshot and the settings dialog."""
    return {name: {"enabled": feature_enabled(cfg, name), "default": default, "description": text}
            for name, (default, text) in FEATURES.items()}














def sync_coverage(status: dict, acceptance: dict) -> None:
    resolved = status.get("requirement_coverage", {}).get("original_symptom_resolved") is True
    status["requirement_coverage"] = coverage_for(acceptance.get("criteria", []), resolved)




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
    if not model_policy_allows(cfg.get("model_policy", DEFAULT_MODEL_POLICY), adapter, model):
        raise HandsoffError(
            f"{tier} reviewer profile {adapter}/{model} is denied by the mission model policy"
        )
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


def reviewer_implementation_contract(status: dict, acceptance: dict) -> dict | None:
    """Return the exact acceptance contract endorsed before implementation.

    The Phase-2 Reviewer's approval is bound to ``design_hash``. Requiring
    that hash to match the current registry gives the Implementer and the
    later implementation Reviewer one identical scope instead of a host
    paraphrase.
    """
    if not isinstance(status, dict) or not isinstance(acceptance, dict) \
            or int(status.get("phase_number", 1) or 1) < 3:
        return None
    review = status.get("design_review")
    criteria = acceptance.get("criteria")
    if not isinstance(review, dict) or review.get("decision") != "approved" \
            or not isinstance(criteria, list) or not criteria:
        return None
    current_hash = design_hash(criteria)
    if review.get("design_hash") != current_hash:
        return None
    exact = [
        {key: deepcopy(item.get(key)) for key in
         ("id", "work_item", "work_item_tag", "type", "requirement", "verification", "tests")
         if key in item}
        for item in criteria[:64] if isinstance(item, dict)
    ]
    body = {
        "schema": 1,
        "issued_by": review.get("by"),
        "design_review_attempt": review.get("attempt"),
        "design_hash": current_hash,
        "governance_hash": review.get("config_hash"),
        "criterion_hashes": [hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"),
                                                        ensure_ascii=False).encode("utf-8")).hexdigest()
                             for item in exact],
        "criteria": exact,
        "instructions": (
            "Implement and verify every criterion exactly as written. Do not broaden, narrow, "
            "or paraphrase the contract. Raise any ambiguity before editing; the implementation "
            "review will judge this same packet."
        ),
    }
    body["contract_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return body
















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












def regression_group(cfg: dict, name: str) -> dict:
    item = next((entry for entry in cfg.get("regressions", []) if entry.get("name") == name), None)
    if item is None:
        raise HandsoffError(f"unknown regression group: {name}")
    return item


RELEASE_CLASSES = ("patch", "minor", "major")




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






def command_sha256(commands: list[str]) -> str:
    return hashlib.sha256(json.dumps(commands, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()














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

MAX_CRITERIA_TRANSACTION_BYTES = 256 * 1024






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





def amendment_hash(base_design_hash: str, operations: list[dict]) -> str:
    """sha256 of the base design hash concatenated with the canonical
    operation records ({op, id, previous_hash, resulting_hash}, in order).
    Bound into the review and the Pilot approval, and recomputed at approve
    time, so a delta that drifted after review can never be approved."""
    return hashlib.sha256(
        (str(base_design_hash) + _canonical({"operations": operations})).encode("utf-8")
    ).hexdigest()




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






# --------------------------------------------------------------------------
# #33: live session status. A managed child beacons `.handsoff-live.json`
# every few seconds; `live_status` folds that liveness signal into the
# structured state (run status, human pause, the ledger-bound session
# record) into one small view the dashboard strip and `status` show.
# --------------------------------------------------------------------------





def write_live_beacon(root: Path, *, session_id: str, role: str, state: str,
                      pid: int | None, ended_at: str | None = None,
                      exit_code: int | None = None, now: datetime | None = None) -> bool:
    """Best-effort atomic write of the seven-key beacon. Returns False
    instead of raising on any OSError: the beacon is a liveness hint, never
    the authority, so a full disk or a bad path must not touch the child's
    lifecycle or the session record."""
    root = Path(root)
    if not root.is_dir():
        return False
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




def operations_path(root: Path) -> Path:
    """Return the bounded, telemetry-only operation journal for ROOT."""
    return Path(root) / OPERATIONS_FILE




def record_session_progress(root: Path, session_id: str, record: dict) -> list[dict]:
    """#215: append one validated progress record to the session, under
    the project lock; the list is bounded and the last state per criterion
    wins when the summary is computed. Returns the session's list."""
    root = Path(root).resolve()
    with project_lock(root):
        cfg = load_config(root)
        status = load_unique_json(status_path(root, cfg))
        session = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(session, dict):
            raise HandsoffError("agent session was not found")
        proposed = deepcopy(status)
        progress = proposed["agent_sessions"][session_id].setdefault("progress", [])
        progress.append({**record, "at": datetime.now(timezone.utc).isoformat()})
        del progress[:-MAX_PROGRESS_RECORDS]
        errors = validate_status_schema(proposed)
        if errors:
            raise HandsoffError(errors[0])
        commit(root, cfg, status=proposed, event_kind="implementer_progress",
               event_message=f"Implementer progress: {record['criterion']} {record['state']}",
               session_id=session_id, criterion=record["criterion"], progress_state=record["state"])
        return deepcopy(progress)




def latest_failed_implementer_progress(status: dict) -> dict | None:
    """#215: the progress summary of the most recent failed Implementer
    session of this run, with the tests it named for done criteria, for a
    relaunch's context; None when there is none."""
    sessions = status.get("agent_sessions") if isinstance(status, dict) else None
    failures = status.get("agent_failures") if isinstance(status, dict) else None
    if not isinstance(sessions, dict) or not isinstance(failures, dict):
        return None
    candidates = [(s.get("ended_at") or "", sid, s) for sid, s in sessions.items()
                  if isinstance(s, dict) and s.get("role") == "implementer" and sid in failures
                  and isinstance(failures[sid], dict) and isinstance(failures[sid].get("progress_summary"), dict)]
    if not candidates:
        return None
    _, sid, session = max(candidates)
    tests = {}
    for item in session.get("progress") or []:
        if isinstance(item, dict) and item.get("state") == "done" and item.get("test"):
            tests[item["criterion"]] = item["test"]
    return {"session_id": sid, "summary": failures[sid]["progress_summary"], "tests": tests,
            "changed_paths": list(failures[sid].get("changed_paths") or [])}


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
    root = Path(root)
    # Liveness is an optional side signal, never a reason to manufacture a
    # missing project root (or make a failed launch look active).
    if not root.is_dir():
        return False
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
               on_progress=None, env: dict | None = None,
               progress_source: str = "verify") -> list[dict]:
    """Actually execute the given commands (default: [checks].commands), in
    the project root. Each result is real evidence a criterion's evidence
    list can reference, not a sentence someone typed. Timeout comes from
    handsoff.toml's checks.timeout_seconds (default 600s) unless overridden."""
    import subprocess
    import handsoff_progress as test_progress
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
    try:
        progress_status = load_unique_json(status_path(root, cfg))
        progress_run_id = feature_hash(progress_status, read_events(root, cfg))
    except (HandsoffError, OSError, KeyError):
        progress_run_id = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    progress = test_progress.start(
        root, run_id=progress_run_id, source=progress_source,
        label="Live verification" if progress_source == "verify-live" else "Targeted verification",
        units=[f"Check {index}" for index in range(1, len(selected) + 1)],
        command_hash=command_sha256(selected),
    )
    all_units = [{"index": index, "label": f"Check {index}", "state": "queued", "total": 1,
                  "done": 0, "progress": 0.0, "elapsed_seconds": 0.0, "result": None}
                 for index, _cmd in enumerate(selected, 1)]
    progress["totals"] = test_progress.aggregate_units(all_units)
    progress["units"] = all_units[:test_progress.MAX_VISIBLE_UNITS]
    test_progress.write(root, progress, expected_execution_id=progress["execution_id"])
    for cmd in selected:
        index = len(results) + 1
        if on_progress:
            on_progress(index, len(selected), cmd, None)
        started = time.time()
        unit = all_units[index - 1]
        unit["state"] = "running"
        progress["state"] = "running"
        progress["totals"] = test_progress.aggregate_units(all_units)
        progress["units"] = all_units[:test_progress.MAX_VISIBLE_UNITS]
        test_progress.write(root, progress, expected_execution_id=progress["execution_id"])
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env={**os.environ, **env} if env else None)
            deadline = time.monotonic() + timeout
            last_heartbeat = time.monotonic()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
                try:
                    stdout, stderr = proc.communicate(timeout=min(1.0, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - last_heartbeat >= test_progress.HEARTBEAT_SECONDS:
                        unit["elapsed_seconds"] = round(time.time() - started, 2)
                        progress["totals"] = test_progress.aggregate_units(all_units)
                        progress["units"] = all_units[:test_progress.MAX_VISIBLE_UNITS]
                        if not test_progress.write(root, progress, expected_execution_id=progress["execution_id"]):
                            pass
                        last_heartbeat = time.monotonic()
            returncode = proc.returncode
            output = stdout + stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output = stdout + stderr + f"\nHANDSOFF: command timed out after {timeout} seconds"
        except OSError as exc:
            returncode = 127
            output = f"HANDSOFF: command launch failed: {exc}"
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
        unit["state"] = "timed_out" if returncode == 124 else ("passed" if returncode == 0 else "failed")
        unit["done"] = 1
        unit["progress"] = 1.0
        unit["elapsed_seconds"] = results[-1]["duration_s"]
        unit["result"] = f"exit {returncode}"
        progress["totals"] = test_progress.aggregate_units(all_units)
        progress["units"] = all_units[:test_progress.MAX_VISIBLE_UNITS]
        progress["state"] = progress["totals"]["state"]
        test_progress.write(root, progress, expected_execution_id=progress["execution_id"])
        if on_progress:
            on_progress(index, len(selected), cmd, results[-1])
    final_state = progress["totals"]["state"]
    if final_state not in test_progress.TERMINAL_STATES:
        final_state = "passed" if all(item["state"] == "passed" for item in all_units) else "failed"
    test_progress.finish(root, progress, state=final_state,
                         result=f"{sum(1 for item in all_units if item['state'] == 'passed')} of {len(all_units)} checks passed")
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










def repository_digest_from_entries(entries: dict[str, str | None]) -> str:
    """The digest `repository_digest` would produce for one scan's entries,
    so a caller that already holds the per-path scan does not list the tree
    a second time (#203: two scans can disagree on a file that only
    existed between them)."""
    pairs = [[relative, entries[relative]] for relative in sorted(entries)]
    return hashlib.sha256(_canonical({"files": pairs}).encode("utf-8")).hexdigest()






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


FALLBACK_SKIP_REASONS = {
    "invalid_profile", "adapter_unavailable", "already_attempted", "reviewer_not_independent",
    "environment_failure", "model_policy_denied", "cross_vendor_not_allowed", "insufficient_capability",
}


def _fallback_decision(action: str, reason: str, *, profile: dict | None = None,
                       skipped: list[dict] | None = None) -> dict:
    return {"action": action, "reason": reason, "profile": profile, "skipped": skipped or []}




def plan_agent_fallback(role: str, failure_category: str, fallback_entries: object,
                        adapter_availability: object, attempted_identities: object,
                        failover_count: object, max_failovers_per_role: object,
                        implementer_profile: object = None, *, model_policy: object = None,
                        required_tier: str | None = None) -> dict:
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
    previous_adapter = None
    for identity in attempted_identities:
        if not isinstance(identity, (list, tuple)) or len(identity) != 2 \
                or identity[0] not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError("fallback planner attempted identity is invalid")
        try:
            model = validate_agent_model(identity[1])
        except HandsoffError as exc:
            raise HandsoffError("fallback planner attempted identity is invalid") from exc
        attempted.add((identity[0], model))
        previous_adapter = identity[0]
    policy = validate_model_policy(model_policy or DEFAULT_MODEL_POLICY)
    if required_tier is not None and required_tier not in ADAPTIVE_ROUTING_TIERS:
        raise HandsoffError("fallback planner required_tier is invalid")
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
        catalog = adaptive_catalog_profile(*identity)
        # Authentication and local runtime failures are not evidence that a
        # different provider is required. Keep the skipped sequence for the
        # incident UI, but never select or launch one of these profiles.
        if failure_category in {"auth_failure", "runtime_environment"}:
            skipped.append({"index": index, "reason": "environment_failure"})
        elif not model_policy_allows(policy, *identity):
            skipped.append({"index": index, "reason": "model_policy_denied"})
        elif previous_adapter and profile["adapter"] != previous_adapter \
                and (failure_category != "rate_limit" or not policy["quota_substitution"]):
            skipped.append({"index": index, "reason": "cross_vendor_not_allowed"})
        elif required_tier is not None and (catalog is None or
                ADAPTIVE_ROUTING_TIERS.index(catalog[0]) < ADAPTIVE_ROUTING_TIERS.index(required_tier)):
            skipped.append({"index": index, "reason": "insufficient_capability"})
        elif not adapter_availability[profile["adapter"]]:
            skipped.append({"index": index, "reason": "adapter_unavailable"})
        elif identity in attempted:
            skipped.append({"index": index, "reason": "already_attempted"})
        elif role == "reviewer" and identity == implementer_identity:
            skipped.append({"index": index, "reason": "reviewer_not_independent"})
        else:
            reason = "quota_substitution" if previous_adapter and profile["adapter"] != previous_adapter else "eligible_fallback"
            return _fallback_decision("select", reason, profile=profile, skipped=skipped)
    reason = "environment_failure" if failure_category in {"auth_failure", "runtime_environment"} else "fallback_exhausted"
    return _fallback_decision("pilot_pause", reason, skipped=skipped)


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
        # #317: the shared rule, not a local one. This used to exclude only
        # an exact run_kind of "test", so the 100 archives written before #49
        # added the field all counted as product, the 81 fixture runs among
        # them included.
        if not isinstance(record, dict) or classify_archive_record(record, path.name) == "test":
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


#: #296: the v0.3.80 release was published and its issues closed while the
#: run sat at Phase 7, with the wheel never installed and `verify-live`
#: never run. A run may always be abandoned, but abandoning it must say so.
#: "aborted": stopped before the release was published.
#: "released_unverified": published, but the installed artifact was never
#: verified, so the release exists and the claim about it does not.
RUN_OUTCOMES = ("closed", "not_planned", "aborted", "released_unverified")
#: The outcomes that describe a run which did NOT reach verified Phase 8.
UNVERIFIED_RUN_OUTCOMES = ("aborted", "released_unverified")


def run_is_live_verified(status: dict, cfg: dict | None = None) -> bool:
    """Has the installed artifact actually been verified where users meet it?

    Phase 8 and 100% are claims the run makes about itself; the live
    verification id is the evidence behind them. #296 exists because the
    two came apart.
    """
    if not isinstance(status, dict):
        return False
    if (cfg or {}).get("require_live_verification", True) and not status.get("live_verification_id"):
        return False
    try:
        phase = int(status.get("phase_number") or 0)
        progress = int(status.get("progress") or 0)
    except (TypeError, ValueError):
        return False
    return phase >= 8 and progress >= 100


def unverified_close_outcome(status: dict) -> str:
    """Name what an early close actually is, rather than calling it clean."""
    published = bool((status or {}).get("release_published")
                     or (status or {}).get("release_transaction")
                     or (status or {}).get("deployment_approved"))
    return "released_unverified" if published else "aborted"


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
MAX_QUESTION_BATCH = 16
QUESTION_FORM_REQUIRED_KEYS = {"text", "options"}
QUESTION_FORM_OPTIONAL_KEYS = {"recommended"}


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
                      runner=subprocess.run,
                      known_handsoff_closed: set[int] | None = None,
                      checkpoint: Callable[[int, str, str], None] | None = None) -> dict:
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
    known_handsoff_closed = set(known_handsoff_closed or ())
    # Read every issue before the first mutation.  A pre-existing closure is
    # safe only when this close transaction already persisted that Handsoff
    # closed the same item.  Human/unknown closures pause the whole report so
    # a comment or parent edit cannot leak out before the conflict is known.
    issue_views: dict[int, dict] = {}
    for item in items:
        number = item["number"]
        view = _gh(["issue", "view", str(number), "--json", "body,url,state,comments"],
                   runner=runner, cwd=root)
        try:
            issue = json.loads(view.stdout) if view.returncode == 0 else None
        except ValueError:
            issue = None
        if not isinstance(issue, dict) or str(issue.get("state") or "").upper() not in {"OPEN", "CLOSED"}:
            return {"posted": [], "skipped": [], "reason": "issue_state",
                    "detail": f"issue #{number} state could not be read; nothing posted"}
        existing = [c.get("body") if isinstance(c, dict) else c for c in (issue.get("comments") or [])]
        has_report = any(isinstance(c, str) and c.lstrip().startswith(
            REPORT_MARKER.split("{head}")[0]) for c in existing)
        report_heads = {
            match.group(1) for comment in existing if isinstance(comment, str)
            for match in re.finditer(r"<!--\s*handsoff-report\s+([0-9a-f]{64})\s*-->", comment)
        }
        closure_heads = {
            match.group(1) for comment in existing if isinstance(comment, str)
            for match in re.finditer(r"Closed by the Handsoff run report \(([0-9a-f]{12})\)", comment)
        }
        canonically_attributed = any(
            any(report_head.startswith(close_head) for report_head in report_heads)
            for close_head in closure_heads
        )
        if str(issue.get("state")).upper() == "CLOSED" \
                and not ((number in known_handsoff_closed and has_report) or canonically_attributed):
            return {"posted": [], "skipped": [], "reason": "issue_state",
                    "detail": f"issue #{number} is closed without attributable Handsoff ownership; nothing posted"}
        issue_views[number] = issue
    posted, skipped = [], []
    body = marker + "\n" + text
    def save(number: int, operation: str, state: str) -> None:
        if checkpoint is not None:
            checkpoint(number, operation, state)

    for item in items:
        number = item["number"]
        issue = issue_views[number]
        existing = [c.get("body") if isinstance(c, dict) else c for c in (issue.get("comments") or [])]
        # any earlier report on this ticket counts: the head moves with every
        # event, the marker's prefix does not. The comment is the only
        # part that is never repeated; close and tick are retried below
        # until each has actually happened (review F1, tranche 2).
        already = any(isinstance(c, str) and c.lstrip().startswith(REPORT_MARKER.split("{head}")[0]) for c in existing)
        url = None
        if already:
            skipped.append({"number": number, "reason": "already posted"})
            save(number, "commented", "complete")
        else:
            save(number, "commented", "intent")
            comment = _gh(["issue", "comment", str(number), "--body", body], runner=runner, cwd=root)
            if comment.returncode != 0:
                skipped.append({"number": number, "reason": "comment failed"})
                continue
            save(number, "commented", "complete")
            url = comment.stdout.strip().splitlines()[-1] if comment.stdout.strip() else None
        closed = str(issue.get("state") or "").upper() == "CLOSED"
        did_close = did_tick = False
        if not closed:
            # Persist before the provider call.  If the response/read-back is
            # lost, a retry may safely reconcile this attempted operation
            # instead of misclassifying its own close as human/unknown.
            save(number, "closed", "intent")
            close_result = _gh(
                ["issue", "close", str(number), "-c", f"Closed by the Handsoff run report ({head[:12]})."],
                runner=runner, cwd=root,
            )
            if close_result.returncode == 0:
                save(number, "closed", "dispatched")
            close_readback = _gh(["issue", "view", str(number), "--json", "state"],
                                 runner=runner, cwd=root)
            try:
                closed = close_readback.returncode == 0 and str(
                    json.loads(close_readback.stdout).get("state") or "").upper() == "CLOSED"
            except ValueError:
                closed = False
            did_close = closed
        if closed:
            save(number, "closed", "complete")
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
                save(number, "ticked", "intent")
                _gh(["issue", "edit", str(parent), "--body", new_body], runner=runner, cwd=root)
                parent_readback = _gh(["issue", "view", str(parent), "--json", "body"],
                                      runner=runner, cwd=root)
                try:
                    observed_parent = json.loads(parent_readback.stdout).get("body") or "" \
                        if parent_readback.returncode == 0 else ""
                except ValueError:
                    observed_parent = ""
                ticked = bool(re.search(rf"^\s*- \[x\] #{number}\b", observed_parent,
                                        re.MULTILINE | re.IGNORECASE))
                did_tick = ticked
            elif re.search(rf"^\s*- \[x\] #{number}\b", parent_body, re.MULTILINE | re.IGNORECASE):
                ticked = True
        if ticked or not parent:
            save(number, "ticked", "complete")
        # Even a no-op read-back is returned so a fresh close episode can
        # persist that every mandatory item is already complete.  ``skipped``
        # still records that no duplicate comment was sent.
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
