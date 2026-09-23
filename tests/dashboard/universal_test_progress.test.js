const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");

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
  assert.match(html, /id="progress-dock"[^>]*aria-label="Always-visible mission progress"/);
  assert.match(html, /id="progress-dock-overall-bar"[^>]*role="progressbar"/);
  assert.match(html, /id="progress-dock-regression-bar"[^>]*role="progressbar"/);
  assert.match(css, /\.progress-dock \{ position: fixed;/);
  assert.match(css, /\.regression-progress-head strong \{[^}]*30px/);
  assert.match(css, /@media \(max-width:/);
});

test("shard grid exposes state, counts, and bounded server-provided rows", () => {
  assert.match(html, /id="regression-progress-workers"/);
  assert.match(app, /unit\.state\.toUpperCase\(\)/);
  assert.match(app, /unit\.done \|\| 0/);
  assert.match(app, /unit\.total/);
  assert.match(css, /\.regression-progress-workers \{[^}]*grid-template-columns:/);
});
