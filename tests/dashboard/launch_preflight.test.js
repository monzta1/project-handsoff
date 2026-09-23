const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");

test("Mission Control has one actionable preflight incident surface", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  for (const id of ["routing-preflight", "routing-preflight-detail", "routing-avoided-retries"])
    assert.match(html, new RegExp(`id="${id}"`));
  assert.match(app, /launchPreflight\.incident/);
  assert.match(app, /avoided_retries/);
  assert.match(app, /Exact adapter\/model check runs before session creation/);
});

test("preflight sidecar invalidates the dashboard snapshot", () => {
  assert.match(read("bin/handsoff_dashboard.py"), /lib\.PREFLIGHT_FILE/);
});
