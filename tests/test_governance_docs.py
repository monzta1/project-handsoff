#!/usr/bin/env python3
import json
import unittest

from tests.test_handsoff_supervisor import docs_text
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class GovernanceDocumentationTests(unittest.TestCase):
    def test_readme_documents_all_four_governance_features(self):
        text = docs_text()
        for heading in (
            "## Review attempts and the convergence cap",
            "## Automatic recovery of stalled runs",
            "## Focused checks versus full regressions",
            "## Work items and the per-item status table",
        ):
            self.assertIn(heading, text)
        for command in ("review-cap-override", "recover --by", "regression-request",
                        "regression-run", "work-items-sync"):
            self.assertIn(command, text)

    def test_every_role_prohibits_regression_gate_bypass(self):
        required = "Never start one in an external shell to evade the product boundary."
        for path in sorted((ROOT / "prompts").glob("*.md")):
            self.assertIn(required, path.read_text(), path.name)

    def test_playbook_makes_provider_quota_cross_vendor_supervisor_discretion(self):
        lanes = (ROOT / "playbook" / "lanes.md").read_text()
        reviewers = (ROOT / "playbook" / "reviewers.md").read_text()
        # #315: lessons.md split by theme; the provider-quota rule is a
        # managed-session lesson.
        lessons = (ROOT / "playbook" / "lessons-agents.md").read_text()
        supervisor = (ROOT / "prompts" / "supervisor.md").read_text()
        for text in (lanes, reviewers, lessons, supervisor):
            normalized = " ".join(text.split())
            self.assertIn("another vendor", normalized)
            self.assertIn("per-session token", normalized)
        self.assertIn("Supervisor discretion", lanes)
        self.assertIn("equivalent-or-stronger", reviewers)
        self.assertIn("provider_quota", supervisor)

    def test_schemas_document_new_structured_state(self):
        status = json.loads((ROOT / "schemas" / "status.schema.json").read_text())
        acceptance = json.loads((ROOT / "schemas" / "acceptance.schema.json").read_text())
        properties = status["properties"]
        for field in ("review_attempts", "review_cap_overrides", "recovery_attempts",
                      "recovery_lease", "regression_requests", "active_work_item"):
            self.assertIn(field, properties)
        self.assertIn("work_items", acceptance["properties"])


if __name__ == "__main__":
    unittest.main()
