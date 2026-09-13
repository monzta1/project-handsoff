// REQ-003: the Flight Log's generic event display handles a future
// failover-shaped event (from issue #23, not yet landed) with no dashboard
// code change, as long as the event carries kind + message.
// Run: node --test tests/dashboard/event_log.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  cleanKind,
  eventMessage,
} = require("../../dashboard/lib/dashboard-logic.js");

test("a synthetic failover-shaped event renders its kind and message verbatim, no special-casing needed", () => {
  const failoverEvent = {
    kind: "agent_replaced",
    at: "2026-09-12T22:00:00Z",
    message:
      "implementer replaced: codex/default -> claude/sonnet-5 (category: non_zero_exit, attempt 2/2, state: recovered)",
  };
  assert.equal(cleanKind(failoverEvent.kind), "agent replaced");
  assert.equal(eventMessage(failoverEvent), failoverEvent.message);
});

test("cleanKind is readable for an ordinary event kind", () => {
  assert.equal(cleanKind("design_round_advanced"), "design round advanced");
});

test("cleanKind falls back to 'event' when kind is missing, never crashes or shows blank", () => {
  assert.equal(cleanKind(undefined), "event");
  assert.equal(cleanKind(null), "event");
  assert.equal(cleanKind(""), "event");
});

test("eventMessage falls back to an honest placeholder when message is missing", () => {
  assert.equal(eventMessage({ kind: "heartbeat" }), "Recorded state transition");
});
