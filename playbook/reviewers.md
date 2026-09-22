# Reviewers

The independent reviewer is the only gate. It is a managed session with an
identity that differs from the host's (`--by codex-reviewer` when the host
is `claude-host`, and the reverse), launched through `handsoff agent`, never
a subagent of the host's own session (the #112 gate refuses a shared host
session id).

The reviewer role is stable; its vendor is replaceable. When the selected
provider/model is at or out of account quota, the Supervisor chooses an
explicit equivalent-or-stronger model from another vendor without asking the
Pilot. The replacement gets a fresh actor and managed session, preserves the
same review kind, attempt and bounded delta packet, and must satisfy reviewer
independence plus the task's reasoning, context, tools and safety constraints.
The ledger and MODEL HANDOFF JOURNEY name both exact adapter/model identities
and `provider_quota` as the reason. Pause for the Pilot only when no eligible
cross-vendor reviewer exists or project policy prohibits all eligible choices.
An internal Handsoff per-session token-budget exhaustion is different: do not
spend again automatically.

**A design review task** names: the kind and attempt (`Design review
(kind: design), attempt 1`), the absolute root, the files the change will
touch with line numbers, the context in three sentences, the questions the
reviewer should judge, the finding limit (400 characters) and the verdict
form. A delta attempt names the packet (`design-review-packet
--disposition F1.1=resolved ...`) and says what changed per finding by
number; the reviewer judges only whether the findings are answered.

**An implementation review task** names the commit range (`git diff
main...HEAD`), the files, the test commands to run (`cd <abs> && python3
-m unittest tests.<module> -v && node --test ...`), the criteria and their
evidence, any deviation from the approved design stated as a deviation,
and the failure modes to look for. The reviewer runs the tests; its verdict
records `tests_executed`.

**A decline** (#177) is reviewed like a proposal, for its evidence and never
for the effort it saves: approve closes the run as not planned, changes send
it back to the Architect.

**What a reviewer catches that a host does not:** tonight's findings were
a client set narrower than the engine's, pending Pilot input mistaken for a
host's silence, a maintenance wake counted as a wake, a decline recorded
before its closure could fail. Answer each by number with a concrete change
and a test; never argue a finding away in prose.
