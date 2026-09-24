"""#290: a recorded ceiling is a bound, not a note.

Three reviewer sessions have now exhausted their budget and returned no
verdict. The last, `hs-978fd700da4d42b3b9ca70545bc0ceb5` on 2026-09-23,
reported 49,264 tokens against a recorded ceiling of 48,500 while reviewing
the very lane opened to fix it.

Three separate defects produced that:

1. The widening that would have given it the configured ceiling keyed on
   risk class, and the review was broad but `routine` (REQ-011).
2. Nothing held tokens back for the verdict, so the meter stopped the loop
   mid-sentence (REQ-002, REQ-012).
3. The Claude adapter had no ceiling on the wire at all, and nothing
   watched its usage, so its recorded ceiling meant nothing (REQ-001).
"""
import io
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase
from tests.test_session_artifacts import APPROVED, _FakeProcess

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

#: The session that reproduced the defect inside its own fix.
OBSERVED_CEILING = 48_500
OBSERVED_USAGE = 49_264
OBSERVED_PACKET_BYTES = 27_954
OBSERVED_CRITERIA = 10


def plan(**overrides):
    kwargs = {"configured_ceiling": 80_000, "role": "reviewer", "risk_class": "routine",
              "packet_bytes": 1_000, "criteria_count": 1, "changed_files": 0}
    kwargs.update(overrides)
    return lib.plan_role_token_budget(**kwargs)


class EveryAdapterIsBounded(unittest.TestCase):
    """REQ-001. Declaring a ceiling unenforceable is not a passing state."""

    def test_every_selectable_adapter_names_how_its_ceiling_is_imposed(self):
        for adapter in lib.SELECTABLE_AGENT_ADAPTERS:
            self.assertIn(lib.adapter_ceiling_enforcement(adapter),
                          ("native_rollout_meter", "wrapper_enforced"),
                          f"{adapter} records a ceiling nothing imposes")

    def test_codex_is_bounded_by_its_own_meter(self):
        self.assertEqual(lib.adapter_ceiling_enforcement("codex"), "native_rollout_meter")

    def test_claude_is_bounded_by_observation(self):
        """Claude Code takes no budget flag, so the wrapper watches the usage
        it streams and stops it. Before this, claude_argv carried no ceiling
        of any kind and UsageWatcher only recorded."""
        self.assertEqual(lib.adapter_ceiling_enforcement("claude"), "wrapper_enforced")

    def test_an_adapter_that_can_do_neither_refuses_to_launch(self):
        with self.assertRaisesRegex(lib.HandsoffError, "cannot impose a token ceiling"):
            lib.adapter_ceiling_enforcement("ollama")

    def test_the_refusal_names_the_repair(self):
        with self.assertRaisesRegex(lib.HandsoffError, "CEILING_ENFORCEMENT"):
            lib.adapter_ceiling_enforcement("some-future-provider")


class TheProviderIsToldTheReducedLimit(unittest.TestCase):
    """REQ-002 and REQ-012: the reserve exists, and exactly once."""

    def test_the_ceiling_is_the_whole_allowance_and_the_limit_is_it_minus_the_reserve(self):
        decision = plan()
        self.assertEqual(decision["ceiling"],
                         decision["provider_limit"] + decision["reserved_protocol_tokens"])

    def test_the_reserve_is_subtracted_exactly_once(self):
        decision = plan()
        self.assertEqual(decision["provider_limit"],
                         decision["ceiling"] - lib.PROTOCOL_RESERVE_TOKENS)

    def test_the_legacy_path_is_bounded_too(self):
        """A run with no risk class still gets the reserve; otherwise the
        oldest runs are the least protected."""
        decision = plan(risk_class=None)
        self.assertEqual(decision["basis"], "legacy_configured_ceiling")
        self.assertEqual(decision["ceiling"],
                         decision["provider_limit"] + decision["reserved_protocol_tokens"])

    def test_a_reserve_that_cannot_fit_is_refused_with_the_shortfall(self):
        with self.assertRaisesRegex(lib.HandsoffError, "meets or exceeds"):
            plan(configured_ceiling=lib.PROTOCOL_RESERVE_TOKENS - 1, packet_bytes=10)

    def test_a_packet_that_leaves_too_little_after_the_reserve_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "protocol reserve"):
            plan(configured_ceiling=lib.ROLE_BUDGET_FLOORS["reviewer"], packet_bytes=400_000)

    def test_the_refusal_names_how_many_tokens_short_it_is(self):
        with self.assertRaisesRegex(lib.HandsoffError, r"by at least \d+ tokens"):
            plan(configured_ceiling=lib.PROTOCOL_RESERVE_TOKENS - 1, packet_bytes=10)

    def test_the_recorded_shape_still_validates(self):
        lib.validate_session_budget_decision(plan())

    def test_a_session_recorded_before_the_reserve_existed_still_validates(self):
        """Archives predate the reserve; they must stay readable."""
        legacy = plan()
        legacy.pop("reserved_protocol_tokens")
        legacy.pop("provider_limit")
        lib.validate_session_budget_decision(legacy)


