#!/usr/bin/env python3
"""GitHub/pip integration for the resumable release transaction.

The transaction module owns ordering and invariants.  This module is the
small, replaceable provider adapter used by the Supervisor command surface.
All provider reads are canonical reads; release assets deliberately come
from the release ``assets_url`` rather than trusting an eventually stale
embedded asset list.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import fcntl

import handsoff_release_transaction as tx


RECORD_NAME = ".handsoff-release-transaction.json"
INSTALL_NAME = ".handsoff-release-install.json"
LOCK_NAME = ".handsoff-release-transaction.lock"
_NOT_FOUND = re.compile(r"(?:HTTP\s+404|not found)", re.IGNORECASE)
_TRANSIENT = re.compile(r"(?:HTTP\s+5\d\d|timed?\s*out|temporar|connection)", re.IGNORECASE)


class ReleaseProviderError(tx.ReleaseTransactionError):
    """A provider command failed without producing authoritative absence."""


Runner = Callable[..., subprocess.CompletedProcess]


def _tail(value: object, limit: int = 400) -> str:
    lines = str(value or "").strip().splitlines()
    return (lines[-1] if lines else "command failed")[-limit:]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_from_origin(root: Path, *, runner: Runner = subprocess.run) -> str:
    result = runner(["git", "config", "--get", "remote.origin.url"], cwd=str(root),
                    capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0:
        raise ReleaseProviderError(f"cannot read origin repository: {_tail(result.stderr)}")
    value = result.stdout.strip().removesuffix(".git")
    match = re.search(r"(?:github\.com[/:])([^/\s]+/[^/\s]+)$", value)
    if not match:
        raise ReleaseProviderError("origin is not a GitHub owner/repository URL")
    return match.group(1)


def head_commit(root: Path, *, runner: Runner = subprocess.run) -> str:
    result = runner(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True,
                    text=True, timeout=20, check=False)
    commit = result.stdout.strip().lower() if result.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseProviderError(f"cannot resolve the merged commit: {_tail(result.stderr)}")
    return commit


@contextmanager
def release_lock(root: Path):
    path = root / LOCK_NAME
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GitHubWheelReleaseAdapter:
    """Concrete adapter for annotated Git tags, GitHub Releases and pip."""

    def __init__(self, root: Path, plan: tx.ReleasePlan, artifact: Path, *,
                 notes_file: Path | None = None, runner: Runner = subprocess.run,
                 python: str = sys.executable):
        self.root = root.resolve()
        self.plan = plan
        self.artifact = artifact.resolve()
        self.notes_file = notes_file.resolve() if notes_file else None
        self.runner = runner
        self.python = python
        self.install_marker = self.root / INSTALL_NAME

    def _run(self, argv: list[str], *, timeout: int = 120,
             binary: bool = False) -> subprocess.CompletedProcess:
        result = self.runner(argv, cwd=str(self.root), capture_output=True,
                             text=not binary, timeout=timeout, check=False)
        if result.returncode != 0:
            detail = _tail(result.stderr)
            raise ReleaseProviderError(f"{' '.join(argv[:3])}: {detail}")
        return result

    def _api(self, endpoint: str) -> tx.Observation[Any]:
        result = self.runner(["gh", "api", endpoint], cwd=str(self.root),
                             capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}"
            if _NOT_FOUND.search(detail):
                return tx.Observation(None, definitive=True)
            if _TRANSIENT.search(detail):
                return tx.Observation(None, definitive=False)
            raise ReleaseProviderError(f"gh api {endpoint}: {_tail(result.stderr or result.stdout)}")
        try:
            return tx.Observation(json.loads(result.stdout), definitive=True)
        except (TypeError, ValueError) as exc:
            raise ReleaseProviderError(f"gh api {endpoint}: invalid JSON") from exc

    @property
    def _repo_api(self) -> str:
        return f"repos/{self.plan.identity.repository}"

    def read_tag(self, identity: tx.ReleaseIdentity) -> tx.Observation[tx.TagState]:
        endpoint = f"{self._repo_api}/git/ref/tags/{quote(identity.version, safe='')}"
        observed = self._api(endpoint)
        if observed.value is None:
            return tx.Observation(None, observed.definitive)
        obj = observed.value.get("object") if isinstance(observed.value, dict) else None
        if not isinstance(obj, dict):
            raise ReleaseProviderError("GitHub tag response lacks object identity")
        annotated = obj.get("type") == "tag"
        identifier = str(obj.get("sha") or "")
        commit = identifier
        if annotated:
            tag = self._api(f"{self._repo_api}/git/tags/{identifier}")
            if tag.value is None:
                return tx.Observation(None, tag.definitive)
            target = tag.value.get("object") if isinstance(tag.value, dict) else None
            commit = str(target.get("sha") or "") if isinstance(target, dict) else ""
        return tx.Observation(tx.TagState(identity.version, commit, annotated, identifier))

    def create_annotated_tag(self, identity: tx.ReleaseIdentity, operation_key: str) -> None:
        local = self.runner(["git", "rev-parse", "--verify", f"refs/tags/{identity.version}^{{tag}}"],
                            cwd=str(self.root), capture_output=True, text=True, timeout=20, check=False)
        if local.returncode != 0:
            self._run(["git", "tag", "-a", identity.version, identity.merged_commit,
                       "-m", identity.version], timeout=30)
        local_commit = self._run(
            ["git", "rev-parse", f"refs/tags/{identity.version}^{{commit}}"], timeout=20,
        ).stdout.strip().lower()
        if local_commit != identity.merged_commit:
            raise tx.InvariantViolation("annotated_tag.commit", identity.merged_commit, local_commit)
        self._run(["git", "push", "origin",
                   f"refs/tags/{identity.version}:refs/tags/{identity.version}"], timeout=120)

    def read_release(self, identity: tx.ReleaseIdentity) -> tx.Observation[tx.ReleaseState]:
        observed = self._api(f"{self._repo_api}/releases/tags/{quote(identity.version, safe='')}")
        if observed.value is None:
            return tx.Observation(None, observed.definitive)
        value = observed.value
        if not isinstance(value, dict):
            raise ReleaseProviderError("GitHub release response is not an object")
        tag = self.read_tag(identity)
        if tag.value is None:
            return tx.Observation(None, tag.definitive)
        return tx.Observation(tx.ReleaseState(
            str(value.get("tag_name") or ""), tag.value.commit,
            str(value.get("id") or ""), str(value.get("html_url") or ""),
            draft=bool(value.get("draft")), assets=(),
            assets_url=str(value.get("assets_url") or "") or None,
        ))

    def create_release(self, identity: tx.ReleaseIdentity, operation_key: str) -> None:
        argv = ["gh", "release", "create", identity.version, "--repo", identity.repository,
                "--verify-tag", "--title", identity.version]
        if self.notes_file:
            argv.extend(["--notes-file", str(self.notes_file)])
        else:
            argv.extend(["--notes", f"Handsoff {identity.version}"])
        self._run(argv, timeout=120)

    def _download_asset(self, endpoint: str) -> bytes:
        return bytes(self._run(["gh", "api", endpoint,
                                "-H", "Accept: application/octet-stream"],
                               timeout=120, binary=True).stdout)

    def read_assets_url(self, assets_url: str) -> tx.Observation[Sequence[tx.AssetState]]:
        observed = self._api(assets_url)
        if observed.value is None:
            return tx.Observation(None, observed.definitive)
        if not isinstance(observed.value, list):
            raise ReleaseProviderError("GitHub assets_url response is not a list")
        wanted = {self.plan.artifact_name, str(self.plan.checksum_name)}
        assets: list[tx.AssetState] = []
        for value in observed.value:
            if not isinstance(value, dict) or value.get("name") not in wanted:
                continue
            asset_id = str(value.get("id") or "")
            contents = self._download_asset(f"{self._repo_api}/releases/assets/{asset_id}")
            name = str(value["name"])
            assets.append(tx.AssetState(
                name=name, sha256=hashlib.sha256(contents).hexdigest(), identifier=asset_id,
                url=str(value.get("browser_download_url") or ""),
                contents=contents if name == self.plan.checksum_name else None,
            ))
        return tx.Observation(tuple(assets), definitive=True)

    def upload_wheel(self, identity: tx.ReleaseIdentity, name: str,
                     operation_key: str) -> None:
        self._run(["gh", "release", "upload", identity.version, str(self.artifact),
                   "--repo", identity.repository], timeout=180)

    def upload_checksum(self, identity: tx.ReleaseIdentity, name: str, contents: bytes,
                        operation_key: str) -> None:
        with tempfile.TemporaryDirectory(prefix="handsoff-release-") as directory:
            path = Path(directory) / name
            path.write_bytes(contents)
            self._run(["gh", "release", "upload", identity.version, str(path),
                       "--repo", identity.repository], timeout=180)

    def _installed_identity(self) -> Mapping[str, Any] | None:
        code = ("import json, handsoff_cli; "
                "print(json.dumps(handsoff_cli._current_identity(), sort_keys=True))")
        result = self.runner([self.python, "-c", code], cwd="/", capture_output=True,
                             text=True, timeout=30, check=False)
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _marker(self) -> Mapping[str, Any] | None:
        try:
            value = json.loads(self.install_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def read_install(self, identity: tx.ReleaseIdentity) -> tx.Observation[tx.InstallState]:
        installed, marker = self._installed_identity(), self._marker()
        if not installed or not marker or installed.get("version") != identity.version:
            return tx.Observation(None, definitive=True)
        return tx.Observation(tx.InstallState(
            str(marker.get("repository") or ""), str(installed.get("version") or ""),
            str(marker.get("commit") or ""), str(marker.get("artifact_sha256") or ""),
        ))

    def install(self, identity: tx.ReleaseIdentity, artifact_url: str,
                operation_key: str) -> None:
        self._run([self.python, "-m", "pip", "install", "--upgrade", artifact_url], timeout=600)
        payload = {
            "repository": identity.repository, "version": identity.version,
            "commit": identity.merged_commit, "artifact_sha256": identity.artifact_sha256,
        }
        temporary = self.install_marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.install_marker)

    def read_manifest(self, identity: tx.ReleaseIdentity) -> tx.Observation[tx.ManifestState]:
        installed, marker = self._installed_identity(), self._marker()
        if not installed or not marker:
            return tx.Observation(None, definitive=True)
        return tx.Observation(tx.ManifestState(
            str(installed.get("version") or ""), str(installed.get("manifest_sha256") or ""),
            str(marker.get("artifact_sha256") or ""),
        ))

    def verify_manifest(self, identity: tx.ReleaseIdentity, operation_key: str) -> None:
        observed = self.read_manifest(identity)
        if observed.value is None:
            raise ReleaseProviderError("installed runtime manifest is unavailable after installation")


def make_plan(root: Path, release_plan: Mapping[str, Any], artifact: Path,
              *, repository: str | None = None, commit: str | None = None,
              manifest: Path | None = None, runner: Runner = subprocess.run) -> tx.ReleasePlan:
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise ReleaseProviderError(f"release artifact does not exist: {artifact}")
    manifest = (manifest or (root / "handsoff-runtime.json")).resolve()
    if not manifest.is_file():
        raise ReleaseProviderError(f"runtime manifest does not exist: {manifest}")
    try:
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseProviderError("runtime manifest is unreadable") from exc
    version = str(release_plan.get("version") or "")
    if manifest_value.get("version") != version:
        raise ReleaseProviderError(
            f"runtime manifest version {manifest_value.get('version')!r} does not match release plan {version!r}"
        )
    identity = tx.ReleaseIdentity(
        repository or repository_from_origin(root, runner=runner), version,
        commit or head_commit(root, runner=runner), _sha256(artifact),
    )
    return tx.ReleasePlan(identity, artifact.name, _sha256(manifest))


def reconcile_release(root: Path, release_plan: Mapping[str, Any], artifact: Path, *,
                      repository: str | None = None, commit: str | None = None,
                      manifest: Path | None = None, notes_file: Path | None = None,
                      runner: Runner = subprocess.run,
                      adapter_factory: Callable[..., tx.ReleaseAdapter] = GitHubWheelReleaseAdapter,
                      python: str = sys.executable) -> dict[str, Any]:
    """Reconcile a planned release and return its verified bounded evidence."""

    root = root.resolve()
    plan = make_plan(root, release_plan, artifact, repository=repository, commit=commit,
                     manifest=manifest, runner=runner)
    store = tx.JsonFileRecordStore(root / RECORD_NAME)
    adapter = adapter_factory(root, plan, artifact, notes_file=notes_file,
                              runner=runner, python=python)
    with release_lock(root):
        record = tx.ReleaseTransaction(plan, store, adapter).reconcile()
    return tx.release_evidence(record)


def completion_error(root: Path, release_plan: Mapping[str, Any] | None) -> str | None:
    """Return why a started release transaction cannot pass the Phase-8 gate."""

    path = root / RECORD_NAME
    if not path.exists():
        return None
    if not isinstance(release_plan, Mapping):
        return "release transaction exists without a release plan"
    try:
        record = tx.JsonFileRecordStore(path).load()
        if record is None:
            return "release transaction record disappeared"
        tx.validate_record(record)
    except tx.ReleaseTransactionError as exc:
        return f"release transaction record is invalid: {exc}"
    if record["identity"]["version"] != release_plan.get("version"):
        return "release transaction version does not match the current release plan"
    if not record["complete"]:
        pending = [name for name in tx.STEP_NAMES if record["steps"][name]["state"] != "complete"]
        return f"release transaction is incomplete ({', '.join(pending)})"
    manifest = record["steps"]["installed_manifest"]
    if manifest["state"] != "complete" or not manifest.get("result"):
        return "installed runtime manifest has not been verified"
    try:
        evidence = tx.release_evidence(record)
    except tx.ReleaseTransactionError as exc:
        return f"release evidence is invalid: {exc}"
    if not evidence.get("manifest_verified"):
        return "installed runtime manifest has not been verified"
    return None
