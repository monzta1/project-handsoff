const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("operation panel has the bounded telemetry targets", () => {
  const html = read("dashboard/index.html");
  for (const id of ["operation-panel", "operation-state", "operation-meta", "operation-history"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
});

test("operation renderer names every supported visual state", () => {
  const app = read("dashboard/app.js");
  assert.match(app, /function renderOperation/);
  for (const state of ["waiting", "timed_out", "stale", "failed", "cancelled", "succeeded", "unavailable"]) {
    assert.match(app, new RegExp(`operation-state-${state}`));
  }
});

test("operation telemetry uses SSE watch and muted state styling", () => {
  const server = read("bin/handsoff_dashboard.py");
  const css = read("dashboard/styles.css");
  assert.match(server, /lib\.operations_path\(root\)/);
  assert.match(server, /"operation": operation/);
  assert.match(css, /\.operation-state-timed_out/);
  const rules = css.match(/\.operation-[^{]+\{[^}]*\}/g) || [];
  assert.doesNotMatch(rules.join("\n"), /lime|#0f0|#00ff00/i);
});
