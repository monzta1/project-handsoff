#!/usr/bin/env python3
"""Adaptive model routing: which model runs a role, and why.

#284, the first bounded subsystem extracted from `handsoff_lib.py`, chosen
on measured coupling rather than taste. At extraction the monolith was
15,013 lines across 466 top-level definitions; this concern was 36 symbols
with 14 outbound dependencies and 11 inbound callers, the loosest coupling
of any subsystem the ticket names that also has production callers.

The boundary is enforced, not merely drawn. `tests/test_routing_boundary.py`
holds the import allowlist, refuses a definition no routing symbol reaches,
and pins `handsoff_lib`'s re-export surface to exactly the moved set.

**Declared migration coupling.** Each function that needs a primitive from
another layer imports it by name inside its own body:

    from handsoff_config import load_config

Deferred rather than module-level because `handsoff_config` imports this
module -- `DEFAULT_CONFIG` embeds the adaptive routing defaults -- and a
module-level import would close that cycle. Naming the primitives per function
keeps the dependency visible to a reader of that function and lets the
boundary test enumerate it.

Each import names the module that DEFINES the symbol, not the monolith that
re-exports it. That distinction was worth making: eleven of these once read
`from handsoff_lib import ...`, which resolved only through a re-export and
made the dependency look like the whole 8,346-line file. Nine now name the
layer they actually depend on, and two remain on the monolith,
`_canonical_provider_model` and `_agent_assignment`, which is what
`docs/ARCHITECTURE-MIGRATION.md` records as remaining work.
"""
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from handsoff_core import HandsoffError, load_unique_json, status_path


# Adaptive routing is intentionally expressed in capability terms. Model
# metadata is closed over a cited provider catalog so a placeholder cannot
# claim capabilities, limits, or prices that the runner never guaranteed.
ADAPTIVE_ROUTING_TIERS = ("FAST", "STANDARD", "PREMIUM")


ADAPTIVE_RISK_CLASSES = (
    "routine", "elevated", "security_sensitive", "persistence_migration",
    "shared_infrastructure", "irreversible",
)


ADAPTIVE_DEFAULT_RISK_POLICY = {
    "routine": {"min_tier": "FAST", "reviewer_required": False,
                "human_gate_required": False, "irreversible": False},
    "elevated": {"min_tier": "STANDARD", "reviewer_required": True,
                  "human_gate_required": False, "irreversible": False},
    "security_sensitive": {"min_tier": "PREMIUM", "reviewer_required": True,
                            "human_gate_required": True, "irreversible": False},
    "persistence_migration": {"min_tier": "PREMIUM", "reviewer_required": True,
                               "human_gate_required": True, "irreversible": False},
    "shared_infrastructure": {"min_tier": "PREMIUM", "reviewer_required": True,
                               "human_gate_required": True, "irreversible": False},
    "irreversible": {"min_tier": "PREMIUM", "reviewer_required": True,
                      "human_gate_required": True, "irreversible": True},
}


ADAPTIVE_MODEL_CATALOG_SOURCE = "https://platform.claude.com/docs/en/models/overview"


OPENAI_MODEL_CATALOG_SOURCE = "https://developers.openai.com/api/docs/guides/latest-model"


ADAPTIVE_PROFILE_FIELDS = ("adapter", "model", "capabilities", "limits", "pricing", "source")


ADAPTIVE_ESCALATION_TERMINAL_OUTCOMES = ("accepted", "rejected", "human_pause")


ADAPTIVE_ESCALATION_QUESTION_STATES = ("open", "resolved", "withdrawn")


ADAPTIVE_ESCALATION_CLAIM_DECISIONS = ("accept", "reject", "repair")


ADAPTIVE_ESCALATION_CHECK_OUTCOMES = ("pass", "fail", "not_applicable", "error")


ADAPTIVE_BUDGET_FIELDS = ("premium_calls", "repair_rounds", "total_calls", "concurrent_premium_agents")


ADAPTIVE_DEFAULT_BUDGETS = {
    "per_mission": {field: None for field in ADAPTIVE_BUDGET_FIELDS},
    "fleet": {field: None for field in ADAPTIVE_BUDGET_FIELDS},
}


