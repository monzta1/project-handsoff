"""#303: opt-in evidence-assisted routing selection.

The off-path fixture `tests/fixtures/evidence_routing_baseline.json` was
captured from the launch path before #303 changed any routing code, so the
comparison is against the old behaviour, not the new module's own output.
"""
import json
import random
import shutil
import sys
import unittest
from copy import deepcopy
from unittest import mock

from tests.test_handsoff_supervisor import ROOT, HandsoffTestCase, run

sys.path.insert(0, str(ROOT / "bin"))
import handsoff_agent as agent  # noqa: E402
import handsoff_evidence as evidence  # noqa: E402
import handsoff_evidence_routing as er  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_shadow as shadow  # noqa: E402
from tests.fixture_state import write_version_pin  # noqa: E402
from tests.test_shadow_evaluator import BASELINE, BOUNDARY, COSTS, OPUS, THRESHOLD  # noqa: E402
from tests.test_shadow_evaluator import many as shadow_many  # noqa: E402
from tests.test_shadow_evaluator import training_success  # noqa: E402

BASELINE_FIXTURE = ROOT / "tests" / "fixtures" / "evidence_routing_baseline.json"

MATRIX_RISKS = ("routine", "elevated", "security_sensitive", "irreversible")
MATRIX_BUDGETS = {"uncapped": "", "capped": "\n[routing_budgets.per_mission]\ntotal_calls = 10\n"}
MATRIX_CANDIDATES = {"claude_only": ("claude",), "claude_codex": ("claude", "codex")}

NOW = "2026-10-01T00:00:00+00:00"
RECENT = "2026-09-25T00:00:00+00:00"
OLD = "2026-06-01T00:00:00+00:00"
KEY = {"repository": "acme/app", "task_class": "bug", "phase": 5, "policy_version": "p1"}
CFG = {"threshold": 0.5, "min_sample": 20, "staleness_days": 30}
ACTIVATION = {"activation_id": "era-0123456789abcdef"}
HAIKU = deepcopy(lib.ADAPTIVE_DEFAULT_PROFILES["FAST"])
SONNET = deepcopy(lib.ADAPTIVE_DEFAULT_PROFILES["STANDARD"])
OPUS_PROFILE = deepcopy(lib.ADAPTIVE_DEFAULT_PROFILES["PREMIUM"])
SWITCH_ON = "\n[adaptive_routing]\nevidence_assisted = true\n"

_serial = iter(range(1, 10 ** 6))


def obs(profile, *, outcome="verified_phase8", tokens=(1000, 500), reported=None, ended=RECENT,
        key=KEY, role="implementer", flags=()):
    """One #301 observation for `profile`."""
    n = next(_serial)
    record = {
        "derivation_version": evidence.DERIVATION_VERSION, "source_archive": f"run-{n}.json",
        "source_sha256": "b" * 64, "repository": key["repository"], "engine_version": "0.4.10",
        "session_id": f"hs-{n}", "role": role, "task_class": key["task_class"], "risk_class": "routine",
        "phase": key["phase"], "adapter": profile["adapter"], "requested_model": profile["model"],
        "reported_model": reported or evidence.UNKNOWN, "tier": evidence.UNKNOWN, "routed": False,
        "tokens": {"tokens_in": tokens[0], "tokens_out": tokens[1], "tokens_total": tokens[0] + tokens[1],
                   "source": "adapter"},
        "duration": {"wall_clock_ms": 60000, "active_ms": evidence.UNKNOWN},
        "retries": 0, "replacements": 0, "outcome": outcome, "policy_version": key["policy_version"],
        "quality_flags": sorted(flags),
    }
    record["negative"] = outcome in evidence.NEGATIVE_OUTCOMES
    record["record_hash"] = evidence.record_hash(record)
    return {"evidence": record, "run_kind": "product", "started_at": None, "ended_at": ended}


def batch(profile, successes, failures=0, **fields):
    return ([obs(profile, **fields) for _ in range(successes)]
            + [obs(profile, outcome="verification_failed", **fields) for _ in range(failures)])


def cand(tier, profile):
    return {"tier": tier, "profile": deepcopy(profile)}


def select(candidates, observations, *, static=None, floor="FAST", cfg=CFG):
    return er.select(candidates, static or candidates[0], floor=floor, observations=observations,
                     now=NOW, key_base=KEY, cfg=cfg, activation=ACTIVATION)


def by_model(record):
    return {item["model"]: item for item in record["candidates"]}


# --------------------------------------------------------------------------
# Launch harness
# --------------------------------------------------------------------------

