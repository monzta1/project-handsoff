#!/usr/bin/env python3
"""Resumable, provider-neutral release publication.

The transaction deliberately knows nothing about GitHub, pip, or Handsoff's
ledger.  A caller supplies a durable record store and an adapter for external
state.  This keeps all network and installation work injectable while the
ordering and conflict rules remain reusable and testable.

Adapters MUST make actions idempotent for the supplied ``operation_key``.
Every retry reads first; an exact object is adopted, a conflicting object is
never overwritten or deleted, and a missing object is created only through
the corresponding idempotent action.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, Protocol, Sequence, TypeVar


RECORD_VERSION = 1
MAX_TRANSITIONS = 16
STEP_NAMES = (
    "annotated_tag",
    "release",
    "wheel_asset",
    "checksum_asset",
    "install",
    "installed_manifest",
)
STEP_STATES = ("intent", "action", "read_back", "complete")

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_TOP_KEYS = {
    "schema_version", "transaction_id", "identity", "expectations",
    "steps", "complete", "created_at", "updated_at",
}
_IDENTITY_KEYS = {"repository", "version", "merged_commit", "artifact_sha256"}
_EXPECTATION_KEYS = {
    "artifact_name", "checksum_name", "checksum_sha256", "manifest_sha256",
}
_STEP_KEYS = {"state", "transitions", "result"}
_TRANSITION_KEYS = {"state", "at"}
_RESULT_KEYS = {
    "identifier", "url", "name", "sha256", "version", "commit",
    "manifest_sha256", "source",
}
_LEGACY_KEYS = {
    "schema_version", "repository", "version", "merged_commit",
    "artifact_sha256", "tag", "release_url", "artifact_names",
    "installed_version", "manifest_verified",
}


class ReleaseTransactionError(RuntimeError):
    """Base class for release transaction failures."""


class RecordValidationError(ReleaseTransactionError):
    """The durable record is malformed or exceeds its bounds."""


class UnknownRecordVersion(ReleaseTransactionError):
    """A newer record may be inspected but cannot be mutated."""

    def __init__(self, version: object):
        self.version = version
        super().__init__(f"release_transaction.record_version is unsupported: {version!r}")


class IdentityConflict(ReleaseTransactionError):
    """A retry attempted to change immutable transaction inputs."""

    def __init__(self, invariant: str, expected: object, observed: object):
        self.invariant = invariant
        self.expected = expected
        self.observed = observed
        super().__init__(f"{invariant} invariant failed: expected {expected!r}, observed {observed!r}")


class InvariantViolation(ReleaseTransactionError):
    """Canonical external state definitively conflicts with the plan."""

    def __init__(self, invariant: str, expected: object, observed: object):
        self.invariant = invariant
        self.expected = expected
        self.observed = observed
        super().__init__(f"{invariant} invariant failed: expected {expected!r}, observed {observed!r}")


class ProviderConsistencyPending(ReleaseTransactionError):
    """An action succeeded but its read-back is not authoritative yet."""

    def __init__(self, step: str, invariant: str):
        self.step = step
        self.invariant = invariant
        self.reason = "provider_eventual_consistency"
        super().__init__(f"{invariant} is not visible yet (provider eventual consistency)")


@dataclass(frozen=True)
class ReleaseIdentity:
    repository: str
    version: str
    merged_commit: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        _bounded_text("release_transaction.identity.repository", self.repository, 256)
        if "/" not in self.repository or any(ch.isspace() for ch in self.repository):
            raise ValueError("repository must be a bounded owner/name identifier")
        _bounded_text("release_transaction.identity.version", self.version, 64)
        commit = self.merged_commit.lower()
        digest = self.artifact_sha256.lower()
        if not _COMMIT.fullmatch(commit):
            raise ValueError("merged_commit must be a 40- or 64-character hexadecimal commit")
        if not _HEX_64.fullmatch(digest):
            raise ValueError("artifact_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "merged_commit", commit)
        object.__setattr__(self, "artifact_sha256", digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "version": self.version,
            "merged_commit": self.merged_commit,
            "artifact_sha256": self.artifact_sha256,
        }

    @property
    def transaction_id(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ReleasePlan:
    identity: ReleaseIdentity
    artifact_name: str
    manifest_sha256: str
    checksum_name: str | None = None

    def __post_init__(self) -> None:
        _asset_name("artifact_name", self.artifact_name)
        checksum_name = self.checksum_name or f"{self.artifact_name}.sha256"
        _asset_name("checksum_name", checksum_name)
        manifest = self.manifest_sha256.lower()
        if not _HEX_64.fullmatch(manifest):
            raise ValueError("manifest_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "checksum_name", checksum_name)
        object.__setattr__(self, "manifest_sha256", manifest)

    @property
    def checksum_contents(self) -> bytes:
        return f"{self.identity.artifact_sha256}  {self.artifact_name}\n".encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        return hashlib.sha256(self.checksum_contents).hexdigest()

    def expectations(self) -> dict[str, str]:
        return {
            "artifact_name": self.artifact_name,
            "checksum_name": str(self.checksum_name),
            "checksum_sha256": self.checksum_sha256,
            "manifest_sha256": self.manifest_sha256,
        }


T = TypeVar("T")


@dataclass(frozen=True)
class Observation(Generic[T]):
    """A provider read and whether absence is currently authoritative."""

    value: T | None
    definitive: bool = True


@dataclass(frozen=True)
class TagState:
    name: str
    commit: str
    annotated: bool
    identifier: str


@dataclass(frozen=True)
class AssetState:
    name: str
    sha256: str
    identifier: str
    url: str
    contents: bytes | None = None


@dataclass(frozen=True)
class ReleaseState:
    tag: str
    commit: str
    identifier: str
    url: str
    draft: bool = False
    assets: tuple[AssetState, ...] = field(default_factory=tuple)
    assets_url: str | None = None


@dataclass(frozen=True)
class InstallState:
    repository: str
    version: str
    commit: str
    artifact_sha256: str


@dataclass(frozen=True)
class ManifestState:
    version: str
    sha256: str
    artifact_sha256: str


class ReleaseAdapter(Protocol):
    """External operations. Action methods must honor ``operation_key``."""

    def read_tag(self, identity: ReleaseIdentity) -> Observation[TagState]: ...
    def create_annotated_tag(self, identity: ReleaseIdentity, operation_key: str) -> None: ...
    def read_release(self, identity: ReleaseIdentity) -> Observation[ReleaseState]: ...
    def create_release(self, identity: ReleaseIdentity, operation_key: str) -> None: ...
    def read_assets_url(self, assets_url: str) -> Observation[Sequence[AssetState]]: ...
    def upload_wheel(self, identity: ReleaseIdentity, name: str,
                     operation_key: str) -> None: ...
    def upload_checksum(self, identity: ReleaseIdentity, name: str, contents: bytes,
                        operation_key: str) -> None: ...
    def read_install(self, identity: ReleaseIdentity) -> Observation[InstallState]: ...
    def install(self, identity: ReleaseIdentity, artifact_url: str,
                operation_key: str) -> None: ...
    def read_manifest(self, identity: ReleaseIdentity) -> Observation[ManifestState]: ...
    def verify_manifest(self, identity: ReleaseIdentity, operation_key: str) -> None: ...


class RecordStore(Protocol):
    def load(self) -> Mapping[str, Any] | None: ...
    def save(self, record: Mapping[str, Any]) -> None: ...


class JsonFileRecordStore:
    """Small atomic JSON store suitable for an integration-owned state path."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> Mapping[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordValidationError(f"cannot read release transaction: {exc}") from exc
        if not isinstance(value, dict):
            raise RecordValidationError("release transaction must be a JSON object")
        return value

    def save(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class _Inspection:
    status: str
    invariant: str
    expected: object
    observed: object
    result: dict[str, Any] | None = None
    definitive: bool = True


Checkpoint = Callable[[str, str | None, Mapping[str, Any]], None]


class ReleaseTransaction:
    """Reconcile one immutable release identity to verified installation."""

    def __init__(self, plan: ReleasePlan, store: RecordStore, adapter: ReleaseAdapter,
                 *, clock: Callable[[], datetime] | None = None,
                 checkpoint: Checkpoint | None = None,
                 legacy_authenticated: bool = False):
        self.plan = plan
        self.store = store
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.checkpoint = checkpoint or (lambda _point, _step, _record: None)
        self.legacy_authenticated = legacy_authenticated

    def inspect(self) -> dict[str, Any]:
        raw = self.store.load()
        if raw is None:
            return {"present": False, "mutable": True, "schema_version": RECORD_VERSION}
        version = raw.get("schema_version", 0)
        if type(version) is not int or version not in (0, RECORD_VERSION):
            return {"present": True, "mutable": False, "schema_version": version}
        if version == 0:
            return {"present": True, "mutable": bool(self.legacy_authenticated),
                    "schema_version": 0, "legacy": True}
        record = copy.deepcopy(dict(raw))
        validate_record(record)
        return {"present": True, "mutable": True, "schema_version": RECORD_VERSION,
                "transaction_id": record["transaction_id"], "complete": record["complete"],
                "steps": {name: record["steps"][name]["state"] for name in STEP_NAMES}}

    def reconcile(self) -> dict[str, Any]:
        record = self._load_or_initialize()
        for step in STEP_NAMES:
            if record["steps"][step]["state"] == "complete":
                inspection = self._inspect_step(step)
                self._require_exact(step, inspection)
                self._check_recorded_result(step, record["steps"][step]["result"], inspection.result)
                continue
            self._reconcile_step(record, step)
        if not record["complete"]:
            record["complete"] = True
            self._save(record)
            self.checkpoint("transaction_completed", None, copy.deepcopy(record))
        return copy.deepcopy(record)

    def _load_or_initialize(self) -> dict[str, Any]:
        raw = self.store.load()
        if raw is None:
            record = _new_record(self.plan, self._now())
            self._save(record)
            self.checkpoint("record_initialized", None, copy.deepcopy(record))
            return record
        version = raw.get("schema_version", 0)
        if type(version) is not int or version not in (0, RECORD_VERSION):
            raise UnknownRecordVersion(version)
        if version == 0:
            if not self.legacy_authenticated:
                raise RecordValidationError(
                    "legacy release transaction requires authenticated migration"
                )
            record = _migrate_legacy(raw, self.plan, self._now())
            self._save(record)
            self.checkpoint("legacy_migrated", None, copy.deepcopy(record))
        else:
            record = copy.deepcopy(dict(raw))
            validate_record(record)
        self._check_plan(record)
        return record

    def _check_plan(self, record: Mapping[str, Any]) -> None:
        expected_identity = self.plan.identity.as_dict()
        for key, expected in expected_identity.items():
            observed = record["identity"].get(key)
            if observed != expected:
                raise IdentityConflict(f"release_transaction.identity.{key}", expected, observed)
        if record["transaction_id"] != self.plan.identity.transaction_id:
            raise IdentityConflict("release_transaction.transaction_id",
                                   self.plan.identity.transaction_id, record["transaction_id"])
        for key, expected in self.plan.expectations().items():
            observed = record["expectations"].get(key)
            if observed != expected:
                raise IdentityConflict(f"release_transaction.expectations.{key}", expected, observed)

    def _reconcile_step(self, record: dict[str, Any], step: str) -> None:
        entry = record["steps"][step]
        if entry["state"] == "pending":
            self._transition(record, step, "intent")
            self.checkpoint("intent_persisted", step, copy.deepcopy(record))

        inspection = self._inspect_step(step)
        self.checkpoint("pre_read", step, copy.deepcopy(record))
        if inspection.status == "exact":
            self._record_read_back(record, step, inspection.result, source="adopted")
            return
        if inspection.status == "conflict":
            self._raise_inspection(inspection)

        if entry["state"] == "intent":
            self._transition(record, step, "action")
            self.checkpoint("action_persisted", step, copy.deepcopy(record))
        self._act(step)
        self.checkpoint("action_performed", step, copy.deepcopy(record))

        inspection = self._inspect_step(step)
        self.checkpoint("read_back_observed", step, copy.deepcopy(record))
        if inspection.status == "conflict":
            self._raise_inspection(inspection)
        if inspection.status == "missing":
            if inspection.definitive:
                self._raise_inspection(inspection)
            raise ProviderConsistencyPending(step, inspection.invariant)
        self._record_read_back(record, step, inspection.result, source="action_reconciled")

    def _record_read_back(self, record: dict[str, Any], step: str,
                          result: dict[str, Any] | None, *, source: str) -> None:
        if result is None:
            raise RecordValidationError(f"{step} exact read-back has no result")
        result = dict(result)
        result["source"] = source
        entry = record["steps"][step]
        if entry["state"] != "read_back":
            entry["result"] = _closed_result(result)
            self._transition(record, step, "read_back")
            self.checkpoint("read_back_persisted", step, copy.deepcopy(record))
        self._transition(record, step, "complete")
        self.checkpoint("completion_persisted", step, copy.deepcopy(record))

    def _transition(self, record: dict[str, Any], step: str, state: str) -> None:
        entry = record["steps"][step]
        if state not in STEP_STATES:
            raise RecordValidationError(f"unknown release step state: {state}")
        allowed = {
            "pending": {"intent"},
            "intent": {"action", "read_back"},  # exact pre-existing object is adopted
            "action": {"read_back"},
            "read_back": {"complete"},
            "complete": set(),
        }
        if state not in allowed[entry["state"]]:
            raise RecordValidationError(f"invalid {step} transition {entry['state']} -> {state}")
        if len(entry["transitions"]) >= MAX_TRANSITIONS:
            raise RecordValidationError(f"{step} exceeds {MAX_TRANSITIONS} transitions")
        entry["state"] = state
        entry["transitions"].append({"state": state, "at": self._now()})
        self._save(record)

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = self._now()
        validate_record(record)
        self.store.save(copy.deepcopy(record))

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _operation_key(self, step: str) -> str:
        return f"release:{self.plan.identity.transaction_id}:{step}"

    def _act(self, step: str) -> None:
        identity = self.plan.identity
        key = self._operation_key(step)
        if step == "annotated_tag":
            self.adapter.create_annotated_tag(identity, key)
        elif step == "release":
            self.adapter.create_release(identity, key)
        elif step == "wheel_asset":
            self.adapter.upload_wheel(identity, self.plan.artifact_name, key)
        elif step == "checksum_asset":
            self.adapter.upload_checksum(identity, str(self.plan.checksum_name),
                                         self.plan.checksum_contents, key)
        elif step == "install":
            release = self._exact_release()
            asset = self._inspect_asset("wheel_asset")
            self._require_exact("wheel_asset", asset)
            self.adapter.install(identity, str(asset.result["url"]), key)  # type: ignore[index]
        elif step == "installed_manifest":
            self.adapter.verify_manifest(identity, key)
        else:  # pragma: no cover - STEP_NAMES is closed and validated
            raise RecordValidationError(f"unknown release step: {step}")

    def _inspect_step(self, step: str) -> _Inspection:
        if step == "annotated_tag":
            return self._inspect_tag()
        if step == "release":
            return self._inspect_release()
        if step in {"wheel_asset", "checksum_asset"}:
            return self._inspect_asset(step)
        if step == "install":
            return self._inspect_install()
        if step == "installed_manifest":
            return self._inspect_manifest()
        raise RecordValidationError(f"unknown release step: {step}")

    def _inspect_tag(self) -> _Inspection:
        observed = self.adapter.read_tag(self.plan.identity)
        tag = observed.value
        if tag is None:
            return _missing("annotated_tag.presence", "annotated tag", observed.definitive)
        if tag.name != self.plan.identity.version:
            return _conflict("annotated_tag.name", self.plan.identity.version, tag.name)
        if not tag.annotated:
            return _conflict("annotated_tag.kind", "annotated", "lightweight")
        if tag.commit.lower() != self.plan.identity.merged_commit:
            return _conflict("annotated_tag.commit", self.plan.identity.merged_commit, tag.commit)
        return _exact(_result(identifier=tag.identifier, version=tag.name,
                              commit=tag.commit.lower()))

    def _inspect_release(self) -> _Inspection:
        observed = self.adapter.read_release(self.plan.identity)
        release = observed.value
        if release is None:
            return _missing("release.presence", "published release", observed.definitive)
        conflict = self._release_conflict(release)
        if conflict:
            return conflict
        return _exact(_result(identifier=release.identifier, url=release.url,
                              version=release.tag, commit=release.commit.lower()))

    def _release_conflict(self, release: ReleaseState) -> _Inspection | None:
        if release.tag != self.plan.identity.version:
            return _conflict("release.tag", self.plan.identity.version, release.tag)
        if release.commit.lower() != self.plan.identity.merged_commit:
            return _conflict("release.commit", self.plan.identity.merged_commit, release.commit)
        if release.draft:
            return _conflict("release.published", True, False)
        return None

    def _exact_release(self) -> ReleaseState:
        observed = self.adapter.read_release(self.plan.identity)
        if observed.value is None:
            self._raise_inspection(_missing("release.presence", "published release",
                                            observed.definitive))
        conflict = self._release_conflict(observed.value)  # type: ignore[arg-type]
        if conflict:
            self._raise_inspection(conflict)
        return observed.value  # type: ignore[return-value]

    def _inspect_asset(self, step: str) -> _Inspection:
        release_observed = self.adapter.read_release(self.plan.identity)
        if release_observed.value is None:
            return _missing("release.presence", "published release", release_observed.definitive)
        release = release_observed.value
        release_conflict = self._release_conflict(release)
        if release_conflict:
            return release_conflict
        name = self.plan.artifact_name if step == "wheel_asset" else str(self.plan.checksum_name)
        expected_sha = (self.plan.identity.artifact_sha256 if step == "wheel_asset"
                        else self.plan.checksum_sha256)
        embedded = self._match_asset(step, release.assets, name, expected_sha)
        if embedded.status == "exact":
            return embedded

        canonical: _Inspection | None = None
        if release.assets_url:
            assets = self.adapter.read_assets_url(release.assets_url)
            if assets.value is None:
                canonical = _missing(f"{step}.presence", name, assets.definitive)
            else:
                canonical = self._match_asset(step, assets.value, name, expected_sha)
                if canonical.status == "missing" and not assets.definitive:
                    canonical = _Inspection(**{**canonical.__dict__, "definitive": False})
            if canonical.status == "exact":
                return canonical
            if canonical.status == "conflict":
                return canonical
        if embedded.status == "conflict":
            return embedded
        return canonical or embedded

    def _match_asset(self, step: str, assets: Sequence[AssetState], name: str,
                     expected_sha: str) -> _Inspection:
        matches = [asset for asset in assets if asset.name == name]
        if not matches:
            return _missing(f"{step}.presence", name, True)
        if len(matches) != 1:
            return _conflict(f"{step}.uniqueness", 1, len(matches))
        asset = matches[0]
        if asset.sha256.lower() != expected_sha:
            return _conflict(f"{step}.sha256", expected_sha, asset.sha256)
        if step == "checksum_asset" and asset.contents != self.plan.checksum_contents:
            observed = None if asset.contents is None else asset.contents.decode("utf-8", "replace")
            return _conflict("checksum_asset.contents",
                             self.plan.checksum_contents.decode("utf-8"), observed)
        return _exact(_result(identifier=asset.identifier, url=asset.url, name=asset.name,
                              sha256=asset.sha256.lower()))

    def _inspect_install(self) -> _Inspection:
        observed = self.adapter.read_install(self.plan.identity)
        install = observed.value
        if install is None:
            return _missing("install.presence", "installed artifact", observed.definitive)
        expected = self.plan.identity
        for key, wanted, actual in (
            ("repository", expected.repository, install.repository),
            ("version", expected.version, install.version),
            ("commit", expected.merged_commit, install.commit.lower()),
            ("artifact_sha256", expected.artifact_sha256, install.artifact_sha256.lower()),
        ):
            if actual != wanted:
                return _conflict(f"install.{key}", wanted, actual)
        return _exact(_result(version=install.version, commit=install.commit.lower(),
                              sha256=install.artifact_sha256.lower()))

    def _inspect_manifest(self) -> _Inspection:
        observed = self.adapter.read_manifest(self.plan.identity)
        manifest = observed.value
        if manifest is None:
            return _missing("installed_manifest.presence", "installed manifest", observed.definitive)
        if manifest.version != self.plan.identity.version:
            return _conflict("installed_manifest.version", self.plan.identity.version,
                             manifest.version)
        if manifest.artifact_sha256.lower() != self.plan.identity.artifact_sha256:
            return _conflict("installed_manifest.artifact_sha256",
                             self.plan.identity.artifact_sha256, manifest.artifact_sha256)
        if manifest.sha256.lower() != self.plan.manifest_sha256:
            return _conflict("installed_manifest.sha256", self.plan.manifest_sha256,
                             manifest.sha256)
        return _exact(_result(version=manifest.version,
                              sha256=manifest.artifact_sha256.lower(),
                              manifest_sha256=manifest.sha256.lower()))

    @staticmethod
    def _raise_inspection(inspection: _Inspection) -> None:
        if inspection.status == "missing" and not inspection.definitive:
            raise ProviderConsistencyPending(inspection.invariant.split(".", 1)[0],
                                             inspection.invariant)
        raise InvariantViolation(inspection.invariant, inspection.expected, inspection.observed)

    @classmethod
    def _require_exact(cls, step: str, inspection: _Inspection) -> None:
        if inspection.status != "exact":
            cls._raise_inspection(inspection)

    @staticmethod
    def _check_recorded_result(step: str, recorded: Mapping[str, Any] | None,
                               observed: Mapping[str, Any] | None) -> None:
        if recorded is None or observed is None:
            raise RecordValidationError(f"completed {step} has no read-back result")
        for key in _RESULT_KEYS - {"source"}:
            old = recorded.get(key)
            new = observed.get(key)
            if old is not None and new != old:
                raise InvariantViolation(f"{step}.recorded_{key}", old, new)


def release_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded ledger/final-report evidence from a complete record."""

    validate_record(record)
    if not record["complete"]:
        raise RecordValidationError("release transaction is not complete")
    steps = record["steps"]
    manifest = steps["installed_manifest"]["result"]
    return {
        "repository": record["identity"]["repository"],
        "tag": record["identity"]["version"],
        "merged_commit": record["identity"]["merged_commit"],
        "release_url": steps["release"]["result"]["url"],
        "artifact_names": [record["expectations"]["artifact_name"],
                           record["expectations"]["checksum_name"]],
        "artifact_sha256": record["identity"]["artifact_sha256"],
        "installed_version": steps["install"]["result"]["version"],
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_verified": True,
    }


def validate_record(record: Mapping[str, Any]) -> None:
    """Validate the complete closed and bounded schema-v1 record."""

    _closed_keys("release_transaction", record, _TOP_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != RECORD_VERSION:
        raise UnknownRecordVersion(record["schema_version"])
    if not _HEX_64.fullmatch(_text(record["transaction_id"])):
        raise RecordValidationError("release_transaction.transaction_id is not sha256")
    _closed_keys("release_transaction.identity", record["identity"], _IDENTITY_KEYS)
    try:
        identity = ReleaseIdentity(**record["identity"])
    except (TypeError, ValueError) as exc:
        raise RecordValidationError(str(exc)) from exc
    if identity.transaction_id != record["transaction_id"]:
        raise RecordValidationError("release_transaction.transaction_id does not match identity")
    _closed_keys("release_transaction.expectations", record["expectations"], _EXPECTATION_KEYS)
    expectations = record["expectations"]
    try:
        _asset_name("artifact_name", expectations["artifact_name"])
        _asset_name("checksum_name", expectations["checksum_name"])
    except ValueError as exc:
        raise RecordValidationError(str(exc)) from exc
    for key in ("checksum_sha256", "manifest_sha256"):
        if not _HEX_64.fullmatch(_text(expectations[key])):
            raise RecordValidationError(f"release_transaction.expectations.{key} is not sha256")
    if not isinstance(record["steps"], Mapping) or set(record["steps"]) != set(STEP_NAMES):
        raise RecordValidationError("release_transaction.steps must contain the closed step set")
    all_complete = True
    for name in STEP_NAMES:
        step = record["steps"][name]
        _closed_keys(f"release_transaction.steps.{name}", step, _STEP_KEYS)
        state = step["state"]
        if state not in ("pending", *STEP_STATES):
            raise RecordValidationError(f"release_transaction.steps.{name}.state is unknown")
        transitions = step["transitions"]
        if not isinstance(transitions, list) or len(transitions) > MAX_TRANSITIONS:
            raise RecordValidationError(f"release_transaction.steps.{name}.transitions is unbounded")
        sequence: list[str] = []
        for transition in transitions:
            _closed_keys(f"release_transaction.steps.{name}.transition", transition,
                         _TRANSITION_KEYS)
            if transition["state"] not in STEP_STATES:
                raise RecordValidationError(f"release_transaction.steps.{name} transition is unknown")
            _bounded_text("transition.at", transition["at"], 40)
            sequence.append(transition["state"])
        legal_sequences = {
            (),
            ("intent",),
            ("intent", "action"),
            ("intent", "read_back"),
            ("intent", "action", "read_back"),
            ("intent", "read_back", "complete"),
            ("intent", "action", "read_back", "complete"),
        }
        if tuple(sequence) not in legal_sequences:
            raise RecordValidationError(f"release_transaction.steps.{name} transitions are not monotonic")
        if state != (sequence[-1] if sequence else "pending"):
            raise RecordValidationError(f"release_transaction.steps.{name} state disagrees with transitions")
        result = step["result"]
        if result is not None:
            _closed_keys(f"release_transaction.steps.{name}.result", result, _RESULT_KEYS)
            for key, limit in (("identifier", 256), ("url", 2048), ("name", 255),
                               ("version", 64), ("commit", 64), ("source", 32)):
                if result[key] is not None:
                    _bounded_text(f"result.{key}", result[key], limit)
            for key in ("sha256", "manifest_sha256"):
                if result[key] is not None and not _HEX_64.fullmatch(_text(result[key])):
                    raise RecordValidationError(f"release_transaction result {key} is not sha256")
            if result["source"] not in {"adopted", "action_reconciled"}:
                raise RecordValidationError("release_transaction result source is unknown")
        if state in {"read_back", "complete"} and result is None:
            raise RecordValidationError(f"release_transaction.steps.{name} lacks read-back result")
        if state not in {"read_back", "complete"} and result is not None:
            raise RecordValidationError(f"release_transaction.steps.{name} has premature result")
        all_complete = all_complete and state == "complete"
    if not isinstance(record["complete"], bool):
        raise RecordValidationError("release_transaction.complete must be boolean")
    if record["complete"] and not all_complete:
        raise RecordValidationError("release_transaction is complete before every step")
    for key in ("created_at", "updated_at"):
        _bounded_text(f"release_transaction.{key}", record[key], 40)


def _new_record(plan: ReleasePlan, now: str) -> dict[str, Any]:
    return {
        "schema_version": RECORD_VERSION,
        "transaction_id": plan.identity.transaction_id,
        "identity": plan.identity.as_dict(),
        "expectations": plan.expectations(),
        "steps": {name: {"state": "pending", "transitions": [], "result": None}
                  for name in STEP_NAMES},
        "complete": False,
        "created_at": now,
        "updated_at": now,
    }


def _migrate_legacy(raw: Mapping[str, Any], plan: ReleasePlan, now: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RecordValidationError("legacy release transaction must be an object")
    unknown = set(raw) - _LEGACY_KEYS
    if unknown:
        raise RecordValidationError(f"legacy release transaction has unknown fields: {sorted(unknown)}")
    identity = {
        "repository": raw.get("repository"),
        "version": raw.get("version"),
        "merged_commit": raw.get("merged_commit"),
        "artifact_sha256": raw.get("artifact_sha256"),
    }
    expected = plan.identity.as_dict()
    for key, value in identity.items():
        if value != expected[key]:
            raise IdentityConflict(f"release_transaction.identity.{key}", expected[key], value)
    # Legacy evidence is deliberately not promoted to completion. Canonical
    # read-back will adopt exact objects one step at a time.
    return _new_record(plan, now)


def _result(**values: Any) -> dict[str, Any]:
    result = {key: None for key in _RESULT_KEYS}
    result.update(values)
    return result


def _closed_result(values: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(values) - _RESULT_KEYS
    if unknown:
        raise RecordValidationError(f"release result has unknown fields: {sorted(unknown)}")
    return {key: values.get(key) for key in _RESULT_KEYS}


def _exact(result: dict[str, Any]) -> _Inspection:
    return _Inspection("exact", "", None, None, _closed_result(result), True)


def _missing(invariant: str, expected: object, definitive: bool) -> _Inspection:
    return _Inspection("missing", invariant, expected, None, None, definitive)


def _conflict(invariant: str, expected: object, observed: object) -> _Inspection:
    return _Inspection("conflict", invariant, expected, observed, None, True)


def _closed_keys(path: str, value: object, expected: set[str]) -> None:
    if not isinstance(value, Mapping):
        raise RecordValidationError(f"{path} must be an object")
    keys = set(value)
    if keys != expected:
        raise RecordValidationError(
            f"{path} fields are closed; missing={sorted(expected - keys)}, unknown={sorted(keys - expected)}"
        )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise RecordValidationError("release transaction string field has the wrong type")
    return value


def _bounded_text(path: str, value: object, limit: int) -> None:
    text = _text(value)
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        raise RecordValidationError(f"{path} must contain 1..{limit} printable characters")


def _asset_name(path: str, value: object) -> None:
    try:
        _bounded_text(path, value, 255)
    except RecordValidationError as exc:
        raise ValueError(str(exc)) from exc
    if Path(_text(value)).name != value:
        raise ValueError(f"{path} must be a base name")


__all__ = [
    "AssetState", "IdentityConflict", "InstallState", "InvariantViolation",
    "JsonFileRecordStore", "ManifestState", "MAX_TRANSITIONS", "Observation",
    "ProviderConsistencyPending", "RECORD_VERSION", "RecordValidationError",
    "ReleaseAdapter", "ReleaseIdentity", "ReleasePlan", "ReleaseState",
    "ReleaseTransaction", "ReleaseTransactionError", "STEP_NAMES", "TagState",
    "UnknownRecordVersion", "release_evidence", "validate_record",
]
