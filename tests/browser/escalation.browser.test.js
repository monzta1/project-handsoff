const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("review and recovery escalations remain visible in Mission Control", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  assert.match(html, /id="input-alert"/);
  assert.match(html, /id="replacement-list"/);
  assert.match(app, /RECOVERY ·/);
  assert.match(app, /ATTEMPT \$\{escapeHtml\(item\.attempt\)\}\/\$\{escapeHtml\(item\.cap\)\}/);
  assert.match(server, /review_cap_overrides/);
  assert.match(server, /status\.get\("escalation"\)/);
});