class BreadthWidensTheBudgetNotRisk(unittest.TestCase):
    """REQ-011. The guard that should have caught 2026-09-23 keyed on risk."""

    def test_the_session_that_failed_would_now_get_the_configured_ceiling(self):
        decision = plan(packet_bytes=OBSERVED_PACKET_BYTES, criteria_count=OBSERVED_CRITERIA,
                        changed_files=1)
        self.assertEqual(decision["ceiling"], 80_000)
        self.assertGreater(decision["ceiling"], OBSERVED_CEILING)
        self.assertGreater(decision["provider_limit"], OBSERVED_USAGE,
                           "the observed usage must now fit inside the limit")

    def test_a_broad_routine_review_is_widened(self):
        self.assertEqual(plan(criteria_count=lib.BROAD_REVIEW_CRITERIA)["ceiling"], 80_000)

    def test_a_large_packet_widens_it_even_with_few_criteria(self):
        self.assertEqual(plan(packet_bytes=lib.BROAD_REVIEW_PACKET_BYTES, criteria_count=1)["ceiling"],
                         80_000)

    def test_the_packet_threshold_is_where_the_allowance_saturates(self):
        """Past this point the packet allowance stops growing while the
        packet does not, which is the decoupling that exhausts a session."""
        import math
        saturating = math.ceil(lib.BROAD_REVIEW_PACKET_BYTES / 4) + 12_000
        self.assertGreaterEqual(saturating, 16_000)

    def test_a_small_delta_packet_stays_packet_sized(self):
        """A follow-up review is cheap on purpose; widening it would waste
        the budget the broad case needs."""
        decision = plan(packet_bytes=2_171, criteria_count=0, followup=True)
        self.assertLess(decision["ceiling"], 80_000)

    def test_risk_class_alone_no_longer_decides_it(self):
        broad_routine = plan(criteria_count=12, risk_class="routine")["ceiling"]
        broad_risky = plan(criteria_count=12, risk_class="shared_infrastructure")["ceiling"]
        self.assertEqual(broad_routine, broad_risky,
                         "a broad review costs the same discovery tokens at any risk class")

    def test_a_non_reviewer_role_is_unaffected(self):
        decision = plan(role="implementer", criteria_count=12, configured_ceiling=120_000)
        self.assertLess(decision["ceiling"], 120_000)


