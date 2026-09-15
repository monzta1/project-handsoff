const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const logic = require(path.join(__dirname, "../../dashboard/lib/dashboard-logic.js"));

test("tranche decision payload binds reordered retained items and explicit drops", () => {
  assert.deepEqual(logic.trancheDecisionPayload("abc", [
    {id: "issue-50", dropped: false}, {id: "issue-47", dropped: true},
    {id: "issue-51", dropped: false},
  ]), {proposal_hash: "abc", order: ["issue-50", "issue-51"], drops: ["issue-47"]});
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  assert.match(app, /\/api\/tranche-approval/);
  assert.match(app, /data-move="up"/);
  assert.match(app, /data-drop/);
  assert.match(logic.trancheIssueDetail({score_inputs: {bug: 40}, blockers: ["dependency_cycle"], cost_shape: null}), /bug 40.*dependency_cycle.*unknown/);
});
