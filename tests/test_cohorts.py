"""#301: cohort queries over the #300 routing evidence projection.

Every fixture is built so the exact value it asserts can only come from the
rule under test: the partial-usage record would move the token mean from 3000
to 6482 if it were counted, twelve sessions on each of two versions would make
twenty-four if they were combined, and a record with no timestamp would change
the cohort size if it were not excluded.
"""
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_cohorts as cohorts  # noqa: E402
import handsoff_evidence as evidence  # noqa: E402

UNKNOWN = evidence.UNKNOWN
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
KEY = {"repository": "acme/app", "task_class": "bug", "phase": 5, "adapter": "claude",
       "model": "opus", "policy_version": "p1"}


def at(hours=0.0, days=0.0, offset_hours=0):
    moment = NOW - timedelta(hours=hours, days=days)
    return moment.astimezone(timezone(timedelta(hours=offset_hours))).isoformat()


def observation(n, *, repository="acme/app", task_class="bug", phase=5, adapter="claude",
                requested="opus", reported=UNKNOWN, policy="p1", outcome="verified_phase8",
                wall_ms=60000, tokens=None, ended=None, started=None, run_kind="product",
                derivation=evidence.DERIVATION_VERSION, source="a" * 64):
    tokens_in, tokens_out, tokens_total = tokens or (UNKNOWN, UNKNOWN, UNKNOWN)
    record = {
        "derivation_version": derivation, "source_archive": f"run-{n}.json", "source_sha256": source,
        "repository": repository, "engine_version": "0.4.7", "session_id": f"hs-{n}",
        "role": "implementer", "task_class": task_class, "risk_class": "standard", "phase": phase,
        "adapter": adapter, "requested_model": requested, "reported_model": reported, "tier": UNKNOWN,
        "routed": False,
        "tokens": {"tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_total": tokens_total,
                   "source": "adapter"},
        "duration": {"wall_clock_ms": UNKNOWN if wall_ms is None else wall_ms, "active_ms": UNKNOWN},
        "retries": 0, "replacements": 0, "outcome": outcome, "policy_version": policy,
        "quality_flags": [],
    }
    record["negative"] = outcome in evidence.NEGATIVE_OUTCOMES
    record["record_hash"] = evidence.record_hash(record)
    return {"evidence": record, "run_kind": run_kind, "started_at": started,
            "ended_at": at(hours=1) if ended is None and started is None else ended}


def many(count, start=0, **fields):
    return [observation(start + i, **fields) for i in range(count)]


def query(observations, cfg=None, key=None, now=NOW.isoformat()):
    return cohorts.aggregate(observations, evidence.DERIVATION_VERSION, now, cfg, key or KEY)


def mostly_missing_usage():
    """25 sessions: 20 known outcomes (15 verified), 21 known wall times, and
    full usage on only three; a fourth carries a total without its split."""
    usage = {0: (100, 900, 1000), 1: (500, 1500, 2000), 2: (1000, 5000, 6000), 3: (None, None, 16928)}
    observations = []
    for i in range(25):
        outcome = "verified_phase8" if i < 15 else "failed_other" if i < 20 else UNKNOWN
        observations.append(observation(
            i, outcome=outcome, wall_ms=(i + 1) * 1000 if i <= 20 else None,
            tokens=tuple(UNKNOWN if v is None else v for v in usage[i]) if i in usage else None,
            ended=at(hours=i + 1)))
    return observations


