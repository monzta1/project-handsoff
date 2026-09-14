const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("multi-item browser table exposes canonical fields and distinct states", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  assert.match(html, /Phase \/ next/);
  assert.match(html, /<th>Blocker<\/th><th>Updated<\/th>/);
  assert.match(app, /workItems\?\.multi/);
  assert.match(app, /data-item-id=/);
  assert.match(app, /DISCREPANT/);
  for (const state of ["done", "blocked", "in_review", "awaiting_approval", "recovering", "in_progress", "not_started"]) {
    assert.match(css, new RegExp(`ticket-state\\.${state}`));
  }
});
