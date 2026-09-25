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
| Fleet registry | 3 | 5 | 2 | pending |
| **Model routing** | **36** | **14** | **11** | **extracted, v0.3.87** |
| Evidence and event ledger | 54 | 22 | 34 | pending |
| Storage and transactions | 34 | 33 | 72 | pending |
| Workflow state machine | 9 | 39 | 5 | pending |
| Agent runtime | 101 | 79 | 54 | pending |
| Dashboard projection | 102 | 83 | 20 | pending |

At the first extraction the monolith was 15,013 lines across 466 top-level
definitions.

## Staged extraction order, and why

Ordered by outbound coupling, because a subsystem that depends on little can
leave behind a small interface, while one that depends on everything drags
the monolith with it.

1. **Model routing** (done). Not the loosest, but the loosest that also has
   production callers and unblocks other work: the routing epic #299 to #305
   needs this seam. Fleet registry is smaller and would have proven less.
2. **Fleet registry** (3 symbols, 5 outbound). The smallest real boundary
   left. Its persisted state is `~/.handsoff/projects.json`, owned outside
   any project root.
3. **Evidence and event ledger** (54 symbols, 22 outbound). 34 inbound
   callers make this the first extraction where re-export alone is not
   enough; expect to repoint callers.
4. **Storage and transactions** (34 symbols, 72 inbound). The hub. Everything
   below waits on it, because `load_unique_json`, `status_path`,
   `project_lock` and `commit` are what the other subsystems call.
5. **Workflow state machine** (9 symbols, 39 outbound). Small but deeply
   dependent; it follows storage rather than leading it.
6. **Agent runtime** (101 symbols) and **dashboard projection** (102
   symbols). Last, and each is likely several lanes.

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

**Re-export keeps the monolith naming every moved symbol**, so extracting a
subsystem barely reduces the line count (15,013 to 14,475 for 36 symbols).
Line count is the wrong measure. What changes is that the boundary is
enforced: `tests/test_routing_boundary.py` holds the import allowlist,
refuses a definition no routing symbol reaches, and pins the re-export
surface to exactly the moved set.
