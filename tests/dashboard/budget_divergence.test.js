// #342 REQ-002, the browser-facing half.
//
// Convention, docs/governance-design.md item 22: these checks are Node-only
// and dependency-free, inspecting the production wiring rather than pretending
// to launch Chrome. Item 23 pairs that with `record-evidence --kind browser`
// as the real attestation, which is why REQ-002 is automated_and_browser.
//
// The field report's complaint was not that the number was wrong. It was that
// it was silent: `[agent_budget] implementer = 500000` was accepted, capped to
// about 80,000, and nothing in the CLI or the dashboard said so. So these
// tests assert the leg NAMES the number that was applied and the number that
// was not, on every path, and never shows an empty or undefined marker.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "../..");
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");

// The block that builds the per-leg budget facts, bounded so a match cannot
// drift in from an unrelated part of a 4,000-line file.
function budgetBlock() {
  const start = app.indexOf("const budgetFacts = [");
  assert.notEqual(start, -1, "the routing leg no longer builds a budget fact list");
  const end = app.indexOf("for (const [labelText, valueText] of budgetFacts)", start);
  assert.notEqual(end, -1, "the budget fact list is no longer rendered");
  return app.slice(start, end);
}

test("the leg reports which ceiling was applied", () => {
  const block = budgetBlock();
  assert.match(block, /ceiling_source/,
    "without this an operator cannot tell a configured ceiling from a calculated one, "
    + "which is the whole of #342");
  assert.match(block, /CEILING/, "the fact needs a label an operator can read");
});

test("the divergence between the two numbers is rendered, not just read", () => {
  const block = budgetBlock();
  assert.match(block, /ceiling_divergence/);
  // Inside a pushed fact, not merely mentioned: a value read into a variable
  // and never displayed is the defect shape this criterion exists for.
  const pushes = block.split("budgetFacts.push(").slice(1);
  assert.ok(pushes.length >= 1, "no fact is pushed for the ceiling decision");
  assert.ok(pushes.some((p) => p.includes("divergence")),
    "the divergence is computed but never reaches a rendered fact");
});

test("the operator-set case names handsoff.toml", () => {
  const block = budgetBlock();
  assert.match(block, /configured_explicitly/,
    "the leg must distinguish a ceiling the operator set from one that is merely the default");
  assert.match(block, /handsoff\.toml/,
    "an operator told their ceiling 'was configured' still has to guess where");
});

test("the capped case tells the operator what to do about it", () => {
  // The other half, and the one the field report lived in: a configured value
  // that did NOT win. Saying so without saying how to make it win leaves the
  // operator exactly where they were.
  const block = budgetBlock();
  const calculatedBranch = block.slice(block.indexOf("} else {"));
  assert.match(calculatedBranch, /configured_ceiling/,
    "the capped branch must name the configured number that was not applied");
  assert.match(calculatedBranch, /agent_budget/,
    "it must name the setting that makes the configured number authoritative");
});

test("a leg with no recorded decision renders as it did before", () => {
  // Compatibility. Every archived leg from before these fields existed must
  // not gain an empty CEILING row or the word undefined.
  const block = budgetBlock();
  assert.match(block, /decision && decision\.ceiling_source/,
    "the ceiling fact must be conditional on the decision carrying the field");
  assert.doesNotMatch(block, /undefined/,
    "an absent decision must not reach the page as the word undefined");
});

test("both numbers are formatted for a human", () => {
  const block = budgetBlock();
  const ceilingFact = block.slice(block.indexOf("if (decision && decision.ceiling_source)"));
  const statements = ceilingFact.split("budgetFacts.push(").slice(1);
  assert.ok(statements.length >= 1);
  for (const [index, statement] of statements.entries()) {
    assert.ok(statement.includes("toLocaleString") || !statement.includes("Number("),
      `ceiling fact ${index + 1} prints a raw token count: ${statement.slice(0, 120)}`);
  }
});

test("the python projection hands the whole decision to the page", () => {
  // The closed-tuple lesson from #347: a record that carries the field and a
  // projection that drops it renders nothing. This one deep-copies, so the
  // test pins that rather than a field list that would not exist.
  const lib = fs.readFileSync(path.join(root, "bin/handsoff_lib.py"), "utf8");
  const assignment = lib.slice(lib.indexOf("def _agent_assignment"));
  // Bounded by the function's own return, which is indented. The first
  // version looked for an unindented "return assignment" and matched
  // nothing, so `body` was 38 characters of the signature and the
  // assertion failed against code that was correct.
  const body = assignment.slice(0, assignment.indexOf("\n    return assignment"));
  assert.ok(body.length > 200, "the projection function was not located");
  assert.match(body, /assignment\["budget_decision"\] = deepcopy\(budget\)/,
    "the projection must pass the decision whole, or each new field needs wiring twice");
});
