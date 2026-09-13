// REQ-007: per-role fallback list is manipulated by pure, boundary-safe
// logic (cap enforced in the logic itself, boundary moves are no-ops, not
// wraps), and round-trips through the settings draft/save serialization.
// Run: node --test tests/dashboard/fallback_order.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  addFallbackEntry,
  moveFallbackEntry,
  removeFallbackEntry,
  buildFallbackDraft,
  serializeFallbackDraft,
  MAX_FALLBACK_ENTRIES,
} = require("../../dashboard/lib/dashboard-logic.js");

test("adding refuses a 9th entry: the cap is enforced by the logic itself, not only a disabled button", () => {
  const entries = Array.from({ length: MAX_FALLBACK_ENTRIES }, () => ({ adapter: "codex", model: "default" }));
  const added = addFallbackEntry(entries);
  assert.equal(added, false);
  assert.equal(entries.length, MAX_FALLBACK_ENTRIES);
});

test("adding below the cap succeeds and appends a codex/default entry", () => {
  const entries = [{ adapter: "claude", model: "opus" }];
  const added = addFallbackEntry(entries);
  assert.equal(added, true);
  assert.deepEqual(entries, [
    { adapter: "claude", model: "opus" },
    { adapter: "codex", model: "default" },
  ]);
});

test("moving the first entry up is a no-op, not a wrap via negative splice index", () => {
  const entries = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const moved = moveFallbackEntry(entries, 0, -1);
  assert.equal(moved, false);
  assert.deepEqual(entries.map((e) => e.id), ["a", "b", "c"]);
});

test("moving the last entry down is a no-op, not a wrap", () => {
  const entries = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const moved = moveFallbackEntry(entries, 2, 1);
  assert.equal(moved, false);
  assert.deepEqual(entries.map((e) => e.id), ["a", "b", "c"]);
});

test("moving a middle entry up/down actually reorders", () => {
  const entries = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.equal(moveFallbackEntry(entries, 1, -1), true);
  assert.deepEqual(entries.map((e) => e.id), ["b", "a", "c"]);
  assert.equal(moveFallbackEntry(entries, 1, 1), true);
  assert.deepEqual(entries.map((e) => e.id), ["b", "c", "a"]);
});

test("removing works at any valid position; an out-of-range index is a no-op", () => {
  const entries = [{ id: "a" }, { id: "b" }];
  assert.equal(removeFallbackEntry(entries, 0), true);
  assert.deepEqual(entries.map((e) => e.id), ["b"]);
  assert.equal(removeFallbackEntry(entries, 5), false);
  assert.deepEqual(entries.map((e) => e.id), ["b"]);
});

test("a fallback list round-trips through draft-build and serialize without reordering or dropping entries", () => {
  const roles = ["architect", "supervisor", "implementer", "reviewer"];
  const savedFallbacks = {
    architect: [{ adapter: "codex", model: "default" }, { adapter: "claude", model: "opus" }],
    supervisor: [],
    implementer: [{ adapter: "claude", model: "sonnet-5" }],
    reviewer: [],
  };
  const draft = buildFallbackDraft(savedFallbacks, roles);
  const serialized = serializeFallbackDraft(draft, roles);
  assert.deepEqual(serialized, savedFallbacks);
});

test("mutating the draft does not mutate the original saved settings object (defensive copy)", () => {
  const roles = ["architect"];
  const savedFallbacks = { architect: [{ adapter: "codex", model: "default" }] };
  const draft = buildFallbackDraft(savedFallbacks, roles);
  addFallbackEntry(draft.architect);
  assert.equal(savedFallbacks.architect.length, 1);
  assert.equal(draft.architect.length, 2);
});
