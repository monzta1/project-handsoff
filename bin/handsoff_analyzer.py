#!/usr/bin/env python3
"""Project Handsoff archive analyzer (#49).

Mines the completed-run archives (`handsoff_lib.archive_run` writes one
JSON record per run) for recurring, evidenced patterns and files them as
improvement tickets in house style. Stdlib only.

Everything a draft or report may contain comes from a fixed allowlist of
ledger facts: the archive file name as run id, the repo name, the first
120 characters of the feature title, event kinds, timestamps, numeric
counters, boolean outcomes, and the text of `pilot_note` events. No other
status, acceptance, verification, or event field is ever read into a fact,
so no prompt, output, or environment content can reach a filed issue.

Rules have fixed ids. R1 to R7 draft issues; R8 and R9 (GATE_WEAKENING_RULES)
only ever produce report entries because their natural remedy is loosening
a gate, and nothing in configuration lifts that.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402

FEATURE_TITLE_CHARS = 120
ISSUE_LABELS = ("from-archive-analysis", "needs-triage")
MARKER_PREFIX = "handsoff-analysis rule:"
TITLE_TOKEN_OVERLAP = 0.6
GH_LIST_LIMIT = 500
GH_TIMEOUT_SECONDS = 60

DESIGN_REVIEW_ATTEMPT_KINDS = frozenset({"design_review_approved", "design_review_changes_requested"})
CRITERIA_MUTATION_KINDS = frozenset({
    "criterion_added", "criterion_updated", "criterion_removed", "criteria_transaction_applied",
})

# Weight decides filing priority (highest first) and report order. The
# Pilot's own notes outrank every inferred pattern.
RULE_WEIGHTS = {
    "R7": 100, "R6": 90, "R3": 80, "R1": 70, "R2": 60, "R5": 50, "R4": 40, "R8": 30, "R9": 30,
}
RULE_TITLES = {
    "R1": "Design-review budget exhausted across product runs",
    "R2": "Acceptance criteria mutated after design approval",
    "R3": "Recovery escalated on a run with no managed agent session",
    "R4": "Verification cache reuse stays low across product runs",
    "R5": "Median design-phase wall clock exceeds the configured threshold",
    "R6": "Live verification failed on a completed run",
    "R7": "Pilot note",
    "R8": "Pilot authorizations past the design-review budget recur",
    "R9": "Review cap overrides recur",
}
# Framework rules diagnose the Handsoff engine itself. Pilot notes are
# intentionally product-owned: they describe the active project's work.
# Unknown future rules fail closed as ambiguous and are report-only.
# Pilot notes are the operator's own words; they belong in the local
# analysis report, never on a tracker, so R7 is report-only by rule.
REPORT_ONLY_RULES = frozenset({"R7"})
RULE_OWNERS = {
    "R1": "framework", "R2": "framework", "R3": "framework",
    "R4": "framework", "R5": "framework", "R6": "framework",
    "R7": "product", "R8": "framework", "R9": "framework",
}
FINDING_OWNERS = frozenset({"framework", "product", "ambiguous"})


class GateWeakening(NamedTuple):
    """A pattern whose natural remedy is loosening a gate. Reported, never
    filed: a ticket that says "raise the cap" is exactly the kind of
    ticket an automated analyzer must not be allowed to open."""
    rule: str
    title: str
    natural_remedy: str


GATE_WEAKENING_RULES = {
    "R8": GateWeakening("R8", RULE_TITLES["R8"], "raising max_autonomous_design_reviews"),
    "R9": GateWeakening("R9", RULE_TITLES["R9"], "raising max_review_rounds"),
}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def normalize_timestamp(value: object) -> str | None:
    """UTC ISO-8601 or None when the value is missing or unparsable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse(value: object) -> datetime | None:
    normalized = normalize_timestamp(value)
    return datetime.fromisoformat(normalized) if normalized else None


def classify_archive(record: dict, file_name: str) -> str:
    """"test" or "product". An explicit run_kind wins; without one, the
    repo name (or the file name when repo is missing) decides by prefix."""
    kind = record.get("run_kind") if isinstance(record, dict) else None
    if kind in lib.RUN_KINDS:
        return kind
    repo = record.get("repo") if isinstance(record, dict) else None
    name = repo if isinstance(repo, str) and repo else file_name
    if any(name.startswith(prefix) for prefix in lib.FIXTURE_ROOT_PREFIXES):
        return "test"
    return "product"


