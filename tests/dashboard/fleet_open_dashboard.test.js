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
