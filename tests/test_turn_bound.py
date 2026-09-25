"""#307: bound the turn, not the reserve.

A reserve carves headroom from below the ceiling, so it only helps if the
session stops below the limit. Session
`hs-d26f19a8a3754328ae26f3a156740692` did not stop below the ceiling at
all:

    ceiling         80,000
    reserve          2,048
    provider_limit  77,952
    reported usage  88,487      (8,487 past the CEILING)
    "tokens used" lines printed: 1, at exit
    verdict emitted: none

Raising the reserve cannot shrink a turn already in flight, and it costs
every well-behaved session real headroom, so `PROTOCOL_RESERVE_TOKENS`
stays at 2,048. Where usage arrives only at exit, the engine bounds the
LAUNCH instead and says so before the tokens are spent.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class TheAdapterFactIsDeclaredNotInferred(unittest.TestCase):
    """REQ-001: inferring it needs the observation the adapter withholds."""

    def test_every_selectable_adapter_declares_its_behaviour(self):
        undeclared = sorted(set(lib.SELECTABLE_AGENT_ADAPTERS) - set(lib.ADAPTER_INTERMEDIATE_USAGE))
        self.assertEqual(undeclared, [],
                         "a new adapter must declare whether it reports usage before exit, "
                         "rather than defaulting into the unbounded path")

    def test_the_map_declares_nothing_that_is_not_an_adapter(self):
        extra = sorted(set(lib.ADAPTER_INTERMEDIATE_USAGE) - set(lib.SELECTABLE_AGENT_ADAPTERS))
        self.assertEqual(extra, [])

    def test_codex_reports_only_at_exit_and_claude_streams(self):
        self.assertFalse(lib.adapter_reports_usage_before_exit("codex"))
        self.assertTrue(lib.adapter_reports_usage_before_exit("claude"))

    def test_an_undeclared_adapter_is_refused_rather_than_assumed(self):
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.adapter_reports_usage_before_exit("some-new-provider")
        self.assertIn("ADAPTER_INTERMEDIATE_USAGE", str(caught.exception))

    def test_the_reserve_is_unchanged_and_says_why(self):
        self.assertEqual(lib.PROTOCOL_RESERVE_TOKENS, 2_048)
        source = (BIN / "handsoff_lib.py").read_text(encoding="utf-8")
        head = source[:source.index("PROTOCOL_RESERVE_TOKENS = 2_048")]
        self.assertIn("#307", head[-1200:],
                      "the fixed reserve must carry the reason it is fixed")


class ATurnThatCouldExceedTheCeilingIsRefusedAtLaunch(unittest.TestCase):
    """REQ-001: the three cases the ticket names.

    Every call names its own archive directory. Letting these fall back to
    the operator's real ~/Documents/Handsoff-Archive made them read live
    data: the basis string depends on how many overshoots are recorded, so
    the suite passed on a machine that had one and failed on CI, which has
    no archive at all. That is the #311 and #323 shape, and #321 exists
    because the same directory must never be touched by a test.
    """

    def setUp(self):
        self.archives = Path(tempfile.mkdtemp(prefix="handsoff-turnbound-"))
        self.addCleanup(shutil.rmtree, self.archives, ignore_errors=True)

    def refusal(self, **kwargs):
        kwargs.setdefault("archives_dir", self.archives)
        return lib.turn_bound_refusal(**kwargs)

    def test_a_tool_running_role_on_a_non_streaming_adapter_is_refused(self):
        refusal = self.refusal(
            adapter="codex", role="reviewer", ceiling=80_000, compact_scope=False,
            safe_minimum=75_000)
        self.assertIsNotNone(refusal)
        self.assertIn("reports usage only at exit", refusal)

    def test_the_refusal_names_both_remedies(self):
        refusal = self.refusal(
            adapter="codex", role="reviewer", ceiling=80_000, compact_scope=False,
            safe_minimum=75_000)
        self.assertIn("compact review scope", refusal)
        self.assertIn("[agent_budget].reviewer", refusal)

    def test_the_refusal_carries_the_number_and_its_basis(self):
        """A bound resting on one observation must not present itself as a
        measured distribution."""
        refusal = self.refusal(
            adapter="codex", role="reviewer", ceiling=80_000, compact_scope=False,
            safe_minimum=75_000)
        self.assertIn("85535", refusal, "the safe minimum plus the worst observed turn")
        self.assertIn("10535", refusal, "the declared worst observed turn")
        self.assertIn("no overshoot is recorded yet", refusal,
                      "an empty archive must say so rather than imply a measured distribution")

    def test_a_streaming_adapter_is_untouched(self):
        """enforce_ceiling already bounds these by observation, so this
        change must not become a global restriction."""
        for role in lib.TOOL_RUNNING_ROLES:
            self.assertIsNone(self.refusal(
                adapter="claude", role=role, ceiling=80_000, compact_scope=False,
                safe_minimum=75_000), role)

    def test_a_compact_scope_is_never_refused(self):
        self.assertIsNone(self.refusal(
            adapter="codex", role="reviewer", ceiling=80_000, compact_scope=True,
            safe_minimum=75_000))

    def test_a_read_only_role_is_never_refused(self):
        """Its turns are small and the existing reserve covers the 764-token
        overshoot measured for one."""
        for role in ("architect", "supervisor"):
            self.assertNotIn(role, lib.TOOL_RUNNING_ROLES)
            self.assertIsNone(self.refusal(
                adapter="codex", role=role, ceiling=40_000, compact_scope=False), role)

    def test_a_ceiling_that_already_accounts_for_the_worst_turn_is_allowed(self):
        """The ticket's second remedy. An unconditional refusal would block
        every Codex reviewer on the engine's own lanes, which is not a
        bound, it is an outage."""
        self.assertIsNone(self.refusal(
            adapter="codex", role="reviewer", ceiling=92_000,
            compact_scope=False, safe_minimum=20_000))

    def test_the_advised_ceiling_actually_passes_on_retry(self):
        """The general cure, not the one arithmetic slip.

        The message told the operator to raise the budget to a figure that
        still failed, because it was derived from the current ceiling
        rather than from the condition being checked. Any remedy a refusal
        names has to satisfy the check it is a remedy for.
        """
        import re
        for ceiling, safe_minimum in ((12_000, 20_000), (80_000, 75_000),
                                      (1_000, 90_000), (40_000, 41_000)):
            refusal = self.refusal(
                adapter="codex", role="reviewer", ceiling=ceiling,
                compact_scope=False, safe_minimum=safe_minimum)
            self.assertIsNotNone(refusal, (ceiling, safe_minimum))
            advised = int(re.search(r"to at least (\d+)", refusal).group(1))
            self.assertIsNone(
                self.refusal(adapter="codex", role="reviewer", ceiling=advised,
                                       compact_scope=False, safe_minimum=safe_minimum),
                f"advised {advised} for ceiling {ceiling}/min {safe_minimum} is still refused")

    def test_the_advised_number_is_the_pass_threshold_not_the_current_ceiling(self):
        refusal = self.refusal(
            adapter="codex", role="reviewer", ceiling=12_000,
            compact_scope=False, safe_minimum=20_000)
        self.assertIn("30535", refusal, "safe_minimum 20000 plus the 10535 worst turn")
        self.assertNotIn("22535", refusal, "that is ceiling-derived and still fails")

    def test_a_ceiling_that_cannot_absorb_one_bad_turn_is_refused(self):
        refusal = self.refusal(
            adapter="codex", role="reviewer", ceiling=12_000,
            compact_scope=False, safe_minimum=20_000)
        self.assertIsNotNone(refusal)
        self.assertIn("cannot absorb one such turn", refusal)


class TheOvershootIsReadFromWhereItIsWritten(unittest.TestCase):
    """REQ-001: the reader is checked against a fixture of the real shape.

    The first version of this reader looked for the value on each session's
    own `failure` field. It is actually recorded on the run's
    `agent_failures` map, keyed by session id, so the reader found nothing
    and reported zero samples: a wrong query that reads as "this has never
    happened".
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="handsoff-overshoot-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _archive(self, name, sessions, failures):
        (self.dir / name).write_text(json.dumps({
            "repo": "project-handsoff", "run_kind": "product",
            "status": {"agent_sessions": sessions, "agent_failures": failures},
        }), encoding="utf-8")

    def test_it_reads_the_real_recorded_shape(self):
        self._archive("a.json",
                      {"hs-1": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 560}})
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir)
        self.assertEqual((result["tokens"], result["samples"]), (560, 1))

    def test_it_takes_the_worst_across_archives_and_counts_them(self):
        self._archive("a.json", {"hs-1": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 560}})
        self._archive("b.json", {"hs-2": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-2": {"ceiling_overshoot_tokens": 9_000}})
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir)
        self.assertEqual((result["tokens"], result["samples"]), (9_000, 2))

    def test_it_does_not_mix_adapters_or_roles(self):
        self._archive("a.json",
                      {"hs-1": {"adapter": "claude", "role": "reviewer"},
                       "hs-2": {"adapter": "codex", "role": "implementer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 9_000},
                       "hs-2": {"ceiling_overshoot_tokens": 8_000}})
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir)
        self.assertEqual((result["tokens"], result["samples"]), (0, 0))

    def test_no_recorded_overshoot_is_zero_samples_not_an_error(self):
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir)
        self.assertEqual((result["tokens"], result["samples"]), (0, 0))

    def test_a_missing_directory_is_zero_samples(self):
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir / "absent")
        self.assertEqual(result["samples"], 0)

    def test_a_malformed_archive_does_not_stop_the_scan(self):
        (self.dir / "bad.json").write_text("[1, 2, 3]", encoding="utf-8")
        self._archive("good.json", {"hs-1": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 700}})
        result = lib.worst_recorded_overshoot("codex", "reviewer", self.dir)
        self.assertEqual((result["tokens"], result["samples"]), (700, 1))

    def test_the_declared_floor_wins_when_the_archive_is_thinner(self):
        """With one 560-token sample the archive would size a ceiling an
        order of magnitude too small for the 10,535 case."""
        self._archive("a.json", {"hs-1": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 560}})
        refusal = lib.turn_bound_refusal(adapter="codex", role="reviewer", ceiling=80_000,
                                         compact_scope=False, safe_minimum=75_000,
                                         archives_dir=self.dir)
        self.assertIn("85535", refusal)

    def test_a_larger_recorded_overshoot_wins_over_the_floor(self):
        self._archive("a.json", {"hs-1": {"adapter": "codex", "role": "reviewer"}},
                      {"hs-1": {"ceiling_overshoot_tokens": 20_000}})
        refusal = lib.turn_bound_refusal(adapter="codex", role="reviewer", ceiling=80_000,
                                         compact_scope=False, safe_minimum=75_000,
                                         archives_dir=self.dir)
        self.assertIn("95000", refusal)
        self.assertIn("worst recorded overshoot 20000", refusal)