def load_archives(directory: Path) -> dict:
    """Every *.json under `directory`. Returns readable (deduplicated
    product archives as {run_id, record}), unreadable (file names),
    skipped_fixtures (file names), and duplicates (file names dropped by
    the repo + started_at dedupe). Nothing is ever deleted or rewritten."""
    directory = Path(directory)
    readable, unreadable, skipped, duplicates = [], [], [], []
    candidates: dict[tuple, list[tuple[str, dict]]] = {}
    paths = sorted(directory.glob("*.json"), key=lambda p: p.name) if directory.is_dir() else []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            unreadable.append(path.name)
            continue
        if not isinstance(record, dict):
            unreadable.append(path.name)
            continue
        if classify_archive(record, path.name) == "test":
            skipped.append(path.name)
            continue
        key = (record.get("repo"), normalize_timestamp(record.get("started_at")))
        candidates.setdefault(key, []).append((path.name, record))
    for key, entries in candidates.items():
        if len(entries) == 1:
            readable.append({"run_id": entries[0][0], "record": entries[0][1]})
            continue
        # Newest archived_at wins; an exact tie keeps the file name that
        # sorts last, so the choice is deterministic across scans.
        ranked = sorted(entries, key=lambda item: (normalize_timestamp(item[1].get("archived_at")) or "", item[0]))
        keep = ranked[-1]
        readable.append({"run_id": keep[0], "record": keep[1]})
        duplicates.extend(name for name, _ in ranked[:-1])
    readable.sort(key=lambda item: item["run_id"])
    return {
        "readable": readable, "unreadable": sorted(unreadable),
        "skipped_fixtures": sorted(skipped), "duplicates": sorted(duplicates),
    }


# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------

def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def run_facts(record: dict) -> dict:
    """The allowlisted facts of one archive. Only ledger records are read:
    status.status for the outcome, event kinds, timestamps, and the numeric
    counters named here. Nothing else on an event is looked at."""
    status = record.get("status") if isinstance(record.get("status"), dict) else {}
    events = [e for e in (record.get("events") or []) if isinstance(e, dict)]
    feature = record.get("feature")
    facts = {
        "repo": record.get("repo") if isinstance(record.get("repo"), str) else None,
        "root": record.get("root") if isinstance(record.get("root"), str) else None,
        "feature": feature[:FEATURE_TITLE_CHARS] if isinstance(feature, str) else None,
        "run_kind": classify_archive(record, ""),
        "started_at": normalize_timestamp(record.get("started_at")),
        "archived_at": normalize_timestamp(record.get("archived_at")),
        "outcome": status.get("status") if isinstance(status.get("status"), str) else None,
        "event_count": len(events),
        "design_phase_hours": None,
        "design_review_attempts": 0,
        "design_review_budget_exhausted": 0,
        "design_review_attempt_authorized": 0,
        "criteria_mutations_after_approval": 0,
        "recovery_escalated": 0,
        "agent_sessions": False,
        "question_raised": 0,
        "pilot_notes": [],
        "checks_run_events": 0,
        "checks_run_measured": 0,
        "checks_launched": 0,
        "checks_reused": 0,
        "cache_observation_source": "events",
        "cache_reuse_opportunities": 0,
        "cache_reuse_hits": 0,
        "cache_reuse_misses": 0,
        "cache_first_executions": 0,
        "cache_binding_changes": 0,
        "cache_ineligible_failed": 0,
        "cache_ineligible_timed_out": 0,
        "cache_ineligible_truncated": 0,
        "verify_live_failures": 0,
        "review_cap_overrides": 0,
    }
    design_start = design_end = initialized = None
    last_approval_index = None
    for index, event in enumerate(events):
        kind = event.get("kind")
        if not isinstance(kind, str):
            continue
        if kind == "initialized" and initialized is None:
            initialized = event.get("at")
        elif kind == "phase_advanced":
            phase = event.get("phase_number")
            if phase == 2 and design_start is None:
                design_start = event.get("at")
            elif phase == 3 and design_end is None:
                design_end = event.get("at")
        elif kind in DESIGN_REVIEW_ATTEMPT_KINDS:
            facts["design_review_attempts"] += 1
        elif kind == "design_review_budget_exhausted":
            facts["design_review_budget_exhausted"] += 1
        elif kind == "design_review_attempt_authorized":
            facts["design_review_attempt_authorized"] += 1
        elif kind == "design_approved":
            last_approval_index = index
        elif kind == "recovery_escalated":
            facts["recovery_escalated"] += 1
        elif kind.startswith("agent_session_"):
            facts["agent_sessions"] = True
        elif kind == "question_raised":
            facts["question_raised"] += 1
        elif kind == "pilot_note":
            text = event.get("text")
            if isinstance(text, str) and text.strip():
                facts["pilot_notes"].append(" ".join(text.split())[:lib.MAX_PILOT_NOTE_LENGTH])
        elif kind == "checks_run":
            facts["checks_run_events"] += 1
            # Only events written since the verification cache (#43) carry
            # the counters; older ones must not read as "zero reuse".
            if "launched_count" in event and "reused_count" in event:
                facts["checks_run_measured"] += 1
                facts["checks_launched"] += _count(event.get("launched_count"))
                facts["checks_reused"] += _count(event.get("reused_count"))
        elif kind == "live_checks_run":
            if event.get("ok") is False:
                facts["verify_live_failures"] += 1
        elif kind == "review_cap_override_recorded":
            facts["review_cap_overrides"] += 1
    if last_approval_index is not None:
        facts["criteria_mutations_after_approval"] = sum(
            1 for event in events[last_approval_index + 1:] if event.get("kind") in CRITERIA_MUTATION_KINDS
        )
    start = _parse(design_start if design_start is not None else initialized)
    end = _parse(design_end)
    if start is not None and end is not None:
        facts["design_phase_hours"] = round((end - start).total_seconds() / 3600, 3)
    diagnostics = verification_cache_diagnostics(record.get("verifications"))
    if diagnostics is not None:
        facts.update(diagnostics)
    return facts


