# Benchmark: the #33 to #40 tranche against the design phase (#45)

Issue #45 asks one question: does the #33 to #40 tranche (delta review
packets #36, the follow-up reviewer tier #37, cached design evidence #38,
on top of the #35 attempt budget) make the design phase cheaper without
making the review worse? This document is the method, the reproduction
commands, the thresholds, and the results table. The harness is
`tools/benchmark_design_phase.py`; its stub mode proves the harness, its
live mode is the paid measurement and runs only on the Pilot's explicit
yes after a cost estimate.

## What is compared

Two arms, each a fresh Handsoff run in a temporary `git clone` of the
fixture repository (this checkout by default) at a pinned revision
(`--fixture-revision`, default `HEAD`), driven by this checkout's own
`bin/` so both arms use identical product code:

| arm | `reviewer_followup` (#37) | `[[design_evidence]]` (#38) | `design-review-packet` (#36) |
|---|---|---|---|
| baseline | absent | absent | never called (the `--no-packets` behaviour) |
| tranche | on | on | called after every changes-requested review |

Everything else is the same in both arms: the task text
(`tools/benchmark_fixture/task.md`), the seeded acceptance criteria (one
`criteria-apply` transaction from `tools/benchmark_fixture/fixture.json`),
the seeded structural defects, the adapter and model profiles, the
`[workflow] max_autonomous_design_reviews` budget (2, the product default),
and the attempt limit (`--max-attempts`, default 3).

Per arm the harness: clones the fixture at the revision, writes the
arm-specific `handsoff.toml`, runs `init`, `criteria-apply`, and
`advance 2 20`, then loops: (tranche only) `design-evidence run`; an
Architect session through `handsoff_agent.execute_launch`; (tranche only,
from the second attempt) `design-review-packet` with each prior finding
dispositioned `resolved` when the Architect's revision carries a
`RESOLVED F<n>.<m>` line for it, `unresolved` otherwise; a Reviewer
session through `execute_launch` (the #37 tier selection and the #36
packet delivery are the product's, not the harness's); then
`record-design-review` with the decision and `FINDING:` lines the Reviewer
returned. The loop ends at `DESIGN_APPROVED`, at `--max-attempts`, or when
the #35 budget is exhausted a second time.

The #35 budget is respected, not bypassed: when a Reviewer launch would be
refused because the budget is exhausted, the harness runs
`design-review-authorize --by benchmark-pilot` exactly once per arm and
records `authorized_attempt` in `run.json`; a second exhaustion stops the
arm with outcome `budget_exhausted`.

## What is recorded

`benchmark/<timestamp>/<arm>/run.json` carries, per session: `role`,
`adapter`, `requested_model`, `reported_model`, `attempt`, `tier` and
`tier_reason`, `packet_id`, `input_tokens`, `output_tokens`,
`usage_source`, `started_at`, `ended_at`, `seconds`, `design_evidence`
(configured artifacts, `design_evidence_recorded` events in the ledger,
and per-artifact state at launch), `design_evidence_run` (executed versus
reused counts from the `design-evidence run` that preceded an Architect
launch), the exact `command` argv, `stdin_bytes`, `stdout_bytes`,
`stdout_sha256`, and the transcript path. Per repeat: `wall_clock_seconds`
(from `init` to the last `record-design-review`, harness overhead
included), `session_seconds_total`, `design_phase_tokens`, `attempts`,
`authorized_attempt`, `defects_found`, every Supervisor command with its
exit code and duration, and `design_timing` from
`lib.summarize_design_timing` over the run's own event log.

`benchmark/<timestamp>/summary.json` carries `mode` (`stub` or `live`),
per-arm medians over `--repeat` runs (wall clock, session seconds, tokens),
`deltas_percent` (tranche relative to baseline; negative is a reduction),
`defect_retention`, `thresholds`, `thresholds_met`, and the reproduction
command.

Token counts come only from the runner's structured output. The harness
appends `--output-format json` to the claude argv (verified in
`claude --help` on this machine: `"json" (single result)`) and `--json`
to the codex argv (verified in `codex exec --help`: `Print events to
stdout as JSONL`). The parser reads `usage.input_tokens` and
`usage.output_tokens` from any JSON object or JSONL event the runner
prints, `modelUsage` keys as the reported model when present, `result`
(claude) or `agent_message` items (codex) as the text. When a runner
reports no usage the token fields are `null`, `usage_source` is `none`,
the repeat's `design_phase_tokens` is `null`, and `thresholds_met` is
`null`. Nothing is ever estimated. The exact field names of the live
runners' usage blocks were not verified on this machine because no live
session was run; the parser tolerates their absence.

The stub (`--stub`) is a script the harness installs as both `claude` and
`codex` in a private directory and resolves through its own `which`, so
PATH is never consulted for an adapter. It reads the whole role prompt
from stdin, sleeps `--stub-sleep` seconds, and emits a fixed answer in the
runner's structured shape with a usage block whose input count is the
prompt's byte length divided by four. The stub Reviewer names every seeded
defect id and returns `DESIGN_CHANGES_REQUESTED` on attempt 1 and
`DESIGN_APPROVED` on attempt 2; the stub Architect marks every finding it
was handed `RESOLVED`. Its numbers exercise the parser and show the
prompt-size effect of packets and evidence. They are not a measurement.

## Thresholds and rules

- Wall clock: the tranche's median design-phase wall clock must be at
  least 30 percent below the baseline's.
- Tokens: the tranche's median design-phase tokens (input plus output over
  every Architect and Reviewer session) must be at least 40 percent below
  the baseline's.
