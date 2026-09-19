import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
const dashboard = read("dashboard/app.js");
const fleet = read("fleet/app.js");
const dashboardMarkup = read("dashboard/index.html");
const fleetMarkup = read("fleet/index.html");
const dashboardStyle = read("dashboard/styles.css");
const fleetStyle = read("fleet/styles.css");

test("both clients render and clear offline state", () => {
  for (const app of [dashboard, fleet]) {
    assert.match(app, /offlineSince|fleetOfflineSince/);
    assert.match(app, /DASHBOARD OFFLINE since/);
    assert.match(app, /classList\.toggle\("is-offline"/);
  }
  assert.match(dashboard, /events\.onerror[\s\S]*refresh\(\)/);
  assert.match(dashboard, /status\.status === "closed"/);
  assert.match(dashboard, /renderRoleChiclets\(status\.status === "closed" \? null/);
  for (const markup of [dashboardMarkup, fleetMarkup]) assert.match(markup, /id="offline-banner" class="offline-banner hidden" role="alert"/);
  for (const style of [dashboardStyle, fleetStyle]) assert.match(style, /body\.is-offline \*.*animation: none !important/s);
  assert.match(dashboardStyle, /\.phase-node\.closed::before/);
});

test("offline dims the mission, holds the stepper, and the stream error triggers one rate-limited refresh", () => {
  assert.match(dashboardStyle, /body\.is-offline main \{ opacity: \.45; filter: grayscale\(\.6\); \}/);
  assert.match(dashboardStyle, /body\.is-closed \*, body\.is-closed \*::before, body\.is-closed \*::after \{ animation: none !important/);
  assert.match(dashboard, /node\.classList\.remove\("active"\);\s*node\.classList\.add\("held"\);/);
  assert.match(dashboard, /if \(Date\.now\(\) - state\.lastStreamRefresh >= 5000\)/);
  assert.match(dashboard, /window\.setInterval\(refresh, 5000\)/);
  assert.match(dashboard, /state\.offlineSince = null;\s*renderOffline\(\);/);
  assert.match(fleet, /STATE_ORDER = \["waiting", "failed", "offline"/);
  assert.match(fleet, /offline: "DASHBOARD OFFLINE"/);
  assert.match(fleetStyle, /\.project\[data-state="offline"\] \{ opacity: \.45/);
});

test("fleet notices a dead server on its own: a 5 second poll and a rate-limited refresh on stream error", () => {
  assert.match(fleet, /window\.setInterval\(refresh, 5000\)/);
  assert.match(fleet, /stream\.onerror = \(\) => \{[\s\S]*?if \(Date\.now\(\) - lastStreamRefresh >= 5000\) \{[\s\S]*?refresh\(\);/);
  assert.match(fleet, /if \(!fleetOfflineSince\) \{\s*fleetOfflineSince = new Date\(\);/);
});

test("going offline cancels running animations on both pages, not only new ones", () => {
  for (const app of [dashboard, fleet]) assert.match(app, /document\.getAnimations\(\)\.forEach\(\(animation\) => animation\.cancel\(\)\)/);
});

test("a finished run whose dashboard was released keeps its celebration: final snapshot, not an outage", () => {
  assert.match(dashboard, /const final = Boolean\(state\.offlineSince\) && \["complete", "closed"\]\.includes\(state\.lastRunStatus\)/);
  assert.match(dashboard, /document\.body\.classList\.toggle\("is-final", final\)/);
  assert.match(dashboard, /this run's dashboard was released at \$\{stamp\}; this page is the final snapshot/);
  assert.match(dashboard, /if \(outage && typeof document\.getAnimations === "function"\)/);
  assert.match(dashboardStyle, /\.offline-banner\.is-final \{ background: var\(--emerald-soft\)/);
});
