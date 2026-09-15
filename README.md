# Project Handsoff

Project Handsoff is a portable, domain-neutral delivery gate for a four-role workflow:

```text
Architect -> design review -> human approval -> Supervisor -> Implementer -> Reviewer -> repair loop -> verified result
```

The Supervisor owns state, phase gates, acceptance evidence, retries, and user escalation. The Implementer changes the target project. The Reviewer independently checks the brief, diff, tests, and original symptom. Handsoff makes their claims auditable; it deliberately does not treat a free-text status update as proof.

MIT licensed, see [LICENSE](LICENSE).

## Quick start

Copy `handsoff.toml`, `schemas/`, `prompts/`, `dashboard/`, and everything in `bin/` into a target project. Configure `[checks].commands` and `[checks].live_commands`, then initialize from the target project's root:

```bash
python3 bin/handsoff_supervisor.py init "Fix the thing that is broken"
python3 bin/handsoff_supervisor.py criterion-update REQ-001 --requirement "Exact observable outcome" --verification automated --test "pytest tests/test_fix.py -q"
```

`init` scaffolds `handsoff-status.json` and `handsoff-acceptance.json` at the project root, and flags the run `requires_design_approval: true`. Use `criterion-update`, `criterion-add`, and `criterion-remove` instead of editing the registry by hand. A batch of changes goes through `criteria-apply --file TX.json --by ACTOR` as one all-or-nothing commit (see "Criteria transactions"). Every command resolves the project root itself: `--root DIR`, then `$HANDSOFF_ROOT`, then the nearest ancestor with `handsoff.toml`, then the current directory.

The Architect (see "Agent roles") collaborates with the human to turn that placeholder criterion into a real design and testable criteria. In Phase 2 an independent reviewer critiques that design first; the Supervisor records either approval or actionable revision findings:

The Architect scales that collaboration to the request. Small, clear, low-risk work gets a concise scope, approach, and criteria proposal; large, ambiguous, high-risk, or cross-cutting work gets fuller exploration and tradeoff analysis. It states which path it recommends and why. The human can say `go deeper` to expand the design or `that's enough, proceed` to stop exploration and submit the smallest sufficient proposal for independent review. That instruction is not itself design approval; both paths retain the same independent-review and human-approval gates.

```bash
python3 bin/handsoff_supervisor.py advance 2 20 --new-design-round
python3 bin/handsoff_supervisor.py record-design-review --by design-reviewer-1 --architect architect-1 --approve --summary "Design and criteria are implementation-ready"
```

Every `record-design-review` (approve or request-changes) counts as one design-review attempt. After `[workflow] max_autonomous_design_reviews` attempts (default 2) the run stops asking for reviews on its own; the Pilot permits exactly one more at a time:

```bash
python3 bin/handsoff_supervisor.py design-review-authorize --by moncy --note "one more pass on the failure-mode criterion"
```

Only after that independent review does the human give explicit approval before anything is built:

```bash
python3 bin/handsoff_supervisor.py design-approve --by moncy --architect architect-1 --summary "Approach, tradeoffs, decisions"
```

Phase 3 ("Design approved") and every phase after it refuse to advance for a newly initialized run until two current, hash-bound decisions exist: an approved independent design review and explicit human design approval. `record-design-review` accepts `--approve` or `--request-changes`; it refuses a reviewer matching the Architect (case- and whitespace-insensitively), and a change request keeps work in Phase 2. `design-approve` likewise refuses Architect self-approval. `design_hash` deliberately excludes each criterion's mutable `state`/`evidence` (unlike `acceptance_hash`, which `review`/`deployment_approved` use and which DOES include them): recording evidence never invalidates either design decision, while adding, removing, or respecifying a criterion invalidates both and rolls the run back to Phase 2. Each requirement is feature-flagged in status, so a run created before that gate existed remains unaffected after upgrading.

The Architect treats existing design docs, settled decisions, and shipped features as settled context to design new work around, not as targets it may reopen on its own judgment (see `prompts/architect.md`); it proposes changing settled/shipped work only when the human explicitly asks for that redesign. `design-approve --redesigns-settled-work "what's changing and why"` is how that explicit exception gets recorded: omitted (the default), an approval makes no claim about touching settled work; passed, it visibly and auditably marks this specific, human-approved design as one that intentionally redesigns something previously settled, rather than fitting around it.

A successful `design-approve` also stamps `authored_by` = the `--architect` identity onto every criterion in the registry at that moment, unless a criterion already carries one from an earlier approval (existing authorship is never reassigned by a later one; a criterion added after approval starts unstamped until the next `design-approve` stamps it). `--summary` itself lands on `status.design_approved.summary`, not only the hash-chained event message, so the design doc is directly readable from `handsoff-status.json`/the dashboard. Together with `design_approved.by` (the human approver), `implemented_by`, and `reviewed_by`, a run's full author/approver/implementer/reviewer chain -- four identities, each expected to differ from the others where the tool enforces it -- is retrievable straight from `handsoff-status.json`/`handsoff-acceptance.json`, no event-log parsing required. `authored_by` is excluded from `criterion_spec_hash` (and therefore `design_hash`) exactly the way `state`/`evidence` already are: it is provenance about who proposed a criterion, not part of the claim being verified, so stamping it can never invalidate an already-recorded evidence binding or trip the design gate on the very approval that just wrote it.

Launch Mission Control from the target project:

```bash
python3 bin/handsoff_supervisor.py dashboard
```

It opens `http://127.0.0.1:8765`, uses a localhost event stream for near-real-time refresh (with focus and polling fallbacks), and shows phase progress, acceptance coverage, audit integrity, evidence, activity, assigned roles, release readiness, and an E.V.E.-style tactical Supervisor briefing. When the Supervisor records a user-dependent pause as `blocked`, or Phase 7 is waiting for deployment approval, a sticky red alert appears, the browser-tab title flashes, and optional desktop notifications identify the exact action required. After an independent design review passes, the alert exposes **Authorize Design**; that button invokes the same hash-bound, non-self-approval gate as `design-approve` and records the actor as `Mission Control Pilot`.

The **Agent Settings** dialog selects `auto`, `codex`, or `claude` plus a runner-default or exact model independently for Supervisor, Architect, Implementer, and Reviewer. A role left unset in `handsoff.toml` launches with the recommended crew (see "Default crew" below) and is labelled `recommended default` in the dialog; an explicit `auto` chooses, at each new role launch, the first installed runnable adapter in the documented order Codex, then Claude Code. Mission Control shows the effective choice either way; selecting Codex or Claude explicitly overrides it per role, and saving the dialog writes every role explicitly. Each role can also have up to eight ordered, explicit Codex/Claude fallback profiles under `[fallback_policy]`; `max_failovers_per_role` defaults to 2 and caps replacement selections without counting the primary. Saving the wrapped settings payload atomically updates `[agents]`, `[models]`, and `[fallback_policy]`. Existing four-role profile and older three-role adapter-only payloads remain valid and leave fallback policy unchanged; the legacy form also preserves the Supervisor adapter/model semantics. Availability means only that the executable was found locally; it does not prove authentication, account entitlement, network access, or model validity. Settings saved during a live session affect future selections only. The pure fallback planner accepts already-classified runtime failures and injected availability, skips unavailable/attempted/non-independent profiles with closed reason codes, and never launches or mutates workflow state. The settings and approval endpoints accept only bounded, exact JSON payloads from the dashboard's own loopback origin and do not enable CORS. Add `--no-open` to start the server without opening a browser, or `--port PORT` to choose another local port. Add `--owned-by-run` when the dashboard is launched for one `/ship-feature` run (`dashboard --no-open --owned-by-run`): the server then writes `.handsoff-dashboard-owner.json` (gitignored) in the project root and `advance 8` with status `complete` shuts it down after the run archive is written, so the port is free for the next run. The persistent LaunchAgent and a manual launch never pass the flag, never write the file, and can never be targeted by a release: the server answers `GET /api/ownership` from the run token and root hash it minted in memory at launch, and `POST /api/shutdown` needs both of them. Note for local development: the dashboard server resolves `dashboard/` and `bin/` relative to the `handsoff_supervisor.py` you actually invoked, not `--root` -- to see edits to the dashboard's own files (`dashboard/*`, `bin/handsoff_dashboard.py`) reflected live, launch it from the copy of this repo you're editing, not a separately installed one.

**Live status.** A strip under the header shows what the run is doing right now: a state pill (`IDLE`, `STARTING`, `RUNNING`, `WAITING`, `STALLED`, `STOPPED`, `FAILED`, `COMPLETE`) that pulses while a managed role is active, the role, "last activity N s ago" ticking every second between snapshots, and a one-line detail (for a finished session the state, exit code, and end time). It is fed by `snapshot.live`, the output of `handsoff_lib.live_status`, which folds the run status, an open human pause, the Phase 7 approval wait, the ledger-bound session record, and the liveness beacon `.handsoff-live.json` (gitignored; written every 5 s by `handsoff_agent.py` while a managed child runs, seven keys only: `session_id`, `role`, `state`, `pid`, `beacon_at`, `ended_at`, `exit_code`) into one view. `status` prints the same view as `live`. The beacon is a hint, never an authority: it is accepted only when its `session_id` is the current session's, a running session without a fresh (15 s) matching beacon reads `STALLED`, and a terminal session reads `STOPPED` or `FAILED` from the session record whatever the beacon says. The event feed stays the sanitized stream; no beacon event is ever logged.

Mission Control's DOM-free display/formatting logic (current-run and next-launch labels, Auto-detect resolution, per-role fallback list manipulation) lives in `dashboard/lib/dashboard-logic.js`, loaded as a plain global before `app.js` and also `require()`-able from Node, so it has its own fast test suite independent of a browser:

```bash
node --test tests/dashboard/*.test.js
```

Run it after changing anything under `dashboard/`, alongside `tests/test_handsoff_supervisor.py`.

Launch a configured role in a fresh session with the active project as its working directory:

