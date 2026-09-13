// REQ-002: Auto-detect is legible (shows its live resolved adapter) without
// ever silently converting the stored selection away from "auto".
// Run: node --test tests/dashboard/auto_detect.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  autoDetectOptionLabel,
  resolveAgentSelectValue,
} = require("../../dashboard/lib/dashboard-logic.js");

test("the Auto-detect option label names the live resolved adapter", () => {
  assert.equal(autoDetectOptionLabel("codex"), "Auto-detect (currently Codex)");
  assert.equal(autoDetectOptionLabel("claude"), "Auto-detect (currently Claude Code)");
});

test("Auto-detect label is still readable when nothing is detected", () => {
  assert.equal(autoDetectOptionLabel(null), "Auto-detect (currently NONE DETECTED)");
});

test("a stored 'auto' selection resolves to the 'auto' select value, never to the resolved adapter", () => {
  assert.equal(resolveAgentSelectValue("auto"), "auto");
});

test("legacy 'configure-me' storage resolves to 'auto', not a fabricated explicit adapter", () => {
  assert.equal(resolveAgentSelectValue("configure-me"), "auto");
});

test("an explicit stored adapter still resolves to itself (no override of a real choice)", () => {
  assert.equal(resolveAgentSelectValue("codex"), "codex");
  assert.equal(resolveAgentSelectValue("claude"), "claude");
});

test("an unrecognized stored adapter resolves to null so the UI shows a custom placeholder instead of silently coercing it", () => {
  assert.equal(resolveAgentSelectValue("some-unlisted-adapter"), null);
});
