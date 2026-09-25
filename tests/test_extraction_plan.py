"""#284 and #332: the plan stays true to the code, and the lane rule stays unambiguous.

A migration document is worth nothing once it describes a codebase that no
longer exists. Every number in `docs/ARCHITECTURE-MIGRATION.md` is recomputed
here from `bin/`, so the document and the code cannot drift apart quietly.

The second half is #332. `playbook/lanes.md` carried two rules that appeared
to contradict each other, with the deciding clause buried at the end of a
paragraph. Read on its own, the parallel-lane rule licensed a separate run
per ticket. Three tickets that shipped in one release on 2026-09-25 were
started as three runs and paid three design reviews, three implementation
reviews, four CI passes and three rebase cycles against one of each.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))

ROOT = BIN.parent
PLAN = ROOT / "docs" / "ARCHITECTURE-MIGRATION.md"
LANES = ROOT / "playbook" / "lanes.md"
LIB = BIN / "handsoff_lib.py"
ROUTING = BIN / "handsoff_routing.py"

#: The subsystems #284 names. The plan must account for every one.
TICKET_SUBSYSTEMS = (
    "storage and transactions", "workflow state machine", "evidence and event ledger",
    "agent runtime", "model routing", "fleet registry", "dashboard projection",
)


class ThePlanAccountsForEverySubsystem(unittest.TestCase):
    """REQ-002. The map exists and covers what the ticket asked for."""

    @classmethod
    def setUpClass(cls):
        cls.text = PLAN.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_the_plan_exists_and_is_substantial(self):
        self.assertGreater(len(self.text), 2_000, "the plan is too thin to be a plan")

    def test_every_ticket_subsystem_appears(self):
        missing = [s for s in TICKET_SUBSYSTEMS if s not in self.lower]
        self.assertEqual(missing, [], "these subsystems are named in #284 but absent from the plan")

    def test_each_subsystem_carries_a_state(self):
        """Looked for in a table row, not at the first mention: every
        subsystem is also named in the opening paragraph, where no state
        belongs, and keying on the first occurrence read that instead."""
        rows = [l.lower() for l in self.text.splitlines() if l.strip().startswith("|")]
        for name in TICKET_SUBSYSTEMS:
            # "already its own module" is a recorded state too: Fleet registry
            # was never inside the monolith, and saying so is more useful than
            # forcing it into the extracted/pending vocabulary.
            states = ("extracted", "pending", "already its own module")
            stated = [r for r in rows if name in r and any(v in r for v in states)]
            self.assertTrue(stated, f"{name} has no table row recording its state")

    def test_the_plan_names_owners_and_persisted_schemas(self):
        self.assertIn("persisted schema", self.lower)
        self.assertIn("owner", self.lower)
        self.assertIn("handsoff-status.json", self.text)

    def test_the_plan_states_an_extraction_order_with_reasons(self):
        self.assertIn("outbound coupling", self.lower,
                      "an order without a stated basis is a preference, not a plan")

    def test_the_plan_declares_which_numbers_are_estimates(self):
        """Keyword grouping misclassified six routing symbols. A plan that
        presented those counts as exact would mislead the next extraction."""
        self.assertIn("indicative", self.lower)
        self.assertIn("keyword", self.lower)
        self.assertIn("re-measure", self.lower)


class ThePlanNumbersMatchTheCode(unittest.TestCase):
    """REQ-002: recomputed, so the document cannot rot."""

    @classmethod
    def setUpClass(cls):
        cls.text = PLAN.read_text(encoding="utf-8")

    def _claimed(self, label):
        """The Symbols / Outbound / Inbound cells of the plan's table row."""
        for line in self.text.splitlines():
            if line.strip().startswith("|") and label.lower() in line.lower():
                cells = [c.strip().strip("*") for c in line.split("|")[1:-1]]
                numbers = [c for c in cells if c.isdigit()]
                if len(numbers) >= 3:
                    return tuple(int(n) for n in numbers[:3])
        self.fail(f"no table row with three counts for {label}")

    def test_the_routing_row_matches_the_extracted_module(self):
        symbols, _outbound, _inbound = self._claimed("model routing")
        tree = ast.parse(ROUTING.read_text(encoding="utf-8"))
        defined = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        defined.discard("_lib")
        self.assertEqual(symbols, len(defined),
                         f"the plan claims {symbols} routing symbols; the module defines {len(defined)}")

    def test_the_monolith_line_count_claim_is_current(self):
        """The plan states the size at extraction and the size after. The
        second number must still describe the file."""
        actual = len(LIB.read_text(encoding="utf-8").splitlines())
        # The monolith shrinks with every stage, so the pattern must not be
        # pinned to the range it happened to be in when this was written.
        claimed = [int(m.replace(",", "")) for m in re.findall(r"\b1\d,\d{3}\b", self.text)]
        self.assertIn(actual, claimed,
                      f"handsoff_lib.py is {actual} lines; the plan names {sorted(set(claimed))}")

    def test_the_deferred_primitives_list_matches_the_module(self):
        """The plan names what must move before the deferred imports can be
        lifted. If the module imports something absent from that list, the
        plan understates the remaining work."""
        tree = ast.parse(ROUTING.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_lib":
                imported |= {a.name for a in node.names}
        missing = sorted(n for n in imported if n not in self.text)
        self.assertEqual(missing, [],
                         "the module pulls these primitives from the monolith but the plan "
                         "does not list them as remaining migration work")


class TheLaneRuleLeadsWithTheReleaseBoundary(unittest.TestCase):
    """REQ-005 (#332). Ordering is the fix, because the old ordering misled a run."""

    @classmethod
    def setUpClass(cls):
        cls.text = LANES.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_the_release_boundary_precedes_the_worktree_per_lane_rule(self):
        release = self.lower.find("separate release")
        worktree = self.lower.find("one worktree per lane")
        self.assertNotEqual(release, -1, "the release boundary is not stated at all")
        self.assertNotEqual(worktree, -1, "the worktree rule vanished")
        self.assertLess(release, worktree,
                        "the worktree-per-lane rule comes first again, which is the ordering "
                        "that licensed three runs for three tickets in one release")

    def test_the_one_run_n_items_rule_is_stated_with_the_boundary(self):
        head = self.lower[:self.lower.find("one worktree per lane")]
        self.assertIn("one run", head)
        self.assertIn("n items", head)

    def test_the_measured_cost_is_recorded_beside_the_rule(self):
        """A rule with its price attached survives; a bare rule gets reasoned past."""
        self.assertIn("2026-09-25", self.text)
        self.assertIn("rebase", self.lower)

    def test_the_rule_says_concurrency_inside_one_run_is_available(self):
        """Otherwise the rule reads as "do not parallelise", which is not it."""
        self.assertTrue("side by side" in self.lower or "concurrency" in self.lower,
                        "the rule must say one run still builds items in parallel")


if __name__ == "__main__":
    unittest.main()
