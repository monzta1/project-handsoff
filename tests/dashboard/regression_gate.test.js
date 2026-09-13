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
  assert.match(app, /fetch\("\/api\/regression-decision"/);
  assert.match(app, /command_sha256/);
  assert.match(app, /repository\?\.head/);
  assert.match(app, /state\.regressionRequest\.commands\.join/);
});
