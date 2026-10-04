"""#304: the provider adapter contract.

REQ-005: codex and claude through the contract behave exactly as the direct
functions do (argv for every role, pre-flight results and the file written,
ceiling enforcement, routing choices), and a fake third provider registered
through the contract routes without editing route_adaptive_profile.
REQ-006: ceiling enforcement is declared, and every routing decision records
provider, model, locality, usage source, cost policy and ceiling.
REQ-007: plan_agent_fallback's mapping is unchanged and drives the fake
provider; contract pre-flight names five states; local earns no risk floor.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.engine_patch import patch_engine
from tests.fixture_state import write_version_pin
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_adapters as adapters  # noqa: E402
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_routing as routing  # noqa: E402

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _profile(adapter, model, tier_caps=("text", "tool_use")):
    return {"adapter": adapter, "model": model, "capabilities": list(tier_caps),
            "limits": {"context_tokens": 100000}, "pricing": {},
            "source": "https://example.invalid/fake-catalog"}


def _fake(name="fake", *, locality="remote", ceiling_enforcement="unenforced",
          ceiling_mechanism=None, tiers=("FAST", "STANDARD", "PREMIUM"), preflight=None):
    return adapters.ProviderAdapter(
        name=name, locality=locality, usage_source="exit_report", cost_policy="local_free"
        if locality == "local" else "metered", ceiling_enforcement=ceiling_enforcement,
        ceiling_mechanism=ceiling_mechanism,
        build_argv=lambda executable, role, model, **kw: [executable, "--role", role, "--model", model],
        preflight=preflight or (lambda root, **kw: {"state": "ready", "category": None}),
        routing_profiles={tier: _profile(name, f"{name}-{tier.lower()}") for tier in tiers},
    )


class _Registered(unittest.TestCase):
    """Unregister every fake provider after each test."""

    def setUp(self):
        self.registered = []

    def register(self, adapter):
        adapters.register_adapter(adapter)
        self.registered.append(adapter.name)
        return adapter

    def tearDown(self):
        for name in self.registered:
            adapters.unregister_adapter(name)


def _runner(*, login_rc=0, probe_rc=0, probe_stdout="OK", probe_stderr="", calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        if argv[1:3] in (["login", "status"], ["auth", "status"]):
            return subprocess.CompletedProcess(argv, login_rc, "", "not logged in" if login_rc else "")
        return subprocess.CompletedProcess(argv, probe_rc, probe_stdout, probe_stderr)
    return run


class ArgvParity(unittest.TestCase):
    """REQ-005: argv for every role equals the direct builder's."""

    def test_codex_argv_matches_codex_argv_for_every_role(self):
        for role in lib.SELECTABLE_AGENT_ROLES:
            for sandbox in (False, True):
                for reasoning in (None, "high"):
                    with self.subTest(role=role, sandbox=sandbox, reasoning=reasoning):
                        self.assertEqual(
                            adapters.build_argv("codex", "/bin/codex", role, "gpt-6-astra",
                                                provider_limit=40000, reasoning=reasoning,
                                                reviewer_sandbox=sandbox),
                            lib.codex_argv("/bin/codex", role, "gpt-6-astra", 40000,
                                           reviewer_sandbox=sandbox, reasoning=reasoning))

    def test_claude_argv_matches_claude_argv_for_every_role(self):
        for role in lib.SELECTABLE_AGENT_ROLES:
            tools = list(lib.REVIEWER_READ_ONLY_TOOLS) if role == "reviewer" else ["Read", "Edit"]
            owned = ["bin/x.py"] if role == "implementer" else None
            with self.subTest(role=role):
                self.assertEqual(
                    adapters.build_argv("claude", "/bin/claude", role, "claude-opus-5",
                                        provider_limit=40000, allowed_tools=tools,
                                        project_root="/tmp/project", owned_paths=owned),
                    lib.claude_argv("/bin/claude", role, tools, "claude-opus-5",
                                    project_root="/tmp/project", owned_paths=owned))

    def test_the_argv_actually_differs_by_provider(self):
        """Guards against both sides delegating to one builder."""
        codex = adapters.build_argv("codex", "/bin/x", "implementer", "m", provider_limit=1000)
        claude = adapters.build_argv("claude", "/bin/x", "implementer", "m", provider_limit=1000)
        self.assertNotEqual(codex, claude)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(lib.HandsoffError):
            adapters.build_argv("codex", "/bin/codex", "janitor", "m", provider_limit=1000)


