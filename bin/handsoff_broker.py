#!/usr/bin/env python3
"""Typed host dispatcher for read-only Supervisor orchestration requests.

The Supervisor agent itself runs read-only. A host integration may pass its
structured request here; this broker accepts only known fields and constructs
fixed, shell-free Handsoff commands. Human approvals never cross this boundary.
The OS sandbox on the owned Supervisor child is the authority boundary; the
in-process token below prevents accidental cross-role dispatch inside the host,
but is deliberately not claimed as authentication against arbitrary local code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_agent as agent_runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402

SUPERVISOR_SCRIPT = Path(__file__).resolve().with_name("handsoff_supervisor.py")
MAX_REQUEST_BYTES = 65536
HUMAN_ONLY_COMMANDS = {
    "design-approve", "deployment-gate", "design-review-authorize", "design-review-escalate",
    "review-cap-override", "recovery-acknowledge", "regression-decide", "regression-finalize",
}
_SUPERVISOR_HOST_CAPABILITY = object()


def _exact_fields(request: dict, required: set[str], optional: set[str] = set()) -> None:
    actual = set(request)
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise lib.HandsoffError(f"broker request is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise lib.HandsoffError(f"broker request has unknown fields: {', '.join(sorted(unknown))}")


def _text(request: dict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or any(ord(c) == 0 for c in value):
        raise lib.HandsoffError(f"broker field {key} must be a non-empty string without NUL")
    return value


def _workflow_argv(root: Path, request: dict) -> list[str]:
    command = _text(request, "command")
    base = [sys.executable, str(SUPERVISOR_SCRIPT), "--root", str(root), command]
    if command in HUMAN_ONLY_COMMANDS:
        raise lib.HandsoffError(f"broker refuses human-only command: {command}")
    if command == "status":
        _exact_fields(request, {"actor", "project_root", "action", "command"})
        return base
    if command == "advance":
        _exact_fields(
            request, {"actor", "project_root", "action", "command", "phase", "progress"},
            {"status", "implemented_by", "next_action", "authorization_hold"},
        )
        phase = request["phase"]
        progress = request["progress"]
        if not isinstance(phase, int) or isinstance(phase, bool) or phase not in lib.PHASES:
            raise lib.HandsoffError("broker advance.phase must be an integer from 1 through 8")
        if not isinstance(progress, (int, float)) or isinstance(progress, bool) or not 0 <= progress <= 100:
            raise lib.HandsoffError("broker advance.progress must be a number from 0 through 100")
        base.extend([str(phase), str(progress)])
        if "status" in request:
            status = _text(request, "status")
            if status not in lib.STATUS_VALUES:
                raise lib.HandsoffError("broker advance.status is invalid")
            base.extend(["--status", status])
        for field, flag in (("implemented_by", "--implemented-by"), ("next_action", "--next-action")):
            if field in request:
                base.extend([flag, _text(request, field)])
        if "authorization_hold" in request:
            hold = _text(request, "authorization_hold")
            if hold != "design_review" or phase != 2 or request.get("status") != "blocked":
                raise lib.HandsoffError(
                    "broker authorization_hold must be design_review on a blocked Phase-2 advance"
                )
            base.extend(["--authorization-hold", hold])
        return base
    if command == "heartbeat":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, {"note"})
        base.extend(["--by", _text(request, "by")])
        if "note" in request:
            base.extend(["--note", _text(request, "note")])
        return base
    if command == "recover":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, {"dry_run"})
        base.extend(["--by", _text(request, "by")])
        if request.get("dry_run") is True:
            base.append("--dry-run")
        elif "dry_run" in request and request["dry_run"] is not False:
            raise lib.HandsoffError("broker recover.dry_run must be boolean")
        return base
    if command == "regression-request":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "group"}, {"reason"})
        base.extend(["--group", _text(request, "group"), "--by", _text(request, "by")])
        if "reason" in request:
            base.extend(["--reason", _text(request, "reason")])
        return base
    if command in {"regression-run", "regression-cancel"}:
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "request_id"})
        base.extend(["--request-id", _text(request, "request_id"), "--by", _text(request, "by")])
        return base
    if command == "work-items-sync":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, {"from_tickets"})
        base.extend(["--by", _text(request, "by")])
        if request.get("from_tickets") is True:
            base.append("--from-tickets")
        elif "from_tickets" in request and request["from_tickets"] is not False:
            raise lib.HandsoffError("broker from_tickets must be boolean")
        return base
    if command == "work-item-activate":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "item"})
        base.extend([_text(request, "item"), "--by", _text(request, "by")])
        return base
    if command == "work-item-update":
        optional = {"title", "url", "github_state", "notes"}
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "item"}, optional)
        base.extend([_text(request, "item"), "--by", _text(request, "by")])
        for field, flag in (("title", "--title"), ("url", "--url"),
                            ("github_state", "--github-state"), ("notes", "--notes")):
            if field in request:
                base.extend([flag, _text(request, field)])
        return base
    if command in {"background-wait-start", "background-wait-end", "human-pause-start", "human-pause-end"}:
        optional = {"note"}
        if command == "background-wait-start":
            optional.add("resume_after_authorization")
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, optional)
        base.extend(["--by", _text(request, "by")])
        if "note" in request:
            base.extend(["--note", _text(request, "note")])
        if request.get("resume_after_authorization") is True:
            base.append("--resume-after-authorization")
        elif "resume_after_authorization" in request and request["resume_after_authorization"] is not False:
            raise lib.HandsoffError("resume_after_authorization must be boolean")
        return base
    if command == "verify":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "criteria"})
        criteria = request["criteria"]
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
            raise lib.HandsoffError("broker verify.criteria must be a non-empty string array")
        for criterion in criteria:
            base.extend(["--criterion", criterion])
        base.extend(["--by", _text(request, "by")])
        return base
    if command == "record-design-review":
        _exact_fields(request, {
            "actor", "project_root", "action", "command", "by", "architect", "decision", "summary",
        }, {"findings", "structural_blocker"})
        decision = _text(request, "decision")
        if decision not in {"approve", "request-changes"}:
            raise lib.HandsoffError("broker design-review decision is invalid")
        base.extend(["--by", _text(request, "by"), "--architect", _text(request, "architect"),
                     f"--{decision}", "--summary", _text(request, "summary")])
        if "structural_blocker" in request:
            # #37: a boolean flag only; the supervisor refuses it without
            # request-changes before writing anything.
            if request["structural_blocker"] is True:
                base.append("--structural-blocker")
            elif request["structural_blocker"] is not False:
                raise lib.HandsoffError("broker record-design-review structural_blocker must be boolean")
        if "findings" in request:
            # #36: bounded here only by shape; the supervisor enforces the
            # count and length caps and refuses before writing.
            findings = request["findings"]
            if not isinstance(findings, list) or not findings \
                    or not all(isinstance(f, str) and f.strip() and not any(ord(c) == 0 for c in f)
                               for f in findings):
                raise lib.HandsoffError("broker record-design-review findings must be a non-empty string array")
            for finding in findings:
                base.extend(["--finding", finding])
        return base
    if command == "design-review-packet":
        # #36: the Supervisor may generate the delta packet for the next
        # attempt; every disposition string is validated by the supervisor
        # against the most recent review's findings.
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, {"dispositions"})
        base.extend(["--by", _text(request, "by")])
        if "dispositions" in request:
            dispositions = request["dispositions"]
            if not isinstance(dispositions, list) or not dispositions \
                    or not all(isinstance(d, str) and d.strip() and not any(ord(c) == 0 for c in d)
                               for d in dispositions):
                raise lib.HandsoffError("broker design-review-packet dispositions must be a non-empty string array")
            for disposition in dispositions:
                base.extend(["--disposition", disposition])
        return base
    if command == "record-review":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"}, {"symptom_reproduced"})
        base.extend(["--by", _text(request, "by")])
        if "symptom_reproduced" in request:
            value = _text(request, "symptom_reproduced")
            if value not in {"yes", "not_applicable"}:
                raise lib.HandsoffError("broker symptom_reproduced is invalid")
            base.extend(["--symptom-reproduced", value])
        return base
    if command == "review-attempt-start":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"},
                      {"reviewer", "trigger", "note"})
        base.extend(["--by", _text(request, "by")])
        for field, flag in (("reviewer", "--reviewer"), ("trigger", "--trigger"), ("note", "--note")):
            if field in request:
                base.extend([flag, _text(request, field)])
        return base
    if command == "record-review-findings":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "findings"})
        findings = request["findings"]
        if not isinstance(findings, list) or not findings or len(findings) > 16 \
                or not all(isinstance(value, str) and value.strip() for value in findings) \
                or len(findings) != len(set(findings)):
            raise lib.HandsoffError("broker findings must be a non-empty unique string array of at most 16")
        base.extend(["--by", _text(request, "by")])
        for finding in findings:
            base.extend(["--finding", finding])
        return base
    if command == "record-symptom-resolved":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "evidence"})
        base.extend(["--evidence", _text(request, "evidence"), "--by", _text(request, "by")])
        return base
    if command == "design-evidence":
        # #38: both actions are non-human-only. `run` refreshes (or reuses)
        # the cached measurements; `show` prints their states as JSON.
        _exact_fields(request, {"actor", "project_root", "action", "command", "evidence_action"},
                      {"by", "ids", "force"})
        evidence_action = _text(request, "evidence_action")
        if evidence_action not in {"run", "show"}:
            raise lib.HandsoffError("broker design-evidence evidence_action must be run or show")
        base.append(evidence_action)
        if evidence_action == "show":
            if set(request) & {"by", "ids", "force"}:
                raise lib.HandsoffError("broker design-evidence show takes no by, ids, or force")
            return base
        if "ids" in request:
            ids = request["ids"]
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
                raise lib.HandsoffError("broker design-evidence ids must be a non-empty string array")
            for artifact_id in ids:
                base.extend(["--id", artifact_id])
        base.extend(["--by", _text(request, "by")])
        if request.get("force") is True:
            base.append("--force")
        elif "force" in request and request["force"] is not False:
            raise lib.HandsoffError("broker design-evidence force must be boolean")
        return base
    raise lib.HandsoffError(f"broker refuses unknown workflow command: {command}")


def execute_request(root: Path, request: dict, *, capability: object,
                    workflow_popen=subprocess.Popen,
                    agent_launcher=agent_runtime.execute_with_recovery) -> int:
    root = root.resolve()
    if capability is not _SUPERVISOR_HOST_CAPABILITY:
        raise lib.HandsoffError("broker accepts requests only from the trusted Supervisor context")
    if not isinstance(request, dict):
        raise lib.HandsoffError("broker request must be an object")
    if request.get("actor") != "supervisor":
        raise lib.HandsoffError("broker request actor must be supervisor")
    if request.get("project_root") != str(root):
        raise lib.HandsoffError("broker request project_root does not match the active project root")
    if not (root / "handsoff.toml").is_file() or not lib.status_path(root, lib.load_config(root)).is_file():
        raise lib.HandsoffError("broker active project is not an initialized Handsoff run")
    action = request.get("action")
    if action == "launch_role":
        _exact_fields(request, {"actor", "project_root", "action", "role", "task"}, {"timeout"})
        role = _text(request, "role")
        if role not in lib.LEGACY_AGENT_ROLES:
            raise lib.HandsoffError("Supervisor may launch only Architect, Implementer, or Reviewer")
        timeout = request.get("timeout", 3600)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise lib.HandsoffError("broker launch timeout must be a positive integer")
        spec = agent_runtime.build_launch_spec(root, role, _text(request, "task"))
        return agent_launcher(spec, timeout=timeout)
    if action == "quality_finding":
        _exact_fields(
            request,
            {"actor", "project_root", "action", "session_id", "finding_code"},
        )
        session_id = _text(request, "session_id")
        finding = lib.record_quality_finding(
            root, session_id=session_id, finding_code=_text(request, "finding_code"),
        )
        status = lib.load_unique_json(lib.status_path(root, lib.load_config(root)))
        source = (status.get("agent_sessions") or {}).get(session_id)
        if not isinstance(source, dict):
            raise lib.HandsoffError("quality finding session disappeared before replacement")
        seed = agent_runtime.LaunchSpec(
            source["role"], source["adapter"], source["requested_model"], (), str(root),
            agent_runtime.build_role_input(
                root, source["role"],
                "Continue the current mission using only the trusted Handsoff state.",
            ), "configured",
        )
        return agent_launcher(
            seed, from_session_id=session_id, quality_finding_id=finding["finding_id"],
        )
    if action == "workflow":
        argv = _workflow_argv(root, request)
        process = workflow_popen(
            argv, cwd=str(root), stdin=None, stdout=None, stderr=None,
            text=True, shell=False, start_new_session=True,
        )
        timeout = lib.load_config(root)["check_timeout_seconds"]
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            agent_runtime._stop_process_group(process)
            raise lib.HandsoffError(f"brokered workflow command timed out after {timeout} seconds") from exc
        except KeyboardInterrupt as exc:
            agent_runtime._stop_process_group(process)
            raise lib.HandsoffError("brokered workflow command cancelled") from exc
        if returncode:
            raise lib.HandsoffError(f"brokered workflow command exited with status {returncode}")
        return 0
    raise lib.HandsoffError("broker action must be workflow, launch_role, or quality_finding")


def parse_request(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise lib.HandsoffError("broker request is too large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise lib.HandsoffError(f"duplicate broker field: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique)
    except json.JSONDecodeError as exc:
        raise lib.HandsoffError(f"invalid broker JSON: {exc}") from exc
    return value


def dispatch_supervisor_request(root: Path, request: dict, **test_overrides) -> int:
    """Host-owner dispatch hook; OS sandboxing, not import privacy, limits roles."""
    return execute_request(
        root, request, capability=_SUPERVISOR_HOST_CAPABILITY, **test_overrides,
    )
