"""#418: init with --item names the scope, so it seeds no placeholder
criterion; design-propose and advance 2 refuse the empty registry and say
how to fill it. init without --item keeps the placeholder."""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

PROPOSAL = {"summary": "s", "approach": ["Data shape: none"], "tradeoffs": ["t"],
            "decisions": ["d"], "constraints": ["c"], "verification": ["v"]}


class InitScopeTests(HandsoffTestCase):
    def _host_architect(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('architect = "auto"', 'architect = "host"', 1))

    def _propose(self):
        proposal = self.tmp / "proposal.json"
        proposal.write_text(json.dumps(PROPOSAL))
        return run(["design-propose", "--file", str(proposal), "--by", "host-architect"], cwd=self.tmp)

    def _assert_names_the_commands(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(lib.EMPTY_REGISTRY_REFUSAL, result.stdout)
        self.assertIn("criterion-add", result.stdout)
        self.assertIn("criteria-apply", result.stdout)

    def test_init_with_items_starts_with_an_empty_registry(self):
        r = run(["init", "Scoped run", "--item", "#70", "--item", "#71"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        acceptance = self.read_acceptance()
        self.assertEqual(acceptance["criteria"], [])
        self.assertFalse(any(c.get("requirement") == lib.PLACEHOLDER_REQUIREMENT for c in acceptance["criteria"]))
        self.assertEqual([item["id"] for item in acceptance["work_items"]], ["issue-70", "issue-71"])
        self.assertEqual(self.read_status()["requirement_coverage"]["failing"], 0)
        # the empty registry is a valid state to add to
        added = run(["criterion-add", "REQ-001", "--type", "primary_fix", "--requirement", "[#70] The thing works",
                     "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual([c["id"] for c in self.read_acceptance()["criteria"]], ["REQ-001"])

    def test_init_without_items_keeps_the_placeholder(self):
        self.init("Unscoped run")
        criteria = self.read_acceptance()["criteria"]
        self.assertEqual(len(criteria), 1)
        self.assertEqual(criteria[0]["requirement"], lib.PLACEHOLDER_REQUIREMENT)
        self.assertEqual(criteria[0]["tests"], lib.PLACEHOLDER_TESTS)

    def test_design_propose_and_advance_2_refuse_an_empty_registry_naming_the_commands(self):
        self._host_architect()
        r = run(["init", "Scoped run", "--item", "#70"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._assert_names_the_commands(self._propose())
        self.assertIsNone(self.read_status().get("design_proposal"))
        self._assert_names_the_commands(run(["advance", "2", "20"], cwd=self.tmp))
        self.assertEqual(self.read_status()["phase_number"], 1)
        # with a criterion the same proposal is recorded
        added = run(["criterion-add", "REQ-001", "--type", "primary_fix", "--requirement", "[#70] The thing works",
                     "--verification", "automated", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        proposed = self._propose()
        self.assertEqual(proposed.returncode, 0, proposed.stdout + proposed.stderr)
        self.assertIn("DESIGN_PROPOSAL_RECORDED", proposed.stdout)


if __name__ == "__main__":
    unittest.main()
