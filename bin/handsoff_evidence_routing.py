#!/usr/bin/env python3
"""Evidence-assisted routing: let measured outcomes reorder qualified candidates.

#303, the selection step of the #299 epic. #300 projects what each session
cost and produced, #301 says how far a cohort can be trusted, #302 measures a
frozen cheaper policy against a holdout. This module is the only place that
evidence may change which model a managed launch gets, and only after the
measurement was approved.

**Off by default.** `[adaptive_routing] evidence_assisted` defaults to false,
and even when true assistance applies only while the latest `activate` or
`rollback` event on this module's ledger is an `activate` recorded under the
engine's current `EVIDENCE_POLICY_VERSION`. A `no_decision` record never
changes that. When the switch is off, or nothing was ever activated, the
launch path never calls into this module, so the persisted `adaptive_routing`
record is the pre-#303 record exactly.

**Activation needs a measurement.** `activate` refuses unless a #302 finding
and the Mission Control approval recorded for it are named, through the same
chain-verified approval ledger `handsoff_shadow.apply_finding` reads, and the
approval is consumed so it cannot activate twice.

**Hard constraints first.** Selection only reorders the candidates
`route_adaptive_profile` already admitted (risk floor, model policy,
capabilities, adapter availability) and re-checks the floor itself. A
candidate whose outcome or token evidence is missing, insufficient, stale or
conflicting is excluded with its reason; if none remains, the static choice is
kept and labelled `static_fallback`.

**Score** is the expected USD cost per verified completion:
((mean_input_tokens * input_per_mtok + mean_output_tokens * output_per_mtok)
/ 1e6) / Wilson lower bound of the verified Phase-8 rate, from one model-version
cohort per candidate (the sufficient one with the largest n), never pooled.

**Rollback.** At every Phase-8 completion `rollback_monitor` compares the
retry, review-rejection and incomplete-run rates of assisted decisions with
static ones from the same recent window, and records a `rollback` naming the
metric when assistance is worse by more than the margin. Cost is never an
input. Assistance then stays off until a fresh approved activation.

Layer: core -> config -> evidence -> cohorts -> shadow -> here.
"""
from __future__ import annotations

import json
import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

import handsoff_cohorts as cohorts
import handsoff_shadow as shadow
from handsoff_core import HandsoffError, _canonical, load_unique_json, project_lock
from handsoff_evidence import DERIVATION_VERSION, UNKNOWN

#: The version of the scoring rule below. An activation is bound to it; a bump
#: leaves every older activation inert until a fresh approved one.
EVIDENCE_POLICY_VERSION = "evidence-wilson.1"

TIERS = ("FAST", "STANDARD", "PREMIUM")
LEDGER_KINDS = ("activate", "rollback", "no_decision")
MODES = ("off", "assisted", "static_fallback")
#: Why a launch was or was not assisted.
REASONS = ("not_activated", "rolled_back", "policy_version_mismatch",
           "assisted_choice", "no_qualified_candidate")
#: Why one candidate was left out of assisted scoring, in the order checked.
EXCLUSION_REASONS = ("below_floor", "cold_start", "outcome_evidence_insufficient", "conflicting",
                     "stale", "pricing_unknown", "token_evidence_insufficient", "below_threshold")
ACTIVATION_ID = re.compile(r"^era-[0-9a-f]{16}$")
MAX_LEDGER_ENTRY_BYTES = 4096
MAX_RECORDED_CANDIDATES = 16

DEFAULT_EVIDENCE_CONFIG = {"threshold": 0.5, "min_sample": 20, "staleness_days": 30}
DEFAULT_ROLLBACK_CONFIG = {"window": 20, "min_assisted": 5, "min_baseline": 5, "margin": 0.10}
#: What the rollback monitor compares, in the order checked. Cost is not one.
ROLLBACK_METRICS = ("retries", "review_rejections", "incomplete_runs")
MAX_ROLLBACK_WINDOW = 200


