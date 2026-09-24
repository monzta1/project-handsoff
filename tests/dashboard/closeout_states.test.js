// #296: Mission Control must be able to say "released" without saying "done".
//
// The v0.3.80 release was published and its issues closed while the run sat
// at Phase 7. The wheel was never installed and verify-live never ran, yet
// nothing on the board distinguished that from a delivered run.

const test = require("node:test");
const assert = require("node:assert");
const vocabulary = require("../../dashboard/lib/run-vocabulary.js");

const {
  STATE_ORDER, STATE_LABELS, FINISHED_STATES,
  CLOSEOUT_LADDER, UNVERIFIED_OUTCOMES, closeoutState, isSuccessfullyDelivered,
} = vocabulary;

test("the five closeout states are all distinguishable", () => {
  for (const state of ["released", "installed", "live_verified", "complete", "aborted"]) {
    assert.ok(STATE_ORDER.includes(state), `${state} is missing from the state order`);
    assert.ok(STATE_LABELS[state], `${state} has no label`);
  }
  const labels = ["released", "installed", "live_verified", "complete", "aborted"].map((s) => STATE_LABELS[s]);
  assert.strictEqual(new Set(labels).size, labels.length, "two closeout states share a label");
});

test("the ladder runs released, installed, live verified, complete", () => {
  assert.deepStrictEqual(CLOSEOUT_LADDER, ["released", "installed", "live_verified", "complete"]);
});

test("a published release that was never installed is not finished", () => {
  assert.ok(!FINISHED_STATES.has("released"));
  assert.ok(!FINISHED_STATES.has("installed"));
  assert.ok(!FINISHED_STATES.has("live_verified"));
});

test("complete and aborted are both finished", () => {
  assert.ok(FINISHED_STATES.has("complete"));
  assert.ok(FINISHED_STATES.has("aborted"));
});

test("each rung is reported from its own evidence", () => {
  assert.strictEqual(closeoutState({ released: true }), "released");
  assert.strictEqual(closeoutState({ released: true, installed: true }), "installed");
  assert.strictEqual(
    closeoutState({ released: true, installed: true, live_verification_id: "ver-1" }),
    "live_verified",
  );
  assert.strictEqual(
    closeoutState({ released: true, installed: true, live_verification_id: "ver-1", phase_number: 8, progress: 100 }),
    "complete",
  );
});

test("the v0.3.80 shape reads as released, not complete", () => {
  // Published, issues closed by hand, run still at Phase 7, nothing installed.
  const run = { released: true, phase_number: 7, progress: 70 };
  assert.strictEqual(closeoutState(run), "released");
  assert.strictEqual(isSuccessfullyDelivered(run), false);
});

test("phase 8 at 100 percent without a verification id is still not complete", () => {
  // The phase is a claim; the verification id is the evidence for it.
  const run = { released: true, installed: true, phase_number: 8, progress: 100 };
  assert.strictEqual(closeoutState(run), "installed");
  assert.strictEqual(isSuccessfullyDelivered(run), false);
});

test("an explicitly aborted run says so", () => {
  assert.strictEqual(closeoutState({ outcome: "aborted", released: true }), "aborted");
});

test("a released-unverified outcome reports the release it really made", () => {
  const run = { outcome: "released_unverified", released: true, phase_number: 7 };
  assert.strictEqual(closeoutState(run), "released");
  assert.strictEqual(isSuccessfullyDelivered(run), false);
});

test("the unverified outcomes are named and neither is complete", () => {
  assert.ok(UNVERIFIED_OUTCOMES.has("aborted"));
  assert.ok(UNVERIFIED_OUTCOMES.has("released_unverified"));
  assert.ok(!UNVERIFIED_OUTCOMES.has("closed"));
  for (const outcome of UNVERIFIED_OUTCOMES) {
    assert.strictEqual(isSuccessfullyDelivered({ outcome, phase_number: 8, progress: 100 }), false);
  }
});

test("a run with no closeout evidence reports nothing rather than guessing", () => {
  assert.strictEqual(closeoutState({}), null);
  assert.strictEqual(closeoutState(null), null);
  assert.strictEqual(closeoutState(undefined), null);
});

test("only the last rung counts as successfully delivered", () => {
  const delivered = { released: true, installed: true, live_verification_id: "ver-1", phase_number: 8, progress: 100 };
  assert.strictEqual(isSuccessfullyDelivered(delivered), true);
  for (const partial of [{ released: true }, { released: true, installed: true },
    { released: true, installed: true, live_verification_id: "ver-1" }]) {
    assert.strictEqual(isSuccessfullyDelivered(partial), false);
  }
});

test("the existing states are untouched", () => {
  for (const state of ["waiting", "failed", "offline", "stalled", "running", "quiet", "closed", "idle", "orphaned"]) {
    assert.ok(STATE_ORDER.includes(state), `${state} was dropped from the state order`);
    assert.ok(STATE_LABELS[state], `${state} lost its label`);
  }
});
