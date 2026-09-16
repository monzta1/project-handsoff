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
