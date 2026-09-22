const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const app = read("dashboard/app.js");
const html = read("dashboard/index.html");
const dashboard = read("bin/handsoff_dashboard.py");

test("a design-lane snapshot marks phases after Phase 3 as waived", () => {
  assert.match(dashboard, /"lane": status\.get\("lane"\)/);
  assert.match(dashboard, /"phases_run": deepcopy\(status\.get\("phases_run", \[\]\)\)/);
  assert.match(dashboard, /"phases_waived": deepcopy\(status\.get\("phases_waived", \[\]\)\)/);
  assert.match(dashboard, /"design_document": status\.get\("design_document"\)/);
  assert.match(dashboard, /"lane_status": \("waived" if lane/);
  assert.match(app, /renderDesignDocument\(snapshot\)/);
  assert.match(app, /const document = snapshot\?\.design_document/);
  assert.match(html, /id="design-document-link" class="design-document-link hidden"/);
});

test("a full-lane snapshot keeps the original eight-phase strip", () => {
  assert.match(dashboard, /every snapshot carries all eight phases/);
  assert.match(dashboard, /"phases": _phase_view\(/);
  assert.match(dashboard, /lane=status\.get\("lane"\)/);
});

test("the phase rail renders lane status and styles waived phases", () => {
  assert.match(app, /class="phase-node \$\{escapeHtml\(phase\.state\)\} lane-\$\{escapeHtml\(phase\.lane_status\)\}"/);
  const styles = read("dashboard/styles.css");
  assert.match(styles, /\.phase-node\.lane-waived\s*\{/);
});