ADAPTIVE_DEFAULT_PROFILES = {
    "FAST": {
        "adapter": "claude", "model": "claude-haiku-4-5-20251001",
        "capabilities": ["text", "vision", "multilingual", "tool_use"],
        "limits": {"context_tokens": 200000, "output_tokens": 64000},
        "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
        "source": ADAPTIVE_MODEL_CATALOG_SOURCE,
    },
    "STANDARD": {
        "adapter": "claude", "model": "claude-sonnet-5",
        "capabilities": ["text", "vision", "multilingual", "tool_use", "extended_thinking"],
        "limits": {"context_tokens": 1000000, "output_tokens": 128000},
        "pricing": {"input_per_mtok": 2.0, "output_per_mtok": 10.0},
        "source": ADAPTIVE_MODEL_CATALOG_SOURCE,
    },
    "PREMIUM": {
        "adapter": "claude", "model": "claude-opus-5",
        "capabilities": ["text", "vision", "multilingual", "tool_use", "extended_thinking"],
        "limits": {"context_tokens": 1000000, "output_tokens": 128000},
        "pricing": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
        "source": ADAPTIVE_MODEL_CATALOG_SOURCE,
    },
}


ADAPTIVE_OPENAI_PROFILES = {
    "PREMIUM": {
        "adapter": "codex", "model": "gpt-6-astra",
        "capabilities": ["text", "vision", "tool_use", "extended_thinking"],
        "limits": {"context_tokens": 272000, "output_tokens": 128000},
        # ChatGPT-account Codex usage does not expose an API price. Empty
        # means unknown, never free.
        "pricing": {}, "source": OPENAI_MODEL_CATALOG_SOURCE,
    },
}


def adaptive_catalog_profile(adapter: str, model: str) -> tuple[str, dict] | None:
    for tier, profile in ADAPTIVE_DEFAULT_PROFILES.items():
        if profile["adapter"] == adapter and profile["model"] == model:
            return tier, deepcopy(profile)
    for tier, profile in ADAPTIVE_OPENAI_PROFILES.items():
        if profile["adapter"] == adapter and profile["model"] == model:
            return tier, deepcopy(profile)
    return None


def validate_adaptive_routing_profiles(value: object) -> dict:
    """Validate a closed, source-cited routing catalog and return a copy."""
    from handsoff_config import DEFAULT_AGENT_MODEL, SELECTABLE_AGENT_ADAPTERS  # #284 deferred: see module docstring
    if not isinstance(value, dict):
        raise HandsoffError("routing_profiles must be a table")
    unknown = set(value) - set(ADAPTIVE_ROUTING_TIERS)
    if unknown:
        raise HandsoffError("routing_profiles has unknown tier(s): " + ", ".join(sorted(unknown)))
    catalog = {
        (profile["adapter"], profile["model"]): (tier, profile)
        for source in (ADAPTIVE_DEFAULT_PROFILES, ADAPTIVE_OPENAI_PROFILES)
        for tier, profile in source.items()
    }
    result = {}
    for tier in ADAPTIVE_ROUTING_TIERS:
        raw = value.get(tier, ADAPTIVE_DEFAULT_PROFILES[tier])
        if not isinstance(raw, dict):
            raise HandsoffError(f"routing_profiles.{tier} must be a table")
        extra = set(raw) - set(ADAPTIVE_PROFILE_FIELDS)
        if extra:
            raise HandsoffError(f"routing_profiles.{tier} has unknown fields: {', '.join(sorted(extra))}")
        adapter, model = raw.get("adapter"), raw.get("model")
        if adapter not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError(f"routing_profiles.{tier}.adapter must be codex or claude")
        if not isinstance(model, str) or not model.strip():
            raise HandsoffError(f"routing_profiles.{tier}.model must be a non-empty string")
        model = model.strip()
        if model == DEFAULT_AGENT_MODEL:
            metadata = {key: raw.get(key) for key in set(raw) - {"adapter", "model"}}
            if any(value not in ({}, [], None) for value in metadata.values()):
                raise HandsoffError(
                    f"routing_profiles.{tier} default deferral cannot claim capabilities, limits, pricing, or source"
                )
            result[tier] = {"adapter": adapter, "model": model, "capabilities": [],
                            "limits": {}, "pricing": {}, "source": None}
            continue
        entry = catalog.get((adapter, model))
        if entry is None or entry[0] != tier:
            raise HandsoffError(
                f"routing_profiles.{tier} must name its documented cost-bound adapter/model pair"
            )
        _documented_tier, documented = entry
        profile = deepcopy(documented)
        profile.update(raw)
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or not all(isinstance(x, str) and x.strip() for x in capabilities):
            raise HandsoffError(f"routing_profiles.{tier}.capabilities must be a non-empty array of strings")
        limits = profile.get("limits")
        if not isinstance(limits, dict) or not limits or any(not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in limits.items()):
            raise HandsoffError(f"routing_profiles.{tier}.limits must map names to non-negative integers")
        pricing = profile.get("pricing")
        if not isinstance(pricing, dict) or (pricing and set(pricing) != {"input_per_mtok", "output_per_mtok"}) \
                or any(not isinstance(number, (int, float)) or isinstance(number, bool) or number < 0
                       for number in pricing.values()):
            raise HandsoffError(f"routing_profiles.{tier}.pricing must be empty or contain non-negative input_per_mtok and output_per_mtok")
        normalized = {"adapter": adapter, "model": model,
                      "capabilities": sorted(set(x.strip() for x in capabilities)),
                      "limits": dict(limits), "pricing": dict(pricing), "source": profile.get("source")}
        expected = {**documented, "capabilities": sorted(documented["capabilities"])}
        if normalized != expected:
            raise HandsoffError(f"routing_profiles.{tier} metadata does not match its documented source")
        result[tier] = normalized
    return result


