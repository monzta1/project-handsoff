#!/usr/bin/env python3
"""Persistent local Fleet Mission Control for registered Handsoff projects."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import fcntl
import json
import os
import shlex
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet_signals as signals_module  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_queue  # noqa: E402

FLEET_ASSET_ROOT = lib.engine_root() / "fleet"
MAX_BODY = 8192
FLEET_TOKEN_ENV = "HANDSOFF_FLEET_TOKEN"


def fleet_token(token_file: str | Path | None = None) -> str | None:
    """#285: the Fleet access token, from --token-file or HANDSOFF_FLEET_TOKEN;
    None when neither holds a non-blank value."""
    if token_file:
        try:
            value = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise lib.HandsoffError(f"Fleet token file is unreadable: {exc}") from exc
        if not value:
            raise lib.HandsoffError("Fleet token file is empty")
        return value
    return (os.environ.get(FLEET_TOKEN_ENV) or "").strip() or None


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
                         forget_after: float = FORGET_AFTER_SECONDS,
                         locked: bool = False) -> list[dict]:
    """#207: every registered root that no longer exists is marked
    missing_since on first sight and forgotten (unregistered, with one log
    line naming its last state and whether the run was ever closed) once it
    has been missing for `forget_after` seconds. A root that is back clears
    the mark. Returns the entries forgotten on this pass."""
    if not locked:
        with registry_lock(path):
            return forget_missing_roots(path, now=now, forget_after=forget_after, locked=True)
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


def live_managed_sessions(path: Path | None = None, states: dict | None = None) -> list[dict]:
    """#216: every managed session that is launching or running on any
    registered root, read from each root's own status file: [{root, role,
    session_id, actor, state}]. A root that is gone or whose status cannot
    be read is skipped, never counted; the register is the list of roots
    and the ledger is the truth about sessions. #393: `states` (root ->
    _run_state) is the build's own read, reused instead of reading again."""
    out = []
    for entry in load_registry(path):
        root = Path(entry["root"])
        if states is not None:
            status = (states.get(entry["root"]) or {}).get("status")
            if not isinstance(status, dict):
                continue
        else:
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


def install_blocked(path: Path | None = None, states: dict | None = None) -> dict | None:
    """#216: the engine badge's word while an install would land on a live
    session: {count, sessions: [{root, role, session_id}]} or None."""
    sessions = live_managed_sessions(path, states)
    if not sessions:
        return None
    return {"count": len(sessions), "sessions": [{k: s[k] for k in ("root", "role", "session_id")} for s in sessions]}


def forget_project(root: Path, path: Path | None = None, *, locked: bool = False) -> dict:
    """The FORGET button: removes exactly one entry whose root is gone;
    refuses a root that exists (close or unregister that one deliberately)."""
    if not locked:
        with registry_lock(path):
            return forget_project(root, path, locked=True)
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

    def __init__(self, path: Path | None = None, *, timeout: float = 5.0):
        self.path = (path or registry_path()).with_suffix(".lock")
        self.handle = None
        self.timeout = timeout

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    _fleet_log(f"registry_lock_timeout lock={self.path} timeout_seconds={self.timeout}", self.path)
                    raise lib.HandsoffError(
                        f"Fleet registry lock timed out after {self.timeout:g}s; retry the mutation")
                time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


def _run_state(root: Path, cfg: dict | None = None) -> dict:
    """What the run at `root` is right now, from its own files: phase,
    status, whether it is closed or complete, its work item numbers and
    last event time. A root with no run reads {'state': 'none'}. #393: a
    build passes the config it already loaded."""
    root = Path(root)
    try:
        cfg = cfg if cfg is not None else lib.load_config(root)
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


def _entry_repo(entry: dict) -> str | None:
    """#400: the owner/name a registered run belongs to: the repository its
    init recorded on its own acceptance registry at claim time, so a later
    origin change never releases its tickets. Only a run with no recorded
    repository (initialised before #400) is read from its root's origin.
    Never stored on the register: an older engine refuses a register entry
    with a key it does not know, so the format stays as is."""
    root = Path(entry["root"])
    try:
        acceptance = lib.load_unique_json(lib.acceptance_path(root, lib.load_config(root)))
    except (lib.HandsoffError, OSError, ValueError):
        acceptance = {}
    if isinstance(acceptance, dict) and "repository" in acceptance:
        recorded = acceptance["repository"]
        return recorded if isinstance(recorded, str) and recorded else None
    return signals_module.origin_repo(root)


