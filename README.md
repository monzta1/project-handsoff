<p align="center"><img src="dashboard/logo.png" alt="Handsoff" width="120"></p>

# Project Handsoff

Project Handsoff is a portable, domain-neutral delivery gate for a four-role workflow:

```text
Architect -> design review -> human approval -> Supervisor -> Implementer -> Reviewer -> repair loop -> verified result
```

The Supervisor owns state, phase gates, acceptance evidence, retries, and user escalation. The Implementer changes the target project. The Reviewer independently checks the brief, diff, tests, and original symptom. Handsoff makes their claims auditable; it deliberately does not treat a free-text status update as proof.

MIT licensed, see [LICENSE](LICENSE).

For a clean first install, upgrade, rollback, or migration from a repository that
contains an older copied runtime, follow [INSTALL.md](INSTALL.md). It uses one
stable command path so shell scripts and background services never point at an
obsolete release-specific environment.

## Quick start

Install one versioned engine, then initialize a thin project. Product repositories keep only `handsoff.toml`, `.handsoff-version`, generated run state, and optional hash-declared prompt overrides; they no longer copy the engine, dashboard, prompts, or schemas:

```bash
python3 -m pip install https://github.com/monzta1/project-handsoff/releases/download/v0.3.63/project_handsoff-0.3.63-py3-none-any.whl
python3 -m pip install https://github.com/monzta1/project-handsoff/releases/download/v0.3.63/project_handsoff-0.3.63-py3-none-any.whl
handsoff init /absolute/path/to/project
handsoff doctor /absolute/path/to/project
```

