#!/usr/bin/env python3
"""Deterministic backlog proposals for Handsoff issue tranches (#50)."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import handsoff_lib as lib

PROPOSAL_FILE = ".handsoff-tranche-proposal.json"
GOVERNANCE_LABELS = {"governance", "process", "policy", "handsoff-governance"}
DEPENDENCY_RE = re.compile(r"(?i)\b(?:depends\s+on|blocked\s+by|after)\b([^\n.;]*)")
ISSUE_RE = re.compile(r"#([1-9][0-9]{0,8})\b")
FILE_RE = re.compile(r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)+|[\w.-]+\.(?:py|js|ts|toml|json|md|yml|yaml|sh))\b")
GATE_RE = re.compile(r"(?i)\b(design gate|review gate|deployment gate|live gate|regression gate)\b")
ARCHIVE_RE = re.compile(r"(?i)(?:\.handsoff-archive/|archive(?:[_ -]?id)?\s*[:#]\s*)([A-Za-z0-9_.-]+)")


def _normalize_label(value: object) -> str:
    text = str(value.get("name") if isinstance(value, dict) else value).strip().casefold()
    text = re.sub(r"^(?:type|kind|area)\s*:\s*", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _labels(issue: dict) -> list[str]:
    values = issue.get("labels") or []
    return sorted({_normalize_label(value)[:64] for value in values if _normalize_label(value)})[:32]


def dependencies(issue: dict) -> list[int]:
    body = str(issue.get("body") or "")
    return sorted({int(number) for match in DEPENDENCY_RE.finditer(body)
                   for number in ISSUE_RE.findall(match.group(1))})


def lane_view(issue: dict, cfg: dict) -> dict:
    facts = issue.get("handsoff_facts")
    required = ("criteria", "primary_fixes", "changed_lines", "changed_files", "touched_paths")
    missing = [name for name in required if not isinstance(facts, dict) or name not in facts]
    if isinstance(facts, dict):
        missing.extend(name for name in required[:4]
                       if name in facts and (not isinstance(facts[name], int) or isinstance(facts[name], bool)))
        if "touched_paths" in facts and (not isinstance(facts["touched_paths"], list)
                                          or not all(isinstance(path, str) for path in facts["touched_paths"])):
            missing.append("touched_paths")
    missing = sorted(set(missing))
    rationale = []
    bounded_facts = None
    if isinstance(facts, dict):
        bounded_facts = {name: facts[name] for name in required[:4]
                         if isinstance(facts.get(name), int) and not isinstance(facts.get(name), bool)}
        if isinstance(facts.get("touched_paths"), list):
            bounded_facts["touched_paths"] = [path[:240] for path in facts["touched_paths"][:64]
                                               if isinstance(path, str)]
    if missing:
        rationale.extend(f"{name}=missing" for name in missing)
        return {"lane": "full", "facts": bounded_facts, "rationale": rationale}
    facts = bounded_facts
    governance = [path for path in facts["touched_paths"]
                  if path == "handsoff.toml" or path.startswith(("schemas/", "prompts/"))
                  or path.startswith("bin/handsoff_")]
    rationale.extend([
        f"criteria={facts['criteria']}/{cfg['small_fix_max_criteria']}",
        f"primary_fixes={facts['primary_fixes']}/1",
        f"changed_lines={facts['changed_lines']}/{cfg['small_fix_max_changed_lines']}",
        f"changed_files={facts['changed_files']}/{cfg['small_fix_max_files']}",
        "touched_paths=" + (", ".join(facts["touched_paths"]) or "none"),
    ])
    eligible = (facts["criteria"] <= cfg["small_fix_max_criteria"]
                and facts["primary_fixes"] == 1
                and facts["changed_lines"] <= cfg["small_fix_max_changed_lines"]
                and facts["changed_files"] <= cfg["small_fix_max_files"]
                and not governance)
    return {"lane": "small-fix" if eligible else "full", "facts": facts, "rationale": rationale}


def _archive_cost(issue: dict, archives: list[tuple[str, dict]], repo: str, lane: str) -> tuple[dict | None, str]:
    labels = set(_labels(issue)) - GOVERNANCE_LABELS
    comparable = []
    for archive_id, archive in archives:
        if str(archive.get("repo") or "").casefold().split("/")[-1] != repo.casefold().split("/")[-1]:
            continue
        status = archive.get("status") or {}
        deliveries = status.get("work_item_delivery") or {}
        lanes = {record.get("lane") for record in deliveries.values() if isinstance(record, dict)} or {"full"}
        archived = list(archive.get("labels", []))
        approved_labels = ((status.get("tranche_approval") or {}).get("labels") or {})
        for values in approved_labels.values():
            if isinstance(values, list):
                archived.extend(values)
        archive_labels = {_normalize_label(x) for x in archived} - GOVERNANCE_LABELS
        if lane in lanes and labels & archive_labels:
            comparable.append((archive_id, status))
    if not comparable:
        return None, "unknown"
    per_role = {}
    for role in lib.SELECTABLE_AGENT_ROLES:
        counts = [sum(1 for session in (status.get("agent_sessions") or {}).values()
                      if isinstance(session, dict) and session.get("role") == role
                      and session.get("running_at") is not None) for _, status in comparable]
        per_role[role] = statistics.median(counts)
    return {"median_launched_sessions": per_role,
            "archive_ids": sorted(archive_id[:240] for archive_id, _ in comparable)}, "archive"


def load_archives(directory: Path) -> list[tuple[str, dict]]:
    records = []
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and (value.get("status") or {}).get("status") == "complete":
            records.append((path.name, value))
    return records


def _score(issue: dict, unlocks: int, lane: str) -> tuple[int, dict]:
    labels = set(_labels(issue))
    body = str(issue.get("body") or "")
    parts = {
        "bug": 40 if "bug" in labels else 0,
        "enhancement": 10 if "enhancement" in labels else 0,
        "archive_evidence": min(20, 5 * len(set(ARCHIVE_RE.findall(body)))),
        "named_files_or_gates": min(18, 3 * len(set(FILE_RE.findall(body)) | set(GATE_RE.findall(body)))),
        "dependent_unlocks": min(20, 10 * unlocks),
        "small_fix_fit": 10 if lane == "small-fix" else 0,
    }
    return sum(parts.values()), parts


def build_proposal(issues: list[dict], archives: list[tuple[str, dict]], repo: str, cfg: dict,
                   *, limit: int = 5, now: str | None = None) -> dict:
    by_number = {int(issue["number"]): issue for issue in issues}
    open_issues = {number: issue for number, issue in by_number.items()
                   if str(issue.get("state", "OPEN")).casefold() == "open"}
    deps = {number: dependencies(issue) for number, issue in open_issues.items()}
    open_deps = {number: {dep for dep in values if dep in open_issues} for number, values in deps.items()}
    missing = {number: sorted(dep for dep in values if dep not in by_number) for number, values in deps.items()}
    unlocks = {number: sum(number in values for values in open_deps.values()) for number in open_issues}
    entries = {}
    for number, issue in open_issues.items():
        lane = lane_view(issue, cfg)
        score, score_inputs = _score(issue, unlocks[number], lane["lane"])
        cost, source = _archive_cost(issue, archives, repo, lane["lane"])
        entries[number] = {"number": number, "title": str(issue.get("title") or f"Issue #{number}")[:200],
                           "url": str(issue.get("url") or "")[:512], "labels": _labels(issue),
                           "dependencies": deps[number], "missing_dependencies": missing[number],
                           "score": score, "score_inputs": score_inputs, **lane,
                           "cost_shape": cost, "cost_source": source, "blockers": []}
    ordered, ready, indegree = [], [], {n: len(open_deps[n]) for n in open_issues}
    ready = [n for n, degree in indegree.items() if degree == 0 and not missing[n]]
    while ready:
        ready.sort(key=lambda n: (-entries[n]["score"], n))
        number = ready.pop(0)
        ordered.append(number)
        for dependent in sorted(open_issues):
            if number in open_deps[dependent]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0 and not missing[dependent]:
                    ready.append(dependent)
    def is_cycle_member(start: int, current: int, seen: set[int]) -> bool:
        for dependency in open_deps.get(current, set()):
            if dependency == start:
                return True
            if dependency not in seen and is_cycle_member(start, dependency, seen | {dependency}):
                return True
        return False

    cycle = sorted(n for n in open_issues if not missing[n] and is_cycle_member(n, n, {n}))
    for number in entries:
        if missing[number]:
            entries[number]["blockers"].append("missing_dependency")
        if number in cycle:
            entries[number]["blockers"].append("dependency_cycle")
    remainder = sorted((n for n in open_issues if n not in ordered), key=lambda n: (-entries[n]["score"], n))
    proposed = (ordered + remainder)[:max(1, min(25, limit))]
    proposal = {"version": 1, "repo": repo, "generated_at": now or datetime.now(timezone.utc).isoformat(),
                "proposed_order": [f"issue-{number}" for number in proposed],
                "issues": [entries[number] for number in proposed]}
    proposal["proposal_hash"] = proposal_hash(proposal)
    return proposal


def proposal_hash(proposal: dict) -> str:
    content = {key: value for key, value in proposal.items() if key != "proposal_hash"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fetch_issues(repo: str) -> list[dict]:
    result = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "100",
                             "--json", "number,title,body,labels,state,url"], text=True,
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise lib.HandsoffError("GitHub issue inventory unavailable")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise lib.HandsoffError("GitHub issue inventory is invalid")
    return value
