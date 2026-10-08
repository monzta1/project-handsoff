"""P1.9: `doctor --probe`, a bounded real protocol exchange per adapter and
model, classified; never run unless asked."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import handsoff_cli as cli
import handsoff_lib as lib
import handsoff_supervisor as sup
from tests.engine_patch import patch_engine
# E1 review: the public `handsoff doctor` entrypoint, run with this module
from tests.test_doctor_entrypoint import PublicDoctorTests  # noqa: F401

NONCE = 424242
GOOD = f'{lib.PROBE_PREFIX} {{"ok": true, "nonce": {NONCE}}}\n'


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class StubRunner:
    """A stub adapter: one scripted outcome per model it is asked about."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        model = argv[argv.index("--model") + 1] if "--model" in argv else "default"
        outcome = self.outcomes[model] if isinstance(self.outcomes, dict) else self.outcomes
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def cfg_for(models=None, adapters=None):
    cfg = {"adapters": adapters if adapters is not None else {"codex": "/bin/echo", "claude": "/bin/echo"},
           "agents": {}, "models": {}}
    for index, model in enumerate(models or []):
        role = lib.SELECTABLE_AGENT_ROLES[index]
        cfg["agents"][role] = "codex"
        cfg["models"][role] = model
    return cfg


class ProbeClassificationTests(unittest.TestCase):
    def probe(self, outcome, *, adapters=None, adapter="codex"):
        runner = StubRunner(outcome)
        result = lib.probe_adapters(cfg_for(adapters=adapters), adapter, which=lambda _name: None,
                                    runner=runner, timeout=5, nonce_source=lambda: NONCE)
        models = result["probes"][adapter]
        return next(iter(models.values()))["class"], result, runner

    def test_missing_binary(self):
        kind, _result, runner = self.probe(completed(), adapters={})
        self.assertEqual(kind, "executable_missing")
        self.assertEqual(runner.calls, [])
        kind, _result, _runner = self.probe(FileNotFoundError("gone"))
        self.assertEqual(kind, "executable_missing")

    def test_credential_error(self):
        self.assertEqual(self.probe(completed(1, "", "Error: Not logged in. Please run codex login"))[0],
                         "credentials_missing")

    def test_sandbox_refusal(self):
        self.assertEqual(self.probe(completed(1, "", "sandbox denied: operation blocked"))[0], "sandbox_refused")

    def test_network_error(self):
        self.assertEqual(self.probe(completed(1, "", "error sending request: Could not resolve host api"))[0],
                         "provider_unreachable")

    def test_timeout(self):
        self.assertEqual(self.probe(subprocess.TimeoutExpired(["codex"], 5))[0], "timeout")

    def test_truncated_budget_with_and_without_a_valid_line(self):
        budget = "Error: shared rollout token budget exhausted"
        self.assertEqual(self.probe(completed(1, GOOD, budget))[0], "available")
        self.assertEqual(self.probe(completed(1, "partial thou", budget))[0], "budget_too_small")

    def test_wrong_nonce_and_non_protocol_reply(self):
        wrong = f'{lib.PROBE_PREFIX} {{"ok": true, "nonce": {NONCE + 1}}}\n'
        self.assertEqual(self.probe(completed(0, wrong))[0], "protocol_invalid")
        self.assertEqual(self.probe(completed(0, "Hello! How can I help?\n"))[0], "protocol_invalid")
        not_json = f"{lib.PROBE_PREFIX} ok nonce {NONCE}\n"
        self.assertEqual(self.probe(completed(0, not_json))[0], "protocol_invalid")

    def test_unrecognised_failure(self):
        self.assertEqual(self.probe(completed(3, "", "something odd happened"))[0], "unknown_error")

    def test_good_line(self):
        kind, result, runner = self.probe(completed(0, GOOD))
        self.assertEqual(kind, "available")
        self.assertEqual(result["probe_summary"], "available")
        call = runner.calls[0]
        self.assertIn(str(NONCE), call["input"])
        self.assertIn(lib.PROBE_PREFIX, call["input"])
        self.assertEqual(call["timeout"], 5)
        # bounded token budget on the wire, from a clean scratch directory
        self.assertIn(f"limit_tokens={lib.PROBE_TOKEN_BUDGET}", " ".join(call["argv"]))
        self.assertTrue(Path(call["cwd"]).name.startswith("handsoff-probe-"))
        self.assertFalse(Path(call["cwd"]).exists())

    def test_claude_stream_json_good_line(self):
        event = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": GOOD.strip()}]}})
        kind, _result, runner = self.probe(completed(0, event + "\n"), adapter="claude")
        self.assertEqual(kind, "available")
        self.assertIn("stream-json", runner.calls[0]["argv"])

    def test_each_adapter_runs_under_the_ceiling_it_reports(self):
        """E1 review: Codex holds the probe budget natively from its argv;
        Claude takes no limit on the argv, so the runner holds it."""
        kind, result, runner = self.probe(completed(0, GOOD), adapter="codex")
        call = runner.calls[0]
        self.assertIn(f"limit_tokens={lib.PROBE_TOKEN_BUDGET}", " ".join(call["argv"]))
        self.assertIsNone(call["token_ceiling"])
        self.assertEqual(result["probes"]["codex"]["default"]["enforcement"], "native_rollout_meter")
        self.assertEqual(result["probes"]["codex"]["default"]["tokens"], lib.PROBE_TOKEN_BUDGET)
        event = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": GOOD.strip()}]}})
        kind, result, runner = self.probe(completed(0, event + "\n"), adapter="claude")
        call = runner.calls[0]
        self.assertEqual(call["argv"][0], "/bin/echo")
        self.assertEqual(call["argv"][1:2], ["-p"])
        self.assertIn("stream-json", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--max-turns") + 1], "1")
        self.assertEqual(call["token_ceiling"], lib.PROBE_TOKEN_BUDGET)
        self.assertEqual(call["adapter"], "claude")
        self.assertEqual(result["probes"]["claude"]["default"]["enforcement"], "wrapper_enforced")
        self.assertEqual(result["probes"]["claude"]["default"]["tokens"], lib.PROBE_TOKEN_BUDGET)
        self.assertIs(lib.probe_adapters.__kwdefaults__["runner"], lib.run_probe)

    def test_precedence_credentials_over_a_valid_line(self):
        self.assertEqual(self.probe(completed(1, GOOD, "401 Unauthorized"))[0], "credentials_missing")

    def test_every_class_is_declared(self):
        self.assertEqual(lib.PROBE_CLASSES, (
            "executable_missing", "credentials_missing", "sandbox_refused", "provider_unreachable",
            "timeout", "available", "budget_too_small", "protocol_invalid", "unknown_error"))


#: a stand-in Claude that streams growing usage and never stops on its own
ENDLESS_CLAUDE = (
    "import json, sys, time\n"
    "sys.stdin.read()\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'thinking'}],"
    " 'usage': {'input_tokens': 1000 * n, 'output_tokens': 10}}}), flush=True)\n"
    "    time.sleep(0.02)\n"
)


class ProbeTokenCeilingTests(unittest.TestCase):
    def test_a_claude_probe_is_stopped_past_its_token_ceiling(self):
        scratch = tempfile.mkdtemp(prefix="handsoff-probe-test-")
        self.addCleanup(shutil.rmtree, scratch, True)
        done = lib.run_probe([sys.executable, "-c", ENDLESS_CLAUDE], input="probe", timeout=30, cwd=scratch,
                             token_ceiling=5000, adapter="claude")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("past the 5000-token ceiling", done.stderr)
        self.assertLessEqual(done.stdout.count("\n"), 8, "stopped at the first report past the ceiling")
        self.assertEqual(lib.classify_probe(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr,
                                            nonce=NONCE), "budget_too_small")

    def test_a_claude_probe_within_its_ceiling_is_untouched(self):
        script = f"import sys; sys.stdin.read(); print({GOOD.strip()!r})"
        done = lib.run_probe([sys.executable, "-c", script], input="probe", timeout=30, cwd=tempfile.gettempdir(),
                             token_ceiling=5000, adapter="claude")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertTrue(lib.probe_line_valid(done.stdout, NONCE))


class ProbePerModelTests(unittest.TestCase):
    def test_two_models_one_failing_give_per_model_results_and_partial(self):
        runner = StubRunner({"gpt-good": completed(0, GOOD), "gpt-bad": completed(3, "", "boom")})
        cfg = cfg_for(models=["gpt-good", "gpt-bad"])
        result = lib.probe_adapters(cfg, "codex", which=lambda _name: None, runner=runner,
                                    timeout=5, nonce_source=lambda: NONCE)
        self.assertEqual(result["probes"]["codex"]["gpt-good"]["class"], "available")
        self.assertEqual(result["probes"]["codex"]["gpt-bad"]["class"], "unknown_error")
        self.assertEqual(result["probe_summary"], "partial")
        self.assertEqual(len(runner.calls), 2)

    def test_summary_unavailable_when_none_is(self):
        self.assertEqual(lib.probe_summary({"codex": {"m": {"class": "timeout"}},
                                            "claude": {"m": {"class": "executable_missing"}}}), "unavailable")
        self.assertEqual(lib.probe_summary({"codex": {"m": {"class": "available"}},
                                            "claude": {"m": {"class": "available"}}}), "available")

    def test_all_probes_every_adapter(self):
        runner = StubRunner(completed(0, GOOD))
        result = lib.probe_adapters(cfg_for(), "all", which=lambda _name: None, runner=runner,
                                    timeout=5, nonce_source=lambda: NONCE)
        self.assertEqual(set(result["probes"]), set(lib.SELECTABLE_AGENT_ADAPTERS))

    def test_unknown_adapter_refused(self):
        with self.assertRaisesRegex(lib.HandsoffError, "--probe takes"):
            lib.probe_adapters(cfg_for(), "gemini")


class ProbeOnlyWhenAskedTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("HANDSOFF_SKIP_PREFLIGHT")
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-probe-")).resolve()
        cli.init_project(self.root, None)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        if self._env is None:
            os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None)
        else:
            os.environ["HANDSOFF_SKIP_PREFLIGHT"] = self._env

    def test_default_doctor_runs_no_probe(self):
        # the engine's own manifest integrity is test_stale_manifest's subject;
        # here only whether a default doctor ever reaches a probe
        identity = {**lib.runtime_identity(self.root), "files": 0, "state": "verified"}
        with patch_engine("probe_adapters") as probe, \
                patch_engine("validate_runtime_integrity", return_value=identity):
            cli.doctor(self.root)
            args = sup.build_parser().parse_args(["--root", str(self.root), "doctor", "--prompts"])
            with contextlib.redirect_stdout(io.StringIO()):
                sup.cmd_doctor(args)
        probe.assert_not_called()

    def test_doctor_probe_reports_and_records_nothing_in_the_ledger(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        stub = {"probes": {"codex": {"default": {"class": "available", "detail": "", "tokens": 1}}},
                "probe_summary": "available"}
        with patch_engine("probe_adapters", return_value=stub) as probe:
            args = sup.build_parser().parse_args(["--root", str(self.root), "doctor", "--probe", "codex"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sup.cmd_doctor(args)
        self.assertEqual(code, 0)
        self.assertEqual(probe.call_args.args[1], "codex")
        self.assertEqual(json.loads(out.getvalue())["probe_summary"], "available")
        after = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        self.assertEqual(before, after)

    def test_a_real_probe_writes_nothing_to_the_project(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        runner = StubRunner(completed(0, GOOD))
        with mock.patch.dict(os.environ, {}):
            lib.probe_adapters(lib.load_config(self.root), "codex", which=lambda _name: "/bin/echo",
                               runner=runner, timeout=5, nonce_source=lambda: NONCE)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        self.assertNotEqual(Path(runner.calls[0]["cwd"]), self.root)


if __name__ == "__main__":
    unittest.main()
