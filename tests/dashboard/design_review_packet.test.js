// #36: Mission Control renders the latest delta review packet as one
// compact line from snapshot.design_review_packet counts only; finding text
// never reaches the dashboard. A missing or null packet reads as "none".
// Run: node --test tests/dashboard/design_review_packet.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { designReviewPacketLabel } = require("../../dashboard/lib/dashboard-logic.js");

const root = path.join(__dirname, "..", "..");

const summary = {
  packet_id: "a29fc3e3abfa520e3834819602275b27",
  attempt: 2,
  previous_attempt: 1,
  design_hash: "6492e72aecef03e2021928076e08e4e2fe4b49be33374158e1dc12c074906fe9",
  stale: true,
  stale_reasons: ["HEAD moved from 6bc2aa05022e to 0c1a0b92d74a since the previous review"],
  findings: { total: 3, resolved: 1, rejected: 1, unresolved: 1 },
  new_findings: 3,
  criteria_delta: { added: 1, removed: 0, changed: 1, unchanged: 0 },
  evidence: 0,
  files_changed: 6,
  truncated: false,
  bytes: 1224,
};

test("the label is built from the counts, the attempt, and the flags", () => {
  assert.equal(
    designReviewPacketLabel(summary),
    "Delta packet for attempt 2 · 3 findings (1 resolved, 1 rejected, 1 unresolved) · criteria +1 -0 ~1 · STALE",
  );
  assert.equal(
    designReviewPacketLabel({ ...summary, stale: false, truncated: true, findings: { total: 1, resolved: 0, rejected: 0, unresolved: 1 } }),
    "Delta packet for attempt 2 · 1 finding (0 resolved, 0 rejected, 1 unresolved) · criteria +1 -0 ~1 · truncated",
  );
});

test("no packet, a null packet, and a legacy snapshot all read as none", () => {
  const none = "Delta packet: none (first review gets the full task)";
  assert.equal(designReviewPacketLabel(null), none);
  assert.equal(designReviewPacketLabel(undefined), none);
  assert.equal(designReviewPacketLabel({}), none);
  assert.equal(designReviewPacketLabel("2"), none);
});

test("malformed counts render as zeros, never NaN or undefined", () => {
  const label = designReviewPacketLabel({ attempt: 3, findings: "many", criteria_delta: null, stale: false });
  assert.equal(label, "Delta packet for attempt 3 · 0 findings (0 resolved, 0 rejected, 0 unresolved) · criteria +0 -0 ~0");
  assert.doesNotMatch(label, /NaN|undefined/);
});

test("app.js renders the label and the server publishes the summary", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  assert.match(html, /id="design-review-packet"/);
  assert.match(app, /\$\("design-review-packet"\)\.textContent = designReviewPacketLabel\(snapshot\.design_review_packet\)/);
  assert.match(server, /"design_review_packet": lib\.design_review_packet_summary\(status\)/);
});
