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
