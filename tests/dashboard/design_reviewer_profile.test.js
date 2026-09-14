// #37: Mission Control renders the reviewer tier the next design-review
// launch selects as one line from policy.design_reviewer_selection only
// (tier, adapter/model, reason), names a post-selection refusal instead
// of hiding it, and appends the latest recorded review's tier when there
// is one. Run: node --test tests/dashboard/design_reviewer_profile.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { designReviewerProfileLabel } = require("../../dashboard/lib/dashboard-logic.js");

const root = path.join(__dirname, "..", "..");

test("next selection renders tier, adapter/model, and the reason", () => {
  assert.equal(
    designReviewerProfileLabel({
      current: null,
      next: { tier: "primary", reason: "first_review", adapter: "codex", model: "default", error: null },
    }),
    "Review profile: primary codex/default (first review)",
  );
  assert.equal(
    designReviewerProfileLabel({
      current: { tier: "primary", reason: "first_review", adapter: "codex", model: "default" },
      next: { tier: "followup", reason: "delta_check", adapter: "claude", model: "claude-haiku", error: null },
    }),
    "Review profile: followup claude/claude-haiku (delta check) · last review: primary codex/default (first review)",
  );
});

test("a refused selection is named, never silently substituted", () => {
  const label = designReviewerProfileLabel({
    current: null,
    next: {
      tier: "followup", reason: "delta_check", adapter: "claude", model: "claude-haiku",
      error: "followup reviewer profile unavailable: claude is not on PATH; install it, set fallback_policy.reviewer, or remove reviewer_followup",
    },
  });
  assert.equal(
    label,
    "Review profile: followup claude/claude-haiku (delta check) · BLOCKED: followup reviewer profile unavailable: claude is not on PATH; install it, set fallback_policy.reviewer, or remove reviewer_followup",
  );
});

test("a missing, null, or legacy selection reads as not selected", () => {
  const none = "Review profile: not selected";
  assert.equal(designReviewerProfileLabel(null), none);
  assert.equal(designReviewerProfileLabel(undefined), none);
  assert.equal(designReviewerProfileLabel({}), none);
  assert.equal(designReviewerProfileLabel({ current: null, next: null }), none);
  assert.equal(designReviewerProfileLabel({ next: { tier: 3 } }), none);
  const label = designReviewerProfileLabel({ next: { tier: "primary", reason: null, adapter: null, model: null } });
  assert.equal(label, "Review profile: primary unresolved/default (unknown)");
  assert.doesNotMatch(label, /NaN|undefined/);
});

test("app.js renders the label and the server publishes the policy", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const server = fs.readFileSync(path.join(root, "bin", "handsoff_dashboard.py"), "utf8");
  assert.match(html, /id="design-reviewer-profile"/);
  assert.match(app, /\$\("design-reviewer-profile"\)\.textContent = designReviewerProfileLabel\(policy\.design_reviewer_selection\)/);
  assert.match(server, /"design_reviewer_selection": design_reviewer_selection/);
  assert.match(server, /lib\.design_reviewer_selection_view\(cfg, status, acceptance\)/);
});