`handsoff doctor` reports the exact engine version, installation source, compatible project pin, offline-verifiable manifest identity, adapter availability, Python version, whether run state exists, and whether a copied legacy runtime still needs migration. Runtime-named paths (`bin`, `dashboard`, `fleet`, `prompts`, `schemas`, `templates`, `handsoff-runtime.json`) are classified by content provenance against the installed manifest signatures, never by name alone: `runtime_paths` lists each detected path as `copied-runtime` or `project-owned` with its source, `legacy_runtime_paths` names only the verified copies, and migration previews never move a project-owned directory (#70). The same report carries a read-only `documentation` audit of the project's root and `docs/` Markdown and text files: each obsolete copied-runtime command form (`python3 bin/handsoff_*.py`) or release reference that differs from the installed engine is listed with its path and a deterministic code, alongside the canonical executable, installed version, and supported pin derived from the installed identity; nothing is rewritten (#71). A missing or incompatible `.handsoff-version` refuses before an agent session is reserved. New projects default to the compatible patch pin `0.3.*`; exact pins such as `v0.3.22` remain supported when strict reproducibility is preferred. Configure `[checks].commands` and `[checks].live_commands`, then initialize the mission from the target project's root or from Mission Control:

```bash
python3 bin/handsoff_supervisor.py init "Fix the thing that is broken"
python3 bin/handsoff_supervisor.py criterion-update REQ-001 --requirement "Exact observable outcome" --verification automated --test "pytest tests/test_fix.py -q"
```

With an installed engine, the equivalent commands are `handsoff supervisor init ...` and `handsoff supervisor criterion-update ...`; the legacy script paths remain supported for existing drop-in repositories.

`init` scaffolds `handsoff-status.json` and `handsoff-acceptance.json` at the project root, and flags the run `requires_design_approval: true`. Use `criterion-update`, `criterion-add`, and `criterion-remove` instead of editing the registry by hand. A batch of changes goes through `criteria-apply --file TX.json --by ACTOR` as one all-or-nothing commit (see "Criteria transactions"). Every command resolves the project root itself: `--root DIR`, then `$HANDSOFF_ROOT`, then the nearest ancestor with `handsoff.toml`, then the current directory.

### Engine upgrade, rollback, and legacy migration

All lifecycle changes have non-destructive previews:

```bash
handsoff upgrade /path/to/project --to 0.3.* --dry-run
handsoff upgrade /path/to/project --to 0.3.*
handsoff rollback /path/to/project --dry-run
handsoff rollback /path/to/project
handsoff migrate /path/to/legacy-drop-in --dry-run
handsoff migrate /path/to/legacy-drop-in
```

Upgrade and rollback atomically change only the project pin after confirming the installed engine satisfies it; an incompatible target prints the exact package install needed and leaves the current pin usable. Pin history is bounded under `.handsoff/engine-pins.json`. Migration first verifies the old drop-in, then moves its runtime-only files to `.handsoff/legacy-runtime/<version>/` so rollback material remains available. Configuration, source, Git history, criteria, approvals, ledgers, session history, and archives stay in place. A locally modified role prompt is preserved only as an explicit `handsoff-overrides.json` entry bound to its SHA-256; trusted core Python, schemas, and UI assets cannot be silently overridden. If any migration step fails, already-moved runtime parts are restored before the command returns.

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

It opens `http://127.0.0.1:8765`, uses a localhost event stream for near-real-time refresh (with focus and polling fallbacks), and shows phase progress, acceptance coverage, audit integrity, evidence, activity, assigned roles, release readiness, and an E.V.E.-style tactical Supervisor briefing. Mission Control is the Pilot control plane: its state-bound **Pilot Operations** panel exposes the decisions currently valid for design approval/revision, deployment approval/hold, design and implementation review budgets, recovery exhaustion/retry, regression authorization/cancellation, amendments, and explicit pause/resume. The existing Questions, Tranche, lane, agent-matrix, and note panels remain their richer operation-specific surfaces. Every control invokes the same lock-protected Supervisor operation as the CLI, carries an action ID bound to the displayed state, rejects stale/double submissions, audits success, and refreshes through SSE immediately. Agent-only design/review/implementation writes are present in one canonical operation inventory but are never exposed as Pilot mutations. The empty dashboard initializes a mission from feature text plus an optional issue reference, so no terminal acknowledgement or follow-up `a` is needed.

The **Agent Settings** dialog selects `auto`, `codex`, or `claude` plus a runner-default or exact model independently for Supervisor, Architect, Implementer, and Reviewer. A role left unset in `handsoff.toml` launches with the recommended crew (see "Default crew" below) and is labelled `recommended default` in the dialog; an explicit `auto` chooses, at each new role launch, the first installed runnable adapter in the documented order Codex, then Claude Code. Mission Control shows the effective choice either way; selecting Codex or Claude explicitly overrides it per role, and saving the dialog writes every role explicitly. Each role can also have up to eight ordered, explicit Codex/Claude fallback profiles under `[fallback_policy]`; `max_failovers_per_role` defaults to 2 and caps replacement selections without counting the primary. Saving the wrapped settings payload atomically updates `[agents]`, `[models]`, and `[fallback_policy]`. Existing four-role profile and older three-role adapter-only payloads remain valid and leave fallback policy unchanged; the legacy form also preserves the Supervisor adapter/model semantics. Availability means only that the executable was found locally; it does not prove authentication, account entitlement, network access, or model validity. Settings saved during a live session affect future selections only. The pure fallback planner accepts already-classified runtime failures and injected availability, skips unavailable/attempted/non-independent profiles with closed reason codes, and never launches or mutates workflow state. The settings and approval endpoints accept only bounded, exact JSON payloads from the dashboard's own loopback origin and do not enable CORS. Add `--no-open` to start the server without opening a browser, or `--port PORT` to choose another local port. Add `--owned-by-run` when the dashboard is launched for one `/ship-feature` run (`dashboard --no-open --owned-by-run`): the server then writes `.handsoff-dashboard-owner.json` (gitignored) in the project root and `advance 8` with status `complete` shuts it down after the run archive is written, so the port is free for the next run. The persistent LaunchAgent and a manual launch never pass the flag, never write the file, and can never be targeted by a release: the server answers `GET /api/ownership` from the run token and root hash it minted in memory at launch, and `POST /api/shutdown` needs both of them. Note for local development: the dashboard server resolves `dashboard/` and `bin/` relative to the `handsoff_supervisor.py` you actually invoked, not `--root` -- to see edits to the dashboard's own files (`dashboard/*`, `bin/handsoff_dashboard.py`) reflected live, launch it from the copy of this repo you're editing, not a separately installed one.

An owned run dashboard performs immediate managed-role handoffs after a completed role changes the assignment and no Pilot decision is pending. It never manufactures the first session for a manual run, relaunches the same completed role, or retries a non-recoverable failure such as token-budget exhaustion.

Phase-2 Architect and Reviewer launches receive a bounded managed design context containing the current criteria, recorded findings, next action, and latest structured proposal, so they do not rediscover those facts through repository-wide searches or agent-output logs. A successful Architect must emit one `HANDSOFF_DESIGN_PROPOSAL` protocol object; the host validates, persists, hashes, and hands it to the Reviewer. Narration-only completion fails. Follow-up design turns (after at least one recorded design review) use a hard 16k token ceiling even when the configured first-pass role budget is higher.

### Fleet Mission Control

Register each project once, then run one persistent fleet service (port `8765` by default):

```bash
python3 bin/handsoff_fleet.py register /absolute/path/to/project
python3 bin/handsoff_fleet.py serve --port 8765
```

Fleet Mission Control shows every registered run's feature, phase, progress, state, live role/profile, activity, pending Pilot decisions, recovery state, engine version, repository root, and verified run-owned port. Its SSE feed updates all rows without allocating another port per project. **Release Port** uses the existing token-and-root-bound dashboard shutdown handshake and only removes stale metadata when ownership cannot be proven; it never signals an unverified PID. **Cleanly Close Run** requires a reason and an exact confirmation dialog. An active close additionally verifies the current session, fresh beacon, PID, and process-group leader before TERM/KILL, terminalizes the session once as cancelled, clears leases and pointers, audits closure, and releases only the owned dashboard. Source, Git history, ledgers, and archives are never deleted. Non-complete runs can be reopened from the same row. Set `HANDSOFF_FLEET_REGISTRY` to relocate the default `~/.handsoff/projects.json` registry (useful for tests and managed installations). Both Fleet pages and Mission Control carry an `ENGINE vX.Y.Z` badge in the topbar (#161): on Fleet it is the engine the Fleet server itself runs (`/api/fleet` carries `engine`), and a card whose project engine differs is marked with the Fleet version beside its own; on Mission Control it is the engine the run uses. `handsoff dashboard` and `handsoff fleet serve` open the browser through a focus-or-open step (#162): on macOS with Google Chrome, a tab already on that URL is brought forward instead of a second one being added; `--no-open` skips it. The Metrics tab also exists on the go, without the Mac: `docs/METRICS-SITE.md` describes metrics.tonecommand.com, a Cloudflare Pages site fed straight from GitHub by one Pages Function and fronted by Cloudflare Access (#163).

**GitHub and Beakon signals (#152).** Each card carries a strip with two lines. `GITHUB <n> ISSUES · <n> PRS · <tag> <age> · CACHED <age>` is read from the project's `origin` remote (https and ssh forms) through `gh api` when the gh CLI is authenticated, else `GITHUB_TOKEN` over HTTPS; with neither the line reads `GITHUB NOT CONFIGURED` (unauthenticated GitHub access is refused on purpose). `BEAKON <n> IN FLIGHT · LAST <OUTCOME> <age> · CACHED <age>` is read from the local Beakon worker's landing folder (`BEAKON_WORK_ROOT`, else `work_root` in `~/.config/beakon/worker.toml`): one `bk-*` folder is one beam, attributed by the `workdir` in its `task.md` frontmatter, in flight until its `result.json` exists; a machine without a worker shows no Beakon line. Both are collected on a background thread inside the Fleet server, every `HANDSOFF_FLEET_SIGNALS_INTERVAL` seconds (default 300), never inside the one-second snapshot; the cache persists to `fleet-signals.json` beside the registry (`HANDSOFF_FLEET_SIGNALS_FILE` overrides) so a restarted Fleet serves the last values with their original age until its first pass lands. A failed read keeps the last good numbers and shows the error beside them. The signals never change a card's state.

**The Metrics tab (#153, #156).** `/metrics` (the METRICS tab in the topbar) is the TPM view of every registered project with a GitHub origin: a KPI row (closed and opened in range, open now, median and p90 days to close, commits and releases in range), charts for closed per day with a 7-day rolling line, opened per day, open backlog per day, days to close per closed issue with a rolling median, and commits per day; a releases list; on the All view a per-project breakdown (open now, opened, closed, median time to close, commits, releases); and a daily table. Filters are a date range (today, 7, 30, 90 days, all, or a custom pair of local dates) and a project. The data is every issue's created and closed time, commits since `HANDSOFF_FLEET_COMMITS_DAYS` days ago (default 180; earlier days render blank, never zero) and every published release, collected per project on a second daemon thread every `HANDSOFF_FLEET_ISSUES_INTERVAL` seconds (default 900, first pass at start) into `fleet-issues.json` beside the registry (`HANDSOFF_FLEET_ISSUES_FILE` overrides) and served whole at `/api/metrics`; the series are computed in the page, so a filter change never leaves the browser. A project whose read fails keeps its previous lists with the error named on the page. Since v0.3.40 the collector reads conditionally and incrementally (#158): every request carries the previous answer's ETag and a `304 Not Modified` costs nothing against the rate limit; issues are read with `since=` the newest cached update and commits with `since=` the newest cached commit, so those URLs stay stable while nothing changes and answer 304 too; a pass where nothing changed is three free requests per project. A full re-page of the issues runs on the first pass, after an error, and every `HANDSOFF_FLEET_FULL_PASS_HOURS` (default 24) to catch deletions and transfers. The default interval is therefore 60 s with a floor of 30 s, and the page's source note shows the remaining GitHub budget (GITHUB BUDGET n OF 5000). Both collector threads also watch the registry (#157): a `fleet register` or `unregister` is noticed within `HANDSOFF_FLEET_WAKE_SECONDS` (default 5) and collected at once instead of at the next interval. Roots that point at one repository, such as a lane worktree beside its checkout, are collected once per pass and the Metrics tab shows one row per repository naming its roots (#160); the Missions tab keeps one card per root. The mark colors are steps of Fleet's cyan and emerald chosen for the dark band and validated with the dataviz palette script; opened columns use the de-emphasis gray on purpose.

**Who fills each station (#164).** The four role chiclets under NEXT ACTION carry one faint word after the role name, the agent family at that station: the prefix of the recorded actor (`claude`, `codex`) when there is one, else the adapter configured for the role (`host`, `claude`, `codex`, `auto`); hover for the model and source. The reviewer chiclet names the design reviewer in Phases 1 and 2 and the implementation reviewer from Phase 3.

**Live status.** A strip under the header shows what the run is doing right now: a state pill (`IDLE`, `STARTING`, `RUNNING`, `WAITING`, `STALLED`, `STOPPED`, `FAILED`, `COMPLETE`) that pulses while a managed role is active, the role, "last activity N s ago" ticking every second between snapshots, and a one-line detail (for a finished session the state, exit code, and end time). It is fed by `snapshot.live`, the output of `handsoff_lib.live_status`, which folds the run status, an open human pause, the Phase 7 approval wait, the ledger-bound session record, and the liveness beacon `.handsoff-live.json` (gitignored; written every 5 s by `handsoff_agent.py` while a managed child runs, seven keys only: `session_id`, `role`, `state`, `pid`, `beacon_at`, `ended_at`, `exit_code`) into one view. `status` prints the same view as `live`. The beacon is a hint, never an authority: it is accepted only when its `session_id` is the current session's, a running session without a fresh (15 s) matching beacon reads `STALLED`, and a terminal session reads `STOPPED` or `FAILED` from the session record whatever the beacon says. The event feed stays the sanitized stream; no beacon event is ever logged.

**Portable managed-agent output (#58–#60).** Mission Control shows the focused managed session's recent stdout/stderr even when the launching terminal is on another host. The runner line-buffers and redacts output before writing the gitignored `.handsoff-agent-output.json`; protocol messages, assigned-prompt echoes, sensitive environment values, authorization and cookie headers, common key/token/password/secret assignments, JWT/GitHub/OpenAI/AWS token shapes, URL credentials, and private-key blocks never enter it. Chunk boundaries are joined before redaction, oversized unterminated lines and redactor failures produce fixed safe markers, and raw output is never the fallback. Storage is bounded to 160 lines for each of the eight most recent sessions, older entries are released, and terminal sessions retain only that bounded tail. Writes are batched every 250 ms or 20 entries/32 KiB under `.handsoff-agent-output.lock`, never the workflow lock; terminalization performs a final bounded flush. The panel distinguishes `SIGNAL ACTIVE`, `RUNNING · QUIET`, `TRANSPORT DISCONNECTED`, `SESSION FAILED`, and `SESSION COMPLETE`. It is telemetry only: the file is never hashed, logged, or read by a workflow gate, while stdout/stderr still mirror to the launching agent window when attached. SSE watches the side file, so appended batches invalidate the dashboard snapshot within one second; refreshing simply re-renders the cursor-numbered bounded tail without duplicating lines.

**Mission economics (#63).** Mission Control and `/api/dashboard` derive content-free performance telemetry from the audited run: total and per-phase wall time, Pilot/background wait, managed-session duration by role/adapter/model, failures, replacements, recoveries, review attempts, and verification duration. Completed archives retain the same bounded summary for Miner analysis. Token fields remain `UNKNOWN` unless a runner supplies authoritative structured usage; Handsoff never estimates tokens from output length. Historical savings remain `unavailable` until a compatible cohort exists, so the UI never invents an ROI claim.

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

Every managed Codex launch has a hard native rollout ceiling. Defaults are
24,000 tokens for Supervisor, 40,000 for Architect and Reviewer, and 80,000
for Implementer; override them with integer values from 8,000 to 500,000 in
`[agent_budget]` (`supervisor`, `architect`, `implementer`, `reviewer`). The
runner disables optional Codex plugins, apps, browser/computer/image surfaces,
skills search, goals, and multi-agent fan-out for managed roles, leaving the
repository shell/edit surface and OS sandbox in place. Budget exhaustion is a
closed, non-recoverable outcome: the run pauses instead of launching a fallback
and paying the same prompt cost again. A Supervisor that exits without an
exact broker request or structured Pilot question fails immediately rather
than being recorded as successful orchestration. Assigned tasks are capped at
16 KiB and one Supervisor response may dispatch at most eight requests.

`handsoff_agent.py` resolves the executable to an absolute path and never uses a shell. The role prompt and task travel on standard input, not in command arguments. Codex uses an ephemeral `codex exec` session; Claude Code uses headless `claude -p`. `default` omits the model flag. Reviewer and Supervisor launches enforce read-only/plan mode at the runner boundary; Architect and Implementer use explicit workspace-write/accept-edits modes. No bypass-permission flags are generated. Every public `launch` enters the managed recovery path, so an eligible classified failure can move to the next configured same-role fallback profile. Runner output streams directly rather than accumulating in memory; unrecoverable nonzero exits, cancellation, timeouts, missing prompts, and missing executables fail visibly, with timeout/cancellation terminating the fresh process group.

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

A managed role doing legitimate long background work that has no progress to report yet may call `heartbeat --by implementer-1 --session hs-SESSION [--note "what's running"]` periodically. The session must be the role's current live managed session. The timestamp is ignored immediately when that session terminates or is replaced, so a detached heartbeat loop cannot conceal an exhausted worker. Explicit non-agent waits use `background-wait-start`/`background-wait-end`; they remain visible without impersonating a managed process.

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
- **The Architect cannot self-approve its own design into execution.** For any run `init` flagged `requires_design_approval` (every new run, going forward), Phase 3+ requires a `design_approved` record bound to a `design_hash` of the current criteria, naming a human approver distinct from the architect identity that proposed the design; `design-approve` refuses `--by == --architect` (compared case- and whitespace-insensitively) outright, and the gate re-checks the same identity mismatch independently every time it runs, the same double enforcement `_review_errors` already applies to `reviewer != implemented_by`. It also refuses while the registry still holds only `init`'s untouched placeholder criterion. A run from before this feature (no `requires_design_approval` field) is never subject to this gate. Since v0.3.41 a project may waive the Pilot's click with `[workflow] require_design_approval = false` (#159): `init` then writes `requires_design_approval: false`, logs a `design_approval_waived` event naming the key, and Phase 3 advances as soon as the independent design review is approved; that review stays mandatory, the deployment gate is untouched, and the key is hashed into the chain of trust like the other governance settings, only while it holds a non-default value so a project that never sets it keeps every recorded approval valid.
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
- **Completed runs are mined for evidenced patterns, and the analyzer can never loosen a gate (#49).** Landing Phase 8 complete scans the run archive right after the archive write and files improvement tickets through `gh` only for the fixed rules R1 to R6, each with run ids and numbers read from ledger records alone; R7 (Pilot notes) and R8 and R9 (the two patterns whose natural remedy is raising a cap) are report entries only, with no configuration to change that; fixture and `run_kind: test` archives are never mined; a scan failure prints `HANDSOFF_ANALYSIS_FAILED` and never fails the advance. See "Archive analysis".
- **A run-owned dashboard is released on completion, and nothing else ever is (#40).** `dashboard --owned-by-run` mints a random `run_token` and the sha256 of the resolved project root, keeps both in memory, and writes them with the pid and port to `.handsoff-dashboard-owner.json`; a server started without the flag writes nothing and reports `owned: false`. When `advance 8` lands `complete`, `release_run_dashboard` reads only the port and token from that file, computes the root hash from its own resolved root, and asks `GET /api/ownership`; it sends `POST /api/shutdown` (both values, re-checked by the server against its in-memory copies, 403 on any mismatch) only when the answer is owned with the same token and the same root hash, then waits until the port refuses connections and removes the file. A refused connection, an unowned server, a foreign token, another root, or a malformed answer removes the stale file and touches no process; no PID is ever signalled. Open SSE clients close because every event loop checks the server's stop flag, and the server's own exit removes the owner file only while it still carries that server's token. The event log records `dashboard_released` (port, pid, reason) or `dashboard_release_skipped` (reason); a repeated completion is a no-op with the skipped reason, and neither outcome can fail the already-committed advance.
- **Design evidence is measured once and reused only while its inputs are provably unchanged (#38).** Each `[[design_evidence]]` table in `handsoff.toml` names a trusted measurement command and the globs it depends on. `design-evidence run --by ACTOR` executes a command only when no record exists, when the cache identity (`sha256(command + "\n" + canonical JSON of the declared inputs list)`) or the input hash (sha256 over the sorted `(path, file sha256)` pairs the globs match) differs from the stored record, or when the stored run exited non-zero; otherwise the record is reused and the runner is never invoked. Editing a declared file, the command, or the glob list reruns it; editing an undeclared file does not; a new commit leaves the artifact `current` but `commit_matches_head` turns false so the exact commit it was measured at is always visible. `--force` reruns regardless (the reviewer's challenge path). Records bind the command sha, identity sha, input hash, head, branch, and dirty flag, and keep at most 8192 bytes of output with the sha256 and byte count of the full output (`truncated` when cut). `design_evidence_view` reports every artifact as `current`, `stale`, `failed`, or `missing`; the dashboard snapshot and the Architect/Reviewer role input show output for `current` artifacts only, and the `design_evidence_recorded` event carries hashes and metadata, never output. See "Design evidence" below.
- **After the first full design review, a follow-up reviewer gets a bounded delta packet, and only for the exact design it was built for (#36).** `record-design-review` accepts repeatable `--finding "text"` (at most 32 per review, 512 characters each, ids `F<attempt>.<n>`), stores them with the record's `attempt` and the commit `head`, and appends a bounded `design_review_history` entry (last 8: attempt, decision, reviewer, design_hash, head, sorted `criteria_ids`, per-criterion spec hashes, `structural_blocker`, findings). `design-review-packet --by ACTOR [--disposition ID=resolved|rejected|unresolved[:note]]...` (Phase 2 only; refused with nothing written while `design_review_attempts` is 0, so the first review always receives the full task) builds `lib.build_design_review_packet`: a deterministic, canonical, sorted packet with the criteria delta against the last recorded review (`added`/`removed` by id, `changed` by spec hash), every prior finding with its disposition (an omitted finding is `unresolved` with a null note, so silence never reads as resolved; `rejected` requires a note; an unknown, duplicate, or malformed disposition is refused before anything is written), `new_findings_since`, the `design_evidence_view` entries with `stale_for_packet`, the repository identity, and `stale`/`stale_reasons` (set when the previous review's head is unknown or differs from HEAD, in which case `files_changed_since_previous` lists `git diff --name-only`). The serialized packet is at most 65536 bytes: over budget it is trimmed in a fixed order (files first 200 then halved, unchanged ids collapsed to a count, finding text and notes cut to 256, evidence reasons dropped, findings cut from the end), each step recorded in `truncated`, and finding ids, criteria ids, hashes, attempt numbers, repository identity, and `stale_reasons` are never trimmed; `packet_id` is sha256 of the final body, so identical inputs give byte-identical packets. Stored as `design_review_packet` with event `design_review_packet_generated` (packet_id, attempt, design_hash, byte size, counts; never finding text). A managed Phase-2 Reviewer whose stored packet is for `attempts + 1` and the current `design_hash` gets `# Delta review packet` (JSON) in front of its role prompt, and its session records `packet_id` and `design_hash`; a packet for another design or attempt is ignored and the reviewer gets full context. Mission Control shows the packet's attempt, disposition counts, criteria delta, and stale flag.
- **Live session status is derived, never declared, and the session record outranks the beacon (#33).** `handsoff_agent.py` writes `.handsoff-live.json` every 5 s while a managed child runs and once more after the terminal `transition_agent_session`, with exactly `session_id`, `role`, `state`, `pid`, `beacon_at`, `ended_at`, `exit_code`; every write is best effort (an `OSError` is swallowed, the child's lifecycle, the session record, and `execute_launch`'s result are untouched), the file is gitignored, never hashed, never read by a gate, and no beacon event reaches the ledger. `handsoff_lib.live_status` derives `idle`, `started`, `running`, `waiting`, `stalled`, `stopped`, `failed`, or `complete` from structured state plus that file: a beacon counts only when its `session_id` is the current session's (a fresh beacon from an older session reads `stalled` with `process_signal: none`), a running session with a beacon older than 15 s reads `stalled` with "no process signal for N s", a terminal session reads `stopped` (completed, cancelled) or `failed` (failed, timed out, failed to start) with `ended_at` and `exit_code` copied from the ledger-bound record even if the beacon still says running, and no session at all reads `idle` with "no managed process is running". `status` prints the view as `live`; Mission Control renders it in the `#live-status` strip and re-reads the snapshot on every beacon write.
- **A managed agent that is still writing output is never reported silent (#41).** `handsoff_agent.py` captures stdout for every managed role (claude and codex children write their work to stdout), streams it through unchanged, parses it for broker requests only when the role is the supervisor, and on every stdout or stderr chunk notes `.handsoff-output-liveness.json` with exactly `session_id`, `role`, `output_at`, `chunks`, `bytes`: two identifiers, one timestamp, two counters, never content. Writes are rate limited to one per second per session (chunks in between only bump the counters), every `OSError` is swallowed, the file is gitignored, never hashed, never read by a gate, and no output event reaches the ledger; phase, progress, acceptance state, and `updated_at` are untouched by any volume of output. Output, beacons, and explicit heartbeats all count only while their `session_id` is the current live session for that role, so a completed, failed, replaced, or unknown process loses liveness immediately. The dashboard says `Agent active; latest output N seconds ago` for bound output. `activity_view` computes the single activity object consumed by both `status` and Mission Control.
- **Follow-up design checks can go to an economical reviewer profile, and never silently to the wrong one (#37).** With `[agents].reviewer_followup` and `[models].reviewer_followup` both set (one without the other is a config error; both absent means today's single-profile behavior, and the keys are a cost knob outside the governance hash), `lib.select_design_reviewer_profile` picks the tier for the next Phase-2 reviewer launch by a fixed precedence, first match wins and becomes the recorded `reason`: attempt 1 (`first_review`), no follow-up configured (`no_followup_configured`), an unconsumed `design-review-escalate` (`pilot_escalation`), the last review flagged `--structural-blocker` (`structural_blocker`), a criterion id added or removed since the last review's persisted `criteria_ids` (`criteria_structure_changed`; a text-only edit does not count), else the follow-up tier (`delta_check`). Two checks then apply to whichever tier was selected and never switch it: with tiering on, the selected adapter/model must differ from the architect's and the implementer's or the clash is named and the launch refused; and the selected adapter executable must be on `PATH`, otherwise `<tier> reviewer profile unavailable: <adapter> is not on PATH; install it, set fallback_policy.reviewer, or remove reviewer_followup` with no session, event, or status change (a missing follow-up never falls back to primary, a missing primary never falls forward). The reviewer session records `tier`, a `design_reviewer_selected` event carries `tier` and `reason` with the launch, and `record-design-review` stores `reviewer_profile` (`adapter`, `model`, `tier`, `reason`) and `structural_blocker` on the record and its history entry, consuming any open escalation. `design-review-escalate --by PILOT [--note]` is human-only (the broker refuses it), Phase 2 only, one at a time. `status` and Mission Control (`policy.design_reviewer_selection`, rendered as `Review profile: <tier> <adapter>/<model> (<reason>)`) show the latest recorded review's tier and what the next launch selects, including any refusal. See "Follow-up reviewer profile" below.
- **Design rounds are tracked, not just capped.** `advance --new-design-round` (only valid when advancing to Phase 2) increments `design_round` by one from whatever is actually on disk, so the cap fires on organic drift rather than only on a manually over-typed number; `--design-round <n>` remains available as an explicit override for fixtures/recovery. Each increment logs a distinct `design_round_advanced` event (not a generic `phase_advanced`) carrying `design_round`, `previous_design_round`, and an optional `--design-round-reason`, so repeated rounds are distinguishable in `handsoff-events.jsonl`.
- **Design-phase time is explainable, not just totaled (AR8).** A round closes (`design_round_ended`) either when the next one starts or when design is approved. A blocked `advance` to Phase 3 caused specifically by the design gate auto-logs `design_approval_requested` (deduped against repeated polling) -- the state machine noticing the run just started waiting on the human, with no separate command needed; `design_approved` remains the existing "granted" event. `background-wait-start`/`-end` and `human-pause-start`/`-end` are small explicit commands the driving agent calls for a background task or a human question mid-round; `background-wait-start` feeds `last_heartbeat_at` through the exact same path `heartbeat` does (see stall detection above), not a second liveness mechanism. `design-timing` (optionally pointed at any `handsoff-events.jsonl`, including an archived run's, via `--events-file`) reads all of this back into active/background_wait/human_wait seconds per round and per design "episode" (a design phase can reopen after a criterion mutation forces a rollback, per AR-003).
- **Writes are atomic.** Every status write lands in a sibling temp file first, then replaces the real file; a process killed mid-write leaves the old file intact, never a truncated one.
- **The audit logs are tamper-evident and tail-anchored.** Events and verification records are separately hash-chained. A stored chain head detects edited, reordered, and deleted tail records. Every event also binds the exact status and acceptance file hashes, so an unlogged hand edit is detected; `verify-log` checks both ledgers and current state.
- **`handsoff.toml` is actually read.** `status_file`, `acceptance_file`, `event_log`, the round/stall limits, and `[checks].commands` all come from the config, not hardcoded defaults, with sane defaults only when a key is absent.
- **A criterion points at something real.** `verify` persists command, exit code, output hash, actor, timestamp, criterion id, and criterion-specification hash. Manual/browser evidence is an explicit named attestation through `record-evidence`, not an arbitrary string inserted into JSON.
- **Identities and records are typed, not just present.** `implemented_by`, `reviewed_by`, and evidence-id fields must be non-empty strings or null; `review` and `deployment_approved` records must carry a non-empty `by`, a timezone-aware `at`, and an `acceptance_hash`, or the whole state is refused as malformed rather than partially trusted. `init`, `verify`, `record-evidence`, `record-review`, and `verify-live` all reject an empty or whitespace-only `--by`/feature/description before touching disk, so `init ""` cannot brick a project against reinitialization and no command can attach evidence to a blank actor.
- **Evidence is structurally validated, not just cryptographically authenticated.** The hash chain proves a verification record was not altered after it was written; it says nothing about whether the record was ever real. Every loaded record, not only ones this CLI just wrote, is separately checked for a non-empty `by`, a recognized `kind`, a non-empty `criteria` list, and a boolean `ok`; a hand-crafted record that chains and hashes perfectly but claims an empty actor is still refused.
- **`handsoff.toml`'s governance settings are in the chain of trust.** `deployment_requires_explicit_approval`, `require_live_verification`, `require_design_approval`, and the round/stall limits are hashed into every review, deployment-approval, and live-verification record at the moment it is granted. Changing one of those settings afterward invalidates the decisions bound to the old value, the same way changing the acceptance registry already did; `[checks].commands`/`live_commands` and the file paths are deliberately excluded from this hash so routine test-list edits do not need a fresh review.
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

### Host-driven Supervisor and Architect (#78)

`[agents].supervisor` and `[agents].architect` accept the explicit value `host`: the role is driven by the interactive session or person running the CLI, not by a managed session. It is refused for `implementer` and `reviewer`, so independent review and managed implementation keep their meaning. A host role reports adapter `host` with no model in `status`, `doctor`, and Mission Control's settings payload; `handsoff_agent.py launch` refuses it; an owned dashboard's orchestration loop never launches a host role and, under a host Supervisor, chains only Implementer to Reviewer (an Implementer is never launched from a phase advance alone); recovery reports `not_applicable / host_role` and never launches a replacement. A host Architect records its bounded proposal with `design-propose --file proposal.json --by ARCHITECT`, which applies exactly the validation and hash binding of the managed `HANDSOFF_DESIGN_PROPOSAL` line and emits the same `design_proposal_recorded` event with a null session id.

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

### Exhausted design-review budget (#76)

The `record-design-review` call that exhausts the autonomous budget writes the hold itself: status `blocked`, `authorization_hold = design_review`, and the exhausted-budget message as `next_action`. A revised Architect proposal recorded while the budget is still exhausted stays `blocked` with the hold re-armed and a `next_action` beginning `Revised design proposal recorded;` so the run never claims a review is underway that cannot launch. Mission Control shows **Authorize one review** whenever the Phase 2 budget is exhausted and the run is not closed, without depending on a separately recorded hold. `design-review-authorize` clears the hold, permits exactly one launch (the reservation written by the managed reviewer suppresses a second), and is refused on a closed run.

### Evidence is bound to the tree that ships (#77)

Every executed `verify` record stores the `repository_digest` it ran against. `lib.evidence_drift` classifies each automated criterion's latest valid evidence as `current`, `stale` (the working tree differs from the digest the checks ran on), or `unknown` (a record written before v0.3.12). `status` prints the report; from Phase 5 onward `validate`, `advance 7`, `advance 8`, `record-review`, and `deployment-gate --approve` refuse while any evidence is stale and name the exact `verify` command that refreshes it, and Mission Control shows an `evidence_drift` input request instead of offering deployment. Gitignored Handsoff side state (`.handsoff*` except the version pin) never counts toward the digest, so a managed session running after `verify` cannot flag its own evidence. Legacy records never block.

### The Reviewer runs tests without writing to the project (#80)

A managed Codex Reviewer launches with its cwd and `TMPDIR` in a fresh per-session scratch directory outside the project and `--sandbox workspace-write --skip-git-repo-check`; Codex bounds writes to that cwd (verified empirically on codex-cli 0.153.4), so the project tree stays unwritable while linked tests can create their fixtures. The packet header names the absolute read-only root and how to run `git -C` and `cd`-based tests against it. The launcher hashes the repository before and after the session and fails a Reviewer that changed the tree with category `reviewer_modified_project`, discarding its result. `prompts/reviewer.md` enumerates the supported finding codes; an unsupported prefix is recorded as `other` with the text preserved instead of a protocol error. Results carry `tests_executed` (`yes`, `no`, or `unknown`), which `record-review`, `record-review-findings`, `review_attempts`, and Mission Control expose. The broker binds a design Reviewer's result to `status.design_proposal.architect` when the Architect is `host` (#78).

### External operation telemetry and bounded stalls (#67, #68)

A managed agent reports each external call with one stdout line `HANDSOFF_OPERATION: {"operation_id":"op-gh01","dependency":"github_api","operation":"create_issue_comment","state":"started","attempt":1,"timeout_seconds":60}` before the call and one terminal line (`succeeded`, `failed`, `timed_out`, `cancelled`, with an optional `category` from the runtime failure categories) after it. The schema is identifier-only (`operation_id` matches `^op-[a-z0-9]{4,32}$`, `dependency` and `operation` match `^[a-z][a-z0-9_.-]{0,63}$`, integers are bounded, unknown keys drop the record), so prose from prompts or stdout cannot be stored; the runner stamps every timestamp itself. Records live in the gitignored `.handsoff-operations.json`, bounded to 64 operations for each of the 8 most recent sessions, with per-session `protocol_warnings` and `late_telemetry` counters; the file is telemetry only, never hashed, never evidence, never read by a gate. `operation_assessment` classifies the current operation as `waiting`, `timed_out`, `stale`, or its terminal state (terminal beats timed_out beats stale beats waiting) and `dependency_class` maps the failure category to `agent_provider`, `authentication`, `network`, `target_service`, or `engine`. Once an operation has been timed out for longer than `[recovery].operation_grace_seconds` (default 120) the runner re-verifies the fresh beacon and that the child pid is its own process-group leader, terminates only that group, fails the session with category `external_timeout` (recoverable) carrying the dependency and operation, and lists the session's succeeded operation ids in the recovery or replacement packet so the resumed attempt does not repeat them. Mission Control's agent panel shows the focused session's current operation (dependency, operation, elapsed and timeout, attempt and retries, last success) with one visual state per assessment and an honest `NO OPERATION TELEMETRY` state for sessions that never reported. Every role prompt documents the schema with a parseable example (#67). A design-reviewer launch is refused while no design proposal is recorded, and a reviewer result whose host dispatch fails is recorded as `dispatch_failed` with the dispatch error's own message instead of a generic non-zero exit (#84).

### Mission Control is the complete operator cockpit (#72)

The dashboard payload carries `operations.inventory`: one entry per canonical operator-facing operation (the design, deployment, review-budget, recovery, pause, close, and regression decisions plus `launch_role`, `verify_criterion`, `verify_live`, and the three engine lifecycle operations), each labelled `actionable` (with the state-bound `action_id` the existing `/api/operator-action` uses), `unavailable` (with the specific reason from the same gate functions the CLI applies), or `read_only`. Nothing is ever silently absent. **Launch role** posts `/api/launch-role` with the displayed action id, the assigned role, and a task of at most 4000 characters; the server refuses stale, host, live-session, wrong-phase, and proposal-less Phase 2 reviewer launches, audits every request as `pilot_launch_requested` with only the task's sha256, and runs the managed session through `handsoff_agent.execute_with_recovery` as actor `Mission Control Pilot`. **Run checks** and **Verify live** post `/api/verify` and `/api/verify-live`, which run the supervisor CLI as a subprocess with the same actor, refuse double submissions while a verify lock is held (a held flock, not a leftover lock file), and report progress and the latest ledger result per criterion under `operations.verification`. The **Engine** section shows the installed version, source, project pin, compatibility, the exact stable upgrade, rollback, and migrate commands for this root, and dry-run previews; execution is deliberately unavailable while a dashboard is serving the root. `[workflow].auto_handoff` (default true) lets a host Supervisor run `dashboard --owned-by-run` without the orchestration loop launching roles on its own, and the stable `handsoff dashboard` command accepts `--owned-by-run`. Fleet cards carry `dashboard_url` only for an ownership-verified owner record, always as `http://127.0.0.1:<port>/`, and render **OPEN DASHBOARD** from it; a missing, stale, or foreign-root owner shows an explanation instead.

### Design loop convergence, ledger hygiene, and the finished documentation audit (#85, #83, #81)

`prompts/architect.md` now requires a `Data shape:` approach item whenever a criterion introduces a persisted record, protocol line, file, or payload field, and requires a revision to answer every prior finding by number; the managed design context carries `prior_findings` as a numbered checklist and tells the reviewer to judge only unanswered items, so the design loop converges inside the unchanged autonomous budget (#85). Failed `verify` check results keep a bounded, redacted `output_tail` on the ledger so a failed check is diagnosable, while successful results stay tail-less by design; the regression-gate tests record a release plan first; and Mission Control no longer offers **Authorize one review** once the last review is approved and only human design approval remains (#83). The documentation audit accepts `[documentation] files` and `exclude` in `handsoff.toml`, honours an inline `handsoff-doc: intentional` marker (bare or as an HTML comment) on the line before a deliberate reference and lists such references under `suppressed`, runs non-interactively as `handsoff doctor ROOT --docs-only` (one `path:code:detail` line per unsuppressed finding, exit 1 only when any remain, `DOCUMENTATION_OK` otherwise), and `handsoff commands` prints a command reference generated from the installed CLI's own argparse definitions (#81).

### A live process is never presumed lost (#86)

The recovery watchdog treats a session's liveness timestamp as advisory: before classifying a session `worker_silent` it reads the live beacon, and while the beacon names that session and its pid still exists the assessment is `active / process_alive`, so a quiet reviewer, a paused runner, or a laptop that slept never has real work killed and a replacement paid for. A beacon for another session, or a pid that no longer exists, falls through to the timestamp rule as before.

### The runner cannot lose work (#92, #87, #89, #90, #91, #101, #107)

Every parsed protocol result (review, design proposal, supervisor request) is persisted on its session record before dispatch; when dispatch fails or the digest guard fires, the result survives on the session and `session-result-adopt --session ID --by ACTOR` replays it through the canonical record command, marks the session adopted, and clears the replacement pause, which Mission Control shows as `adopted`. The digest guard now lists the changed paths and, for a sandboxed reviewer, treats edits outside its scratch directory as host edits (`host_edited_during_review`) instead of failing the reviewer; `status` warns while a reviewer is live. A Phase 5 reviewer launch is refused before any session is reserved when the original symptom is unresolved or an automated criterion lacks evidence, naming the command to run; an unborn git HEAD is treated like a non-git root. A reviewer, architect, or supervisor that exits 0 without a protocol result is failed as `no_artifact`, recoverable once. Claude read-only roles run with `--output-format stream-json` and no plan mode so a final tool call cannot swallow the verdict; the Claude implementer receives `--allowedTools` generated from `[checks].commands`, `verify`, `record-symptom-resolved`, and `[implementer].commands`, and `doctor` warns when a check command cannot be expressed. `doctor` pre-flights every configured adapter with one bounded round-trip and caches the result in `.handsoff-preflight.json`; a launch refuses an adapter with a fresh failed pre-flight unless `--skip-preflight`; `[adapters]` pins executables; SSL, sandbox, and read-only-home errors classify as `runtime_environment` and never select another provider family. Budgets: per-adapter defaults (codex reviewer 80000), a derived `[agent_budget].followup_design`, and a low-budget warning. `agent inspect` leaves no scratch directory; a completed launch removes its own.
### Approvals survive bookkeeping (#93, #97, #99, #100, #103)

`criterion-update`, `criterion-add`, and `criteria-apply` refuse on a run with a recorded design approval unless `--revoke-approval` is passed; with the flag the event lists exactly what was revoked and `next_action` is recomputed. `handsoff.toml` is not part of the repository digest: a `[checks].live_commands` or budget line never reads as source drift, while a change to `[checks].commands` still stales evidence through the verification config hash stored on every executed record. `design-propose` and `design-approve` warn about tests that match no configured check command. `design-approve` refuses while a required work item has no criteria; `work-item-remove` of a criteria-less item is allowed after deployment approval; `deployment-gate --revoke --by PILOT --reason TEXT` (human-only) returns a run to Phase 7 awaiting approval and appears in Mission Control. `advance PHASE` may omit progress (keeps the current value) and clamps a lower value with a note; `--new-design-round` never lowers it. Design review and approval bind the proposal hash, a new proposal invalidates both with a `decisions_invalidated` event, and `design-propose` records provenance (actor, pid, executable, host session id) that the reviewer-independence check compares against.
### One liveness answer on every surface (#94, #95, #96)

`lib.liveness_view` computes seconds since activity, the process signal, the stall warning, and the recovery assessment from the current beacon (falling back to session liveness, then `updated_at`), and both `status` and `/api/dashboard` serve that one result, so a stall warning can never sit beside a fresh heartbeat and the two surfaces cannot disagree; a warning that disappears is ledgered once as `stall_cleared`, and `metrics.phase_seconds` charges time to the phase in force at each event. While a reviewer session is live, `design_reviewer_selection.current` is rebuilt from the newest live reviewer session, `design_review_attempts` is never reported below the recorded reviews, and `consistency_errors` names any disagreement, which Mission Control shows as a fault banner. The agent output panel replaced its generic quiet message with a closed set of states in precedence order: `completed`, `transport_disconnected`, `stale_heartbeat` (naming the timeout, using the same beacon age recovery uses), `active_output`, `connected_no_output`, each with start time, elapsed time, last heartbeat age, and last output timestamp.

### A managed crew runs from Mission Control alone (#117, #118, #119, #120, #121, #122, #123, #125)

The first field proof on a real thin project (#108, ToneCommand, v0.3.22) reached Phase 8 only with seven manual steps. Each is closed: `init` retires a complete or closed run's ledgers into `.handsoff-archive/<date>-<slug>/` (`HANDSOFF_RETIRED`) instead of refusing (#118); the Pilot console launches every managed role, including the Architect at Phase 1 and the Supervisor whenever it is assigned, and Initialize mission on an owned dashboard with a managed Architect launches it once with the objective (#119); answering a blocking question relaunches the role that asked, with the answer handed over (#120); the Architect records criteria through a `HANDSOFF_BROKER_REQUEST` criteria transaction the host applies, sandboxed roles are told the host records for them, orchestration tasks are role-specific, and an approval with `structural_blocker: true` is refused as a contradiction (#121); full evidence at Phase 4 and a recorded review at Phase 5 hand off to the Supervisor, `advance 5` takes the completed Implementer's actor, and `record-review` rewrites `next_action` so a managed Supervisor advances instead of re-reviewing (#122, #125); a terminal session never blocks a relaunch and a non-recoverable replacement pause exposes `recovery_acknowledge` (#123); every `initialized` and `agent_session_launching` event carries the engine identity and `status` prints `engine_history` (#117). `docs/FIELD-PROOF.md` is the runbook and `tools/run_evidence.py` prints the evidence row from the ledgers.

### Remote access through a tunnel (#124)

`[dashboard] public_origins` and `HANDSOFF_PUBLIC_ORIGINS` name the exact browser origins Mission Control and Fleet accept besides loopback; Fleet links run dashboards on the public host when the request arrived on one. The servers still bind loopback and have no login; `docs/REMOTE-ACCESS.md` covers Tailscale `serve` and Cloudflare Tunnel with an Access policy.

### v0.3.22 field notes: managed Claude launches, installed-engine permissions, reaffirmed reviews (field notes, 2026-09-18)

Eight defects observed on a real thin project during one run from `init` to Phase 8 with the v0.3.22 engine, each with its cause and fix:

1. **Every managed Claude role failed at launch.** Cause: the Claude CLI refuses `--output-format stream-json` under `--print` without `--verbose`, and the adapter pre-flight used a hand-written text-mode argv that never hit the flag. Fix: one shared `lib.claude_argv` (with `--verbose` next to `--output-format stream-json`) builds the argv for both launch paths, and pre-flight now probes each adapter with the launcher's own argv shape (`lib.claude_argv` / `lib.codex_argv`, read-only role, no model override).
2. **Generated implementer permissions used the drop-in script form on an installed-engine project**, which has no `bin/`, so `record-symptom-resolved` was refused. Fix: `lib.implementer_allowed_tools` names the forms that exist for the project (`python3 bin/handsoff_supervisor.py ...` for a drop-in root; `handsoff supervisor ...` plus the resolved absolute console path otherwise), and the implementer's role input lists the exact permitted forms.
3. **A product-tree change after deployment approval voided the review, and re-adopting the same verdict was refused.** Fix, the safe half: `record-review --reaffirm --by REVIEWER` re-binds the latest approved attempt after an evidence-only refresh (unchanged criterion specs), opens no attempt, spends no budget, re-runs every gate, records `review_reaffirmed`, and is refused when the design hash changed, the reviewer differs, an attempt is open, or no approved attempt exists; `session-result-adopt` replays an already-adopted approved verdict as a reaffirmation when the review was revoked. A real post-approval product change still needs a fresh review; that half is deliberately refused as unsafe.
4. **The Phase 5 reviewer launch pre-check missed `automated_and_browser` criteria without browser evidence.** Fix: the pre-check reads the ledger and names every missing kind with the command that supplies it (`verify --criterion ID`, `record-evidence ID --kind browser|manual`).
5. **A `[checks].live_commands` entry with shell operators was accepted until `verify-live`, after deployment approval.** Fix: config load refuses it with verify-live's own message, so `validate`, `status`, `doctor` and every command surface it at configuration time.
6. Review budget accounting: a re-record of an adopted verdict no longer consumes an attempt (covered by 3). Documentation-only findings still count; the cap override remains the Pilot's lever.
7. **Run write-ups inside the project root staled the run's evidence and tripped the documentation audit.** Fix: documented below; put notes under `[digest] ignore` (and `[documentation] exclude` or a `handsoff-doc: intentional` marker for the audit), or keep them outside the root.
8. **Codex reviewers could not run tests that bind a loopback listener.** Fix: workspace-write Codex sessions (the Reviewer in its scratch directory and the Implementer) get `-c sandbox_workspace_write.network_access=true`; read-only Architect and Supervisor sessions do not.

#### Keeping write-ups out of the repository digest

`[digest] ignore` in `handsoff.toml` lists glob patterns excluded from the repository digest that evidence is bound to (a match applies to the full relative path and to any path component). Run notes, field write-ups and gap lists that live inside the project root belong there, so editing them between `verify` and `advance` does not read as evidence drift. The documentation audit is a separate filter: `[documentation] exclude` keeps a file out of the audit entirely, and a `handsoff-doc: intentional` marker on the line before a deliberate release reference suppresses just that finding. Anything not covered by either is simplest kept outside the project root.

### v0.3.25 field notes: the pre-flight probe, the version pin, and the release cadence (field notes, 2026-09-18)

Three defects observed on three thin projects right after the v0.3.22 to v0.3.25 upgrade, each with its cause and fix:

1. **`doctor` reported a working Codex as `unreachable, exit code 1` inside a real project, and a launch within 24 hours would have refused the adapter on that cached result.** Cause: the probe borrowed `MIN_AGENT_TOKEN_BUDGET` (8,000 tokens at prefill weight 1.0) and ran from the project root, where the reviewer-shaped prompt plus the project's context cost 13,008 tokens; Codex answered `OK` and then exited 1 on the rollout-budget error, and the probe judged by exit code alone. Fix: the probe has its own `PREFLIGHT_TOKEN_BUDGET` (24,000), runs from a throwaway scratch directory with the managed Reviewer's exact launch shape (`--skip-git-repo-check`, scratch sandbox), and records `reachable` with the reason `OK before trailing token-budget exhaustion` when `OK` precedes a budget error, the same acceptance the launcher already applies to a complete protocol line (#114). Any other non-zero exit is still `unreachable` with the bounded, redacted reason. `tests/live_doctor_smoke.py` now asserts the installed engine reports Codex reachable on a fresh thin project and on this repository's own root.
2. **An engine upgrade read as source drift on every completed run.** Cause: `upgrade --to` rewrites `.handsoff-version`, and the pin was the one `.handsoff*` file the repository digest deliberately kept. Fix: the pin is excluded from the digest like `handsoff.toml` (#93); the engine identity stays auditable through `engine_history` and the engine recorded on every `initialized` and `agent_session_launching` event. The documentation audit still flags an exact release reference in an instruction file, and editing that file is a product-tree change, so INSTALL.md now tells thin projects to name the compatible line `0.3.*` in `AGENTS.md`, `SKILL.md` and restart prompts, or to list those files under `[digest] ignore` (below).
3. **Three patch releases in nine minutes, two of them logo-only, each costing a pin bump per project.** Cause: the upgrade runbook showed the exact-pin bump as the routine path even though `init` defaults to `0.3.*`. Fix: INSTALL.md documents the compatible line as the default (an exact-pinned project is moved to `0.3.*` once), keeps the exact bump as the strict-reproducibility exception, `docs/FIELD-PROOF.md` follows, and the release procedure and cadence rule are written down below: a release is cut when behaviour, prompts, schemas or the runtime manifest change; a cosmetic-only change to the dashboard rides with the next such release.

### v0.3.31 field note: a revised criterion could never pass amendment review (#140, 2026-09-18)

`amendment-revise` records the cumulative delta as a history, so revising a criterion the open amendment already changed appends a second operation record for the same id. `recompute_amendment_hash` then compared EVERY record's resulting hash with the registry and refused the review with "no longer matches the reviewed amendment" although nothing had drifted. It now checks the last record per id; the full history still feeds `amendment_hash`, so a delta that drifted after review is still refused. Found on the first real revise against the same criterion (ToneCommand, HeadRush lane); the existing test revised twice and only asserted that approval fails for want of a review.

### Project artwork on Mission Control and Fleet

A project may carry its own logo: `[project] logo = "docs/img/logo.png"` in
`handsoff.toml` (a png, jpg, webp or svg inside the project, under 4 MiB),
or one of the conventional paths (`logo.png`, `logo.svg`, `docs/img/logo.png`,
`ui/logo.png`, `assets/logo.png`, `static/logo.png`, `site/logo.png`) when the
key is unset. The run dashboard shows it beside the mission title and serves it
at `/project-logo`; Fleet shows it on the project's card through an opaque
`/project-logo/<key>` route that never reveals the path. No logo, a missing
file, a wrong type or a file outside the project simply means no logo; branding
never blocks a run. Both dashboards carry the Handsoff mark in their header.

### v0.3.32 field notes: work-item tombstones, amendment reviewers, the phantom selection fault (#141 #142 #144 #145, 2026-09-18)

Four defects from ToneCommand's three runs of 2026-09-18, each with cause and fix:

1. **A removed work item came back on every criteria transaction (#141).** Cause: `derive_work_item_registry` seeds items from the feature title's `#N` refs and `work-item-remove` left no trace, so `criteria-apply`, `amendment-open` and `amendment-revise` all re-added it, three times in one run. Fix: the removal is a tombstone (`acceptance.removed_work_items`, `{id, by, at}`) that derivation honours; the two deliberate ways back, `work-items-sync --item` and a criterion tagged `[#N]`, delete the tombstone in the same commit that re-adds the item.
2. **A managed reviewer's verdict during an open amendment read as a failure (#142).** Cause: the broker only knew `record-review`, which the amendment freeze refuses, so the dispatch failed and the session was marked non-recoverable, with a phase attempt spent. Fix: `handsoff agent launch reviewer --amendment <id>` records the id on the session; the broker dispatches its verdict as `amendment-review` when, and only when, that amendment is open, its hash recomputes clean, the session started after it opened, and its criteria and work items are still in the run; no attempt is opened and no budget spent. A reviewer launched without the flag while an amendment is open is refused with the flag named.
3. **`test_fleet.ProjectLogoTests` broke when this repository declared its own logo (#144).** Cause: fixture projects copy the repo's `handsoff.toml`, logo line included. Fix: `normalize_fixture_config` drops `[project] logo`.
4. **Mission Control said "live reviewer session ... has no selection metadata" on every design review (#145).** Cause: `design_reviewer_selection_view` checked a status key nothing wrote. Fix: the Phase 2 launch persists `design_reviewer_selection.current` in the commit that logs `design_reviewer_selected`; a genuine mismatch (another session id or actor) still reports.

Also in this release: Fleet cards show `STARTED` and `FINISHED` wall-clock stamps beside the elapsed clock (#143), and the card's close control reads `CLOSE RUN` (cleanly was implicit).

### v0.3.33 field notes: amendment verdicts by binding, banners by turn, live verification in view (#146, #147, #148, #149, #150)

- **#146** A reviewer session launched for an open amendment dispatches `amendment-review` whatever `kind` the reviewer wrote, with every finding on a request-changes verdict; `session-result-adopt` takes the same path for a persisted verdict. `amendment-review` gained `--finding` (at most 32, 512 characters each; never on an approval) and `--adopted-session`/`--adopted-by`; the review record carries `findings`, `adopted_session` and `adopted_by`.
- **#147** `input_required` carries `turn` (pilot, reviewer, architect, supervisor), `preauthorized` (the newest human `pilot_note` whose text says pre-authoriz..., only for the Pilot's own turn) and `amendment_round`. The briefing label and headline are derived from them (Under independent review, Architect revising, Pilot approval needed, Pre-authorized by pilot note); the amber demand styling, title flip and browser notification fire only for the Pilot's own turn; Fleet reports `waiting` only then.
- **#148** `verify-live` keeps `.handsoff-live-inflight.json` (side state, outside the digest) while it runs, through `run_checks(on_progress=...)`. The snapshot's `verification.live` carries `in_flight` and `last_failure`; the Phase 7 display name reads LIVE VERIFICATION RUNNING or LIVE VERIFICATION FAILED instead of ready to ship; Fleet reports `running` or `failed` accordingly; the Phase 7 card lists the failing command with its output tail until a later live run passes.
- **#149** Fleet has its own inline SVG favicon (a constellation of mission dots in Fleet's accent); run dashboards keep the state-coloured canvas icon.
- **#150** Fleet lists ongoing runs in the grid and completed or closed runs in a collapsed section below, counted, remembered per browser. **#154** (v0.3.36): a registered project with no run reads `idle` and sits in that section too (COMPLETED, CLOSED AND IDLE); `quiet` is an initialized run with nothing moving.

### v0.3.34 field note: a dashboard whose server is gone, or whose run is closed, looks that way (#151)

When `/api/dashboard` (or `/api/fleet`) stops answering, the page sets `body.is-offline` within one poll interval: every CSS animation and transition stops, the mission dims, the stepper's active node is held, and a banner reads DASHBOARD OFFLINE since HH:MM: the server on this port is not answering; the first successful fetch clears it. A run with `run_closed` renders closed on the server side: the current phase's state is `closed` (never `active`), `status.phase` reads Run closed, the briefing reads Mission closed by WHO: REASON, no role is active. Fleet reports `offline` (counted, card dimmed, badge DASHBOARD OFFLINE) for a moving run whose owner record names a dashboard that does not answer. A completed or closed run whose dashboard goes away (advance 8 releases the owned port) is not an outage: the page keeps its celebration and a calm banner says the dashboard was released at HH:MM and this is the final snapshot (#155, v0.3.38). Fleet's own page polls every 5 seconds and refreshes once on a stream error, so a Fleet server dying under an operator is noticed the same way (v0.3.35). `tests/live_offline_smoke.py` proves both pages through headless Chrome's own DOM against the installed engine (`--tree` serves this checkout instead), and runs first in `live_commands`.

### v0.3.37 field note: Fleet cards carry GitHub and Beakon signals (#152)

Cause: Fleet showed a project's Handsoff state only; what was open on GitHub and which beams the Beakon worker had in flight for it lived on two other screens. Fix: `bin/handsoff_fleet_signals.py` collects both per registered project on a daemon thread the Fleet server starts (first pass immediately, then every 300 s), swaps the results into one `SignalCache` under a lock and persists it beside the registry; `build_fleet` merges `github` and `beakon` from that cache and never fetches. GitHub is read through `gh api` (or `GITHUB_TOKEN`), three calls per project: open issues, open pull requests, latest release. Beakon is read from the worker's landing folder, so it needs neither the gateway nor a token. A per-project read failure keeps the previous good values with the error text; a first-ever failure shows the error with null counts. The card renders `GITHUB ...` and `BEAKON ...` lines with both cache ages; `tests/live_fleet_signals_smoke.py` proves the installed Fleet serves the strip and a release tag that matches `gh release view`, and runs first in `live_commands`. Daily throughput per project (issues closed per day, time to close) is #153.

### v0.3.39 field note: Fleet has a Metrics tab (#153, #156)

Cause: Fleet answered "what is happening now" and nothing about progress over time; how many tickets closed, how long they took, how many commits and releases landed lived in GitHub's own screens, one repository at a time. Fix: `bin/handsoff_fleet_signals.py` gains `fetch_issues`, `fetch_commits` and `fetch_releases` plus an `IssueCache` (same lifecycle as the #152 signal cache: one file beside the registry, load once, swap whole under a lock, a failing project keeps its previous entry with the error; the three reads for a project succeed together or not at all), refreshed by a second daemon thread the Fleet server starts after it binds. `/api/metrics` serves the cache with `started_at` and `refreshed_at`, only for roots still registered and only when the cached repository is still the project's origin. `fleet/metrics.html` and `fleet/metrics.js` render the tab under the existing CSP with hand-built SVG (attributes and classes only, no library, no inline styles): local calendar days, DST-safe day ends, nearest-rank p90, commit days before the collector's window shown as unknown rather than zero. `tests/live_fleet_metrics_smoke.py` proves the installed Fleet serves the tab, that its own collector refreshed since it started, and that the project-handsoff issue count equals gh's open plus closed totals; it runs first in `live_commands`.

### v0.3.40 field note: the Metrics collector reads GitHub conditionally (#158)

Cause: every Metrics pass re-paged every issue, commit and release of every project, about twenty counted requests, which made a one-minute interval wasteful and anything faster unsafe. Fix: `conditional_reader()` in `bin/handsoff_fleet_signals.py` reads through `gh api -i` with `If-None-Match` (a 304 exits 1 in gh but its status line and headers are parsed; the token path uses urllib), and `collect_project` keeps per-entry ETags, `updated_since` and `full_pass_at`: incremental issue reads merge by number, commit reads by sha inside the bounded window, releases stay one read behind their ETag, and a project's three reads still succeed together or its previous entry is kept whole. A pass with nothing changed is three 304s and zero counted requests (measured on the four registered repositories: pass three answered 12 requests, 0 counted, budget unchanged). The default `HANDSOFF_FLEET_ISSUES_INTERVAL` is 60 with a floor of 30; the entry carries `requests_total` and `requests_counted`, `/api/metrics` carries `rate_limit`, and the page shows the budget. `tests/live_fleet_metrics_smoke.py` now watches a second pass land and fails unless it counted nothing or names what changed.

### v0.3.41 field notes: the design click as config, prompt first collection, one Metrics row per repository (#159, #157, #160)

- **#159** Cause: every `init` flagged `requires_design_approval: true`, so Phase 3 always waited for a Pilot click after the independent design critique had already approved, which for a Pilot who trusts the critique is a bottleneck with no information in it. Fix: `[workflow] require_design_approval` (default true) in handsoff.toml; with false, `init` writes the flag false and logs `design_approval_waived`, Phase 3 advances on the approved design review alone, the review stays mandatory, and the key is hashed into the chain of trust only while non-default so existing approvals keep their hash.
- **#157** Cause: a newly registered project waited a full signals (300 s) or metrics (60 s, formerly 900 s) interval before its first collection. Fix: `start_refresh_thread` samples the registry's (mtime_ns, size) before each pass and every `HANDSOFF_FLEET_WAKE_SECONDS` while waiting; a change starts a pass within one wake period of the later of the change landing and the running pass ending.
- **#160** Cause: rows on the Metrics tab were per registered root, so a lane worktree beside its checkout showed the same repository twice and cost double reads. Fix: `IssueCache` collects each repository (owner/repo, case-insensitive) once per pass and serves equal entries to every root sharing it; `/api/metrics` stays one entry per root; the page groups by repository for the filter, the breakdown and the totals.

### v0.3.42 field notes: the engine badge, and one tab per dashboard (#161, #162)

- **#161** Cause: Mission Control named the engine only in a small grey eyebrow (UNKNOWN until the snapshot) and the ENGINE console pane; Fleet named it only inside each card's meta line and never for the Fleet server itself. Fix: one `ENGINE vX.Y.Z` badge in the topbar of both pages, UNKNOWN until data arrives; `/api/fleet` and the event stream carry `engine` (read once from the engine's own manifest); a card whose project engine differs from the Fleet engine is marked `engine-drift` and its meta reads `ENGINE vA (fleet vB)`.
- **#162** Cause: launchers opened the dashboard with macOS `open -a "Google Chrome" URL`, which always adds a tab even when one is already on that URL (measured 1, 2, 3 across three calls), so a run on a reused port showed twice. Fix: `lib.open_dashboard_url` runs one AppleScript that brings an existing tab forward or opens the location, answering `found` or `opened`; `webbrowser.open` is the fallback only when osascript could not start or answered neither word; both servers use it unless `--no-open`.

### v0.3.43 field note: the Metrics tab on the go (#163)

Cause: the Metrics tab needed the Fleet server on the Mac, although everything it shows comes from GitHub. Fix: `web/` holds a Cloudflare Pages site (`metrics.tonecommand.com`): one Pages Function (`web/functions/api/metrics.js`) reads the configured repositories with a token that lives only in a Pages secret and answers Fleet's `/api/metrics` shape, cached at the edge for 60 s; the page is `fleet/metrics.js` and `fleet/styles.css` copied unchanged plus a phone-first stylesheet; `.github/workflows/metrics-site.yml` deploys it on push; Cloudflare Access with the Pilot's email fronts the hostname because two repositories are private. `tests/live_metrics_site_smoke.py` runs first in `live_commands` and proves the deployed host redirects unauthenticated requests to Access.

### v0.3.44 field note: the role chiclets say who fills the station (#164)

Cause: the chiclets named the station only; which agent family held it was three panels away in CREW. Fix: `roleWord(role, snapshot)` in `dashboard/lib/dashboard-logic.js` (the recorded actor's `claude-`/`codex-` prefix, else the configured adapter, else nothing) and `roleTitle` for the hover; `renderRoleChiclets` appends one faint lowercase word; the reviewer chiclet reads the design reviewer in Phases 1 and 2 and the implementation reviewer otherwise. `tests/dashboard/role_words.test.js` runs the real renderer against a stub DOM for all four chiclets.

### v0.3.45 field note: the engine is named once

Cause: after #161 put the ENGINE badge in the topbar, the older grey eyebrow next to the project name still printed the same version and source, so the engine appeared twice on one screen. Fix: the eyebrow carries the project name only; `app.js` writes the version to the badge alone. Cosmetic, shipped on its own because the doubled line was in front of the operator.

### v0.3.46 field note: one logo, and the mission column keeps its name

Cause: on Handsoff's own runs the project artwork is the engine's brand mark, so the topbar showed the same logo twice; and between 1000 and 1240 px the mission column (`minmax(0, 1fr)`) collapsed to nothing, its project logo spilled under the phase pill and `MISSION COMPLETE` was drawn over it. Fix: `project_logo()` skips artwork whose bytes equal `dashboard/logo.png`; `.topbar-mission` clips its overflow and its copy keeps a 120 px floor, while below 1240 px the sync stamp hides and the pilot note input narrows first. Note for maintainers: after editing any runtime file, regenerate the manifest before running the Python suites; the fixtures recognise their engine copy by the manifest hash and otherwise fall back to the pin path with dozens of unrelated failures.

### v0.3.47 field notes: failing first, launch rules, one ticket one run (#165, #167, #166)

Cause: three losses from the 2026-09-19 runs. A green test with no red behind it was accepted as evidence; a reviewer launched at Phase 1 lost a whole Codex verdict to `dispatch_failed`, and a packet with a command line in `tests_executed` lost another to `orchestration_noop`; two sessions ran the same five tickets on two ports until the operator compared browser tabs. Fix: the three behaviours under "Workflow features" above, each behind a `[features]` switch that Mission Control edits. Two things learned while building: the fixtures recognise their engine copy by the runtime manifest hash, so every runtime edit is followed by `python3 bin/handsoff_manifest.py --version vX.Y.Z` before the Python suites run; and `init` now registers the run itself, so every fixture sets `HANDSOFF_FLEET_REGISTRY` (the base test case does) or the operator's own register fills with temp roots.

### v0.3.48 field notes: the board closed (#168, #169, #170, #171, #172)

Cause: five open tickets, one filed by the Pilot during tranche 1 (a run that had adopted a failed session's verdict showed FAILED on Fleet). Fix: the five sections above, three more `[features]` switches, and one repair found on the way: the runner persists every reviewer result as kind `review`, so a design verdict recovered from a refused packet adopts through the design branch now. Learned: the design gate must not compare the rules set (a Phase 6 hook edit would demand a new design); it records, the review compares.

### v0.3.49 field notes: CI on the board (#181)

Cause: with `main` protected (#178) every lane waits about two minutes on
the pull request's checks (#180), and the run sat at Phase 6 looking idle
while the operator watched a GitHub tab. Fix: the section "CI is a step of
the run" above; `ci-watch`, the CI row, and the Phase 7 gate on a red
check. Found on the way and filed, not fixed here: a missing
`.handsoff-version` pin takes the whole page offline instead of one badge
(#185); the operations console prints 21 rows that cannot act (#183,
#184); the page says "host" where it could say which host (#186).

### v0.3.51 field notes: a lane cannot trip on the floor (#185, #190)

Cause: two runs on 2026-09-21 stopped four times for reasons that were
not the code. A worktree without `.handsoff-version` made every
`/api/dashboard` request raise, so the page read DASHBOARD OFFLINE while
the server was up; `live_offline_smoke` printed one word when the host's
`python3` lacked `websockets`; after the release was installed, `advance 8`
refused "the rules set changed (engine:version)" while `record-review
--reaffirm` refused "nothing to reaffirm"; and a `[checks]` test asserted a
literal engine version. Fix: the snapshot degrades the engine badge to
UNKNOWN with the reason on the audit strip and `init` writes the missing
pin; the smoke names the interpreter and the fix, and `websockets` is the
engine's `live` extra; reaffirm re-binds a current review whose rules set
changed and the ledger names what changed; the test reads the manifest.
The landing order below is the order this release was landed by.

### v0.3.52 field notes: the page says what matters (#186, #183, #184, #181)

Cause: two hosts ran lanes side by side and the page said "host" for both;
the console listed 21 rows that could not act; fixed copy, an empty card
and a grid of token dashes filled the rest; and the new CI row showed one
run's queue time as the estimate, a timer that jumped once a poll, and a
red watch a rerun could not clear. Fix: the three sections above.

### v0.3.53 field notes: the suite means the whole tree (#179, #176, #177)

Cause: CI ran one file of 54; the runner reported a broken pipe where a
child had simply exited (three red shards in five pull requests); five
modules were red on `main` for reasons no lane had touched (a shared fleet
register, the dogfood config inherited by a fixture, a review not
reaffirmed after a config edit, a README wording, the checkout's own run
refusing a fixture's launch); the Phase 8 implementer gate fired on one
run and not another; and the Architect had no way to say a change is not
needed. Fix: the sections "What CI runs", "One implementer rule for every
run" and "The Architect can decline" above. Open on #177: the runner does
not yet dispatch `HANDSOFF_DESIGN_DECLINE` from a managed Architect and the
design reviewer does not yet review a decline. Filed on the way: #193, the
clocks count the hours the Mac was asleep.

### v0.3.55 field notes: the board says who it waits on (#194, #198, #199)

Cause: a run whose host had stopped for eight hours after the Pilot
authorized a review attempt read "telemetry stalled"; the CI row had a bar
and no number; the crew chiclets named the family but not whether the
station was the host or a managed session. Fix: the host-wait line and
the Fleet tag, percent complete on the CI label, `host` or `managed` on
every chiclet.
### v0.3.56 field notes: the clocks know the Mac slept, and a decline is reviewed (#193, #177)

Cause: two runs read "design debate for 7 hours" while the machine was
asleep; #177's decline closed a run without the reviewer's word and a
managed Architect had no way to emit it. Fix: the sections "The clocks
know the Mac slept" and "The Architect can decline" above.

### v0.3.57 field note: the sleep log is read off the request path (#193)

Cause: `pmset -g log` is 33,000 lines and takes three seconds on this
Mac; v0.3.56 read it on the first snapshot, so every new dashboard's
first page waited that long and `live_offline_smoke` found the page not
yet rendered. Fix: the log is read on a background thread into the
per-process cache; a request before the first read lands sees no sleep
(wall clock) and the next one sees the intervals; nothing on the request
path waits for it.

### v0.3.58 field note: a stale manifest names its fix (#204)

Cause: three times in one day a host edited bin/ or prompts/ and launched
a reviewer or ran verify before regenerating the manifest, and read
"override not declared" or "runtime files do not match; reinstall the
engine", both pointing at the wrong place; a knowledge-base rule did not
stop the third. Fix: `stale_manifest_refusal` and the one line above,
from every path that reads the manifest.

### v0.3.59 field note: the badge reads the stale manifest too (#204)

Cause: v0.3.58 claimed every reader refused, and the dashboard's engine
badge did not: the check sat only on the missing-pin path, so with a pin
present (every initialised project) an edited runtime file left the badge
reading healthy; the acceptance named the engine view and no test
exercised it. Fix: the check runs on every read of the runtime identity;
the badge reads ENGINE UNKNOWN with the reason (#185) and Fleet reads
unknown; the test now covers both. Lesson, in the knowledge base: a claim
of "every reader" needs a test per reader, named in the criterion.

### v0.3.60 field note: the playbook ships with the engine (#208)

Cause: the rules for running a lane lived in one project's local,
gitignored knowledge base on one machine; a host on the Studio would start
without them. Fix: `playbook/` in the wheel, `handsoff playbook`, and the
briefing carrying it on every launch.

### v0.3.61 field notes: forgotten roots, and the reviewer blamed for a temp file (#207, #203)

#207. Cause: `init` registers the lane's worktree with Fleet; nothing
removed the entry when the worktree went, so nine ORPHANED cards stood
after one day of lanes. Fix: a root that is gone is ORPHANED for one Fleet
pass (`missing_since` on the entry), then forgotten with one
`fleet_entry_forgotten` line in `~/.handsoff/fleet.log` naming its last
state and whether the run was never closed; a transient absence clears
the mark; the card carries FORGET (`/api/forget`), never Close Run.

#203. Cause, found by running the module under an instrumented digest:
the runtime's own atomic writers leave `..handsoff-live.json.tmp-<pid>-<hex>`
on disk for a few milliseconds, and `..handsoff` escaped the `.handsoff`
exclusion. The reviewer's tree was scanned twice (paths, then digest);
a scan that landed in that window saw a file the other did not, and the
managed reviewer was blamed for a tree it never touched. Every CI sighting
named a sibling test's `PROBE.py` only because the runner's log interleaves
that test's output. Fix: in-flight Handsoff temp files are excluded, both
views come from one scan, the failure record says what was seen (path,
appeared/vanished/changed, mtime, seconds after session start), and the
post-exit reader drain has its own 60 s budget instead of the 5 s join.

### v0.3.62 field note: the Miner leaves the engine (#174, lane 1)

`bin/handsoff_analyzer.py` is gone; the archive scan is `monzta1/miner`
v0.1.0 (its release wheel, `MINER_RELEASE`), installed beside the engine. The Phase 8 trigger and
`analyze-archives` call `miner scan` / `miner propose-rules` and read the
report; without a Miner the trigger prints `HANDSOFF_ANALYSIS_SKIPPED` and
the command refuses with the install hint. The shim stays for one release.
Lesson: an extracted component's callers get a fake on PATH in the engine's
tests and a live smoke against the real install, never a copy of the code.

### v0.3.63 field note: the Miner beside a framework Python (#174)

`verify-live` on v0.3.62 refused the shim on the very machine it was built
on: inside the dedicated venv, macOS's framework build reports the
framework binary as `sys.executable`, so "beside the interpreter" was not
`venv/bin`. The lookup now reads `sys.prefix/bin/miner` first. Lesson: a
live smoke that runs the installed pair is the only proof of an install
path; the unit tests had a fake on PATH and could not see it.

### Cutting a release

Every release is a wheel attached to a GitHub release whose tag matches `pyproject.toml` and `handsoff-runtime.json`. The steps, in order, with `vX.Y.Z` the release being cut:

1. Set `version` in `pyproject.toml` to `X.Y.Z`.
2. Regenerate the runtime manifest after the last runtime-file edit: `python3 bin/handsoff_manifest.py --version vX.Y.Z` (it rewrites `handsoff-runtime.json`; a wheel built from a stale manifest makes `doctor` refuse every project). The manifest covers `rules/` too. In this checkout the engine enforces it (#204): once a file the manifest lists changes, every ledger command, the reviewer launch, `doctor` and the dashboard refuse with `the runtime manifest is stale (<files> changed after it was written): run python3 bin/handsoff_manifest.py --version vX.Y.Z, then retry`, so a reviewer never reads a tree the manifest does not describe.
3. Point the wheel references in `INSTALL.md` and this README at `vX.Y.Z`; the rollback example in `INSTALL.md` keeps its older release under its `handsoff-doc: intentional` marker.
4. Commit as `Bump to vX.Y.Z` (or fold the bump into the feature commit, as the field-note fixes do), then `git tag -a vX.Y.Z -m "vX.Y.Z: one-line summary"`.
5. `git push origin main` and `git push origin vX.Y.Z`.
6. `python3 -m pip wheel --no-deps -w dist .` (the checkout's own `build/` folder shadows the `build` module, so `python3 -m build` fails here) and `gh release create vX.Y.Z dist/project_handsoff-X.Y.Z-py3-none-any.whl --title vX.Y.Z --notes "..."`; the asset URL is the one `INSTALL.md` prints.
7. Upgrade the dedicated environment per `INSTALL.md` ("Clean patch upgrade") and confirm with `handsoff version --json` and `handsoff doctor` on a thin project. Projects on `0.3.*` need nothing else.

#### What CI runs

`.github/workflows/ci.yml` runs on every pull request and every push to
`main`: six `python (shard i of 6)` jobs deal the classes of
`tests/test_handsoff_supervisor.py` (`tests/shard.py`, weights in
`tests/shard_weights.json`), four `modules (shard i of 4)` jobs deal every
other `tests/test_*.py` module, one process per module (`tests/shard.py
--modules`, weights in `tests/shard_module_weights.json`; the scripts that
are not unittest modules are named in `MODULE_SCRIPTS` and never silently
skipped), and `dashboard` runs the node suite. The required check `tests`
gathers all three, so `main` cannot take a change that reddens any module
(#179). Refresh the weight files from a run's log when a shard drifts past
the others.

#### Landing a lane in this repository, in order

`main` is protected (required check `tests`, strict), so a lane lands
through a pull request, and the live checks compare the installed engine
with the checkout. The order, with the refusal each step prevents:

1. `advance 6`, push the branch, `gh pr create`, `ci-watch --pr N` (#181),
   `gh pr merge N --auto --merge --delete-branch`; wait for MERGED. Stay in
   the worktree: its tree is what `main` now holds, so the evidence stands.
2. `advance 7`.
3. Cut the release from the merged commit: steps 1 to 6 above, with an
   ANNOTATED tag on that commit (`git tag -a vX.Y.Z <sha> -m vX.Y.Z`; a
   lightweight tag fails `live_release_smoke`, and deleting a tag under a
   published release turns it into a draft).
4. Install it (INSTALL.md "Clean patch upgrade") and kickstart the Fleet
   LaunchAgent. Until this step `verify-live` refuses: "installed engine is
   X, this checkout is Y: install the release first".
5. `verify-live --by <pilot>`; the Fleet signals cache turns over within a
   minute of the release, so a "latest release reads X, gh says Y" line
   means wait one minute and run it again.
6. `work-item-update`, then `advance 8 100`. If it refuses "the rules set
   changed since it was recorded (engine:version)", the review was
   recorded under the previous engine: `record-review --by <reviewer>
   --reaffirm ...` re-binds it (#179) and `advance 8` passes.
7. Close the issue with the result per acceptance line, `run-close`,
   archive the ledgers, remove the worktree.

Cut a release when behaviour, prompts, schemas or the runtime manifest change. A cosmetic-only change (dashboard markup, styles, assets) rides with the next such release; every project on the compatible line picks it up then without a pin bump.

## Workflow features

Three behaviours added in v0.3.47 are each a switch under `[features]` in
`handsoff.toml`, edited from Mission Control's CREW dialog (the WORKFLOW
FEATURES group, its own SAVE FEATURES button, written through the same
locked, re-parsed path as the agent matrix, audited as a `features_updated`
event). A switch at its default keeps every recorded review, design
approval and deployment approval hash byte for byte; a flipped switch is a
policy change and revokes them, like any `[workflow]` key.

```toml
[features]
failing_first = false        # #165, default off
launch_rules = true          # #167, default on
ticket_lock = true           # #166, default on
token_accounting = true      # #168, default on
review_binds_rules = true    # #170, default on
report_posting = false       # #171, default off
```

### Failing first (#165)

A criterion's own commands must be seen to fail on the tree before the
feature, or the later green run proves nothing. `verify --criterion REQ-n
--expect-fail --by ACTOR` runs the bound commands now and records a
`baseline` run: valid (`ok: true`) when every command exited non-zero,
`baseline_invalid` when any passed. The record is bound to the criterion's
spec hash and the repository digest like any run; it never changes the
criterion's state and never satisfies the checks requirement. With the
switch on, the Phase 6 evidence gate and the 95% progress gate refuse an
automated criterion that reads passing with no valid baseline behind it
(`baseline gate: REQ-n passed without a recorded failing run`). A test born
with the feature in the same commit has no earlier tree to fail on:
`criterion-add`/`criterion-update ... --baseline not_applicable
--baseline-reason TEXT` declares that, reason audited, and `--baseline
none` clears it. Mission Control shows `red <date>` or `no red: <reason>`
beside the pass on the criterion row.

### Launch rules (#167)

The engine learns from its own run history. `rules/*.json` in the runtime
manifest hold one rule each: `id`, `cause` (the archived run, event and
date it came from), `when` and `refuse`. Launch rules (`when.command =
"launch"`, with `role`, `phase_in`, `amendment`) are evaluated in
`build_launch_spec` before an adapter is resolved or a session reserved,
so a refusal costs no attempt and no tokens; the launching event records
`launch_rules: evaluated` or `disabled`. Packet rules (`when.command =
"packet"`, `field`, `allowed`, optional `recover_as`) are evaluated in
`parse_reviewer_result`: a value outside `allowed` is refused naming the
field and the value seen, and when the rule says `recover_as` the packet
is kept on the session with that field repaired so
`session-result-adopt --session ID --by ACTOR` can re-record the verdict
deliberately. A packet rule may instead carry `max_chars` with `recover =
"truncate"`, for a list field whose items have a bound. The first three
rules are real losses: a reviewer launched at Phase 1 (no design gate to
receive a verdict) and a `tests_executed` that was not the bare word, both
2026-09-19, and a finding of 700 characters that cost the tranche-2
implementation verdict on 2026-09-20. A project may add its own
under `handsoff-rules/`. `analyze-archives --propose-rules` scans the
archive for a failure shape (category, role, the event before the launch)
that repeats across two or more runs and writes a draft under
`rules/proposed/`; nothing there is evaluated until a human moves it up
and commits it. `rules/README.md` has the format.

### One ticket, one run (#166)

`init --item "#NN ..."` takes an exclusive lock beside the fleet register
(`~/.handsoff/projects.lock`), checks every registered run that is not
closed or complete for `#NN`, and registers the new run with its ticket
numbers in the same transaction, so two concurrent inits on one ticket end
with exactly one owner. A refusal names the owner's root, phase, port when
its dashboard answers, and the age of its last event. `init --adopt` takes
a ticket over only from an owner that is dead by the Fleet liveness rules
(no live session with a fresh beacon or verified PID, no owned dashboard
answering) and records `work_item_adopted` on the new ledger; a live owner
must be `run-close`d first. `run-close` and Phase 8 completion write the
released state on the register entry, and the lock also reads each run's
own status, so a closed or complete run never holds a ticket. Fleet marks
a ticket two live runs both list with `CLAIMED TWICE` on both cards. The
register is per user, so runs on another machine are not seen.

### What a ticket cost (#168)

The runner watches every streamed output line on both streams and keeps
the last usage the adapter printed: Codex's `tokens used` line and the
number after it, Claude's stream-json `usage.input_tokens` and
`usage.output_tokens`. It is recorded as `usage` on the session and on its
terminal lifecycle event (`source: adapter`); a session whose adapter
printed nothing records `not reported`; with `token_accounting` off,
`disabled`. Nothing is ever estimated. `status` sums it by role and by
phase (`usage`), the Phase 8 archive keeps it, and the Fleet Metrics
breakdown has a TOKENS column: the recorded totals of the runs that
closed each ticket in the range, `not reported` when none of them carried
a number, never zero. A run that closed several tickets is counted against
each and marked as shared.

### Repeat runs as evidence (#169)

A criterion may carry `repeat = N` (1 to 50) and `seed_env = NAME`
(`criterion-add`/`criterion-update --repeat N --seed-env NAME`). `verify`
then runs that criterion's commands N times in sequence with no cache and
stops at the first failure; the record carries `attempts` (number, exit
codes, output hashes, seed, and the failing attempt's output tail) and its
description names the failing attempt and seed. Each attempt's seed is
`sha256(run_hash:attempt)[:8]`, distinct and reproducible, set in `NAME`.
The criterion row reads `5/5` or `failed at attempt 3 of 5 (seed ab12cd34)`.
`--expect-fail` does not combine with repeat, and `[[regressions]]` groups
never repeat.

### The review certifies the rules it ran under (#170)

`rules_set_hash` covers `handsoff.toml`, `.claude/settings.json`,
`.claude/settings.local.json`, `.codex/config.toml`, `AGENTS.md`,
`CLAUDE.md` (absent files hash as absent), the engine's `prompts/reviewer.md`
and `rules/*.json`, and the engine version; contents are hashed, never
stored, and `.env` is never read. `record-review`, `design-approve`,
`deployment-gate --approve` and `verify-live` record `rules_hash` and the
entries behind it. The review gate, the deployment gate and the live gate
refuse when the set moved since the decision, naming the files
(`review gate: the rules set changed since it was recorded
(.claude/settings.json); record it again`); a fresh review re-binds. The
design approval records the set but is not compared, so a hook edit at
Phase 6 asks for a new review, not a new design. A decision recorded
before the field existed carries no hash and stays valid. `doctor` reports
`rules-set-changed: <files>`. With `review_binds_rules` off the hash is
still recorded, nothing is refused. A hook the adapter reads from outside
the project (a global `~/.claude/settings.json`) is not in the set.

### The final report posts itself (#171)

`run-close --post`, or Phase 8 completion with `report_posting = true`,
renders the report from an allowlist of ledger fields in fixed wording
(commits and where pushed; each criterion's id, policy, state and the
first 120 characters of its text with its evidence run; the review's
actor, time and hashes; the deployment approval and live check; usage
totals; work items; `validate`'s exact lines), rewrites home paths to `~`,
passes it through the output redactor, and refuses to post
(`report_not_posted`, reason `redaction`) if any line still looks like a
credential. It then posts one comment per issue work item through `gh`,
marked `<!-- handsoff-report HEAD -->`, closes the item, and ticks its box
in a parent epic named by a `Parent: #NN` line in the item body. The
ledger records `report_posted` with the comment URLs; a ticket that
already carries a report is skipped; without `gh` login nothing leaves
(`report_not_posted`, reason `gh_auth`). Pilot notes, chat and agent
output never enter the report. `tests/fixtures/final_report.md` pins the
wording.

### A stale failure never outranks the ledger (#172)

A current session in state `failed` reads `stopped` on Mission Control
when its persisted verdict was adopted (`result.adopted_at`) or when the
run has since advanced past the phase the session ran in; the detail says
which. Fleet therefore classifies such a run by its other rules, never as
FAILED. `session-result-adopt` rewrites a beacon naming that session with
state `adopted`. The failure itself stays in the ledger.

### The playbook ships with the engine (#208)

How to run a lane is engine knowledge, not any one project's: it lives in
`playbook/` (`INDEX.md`, `lanes.md`, `landing.md`, `reviewers.md`,
`lessons.md`), listed in the runtime manifest and shipped in the wheel, so
every machine that installs Handsoff has the same rules. `handsoff
playbook` prints the index and `handsoff playbook <topic>` one topic. A
host reads `lanes.md` before `init`, `landing.md` before `advance 6` and
`lessons.md` once per session; every managed launch carries the index and
`lanes.md` ahead of the project's own `[briefing]` knowledge base (#175),
with no configuration, and `--topic lessons` rides like a project topic.
Project knowledge (a device's quirks, a repository's audits, accounts)
stays in the project's knowledge base, declared in `handsoff.toml
[briefing]`, and never in the engine. A lesson that cost a round goes into
`lessons.md` in the same session; a bullet broken twice becomes a refusal.

### The clocks know the Mac slept (#193)

A laptop that sleeps after a minute idle made two runs read "design debate
for 7 hours" while nothing was happening. macOS logs every transition
(`pmset -g log`: "Entering Sleep", "Wake", "DarkWake"), each line with
its own UTC offset, so `machine_sleep_intervals` reads them exactly: a
Sleep opens an interval, only a Wake closes it, a DarkWake (maintenance)
leaves it open, an open one closes at now, and the offset on the line is
the timezone (a DST change is two offsets, nothing ambiguous). Every
duration the board computes is awake time with the sleep beside it: the
run elapsed, each phase, session durations, verification and wait times
(`metrics.asleep_seconds`, `metrics.phase_asleep_seconds`), the liveness
age and the stall warning (a closed lid is never a stall), the CI row's
elapsed. Mission Control's LCD clocks count awake time and read "asleep
7 h 03 m" beneath; the phase list and the Fleet card carry the same words.
The ledger is untouched; sleep is subtracted at read time; the log is
read on a background thread (it is tens of thousands of lines), so a
request never waits for it; on a machine without `pmset` every number is
the wall clock and the sleep is 0.

### The Architect can decline (#177)

Some issues should die at Phase 2. When the criteria describe a change
whose absence causes no observed harm, or a harm the engine already
prevents elsewhere, the Architect's honest outcome is not a design for
unnecessary work: `prompts/architect.md` names a third protocol line,
`HANDSOFF_DESIGN_DECLINE: {"reason": ..., "evidence": [...],
"alternative": ...|null}`, and the host records it:

```
python3 bin/handsoff_supervisor.py design-decline --by claude-architect \
  --reason "the Phase 8 gate already refuses an item without implemented_by" \
  --evidence "lane A refused issue-179 at advance 8 on 2026-09-21" \
  --alternative "fill every required item at Phase 5 and keep the gate"
```

Only at Phase 2, with no approved design, no live managed session and an
open run. The decline (reason, up to eight evidence lines, an alternative,
the design and acceptance hashes of the criteria it answers) is recorded
as a pending state, and the independent design reviewer judges it like a
proposal, for its evidence and never for the effort saved: the reviewer
packet carries `design_decline` in place of a proposal; `record-design-review
--approve` records `design_decline_approved` and closes the run with
`outcome: not_planned` in the same commit; `--request-changes` records
`design_decline_changes_requested` with the findings and sends it back,
the run staying at Phase 2 for a new proposal or decline. `advance 3` is
refused while a decline is pending; the architect cannot review its own
decline. A managed Architect emits the line and the runner records it
with the session's actor (a turn with both a proposal and a decline is
refused). Mission Control's briefing reads "Not planned, declined by X:
reason" once approved; Fleet reads the run as closed, never failed. Not
yet: a Pilot veto from Mission Control (a closed run can be reopened as
today).

### One implementer rule for every run (#176)

`advance 5 --implemented-by X` records X as the run's implementer and on
every required work item's delivery record that has none, creating the
record for a tag-derived item (one that entered the registry through a
`[#N]` criterion rather than `init --item`). A single-item run and a
three-item run therefore reach the Phase 8 gate the same way. A host that
wants a different actor on one item sets it with `work-item-update
--implemented-by` before Phase 8; an item that appears after Phase 5 is
still refused there until it has one.

### The page names the host (#186)

Two hosts run lanes side by side, and the page used to say "host" for
both. `init --by ACTOR` records the actor driving the run on
`initialized`; the snapshot's `host` block is `{family, actor, source}`
where the family is read from the actor's prefix (`claude-`, `codex-`,
the #164 rule) and from nothing else: `initialized.by` first, else the
newest actor on a host-side command in the ledger (criteria, proposals,
evidence, notes, the CI watch, work items) or `status.implemented_by`,
else `unknown`. Mission Control shows `HOST CLAUDE`, `HOST CODEX` or
`HOST UNKNOWN` beside the engine badge; a supervisor or architect station
filled by the host reads the family instead of "host"; a Fleet card
carries the family beside the project name. Nothing guesses: a run with
no family-prefixed actor anywhere reads unknown.

When the ball is with the host and the host has gone quiet, the board
says so (#194): `host_wait_view` reads the ledger's own newest write
(`updated_at` or the last event; a heartbeat or a session's output is not
the host writing), and past `stall_minutes` with no live managed session,
no Pilot decision pending and no pause, the briefing reads "Waiting on the
host (codex) since 03:03Z: launch design-review attempt 3 (8 h 04 m)",
names the LAUNCH ROLE button when the action is a launch, and the Fleet
card carries "WAITING ON HOST CODEX 8 h 04 m". Each crew chiclet also says
whether its station is the host or a managed session: `SUPERVISOR claude ·
host`, `IMPLEMENTER codex · managed` (#199).

### The console shows what can act (#183, #184)

`engine-upgrade`, `engine-rollback` and `engine-migrate` are CLI commands
and are no longer listed in the Mission Control inventory, where they sat
as three permanent READ ONLY rows. The engine pane keeps the version line
and the command list behind its disclosure and drops the upgrade and
migrate previews and the "execution is not offered" line. The briefing's
fixed reassurance copy is gone and its label shows only when the tone is
not steady. The FAILOVER card appears once it has a replacement or a
recovery. The token cell is one line, "tokens: not reported by <adapter>",
until a session reports usage. The API keeps every field it had except
the three engine rows and the previews.

### CI is a step of the run (#181)

Since #178 a lane in a repository with a protected `main` lands through a
pull request and waits for the required check. That wait belongs to the
run, so it is on the board:

```
python3 bin/handsoff_supervisor.py ci-watch --pr 180 --by claude-host
python3 bin/handsoff_supervisor.py ci-watch --poll
```

`ci-watch --pr N`, run right after `gh pr create`, records a
`ci_watch_started` event with the PR number and URL, its head commit, the
names of its checks, and the time to expect: for every workflow named on
the checks (sorted, so the choice is deterministic), the last five
successful runs on `main`, each measured as its longest job's start to
completion (the work; a run's own clock would count the minutes a job sat
queued, which once made a 1.5 minute run read 6), the median of those, the
largest workflow's median, and the runs counted as `expected_source`. No
history means `expected_seconds` null and the note "no previous run to
compare". `status.ci` holds the watch; the checks
themselves live in the `.handsoff-ci.json` side file, Handsoff state the
digest never counts.

Mission Control then shows a CI row under the phase rail: a pill (CI
RUNNING, PASSED, FAILED), a bar of elapsed over expected (indeterminate
without history, "over the last run's time" once past it), one cell per
check with its state and elapsed time linking to its job (a matrix
workflow renders one cell per job: the six `python (shard i of 6)` from
#180; a check that has not started reads "queued"), and the PR link. The
label leads with percent complete (#198): time-based like the bar and
capped at 99 until every check is done, the checks-done fraction when
there is no estimate, 100 on passed; it ticks every second on the page
from the snapshot's anchor with the elapsed time.
The snapshot refreshes the checks through `gh pr checks` at most once a
minute (the #158 conditional pattern); `--poll` forces one refresh and
prints the view as JSON, in the state the ledger holds after the poll. The engine reads gh's
output and never merges: merging stays `gh pr merge --auto`. Nothing from
gh's environment is read or stored; only the check fields named in
`CI_CHECK_FIELDS` ever reach the ledger or the side file.

The first time every check has completed, one terminal event is recorded:
`ci_passed`, or `ci_failed` naming the first red check. While the watched
head has a failed check, `compute_errors` adds one line, `CI: <check>
failed on PR #N (<url>); rerun or push, then ci-watch --pr N again`, which
refuses the Phase 6 to 7 transition. A red watch keeps refreshing: a rerun
of the failed job on the same head that comes back green records
`ci_passed` (once, marked `after_rerun`) and clears the line; a new head
watched with `ci-watch` replaces the watch outright. A passed watch adds nothing. A terminal
watch is served from the side file and never asks gh again.

## Review attempts and the convergence cap

Implementation reviews are persistent `review_attempts`, not chat claims. Starting a managed Reviewer opens an attempt automatically; findings close it as `changes_requested`, and approval closes it as `approved`. Findings are accepted only in Phase 4 or later and the complete proposed state is validated before it is written. Evidence attached while review is open refreshes that attempt's acceptance binding atomically, while changing a criterion specification abandons the attempt. A stale attempt can still be closed fail-closed with findings, but can never be approved. `review_round` is derived from the legacy offset plus that ledger. At `max_review_rounds`, Handsoff blocks before another Reviewer launches and Mission Control shows the required operator action. Only `review-cap-override --by OPERATOR --reason TEXT` grants one additional attempt.

## Automatic recovery of stalled runs

The `[recovery]` policy drives a lease-protected watchdog. It selects an exact current failed or silent managed session, then recovers only that session's immutable recorded role; a completed or unrelated role can never consume the failure's attempts. New sessions carry their launch phase, retry caps apply to the contiguous failed-session chain rather than lifetime history, and acknowledging an exhausted hold binds to that exact session so the same failure cannot immediately re-arm. `recover --by ACTOR` performs one bounded restart while preserving phase, acceptance, and evidence. Exhaustion becomes a visible blocked escalation. Liveness pings are advisory and unauthenticated: they may postpone recovery, never trigger it; a presumed-lost child is superseded rather than signalled.

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

Handsoff normalizes test footprints and refuses a configured whole-suite command, or an equivalent spelling, through ordinary `verify`. First record the intended semantic release with `release-plan --version vX.Y.Z --by PILOT`. Patch and minor plans expose the configured focused checks and block full-regression requests by default; a non-major exception requires `--full-regression-override-reason TEXT` and is audited. Major plans are eligible for the existing gate. The lifecycle is then `regression-request --group NAME --by ACTOR --reason TEXT`, a same-origin Mission Control **Accept Regression** or **Decline** decision, then `regression-run --request-id ID --by ACTOR`. Changing the release plan invalidates a pending or accepted request. Acceptance is single-use and bound to the run, release version/class, exact command hash, repository content and commit pair, configuration, acceptance, work-item scope, requester session, and session/recovery epoch. Expiry or any bound change invalidates it. A launched request locks ordinary workflow mutations until the nonce-bound runner terminalizes; only a human can fail a stranded launch with `regression-finalize`. Handsoff cannot intercept arbitrary operating-system processes outside its execution boundary, so role instructions explicitly prohibit external-shell bypasses.

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

## Archive analysis: the Miner

The completed-run archive described above is mined by the Miner
(`monzta1/miner`, its own repository since v0.3.62, #174), not by the engine.
The Miner reads every `*.json` under the archive directory, evaluates its
rules, drafts issues in house style, dedupes against the board and files
them once, on the right board; it is the only thing that files issues. Its
README carries the rules, the exclusions, the filing policy and the
`miner.toml` keys, which are the `[analysis]` keys with the same meaning.

What stays in the engine:

- **The trigger.** Right after the Phase 8 archive write, when `[analysis]
  enabled` is true (the default), the engine runs `miner scan --root <root>
  --json` and records `archive_scan_completed` on the run's live ledger with
  the report's counters (`findings`, `filed`, `suppressed`, `excluded`,
  `skipped_fixtures`, `unreadable`, `report_path`). A failure of any kind
  prints `HANDSOFF_ANALYSIS_FAILED (run still completed successfully)` and
  never fails the advance. Without a Miner installed the step prints
  `HANDSOFF_ANALYSIS_SKIPPED (run still completed successfully)` with the
  install hint and records nothing.
- **The shim.** `handsoff supervisor analyze-archives [--dry-run]
  [--archive-dir DIR] [--propose-rules]` calls `miner scan` or `miner
  propose-rules` with the same flags and prints the report path and the
  summary it always printed; without a Miner it refuses with
  `SHIP_FEATURE_BLOCKED: no Miner is installed; install the Miner: ...`.
  The shim stays for one release, then goes; call `miner` directly.
- **`pilot-note`** and the `[analysis]` table, which the Miner reads from a
  Handsoff project's `handsoff.toml` when there is no `miner.toml`.

The Miner is found through `HANDSOFF_MINER` (an executable), else `miner` on
`PATH`, else `miner` beside the engine's own interpreter. The engine
release names the Miner release it was verified with (`MINER_RELEASE`,
v0.1.0 for v0.3.62); the dedicated environment gets it from that release's
wheel, the repository being private:

```bash
gh release download v0.1.0 --repo monzta1/miner --pattern 'miner-*.whl' --dir /tmp
~/.local/share/handsoff/venv/bin/pip install /tmp/miner-0.1.0-py3-none-any.whl
``` Tests of the trigger and the shim use a fake `miner`
on `PATH`; the equality of the Miner's report with the engine's former scan
is proven in the Miner's own repository.

## Work items and the per-item status table

Scope means the set of items that carry acceptance criteria: the work-item scope hash covers the id, kind, and number of every item with at least one mapped criterion (untagged criteria attach to a single-item registry's only item), so editing `required`, `notes`, `title`, `url`, or `github_state` with `work-item-update` changes no scope hash and resets no decision (gates also accept the legacy digest that included `required`, so runs recorded by earlier engines keep their approvals). Once deployment approval is recorded the item set is frozen: a `work-items-sync` or `work-item-update` that would add or remove items is refused with the reason. `work-items-sync` never appends a feature-title ask beside explicit issue items; it prints `WORK_ITEM_SYNC_SKIPPED` and accepts `--item` for deliberate additions. A criterion whose tag matches no registry item is always rendered as `unattributed`, in single-item registries too, and `status` lists `unattributed_criteria` (#79). Criteria transactions register criterion-tag identities but never append a feature-title ask beside explicit issue items; `work-item-remove ITEM --by ACTOR` removes an item that maps to zero criteria without invalidating any decision, and is refused for items with criteria or after deployment approval; a required item with no criteria is reported as `unscoped` with that command in the gate message. Gate comparisons accept every historical digest formula (pre-v0.3.11 with `required`, the same normalized to true, and the v0.3.11 identity-only digest over all items), so runs recorded by earlier engines keep their approvals (#82). New runs persist `work_items` automatically. GitHub references become stable `issue-N` identities; criterion prefixes such as `[#29]` and `[cross]` map acceptance to `issue-29` and `ask-cross`. Plain asks split only on explicit semicolons, newlines, or numbered prefixes, never on an ambiguous conjunction. `work-items-sync --by ACTOR --from-tickets` migrates a legacy run; `work-item-activate` records the current item, and `work-item-update` changes display metadata, implementation identity, or recorded GitHub state. Mission Control shows the queue with canonical status, lane, measured caps, per-item progress, blocker, timestamps, and GitHub discrepancies. Recorded GitHub state is display input only; it never overrides Handsoff evidence. Persisted required items prevent completion until every row is done; optional rows remain visible without holding completion, while legacy derived-only rows remain informational.

For a measured low-risk item, initialize with `--lane small-fix` or run `lane-request ITEM --by ACTOR`, then let the Pilot use Mission Control or `lane-confirm ITEM --by PILOT`. Defaults are one primary fix, at most 3 criteria, 200 changed lines, and 6 files; changes to Handsoff configuration, schemas, prompts, or gate code escalate the item to the full lane. The design cycle is the only skipped stage: item-bound evidence, an implementation identity, an independent item review (`record-review --item ITEM`), deployment approval, live verification, and intact ledgers remain mandatory. Progress is 60% pro-rata acceptance plus five 8% gates and the run total is the half-up mean of required items.

`plan-tranche --repo OWNER/REPO [--issues-file issues.json]` writes only the gitignored `.handsoff-tranche-proposal.json`. It orders explicit dependencies deterministically, exposes every score input, proposes a lane only from supplied measurable facts, and reports archive-derived role-session medians or `unknown`. The Pilot may reorder or drop rows in Mission Control, or use `tranche-approve --proposal-hash HASH --item issue-N ... --drop issue-N ... --by PILOT`; the decision is hash-bound, Phase-1-only, atomic, and audited. Until approval, acceptance, status, event, and verification ledgers are byte-identical.

Recovery replaces only a managed role with authenticated `agent_session_*` history. A manual/external run with no managed session returns `not_applicable / no_managed_session`, writes nothing, launches nothing, and cannot consume recovery attempts or raise a false recovery hold.

## Known limitations

- **The advisory file lock is best-effort and POSIX-only.** `project_lock()` uses `fcntl.flock` around the whole read-validate-write; on a platform without `fcntl` it is a silent no-op, and multiple writers on such a platform can still race. Enforce single-writer discipline at the process level (only the Supervisor writes `handsoff-status.json`) if you need this on Windows.
- **`schemas/*.json` document the expected shape; they are not executed.** Runtime enforcement lives in `handsoff_lib.py`. Editing a schema file changes documentation, not behavior.
- **A killed process can leave a stray temp file.** `atomic_write_json` cleans up its `.tmp<pid>` file on any ordinary exception, but a `SIGKILL` or power loss between the write and the atomic rename can still leave one behind. Harmless (the real file is never touched), just worth pruning occasionally.
- **Stall detection accepts owned liveness only.** Workflow progress, output, beacons, and `heartbeat --session hs-SESSION` are reconciled against the current managed session. An unowned legacy heartbeat is retained for compatibility but cannot suppress a stall warning; a terminal or replaced session invalidates its heartbeat immediately. Declared background waits remain a separate, explicit state.
- **`verify` and `design-evidence run` run commands with `shell=True`.** `[checks].commands` and `[[design_evidence]].command` are trusted configuration, the same trust level as any other line in `handsoff.toml`; do not populate them from untrusted input.
- **Recovery costs real agent time and is bounded.** A recovery launch is a new model session. `max_attempts` limits that cost, and exhaustion requires an operator acknowledgement.
- **`verify` runs a deliberately small shell-command language.** `[checks].commands` is trusted configuration, but Handsoff rejects command substitution, process substitution, redirects, control operators, and other shell expansion forms before execution. Simple argv and test-path globs remain supported.
- **Hash chains detect tampering; they do not provide access control.** A writer that can replace a ledger and its separate anchor can forge a new history. Protect the project directory and CI artifacts with normal filesystem/repository permissions. Mission Control exposes bounded same-origin loopback mutations for the canonical Pilot operations described above; every request re-derives current state and uses the same Supervisor gate as the CLI. Anyone able to execute same-origin browser code on that local dashboard can invoke those Pilot actions, while agent-only protocol writes remain unavailable through the dashboard.
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
5. Run `python3 bin/handsoff_supervisor.py dashboard` for local monitoring, agent selection, and every Pilot decision. Use the CLI only for automation and agent protocol operations.

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
