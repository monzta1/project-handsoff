#!/usr/bin/env python3
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class GovernanceDocumentationTests(unittest.TestCase):
    def test_readme_documents_all_four_governance_features(self):
        text = (ROOT / "README.md").read_text()
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
