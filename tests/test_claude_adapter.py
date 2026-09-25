"""Focused REQ-003 and REQ-004 checks for the Claude adapter boundary."""
import json
import sys
from pathlib import Path
from unittest import TestCase, mock

from tests.engine_patch import patch_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_agent as agent
import handsoff_lib as lib


class ClaudeAdapterTests(TestCase):
    def test_stream_json_read_only_argv(self):
        cfg = lib.load_config(Path('.'))
        cfg['agents']['reviewer'] = 'claude'
        cfg['reviewer_isolation'] = {
            'compatibility_mode': True, 'compatibility_approved': True,
        }
        # The launch spec is computed against a fixture, never the repository's own run.
        with mock.patch.object(lib, "validate_runtime_integrity"), patch_engine("load_config", return_value=cfg), mock.patch.object(agent, "build_role_input", return_value="task"), mock.patch.object(agent, "applicable_design_review_packet", return_value=None), mock.patch.object(agent, "_refuse_reviewer_launch_over_budget"), mock.patch.object(lib, "managed_design_context", return_value=None), \
                mock.patch.object(lib, "evaluate_launch_rules", return_value=None), \
                patch_engine("status_path", return_value=Path("/nonexistent-handsoff-status")):
            # the launch rules read the repository's own run (#165); a checkout
            # at Phase 1 would refuse the reviewer here, which is not this test
            spec = agent.build_launch_spec(Path('.'), 'reviewer', 'review', which=lambda x: '/bin/claude', skip_preflight=True)
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
        # A runtime drop-in root (this repository has bin/) keeps the script form.
        tools = lib.implementer_allowed_tools({'check_commands': ['python3 -m unittest tests.test_claude_adapter'], 'implementer_commands': []}, Path('.'))
        self.assertIn('Bash(python3 -m unittest tests.test_claude_adapter)', tools)
        self.assertIn('Bash(python3 bin/handsoff_supervisor.py verify*)', tools)
        self.assertIn('Bash(python3 bin/handsoff_supervisor.py record-symptom-resolved*)', tools)
        self.assertNotIn('Bash(handsoff supervisor verify*)', tools)

    def test_installed_engine_project_gets_console_forms(self):
        """Field-note defect 2: an installed-engine project has no bin/, so the
        drop-in script form matched nothing and record-symptom-resolved was refused."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tools = lib.implementer_allowed_tools({'check_commands': [], 'implementer_commands': []}, Path(tmp), which=lambda name: '/opt/venv/bin/handsoff')
            self.assertNotIn('Bash(python3 bin/handsoff_supervisor.py verify*)', tools)
            self.assertIn('Bash(handsoff supervisor verify*)', tools)
            self.assertIn('Bash(handsoff supervisor record-symptom-resolved*)', tools)
            self.assertIn('Bash(/opt/venv/bin/handsoff supervisor verify*)', tools)
            self.assertIn('Bash(/opt/venv/bin/handsoff supervisor record-symptom-resolved*)', tools)
            # console not on PATH: only the bare console form
            tools = lib.implementer_allowed_tools({'check_commands': [], 'implementer_commands': []}, Path(tmp), which=lambda name: None)
            self.assertEqual([t for t in tools if 'supervisor' in t], ['Bash(handsoff supervisor verify*)', 'Bash(handsoff supervisor record-symptom-resolved*)'])
            section = lib.implementer_permissions_section(Path(tmp), which=lambda name: '/opt/venv/bin/handsoff')
            self.assertIn('`handsoff supervisor verify ...`', section)
            self.assertIn('`/opt/venv/bin/handsoff supervisor record-symptom-resolved ...`', section)
            self.assertIn('--root is unnecessary', section)

    def test_claude_argv_carries_verbose_next_to_stream_json(self):
        """Field-note defect 1: the CLI refuses --output-format stream-json under
        --print without --verbose, so every managed Claude role exited 1 at launch."""
        argv = lib.claude_argv('/bin/claude', 'implementer', ['Read'], 'claude-opus-5')
        i = argv.index('--output-format')
        self.assertEqual(argv[i - 1], '--verbose')
        self.assertEqual(argv[i + 1], 'stream-json')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'Read')
        self.assertEqual(argv[-2:], ['--model', 'claude-opus-5'])
        read_only = lib.claude_argv('/bin/claude', 'reviewer', None)
        self.assertIn('--verbose', read_only)
        self.assertEqual(read_only[read_only.index('--permission-mode') + 1], 'default')
        self.assertNotIn('--model', read_only)
        # both launch paths go through the helper
        cfg = lib.load_config(Path('.'))
        cfg['agents']['implementer'] = 'claude'
        with mock.patch.object(lib, "validate_runtime_integrity"), patch_engine("load_config", return_value=cfg), mock.patch.object(agent, "build_role_input", return_value="task"), mock.patch.object(agent, "applicable_design_review_packet", return_value=None), mock.patch.object(agent, "_refuse_reviewer_launch_over_budget"), mock.patch.object(lib, "managed_design_context", return_value=None), patch_engine("status_path", return_value=Path("/nonexistent-handsoff-status")):
            spec = agent.build_launch_spec(Path('.'), 'implementer', 'build', which=lambda x: '/bin/claude', skip_preflight=True)
            fallback = agent.build_profile_launch_spec(Path('.'), 'implementer', 'build', {'adapter': 'claude', 'model': 'default'}, which=lambda x: '/bin/claude', skip_preflight=True)
        for argv in (spec.argv, fallback.argv):
            i = argv.index('--output-format')
            self.assertEqual(argv[i - 1], '--verbose', argv)

    def test_implementer_role_input_names_permitted_forms(self):
        with mock.patch.object(lib, "resume_scope_section", return_value=""), patch_engine("load_config", return_value=lib.load_config(Path('.'))):
            text = agent.build_role_input(Path('.'), 'implementer', 'build it')
        self.assertIn('# Permitted supervisor commands', text)
        self.assertIn('`python3 bin/handsoff_supervisor.py verify ...`', text)

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