def verification_cache_diagnostics(value: object) -> dict | None:
    """Classify structural cache opportunities without reading check output.

    Archive records can contain one verification row per criterion even when
    one command was launched once. Rows carrying the same binding and durable
    result fingerprint are therefore collapsed into one execution before the
    sequence is measured. Legacy archives without cache-era records return
    None so their event counters retain the old fallback behavior.
    """
    if not isinstance(value, list):
        return None
    observations: list[dict] = []
    seen_rows: set[tuple] = set()
    for record in value:
        if not isinstance(record, dict) or record.get("kind") != "checks" \
                or not isinstance(record.get("binding"), dict):
            continue
        results = record.get("results") if isinstance(record.get("results"), list) else []
        for result in results:
            if not isinstance(result, dict):
                continue
            command = result.get("command")
            binding = record["binding"].get(command) if isinstance(command, str) else None
            if not isinstance(binding, str):
                continue
            at_bucket = str(record.get("at") or "").split(".", 1)[0]
            fingerprint = (
                record.get("feature_hash"), command, binding, result.get("output_sha256"),
                result.get("exit_code"), result.get("timed_out"), result.get("truncated"),
                result.get("duration_s"), record.get("reused_from"), at_bucket,
            )
            if fingerprint in seen_rows:
                continue
            seen_rows.add(fingerprint)
            observations.append({
                "command": command, "binding": binding,
                "reused": record.get("executed") is False and isinstance(record.get("reused_from"), str),
                "ok": record.get("ok") is True and result.get("exit_code") == 0,
                "timed_out": result.get("timed_out") is True,
                "truncated": result.get("truncated") is True,
            })
    if not observations:
        return None
    counts = {
        "cache_observation_source": "verification_ledger",
        "cache_reuse_opportunities": 0, "cache_reuse_hits": 0, "cache_reuse_misses": 0,
        "cache_first_executions": 0, "cache_binding_changes": 0,
        "cache_ineligible_failed": 0, "cache_ineligible_timed_out": 0,
        "cache_ineligible_truncated": 0,
    }
    previous: dict[str, dict] = {}
    for current in observations:
        prior = previous.get(current["command"])
        if prior is None:
            counts["cache_first_executions"] += 1
        elif prior["binding"] != current["binding"]:
            counts["cache_binding_changes"] += 1
        elif prior["timed_out"]:
            counts["cache_ineligible_timed_out"] += 1
        elif prior["truncated"]:
            counts["cache_ineligible_truncated"] += 1
        elif not prior["ok"]:
            counts["cache_ineligible_failed"] += 1
        else:
            counts["cache_reuse_opportunities"] += 1
            if current["reused"]:
                counts["cache_reuse_hits"] += 1
            else:
                counts["cache_reuse_misses"] += 1
        previous[current["command"]] = current
    return counts


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def _finding(rule: str, run_ids: list[str], numbers: dict, *, text: str | None = None) -> dict:
    finding = {
        "rule": rule,
        "weight": RULE_WEIGHTS[rule],
        "title": RULE_TITLES[rule],
        "run_ids": sorted(run_ids),
        "numbers": numbers,
        "excluded": rule in GATE_WEAKENING_RULES,
        "marker": rule,
    }
    if text is not None:
        finding["text"] = text
        finding["title"] = f"Pilot note: {text[:80]}"
        finding["marker"] = f"{rule}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
    return finding


