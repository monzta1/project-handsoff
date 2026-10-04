"""#342: an explicit [agent_budget] key is an authority, not a cap.

`ceiling = min(configured, max(floor, calculated))` made every configured
value above the calculated one produce the same ceiling, so `implementer =
500000` and `implementer = 80000` were the same run. A field report spent the
larger part of a day on it: managed Codex launches exhausted at 74k to 80k
tokens against a configured 500k, each exhaustion costing a half-written work
item, and the operator eventually abandoned the managed launcher for raw
`codex exec`.

Two of these tests are about a distinction that no value comparison can make.
An operator who writes `implementer = 80000` has chosen the number that
happens to equal the built-in default; `get(role, DEFAULT)` cannot tell them
apart from an operator who wrote nothing, which is why the criterion says key
membership and why the tests below set a budget EQUAL to the default on
purpose.
"""
import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT
from tests.guards import guard

sys.path.insert(0, str(BIN))
import handsoff_config as config  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_schema as schema  # noqa: E402


def project_budget(**roles):
    """This repository's handsoff.toml with [agent_budget] replaced outright.

    Replaced rather than appended to: a second `[agent_budget]` is a
    duplicate-table TOML error, and an inserted line beside an existing key
    for the same role is "cannot overwrite a value". Both look exactly like a
    validation refusal from the code under test.
    """
    root = Path(tempfile.mkdtemp(prefix="handsoff-test-budget-"))
    text = (ROOT / "handsoff.toml").read_text(encoding="utf-8")
    body = "".join(f"{role} = {value}\n" for role, value in roles.items())
    text, count = re.subn(r"\[agent_budget\]\n(?:[^\[]*\n)?", f"[agent_budget]\n{body}\n", text,
                          count=1)
    assert count == 1, "could not locate [agent_budget] in the fixture config"
    (root / "handsoff.toml").write_text(text, encoding="utf-8")
    return root


def decision(configured, *, explicit, role="implementer", risk_class="routine",
             packet_bytes=40_000, criteria_count=9, changed_files=12):
    return lib.plan_role_token_budget(
        configured_ceiling=configured, role=role, risk_class=risk_class,
        packet_bytes=packet_bytes, criteria_count=criteria_count,
        changed_files=changed_files, configured_explicitly=explicit)


class KeyMembershipIsRecordedAtLoad(unittest.TestCase):
    """REQ-001, the config half: WHICH roles were set is its own fact."""

    def test_a_role_the_operator_set_is_listed_as_explicit(self):
        cfg = lib.load_config(project_budget(implementer=500_000))
        self.assertEqual(cfg["agent_token_budgets_explicit"], ["implementer"])
        self.assertEqual(cfg["agent_token_budgets"]["implementer"], 500_000)

    def test_a_role_the_operator_did_not_set_is_not_listed(self):
        cfg = lib.load_config(project_budget(implementer=500_000))
        for role in ("architect", "reviewer", "supervisor"):
            self.assertNotIn(role, cfg["agent_token_budgets_explicit"])
            self.assertEqual(cfg["agent_token_budgets"][role],
                             config.DEFAULT_AGENT_TOKEN_BUDGETS[role],
                             "an unset role must still receive its default number")

    def test_a_value_equal_to_the_default_still_counts_as_explicit(self):
        """The distinction a value comparison cannot make, and the reason the
        criterion says key membership."""
        default = config.DEFAULT_AGENT_TOKEN_BUDGETS["implementer"]
        cfg = lib.load_config(project_budget(implementer=default))
        self.assertEqual(cfg["agent_token_budgets_explicit"], ["implementer"])

    def test_an_empty_table_leaves_nothing_explicit(self):
        cfg = lib.load_config(project_budget())
        self.assertEqual(cfg["agent_token_budgets_explicit"], [])

    def test_every_role_set_lists_every_role_sorted(self):
        cfg = lib.load_config(project_budget(reviewer=90_000, architect=50_000,
                                             implementer=100_000, supervisor=30_000))
        self.assertEqual(cfg["agent_token_budgets_explicit"],
                         ["architect", "implementer", "reviewer", "supervisor"])

    def test_the_default_config_claims_no_explicit_roles(self):
        self.assertEqual(config.DEFAULT_CONFIG["agent_token_budgets_explicit"], [],
                         "a built-in default is not an operator's choice")

    def test_an_out_of_range_value_is_still_refused(self):
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.load_config(project_budget(implementer=1))
        self.assertIn("agent_budget.implementer", str(caught.exception))


