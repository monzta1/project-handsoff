#!/usr/bin/env python3
"""Test that documentation is current with installed command forms and API contracts."""

import json
import argparse
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

import handsoff_cli as cli
import handsoff_lib as lib
import handsoff_supervisor


class TestDocumentationCurrent(unittest.TestCase):
    """Verify documentation references are current and complete."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.repo_root = Path(__file__).parent.parent
        cls.doc_files = {
            'README': cls.repo_root / 'README.md',
            'INSTALL': cls.repo_root / 'INSTALL.md',
            'PACKAGE': cls.repo_root / 'PACKAGE.md',
            'REFERENCE': cls.repo_root / 'docs' / 'REFERENCE.md',
        }
        cls.historical_files = {
            'HANDOFF_GOVERNANCE_28_31': cls.repo_root / 'HANDOFF_GOVERNANCE_28_31.md',
            'FIELD-NOTES': cls.repo_root / 'docs' / 'FIELD-NOTES.md',
            'governance-design': cls.repo_root / 'docs' / 'governance-design.md',
            'ARCHITECTURE-MIGRATION': cls.repo_root / 'docs' / 'ARCHITECTURE-MIGRATION.md',
        }
        cls.old_command = re.compile(
            r"(?:python\d*(?:\.\d+)?\s+)?(?:\S*/)?bin/handsoff_(?:supervisor|dashboard|agent|cli|fleet)\.py"
        )

    def test_req_001_drop_in_commands_marked_in_reference_files(self):
        """REQ-001: Every drop-in occurrence in doc files has intentional marker above it."""
        for name, path in self.doc_files.items():
            with self.subTest(file=name):
                if not path.exists():
                    self.fail(f"{name} not found at {path}")

                text = path.read_text(encoding="utf-8")
                lines = text.splitlines()

                for match in self.old_command.finditer(text):
                    line_number = text.count("\n", 0, match.start())
                    # Check that this is not a supervisor/agent/fleet invocation after replacement
                    matched_text = match.group(0)
                    if "supervisor" in matched_text or "agent" in matched_text or "fleet" in matched_text:
                        # This should have been replaced or marked
                        if line_number > 0:
                            prev_line = lines[line_number - 1].strip()
                            self.assertIn(
                                "handsoff-doc: intentional",
                                prev_line,
                                f"{name} line {line_number + 1}: drop-in command not marked with intentional marker"
                            )
                        else:
                            self.fail(f"{name} line {line_number + 1}: drop-in command at start of file, cannot be marked")

    def test_req_002_all_supervisor_subcommands_documented(self):
        """REQ-002: Every supervisor subcommand appears in REFERENCE.md as a whole token."""
        reference_path = self.repo_root / 'docs' / 'REFERENCE.md'
        reference_text = reference_path.read_text(encoding="utf-8")

        parser = handsoff_supervisor.build_parser()

        subparsers_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break

        self.assertIsNotNone(subparsers_action, "Could not find subparsers in the supervisor parser")

        commands = sorted(subparsers_action.choices.keys())

        for name in commands:
            with self.subTest(command=name):
                pattern = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
                self.assertIsNotNone(
                    re.search(pattern, reference_text),
                    f"REFERENCE.md missing documentation for '{name}'"
                )

    def test_req_003_release_adapter_contract_correct(self):
        """REQ-003: Release adapter docstrings mention read-before-act and advisory operation_key."""
        release_tx_path = self.repo_root / 'bin' / 'handsoff_release_transaction.py'
        text = release_tx_path.read_text(encoding="utf-8")

        # Check module docstring
        module_match = re.search(r'"""(.*?)"""', text, re.DOTALL)
        self.assertIsNotNone(module_match, "Module docstring not found")
        module_doc = module_match.group(1)

        self.assertIn("reads before it acts", module_doc,
                     "Module docstring should mention 'reads before it acts'")
        self.assertIn("adopts an exact match", module_doc,
                     "Module docstring should mention 'adopts an exact match'")
        self.assertIn("operation_key is advisory", module_doc,
                     "Module docstring should mention operation_key is advisory")
        self.assertNotIn("MUST make actions idempotent", module_doc,
                        "Module docstring should not contain 'MUST make actions idempotent'")

        # Check ReleaseAdapter docstring
        adapter_match = re.search(
            r'class ReleaseAdapter\(Protocol\):\s+"""(.*?)"""',
            text, re.DOTALL
        )
        self.assertIsNotNone(adapter_match, "ReleaseAdapter docstring not found")
        adapter_doc = adapter_match.group(1)

        self.assertIn("reads before it acts", adapter_doc,
                     "ReleaseAdapter docstring should mention 'reads before it acts'")
        self.assertIn("adopts an exact match", adapter_doc,
                     "ReleaseAdapter docstring should mention 'adopts an exact match'")
        self.assertIn("operation_key is advisory", adapter_doc,
                     "ReleaseAdapter docstring should mention operation_key is advisory")

    def test_req_004_historical_records_marked(self):
        """REQ-004: Historical records have markers before drop-in occurrences; audit is clean."""
        for name, path in self.historical_files.items():
            if not path.exists():
                continue

            with self.subTest(file=name):
                text = path.read_text(encoding="utf-8")
                lines = text.splitlines()

                for match in self.old_command.finditer(text):
                    line_number = text.count("\n", 0, match.start())
                    if line_number > 0:
                        prev_line = lines[line_number - 1].strip()
                        self.assertIn(
                            "handsoff-doc: intentional",
                            prev_line,
                            f"{name} line {line_number + 1}: drop-in command not marked"
                        )
                    else:
                        self.fail(f"{name} line {line_number + 1}: drop-in command at start of file")

        # Run the documentation audit
        try:
            result = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, '{self.repo_root}/bin'); "
                 f"import handsoff_cli as cli; "
                 f"from pathlib import Path; "
                 f"diag = cli._documentation_diagnosis(Path('{self.repo_root}'), "
                 f"{{'version': '0.5.3', 'source': 'installed-engine'}}); "
                 f"print(len(diag['diagnostics']))"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                self.fail(f"Documentation audit failed: {result.stderr}")
            diagnostic_count = int(result.stdout.strip())
            self.assertEqual(diagnostic_count, 0,
                           f"Documentation audit found {diagnostic_count} issues")
        except Exception as e:
            self.fail(f"Could not run documentation audit: {e}")


if __name__ == "__main__":
    unittest.main()
