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
MAX_REVIEW_RESULT_BYTES = 16 * 1024
HUMAN_ONLY_COMMANDS = {
    "design-approve", "deployment-gate", "design-review-authorize", "design-review-escalate",
    "review-cap-override", "recovery-acknowledge", "regression-decide", "regression-finalize",
    "amendment-approve", "question-answer",
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


def _adoption_args(request: dict, base: list[str]) -> None:
    """#115: an adopted verdict names the session it came from and who
    adopted it; both or neither."""
    if "adopted_session" in request or "adopted_by" in request:
        if "adopted_session" not in request or "adopted_by" not in request:
            raise lib.HandsoffError("broker adopted_session and adopted_by must be given together")
        base.extend(["--adopted-session", _text(request, "adopted_session"),
                     "--adopted-by", _text(request, "adopted_by")])


def _extend_tests_executed(base: list[str], request: dict) -> None:
    """#80: a Reviewer reports whether it actually ran the linked tests; the
    broker forwards only the closed set the supervisor accepts, so a
    malformed value is refused here instead of surfacing as a protocol
    error after the review already completed."""
    if "tests_executed" not in request:
        return
    value = _text(request, "tests_executed")
    if value not in {"yes", "no", "unknown"}:
        raise lib.HandsoffError("broker tests_executed must be yes, no, or unknown")
    base.extend(["--tests-executed", value])


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
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "session"}, {"note"})
        base.extend(["--by", _text(request, "by"), "--session", _text(request, "session")])
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
        }, {"findings", "structural_blocker", "session", "adopted_session", "adopted_by"})
        decision = _text(request, "decision")
        if decision not in {"approve", "request-changes"}:
            raise lib.HandsoffError("broker design-review decision is invalid")
        base.extend(["--by", _text(request, "by"), "--architect", _text(request, "architect"),
                     f"--{decision}", "--summary", _text(request, "summary")])
        if "session" in request:
            base.extend(["--session", _text(request, "session")])
        _adoption_args(request, base)
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
        _exact_fields(request, {"actor", "project_root", "action", "command", "by"},
                      {"symptom_reproduced", "session", "tests_executed", "adopted_session", "adopted_by", "reaffirm"})
        base.extend(["--by", _text(request, "by")])
        if "session" in request:
            base.extend(["--session", _text(request, "session")])
        if request.get("reaffirm") is True:
            base.append("--reaffirm")
        elif "reaffirm" in request:
            raise lib.HandsoffError("broker reaffirm must be true when present")
        _adoption_args(request, base)
        _extend_tests_executed(base, request)
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
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "findings"},
                      {"session", "tests_executed", "adopted_session", "adopted_by"})
        _adoption_args(request, base)
        _extend_tests_executed(base, request)
        findings = request["findings"]
        if not isinstance(findings, list) or not findings or len(findings) > 16 \
                or not all(isinstance(value, str) and value.strip() for value in findings) \
                or len(findings) != len(set(findings)):
            raise lib.HandsoffError("broker findings must be a non-empty unique string array of at most 16")
        base.extend(["--by", _text(request, "by")])
        if "session" in request:
            base.extend(["--session", _text(request, "session")])
        for finding in findings:
            base.extend(["--finding", finding])
        return base
    if command == "record-symptom-resolved":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "evidence"})
        base.extend(["--evidence", _text(request, "evidence"), "--by", _text(request, "by")])
        return base
    if command == "criteria-apply":
        # #44: the Supervisor may apply (or preview) a criteria transaction
        # file; every operation is validated by the supervisor's planner
        # and refused before anything is written.
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "file"}, {"dry_run"})
        base.extend(["--file", _text(request, "file"), "--by", _text(request, "by")])
        if request.get("dry_run") is True:
            base.append("--dry-run")
        elif "dry_run" in request and request["dry_run"] is not False:
            raise lib.HandsoffError("broker criteria-apply.dry_run must be boolean")
        return base
    if command == "amendment-open":
        # #42: the Supervisor may open a scoped amendment from a transaction
        # file; the supervisor's planner classifies it and refuses a full
        # redesign before anything is written. Approval stays human-only.
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "file"},
                      {"summary", "request_full_redesign"})
        base.extend(["--file", _text(request, "file"), "--by", _text(request, "by")])
        if "summary" in request:
            base.extend(["--summary", _text(request, "summary")])
        if request.get("request_full_redesign") is True:
            base.append("--request-full-redesign")
        elif "request_full_redesign" in request and request["request_full_redesign"] is not False:
            raise lib.HandsoffError("broker amendment-open.request_full_redesign must be boolean")
        return base
    if command == "amendment-revise":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "file"})
        base.extend(["--file", _text(request, "file"), "--by", _text(request, "by")])
        return base
    if command == "amendment-review":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "decision", "summary"},
                      {"findings", "adopted_session", "adopted_by"})
        decision = _text(request, "decision")
        if decision not in {"approve", "request-changes"}:
            raise lib.HandsoffError("broker amendment-review decision is invalid")
        base.extend(["--by", _text(request, "by"), f"--{decision}", "--summary", _text(request, "summary")])
        findings = request.get("findings", [])
        if not isinstance(findings, list) or len(findings) > lib.MAX_AMENDMENT_REVIEW_FINDINGS \
                or not all(isinstance(f, str) and f.strip() and len(f) <= 512 and not any(ord(c) == 0 for c in f)
                           for f in findings):
            raise lib.HandsoffError("broker amendment-review findings must be a short list of non-empty strings")
        for finding in findings:
            base.extend(["--finding", finding])
        _adoption_args(request, base)
        return base
    if command == "question-raise":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "role", "text"}, {"session"})
        role = _text(request, "role")
        if role not in lib.SELECTABLE_AGENT_ROLES:
            raise lib.HandsoffError("broker question-raise role is invalid")
        base.extend(["--role", role, "--text", _text(request, "text"), "--by", _text(request, "by")])
        if "session" in request:
            base.extend(["--session", _text(request, "session")])
        return base
    if command == "pilot-note":
        # #49: the Supervisor may relay a Pilot observation onto the run
        # ledger; the supervisor validates the 1 to 512 character bound.
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "text"})
        base.extend(["--by", _text(request, "by"), "--text", _text(request, "text")])
        return base
    if command == "amendment-escalate":
        _exact_fields(request, {"actor", "project_root", "action", "command", "by", "reason"})
        base.extend(["--by", _text(request, "by"), "--reason", _text(request, "reason")])
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


