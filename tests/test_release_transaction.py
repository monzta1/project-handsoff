import copy
import hashlib
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import handsoff_release_transaction as release_tx


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
WHEEL_SHA = "1" * 64
OTHER_SHA = "2" * 64
MANIFEST_SHA = "3" * 64
REPOSITORY = "owner/project-handsoff"
VERSION = "v1.2.3"
WHEEL = "project_handsoff-1.2.3-py3-none-any.whl"


class MemoryStore:
    def __init__(self, value=None):
        self.value = copy.deepcopy(value)
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.value)

    def save(self, record):
        self.value = copy.deepcopy(dict(record))
        self.saves += 1


class FakeAdapter:
    """In-memory provider. No test in this module performs network or pip I/O."""

    def __init__(self, plan):
        self.plan = plan
        self.tag = None
        self.release = None
        self.canonical_assets = []
        self.installation = None
        self.manifest = None
        self.calls = Counter()
        self.action_attempts = Counter()
        self.operation_keys = set()
        self.drop_actions = set()
        self.definitive = {name: True for name in release_tx.STEP_NAMES}
        self.canonical_definitive = True

    def _action(self, step, key, apply):
        self.action_attempts[step] += 1
        if key in self.operation_keys:
            return
        self.operation_keys.add(key)
        self.calls[step] += 1
        if step not in self.drop_actions:
            apply()

    def read_tag(self, identity):
        return release_tx.Observation(self.tag, self.definitive["annotated_tag"])

    def create_annotated_tag(self, identity, operation_key):
        self._action("annotated_tag", operation_key, lambda: setattr(
            self, "tag", release_tx.TagState(identity.version, identity.merged_commit,
                                               True, "tag-object-1")))

    def read_release(self, identity):
        return release_tx.Observation(self.release, self.definitive["release"])

    def create_release(self, identity, operation_key):
        self._action("release", operation_key, lambda: setattr(
            self, "release", release_tx.ReleaseState(
                identity.version, identity.merged_commit, "release-1",
                f"https://example.test/{identity.repository}/releases/{identity.version}",
                assets_url="https://api.example.test/releases/1/assets")))

    def read_assets_url(self, assets_url):
        return release_tx.Observation(tuple(self.canonical_assets), self.canonical_definitive)

    def _append_asset(self, asset):
        if not any(existing.name == asset.name for existing in self.canonical_assets):
            self.canonical_assets.append(asset)

    def upload_wheel(self, identity, name, operation_key):
        asset = release_tx.AssetState(
            name, identity.artifact_sha256, "wheel-1",
            f"https://example.test/download/{identity.version}/{name}")
        self._action("wheel_asset", operation_key, lambda: self._append_asset(asset))

    def upload_checksum(self, identity, name, contents, operation_key):
        asset = release_tx.AssetState(
            name, hashlib.sha256(contents).hexdigest(), "checksum-1",
            f"https://example.test/download/{identity.version}/{name}", contents)
        self._action("checksum_asset", operation_key, lambda: self._append_asset(asset))

    def read_install(self, identity):
        return release_tx.Observation(self.installation, self.definitive["install"])

    def install(self, identity, artifact_url, operation_key):
        self._action("install", operation_key, lambda: setattr(
            self, "installation", release_tx.InstallState(
                identity.repository, identity.version, identity.merged_commit,
                identity.artifact_sha256)))

    def read_manifest(self, identity):
        return release_tx.Observation(self.manifest, self.definitive["installed_manifest"])

    def verify_manifest(self, identity, operation_key):
        self._action("installed_manifest", operation_key, lambda: setattr(
            self, "manifest", release_tx.ManifestState(
                identity.version, self.plan.manifest_sha256, identity.artifact_sha256)))

    def make_all_exact(self):
        identity = self.plan.identity
        self.tag = release_tx.TagState(identity.version, identity.merged_commit, True,
                                       "tag-object-1")
        wheel = release_tx.AssetState(
            self.plan.artifact_name, identity.artifact_sha256, "wheel-1",
            f"https://example.test/download/{identity.version}/{self.plan.artifact_name}")
        checksum = release_tx.AssetState(
            str(self.plan.checksum_name), self.plan.checksum_sha256, "checksum-1",
            f"https://example.test/download/{identity.version}/{self.plan.checksum_name}",
            self.plan.checksum_contents)
        self.canonical_assets = [wheel, checksum]
        self.release = release_tx.ReleaseState(
            identity.version, identity.merged_commit, "release-1",
            f"https://example.test/{identity.repository}/releases/{identity.version}",
            assets=(wheel, checksum),
            assets_url="https://api.example.test/releases/1/assets")
        self.installation = release_tx.InstallState(
            identity.repository, identity.version, identity.merged_commit,
            identity.artifact_sha256)
        self.manifest = release_tx.ManifestState(
            identity.version, self.plan.manifest_sha256, identity.artifact_sha256)


