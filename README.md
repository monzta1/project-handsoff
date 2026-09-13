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

`init` scaffolds `handsoff-status.json` and `handsoff-acceptance.json` at the project root, and flags the run `requires_design_approval: true`. Use `criterion-update`, `criterion-add`, and `criterion-remove` instead of editing the registry by hand. Every command resolves the project root itself: `--root DIR`, then `$HANDSOFF_ROOT`, then the nearest ancestor with `handsoff.toml`, then the current directory.

The Architect (see "Agent roles") collaborates with the human to turn that placeholder criterion into a real design and testable criteria. In Phase 2 an independent reviewer critiques that design first; the Supervisor records either approval or actionable revision findings:

The Architect scales that collaboration to the request. Small, clear, low-risk work gets a concise scope, approach, and criteria proposal; large, ambiguous, high-risk, or cross-cutting work gets fuller exploration and tradeoff analysis. It states which path it recommends and why. The human can say `go deeper` to expand the design or `that's enough, proceed` to stop exploration and submit the smallest sufficient proposal for independent review. That instruction is not itself design approval; both paths retain the same independent-review and human-approval gates.

```bash
python3 bin/handsoff_supervisor.py advance 2 20 --new-design-round
python3 bin/handsoff_supervisor.py record-design-review --by design-reviewer-1 --architect architect-1 --approve --summary "Design and criteria are implementation-ready"
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

The **Agent Settings** dialog selects `auto`, `codex`, or `claude` plus a runner-default or exact model independently for Supervisor, Architect, Implementer, and Reviewer. `auto` is the zero-config default: at each new role launch it chooses the first installed runnable adapter in the documented order Codex, then Claude Code. Mission Control shows that effective choice; selecting Codex or Claude explicitly overrides it per role. Legacy `configure-me` values also resolve automatically after upgrading. Saving atomically updates `[agents]` and `[models]` without rewriting unrelated TOML. Older three-role adapter-only payloads remain valid: they preserve the Supervisor adapter and any configured Supervisor model while resetting the submitted roles to runner-default models. Availability means only that the executable was found locally—it does not prove authentication, account entitlement, network access, or model validity, and an unavailable profile may still be saved for another machine. The settings and approval endpoints accept only small, exact JSON payloads from the dashboard's own loopback origin and do not enable CORS. Add `--no-open` to start the server without opening a browser, or `--port PORT` to choose another local port.

Launch a configured role in a fresh session with the active project as its working directory:

```bash
python3 bin/handsoff_agent.py inspect architect --task "Design the requested feature"
python3 bin/handsoff_agent.py launch implementer --task "Implement the approved criteria" --by implementer-1
```

`handsoff_agent.py` resolves the executable to an absolute path and never uses a shell. The role prompt and task travel on standard input, not in command arguments. Codex uses an ephemeral `codex exec` session; Claude Code uses headless `claude -p`. `default` omits the model flag. Reviewer and Supervisor launches enforce read-only/plan mode at the runner boundary; Architect and Implementer use explicit workspace-write/accept-edits modes. No bypass-permission flags are generated. Runner output streams directly rather than accumulating in memory; nonzero exits, cancellation, timeouts, missing prompts, and missing executables fail visibly, with timeout/cancellation terminating the fresh process group.

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
- **Deployment approval is load-bearing, not a side command you can skip.** `advance` to Phase 8 checks for a recorded approval; `deployment-gate --approve` is what records it, and refuses before Phase 7 so approval cannot be granted before an Implementer or Reviewer has touched anything. The approval is bound to a hash of the acceptance criteria at the moment it was given; if the registry changes afterward, the Phase 8 gate recomputes the hash and refuses the now-stale approval.
- **Live verified means live checked.** With `require_live_verification = true`, Phase 8 requires `[checks].live_commands` to pass after deployment approval and against the unchanged acceptance registry. `verify-live` re-reads `handsoff.toml` from disk after the checks finish, not the config it captured before starting them, so a policy change landing in that window (a slow live suite is exactly when this matters) is actually detected rather than compared against itself.
- **Mutations are serialized.** `advance`, criterion mutations, evidence/review recording, deployment approval, and verification use the project lock. `verify` rechecks criterion hashes after long-running commands before attaching their results.
- **A malformed hand-edit refuses cleanly, it does not crash.** `progress`, `design_round`, and `review_round` are type-checked before any gate logic casts them, including rejecting `NaN`/`Infinity` (valid JSON-extension floats that pass a plain `isinstance(x, float)` check and then crash `int()`/`float()` arithmetic). A bad value in a hand-edited status.json is reported as a validation error, not a raw Python traceback, in both `handsoff_supervisor.py` and `validate_handsoff_status.py`. Both scripts also catch any unexpected exception as a last resort, for the same reason.
- **The acceptance hash behind a deployment approval ignores harmless reordering.** It sorts criteria by id first, so re-saving or merging `handsoff-acceptance.json` without changing any criterion's content does not falsely invalidate a still-valid approval.
- **Round and stall limits are enforced, not decorative.** `design_round` and `review_round` past `handsoff.toml`'s `max_design_rounds`/`max_review_rounds` block further advancement. A run with no update past `stall_minutes` surfaces a `stall_warning` in `status` for escalation; this is advisory, not a hard block, so a stalled run can still be inspected and unstuck.
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

## Known limitations

- **The advisory file lock is best-effort and POSIX-only.** `project_lock()` uses `fcntl.flock` around the whole read-validate-write; on a platform without `fcntl` it is a silent no-op, and multiple writers on such a platform can still race. Enforce single-writer discipline at the process level (only the Supervisor writes `handsoff-status.json`) if you need this on Windows.
- **`schemas/*.json` document the expected shape; they are not executed.** Runtime enforcement lives in `handsoff_lib.py`. Editing a schema file changes documentation, not behavior.
- **A killed process can leave a stray temp file.** `atomic_write_json` cleans up its `.tmp<pid>` file on any ordinary exception, but a `SIGKILL` or power loss between the write and the atomic rename can still leave one behind. Harmless (the real file is never touched), just worth pruning occasionally.
- **Stall detection reads the freshest of `updated_at` and `last_heartbeat_at`,** not `updated_at` alone. A process that hangs without ever calling `advance` again, and never calls `heartbeat` either, correctly shows as stalled. A process that is merely slow but still calling `advance` periodically will not, as before. A process doing legitimate long background work (no progress to report, but alive) should call `heartbeat --by <id>` periodically; while that heartbeat is fresh, the run reads as "working (background task)," not stalled, even though `updated_at` itself is stale. This still is not a live process heartbeat in the OS sense: it proves *something* called `heartbeat` recently, not that the specific background task it describes is still running, so a caller that calls `heartbeat` and then genuinely hangs will misreport as busy until its next heartbeat would have been due.
- **`verify` runs commands with `shell=True`.** `[checks].commands` is trusted configuration, the same trust level as any other line in `handsoff.toml`; do not populate it from untrusted input.
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

These prompts are combined with each assigned task by `handsoff_agent.py`. `[agents]` selects the adapter and `[models]` selects `default` or an exact model ID/alias for all four roles. The Reviewer can independently critique the Phase-2 design and later review implementation; the read-only Supervisor requests allowed state transitions through the trusted host broker, which records those distinct decisions with `record-design-review` and `record-review`. The Architect proposes a design and criteria; the human records final design approval with `design-approve` or Mission Control's **Authorize Design** control.

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
