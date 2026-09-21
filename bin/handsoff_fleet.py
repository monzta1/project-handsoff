#!/usr/bin/env python3
"""Persistent local Fleet Mission Control for registered Handsoff projects."""
from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet_signals as signals_module  # noqa: E402
import handsoff_lib as lib  # noqa: E402

FLEET_ASSET_ROOT = lib.engine_root() / "fleet"
MAX_BODY = 8192


def fleet_engine_identity() -> dict:
    """#161: the engine THIS Fleet server runs, read once from the engine's
    own runtime manifest; unknown when it cannot be read."""
    root = lib.engine_root()
    try:
        version = json.loads((root / lib.RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8")).get("version") or "unknown"
    except (OSError, ValueError, AttributeError):
        version = "unknown"
    source = "project-drop-in" if (root / "bin").is_dir() and (root / "pyproject.toml").is_file() else "installed-engine"
    return {"version": version, "source": source if version != "unknown" else "unknown"}


FLEET_ENGINE = fleet_engine_identity()


def registry_path() -> Path:
    override = os.environ.get("HANDSOFF_FLEET_REGISTRY")
    return Path(override).expanduser().resolve() if override else Path.home() / ".handsoff" / "projects.json"


def load_registry(path: Path | None = None) -> list[dict]:
    path = path or registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise lib.HandsoffError(f"Fleet registry is unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("projects"), list):
        raise lib.HandsoffError("Fleet registry is invalid")
    result = []
    seen = set()
    for item in payload["projects"]:
        if not isinstance(item, dict) or not {"root", "registered_at"} <= set(item) \
                or set(item) - {"root", "registered_at", REGISTRY_MISSING_KEY, *REGISTRY_LOCK_KEYS}:
            raise lib.HandsoffError("Fleet registry contains an invalid project")
        root = str(Path(item["root"]).expanduser().resolve())
        if root not in seen:
            entry = {"root": root, "registered_at": item["registered_at"]}
            # #166: what the ticket lock wrote last (informational; the run's
            # own status file on disk is what the lock reads).
            for key in (*REGISTRY_LOCK_KEYS, REGISTRY_MISSING_KEY):
                if key in item:
                    entry[key] = item[key]
            result.append(entry)
            seen.add(root)
    return result


#: #166: optional per-entry fields the ticket lock maintains.
REGISTRY_LOCK_KEYS = ("work_items", "state", "updated_at")
#: #207: a register entry whose root vanished is forgotten once it has been
#: gone for a full pass, never on a transient absence; the entry records
#: when it was first seen missing.
REGISTRY_MISSING_KEY = "missing_since"
#: #207: the forget interval. A root is marked missing_since by the first
#: Fleet pass that finds it gone and removed by the first pass at or after
#: this many seconds from that mark; build_fleet runs a pass on every page
#: poll (5 s), so ORPHANED shows for this long plus at most one poll.
FORGET_AFTER_SECONDS = 60.0


def fleet_log_path(path: Path | None = None) -> Path:
    """The fleet log lives beside the register it describes."""
    return (path or registry_path()).parent / "fleet.log"


def _fleet_log(line: str, path: Path | None = None) -> None:
    try:
        path = fleet_log_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")
    except OSError:
        pass


def forget_missing_roots(path: Path | None = None, *, now: datetime | None = None,
                         forget_after: float = FORGET_AFTER_SECONDS) -> list[dict]:
    """#207: every registered root that no longer exists is marked
    missing_since on first sight and forgotten (unregistered, with one log
    line naming its last state and whether the run was ever closed) once it
    has been missing for `forget_after` seconds. A root that is back clears
    the mark. Returns the entries forgotten on this pass."""
    now = now or datetime.now(timezone.utc)
    projects = load_registry(path)
    changed = False
    forgotten = []
    retained = []
    for item in projects:
        root = Path(item["root"])
        if root.exists():
            if item.pop(REGISTRY_MISSING_KEY, None) is not None:
                changed = True
            retained.append(item)
            continue
        since = item.get(REGISTRY_MISSING_KEY)
        try:
            since_at = datetime.fromisoformat(since) if isinstance(since, str) else None
        except ValueError:
            since_at = None
        if since_at is None:
            item[REGISTRY_MISSING_KEY] = now.isoformat()
            changed = True
            retained.append(item)
            continue
        if (now - since_at).total_seconds() < forget_after:
            retained.append(item)
            continue
        # at or after one forget interval: this pass removes it
        state = item.get("state") or "unknown"
        never_closed = state not in ("closed", "complete")
        _fleet_log(f"fleet_entry_forgotten root={item['root']} last_state={state}"
                   f"{' run_never_closed=true' if never_closed else ''} missing_since={since}", path)
        forgotten.append({**item, "run_never_closed": never_closed})
        changed = True
    if changed:
        save_registry(retained, path)
    return forgotten


def live_managed_sessions(path: Path | None = None) -> list[dict]:
    """#216: every managed session that is launching or running on any
    registered root, read from each root's own status file: [{root, role,
    session_id, actor, state}]. A root that is gone or whose status cannot
    be read is skipped, never counted; the register is the list of roots
    and the ledger is the truth about sessions."""
    out = []
    for entry in load_registry(path):
        root = Path(entry["root"])
        try:
            cfg = lib.load_config(root)
            status = lib.load_unique_json(lib.status_path(root, cfg))
        except (lib.HandsoffError, OSError, ValueError):
            continue
        for role, session in lib.current_agent_sessions(status).items():
            if isinstance(session, dict) and session.get("state") in ("launching", "running"):
                out.append({"root": str(root), "role": role, "session_id": session.get("session_id"),
                            "actor": session.get("actor"), "state": session.get("state")})
    return sorted(out, key=lambda item: (item["root"], item["role"]))


def install_blocked(path: Path | None = None) -> dict | None:
    """#216: the engine badge's word while an install would land on a live
    session: {count, sessions: [{root, role, session_id}]} or None."""
    sessions = live_managed_sessions(path)
    if not sessions:
        return None
    return {"count": len(sessions), "sessions": [{k: s[k] for k in ("root", "role", "session_id")} for s in sessions]}


def forget_project(root: Path, path: Path | None = None) -> dict:
    """The FORGET button: removes exactly one entry whose root is gone;
    refuses a root that exists (close or unregister that one deliberately)."""
    if Path(str(root)).exists():
        raise lib.HandsoffError("the root exists; close the run or unregister it deliberately")
    projects = load_registry(path)
    entry = next((item for item in projects if item["root"] == str(root)), None)
    if entry is None:
        raise lib.HandsoffError("project is not registered")
    state = entry.get("state") or "unknown"
    never_closed = state not in ("closed", "complete")
    since = entry.get(REGISTRY_MISSING_KEY)
    _fleet_log(f"fleet_entry_forgotten root={entry['root']} last_state={state}"
               f"{' run_never_closed=true' if never_closed else ''} missing_since={since}"
               f" by=Fleet Mission Control Pilot", path)
    save_registry([item for item in projects if item["root"] != str(root)], path)
    return {"forgotten": entry["root"], "last_state": state, "run_never_closed": never_closed,
            "missing_since": since}
#: How long a register claim stands on its own before the run's status
#: file must exist to back it.
CLAIM_GRACE_SECONDS = 600


def _seconds_since(stamp: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp)).astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


class registry_lock:
    """#166: one exclusive lock beside the register, held from the read
    through the write, so two inits on one ticket cannot both see it free."""

    def __init__(self, path: Path | None = None):
        self.path = (path or registry_path()).with_suffix(".lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


def _run_state(root: Path) -> dict:
    """What the run at `root` is right now, from its own files: phase,
    status, whether it is closed or complete, its work item numbers and
    last event time. A root with no run reads {'state': 'none'}."""
    root = Path(root)
    try:
        cfg = lib.load_config(root)
        status_file = lib.status_path(root, cfg)
        if not status_file.is_file():
            return {"state": "none", "numbers": set()}
        status = lib.load_unique_json(status_file)
        acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    except (lib.HandsoffError, OSError, ValueError):
        return {"state": "unreadable", "numbers": set()}
    closed = isinstance(status.get("run_closed"), dict)
    complete = status.get("status") == "complete" or int(status.get("phase_number") or 0) >= 8
    numbers = set()
    for item in acceptance.get("work_items") or []:
        if isinstance(item, dict) and item.get("kind") == "issue" and isinstance(item.get("number"), int):
            numbers.add(item["number"])
    if not numbers:
        for item in lib.derive_work_items(status, acceptance, cfg).get("items", []):
            if item.get("kind") == "issue" and isinstance(item.get("number"), int):
                numbers.add(item["number"])
    return {"state": "closed" if closed else "complete" if complete else "open",
            "numbers": numbers, "phase": status.get("phase_number"),
            "updated_at": status.get("updated_at"), "feature": status.get("feature"),
            "status": status, "cfg": cfg}


def owner_alive(root: Path, state: dict | None = None) -> bool:
    """#166: the Fleet liveness rules, for adoption. Alive when a managed
    session is live with a fresh beacon or a verified PID, or when the
    run's owned dashboard answers as the owner. Otherwise dead."""
    root = Path(root)
    state = state or _run_state(root)
    status, cfg = state.get("status"), state.get("cfg")
    if isinstance(status, dict) and cfg is not None:
        try:
            view = lib.liveness_view(status, root, cfg)
            if view.get("process_signal") == "fresh":
                return True
        except (lib.HandsoffError, OSError, ValueError):
            pass
        for session_id, session in (status.get("agent_sessions") or {}).items():
            if isinstance(session, dict) and session.get("state") in lib.AGENT_SESSION_LIVE_STATES \
                    and lib._beacon_process_alive(root, session_id):
                return True
    owner = _owner_view(root)
    return bool(owner and owner.get("health") == "live")


def _age_text(stamp: str | None) -> str:
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp)).astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return "unknown age"
    minutes = int(max(seconds, 0) // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def ticket_owners(numbers: set[int], *, exclude_root: Path | None = None, path: Path | None = None) -> list[dict]:
    """Every registered run that is not closed or complete and lists one of
    `numbers`. Caller holds registry_lock when the answer decides a write."""
    exclude = str(Path(exclude_root).expanduser().resolve()) if exclude_root else None
    owners = []
    for entry in load_registry(path):
        if entry["root"] == exclude or not Path(entry["root"]).is_dir():
            continue  # a root that is gone holds nothing
        state = _run_state(Path(entry["root"]))
        if state["state"] == "none" and entry.get("state") == "open":
            # The claim was written under the lock and the status file is
            # still being created (init writes it after claiming). The
            # register entry IS the claim for a short grace; an init that
            # died in between leaves nothing that outlives it.
            if _seconds_since(entry.get("updated_at")) <= CLAIM_GRACE_SECONDS:
                state = {"state": "open", "numbers": set(entry.get("work_items") or []),
                         "phase": 1, "updated_at": entry.get("updated_at"), "feature": None}
        if state["state"] != "open":
            continue
        shared = sorted(numbers & state["numbers"])
        if not shared:
            continue
        owner = _owner_view(Path(entry["root"])) or {}
        owners.append({"root": entry["root"], "numbers": shared, "phase": state.get("phase"),
                       "port": owner.get("port") if owner.get("health") == "live" else None,
                       "last_event": state.get("updated_at"), "age": _age_text(state.get("updated_at")),
                       "alive": owner_alive(Path(entry["root"]), state), "feature": state.get("feature")})
    return owners


def claim_tickets(root: Path, numbers: set[int], *, adopt: bool = False, path: Path | None = None) -> dict:
    """#166: refuse when a live registered run owns any of `numbers`; with
    `adopt`, take over only from a dead owner (closed and complete owners
    never hold a ticket). On success the run is registered in the same
    locked transaction. Returns {'adopted_from': [...]} for the ledger."""
    root = Path(root).expanduser().resolve()
    with registry_lock(path):
        owners = ticket_owners(numbers, exclude_root=root, path=path)
        live = [o for o in owners if o["alive"] or not adopt]
        if owners and not adopt:
            o = owners[0]
            port = f", port {o['port']}" if o.get("port") else ""
            raise lib.HandsoffError(
                f"ticket lock: #{o['numbers'][0]} is owned by {o['root']} (phase {o['phase']}{port}, "
                f"last event {o['age']}); run-close it first, or init --adopt if it is dead")
        if adopt and live:
            o = live[0]
            raise lib.HandsoffError(
                f"ticket lock: #{o['numbers'][0]} is owned by a LIVE run at {o['root']} (phase {o['phase']}, "
                f"last event {o['age']}); a live owner cannot be adopted, run-close it first")
        note_registry_state(root, numbers, "open", path=path, locked=True)
        return {"adopted_from": [{"root": o["root"], "numbers": o["numbers"], "phase": o["phase"]} for o in owners]}


def note_registry_state(root: Path, numbers: set[int] | None, state: str, *, path: Path | None = None,
                        locked: bool = False) -> dict:
    """Write the run's state and ticket numbers on its register entry
    (registering it first if needed). `state` is open, closed or complete."""
    root = Path(root).expanduser().resolve()

    def write():
        projects = load_registry(path)
        entry = next((item for item in projects if item["root"] == str(root)), None)
        if entry is None:
            if not (root / "handsoff.toml").is_file():
                raise lib.HandsoffError(f"not a Handsoff project: {root}")
            entry = {"root": str(root), "registered_at": datetime.now(timezone.utc).isoformat()}
            projects.append(entry)
            projects.sort(key=lambda item: item["root"])
        entry["state"] = state
        if numbers is not None:
            entry["work_items"] = sorted(numbers)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_registry(projects, path)
        return dict(entry)

    if locked:
        return write()
    with registry_lock(path):
        return write()


def claimed_twice(path: Path | None = None) -> dict[str, list[int]]:
    """#166: root -> ticket numbers that another live open run also lists,
    for the red mark on both Fleet cards. Possible only through a register
    written outside the lock (or two machines), so it is shown, never fixed."""
    states = {entry["root"]: _run_state(Path(entry["root"])) for entry in load_registry(path)}
    result: dict[str, list[int]] = {}
    roots = [r for r, st in states.items() if st["state"] == "open" and st["numbers"]]
    for root in roots:
        for other in roots:
            if other == root:
                continue
            shared = states[root]["numbers"] & states[other]["numbers"]
            if shared:
                result.setdefault(root, [])
                result[root] = sorted(set(result[root]) | shared)
    return result


def save_registry(projects: list[dict], path: Path | None = None) -> None:
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lib.atomic_write_json(path, {"schema": 1, "projects": projects})


def register_project(root: Path, path: Path | None = None) -> dict:
    root = root.expanduser().resolve()
    if not (root / "handsoff.toml").is_file():
        raise lib.HandsoffError(f"not a Handsoff project: {root}")
    projects = load_registry(path)
    if not any(item["root"] == str(root) for item in projects):
        projects.append({"root": str(root), "registered_at": datetime.now(timezone.utc).isoformat()})
        projects.sort(key=lambda item: item["root"])
        save_registry(projects, path)
    return next(item for item in projects if item["root"] == str(root))


def unregister_project(root: Path, path: Path | None = None) -> bool:
    root = str(root.expanduser().resolve())
    projects = load_registry(path)
    retained = [item for item in projects if item["root"] != root]
    if len(retained) == len(projects):
        return False
    save_registry(retained, path)
    return True


def _owner_view(root: Path) -> dict | None:
    path = lib.dashboard_owner_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"health": "stale", "reason": "owner metadata is unreadable"}
    if not isinstance(record, dict):
        return {"health": "stale", "reason": "owner metadata is invalid"}
    result = {key: record.get(key) for key in (
        "pid", "host", "port", "run_token", "root_sha256", "started_at", "feature",
    )}
    expected_root = lib.dashboard_root_sha256(root)
    if record.get("root_sha256") != expected_root:
        return {**result, "health": "stale", "reason": "root binding mismatch"}
    try:
        host = record.get("host") if record.get("host") in lib.DASHBOARD_LOOPBACK_HOSTS else "127.0.0.1"
        answer = lib._dashboard_json_request(
            f"{lib._dashboard_base_url(host, record.get('port'))}/api/ownership", timeout=.25,
        )
        matched = answer.get("owned") is True and answer.get("run_token") == record.get("run_token") \
            and answer.get("root_sha256") == expected_root
        result.update({"health": "healthy" if matched else "stale",
                       "reason": "ownership verified" if matched else "live ownership mismatch"})
    except (OSError, ValueError, TypeError):
        result.update({"health": "stale", "reason": "owned dashboard is not responding"})
    return result


def _engine_version(root: Path) -> str:
    try:
        return lib.runtime_identity(root).get("version") or "unknown"
    except (OSError, ValueError, AttributeError, lib.HandsoffError):
        return "unknown"


def logo_key(root: Path) -> str:
    """Stable, opaque id for a registered root's logo route (never the path)."""
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]