def adaptive_routing_profiles(cfg: dict | None = None) -> dict:
    """Return the validated routing catalog suitable for audit/status output."""
    return validate_adaptive_routing_profiles((cfg or {}).get("adaptive_routing_profiles", ADAPTIVE_DEFAULT_PROFILES))


def validate_adaptive_routing_budgets(value: object) -> dict:
    """Validate finite per-mission and fleet safety budgets.

    ``None`` means that a dimension is not capped.  Counters are supplied by
    the mission coordinator, keeping this policy independent of adapters.
    """
    if not isinstance(value, dict):
        raise HandsoffError("routing_budgets must be a table")
    unknown = set(value) - {"per_mission", "fleet"}
    if unknown:
        raise HandsoffError("routing_budgets has unknown scope(s): " + ", ".join(sorted(unknown)))
    result = deepcopy(ADAPTIVE_DEFAULT_BUDGETS)
    for scope in ("per_mission", "fleet"):
        raw = value.get(scope, {})
        if not isinstance(raw, dict):
            raise HandsoffError(f"routing_budgets.{scope} must be a table")
        unknown_fields = set(raw) - set(ADAPTIVE_BUDGET_FIELDS)
        if unknown_fields:
            raise HandsoffError(f"routing_budgets.{scope} has unknown fields: {', '.join(sorted(unknown_fields))}")
        for field, limit in raw.items():
            if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
                raise HandsoffError(f"routing_budgets.{scope}.{field} must be a non-negative integer or null")
            result[scope][field] = limit
    return result


def adaptive_routing_budgets(cfg: dict | None = None) -> dict:
    return validate_adaptive_routing_budgets((cfg or {}).get("adaptive_routing_budgets", ADAPTIVE_DEFAULT_BUDGETS))


def evaluate_adaptive_budget(cfg: dict | None = None, *, mission_usage=None, fleet_usage=None,
                             deterministic_checks_complete=False) -> dict:
    """Return a safe continuation decision after deterministic checks drain.

    The first exhausted dimension is reported distinctly; no PREMIUM call is
    authorized while checks are still in flight.
    """
    budgets = adaptive_routing_budgets(cfg)
    usage = {"per_mission": dict(mission_usage or {}), "fleet": dict(fleet_usage or {})}
    if not deterministic_checks_complete:
        return {"state": "paused", "reason": "deterministic_checks_in_flight", "scope": None, "field": None}
    for scope in ("per_mission", "fleet"):
        for field in ADAPTIVE_BUDGET_FIELDS:
            limit = budgets[scope][field]
            used = usage[scope].get(field, 0)
            if not isinstance(used, int) or isinstance(used, bool) or used < 0:
                raise HandsoffError(f"{scope} usage {field} must be a non-negative integer")
            if limit is not None and used >= limit:
                return {"state": "paused", "reason": f"{scope}_{field}_exhausted",
                        "scope": scope, "field": field, "used": used, "limit": limit}
    return {"state": "allowed", "reason": "within_budget", "scope": None, "field": None}


def validate_adaptive_risk_policy(value: object) -> dict:
    """Validate the closed, provider-independent mission risk policy."""
    if not isinstance(value, dict) or set(value) != set(ADAPTIVE_RISK_CLASSES):
        raise HandsoffError("risk_policy must contain exactly the six adaptive risk classes")
    result = {}
    for risk_class in ADAPTIVE_RISK_CLASSES:
        row = value[risk_class]
        if not isinstance(row, dict) or set(row) != {"min_tier", "reviewer_required", "human_gate_required", "irreversible"}:
            raise HandsoffError(f"risk_policy.{risk_class} has invalid fields")
        if row["min_tier"] not in ADAPTIVE_ROUTING_TIERS:
            raise HandsoffError(f"risk_policy.{risk_class}.min_tier is invalid")
        if any(not isinstance(row[field], bool) for field in ("reviewer_required", "human_gate_required", "irreversible")):
            raise HandsoffError(f"risk_policy.{risk_class} gate fields must be booleans")
        result[risk_class] = dict(row)
    return result


