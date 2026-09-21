const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// #181: the CI row under the phase rail. Pure text helpers from
// dashboard/lib/dashboard-logic.js, and renderCi from app.js run against
// a small DOM shim: hidden without a watch; pill, bar, elapsed/expected,
// one cell per check with its state, and the PR link with one.
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const app = read("dashboard/app.js");
const html = read("dashboard/index.html");
const css = read("dashboard/styles.css");

const shards = [0, 1, 2, 3, 4, 5].map((i) => ({ name: `python (shard ${i} of 6)`, state: i < 3 ? "SUCCESS" : "IN_PROGRESS",
  elapsed_seconds: i < 3 ? 70 + i : 40, link: `https://github.com/monzta1/x/actions/runs/1/job/${i}` }));
const running = { pr: 180, head: "abc", url: "https://github.com/monzta1/x/pull/180", state: "running",
  elapsed_seconds: 62, expected_seconds: 124, progress: 0.5, failed_check: null, note: null,
  checks: [...shards, { name: "dashboard", state: "SUCCESS", elapsed_seconds: 6, link: "https://github.com/monzta1/x/actions/runs/1/job/9" },
    { name: "tests", state: "PENDING", elapsed_seconds: null, link: null }] };

function page() {
  const byId = new Map();
  const element = (id) => {
    const el = { id, textContent: "", innerHTML: "", classes: new Set(), dataset: {}, attrs: {}, style: {},
      classList: { add(c) { el.classes.add(c); }, remove(c) { el.classes.delete(c); }, toggle(c, on) { on ? el.classes.add(c) : el.classes.delete(c); }, contains: (c) => el.classes.has(c) },
      setAttribute(k, v) { el.attrs[k] = v; }, removeAttribute(k) { delete el.attrs[k]; } };
    byId.set(id, el);
    return el;
  };
  for (const id of ["ci-status", "ci-state", "ci-link", "ci-progress-label", "ci-note", "ci-bar", "ci-bar-fill", "ci-cells"]) element(id);
  const start = app.indexOf("function renderCi(");
  const end = app.indexOf("\nfunction renderLiveAge(");
  assert.ok(start > 0 && end > start, "renderCi and renderCiClock are defined before renderLiveAge in app.js");
  const escapeStart = app.indexOf("function escapeHtml(");
  const escapeEnd = app.indexOf("\n}", escapeStart) + 2;
  // #181 Lane B: the row ticks from state.ci between snapshots; the clock is injectable
  const context = { $: (id) => byId.get(id) || null, Array, String, Number, Math, Date, state: { ci: null },
    ciStateLabel: logic.ciStateLabel, ciProgressLabel: logic.ciProgressLabel, ciBarPercent: logic.ciBarPercent,
    ciCellLabel: logic.ciCellLabel, ciNote: logic.ciNote, ciTicked: logic.ciTicked };
  vm.createContext(context);
  vm.runInContext(`${app.slice(escapeStart, escapeEnd)}\n${app.slice(start, end)}`, context);
  return { byId, context, render: (ci) => vm.runInContext(`renderCi(${JSON.stringify(ci)});`, context),
    tick: (ms) => vm.runInContext(`renderCiClock(state.ci.receivedAt + ${ms});`, context) };
}

test("the helpers read seconds, state, progress, cells and the note", () => {
  assert.equal(logic.ciSeconds(62), "1m 02s");
  assert.equal(logic.ciSeconds(59), "59s");
  assert.equal(logic.ciSeconds(124), "2m 04s");
  assert.equal(logic.ciSeconds(null), null);
  assert.equal(logic.ciStateLabel(running), "CI RUNNING");
  assert.equal(logic.ciStateLabel({ state: "passed" }), "CI PASSED");
  assert.equal(logic.ciStateLabel({ state: "failed" }), "CI FAILED");
  assert.equal(logic.ciProgressLabel(running), "50% · 4 of 8 checks done · 1m 02s of about 2m 04s");
  assert.equal(logic.ciProgressLabel({ ...running, expected_seconds: null, progress: null }), "50% · 4 of 8 checks done · 1m 02s elapsed");
  assert.equal(logic.ciProgressLabel({ ...running, state: "passed", elapsed_seconds: 124 }), "100% · passed in 2m 04s (last run 2m 04s)");
  assert.equal(logic.ciProgressLabel({ ...running, state: "failed", failed_check: "python (shard 4 of 6)", elapsed_seconds: 90 }), "python (shard 4 of 6) failed after 1m 30s");
  assert.equal(logic.ciBarPercent(running), 50);
  assert.equal(logic.ciBarPercent({ ...running, progress: null }), null);
  assert.equal(logic.ciBarPercent({ ...running, progress: 7 }), 100);
  assert.equal(logic.ciBarPercent({ state: "failed" }), 100);
  assert.equal(logic.ciCellLabel(shards[0]), "python (shard 0 of 6) 1m 10s");
  assert.equal(logic.ciCellLabel({ name: "tests", elapsed_seconds: null }), "tests");
  assert.equal(logic.ciNote(running), "", "#198: the count moved into the label");
  assert.equal(logic.ciNote({ ...running, note: "no previous run to compare" }), "no previous run to compare");
  assert.equal(logic.ciNote({ state: "passed", checks: [] }), "");
});

