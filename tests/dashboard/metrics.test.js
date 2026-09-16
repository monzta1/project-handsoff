const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("Mission Control surfaces truthful run economics without estimating tokens", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  const server = read("bin/handsoff_dashboard.py");
  for (const id of ["metrics-panel", "metrics-elapsed", "metrics-sessions", "metrics-failures",
                    "metrics-reviews", "metrics-verification", "metrics-pilot-wait", "metrics-tokens",
                    "metrics-phase-list", "metrics-session-list"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /function renderMetrics\(metrics\)/);
  assert.match(app, /total == null \? "UNKNOWN"/);
  assert.match(app, /metrics\.largest_sessions/);
  assert.match(server, /metrics = lib\.build_run_metrics/);
  assert.match(server, /"metrics": metrics/);
});
