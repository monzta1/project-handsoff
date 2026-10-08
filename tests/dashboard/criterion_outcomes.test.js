const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const logic = require(path.join(__dirname, "../../dashboard/lib/dashboard-logic.js"));

// P1.1: renderCriteria from app.js run against a small DOM shim, the way
// work_item_lanes.test.js runs renderWorkItems: a criterion's declared
// outcome and evidence classes are visible (escaped) and a criterion without
// them renders as before.
function renderCriteriaList(criteria) {
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  const slice = (name) => {
    const start = app.indexOf(`function ${name}(`);
    const end = app.indexOf("\n}\n", start) + 3;
    assert.ok(start >= 0 && end > start, `${name} is defined in app.js`);
    return app.slice(start, end);
  };
  const list = { id: "criteria-list", innerHTML: "" };
  const context = { $: (id) => (id === "criteria-list" ? list : null), String,
    baselineLabel: logic.baselineLabel, repeatLabel: logic.repeatLabel };
  vm.createContext(context);
  vm.runInContext(["escapeHtml", "renderCriteria"].map(slice).join("\n"), context);
  vm.runInContext(`renderCriteria(${JSON.stringify(criteria)});`, context);
  return list.innerHTML;
}

const base = (id, extra) => ({ id, requirement: `requirement ${id}`, type: "primary_fix",
  verification: "automated", state: "pending", evidence: [], ...extra });

test("a criterion's outcome and evidence classes are shown on the board", () => {
  const html = renderCriteriaList([
    base("REQ-001", { outcome: "The page shows <b>saved</b> & stays", evidence_classes: ["checks", "browser"] }),
    base("REQ-002"),
  ]);
  const cards = html.split('<div class="criterion">').slice(1);
  assert.equal(cards.length, 2);
  assert.ok(cards[0].includes('<p class="criterion-outcome">Outcome: The page shows &lt;b&gt;saved&lt;/b&gt; &amp; stays</p>'), cards[0]);
  assert.ok(cards[0].includes('<span class="criterion-classes">classes: checks, browser</span>'), cards[0]);
  assert.doesNotMatch(cards[0], /<b>saved/);
  // a criterion without the fields renders nothing new
  assert.doesNotMatch(cards[1], /criterion-outcome|criterion-classes/);
  assert.doesNotMatch(renderCriteriaList([base("REQ-003", { outcome: null, evidence_classes: [] })]),
    /criterion-outcome|criterion-classes/);
});

test("evidence class names are escaped", () => {
  const html = renderCriteriaList([base("REQ-001", { evidence_classes: ["<manual>"] })]);
  assert.match(html, /classes: &lt;manual&gt;/);
  assert.doesNotMatch(html, /<manual>/);
});
