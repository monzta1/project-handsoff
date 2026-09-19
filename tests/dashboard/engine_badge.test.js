const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// #161: one ENGINE badge in the topbar of both pages, fed by the Fleet
// payload's engine on Fleet and by the snapshot's engine on Mission Control,
// UNKNOWN until the first snapshot; Fleet cards mark engine drift.
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const fleetHtml = read("fleet/index.html");
const metricsHtml = read("fleet/metrics.html");
const dashHtml = read("dashboard/index.html");
const fleetApp = read("fleet/app.js");
const dashApp = read("dashboard/app.js");

test("both pages carry the badge span in the topbar meta, reading UNKNOWN before any data", () => {
  for (const html of [fleetHtml, metricsHtml, dashHtml]) {
    assert.match(html, /<span id="engine-badge" class="engine-badge" title="[^"]+">ENGINE UNKNOWN<\/span>/);
    const meta = html.indexOf('class="topbar-meta"');
    assert.ok(meta > 0 && html.indexOf('id="engine-badge"') > meta, "badge sits inside .topbar-meta");
  }
  for (const css of [read("fleet/styles.css"), read("dashboard/styles.css")]) {
    assert.match(css, /\.engine-badge \{ display: inline-flex;/);
  }
});

function fleetPage() {
  const byId = new Map();
  const element = (id) => {
    const el = { id, textContent: "", innerHTML: "", title: "", dataset: {}, value: "", classList: { toggle() {}, add() {}, remove() {}, contains: () => false }, addEventListener() {}, querySelector: () => element("x"), showModal() {} };
    if (id) byId.set(id, el);
    return el;
  };
  for (const id of ["engine-badge", "synced", "summary", "decisions", "decision-count", "decision-list", "fleet-count", "projects", "finished-runs", "finished-count", "finished-projects", "link", "offline-banner", "confirm", "toast", "fleet-clock"]) element(id);
  const context = {
    document: { getElementById: (id) => byId.get(id) || null, querySelectorAll: () => [], body: element("body") },
    localStorage: { getItem: () => null, setItem() {} }, fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} }, setTimeout: () => 0, setInterval: () => 0, clearTimeout() {},
    Date, JSON, Math, Number, String, Array, Boolean, Set, Object, Promise, Error,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fleetApp, context, { filename: "fleet/app.js" });
  return { context, byId };
}

const project = (name, engine) => ({
  root: `/tmp/${name}`, name, registered_at: "2026-09-19T00:00:00Z", initialized: true, feature: "F", phase: "Implementation",
  phase_number: 4, progress: 40, state: "running", decisions: [], engine_version: engine, updated_at: "2026-09-19T00:00:00Z",
  dashboard_url: null, dashboard_note: "", github: null, beakon: null,
});

test("the Fleet badge reads the server's engine and cards mark drift against it", () => {
  const { context, byId } = fleetPage();
  const data = { generated_at: "2026-09-19T00:00:00Z", engine: { version: "v0.3.41", source: "installed-engine" }, decisions: [],
    counts: {}, projects: [project("same", "v0.3.41"), project("older", "v0.3.39"), project("none", "unknown")] };
  context.render(data);
  assert.equal(byId.get("engine-badge").textContent, "ENGINE v0.3.41");
  assert.match(byId.get("engine-badge").title, /installed-engine/);
  const cards = byId.get("projects").innerHTML;
  assert.match(cards, /<article class="project" data-state="running">[\s\S]*?ENGINE v0\.3\.41<\/span>/);
  assert.match(cards, /<article class="project engine-drift"[\s\S]*?ENGINE v0\.3\.39 \(fleet v0\.3\.41\)<\/span>/);
  assert.match(cards, /ENGINE unknown<\/span>/);
  assert.doesNotMatch(cards, /ENGINE unknown \(fleet/);
  context.render({ ...data, engine: { version: "unknown", source: "unknown" } });
  assert.equal(byId.get("engine-badge").textContent, "ENGINE UNKNOWN");
  assert.doesNotMatch(byId.get("projects").innerHTML, /engine-drift/);
});

test("Mission Control sets the badge from the snapshot's engine, UNKNOWN when it is missing", () => {
  assert.match(dashApp, /const badge = \$\("engine-badge"\);/);
  assert.match(dashApp, /badge\.textContent = `ENGINE \$\{version && version !== "unknown" \? version : "UNKNOWN"\}`;/);
  assert.match(dashApp, /badge\.title = snapshot\.engine\?\.source \? `Engine this run uses \(\$\{snapshot\.engine\.source\}\)` : "Engine this run uses";/);
});
