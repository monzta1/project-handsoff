# Continuation handoff: governance run for issues #28, #29, #30, #31

Written 2026-09-13 by the Supervisor session (`claude-supervisor-gov`) at a context checkpoint. Everything below is read from Handsoff state, not memory; re-check with `status` before acting.

## Where the run is

- Worktree: `/Users/moncyabraham/Projects/project-handsoff-claude-governance`, branch `claude/handsoff-governance-28-31` (from `main` at `8f5d84e`).
- Handsoff: Phase 2 (Design debate), progress 10, status `in_progress`, design round 1. One open background wait (Architect drafting). Event log intact (10 events at checkpoint).
- Mission Control: `python3 bin/handsoff_supervisor.py --root <worktree> dashboard --port 8771 --no-open` (session-local process, restart it after any `bin/` or `dashboard/` change). URL http://127.0.0.1:8771/.
- Roles: all Claude Code (`[agents]` all `claude`, `[fallback_policy]` Claude-only `opus` entries). Never Codex.
- Managed Architect session `claude-architect-gov` was RUNNING at checkpoint (launched with a 5400 s timeout). Do not launch a second Architect; wait for its `agent_session_completed` (or failed/timed_out) event in `handsoff-events.jsonl`.

## Four-ticket table at checkpoint

| Ticket | Handsoff status | Evidence so far | Notes |
|---|---|---|---|
| #31 review attempts | not_started (design in progress) | none | primary_fix criterion REQ-001 still the init placeholder until the Architect rewrites it |
| #30 supervisor recovery | not_started (design in progress) | none | |
| #28 regression gate | not_started (design in progress) | none | full regression is NOT approved for this run; never run it |
| #29 multi-item table | not_started (design in progress) | none | `[[tickets]]` in handsoff.toml is a temporary observation bootstrap, not the implementation |

No ticket may be marked done; nothing is implemented yet.

## Committed at checkpoint

- `handsoff.toml`: Claude-only roles and fallbacks, temporary `[[tickets]]` bootstrap for 31/30/28/29, `[checks].commands` still empty (the Architect adds focused commands).
- `bin/handsoff_dashboard.py`: Mission Control now lights the chiclet of any live managed session and shows a live session on its Command Crew row (a live reviewer goes to DESIGN REVIEWER through Phase 2, to REVIEWER from Phase 5). Verified with `python3 tests/test_handsoff_supervisor.py TestAgentRuntimeTelemetry.test_dashboard_exposes_truthful_crew_and_replacement_telemetry TestAgentRuntimeTelemetry.test_dashboard_distinguishes_managed_legacy_and_next_launch_profiles` (OK) and `node --test tests/dashboard/crew_and_failover.test.js`. Needs a unit test of its own in the implementation phase.
- This handoff file.

Untracked and deliberately not committed: `.claude/settings.local.json` (worktree-scoped permission allowlist for role sessions), all `handsoff-*.json*` state (gitignored audit state, must never be deleted or hand-edited).

## How roles are launched (the only approved path)

`python3 /Users/moncyabraham/.claude/approved-launchers/handsoff_launch_role.py ROLE TASK_FILE ACTOR [TIMEOUT]`

ROLE is architect, implementer, or reviewer. TASK_FILE must live under `/Users/moncyabraham/.claude/approved-launchers/tasks/`. Run it in the background and read its log; the session appears in Handsoff as a managed `agent_session`. Task briefs already there: `task_architect.md` (in use), `task_design_review_r1.md` (ready, reviewer id `reviewer-gov-design-r1`).

Background: the global Stop hook `~/.claude/hooks/stop-review-claude.mjs` was fixed by the operator on 2026-09-13 (child marker `CLAUDE_STOP_REVIEW_CHILD`); before that every `claude -p` child recursed. Do not touch hooks or permission settings from Claude; the auto-mode classifier refuses and the operator applies such edits by hand.

## Operator authorizations already given

- Design, implement, focused checks, documentation, commit, push to `origin/claude/handsoff-governance-28-31`, close each issue only when its own criteria are verified.
- After the independent design review approves: `design-approve --by moncy --architect claude-architect-gov --summary "..."` (operator-provided authorization, never Claude self-approval).
- NOT authorized: any full regression, broad pytest, whole dashboard suite, deployment, CI, PR open or merge. Full regression stays awaiting a separate Mission Control gate (#28).

## Exact next actions

1. Wait for the Architect session to end. Then `background-wait-end --by claude-supervisor-gov`, read `docs/governance-design.md`, `handsoff-acceptance.json`, and `[checks].commands`. Sanity-check: every criterion test string exists verbatim in `[checks].commands`, no command runs the whole suite, exactly one primary_fix.
2. Launch the design reviewer: `python3 /Users/moncyabraham/.claude/approved-launchers/handsoff_launch_role.py reviewer /Users/moncyabraham/.claude/approved-launchers/tasks/task_design_review_r1.md reviewer-gov-design-r1 3600` (background). Record with `record-design-review --by reviewer-gov-design-r1 --architect claude-architect-gov --approve|--request-changes --summary "..."`. On changes: new Architect session with the findings quoted, `advance 2 <progress> --new-design-round`, fresh reviewer id r2, repeat (cap 3).
3. On approval: `design-approve --by moncy --architect claude-architect-gov --summary "..."`, then `advance 3 30`, `advance 4 40 --implemented-by claude-implementer-gov`.
4. Implement in order #31, #30, #28, #29: one Implementer session per ticket (`claude-implementer-gov`, brief under tasks/), focused checks via `verify --criterion ID --by claude-supervisor-gov`, browser evidence via `record-evidence`, `record-symptom-resolved` after REQ-001 passes, fresh implementation reviewer per ticket (`reviewer-gov-impl-<ticket>-r<n>`), scoped commit per ticket, dashboard row updated from canonical state once #29 lands.
5. Phase 5 `record-review --by <final reviewer id>`, Phase 6 docs and role instructions, Phase 7 commit and push (pre-authorized) and `deployment-gate --approve --by moncy`, Phase 8 `verify-live` then `advance 8 100`. Close each issue with an evidence comment. Final report includes the four-ticket table and states that the full regression was not run.
