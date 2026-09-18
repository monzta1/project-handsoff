#!/usr/bin/env python3
"""Print the evidence summary for one or more Handsoff runs, straight from
the ledgers, so a field proof (#108) never needs hand-transcribed numbers.

    python3 tools/run_evidence.py /path/to/project [/path/to/.handsoff-archive/run ...]

Each argument is a directory holding handsoff-status.json,
handsoff-events.jsonl and handsoff-verifications.jsonl (a project root or an
archived run). Output is one Markdown table row per run in the format used
on issue #75, followed by the intervention breakdown.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PILOT_GATES = {
    "design_approved", "deployment_approved", "design_review_attempt_authorized",
    "review_cap_override_recorded", "regression_decided", "amendment_approved",
    "recovery_acknowledged", "run_reopened",
}
INTERVENTIONS = {
    "agent_replacement_pilot_pause", "recovery_escalated", "design_review_attempt_authorized",
    "review_cap_override_recorded", "run_closed", "human_pause_started", "recovery_acknowledged",
    "session_result_adopted", "deployment_revoked", "protocol_silence_enforced",
}


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _hours(start: str | None, end: str | None) -> str:
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return f"{delta.total_seconds() / 3600:.1f} h"
    except (TypeError, ValueError):
        return "n/a"


def summarize(root: Path) -> dict:
    status = json.loads((root / "handsoff-status.json").read_text(encoding="utf-8"))
    events = _jsonl(root / "handsoff-events.jsonl")
    verifications = _jsonl(root / "handsoff-verifications.jsonl")
    sessions = status.get("agent_sessions") or {}
    kinds = [e.get("kind") for e in events]
    first = events[0].get("at") if events else status.get("created_at")
    last = events[-1].get("at") if events else status.get("updated_at")
    outcome = "closed" if isinstance(status.get("run_closed"), dict) else (
        "complete" if status.get("status") == "complete" or int(status.get("phase_number") or 0) >= 8
        else status.get("status") or "unknown")
    failed = sum(1 for s in sessions.values() if s.get("state") in {"failed", "timed_out", "failed_to_start"})
    replaced = len(status.get("agent_replacements") or [])
    design_reviews = kinds.count("design_review_approved") + kinds.count("design_review_changes_requested")
    impl_reviews = len(status.get("review_attempts") or [])
    checks = [v for v in verifications if v.get("kind") == "checks"]
    executed = sum(1 for v in checks if v.get("executed") is not False)
    reused = sum(1 for v in checks if v.get("executed") is False)
    live = sum(1 for v in verifications if v.get("kind") == "live")
    gates = sum(1 for k in kinds if k in PILOT_GATES)
    interventions: dict[str, int] = {}
    for k in kinds:
        if k in INTERVENTIONS:
            interventions[k] = interventions.get(k, 0) + 1
    items = [w.get("id") for w in (json.loads((root / "handsoff-acceptance.json").read_text(encoding="utf-8")).get("work_items") or [])] \
        if (root / "handsoff-acceptance.json").is_file() else []
    return {
        "run": ", ".join(i.replace("issue-", "#") for i in items) or (status.get("feature") or root.name)[:40],
        "elapsed": _hours(first, last), "outcome": outcome,
        "sessions": f"{len(sessions)} ({failed} / {replaced})",
        "design_reviews": design_reviews, "impl_reviews": impl_reviews,
        "checks": f"{executed} / {reused}", "live": live, "gates": gates,
        "interventions": ", ".join(f"{k} x{v}" for k, v in sorted(interventions.items())) or "none",
        "engine": " / ".join(sorted({str(e.get("engine", {}).get("version")) for e in events
                                    if isinstance(e.get("engine"), dict) and e["engine"].get("version")}))
                  or (status.get("engine") or {}).get("version") or "not ledgered",
    }


def main(argv: list[str]) -> int:
    roots = [Path(a).expanduser().resolve() for a in argv[1:]] or [Path.cwd()]
    rows = [summarize(r) for r in roots]
    print("| Run | Engine | Elapsed | Outcome | Sessions (failed / replaced) | Design reviews | Impl. reviews | Checks executed / reused | Live runs | Pilot gates | Interventions |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['run']} | {r['engine']} | {r['elapsed']} | {r['outcome']} | {r['sessions']} | {r['design_reviews']} | "
              f"{r['impl_reviews']} | {r['checks']} | {r['live']} | {r['gates']} | {r['interventions']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
