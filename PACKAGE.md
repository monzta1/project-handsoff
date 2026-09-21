# Project Handsoff

A delivery gate that ships one issue at a time with an evidence ledger, a
design gate, an independent reviewer, live verification and a release step.
Four roles (Architect, Supervisor, Implementer, Reviewer), eight phases, one
board per run and one Fleet page for every registered project.

Install the wheel of the current release into its own environment, then
`handsoff init <project>` and `handsoff doctor <project>`. The playbook that
ships with the engine (`handsoff playbook`) is the recipe a host follows for
a lane; `handsoff commands` prints the full command reference.

The documentation lives in the repository: README.md (the landing page),
INSTALL.md, docs/REFERENCE.md and docs/FIELD-NOTES.md.