class EvidenceRoutingRefusal(HandsoffError):
    """Activation refused, with the reason named."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def evidence_config(root: Path) -> dict:
    """`[adaptive_routing]` from handsoff.toml: the switch (default off) and
    the `[adaptive_routing.evidence]` scoring settings, with the
    `[adaptive_routing.evidence.rollback]` monitor settings validated by
    `rollback_config`."""
    result = {"evidence_assisted": False, **DEFAULT_EVIDENCE_CONFIG, "rollback": {}}
    path = Path(root) / "handsoff.toml"
    if not path.is_file():
        return result
    if tomllib is None:
        raise HandsoffError("handsoff.toml present but no TOML parser available (need Python 3.11+)")
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("adaptive_routing", {})
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"cannot load {path}: {exc}") from exc
    if not isinstance(table, dict) or set(table) - {"evidence_assisted", "evidence"}:
        raise HandsoffError("handsoff.toml: adaptive_routing allows only evidence_assisted and evidence")
    switch = table.get("evidence_assisted", False)
    if not isinstance(switch, bool):
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence_assisted must be boolean")
    settings = table.get("evidence", {})
    if not isinstance(settings, dict) or set(settings) - {*DEFAULT_EVIDENCE_CONFIG, "rollback"}:
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence allows only "
                            "threshold, min_sample, staleness_days and rollback")
    threshold = settings.get("threshold", DEFAULT_EVIDENCE_CONFIG["threshold"])
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence.threshold must be in (0, 1]")
    for name in ("min_sample", "staleness_days"):
        value = settings.get(name, DEFAULT_EVIDENCE_CONFIG[name])
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HandsoffError(f"handsoff.toml: adaptive_routing.evidence.{name} must be a positive integer")
        result[name] = value
    rollback = settings.get("rollback", {})
    if not isinstance(rollback, dict):
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence.rollback must be a table")
    result.update(evidence_assisted=switch, threshold=float(threshold), rollback=rollback_config(rollback))
    return result


def rollback_config(table: dict) -> dict:
    """The rollback monitor's settings. Both arms need at least one decision
    and must fit the window together, or the configuration is refused."""
    unknown = sorted(set(table) - set(DEFAULT_ROLLBACK_CONFIG))
    if unknown:
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence.rollback allows only "
                            "window, min_assisted, min_baseline and margin")
    result = {**DEFAULT_ROLLBACK_CONFIG, **table}
    for name in ("window", "min_assisted", "min_baseline"):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ROLLBACK_WINDOW:
            raise HandsoffError(f"handsoff.toml: adaptive_routing.evidence.rollback.{name} must be "
                                f"an integer from 1 to {MAX_ROLLBACK_WINDOW}")
    if result["min_assisted"] + result["min_baseline"] > result["window"]:
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence.rollback: "
                            "min_assisted + min_baseline must not exceed window")
    margin = result["margin"]
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not 0 <= margin <= 1:
        raise HandsoffError("handsoff.toml: adaptive_routing.evidence.rollback.margin must be in [0, 1]")
    result["margin"] = float(margin)
    return result


# --------------------------------------------------------------------------
# Ledger: activate, rollback, no_decision; hash-chained
# --------------------------------------------------------------------------

def ledger_path(root: Path) -> Path:
    return Path(root) / ".handsoff" / "evidence-routing" / "ledger.jsonl"


def read_ledger(root: Path) -> list[dict]:
    """Every event, chain verified; a broken chain refuses."""
    path = ledger_path(root)
    if not path.is_file():
        return []
    entries, prev = [], "GENESIS"
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HandsoffError(f"evidence routing ledger line {number} is not JSON") from exc
        body = {k: v for k, v in entry.items() if k != "hash"} if isinstance(entry, dict) else {}
        if body.get("prev_hash") != prev or entry.get("hash") != shadow._sha256(body) \
                or body.get("kind") not in LEDGER_KINDS:
            raise HandsoffError(f"evidence routing ledger chain is broken at line {number}")
        prev = entry["hash"]
        entries.append(entry)
    return entries


def append_ledger(root: Path, kind: str, **fields) -> dict:
    """Append one event. The caller holds the project lock."""
    if kind not in LEDGER_KINDS:
        raise HandsoffError(f"evidence routing ledger kind must be one of {', '.join(LEDGER_KINDS)}")
    entries = read_ledger(root)
    body = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind,
            "prev_hash": entries[-1]["hash"] if entries else "GENESIS", **fields}
    body["hash"] = shadow._sha256(body)
    line = _canonical(body)
    if len(line.encode("utf-8")) > MAX_LEDGER_ENTRY_BYTES:
        raise HandsoffError(f"evidence routing ledger entry exceeds {MAX_LEDGER_ENTRY_BYTES} bytes")
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return body


def assisted_state(root: Path, cfg: dict | None = None) -> dict:
    """Whether assistance applies now, derived only from the switch, the
    latest activate or rollback event, and the engine's policy version."""
    cfg = evidence_config(root) if cfg is None else cfg
    if not cfg["evidence_assisted"]:
        return {"active": False, "reason": "disabled", "activation": None}
    decisive = [entry for entry in read_ledger(root) if entry.get("kind") in ("activate", "rollback")]
    activation = next((entry for entry in reversed(decisive) if entry["kind"] == "activate"), None)
    if not decisive:
        return {"active": False, "reason": "not_activated", "activation": None}
    if decisive[-1]["kind"] == "rollback":
        return {"active": False, "reason": "rolled_back", "activation": activation}
    if activation.get("policy_version") != EVIDENCE_POLICY_VERSION:
        return {"active": False, "reason": "policy_version_mismatch", "activation": activation}
    return {"active": True, "reason": "activated", "activation": activation}