def project_logo_url(root: Path) -> str | None:
    try:
        found = lib.project_logo(root, lib.load_config(root))
    except (lib.HandsoffError, OSError):
        found = None
    return f"/project-logo/{logo_key(root)}" if found else None


def project_view(entry: dict, signals: "signals_module.SignalCache | None" = None) -> dict:
    root = Path(entry["root"])
    # #152: the GitHub and Beakon signals come from the cache the server's
    # refresh thread fills; this function runs once a second and never
    # fetches anything itself.
    cached = signals.get(root) if signals is not None else dict(signals_module.EMPTY)
    owner = _owner_view(root)
    logo_url = project_logo_url(root) if root.exists() else None
    # REQ-006: Fleet may link only to a currently verified run-owned listener.
    # The loopback URL is deliberate so registry metadata can never redirect a
    # Fleet operator to a host chosen by project data or a stale owner file.
    if owner is None:
        dashboard_url, dashboard_note = None, "no run-owned dashboard"
    elif owner.get("health") == "stale":
        dashboard_url, dashboard_note = None, f"dashboard ownership is stale: {owner.get('reason', 'unknown reason')}"
    elif owner.get("health") == "healthy" and type(owner.get("port")) is int and 1 <= owner["port"] <= 65535:
        dashboard_url, dashboard_note = f"http://127.0.0.1:{owner['port']}/", ""
    else:
        dashboard_url, dashboard_note = None, "run dashboard is unavailable"
    try:
        snap = dashboard.build_snapshot(root)
    except Exception as exc:  # fleet must retain a broken project as an actionable row
        snap = {"initialized": False, "error": f"{type(exc).__name__}: {exc}"}
    if not snap.get("initialized"):
        seed = {"root": str(root), "error": snap.get("error"), "owner": owner}
        binding = hashlib.sha256(json.dumps(seed, sort_keys=True).encode()).hexdigest()[:20]
        return {"root": str(root), "name": root.name, "registered_at": entry["registered_at"], "logo_url": logo_url,
                # #154: a registered project with no run is idle, not quiet; idle
                # is filed with the finished runs, quiet is an ongoing run.
                "initialized": False, "state": "orphaned" if not root.exists() else "idle",
                "missing_since": entry.get(REGISTRY_MISSING_KEY),  # #207
                "error": snap.get("error"), "owner": owner, "binding": binding,
                "engine_version": _engine_version(root), "decisions": [],
                "dashboard_url": dashboard_url, "dashboard_note": dashboard_note,
                "github": cached["github"], "beakon": cached["beakon"]}
    status = snap["status"]
    live = snap.get("live") or {}
    verification_live = (snap.get("verification") or {}).get("live") or {}
    closed = isinstance(status.get("run_closed"), dict) or status.get("status") == "closed"
    if closed:
        state = "closed"
    elif status.get("status") == "complete":
        state = "complete"
    elif (owner is not None and owner.get("health") != "healthy"
          and owner.get("reason") == "owned dashboard is not responding"):
        state = "offline"
    elif (snap.get("input_required", {}).get("required")
          and snap.get("input_required", {}).get("turn") == "pilot"
          and snap.get("input_required", {}).get("preauthorized") is None):
        state = "waiting"
    elif (snap.get("verification") or {}).get("live", {}).get("in_flight"):
        state = "running"
    elif (snap.get("verification") or {}).get("live", {}).get("last_failure"):
        state = "failed"
    elif live.get("state") in {"running", "started"}:
        state = "running"
    elif live.get("state") == "stalled":
        state = "stalled"
    elif live.get("state") == "failed":
        state = "failed"
    else:
        state = "quiet"
    role = snap.get("actors", {}).get("active_role")
    session = (snap.get("runtime", {}).get("current_sessions") or {}).get(role) if role else None
    decisions = [item for item in (snap.get("operator_actions") or [])
                 if item.get("kind") not in {"run_close", "run_reopen", "pause"}]
    seed = {"root": str(root), "updated_at": status.get("updated_at"),
            "owner": owner.get("run_token") if owner else None,
            "closed": status.get("run_closed"), "actions": [item.get("action_id") for item in decisions]}
    binding = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return {
        "root": str(root), "name": root.name, "registered_at": entry["registered_at"], "initialized": True,
        "logo_url": logo_url,
        "feature": snap.get("project", {}).get("feature"), "phase": status.get("phase"),
        "host": (snap.get("host") or {}).get("family") or "unknown",  # #186
        "host_wait": snap.get("host_wait"),  # #194
        "phase_number": status.get("phase_number"), "progress": status.get("progress"), "state": state,
        "next_action": status.get("next_action"), "role": role,
        "started_at": (snap.get("metrics") or {}).get("started_at"),
        "asleep_seconds": (snap.get("metrics") or {}).get("asleep_seconds"),  # #193
        "phase_asleep_seconds": ((snap.get("metrics") or {}).get("phase_asleep_seconds") or {}).get(str(status.get("phase_number"))),
        "ended_at": (snap.get("metrics") or {}).get("ended_at"), "adapter": session.get("adapter") if session else None,
        "model": (session.get("reported_model") or session.get("requested_model")) if session else None,
        "last_activity": (snap.get("live") or {}).get("last_activity_at") or status.get("updated_at"),
        "decisions": decisions, "failure": snap.get("recovery"), "owner": owner,
        "engine_version": _engine_version(root), "binding": binding,
        "updated_at": status.get("updated_at"), "run_closed": status.get("run_closed"),
        "sessions": list((snap.get("runtime", {}).get("current_sessions") or {}).values()),
        "dashboard_url": dashboard_url, "dashboard_note": dashboard_note,
        "github": cached["github"], "beakon": cached["beakon"],
    }


