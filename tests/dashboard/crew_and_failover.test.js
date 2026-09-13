// Issue #25 correction: historical crew identities must not masquerade as
// verified provider/model assignments, and failover state must be explicit.
// Run: node --test tests/dashboard/crew_and_failover.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  crewProfileLabel,
  replacementHeadline,
  replacementDetail,
} = require("../../dashboard/lib/dashboard-logic.js");

test("an externally recorded actor is labelled unknown rather than inferred from its name", () => {
  assert.equal(
    crewProfileLabel({ key: "implementer", actor: "claude-implementer-issue25", session: null }),
    "External/manual launch · provider, model, and session not recorded",
  );
});

test("a managed crew member shows its recorded provider, models, session, and state", () => {
  const label = crewProfileLabel({
    key: "reviewer",
    actor: "reviewer-1",
    session: {
      adapter: "codex",
      requested_model: "gpt-5.4",
      reported_model: "gpt-5.4-2026-08-15",
      session_id: "hs-review",
      state: "completed",
    },
  });
  assert.equal(label, "Codex · gpt-5.4 · gpt-5.4-2026-08-15 · hs-review · COMPLETED");
});

test("human approval is never shown as an agent runtime profile", () => {
  assert.equal(
    crewProfileLabel({ key: "approver", actor: "Mission Control Pilot", session: null }),
    "Human authorization",
  );
});

test("failover telemetry shows both profiles, trigger, category, attempt, and state", () => {
  const replacement = {
    role: "implementer",
    state: "recovered",
    attempt: 1,
    cap: 2,
    trigger: "runtime_failure",
    category: "rate_limited",
    from_session_id: "hs-old",
    to_session_id: "hs-new",
    from_profile: { adapter: "codex", requested_model: "default" },
    to_profile: { adapter: "claude", requested_model: "sonnet" },
  };
  assert.equal(replacementHeadline(replacement), "IMPLEMENTER · RECOVERED · ATTEMPT 1/2");
  assert.equal(
    replacementDetail(replacement),
    "Codex default (hs-old) → Claude Code sonnet (hs-new) · runtime failure / rate limited",
  );
});
