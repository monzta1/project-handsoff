const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");

// #147: the alert card is labelled by whose turn it is, and only the
// Pilot's own turn without a standing pre-authorization is styled as a
// demand (amber body class, title flip, browser notification).
test("the alert label element exists and app.js sets it from turn and preauthorized", () => {
  assert.match(html, /<span id="input-alert-label">AUTHORIZATION REQUIRED<\/span>/);
  assert.match(app, /function gateLabel\(inputRequest\)/);
  assert.match(app, /\$\("input-alert-label"\)\.textContent = label/);
  assert.match(app, /if \(inputRequest\.turn === "pilot" && inputRequest\.preauthorized\)/);
  assert.match(app, /return `PRE-AUTHORIZED BY PILOT NOTE\$\{stamp\}`/);
  assert.match(app, /return `UNDER INDEPENDENT REVIEW\$\{target\}`/);
  assert.match(app, /ROUND \$\{inputRequest\.amendment_round \|\| 1\}/);
  assert.match(app, /if \(inputRequest\.turn === "architect"\) return "ARCHITECT REVISING"/);
  assert.match(app, /return "PILOT APPROVAL NEEDED"/);
});

test("demand styling, title flip and notification fire only for the pilot's own turn", () => {
  assert.match(app, /const pilotTurn = required && inputRequest\?\.turn === "pilot" && !inputRequest\?\.preauthorized/);
  assert.match(app, /document\.body\.classList\.toggle\("input-is-required", pilotTurn\)/);
  assert.match(app, /if \(pilotTurn && signature !== state\.alertSignature && "Notification" in window/);
  assert.match(app, /state\.pilotGate && state\.titleFlip \? "🔴 PILOT AUTHORIZATION REQUIRED"/);
  assert.match(app, /pilotGate \? "blocked" : "in_progress"/);
});

test("the briefing headline is the server's wording, not a second copy in the client", () => {
  assert.match(app, /\$\("supervisor-headline"\)\.textContent = verificationHeadline \|\| supervisor\.headline/);
  assert.doesNotMatch(app, /gateHeadline/);
});