def _public_dashboard_url(project: dict, public_base: str | None) -> dict:
    """#124: through a tunnel the loopback link does not resolve; when the
    request came in on a configured public origin, link the owned port on
    that host instead (the port itself is still what the tunnel exposes)."""
    if not public_base or not project.get("dashboard_url"):
        return project
    from urllib.parse import urlsplit
    port = urlsplit(project["dashboard_url"]).port
    base = urlsplit(public_base)
    return {**project, "dashboard_url": f"{base.scheme}://{base.hostname}:{port}/"}


def build_metrics(path: Path | None = None, issues: "signals_module.IssueCache | None" = None,
                  started_at: str | None = None) -> dict:
    """#153: the Metrics tab's data, read from the issue cache only. Only
    currently registered roots appear, and a cached entry whose repo is no
    longer the project's origin is dropped: issues never follow a root to a
    different repository."""
    projects = []
    for entry in load_registry(path):
        root = Path(entry["root"])
        cached = issues.get(root) if issues is not None else None
        if cached is None:
            projects.append({"root": str(root), "name": root.name, "repo": None, "fetched_at": None,
                             "error": None, "issues": [], "commits": [], "releases": [], "commits_since": None})
            continue
        current = signals_module.origin_repo(root) if root.exists() else None
        # #160: identity is case-insensitive, like the collector's grouping.
        if cached.get("repo") and signals_module.repo_identity(cached["repo"]) != signals_module.repo_identity(current):
            projects.append({"root": str(root), "name": root.name, "repo": current, "fetched_at": None,
                             "error": f"cached issues belong to {cached['repo']}; awaiting refresh",
                             "issues": [], "commits": [], "releases": [], "commits_since": None})
            continue
        projects.append({"root": str(root), "name": root.name, "repo": cached.get("repo"),
                         "fetched_at": cached.get("fetched_at"), "error": cached.get("error"),
                         "issues": list(cached.get("issues") or []), "commits": list(cached.get("commits") or []),
                         "releases": list(cached.get("releases") or []), "commits_since": cached.get("commits_since"),
                         "updated_since": cached.get("updated_since"), "full_pass_at": cached.get("full_pass_at"),
                         "requests_total": cached.get("requests_total"), "requests_counted": cached.get("requests_counted")})
    # #168: tokens per closed ticket, from archived runs' recorded usage only.
    try:
        per_ticket = lib.tokens_per_ticket()
    except (OSError, ValueError):
        per_ticket = {}
    for project in projects:
        entry = per_ticket.get(project["root"]) or {}
        project["tokens"] = entry.get("tickets", {})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "started_at": started_at,
            "refreshed_at": issues.refreshed_at if issues is not None else None,
            # #158: the last X-RateLimit answer the collector saw, for the page's budget readout.
            "rate_limit": issues.last_rate if issues is not None else None, "projects": projects}


