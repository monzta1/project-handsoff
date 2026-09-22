# Running a Handsoff lane

The recipe every host (a person, Claude Code, Codex) follows for one issue
in one repository: worktree off `main`, board first, criteria, a design the
independent reviewer approves, implement, verify, an implementation review,
land through a pull request, live-verify against the installed engine,
close with the written result. Read this file before `init`; read
`landing.md` before `advance 6`; read `lessons.md` once per session.

**One run, N tickets.** Open tickets that will land together are one run:
`init --item "#1" --item "#2" ...`, one criterion per acceptance line with
its `[#N]` tag, one worktree, one pull request, one release. Start a
separate run only for a ticket that must land or release on its own.

**Board first.** "Board" means the Handsoff run dashboard: `init --by
<you>`, then `dashboard --owned-by-run --port <free> --no-open` served from
the engine that carries your change (in an engine checkout, `python3
bin/handsoff_supervisor.py`, never the installed `handsoff`), confirm it
answers 200, open it in the browser, then the issue comment "started", then
criteria. The Pilot watches the dash, not the terminal.

**Criteria.**
- Every automated test string must already be in `handsoff.toml [checks]
  commands`, verbatim, before `criteria-apply`; the transaction is refused
  otherwise.
- `verify` always takes `--criterion REQ-00n --by <you>`; bare `verify` fails.
- A criterion whose deliverable RUNS SOMEWHERE ELSE (a CI workflow, another
  machine, a device) is `manual`, and its evidence is that place's own run:
  the workflow URL and conclusion, the read-back, the times. A local build
  plus a test that reads the workflow file is not evidence.
- A criterion that says "every reader" (every command, every page, every
  path) gets one test per reader, named in the criterion.
- Proof that cannot run now is recorded as PENDING in the evidence text and
  stated in the closing comment; never implied.
- The registry reads one `[#N]` tag per criterion: a criterion tagged
  `[#183] [#184]` scopes #183 only; give each issue its own criterion or mark
  the other item optional at Phase 8.

**Design proposal.** Six keys (summary, approach, tradeoffs, decisions,
verification, constraints); every string at most 512 characters, at most
eight items per array; one approach item begins "Data shape:". Write the
JSON with a script that checks the lengths, and only then `design-propose`;
never launch a reviewer until DESIGN_PROPOSAL_RECORDED has printed for the
revision you want reviewed. Two autonomous review attempts; a third needs
`design-review-authorize --by <pilot>` plus a `pilot-note`.

**While you implement.** `advance 4 40` as soon as the design is approved
and before building, so the phase strip says implementation during the work
instead of a wait (the `heartbeat` command is bound to a managed session).

**Reviewers.** A managed session (`handsoff agent --root <abs> launch
reviewer --by <reviewer actor> --task "..."`) whose identity differs from
yours, launched with the host session variables unset
(`CLAUDE_CODE_SESSION_ID`, `CODEX_COMPANION_SESSION_ID`, `CLAUDECODE`,
`CLAUDE_CODE_ENTRYPOINT`) and `TMPDIR` set to a scratch directory. Its cwd
is empty: absolute paths in the task. Findings are at most 400 characters;
`record-review-findings` wants `CODE: summary` with a supported code. A
verdict that fails to dispatch is recovered with `session-result-adopt
--session hs-...`; a reviewer you killed stays "live" to the watchdog for
`stall_minutes`, then `recover --by <you>` relaunches it.

**Provider quota is Supervisor discretion.** Route the same task to an explicit
equivalent model from another vendor, preserving constraints and independence;
ledger both models and `provider_quota`. Ask the Pilot only if no route is
allowed. A per-session token ceiling pauses without fallback.

**Tree changes after verify are evidence drift.** Re-run every `verify`,
then `record-review --by <reviewer> --reaffirm --tests-executed yes
--symptom-reproduced not_applicable`. This includes a rebase onto a moved
`main` and a documented line count.

**Before pushing.** Run the whole node suite and every python module your
files cite; a red job on the PR is a wasted CI round. Regenerate the
runtime manifest after the last edit to bin/, prompts/, schemas/ or
dashboard/ (the engine refuses until you do, and names the command).

**Parallel lanes.** One worktree per lane off `main`. Run two lanes at once
only when they touch different files; the second to land rebases (keep both
sides, then `node --check` and `python3 -c "import ast"` on every merged
file), regenerates the manifest, re-verifies, reaffirms. Landing is serial
by construction: one installed engine, one live verification at a time.

**One criterion per Implementer launch (#215).** When the registry has
more than three automated criteria, brief the managed Implementer one
criterion (or one small group) per launch. A 120,000-token budget ran
out twice on 2026-09-19, at seven and at three criteria, and each time the
tree held a mostly usable diff with no account of what was done. Since
v0.3.70 the Implementer prints `HANDSOFF_PROGRESS` per criterion and a
relaunch receives the done list, but a launch that fits its budget never
needs the account.