```bash
python3 bin/handsoff_agent.py inspect architect --task "Design the requested feature"
python3 bin/handsoff_agent.py launch implementer --task "Implement the approved criteria" --by implementer-1
```

`handsoff_agent.py` resolves the executable to an absolute path and never uses a shell. The role prompt and task travel on standard input, not in command arguments. Codex uses an ephemeral `codex exec` session; Claude Code uses headless `claude -p`. `default` omits the model flag. Reviewer and Supervisor launches enforce read-only/plan mode at the runner boundary; Architect and Implementer use explicit workspace-write/accept-edits modes. No bypass-permission flags are generated. Runner output streams directly rather than accumulating in memory; nonzero exits, cancellation, timeouts, missing prompts, and missing executables fail visibly, with timeout/cancellation terminating the fresh process group.

Brokered role launches recover only from host-classified failures tied to the exact terminal session. Under the project lock, HandsOff rechecks audit integrity and the current-session pointer, derives a bounded secret-free git/criteria/evidence handoff, reserves a fresh same-role fallback session, and atomically claims it before starting only that exact profile; claim replay fails before process creation. TERM-to-KILL ownership stays with the live launcher; the status never stores process IDs, prompts, output, environment, credentials, or token values. Cancellation, unknown/nonrecoverable failures, live-session replacement requests, an exhausted cap, and quality claims outside the configured review boundary record a Pilot pause without changing workflow gates. A Supervisor quality request contains only the exact completed `session_id` and one closed `finding_code`; callers cannot choose the failure category, reason, model, or handoff. Quality eligibility uses the authenticated monotonic `review_round` and `max_review_rounds`, and duplicate findings for the same session and review round are rejected. Each Reviewer session also binds its immutable Implementer identity for every later fallback in that Reviewer chain.

Each managed launch records a bounded, host-generated session ID and an immutable snapshot of its role, actor, resolved adapter, requested model, resolution source, and lifecycle. `--by` is optional; when omitted the documented identity is `<resolved-adapter>-<role>`. The launch snapshot is committed under the project lock before the process starts, and a second live session for the same role is refused. Telemetry writes fail closed if the event or verification ledger no longer authenticates the current status; they never re-anchor an unlogged edit. Once a child reaches `running`, completion and every handled error path record exactly one terminal lifecycle transition, including early stream closure. Mission Control uses this telemetry for **THIS RUN** while Agent Settings remains **NEXT LAUNCH**. A requested model is not presented as the model actually used: `reported_model` stays null unless a future trusted runner protocol supplies it, and ordinary runner output is never parsed for that purpose. Prompts, task text, stdout/stderr, environment values, credentials, API keys, and token values are never stored in runtime telemetry.

Supervisor mutations must cross the typed host dispatcher in `bin/handsoff_broker.py`. The host process that launches the read-only Supervisor captures `HANDSOFF_BROKER_REQUEST:` protocol lines from that exact child and dispatches them in-process; there is no caller-supplied trust flag or standalone broker entry point. The actual authority boundary is the OS-enforced read-only/plan sandbox inherited by the Supervisor child and anything it starts. The module's in-process token prevents accidental cross-role dispatch inside the owning host; it is not authentication against arbitrary local Python code, which already runs with the invoking user's filesystem authority. The broker accepts typed JSON tied to the exact active project root, builds only allowlisted `handsoff_supervisor.py` arguments, and always uses `shell=False` in a fresh process group. It may launch Architect, Implementer, or Reviewer but never another Supervisor. It rejects unknown fields/actions, arbitrary commands, path/root substitution, direct product-file mutation, and the human-only `design-approve` and `deployment-gate` commands. Timeout or cancellation terminates the entire brokered process group.

The evidence-bearing part of a normal run looks like this:

```bash
python3 bin/handsoff_supervisor.py verify --criterion REQ-001 --by implementer-1
python3 bin/handsoff_supervisor.py record-symptom-resolved --evidence vr-... --by implementer-1
python3 bin/handsoff_supervisor.py record-review --by reviewer-1
python3 bin/handsoff_supervisor.py deployment-gate --approve --by release-owner
python3 bin/handsoff_supervisor.py verify-live --by production-monitor
```

`verify` and `verify-live` return the exact `vr-...` run id. Phase advances still happen one at a time with `advance PHASE PROGRESS`. The framework is integration-neutral: it does not assume a language, tracker, hosting platform, or repository provider.

A run doing legitimate long background work (a multi-hour design review, a slow check, waiting on an async agent) that has no progress to report yet should call `heartbeat --by implementer-1 [--note "what's running"]` periodically. It records a pure liveness timestamp (`last_heartbeat_at`) and an audited `heartbeat` event, without touching `phase_number`, `progress`, or `updated_at` -- those stay reserved for calls that actually advance the work. `status` and the dashboard read the freshest of `updated_at` and `last_heartbeat_at` when deciding whether a run has stalled, so a busy-but-alive run backed by a recent heartbeat never reads as stalled, and a run with neither signal current still does. See "Known limitations" for what this does and does not cover.

Each criterion declares one verification policy: `automated`, `manual`, `browser`, or `automated_and_browser`. An automated criterion's `tests` must exactly name commands in `[checks].commands`, and `verify` runs and judges only that criterion's own tests: an unrelated command elsewhere in `[checks].commands` can neither fail it nor satisfy it, and naming several criteria in one call still gives each its own independent evidence record. `[checks].commands` also decides run order: `verify` runs the union of needed commands in that configured order, not alphabetically. The combined policy requires both automated and browser evidence, and a criterion only reads as `passing` once every kind its policy requires has a valid record; one kind landing first leaves it `not_tested`, not a premature `passing`. Command output is shown to the caller but not persisted, only its SHA-256 digest and execution metadata enter the ledger, avoiding accidental secret retention. Each command has a configurable timeout (`[checks].timeout_seconds`, default 600s); a hanging check is killed and recorded as exit 124 instead of hanging the Supervisor.

If an interrupted write ever leaves the ledgers out of sync with `handsoff-status.json` (see Known limitations), run `handsoff_supervisor.py doctor` (add `--dry-run` to preview). It recovers only the two specific, provably safe gaps a crash between writes can leave, and refuses, with a specific reason, on anything that looks like real tampering or an invalid resulting state.

## Eight phases

1. Orient
2. Design debate
3. Design approved
4. Implementation
5. Independent review
6. Checks and documentation
7. Awaiting deployment approval
8. Live verified

## The run archive

Landing Phase 8 with status `complete` automatically writes one
self-contained JSON record of the whole run (status, acceptance criteria,
verification ledger, event log) to `~/Documents/Handsoff-Archive/` (override
with `HANDSOFF_ARCHIVE_DIR`), named `<repo>-<timestamp>-<feature-slug>.json`.
This is centralized outside any project repo on purpose, so a repo's own
archive/cleanup of its `handsoff-*.json` files never loses the history, and
so patterns across every project Handsoff has ever run in can be mined later
to improve Handsoff itself (which findings reviewers catch most often, how
many design/review rounds a typical run takes, and so on). It never blocks
or fails a run: an archive write failure prints a warning and the run still
reports success, since the phase transition it is recording already
committed by the time archiving happens.

## What is actually enforced

Every claim below is backed by a test in `tests/test_handsoff_supervisor.py`. Run it after copying the framework in, and again after changing `handsoff_lib.py`, to confirm the guarantees still hold:

```bash
python3 tests/test_handsoff_supervisor.py -v
```