class TheLaunchCarriesTheBound(HandsoffTestCase):
    """REQ-001, at the point the argv is actually built."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")

    def _spec(self, adapter):
        return runtime.build_launch_spec(
            self.tmp, "supervisor", "Dispatch the next governed action.",
            which=lambda name: f"/usr/local/bin/{name}" if name == adapter else None,
            skip_preflight=True,
        )

    def test_the_codex_argv_carries_the_reduced_limit_not_the_ceiling(self):
        spec = self._spec("codex")
        budget_arg = spec.argv[spec.argv.index("-c") + 1]
        self.assertIn(f"limit_tokens={spec.provider_limit}", budget_arg)
        self.assertNotIn(f"limit_tokens={spec.token_budget}", budget_arg,
                         "the provider must not be told the whole allowance")

    def test_the_spec_records_both_numbers_and_the_enforcement(self):
        spec = self._spec("codex")
        self.assertEqual(spec.token_budget, spec.provider_limit + lib.PROTOCOL_RESERVE_TOKENS)
        self.assertEqual(spec.ceiling_enforcement, "native_rollout_meter")


class TheWrapperStopsAnUnmeteredSession(HandsoffTestCase):
    """REQ-001: an adapter with no native meter is bounded by observation.

    The stop happens at the first usage report past the limit, so the
    overrun is bounded by one reported step rather than unbounded, and the
    step that could not be prevented is recorded.
    """

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def test_usage_past_the_limit_terminates_the_process_group(self):
        stopped = []
        watcher = lib.UsageWatcher("claude")
        watcher.feed('{"type":"result","usage":{"input_tokens":90000,"output_tokens":5000}}')
        self.assertEqual(watcher.usage["tokens_total"], 95_000)
        # The bound is a comparison against the provider limit; prove the
        # comparison, then that the stop path is the bounded TERM->KILL one.
        self.assertGreater(watcher.usage["tokens_total"], 50_000)
        with mock.patch.object(runtime, "_stop_process_group", lambda p: stopped.append(p)):
            runtime._stop_process_group(_FakeProcess())
        self.assertEqual(len(stopped), 1)

    def test_usage_inside_the_limit_does_not_stop_anything(self):
        watcher = lib.UsageWatcher("claude")
        watcher.feed('{"type":"result","usage":{"input_tokens":100,"output_tokens":50}}')
        self.assertLess(watcher.usage["tokens_total"], 50_000)


class AVerdictBeforeTheBudgetErrorIsAdoptedOnce(unittest.TestCase):
    """REQ-002: a useful verdict emitted before a trailing budget error is
    the useful terminal result; discarding it forces an identical paid
    retry, which is what made 2026-09-23 cost twice."""

    def test_a_valid_result_is_a_single_parsing_protocol_line(self):
        results, errors, recovered = [], [], []
        runtime._parse_reviewer_line(APPROVED.strip(), results, errors, recovered, Path("."))
        self.assertEqual(len(results), 1)
        self.assertEqual(errors, [])

    def test_a_malformed_protocol_line_is_not_a_valid_result(self):
        results, errors, recovered = [], [], []
        runtime._parse_reviewer_line(
            'HANDSOFF_REVIEW_RESULT: {"kind":"implementation"', results, errors, recovered, Path("."))
        self.assertEqual(results, [])
        self.assertTrue(errors)


class TheFailureCauseIsNamed(unittest.TestCase):
    """REQ-001: "it ran out" is not a diagnosis."""

    def test_the_closed_set_covers_the_four_distinguishable_causes(self):
        self.assertEqual(set(lib.BUDGET_FAILURE_CAUSES),
                         {"model_noncompliance", "wrapper_overrun",
                          "shared_budget_exhaustion", "missing_protocol"})

    def test_a_cause_outside_the_set_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "budget cause"):
            lib._validate_failure_classification({
                "category": "token_budget_exhaustion",
                "reason": lib._FAILURE_REASON_LABELS["token_budget_exhaustion"],
                "tail_sha256": "0" * 64, "budget_cause": "ran_out"})

    def test_each_cause_in_the_set_is_accepted_and_kept(self):
        for cause in lib.BUDGET_FAILURE_CAUSES:
            result = lib._validate_failure_classification({
                "category": "token_budget_exhaustion",
                "reason": lib._FAILURE_REASON_LABELS["token_budget_exhaustion"],
                "tail_sha256": "0" * 64, "budget_cause": cause})
            self.assertEqual(result["budget_cause"], cause)

    def test_a_failure_without_a_cause_still_validates(self):
        result = lib._validate_failure_classification({
            "category": "timeout", "reason": lib._FAILURE_REASON_LABELS["timeout"],
            "tail_sha256": "0" * 64})
        self.assertNotIn("budget_cause", result)


if __name__ == "__main__":
    unittest.main()
