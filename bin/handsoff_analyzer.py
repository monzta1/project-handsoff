"""Report-only, lane-aware checks for completed Handsoff archives."""

import json
from pathlib import Path


def scan(archive_dir: Path) -> list[dict]:
    """Return findings for applicable lane rules; never mutate an archive."""
    findings = []
    for path in sorted(Path(archive_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("run_kind") == "test":
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
        if lane == "design" and ("implementation" in rule or "review" in rule or "r4" in rule):
            continue
        if lane == "review" and ("design" in rule or "r1" in rule or "r2" in rule or "r3" in rule):
            continue
        result.append(finding)
    if lane == "review" and 6 in phases and not status.get("review"):
        result.append({"rule": "R10", "title": "Review lane has no review record",
                       "text": "review-lane archive has no review record", "run_ids": [],
                       "numbers": [], "excluded": []})
    return result
