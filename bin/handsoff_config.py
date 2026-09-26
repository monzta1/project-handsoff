#!/usr/bin/env python3
"""Handsoff configuration: defaults, loading and the model policy.

#284 stage 2. Forty-five symbols, sitting ABOVE model routing rather than
below it: `DEFAULT_CONFIG` embeds the adaptive routing defaults, which is
why the first attempt at a core that owned configuration did not close.
The layering the reference graph produced is therefore

    handsoff_core  ->  handsoff_routing  ->  handsoff_config

and each imports the one before it at module level, with no cycle and no
deferred imports.

`tomllib` keeps its guard. It is absent before Python 3.11, and
`load_config` raises a named error in that case rather than failing on an
attribute of None. A bare import here would kill every importer of this
module on an older interpreter, which is the mistake stage 1 made with
`fcntl` and had to correct.
"""
from __future__ import annotations

import math
import re
import shlex
from copy import deepcopy
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

from handsoff_core import HandsoffError
from handsoff_routing import (
    ADAPTIVE_DEFAULT_BUDGETS,
    ADAPTIVE_DEFAULT_PROFILES,
    ADAPTIVE_DEFAULT_RISK_POLICY,
    validate_adaptive_risk_policy,
    validate_adaptive_routing_budgets,
    validate_adaptive_routing_profiles,
)



MAX_FALLBACK_PROFILES = 8


DEFAULT_MAX_FAILOVERS_PER_ROLE = 2


# #35: how many design-review attempts (approve or request-changes, every
# record-design-review counts) a run may consume on its own before the
# Pilot has to authorize each further attempt one at a time.
#: #320: the nearest-rank p90 of 100 measured product runs. Attempts
#: distribute 24/49/17/6/1/1/2 at 1,2,3,4,5,6,8. A limit of 2 interrupts
#: 27% of runs and a limit of 3 interrupts 10%. The gate at 2 saved no
#: tokens: 27 runs went past it, so authorization was granted and the
#: attempt ran anyway. Kept rather than removed because four runs needed
#: five to eight attempts, where the design is wrong and a human should look.
DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS = 3


# Hard ceilings for one managed Codex session.  These are deliberately
# conservative: a role that cannot finish inside its allowance must return a
# bounded handoff or stop for the Pilot, never silently consume an unlimited
# rollout.  Projects may lower or raise an individual role under
# [agent_budget], but may not disable the ceiling.
DEFAULT_AGENT_TOKEN_BUDGETS = {
    "architect": 40_000,
    "supervisor": 24_000,
    "implementer": 80_000,
    "reviewer": 80_000,
}


MIN_AGENT_TOKEN_BUDGET = 8_000


MAX_AGENT_TOKEN_BUDGET = 500_000


TICKET_STATES = frozenset({"done", "in_progress", "not_started", "blocked"})


DESIGN_EVIDENCE_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


MAX_DESIGN_EVIDENCE_ENTRIES = 16


BRIEFING_CONFIG_KEYS = frozenset({"index", "root"})


#: #39: the crew a role gets when handsoff.toml does not name one (or still
#: carries the legacy "configure-me" placeholder). Architect, Supervisor,
#: and Implementer share one premium reasoning profile: design needs the
#: strongest reasoning available, and the operator keeps the same Opus
#: profile for orchestration and implementation so one run has one
#: consistent voice. The Reviewer runs on an independent provider family
#: so the critique never comes from the model being critiqued. An explicit
#: "auto" is NOT part of this table: it keeps the older first-installed
#: auto-detect path (DEFAULT_AGENT_PREFERENCE) on purpose.
RECOMMENDED_CREW = {
    "architect": {"adapter": "claude", "model": "claude-opus-5"},
    "supervisor": {"adapter": "claude", "model": "claude-opus-5"},
    "implementer": {"adapter": "claude", "model": "claude-opus-5"},
    "reviewer": {"adapter": "codex", "model": "default"},
}


RECOMMENDED_PROFILE_SOURCE = "recommended"


EXPLICIT_PROFILE_SOURCE = "explicit"


RUNNER_DEFAULT_PROFILE_SOURCE = "runner_default"


#: #37: the optional economical follow-up reviewer profile. Both keys
#: ([agents].reviewer_followup and [models].reviewer_followup) present
#: enables tiering; both absent reproduces the single-profile behavior
#: exactly; exactly one present is a config error. It is a cost knob, not
#: a gate, so it is deliberately NOT in GOVERNANCE_CONFIG_KEYS.
FOLLOWUP_REVIEWER_KEY = "reviewer_followup"


DEFAULT_SMALL_FIX_MAX_CRITERIA = 3


DEFAULT_SMALL_FIX_MAX_CHANGED_LINES = 200


DEFAULT_SMALL_FIX_MAX_FILES = 6


DEFAULT_MODEL_POLICY = {
    "allowed_adapters": ["codex", "claude"],
    "denied_models": [],
    "quota_substitution": True,
}


