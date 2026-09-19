const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "fleet/styles.css"), "utf8");

// #150: ongoing runs are the main grid; completed and closed runs sit in a
// collapsed section below with a count, remembered per browser.
test("fleet splits ongoing and finished runs", () => {
  assert.match(app, /const FINISHED_STATES = new Set\(\["complete", "closed"\]\)/);
  assert.match(app, /const ongoing = projects\.filter\(\(project\) => !FINISHED_STATES\.has\(project\.state\)\)/);
  assert.match(app, /const finished = projects\.filter\(\(project\) => FINISHED_STATES\.has\(project\.state\)\)/);
  assert.match(app, /\$\("projects"\)\.innerHTML = ongoing\.length \? ongoing\.map\(projectCard\)/);
  assert.match(app, /\$\("finished-projects"\)\.innerHTML = finished\.map\(projectCard\)/);
  assert.match(app, /\$\("finished-count"\)\.textContent = `\$\{finished\.length\} RUN/);
  assert.match(app, /finishedSection\.classList\.toggle\("hidden", finished\.length === 0\)/);
  // The ordering is Fleet's existing state order; counters and decisions are untouched.
  assert.match(app, /STATE_ORDER\.indexOf\(a\.state\) - STATE_ORDER\.indexOf\(b\.state\)/);
  assert.match(app, /\$\("summary"\)\.innerHTML = STATE_ORDER\.map/);
  assert.match(app, /\$\("decision-list"\)\.innerHTML = data\.decisions\.map/);
});

test("the finished section is a collapsed disclosure with a count, remembered per browser", () => {
  assert.match(html, /<details id="finished-runs" class="finished-runs hidden">/);
  assert.match(html, /<summary><span class="panel-label">COMPLETED AND CLOSED<\/span><span id="finished-count" class="section-meta">0 RUNS<\/span><\/summary>/);
  assert.match(html, /<div id="finished-projects" class="projects"><\/div>/);
  assert.match(html, /<p class="panel-label">ONGOING MISSIONS<\/p>/);
  assert.match(app, /localStorage\.getItem\("fleet\.finished\.open"\)/);
  assert.match(app, /section\.addEventListener\("toggle"/);
  assert.match(css, /\.finished-runs > summary::after \{ content: "SHOW"/);
  assert.match(css, /\.finished-runs\[open\] > summary::after \{ content: "HIDE"; \}/);
});
