const test = require("node:test");
const assert = require("node:assert/strict");
const { reviewRoundLabel } = require("../../dashboard/lib/dashboard-logic.js");

test("review round label uses structured effective cap", () => {
  assert.equal(reviewRoundLabel({ review_round: 2, max_review_rounds: 3, effective_max_review_rounds: 3 }), "2 / 3");
  assert.equal(reviewRoundLabel({ review_round: 3, max_review_rounds: 3, effective_max_review_rounds: 4, review_cap_overrides: 1 }), "3 / 4 (1 override)");
  assert.equal(reviewRoundLabel({ review_round: 5, effective_max_review_rounds: 7, review_cap_overrides: 2 }), "5 / 7 (2 overrides)");
});
