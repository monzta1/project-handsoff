#!/usr/bin/env python3
"""Handsoff engine resources: where the installed engine's files are, and the briefing built from them.

#284 stage 4. Fourteen symbols covering the engine root, resource paths,
the playbook index and the briefing assembled for a managed launch, plus
the repository and preflight snapshots that describe the installed tree.

**Named for what the graph produced, not for what was looked for.** Seeding
"snapshot" and "briefing" to find dashboard projection returned this group
instead: resource location and briefing assembly, which is a real concern
and not the dashboard. Calling it a projection layer would have put the
wrong name on a correct boundary. Dashboard projection is still inside the
monolith.

`engine_root` resolves from this module's own `__file__` and therefore
still points at `bin/`, exactly as it did from the monolith; the
resource-path tests assert that rather than assume it.

Layer: `handsoff_core` -> `handsoff_ledger` -> here. The preflight
filename belongs to the ledger, which owns the run's on-disk files.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sysconfig
from pathlib import Path

from handsoff_core import HandsoffError, load_unique_json
from handsoff_ledger import PREFLIGHT_FILE



PREFLIGHT_SCHEMA = 2


MAX_PREFLIGHT_ENTRIES = 16


# #175: managed-session knowledge briefings are bounded so a project cannot
# turn an indexed file into an unbounded launch prompt.
MAX_BRIEFING_FILE_BYTES = 64 * 1024


MAX_BRIEFING_TOTAL_BYTES = 256 * 1024


RUNTIME_MANIFEST_FILE = "handsoff-runtime.json"


def engine_root() -> Path:
    """Locate this engine's immutable resources in a checkout or installation."""
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / RUNTIME_MANIFEST_FILE).is_file() and (checkout / "dashboard").is_dir():
        return checkout
    return Path(sysconfig.get_path("data")) / "share" / "handsoff"


def engine_resource_path(relative: str) -> Path:
    if not isinstance(relative, str) or relative.startswith(("/", "../")):
        raise HandsoffError("engine resource path is invalid")
    root = engine_root()
    if relative.startswith("bin/") and not (root / relative).exists():
        return Path(__file__).resolve().parent / Path(relative).name
    return root / relative


PLAYBOOK_DIR = "playbook"


PLAYBOOK_INDEX = "index.json"


def playbook_root() -> Path:
    """The engine's own playbook: the checkout's copy in a drop-in, else the
    installed engine's (#208). Engine knowledge, shipped with the engine."""
    return engine_resource_path(PLAYBOOK_DIR)


def playbook_index() -> dict:
    path = playbook_root() / PLAYBOOK_INDEX
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"the engine playbook index is missing or unreadable: {path}; reinstall the Handsoff engine") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("topics"), dict):
        raise HandsoffError("the engine playbook index must be a version 1 object with topics")
    return manifest


