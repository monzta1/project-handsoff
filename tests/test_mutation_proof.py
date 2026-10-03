"""#349: the engine removes the behaviour and requires the test to notice.

Every other evidence path here asks whether a command exited zero. None of
them asks whether that command would still exit zero with the implementation
gutted, and in this repository the answer was often yes: stubbing one refusal
in `validate_status_schema` to `return []` passed all 1,727 tests.

So these tests are about one property, stated two ways:

- a command that asserts something about the symbol must produce `ok: True`;
- a command that merely executes it must produce `ok: False` WITH a refusal.

The second half is the one that matters. A mutation tester that approves
everything is worse than none, because it issues the exact assurance the
author was missing. Several tests below therefore assert the refusal, not
just the absence of approval.
"""
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import fixture_state
from tests.test_handsoff_supervisor import (
    BIN, ROOT, HandsoffTestCase, run, set_fixture_check_commands)

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_mutation as mutation  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402
from tests.engine_patch import patch_engine  # noqa: E402


def _accepted_flags(command):
    """The long flags `command` really accepts, read from the CLI itself.

    Asked of the live parser rather than restated here: a hard-coded list is
    exactly what let the gap message and the CLI drift apart.
    """
    done = subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), command, "--help"],
                          capture_output=True, text=True, timeout=60)
    import re
    return set(re.findall(r"--[a-z][a-z-]+", done.stdout))


SUBJECT = textwrap.dedent('''
    """A tiny module standing in for an engine module."""


    def refuse_unknown(kind):
        """Return a refusal string, or None when the kind is allowed."""
        if kind not in ("checks", "manual"):
            return f"unknown kind {kind}"
        return None


    def untouched(value):
        return value * 2
''').lstrip()


def project(test_source: str) -> Path:
    """A throwaway project: one subject module, one test of it."""
    root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-fixture-"))
    (root / "bin").mkdir()
    (root / "bin" / "subject.py").write_text(SUBJECT, encoding="utf-8")
    (root / "check.py").write_text(test_source, encoding="utf-8")
    return root


ASSERTS_THE_REFUSAL = textwrap.dedent('''
    import sys
    sys.path.insert(0, "bin")
    import subject
    assert subject.refuse_unknown("nonsense") == "unknown kind nonsense", "no refusal"
    assert subject.refuse_unknown("checks") is None
    print("ok")
''').lstrip()

ASSERTS_NOTHING = textwrap.dedent('''
    import sys
    sys.path.insert(0, "bin")
    import subject
    subject.refuse_unknown("nonsense")   # executed, and nothing is asked of it
    print("ok")
''').lstrip()

ALREADY_BROKEN = textwrap.dedent('''
    import sys
    sys.path.insert(0, "bin")
    import subject
    assert subject.refuse_unknown("checks") == "this was never true"
''').lstrip()


#: The reviewer's reproduction for finding 1, kept as a fixture.
#: `irrelevant_helper` is named by no test and runs once while the module
#: loads, so neutralising it raises at import and the command "fails after"
#: for a reason that has nothing to do with any assertion.
IMPORT_TIME_SUBJECT = textwrap.dedent("""
    def irrelevant_helper(value):
        '''Called while the module loads, and named by no test.'''
        return {"configured": value}


    SETTINGS = irrelevant_helper(7)["configured"]


    def actual_feature(kind):
        if kind not in ("checks", "manual"):
            return f"unknown kind {kind}"
        return None
""").lstrip()

ASSERTS_THE_FEATURE = textwrap.dedent("""
    import sys
    sys.path.insert(0, "bin")
    import subject
    assert subject.actual_feature("nonsense") == "unknown kind nonsense"
    print("ok")
""").lstrip()

TWO_DEFINITIONS = textwrap.dedent("""
    def helper(value):
        return value + 1


    class Thing:
        def helper(self, value):
            return value - 1
""").lstrip()

METHOD_ONLY = textwrap.dedent("""
    class Holder:
        def only_here(self):
            return "real"
""").lstrip()