- Defect retention: a seeded defect counts as found in an arm when any
  Reviewer output in that arm names its id or one of the fixture's
  keywords for it. Every defect found in the baseline must also be found
  in the tranche (`defect_retention.retained`); anything found in the
  baseline and missing from the tranche is listed in `missing_in_tranche`
  and fails the benchmark regardless of the cost deltas.
- `thresholds_met` is true only when all three hold, false when any is
  measured and fails, and `null` when the token comparison is impossible.
- Gates unchanged: the harness changes no gate. Both arms run the same
  `record-design-review`, the same `design_hash` binding, the same
  reviewer-differs-from-architect refusal, the same #35 budget with the
  same `design-review-authorize` path, and the same Phase 2 rules. The
  tranche's features are cost knobs (`reviewer_followup` is deliberately
  outside `GOVERNANCE_CONFIG_KEYS`; packets and evidence change what a
  reviewer is shown, never what is recorded); the baseline arm simply does
  not configure them. No product code was modified for this benchmark.

## Reproduction

```bash
# Stub run (seconds, free, proves the harness; the numbers are labelled STUB):
python3 tools/benchmark_design_phase.py --stub

# The same, as the automated criterion runs it against a temporary fixture:
python3 -m unittest tests.test_handsoff_supervisor.TestBenchmarkHarness -v

# Live run (paid; only after the Pilot's yes to a cost estimate):
python3 tools/benchmark_design_phase.py --live --fixture-revision <commit> \
    --adapter-path "$HOME/Library/Application Support/Claude/claude-code/<version>/claude.app/Contents/MacOS" \
    --out benchmark/live-<date>
```

Useful flags: `--arms baseline` or `--arms tranche` for one arm,
`--repeat N` for medians over N runs per arm, `--max-attempts`,
`--session-timeout`, `--no-packets` (ablation: tranche without packets),
`--keep-workdir` to inspect the temporary clones, `--fixture` and
`--task-file` to point at another fixture. `benchmark/` is gitignored;
the numbers are copied into the table below by hand.

On this machine `claude` is not on PATH (the only binary is inside the
desktop app bundle), which is what `--adapter-path` is for. In `--live`
mode the harness also drops the nested Claude Code session variables
(`CLAUDECODE` and its siblings) from the child environment so a launch
from inside a Claude Code session can start.

## Cost note

