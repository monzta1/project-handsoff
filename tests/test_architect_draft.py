"""#445: a host-drafted proposal reaches the managed Architect inline, never
as a path its sandbox may not read, and a failed brokered workflow command
says why it failed.

On 2026-10-10 an Architect was told to emit a proposal the host had saved in
its own scratchpad; the sandbox refused the read and the Pilot pasted it by
hand. Earlier in that run an Architect criteria request ended protocol_refused
with only "brokered workflow command exited with status 1".

REQ-001: `--draft FILE` is read and validated on the host and inlined under
`# Host-drafted design proposal`, also in a replacement's input; an Architect
task naming an existing file outside the root is refused before any session
is reserved. REQ-002: the broker's error and the Architect's protocol_refused
reason carry the workflow command's stderr."""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_broker as broker  # noqa: E402
import handsoff_lib as lib  # noqa: E402
from tests.engine_patch import patch_engine  # noqa: E402
from tests.fixture_state import write_version_pin  # noqa: E402

CLAUDE = lambda name: f"/usr/local/bin/{name}"  # noqa: E731
EURO = "€"  # three UTF-8 bytes, six when JSON-escaped


def proposal(**overrides):
    value = {"summary": "Hand the draft over inline.",
             "approach": ["Data shape: no persisted change."],
             "tradeoffs": [], "decisions": ["Inline, not a path."], "constraints": [],
             "verification": ["tests/test_architect_draft.py"]}
    value.update(overrides)
    return value


def serialized(value):
    return json.dumps(lib.validate_design_proposal(value), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def boundary_proposal(target):
    """A valid proposal of multi-byte text whose inlined JSON is exactly
    `target` UTF-8 bytes (its escaped file form is about twice that)."""
    value = proposal()
    arrays = ["approach", "decisions", "verification", "tradeoffs", "constraints"]

    def gap():
        return target - len(serialized(value).encode("utf-8"))

    def append(item):
        field = next(name for name in arrays if len(value[name]) < 8)
        value[field] = value[field] + [item]
    while gap() > 2000:
        append(EURO * 500)  # 1503 bytes with its comma and quotes
    while gap() > 500:
        append(EURO * 100)  # 303 bytes
    value["summary"] += "a" * gap()
    assert len(serialized(value).encode("utf-8")) == target
    return value


class _InputPipe:
    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)

    def close(self):
        return None


class _FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, pid=4245):
        self.pid = pid
        self.returncode = returncode
        self.stdin = _InputPipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


class ArchitectDraftCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "prompts", self.tmp / "prompts", copy_function=shutil.copyfile)
        write_version_pin(self.tmp)
        self.init("Architect draft #445")
        self.outside = Path(tempfile.mkdtemp(prefix="hs-445-scratchpad-")).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.assertNotIn(self.tmp.resolve(), self.outside.parents)

    def draft_file(self, value, name="design.json"):
        path = self.outside / name
        path.write_text(json.dumps(value), encoding="utf-8")  # ensure_ascii: the escaped form
        return path

    def ledger(self):
        return ((self.tmp / "handsoff-status.json").read_bytes(),
                (self.tmp / "handsoff-events.jsonl").read_bytes())

    def spec(self, task="Design the fix.", **kwargs):
        with patch_engine("validate_runtime_integrity", return_value=None):
            return runtime.build_launch_spec(self.tmp, "architect", task, which=CLAUDE, **kwargs)


