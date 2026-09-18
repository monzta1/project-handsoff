const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("Fleet cards expose the verified dashboard URL in a new tab", () => {
  const app = read("fleet/app.js");
  assert.match(app, /OPEN DASHBOARD/);
  assert.match(app, /project\.dashboard_url/);
  assert.match(app, /target="_blank"/);
  assert.match(app, /rel="noopener"/);
});

test("cards render the dashboard explanation when no URL exists", () => {
  assert.match(read("fleet/app.js"), /project\.dashboard_note/);
});

test("Fleet markup and renderer escape card text without lime colors", () => {
  const app = read("fleet/app.js");
  assert.match(read("fleet/index.html"), /id="projects"/);
  assert.match(app, /replaceAll\("&","&amp;"\)/);
  const styles = read("fleet/styles.css");
  assert.doesNotMatch(styles, /lime|#0f0|#00ff00/i);
});

test("Fleet carries the Handsoff mark and each project's own logo when it has one", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const root = path.resolve(__dirname, "../..");
  const html = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");
  const mc = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
  assert.match(html, /<img class="brand-mark" src="\/logo\.png"/);
  assert.doesNotMatch(html, /E<span>\/\/<\/span>V/);
  assert.match(app, /project\.logo_url \? `<img class="project-logo" src="\$\{esc\(project\.logo_url\)\}"/);
  assert.match(mc, /snapshot\.project\.logo_url/);
});
