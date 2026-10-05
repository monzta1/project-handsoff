"""Report-only, lane-aware checks for completed Handsoff archives."""

import json
import re
from pathlib import Path

import handsoff_lib as lib


def scan(archive_dir: Path) -> list[dict]:
    """Return findings for applicable lane rules; never mutate an archive."""
    findings = []
    for path in sorted(Path(archive_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The isinstance guard is load-bearing and separate from the
        # classification: classify_archive_record tolerates a non-dict
        # record, so without it a list-shaped archive with a product
        # name reaches record.get("status") below and raises.
        if not isinstance(record, dict) or lib.classify_archive_record(record, path.name) == "test":
            continue
        status = record.get("status") if isinstance(record.get("status"), dict) else {}
        lane = status.get("lane")
        phases = set(status.get("phases_run") or [])
        if lane == "review" and 6 in phases and not status.get("review"):
            findings.append({
                "rule": "R10", "title": "Review lane has no review record",
                "text": "review-lane archive has no review record", "run_ids": [path.name],
                "numbers": [], "excluded": [],
            })
    return findings


def applicable_findings(record: dict, findings: list[dict]) -> list[dict]:
    """Apply lane scope without turning report-only rules into passes."""
    status = record.get("status") if isinstance(record.get("status"), dict) else {}
    lane = status.get("lane")
    phases = set(status.get("phases_run") or [])
    result = []
    for finding in findings:
        rule = str(finding.get("rule", "")).lower()
        # Whole rule ids, not substrings: "r1" in "r10" dropped the review
        # lane's own R10 finding and its run ids.
        rule_id = re.match(r"r\d+", rule)
        rule_id = rule_id.group(0) if rule_id else ""
        if lane == "design" and ("implementation" in rule or "review" in rule or rule_id == "r4"):
            continue
        if lane == "review" and ("design" in rule or rule_id in {"r1", "r2", "r3"}):
            continue
        result.append(finding)
    if lane == "review" and 6 in phases and not status.get("review") \
            and not any(str(item.get("rule", "")).upper() == "R10" for item in result):
        result.append({"rule": "R10", "title": "Review lane has no review record",
                       "text": "review-lane archive has no review record", "run_ids": [],
                       "numbers": [], "excluded": []})
    return result
