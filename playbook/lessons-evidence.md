# Lessons: evidence that holds

Manifests, tests, proofs and the claims they support.

Each one cost a round. When a refusal, a wasted CI round or a stalled
session was caused by process (not the code), add one bullet to the
fitting file here, with the refusal line it prevents; a bullet that gets
broken twice becomes a refusal in the engine. Add, do not evict: if a
file no longer fits its budget, tests/test_playbook_budget.py says so
with the number.

- Regenerate the runtime manifest after the LAST edit to bin/, prompts/,
  schemas/ or dashboard/. The engine and the preflight both refuse by name.
- Serve the lane's dashboard from the worktree's engine, not the installed one.
- A proof of the lane's own change runs the worktree's `bin/`, not the
  installed CLI; the installed engine predates the change by definition.
- Run the WHOLE node suite (nine seconds) and the modules your files cite
  before pushing.
- Local timings are not evidence: a laptop sleeps and a 50-second sleep once
  took 23 minutes. `caffeinate -is`; read durations off the CI runner.
- Fixtures never inherit the dogfood `handsoff.toml` and never share a fleet
  register; both made whole modules red on `main`.
- Measure a new external call before it goes on a request path: `pmset -g log`
  was 33,000 lines and three seconds and blocked a snapshot. Cache it, read it
  in the background.
- A red shard on a green tree: `gh run rerun <id> --failed`; a real failure is
  a new commit and a full run.
- Unit tests with a fake on PATH cannot see an install path: `sys.executable`
  inside a macOS framework venv is the framework binary, not `venv/bin`,
  and only the live smoke against the installed pair caught it. Every
  "the installed X finds Y" claim gets a live smoke.
- An exactly-once hand-off over Beakon rides on the gateway's content digest:
  deterministic bytes per item, the marker with those bytes saved before
  transmit, the marker's own bytes retransmitted (never a rebuild from an
  issue that may have been edited), the receipt saved before the comment.
- A Phase 8 run from a checkout runs under the system interpreter: anything
  the engine finds through its own venv (the Miner) is absent there and the
  trigger takes its skip path; do not read that ledger as "the install works".
- Before the push, run every suite whose files you touched, not the one you
  wrote: a new first line of output (`INSTALL_CHECK_OK`) and a new row in a
  panel broke three neighbouring suites on CI, one round each.
- "Add no command to [checks]" reads as "remove the ones there". Say "do not
  edit handsoff.toml at all"; a removed command fails `verify` with
  "criterion tests must exactly match configured check commands".
- A test that zeroes a field to make a count come out proves a state the
  engine cannot produce: `phases_waived = []` never happens on a design lane,
  and the real union kept all eight phases while the suite stayed green.
- A schema keyword the in-repo validator does not implement enforces nothing.
  `allOf`/`if`/`then` in snapshot.schema.json read as a tighter constraint and
  silently replaced `minItems: 8`; a full run truncated to one phase validated
  clean. Extend the validator or keep the constraint in the test.
- Assert the consumer, not the producer. `lane_status` was emitted, asserted,
  and never read by renderPhases, so the strip was identical on the page.
- Follow the value to where a person sees it. Twice this run a criterion was
  verified on a green suite plus an existing mechanism, and twice the value
  stopped before the page.
- A command that prints SHIP_FEATURE_BLOCKED can still exit 0: read the
  output, never `cmd >/dev/null && echo OK`.
- Commit the regenerated manifest WITH the bin/ change that made it stale,
  in the same commit. Regenerating after the commit leaves main carrying a
  digest that matches neither the old file nor the new one.
- Assert behaviour, not existence: a test that a thread existed passed while it
  retired at the first pause.
- Quote the measurement that hurts.
- Board and CLI disagreeing: suspect the QUERY first. A granted authorization
  read as None because the host polled the wrong key (#308).
- A derived-set test must not pass vacuously: name what it analyses, refuse a
  computed name, floor the count.
