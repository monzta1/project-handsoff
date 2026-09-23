const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const { friendlyActorLabel } = require(path.join(root, "dashboard", "lib", "dashboard-logic.js"));

test("Mission Control renders an explicit regression Accept/Decline gate", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="regression-accept"/);
  assert.match(html, /id="regression-decline"/);
  assert.match(html, /id="regression-details"/);
  assert.match(html, /id="regression-alert"/);
  assert.match(html, /id="regression-last"/);
  assert.match(html, /id="regression-progress"/);
  assert.match(html, /id="regression-progress-bar"[^>]*role="progressbar"/);
  assert.match(html, /id="regression-progress-workers"/);
  assert.match(html, /id="topbar-progress"/);
  assert.match(html, /id="topbar-overall-progress"[^>]*role="progressbar"/);
  assert.match(html, /id="topbar-test-state"/);
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  assert.match(css, /\.topbar \{[^}]*position: sticky;/);
  assert.match(app, /topbar-overall-progress/);
  assert.match(app, /topbar-test-state/);
  assert.doesNotMatch(html, /id="progress-dock"/);
  assert.match(app, /fetch\("\/api\/regression-decision"/);
  assert.match(app, /command_sha256/);
  for (const field of ["content_sha256", "commit_pair", "requested_by", "expires_at", "completed_at"]) {
    assert.match(app, new RegExp(field));
  }
  assert.match(app, /regressionRecordText/);
  assert.match(app, /releasePlanText/);
  assert.match(app, /NO, TARGETED TESTS ONLY/);
  assert.match(app, /release_version/);
  assert.match(app, /regression\?\.last/);
  assert.match(app, /renderTestProgress\(snapshot\.test_progress \|\| null\)/);
  assert.match(app, /progress\.source === "regression"/);
});

test("Mission Control uses friendly role names without hiding model identity", () => {
  assert.equal(friendlyActorLabel({ role: "reviewer", purpose: "design review", actor: "claude-reviewer" }),
    "Design Reviewer");
  assert.equal(friendlyActorLabel({ role: "reviewer", purpose: "implementation review", actor: "claude-reviewer" }),
    "Implementation Reviewer");
  assert.equal(friendlyActorLabel({ role: "implementer", actor: "codex-implementer" }),
    "Implementation Engineer");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(app, /friendlyActorLabel\(item\)/);
  assert.match(app, /effectiveProfileLabel/);
});
