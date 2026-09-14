# Governance design: review convergence (#31), supervisor recovery (#30), regression gate (#28), multi-item work table (#29)

Initial design: `claude-architect-gov`. Acceptance completion and takeover: `codex-architect-gov`. Status: proposal, awaiting independent design review and human `design-approve`. Plain ASCII throughout.

## 1. Overview and dependency order

Four GitHub issues are delivered as one integrated design so that their state models compose instead of collide. Implementation order is fixed by dependency:

1. **#31 review attempts.** Adds the structured, monotonic review-attempt ledger and the enforced convergence cap. Everything else reads it: recovery must respect it, the regression gate must not bypass it, the work-item table must show `in_review` and the cap escalation.
2. **#30 supervisor recovery.** Adds the recovery assessment, the single lease, bounded relaunch through the existing replacement machinery, and the shared `escalation` field that #31 also uses.
3. **#28 regression gate.** Adds the classified `[[regressions]]` groups, the hash-bound single-use approval request, the Mission Control Accept/Decline card, and the `awaiting_approval` exclusion that recovery honors.
4. **#29 work items.** Derives per-item rows from canonical state (criteria tags, review attempts, recovery, regression requests, escalation) and adds the aggregate gate that refuses completion while any required item is unfinished.

Settled context that this design extends and never reopens: the eight-phase state machine, `commit()` write-ahead journaling, the two hash-chained ledgers, the managed session and replacement model (`agent_sessions`, `agent_replacements`, `plan_agent_fallback`, `reserve_agent_replacement`, `execute_with_recovery`), the human-only `design-approve` and `deployment-gate` gates, the loopback-only POST pattern of the dashboard, and the Mission Control look (blue accent, phase rail, E.V.E. voice).

Shared conventions used by every ticket below:

- Every new status field is optional and nullable for a status file written before this design, so a legacy run stays valid after upgrading `bin/`. `init` writes explicit defaults for new runs. Enforcement that depends on a persisted new registry is feature-gated until the corresponding migration commit has populated it.
- Every mutation goes through `lib.commit()` under `project_lock`, so every write is journaled and hash-chained. No new code path writes status or acceptance directly.
- New ids use the existing bounded-id helper `_new_bounded_id` with new prefixes: `ha-` review attempt, `ho-` cap override, `hv-` recovery attempt, `hl-` recovery lease, `hg-` regression request. Pattern `^h[a-z]-[0-9a-f]{32}$` per prefix.
- `run_id` is not a new field. It is defined as the `hash` of the first event in `handsoff-events.jsonl` (the `initialized` event). It is stable, chain-anchored, available for this live run, and cannot be edited without breaking the chain. `lib.run_id(root, cfg)` returns it; a missing or empty log raises `HandsoffError`.
- Behavior is adapter-neutral: no rule below inspects the adapter name. Adapter and model only appear inside session snapshots that already exist.

## 2. Issue #31: structural review attempts and convergence limit

### 2.1 Problem restated

`review_round` exists but nothing increments it. A Supervisor can narrate "review round 6" while `handsoff-status.json` still says `0`, so `max_review_rounds` never fires and `record_quality_finding`'s eligibility (which reads `review_round`) is starved.

### 2.2 State model

`review_round` (existing, integer) remains the authoritative cumulative count of implementation-review attempts ever opened in this run. New runs derive it as `review_round == legacy_review_round_offset + len(review_attempts)`, where the offset is zero. On first #31 mutation, a legacy run with nonzero `review_round` and no ledger stores that count in `legacy_review_round_offset` instead of fabricating detail that old Handsoff never recorded; all newly opened attempts then append normally. This migrates any currently valid nonnegative legacy count without exceeding ledger capacity.

New status fields:

| Field | Type | Invariant (enforced in `validate_status_schema`) |
|---|---|---|
| `legacy_review_round_offset` | integer >= 0 | Default `0`; on legacy migration equals the pre-ledger `review_round` and never changes afterwards. |
| `review_attempts` | array, max 64 | Present on every run `init` creates from now on (default `[]`). Each element has exactly the keys below. `attempt` values equal `legacy_review_round_offset + 1..review_round` in array order. At most one element has `disposition == "open"` and it must be last. `review_round == legacy_review_round_offset + len(review_attempts)`. |
| `review_attempts[].attempt_id` | string | matches `^ha-[0-9a-f]{32}$`, unique |
| `review_attempts[].attempt` | int >= 1 | equals `legacy_review_round_offset +` its 1-based array index |
| `review_attempts[].opened_at` | tz-aware ISO timestamp | required |
| `review_attempts[].closed_at` | tz-aware ISO timestamp or null | null iff `disposition == "open"` |
| `review_attempts[].opened_by` | non-empty string, max 128 | actor that opened it (`--by`, session actor, or `advance` caller literal `advance`) |
| `review_attempts[].reviewer` | string or null | reviewer identity once known |
| `review_attempts[].session_ids` | array of session ids, max 160 | every managed reviewer session that served this attempt. The bound covers an initial primary plus eight fallbacks and all sixteen recovery episodes at nine launches each (153 maximum); ids remain authenticated here even if terminal session detail is later pruned from `agent_sessions`. |
| `review_attempts[].trigger` | enum `initial`, `changes_requested`, `acceptance_changed`, `implementation_changed`, `supervisor_remediation`, `manual_override` | |
| `review_attempts[].trigger_detail` | string, max 512 | free text or the previous attempt id |
| `review_attempts[].acceptance_hash` | string | `acceptance_hash(criteria)` at open |
| `review_attempts[].phase_number` | int 1..8 | phase at open |
| `review_attempts[].disposition` | enum `open`, `approved`, `changes_requested`, `abandoned`, `unrecorded` | |
| `review_attempts[].findings` | array, max 16, of `{code, summary}` | `code` in `acceptance_not_met`, `incorrect_implementation`, `review_changes_requested`, `other`; `summary` 1..512 chars |
| `review_cap_overrides` | array, max 8 | each `{override_id ^ho-..., by, at, reason, config_hash, review_round_at_grant}`; `by` and `reason` non-empty, `at` tz-aware |
| `escalation` | object or null | `{kind, at, reason, required_action, source}`; `kind` in `review_cap_exhausted`, `recovery_exhausted`, `recovery_paused`; all strings non-empty; `at` tz-aware. Shared with #30. |

`schemas/status.schema.json` gains matching definitions: `review_attempts` (array of the object above with `additionalProperties: false`), `review_cap_overrides`, `escalation`. The schema stays documentation; enforcement is `validate_status_schema`.

**Effective cap** = `max(cfg.max_review_rounds, legacy_review_round_offset) + count(valid overrides)`. `max_review_rounds` is bounded to `1..56`; on new runs at most eight overrides make 64, equal to the ledger capacity. A migrated offset above the base starts exactly exhausted at its historical count; one human override raises the cap by one and permits exactly one new attempt. Migrated history consumes no ledger slots, so up to eight overridden post-migration attempts still fit. Changing `max_review_rounds` invalidates earlier overrides, as `config_hash` already does.

### 2.3 How attempts open and close (automatic, no Supervisor memory required)

An attempt opens through `lib.open_review_attempt(status, cfg, *, by, trigger, detail, reviewer, session_id)`, which is called from exactly these places, always inside an existing locked commit:

1. `create_agent_session(role="reviewer")` while `phase_number >= 4`: opens an attempt in the same commit as the `agent_session_launching` event when no attempt is open; if one is open, appends the session id to it (a relaunch or a fallback continues the attempt). Phase 2 reviewer sessions (design critique) never touch attempts.
2. `review-attempt-start` (new command, brokered): explicit open for an unmanaged reviewer launch.
3. `record-review` with no open attempt: opens and closes one attempt as `approved` in the same commit (legacy flow keeps counting). With an open attempt, closes it as `approved`.
4. `record-review-findings` (new command, brokered) is valid only in Phase 4 or later. With no open attempt it opens and closes one as `changes_requested`; with an open attempt, it closes that attempt. The complete proposed state must pass the shared gates before commit.
5. `advance --review-round N`: retained for fixtures and recovery. `N < review_round` is refused with `review_round is monotonic`. `N == review_round` is a no-op. `N > review_round` appends `N - review_round` synthetic attempts (`trigger manual_override`, `disposition unrecorded`, `opened_by "advance"`) which are still subject to the cap gate, so `TestRoundCaps` keeps its `round cap` refusal unchanged.