def activate(root: Path, *, finding: str, approval: str, by: str) -> dict:
    """Record one activation. Refuses unless Mission Control recorded
    `approval` for `finding` and it has not been used; consumes it."""
    root = Path(root)
    if not isinstance(finding, str) or not shadow.FINDING_ID.match(finding):
        raise EvidenceRoutingRefusal("evidence routing activate: a #302 finding id (shf-...) is required")
    if not isinstance(approval, str) or not shadow.APPROVAL_ID.match(approval):
        raise EvidenceRoutingRefusal("evidence routing activate: a Mission Control approval id (apr-...) is required")
    if not isinstance(by, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", by):
        raise EvidenceRoutingRefusal("evidence routing activate: --by must be an actor name")
    path = shadow.approvals_dir(root) / f"{approval}.json"
    if not path.is_file():
        raise EvidenceRoutingRefusal(f"evidence routing activate: no Mission Control approval {approval} exists")
    record = shadow.validate_approval(load_unique_json(path))
    if record["approval_id"] != approval:
        raise EvidenceRoutingRefusal(f"evidence routing activate: approval file {approval} names another approval")
    if record["finding_id"] != finding:
        raise EvidenceRoutingRefusal(f"evidence routing activate: approval {approval} is bound to finding "
                                     f"{record['finding_id']}, not {finding}")
    with project_lock(root):
        entries = shadow._ledger(root)
        if not any(e.get("kind") == "recorded" and e.get("approval_id") == approval
                   and e.get("approval_sha256") == shadow._sha256(record) for e in entries):
            raise EvidenceRoutingRefusal(f"evidence routing activate: approval {approval} "
                                         "was not recorded by Mission Control")
        if any(e.get("kind") == "consumed" and e.get("approval_id") == approval for e in entries):
            raise EvidenceRoutingRefusal(f"evidence routing activate: approval {approval} was already used")
        read_ledger(root)  # refuse on a broken chain before consuming anything
        shadow._ledger_append(root, "consumed", approval_id=approval, finding_id=finding,
                              change_sha256=record["change_sha256"])
        return append_ledger(root, "activate", activation_id="era-" + secrets.token_hex(8),
                             finding_id=finding, approval_id=approval,
                             policy_version=EVIDENCE_POLICY_VERSION, actor=by)


# --------------------------------------------------------------------------
# Selection: pure given its inputs
# --------------------------------------------------------------------------

def _wilson_lower(successes: int, n: int) -> float | None:
    interval = shadow.wilson(successes, n)
    return interval[0] if interval else None


def _cohort_records(observations: list[dict], now, settings: dict, key: dict,
                    widened: set, version: str) -> list[dict]:
    """The records of one version cohort, filtered exactly as `aggregate`
    filters its population, so token means come from the same sessions the
    success counts did."""
    moment = cohorts._utc(now)
    population = []
    for observation in observations:
        record = observation.get("evidence") or {}
        when = cohorts.observed_at(observation)
        if record.get("derivation_version") != DERIVATION_VERSION or cohorts.is_fixture(observation) \
                or when is None or when > moment:
            continue
        population.append((record, when))
    population, _ = cohorts._retain(population, settings)
    return [record for record, _ in population
            if cohorts._matches(record, key, widened) and cohorts.model_version(record) == version]


def _version_successes(cohort: dict) -> tuple[int, int]:
    metric = cohort["metrics"]["success_rate"]
    n = metric["n_known"]
    return (round(metric["value"] * n) if metric["value"] is not None else 0), n


def evaluate_candidate(candidate: dict, *, floor: str, observations: list[dict], now,
                       key_base: dict, cfg: dict) -> dict:
    """One candidate's evidence, its score, or the reason it is excluded."""
    tier, profile = candidate["tier"], candidate["profile"]
    entry = {"tier": tier, "adapter": profile["adapter"], "model": profile["model"],
             "cohort_version": None, "n": 0, "successes": 0, "wilson_lower": None,
             "mean_input_tokens": None, "mean_output_tokens": None, "score_usd": None,
             "excluded_reason": None}

    def excluded(reason):
        entry["excluded_reason"] = reason
        return entry

    if TIERS.index(tier) < TIERS.index(floor):
        return excluded("below_floor")
    settings = {"min_sample": cfg["min_sample"], "staleness_days": cfg["staleness_days"]}
    key = {**key_base, "adapter": profile["adapter"], "model": profile["model"]}
    result = cohorts.aggregate(observations, DERIVATION_VERSION, now, settings, key)
    if not result["cohorts"]:
        return excluded("cold_start")
    sufficient = [cohort for cohort in result["cohorts"] if cohort["sufficient"]]
    if not sufficient:
        return excluded("outcome_evidence_insufficient")
    intervals = [shadow.wilson(*_version_successes(cohort)) for cohort in sufficient]
    if any(a[1] < b[0] or b[1] < a[0] for i, a in enumerate(intervals) for b in intervals[i + 1:]):
        return excluded("conflicting")
    # One cohort, never pooled: largest n, then the newest last observation.
    chosen = min(sufficient, key=lambda cohort: (-_version_successes(cohort)[1],
                                                 cohort["age"]["newest_s"], cohort["key"]["model"]))
    successes, n = _version_successes(chosen)
    entry.update(cohort_version=chosen["key"]["model"], n=n, successes=successes,
                 wilson_lower=_wilson_lower(successes, n))
    if chosen["stale_or_biased"]:
        return excluded("stale")
    pricing = profile.get("pricing") or {}
    if set(pricing) != {"input_per_mtok", "output_per_mtok"}:
        return excluded("pricing_unknown")
    widened = {item["dimension"] for item in result["widened"]}
    records = _cohort_records(observations, now, {**cohorts.DEFAULT_CONFIG, **settings},
                              key, widened, chosen["key"]["model"])
    full = [record["tokens"] for record in records if shadow.full_usage(record)]
    if len(full) < cfg["min_sample"]:
        return excluded("token_evidence_insufficient")
    entry["mean_input_tokens"] = sum(item["tokens_in"] for item in full) / len(full)
    entry["mean_output_tokens"] = sum(item["tokens_out"] for item in full) / len(full)
    if entry["wilson_lower"] is None or entry["wilson_lower"] < cfg["threshold"]:
        return excluded("below_threshold")
    unit = (entry["mean_input_tokens"] * pricing["input_per_mtok"]
            + entry["mean_output_tokens"] * pricing["output_per_mtok"]) / 1e6
    entry["score_usd"] = unit / entry["wilson_lower"]
    return entry


def select(candidates: list[dict], static: dict, *, floor: str, observations: list[dict], now,
           key_base: dict, cfg: dict, activation: dict) -> tuple[dict, dict]:
    """The assisted pick among `candidates` ({tier, profile}), or `static`.
    Returns (chosen candidate, evidence record for adaptive_routing)."""
    scored = [evaluate_candidate(candidate, floor=floor, observations=observations, now=now,
                                 key_base=key_base, cfg=cfg) for candidate in candidates]
    eligible = [(entry, candidate) for entry, candidate in zip(scored, candidates)
                if entry["excluded_reason"] is None]
    record = {"mode": "static_fallback", "reason": "no_qualified_candidate",
              "policy_version": EVIDENCE_POLICY_VERSION, "activation_id": activation["activation_id"],
              "threshold": cfg["threshold"],
              "static": {"tier": static["tier"], "adapter": static["profile"]["adapter"],
                         "model": static["profile"]["model"]},
              "candidates": scored[:MAX_RECORDED_CANDIDATES]}
    if not eligible:
        return static, record
    _, chosen = min(eligible, key=lambda pair: (pair[0]["score_usd"], TIERS.index(pair[0]["tier"]),
                                                pair[0]["adapter"], pair[0]["model"]))
    record.update(mode="assisted", reason="assisted_choice")
    return chosen, record


def off_record(state: dict) -> dict:
    """The record a launch carries when an activation exists but does not apply."""
    return {"mode": "off", "reason": state["reason"], "policy_version": EVIDENCE_POLICY_VERSION,
            "activation_id": state["activation"]["activation_id"], "threshold": None,
            "static": None, "candidates": []}


def launch_selector(root: Path, *, role: str, status: dict, now=None):
    """What the launch path hands `route_adaptive_profile`: None when nothing
    was ever activated or the switch is off (the pre-#303 path, untouched),
    else a callable choosing among the admitted candidates."""
    cfg = evidence_config(root)
    state = assisted_state(root, cfg)
    if state["activation"] is None:
        return None
    if not state["active"]:
        record = off_record(state)
        return lambda candidates, static, floor: (static, deepcopy(record))
    import handsoff_lib as lib
    moment = now or datetime.now(timezone.utc).isoformat()
    directory = lib.archive_dir()
    observations = [item for item in (cohorts.load_observations(directory) if directory.is_dir() else [])
                    if (item.get("evidence") or {}).get("role") == role]
    from handsoff_evidence import recorded_work_item_labels, task_class_for_labels
    policy = status.get("model_policy")
    key_base = {"repository": Path(root).name,
                "task_class": task_class_for_labels(recorded_work_item_labels(status)),
                "phase": int(status.get("phase_number", 1) or 1),
                "policy_version": policy.get("version", UNKNOWN) if isinstance(policy, dict) else UNKNOWN}

    def selector(candidates, static, floor):
        return select(candidates, static, floor=floor, observations=observations, now=moment,
                      key_base=key_base, cfg=cfg, activation=state["activation"])
    return selector


# --------------------------------------------------------------------------
# Rollback: assisted against static decisions from the same recent window
# --------------------------------------------------------------------------

def completed_decisions(directory: Path, repository: str) -> list[dict]:
    """Every routed session with a known outcome in this repository's
    archived product runs, oldest first. Mode `assisted` is the assisted
    arm; no evidence block, `off` or `static_fallback` is the static
    baseline. Token usage is not read, so cost cannot reach the monitor."""
    from handsoff_config import classify_archive_record
    from handsoff_evidence import _replacement_count, session_outcome
    rows = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HandsoffError(f"evidence routing rollback: {path.name} is not readable JSON: {exc}") from exc
        if not isinstance(archive, dict) or archive.get("repo") != repository \
                or classify_archive_record(archive, path.name) == "test":
            continue
        status = archive.get("status") if isinstance(archive.get("status"), dict) else {}
        sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}
        for sid, session in sessions.items():
            routing = session.get("adaptive_routing") if isinstance(session, dict) else None
            if not isinstance(routing, dict):
                continue
            outcome = session_outcome(status, session)
            if outcome == UNKNOWN:
                continue
            block = routing.get("evidence") if isinstance(routing.get("evidence"), dict) else {}
            rows.append({"order": (str(archive.get("completed_at") or ""), str(session.get("started_at") or ""),
                                   path.name, str(sid)),
                         "mode": block.get("mode") or "static", "activation_id": block.get("activation_id"),
                         "retries": _replacement_count(status, sid) > 0,
                         "review_rejections": outcome == "review_changes_requested",
                         "incomplete_runs": outcome != "verified_phase8"})
    rows.sort(key=lambda row: row["order"])
    return rows