test("renderCi hides the row without a watch and fills it with one", () => {
  const { byId, render } = page();
  render(null);
  assert.ok(byId.get("ci-status").classes.has("hidden"));
  render(running);
  const strip = byId.get("ci-status");
  assert.ok(!strip.classes.has("hidden"));
  assert.equal(strip.dataset.state, "running");
  assert.equal(byId.get("ci-state").textContent, "CI RUNNING");
  assert.equal(byId.get("ci-link").textContent, "PR #180");
  assert.equal(byId.get("ci-link").attrs.href, "https://github.com/monzta1/x/pull/180");
  assert.equal(byId.get("ci-progress-label").textContent, "50% · 4 of 8 checks done · 1m 02s of about 2m 04s");
  assert.equal(byId.get("ci-note").textContent, "");
  assert.equal(byId.get("ci-bar-fill").style.width, "50%");
  assert.equal(byId.get("ci-bar").attrs["aria-valuenow"], "50");
  assert.ok(!byId.get("ci-bar").classes.has("is-indeterminate"));
  const cells = byId.get("ci-cells").innerHTML;
  assert.equal((cells.match(/class="ci-cell"/g) || []).length, 8, "one cell per check");
  assert.match(cells, /data-state="SUCCESS"[^>]*href="https:\/\/github\.com\/monzta1\/x\/actions\/runs\/1\/job\/0"[^>]*>python \(shard 0 of 6\) 1m 10s<\/a>/);
  assert.match(cells, /data-state="IN_PROGRESS"[^>]*>python \(shard 4 of 6\) 40s<\/a>/);
  assert.match(cells, /<a class="ci-cell" data-state="PENDING" title="PENDING">tests<\/a>/, "a check with no link is a cell without an href");
});

test("no history means an indeterminate bar; terminal states fill it and colour the pill", () => {
  const { byId, render } = page();
  render({ ...running, expected_seconds: null, progress: null, note: "no previous run to compare" });
  assert.ok(byId.get("ci-bar").classes.has("is-indeterminate"));
  assert.equal(byId.get("ci-bar-fill").style.width, "");
  assert.equal(byId.get("ci-note").textContent, "no previous run to compare");
  render({ ...running, state: "passed", progress: 1, elapsed_seconds: 124 });
  assert.equal(byId.get("ci-status").dataset.state, "passed");
  assert.equal(byId.get("ci-bar-fill").style.width, "100%");
  assert.equal(byId.get("ci-state").textContent, "CI PASSED");
  render({ ...running, state: "failed", progress: 1, failed_check: "python (shard 4 of 6)", elapsed_seconds: 90 });
  assert.equal(byId.get("ci-status").dataset.state, "failed");
  assert.equal(byId.get("ci-progress-label").textContent, "python (shard 4 of 6) failed after 1m 30s");
  render({ ...running, url: null, pr: null });
  assert.equal(byId.get("ci-link").textContent, "PR");
  assert.equal(byId.get("ci-link").attrs.href, undefined);
});

test("the page carries the row under the phase rail, app.js renders it from snapshot.ci, and the states are styled", () => {
  const rail = html.indexOf('id="phase-rail"');
  const row = html.indexOf('<section id="ci-status" class="ci-status hidden"');
  assert.ok(rail > 0 && row > rail, "the CI row sits after the phase rail");
  for (const id of ["ci-state", "ci-link", "ci-progress-label", "ci-note", "ci-bar", "ci-bar-fill", "ci-cells"]) assert.ok(html.includes(`id="${id}"`), id);
  assert.match(app, /renderCi\(snapshot\.ci \|\| null\);/);
  assert.match(app, /if \(!snapshot\.initialized\) \{\n    renderLive\(null\);\n    state\.ci = null;\n    renderCi\(null\);/);
  assert.match(app, /window\.setInterval\(renderCiClock, 1000\);/);
  for (const state of ["running", "passed", "failed"]) assert.match(css, new RegExp(`\\.ci-status\\[data-state="${state}"\\] \\.ci-pill`));
  assert.match(css, /\.ci-bar\.is-indeterminate \.ci-bar-fill/);
  assert.match(css, /\.ci-cell\[data-state="IN_PROGRESS"\]::before/);
});

test("the label ticks between snapshots from the server's elapsed and freezes on a terminal watch (#181, Lane B)", () => {
  const { byId, render, tick } = page();
  render(running);
  assert.equal(byId.get("ci-progress-label").textContent, "50% · 4 of 8 checks done · 1m 02s of about 2m 04s");
  tick(5000);
  assert.equal(byId.get("ci-progress-label").textContent, "54% · 4 of 8 checks done · 1m 07s of about 2m 04s");
  assert.equal(byId.get("ci-bar-fill").style.width, "54%");
  tick(90000);
  assert.equal(byId.get("ci-bar-fill").style.width, "100%", "capped at the estimate; the note says over");
  render({ ...running, state: "passed", progress: 1, elapsed_seconds: 124 });
  tick(30000);
  assert.equal(byId.get("ci-progress-label").textContent, "100% · passed in 2m 04s (last run 2m 04s)", "a terminal watch does not tick");
  assert.deepEqual(logic.ciTicked({ state: "running", elapsed_seconds: 10, expected_seconds: null, progress: null }, 5), { state: "running", elapsed_seconds: 15, expected_seconds: null, progress: null });
  assert.equal(logic.ciCellLabel({ name: "tests", state: "QUEUED", queued: true, elapsed_seconds: null }), "tests queued");
});
