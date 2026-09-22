const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const { adaptiveRoutingView } = require("../../dashboard/lib/dashboard-logic.js");

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
    "routing-not-used", "routing-details", "routing-assignments", "routing-journey-map", "routing-journey-track", "routing-selections",
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(app, /renderAdaptiveRouting\(snapshot\.adaptive_routing \|\| null\)/);
  assert.doesNotMatch(app, /snapshot\.routing/);
  assert.match(logic, /NOT USED/);
  assert.match(app, /AGENT|routing-selection-agent/);
  assert.match(app, /DESIGN CHALLENGE/);
  assert.match(app, /IMPLEMENTATION AUDIT/);
  assert.match(app, /Not reported by provider/);
  assert.match(app, /adapter_reported/);
  assert.match(app, /matches route/);
  assert.match(app, /differs from routed/);
  assert.match(logic, /PAUSED/);
  assert.match(app, /Routing is paused/);
  assert.match(html, /MODEL HANDOFF JOURNEY/);
  assert.match(app, /PROVIDER HANDOFF/);
  assert.match(app, /MODEL SHIFT/);
  assert.match(app, /MODEL CONTINUES/);
  assert.match(app, /routing-map-transfer/);
  assert.match(app, /IN FLIGHT/);
  assert.match(read("dashboard/styles.css"), /routing-handoff-track/);
  assert.match(read("dashboard/styles.css"), /routing-map-orbit/);
  assert.match(read("dashboard/styles.css"), /\.routing-journey-map \{[^}]*overflow-x: auto/);
  assert.match(read("dashboard/styles.css"), /\.routing-journey-track \{[^}]*min-width: calc\(var\(--journey-legs\) \* 112px\)/);
  assert.match(app, /journeyTrack\.append\(mapStop\)/);
});

test("model handoff journey is deterministic and derives honest per-mission attempts", () => {
  const base = { adaptive: false, tier: null, adapter: "claude", model: "claude-opus-5", state: "completed" };
  const view = adaptiveRoutingView({ selections: [
    { ...base, session_id: "z-null", role: "reviewer", phase_number: 5, started_at: null, ended_at: null },
    { ...base, session_id: "b", role: "reviewer", phase_number: 5, started_at: "2026-09-22T18:00:00Z", ended_at: null },
    { ...base, session_id: "a", role: "reviewer", phase_number: 5, started_at: "2026-09-22T18:00:00Z", ended_at: "2026-09-22T18:01:00Z" },
    { ...base, session_id: "early", role: "architect", phase_number: 2, started_at: "2026-09-22T17:00:00Z", ended_at: "2026-09-22T17:01:00Z" },
  ] });
  assert.deepEqual(view.selections.map((item) => item.session_id), ["early", "a", "b", "z-null"]);
  assert.deepEqual(view.selections.map((item) => item.journey_index), [1, 2, 3, 4]);
  assert.deepEqual(view.selections.map((item) => item.journey_attempt), [1, 1, 2, 3]);
  assert.equal(view.selections[1].ended_at, "2026-09-22T18:01:00Z");
  assert.equal(view.selections[2].ended_at, null);
});
