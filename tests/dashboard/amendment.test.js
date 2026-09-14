// #42: Mission Control renders snapshot.amendment (the open scoped
// post-approval amendment) as an AMENDMENT panel built from ids, counts,
// and hashes only, and the input-required banner names the pending
// decision. Criterion i42-dashboard.
// Run: node --test tests/dashboard/amendment.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  AMENDMENT_DECISIONS,
  amendmentPendingDecision,
  showAmendmentPanel,
  amendmentHeadline,
  amendmentDecisionLabel,
  amendmentDecisionsView,
  amendmentIdsLabel,
  amendmentEvidenceLabel,
  amendmentReasonsView,
} = require("../../dashboard/lib/dashboard-logic.js");

const open = {
  amendment_id: "am-0123456789abcdef0123456789abcdef",
  state: "open",
  by: "architect-1",
  classification: "scoped",
  classification_reasons: [],
  changed_ids: ["REQ-002"],
  dependent_ids: ["REQ-001"],
  affected_work_items: ["issue-101"],
  retained_evidence_count: 3,
  frozen_phase: 4,
  frozen_progress: 40,
  base_design_hash: "aa".repeat(32),
  resulting_design_hash: "bb".repeat(32),
  amendment_hash: "cc".repeat(32),
  required_decisions: [
    { decision: "review", status: "pending", by: null, at: null },
    { decision: "pilot_approval", status: "pending", by: null, at: null },
  ],
  pending_decision: "review",
  history_count: 0,
};

test("the pending decision follows the review record, in order", () => {
  assert.deepEqual(AMENDMENT_DECISIONS, ["review", "revision", "pilot_approval"]);
  assert.equal(amendmentPendingDecision(open), "review");
  const reviewed = {
    ...open, pending_decision: "pilot_approval",
    required_decisions: [
      { decision: "review", status: "approved", by: "reviewer-1", at: "2026-09-14T10:00:00+00:00" },
      { decision: "pilot_approval", status: "pending", by: null, at: null },
    ],
  };
  assert.equal(amendmentPendingDecision(reviewed), "pilot_approval");
  const rejected = {
    ...open, pending_decision: undefined,
    required_decisions: [
      { decision: "review", status: "changes_requested", by: "reviewer-1", at: "2026-09-14T10:00:00+00:00" },
      { decision: "pilot_approval", status: "pending", by: null, at: null },
    ],
  };
  assert.equal(amendmentPendingDecision(rejected), "revision");
  assert.equal(amendmentPendingDecision({ ...open, state: "approved" }), null);
  assert.equal(amendmentPendingDecision(null), null);
});

test("the panel shows only for an open amendment and names what it changed", () => {
  assert.equal(showAmendmentPanel(open), true);
  assert.equal(showAmendmentPanel(null), false);
  assert.equal(showAmendmentPanel({ ...open, state: "escalated" }), false);
  assert.equal(
    amendmentHeadline(open),
    "am-0123456789abcdef0123456789abcdef · SCOPED · 1 criterion changed · issue-101 · frozen at Phase 4",
  );
  assert.equal(amendmentHeadline(null), "No amendment open");
  assert.equal(amendmentIdsLabel(open.changed_ids, "Changed criteria"), "Changed criteria: REQ-002");
  assert.equal(amendmentIdsLabel(open.dependent_ids, "Dependent criteria"), "Dependent criteria: REQ-001");
  assert.equal(amendmentIdsLabel([], "Affected work items"), "Affected work items: none");
  assert.equal(
    amendmentEvidenceLabel(open),
    "3 criteria outside the change keep valid evidence · 1 reset to not tested",
  );
});

test("required decisions, classification, and reasons render from the snapshot only", () => {
  assert.equal(amendmentDecisionLabel(open), "PENDING: independent amendment review, then Pilot approval");
  assert.equal(
    amendmentDecisionLabel({ ...open, pending_decision: "pilot_approval" }),
    "PENDING: Pilot approval (amendment-approve)",
  );
  assert.equal(
    amendmentDecisionLabel({ ...open, pending_decision: "revision" }),
    "PENDING: reviewer requested changes; Architect revises or escalates",
  );
  assert.equal(amendmentDecisionLabel(null), "No decision pending");
  assert.deepEqual(amendmentDecisionsView(open), ["review: pending", "pilot approval: pending"]);
  assert.deepEqual(
    amendmentDecisionsView({ ...open, required_decisions: [
      { decision: "review", status: "approved", by: "reviewer-1" },
      { decision: "pilot_approval", status: "recorded", by: "pilot" },
    ] }),
    ["review: approved by reviewer-1", "pilot approval: recorded by pilot"],
  );
  assert.deepEqual(amendmentReasonsView(open), []);
  assert.deepEqual(
    amendmentReasonsView({ ...open, classification: "full_redesign",
      classification_reasons: ["operation 1 (add REQ-009): an add operation is added scope", 7] }),
    ["operation 1 (add REQ-009): an add operation is added scope"],
  );
});

test("the dashboard page carries the AMENDMENT panel and app.js renders snapshot.amendment", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../dashboard/index.html"), "utf8");
  assert.match(html, /id="amendment-panel"/);
  assert.match(html, /AMENDMENT<\/p>/);
  for (const id of ["amendment-state", "amendment-headline", "amendment-decision", "amendment-list"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  assert.match(app, /function renderAmendment\(amendment\)/);
  assert.match(app, /renderAmendment\(snapshot\.amendment \|\| null\)/);
  for (const helper of ["showAmendmentPanel", "amendmentHeadline", "amendmentDecisionLabel",
    "amendmentDecisionsView", "amendmentIdsLabel", "amendmentEvidenceLabel", "amendmentReasonsView"]) {
    assert.match(app, new RegExp(`${helper}\\(`));
  }
  const css = fs.readFileSync(path.join(__dirname, "../../dashboard/styles.css"), "utf8");
  assert.match(css, /\.amendment-panel\b/);
  // The banner branch lives in the server-side snapshot: the kinds it emits
  // are the ones app.js already routes through the generic alert path.
  const server = fs.readFileSync(path.join(__dirname, "../../bin/handsoff_dashboard.py"), "utf8");
  for (const kind of ["amendment_review", "amendment_revision", "amendment_approval"]) {
    assert.match(server, new RegExp(`kind = "${kind}"`));
  }
  assert.match(server, /"amendment": lib\.amendment_view\(status, acceptance, cfg, verifications\)/);
});
