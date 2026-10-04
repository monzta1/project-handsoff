#!/usr/bin/env python3
"""Durable Fleet job queue: a locked JSON store and an append-only journal (#285).

The store, `fleet-queue.json` beside the Fleet registry, holds the current
state of every job. The journal, `fleet-queue.journal/NNNNNN.jsonl`, holds
every transition and every heartbeat as one hash-chained line, and is the
authority: each mutation takes one exclusive lock, appends and fsyncs its
journal line, and only then rewrites the store. A crash between the two is
healed on the next load by applying the lines past the store's `last_seq`.

Only a demonstrably incomplete tail is ever discarded: the active segment's
final line when it has no terminating newline and its seq is beyond the
store's `last_seq`, so it was never acknowledged. Any newline-terminated
line, or any line at or below `last_seq`, that fails to parse or to hash is
committed corruption, refused as `QueueCorrupt` by name and never truncated.

Leases carry a worker, an expiry and a per-job epoch (the fencing token,
incremented on every grant). Every lease check compares all three under the
same lock as the mutation it guards, with one clock read inside the lock, so
an expired owner is refused even before anyone reclaims the job.

The journal is never pruned. Past `segment_bytes` the active segment closes
with a checkpoint line, a `checkpoint-NNNNNN.json` snapshot is written, and
segment N+1 continues the chain. Old segments stay on disk; replay starts
from the latest checkpoint that verifies, and replay from segment 1 gives the
same store.

The queue has no remote transport, so a job naming another host is refused.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from handsoff_core import HandsoffError, _canonical
from handsoff_ledger import atomic_write_json

STORE_FILE = "fleet-queue.json"
JOURNAL_DIR = "fleet-queue.journal"
LOCK_FILE = "fleet-queue.lock"
STORE_VERSION = 1
GENESIS_HASH = "0" * 64
DEFAULT_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 20
MAX_JOBS = 10_000
MAX_PAYLOAD_BYTES = 16 * 1024
DEFAULT_SEGMENT_BYTES = 1024 * 1024

STATES = ("queued", "leased", "succeeded", "failed", "cancelled")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
OPS = ("submit", "claim", "heartbeat", "succeed", "fail", "expire", "cancel", "checkpoint")

JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{16}$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
WORKER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SEGMENT_PATTERN = re.compile(r"^(\d{6})\.jsonl$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class QueueError(HandsoffError):
    """A queue request refused as invalid for the current state."""


class QueueCorrupt(QueueError):
    """The store or journal fails verification; nothing was truncated."""


class LeaseRefused(QueueError):
    """A lease call refused: unknown job, not leased, or already terminal."""


class StaleLease(LeaseRefused):
    """A lease call from a worker or epoch that no longer holds the job."""


class LeaseExpired(LeaseRefused):
    """A lease call at or past the lease expiry, before or after a reclaim."""


def default_base_dir() -> Path:
    """The Fleet registry's directory, honouring HANDSOFF_FLEET_REGISTRY."""
    override = os.environ.get("HANDSOFF_FLEET_REGISTRY")
    registry = Path(override).expanduser().resolve() if override else Path.home() / ".handsoff" / "projects.json"
    return registry.parent


def local_host() -> str:
    return os.uname().nodename


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise QueueCorrupt(f"timestamp {text!r} has no timezone")
    return moment


def line_hash(body: dict, prev_hash: str) -> str:
    """sha256 of the canonical line body (everything but `hash`) plus prev_hash."""
    return hashlib.sha256((_canonical(body) + prev_hash).encode("utf-8")).hexdigest()


def snapshot_hash(store: dict) -> str:
    return hashlib.sha256(_canonical(store).encode("utf-8")).hexdigest()


def empty_store() -> dict:
    return {"version": STORE_VERSION, "last_seq": 0, "jobs": {}}