def build_fleet(path: Path | None = None, public_base: str | None = None,
                signals: "signals_module.SignalCache | None" = None) -> dict:
    forget_missing_roots(path)  # #207: a vanished root is ORPHANED for one pass, then gone
    projects = [_public_dashboard_url(project_view(entry, signals), public_base) for entry in load_registry(path)]
    # #166: a ticket two live open runs both list is shown in red on both cards.
    try:
        twice = claimed_twice(path)
    except lib.HandsoffError:
        twice = {}
    for item in projects:
        item["claimed_twice"] = twice.get(item["root"], [])
    decisions = [{"root": item["root"], "project": item["name"], "feature": item.get("feature"), **action}
                 for item in projects for action in item.get("decisions", [])]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "projects": projects,
            "engine": {**FLEET_ENGINE, "install_blocked": install_blocked(path)},  # #161, #216
            "decisions": decisions, "counts": {state: sum(item["state"] == state for item in projects)
                                                 for state in ("running", "quiet", "waiting", "stalled", "failed", "offline", "complete", "closed", "idle", "orphaned")}}


class FleetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, registry: Path | None = None, *, signals=None, signals_interval=None,
                 issues=None, issues_interval=None):
        self.registry = registry
        self.stopping = False
        self.started_at = datetime.now(timezone.utc).isoformat()
        # #152: one signal cache for the server's lifetime, filled by a daemon
        # thread that starts now and returns control before its first pass.
        # #153: the issue cache behind the Metrics tab, same shape, its own
        # slower clock (default 900 s; the lists are larger).
        self.signals = signals if signals is not None else signals_module.SignalCache(registry=registry or registry_path())
        self.issues = issues if issues is not None else signals_module.IssueCache(registry=registry or registry_path())
        super().__init__(address, FleetHandler)
        # Only a server that bound its port collects; a failed bind leaves no thread behind.
        roots = lambda: [entry["root"] for entry in load_registry(self.registry)]  # noqa: E731
        # #157: both threads wake on a registry change, so a project registered
        # mid-interval is collected within seconds, not at the next pass.
        wake_path = self.registry or registry_path()
        self.signals_thread = signals_module.start_refresh_thread(self.signals, roots, interval=signals_interval,
                                                                  wake_path=wake_path)
        self.issues_thread = signals_module.start_refresh_thread(
            self.issues, roots, name="fleet-issues", wake_path=wake_path,
            interval=signals_module.issues_interval() if issues_interval is None else issues_interval)