class Crash(RuntimeError):
    pass


def plan(artifact_sha=WHEEL_SHA):
    return release_tx.ReleasePlan(
        release_tx.ReleaseIdentity(REPOSITORY, VERSION, COMMIT, artifact_sha),
        WHEEL,
        MANIFEST_SHA,
    )


class ReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.store = MemoryStore()
        self.adapter = FakeAdapter(self.plan)
        self.clock = lambda: datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

    def transaction(self, **kwargs):
        return release_tx.ReleaseTransaction(
            self.plan, self.store, self.adapter, clock=self.clock, **kwargs)

    def assert_invariant(self, invariant, callback):
        with self.assertRaises(release_tx.InvariantViolation) as caught:
            callback()
        self.assertEqual(caught.exception.invariant, invariant)
        self.assertIn(f"{invariant} invariant failed", str(caught.exception))

    def test_new_transaction_completes_and_reports_full_release_evidence(self):
        record = self.transaction().reconcile()
        self.assertTrue(record["complete"])
        self.assertEqual(set(record["steps"]), set(release_tx.STEP_NAMES))
        self.assertEqual(self.adapter.calls, Counter({name: 1 for name in release_tx.STEP_NAMES}))
        evidence = release_tx.release_evidence(record)
        self.assertEqual(evidence, {
            "repository": REPOSITORY,
            "tag": VERSION,
            "merged_commit": COMMIT,
            "release_url": f"https://example.test/{REPOSITORY}/releases/{VERSION}",
            "artifact_names": [WHEEL, f"{WHEEL}.sha256"],
            "artifact_sha256": WHEEL_SHA,
            "installed_version": VERSION,
            "manifest_sha256": MANIFEST_SHA,
            "manifest_verified": True,
        })
        for step in release_tx.STEP_NAMES:
            self.assertEqual(
                [item["state"] for item in record["steps"][step]["transitions"]],
                ["intent", "action", "read_back", "complete"],
            )

    def test_exact_remote_and_local_objects_are_adopted_without_actions(self):
        self.adapter.make_all_exact()
        record = self.transaction().reconcile()
        self.assertEqual(self.adapter.calls, Counter())
        for step in release_tx.STEP_NAMES:
            self.assertEqual(record["steps"][step]["result"]["source"], "adopted")
            self.assertEqual(
                [item["state"] for item in record["steps"][step]["transitions"]],
                ["intent", "read_back", "complete"],
            )

    def test_completed_retry_revalidates_every_invariant_without_actions(self):
        first = self.transaction().reconcile()
        attempts = self.adapter.action_attempts.copy()
        second = self.transaction().reconcile()
        self.assertEqual(second, first)
        self.assertEqual(self.adapter.action_attempts, attempts)
        self.adapter.manifest = release_tx.ManifestState(VERSION, OTHER_SHA, WHEEL_SHA)
        self.assert_invariant("installed_manifest.sha256", self.transaction().reconcile)

    def test_every_step_resumes_at_every_durable_crash_boundary(self):
        points = (
            "intent_persisted", "pre_read", "action_persisted", "action_performed",
            "read_back_observed", "read_back_persisted", "completion_persisted",
        )
        for step in release_tx.STEP_NAMES:
            for point in points:
                with self.subTest(step=step, point=point):
                    current_plan = plan()
                    store = MemoryStore()
                    adapter = FakeAdapter(current_plan)
                    crashed = False

                    def checkpoint(seen_point, seen_step, _record):
                        nonlocal crashed
                        if not crashed and (seen_point, seen_step) == (point, step):
                            crashed = True
                            raise Crash(f"{step}:{point}")

                    transaction = release_tx.ReleaseTransaction(
                        current_plan, store, adapter, clock=self.clock,
                        checkpoint=checkpoint)
                    with self.assertRaises(Crash):
                        transaction.reconcile()
                    completed = release_tx.ReleaseTransaction(
                        current_plan, store, adapter, clock=self.clock).reconcile()
                    self.assertTrue(completed["complete"])
                    self.assertEqual(adapter.calls,
                                     Counter({name: 1 for name in release_tx.STEP_NAMES}))
                    for name in release_tx.STEP_NAMES:
                        names = [transition["state"]
                                 for transition in completed["steps"][name]["transitions"]]
                        self.assertEqual(len(names), len(set(names)))
                        self.assertLessEqual(len(names), release_tx.MAX_TRANSITIONS)

    def test_crash_before_aggregate_completion_resumes_without_side_effects(self):
        crashed = False

        def checkpoint(point, _step, _record):
            nonlocal crashed
            if point == "transaction_completed" and not crashed:
                crashed = True
                raise Crash(point)

        with self.assertRaises(Crash):
            self.transaction(checkpoint=checkpoint).reconcile()
        attempts = self.adapter.action_attempts.copy()
        self.assertTrue(self.transaction().reconcile()["complete"])
        self.assertEqual(self.adapter.action_attempts, attempts)

    def test_lost_action_response_is_adopted_on_retry(self):
        crashed = False

        def checkpoint(point, step, _record):
            nonlocal crashed
            if point == "action_performed" and step == "wheel_asset" and not crashed:
                crashed = True
                raise Crash(point)

        with self.assertRaises(Crash):
            self.transaction(checkpoint=checkpoint).reconcile()
        self.assertEqual(self.adapter.calls["wheel_asset"], 1)
        self.assertTrue(self.transaction().reconcile()["complete"])
        self.assertEqual(self.adapter.calls["wheel_asset"], 1)
        names = [asset.name for asset in self.adapter.canonical_assets]
        self.assertEqual(names.count(WHEEL), 1)

    def test_provider_eventual_consistency_is_retryable_and_distinct_from_missing(self):
        original = self.adapter.read_assets_url
        lag_once = True

        def lagged(url):
            nonlocal lag_once
            answer = original(url)
            if (lag_once and self.adapter.calls["wheel_asset"]
                    and any(asset.name == WHEEL for asset in answer.value)):
                lag_once = False
                return release_tx.Observation((), definitive=False)
            return answer

        self.adapter.read_assets_url = lagged
        with self.assertRaises(release_tx.ProviderConsistencyPending) as caught:
            self.transaction().reconcile()
        self.assertEqual(caught.exception.reason, "provider_eventual_consistency")
        self.assertEqual(caught.exception.invariant, "wheel_asset.presence")
        self.assertTrue(self.transaction().reconcile()["complete"])
        self.assertEqual(self.adapter.calls["wheel_asset"], 1)

    def test_genuinely_missing_wheel_fails_the_presence_invariant(self):
        self.adapter.drop_actions.add("wheel_asset")
        self.assert_invariant("wheel_asset.presence", self.transaction().reconcile)
        self.assertFalse(self.store.value["complete"])
        self.assertEqual(self.store.value["steps"]["wheel_asset"]["state"], "action")

    def test_empty_embedded_assets_fall_back_to_canonical_assets_url(self):
        self.adapter.make_all_exact()
        self.adapter.release = release_tx.ReleaseState(
            VERSION, COMMIT, "release-1", "https://example.test/release",
            assets=(), assets_url="https://api.example.test/releases/1/assets")
        record = self.transaction().reconcile()
        self.assertTrue(record["complete"])
        self.assertEqual(self.adapter.calls, Counter())

    def test_stale_embedded_asset_falls_back_to_exact_canonical_asset(self):
        self.adapter.make_all_exact()
        stale = release_tx.AssetState(WHEEL, OTHER_SHA, "stale-wheel", "https://stale")
        checksum = self.adapter.canonical_assets[1]
        self.adapter.release = release_tx.ReleaseState(
            VERSION, COMMIT, "release-1", "https://example.test/release",
            assets=(stale, checksum), assets_url="https://api.example.test/releases/1/assets")
        self.assertTrue(self.transaction().reconcile()["complete"])
        self.assertEqual(self.store.value["steps"]["wheel_asset"]["result"]["identifier"],
                         "wheel-1")

    def test_conflicting_canonical_asset_is_never_deleted_or_replaced(self):
        self.adapter.make_all_exact()
        wrong = release_tx.AssetState(WHEEL, OTHER_SHA, "wheel-wrong", "https://wrong")
        self.adapter.canonical_assets[0] = wrong
        self.adapter.release = release_tx.ReleaseState(
            VERSION, COMMIT, "release-1", "https://example.test/release",
            assets=(wrong,), assets_url="https://api.example.test/releases/1/assets")
        before = copy.deepcopy(self.adapter.canonical_assets)
        self.assert_invariant("wheel_asset.sha256", self.transaction().reconcile)
        self.assertEqual(self.adapter.canonical_assets, before)
        self.assertEqual(self.adapter.calls["wheel_asset"], 0)

    def test_lightweight_tag_names_its_exact_failed_invariant(self):
        self.adapter.tag = release_tx.TagState(VERSION, COMMIT, False, "lightweight")
        self.assert_invariant("annotated_tag.kind", self.transaction().reconcile)
        self.assertEqual(self.adapter.calls["annotated_tag"], 0)

    def test_wrong_tag_commit_names_its_exact_failed_invariant(self):
        self.adapter.tag = release_tx.TagState(VERSION, OTHER_COMMIT, True, "tag-object")
        self.assert_invariant("annotated_tag.commit", self.transaction().reconcile)

    def test_wrong_release_commit_names_its_exact_failed_invariant(self):
        self.adapter.make_all_exact()
        self.adapter.release = release_tx.ReleaseState(
            VERSION, OTHER_COMMIT, "release-1", "https://example.test/release")
        self.assert_invariant("release.commit", self.transaction().reconcile)

    def test_checksum_mismatch_names_its_exact_failed_invariant(self):
        self.adapter.make_all_exact()
        wrong = release_tx.AssetState(
            str(self.plan.checksum_name), self.plan.checksum_sha256, "checksum-1",
            "https://example.test/checksum", b"wrong checksum\n")
        self.adapter.canonical_assets[1] = wrong
        self.adapter.release = release_tx.ReleaseState(
            VERSION, COMMIT, "release-1", "https://example.test/release",
            assets=(self.adapter.canonical_assets[0], wrong),
            assets_url="https://api.example.test/releases/1/assets")
        self.assert_invariant("checksum_asset.contents", self.transaction().reconcile)

    def test_install_identity_mismatch_blocks_completion(self):
        self.adapter.make_all_exact()
        self.adapter.installation = release_tx.InstallState(
            REPOSITORY, VERSION, COMMIT, OTHER_SHA)
        self.assert_invariant("install.artifact_sha256", self.transaction().reconcile)

    def test_installed_manifest_mismatch_blocks_completion(self):
        self.adapter.make_all_exact()
        self.adapter.manifest = release_tx.ManifestState(VERSION, OTHER_SHA, WHEEL_SHA)
        self.assert_invariant("installed_manifest.sha256", self.transaction().reconcile)
        self.assertFalse(self.store.value["complete"])

    def test_duplicate_assets_refuse_instead_of_guessing(self):
        self.adapter.make_all_exact()
        duplicate = copy.deepcopy(self.adapter.canonical_assets[0])
        self.adapter.canonical_assets.append(duplicate)
        self.adapter.release = release_tx.ReleaseState(
            VERSION, COMMIT, "release-1", "https://example.test/release",
            assets=(), assets_url="https://api.example.test/releases/1/assets")
        self.assert_invariant("wheel_asset.uniqueness", self.transaction().reconcile)

    def test_retry_with_changed_immutable_identity_is_refused_before_reads(self):
        self.transaction().reconcile()
        original_attempts = self.adapter.action_attempts.copy()
        changed = plan(OTHER_SHA)
        with self.assertRaises(release_tx.IdentityConflict) as caught:
            release_tx.ReleaseTransaction(changed, self.store, self.adapter,
                                          clock=self.clock).reconcile()
        self.assertEqual(caught.exception.invariant,
                         "release_transaction.identity.artifact_sha256")
        self.assertEqual(self.adapter.action_attempts, original_attempts)

    def test_authenticated_legacy_record_migrates_pending_then_adopts(self):
        self.adapter.make_all_exact()
        legacy = {
            "schema_version": 0,
            "repository": REPOSITORY,
            "version": VERSION,
            "merged_commit": COMMIT,
            "artifact_sha256": WHEEL_SHA,
            "tag": VERSION,
            "release_url": "https://legacy.example/release",
            "artifact_names": [WHEEL],
            "installed_version": VERSION,
            "manifest_verified": True,
        }
        self.store = MemoryStore(legacy)
        record = self.transaction(legacy_authenticated=True).reconcile()
        self.assertEqual(record["schema_version"], 1)
        self.assertTrue(record["complete"])
        self.assertEqual(self.adapter.calls, Counter())
        self.assertTrue(all(step["result"]["source"] == "adopted"
                            for step in record["steps"].values()))

    def test_legacy_evidence_is_not_synthesized_complete(self):
        legacy = {
            "repository": REPOSITORY,
            "version": VERSION,
            "merged_commit": COMMIT,
            "artifact_sha256": WHEEL_SHA,
            "manifest_verified": True,
        }
        self.store = MemoryStore(legacy)
        self.adapter.drop_actions.add("annotated_tag")
        self.assert_invariant(
            "annotated_tag.presence",
            self.transaction(legacy_authenticated=True).reconcile,
        )
        self.assertFalse(self.store.value["complete"])
        self.assertEqual(self.store.value["steps"]["annotated_tag"]["state"], "action")

    def test_unauthenticated_legacy_record_is_read_only(self):
        self.store = MemoryStore({
            "repository": REPOSITORY,
            "version": VERSION,
            "merged_commit": COMMIT,
            "artifact_sha256": WHEEL_SHA,
        })
        self.assertFalse(self.transaction().inspect()["mutable"])
        with self.assertRaisesRegex(release_tx.RecordValidationError,
                                    "authenticated migration"):
            self.transaction().reconcile()

    def test_unknown_record_version_is_inspectable_but_refuses_mutation(self):
        unknown = {"schema_version": 99, "opaque_future_field": {"anything": True}}
        self.store = MemoryStore(unknown)
        self.assertEqual(self.transaction().inspect(), {
            "present": True, "mutable": False, "schema_version": 99,
        })
        with self.assertRaises(release_tx.UnknownRecordVersion) as caught:
            self.transaction().reconcile()
        self.assertEqual(caught.exception.version, 99)
        self.assertEqual(self.store.value, unknown)

    def test_closed_record_rejects_unknown_fields_and_unbounded_transitions(self):
        record = self.transaction().reconcile()
        extra = copy.deepcopy(record)
        extra["unexpected"] = "not allowed"
        with self.assertRaisesRegex(release_tx.RecordValidationError, "fields are closed"):
            release_tx.validate_record(extra)
        unbounded = copy.deepcopy(record)
        unbounded["steps"]["release"]["transitions"] *= 5
        with self.assertRaisesRegex(release_tx.RecordValidationError, "unbounded"):
            release_tx.validate_record(unbounded)

    def test_recorded_remote_identifier_cannot_silently_change(self):
        self.transaction().reconcile()
        self.adapter.tag = release_tx.TagState(VERSION, COMMIT, True, "replacement-tag")
        self.assert_invariant("annotated_tag.recorded_identifier",
                              self.transaction().reconcile)


if __name__ == "__main__":
    unittest.main()
