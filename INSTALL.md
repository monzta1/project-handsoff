# Install and upgrade Handsoff

Use one long-lived virtual environment and one stable command path. Projects then
contain configuration, a compatible version pin, and mission state, not a copied
Handsoff engine. Upgrading that single environment updates every compatible thin
project without leaving launch scripts pointed at an old release.

## Clean installation

Handsoff requires Python 3.11 or newer. These commands create a dedicated runtime
and expose `handsoff` at `$HOME/.local/bin/handsoff`:

```bash
python3 -m venv "$HOME/.local/share/handsoff/venv"
"$HOME/.local/share/handsoff/venv/bin/python" -m pip install --upgrade pip
"$HOME/.local/share/handsoff/venv/bin/python" -m pip install \
  "https://github.com/monzta1/project-handsoff/releases/download/v0.3.33/project_handsoff-0.3.33-py3-none-any.whl"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.local/share/handsoff/venv/bin/handsoff" "$HOME/.local/bin/handsoff"
```

Add `$HOME/.local/bin` to `PATH` once if it is not already there. Then initialize
each repository as a thin Handsoff project:

```bash
handsoff init /absolute/path/to/project
handsoff doctor /absolute/path/to/project
```

`init` defaults to a compatible patch pin such as `0.3.*`. That lets a project use
security and bug-fix releases within the same minor line. Pass `--pin v0.3.33` only
when the project must remain on one exact engine build.

## Clean patch upgrade

Install the new wheel into the same dedicated environment. Do not create a
version-named environment and do not change scripts, aliases, or LaunchAgents:

```bash
"$HOME/.local/share/handsoff/venv/bin/python" -m pip install --upgrade --force-reinstall \
  "https://github.com/monzta1/project-handsoff/releases/download/v0.3.33/project_handsoff-0.3.33-py3-none-any.whl"
handsoff version --json
handsoff doctor /absolute/path/to/project
```

A project pinned to the compatible line `0.3.*` (the `init` default) accepts the
new patch immediately: no pin bump, no edit inside the project, and no evidence
drift on its completed runs. A project still on an exact pin is moved to the
compatible line once, and never needs a per-patch bump again:

```bash
handsoff upgrade /absolute/path/to/project --to 0.3.* --dry-run
handsoff upgrade /absolute/path/to/project --to 0.3.*
```

Keep an exact pin only when the project must stay on one exact engine build
(strict reproducibility). Then, and only then, record each patch explicitly:

```bash
handsoff upgrade /absolute/path/to/project --to v0.3.33 --dry-run
handsoff upgrade /absolute/path/to/project --to v0.3.33
```

Replace `v0.3.29` with the release being installed. Instruction files that a
project keeps for its agents (`AGENTS.md`, a `SKILL.md`, restart prompts) should
name the compatible line, `0.3.*`, rather than an exact release: the
documentation audit flags an exact release reference that no longer matches the
installed engine, and editing those files after an upgrade is a product-tree
change that stales the evidence of every completed run. If they must name an
exact release, list them under `[digest] ignore` in `handsoff.toml` (see the
README, "Keeping write-ups out of the repository digest").

## After an upgrade: an in-flight run

If an upgrade finds a run that is still in progress, choose one of these actions:

- Recover keeps the run and its ledgers and retries the assigned role once:
  `handsoff supervisor --root /absolute/path/to/project recover --by ACTOR`
- Close keeps the ledgers, releases the dashboard, and marks the run closed:
  `handsoff supervisor --root /absolute/path/to/project run-close --by ACTOR --reason TEXT`
- Reopen restores a closed run at its recorded phase:
  `handsoff supervisor --root /absolute/path/to/project run-reopen --by ACTOR --reason TEXT`

## Minor or major upgrade

A new minor or major line is not activated implicitly. Install the new wheel,
preview the pin change, and activate it only after the preview is correct:

```bash
handsoff upgrade /absolute/path/to/project --to 0.4.* --dry-run
handsoff upgrade /absolute/path/to/project --to 0.4.*
handsoff doctor /absolute/path/to/project
```

If the installed engine cannot satisfy the requested pin, the preview reports the
required release and leaves the existing project pin untouched.

## Rollback

Reinstall the previous wheel into the same environment, then restore the previous
project pin from Handsoff's bounded pin history:

```bash
"$HOME/.local/share/handsoff/venv/bin/python" -m pip install --force-reinstall \
<!-- handsoff-doc: intentional -->
  "https://github.com/monzta1/project-handsoff/releases/download/v0.3.16/project_handsoff-0.3.16-py3-none-any.whl"
handsoff rollback /absolute/path/to/project --dry-run
handsoff rollback /absolute/path/to/project
handsoff doctor /absolute/path/to/project
```

Mission state, criteria, approvals, ledgers, archives, and Git history are not
deleted by upgrade or rollback.

## Migrate an older copied runtime

Older projects may contain Handsoff-owned `bin/`, `dashboard/`, `fleet/`,
`schemas/`, `templates/`, or `handsoff-runtime.json` paths. First install the
global engine above, then preview and perform the migration:

```bash
handsoff migrate /absolute/path/to/legacy-project --dry-run
handsoff migrate /absolute/path/to/legacy-project
handsoff doctor /absolute/path/to/legacy-project
```

The migration verifies the copied runtime before moving its runtime-only files to
`.handsoff/legacy-runtime/<version>/`. It preserves project configuration, source,
criteria, approvals, events, verification records, session history, and archives.
Modified role prompts survive only as explicit SHA-256-bound overrides. If a move
fails, Handsoff restores the files it already moved.

After migration, replace old invocations such as:

```text
<project>/bin/handsoff_supervisor.py
<version-specific-venv>/bin/handsoff
```

with the stable command:

```text
$HOME/.local/bin/handsoff
```

For example, a persistent Fleet Mission Control service should execute:

```bash
$HOME/.local/bin/handsoff fleet serve --port 8765
```

The Fleet registry stores project roots rather than engine paths, so it does not
need to be rebuilt after an engine upgrade.

## Prove that nothing points at an old release

Run these checks after installation or migration:

```bash
type -a handsoff
command -v handsoff
handsoff version --json
handsoff doctor /absolute/path/to/project
```

`command -v` should resolve to `$HOME/.local/bin/handsoff`. Review any additional
paths printed by `type -a` and remove obsolete aliases or links. `version --json`
must show the intended release. `doctor` must report `installed-engine`,
`migration_required: false`, the compatible project pin, and no unexpected
`legacy_runtime_paths`.
