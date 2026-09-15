const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const logic = require(path.join(__dirname, "../../dashboard/lib/dashboard-logic.js"));

test("work-item lane and progress labels are compact and bounded", () => {
  assert.equal(logic.workItemLaneLabel({ lane: "full" }), "FULL");
  assert.equal(logic.workItemLaneLabel({ lane: "small-fix" }), "SMALL FIX · UNCONFIRMED");
  assert.equal(logic.workItemProgressLabel({ progress: 101 }), "100%");
  assert.equal(logic.workItemProgressLabel({ progress: 37.6 }), "38%");
  assert.equal(logic.showWorkItemTable({items: [{lane: "small-fix"}], multi: false}), true);
  assert.match(logic.workItemLaneDetail({lane_facts: {criteria: 1, changed_lines: 20, changed_files: 2,
    caps: {criteria: 3, changed_lines: 200, changed_files: 6}}}), /1\/3 criteria.*20\/200 lines.*2\/6 files/);
});

test("dashboard offers only eligible unconfirmed small-fix confirmation", () => {
  assert.equal(logic.smallFixCanConfirm({ lane: "small-fix" }), true);
  assert.equal(logic.smallFixCanConfirm({ lane: "full" }), false);
  assert.equal(logic.smallFixCanConfirm({ lane: "small-fix", lane_escalation: "too large" }), false);
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  assert.match(app, /\/api\/lane-confirm/);
  assert.match(app, /workItemProgressLabel\(item\)/);
});
