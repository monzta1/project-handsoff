const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");
const regressionRunner = fs.readFileSync(path.join(root, "bin/handsoff_regress.py"), "utf8");

test("any normalized test execution gets a prominent accessible progress surface", () => {
  assert.match(html, /id="test-progress-panel"[^>]*aria-live="polite"/);
  assert.match(html, /LIVE TEST EXECUTION/);
  assert.match(html, /id="regression-progress-bar"[^>]*role="progressbar"[^>]*aria-label="Test execution progress"/);
  assert.match(app, /function renderTestProgress\(progress\)/);
  assert.match(app, /renderTestProgress\(snapshot\.test_progress \|\| null\)/);
  assert.match(app, /progress\.source === "ci"/);
  assert.match(app, /progress\.units \|\| \[\]/);
});

test("overall and test progress stay visible with readable responsive sizing", () => {
  assert.match(html, /id="topbar-progress"[^>]*aria-label="Always-visible mission progress"/);
  assert.match(html, /id="topbar-overall-progress"[^>]*role="progressbar"/);
  assert.match(html, /id="topbar-criteria"/);
  assert.match(html, /id="topbar-test-state"/);
  assert.match(css, /\.topbar \{[^}]*position: sticky;/);
  assert.doesNotMatch(html, /id="progress-dock"/);
  assert.match(css, /\.regression-progress-head strong \{[^}]*30px/);
  assert.match(css, /@media \(max-width:/);
});

test("sticky progress stays proportional to the other topbar controls", () => {
  assert.match(css, /\.settings-toggle \{[^}]*height: 34px;/);
  assert.match(css, /\.topbar-progress \{[^}]*height: 34px;/);
  assert.match(css, /\.topbar-progress-item strong \{[^}]*13px\/1 var\(--mono\)/);
  assert.match(css, /\.topbar-progress-item small \{ display: none; \}/);
});

test("shard grid exposes state, counts, and bounded server-provided rows", () => {
  assert.match(html, /id="regression-progress-workers"/);
  assert.match(app, /unit\.state\.toUpperCase\(\)/);
  assert.match(app, /unit\.done \|\| 0/);
  assert.match(app, /unit\.total/);
  assert.match(css, /\.regression-progress-workers \{[^}]*grid-template-columns:/);
});

test("dashboard progress is fed by the shared versioned five-shard inventory", () => {
  assert.match(regressionRunner, /INVENTORY_SCHEMA_VERSION = 1/);
  assert.match(regressionRunner, /INVENTORY_VIEWS = \("local", "ci", "release", "dashboard"\)/);
  assert.match(regressionRunner, /snapshot\["inventory"\] = state\["inventory"\]/);
  assert.match(regressionRunner, /snapshot\["inventory_id"\]/);
  assert.match(regressionRunner, /DEFAULT_SHARDS = 5/);
  assert.match(regressionRunner, /def balanced_shards\(/);
  assert.match(html, /id="regression-progress-mode"/);
  assert.match(app, /progress\.worker_count/);
  assert.match(app, /progress\.fallback_reason/);
  assert.match(app, /progress\.mode/);
  assert.match(regressionRunner, /"worker_count": max_shards if use_shards else 1/);
  assert.match(regressionRunner, /HANDSOFF_REGRESS_PLAN: mode=/);
});

test("reviewer isolation is visible on the model handoff journey", () => {
  assert.match(app, /item\.reviewer_isolation/);
  assert.match(app, /REVIEWER ISOLATION/);
  assert.match(app, /isolation\.enforcement/);
  assert.match(app, /isolation\.project_access/);
  assert.match(app, /isolation\.network_policy/);
  assert.match(app, /isolation\.credentials/);
});