class AnExplicitCeilingIsApplied(unittest.TestCase):
    """REQ-001, the planner half: the configured number IS the ceiling."""

    def test_the_configured_value_is_the_ceiling_exactly(self):
        plan = decision(500_000, explicit=True)
        self.assertEqual(plan["ceiling"], 500_000)
        self.assertEqual(plan["ceiling_source"], "configured")

    def test_the_old_formula_would_have_produced_a_different_number(self):
        """Pins the defect, not just the fix. Without this the test would pass
        against the original code the moment the calculation happened to reach
        the configured value."""
        plan = decision(500_000, explicit=True)
        old = min(500_000, plan["calculated_ceiling"])
        self.assertLess(old, 500_000,
                        "the fixture no longer reproduces the capping the issue reported")
        self.assertNotEqual(plan["ceiling"], old)

    def test_the_provider_is_told_the_ceiling_minus_the_reserve(self):
        """Unchanged by this work, and the reason it must stay unchanged: the
        reserve is what lets a session emit its verdict before exhausting."""
        plan = decision(500_000, explicit=True)
        self.assertEqual(plan["provider_limit"], 500_000 - lib.PROTOCOL_RESERVE_TOKENS)
        self.assertEqual(plan["reserved_protocol_tokens"], lib.PROTOCOL_RESERVE_TOKENS)

    def test_an_unset_role_keeps_the_calculated_ceiling(self):
        """The compatibility half. An operator who configures nothing must get
        exactly the behaviour they had before this change."""
        plan = decision(500_000, explicit=False)
        self.assertEqual(plan["ceiling"], min(500_000, plan["calculated_ceiling"]))
        self.assertEqual(plan["ceiling_source"], "calculated")
        self.assertLess(plan["ceiling"], 500_000)

    def test_an_explicit_ceiling_below_the_calculated_one_is_also_honoured(self):
        """Authority runs both ways. An operator capping a role DOWN, to keep a
        cheap lane cheap, is as much a decision as raising one."""
        plan = decision(20_000, explicit=True, role="supervisor")
        self.assertEqual(plan["ceiling"], 20_000)
        self.assertGreaterEqual(plan["calculated_ceiling"], plan["ceiling"])

    def test_an_explicit_ceiling_is_not_raised_to_the_role_floor(self):
        plan = decision(10_000, explicit=True, role="supervisor")
        self.assertEqual(plan["ceiling"], 10_000,
                         "the floor overrode an explicit operator decision")

    def test_the_calculated_figure_is_recorded_even_when_it_loses(self):
        plan = decision(500_000, explicit=True)
        self.assertIsInstance(plan["calculated_ceiling"], int)
        self.assertGreater(plan["calculated_ceiling"], 0)

    def test_the_divergence_is_the_gap_between_the_two(self):
        plan = decision(500_000, explicit=True)
        self.assertEqual(plan["ceiling_divergence"],
                         500_000 - min(500_000, plan["calculated_ceiling"]))
        self.assertGreater(plan["ceiling_divergence"], 0)

    def test_agreement_reports_no_divergence(self):
        plan = decision(500_000, explicit=False)
        self.assertEqual(plan["ceiling_divergence"], 0,
                         "a leg with nothing to report must not show a divergence")

    def test_a_broad_review_still_receives_the_whole_configured_ceiling(self):
        """The existing broad-review widening, which must not regress: it was
        added because a reviewer exhausted after doing the work but before
        issuing its verdict, three times."""
        plan = decision(90_000, explicit=False, role="reviewer",
                        criteria_count=lib.BROAD_REVIEW_CRITERIA, packet_bytes=1_000)
        self.assertEqual(plan["ceiling"], 90_000)

    def test_the_legacy_risk_free_path_is_unchanged(self):
        plan = lib.plan_role_token_budget(configured_ceiling=90_000, role="implementer",
                                          risk_class=None, packet_bytes=100)
        self.assertEqual(plan["ceiling"], 90_000)
        self.assertEqual(plan["basis"], "legacy_configured_ceiling")
        self.assertIsNone(plan["calculated_ceiling"])
        self.assertEqual(plan["ceiling_divergence"], 0)

    def test_the_flag_defaults_to_the_old_behaviour(self):
        """Every caller that does not pass it keeps the previous ceiling, so an
        unthreaded call site is a visible regression rather than a silent
        change of policy."""
        self.assertEqual(
            lib.plan_role_token_budget(configured_ceiling=500_000, role="implementer",
                                       risk_class="routine", packet_bytes=40_000,
                                       criteria_count=9, changed_files=12)["ceiling_source"],
            "calculated")


