"""Focused checks for the #175 managed-session knowledge briefing."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-briefing-test-"))
        shutil.copy(ROOT / "tests/fixtures/briefing-index.json", self.tmp / "index.json")
        shutil.copytree(ROOT / "tests/fixtures/briefing-kb", self.tmp / "kb")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, enabled=True):
        if enabled:
            (self.tmp / "handsoff.toml").write_text(
                "[briefing]\nindex = \"index.json\"\nroot = \"kb\"\n",
                encoding="utf-8",
            )
        return lib.load_config(self.tmp)

    def test_fixture_has_tonecommand_manifest_shape(self):
        manifest = json.loads((ROOT / "tests/fixtures/briefing-index.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertIsInstance(manifest["topics"], dict)
        self.assertIsInstance(manifest["always_load"], list)
        self.assertTrue(all(isinstance(item, dict) and "file" in item and "topics" in item
                            for item in manifest["files"]))

    def test_always_load_and_one_launch_topic_are_prepended_in_index_order(self):
        section = lib.briefing_section(self.tmp, self._config(), "ui")
        self.assertTrue(section.startswith("# Knowledge base briefing\n\n## INDEX.md"))
        self.assertLess(section.index("## INDEX.md"), section.index("## UI.md"))
        self.assertNotIn("HARDWARE.md", section)
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            text = agent.build_role_input(self.tmp, "implementer", "do the task", "ui")
        # #208: the engine's playbook rides ahead of the project's knowledge base
        self.assertTrue(text.startswith("# Handsoff playbook\n\n## playbook/INDEX.md"))
        self.assertLess(text.index("# Handsoff playbook"), text.index("# Knowledge base briefing"))
        self.assertIn("# Assigned task\n\ndo the task", text)

    def test_missing_selected_file_refuses_before_prompt_assembly(self):
        self._config()
        (self.tmp / "kb" / "UI.md").unlink()
        with self.assertRaisesRegex(lib.HandsoffError, "briefing file is missing"):
            lib.briefing_section(self.tmp, lib.load_config(self.tmp), "ui")

    def test_missing_index_refuses_before_session_reservation(self):
        self._config()
        (self.tmp / "index.json").unlink()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")
        with mock.patch.object(lib, "create_agent_session") as reserve:
            with self.assertRaisesRegex(lib.HandsoffError, "briefing index is missing"):
                agent.build_launch_spec(self.tmp, "implementer", "do the task",
                                        which=lambda _: "/bin/codex", skip_preflight=True)
        reserve.assert_not_called()

    def test_topic_requires_configured_briefing_and_unknown_topic_is_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, r"requires a \[briefing\] block"):
            lib.briefing_section(self.tmp, self._config(False), "ui")
        with self.assertRaisesRegex(lib.HandsoffError, "not declared"):
            lib.briefing_section(self.tmp, self._config(), "missing")

    def test_no_briefing_block_preserves_role_input(self):
        cfg = self._config(False)
        toml_before = (self.tmp / "handsoff.toml").read_bytes() if (self.tmp / "handsoff.toml").exists() else None
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            text = agent.build_role_input(self.tmp, "architect", "do the task")
        self.assertNotIn("# Knowledge base briefing", text)
        self.assertIn("# Handsoff playbook", text, "#208: the playbook needs no [briefing] block")
        self.assertIn("ROLE PROMPT\n\n# Assigned task\n\ndo the task", text)
        self.assertIsNone(cfg["briefing"])
        self.assertEqual((self.tmp / "handsoff.toml").read_bytes() if (self.tmp / "handsoff.toml").exists() else None,
                         toml_before)

    def test_topic_is_only_a_launch_input_and_is_not_persisted(self):
        self._config()
        before = (self.tmp / "handsoff.toml").read_bytes()
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            text = agent.build_role_input(self.tmp, "implementer", "do the task", "ui")
        self.assertIn("## UI.md", text)
        self.assertNotIn("## HARDWARE.md", text)
        self.assertEqual((self.tmp / "handsoff.toml").read_bytes(), before)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())

    def test_prompts_state_briefing_authority(self):
        for role in ("reviewer", "implementer"):
            prompt = (ROOT / "prompts" / f"{role}.md").read_text(encoding="utf-8")
            self.assertIn("The briefing is authoritative for process rules; the diff is the evidence, not the story.", prompt)


if __name__ == "__main__":
    unittest.main()
