#!/usr/bin/env python3
"""Handsoff evidence ledger: events, verifications and the digests that bind them.

#284 stage 3. Fifty-four symbols covering the append-only event log, the
verification log, the repository digest and the evidence-drift check that
decides whether recorded evidence still describes the tree it was taken on.

This is the first extraction with real inbound weight: 76 symbols in the
monolith call into it. They keep working because `handsoff_lib` re-exports
the whole set, so this lane changes no call site, but it is the point at
which re-export stops being cosmetic and starts carrying the migration.

Layer: `handsoff_core` -> `handsoff_routing` -> `handsoff_config` -> here.
Every import below is module level; nothing is deferred.

Nothing about the persisted shape changes. The event log, the verification
log and the digest are byte-identical before and after, which is what
criterion 4 of #284 requires and what `tests/test_ledger_layer.py` pins
against records captured from the pre-extraction module.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import (
    HandsoffError,
    _canonical,
    acceptance_hash,
    acceptance_path,
    criterion_spec_hash,
    design_hash,
    durable_replace,
    load_unique_json,
    status_path,
)
from handsoff_routing import adaptive_deployment_approval_required
from handsoff_config import (
    DEFAULT_CONFIG,
    DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    DEFAULT_SMALL_FIX_MAX_CRITERIA,
    DEFAULT_SMALL_FIX_MAX_FILES,
    FEATURES,
    load_config,
)



PREFLIGHT_FILE = ".handsoff-preflight.json"


#: #38: cached, hash-bound design evidence. The side file is generated
#: state (gitignored), never a ledger: it holds the bounded output of
#: trusted configured commands, and the event log only ever carries hashes.
DESIGN_EVIDENCE_FILE = "handsoff-design-evidence.json"


#: #33: liveness beacon written by `handsoff_agent.execute_launch` while a
#: managed child runs. Generated state (gitignored), never hashed, never
#: read by any gate: identifiers, integers, and timestamps only. The
#: ledger-bound session record stays the authority on lifecycle; the beacon
#: only says whether the process that owns that session is still signalling.
LIVE_BEACON_FILE = ".handsoff-live.json"


LIVE_INFLIGHT_FILE = ".handsoff-live-inflight.json"


#: #41: output liveness. Every stdout/stderr chunk a managed child writes
#: bumps this file (gitignored, never hashed, never logged, never read by
#: a gate): identifiers, one timestamp, and two counters, never content.
#: It is what lets `stall_warning` see a child that is streaming output
#: while the workflow and heartbeat timestamps sit idle. Writes are rate
#: limited to one per second per session; chunks in between only bump the
#: counters. The file only counts while bound to the current session for
#: its role in a live state, so a process exit expires the signal at once.
OUTPUT_LIVENESS_FILE = ".handsoff-output-liveness.json"


WORK_ITEM_TAG_PATTERN = re.compile(r"^\[(#\d{1,9}|[a-z0-9][a-z0-9-]{0,39})\]\s")


MAX_WORK_ITEMS = 64


ANALYSIS_DIR = ".handsoff-analysis"


VERIFICATION_REQUIREMENTS = {
    "automated": {"checks"},
    "manual": {"manual"},
    "browser": {"browser"},
    "automated_and_browser": {"checks", "browser"},
}


def event_log_path(root: Path, cfg: dict) -> Path:
    return root / cfg["event_log"]


def verification_log_path(root: Path, cfg: dict) -> Path:
    return root / cfg["verification_log"]


def event_head_path(root: Path) -> Path:
    return root / ".handsoff-event-head.json"


def write_ahead_path(root: Path) -> Path:
    return root / ".handsoff-writeahead.json"


def atomic_write_json(path: Path, data: object) -> None:
    """Durably replace a JSON record and retain one last-known-good copy."""
    durable_replace(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _serialized_digest(data: dict) -> str:
    """The sha256 of the exact bytes atomic_write_json would produce for
    this value, in the same format, so a later comparison against the
    real file on disk (hashed the same way _file_sha256 does) can never
    mismatch on serialization alone."""
    return hashlib.sha256((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")).hexdigest()


def write_ahead(root: Path, *, status: dict | None = None, acceptance: dict | None = None) -> None:
    """Record, BEFORE any target file is touched, exactly which file(s)
    this in-flight write intends to produce and their exact resulting
    content. This is what lets `doctor` tell a genuinely interrupted
    write apart from an unrelated hand edit that merely happens to still
    validate: doctor will only re-anchor the event log to a state that
    exactly matches a journal entry naming it as the intended result of
    a real command, never to any state that simply passes the gates.
    Caller must hold project_lock and call clear_write_ahead once the
    matching commit (through its append_event) has completed; see
    commit()."""
    entry: dict = {"at": datetime.now(timezone.utc).isoformat()}
    if status is not None:
        entry["status_sha256"] = _serialized_digest(status)
    if acceptance is not None:
        entry["acceptance_sha256"] = _serialized_digest(acceptance)
    atomic_write_json(write_ahead_path(root), entry)


def clear_write_ahead(root: Path) -> None:
    try:
        write_ahead_path(root).unlink()
    except Exception:
        pass


def commit(root: Path, cfg: dict, *, status: dict | None = None, acceptance: dict | None = None,
          event_kind: str, event_message: str, extra_events: list[dict] | None = None,
          **event_extra) -> str:
    """Write status and/or acceptance, and append the event(s) describing
    them, as one write-ahead-journaled unit. Every mutating command uses
    this instead of calling atomic_write_json/append_event directly, so
    every real write leaves the journal `doctor` needs to recover it
    safely. Caller must hold project_lock for the entire surrounding
    read-validate-write, not just this call.

    `extra_events`, if given, are appended (in list order) BEFORE the
    primary event_kind/event_message, so a single state transition that is
    really two consecutive facts (a design round ending because the next
    one just started, say) can log both without a second write-ahead cycle
    or a second caller of commit(). Each entry is {"kind": ..., "message":
    ..., **extra}. Returns the hash of the PRIMARY event only; callers that
    need an extra event's own hash should read it back from the log."""
    # A scoped amendment deliberately freezes both phase and progress.
    # Recomputing item progress while its changed criteria are temporarily
    # reset would make the just-written amendment contradict its own frozen
    # snapshot and render an otherwise valid run invalid.
    preserve_progress = bool(status is not None and status.pop("_preserve_progress", False))
    if status is not None and "work_item_delivery" in status and open_amendment(status) is None and not preserve_progress:
        progress_acceptance = acceptance
        if progress_acceptance is None and acceptance_path(root, cfg).is_file():
            progress_acceptance = load_unique_json(acceptance_path(root, cfg))
        if isinstance(progress_acceptance, dict):
            # The derived value only ever raises progress here: an operator
            # value written by advance (#100) is never fought by bookkeeping,
            # and rollbacks set their own lower value explicitly.
            derived = overall_item_progress(status, progress_acceptance, cfg)
            status["progress"] = max(int(status.get("progress", 0) or 0), derived)
    if status is not None and preserve_progress and "progress" in event_extra:
        status["progress"] = event_extra["progress"]
    write_ahead(root, status=status, acceptance=acceptance)
    if acceptance is not None:
        atomic_write_json(acceptance_path(root, cfg), acceptance)
    if status is not None:
        atomic_write_json(status_path(root, cfg), status)
    for extra in extra_events or ():
        rest = {k: v for k, v in extra.items() if k not in ("kind", "message")}
        append_event(root, cfg, extra["kind"], extra["message"], **rest)
    event_hash = append_event(root, cfg, event_kind, event_message, **event_extra)
    clear_write_ahead(root)
    return event_hash


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return "GENESIS"
    last = ""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return "GENESIS"
    return json.loads(last)["hash"]