def adaptive_risk_policy(cfg: dict | None = None) -> dict:
    """Return the exact six-row risk policy used by adaptive routing."""
    return validate_adaptive_risk_policy((cfg or {}).get("risk_policy", ADAPTIVE_DEFAULT_RISK_POLICY))


def classify_adaptive_risk(risk_class: str) -> str:
    """Validate and return one canonical mission risk class."""
    if not isinstance(risk_class, str) or risk_class not in ADAPTIVE_RISK_CLASSES:
        raise HandsoffError("risk_class must be one of " + ", ".join(ADAPTIVE_RISK_CLASSES))
    return risk_class


def route_adaptive_profile(cfg: dict | None = None, *, required_capabilities=(), minimum_tier=None,
                           available_tiers=None, available_adapters=None, risk_class="routine",
                           mission_usage=None, fleet_usage=None,
                           deterministic_checks_complete=False) -> dict:
    """Select the lowest qualified tier, or return an auditable pause.

    Review and human approvals are obligations on their native workflow
    phases, not prerequisites for launching the worker that performs the job.
    """
    from handsoff_config import DEFAULT_AGENT_MODEL, DEFAULT_MODEL_POLICY, SELECTABLE_AGENT_ADAPTERS, model_policy_allows, validate_model_policy  # #284 deferred: see module docstring
    profiles = adaptive_routing_profiles(cfg)
    model_policy = validate_model_policy((cfg or {}).get("model_policy", DEFAULT_MODEL_POLICY))
    risk_class = classify_adaptive_risk(risk_class)
    policy = adaptive_risk_policy(cfg)
    risk = policy[risk_class]
    budget = evaluate_adaptive_budget(cfg, mission_usage=mission_usage, fleet_usage=fleet_usage,
                                      deterministic_checks_complete=deterministic_checks_complete)
    required_floor = risk["min_tier"]
    if minimum_tier is not None and minimum_tier not in ADAPTIVE_ROUTING_TIERS:
        raise HandsoffError(f"minimum_tier must be one of {', '.join(ADAPTIVE_ROUTING_TIERS)}")
    if minimum_tier is None or ADAPTIVE_ROUTING_TIERS.index(minimum_tier) < ADAPTIVE_ROUTING_TIERS.index(required_floor):
        minimum_tier = required_floor
    required = sorted(set(required_capabilities))
    if any(not isinstance(capability, str) or not capability.strip() for capability in required):
        raise HandsoffError("required_capabilities must contain non-empty strings")
    if minimum_tier is not None and minimum_tier not in ADAPTIVE_ROUTING_TIERS:
        raise HandsoffError(f"minimum_tier must be one of {', '.join(ADAPTIVE_ROUTING_TIERS)}")
    allowed = set(ADAPTIVE_ROUTING_TIERS if available_tiers is None else available_tiers)
    adapters = set(SELECTABLE_AGENT_ADAPTERS if available_adapters is None else available_adapters)
    start = ADAPTIVE_ROUTING_TIERS.index(minimum_tier) if minimum_tier else 0
    candidates = []
    for tier in ADAPTIVE_ROUTING_TIERS[start:]:
        tier_profiles = [profiles[tier]]
        alternate = ADAPTIVE_OPENAI_PROFILES.get(tier)
        if alternate and (alternate["adapter"], alternate["model"]) != \
                (profiles[tier]["adapter"], profiles[tier]["model"]):
            tier_profiles.append(alternate)
        for preference, profile in enumerate(tier_profiles):
            missing = sorted(set(required) - set(profile["capabilities"]))
            if tier in allowed and profile["adapter"] in adapters and profile["model"] != DEFAULT_AGENT_MODEL \
                    and model_policy_allows(model_policy, profile["adapter"], profile["model"]) and not missing:
                # Configured profile wins ties; hard constraints can expose an
                # equivalent documented cross-vendor candidate.
                candidates.append((ADAPTIVE_ROUTING_TIERS.index(tier), preference, tier, profile))
    routing_metadata = {"risk_class": risk_class, "risk_policy": deepcopy(risk),
                        "model_policy": deepcopy(model_policy),
                        "reviewer_required": risk["reviewer_required"],
                        "human_gate_required": risk["human_gate_required"]}
    if budget["state"] != "allowed":
        return {"state": "paused", "tier": None, "profile": None,
                "required_capabilities": required, "reason": budget["reason"],
                "budget": budget, **routing_metadata}
    if candidates:
        _, _, tier, profile = min(candidates)
        return {"state": "selected", "tier": tier, "profile": deepcopy(profile),
                "required_capabilities": required, "reason": "qualified_profile", **routing_metadata}
    available = [tier for tier in ADAPTIVE_ROUTING_TIERS if tier in allowed and any(
        profile["adapter"] in adapters and model_policy_allows(model_policy, profile["adapter"], profile["model"])
        for profile in ([profiles[tier]] + ([ADAPTIVE_OPENAI_PROFILES[tier]]
                                            if tier in ADAPTIVE_OPENAI_PROFILES else []))
    )]
    reason_kind = "required_capability_unavailable" if not any(
        not (set(required) - set(profiles[tier]["capabilities"])) for tier in available
    ) else "required_tier_unavailable"
    return {"state": "paused", "tier": None, "profile": None,
            "required_capabilities": required, "reason": reason_kind,
            "detail": "No available configured profile satisfies the required policy and hard model constraints",
            "model_policy": deepcopy(model_policy),
            **routing_metadata}


