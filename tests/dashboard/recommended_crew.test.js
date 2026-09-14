// #39: a role whose profile came from the recommended crew is labelled
// "recommended default" in Agent Settings; a saved choice is never
// relabelled and a default is never shown as a choice.
// Run: node --test tests/dashboard/recommended_crew.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { profileSourceLabel } = require("../../dashboard/lib/dashboard-logic.js");

test("a fully recommended role is labelled recommended default", () => {
  assert.equal(
    profileSourceLabel({ adapter: "recommended", model: "recommended" }),
    "recommended default",
  );
});

test("a fully explicit role is labelled explicit, never recommended", () => {
  assert.equal(
    profileSourceLabel({ adapter: "explicit", model: "explicit" }),
    "explicit adapter · explicit model",
  );
});

test("a partial override names which half is still the recommended default", () => {
  assert.equal(
    profileSourceLabel({ adapter: "explicit", model: "recommended" }),
    "explicit adapter · recommended default model",
  );
});

test("an overridden adapter with no model named shows the runner default, not a recommended model", () => {
  assert.equal(
    profileSourceLabel({ adapter: "explicit", model: "runner_default" }),
    "explicit adapter · runner default model",
  );
});

test("a settings payload from a server without profile_sources reads as explicit", () => {
  assert.equal(profileSourceLabel(undefined), "explicit");
});

test("the next-launch line in app.js carries the provenance label and the dialog explains the crew", () => {
  const appJs = fs.readFileSync(path.join(__dirname, "..", "..", "dashboard", "app.js"), "utf8");
  assert.match(appJs, /profileSourceLabel\(sources\)/);
  const indexHtml = fs.readFileSync(path.join(__dirname, "..", "..", "dashboard", "index.html"), "utf8");
  assert.match(indexHtml, /RECOMMENDED CREW/);
  assert.match(indexHtml, /claude-opus-5/);
  assert.match(indexHtml, /recommended default/);
});
