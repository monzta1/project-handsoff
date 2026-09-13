const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("dashboard contains and renders the configured ticket table", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="ticket-list"/);
  assert.match(html, /<th>Issue<\/th><th>Title<\/th><th>Status<\/th>/);
  assert.match(app, /function renderTickets\(tickets\)/);
  assert.match(app, /renderTickets\(snapshot\.tickets \|\| \[\]\)/);
  assert.match(app, /escapeHtml\(ticket\.title\)/);
  assert.match(app, /escapeHtml\(ticket\.status\)/);
});
