# Field proof: one clean run on a real thin project (#108)

A capability claim is not proof. This runbook produces a measured, ledger-backed
run of a low-risk feature on a real project with the installed engine, with no
hand-transcribed results and no replay, and posts the evidence.

## Before you start

- The project has a `handsoff.toml`, a `.handsoff-version` pin, check commands
  that pass on a clean tree, and at least one `live_commands` entry that
  exercises the deployed result.
- The installed engine is the release under test:

```bash
python3 -m pip install --upgrade "https://github.com/monzta1/project-handsoff/releases/download/vX.Y.Z/project_handsoff-X.Y.Z-py3-none-any.whl"
handsoff upgrade /abs/path/to/project --to X.Y.*
handsoff doctor /abs/path/to/project
```

A project already on the compatible line `X.Y.*` needs no `upgrade` call at
all; pass `--to vX.Y.Z` only when the proof must pin one exact build.

`doctor` must report `ok: true`, `prompt_overrides: []` (or only
`declared_current` entries), no `config-claim-contradiction` diagnostics, and
`run_triage: null` (no stranded run). Fix anything it names before continuing;
each fix is its own issue, linked from the #108 comment.

## The run

1. Start the run-owned dashboard and register the project with Fleet:

   ```bash
   handsoff dashboard --root /abs/path/to/project --port 8766 --owned-by-run
   handsoff fleet register /abs/path/to/project
   ```

2. In Mission Control, **Initialize mission** with a low-risk feature (a
   documented behaviour change with one automated criterion is ideal) and the
   issue number.
3. Let the crew run. The Pilot's only actions are the gates Mission Control
   exposes: **Authorize design**, **Authorize deployment**, and any
   **Authorize one review** when the design budget is exhausted. Do not run
   supervisor commands by hand; if you have to, that is a gap, and it is the
   most valuable finding of the exercise.
4. When the run reaches Phase 8, the dashboard releases its port and the run
   is archived.

## The evidence

Print the summary straight from the ledgers and paste it into the #108 comment:

```bash
python3 tools/run_evidence.py /abs/path/to/project
```

The row carries elapsed time, outcome, sessions (failed / replaced), design and
implementation review counts, executed versus reused checks, live runs, Pilot
gates, and every intervention kind with its count. Nothing in it is typed by
hand.

## What counts as a gap

Anything that made you leave Mission Control: a refused launch, a stranded
result you had to adopt, a pause you had to acknowledge, a review the budget
refused, a check that needed a manual rerun. File each as its own issue with
the ledger event kinds it produced, link it from the #108 comment, and keep the
run's archived ledgers.
