#!/usr/bin/env python3
"""#308: the rule about what `status` must show, and the analysis that enforces it.

Handsoff's recurring defect shape is state produced and never consumed: a
ceiling recorded but not imposed, a model parsed but not stored, a deadline
computed but not called. The status projection is the same shape one level
up. It is hand-built, so a field a gate consults can be absent from it, and
nothing distinguishes "not shown because it is surfaced elsewhere" from "not
shown because nobody added it".

The rule: every status field a gate path consults is REACHABLE from the
status output, by its own key, through a declared derived path, or by an
explicit per-field `not_surfaced` declaration with a reason.

The analysis derives the consulted set from the source rather than from a
hand-written list, so a new gate field cannot be added without being
declared. It is deliberately strict about what it will accept, because an
analysis that silently matches nothing would pass while proving nothing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: The gate paths this rule covers, named rather than discovered. A loose
#: sweep would drift as the module grows; this list is the contract, and a
#: function named here that no longer exists is an error, not a silent skip.
GATE_FUNCTIONS = {
    # #284: `design_review_budget` moved to the agent runtime with the rest of
    # the session lifecycle. This map is named rather than discovered, so an
    # extraction that relocates a gate must say so here; the analyser refusing
    # a gate it cannot find is the contract working, not a false alarm.
    "handsoff_lib.py": ("compute_errors", "gate_progress"),
    "handsoff_agent_runtime.py": ("design_review_budget",),
    "handsoff_supervisor.py": ("refresh_performance_state",),
    "handsoff_agent.py": ("build_launch_spec", "build_profile_launch_spec"),
}

#: A gate that reads no status field itself because it delegates to one that
#: does. Declared rather than omitted: dropping it from the contract would
#: hide the fact that it is a gate at all, and the test asserts its delegate
#: is analysed, so the chain cannot be broken silently.
DELEGATING_GATES = {
    "performance_mutation_refusal": "refresh_performance_state",
}

#: REQ-001: a run that finds fewer than this many consulted fields has not
#: analysed anything real, whatever it reports. The floor is set below the
#: count observed on v0.3.81 so ordinary edits do not trip it, and far above
#: zero so a broken matcher cannot pass.
MINIMUM_CONSULTED_FIELDS = 20

#: How a consulted field may be surfaced.
OWN_KEY = "own_key"
DERIVED = "derived"
NOT_SURFACED = "not_surfaced"


class ObservabilityError(Exception):
    """The analysis could not run, which is different from a rule violation."""


@dataclass(frozen=True)
class Consulted:
    """One status field read by one gate function."""
    field: str
    function: str
    module: str


class _StatusReads(ast.NodeVisitor):
    """Collect literal status field names read inside one function.

    Only the three access forms REQ-001 names are recognised:
    `status.get(NAME)`, `status.get(NAME, default)` and `status[NAME]`. A
    computed name is refused rather than skipped, because skipping it would
    let a field escape the rule silently, which is the exact failure this
    module exists to prevent.
    """

    #: The local names a gate function uses for the status mapping.
    STATUS_NAMES = {"status", "proposed", "close_status", "run_status"}

    def __init__(self, function: str, module: str):
        self.function = function
        self.module = module
        self.found: set[Consulted] = set()
        self.computed: list[str] = []

    def _record(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.found.add(Consulted(node.value, self.function, self.module))
        else:
            self.computed.append(f"{self.module}:{self.function}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" \
                and isinstance(func.value, ast.Name) and func.value.id in self.STATUS_NAMES \
                and node.args:
            self._record(node.args[0])
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.STATUS_NAMES:
            self._record(node.slice)
        self.generic_visit(node)


def consulted_fields(bin_dir: Path) -> set[Consulted]:
    """Every status field the named gate paths read, derived from source."""
    bin_dir = Path(bin_dir)
    found: set[Consulted] = set()
    computed: list[str] = []
    for module, functions in GATE_FUNCTIONS.items():
        path = bin_dir / module
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            raise ObservabilityError(f"cannot analyse {module}: {exc}") from exc
        by_name = {node.name: node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for function in functions:
            node = by_name.get(function)
            if node is None:
                raise ObservabilityError(
                    f"{module}: gate function {function!r} is named in GATE_FUNCTIONS but does "
                    "not exist; the contract and the code have drifted")
            visitor = _StatusReads(function, module)
            visitor.visit(node)
            found |= visitor.found
            computed += visitor.computed
    if computed:
        raise ObservabilityError(
            "a gate path reads a status field under a computed name, which cannot be checked: "
            + ", ".join(sorted(set(computed))))
    if len(found) < MINIMUM_CONSULTED_FIELDS:
        raise ObservabilityError(
            f"the analysis found only {len(found)} consulted fields, below the floor of "
            f"{MINIMUM_CONSULTED_FIELDS}; the matcher is broken rather than the code being clean")
    return found


def reachable_in(payload: dict, path: str) -> bool:
    """Is this dotted path present in a rendered status payload?"""
    node: object = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def validate_declarations(declarations: dict) -> None:
    """REQ-005: not_surfaced is bounded, per field, and justified.

    A shared reason, a wildcard, or a pattern would turn the declaration
    into the escape valve the design reviewer refused. Each entry stands on
    its own words.
    """
    reasons: dict[str, str] = {}
    for field, entry in declarations.items():
        if not isinstance(entry, dict) or "kind" not in entry:
            raise ObservabilityError(f"{field}: declaration must be a mapping with a kind")
        if any(ch in field for ch in "*?[]") or field.endswith("_"):
            raise ObservabilityError(f"{field}: a declaration names exactly one field, never a pattern")
        kind = entry["kind"]
        if kind not in (OWN_KEY, DERIVED, NOT_SURFACED):
            raise ObservabilityError(f"{field}: unknown declaration kind {kind!r}")
        if kind == DERIVED and not entry.get("path"):
            raise ObservabilityError(f"{field}: a derived declaration needs the path it is surfaced at")
        if kind == NOT_SURFACED:
            reason = entry.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 20:
                raise ObservabilityError(
                    f"{field}: not_surfaced needs a reason of its own, at least 20 characters")
            if reason in reasons.values():
                shared = next(k for k, v in reasons.items() if v == reason)
                raise ObservabilityError(
                    f"{field}: shares its not_surfaced reason verbatim with {shared}; a shared "
                    "constant is a blanket exemption, not a per-field decision")
            reasons[field] = reason


#: #308: how every consulted field is surfaced. Derived from the analysis
#: above on v0.3.81, then decided one field at a time. A field absent from
#: this map is a rule violation, not a default.
STATUS_OBSERVABILITY: dict[str, dict] = {
    # Surfaced under their own key in the status payload.
    **{name: {"kind": OWN_KEY} for name in (
        "design_review", "design_review_attempts", "design_round", "escalation",
        "feature", "live_verification_id", "phase_number", "progress",
        "review_round", "reviewed_by", "status",
        # Added by this lane: gate state a host polls during an unattended run.
        "deployment_approved", "design_approved", "design_review_authorization",
        "implemented_by", "lane", "model_policy", "original_symptom_evidence_id",
        "requirement_coverage", "review", "risk_class", "updated_at",
        "verification_head", "work_item_delivery",
    )},
    # Deliberately not surfaced, each for its own reason. These are the only
    # three; REQ-005 asserts that count, so a fourth is a visible change.
    "agent_sessions": {
        "kind": NOT_SURFACED,
        "reason": "One record per managed session, each carrying argv, budget decision, "
                  "usage and isolation contract; the running one is summarised by `live` "
                  "and `activity`, and Mission Control reads the file directly.",
    },
    "design_proposal": {
        "kind": NOT_SURFACED,
        "reason": "The Architect's full proposal text, up to eight items per key; it is "
                  "reviewed through design-review-packet rather than polled, and its hash "
                  "is what the gates actually bind to.",
    },
    "design_review_history": {
        "kind": NOT_SURFACED,
        "reason": "Every recorded review with its full findings, which grows without bound "
                  "across attempts; design_review_budget summarises the count and "
                  "design_review carries the most recent verdict.",
    },
}

#: REQ-002: the five states a host polls during an unattended run, at the
#: exact payload paths the test asserts.
HOST_POLLED_PATHS = ("review", "risk_class", "updated_at", "regression_requests", "recovery_lease")
