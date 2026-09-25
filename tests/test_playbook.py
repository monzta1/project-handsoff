"""#208: the lane playbook ships with the engine. `handsoff playbook` prints
it from any project; every file is in the manifest and the wheel; every
managed launch carries the index and the lane recipe ahead of the project's
own knowledge base; on the machine that holds the local KB, nothing in its
engine rules is missing from the playbook."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402

PLAYBOOK = ROOT / "playbook"
LOCAL_KB = Path.home() / "Projects" / "fm9-tone" / "kb" / "HANDSOFF.md"


class PlaybookShipsWithTheEngineTests(unittest.TestCase):
    def test_host_owns_continuous_monitoring_until_completion(self):
        lanes = (PLAYBOOK / "lanes.md").read_text(encoding="utf-8")
        self.assertIn("Continuous monitoring is a host obligation", lanes)
        self.assertIn("until `advance\n8 100`", lanes)
        self.assertIn("without a prompt or shorthand", lanes)

    def test_the_cli_prints_the_index_and_a_topic_from_a_thin_project(self):
        thin = Path(tempfile.mkdtemp(prefix="handsoff-thin-"))
        index = subprocess.run([sys.executable, str(ROOT / "bin" / "handsoff_cli.py"), "playbook"],
                               cwd=str(thin), capture_output=True, text=True, timeout=30)
        self.assertEqual(index.returncode, 0, index.stderr)
        self.assertTrue(index.stdout.startswith("# Handsoff playbook"))
        for topic in ("lanes", "landing", "reviewers", "lessons-lane"):
            self.assertIn(f"| {topic} |", index.stdout)
        lessons = subprocess.run([sys.executable, str(ROOT / "bin" / "handsoff_cli.py"), "playbook", "lessons-lane"],
                                 cwd=str(thin), capture_output=True, text=True, timeout=30)
        self.assertEqual(lessons.returncode, 0, lessons.stderr)
        self.assertTrue(lessons.stdout.startswith("# Lessons: running and landing a lane"))
        self.assertIn("One `[#N]` tag per criterion", lessons.stdout)
        bad = subprocess.run([sys.executable, str(ROOT / "bin" / "handsoff_cli.py"), "playbook", "nope"],
                             cwd=str(thin), capture_output=True, text=True, timeout=30)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("playbook topic is not declared: nope", bad.stdout + bad.stderr)

    def test_every_playbook_file_is_in_the_manifest_and_the_wheel(self):
        files = sorted(p.name for p in PLAYBOOK.iterdir() if p.is_file())
        self.assertEqual(files, ["INDEX.md", "index.json", "landing.md", "lanes.md",
                                 "lessons-agents.md", "lessons-binding.md",
                                 "lessons-evidence.md", "lessons-lane.md",
                                 "protocol.md", "reviewers.md"])
        manifest = json.loads((ROOT / "handsoff-runtime.json").read_text())
        for name in files:
            self.assertIn(f"playbook/{name}", manifest["files"], name)
        pyproject = (ROOT / "pyproject.toml").read_text()
        line = next(l for l in pyproject.splitlines() if l.startswith('"share/handsoff/playbook"'))
        for name in files:
            self.assertIn(f'"playbook/{name}"', line, name)
        # the manifest generator lists them, so a stale copy is refused like any runtime file
        self.assertIn('"playbook/lanes.md"', (ROOT / "bin" / "handsoff_manifest.py").read_text())
        index = json.loads((PLAYBOOK / "index.json").read_text())
        self.assertEqual(index["always_load"], ["INDEX.md", "lanes.md"])
        self.assertEqual(set(index["topics"]), {"lanes", "landing", "reviewers", "protocol",
                                                "lessons-lane", "lessons-agents",
                                                "lessons-evidence", "lessons-binding"})
        for item in index["files"]:
            self.assertTrue((PLAYBOOK / item["file"]).is_file(), item["file"])
        for name in files:
            if name.endswith(".md"):
                self.assertNotIn(chr(0x2014), (PLAYBOOK / name).read_text(), f"{name}: no em dash")

    def test_every_managed_launch_carries_the_playbook_ahead_of_the_project_kb(self):
        tmp = Path(tempfile.mkdtemp(prefix="handsoff-playbook-launch-"))
        (tmp / "handsoff.toml").write_text("", encoding="utf-8")
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            text = agent.build_role_input(tmp, "reviewer", "review it")
        self.assertTrue(text.startswith("# Handsoff playbook\n\n## playbook/INDEX.md"))
        self.assertIn("## playbook/lanes.md", text)
        # the order every launch keeps: playbook, then the project KB, then
        # role context (sandbox, design context, a packet), then the role
        # prompt, then the task (review F1.2)
        self.assertLess(text.index("# Handsoff playbook"), text.index("# Sandbox"))
        self.assertLess(text.index("# Sandbox"), text.index("ROLE PROMPT"))
        self.assertLess(text.index("ROLE PROMPT"), text.index("# Assigned task"))
        self.assertNotIn("## playbook/lessons-lane.md", text, "a topic rides only when asked")
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            with_topic = agent.build_role_input(tmp, "reviewer", "review it", "lessons-lane")
        self.assertIn("## playbook/lessons-lane.md", with_topic)
        # every topic's section fits the bound the engine enforces at launch (F1.2)
        index = json.loads((PLAYBOOK / "index.json").read_text())
        for name in sorted(index["topics"]):
            section = lib.playbook_section(name)
            self.assertLessEqual(len(section.encode("utf-8")), lib.MAX_PLAYBOOK_SECTION_BYTES, name)
        self.assertLessEqual(len(lib.playbook_section(None).encode("utf-8")), lib.MAX_PLAYBOOK_SECTION_BYTES)
        # an oversize playbook is refused at launch; proven on a temp copy, never the real one
        import shutil
        copy = Path(tempfile.mkdtemp(prefix="handsoff-playbook-copy-")) / "playbook"
        shutil.copytree(PLAYBOOK, copy)
        (copy / "huge.md").write_text("x" * (lib.MAX_PLAYBOOK_SECTION_BYTES + 1))
        big = {**index, "files": [*index["files"], {"file": "huge.md", "topics": ["lanes"]}]}
        with patch_engine("playbook_index", return_value=big), \
                patch_engine("playbook_root", return_value=copy):
            with self.assertRaisesRegex(lib.HandsoffError, "over 16384"):
                lib.playbook_section("lanes")
        # a project with its own [briefing] KB gets both, playbook first
        import shutil
        shutil.copy(ROOT / "tests/fixtures/briefing-index.json", tmp / "index.json")
        shutil.copytree(ROOT / "tests/fixtures/briefing-kb", tmp / "kb")
        (tmp / "handsoff.toml").write_text("[briefing]\nindex = \"index.json\"\nroot = \"kb\"\n", encoding="utf-8")
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            both = agent.build_role_input(tmp, "implementer", "build it", "ui")
        self.assertLess(both.index("# Handsoff playbook"), both.index("# Knowledge base briefing"))
        self.assertLess(both.index("# Knowledge base briefing"), both.index("ROLE PROMPT"))
        self.assertLess(both.index("ROLE PROMPT"), both.index("# Assigned task"))
        self.assertIn("## UI.md", both)
        # a topic named in both indexes rides from both (F1.1); one in neither is refused
        collide = json.loads((tmp / "index.json").read_text())
        collide["topics"]["lessons-lane"] = "the project's own lessons"
        collide["files"].append({"file": "UI.md", "topics": ["lessons-lane"]})
        (tmp / "index.json").write_text(json.dumps(collide))
        with mock.patch.object(agent, "_role_prompt", return_value="ROLE PROMPT"):
            shared = agent.build_role_input(tmp, "implementer", "build it", "lessons-lane")
        self.assertIn("## playbook/lessons-lane.md", shared)
        self.assertIn("## UI.md", shared)
        with self.assertRaisesRegex(lib.HandsoffError, "not declared in the index: nowhere"):
            lib.briefing_section(tmp, lib.load_config(tmp), "nowhere")

    @unittest.skipUnless(LOCAL_KB.is_file(), "the local knowledge base is on one machine only")
    def test_nothing_in_the_local_kb_engine_rules_is_missing_from_the_playbook(self):
        """Bullet-level: every lesson bullet in the local KB names a thing
        the playbook also names (its first distinctive words)."""
        local = LOCAL_KB.read_text()
        playbook = "\n".join((PLAYBOOK / n).read_text() for n in ("lanes.md", "landing.md", "reviewers.md",
                                                   "lessons-lane.md", "lessons-agents.md",
                                                   "lessons-evidence.md")).lower()
        lessons = local.split("**Lessons of 2026-09-21")[1] if "**Lessons of 2026-09-21" in local else ""
        bullets = [b.strip() for b in re.split(r"\n- ", lessons) if b.strip()][1:]
        keys = {
            "manifest": "regenerate the runtime manifest", "design-propose": "design_proposal_recorded",
            "worktree's engine": "worktree's engine", "node suite": "whole node suite",
            "Landing order": "landing order", "rerun on the same head": "rerun on the same head",
            "one `[#N]` tag": "one `[#n]` tag", "Local timings": "local timings", "init --by": "init --by",
            "Fixtures never inherit": "fixtures never inherit", "Measure a new external call": "measure a new external call",
            "Two hosts release": "two hosts release", "red shard": "red shard", "every reader": "every reader",
            "board sees nothing": "board sees nothing",
        }
        for bullet in bullets:
            hit = next((needle for key, needle in keys.items() if key.lower() in bullet.lower()), None)
            self.assertIsNotNone(hit, f"no playbook key for the local bullet: {bullet[:80]}")
            self.assertIn(hit, playbook, f"the playbook lacks: {hit}")


if __name__ == "__main__":
    unittest.main()