class MetricsTest(unittest.TestCase):
    """REQ-001."""

    def test_exact_metrics_on_a_mostly_missing_usage_fixture(self):
        result = query(mostly_missing_usage())
        self.assertEqual(len(result["cohorts"]), 1)
        cohort = result["cohorts"][0]
        self.assertEqual(cohort["size"], 25)
        self.assertEqual(cohort["coverage"], 3 / 25)
        self.assertEqual(cohort["age"], {"newest_s": 3600, "oldest_s": 25 * 3600})
        self.assertEqual(cohort["metrics"]["success_rate"],
                         {"value": 0.75, "n_known": 20, "missing_rate": 1 - 20 / 25,
                          "reason": None, "shortfall": 0})
        self.assertEqual(cohort["metrics"]["median_wall_seconds"],
                         {"value": 11.0, "n_known": 21, "missing_rate": 1 - 21 / 25,
                          "reason": None, "shortfall": 0})
        self.assertEqual(cohort["metrics"]["mean_total_tokens"],
                         {"value": None, "n_known": 3, "missing_rate": 1 - 3 / 25,
                          "reason": "below_min_sample", "shortfall": 17})
        self.assertTrue(result["sufficient"])
        self.assertEqual(result["widened"], [])

    def test_token_mean_counts_only_full_usage(self):
        cohort = query(mostly_missing_usage(), cfg={"min_sample": 3})["cohorts"][0]
        self.assertEqual(cohort["metrics"]["mean_total_tokens"]["value"], 3000.0)
        self.assertEqual(cohort["metrics"]["mean_total_tokens"]["n_known"], 3)

    def test_min_sample_defaults_to_twenty_per_metric(self):
        self.assertEqual(cohorts.DEFAULT_CONFIG["min_sample"], 20)
        cohort = query(mostly_missing_usage()[:19] + mostly_missing_usage()[20:21])["cohorts"][0]
        # 15 verified + 4 failed + 1 UNKNOWN: 19 known outcomes, one short.
        self.assertEqual(cohort["metrics"]["success_rate"],
                         {"value": None, "n_known": 19, "missing_rate": 1 - 19 / 20,
                          "reason": "below_min_sample", "shortfall": 1})
        self.assertEqual(cohort["metrics"]["median_wall_seconds"]["value"], 10.5)

    def test_zero_known_values_are_null_with_no_observations(self):
        result = query(many(5, outcome=UNKNOWN, wall_ms=None))
        cohort = result["cohorts"][0]
        for name in cohorts.METRICS:
            self.assertEqual(cohort["metrics"][name],
                             {"value": None, "n_known": 0, "missing_rate": 1.0,
                              "reason": "no_observations", "shortfall": 20})
        self.assertEqual(cohort["coverage"], 0.0)
        self.assertFalse(result["sufficient"])


class VersionTest(unittest.TestCase):
    """REQ-002."""

    def test_version_is_reported_model_else_requested(self):
        record = observation(0, requested="opus", reported="opus-5-5")["evidence"]
        self.assertEqual(cohorts.model_version(record), "opus-5-5")
        record = observation(0, requested="opus", reported=UNKNOWN)["evidence"]
        self.assertEqual(cohorts.model_version(record), "opus")
        del record["reported_model"]
        self.assertEqual(cohorts.model_version(record), "opus")

    def test_absent_reports_group_under_the_requested_model(self):
        result = query(many(20, reported=UNKNOWN))
        self.assertEqual([cohort["key"]["model"] for cohort in result["cohorts"]], ["opus"])
        self.assertTrue(result["sufficient"])

    def test_combining_versions_never_qualifies(self):
        observations = many(12, reported="opus-5-0") + many(12, start=12, reported="opus-5-5")
        result = query(observations)
        self.assertFalse(result["sufficient"])
        self.assertEqual([(c["key"]["model"], c["size"], c["metrics"]["success_rate"]["shortfall"])
                          for c in result["cohorts"]],
                         [("opus-5-0", 12, 8), ("opus-5-5", 12, 8)])
        self.assertEqual([step["dimension"] for step in result["widened"]],
                         list(cohorts.WIDENING_ORDER))
        # The same 24 on one version qualify at the exact key.
        single = query(many(24, reported="opus-5-5"))
        self.assertTrue(single["sufficient"])
        self.assertEqual(single["widened"], [])

    def test_a_widening_that_combines_versions_stays_insufficient(self):
        observations = many(12, reported="opus-5-0") + many(12, start=12, reported="opus-5-5", policy="p0")
        result = query(observations)
        self.assertFalse(result["sufficient"])
        self.assertEqual(result["widened"][0], {"dimension": "policy_version", "reason": "sparse"})
        self.assertEqual(sorted(c["size"] for c in result["cohorts"]), [12, 12])

    def test_candidate_not_selected_within_window_is_stale(self):
        stale = query(many(20, ended=at(days=31)))["cohorts"][0]
        self.assertTrue(stale["stale_or_biased"])
        self.assertEqual(stale["last_selected_at"], (NOW - timedelta(days=31)).isoformat())
        fresh = query(many(20, ended=at(days=29)))["cohorts"][0]
        self.assertFalse(fresh["stale_or_biased"])
        self.assertEqual(fresh["last_selected_at"], (NOW - timedelta(days=29)).isoformat())
        self.assertEqual(cohorts.DEFAULT_CONFIG["staleness_days"], 30)

    def test_candidate_never_selected_on_its_adapter_is_stale(self):
        result = query(many(20, adapter="codex"))
        self.assertEqual(result["widened"][-1], {"dimension": "adapter", "reason": "never_populated"})
        cohort = result["cohorts"][0]
        self.assertTrue(cohort["stale_or_biased"])
        self.assertIsNone(cohort["last_selected_at"])