class NoTestInThisSuiteReadsTheRealArchive(unittest.TestCase):
    """#321's rule, enforced here rather than remembered.

    The first version of this suite let every refusal fall back to the
    operator's real ~/Documents/Handsoff-Archive. It passed on a machine
    holding one recorded overshoot and failed where none exists, because
    the refusal's basis string depends on the sample count. Derived from
    this file's own source so a new test cannot quietly reintroduce it.
    """

    #: The one call allowed to omit it: the fixture helper that supplies it.
    EXEMPT_FUNCTIONS = {"refusal"}

    def test_every_archive_reading_call_names_its_own_directory(self):
        import ast
        source = (Path(__file__)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name not in ("turn_bound_refusal", "worst_recorded_overshoot"):
                continue
            if enclosing.get(id(node)) in self.EXEMPT_FUNCTIONS:
                continue
            keywords = {k.arg for k in node.keywords}
            positional = name == "worst_recorded_overshoot" and len(node.args) >= 3
            if "archives_dir" not in keywords and not positional:
                offenders.append(f"line {node.lineno}: {name}")
        self.assertEqual(offenders, [],
                         "these calls fall back to the operator's real archive, so the "
                         "suite reads live data and its result depends on the machine")


class TheDeclaredFloorCarriesItsProvenance(unittest.TestCase):
    """REQ-001: a measurement whose archive is gone is still a measurement,
    but it has to say where it came from."""

    def test_codex_has_a_declared_worst_turn(self):
        self.assertEqual(lib.MEASURED_WORST_TURN_OVERSHOOT["codex"], 10_535)

    def test_the_declaration_names_the_session_that_produced_it(self):
        source = (BIN / "handsoff_lib.py").read_text(encoding="utf-8")
        head = source[:source.index("MEASURED_WORST_TURN_OVERSHOOT = ")]
        self.assertIn("hs-d26f19a8a3754328ae26f3a156740692", head[-1400:])
        self.assertIn("88,487", head[-1400:])


if __name__ == "__main__":
    unittest.main()

