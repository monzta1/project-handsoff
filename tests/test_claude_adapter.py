"""Focused REQ-003 and REQ-004 checks for the Claude adapter boundary."""
import json
import sys
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_agent as agent
import handsoff_lib as lib


class ClaudeAdapterTests(TestCase):
    def test_stream_json_read_only_argv(self):
        cfg = lib.load_config(Path('.'))
        cfg['agents']['reviewer'] = 'claude'
        # The launch spec is computed against a fixture, never the repository's own run.
        with mock.patch.object(lib, "validate_runtime_integrity"), mock.patch.object(lib, "load_config", return_value=cfg), mock.patch.object(agent, "build_role_input", return_value="task"), mock.patch.object(agent, "applicable_design_review_packet", return_value=None), mock.patch.object(agent, "_refuse_reviewer_launch_over_budget"), mock.patch.object(lib, "managed_design_context", return_value=None):
            spec = agent.build_launch_spec(Path('.'), 'reviewer', 'review', which=lambda x: '/bin/claude')
        self.assertIn('--output-format', spec.argv)
        self.assertIn('stream-json', spec.argv)
        self.assertIn('--permission-mode', spec.argv)
        self.assertIn('default', spec.argv)
        self.assertNotIn('plan', spec.argv)

    def test_tool_use_event_has_no_protocol_text(self):
        event = json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}})
        self.assertEqual(agent._claude_logical_lines(event), [])

    def test_assistant_text_delta_is_extracted(self):
        line = json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'HANDSOFF_REVIEW_RESULT: {}'}})
        self.assertEqual(agent._claude_logical_lines(line), ['HANDSOFF_REVIEW_RESULT: {}'])

    def test_plain_claude_output_falls_back(self):
        self.assertEqual(agent._claude_logical_lines('plain protocol text'), ['plain protocol text'])

    def test_implementer_allowlist_contains_configured_checks_and_evidence(self):
        tools = lib.implementer_allowed_tools({'check_commands': ['python3 -m unittest tests.test_claude_adapter'], 'implementer_commands': []})
        self.assertIn('Bash(python3 -m unittest tests.test_claude_adapter)', tools)
        self.assertIn('Bash(python3 bin/handsoff_supervisor.py verify*)', tools)
        self.assertIn('Bash(python3 bin/handsoff_supervisor.py record-symptom-resolved*)', tools)

    def test_implementer_extra_commands_are_preserved(self):
        self.assertIn('Bash(custom command)', lib.implementer_allowed_tools({'check_commands': [], 'implementer_commands': ['Bash(custom command)']}))

    def test_prompts_document_protocol_and_permissions(self):
        root = Path(__file__).resolve().parents[1] / 'prompts'
        reviewer = (root / 'reviewer.md').read_text()
        supervisor = (root / 'supervisor.md').read_text()
        implementer = (root / 'implementer.md').read_text()
        self.assertIn('final message must be exactly the single applicable protocol line', reviewer)
        self.assertIn('ExitPlanMode', supervisor)
        self.assertIn('launcher allows exactly the configured check commands, verify, and record-symptom-resolved', implementer)
