#!/usr/bin/env python3
"""Routing cohorts: what the #300 evidence says about one candidate.

#301, the second step of the #299 epic. A cohort is every observed session that
shares a key: (repository, task_class, phase, adapter, model, policy_version).
This module answers a cohort query with metrics and with how much each metric
can be trusted. It changes no gate and writes nothing.

**Pure.** `aggregate` reads no clock and no file. `now`, the derivation version
and the cohort config are explicit arguments, the inputs are sorted by record
hash before anything is counted, and the result is serialised by
`canonical_json`, so identical inputs give byte-identical output in any record
order. Only `main` touches the filesystem, and only to read archives.

**Every metric carries its own evidence.** `n_known` counts the records whose
value for that metric is known, `missing_rate` is `1 - n_known / size`, and
`min_sample` applies per metric to `n_known`. A cohort can therefore report a
success rate while its token mean stays null: real archives mostly lack the
input/output split, and a mean over the few that have it is not a mean anyone
should route on.

**A model upgrade never borrows evidence.** A record's model version is the
reported model when known, else the requested model. Records are split by
version after every widening step and sufficiency is judged per version, so
twelve sessions on one version and twelve on another never make twenty.

**Widening is one fixed order** (`WIDENING_ORDER`): policy_version, then phase,
then task_class, then adapter. Repository and model are never widened. Each
step is recorded with its reason: `never_populated` when no retained record
carries the key's value for that dimension, `sparse` when some do but too few.
The UNKNOWN task class is its own bucket in both directions: an UNKNOWN key
never matches a known class, a known key never matches UNKNOWN, and widening
task_class for an UNKNOWN key would widen nothing, so it is not recorded.

Layer: core -> config -> evidence -> here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from handsoff_core import HandsoffError, _canonical
from handsoff_config import classify_archive_record
from handsoff_evidence import DERIVATION_VERSION, UNKNOWN, project_archive

#: The cohort key, in the order every result reports it.
KEY_FIELDS = ("repository", "task_class", "phase", "adapter", "model", "policy_version")

#: The one widening order. Least informative dimension first: a policy bump
#: rarely changes how a model performs, an adapter change always might.
WIDENING_ORDER = ("policy_version", "phase", "task_class", "adapter")

#: Defaults; a caller's config may override any of these and nothing else.
DEFAULT_CONFIG = {
    "min_sample": 20,
    "staleness_days": 30,
    "retention_per_repository": 2000,
    "retention_fleet": 20000,
}

#: The outcome a success is counted from: the only one that means a result was
#: proven where users meet it.
VERIFIED_OUTCOME = "verified_phase8"

METRICS = ("success_rate", "median_wall_seconds", "mean_total_tokens")


def _utc(value: object) -> datetime | None:
    """An aware datetime in UTC, or None. An offset-less time cannot be placed
    on the UTC line, so it counts as absent rather than being guessed."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def observed_at(observation: dict) -> datetime | None:
    """ended_at, else started_at, in UTC; None when neither is usable."""
    return _utc(observation.get("ended_at")) or _utc(observation.get("started_at"))


def model_version(record: dict) -> str:
    """The reported model when known, else the requested model.

    The projection writes the literal UNKNOWN for an absent report, so that
    literal is treated exactly like a missing field.
    """
    reported = record.get("reported_model")
    if isinstance(reported, str) and reported and reported != UNKNOWN:
        return reported
    requested = record.get("requested_model")
    return requested if isinstance(requested, str) and requested else UNKNOWN


def is_fixture(observation: dict) -> bool:
    """The #317 rule, `classify_archive_record`, applied to the observation's
    archive kind when it was recorded and to its repository and source name
    otherwise."""
    record = observation.get("evidence") or {}
    stand_in = {"repo": record.get("repository")}
    if observation.get("run_kind") is not None:
        stand_in["run_kind"] = observation.get("run_kind")
    return classify_archive_record(stand_in, str(record.get("source_archive") or "")) == "test"


def observations_from_archive(archive: dict, *, source_sha256: str, source_name: str) -> list[dict]:
    """The #300 records of one archive, each wrapped with what a cohort needs
    and the projection does not carry: the session's timestamps and the
    archive's kind. The record itself is untouched, so its hash still verifies.
    """
    records = project_archive(archive, source_sha256=source_sha256, source_name=source_name)
    status = archive.get("status") if isinstance(archive.get("status"), dict) else {}
    sessions = status.get("agent_sessions") if isinstance(status.get("agent_sessions"), dict) else {}
    kind = classify_archive_record(archive, source_name)
    observations = []
    for record in records:
        session = sessions.get(record["session_id"]) or {}
        observations.append({"evidence": record, "run_kind": kind,
                             "started_at": session.get("started_at"),
                             "ended_at": session.get("ended_at")})
    return observations


