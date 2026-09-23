const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");

test("Mission Control renders hard model constraints and exact host identity", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  assert.match(html, /id="routing-policy"/);
  assert.match(app, /allowed_adapters/);
  assert.match(app, /denied_models/);
  assert.match(app, /GPT-5\.6 Sol|item\.model/);
  assert.match(app, /exact deployment variant not exposed/);
});

test("dashboard snapshot exposes the run policy rather than only mutable config", () => {
  const server = read("bin/handsoff_dashboard.py");
  assert.match(server, /status\.get\("model_policy"/);
  assert.match(server, /"model_policy"/);
});
