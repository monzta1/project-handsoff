// #215: the replacement pause says what the previous Implementer finished.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { progressSummaryLabel } = require("../../dashboard/lib/dashboard-logic.js");

test("the label reads done with tests, partial and untouched", () => {
  const progress = { session_id: "hs-1", summary: { done: ["REQ-001", "REQ-002"], partial: ["REQ-004"], untouched: ["REQ-005", "REQ-006", "REQ-007"] },
    tests: { "REQ-001": "t1", "REQ-002": "t2" } };
  assert.equal(progressSummaryLabel(progress), "REQ-001, REQ-002 done with tests; REQ-004 partial; REQ-005, REQ-006, REQ-007 untouched");
  assert.equal(progressSummaryLabel({ ...progress, tests: { "REQ-001": "t1" } }), "REQ-001, REQ-002 done (1 with tests); REQ-004 partial; REQ-005, REQ-006, REQ-007 untouched");
  assert.equal(progressSummaryLabel({ session_id: "hs-2", summary: { done: [], partial: [], untouched: ["REQ-001"] }, tests: {} }), "REQ-001 untouched");
  assert.equal(progressSummaryLabel(null), "");
});

test("the page renders the progress row in the replacement panel", () => {
  const app = fs.readFileSync(path.join(__dirname, "..", "..", "dashboard", "app.js"), "utf8");
  assert.match(app, /renderReplacements\(state\.replacements, snapshot\.recovery\?\.attempts \|\| \[\], snapshot\.recovery\?\.implementer_progress \|\| null\)/);
  assert.match(app, /IMPLEMENTER PROGRESS · \$\{escapeHtml\(implementerProgress\.session_id\)\}/);
  assert.match(app, /panel\.classList\.toggle\("hidden", total === 0 && !implementerProgress\)/);
});
