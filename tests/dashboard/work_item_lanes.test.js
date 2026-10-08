const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const logic = require(path.join(__dirname, "../../dashboard/lib/dashboard-logic.js"));

test("work-item lane and progress labels are compact and bounded", () => {
  assert.equal(logic.workItemLaneLabel({ lane: "full" }), "FULL");
  assert.equal(logic.workItemLaneLabel({ lane: "small-fix" }), "SMALL FIX · UNCONFIRMED");
  assert.equal(logic.workItemProgressLabel({ progress: 101 }), "100%");
  assert.equal(logic.workItemProgressLabel({ progress: 37.6 }), "38%");
  assert.equal(logic.showWorkItemTable({items: [{lane: "small-fix"}], multi: false}), true);
  assert.match(logic.workItemLaneDetail({lane_facts: {criteria: 1, changed_lines: 20, changed_files: 2,
    caps: {criteria: 3, changed_lines: 200, changed_files: 6}}}), /1\/3 criteria.*20\/200 lines.*2\/6 files/);
});

test("dashboard offers only eligible unconfirmed small-fix confirmation", () => {
  assert.equal(logic.smallFixCanConfirm({ lane: "small-fix" }), true);
  assert.equal(logic.smallFixCanConfirm({ lane: "full" }), false);
  assert.equal(logic.smallFixCanConfirm({ lane: "small-fix", lane_escalation: "too large" }), false);
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  assert.match(app, /\/api\/lane-confirm/);
  assert.match(app, /workItemProgressLabel\(item\)/);
});

// #415: renderWorkItems from app.js run against a small DOM shim, the way
// ci_row.test.js runs renderCi: the visible row states how the item becomes
// done and names each criterion still not passing.
function renderBoard(workItems) {
  const vm = require("node:vm");
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  const slice = (name) => {
    const start = app.indexOf(`function ${name}(`);
    const end = app.indexOf("\n}\n", start) + 3;
    assert.ok(start >= 0 && end > start, `${name} is defined in app.js`);
    return app.slice(start, end);
  };
  const byId = new Map();
  for (const id of ["ticket-panel", "ticket-total", "ticket-list"]) {
    const el = { id, textContent: "", innerHTML: "", classes: new Set() };
    el.classList = { toggle(c, on) { on ? el.classes.add(c) : el.classes.delete(c); } };
    byId.set(id, el);
  }
  const context = { $: (id) => byId.get(id) || null, document: { querySelectorAll: () => [] },
    String, Date, Math, Number, Array,
    showWorkItemTable: logic.showWorkItemTable, workItemLaneLabel: logic.workItemLaneLabel,
    workItemLaneDetail: logic.workItemLaneDetail, smallFixCanConfirm: logic.smallFixCanConfirm,
    workItemProgressLabel: logic.workItemProgressLabel, workItemDoneWhen: logic.workItemDoneWhen };
  vm.createContext(context);
  vm.runInContext(["escapeHtml", "relativeTime", "renderWorkItems"].map(slice).join("\n"), context);
  vm.runInContext(`renderWorkItems(${JSON.stringify(workItems)});`, context);
  return byId.get("ticket-list").innerHTML;
}

test("the visible board states each item's done condition and its unmet criteria", () => {
  const condition = "a work item is done when every criterion tagged to it passes";
  const row = (id, extra) => ({ id, number: 1, title: id, lane: "full", progress: 40, status: "in_progress",
    done_when: condition, ...extra });
  const html = renderBoard({ multi: true, items: [
    row("issue-101", { done: false, unmet_criteria: ["REQ-001", "REQ-004"] }),
    row("issue-102", { status: "done", done: true, unmet_criteria: [] }),
  ] });
  const rows = html.split("<tr ").slice(1);
  assert.equal(rows.length, 2);
  assert.ok(rows[0].includes(`<small class="ticket-done-when">${condition}; not passing: REQ-001, REQ-004</small>`), rows[0]);
  assert.ok(rows[1].includes(`<small class="ticket-done-when">${condition}</small>`), rows[1]);
  assert.doesNotMatch(rows[1], /not passing/);
  // a row from an engine without the field renders as before
  assert.doesNotMatch(renderBoard({ multi: true, items: [{ id: "issue-9", status: "not_started" }, { id: "issue-8" }] }),
    /ticket-done-when/);
  assert.match(renderBoard({ multi: true, items: [row("issue-7", { done_when: "<b>", unmet_criteria: [] }), row("issue-6")] }),
    /<small class="ticket-done-when">&lt;b&gt;<\/small>/, "the condition is escaped");
});