class DraftInline(ArchitectDraftCase):
    """REQ-001"""

    def test_the_draft_is_inlined_exactly_and_names_no_path(self):
        value = proposal(summary="Résumé of the fix — inline.")
        path = self.draft_file(value)
        spec = self.spec(draft=path)
        expected = serialized(value)
        self.assertIn(f"{runtime.HOST_DRAFT_HEADING}\n\n", spec.stdin)
        section = spec.stdin.split(runtime.HOST_DRAFT_HEADING, 1)[1].split("# Assigned task", 1)[0]
        self.assertIn(expected, section)
        self.assertIn("there is no file to open", section)
        self.assertNotIn(str(path), spec.stdin)
        self.assertNotIn("\\u", section)  # ensure_ascii=False: no escaping growth
        self.assertLess(spec.stdin.index(runtime.HOST_DRAFT_HEADING), spec.stdin.index("# Assigned task"))
        # without --draft the input carries no draft section
        self.assertNotIn(runtime.HOST_DRAFT_HEADING, self.spec().stdin)

    def test_an_invalid_draft_and_a_draft_for_another_role_are_refused(self):
        before = self.ledger()
        bad = {"not json": None}
        for value, message in ((bad, "design proposal must contain exactly"),
                               (proposal(approach=[]), "approach must contain 1 to 8")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(lib.HandsoffError, message):
                    self.spec(draft=self.draft_file(value))
        (self.outside / "broken.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(lib.HandsoffError, "is not UTF-8 JSON"):
            self.spec(draft=self.outside / "broken.json")
        with self.assertRaisesRegex(lib.HandsoffError, "cannot be read"):
            self.spec(draft=self.outside / "missing.json")
        good = self.draft_file(proposal())
        for role in ("implementer", "reviewer"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(lib.HandsoffError, "--draft applies to architect launches only"):
                    runtime.build_launch_spec(self.tmp, role, "task", which=CLAUDE, draft=good)
        self.assertEqual(self.ledger(), before)

    def test_a_unicode_draft_at_the_cap_launches_and_one_byte_over_is_refused(self):
        at_cap = boundary_proposal(runtime.DRAFT_MAX_BYTES)
        spec = self.spec(draft=self.draft_file(at_cap))
        self.assertIn(serialized(at_cap), spec.stdin)
        # the file is the escaped form, well past the cap: only the inlined size counts
        self.assertGreater(self.draft_file(at_cap).stat().st_size, runtime.DRAFT_MAX_BYTES + 8192)
        before = self.ledger()
        over = boundary_proposal(runtime.DRAFT_MAX_BYTES + 1)
        with self.assertRaises(lib.HandsoffError) as refused:
            self.spec(draft=self.draft_file(over))
        self.assertIn(f"serializes to {runtime.DRAFT_MAX_BYTES + 1} UTF-8 bytes", str(refused.exception))
        self.assertIn(f"the cap is {runtime.DRAFT_MAX_BYTES}", str(refused.exception))
        self.assertEqual(self.ledger(), before)
        self.assertNotIn("agent_sessions", json.loads(before[0]) or {})

    def test_a_draft_launch_over_the_replacement_budget_is_refused(self):
        path = self.draft_file(proposal())
        size = len(self.spec(draft=path).stdin.encode("utf-8"))
        before = self.ledger()
        reserve = runtime.MAX_REPLACEMENT_INPUT_BYTES - size + 1
        with mock.patch.object(runtime, "REPLACEMENT_HANDOFF_RESERVE_BYTES", reserve), \
                self.assertRaises(lib.HandsoffError) as refused:
            self.spec(draft=path)
        self.assertIn(f"architect input with --draft is {size} UTF-8 bytes; the budget is {size - 1}",
                      str(refused.exception))
        self.assertEqual(self.ledger(), before)

    def test_a_replacement_keeps_the_draft_byte_for_byte(self):
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        at_cap = boundary_proposal(runtime.DRAFT_MAX_BYTES)
        spec = self.spec(draft=self.draft_file(at_cap))
        cfg = lib.load_config(self.tmp)
        payload = {"profiles": lib.agent_profiles(cfg), "fallbacks": lib.fallback_profiles(cfg),
                   "max_failovers_per_role": 2}
        # a catalogued same-vendor model, so the adaptive tier floor admits it
        payload["fallbacks"]["architect"] = [{"adapter": "claude", "model": "claude-opus-5"}]
        self.assertEqual(spec.adapter, "claude")
        self.assertNotEqual(spec.model, "claude-opus-5")
        lib.update_agent_settings(self.tmp, payload)
        result = proposal(summary="The Architect's revision.")
        first = _FakeProcess(stderr="runner crashed\n", returncode=1)
        second = _FakeProcess(f"{runtime.DESIGN_RESULT_PREFIX} {json.dumps(result)}\n")
        launches = [first, second]
        with mock.patch.object(lib, "launch_preflight", return_value={"state": "ready"}), \
                patch_engine("validate_runtime_integrity", return_value=None):
            code = runtime.execute_with_recovery(
                spec, popen_factory=lambda *a, **k: launches.pop(0), which=CLAUDE,
                snapshotter=lambda root: {"head": "a" * 40, "branch": "main", "dirty": True,
                                          "status_sha256": "b" * 64})
        self.assertEqual(code, 0)
        self.assertEqual(launches, [], "the replacement never launched")
        replacement = "".join(second.stdin.written)
        self.assertIn("# Trusted replacement handoff", replacement)
        self.assertLessEqual(len(replacement.encode("utf-8")), runtime.MAX_REPLACEMENT_INPUT_BYTES)
        section = spec.stdin.split(runtime.HOST_DRAFT_HEADING, 1)[1].split("# Assigned task", 1)[0]
        self.assertIn(runtime.HOST_DRAFT_HEADING + section, replacement)
        self.assertIn(serialized(at_cap), replacement)
        self.assertEqual(self.read_status()["design_proposal"]["summary"], "The Architect's revision.")


class OutsideRootPath(ArchitectDraftCase):
    """REQ-001"""

    def test_a_task_naming_a_file_outside_the_root_is_refused_before_a_session(self):
        scratch = self.draft_file(proposal())
        before = self.ledger()
        for task in (f"Emit the proposal saved at {scratch}.",
                     f"Read `{scratch}` and emit it.",
                     f"Proposal: ({scratch})"):
            with self.subTest(task=task):
                with self.assertRaises(lib.HandsoffError) as refused:
                    self.spec(task)
                self.assertIn(f"architect task names {scratch} outside the project root", str(refused.exception))
                self.assertIn("pass a host draft with --draft FILE", str(refused.exception))
        self.assertEqual(self.ledger(), before)
        self.assertNotIn("agent_sessions", json.loads(before[0]))

    def test_a_quoted_path_with_spaces_is_read_whole_and_refused(self):
        spaced = self.draft_file(proposal(), name="draft with spaces.json")
        before = self.ledger()
        for quote in ("'", '"', "`"):
            task = f"Emit the proposal saved at {quote}{spaced}{quote}."
            with self.subTest(quote=quote):
                with self.assertRaises(lib.HandsoffError) as refused:
                    self.spec(task)
                self.assertIn(f"architect task names {spaced} outside the project root", str(refused.exception))
        self.assertEqual(self.ledger(), before)
        # the same file name inside the root is fine, quoted or not
        inside = self.tmp / "draft with spaces.json"
        inside.write_text("{}", encoding="utf-8")
        self.assertEqual(self.spec(f"See '{inside.resolve()}'.").role, "architect")

    def test_in_root_paths_missing_paths_and_directories_are_accepted(self):
        inside = (self.tmp / "handsoff.toml").resolve()
        for task in (f"See {inside} for the checks.",
                     f"Compare with {self.outside / 'never-written.json'}.",
                     f"The scratch directory is {self.outside}.",
                     "Edit bin/handsoff_agent.py and https://github.com/monzta1/project-handsoff/issues/445."):
            with self.subTest(task=task):
                self.assertEqual(self.spec(task).role, "architect")

    def test_other_roles_are_not_scanned(self):
        scratch = self.draft_file(proposal())
        self.assertEqual(runtime.architect_task_outside_path(self.tmp, f"at {scratch}"), str(scratch))
        spec = runtime.build_launch_spec(self.tmp, "implementer", f"Context is in {scratch}.", which=CLAUDE)
        self.assertEqual(spec.role, "implementer")


class BrokeredStderr(ArchitectDraftCase):
    """REQ-002"""

    def setUp(self):
        super().setUp()
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)

    def request(self):
        return broker.parse_architect_request(json.dumps({
            "actor": "architect", "project_root": str(self.tmp.resolve()), "action": "criteria",
            "by": "codex-architect",
            "operations": [{"op": "update", "id": "REQ-999", "fields": {"requirement": "no such criterion"}}]}))

    def execute(self, stderr="", stdout="", code=1):
        """execute_request with a child that writes these streams and exits `code`."""
        class Child:
            pid = None

            def wait(self, timeout=None):
                return code

        def factory(argv, **kwargs):
            kwargs["stderr"].write(stderr.encode("utf-8"))
            kwargs["stdout"].write(stdout.encode("utf-8"))
            return Child()
        request = {"actor": "supervisor", "project_root": str(self.tmp.resolve()),
                   "action": "workflow", "command": "status"}
        return broker.execute_request(self.tmp, request, capability=broker._SUPERVISOR_HOST_CAPABILITY,
                                      workflow_popen=factory)

    def test_a_failed_workflow_command_reports_its_stderr_and_still_echoes_it(self):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out), \
                self.assertRaises(broker.BrokeredWorkflowError) as raised:
            self.execute(stderr="Traceback: criteria file is not JSON\n", stdout="partial\n", code=1)
        error = raised.exception
        self.assertIsInstance(error, lib.HandsoffError)
        self.assertEqual(error.returncode, 1)
        self.assertEqual(str(error), "brokered workflow command exited with status 1: "
                                     "Traceback: criteria file is not JSON partial")
        self.assertEqual(error.stderr_tail, "Traceback: criteria file is not JSON")
        # the host still sees each stream, unchanged, on its own stream
        self.assertEqual(err.getvalue(), "Traceback: criteria file is not JSON\n")
        self.assertEqual(out.getvalue(), "partial\n")

    def test_the_real_criteria_apply_refusal_reaches_the_error(self):
        # the supervisor CLI prints its refusal on stdout, so stdout is reported too
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(broker.BrokeredWorkflowError) as raised:
            broker.execute_architect_criteria(self.tmp, self.request(),
                                              capability=broker._SUPERVISOR_HOST_CAPABILITY)
        error = raised.exception
        self.assertEqual(error.returncode, 1)
        self.assertIn("SHIP_FEATURE_BLOCKED", error.output_tail)
        self.assertIn("REQ-999", error.output_tail)
        self.assertEqual(error.stderr_tail, "")  # the refusal is on stdout only
        self.assertIn(error.output_tail, str(error))
        self.assertIn("REQ-999", out.getvalue())

    def test_a_quiet_failure_keeps_the_old_message_and_a_long_one_keeps_its_tail(self):
        with mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(broker.BrokeredWorkflowError) as quiet:
                self.execute(code=3)
            self.assertEqual(str(quiet.exception), "brokered workflow command exited with status 3")
            with self.assertRaises(broker.BrokeredWorkflowError) as noisy:
                self.execute(stderr="x" * 2000 + "\n  the real reason\n", code=2)
            self.assertTrue(noisy.exception.output_tail.endswith("the real reason"))
            self.assertEqual(len(noisy.exception.output_tail), broker.MAX_WORKFLOW_OUTPUT_TAIL)
            self.assertEqual(self.execute(stderr="warning only\n", code=0), 0)

    def test_long_stdout_never_displaces_stderr(self):
        limit = broker.MAX_WORKFLOW_OUTPUT_TAIL
        with mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(broker.BrokeredWorkflowError) as raised:
                self.execute(stderr="DIAGNOSTIC: bad operation\n", stdout="y" * 5000 + "\n", code=1)
        error = raised.exception
        self.assertTrue(error.output_tail.startswith("DIAGNOSTIC: bad operation "), error.output_tail[:80])
        self.assertEqual(len(error.output_tail), limit)  # stdout fills only the room left
        self.assertIn("DIAGNOSTIC: bad operation", str(error))
        # the Architect's 160-character tail keeps it too
        self.assertTrue(error.tail(160).startswith("DIAGNOSTIC: bad operation "))
        self.assertEqual(len(error.tail(160)), 160)
        # boundaries: stderr filling the limit leaves no stdout; no stderr gives stdout's tail
        full = broker.BrokeredWorkflowError(1, "e" * 2000, "o" * 50)
        self.assertEqual(full.output_tail, "e" * limit)
        self.assertEqual(full.tail(160), "e" * 160)
        edge = broker.BrokeredWorkflowError(1, "e" * 159, "o" * 50)
        self.assertEqual(edge.tail(160), "e" * 159)
        fits = broker.BrokeredWorkflowError(1, "e" * 158, "o" * 50)
        self.assertEqual(fits.tail(160), "e" * 158 + " o")
        quiet = broker.BrokeredWorkflowError(1, "", "o" * 50 + " SHIP_FEATURE_BLOCKED: why")
        self.assertTrue(quiet.tail(160).endswith("SHIP_FEATURE_BLOCKED: why"))

    def test_the_architect_refusal_reason_carries_the_stderr(self):
        line = (f"{runtime.SUPERVISOR_REQUEST_PREFIX} " + json.dumps({
            "actor": "architect", "project_root": str(self.tmp.resolve()), "action": "criteria",
            "by": "codex-architect",
            "operations": [{"op": "update", "id": "REQ-999", "fields": {"requirement": "no such criterion"}}]})
            + f"\n{runtime.DESIGN_RESULT_PREFIX} {json.dumps(proposal())}\n")
        spec = runtime.LaunchSpec("architect", "codex", "default", ("/bin/codex", "exec", "-"), str(self.tmp),
                                  "bounded prompt", token_budget=40_000, project_root=str(self.tmp.resolve()))
        with mock.patch.object(sys, "stderr", io.StringIO()) as echoed, \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                self.assertRaises(runtime.AgentLaunchError) as raised:
            runtime.execute_launch(spec, popen_factory=mock.Mock(return_value=_FakeProcess(line)),
                                   beacon_interval=0.01)
        status = self.read_status()
        failure = status["agent_failures"][raised.exception.session_id]
        self.assertEqual(failure["category"], "protocol_refused")
        self.assertTrue(failure["reason"].startswith("Architect criteria-apply exited 1: "), failure["reason"])
        self.assertIn("REQ-999", failure["reason"])
        self.assertLessEqual(len(failure["reason"]), 200)
        self.assertIn("HANDSOFF_AGENT_REFUSED: Architect criteria-apply exited 1: ", echoed.getvalue())
        self.assertEqual(lib.validate_status_schema(status), [])
        self.assertNotIn("design_proposal", status)


if __name__ == "__main__":
    unittest.main()