def evaluate_rules(facts_by_run: dict, cfg: dict) -> list[dict]:
    """Ordered findings over the product runs' facts. Findings are ordered
    by weight descending, then rule id, then sorted run ids (then note text
    for R7); a finding never carries an empty run-id list."""
    analysis = cfg.get("analysis") or lib.DEFAULT_CONFIG["analysis"]
    threshold = float(analysis.get("design_phase_hours_threshold", 1.0))
    findings: list[dict] = []
    runs = sorted(facts_by_run)

    def with_count(field: str, minimum_runs: int, rule: str) -> None:
        hits = [run for run in runs if facts_by_run[run].get(field, 0) > 0]
        if len(hits) >= minimum_runs:
            findings.append(_finding(rule, hits, {
                "runs": len(hits), field: sum(facts_by_run[run][field] for run in hits),
            }))

    with_count("design_review_budget_exhausted", 2, "R1")
    with_count("criteria_mutations_after_approval", 2, "R2")

    r3 = [run for run in runs if facts_by_run[run].get("recovery_escalated", 0) > 0
          and not facts_by_run[run].get("agent_sessions")]
    if r3:
        findings.append(_finding("R3", r3, {
            "runs": len(r3), "recovery_escalated": sum(facts_by_run[run]["recovery_escalated"] for run in r3),
            "agent_session_events": 0,
        }))

    ledger_runs = [run for run in runs
                   if facts_by_run[run].get("cache_observation_source") == "verification_ledger"]
    if ledger_runs:
        r4 = [run for run in ledger_runs if facts_by_run[run].get("cache_reuse_opportunities", 0) > 0]
        launched = sum(facts_by_run[run].get("cache_reuse_misses", 0) for run in r4)
        reused = sum(facts_by_run[run].get("cache_reuse_hits", 0) for run in r4)
    else:
        r4 = [run for run in runs if facts_by_run[run].get("checks_run_measured", 0) >= 2]
        launched = sum(facts_by_run[run].get("checks_launched", 0) for run in r4)
        reused = sum(facts_by_run[run].get("checks_reused", 0) for run in r4)
    if len(r4) >= 3 and launched + reused > 0:
        ratio = reused / (launched + reused)
        if ratio < 0.20:
            findings.append(_finding("R4", r4, {
                "runs": len(r4), "launched": launched, "reused": reused,
                "reuse_percent": round(ratio * 100, 1),
            }))

    measured = [run for run in runs if isinstance(facts_by_run[run].get("design_phase_hours"), (int, float))]
    if len(measured) >= 3:
        median = statistics.median(facts_by_run[run]["design_phase_hours"] for run in measured)
        if median > threshold:
            findings.append(_finding("R5", measured, {
                "runs": len(measured), "median_hours": round(median, 2), "threshold_hours": threshold,
            }))

    r6 = [run for run in runs if facts_by_run[run].get("verify_live_failures", 0) > 0]
    if r6:
        findings.append(_finding("R6", r6, {
            "runs": len(r6), "verify_live_failures": sum(facts_by_run[run]["verify_live_failures"] for run in r6),
        }))

    notes: dict[str, list[str]] = {}
    for run in runs:
        for text in facts_by_run[run].get("pilot_notes") or []:
            if run not in notes.setdefault(text, []):
                notes[text].append(run)
    for text in sorted(notes):
        findings.append(_finding("R7", notes[text], {"runs": len(notes[text])}, text=text))

    with_count("design_review_attempt_authorized", 2, "R8")
    with_count("review_cap_overrides", 2, "R9")

    findings = [f for f in findings if f["run_ids"]]
    findings.sort(key=lambda f: (-f["weight"], f["rule"], f["run_ids"], f.get("text", "")))
    return findings


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------