- **The gate checks proposed state.** `advance` validates the state it is about to write. Progress cannot decrease, is bounded from 0–100, and the phase label must match the phase number.
- **Passing means evidenced.** Phase 6+, 95%+ progress, and deployable/complete statuses require every criterion to reference a successful record in the hash-chained verification ledger. Each record is bound to the criterion specification, so changing the requirement invalidates its earlier evidence.
- **Coverage is derived.** Passing/failing/not-tested/blocked totals must exactly match the acceptance registry. They cannot drift into a second, contradictory source of truth.
- **No self-approval.** Phase 6+ requires an independent review record bound to the current acceptance hash, a complete reviewer checklist, an implementer identity, and a different reviewer identity.
- **A design approval also stamps who proposed it, safely.** `design-approve` writes `authored_by` = `--architect` onto every criterion present at that moment (skipping any that already carry a non-null `authored_by` from an earlier approval) through the same `commit()` call that persists the rest of the approval, so the stamp survives a fresh reload from disk, not just an in-memory computation. `criterion_spec_hash`'s exclusion set is `{state, evidence, authored_by}`: excluding `authored_by` means stamping it can never change a criterion's spec hash, never invalidate a verification record already bound to that hash, and never make `design_hash` (built from the same per-criterion hash) mismatch on a gate check run immediately after the very approval that just wrote it.
- **The Architect cannot self-approve its own design into execution.** For any run `init` flagged `requires_design_approval` (every new run, going forward), Phase 3+ requires a `design_approved` record bound to a `design_hash` of the current criteria, naming a human approver distinct from the architect identity that proposed the design; `design-approve` refuses `--by == --architect` (compared case- and whitespace-insensitively) outright, and the gate re-checks the same identity mismatch independently every time it runs, the same double enforcement `_review_errors` already applies to `reviewer != implemented_by`. It also refuses while the registry still holds only `init`'s untouched placeholder criterion. A run from before this feature (no `requires_design_approval` field) is never subject to this gate.
- **The design itself is independently reviewed (AR7).** Every new run is also flagged `requires_design_review`. Phase 3+ requires `record-design-review --approve` from an identity distinct from the Architect, bound to the current `design_hash` and governance configuration. `--request-changes` records the finding, clears premature human approval, and keeps the run in Phase 2. Any criteria mutation clears both design decisions; the phase gate independently rechecks the record, hashes, decision, and identity so a hand-crafted bypass is refused. Pre-AR7 runs without the flag remain unaffected.
- **A criteria-registry mutation invalidates a stale design approval, never just leaves it stale on disk.** `criterion-add`/`update`/`remove` clear `design_approved` and, for a flagged run at Phase 3+, roll it back to Phase 2 -- landing at Phase 3+ with no valid approval would otherwise be a self-contradictory state no other rollback in this tool produces. Recording evidence (`verify`, `record-evidence`, `record-symptom-resolved`) does not touch `design_approved`: it changes whether a criterion is proven, not what is being asked for.
- **A batch of criteria changes lands as one transaction, or not at all (#44).** `criteria-apply --file TX.json --by ACTOR` reads 1 to 64 `add`/`update`/`remove` operations (exact key sets, each id at most once, file at most 256 KiB) and `lib.plan_criteria_transaction` applies them in order to a deep copy of the registry with the same field validator the single commands use (`lib.validate_criterion_fields`, shared by `criterion-add`/`update`/`remove`). It refuses an unknown id on update or remove, a duplicate id on add, an automated `tests` entry (a criterion whose policy requires `checks`) that is not one of `[checks].commands` or that the #28 footprint gate rejects exactly as `run_checks` would (shell control operators, or a command that captures a `[[regressions]]` group; manual and browser entries are attestation descriptions and are not gated, as with the single commands), a result with zero or more than one `primary_fix`, and a resulting registry that fails the acceptance schema. A refusal prints `SHIP_FEATURE_BLOCKED: operation N (<op> <id>): <reason>` (N counted from 1 in file order) and writes nothing: status, acceptance, and both ledgers are byte-identical afterwards. An accepted plan is written by ONE `commit()` under the project lock carrying status, acceptance, and one `criteria_transaction_applied` event (`operations` as `{op, id, previous_hash, resulting_hash}` per change, `registry_hash_before`/`after`, `design_hash_after`, `operation_count`; never requirement text or test commands), and the acceptance write is the existing atomic replace, so a concurrent reader sees the registry before or after, never in between. Decisions are invalidated exactly once with the same semantics as one `criterion-update`: `design_approved`, `design_review`, `review`, `deployment_approved`, and `live_verification_id` are cleared and a flagged run at Phase 3+ rolls back to Phase 2 in that same status write, with no `phase_advanced` event. `--dry-run` runs the identical validation and hash calculation, prints the plan as JSON with `would_invalidate: {design, review, deployment, live}`, and writes nothing. A launched regression locks the command out like every other workflow mutation. The broker allows `criteria-apply` (`file`, `by`, optional boolean `dry_run`) for the Supervisor.
- **A small post-approval correction goes through a scoped amendment, and only the planner decides that it is small (#42).** After Phase 3 with a valid design approval, `amendment-open --file TX.json --by ARCHITECT [--summary TEXT] [--request-full-redesign]` plans the same transaction file `criteria-apply` takes and `lib.classify_amendment` classifies it: `full_redesign` when any operation is an `add` (added scope), an operation changes which criterion is the `primary_fix`, a verification policy is downgraded (`automated_and_browser` to anything else, `automated` to `manual`/`browser`, or an automated criterion loses tests without replacement), the changed criteria span more than one derived work item, the work-item scope would change, or `--request-full-redesign` was passed; otherwise `scoped`. A `full_redesign` delta prints `AMENDMENT_REFUSED: full_redesign` with the reason list and writes nothing; the ordinary path (`criteria-apply`, `criterion-update`) with its design invalidation and Phase 2 rollback is the only way to land it. A `scoped` delta is applied in one `commit()` with one `amendment_opened` event: every changed criterion is reset to `not_tested` with `evidence` cleared, every other criterion is byte-identical, review/deployment/live decisions are invalidated exactly as evidence recording does, and `design_approved`/`design_review` stay on file on their base hash (the design gates accept the base hash only while the recorded amendment's `resulting_design_hash` equals the registry). While the amendment is open the run is frozen: `advance` to any other phase or progress (forward or back), `record-review`, `deployment-gate`, `verify-live`, and every criterion mutation (`criterion-*`, `criteria-apply`) are refused with an `amendment gate:` message and nothing written; `verify` and `record-evidence` stay allowed so the correction can be evidenced. `amendment-review --by REVIEWER --approve|--request-changes --summary TEXT` (reviewer distinct from the amendment's author and the architect on record) binds to `amendment_hash = sha256(base_design_hash + canonical operations)` recomputed from the registry on disk; `amendment-approve --by PILOT` (human-only, the broker refuses it) requires an approved review on that exact recomputed hash, rewrites `design_approved.design_hash`/`design_review.design_hash` (and `scope_hash`) to the amended design with the amendment id appended to each record's `amended_by`, closes the amendment into `amendments` (last 16), and resumes the frozen phase and progress with no `phase_advanced` event. `amendment-revise --file TX.json --by ARCHITECT` applies a follow-up under the same rules over the cumulative delta (an expansion to `full_redesign` refuses and points at escalation), recomputes the hash, and clears the stale review; `amendment-escalate --by ACTOR --reason TEXT` closes it as `escalated` and takes the full path (design decisions cleared, flagged run back to Phase 2). Restart, rollback, a stale hash at approve time, reviewer equal to the architect, approval before review, and a concurrent mutation are all refused without writing; a malformed `amendment` record refuses the whole state. Mission Control renders `snapshot.amendment` (changed and dependent ids, affected work items, retained evidence count, required decisions, classification and reasons) and its input-required banner names the pending decision.
- **Deployment approval is load-bearing, not a side command you can skip.** `advance` to Phase 8 checks for a recorded approval; `deployment-gate --approve` is what records it, and refuses before Phase 7 so approval cannot be granted before an Implementer or Reviewer has touched anything. The approval is bound to a hash of the acceptance criteria at the moment it was given; if the registry changes afterward, the Phase 8 gate recomputes the hash and refuses the now-stale approval.
- **Live verified means live checked.** With `require_live_verification = true`, Phase 8 requires `[checks].live_commands` to pass after deployment approval and against the unchanged acceptance registry. `verify-live` re-reads `handsoff.toml` from disk after the checks finish, not the config it captured before starting them, so a policy change landing in that window (a slow live suite is exactly when this matters) is actually detected rather than compared against itself.
- **Mutations are serialized.** `advance`, criterion mutations, evidence/review recording, deployment approval, and verification use the project lock. `verify` rechecks criterion hashes after long-running commands before attaching their results.
- **A targeted check runs once per bound state, and only a passing executed record is ever reused (#43).** `verify` binds every needed command to `verification_binding = sha256(command, repository digest, verification_config_hash, sorted spec hashes of the criteria verified with it)`. The repository digest hashes every tracked and untracked-not-ignored file's content (dirty state included; a plain directory hashes every file minus Handsoff's own generated names), and `verification_config_hash` covers the governance keys plus `[checks].commands`, `[checks].timeout_seconds`, and the `[[regressions]]` groups. A record is reusable only when it is `kind == "checks"`, `ok`, `executed`, from this run (`feature_hash`), on the same binding, and every result in it has exit 0, `timed_out false`, `truncated false`; a failed, timed-out (124), or truncated result disqualifies the whole record, a reused record is never itself a source, and `verify-live`, manual/browser attestation, and `regression-run` never consult the cache. Concurrent verifies of one binding serialize on `.handsoff-verify-inflight/<binding>.lock` so the second re-reads the ledger and reuses. `--no-cache` forces a launch.
- **A malformed hand-edit refuses cleanly, it does not crash.** `progress`, `design_round`, and `review_round` are type-checked before any gate logic casts them, including rejecting `NaN`/`Infinity` (valid JSON-extension floats that pass a plain `isinstance(x, float)` check and then crash `int()`/`float()` arithmetic). A bad value in a hand-edited status.json is reported as a validation error, not a raw Python traceback, in both `handsoff_supervisor.py` and `validate_handsoff_status.py`. Both scripts also catch any unexpected exception as a last resort, for the same reason.
- **The acceptance hash behind a deployment approval ignores harmless reordering.** It sorts criteria by id first, so re-saving or merging `handsoff-acceptance.json` without changing any criterion's content does not falsely invalidate a still-valid approval.
- **Round and stall limits are enforced, not decorative.** `design_round` and `review_round` past `handsoff.toml`'s `max_design_rounds`/`max_review_rounds` block further advancement. A run with no update past `stall_minutes` surfaces a `stall_warning` in `status` for escalation; this is advisory, not a hard block, so a stalled run can still be inspected and unstuck.
- **A declared human pause is not a stall.** `human-pause-start --by <id> [--note "what is being waited on"]` persists a `human_pause` record (`by`, `since`, `note`) on status, and while it is open `status` and the dashboard report `stall_warning: null` and an `activity_note` of `waiting on <by> since <n> min ago: <note>` (no suffix when no note was given) no matter how stale `updated_at` gets, because a pause is a durable declared state, not a heartbeat that expires after `stall_minutes`. `human-pause-end` clears the record without touching `updated_at`, so the same stale state warns again the moment nobody is being waited on. A malformed `human_pause` (missing `by`, naive timestamp, extra keys) is refused as a schema error, never read as an open pause.
- **Design-review attempts are budgeted, and the Pilot spends the budget one attempt at a time (#35).** `record-design-review` increments a cumulative `design_review_attempts` on status (approve and request-changes alike; the event carries `design_review_attempt`, `design_review_limit`, and `authorized`), independent of `design_round`, which only moves when someone passes `--new-design-round` and was how one real run burned five reviews at `design_round: 0`. The number never decrements: criterion mutations, `advance --design-round N`, rollbacks, and process restarts preserve it, and a hand-edited decrement is refused at the next command because every event binds the status file hash. Once `attempts >= [workflow] max_autonomous_design_reviews` (default 2, governance-bound like the round caps) a further `record-design-review` and a managed Phase-2 reviewer launch are both refused with a message naming `design-review-authorize`; a change request that reaches the limit leaves the run `blocked` with that exact command in `next_action` (event `design_review_budget_exhausted`), and Phase 3 stays closed, since reaching the budget never approves a design. `design-review-authorize --by PILOT [--note]` (human-only, the broker refuses it; refused before exhaustion or while an earlier authorization is unconsumed) records `design_review_authorization` permitting exactly one more attempt, managed or human-recorded: `lib.create_agent_session` reserves it under the project lock by writing `launch_session_id` in the same commit that records the launch (so of two concurrent launches exactly one reserves, the other is refused with no session, event, or status change; `build_launch_spec`'s early refusal is advisory only), a runtime-failure replacement carries the same reservation forward, and the next `record-design-review` sets `consumed_at`, after which the next record and the next launch are refused again. Mission Control renders `Design review N/M` from `policy.design_review_attempts` and `policy.max_autonomous_design_reviews`, never from prose.
- **A role left unset launches with the recommended crew, and says so (#39).** With no `[agents].<role>`/`[models].<role>` key (or the legacy `configure-me` placeholder) a role resolves to `handsoff_lib.RECOMMENDED_CREW` (Architect, Supervisor, Implementer: `claude`/`claude-opus-5`; Reviewer: `codex`/`default`); an explicit value for one role never changes the other three, and an explicit `auto` keeps the older auto-detect path. Every report of the profile (`status` `crew`, the dashboard settings view, audited review profiles, session telemetry via `resolution_source: "recommended"`) carries the provenance, `available` in `crew_view` means executable discovery only, and a launch whose adapter is not on `PATH` is refused before a session exists with a message naming the role and the remedies. See "Default crew" below.
- **A role's question reaches the Pilot without Supervisor relay (#46).** A managed Architect, Implementer, Reviewer, or Supervisor prints one line `HANDSOFF_QUESTION: <text>`; `handsoff_agent.py` records it the moment it is read (bounded text, role, session id, timestamps; never prompt, output, or environment content) as `status.pending_questions[]` with a `question_raised` event. A question from the role's CURRENT live session sets `status` to `blocked` with the question as `next_action`, so the existing input-required alert, tab flash, and notification fire; a question from a completed, replaced, or unknown session is recorded and shown but never holds the run. `question-answer --id --by --text` (human-only in the broker; also the Answer control in Mission Control's Questions panel, recorded as `Mission Control Pilot`) audits the answer and lifts the block once no blocking question remains, restoring the previous `next_action`; the answer is handed to that role exactly once, at its next launch, under "Pilot answers to your earlier questions", with a `question_answers_delivered` event. `question-raise` exists for a Supervisor raising a question on a role's behalf. Tests: `TestRoleQuestions`, `tests/dashboard/questions.test.js`.
- **Structured questions with options, answered as one form per role (#48).** After `HANDSOFF_QUESTION:` a candidate that begins with `{` is parsed as a JSON form: `{"text": "Which path?", "options": ["Concise", "Full"], "recommended": "Concise"}` (text 1 to 1024 characters, 1 to 6 unique options of up to 120 characters, optional `recommended` equal to one option). Any other candidate is kept verbatim as plain text with `form_error` naming the rule it broke (`malformed_json`, `not_object`, `unknown_keys`, `missing_keys`, `text_bounds`, `options_bounds`, `duplicate_options`, `recommended_not_offered`), so nothing a role asked is lost; text without a leading brace is a #46 plain-text question. Records carry `options`, `recommended`, `form_error`, `chosen_option` and `other_text` (older records without them still validate). `question-answer --batch FILE` and `POST /api/question-answers` take `{"answers": [{"question_id", "choice"} | {"question_id", "other"}]}` (1 to 16 entries, no duplicate ids; plain-text questions accept only `other`); any bad entry refuses the whole batch by index and writes nothing, and success is one commit with one `question_answers_recorded` event, lifting the block exactly once. `question_answered` events keep `answer` and gain `chosen_option` and `other_text`. Mission Control renders all open questions of a role as one numbered form (radio row per question, recommended preselected, an Other radio revealing a one-line field, answered rows collapsed to one muted line, one Send answers button per role) and the input-required banner shows one card per role naming the count. The role prompts ask for at most three questions per turn, each with options and a recommended answer. Tests: `TestStructuredQuestions`, `tests/dashboard/questions.test.js`.
- **Completed runs are mined for evidenced patterns, and the analyzer can never loosen a gate (#49).** Landing Phase 8 complete scans the run archive right after the archive write and files improvement tickets through `gh` only for the fixed rules R1 to R7, each with run ids and numbers read from ledger records alone; R8 and R9 (the two patterns whose natural remedy is raising a cap) are report entries only, with no configuration to change that; fixture and `run_kind: test` archives are never mined; a scan failure prints `HANDSOFF_ANALYSIS_FAILED` and never fails the advance. See "Archive analysis".
- **A run-owned dashboard is released on completion, and nothing else ever is (#40).** `dashboard --owned-by-run` mints a random `run_token` and the sha256 of the resolved project root, keeps both in memory, and writes them with the pid and port to `.handsoff-dashboard-owner.json`; a server started without the flag writes nothing and reports `owned: false`. When `advance 8` lands `complete`, `release_run_dashboard` reads only the port and token from that file, computes the root hash from its own resolved root, and asks `GET /api/ownership`; it sends `POST /api/shutdown` (both values, re-checked by the server against its in-memory copies, 403 on any mismatch) only when the answer is owned with the same token and the same root hash, then waits until the port refuses connections and removes the file. A refused connection, an unowned server, a foreign token, another root, or a malformed answer removes the stale file and touches no process; no PID is ever signalled. Open SSE clients close because every event loop checks the server's stop flag, and the server's own exit removes the owner file only while it still carries that server's token. The event log records `dashboard_released` (port, pid, reason) or `dashboard_release_skipped` (reason); a repeated completion is a no-op with the skipped reason, and neither outcome can fail the already-committed advance.
- **Design evidence is measured once and reused only while its inputs are provably unchanged (#38).** Each `[[design_evidence]]` table in `handsoff.toml` names a trusted measurement command and the globs it depends on. `design-evidence run --by ACTOR` executes a command only when no record exists, when the cache identity (`sha256(command + "\n" + canonical JSON of the declared inputs list)`) or the input hash (sha256 over the sorted `(path, file sha256)` pairs the globs match) differs from the stored record, or when the stored run exited non-zero; otherwise the record is reused and the runner is never invoked. Editing a declared file, the command, or the glob list reruns it; editing an undeclared file does not; a new commit leaves the artifact `current` but `commit_matches_head` turns false so the exact commit it was measured at is always visible. `--force` reruns regardless (the reviewer's challenge path). Records bind the command sha, identity sha, input hash, head, branch, and dirty flag, and keep at most 8192 bytes of output with the sha256 and byte count of the full output (`truncated` when cut). `design_evidence_view` reports every artifact as `current`, `stale`, `failed`, or `missing`; the dashboard snapshot and the Architect/Reviewer role input show output for `current` artifacts only, and the `design_evidence_recorded` event carries hashes and metadata, never output. See "Design evidence" below.
- **After the first full design review, a follow-up reviewer gets a bounded delta packet, and only for the exact design it was built for (#36).** `record-design-review` accepts repeatable `--finding "text"` (at most 32 per review, 512 characters each, ids `F<attempt>.<n>`), stores them with the record's `attempt` and the commit `head`, and appends a bounded `design_review_history` entry (last 8: attempt, decision, reviewer, design_hash, head, sorted `criteria_ids`, per-criterion spec hashes, `structural_blocker`, findings). `design-review-packet --by ACTOR [--disposition ID=resolved|rejected|unresolved[:note]]...` (Phase 2 only; refused with nothing written while `design_review_attempts` is 0, so the first review always receives the full task) builds `lib.build_design_review_packet`: a deterministic, canonical, sorted packet with the criteria delta against the last recorded review (`added`/`removed` by id, `changed` by spec hash), every prior finding with its disposition (an omitted finding is `unresolved` with a null note, so silence never reads as resolved; `rejected` requires a note; an unknown, duplicate, or malformed disposition is refused before anything is written), `new_findings_since`, the `design_evidence_view` entries with `stale_for_packet`, the repository identity, and `stale`/`stale_reasons` (set when the previous review's head is unknown or differs from HEAD, in which case `files_changed_since_previous` lists `git diff --name-only`). The serialized packet is at most 65536 bytes: over budget it is trimmed in a fixed order (files first 200 then halved, unchanged ids collapsed to a count, finding text and notes cut to 256, evidence reasons dropped, findings cut from the end), each step recorded in `truncated`, and finding ids, criteria ids, hashes, attempt numbers, repository identity, and `stale_reasons` are never trimmed; `packet_id` is sha256 of the final body, so identical inputs give byte-identical packets. Stored as `design_review_packet` with event `design_review_packet_generated` (packet_id, attempt, design_hash, byte size, counts; never finding text). A managed Phase-2 Reviewer whose stored packet is for `attempts + 1` and the current `design_hash` gets `# Delta review packet` (JSON) in front of its role prompt, and its session records `packet_id` and `design_hash`; a packet for another design or attempt is ignored and the reviewer gets full context. Mission Control shows the packet's attempt, disposition counts, criteria delta, and stale flag.
- **Live session status is derived, never declared, and the session record outranks the beacon (#33).** `handsoff_agent.py` writes `.handsoff-live.json` every 5 s while a managed child runs and once more after the terminal `transition_agent_session`, with exactly `session_id`, `role`, `state`, `pid`, `beacon_at`, `ended_at`, `exit_code`; every write is best effort (an `OSError` is swallowed, the child's lifecycle, the session record, and `execute_launch`'s result are untouched), the file is gitignored, never hashed, never read by a gate, and no beacon event reaches the ledger. `handsoff_lib.live_status` derives `idle`, `started`, `running`, `waiting`, `stalled`, `stopped`, `failed`, or `complete` from structured state plus that file: a beacon counts only when its `session_id` is the current session's (a fresh beacon from an older session reads `stalled` with `process_signal: none`), a running session with a beacon older than 15 s reads `stalled` with "no process signal for N s", a terminal session reads `stopped` (completed, cancelled) or `failed` (failed, timed out, failed to start) with `ended_at` and `exit_code` copied from the ledger-bound record even if the beacon still says running, and no session at all reads `idle` with "no managed process is running". `status` prints the view as `live`; Mission Control renders it in the `#live-status` strip and re-reads the snapshot on every beacon write.
- **A managed agent that is still writing output is never reported silent (#41).** `handsoff_agent.py` captures stdout for every managed role (claude and codex children write their work to stdout), streams it through unchanged, parses it for broker requests only when the role is the supervisor, and on every stdout or stderr chunk notes `.handsoff-output-liveness.json` with exactly `session_id`, `role`, `output_at`, `chunks`, `bytes`: two identifiers, one timestamp, two counters, never content. Writes are rate limited to one per second per session (chunks in between only bump the counters), every `OSError` is swallowed, the file is gitignored, never hashed, never read by a gate, and no output event reaches the ledger; phase, progress, acceptance state, and `updated_at` are untouched by any volume of output. `handsoff_lib.output_liveness_for` binds the record to the run only when its `session_id` is the recorded current session for its `role` and that session is launching or running, so output from a completed, failed, replaced, or unknown session never counts and a process exit ends the signal at once (`live_status` reads the terminal state). `stall_warning` reads the freshest of `updated_at`, `last_heartbeat_at`, and the bound `output_at`; `activity_note` reads `Agent active; latest output N seconds ago` when the workflow timestamp is stale but bound output is within `stall_minutes`; an explicit `heartbeat` with no output file still suppresses the warning. `handsoff_lib.activity_view` computes one `activity` object (`source` among `workflow`, `heartbeat`, `output`, `beacon`, `session`, `pause`; `at`; `seconds_ago`; `stall_warning`; `activity_note`) that both `status` and `GET /api/dashboard` carry, so the CLI and Mission Control cannot disagree; `live` gains `activity_source` naming the same signal.
- **Follow-up design checks can go to an economical reviewer profile, and never silently to the wrong one (#37).** With `[agents].reviewer_followup` and `[models].reviewer_followup` both set (one without the other is a config error; both absent means today's single-profile behavior, and the keys are a cost knob outside the governance hash), `lib.select_design_reviewer_profile` picks the tier for the next Phase-2 reviewer launch by a fixed precedence, first match wins and becomes the recorded `reason`: attempt 1 (`first_review`), no follow-up configured (`no_followup_configured`), an unconsumed `design-review-escalate` (`pilot_escalation`), the last review flagged `--structural-blocker` (`structural_blocker`), a criterion id added or removed since the last review's persisted `criteria_ids` (`criteria_structure_changed`; a text-only edit does not count), else the follow-up tier (`delta_check`). Two checks then apply to whichever tier was selected and never switch it: with tiering on, the selected adapter/model must differ from the architect's and the implementer's or the clash is named and the launch refused; and the selected adapter executable must be on `PATH`, otherwise `<tier> reviewer profile unavailable: <adapter> is not on PATH; install it, set fallback_policy.reviewer, or remove reviewer_followup` with no session, event, or status change (a missing follow-up never falls back to primary, a missing primary never falls forward). The reviewer session records `tier`, a `design_reviewer_selected` event carries `tier` and `reason` with the launch, and `record-design-review` stores `reviewer_profile` (`adapter`, `model`, `tier`, `reason`) and `structural_blocker` on the record and its history entry, consuming any open escalation. `design-review-escalate --by PILOT [--note]` is human-only (the broker refuses it), Phase 2 only, one at a time. `status` and Mission Control (`policy.design_reviewer_selection`, rendered as `Review profile: <tier> <adapter>/<model> (<reason>)`) show the latest recorded review's tier and what the next launch selects, including any refusal. See "Follow-up reviewer profile" below.
- **Design rounds are tracked, not just capped.** `advance --new-design-round` (only valid when advancing to Phase 2) increments `design_round` by one from whatever is actually on disk, so the cap fires on organic drift rather than only on a manually over-typed number; `--design-round <n>` remains available as an explicit override for fixtures/recovery. Each increment logs a distinct `design_round_advanced` event (not a generic `phase_advanced`) carrying `design_round`, `previous_design_round`, and an optional `--design-round-reason`, so repeated rounds are distinguishable in `handsoff-events.jsonl`.
- **Design-phase time is explainable, not just totaled (AR8).** A round closes (`design_round_ended`) either when the next one starts or when design is approved. A blocked `advance` to Phase 3 caused specifically by the design gate auto-logs `design_approval_requested` (deduped against repeated polling) -- the state machine noticing the run just started waiting on the human, with no separate command needed; `design_approved` remains the existing "granted" event. `background-wait-start`/`-end` and `human-pause-start`/`-end` are small explicit commands the driving agent calls for a background task or a human question mid-round; `background-wait-start` feeds `last_heartbeat_at` through the exact same path `heartbeat` does (see stall detection above), not a second liveness mechanism. `design-timing` (optionally pointed at any `handsoff-events.jsonl`, including an archived run's, via `--events-file`) reads all of this back into active/background_wait/human_wait seconds per round and per design "episode" (a design phase can reopen after a criterion mutation forces a rollback, per AR-003).
- **Writes are atomic.** Every status write lands in a sibling temp file first, then replaces the real file; a process killed mid-write leaves the old file intact, never a truncated one.
- **The audit logs are tamper-evident and tail-anchored.** Events and verification records are separately hash-chained. A stored chain head detects edited, reordered, and deleted tail records. Every event also binds the exact status and acceptance file hashes, so an unlogged hand edit is detected; `verify-log` checks both ledgers and current state.
- **`handsoff.toml` is actually read.** `status_file`, `acceptance_file`, `event_log`, the round/stall limits, and `[checks].commands` all come from the config, not hardcoded defaults, with sane defaults only when a key is absent.
- **A criterion points at something real.** `verify` persists command, exit code, output hash, actor, timestamp, criterion id, and criterion-specification hash. Manual/browser evidence is an explicit named attestation through `record-evidence`, not an arbitrary string inserted into JSON.
- **Identities and records are typed, not just present.** `implemented_by`, `reviewed_by`, and evidence-id fields must be non-empty strings or null; `review` and `deployment_approved` records must carry a non-empty `by`, a timezone-aware `at`, and an `acceptance_hash`, or the whole state is refused as malformed rather than partially trusted. `init`, `verify`, `record-evidence`, `record-review`, and `verify-live` all reject an empty or whitespace-only `--by`/feature/description before touching disk, so `init ""` cannot brick a project against reinitialization and no command can attach evidence to a blank actor.
- **Evidence is structurally validated, not just cryptographically authenticated.** The hash chain proves a verification record was not altered after it was written; it says nothing about whether the record was ever real. Every loaded record, not only ones this CLI just wrote, is separately checked for a non-empty `by`, a recognized `kind`, a non-empty `criteria` list, and a boolean `ok`; a hand-crafted record that chains and hashes perfectly but claims an empty actor is still refused.
- **`handsoff.toml`'s governance settings are in the chain of trust.** `deployment_requires_explicit_approval`, `require_live_verification`, and the round/stall limits are hashed into every review, deployment-approval, and live-verification record at the moment it is granted. Changing one of those settings afterward invalidates the decisions bound to the old value, the same way changing the acceptance registry already did; `[checks].commands`/`live_commands` and the file paths are deliberately excluded from this hash so routine test-list edits do not need a fresh review.
- **A configured state-file path cannot resolve outside the project root, symlinks included.** `handsoff.toml`'s `status_file`, `acceptance_file`, `event_log`, and `verification_log` are checked both as literal strings (no `..`, not absolute) and by resolving the real path and confirming it is still a descendant of the resolved project root, so a symlinked directory component cannot quietly redirect state outside the project.
- **One validator, not two that can drift.** `handsoff_supervisor.py validate` and `validate_handsoff_status.py` both call the same `compute_errors()` in `handsoff_lib.py`.
- **An interrupted cross-file write has a supported recovery path that cannot be used to launder a hand edit.** Every mutating command writes through one shared `commit()` in `handsoff_lib.py`, which journals the exact digest of what it is about to write *before* touching any file, then clears the journal once the describing event is durably appended. `doctor` (see Quick start) uses that journal, not gate-passing alone, to tell a real interrupted write apart from an untracked edit that merely happens to still validate: it closes the event-log gap only when the current status/acceptance content exactly matches a journal entry proving a real command intended it, and it patches a stale `verification_head` anchor (a value it derives itself from the authenticated ledger, never from file content) only when status has not otherwise drifted from the last logged event. A hand edit with no journal behind it is refused, even if it happens to pass every other gate; so is a journal-confirmed write whose content still fails validation. Anything else (a broken hash chain, a deleted tail) is reported and left for the operator to restore from version control or backup.

## Default crew

A role that `handsoff.toml` does not name (no `[agents].<role>` key, no `[models].<role>` key, or the legacy `configure-me` placeholder) launches with the recommended crew:

| Role | Adapter | Model | Why |
| --- | --- | --- | --- |
| Architect | `claude` | `claude-opus-5` | Design and acceptance criteria need the strongest reasoning available; a weak design costs more later than a premium model costs now. |
| Supervisor | `claude` | `claude-opus-5` | The same Opus profile is kept for orchestration, by operator preference, so one run has one consistent voice reading and enforcing its own state. |
| Implementer | `claude` | `claude-opus-5` | Same profile as the Architect and Supervisor, again by operator preference: the implementer reads the design the way its author meant it. |
| Reviewer | `codex` | `default` | An independent provider family, so the critique never comes from the model being critiqued. `default` sends no model flag; the Codex CLI picks from its own configuration. |

The table lives in code as `handsoff_lib.RECOMMENDED_CREW`, and `DEFAULT_CONFIG["agents"]`/`["models"]` are built from it.

**Overriding any role.** Set the key for that role only; the other three keep the recommended defaults.

```toml
[agents]
reviewer = "claude"          # reviewer adapter overridden, the other three stay recommended

[models]
reviewer = "sonnet"          # reviewer model overridden
architect = "claude-opus-5"  # naming the recommended value explicitly is fine; it is then reported as explicit
```

Two rules keep an override from producing something you did not ask for:

- An explicit `auto` is a real choice, not a placeholder: it keeps the older auto-detect path (first installed adapter in the order Codex, then Claude Code), and its session telemetry still says `auto_detected`.
- The recommended model belongs to the recommended adapter. If you override a role's adapter to one that differs from its recommended adapter and name no model, the role runs with the runner default (`default`, no model flag) rather than the other runner's model id, and that model is reported with source `runner_default`. Name a model to pin one.

**How the defaults are reported.** `load_config` records `cfg["profile_sources"][role] = {"adapter": ..., "model": ...}`, each `explicit`, `recommended`, or `runner_default`. `handsoff_supervisor.py status` prints `crew` (from `handsoff_lib.crew_view`) with, per role, the requested adapter and model, both sources, `available`, and `executable`; the dashboard settings view carries the same `crew` plus `profile_sources`, and the Agent Settings dialog labels a role `recommended default` when both halves came from the table. `available` is `executable discovery only`: it says the adapter executable is on `PATH` and nothing more. Whether a model id is valid for Claude Code or Codex cannot be checked offline, so the view states that scope instead of claiming it. A managed launch of a recommended role records `resolution_source: "recommended"` on its session; a launch whose adapter is not installed is refused before any session exists, with a message naming the role, that the profile was the recommended default, and the remedies (install the adapter, set `[agents].<role>` and `[models].<role>`, or add a `fallback_policy.<role>` profile), so nothing ever claims the recommended profile ran when it did not.

## Design evidence

Some design questions are cheaper to measure than to reason about (an AST inventory, a dependency graph, a schema dump), but rerunning the measurement on every Architect or Reviewer launch wastes tokens and wall clock. `[[design_evidence]]` caches such measurements and binds each cached result to the exact command and input files that produced it:

```toml
[[design_evidence]]
id = "ast-inventory"                              # [a-z0-9-]{1,64}, unique
command = "python3 tools/ast_inventory.py"        # trusted, same level as [checks].commands
inputs = ["bin/*.py", "tools/ast_inventory.py"]   # globs relative to the project root
```

At most 16 entries; an absent section changes nothing. Two commands:

- `handsoff_supervisor.py design-evidence run [--id ID ...] --by ACTOR [--force]` runs every requested artifact that is missing, stale, or failed and reuses the rest; `--force` reruns even a current one. Commands run with `shell=True` in the project root under `[checks].timeout_seconds`, like `verify`.
- `handsoff_supervisor.py design-evidence show` prints the state view as JSON (`current`, `stale`, `failed`, `missing`, with the reasons, hashes, and `commit_matches_head`), never the output.

Results live in `handsoff-design-evidence.json`. That file is generated state and belongs in `.gitignore` (this repository's already lists it); it is **not** a ledger: it holds the bounded output (8192 bytes at most) of trusted configured commands and can be deleted at any time to force a fresh measurement. The hash-chained event log records only `design_evidence_recorded` events carrying the artifact id, input hash, output sha256, exit code, truncation flag, and commit. The Architect and the Reviewer receive a `# Design evidence` section in their role input listing every artifact by state, with output for `current` artifacts only; the Supervisor and the Implementer do not. Mission Control shows the same states as pills.

## Delta review packets

The first independent design review always gets the full task. Every review after it can start from a delta instead (#36):

```bash
handsoff_supervisor.py record-design-review --by codex-reviewer --architect claude-architect \
    --request-changes --summary "Two gaps" --finding "No failure-mode criterion" --finding "Tests name no fixture"
# ... the Architect revises the criteria, maybe commits ...
handsoff_supervisor.py design-review-packet --by claude-supervisor \
    --disposition "F1.1=resolved" --disposition "F1.2=rejected:the fixture is named in the test list"
```

The packet (`design_review_packet` on status, printed as JSON) carries the criteria delta since the last recorded review, each prior finding with its disposition (omitted means `unresolved`), the new findings, the cached design-evidence states flagged `stale_for_packet` when the repository moved, the repository identity, `stale`/`stale_reasons`, and `files_changed_since_previous`. It is canonical and sorted, at most 65536 bytes (trimmed in a fixed order with a `truncated` map, ids and hashes never cut), and `packet_id` is the sha256 of the final body. The next managed Reviewer launch in Phase 2 receives it as `# Delta review packet` in front of the role prompt only while it matches the next attempt and the current `design_hash`; edit a criterion after generating it and the reviewer silently gets full context again until a new packet is generated. The session records `packet_id`/`design_hash`, and the `design_review_packet_generated` event carries the id, attempt, hash, byte size, and counts, never finding text. The Supervisor can run both commands through the broker (`record-design-review` with `findings`, `design-review-packet` with `dispositions`).

## Follow-up reviewer profile

A design that came back with two findings rarely needs the premium reviewer to read the whole thing again. With both keys set, every attempt after the first that reaches the `delta_check` rule launches the follow-up profile instead (#37):

```toml
[agents]
reviewer = "codex"                 # primary tier: the first review and every structural re-review
reviewer_followup = "claude"       # follow-up tier: delta checks only

[models]
reviewer = "default"
reviewer_followup = "claude-haiku" # must differ from the architect and implementer profiles
```

What sends an attempt back to the primary tier, in precedence order: the first review; an open `handsoff_supervisor.py design-review-escalate --by PILOT [--note "why"]` (Pilot-only, consumed by the next `record-design-review`); the previous review recorded with `record-design-review --request-changes --structural-blocker`; a criterion id added or removed since that review (a text-only `criterion-update` keeps the delta check). The selection is pure over the config, the status file, and the acceptance registry, so `status` (`design_reviewer_selection`) and the dashboard show exactly what the next launch will do, and `next.error` names an independence or availability refusal in advance. Remove both keys to return to a single reviewer profile; the four role keys, the Agent Settings dialog, and every existing record are unaffected either way.

## Benchmarking the design phase

`tools/benchmark_design_phase.py` (#45) runs two Handsoff design phases on a temporary clone of a fixture repository, `baseline` (no follow-up reviewer, no design evidence, no delta packets) and `tranche` (all three on), with the same task, seeded criteria, and seeded structural defects, through real managed Architect and Reviewer sessions. `--stub` installs a deterministic adapter and proves the harness in seconds; `--live` is the paid measurement and runs only after the Pilot approves a cost estimate. Token counts come only from the runners' structured output (`null` when absent, never estimated). Method, thresholds (30 percent wall clock, 40 percent tokens, every baseline-found defect retained), reproduction commands, and the results table live in `docs/benchmark-33-40.md`; `benchmark/` output is gitignored.

## Review attempts and the convergence cap

Implementation reviews are persistent `review_attempts`, not chat claims. Starting a managed Reviewer opens an attempt automatically; findings close it as `changes_requested`, and approval closes it as `approved`. Findings are accepted only in Phase 4 or later and the complete proposed state is validated before it is written. Evidence attached while review is open refreshes that attempt's acceptance binding atomically, while changing a criterion specification abandons the attempt. A stale attempt can still be closed fail-closed with findings, but can never be approved. `review_round` is derived from the legacy offset plus that ledger. At `max_review_rounds`, Handsoff blocks before another Reviewer launches and Mission Control shows the required operator action. Only `review-cap-override --by OPERATOR --reason TEXT` grants one additional attempt.

## Automatic recovery of stalled runs

The `[recovery]` policy drives a lease-protected watchdog. It evaluates the role assigned to the current phase and that session's own liveness; unrelated agent activity cannot hide a failed or silent worker. `recover --by ACTOR` performs one bounded restart while preserving phase, acceptance, and evidence. Exhaustion becomes a visible blocked escalation. Liveness pings are advisory and unauthenticated: they may postpone recovery, never trigger it; a presumed-lost child is superseded rather than signalled.

## Focused checks versus full regressions

Focused criterion checks stay in `[checks].commands`. Full suites are separate named groups:

```toml
[regression_gate]
approval_timeout_minutes = 30
launch_window_minutes = 10

[[regressions]]
name = "python-full"
commands = ["python3 tests/test_handsoff_supervisor.py"]
```

Handsoff normalizes test footprints and refuses a configured whole-suite command—or an equivalent spelling—through ordinary `verify`. The lifecycle is `regression-request --group NAME --by ACTOR --reason TEXT`, a same-origin Mission Control **Accept Regression** or **Decline** decision, then `regression-run --request-id ID --by ACTOR`. Acceptance is single-use and bound to the run, exact command hash, repository content and commit pair, configuration, acceptance, work-item scope, requester session, and session/recovery epoch. Expiry or any bound change invalidates it. A launched request locks ordinary workflow mutations until the nonce-bound runner terminalizes; only a human can fail a stranded launch with `regression-finalize`. Handsoff cannot intercept arbitrary operating-system processes outside its execution boundary, so role instructions explicitly prohibit external-shell bypasses.

### Criteria transactions

When the Architect revises several criteria at once (a design round that adds five, respecifies five, and drops two), one `criteria-apply` call replaces the equivalent sequence of single commands, produces the same final registry, and logs one event instead of twelve. The file is a JSON object with exactly `operations`:

```json
{"operations": [
  {"op": "add", "criterion": {"id": "REQ-031", "type": "supporting",
   "requirement": "#44 Exact observable outcome", "verification": "automated",
   "tests": ["python3 -m unittest tests.test_fix -v"]}},
  {"op": "update", "id": "REQ-002", "fields": {"requirement": "#44 Revised outcome"}},
  {"op": "remove", "id": "REQ-005"}
]}
```

An `add` criterion object has exactly `id`, `type`, `requirement`, `verification`, and a non-empty `tests` list; `state` and `evidence` are not accepted and start as `not_tested` and `[]`, exactly as `criterion-add` sets them. `update.fields` is any non-empty subset of `requirement`, `verification`, `tests`, `type`, `state` (`state` among `failing`, `not_tested`, `blocked`, the same set `criterion-update` takes); a spec change clears `evidence` and resets `state` to `not_tested` unless `state` is given. Operations apply in file order, and each id may appear in at most one operation, so a criterion the transaction adds cannot also be updated or removed by it. A changed or added `primary_fix` resets the original-symptom binding, as the single commands do.

```bash
python3 bin/handsoff_supervisor.py criteria-apply --file TX.json --by architect-1 --dry-run   # plan as JSON, nothing written
python3 bin/handsoff_supervisor.py criteria-apply --file TX.json --by architect-1             # one commit, one event
```

The dry-run plan lists every operation with its `previous_hash` (spec hash before, null for add) and `resulting_hash` (null for remove), `registry_hash_before`/`registry_hash_after` (`acceptance_hash`), `design_hash_before`/`design_hash_after`, `work_items_after` (the effective work item ids), and `would_invalidate`. The applied event carries the same operations and hashes, so the plan a reviewer saw and the change that landed can be compared hash for hash. The single commands are unchanged and keep working before and after a transaction; a legacy registry without persisted `work_items` applies cleanly and stays derived.

### Amendment lane

Once a design is approved and the run is past Phase 2, a small correction to an already-approved criterion (a requirement sharpened after the Implementer's finding, a test command renamed) does not have to throw the design away. `amendment-open` reads the same `{"operations": [...]}` file as `criteria-apply`, plans it with the same planner, and classifies the delta itself. The caller never chooses: an `add`, a change to which criterion is the `primary_fix`, a verification downgrade, a change spanning two work items, a change to the work-item scope, or `--request-full-redesign` is `full_redesign` and refuses to open; everything else is `scoped`.

```bash
python3 bin/handsoff_supervisor.py amendment-open --file TX.json --by architect-1 --summary "Sharpen REQ-002"
python3 bin/handsoff_supervisor.py verify --criterion REQ-002 --by implementer-1        # evidence the correction while frozen
python3 bin/handsoff_supervisor.py amendment-review --by reviewer-1 --approve --summary "Scoped, matches the finding"
python3 bin/handsoff_supervisor.py amendment-approve --by pilot                         # human-only; resumes the frozen phase
```

A scoped open applies the operations in one commit (`amendment_opened`), resets the changed criteria to `not_tested` with their evidence cleared, leaves every other criterion untouched, invalidates the review, deployment, and live decisions, and freezes the run at its current phase and progress. `status["amendment"]` holds the open record: `amendment_id` (`am-<32 hex>`), `base_design_hash`, `changed_ids`, `dependent_ids` (criteria in the same work item whose requirement text names a changed id, a cheap deterministic signal for the reviewer), `affected_work_items`, the `operations` with their spec hashes, `amendment_hash`, `resulting_design_hash`, `classification` and `classification_reasons`, `frozen_phase`/`frozen_progress`, `review`, `pilot_approval`, `state`. While it is open, `advance`, `record-review`, `deployment-gate`, `verify-live`, `criterion-*`, and `criteria-apply` are refused; `verify` and `record-evidence` are not.

The review binds to `amendment_hash`, recomputed from the registry on disk; `amendment-approve` recomputes it again and refuses with an audit block when anything moved after the review. On approval the design decisions are rewritten to the amended design hash (with the amendment id appended to `amended_by`), the amendment closes as `approved` into `status["amendments"]`, and the run resumes exactly where it was frozen (`amendment_reviewed`, `amendment_approved`; no `phase_advanced`). `--request-changes` keeps it open: the Architect applies `amendment-revise --file TX.json --by architect-1` (same rules over the cumulative delta; the hash is recomputed and the stale review cleared) or `amendment-escalate --by ACTOR --reason TEXT` closes it as `escalated` and takes the full redesign path (`amendment_escalated`: design decisions cleared, a flagged run back to Phase 2, the amended registry kept). The broker allows `amendment-open` (`by`, `file`, optional `summary` and boolean `request_full_redesign`), `amendment-revise` (`by`, `file`), `amendment-review` (`by`, `decision` of `approve`/`request-changes`, `summary`), and `amendment-escalate` (`by`, `reason`) for the Supervisor; `amendment-approve` is human-only.

### Verification cache

Naming several criteria in one `verify` call already ran the union of their commands once. Since #43 that launch is also bound to the exact state it proved something about, so a later `verify` of the same commands against unchanged state launches nothing:

```bash
python3 bin/handsoff_supervisor.py verify --criterion REQ-001 --criterion REQ-002 --by implementer-1    # launched: [cmd], reused: {}
python3 bin/handsoff_supervisor.py verify --criterion REQ-001 --criterion REQ-002 --by implementer-1    # launched: [], reused: {cmd: vr-...}
python3 bin/handsoff_supervisor.py verify --criterion REQ-001 --by implementer-1 --no-cache             # launched again regardless
```

Every `checks` record carries `binding` (command to binding hash), `executed`, `reused_from` (the source `vr-...` run id, or null), and `feature_hash` inside the hashed, chained record, and each entry in `results` carries `duration_s`, `timed_out`, `truncated`, and `output_bytes` next to the command, exit code, and output digest. A reused record copies its results from the source (each copied entry also names its `reused_from`), so the flags travel with it. A criterion with several tests gets `executed true` only when every one of its commands was launched in that call; a partially reused record is never a source and names a single `reused_from` only when all of its reused results came from one record. The CLI output lists `launched` and `reused`, the `checks_run` event carries `launched_count` and `reused_count`, and Mission Control's evidence telemetry shows an EXECUTED or REUSED pill per record (a record written before #43 has neither field, loads as before, and is never a reuse source).

Invalidation is implicit: editing or adding any file the digest covers, changing a check command, the timeout, a regression group, a governance value, or the spec of a bound criterion changes the binding, and the next `verify` launches. The cache is the ledger itself: there is no side file to clear, and `.handsoff-verify-inflight/` holds only lock files.

## Archive analysis

`bin/handsoff_analyzer.py` (stdlib only) mines the completed-run archive
described above and files evidenced improvement tickets, so what Handsoff
learns about itself across every project turns into tracked work instead of
a folder nobody reads (#49).

**What it reads.** Every `*.json` under the archive directory. A file that
does not parse is listed under `unreadable` and skipped. Two archives with
the same `repo` and `started_at` are one run: the newest `archived_at` is
kept (an exact tie keeps the file name that sorts last) and the rest are
listed under `duplicates`. An archive whose `run_kind` is `test`, or, when
`run_kind` is absent, whose repo name starts with `handsoff-test-`,
`handsoff-selfcheck`, `handsoff-dropin`, `handsoff-benchmark`, or
`handsoff-fixture`, is listed under `skipped_fixtures` and never mined.
Nothing in the archive is ever deleted or rewritten. `archive_run` writes
`run_kind` into every new archive: an explicit `HANDSOFF_RUN_KIND` of `test`
or `product` wins, otherwise the run root's name decides by the same
prefixes, else `product`.

**Facts per run**, read only from ledger records: the outcome
(`status.status`); the design-phase hours as wall clock from the first
`phase_advanced` to Phase 2 (or `initialized` when there is none) to the first
`phase_advanced` to Phase 3, waits included, unmeasured when either timestamp
is missing or unparsable; the `design_review_approved` and
`design_review_changes_requested` attempt count; the
`design_review_budget_exhausted` and `design_review_attempt_authorized`
counts; criteria mutations after the last `design_approved`
(`criterion_added`, `criterion_updated`, `criterion_removed`,
`criteria_transaction_applied`); the `recovery_escalated` count and whether
any `agent_session_*` event exists; the `question_raised` count; `pilot_note`
texts; the `checks_run` `launched_count` and `reused_count` sums; failed
`live_checks_run` events (`verify-live` failures); `review_cap_override_recorded`
counts. Timestamps are normalized to UTC ISO-8601.

**Rules** (fixed ids; weight decides filing priority, highest first):

| Rule | Weight | Fires when |
| --- | --- | --- |
| R7 | 100 | each distinct `pilot_note` text, with the run ids that carry it |
| R6 | 90 | any `verify-live` failure |
| R3 | 80 | `recovery_escalated` on any run with zero `agent_session_*` events |
| R1 | 70 | design-review budget exhausted in at least 2 product runs |
| R2 | 60 | criteria mutated after design approval in at least 2 runs |
| R5 | 50 | median measured design-phase hours above `[analysis].design_phase_hours_threshold`, at least 3 measured runs |
| R4 | 40 | reused / (launched + reused), summed over product runs with at least 2 `checks_run` events, below 0.20 with at least 3 such runs |
| R8 | 30 | Pilot authorizations past the design-review budget in at least 2 runs (report only) |
| R9 | 30 | review cap overrides in at least 2 runs (report only) |

Findings are ordered by weight descending, then rule id, then sorted run
ids; run-id lists are sorted; percentages are rounded to one decimal, hours
to two. A finding with an empty run-id list is never written or filed.

**Drafts and filing.** Each R1 to R7 finding drafts an issue in house style:
a title, `## Symptom` with the evidence (run ids and numbers), `## Cause
hypothesis` labelled as a hypothesis, `## Benefit`, `## Required behavior`,
`## Acceptance criteria`, and a hidden marker line `<!-- handsoff-analysis
rule:<id> -->` (R7 markers carry a digest of the note text after the rule
id, so one filed note never suppresses a different one). A draft may contain
only: the archive file name as run id, the repo, the first 120 characters of
the feature title, event kinds, timestamps, numeric counters, boolean
outcomes, and pilot-note text. No prompt, output, environment, status,
acceptance, verification, or event-message content is ever serialized into
a draft or a report. Filing goes through an injectable GitHub client; the
default wraps `gh` (`gh issue list` and `gh issue create`, no shell) and
tests inject a fake. Every filed issue carries the labels
`from-archive-analysis` and `needs-triage`. Before filing, the client lists
open issues and issues closed within `dedupe_days`; a draft is suppressed
(and listed in the report) when an existing issue carries the same marker or
its normalized title shares at least 60 percent of tokens (Jaccard over
lowercase alphanumeric tokens). At most `max_tickets_per_scan` issues are
filed per scan, highest weight first; the rest are listed under
`not_filed`. `--dry-run`, `filing = "report_only"`, and a missing `gh`
executable file nothing and say so in the report's `filing` entry.

**Hard exclusions.** `GATE_WEAKENING_RULES` is the fixed set {R8, R9}: their
natural remedy is raising `max_autonomous_design_reviews` or
`max_review_rounds`, so they produce report entries only and are never
filed, with no configuration to lift that.

**Triggers and the report.** `advance 8` with status `complete` runs a scan
right after the archive write when `[analysis].enabled` is true (honouring
`HANDSOFF_ARCHIVE_DIR`), includes the archive just written without
rewriting it, and records `archive_scan_completed` (findings, filed,
suppressed, excluded, skipped_fixtures, unreadable counts, report path) on
the live run's ledger only. A scan failure prints
`HANDSOFF_ANALYSIS_FAILED (run still completed successfully): ...` and never
fails the advance. On demand:

```bash
python3 bin/handsoff_supervisor.py analyze-archives [--dry-run] [--archive-dir DIR]
python3 bin/handsoff_supervisor.py pilot-note --by moncy --text "Reviewer keeps asking for screenshots"
```

Each scan writes `.handsoff-analysis/<timestamp>.json` (gitignored and
excluded from the repository digest) and prints `HANDSOFF_ANALYSIS_REPORT:
<path>`. `pilot-note` (1 to 512 characters) records a `pilot_note` event on
the current run; `POST /api/pilot-note` (same-origin, actor `Mission Control
Pilot`) does the same from the small input in the dashboard header; the next
scan lists that note as an R7 finding with its run id.

**`[analysis]` keys** in `handsoff.toml` (every key optional; an invalid
value is a load error like every other section):

```toml
[analysis]
enabled = true                      # bool, default true
max_tickets_per_scan = 5            # int 0 to 50, default 5
dedupe_days = 30                    # int 0 to 365, default 30
design_phase_hours_threshold = 1.0  # number greater than 0, default 1.0
archive_dir = "~/Documents/Handsoff-Archive"  # optional; default HANDSOFF_ARCHIVE_DIR, else the Documents archive
filing = "gh"                       # "gh" (default) or "report_only" (never construct a GitHub client)
```

## Work items and the per-item status table

New runs persist `work_items` automatically. GitHub references become stable `issue-N` identities; criterion prefixes such as `[#29]` and `[cross]` map acceptance to `issue-29` and `ask-cross`. Plain asks split only on explicit semicolons, newlines, or numbered prefixes—never on an ambiguous conjunction. `work-items-sync --by ACTOR --from-tickets` migrates a legacy run; `work-item-activate` records the current item, and `work-item-update` changes display metadata or recorded GitHub state. Mission Control automatically shows the multi-item table with canonical Handsoff status, next action, blocker, timestamps, and GitHub discrepancies. Recorded GitHub state is display input only; it never overrides Handsoff evidence. Persisted required items prevent completion until every row is done, while legacy derived-only rows remain informational.

## Known limitations

- **The advisory file lock is best-effort and POSIX-only.** `project_lock()` uses `fcntl.flock` around the whole read-validate-write; on a platform without `fcntl` it is a silent no-op, and multiple writers on such a platform can still race. Enforce single-writer discipline at the process level (only the Supervisor writes `handsoff-status.json`) if you need this on Windows.
- **`schemas/*.json` document the expected shape; they are not executed.** Runtime enforcement lives in `handsoff_lib.py`. Editing a schema file changes documentation, not behavior.
- **A killed process can leave a stray temp file.** `atomic_write_json` cleans up its `.tmp<pid>` file on any ordinary exception, but a `SIGKILL` or power loss between the write and the atomic rename can still leave one behind. Harmless (the real file is never touched), just worth pruning occasionally.
- **Stall detection reads the freshest of `updated_at` and `last_heartbeat_at`,** not `updated_at` alone. A process that hangs without ever calling `advance` again, and never calls `heartbeat` either, correctly shows as stalled. A process that is merely slow but still calling `advance` periodically will not, as before. A process doing legitimate long background work (no progress to report, but alive) should call `heartbeat --by <id>` periodically; while that heartbeat is fresh, the run reads as "working (background task)," not stalled, even though `updated_at` itself is stale. This still is not a live process heartbeat in the OS sense: it proves *something* called `heartbeat` recently, not that the specific background task it describes is still running, so a caller that calls `heartbeat` and then genuinely hangs will misreport as busy until its next heartbeat would have been due.
- **`verify` and `design-evidence run` run commands with `shell=True`.** `[checks].commands` and `[[design_evidence]].command` are trusted configuration, the same trust level as any other line in `handsoff.toml`; do not populate them from untrusted input.
- **Recovery costs real agent time and is bounded.** A recovery launch is a new model session. `max_attempts` limits that cost, and exhaustion requires an operator acknowledgement.
- **`verify` runs a deliberately small shell-command language.** `[checks].commands` is trusted configuration, but Handsoff rejects command substitution, process substitution, redirects, control operators, and other shell expansion forms before execution. Simple argv and test-path globs remain supported.
- **Hash chains detect tampering; they do not provide access control.** A writer that can replace a ledger and its separate anchor can forge a new history. Protect the project directory and CI artifacts with normal filesystem/repository permissions. Mission Control has two narrow loopback-only write endpoints: Agent Settings updates role profiles, and Authorize Design records the current hash-bound human approval through the existing gate. Anyone able to execute same-origin browser code on that local dashboard can invoke those actions; other workflow mutations remain unavailable through the dashboard.
- **Cross-file commits are fail-safe, not transactional.** A crash between appending evidence and updating status leaves a chain-head mismatch that blocks delivery. It will not silently accept the half-commit; run `handsoff_supervisor.py doctor` to attempt recovery. `doctor` only closes the two journal-provable gaps described above: an event log or acceptance/status pair damaged by anything else (a broken hash chain, a deleted tail, hand-edited ledger lines, an edit with no write-ahead journal behind it) still requires restoring from version control or backup.
- **`doctor` recovers ledger *anchoring*, not lost writes.** If the crash happened before a write reached disk at all (as opposed to after one write landed but before its companion write or event did), there is nothing to recover from; `doctor` will correctly report nothing wrong; get the missing information from the operator or agent that was mid-command.
- **The write-ahead journal (`.handsoff-writeahead.json`) is a single, overwritten-per-command file, not a full log.** It only ever proves the *most recent* in-flight write, matching the project lock's one-writer-at-a-time discipline; it is not a history of past writes and is not meant to be. It is generated state, alongside the lock and chain-head files, and belongs in `.gitignore`.
- **`design-approve` proves an identity, not a review.** It proves that a non-empty, non-architect identity invoked the command, that a non-empty summary was recorded, and that the approval is bound to the exact criteria hash it applied to; it cannot verify that identity is actually human, or that anyone read or understood the design. The untouched-placeholder guard is a literal, exact-text match against `init`'s default criterion (see "Quick start"), trivially defeated by editing even one character of that text while authoring no real criterion at all -- a real but disclosed gap, not a closed loophole. Both limitations mirror `record-review`'s existing trust model exactly: the tool enforces a distinct identity and a bound hash, not the quality of the judgment behind either.
- **`authored_by` records who ran `design-approve`, not independent proof of who wrote the criteria text.** Like `design-approve` proving an identity rather than a review (above), the stamp is exactly the `--architect` string passed at approval time; the tool has no way to confirm that identity actually drafted the requirement/tests it is now attached to, only that someone invoked the command claiming it, distinct from the human approver.
- **`--redesigns-settled-work` proves a flag was set, not that the claim is true.** The CLI cannot verify that a design genuinely redesigns previously-settled work rather than merely fitting around it, or that the human genuinely asked for it -- it can only prove someone explicitly, intentionally passed a non-empty value at approval time, the same trust model as every other identity/attestation this tool already has. The instruction to respect settled work at all (`prompts/architect.md`) is prompt-level, not code-enforced, same as every other role's actual conduct in this tool (see "Agent roles").

## Agent roles

- `prompts/architect.md`: collaborative design authoring, ahead of the Supervisor.
- `prompts/supervisor.md`: state machine, handoffs, gates, and escalation.
- `prompts/implementer.md`: scoped implementation and repair instructions.
- `prompts/reviewer.md`: independent, read-only verification rubric.

These prompts are combined with each assigned task by `handsoff_agent.py`. `[agents]` selects the adapter and `[models]` selects `default` or an exact model ID/alias for all four roles; a role with neither key set takes the recommended crew described under "Default crew". The Reviewer can independently critique the Phase-2 design and later review implementation; the read-only Supervisor requests allowed state transitions through the trusted host broker, which records those distinct decisions with `record-design-review` and `record-review`. The Architect proposes a design and criteria; the human records final design approval with `design-approve` or Mission Control's **Authorize Design** control.

## Import into another project

1. Copy the framework files into the project.
2. Run `handsoff_supervisor.py init "<feature>"`, then use the criterion commands to define the acceptance registry.
3. Set `[checks].commands` and `[checks].live_commands` to the project's pre-deployment and deployed-system checks.
4. Start the Supervisor with the target issue or brief.
5. Run `python3 bin/handsoff_supervisor.py dashboard` for local progress monitoring, agent selection, and explicit design authorization; use the CLI for every other workflow-state mutation.

No project-specific work, credentials, hostnames, or tracker assumptions are included.

## Self-hosting

Handsoff can track work on itself from the repository root. During an active self-hosting run, root `[checks].commands` may contain only that run's explicitly approved targeted checks; fixture tests normalize copied configs so those dogfood commands are never executed accidentally. Anyone importing the framework must replace `[checks].commands` and `[checks].live_commands` with the target project's own checks as step 3 above requires.

A separate, gitignored root remains useful when isolation is preferred (use absolute paths in its `[checks]` commands, since it is not the repo root):

```bash
mkdir -p .handsoff-selfcheck
echo ".handsoff-selfcheck/" >> .gitignore
# write .handsoff-selfcheck/handsoff.toml with real commands, absolute paths
cd .handsoff-selfcheck && python3 ../bin/handsoff_supervisor.py init "..."
```