Trigger is derived when not passed: `initial` for the first attempt; `changes_requested` when the previous attempt closed with that disposition; `acceptance_changed` when `acceptance_hash(criteria)` differs from the previous attempt's stored hash; otherwise `implementation_changed`. `--trigger supervisor_remediation` may be passed explicitly.

**Cap enforcement at open time.** `open_review_attempt` refuses when `review_round >= effective_cap`. The refusing command prints `REVIEW_ATTEMPT_REFUSED: review budget exhausted (3 of 3 attempts used)` and, in the same locked commit, sets `status = "blocked"`, `escalation = {kind: review_cap_exhausted, reason, required_action, source: last attempt id}`, and `next_action` to the literal operator instruction (see 2.5). `create_agent_session` for a reviewer raises the same refusal before any process starts, which is what makes a fourth reviewer launch impossible rather than merely discouraged.

**Cap enforcement at close time.** When `record-review-findings` closes attempt `k` with `changes_requested` and `k == effective_cap`, the same commit sets the escalation above (no remaining budget). Otherwise the run rolls back to Phase 4 (`Implementation`, `progress = min(progress, 40)`, `status in_progress`, `next_action` "Implementer addresses N findings from attempt k; Supervisor then starts attempt k+1 of CAP").

### 2.4 Gate rules added to `compute_errors`

- `round cap: review_round R exceeds effective max_review_rounds C (base B plus O overrides), escalate to the user` replaces the existing message body but keeps the `round cap:` prefix.
- `escalation gate: run is escalated (<kind>); status must stay blocked until the escalation is cleared by <command>` when `escalation` is non-null and proposed `status != "blocked"`.
- `review attempt gate: an open review attempt exists but status is complete` (an attempt can never be left open on a completed run).
- Schema-level: the invariants in the table above.

### 2.5 Commands and flags

| Command | Who | Effect |
|---|---|---|
| `review-attempt-start --by ACTOR [--reviewer ID] [--trigger T] [--note TEXT]` | Supervisor via broker, or operator | Opens attempt (phase >= 4 required). Prints `REVIEW_ATTEMPT_OPENED: ha-... (attempt k of CAP)` or `REVIEW_ATTEMPT_REFUSED`. Event `review_attempt_opened` or `review_attempt_refused`. |
| `record-review-findings --by REVIEWER --finding "CODE: summary" [--finding ...]` | Supervisor via broker on the Reviewer's behalf | Phase 4+ only. Closes the open attempt as `changes_requested` with structured findings (1..16), or opens and closes one when none exists. Refuses `--by` equal to `implemented_by` (case and whitespace insensitive) and validates the full proposed state before writing. Rolls back to Phase 4 or escalates at the cap. Events `review_attempt_closed` (+ `review_cap_escalated` at cap). |
| `record-review --by REVIEWER` (existing) | unchanged surface | Additionally closes the open attempt as `approved` (extra event `review_attempt_closed` in the same commit) or opens and closes one. |
| `review-cap-override --by OPERATOR --reason TEXT` | human only (broker refuses; not exposed in the dashboard) | Appends one override (+1 attempt), clears `escalation` when its kind is `review_cap_exhausted`, restores `status in_progress`, sets `next_action` "Start review attempt k+1 of CAP". Event `review_cap_override_recorded`. Refuses if the resulting effective cap would exceed `max_review_rounds + 8`. |
| `advance --review-round N` (existing) | | Monotonic as described in 2.3. |

`status` output gains `review_attempts` (compact: attempt, disposition, trigger, reviewer), `effective_max_review_rounds`, and `escalation`.

### 2.6 Broker changes

`HUMAN_ONLY_COMMANDS` adds `review-cap-override`. `_workflow_argv` accepts `review-attempt-start` (`by`, optional `reviewer`, `trigger`, `note`) and `record-review-findings` (`by`, `findings` as a non-empty string array, each becomes one `--finding`). A duplicate finding string in one request is refused.

### 2.7 Preservation guarantees

