const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Tranche 2 (#168 #169 #170 #171 #172): what the dashboards show.
const root = path.resolve(__dirname, "../..");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const metrics = require(path.join(root, "fleet/metrics.js"));
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");
const fleetApp = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");

test("the three new switches render through the existing FEATURES rows with no markup change (#168 #170 #171)", () => {
  const settings = { features: {
    token_accounting: { enabled: true, default: true, description: "usage" },
    review_binds_rules: { enabled: false, default: true, description: "rules" },
    report_posting: { enabled: false, default: false, description: "post" },
  } };
  const rows = logic.featureSwitchRows(settings);
  assert.deepEqual(rows.map((r) => [r.name, r.label, r.enabled, r.isDefault]), [
    ["token_accounting", "TOKEN ACCOUNTING", true, true],
    ["review_binds_rules", "REVIEW BINDS RULES", false, false],
    ["report_posting", "REPORT POSTING", false, true],
  ]);
});

test("repeatLabel: N/N, the failing attempt with its seed, or the pending count (#169)", () => {
  assert.equal(logic.repeatLabel({ repeat_view: { kind: "passed", attempts: 5, repeat: 5 } }), "5/5");
  assert.equal(logic.repeatLabel({ repeat_view: { kind: "failed", attempt: 3, repeat: 5, seed: "ab12cd34" } }), "failed at attempt 3 of 5 (seed ab12cd34)");
  assert.equal(logic.repeatLabel({ repeat_view: { kind: "failed", attempt: 2, repeat: 4, seed: null } }), "failed at attempt 2 of 4");
  assert.equal(logic.repeatLabel({ repeat_view: { kind: "pending", repeat: 10 } }), "repeat 10");
  assert.equal(logic.repeatLabel({}), "");
  assert.equal(logic.repeatLabel(null), "");
  assert.match(app, /criterion-repeat/);
  assert.match(css, /\.criterion-repeat\b/);
});

test("tokens per closed ticket: summed from recorded usage, never zero when nothing was recorded (#168)", () => {
  const closures = [{ issue: { number: 165 } }, { issue: { number: 166 } }, { issue: { number: 172 } }];
  const project = { tokens: { "165": { tokens_total: 9000, reported: true }, "166": { tokens_total: 9000, reported: true }, "172": { tokens_total: null, reported: false } } };
  const value = metrics.tokensForClosed(project, closures);
  assert.deepEqual(value, { total: 18000, reported: 2, closed: 3 });
  assert.equal(metrics.formatTokens(value), "18,000 (2 of 3 reported)");
  assert.equal(metrics.formatTokens({ total: 0, reported: 0, closed: 2 }), "not reported");
  assert.equal(metrics.formatTokens({ total: 0, reported: 0, closed: 0 }), "n/a");
  assert.equal(metrics.formatTokens(metrics.tokensForClosed({}, closures)).startsWith("not reported"), true);
  const rows = metrics.breakdownRows([{ name: "p", root: "/p", repo: "o/r", issues: [
    { number: 165, created_at: "2026-09-01T00:00:00Z", closed_at: "2026-09-10T00:00:00Z" },
  ], tokens: { "165": { tokens_total: 4200, reported: true } } }], "2026-09-01", "2026-09-20");
  assert.equal(metrics.formatTokens(rows[0].tokens), "4,200");
  assert.match(fs.readFileSync(path.join(root, "fleet/metrics.js"), "utf8"), /"TOKENS"/);
});

test("Fleet never draws a claimed-twice or failed mark from a stale reading; the card copy is what the server classified (#172)", () => {
  // the classification lives server-side (lib.live_status); the card only echoes state
  assert.doesNotMatch(fleetApp, /live\.state === "failed"/);
  assert.match(fleetApp, /data-state="\$\{esc\(state\)\}"/);
});
