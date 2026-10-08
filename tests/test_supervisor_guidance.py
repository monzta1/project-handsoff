"""#430 and #431: bundled Supervisor guidance.

#430: concurrent implementers with disjoint `--owns` sets already work
(#359), but neither the supervisor prompt nor the playbook said to use
them, so parallelism only ever happened across lanes. #431: a standing
recommendation on role combinations (the `crews` topic), pointed at from
lanes.md and the supervisor prompt where `[agents]` is chosen, and never
enforced over a project's own `[agents]`.

Source-reading: the guidance is text a role reads, so the text is what is
pinned. Whitespace is normalised so a reflow does not break a pin.
"""
import json
import sys
import unittest
from pathlib import Path

from tests.guards import guard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import handsoff_lib as lib  # noqa: E402

PLAYBOOK = ROOT / "playbook"
PROMPTS = ROOT / "prompts"

#: The byte slack every playbook topic keeps under the launch cap.
MIN_SLACK_BYTES = 512


def _text(path):
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(lanes, heading):
    """One bold-headed paragraph of lanes.md, up to the next bold heading."""
    start = lanes.index(heading)
    end = lanes.find(" **", start + len(heading))
    return lanes[start:end if end != -1 else len(lanes)]


@guard
class ParallelImplementerGuidance(unittest.TestCase):
    """REQ-007: prompts/supervisor.md and playbook/lanes.md (Phase 4)."""

    def setUp(self):
        self.supervisor = _text(PROMPTS / "supervisor.md")
        lanes = _text(PLAYBOOK / "lanes.md")
        self.lanes = _section(lanes, "**Parallel implementers (Phase 4).**")
        self.split = _section(lanes, "**One criterion per Implementer launch (#215).**")

    def test_items_are_mapped_to_the_files_they_change_at_advance_4(self):
        self.assertIn("at `advance 4`, map each work item or criterion to the files it will change",
                      self.supervisor)
        self.assertIn("At `advance 4`, map each item to the files it changes", self.lanes)

    def test_disjoint_groups_get_one_concurrent_implementer_each_with_owns_and_item(self):
        self.assertIn("launch one implementer per group concurrently, each with its own `--owns` "
                      "and `--item` binding", self.supervisor)
        self.assertIn("Disjoint path groups: one implementer each, launched concurrently with "
                      "its own `--owns` and `--item`", self.lanes)

    def test_the_shared_interface_and_each_shared_files_owner_are_fixed_in_every_task(self):
        self.assertIn("fix the shared interface (a helper's name and signature) and every shared "
                      "file's single owner in every task up front", self.supervisor)
        self.assertIn("every task fixes the shared interface and each shared file's single owner",
                      self.lanes)

    def test_overlapping_paths_stay_one_implementer(self):
        self.assertIn("Overlapping paths (a shared core module) stay one implementer", self.supervisor)
        self.assertIn("Overlapping paths stay one implementer", self.lanes)

    def test_a_task_too_big_for_one_budget_is_split_the_same_way(self):
        self.assertIn("A task too large for one session's token budget is split the same way "
                      "rather than retried whole", self.supervisor)
        self.assertIn("a task too big for one budget is split the same way, not retried whole",
                      self.split)


CREWS = {
    "Default": "Architect and Supervisor on the host (Claude or Copilot); Implementer Codex; "
               "independent Reviewer Claude in a separate managed session; browser QA optional "
               "and non-authoritative.",
    "Strongest independence": "Architect, Supervisor and Implementer Claude; Reviewer Codex; "
                              "live or browser verification by Playwright or a QA agent.",
    "High-risk infrastructure": "Implementer Codex; code Reviewer Claude; a separate security or "
                                "config reviewer; deterministic checks plus live smoke tests; the "
                                "human owner approves the observed business outcome, so agents "
                                "are never the only approval layer.",
    "UI-heavy": "Implementer Codex; Reviewer Claude; browser tester (QA agent); accessibility via "
                "Playwright plus axe-core; the final decision rests with the Handsoff reviewer, "
                "plus the human for important workflows. Generated QA tests stay untrusted "
                "evidence until reviewed.",
    "Small fix": "One implementer (Codex or Claude); a reviewer from a different provider in a "
                 "separate session; focused tests plus one smoke check. No full multi-agent lane "
                 "for a one-line, low-risk change.",
}

RULES = [
    "The same session never implements and reviews its own work.",
    "Prefer different providers for implementation and final review.",
    "The supervisor stays separate from the implementer.",
    "Browser QA only when the change has meaningful UI behavior.",
    "Security or data specialists only when the risk justifies the cost.",
    "Decisions come from evidence, never agent confidence or prose.",
    "Human confirmation for business behavior that tests cannot objectively establish.",
]


@guard
class CrewsGuidance(unittest.TestCase):
    """REQ-010: the `crews` topic, its references, and that it is advice."""

    def setUp(self):
        self.crews = _text(PLAYBOOK / "crews.md")

    def test_every_recommended_crew_is_carried_verbatim(self):
        for name, body in CREWS.items():
            self.assertIn(f"**{name}", self.crews, name)
            self.assertIn(body, self.crews, name)

    def test_the_seven_rules_are_carried_in_order(self):
        positions = []
        for number, rule in enumerate(RULES, start=1):
            self.assertIn(f"{number}. {rule}", self.crews, rule)
            positions.append(self.crews.index(rule))
        self.assertEqual(positions, sorted(positions))

    def test_it_is_a_recommendation_never_enforced_over_a_projects_agents(self):
        self.assertIn("A recommendation only: a project's own `[agents]` always wins and nothing "
                      "here is enforced", self.crews)
        self.assertIn("a recommendation never enforced over a project's `[agents]`",
                      _text(PLAYBOOK / "lanes.md"))
        self.assertIn("It is a recommendation only, never enforced over a project's own `[agents]`",
                      _text(PROMPTS / "supervisor.md"))

    def test_lanes_and_the_supervisor_point_at_the_topic_where_agents_is_chosen(self):
        self.assertIn("**Crews.** Choose `[agents]` with the `crews` topic",
                      _text(PLAYBOOK / "lanes.md"))
        self.assertIn("When choosing `[agents]`, consult the playbook topic `crews`",
                      _text(PROMPTS / "supervisor.md"))

    def test_the_topic_is_declared_in_the_index_and_the_index_page(self):
        index = json.loads((PLAYBOOK / "index.json").read_text(encoding="utf-8"))
        self.assertIn("crews", index["topics"])
        self.assertIn({"file": "crews.md", "topics": ["crews"]}, index["files"])
        self.assertIn("| crews | crews.md |", (PLAYBOOK / "INDEX.md").read_text(encoding="utf-8"))
        self.assertIn("# Recommended crews", lib.playbook_section("crews"))


@guard
class GuidanceKeepsThePlaybookBudget(unittest.TestCase):
    """REQ-007 and REQ-010: every playbook topic keeps its byte slack."""

    def test_every_topic_and_the_core_keep_their_slack(self):
        for topic in [None] + sorted(lib.playbook_index()["topics"]):
            slack = lib.MAX_PLAYBOOK_SECTION_BYTES - len(lib.playbook_section(topic).encode("utf-8"))
            self.assertGreaterEqual(slack, MIN_SLACK_BYTES, topic)

    def test_no_em_dash_in_the_guidance(self):
        for path in (PLAYBOOK / "crews.md", PLAYBOOK / "lanes.md", PLAYBOOK / "INDEX.md",
                     PROMPTS / "supervisor.md"):
            self.assertNotIn(chr(0x2014), path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