def _adaptive_model_reconciliation(session: dict) -> dict:
    """Reconcile the immutable routed choice with provider-reported reality."""
    from handsoff_lib import _canonical_provider_model  # #284 deferred: see module docstring
    route = session.get("adaptive_routing") if isinstance(session.get("adaptive_routing"), dict) else None
    if route is None:
        return {"consistency": "not_applicable", "effective_tier": None, "effective_profile": None}
    selected_model = route.get("model")
    reported_model = _canonical_provider_model(session.get("reported_model"))
    if reported_model is None:
        return {"consistency": "pending_verification", "effective_tier": route.get("tier"),
                "effective_profile": route.get("profile")}
    if reported_model == selected_model:
        return {"consistency": "matched", "effective_tier": route.get("tier"),
                "effective_profile": route.get("profile")}
    for tier, profile in ADAPTIVE_DEFAULT_PROFILES.items():
        if profile["model"] == reported_model and profile["adapter"] == session.get("adapter"):
            return {"consistency": "mismatch", "effective_tier": tier,
                    "effective_profile": deepcopy(profile)}
    # Unknown provider models count as PREMIUM so a mismatch can never
    # bypass the strongest safety budget.
    return {"consistency": "mismatch", "effective_tier": "PREMIUM", "effective_profile": None}


def adaptive_usage(status: dict) -> dict:
    """Derive budget counters only from the run's committed ledgers."""
    from handsoff_schema import AGENT_SESSION_LIVE_STATES  # #284 deferred: see module docstring
    sessions = (status or {}).get("agent_sessions") or {}
    routed = [session for session in sessions.values()
              if isinstance(session, dict) and isinstance(session.get("adaptive_routing"), dict)]
    repairs = sum(1 for attempt in ((status or {}).get("review_attempts") or [])
                  if isinstance(attempt, dict) and attempt.get("disposition") == "changes_requested")
    return {
        "premium_calls": sum(_adaptive_model_reconciliation(session)["effective_tier"] == "PREMIUM"
                             for session in routed),
        "repair_rounds": repairs,
        "total_calls": len(routed),
        "concurrent_premium_agents": sum(
            session.get("state") in AGENT_SESSION_LIVE_STATES
            and _adaptive_model_reconciliation(session)["effective_tier"] == "PREMIUM" for session in routed
        ),
    }


def adaptive_fleet_usage(root: Path, *, current_status: dict | None = None,
                         registry: Path | None = None) -> dict:
    """Sum adaptive counters from every run registered with Fleet.

    The registry selects roots; each root's status ledger remains the source
    of truth. Unreadable or vanished roots are skipped conservatively.
    """
    from handsoff_config import load_config  # #284 deferred: see module docstring
    registry = registry or Path(os.environ.get("HANDSOFF_FLEET_REGISTRY", "~/.handsoff/projects.json")).expanduser()
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
        projects = payload.get("projects", []) if isinstance(payload, dict) else []
    except (OSError, ValueError):
        projects = []
    roots = {str(Path(item.get("root", "")).expanduser().resolve()) for item in projects
             if isinstance(item, dict) and isinstance(item.get("root"), str) and item.get("root")}
    roots.add(str(root.resolve()))
    total = {field: 0 for field in ADAPTIVE_BUDGET_FIELDS}
    for registered in sorted(roots):
        candidate = Path(registered)
        try:
            status = current_status if candidate == root.resolve() and current_status is not None else \
                load_unique_json(status_path(candidate, load_config(candidate)))
            usage = adaptive_usage(status)
        except (HandsoffError, OSError, ValueError):
            continue
        for field in ADAPTIVE_BUDGET_FIELDS:
            total[field] += usage[field]
    return total


