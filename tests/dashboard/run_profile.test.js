// REQ-001: current-run identity/model-text display logic.
// Run: node --test tests/dashboard/run_profile.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  runProfileLabel,
  adapterLabel,
} = require("../../dashboard/lib/dashboard-logic.js");

test("assigned session shows adapter, requested and reported model, actor, and state", () => {
  const label = runProfileLabel("implementer", {
    runSessions: {
      implementer: {
        adapter: "claude",
        requested_model: "sonnet-5",
        reported_model: "claude-sonnet-5-20260201",
        actor: "claude-implementer-issue25",
        session_id: "sess-01",
        state: "running",
      },
    },
  });
  assert.equal(
    label,
    "THIS RUN: Claude Code · requested sonnet-5 · reported claude-sonnet-5-20260201 · claude-implementer-issue25 · sess-01 · RUNNING",
  );
});

test("a session with no reported_model shows honest 'exact model not reported' text, never a fabricated one", () => {
  const label = runProfileLabel("reviewer", {
    runSessions: {
      reviewer: {
        adapter: "codex",
        requested_model: "default",
        reported_model: null,
        actor: "reviewer-1",
        session_id: "sess-02",
        state: "running",
      },
    },
  });
  assert.match(label, /exact model not reported/);
  assert.doesNotMatch(label, /reported null/);
});

test("an unassigned role shows the vacant-station text, not a blank or crash", () => {
  const label = runProfileLabel("supervisor", { runSessions: {}, actors: {} });
  assert.equal(label, "THIS RUN: station not assigned · profile not recorded");
});

test("a role with a recorded actor but no live session shows profile-not-recorded plus the actor", () => {
  const label = runProfileLabel("architect", {
    runSessions: {},
    actors: { architect: "claude-architect-issue25" },
  });
  assert.equal(label, "THIS RUN: profile not recorded · claude-architect-issue25");
});

test("adapterLabel never fabricates a name for an unrecognized/missing adapter", () => {
  assert.equal(adapterLabel("codex"), "Codex");
  assert.equal(adapterLabel("claude"), "Claude Code");
  assert.equal(adapterLabel(null), "NONE DETECTED");
  assert.equal(adapterLabel(undefined), "NONE DETECTED");
});