class LaunchHarness(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _fresh(self):
        shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _project(self, risk_class, extra_toml=""):
        result = run(["init", "Evidence", "--risk-class", risk_class], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if extra_toml:
            with (self.tmp / "handsoff.toml").open("a", encoding="utf-8") as handle:
                handle.write(extra_toml)
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="phase four")

    def _launch(self, adapters=("claude", "codex"), observations=None):
        """Build the launch spec and commit the session; return the persisted record."""
        loader = mock.Mock(return_value=list(observations or []))
        lib.archive_dir().mkdir(parents=True, exist_ok=True)
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="task"), \
                mock.patch.object(agent, "applicable_design_review_packet", return_value=None), \
                mock.patch.object(er.cohorts, "load_observations", loader):
            spec = agent.build_launch_spec(
                self.tmp, "implementer", "task",
                which=lambda name: f"/opt/test/{name}" if name in adapters else None,
                skip_preflight=True)
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="test-implementer", adapter=spec.adapter,
            requested_model=spec.model, resolution_source=spec.resolution_source,
            adaptive_routing=spec.adaptive_routing)
        return self.read_status()["agent_sessions"][session["session_id"]]["adaptive_routing"]

    def _launch_key(self):
        status = self.read_status()
        policy = status.get("model_policy")
        return {"repository": self.tmp.name,
                "task_class": evidence.task_class_for_labels(evidence.recorded_work_item_labels(status)),
                "phase": 4,
                "policy_version": policy.get("version", evidence.UNKNOWN) if isinstance(policy, dict)
                else evidence.UNKNOWN}

    def _rich_history(self):
        """Cheap haiku fails often; sonnet succeeds every time."""
        key = self._launch_key()
        return batch(HAIKU, 8, 22, key=key) + batch(SONNET, 30, key=key)

    def _finding(self):
        records = training_success() + shadow_many(30, 100, task_class="feature") \
            + shadow_many(10, 200, profile=OPUS)
        report = shadow.evaluate(records, boundary=BOUNDARY, threshold=THRESHOLD, cost_table=COSTS,
                                 baseline=BASELINE)
        return next(item for item in report["variants"]["all_evidence"]["findings"]
                    if item["cohort_key"] == "acme/app|implementer|bug")

    def _approved(self):
        finding = self._finding()
        return finding, shadow.record_approval(self.tmp, finding)

    def _activate(self):
        finding, approval = self._approved()
        return er.activate(self.tmp, finding=finding["finding_id"], approval=approval["approval_id"],
                           by="pilot")


# --------------------------------------------------------------------------
# REQ-001: off by default, activation, policy version
# --------------------------------------------------------------------------

class OffByDefaultTests(LaunchHarness):
    def _matrix(self, extra=""):
        records = {}
        for risk in MATRIX_RISKS:
            for budget_name, budget in MATRIX_BUDGETS.items():
                for cand_name, adapters in MATRIX_CANDIDATES.items():
                    self._fresh()
                    self._project(risk, budget + extra)
                    records[f"{risk}|{budget_name}|{cand_name}"] = self._launch(
                        adapters, observations=self._rich_history())
        return records

    def test_off_persisted_record_equals_frozen_baseline(self):
        baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(baseline), 16)
        self.assertEqual(self._matrix(), baseline)

    def test_switch_on_without_an_activation_still_equals_the_baseline(self):
        baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self._matrix(SWITCH_ON), baseline)

    def test_switch_defaults_off_and_an_activation_alone_does_nothing(self):
        self._project("routine")
        self.assertFalse(er.evidence_config(self.tmp)["evidence_assisted"])
        self._activate()
        record = self._launch(observations=self._rich_history())
        self.assertNotIn("evidence", record)
        self.assertEqual(record["tier"], "FAST")