def validate_session_adaptive_routing(value: object) -> dict:
    required = {"risk_class", "tier", "adapter", "model", "profile", "reason",
                "reviewer_required", "human_gate_required"}
    if not isinstance(value, dict) or set(value) != required:
        raise HandsoffError("session adaptive_routing has invalid fields")
    risk_class = classify_adaptive_risk(value.get("risk_class"))
    tier = value.get("tier")
    if tier not in ADAPTIVE_ROUTING_TIERS:
        raise HandsoffError("session adaptive_routing tier is invalid")
    profiles = validate_adaptive_routing_profiles({tier: value.get("profile")})
    profile = profiles[tier]
    if value.get("adapter") != profile["adapter"] or value.get("model") != profile["model"]:
        raise HandsoffError("session adaptive_routing pair does not match its profile")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise HandsoffError("session adaptive_routing reason must be non-empty")
    if any(not isinstance(value.get(field), bool) for field in ("reviewer_required", "human_gate_required")):
        raise HandsoffError("session adaptive_routing obligations must be booleans")
    return {**deepcopy(value), "risk_class": risk_class, "profile": profile}


def adaptive_deployment_approval_required(status: dict, cfg: dict) -> bool:
    risk_class = status.get("risk_class") if isinstance(status, dict) else None
    adaptive = bool(risk_class and adaptive_risk_policy(cfg)[classify_adaptive_risk(risk_class)]["human_gate_required"])
    return bool(cfg.get("deployment_requires_explicit_approval", True) or adaptive)