def _config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    unknown = sorted(set(cfg or {}) - set(DEFAULT_CONFIG))
    if unknown:
        raise HandsoffError(f"cohort config: unknown setting(s) {', '.join(unknown)}")
    merged.update(cfg or {})
    for name, value in merged.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HandsoffError(f"cohort config: {name} must be a positive integer")
    return merged


def _key(key: dict) -> dict:
    if not isinstance(key, dict) or set(key) != set(KEY_FIELDS):
        raise HandsoffError(f"cohort key: exactly {', '.join(KEY_FIELDS)} are required")
    return {name: key[name] for name in KEY_FIELDS}


def _matches(record: dict, key: dict, widened: set) -> bool:
    if record.get("repository") != key["repository"]:
        return False
    if key["model"] not in (record.get("requested_model"), model_version(record)):
        return False
    if "task_class" in widened:
        if (record.get("task_class") == UNKNOWN) != (key["task_class"] == UNKNOWN):
            return False
    elif record.get("task_class") != key["task_class"]:
        return False
    return all(record.get(name) == key[name]
               for name in ("phase", "adapter", "policy_version") if name not in widened)


def _metric(values: list, size: int, min_sample: int, reduce) -> dict:
    n_known = len(values)
    entry = {"value": None, "n_known": n_known,
             "missing_rate": 1.0 - n_known / size if size else 1.0,
             "reason": None, "shortfall": 0}
    if n_known == 0:
        entry.update(reason="no_observations", shortfall=min_sample)
    elif n_known < min_sample:
        entry.update(reason="below_min_sample", shortfall=min_sample - n_known)
    else:
        entry["value"] = reduce(values)
    return entry


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def metrics(group: list[tuple], min_sample: int) -> dict:
    """The three metrics over one version's records, each with its evidence."""
    records = [record for record, _ in group]
    size = len(records)
    outcomes = [record.get("outcome") for record in records if record.get("outcome") not in (None, UNKNOWN)]
    walls = [(record.get("duration") or {}).get("wall_clock_ms") for record in records]
    walls = [value / 1000 for value in walls if _is_int(value)]
    tokens = []
    for record in records:
        usage = record.get("tokens") or {}
        if all(_is_int(usage.get(name)) for name in ("tokens_in", "tokens_out", "tokens_total")):
            tokens.append(usage["tokens_total"])
    return {
        "success_rate": _metric(outcomes, size, min_sample,
                                lambda vals: sum(1 for v in vals if v == VERIFIED_OUTCOME) / len(vals)),
        "median_wall_seconds": _metric(walls, size, min_sample, lambda vals: float(statistics.median(vals))),
        "mean_total_tokens": _metric(tokens, size, min_sample, lambda vals: sum(vals) / len(vals)),
    }


def _full_inputs(record: dict) -> bool:
    usage = record.get("tokens") or {}
    return (record.get("outcome") not in (None, UNKNOWN)
            and _is_int((record.get("duration") or {}).get("wall_clock_ms"))
            and all(_is_int(usage.get(name)) for name in ("tokens_in", "tokens_out", "tokens_total")))


def _retain(population: list[tuple], cfg: dict) -> tuple[list[tuple], dict]:
    """Newest first per repository, then across the Fleet; the rest pruned and counted."""
    def newest_first(items):
        return sorted(items, key=lambda item: (item[1], item[0]["record_hash"]), reverse=True)

    by_repository: dict = {}
    for item in population:
        by_repository.setdefault(item[0].get("repository"), []).append(item)
    kept, pruned_repository = [], 0
    for repository in sorted(by_repository, key=str):
        ordered = newest_first(by_repository[repository])
        kept += ordered[:cfg["retention_per_repository"]]
        pruned_repository += len(ordered[cfg["retention_per_repository"]:])
    ordered = newest_first(kept)
    kept = ordered[:cfg["retention_fleet"]]
    return kept, {"repository": pruned_repository, "fleet": len(ordered) - len(kept)}


def _version_cohort(version: str, group: list[tuple], key: dict, population: list[tuple],
                    now: datetime, cfg: dict) -> dict:
    times = [moment for _, moment in group]
    size = len(group)
    selected = [moment for record, moment in population
                if record.get("adapter") == key["adapter"] and model_version(record) == version]
    last_selected = max(selected) if selected else None
    stale = last_selected is None or now - last_selected > timedelta(days=cfg["staleness_days"])
    result = {
        "key": dict(key, model=version),
        "size": size,
        "coverage": sum(1 for record, _ in group if _full_inputs(record)) / size if size else 0.0,
        "age": {"newest_s": int((now - max(times)).total_seconds()) if times else None,
                "oldest_s": int((now - min(times)).total_seconds()) if times else None},
        "metrics": metrics(group, cfg["min_sample"]),
        "stale_or_biased": stale,
        "last_selected_at": _iso(last_selected) if last_selected else None,
    }
    result["sufficient"] = result["metrics"]["success_rate"]["value"] is not None
    return result


def _split(matched: list[tuple]) -> dict:
    versions: dict = {}
    for item in matched:
        versions.setdefault(model_version(item[0]), []).append(item)
    return versions