class WideningTest(unittest.TestCase):
    """REQ-003."""

    def test_order_is_fixed_and_documented(self):
        self.assertEqual(cohorts.WIDENING_ORDER, ("policy_version", "phase", "task_class", "adapter"))
        self.assertIn("policy_version, then phase,\nthen task_class, then adapter", cohorts.__doc__)

    def test_widening_stops_at_the_first_sufficient_step(self):
        result = query(many(5) + many(20, start=5, policy="p0"))
        self.assertEqual(result["widened"], [{"dimension": "policy_version", "reason": "sparse"}])
        self.assertEqual(result["cohorts"][0]["size"], 25)

    def test_never_populated_is_distinguished_from_sparse(self):
        result = query(many(20, policy="p0"), key=dict(KEY, policy_version="p9"))
        self.assertEqual(result["widened"], [{"dimension": "policy_version", "reason": "never_populated"}])

    def test_every_dimension_in_order(self):
        observations = many(1) + many(20, start=1, policy="p0", phase=6, task_class="feature", adapter="codex")
        result = query(observations)
        self.assertEqual(result["widened"], [{"dimension": name, "reason": "sparse"}
                                             for name in cohorts.WIDENING_ORDER])
        self.assertEqual(result["cohorts"][0]["size"], 21)

    def test_unknown_task_class_matches_only_unknown(self):
        observations = many(1, task_class=UNKNOWN) + many(20, start=1, task_class="bug")
        result = query(observations, key=dict(KEY, task_class=UNKNOWN))
        self.assertEqual(result["cohorts"][0]["size"], 1)
        self.assertEqual([step["dimension"] for step in result["widened"]],
                         ["policy_version", "phase", "adapter"])

    def test_known_task_class_never_widens_into_unknown(self):
        observations = many(1, task_class="bug") + many(20, start=1, task_class=UNKNOWN)
        result = query(observations)
        self.assertIn({"dimension": "task_class", "reason": "sparse"}, result["widened"])
        self.assertEqual(result["cohorts"][0]["size"], 1)
        widened = query(many(1) + many(20, start=1, task_class="feature"))
        self.assertEqual(widened["cohorts"][0]["size"], 21)