_DRAFT_TEXT = {
    "R1": {
        "symptom": "The autonomous design-review budget ran out in {runs} product runs "
                   "({design_review_budget_exhausted} design_review_budget_exhausted events in total).",
        "hypothesis": "Hypothesis: the Architect's first design or the reviewer's findings are not converging "
                      "within the budget, so runs stall on a Pilot authorization instead of finishing design.",
        "benefit": "Fewer runs blocked on a human authorization that only exists because the design loop "
                   "did not converge on its own.",
        "required": "Investigate why the design loop fails to converge within max_autonomous_design_reviews "
                    "on these runs and fix the cause (prompt, packet, or reviewer profile), without raising "
                    "the budget.",
        "acceptance": [
            "The cause is named with evidence from the listed run ids.",
            "A targeted test reproduces the non-converging loop and passes after the fix.",
            "max_autonomous_design_reviews is unchanged.",
        ],
    },
    "R2": {
        "symptom": "Acceptance criteria were mutated after the last design approval in {runs} product runs "
                   "({criteria_mutations_after_approval} mutation events after approval in total).",
        "hypothesis": "Hypothesis: designs are approved before their criteria are testable, so the "
                      "Implementer reshapes them after the fact.",
        "benefit": "An approved design that stays approved: fewer invalidated approvals and re-reviews.",
        "required": "Find what the approved criteria were missing on these runs and move that check "
                    "ahead of design approval.",
        "acceptance": [
            "The mutations on the listed runs are categorised (requirement, tests, verification kind).",
            "The design gate, the Architect prompt, or the amendment lane is adjusted so the same "
            "category is caught before approval.",
            "A targeted test covers the new pre-approval check.",
        ],
    },
    "R3": {
        "symptom": "Recovery escalated on {runs} run(s) that recorded no agent_session_* event at all "
                   "({recovery_escalated} recovery_escalated events).",
        "hypothesis": "Hypothesis: recovery is treating a run that never launched a managed agent as a "
                      "silent worker and exhausting its attempts against nothing.",
        "benefit": "Recovery attempts spent only on runs that actually had a managed session to recover.",
        "required": "Recovery must distinguish a run with no managed session from a lost worker and "
                    "must not escalate the former.",
        "acceptance": [
            "A run with zero agent_session_* events never produces recovery_escalated.",
            "A run with a lost managed session still escalates as before.",
            "Both cases are covered by targeted tests.",
        ],
    },
    "R4": {
        "symptom": "Across {runs} product runs with at least two checks_run events, verify reused "
                   "{reused} of {launched_plus_reused} check executions ({reuse_percent} percent, "
                   "launched {launched}).",
        "hypothesis": "Hypothesis: something in the verification binding changes between verify calls "
                      "(a generated file inside the digest, a criterion spec edit, a config change), so "
                      "eligible records are never reused.",
        "benefit": "Shorter Phase 6 wall clock with the same evidence.",
        "required": "Identify what invalidates the binding on these runs and either exclude it from the "
                    "digest (when it is Handsoff-generated) or surface it in the verify output.",
        "acceptance": [
            "The invalidation source on the listed runs is named with evidence.",
            "Repeated verify calls on an unchanged tree reuse the earlier record.",
            "Reuse never applies to a failed, live, or regression record.",
        ],
    },
    "R5": {
        "symptom": "The median measured design phase across {runs} product runs was {median_hours} hours, "
                   "above the {threshold_hours} hour threshold.",
        "hypothesis": "Hypothesis: design rounds wait on human approval or on slow reviewers longer than "
                      "the design work itself takes.",
        "benefit": "Faster path from init to an approved design.",
        "required": "Break the design-phase time on these runs down by category (active, background "
                    "wait, human wait) using the archived events and remove the dominant wait.",
        "acceptance": [
            "The dominant category on the listed runs is named with numbers.",
            "A change targeting that category is shipped with a targeted test.",
            "No gate is loosened to achieve it.",
        ],
    },
    "R6": {
        "symptom": "Live verification failed on {runs} completed run(s) ({verify_live_failures} failed "
                   "live_checks_run events).",
        "hypothesis": "Hypothesis: the live checks depend on timing or on state that deployment did not "
                      "settle before verify-live ran.",
        "benefit": "A live failure that means the deployment is wrong, not that the check was early.",
        "required": "Reproduce the failing live check on the listed runs and fix the check, the "
                    "deployment step, or the ordering between them.",
        "acceptance": [
            "The failing live command and its cause are named.",
            "verify-live passes on the first attempt after deployment approval on a fixture.",
            "require_live_verification is unchanged.",
        ],
    },
    "R7": {
        "symptom": "The Pilot recorded this note on {runs} run(s): \"{text}\"",
        "hypothesis": "Hypothesis: the note describes friction the Pilot hit during the run; the run ids "
                      "carry the ledger context.",
        "benefit": "The Pilot's own observation becomes a tracked ticket instead of a lost remark.",
        "required": "Read the note, inspect the listed runs, and turn the observation into a concrete "
                    "behaviour change or close with a reason.",
        "acceptance": [
            "The note is restated as a required behaviour with a test, or closed with a written reason.",
        ],
    },
}