ARCHITECT_CRITERIA_FIELDS = {"actor", "project_root", "action", "operations", "by"}


def parse_architect_request(text: str) -> dict:
    """#121: the Architect's only broker request, a criteria transaction
    {actor architect, project_root, action criteria, operations, by}.
    Operations are validated by the criteria planner on execution."""
    value = parse_request(text)
    if not isinstance(value, dict) or set(value) != ARCHITECT_CRITERIA_FIELDS:
        raise lib.HandsoffError("Architect broker request must have exactly actor, project_root, action, operations, by")
    if value.get("actor") != "architect" or value.get("action") != "criteria":
        raise lib.HandsoffError("Architect broker request must be actor architect, action criteria")
    if not isinstance(value.get("operations"), list) or not value["operations"] or len(value["operations"]) > 32:
        raise lib.HandsoffError("Architect criteria request needs 1 to 32 operations")
    if not isinstance(value.get("by"), str) or not value["by"].strip():
        raise lib.HandsoffError("Architect criteria request needs a non-empty by")
    return value


def execute_architect_criteria(root: Path, request: dict, *, capability: object,
                               workflow_popen=subprocess.Popen) -> int:
    """Run the Architect's criteria transaction through criteria-apply on
    the host, exactly as a Supervisor request would."""
    root = root.resolve()
    if capability is not _SUPERVISOR_HOST_CAPABILITY:
        raise lib.HandsoffError("broker accepts requests only from the trusted host context")
    if request.get("project_root") != str(root):
        raise lib.HandsoffError("broker request project_root does not match the active project root")
    import json as _json
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="handsoff-criteria-", delete=False, encoding="utf-8") as handle:
        _json.dump({"operations": request["operations"]}, handle)
        path = handle.name
    try:
        translated = {"actor": "supervisor", "project_root": str(root), "action": "workflow",
                      "command": "criteria-apply", "file": path, "by": request["by"]}
        return execute_request(root, translated, capability=capability, workflow_popen=workflow_popen)
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


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