def _segment_name(number: int) -> str:
    return f"{number:06d}.jsonl"


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_line(store: dict, line: dict) -> None:
    """Apply one verified journal line to a store. Replay and recovery both
    go through here, so the store a mutation writes is the store replay
    reproduces."""
    op, job_id, at, fields = line["op"], line["job"], line["at"], line["fields"]
    jobs = store["jobs"]
    if op == "checkpoint":
        store["last_seq"] = line["seq"]
        return
    if op == "submit":
        if job_id in jobs:
            raise QueueCorrupt(f"journal seq {line['seq']} resubmits {job_id}")
        jobs[job_id] = {
            "id": job_id, "key": fields["key"], "host": fields["host"], "payload": fields["payload"],
            "state": "queued", "attempts": 0, "max_attempts": fields["max_attempts"], "epoch": 0,
            "lease": None, "created_at": at, "updated_at": at,
        }
    else:
        job = jobs.get(job_id)
        if job is None:
            raise QueueCorrupt(f"journal seq {line['seq']} names unknown job {job_id}")
        if op == "claim":
            job["state"] = "leased"
            job["epoch"] = line["epoch"]
            job["lease"] = {"worker": line["worker"], "expires_at": fields["expires_at"]}
        elif op == "heartbeat":
            if not job["lease"]:
                raise QueueCorrupt(f"journal seq {line['seq']} heartbeats unleased {job_id}")
            job["lease"]["expires_at"] = fields["expires_at"]
        elif op in ("fail", "expire"):
            job["attempts"] = fields["attempts"]
            job["state"] = fields["state"]
            job["lease"] = None
        elif op == "succeed":
            job["state"] = "succeeded"
            job["lease"] = None
        elif op == "cancel":
            job["state"] = "cancelled"
            job["lease"] = None
        else:
            raise QueueCorrupt(f"journal seq {line['seq']} has unknown op {op!r}")
        job["updated_at"] = at
    store["last_seq"] = line["seq"]


