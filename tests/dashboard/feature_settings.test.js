const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// #165 #167 #166: the WORKFLOW FEATURES group in the settings dialog.
const root = path.resolve(__dirname, "../..");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");

const settings = {
  features: {
    failing_first: { enabled: false, default: false, description: "Red before green (#165)." },
    launch_rules: { enabled: false, default: true, description: "Rules at launch (#167)." },
    ticket_lock: { enabled: true, default: true, description: "One ticket, one run (#166)." },
  },
};

test("featureSwitchRows: one row per feature in engine order, with label, enabled, default marker and description", () => {
  const rows = logic.featureSwitchRows(settings);
  assert.deepEqual(rows.map((r) => r.name), ["failing_first", "launch_rules", "ticket_lock"]);
  assert.deepEqual(rows[0], { name: "failing_first", label: "FAILING FIRST", enabled: false, isDefault: true, description: "Red before green (#165)." });
  assert.equal(rows[1].enabled, false);
  assert.equal(rows[1].isDefault, false, "launch_rules is off although its default is on");
  assert.equal(rows[2].isDefault, true);
  // an older engine's snapshot has no table: no rows, nothing to save
  assert.deepEqual(logic.featureSwitchRows({}), []);
  assert.deepEqual(logic.featureSwitchRows(null), []);
  assert.deepEqual(logic.featureSwitchRows({ features: "nope" }), []);
  // a malformed item never renders as enabled
  assert.equal(logic.featureSwitchRows({ features: { x: { enabled: "true" } } })[0].enabled, false);
});

test("featuresPayload is exactly the documented body: every known feature, a literal boolean each", () => {
  const rows = logic.featureSwitchRows(settings);
  const checked = { failing_first: true, launch_rules: undefined, ticket_lock: true };
  assert.deepEqual(logic.featuresPayload(rows, (name) => checked[name]), { failing_first: true, launch_rules: false, ticket_lock: true });
  assert.deepEqual(logic.featuresPayload([], () => true), {});
});

test("the dialog carries the group, the renderer draws the rows and the save posts to /api/settings/features", () => {
  assert.match(html, /id="feature-switches"/);
  assert.match(html, /id="features-save"/);
  assert.match(html, /WORKFLOW FEATURES/);
  assert.match(app, /fetch\("\/api\/settings\/features"/);
  assert.match(app, /\$\("features-save"\)\.addEventListener\("click", saveFeatureSettings\)/);
  for (const cls of ["feature-settings", "feature-switches", "feature-switch", "feature-changed"]) {
    assert.match(css, new RegExp(`\\.${cls}\\b`), `styles for .${cls}`);
  }
  // the real renderer against a stub DOM
  const start = app.indexOf("function renderFeatureSwitches(");
  const end = app.indexOf("\nasync function saveFeatureSettings(");
  assert.ok(start > 0 && end > start);
  const host = { innerHTML: "" };
  const save = { disabled: null };
  const $ = (id) => ({ "feature-switches": host, "features-save": save })[id];
  const escapeHtml = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const render = new Function("$", "escapeHtml", "featureSwitchRows", "state", app.slice(start, end) + "\nrenderFeatureSwitches();");
  render($, escapeHtml, logic.featureSwitchRows, { settings });
  assert.equal((host.innerHTML.match(/<label class="feature-switch">/g) || []).length, 3);
  assert.match(host.innerHTML, /data-feature="ticket_lock" checked/);
  assert.match(host.innerHTML, /data-feature="failing_first">/);
  assert.match(host.innerHTML, /LAUNCH RULES<\/strong> <em class="feature-changed">changed from default<\/em>/);
  assert.doesNotMatch(host.innerHTML, /TICKET LOCK<\/strong> <em/);
  assert.equal(save.disabled, false);
  render($, escapeHtml, logic.featureSwitchRows, { settings: {} });
  assert.equal(host.innerHTML, "");
  assert.equal(save.disabled, true);
});

test("baselineLabel: red beside the pass, the declared exception, or nothing (#165)", () => {
  assert.equal(logic.baselineLabel({ baseline_view: { kind: "recorded", at: "2026-09-20T10:11:12+00:00", run_id: "vr-1" } }), "red 2026-09-20");
  assert.equal(logic.baselineLabel({ baseline_view: { kind: "recorded" } }), "red recorded");
  assert.equal(logic.baselineLabel({ baseline_view: { kind: "not_applicable", reason: "born with the feature" } }), "no red: born with the feature");
  assert.equal(logic.baselineLabel({ baseline_view: { kind: "none" } }), "");
  assert.equal(logic.baselineLabel({}), "");
  assert.equal(logic.baselineLabel(null), "");
  // the criterion row carries it
  assert.match(app, /criterion-baseline/);
  assert.match(css, /\.criterion-baseline\b/);
});
