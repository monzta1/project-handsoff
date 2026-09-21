"""#159: `[workflow] require_design_approval` in handsoff.toml.

Default true keeps every existing behaviour and every existing hash; false
lets `init` waive the Pilot's design click while the independent design
review stays mandatory.
"""
import json
import os
import re
import sys
import unittest

from tests.test_handsoff_supervisor import docs_text
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import handsoff_lib as lib  # noqa: E402
from test_handsoff_supervisor import HandsoffTestCase, run  # noqa: E402


def _set_workflow_key(path: Path, key: str, value: str | None) -> None:
    """Write `key = value` under [workflow] (or drop the key when value is None)."""
    text = path.read_text()
    text = re.sub(rf"^{key} = .*\n", "", text, flags=re.M)
    if value is not None:
        text = text.replace("[workflow]\n", f"[workflow]\n{key} = {value}\n", 1)
    path.write_text(text)


class ConfigKeyTests(HandsoffTestCase):
    def test_absent_true_and_false_load_and_non_bool_is_refused(self):
        toml = self.tmp / "handsoff.toml"
        self.assertTrue(lib.load_config(self.tmp)["require_design_approval"])
        _set_workflow_key(toml, "require_design_approval", "true")
        self.assertTrue(lib.load_config(self.tmp)["require_design_approval"])
        _set_workflow_key(toml, "require_design_approval", "false")
        self.assertFalse(lib.load_config(self.tmp)["require_design_approval"])
        _set_workflow_key(toml, "require_design_approval", '"no"')
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.load_config(self.tmp)
        self.assertIn("workflow.require_design_approval must be boolean", str(caught.exception))

    def test_config_hash_is_unchanged_at_the_default_and_changes_when_waived(self):
        toml = self.tmp / "handsoff.toml"
        absent = lib.config_hash(lib.load_config(self.tmp))
        # The hash a pre-#159 engine computed: the same governance keys without the new one.
        legacy = dict(lib.load_config(self.tmp))
        legacy.pop("require_design_approval")
        with mock.patch.object(lib, "GOVERNANCE_CONFIG_KEYS", tuple(k for k in lib.GOVERNANCE_CONFIG_KEYS if k != "require_design_approval")):
            pre_159 = lib.config_hash(legacy)
        self.assertEqual(absent, pre_159)
        _set_workflow_key(toml, "require_design_approval", "true")
        self.assertEqual(lib.config_hash(lib.load_config(self.tmp)), absent)
        _set_workflow_key(toml, "require_design_approval", "false")
        self.assertNotEqual(lib.config_hash(lib.load_config(self.tmp)), absent)


class WaivedGateTests(HandsoffTestCase):
    """A run on a project that waives the Pilot's design click."""

    def setUp(self):
        super().setUp()
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('architect = "auto"', 'architect = "host"'))
        _set_workflow_key(toml, "require_design_approval", "false")
        self.init("Waived design gate")
        run(["criterion-update", "REQ-001", "--requirement", "Waived design"], self.tmp)
        run(["advance", "2", "20"], self.tmp)

    def propose(self):
        path = self.tmp / "proposal.json"
        path.write_text(json.dumps({
            "summary": "Waived proposal", "approach": ["Keep the existing path"], "tradeoffs": [],
            "decisions": ["Bind decisions to the proposal"], "constraints": [], "verification": ["Run focused checks"],
        }))
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_COMPANION_SESSION_ID": "host-session-1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        result = run(["design-propose", "--file", str(path), "--by", "architect-1"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def review(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "host-session-reviewer", "CODEX_COMPANION_SESSION_ID": ""}, clear=True):
            result = run(["record-design-review", "--approve", "--by", "reviewer-1",
                          "--architect", "architect-1", "--summary", "independent"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def events(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]

    def test_init_writes_the_flag_false_and_logs_the_waiver(self):
        status = self.read_status()
        self.assertFalse(status["requires_design_approval"])
        self.assertTrue(status["requires_design_review"])
        waived = [e for e in self.events() if e["kind"] == "design_approval_waived"]
        self.assertEqual(len(waived), 1)
        self.assertEqual(waived[0]["config_key"], "require_design_approval")
        self.assertIn("require_design_approval = false", waived[0]["message"])
        self.assertIn("independent design review remains required", waived[0]["message"])

    def test_phase_3_needs_the_independent_review_but_no_pilot_click(self):
        self.propose()
        refused = run(["advance", "3", "30"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("design review", (refused.stdout + refused.stderr).lower())
        review = self.review()
        self.assertIn("DESIGN_REVIEW_APPROVED", review.stdout)
        status = self.read_status()
        self.assertIsNone(status["design_approved"])
        self.assertEqual(status["status"], "in_progress")
        self.assertIn("waived by config", status["next_action"])
        advanced = run(["advance", "3", "30"], self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)
        self.assertIsNone(self.read_status()["design_approved"])

    def test_the_dashboard_never_asks_the_pilot_for_the_design_click(self):
        self.propose()
        self.review()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        import handsoff_dashboard as dashboard
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot.get("input_required") or {}
        self.assertFalse(request.get("required"), request)
        offered = {item.get("action_id") or item.get("kind") or item.get("id") for item in snapshot.get("operator_actions") or []
                   if item.get("availability") == "actionable"}
        self.assertNotIn("design_approve", offered, offered)


class DefaultGateTests(HandsoffTestCase):
    def test_a_default_project_still_flags_the_run_and_still_needs_the_click(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('architect = "auto"', 'architect = "host"'))
        self.init("Default design gate")
        self.assertTrue(self.read_status()["requires_design_approval"])
        self.assertEqual([e for e in (json.loads(l) for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines())
                          if e["kind"] == "design_approval_waived"], [])


class DocsTests(unittest.TestCase):
    def test_readme_and_template_document_the_key(self):
        readme = docs_text()
        self.assertIn("require_design_approval", readme)
        template = (ROOT / "templates" / "handsoff.toml").read_text()
        self.assertRegex(template, r"(?m)^require_design_approval = true", "the template names the key at its default")


if __name__ == "__main__":
    unittest.main()
