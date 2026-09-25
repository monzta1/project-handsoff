# Lessons: boundaries, and tests that do not reach what they name

Split from lessons-evidence.md when the monolith split (#284) started
producing a defect shape of its own: a test that names the right thing and
never reaches it, because a name has more than one binding or because the
input never satisfied the condition under test.

- A patch replaces one binding, not a symbol. After a split, `from X import f`
  gives each importing module its own binding; `mock.patch.object(lib, "f")`
  replaces the monolith's only. Two tests stayed green while exercising the real
  code: a commit that ran inside another module, and a sleep fixture that read
  the operator's real pmset log, 281 intervals where it supplied 1. Whether a
  patch lands depends on the call path, which the syntax does not reveal, so
  patch every binding (`tests/engine_patch.patch_engine`) instead of reasoning
  about which one runs.
- When a rule cannot be decided, change the shape so it cannot be broken. Four
  attempts at deciding early binding statically each gave false positives;
  making the inert patch unexpressible took one helper and no judgement calls.
- A pattern pinned to today's magnitude fails tomorrow for a reason unrelated
  to what it checks: `\b1\d,\d{3}\b` matched the line-count claim until the
  file dropped under 10,000 lines, then failed whatever the document said.
- The count of recorded cases is not coverage. A contract fixture of 39 cases
  had 15 recording `[]` or `null`, so stubbing `ci_gate_errors` to `return []`
  passed the whole suite. Two causes, both about reaching the code: the input
  used state "FAILURE" where the function tests for "failed", and the evidence
  records used `{"criterion": id}` where the ledger writes `{"criteria": [id],
  "criterion_hashes": {...}}`. Require every function to have one case whose
  value is not what a broken function returns by accident, and keep the empty
  halves as the other side of a pair.
- Build a fixture's inputs from a really initialized project. Three attempts at
  hand-building an acceptance registry produced only validation refusals, each
  revealing one more required field; a fixture of matching refusals on both
  sides proves nothing about the change.
- Mutate the code, not the test, to find out whether a test works. Seven stubs
  in one extraction, each reverted straight after: five caught, and the two
  that were not pointed at cases that asserted nothing.
- Check the delimiter before trusting a negative result. A shell loop split its
  targets on `|`, which appears in `-> str | None`, so one "uncaught mutation"
  was a file that was never mutated.