def rollback_monitor(root: Path, *, directory: Path | None = None) -> dict:
    """Run once at Phase-8 completion while assistance is active. Takes the
    last `window` decisions; the assisted arm is this activation's assisted
    decisions, the baseline every static one. An arm under its minimum
    records no_decision; an assisted rate above the baseline's by more than
    `margin` records a rollback naming the metric."""
    root = Path(root)
    cfg = evidence_config(root)
    settings = cfg["rollback"]
    if directory is None:
        import handsoff_lib as lib
        directory = lib.archive_dir()
    with project_lock(root):
        state = assisted_state(root, cfg)
        if not state["active"]:
            return {"state": "inactive", "reason": state["reason"]}
        activation_id = state["activation"]["activation_id"]
        rows = completed_decisions(directory, root.name) if Path(directory).is_dir() else []
        window = rows[-settings["window"]:]
        assisted = [row for row in window if row["mode"] == "assisted" and row["activation_id"] == activation_id]
        baseline = [row for row in window if row["mode"] != "assisted"]
        counts = {"window": len(window), "assisted": len(assisted), "baseline": len(baseline)}
        if len(assisted) < settings["min_assisted"] or len(baseline) < settings["min_baseline"]:
            reason = ("assisted_below_minimum" if len(assisted) < settings["min_assisted"]
                      else "baseline_below_minimum")
            append_ledger(root, "no_decision", activation_id=activation_id, metric=None, reason=reason,
                          counts=counts, config=settings)
            return {"state": "no_decision", "reason": reason, "counts": counts}
        rates = {metric: {"assisted": sum(row[metric] for row in assisted) / len(assisted),
                          "baseline": sum(row[metric] for row in baseline) / len(baseline)}
                 for metric in ROLLBACK_METRICS}
        breached = next((metric for metric in ROLLBACK_METRICS
                         if round(rates[metric]["assisted"] - rates[metric]["baseline"], 12) > settings["margin"]),
                        None)
        if breached is None:
            return {"state": "kept", "counts": counts, "rates": rates}
        append_ledger(root, "rollback", activation_id=activation_id, metric=breached, counts=counts,
                      rates=rates, config=settings)
        return {"state": "rolled_back", "metric": breached, "counts": counts, "rates": rates}


