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
    BIN, HandsoffTestCase, run, set_fixture_check_commands)

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_mutation as mutation  # noqa: E402


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
        browser. A gap message naming it would hand the reviewer a dead end."""
        gaps = lib.reviewer_launch_evidence_gaps(
            [{"id": "REQ-1", "verification": "automated_and_mutation", "evidence": []}], [])
        proof = [g for g in gaps if "mutation" in g]
        self.assertEqual(len(proof), 1, gaps)
        self.assertIn("mutation-proof REQ-1", proof[0])
        self.assertNotIn("record-evidence", proof[0])


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