def append_event(root: Path, cfg: dict, kind: str, message: str, **extra) -> str:
    path = event_log_path(root, cfg)
    prev_hash = _last_hash(path)
    body = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "message": message,
            "prev_hash": prev_hash,
            "status_sha256": _file_sha256(status_path(root, cfg)),
            "acceptance_sha256": _file_sha256(acceptance_path(root, cfg)), **extra}
    body["hash"] = hashlib.sha256((_canonical(body) + prev_hash).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(body) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    atomic_write_json(event_head_path(root), {"hash": body["hash"]})
    return body["hash"]


def evidence_drift(root: Path, cfg: dict, acceptance: dict,
                   verifications: list[dict]) -> dict:
    """Classify newest valid automated evidence against one cached digest.

    Legacy records remain unknown rather than stale, so adding this integrity
    check cannot unexpectedly invalidate an existing run.
    """
    current_digest = repository_digest(root, cfg)
    current_config = verification_config_hash(cfg)
    result = {"current_digest": current_digest, "current": [], "stale": [],
              "unknown": [], "refresh_commands": [], "changed_paths": [],
              "changed_paths_truncated": False, "changed_paths_note": None}
    current_entries = repository_digest_entries(root, cfg)
    for criterion in acceptance.get("criteria", []):
        cid = criterion.get("id")
        if "checks" not in VERIFICATION_REQUIREMENTS.get(criterion.get("verification"), set()):
            continue
        record = next((candidate for candidate in reversed(verifications)
                       if candidate.get("kind") == "checks"
                       and candidate.get("ok") is True
                       and cid in candidate.get("criteria", [])
                       and candidate.get("criterion_hashes", {}).get(cid) == criterion_spec_hash(criterion)), None)
        if record is None:
            continue
        digest = record.get("repository_digest")
        recorded_config = record.get("config_hash")
        if digest is None:
            result["unknown"].append(cid)
            result["changed_paths"] = None
            result["changed_paths_note"] = "snapshot not recorded"
        elif digest == current_digest and (recorded_config is None or recorded_config == current_config):
            result["current"].append(cid)
        else:
            result["stale"].append(cid)
            snapshot_path = root / ".handsoff-digests" / f"{digest}.json"
            if snapshot_path.is_file():
                try:
                    old_entries = load_unique_json(snapshot_path).get("entries", {})
                    changed = sorted({*old_entries, *current_entries} - {
                        path for path in set(old_entries) & set(current_entries)
                        if old_entries[path] == current_entries[path]
                    })
                    if len(changed) > 32:
                        result["changed_paths_truncated"] = True
                    result["changed_paths"] = changed[:32]
                except (OSError, HandsoffError, ValueError):
                    result["changed_paths_note"] = "snapshot not recorded"
            else:
                result["changed_paths_note"] = "snapshot not recorded"
            result["refresh_commands"].append(
                f"handsoff_supervisor.py verify --criterion {cid} --by ACTOR")
    return result


def feature_enabled(cfg: dict, name: str) -> bool:
    if name not in FEATURES:
        raise HandsoffError(f"unknown workflow feature: {name}")
    return bool((cfg or {}).get("features", {}).get(name, FEATURES[name][0]))


GOVERNANCE_CONFIG_KEYS = (
    "deployment_requires_explicit_approval", "require_live_verification",
    "max_design_rounds", "max_review_rounds", "stall_minutes",
    "max_autonomous_design_reviews", "small_fix_max_criteria",
    "small_fix_max_changed_lines", "small_fix_max_files",
    "require_design_approval",
)


# Governance keys added after runs were already in flight. A key in this
# set is hashed only while it holds a non-default value: an absent (or
# explicitly default) key must reproduce the pre-existing hash byte for
# byte, or upgrading bin/ would invalidate every design review, design
# approval, and deployment approval already recorded on every project
# running Handsoff (this repo's own run included). Changing the key to
# anything else still invalidates the decisions bound to the old value,
# which is the whole point of the chain of trust.
_LEGACY_OPTIONAL_GOVERNANCE_KEYS = {
    "max_autonomous_design_reviews": DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    "small_fix_max_criteria": DEFAULT_SMALL_FIX_MAX_CRITERIA,
    "small_fix_max_changed_lines": DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    "small_fix_max_files": DEFAULT_SMALL_FIX_MAX_FILES,
    "require_design_approval": True,  # #159
}


def config_hash(cfg: dict) -> str:
    """Binds a review, a deployment approval, or a live verification to the
    governance policy in force when it was recorded. Without this, someone
    could flip deployment_requires_explicit_approval or
    require_live_verification off AFTER a review, silently downgrading what
    the workflow requires without invalidating anything already granted."""
    bound = {}
    for key in GOVERNANCE_CONFIG_KEYS:
        if key in _LEGACY_OPTIONAL_GOVERNANCE_KEYS:
            default = _LEGACY_OPTIONAL_GOVERNANCE_KEYS[key]
            if cfg.get(key, default) == default:
                continue
        bound[key] = cfg.get(key)
    # The execution profile was introduced after runs already existed. Bind
    # every non-default profile so changing posture invalidates subsequent
    # decisions. The sole migration exception is an existing dogfood run
    # with active waivers: those exact waiver booleans are already present in
    # ``bound``, so adding the profile as a second representation would only
    # invalidate an in-flight approval without strengthening the decision.
    # Once the waivers are removed, dogfood itself is non-default and binds.
    profile = cfg.get("execution_profile", "safe")
    waivers_active = (not cfg.get("deployment_requires_explicit_approval", True)
                      or not cfg.get("require_design_approval", True))
    if profile != "safe" and not (profile == "dogfood" and waivers_active):
        bound["execution_profile"] = profile
    # [features] switches bind the same way: a switch at its default keeps
    # every recorded hash byte for byte; a flipped one revokes what was
    # granted under the other setting.
    for name, (default, _text) in FEATURES.items():
        if feature_enabled(cfg, name) != default:
            bound["features." + name] = feature_enabled(cfg, name)
    return hashlib.sha256(_canonical(bound).encode("utf-8")).hexdigest()


def _design_hash_current(recorded: object, status: dict, acceptance: dict) -> bool:
    """A design decision is current when its hash is the registry's design
    hash, or (#42) while a scoped amendment is open: the decision still
    carries the amendment's base hash and the registry is exactly the
    amended one. Approving the amendment rewrites the decision to the
    resulting hash; escalating it clears the decision."""
    current = design_hash(acceptance.get("criteria", []))
    if recorded == current:
        return True
    amendment = open_amendment(status)
    return bool(amendment) and amendment.get("base_design_hash") == recorded \
        and amendment.get("resulting_design_hash") == current


def _design_errors(status: dict, acceptance: dict, cfg: dict, root: Path | None = None) -> list[str]:
    """The Architect gate: Phase 3+ requires a recorded human design
    approval, for any run `requires_design_approval` (a run a NEW init
    created). Absent for any status.json that predates this field --
    those runs are simply never subject to this check, so an
    already-in-progress run elsewhere is unaffected by upgrading bin/."""
    if not status.get("requires_design_approval"):
        return []
    errors: list[str] = []
    approval = status.get("design_approved")
    if not isinstance(approval, dict):
        return ["design gate: Phase 3+ requires a recorded human design approval"]
    if not _design_hash_current(approval.get("design_hash"), status, acceptance):
        errors.append("design gate: criteria were added, removed, or respecified since design approval; record a new approval")
    if approval.get("config_hash") != config_hash(cfg):
        errors.append("design gate: workflow policy changed since design approval; record a new approval")
    # #170: the design approval RECORDS the rules set it was given under;
    # the comparison belongs to the review, deployment and live gates, so
    # a hook edit at Phase 6 asks for a fresh review, not a fresh design.
    if "work_items" in acceptance and not scope_hash_matches(approval.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
        errors.append("design gate: work-item scope changed since design approval; record a new approval")
    proposal = status.get("design_proposal")
    if isinstance(proposal, dict) and approval.get("proposal_hash") != proposal.get("proposal_hash"):
        errors.append(f"stale proposal: approval binds {approval.get('proposal_hash')}, current is {proposal.get('proposal_hash')}")
    approver = approval.get("by")
    architect = approval.get("architect")
    if not approver:
        errors.append("design gate: design approval must identify the human approver")
    if not architect:
        errors.append("design gate: design approval must identify the architect")
    if approver and architect and approver.strip().casefold() == architect.strip().casefold():
        errors.append("design gate: approver must differ from the architect, no self-approval")
    return errors


def _design_review_errors(status: dict, acceptance: dict, cfg: dict) -> list[str]:
    """AR7 gate: new runs cannot leave Phase 2 until an independent
    reviewer approved the exact current design. The opt-in status flag is
    absent from pre-AR7 runs, preserving their in-flight behavior."""
    if not status.get("requires_design_review"):
        return []
    review = status.get("design_review")
    if not isinstance(review, dict):
        return ["design review gate: Phase 3+ requires an approved independent design review"]
    errors: list[str] = []
    if review.get("decision") != "approved":
        errors.append("design review gate: the current design review requested changes")
    if not _design_hash_current(review.get("design_hash"), status, acceptance):
        errors.append("design review gate: criteria changed since design review; record a new design review")
    if review.get("config_hash") != config_hash(cfg):
        errors.append("design review gate: workflow policy changed since design review; record a new design review")
    if "work_items" in acceptance and not scope_hash_matches(review.get("scope_hash"), acceptance["work_items"], acceptance.get("criteria", [])):
        errors.append("design review gate: work-item scope changed since design review; record a new design review")
    proposal = status.get("design_proposal")
    if isinstance(proposal, dict) and review.get("proposal_hash") != proposal.get("proposal_hash"):
        errors.append(f"stale proposal: approval binds {review.get('proposal_hash')}, current is {proposal.get('proposal_hash')}")
    reviewer = review.get("by")
    architect = review.get("architect")
    if not reviewer:
        errors.append("design review gate: design review must identify its reviewer")
    if not architect:
        errors.append("design review gate: design review must identify the architect")
    if reviewer and architect and reviewer.strip().casefold() == architect.strip().casefold():
        errors.append("design review gate: reviewer must differ from the architect, no self-review")
    approval = status.get("design_approved")
    approved_architect = approval.get("architect") if isinstance(approval, dict) else None
    if approved_architect and architect \
            and approved_architect.strip().casefold() != architect.strip().casefold():
        errors.append("design review gate: reviewed architect differs from the architect named in human approval")
    return errors


def _work_item_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:40].rstrip("-")
    return slug or "item"


def criterion_work_item_id(criterion: dict) -> str | None:
    requirement = criterion.get("requirement") if isinstance(criterion, dict) else None
    match = WORK_ITEM_TAG_PATTERN.match(requirement or "")
    if not match:
        return None
    tag = match.group(1)
    return f"issue-{tag[1:]}" if tag.startswith("#") else f"ask-{tag}"


def removed_work_item_ids(acceptance: dict) -> set[str]:
    """Ids the Pilot removed with work-item-remove (#141 tombstones)."""
    out = set()
    for record in acceptance.get("removed_work_items") or []:
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            out.add(record["id"])
    return out


def derive_work_item_registry(acceptance: dict, cfg: dict, *, now: str | None = None,
                              explicit_items: list[str] | None = None) -> list[dict]:
    """Derive stable scope from criterion tags, feature issue refs and legacy display metadata."""
    now = now or datetime.now(timezone.utc).isoformat()
    tickets = {int(item["number"]): item for item in cfg.get("tickets", [])}
    identities: dict[str, tuple[str, int | None, str]] = {}
    feature = str(acceptance.get("feature") or "")

    def add_text(text: str) -> None:
        issue = re.fullmatch(r"\s*#([1-9][0-9]{0,8})(?:\s+(.+?))?\s*", text)
        if issue:
            number = int(issue.group(1))
            supplied = (issue.group(2) or "").strip()
            title = supplied or tickets.get(number, {}).get("title") or f"Issue #{number}"
            identities[f"issue-{number}"] = ("issue", number, title)
            return
        slug = _work_item_slug(text)
        identities.setdefault(f"ask-{slug}", ("ask", None, text.strip()[:200]))

    if explicit_items:
        for item in explicit_items[:MAX_WORK_ITEMS]:
            add_text(item)
    else:
        # Explicit separators are promises. A segment containing issue refs
        # contributes those issues; a segment without one remains a plain ask.
        parts = [part.strip(" -\t") for part in
                 re.split(r"[;\n]+|(?:^|\s)\d+[.)]\s+", feature)
                 if part.strip(" -\t")]
        for part in parts[:MAX_WORK_ITEMS]:
            numbers = re.findall(r"(?<!\w)#([1-9][0-9]{0,8})\b", part)
            if numbers:
                for number_text in numbers:
                    number = int(number_text)
                    identities[f"issue-{number}"] = (
                        "issue", number,
                        tickets.get(number, {}).get("title") or f"Issue #{number}",
                    )
            else:
                add_text(part)
    tagged: set[str] = set()
    for criterion in acceptance.get("criteria", []):
        item_id = criterion_work_item_id(criterion)
        if not item_id:
            continue
        tagged.add(item_id)
        if item_id.startswith("issue-"):
            number = int(item_id[6:])
            title = tickets.get(number, {}).get("title") or f"Issue #{number}"
            identities[item_id] = ("issue", number, title)
        else:
            title = item_id[4:].replace("-", " ").title()
            identities[item_id] = ("ask", None, title)
    # #141: an item the Pilot removed stays removed. The feature title still
    # names it, so title derivation would quietly bring it back on the next
    # transaction; the tombstone says the removal was a decision. A tagged
    # criterion or an explicit --item is the deliberate way back, and the
    # caller clears the tombstone in the same commit (clear_work_item_tombstones).
    explicit_ids = set()
    for item in explicit_items or []:
        issue = re.fullmatch(r"\s*#([1-9][0-9]{0,8})(?:\s+(.+?))?\s*", item)
        explicit_ids.add(f"issue-{int(issue.group(1))}" if issue else f"ask-{_work_item_slug(item)}")
    for item_id in removed_work_item_ids(acceptance):
        if item_id not in tagged and item_id not in explicit_ids:
            identities.pop(item_id, None)
    items = []
    for item_id, (kind, number, title) in identities.items():
        ticket = tickets.get(number, {}) if number is not None else {}
        items.append({
            "id": item_id, "kind": kind, "number": number, "title": title[:200],
            "url": str(ticket.get("url") or ""), "required": True,
            "github_state": None, "github_checked_at": None,
            "created_at": now, "updated_at": now, "notes": "",
        })
    return sorted(items, key=lambda item: (item["kind"] != "issue", item["number"] or 0, item["id"]))[:MAX_WORK_ITEMS]


def effective_work_items(acceptance: dict, cfg: dict) -> tuple[list[dict], str]:
    persisted = acceptance.get("work_items")
    if isinstance(persisted, list):
        return persisted, "persisted"
    return derive_work_item_registry(acceptance, cfg), "derived"


def scoped_work_items(items: list[dict], criteria: list[dict] | None) -> list[dict]:
    """The items that actually carry acceptance criteria. Scope is what the
    criteria promise: an item nobody has tagged a criterion to (a spurious
    title-derived ask, an issue registered ahead of its criteria) is not
    part of what a reviewer or the Pilot judged, so adding or removing it
    must not invalidate their decisions (#82). With `criteria` None the
    whole registry counts, which is what pure-registry callers expect.
    Mirrors derive_work_items: untagged criteria attach to a single-item
    registry's only item."""
    if criteria is None:
        return list(items)
    ids = {item.get("id") for item in items}
    mapped: set[str] = set()
    for criterion in criteria:
        item_id = criterion_work_item_id(criterion)
        if item_id is None and len(items) == 1:
            item_id = items[0].get("id")
        if item_id in ids:
            mapped.add(item_id)
    return [item for item in items if item.get("id") in mapped]


def work_item_scope_hash(items: list[dict], criteria: list[dict] | None = None) -> str:
    scope = sorted(({"id": item.get("id"), "kind": item.get("kind"),
                    "number": item.get("number")} for item in scoped_work_items(items, criteria)),
                   key=lambda item: item["id"] or "")
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def work_item_scope_hashes(items: list[dict], criteria: list[dict] | None = None) -> dict[str, str]:
    """Current digest plus every historical formula a recorded decision may
    carry: the pre-v0.3.11 digest over all items with `required`, the same
    with `required` normalized to true, and the v0.3.11 identity-only
    digest over all items. Gates accept any of them so runs recorded by an
    earlier engine keep their approvals."""
    current = work_item_scope_hash(items, criteria)
    all_items = work_item_scope_hash(items)
    legacy_scope = sorted(({
        "id": item.get("id"), "kind": item.get("kind"),
        "number": item.get("number"), "required": item.get("required", True),
    } for item in items), key=lambda item: item["id"] or "")
    legacy = hashlib.sha256(json.dumps(legacy_scope, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    legacy_normalized_scope = sorted(({**item, "required": True} for item in legacy_scope),
                                     key=lambda item: item["id"] or "")
    legacy_normalized = hashlib.sha256(json.dumps(legacy_normalized_scope, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
    return {"current": current, "all_items": all_items, "legacy": legacy,
            "legacy_normalized": legacy_normalized}


def scope_hash_matches(recorded: object, items: list[dict], criteria: list[dict] | None = None) -> bool:
    """Accept any known digest so existing approvals remain valid after migration."""
    return recorded in work_item_scope_hashes(items, criteria).values()


def work_item_delivery(status: dict, item_id: str) -> dict:
    record = (status.get("work_item_delivery") or {}).get(item_id)
    if isinstance(record, dict):
        return record
    return {
        "lane": "full", "requested_lane": "full", "confirmed_by": None,
        "confirmed_at": None, "facts": None, "escalation_reason": None,
        "implemented_by": status.get("implemented_by"),
        "reviewed_by": status.get("reviewed_by"), "review_hash": None,
        "baseline_head": None,
    }


def item_criteria(acceptance: dict, item_id: str) -> list[dict]:
    registry = acceptance.get("work_items") or []
    return [criterion for criterion in acceptance.get("criteria", [])
            if criterion_work_item(criterion, registry) == item_id]


def item_acceptance_hash(acceptance: dict, item_id: str) -> str:
    return acceptance_hash(item_criteria(acceptance, item_id))


def item_progress(status: dict, acceptance: dict, cfg: dict, item_id: str) -> dict:
    own = item_criteria(acceptance, item_id)
    delivery = work_item_delivery(status, item_id)
    passing = sum(c.get("state") == "passing" for c in own)
    criteria_points = 60.0 * passing / len(own) if own else 0.0
    lane_gate = (bool(delivery.get("confirmed_by")) if delivery.get("lane") == "small-fix"
                 else not _design_errors(status, acceptance, cfg)
                 and not _design_review_errors(status, acceptance, cfg))
    implemented_by = delivery.get("implemented_by")
    implemented = bool(implemented_by and any(c.get("evidence") for c in own))
    item_hash = item_acceptance_hash(acceptance, item_id)
    reviewed = bool(delivery.get("reviewed_by") and delivery.get("review_hash") == item_hash)
    global_review = status.get("review") or {}
    if global_review.get("acceptance_hash") == acceptance_hash(acceptance.get("criteria", [])):
        reviewed = True
    approval = status.get("deployment_approved") or {}
    deployed = (not adaptive_deployment_approval_required(status, cfg)
                or approval.get("acceptance_hash") == acceptance_hash(acceptance.get("criteria", [])))
    live = not cfg.get("require_live_verification", True) or bool(status.get("live_verification_id"))
    gates = {"lane": lane_gate, "implemented": implemented, "reviewed": reviewed,
             "deployed": deployed, "live": live}
    # #102: gate weights, not phase weights. An approved design reads 25,
    # partial evidence climbs from 25 to 45 with the passing fraction, and
    # each later gate lands on its own step, so a run never reads 8 percent
    # with its design fully approved.
    # A small-fix item has no design review; its lane confirmation is the
    # equivalent step.
    design_reviewed = (lane_gate if delivery.get("lane") == "small-fix"
                       else not _design_review_errors(status, acceptance, cfg))
    fraction = passing / len(own) if own else 0.0
    symptom = bool((status.get("requirement_coverage") or {}).get("original_symptom_resolved")
                   or status.get("original_symptom_evidence_id"))
    phase = int(status.get("phase_number", 0) or 0)
    value = 5
    if design_reviewed:
        value = 15
    if lane_gate:
        value = 25 + int(math.floor(20 * fraction + 0.5))
    if own and passing == len(own) and lane_gate:
        value = 45
        if symptom:
            value = 50
        if reviewed:
            value = 65
        # A gate the project switched off clears with the phase that would
        # have asked for it, never ahead of the run (the 95 percent step is
        # reserved for a work item that is actually done).
        if reviewed and deployed and phase >= 7:
            value = 80
        if reviewed and deployed and live and phase >= 8:
            value = 95
    if status.get("status") == "complete" or phase >= 8 and reviewed and deployed and live:
        value = 100
    value = min(100, max(0, value))
    return {"percent": value, "passing": passing, "total": len(own), "gates": gates,
            "lane": delivery.get("lane", "full"), "facts": delivery.get("facts"),
            "escalation_reason": delivery.get("escalation_reason")}


def overall_item_progress(status: dict, acceptance: dict, cfg: dict) -> int:
    items, _ = effective_work_items(acceptance, cfg)
    values = [item_progress(status, acceptance, cfg, item["id"])["percent"]
              for item in items if item.get("required", True)]
    return int(math.floor(sum(values) / len(values) + 0.5)) if values else 0


def open_amendment(status: dict) -> dict | None:
    """The open amendment record on `status`, or None. Only state `open`
    counts; a closed record left in `status["amendment"]` by a hand edit is
    a schema error, never a silent freeze."""
    record = status.get("amendment") if isinstance(status, dict) else None
    return record if isinstance(record, dict) and record.get("state") == "open" else None


def criterion_work_item(criterion: dict, registry: list[dict]) -> str:
    """The work item a criterion belongs to, by the same rule
    derive_work_items renders: its leading tag, else the only item of a
    single-item run, else `unattributed`."""
    item_id = criterion_work_item_id(criterion)
    if item_id is None and len(registry) == 1:
        return registry[0]["id"]
    known = {item.get("id") for item in registry}
    return item_id if item_id in known else "unattributed"


VERIFY_INFLIGHT_DIR = ".handsoff-verify-inflight"


# Handsoff's own generated state, never part of the repository digest in
# either mode: the ledgers, anchors, locks, beacons, and the in-flight
# directory would otherwise churn the digest on every evidence write.
HANDSOFF_GENERATED_NAMES = frozenset({
    ".handsoff.lock", ".handsoff-event-head.json", ".handsoff-writeahead.json",
    ".handsoff-session-liveness.json", ".handsoff-dashboard-owner.json",
    # Agent output is gitignored Handsoff runtime state, not repository evidence.
    ".handsoff-agent-output.json", ".handsoff-regression.json", ".handsoff-test-progress.json",
    LIVE_BEACON_FILE, OUTPUT_LIVENESS_FILE, DESIGN_EVIDENCE_FILE, PREFLIGHT_FILE,
    LIVE_INFLIGHT_FILE,
    ".handsoff-selfcheck", ".handsoff-archive", VERIFY_INFLIGHT_DIR, ANALYSIS_DIR,
    "__pycache__", ".git",
})


def _digest_listing(root: Path, cfg: dict) -> list[str]:
    """List candidate paths once, applying git and configured ignore rules."""
    listed = None
    try:
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(root),
                               capture_output=True, text=True, timeout=10, check=False)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            tracked = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True,
                                     timeout=30, check=True).stdout
            untracked = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                                       cwd=str(root), capture_output=True, timeout=30, check=True).stdout
            listed = [x.decode("utf-8", "replace") for x in (tracked + untracked).split(b"\0") if x]
    except (OSError, subprocess.SubprocessError):
        listed = None
    if listed is None:
        listed = []
        gitignore_rules = []
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in HANDSOFF_GENERATED_NAMES)
            base = Path(directory).relative_to(root).as_posix()
            if base == ".": base = ""
            if ".gitignore" in files:
                for line in (Path(directory) / ".gitignore").read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("!"):
                        gitignore_rules.append((base, line))
            for filename in files:
                if filename == ".gitignore":
                    continue
                relative = filename if not base else f"{base}/{filename}"
                ignored = False
                for rule_base, rule in gitignore_rules:
                    target = relative[len(rule_base) + 1:] if rule_base and relative.startswith(rule_base + "/") else relative
                    pattern = rule.rstrip("/")
                    if rule.endswith("/") and (target == pattern or target.startswith(pattern + "/")):
                        ignored = True
                    elif rule.startswith("/") and fnmatch.fnmatch(target, pattern.lstrip("/")):
                        ignored = True
                    elif fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(Path(target).name, pattern):
                        ignored = True
                if not ignored:
                    listed.append(relative)
    ignores = [item for item in cfg.get("digest_ignore", []) if isinstance(item, str)]
    return sorted({path for path in listed
                   if not any(fnmatch.fnmatch(path, glob) or any(fnmatch.fnmatch(part, glob) for part in path.split("/"))
                              for glob in ignores)})


HANDSOFF_TEMP_COMPONENT = re.compile(r"\.*handsoff[^/]*\.tmp-?\d+[^/]*")


def _digest_excluded(relative: str, state_files: set[str]) -> bool:
    """Runtime bookkeeping never counts as repository content. Beyond the
    enumerated names, every `.handsoff*` path component is Handsoff side
    state (locks, beacons, output tails, the version pin, future files).
    Without the structural rule a managed session running after `verify`
    would write a side file and flag its own evidence stale on a root that
    is not a git checkout (#77)."""
    parts = relative.split("/")
    if relative in state_files or relative in {f"{name}.bak" for name in state_files}:
        return True
    if any(part in HANDSOFF_GENERATED_NAMES for part in parts):
        return True
    # Every `.handsoff*` path component, the version pin included: the pin
    # is Handsoff configuration, not product source. `upgrade --to` rewrites
    # it on every engine upgrade, which used to stale the evidence of every
    # completed run in the project (v0.3.25 field-note defect 2). The engine
    # identity stays auditable through `engine_history` and the engine
    # recorded on every initialized and agent_session_launching event.
    if any(part.startswith(".handsoff") for part in parts):
        return True
    # #203: the in-flight temp file of Handsoff's own atomic writers
    # (`..handsoff-live.json.tmp-<pid>-<hex>`, `handsoff-status.json.tmp<pid>`)
    # exists for a few milliseconds between write and rename. A digest scan
    # that lands in that window used to see a file the other scan did not,
    # and a managed reviewer was blamed for a tree it never touched.
    if any(HANDSOFF_TEMP_COMPONENT.fullmatch(part) for part in parts):
        return True
    # handsoff.toml is Handsoff's own configuration, not the product under
    # test: a live_commands line or a budget tweak must not read as source
    # drift (#93). What configuration CAN change the meaning of evidence,
    # the check commands, is bound separately through the verification
    # config hash stored on every executed record (see evidence_drift).
    if relative == "handsoff.toml":
        return True
    return parts[-1].endswith(".pyc")


def _digest_entry(root: Path, relative: str) -> str | None:
    path = root / relative
    if path.is_symlink():
        return hashlib.sha256(("symlink:" + os.readlink(path)).encode("utf-8", "replace")).hexdigest()
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_digest(root: Path, cfg: dict | None = None) -> str:
    """sha256 over the sorted (relative path, file sha256) pairs of every
    tracked file plus every untracked-not-ignored file when `root` is a git
    checkout (dirty state included by construction: the working tree
    content is hashed, not HEAD). A root that is not a git checkout hashes
    every file under it minus Handsoff's generated names. A tracked file
    deleted from the working tree contributes a null hash, so a deletion
    changes the digest too. Gitignored Handsoff state files are omitted by
    git's untracked-file query, preventing runtime bookkeeping from causing
    evidence drift."""
    names = cfg or DEFAULT_CONFIG
    state_files = {names["status_file"], names["acceptance_file"], names["event_log"], names["verification_log"]}
    pairs = []
    for relative in _digest_listing(root, names):
        if _digest_excluded(relative, state_files):
            continue
        pairs.append([relative, _digest_entry(root, relative)])
    return hashlib.sha256(_canonical({"files": pairs}).encode("utf-8")).hexdigest()


def repository_digest_entries(root: Path, cfg: dict | None = None) -> dict[str, str | None]:
    """Return per-path working-tree digests so a sandbox violation can name files.

    Handsoff state is excluded for the same reason as repository_digest: its own
    bookkeeping must not look like an agent edit.
    """
    names = cfg or load_config(root)
    state_files = {names["status_file"], names["acceptance_file"], names["event_log"], names["verification_log"]}
    return {path: _digest_entry(root, path) for path in _digest_listing(root, names)
            if not _digest_excluded(path, state_files)}


def verification_config_hash(cfg: dict) -> str:
    """The configuration that changes what a targeted check proves: the
    governance keys, [checks].commands, the per-command timeout, and the
    regression groups. Distinct from config_hash (which binds decisions to
    governance policy alone) and deliberately blind to file paths,
    [agents], [models], [fallback_policy], [recovery], [design_evidence]
    and tickets, none of which alter a check's meaning."""
    bound = {key: cfg.get(key) for key in GOVERNANCE_CONFIG_KEYS}
    bound["check_commands"] = list(cfg.get("check_commands", []))
    bound["check_timeout_seconds"] = cfg.get("check_timeout_seconds")
    bound["regressions"] = [{"name": group.get("name"), "commands": list(group.get("commands", []))}
                            for group in cfg.get("regressions", [])]
    return hashlib.sha256(_canonical(bound).encode("utf-8")).hexdigest()
