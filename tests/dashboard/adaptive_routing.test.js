const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("canonical routing telemetry contains the complete per-mission record", () => {
  const logic = read("dashboard/lib/dashboard-logic.js");
  for (const field of [
    "tier", "model", "token_usage", "estimated_cost", "duration_ms",
    "escalation_reason", "repair_rounds", "review_rounds",
    "active_premium_scope", "outcome", "calls_by_tier",
  ]) assert.match(logic, new RegExp(`\\b${field}\\b`));
  assert.match(logic, /function adaptiveRoutingView/);
});

test("Mission Control exposes routing counters and explicit unavailable pause", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  const logic = read("dashboard/lib/dashboard-logic.js");
  for (const id of [
    "adaptive-routing-panel", "routing-tier", "routing-model", "routing-tokens",
    "routing-cost", "routing-duration", "routing-escalation", "routing-rounds",
    "routing-premium-scope", "routing-outcome", "routing-pause-detail",
    "routing-calls-fast", "routing-calls-standard", "routing-calls-premium",
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(app, /snapshot\.adaptive_routing \|\| snapshot\.routing/);
  assert.match(logic, /PAUSED/);
  assert.match(app, /Routing is paused/);
});
