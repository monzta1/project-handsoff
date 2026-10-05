const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// #373: the Fleet page's engine panel, driven through the page's own
// render(snapshot) into the #engine-panel target of the real index.html.
const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");
const vocab = fs.readFileSync(path.join(root, "dashboard/lib/run-vocabulary.js"), "utf8");
const app = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");

function element(id) {
  const classes = new Set();
  return {
    id, innerHTML: "", textContent: "", value: "", open: false, title: "", returnValue: "", dataset: {},
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      toggle: (name, on) => ((on ?? !classes.has(name)) ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    addEventListener() {}, showModal() {},
    querySelector: () => ({ textContent: "" }),
  };
}

// A minimal document whose getElementById answers only the ids the real
// fleet/index.html declares, so a panel the page lacks is never invented.
function loadPage({ drop = [] } = {}) {
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]).filter((id) => !drop.includes(id));
  const elements = Object.fromEntries(ids.map((id) => [id, element(id)]));
  const context = {
    module: { exports: {} }, console, Date, Math, Number, String, Array, Object, Set, JSON, Promise, Error,
    document: {
      getElementById: (id) => elements[id] || null,
      querySelectorAll: () => [],
      body: element("body"),
    },
    window: { setInterval() {} },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} },
    setTimeout() {},
  };
  vm.createContext(context);
  vm.runInContext(vocab, context, { filename: "run-vocabulary.js" });
  vm.runInContext(app, context, { filename: "fleet/app.js" });
  assert.equal(typeof context.module.exports.render, "function", "fleet/app.js exports the page's render");
  return { render: context.module.exports.render, elements };
}

function snapshot(engine) {
  return {
    generated_at: "2026-10-04T12:00:00+00:00", projects: [], decisions: [], counts: {},
    engine: { version: "0.5.1", source: "installed-engine", install_blocked: null, ...engine },
  };
}

const ROOT_OK = "/work/current";
const ROOT_LINE = "/work/previous";
const ROOT_EXACT = "/work/exact";

test("an engine in step: installed, latest, no BEHIND, every pin OK with no command", () => {
  const { render, elements } = loadPage();
  render(snapshot({
    installed: "0.5.1", manifest: "abc", manifest_status: "current", behind: false,
    latest: { tag: "v0.5.1", date: "2026-10-03T12:00:00Z", title: "Engine panel" },
    projects: [{ root: ROOT_OK, pin: "0.5.*", satisfied: true, upgrade: null }],
  }));
  const panel = elements["engine-panel"].innerHTML;
  assert.ok(panel.length > 0, "render mounted the engine panel into #engine-panel");
  assert.match(panel, /Installed 0\.5\.1/);
  assert.match(panel, /LATEST v0\.5\.1 · .+ · Engine panel/);
  assert.doesNotMatch(panel, /BEHIND/);
  assert.match(panel, new RegExp(`<li class="engine-pin" data-root="${ROOT_OK}">.*PIN 0\\.5\\.\\*.*<span class="pin-ok">OK</span></li>`));
  assert.doesNotMatch(panel, /REFUSED/);
  assert.doesNotMatch(panel, /handsoff upgrade/);
});

test("an engine behind a release: BEHIND, and each refused pin shows REFUSED and its exact upgrade command", () => {
  const { render, elements } = loadPage();
  render(snapshot({
    installed: "0.5.1", manifest: "abc", manifest_status: "current", behind: true,
    latest: { tag: "v0.6.0", date: "2026-10-03T12:00:00Z", title: "Next line" },
    projects: [
      { root: ROOT_OK, pin: "0.5.*", satisfied: true, upgrade: null },
      { root: ROOT_LINE, pin: "0.4.*", satisfied: false, upgrade: `handsoff upgrade ${ROOT_LINE} --to 0.5.*` },
      { root: ROOT_EXACT, pin: "v0.4.3", satisfied: false, upgrade: `handsoff upgrade ${ROOT_EXACT} --to v0.5.1` },
    ],
  }));
  const panel = elements["engine-panel"].innerHTML;
  assert.match(panel, /Installed 0\.5\.1/);
  assert.match(panel, /LATEST v0\.6\.0 · .+ · Next line/);
  assert.match(panel, /<span class="engine-behind">BEHIND<\/span>/);
  const rows = [...panel.matchAll(/<li class="engine-pin[^"]*" data-root="([^"]+)">(.*?)<\/li>/g)];
  assert.deepEqual(rows.map((row) => row[1]), [ROOT_OK, ROOT_LINE, ROOT_EXACT]);
  assert.doesNotMatch(rows[0][2], /REFUSED|handsoff upgrade/);
  assert.match(rows[1][2], /PIN 0\.4\.\*.*<span class="pin-refused">REFUSED<\/span><code class="pin-upgrade">handsoff upgrade \/work\/previous --to 0\.5\.\*<\/code>/);
  assert.match(rows[2][2], /PIN v0\.4\.3.*<span class="pin-refused">REFUSED<\/span><code class="pin-upgrade">handsoff upgrade \/work\/exact --to v0\.5\.1<\/code>/);
});

test("the page's render fails when fleet/index.html has no #engine-panel target", () => {
  assert.match(html, /<section id="engine-panel"/, "fleet/index.html declares the panel target");
  const { render } = loadPage({ drop: ["engine-panel"] });
  assert.throws(() => render(snapshot({ installed: "0.5.1", projects: [] })), /engine-panel/);
});
