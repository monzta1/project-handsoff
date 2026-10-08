"""#305: the ollama adapter, a local model behind the #304 contract.

REQ-004: ollama is registered through the contract (locality local, cost
policy local_compute, usage source and ceiling enforcement declared) and is
accepted by configuration, adapter discovery, session creation and protocol
adoption for architect, supervisor and reviewer, never implementer. Its loop
runs over Ollama's HTTP API with native tool calling and three read-only
tools confined to the project root. A fake Ollama server (a threaded
http.server on a free loopback port) drives the real managed launch path,
build_launch_spec and execute_launch running the real runner as a child
process, to a recorded protocol result, metered and unmetered.
REQ-005: pre-flight names service_unavailable, model_not_pulled,
model_not_loadable and ready; a local session draws no paid cloud capacity
but counts as a call, holds its role's one live session and is bounded by
its token ceiling.
REQ-006: the role, capability and risk floors hold on every launch path
(explicit profile, adaptive selection, reserved fallback); a local failure
falls back to a configured cloud provider through the bounded replacement
path; a different reported model is refused as model_identity_mismatch.
"""
from __future__ import annotations

import io
import json
import shutil
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_adapters as adapters  # noqa: E402
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_ollama as ollama  # noqa: E402
import handsoff_routing as routing  # noqa: E402

MODEL = "qwen3:8b"
ROLES = ("architect", "supervisor", "reviewer")
#: The launch builders resolve the executable they record.
PYTHON = str(Path(sys.executable).resolve())


def _verdict(**overrides):
    packet = {"kind": "implementation", "decision": "approved", "summary": "local review",
              "findings": [], "structural_blocker": False, "symptom_reproduced": "yes",
              "tests_executed": "yes"}
    packet.update(overrides)
    return "HANDSOFF_REVIEW_RESULT: " + json.dumps(packet)


def _chat(content="", *, tool_calls=None, model=MODEL, usage=(1200, 80)):
    response = {"model": model, "done": True,
                "message": {"role": "assistant", "content": content}}
    if tool_calls:
        response["message"]["tool_calls"] = tool_calls
    if usage is not None:
        response["prompt_eval_count"], response["eval_count"] = usage
    return response