def briefing_section(root: Path, cfg: dict, topic: str | None = None) -> str:
    """Resolve the bounded knowledge briefing for one managed launch.

    A missing [briefing] block is deliberately a no-op. When configured, the
    index is authoritative: always_load files are included first, followed by
    files whose declared topics match the optional one-launch topic. Every
    selected path must remain inside the project root and exist as a regular
    file before a managed session can be reserved.
    """
    briefing = cfg.get("briefing")
    if briefing is None:
        if topic is not None and topic not in playbook_index()["topics"]:
            raise HandsoffError("--topic requires a [briefing] block in handsoff.toml, or a playbook topic")
        return ""
    if topic is not None and (not isinstance(topic, str) or not topic.strip()):
        raise HandsoffError("--topic must be a non-empty topic name")
    root = Path(root).resolve()
    index_path = (root / briefing["index"]).resolve()
    if not index_path.is_file():
        raise HandsoffError(f"briefing index is missing: {index_path}")
    kb_root = (root / briefing.get("root", "")).resolve() if briefing.get("root") else index_path.parent
    try:
        index_path.relative_to(root)
        kb_root.relative_to(root)
    except ValueError as exc:
        raise HandsoffError("briefing paths must remain inside the project root") from exc
    try:
        manifest = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"cannot read briefing index {index_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise HandsoffError("briefing index must be a version 1 object")
    topics = manifest.get("topics")
    always_load = manifest.get("always_load")
    files = manifest.get("files")
    if not isinstance(topics, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in topics.items()):
        raise HandsoffError("briefing index topics must map strings to strings")
    if not isinstance(always_load, list) or not all(isinstance(item, str) and item.strip() for item in always_load):
        raise HandsoffError("briefing index always_load must be a list of file names")
    if not isinstance(files, list):
        raise HandsoffError("briefing index files must be a list")
    declared = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"].strip():
            raise HandsoffError("briefing index files must contain a file name")
        item_topics = item.get("topics", [])
        if not isinstance(item_topics, list) or not all(isinstance(value, str) and value in topics for value in item_topics):
            raise HandsoffError(f"briefing index topics are invalid for {item['file']}")
        declared[item["file"]] = tuple(item_topics)
    selected = list(always_load)
    if topic is not None:
        topic = topic.strip()
        # A topic is looked up in both indexes (#208): a playbook topic rides
        # from the playbook, a project topic from the project's KB, and a name
        # declared in both rides from both. Only a name in neither is refused.
        if topic not in topics:
            if topic in playbook_index()["topics"]:
                topic = None  # carried by playbook_section
            else:
                raise HandsoffError(f"briefing topic is not declared in the index: {topic}")
        else:
            selected.extend(name for name, item_topics in declared.items() if topic in item_topics)
    unique = []
    for name in selected:
        if name not in unique:
            unique.append(name)
    sections = []
    total = 0
    for name in unique:
        relative = Path(name)
        if relative.is_absolute():
            raise HandsoffError(f"briefing file must be relative: {name}")
        path = (kb_root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HandsoffError(f"briefing file escapes the project root: {name}") from exc
        if not path.is_file():
            raise HandsoffError(f"briefing file is missing: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise HandsoffError(f"cannot read briefing file {path}: {exc}") from exc
        if len(data) > MAX_BRIEFING_FILE_BYTES:
            raise HandsoffError(f"briefing file is larger than {MAX_BRIEFING_FILE_BYTES} bytes: {path}")
        total += len(data)
        if total > MAX_BRIEFING_TOTAL_BYTES:
            raise HandsoffError(f"briefing exceeds {MAX_BRIEFING_TOTAL_BYTES} bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandsoffError(f"briefing file is not UTF-8: {path}") from exc
        sections.append(f"## {name}\n\n{text.rstrip()}")
    if not sections:
        raise HandsoffError("briefing index selected no files")
    return "# Knowledge base briefing\n\n" + "\n\n".join(sections)


def launch_preflight_snapshot(root: Path) -> dict:
    """Bounded, content-free Mission Control view of exact-launch readiness."""
    path = Path(root) / PREFLIGHT_FILE
    try:
        store = load_unique_json(path)
    except HandsoffError:
        return {"state": "not_checked", "incident": None, "entries": [], "avoided_retries": 0}
    if not isinstance(store, dict) or store.get("schema") != PREFLIGHT_SCHEMA:
        return {"state": "legacy", "incident": None, "entries": [], "avoided_retries": 0}
    safe_fields = ("fingerprint", "adapter", "model", "state", "category", "reason",
                   "checked_at", "expires_at", "provider_probe_calls", "coalesced_count")
    entries = [{field: item.get(field) for field in safe_fields}
               for item in (store.get("entries") or {}).values() if isinstance(item, dict)]
    entries.sort(key=lambda item: item.get("checked_at") or "", reverse=True)
    incident = store.get("incident") if isinstance(store.get("incident"), dict) else None
    incident_view = ({field: incident.get(field) for field in safe_fields} if incident else None)
    return {"state": "blocked" if incident else (entries[0]["state"] if entries else "not_checked"),
            "incident": incident_view, "entries": entries[:MAX_PREFLIGHT_ENTRIES],
            "avoided_retries": sum(int(item.get("coalesced_count") or 0) for item in entries)}


def repository_snapshot(root: Path, *, runner=subprocess.run) -> dict:
    """Return bounded exact git identity without retaining porcelain text."""
    def git(*args: str) -> str:
        try:
            result = runner(
                ["git", *args], cwd=str(root.resolve()), shell=False, text=True,
                capture_output=True, timeout=3, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            if "unborn branch" in stderr.lower() or "ambiguous argument 'head'" in stderr.lower():
                raise HandsoffError("non-git root") from None
            raise HandsoffError(f"cannot establish repository identity: {type(exc).__name__}") from exc
        return result.stdout
    head = git("rev-parse", "HEAD").strip()
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").strip().split()
    branch = git("branch", "--show-current").strip() or "(detached)"
    porcelain = git("status", "--porcelain=v1")
    tracked_diff = git("diff", "--binary", "HEAD")
    untracked = [line for line in git("ls-files", "--others", "--exclude-standard").splitlines() if line]
    content = bytearray(tracked_diff.encode("utf-8", "replace"))
    for relative in sorted(untracked):
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
            if path.is_file():
                content.extend(relative.encode("utf-8", "replace") + b"\0" + path.read_bytes())
        except (OSError, ValueError):
            raise HandsoffError("cannot establish repository content identity") from None
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head) or len(branch) > 256:
        raise HandsoffError("cannot establish bounded repository identity")
    # Bind the reviewed change range, not merely HEAD's immediate parent.
    # Prefer the remote's declared default branch, then conventional local
    # names. Repositories without one retain the safe parent fallback.
    base = None
    candidates = []
    try:
        symbolic = runner(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=str(root.resolve()), shell=False, text=True, capture_output=True,
            timeout=3, check=False,
        )
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            candidates.append(symbolic.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.extend(["refs/remotes/origin/main", "main", "master"])
    for candidate in dict.fromkeys(candidates):
        try:
            merged = runner(
                ["git", "merge-base", "HEAD", candidate], cwd=str(root.resolve()),
                shell=False, text=True, capture_output=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if merged.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", merged.stdout.strip()):
            base = merged.stdout.strip().lower()
            break
    remote = ""
    try:
        remote_result = runner(["git", "remote", "get-url", "origin"], cwd=str(root.resolve()),
                               shell=False, text=True, capture_output=True, timeout=3, check=False)
        if remote_result.returncode == 0:
            remote = remote_result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "remote": remote,
        "path": str(root.resolve()), "head": head.lower(), "branch": branch, "dirty": bool(porcelain),
        "status_sha256": hashlib.sha256(porcelain.encode("utf-8", "replace")).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "commit_pair": {"before": base or (parents[1].lower() if len(parents) > 1 else head.lower()),
                        "after": head.lower()},
    }