class FleetHandler(BaseHTTPRequestHandler):
    server: FleetServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def _headers(self, code, content_type, length):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()

    def _json(self, code, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        self._headers(code, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _same_origin(self):
        # #124: loopback always; otherwise one HANDSOFF_PUBLIC_ORIGINS entry, exactly.
        try:
            public = lib.fleet_public_origins()
        except lib.HandsoffError:
            public = []
        return lib.origin_allowed(self.headers.get("Origin"), self.server.server_port, public)

    def _public_base(self) -> str | None:
        """The public scheme://host this request arrived on, when it is a
        configured public origin; None for loopback requests."""
        try:
            public = lib.fleet_public_origins()
        except lib.HandsoffError:
            return None
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
        scheme = (self.headers.get("X-Forwarded-Proto") or "http").strip().lower()
        for candidate in (f"{scheme}://{host}", f"https://{host}"):
            try:
                canonical = lib.normalize_public_origins([candidate], "request")[0]
            except lib.HandsoffError:
                continue
            if canonical in public:
                return canonical
        return None

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/fleet":
            self._json(HTTPStatus.OK, build_fleet(self.server.registry, public_base=self._public_base(),
                                                  signals=self.server.signals))
            return
        if path == "/api/metrics":
            self._json(HTTPStatus.OK, build_metrics(self.server.registry, self.server.issues, self.server.started_at))
            return
        if path == "/api/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = None
            try:
                while not self.server.stopping:
                    payload = build_fleet(self.server.registry, signals=self.server.signals)
                    signature = hashlib.sha256(json.dumps(payload["projects"], sort_keys=True).encode()).hexdigest()
                    if signature != last:
                        self.wfile.write(f"event: fleet\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())
                        self.wfile.flush()
                        last = signature
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path.startswith("/project-logo/"):
            key = path[len("/project-logo/"):]
            found = None
            for entry in load_registry(self.server.registry):
                root = Path(entry["root"])
                if root.exists() and logo_key(root) == key:
                    try:
                        found = lib.project_logo(root, lib.load_config(root))
                    except (lib.HandsoffError, OSError):
                        found = None
                    break
            if not found:
                self._json(HTTPStatus.NOT_FOUND, {"error": "No project logo"})
                return
            try:
                body = found[0].read_bytes()
            except OSError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "No project logo"})
                return
            self._headers(HTTPStatus.OK, found[1], len(body))
            self.wfile.write(body)
            return
        if path == "/logo.png":
            # The Handsoff mark, shared with Mission Control.
            try:
                body = lib.engine_resource_path("dashboard/logo.png").read_bytes()
            except OSError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            self._headers(HTTPStatus.OK, "image/png", len(body))
            self.wfile.write(body)
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                  "/metrics": ("metrics.html", "text/html; charset=utf-8"),
                  "/metrics.js": ("metrics.js", "text/javascript; charset=utf-8")}
        asset = assets.get(path)
        if asset:
            try:
                body = (FLEET_ASSET_ROOT / asset[0]).read_bytes()
                self._headers(HTTPStatus.OK, asset[1], len(body))
                self.wfile.write(body)
            except OSError:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Fleet assets are missing"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/api/release-port", "/api/close-run", "/api/reopen-run", "/api/forget"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not self._same_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Same-origin Fleet request required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise lib.HandsoffError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise lib.HandsoffError("request must be a JSON object")
            root = str(Path(body.get("root", "")).resolve())
            project = next((item for item in build_fleet(self.server.registry, signals=self.server.signals)["projects"]
                            if item["root"] == root), None)
            if not project:
                raise lib.HandsoffError("project is not registered")
            if body.get("binding") != project.get("binding"):
                self._json(HTTPStatus.CONFLICT, {"error": "Fleet state changed; review the refreshed row"})
                return
            if body.get("confirm") is not True:
                raise lib.HandsoffError("explicit confirmation is required")
            if path == "/api/forget":
                result = forget_project(Path(root), self.server.registry)  # #207
            elif path == "/api/release-port":
                result = lib.release_run_dashboard(Path(root))
            elif path == "/api/close-run":
                result = lib.close_run(Path(root), by="Fleet Mission Control Pilot",
                                       reason=body.get("reason"), expected_updated_at=project.get("updated_at"),
                                       cancel_active=body.get("cancel_active") is True)
            else:
                result = lib.reopen_run(Path(root), by="Fleet Mission Control Pilot",
                                        reason=body.get("reason"), expected_updated_at=project.get("updated_at"))
            self._json(HTTPStatus.OK, {"ok": True, "result": result})
        except (lib.HandsoffError, ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def serve(host="127.0.0.1", port=8765, *, open_browser=True, registry=None):
    if host not in lib.DASHBOARD_LOOPBACK_HOSTS:
        raise lib.HandsoffError("Fleet Mission Control binds to localhost only")
    server = FleetServer((host, port), registry)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"HANDSOFF_FLEET: {url}")
    if open_browser:
        threading.Timer(.25, lambda: lib.open_dashboard_url(url)).start()
    try:
        server.serve_forever(.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping = True
        server.signals_thread.stop_event.set()
        server.issues_thread.stop_event.set()
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Handsoff Fleet Mission Control")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("root")
    unregister = sub.add_parser("unregister")
    unregister.add_argument("root")
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "register":
            print(json.dumps(register_project(Path(args.root)), sort_keys=True))
            return 0
        if args.command == "unregister":
            print("UNREGISTERED" if unregister_project(Path(args.root)) else "NOT_REGISTERED")
            return 0
        return serve(args.host, args.port, open_browser=not args.no_open)
    except lib.HandsoffError as exc:
        print(f"HANDSOFF_FLEET_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
