const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("dashboard contains and renders the canonical work-item table", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="ticket-list"/);
  assert.match(html, /<th>Item<\/th><th>Issue<\/th><th>Title<\/th><th>Lane<\/th><th>Progress<\/th><th>Status<\/th>/);
  assert.match(app, /function renderWorkItems\(workItems\)/);
  assert.match(app, /renderWorkItems\(snapshot\.work_items/);
  assert.match(app, /escapeHtml\(item\.title\)/);
  assert.match(app, /escapeHtml\(item\.status\)/);
});

test("work items read as labelled cards at phone width instead of one character per line", () => {
  // Field report 2026-09-18 (Beakon mirror on a phone): the nine-column
  // table inherited overflow-wrap: anywhere and squeezed every cell to a
  // single character per line.
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  for (const label of ["Item", "Issue", "Title", "Lane", "Progress", "Status", "Phase / next", "Blocker", "Updated"]) {
    assert.match(app, new RegExp(`<td data-label="${label.replace("/", "\\/")}"`));
  }
  assert.match(css, /\.ticket-table, \.verification-latest \{[^}]*overflow-wrap: normal/);
  const phone = css.slice(css.indexOf("@media (max-width: 760px)"));
  assert.match(phone, /\.ticket-table thead \{ display: none; \}/);
  assert.match(phone, /\.ticket-table td::before \{ content: attr\(data-label\)/);
  assert.match(phone, /\.ticket-table td \{ display: grid; grid-template-columns: 82px minmax\(0, 1fr\)/);
});
