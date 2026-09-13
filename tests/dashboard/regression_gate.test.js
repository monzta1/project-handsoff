const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("Mission Control renders an explicit regression Accept/Decline gate", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="regression-accept"/);
  assert.match(html, /id="regression-decline"/);
  assert.match(html, /id="regression-details"/);
  assert.match(html, /id="regression-alert"/);
  assert.match(html, /id="regression-last"/);
  assert.match(app, /fetch\("\/api\/regression-decision"/);
  assert.match(app, /command_sha256/);
  for (const field of ["content_sha256", "commit_pair", "requested_by", "expires_at", "completed_at"]) {
    assert.match(app, new RegExp(field));
  }
  assert.match(app, /regressionRecordText/);
  assert.match(app, /regression\?\.last/);
});
