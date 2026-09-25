import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import handsoff_release_runtime as runtime
import handsoff_release_transaction as tx
import handsoff_supervisor as supervisor


COMMIT = "a" * 40


class MemoryAdapter:
    def __init__(self, _root, plan, _artifact, **_kwargs):
        self.plan = plan
        self.tag = self.release = self.installation = self.manifest = None
        self.assets = []

    def read_tag(self, identity):
        return tx.Observation(self.tag)

    def create_annotated_tag(self, identity, _key):
        self.tag = tx.TagState(identity.version, identity.merged_commit, True, "tag")

    def read_release(self, identity):
        return tx.Observation(self.release)

    def create_release(self, identity, _key):
        self.release = tx.ReleaseState(identity.version, identity.merged_commit, "release",
                                       "https://example/release", assets_url="assets-url")

    def read_assets_url(self, _url):
        return tx.Observation(tuple(self.assets))

    def upload_wheel(self, identity, name, _key):
        self.assets.append(tx.AssetState(name, identity.artifact_sha256, "wheel",
                                         "https://example/wheel"))

    def upload_checksum(self, _identity, name, contents, _key):
        self.assets.append(tx.AssetState(name, hashlib.sha256(contents).hexdigest(), "sum",
                                         "https://example/sum", contents))

    def read_install(self, _identity):
        return tx.Observation(self.installation)

    def install(self, identity, _url, _key):
        self.installation = tx.InstallState(identity.repository, identity.version,
                                             identity.merged_commit, identity.artifact_sha256)

    def read_manifest(self, _identity):
        return tx.Observation(self.manifest)

    def verify_manifest(self, identity, _key):
        self.manifest = tx.ManifestState(identity.version, self.plan.manifest_sha256,
                                         identity.artifact_sha256)


class ReleaseRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-release-runtime-"))
        self.artifact = self.root / "project_handsoff-1.2.3-py3-none-any.whl"
        self.artifact.write_bytes(b"wheel")
        self.manifest = self.root / "handsoff-runtime.json"
        self.manifest.write_text(json.dumps({"schema": 1, "version": "v1.2.3", "files": {"x": "y"}}))
        self.release_plan = {"version": "v1.2.3"}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_supported_api_reconciles_through_installed_manifest(self):
        evidence = runtime.reconcile_release(
            self.root, self.release_plan, self.artifact, repository="owner/repo",
            commit=COMMIT, manifest=self.manifest, adapter_factory=MemoryAdapter,
        )
        self.assertTrue(evidence["manifest_verified"])
        self.assertEqual(evidence["tag"], "v1.2.3")
        record = json.loads((self.root / runtime.RECORD_NAME).read_text())
        self.assertTrue(record["complete"])
        self.assertEqual(record["steps"]["installed_manifest"]["state"], "complete")
        self.assertIsNone(runtime.completion_error(self.root, self.release_plan))

    def test_phase_8_gate_blocks_a_started_incomplete_transaction(self):
        plan = runtime.make_plan(self.root, self.release_plan, self.artifact,
                                 repository="owner/repo", commit=COMMIT, manifest=self.manifest)
        record = tx._new_record(plan, "2026-09-23T12:00:00Z")
        tx.JsonFileRecordStore(self.root / runtime.RECORD_NAME).save(record)
        self.assertIn("incomplete", runtime.completion_error(self.root, self.release_plan))

    def test_github_adapter_uses_assets_url_as_canonical_fallback(self):
        plan = runtime.make_plan(self.root, self.release_plan, self.artifact,
                                 repository="owner/repo", commit=COMMIT, manifest=self.manifest)
        checksum = plan.checksum_contents
        calls = Counter()

        def runner(argv, **kwargs):
            calls[tuple(argv[:3])] += 1
            endpoint = argv[2] if argv[:2] == ["gh", "api"] else ""
            if endpoint.endswith("releases/tags/v1.2.3"):
                body = {"tag_name": "v1.2.3", "id": 7, "html_url": "release",
                        "draft": False, "assets": [], "assets_url": "canonical-assets"}
            elif endpoint.endswith("git/ref/tags/v1.2.3"):
                body = {"object": {"type": "tag", "sha": "tag-object"}}
            elif endpoint.endswith("git/tags/tag-object"):
                body = {"object": {"sha": COMMIT}}
            elif endpoint == "canonical-assets":
                body = [{"id": 8, "name": plan.artifact_name, "browser_download_url": "wheel"},
                        {"id": 9, "name": plan.checksum_name, "browser_download_url": "sum"}]
            elif endpoint.endswith("assets/8"):
                return subprocess.CompletedProcess(argv, 0, self.artifact.read_bytes(), b"")
            elif endpoint.endswith("assets/9"):
                return subprocess.CompletedProcess(argv, 0, checksum, b"")
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

        adapter = runtime.GitHubWheelReleaseAdapter(self.root, plan, self.artifact, runner=runner)
        release = adapter.read_release(plan.identity).value
        self.assertEqual(release.assets, ())
        assets = adapter.read_assets_url(release.assets_url).value
        self.assertEqual({asset.name for asset in assets}, {plan.artifact_name, plan.checksum_name})
        self.assertEqual(next(a for a in assets if a.name == plan.checksum_name).contents, checksum)

    def test_supervisor_exposes_and_ledgers_release_reconcile(self):
        parsed = supervisor.build_parser().parse_args([
            "--root", str(self.root), "release-reconcile", "--artifact", str(self.artifact),
            "--by", "host", "--repository", "owner/repo", "--commit", COMMIT,
        ])
        self.assertEqual(parsed.command, "release-reconcile")
        status = {"phase_number": 7, "release_plan": copy.deepcopy(self.release_plan),
                  "deployment_approved": {"by": "pilot"}}
        evidence = {"tag": "v1.2.3", "manifest_verified": True}
        with mock.patch.object(supervisor.lib, "resolve_root", return_value=self.root), \
                patch_engine("load_config", return_value={}), \
                patch_engine("project_lock", return_value=nullcontext()), \
                mock.patch.object(supervisor, "_load_all", return_value=(status, {}, [], [])), \
                mock.patch.object(supervisor, "_audit_errors", return_value=[]), \
                patch_engine("adaptive_deployment_approval_required", return_value=False), \
                mock.patch.object(supervisor.release_runtime, "reconcile_release", return_value=evidence) as run, \
                mock.patch.object(supervisor, "_load", return_value=(status, {})), \
                patch_engine("commit") as commit:
            self.assertEqual(supervisor.cmd_release_reconcile(parsed), 0)
        run.assert_called_once()
        self.assertEqual(commit.call_args.kwargs["event_kind"], "release_reconciled")


if __name__ == "__main__":
    unittest.main()
