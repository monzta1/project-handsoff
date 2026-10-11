// #456: a verified view-only dashboard is the run's dashboard too. Fleet links
// the card to it and labels the link VIEW ONLY; an owned dashboard's link is
// unchanged.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");

// The page loaded whole, as fleet_sentinel_queue.test.js does.
function loadFleetPage() {
  const elements = {};
  const element = () => {
    const classes = new Set();
    return {
      textContent: "", innerHTML: "", value: "", dataset: {}, open: false, classes,
      classList: {
        toggle(name, on) { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); },
        add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      },
      addEventListener() {}, querySelector: element, showModal() {},
    };
  };
  const context = {
    document: { getElementById: (id) => (elements[id] ||= element()), querySelectorAll: () => [], body: element() },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {},
    Date, JSON, Math, Number, String, Array, Boolean, Set, Object, Promise, Error,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "dashboard/lib/run-vocabulary.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(root, "fleet/app.js"), "utf8"), context, { filename: "fleet/app.js" });
  return context;
}

function project(owner) {
  return {
    root: "/tmp/lane", state: "running", initialized: true, feature: "Sentinel lane", phase: "Implementation",
    progress: 40, updated_at: new Date().toISOString(), owner,
    dashboard_url: owner && owner.health === "healthy" ? `http://127.0.0.1:${owner.port}/` : null,
    dashboard_note: owner ? "" : "no run-owned dashboard",
  };
}

const anchor = (markup) => (markup.match(/<a class="open-dashboard"[^>]*>[^<]*<\/a>/) || [null])[0];

test("#456: a view-only dashboard's card links to it and labels the link view only", () => {
  const page = loadFleetPage();
  const markup = page.projectCard(project({ port: 8812, health: "healthy", view_only: true }));
  assert.equal(anchor(markup),
    '<a class="open-dashboard" href="http://127.0.0.1:8812/" target="_blank" rel="noopener">OPEN DASHBOARD · VIEW ONLY</a>');
});

test("#456: an owned dashboard's link is unchanged", () => {
  const page = loadFleetPage();
  const markup = page.projectCard(project({ port: 8813, health: "healthy" }));
  assert.equal(anchor(markup),
    '<a class="open-dashboard" href="http://127.0.0.1:8813/" target="_blank" rel="noopener">OPEN DASHBOARD</a>');
  assert.equal(markup.includes("VIEW ONLY"), false);
});

test("#456: without a dashboard there is no link and no view-only label", () => {
  const page = loadFleetPage();
  const markup = page.projectCard(project(null));
  assert.equal(anchor(markup), null);
  assert.equal(markup.includes("VIEW ONLY"), false);
  assert.match(markup, /no run-owned dashboard/);
});