def parse_reviewer_result(text: str) -> dict:
    """Parse the one bounded result a read-only Reviewer returns to its host."""
    if len(text.encode("utf-8")) > MAX_REVIEW_RESULT_BYTES:
        raise lib.HandsoffError("Reviewer result is too large")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise lib.HandsoffError(f"invalid Reviewer result JSON: {exc}") from exc
    required = {"kind", "decision", "summary", "findings", "structural_blocker", "symptom_reproduced"}
    allowed = required | {"tests_executed"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise lib.HandsoffError("Reviewer result has invalid fields")
    if value["kind"] not in {"design", "implementation"}:
        raise lib.HandsoffError("Reviewer result kind is invalid")
    if value["decision"] not in {"approved", "changes_requested"}:
        raise lib.HandsoffError("Reviewer result decision is invalid")
    if not isinstance(value["summary"], str) or not value["summary"].strip() or len(value["summary"]) > 2048:
        raise lib.HandsoffError("Reviewer result summary is invalid")
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 32 \
            or not all(isinstance(item, str) and item.strip() and len(item) <= 512 for item in findings):
        raise lib.HandsoffError("Reviewer result findings are invalid")
    if value["decision"] == "changes_requested" and not findings:
        raise lib.HandsoffError("Reviewer changes require at least one finding")
    if value["decision"] == "approved" and findings:
        raise lib.HandsoffError("Reviewer approval cannot include findings")
    if not isinstance(value["structural_blocker"], bool):
        raise lib.HandsoffError("Reviewer structural_blocker must be boolean")
    if value["kind"] != "design" and value["structural_blocker"]:
        raise lib.HandsoffError("structural_blocker applies only to design review; a blocked recording is never a structural blocker")
    if value["decision"] == "approved" and value["structural_blocker"]:
        raise lib.HandsoffError("Reviewer approval contradicts structural_blocker true; approve, or request changes with the blocker as a finding")
    if value["symptom_reproduced"] not in {"yes", "not_applicable"}:
        raise lib.HandsoffError("Reviewer symptom_reproduced is invalid")
    if value.get("tests_executed", "unknown") not in {"yes", "no", "unknown"}:
        raise lib.HandsoffError("Reviewer tests_executed is invalid")
    value.setdefault("tests_executed", "unknown")
    normalized = []
    for finding in value["findings"]:
        code, sep, summary = finding.partition(":")
        if not sep or code.strip() not in lib.REVIEW_FINDING_CODES:
            normalized.append("other: " + finding)
        else:
            normalized.append(code.strip() + ": " + summary.strip())
    value["findings"] = normalized
    return value


def _amendment_review_request(root: Path, cfg: dict, base: dict, status: dict, session: dict,
                              result: dict) -> dict:
    """#146: the amendment-review request for a reviewer session that was
    launched for an open amendment, whatever kind the reviewer wrote on
    the verdict. One builder serves live dispatch and session-result-adopt,
    so a request-changes verdict carries every finding on both paths. The
    #142 binding and scope checks live here."""
    open_record = lib.open_amendment(status)
    amendment_id = session.get("amendment_id")
    if not amendment_id:
        raise lib.HandsoffError("amendment Reviewer result: no amendment is bound to the session")
    if not open_record:
        raise lib.HandsoffError("amendment Reviewer result: no amendment is open")
    if open_record.get("amendment_id") != amendment_id:
        raise lib.HandsoffError(
            f"amendment Reviewer result: session was launched for {amendment_id}, "
            f"the open amendment is {open_record.get('amendment_id')}")
    if (session.get("started_at") or "") < (open_record.get("opened_at") or ""):
        raise lib.HandsoffError("amendment Reviewer result: the session was launched before the amendment opened")
    acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    _hash, problems = lib.recompute_amendment_hash(open_record, acceptance.get("criteria", []))
    if problems:
        raise lib.HandsoffError("amendment Reviewer result: " + "; ".join(problems))
    criteria_ids = {c.get("id") for c in acceptance.get("criteria", [])}
    missing = [cid for cid in (open_record.get("changed_ids") or []) if cid not in criteria_ids]
    items, _ = lib.effective_work_items(acceptance, cfg)
    required = {item.get("id") for item in items if item.get("required", True)}
    out_of_scope = [wid for wid in (open_record.get("affected_work_items") or []) if wid not in required]
    if missing or out_of_scope:
        raise lib.HandsoffError(
            "amendment Reviewer result: the amendment is out of scope "
            f"(missing criteria {missing}, items no longer in the run {out_of_scope})")
    request = {**base, "command": "amendment-review",
               "decision": "approve" if result.get("decision") == "approved" else "request-changes",
               "summary": result.get("summary")}
    findings = [str(item) for item in (result.get("findings") or []) if str(item).strip()]
    if findings:
        request["findings"] = findings
    request.pop("session", None)          # amendment-review binds no phase session
    return request


def _reviewer_result_request(root: Path, session_id: str, result: dict, actor: str | None = None) -> dict:
    cfg = lib.load_config(root)
    status = lib.load_unique_json(lib.status_path(root, cfg))
    sessions = status.get("agent_sessions") or {}
    current = status.get("current_agent_sessions") or {}
    session = sessions.get(session_id)
    if not isinstance(session, dict) or session.get("role") != "reviewer" \
            or current.get("reviewer") != session_id \
            or (session.get("state") not in lib.AGENT_SESSION_LIVE_STATES and actor is None):
        raise lib.HandsoffError("Reviewer result is not bound to the current live Reviewer session")
    reviewer = session["actor"]
    base = {"actor": "supervisor", "project_root": str(root), "action": "workflow", "by": actor or reviewer,
            "session": session_id}
    if session.get("amendment_id"):
        # #146: the session's amendment binding outranks the reviewer's kind label.
        return _amendment_review_request(root, cfg, base, status, session, result)
    open_record = lib.open_amendment(status)
    if open_record:
        # #142: while an amendment is open, only a session launched for it
        # may deliver a verdict, whatever kind that verdict carries.
        raise lib.HandsoffError(
            f"an amendment ({open_record.get('amendment_id')}) is open: launch the reviewer with "
            f"--amendment {open_record.get('amendment_id')} so its verdict is recorded as the amendment review")
    if result["kind"] == "design":
        if status.get("phase_number") != 2:
            raise lib.HandsoffError("design Reviewer result requires Phase 2")
        architect_session = lib.current_agent_sessions(status).get("architect")
        architect = architect_session.get("actor") if isinstance(architect_session, dict) else None
        if not architect:
            architect = (status.get("design_proposal") or {}).get("architect")
        if not architect:
            raise lib.HandsoffError("design Reviewer result has no managed Architect identity")
        request = {
            **base, "command": "record-design-review", "architect": architect,
            "decision": "approve" if result["decision"] == "approved" else "request-changes",
            "summary": result["summary"],
        }
        if result["findings"]:
            request["findings"] = result["findings"]
        if result["structural_blocker"]:
            request["structural_blocker"] = True
        return request
    if status.get("phase_number") != 5:
        raise lib.HandsoffError("implementation Reviewer result requires Phase 5")
    if result["decision"] == "approved":
        return {**base, "command": "record-review", "symptom_reproduced": result["symptom_reproduced"],
                "tests_executed": result.get("tests_executed", "unknown")}
    return {**base, "command": "record-review-findings", "findings": result["findings"],
            "tests_executed": result.get("tests_executed", "unknown")}


def dispatch_reviewer_result(root: Path, session_id: str, result: dict, **test_overrides) -> int:
    """Host bridge from a read-only Reviewer result to Supervisor-owned state."""
    root = root.resolve()
    request = _reviewer_result_request(root, session_id, result)
    return execute_request(root, request, capability=_SUPERVISOR_HOST_CAPABILITY, **test_overrides)


def dispatch_supervisor_request(root: Path, request: dict, **test_overrides) -> int:
    """Host-owner dispatch hook; OS sandboxing, not import privacy, limits roles."""
    return execute_request(
        root, request, capability=_SUPERVISOR_HOST_CAPABILITY, **test_overrides,
    )
