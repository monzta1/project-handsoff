"""#217: playbook/protocol.md is the one document of the managed-role
protocol, and it says exactly what the code enforces. Every field set here
is read from the validators, never retyped."""
import inspect
import json
import re
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT

sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402

DOC = ROOT / "playbook" / "protocol.md"


def section(text, prefix):
    start = text.index(f"## {prefix}")
    rest = text[start + 3:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def table_rows(block):
    rows = []
    for line in block.splitlines():
        if line.startswith("|") and not line.startswith("|---") and not line.startswith("| field") and not line.startswith("| command"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
    return rows


def codes(cell):
    return set(re.findall(r"`([^`]+)`", cell)) - {"none"}


class ProtocolContractTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()

    def test_every_prefix_the_runner_knows_has_a_section(self):
        """[#217] the prefixes come from the runner's constants."""
        prefixes = {agent.SUPERVISOR_REQUEST_PREFIX, agent.REVIEW_RESULT_PREFIX, agent.DESIGN_RESULT_PREFIX,
                    agent.DECLINE_RESULT_PREFIX, "HANDSOFF_OPERATION:"}
        progress = getattr(agent, "PROGRESS_PREFIX", "HANDSOFF_PROGRESS:")
        prefixes.add(progress)
        for prefix in prefixes:
            self.assertIn(f"## {prefix.rstrip(':')}", self.text, prefix)

    def test_the_broker_table_equals_the_code(self):
        """[#217] acceptance: every command's field set, both ways."""
        block = section(self.text, "HANDSOFF_BROKER_REQUEST")
        base = {"actor", "project_root", "action", "command"}
        documented = {}
        for cells in table_rows(block):
            documented[cells[0].strip("`")] = (codes(cells[1]) | base, codes(cells[2]))
        self.assertEqual(set(documented), set(broker.BROKER_REQUEST_FIELDS))
        for command, (req, opt) in broker.BROKER_REQUEST_FIELDS.items():
            self.assertEqual(documented[command][0], set(req), f"{command} required")
            self.assertEqual(documented[command][1], set(opt), f"{command} optional")
        for command in broker.HUMAN_ONLY_COMMANDS:
            self.assertIn(f"`{command}`", block)
        # the broker really reads the table: every _exact_fields call in the dispatcher goes through it
        source = inspect.getsource(broker._workflow_argv)
        self.assertNotIn("_exact_fields(request, {", source)
        self.assertGreaterEqual(source.count("BROKER_REQUEST_FIELDS[command]"), len(broker.BROKER_REQUEST_FIELDS) - 8)

    def test_the_review_result_table_equals_the_parser(self):
        block = section(self.text, "HANDSOFF_REVIEW_RESULT")
        rows = {cells[0].strip("`"): cells for cells in table_rows(block)}
        source = inspect.getsource(broker.parse_reviewer_result)
        required = set(re.search(r'required = \{([^}]*)\}', source).group(1).replace('"', "").replace(" ", "").split(","))
        allowed = required | set(re.search(r'allowed = required \| \{([^}]*)\}', source).group(1).replace('"', "").replace(" ", "").split(","))
        self.assertEqual(set(rows), allowed)
        for field in required:
            self.assertEqual(rows[field][2], "yes", field)
        for field in allowed - required:
            self.assertEqual(rows[field][2], "no", field)
        self.assertIn(str(broker.MAX_REVIEW_RESULT_BYTES), block)
        for value in ("`approved`", "`changes_requested`", "`yes`", "`not_applicable`", "`unknown`"):
            self.assertIn(value, block)

    def test_the_proposal_decline_operation_and_progress_tables_equal_the_validators(self):
        proposal = section(self.text, "HANDSOFF_DESIGN_PROPOSAL")
        self.assertEqual([cells[0].strip("`") for cells in table_rows(proposal)], list(lib.DESIGN_PROPOSAL_FIELDS))
        decline = section(self.text, "HANDSOFF_DESIGN_DECLINE")
        self.assertEqual({cells[0].strip("`") for cells in table_rows(decline)}, {"reason", "evidence", "alternative"})
        self.assertIn(str(lib.MAX_DECLINE_EVIDENCE), decline)
        operation = section(self.text, "HANDSOFF_OPERATION")
        source = inspect.getsource(lib.validate_operation_line)
        allowed = set(re.search(r'allowed = \{([^}]*)\}', source, re.S).group(1).replace('"', "").replace("\n", "").replace(" ", "").split(","))
        required = set(re.search(r'required = \(([^)]*)\)', source).group(1).replace('"', "").replace(" ", "").split(","))
        rows = {cells[0].strip("`"): cells for cells in table_rows(operation)}
        self.assertEqual(set(rows), allowed)
        for field in allowed:
            self.assertEqual(rows[field][2], "yes" if field in required else "no", field)
        for state in lib.OPERATION_STATES:
            self.assertIn(f"`{state}`", operation)
        progress = section(self.text, "HANDSOFF_PROGRESS")
        rows = {cells[0].strip("`"): cells for cells in table_rows(progress)}
        self.assertEqual(set(rows), {"criterion", "state", "test", "note"})
        if hasattr(lib, "validate_progress_line"):
            for state in lib.PROGRESS_STATES:
                self.assertIn(f"`{state}`", progress)

    def test_the_playbook_lists_the_topic_in_both_indexes(self):
        """[#217] acceptance 2: handsoff playbook protocol."""
        index = json.loads((ROOT / "playbook" / "index.json").read_text())
        self.assertEqual(index["topics"].get("protocol"), "the managed-role protocol")
        self.assertIn("protocol.md", [f["file"] for f in index["files"]])
        self.assertNotIn("protocol.md", index["always_load"])
        human = (ROOT / "playbook" / "INDEX.md").read_text()
        self.assertIn("protocol", human)
        self.assertIn("protocol.md", human)
        self.assertEqual(lib.playbook_text("protocol"), self.text)
        self.assertIn("The managed-role protocol", lib.playbook_text("protocol"))


if __name__ == "__main__":
    unittest.main()
