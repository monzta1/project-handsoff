"""P0.4: the prompt and protocol preflight before a managed launch, and
`doctor --prompts`."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import handsoff_agent as agent
import handsoff_cli as cli
import handsoff_lib as lib
import handsoff_supervisor as sup


class PromptPreflightTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("HANDSOFF_SKIP_PREFLIGHT")
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-prompt-preflight-")).resolve()
        cli.init_project(self.root, None)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        if self._env is None:
            os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None)
        else:
            os.environ["HANDSOFF_SKIP_PREFLIGHT"] = self._env

    def override(self, role, text, *, declare=True, write=True):
        relative = f"prompts/{role}.md"
        path = self.root / relative
        path.parent.mkdir(exist_ok=True)
        files = {}
        if write:
            path.write_text(text, encoding="utf-8")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if declare:
            lib.atomic_write_json(self.root / lib.OVERRIDES_FILE, {"schema": 1, "files": files})
        return relative

    def engine_text(self, role):
        return lib.engine_resource_path(f"prompts/{role}.md").read_text(encoding="utf-8")

    def test_engine_prompts_pass_for_every_role(self):
        report = lib.prompt_preflight_report(self.root)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["roles"]), set(lib.SELECTABLE_AGENT_ROLES))
        for role in lib.SELECTABLE_AGENT_ROLES:
            self.assertEqual(report["roles"][role]["verdict"], "ok")
            self.assertEqual(report["roles"][role]["source"], "engine")
            self.assertEqual(Path(report["roles"][role]["path"]),
                             lib.engine_resource_path(f"prompts/{role}.md"))
            self.assertEqual(agent._role_prompt(self.root, role),
                             lib.engine_resource_path(f"prompts/{role}.md").read_text(encoding="utf-8").rstrip())

    def test_override_missing_the_reviewer_result_line_is_refused_naming_it(self):
        relative = self.override("reviewer", "A custom reviewer prompt with no protocol line.\n")
        item = lib.prompt_preflight(self.root, "reviewer")
        self.assertEqual((item["source"], item["verdict"]), ("override", "protocol_missing"))
        with self.assertRaises(lib.HandsoffError) as caught:
            agent._role_prompt(self.root, "reviewer")
        message = str(caught.exception)
        self.assertIn("HANDSOFF_REVIEW_RESULT", message)
        self.assertIn(str(self.root / relative), message)
        self.assertIn("repair:", message)
        self.assertIn(lib.OVERRIDES_FILE, message)

    def test_undeclared_override_is_refused(self):
        relative = self.override("implementer", self.engine_text("implementer"), declare=False)
        item = lib.prompt_preflight(self.root, "implementer")
        self.assertEqual(item["verdict"], "undeclared")
        with self.assertRaises(lib.HandsoffError) as caught:
            agent._role_prompt(self.root, "implementer")
        self.assertIn(relative, str(caught.exception))
        self.assertIn("declare it", str(caught.exception))

    def test_declared_override_whose_file_is_absent_is_refused_not_replaced(self):
        relative = self.override("reviewer", self.engine_text("reviewer"), write=False)
        self.assertFalse((self.root / relative).exists())
        item = lib.prompt_preflight(self.root, "reviewer")
        self.assertEqual((item["source"], item["verdict"]), ("override", "declared_missing"))
        with self.assertRaisesRegex(lib.HandsoffError, "declared_missing"):
            agent._role_prompt(self.root, "reviewer")
        # the resolver itself never falls back to the engine prompt either
        with self.assertRaisesRegex(lib.HandsoffError, "declared .* but the file is missing"):
            lib.project_resource_path(self.root, relative)

    def test_supervisor_marker_is_checked(self):
        self.override("supervisor", "Supervise the run carefully.\n")
        item = lib.prompt_preflight(self.root, "supervisor")
        self.assertEqual(item["verdict"], "protocol_missing")
        self.assertEqual(item["missing"], ["HANDSOFF_BROKER_REQUEST"])
        with self.assertRaisesRegex(lib.HandsoffError, "HANDSOFF_BROKER_REQUEST"):
            agent._role_prompt(self.root, "supervisor")
        self.override("supervisor", "Print HANDSOFF_BROKER_REQUEST: {...} per action.\n")
        self.assertEqual(lib.prompt_preflight(self.root, "supervisor")["verdict"], "ok")

    def test_implementer_and_architect_markers(self):
        self.override("implementer", "Implement it.\n")
        self.assertEqual(lib.prompt_preflight(self.root, "implementer")["missing"], ["HANDSOFF_PROGRESS"])
        # the architect's decline line alone satisfies its protocol floor
        self.override("architect", "Decline with HANDSOFF_DESIGN_DECLINE: {...}\n")
        self.assertEqual(lib.prompt_preflight(self.root, "architect")["verdict"], "ok")
        self.override("architect", "Design something.\n")
        self.assertEqual(lib.prompt_preflight(self.root, "architect")["missing"],
                         ["HANDSOFF_DESIGN_PROPOSAL", "HANDSOFF_DESIGN_DECLINE"])

    def test_empty_override_is_refused(self):
        self.override("reviewer", "   \n")
        self.assertEqual(lib.prompt_preflight(self.root, "reviewer")["verdict"], "empty")

    def run_doctor(self, *argv):
        args = sup.build_parser().parse_args(["--root", str(self.root), "doctor", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sup.cmd_doctor(args)
        return code, json.loads(out.getvalue())

    def test_doctor_prompts_lists_each_role_source_and_verdict(self):
        code, report = self.run_doctor("--prompts")
        self.assertEqual(code, 0)
        roles = report["prompts"]["roles"]
        self.assertEqual(set(roles), set(lib.SELECTABLE_AGENT_ROLES))
        for role, item in roles.items():
            self.assertEqual((item["source"], item["verdict"]), ("engine", "ok"), role)
            self.assertTrue(item["path"].endswith(f"prompts/{role}.md"))
        self.assertNotIn("probes", report)
        self.override("reviewer", "No protocol here.\n")
        code, report = self.run_doctor("--prompts")
        self.assertEqual(code, 1)
        reviewer = report["prompts"]["roles"]["reviewer"]
        self.assertEqual((reviewer["source"], reviewer["verdict"]), ("override", "protocol_missing"))
        self.assertIn(lib.OVERRIDES_FILE, reviewer["repair"])


if __name__ == "__main__":
    unittest.main()
