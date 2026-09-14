// #33: Mission Control shows a live ship-feature status strip fed by
// snapshot.live: a state pill that pulses while a managed role is running,
// the role, "last activity N s ago" ticking locally every second, and the
// server's detail line. The pure label logic lives in dashboard-logic.js.
// Run: node --test tests/dashboard/live_status.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  LIVE_STATES,
  liveStateLabel,
  liveStatusView,
  liveAgeLabel,
} = require("../../dashboard/lib/dashboard-logic.js");

const read = (file) => fs.readFileSync(path.join(__dirname, "../..", file), "utf8");

test("every live state maps to its own pill label, tone, and pulse", () => {
  assert.deepEqual(LIVE_STATES, ["idle", "started", "running", "waiting", "stalled", "stopped", "failed", "complete"]);
  const expected = {
    idle: ["IDLE", "muted", false],
    started: ["STARTING", "good", true],
    running: ["RUNNING", "good", true],
    waiting: ["WAITING", "warning", false],
    stalled: ["STALLED", "bad", false],
    stopped: ["STOPPED", "muted", false],
    failed: ["FAILED", "bad", false],
    complete: ["COMPLETE", "good", false],
  };
  for (const state of LIVE_STATES) {
    const [label, tone, pulsing] = expected[state];
    assert.equal(liveStateLabel(state), label);
    const view = liveStatusView({ state, role: "reviewer", detail: "detail text" });
    assert.equal(view.state, state);
    assert.equal(view.label, label);
    assert.equal(view.tone, tone);
    assert.equal(view.pulsing, pulsing, `${state} pulses only while a process is active`);
    assert.equal(view.role, "REVIEWER");
    assert.equal(view.detail, "detail text");
  }
  assert.equal(liveStateLabel("surprise"), "UNKNOWN");
  const unknown = liveStatusView({ state: "surprise" });
  assert.equal(unknown.state, "unknown");
  assert.equal(unknown.label, "UNKNOWN");
  assert.equal(unknown.pulsing, false);
  const empty = liveStatusView(null);
  assert.equal(empty.role, "NO ROLE");
  assert.equal(empty.detail, "no managed process is running");
});

test("the age line ticks forward from the server's reading between snapshots", () => {
  const live = { state: "running", seconds_since_activity: 4 };
  assert.equal(liveAgeLabel(live), "last activity 4 s ago");
  assert.equal(liveAgeLabel(live, 0.4), "last activity 4 s ago");
  assert.equal(liveAgeLabel(live, 1), "last activity 5 s ago");
  assert.equal(liveAgeLabel(live, 2.9), "last activity 6 s ago");
  assert.equal(liveAgeLabel(live, -30), "last activity 4 s ago", "a negative elapsed never rewinds");
  assert.equal(liveAgeLabel({ seconds_since_activity: 119 }, 0), "last activity 119 s ago");
  assert.equal(liveAgeLabel({ seconds_since_activity: 120 }, 0), "last activity 2 min ago");
  assert.equal(liveAgeLabel({ seconds_since_activity: 7200 }, 0), "last activity 2 h ago");
  assert.equal(liveAgeLabel({ seconds_since_activity: null }, 5), "last activity unknown");
  assert.equal(liveAgeLabel(null, 5), "last activity unknown");
});

test("the page carries the #live-status strip under the header with its four fields", () => {
  const html = read("dashboard/index.html");
  const header = html.indexOf("</header>");
  const strip = html.indexOf('id="live-status"');
  const main = html.indexOf('<main id="dashboard"');
  assert.ok(header > 0 && strip > header && strip < main, "the strip sits between the header and main");
  for (const id of ["live-state", "live-role", "live-age", "live-detail"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /class="live-pill"/);
  const css = read("dashboard/styles.css");
  assert.match(css, /\.live-status\.is-pulsing \.live-pill \{ animation: live-pulse/);
  assert.match(css, /@keyframes live-pulse/);
  for (const tone of ["good", "warning", "bad"]) {
    assert.match(css, new RegExp(`\\.live-status\\[data-tone="${tone}"\\] \\.live-pill`));
  }
});

test("app.js defines renderLive and the 1 s ticker, and render() feeds it snapshot.live", () => {
  const app = read("dashboard/app.js");
  assert.match(app, /function renderLive\(live\)/);
  assert.match(app, /function renderLiveAge\(\)/);
  assert.match(app, /renderLive\(snapshot\.live\)/);
  assert.match(app, /renderLive\(null\)/, "an uninitialized snapshot hides the strip");
  assert.match(app, /window\.setInterval\(renderLiveAge, 1000\)/);
  assert.match(app, /liveStatusView\(state\.live\)/);
  assert.match(app, /liveAgeLabel\(state\.live, elapsed\)/);
  assert.match(app, /classList\.toggle\("is-pulsing", view\.pulsing\)/);
  const renderStart = app.indexOf("function render(snapshot)");
  const renderBody = app.slice(renderStart, app.indexOf("\n}\n", renderStart));
  assert.ok(renderBody.includes("renderLive(snapshot.live)"), "render() calls renderLive with snapshot.live");
});
