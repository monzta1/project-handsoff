# Benchmark design task: `run-export`

You are working in a copy of the Project Handsoff repository. Design (do not
implement) a new Supervisor command, `handsoff_supervisor.py run-export`,
that writes one JSON bundle describing a finished run so an operator can
attach it to an issue without opening four ledger files.

The acceptance criteria are already seeded in `handsoff-acceptance.json`.
Do not add, remove, or rewrite criteria; the benchmark holds them fixed so
the two arms review the same registry. Refer to criteria by id.

## Proposed approach (the Architect refines this, the Reviewer critiques it)

1. `run-export --out PATH` loads `handsoff-status.json`,
   `handsoff-acceptance.json`, and both ledgers directly with
   `json.load` and `readlines`, then merges them into one dictionary.
2. The bundle carries, per agent session, the last 8192 characters of the
   session's stdout and stderr so a reader can see what each agent said.
3. The bundle is written with `open(PATH, "w")` followed by `json.dump`.
4. The event log's hash chain is re-verified before the bundle is written
   and the head hash is copied into the bundle.
5. The command refuses to run while a regression is launched.

## What the Architect returns

A design in prose: approach, data flow, failure modes, and how each seeded
criterion is satisfied. When you are revising after a review, list every
finding you addressed as a line `RESOLVED <finding id>: <what changed>`
(for example `RESOLVED F1.2: the bundle no longer carries session output`)
so the Supervisor can disposition it. Return the whole design each time,
not a diff.

## What the Reviewer returns

Actionable findings, one per line, each starting with `FINDING: `. Name the
concrete defect (what breaks, where, and why) rather than a category. Then
exactly one of `DESIGN_APPROVED` or `DESIGN_CHANGES_REQUESTED` on its own
line. Approve only when the design is implementation-ready against every
seeded criterion and the repository's settled rules (README "What is
actually enforced").
