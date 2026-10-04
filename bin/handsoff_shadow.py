#!/usr/bin/env python3
"""Shadow routing: what a frozen cheaper policy would have done, replayed.

#302, the third step of the #299 epic. The #300 records say what each session
cost and produced; the #301 cohorts say how far a metric can be trusted. This
module asks the question routing actually has: would a cheaper candidate have
done as well, judged on outcomes it never saw while it was being chosen.

**The split is on when the OUTCOME became known**, not on when the session
ended. A session can end on Monday and have its Phase-8 outcome land on
Friday; splitting on session end would let Friday's outcome train a policy
that is then scored on Tuesday. A record's split time is
`outcome_available_at`: the archive's completed (verified or terminal) time,
else its archived time. A record with neither is excluded and counted
(`unknown_availability`), never placed by a guess.

**The boundary is an input, fixed before anything is scored**, and recorded
with every other input in `inputs`. Records available strictly before it are
training; the rest are holdout.

**One freezing rule** (`POLICY_RULE`): per cohort, the cheapest candidate by
the declared cost table whose training Wilson 95% lower bound on the verified
Phase-8 rate meets `threshold`; the static baseline is always admissible and
wins every tie. The frozen policy is hashed (`policy_sha256`) before the
holdout is read, so nothing in the holdout can move the boundary or the
choice. Holdout scoring then reports baseline against policy per cohort.

**Nothing here routes.** Evaluation and shadow mode only read. Both run under
`routing_unchanged`, which hashes the routing configuration and the routed
choice before and after and fails on any difference. A recommendation reaches
routing only through `apply_finding`, and only with a Pilot approval that
Mission Control's approval endpoint wrote (`record_approval`), bound to the
finding id and to the sha256 of the exact proposed change, used once. No
actor name passed on a command line is ever an approval. Honest limit: a
same-user process able to read the dashboard's Pilot token, or to write the
approval ledger by hand, is outside this boundary.

Layer: core -> config -> evidence -> cohorts -> here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import HandsoffError, _atomic_write_text, _canonical, load_unique_json, project_lock
from handsoff_cohorts import is_fixture, model_version, observations_from_archive
from handsoff_evidence import DERIVATION_VERSION, UNKNOWN

#: The version of the freezing rule below. Every finding names it.
SHADOW_POLICY_VERSION = "shadow-wilson.1"

POLICY_RULE = ("per cohort, the cheapest candidate by the declared cost table whose training "
               "Wilson 95% lower bound meets the threshold, else the static baseline; ties keep the baseline")

#: z for a two-sided 95% Wilson interval.
WILSON_Z = 1.959963984540054

#: The default floor on full-usage coverage below which the scorer refuses.
DEFAULT_MIN_FULL_USAGE_COVERAGE = 0.5

#: The one outcome counted as a success.
VERIFIED_OUTCOME = "verified_phase8"

#: Quality flags that make a record low-quality evidence. The second variant
#: of every report drops records carrying any of them.
LOW_QUALITY_FLAGS = frozenset({"usage_not_reported", "usage_partial", "duration_unavailable", "legacy_archive"})

VARIANTS = ("all_evidence", "excluding_low_quality")

RECOMMENDATIONS = ("cheaper", "baseline", "refused")

MAX_COHORT_KEY = 128
MAX_APPROVALS = 256

APPROVAL_ID = re.compile(r"^apr-[a-z0-9]{12}$")
FINDING_ID = re.compile(r"^shf-[a-z0-9]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
POLICY_VERSION = re.compile(r"^[a-z0-9._-]{1,64}$")
APPROVAL_FIELDS = ("approval_id", "finding_id", "change_sha256", "policy_version", "created_at", "source")
APPROVAL_SOURCE = "mission_control"


class ShadowRefusal(HandsoffError):
    """The evaluator or the apply path refused, with the reason named."""


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _measured(value) -> dict:
    return {"value": value, "value_state": "measured"}


def _unavailable() -> dict:
    return {"value": None, "value_state": "unavailable"}


def wilson(successes: int, n: int, z: float = WILSON_Z) -> tuple[float, float] | None:
    """The Wilson score interval, or None for an empty sample."""
    if n <= 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


# --------------------------------------------------------------------------
# Replay records
# --------------------------------------------------------------------------

def outcome_available_at(archive: dict) -> str | None:
    """When this archive's outcomes became known: the completed (verified or
    terminal) time, else the archived time, else None."""
    for name in ("completed_at", "archived_at"):
        if _utc(archive.get(name)) is not None:
            return archive[name]
    return None


def replay_records_from_archive(archive: dict, *, source_sha256: str, source_name: str) -> list[dict]:
    """The #301 observations of one archive, each carrying the time its
    outcome became available."""
    available = outcome_available_at(archive)
    return [dict(item, outcome_available_at=available)
            for item in observations_from_archive(archive, source_sha256=source_sha256,
                                                  source_name=source_name)]


def load_replay_records(directory: Path) -> list[dict]:
    records = []
    for path in sorted(Path(directory).glob("*.json")):
        raw = path.read_bytes()
        try:
            archive = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandsoffError(f"shadow: {path.name} is not valid JSON: {exc}") from exc
        if isinstance(archive, dict):
            records += replay_records_from_archive(
                archive, source_sha256=hashlib.sha256(raw).hexdigest(), source_name=path.name)
    return records


def cohort_key(record: dict) -> str:
    return f"{record.get('repository')}|{record.get('role')}|{record.get('task_class')}"


def candidate_of(record: dict) -> tuple[str, str]:
    return str(record.get("adapter")), model_version(record)


def full_usage(record: dict) -> bool:
    """All three token counts reported as integers. A record flagged
    usage_partial or usage_not_reported never qualifies, whatever numbers it
    carries, so a missing half can never enter a mean as zero."""
    flags = set(record.get("quality_flags") or [])
    if flags & {"usage_partial", "usage_not_reported"}:
        return False
    usage = record.get("tokens") or {}
    return all(_is_int(usage.get(name)) for name in ("tokens_in", "tokens_out", "tokens_total"))


def _outcome_known(record: dict) -> bool:
    return record.get("outcome") not in (None, UNKNOWN)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _profile(value: object, where: str) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"adapter", "model"} \
            or not all(isinstance(value[name], str) and value[name] for name in ("adapter", "model")):
        raise HandsoffError(f"shadow: {where} must be an object with exactly adapter and model strings")
    return value["adapter"], value["model"]


def _cost_table(value: object) -> dict:
    if not isinstance(value, list):
        raise HandsoffError("shadow: cost table must be a list of {adapter, model, cost}")
    table = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != {"adapter", "model", "cost"}:
            raise HandsoffError(f"shadow: cost table entry {index} must have exactly adapter, model and cost")
        key = _profile({"adapter": entry["adapter"], "model": entry["model"]}, f"cost table entry {index}")
        cost = entry["cost"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0 or not math.isfinite(cost):
            raise HandsoffError(f"shadow: cost table entry {index} cost must be a finite number >= 0")
        if key in table:
            raise HandsoffError(f"shadow: cost table lists {key[0]}/{key[1]} twice")
        table[key] = float(cost)
    return table


def _baseline(value: object) -> dict:
    if not isinstance(value, dict) or not value:
        raise HandsoffError("shadow: baseline must map each role to {adapter, model}")
    return {str(role): _profile(profile, f"baseline.{role}") for role, profile in value.items()}


def _inputs(boundary, threshold, cost_table, baseline, policy_version, min_full_usage_coverage) -> dict:
    moment = _utc(boundary)
    if moment is None:
        raise HandsoffError("shadow: boundary must be an ISO-8601 time with an offset")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise HandsoffError("shadow: threshold must be a number in (0, 1]")
    if isinstance(min_full_usage_coverage, bool) or not isinstance(min_full_usage_coverage, (int, float)) \
            or not 0 <= min_full_usage_coverage <= 1:
        raise HandsoffError("shadow: min_full_usage_coverage must be a number in [0, 1]")
    if not isinstance(policy_version, str) or not POLICY_VERSION.match(policy_version):
        raise HandsoffError("shadow: policy_version must match ^[a-z0-9._-]{1,64}$")
    costs = _cost_table(cost_table)
    base = _baseline(baseline)
    return {
        "boundary": moment.isoformat(),
        "threshold": float(threshold),
        "min_full_usage_coverage": float(min_full_usage_coverage),
        "policy_version": policy_version,
        "rule": POLICY_RULE,
        "cost_table": sorted(({"adapter": a, "model": m, "cost": c} for (a, m), c in costs.items()),
                             key=lambda item: (item["adapter"], item["model"])),
        "baseline": {role: {"adapter": a, "model": m} for role, (a, m) in sorted(base.items())},
    }, moment, costs, base


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def _success_stats(records: list[dict]) -> dict:
    known = [record for record in records if _outcome_known(record)]
    successes = sum(1 for record in known if record["outcome"] == VERIFIED_OUTCOME)
    interval = wilson(successes, len(known))
    return {
        "n": len(known),
        "successes": successes,
        "success_rate": _measured(successes / len(known)) if known else _unavailable(),
        "interval": _measured([interval[0], interval[1]]) if interval else _unavailable(),
    }


def _token_means(records: list[dict]) -> dict:
    full = [record["tokens"] for record in records if full_usage(record)]
    out = {"n_known": len(full)}
    for name in ("tokens_in", "tokens_out", "tokens_total"):
        out["mean_" + name] = _measured(sum(item[name] for item in full) / len(full)) if full else _unavailable()
    return out


def _missing(records: list[dict]) -> dict:
    size = len(records)
    if not size:
        return {"outcome": _unavailable(), "full_usage": _unavailable(), "duration": _unavailable()}
    return {
        "outcome": _measured(sum(1 for r in records if not _outcome_known(r)) / size),
        "full_usage": _measured(sum(1 for r in records if not full_usage(r)) / size),
        "duration": _measured(sum(1 for r in records
                                  if not _is_int((r.get("duration") or {}).get("wall_clock_ms"))) / size),
    }


def _coverage(records: list[dict]) -> dict:
    full = sum(1 for record in records if full_usage(record))
    return {"n": len(records), "full_usage": full,
            "coverage": _measured(full / len(records)) if records else _unavailable()}


def _arm(records: list[dict]) -> dict:
    return {**_success_stats(records), "tokens": _token_means(records),
            "model_versions": sorted({model_version(r) for r in records})}


# --------------------------------------------------------------------------
# Freezing and scoring
# --------------------------------------------------------------------------

def _profile_dict(key: tuple[str, str]) -> dict:
    return {"adapter": key[0], "model": key[1]}


def freeze_policy(training: list[dict], *, threshold: float, costs: dict, baseline: dict) -> dict:
    """The frozen choice per cohort, from training records only."""
    cohorts: dict = {}
    for record in training:
        cohorts.setdefault(cohort_key(record), []).append(record)
    policy = {}
    for key in sorted(cohorts):
        records = cohorts[key]
        role = records[0].get("role")
        base = baseline[role]
        base_cost = costs.get(base)
        by_candidate: dict = {}
        for record in records:
            by_candidate.setdefault(candidate_of(record), []).append(record)
        stats = {}
        for cand in sorted(by_candidate):
            known = [r for r in by_candidate[cand] if _outcome_known(r)]
            successes = sum(1 for r in known if r["outcome"] == VERIFIED_OUTCOME)
            interval = wilson(successes, len(known))
            stats[cand] = {"n": len(known), "successes": successes,
                           "lower_bound": interval[0] if interval else None, "cost": costs.get(cand)}
        cheaper = [cand for cand, item in stats.items()
                   if cand != base and item["cost"] is not None and base_cost is not None
                   and item["cost"] < base_cost]
        eligible = [cand for cand in cheaper
                    if stats[cand]["lower_bound"] is not None and stats[cand]["lower_bound"] >= threshold]
        entry = {"role": role, "baseline": _profile_dict(base), "baseline_cost": base_cost,
                 "candidates": [{**_profile_dict(cand), **stats[cand]} for cand in sorted(stats)],
                 "gap": None}
        if eligible:
            choice = min(eligible, key=lambda cand: (stats[cand]["cost"], cand))
            entry.update(recommendation="cheaper", choice=_profile_dict(choice),
                         training=dict(stats[choice], threshold=threshold))
        elif cheaper:
            best = max(cheaper, key=lambda cand: (stats[cand]["lower_bound"] or 0.0, -stats[cand]["cost"], cand))
            bound = stats[best]["lower_bound"]
            entry.update(recommendation="refused", choice=_profile_dict(base),
                         training=dict(stats[best], threshold=threshold),
                         gap={**_profile_dict(best), "lower_bound": bound, "threshold": threshold,
                              "shortfall": threshold - (bound or 0.0),
                              "reason": (f"{best[0]}/{best[1]} training Wilson lower bound "
                                         f"{(bound or 0.0):.4f} is below the threshold {threshold:.4f}")})
        else:
            entry.update(recommendation="baseline", choice=_profile_dict(base),
                         training=dict(stats.get(base) or {"n": 0, "successes": 0, "lower_bound": None,
                                                           "cost": base_cost}, threshold=threshold))
        policy[key] = entry
    return policy


def _change(entry: dict) -> dict:
    return {"kind": "agent_profile", "role": entry["role"], "from": entry["baseline"], "to": entry["choice"]}


def finding_id(*, variant: str, cohort: str, policy_version: str, policy_sha256: str,
               recommendation: str) -> str:
    return "shf-" + _sha256({"variant": variant, "cohort_key": cohort, "policy_version": policy_version,
                             "policy_sha256": policy_sha256, "recommendation": recommendation})[:12]


def _variant(records: list[dict], name: str, *, moment: datetime, inputs: dict, inputs_sha256: str,
             costs: dict, baseline: dict) -> dict:
    training = [r for r in records if _utc(r["_available"]) < moment]
    holdout = [r for r in records if _utc(r["_available"]) >= moment]
    policy = freeze_policy([r["record"] for r in training], threshold=inputs["threshold"],
                           costs=costs, baseline=baseline)
    # Frozen: hashed before a single holdout record is read.
    policy_sha256 = _sha256({"inputs_sha256": inputs_sha256, "variant": name, "policy": policy})

    holdout_by_cohort: dict = {}
    for item in holdout:
        holdout_by_cohort.setdefault(cohort_key(item["record"]), []).append(item["record"])
    training_by_cohort: dict = {}
    for item in training:
        training_by_cohort.setdefault(cohort_key(item["record"]), []).append(item["record"])

    cohorts, findings = [], []
    for key in sorted(set(policy) | set(holdout_by_cohort)):
        held = holdout_by_cohort.get(key, [])
        trained = training_by_cohort.get(key, [])
        entry = policy.get(key)
        if entry is None:
            role = held[0].get("role")
            entry = {"role": role, "baseline": _profile_dict(baseline[role]), "recommendation": "baseline",
                     "choice": _profile_dict(baseline[role]), "gap": None, "candidates": [],
                     "training": {"n": 0, "successes": 0, "lower_bound": None, "threshold": inputs["threshold"]}}
        base = (entry["baseline"]["adapter"], entry["baseline"]["model"])
        choice = (entry["choice"]["adapter"], entry["choice"]["model"])
        everything = trained + held
        cohorts.append({
            "cohort_key": key,
            "policy_version": inputs["policy_version"],
            "routing_policy_versions": sorted({str(r.get("policy_version")) for r in everything}),
            "model_versions": sorted({model_version(r) for r in everything}),
            "n": len(everything),
            "n_training": len(trained),
            "n_holdout": len(held),
            "coverage": _coverage(everything),
            "missing_data": _missing(everything),
            "recommendation": entry["recommendation"],
            "choice": entry["choice"],
            "gap": entry["gap"],
            "training": entry["training"],
            "holdout": {
                "baseline": _arm([r for r in held if candidate_of(r) == base]),
                "policy": _arm([r for r in held if candidate_of(r) == choice]),
                "not_comparable": sum(1 for r in held if candidate_of(r) not in (base, choice)),
            },
        })
        if entry["recommendation"] in ("cheaper", "refused"):
            change = _change(entry) if entry["recommendation"] == "cheaper" else None
            findings.append({
                "finding_id": finding_id(variant=name, cohort=key, policy_version=inputs["policy_version"],
                                         policy_sha256=policy_sha256, recommendation=entry["recommendation"]),
                "variant": name,
                "cohort_key": key,
                "policy_version": inputs["policy_version"],
                "policy_sha256": policy_sha256,
                "boundary": inputs["boundary"],
                "recommendation": entry["recommendation"],
                "baseline": entry["baseline"],
                "candidate": entry["choice"] if change else {"adapter": entry["gap"]["adapter"],
                                                              "model": entry["gap"]["model"]},
                "training": entry["training"],
                "gap": entry["gap"],
                "change": change,
                "change_sha256": _sha256(change) if change else None,
            })
    return {"variant": name, "n": len(records), "n_training": len(training), "n_holdout": len(holdout),
            "policy": policy, "policy_sha256": policy_sha256, "cohorts": cohorts, "findings": findings}


def evaluate(records: list[dict], *, boundary: str, threshold: float, cost_table: list,
             baseline: dict, policy_version: str = SHADOW_POLICY_VERSION,
             min_full_usage_coverage: float = DEFAULT_MIN_FULL_USAGE_COVERAGE,
             derivation_version: int = DERIVATION_VERSION) -> dict:
    """One replay evaluation. A pure function of its arguments: no clock, no file.

    The inputs, the boundary among them, are validated and hashed before any
    record is read. Exclusions are counted, in order: another derivation
    version, a fixture, unknown outcome availability, a cohort key over
    MAX_COHORT_KEY characters, a role the baseline does not name. The scorer
    then refuses outright when full-usage coverage is below the floor.
    """
    inputs, moment, costs, base = _inputs(boundary, threshold, cost_table, baseline, policy_version,
                                          min_full_usage_coverage)
    inputs_sha256 = _sha256(inputs)

    excluded = {"derivation_mismatch": 0, "fixture": 0, "unknown_availability": 0,
                "cohort_key_too_long": 0, "no_baseline_for_role": 0}
    included = []
    ordered = sorted(records, key=lambda item: (str((item.get("evidence") or {}).get("record_hash")),
                                                str(item.get("outcome_available_at"))))
    for item in ordered:
        record = item.get("evidence") or {}
        if record.get("derivation_version") != derivation_version:
            excluded["derivation_mismatch"] += 1
        elif is_fixture(item):
            excluded["fixture"] += 1
        elif _utc(item.get("outcome_available_at")) is None:
            excluded["unknown_availability"] += 1
        elif len(cohort_key(record)) > MAX_COHORT_KEY:
            excluded["cohort_key_too_long"] += 1
        elif record.get("role") not in base:
            excluded["no_baseline_for_role"] += 1
        else:
            included.append({"record": record, "_available": item["outcome_available_at"]})

    coverage = _coverage([item["record"] for item in included])
    floor = inputs["min_full_usage_coverage"]
    value = coverage["coverage"]["value"]
    if value is None or value < floor:
        needed = max(1, math.ceil(floor * coverage["n"]) - coverage["full_usage"])
        shown = "unavailable" if value is None else f"{value:.4f}"
        raise ShadowRefusal(
            f"full-usage coverage {shown} is below the declared floor {floor:.4f}: "
            f"{coverage['full_usage']} of {coverage['n']} records report tokens_in, tokens_out and "
            f"tokens_total; {needed} more full-usage record(s) needed")

    variants = {}
    for name in VARIANTS:
        subset = included if name == "all_evidence" else [
            item for item in included if not set(item["record"].get("quality_flags") or []) & LOW_QUALITY_FLAGS]
        variants[name] = _variant(subset, name, moment=moment, inputs=inputs, inputs_sha256=inputs_sha256,
                                  costs=costs, baseline=base)
    report = {
        "schema": 1,
        "evaluator": "handsoff_shadow",
        "policy_version": policy_version,
        "derivation_version": derivation_version,
        "boundary": inputs["boundary"],
        "inputs": inputs,
        "inputs_sha256": inputs_sha256,
        "excluded": excluded,
        "included": len(included),
        "low_quality_flags": sorted(LOW_QUALITY_FLAGS),
        "full_usage_coverage": coverage,
        "missing_data": _missing([item["record"] for item in included]),
        "variants": variants,
    }
    report["report_sha256"] = _sha256(report)
    return report


# --------------------------------------------------------------------------
# Routing is never touched by evaluation or shadow mode
# --------------------------------------------------------------------------

def routing_config_sha256(root: Path) -> str:
    """The routing configuration as bytes on disk and as resolved."""
    import handsoff_lib as lib
    path = Path(root) / "handsoff.toml"
    cfg = lib.load_config(Path(root))
    body = {
        "toml_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        "agents": cfg.get("agents"), "models": cfg.get("models"), "fallbacks": cfg.get("fallbacks"),
        "adaptive_routing_profiles": cfg.get("adaptive_routing_profiles"),
        "risk_policy": cfg.get("risk_policy"), "model_policy": cfg.get("model_policy"),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def routed_choice(root: Path) -> dict:
    """What routing currently assigns each role."""
    import handsoff_lib as lib
    return lib.agent_profiles(lib.load_config(Path(root)))


def routing_unchanged(root: Path, action):
    """Run `action()` and fail if routing config or the routed choice moved."""
    before = (routing_config_sha256(root), _sha256(routed_choice(root)))
    result = action()
    after = (routing_config_sha256(root), _sha256(routed_choice(root)))
    if before != after:
        raise ShadowRefusal("shadow: routing configuration changed during a read-only evaluation "
                            f"(before {before[0][:12]}/{before[1][:12]}, after {after[0][:12]}/{after[1][:12]})")
    return result


def evaluate_project(root: Path, records: list[dict], **kwargs) -> dict:
    """`evaluate` with the baseline read from the project's routing, which
    is hashed before and after."""
    def run():
        baseline = {role: {"adapter": profile["adapter"], "model": profile["model"] or "default"}
                    for role, profile in routed_choice(root).items()}
        return evaluate(records, baseline=baseline, **kwargs)
    return routing_unchanged(root, run)


def shadow_route(root: Path, report: dict, *, role: str, repository: str, task_class: str,
                 variant: str = "all_evidence") -> dict:
    """Shadow mode: the live routed choice beside what the frozen policy
    would pick for the same cohort. Reads only; the route is unchanged."""
    def run():
        routed = routed_choice(root).get(role)
        key = f"{repository}|{role}|{task_class}"
        entry = ((report.get("variants") or {}).get(variant) or {}).get("policy", {}).get(key)
        shadow = entry["choice"] if entry else None
        return {"cohort_key": key, "variant": variant, "routed": routed, "shadow": shadow,
                "recommendation": entry["recommendation"] if entry else "baseline",
                "policy_sha256": ((report.get("variants") or {}).get(variant) or {}).get("policy_sha256"),
                "agrees": shadow is None or (routed or {}).get("adapter") == shadow["adapter"]
                          and (routed or {}).get("model") == shadow["model"]}
    return routing_unchanged(root, run)


# --------------------------------------------------------------------------
# Pilot approvals: written only by Mission Control's endpoint
# --------------------------------------------------------------------------

def approvals_dir(root: Path) -> Path:
    return Path(root) / ".handsoff" / "shadow-approvals"


def _ledger_path(root: Path) -> Path:
    return approvals_dir(root) / "ledger.jsonl"


def validate_finding(finding: object) -> dict:
    """A finding whose id and change hash recompute from its own content."""
    if not isinstance(finding, dict):
        raise ShadowRefusal("shadow finding must be an object")
    fid = finding.get("finding_id")
    if not isinstance(fid, str) or not FINDING_ID.match(fid):
        raise ShadowRefusal("shadow finding id must match ^shf-[a-z0-9]{12}$")
    if not isinstance(finding.get("policy_version"), str) or not POLICY_VERSION.match(finding["policy_version"]):
        raise ShadowRefusal("shadow finding policy_version is malformed")
    expected = finding_id(variant=str(finding.get("variant")), cohort=str(finding.get("cohort_key")),
                          policy_version=finding["policy_version"],
                          policy_sha256=str(finding.get("policy_sha256")),
                          recommendation=str(finding.get("recommendation")))
    if fid != expected:
        raise ShadowRefusal(f"shadow finding {fid} does not match its own content")
    if finding.get("recommendation") != "cheaper" or not isinstance(finding.get("change"), dict):
        raise ShadowRefusal(f"shadow finding {fid} recommends no routing change")
    if finding.get("change_sha256") != _sha256(finding["change"]):
        raise ShadowRefusal(f"shadow finding {fid}: change_sha256 does not match the proposed change")
    return finding


def validate_approval(record: object) -> dict:
    if not isinstance(record, dict) or set(record) != set(APPROVAL_FIELDS):
        raise ShadowRefusal("shadow approval must carry exactly " + ", ".join(APPROVAL_FIELDS))
    checks = (("approval_id", APPROVAL_ID), ("finding_id", FINDING_ID), ("change_sha256", SHA256),
              ("policy_version", POLICY_VERSION))
    for name, pattern in checks:
        if not isinstance(record[name], str) or not pattern.match(record[name]):
            raise ShadowRefusal(f"shadow approval {name} is malformed")
    if _utc(record["created_at"]) is None:
        raise ShadowRefusal("shadow approval created_at must be ISO-8601 UTC")
    if record["source"] != APPROVAL_SOURCE:
        raise ShadowRefusal("shadow approval source must be mission_control")
    return record


def _ledger(root: Path) -> list[dict]:
    """The approval ledger, chain verified; a broken chain refuses."""
    path = _ledger_path(root)
    if not path.is_file():
        return []
    entries, prev = [], "GENESIS"
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ShadowRefusal(f"shadow approval ledger line {number} is not JSON") from exc
        body = {k: v for k, v in entry.items() if k != "hash"} if isinstance(entry, dict) else {}
        if body.get("prev_hash") != prev or entry.get("hash") != _sha256(body):
            raise ShadowRefusal(f"shadow approval ledger chain is broken at line {number}")
        prev = entry["hash"]
        entries.append(entry)
    return entries


def _ledger_append(root: Path, kind: str, **fields) -> dict:
    entries = _ledger(root)
    body = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind,
            "prev_hash": entries[-1]["hash"] if entries else "GENESIS", **fields}
    body["hash"] = _sha256(body)
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(body) + "\n")
    return body


def record_approval(root: Path, finding: dict) -> dict:
    """Write one Pilot approval. Called only by Mission Control's
    /api/shadow-approval endpoint after it has checked the Pilot token."""
    finding = validate_finding(finding)
    root = Path(root)
    with project_lock(root):
        directory = approvals_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        if len(list(directory.glob("apr-*.json"))) >= MAX_APPROVALS:
            raise ShadowRefusal(f"shadow approvals: at most {MAX_APPROVALS} are kept; archive some first")
        record = {
            "approval_id": "apr-" + secrets.token_hex(6),
            "finding_id": finding["finding_id"],
            "change_sha256": finding["change_sha256"],
            "policy_version": finding["policy_version"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": APPROVAL_SOURCE,
        }
        validate_approval(record)
        _atomic_write_text(directory / f"{record['approval_id']}.json",
                           json.dumps(record, indent=2, sort_keys=True) + "\n")
        _ledger_append(root, "recorded", approval_id=record["approval_id"], approval_sha256=_sha256(record))
    return record


def _report_finding(report: dict, fid: str) -> dict:
    for variant in (report.get("variants") or {}).values():
        for finding in variant.get("findings") or []:
            if finding.get("finding_id") == fid:
                return finding
    raise ShadowRefusal(f"shadow finding {fid} is not in this report")


def apply_finding(root: Path, report: dict, *, finding: str, approval: str) -> dict:
    """Apply one recommendation to routing config. Refuses unless an approval
    written by Mission Control exists for exactly this finding and change,
    and has not been used before."""
    import handsoff_lib as lib
    root = Path(root)
    if not isinstance(approval, str) or not APPROVAL_ID.match(approval):
        raise ShadowRefusal("shadow apply: an approval id from Mission Control (apr-...) is required")
    target = validate_finding(_report_finding(report, finding))
    path = approvals_dir(root) / f"{approval}.json"
    if not path.is_file():
        raise ShadowRefusal(f"shadow apply: no Mission Control approval {approval} exists")
    record = validate_approval(load_unique_json(path))
    if record["approval_id"] != approval:
        raise ShadowRefusal(f"shadow apply: approval file {approval} names another approval")
    entries = _ledger(root)
    if not any(e.get("kind") == "recorded" and e.get("approval_id") == approval
               and e.get("approval_sha256") == _sha256(record) for e in entries):
        raise ShadowRefusal(f"shadow apply: approval {approval} was not recorded by Mission Control")
    if any(e.get("kind") == "consumed" and e.get("approval_id") == approval for e in entries):
        raise ShadowRefusal(f"shadow apply: approval {approval} was already used")
    if record["finding_id"] != target["finding_id"]:
        raise ShadowRefusal(f"shadow apply: approval {approval} is bound to finding {record['finding_id']}, "
                            f"not {target['finding_id']}")
    if record["change_sha256"] != target["change_sha256"]:
        raise ShadowRefusal(f"shadow apply: approval {approval} is bound to a different change")
    if record["policy_version"] != target["policy_version"]:
        raise ShadowRefusal(f"shadow apply: approval {approval} is bound to another policy version")

    change = target["change"]
    cfg = lib.load_config(root)
    current = lib.agent_profiles(cfg).get(change["role"])
    if current != change["from"]:
        raise ShadowRefusal(f"shadow apply: {change['role']} is routed to {current}, not the "
                            f"{change['from']} this finding was computed against")
    assignments = {role: {"adapter": cfg["agents"][role], "model": cfg["models"][role]}
                   for role in lib.agent_profiles(cfg)}
    assignments[change["role"]] = dict(change["to"])
    with project_lock(root):
        if any(e.get("kind") == "consumed" and e.get("approval_id") == approval for e in _ledger(root)):
            raise ShadowRefusal(f"shadow apply: approval {approval} was already used")
        _ledger_append(root, "consumed", approval_id=approval, finding_id=target["finding_id"],
                       change_sha256=target["change_sha256"])
    lib.update_agent_config(root, assignments)
    return {"approval_id": approval, "finding_id": target["finding_id"], "change": change,
            "routing_config_sha256": routing_config_sha256(root)}


def main(argv: list[str] | None = None) -> int:
    """Read-only: evaluate the archives in a directory and print the report."""
    parser = argparse.ArgumentParser(prog="handsoff_shadow", description=main.__doc__)
    parser.add_argument("--root", default=".", type=Path)
    parser.add_argument("--archives", required=True, type=Path)
    parser.add_argument("--boundary", required=True, help="ISO-8601 with offset, fixed before scoring")
    parser.add_argument("--threshold", required=True, type=float)
    parser.add_argument("--costs", required=True, type=Path, help="JSON list of {adapter, model, cost}")
    parser.add_argument("--min-full-usage-coverage", type=float, default=DEFAULT_MIN_FULL_USAGE_COVERAGE)
    args = parser.parse_args(argv)
    try:
        report = evaluate_project(args.root.resolve(), load_replay_records(args.archives),
                                  boundary=args.boundary, threshold=args.threshold,
                                  cost_table=load_unique_json(args.costs),
                                  min_full_usage_coverage=args.min_full_usage_coverage)
    except HandsoffError as exc:
        print(f"handsoff_shadow: {exc}", file=sys.stderr)
        return 2
    print(_canonical(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
