// #58: portable managed-agent output has one bounded, refresh-safe panel.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("Mission Control renders every portable output transport state", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  const css = read("dashboard/styles.css");
  for (const id of ["agent-output-panel", "agent-output-state", "agent-output-meta", "agent-output-log"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  for (const state of ["running_output", "running_quiet", "transport_disconnected", "failed", "completed"]) {
    assert.match(app, new RegExp(`${state}:`));
  }
  assert.match(app, /renderAgentOutput\(snapshot\.runtime\?\.agent_output \|\| null\)/);
  assert.match(app, /\.map\(\(entry\) =>/);
  assert.match(css, /\.agent-output-log/);
  assert.match(css, /\.output-state-transport_disconnected/);
});

test("the server invalidates SSE snapshots when the portable output file changes", () => {
  const server = read("bin/handsoff_dashboard.py");
  assert.match(server, /lib\.agent_output_path\(root\)/);
  assert.match(server, /agent_output = lib\.agent_output_view\(status, root\)/);
  assert.match(server, /"agent_output": agent_output/);
});