class InputsTest(unittest.TestCase):
    """REQ-004."""

    def test_retention_defaults(self):
        self.assertEqual(cohorts.DEFAULT_CONFIG["retention_per_repository"], 2000)
        self.assertEqual(cohorts.DEFAULT_CONFIG["retention_fleet"], 20000)

    def test_retention_prunes_oldest_first_and_counts(self):
        repo_a = [observation(i, ended=at(hours=i)) for i in range(1, 6)]
        repo_b = [observation(10 + i, repository="acme/other", ended=at(hours=hours))
                  for i, hours in enumerate((0.5, 1.5))]
        result = query(repo_a + repo_b, cfg={"min_sample": 1, "retention_per_repository": 3,
                                             "retention_fleet": 4})
        self.assertEqual(result["pruned"], {"repository": 2, "fleet": 1})
        cohort = result["cohorts"][0]
        self.assertEqual(cohort["size"], 2)
        self.assertEqual(cohort["age"], {"newest_s": 3600, "oldest_s": 7200})

    def test_fixture_records_never_enter_a_cohort(self):
        def archive(repo, run_kind=None):
            body = {"repo": repo, "status": {
                "status": "complete", "phase_number": 8,
                "tranche_approval": {"labels": {"#1": ["bug"]}},
                "agent_sessions": {"hs-1": {
                    "session_id": "hs-1", "adapter": "claude", "requested_model": "opus",
                    "phase_number": 5, "state": "completed", "started_at": at(hours=2),
                    "ended_at": at(hours=1), "result": {"payload": {"decision": "approved"}}}}}}
            if run_kind:
                body["run_kind"] = run_kind
            return body
        observations = []
        for name, body in (("product.json", archive("acme/app", "product")),
                           ("kind.json", archive("acme/app", "test")),
                           ("legacy.json", archive("handsoff-test-acme"))):
            observations += cohorts.observations_from_archive(body, source_sha256=name * 4,
                                                              source_name=name)
        self.assertEqual([obs["run_kind"] for obs in observations], ["product", "test", "test"])
        # A legacy observation with no recorded kind is still classified by the #317 rule.
        observations.append(dict(observations[2], run_kind=None))
        key = dict(KEY, policy_version=UNKNOWN)
        result = query(observations, cfg={"min_sample": 1}, key=key)
        self.assertEqual(result["excluded"]["fixture"], 3)
        self.assertEqual(result["cohorts"][0]["size"], 1)
        self.assertEqual(result["cohorts"][0]["metrics"]["success_rate"]["value"], 1.0)
        self.assertEqual(result["sources"], ["product.jsonproduct.jsonproduct.jsonproduct.json"])

    def test_observed_at_is_ended_else_started_and_missing_is_counted(self):
        observations = [observation(0, ended=at(hours=1)),
                        observation(1, ended=None, started=at(hours=5)),
                        observation(2, ended=None, started=None),
                        observation(3, ended="2026-10-02T00:00:00", started=None)]
        observations[2]["ended_at"] = None
        result = query(observations, cfg={"min_sample": 1})
        self.assertEqual(result["excluded"]["no_observed_at"], 2)
        self.assertEqual(result["cohorts"][0]["size"], 2)
        self.assertEqual(result["cohorts"][0]["age"], {"newest_s": 3600, "oldest_s": 5 * 3600})
        both = observation(4, ended=at(hours=1), started=at(hours=9))
        self.assertEqual(cohorts.observed_at(both), NOW - timedelta(hours=1))

    def test_equivalent_utc_offsets_give_identical_bytes(self):
        utc = [observation(i, ended=at(hours=i + 1)) for i in range(20)]
        shifted = [observation(i, ended=at(hours=i + 1, offset_hours=(i % 5) - 2)) for i in range(20)]
        self.assertNotEqual(utc[3]["ended_at"], shifted[3]["ended_at"])
        later_now = NOW.astimezone(timezone(timedelta(hours=9))).isoformat()
        self.assertEqual(cohorts.canonical_json(query(utc)),
                         cohorts.canonical_json(query(shifted, now=later_now)))

    def test_shuffled_records_give_identical_bytes(self):
        observations = (mostly_missing_usage() + many(12, start=40, reported="opus-5-5")
                        + many(6, start=60, policy="p0", ended=at(days=2)))
        expected = cohorts.canonical_json(query(observations))
        for seed in range(5):
            shuffled = list(observations)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(cohorts.canonical_json(query(shuffled)), expected)

    def test_result_binds_its_inputs(self):
        result = query(many(20))
        self.assertEqual(result["derivation_version"], evidence.DERIVATION_VERSION)
        self.assertEqual(result["now"], NOW.isoformat())
        self.assertEqual(result["config"], cohorts.DEFAULT_CONFIG)
        self.assertEqual(result["sources"], ["a" * 64])
        other = cohorts.aggregate(many(20), evidence.DERIVATION_VERSION + 1, NOW.isoformat(), None, KEY)
        self.assertEqual(other["excluded"]["derivation_mismatch"], 20)
        self.assertEqual(other["cohorts"], [])


if __name__ == "__main__":
    unittest.main()
