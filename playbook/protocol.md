# The managed-role protocol

One versioned document of every line a managed role prints and the host
reads (#217). The field sets below are the ones the code enforces:
`tests/test_protocol_contract.py` reads them from the validators and fails
when this document and the code disagree. A line the host cannot parse is a
protocol warning on the session, never a change to the ledger; a line it
can parse is validated, then dispatched under the host's own capability.
Every payload is one JSON object on one line after the prefix.

## HANDSOFF_BROKER_REQUEST (any managed role)

A request that the host broker turns into one `handsoff supervisor`
command, under the host's capability, on the project root the session was
launched for. The base fields on every request: `actor` (the session's
actor), `project_root` (must equal the launch root), `action` (always
`workflow`), `command` (one of the table). Commands that only a person
runs are refused whatever the fields: `amendment-approve`, `deployment-gate`, `design-approve`, `design-review-authorize`, `design-review-escalate`, `question-answer`, `recovery-acknowledge`, `regression-decide`, `regression-finalize`, `review-cap-override`.

```
HANDSOFF_BROKER_REQUEST: {"actor": "codex-implementer", "project_root": "/abs/root", "action": "workflow", "command": "verify", "by": "codex-implementer", "criteria": ["REQ-001"]}
```

| command | required beyond the base | optional |
|---|---|---|
| `advance` | `phase`, `progress` | `authorization_hold`, `implemented_by`, `next_action`, `status` |
| `amendment-escalate` | `by`, `reason` | none |
| `amendment-open` | `by`, `file` | `request_full_redesign`, `summary` |
| `amendment-review` | `by`, `decision`, `summary` | `adopted_by`, `adopted_session`, `findings` |
| `amendment-revise` | `by`, `file` | none |
| `background-wait-end` | `by` | `note`, `resume_after_authorization` |
| `background-wait-start` | `by` | `note`, `resume_after_authorization` |
| `criteria-apply` | `by`, `file` | `dry_run` |
| `design-evidence` | `evidence_action` | `by`, `force`, `ids` |
| `design-review-packet` | `by` | `dispositions` |
| `heartbeat` | `by`, `session` | `note` |
| `human-pause-end` | `by` | `note`, `resume_after_authorization` |
| `human-pause-start` | `by` | `note`, `resume_after_authorization` |
| `pilot-note` | `by`, `text` | none |
| `question-raise` | `by`, `role`, `text` | `session` |
| `record-design-review` | `architect`, `by`, `decision`, `summary` | `adopted_by`, `adopted_session`, `findings`, `session`, `structural_blocker` |
| `record-review` | `by` | `adopted_by`, `adopted_session`, `reaffirm`, `session`, `symptom_reproduced`, `tests_executed` |
| `record-review-findings` | `by`, `findings` | `adopted_by`, `adopted_session`, `session`, `tests_executed` |
| `record-symptom-resolved` | `by`, `evidence` | none |
| `recover` | `by` | `dry_run` |
| `regression-cancel` | `by`, `request_id` | none |
| `regression-request` | `by`, `group` | `reason` |
| `regression-run` | `by`, `request_id` | none |
| `review-attempt-start` | `by` | `note`, `reviewer`, `trigger` |
| `status` | none | none |
| `verify` | `by`, `criteria` | none |
| `work-item-activate` | `by`, `item` | none |
| `work-item-update` | `by`, `item` | `github_state`, `notes`, `title`, `url` |
| `work-items-sync` | `by` | `from_tickets` |

Values: `by` is the actor recording; `phase` and `progress` are integers;
`dry_run`, `force`, `reaffirm`, `from_tickets`, `resume_after_authorization`,
`request_full_redesign` and `structural_blocker` are booleans;
`tests_executed` is `yes`, `no` or `unknown`; `symptom_reproduced` is
`yes` or `not_applicable`; `decision` is `approved` or `changes_requested`;
`findings` is a list of strings; `criteria` a list of criterion ids;
`file` a path the host can read. A field outside the set, or a required
one missing, refuses the request naming it.

## HANDSOFF_REVIEW_RESULT (the Reviewer)

The verdict of a design or implementation review, at most 16384 bytes.

```
HANDSOFF_REVIEW_RESULT: {"kind": "implementation", "decision": "approved", "summary": "bounded rationale", "findings": [], "structural_blocker": false, "symptom_reproduced": "yes", "tests_executed": "yes"}
```

| field | values | required |
|---|---|---|
| `kind` | `design`, `implementation` | yes |
| `decision` | `approved`, `changes_requested` | yes |
| `summary` | 1 to 2048 characters | yes |
| `findings` | list of at most 32 strings, each at most 512 characters; empty when approved, non-empty when changes are requested | yes |
| `structural_blocker` | boolean; true only on a design review | yes |
| `symptom_reproduced` | `yes`, `not_applicable` | yes |
| `tests_executed` | `yes`, `no`, `unknown` (absent reads `unknown`) | no |

The host records it with `record-review` or `record-review-findings`
(implementation) or `record-design-review` (design); the result is
persisted on the session before dispatch, so a refused dispatch can be
adopted later with `session-result-adopt`.

## HANDSOFF_DESIGN_PROPOSAL (the Architect)

The six keys, every string at most 512 characters, every list at most 8
items (`approach`, `decisions` and `verification` at least 1); one approach
item begins `Data shape:` when a criterion introduces a persisted record.

```
HANDSOFF_DESIGN_PROPOSAL: {"summary": "...", "approach": ["Data shape: ...", "..."], "tradeoffs": ["..."], "decisions": ["..."], "constraints": ["..."], "verification": ["..."]}
```

| field | shape |
|---|---|
| `summary` | string, 1 to 512 characters |
| `approach` | list of 1 to 8 strings of at most 512 characters |
| `tradeoffs` | list of 0 to 8 strings of at most 512 characters |
| `decisions` | list of 1 to 8 strings of at most 512 characters |
| `constraints` | list of 0 to 8 strings of at most 512 characters |
| `verification` | list of 1 to 8 strings of at most 512 characters |

Recorded with `design-propose` semantics on the run; a revision after
findings answers every prior finding by number in its packet.

## HANDSOFF_DESIGN_DECLINE (the Architect)

The Architect declines the change (#177): the run closes as not planned
once the reviewer agrees.

```
HANDSOFF_DESIGN_DECLINE: {"reason": "why the change is not needed", "evidence": ["what shows it"], "alternative": "what to do instead, or null"}
```

| field | shape | required |
|---|---|---|
| `reason` | 1 to 512 characters | yes |
| `evidence` | list of at most 8 strings of 1 to 512 characters | no |
| `alternative` | 1 to 512 characters, or null | no |

## HANDSOFF_OPERATION (any managed role)

Telemetry for one external call the role makes (a GitHub API call, a
download): the host shows it on the board and bounds the stall clock by
its timeout. Never a change to the ledger.

```
HANDSOFF_OPERATION: {"operation_id": "op-ghdl", "dependency": "github_api", "operation": "download_release_asset", "state": "started", "attempt": 1, "timeout_seconds": 120}
```

| field | values | required |
|---|---|---|
| `operation_id` | `op-` and 4 to 32 lowercase letters or digits | yes |
| `dependency` | an identifier naming the external system | yes |
| `operation` | an identifier naming the call | yes |
| `state` | `started`, `succeeded`, `failed`, `timed_out`, `cancelled` | yes |
| `attempt` | integer, 1 or more | yes |
| `timeout_seconds` | integer, the call's own deadline | yes |
| `category` | one of the failure categories, on a failed state | no |

## HANDSOFF_PROGRESS (the Implementer, from #215)

One line per criterion as the Implementer finishes or abandons it, kept on
its session so a budget that runs out leaves an account and a relaunch
does not redo finished work.

```
HANDSOFF_PROGRESS: {"criterion": "REQ-001", "state": "done", "test": "python3 -m unittest tests.test_x -v", "note": ""}
```

| field | values | required |
|---|---|---|
| `criterion` | a criterion id | yes |
| `state` | `done`, `partial`, `untouched` | yes |
| `test` | the command run, at most 512 characters | no |
| `note` | at most 200 characters | no |

Until the engine that carries #215 is installed, the line is a protocol
warning like any other unknown line.
