#!/usr/bin/env python3
"""The provider adapter contract (#304).

One record per provider says everything Handsoff needs to route, launch and
bound it: its name, where it runs, where its usage comes from, how its cost
is accounted, whether it can enforce a budget ceiling, how to build its argv
for a role, and how to pre-flight it. codex and claude are expressed here by
delegating to the functions that launch them today (`codex_argv`,
`claude_argv`, `launch_preflight`), so the contract is a thin layer whose
parity with those functions is provable rather than a second implementation.

A third provider joins with `register_adapter`. That adds its per-tier
profiles to the routing registry, which `route_adaptive_profile`,
`validate_model_policy` and `plan_agent_fallback` already read, so none of
them is edited for it. Model policy still decides whether it may run: the
default policy names only codex and claude.

Ceiling enforcement is declared, never assumed. An adapter that cannot
enforce a ceiling says `unenforced`, and a routing decision then records
`unenforced` instead of a number that nothing would hold.
"""
from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import handsoff_lib as lib
from handsoff_core import HandsoffError
from handsoff_routing import (  # noqa: F401
    ADAPTER_LOCALITIES,
    ADAPTIVE_BUILTIN_PROVIDERS,
    ADAPTIVE_PROVIDER_REGISTRY,
    CEILING_ENFORCEMENT_DECLARATIONS,
    register_adaptive_provider,
    route_adaptive_profile,
    unregister_adaptive_provider,
)


#: What a contract pre-flight can say, in the order a launch meets them.
CONTRACT_PREFLIGHT_STATES = ("executable_missing", "auth_failure", "model_unavailable",
                             "runtime_not_ready", "ready")


@dataclass(frozen=True)
class ProviderAdapter:
    """One provider, as the engine needs to know it.

    `build_argv(executable, role, model, *, provider_limit, reasoning,
    reviewer_sandbox, allowed_tools, project_root, owned_paths)` returns the
    launch argv. `preflight(root, *, model, executable, argv, cwd, **kw)`
    returns a `launch_preflight`-shaped result: `state` ready or blocked and,
    when blocked, a `category`.
    """
    name: str
    locality: str
    usage_source: str
    cost_policy: str
    ceiling_enforcement: str
    build_argv: Callable[..., list]
    preflight: Callable[..., dict]
    #: Per-tier profiles routing may choose for this provider besides the
    #: configured tier profile.
    routing_profiles: dict = field(default_factory=dict)
    #: How an enforced ceiling is held (`CEILING_ENFORCEMENT` vocabulary).
    ceiling_mechanism: str | None = None


_ADAPTERS: dict[str, ProviderAdapter] = {}


def _validate(adapter: object) -> ProviderAdapter:
    if not isinstance(adapter, ProviderAdapter):
        raise HandsoffError("an adapter must be a ProviderAdapter")
    if adapter.locality not in ADAPTER_LOCALITIES:
        raise HandsoffError(f"adapter {adapter.name} locality must be remote or local")
    for name in ("usage_source", "cost_policy"):
        value = getattr(adapter, name)
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"adapter {adapter.name} must declare {name}")
    if adapter.ceiling_enforcement not in CEILING_ENFORCEMENT_DECLARATIONS:
        raise HandsoffError(
            f"adapter {adapter.name} must declare ceiling enforcement as enforced or unenforced")
    if adapter.ceiling_enforcement == "enforced" and not adapter.ceiling_mechanism:
        raise HandsoffError(f"adapter {adapter.name} declares an enforced ceiling without naming how")
    if not callable(adapter.build_argv) or not callable(adapter.preflight):
        raise HandsoffError(f"adapter {adapter.name} must provide build_argv and preflight")
    return adapter


def register_adapter(adapter: ProviderAdapter) -> ProviderAdapter:
    """Register a third provider: the contract record and its routing profiles."""
    _validate(adapter)
    if adapter.name in ADAPTIVE_BUILTIN_PROVIDERS:
        raise HandsoffError(f"adapter {adapter.name} is built in")
    register_adaptive_provider(adapter.name, adapter.routing_profiles)
    _ADAPTERS[adapter.name] = adapter
    return adapter


def unregister_adapter(name: str) -> None:
    unregister_adaptive_provider(name)
    _ADAPTERS.pop(name, None)


def get_adapter(name: str) -> ProviderAdapter:
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise HandsoffError(f"no provider adapter is registered as {name!r}")
    return adapter


def adapter_names() -> tuple[str, ...]:
    return tuple(_ADAPTERS)


def build_argv(name: str, executable: str, role: str, model: str, *, provider_limit: int,
               reasoning: str | None = None, reviewer_sandbox: bool = False,
               allowed_tools: list[str] | None = None, project_root: Path | str | None = None,
               owned_paths: list[str] | None = None) -> list[str]:
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise HandsoffError(f"role {role!r} is not a managed role")
    return list(get_adapter(name).build_argv(
        executable, role, model, provider_limit=provider_limit, reasoning=reasoning,
        reviewer_sandbox=reviewer_sandbox, allowed_tools=allowed_tools,
        project_root=project_root, owned_paths=owned_paths))


