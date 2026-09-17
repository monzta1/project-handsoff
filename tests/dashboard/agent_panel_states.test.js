const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("panel publishes timing fields and the consistency fault", () => {
  const html = read("dashboard/index.html");
  for (const id of ["agent-output-timing", "consistency-fault"]) assert.match(html, new RegExp(`id="${id}"`));
});

test("app maps every output state", () => {
  const app = read("dashboard/app.js");
  for (const state of ["connected_no_output", "active_output", "stale_heartbeat", "transport_disconnected", "completed"]) assert.match(app, new RegExp(`${state}:`));
  assert.doesNotMatch(app, /Managed process is running quietly/);
});

test("styles define the muted state palette", () => {
  const css = read("dashboard/styles.css");
  for (const state of ["connected_no_output", "active_output", "stale_heartbeat", "transport_disconnected", "completed"]) assert.match(css, new RegExp(`output-state-${state}`));
  assert.doesNotMatch(css, /#(?:0f0|00ff00)\b|\blime\b/);
});

test("dashboard serializes beacon age", () => {
  assert.match(read("bin/handsoff_lib.py"), /last_heartbeat_age_seconds/);
  assert.match(read("bin/handsoff_dashboard.py"), /"agent_output": agent_output/);
});