def _tool(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class FakeOllama:
    """A threaded HTTP server that answers like Ollama, from a script.

    `tags` lists pulled model names; `show` is the capability list; `load`
    is the HTTP status of the loading /api/generate; `chat` is a list of
    responses served in order, each a dict or an int HTTP status."""

    def __init__(self, *, tags=(MODEL,), show=("completion", "tools"), load=200, chat=()):
        self.tags, self.show, self.load = list(tags), list(show), load
        self.chat = list(chat)
        self.requests: list[tuple[str, dict | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _send(self, status, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.requests.append((self.path, None))
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": name, "model": name} for name in owner.tags]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append((self.path, payload))
                if self.path == "/api/show":
                    self._send(200, {"capabilities": owner.show})
                elif self.path == "/api/generate":
                    if owner.load == 200:
                        self._send(200, {"model": payload.get("model"), "done": True, "done_reason": "load"})
                    else:
                        self._send(owner.load, {"error": "model requires more system memory than is available"})
                elif self.path == "/api/chat":
                    reply = owner.chat.pop(0) if owner.chat else 500
                    if isinstance(reply, int):
                        self._send(reply, {"error": "server error"})
                    else:
                        self._send(200, reply)
                else:
                    self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def chat_requests(self):
        return [payload for path, payload in self.requests if path == "/api/chat"]


def _closed_host():
    """A loopback origin nothing listens on."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    host = f"http://127.0.0.1:{server.server_address[1]}"
    server.server_close()
    return host


def _set_key(toml: Path, section: str, key: str, literal: str) -> None:
    """Set one key inside one [section] of a fixture handsoff.toml."""
    lines = toml.read_text().splitlines()
    start = lines.index(f"[{section}]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    for index in range(start + 1, end):
        if lines[index].split("=", 1)[0].strip() == key:
            lines[index] = f"{key} = {literal}"
            break
    else:
        lines.insert(start + 1, f"{key} = {literal}")
    toml.write_text("\n".join(lines) + "\n")


class _Project(HandsoffTestCase):
    """A fixture project with the prompts and a fake Ollama server."""

    def setUp(self):
        super().setUp()
        # the docstring promises the prompts; launch reads prompts/<role>.md
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts", dirs_exist_ok=True)
        write_version_pin(self.tmp)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        super().tearDown()

    def serve(self, **kwargs) -> FakeOllama:
        server = FakeOllama(**kwargs)
        self.servers.append(server)
        return server

    def declare(self, host, *, model=MODEL, capability="FAST", risks=("routine",)):
        """Declare ollama, and let the mission model policy name it: the
        default policy names only codex and claude (#304)."""
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + (
            f'\n[ollama]\nhost = "{host}"\nmodel = "{model}"\ncapability_tier = "{capability}"\n'
            f"allowed_risk_classes = {json.dumps(list(risks))}\n"
            '\n[model_policy]\nallowed_adapters = ["codex", "claude", "ollama"]\n'))

    def assign(self, role, adapter, model=None):
        toml = self.tmp / "handsoff.toml"
        _set_key(toml, "agents", role, json.dumps(adapter))
        if model is not None:
            _set_key(toml, "models", role, json.dumps(model))

    def phase5(self):
        self.init("Ollama fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def at_phase(self, phase, risk_class="routine", model_policy=None):
        result = self.init_with_risk(risk_class)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        status.update(phase_number=phase, phase=lib.PHASES[phase], progress=40)
        if model_policy is not None:
            status["model_policy"] = model_policy
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="fixture phase")

    def init_with_risk(self, risk_class):
        from tests.test_handsoff_supervisor import run
        return run(["init", "Ollama fixture", "--risk-class", risk_class], cwd=self.tmp)

    def set_risk(self, risk_class):
        status = self.read_status()
        status["risk_class"] = risk_class
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="fixture risk")

    def spec(self, role, *, preflight=False):
        """build_launch_spec as the CLI calls it; only claude is on PATH."""
        saved = os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None) if preflight else None
        try:
            with mock.patch.object(agent.lib, "validate_runtime_integrity"):
                return agent.build_launch_spec(
                    self.tmp, role, "Review the change.",
                    which=lambda name: f"/opt/test/{name}" if name == "claude" else None)
        finally:
            if saved is not None:
                os.environ["HANDSOFF_SKIP_PREFLIGHT"] = saved

    def launch(self, spec):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            try:
                return agent.execute_launch(spec, beacon_interval=0.01), None, out.getvalue()
            except agent.AgentLaunchError as exc:
                return None, exc, out.getvalue()

    def session(self, role):
        status = self.read_status()
        sid = lib.role_session_ids(status)[role]  # #420: an ended session leaves the pointer
        return status, status["agent_sessions"][sid], (status.get("agent_failures") or {}).get(sid)


class Registration(unittest.TestCase):
    """REQ-004: ollama is a contract adapter, not codex or claude."""

    def test_the_contract_record_declares_every_fact(self):
        record = adapters.get_adapter("ollama")
        self.assertEqual((record.locality, record.cost_policy, record.usage_source,
                          record.ceiling_enforcement, record.ceiling_mechanism),
                         ("local", "local_compute", "stream", "enforced", "wrapper_enforced"))
        self.assertIs(record.build_argv, ollama.build_argv)
        self.assertIs(record.preflight, ollama.preflight)
        self.assertIn("ollama", adapters.adapter_names())
        self.assertIn("ollama", lib.adaptive_provider_names())

    def test_codex_and_claude_tables_are_unchanged(self):
        self.assertEqual(lib.SELECTABLE_AGENT_ADAPTERS, ("codex", "claude"))
        self.assertEqual(lib.CEILING_ENFORCEMENT, {"codex": "native_rollout_meter", "claude": "wrapper_enforced"})
        self.assertEqual(lib.ADAPTER_INTERMEDIATE_USAGE, {"codex": False, "claude": True})
        self.assertEqual(lib.validate_model_policy({})["allowed_adapters"], ["codex", "claude"])

    def test_roles_are_architect_supervisor_reviewer_never_implementer(self):
        for role in ROLES:
            self.assertTrue(lib.adapter_serves_role("ollama", role))
            self.assertEqual(lib.default_agent_actor("ollama", role), f"ollama-{role}")
        self.assertFalse(lib.adapter_serves_role("ollama", "implementer"))
        with self.assertRaises(lib.HandsoffError):
            lib.default_agent_actor("ollama", "implementer")
        with self.assertRaises(lib.HandsoffError):
            adapters.build_argv("ollama", sys.executable, "implementer", MODEL,
                                provider_limit=1000, project_root=tempfile.mkdtemp())

    def test_the_argv_runs_the_loop_with_the_declared_host(self):
        root = Path(tempfile.mkdtemp())
        (root / "handsoff.toml").write_text('[ollama]\nhost = "http://127.0.0.1:4000"\n')
        argv = adapters.build_argv("ollama", sys.executable, "reviewer", MODEL,
                                   provider_limit=5000, project_root=root)
        self.assertEqual(argv[:3], [sys.executable, str(BIN.resolve() / "handsoff_ollama.py"), "run"])
        self.assertEqual(argv[argv.index("--host") + 1], "http://127.0.0.1:4000")
        self.assertEqual(argv[argv.index("--provider-limit") + 1], "5000")
        self.assertEqual(argv[argv.index("--project-root") + 1], str(root.resolve()))
        self.assertFalse({Path(item).name for item in argv} & {"codex", "claude"})


class Configuration(_Project):
    """REQ-004: configuration and discovery accept ollama for its roles."""

    def test_a_role_may_name_ollama_and_discovery_finds_it(self):
        self.assertEqual(lib.contract_adapter_availability(lib.load_config(self.tmp)), {})
        self.assign("reviewer", "ollama", MODEL)
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["agents"]["reviewer"], "ollama")
        self.assertTrue(cfg["ollama"]["declared"])
        self.assertEqual(lib.contract_adapter_availability(cfg), {"ollama": True})

    def test_implementer_cannot_name_ollama_in_agents_or_fallbacks(self):
        toml = self.tmp / "handsoff.toml"
        original = toml.read_text()
        self.assign("implementer", "ollama", MODEL)
        with self.assertRaisesRegex(lib.HandsoffError, "agents.implementer cannot be ollama"):
            lib.load_config(self.tmp)
        toml.write_text(original)
        _set_key(toml, "fallback_policy", "implementer", '[{adapter = "ollama", model = "qwen3:8b"}]')
        with self.assertRaisesRegex(lib.HandsoffError, "fallback_policy.implementer"):
            lib.load_config(self.tmp)

    def test_the_table_declares_local_routing_profiles_up_to_its_capability(self):
        self.declare("http://127.0.0.1:4000", capability="STANDARD")
        cfg = lib.load_config(self.tmp)
        self.assertEqual(sorted(cfg["local_routing_profiles"]), ["FAST", "STANDARD"])
        profile = cfg["local_routing_profiles"]["STANDARD"]
        self.assertEqual((profile["adapter"], profile["model"], profile["pricing"], profile["source"]),
                         ("ollama", MODEL, {}, "local:ollama"))

    def test_an_invalid_table_is_refused(self):
        toml = self.tmp / "handsoff.toml"
        original = toml.read_text()
        for bad in ('host = "ftp://x"', 'capability_tier = "ULTRA"', 'allowed_risk_classes = ["chaos"]',
                    'nonsense = 1'):
            toml.write_text(original + f"\n[ollama]\n{bad}\n")
            with self.subTest(bad=bad), self.assertRaises(lib.HandsoffError):
                lib.load_config(self.tmp)


class ReadOnlyTools(unittest.TestCase):
    """REQ-004: read a file, list files, search text, inside the root only."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hello')\nTOKEN = 1\n")
        outside = Path(tempfile.mkdtemp()).resolve()
        (outside / "secret.txt").write_text("TOKEN = outside\n")
        (self.root / "escape").symlink_to(outside / "secret.txt")
        self.outside = outside

    def test_read_list_and_search_inside_the_root(self):
        self.assertIn("1: print('hello')", ollama.run_tool(self.root, "read_file", {"path": "src/app.py"}))
        listing = ollama.run_tool(self.root, "list_files", {"recursive": True})
        self.assertIn("src/app.py", listing)
        self.assertNotIn("escape", listing)
        found = ollama.run_tool(self.root, "search_text", {"text": "TOKEN"})
        self.assertIn("src/app.py:2: TOKEN = 1", found)
        self.assertNotIn("outside", found)

    def test_every_escape_is_refused(self):
        for path in ("../" + self.outside.name + "/secret.txt", str(self.outside / "secret.txt"), "escape"):
            with self.subTest(path=path):
                result = ollama.run_tool(self.root, "read_file", {"path": path})
                self.assertTrue(result.startswith("error:"), result)
                self.assertNotIn("TOKEN = outside", result)
        self.assertTrue(ollama.run_tool(self.root, "list_files", {"path": ".."}).startswith("error:"))

    def test_no_tool_writes_or_runs_anything(self):
        self.assertEqual(sorted(tool["function"]["name"] for tool in ollama.TOOLS),
                         ["list_files", "read_file", "search_text"])
        self.assertTrue(ollama.run_tool(self.root, "write_file", {"path": "x"}).startswith("error:"))
        self.assertFalse((self.root / "x").exists())


class Preflight(_Project):
    """REQ-005: four states, against a fake server."""

    def test_the_four_states(self):
        cases = {
            "service_unavailable": (None, {}),
            "model_not_pulled": (True, {"tags": ("llama3:8b",)}),
            "model_not_loadable": (True, {"load": 500}),
            "ready": (True, {}),
        }
        for expected, (live, kwargs) in cases.items():
            host = self.serve(**kwargs).host if live else _closed_host()
            with self.subTest(state=expected):
                self.assertEqual(ollama.probe(host, MODEL)["state"], expected)
                result = adapters.preflight("ollama", self.tmp, model=MODEL, executable=sys.executable,
                                            argv=[], cwd=str(self.tmp), host=host)
                self.assertEqual(result["preflight"]["ollama_state"], expected)
                self.assertEqual(result["state"], {"service_unavailable": "runtime_not_ready",
                                                   "ready": "ready"}.get(expected, "model_unavailable"))
        self.assertEqual(tuple(cases), ollama.PREFLIGHT_STATES)

    def test_a_model_without_native_tool_calling_is_not_loadable(self):
        server = self.serve(show=("completion",))
        result = ollama.probe(server.host, MODEL)
        self.assertEqual(result["state"], "model_not_loadable")
        self.assertIn("tool calling", result["reason"])

    def test_the_launch_is_refused_before_a_session_exists(self):
        server = self.serve(tags=("llama3:8b",))
        self.declare(server.host)
        self.assign("reviewer", "ollama", MODEL)
        self.phase5()
        with self.assertRaisesRegex(lib.HandsoffError, r"preflight blocked \(model_not_pulled\)"):
            self.spec("reviewer", preflight=True)
        self.assertEqual(self.read_status().get("agent_sessions") or {}, {})


class ManagedLaunch(_Project):
    """REQ-004: the real managed launch path to a recorded protocol result."""

    def _reviewer(self, server):
        self.declare(server.host)
        self.assign("reviewer", "ollama", MODEL)
        self.phase5()
        spec = self.spec("reviewer")
        self.assertEqual((spec.adapter, spec.model, spec.argv[0]), ("ollama", MODEL, PYTHON))
        self.assertEqual(spec.ceiling_enforcement, "wrapper_enforced")
        return spec

    def test_a_local_reviewer_reads_the_project_and_its_verdict_is_recorded(self):
        server = self.serve(chat=[_chat(tool_calls=[_tool("read_file", path="handsoff.toml")]),
                                  _chat(_verdict(), usage=(2300, 120))])
        spec = self._reviewer(server)
        code, error, out = self.launch(spec)
        self.assertIsNone(error, f"{error}\n{out}")
        self.assertEqual(code, 0)
        status, session, failure = self.session("reviewer")
        self.assertIsNone(failure)
        self.assertEqual((session["adapter"], session["actor"], session["state"]),
                         ("ollama", "ollama-reviewer", "completed"))
        self.assertEqual(session["reported_model"], MODEL)
        self.assertIsNotNone(status["review"], "the verdict was dispatched through record-review")
        self.assertEqual(status["review"]["by"], "ollama-reviewer")
        self.assertEqual(session["usage"], {"tokens_in": 3500, "tokens_out": 200, "tokens_total": 3700,
                                            "source": "adapter"})
        self.assertEqual(session["routing_contract"]["locality"], "local")
        self.assertEqual(session["routing_contract"]["cost_policy"], {"policy": "local_compute", "pricing": {}})
        self.assertEqual(session["routing_contract"]["ceiling"], spec.token_budget)
        self.assertEqual(session["reviewer_isolation"]["enforcement"], "tool_confined")
        self.assertEqual(session["reviewer_isolation"]["network_policy"], "loopback")
        # the tool loop ran natively, inside the project root
        first, second = server.chat_requests()
        self.assertEqual(sorted(t["function"]["name"] for t in first["tools"]),
                         ["list_files", "read_file", "search_text"])
        tool_message = second["messages"][-1]
        self.assertEqual((tool_message["role"], tool_message["tool_name"]), ("tool", "read_file"))
        self.assertIn("[ollama]", tool_message["content"])
        self.assertEqual(lib.validate_status_schema(status), [])

    def test_unavailable_usage_is_recorded_as_unreported_and_the_result_still_lands(self):
        server = self.serve(chat=[_chat(_verdict(), usage=None)])
        code, error, out = self.launch(self._reviewer(server))
        self.assertIsNone(error, f"{error}\n{out}")
        status, session, _ = self.session("reviewer")
        self.assertEqual(session["usage"], {"tokens_in": None, "tokens_out": None, "tokens_total": None,
                                            "source": "not reported"})
        self.assertIsNotNone(status["review"])
        self.assertIn('"usage_available": false', out)

    def test_a_reported_model_mismatch_is_refused(self):
        server = self.serve(chat=[_chat(_verdict(), model="llama3:8b")])
        code, error, out = self.launch(self._reviewer(server))
        self.assertIsInstance(error, agent.AgentLaunchError)
        self.assertIn("model identity mismatch", str(error))
        status, session, failure = self.session("reviewer")
        self.assertEqual(failure["category"], "model_identity_mismatch")
        self.assertEqual(session["reported_model"], "llama3:8b")
        self.assertIsNone(status["review"], "no verdict from a substituted model is adopted")

    def test_the_latest_alias_is_the_same_model(self):
        self.assertTrue(ollama.same_model("qwen3", "qwen3:latest"))
        self.assertFalse(ollama.same_model("qwen3:8b", "qwen3:latest"))

    def test_protocol_adoption_accepts_the_ollama_session(self):
        server = self.serve(chat=[_chat("thinking out loud", usage=(900, 40)), 500])
        spec = self._reviewer(server)
        # exit 0 without a protocol line: adoption refuses for lack of an
        # artifact, not because the adapter is unknown
        code, error, out = self.launch(spec)
        status, session, failure = self.session("reviewer")
        self.assertEqual(failure["category"], "no_artifact")
        self.assertEqual(lib.validate_status_schema(status), [])


class SessionCreation(_Project):
    """REQ-004: session creation and the schema accept ollama per role."""

    def test_session_creation_accepts_supported_roles_and_refuses_implementer(self):
        self.declare("http://127.0.0.1:4000")
        self.at_phase(4)
        session = lib.create_agent_session(self.tmp, role="supervisor", actor="ollama-supervisor",
                                           adapter="ollama", requested_model=MODEL,
                                           resolution_source="configured")
        self.assertEqual(session["adapter"], "ollama")
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])
        with self.assertRaisesRegex(lib.HandsoffError, "does not serve the implementer role"):
            lib.create_agent_session(self.tmp, role="implementer", actor="ollama-implementer",
                                     adapter="ollama", requested_model=MODEL,
                                     resolution_source="configured")
        status = self.read_status()
        forged = dict(status["agent_sessions"][session["session_id"]], role="implementer")
        status["agent_sessions"][session["session_id"]] = forged
        self.assertTrue(any("invalid adapter" in error for error in lib.validate_status_schema(status)))


class Budgets(_Project):
    """REQ-005: no cloud capacity drawn; calls, concurrency and tokens counted."""

    def _local_routed_session(self):
        self.declare("http://127.0.0.1:4000", capability="PREMIUM", risks=("irreversible",))
        self.at_phase(4, "irreversible", {"allowed_adapters": ["ollama"], "denied_models": [],
                                          "quota_substitution": True})
        spec = self.spec("architect")
        self.assertEqual((spec.adapter, spec.adaptive_routing["tier"]), ("ollama", "PREMIUM"))
        lib.create_agent_session(self.tmp, role="architect", actor="ollama-architect", adapter="ollama",
                                 requested_model=MODEL, resolution_source="adaptive",
                                 adaptive_routing=spec.adaptive_routing, routing_contract=spec.routing_contract)
        return spec

    def test_a_local_premium_session_draws_no_premium_capacity_but_is_a_call(self):
        self._local_routed_session()
        status = self.read_status()
        usage = lib.adaptive_usage(status)
        self.assertEqual((usage["premium_calls"], usage["concurrent_premium_agents"], usage["total_calls"]),
                         (0, 0, 1))
        # the same record on a remote adapter does draw it
        sid = lib.role_session_ids(status)["architect"]  # #420
        status["agent_sessions"][sid]["routing_contract"]["locality"] = "remote"
        remote = lib.adaptive_usage(status)
        self.assertEqual((remote["premium_calls"], remote["concurrent_premium_agents"]), (1, 1))

    def test_the_call_budget_counts_a_local_call_and_the_premium_budget_does_not(self):
        self._local_routed_session()
        status = self.read_status()
        cfg = {**lib.load_config(self.tmp), "model_policy": status["model_policy"]}
        common = dict(risk_class="irreversible", available_adapters=["ollama"],
                      mission_usage=lib.adaptive_usage(status), deterministic_checks_complete=True)
        premium_cap = {**cfg, "adaptive_routing_budgets": {"per_mission": {"premium_calls": 1}, "fleet": {}}}
        self.assertEqual(lib.route_adaptive_profile(premium_cap, **common)["state"], "selected")
        call_cap = {**cfg, "adaptive_routing_budgets": {"per_mission": {"total_calls": 1}, "fleet": {}}}
        self.assertEqual(lib.route_adaptive_profile(call_cap, **common)["reason"],
                         "per_mission_total_calls_exhausted")

    def test_a_local_session_holds_its_role_like_any_other(self):
        self._local_routed_session()
        with self.assertRaisesRegex(lib.HandsoffError, "already has live agent session"):
            lib.create_agent_session(self.tmp, role="architect", actor="claude-architect", adapter="claude",
                                     requested_model="claude-opus-5", resolution_source="configured")

    def test_the_token_ceiling_bounds_a_local_session(self):
        server = self.serve(chat=[_chat(_verdict(), usage=(990_000, 20_000))])
        self.declare(server.host)
        self.assign("reviewer", "ollama", MODEL)
        self.phase5()
        spec = self.spec("reviewer")
        self.assertEqual(spec.argv[spec.argv.index("--provider-limit") + 1], str(spec.provider_limit))
        code, error, out = self.launch(spec)
        self.assertIsInstance(error, agent.AgentLaunchError)
        status, session, failure = self.session("reviewer")
        self.assertEqual(failure["category"], "token_budget_exhaustion")
        self.assertEqual(session["usage"]["tokens_total"], 1_010_000)
        self.assertIsNone(status["review"])


class MixedMetering(_Project):
    """Implementation review attempt 1: one turn without usage switched the
    ceiling off, so a later 1,020,000-token turn ran under a small limit."""

    def test_an_unmetered_turn_does_not_switch_the_ceiling_off(self):
        server = self.serve(chat=[
            _chat(tool_calls=[_tool("read_file", path="handsoff.toml")], usage=(100, 20)),
            _chat(tool_calls=[_tool("read_file", path="handsoff.toml")], usage=None),
            _chat(tool_calls=[_tool("read_file", path="handsoff.toml")], usage=(1_000_000, 20_000)),
            _chat(_verdict(), usage=(10, 10)),
        ])
        self.declare(server.host)
        self.assign("reviewer", "ollama", MODEL)
        self.phase5()
        code, error, out = self.launch(self.spec("reviewer"))
        self.assertIsInstance(error, agent.AgentLaunchError)
        status, session, failure = self.session("reviewer")
        self.assertEqual(failure["category"], "token_budget_exhaustion")
        self.assertIsNone(status["review"], "the session stopped at its ceiling, before any verdict")
        # usage stays honest: after an unmetered turn no complete total is claimed
        self.assertNotEqual((session.get("usage") or {}).get("tokens_total"), 120)


class Floors(_Project):
    """REQ-006: role, capability and risk floors on every launch path."""

    def test_an_explicit_high_risk_launch_is_refused_before_a_session_exists(self):
        self.declare("http://127.0.0.1:4000")
        self.assign("reviewer", "ollama", MODEL)
        self.phase5()
        self.set_risk("irreversible")
        with self.assertRaisesRegex(lib.HandsoffError, "refused before session creation: irreversible work"):
            self.spec("reviewer")
        self.assertEqual(self.read_status().get("agent_sessions") or {}, {})

    def test_the_capability_floor_holds_even_where_policy_allows_the_risk(self):
        self.declare("http://127.0.0.1:4000", risks=("routine", "elevated"))
        self.assign("architect", "ollama", MODEL)
        self.at_phase(4, "elevated")
        with self.assertRaisesRegex(lib.HandsoffError, r"needs STANDARD capability.*capability_tier"):
            self.spec("architect")
        self.assertEqual(self.read_status().get("agent_sessions") or {}, {})

    def test_an_explicit_routine_launch_passes(self):
        self.declare("http://127.0.0.1:4000")
        self.assign("architect", "ollama", MODEL)
        self.at_phase(4)
        self.assertEqual(self.spec("architect").adapter, "ollama")

    def test_adaptive_selection_respects_the_floors(self):
        self.declare("http://127.0.0.1:4000")
        only_ollama = {"allowed_adapters": ["ollama"], "denied_models": [], "quota_substitution": True}
        both = {"allowed_adapters": ["ollama", "claude"], "denied_models": [], "quota_substitution": True}
        self.at_phase(4, "routine", only_ollama)
        spec = self.spec("architect")
        self.assertEqual((spec.adapter, spec.resolution_source), ("ollama", "adaptive"))
        self.assertEqual(spec.adaptive_routing["profile"]["source"], "local:ollama")
        # an implementer is never routed to it
        with self.assertRaisesRegex(lib.HandsoffError, "adaptive routing paused"):
            self.spec("implementer")
        # above its floors it is not a candidate: claude takes the work
        self.set_risk("elevated")
        status = self.read_status()
        status["model_policy"] = both
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="both")
        spec = self.spec("architect")
        self.assertEqual((spec.adapter, spec.adaptive_routing["tier"]), ("claude", "STANDARD"))

    def test_a_routed_local_session_validates_and_a_priced_one_does_not(self):
        self.declare("http://127.0.0.1:4000")
        self.at_phase(4, "routine", {"allowed_adapters": ["ollama"], "denied_models": [],
                                     "quota_substitution": True})
        spec = self.spec("architect")
        routing.validate_session_adaptive_routing(spec.adaptive_routing)
        forged = json.loads(json.dumps(spec.adaptive_routing))
        forged["profile"]["pricing"] = {"input_per_mtok": 0.0, "output_per_mtok": 0.0}
        with self.assertRaises(lib.HandsoffError):
            routing.validate_session_adaptive_routing(forged)

    def test_fallback_selection_respects_the_floors(self):
        entries = [{"adapter": "ollama", "model": MODEL}]
        availability = {"codex": True, "claude": True, "ollama": True}
        attempted = [("claude", "claude-opus-5")]
        policy = {"allowed_adapters": ["claude", "ollama"], "quota_substitution": True}
        refused = lib.plan_agent_fallback("implementer", "rate_limit", entries, availability, attempted,
                                          0, 2, model_policy=policy)
        self.assertEqual(refused["skipped"], [{"index": 0, "reason": "insufficient_capability"}])
        chosen = lib.plan_agent_fallback("architect", "rate_limit", entries, availability, attempted,
                                         0, 2, model_policy=policy)
        self.assertEqual(chosen["profile"], entries[0])
        # the reserved profile meets the risk and role floors when it is built
        self.declare("http://127.0.0.1:4000")
        self.at_phase(4, "irreversible")
        with mock.patch.object(agent.lib, "validate_runtime_integrity"):
            with self.assertRaisesRegex(lib.HandsoffError, "refused before session creation: irreversible"):
                agent.build_profile_launch_spec(self.tmp, "architect", "task", entries[0], skip_preflight=True)
            with self.assertRaisesRegex(lib.HandsoffError, "does not serve the implementer role"):
                agent.build_profile_launch_spec(self.tmp, "implementer", "task", entries[0], skip_preflight=True)
            self.set_risk("routine")
            spec = agent.build_profile_launch_spec(self.tmp, "architect", "task", entries[0], skip_preflight=True)
        self.assertEqual((spec.adapter, spec.argv[0], spec.routing_contract["locality"]),
                         ("ollama", PYTHON, "local"))

    def test_a_local_failure_falls_back_to_the_configured_cloud_provider(self):
        server = self.serve(chat=[503])
        self.declare(server.host)
        self.assign("supervisor", "ollama", MODEL)
        _set_key(self.tmp / "handsoff.toml", "fallback_policy", "supervisor",
                 '[{adapter = "claude", model = "claude-sonnet-5"}]')
        self.at_phase(4)
        spec = self.spec("supervisor")
        self.assertEqual(spec.adapter, "ollama")
        question = 'HANDSOFF_QUESTION: {"text": "Continue?", "options": ["Yes", "No"], "recommended": "Yes"}\n'
        launched = []

        def popen(argv, **kwargs):
            launched.append(argv[1] if len(argv) > 1 else argv[0])
            if str(argv[1]).endswith("handsoff_ollama.py"):
                return subprocess.Popen(argv, **kwargs)
            from tests.test_session_result_autoadopt import _FakeProcess
            return _FakeProcess(stdout=question)

        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(agent.lib, "validate_runtime_integrity"):
            code = agent.execute_with_recovery(
                spec, popen_factory=popen,
                which=lambda name: f"/opt/test/{name}" if name == "claude" else None,
                snapshotter=lambda root: {"head": "a" * 40, "branch": "main", "dirty": False,
                                          "status_sha256": "b" * 64})
        self.assertEqual(code, 0, out.getvalue())
        status = self.read_status()
        replacement = status["agent_replacements"][-1]
        self.assertEqual((replacement["action"], replacement["planner_reason"], replacement["selected_profile"]),
                         ("launch", "local_failure_substitution",
                          {"adapter": "claude", "model": "claude-sonnet-5"}))
        source = status["agent_sessions"][replacement["from_session_id"]]
        target = status["agent_sessions"][replacement["to_session_id"]]
        self.assertEqual((source["adapter"], source["state"]), ("ollama", "failed"))
        self.assertEqual((target["adapter"], target["state"]), ("claude", "completed"))
        self.assertEqual(replacement["attempt"], 1)
        self.assertEqual(len(launched), 2)


if __name__ == "__main__":
    unittest.main()
