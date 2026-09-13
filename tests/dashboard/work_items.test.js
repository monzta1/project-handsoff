const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  workItemStateLabel, workItemBlockerText, discrepancyLabel, showWorkItemTable,
} = require("../../dashboard/lib/dashboard-logic.js");

test("work-item labels cover every canonical state", () => {
  for (const state of ["done", "blocked", "in_review", "awaiting_approval", "recovering", "in_progress", "not_started"]) {
    assert.equal(workItemStateLabel(state), state.replaceAll("_", " ").toUpperCase());
  }
  assert.equal(workItemBlockerText({ status: "done" }), "Complete");
  assert.match(discrepancyLabel({ discrepancy: "GitHub still open" }), /^DISCREPANT:/);
  assert.equal(showWorkItemTable({ multi: false, items: [{}] }), false);
  assert.equal(showWorkItemTable({ multi: true, items: [{}, {}] }), true);
});

test("dashboard renders canonical work-item fields rather than configured ticket status", () => {
  const root = path.join(__dirname, "..", "..");
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /<th>Item<\/th><th>Issue<\/th><th>Title<\/th><th>Status<\/th>/);
  assert.match(app, /function renderWorkItems\(workItems\)/);
  assert.match(app, /data-item-id=/);
  assert.match(app, /item\.phase_or_next/);
  assert.match(app, /item\.blocker/);
  assert.match(app, /item\.discrepancy/);
  assert.doesNotMatch(app, /renderTickets\(snapshot\.tickets/);
});
