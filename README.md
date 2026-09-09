# Project Handsoff

Project Handsoff is a portable, domain-neutral delivery framework with three roles:

```text
Supervisor -> Implementer -> Reviewer -> repair loop -> verified result
```

The Supervisor owns state, phase gates, acceptance evidence, retries, and user escalation. The Implementer changes the target project. The Reviewer is read-only and independently checks the brief, diff, tests, and original symptom.

## Quick start

Copy `handsoff.toml`, `schemas/`, `prompts/`, and everything in `bin/` into a target project, then from that project's root:

```bash
python3 bin/handsoff_supervisor.py init "Fix the thing that is broken"
python3 bin/handsoff_supervisor.py status
python3 bin/handsoff_supervisor.py validate
python3 bin/handsoff_supervisor.py advance 2 20
```

`init` scaffolds `handsoff-status.json` and `handsoff-acceptance.json` at the project root. Replace the acceptance registry's placeholder criterion with the real one(s) for this piece of work next. Every command resolves the project root itself: `--root DIR`, then `$HANDSOFF_ROOT`, then the nearest ancestor of the current directory that has a `handsoff.toml`, then the current directory. Run the commands from anywhere inside the project; you do not need to `cd` into `bin/`.

The framework is intentionally integration-neutral. Configure test commands, evidence paths, and workflow limits in `handsoff.toml`. It does not assume a language, tracker, hosting platform, or repository provider.

## Eight phases

1. Orient
2. Design debate
3. Design approved
4. Implementation
5. Independent review
6. Checks and documentation
7. Awaiting deployment approval
8. Live verified

## What is actually enforced

Every claim below is backed by a test in `tests/test_handsoff_supervisor.py`. Run it after copying the framework in, and again after changing `handsoff_lib.py`, to confirm the guarantees still hold:

```bash
python3 tests/test_handsoff_supervisor.py -v
```

- **The gate checks the state a call is about to write, not the state already on disk.** `advance` builds the proposed status in memory, validates that, and only writes it if the proposal passes. Phase 6 and beyond require every acceptance criterion `passing` and the original symptom marked resolved; 95%+ progress and a `ready_to_deploy`/`complete` status require the same.
- **No self-approval.** Phase 6+ requires BOTH `implemented_by` and `reviewed_by` recorded, and they must differ. Leaving either unset blocks the transition; it is not enough to only check them when both happen to be filled in.
- **Deployment approval is load-bearing, not a side command you can skip.** `advance` to Phase 8 checks for a recorded approval; `deployment-gate --approve` is what records it, and refuses before Phase 7 so approval cannot be granted before an Implementer or Reviewer has touched anything. The approval is bound to a hash of the acceptance criteria at the moment it was given; if the registry changes afterward, the Phase 8 gate recomputes the hash and refuses the now-stale approval.
- **The read-validate-write sequence is one locked operation, not three, in every command that writes.** `advance`, `deployment-gate`, `init`, and `verify` all hold the project lock across their reads, validation, and writes, including the event log append. Locking only the final write, or locking some write paths and not others, is how a race gets in: two concurrent `init` or `verify` calls used to fork the event log's hash chain and produce false tamper reports on lines nobody had touched.
- **A malformed hand-edit refuses cleanly, it does not crash.** `progress`, `design_round`, and `review_round` are type-checked before any gate logic casts them, including rejecting `NaN`/`Infinity` (valid JSON-extension floats that pass a plain `isinstance(x, float)` check and then crash `int()`/`float()` arithmetic). A bad value in a hand-edited status.json is reported as a validation error, not a raw Python traceback, in both `handsoff_supervisor.py` and `validate_handsoff_status.py`. Both scripts also catch any unexpected exception as a last resort, for the same reason.
- **The acceptance hash behind a deployment approval ignores harmless reordering.** It sorts criteria by id first, so re-saving or merging `handsoff-acceptance.json` without changing any criterion's content does not falsely invalidate a still-valid approval.
- **Round and stall limits are enforced, not decorative.** `design_round` and `review_round` past `handsoff.toml`'s `max_design_rounds`/`max_review_rounds` block further advancement. A run with no update past `stall_minutes` surfaces a `stall_warning` in `status` for escalation; this is advisory, not a hard block, so a stalled run can still be inspected and unstuck.
- **Writes are atomic.** Every status write lands in a sibling temp file first, then replaces the real file; a process killed mid-write leaves the old file intact, never a truncated one.
- **The event log is tamper-evident.** Each event is hash-chained to the one before it. `verify-log` walks the chain and reports exactly which line was edited or reordered, if any.
- **`handsoff.toml` is actually read.** `status_file`, `acceptance_file`, `event_log`, the round/stall limits, and `[checks].commands` all come from the config, not hardcoded defaults, with sane defaults only when a key is absent.
- **A criterion can point at something real.** `handsoff_supervisor.py verify` runs `[checks].commands` for real and reports each command's exit code and output hash, evidence that something executed, not a sentence someone typed into JSON.
- **One validator, not two that can drift.** `handsoff_supervisor.py validate` and `validate_handsoff_status.py` both call the same `compute_errors()` in `handsoff_lib.py`.

## Known limitations

- **The advisory file lock is best-effort and POSIX-only.** `project_lock()` uses `fcntl.flock` around the whole read-validate-write; on a platform without `fcntl` it is a silent no-op, and multiple writers on such a platform can still race. Enforce single-writer discipline at the process level (only the Supervisor writes `handsoff-status.json`) if you need this on Windows.
- **`schemas/*.json` document the expected shape; they are not executed.** The rules actually enforced at runtime are the hand-written checks in `handsoff_lib.py`'s `validate_status_schema`/`validate_acceptance_schema`, which cover the same required fields and enum values. Editing a schema file changes documentation, not behavior.
- **A killed process can leave a stray temp file.** `atomic_write_json` cleans up its `.tmp<pid>` file on any ordinary exception, but a `SIGKILL` or power loss between the write and the atomic rename can still leave one behind. Harmless (the real file is never touched), just worth pruning occasionally.
- **Stall detection reads `updated_at` on the status file**, not a live heartbeat. A process that hangs without ever calling `advance` again will correctly show as stalled; a process that is merely slow but still calling `advance` periodically will not.
- **`verify` runs commands with `shell=True`.** `[checks].commands` is trusted configuration, the same trust level as any other line in `handsoff.toml`; do not populate it from untrusted input.
- **The hash chain detects tampering, it does not prevent it.** Anyone with filesystem access can still delete the whole log, or delete and rewrite it consistently from scratch. It catches an edited or reordered record; it is not a substitute for real access control.

## Agent roles

- `prompts/supervisor.md`: state machine, handoffs, gates, and escalation.
- `prompts/implementer.md`: scoped implementation and repair instructions.
- `prompts/reviewer.md`: independent, read-only verification rubric.

These are prose instructions for whichever agent or person plays each role. `handsoff.toml`'s `[agents]` section names them for a human operator to configure a launch command against; nothing in `bin/` spawns a process from it today. If you wire that up, keep the constraint the prompts already assume: the Reviewer never writes to the target project.

## Import into another project

1. Copy the framework files into the project.
2. Run `handsoff_supervisor.py init "<feature>"`, then edit `handsoff-acceptance.json` to name the real criteria.
3. Set `[checks].commands` in `handsoff.toml` to the project's actual test/check commands, and the round/stall limits if the defaults do not fit.
4. Start the Supervisor with the target issue or brief.
5. Point any UI at `handsoff-status.json` for progress monitoring.

No project-specific work, credentials, hostnames, or tracker assumptions are included.
