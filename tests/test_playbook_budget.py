"""#315: a lesson is added without evicting another.

The ticket asks to split the always-loaded core from on-demand topics.
That split already existed: `always_load` is `INDEX.md` plus `lanes.md`,
7,263 bytes assembled, against a 16,384 cap. What did not exist was a
bound on any single *topic*, and one topic had grown to fill the rest:
`lessons.md` at 9,203 bytes took the worst-case launch to 16,198, leaving
186 bytes. Two lanes on 2026-09-24 each compressed four older lessons to
fit two new ones, which is eviction decided by whoever was closest to the
ceiling.

`lessons.md` is now three themed topics, so a lesson goes to the file it
belongs in and every lessons topic has thousands of bytes of room. The
budget is enforced here, per topic, with the number, instead of being
discovered when a managed launch is refused.

`protocol.md` is now the tightest topic and is declared as such rather
than quietly permitted: it is a generated-shape reference that grows when
the protocol grows, which is rarer than a lesson, and it is the next
candidate to split if it does.
"""
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

PLAYBOOK = BIN.parent / "playbook"

#: Free bytes a topic must leave under the cap. Set from measurement, not
#: taste: the lessons topics leave 4,350 to 7,357, so 4,096 is a real margin
#: every one of them clears while still refusing a topic that has grown into
#: the space the next lesson needs. It has already done that job once:
#: lessons-evidence fell to 3,314 and the three lessons that pushed it there
#: became lessons-binding.md rather than evicting four older ones.
MIN_TOPIC_SLACK_BYTES = 4096

#: Topics that do not meet the margin, each with the reason and the slack
#: measured when it was declared. Declared rather than filtered, so a
#: second tight topic cannot appear without someone writing down why.
DECLARED_TIGHT_TOPICS = {
    "protocol": (
        "a reference derived from the validators: it grows when the protocol "
        "grows, which is far rarer than adding a lesson, and it is the next "
        "candidate to split if it does. Measured slack when declared: 1001",
        512),
}


def _slack(topic):
    return lib.MAX_PLAYBOOK_SECTION_BYTES - len(lib.playbook_section(topic).encode("utf-8"))


class EveryTopicLeavesRoomToGrow(unittest.TestCase):
    """REQ-001: the budget is enforced per topic, with the number."""

    def setUp(self):
        self.topics = sorted(lib.playbook_index()["topics"])

    def test_every_topic_clears_the_margin_or_is_declared_tight(self):
        thin = {}
        for topic in self.topics:
            slack = _slack(topic)
            floor = DECLARED_TIGHT_TOPICS.get(topic, (None, MIN_TOPIC_SLACK_BYTES))[1]
            if slack < floor:
                thin[topic] = (slack, floor)
        self.assertEqual(
            thin, {},
            "these topics have grown into the space the next lesson needs; "
            "split the file rather than evicting from it (topic: slack, floor)")

    def test_no_topic_can_exceed_the_cap_at_all(self):
        for topic in self.topics:
            self.assertGreater(_slack(topic), 0,
                               f"{topic} would be refused at a managed launch")

    def test_the_core_alone_is_far_from_the_cap(self):
        """always_load rides on every launch, so it is paid every time."""
        self.assertGreaterEqual(_slack(None), 8192, "the core has grown into the topic budget")

    def test_each_declared_tight_topic_states_a_reason(self):
        for topic, (reason, floor) in DECLARED_TIGHT_TOPICS.items():
            self.assertIn(topic, self.topics, f"{topic} is declared tight but is not a topic")
            self.assertGreaterEqual(len(reason), 60, f"{topic} has no stated reason")
            self.assertGreater(floor, 0)

    def test_a_declared_tight_topic_that_is_no_longer_tight_is_reported(self):
        """An exception kept after it stops applying hides the next one."""
        for topic in DECLARED_TIGHT_TOPICS:
            self.assertLess(
                _slack(topic), MIN_TOPIC_SLACK_BYTES,
                f"{topic} now clears the margin; remove it from DECLARED_TIGHT_TOPICS")

    def test_the_lessons_topics_all_clear_the_margin(self):
        """The ticket's own goal, stated as its own assertion."""
        lessons = [t for t in self.topics if t.startswith("lessons")]
        self.assertGreaterEqual(len(lessons), 3, "the lessons split is missing")
        for topic in lessons:
            self.assertNotIn(topic, DECLARED_TIGHT_TOPICS)
            self.assertGreaterEqual(_slack(topic), MIN_TOPIC_SLACK_BYTES, topic)