A live run is two complete design phases with real Architect and Reviewer
sessions: per arm, up to `--max-attempts` (3) Architect sessions on the
architect profile (`claude` / `claude-opus-5` in the fixture) and up to 3
Reviewer sessions (codex primary; in the tranche arm the follow-up
attempts go to `claude` / `claude-haiku-4-5-20251001`, a label copied from the README
example that the Pilot should replace with a real model id in
`tools/benchmark_fixture/fixture.json` before a live run). With the
fixture's default budget of 2 and one authorization per arm, the minimum
is 2 Architect plus 2 Reviewer sessions per arm (4 per arm, 8 total) and
the maximum is 12 sessions total. Each session's prompt is 4 to 8 KB
before the model reads any of the repository (the stub run's
`stdin_bytes`); the real token cost depends on how much of the repository
each session reads, which the stub cannot predict. The Supervisor records
`human-pause-start --by moncy --note "#45 live benchmark needs spend
approval: <estimate>"`, presents the estimate, and runs `--live` only on
an explicit yes.

## Results

| row | mode | arm | wall clock (s) | sessions | design-phase tokens (in + out) | attempts | authorized | defects found | notes |
|---|---|---|---|---|---|---|---|---|---|
| STUB | stub | baseline | 3.029 | 4 | 5473 (5315 + 158) | 2 | none | SD-1, SD-2, SD-3 | `benchmark/20260914T144551Z`, fixture `a4f9acf`, `--stub-sleep 0.2` |
| STUB | stub | tranche | 4.145 | 4 | 7137 (6979 + 158) | 2 | none | SD-1, SD-2, SD-3 | same run; attempt 2 reviewer `claude-haiku-4-5-20251001` follow-up tier with a delta packet (id prefix `262f0e37`), design evidence executed 2 then reused 2 |
| STUB | stub | deltas | +36.84 % | | +30.4 % | | | retained: true, missing: none | `thresholds_met: false` (not a measurement: stub tokens are prompt bytes / 4, so packets and evidence can only add bytes; stub wall clock is harness overhead plus 0.2 s sleeps) |
| LIVE | live | baseline | 1085.3 | 6 | 390398 (318506 in + 71892 out) | 3 (attempt 3 Pilot-authorized) | none | SD-1, SD-2, SD-3 | `benchmark/live-20260914`, fixture `a4f9acf`, 2026-09-14T14:59:08Z to 15:22:07Z; architect claude-opus-5 x3 (259 s, 246 s, 472 s), reviewer codex default x3 (37 s, 36 s, 34 s, about 100k input tokens each); outcome max_attempts (never approved) |
| LIVE | live | tranche | 1173.5 | 6 | 156102 (77504 in + 78598 out) | 3 (attempt 3 Pilot-authorized) | design evidence executed 2 on attempt 1, reused 2 on attempts 2 and 3; delta packets on attempts 2 and 3 | SD-1, SD-2, SD-3 | same run, 15:22:08Z to 15:52:45Z; architect claude-opus-5 x3 (330 s, 266 s, 262 s), reviewer attempt 1 codex default primary (27 s, 77k input), attempts 2 and 3 claude-haiku-4-5-20251001 follow-up tier (147 s, 138 s); outcome approved on attempt 3 |
| LIVE | live | deltas | +8.13 % (slower) | | -60.01 % (fewer) | | | retained: true, missing: none | `thresholds_met: false`: tokens threshold (40 %) met at 60 %; wall-clock threshold (30 %) NOT met, the tranche was 8 % slower on n=1, its two Haiku follow-up reviews (about 140 s each) cost more wall clock than the Codex reviews they replaced (about 35 s each) while the Architect sessions dominate both arms. Caveat: Claude Code `usage.input_tokens` excludes cache-read and cache-creation tokens, so Architect and Haiku input is under-counted in both arms equally; the token delta is dominated by three Codex reviewer inputs of about 100k tokens in the baseline versus one in the tranche. |

STUB rows are the output of one `python3 tools/benchmark_design_phase.py
--stub` on 2026-09-14 against this repository at commit `a4f9acf`. They
prove that the harness drives both arms end to end (the tranche arm's
second reviewer ran on the follow-up tier with a delta packet and reused
both cached evidence artifacts), that the usage parser reads both runner
shapes, and that the retention check works. They say nothing about the
real cost delta.

The LIVE rows are filled by pasting a `--live` run's `summary.json`
medians, with the per-session model profiles, attempt counts, tokens,
wall-clock boundaries, cache state, and commands available in the two
`run.json` files, and a plain statement of whether the thresholds were met
or an honest miss.