def _marker_line(marker: str) -> str:
    return f"<!-- {MARKER_PREFIX}{marker} -->"


def draft_issue(finding: dict) -> dict:
    """An issue in house style from one finding: title, Symptom (with run
    ids and numbers), Cause hypothesis, Benefit, Required behavior,
    Acceptance criteria, and the hidden marker line."""
    rule = finding["rule"]
    if rule in GATE_WEAKENING_RULES:
        raise lib.HandsoffError(f"{rule} is a gate-weakening pattern and is never drafted")
    text = _DRAFT_TEXT[rule]
    numbers = dict(finding.get("numbers") or {})
    numbers["launched_plus_reused"] = numbers.get("launched", 0) + numbers.get("reused", 0)
    numbers["text"] = finding.get("text", "")
    run_lines = "\n".join(f"- `{run_id}`" for run_id in finding["run_ids"])
    number_lines = "\n".join(
        f"- {key}: {value}" for key, value in sorted((finding.get("numbers") or {}).items())
    )
    body = "\n".join([
        "## Symptom",
        "",
        text["symptom"].format(**numbers),
        "",
        "Evidence (archive run ids):",
        "",
        run_lines,
        "",
        "Numbers:",
        "",
        number_lines,
        "",
        "## Cause hypothesis",
        "",
        text["hypothesis"],
        "",
        "## Benefit",
        "",
        text["benefit"],
        "",
        "## Required behavior",
        "",
        text["required"],
        "",
        "## Acceptance criteria",
        "",
        "\n".join(f"{index}. {line}" for index, line in enumerate(text["acceptance"], start=1)),
        "",
        f"Filed by the Handsoff archive analyzer (rule {rule}).",
        "",
        _marker_line(finding["marker"]),
        "",
    ])
    return {
        "rule": rule, "marker": finding["marker"], "weight": finding["weight"],
        "title": finding["title"], "body": body, "labels": list(ISSUE_LABELS),
        "run_ids": list(finding["run_ids"]), "owner": RULE_OWNERS.get(rule, "ambiguous"),
    }


# --------------------------------------------------------------------------
# dedupe and filing
# --------------------------------------------------------------------------

def _title_tokens(title: object) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", str(title or "").lower()) if token}


