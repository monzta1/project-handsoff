"""Field-note defects 5 and 7: live_commands validated at load; README documents [digest] ignore."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from unittest import TestCase

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run, docs_text

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class LiveCommandLoadValidationTests(HandsoffTestCase):
    def _set_live(self, command):
        toml = self.tmp / "handsoff.toml"
        import json
        text = re.sub(r"live_commands = \[[^\]]*\]", lambda m: "live_commands = [" + json.dumps(command) + "]", toml.read_text(), count=1, flags=re.S)
        toml.write_text(text)

    def test_operator_in_live_command_is_refused_at_load(self):
        self.init("Live command fixture")
        self._set_live("jump -J host 'bash -s -- --url x' < healthz-monitor.sh")
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.load_config(self.tmp)
        self.assertIn("checks.live_commands[0]", str(caught.exception))
        self.assertIn(lib.PLAIN_COMMAND_MESSAGE, str(caught.exception))
        for command in ("validate", "status", "doctor"):
            result = run([command], cwd=self.tmp)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn("shell expansion or control operators", result.stdout + result.stderr, command)

    def test_plain_live_command_still_loads_and_check_commands_unchanged(self):
        self.init("Live command fixture")
        self._set_live("scripts/live_smoke.sh --url https://example.test/health")
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["live_check_commands"], ["scripts/live_smoke.sh --url https://example.test/health"])
        self.assertEqual(run(["validate"], cwd=self.tmp).returncode, 0)
        # verify-live's own check is the very same function
        with self.assertRaisesRegex(lib.HandsoffError, lib.PLAIN_COMMAND_MESSAGE):
            lib.assert_plain_command("a | b")
        self.assertEqual(lib.assert_plain_command("python3 -m unittest tests.test_x -v"), ["python3", "-m", "unittest", "tests.test_x", "-v"])


class DigestIgnoreDocsTests(TestCase):
    def test_readme_documents_digest_ignore_next_to_documentation_exclude(self):
        readme = docs_text()
        i = readme.index("[digest] ignore")
        self.assertIn("[documentation] exclude", readme)
        self.assertIn("handsoff-doc: intentional", readme)
        section = readme[max(0, i - 2000): i + 2000]
        self.assertIn("write-ups", section)
        self.assertIn("repository digest", section)

    def test_changelog_names_each_field_note_defect(self):
        readme = docs_text()
        for phrase in ("--verbose", "installed-engine", "--reaffirm", "automated_and_browser",
                       "live_commands", "[digest] ignore", "network_access", "field notes"):
            self.assertIn(phrase, readme, phrase)


class DogfoodExecutionProfileTests(HandsoffTestCase):
    def _enable_dogfood_waivers(self):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace("deployment_requires_explicit_approval = true",
                                        "deployment_requires_explicit_approval = false")
        text = text.replace("require_design_approval = true", "require_design_approval = false")
        toml.write_text(text)

    def test_checked_in_profile_is_explicit_and_posture_is_recorded_at_init(self):
        self._enable_dogfood_waivers()
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["execution_profile"], "dogfood")
        self.init("Dogfood posture")
        posture = self.read_status()["approval_posture"]
        self.assertEqual(posture["profile"], "dogfood")
        self.assertTrue(posture["waivers_active"])
        events = [__import__("json").loads(line) for line in
                  (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["kind"] == "approval_posture_recorded" for row in events), 1)

    def test_shared_or_production_profile_cannot_inherit_dogfood_waivers(self):
        self._enable_dogfood_waivers()
        toml = self.tmp / "handsoff.toml"
        for profile in ("unattended", "shared", "production", "safe"):
            changed = re.sub(r'(?m)^profile = "dogfood"$', f'profile = "{profile}"', toml.read_text())
            toml.write_text(changed)
            with self.assertRaisesRegex(lib.HandsoffError, "cannot inherit dogfood waivers"):
                lib.load_config(self.tmp)
            toml.write_text(re.sub(r'(?m)^profile = ".*"$', 'profile = "dogfood"', toml.read_text()))

    def test_safe_template_and_dogfood_profile_are_governance_bound(self):
        template = (ROOT / "templates" / "handsoff.toml").read_text()
        self.assertIn('profile = "safe"', template)
        dogfood = lib.load_config(self.tmp)
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text().replace("deployment_requires_explicit_approval = false",
                                        "deployment_requires_explicit_approval = true")
        text = text.replace("require_design_approval = false", "require_design_approval = true")
        text = text.replace('profile = "dogfood"', 'profile = "safe"')
        toml.write_text(text)
        self.assertNotEqual(lib.config_hash(dogfood), lib.config_hash(lib.load_config(self.tmp)))