class TheIndexIsClosed(unittest.TestCase):
    """REQ-001: no file unreachable, no topic pointing at nothing."""

    def setUp(self):
        self.manifest = lib.playbook_index()

    def test_every_file_on_disk_is_reachable(self):
        declared = set(self.manifest.get("always_load") or [])
        declared |= {item["file"] for item in self.manifest.get("files", [])}
        on_disk = {p.name for p in PLAYBOOK.glob("*.md")}
        self.assertEqual(sorted(on_disk - declared), [],
                         "these playbook files are reachable from no topic and no always_load")

    def test_every_declared_file_exists(self):
        for item in self.manifest.get("files", []):
            self.assertTrue((PLAYBOOK / item["file"]).is_file(), item["file"])
        for name in self.manifest.get("always_load") or []:
            self.assertTrue((PLAYBOOK / name).is_file(), name)

    def test_every_topic_names_at_least_one_file(self):
        for topic in self.manifest["topics"]:
            files = [i["file"] for i in self.manifest.get("files", []) if topic in i.get("topics", [])]
            self.assertTrue(files, f"topic {topic} resolves to no file")

    def test_the_retired_lessons_topic_is_gone_from_both_sides(self):
        self.assertNotIn("lessons", self.manifest["topics"])
        self.assertFalse((PLAYBOOK / "lessons.md").exists())

    def test_the_index_page_lists_every_topic(self):
        """INDEX.md is the map a role reads; a topic missing from it is a
        topic nobody asks for."""
        page = (PLAYBOOK / "INDEX.md").read_text(encoding="utf-8")
        for topic in self.manifest["topics"]:
            self.assertIn(topic, page, f"{topic} is not listed in INDEX.md")


