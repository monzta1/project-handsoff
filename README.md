<p align="center"><img src="dashboard/logo.png" alt="Handsoff" width="120"></p>

# Project Handsoff

A delivery gate that ships one issue at a time with an evidence ledger, a
design gate, an independent reviewer, live verification and a release step.
Four roles, eight phases, one board:

```text
Architect -> design review -> Supervisor -> Implementer -> Reviewer -> live verification -> release
```

The Supervisor owns the ledger, the phase gates, the acceptance evidence and
the escalations. The Implementer changes the project. The Reviewer judges the
diff independently, as a managed session the host cannot talk over. Nothing
advances without evidence on the ledger, and the ledger is what the
dashboard shows.

MIT licensed, see [LICENSE](LICENSE).

## Install

One versioned engine in its own environment; a project keeps only
`handsoff.toml` and `.handsoff-version`.

```bash
python3 -m pip install https://github.com/monzta1/project-handsoff/releases/download/v0.3.82/project_handsoff-0.3.82-py3-none-any.whl
handsoff init /absolute/path/to/project
handsoff doctor /absolute/path/to/project
```

[INSTALL.md](INSTALL.md) has the dedicated-environment setup, upgrades,
rollback and migration. `handsoff update` brings every tool of the house to
its latest release in one line (below).

## Run a lane

Every issue is one lane: a worktree off `main`, the board first, criteria,
a design the reviewer approves, implement, verify, an implementation review,
land through a pull request, live-verify against the installed engine,
close the issue with the written result. The playbook ships with the engine
and is what a host (a person, Claude Code, Codex) reads before starting:

```bash
handsoff playbook            # the index
handsoff playbook lanes      # the recipe, start to finish
handsoff playbook landing    # merge, release, install, verify-live, close
handsoff playbook lessons    # what earlier lanes learned
```

The board is Mission Control, one page per run:

```bash
handsoff supervisor --root /path/to/project init "Fix the thing" --by <you> --item "#123"
handsoff supervisor --root /path/to/project dashboard --owned-by-run --port 8790
```

Every new run uses adaptive model routing. `init` defaults to `--risk-class
routine`; pass `elevated` or another documented risk class when the work needs
stronger review or approval obligations. Mission Control shows exactly which
agent and phase received which tier, adapter, and model. Historical statuses
with no stored risk class show `NOT USED` until their next normal managed
launch safely defaults them to routine routing.

Every registered project sits on the Fleet page (`handsoff fleet serve`,
`http://127.0.0.1:8765/`), with a Metrics tab for issues, commits and
releases per repository.

Mission Control uses readable crew names such as **Solution Architect** and
**Implementation Reviewer**, while the adjacent run profile keeps the exact
provider, requested model, provider-reported model, session, and state. An
accepted full regression gets a large live progress bar, result counts, and
worker cards. Eligible Python unittest suites run in exactly five isolated
source snapshots by default. The dashboard and CLI both show `mode`,
`worker_count`, per-shard progress, and any serial `fallback_reason`. Mission
Control's sticky top bar keeps overall completion, completed criteria, and the
current test state visible while scrolling; it replaces the former Pilot-note
composer and duplicate bottom progress dock.

This repository intentionally uses `[execution] profile = "dogfood"`, which
names its local self-hosting waiver of the design and deployment clicks.
Generated projects default to `safe`; unattended, shared, and production
profiles refuse those waivers. Reviewer launches use one provider-neutral
contract: Codex gets an external write sandbox, a read-only project, sanitized
credentials, bounded subprocess lifetime, and denied network; adapters that
cannot enforce that boundary are refused before session reservation unless a
weaker compatibility mode is explicitly enabled and approved.

## The house

| Tool | What it does | Where |
|---|---|---|
| Handsoff | the engine: ledger, gates, reviewer, dashboards, releases | this repository |
| Fleet Mission Control | every registered project's run on one page, plus Metrics | `handsoff fleet serve` |
| The Miner | reads what runs left behind (archives, Beakon's run log) and files the issues, once | `monzta1/miner` |
| Sentinel | the receiver: watches a repository's board, hands labelled items to Handsoff over Beakon, reviews pull requests | `monzta1/sentinel` |
| Beakon | the task bus: a beam from anywhere lands on the machine that runs it, with a receipt | `monzta1/beakon` |

`handsoff update` brings all four to their latest releases on this machine
and restarts what runs here; `handsoff update --dry-run` says what it would
do. Never a downgrade, never over local changes, never a token.

## Where the rest lives

- [docs/REFERENCE.md](docs/REFERENCE.md): the eight phases, what is enforced,
  the crew, design evidence and delta packets, every workflow feature,
  review attempts and recovery, focused checks versus regressions, updating
  the house, the Miner, work items, known limitations, agent roles, importing
  into another project, self-hosting, and the release-cutting steps.
- [docs/FIELD-NOTES.md](docs/FIELD-NOTES.md): one entry per release that
  taught something, cause and fix.
- [INSTALL.md](INSTALL.md): install, upgrade, rollback, migration.
- [docs/FIELD-PROOF.md](docs/FIELD-PROOF.md), [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md),
  [docs/METRICS-SITE.md](docs/METRICS-SITE.md): the live proofs, the tunnel,
  the public metrics site.
- `handsoff commands`: the full command reference, generated from the parser.
