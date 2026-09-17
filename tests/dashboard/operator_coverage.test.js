const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const html = read("dashboard/index.html");
const app = read("dashboard/app.js");
const css = read("dashboard/styles.css");

test("panel contains launch, verify, and engine surfaces", () => {
  for (const id of ["launch-role-form", "launch-role-task", "verify-form", "engine-panel"]) assert.match(html, new RegExp(`id="${id}"`));
});
test("inventory is rendered and actionable kinds are deduplicated", () => {
  assert.match(app, /snapshot\.operations/);
  for (const cls of ["op-actionable", "op-unavailable", "op-readonly"]) assert.match(app, new RegExp(cls));
  assert.match(app, /new Set\(actions\.map.*kind/);
});
test("operation forms post state-bound action ids", () => {
  for (const endpoint of ["/api/launch-role", "/api/verify", "/api/verify-live"]) assert.match(app, new RegExp(endpoint.replaceAll("/", "\\/")));
  assert.match(app, /action_id: launch\.action_id/);
  assert.match(app, /action_id: verify\.action_id/);
  assert.match(app, /action_id: live\.action_id/);
});
test("untrusted reasons and consequences use the escape helper", () => {
  assert.match(app, /escapeHtml\(item\.reason/);
  assert.match(app, /escapeHtml\(item\.consequence/);
  assert.match(app, /escapeHtml\(launch\?\.consequence/);
});
test("operation palette classes avoid saturated green", () => {
  for (const cls of ["op-actionable", "op-unavailable", "op-readonly"]) assert.match(css, new RegExp(`\\.${cls}`));
  assert.doesNotMatch(css, /lime|#0f0|#00ff00/i);
});
test("new coverage sources contain no em dash", () => {
  const panel = html.slice(html.indexOf('id="operator-actions-panel"'), html.indexOf('</section>', html.indexOf('id="operator-actions-panel"')));
  assert.doesNotMatch(panel, /—/);
  assert.doesNotMatch(app.slice(app.indexOf("function renderOperations"), app.indexOf("function postOperation")), /—/);
  assert.doesNotMatch(css.slice(css.indexOf(".operator-inventory"), css.indexOf(".toast")), /—/);
});
