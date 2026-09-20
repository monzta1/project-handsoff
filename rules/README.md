# Launch rules (#167)

One JSON file per rule. The engine learns from its own run history: every
file here names the archived failure it came from (`cause`), the exact
situation it matches (`when`) and the refusal text the operator reads.
A matching launch rule refuses before any process is spawned, so it costs
no session, no attempt and no tokens. A matching packet rule refuses a
reviewer result at the packet boundary, names the field, and keeps the
verdict recoverable through `session-result-adopt` when the file says
`recover_as`.

Predicates (`when`): `command` is `launch` or `packet`; `role` is one of
the managed roles; `phase_in` is a list of phase numbers (launch rules);
`amendment` is true or false (launch rules; absent means either); `field`
names the packet field (packet rules); `allowed` lists its exact accepted
values (with optional `recover_as`, one of them), or `max_chars` bounds a
string or every string in a list (with optional `recover: "truncate"`).

`rules/proposed/` holds drafts written by `analyze-archives
--propose-rules`. Nothing there is evaluated: a human moves a draft up
one directory and commits it, or deletes it.

`[features].launch_rules = false` in a project's `handsoff.toml` skips the
evaluation for that project; the built-in field validation stays as the
floor.
