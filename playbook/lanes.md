# Running a Handsoff lane

The host recipe: worktree off `main`; board; criteria; independently approved
design; implementation; verification; review; PR; release; installed-engine
live check; written close. Read this before `init`, `landing.md` before Phase
6, and `lessons.md` once per session.

**One run, N tickets.** Tickets landing together use one `init`, worktree,
PR, and release. Tag each criterion `[#N]`. Split only independent releases.

**Board first.** After `init --by <you>`, serve `dashboard --owned-by-run
--port <free> --no-open` from the engine being changed, confirm HTTP 200,
open it, post the issue's started comment, then apply criteria. The Pilot
watches the dashboard, not the terminal.

**Exact host identity is automatic.** Start the dashboard with
`HANDSOFF_HOST_MODEL=<runtime's exact model>`. Never ask the Pilot or infer a
variant from a family; record unavailable only when the runtime hides it.

**Criteria.**
- Automated test strings must already appear verbatim in `[checks] commands`.
- `verify` always takes `--criterion REQ-00n --by <you>`; bare `verify` fails.
- A remote CI/device criterion is `manual`; evidence is its own run/read-back,
  not a local build or a test that merely reads its configuration.
- A criterion that says "every reader" (every command, every page, every
  path) gets one test per reader, named in the criterion.
- Mark proof that cannot run now PENDING in evidence and the closing comment.
- Use one `[#N]` per criterion; duplicate it or mark the item optional at
  Phase 8 rather than combining tags.

**Design proposal.** Use the six required keys, strings <=512 characters,
arrays <=8 entries, and one `Data shape:` approach item. Validate the JSON;
launch review only after `DESIGN_PROPOSAL_RECORDED`. A third attempt needs
`design-review-authorize --by <pilot>` and a `pilot-note`.

**Reviewer first.** After design approval, `advance 4 40`. Approval issues
the exact reviewer-bound acceptance registry as the Implementer's contract;
never substitute a host paraphrase. Later review judges that same contract.

**Continuous monitoring is a host obligation.** From `init` until `advance
8 100`, the host monitors sessions, shards, CI, merge, release, install, and live
checks without a prompt or shorthand. Keep the dashboard live, report material
progress, recover in-scope failures, and start each eligible step. Pause only
for a new Pilot decision or unresolvable external blocker; state it once.

**Reviewers.** Launch an independent managed reviewer with host session vars
unset, scratch `TMPDIR`, and absolute project paths. Findings are <=400
characters and use supported `CODE: summary` values. Adopt an undispatched
verdict with `session-result-adopt`; recover a killed session after its stall
window.

**Provider quota is Supervisor discretion.** Route the unchanged task to an
explicit equivalent model from another vendor, preserving independence and ledgering
both models plus `provider_quota`. Ask only when no route is allowed. Auth,
runtime, and per-session token-cap failures are not quota; size caps by role,
risk, and packet.

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
