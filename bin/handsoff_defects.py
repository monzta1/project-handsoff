#!/usr/bin/env python3
"""Escaped defects: a tracked ledger whose open entries a later run inherits.

A defect that escaped a shipped run is recorded with the control that
should have caught it and the regression that would prove it fixed. Every
later run whose criteria may touch the same paths inherits it as a
regression: Phase 3 refuses until the run either adopts it (a criterion
whose requirement cites the defect id) or declines it for this run only.
A decline never closes the defect, so the next overlapping run inherits it
again. Closing needs a run that adopted it to have reached Phase 8.

The ledger is `handsoff-defects.jsonl` at the project root. It is tracked
repository content, but excluded from the evidence digest by name, so
recording a defect never stales evidence. It is append-only: a defect is
one `record` line, and each decline or close is one `decision` line that
names it. `load_defects` folds them back into one dict per defect.

Overlap is deliberately conservative. Two globs overlap unless their
literal prefixes (each glob up to its first wildcard) prove them disjoint,
so a defect is never silently dropped because two patterns were spelled
differently.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import HandsoffError, project_lock
from handsoff_config import load_config

DEFECTS_FILE = "handsoff-defects.jsonl"

CONTROLS = (
    "requirement", "implementation", "test_selection", "environment",
    "reviewer_visibility", "live_verification", "deployment", "handsoff_integrity",
)

DECISION_ACTIONS = ("decline", "close")

RECORD_FIELDS = ("issue", "control", "summary", "regression", "paths", "by")

MAX_TEXT_CHARS = 2000
MAX_DEFECT_PATHS = 32
MAX_PATH_CHARS = 512

_WILDCARDS = re.compile(r"[*?\[]")


def defects_path(root: Path) -> Path:
    return Path(root) / DEFECTS_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(fields: dict, key: str, *, required: bool = True) -> str:
    value = fields.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise HandsoffError(f"defect: {key} must be a non-empty string")
    clean = value.strip()
    if len(clean) > MAX_TEXT_CHARS:
        raise HandsoffError(f"defect: {key} is longer than {MAX_TEXT_CHARS} characters")
    return clean


def _normal_glob(glob: str) -> str:
    clean = glob.strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean


def _paths(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HandsoffError("defect: paths must be a list of globs")
    if len(value) > MAX_DEFECT_PATHS:
        raise HandsoffError(f"defect: at most {MAX_DEFECT_PATHS} paths")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HandsoffError("defect: each path must be a non-empty glob")
        if len(item) > MAX_PATH_CHARS:
            raise HandsoffError(f"defect: a path is longer than {MAX_PATH_CHARS} characters")
        clean = _normal_glob(item)
        if clean not in out:
            out.append(clean)
    return out


def _append(root: Path, line: dict) -> None:
    """One JSON line, flushed to disk before the lock is released."""
    path = defects_path(root)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_lines(root: Path) -> list[dict]:
    path = defects_path(root)
    if not path.is_file():
        return []
    lines = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandsoffError(f"{DEFECTS_FILE} line {number} is not JSON: {exc.msg}") from exc
        if not isinstance(line, dict) or line.get("kind") not in {"record", "decision"}:
            raise HandsoffError(f"{DEFECTS_FILE} line {number} is neither a record nor a decision")
        lines.append(line)
    return lines


def load_defects(root: Path) -> list[dict]:
    """Every defect in recording order, its decisions folded in.

    A defect's state is `open` until a close decision lands, then `closed`.
    A decline is recorded in `decisions` and `declined_runs` but leaves the
    defect open, because it binds only the run that made it.
    """
    defects: dict[str, dict] = {}
    for line in _read_lines(root):
        if line["kind"] == "record":
            defect = {key: value for key, value in line.items() if key != "kind"}
            defect.update(state="open", decisions=[], declined_runs=[])
            defects[defect["id"]] = defect
            continue
        defect = defects.get(line.get("defect_id"))
        if defect is None:
            raise HandsoffError(f"{DEFECTS_FILE} has a decision for unknown defect {line.get('defect_id')!r}")
        decision = {key: value for key, value in line.items() if key not in {"kind", "defect_id"}}
        defect["decisions"].append(decision)
        if decision.get("action") == "decline" and decision.get("run_id"):
            defect["declined_runs"].append(decision["run_id"])
        elif decision.get("action") == "close":
            defect["state"] = "closed"
    return list(defects.values())


def _find(root: Path, defect_id: str) -> dict:
    for defect in load_defects(root):
        if defect["id"] == defect_id:
            return defect
    raise HandsoffError(f"defect: no defect {defect_id!r} in {DEFECTS_FILE}")


def record_defect(root: Path, fields: dict, *, lock_held: bool = False) -> dict:
    """Append one open defect; returns it as `load_defects` would show it."""
    if not isinstance(fields, dict):
        raise HandsoffError("defect: fields must be an object")
    unknown = sorted(set(fields) - set(RECORD_FIELDS))
    if unknown:
        raise HandsoffError(f"defect: unknown field(s) {unknown}")
    control = fields.get("control")
    if control not in CONTROLS:
        raise HandsoffError(f"defect: control must be one of {', '.join(CONTROLS)}")
    line = {
        "kind": "record",
        "id": f"DEF-{uuid.uuid4().hex[:8]}",
        "issue": _text(fields, "issue"),
        "control": control,
        "summary": _text(fields, "summary"),
        "regression": _text(fields, "regression"),
        "paths": _paths(fields.get("paths")),
        "recorded_by": _text(fields, "by"),
        "at": _now(),
    }
    root = Path(root).resolve()
    # E4 live proof: the CLI already holds the (non-reentrant) project lock
    with (contextlib.nullcontext() if lock_held else project_lock(root)):
        _read_lines(root)  # refuse to append to a ledger that no longer parses
        _append(root, line)
    return _find(root, line["id"])


def _criteria(acceptance) -> list[dict]:
    if isinstance(acceptance, dict):
        acceptance = acceptance.get("criteria")
    return [c for c in (acceptance or []) if isinstance(c, dict)]


def cites(criterion: dict, defect_id: str) -> bool:
    """A criterion adopts a defect when its requirement names the id as a word."""
    requirement = criterion.get("requirement")
    if not isinstance(requirement, str):
        return False
    return re.search(rf"(?<![\w-]){re.escape(defect_id)}(?![\w-])", requirement) is not None


def adopting_criteria(acceptance, defect_id: str) -> list[str]:
    return [str(c.get("id")) for c in _criteria(acceptance) if cites(c, defect_id)]


def _phase_number(status: dict) -> int:
    try:
        return int(status.get("phase_number", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _current_run(root: Path) -> tuple[dict, dict]:
    cfg = load_config(root)
    found = []
    for name in (cfg["status_file"], cfg["acceptance_file"]):
        path = root / name
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
        except json.JSONDecodeError as exc:
            raise HandsoffError(f"defect: {name} is not JSON: {exc.msg}") from exc
    return found[0], found[1]


def decide_defect(root: Path, defect_id: str, action: str, reason, by: str, run_id=None, *,
                  lock_held: bool = False) -> dict:
    """Decline a defect for one run, or close it.

    `decline` needs the run id and a reason; the defect stays open, so a
    later overlapping run inherits it again. `close` is refused until the
    project's current run has adopted the defect and reached Phase 8;
    a closed defect is never inherited and takes no further decisions.
    """
    if action not in DECISION_ACTIONS:
        raise HandsoffError(f"defect: action must be one of {', '.join(DECISION_ACTIONS)}")
    if not isinstance(defect_id, str) or not defect_id.strip():
        raise HandsoffError("defect: --id must be a non-empty string")
    defect_id = defect_id.strip()
    actor = _text({"by": by}, "by")
    root = Path(root).resolve()
    line = {"kind": "decision", "defect_id": defect_id, "action": action, "by": actor, "at": _now()}
    with (contextlib.nullcontext() if lock_held else project_lock(root)):
        defect = _find(root, defect_id)
        if defect["state"] == "closed":
            raise HandsoffError(f"defect: {defect_id} is already closed")
        if action == "decline":
            if not isinstance(run_id, str) or not run_id.strip():
                raise HandsoffError("defect decline: a run id is required; a decline binds one run only")
            line["run_id"] = run_id.strip()
            line["reason"] = _text({"reason": reason}, "reason")
            if line["run_id"] in defect["declined_runs"]:
                raise HandsoffError(f"defect: {defect_id} is already declined for run {line['run_id']}")
        else:
            status, acceptance = _current_run(root)
            adopters = adopting_criteria(acceptance, defect_id)
            phase = _phase_number(status)
            if not adopters:
                raise HandsoffError(
                    f"defect close: no criterion in the current run cites {defect_id}; "
                    "a defect closes only after a run that adopted it reaches Phase 8")
            if phase < 8:
                raise HandsoffError(
                    f"defect close: the run adopting {defect_id} ({', '.join(adopters)}) is at "
                    f"Phase {phase}; close it once that run reaches Phase 8")
            line["adopted_by"] = adopters
            line["phase_number"] = phase
            if isinstance(run_id, str) and run_id.strip():
                line["run_id"] = run_id.strip()
            if isinstance(reason, str) and reason.strip():
                line["reason"] = _text({"reason": reason}, "reason")
        _append(root, line)
    return _find(root, defect_id)


def _literal_prefix(glob: str) -> str:
    clean = _normal_glob(glob)
    match = _WILDCARDS.search(clean)
    return clean if match is None else clean[:match.start()]


def globs_overlap(a: str, b: str) -> bool:
    """True unless the two globs are provably disjoint.

    Each glob is cut at its first wildcard. If either literal prefix is a
    prefix of the other, some path may match both, so they overlap:
    `src/*/handler.py` and `src/auth/*.py` share `src/`. Only prefixes that
    diverge (`src/auth/` against `docs/`) prove the globs disjoint.
    """
    left, right = _literal_prefix(a), _literal_prefix(b)
    return left.startswith(right) or right.startswith(left)


def defect_overlaps_run(defect: dict, acceptance) -> bool:
    """A defect without paths, or a run with any criterion without paths
    (or no criteria at all), overlaps everything."""
    defect_paths = defect.get("paths") or []
    criteria = _criteria(acceptance)
    if not defect_paths or not criteria:
        return True
    run_paths = []
    for criterion in criteria:
        paths = [p for p in (criterion.get("paths") or []) if isinstance(p, str) and p.strip()]
        if not paths:
            return True
        run_paths.extend(paths)
    return any(globs_overlap(d, r) for d in defect_paths for r in run_paths)


def inherited_regressions(root: Path, acceptance, run_id) -> list[dict]:
    """Open defects overlapping this run, minus those it adopted or declined.

    The remainder is what Phase 3 must refuse on. Closed defects are never
    inherited.
    """
    run = str(run_id).strip() if run_id is not None else ""
    out = []
    for defect in load_defects(root):
        if defect["state"] != "open":
            continue
        if run and run in defect["declined_runs"]:
            continue
        if adopting_criteria(acceptance, defect["id"]):
            continue
        if defect_overlaps_run(defect, acceptance):
            out.append(defect)
    return out