@guard
class BothLaunchBuildersThreadIt(unittest.TestCase):
    """REQ-006. #347 shipped with one of the two builders threaded and the
    other not, which left every failed-over role on the old behaviour. The
    failover path is the one that regresses silently, because it is taken only
    after a session has already failed."""

    def setUp(self):
        self.source = (BIN / "handsoff_agent.py").read_text(encoding="utf-8")

    def test_both_planner_call_sites_read_the_explicit_key_set(self):
        threaded = self.source.count(
            'configured_explicitly=role in cfg["agent_token_budgets_explicit"]')
        self.assertEqual(threaded, 2,
                         "both plan_role_token_budget call sites must decide authority from key "
                         f"membership; found {threaded}. build_profile_launch_spec is the failover "
                         "path, and leaving it out means an explicit budget stops applying the "
                         "moment a role fails over once.")

    def test_no_call_site_decides_authority_by_comparing_against_the_default(self):
        self.assertNotIn("DEFAULT_AGENT_TOKEN_BUDGETS[role] !=", self.source)
        self.assertNotIn("!= DEFAULT_AGENT_TOKEN_BUDGETS", self.source)

    def test_every_planner_call_passes_the_flag(self):
        """Counted against the calls themselves, so a third launch builder
        added later cannot quietly omit it."""
        calls = self.source.count("lib.plan_role_token_budget(")
        self.assertEqual(
            calls, self.source.count('configured_explicitly=role in cfg["agent_token_budgets_explicit"]'),
            f"{calls} planner calls in handsoff_agent.py but not all pass configured_explicitly")


