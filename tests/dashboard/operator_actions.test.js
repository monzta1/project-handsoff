const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");

test("Mission Control initializes and executes state-bound operations without a terminal", () => {
  assert.match(html, /id="mission-init-form"/);
  assert.match(html, /id="operator-actions-panel"/);
  assert.doesNotMatch(html, /python3 bin\/handsoff_supervisor\.py init/);
  assert.match(script, /fetch\("\/api\/init"/);
  assert.match(script, /fetch\("\/api\/operator-action"/);
  assert.match(script, /action_id: action\.action_id/);
  assert.match(script, /Reason required/);
});

test("the decision the alert card carries is not listed a second time, and a blocked gate parks the button", () => {
  // Field report 2026-09-18: two AUTHORIZE DESIGN buttons on screen while
  // the gate refused for a reason the Pilot never saw.
  assert.match(script, /carriedByAlert = state\.inputRequired \? ALERT_KINDS\[state\.inputKind\] : null/);
  assert.match(script, /action\.kind !== carriedByAlert/);
  assert.match(script, /design_approval: "design_approve", deployment_approval: "deployment_approve"/);
  assert.match(script, /inputRequest\?\.blockers/);
  assert.match(script, /approvalButton\.textContent = "AUTHORIZATION BLOCKED"/);
  assert.match(script, /approvalButton\.title = blockers\.join\("; "\)/);
  // The server's refusal text is what the toast shows.
  assert.match(script, /throw new Error\(result\.error \|\| `Authorization returned \$\{response\.status\}`\)/);
});
