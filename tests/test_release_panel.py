"""#373: the Fleet snapshot's engine block, the installed engine against
the newest release and each registered project's pin."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_cli  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_fleet_signals as signals  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class ReleasePanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.registry = base / "projects.json"
        # The facts `handsoff version --json` prints for this engine.
        self.installed = handsoff_cli._current_identity()["version"]
        major, minor, patch = lib._version_tuple(self.installed)
        self.bare = f"{major}.{minor}.{patch}"  # the manifest may carry a leading v
        self.assertGreaterEqual(minor, 1, "the fixture needs a previous minor line")
        self.line = f"{major}.{minor}"
        self.pins = {
            "current-line": f"{major}.{minor}.*",
            "previous-line": f"{major}.{minor - 1}.*",
            "older-exact": f"v{major}.{minor - 1}.3",
        }
        self.roots = {}
        for name, pin in self.pins.items():
            root = (base / name).resolve()
            root.mkdir()
            (root / lib.VERSION_PIN_FILE).write_text(pin + "\n", encoding="utf-8")
            self.roots[name] = root
        fleet.save_registry([{"root": str(root), "registered_at": "2026-10-01T00:00:00+00:00"}
                             for root in sorted(self.roots.values())], self.registry)
        # A fake release list newer than the install, cached for a root whose
        # origin is the engine repository, plus another repo's newer tag.
        self.newer = f"v{major}.{minor + 1}.0"
        cache = base / "fleet-issues.json"
        cache.write_text(json.dumps({"schema": 1, "projects": {
            str(self.roots["current-line"]): {"repo": signals.ENGINE_REPO, "fetched_at": "2026-10-04T00:00:00+00:00",
                                              "error": None, "issues": [], "releases": [
                {"tag_name": f"v{self.bare}", "name": "the installed one",
                 "published_at": "2026-09-30T00:00:00Z", "html_url": None},
                {"tag_name": self.newer, "name": "Engine panel", "published_at": "2026-10-03T12:00:00Z",
                 "html_url": None},
                {"tag_name": "v99.0.0", "name": "draft-like", "published_at": None, "html_url": None},
            ]},
            str(self.roots["previous-line"]): {"repo": "someone/else", "fetched_at": None, "error": None,
                                               "issues": [], "releases": [
                {"tag_name": "v98.0.0", "name": "not the engine", "published_at": "2026-10-04T00:00:00Z"}]},
        }}), encoding="utf-8")
        self.issues = signals.IssueCache(path=cache)

    def project(self, block, name):
        return next(row for row in block["projects"] if row["root"] == str(self.roots[name]))

    def test_snapshot_carries_the_engine_block(self):
        snapshot = fleet.build_fleet(self.registry, issues=self.issues)
        engine = snapshot["engine"]
        self.assertEqual(engine["installed"], self.installed)
        self.assertEqual(engine["manifest"], handsoff_cli._current_identity()["manifest_sha256"])
        self.assertIn(engine["manifest_status"], {"current", "stale"})
        self.assertEqual(engine["latest"], {"tag": self.newer, "date": "2026-10-03T12:00:00Z", "title": "Engine panel"})
        self.assertIs(engine["behind"], True)
        self.assertEqual(len(engine["projects"]), 3)
        # the existing badge fields stay beside the block
        self.assertIn("version", engine)
        self.assertIn("install_blocked", engine)

    def test_each_project_carries_pin_satisfied_and_exact_upgrade(self):
        block = fleet.engine_block(self.registry, self.issues)
        current = self.project(block, "current-line")
        self.assertEqual(current, {"root": str(self.roots["current-line"]), "pin": self.pins["current-line"],
                                   "satisfied": True, "upgrade": None})
        previous = self.project(block, "previous-line")
        self.assertEqual(previous["pin"], self.pins["previous-line"])
        self.assertIs(previous["satisfied"], False)
        self.assertEqual(previous["upgrade"],
                         f"handsoff upgrade {self.roots['previous-line']} --to {self.line}.*")
        exact = self.project(block, "older-exact")
        self.assertEqual(exact["pin"], self.pins["older-exact"])
        self.assertIs(exact["satisfied"], False)
        self.assertEqual(exact["upgrade"], f"handsoff upgrade {self.roots['older-exact']} --to v{self.bare}")

    def test_upgrade_command_contract(self):
        self.assertIsNone(fleet.upgrade_command("/p", "0.5.*", "0.5.1"))
        self.assertEqual(fleet.upgrade_command("/p", "0.4.*", "0.5.1"), "handsoff upgrade /p --to 0.5.*")
        self.assertEqual(fleet.upgrade_command("/p", "v0.4.3", "0.5.1"), "handsoff upgrade /p --to v0.5.1")
        self.assertEqual(fleet.upgrade_command("/p", "==0.4.3", "0.5.1"), "handsoff upgrade /p --to v0.5.1")
        self.assertEqual(fleet.upgrade_command("/a b", "0.4.*", "0.5.1"), "handsoff upgrade '/a b' --to 0.5.*")

    def test_not_behind_without_a_newer_release(self):
        facts = {"installed": "99.0.0", "manifest": None, "manifest_status": "current"}
        block = fleet.engine_block(self.registry, self.issues, facts=facts)
        self.assertEqual(block["latest"]["tag"], self.newer)
        self.assertIs(block["behind"], False)
        empty = fleet.engine_block(self.registry, None)
        self.assertIsNone(empty["latest"])
        self.assertIs(empty["behind"], False)


if __name__ == "__main__":
    unittest.main()