def adaptive_routing_snapshot(status: dict, cfg: dict | None = None, host: dict | None = None,
                              events: list[dict] | None = None) -> dict:
    """Build the total dashboard shape from recorded routing/session facts."""
    from handsoff_schema import AGENT_SESSION_LIVE_STATES, PHASES  # #284 deferred: see module docstring
    from handsoff_projection import actor_family  # #284 deferred: see module docstring
    from handsoff_lib import _agent_assignment  # #284 deferred: see module docstring
    effective_cfg = dict(cfg or {})
    if isinstance(status.get("model_policy"), dict):
        effective_cfg["model_policy"] = status["model_policy"]
    sessions = (status or {}).get("agent_sessions") or {}
    routed = [session for session in sessions.values()
              if isinstance(session, dict) and isinstance(session.get("adaptive_routing"), dict)]
    calls = {tier: 0 for tier in ADAPTIVE_ROUTING_TIERS}
    tokens_in = tokens_out = tokens_total = 0
    known_cost = 0.0
    cost_reported = False
    duration_ms = 0
    duration_reported = False
    for session in routed:
        route = session["adaptive_routing"]
        calls[route["tier"]] += 1
        usage = session.get("usage") or {}
        if usage.get("source") == "adapter":
            token_in = usage.get("tokens_in") if isinstance(usage.get("tokens_in"), int) else 0
            token_out = usage.get("tokens_out") if isinstance(usage.get("tokens_out"), int) else 0
            tokens_in += token_in
            tokens_out += token_out
            tokens_total += usage.get("tokens_total") if isinstance(usage.get("tokens_total"), int) else token_in + token_out
            effective_profile = _adaptive_model_reconciliation(session)["effective_profile"]
            pricing = (effective_profile or {}).get("pricing", {})
            if isinstance(pricing.get("input_per_mtok"), (int, float)) and isinstance(pricing.get("output_per_mtok"), (int, float)):
                known_cost += token_in * pricing["input_per_mtok"] / 1_000_000
                known_cost += token_out * pricing["output_per_mtok"] / 1_000_000
                cost_reported = True
        try:
            start = datetime.fromisoformat(session.get("running_at") or session["started_at"])
            end = datetime.fromisoformat(session["ended_at"])
            duration_ms += max(0, round((end - start).total_seconds() * 1000))
            duration_reported = True
        except (KeyError, TypeError, ValueError):
            pass
    risk_class = status.get("risk_class")
    latest = routed[-1]["adaptive_routing"] if routed else {}
    if not latest and risk_class:
        ready = route_adaptive_profile(
            effective_cfg, required_capabilities=("text", "tool_use"),
            risk_class=risk_class, deterministic_checks_complete=True,
        )
        if ready.get("state") == "selected":
            latest = {
                "tier": ready["tier"],
                "adapter": ready["profile"]["adapter"],
                "model": ready["profile"]["model"],
            }
    escalation = status.get("adaptive_escalation") if isinstance(status.get("adaptive_escalation"), dict) else {}
    repairs = sum(1 for attempt in (status.get("review_attempts") or [])
                  if isinstance(attempt, dict) and attempt.get("disposition") == "changes_requested")
    selections = [_agent_assignment(session) for session in sessions.values()
                  if isinstance(session, dict)]
    live_managed = any(session.get("state") in AGENT_SESSION_LIVE_STATES
                       for session in sessions.values() if isinstance(session, dict))
    phase = int(status.get("phase_number", 0) or 0)
    host_role = {1: "architect", 3: "supervisor", 4: "implementer", 6: "implementer",
                 7: "supervisor", 8: "supervisor"}.get(phase)
    host = host if isinstance(host, dict) else {}
    model_class = host.get("model_class") if isinstance(host.get("model_class"), str) else None
    events = [event for event in (events or []) if isinstance(event, dict)]
    implemented_by = status.get("implemented_by")
    managed_phase4 = any(
        item.get("role") == "implementer" and item.get("phase_number") == 4
        for item in selections
    )
    if model_class and isinstance(implemented_by, str) and implemented_by and not managed_phase4:
        phase4 = next((event for event in events
                       if event.get("kind") == "phase_advanced" and event.get("phase_number") == 4), None)
        phase5 = next((event for event in events
                       if event.get("kind") == "phase_advanced" and event.get("phase_number") == 5), None)
        family = actor_family(implemented_by) or host.get("family") or "host"
        selections.append({
            "session_id": "host-phase-4", "role": "implementer", "actor": implemented_by,
            "purpose": PHASES[4], "phase_number": 4,
            "started_at": (phase4 or {}).get("at"), "ended_at": (phase5 or {}).get("at"),
            "adaptive": False, "tier": None, "adapter": family, "model": model_class,
            "requested_model": None, "model_source": "host_runtime",
            "model_consistency": "not_applicable", "reason": "host_runtime",
            "state": "completed" if phase5 else "running",
        })
    if host_role and model_class and not live_managed and status.get("status") not in {"complete", "blocked"} \
            and not isinstance(status.get("human_pause"), dict):
        family = host.get("family") if host.get("family") in {"codex", "claude"} else "host"
        selections.append({
            "session_id": f"host-phase-{phase}", "role": host_role,
            "actor": host.get("actor") or f"{family}-host", "purpose": PHASES.get(phase, "Host work"),
            "phase_number": phase, "started_at": status.get("updated_at"), "ended_at": None,
            "adaptive": False, "tier": None, "adapter": family, "model": model_class,
            "requested_model": None, "model_source": "host_runtime",
            "model_consistency": "not_applicable", "reason": "host_runtime",
            "state": "running", "budget_decision": None, "usage": None,
        })
    selections.sort(key=lambda item: (item.get("started_at") is None, item.get("started_at") or "",
                                      item.get("session_id") or ""))
    return {
        # `used` means the run is governed by adaptive routing. Calls remain
        # separately auditable in calls_by_tier and selections, so a newly
        # initialized zero-call run is visibly ready without inventing use.
        "used": bool(risk_class or routed), "risk_class": risk_class,
        "tier": latest.get("tier"), "adapter": latest.get("adapter"), "model": latest.get("model"),
        "token_usage": {"input": tokens_in, "output": tokens_out, "total": tokens_total},
        "estimated_cost": round(known_cost, 6) if cost_reported else None,
        "duration_ms": duration_ms if duration_reported else None,
        "escalation_reason": escalation.get("reason"), "repair_rounds": repairs,
        "review_rounds": len(status.get("review_attempts") or []),
        "active_premium_scope": "mission" if any(
            session.get("state") in AGENT_SESSION_LIVE_STATES
            and _adaptive_model_reconciliation(session)["effective_tier"] == "PREMIUM" for session in routed) else None,
        "outcome": escalation.get("outcome"), "calls_by_tier": calls,
        "selections": selections,
        "pause": ({"reason": escalation.get("reason"), "scope": "mission"}
                  if escalation.get("outcome") == "human_pause" else None),
    }


def _adaptive_required_text(value, field, maximum=512):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise HandsoffError(f"{field} must be a non-empty string of at most {maximum} characters")
    return value


def _adaptive_mission_binding(mission_id, acceptance_hash):
    return {"mission_id": _adaptive_required_text(mission_id, "mission_id"),
            "acceptance_hash": _adaptive_required_text(acceptance_hash, "acceptance_hash")}


