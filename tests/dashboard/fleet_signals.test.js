const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const app = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "fleet/styles.css"), "utf8");

// #152: fleet/app.js is loaded whole into a vm context with just enough of a
// browser for its top level to run, then the real projectCard renders four
// projects. The assertions are on the text a Pilot would read, not on the
// source. No fetch ever resolves, so render() is never driven by the page.
function loadFleetPage() {
  const element = () => ({
    textContent: "", innerHTML: "", value: "", dataset: {}, open: false,
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, querySelector: element, showModal() {},
  });
  const context = {
    document: { getElementById: element, querySelectorAll: () => [], body: element() },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {},
    Date, JSON, Math, Number, String, Array, Boolean, Set, Object, Promise, Error,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "..", "dashboard", "lib", "run-vocabulary.js"), "utf8"), context, { filename: "dashboard/lib/run-vocabulary.js" });  // #218: loaded before app.js, as the page does
  vm.runInContext(app, context, { filename: "fleet/app.js" });
  return context;
}

const now = Date.now();
const iso = (secondsAgo) => new Date(now - secondsAgo * 1000).toISOString();
const base = (name, extra) => ({
  root: `/tmp/${name}`, name, registered_at: iso(86400), initialized: true, feature: `Feature ${name}`,
  phase: "Implementation", phase_number: 4, progress: 40, state: "running", decisions: [],
  engine_version: "v0.3.36", updated_at: iso(30), dashboard_url: null, dashboard_note: "no run-owned dashboard",
  ...extra,
});
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

test("a populated card reads counts, release with its age, beams and both cache ages", () => {
  const page = loadFleetPage();
  const html = page.projectCard(base("alpha", {
    github: { repo: "o/alpha", open_issues: 3, open_prs: 2, latest_release: { tag: "v1.4.0", published_at: iso(2 * 86400) },
              fetched_at: iso(4 * 60), error: null },
    beakon: { in_flight: 1, last: { receipt: "bk-1", outcome: "done", finished_at: iso(3 * 3600) }, fetched_at: iso(4 * 60) },
  }));
  const flat = text(html);
  assert.match(html, /<div class="signals">/);
  assert.match(flat, /GITHUB 3 ISSUES · 2 PRS · v1\.4\.0 2 d ago · CACHED 4 min ago/);
  assert.match(flat, /BEAKON 1 IN FLIGHT · LAST DONE 3 h ago · CACHED 4 min ago/);
  assert.doesNotMatch(flat, /ERROR/);
  // The strip sits in the card before the crew and port line.
  assert.ok(html.indexOf('class="signals"') < html.indexOf('class="project-meta"'));
});

test("a collector error keeps the last good numbers beside the error text", () => {
  const page = loadFleetPage();
  const flat = text(page.projectCard(base("beta", {
    github: { repo: "o/beta", open_issues: 8, open_prs: 0, latest_release: null, fetched_at: iso(2 * 3600), error: "GitHub HTTP 503" },
    beakon: { in_flight: 0, last: null, fetched_at: iso(60) },
  })));
  assert.match(flat, /GITHUB 8 ISSUES · 0 PRS · NO RELEASE · CACHED 2 h ago ERROR: GitHub HTTP 503/);
  assert.match(flat, /BEAKON 0 IN FLIGHT · NO BEAMS YET · CACHED 1 min ago/);
});

test("an unconfigured GitHub says so and a first-fetch failure shows its error", () => {
  const page = loadFleetPage();
  const unconfigured = text(page.projectCard(base("gamma", {
    github: { repo: "o/gamma", open_issues: null, open_prs: null, latest_release: null, fetched_at: iso(10), error: "GitHub is not configured" },
    beakon: null,
  })));
  assert.match(unconfigured, /GITHUB NOT CONFIGURED/);
  const failed = text(page.projectCard(base("delta", {
    github: { repo: null, open_issues: null, open_prs: null, latest_release: null, fetched_at: null, error: "no GitHub origin remote" },
    beakon: null,
  })));
  assert.match(failed, /GITHUB ERROR: no GitHub origin remote/);
});

test("a null Beakon signal renders no Beakon line and a missing GitHub signal reads pending", () => {
  const page = loadFleetPage();
  const html = page.projectCard(base("epsilon", { github: null, beakon: null }));
  assert.match(text(html), /GITHUB PENDING/);
  assert.doesNotMatch(html, /BEAKON/);
  assert.equal((html.match(/class="signal /g) || []).length + (html.match(/class="signal"/g) || []).length, 1);
});

test("signal text is escaped and the strip has styles in the shared palette", () => {
  const page = loadFleetPage();
  const html = page.projectCard(base("zeta", {
    github: { repo: "o/z", open_issues: 1, open_prs: 1, latest_release: { tag: "<b>x</b>", published_at: iso(5) }, fetched_at: iso(5), error: "<script>" },
    beakon: null,
  }));
  assert.doesNotMatch(html, /<b>x<\/b>/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.match(html, /ERROR: &lt;script&gt;/);
  assert.match(css, /\.signals \{ display: flex; flex-direction: column;/);
  assert.match(css, /\.signal b \{ color: var\(--accent\);/);
  assert.match(css, /\.signal-beakon b \{ color: var\(--emerald\); \}/);
  assert.match(css, /\.signal-error, \.signal-error-text \{ color: var\(--amber\);/);
});