- `_invalidate_decisions` never removes `review_round`, closed `review_attempts`, `review_cap_overrides`, or `escalation`. Criterion mutation, evidence recording, symptom resolution, and phase rollback therefore preserve the counter. A criterion-specification mutation while an attempt is open atomically closes it as `abandoned` with finding code `acceptance_changed`; a later reviewer must open a fresh attempt. An audited evidence-only mutation (`verify` or `record-evidence`) instead refreshes the open attempt's acceptance binding in the same locked commit, because it changes proof state without changing the reviewed claim. `record-review` still refuses any stale binding. `record-review-findings` may close a stale attempt only with the fail-closed `changes_requested` disposition and logs both hashes; it can never grant approval.
- Process restart: the fields are on disk under the chain; a fresh process reads them unchanged.
- Recovery (#30) appends session ids to an open attempt. In Phase 5 with no open attempt, a reviewer recovery opens a normal `supervisor_remediation` attempt before launch when budget remains; at the cap it escalates instead. Reviewer session creation is always refused for a completed run, so recovery cannot manufacture attempts after completion.
- `record_quality_finding` keeps reading `review_round`; it now reflects real attempts.

### 2.8 Dashboard

Snapshot additions: `policy.review_round` (unchanged source), `policy.effective_max_review_rounds`, `policy.review_cap_overrides` (count), `review.attempts` (list of `{attempt, attempt_id, trigger, disposition, reviewer, opened_at, closed_at, findings_count}`), `escalation` (top level, same shape as status).

UI: the existing "Review cycles" mini-stat renders `reviewRoundLabel(policy)` from `dashboard/lib/dashboard-logic.js` (`"2 / 3"` or `"3 / 4 (1 override)"`), never from event text. A new right-stack panel `#review-attempts-panel` ("REVIEW CONVERGENCE") lists attempts with trigger, disposition, reviewer, and findings count. When `escalation.kind == review_cap_exhausted`, the existing sticky red input alert shows the escalation reason and the exact `review-cap-override` command; `input_required.kind` is `escalation`.

### 2.9 Out of scope

Design-round attempts (Phase 2) are already tracked by `design_round`; no change. No automatic reviewer relaunch after `changes_requested` (that remains the Supervisor's remediation loop). No dashboard button for the override: it is a budget decision that requires a reason.

## 3. Issue #30: bounded automatic recovery of stalled runs

### 3.1 Problem restated

Stall detection is advisory. When the driving process disappears (a killed host, a Supervisor that exited without relaunching, a run with no managed session at all), Mission Control reports "stalled" forever and nothing restores progress or escalates. Replacement today only triggers from inside the host that launched the failed session.

### 3.2 State model

| Field | Type | Invariant (`validate_status_schema`) |
|---|---|---|
| `recovery_attempts` | array, max 16 | each element: `recovery_id ^hv-[0-9a-f]{32}$` unique; `role` in the four roles; `trigger` in `worker_terminal`, `worker_silent`, `silent_run`; `from_session_id` session id or null; `to_session_id` session id or null; `attempt` int >= 1 equal to index+1; `cap` int >= 0; `holder` non-empty string; `state` in `reserved`, `launched`, `recovered`, `failed`, `escalated`; `reason` string; `at`, `launched_at` (nullable), `ended_at` (nullable) tz-aware. `reserved`/`launched` may only be the last element. Configuration constrains `max_attempts` to `0..16`, matching retained history. |
| `recovery_lease` | object or null | `{lease_id ^hl-..., holder, acquired_at, expires_at, recovery_id}`; when non-null, `recovery_id` must name the last recovery attempt and its state must be `reserved` or `launched`. |
| `escalation` | shared with #31 | kinds `recovery_exhausted`, `recovery_paused` |
| `agent_failures[].category` and `agent_replacements[].category` | existing enums | gain the value `presumed_lost` (label "host watchdog found no liveness signal past the threshold"); `FAILURE_CATEGORIES` and `RECOVERABLE_FAILURE_CATEGORIES` include it; `schemas/status.schema.json` enums updated. |

Host liveness is **not** stored in status. `execute_launch` writes `.handsoff-session-liveness.json` (generated state, gitignored, like the lock file) every `[recovery].liveness_seconds` while its child runs: a JSON object mapping `session_id` to the last ping timestamp; entries are removed at the terminal transition. Every read-modify-write holds `project_lock`, reloads the whole map, writes a same-directory temporary file, `fsync`s it, and atomically `os.replace`s it, so concurrent hosts cannot lose each other's entries. Assessment consults only the assigned session's entry; an unrelated live role cannot mask the assigned worker. The file is unauthenticated on purpose: a fresh entry can only delay recovery (conservative direction), never trigger it. `_artifact_signature` ignores it so pings do not spam SSE invalidations.

### 3.3 Config keys (`handsoff.toml`)

```
[recovery]
enabled = true                      # false disables assessment and launches entirely
max_attempts = 3                    # recovery episodes per run before escalation
lease_minutes = 15                  # a reserved lease that never launched expires after this
worker_loss_grace_minutes = 2       # terminal assigned session with no follow-up activity
live_session_silence_minutes = 10   # a running session with no host ping or status signal
liveness_seconds = 60               # host ping interval written by execute_launch
dashboard_watchdog = true           # Mission Control runs the watchdog thread
poll_seconds = 30                   # watchdog assessment interval
```

All keys are validated in `load_config` (booleans and bounded integers): `max_attempts 0..16`, `lease_minutes 1..1440`, `worker_loss_grace_minutes 1..1440`, `live_session_silence_minutes 1..1440`, `liveness_seconds 1..3600`, and `poll_seconds 1..3600`. `max_review_rounds` is bounded to `1..56`, leaving room for all eight human overrides inside the 64-entry ledger. None of the recovery keys are in `GOVERNANCE_CONFIG_KEYS`; changing them does not invalidate reviews or approvals.

### 3.4 Assessment (pure function, `lib.recovery_assessment(status, cfg, liveness, events, now)`)

Returns `{state, reason, assigned_role, silent_minutes, threshold_minutes, lost_session_id}` with `state` in:

- Before exclusions are evaluated, a lazy normalization step identifies an expired `recovery_lease`, closes its matching `reserved`/`launched` attempt as `failed` with reason `lease_expired`, clears the lease, and commits `recovery_lease_expired`. The pure assessor receives this normalized snapshot; the mutating caller performs the normalization once under the lock.
- `not_applicable` when any exclusion holds: `recovery.enabled` false; `status` in `complete`, `blocked`, `awaiting_approval`, `ready_to_deploy`; `phase_number` 8; `escalation` non-null; `authorization_hold` set; Phase 7 deployment wait; Phase 2 with an approved design review and no human approval (the existing `design_approval` input request); an open `human_pause_started`; an open `background_wait_started` whose heartbeat is fresh (`activity_note` non-null); a regression request in `awaiting_approval` or `accepted` (#28); a recovery attempt in `reserved`/`launched`; an unexpired `recovery_lease` held by someone else.
- `active` when the assigned role has a live current session whose own liveness ping (or its own `running_at` before the first ping) is within `live_session_silence_minutes`. Unrelated sessions, global `updated_at`, global heartbeats, and other status mutations never count for an assigned worker.
- `worker_terminal` when the assigned role's current session is terminal and that session's own `ended_at` is older than `worker_loss_grace_minutes`; unrelated live roles do not suppress it.
- `worker_silent` when the assigned role's current session is live but its own liveness ping (or its own `running_at` when no ping was ever written) is older than `live_session_silence_minutes`. `lost_session_id` names that exact session. Unrelated heartbeats, status writes, and live roles are ignored.
- `silent_run` only when the assigned role has no current session at all and the freshest global status/heartbeat signal is older than `stall_minutes`; global activity is relevant solely to this no-assignment case.

`assigned_role(status)` is moved into `handsoff_lib` from the dashboard's `_active_role` mapping (phase 1 architect, 2 reviewer or architect after `changes_requested`, 3 supervisor, 4 implementer, 5 reviewer, 6 implementer, 7 and 8 supervisor); the dashboard imports it so both agree.

### 3.5 Recovery step (`lib.recover_run(root, *, actor, launcher, which, snapshotter, now)`)

One bounded step, idempotent, safe under concurrent callers:

1. Under `project_lock`: reload, run `_assert_agent_telemetry_integrity`, expire and close any stale lease/attempt as described above, then compute the assessment. If `not_applicable` or `active`, return `{action: "skipped", reason}`; it writes nothing unless the stale-lease normalization itself was required.
2. If `len(recovery_attempts) >= recovery.max_attempts`: commit `status blocked`, `escalation {kind: recovery_exhausted}`, `next_action` naming `recovery-acknowledge`, event `recovery_escalated`; return `escalated`.
3. Otherwise commit, in one write: a `recovery_lease` (`expires_at = now + lease_minutes`), a `recovery_attempts` entry in state `reserved` with a pre-allocated `to_session_id`, and, for `worker_silent`, the lost session's transition to `failed` with failure category `presumed_lost` (same rules as `transition_agent_session`, applied inline). Events `recovery_lease_acquired`, `recovery_attempt_started` (and `agent_session_failed` for the lost session). Cross-feature rule: if the assigned role is `reviewer` and no attempt is open, open a `supervisor_remediation` review attempt in this commit when budget remains; at the cap, do not reserve and escalate `recovery_paused` with reason `review_cap_reached`. On completed runs both ordinary reviewer creation and recovery refuse before any attempt opens.
4. Release the lock. Build the launch spec with the primary profile (`build_launch_spec`, `resolution_source configured`) and a fixed task: "Resume Phase N (<name>) from trusted Handsoff state: read handsoff-status.json, handsoff-acceptance.json and the event log, continue the assigned role's work, and do not repeat evidenced work." Launch with `execute_with_recovery(spec, session_id_factory=<returns the pre-allocated id>)`. The reserved record moves to `launched` when the session reaches `running` (hooked in `transition_agent_session`, the same way replacement records already follow their session). A restart failure flows through the existing planner and `[fallback_policy]`, so fallback order and `max_failovers_per_role` apply unchanged.
5. On return: `recovered` if the relaunched chain ended `completed`; `failed` otherwise (reason from the last failure category or planner reason). The lease is cleared in the same commit. A planner `pilot_pause` result escalates immediately with `recovery_paused` (reason is the planner reason, e.g. `cap_exhausted`, `non_recoverable_failure`).

The launcher is injectable for tests (fake popen); production callers pass `handsoff_agent.execute_with_recovery`.

### 3.6 Watchers and commands

| Surface | Behavior |
|---|---|
| `recover --by ACTOR [--dry-run]` | One step. `--dry-run` prints the assessment only. Outputs `RECOVERY_SKIPPED: <reason>`, `RECOVERY_LAUNCHED: hv-... (attempt n of cap)`, `RECOVERY_RECOVERED`, `RECOVERY_FAILED`, `RECOVERY_ESCALATED`. Brokered: a live Supervisor may request it for a lost worker. |
| `watch --by ACTOR [--interval S]` | Foreground loop calling `recover` every interval; Ctrl-C stops it. |
| Mission Control watchdog | `DashboardServer` starts a daemon thread when `recovery.dashboard_watchdog` is true, actor `Mission Control Watchdog`, interval `recovery.poll_seconds`. Exceptions are printed to stderr and never stop serving. |
| `recovery-acknowledge --by OPERATOR --reason TEXT` | Human only (broker refuses). Clears an `escalation` of kind `recovery_exhausted` or `recovery_paused`, sets `status in_progress` and a `next_action` telling the operator to relaunch manually with `handsoff_agent.py launch`, and resets nothing else (attempt history stays). Event `recovery_acknowledged`. |

Duplicate prevention: the lease plus the `reserved/launched must be last` invariant. A second watcher inside the same lock window sees the lease and skips with `lease_held`; a crashed holder's lease expires after `lease_minutes` and the next watcher may proceed (its abandoned attempt is closed as `failed` with reason `lease_expired` first).

### 3.7 Gate rules added to `compute_errors`

- `escalation gate` (shared, section 2.4).
- `recovery gate: recovery_lease references an attempt that is not reserved or launched`.
- `recovery gate: a reserved or launched recovery attempt exists on a completed run`.

### 3.8 Dashboard

Snapshot: `recovery: {assessment: {state, reason, assigned_role, silent_minutes, threshold_minutes}, lease, attempts: [...last 8...], cap, watchdog_enabled}`. The FAILOVER TELEMETRY panel renders recovery attempts as rows headed `RECOVERY · <ROLE> · <STATE> · ATTEMPT n/cap` using `recoveryHeadline`/`recoveryDetail` in `dashboard-logic.js`. The E.V.E. briefing gets a `recovering` tone/label ("Continuity protocol engaged") while an attempt is `reserved`/`launched`, and the escalation flows through the existing input alert. The work-item table (#29) shows `recovering`.

### 3.9 Out of scope

Killing a presumed-lost process (no PIDs are stored by design; the session is superseded, not signalled). Recovery of the dashboard process itself. Recovery on platforms without `fcntl` (the lease still works, but the lock is advisory there, as already documented).

## 4. Issue #28: hard-gated full regressions

### 4.1 Problem restated

Nothing distinguishes a focused check from a whole-suite regression, and nothing stops an agent from launching one. The operator wants a fail-closed approval bound to exactly what will run.

### 4.2 Config keys

```
[checks]
commands = [...]          # focused checks, ungated, used by `verify` (unchanged)
live_commands = [...]     # unchanged

[[regressions]]
name = "python-full"                                   # ^[a-z0-9][a-z0-9-]{0,39}$, unique
commands = ["python3 tests/test_handsoff_supervisor.py"]   # 1..16 non-empty strings
reason = "Full Python regression before review handoff"    # default reason on the card
scope = "5,300-line unittest suite, roughly 4 minutes"    # free-text estimate
repositories = ["."]                                   # relative paths under the root, default ["."]
timeout_seconds = 3600                                 # optional, default [checks].timeout_seconds

[regression_gate]
approval_timeout_minutes = 30     # awaiting_approval expires
launch_window_minutes = 10        # accepted but not launched expires
```

`load_config` rules: a command string that appears in any `[[regressions]].commands` and also in `[checks].commands` or `[checks].live_commands` is refused (`handsoff.toml: command "..." is both a focused check and a regression`). It also computes a normalized test footprint from argv parsed with `shlex`: interpreter aliases are collapsed (`python`, `python3`, and the configured virtualenv Python), paths are resolved relative to root, repeated whitespace and harmless flags are ignored, and directory/glob/module selectors are expanded against the repository. A focused command is refused if its footprint contains every test target in a configured regression group, catching equivalent whole-suite spellings rather than only byte-identical strings. Shell metacharacters, command substitution, pipes, redirects, and wrapper interpreters are forbidden in focused checks; regression groups may retain shell syntax only inside the gate. `run_checks` repeats this classifier immediately before process launch, forming the trusted boundary for every Handsoff-owned test execution path; direct operator or third-party shell processes are outside Handsoff's process boundary and are explicitly not claimed as interceptable. Bypass variants are covered in `test_regression_gate.py`. Duplicate group names are refused. Every `repositories` entry must resolve inside the root (same containment check as state paths). The Implementer adds two groups to this repository's `handsoff.toml`: `python-full` (`python3 tests/test_handsoff_supervisor.py`) and `dashboard-full` (`node --test tests/dashboard/*.test.js`). Neither is approved for this run.

`regression_command_hash(group)` = sha256 of canonical JSON `{name, commands, normalized_test_footprints, repositories, timeout_seconds}`.

### 4.3 State model

`regression_requests`: array, max 16. At most one element may be in `awaiting_approval`, `accepted`, or `launched` (schema invariant). Before a seventeenth request, terminal entries are pruned oldest-first in the same chained commit while a `regression_history_pruned` event records their ids and terminal digests; if no terminal entry can be pruned, the request is refused. Full outcome history remains authenticated in the append-only event log.

| Key | Type / rule |
|---|---|
| `request_id` | `^hg-[0-9a-f]{32}$`, unique |
| `run_id` | string, equals `lib.run_id()` at request |
| `group`, `commands`, `command_hash` | copied from config at request; `commands` 1..16 strings |
| `reason`, `scope` | non-empty strings (reason from `--reason` or the group default) |
| `repositories` | array of `{path, head, branch, dirty, worktree_sha256}` from `repository_snapshot` per configured repository; `head` 40..64 hex. The digest algorithm is defined below. |
| `commit_pair` | `{base, head}`: `head` is the root repository HEAD; `base` is `git merge-base HEAD <default branch>` when resolvable (`refs/remotes/origin/HEAD`, else `main`, else `master`), otherwise null. Shown on the card; both bound. |
| `config_hash` | `config_hash(cfg)` at request |
| `acceptance_hash`, `scope_hash` | `acceptance_hash(criteria)` and `work_item_scope_hash(work_items)` at request; the latter binds item id, kind, number, and `required`, excluding display-only title/url/GitHub fields |
| `requested_by`, `requested_at` | actor, tz-aware |
| `requested_in_session` | session id of the requesting role's current live session, or null |
| `request_expires_at` | tz-aware |
| `state` | enum `awaiting_approval`, `accepted`, `declined`, `expired`, `invalidated`, `launched`, `completed`, `failed`, `cancelled` |
| `decision` | null or `{by, at, decision, command_hash_echo}`; `decision` in `accept`, `decline` |
| `acceptance_token` | null or 64-hex: sha256 of canonical `{run_id, request_id, command_hash, repositories, config_hash, acceptance_hash, decided_at, nonce}`; the nonce is random and discarded, so the token cannot be recomputed by anyone who did not witness the decision |
| `accept_expires_at` | null or tz-aware |
| `epoch` | null or `{sessions, replacements, recoveries}`: counts of `agent_sessions`, `agent_replacements`, `recovery_attempts` at acceptance |
| `launch_nonce_sha256` | null or 64 lowercase hex; null through `awaiting_approval`/`accepted` and all terminal states reached without launch; set when `launched` to SHA-256 of a 32-byte random nonce retained only by that runner, and preserved on `completed`/post-launch `failed` for audit. A `launched` request must have it; no pre-launch state may have it. |
| `launched_by`, `launched_at`, `ended_at` | nullable |
| `results` | array of `{command, exit_code, output_sha256, duration_s}` (no output text) |
| `invalidation_reason` | string or null |

`validate_status_schema` checks every key, enum, hex pattern, and timestamp; a malformed element makes the whole status invalid, so every command refuses (fail closed).

`worktree_sha256` is deterministic canonical JSON over Git-observed repository content. Enumerate `git ls-files -z --cached --others --exclude-standard`, byte-sort normalized POSIX relative paths, reject paths that escape the repository, and hash each entry as `{path, kind, mode, content_sha256}`. Regular-file content is hashed from bytes; symlinks hash the link target bytes without following; gitlinks hash the recorded object id plus the nested worktree digest when present. `.git`, Handsoff generated state (`handsoff-status.json`, `handsoff-acceptance.json`, both ledgers, lock, liveness), and configured explicit exclusions are omitted. The outer digest is SHA-256 of UTF-8 canonical JSON with sorted keys and compact separators. Tracked deletions are included as `{kind:"deleted", content_sha256:null}` from `git status --porcelain=v1 -z`, so identical porcelain text with changed file bytes cannot collide.

### 4.4 Lifecycle and commands

| Command | Who | Rules |
|---|---|---|
| `regression-request --group NAME --by ACTOR [--reason TEXT]` | Supervisor via broker, or operator | Refuses when a request is already open, when the root (or any configured repository) is not a git repository (`REGRESSION_BLOCKED: repository identity unavailable`), or when the group is unknown. Commits the element in `awaiting_approval`, sets `status blocked`, `next_action` "Full regression '<group>' awaits Pilot Accept/Decline in Mission Control (request hg-...)". Event `regression_requested`. Prints `REGRESSION_REQUESTED: hg-...`. |
| `regression-decide --request-id ID --accept|--decline --by OPERATOR --command-hash HASH` | Human only (broker refuses). Also invoked by the dashboard POST. | `--command-hash` must equal the stored hash (the deciding UI must echo what it displayed). Refuses when expired (state flips to `expired` in the same commit, event `regression_expired`) or not `awaiting_approval`. Accept: state `accepted`, token, `accept_expires_at`, `epoch`; `status` stays `blocked` with `next_action` "Regression accepted by X; Supervisor runs regression-run --request-id ID". Decline: state `declined`, `status in_progress`, `next_action` "Regression declined by X (reason); continue without it or request again". Events `regression_accepted` / `regression_declined` with `by` and `at`. |
| `regression-run --request-id ID --by ACTOR` | Supervisor via broker, or operator | Under the lock: state must be `accepted`; not past `accept_expires_at`; every bound input must match; requester session and epoch must still match. Any mismatch commits `invalidated` and refuses. Otherwise it commits state `launched`, a random `launch_nonce_sha256`, and keeps the run `blocked` before releasing the lock. Commands run only through the regression-only launch path. On return, a compare-and-set lock reload requires the request still be `launched` with the same nonce. It recomputes every bound input: matching inputs permit `completed`/`failed` from exit results; any repository/config/acceptance/scope/session/epoch change forces `failed` with reason `binding_changed_during_execution`, never `completed`. If a human already finalized the launch, the runner records `regression_late_result_ignored` without overwriting terminal state. A second run sees consumed state and refuses. |
| `regression-cancel --request-id ID --by ACTOR [--reason]` | Supervisor via broker, or operator | From `awaiting_approval` or `accepted`: state `cancelled`, `status in_progress`. Event `regression_cancelled`. |
| `regression-finalize --request-id ID --failed --by OPERATOR --reason TEXT` | human only; broker refuses | Fail-closed crash recovery for a request stranded in `launched`. It may only move `launched` to `failed`, never to `completed` or back to `accepted`; records `ended_at`, reason, identity, and `regression_failed` event, then returns the run to `in_progress`. A new request and a fresh Pilot acceptance are required to retry. |

Invalidation that Handsoff itself causes is applied eagerly while a request is `accepted`: criterion or scope mutation, override/acknowledgement, session, replacement, and recovery changes mark it `invalidated`. While a request is `launched`, the regression gate refuses every ordinary workflow mutation (`advance`, criteria/evidence/work-item mutations, session/replacement/recovery creation, review, approvals, and another regression transition); only the runner's nonce-bound terminal commit and human `regression-finalize --failed` may change state. External repository/config changes cannot be prevented, so the runner revalidates them before allowing `completed`. Display-only work-item updates remain allowed because they are not bound.

Expiry is lazy and fail closed: nothing runs in the background to flip states, but every reader (`regression-run`, `regression-decide`, the snapshot) treats a past `request_expires_at`/`accept_expires_at` as expired, and the next mutating command on the request records `expired`.

Focused checks stay ungated: `verify` reads only `[checks].commands`; the disjointness rule guarantees a regression command can never be a criterion test.

### 4.5 Gate rules added to `compute_errors`

- `regression gate: a full regression is awaiting Pilot decision, accepted, or launched; status must stay blocked until it is declined, cancelled, expired, failed, or completed` when an open request exists and proposed `status != blocked`.
- `regression gate: request <id> is launched; only its nonce-bound terminal commit or human regression-finalize --failed is permitted` is checked by the shared mutation preflight, so concurrent workflow progress cannot race the runner.
- `regression gate: a launched regression exists on a completed run`.
- Schema: at most one open request; every field typed.

### 4.6 Dashboard

Snapshot: `regression: {current: {...element fields..., still_valid: bool, validity_reasons: [...]}, history: [last 8 closed]}`. `still_valid` recomputes command hash, config hash, acceptance hash, repository snapshots, and epoch on every snapshot, so an acceptance that a new commit silently invalidated is displayed as `INVALIDATED (commit changed)` before anyone tries to launch it. `input_required.kind` gains `regression_approval` (priority: `deployment_approval`, then `regression_approval`, then `design_approval`, then `escalation`, then `blocked`).

UI: a new section `#regression-alert` directly under `#input-alert`, amber framed, titled "REGRESSION AUTHORIZATION". It shows: group name, every command in a `<code>` line, each repository with path, branch, head (short and full in `title`), dirty flag and worktree digest, the commit pair, reason, scope, requester, request time and expiry countdown. Buttons `#regression-accept` ("ACCEPT REGRESSION") and `#regression-decline` ("DECLINE"). After a decision the card shows the decision identity and timestamp (`decision.by`, `decision.at`), then `LAUNCHED by ... at ...`, `COMPLETED`/`FAILED` with per-command exit codes. The card is visible while the current request is `awaiting_approval`, `accepted`, or `launched`; the last closed request is summarized in one line `#regression-last`.

POST `/api/regression-decision`, loopback same-origin only, body exactly `{"request_id": "...", "decision": "accept"|"decline", "command_hash": "..."}` (max 4 KiB, duplicate keys refused). It calls `supervisor.cmd_regression_decide` with `by = "Mission Control Pilot"`. A request id that is not the current open request returns 409. No CORS.

### 4.7 Role instructions and broker

`prompts/supervisor.md`, `prompts/implementer.md`, `prompts/reviewer.md`, and `prompts/architect.md` each gain the sentence group in section 9. Broker: `HUMAN_ONLY_COMMANDS` adds `regression-decide`; `_workflow_argv` accepts `regression-request` (`group`, `by`, optional `reason`), `regression-run` (`request_id`, `by`), `regression-cancel` (`request_id`, `by`, optional `reason`).

### 4.8 Out of scope

Running regressions in CI, multi-root git orchestration beyond snapshotting listed repositories, and making regression results count as criterion evidence (criteria are proven by focused checks; a regression is a release safety net recorded in the event log).

## 5. Issue #29: derived per-item status table

### 5.1 Problem restated

The `[[tickets]]` table exists only if an agent remembers to configure it, and its `status` values are hand-typed. Per-item visibility must be derived from canonical state.

### 5.2 Work-item registry and identity

`handsoff-acceptance.json` gains a top-level `work_items` array (max 64), validated by `validate_acceptance_schema` and documented in `schemas/acceptance.schema.json`:

| Key | Rule |
|---|---|
| `id` | `issue-<number>` for GitHub issues, `ask-<slug>` for plain-language asks; slug `^[a-z0-9][a-z0-9-]{0,39}$`; unique; never changed after creation |
| `kind` | `issue` or `ask` |
| `number` | positive int for `issue`, null for `ask` |
| `title` | non-empty, max 200 |
| `url` | string (may be empty); derived from `[project].issue_url_template` (new optional key, e.g. `"https://github.com/monzta1/project-handsoff/issues/{number}"`) when empty for issues |
| `required` | bool, default true |
| `github_state` | `open`, `closed`, or null |
| `github_checked_at` | tz-aware or null |
| `created_at`, `updated_at` | tz-aware |
| `notes` | string, max 512 |

Display-only fields (`title`, `url`, `github_state`, `github_checked_at`, `notes`, timestamps) are excluded from `acceptance_hash` and `design_hash`, so routine presentation updates never invalidate reviews or approvals; the file hash in every event still logs each change. Execution-relevant scope fields (`id`, `kind`, `number`, `required`) are canonicalized by `work_item_scope_hash(effective_work_items)`, where `effective_work_items` is the persisted registry when present and otherwise the deterministic derived registry from feature text, criterion tags, and deprecated tickets. This derived scope exists now, before Phase-2 approval, so design review and human approval bind it even though persistence lands during #29 implementation. `work-items-sync` must produce the identical scope or it invalidates both decisions and requires fresh reviews; later scope changes likewise invalidate affected decisions. Display-only migration never invalidates.

Newly recorded `design_review`, `design_approved`, `review`, `deployment_approved`, and regression request records each store required `scope_hash`; their gate checks recompute and compare it alongside the existing design/acceptance/config hashes. Legacy records without `scope_hash` remain schema-valid but are not silently trusted by a scope-aware completion/deployment gate. A one-time `work-items-sync` bootstrap may attach the current derived hash to a pre-#29 `design_review`/`design_approved` pair only when: the complete, non-empty event chain is intact and its latest event anchors the current status and acceptance files; both records' existing design/config hashes still match; the approval event's recorded `acceptance_sha256` equals the current acceptance file; every effective required item is derived from the chain-anchored criterion tags (tickets contribute display metadata only); and pre/post persistence scope hashes are identical. Deleting both the log and its head is corruption once project state exists, not a new GENESIS state. The bootstrap commits `scope_binding_bootstrapped` naming both decision timestamps and the hash. A failed authenticated-bootstrap condition clears both decisions and requires fresh independent and human approval; an already-broken audit chain blocks `work-items-sync` without re-anchoring the untrusted edit. This is the path for this live run's approvals, which necessarily predate the #29 implementation.

Status gains `active_work_item` (string id or null, set by `work-item-activate`).

**Criterion to item mapping.** A criterion belongs to the item named by the tag at the start of its `requirement`: `[#31]` maps to `issue-31`; `[cross]` maps to `ask-cross`. Grammar: `^\[(#\d{1,9}|[a-z0-9][a-z0-9-]{0,39})\]\s`. In a multi-item run an untagged criterion belongs to the synthetic row `unattributed` (required, never done) so it is visible; `criterion-add`/`criterion-update` print `WORK_ITEM_WARNING: untagged criterion in a multi-item run` but do not refuse. In a single-item run every criterion belongs to the only item.

### 5.3 Detection at initialization (documented parsing rules)

`init "<feature text>" [--item TEXT ...]`:

1. Explicit `--item` flags win. `--item "#31"` or `--item "#31 Title"` declares an issue; any other text declares an ask. Mixed lists are fine.
2. Otherwise, every `#<digits>` token in the feature text declares an issue. Its title is the clause containing the token, where clauses are split on `,`, `;`, and newlines; the token and surrounding parentheses are removed; a leading `Label:` prefix in the first clause is dropped. This feature text yields four items: `issue-31` "review convergence", `issue-30` "supervisor recovery", `issue-28` "regression gate", `issue-29` "multi-item ticket table".
3. If no issue token exists, asks are split only on `;`, newlines, or numbered prefixes (`1)`, `2.`). The word "and", commas, colons, and parentheses never split: they are too ambiguous, so the text stays one ask. The slug is the first four words slugified, made unique with `-2`, `-3`.
4. A run with exactly one item is a single-item run; the item still exists in the registry (id `ask-<slug>` or `issue-N`).
5. New tags seen by `criterion-add`/`criterion-update` auto-create missing items with a derived title (`Issue 31` or the humanized slug) in the same commit; items are never auto-removed.

### 5.4 Commands

| Command | Who | Effect |
|---|---|---|
| `work-items-sync --by ACTOR [--from-tickets]` | Supervisor via broker, or operator | Creates missing items from criteria tags, feature text, and (with the flag) the deprecated `[[tickets]]` config (number, title, url; the config `status` is ignored). With `--from-tickets`, titles and urls from tickets overwrite derived ones. Persists `work_items` through `commit()`; event `work_items_synced`. Idempotent. |
| `work-item-update ID [--title T] [--url U] [--required|--optional] [--github-state open|closed] [--note TEXT]` | Supervisor via broker (github-state, title, url, note) or operator (all) | Updates the record; id immutable; event `work_item_updated`. Handsoff never fetches GitHub itself; `--github-state` is how a Supervisor records what it observed, and it never changes execution status. |
| `work-item-activate ID --by ACTOR` | Supervisor via broker | Sets `active_work_item`; event `work_item_activated`. |

`load_config` keeps accepting `[[tickets]]` (deprecated); when `work_items` is persisted the tickets are ignored and the snapshot reports `tickets_config_deprecated: true`.

### 5.5 Status derivation (`lib.derive_work_items(status, acceptance, cfg, verifications, now)`)

Rows come from the persisted registry; when the key is absent (a legacy run, or this run before sync) they are derived on the fly from criteria tags and feature text and the snapshot reports `registry: "derived"`. Derived legacy rows are informational only: the completion gate activates only once `work_items` has been persisted, preventing an upgrade from creating new blockers on an existing run. New runs persist their initial registry during `init`, so the gate is active from creation. Active global gates intentionally outrank criterion completion so an all-passing row cannot hide that the run is waiting, recovering, reviewing, or blocked. Per row, first matching rule wins:

1. `blocked`: any criterion `blocked`, or `escalation` non-null, or `status == blocked` without an open regression request.
2. `awaiting_approval`: an open regression request, Phase 7 without deployment approval, or Phase 2 awaiting human design approval.
3. `recovering`: a recovery attempt in `reserved` or `launched`.
4. `in_review`: an open review attempt, or Phase 5. This global workflow state outranks item-local completion so rows cannot say done before the review closes.
5. `done`: at least one criterion, all its criteria `passing`, and none of the global gates above applies.
6. `in_progress`: `active_work_item == id`, or any verification record references one of its criteria.
7. `not_started` otherwise.

Other columns: `phase` (run phase name), `next_action` (blocker text, else `verify <first non-passing criterion id>`, else the run's `next_action`), `blocker_reason` (escalation reason, regression request summary, or blocked criterion ids), `url`, `github_state`, `discrepancy` (null, or `"GitHub issue closed but Handsoff status is <status>"` when `github_state == closed` and status is not `done`, or `"Handsoff done but GitHub issue still open"` when the run is complete), `last_updated_at` (max of the record's `updated_at`, the latest verification timestamp touching its criteria, and the latest review attempt or regression event naming it), `criteria` counts.

`aggregate`: `{required_total, done, unfinished_ids, blocked_ids}` where `done` counts only rows whose final derived state is `done`; active global gates therefore make aggregate completion visibly false.

### 5.6 Gate rule added to `compute_errors`

`work items gate: required work item <id> is <status>; a run cannot be complete while any required item is not done` for proposed `status == complete` or `phase_number == 8` when a persisted registry exists and any required row (including `unattributed` and an item with zero criteria) is not `done`. Legacy derived-only rows never retroactively block completion.

### 5.7 Dashboard

Snapshot `work_items: {multi, registry, tickets_config_deprecated, items: [...], aggregate}` replaces `tickets`. `multi` is true when more than one row exists (synthetic `unattributed` counts).

UI: the existing `#ticket-panel` becomes the work-item table, visible whenever `multi` is true, in every phase, with columns ITEM, ISSUE, TITLE (link when url), STATUS, PHASE / NEXT, BLOCKER, UPDATED. Rows carry `data-item-id`, status badges reuse `.ticket-state` with classes for all seven states (`done` emerald, `blocked` red, `in_review` violet, `awaiting_approval` and `recovering` amber, `in_progress` blue, `not_started` faint) plus a `DISCREPANT` badge with the reason in `title` and inline text. Single-item runs hide the panel (existing compact presentation). The E.V.E. briefing summary appends "N of M work items done; unfinished: ids" whenever `multi`. Pure label helpers (`workItemStateLabel`, `workItemBlockerText`, `discrepancyLabel`) live in `dashboard-logic.js`.

### 5.8 Out of scope

Fetching GitHub state (integration neutrality), per-item phases (all items share the run's phase), and reordering rows (declaration order is the order).

## 6. Cross-feature cases (each becomes a focused test)

1. **Recovery never runs during a regression wait.** `recovery_assessment` returns `not_applicable` with reason `regression_pending` while a request is `awaiting_approval` or `accepted`; `recover` writes nothing. Test: `tests/test_governance_cross.py::CrossCases::test_recovery_skips_regression_wait`.
2. **Recovery cannot reset or bypass the review counter.** A reviewer relaunch appends its session id to the open attempt; `review_round` is unchanged. With no open attempt in Phase 5 and `review_round >= effective_cap`, `recover_run` escalates `recovery_paused` (`review_cap_reached`) instead of launching. `review_round` never decreases in any recovery path. Test: `test_recovery_respects_review_cap`.
3. **Remediation stops at the cap.** Through the broker: attempt 1 `record-review-findings`, implementer relaunch, attempt 2, attempt 3 findings at cap `3` triggers `review_cap_escalated`; `review-attempt-start` and a reviewer `create_agent_session` are refused; after `review-cap-override` attempt 4 opens. Test: `test_remediation_loop_stops_at_cap`.
4. **Acceptance invalidated by any bound change and not replayable.** Change group commands (config), `max_review_rounds` (config hash), commit (new git commit), worktree (touch a file), criteria (`criterion-update`): each makes `regression-run` refuse with `regression_invalidated`; replaying `regression-decide` on a decided request and replaying `regression-run` on a launched one are refused. Test: `tests/test_regression_gate.py::RegressionGate::test_bound_inputs_invalidate_and_replay_refused`.
5. **A recovered or restarted process cannot reuse an acceptance.** After accept, a recovery attempt (or any new managed session for the requesting role) changes the epoch; `regression-run` refuses with `epoch_changed`; a new request is required. Test: `tests/test_governance_cross.py::CrossCases::test_restart_cannot_reuse_acceptance`.
6. **Table exposes every state accurately.** Drive one multi-item fixture through regression wait (`awaiting_approval`), recovery reserved (`recovering`), open review attempt (`in_review`), escalation (`blocked`), `github_state closed` on an unfinished item (`DISCREPANT`), and all criteria passing (`done`); assert derived rows in Python and rendered rows in the browser. Tests: `test_governance_cross.py::CrossCases::test_table_states` and `tests/browser/work_items.browser.test.js`.
7. **Aggregate cannot hide a ticket.** With three items done and one `blocked` or `not_started`, `advance 8 100` is refused by the work items gate even when the phase gates would pass; the briefing lists the unfinished id. Test: `test_aggregate_cannot_hide_items`.
8. **Adapter parity.** Run the same #31/#30/#28/#29 sequence twice with `[agents]` all `codex` and all `claude` (fake `which`, fake popen); diff the resulting status with adapter/model/actor fields masked; must be identical. Test: `test_adapter_parity`.
9. **Audit integrity everywhere.** After each of restart (fresh subprocess), rollback (criterion mutation at Phase 5), remediation (findings), recovery relaunch, dashboard reconnect (server restart), and every rejection path (refused attempt, declined regression, refused launch, refused override): `verify-log` prints `EVENT_LOG_INTACT` and `status.verification_head` equals the ledger tail. Test: `test_audit_integrity_survives_everything`.

## 7. Migration note for this live run

This run (`claude/handsoff-governance-28-31`, initialized 2026-09-13) is chain-anchored history and is never re-initialized. Every new field is optional, so the run stays valid the moment the new `bin/` lands. Concretely:

1. After #31 lands: `review_attempts`, `review_cap_overrides`, and `escalation` are absent. Readers treat absence as `[]`, `[]`, `null`. Because this run's legacy `review_round` is zero, the first Phase 5 reviewer launch writes one real attempt and `review_round 1` in the same commit. A legacy fixture with any `review_round > 0` stores that number once as `legacy_review_round_offset` before appending new structured attempts, preserving the historical count without inventing unavailable per-attempt facts.
2. After #30 lands: `recovery_attempts` and `recovery_lease` are absent (treated as `[]` and `null`). The Mission Control process at `http://127.0.0.1:8771/` runs old code; the operator restarts it (the handoff file already requires a restart after any `bin/` or `dashboard/` change) to get the watchdog. The Architect session that wrote this document was launched by the old launcher and never wrote liveness pings; it ends through its own host's terminal transition, which still validates because unknown top-level status keys are ignored by both old and new validators.
3. After #28 lands: `regression_requests` absent (treated as `[]`). The Implementer adds the two `[[regressions]]` groups. No regression is requested or approved in this run unless the operator accepts one in Mission Control.
4. Before design approval, `work_item_scope_hash` already binds the deterministic effective registry derived from this feature text, criterion tags, and bootstrap tickets: `issue-31`, `issue-30`, `issue-28`, `issue-29`, and `ask-cross`. After #29 lands, `python3 bin/handsoff_supervisor.py --root /Users/moncyabraham/Projects/project-handsoff-claude-governance work-items-sync --by codex-supervisor-gov --from-tickets` persists exactly that registry through `commit()`. The command compares pre- and post-persistence scope hashes; equality preserves approvals, while any difference invalidates both design decisions and requires fresh independent/human approval. The Implementer then deletes the `[[tickets]]` block; until then it is ignored and flagged deprecated. Before sync, the table is informational and does not gate completion.
5. `run_id` for this run is the hash of its `initialized` event; nothing needs to be written.

## 8. Test plan

Each file is narrow, stdlib `unittest` or `node --test`, runnable alone, and exactly matches an entry in `[checks].commands`. None runs the whole suite. Python fixtures reuse `HandsoffTestCase`, `normalize_fixture_config`, the `_write_status` re-anchoring helper (only for simulating passage of time, never for forging governance state), the `TestAgentReplacement` fake-process pattern, and the `DashboardServer`-in-thread pattern; the `TestAgentReplacement._set_review_round` helper is updated to open real attempts because `review_round` is now derived.

| Command | Proves |
|---|---|
| `python3 tests/test_review_attempts.py` | #31: attempts open automatically from reviewer sessions and explicit commands; `review_round` equals legacy offset plus structured attempts in status, snapshot, and event history; a fourth attempt is refused at cap 3; human override adds one; criteria mutation abandons a stale-hash attempt; legacy counts including values above 64 migrate as an offset; configured cap plus overrides never exceeds 64; session history fits primary/fallback/recovery paths; completed runs cannot open attempts. |
| `python3 tests/test_supervisor_recovery.py` | #30: assessment states and every exclusion; unrelated global heartbeats, status mutations, and live roles cannot mask assigned terminal/silent workers; locked atomic concurrent ping updates preserve both entries; stale leases close before assessment and can recover; relaunch preserves state and follows fallback limits; concurrent watchers produce one lease; exhaustion escalates; bounded config validation; watchdog no-op writes no events. |
| `python3 tests/test_regression_gate.py` | #28: config classification, exact overlap, equivalent whole-suite footprint and shell-wrapper bypass refusal at load and `run_checks`; request bindings including scope; canonical worktree digest changes with bytes, symlink, deletion, untracked file, and nested repository; focused `verify` remains ungated; Accept/Decline UI; expiry; every invalidation; launched state blocks concurrent mutations; post-run CAS and binding revalidation prevent false completion; human-fail finalization wins races; bounded history pruning; malformed state fails closed. |
| `python3 tests/test_work_items.py` | #29: parsing of issues, asks, mixed, and ambiguous text; pre-approval derived scope hash; identical persistence preserves approvals and differing scope invalidates; identity stable across display updates; active global gates outrank `done`; aggregate gate on persisted registries; a legacy derived registry is informational and does not newly block completion; single-item `multi=false`; bootstrap migration. |
| `python3 tests/test_governance_cross.py` | Section 6 cases 1, 2, 3, 5, 6 (derivation half), 7, 8, 9. |
| `python3 tests/test_governance_docs.py` | README contains the four new sections and the focused-versus-regression configuration; each `prompts/*.md` contains the regression-gate prohibition sentence; `schemas/status.schema.json` and `schemas/acceptance.schema.json` list every new field and enum value defined in `handsoff_lib` (constants compared programmatically). |
| `node --test tests/dashboard/review_round.test.js` | `reviewRoundLabel` derives the label from structured policy, including overrides, never from event text; escalation copy helpers. |
| `node --test tests/dashboard/work_items.test.js` | Pure label helpers for all seven states, discrepancy badge, blocker text, single-item hiding decision. |
| `node --test tests/dashboard/regression_gate.test.js` | Card formatting helpers: command list, repository lines, commit pair, expiry countdown, decision line with actor and timestamp, validity reasons. |
| `node --test tests/browser/regression_gate.browser.test.js` | Complete Accept interaction and complete Decline interaction in a real browser: card content, button click, resulting `regression_requests` state, actor `Mission Control Pilot`, timestamp shown; a stale card (request cancelled underneath) gets 409 and shows the rejection. |
| `node --test tests/browser/work_items.browser.test.js` | Table for two GitHub issues, two unnumbered asks, and mixed inputs; visible across phases; blocked and done rows visually distinct (computed class and color); live update on state change; identical rows after `Page.reload` and after the dashboard server is restarted on the same port (reconnection); the six cross-feature states of case 6. |
| `node --test tests/browser/escalation.browser.test.js` | Review-cap escalation and recovery-exhausted escalation render in the sticky alert with the exact operator command; recovery attempt rows appear in the failover panel with attempt n of cap. |

### 8.1 Browser harness (dependency-free)

`tests/browser/harness.js` (shared, no npm packages):

- **Browser resolution**, first hit wins, else the test fails with `BROWSER_UNAVAILABLE` (never a silent pass): `$HANDSOFF_BROWSER`; `~/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell`; `~/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux/chrome-headless-shell`; the Playwright `chromium-1243` "Google Chrome for Testing" binary under either cache; `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`; `google-chrome` or `chromium` on PATH.
- **Launch**: `spawn(binary, ["--remote-debugging-port=0", "--user-data-dir=<mkdtemp>", "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"])` with `--headless=new` prepended for full Chrome builds. Parse `DevTools listening on ws://...` from stderr (10 s timeout), GET `/json/version` for `webSocketDebuggerUrl`, connect with Node's global `WebSocket`.
- **Protocol**: `Target.createTarget`, `Target.attachToTarget {flatten: true}`, `Page.enable`, `Runtime.enable`, `Page.navigate` and wait for `Page.loadEventFired`; `evaluate(expr)` via `Runtime.evaluate {returnByValue: true, awaitPromise: true}`; `waitFor(expr, timeoutMs)` polls every 100 ms; `click(selector)` scrolls the element into view, reads `getBoundingClientRect`, and dispatches real `Input.dispatchMouseEvent` `mouseMoved`, `mousePressed`, `mouseReleased` at its center (verified on this machine: a real dispatched click changed the DOM); `reload()` via `Page.reload` and a fresh load wait. Console errors are collected through `Runtime.consoleAPICalled` and fail the test.
- **Shutdown**: close the WebSocket, `SIGTERM` the browser, await its `exit` event, then remove the user-data directory (removing it before exit races Chrome's own writes).
- **Fixture root**: `mkdtemp`, copy `handsoff.toml` (normalized exactly as `normalize_fixture_config` does: roles `auto`, models `default`, fallbacks `[]`, commands `[]`, live_commands `[]`), `schemas/`, `prompts/`; `git init` plus one commit when a test needs repository identity. State is driven only through `python3 bin/handsoff_supervisor.py --root <fixture> ...` (a `run(args)` helper with `spawnSync`), never by writing state files.
- **Dashboard**: `spawn(python3, [bin/handsoff_supervisor.py, "--root", fixture, "dashboard", "--no-open", "--port", "0"])`; parse `HANDSOFF_DASHBOARD: http://127.0.0.1:<port>/` from stdout; `/healthz` readiness poll; on teardown `SIGTERM` and await exit. Reconnection tests kill the server and start a new one with `--port <same port>`, then wait for `#connection-status` to read `TACTICAL LINK: LIVE` again.
- **Assertions**: DOM via `evaluate`, Handsoff state via reading `handsoff-status.json`/`handsoff-acceptance.json` from the fixture and via `verify-log`.

## 9. Documentation and role-instruction updates

README (new sections, each with the exact configuration and commands): "Review attempts and the convergence cap", "Automatic recovery of stalled runs", "Focused checks versus full regressions" (the disjointness rule, `[[regressions]]`, `[regression_gate]`, the request/decide/run lifecycle, the safety invariant, what invalidates an acceptance), "Work items and the per-item status table" (parsing rules of 5.3, ambiguity handling, `work-items-sync`, GitHub state as recorded input, discrepancy display). "What is actually enforced" gains one bullet per gate rule above. "Known limitations" gains: the liveness file is unauthenticated and only delays recovery; a presumed-lost session is superseded, not signalled; recovery launches cost real agent time and are bounded by `max_attempts`; lazy expiry.

Role prompts, exact wording added to `prompts/supervisor.md`, `prompts/implementer.md`, `prompts/reviewer.md`, and `prompts/architect.md` (same paragraph in each):

> Full regression suites are the commands listed under `[[regressions]]` in `handsoff.toml`. Every Handsoff-owned execution path is hard-gated behind Mission Control Accept/Decline, including normalized equivalent whole-suite invocations. Never start one in an external shell to evade the product boundary. Request one with `regression-request`, wait for the Pilot's decision, and run it only through `regression-run` against that exact acceptance. Focused per-criterion checks through `verify` remain ungated.

`prompts/supervisor.md` additionally: record reviewer verdicts with `record-review` or `record-review-findings` (never prose), never ask for a review attempt beyond the cap (request `review-cap-override` from the operator instead), and call `recover --by YOUR_ID` when a worker session is lost. `prompts/reviewer.md` additionally: return `IMPLEMENTATION_CHANGES_REQUESTED` with findings formatted as `CODE: summary` lines so the Supervisor can record them structurally.

## 10. Product decisions and rationale

1. **`review_round` stays the counter; `review_attempts` is the ledger.** Quality findings, the cap gate, and the dashboard already read `review_round`; making it derived from a structured list fixes the symptom without a second counter that could drift.
2. **Attempts open on reviewer session creation, not only on explicit command.** The bug was an agent forgetting; the fix must not depend on remembering. Explicit commands remain for unmanaged reviewers.
3. **Cap refusal happens before a reviewer process exists.** Refusing at `create_agent_session` is what makes a fourth attempt impossible rather than merely blocked afterwards.
4. **One override equals one extra attempt, granted by a human with a reason, CLI only.** Budgets are operator decisions; a dashboard button would invite reflexive clicks.
5. **`changes_requested` rolls the run back to Phase 4.** It reuses the existing rollback semantics (progress capped at 40) instead of inventing a "review failed" phase.
6. **A shared `escalation` field with `status blocked`.** The dashboard already treats `blocked` as a sticky red alert; a typed reason on top of it makes both #31 and #30 explicit without a new status value.
7. **Recovery is a host-side, lease-protected step usable by the dashboard, a CLI loop, or a one-shot command.** The Supervisor is itself a session that can be lost, so the watchdog cannot live inside it. Duplicate prevention is a single lease under the existing lock.
8. **Liveness pings live in a locked, atomically replaced unauthenticated side file and are session-specific.** Chaining a ping every minute would flood the event log; an unauthenticated file can only postpone recovery, never cause it, so it is safe outside the chain.
9. **`presumed_lost` is a new closed failure category.** It keeps the session state machine and the planner untouched while making supersession auditable and recoverable.
10. **Recovery relaunches the assigned role with the primary profile and reuses `execute_with_recovery` for fallback.** One fallback planner, one cap, one audit shape.
11. **`run_id` is the first event's hash.** No new field, works for this live run, and cannot be edited without breaking the chain.
12. **Regressions are separate groups, footprint-disjoint from focused checks, and never criterion evidence.** Classification is enforced both at config load and immediately before Handsoff-owned check execution, so an equivalent whole-suite spelling cannot turn `verify` into a bypass. Handsoff does not claim to intercept unrelated OS processes.
13. **Acceptance binding includes run id, command hash, governance config, acceptance hash, every repository's head plus worktree digest, the commit pair, the requesting session, and a session/replacement/recovery epoch.** The epoch is what makes "a recovered or restarted process cannot reuse an acceptance" structural rather than advisory.
14. **Acceptance is consumed at `launched`, in a commit before any process starts.** A crash after that commit leaves the request unusable; a human may terminalize it as failed, but retry requires a new request and decision.
15. **Expiry is lazy.** No background timers; every reader treats past deadlines as expired and the next mutation records it.
16. **The deciding UI must echo the command hash.** A stale browser tab cannot accept a request it did not display.
17. **Git identity is required for a regression request.** Without it the "exact regression" cannot be proven; tests use `git init` fixtures.
18. **Work items are a persisted registry in the acceptance file, mapped to criteria by a requirement-text tag.** The tag is already the operator's convention; a registry keeps ids stable while titles change. Display metadata stays outside decision hashes, while execution-relevant scope is bound separately.
19. **Ambiguous asks are not split.** Only `;`, newlines, and numbered prefixes split; "and" never does. Under-splitting produces one honest row; over-splitting produces phantom rows that block completion.
20. **GitHub state is a recorded input, never fetched, never authoritative.** Discrepancies are displayed, not resolved.
21. **The aggregate gate keys on required rows, including empty ones.** An item declared at init with no criteria is an unfinished promise and must block completion.
22. **Browser-facing automated checks are Node-only and dependency-free.** They inspect the production dashboard/server wiring without pretending to launch Chrome. A real Mission Control interaction is recorded separately as browser evidence.
23. **`automated_and_browser` for UI criteria.** `verify` executes the Node wiring check as the automated half; `record-evidence --kind browser` is the real-browser attestation half, matching the existing evidence model.
24. **Legacy runs stay valid without gaining surprise blockers.** Every new field is optional; nonzero legacy review rounds backfill into structured history; derived legacy work items remain informational until explicitly persisted; `init` writes defaults; this live run migrates with one `work-items-sync` command and zero hand edits.
