# Lessons: would this test notice if the code broke

Split from lessons-evidence.md when #349 added a mechanism for the
question and the answers filled the file. Every lesson here comes from
the same discovery: a gate that records a command exiting zero records
nothing about whether the command would still exit zero with the
implementation gutted. `handsoff_supervisor.py mutation-proof CRITERION
--by ACTOR` applies the mutation itself and refuses evidence from a test
that still passes.

- A green suite is not a tested suite. Stubbing one refusal in
  `validate_status_schema` to `return []` passed all 1,727 tests. Before
  claiming a criterion is covered, neutralise the function it names and watch
  the test fail: `mutation-proof CRIT --target FILE --symbol FUNCTION --by ME`.
  The engine applies the mutation in a throwaway copy and records nothing when
  the test still passes.
- Mutation-prove your own new tests, not only the code. Four of the suites in
  this run had a symbol that could be gutted while every test passed, and the
  one that mattered was the guard against a mutation escaping into the real
  checkout.
- A test whose evidence lives in gitignored local state passes on the author's
  machine and nowhere else. A sweep over `.handsoff-archive/` read as proof of
  archive compatibility here, and would have failed in CI and inside a
  mutation copy; the fix is a real archived document committed as a fixture.
- A message that names a command is not a tested message. Removing two flags
  from a CLI left the Phase 5 gap message printing them, and the test passed
  because it checked the command NAME. Parse the flags out of the message and
  ask argparse whether each one exists.
- Never hold the project lock across work that writes no state. Three suite
  runs inside one `with project_lock` blocked `heartbeat`, which is the signal
  the watchdog reads. Release it, and re-establish what it protected on
  reacquisition: the spec hash and the source digest, re-checked.
- A hand-built argparse Namespace in a test drifts the moment the CLI gains a
  flag. Parse real argv through the parser, so a new required flag is a parser
  error rather than an AttributeError in code that was correct.
- No fixed number of repeats makes an unsound command sound. Any N is defeated
  by an N+1-strike resource, and a flaky suite forges a result by chance.
  Repetition buys a confidence level; report it as one and name the residual.
- When a limit cannot be fixed, write it down at full width. A limit described
  more narrowly than it is reads as exotic and gets discounted.
- Assert behaviour, not existence: a test that a thread existed passed while it
  retired at the first pause.
- A derived-set test must not pass vacuously: name what it analyses, refuse a
  computed name, floor the count.
- A command that prints SHIP_FEATURE_BLOCKED can still exit 0: read the
  output, never `cmd >/dev/null && echo OK`.