DEFAULT_CONFIG = {
    "adaptive_routing_profiles": deepcopy(ADAPTIVE_DEFAULT_PROFILES),
    "adaptive_routing_budgets": deepcopy(ADAPTIVE_DEFAULT_BUDGETS),
    "risk_policy": deepcopy(ADAPTIVE_DEFAULT_RISK_POLICY),
    "model_policy": deepcopy(DEFAULT_MODEL_POLICY),
    "reviewer_isolation": {"compatibility_mode": False, "compatibility_approved": False},
    "execution_profile": "safe",
    "logo": None,
    "status_file": "handsoff-status.json",
    "acceptance_file": "handsoff-acceptance.json",
    "event_log": "handsoff-events.jsonl",
    "verification_log": "handsoff-verifications.jsonl",
    "max_design_rounds": 3,
    "auto_handoff": True,
    "max_review_rounds": 3,
    "stall_minutes": 10,
    "max_autonomous_design_reviews": DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS,
    "small_fix_max_criteria": DEFAULT_SMALL_FIX_MAX_CRITERIA,
    "small_fix_max_changed_lines": DEFAULT_SMALL_FIX_MAX_CHANGED_LINES,
    "small_fix_max_files": DEFAULT_SMALL_FIX_MAX_FILES,
    "require_live_verification": True,
    "deployment_requires_explicit_approval": True,
    # #159: false lets a project waive the Pilot's design click; the
    # independent design review stays mandatory either way.
    "require_design_approval": True,
    # #165 #167 #166: workflow features, each a switch under [features] that
    # Mission Control's settings dialog edits. Defaults live in FEATURES.
    "features": {},
    "check_commands": [],
    "briefing": None,
    "digest_ignore": [],
    "implementer_commands": [],
    "documentation": {"files": [], "exclude": []},
    # #124: exact browser origins (scheme://host[:port]) that may drive the
    # run dashboard through a tunnel or private network; loopback is always
    # accepted. Fleet reads HANDSOFF_PUBLIC_ORIGINS instead (no project).
    "public_origins": [],
    "live_check_commands": [],
    "check_timeout_seconds": 600,
    "tickets": [],
    "design_evidence": [],
    "regressions": [],
    "regression_gate": {
        "approval_timeout_minutes": 30, "launch_window_minutes": 10,
        "full_regression_major_only": True,
    },
    "agents": {role: profile["adapter"] for role, profile in RECOMMENDED_CREW.items()},
    "models": {role: profile["model"] for role, profile in RECOMMENDED_CREW.items()},
    "fallbacks": {
        "architect": [],
        "supervisor": [],
        "implementer": [],
        "reviewer": [],
    },
    "max_failovers_per_role": DEFAULT_MAX_FAILOVERS_PER_ROLE,
    "agent_token_budgets": dict(DEFAULT_AGENT_TOKEN_BUDGETS),
    "adapters": {},
    "followup_design_token_budget": None,
    "reviewer_followup": None,
    "recovery": {
        "enabled": True, "max_attempts": 3, "lease_minutes": 15,
        "worker_loss_grace_minutes": 2, "live_session_silence_minutes": 10,
        "protocol_silence_minutes": {"architect": 0, "supervisor": 0, "implementer": 0, "reviewer": 0},
        "liveness_seconds": 60, "dashboard_watchdog": True, "poll_seconds": 30,
        "operation_grace_seconds": 120,
    },
    # #49: the archive analyzer. `filing` is "gh" (file through the gh CLI)
    # or "report_only" (never construct a GitHub client; the report still
    # lists every draft). archive_dir None means HANDSOFF_ARCHIVE_DIR or
    # the default Documents archive.
    "analysis": {
        "enabled": True, "max_tickets_per_scan": 5, "dedupe_days": 30,
        "design_phase_hours_threshold": 1.0, "archive_dir": None, "filing": "gh",
        "framework_repo": "monzta1/project-handsoff",
    },
}


ANALYSIS_FILING_MODES = ("gh", "report_only")


AGENT_ROLES = ("architect", "supervisor", "implementer", "reviewer")


SELECTABLE_AGENT_ROLES = AGENT_ROLES


SELECTABLE_AGENT_ADAPTERS = ("codex", "claude")


HOST_AGENT_ADAPTER = "host"


HOST_CAPABLE_ROLES = ("supervisor", "architect")


AUTO_AGENT_ADAPTER = "auto"


LEGACY_UNCONFIGURED_AGENT_ADAPTER = "configure-me"


AGENT_SETTING_ADAPTERS = (AUTO_AGENT_ADAPTER, *SELECTABLE_AGENT_ADAPTERS)


DEFAULT_AGENT_MODEL = "default"


MAX_AGENT_MODEL_LENGTH = 128


