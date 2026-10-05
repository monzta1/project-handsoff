const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "fleet/styles.css"), "utf8");
const vm = require("node:vm");

// #389: the page loaded whole, as fleet_signals.test.js does, so the real
// projectCard renders from snapshot fields.
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
  vm.runInContext(fs.readFileSync(path.join(root, "dashboard/lib/run-vocabulary.js"), "utf8"), context);
  vm.runInContext(app, context, { filename: "fleet/app.js" });
  return context;
}
const card = (extra) => ({
  root: "/tmp/project-handsoff-lane-381-393", registered_at: new Date().toISOString(), initialized: true,
  feature: "Lane", phase: "Implementation", phase_number: 4, progress: 40, decisions: [], engine_version: "v0.5.6",
  updated_at: new Date().toISOString(), dashboard_url: null, dashboard_note: "no run-owned dashboard", ...extra,
});
const flat = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

test("#389: the card is named by the project, with the folder shown separately", () => {
  const page = loadFleetPage();
  const html = page.projectCard(card({ name: "project-handsoff", folder: "project-handsoff-lane-381-393", state: "running" }));
  assert.match(html, /<p class="project-name">project-handsoff <span class="project-folder">project-handsoff-lane-381-393<\/span>/);
  assert.match(css, /\.project-folder \{/);
});

test("#389: host_working reads HOST WORKING with its age; quiet still reads QUIET", () => {
  const page = loadFleetPage();
  const working = page.projectCard(card({ name: "p", folder: "p", state: "host_working", host_working_age_seconds: 185 }));
  assert.match(working, /data-state="host_working"/);
  assert.match(flat(working), /^HOST WORKING 3 min /);
  const fresh = page.projectCard(card({ name: "p", folder: "p", state: "host_working", host_working_age_seconds: 42 }));
  assert.match(flat(fresh), /^HOST WORKING 42 s /);
  const quiet = page.projectCard(card({ name: "p", folder: "p", state: "quiet", host_working_age_seconds: null }));
  assert.match(flat(quiet), /^QUIET /);
  assert.doesNotMatch(flat(quiet), /HOST WORKING/);
  assert.match(css, /\.project\[data-state="host_working"\] \{/);
});

// #150: ongoing runs are the main grid; completed and closed runs sit in a
// collapsed section below with a count, remembered per browser.
test("fleet splits ongoing and finished runs", () => {
  // #218: the set lives in the shared vocabulary the page loads first
  const vocab = fs.readFileSync(path.join(__dirname, "..", "..", "dashboard", "lib", "run-vocabulary.js"), "utf8");
  // #296 added "aborted" as finished, and deliberately kept "released",
  // "installed" and "live_verified" OUT of the set: a published release
  // whose artifact was never installed and verified is still in flight.
  assert.match(vocab, /const FINISHED_STATES = new Set\(\["complete", "closed", "aborted", "idle", "orphaned"\]\)/);
  assert.match(vocab, /idle: "NO RUN"/);
  assert.match(app, /const ongoing = projects\.filter\(\(project\) => !FINISHED_STATES\.has\(project\.state\)\)/);
  assert.match(app, /const finished = projects\.filter\(\(project\) => FINISHED_STATES\.has\(project\.state\)\)/);
  assert.match(app, /\$\("projects"\)\.innerHTML = ongoing\.length \? ongoing\.map\(projectCard\)/);
  assert.match(app, /\$\("finished-projects"\)\.innerHTML = finished\.map\(projectCard\)/);
  assert.match(app, /\$\("finished-count"\)\.textContent = `\$\{finished\.length\} RUN/);
  assert.match(app, /finishedSection\.classList\.toggle\("hidden", finished\.length === 0\)/);
  // The ordering is Fleet's existing state order (#389: host_working ranks
  // as running); counters and decisions are untouched.
  assert.match(app, /stateRank\(a\.state\) - stateRank\(b\.state\)/);
  assert.match(app, /return STATE_ORDER\.indexOf\(state === "host_working" \? "running" : state\);/);
  assert.match(app, /\$\("summary"\)\.innerHTML = STATE_ORDER\.map/);
  assert.match(app, /\$\("decision-list"\)\.innerHTML = data\.decisions\.map/);
});

test("the finished section is a collapsed disclosure with a count, remembered per browser", () => {
  assert.match(html, /<details id="finished-runs" class="finished-runs hidden">/);
  assert.match(html, /<summary><span class="panel-label">COMPLETED, CLOSED AND IDLE<\/span><span id="finished-count" class="section-meta">0 RUNS<\/span><\/summary>/);
  assert.match(html, /<div id="finished-projects" class="projects"><\/div>/);
  assert.match(html, /<p class="panel-label">ONGOING MISSIONS<\/p>/);
  assert.match(app, /localStorage\.getItem\("fleet\.finished\.open"\)/);
  assert.match(app, /section\.addEventListener\("toggle"/);
  assert.match(css, /\.finished-runs > summary::after \{ content: "SHOW"/);
  assert.match(css, /\.finished-runs\[open\] > summary::after \{ content: "HIDE"; \}/);
});

test("#389: the summary strip has a HOST WORKING tile, so a host-working run is counted where it is seen", () => {
  const vocabulary = require("../../dashboard/lib/run-vocabulary.js");
  assert.ok(vocabulary.STATE_ORDER.includes("host_working"));
  assert.equal(vocabulary.STATE_LABELS.host_working, "HOST WORKING");
  assert.ok(vocabulary.STATE_ORDER.indexOf("host_working") > vocabulary.STATE_ORDER.indexOf("running"));
  assert.ok(!vocabulary.FINISHED_STATES.has("host_working"));
});
