const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("dashboard contains and renders the canonical work-item table", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="ticket-list"/);
  assert.match(html, /<th>Item<\/th><th>Issue<\/th><th>Title<\/th><th>Status<\/th>/);
  assert.match(app, /function renderWorkItems\(workItems\)/);
  assert.match(app, /renderWorkItems\(snapshot\.work_items/);
  assert.match(app, /escapeHtml\(item\.title\)/);
  assert.match(app, /escapeHtml\(item\.status\)/);
});
