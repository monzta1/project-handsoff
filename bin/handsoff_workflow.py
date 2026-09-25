#!/usr/bin/env python3
"""Handsoff workflow state machine: what a proposed transition must satisfy.

#284 stage 7, the last of the seven subsystems the ticket names. Forty-seven
symbols: the audit that decides whether a phase may be left
(`compute_errors` and the per-concern error functions it composes), the gates
that refuse an action outright (`lane_gate_refusal`, `ci_gate_errors`,
`full_design_required`), the criteria transaction that validates a whole
batch of criterion edits before any of it is written
(`plan_criteria_transaction`), the work-item derivation the phases are scored
against, and the launch rules that bind to a phase.

This is the decide side, against handsoff_projection's read side: everything
here answers "may this state be written", and nothing here writes. The
persistence itself stays with the ledger, which is what keeps criterion 3 --
validate the proposed state before persisting it -- a property of the
boundary rather than of each call site.

Layer: core -> routing -> config -> ledger -> resources -> agent_runtime
-> here. Every import is module level.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import (
    HandsoffError, _canonical, acceptance_hash, criterion_spec_hash, design_hash,
)
from handsoff_config import AGENT_ROLES, normalized_test_footprint
from handsoff_routing import adaptive_deployment_approval_required
from handsoff_ledger import (
    VERIFICATION_REQUIREMENTS, _design_errors, _design_review_errors, _last_hash,
    config_hash, criterion_work_item, criterion_work_item_id, derive_work_item_registry,
    effective_work_items, evidence_drift, feature_enabled, item_progress, open_amendment,
    overall_item_progress, scope_hash_matches, verification_log_path, work_item_delivery,
    work_item_scope_hash,
)
from handsoff_resources import RUNTIME_MANIFEST_FILE, engine_resource_path, engine_root
from handsoff_agent_runtime import (
    CRITERIA_TRANSACTION_OPS, VERIFICATION_KINDS, active_regression_request,
    current_review_attempt, effective_review_cap, validate_acceptance_schema,
    validate_status_schema,
)



def lane_gate_refusal(status: dict, action: str) -> str | None:
    """Return the refusal for an action unavailable to a design-lane run."""
    lane = status.get("lane")
    if lane == "design" and action in {"advance_4", "record-review", "deployment-gate", "verify-live"}:
        return f"design lane refuses {action}"
    if lane == "review" and action in {"advance_4", "deployment-gate", "verify-live"}:
        return f"review lane refuses {action}"
    if lane == "review" and action == "advance_1":
        return "review lane refuses advance_1"
    return None


WORK_ITEM_STATES = {
    "done", "blocked", "in_review", "awaiting_approval", "recovering",
    "in_progress", "not_started", "unscoped",
}


BASELINE_NOT_APPLICABLE = "not_applicable"


CHECKLIST_VALUES = {
    "symptom_reproduced": {"yes", "not_applicable"},
    "symptom_resolved": {"yes"},
    "all_criteria_verified": {"yes"},
    "evidence_attached": {"yes"},
}


RULE_COMMANDS = ("launch", "packet")


RULE_WHEN_KEYS = {"command", "role", "phase_in", "amendment", "field"}


RULES_DIR = "rules"


PROJECT_RULES_DIR = "handsoff-rules"


MAX_RULE_BYTES = 16 * 1024


def _validate_rule(rule: object, source: str) -> dict:
    if not isinstance(rule, dict):
        raise HandsoffError(f"rule {source}: not an object")
    for key in ("id", "cause", "when", "refuse"):
        if key not in rule:
            raise HandsoffError(f"rule {source}: missing {key}")
    if not isinstance(rule["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", rule["id"]):
        raise HandsoffError(f"rule {source}: id must be a short lowercase slug")
    if not isinstance(rule["refuse"], str) or not rule["refuse"].strip() or len(rule["refuse"]) > 512:
        raise HandsoffError(f"rule {source}: refuse must be 1 to 512 characters")
    cause = rule["cause"]
    if not isinstance(cause, dict) or not isinstance(cause.get("event"), str) or not isinstance(cause.get("at"), str):
        raise HandsoffError(f"rule {source}: cause needs at least event and at")
    when = rule["when"]
    if not isinstance(when, dict) or set(when) - RULE_WHEN_KEYS or when.get("command") not in RULE_COMMANDS:
        raise HandsoffError(f"rule {source}: when.command must be launch or packet and keys limited to "
                            + ", ".join(sorted(RULE_WHEN_KEYS)))
    if "role" in when and when["role"] not in AGENT_ROLES:
        raise HandsoffError(f"rule {source}: when.role must be a managed role")
    if when["command"] == "launch":
        phases = when.get("phase_in")
        if phases is not None and (not isinstance(phases, list) or not phases
                                   or not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 8 for p in phases)):
            raise HandsoffError(f"rule {source}: when.phase_in must be a non-empty list of phase numbers")
        if "amendment" in when and not isinstance(when["amendment"], bool):
            raise HandsoffError(f"rule {source}: when.amendment must be boolean")
        if "field" in when:
            raise HandsoffError(f"rule {source}: when.field belongs to packet rules")
    else:
        if not isinstance(when.get("field"), str) or not when["field"].strip():
            raise HandsoffError(f"rule {source}: a packet rule needs when.field")
        allowed = rule.get("allowed")
        max_chars = rule.get("max_chars")
        if allowed is None and max_chars is None:
            raise HandsoffError(f"rule {source}: a packet rule needs allowed (exact values) or max_chars")
        if allowed is not None and (not isinstance(allowed, list) or not allowed or not all(isinstance(a, str) for a in allowed)):
            raise HandsoffError(f"rule {source}: allowed must be a non-empty list of strings")
        if max_chars is not None and (not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1):
            raise HandsoffError(f"rule {source}: max_chars must be a positive integer")
        if "recover_as" in rule and (allowed is None or rule["recover_as"] not in allowed):
            raise HandsoffError(f"rule {source}: recover_as must be one of allowed")
        if "recover" in rule and rule["recover"] != "truncate":
            raise HandsoffError(f"rule {source}: recover may only be truncate")
        if rule.get("recover") == "truncate" and max_chars is None:
            raise HandsoffError(f"rule {source}: recover truncate needs max_chars")
        for key in ("phase_in", "amendment"):
            if key in when:
                raise HandsoffError(f"rule {source}: when.{key} belongs to launch rules")
    return rule


def load_launch_rules(root: Path | None = None) -> list[dict]:
    """Every rule the engine ships (rules/*.json in the runtime manifest)
    plus a project's own handsoff-rules/*.json. Ids are unique across both;
    rules/proposed/ is never read. A malformed file is an error, never a
    silently skipped rule."""
    rules: list[dict] = []
    seen: set[str] = set()
    directories = [engine_resource_path(RULES_DIR)]
    if root is not None:
        directories.append(Path(root) / PROJECT_RULES_DIR)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stat().st_size > MAX_RULE_BYTES:
                raise HandsoffError(f"rule {path.name}: larger than {MAX_RULE_BYTES} bytes")
            try:
                rule = _validate_rule(json.loads(path.read_text(encoding="utf-8")), path.name)
            except ValueError as exc:
                raise HandsoffError(f"rule {path.name}: invalid JSON ({exc})") from exc
            if rule["id"] in seen:
                raise HandsoffError(f"rule {path.name}: duplicate rule id {rule['id']}")
            seen.add(rule["id"])
            rules.append({**rule, "source": str(path)})
    return rules


#: Project files that decide what a reviewer saw and could run. Contents
#: are hashed, never stored; .env and credential files are never in the set.
RULES_SET_PROJECT_FILES = ("handsoff.toml", ".claude/settings.json", ".claude/settings.local.json",
                           ".codex/config.toml", "AGENTS.md", "CLAUDE.md")


def rules_set_entries(root: Path) -> dict[str, str | None]:
    """Path -> sha256 of contents, or None when absent. Engine entries
    (reviewer prompt, rules/*.json, manifest version) are keyed 'engine:'."""
    root = Path(root).resolve()
    entries: dict[str, str | None] = {}
    for relative in RULES_SET_PROJECT_FILES:
        path = root / relative
        try:
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            entries[relative] = None
    prompt = engine_resource_path("prompts/reviewer.md")
    try:
        entries["engine:prompts/reviewer.md"] = hashlib.sha256(prompt.read_bytes()).hexdigest() if prompt.is_file() else None
    except OSError:
        entries["engine:prompts/reviewer.md"] = None
    for rule in load_launch_rules(root):
        try:
            entries[f"engine:{Path(rule['source']).name}" if "/rules/" in rule["source"].replace(str(root), "")
                    else f"project:{Path(rule['source']).name}"] = hashlib.sha256(Path(rule["source"]).read_bytes()).hexdigest()
        except OSError:
            continue
    try:
        entries["engine:version"] = json.loads((engine_root() / RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        entries["engine:version"] = None
    return entries


def rules_set_hash(root: Path, cfg: dict | None = None) -> str:
    return hashlib.sha256(_canonical(rules_set_entries(root)).encode("utf-8")).hexdigest()


def rules_set_diff(root: Path, recorded_entries: dict | None) -> list[str]:
    """Which entries differ from a recorded snapshot; every entry when no
    snapshot was recorded (an older decision carries the hash only)."""
    current = rules_set_entries(root)
    if not isinstance(recorded_entries, dict):
        return sorted(current)
    changed = [key for key in sorted(set(current) | set(recorded_entries))
               if current.get(key) != recorded_entries.get(key)]
    return changed


def rules_binding_errors(root: Path | None, cfg: dict, decision: dict | None, label: str) -> list[str]:
    """#170: refuse when the rules set changed since `decision` was recorded.
    A decision without rules_hash (recorded before the field existed) is
    accepted as it stands. With the switch off nothing is checked."""
    if root is None or not isinstance(decision, dict) or not feature_enabled(cfg, "review_binds_rules"):
        return []
    recorded = decision.get("rules_hash")
    if not recorded:
        return []
    if recorded == rules_set_hash(root, cfg):
        return []
    changed = rules_set_diff(root, decision.get("rules_entries"))
    shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
    return [f"{label}: the rules set changed since it was recorded ({shown}); record it again"]


def append_verification(root: Path, cfg: dict, *, kind: str, ok: bool,
                        by: str, criteria: list[dict], results: list[dict] | None = None,
                        commands: list[str] | None = None,
                        description: str | None = None,
                        acceptance_digest: str | None = None,
                        config_digest: str | None = None,
                        binding: dict | None = None, executed: bool = True,
                        reused_from: str | None = None,
                        feature_hash: str | None = None,
                        repository_digest: str | None = None,
                        attempts: list[dict] | None = None,
                        rules: dict | None = None) -> dict:
    """Append a hash-chained evidence record. Caller must hold project_lock.

    #43: `binding` (command to verification_binding hash), `executed`,
    `reused_from`, and `feature_hash` are part of the hashed record so a
    reused record cannot later be passed off as an executed one. Legacy
    records written before these fields existed simply lack them and are
    never reuse sources.

    The chain proves a record was not altered AFTER it was written; it says
    nothing about whether the record was meaningful WHEN it was written.
    That is what this validates: an empty actor, an unknown kind, or a
    record naming zero criteria would hash and chain just as cleanly as a
    real one, so those are rejected here, structurally, before anything
    is appended."""
    if not isinstance(by, str) or not by.strip():
        raise HandsoffError("verification record: 'by' must be a non-empty string")
    if kind not in VERIFICATION_KINDS:
        raise HandsoffError(f"verification record: 'kind' must be one of {sorted(VERIFICATION_KINDS)}")
    if not criteria or not all(isinstance(c, dict) and c.get("id") for c in criteria):
        raise HandsoffError("verification record: 'criteria' must be a non-empty list of criteria with ids")
    if binding is not None and not isinstance(binding, dict):
        raise HandsoffError("verification record: 'binding' must be an object or null")
    if not isinstance(executed, bool):
        raise HandsoffError("verification record: 'executed' must be a boolean")
    if reused_from is not None and (not isinstance(reused_from, str) or not reused_from.strip()):
        raise HandsoffError("verification record: 'reused_from' must be a run id or null")
    if executed and reused_from is not None:
        raise HandsoffError("verification record: an executed record cannot name a reuse source")
    path = verification_log_path(root, cfg)
    prev_hash = _last_hash(path)
    record = {
        "run_id": f"vr-{uuid.uuid4().hex}",
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "ok": bool(ok),
        "by": by,
        "criteria": [c["id"] for c in criteria],
        "criterion_hashes": {c["id"]: criterion_spec_hash(c) for c in criteria},
        "results": results or [],
        "commands": list(commands or []),
        "description": description or "",
        "acceptance_hash": acceptance_digest,
        "config_hash": config_digest,
        "binding": binding,
        "executed": executed,
        "reused_from": reused_from,
        "feature_hash": feature_hash,
        "repository_digest": repository_digest,
        "prev_hash": prev_hash,
    }
    if attempts is not None:
        record["attempts"] = attempts  # #169: only a repeat record carries it
    if rules is not None:
        record["rules_hash"] = rules["rules_hash"]  # #170: a live record binds its rules set
        record["rules_entries"] = rules["rules_entries"]
    record["hash"] = hashlib.sha256((_canonical(record) + prev_hash).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def _is_green(criteria: list[dict]) -> bool:
    return bool(criteria) and all(c.get("state") == "passing" for c in criteria)


def coverage_for(criteria: list[dict], resolved: bool = False) -> dict:
    counts = {"passing": 0, "failing": 0, "not_tested": 0, "blocked": 0}
    for criterion in criteria:
        state = criterion.get("state")
        if state in counts:
            counts[state] += 1
    return {**counts, "original_symptom_resolved": bool(resolved)}


def valid_evidence_kinds(criterion: dict, verifications: list[dict]) -> set[str]:
    """Which required evidence kinds this criterion actually has a valid,
    spec-matching, successful record for right now. `verifications` may be
    a list of already-loaded records, an in-memory list a caller just
    appended to, or any combination; only ok=True, criterion-id-matching,
    current-spec-hash-matching records count."""
    cid = criterion.get("id")
    spec = criterion_spec_hash(criterion)
    kinds: set[str] = set()
    for record in verifications:
        if not isinstance(record, dict):
            continue
        if (record.get("ok") is True and cid in record.get("criteria", [])
                and record.get("criterion_hashes", {}).get(cid) == spec):
            kinds.add(record.get("kind"))
    return kinds


def _evidence_errors(criteria: list[dict], verifications: list[dict]) -> list[str]:
    errors: list[str] = []
    for criterion in criteria:
        if criterion.get("state") != "passing":
            continue
        cid = criterion.get("id")
        required = VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set())
        missing = required - valid_evidence_kinds(criterion, verifications)
        if missing:
            errors.append(f"evidence gate: passing criterion {cid} lacks valid {', '.join(sorted(missing))} evidence")
    return errors


def criterion_baseline(criterion: dict, verifications: list[dict]) -> dict | None:
    """#165: the newest VALID baseline (kind baseline, ok True) bound to this
    criterion's current spec hash, or None. A baseline recorded against an
    older wording of the criterion does not count: the claim changed."""
    cid = criterion.get("id")
    spec = criterion_spec_hash(criterion)
    found = None
    for record in verifications:
        if (isinstance(record, dict) and record.get("kind") == "baseline" and record.get("ok") is True
                and cid in record.get("criteria", []) and record.get("criterion_hashes", {}).get(cid) == spec):
            found = record
    return found


def baseline_errors(criteria: list[dict], verifications: list[dict], cfg: dict) -> list[str]:
    """#165: with features.failing_first on, every automated criterion that
    reads passing must have a valid failing run behind it, unless it says
    baseline = not_applicable with a reason. Off: nothing is asked."""
    if not feature_enabled(cfg, "failing_first"):
        return []
    errors: list[str] = []
    for criterion in criteria:
        if criterion.get("state") != "passing":
            continue
        if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
            continue
        cid = criterion.get("id")
        if criterion.get("baseline") == BASELINE_NOT_APPLICABLE:
            if not str(criterion.get("baseline_reason") or "").strip():
                errors.append(f"baseline gate: {cid} says baseline not_applicable without a reason")
            continue
        if criterion_baseline(criterion, verifications) is None:
            errors.append(f"baseline gate: {cid} passed without a recorded failing run; run handsoff_supervisor.py "
                          f"verify --criterion {cid} --expect-fail --by ACTOR on the tree before the feature, "
                          f"or mark it --baseline not_applicable --baseline-reason TEXT")
    return errors


def _review_errors(status: dict, acceptance: dict, cfg: dict, root: Path | None = None) -> list[str]:
    errors: list[str] = []
    review = status.get("review")
    if not isinstance(review, dict):
        return ["review gate: Phase 6+ requires a recorded independent review"]
    if review.get("acceptance_hash") != acceptance_hash(acceptance.get("criteria", [])):
        errors.append("review gate: acceptance changed since review; record a new review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("review gate: workflow policy changed since review; record a new review")
    errors.extend(rules_binding_errors(root, cfg, review, "review gate"))  # #170
    if "work_items" in acceptance and not scope_hash_matches(review.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
        errors.append("review gate: work-item scope changed since review; record a new review")
    reviewer = review.get("by")
    if not reviewer:
        errors.append("review gate: review must identify its reviewer")
    implementer = status.get("implemented_by")
    if reviewer and implementer \
            and reviewer.strip().casefold() == implementer.strip().casefold():
        errors.append("review gate: reviewer must differ from implementer, no self-approval")
    adopted = status.get("implementation_adopted")
    if isinstance(adopted, dict):
        for field, label in (("commit_author", "adopted commit author"),
                             ("adopting_actor", "adopting actor")):
            identity = adopted.get(field)
            if identity and reviewer and reviewer.strip().casefold() == str(identity).strip().casefold():
                errors.append(f"review gate: reviewer must differ from {label}")
    checklist = review.get("checklist", {})
    for field, allowed in CHECKLIST_VALUES.items():
        if checklist.get(field) not in allowed:
            errors.append(f"review gate: checklist field '{field}' is incomplete")
    return errors


GATE_PROGRESS_WEIGHTS = (
    ("initialized", 5), ("design_reviewed", 15), ("design_approved", 25), ("evidence", 45),
    ("symptom", 50), ("review", 65), ("deployment", 80), ("live", 95), ("complete", 100),
)


def gate_progress(status: dict, acceptance: dict) -> dict:
    """#102: progress as gates cleared, not phase index. A run with an
    approved design used to read 8 percent because progress was
    phase-weighted; here it reads 25. Gates are cumulative: the percent is
    the weight of the highest gate cleared, `cleared` lists them in order."""
    criteria = acceptance.get("criteria", []) if isinstance(acceptance, dict) else []
    automated = [c for c in criteria if "checks" in VERIFICATION_REQUIREMENTS.get(c.get("verification"), set())]
    review = status.get("design_review") if isinstance(status, dict) else None
    facts = {
        "initialized": bool(status),
        "design_reviewed": isinstance(review, dict) and review.get("decision") == "approved",
        "design_approved": isinstance(status.get("design_approved"), dict),
        "evidence": bool(automated) and all(c.get("state") == "passing" for c in automated),
        "symptom": bool((status.get("requirement_coverage") or {}).get("original_symptom_resolved")
                        or status.get("original_symptom_evidence_id")),
        "review": isinstance(status.get("review"), dict) and bool(status.get("reviewed_by")),
        "deployment": isinstance(status.get("deployment_approved"), dict),
        "live": bool(status.get("live_verification_id")),
        "complete": status.get("status") == "complete" or int(status.get("phase_number", 0) or 0) >= 8,
    }
    cleared = [name for name, _ in GATE_PROGRESS_WEIGHTS if facts[name]]
    percent = max((weight for name, weight in GATE_PROGRESS_WEIGHTS if facts[name]), default=0)
    return {"percent": percent, "cleared": cleared}


def _valid_symptom_record(status: dict, criteria: list[dict], verifications: list[dict]) -> dict | None:
    evidence_id = status.get("original_symptom_evidence_id")
    primary = {c["id"]: c for c in criteria if c.get("type") == "primary_fix"}
    for record in verifications:
        if record.get("run_id") != evidence_id or record.get("ok") is not True:
            continue
        for cid in set(record.get("criteria", [])) & set(primary):
            if record.get("criterion_hashes", {}).get(cid) == criterion_spec_hash(primary[cid]):
                return record
    return None


def _valid_review_anchor(status: dict) -> dict | None:
    """A review lane anchors its evidence to the adopted implementation.

    Review lanes have no original symptom to resolve, but they must still
    carry an immutable commit identity and concrete provenance before they
    can cross the implementation gates.
    """
    adopted = status.get("implementation_adopted")
    if not isinstance(adopted, dict):
        return None
    if not str(adopted.get("sha") or "").strip():
        return None
    if not str(adopted.get("commit_author") or "").strip():
        return None
    if not str(adopted.get("adopting_actor") or "").strip():
        return None
    return adopted


def compute_errors(status: dict, acceptance: dict, cfg: dict, *, now: datetime | None = None,
                   verifications: list[dict] | None = None,
                   verification_problems: list[str] | None = None,
                   root: Path | None = None) -> list[str]:
    """Every rule a transition must satisfy, evaluated against WHATEVER
    status dict is passed in. Callers that want to gate a transition must
    pass the PROPOSED status, the one they are about to write, not the one
    already on disk: this function has no way to know which you meant, and
    checking the wrong one is exactly how the original tool let an
    unguarded write through."""
    now = now or datetime.now(timezone.utc)
    errors = validate_status_schema(status)
    errors += validate_acceptance_schema(acceptance)
    errors += [f"verification ledger: {p}" for p in (verification_problems or [])]
    records = verifications or []
    actual_verification_head = records[-1].get("hash") if records else "GENESIS"
    if isinstance(status, dict) and status.get("verification_head") != actual_verification_head:
        errors.append("verification ledger: tail does not match the anchored head; evidence was deleted or an append was interrupted")
    if errors:
        return errors  # a malformed shape makes every gate below meaningless

    criteria = acceptance.get("criteria", [])
    gate_criteria = criteria
    registry = acceptance.get("work_items")
    if isinstance(registry, list):
        required_ids = {item.get("id") for item in registry if item.get("required", True)}
        gate_criteria = [criterion for criterion in criteria
                         if criterion_work_item(criterion, registry) in required_ids
                         or criterion_work_item(criterion, registry) == "unattributed"]
    coverage = status.get("requirement_coverage", {})
    green = _is_green(gate_criteria)
    resolved = coverage.get("original_symptom_resolved") is True
    phase = int(status.get("phase_number", 0) or 0)
    progress = float(status.get("progress", 0) or 0)
    evidence_errors = _evidence_errors(gate_criteria, verifications or [])
    if phase >= 6 or progress >= 95:
        # #165: the failing-first gate rides with the evidence gate, so a
        # green with no red behind it blocks Phase 6+ and 95%+ alike.
        evidence_errors.extend(baseline_errors(gate_criteria, verifications or [], cfg))
    if root is not None and phase >= 5:
        drift = evidence_drift(root, cfg, acceptance, records)
        for cid in drift["stale"]:
            paths = ", ".join(drift.get("changed_paths", []))
            suffix = f"; changed paths: {paths}" if paths else ""
            errors.append(f"evidence drift: {cid} was verified on a different repository digest{suffix}; "
                          f"re-run handsoff_supervisor.py verify --criterion {cid} --by ACTOR")
    expected_coverage = coverage_for(criteria, resolved)
    review_lane = status.get("lane") == "review"
    symptom_record = _valid_symptom_record(status, gate_criteria, verifications or [])
    anchor_record = _valid_review_anchor(status) if review_lane else symptom_record

    if status.get("feature") != acceptance.get("feature"):
        errors.append("state gate: status and acceptance describe different features")
    if coverage != expected_coverage:
        errors.append("state gate: requirement_coverage does not match the acceptance registry")
    if phase == 7 and status.get("status") not in {"awaiting_approval", "ready_to_deploy"}:
        errors.append("state gate: Phase 7 requires status 'awaiting_approval' or 'ready_to_deploy'")

    if phase >= 3 and full_design_required(status, acceptance, cfg):
        errors.extend(_design_review_errors(status, acceptance, cfg))
        errors.extend(_design_errors(status, acceptance, cfg, root))
    # #42: an open amendment pins the run to the phase and progress it was
    # opened at, forward and back, until it is approved or escalated.
    errors.extend(amendment_freeze_errors(status))

    if phase >= 6 and (not green or (not anchor_record) or evidence_errors):
        errors.append("phase gate: every criterion and the original symptom must have verified evidence before Phase 6+")
        if review_lane and not anchor_record:
            errors.append("review anchor gate: review lane requires a valid implementation_adopted record with sha, commit_author, and adopting_actor")
        elif resolved and not symptom_record:
            errors.append("symptom gate: resolved original symptom must reference a successful verification run")
        errors.extend(evidence_errors)
    if progress >= 95 and (not green or not anchor_record or evidence_errors):
        errors.append("progress gate: 95%+ requires verified acceptance and a resolved original symptom")
        if review_lane and not anchor_record:
            errors.append("review anchor gate: review lane requires a valid implementation_adopted record with sha, commit_author, and adopting_actor")
        if phase < 6:
            errors.extend(evidence_errors)  # name the baseline gaps here too (#165)
    if "work_items" in acceptance and progress >= 95:
        unfinished = [item for item in derive_work_items(status, acceptance, cfg)["items"]
                      if item.get("required") and item.get("status") != "done"]
        for item in unfinished:
            if item.get("status") == "unscoped":
                tag = f"#{item['number']}" if item.get("kind") == "issue" else item["id"][4:]
                errors.append(f"progress gate: required work item {item['id']} has no acceptance criteria (unscoped); run handsoff_supervisor.py work-item-remove {item['id']} --by ACTOR or tag a criterion [{tag}]")
            else:
                errors.append(f"progress gate: required work item {item['id']} must be done before 95%+")
    if status.get("status") in ("ready_to_deploy", "awaiting_approval", "complete", "review_complete") and (not green or not anchor_record or evidence_errors):
        errors.append("status gate: acceptance registry is not fully green")
    if "work_items" in acceptance and (phase >= 8 or status.get("status") == "complete"):
        unfinished = [item for item in derive_work_items(status, acceptance, cfg)["items"]
                      if item.get("required") and item.get("status") != "done"]
        for item in unfinished:
            if item.get("status") == "unscoped":
                tag = f"#{item['number']}" if item.get("kind") == "issue" else item["id"][4:]
                errors.append(f"work items gate: required work item {item['id']} has no acceptance criteria (unscoped); run handsoff_supervisor.py work-item-remove {item['id']} --by ACTOR or tag a criterion [{tag}]")
            else:
                errors.append(f"work items gate: required work item {item['id']} is {item['status']}; a run cannot complete while it is unfinished")
        # #116: every required item names who implemented it, or the
        # completion audit is silently weaker for items added mid-run.
        delivery = status.get("work_item_delivery")
        if isinstance(delivery, dict):
            for item in derive_work_items(status, acceptance, cfg)["items"]:
                record = delivery.get(item["id"])
                if item.get("required") and item.get("status") != "unscoped" \
                        and (not isinstance(record, dict) or not record.get("implemented_by")):
                    errors.append(f"work items gate: work item {item['id']} has no implemented_by; run handsoff_supervisor.py work-item-update {item['id']} --by ACTOR --implemented-by ACTOR")

    if phase >= 6:
        implemented_by = status.get("implemented_by")
        if not implemented_by:
            errors.append("review gate: Phase 6+ requires 'implemented_by' to be recorded")
        errors.extend(_review_errors(status, acceptance, cfg, root))

    if adaptive_deployment_approval_required(status, cfg) and phase >= 8:
        approval = status.get("deployment_approved")
        if not approval or not approval.get("at"):
            errors.append("deployment gate: Phase 8 requires a recorded deployment approval")
        elif approval.get("acceptance_hash") != acceptance_hash(criteria):
            errors.append("deployment gate: the acceptance registry changed since approval was given, re-approve")
        elif approval.get("config_hash") != config_hash(cfg):
            errors.append("deployment gate: workflow policy changed since approval was given, re-approve")
        else:
            errors.extend(rules_binding_errors(root, cfg, approval, "deployment gate"))  # #170

    if phase >= 3 and pending_design_decline(status):
        errors.append("design gate: a decline is pending the independent reviewer's word; approve closes the run as not planned, changes send it back")  # #177
    if phase >= 7:
        errors.extend(ci_gate_errors(status))  # #181

    if phase >= 8:
        if progress != 100 or status.get("status") != "complete":
            errors.append("live gate: Phase 8 requires progress 100 and status 'complete'")
        if cfg.get("require_live_verification", True):
            live_id = status.get("live_verification_id")
            record = next((r for r in (verifications or []) if r.get("run_id") == live_id), None)
            approval = status.get("deployment_approved") or {}
            if not record or record.get("kind") != "live" or record.get("ok") is not True:
                errors.append("live gate: Phase 8 requires a successful live verification run")
            elif record.get("acceptance_hash") != acceptance_hash(criteria):
                errors.append("live gate: acceptance changed since live verification")
            elif record.get("config_hash") != config_hash(cfg):
                errors.append("live gate: workflow policy changed since live verification; run it again")
            elif rules_binding_errors(root, cfg, record, "live gate"):
                errors.extend(rules_binding_errors(root, cfg, record, "live gate"))  # #170
            elif approval.get("at") and record.get("at", "") <= approval.get("at", ""):
                errors.append("live gate: live verification must occur after deployment approval")

    design_round = int(status.get("design_round", 0) or 0)
    review_round = int(status.get("review_round", 0) or 0)
    max_design = int(cfg.get("max_design_rounds", 3))
    max_review = effective_review_cap(status, cfg) if "review_attempts" in status else int(cfg.get("max_review_rounds", 3))
    if design_round > max_design:
        errors.append(f"round cap: design_round {design_round} exceeds max_design_rounds {max_design}, escalate to the user")
    if review_round > max_review:
        errors.append(f"round cap: review_round {review_round} exceeds effective max_review_rounds {max_review}, escalate to the user")
    escalation = status.get("escalation")
    if escalation is not None and status.get("status") != "blocked":
        errors.append(
            f"escalation gate: run is escalated ({escalation.get('kind')}); status must stay blocked "
            f"until the escalation is cleared by {escalation.get('required_action')}"
        )
    if current_review_attempt(status) is not None and status.get("status") == "complete":
        errors.append("review attempt gate: an open review attempt exists but status is complete")

    return errors


def configured_regression_commands(cfg: dict) -> set[str]:
    return {command for group in cfg.get("regressions", []) for command in group.get("commands", [])}


def full_design_required(status: dict, acceptance: dict, cfg: dict) -> bool:
    if status.get("lane") == "review":
        return False
    items, _ = effective_work_items(acceptance, cfg)
    for item in items:
        if not item.get("required", True):
            continue
        delivery = work_item_delivery(status, item["id"])
        if delivery.get("lane") != "small-fix" or not delivery.get("confirmed_by"):
            return True
    return False


def derive_work_items(status: dict, acceptance: dict, cfg: dict) -> dict:
    registry, source = effective_work_items(acceptance, cfg)
    criteria = acceptance.get("criteria", [])
    multi = len(registry) > 1
    mapping: dict[str, list[dict]] = {item["id"]: [] for item in registry}
    for criterion in criteria:
        item_id = criterion_work_item_id(criterion)
        if item_id is None and len(registry) == 1:
            item_id = registry[0]["id"]
        if item_id in mapping:
            mapping[item_id].append(criterion)
        elif item_id is not None:
            mapping.setdefault("unattributed", []).append(criterion)
    rows = list(registry)
    if "unattributed" in mapping and not any(item["id"] == "unattributed" for item in rows):
        timestamp = status.get("updated_at") or datetime.now(timezone.utc).isoformat()
        rows.append({"id": "unattributed", "kind": "ask", "number": None,
                     "title": "Unattributed acceptance criteria", "url": "", "required": True,
                     "github_state": None, "github_checked_at": None,
                     "created_at": timestamp, "updated_at": timestamp, "notes": ""})
    regression = active_regression_request(status)
    recovering = any(item.get("state") in {"reserved", "launched"}
                     for item in status.get("recovery_attempts", []))
    reviewing = bool(current_review_attempt(status)) or int(status.get("phase_number", 1) or 1) == 5
    escalation = status.get("escalation") if isinstance(status.get("escalation"), dict) else None
    rendered = []
    for item in rows:
        own = mapping.get(item["id"], [])
        progress_view = item_progress(status, acceptance, cfg, item["id"])
        blocker = ""
        if escalation or status.get("status") == "blocked":
            state = "blocked"
            blocker = (escalation or {}).get("reason") or status.get("next_action", "Run blocked")
        elif regression and regression.get("state") in {"awaiting_approval", "accepted"}:
            state = "awaiting_approval"
            blocker = f"Regression {regression.get('group')} awaits Pilot action"
        elif recovering:
            state = "recovering"
            blocker = "Assigned worker recovery is active"
        elif reviewing:
            state = "in_review"
        elif item.get("required", True) and not own:
            state = "unscoped"
            blocker = "Required work item has no mapped acceptance criteria"
        elif progress_view["percent"] == 100:
            state = "done"
        elif any(c.get("state") == "blocked" for c in own):
            state = "blocked"
            blocker = "A mapped acceptance criterion is blocked"
        elif status.get("active_work_item") == item["id"] or any(c.get("evidence") for c in own):
            state = "in_progress"
        else:
            state = "not_started"
        github_state = item.get("github_state")
        discrepancy = None
        if github_state == "closed" and state != "done":
            discrepancy = "GitHub is closed but Handsoff work is unfinished"
        elif github_state == "open" and state == "done":
            discrepancy = "Handsoff is done but GitHub is still open"
        rendered.append({**item, "status": state, "criteria": [c.get("id") for c in own],
                         "lane": progress_view["lane"], "progress": progress_view["percent"],
                         "lane_confirmed": bool(work_item_delivery(status, item["id"]).get("confirmed_by")),
                         "lane_facts": progress_view["facts"],
                         "lane_escalation": progress_view["escalation_reason"],
                         "phase_or_next": status.get("next_action") or status.get("phase"),
                         "blocker": blocker, "discrepancy": discrepancy})
    counts = {state: sum(item["status"] == state for item in rendered) for state in WORK_ITEM_STATES}
    unattributed_criteria = [c.get("id") for c in mapping.get("unattributed", [])]
    return {"multi": len(rendered) > 1, "registry": source,
            "tickets_config_deprecated": bool(cfg.get("tickets")) and source == "persisted",
            "unattributed_criteria": unattributed_criteria,
            "items": rendered, "aggregate": {"total": len(rendered), "done": counts["done"],
                                               "counts": counts,
                                               "progress": overall_item_progress(status, acceptance, cfg)}}


CRITERION_TYPES = ("primary_fix", "supporting")


CRITERION_SETTABLE_STATES = ("failing", "not_tested", "blocked")


CRITERION_ADD_FIELDS = ("id", "type", "requirement", "verification", "tests")


CRITERION_UPDATE_FIELDS = ("requirement", "verification", "tests", "type", "state",
                           # #165: a criterion may declare that no failing run can exist
                           # for it (a test born with the feature), with the reason audited
                           "baseline", "baseline_reason",
                           # #169: N green runs in a row, with a seed per attempt
                           "repeat", "seed_env")


MAX_REPEAT = 50


MAX_CRITERIA_TRANSACTION_OPERATIONS = 64


class CriteriaTransactionError(HandsoffError):
    """A refused operation. `str()` yields the documented refusal text
    `operation N (<op> <id>): <reason>`, with N counted from 1 in file
    order, so the supervisor's generic SHIP_FEATURE_BLOCKED prefix
    completes the message without a second formatting path."""

    def __init__(self, index: int, op: object, criterion_id: object, reason: str):
        self.index = index
        self.op = op if isinstance(op, str) and op else "?"
        self.criterion_id = criterion_id if isinstance(criterion_id, str) and criterion_id else "?"
        self.reason = reason
        super().__init__(f"operation {index} ({self.op} {self.criterion_id}): {reason}")


def validate_criterion_fields(fields: dict, *, require_all: bool = False) -> list[str]:
    """The one field validator behind criterion-add, criterion-update, and
    criteria-apply. `require_all` is the add form: id, type, requirement,
    verification, and a non-empty tests list must all be present. Without
    it (the update and remove forms) any subset of the fields is checked,
    `id` included; whether a subset may be empty is the caller's rule.
    Returns a list of problems."""
    if not isinstance(fields, dict):
        return ["criterion fields must be an object"]
    allowed = set(CRITERION_ADD_FIELDS) | set(CRITERION_UPDATE_FIELDS)
    unknown = sorted(set(fields) - allowed)
    if unknown:
        return [f"unknown criterion field(s): {', '.join(unknown)}"]
    errors: list[str] = []
    if require_all:
        missing = [field for field in CRITERION_ADD_FIELDS if field not in fields]
        if missing:
            errors.append(f"missing criterion field(s): {', '.join(missing)}")
        if "state" in fields:
            errors.append("a new criterion may not set 'state'; it starts not_tested")
    if "id" in fields and (not isinstance(fields["id"], str) or not fields["id"].strip()):
        errors.append("'id' must be a non-empty string")
    if "type" in fields and fields["type"] not in CRITERION_TYPES:
        errors.append(f"'type' must be one of {', '.join(CRITERION_TYPES)}")
    if "requirement" in fields and (not isinstance(fields["requirement"], str)
                                    or not fields["requirement"].strip()):
        errors.append("'requirement' must be a non-empty string")
    if "verification" in fields and fields["verification"] not in VERIFICATION_REQUIREMENTS:
        errors.append(f"'verification' must be one of {', '.join(VERIFICATION_REQUIREMENTS)}")
    if "tests" in fields:
        tests = fields["tests"]
        if not isinstance(tests, list) or not tests \
                or not all(isinstance(test, str) and test.strip() for test in tests):
            errors.append("'tests' must be a non-empty list of non-empty strings")
    if "state" in fields and fields["state"] not in CRITERION_SETTABLE_STATES:
        errors.append(f"'state' must be one of {', '.join(CRITERION_SETTABLE_STATES)}")
    if "baseline" in fields and fields["baseline"] not in (BASELINE_NOT_APPLICABLE, None):
        errors.append(f"'baseline' may only be {BASELINE_NOT_APPLICABLE} (or absent)")
    if fields.get("baseline") == BASELINE_NOT_APPLICABLE and not str(fields.get("baseline_reason") or "").strip():
        errors.append("'baseline_reason' is required with baseline not_applicable")
    if "baseline_reason" in fields and fields.get("baseline") != BASELINE_NOT_APPLICABLE:
        errors.append("'baseline_reason' needs baseline not_applicable")
    if "repeat" in fields and fields["repeat"] is not None and (
            not isinstance(fields["repeat"], int) or isinstance(fields["repeat"], bool)
            or not 1 <= fields["repeat"] <= MAX_REPEAT):
        errors.append(f"'repeat' must be an integer from 1 to {MAX_REPEAT}")
    if "seed_env" in fields and fields["seed_env"] is not None and (
            not isinstance(fields["seed_env"], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", fields["seed_env"])):
        errors.append("'seed_env' must be an environment variable name (A-Z, 0-9, _)")
    if fields.get("seed_env") and not fields.get("repeat"):
        errors.append("'seed_env' needs repeat")
    return errors


def sync_work_item_registry(acceptance: dict, cfg: dict) -> bool:
    """Append newly declared criterion identities to a persisted registry
    without deleting stable promises. A legacy registry (no persisted
    `work_items`) is left alone: its items stay derived on read."""
    existing = acceptance.get("work_items")
    if not isinstance(existing, list):
        return False
    known = {item.get("id") for item in existing}
    criterion_ids = {criterion_work_item_id(criterion)
                     for criterion in acceptance.get("criteria", [])}
    criterion_ids.discard(None)
    has_persisted_issue = any(item.get("kind") == "issue" for item in existing)
    changed = False
    for item in derive_work_item_registry(acceptance, cfg):
        if item["id"] not in known:
            if has_persisted_issue and item.get("kind") == "ask" and item["id"] not in criterion_ids:
                continue
            existing.append(item)
            known.add(item["id"])
            changed = True
    existing.sort(key=lambda item: (item["kind"] != "issue", item.get("number") or 0, item["id"]))
    return changed


def _transaction_test_gate(index: int, op: str, criterion: dict, cfg: dict, root: Path) -> None:
    """For a criterion `verify` will run (its policy requires `checks`):
    every tests entry must be one of [checks].commands, the rule `verify`
    applies at run time, and must pass the #28 footprint gate exactly as
    `run_checks` applies it (no shell control operators, never a command
    that captures a gated regression group), so a transaction can never
    register a test that verification would later refuse. Manual and
    browser criteria carry attestation descriptions, never commands, and
    are not gated here, matching the single commands."""
    if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
        return
    regression_commands = configured_regression_commands(cfg)
    for test in criterion.get("tests", []):
        if test not in cfg.get("check_commands", []):
            raise CriteriaTransactionError(
                index, op, criterion.get("id"),
                f"automated test {test!r} is not one of [checks].commands",
            )
        try:
            footprint = normalized_test_footprint(test, root)
        except HandsoffError as exc:
            raise CriteriaTransactionError(index, op, criterion.get("id"), f"test {test!r}: {exc}") from exc
        for gated in regression_commands:
            gated_footprint = normalized_test_footprint(gated, root)
            if "*" in footprint or ("*" not in gated_footprint and gated_footprint <= set(footprint)):
                raise CriteriaTransactionError(
                    index, op, criterion.get("id"),
                    f"test {test!r} captures gated regression group commands; "
                    "full regressions go through regression-request",
                )


def plan_criteria_transaction(acceptance: dict, cfg: dict, operations: list[dict], *,
                              root: Path | None = None) -> dict:
    """Apply `operations` in order to a deep copy of `acceptance` and return
    the plan, or raise CriteriaTransactionError (HandsoffError) naming the
    first refused operation. Nothing passed in is mutated.

    The plan carries `operations` ([{op, id, previous_hash, resulting_hash}]),
    `operation_count`, `registry_hash_before`/`registry_hash_after`
    (`acceptance_hash`), `design_hash_before`/`design_hash_after`,
    `work_items_after` (effective work item ids), `work_item_scope_changed`,
    `resets_original_symptom` (a primary_fix spec was added or changed),
    and the full `criteria_after` and `work_items_registry_after` that
    `apply_criteria_plan` writes. `root` is where test footprints resolve
    their globs; it defaults to the current directory."""
    root = root if root is not None else Path.cwd()
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_CRITERIA_TRANSACTION_OPERATIONS:
        raise HandsoffError(
            f"transaction: 'operations' must contain 1 to {MAX_CRITERIA_TRANSACTION_OPERATIONS} entries"
        )
    before_criteria = acceptance.get("criteria")
    if not isinstance(before_criteria, list):
        raise HandsoffError("transaction: acceptance registry has no criteria list")
    planned = deepcopy(acceptance)
    criteria: list[dict] = planned["criteria"]
    before_items, _ = effective_work_items(acceptance, cfg)
    before_scope = work_item_scope_hash(before_items, before_criteria)
    seen_ids: set[str] = set()
    records: list[dict] = []
    resets_symptom = False
    last_primary_touch: int | None = None

    def lookup(criterion_id: str) -> dict | None:
        return next((c for c in criteria if c.get("id") == criterion_id), None)

    for position, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise CriteriaTransactionError(position, None, None, "operation must be an object")
        op = operation.get("op")
        if op not in CRITERIA_TRANSACTION_OPS:
            raise CriteriaTransactionError(position, op, operation.get("id"),
                                           f"'op' must be one of {', '.join(CRITERIA_TRANSACTION_OPS)}")
        if op == "add":
            if set(operation) != {"op", "criterion"}:
                raise CriteriaTransactionError(position, op, None,
                                               "an add operation has exactly 'op' and 'criterion'")
            spec = operation["criterion"]
            if not isinstance(spec, dict) or set(spec) != set(CRITERION_ADD_FIELDS):
                raise CriteriaTransactionError(
                    position, op, spec.get("id") if isinstance(spec, dict) else None,
                    "an add criterion has exactly id, type, requirement, verification, tests",
                )
            criterion_id = spec.get("id")
            problems = validate_criterion_fields(spec, require_all=True)
            if problems:
                raise CriteriaTransactionError(position, op, criterion_id, "; ".join(problems))
            if criterion_id in seen_ids:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "criterion id appears in an earlier operation")
            if lookup(criterion_id) is not None:
                raise CriteriaTransactionError(position, op, criterion_id, "criterion already exists")
            seen_ids.add(criterion_id)
            criterion = {
                "id": criterion_id, "type": spec["type"], "requirement": spec["requirement"],
                "verification": spec["verification"], "tests": list(spec["tests"]),
                "evidence": [], "state": "not_tested",
            }
            _transaction_test_gate(position, op, criterion, cfg, root)
            criteria.append(criterion)
            if criterion["type"] == "primary_fix":
                resets_symptom = True
                last_primary_touch = position
            records.append({"op": op, "id": criterion_id, "previous_hash": None,
                            "resulting_hash": criterion_spec_hash(criterion)})
            continue
        criterion_id = operation.get("id")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise CriteriaTransactionError(position, op, criterion_id, "'id' must be a non-empty string")
        if op == "update":
            if set(operation) != {"op", "id", "fields"}:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "an update operation has exactly 'op', 'id' and 'fields'")
        elif set(operation) != {"op", "id"}:
            raise CriteriaTransactionError(position, op, criterion_id,
                                           "a remove operation has exactly 'op' and 'id'")
        if criterion_id in seen_ids:
            raise CriteriaTransactionError(position, op, criterion_id,
                                           "criterion id appears in an earlier operation")
        seen_ids.add(criterion_id)
        criterion = lookup(criterion_id)
        if criterion is None:
            raise CriteriaTransactionError(position, op, criterion_id, "unknown criterion")
        previous_hash = criterion_spec_hash(criterion)
        if op == "remove":
            if len(criteria) == 1:
                raise CriteriaTransactionError(position, op, criterion_id,
                                               "acceptance registry must retain at least one criterion")
            if criterion.get("type") == "primary_fix":
                last_primary_touch = position
            criteria[:] = [c for c in criteria if c is not criterion]
            records.append({"op": op, "id": criterion_id, "previous_hash": previous_hash,
                            "resulting_hash": None})
            continue
        fields = operation["fields"]
        if not isinstance(fields, dict) or not fields or set(fields) - set(CRITERION_UPDATE_FIELDS):
            raise CriteriaTransactionError(
                position, op, criterion_id,
                "'fields' must be a non-empty object with keys among requirement, verification, tests, type, state",
            )
        problems = validate_criterion_fields(fields)
        if problems:
            raise CriteriaTransactionError(position, op, criterion_id, "; ".join(problems))
        was_primary = criterion.get("type") == "primary_fix"
        spec_changed = False
        for field in ("requirement", "verification", "type"):
            if field in fields and criterion.get(field) != fields[field]:
                criterion[field] = fields[field]
                spec_changed = True
        if "tests" in fields:
            criterion["tests"] = list(fields["tests"])
            spec_changed = True
        if "state" in fields:
            criterion["state"] = fields["state"]
        if spec_changed or "state" in fields:
            criterion["evidence"] = []
            if "state" not in fields:
                criterion["state"] = "not_tested"
        if "tests" in fields or "verification" in fields:
            _transaction_test_gate(position, op, criterion, cfg, root)
        if spec_changed and (was_primary or criterion.get("type") == "primary_fix"):
            resets_symptom = True
        if was_primary or criterion.get("type") == "primary_fix":
            last_primary_touch = position
        records.append({"op": op, "id": criterion_id, "previous_hash": previous_hash,
                        "resulting_hash": criterion_spec_hash(criterion)})

    primary_count = sum(1 for c in criteria if c.get("type") == "primary_fix")
    if primary_count != 1:
        culprit = last_primary_touch or len(operations)
        culprit_record = records[culprit - 1]
        raise CriteriaTransactionError(
            culprit, culprit_record["op"], culprit_record["id"],
            f"the resulting registry would have {primary_count} primary_fix criteria; exactly one is required",
        )
    errors = validate_acceptance_schema(planned)
    if errors:
        culprit = len(operations)
        for record in reversed(records):
            if any(f"criterion {record['id']} " in error for error in errors):
                culprit = records.index(record) + 1
                break
        culprit_record = records[culprit - 1]
        raise CriteriaTransactionError(culprit, culprit_record["op"], culprit_record["id"],
                                       "resulting registry fails validation: " + "; ".join(errors))
    registry_changed = sync_work_item_registry(planned, cfg)
    after_items, _ = effective_work_items(planned, cfg)
    return {
        "operations": records,
        "operation_count": len(records),
        "registry_hash_before": acceptance_hash(before_criteria),
        "registry_hash_after": acceptance_hash(criteria),
        "design_hash_before": design_hash(before_criteria),
        "design_hash_after": design_hash(criteria),
        "work_items_after": [item["id"] for item in after_items],
        "work_item_scope_changed": before_scope != work_item_scope_hash(after_items, criteria),
        "resets_original_symptom": resets_symptom,
        "criteria_after": criteria,
        "work_items_registry_after": planned.get("work_items") if isinstance(planned.get("work_items"), list) else None,
    }


def amendment_freeze_errors(status: dict) -> list[str]:
    """The compute_errors rule: while an amendment is open the run is
    pinned to the phase and progress it was opened at (after the review,
    deployment, and live decisions were invalidated). Any other proposed
    phase or progress is refused, forward or back."""
    amendment = open_amendment(status)
    if amendment is None:
        return []
    phase = int(status.get("phase_number", 0) or 0)
    progress = float(status.get("progress", 0) or 0)
    if phase != amendment.get("frozen_phase") or progress != float(amendment.get("frozen_progress", 0) or 0):
        return [f"amendment gate: amendment {amendment.get('amendment_id')} is open; "
                "record its review and Pilot approval first"]
    return []


def feature_hash(status: dict, events: list[dict]) -> str:
    """Identity of the current run: sha256 of the feature text plus the
    timestamp of the run's first event (the `initialized` record), the same
    anchor the regression gate uses for its run id. A record from an
    earlier run of the same feature therefore never satisfies this one."""
    initialized_at = str(events[0].get("at") or "") if events else ""
    return hashlib.sha256((str(status.get("feature") or "") + initialized_at).encode("utf-8")).hexdigest()


def ci_gate_errors(status: dict) -> list[str]:
    """One line while the watched head has a failed check. Evaluated on the
    proposed status like every gate, so it refuses the 6 to 7 transition."""
    watch = status.get("ci") if isinstance(status, dict) else None
    if not isinstance(watch, dict) or watch.get("state") != "failed":
        return []
    check = watch.get("failed_check") or "a check"
    link = f" ({watch['url']})" if isinstance(watch.get("url"), str) else ""
    return [f"CI: {check} failed on PR #{watch.get('pr')}{link}; rerun or push, then ci-watch --pr {watch.get('pr')} again"]


def pending_design_decline(status: dict) -> dict | None:
    declined = status.get("design_declined") if isinstance(status, dict) else None
    return declined if isinstance(declined, dict) and declined.get("decision") == "pending" else None
