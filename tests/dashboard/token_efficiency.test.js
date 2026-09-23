const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");

test("journey cards distinguish ceilings, packets, actual usage, and unknown cost", () => {
  const app = read("dashboard/app.js");
  for (const word of ["BUDGET", "PACKET", "USAGE", "COST"]) assert.match(app, new RegExp(word));
  assert.match(app, /budget_decision\.ceiling/);
  assert.match(app, /packet_bytes/);
  assert.match(app, /tokens_total/);
  assert.match(app, /not exposed/);
});

test("budget telemetry has a legible visual treatment", () => {
  assert.match(read("dashboard/styles.css"), /\.routing-selection-budget/);
});
