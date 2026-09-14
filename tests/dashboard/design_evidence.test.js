// #38: Mission Control renders each cached design-evidence artifact as a
// state pill (current, stale, failed, missing) built from the snapshot's
// states and hashes only; the snapshot never carries command output.
// Run: node --test tests/dashboard/design_evidence.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  DESIGN_EVIDENCE_STATES,
  designEvidenceState,
  designEvidenceDetail,
  eventDetail,
} = require("../../dashboard/lib/dashboard-logic.js");

test("every design-evidence state maps to its own pill class", () => {
  assert.deepEqual(DESIGN_EVIDENCE_STATES, ["current", "stale", "failed", "missing"]);
  for (const state of DESIGN_EVIDENCE_STATES) {
    assert.equal(designEvidenceState({ state }), state);
  }
  assert.equal(designEvidenceState({ state: "surprise" }), "missing");
  assert.equal(designEvidenceState(null), "missing");
});

test("the detail line names the reasons, input count, and commit drift", () => {
  const current = {
    id: "ast-inventory", state: "current", reasons: [], matched_files: 3,
    head: "0123456789abcdef0123456789abcdef01234567", commit_matches_head: true,
    at: "2026-09-14T10:00:00+00:00", by: "architect-1",
  };
  assert.equal(
    designEvidenceDetail(current),
    "3 input files · commit 0123456789ab (HEAD) · measured 2026-09-14T10:00:00+00:00 by architect-1",
  );
  const stale = { ...current, state: "stale", reasons: ["input files changed"], commit_matches_head: false };
  assert.equal(
    designEvidenceDetail(stale),
    "input files changed · 3 input files · commit 0123456789ab (HEAD has moved) · measured 2026-09-14T10:00:00+00:00 by architect-1",
  );
  assert.equal(designEvidenceDetail({ id: "never", state: "missing", reasons: [], matched_files: null }), "No measurement recorded.");
});

test("a design_evidence_recorded event shows hashes and metadata, never output", () => {
  const event = {
    kind: "design_evidence_recorded", artifact_id: "ast-inventory", exit_code: 0, truncated: true,
    head: "0123456789abcdef0123456789abcdef01234567", output_sha256: "ff".repeat(32),
  };
  assert.equal(eventDetail(event), "ast-inventory · exit 0 · truncated · commit 0123456789ab");
  assert.equal(eventDetail({ kind: "phase_advanced" }), "");
});

test("the dashboard page carries the design-evidence panel and state pills", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../dashboard/index.html"), "utf8");
  assert.match(html, /id="design-evidence-panel"/);
  assert.match(html, /id="design-evidence-list"/);
  const app = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
  assert.match(app, /renderDesignEvidence\(snapshot\.design_evidence \|\| \[\]\)/);
  assert.match(app, /evidence-pill/);
  const css = fs.readFileSync(path.join(__dirname, "../../dashboard/styles.css"), "utf8");
  for (const state of DESIGN_EVIDENCE_STATES) {
    assert.match(css, new RegExp(`\\.evidence-pill\\.${state}\\b`));
  }
});
