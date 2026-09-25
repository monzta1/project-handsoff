// #333: the panel must say whether its header model ran or is only what
// routing would choose. The reported symptom was a header reading
// "SELECTED MODEL claude-haiku-4-5-20251001" on a run whose two sessions both
// ran codex/gpt-5.6-luna, because nothing had been routed and the builder
// computed a choice at read time.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const { adaptiveRoutingView, adaptiveRoutingModelLabel } = require("../../dashboard/lib/dashboard-logic.js");

test("the label distinguishes a recorded decision from a reported model from a projection", () => {
  assert.equal(adaptiveRoutingModelLabel({ header_source: "routed", model: "gpt-6-astra" }),
    "SELECTED MODEL");
  assert.equal(adaptiveRoutingModelLabel({ header_source: "reported", model: "gpt-5.6-luna" }),
    "MODEL THAT RAN");
  assert.equal(adaptiveRoutingModelLabel({
    header_source: "projection",
    would_route_to: { tier: "FAST", model: "claude-haiku-4-5-20251001" },
  }), "WOULD ROUTE TO");
});

test("the view carries header_source and would_route_to through", () => {
  const view = adaptiveRoutingView({
    used: true, header_source: "reported", model: "gpt-5.6-luna", tier: null,
    would_route_to: { tier: "FAST", adapter: "claude", model: "claude-haiku-4-5-20251001" },
  });
  assert.equal(view.header_source, "reported");
  assert.equal(view.model, "gpt-5.6-luna");
  assert.deepEqual(view.would_route_to,
    { tier: "FAST", adapter: "claude", model: "claude-haiku-4-5-20251001" });
});

test("a snapshot without the fields degrades rather than throwing", () => {
  const view = adaptiveRoutingView({ used: true, model: "gpt-6-astra" });
  assert.equal(view.header_source, null);
  assert.equal(view.would_route_to, null);
  assert.equal(adaptiveRoutingModelLabel({ used: true, model: "gpt-6-astra" }), "SELECTED MODEL");
});

test("the page has an element for the label, and app.js sets it", () => {
  // Assert the consumer, not the producer: a label computed and never
  // rendered leaves the page identical, which is the #319 shape.
  assert.match(read("dashboard/index.html"), /id="routing-model-label"/);
  const app = read("dashboard/app.js");
  assert.match(app, /set\("routing-model-label", adaptiveRoutingModelLabel\(routing\)\)/);
});

test("the header falls back to the projection only for display, never silently", () => {
  // app.js may show would_route_to's model, but only alongside the label that
  // says it is a projection. Both reads appear on adjacent lines.
  const app = read("dashboard/app.js");
  const labelAt = app.indexOf('set("routing-model-label"');
  const modelAt = app.indexOf('set("routing-model"');
  assert.ok(labelAt > -1 && modelAt > -1, "both the label and the model are set");
  assert.ok(labelAt < modelAt, "the label is set before the model it describes");
});
