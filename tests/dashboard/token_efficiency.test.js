const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");

test("journey cards distinguish safe ceilings, packet estimates, actual usage, and selection basis", () => {
  const app = read("dashboard/app.js");
  for (const word of ["BUDGET", "PACKET", "USAGE", "WHY"]) assert.match(app, new RegExp(word));
  assert.match(app, /decision\.ceiling/);
  assert.match(app, /safe_minimum/);
  assert.match(app, /estimated_input_tokens/);
  assert.match(app, /packet_bytes/);
  assert.match(app, /tokens_total/);
  assert.match(app, /decision\.basis/);
});

test("budget telemetry has a legible visual treatment", () => {
  assert.match(read("dashboard/styles.css"), /\.routing-selection-budget/);
});
