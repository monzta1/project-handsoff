const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const logic = require(path.join(root, "dashboard", "lib", "dashboard-logic.js"));

// #49: a small pilot-note input in the header posts to /api/pilot-note;
// the server records a pilot_note event as the Mission Control Pilot.
test("index.html carries the header pilot-note input and app.js posts it", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  const header = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
  assert.match(header, /id="pilot-note-form"/);
  assert.match(header, /id="pilot-note-text"/);
  assert.match(header, /maxlength="512"/);
  assert.match(header, /id="pilot-note-send"[^>]*>SEND</);
  assert.match(app, /async function sendPilotNote\(event\)/);
  assert.match(app, /fetch\("\/api\/pilot-note"/);
  assert.match(app, /body: JSON\.stringify\(\{ text \}\)/);
  assert.match(app, /pilotNoteText\(input\.value\)/);
  assert.match(app, /\$\("pilot-note-form"\)\.addEventListener\("submit", sendPilotNote\)/);
  assert.match(css, /\.pilot-note-input/);
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
