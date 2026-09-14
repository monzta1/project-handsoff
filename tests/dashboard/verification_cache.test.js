// #43: Mission Control's evidence telemetry shows whether a verification
// record was executed (its commands launched for it) or reused (results
// copied from an earlier executed record of the same binding). A legacy
// record without the field gets no pill. Criterion
// i43-never-reuse-failures-live-regression.
// Run: node --test tests/dashboard/verification_cache.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  verificationExecutionState,
  verificationExecutionLabel,
} = require("../../dashboard/lib/dashboard-logic.js");

test("an executed record reads EXECUTED", () => {
  const record = { kind: "checks", ok: true, executed: true, reused_from: null };
  assert.equal(verificationExecutionState(record), "executed");
  assert.equal(verificationExecutionLabel(record), "EXECUTED");
});

test("a reused record reads REUSED and names its source run", () => {
  const record = { kind: "checks", ok: true, executed: false, reused_from: "vr-0123456789abcdef0123456789abcdef" };
  assert.equal(verificationExecutionState(record), "reused");
  assert.equal(verificationExecutionLabel(record), "REUSED · from vr-01234567");
});

test("a legacy record without the field gets no pill", () => {
  const record = { kind: "checks", ok: true };
  assert.equal(verificationExecutionState(record), null);
  assert.equal(verificationExecutionLabel(record), "");
  assert.equal(verificationExecutionState(null), null);
  assert.equal(verificationExecutionState({ executed: "yes" }), null);
});

test("the evidence list renders the pill from the shared helpers", () => {
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "../../dashboard/styles.css"), "utf8");
  assert.match(app, /verificationExecutionState\(record\)/);
  assert.match(app, /verificationExecutionLabel\(record\)/);
  assert.match(app, /class="execution-pill/);
  assert.match(css, /\.execution-pill\.executed/);
  assert.match(css, /\.execution-pill\.reused/);
});
