# Decomposing handsoff_lib.py

`bin/handsoff_lib.py` centralises storage, schema validation, workflow
transitions, evidence and ledger handling, agent runtime, model routing,
Fleet registry, dashboard projection, metrics, recovery and process
execution. A change in one concern therefore has a large blast radius.

This document is the dependency map and the staged plan (#284, criterion 1).
`tests/test_extraction_plan.py` recomputes every number here from `bin/` and
fails when the document and the code disagree, so the plan cannot rot into a
description of a codebase that no longer exists.

## How the numbers are derived, and how far to trust them

**Exact**, for an extracted subsystem: symbol count, outbound dependency
count and inbound caller count are read from the reference graph of the
extracted module against the monolith.

**Indicative**, for a subsystem still inside: members are grouped by keyword
match on symbol names, so a symbol can fall into two groups and the totals
overlap. Treat these as an ordering signal, not an inventory.

That caveat is not decoration. Grouping model routing by the name prefix
`adaptive_` reported 19 outbound dependencies; five symbols central to the
concern (`classify_adaptive_risk`, `evaluate_adaptive_budget`,
`_adaptive_model_reconciliation`, `bound_adaptive_escalation`,
`record_adaptive_check`) do not carry that prefix and read as external until
the grouping followed the reference graph instead. A sixth,
`OPENAI_MODEL_CATALOG_SOURCE`, was needed at module level where no deferred
import can help. Designing against the prefix numbers would have produced an
interface wrong by six symbols. **Re-measure from the graph before extracting
anything below.**

## Dependency map

| Subsystem | Symbols | Outbound | Inbound | State |
|---|---|---|---|---|
| **Core primitives** | **13** | **0** | hub | **extracted, stage 1** |
| **Configuration** | **45** | core, routing | many | **extracted, stage 2** |
| **Evidence and event ledger** | **54** | core, routing, config | 76 | **extracted, stage 3** |
| **Engine resources and briefing** | **14** | core, ledger | 16 | **extracted, stage 4** |
| **Agent runtime** | **154** | core, routing, config, ledger, resources | 57 | **extracted, stage 5** |
| Fleet registry | n/a | n/a | n/a | **already its own module** |
| **Model routing** | **36** | **14** | **11** | **extracted, v0.3.87** |
| Storage and transactions | 34 | 33 | 72 | pending |
| Workflow state machine | 9 | 39 | 5 | pending |
| Dashboard projection | 102 | 83 | 20 | pending |

At the first extraction the monolith was 15,013 lines across 466 top-level
definitions.

## Stage 1: the core primitives

`bin/handsoff_core.py` holds the thirteen symbols with **zero** outbound
dependencies on the engine: `HandsoffError` (121 inbound callers),
`_canonical`, `_atomic_write_text`, `durable_replace`, `load_unique_json`,
`project_lock`, `lock_path`, `status_path`, `acceptance_path`,
`durable_backup_path`, and the three content hashes `acceptance_hash`,
`criterion_spec_hash` and `design_hash`.

It exists so the layers above can import at module level. Model routing's
deferred imports, which the first extraction needed to avoid a cycle, are
ordinary imports for every primitive the core owns.

**A first attempt at this did not close.** Seeding from the obvious hubs
gave 93 symbols that pulled in `DEFAULT_CONFIG`, which embeds the adaptive
routing defaults, so that core depended on routing while routing depends on
core. Configuration therefore sits ABOVE routing, not below it. The
thirteen-symbol version has zero leaks, verified before extracting.

**A guarded import nearly became an unguarded one.** The monolith wraps
`import fcntl` in try/except so `project_lock` degrades to a documented
no-op where it is absent. The first draft of the core imported it bare,
which would have made every importer of the core die on a non-POSIX
platform while leaving the `if fcntl is None` branch as dead code.
`tests/test_core_layer.py` simulates that platform rather than trusting the
guard.

## Staged extraction order, and why

Ordered by outbound coupling, because a subsystem that depends on little can
leave behind a small interface, while one that depends on everything drags
the monolith with it.

1. **Model routing** (done). Not the loosest, but the loosest that also has
   production callers and unblocks other work: the routing epic #299 to #305
   needs this seam. Fleet registry is smaller and would have proven less.
2. **Core primitives** (done, stage 1). 13 symbols, zero outbound. The
   layer that let everything above it stop deferring imports.
3. **Configuration** (done, stage 2). 45 symbols. Sits ABOVE routing,
   because `DEFAULT_CONFIG` embeds the routing defaults.
4. **Evidence and event ledger** (done, stage 3). 54 symbols, 76 inbound
   callers: the point at which re-export carries the migration rather than
   hiding a rename.
5. **Fleet registry**: not pending. `bin/handsoff_fleet.py` already owns it,
   907 lines and 33 symbols including `registry_path`, `load_registry`,
   `forget_project` and `registry_lock`. #284 listed it because the review
   inferred subsystems from responsibilities rather than from the file
   layout, and the first version of this plan repeated that by grouping on
   keywords. Only `fleet_public_origins` remains in the monolith.
6. **Workflow state machine**, then **agent runtime** and **dashboard
   projection**. Seeding "workflow" by keyword produced a 188-symbol
   closure, which means the seeds reached half the file rather than a
   boundary. Seed it from the reference graph, as stage 1 was.

## Public interfaces and persisted schemas

| Boundary | Public interface | Persisted schema | Owner |
|---|---|---|---|
| Model routing | `route_adaptive_profile`, `adaptive_routing_profiles`, `adaptive_routing_budgets`, `adaptive_risk_policy`, `classify_adaptive_risk`, `adaptive_routing_snapshot` | `status.adaptive_routing`, `status.adaptive_escalation` (unchanged by extraction) | engine maintainer |
| Fleet registry | `fleet_registry_path`, register and forget | `~/.handsoff/projects.json` | engine maintainer |
| Evidence ledger | `commit`, `verify_log`, `evidence_drift` | `handsoff-events.jsonl`, `handsoff-verifications.jsonl`, `.handsoff-event-head.json` | engine maintainer |
| Storage | `load_unique_json`, `status_path`, `project_lock` | `handsoff-status.json`, `handsoff-acceptance.json` | engine maintainer |

No extraction may change a persisted schema. Where a subsystem owns one, the
extraction is contract-equivalent by construction: the same bytes are written
before and after.

## Remaining migration boundaries

**Model routing still imports the monolith.** Each function that needs a
primitive imports it by name inside its own body:

```python
from handsoff_lib import HandsoffError, load_config
```

Deferred rather than module-level, because `handsoff_lib` imports
`handsoff_routing` to re-export its symbols and a module-level import would
close that cycle. This is a recorded step, not a finished decoupling.

To lift it, these fourteen primitives must move into a shared core that both
modules import: `HandsoffError`, `load_config`, `load_unique_json`,
`status_path`, `actor_family`, `model_policy_allows`, `validate_model_policy`,
`_agent_assignment`, `_canonical_provider_model`, `PHASES`,
`AGENT_SESSION_LIVE_STATES`, `DEFAULT_AGENT_MODEL`, `DEFAULT_MODEL_POLICY`,
`SELECTABLE_AGENT_ADAPTERS`. That is stage 4 (storage) plus a small constants
module, and it is the point at which the deferred imports become ordinary ones.

An earlier draft of this list also named `acceptance_hash`, which the moved
code uses only as a local name and never imports. It was a primitive that
existed nowhere, overstating the remaining coupling by one.
`tests/test_routing_boundary.py` now compares the declared list against the
module's actual imports in both directions, because a rule that only asks
"is every import declared" cannot see an entry that names nothing.

## Re-export is not a shim for monkey-patching

`handsoff_lib` re-exports every moved symbol, so no caller changed. It does
NOT redirect attribute patching. An extracted module that does
`from handsoff_ledger import commit` binds that name at import time, so
`mock.patch.object(handsoff_lib, "commit")` no longer reaches it.

That surfaced in `test_telemetry_is_optional_non_gating_and_failure_safe`,
which patched `handsoff_lib.commit` to prove a write failure propagates and
leaves no state behind. It passed while `create_agent_session` lived in the
monolith and failed the moment it moved, because the assertion silently
stopped exercising the path it named. Tests that patch a moved symbol must
patch the module that now owns the call.

The alternative, importing modules rather than names (`import
handsoff_ledger` then `handsoff_ledger.commit(...)`), would keep a single
patch point at the cost of a module lookup per call. It is not done here.

What is done instead is `tests/engine_patch.patch_engine`, which replaces the
name on every module that binds it. Two tests had already failed this way
while staying green: one patched `lib.commit` while the commit ran inside
handsoff_agent_runtime, and the sleep tests patched `lib._read_pmset_log`
while handsoff_projection called its own binding and read the operator's real
pmset log, 281 intervals where the fixture supplied 1. Neither failed; both
simply stopped testing what they named.

There is no static rule that separates the broken patches from the working
ones. `lib.playbook_section` is defined in the monolith and calls
`playbook_index()` bare, so that resolves in the monolith's namespace and a
patch on `lib.playbook_index` does reach it; `handsoff_projection._read_pmset_log`
is called bare inside its own module, so a patch on the monolith does not.
Which one a test hits depends on the call path it drives, and four attempts at
deciding that from the syntax gave both false positives and false negatives.
So the rule is not "work out the call path" but "replace every binding":
`test_module_layers.py` derives the moved set from the trees and refuses any
`mock.patch.object(lib, "<moved name>")`, making the inert patch unexpressible
rather than merely detectable.

## Stage 7: the workflow state machine

The last of the seven subsystems #284 names. Forty-seven symbols, 1,110 lines:
the audit that decides whether a phase may be left (`compute_errors` and the
per-concern error functions it composes), the gates that refuse an action
outright (`lane_gate_refusal`, `ci_gate_errors`, `full_design_required`), the
criteria transaction that validates a whole batch of edits before any of it is
written, the work-item derivation the phases are scored against, and the launch
rules that bind to a phase.

The boundary was derived, not named: seeding the graph with the eight gate
functions the supervisor calls on the advance path and taking the transitive
closure gave 47 symbols with zero leaks, every dependency already in a layer
below. The same seeds against the pre-stage-5 tree would have dragged 188
symbols, which is the whole argument for extracting bottom-up.

`handsoff_workflow` is the decide side against `handsoff_projection`'s read
side: everything in it answers "may this state be written", and nothing in it
writes. `tests/test_workflow_layer.py` holds that as a rule -- the module
source may not contain `write_text(`, `durable_replace(` or `commit(` -- which
is what makes criterion 3, validate the proposed state before persisting it, a
property of the boundary rather than a habit at each call site.

**Re-export keeps the monolith naming every moved symbol**, so extracting a
subsystem barely reduces the line count (15,013 to 8,344 after seven extractions,
which is 44 percent out and still leaves the largest file in the tree).
Line count is the wrong measure. What changes is that the boundary is
enforced: `tests/test_routing_boundary.py` holds the import allowlist,
refuses a definition no routing symbol reaches, and pins the re-export
surface to exactly the moved set.
