const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Lane E (#193): the asleep words beside the clocks, the phase list and the Fleet card.
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const app = read("dashboard/app.js");
const html = read("dashboard/index.html");
const fleetApp = read("fleet/app.js");

test("asleepLabel reads minutes and hours and nothing under a minute", () => {
  assert.equal(logic.asleepLabel(25500), "asleep 7 h 05 m");
  assert.equal(logic.asleepLabel(900), "asleep 15 min");
  assert.equal(logic.asleepLabel(30), "");
  assert.equal(logic.asleepLabel(0), "");
  assert.equal(logic.asleepLabel(null), "");
});

test("the page subtracts sleep on both LCDs, names it beneath them and on the phase list", () => {
  assert.match(html, /<small class="clock-asleep" id="mission-clock-asleep"><\/small>/);
  assert.match(html, /<small class="clock-asleep" id="phase-clock-asleep"><\/small>/);
  assert.match(app, /lcdText\(\(end - new Date\(anchors\.startedAt\)\.getTime\(\)\) \/ 1000 - \(anchors\.asleepSeconds \|\| 0\)\)/);
  assert.match(app, /lcdText\(\(end - new Date\(anchors\.phaseStartedAt\)\.getTime\(\)\) \/ 1000 - \(anchors\.phaseAsleepSeconds \|\| 0\)\)/);
  assert.match(app, /clockNote\.textContent = asleep;/);
  assert.match(app, /phaseNote\.textContent = asleepLabel\(\(metrics\.phase_asleep_seconds \|\| \{\}\)\[currentPhase\]\);/);
  assert.match(app, /\$\("metrics-elapsed"\)\.textContent = metricDuration\(metrics\.elapsed_seconds\) \+ \(asleep \? ` \(\$\{asleep\}\)` : ""\);/);
  assert.match(app, /const slept = asleepLabel\(\(metrics\.phase_asleep_seconds \|\| \{\}\)\[phase\]\);/);
  assert.match(read("dashboard/styles.css"), /\.clock-asleep:empty \{ display: none; \}/);
});

test("the Fleet card names the sleep in the current phase", () => {
  assert.match(fleetApp, /<p class="phase">\$\{phase\}\$\{asleepSuffix\(project\)\}\$\{missing\}<\/p>/);
  const start = fleetApp.indexOf("function asleepSuffix("), end = fleetApp.indexOf("\nfunction projectCard(");
  const esc = (v) => String(v ?? "");
  const asleepSuffix = new Function("esc", `${fleetApp.slice(start, end)}\nreturn asleepSuffix;`)(esc);
  assert.equal(asleepSuffix({ phase_asleep_seconds: 25500 }), ' <span class="asleep">asleep 7 h 05 m</span>');
  assert.equal(asleepSuffix({ phase_asleep_seconds: 120 }), ' <span class="asleep">asleep 2 min</span>');
  assert.equal(asleepSuffix({ phase_asleep_seconds: 10 }), "");
  assert.equal(asleepSuffix({}), "");
});