class Queue:
    """One host's durable job queue. `now` is injectable so expiry needs no
    sleep; `after_journal` is a fault-injection hook called after a line is
    fsynced and before the store is written."""

    def __init__(self, base_dir: Path | None = None, *, now=None, host: str | None = None,
                 segment_bytes: int = DEFAULT_SEGMENT_BYTES, after_journal=None):
        self.base_dir = Path(base_dir) if base_dir is not None else default_base_dir()
        self.store_path = self.base_dir / STORE_FILE
        self.journal_dir = self.base_dir / JOURNAL_DIR
        self.lock_path = self.base_dir / LOCK_FILE
        self.now = now or _utc_now
        self.host = host or local_host()
        self.segment_bytes = segment_bytes
        self.after_journal = after_journal

    # -- locking and loading ------------------------------------------------

    @contextmanager
    def _locked(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(exist_ok=True)
        with open(self.lock_path, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_store(self) -> dict:
        try:
            store = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_store()
        except (OSError, ValueError) as exc:
            raise QueueCorrupt(f"{STORE_FILE} is unreadable: {type(exc).__name__}") from exc
        if (not isinstance(store, dict) or store.get("version") != STORE_VERSION
                or not isinstance(store.get("last_seq"), int) or store["last_seq"] < 0
                or not isinstance(store.get("jobs"), dict)):
            raise QueueCorrupt(f"{STORE_FILE} does not have the version {STORE_VERSION} shape")
        return store

    def _segments(self) -> list[tuple[int, Path]]:
        found = []
        for path in self.journal_dir.iterdir() if self.journal_dir.is_dir() else []:
            match = SEGMENT_PATTERN.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
        found.sort()
        for index, (number, _path) in enumerate(found, start=1):
            if number != index:
                raise QueueCorrupt(f"journal segment {_segment_name(index)} is missing")
        return found

    def _discard_torn_tail(self, path: Path, data: bytes, cut: int, seq: int) -> None:
        target = self.journal_dir / f"torn-{seq}.bin"
        counter = 1
        while target.exists():
            target = self.journal_dir / f"torn-{seq}-{counter}.bin"
            counter += 1
        target.write_bytes(data[cut:])
        with open(path, "r+b") as handle:
            handle.truncate(cut)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_journal(self, store_last_seq: int, *, repair: bool) -> list[dict]:
        """Every verified line, in order. A torn tail is discarded only when
        `repair` is set and the REQ-003 rule allows it; anything else that
        fails verification raises QueueCorrupt naming segment, line and seq."""
        lines: list[dict] = []
        prev_hash = GENESIS_HASH
        segments = self._segments()
        for position, (number, path) in enumerate(segments):
            name = _segment_name(number)
            data = path.read_bytes()
            cut = data.rfind(b"\n") + 1
            tail = data[cut:]
            last_segment = position == len(segments) - 1
            raw_lines = data[:cut].split(b"\n")[:-1] if cut else []
            for index, raw in enumerate(raw_lines, start=1):
                expected_seq = (lines[-1]["seq"] if lines else 0) + 1
                where = f"journal segment {name} line {index} seq {expected_seq}"
                try:
                    line = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise QueueCorrupt(f"{where} does not parse") from exc
                self._verify_line(line, expected_seq, prev_hash, where)
                lines.append(line)
                prev_hash = line["hash"]
            if lines and lines[-1]["op"] == "checkpoint" and not last_segment:
                if lines[-1]["fields"].get("segment") != number + 1:
                    raise QueueCorrupt(f"journal segment {name} checkpoint names the wrong next segment")
            elif not last_segment:
                raise QueueCorrupt(f"journal segment {name} ends without a checkpoint line")
            if tail:
                prior = lines[-1]["seq"] if lines else 0
                try:
                    parsed = json.loads(tail.decode("utf-8"))
                    seq = parsed.get("seq") if isinstance(parsed, dict) else None
                    seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else prior + 1
                except (UnicodeDecodeError, ValueError):
                    seq = prior + 1
                where = f"journal segment {name} line {len(raw_lines) + 1} seq {seq}"
                # Decide from the VERIFIED prefix, never the tail's own seq:
                # the tail is unverified, so a forged seq must not license
                # truncation. Only when every committed record (up to the
                # store's last seq) is already in the verified prefix is the
                # tail provably uncommitted (implementation review attempt 1).
                if not last_segment or prior < store_last_seq or seq <= store_last_seq:
                    raise QueueCorrupt(f"{where} has no terminating newline but was committed")
                if repair:
                    self._discard_torn_tail(path, data, cut, seq)
        return lines

    @staticmethod
    def _verify_line(line: object, expected_seq: int, prev_hash: str, where: str) -> None:
        if not isinstance(line, dict) or set(line) != {
                "seq", "job", "op", "worker", "epoch", "at", "fields", "prev_hash", "hash"}:
            raise QueueCorrupt(f"{where} does not have the journal line shape")
        if line["seq"] != expected_seq:
            raise QueueCorrupt(f"{where} has seq {line['seq']!r}: a gap in the journal")
        if line["prev_hash"] != prev_hash:
            raise QueueCorrupt(f"{where} breaks the hash chain")
        if line["op"] not in OPS or not isinstance(line["fields"], dict):
            raise QueueCorrupt(f"{where} has an invalid op or fields")
        if not isinstance(line["hash"], str) or not HASH_PATTERN.match(line["hash"]):
            raise QueueCorrupt(f"{where} has a malformed hash")
        body = {k: v for k, v in line.items() if k != "hash"}
        if line_hash(body, prev_hash) != line["hash"]:
            raise QueueCorrupt(f"{where} fails its hash")

    def _load(self) -> tuple[dict, list[dict]]:
        """Load under the lock: verify the journal, discard a torn tail, and
        apply any journaled line the store has not yet absorbed."""
        store = self._read_store()
        lines = self._read_journal(store["last_seq"], repair=True)
        journal_seq = lines[-1]["seq"] if lines else 0
        if store["last_seq"] > journal_seq:
            raise QueueCorrupt(
                f"{STORE_FILE} last_seq {store['last_seq']} is beyond the journal's last seq {journal_seq}")
        if journal_seq > store["last_seq"]:
            for line in lines[store["last_seq"]:]:
                apply_line(store, line)
            atomic_write_json(self.store_path, store)
        return store, lines

    # -- appending ------------------------------------------------------------

    def _active_segment(self, lines: list[dict]) -> int:
        segments = self._segments()
        number = segments[-1][0] if segments else 1
        if lines and lines[-1]["op"] == "checkpoint" and segments:
            number = lines[-1]["fields"]["segment"]
        return number

    def _commit(self, store: dict, lines: list[dict], pending: list[dict]) -> None:
        """Journal first, then store. Each pending entry is a line body
        without seq, prev_hash or hash."""
        if not pending:
            return
        path = self.journal_dir / _segment_name(self._active_segment(lines))
        created = not path.exists()
        prev_hash = lines[-1]["hash"] if lines else GENESIS_HASH
        seq = lines[-1]["seq"] if lines else 0
        written = []
        for entry in pending:
            seq += 1
            body = dict(entry, seq=seq, prev_hash=prev_hash)
            line = dict(body, hash=line_hash(body, prev_hash))
            written.append(line)
            prev_hash = line["hash"]
        with open(path, "ab") as handle:
            handle.write("".join(_canonical(line) + "\n" for line in written).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            _fsync_dir(self.journal_dir)
        if self.after_journal is not None:
            self.after_journal()
        for line in written:
            apply_line(store, line)
            lines.append(line)
        atomic_write_json(self.store_path, store)
        if path.stat().st_size > self.segment_bytes:
            self._rotate(store, lines, path)

    def _rotate(self, store: dict, lines: list[dict], path: Path) -> None:
        number = int(SEGMENT_PATTERN.match(path.name).group(1)) + 1
        snapshot = dict(store, last_seq=store["last_seq"] + 1)
        digest = snapshot_hash(snapshot)
        prev_hash = lines[-1]["hash"]
        body = {"seq": snapshot["last_seq"], "job": None, "op": "checkpoint", "worker": None, "epoch": 0,
                "at": _iso(self.now()), "fields": {"segment": number, "snapshot_hash": digest},
                "prev_hash": prev_hash}
        line = dict(body, hash=line_hash(body, prev_hash))
        atomic_write_json(self.journal_dir / f"checkpoint-{number:06d}.json", {
            "segment": number, "seq": line["seq"], "hash": line["hash"],
            "store": snapshot, "snapshot_hash": digest,
        })
        with open(path, "ab") as handle:
            handle.write((_canonical(line) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        apply_line(store, line)
        lines.append(line)
        atomic_write_json(self.store_path, store)
        (self.journal_dir / _segment_name(number)).touch()
        _fsync_dir(self.journal_dir)

    # -- reading --------------------------------------------------------------

    def load(self) -> dict:
        with self._locked():
            store, _lines = self._load()
        return store

    def job(self, job_id: str) -> dict | None:
        return self.load()["jobs"].get(job_id)

    def journal(self) -> list[dict]:
        with self._locked():
            _store, lines = self._load()
        return lines

    def replay(self, *, from_start: bool = False) -> dict:
        """Rebuild the store from the journal alone: from the latest verified
        checkpoint, or from segment 1 when `from_start` is set."""
        with self._locked():
            _store, lines = self._load()
        store = empty_store()
        start = 0
        if not from_start:
            checkpoint = self._latest_verified_checkpoint(lines)
            if checkpoint is not None:
                store = json.loads(json.dumps(checkpoint["store"]))
                start = checkpoint["seq"]
        for line in lines[start:]:
            apply_line(store, line)
        return store

    def _latest_verified_checkpoint(self, lines: list[dict]) -> dict | None:
        candidates = sorted(self.journal_dir.glob("checkpoint-*.json"), reverse=True)
        for path in candidates:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                seq = record["seq"]
                line = lines[seq - 1] if isinstance(seq, int) and 0 < seq <= len(lines) else None
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (line is not None and line["op"] == "checkpoint" and line["hash"] == record.get("hash")
                    and line["fields"].get("snapshot_hash") == record.get("snapshot_hash")
                    and snapshot_hash(record.get("store")) == record.get("snapshot_hash")
                    and record["store"].get("last_seq") == seq):
                return record
        return None

    # -- mutations ------------------------------------------------------------

    def submit(self, key: str, payload: dict, *, host: str | None = None,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
        """Persist a job and return its id; a known key returns the existing
        id and writes nothing."""
        host = self.host if host is None else host
        if not isinstance(host, str) or not 1 <= len(host) <= 255:
            raise QueueError("host must be 1 to 255 characters")
        if host not in {self.host, "localhost"}:
            raise QueueError(
                f"job names host {host!r} but this queue runs on {self.host!r} and has no remote transport")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            raise QueueError("idempotency key must match ^[A-Za-z0-9._:-]{1,128}$")
        if not isinstance(payload, dict) or len(_canonical(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise QueueError(f"payload must be a JSON object of at most {MAX_PAYLOAD_BYTES} bytes")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) \
                or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise QueueError(f"max_attempts must be 1 to {MAX_ATTEMPTS_LIMIT}")
        with self._locked():
            store, lines = self._load()
            for job in store["jobs"].values():
                if job["key"] == key:
                    return job["id"]
            if len(store["jobs"]) >= MAX_JOBS:
                raise QueueError(f"the queue holds its maximum of {MAX_JOBS} jobs")
            job_id = f"job-{uuid.uuid4().hex[:16]}"
            self._commit(store, lines, [{
                "job": job_id, "op": "submit", "worker": None, "epoch": 0, "at": _iso(self.now()),
                "fields": {"key": key, "host": host, "payload": payload, "max_attempts": max_attempts},
            }])
            return job_id

    def claim(self, worker: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict | None:
        """Lease the first queued job, or the first leased job whose lease
        has expired (journaling its expiry first). None when nothing is free."""
        self._check_worker(worker)
        with self._locked():
            store, lines = self._load()
            now = self.now()
            at = _iso(now)
            pending = []
            granted = None
            for job in sorted(store["jobs"].values(), key=lambda item: (item["created_at"], item["id"])):
                state = job["state"]
                if state == "leased" and now >= _parse_iso(job["lease"]["expires_at"]):
                    attempts = job["attempts"] + 1
                    state = "failed" if attempts >= job["max_attempts"] else "queued"
                    pending.append({"job": job["id"], "op": "expire", "worker": job["lease"]["worker"],
                                    "epoch": job["epoch"], "at": at,
                                    "fields": {"attempts": attempts, "state": state}})
                if state == "queued":
                    granted = job["id"]
                    pending.append({"job": granted, "op": "claim", "worker": worker,
                                    "epoch": job["epoch"] + 1, "at": at,
                                    "fields": {"expires_at": _iso(now + timedelta(seconds=lease_seconds))}})
                    break
            self._commit(store, lines, pending)
            return dict(store["jobs"][granted]) if granted else None

    def heartbeat(self, job_id: str, worker: str, epoch: int, *,
                  lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
        with self._locked():
            store, lines = self._load()
            now = self.now()
            job = self._owned(store, job_id, worker, epoch, now)
            self._commit(store, lines, [{
                "job": job_id, "op": "heartbeat", "worker": worker, "epoch": epoch, "at": _iso(now),
                "fields": {"expires_at": _iso(now + timedelta(seconds=lease_seconds))},
            }])
            return dict(job)

    def succeed(self, job_id: str, worker: str, epoch: int) -> dict:
        with self._locked():
            store, lines = self._load()
            now = self.now()
            job = self._owned(store, job_id, worker, epoch, now)
            self._commit(store, lines, [{
                "job": job_id, "op": "succeed", "worker": worker, "epoch": epoch, "at": _iso(now), "fields": {},
            }])
            return dict(job)

    def fail(self, job_id: str, worker: str, epoch: int) -> dict:
        """The owner reports failure: re-queue while attempts remain, else end failed."""
        with self._locked():
            store, lines = self._load()
            now = self.now()
            job = self._owned(store, job_id, worker, epoch, now)
            attempts = job["attempts"] + 1
            state = "failed" if attempts >= job["max_attempts"] else "queued"
            self._commit(store, lines, [{
                "job": job_id, "op": "fail", "worker": worker, "epoch": epoch, "at": _iso(now),
                "fields": {"attempts": attempts, "state": state, "requeued": state == "queued"},
            }])
            return dict(job)

    def cancel(self, job_id: str) -> dict:
        with self._locked():
            store, lines = self._load()
            job = store["jobs"].get(job_id)
            if job is None:
                raise LeaseRefused(f"unknown job {job_id}")
            if job["state"] not in ("queued", "leased"):
                raise LeaseRefused(f"job {job_id} is {job['state']} and cannot be cancelled")
            self._commit(store, lines, [{
                "job": job_id, "op": "cancel", "worker": None, "epoch": job["epoch"],
                "at": _iso(self.now()), "fields": {},
            }])
            return dict(job)

    @staticmethod
    def _check_worker(worker: object) -> None:
        if not isinstance(worker, str) or not WORKER_PATTERN.match(worker):
            raise QueueError("worker must match ^[A-Za-z0-9._:-]{1,64}$")

    def _owned(self, store: dict, job_id: str, worker: str, epoch: int, now: datetime) -> dict:
        """The lease check every owner call makes, inside the mutation's lock."""
        self._check_worker(worker)
        job = store["jobs"].get(job_id)
        if job is None:
            raise LeaseRefused(f"unknown job {job_id}")
        if job["state"] in TERMINAL_STATES:
            raise LeaseRefused(f"job {job_id} is {job['state']}")
        if job["state"] != "leased" or not job["lease"]:
            raise StaleLease(f"job {job_id} is not leased")
        if job["lease"]["worker"] != worker or job["epoch"] != epoch:
            raise StaleLease(f"job {job_id} is leased to {job['lease']['worker']} at epoch {job['epoch']}")
        if now >= _parse_iso(job["lease"]["expires_at"]):
            raise LeaseExpired(f"job {job_id} lease expired at {job['lease']['expires_at']}")
        return job
