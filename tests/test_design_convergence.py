"""Focused REQ-001 coverage for bounded design-review convergence context."""
from __future__ import annotations

import shutil

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, ROOT, run

import sys
sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class TestDesignConvergence(HandsoffTestCase):
    """The packet must make revision obligations explicit and bounded."""

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts")
        shutil.copy(ROOT / ".handsoff-version", self.tmp / ".handsoff-version")
        config = self.tmp / "handsoff.toml"
        config.write_text(config.read_text().replace('architect = "auto"', 'architect = "host"'))

    def _phase_two(self, item="#1"):
        self.assertEqual(run(["init", "Convergence fixture", "--item", item], self.tmp).returncode, 0)
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement",
                              "[ #1 ] persists a record"], self.tmp).returncode, 0)
        result = run(["advance", "2", "10"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def _proposal(approach, decisions=("one",)):
        return {"summary": "bounded proposal", "approach": list(approach), "tradeoffs": [],
                "decisions": list(decisions), "constraints": [], "verification": ["focused test"]}

    def _review(self):
        result = run(["record-design-review", "--by", "design-reviewer", "--architect",
                      "host-architect", "--request-changes", "--summary", "Needs convergence",
                      "--finding", "Persisted field types are unspecified",
                      "--finding", "The identifier range and storage bound are missing"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_architect_prompt_requires_data_shape_and_revisions(self):
        text = (self.tmp / "prompts" / "architect.md").read_text()
        for fragment in ("Data shape:", "fields, their types", "closed value sets",
                         "integer ranges", "identifier shapes (regex)", "storage bound",
                         "Revisions:", "prior finding", "in order", "resolved",
                         "declined because"):
            self.assertIn(fragment, text)
        self.assertNotIn("—", text)

    def test_prior_findings_are_numbered_in_review_order_and_bounded(self):
        self._phase_two()
        lib.record_design_proposal(self.tmp, None, self._proposal(["No data shape"]),
                                   architect_actor="host-architect")
        self._review()
        packet = lib.managed_design_context(self.tmp, "architect")
        self.assertEqual(packet["prior_findings"], [
            {"number": 1, "text": "Persisted field types are unspecified"},
            {"number": 2, "text": "The identifier range and storage bound are missing"},
        ])
        self.assertIn("unanswered", packet["instructions"])

    def test_revision_and_reviewer_packet_retain_prior_findings(self):
        self._phase_two()
        lib.record_design_proposal(self.tmp, None, self._proposal(["No data shape"]),
                                   architect_actor="host-architect")
        self._review()
        lib.record_design_proposal(
            self.tmp, None,
            self._proposal(["Data shape: record_id string matching ^rec-[a-z0-9]{1,32}$; "
                            "count integer range 0..100; state closed set {open,closed}; "
                            "storage bound 32 records"],
                           ("1 resolved by defining fields; 2 resolved by bounded identifier and storage",)),
            architect_actor="host-architect")
        packet = lib.managed_design_context(self.tmp, "reviewer")
        self.assertEqual([item["number"] for item in packet["prior_findings"]], [1, 2])
        self.assertIn("Persisted field types", packet["prior_findings"][0]["text"])

    def test_no_design_review_has_no_prior_findings(self):
        self._phase_two()
        lib.record_design_proposal(self.tmp, None, self._proposal(["first"]),
                                   architect_actor="host-architect")
        self.assertEqual(lib.managed_design_context(self.tmp, "architect")["prior_findings"], [])

    def test_design_review_findings_are_bounded_to_32_and_512(self):
        self._phase_two()
        status = self.read_status()
        status["design_review"] = {"findings": [{"text": "x" * 700} for _ in range(40)]}
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="test_review", event_message="bounded review")
        findings = lib.managed_design_context(self.tmp, "architect")["prior_findings"]
        self.assertEqual(len(findings), 32)
        self.assertTrue(all(len(item["text"]) == 512 for item in findings))

    def test_autonomous_design_review_default_is_unchanged(self):
        self.assertEqual(lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS, 2)
        self.assertEqual(lib.load_config(self.tmp)["max_autonomous_design_reviews"], 2)
