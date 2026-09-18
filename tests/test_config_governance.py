"""Field-note defects 5 and 7: live_commands validated at load; README documents [digest] ignore."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from unittest import TestCase

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

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
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        i = readme.index("[digest] ignore")
        self.assertIn("[documentation] exclude", readme)
        self.assertIn("handsoff-doc: intentional", readme)
        section = readme[max(0, i - 2000): i + 2000]
        self.assertIn("write-ups", section)
        self.assertIn("repository digest", section)

    def test_changelog_names_each_field_note_defect(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in ("--verbose", "installed-engine", "--reaffirm", "automated_and_browser",
                       "live_commands", "[digest] ignore", "network_access", "field notes"):
            self.assertIn(phrase, readme, phrase)
