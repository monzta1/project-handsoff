const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("Mission Control surfaces truthful run economics without estimating tokens", () => {
  const html = read("dashboard/index.html");
  const app = read("dashboard/app.js");
  const server = read("bin/handsoff_dashboard.py");
  for (const id of ["metrics-panel", "metrics-elapsed", "metrics-sessions", "metrics-failures",
                    "metrics-reviews", "metrics-verification", "metrics-pilot-wait", "metrics-tokens",
                    "metrics-phase-list", "metrics-session-list"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /function renderMetrics\(metrics\)/);
  // #184: the token cell folds into one line until a session reports usage; nothing is estimated
  assert.match(app, /const reported = total != null;/);
  assert.match(app, /tokens: not reported by/);
  assert.match(app, /metrics\.largest_sessions/);
  assert.match(server, /metrics = lib\.build_run_metrics/);
  assert.match(server, /"metrics": metrics/);
});

test("LCD mission clocks tick from ledger anchors and freeze on completion", () => {
  const app = read("dashboard/app.js");
  const html = read("dashboard/index.html");
  const css = read("dashboard/styles.css");
  for (const id of ["mission-clock", "phase-clock"]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /lcd-ghost" aria-hidden="true">88:88:88</);
  assert.match(app, /state\.clocks = \{ startedAt: metrics\.started_at \|\| null, endedAt: metrics\.ended_at \|\| null, phaseStartedAt: metrics\.phase_started_at \|\| null \}/);
  assert.match(app, /window\.setInterval\(renderClocks, 1000\)/);
  assert.match(app, /mission\.dataset\.frozen = anchors\.endedAt \? "true" : "false"/);
  assert.match(css, /\.lcd\[data-frozen="true"\] \.lcd-live/);
  const lib = read("bin/handsoff_lib.py");
  for (const key of ['"started_at": start.isoformat()', '"ended_at": end.isoformat() if complete and end else None', '"phase_started_at"']) assert.ok(lib.includes(key), key);
  const fleet = read("fleet/app.js");
  assert.match(fleet, /data-started-at/);
  assert.match(read("fleet/index.html"), /id="fleet-clock"/);
  assert.match(read("bin/handsoff_fleet.py"), /"started_at": \(snap\.get\("metrics"\) or \{\}\)\.get\("started_at"\)/);
});
