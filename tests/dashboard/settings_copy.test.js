// REQ-004: Agent Settings must state that saving during a live run affects
// only the next launch/replacement, never the currently running process.
// Run: node --test tests/dashboard/settings_copy.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const indexHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "dashboard", "index.html"),
  "utf8",
);

test("fallback settings panel states edits apply to future replacement choices only", () => {
  assert.match(
    indexHtml,
    /Future replacement choices only\. Current sessions remain unchanged\./,
  );
});

test("the save-confirmation copy also frames the change as future launches, not the active run", () => {
  const appJs = fs.readFileSync(
    path.join(__dirname, "..", "..", "dashboard", "app.js"),
    "utf8",
  );
  assert.match(appJs, /Future launches will use these exact profiles/);
});
