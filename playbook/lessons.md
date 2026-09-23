# Lessons, each one cost a round

Read once per session. When a refusal, a wasted CI round or a stalled
session was caused by process (not the code), add one bullet here in the
same session, with the refusal line it prevents; a bullet that gets broken
twice becomes a refusal in the engine.

- Regenerate the runtime manifest after the LAST edit to bin/, prompts/,
  schemas/ or dashboard/ and BEFORE launching any reviewer or running any
  suite. The engine now refuses with "the runtime manifest is stale (...):
  run python3 bin/handsoff_manifest.py --version vX.Y.Z, then retry".
- Never launch a reviewer until `design-propose` has printed
  DESIGN_PROPOSAL_RECORDED for the revision you want reviewed. A launch you
  then kill leaves a "live" session the watchdog holds for ten minutes.
- Serve the lane's dashboard from the worktree's engine, not the installed
  `handsoff`, or the page cannot show the lane's own changes. Copy the
  version pin into a fresh worktree (`init` writes it from v0.3.51).
- A proof of the lane's own change runs the worktree's `bin/`, not the
  installed CLI; the installed engine predates the change by definition.
- Run the WHOLE node suite (nine seconds) and the modules your files cite
  before pushing.
- Landing order: merge, `advance 7`, release with an ANNOTATED tag on the
  merged sha, install, kickstart Fleet, wait a minute, `verify-live`, then
  `advance 8`; if it names engine:version, `record-review --reaffirm`.
- A rerun on the same head greens a red CI watch by itself from v0.3.52.
- One `[#N]` tag per criterion scopes one item.
- Local timings are not evidence: a laptop sleeps after a minute idle and a
  50-second sleep once took 23 minutes of wall clock. `caffeinate -is` for
  the session; read durations off the CI runner; the board subtracts sleep
  from v0.3.56.
- `init --by <actor>` so the page names the host; without it the family is
  read from the ledger's actors.
- Fixtures never inherit the dogfood `handsoff.toml` (normalize it) and never
  share a fleet register; both made whole modules red on `main`.
- Measure a new external call before it goes on a request path: `pmset -g
  log` was 33,000 lines and three seconds, and a first snapshot waited for
  it until the offline smoke caught the page unrendered. A shell command in a
  snapshot needs a cache and a background read.
- Two hosts release from one machine: read the next patch number from `gh
  release list` at bump time; the second lane to land rebases, checks every
  merged file parses, regenerates the manifest, re-verifies, reaffirms.
- A criterion that says "every reader" gets one test per reader, named in
  the criterion; a claim in a commit message is not evidence.
- The board sees nothing while a host implements; `advance 4` before
  building.
- A red shard on a green tree: `gh run rerun <id> --failed`; a real failure is
  a new commit and a full run.
- `init --item` takes `#174` or `issue-174`, never a bare `174`: a bare
  number becomes an "ask" item with no ticket, the run is never registered
  with Fleet, and the card is missing until `fleet register` by hand.
- The implementation reviewer is launched from Phase 5: at Phase 4 its
  result is persisted but refused ("requires Phase 5"); `advance 5
  --implemented-by`, then `session-result-adopt --session <sid>` recovers it.
- Wait for a managed session on the LEDGER (the session's state leaving
  launching/running), never by grepping its log: the test suites the
  reviewer runs print `HANDSOFF_REVIEW_RESULT` and `SHIP_FEATURE_BLOCKED`
  lines of their own, and a grep on those wakes early with the wrong verdict.
- Never chain `design-propose && design-review-packet && launch` on a
  `grep -c` or any step that can print without succeeding: a refused
  proposal left the old one recorded and the reviewer reviewed that. The
  launch waits for `DESIGN_PROPOSAL_RECORDED` and the packet's JSON.
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
- The board moves only when the host records: criteria and the proposal go
  in before the first line of code, or the Pilot watches Orient for the
  whole build.
- Versions are patch only, in every repository: 0.1.0 becomes 0.1.1, not
  0.2.0, whatever the lane added. A minor is Moncy's call.
- Parallel lanes: `advance 3` and `advance 4` the moment the design is
  approved, before the first line of code, or the Pilot watches five boards
  read "Design approved" for half an hour. Every lane touches
  `handsoff.toml [checks]` and the manifest, so each landing rebases with
  the checks list as main's plus its own lines and the manifest regenerated.
- After a fix mid-review, `verify` pulls the run back to Phase 4; `advance
  5` again before the reviewer's result can dispatch, or adopt it after.
- Before the push, run every suite whose files you touched, not the one you
  wrote: a new first line of output (`INSTALL_CHECK_OK`) and a new row in a
  panel broke three neighbouring suites on CI, one round each.
- The last merge comes before the live runs: a tests-only merge that lands
  between `verify` and `verify-live` invalidates every lane's evidence and
  costs the whole live pass again. Order: last merge, re-verify, reaffirm,
  `advance 6`, `advance 7`, `verify-live`, `advance 8`.
- A set of parallel lanes is one release, not one per lane: land them in
  sequence, bump once, install once, run `verify-live` once per lane against
  that one install.
- Several open tickets that will land together are ONE run with N items
  (`init --item "#1" --item "#2" ...`, one `[#N]` tag per criterion), one
  worktree, one pull request, one reviewer pass per phase, one live pass,
  one release. Five separate runs for five tickets on 2026-09-21 cost five
  of everything; a separate run is for a ticket that must land or release
  on its own.

- Regenerate the runtime manifest between IMPLEMENTER launches, not only
  before a reviewer: the previous launch's edits to bin/ or schemas/ make it
  stale, and the next managed launch is refused outright and does no work.
- A managed implementer reports a criterion done having covered its first
  clause only. Read the criterion text against the tests before `verify`, or
  the ledger records something untrue: four criteria came back this way in one
  run, each a round.
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
- A command that prints SHIP_FEATURE_BLOCKED can still exit 0. Never gate a
  check on `cmd >/dev/null && echo OK`: read the output, or compare the
  digests yourself. A stale manifest passed that way and was one push from
  main, where every engine command would have refused.
- Commit the regenerated manifest WITH the bin/ change that made it stale,
  in the same commit. Regenerating after the commit leaves main carrying a
  digest that matches neither the old file nor the new one.
- Size a delta review task to the delta. A re-review of a two-file fix given
  the full-feature task and three test commands burned an 80k budget with no
  verdict; the same review scoped to `git diff A..B` and one command answered
  in 12k. A budget-exhausted reviewer spends an attempt and returns nothing.
- Provider quota changes the vendor, not the role: route to another vendor and
  record `provider_quota`. A per-session token ceiling pauses without fallback.
- A run approaching two active hours is a performance incident, not a reason to
  hide time in a wait state. Warn at 90 minutes; at 120 checkpoint, fence late
  results, pause visibly, and require an explicit reevaluation decision.