# --------------------------------------------------------------------------
# The persisted record's shape
# --------------------------------------------------------------------------

def _number(value, *, nullable=True, maximum=None) -> bool:
    if value is None:
        return nullable
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value \
        and value >= 0 and (maximum is None or value <= maximum)


def validate_evidence_record(value: object) -> dict:
    """The `adaptive_routing.evidence` block a session may carry."""
    fields = {"mode", "reason", "policy_version", "activation_id", "threshold", "static", "candidates"}
    if not isinstance(value, dict) or set(value) != fields:
        raise HandsoffError("adaptive_routing.evidence has invalid fields")
    if value["mode"] not in MODES or value["reason"] not in REASONS:
        raise HandsoffError("adaptive_routing.evidence mode or reason is invalid")
    if not isinstance(value["policy_version"], str) or not shadow.POLICY_VERSION.match(value["policy_version"]):
        raise HandsoffError("adaptive_routing.evidence policy_version is malformed")
    if not isinstance(value["activation_id"], str) or not ACTIVATION_ID.match(value["activation_id"]):
        raise HandsoffError("adaptive_routing.evidence activation_id is malformed")
    if not _number(value["threshold"], maximum=1):
        raise HandsoffError("adaptive_routing.evidence threshold is invalid")
    static = value["static"]
    if static is not None and (not isinstance(static, dict) or set(static) != {"tier", "adapter", "model"}
                               or static["tier"] not in TIERS):
        raise HandsoffError("adaptive_routing.evidence static choice is invalid")
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_RECORDED_CANDIDATES:
        raise HandsoffError(f"adaptive_routing.evidence candidates must be a list of at most {MAX_RECORDED_CANDIDATES}")
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {
                "tier", "adapter", "model", "cohort_version", "n", "successes", "wilson_lower",
                "mean_input_tokens", "mean_output_tokens", "score_usd", "excluded_reason"}:
            raise HandsoffError("adaptive_routing.evidence candidate has invalid fields")
        if item["tier"] not in TIERS or item["excluded_reason"] not in (None, *EXCLUSION_REASONS):
            raise HandsoffError("adaptive_routing.evidence candidate tier or exclusion is invalid")
        version = item["cohort_version"]
        if version is not None and (not isinstance(version, str) or len(version) > 64):
            raise HandsoffError("adaptive_routing.evidence candidate cohort_version is invalid")
        n, successes = item["n"], item["successes"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0 or isinstance(successes, bool) \
                or not isinstance(successes, int) or not 0 <= successes <= n:
            raise HandsoffError("adaptive_routing.evidence candidate counts are invalid")
        if not (_number(item["wilson_lower"], maximum=1) and _number(item["mean_input_tokens"])
                and _number(item["mean_output_tokens"]) and _number(item["score_usd"])):
            raise HandsoffError("adaptive_routing.evidence candidate components are invalid")
    if value["mode"] == "assisted" and not any(item["excluded_reason"] is None for item in candidates):
        raise HandsoffError("adaptive_routing.evidence assisted with no eligible candidate")
    return deepcopy(value)