class TheSessionRecordCarriesTheFacts(unittest.TestCase):
    """REQ-002, the record half. A number on the page that is not in the
    ledger is a number nobody can audit afterwards."""

    def plan(self, **kwargs):
        return decision(500_000, explicit=True, **kwargs)

    def test_a_decision_with_the_new_fields_validates(self):
        schema.validate_session_budget_decision(self.plan())

    def test_the_older_three_shapes_still_validate(self):
        """Archive compatibility. The engine validates these against EXACT
        field sets, so an unconditional field would invalidate every session
        recorded before this change."""
        full = self.plan()
        for drop in ([], ["calculated_ceiling", "configured_explicitly",
                          "ceiling_source", "ceiling_divergence"]):
            shape = {k: v for k, v in full.items() if k not in drop}
            schema.validate_session_budget_decision(shape)

    def test_a_real_recorded_status_still_validates(self):
        """The compatibility claim against real recorded data.

        Two earlier versions of this test were wrong in opposite directions.
        The first read a fixture that is not in this checkout and SKIPPED,
        reporting a pass while proving nothing. The second swept
        `.handsoff-archive/`, which is gitignored: it passed here and would
        have failed in CI and inside a mutation proof, both of which see a
        tree without it.

        So the fixture is a real archived run -- 25 managed sessions written
        by v0.3.x, one absolute home path replaced -- committed alongside the
        test. The engine validates session records against EXACT field sets,
        which makes this document precisely what an unconditional new field
        would break.
        """
        baseline = ROOT / "tests" / "fixtures" / "status_governance_baseline.json"
        status = json.loads(baseline.read_text(encoding="utf-8"))
        sessions = status.get("agent_sessions") or {}
        self.assertGreaterEqual(len(sessions), 20,
                                "the fixture carries too few sessions to say anything")
        for session in sessions.values():
            for field in ("ceiling_source", "ceiling_divergence"):
                self.assertNotIn(field, session.get("budget_decision") or {},
                                 "the fixture already carries the new fields, so it cannot "
                                 "prove a document written before them still validates")
        self.assertEqual(schema.validate_status_schema(status), [],
                         "a status written before these fields stopped validating")

    def test_the_fixture_is_tracked_so_ci_and_a_mutation_copy_both_see_it(self):
        """Pins the lesson. A test whose evidence lives only in gitignored
        local state passes on the author's machine and nowhere else."""
        relative = "tests/fixtures/status_governance_baseline.json"
        self.assertTrue((ROOT / relative).is_file(), f"{relative} is missing from this tree")
        if not (ROOT / ".git").exists():
            # A tree with no repository is itself the evidence: this is a CI
            # export or a mutation-proof copy, and the fixture arrived with it.
            # Shelling out to git here is what made the FIRST version of this
            # test fail inside the proof copy, where `.git` is excluded.
            return
        import subprocess
        listed = subprocess.run(["git", "ls-files", "--error-unmatch", relative],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0,
                         "the baseline fixture is not tracked by git: "
                         + (listed.stderr or "").strip())

    def test_a_divergence_that_does_not_follow_from_the_numbers_is_refused(self):
        forged = copy.deepcopy(self.plan())
        forged["ceiling_divergence"] = 1
        with self.assertRaises(lib.HandsoffError) as caught:
            schema.validate_session_budget_decision(forged)
        self.assertIn("does not follow", str(caught.exception))

    def test_a_divergence_with_no_calculated_ceiling_is_refused(self):
        """The legacy path computes nothing, so there is no figure to diverge
        FROM. Leaving this to the arithmetic check meant any divergence at all
        validated on that path."""
        forged = lib.plan_role_token_budget(configured_ceiling=90_000, role="implementer",
                                            risk_class=None, packet_bytes=100)
        forged["ceiling_divergence"] = 99
        with self.assertRaises(lib.HandsoffError) as caught:
            schema.validate_session_budget_decision(forged)
        self.assertIn("no calculated ceiling", str(caught.exception))

    def test_claiming_the_configured_ceiling_won_without_applying_it_is_refused(self):
        forged = copy.deepcopy(self.plan())
        forged["ceiling"] = 60_000
        forged["ceiling_divergence"] = 60_000 - min(500_000, forged["calculated_ceiling"])
        with self.assertRaises(lib.HandsoffError) as caught:
            schema.validate_session_budget_decision(forged)
        self.assertIn("did not apply it", str(caught.exception))

    def test_an_unknown_ceiling_source_is_refused(self):
        forged = copy.deepcopy(self.plan())
        forged["ceiling_source"] = "vibes"
        with self.assertRaises(lib.HandsoffError):
            schema.validate_session_budget_decision(forged)

    def test_a_non_boolean_explicit_flag_is_refused(self):
        forged = copy.deepcopy(self.plan())
        forged["configured_explicitly"] = "yes"
        with self.assertRaises(lib.HandsoffError):
            schema.validate_session_budget_decision(forged)

    def test_the_journey_leg_carries_the_whole_decision_to_the_page(self):
        plan = self.plan()
        leg = lib._agent_assignment({
            "session_id": "hs-" + "3" * 32, "role": "implementer", "adapter": "codex",
            "phase_number": 4, "state": "completed", "budget_decision": plan})
        for field in ("ceiling", "configured_ceiling", "calculated_ceiling",
                      "ceiling_source", "ceiling_divergence", "configured_explicitly"):
            self.assertIn(field, leg["budget_decision"],
                          f"the projection dropped {field} before the page could read it")
        self.assertEqual(leg["budget_decision"]["ceiling_divergence"],
                         plan["ceiling_divergence"])

    def test_a_leg_from_before_the_fields_existed_keeps_its_shape(self):
        legacy = {k: v for k, v in self.plan().items()
                  if k not in ("calculated_ceiling", "configured_explicitly",
                               "ceiling_source", "ceiling_divergence")}
        leg = lib._agent_assignment({
            "session_id": "hs-" + "4" * 32, "role": "implementer", "adapter": "codex",
            "phase_number": 4, "state": "completed", "budget_decision": legacy})
        self.assertNotIn("ceiling_source", leg["budget_decision"])


class TheConfigurationSaysSo(unittest.TestCase):
    """REQ-007's subject, asserted here so the manual attestation is reading a
    document the suite also checks rather than one it merely hopes exists."""

    def test_the_agent_budget_comment_states_the_authority_rule(self):
        text = (ROOT / "handsoff.toml").read_text(encoding="utf-8")
        table = text[text.index("[agent_budget]"):]
        comment = table[:table.index("architect =")]
        self.assertIn("authoritative", comment)
        self.assertIn("calculated", comment,
                      "the comment must name the other number, or an operator cannot "
                      "understand what their key is overriding")
        self.assertIn("reserve", comment,
                      "the comment must say the protocol reserve still applies")


if __name__ == "__main__":
    unittest.main()
