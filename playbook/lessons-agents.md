# Lessons: managed sessions

Launching, waiting on, and bounding managed roles.

Each one cost a round. When a refusal, a wasted CI round or a stalled
session was caused by process (not the code), add one bullet to the
fitting file here, with the refusal line it prevents; a bullet that gets
broken twice becomes a refusal in the engine. Add, do not evict: if a
file no longer fits its budget, tests/test_playbook_budget.py says so
with the number.

- Never launch a reviewer before `design-propose` prints
  DESIGN_PROPOSAL_RECORDED; a killed launch leaves a live session for ten
  minutes.
- The implementation reviewer is launched from Phase 5: at Phase 4 its
  result is persisted but refused ("requires Phase 5"); `advance 5
  --implemented-by`, then `session-result-adopt --session <sid>` recovers it.
- Wait for a managed session on the LEDGER (the session's state leaving
  launching/running), never by grepping its log: the test suites the
  reviewer runs print `HANDSOFF_REVIEW_RESULT` and `SHIP_FEATURE_BLOCKED`
  lines of their own, and a grep on those wakes early with the wrong verdict.
- Never chain `design-propose && design-review-packet && launch` on a
  `grep -c` or any step that can print without succeeding: a refused
  proposal left the old one recorded and the reviewer reviewed that. The
  launch waits for `DESIGN_PROPOSAL_RECORDED` and the packet's JSON.
- Regenerate the runtime manifest between IMPLEMENTER launches, not only
  before a reviewer: the previous launch's edits to bin/ or schemas/ make it
  stale, and the next managed launch is refused outright and does no work.
- A managed implementer reports a criterion done having covered its first
  clause only. Read the criterion text against the tests before `verify`, or
  the ledger records something untrue: four criteria came back this way in one
  run, each a round.
- Size a delta review task to the delta: a budget-exhausted reviewer spends
  an attempt and returns nothing.
- Provider quota changes the vendor, not the role: route to another vendor and
  record `provider_quota`. A per-session token ceiling pauses without fallback.
- A run approaching two active hours is a performance incident, not a reason to
  hide time in a wait state. Warn at 90 minutes; at 120 checkpoint, fence late
  results, pause visibly, and require an explicit reevaluation decision.
- Bound the reviewer's exploration, not its budget. Five reviews, one lane:
  unbounded 49,264 tokens and no verdict; scope-bounded 18,107 with one; a
  delta packet 10,342; "run every suite" 88,487 and no verdict; "run one
  named module" 27,364 with tests_executed yes. Raising a ceiling fixed none.
- A native meter is evaluated BETWEEN turns, so it stops within one turn of
  the limit, not at it: one tool-heavy turn went 8,487 past the whole ceiling,
  which no protocol reserve can cover (a reserve carves from below it). State
  a bound's granularity; never call a per-turn bound a cap.