class TheManifestCoversTheSameFiles(unittest.TestCase):
    """REQ-001: the index and the runtime manifest are two registries of
    one file set, and they must agree.

    Splitting lessons.md broke manifest generation outright, because
    handsoff_manifest.py carries its own hardcoded list and still named a
    file that no longer existed. The index-closure test above passed
    throughout: it checks that every file is reachable from a topic, which
    says nothing about whether the manifest covers it. A playbook file the
    manifest does not cover can change without invalidating evidence.
    """

    def _manifest_playbook_paths(self):
        import ast
        source = (BIN / "handsoff_manifest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith("playbook/"):
                found.add(node.value)
        return found

    def test_the_manifest_covers_every_playbook_file_on_disk(self):
        on_disk = {f"playbook/{p.name}" for p in PLAYBOOK.iterdir() if p.is_file()}
        missing = sorted(on_disk - self._manifest_playbook_paths())
        self.assertEqual(missing, [],
                         "these playbook files are not covered by the runtime manifest, "
                         "so they can change without invalidating evidence")

    def test_the_wheel_packages_every_playbook_file(self):
        """The fifth registry of the same file set.

        pyproject.toml lists the playbook files installed to
        share/handsoff/playbook. It is separate from the index, separate
        from the manifest, and it is what a built wheel actually ships. A
        file missing here installs absent while the manifest expects it
        present, so the installed engine fails its own integrity check.
        """
        packaged = self._pyproject_playbook_paths()
        on_disk = {f"playbook/{p.name}" for p in PLAYBOOK.iterdir() if p.is_file()}
        self.assertEqual(sorted(on_disk - packaged), [],
                         "these playbook files exist but the wheel would not ship them")

    def test_the_wheel_packages_no_playbook_file_that_is_gone(self):
        """A dead entry breaks the sdist and wheel build outright."""
        stale = sorted(p for p in self._pyproject_playbook_paths()
                       if not (BIN.parent / p).is_file())
        self.assertEqual(stale, [],
                         "pyproject names playbook files that do not exist; the build fails")

    def test_all_three_registries_name_the_same_files(self):
        """Index, runtime manifest and wheel packaging, compared directly.

        Splitting lessons.md had to be done in five places. Closing two of
        them left the build broken and three suites crashing, so the set
        is asserted as one thing rather than three.
        """
        manifest = self._manifest_playbook_paths()
        packaged = self._pyproject_playbook_paths()
        index = set(self.manifest_topics_files())
        self.assertEqual(manifest, packaged,
                         "the runtime manifest and the wheel disagree about the playbook")
        self.assertEqual(manifest, index,
                         "the runtime manifest and playbook/index.json disagree")

    def manifest_topics_files(self):
        """Every content file the index names, plus the index itself.

        index.json is a covered runtime file and is packaged, but it does
        not appear among its own entries: it is the map, not a topic. That
        one asymmetry is stated here rather than papered over by a looser
        comparison, so any other difference still fails.
        """
        manifest = lib.playbook_index()
        names = set(manifest.get("always_load") or [])
        names |= {item["file"] for item in manifest.get("files", [])}
        names.add("index.json")
        return {f"playbook/{n}" for n in names}

    def _pyproject_playbook_paths(self):
        import re
        text = (BIN.parent / "pyproject.toml").read_text(encoding="utf-8")
        return set(re.findall(r'"(playbook/[^"]+)"', text))

    def test_the_manifest_names_no_playbook_file_that_is_gone(self):
        stale = sorted(p for p in self._manifest_playbook_paths()
                       if not (BIN.parent / p).is_file())
        self.assertEqual(stale, [],
                         "the manifest names playbook files that do not exist, "
                         "so regenerating it fails outright")


class NoLessonWasLostInTheSplit(unittest.TestCase):
    """REQ-001: the content moved; it was not summarised away.

    Derived from git rather than from a copy kept beside it, so the check
    is against what was actually there before.
    """

    @staticmethod
    def _bullets(text):
        """Every top-level bullet, each with its continuation lines.

        Written as an explicit scan rather than a split-and-rejoin: the
        first version of this helper prepended a marker to a chunk that
        already carried one, which doubled the first bullet of every file
        and made three real lessons look lost. The split itself had the
        same off-by-one, so the test and the thing it checked were wrong
        in the same way.
        """
        bullets, current = [], None
        for line in text.splitlines():
            if line.startswith("- "):
                if current is not None:
                    bullets.append("\n".join(current).rstrip())
                current = [line]
            elif current is not None:
                if line.strip() == "":
                    bullets.append("\n".join(current).rstrip())
                    current = None
                else:
                    current.append(line)
        if current is not None:
            bullets.append("\n".join(current).rstrip())
        return bullets

    def _original_bullets(self):
        import subprocess
        result = subprocess.run(
            ["git", "show", "origin/main:playbook/lessons.md"],
            cwd=BIN.parent, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("origin/main not available to compare against")
        return self._bullets(result.stdout)

    def _split_bullets(self):
        bullets = []
        for name in sorted(PLAYBOOK.glob("lessons-*.md")):
            bullets += self._bullets(name.read_text(encoding="utf-8"))
        return bullets

    def test_every_original_bullet_survives_somewhere(self):
        original = self._original_bullets()
        moved = {" ".join(b.split()) for b in self._split_bullets()}
        missing = [b for b in original if " ".join(b.split()) not in moved]
        self.assertEqual(missing, [], "these lessons were lost in the split")

    def test_no_bullet_was_duplicated_across_topics(self):
        moved = [" ".join(b.split()) for b in self._split_bullets()]
        duplicates = sorted({b for b in moved if moved.count(b) > 1})
        self.assertEqual(duplicates, [], "a lesson appears in more than one topic")

    def test_no_lesson_was_dropped_and_new_ones_are_allowed(self):
        """The lane's whole point is that a lesson can be added without
        evicting one, so this floors the count rather than fixing it: every
        original bullet must survive, and the total may grow."""
        self.assertGreaterEqual(len(self._split_bullets()), len(self._original_bullets()))


if __name__ == "__main__":
    unittest.main()