def contract_preflight_state(result: dict) -> str:
    """Derive the contract state from a ready/blocked result and its category."""
    if not isinstance(result, dict) or result.get("state") not in {"ready", "blocked"}:
        raise HandsoffError("a pre-flight result must be ready or blocked")
    if result["state"] == "ready":
        return "ready"
    category = result.get("category")
    if category in {"auth_failure", "model_unavailable"}:
        return category
    return "runtime_not_ready"


def _executable_present(executable: object) -> bool:
    if not isinstance(executable, str) or not executable:
        return False
    path = Path(executable)
    return path.is_file() and os.access(path, os.X_OK)


def preflight(name: str, root: Path, *, model: str, executable: str | None,
              argv: list[str] | tuple[str, ...], cwd: str, **kwargs) -> dict:
    """Pre-flight one provider/model and name the contract state.

    A missing executable is decided here, before the adapter is asked, so no
    adapter's probe ever runs a path that is not there.
    """
    adapter = get_adapter(name)
    if not _executable_present(executable):
        return {"adapter": name, "model": model, "state": "executable_missing", "preflight": None}
    result = adapter.preflight(root, model=model, executable=executable, argv=argv, cwd=cwd, **kwargs)
    return {"adapter": name, "model": model, "state": contract_preflight_state(result),
            "preflight": result}


def ceiling_record(name: str, ceiling: int | None) -> int | str | None:
    """The ceiling a decision records: the number only when it is enforced."""
    if get_adapter(name).ceiling_enforcement == "unenforced":
        return "unenforced"
    return ceiling


def _decision(profile: dict, ceiling: int | None) -> dict:
    adapter = get_adapter(profile["adapter"])
    return {
        "provider": adapter.name, "model": profile["model"], "locality": adapter.locality,
        "usage_source": adapter.usage_source,
        "cost_policy": {"policy": adapter.cost_policy, "pricing": deepcopy(profile.get("pricing") or {})},
        "ceiling": ceiling_record(adapter.name, ceiling),
    }


def route(cfg: dict | None = None, *, ceiling: int | None = None, **kwargs) -> dict:
    """`route_adaptive_profile`, plus the facts every decision records."""
    routed = route_adaptive_profile(cfg, **kwargs)
    decision = {"provider": None, "model": None, "locality": None, "usage_source": None,
                "cost_policy": None, "ceiling": None}
    if routed.get("state") == "selected":
        decision = _decision(routed["profile"], ceiling)
    return {**routed, "decision": decision}


def catalog_profile(cfg: dict | None, adapter: str, model: str) -> dict:
    """The configured routing profile for this exact pair, so a failover or
    explicitly routed launch records the same catalog pricing a routed one
    does; a pair the catalog does not list gets no pricing."""
    for profile in lib.adaptive_routing_profiles(cfg).values():
        if profile.get("adapter") == adapter and profile.get("model") == model:
            return profile
    for profiles in (lib.ADAPTIVE_DEFAULT_PROFILES, lib.ADAPTIVE_OPENAI_PROFILES):
        for profile in profiles.values():
            if profile.get("adapter") == adapter and profile.get("model") == model:
                return profile
    return {"adapter": adapter, "model": model}


def session_contract(profile: dict, ceiling: int) -> dict:
    """The contract block a routed managed session persists (REQ-006): the
    decision facts plus the adapter's declared ceiling enforcement."""
    return {**_decision(profile, ceiling),
            "ceiling_enforcement": get_adapter(profile["adapter"]).ceiling_enforcement}


#: Fallback is today's planner, unchanged: rate_limit with quota_substitution
#: may replace across providers within the cap; auth_failure,
#: runtime_environment, a reached cap and a policy-denied candidate pause.
plan_fallback = lib.plan_agent_fallback


def _codex_argv(executable, role, model, *, provider_limit, reasoning=None, reviewer_sandbox=False,
                allowed_tools=None, project_root=None, owned_paths=None):
    return lib.codex_argv(executable, role, model, provider_limit,
                          reviewer_sandbox=reviewer_sandbox, reasoning=reasoning)


def _claude_argv(executable, role, model, *, provider_limit, reasoning=None, reviewer_sandbox=False,
                 allowed_tools=None, project_root=None, owned_paths=None):
    return lib.claude_argv(executable, role, allowed_tools, model,
                           project_root=project_root, owned_paths=owned_paths)


def _launch_preflight_for(name: str):
    def run(root, *, model, executable, argv, cwd, **kwargs):
        return lib.launch_preflight(root, adapter=name, model=model, executable=executable,
                                    argv=argv, cwd=cwd, **kwargs)
    return run


def _builtin(name: str, build, cost_policy: str) -> ProviderAdapter:
    return _validate(ProviderAdapter(
        name=name, locality="remote",
        usage_source="stream" if lib.ADAPTER_INTERMEDIATE_USAGE[name] else "exit_report",
        cost_policy=cost_policy, ceiling_enforcement="enforced",
        ceiling_mechanism=lib.CEILING_ENFORCEMENT[name],
        build_argv=build, preflight=_launch_preflight_for(name),
        routing_profiles=ADAPTIVE_PROVIDER_REGISTRY[name],
    ))


_ADAPTERS["claude"] = _builtin("claude", _claude_argv, "catalog_pricing")
# An empty catalog price means unknown, never free (ADAPTIVE_OPENAI_PROFILES).
_ADAPTERS["codex"] = _builtin("codex", _codex_argv, "subscription_unpriced")