def normalize_public_origins(value, label: str) -> list[str]:
    """#124: an origin is scheme://host[:port], nothing else. Each entry is
    canonicalised (lowercase scheme and host, explicit port dropped only
    when it is the scheme default) so comparison is exact, never a prefix."""
    from urllib.parse import urlsplit
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise HandsoffError(f"{label} must be a list of non-empty origin strings")
    result = []
    for item in value:
        parts = urlsplit(item.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"} \
                or parts.query or parts.fragment or parts.username or parts.password:
            raise HandsoffError(f"{label} entry {item!r} must be scheme://host[:port] with no path")
        port = parts.port
        default = 443 if parts.scheme == "https" else 80
        host = parts.hostname.lower()
        canonical = f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")
        if canonical not in result:
            result.append(canonical)
    return result


def load_config(root: Path) -> dict:
    """handsoff.toml, actually read this time. Missing keys fall back to
    DEFAULT_CONFIG rather than erroring, since a fresh project may not have
    customised every field yet."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["agents"] = dict(DEFAULT_CONFIG["agents"])
    cfg["models"] = dict(DEFAULT_CONFIG["models"])
    cfg["adaptive_routing_profiles"] = deepcopy(ADAPTIVE_DEFAULT_PROFILES)
    cfg["adaptive_routing_budgets"] = deepcopy(ADAPTIVE_DEFAULT_BUDGETS)
    cfg["risk_policy"] = deepcopy(ADAPTIVE_DEFAULT_RISK_POLICY)
    cfg["fallbacks"] = {role: [] for role in DEFAULT_CONFIG["fallbacks"]}
    cfg["agent_token_budgets"] = dict(DEFAULT_AGENT_TOKEN_BUDGETS)
    cfg["adapters"] = {}
    cfg["design_evidence"] = []
    cfg["profile_sources"] = {
        role: {"adapter": RECOMMENDED_PROFILE_SOURCE, "model": RECOMMENDED_PROFILE_SOURCE}
        for role in AGENT_ROLES
    }
    cfg["recovery"] = dict(DEFAULT_CONFIG["recovery"])
    cfg["recovery"]["protocol_silence_minutes"] = dict(DEFAULT_CONFIG["recovery"]["protocol_silence_minutes"])
    cfg["regression_gate"] = dict(DEFAULT_CONFIG["regression_gate"])
    cfg["analysis"] = dict(DEFAULT_CONFIG["analysis"])
    cfg["documentation"] = {key: list(value) for key, value in DEFAULT_CONFIG["documentation"].items()}
    path = root / "handsoff.toml"
    if not path.is_file():
        return cfg
    if tomllib is None:
        raise HandsoffError("handsoff.toml present but no TOML parser available (need Python 3.11+)")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandsoffError(f"cannot load {path}: {exc}") from exc
    project = raw.get("project", {})
    workflow = raw.get("workflow", {})
    agents = raw.get("agents", {})
    models = raw.get("models", {})
    fallback_policy = raw.get("fallback_policy", {})
    agent_budget = raw.get("agent_budget", {})
    adapters = raw.get("adapters", {})
    checks = raw.get("checks", {})
    implementer = raw.get("implementer", {})
    documentation = raw.get("documentation", {})
    dashboard_table = raw.get("dashboard", {})
    recovery = raw.get("recovery", {})
    regression_gate = raw.get("regression_gate", {})
    analysis = raw.get("analysis", {})
    routing_profiles = raw.get("routing_profiles", {})
    routing_budgets = raw.get("routing_budgets", {})
    risk_policy = raw.get("risk_policy", {})
    model_policy = raw.get("model_policy", {})
    reviewer_isolation = raw.get("reviewer_isolation", {})
    execution = raw.get("execution", {})
    digest = raw.get("digest", {})
    briefing = raw.get("briefing")
    regressions = raw.get("regressions", [])
    tickets = raw.get("tickets", [])
    if not isinstance(digest, dict):
        raise HandsoffError("handsoff.toml: digest must be a table")
    if not all(isinstance(section, dict) for section in (project, workflow, agents, models, fallback_policy, agent_budget, adapters, checks, implementer, documentation, recovery, regression_gate, analysis, routing_profiles, routing_budgets, risk_policy, model_policy, reviewer_isolation, execution)):
        raise HandsoffError(
            "handsoff.toml: project, workflow, agents, models, fallback_policy, agent_budget, checks, implementer, documentation, recovery, regression_gate, analysis, routing_profiles, routing_budgets, risk_policy, and model_policy must be tables"
        )
    cfg["adaptive_routing_profiles"] = validate_adaptive_routing_profiles(routing_profiles or ADAPTIVE_DEFAULT_PROFILES)
    cfg["adaptive_routing_budgets"] = validate_adaptive_routing_budgets(routing_budgets or ADAPTIVE_DEFAULT_BUDGETS)
    cfg["risk_policy"] = validate_adaptive_risk_policy(risk_policy or ADAPTIVE_DEFAULT_RISK_POLICY)
    cfg["model_policy"] = validate_model_policy(model_policy or DEFAULT_MODEL_POLICY)
    unknown_isolation = set(reviewer_isolation) - {"compatibility_mode", "compatibility_approved"}
    if unknown_isolation:
        raise HandsoffError("handsoff.toml: reviewer_isolation has unknown keys: "
                            + ", ".join(sorted(unknown_isolation)))
    for key in ("compatibility_mode", "compatibility_approved"):
        value = reviewer_isolation.get(key, False)
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: reviewer_isolation.{key} must be boolean")
    if reviewer_isolation.get("compatibility_approved") and not reviewer_isolation.get("compatibility_mode"):
        raise HandsoffError("handsoff.toml: reviewer isolation compatibility approval requires compatibility_mode")
    cfg["reviewer_isolation"] = {
        "compatibility_mode": bool(reviewer_isolation.get("compatibility_mode")),
        "compatibility_approved": bool(reviewer_isolation.get("compatibility_approved")),
    }
    if set(execution) - {"profile"}:
        raise HandsoffError("handsoff.toml: execution may contain only profile")
    profile = execution.get("profile", "safe")
    if profile not in {"safe", "dogfood", "unattended", "shared", "production"}:
        raise HandsoffError("handsoff.toml: execution.profile must be safe, dogfood, unattended, shared, or production")
    cfg["execution_profile"] = profile
    if briefing is not None:
        if not isinstance(briefing, dict):
            raise HandsoffError("handsoff.toml: briefing must be a table")
        unknown_briefing = set(briefing) - BRIEFING_CONFIG_KEYS
        if unknown_briefing:
            raise HandsoffError(
                "handsoff.toml: briefing has unknown keys: " + ", ".join(sorted(unknown_briefing))
            )
        index = briefing.get("index")
        if not isinstance(index, str) or not index.strip() or Path(index).is_absolute() or ".." in Path(index).parts:
            raise HandsoffError("handsoff.toml: briefing.index must be a safe relative path")
        kb_root = briefing.get("root", "")
        if not isinstance(kb_root, str) or (kb_root and (Path(kb_root).is_absolute() or ".." in Path(kb_root).parts)):
            raise HandsoffError("handsoff.toml: briefing.root must be a safe relative path when set")
        cfg["briefing"] = {"index": index.strip(), "root": kb_root.strip()}
    cfg["status_file"] = project.get("status_file", cfg["status_file"])
    cfg["acceptance_file"] = project.get("acceptance_file", cfg["acceptance_file"])
    cfg["event_log"] = project.get("event_log", cfg["event_log"])
    cfg["verification_log"] = project.get("verification_log", cfg["verification_log"])
    logo = project.get("logo")
    if logo is not None and (not isinstance(logo, str) or not logo.strip()):
        raise HandsoffError("handsoff.toml: project.logo must be a non-empty relative path when set")
    cfg["logo"] = logo
    ignore = digest.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(item, str) for item in ignore):
        raise HandsoffError("handsoff.toml: digest.ignore must be a list of strings")
    cfg["digest_ignore"] = list(ignore)
    if set(adapters) - set(SELECTABLE_AGENT_ADAPTERS):
        raise HandsoffError("handsoff.toml: adapters may only contain codex and claude")
    for adapter, value in adapters.items():
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"handsoff.toml: adapters.{adapter} must name an existing file")
        path_value = Path(value).expanduser()
        if not path_value.is_absolute(): path_value = root / path_value
        if not path_value.is_file(): raise HandsoffError(f"handsoff.toml: adapters.{adapter} must name an existing file")
        cfg["adapters"][adapter] = str(path_value.resolve())
    for key in ("max_design_rounds", "max_review_rounds", "stall_minutes", "max_autonomous_design_reviews",
                "small_fix_max_criteria", "small_fix_max_changed_lines", "small_fix_max_files"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be an integer")
        cfg[key] = value
    for key in ("auto_handoff", "require_live_verification", "deployment_requires_explicit_approval",
                "require_design_approval"):
        value = workflow.get(key, cfg[key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: workflow.{key} must be boolean")
        cfg[key] = value
    waived = not cfg["deployment_requires_explicit_approval"] or not cfg["require_design_approval"]
    if waived and cfg["execution_profile"] != "dogfood":
        raise HandsoffError(
            "approval waivers require [execution] profile = \"dogfood\"; safe, unattended, shared, "
            "and production profiles cannot inherit dogfood waivers"
        )
    features = raw.get("features", {})
    if not isinstance(features, dict):
        raise HandsoffError("handsoff.toml: [features] must be a table")
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise HandsoffError("handsoff.toml: unknown [features] key(s): " + ", ".join(unknown)
                            + "; known: " + ", ".join(FEATURES))
    for key, value in features.items():
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: features.{key} must be boolean")
    cfg["features"] = {name: bool(features.get(name, default)) for name, (default, _text) in FEATURES.items()}
    # #39: a role key absent from the TOML (or still holding the legacy
    # "configure-me" placeholder) takes the recommended crew profile. Any
    # explicit value, "auto" included, is kept as written and only ever
    # affects its own role.
    for role in AGENT_ROLES:
        if role not in agents:
            continue
        value = agents[role]
        if not isinstance(value, str) or not value.strip():
            raise HandsoffError(f"handsoff.toml: agents.{role} must be a non-empty string")
        value = value.strip()
        if value == LEGACY_UNCONFIGURED_AGENT_ADAPTER:
            continue
        if value not in AGENT_SETTING_ADAPTERS and value != HOST_AGENT_ADAPTER:
            raise HandsoffError(f"handsoff.toml: agents.{role} must be exactly 'auto', 'codex', 'claude', or 'host'")
        cfg["agents"][role] = value
        if value == HOST_AGENT_ADAPTER and role not in HOST_CAPABLE_ROLES:
            raise HandsoffError(f"[agents].{role} cannot be host: only supervisor and architect may be host-driven")
        cfg["profile_sources"][role]["adapter"] = EXPLICIT_PROFILE_SOURCE
    for role in SELECTABLE_AGENT_ROLES:
        if role in models:
            cfg["models"][role] = validate_agent_model(models[role])
            cfg["profile_sources"][role]["model"] = EXPLICIT_PROFILE_SOURCE
        elif cfg["agents"][role] != RECOMMENDED_CREW[role]["adapter"]:
            # The recommended model belongs to the recommended adapter. With
            # the adapter overridden (or set to "auto") and no model named,
            # passing that model id to a different runner would be wrong,
            # so the runner's own default is used and labelled as such.
            cfg["models"][role] = DEFAULT_AGENT_MODEL
            cfg["profile_sources"][role]["model"] = RUNNER_DEFAULT_PROFILE_SOURCE
    # #37: the follow-up reviewer profile is enabled only by BOTH keys.
    # Half a profile is refused rather than guessed, and "auto" is not a
    # profile (the follow-up must be a specific adapter so the independence
    # check against the architect and implementer means something).
    followup_adapter = agents.get(FOLLOWUP_REVIEWER_KEY)
    followup_model = models.get(FOLLOWUP_REVIEWER_KEY)
    if (followup_adapter is None) != (followup_model is None):
        raise HandsoffError(
            f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} and models.{FOLLOWUP_REVIEWER_KEY} "
            "must be set together (both present enables the follow-up reviewer tier; both absent disables it)"
        )
    if followup_adapter is not None:
        if not isinstance(followup_adapter, str) or not followup_adapter.strip():
            raise HandsoffError(f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} must be a non-empty string")
        followup_adapter = followup_adapter.strip()
        if followup_adapter not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError(
                f"handsoff.toml: agents.{FOLLOWUP_REVIEWER_KEY} must be exactly 'codex' or 'claude'"
            )
        try:
            followup_model = validate_agent_model(followup_model)
        except HandsoffError as exc:
            raise HandsoffError(f"handsoff.toml: models.{FOLLOWUP_REVIEWER_KEY}: {exc}") from exc
        cfg["reviewer_followup"] = {"adapter": followup_adapter, "model": followup_model}
    else:
        cfg["reviewer_followup"] = None
    allowed_fallback_keys = {*SELECTABLE_AGENT_ROLES, "max_failovers_per_role"}
    unknown_fallback_keys = set(fallback_policy) - allowed_fallback_keys
    if unknown_fallback_keys:
        raise HandsoffError(
            f"handsoff.toml: fallback_policy has unknown keys: {', '.join(sorted(unknown_fallback_keys))}"
        )
    for role in SELECTABLE_AGENT_ROLES:
        cfg["fallbacks"][role] = validate_fallback_entries(
            fallback_policy.get(role, []), field=f"fallback_policy.{role}",
        )
    cfg["max_failovers_per_role"] = validate_max_failovers(
        fallback_policy.get("max_failovers_per_role", DEFAULT_MAX_FAILOVERS_PER_ROLE)
    )
    unknown_budget_roles = set(agent_budget) - {*SELECTABLE_AGENT_ROLES, "followup_design"}
    if unknown_budget_roles:
        raise HandsoffError(
            "handsoff.toml: agent_budget has unknown keys: "
            + ", ".join(sorted(unknown_budget_roles))
        )
    for role in SELECTABLE_AGENT_ROLES:
        value = agent_budget.get(role, DEFAULT_AGENT_TOKEN_BUDGETS[role])
        if not isinstance(value, int) or isinstance(value, bool) \
                or not MIN_AGENT_TOKEN_BUDGET <= value <= MAX_AGENT_TOKEN_BUDGET:
            raise HandsoffError(
                f"handsoff.toml: agent_budget.{role} must be an integer from "
                f"{MIN_AGENT_TOKEN_BUDGET} to {MAX_AGENT_TOKEN_BUDGET}"
            )
        cfg["agent_token_budgets"][role] = value
    followup_budget = agent_budget.get("followup_design")
    if followup_budget is not None:
        if not isinstance(followup_budget, int) or isinstance(followup_budget, bool) or followup_budget < MIN_AGENT_TOKEN_BUDGET:
            raise HandsoffError("handsoff.toml: agent_budget.followup_design must be a positive integer")
        cfg["followup_design_token_budget"] = followup_budget
    for config_key, toml_key in (("check_commands", "commands"), ("live_check_commands", "live_commands")):
        value = checks.get(toml_key, cfg[config_key])
        if not isinstance(value, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in value):
            raise HandsoffError(f"handsoff.toml: checks.{toml_key} must be an array of non-empty command strings")
        cfg[config_key] = list(value)
    # Field-note defect 5: a live command with shell operators used to pass
    # validate, status and doctor and fail only at verify-live, after
    # deployment approval. Refuse it at load with verify-live's own message.
    for index, command in enumerate(cfg["live_check_commands"]):
        try:
            assert_plain_command(command)
        except HandsoffError as exc:
            raise HandsoffError(f"handsoff.toml: checks.live_commands[{index}]: {exc}") from exc
    implementer_commands = implementer.get("commands", [])
    if not isinstance(implementer_commands, list) or not all(isinstance(cmd, str) and cmd.strip() for cmd in implementer_commands):
        raise HandsoffError("handsoff.toml: implementer.commands must be an array of non-empty command strings")
    cfg["implementer_commands"] = list(implementer_commands)
    timeout_value = checks.get("timeout_seconds", cfg["check_timeout_seconds"])
    if not isinstance(timeout_value, int) or isinstance(timeout_value, bool) or timeout_value <= 0:
        raise HandsoffError("handsoff.toml: checks.timeout_seconds must be a positive integer")
    cfg["check_timeout_seconds"] = timeout_value
    for key in ("files", "exclude"):
        value = documentation.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise HandsoffError(f"handsoff.toml: documentation.{key} must be a list of non-empty strings")
        cfg["documentation"][key] = list(value)
    if not isinstance(dashboard_table, dict):
        raise HandsoffError("handsoff.toml: dashboard must be a table")
    cfg["public_origins"] = normalize_public_origins(dashboard_table.get("public_origins", []), "handsoff.toml: dashboard.public_origins")
    unknown_gate = set(regression_gate) - set(DEFAULT_CONFIG["regression_gate"])
    if unknown_gate:
        raise HandsoffError(f"handsoff.toml: regression_gate has unknown keys: {', '.join(sorted(unknown_gate))}")
    for key in ("approval_timeout_minutes", "launch_window_minutes"):
        value = regression_gate.get(key, cfg["regression_gate"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440:
            raise HandsoffError(f"handsoff.toml: regression_gate.{key} must be an integer from 1 to 1440")
        cfg["regression_gate"][key] = value
    major_only = regression_gate.get(
        "full_regression_major_only", cfg["regression_gate"]["full_regression_major_only"],
    )
    if not isinstance(major_only, bool):
        raise HandsoffError("handsoff.toml: regression_gate.full_regression_major_only must be boolean")
    cfg["regression_gate"]["full_regression_major_only"] = major_only
    if not isinstance(regressions, list):
        raise HandsoffError("handsoff.toml: regressions must be an array of tables")
    normalized_regressions = []
    for index, item in enumerate(regressions):
        allowed = {"name", "commands", "timeout_seconds"}
        if not isinstance(item, dict) or not {"name", "commands"} <= set(item) or set(item) - allowed:
            raise HandsoffError(
                f"handsoff.toml: regressions[{index}] must contain name, commands, and optional timeout_seconds"
            )
        name, commands = item.get("name"), item.get("commands")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise HandsoffError(f"handsoff.toml: regressions[{index}].name is invalid")
        if not isinstance(commands, list) or not commands or not all(isinstance(cmd, str) and cmd.strip() for cmd in commands):
            raise HandsoffError(f"handsoff.toml: regressions[{index}].commands must be non-empty strings")
        normalized = {"name": name, "commands": list(commands)}
        if "timeout_seconds" in item:
            regression_timeout = item["timeout_seconds"]
            if not isinstance(regression_timeout, int) or isinstance(regression_timeout, bool) \
                    or regression_timeout <= 0:
                raise HandsoffError(
                    f"handsoff.toml: regressions[{index}].timeout_seconds must be a positive integer"
                )
            normalized["timeout_seconds"] = regression_timeout
        normalized_regressions.append(normalized)
    if len({item["name"] for item in normalized_regressions}) != len(normalized_regressions):
        raise HandsoffError("handsoff.toml: regression names must be unique")
    cfg["regressions"] = normalized_regressions
    allowed_recovery = set(DEFAULT_CONFIG["recovery"])
    unknown_recovery = set(recovery) - allowed_recovery
    if unknown_recovery:
        raise HandsoffError(
            f"handsoff.toml: recovery has unknown keys: {', '.join(sorted(unknown_recovery))}"
        )
    for key in ("enabled", "dashboard_watchdog"):
        value = recovery.get(key, cfg["recovery"][key])
        if not isinstance(value, bool):
            raise HandsoffError(f"handsoff.toml: recovery.{key} must be boolean")
        cfg["recovery"][key] = value
    bounds = {
        "max_attempts": (0, 16), "lease_minutes": (1, 1440),
        "worker_loss_grace_minutes": (1, 1440), "live_session_silence_minutes": (1, 1440),
        "liveness_seconds": (1, 3600), "poll_seconds": (1, 3600),
        "operation_grace_seconds": (0, 3600),
    }
    for key, (minimum, maximum) in bounds.items():
        value = recovery.get(key, cfg["recovery"][key])
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise HandsoffError(
                f"handsoff.toml: recovery.{key} must be an integer from {minimum} to {maximum}"
            )
        cfg["recovery"][key] = value
    protocol_limits = recovery.get("protocol_silence_minutes", cfg["recovery"]["protocol_silence_minutes"])
    if not isinstance(protocol_limits, dict):
        raise HandsoffError("handsoff.toml: recovery.protocol_silence_minutes must be a per-role table")
    unknown_roles = set(protocol_limits) - set(AGENT_ROLES)
    if unknown_roles:
        raise HandsoffError("handsoff.toml: recovery.protocol_silence_minutes has unknown roles: " + ", ".join(sorted(unknown_roles)))
    for role_name in AGENT_ROLES:
        value = protocol_limits.get(role_name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1440:
            raise HandsoffError(f"handsoff.toml: recovery.protocol_silence_minutes.{role_name} must be an integer from 0 to 1440")
        cfg["recovery"]["protocol_silence_minutes"][role_name] = value
    cfg["analysis"] = _validate_analysis_config(analysis)
    if not isinstance(tickets, list):
        raise HandsoffError("handsoff.toml: tickets must be an array of tables")
    normalized_tickets = []
    for index, ticket in enumerate(tickets):
        if not isinstance(ticket, dict):
            raise HandsoffError(f"handsoff.toml: tickets[{index}] must be a table")
        unknown = set(ticket) - {"number", "title", "status", "url"}
        if unknown:
            raise HandsoffError(
                f"handsoff.toml: tickets[{index}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        number, title, state, url = (
            ticket.get("number"), ticket.get("title"), ticket.get("status"), ticket.get("url", "")
        )
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise HandsoffError(f"handsoff.toml: tickets[{index}].number must be a positive integer")
        if not isinstance(title, str) or not title.strip():
            raise HandsoffError(f"handsoff.toml: tickets[{index}].title must be a non-empty string")
        if state not in TICKET_STATES:
            raise HandsoffError(
                f"handsoff.toml: tickets[{index}].status must be one of {', '.join(sorted(TICKET_STATES))}"
            )
        if not isinstance(url, str):
            raise HandsoffError(f"handsoff.toml: tickets[{index}].url must be a string")
        normalized_tickets.append({
            "number": number, "title": title.strip(), "status": state, "url": url.strip(),
        })
    if len({ticket["number"] for ticket in normalized_tickets}) != len(normalized_tickets):
        raise HandsoffError("handsoff.toml: ticket numbers must be unique")
    cfg["tickets"] = normalized_tickets
    cfg["design_evidence"] = _validate_design_evidence_config(raw.get("design_evidence", []))
    resolved_root = root.resolve()
    for key in ("status_file", "acceptance_file", "event_log", "verification_log", "logo"):
        value = cfg[key]
        if key == "logo" and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or Path(value).is_absolute() or ".." in Path(value).parts:
            raise HandsoffError(f"handsoff.toml: project.{key} must be a safe relative path")
        # The string-only check above rejects ".." and absolute paths, but
        # a symlinked PARENT DIRECTORY defeats it just as completely: the
        # string never says ".." while the real file still lands outside
        # the project. Resolve the full path and confirm it is still a
        # descendant of the root after that resolution, not just lexically.
        try:
            (root / value).resolve().relative_to(resolved_root)
        except ValueError:
            raise HandsoffError(
                f"handsoff.toml: project.{key} resolves outside the project root "
                f"(a parent directory may be a symlink)") from None
    for key in ("max_design_rounds", "stall_minutes", "max_autonomous_design_reviews",
                "small_fix_max_criteria", "small_fix_max_changed_lines", "small_fix_max_files"):
        if cfg[key] < 0:
            raise HandsoffError(f"handsoff.toml: {key} must not be negative")
    if not 1 <= cfg["max_review_rounds"] <= 56:
        raise HandsoffError("handsoff.toml: workflow.max_review_rounds must be an integer from 1 to 56")
    if not 1 <= cfg["small_fix_max_criteria"] <= 64:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_criteria must be an integer from 1 to 64")
    if not 1 <= cfg["small_fix_max_changed_lines"] <= 100000:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_changed_lines must be an integer from 1 to 100000")
    if not 1 <= cfg["small_fix_max_files"] <= 1000:
        raise HandsoffError("handsoff.toml: workflow.small_fix_max_files must be an integer from 1 to 1000")
    ensure_regression_config_is_disjoint(cfg, root)
    return cfg


def _validate_analysis_config(analysis: dict) -> dict:
    """#49: the [analysis] table. Every key is optional; an invalid value is
    a load error like every other section."""
    cfg = dict(DEFAULT_CONFIG["analysis"])
    unknown = set(analysis) - set(cfg)
    if unknown:
        raise HandsoffError(f"handsoff.toml: analysis has unknown keys: {', '.join(sorted(unknown))}")
    enabled = analysis.get("enabled", cfg["enabled"])
    if not isinstance(enabled, bool):
        raise HandsoffError("handsoff.toml: analysis.enabled must be boolean")
    cfg["enabled"] = enabled
    for key, (minimum, maximum) in (("max_tickets_per_scan", (0, 50)), ("dedupe_days", (0, 365))):
        value = analysis.get(key, cfg[key])
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise HandsoffError(f"handsoff.toml: analysis.{key} must be an integer from {minimum} to {maximum}")
        cfg[key] = value
    threshold = analysis.get("design_phase_hours_threshold", cfg["design_phase_hours_threshold"])
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
            or not math.isfinite(threshold) or threshold <= 0:
        raise HandsoffError("handsoff.toml: analysis.design_phase_hours_threshold must be a number greater than 0")
    cfg["design_phase_hours_threshold"] = float(threshold)
    archive = analysis.get("archive_dir", cfg["archive_dir"])
    if archive is not None and (not isinstance(archive, str) or not archive.strip()):
        raise HandsoffError("handsoff.toml: analysis.archive_dir must be a non-empty string when set")
    cfg["archive_dir"] = archive.strip() if isinstance(archive, str) else None
    framework_repo = analysis.get("framework_repo", cfg["framework_repo"])
    if not isinstance(framework_repo, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", framework_repo.strip()):
        raise HandsoffError("handsoff.toml: analysis.framework_repo must be an owner/repository slug")
    cfg["framework_repo"] = framework_repo.strip()
    filing = analysis.get("filing", cfg["filing"])
    if filing not in ANALYSIS_FILING_MODES:
        raise HandsoffError("handsoff.toml: analysis.filing must be exactly 'gh' or 'report_only'")
    cfg["filing"] = filing
    return cfg


def _validate_design_evidence_config(value: object) -> list[dict]:
    """#38: `[[design_evidence]]` entries, each {id, command, inputs}. An
    absent section is an empty list and changes nothing. Commands are
    trusted configuration at the same level as [checks].commands; the
    input globs are relative to the project root and may not escape it."""
    if not isinstance(value, list):
        raise HandsoffError("handsoff.toml: design_evidence must be an array of tables")
    if len(value) > MAX_DESIGN_EVIDENCE_ENTRIES:
        raise HandsoffError(
            f"handsoff.toml: design_evidence may hold at most {MAX_DESIGN_EVIDENCE_ENTRIES} entries"
        )
    entries = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise HandsoffError(f"handsoff.toml: design_evidence[{index}] must be a table")
        unknown = set(entry) - {"id", "command", "inputs"}
        if unknown:
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        artifact_id, command, inputs = entry.get("id"), entry.get("command"), entry.get("inputs")
        if not isinstance(artifact_id, str) or not DESIGN_EVIDENCE_ID_PATTERN.match(artifact_id):
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}].id must match [a-z0-9-]{{1,64}}"
            )
        if not isinstance(command, str) or not command.strip():
            raise HandsoffError(f"handsoff.toml: design_evidence[{index}].command must be a non-empty string")
        if not isinstance(inputs, list) or not inputs \
                or not all(isinstance(pattern, str) and pattern.strip() for pattern in inputs):
            raise HandsoffError(
                f"handsoff.toml: design_evidence[{index}].inputs must be a non-empty array of glob strings"
            )
        for pattern in inputs:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise HandsoffError(
                    f"handsoff.toml: design_evidence[{index}].inputs must be relative globs inside the project root"
                )
        entries.append({"id": artifact_id, "command": command.strip(), "inputs": [p.strip() for p in inputs]})
    if len({entry["id"] for entry in entries}) != len(entries):
        raise HandsoffError("handsoff.toml: design_evidence ids must be unique")
    return entries


def validate_agent_model(value: object) -> str:
    """Validate a literal runner model ID without interpreting it.

    The value is passed as one argv element only after validation. Leading
    dashes are rejected so a model can never be confused for another CLI
    option even if a runner changes how it parses option values.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise HandsoffError("agent model must be a non-empty string without surrounding whitespace")
    if len(value) > MAX_AGENT_MODEL_LENGTH:
        raise HandsoffError(f"agent model must be at most {MAX_AGENT_MODEL_LENGTH} characters")
    if value.startswith("-"):
        raise HandsoffError("agent model must not begin with '-'")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HandsoffError("agent model must not contain control characters")
    return value


def validate_model_policy(value: object) -> dict:
    """Validate hard mission routing constraints.

    These constraints are filters, never preferences: a route that cannot
    satisfy them pauses instead of silently selecting another model.
    """
    if not isinstance(value, dict):
        raise HandsoffError("model_policy must be a table")
    allowed_fields = {"allowed_adapters", "denied_models", "quota_substitution"}
    extra = set(value) - allowed_fields
    if extra:
        raise HandsoffError("model_policy has unknown fields: " + ", ".join(sorted(extra)))
    adapters = value.get("allowed_adapters", ["codex", "claude"])
    if not isinstance(adapters, list) or not adapters or len(adapters) > 2 \
            or any(adapter not in {"codex", "claude"} for adapter in adapters) \
            or len(set(adapters)) != len(adapters):
        raise HandsoffError("model_policy.allowed_adapters must be a unique non-empty subset of codex and claude")
    denied = value.get("denied_models", [])
    if not isinstance(denied, list) or len(denied) > 32:
        raise HandsoffError("model_policy.denied_models must be an array of at most 32 model ids")
    denied = [validate_agent_model(model) for model in denied]
    if len(set(model.casefold() for model in denied)) != len(denied):
        raise HandsoffError("model_policy.denied_models must be unique")
    quota = value.get("quota_substitution", True)
    if not isinstance(quota, bool):
        raise HandsoffError("model_policy.quota_substitution must be boolean")
    return {"allowed_adapters": list(adapters), "denied_models": denied,
            "quota_substitution": quota}


def model_policy_allows(policy: object, adapter: str, model: str) -> bool:
    normalized = validate_model_policy(policy or DEFAULT_MODEL_POLICY)
    return adapter in normalized["allowed_adapters"] and model.casefold() not in {
        item.casefold() for item in normalized["denied_models"]
    }


def validate_fallback_entries(value: object, *, field: str = "fallbacks") -> list[dict]:
    """Validate and copy one role's bounded, ordered fallback profiles."""
    if not isinstance(value, list):
        raise HandsoffError(f"{field} must be an array")
    if len(value) > MAX_FALLBACK_PROFILES:
        raise HandsoffError(f"{field} must contain at most {MAX_FALLBACK_PROFILES} profiles")
    result = []
    for index, profile in enumerate(value):
        if not isinstance(profile, dict) or set(profile) != {"adapter", "model"}:
            raise HandsoffError(f"{field}[{index}] must contain exactly adapter and model")
        adapter = profile.get("adapter")
        if adapter not in SELECTABLE_AGENT_ADAPTERS:
            raise HandsoffError(f"{field}[{index}].adapter must be exactly 'codex' or 'claude'")
        result.append({"adapter": adapter, "model": validate_agent_model(profile.get("model"))})
    return result


def validate_max_failovers(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_FALLBACK_PROFILES:
        raise HandsoffError(
            f"fallback_policy.max_failovers_per_role must be an integer from 0 to {MAX_FALLBACK_PROFILES}"
        )
    return value


#: [features] switches: name -> (default, one line for the settings dialog).
#: failing_first is off by default because turning it on refuses every
#: project's next Phase 6 until baselines exist; the other two only refuse
#: what was already a mistake.
FEATURES = {
    "failing_first": (False, "A criterion's test must be seen to fail before the feature; Phase 6 and Phase 8 refuse a pass with no recorded failing run behind it (#165)."),
    "launch_rules": (True, "Rules distilled from run history are evaluated before a managed launch and at the reviewer packet boundary; a match refuses with the rule's cause (#167)."),
    "ticket_lock": (True, "init refuses a ticket that another live registered run already owns; --adopt takes over only a dead or closed owner (#166)."),
    "token_accounting": (True, "The usage each adapter prints is recorded on its session and summed by role, phase and ticket; nothing is estimated (#168)."),
    "review_binds_rules": (True, "A review, design approval and deployment approval carry the hash of the rules set they ran under; a changed hook or config revokes them (#170)."),
    "report_posting": (False, "Phase 8 completion posts the ledger's report to each ticket and closes it; off, nothing leaves the machine unless run-close --post says so (#171)."),
}


PLAIN_COMMAND_MESSAGE = "test commands may not contain shell expansion or control operators"


def assert_plain_command(command: str) -> list[str]:
    """Refuse every construct that can manufacture a different command after
    validation; the allowed language is simple argv plus path globs. Shared by
    verify-live and, since the field-note fixes, by config load, so an operator
    in [checks].live_commands is refused at configuration time (defect 5)."""
    if not isinstance(command, str) or not command.strip():
        raise HandsoffError("test command must be a non-empty string")
    if re.search(r"[\$`;&|<>(){}\r\n]", command):
        raise HandsoffError(PLAIN_COMMAND_MESSAGE)
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise HandsoffError(f"invalid test command: {exc}") from exc
    if any(token in {"|", "||", "&&", ";", ">", ">>", "<"} for token in words):
        raise HandsoffError("test commands may not contain shell control operators")
    return words


def normalized_test_footprint(command: str, root: Path) -> frozenset[str]:
    """Return the repository-relative tests a command can execute.

    This is intentionally conservative: a command that names a tests directory,
    wildcard, discovery mode, or an unrecognised test runner is treated as broad.
    Handsoff only needs to distinguish configured focused checks from configured
    regression groups; it is not a general shell parser.
    """
    # Commands use a shell so configured test-path globs continue to work.
    words = assert_plain_command(command)
    lowered = [Path(word).name.lower() for word in words]
    footprint: set[str] = set()
    for word in words:
        candidate = word.split("::", 1)[0]
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate.startswith("tests.") and "/" not in candidate:
            candidate = candidate.replace(".", "/") + ".py"
        if not (candidate.startswith("tests/") or candidate == "tests"):
            continue
        if candidate == "tests" or candidate.endswith("/"):
            return frozenset({"*"})
        if any(ch in candidate for ch in "*?["):
            matches = sorted(root.glob(candidate))
            if not matches:
                return frozenset({"*"})
            footprint.update(path.resolve().relative_to(root.resolve()).as_posix() for path in matches if path.is_file())
        else:
            footprint.add(Path(candidate).as_posix())
    if "unittest" in lowered and "discover" in lowered:
        return frozenset({"*"})
    if not footprint:
        return frozenset({"*"})
    return frozenset(footprint)


def ensure_regression_config_is_disjoint(cfg: dict, root: Path) -> None:
    regression_footprints = []
    for group in cfg.get("regressions", []):
        footprint = set().union(*(normalized_test_footprint(cmd, root) for cmd in group["commands"]))
        regression_footprints.append((group["name"], footprint))
    for command in cfg.get("check_commands", []):
        focused = normalized_test_footprint(command, root)
        for name, regression in regression_footprints:
            captures_group = "*" in focused or ("*" not in regression and regression <= set(focused))
            if captures_group:
                raise HandsoffError(
                    f"focused check overlaps gated regression group {name}: {command}"
                )


#: #284: these two live below the ledger because handsoff_schema needs them
#: and sits under the ledger, which cannot be imported from below. The ledger
#: still names both, so every existing importer is unaffected.
MAX_WORK_ITEMS = 64


VERIFICATION_REQUIREMENTS = {
    "automated": {"checks"},
    "manual": {"manual"},
    "browser": {"browser"},
    "automated_and_browser": {"checks", "browser"},
}

# #300: moved down from handsoff_lib so handsoff_evidence can use the ONE
# rule instead of its own `run_kind == "test"` comparison. That comparison
# is the exact defect #317 fixed: it misses the 100 archives written before
# run_kind existed, whose kind is decided by the repo-name prefix.
# #49: a run whose root name starts with one of these is a test fixture,
# a self-check, a drop-in or a benchmark run, never a product run.
FIXTURE_ROOT_PREFIXES = (
    "handsoff-test-", "handsoff-selfcheck", "handsoff-dropin", "handsoff-benchmark", "handsoff-fixture",
)
RUN_KINDS = ("test", "product")

#: #317: the ONE rule that decides what an archive is. Three readers used to
#: disagree about the same files. The Miner's answer was the correct one and
#: is adopted here: an explicit run_kind wins, and without one the repo name,
#: or the file name when the repo is absent, decides by prefix.
#:
#: The leniency it replaces mattered. `tokens_per_ticket` excluded only an
#: exact run_kind of "test", so the 100 archives written before #49 added the
#: field all passed as product, including the 81 that were fixture runs, and
#: those figures ride on every Fleet card.
#:
#: An unexpected value is NOT honoured. Trusting it would let a typo or a
#: hand-edited archive declare itself product; falling through to the name
#: rule keeps the decision on evidence the archive cannot fake about itself.
def classify_archive_record(record: object, file_name: str = "") -> str:
    """Return "test" or "product" for one archive record."""
    kind = record.get("run_kind") if isinstance(record, dict) else None
    if isinstance(kind, str) and kind in RUN_KINDS:
        return kind
    repo = record.get("repo") if isinstance(record, dict) else None
    name = repo if isinstance(repo, str) and repo else (file_name or "")
    if any(name.startswith(prefix) for prefix in FIXTURE_ROOT_PREFIXES):
        return "test"
    return "product"
