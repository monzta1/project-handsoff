"""#284 criterion 3: state transitions validate proposed state before persistence.

**This criterion is not met, and this module records how far off it is
rather than asserting a property that does not hold.** It was the criterion
I closed #284 on without examining, so the point here is a number that
cannot drift, not a claim.

Measured across `handsoff_lib`, `handsoff_supervisor` and
`handsoff_ledger`: 71 functions commit a status. 55 validate the proposed
state first, by schema validation or by computing the gates
(`compute_errors`, `gate_progress`, `completion_error`,
`lane_gate_refusal`). 16 do not.

A first attempt at this measurement looked only for
`validate_status_schema` and reported 56 unvalidated, which would have been
a false alarm: `cmd_advance` validates through `compute_errors` and the
gate helpers, not the schema validator. A narrow query is evidence about
the query.

Most of the 16 are not transitions at all: `ci_view`, `cmd_heartbeat`,
`cmd_background_wait_start`/`_end` and `cmd_design_review_packet` record
observations, and `cmd_init` creates a run with no prior state to validate
against. Several are genuine transitions and are the real gap:
`record_stall_transition`, `cmd_human_pause_start`/`_end`, `cmd_recover`,
`record_design_decline`, `cmd_amendment_review`.

The extraction did not change any of this; the functions moved wholesale.
The ratchet below stops it getting worse while the gap is closed.
"""
import ast
import pathlib
import unittest

from tests.test_handsoff_supervisor import BIN

SOURCES = ("handsoff_lib", "handsoff_supervisor", "handsoff_ledger")

#: Ways a function can validate the state it is about to persist.
VALIDATORS = ("validate_status_schema", "validate_acceptance_schema", "compute_errors",
              "gate_progress", "completion_error", "lane_gate_refusal",
              "ensure_no_launched_regression", "_assert_agent_telemetry_integrity",
              "refusal", "validate_")

#: The functions that commit a status without validating it first, as measured
#: at stage 4. This set may SHRINK and must never grow: a new unvalidated
#: write is a new way to persist a state nothing checked.
#:
#: Not transitions (they record an observation, or create the run):
#:   ci_view, ci_watch_start, questions_prompt_section, cmd_heartbeat,
#:   cmd_background_wait_start, cmd_background_wait_end,
#:   cmd_design_review_packet, cmd_init
#: Genuine transitions, and the actual gap criterion 3 names:
#:   record_stall_transition, record_design_decline, cmd_human_pause_start,
#:   cmd_human_pause_end, cmd_recover, cmd_amendment_review,
#:   cmd_design_review_authorize, cmd_design_review_escalate
KNOWN_UNVALIDATED = {
    "handsoff_lib.ci_view",
    "handsoff_lib.ci_watch_start",
    "handsoff_lib.questions_prompt_section",
    "handsoff_lib.record_design_decline",
    "handsoff_lib.record_stall_transition",
    "handsoff_supervisor.cmd_amendment_review",
    "handsoff_supervisor.cmd_background_wait_end",
    "handsoff_supervisor.cmd_background_wait_start",
    "handsoff_supervisor.cmd_design_review_authorize",
    "handsoff_supervisor.cmd_design_review_escalate",
    "handsoff_supervisor.cmd_design_review_packet",
    "handsoff_supervisor.cmd_heartbeat",
    "handsoff_supervisor.cmd_human_pause_end",
    "handsoff_supervisor.cmd_human_pause_start",
    "handsoff_supervisor.cmd_init",
    "handsoff_supervisor.cmd_recover",
}


def _measure():
    validated, unvalidated = set(), set()
    for module in SOURCES:
        path = BIN / f"{module}.py"
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            commits = any(
                isinstance(c, ast.Call)
                and getattr(c.func, "id", getattr(c.func, "attr", None)) == "commit"
                and any(k.arg == "status" for k in c.keywords)
                for c in ast.walk(fn))
            if not commits:
                continue
            calls = {getattr(c.func, "attr", getattr(c.func, "id", ""))
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            name = f"{module}.{fn.name}"
            if any(any(v in c for v in VALIDATORS) for c in calls):
                validated.add(name)
            else:
                unvalidated.add(name)
    return validated, unvalidated


class TheGapIsPinnedAndMayOnlyShrink(unittest.TestCase):
    """The ratchet. Criterion 3 is open; this stops it widening."""

    @classmethod
    def setUpClass(cls):
        cls.validated, cls.unvalidated = _measure()

    def test_no_new_unvalidated_status_write_appears(self):
        new = sorted(self.unvalidated - KNOWN_UNVALIDATED)
        self.assertEqual(new, [],
                         "these functions persist a status without validating the proposed "
                         "state first, and are not in the recorded set. Either validate "
                         "before commit, or add it with the reason it is not a transition.")

    def test_a_closed_gap_is_removed_from_the_record(self):
        """An entry kept after it starts validating overstates the gap."""
        stale = sorted(KNOWN_UNVALIDATED - self.unvalidated)
        self.assertEqual(stale, [],
                         "these now validate; remove them from KNOWN_UNVALIDATED so the "
                         "recorded gap stays honest")

    def test_the_majority_already_validate(self):
        """Guards against a measurement that silently stops finding anything."""
        self.assertGreaterEqual(len(self.validated), 50,
                                f"only {len(self.validated)} validated; the matcher is broken")
        self.assertGreater(len(self.validated), len(self.unvalidated))

    def test_the_measurement_is_not_vacuous(self):
        total = len(self.validated) + len(self.unvalidated)
        self.assertGreaterEqual(total, 65, f"only found {total} status-committing functions")


class TheLedgerDoesNotValidateOnBehalfOfCallers(unittest.TestCase):
    """Why this is a caller-side property at all.

    `commit` documents it: "Caller must hold project_lock for the entire
    surrounding read-validate-write". If it validated internally this
    criterion would be structural rather than a per-call-site habit, and
    that is the obvious way to close the gap.
    """

    def test_commit_does_not_validate_the_status_it_writes(self):
        src = (BIN / "handsoff_ledger.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "commit")
        body = ast.get_source_segment(src, fn) or ""
        self.assertNotIn("validate_status_schema", body,
                         "commit now validates; criterion 3 can become structural and this "
                         "test and the ratchet above should be replaced by that")

    def test_commit_documents_that_the_caller_validates(self):
        src = (BIN / "handsoff_ledger.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "commit")
        self.assertIn("read-validate-write", ast.get_docstring(fn) or "")


if __name__ == "__main__":
    unittest.main()