class ActivationTests(LaunchHarness):
    def setUp(self):
        super().setUp()
        self._project("routine")

    def _run(self, *args):
        return run(["evidence-routing-activate", *args], cwd=self.tmp)

    def test_the_command_records_the_activation_with_the_policy_version_and_consumes_the_approval(self):
        finding, approval = self._approved()
        result = self._run("--finding", finding["finding_id"], "--approval", approval["approval_id"],
                           "--by", "pilot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EVIDENCE_ROUTING_ACTIVATED", result.stdout)
        [entry] = er.read_ledger(self.tmp)
        self.assertEqual((entry["kind"], entry["policy_version"], entry["finding_id"], entry["approval_id"]),
                         ("activate", er.EVIDENCE_POLICY_VERSION, finding["finding_id"],
                          approval["approval_id"]))
        self.assertRegex(entry["activation_id"], er.ACTIVATION_ID)
        self.assertTrue(any(e["kind"] == "consumed" and e["approval_id"] == approval["approval_id"]
                            for e in shadow._ledger(self.tmp)))
        again = self._run("--finding", finding["finding_id"], "--approval", approval["approval_id"],
                          "--by", "pilot")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already used", again.stdout + again.stderr)
        self.assertEqual(len(er.read_ledger(self.tmp)), 1)

    def test_refused_without_a_finding_or_an_approval(self):
        finding, approval = self._approved()
        for args in (("--approval", approval["approval_id"], "--by", "pilot"),
                     ("--finding", finding["finding_id"], "--by", "pilot")):
            result = self._run(*args)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(er.read_ledger(self.tmp), [])

    def test_refused_for_an_unknown_unrecorded_or_mismatched_approval(self):
        finding, approval = self._approved()
        with self.assertRaisesRegex(er.EvidenceRoutingRefusal, "no Mission Control approval"):
            er.activate(self.tmp, finding=finding["finding_id"], approval="apr-000000000000", by="pilot")
        with self.assertRaisesRegex(er.EvidenceRoutingRefusal, "bound to finding"):
            er.activate(self.tmp, finding="shf-000000000000", approval=approval["approval_id"], by="pilot")
        forged = dict(approval, approval_id="apr-111111111111")
        (shadow.approvals_dir(self.tmp) / "apr-111111111111.json").write_text(json.dumps(forged))
        with self.assertRaisesRegex(er.EvidenceRoutingRefusal, "not recorded by Mission Control"):
            er.activate(self.tmp, finding=finding["finding_id"], approval="apr-111111111111", by="pilot")
        self.assertEqual(er.read_ledger(self.tmp), [])

    def test_effective_state_derives_only_from_the_latest_activate_or_rollback(self):
        on = {**er.evidence_config(self.tmp), "evidence_assisted": True}
        self.assertEqual(er.assisted_state(self.tmp, on)["reason"], "not_activated")
        self._activate()
        self.assertEqual(er.assisted_state(self.tmp, {**on, "evidence_assisted": False})["reason"], "disabled")
        self.assertTrue(er.assisted_state(self.tmp, on)["active"])
        with lib.project_lock(self.tmp):
            er.append_ledger(self.tmp, "no_decision", metric=None)
        self.assertTrue(er.assisted_state(self.tmp, on)["active"], "no_decision never changes it")
        with lib.project_lock(self.tmp):
            er.append_ledger(self.tmp, "rollback", metric="retries")
            er.append_ledger(self.tmp, "no_decision", metric=None)
        self.assertEqual(er.assisted_state(self.tmp, on)["reason"], "rolled_back")
        self._activate()
        self.assertTrue(er.assisted_state(self.tmp, on)["active"], "a fresh approved activation turns it on")

    def test_a_broken_ledger_chain_refuses(self):
        self._activate()
        path = er.ledger_path(self.tmp)
        path.write_text(path.read_text().replace(er.EVIDENCE_POLICY_VERSION, "evidence-wilson.0"))
        with self.assertRaisesRegex(lib.HandsoffError, "chain is broken"):
            er.read_ledger(self.tmp)


class LaunchToPersistedRecordTests(LaunchHarness):
    def test_an_activated_policy_changes_the_persisted_selection(self):
        self._project("routine", SWITCH_ON)
        static = self._launch(observations=self._rich_history())
        self.assertEqual((static["tier"], static["model"]), ("FAST", HAIKU["model"]))
        self.assertNotIn("evidence", static)

        self._fresh()
        self._project("routine", SWITCH_ON)
        activation = self._activate()
        record = self._launch(observations=self._rich_history())
        self.assertEqual((record["tier"], record["adapter"], record["model"]),
                         ("STANDARD", "claude", SONNET["model"]))
        self.assertEqual(record["profile"]["model"], SONNET["model"])
        block = record["evidence"]
        self.assertEqual((block["mode"], block["reason"], block["policy_version"], block["activation_id"]),
                         ("assisted", "assisted_choice", er.EVIDENCE_POLICY_VERSION,
                          activation["activation_id"]))
        self.assertEqual(block["static"], {"tier": "FAST", "adapter": "claude", "model": HAIKU["model"]})
        rows = by_model(block)
        self.assertEqual(rows[HAIKU["model"]]["excluded_reason"], "below_threshold")
        self.assertEqual(rows[OPUS_PROFILE["model"]]["excluded_reason"], "cold_start")
        self.assertEqual(rows["gpt-6-astra"]["excluded_reason"], "cold_start")
        sonnet = rows[SONNET["model"]]
        self.assertEqual((sonnet["n"], sonnet["successes"], sonnet["excluded_reason"]), (30, 30, None))
        expected = ((1000 * 2.0 + 500 * 10.0) / 1e6) / sonnet["wilson_lower"]
        self.assertAlmostEqual(sonnet["score_usd"], expected, places=12)

    def test_an_old_activation_after_a_policy_version_bump_leaves_assistance_off(self):
        self._project("routine", SWITCH_ON)
        activation = self._activate()
        with mock.patch.object(er, "EVIDENCE_POLICY_VERSION", "evidence-wilson.2"):
            record = self._launch(observations=self._rich_history())
        self.assertEqual((record["tier"], record["model"]), ("FAST", HAIKU["model"]))
        self.assertEqual(record["evidence"]["mode"], "off")
        self.assertEqual(record["evidence"]["reason"], "policy_version_mismatch")
        self.assertEqual(record["evidence"]["policy_version"], "evidence-wilson.2")
        self.assertEqual(record["evidence"]["activation_id"], activation["activation_id"])

    def test_a_rolled_back_activation_persists_off_with_the_reason(self):
        self._project("routine", SWITCH_ON)
        self._activate()
        with lib.project_lock(self.tmp):
            er.append_ledger(self.tmp, "rollback", metric="retries")
        record = self._launch(observations=self._rich_history())
        self.assertEqual((record["tier"], record["evidence"]["mode"], record["evidence"]["reason"]),
                         ("FAST", "off", "rolled_back"))

    def test_the_risk_floor_holds_on_the_launch_path(self):
        """REQ-002: elevated risk; haiku's history is perfect and cheapest."""
        self._project("elevated", SWITCH_ON)
        self._activate()
        key = self._launch_key()
        history = batch(HAIKU, 40, key=key, tokens=(10, 10)) + batch(SONNET, 30, key=key)
        record = self._launch(observations=history)
        self.assertEqual(record["tier"], "STANDARD")
        self.assertNotIn(HAIKU["model"], by_model(record["evidence"]),
                         "a below-floor candidate is never even scored")

    def test_a_malformed_evidence_block_is_refused_before_the_session_exists(self):
        self._project("routine", SWITCH_ON)
        record = self._launch()
        self.assertNotIn("evidence", record)
        bad = dict(record, evidence={"mode": "assisted"})
        with self.assertRaisesRegex(lib.HandsoffError, "evidence has invalid fields"):
            lib.create_agent_session(self.tmp, role="reviewer", actor="test-reviewer", adapter="claude",
                                     requested_model=record["model"], resolution_source="adaptive",
                                     adaptive_routing=bad)


