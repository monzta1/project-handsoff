const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");

test("browser surface wires exact regression request to Accept and Decline", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  for (const id of ["regression-details", "regression-alert", "regression-status-details",
                    "regression-last", "regression-accept", "regression-decline"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /decideRegression\("accept"\)/);
  assert.match(app, /decideRegression\("decline"\)/);
  assert.match(app, /command_hash: request\.command_sha256/);
  assert.match(server, /pending\.get\("command_sha256"\) != requested\["command_hash"\]/);
  assert.match(server, /Mission Control Pilot/);
  assert.match(server, /HTTPStatus\.CONFLICT/);
  assert.match(server, /"current": lib\.active_regression_request/);
  assert.match(app, /Commit pair:/);
  assert.match(app, /Last closed request:/);
});