def aggregate(observations: list[dict], derivation_version: int, now, cfg: dict | None,
              key: dict) -> dict:
    """One cohort query. A pure function of its arguments.

    Exclusions happen in this order and are each counted: a record projected
    under another derivation version, a fixture record, a record with no usable
    observed_at, a record observed after `now`. Retention then prunes oldest
    first. The key is matched, then widened one dimension at a time in
    `WIDENING_ORDER` until some model version's success rate meets
    `min_sample` on its own, or nothing is left to widen.
    """
    moment = _utc(now)
    if moment is None:
        raise HandsoffError("cohort: now must be an ISO-8601 time with an offset")
    cfg = _config(cfg)
    key = _key(key)

    excluded = {"derivation_mismatch": 0, "fixture": 0, "no_observed_at": 0, "observed_after_now": 0}
    population = []
    ordered = sorted(observations, key=lambda obs: tuple(
        str(value) for value in ((obs.get("evidence") or {}).get("record_hash"), obs.get("ended_at"),
                                 obs.get("started_at"), obs.get("run_kind"))))
    for observation in ordered:
        record = observation.get("evidence") or {}
        if record.get("derivation_version") != derivation_version:
            excluded["derivation_mismatch"] += 1
            continue
        if is_fixture(observation):
            excluded["fixture"] += 1
            continue
        when = observed_at(observation)
        if when is None:
            excluded["no_observed_at"] += 1
            continue
        if when > moment:
            excluded["observed_after_now"] += 1
            continue
        population.append((record, when))
    population, pruned = _retain(population, cfg)
    population.sort(key=lambda item: (item[0]["record_hash"], item[1]))

    widened: list[dict] = []
    widened_names: set = set()
    matched = [item for item in population if _matches(item[0], key, widened_names)]
    versions = _split(matched)
    for dimension in WIDENING_ORDER:
        if any(_metric_sufficient(group, cfg) for group in versions.values()):
            break
        if dimension == "task_class" and key["task_class"] == UNKNOWN:
            continue
        populated = any(record.get(dimension) == key[dimension] for record, _ in population)
        widened.append({"dimension": dimension, "reason": "sparse" if populated else "never_populated"})
        widened_names.add(dimension)
        matched = [item for item in population if _matches(item[0], key, widened_names)]
        versions = _split(matched)

    cohorts = [_version_cohort(version, versions[version], key, population, moment, cfg)
               for version in sorted(versions)]
    return {
        "key": key,
        "derivation_version": derivation_version,
        "now": _iso(moment),
        "config": cfg,
        "sources": sorted({str(record.get("source_sha256")) for record, _ in matched}),
        "excluded": excluded,
        "pruned": pruned,
        "widened": widened,
        "sufficient": any(cohort["sufficient"] for cohort in cohorts),
        "cohorts": cohorts,
    }


def _metric_sufficient(group: list[tuple], cfg: dict) -> bool:
    known = sum(1 for record, _ in group if record.get("outcome") not in (None, UNKNOWN))
    return known >= cfg["min_sample"]


def canonical_json(result: dict) -> str:
    """The one serialisation, so equal results are equal bytes."""
    return _canonical(result)


def load_observations(directory: Path) -> list[dict]:
    """Every archive in a directory as observations. Fixture archives are kept
    so `aggregate` excludes and counts them rather than this loader hiding them."""
    observations = []
    for path in sorted(Path(directory).glob("*.json")):
        raw = path.read_bytes()
        try:
            archive = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandsoffError(f"cohort: {path.name} is not valid JSON: {exc}") from exc
        if isinstance(archive, dict):
            observations += observations_from_archive(
                archive, source_sha256=hashlib.sha256(raw).hexdigest(), source_name=path.name)
    return observations


def main(argv: list[str] | None = None) -> int:
    """Read-only: print one cohort query over an archive directory."""
    parser = argparse.ArgumentParser(prog="handsoff_cohorts", description=main.__doc__)
    parser.add_argument("--archives", required=True, type=Path)
    for name in KEY_FIELDS:
        parser.add_argument("--" + name.replace("_", "-"), required=True, dest=name)
    parser.add_argument("--now", help="ISO-8601 with offset; defaults to the current time")
    parser.add_argument("--min-sample", type=int, default=DEFAULT_CONFIG["min_sample"])
    parser.add_argument("--staleness-days", type=int, default=DEFAULT_CONFIG["staleness_days"])
    args = parser.parse_args(argv)
    key = {name: getattr(args, name) for name in KEY_FIELDS}
    if key["phase"].isdigit():
        key["phase"] = int(key["phase"])
    now = args.now or datetime.now(timezone.utc).isoformat()
    try:
        result = aggregate(load_observations(args.archives), DERIVATION_VERSION, now,
                           {"min_sample": args.min_sample, "staleness_days": args.staleness_days}, key)
    except HandsoffError as exc:
        print(f"handsoff_cohorts: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