class AnIrrelevantSymbolCannotBeMadeToLookProved(unittest.TestCase):
    """Review finding 1, reproduced and pinned.

    The hole: `prove` only asked whether the command passed before and failed
    after. A function called at module scope, which no test mentions, raises
    when neutralised, so the command failed after and an irrelevant symbol
    reported `ok: True`.

    The fix is differential, not a blanket import check: the probe runs against
    the UNMUTATED copy too, and only "imported before, does not import after"
    is a verdict. A module that is not importable standalone for some unrelated
    reason therefore cannot produce a false refusal.
    """

    def subject(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-importtime-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(IMPORT_TIME_SUBJECT, encoding="utf-8")
        (root / "check.py").write_text(ASSERTS_THE_FEATURE, encoding="utf-8")
        return root

    def test_a_symbol_whose_loss_breaks_the_import_is_refused(self):
        record = mutation.prove(self.subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="irrelevant_helper", timeout=60)
        self.assertTrue(record["passed_before"], record)
        self.assertTrue(record["failed_after"], record)
        self.assertFalse(record["import_intact"], record)
        self.assertFalse(record["ok"],
                         "an irrelevant symbol was reported as proved; the command failed "
                         "after only because the module stopped loading")
        self.assertIn("importing", record["refusal"])
        self.assertIn("irrelevant_helper", record["refusal"])

    def test_the_symbol_the_test_actually_asserts_is_still_proved(self):
        """The inverse, in the same module. A guard that refused both would
        pass the test above while making the tool useless."""
        record = mutation.prove(self.subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="actual_feature", timeout=60)
        self.assertTrue(record["ok"], record)
        self.assertTrue(record["import_intact"], record)

    def test_a_module_that_never_imported_standalone_is_not_falsely_refused(self):
        """The differential half. The probe loads the file by path, so a module
        depending on a package context fails the probe BEFORE the mutation too,
        and the check must then say nothing rather than refuse."""
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-noimport-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(
            "import handsoff_module_that_does_not_exist  # noqa: F401\n" + SUBJECT, encoding="utf-8")
        (root / "check.py").write_text('print("ok")\n', encoding="utf-8")
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertTrue(record["import_intact"],
                        "an unimportable-anyway module was reported as broken by the mutation")
        # It is still refused, because `print("ok")` notices nothing. The point
        # is WHICH refusal: the honest one, not the import one.
        self.assertFalse(record["ok"])
        self.assertIn("asserts nothing", record["refusal"])


#: Round 2's forgery (b), kept as a fixture: the target refuses a second
#: import, standing in for a singleton, a bound port or a lock file.
ONE_SHOT_SUBJECT = textwrap.dedent("""
    import pathlib

    _MARKER = pathlib.Path("imported.once")
    if _MARKER.exists():
        raise RuntimeError("this module refuses a second import")
    _MARKER.write_text("x")


    def totally_unrelated_symbol():
        '''Called by no test anywhere.'''
        return "unused"


    def actual_feature(kind):
        if kind not in ("checks", "manual"):
            return f"unknown kind {kind}"
        return None
""").lstrip()


class EveryRunGetsItsOwnTree(unittest.TestCase):
    """Round 2 finding 1b, reproduced and pinned.

    All five runs used to share one copy. A review forged a proof for an
    arbitrary symbol with a target that raises on a second import: the first
    run consumed the one-shot resource, every later run failed for that
    reason, and the failure was read as the test detecting the mutation.

    A fresh copy per run removes the cause rather than detecting it. It also
    fixes a quieter version of the same problem, where a suite that writes
    into its own tree changed the conditions of the run that followed it.
    """

    def subject(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-oneshot-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(ONE_SHOT_SUBJECT, encoding="utf-8")
        (root / "check.py").write_text(ASSERTS_THE_FEATURE, encoding="utf-8")
        return root

    def test_a_one_shot_import_cannot_forge_a_proof_for_an_unrelated_symbol(self):
        record = mutation.prove(self.subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="totally_unrelated_symbol",
                                timeout=60)
        self.assertFalse(record["ok"], record)
        self.assertIn("asserts nothing", record["refusal"],
                      "the refusal must be the honest one; a one-shot resource used to make "
                      "this look like detection")

    def test_the_runs_do_not_share_a_working_tree(self):
        """Proven by where each run executed, not by inspection."""
        seen = []
        root = self.subject()
        mutation.prove(root, command="true", target="bin/subject.py", symbol="actual_feature",
                       runner=lambda c, cwd, t: (seen.append(str(cwd)),
                                                 {"exit_code": 0, "timed_out": False, "tail": ""})[1])
        self.assertEqual(len(seen), len(set(seen)),
                         f"two runs shared a copy: {sorted(seen)}")
        self.assertGreaterEqual(len(seen), 5, "expected a baseline, a control, two probes and the mutated run")

    def test_the_real_symbol_is_still_proved_in_the_same_module(self):
        record = mutation.prove(self.subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="actual_feature", timeout=60)
        self.assertTrue(record["ok"], record)


class ACommandThatIsNotReproducibleProvesNothing(unittest.TestCase):
    """The control run. A fresh copy per run cures state inside the tree; a
    command whose result depends on state OUTSIDE it still cannot support a
    proof, because its failure after the mutation is not attributable."""

    def subject(self, marker):
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-control-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(SUBJECT, encoding="utf-8")
        (root / "check.py").write_text(textwrap.dedent(f"""
            import pathlib, sys
            marker = pathlib.Path({str(marker)!r})
            if marker.exists():
                sys.exit(3)
            marker.write_text("x")
            sys.path.insert(0, "bin")
            import subject
            assert subject.refuse_unknown("nonsense") == "unknown kind nonsense"
            print("ok")
        """).lstrip(), encoding="utf-8")
        return root

    def test_a_command_that_passes_once_and_fails_on_a_clean_copy_is_refused(self):
        marker = Path(tempfile.mkdtemp(prefix="handsoff-mutation-marker-")) / "once"
        record = mutation.prove(self.subject(marker), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertEqual(record["clean_exit_codes"][0], 0, record)
        self.assertNotEqual(set(record["clean_exit_codes"]), {0},
                            "the fixture no longer reproduces a command that stops passing")
        self.assertFalse(record["reproducible"], record)
        self.assertFalse(record["ok"],
                         "with one clean run this records a proof: the mutated run fails for "
                         "the same reason the later clean runs did, which has nothing to do "
                         "with the mutation")
        self.assertIn("disagreed with itself", record["refusal"])
        self.assertEqual(record["mutated_exit_codes"], [],
                         "the mutated runs should be skipped once the clean side disagrees; "
                         "they cost a full suite run each and could prove nothing")

    def test_a_reproducible_command_reports_reproducible(self):
        """The inverse, so a guard that refused everything would not pass the
        test above while making the tool useless."""
        root = project(ASSERTS_THE_REFUSAL)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertTrue(record["reproducible"], record)
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["control_exit_code"], 0)


class TheRecordStatesWhatWasEstablished(unittest.TestCase):
    """The proof's claim is narrower than "the tests assert this behaviour",
    and the record has to carry every input to that judgement so a reader can
    reach it themselves rather than trust `ok`."""

    def test_every_input_to_the_verdict_is_recorded(self):
        root = project(ASSERTS_THE_REFUSAL)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        for field in ("passed_before", "reproducible", "failed_after", "import_intact",
                      "baseline_exit_code", "control_exit_code", "mutated_exit_code",
                      "command", "target", "symbol", "mutation_version", "source_digest"):
            self.assertIn(field, record, f"the record omits {field}, so ok cannot be re-derived")

    def test_ok_is_exactly_the_conjunction_of_the_four_conditions(self):
        """Read off the source. If `ok` could be true with any one of them
        false, the recorded fields would no longer explain the verdict."""
        source = (BIN / "handsoff_mutation.py").read_text(encoding="utf-8")
        verdict = source[source.index('"ok": bool('):]
        verdict = verdict[:verdict.index("\n")]
        for condition in ("passed_before", "reproducible", "failed_after", "not broke_import"):
            self.assertIn(condition, verdict, f"ok does not depend on {condition}: {verdict}")

    def test_the_documented_limit_is_stated_where_an_author_reads_it(self):
        """Round 2 finding 1a: a symbol used in a test file's module-level code
        is detected by crash rather than by assertion, and this proof does not
        tell the two apart. Telling them apart means parsing an arbitrary
        runner's output, which would be a guess dressed as a gate. So the limit
        is disclosed, and this test is what keeps it disclosed."""
        doc = mutation.prove.__doc__ or ""
        self.assertIn("establishes, exactly", doc)
        self.assertIn("collection", doc,
                      "the docstring must name the case it cannot distinguish")
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        self.assertIn("detection by crash", reference.lower(),
                      "docs/REFERENCE.md must state the limit for an operator too")


#: Round 3 finding 7, the plainest form of "detection by crash": a test that
#: consumes the mutated symbol's return value in arithmetic and asserts
#: nothing about it. No special setup, and probably the common case.
CONSUMED_WITHOUT_ASSERTION = textwrap.dedent("""
    def get_divisor():
        return 2


    def other_real_feature(value):
        return value + 1
""").lstrip()

USES_THE_VALUE_WITHOUT_ASSERTING = textwrap.dedent("""
    import sys
    sys.path.insert(0, "bin")
    import subject
    result = 10 / subject.get_divisor()     # consumed, never asserted on
    assert subject.other_real_feature(1) == 2
    print("ok")
""").lstrip()


class TheDisclosedLimitIsTheCommonCase(unittest.TestCase):
    """Round 3 finding 7, pinned so the disclosure cannot quietly narrow again.

    The proof establishes that the suite goes RED without the behaviour, not
    that an assertion about it fails. `return None` keeps the module importable
    by design, but a None flowing into arithmetic, indexing, iteration or
    attribute access raises at the point of use, so any code the suite reaches
    that consumes the value without asserting on it is enough.

    The first disclosure illustrated only a test file's module-level code,
    which made the limit sound exotic. A review showed `10 / get_divisor()`
    with nothing asserted, which is ordinary. This test asserts the behaviour
    IS what it is, rather than pretending otherwise, and the companion test
    below asserts the limit stays written down where an author reads it.
    """

    def test_a_value_consumed_without_an_assertion_still_reports_a_proof(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-consumed-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(CONSUMED_WITHOUT_ASSERTION, encoding="utf-8")
        (root / "check.py").write_text(USES_THE_VALUE_WITHOUT_ASSERTING, encoding="utf-8")
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="get_divisor", timeout=60)
        self.assertTrue(record["ok"],
                        "the recorded behaviour changed: this is the disclosed limit, and if it "
                        "now refuses, the documentation claiming otherwise is wrong")
        self.assertTrue(record["import_intact"],
                        "the crash is at the point of use, not at import, which is why the "
                        "import probe cannot see it")

    def test_the_limit_names_the_ordinary_case_and_not_only_the_exotic_one(self):
        doc = mutation.prove.__doc__ or ""
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        for text, where in ((doc, "prove.__doc__"), (reference, "docs/REFERENCE.md")):
            with self.subTest(where=where):
                self.assertIn("arithmetic", text,
                              f"{where} must name the ordinary way a None crashes, or an author "
                              "will under-rate how often the floor is a crash")
                self.assertIn("setUp", text,
                              f"{where} must say the consuming code can be outside a test body")
                self.assertIn("NOT that an assertion", text.replace("does not make", "NOT that an assertion"),
                              f"{where} must deny the stronger claim explicitly")

    def test_the_confidence_level_is_disclosed_as_one(self):
        """`ok` is not a guarantee, and two reviews proved it. The number and
        the measurement both belong in the text, so nobody reads `ok: true` as
        certainty."""
        doc = mutation.prove.__doc__ or ""
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        for text, where in ((doc, "prove.__doc__"), (reference, "docs/REFERENCE.md")):
            with self.subTest(where=where):
                self.assertIn("confidence level", text, f"{where} must not imply a guarantee")
                self.assertIn("flaky", text, f"{where} must name the unsound input")
        self.assertIn("CONFIRMATIONS", mutation.prove.__doc__ or "")
        self.assertGreaterEqual(mutation.CONFIRMATIONS, 2,
                                "one run of each side is the configuration a review forged")


class TheWholeProofIsBounded(unittest.TestCase):
    """Round 3 finding 3, second half, and a gap the tool found in its own fix.

    A per-run timeout bounds one run and says nothing about a proof that makes
    up to `2 * CONFIRMATIONS + 2` of them: at the 900s default that is hours,
    and a run that long is a hung run however correct each piece is.

    These tests exist because the engine refused its own suite for `_past`:
    neutralised to `return None` the deadline never fires, and all 81 tests
    still passed. The guard was real, verified by hand once, and covered by
    nothing, which is exactly what this whole feature is for.
    """

    def slow_subject(self):
        root = Path(tempfile.mkdtemp(prefix="handsoff-mutation-budget-"))
        (root / "bin").mkdir()
        (root / "bin" / "subject.py").write_text(SUBJECT, encoding="utf-8")
        (root / "check.py").write_text(textwrap.dedent("""
            import sys, time
            time.sleep(0.6)
            sys.path.insert(0, "bin")
            import subject
            assert subject.refuse_unknown("nonsense") == "unknown kind nonsense"
            print("ok")
        """).lstrip(), encoding="utf-8")
        return root

    def test_a_proof_that_exceeds_its_budget_stops_and_says_so(self):
        record = mutation.prove(self.slow_subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown",
                                timeout=60, total_timeout=1)
        self.assertTrue(record["ran_out_of_budget"], record)
        self.assertFalse(record["ok"], "a proof that ran out of budget was reported as a proof")
        self.assertIn("budget", record["refusal"])
        self.assertLess(len(record["clean_exit_codes"]), mutation.CONFIRMATIONS,
                        "the budget did not stop the runs; every clean run still happened")

    def test_running_out_of_budget_is_reported_differently_from_a_failed_proof(self):
        """An author who hit a time limit has a different next step from one
        whose test does not detect the mutation. Reporting both as 'the proof
        failed' sends them to rewrite a test that may be fine."""
        record = mutation.prove(self.slow_subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown",
                                timeout=60, total_timeout=1)
        self.assertNotIn("asserts nothing", record["refusal"])
        self.assertNotIn("still passed", record["refusal"])
        self.assertIn("budget", record["refusal"])

    def test_a_generous_budget_does_not_interfere(self):
        """The inverse. A guard that fired early would pass the tests above
        while making every honest proof fail."""
        record = mutation.prove(self.slow_subject(), command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown",
                                timeout=60, total_timeout=600)
        self.assertFalse(record["ran_out_of_budget"], record)
        self.assertTrue(record["ok"], record)
        self.assertEqual(len(record["clean_exit_codes"]), mutation.CONFIRMATIONS)
        self.assertEqual(len(record["mutated_exit_codes"]), mutation.CONFIRMATIONS)

    def test_no_budget_at_all_means_no_deadline(self):
        """`total_timeout=None` is the library default, so a caller that passes
        nothing must behave exactly as it did before the budget existed."""
        record = mutation.prove(project(ASSERTS_THE_REFUSAL),
                                command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertFalse(record["ran_out_of_budget"], record)
        self.assertTrue(record["ok"], record)

    def test_the_cli_bounds_the_proof_by_default(self):
        """A library caller may opt out; an operator running the command must
        not have to know to opt in."""
        parsed = supervisor.build_parser().parse_args(
            ["--root", ".", "mutation-proof", "REQ-001", "--by", "someone"])
        self.assertEqual(parsed.total_timeout, mutation.DEFAULT_TOTAL_TIMEOUT)
        self.assertIsNotNone(parsed.total_timeout, "the CLI leaves the proof unbounded")
        self.assertLessEqual(mutation.DEFAULT_TOTAL_TIMEOUT, 3600,
                             "a default budget longer than an hour is not a bound a run can use")


class ARefusalIsNeverATraceback(unittest.TestCase):
    """Review finding 2. REQ-012 promises every refusal carries a reason an
    author can act on. Pointing the target at a file this tool's own digest
    treats as engine source raised SyntaxError out of the CLI instead."""

    def test_a_javascript_target_is_refused_in_words(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "dashboard").mkdir()
        (root / "dashboard" / "app.js").write_text(
            "export function routingModelLabel(item) { return item.model; }\n", encoding="utf-8")
        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.prove(root, command="true", target="dashboard/app.js",
                           symbol="routingModelLabel",
                           runner=lambda c, cwd, t: {"exit_code": 0, "timed_out": False, "tail": ""})
        message = str(caught.exception)
        self.assertIn("not parseable Python", message)
        self.assertIn("dashboard/app.js", message,
                      "the refusal must name the file the author gave")

    def test_a_binary_target_is_refused_in_words(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "bin" / "blob.py").write_bytes(b"\x00\x01\x02\xff")
        with self.assertRaises(lib.HandsoffError):
            mutation.prove(root, command="true", target="bin/blob.py", symbol="anything",
                           runner=lambda c, cwd, t: {"exit_code": 0, "timed_out": False, "tail": ""})


class AnAmbiguousSymbolIsRefusedRatherThanGuessed(unittest.TestCase):
    """Review finding 4. `neutralize` walked the whole tree and took the first
    match, so a nested definition sharing a name decided the mutation silently
    and the record named a symbol that was not the one neutralised.
    `where_defined` read only `tree.body` and could never locate a method, so
    the re-export hint was blind to them."""

    def test_two_definitions_of_one_name_are_refused_with_their_lines(self):
        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.neutralize(TWO_DEFINITIONS, "helper")
        message = str(caught.exception)
        self.assertIn("defined 2 times", message)
        self.assertIn("line", message, "the refusal must say where, or it is unactionable")

    def test_where_defined_and_neutralize_agree_about_methods(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "bin" / "holder.py").write_text(METHOD_ONLY, encoding="utf-8")
        self.assertEqual(mutation.where_defined(root, "only_here"), ["bin/holder.py"],
                         "where_defined cannot see a method that neutralize would mutate")
        self.assertIn("return None", mutation.neutralize(METHOD_ONLY, "only_here"))


class TheEscapeGuardCoversTheTargetWhereverItIs(unittest.TestCase):
    """Review finding 3. `_source_digest` covered only `bin/**/*.py` and
    `dashboard/**/*.js`, while nothing restricted the target, so an escape into
    a target elsewhere returned normally with the real file mutated on disk."""

    def test_an_escape_into_a_target_outside_bin_is_refused(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "pkg").mkdir()
        elsewhere = root / "pkg" / "feature.py"
        elsewhere.write_text(SUBJECT, encoding="utf-8")

        def escaping(command, cwd, timeout):
            elsewhere.write_text(elsewhere.read_text() + "\n# escaped\n", encoding="utf-8")
            return {"exit_code": 0, "timed_out": False, "tail": ""}

        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.prove(root, command="true", target="pkg/feature.py",
                           symbol="refuse_unknown", runner=escaping)
        self.assertIn("changed during the proof", str(caught.exception))

    def test_the_digest_includes_the_target(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "pkg").mkdir()
        elsewhere = root / "pkg" / "feature.py"
        elsewhere.write_text(SUBJECT, encoding="utf-8")
        before = mutation._source_digest(root, elsewhere)
        elsewhere.write_text(SUBJECT + "\nX = 1\n", encoding="utf-8")
        self.assertNotEqual(mutation._source_digest(root, elsewhere), before)


class TheSurgeryKeepsTheModuleImportable(unittest.TestCase):
    """`neutralize` removes behaviour without breaking collection.

    Deleting the function instead would raise ImportError or AttributeError
    during collection, failing the suite for a reason that has nothing to do
    with whether the test asserts anything. Every mutation would then look
    detected.
    """

    def test_the_body_is_replaced_and_the_signature_survives(self):
        mutated = mutation.neutralize(SUBJECT, "refuse_unknown")
        self.assertIn("def refuse_unknown(kind):", mutated)
        self.assertIn("return None", mutated)
        self.assertNotIn("unknown kind", mutated)

    def test_the_mutated_module_still_imports_and_the_symbol_still_resolves(self):
        namespace: dict = {}
        exec(compile(mutation.neutralize(SUBJECT, "refuse_unknown"), "<mutated>", "exec"), namespace)
        self.assertIsNone(namespace["refuse_unknown"]("nonsense"),
                          "the neutralised function must resolve and return None")

    def test_the_docstring_is_kept_so_the_mutation_reads_as_a_diff(self):
        self.assertIn("Return a refusal string", mutation.neutralize(SUBJECT, "refuse_unknown"))

    def test_other_functions_are_untouched(self):
        namespace: dict = {}
        exec(compile(mutation.neutralize(SUBJECT, "refuse_unknown"), "<mutated>", "exec"), namespace)
        self.assertEqual(namespace["untouched"](3), 6,
                         "neutralising one symbol changed another, so a detected "
                         "mutation would not say which behaviour was missed")

    def test_an_unknown_symbol_is_refused_rather_than_silently_skipped(self):
        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.neutralize(SUBJECT, "no_such_function")
        self.assertIn("no_such_function", str(caught.exception))

    def test_a_function_whose_body_is_only_a_docstring_is_still_neutralised(self):
        source = 'def stub(a):\n    """Nothing yet."""\n'
        self.assertIn("return None", mutation.neutralize(source, "stub"))


class TheProofDiscriminates(unittest.TestCase):
    """The whole point: a test that asserts, versus one that only runs."""

    def test_a_test_that_asserts_the_behaviour_proves_the_mutation_detected(self):
        root = project(ASSERTS_THE_REFUSAL)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertTrue(record["passed_before"], record)
        self.assertTrue(record["failed_after"], record)
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["baseline_exit_code"], 0)
        self.assertNotEqual(record["mutated_exit_code"], 0)

    def test_a_test_that_asserts_nothing_is_refused_and_says_why(self):
        """The defect this exists for. The command passes both halves, so the
        only honest answer is that the test does not notice."""
        root = project(ASSERTS_NOTHING)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertTrue(record["passed_before"], record)
        self.assertFalse(record["failed_after"], record)
        self.assertFalse(record["ok"], record)
        self.assertIn("refuse_unknown", record["refusal"])
        self.assertIn("asserts nothing", record["refusal"])

    def test_an_already_failing_command_cannot_prove_anything(self):
        """A broken test 'detects' every mutation, including this one. Without
        the passed-before half, `ok` would be true for a suite that is simply
        red, which is the easiest possible way to forge this evidence."""
        root = project(ALREADY_BROKEN)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertFalse(record["passed_before"], record)
        self.assertFalse(record["ok"], record)
        self.assertIn("unmutated", record["refusal"])

    def test_a_timeout_is_not_a_detected_mutation(self):
        """A mutated run that never finished says nothing. Counting a timeout
        as a failure would make an infinite loop look like a proof."""
        root = project(ASSERTS_THE_REFUSAL)
        record = mutation.prove(root, command=f"{sys.executable} check.py",
                                target="bin/subject.py", symbol="refuse_unknown", timeout=60,
                                runner=lambda c, cwd, t: {"exit_code": None, "timed_out": True,
                                                          "tail": "timed out"})
        self.assertFalse(record["ok"], record)

    def test_the_record_names_the_version_of_the_surgery(self):
        root = project(ASSERTS_THE_REFUSAL)
        record = mutation.prove(root, command="true", target="bin/subject.py",
                                symbol="refuse_unknown", timeout=60,
                                runner=lambda c, cwd, t: {"exit_code": 0, "timed_out": False, "tail": ""})
        self.assertEqual(record["mutation_version"], mutation.MUTATION_VERSION)


class TheRealTreeIsNeverTouched(unittest.TestCase):
    """A mutation escaping into a checkout is the worst failure available to a
    tool whose product is trust."""

    def test_the_subject_file_is_byte_identical_afterwards(self):
        root = project(ASSERTS_THE_REFUSAL)
        before = (root / "bin" / "subject.py").read_bytes()
        mutation.prove(root, command=f"{sys.executable} check.py",
                       target="bin/subject.py", symbol="refuse_unknown", timeout=60)
        self.assertEqual((root / "bin" / "subject.py").read_bytes(), before,
                         "the mutation reached the caller's own tree")

    def test_the_command_runs_somewhere_other_than_the_project_root(self):
        """Proven by where the command executed, not by inspection: if the
        proof ran in `root` it would have to edit `root` to mutate anything."""
        root = project(ASSERTS_THE_REFUSAL)
        seen = []
        mutation.prove(root, command="true", target="bin/subject.py", symbol="refuse_unknown",
                       timeout=60,
                       runner=lambda c, cwd, t: (seen.append(Path(cwd)),
                                                 {"exit_code": 0, "timed_out": False, "tail": ""})[1])
        self.assertTrue(seen, "the runner was never called")
        for cwd in seen:
            self.assertNotEqual(cwd.resolve(), root.resolve(),
                                "the proof ran in the real project root")

    def test_a_source_file_changing_during_the_proof_is_refused(self):
        """The escape guard itself, not the absence of an escape.

        The engine found this gap in the first version of this suite: every
        other test here reads the subject's bytes directly, so `_source_digest`
        could be neutralised to `return None`, both digests became None, the
        comparison held, and all 34 tests passed. The guard against the worst
        failure available to this tool was not covered by anything.

        Simulated by a runner that writes to the real tree, which is exactly
        the shape of an escape: the proof is supposed to touch only its copy.
        """
        root = project(ASSERTS_THE_REFUSAL)
        subject = root / "bin" / "subject.py"

        def escaping(command, cwd, timeout):
            subject.write_text(subject.read_text() + "\n# written by the escape\n",
                               encoding="utf-8")
            return {"exit_code": 0, "timed_out": False, "tail": ""}

        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.prove(root, command="true", target="bin/subject.py",
                           symbol="refuse_unknown", runner=escaping)
        self.assertIn("changed during the proof", str(caught.exception))
        self.assertIn("refusing to record", str(caught.exception))

    def test_an_unrelated_change_during_the_proof_does_not_refuse(self):
        """The other half, and the reason the digest is narrow. The first
        version digested the whole tree and refused a legitimate proof, because
        a live run writes `handsoff-status.json` and liveness files throughout.
        A guard that fires on normal operation gets switched off."""
        root = project(ASSERTS_THE_REFUSAL)

        def writes_state(command, cwd, timeout):
            (root / "handsoff-status.json").write_text("{}", encoding="utf-8")
            return {"exit_code": 0, "timed_out": False, "tail": ""}

        record = mutation.prove(root, command="true", target="bin/subject.py",
                                symbol="refuse_unknown", runner=writes_state)
        self.assertFalse(record["ok"], "the stub runner passes both halves")
        self.assertIn("still passed", record["refusal"])

    def test_the_digest_covers_every_source_file_not_only_the_target(self):
        """An escape into a DIFFERENT module is still an escape."""
        root = project(ASSERTS_THE_REFUSAL)
        other = root / "bin" / "bystander.py"
        other.write_text("X = 1\n", encoding="utf-8")

        def escaping(command, cwd, timeout):
            other.write_text("X = 2\n", encoding="utf-8")
            return {"exit_code": 0, "timed_out": False, "tail": ""}

        with self.assertRaises(lib.HandsoffError):
            mutation.prove(root, command="true", target="bin/subject.py",
                           symbol="refuse_unknown", runner=escaping)

    def test_a_target_outside_the_root_is_refused(self):
        root = project(ASSERTS_THE_REFUSAL)
        with self.assertRaises(lib.HandsoffError):
            mutation.prove(root, command="true", target="../escape.py", symbol="refuse_unknown")

    def test_a_missing_target_is_refused_before_anything_runs(self):
        root = project(ASSERTS_THE_REFUSAL)
        calls = []
        with self.assertRaises(lib.HandsoffError):
            mutation.prove(root, command="true", target="bin/absent.py", symbol="refuse_unknown",
                           runner=lambda c, cwd, t: (calls.append(c),
                                                     {"exit_code": 0, "timed_out": False, "tail": ""})[1])
        self.assertEqual(calls, [], "the command ran against a target that does not exist")


class TheRefusalPointsAtTheDefinition(unittest.TestCase):
    """This engine re-exports hundreds of symbols, so an author naming the
    file they found a symbol in will frequently name a re-export. A correct
    refusal that does not say where the definition lives is useless."""

    def test_where_defined_finds_the_definition_and_not_the_reexport(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "bin" / "facade.py").write_text(
            "from subject import refuse_unknown  # noqa: F401\n", encoding="utf-8")
        self.assertEqual(mutation.where_defined(root, "refuse_unknown"), ["bin/subject.py"])

    def test_naming_the_reexport_is_refused_with_the_defining_file(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "bin" / "facade.py").write_text(
            "from subject import refuse_unknown  # noqa: F401\n", encoding="utf-8")
        with self.assertRaises(lib.HandsoffError) as caught:
            mutation.prove(root, command="true", target="bin/facade.py", symbol="refuse_unknown",
                           runner=lambda c, cwd, t: {"exit_code": 0, "timed_out": False, "tail": ""})
        message = str(caught.exception)
        self.assertIn("bin/subject.py", message)
        self.assertIn("re-export", message)

    def test_where_defined_tolerates_a_file_it_cannot_parse(self):
        root = project(ASSERTS_THE_REFUSAL)
        (root / "bin" / "broken.py").write_text("def (:\n", encoding="utf-8")
        self.assertEqual(mutation.where_defined(root, "refuse_unknown"), ["bin/subject.py"])


class ThePolicyIsPartOfTheClosedSets(unittest.TestCase):
    """The kind and the policy have to be registered, or `append_verification`
    rejects the record and `criterion_fully_evidenced` never requires it."""

    def test_the_ledger_accepts_the_kind(self):
        import handsoff_agent_runtime as runtime
        self.assertIn("mutation", runtime.VERIFICATION_KINDS)

    def test_a_policy_requires_both_the_checks_and_the_proof(self):
        self.assertEqual(lib.VERIFICATION_REQUIREMENTS["automated_and_mutation"],
                         {"checks", "mutation"})

    def test_a_criterion_under_that_policy_is_not_passing_on_checks_alone(self):
        criterion = {"id": "REQ-1", "verification": "automated_and_mutation", "evidence": ["vr-1"]}
        records = [{"run_id": "vr-1", "kind": "checks", "ok": True, "criteria": ["REQ-1"],
                    "criterion_hashes": {"REQ-1": lib.criterion_spec_hash(criterion)}}]
        self.assertFalse(lib.criterion_fully_evidenced(criterion, records),
                         "checks alone satisfied a policy that also requires the proof")

    def test_dropping_the_proof_requirement_is_a_downgrade(self):
        """Derived from the policy table, not from naming the strongest policy.
        The first version of `_verification_downgrade` hardcoded
        `automated_and_browser` as the top, so this policy could have been
        dropped to plain `automated` with nothing recorded."""
        reason = lib._verification_downgrade({"verification": "automated_and_mutation"},
                                             {"verification": "automated"})
        self.assertIsNotNone(reason)
        self.assertIn("mutation", reason)

    def test_adding_the_proof_requirement_is_not_a_downgrade(self):
        self.assertIsNone(lib._verification_downgrade({"verification": "automated"},
                                                      {"verification": "automated_and_mutation"}))

    def test_the_phase_five_gap_names_a_command_that_exists(self):
        """`record-evidence --kind mutation` exits 2: its choices are manual and
        browser. A gap message naming it would hand the reviewer a dead end.

        The first version of this test asserted the command NAME and stopped
        there. It kept passing when #349 removed `--target`/`--symbol` from the
        CLI while this message still printed them, so a review ran the printed
        instruction verbatim and got `unrecognized arguments`. The test below
        parses every flag out of the message and asks argparse itself whether it
        exists, which is the only version of this assertion that cannot rot.
        """
        gaps = lib.reviewer_launch_evidence_gaps(
            [{"id": "REQ-1", "verification": "automated_and_mutation", "evidence": []}], [])
        proof = [g for g in gaps if "mutation" in g]
        self.assertEqual(len(proof), 1, gaps)
        self.assertNotIn("record-evidence", proof[0])
        self.assertIn("mutation-proof REQ-1", proof[0])

        import re as _re
        flags_by_command = {}
        for command in ("criterion-update", "mutation-proof"):
            self.assertIn(command, proof[0], f"the message must name {command}")
            # The flags that follow this command in the message, up to the next
            # command name or the end.
            tail = proof[0].split(command, 1)[1]
            for other in ("criterion-update", "mutation-proof"):
                if other != command and other in tail:
                    tail = tail.split(other, 1)[0]
            flags_by_command[command] = set(_re.findall(r"--[a-z][a-z-]+", tail))
        self.assertTrue(flags_by_command["mutation-proof"],
                        "the message gives mutation-proof no flags at all")
        for command, flags in flags_by_command.items():
            accepted = _accepted_flags(command)
            self.assertTrue(accepted, f"could not read {command}'s flags from the CLI")
            for flag in sorted(flags):
                with self.subTest(command=command, flag=flag):
                    self.assertIn(flag, accepted,
                                  f"the gap message tells the reviewer to pass {flag} to "
                                  f"{command}, which the CLI rejects")


class TheCommandRefusesAndRecords(HandsoffTestCase):
    """End to end through the CLI, which is where the gate actually lives."""

    def arrange(self, policy="automated_and_mutation", tests=("true",),
                target="bin/handsoff_schema.py", symbol="validate_status_schema"):
        set_fixture_check_commands(self.tmp / "handsoff.toml", list(tests))
        self.init("Mutation proof fixture")
        args = ["criterion-update", "REQ-001", "--verification", policy,
                "--requirement", "Neutralising the named symbol fails this criterion's own tests"]
        for command in tests:
            args += ["--test", command]
        if target and policy == "automated_and_mutation":
            args += ["--mutation-target", target, "--mutation-symbol", symbol]
        r = run(args, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def write_subject_suite(self):
        """A real test of a real engine refusal, used by the proofs below."""
        (self.tmp / "tests").mkdir(exist_ok=True)
        (self.tmp / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.tmp / "tests" / "test_mutation_subject.py").write_text(textwrap.dedent('''
            import sys, unittest
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
            import handsoff_schema as schema


            class TheSchemaRefusesAnEmptyDocument(unittest.TestCase):
                def test_a_status_document_missing_everything_is_refused(self):
                    self.assertTrue(schema.validate_status_schema({}),
                                    "the schema accepted an empty status document")

                def test_an_acceptance_registry_missing_everything_is_refused(self):
                    self.assertTrue(schema.validate_acceptance_schema({}),
                                    "the schema accepted an empty acceptance registry")
        ''').lstrip(), encoding="utf-8")

    def test_the_policy_cannot_be_set_without_declaring_the_claim(self):
        """The companion to the refusal above: the engine will not let a
        criterion carry this policy with nothing declared, so the legacy shape
        is the only way in and the proof-time refusal is the backstop."""
        set_fixture_check_commands(self.tmp / "handsoff.toml", ["true"])
        self.init("Mutation proof fixture")
        r = run(["criterion-update", "REQ-001", "--verification", "automated_and_mutation",
                 "--requirement", "No declared symbol"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must declare", r.stdout)
        self.assertEqual(r.stdout.count("must declare"), 1,
                         "the refusal is printed twice: " + r.stdout)

    def test_a_criterion_whose_policy_does_not_require_it_is_refused(self):
        self.arrange(policy="automated")
        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("does not require mutation evidence", r.stdout)
        self.assertIn("automated_and_mutation", r.stdout,
                      "the refusal must name the policy that would accept it")

    def test_an_unknown_criterion_is_refused(self):
        self.arrange()
        r = run(["mutation-proof", "REQ-404", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("unknown criterion", r.stdout)

    def test_an_empty_actor_is_refused(self):
        self.arrange()
        r = run(["mutation-proof", "REQ-001", "--by", "   "], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("--by", r.stdout)

    def test_a_failed_proof_records_nothing_at_all(self):
        """`true` passes before and after. The ledger must stay exactly as it
        was: a record saying 'the test does not detect this' would be a durable
        artifact that looks like evidence and means its opposite."""
        self.arrange(tests=("true",))
        log = self.tmp / "handsoff-verifications.jsonl"
        before = log.read_text() if log.exists() else ""
        head_before = self.read_status().get("verification_head")

        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing recorded", r.stdout)
        self.assertEqual(json.loads(r.stdout.split("SHIP_FEATURE_BLOCKED")[0])["recorded"], False)
        self.assertEqual(log.read_text() if log.exists() else "", before,
                         "a refused proof appended to the ledger")
        self.assertEqual(self.read_status().get("verification_head"), head_before)
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertNotEqual(criterion["state"], "passing")

    def test_a_proof_that_holds_is_recorded_against_the_criterion(self):
        """The real thing: the criterion's own test suite asserts a refusal in
        `validate_status_schema`, and neutralising it makes that suite fail."""
        suite = ("python3 -m unittest tests.test_mutation_subject -q",)
        self.arrange(tests=suite)
        self.write_subject_suite()

        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["recorded"])
        self.assertEqual(payload["baseline_exit_code"], 0)
        self.assertNotEqual(payload["mutated_exit_code"], 0)

        records = [json.loads(line) for line in
                   (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines() if line.strip()]
        recorded = next(r for r in records if r["run_id"] == payload["run_id"])
        self.assertEqual(recorded["kind"], "mutation")
        self.assertTrue(recorded["ok"])
        self.assertEqual(recorded["criteria"], ["REQ-001"])
        self.assertEqual(recorded["commands"], list(suite))
        self.assertIn("validate_status_schema", recorded["description"])
        self.assertEqual(self.read_status()["verification_head"], recorded["hash"],
                         "the chain head did not move to the new record")
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertIn(payload["run_id"], criterion["evidence"])
        self.assertEqual(criterion["state"], "not_tested",
                         "the proof alone must not make it passing; the checks half is outstanding")

    def test_a_criterion_naming_no_test_is_refused(self):
        self.arrange()
        # Only reachable by a hand edit: `criterion-add --test` is required and
        # `criterion-update` cannot empty the list. fixture_state is how a test
        # puts such a document on disk without pretending a transition wrote it.
        acceptance = self.read_acceptance()
        for criterion in acceptance["criteria"]:
            if criterion["id"] == "REQ-001":
                criterion["tests"] = []
        cfg = lib.load_config(self.tmp)
        fixture_state.force_acceptance(self.tmp, cfg, acceptance, anchor=True)
        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("names no test command", r.stdout)

    def test_the_proof_chooses_neither_the_command_nor_the_symbol(self):
        """Nothing about what counts as proved is chosen at proof time.

        A review reproduced the alternative: with `--symbol` on the command
        line, an author could name a function called at import time that no
        test mentions, watch the mutated run fail on a load error, and record
        a proof. It also showed that such a proof stayed valid evidence for
        the criterion forever, since nothing bound the two.
        """
        r = run(["mutation-proof", "--help"], cwd=self.tmp)
        for flag in ("--command", "--target", "--symbol"):
            self.assertNotIn(flag, r.stdout,
                             f"{flag} lets the author choose what counts as proved")

    def test_a_criterion_declaring_no_symbol_is_refused_with_the_command_to_fix_it(self):
        """Only reachable for a criterion written before the fields existed:
        `criterion-add`/`criterion-update` now refuse the policy without them,
        which the sibling test below asserts. fixture_state is how a test puts
        such a legacy document on disk without pretending a transition wrote it."""
        self.arrange()
        acceptance = self.read_acceptance()
        for criterion in acceptance["criteria"]:
            criterion.pop("mutation_target", None)
            criterion.pop("mutation_symbol", None)
        cfg = lib.load_config(self.tmp)
        fixture_state.force_acceptance(self.tmp, cfg, acceptance, anchor=True)
        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("declares no mutation target", r.stdout)
        self.assertIn("--mutation-target", r.stdout,
                      "the refusal must name the command that fixes it")

    def test_changing_the_declared_symbol_discards_the_proof(self):
        """The binding, end to end. A proof is evidence for the symbol it
        neutralised; a criterion that now names a different one has not been
        proved, and the recorded proof must stop counting."""
        suite = ("python3 -m unittest tests.test_mutation_subject -q",)
        self.arrange(tests=suite)
        self.write_subject_suite()
        proof = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(proof.returncode, 0, proof.stdout + proof.stderr)
        before = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertTrue(before["evidence"])

        changed = run(["criterion-update", "REQ-001", "--mutation-symbol", "validate_acceptance_schema"],
                      cwd=self.tmp)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        after = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(after["evidence"], [],
                         "the proof of the old symbol still counts for the new claim")
        self.assertNotEqual(after["state"], "passing")

    def test_every_one_of_several_tests_must_pass_before_the_proof(self):
        """A criterion carrying two tests, one of them already red. The
        baseline half must fail, so nothing is recorded: the claim is that the
        criterion's tests detect the mutation, not that one of them does while
        another is broken."""
        self.arrange(tests=("true", "false"))
        r = run(["mutation-proof", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("unmutated", r.stdout)


class NoOtherPathCanWriteOne(HandsoffTestCase):
    """The claim the whole gate rests on: a `mutation` record in the ledger
    means the engine applied a mutation and the test failed. If any other
    command can write one, the gate is decorative."""

    def test_record_evidence_cannot_be_told_to_write_one(self):
        """`--kind` is a closed choice set of manual and browser. An operator
        attesting "I mutation-tested it" is the exact unverified claim this
        exists to stop, so there is no spelling of record-evidence that
        produces this kind."""
        set_fixture_check_commands(self.tmp / "handsoff.toml", ["true"])
        self.init("Mutation ledger fixture")
        r = run(["record-evidence", "REQ-001", "--kind", "mutation",
                 "--description", "I mutation-tested it", "--by", "test-implementer"],
                cwd=self.tmp)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("invalid choice", r.stderr)

    def test_only_one_call_site_in_the_engine_writes_this_kind(self):
        """Counted across the source, so a second writer added later shows up
        here rather than in a ledger nobody re-reads."""
        source = (BIN / "handsoff_supervisor.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('kind="mutation"'), 1,
                         "more than one place writes a mutation record; each one is a way "
                         "for the evidence to mean something other than a proof")
        for other in sorted(BIN.glob("handsoff_*.py")):
            if other.name == "handsoff_supervisor.py":
                continue
            with self.subTest(module=other.name):
                self.assertNotIn('kind="mutation"', other.read_text(encoding="utf-8"))

    def test_a_hand_appended_record_breaks_the_chain_and_is_refused(self):
        """The remaining route is editing the ledger file. The chain catches
        it, and this asserts the engine REFUSES rather than merely reporting,
        because a forged proof is the one record that must never be trusted."""
        set_fixture_check_commands(self.tmp / "handsoff.toml", ["true"])
        self.init("Mutation ledger fixture")
        log = self.tmp / "handsoff-verifications.jsonl"
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        import handsoff_lib as engine
        forged = {
            "run_id": "vr-" + "f" * 32, "at": "2026-10-02T00:00:00+00:00",
            "kind": "mutation", "ok": True, "by": "test-implementer",
            "criteria": ["REQ-001"],
            "criterion_hashes": {"REQ-001": engine.criterion_spec_hash(criterion)},
            "results": [], "commands": ["true"], "description": "forged",
            "prev_hash": None, "hash": "0" * 64,
        }
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged) + "\n")
        r = run(["validate"], cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0,
                            "a forged mutation record passed validation: " + r.stdout)
        self.assertIn("SHIP_FEATURE", r.stdout + r.stderr)


class TheTransactionPathEnforcesTheSameRule(HandsoffTestCase):
    """`criteria-apply` is a THIRD way into the registry, beside criterion-add
    and criterion-update, and it had its own copy of the merge.

    Found while checking the review's question about it: `CRITERION_UPDATE_FIELDS`
    listed the two new fields, so the key check accepted them, and the apply loop
    then dropped them. Accepted and silently discarded is write-without-read, in
    the one place that decides what a criterion claims.
    """

    def setUp(self):
        super().setUp()
        set_fixture_check_commands(self.tmp / "handsoff.toml", ["true"])
        self.init("Transaction fixture")

    def apply(self, *operations):
        path = self.tmp / "tx.json"
        path.write_text(json.dumps({"operations": list(operations)}), encoding="utf-8")
        return run(["criteria-apply", "--file", "tx.json", "--by", "test-architect"], cwd=self.tmp)

    def criterion(self, cid):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def test_an_added_criterion_can_declare_the_pair(self):
        r = self.apply({"op": "add", "criterion": {
            "id": "REQ-900", "type": "supporting", "requirement": "The tests detect its loss",
            "verification": "automated_and_mutation", "tests": ["true"],
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        added = self.criterion("REQ-900")
        self.assertEqual(added["mutation_target"], "bin/handsoff_schema.py")
        self.assertEqual(added["mutation_symbol"], "validate_status_schema")

    def test_adding_the_policy_without_the_pair_is_refused(self):
        r = self.apply({"op": "add", "criterion": {
            "id": "REQ-901", "type": "supporting", "requirement": "No declared symbol",
            "verification": "automated_and_mutation", "tests": ["true"]}})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must declare", r.stdout)

    def test_an_update_can_set_the_pair_and_it_is_not_dropped(self):
        r = self.apply({"op": "update", "id": "REQ-001", "fields": {
            "requirement": "The tests detect the loss of the named function",
            "verification": "automated_and_mutation", "tests": ["true"],
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        updated = self.criterion("REQ-001")
        self.assertEqual(updated["mutation_symbol"], "validate_status_schema",
                         "the transaction accepted the field and then dropped it")

    def test_switching_an_existing_criterion_to_the_policy_without_the_pair_is_refused(self):
        """Judged on the RESULTING criterion, not on the fields being changed.
        On `fields` alone this was accepted and only failed at proof time."""
        r = self.apply({"op": "update", "id": "REQ-001",
                        "fields": {"verification": "automated_and_mutation"}})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must declare", r.stdout)

    def test_an_add_without_the_pair_is_still_accepted(self):
        """Every transaction file written before these fields existed must
        still apply, so the pair is optional in the spec."""
        r = self.apply({"op": "add", "criterion": {
            "id": "REQ-902", "type": "supporting", "requirement": "An ordinary automated criterion",
            "verification": "automated", "tests": ["true"]}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        added = self.criterion("REQ-902")
        for field in ("mutation_target", "mutation_symbol"):
            self.assertNotIn(field, added,
                             "an absent field was written as null, which changes the criterion "
                             "spec hash and would discard recorded evidence")

    def test_an_unknown_field_is_still_refused(self):
        r = self.apply({"op": "add", "criterion": {
            "id": "REQ-903", "type": "supporting", "requirement": "r",
            "verification": "automated", "tests": ["true"], "mutation_targets": "typo"}})
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_renaming_only_the_symbol_is_accepted(self):
        """Round 3 finding 5. A criterion that already carries both fields, with
        only the symbol renamed, was refused with "must be set together", which
        is misleading because they already were. `cmd_criterion_update`
        backfills from the stored criterion before validating; this path had no
        equivalent, so the transaction was strictly less capable than the
        single-criterion CLI for the same edit."""
        added = self.apply({"op": "add", "criterion": {
            "id": "REQ-904", "type": "supporting", "requirement": "The tests detect its loss",
            "verification": "automated_and_mutation", "tests": ["true"],
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        renamed = self.apply({"op": "update", "id": "REQ-904",
                              "fields": {"mutation_symbol": "validate_acceptance_schema"}})
        self.assertEqual(renamed.returncode, 0, renamed.stdout + renamed.stderr)
        criterion = self.criterion("REQ-904")
        self.assertEqual(criterion["mutation_symbol"], "validate_acceptance_schema")
        self.assertEqual(criterion["mutation_target"], "bin/handsoff_schema.py",
                         "the untouched half of the pair was lost")

    def test_renaming_only_the_target_is_accepted(self):
        added = self.apply({"op": "add", "criterion": {
            "id": "REQ-905", "type": "supporting", "requirement": "The tests detect its loss",
            "verification": "automated_and_mutation", "tests": ["true"],
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        moved = self.apply({"op": "update", "id": "REQ-905",
                            "fields": {"mutation_target": "bin/handsoff_workflow.py"}})
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertEqual(self.criterion("REQ-905")["mutation_target"], "bin/handsoff_workflow.py")

    def test_setting_half_the_pair_on_a_criterion_that_has_neither_is_refused(self):
        """The rule still holds where there is nothing to backfill from."""
        r = self.apply({"op": "update", "id": "REQ-001",
                        "fields": {"mutation_symbol": "validate_status_schema"}})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must be set together", r.stdout)

    def test_clearing_one_half_of_the_pair_is_refused(self):
        added = self.apply({"op": "add", "criterion": {
            "id": "REQ-906", "type": "supporting", "requirement": "The tests detect its loss",
            "verification": "automated_and_mutation", "tests": ["true"],
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        r = self.apply({"op": "update", "id": "REQ-906", "fields": {"mutation_symbol": ""}})
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_setting_the_pair_discards_the_recorded_evidence(self):
        """Changing what a criterion claims cannot leave the old proof counting."""
        # init's placeholder test is not in [checks].commands, so point the
        # criterion at the one command this fixture configures.
        pointed = run(["criterion-update", "REQ-001", "--test", "true"], cwd=self.tmp)
        self.assertEqual(pointed.returncode, 0, pointed.stdout + pointed.stderr)
        verified = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertTrue(self.criterion("REQ-001")["evidence"])
        r = self.apply({"op": "update", "id": "REQ-001", "fields": {
            "mutation_target": "bin/handsoff_schema.py", "mutation_symbol": "validate_status_schema"}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.criterion("REQ-001")["evidence"], [],
                         "a proof of the previous claim still counts for the new one")


class AnAbsentFieldHashesExactlyAsBefore(unittest.TestCase):
    """The compatibility claim with teeth. `criterion_spec_hash` hashes the
    field SET, so a criterion carrying an explicit null hashes differently from
    one without the key. Every recorded evidence binding in every project would
    break if any path wrote null, which is why each path above writes the field
    only when it has a value."""

    BASE = {"id": "REQ-X", "type": "supporting", "requirement": "r",
            "verification": "automated", "tests": ["true"],
            "state": "not_tested", "evidence": []}

    def test_absent_and_null_are_not_the_same_hash(self):
        self.assertNotEqual(
            lib.criterion_spec_hash(self.BASE),
            lib.criterion_spec_hash({**self.BASE, "mutation_target": None, "mutation_symbol": None}),
            "if these were equal the guard below would be unnecessary; it is not")

    def test_no_engine_path_writes_the_fields_as_null(self):
        """Read off the source, because the cost of being wrong is every
        recorded criterion in every project at once."""
        for module in ("handsoff_supervisor.py", "handsoff_workflow.py"):
            source = (BIN / module).read_text(encoding="utf-8")
            for field in ("mutation_target", "mutation_symbol"):
                self.assertNotIn(f'criterion["{field}"] = None', source,
                                 f"{module} writes {field} as null onto a criterion")

    def test_every_criterion_without_the_fields_keeps_its_recorded_hash(self):
        """Against this repository's own acceptance registry, whose criteria
        the engine really recorded and whose evidence really binds to these
        hashes. An earlier draft of this test read a status file with no inline
        criteria and SKIPPED, which proves nothing; that is the shape of
        defect this suite exists to catch."""
        registry = ROOT / "handsoff-acceptance.json"
        if not registry.is_file():
            # Not a skip dressed as a pass: this file is run state, absent in a
            # fresh clone, and the claim is then carried by the two tests above.
            return
        criteria = json.loads(registry.read_text(encoding="utf-8")).get("criteria") or []
        self.assertTrue(criteria, "the registry carries no criteria")
        without = [c for c in criteria if "mutation_target" not in c]
        self.assertTrue(without, "every criterion declares a mutation target; nothing to prove")
        for criterion in without:
            with self.subTest(criterion=criterion["id"]):
                stripped = {k: v for k, v in criterion.items()
                            if k not in ("mutation_target", "mutation_symbol")}
                self.assertEqual(lib.criterion_spec_hash(stripped),
                                 lib.criterion_spec_hash(criterion),
                                 "adding the fields to the engine changed this criterion's hash")


class TheProofDoesNotHoldTheProjectLock(HandsoffTestCase):
    """Round 3 finding 3, pinned behaviourally.

    The whole proof ran inside one `with project_lock`: three full runs of the
    criterion's suite, each bounded by `--timeout` (900s by default), holding a
    blocking `fcntl.flock` with no timeout. `heartbeat` takes the same lock, so
    a slow suite silently blocked the liveness signal the watchdog reads, which
    is the exact failure the watchdog exists to catch.

    Asserted by trying to take the lock from inside the proof, rather than by
    reading the source: the indentation of a `with` block is precisely the kind
    of thing that looks right and is not.
    """

    def setUp(self):
        super().setUp()
        set_fixture_check_commands(self.tmp / "handsoff.toml", ["true"])
        self.init("Lock scope fixture")
        r = run(["criterion-update", "REQ-001", "--verification", "automated_and_mutation",
                 "--requirement", "The tests detect the loss of the named function",
                 "--test", "true", "--mutation-target", "bin/handsoff_schema.py",
                 "--mutation-symbol", "validate_status_schema"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def proof_args(self, criterion="REQ-001", by="test-implementer", timeout=60):
        """Parsed from real argv.

        A hand-built Namespace drifted the moment the CLI gained
        `--total-timeout`: three tests errored with AttributeError against code
        that was correct. Going through `build_parser` means the test exercises
        the same argument surface an operator does, and a new required flag
        shows up as a parser error rather than as a mystery attribute.
        """
        return supervisor.build_parser().parse_args(
            ["--root", str(self.tmp), "mutation-proof", criterion, "--by", by,
             "--timeout", str(timeout)])

    def lock_is_free(self):
        """True when nothing holds the project lock right now."""
        import fcntl
        path = lib.lock_path(self.tmp)
        path.touch(exist_ok=True)
        with path.open("r+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True

    def test_the_lock_is_free_while_the_proof_runs(self):
        observed = {}

        def proving(root, **kwargs):
            # Called where `mutation.prove` would be, so this is exactly the
            # window during which the suite runs.
            observed["free_during_proof"] = self.lock_is_free()
            return {"mutation_version": 1, "command": kwargs["command"],
                    "target": kwargs["target"], "symbol": kwargs["symbol"],
                    "baseline_exit_code": 0, "control_exit_code": 0, "mutated_exit_code": 1,
                    "passed_before": True, "reproducible": True, "failed_after": True,
                    "import_intact": True, "ok": True,
                    "source_digest": mutation.source_digest(root, kwargs["target"])}

        with patch_engine("prove", side_effect=proving):
            exit_code = supervisor.cmd_mutation_proof(self.proof_args())
        self.assertEqual(exit_code, 0)
        self.assertTrue(observed.get("free_during_proof"),
                        "the project lock was held while the proof ran, so heartbeat and every "
                        "other lock-taking command would block for the whole suite")

    def test_the_lock_is_taken_again_to_record(self):
        """The other half: released for the proof, not abandoned. Without the
        second acquisition two proofs could interleave their writes."""
        source = (BIN / "handsoff_supervisor.py").read_text(encoding="utf-8")
        body = source[source.index("def cmd_mutation_proof"):]
        body = body[:body.index("\ndef ", 1)]
        self.assertEqual(body.count("with lib.project_lock(root):"), 2,
                         "the command must take the lock twice: once to read the claim, once "
                         "to record the result")
        self.assertLess(body.index("mutation.prove("),
                        body.rindex("with lib.project_lock(root):"),
                        "the proof must run before the recording lock is taken")

    def test_a_claim_that_changed_during_the_proof_is_refused(self):
        """What the released lock gave up is re-established on reacquisition.
        A criterion edited mid-proof must not receive the old proof."""
        def proving(root, **kwargs):
            edited = run(["criterion-update", "REQ-001", "--mutation-symbol",
                          "validate_acceptance_schema"], cwd=self.tmp)
            self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
            return {"mutation_version": 1, "command": kwargs["command"],
                    "target": kwargs["target"], "symbol": kwargs["symbol"],
                    "baseline_exit_code": 0, "control_exit_code": 0, "mutated_exit_code": 1,
                    "passed_before": True, "reproducible": True, "failed_after": True,
                    "import_intact": True, "ok": True,
                    "source_digest": mutation.source_digest(root, kwargs["target"])}

        with patch_engine("prove", side_effect=proving):
            exit_code = supervisor.cmd_mutation_proof(self.proof_args())
        self.assertEqual(exit_code, 1, "a proof of the previous claim was recorded")
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(criterion["evidence"], [])

    def test_a_tree_that_changed_during_the_proof_is_refused(self):
        def proving(root, **kwargs):
            digest = mutation.source_digest(root, kwargs["target"])
            (self.tmp / "bin" / "handsoff_schema.py").write_text(
                (self.tmp / "bin" / "handsoff_schema.py").read_text() + "\n# edited mid-proof\n",
                encoding="utf-8")
            return {"mutation_version": 1, "command": kwargs["command"],
                    "target": kwargs["target"], "symbol": kwargs["symbol"],
                    "baseline_exit_code": 0, "control_exit_code": 0, "mutated_exit_code": 1,
                    "passed_before": True, "reproducible": True, "failed_after": True,
                    "import_intact": True, "ok": True, "source_digest": digest}

        with patch_engine("prove", side_effect=proving):
            exit_code = supervisor.cmd_mutation_proof(self.proof_args())
        self.assertEqual(exit_code, 1, "a proof describing a tree that is gone was recorded")


class TheCommandIsRegisteredLikeEveryOther(unittest.TestCase):
    def test_it_appears_in_the_command_classification(self):
        source = (BIN / "handsoff_supervisor.py").read_text(encoding="utf-8")
        self.assertIn('"mutation-proof": {"class"', source,
                      "an unclassified command is invisible to the observability audit")

    def test_the_cli_exposes_it(self):
        done = subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertIn("mutation-proof", done.stdout)


if __name__ == "__main__":
    unittest.main()
