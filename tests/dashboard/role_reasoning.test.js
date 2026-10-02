// #347 REQ-009, the Node wiring half.
//
// Convention, docs/governance-design.md item 22: browser-facing automated
// checks here are Node-only and dependency-free, inspecting the production
// wiring without pretending to launch Chrome. Item 23 pairs that with
// `record-evidence --kind browser` as the real attestation, which is why
// REQ-009 is automated_and_browser and not automated: a design review found
// that a source match alone can be satisfied by a string in a comment while
// the rendered line never changes.
//
// So this file deliberately does NOT assert that `reasoning_effort` merely
// appears in app.js. It asserts the label function that renders beside the
// model consumes it, and that the snapshot that feeds the page carries it.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("the model label beside each journey leg consumes the recorded effort", () => {
  const app = read("dashboard/app.js");
  const label = app.slice(app.indexOf("function routingModelLabel"));
  const body = label.slice(0, label.indexOf("\n}") + 2);
  assert.match(body, /reasoning_effort/,
    "routingModelLabel must read the effort, or the value never reaches the leg an operator reads");
  assert.match(body, /item\?\.model/,
    "the effort belongs beside the model, not instead of it");
});

test("the effort is rendered, not merely referenced somewhere in the file", () => {
  // The distinction the design review drew: a reference in a comment or a
  // dead branch satisfies a bare source match. Requiring it inside the
  // returned template of the label function ties it to what is displayed.
  const app = read("dashboard/app.js");
  const label = app.slice(app.indexOf("function routingModelLabel"));
  const body = label.slice(0, label.indexOf("\n}") + 2);
  // EVERY return path, not some. `some` was the first version of this
  // assertion and a mutation that dropped the effort from the common
  // `item?.model` path still passed, because the fallback path mentioned it.
  // That is the defect this whole criterion exists to prevent, reproduced in
  // the test meant to catch it.
  const body2 = body.replace(/\n\s*/g, " ");
  const paths = body2.split("return").slice(1);
  assert.ok(paths.length >= 2, "expected more than one return path to check");
  for (const [index, segment] of paths.entries()) {
    const statement = segment.split(";")[0];
    assert.ok(statement.includes("effort"),
      `return path ${index + 1} of routingModelLabel drops the effort: ${statement.trim()}`);
  }
});

test("the snapshot session view carries the field to the page", () => {
  // The closed tuple in handsoff_dashboard decides what the page can see at
  // all. A record that carries the field and a view that drops it renders
  // nothing, which is the shape of the defect this criterion exists for.
  const view = read("bin/handsoff_dashboard.py");
  const tuple = view.slice(view.indexOf('fields = ("session_id"'));
  assert.match(tuple.slice(0, tuple.indexOf(")") + 1), /reasoning_effort/,
    "the session field tuple drops reasoning_effort before the page sees it");
});

test("a leg with no recorded effort renders exactly as it did before", () => {
  // The compatibility half. An archived leg from before the field existed
  // must not gain an empty marker or an undefined in the rendered string.
  const app = read("dashboard/app.js");
  const label = app.slice(app.indexOf("function routingModelLabel"));
  const body = label.slice(0, label.indexOf("\n}") + 2);
  assert.match(body, /item\?\.reasoning_effort\s*\?/,
    "the effort must be conditional, so a leg without one is unchanged");
  assert.doesNotMatch(body, /undefined/,
    "an absent effort must not reach the page as the word undefined");
});
