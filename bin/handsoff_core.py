#!/usr/bin/env python3
"""Handsoff core: the primitives every other subsystem calls.

#284 stage 1. Thirteen symbols with **zero** outbound dependencies on the
rest of the engine, derived from the reference graph rather than chosen:
the error type (121 inbound callers), canonical JSON form, atomic and
durable writes, the project lock, the status and acceptance paths, and the
three content hashes that bind evidence.

This is the bottom layer. It imports nothing from Handsoff, so anything
may import it at module level without a cycle, which is what lets the
subsystems above it drop the deferred-import workaround that the first
extraction (model routing) had to use.

Deliberately **no configuration**. `DEFAULT_CONFIG` and `load_config` are
not here: `DEFAULT_CONFIG` embeds the adaptive routing defaults, so a core
that owned it would depend on routing, and routing depends on this. Config
therefore sits ABOVE routing, not below it. That interlock is why the
first attempt at a 93-symbol core did not close, and measuring it is what
produced this layering.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# The monolith guarded this and so must the core: `project_lock` degrades to a
# documented no-op where fcntl is absent (docs/REFERENCE.md, "Known
# limitations"). A bare import would make every module that imports the core
# die on import instead, and would leave the `if fcntl is None` branch below
# as dead code claiming a graceful degradation it no longer performs.
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None


class HandsoffError(Exception):
    """A config or state file problem that stops us before any gate logic
    runs, distinct from a gate simply refusing a transition."""


def _atomic_write_text(path: Path, text: str) -> None:
    """Durably replace a UTF-8 text file using the shared state primitive."""
    durable_replace(path, text.encode("utf-8"))


def status_path(root: Path, cfg: dict) -> Path:
    return root / cfg["status_file"]


def acceptance_path(root: Path, cfg: dict) -> Path:
    return root / cfg["acceptance_file"]


def lock_path(root: Path) -> Path:
    return root / ".handsoff.lock"


@contextmanager
def project_lock(root: Path):
    """Advisory single-writer lock around a read-modify-write. Best effort:
    on a platform without fcntl this is a no-op, which is a known
    limitation (see README), not a silent claim of safety it cannot keep."""
    if fcntl is None:
        yield
        return
    lp = lock_path(root)
    lp.touch(exist_ok=True)
    with lp.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_unique_json(path: Path) -> dict:
    """Parse JSON, rejecting a duplicate top-level-or-nested key rather than
    silently keeping the last one, the way plain json.loads would."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise HandsoffError(f"duplicate JSON key '{key}' in {path}")
            out[key] = value
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HandsoffError(f"cannot read {path}: {e}") from e
    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as e:
        raise HandsoffError(f"invalid JSON in {path}: {e}") from e


def durable_backup_path(path: Path) -> Path:
    """One bounded last-known-good copy; a newer write replaces the old copy."""
    return Path(path).with_name(f"{Path(path).name}.bak")


def durable_replace(path: Path, payload: bytes, *, fault=None, keep_backup: bool = True) -> dict:
    """Flush, atomically replace, and sync one durable record.

    ``fault(stage)`` is a test-only interruption hook.  At every boundary the
    target is either its prior complete bytes or the complete new payload.
    This is filesystem durability, not a database transaction spanning files.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with tmp.open("wb") as handle:
            handle.write(bytes(payload))
            if fault:
                fault("before_flush")
            handle.flush()
            os.fsync(handle.fileno())
            if fault:
                fault("after_flush")
        try:
            os.chmod(tmp, path.stat().st_mode)
        except OSError:
            pass
        if keep_backup and path.is_file():
            previous = path.read_bytes()
            durable_replace(durable_backup_path(path), previous, keep_backup=False)
        os.replace(tmp, path)
        if fault:
            fault("after_replace")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path.parent, flags)
            try:
                if fault:
                    fault("during_directory_sync")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            capability = {"level": "full", "file_fsync": True,
                          "directory_fsync": True, "reason": None}
        except OSError as exc:
            capability = {"level": "best_effort", "file_fsync": True,
                          "directory_fsync": False,
                          "reason": f"directory fsync unavailable: {type(exc).__name__}"}
        return capability
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def criterion_spec_hash(criterion: dict) -> str:
    """Hash the claim being verified, excluding mutable outcome fields and
    authored_by -- the last is provenance metadata about who proposed the
    criterion, not part of the claim being verified, so stamping it at
    design-approve time can never change this hash, invalidate an
    already-recorded evidence binding, or mismatch a freshly recomputed
    design_hash (which is built from this same hash per criterion)."""
    spec = {k: v for k, v in criterion.items() if k not in {"state", "evidence", "authored_by"}}
    return hashlib.sha256(_canonical(spec).encode("utf-8")).hexdigest()


def acceptance_hash(criteria: list[dict]) -> str:
    """Binds a deployment approval to the exact criteria states it was
    given against. If the registry changes afterward (a criterion flips,
    one is added or removed), this changes too, and the Phase 8 gate
    refuses the now-stale approval rather than honoring it blindly.

    Sorted by id first: `_canonical` sorts each dict's own keys but not
    list order, so a harmless reordering of the criteria array (a
    re-save, a merge) would otherwise change the hash and falsely
    invalidate a still-valid approval. Sorting fails safe either way,
    over-blocking rather than under-blocking, but there is no reason to
    pay for it when the content genuinely has not changed."""
    ordered = sorted(criteria, key=lambda c: c.get("id") or "")
    return hashlib.sha256(_canonical({"criteria": ordered}).encode("utf-8")).hexdigest()


def design_hash(criteria: list[dict]) -> str:
    """Binds a design approval to the SPEC of each criterion (id, type,
    requirement, verification, tests), never its evidence state --
    unlike acceptance_hash, which deliberately includes state/evidence so
    a deployment/review approval notices new or changed evidence. A
    design approval is given before implementation exists; if it were
    bound to acceptance_hash, the very first `verify` call (which flips
    a criterion's state) would invalidate it, forcing re-approval for
    every criterion the moment it is first evidenced. Adding, removing,
    or respecifying a criterion still invalidates it; recording evidence
    about one that already exists does not."""
    ordered = sorted(criteria, key=lambda c: c.get("id") or "")
    return hashlib.sha256(_canonical(
        {"criteria": [{"id": c.get("id"), "spec": criterion_spec_hash(c)} for c in ordered]}
    ).encode("utf-8")).hexdigest()
