const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// #153: fleet/metrics.js rendered in a vm context with a small DOM that
// records every element, attribute and textContent write. The assertions
// are on what the Pilot would read: KPI values, legends, column widths,
// releases rows, the per-project breakdown, the daily table, and that
// tooltips are written with textContent only.
const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "fleet/metrics.js"), "utf8");
const css = fs.readFileSync(path.join(root, "fleet/styles.css"), "utf8");
const html = fs.readFileSync(path.join(root, "fleet/metrics.html"), "utf8");

function makeDom() {
  const byId = new Map();
  const created = [];
  class Element {
    constructor(name, ns) {
      this.name = name; this.ns = ns; this.attributes = {}; this.children = []; this.listeners = {};
      this.dataset = {}; this._text = ""; this._innerHTML = null; this.style = {}; this.value = "";
      this.classList = {
        toggle: (cls, force) => { const on = force === undefined ? !this.classes.has(cls) : force; on ? this.classes.add(cls) : this.classes.delete(cls); },
        add: (cls) => this.classes.add(cls), remove: (cls) => this.classes.delete(cls), contains: (cls) => this.classes.has(cls),
      };
      this.classes = new Set();
      created.push(this);
    }
    get className() { return [...this.classes].join(" "); }
    set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
    get id() { return this.attributes.id || ""; }
    set id(value) { this.attributes.id = value; byId.set(value, this); }
    get textContent() { return this._text; }
    set textContent(value) { this._text = String(value); this.children = []; }
    set innerHTML(value) { this._innerHTML = value; }
    get innerHTML() { return this._innerHTML; }
    setAttribute(key, value) {
      this.attributes[key] = String(value);
      if (key === "id") byId.set(String(value), this);
      if (key === "class") this.className = String(value);
    }
    getAttribute(key) { return this.attributes[key]; }
    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
    replaceChildren(...nodes) { this.children = []; nodes.forEach((node) => this.appendChild(node)); }
    get childNodes() { return this.children.slice(); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    querySelectorAll() { return []; }
    querySelector() { return null; }
    text() { return this.children.length ? this.children.map((c) => c.text()).join(" ") : this._text; }
    find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.find(pred, out)); return out; }
  }
  const body = new Element("body");
  const document = {
    body,
    createElement: (name) => new Element(name),
    createElementNS: (ns, name) => new Element(name, ns),
    getElementById: (id) => byId.get(id) || null,
    querySelectorAll: () => [],
  };
  for (const id of ["kpis", "charts", "releases", "breakdown", "table-wrap", "range-label", "source-note", "project", "custom-range", "from", "to", "toggle-table", "synced", "link"]) {
    const element = new Element(id === "project" ? "select" : "div");
    element.id = id;
  }
  return { document, body, byId, created, Element };
}

function loadPage() {
  const dom = makeDom();
  const context = {
    document: dom.document, module: { exports: {} }, Date, Math, Number, String, Map, Set, Array, Object, JSON,
    fetch: () => new Promise(() => {}), setInterval: () => 0, setTimeout: () => 0, console,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "fleet/metrics.js" });
  return { m: context.module.exports, dom };
}

const local = (y, mo, d, h = 12) => new Date(y, mo - 1, d, h).toISOString();
const issue = (number, created, closed = null) => ({ number, title: `Issue ${number} <b>`, html_url: `https://x/${number}`, created_at: created, closed_at: closed });

function fixtureData() {
  const since = local(2026, 8, 1);
  return {
    generated_at: local(2026, 9, 19), started_at: local(2026, 9, 19, 8), refreshed_at: local(2026, 9, 19, 9),
    projects: [
      { root: "/p/alpha", name: "alpha", repo: "o/alpha", fetched_at: local(2026, 9, 19, 9), error: null, commits_since: since,
        issues: [issue(1, local(2026, 9, 10), local(2026, 9, 12)), issue(2, local(2026, 9, 12), local(2026, 9, 12, 20)), issue(3, local(2026, 9, 13))],
        commits: [{ sha: "a", date: local(2026, 9, 12), message: "m" }, { sha: "b", date: local(2026, 9, 13), message: "m" }],
        releases: [{ tag_name: "v1.0", name: "One", html_url: "https://x/rel/1", published_at: local(2026, 9, 13) }] },
      { root: "/p/beta", name: "beta", repo: "o/beta", fetched_at: local(2026, 9, 19, 9), error: null, commits_since: since,
        issues: [issue(4, local(2026, 9, 11), local(2026, 9, 14))], commits: [], releases: [] },
      { root: "/p/gamma", name: "gamma", repo: null, fetched_at: null, error: "no GitHub origin remote", commits_since: null, issues: [], commits: [], releases: [] },
    ],
  };
}

function renderFixture(overrides = {}) {
  const { m, dom } = loadPage();
  m.state.data = fixtureData();
  m.state.project = "all";
  m.state.preset = "custom";
  m.state.from = "2026-09-11";
  m.state.to = "2026-09-14";
  Object.assign(m.state, overrides);
  const series = m.renderAll();
  return { m, dom, series };
}