def validate_adaptive_check_plan(checks, *, mission_id, acceptance_hash):
    """Validate and deterministically order checks before model escalation."""
    binding = _adaptive_mission_binding(mission_id, acceptance_hash)
    if not isinstance(checks, (list, tuple)) or len(checks) > 32:
        raise HandsoffError("deterministic checks must contain at most 32 items")
    result = []
    seen = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise HandsoffError(f"deterministic check {index} must be a record")
        if set(check) - {"check_id", "command", "applies_to", "order"}:
            raise HandsoffError(f"deterministic check {index} has unknown fields")
        check_id = _adaptive_required_text(check.get("check_id"), "check_id", 96)
        if check_id in seen:
            raise HandsoffError("deterministic check ids must be unique")
        seen.add(check_id)
        command = _adaptive_required_text(check.get("command"), "command", 1024)
        applies_to = check.get("applies_to", "all")
        if not isinstance(applies_to, str) or not applies_to.strip():
            raise HandsoffError("deterministic check applies_to must be a non-empty string")
        order = check.get("order", index)
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            raise HandsoffError("deterministic check order must be a non-negative integer")
        result.append({**binding, "check_id": check_id, "command": command,
                       "applies_to": applies_to.strip(), "order": order})
    return sorted(result, key=lambda row: (row["order"], row["check_id"]))


def record_adaptive_check(check, *, outcome, evidence=None, detail=""):
    """Create the auditable result for one already-bound deterministic check."""
    if not isinstance(check, dict) or not check.get("mission_id") or not check.get("acceptance_hash"):
        raise HandsoffError("deterministic check is missing mission binding")
    if outcome not in ADAPTIVE_ESCALATION_CHECK_OUTCOMES:
        raise HandsoffError("invalid deterministic check outcome")
    record = dict(check)
    record.update({"record_type": "deterministic_check", "outcome": outcome,
                   "evidence": list(evidence or []), "detail": detail})
    return record


def adaptive_escalation_records(*, mission_id, acceptance_hash, implementer, reviewer,
                                supporting_evidence=(), unresolved_question=None):
    """Return separate, mission-bound claim/evidence/question audit records."""
    binding = _adaptive_mission_binding(mission_id, acceptance_hash)
    records = []
    for role, claim in (("implementer", implementer), ("reviewer", reviewer)):
        if not isinstance(claim, dict):
            raise HandsoffError(f"{role} claim must be a record")
        decision = claim.get("decision")
        if decision not in ADAPTIVE_ESCALATION_CLAIM_DECISIONS:
            raise HandsoffError(f"{role} claim decision is invalid")
        records.append({**binding, "record_type": f"{role}_claim", "role": role,
                        "claim_id": _adaptive_required_text(claim.get("claim_id"), "claim_id", 96),
                        "actor": _adaptive_required_text(claim.get("actor"), "actor", 128),
                        "decision": decision,
                        "summary": _adaptive_required_text(claim.get("summary"), "summary")})
    for evidence in supporting_evidence:
        if not isinstance(evidence, dict):
            raise HandsoffError("supporting evidence must be records")
        records.append({**binding, "record_type": "supporting_evidence",
                        "evidence_id": _adaptive_required_text(evidence.get("evidence_id"), "evidence_id", 96),
                        "kind": _adaptive_required_text(evidence.get("kind"), "kind", 64),
                        "detail": _adaptive_required_text(evidence.get("detail"), "detail")})
    if unresolved_question is not None:
        records.append({**binding, "record_type": "unresolved_question",
                        "question_id": _adaptive_required_text(unresolved_question.get("question_id"), "question_id", 96),
                        "question": _adaptive_required_text(unresolved_question.get("question"), "question"),
                        "state": unresolved_question.get("state", "open")})
        if records[-1]["state"] not in ADAPTIVE_ESCALATION_QUESTION_STATES:
            raise HandsoffError("unresolved question state is invalid")
    return records


def bound_adaptive_escalation(*, disagreement_rounds=0, repair_rounds=0,
                              max_disagreement_rounds=2, max_repair_rounds=2,
                              human_decision=None):
    """Return a terminal decision or a human pause; never permit an open loop."""
    for value, name in ((disagreement_rounds, "disagreement_rounds"),
                        (repair_rounds, "repair_rounds"),
                        (max_disagreement_rounds, "max_disagreement_rounds"),
                        (max_repair_rounds, "max_repair_rounds")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HandsoffError(f"{name} must be a non-negative integer")
    if max_disagreement_rounds == 0 or max_repair_rounds == 0:
        raise HandsoffError("escalation round limits must be positive")
    if human_decision in ADAPTIVE_ESCALATION_TERMINAL_OUTCOMES:
        outcome = human_decision
        reason = "human_decision"
    elif disagreement_rounds >= max_disagreement_rounds:
        outcome, reason = "human_pause", "disagreement_limit"
    elif repair_rounds >= max_repair_rounds:
        outcome, reason = "human_pause", "repair_limit"
    else:
        outcome, reason = None, "continue"
    return {"state": "terminal" if outcome else "active", "outcome": outcome,
            "reason": reason, "disagreement_rounds": disagreement_rounds,
            "repair_rounds": repair_rounds, "max_disagreement_rounds": max_disagreement_rounds,
            "max_repair_rounds": max_repair_rounds}