# --------------------------------------------------------------------------
# REQ-002: hard constraints first, exclusions, static_fallback
# --------------------------------------------------------------------------

class ExclusionTests(unittest.TestCase):
    def test_cold_start_keeps_the_static_choice_as_static_fallback(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        chosen, record = select(candidates, [])
        self.assertEqual(chosen["profile"]["model"], HAIKU["model"])
        self.assertEqual((record["mode"], record["reason"]), ("static_fallback", "no_qualified_candidate"))
        self.assertEqual({item["excluded_reason"] for item in record["candidates"]}, {"cold_start"})
        er.validate_evidence_record(record)

    def test_a_mix_scores_only_the_sufficient_candidates(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET), cand("PREMIUM", OPUS_PROFILE)]
        history = batch(HAIKU, 5) + batch(SONNET, 30) + batch(OPUS_PROFILE, 3, 2)
        chosen, record = select(candidates, history)
        self.assertEqual(chosen["profile"]["model"], SONNET["model"])
        rows = by_model(record)
        self.assertEqual(rows[HAIKU["model"]]["excluded_reason"], "outcome_evidence_insufficient")
        self.assertEqual(rows[OPUS_PROFILE["model"]]["excluded_reason"], "outcome_evidence_insufficient")
        self.assertIsNone(rows[SONNET["model"]]["excluded_reason"])
        self.assertEqual(record["mode"], "assisted")

    def test_stale_evidence_is_excluded(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        chosen, record = select(candidates, batch(HAIKU, 2, 28) + batch(SONNET, 30, ended=OLD))
        self.assertEqual(by_model(record)[SONNET["model"]]["excluded_reason"], "stale")
        self.assertEqual((chosen["profile"]["model"], record["mode"]), (HAIKU["model"], "static_fallback"))

    def test_conflicting_version_cohorts_are_excluded(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        history = batch(HAIKU, 2, 28) + batch(SONNET, 30, reported="sonnet-v1") \
            + batch(SONNET, 3, 27, reported="sonnet-v2")
        chosen, record = select(candidates, history)
        self.assertEqual(by_model(record)[SONNET["model"]]["excluded_reason"], "conflicting")
        self.assertEqual(record["mode"], "static_fallback")

    def test_a_below_floor_candidate_is_never_selected_however_cheap(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        history = batch(HAIKU, 60, tokens=(1, 1)) + batch(SONNET, 25, 5, tokens=(9000, 9000))
        chosen, record = select(candidates, history, static=candidates[1], floor="STANDARD")
        self.assertEqual(chosen["profile"]["model"], SONNET["model"])
        self.assertEqual(by_model(record)[HAIKU["model"]]["excluded_reason"], "below_floor")
        self.assertIsNone(by_model(record)[HAIKU["model"]]["score_usd"])

    def test_route_never_offers_a_below_floor_candidate_to_the_selector(self):
        seen = []

        def selector(candidates, static, floor):
            seen.append((floor, [item["tier"] for item in candidates]))
            return static, {}
        lib.route_adaptive_profile(risk_class="elevated", deterministic_checks_complete=True,
                                   evidence_selector=selector)
        self.assertEqual(seen[0][0], "STANDARD")
        self.assertNotIn("FAST", seen[0][1])

    def test_unknown_pricing_is_excluded(self):
        codex = deepcopy(lib.ADAPTIVE_OPENAI_PROFILES["PREMIUM"])
        candidates = [cand("PREMIUM", OPUS_PROFILE), cand("PREMIUM", codex)]
        chosen, record = select(candidates, batch(OPUS_PROFILE, 2, 28) + batch(codex, 30))
        self.assertEqual(by_model(record)["gpt-6-astra"]["excluded_reason"], "pricing_unknown")
        self.assertEqual(record["mode"], "static_fallback")


# --------------------------------------------------------------------------
# REQ-003: the score, cohorts, threshold, ties, determinism
# --------------------------------------------------------------------------

def priced(adapter, model, input_price, output_price):
    return {"adapter": adapter, "model": model, "capabilities": ["text", "tool_use"], "limits": {},
            "pricing": {"input_per_mtok": input_price, "output_per_mtok": output_price}, "source": "test"}


class ScoreTests(unittest.TestCase):
    def test_asymmetric_input_and_output_prices_are_applied_separately(self):
        # Input-heavy usage. A has cheap input and dear output, B the same
        # price both ways. A's mean price (10.5) is above B's (8), so pricing
        # total tokens at a mean, or swapping the two prices, picks B.
        a, b = priced("claude", "m-a", 1.0, 20.0), priced("claude", "m-b", 8.0, 8.0)
        history = batch(a, 30, tokens=(10000, 100)) + batch(b, 30, tokens=(10000, 100))
        chosen, record = select([cand("STANDARD", b), cand("STANDARD", a)], history)
        self.assertEqual(chosen["profile"]["model"], "m-a")
        rows = by_model(record)
        lower = shadow.wilson(30, 30)[0]
        self.assertAlmostEqual(rows["m-a"]["score_usd"], ((10000 * 1.0 + 100 * 20.0) / 1e6) / lower, places=12)
        self.assertAlmostEqual(rows["m-b"]["score_usd"], ((10000 * 8.0 + 100 * 8.0) / 1e6) / lower, places=12)
        self.assertEqual((rows["m-a"]["mean_input_tokens"], rows["m-a"]["mean_output_tokens"]), (10000, 100))

    def test_sufficient_outcomes_with_insufficient_tokens_are_excluded(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        history = batch(HAIKU, 2, 28) + batch(SONNET, 25, flags=("usage_partial",)) + batch(SONNET, 5)
        chosen, record = select(candidates, history)
        row = by_model(record)[SONNET["model"]]
        self.assertEqual((row["n"], row["excluded_reason"]), (30, "token_evidence_insufficient"))
        self.assertEqual(record["mode"], "static_fallback")

    def test_the_largest_version_cohort_is_used_and_never_pooled(self):
        history = batch(SONNET, 25, reported="sonnet-v1", tokens=(1000, 1000)) \
            + batch(SONNET, 38, 2, reported="sonnet-v2", tokens=(2000, 2000))
        _, record = select([cand("STANDARD", SONNET)], history)
        row = record["candidates"][0]
        self.assertEqual((row["cohort_version"], row["n"], row["successes"]), ("sonnet-v2", 40, 38))
        self.assertEqual((row["mean_input_tokens"], row["mean_output_tokens"]), (2000, 2000))
        self.assertAlmostEqual(row["wilson_lower"], shadow.wilson(38, 40)[0], places=12)

    def test_equal_version_cohorts_break_on_the_newest_last_observation(self):
        # The newer cohort sorts last by name, so only the recency rule picks it.
        history = batch(SONNET, 30, reported="sonnet-v0", ended="2026-09-10T00:00:00+00:00") \
            + batch(SONNET, 30, reported="sonnet-v1", ended="2026-09-28T00:00:00+00:00")
        _, record = select([cand("STANDARD", SONNET)], history)
        self.assertEqual(record["candidates"][0]["cohort_version"], "sonnet-v1")

    def test_all_below_the_threshold_is_static_fallback(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET)]
        history = batch(HAIKU, 18, 12) + batch(SONNET, 20, 10)
        chosen, record = select(candidates, history, cfg={**CFG, "threshold": 0.9})
        self.assertEqual({row["excluded_reason"] for row in record["candidates"]}, {"below_threshold"})
        self.assertEqual((chosen["profile"]["model"], record["mode"]), (HAIKU["model"], "static_fallback"))
        self.assertTrue(all(row["score_usd"] is None for row in record["candidates"]))

    def test_ties_break_by_tier_then_adapter_then_model(self):
        def tie(*entries):
            profiles = [(tier, priced(adapter, model, 1.0, 1.0)) for tier, adapter, model in entries]
            history = [item for _, profile in profiles for item in batch(profile, 30)]
            chosen, _ = select([cand(tier, profile) for tier, profile in reversed(profiles)], history)
            return chosen["tier"], chosen["profile"]["adapter"], chosen["profile"]["model"]
        self.assertEqual(tie(("FAST", "codex", "z"), ("STANDARD", "claude", "a")), ("FAST", "codex", "z"))
        self.assertEqual(tie(("STANDARD", "claude", "z"), ("STANDARD", "codex", "a")), ("STANDARD", "claude", "z"))
        self.assertEqual(tie(("STANDARD", "claude", "a"), ("STANDARD", "claude", "b")), ("STANDARD", "claude", "a"))

    def test_identical_inputs_give_the_identical_decision(self):
        candidates = [cand("FAST", HAIKU), cand("STANDARD", SONNET), cand("PREMIUM", OPUS_PROFILE)]
        history = batch(HAIKU, 8, 22) + batch(SONNET, 30) + batch(OPUS_PROFILE, 30, tokens=(900, 400))
        first = select(candidates, history)
        shuffled = list(history)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(select(candidates, shuffled), sort_keys=True))


# --------------------------------------------------------------------------
# REQ-004: existing rails stay
# --------------------------------------------------------------------------

class RailsTests(unittest.TestCase):
    def _assisted_session(self, reported):
        _, record = select([cand("FAST", HAIKU), cand("STANDARD", SONNET)],
                           batch(HAIKU, 30, tokens=(10, 10)) + batch(SONNET, 30))
        self.assertEqual(record["mode"], "assisted")
        return {"adapter": "claude", "reported_model": reported,
                "adaptive_routing": {"tier": "FAST", "model": HAIKU["model"], "profile": HAIKU,
                                     "evidence": record}}

    def test_a_model_mismatch_counts_as_the_stronger_tier(self):
        self.assertEqual(lib._adaptive_model_reconciliation(
            self._assisted_session(OPUS_PROFILE["model"]))["effective_tier"], "PREMIUM")
        self.assertEqual(lib._adaptive_model_reconciliation(
            self._assisted_session("some-unknown-model"))["effective_tier"], "PREMIUM")

    def test_repeated_failures_stop_at_the_existing_fallback_cap(self):
        entries = [{"adapter": "claude", "model": model} for model in
                   (HAIKU["model"], SONNET["model"], OPUS_PROFILE["model"])] * 2
        attempted, count, decisions = [("claude", SONNET["model"])], 0, []
        for _ in range(50):
            decision = lib.plan_agent_fallback(
                "implementer", "process_crash", entries[:8], {"claude": True, "codex": False},
                attempted, count, 2, required_tier="STANDARD")
            decisions.append(decision)
            if decision["action"] != "select":
                break
            attempted.append((decision["profile"]["adapter"], decision["profile"]["model"]))
            count += 1
        self.assertLessEqual(count, 2)
        self.assertEqual(decisions[-1]["action"], "pilot_pause")
        self.assertLess(len(decisions), 50, "no unbounded retry loop")

    def test_budget_exhaustion_keeps_its_refusal_before_any_evidence_is_read(self):
        selector = mock.Mock()
        cfg = {"adaptive_routing_budgets": {"per_mission": {"premium_calls": 0}}}
        routed = lib.route_adaptive_profile(cfg, risk_class="irreversible", mission_usage={"premium_calls": 0},
                                            deterministic_checks_complete=True, evidence_selector=selector)
        self.assertEqual((routed["state"], routed["reason"]), ("paused", "per_mission_premium_calls_exhausted"))
        selector.assert_not_called()
        self.assertNotIn("evidence", routed)


# --------------------------------------------------------------------------
# REQ-005: the rollback monitor
# --------------------------------------------------------------------------

def rollback_toml(window=10, min_assisted=3, min_baseline=3, margin=0.1):
    return (SWITCH_ON + "\n[adaptive_routing.evidence.rollback]\n"
            f"window = {window}\nmin_assisted = {min_assisted}\nmin_baseline = {min_baseline}\n"
            f"margin = {margin}\n")


_archive_serial = iter(range(1, 10 ** 6))


def write_archive(directory, repo, decisions, *, completed, activation_id=None):
    """One archived product run. Each decision is (mode, result) or
    (mode, result, tokens); mode `static` has no evidence block, result is
    ok, rejected, retried or incomplete."""
    n = next(_archive_serial)
    sessions, replacements = {}, []
    for index, decision in enumerate(decisions):
        mode, result = decision[:2]
        tokens = decision[2] if len(decision) > 2 else (1000, 500)
        sid = f"hs-{n:04d}{index:04d}"
        routing = {"risk_class": "routine", "tier": "FAST", "adapter": "claude", "model": HAIKU["model"],
                   "profile": HAIKU, "reason": "qualified_profile", "reviewer_required": False,
                   "human_gate_required": False}
        if mode != "static":
            routing["evidence"] = {"mode": mode, "activation_id": activation_id}
        session = {"role": "implementer", "adapter": "claude", "requested_model": HAIKU["model"],
                   "state": "completed", "started_at": f"{completed[:10]}T00:{index // 60:02d}:{index % 60:02d}+00:00",
                   "usage": {"tokens_in": tokens[0], "tokens_out": tokens[1], "tokens_total": sum(tokens),
                             "source": "adapter"},
                   "result": {"payload": {"decision": "changes_requested" if result == "rejected" else "approved",
                                          "tests_executed": "yes"}},
                   "adaptive_routing": routing}
        if result == "incomplete":
            session.update(state="failed", result=None)
        if result == "retried":
            replacements.append({"from_session_id": sid})
        sessions[sid] = session
    archive = {"repo": repo, "run_kind": "product", "completed_at": completed,
               "status": {"status": "complete", "phase_number": 8, "agent_sessions": sessions,
                          "agent_replacements": replacements}}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"run-{n:06d}.json").write_text(json.dumps(archive), encoding="utf-8")


class RollbackMonitorTests(LaunchHarness):
    def setUp(self):
        super().setUp()
        self.archives = self.tmp.parent / f"{self.tmp.name}-archives"
        self.addCleanup(shutil.rmtree, self.archives, True)

    def _start(self, **settings):
        self._project("routine", rollback_toml(**settings))
        return self._activate()["activation_id"]

    def _archive(self, decisions, completed, activation_id):
        write_archive(self.archives, self.tmp.name, decisions, completed=completed, activation_id=activation_id)

    def _monitor(self):
        return er.rollback_monitor(self.tmp, directory=self.archives)

    def _on(self):
        return er.assisted_state(self.tmp)["active"]

    def test_the_configuration_is_refused_unless_both_arm_minimums_fit_the_window(self):
        for table in ({"min_assisted": 0}, {"min_baseline": 0}, {"window": 10, "min_assisted": 6, "min_baseline": 5},
                      {"margin": 1.5}, {"margin": -0.1}, {"window": True}, {"cost_margin": 0.1}):
            with self.subTest(table=table), self.assertRaises(lib.HandsoffError):
                er.rollback_config(table)
        self.assertEqual(er.rollback_config({"window": 10, "min_assisted": 5, "min_baseline": 5})["window"], 10)
        self._project("routine", rollback_toml(window=5, min_assisted=3, min_baseline=3))
        with self.assertRaisesRegex(lib.HandsoffError, "min_assisted \\+ min_baseline must not exceed window"):
            er.evidence_config(self.tmp)
        with self.assertRaisesRegex(lib.HandsoffError, "must not exceed window"):
            self._monitor()

    def test_sparse_arms_record_no_decision_and_active_assistance_stays_on(self):
        activation = self._start()
        self._archive([("assisted", "rejected")] * 2 + [("static", "ok")] * 8, "2026-09-20T00:00:00+00:00",
                      activation)
        result = self._monitor()
        self.assertEqual((result["state"], result["reason"]), ("no_decision", "assisted_below_minimum"))
        self.assertEqual(result["counts"], {"window": 10, "assisted": 2, "baseline": 8})
        self.assertEqual(er.read_ledger(self.tmp)[-1]["kind"], "no_decision")
        self.assertTrue(self._on(), "a no_decision record never switches assistance off")

        self._archive([("assisted", "rejected")] * 8, "2026-09-21T00:00:00+00:00", activation)
        result = self._monitor()
        self.assertEqual((result["state"], result["reason"]), ("no_decision", "baseline_below_minimum"))
        self.assertTrue(self._on())
        self.assertNotIn("rollback", [entry["kind"] for entry in er.read_ledger(self.tmp)])

    def test_baseline_decisions_evicted_from_the_window_no_longer_count(self):
        activation = self._start(window=6)
        self._archive([("static", "ok")] * 3, "2026-09-01T00:00:00+00:00", activation)
        self._archive([("assisted", "rejected")] * 5, "2026-09-20T00:00:00+00:00", activation)
        result = self._monitor()
        self.assertEqual((result["state"], result["reason"]), ("no_decision", "baseline_below_minimum"))
        self.assertEqual(result["counts"], {"window": 6, "assisted": 5, "baseline": 1})
        self.assertTrue(self._on())

        # The same history inside a window that still holds the baseline rolls back.
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("window = 6", "window = 8"))
        result = self._monitor()
        self.assertEqual((result["state"], result["metric"]), ("rolled_back", "review_rejections"))
        self.assertEqual(result["counts"], {"window": 8, "assisted": 5, "baseline": 3})

    def test_higher_cost_alone_never_rolls_back(self):
        activation = self._start()
        self._archive([("static", "ok", (1000, 500))] * 5 + [("assisted", "ok", (900000, 500000))] * 5,
                      "2026-09-20T00:00:00+00:00", activation)
        result = self._monitor()
        self.assertEqual(result["state"], "kept")
        self.assertEqual(result["rates"], {metric: {"assisted": 0.0, "baseline": 0.0}
                                           for metric in er.ROLLBACK_METRICS})
        self.assertEqual([entry["kind"] for entry in er.read_ledger(self.tmp)], ["activate"])
        self.assertTrue(self._on())

    def test_a_breach_names_its_metric(self):
        for result_kind, metric in (("retried", "retries"), ("rejected", "review_rejections"),
                                    ("incomplete", "incomplete_runs")):
            with self.subTest(metric=metric):
                self._fresh()
                shutil.rmtree(self.archives, ignore_errors=True)
                activation = self._start()
                self._archive([("static_fallback", "ok")] * 2 + [("static", "ok")] * 3
                              + [("assisted", result_kind)] + [("assisted", "ok")] * 4,
                              "2026-09-20T00:00:00+00:00", activation)
                result = self._monitor()
                self.assertEqual((result["state"], result["metric"]), ("rolled_back", metric))
                self.assertAlmostEqual(result["rates"][metric]["assisted"], 0.2)
                self.assertEqual(result["rates"][metric]["baseline"], 0.0)
                entry = er.read_ledger(self.tmp)[-1]
                self.assertEqual((entry["kind"], entry["metric"], entry["activation_id"]),
                                 ("rollback", metric, activation))

    def test_a_breach_within_the_margin_is_kept(self):
        activation = self._start(margin=0.25)
        self._archive([("static", "ok")] * 5 + [("assisted", "rejected")] + [("assisted", "ok")] * 4,
                      "2026-09-20T00:00:00+00:00", activation)
        self.assertEqual(self._monitor()["state"], "kept")
        self.assertTrue(self._on())

    def test_a_rollback_stays_off_until_a_fresh_approved_activation(self):
        activation = self._start()
        self._archive([("static", "ok")] * 5 + [("assisted", "rejected")] * 5, "2026-09-20T00:00:00+00:00",
                      activation)
        self.assertEqual(self._monitor()["state"], "rolled_back")
        self.assertFalse(self._on())
        record = self._launch(observations=self._rich_history())
        self.assertEqual((record["tier"], record["evidence"]["mode"], record["evidence"]["reason"]),
                         ("FAST", "off", "rolled_back"))
        # Better outcomes later do not switch it back on by themselves.
        self._archive([("static", "rejected")] * 4, "2026-09-25T00:00:00+00:00", activation)
        self.assertEqual(self._monitor(), {"state": "inactive", "reason": "rolled_back"})
        self.assertFalse(self._on())
        self.assertEqual([entry["kind"] for entry in er.read_ledger(self.tmp)], ["activate", "rollback"])

        fresh = self._activate()["activation_id"]
        self.assertTrue(self._on())
        self.assertEqual(er.assisted_state(self.tmp)["activation"]["activation_id"], fresh)
        # The old activation's assisted decisions are not the new one's arm.
        result = self._monitor()
        self.assertEqual((result["state"], result["reason"]), ("no_decision", "assisted_below_minimum"))
        self.assertTrue(self._on())


