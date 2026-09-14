// i35-dashboard-round-label: Mission Control renders "Design review N/M"
// from policy.design_review_attempts and policy.max_autonomous_design_reviews,
// never from prose, and the blocked banner carries next_action verbatim.
// Run: node --test tests/dashboard/design_review_budget.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { designReviewBudgetLabel } = require("../../dashboard/lib/dashboard-logic.js");

const root = path.join(__dirname, "..", "..");

test("label is derived from the two policy counters only", () => {
  assert.equal(designReviewBudgetLabel({
    design_review_attempts: 2,
    max_autonomous_design_reviews: 2,
    design_review_authorization: null,
    next_action: "Design review 9/9 written in prose must be ignored",
  }), "Design review 2/2");
  assert.equal(designReviewBudgetLabel({
    design_review_attempts: 0, max_autonomous_design_reviews: 2, design_review_authorization: null,
  }), "Design review 0/2");
});

test("an unconsumed Pilot authorization is shown next to the count", () => {
  const label = designReviewBudgetLabel({
    design_review_attempts: 2,
    max_autonomous_design_reviews: 2,
    design_review_authorization: {
      by: "moncy", at: "2026-09-14T00:00:00+00:00", note: null,
      attempt_permitted: 3, launch_session_id: null, consumed_at: null,
    },
  });
  assert.equal(label, "Design review 2/2 · attempt 3 authorized");
});

test("a consumed authorization no longer reads as available", () => {
  const label = designReviewBudgetLabel({
    design_review_attempts: 3,
    max_autonomous_design_reviews: 2,
    design_review_authorization: {
      by: "moncy", at: "2026-09-14T00:00:00+00:00", note: null,
      attempt_permitted: 3, launch_session_id: null, consumed_at: "2026-09-14T00:05:00+00:00",
    },
  });
  assert.equal(label, "Design review 3/2");
});

test("a legacy snapshot without the policy fields renders zeros, never NaN or undefined", () => {
  assert.equal(designReviewBudgetLabel({}), "Design review 0/0");
  assert.equal(designReviewBudgetLabel(undefined), "Design review 0/0");
});

test("app.js renders the label into the policy strip and the banner shows next_action", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  assert.match(html, /id="design-review-budget"/);
  assert.match(app, /\$\("design-review-budget"\)\.textContent = designReviewBudgetLabel\(policy\)/);
  // The blocked banner is fed by input_required.message, which the server
  // sets to next_action verbatim for a blocked run; the exhausted
  // record-design-review writes the authorization command there.
  assert.match(server, /"design_review_attempts": design_review_budget\["attempts"\]/);
  assert.match(server, /"max_autonomous_design_reviews": design_review_budget\["limit"\]/);
  assert.match(server, /message = next_action/);
  assert.match(app, /\$\("supervisor-next"\)\.textContent = supervisor\.next_action/);
});