def ticket_owners(numbers: set[int], *, exclude_root: Path | None = None, path: Path | None = None,
                  repo: str | None = None) -> list[dict]:
    """Every registered run that is not closed or complete and lists one of
    `numbers`. Caller holds registry_lock when the answer decides a write.
    #400: tickets compare within one repository; two known, different
    repositories never share one, and an unknown side compares by number."""
    exclude = str(Path(exclude_root).expanduser().resolve()) if exclude_root else None
    owners = []
    for entry in load_registry(path):
        if entry["root"] == exclude or not Path(entry["root"]).is_dir():
            continue  # a root that is gone holds nothing
        entry_repo = _entry_repo(entry)
        if repo and entry_repo and signals_module.repo_identity(repo) != signals_module.repo_identity(entry_repo):
            continue
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
        owners.append({"root": entry["root"], "repo": entry_repo, "numbers": shared, "phase": state.get("phase"),
                       "port": owner.get("port") if owner.get("health") == "live" else None,
                       "last_event": state.get("updated_at"), "age": _age_text(state.get("updated_at")),
                       "alive": owner_alive(Path(entry["root"]), state), "feature": state.get("feature")})
    return owners


def claim_tickets(root: Path, numbers: set[int], *, adopt: bool = False, path: Path | None = None,
                  repo: str | None = None) -> dict:
    """#166: refuse when a live registered run owns any of `numbers`; with
    `adopt`, take over only from a dead owner (closed and complete owners
    never hold a ticket). On success the run is registered in the same
    locked transaction. Returns {'adopted_from': [...]} for the ledger.
    #400: `repo` is the run's owner/name (its origin remote when omitted)."""
    root = Path(root).expanduser().resolve()
    repo = repo if repo is not None else signals_module.origin_repo(root)
    with registry_lock(path):
        owners = ticket_owners(numbers, exclude_root=root, path=path, repo=repo)
        live = [o for o in owners if o["alive"] or not adopt]
        if owners and not adopt:
            o = owners[0]
            port = f", port {o['port']}" if o.get("port") else ""
            raise lib.HandsoffError(
                f"ticket lock: #{o['numbers'][0]} is owned by {o['root']} (repository "
                f"{o['repo'] or 'unknown'}, phase {o['phase']}{port}, "
                f"last event {o['age']}); run-close it first, or init --adopt if it is dead")
        if adopt and live:
            o = live[0]
            raise lib.HandsoffError(
                f"ticket lock: #{o['numbers'][0]} is owned by a LIVE run at {o['root']} (repository "
                f"{o['repo'] or 'unknown'}, phase {o['phase']}, "
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


def claimed_twice(path: Path | None = None, states: dict | None = None) -> dict[str, list[int]]:
    """#166: root -> ticket numbers that another live open run also lists,
    for the red mark on both Fleet cards. Possible only through a register
    written outside the lock (or two machines), so it is shown, never fixed."""
    if states is None:
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


def register_project(root: Path, path: Path | None = None, *, locked: bool = False) -> dict:
    root = root.expanduser().resolve()
    if not (root / "handsoff.toml").is_file():
        raise lib.HandsoffError(f"not a Handsoff project: {root}")
    if not locked:
        with registry_lock(path):
            return register_project(root, path, locked=True)
    projects = load_registry(path)
    changed = False
    if not any(item["root"] == str(root) for item in projects):
        projects.append({"root": str(root), "registered_at": datetime.now(timezone.utc).isoformat()})
        projects.sort(key=lambda item: item["root"])
        changed = True
    entry = next(item for item in projects if item["root"] == str(root))
    # #432: the record's state is the run's, so a reopened run reads open.
    changed = _derive_entry_state(entry, _run_state(root)) or changed
    if changed:
        save_registry(projects, path)
    return entry


#: #432: run states a register entry records; 'none' and 'unreadable' leave it alone.
REGISTRY_DERIVED_STATES = ("open", "closed", "complete")


def _derive_entry_state(entry: dict, run_state: dict | None) -> bool:
    """#432: set `entry['state']` to the run's own state when the run's
    status says open, closed or complete and the stored value differs, so a
    stored closed never outlives a reopen. True when the entry changed."""
    derived = (run_state or {}).get("state")
    if derived not in REGISTRY_DERIVED_STATES or entry.get("state") == derived:
        return False
    entry["state"] = derived
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    return True


def sync_registry_states(states: dict[str, dict], path: Path | None = None) -> list[str]:
    """#432: every register entry whose stored state differs from its run's
    (`states` is root -> _run_state) is rewritten under the lock. Returns the
    roots changed; writes nothing when none did."""
    if not any((states.get(entry["root"]) or {}).get("state") in REGISTRY_DERIVED_STATES
               and entry.get("state") != states[entry["root"]]["state"] for entry in load_registry(path)):
        return []
    with registry_lock(path):
        projects = load_registry(path)
        changed = [entry["root"] for entry in projects if _derive_entry_state(entry, states.get(entry["root"]))]
        if changed:
            save_registry(projects, path)
        return changed


def unregister_project(root: Path, path: Path | None = None, *, locked: bool = False) -> bool:
    root = str(root.expanduser().resolve())
    if not locked:
        with registry_lock(path):
            return unregister_project(Path(root), path, locked=True)
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


#: #393: a cached view is rebuilt after this long even when no file moved,
#: because a few snapshot fields (a stalled beacon, a host's silence) are
#: read from the clock rather than from a file.
SNAPSHOT_MAX_AGE_SECONDS = 30.0
#: #393: owner probes run side by side, at most this many at once.
OWNER_PROBE_WORKERS = 16
#: The run's durable runtime-control records (monitor, performance clock).
RUNTIME_CONTROL_DIR = ".handsoff-runtime-control"
#: #393: the performance clock is rewritten by every snapshot build (its
#: active seconds tick), and its timeline journal (#385) is appended by the
#: same refresh on a transition, so both are the build's output, not an
#: input. A pause or resume therefore reaches the card within the cache's
#: SNAPSHOT_MAX_AGE_SECONDS rather than at once.
RUNTIME_CONTROL_SELF_WRITTEN = frozenset({"performance.json", "performance.json.bak",
                                          "performance-timeline.jsonl"})


def _pinned_root(root: Path, text: str) -> Path:
    """#393: `root` as a path whose handsoff.toml reads back `text`, so
    load_config parses the build's one read instead of reading the file
    again. Every path derived from it keeps the pin (it is a class attribute)."""
    pinned = str(root / "handsoff.toml")

    class PinnedPath(type(Path())):
        def read_text(self, *args, **kwargs):
            return text if str(self) == pinned else super().read_text(*args, **kwargs)
    return PinnedPath(root)


def _file_stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        return (-1, -1, -1)


def project_config(root: Path, cache: "ProjectViewCache | None" = None) -> dict:
    """#393: the one read of a project's handsoff.toml in a Fleet build:
    the loaded config (None when it does not load, with the reason) and
    (#389) its configured [project] name, which load_config does not keep.
    With `cache`, an unchanged file is not read at all."""
    root = Path(root)
    stamp = _file_stamp(root / "handsoff.toml")
    hit = cache.config(str(root), stamp) if cache is not None else None
    if hit is not None:
        return hit
    try:
        text = (root / "handsoff.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError as exc:
        text = exc
    cfg, error, value = None, None, None
    try:
        if isinstance(text, OSError):
            raise lib.HandsoffError(f"cannot load {root / 'handsoff.toml'}: {text}")
        cfg = lib.load_config(_pinned_root(root, text) if isinstance(text, str) else root)
    except (lib.HandsoffError, OSError, ValueError) as exc:
        error = str(exc)
    if isinstance(text, str):
        try:
            value = (tomllib.loads(text).get("project") or {}).get("name")
        except (ValueError, AttributeError):
            value = None
    config = {"cfg": cfg, "configured_name": value.strip() if isinstance(value, str) and value.strip() else None,
              "config_error": error}
    if cache is not None:
        cache.put_config(str(root), stamp, config)
    return config


def card_name(root: Path, configured_name: str | None, origin: str | None = None) -> str:
    """#389: a card is named by the project's configured [project] name,
    else its origin repository's name, else its folder."""
    if configured_name:
        return configured_name
    root = Path(root)
    repo = origin if origin is not None else (signals_module.origin_repo(root) if root.exists() else None)
    return repo.rsplit("/", 1)[-1] if repo else root.name


def _git_paths(root: Path) -> list[Path]:
    """#393: the repository files whose change means a commit or checkout:
    HEAD, the ref HEAD names, packed-refs and the index. A worktree's .git
    file is followed to its own git dir and the common dir it shares."""
    for candidate in (root, *root.parents):
        dot = candidate / ".git"
        if dot.exists():
            break
    else:
        return []
    try:
        if dot.is_dir():
            gitdir = dot
        else:
            text = dot.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return []
            gitdir = (candidate / text[len("gitdir:"):].strip()).resolve()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        paths = [gitdir / "HEAD", gitdir / "index", common / "packed-refs"]
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            paths += [gitdir / ref, common / ref]
        return paths
    except OSError:
        return []


def artifact_signature(root: Path, cfg: dict | None) -> tuple:
    """#393: a cheap fingerprint of everything a project's Fleet view is
    built from: the ledger files (the dashboard's own SSE list), the
    runtime-control records, the dashboard owner record, and the repository
    HEAD and index, so a commit or a checkout invalidates it too."""
    root = Path(root)
    paths = [root / "handsoff.toml", root / lib.RUNTIME_MANIFEST_FILE, root / lib.VERSION_PIN_FILE,
             lib.dashboard_owner_path(root), lib.live_beacon_path(root)]
    if cfg is not None:
        try:
            paths += [lib.status_path(root, cfg), lib.acceptance_path(root, cfg), lib.event_log_path(root, cfg),
                      lib.verification_log_path(root, cfg), lib.design_evidence_path(root),
                      lib.output_liveness_path(root), lib.agent_output_path(root), lib.operations_path(root),
                      root / lib.PREFLIGHT_FILE, root / ".handsoff-regression.json",
                      root / dashboard.test_progress.PROGRESS_FILE, root / dashboard.tranche.PROPOSAL_FILE]
        except lib.HandsoffError:
            pass
    try:
        paths += sorted(root.glob(".handsoff-live-*.json"))
        control = root / RUNTIME_CONTROL_DIR
        paths += sorted(path for path in control.iterdir()
                        if path.name not in RUNTIME_CONTROL_SELF_WRITTEN) if control.is_dir() else []
    except OSError:
        pass
    paths += _git_paths(root)
    signature = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
        except OSError:
            signature.append((str(path), -1, -1, -1))
    return tuple(signature)


class ProjectViewCache:
    """#393: each project's snapshot and run state, kept until its artifact
    signature changes (or SNAPSHOT_MAX_AGE_SECONDS pass), so a warm Fleet
    build rebuilds nothing that did not change."""

    def __init__(self, max_age: float = SNAPSHOT_MAX_AGE_SECONDS):
        self.max_age = max_age
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}

    def config(self, root: str, stamp: tuple) -> dict | None:
        with self.lock:
            entry = self.configs.get(root)
        if entry and entry["stamp"] == stamp and time.monotonic() - entry["at"] < self.max_age:
            return entry["config"]
        return None

    def put_config(self, root: str, stamp: tuple, config: dict) -> None:
        with self.lock:
            self.configs[root] = {"stamp": stamp, "config": config, "at": time.monotonic()}

    def get(self, root: str, signature: tuple) -> dict | None:
        with self.lock:
            entry = self.entries.get(root)
        if entry and entry["signature"] == signature and time.monotonic() - entry["at"] < self.max_age:
            return entry
        return None

    def put(self, root: str, signature: tuple, snap: dict, state: dict) -> None:
        with self.lock:
            self.entries[root] = {"signature": signature, "snap": snap, "state": state, "at": time.monotonic()}

    def retain(self, roots: set[str]) -> None:
        with self.lock:
            for root in set(self.entries) - roots:
                del self.entries[root]
            for root in set(self.configs) - roots:
                del self.configs[root]


VIEW_CACHE = ProjectViewCache()


def project_facts(root: Path, config: dict | None = None, cache: ProjectViewCache | None = None) -> dict:
    """#393: the config read once, plus the snapshot and run state, from
    `cache` when the project's artifact signature is unchanged."""
    root = Path(root)
    config = config if config is not None else project_config(root, cache)
    cfg = config["cfg"]
    signature = artifact_signature(root, cfg)
    hit = cache.get(str(root), signature) if cache is not None else None
    if hit is not None:
        return {**config, "snap": hit["snap"], "state": hit["state"]}
    if cfg is None:
        # The snapshot would load the same config and fail the same way.
        snap = {"initialized": False, "generated_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
                "error": config.get("config_error") or "handsoff.toml did not load"}
    else:
        try:
            snap = dashboard.build_snapshot(root, cfg=cfg)
        except Exception as exc:  # fleet must retain a broken project as an actionable row
            snap = {"initialized": False, "error": f"{type(exc).__name__}: {exc}"}
    state = _run_state(root, cfg) if cfg is not None else {"state": "unreadable", "numbers": set()}
    if cache is not None:
        cache.put(str(root), signature, snap, state)
    return {**config, "snap": snap, "state": state}


def host_working_age(root: Path, cfg: dict | None, status: dict, now: float | None = None) -> int | None:
    """#389: seconds since the run's ledger (status or event log) was last
    written, when no managed session is live and that write falls within
    [recovery] live_session_silence_minutes; None otherwise."""
    if cfg is None or any(isinstance(session, dict) and session.get("state") in lib.AGENT_SESSION_LIVE_STATES
                          for session in (status.get("agent_sessions") or {}).values()):
        return None
    written = []
    for path in (lib.status_path(root, cfg), lib.event_log_path(root, cfg)):
        try:
            written.append(path.stat().st_mtime)
        except OSError:
            pass
    if not written:
        return None
    age = max(0.0, (time.time() if now is None else now) - max(written))
    window = float((cfg.get("recovery") or {}).get("live_session_silence_minutes", 10)) * 60
    return int(age) if age <= window else None


_PROBE = object()


def project_view(entry: dict, signals: "signals_module.SignalCache | None" = None, *,
                 facts: dict | None = None, owner=_PROBE) -> dict:
    root = Path(entry["root"])
    # #152: the GitHub and Beakon signals come from the cache the server's
    # refresh thread fills; this function runs once a second and never
    # fetches anything itself.
    cached = signals.get(root) if signals is not None else dict(signals_module.EMPTY)
    # #393: a build hands in the facts it read (config once, snapshot from
    # the cache) and the owner it probed alongside the others.
    facts = facts if facts is not None else project_facts(root)
    owner = _owner_view(root) if owner is _PROBE else owner
    cfg = facts["cfg"]
    logo_url = None
    if root.exists() and cfg is not None:
        try:
            logo_url = f"/project-logo/{logo_key(root)}" if lib.project_logo(root, cfg) else None
        except (lib.HandsoffError, OSError):
            logo_url = None
    github = cached.get("github") if isinstance(cached.get("github"), dict) else {}
    name = card_name(root, facts["configured_name"], github.get("repo"))
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
    snap = facts["snap"]
    if not snap.get("initialized"):
        seed = {"root": str(root), "error": snap.get("error"), "owner": owner}
        binding = hashlib.sha256(json.dumps(seed, sort_keys=True).encode()).hexdigest()[:20]
        return {"root": str(root), "name": name, "folder": root.name,  # #389
                "registered_at": entry["registered_at"], "logo_url": logo_url, "host_working_age_seconds": None,
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
    # #389: no managed session is live, but a host that wrote the ledger
    # within the silence window is working; quiet means nothing is happening.
    host_age = host_working_age(root, cfg, status) if state == "quiet" else None
    if host_age is not None:
        state = "host_working"
    role = snap.get("actors", {}).get("active_role")
    session = (snap.get("runtime", {}).get("current_sessions") or {}).get(role) if role else None
    decisions = [item for item in (snap.get("operator_actions") or [])
                 if item.get("kind") not in {"run_close", "run_reopen", "pause"}]
    seed = {"root": str(root), "updated_at": status.get("updated_at"),
            "owner": owner.get("run_token") if owner else None,
            "closed": status.get("run_closed"), "actions": [item.get("action_id") for item in decisions]}
    binding = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return {
        "root": str(root), "name": name, "folder": root.name,  # #389
        "registered_at": entry["registered_at"], "initialized": True,
        "logo_url": logo_url, "host_working_age_seconds": host_age,
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


#: #373: the engine panel lists at most this many projects.
ENGINE_PANEL_PROJECTS = 64


def installed_engine_facts() -> dict:
    """#373: what `handsoff version --json` computes for this engine (its
    version and manifest digest), plus whether the manifest is in step;
    'unknown' and 'unreadable' when the manifest cannot be read."""
    import handsoff_cli  # deferred: handsoff_cli imports this module
    try:
        identity = handsoff_cli._current_identity()
    except (OSError, ValueError, KeyError, TypeError):
        return {"installed": "unknown", "manifest": None, "manifest_status": "unreadable"}
    stale = lib.stale_manifest_refusal(lib.engine_root())
    return {"installed": str(identity["version"]), "manifest": identity.get("manifest_sha256"),
            "manifest_status": "stale" if stale else "current"}


def upgrade_command(root: str | Path, pin: str, installed: str) -> str | None:
    """#373: the exact command that moves `root`'s pin onto the installed
    engine, or None when the pin already accepts it. A wildcard pin moves to
    the installed line (major.minor.*); an exact pin moves to v<installed>."""
    if lib.version_satisfies(installed, pin):
        return None
    major, minor, _patch = lib._version_tuple(installed)
    target = f"{major}.{minor}.*" if "*" in pin else f"v{major}.{minor}.{_patch}"
    return f"handsoff upgrade {shlex.quote(str(root))} --to {target}"


def _project_pin(root: Path) -> str | None:
    try:
        return (root / lib.VERSION_PIN_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def engine_block(path: Path | None = None, issues: "signals_module.IssueCache | None" = None, *,
                 facts: dict | None = None) -> dict:
    """#373: the engine panel's data: the installed engine (version and
    manifest, as `handsoff version --json` reads them), the newest engine
    release in the Fleet issue cache, whether the install is behind it, and
    per registered project its pin, whether the installed engine satisfies
    it and, when not, the exact upgrade command."""
    facts = facts if facts is not None else installed_engine_facts()
    installed = facts["installed"]
    try:
        current = lib._version_tuple(installed)
    except lib.HandsoffError:
        current = None
    latest = signals_module.latest_engine_release(issues)
    behind = bool(latest and current and lib._version_tuple(latest["tag"]) > current)
    projects = []
    for entry in load_registry(path)[:ENGINE_PANEL_PROJECTS]:
        root = Path(entry["root"])
        pin = _project_pin(root)
        satisfied, upgrade = None, None
        if pin is not None and current is not None:
            try:
                satisfied = lib.version_satisfies(installed, pin)
                upgrade = None if satisfied else upgrade_command(root, pin, installed)
            except lib.HandsoffError:
                satisfied = False  # an unreadable pin refuses every engine
        projects.append({"root": str(root), "pin": pin, "satisfied": satisfied, "upgrade": upgrade})
    return {**facts, "latest": latest, "behind": behind, "projects": projects}


def _probe_owners(roots: list[Path]) -> list[dict | None]:
    """#393: every registered root's owner probe at once (each is a loopback
    request with its own short timeout), in registry order."""
    if not roots:
        return []
    with ThreadPoolExecutor(max_workers=min(OWNER_PROBE_WORKERS, len(roots))) as pool:
        return list(pool.map(_owner_view, roots))


def _build_fleet(path: Path | None = None, public_base: str | None = None,
                 signals: "signals_module.SignalCache | None" = None,
                 issues: "signals_module.IssueCache | None" = None,
                 cache: ProjectViewCache | None = None) -> dict:
    cache = VIEW_CACHE if cache is None else cache
    forget_missing_roots(path)  # #207: a vanished root is ORPHANED for one pass, then gone
    entries = load_registry(path)
    roots = [Path(entry["root"]) for entry in entries]
    owners = _probe_owners(roots)
    # #393: one config read per project per build, reused by every reader below.
    facts = [project_facts(root, project_config(root, cache), cache) for root in roots]
    cache.retain({str(root) for root in roots})
    projects = [_public_dashboard_url(project_view(entry, signals, facts=fact, owner=owner), public_base)
                for entry, fact, owner in zip(entries, facts, owners)]
    states = {entry["root"]: fact["state"] for entry, fact in zip(entries, facts)}
    try:
        sync_registry_states(states, path)  # #432: a reopened run reads open on the next refresh
    except (lib.HandsoffError, OSError):
        pass  # the cards read status directly; the stored record catches up next build
    # #166: a ticket two live open runs both list is shown in red on both cards.
    try:
        twice = claimed_twice(path, states)
    except lib.HandsoffError:
        twice = {}
    for item in projects:
        item["claimed_twice"] = twice.get(item["root"], [])
    decisions = [{"root": item["root"], "project": item["name"], "feature": item.get("feature"), **action}
                 for item in projects for action in item.get("decisions", [])]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "projects": projects,
            "engine": {**FLEET_ENGINE, "install_blocked": install_blocked(path, states),  # #161, #216
                       **engine_block(path, issues)},  # #373
            "decisions": decisions, "counts": {state: sum(item["state"] == state for item in projects)
                                                 for state in ("running", "host_working", "quiet", "waiting", "stalled", "failed", "offline", "complete", "closed", "idle", "orphaned")}}


class SingleFlight:
    """#393: concurrent callers with one key share one in-flight call; the
    first runs it, the rest wait for its result (or its exception)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: dict = {}

    def do(self, key, function):
        with self.lock:
            call = self.calls.get(key)
            leader = call is None
            if leader:
                call = self.calls[key] = {"done": threading.Event(), "result": None, "error": None, "waiting": 0}
            else:
                call["waiting"] += 1
        if leader:
            try:
                call["result"] = function()
            except BaseException as exc:  # noqa: BLE001 - handed to every waiter as is
                call["error"] = exc
            finally:
                with self.lock:
                    self.calls.pop(key, None)
                call["done"].set()
        else:
            call["done"].wait()
        if call["error"] is not None:
            raise call["error"]
        return call["result"]

    def waiting(self, key) -> int:
        with self.lock:
            call = self.calls.get(key)
            return call["waiting"] if call else 0


FLEET_BUILDS = SingleFlight()


def build_fleet(path: Path | None = None, public_base: str | None = None,
                signals: "signals_module.SignalCache | None" = None,
                issues: "signals_module.IssueCache | None" = None) -> dict:
    """The /api/fleet payload. #393: single-flight, so concurrent requests
    for the same view share one build; each project's view comes from the
    signature cache when nothing it is built from has changed."""
    key = (str(path), public_base, id(signals), id(issues))
    return FLEET_BUILDS.do(key, lambda: _build_fleet(path, public_base, signals, issues))


def _rediscovery_ledger_state(status: dict) -> str:
    if isinstance(status.get("run_closed"), dict) or status.get("status") in ("closed", "complete"):
        return "completed"
    value = status.get("status")
    if value in ("active", "in_progress", "paused", "blocked", "cancelled", "failed"):
        return value
    return "paused" if str(value or "").startswith("paused") else "active"


def _rediscovery_time(*values) -> str:
    for value in values:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is not None:
            return parsed.isoformat()
    return datetime.now(timezone.utc).isoformat()


#: #384: the registry's own run states, as rediscovery reads them.
REGISTRY_REDISCOVERY_STATES = {"open": "active", "closed": "completed", "complete": "completed"}


def rediscovery_inputs(path: Path | None = None) -> dict:
    """#384: make_rediscovery_inputs from the Fleet registry, each registered
    root's ledger (status and event log) and its runtime-control monitor
    record. Reads only; a root with no run, or a monitor record that does
    not validate, contributes nothing."""
    import handsoff_runtime_control as runtime_control
    import handsoff_supervisor as supervisor  # deferred: the run id is the supervisor's (#383 lane A)
    fleet_runs, ledger_runs, monitors = [], [], []
    for entry in load_registry(path):
        root = Path(entry["root"])
        state = _run_state(root)
        status, cfg = state.get("status"), state.get("cfg")
        if not isinstance(status, dict) or cfg is None:
            continue
        events = lib.read_events(root, cfg)
        run_id = supervisor._runtime_run_id(root, events)
        stored = state["state"] if state["state"] in REGISTRY_DERIVED_STATES else entry.get("state")  # #432
        registry_state = REGISTRY_REDISCOVERY_STATES.get(stored)
        if registry_state:
            fleet_runs.append({"run_id": run_id, "root": entry["root"], "state": registry_state, "cursor": 0,
                               "updated_at": _rediscovery_time(entry.get("updated_at"), entry.get("registered_at"))})
        ledger_runs.append({"run_id": run_id, "root": entry["root"], "state": _rediscovery_ledger_state(status),
                            "cursor": len(events), "updated_at": _rediscovery_time(status.get("updated_at"))})
        try:
            monitor = json.loads((root / RUNTIME_CONTROL_DIR / "monitor.json").read_text(encoding="utf-8"))
            runtime_control.validate_monitor(monitor)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        monitors.append(monitor)
    return runtime_control.make_rediscovery_inputs(fleet_runs, ledger_runs, monitors)


def rediscover(path: Path | None = None) -> list[dict]:
    """#384: the runs whose monitoring must resume, from rediscover_active_runs.
    Writes no file."""
    import handsoff_runtime_control as runtime_control
    return runtime_control.rediscover_active_runs(rediscovery_inputs(path))


def queue_listing(store: dict) -> dict:
    """#386: a queue store as the CLI and /api/queue print it: last_seq and
    every job in submission order. `list` and `replay` share this shape."""
    jobs = sorted(store["jobs"].values(), key=lambda job: (job["created_at"], job["id"]))
    return {"last_seq": store["last_seq"], "jobs": jobs}


def read_queue(base_dir: Path | None = None) -> dict:
    """#386: the queue's listing, read-only: a queue never written to reads
    empty instead of creating its directory."""
    queue = handsoff_queue.Queue(base_dir)
    if not queue.store_path.exists() and not queue.journal_dir.exists():
        return queue_listing(handsoff_queue.empty_store())
    return queue_listing(queue.load())


class FleetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, registry: Path | None = None, *, signals=None, signals_interval=None,
                 issues=None, issues_interval=None, token: str | None = None):
        self.registry = registry
        self.token = token  # #285: when set, every /api request needs this Bearer value
        self.stopping = False
        self.started_at = datetime.now(timezone.utc).isoformat()
        # #152: one signal cache for the server's lifetime, filled by a daemon
        # thread that starts now and returns control before its first pass.
        # #153: the issue cache behind the Metrics tab, same shape, its own
        # slower clock (default 900 s; the lists are larger).
        self.signals = signals if signals is not None else signals_module.SignalCache(registry=registry or registry_path())
        self.issues = issues if issues is not None else signals_module.IssueCache(registry=registry or registry_path())
        # #384: the runs to resume monitoring, found once as the server starts
        # (after a reboot nothing else would) and served on /api/fleet.
        try:
            self.rediscovered, self.rediscovery_error = rediscover(registry), None
        except Exception as exc:  # a broken record must not stop Fleet from serving
            self.rediscovered, self.rediscovery_error = [], f"{type(exc).__name__}: {exc}"
        _fleet_log(f"fleet_rediscovered runs={len(self.rediscovered)}"
                   f"{' error=' + self.rediscovery_error if self.rediscovery_error else ''}", registry)
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

    def _require_token(self, path) -> bool:
        """#285: with a token configured, an /api request without the matching
        Bearer value is answered 401 here, before any Origin check, body read
        or mutation. True when the request may proceed."""
        token = self.server.token
        if not token or not (path == "/api" or path.startswith("/api/")):
            return True
        scheme, _, presented = (self.headers.get("Authorization") or "").strip().partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(presented.strip().encode(), token.encode()):
            return True
        self.close_connection = True
        body = json.dumps({"error": "Fleet access token required"}, separators=(",", ":")).encode()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", 'Bearer realm="handsoff-fleet"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        return False

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
        if not self._require_token(path):
            return
        if path == "/api/fleet":
            payload = build_fleet(self.server.registry, public_base=self._public_base(),
                                  signals=self.server.signals, issues=self.server.issues)
            self._json(HTTPStatus.OK, {**payload, "rediscovered": self.server.rediscovered,  # #384
                                       "rediscovery_error": self.server.rediscovery_error})
            return
        if path == "/api/queue":
            # #386: the durable queue, read-only, behind the same token check.
            try:
                listing = read_queue((self.server.registry or registry_path()).parent)
            except handsoff_queue.QueueError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, listing)
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
                    payload = build_fleet(self.server.registry, signals=self.server.signals, issues=self.server.issues)
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
        if path == "/lib/run-vocabulary.js":
            # #218: the run vocabulary is the engine's dashboard/lib file, served here too
            try:
                body = lib.engine_resource_path("dashboard/lib/run-vocabulary.js").read_bytes()
            except OSError:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "run vocabulary is missing"})
                return
            self._headers(HTTPStatus.OK, "text/javascript; charset=utf-8", len(body))
            self.wfile.write(body)
            return
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
        if not self._require_token(path):
            return
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
            # #393: a mutation checks its binding against a build of its own,
            # never one that started before the request arrived.
            project = next((item for item in _build_fleet(self.server.registry, signals=self.server.signals)["projects"]
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


def serve(host="127.0.0.1", port=8765, *, open_browser=True, registry=None, token_file=None):
    token = fleet_token(token_file)
    # #285: a non-loopback bind is refused unless an access token is configured.
    if host not in lib.DASHBOARD_LOOPBACK_HOSTS and not token:
        raise lib.HandsoffError(f"Fleet Mission Control binds to localhost only unless an access token is "
                                f"configured ({FLEET_TOKEN_ENV} or --token-file)")
    server = FleetServer((host, port), registry, token=token)
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


def queue_command(args) -> int:
    """#386: one queue operation on the Queue at its default directory,
    printed as JSON. A refusal (a stale epoch, an unknown job) raises a
    QueueError, which main reports as HANDSOFF_FLEET_BLOCKED."""
    queue = handsoff_queue.Queue()
    command = args.queue_command
    if command == "submit":
        try:
            payload = json.loads(args.payload)
        except ValueError as exc:
            raise handsoff_queue.QueueError(f"--payload is not JSON: {exc}") from exc
        result = {"job_id": queue.submit(args.key, payload, max_attempts=args.max_attempts)}
    elif command == "list":
        result = read_queue()
    elif command == "show":
        result = read_queue()
        result = next((job for job in result["jobs"] if job["id"] == args.job), None)
        if result is None:
            raise handsoff_queue.QueueError(f"unknown job {args.job}")
    elif command == "claim":
        result = queue.claim(args.worker, lease_seconds=args.lease_seconds)
    elif command == "heartbeat":
        result = queue.heartbeat(args.job, args.worker, args.epoch, lease_seconds=args.lease_seconds)
    elif command == "succeed":
        result = queue.succeed(args.job, args.worker, args.epoch)
    elif command == "fail":
        result = queue.fail(args.job, args.worker, args.epoch)
    elif command == "cancel":
        result = queue.cancel(args.job)
    else:
        result = queue_listing(queue.replay(from_start=args.from_start))
    print(json.dumps(result, indent=2, sort_keys=True))
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
    server.add_argument("--token-file", help=f"file holding the Fleet access token (else {FLEET_TOKEN_ENV})")
    sub.add_parser("rediscover", help="print the runs whose monitoring must resume; writes nothing (#384)")
    queue = sub.add_parser("queue", help="the durable Fleet job queue (#386)")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    submit = queue_sub.add_parser("submit")
    submit.add_argument("--key", required=True, help="idempotency key; a known key returns its job")
    submit.add_argument("--payload", default="{}", help="the job's JSON object")
    submit.add_argument("--max-attempts", type=int, default=handsoff_queue.DEFAULT_MAX_ATTEMPTS)
    queue_sub.add_parser("list")
    show = queue_sub.add_parser("show")
    show.add_argument("job")
    claim = queue_sub.add_parser("claim")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=int, default=handsoff_queue.DEFAULT_LEASE_SECONDS)
    for name in ("heartbeat", "succeed", "fail"):
        owner = queue_sub.add_parser(name)
        owner.add_argument("job")
        owner.add_argument("--worker", required=True)
        owner.add_argument("--epoch", type=int, required=True, help="the epoch claim returned")
        if name == "heartbeat":
            owner.add_argument("--lease-seconds", type=int, default=handsoff_queue.DEFAULT_LEASE_SECONDS)
    cancel = queue_sub.add_parser("cancel")
    cancel.add_argument("job")
    replay = queue_sub.add_parser("replay")
    replay.add_argument("--from-start", action="store_true", help="replay from segment 1, not the latest checkpoint")
    args = parser.parse_args()
    try:
        if args.command == "rediscover":
            print(json.dumps(rediscover(), indent=2, sort_keys=True))
            return 0
        if args.command == "queue":
            return queue_command(args)
        if args.command == "register":
            print(json.dumps(register_project(Path(args.root)), sort_keys=True))
            return 0
        if args.command == "unregister":
            print("UNREGISTERED" if unregister_project(Path(args.root)) else "NOT_REGISTERED")
            return 0
        return serve(args.host, args.port, open_browser=not args.no_open, token_file=args.token_file)
    except lib.HandsoffError as exc:
        print(f"HANDSOFF_FLEET_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