class RollbackPhaseEightHookTests(LaunchHarness):
    def test_advance_8_complete_invokes_the_rollback_monitor(self):
        self.init()
        with (self.tmp / "handsoff.toml").open("a", encoding="utf-8") as handle:
            handle.write(rollback_toml())
        activation = self._activate()["activation_id"]
        write_archive(lib.archive_dir(), self.tmp.name,
                      [("static", "ok")] * 5 + [("assisted", "rejected")] * 5,
                      completed="2026-09-20T00:00:00+00:00", activation_id=activation)
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        approve = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertTrue(er.assisted_state(self.tmp)["active"])

        result = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read_status()["phase_number"], 8)
        self.assertIn("EVIDENCE_ROUTING_ROLLED_BACK: review_rejections", result.stdout)
        entry = er.read_ledger(self.tmp)[-1]
        self.assertEqual((entry["kind"], entry["metric"], entry["activation_id"]),
                         ("rollback", "review_rejections", activation))
        self.assertEqual(er.assisted_state(self.tmp)["reason"], "rolled_back")

    def test_a_monitor_failure_never_fails_the_completed_advance(self):
        self.init()
        with (self.tmp / "handsoff.toml").open("a", encoding="utf-8") as handle:
            handle.write(rollback_toml())
        self._activate()
        self.set_criterion_state("passing", resolved=True)
        self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp).returncode, 0)
        self.assertEqual(run(["verify-live", "--by", "monitor"], cwd=self.tmp).returncode, 0)
        lib.archive_dir().mkdir(parents=True, exist_ok=True)
        (lib.archive_dir() / "corrupt.json").write_text("{", encoding="utf-8")
        result = run(["advance", "8", "100"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HANDSOFF_EVIDENCE_ROLLBACK_FAILED", result.stdout)
        self.assertEqual(self.read_status()["phase_number"], 8)



class TheEvidenceLineIsDocumented(unittest.TestCase):
    """REQ-006 (#299): one reference section names every part of the line."""

    def test_the_reference_names_every_part_in_order_and_off_by_default(self):
        text = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        start = text.index("### Evidence-assisted routing, measured before activated (#299)")
        section = text[start:text.index("\n### ", start + 10)]
        positions = [section.index(tag) for tag in ("(#300)", "(#301)", "(#302)", "(#304, #305)", "(#303)")]
        self.assertEqual(positions, sorted(positions), "the line is described in measure-before-activate order")
        for phrase in ("off by default", "static_fallback", "Higher cost alone never triggers a rollback",
                       "Mission Control approval", "scoring policy version"):
            self.assertIn(phrase, section)


if __name__ == "__main__":
    unittest.main()
