# Recommended crews

Read when choosing `[agents]` at `init`. A recommendation only: a
project's own `[agents]` always wins and nothing here is enforced. More
agents are not automatically better; separate planning, implementation,
review and verification.

**Default.** Architect and Supervisor on the host (Claude or Copilot);
Implementer Codex; independent Reviewer Claude in a separate managed
session; browser QA optional and non-authoritative.

**Strongest independence.** Architect, Supervisor and Implementer Claude;
Reviewer Codex; live or browser verification by Playwright or a QA agent.

**High-risk infrastructure** (migrations, authorization, CI workflows,
shared infrastructure). Implementer Codex; code Reviewer Claude; a separate
security or config reviewer; deterministic checks plus live smoke tests;
the human owner approves the observed business outcome, so agents are
never the only approval layer.

**UI-heavy.** Implementer Codex; Reviewer Claude; browser tester (QA
agent); accessibility via Playwright plus axe-core; the final decision
rests with the Handsoff reviewer, plus the human for important workflows.
Generated QA tests stay untrusted evidence until reviewed.

**Small fix.** One implementer (Codex or Claude); a reviewer from a
different provider in a separate session; focused tests plus one smoke
check. No full multi-agent lane for a one-line, low-risk change.

**Rules.**
1. The same session never implements and reviews its own work.
2. Prefer different providers for implementation and final review.
3. The supervisor stays separate from the implementer.
4. Browser QA only when the change has meaningful UI behavior.
5. Security or data specialists only when the risk justifies the cost.
6. Decisions come from evidence, never agent confidence or prose.
7. Human confirmation for business behavior that tests cannot objectively
   establish.
