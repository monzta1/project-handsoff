const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const logic = require(path.join(root, "dashboard", "lib", "dashboard-logic.js"));

// The legacy API remains compatible, but the sticky header is dedicated to
// mission, criterion, and test progress instead of a note composer.
test("the header replaces Pilot notes with persistent progress while preserving the API", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  const header = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
  assert.doesNotMatch(header, /pilot-note/);
  assert.match(header, /id="topbar-overall"/);
  assert.match(header, /id="topbar-criteria"/);
  assert.match(header, /id="topbar-test-state"/);
  assert.doesNotMatch(app, /sendPilotNote/);
  assert.match(css, /\.topbar-progress/);
  assert.match(server, /"\/api\/pilot-note"/);
  assert.match(server, /record_pilot_note\(self\.server\.project_root, by="Mission Control Pilot"/);
});

test("pilotNoteText collapses whitespace and enforces the 1 to 512 bound", () => {
  assert.equal(logic.PILOT_NOTE_MAX_LENGTH, 512);
  assert.equal(logic.pilotNoteText("  The   reviewer\n needs  screenshots "), "The reviewer needs screenshots");
  assert.equal(logic.pilotNoteText(""), "");
  assert.equal(logic.pilotNoteText("   "), "");
  assert.equal(logic.pilotNoteText(null), "");
  assert.equal(logic.pilotNoteText(undefined), "");
  assert.equal(logic.pilotNoteText("x".repeat(512)), "x".repeat(512));
  assert.equal(logic.pilotNoteText("x".repeat(513)), "");
});