test("the KPI row reads the totals for the range", () => {
  const { dom } = renderFixture();
  const tiles = dom.byId.get("kpis").children.map((tile) => [tile.attributes["data-kpi"], tile.children[1].textContent]);
  assert.deepEqual(tiles, [
    ["closed", "3"], ["opened", "3"], ["open", "1"], ["median", "2.0 d"], ["p90", "3.0 d"], ["commits", "2"], ["releases", "1"],
  ]);
  assert.equal(dom.byId.get("range-label").textContent.includes("4 DAYS"), true);
});

test("five charts render, the two-series ones carry legends, and columns respect the width cap and gap", () => {
  const { dom, m } = renderFixture();
  const charts = dom.byId.get("charts").children;
  assert.deepEqual(charts.map((panel) => panel.id), ["chart-throughput", "chart-intake", "chart-backlog", "chart-time-to-close", "chart-delivery"]);
  const legends = charts.map((panel) => panel.find((el) => el.classes.has("legend")).length);
  assert.deepEqual(legends, [1, 0, 0, 1, 1]);
  const legendText = charts[0].find((el) => el.classes.has("legend"))[0].text();
  assert.match(legendText, /Closed/);
  assert.match(legendText, /7-day rolling average/);
  let columnsSeen = 0;
  for (const panel of charts) {
    for (const column of panel.find((el) => el.name === "path" && el.classes.has("column"))) {
      columnsSeen += 1;
      const width = Number(column.attributes.d.match(/h(-?[\d.]+) a/)[1]) + 2 * Math.min(4, 24);
      assert.ok(width <= m.MAX_BAR + 0.001, `column wider than ${m.MAX_BAR}: ${width}`);
    }
    for (const line of panel.find((el) => el.classes.has("series-line"))) assert.equal(line.attributes.fill, "none");
  }
  assert.ok(columnsSeen >= 5, `expected columns on three charts, saw ${columnsSeen}`);
  assert.equal(m.BAR_GAP, 2);
  assert.match(css, /\.series-line \{ stroke-width: 2;/);
  assert.match(css, /\.gridline \{ stroke: var\(--line\); stroke-width: 1; \}/);
  assert.match(css, /--series-1: #0f95cc; --series-2: #1fae72;/);
  assert.match(css, /\.series-opened \.column \{ fill: var\(--faint\); \}/);
});

test("tooltips and every label are written with textContent, never innerHTML", () => {
  const { dom } = renderFixture();
  const written = dom.created.filter((el) => el.innerHTML !== null);
  assert.equal(written.length, 0);
  // A closed-issue dot's tooltip shows the issue title escaped by construction.
  const hit = dom.byId.get("chart-time-to-close").find((el) => el.classes.has("dot-hit"))[0];
  hit.listeners.pointermove[0]({ clientX: 10, clientY: 20 });
  const box = dom.byId.get("chart-tooltip");
  assert.equal(box.classes.has("hidden"), false);
  const rows = box.children.map((row) => row.children.map((c) => c.textContent));
  assert.equal(rows.some((row) => row[1].includes("<b>")), true);
  assert.equal(box.style.left, "24px");   // positioned through the CSSOM, never a style attribute
  // The crosshair band on the closed chart lists every series at that day.
  const band = dom.byId.get("chart-throughput").find((el) => el.classes.has("hit-band"))[1];
  band.listeners.pointermove[0]({ clientX: 1, clientY: 1 });
  const labels = box.children.map((row) => row.children[1].textContent);
  assert.deepEqual(labels.slice(1), ["closed", "7-day average"]);
});

test("the releases panel lists releases in range with links and the daily table carries commits", () => {
  const { dom } = renderFixture();
  const releases = dom.byId.get("releases");
  assert.match(releases.text(), /1 release in range/);
  const rows = releases.find((el) => el.name === "tr");
  assert.equal(rows.length, 2);
  const link = rows[1].find((el) => el.name === "a")[0];
  assert.equal(link.href, "https://x/rel/1");
  assert.equal(link.textContent, "v1.0");
  assert.equal(rows[1].children[0].textContent, "alpha");
  const table = dom.byId.get("table-wrap").find((el) => el.name === "table")[0];
  assert.deepEqual(table.children[0].children[0].children.map((th) => th.textContent), ["DATE", "OPENED", "CLOSED", "BACKLOG", "COMMITS"]);
  const body = table.children[1].children.map((tr) => tr.children.map((td) => td.textContent));
  assert.deepEqual(body, [
    ["2026-09-11", "1", "0", "2", "0"], ["2026-09-12", "1", "2", "1", "1"], ["2026-09-13", "1", "0", "2", "1"], ["2026-09-14", "0", "1", "1", "0"],
  ]);
});

test("the per-project breakdown appears on the All view only and skips projects without a repo", () => {
  const all = renderFixture();
  const breakdown = all.dom.byId.get("breakdown");
  assert.equal(breakdown.classes.has("hidden"), false);
  const rows = breakdown.find((el) => el.name === "tr").slice(1).map((tr) => tr.children.map((td) => td.textContent));
  assert.deepEqual(rows, [
    ["alpha", "o/alpha", "1", "2", "2", "1.2 d", "2", "1"],
    ["beta", "o/beta", "0", "1", "1", "3.0 d", "0", "0"],
  ]);
  const one = renderFixture({ project: "o/beta" });   // #160: the filter value is the repository identity
  assert.equal(one.dom.byId.get("breakdown").classes.has("hidden"), true);
  assert.equal(one.dom.byId.get("kpis").children[0].children[1].textContent, "1");
  assert.match(one.dom.byId.get("source-note").textContent, /1 ISSUES/);
});

test("#160: roots sharing one repository are one filter entry, one breakdown row and counted once", () => {
  const { m, dom } = loadPage();
  const data = fixtureData();
  const alpha = data.projects[0];
  // A lane worktree beside its checkout: same repo (different case in the remote), same data.
  data.projects.push({ ...alpha, root: "/p/alpha-lane-x", name: "alpha-lane-x", repo: "O/Alpha" });
  m.state.data = data;
  m.state.project = "all";
  m.state.preset = "custom";
  m.state.from = "2026-09-11";
  m.state.to = "2026-09-14";
  m.fillProjects(data);
  const options = dom.byId.get("project").children.map((option) => [option.value, option.textContent]);
  assert.deepEqual(options, [["all", "All projects"], ["o/alpha", "o/alpha (alpha, alpha-lane-x)"], ["o/beta", "beta (o/beta)"]]);
  const series = m.renderAll();
  // Counted once: alpha's 3 issues + beta's 1, not alpha's twice.
  assert.equal(series.totals.closed, 3);
  assert.equal(series.totals.commits, 2);
  assert.equal(series.totals.releases, 1);
  assert.match(dom.byId.get("source-note").textContent, /^4 ISSUES/);
  const rows = dom.byId.get("breakdown").find((el) => el.name === "tr").slice(1).map((tr) => tr.children.map((td) => td.textContent));
  assert.deepEqual(rows.map((row) => row[0]), ["alpha + alpha-lane-x", "beta"]);
  assert.deepEqual(rows[0].slice(1), ["o/alpha", "1", "2", "2", "1.2 d", "2", "1"]);
  const grouped = m.groupByRepo(data.projects);
  // The vm realm's arrays are not reference-equal to ours; compare the JSON.
  assert.deepEqual(JSON.parse(JSON.stringify(grouped.map((g) => [g.identity, g.roots]))),
    [["o/alpha", ["alpha", "alpha-lane-x"]], ["o/beta", ["beta"]], ["root:/p/gamma", ["gamma"]]]);
});

test("a project with a collector error is named in the source note and unknown commit days are blank", () => {
  const { dom } = renderFixture();
  assert.match(dom.byId.get("source-note").textContent, /gamma \(no GitHub origin remote\)/);
  const early = renderFixture({ from: "2026-07-30", to: "2026-08-02" });
  const note = early.dom.byId.get("chart-delivery").find((el) => el.classes.has("chart-note"));
  assert.equal(note.length, 1);
  assert.match(note[0].textContent, /earlier days are blank, not zero/);
  const body = early.dom.byId.get("table-wrap").find((el) => el.name === "tbody")[0].children.map((tr) => tr.children[4].textContent);
  // Aug 1 contains commits_since (noon), so it is unknown; only Aug 2 is a known day.
  assert.deepEqual(body, ["", "", "", "0"]);
});

test("the source note carries the GitHub budget when the collector reports it, and not otherwise", () => {
  const withBudget = renderFixture();
  assert.doesNotMatch(withBudget.dom.byId.get("source-note").textContent, /GITHUB BUDGET/);
  const { m, dom } = loadPage();
  m.state.data = { ...fixtureData(), rate_limit: { remaining: 4812, limit: 5000, reset_at: null } };
  m.state.project = "all";
  m.state.preset = "custom";
  m.state.from = "2026-09-11";
  m.state.to = "2026-09-14";
  m.renderAll();
  assert.match(dom.byId.get("source-note").textContent, /GITHUB BUDGET 4812 OF 5000$/);
});

test("the page markup carries the tab strip, the filter row and the CSP-safe script tag", () => {
  assert.match(html, /<nav class="tabs" aria-label="Fleet pages"><a href="\/">MISSIONS<\/a><a href="\/metrics" aria-current="page">METRICS<\/a><\/nav>/);
  assert.match(html, /data-preset="today"[\s\S]*data-preset="7"[\s\S]*data-preset="30" class="active"[\s\S]*data-preset="90"[\s\S]*data-preset="all"[\s\S]*data-preset="custom"/);
  assert.match(html, /<input id="from" type="date">/);
  assert.match(html, /<select id="project">/);
  assert.match(html, /<script src="\/metrics.js" defer><\/script>/);
  assert.doesNotMatch(html, /style="/);
  assert.doesNotMatch(source, /innerHTML\s*=/);
});