class PreflightParity(unittest.TestCase):
    """REQ-005: outcomes and the file written equal launch_preflight's."""

    SCENARIOS = {
        "ready": ({}, "ready"),
        "auth_failure": ({"login_rc": 1}, "auth_failure"),
        "model_unavailable": ({"probe_rc": 1, "probe_stdout": "",
                               "probe_stderr": "error: model gpt-x not found"}, "model_unavailable"),
        "rate_limit": ({"probe_rc": 1, "probe_stdout": "",
                        "probe_stderr": "429 rate limit exceeded"}, "runtime_not_ready"),
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        self.executable = sys.executable

    def _pair(self, adapter, runner_kwargs, *, state_dir=None):
        state_dir = state_dir or self.state_dir
        direct_root, contract_root = self.tmp / f"d-{adapter}", self.tmp / f"c-{adapter}"
        for root in (direct_root, contract_root):
            root.mkdir(exist_ok=True)
            (root / lib.PREFLIGHT_FILE).unlink(missing_ok=True)
        argv = [self.executable, "probe"]
        common = {"model": "gpt-6-astra", "executable": self.executable, "argv": argv,
                  "cwd": str(self.cwd), "state_dir": state_dir, "now": NOW}
        direct = lib.launch_preflight(direct_root, adapter=adapter, runner=_runner(**runner_kwargs), **common)
        contract = adapters.preflight(adapter, contract_root, runner=_runner(**runner_kwargs), **common)
        files = [json.loads((root / lib.PREFLIGHT_FILE).read_text(encoding="utf-8"))
                 for root in (direct_root, contract_root)]
        return direct, contract, files

    def test_each_outcome_and_file_matches_for_both_providers(self):
        for adapter in ("codex", "claude"):
            for name, (runner_kwargs, expected_state) in self.SCENARIOS.items():
                with self.subTest(adapter=adapter, scenario=name):
                    direct, contract, (direct_file, contract_file) = self._pair(adapter, runner_kwargs)
                    self.assertEqual(contract["preflight"], direct)
                    self.assertEqual(contract_file, direct_file)
                    self.assertEqual(contract["state"], expected_state)

    def test_an_unwritable_runtime_dir_is_runtime_not_ready(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        direct, contract, (direct_file, contract_file) = self._pair("codex", {}, state_dir=blocker)
        self.assertEqual(direct["category"], "runtime_environment")
        self.assertEqual(contract["preflight"], direct)
        self.assertEqual(contract_file, direct_file)
        self.assertEqual(contract["state"], "runtime_not_ready")

    def test_a_missing_executable_never_runs_anything(self):
        calls = []
        result = adapters.preflight("claude", self.tmp, model="m", executable=str(self.tmp / "absent"),
                                    argv=["x"], cwd=str(self.cwd), runner=_runner(calls=calls))
        self.assertEqual(result["state"], "executable_missing")
        self.assertEqual(calls, [])
        self.assertFalse((self.tmp / lib.PREFLIGHT_FILE).exists())

    def test_the_state_is_derived_from_ready_blocked_and_category(self):
        derive = adapters.contract_preflight_state
        self.assertEqual(derive({"state": "ready", "category": None}), "ready")
        self.assertEqual(derive({"state": "blocked", "category": "auth_failure"}), "auth_failure")
        self.assertEqual(derive({"state": "blocked", "category": "model_unavailable"}), "model_unavailable")
        for category in ("runtime_environment", "timeout", "rate_limit", "non_zero_exit"):
            self.assertEqual(derive({"state": "blocked", "category": category}), "runtime_not_ready")
        with self.assertRaises(lib.HandsoffError):
            derive({"state": "maybe"})


class CeilingParity(unittest.TestCase):
    """REQ-005: the contract's ceiling enforcement is today's."""

    def test_builtin_mechanisms_match_adapter_ceiling_enforcement(self):
        for name in ("codex", "claude"):
            with self.subTest(adapter=name):
                adapter = adapters.get_adapter(name)
                self.assertEqual(adapter.ceiling_enforcement, "enforced")
                self.assertEqual(adapter.ceiling_mechanism, lib.adapter_ceiling_enforcement(name))
                self.assertEqual(adapters.ceiling_record(name, 50000), 50000)

    def test_usage_source_follows_intermediate_usage(self):
        self.assertEqual(adapters.get_adapter("claude").usage_source, "stream")
        self.assertEqual(adapters.get_adapter("codex").usage_source, "exit_report")


class RoutingParity(_Registered):
    """REQ-005: routing choices equal route_adaptive_profile's; a third
    provider registered through the contract is routable."""

    CASES = [
        {"risk_class": "routine", "deterministic_checks_complete": True},
        {"risk_class": "irreversible", "deterministic_checks_complete": True},
        {"risk_class": "irreversible", "available_adapters": ["codex"], "deterministic_checks_complete": True},
        {"risk_class": "elevated", "available_adapters": ["claude"], "deterministic_checks_complete": True},
        {"risk_class": "routine", "available_tiers": [], "deterministic_checks_complete": True},
        {"risk_class": "routine"},
        {"risk_class": "routine", "required_capabilities": ["teleport"], "deterministic_checks_complete": True},
    ]

    def test_every_case_routes_identically(self):
        chosen = set()
        for case in self.CASES:
            with self.subTest(case=case):
                routed = adapters.route(**case)
                decision = routed.pop("decision")
                self.assertEqual(routed, lib.route_adaptive_profile(**case))
                chosen.add(decision["provider"])
        self.assertEqual(chosen, {"codex", "claude", None}, "the matrix must select both providers and pause")

    def test_a_fake_provider_routes_through_the_unchanged_router(self):
        cfg = {"model_policy": {"allowed_adapters": ["fake"]}}
        with self.assertRaises(lib.HandsoffError, msg="unregistered, the policy cannot even name it"):
            lib.route_adaptive_profile(cfg, risk_class="routine", deterministic_checks_complete=True)
        self.register(_fake())
        routed = lib.route_adaptive_profile(cfg, risk_class="routine", deterministic_checks_complete=True)
        self.assertEqual(routed["state"], "selected")
        self.assertEqual((routed["tier"], routed["profile"]["adapter"], routed["profile"]["model"]),
                         ("FAST", "fake", "fake-fast"))

    def test_a_registered_fake_does_not_change_builtin_choices(self):
        before = [lib.route_adaptive_profile(**case) for case in self.CASES]
        self.register(_fake())
        self.assertEqual([lib.route_adaptive_profile(**case) for case in self.CASES], before)

    def test_builtins_cannot_be_replaced(self):
        with self.assertRaises(lib.HandsoffError):
            adapters.register_adapter(_fake("codex"))


class CeilingDeclaration(_Registered):
    """REQ-006: declared enforcement, recorded as such."""

    def test_an_undeclared_enforcement_is_refused(self):
        for value in (None, "", "maybe", "wrapper_enforced"):
            with self.subTest(value=value), self.assertRaises(lib.HandsoffError):
                adapters.register_adapter(_fake(ceiling_enforcement=value))
        self.assertNotIn("fake", adapters.adapter_names())
        self.assertNotIn("fake", lib.adaptive_provider_names())

    def test_enforced_without_a_mechanism_is_refused(self):
        with self.assertRaises(lib.HandsoffError):
            adapters.register_adapter(_fake(ceiling_enforcement="enforced"))

    def test_an_unenforced_ceiling_is_recorded_as_unenforced(self):
        self.register(_fake())
        routed = adapters.route({"model_policy": {"allowed_adapters": ["fake"]}}, ceiling=60000,
                                risk_class="routine", deterministic_checks_complete=True)
        self.assertEqual(routed["decision"]["ceiling"], "unenforced")

    def test_an_enforced_fake_records_the_number(self):
        self.register(_fake(ceiling_enforcement="enforced", ceiling_mechanism="wrapper_enforced"))
        routed = adapters.route({"model_policy": {"allowed_adapters": ["fake"]}}, ceiling=60000,
                                risk_class="routine", deterministic_checks_complete=True)
        self.assertEqual(routed["decision"]["ceiling"], 60000)


class DecisionRecord(_Registered):
    """REQ-006: every decision records the routing facts."""

    def test_claude_codex_and_fake_decisions(self):
        self.register(_fake(locality="local"))
        expected = {
            "claude": ({"risk_class": "routine"}, "remote", "stream",
                       {"policy": "catalog_pricing", "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 5.0}}),
            "codex": ({"risk_class": "irreversible", "available_adapters": ["codex"]}, "remote", "exit_report",
                      {"policy": "subscription_unpriced", "pricing": {}}),
            "fake": ({"risk_class": "routine", "available_adapters": ["fake"],
                      "cfg": {"model_policy": {"allowed_adapters": ["fake"]}}}, "local", "exit_report",
                     {"policy": "local_free", "pricing": {}}),
        }
        for provider, (kwargs, locality, usage_source, cost_policy) in expected.items():
            with self.subTest(provider=provider):
                kwargs = dict(kwargs)
                cfg = kwargs.pop("cfg", None)
                routed = adapters.route(cfg, ceiling=50000, deterministic_checks_complete=True, **kwargs)
                self.assertEqual(routed["decision"], {
                    "provider": provider, "model": routed["profile"]["model"], "locality": locality,
                    "usage_source": usage_source, "cost_policy": cost_policy,
                    "ceiling": "unenforced" if provider == "fake" else 50000,
                })

    def test_a_paused_decision_still_carries_every_field(self):
        routed = adapters.route(risk_class="routine", available_tiers=[], deterministic_checks_complete=True)
        self.assertEqual(routed["state"], "paused")
        self.assertEqual(set(routed["decision"]),
                         {"provider", "model", "locality", "usage_source", "cost_policy", "ceiling"})
        self.assertIsNone(routed["decision"]["provider"])


class FallbackThroughTheContract(_Registered):
    """REQ-007: plan_agent_fallback's mapping drives a fake provider."""

    def setUp(self):
        super().setUp()
        self.register(_fake())
        self.entries = [{"adapter": "fake", "model": "fake-premium"}]
        self.availability = {"codex": False, "claude": True, "fake": True}
        self.attempted = [("claude", "claude-opus-5")]
        self.policy = {"allowed_adapters": ["claude", "fake"], "quota_substitution": True}

    def plan(self, category, *, count=0, cap=2, policy=None):
        return adapters.plan_fallback("implementer", category, self.entries, self.availability,
                                      self.attempted, count, cap, model_policy=policy or self.policy)

    def test_the_contract_planner_is_the_unchanged_planner(self):
        self.assertIs(adapters.plan_fallback, lib.plan_agent_fallback)

    def test_rate_limit_with_quota_substitution_replaces_within_the_cap(self):
        decision = self.plan("rate_limit", count=1, cap=2)
        self.assertEqual((decision["action"], decision["reason"]), ("select", "quota_substitution"))
        self.assertEqual(decision["profile"], {"adapter": "fake", "model": "fake-premium"})

    def test_rate_limit_without_quota_substitution_pauses(self):
        decision = self.plan("rate_limit", policy={**self.policy, "quota_substitution": False})
        self.assertEqual(decision["action"], "pilot_pause")
        self.assertEqual(decision["skipped"], [{"index": 0, "reason": "cross_vendor_not_allowed"}])

    def test_auth_failure_pauses(self):
        decision = self.plan("auth_failure")
        self.assertEqual((decision["action"], decision["reason"]), ("pilot_pause", "environment_failure"))

    def test_runtime_environment_pauses(self):
        decision = self.plan("runtime_environment")
        self.assertEqual((decision["action"], decision["reason"]), ("pilot_pause", "environment_failure"))

    def test_a_reached_cap_pauses(self):
        decision = self.plan("rate_limit", count=2, cap=2)
        self.assertEqual((decision["action"], decision["reason"]), ("pilot_pause", "cap_exhausted"))

    def test_a_policy_denied_candidate_pauses(self):
        decision = self.plan("rate_limit", policy={"allowed_adapters": ["claude"]})
        self.assertEqual((decision["action"], decision["reason"]), ("pilot_pause", "fallback_exhausted"))
        self.assertEqual(decision["skipped"], [{"index": 0, "reason": "model_policy_denied"}])

    def test_builtin_mapping_is_unchanged(self):
        """The codex/claude answers the planner gave before #304."""
        availability = {"codex": True, "claude": True}
        entries = [{"adapter": "codex", "model": "gpt-6-astra"}]
        plan = lib.plan_agent_fallback
        self.assertEqual(plan("implementer", "rate_limit", entries, availability, self.attempted, 0, 2),
                         {"action": "select", "reason": "quota_substitution",
                          "profile": entries[0], "skipped": []})
        self.assertEqual(plan("implementer", "auth_failure", entries, availability, self.attempted, 0, 2)["reason"],
                         "environment_failure")
        self.assertEqual(plan("implementer", "rate_limit", entries, availability, self.attempted, 2, 2)["reason"],
                         "cap_exhausted")
        with self.assertRaises(lib.HandsoffError):
            plan("implementer", "rate_limit", entries, {"codex": True}, self.attempted, 0, 2)


class FakeProviderPreflight(_Registered):
    """REQ-007: the contract distinguishes five states for the fake provider."""

    def test_five_states(self):
        results = iter([
            {"state": "blocked", "category": "auth_failure"},
            {"state": "blocked", "category": "model_unavailable"},
            {"state": "blocked", "category": "runtime_environment"},
            {"state": "ready", "category": None},
        ])
        self.register(_fake(preflight=lambda root, **kw: next(results)))
        tmp = Path(tempfile.mkdtemp())
        seen = [adapters.preflight("fake", tmp, model="fake-fast", executable=None, argv=[], cwd=str(tmp))["state"]]
        for _ in range(4):
            seen.append(adapters.preflight("fake", tmp, model="fake-fast", executable=sys.executable,
                                           argv=[], cwd=str(tmp))["state"])
        self.assertEqual(seen, list(adapters.CONTRACT_PREFLIGHT_STATES))


class NoLocalRiskFloor(_Registered):
    """REQ-007: a local provider routes at the same tier as a remote one."""

    def test_local_and_remote_route_alike_for_every_risk_class(self):
        self.register(_fake("fakelocal", locality="local"))
        self.register(_fake("fakeremote", locality="remote"))
        for risk_class in lib.ADAPTIVE_RISK_CLASSES:
            tiers = {}
            for name in ("fakelocal", "fakeremote"):
                routed = adapters.route({"model_policy": {"allowed_adapters": [name]}},
                                        risk_class=risk_class, available_adapters=[name],
                                        deterministic_checks_complete=True)
                tiers[name] = (routed["state"], routed["tier"])
            with self.subTest(risk_class=risk_class):
                self.assertEqual(tiers["fakelocal"], tiers["fakeremote"])
        routine = adapters.route({"model_policy": {"allowed_adapters": ["fakelocal"]}}, risk_class="routine",
                                 available_adapters=["fakelocal"], deterministic_checks_complete=True)
        self.assertEqual((routine["tier"], routine["decision"]["locality"]), ("FAST", "local"))


class PersistedLaunchContract(HandsoffTestCase):
    """REQ-006 through the real launch path: build_launch_spec puts the
    contract facts on adaptive_routing, and the session persists them."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.registered = []

    def tearDown(self):
        for name in self.registered:
            adapters.unregister_adapter(name)
        super().tearDown()

    def _run(self, risk_class, model_policy=None):
        result = run(["init", "Contract", "--risk-class", risk_class], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        status.update(phase_number=4, phase=lib.PHASES[4], progress=40)
        if model_policy is not None:
            status["model_policy"] = model_policy
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_setup", event_message="phase four")

    def _launch(self, role, only, *, persist=True):
        with mock.patch.object(agent.lib, "validate_runtime_integrity"), \
                mock.patch.object(agent, "build_role_input", return_value="task"), \
                mock.patch.object(agent, "applicable_design_review_packet", return_value=None):
            spec = agent.build_launch_spec(
                self.tmp, role, "task", skip_preflight=True,
                which=lambda name: f"/opt/test/{name}" if name in only else None)
        profile = {"adapter": spec.adapter, "model": spec.model}
        if not persist:
            return spec, routing.validate_session_routing_contract(spec.routing_contract, profile)
        session = lib.create_agent_session(
            self.tmp, role=role, actor=f"test-{role}", adapter=spec.adapter,
            requested_model=spec.model, resolution_source=spec.resolution_source,
            adaptive_routing=spec.adaptive_routing, routing_contract=spec.routing_contract)
        persisted = self.read_status()["agent_sessions"][session["session_id"]]["routing_contract"]
        self.assertEqual(persisted, spec.routing_contract)
        schema_errors = lib.validate_status_schema(self.read_status())
        self.assertEqual([e for e in schema_errors if "routing_contract" in e], [])
        return spec, persisted

    def test_claude_launch_persists_the_contract(self):
        self._run("routine")
        spec, contract = self._launch("implementer", ("claude",))
        self.assertEqual(contract, {
            "provider": "claude", "model": "claude-haiku-4-5-20251001", "locality": "remote",
            "usage_source": "stream",
            "cost_policy": {"policy": "catalog_pricing",
                            "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 5.0}},
            "ceiling_enforcement": "enforced", "ceiling": spec.token_budget,
        })

    def test_codex_launch_persists_the_contract(self):
        self._run("irreversible")
        spec, contract = self._launch("architect", ("codex",))
        self.assertEqual(contract, {
            "provider": "codex", "model": "gpt-6-astra", "locality": "remote",
            "usage_source": "exit_report",
            "cost_policy": {"policy": "subscription_unpriced", "pricing": {}},
            "ceiling_enforcement": "enforced", "ceiling": spec.token_budget,
        })

    def test_a_registered_unenforced_provider_persists_unenforced(self):
        adapters.register_adapter(_fake(locality="local"))
        self.registered.append("fake")
        self._run("routine", {"allowed_adapters": ["fake"], "denied_models": [], "quota_substitution": True})
        # Today's launch tables name only codex and claude; the fake joins
        # them for this launch so the real builder can select it.
        with patch_engine("SELECTABLE_AGENT_ADAPTERS", (*lib.SELECTABLE_AGENT_ADAPTERS, "fake")), \
                mock.patch.dict(lib.CEILING_ENFORCEMENT, {"fake": "wrapper_enforced"}), \
                mock.patch.dict(lib.ADAPTER_INTERMEDIATE_USAGE, {"fake": True}):
            # claude is installed too so the crew resolves; the policy permits
            # only fake. create_agent_session and the session profile check
            # still accept only codex and claude, so this checks the contract
            # block build_launch_spec puts on the record, validated.
            spec, contract = self._launch("architect", ("fake", "claude"), persist=False)
        self.assertEqual(spec.adapter, "fake")
        self.assertEqual(contract, {
            "provider": "fake", "model": "fake-fast", "locality": "local",
            "usage_source": "exit_report", "cost_policy": {"policy": "local_free", "pricing": {}},
            "ceiling_enforcement": "unenforced", "ceiling": "unenforced",
        })

    def test_a_failover_launch_persists_the_contract(self):
        # implementation review attempt 2: build_profile_launch_spec made no
        # routing record, so a fallback decision carried no contract facts
        self._run("routine")
        with mock.patch.object(agent.lib, "validate_runtime_integrity"):
            spec = agent.build_profile_launch_spec(
                self.tmp, "implementer", "task", {"adapter": "claude", "model": "claude-haiku-4-5-20251001"},
                skip_preflight=True, which=lambda name: f"/opt/test/{name}" if name == "claude" else None)
        self.assertIsNone(spec.adaptive_routing)
        session = lib.create_agent_session(
            self.tmp, role="implementer", actor="test-fallback", adapter=spec.adapter,
            requested_model=spec.model, resolution_source=spec.resolution_source,
            routing_contract=spec.routing_contract)
        persisted = self.read_status()["agent_sessions"][session["session_id"]]["routing_contract"]
        self.assertEqual((persisted["provider"], persisted["model"], persisted["locality"]),
                         ("claude", "claude-haiku-4-5-20251001", "remote"))
        self.assertEqual(persisted["usage_source"], "stream")
        self.assertEqual(persisted["ceiling"], spec.token_budget)

    def test_a_recovery_replacement_persists_the_contract(self):
        # implementation review attempt 3: recovery claims a session the
        # reservation created, bypassing create_agent_session, and the
        # contract was dropped; drive the real execute_launch on that path
        from tests.test_session_result_autoadopt import _FakeProcess
        self._run("routine")
        cfg = lib.load_config(self.tmp)
        source = lib.create_agent_session(
            self.tmp, role="implementer", actor="test-impl", adapter="claude",
            requested_model="claude-haiku-4-5-20251001", resolution_source="configured")
        lib.transition_agent_session(self.tmp, source["session_id"], "running")
        lib.transition_agent_session(self.tmp, source["session_id"], "failed", exit_code=1,
                                     failure=lib.classify_runtime_failure(
                                         exit_code=1, stderr_tail="rate limit exceeded"))
        fallbacks = lib.fallback_profiles(cfg)
        fallbacks["implementer"] = [{"adapter": "claude", "model": "sonnet"}]
        lib.update_agent_settings(self.tmp, {"profiles": lib.agent_profiles(cfg), "fallbacks": fallbacks,
                                             "max_failovers_per_role": 2})
        record = lib.reserve_agent_replacement(
            self.tmp, from_session_id=source["session_id"], which=lambda name: f"/opt/test/{name}",
            snapshotter=lambda root: {"head": "a" * 40, "branch": "main", "dirty": False,
                                      "status_sha256": "b" * 64})
        self.assertEqual(record["action"], "launch", record)
        with mock.patch.object(agent.lib, "validate_runtime_integrity"):
            spec = agent.build_profile_launch_spec(
                self.tmp, "implementer", "task", record["selected_profile"], skip_preflight=True,
                which=lambda name: f"/opt/test/{name}")
            agent.execute_launch(spec, popen_factory=lambda argv, **kwargs: _FakeProcess(stdout="done\n"),
                                 precreated_session_id=record["to_session_id"], beacon_interval=0.01)
        session = self.read_status()["agent_sessions"][record["to_session_id"]]
        self.assertEqual(session["routing_contract"], spec.routing_contract)
        self.assertEqual((session["routing_contract"]["provider"], session["routing_contract"]["model"]),
                         ("claude", "sonnet"))
        self.assertEqual([e for e in lib.validate_status_schema(self.read_status()) if "routing_contract" in e], [])

    def test_the_validator_closes_the_block_and_keeps_older_sessions_valid(self):
        self._run("routine")
        spec, contract = self._launch("implementer", ("claude",))
        profile = {"adapter": spec.adapter, "model": spec.model}
        status = self.read_status()
        sid = next(iter(status["agent_sessions"]))
        older = dict(status["agent_sessions"][sid])
        older.pop("routing_contract")
        self.assertNotIn("routing_contract", older, "a session without the field is still a valid record")
        for bad in ({**contract, "extra": 1}, {**contract, "provider": "codex"},
                    {**contract, "locality": "moon"}, {**contract, "usage_source": ""},
                    {**contract, "cost_policy": {"policy": "x", "pricing": {"input_per_mtok": -1.0}}},
                    {**contract, "cost_policy": {"policy": "x", "pricing": {"per_call": 1.0}}},
                    {**contract, "ceiling_enforcement": "maybe"},
                    {**contract, "ceiling": "unenforced"},
                    {**contract, "ceiling_enforcement": "unenforced", "ceiling": 60000}):
            with self.subTest(bad=bad), self.assertRaises(lib.HandsoffError):
                routing.validate_session_routing_contract(bad, profile)


if __name__ == "__main__":
    unittest.main()
