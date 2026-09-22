const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("canonical routing telemetry contains the complete per-mission record", () => {
  const logic = read("dashboard/lib/dashboard-logic.js");
  for (const field of [
    "used", "tier", "model", "token_usage", "estimated_cost", "duration_ms",
    "escalation_reason", "repair_rounds", "review_rounds",
    "active_premium_scope", "outcome", "calls_by_tier", "selections",
  ]) assert.match(logic, new RegExp(`\\b${field}\\b`));
  assert.match(logic, /function adaptiveRoutingView/);
});

test("Mission Control exposes assignments and explicit not-used state without a legacy fallback", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  const logic = read("dashboard/lib/dashboard-logic.js");
  for (const id of [
    "adaptive-routing-panel", "routing-tier", "routing-model", "routing-tokens",
    "routing-cost", "routing-duration", "routing-escalation", "routing-rounds",
    "routing-premium-scope", "routing-outcome", "routing-pause-detail",
    "routing-calls-fast", "routing-calls-standard", "routing-calls-premium",
    "routing-not-used", "routing-details", "routing-assignments", "routing-selections",
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(app, /renderAdaptiveRouting\(snapshot\.adaptive_routing \|\| null\)/);
  assert.doesNotMatch(app, /snapshot\.routing/);
  assert.match(logic, /NOT USED/);
  assert.match(app, /AGENT|routing-selection-agent/);
  assert.match(app, /DESIGN CHALLENGE/);
  assert.match(app, /IMPLEMENTATION AUDIT/);
  assert.match(app, /Not reported by provider/);
  assert.match(app, /adapter_reported/);
  assert.match(logic, /PAUSED/);
  assert.match(app, /Routing is paused/);
});