def _title_overlap(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _issue_is_candidate(issue: dict, dedupe_days: int, now: datetime) -> bool:
    state = str(issue.get("state") or "").lower()
    if state == "open":
        return True
    closed_at = _parse(issue.get("closed_at"))
    if closed_at is None:
        return False
    return now - closed_at <= timedelta(days=dedupe_days)


def dedupe(drafts: list[dict], existing_issues: list[dict], dedupe_days: int, *,
           now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Split drafts into (to_file, suppressed). A draft is suppressed when
    an open issue, or one closed within dedupe_days, carries the same rule
    marker or a normalized title sharing at least 60 percent of tokens."""
    now = now or datetime.now(timezone.utc)
    candidates = [issue for issue in existing_issues if isinstance(issue, dict)
                  and _issue_is_candidate(issue, dedupe_days, now)]
    keep, suppressed = [], []
    for draft in drafts:
        marker = f"{MARKER_PREFIX}{draft['marker']}"
        tokens = _title_tokens(draft["title"])
        match = None
        for issue in candidates:
            if marker in str(issue.get("body") or ""):
                match = {"reason": "marker", "issue": issue.get("number"), "issue_title": issue.get("title")}
                break
            if _title_overlap(tokens, _title_tokens(issue.get("title"))) >= TITLE_TOKEN_OVERLAP:
                match = {"reason": "title", "issue": issue.get("number"), "issue_title": issue.get("title")}
                break
        if match is None:
            keep.append(draft)
        else:
            suppressed.append({"rule": draft["rule"], "title": draft["title"], "run_ids": draft["run_ids"], **match})
    return keep, suppressed


class GhClient:
    """The default GitHub client: wraps the gh CLI. `available()` is False
    when no gh executable is on PATH, in which case nothing is filed."""

    def __init__(self, root: Path, *, repo: str | None = None,
                 runner=subprocess.run, which=shutil.which):
        self.root = Path(root)
        self.repo = repo
        self._runner = runner
        self._which = which

    def available(self) -> bool:
        return self._which("gh") is not None

    def _run(self, args: list[str]) -> str:
        if self.repo:
            args = [*args, "--repo", self.repo]
        result = self._runner(["gh", *args], cwd=str(self.root), shell=False, text=True,
                              capture_output=True, timeout=GH_TIMEOUT_SECONDS, check=False)
        if result.returncode != 0:
            raise lib.HandsoffError(f"gh {args[0]} {args[1]} failed: {result.stderr.strip()[:200]}")
        return result.stdout

    def list_issues(self) -> list[dict]:
        raw = json.loads(self._run([
            "issue", "list", "--state", "all", "--limit", str(GH_LIST_LIMIT),
            "--json", "number,title,body,state,closedAt,labels",
        ]) or "[]")
        issues = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            issues.append({
                "number": item.get("number"), "title": item.get("title"), "body": item.get("body"),
                "state": item.get("state"), "closed_at": item.get("closedAt"),
                "labels": [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)],
            })
        return issues

    LABEL_DESCRIPTIONS = {
        "from-archive-analysis": "Filed by the Handsoff archive analyzer from evidenced run patterns",
        "needs-triage": "Awaiting planning: decide whether and when to do it",
    }

    def ensure_labels(self, labels: list[str]) -> None:
        """A repository that has never been scanned lacks the two labels;
        `gh issue create --label` then fails outright. Create them first
        (idempotent: --force updates an existing label in place)."""
        for label in labels:
            self._run(["label", "create", label, "--force", "--color", "5319e7",
                       "--description", self.LABEL_DESCRIPTIONS.get(label, "Handsoff archive analysis")])

    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        self.ensure_labels(labels)
        args = ["issue", "create", "--title", title, "--body", body]
        for label in labels:
            args.extend(["--label", label])
        return self._run(args).strip()


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def analysis_dir(root: Path) -> Path:
    return Path(root) / lib.ANALYSIS_DIR


def _effective_archive_dir(cfg: dict, override: Path | str | None) -> Path:
    if override:
        return Path(override).expanduser()
    configured = (cfg.get("analysis") or {}).get("archive_dir")
    if configured:
        return Path(configured).expanduser()
    return lib.archive_dir()


def scan(root: Path, cfg: dict, *, archive_directory: Path | str | None = None, client=None,
         dry_run: bool = False, now: datetime | None = None) -> dict:
    """One full pass: load, fact, evaluate, draft, dedupe, file (subject to
    the cap, dry-run, filing mode and gh availability), then write the
    report to .handsoff-analysis/<timestamp>.json and return it."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    analysis = cfg.get("analysis") or dict(lib.DEFAULT_CONFIG["analysis"])
    directory = _effective_archive_dir(cfg, archive_directory)
    loaded = load_archives(directory)
    facts_by_run = {item["run_id"]: run_facts(item["record"]) for item in loaded["readable"]}
    findings = evaluate_rules(facts_by_run, cfg)
    excluded = [f for f in findings if f["excluded"]]
    report_only = [f for f in findings if not f["excluded"] and f["rule"] in REPORT_ONLY_RULES]
    drafts = [draft_issue(f) for f in findings
              if not f["excluded"] and f["rule"] not in REPORT_ONLY_RULES and f["run_ids"]]
    ambiguous = [d for d in drafts if d["owner"] == "ambiguous"]
    # A product-owned finding is filed on the active project's repository,
    # so it must come from this project's runs only: the archive holds
    # every project's runs, and another project's finding filed from here
    # would land on the wrong tracker.
    this_root = str(root.resolve())
    foreign = [d for d in drafts if d["owner"] == "product"
               and any((facts_by_run.get(run) or {}).get("root") != this_root for run in d["run_ids"])]
    filable_drafts = [d for d in drafts if d["owner"] != "ambiguous" and d not in foreign]

    filing = {"mode": None, "note": None, "cap": analysis["max_tickets_per_scan"]}
    filed, suppressed, not_filed = [], [], []
    to_file = list(filable_drafts)
    not_filed.extend({"rule": f["rule"], "title": f["title"], "run_ids": f["run_ids"],
                      "reason": "report_only_rule", "owner": RULE_OWNERS.get(f["rule"], "ambiguous")}
                     for f in report_only)
    not_filed.extend({"rule": d["rule"], "title": d["title"], "run_ids": d["run_ids"],
                      "reason": "ambiguous_owner", "owner": d["owner"]} for d in ambiguous)
    not_filed.extend({"rule": d["rule"], "title": d["title"], "run_ids": d["run_ids"],
                      "reason": "foreign_root", "owner": d["owner"],
                      "destination": "active_product_repository"} for d in foreign)
    if dry_run:
        filing["mode"] = "dry_run"
        filing["note"] = "dry run: nothing was filed and no GitHub client was consulted"
    elif analysis.get("filing") == "report_only":
        filing["mode"] = "report_only"
        filing["note"] = "analysis.filing is report_only: nothing was filed and no GitHub client was consulted"
    elif not filable_drafts:
        filing["mode"] = "nothing_to_file"
        filing["note"] = "no filable findings"
    else:
        filing["mode"] = "gh"
        groups = {"framework": [], "product": []}
        for draft in filable_drafts:
            groups[draft["owner"]].append(draft)
        cap = analysis["max_tickets_per_scan"]
        remaining = cap
        missing = False
        # A single injected client preserves the original test/integration API.
        shared_client = client if client is not None and not isinstance(client, dict) else None
        for owner in ("product", "framework"):
            group = groups[owner]
            if not group:
                continue
            gh = shared_client or ((client or {}).get(owner) if isinstance(client, dict) else None)
            destination = "active_product_repository" if owner == "product" else analysis["framework_repo"]
            gh = gh or GhClient(root, repo=None if owner == "product" else analysis["framework_repo"])
            if not gh.available():
                missing = True
                not_filed.extend({"rule": d["rule"], "title": d["title"], "run_ids": d["run_ids"],
                                  "reason": "gh_missing", "owner": owner,
                                  "destination": destination} for d in group)
                continue
            candidates, group_suppressed = dedupe(group, gh.list_issues(), analysis["dedupe_days"], now=now)
            suppressed.extend({**item, "owner": owner, "destination": destination}
                              for item in group_suppressed)
            candidates.sort(key=lambda d: -d["weight"])
            for draft in candidates[:remaining]:
                url = gh.create_issue(draft["title"], draft["body"], draft["labels"])
                filed.append({"rule": draft["rule"], "title": draft["title"], "url": url,
                              "run_ids": draft["run_ids"], "owner": owner,
                              "destination": destination})
            not_filed.extend({"rule": d["rule"], "title": d["title"], "run_ids": d["run_ids"],
                              "reason": "cap", "owner": owner, "destination": destination}
                             for d in candidates[remaining:])
            remaining = max(0, remaining - len(candidates[:remaining]))
        if missing and not filed:
            filing["mode"] = "gh_missing"
            filing["note"] = "gh executable not found for any destination: nothing was filed"
        else:
            filing["note"] = f"filed {len(filed)} destination-routed drafts (cap {cap})"

    report = {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "archive_dir": str(directory),
        "config": {
            "max_tickets_per_scan": analysis["max_tickets_per_scan"],
            "dedupe_days": analysis["dedupe_days"],
            "design_phase_hours_threshold": analysis["design_phase_hours_threshold"],
            "framework_repo": analysis["framework_repo"],
        },
        "runs": {
            "product": [item["run_id"] for item in loaded["readable"]],
            "skipped_fixtures": loaded["skipped_fixtures"],
            "unreadable": loaded["unreadable"],
            "duplicates": loaded["duplicates"],
        },
        "facts": facts_by_run,
        "findings": findings,
        "excluded": excluded,
        "drafts": drafts,
        "filing": filing,
        "filed": filed,
        "suppressed": suppressed,
        "not_filed": not_filed,
    }
    out_dir = analysis_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{now.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def scan_event_fields(report: dict) -> dict:
    """The counters `archive_scan_completed` records on the live ledger."""
    return {
        "findings": len(report["findings"]),
        "filed": len(report["filed"]),
        "suppressed": len(report["suppressed"]),
        "excluded": len(report["excluded"]),
        "skipped_fixtures": len(report["runs"]["skipped_fixtures"]),
        "unreadable": len(report["runs"]["unreadable"]),
        "report_path": report["report_path"],
    }
