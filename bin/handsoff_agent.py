#!/usr/bin/env python3
"""Build and launch least-privilege Codex or Claude Code role sessions."""
from __future__ import annotations

import argparse
import codecs
import dataclasses
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402
import handsoff_adapters as adapters  # noqa: E402


@dataclass(frozen=True)
class LaunchSpec:
    role: str
    adapter: str
    model: str
    argv: tuple[str, ...]
    cwd: str
    stdin: str
    resolution_source: str = "configured"
    # #36: the delta review packet a Phase-2 reviewer was launched with, if any.
    packet_id: str | None = None
    design_hash: str | None = None
    # #37: the reviewer tier the Phase-2 selection chose, and why.
    tier: str | None = None
    tier_reason: str | None = None
    # #142: the open amendment this reviewer was launched for, if any.
    amendment_id: str | None = None
    # Host-enforced native rollout ceiling when the selected adapter
    # supports it.  It is configuration, never inferred from output.
    token_budget: int | None = None
    # #290: the limit actually handed to the provider (the ceiling minus the
    # protocol reserve), and how that limit is imposed. Both are recorded so
    # a session's bound can be audited without re-deriving it.
    provider_limit: int | None = None
    ceiling_enforcement: str | None = None
    env_overrides: dict[str, str] | None = None
    project_root: str | None = None
    # Opt-in adaptive selection, committed atomically with the session.
    adaptive_routing: dict | None = None
    # Deterministic pre-launch sizing facts; estimates are not usage.
    budget_decision: dict | None = None
    reviewer_isolation: dict | None = None
    # #347: the effort this launch was given, recorded on the session so a
    # run can be explained afterwards rather than only observed at launch.
    reasoning_effort: str | None = None
    routing_contract: dict | None = None
    # #359: the implementer's declared ownership (--owns), normalized.
    owned_paths: tuple[str, ...] | None = None
    # #388: a compact reviewer's validated file ranges. Set, the reviewer runs
    # in a scratch directory holding only those slices and cannot run tests.
    compact_scope: tuple[dict, ...] | None = None


SUPERVISOR_REQUEST_PREFIX = "HANDSOFF_BROKER_REQUEST:"
REVIEW_RESULT_PREFIX = "HANDSOFF_REVIEW_RESULT:"
DESIGN_RESULT_PREFIX = "HANDSOFF_DESIGN_PROPOSAL:"
#: #177: the Architect's third outcome; recorded as a pending decline the reviewer resolves
DECLINE_RESULT_PREFIX = "HANDSOFF_DESIGN_DECLINE:"
MANAGED_ROLE_ENV = "HANDSOFF_MANAGED_SESSION_ROLE"
MANAGED_SESSION_ENV = "HANDSOFF_MANAGED_SESSION_ID"
MAX_AGENT_TASK_BYTES = 16 * 1024
#: #203: how long an output reader may keep draining after the child exited.
READER_DRAIN_SECONDS = 60
MAX_REPLACEMENT_INPUT_BYTES = 64 * 1024
MAX_SUPERVISOR_REQUESTS = 8
IMPLEMENTATION_REVIEW_PACKET_HEADING = "# Implementation review delta"
#: #397: the engine-built packet every Phase-5 reviewer launch carries.
IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING = "# Implementation review packet"

# Managed coding roles need the repository shell, not the user's entire
# interactive Codex plugin/app/tool catalogue.  Disabling those optional
# surfaces materially reduces the fixed prompt paid again on every tool
# turn while leaving the OS sandbox and core shell/edit tools intact.
CODEX_DISABLED_FEATURES = lib.CODEX_DISABLED_FEATURES


def _claude_allowed_tools(cfg: dict, root: Path, role: str, *, which=shutil.which) -> list[str]:
    """The Claude allowlist for one role, from ONE implementation.

    #343 REQ-003 names both launch builders because the two had their own copy
    of this expression and #347 had just shipped with one of its two sites
    threaded and the other not. A shared function is the only version of
    "both builders agree" that cannot drift.

    Reviewer: the read-only set, so the review can open the project and run
    its tests but cannot edit it. Architect and supervisor: unchanged, the
    empty allowlist they have always had. Implementer: unchanged.
    """
    if role == "reviewer":
        return list(lib.REVIEWER_READ_ONLY_TOOLS)
    if role in {"supervisor", "architect"}:
        return []
    return lib.implementer_allowed_tools(cfg, root, which)


def parse_compact_scope(values: list[str] | None) -> list[dict] | None:
    """#388: turn repeated `--compact-scope PATH:START-END` into a validated scope."""
    if not values:
        return None
    entries = []
    for value in values:
        path, separator, span = str(value).rpartition(":")
        start, dash, end = span.partition("-")
        if not separator or not dash or not start.isdigit() or not end.isdigit():
            raise lib.HandsoffError(f"--compact-scope {value!r} must be PATH:START-END")
        entries.append({"path": path, "start": int(start), "end": int(end)})
    return lib.validate_compact_review_scope(entries)


def _compact_denied_tools(root: Path) -> list[str]:
    """#388: what a compact Claude reviewer may not do: run a shell, or read,
    grep or glob the project by its absolute path (`//` is Claude's absolute
    path form). Its working directory, the slices, stays readable."""
    project = f"/{Path(root).resolve()}/**"
    return ["Bash"] + [f"{tool}({project})" for tool in ("Read", "Grep", "Glob")]


def build_compact_role_input(root: Path, task: str, scope: list[dict]) -> str:
    """#388: the reduced reviewer packet. It names only the slices: no
    playbook, briefing, design packet, delta or project root, because the
    reviewer can open nothing else and a packet pointing at the project
    invites exactly the exploration the compact scope exists to prevent."""
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    if len(task.encode("utf-8")) > MAX_AGENT_TASK_BYTES:
        raise lib.HandsoffError(f"task exceeds {MAX_AGENT_TASK_BYTES} UTF-8 bytes")
    slices = "\n".join(f"- {entry['path']} lines {entry['start']}-{entry['end']}" for entry in scope)
    # #397: a Phase-5 compact reviewer gets the same packet, told to judge
    # from the recorded results because it cannot run them.
    review_packet = applicable_implementation_review_packet(Path(root).resolve(), "reviewer", compact=True)
    packet_section = ("" if review_packet is None else
                      f"{IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING}\n\n"
                      f"{json.dumps(review_packet, sort_keys=True, separators=(',', ':'))}\n\n")
    return (f"{packet_section}# Sandbox\n\nYou review a compact scope. Do not run `handsoff supervisor` commands. The host "
            "records your HANDSOFF_REVIEW_RESULT line.\n\n"
            "# Compact review scope\n\nYour working directory holds only these slices, one `.slice` "
            "file each, and SCOPE.json. The project is not readable and no test can run, so this "
            f"review is recorded with tests_executed no.\n\n{slices}\n\n"
            f"{_role_prompt(Path(root).resolve(), 'reviewer')}\n\n# Assigned task\n\n{task}")


def _codex_argv(executable: str, role: str, model: str, token_budget: int,
                *, reviewer_sandbox: bool = False, reasoning: str | None = None) -> list[str]:
    return lib.codex_argv(executable, role, model, token_budget,
                          reviewer_sandbox=reviewer_sandbox, reasoning=reasoning)


def _contract_model(cfg: dict, adapter: str, model: str) -> str:
    """#305: a contract adapter has no runner default to defer to, so a role
    left at the default model takes the adapter's declared model."""
    if adapter not in lib.CONTRACT_AGENT_ADAPTERS or model != lib.DEFAULT_AGENT_MODEL:
        return model
    declared = (cfg.get(adapter) or {}).get("model")
    if not declared:
        raise lib.HandsoffError(f"{adapter} needs a model: set [models].<role> or [{adapter}].model")
    return declared


def _contract_executable(cfg: dict, adapter: str) -> str | None:
    """#305: a contract adapter's loop is Handsoff's own (handsoff_ollama.py),
    run by this interpreter. It is discovered by declaration, not on PATH."""
    return sys.executable if lib.contract_adapter_availability(cfg).get(adapter) else None


def _contract_preflight(root: Path, cfg: dict, adapter: str, model: str, executable: str,
                        argv: list[str], cwd: str, label: str) -> None:
    """#305: the contract pre-flight, refusing with the adapter's own state."""
    result = adapters.preflight(adapter, root, model=model, executable=executable, argv=argv,
                                cwd=cwd, host=(cfg.get(adapter) or {}).get("host"))
    if result["state"] != "ready":
        detail = result["preflight"] or {}
        raise lib.HandsoffError(
            f"{adapter}/{model} {label} ({detail.get('ollama_state') or result['state']}): "
            f"{detail.get('reason') or 'executable missing'}")


def _reviewer_scratch(root: Path, adapter: str, role: str, *, create: bool = True) -> Path | None:
    """Create the provider-neutral external scratch root for a Reviewer."""
    if role != "reviewer":
        return None
    root = root.resolve()
    if not create:
        return root / ".handsoff-reviewer-inspect"
    scratch = Path(tempfile.mkdtemp(prefix="handsoff-reviewer-")).resolve()
    assert not (scratch == root or root in scratch.parents)
    return scratch


@contextmanager
def _scratch_released_on_refusal(scratch: Path | None, *, created: bool):
    """#348: a launch refused after the reviewer scratch exists (pre-flight,
    a budget refusal) removes it, so refusing before reservation costs
    nothing on every refusal path, not only the isolation-contract one."""
    try:
        yield
    except BaseException:
        if created and scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        raise


class AgentLaunchError(lib.HandsoffError):
    """A managed child failed after its host-authenticated session existed."""
    def __init__(self, message: str, session_id: str):
        super().__init__(message)
        self.session_id = session_id


def _role_prompt(root: Path, role: str) -> str:
    path = lib.project_resource_path(root, f"prompts/{role}.md")
    if not path.is_file():
        raise lib.HandsoffError(f"role prompt is missing: {path}")
    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise lib.HandsoffError(f"cannot read role prompt {path}: {exc}") from exc
    if not prompt.strip():
        raise lib.HandsoffError(f"role prompt is empty: {path}")
    return prompt.rstrip()


DESIGN_EVIDENCE_ROLES = ("architect", "reviewer")
DESIGN_REVIEW_PACKET_HEADING = "# Delta review packet"


def applicable_design_review_packet(root: Path, cfg: dict, role: str) -> dict | None:
    """#36: the stored packet, only for a Reviewer in Phase 2 when it was
    built for the next attempt of the current design (attempt == attempts + 1
    and design_hash equal to the current design hash). Anything else, or an
    uninitialized project, yields None and the reviewer gets full context."""
    if role != "reviewer":
        return None
    status_file = lib.status_path(root, cfg)
    acceptance_file = lib.acceptance_path(root, cfg)
    if not status_file.is_file() or not acceptance_file.is_file():
        return None
    status = lib.load_unique_json(status_file)
    acceptance = lib.load_unique_json(acceptance_file)
    if not isinstance(status, dict) or not isinstance(acceptance, dict):
        return None
    return lib.applicable_design_review_packet(status, cfg, acceptance.get("criteria", []))


def _working_tree_changed_files(root: Path) -> list[str]:
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(root),
        text=True, capture_output=True, check=False,
    )
    return sorted({line[3:].strip() for line in changed.stdout.splitlines()
                   if len(line) > 3 and line[3:].strip()})[:32]


def implementation_review_delta_packet(root: Path, cfg: dict, role: str) -> dict | None:
    """Bounded follow-up facts; never resend the unchanged implementation context."""
    if role != "reviewer":
        return None
    try:
        status = lib.load_unique_json(lib.status_path(root, cfg))
        acceptance = lib.load_unique_json(lib.acceptance_path(root, cfg))
    except (lib.HandsoffError, OSError, ValueError):
        return None
    attempts = [item for item in (status.get("review_attempts") or []) if isinstance(item, dict)]
    previous = next((item for item in reversed(attempts)
                     if item.get("disposition") == "changes_requested"), None)
    if int(status.get("phase_number", 0) or 0) < 5 or previous is None:
        return None
    changed_files = _working_tree_changed_files(root)
    records, _problems = lib.load_verifications(root, cfg)
    closed_at = previous.get("closed_at") or ""
    evidence = [record.get("run_id") for record in records
                if isinstance(record, dict) and (record.get("at") or "") > closed_at
                and isinstance(record.get("run_id"), str)][-32:]
    criteria = [item.get("id") for item in (acceptance.get("criteria") or [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)][:64]
    findings = [{"code": item.get("code"), "summary": str(item.get("summary") or "")[:240]}
                for item in (previous.get("findings") or []) if isinstance(item, dict)][:16]
    payload = {
        "schema": 1, "previous_attempt": previous.get("attempt"),
        "changed_files": changed_files, "unresolved_findings": findings,
        "affected_criteria": criteria, "new_evidence": evidence,
    }
    payload["packet_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return payload


def applicable_implementation_review_packet(root: Path, role: str, *, compact: bool = False,
                                            diff: dict | None = None) -> dict | None:
    """#397: the engine-built packet for a Reviewer launched at Phase 5; None
    for any other role or phase, or an uninitialized project."""
    if role != "reviewer":
        return None
    cfg = lib.load_config(root)
    status_file = lib.status_path(root, cfg)
    if not status_file.is_file() or not lib.acceptance_path(root, cfg).is_file():
        return None
    if not lib.implementation_review_packet_applies(lib.load_unique_json(status_file), role):
        return None
    return lib.implementation_review_packet(root, cfg, compact=compact, diff=diff)


def build_role_input(root: Path, role: str, task: str, topic: str | None = None) -> str:
    """Build the in-memory role prompt without persisting the assigned task.

    #38: the Architect and the Reviewer also receive a `# Design evidence`
    section listing every configured measurement by state, with the bounded
    output of `current` artifacts only. Nothing is appended when no
    [[design_evidence]] entry is configured, and the other roles never see
    the section.

    #36: a Phase-2 Reviewer whose stored delta packet matches the current
    design gets `# Delta review packet` (canonical JSON) in front of the
    role prompt; a missing or mismatched packet changes nothing."""
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise lib.HandsoffError("role must be architect, implementer, or reviewer")
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    if len(task.encode("utf-8")) > MAX_AGENT_TASK_BYTES:
        raise lib.HandsoffError(f"task exceeds {MAX_AGENT_TASK_BYTES} UTF-8 bytes")
    root = root.resolve()
    cfg = lib.load_config(root)
    implementation_delta = implementation_review_delta_packet(root, cfg, role)
    briefing = lib.briefing_section(root, cfg, topic)
    text = f"{_role_prompt(root, role)}\n\n# Assigned task\n\n{task}"
    if role in {"implementer", "reviewer"}:
        status_file = lib.status_path(root, cfg)
        acceptance_file = lib.acceptance_path(root, cfg)
        if status_file.is_file() and acceptance_file.is_file():
            contract = lib.reviewer_implementation_contract(
                lib.load_unique_json(status_file), lib.load_unique_json(acceptance_file),
            )
            if contract is not None:
                text = ("# Reviewer-approved implementation contract\n\n"
                        + json.dumps(contract, sort_keys=True, separators=(",", ":"))
                        + "\n\n" + text)
    if role == "reviewer":
        text = (f"# Project root (read-only)\n\n{root}: use git as `git -C {root} ...` and tests as `cd {root} && python3 -m unittest ...`. "
                "Write only inside the current directory.\n\n" + text)
    if role == "implementer":
        text = f"{lib.implementer_permissions_section(root)}\n\n{text}"
        progress = _progress_section(root, role)  # #215: a relaunch knows what was finished
        if progress:
            text = f"{text}\n\n{progress}"
    if role in {"architect", "reviewer"}:
        # #121: the sandbox cannot take the project lock; the host records
        # every protocol line, so supervisor commands are never the answer.
        text = ("# Sandbox\n\nYour sandbox is read-only for the project. Do not run `handsoff supervisor` "
                "commands; they will fail on the project lock. The host records your protocol lines "
                "(HANDSOFF_BROKER_REQUEST criteria transactions and HANDSOFF_DESIGN_PROPOSAL for the Architect, "
                "HANDSOFF_REVIEW_RESULT for the Reviewer).\n\n" + text)
    if role in DESIGN_EVIDENCE_ROLES:
        cfg = lib.load_config(root)
        context = None if implementation_delta is not None else lib.managed_design_context(root, role)
        if context is not None:
            context_json = json.dumps(context, sort_keys=True, separators=(",", ":"))
            text = f"# Managed design context\n\n{context_json}\n\n{text}"
        packet = applicable_design_review_packet(root, cfg, role)
        if packet is not None:
            packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
            text = f"{DESIGN_REVIEW_PACKET_HEADING}\n\n{packet_json}\n\n{text}"
        # #397: the engine-built packet; a follow-up still carries its delta.
        review_packet = applicable_implementation_review_packet(root, role)
        if review_packet is not None:
            review_json = json.dumps(review_packet, sort_keys=True, separators=(",", ":"))
            text = f"{IMPLEMENTATION_REVIEW_FULL_PACKET_HEADING}\n\n{review_json}\n\n{text}"
        if implementation_delta is not None:
            delta_json = json.dumps(implementation_delta, sort_keys=True, separators=(",", ":"))
            text = f"{IMPLEMENTATION_REVIEW_PACKET_HEADING}\n\n{delta_json}\n\n{text}"
        evidence = lib.design_evidence_prompt_section(root, cfg)
        if evidence:
            text = f"{text}\n\n{evidence}"
    # #104: an implementer or reviewer relaunch continues with the
    # unfinished scope only.
    if role in {"implementer", "reviewer"}:
        try:
            scope = lib.resume_scope_section(root)
        except lib.HandsoffError:
            scope = ""
        if scope:
            text = f"{text}\n\n{scope}"
    # #46: answers the Pilot recorded for this role's earlier questions are
    # handed over exactly once, on the next launch, and the hand-over is
    # audited (questions_prompt_section marks them delivered).
    try:
        answers = lib.questions_prompt_section(root, role)
    except lib.HandsoffError:
        answers = ""
    if answers:
        text = f"{text}\n\n{answers}"
    if briefing:
        text = f"{briefing}\n\n{text}"
    # #208: the engine's playbook rides ahead of the project's knowledge base
    # on every launch, so a managed session on any machine carries the lane
    # rules without configuration.
    text = f"{lib.playbook_section(topic)}\n\n{text}"
    return text


def _session_started_at(root: Path, session_id: str) -> str | None:
    try:
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, lib.load_config(root)))
        return ((status.get("agent_sessions") or {}).get(session_id) or {}).get("started_at")
    except (lib.HandsoffError, OSError, ValueError):
        return None


def _record_session_fields(root: Path, session_id: str, fields: dict, *, event_kind: str,
                           event_message: str, **extra) -> None:
    """Write named fields onto one session record and log the event, in one commit."""
    with lib.project_lock(root):
        cfg = lib.load_config(root)
        status = lib.load_unique_json(lib.status_path(root, cfg))
        if not isinstance((status.get("agent_sessions") or {}).get(session_id), dict):
            raise lib.HandsoffError("agent session was not found")
        proposed = json.loads(json.dumps(status))
        proposed["agent_sessions"][session_id].update(json.loads(json.dumps(fields)))
        errors = lib.validate_status_schema(proposed)
        if errors:
            raise lib.HandsoffError(errors[0])
        lib.commit(root, cfg, status=proposed, event_kind=event_kind, event_message=event_message,
                   session_id=session_id, **extra)


def _utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _launch_episode(history: dict, started_at: str | None) -> dict | None:
    """#383: the episode a session was launched in is the last one started at
    or before the session's started_at, whichever episode is current now."""
    started = _utc(started_at)
    if started is None:
        return None
    launch = None
    for episode in history.get("episodes") or []:
        begun = _utc(episode.get("started_at"))
        if begun is not None and begun <= started:
            launch = episode
    return launch


#: #383: implementer sessions whose workspace is held for adoption, so the
#: launcher's cleanup leaves it in place even if the session record refused.
_HELD_WORKSPACES: set[str] = set()


def _quarantine_late_result(root: Path, session_id: str, role: str, kind: str,
                            result: dict | None) -> dict | None:
    """#383: hold a result whose launch episode was paused for performance review.

    A session that finishes during a pause did its work under an episode the
    Pilot has stopped; dispatching or applying it would start exactly the work
    the pause exists to stop. The result is kept on the session as
    `quarantined_result` (an implementer's workspace stays unapplied) so
    session-result-adopt can apply it once after performance-resume. Returns
    the record when the result was quarantined, None when it may proceed.
    A run with neither a performance record nor a timeline journal has never
    been on a clock and cannot be paused, so nothing is created for it here.
    A missing or tampered record with a journal is still clocked (#385).
    """
    try:
        import handsoff_runtime_control as runtime_control
        supervisor = __import__("handsoff_supervisor")
        if not any(supervisor._runtime_path(root, name).exists()
                   for name in (supervisor.PERFORMANCE_RECORD, supervisor.PERFORMANCE_TIMELINE_JOURNAL)):
            return None
        # The deadline may have passed while the session ran; evaluate it now
        # so the pause is on the record before the result is judged. The tick
        # rebuilds a missing or failing record from the timeline journal.
        supervisor.performance_tick(root)
        history = supervisor._runtime_read(root, supervisor.PERFORMANCE_RECORD,
                                           "handsoff.performance_history")
        episode = _launch_episode(history or {}, _session_started_at(root, session_id))
        if episode is None or runtime_control.late_result_disposition(
                history, episode["episode_id"], session_id) != "quarantine":
            return None
    except (ImportError, lib.HandsoffError, OSError, ValueError):
        return None
    record = {"kind": kind, "role": role, "episode_id": episode["episode_id"],
              "quarantined_at": datetime.now(timezone.utc).isoformat(),
              "result": result, "adopted_at": None, "adopted_by": None}
    if kind == "implementer_workspace":
        _HELD_WORKSPACES.add(session_id)
    message = "Late managed result quarantined: its launch episode is paused for performance review"
    try:
        _record_session_fields(root, session_id, {"quarantined_result": record},
                               event_kind="late_result_quarantined", event_message=message,
                               episode_id=episode["episode_id"], role=role)
    except lib.HandsoffError as exc:
        # Still held: the event says so, and the reviewer's verdict is
        # already on the session as its persisted result.
        lib.append_event(root, lib.load_config(root), "late_result_quarantined", message,
                         session_id=session_id, episode_id=episode["episode_id"], role=role,
                         record_refused=str(exc)[:200])
    sys.stdout.write(f"LATE_RESULT_QUARANTINED: session {session_id} ({role}) launched in "
                     f"{episode['episode_id']}, which is paused_for_performance_review; adopt it "
                     "with session-result-adopt after performance-resume\n")
    sys.stdout.flush()
    return record


def _describe_tree_changes(root: Path, before: dict, after: dict, changed_paths: list[str], started_at: str | None) -> list[dict]:
    """#203: one record per changed path: appeared, vanished or changed, the
    file's mtime, and how many seconds after the session started it was
    written (negative means before). Content-free: paths and times only."""
    out = []
    try:
        started = datetime.fromisoformat(started_at) if isinstance(started_at, str) else None
    except ValueError:
        started = None
    for path in changed_paths[:16]:
        kind = "appeared" if before.get(path) is None else "vanished" if after.get(path) is None else "changed"
        mtime = None
        offset = None
        try:
            stat = (Path(root) / path).stat()
            written = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            mtime = written.isoformat()
            if started is not None:
                offset = round((written - started).total_seconds(), 3)
        except OSError:
            pass
        out.append({"path": path, "kind": kind, "mtime": mtime, "seconds_after_session_start": offset})
    return out


def _progress_section(root: Path, role: str) -> str:
    """#215: what the previous failed Implementer session of this run said
    it finished, so a relaunch does not redo it; empty on a first launch or
    for any other role."""
    if role != "implementer":
        return ""
    try:
        with lib.project_lock(root):
            status = lib.load_unique_json(lib.status_path(root, lib.load_config(root)))
        previous = lib.latest_failed_implementer_progress(status)
    except (lib.HandsoffError, OSError, ValueError):
        previous = None
    if not previous:
        return ""
    summary = previous["summary"]
    lines = ["# Progress so far", "",
             f"The previous Implementer session ({previous['session_id']}) ended before finishing. Its ledgered account:"]
    for criterion in summary.get("done", []):
        test = previous["tests"].get(criterion)
        lines.append(f"- {criterion}: done" + (f" (test: {test})" if test else ""))
    for criterion in summary.get("partial", []):
        lines.append(f"- {criterion}: partial; check the tree before continuing")
    for criterion in summary.get("untouched", []):
        lines.append(f"- {criterion}: untouched")
    if previous.get("changed_paths"):
        lines.append("Paths it changed: " + ", ".join(previous["changed_paths"][:16]))
    lines.append("Do not redo a done criterion; run its test to confirm, then continue with the rest.")
    return "\n".join(lines)


def _completed_operations_section(root: Path, session_id: str | None) -> str:
    """Build a small id-only resume note so successful external work is not repeated."""
    if not session_id:
        return ""
    ids = lib.succeeded_operation_ids(root, session_id)
    if not ids:
        return ""
    return "# Completed external operations\n\n" + "\n".join(f"- {item}" for item in ids[:64]) \
        + "\n\nThese already succeeded; do not repeat them."


def _refuse_reviewer_launch_over_budget(root: Path, cfg: dict, role: str) -> None:
    """#35 advisory pre-check: fail a managed Phase-2 reviewer launch fast,
    with the authorization command in the message, before any profile
    resolution, availability lookup, or prompt assembly happens. This
    reads status once without the lock and decides nothing on its own:
    the lock-protected reservation inside lib.create_agent_session, which
    re-reads status under project_lock, is the sole authorization
    decision. Two concurrent launches can both pass here; exactly one can
    reserve there. Nothing is written on refusal: build_launch_spec
    returns before execute_launch ever runs."""
    if role != "reviewer":
        return
    status_file = lib.status_path(root, cfg)
    if not status_file.is_file():
        return
    status = lib.load_unique_json(status_file)
    if not isinstance(status, dict) or status.get("phase_number") != 2:
        return
    refusal = lib.design_review_launch_refusal(lib.design_review_budget(status, cfg), status)
    if refusal:
        raise lib.HandsoffError(refusal)


def _phase2_design_reviewer_selection(root: Path, cfg: dict, role: str, *, which) -> dict | None:
    """#37: the tiered reviewer selection, only for a Reviewer launch on an
    initialized project in Phase 2. Independence and availability are
    checked inside lib.select_design_reviewer_profile for whichever tier
    was selected; a refusal raises before any session exists."""
    if role != "reviewer":
        return None
    status_file = lib.status_path(root, cfg)
    acceptance_file = lib.acceptance_path(root, cfg)
    if not status_file.is_file() or not acceptance_file.is_file():
        return None
    status = lib.load_unique_json(status_file)
    acceptance = lib.load_unique_json(acceptance_file)
    if not isinstance(status, dict) or status.get("phase_number") != 2 or not isinstance(acceptance, dict):
        return None
    return lib.select_design_reviewer_profile(cfg, status, acceptance, which=which)


def build_launch_spec(root: Path, role: str, task: str, *, which=shutil.which, skip_preflight: bool = False,
                      inspection: bool = False, amendment: bool = False, topic: str | None = None,
                      owned_paths: list[str] | None = None,
                      compact_scope: list[dict] | None = None) -> LaunchSpec:
    root = root.resolve()
    lib.validate_runtime_integrity(root)
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise lib.HandsoffError("role must be architect, implementer, or reviewer")
    scope = None
    if compact_scope:
        if role != "reviewer":
            raise lib.HandsoffError("--compact-scope applies to reviewer launches only")
        scope = lib.validate_compact_review_scope(compact_scope)
    if owned_paths:
        if role != "implementer":
            raise lib.HandsoffError("--owns applies to implementer launches only")
        owned_paths = lib.normalize_owned_paths(root, owned_paths)
    else:
        owned_paths = None
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    cfg = lib.load_config(root)
    # #167: rules distilled from run history refuse first, before an
    # adapter is resolved or a session reserved: a refusal costs nothing.
    status_file = lib.status_path(root, cfg)
    phase = None
    run_status = None
    if status_file.is_file():
        try:
            run_status = lib.load_unique_json(status_file)
            phase = int(run_status.get("phase_number") or 0) or None
        except (ValueError, TypeError):
            phase = None
    if role == "implementer" and isinstance(run_status, dict):
        # #407: refused here, before any scratch, session or reservation
        refusal = lib.implementer_ownership_refusal(run_status, owned_paths)
        if refusal:
            raise lib.HandsoffError(refusal)
    effective_model_policy = lib.validate_model_policy(
        (run_status or {}).get("model_policy", cfg.get("model_policy", lib.DEFAULT_MODEL_POLICY))
    )
    route_cfg = {**cfg, "model_policy": effective_model_policy}
    rule = lib.evaluate_launch_rules(root, cfg, role=role, phase=phase, amendment=amendment)
    if rule is not None:
        raise lib.HandsoffError(lib.rule_refusal(rule))
    if role == "reviewer":
        status_file = lib.status_path(root, cfg)
        if status_file.is_file():
            status = lib.load_unique_json(status_file)
            if status.get("phase_number") == 2 and not status.get("design_proposal"):
                raise lib.HandsoffError("reviewer launch refused: no design proposal is recorded; run design-propose or an Architect session first")
            if status.get("phase_number") == 5:
                acceptance = lib.load_unique_json(lib.acceptance_path(root, lib.load_config(root)))
                records, _problems = lib.load_verifications(root, cfg)
                if not status.get("original_symptom_evidence_id"):
                    # #419: name the exact run id the command accepts
                    step = lib.symptom_resolution_step(acceptance, records) \
                        or "handsoff_supervisor.py record-symptom-resolved --evidence <run_id> --by ACTOR"
                    raise lib.HandsoffError(f"reviewer launch refused: run {step} first")
                gaps = lib.reviewer_launch_evidence_gaps(acceptance.get("criteria", []), records)
                if gaps:
                    raise lib.HandsoffError("reviewer launch refused, evidence still missing: " + "; ".join(gaps))
    if lib.agent_profiles(cfg)[role]["adapter"] == lib.HOST_AGENT_ADAPTER:
        raise lib.HandsoffError(f"{role} is host-driven; run the Supervisor CLI directly instead of launching a managed session")
    _refuse_reviewer_launch_over_budget(root, cfg, role)
    selection = _phase2_design_reviewer_selection(root, route_cfg, role, which=which)
    explicit_role_route = False
    if selection is not None:
        adapter = selection["adapter"]
        model = lib.validate_agent_model(_contract_model(cfg, adapter, selection["model"]))
        resolution_source = selection["resolution_source"]
        tier, tier_reason = selection["tier"], selection["reason"]
    else:
        tier = tier_reason = None
        configured_adapter = lib.agent_profiles(cfg)[role]["adapter"]
        profile = lib.resolved_agent_profiles(cfg, which=which, require_available=True)[role]
        adapter = profile["adapter"]
        if adapter not in lib.SELECTABLE_AGENT_ADAPTERS and adapter not in lib.CONTRACT_AGENT_ADAPTERS:
            raise lib.HandsoffError(f"role {role} uses unsupported adapter: {adapter}")
        model = lib.validate_agent_model(_contract_model(cfg, adapter, profile["model"]))
        # #39: "recommended" only when the adapter itself came from
        # RECOMMENDED_CREW; an explicit "auto" keeps the older auto-detect
        # label, and any other explicit value is "configured" (load_config
        # already folds the legacy "configure-me" placeholder into the
        # recommended crew, so it never reaches here). Nothing here ever
        # records the recommended profile as launched when it was not: the
        # executable check below refuses first.
        adapter_source = profile["source"]["adapter"]
        model_source = profile["source"]["model"]
        # An explicit role adapter or model is a mission constraint, not a
        # hint for adaptive routing to replace.  In particular, an explicit
        # Codex implementer must never be rewritten to the built-in Claude
        # PREMIUM profile for a shared-infrastructure run.
        configured_model = cfg.get("models", {}).get(role, lib.DEFAULT_AGENT_MODEL)
        explicit_role_route = (
            configured_adapter != lib.AUTO_AGENT_ADAPTER
            or configured_model != lib.DEFAULT_AGENT_MODEL
        ) and (adapter_source == "explicit" or model_source == "explicit")
        if configured_adapter == lib.AUTO_AGENT_ADAPTER:
            resolution_source = "auto_detected"
        elif adapter_source == lib.RECOMMENDED_PROFILE_SOURCE:
            resolution_source = "recommended"
        else:
            resolution_source = "configured"
    adaptive_routing = None
    if selection is None and isinstance(run_status, dict) and not explicit_role_route:
        # Runs created before adaptive routing may have no stored risk class.
        # A normal managed launch defaults those active runs to routine; the
        # session commit persists that upgrade atomically with the route.
        effective_risk_class = run_status.get("risk_class") or "routine"
        available_adapters = [candidate for candidate in lib.SELECTABLE_AGENT_ADAPTERS
                              if cfg.get("adapters", {}).get(candidate) or which(candidate)]
        # #305: a declared local adapter is a candidate only where its floors
        # permit this role and risk, so routing never ranks a refused choice.
        available_adapters += [candidate for candidate in lib.contract_adapter_availability(cfg)
                               if adapters.local_floor_refusal(cfg, candidate, role, effective_risk_class) is None]
        import handsoff_evidence_routing as evidence_routing
        routed = lib.route_adaptive_profile(
            route_cfg, required_capabilities=("text", "tool_use"),
            available_adapters=available_adapters, risk_class=effective_risk_class,
            mission_usage=lib.adaptive_usage(run_status),
            fleet_usage=lib.adaptive_fleet_usage(root, current_status=run_status),
            # Deterministic-check draining gates repair/escalation
            # continuation, not an initial managed launch.
            deterministic_checks_complete=True,
            # #303: None unless evidence-assisted routing was ever activated,
            # so a never-activated launch takes the static path unchanged.
            evidence_selector=evidence_routing.launch_selector(root, role=role, status=run_status),
        )
        if routed.get("state") != "selected":
            raise lib.HandsoffError(f"adaptive routing paused: {routed.get('reason')}")
        profile = routed["profile"]
        adapter, model, resolution_source = profile["adapter"], profile["model"], "adaptive"
        adaptive_routing = {
            "risk_class": routed["risk_class"], "tier": routed["tier"],
            "adapter": adapter, "model": model, "profile": profile,
            "reason": routed["reason"],
            "reviewer_required": routed["reviewer_required"],
            "human_gate_required": routed["human_gate_required"],
        }
        if "evidence" in routed:
            adaptive_routing["evidence"] = routed["evidence"]
    # #305: every path above (Phase-2 selection, explicit role profile,
    # adaptive route) meets the same floors before anything is reserved.
    adapters.enforce_local_floors(cfg, role, (run_status or {}).get("risk_class"),
                                  {"adapter": adapter, "model": model})
    if not lib.model_policy_allows(effective_model_policy, adapter, model):
        raise lib.HandsoffError(
            f"managed launch refused: {adapter}/{model} is denied by the mission model policy"
        )
    isolation = lib.reviewer_isolation_contract(adapter, cfg) if role == "reviewer" else None
    if isolation is not None and isolation["decision"] != "enforce":
        raise lib.HandsoffError(
            f"reviewer launch refused before session reservation: {isolation['reason']} "
            f"(adapter={adapter}, enforcement={isolation['enforcement']}, decision={isolation['decision']})"
        )
    stdin = build_compact_role_input(root, task, scope) if scope else build_role_input(root, role, task, topic)
    implementation_delta = implementation_review_delta_packet(root, cfg, role)
    packet = applicable_design_review_packet(root, cfg, role)
    criteria_count = 0
    try:
        criteria_count = len(lib.load_unique_json(lib.acceptance_path(root, cfg)).get("criteria") or [])
    except (lib.HandsoffError, OSError, ValueError):
        pass
    budget_decision = lib.plan_role_token_budget(
        configured_ceiling=cfg["agent_token_budgets"][role], role=role,
        # #342: key membership, never a comparison against the default.
        configured_explicitly=role in cfg["agent_token_budgets_explicit"],
        risk_class=(run_status or {}).get("risk_class"),
        packet_bytes=len(stdin.encode("utf-8")), criteria_count=criteria_count,
        changed_files=(len(implementation_delta["changed_files"])
                       if implementation_delta is not None else len(_working_tree_changed_files(root))),
        followup=implementation_delta is not None,
    )
    token_budget = budget_decision["ceiling"]
    # #304 REQ-006: every launch records the adapter contract facts,
    # including an `unenforced` ceiling the adapter declares; the failover
    # builder below records the same block for its reserved profile.
    routing_contract = adapters.session_contract(
        adaptive_routing["profile"] if adaptive_routing is not None
        else adapters.catalog_profile(route_cfg, adapter, model), token_budget)
    # #290: the provider is told the ceiling minus the protocol reserve, so a
    # session that spends everything still has room to emit its verdict. The
    # planner already refused a reserve that cannot fit.
    provider_limit = budget_decision["provider_limit"]
    ceiling_enforcement = lib.adapter_ceiling_enforcement(adapter)
    # #307: where usage arrives only at exit, no reserve can bound a turn
    # already in flight, so the launch is bounded instead of the budget.
    # Refusing here costs nothing; letting it run costs the whole ceiling
    # and returns no verdict, which is what session
    # hs-d26f19a8a3754328ae26f3a156740692 did.
    # #388: a compact reviewer can reach nothing but its slices, so its turns
    # are bounded by the scope and the refusal does not apply.
    turn_refusal = lib.turn_bound_refusal(
        adapter=adapter, role=role, ceiling=token_budget, compact_scope=scope is not None,
        safe_minimum=budget_decision["safe_minimum"])
    if turn_refusal:
        raise lib.HandsoffError(f"managed launch refused before session creation: {turn_refusal}")
    if provider_limit < budget_decision["safe_minimum"]:
        raise lib.HandsoffError(
            "managed launch refused before session creation: rendered packet estimates "
            f"{budget_decision['estimated_input_tokens']} input tokens, reserves "
            f"{budget_decision['response_reserve_tokens']} response tokens, and requires at least "
            f"{budget_decision['safe_minimum']} tokens; selected ceiling is {token_budget} "
            f"leaving {provider_limit} after the {budget_decision['reserved_protocol_tokens']}-token "
            "protocol reserve"
        )
    _refuse_oversized_implementation_review(root, cfg, run_status, role, scope,
                                            "managed launch refused before session creation")
    executable = _contract_executable(cfg, adapter) if adapter in lib.CONTRACT_AGENT_ADAPTERS \
        else cfg.get("adapters", {}).get(adapter) or which(adapter)
    if not executable:
        # An auto-detected adapter is by construction installed, so only a
        # recommended default or an explicit selection can land here.
        raise lib.HandsoffError(lib.unavailable_adapter_message(role, adapter, model, resolution_source))
    executable = str(Path(executable).resolve())
    # #343 REQ-004/REQ-008: before the scratch directory, before the session,
    # before the review attempt. A Claude reviewer that cannot read the project
    # produced a packet-only review that still SPENT one of the two autonomous
    # attempts, so the refusal has to land where nothing has been consumed yet.
    # Gated on this exact pair: architect and supervisor share the reviewer's
    # empty allowlist and run from the project root, and a probe that applied
    # to every role would refuse launches #343 never asked to change.
    if role == "reviewer" and adapter == "claude":
        # #388: a compact reviewer never reads the project, so there is
        # nothing for the probe to check.
        access = {"readable": True} if scope else lib.reviewer_read_access(root)
        if not access["readable"]:
            raise lib.HandsoffError(
                "managed Claude reviewer launch refused before session reservation: "
                + access["reason"])
    scratch = _reviewer_scratch(root, adapter, role, create=not inspection)
    with _scratch_released_on_refusal(scratch, created=not inspection):
        if scope and not inspection:
            lib.materialize_compact_scope(root, scope, scratch)
        if adapter == "codex":
            argv = _codex_argv(executable, role, model, provider_limit,
                               reviewer_sandbox=scratch is not None,
                               reasoning=cfg["model_reasoning"].get(role))
        elif adapter in lib.CONTRACT_AGENT_ADAPTERS:
            argv = adapters.build_argv(adapter, executable, role, model,
                                       provider_limit=provider_limit, project_root=root)
        else:
            allowed = [tool for tool in _claude_allowed_tools(cfg, root, role, which=which)
                       if not (scope and tool == "Bash")]
            # #388: a compact reviewer gets no --add-dir and an explicit deny
            # on the project path, so a read outside its slices is refused.
            argv = lib.claude_argv(executable, role, allowed, model, project_root=root if not scope else None,
                                   owned_paths=owned_paths)
            if scope:
                argv += ["--disallowedTools", ",".join(_compact_denied_tools(root))]
        if not skip_preflight and not inspection \
                and os.environ.get("HANDSOFF_SKIP_PREFLIGHT") != "1":
            if adapter in lib.CONTRACT_AGENT_ADAPTERS:
                _contract_preflight(root, cfg, adapter, model, executable, argv, str(scratch or root),
                                    "launch preflight blocked")
            else:
                preflight = lib.launch_preflight(
                    root, adapter=adapter, model=model, executable=executable,
                    argv=argv, cwd=str(scratch or root),
                )
                if preflight["state"] != "ready":
                    raise lib.HandsoffError(
                        f"{adapter}/{model} launch preflight blocked ({preflight['category']}): {preflight['reason']}"
                    )

    return LaunchSpec(
        role=role,
        adapter=adapter,
        model=model,
        argv=tuple(argv),
        cwd=str(scratch or root),
        stdin=stdin,
        resolution_source=resolution_source,
        packet_id=packet["packet_id"] if packet else None,
        design_hash=packet["design_hash"] if packet else None,
        tier=tier,
        tier_reason=tier_reason,
        token_budget=token_budget,
        provider_limit=provider_limit,
        ceiling_enforcement=ceiling_enforcement,
        env_overrides={"TMPDIR": str(scratch)} if scratch else None,
        project_root=str(root),
        adaptive_routing=adaptive_routing,
        budget_decision=budget_decision,
        reviewer_isolation=isolation,
        reasoning_effort=cfg["model_reasoning"].get(role),
        routing_contract=routing_contract,
        owned_paths=tuple(owned_paths) if owned_paths else None,
        compact_scope=tuple(scope) if scope else None,
    )


def _refuse_oversized_implementation_review(root: Path, cfg: dict, status: object, role: str,
                                            scope: list | None, refused: str) -> None:
    """#397: a Phase-5 review whose packet is over its cap, or whose diff
    cannot fit the reviewer budget, is refused before the scratch, the session
    or any reservation, on the primary and the fallback path alike. A compact
    launch is estimated from its slices, not the diff."""
    if not lib.implementation_review_packet_applies(status, role):
        return
    diff = lib.implementation_review_diff(root)
    review_packet = lib.implementation_review_packet(root, cfg, compact=scope is not None, diff=diff)
    size_refusal = lib.implementation_review_size_refusal(
        diff_bytes=lib.compact_scope_bytes(root, scope) if scope else diff["bytes"],
        packet_bytes=lib.implementation_review_packet_bytes(review_packet),
        budget=cfg["agent_token_budgets"]["reviewer"], compact=scope is not None)
    if size_refusal:
        raise lib.HandsoffError(f"{refused}: {size_refusal}")


def build_profile_launch_spec(root: Path, role: str, task: str, profile: dict,
                              *, which=shutil.which, skip_preflight: bool = False,
                              owned_paths: tuple[str, ...] | None = None,
                              compact_scope: tuple[dict, ...] | None = None) -> LaunchSpec:
    """Build a launch only from the exact fallback profile reserved by the host.
    `owned_paths` (#359) carries a replaced implementer's ownership over, and
    `compact_scope` (#388) a replaced compact reviewer's slices."""
    lib.validate_runtime_integrity(root)
    if role not in lib.SELECTABLE_AGENT_ROLES or not isinstance(profile, dict) \
            or set(profile) != {"adapter", "model"}:
        raise lib.HandsoffError("reserved fallback profile is invalid")
    if not isinstance(task, str) or not task.strip() \
            or len(task.encode("utf-8")) > MAX_REPLACEMENT_INPUT_BYTES:
        raise lib.HandsoffError(
            f"replacement input must be non-empty and at most {MAX_REPLACEMENT_INPUT_BYTES} UTF-8 bytes"
        )
    adapter = profile.get("adapter")
    if adapter == lib.HOST_AGENT_ADAPTER:
        raise lib.HandsoffError(f"{role} is host-driven; run the Supervisor CLI directly instead of launching a managed session")
    model = lib.validate_agent_model(profile.get("model"))
    if adapter not in lib.SELECTABLE_AGENT_ADAPTERS and adapter not in lib.CONTRACT_AGENT_ADAPTERS:
        raise lib.HandsoffError("reserved fallback adapter is invalid")
    cfg = lib.load_config(root)
    status_file = lib.status_path(root, cfg)
    status = lib.load_unique_json(status_file) if status_file.is_file() else {}
    # #305: a reserved fallback onto a local adapter meets the same floors.
    adapters.enforce_local_floors(cfg, role, status.get("risk_class"), {"adapter": adapter, "model": model})
    policy = status.get("model_policy", cfg.get("model_policy", lib.DEFAULT_MODEL_POLICY))
    if not lib.model_policy_allows(policy, adapter, model):
        raise lib.HandsoffError(f"reserved fallback {adapter}/{model} is denied by the mission model policy")
    executable = _contract_executable(cfg, adapter) if adapter in lib.CONTRACT_AGENT_ADAPTERS \
        else which(adapter)
    if not executable:
        raise lib.HandsoffError(f"reserved {adapter} executable is no longer available")
    executable = str(Path(executable).resolve())
    # #352: plan_role_token_budget below decides the ceiling, exactly as on
    # the primary path.
    configured_budget = cfg["agent_token_budgets"][role]
    scope = lib.validate_compact_review_scope(list(compact_scope)) if compact_scope else None
    if scope and role != "reviewer":
        raise lib.HandsoffError("--compact-scope applies to reviewer launches only")
    isolation = lib.reviewer_isolation_contract(adapter, cfg) if role == "reviewer" else None
    if isolation is not None and isolation["decision"] != "enforce":
        raise lib.HandsoffError(
            f"reviewer fallback refused before session reservation: {isolation['reason']} "
            f"(adapter={adapter}, decision={isolation['decision']})"
        )
    _refuse_oversized_implementation_review(root, cfg, status, role, scope,
                                            "managed fallback launch refused before reservation")
    # #343 REQ-004/REQ-008: before the scratch directory, before the session,
    # before the review attempt. A Claude reviewer that cannot read the project
    # produced a packet-only review that still SPENT one of the two autonomous
    # attempts, so the refusal has to land where nothing has been consumed yet.
    # Gated on this exact pair: architect and supervisor share the reviewer's
    # empty allowlist and run from the project root, and a probe that applied
    # to every role would refuse launches #343 never asked to change.
    if role == "reviewer" and adapter == "claude":
        # #388: a compact reviewer never reads the project, so there is
        # nothing for the probe to check.
        access = {"readable": True} if scope else lib.reviewer_read_access(root)
        if not access["readable"]:
            raise lib.HandsoffError(
                "managed Claude reviewer launch refused before session reservation: "
                + access["reason"])
    scratch = _reviewer_scratch(root, adapter, role)
    with _scratch_released_on_refusal(scratch, created=True):
        if scope:
            lib.materialize_compact_scope(root, scope, scratch)
        # #290: the codex argv is built once, below, after the budget decision
        # exists, so the meter can be set to the provider limit. Building it
        # here as well left a second copy metered on the whole ceiling.
        argv = []
        if adapter != "codex" and adapter not in lib.CONTRACT_AGENT_ADAPTERS:
            allowed = [tool for tool in _claude_allowed_tools(cfg, root, role)
                       if not (scope and tool == "Bash")]
            argv = lib.claude_argv(executable, role, allowed, model, project_root=root if not scope else None,
                                   owned_paths=list(owned_paths) if owned_paths else None)
            if scope:
                argv += ["--disallowedTools", ",".join(_compact_denied_tools(root))]
        budget_decision = lib.plan_role_token_budget(
            configured_ceiling=configured_budget, role=role,
            # The failover path. #347 shipped with one of its two launch builders
            # threaded and the other not, which left every failed-over role on the
            # old behaviour; the same mistake here would mean an explicit budget
            # silently stops applying the moment a role fails over once.
            configured_explicitly=role in cfg["agent_token_budgets_explicit"],
            risk_class=status.get("risk_class"), packet_bytes=len(task.encode("utf-8")),
            followup=True,
        )
        token_budget = budget_decision["ceiling"]
        provider_limit = budget_decision["provider_limit"]
        # #290: measured against what the provider is actually given.
        if provider_limit < budget_decision["safe_minimum"]:
            raise lib.HandsoffError(
                "managed fallback launch refused before reservation: rendered packet estimates "
                f"{budget_decision['estimated_input_tokens']} input tokens, reserves "
                f"{budget_decision['response_reserve_tokens']} response tokens, and requires at least "
                f"{budget_decision['safe_minimum']} tokens; selected ceiling is {token_budget}"
            )
        if adapter == "codex":
            # #290: the meter is set to the PROVIDER LIMIT, as codex_argv's
            # contract requires. Passing the whole ceiling here left a fallback
            # session free to spend the entire allowance before stopping, with
            # nothing held back for its verdict: the very overshoot this lane
            # exists to bound, on the path taken precisely when a session has
            # already failed once.
            argv = _codex_argv(executable, role, model, provider_limit,
                               reviewer_sandbox=scratch is not None,
                               reasoning=cfg["model_reasoning"].get(role))
        elif adapter in lib.CONTRACT_AGENT_ADAPTERS:
            argv = adapters.build_argv(adapter, executable, role, model,
                                       provider_limit=provider_limit, project_root=root)
        if not skip_preflight and os.environ.get("HANDSOFF_SKIP_PREFLIGHT") != "1":
            if adapter in lib.CONTRACT_AGENT_ADAPTERS:
                _contract_preflight(root, cfg, adapter, model, executable, argv, str(scratch or root),
                                    "reserved fallback preflight blocked")
            else:
                preflight = lib.launch_preflight(
                    root, adapter=adapter, model=model, executable=executable,
                    argv=argv, cwd=str(scratch or root),
                )
                if preflight["state"] != "ready":
                    raise lib.HandsoffError(
                        f"reserved fallback {adapter}/{model} preflight blocked "
                        f"({preflight['category']}): {preflight['reason']}"
                    )
    return LaunchSpec(
        role, adapter, model, tuple(argv), str(scratch or root.resolve()), task, "fallback",
        token_budget=token_budget,
        provider_limit=provider_limit,
        ceiling_enforcement=lib.adapter_ceiling_enforcement(adapter),
        env_overrides={"TMPDIR": str(scratch)} if scratch else None,
        project_root=str(root.resolve()),
        budget_decision=budget_decision,
        reviewer_isolation=isolation,
        reasoning_effort=cfg["model_reasoning"].get(role),
        routing_contract=adapters.session_contract(adapters.catalog_profile(cfg, adapter, model), token_budget),
        owned_paths=tuple(owned_paths) if owned_paths else None,
        compact_scope=tuple(scope) if scope else None,
    )


def _stop_process_group(process) -> None:
    """Bounded TERM→KILL shutdown for the fresh session we created."""
    pid = getattr(process, "pid", None)
    group_signalled = False
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
            group_signalled = True
        except (OSError, ProcessLookupError):
            pass
    if not group_signalled:
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise lib.HandsoffError("agent process group did not stop after SIGKILL") from exc


def _check_operation_timeout(root: Path, cfg: dict, session_id: str, process,
                             now: datetime) -> str:
    """Terminate only a still-proven child after its declared external timeout.

    Every ownership fact is read immediately before signalling because a PID
    can be reused after the original child exits or changes process groups.
    """
    operation = lib.current_operation(root, session_id)
    if not operation or lib.operation_assessment(operation, now, 0) != "timed_out":
        return "unverified"
    started = datetime.fromisoformat(operation["started_at"])
    deadline = started + timedelta(seconds=operation["timeout_seconds"])
    grace = cfg.get("recovery", {}).get("operation_grace_seconds", 120)
    if (now - deadline).total_seconds() < grace:
        return "unverified"
    beacon = lib.read_live_beacon(root)
    pid = getattr(process, "pid", None)
    if not isinstance(beacon, dict) or beacon.get("session_id") != session_id \
            or beacon.get("state") not in {"started", "running"} or beacon.get("pid") != pid:
        return "unverified"
    age = lib._seconds_since(beacon.get("beacon_at"), now)
    if age is None or age < 0 or age > lib.LIVE_BEACON_FRESH_SECONDS or not isinstance(pid, int) or pid <= 1:
        return "unverified"
    try:
        if os.getpgid(pid) != pid:
            return "unverified"
        os.killpg(pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(pid, signal.SIGKILL)
        return "terminated"
    except (OSError, ProcessLookupError):
        return "unverified"


def _failure_exit_code(process) -> int:
    value = getattr(process, "returncode", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value != 0 else 1


def _terminalize_runner_io_failure(root: Path, session_id: str, process, *, stop_first: bool) -> None:
    """Bound child cleanup and record exactly one failed terminal transition."""
    try:
        if stop_first:
            _stop_process_group(process)
        else:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process_group(process)
    finally:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=_failure_exit_code(process),
            failure=lib.classify_runtime_failure(exit_code=_failure_exit_code(process)),
        )


#: #408: shell operators inside one shlex punctuation token (`&;`, `2>&1`),
#: longest first; the separators end a simple command, the rest redirect.
_SHELL_OPERATOR = re.compile(r"&&|\|\||;;|\|&|&>>|&>|>&|<&|>>|<<|<>|>\||[;&|()<>]")
_SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_BACKGROUND_ID = re.compile(r"\bID:\s*([A-Za-z0-9_.-]+)")
_DONE_STATUS = r"(?:completed|failed|killed|exited|stopped)"
_BACKGROUND_DONE = re.compile(r"<status>\s*" + _DONE_STATUS + r"\s*</status>|"
                              r"\bstatus:\s*" + _DONE_STATUS + r"\b", re.IGNORECASE)
_TASK_NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_TASK_ID = re.compile(r"<(?:task|shell|bash)[-_]id>\s*([A-Za-z0-9_.-]+)\s*</(?:task|shell|bash)[-_]id>")


class _Script:
    """#408: the text a `_ShellLexer` reads, one character at a time. An
    unquoted newline reads as `;` (it ends a command), `#` starts a comment
    only at a word start, and a comment stops before its newline."""

    def __init__(self, text: str):
        self.text, self.pos, self.lexer = text, 0, None

    def read(self, _size: int = 1) -> str:
        state = self.lexer.state
        if state == "c":
            self.lexer.operator = True  # the token being read is unquoted punctuation
        if self.pos >= len(self.text):
            return ""
        char = self.text[self.pos]
        self.pos += 1
        if state in ("'", '"', "\\"):
            return char
        if char == "\n":
            return ";"
        self.lexer.commenters = "" if state == "a" else "#"
        return char

    def readline(self) -> str:
        end = self.text.find("\n", self.pos)
        self.pos = len(self.text) if end < 0 else end
        return ""

    def close(self) -> None:
        pass


class _ShellLexer(shlex.shlex):
    """#408: shlex in POSIX mode with punctuation characters, which also says
    whether each token was an unquoted operator (`'&'` and `\\&` are words)."""

    def __init__(self, text: str):
        script = _Script(text)
        super().__init__(script, posix=True, punctuation_chars=True)
        script.lexer = self
        self.whitespace_split = True
        self.operator = False

    def read_token(self):
        self.operator = False
        return super().read_token()


def _simple_commands(script: str):
    """#408: (words, separator) for each simple command, redirections and
    their targets dropped. Raises ValueError for text shlex cannot read."""
    lexer, words, redirect = _ShellLexer(script), [], False
    while (token := lexer.get_token()) is not None:
        if not lexer.operator:
            if not redirect:
                words.append(token)
            redirect = False
            continue
        for operator in _SHELL_OPERATOR.findall(token):
            if operator in _SEPARATORS:
                yield words, operator
                words, redirect = [], False
            else:
                if words and words[-1].isdigit():
                    words.pop()  # the descriptor of `2>&1`
                redirect = True
    yield words, None


def _outstanding(script: str) -> int:
    """#408: how many processes `script` leaves running when it returns.
    Each simple command ended by `&` is a job and `$!` names the latest.
    `wait` collects every job not disowned, `wait $!` the latest, `wait %N`
    job N, any other operand nothing; `disown` keeps a job running but out
    of the wait set. `nohup` and `setsid` jobs are waitable like any other;
    `setsid -f` and a nested `bash -c` that leaves work running are
    outstanding whatever waits. A single-quoted `'$!'` collects nothing."""
    jobs: list[dict] = []
    pinned = 0
    # A single-quoted '$!' is literal text, not the latest job; POSIX shlex
    # drops the quotes, so mark it before tokenizing (it collects nothing).
    script = script.replace("'$!'", "__literal_dollar_bang__")
    for words, separator in _simple_commands(script):
        if not words:
            continue
        name, operands = Path(words[0]).name, words[1:]
        if separator == "&":
            # nohup and setsid (without -f) still run as the shell's child,
            # so a later wait collects them like any other job
            jobs.append({"waitable": True, "collected": False})
            continue
        if name == "setsid" and any(word == "--fork" or (word.startswith("-") and not word.startswith("--")
                                                          and "f" in word) for word in operands):
            pinned += 1
        elif name in _SHELLS and len(operands) >= 2 and operands[0].startswith("-") \
                and not operands[0].startswith("--") and "c" in operands[0]:
            pinned += _outstanding(operands[1])
        elif name in ("wait", "disown"):
            if not operands:
                chosen = jobs if name == "wait" else jobs[-1:]
            elif name == "disown" and operands in (["-a"], ["-r"]):
                chosen = jobs
            else:
                chosen = []
                for operand in operands:
                    if operand == "$!":
                        chosen += jobs[-1:]
                    elif re.fullmatch(r"%[1-9][0-9]*", operand) and int(operand[1:]) <= len(jobs):
                        chosen.append(jobs[int(operand[1:]) - 1])
            for job in chosen:
                if name == "disown":
                    job["waitable"] = False
                elif job["waitable"]:
                    job["collected"] = True
    return pinned + sum(1 for job in jobs if not job["collected"])


def _detaches(command: str) -> bool:
    """#408: the command leaves a process running after it returns. A
    command shlex cannot read is not background work, so it never exempts
    a failure from the review budget."""
    try:
        return _outstanding(command) > 0
    except ValueError:
        return False


class _BackgroundWork:
    """#408: background work a managed role started, read from its streamed
    events: a tool call the stream marks run_in_background, or a command that
    backgrounds a process outside quotes with no later wait. A background
    call is closed when the stream reports it finished: the background task
    notification, or a BashOutput/TaskOutput poll or KillShell result saying
    so. A detached process is never seen to finish. `outstanding` is what was
    still running at the session's last turn."""

    def __init__(self):
        self._open: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._polls: dict[str, tuple[str, str]] = {}

    @property
    def outstanding(self) -> list[str]:
        return sorted(self._open)

    def feed(self, line: str) -> None:
        text = line.strip()
        if not text.startswith("{"):
            return
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        item = event.get("item")
        if event.get("type") == "item.started" and isinstance(item, dict) \
                and item.get("type") == "command_execution" and isinstance(item.get("command"), str) \
                and _detaches(item["command"]):
            self._open[str(item.get("id") or len(self._open))] = "detached"
        if event.get("type") == "system" and isinstance(event.get("task_id"), str) \
                and isinstance(event.get("status"), str) and re.fullmatch(_DONE_STATUS, event["status"]):
            self._close(event["task_id"])  # the stream's own task notification
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            self._notifications(content)
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self._tool_use(block)
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                self._tool_result(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                self._notifications(block["text"])

    def _tool_use(self, block: dict) -> None:
        call_id = str(block.get("id") or "")
        name = block.get("name")
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        target = next((args[key] for key in ("bash_id", "shell_id", "task_id")
                       if isinstance(args.get(key), str)), None)
        if args.get("run_in_background") is True:
            self._open[call_id] = "background"
        elif isinstance(args.get("command"), str) and _detaches(args["command"]):
            self._open[call_id] = "detached"
        elif name in ("BashOutput", "TaskOutput") and target:
            self._polls[call_id] = ("poll", target)
        elif name in ("KillShell", "KillBash", "TaskStop") and target:
            self._polls[call_id] = ("kill", target)

    def _tool_result(self, block: dict) -> None:
        call_id = str(block.get("tool_use_id") or "")
        body = block.get("content")
        if isinstance(body, list):
            body = "\n".join(str(part.get("text") or "") for part in body if isinstance(part, dict))
        body = body if isinstance(body, str) else ""
        if self._open.get(call_id) == "background":
            match = _BACKGROUND_ID.search(body)
            if match:
                self._aliases[match.group(1)] = call_id
        kind, target = self._polls.pop(call_id, (None, None))
        if (kind == "kill" and block.get("is_error") is not True) \
                or (kind == "poll" and _BACKGROUND_DONE.search(body)):
            self._close(target)
        self._notifications(body)

    def _notifications(self, text: str) -> None:
        """Claude reports a finished background task as a
        `<task-notification>` naming its id and a terminal status."""
        for note in _TASK_NOTIFICATION.findall(text):
            task = _TASK_ID.search(note)
            if task and _BACKGROUND_DONE.search(note):
                self._close(task.group(1))

    def _close(self, target: str) -> None:
        key = self._aliases.get(target, target)
        if self._open.get(key) == "background":
            self._open.pop(key)


class _ChunkReader:
    """#41: read a child's text pipe as output ARRIVES, not in 4096-character
    blocks. `TextIOWrapper.read(n)` blocks until n characters or EOF, so a
    child printing a short line every few seconds looked silent until it
    exited. The underlying buffer's `read1` returns whatever is available;
    an incremental decoder keeps a multibyte character split across two
    reads intact. A stream without a buffer (test doubles) falls back to
    `read(4096)` unchanged."""

    def __init__(self, stream, size: int = 4096):
        self.stream = stream
        self.size = size
        buffer = getattr(stream, "buffer", None)
        self.read1 = getattr(buffer, "read1", None) if buffer is not None else None
        if callable(self.read1):
            encoding = getattr(stream, "encoding", None) or "utf-8"
            errors = getattr(stream, "errors", None) or "strict"
            self.decoder = codecs.getincrementaldecoder(encoding)(errors)
        else:
            self.read1 = None
            self.decoder = None

    def read(self) -> str:
        """Empty only at EOF: a read that lands mid-character yields no text
        yet, so keep reading rather than mistake it for the end."""
        if self.read1 is None:
            return self.stream.read(self.size)
        while True:
            data = self.read1(self.size)
            text = self.decoder.decode(data, final=not data)
            if text or not data:
                return text


def _process_pid(process) -> int | None:
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


class _LiveBeacon:
    """#33: a daemon thread that writes `.handsoff-live.json` every
    `interval` seconds while the child runs, plus one final write after the
    terminal `transition_agent_session`. Every write is best effort: an
    OSError is swallowed inside `lib.write_live_beacon`, a thread that
    cannot start is ignored, and nothing here can change the child's
    lifecycle, the session record, or the return value of execute_launch.
    The beacon carries identifiers, integers, and timestamps only."""

    def __init__(self, root: Path, session_id: str, role: str, process, interval: float,
                 per_session: bool = False):
        self.root = root
        self.session_id = session_id
        self.role = role
        self.per_session = per_session  # #359: also beacon the session's own file
        self.process = process
        self.pid = _process_pid(process)
        self.interval = max(float(interval), 0.01)
        self.stop = threading.Event()
        self.thread = None
        self.operation_terminated = False

    def _beat(self) -> None:
        while not self.stop.is_set():
            lib.write_live_beacon(self.root, session_id=self.session_id, role=self.role,
                                  state="running", pid=self.pid, per_session=self.per_session)
            if _check_operation_timeout(self.root, lib.load_config(self.root), self.session_id,
                                        self.process, datetime.now(timezone.utc)) == "terminated":
                self.operation_terminated = True
            self.stop.wait(self.interval)

    def start(self) -> None:
        try:
            self.thread = threading.Thread(target=self._beat, daemon=True)
            self.thread.start()
        except Exception:
            self.thread = None

    def finish(self) -> None:
        """Stop the periodic writer, then write the final beacon from the
        session record the terminal transition just committed, which is the
        authority on the terminal state and the child's exit code."""
        self.stop.set()
        try:
            if self.thread is not None:
                self.thread.join(timeout=5)
        except Exception:
            pass
        try:
            cfg = lib.load_config(self.root)
            record = lib.load_unique_json(lib.status_path(self.root, cfg)).get("agent_sessions", {}).get(self.session_id)
        except Exception:
            record = None
        if not isinstance(record, dict) or record.get("state") not in lib.AGENT_SESSION_TERMINAL_STATES:
            return
        # #345: a verdict the launcher adopted keeps the #172 adopted beacon
        adopted = isinstance(record.get("result"), dict) and record["result"].get("adopted_at")
        lib.write_live_beacon(
            self.root, session_id=self.session_id, role=self.role,
            state="adopted" if adopted else record["state"],
            pid=None if adopted else self.pid, ended_at=record.get("ended_at"), exit_code=record.get("exit_code"),
            per_session=self.per_session,
        )


_AUTH_PATTERN = re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)\S+")
_COOKIE_PATTERN = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)([\"']?[A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTH|COOKIE|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]*[\"']?\s*[=:]\s*[\"']?)[^\s,}\"']+"
)
_TOKEN_PATTERNS = (
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)
_URL_USERINFO_PATTERN = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")
_SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|PRIVATE|PASSPHRASE)"
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----")
_CONTROL_PREFIXES = (SUPERVISOR_REQUEST_PREFIX, REVIEW_RESULT_PREFIX, "HANDSOFF_QUESTION:",
                     "HANDSOFF_OPERATION:")
_SAFE_REDACTION_FAILURE = "[OUTPUT REDACTION FAILED]"
_SAFE_OVERSIZED_OUTPUT = "[OVERSIZED OUTPUT REDACTED]"


def _sensitive_environment_values(environment: dict[str, str]) -> tuple[str, ...]:
    values = {
        value for name, value in environment.items()
        if _SENSITIVE_ENV_NAME.search(str(name)) and isinstance(value, str) and len(value) >= 4
    }
    return tuple(sorted(values, key=len, reverse=True))


def _reviewer_environment(environment: dict[str, str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Pass operational variables but strip every credential-shaped name."""
    cleaned = {name: value for name, value in environment.items()
               if not _SENSITIVE_ENV_NAME.search(str(name))}
    cleaned.update(overrides or {})
    return cleaned


def _redact_output_line(line: str, prompt_lines: set[str], prompt_fragments: tuple[str, ...],
                        sensitive_values: tuple[str, ...]) -> str:
    """Host-side redaction before output can touch portable storage."""
    try:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _CONTROL_PREFIXES):
            return "[HANDSOFF CONTROL MESSAGE REDACTED]"
        if stripped and stripped in prompt_lines:
            return "[ASSIGNED PROMPT ECHO REDACTED]"
        redacted = line
        for fragment in prompt_fragments:
            redacted = redacted.replace(fragment, "[ASSIGNED PROMPT REDACTED]")
        for value in sensitive_values:
            redacted = redacted.replace(value, "[REDACTED]")
        redacted = _AUTH_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        redacted = _COOKIE_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        redacted = _ASSIGNMENT_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        redacted = _URL_USERINFO_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]@", redacted)
        return lib.redact_output_text(redacted)
    except Exception:
        return _SAFE_REDACTION_FAILURE


class _PortableOutput:
    """Fail-closed redaction plus bounded, batched persistence for #59/#60."""

    def __init__(self, root: Path, session_id: str, role: str, adapter: str, prompt: str,
                 environment: dict[str, str]):
        self.root = root
        self.session_id = session_id
        self.role = role
        self.adapter = adapter
        self.pending = {"stdout": "", "stderr": ""}
        self.private_key_block = {"stdout": False, "stderr": False}
        self.source_bytes = 0
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.stop = threading.Event()
        self.queue: list[dict] = []
        self.queue_bytes = 0
        self.write_count = 0
        self.prompt_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
        self.prompt_fragments = tuple(sorted(
            (line for line in self.prompt_lines if len(line) >= 12), key=len, reverse=True,
        ))
        self.sensitive_values = _sensitive_environment_values(environment)
        lib.start_agent_output(root, session_id, role, adapter)
        self.flusher = threading.Thread(target=self._flush_loop, daemon=True)
        try:
            self.flusher.start()
        except Exception:
            self.flusher = None

    def _safe_line(self, stream: str, line: str) -> str | None:
        try:
            if self.private_key_block[stream]:
                if _PRIVATE_KEY_END.search(line):
                    self.private_key_block[stream] = False
                return None
            if _PRIVATE_KEY_BEGIN.search(line):
                self.private_key_block[stream] = not bool(_PRIVATE_KEY_END.search(line))
                return "[PRIVATE KEY BLOCK REDACTED]"
            return _redact_output_line(
                line, self.prompt_lines, self.prompt_fragments, self.sensitive_values,
            )
        except Exception:
            return _SAFE_REDACTION_FAILURE

    def _enqueue_locked(self, stream: str, line: str) -> bool:
        safe = self._safe_line(stream, line.rstrip("\r"))
        if safe is None:
            return False
        safe = safe[:lib.MAX_AGENT_OUTPUT_LINE_CHARS]
        self.queue.append({
            "at": datetime.now(timezone.utc).isoformat(), "stream": stream, "text": safe,
        })
        self.queue_bytes += len(safe.encode("utf-8", "replace"))
        return (len(self.queue) >= lib.AGENT_OUTPUT_FLUSH_MAX_ENTRIES
                or self.queue_bytes >= lib.AGENT_OUTPUT_FLUSH_MAX_BYTES)

    def _drain(self) -> None:
        with self.flush_lock:
            while True:
                with self.lock:
                    if not self.queue:
                        return
                    batch = self.queue[:lib.AGENT_OUTPUT_FLUSH_MAX_ENTRIES]
                    del self.queue[:len(batch)]
                    self.queue_bytes = sum(len(item["text"].encode("utf-8", "replace"))
                                           for item in self.queue)
                    source_bytes = self.source_bytes
                if lib.append_agent_output_batch(
                        self.root, self.session_id, batch, source_bytes):
                    self.write_count += 1

    def _flush_loop(self) -> None:
        while not self.stop.wait(lib.AGENT_OUTPUT_FLUSH_INTERVAL_SECONDS):
            self._drain()

    def feed(self, stream: str, chunk: str) -> None:
        threshold = False
        with self.lock:
            self.source_bytes += len(chunk.encode("utf-8", "replace"))
            pending = self.pending[stream] + chunk
            lines = pending.split("\n")
            self.pending[stream] = lines.pop()
            for line in lines:
                threshold = self._enqueue_locked(stream, line) or threshold
            # Never persist a raw oversized partial line: it can contain a
            # prompt, private key, or secret split exactly at a chunk edge.
            if len(self.pending[stream].encode("utf-8", "replace")) > 65536:
                self.pending[stream] = ""
                threshold = self._enqueue_locked(stream, _SAFE_OVERSIZED_OUTPUT) or threshold
        if threshold:
            self._drain()

    def flush(self, stream: str) -> None:
        with self.lock:
            line = self.pending.get(stream, "")
            self.pending[stream] = ""
            if line:
                self._enqueue_locked(stream, line)

    def finish(self) -> None:
        self.stop.set()
        if self.flusher is not None:
            try:
                self.flusher.join(timeout=2)
            except Exception:
                pass
        for stream in ("stdout", "stderr"):
            self.flush(stream)
        self._drain()
        try:
            cfg = lib.load_config(self.root)
            session = lib.load_unique_json(lib.status_path(self.root, cfg)).get("agent_sessions", {}).get(self.session_id, {})
            terminal_state = session.get("state", "failed")
        except Exception:
            terminal_state = "failed"
        lib.finish_agent_output(self.root, self.session_id, terminal_state)


def execute_launch(spec: LaunchSpec, *, timeout: int = 3600, actor: str | None = None,
                   popen_factory=subprocess.Popen, session_id_factory=None,
                   precreated_session_id: str | None = None,
                   beacon_interval: float = lib.LIVE_BEACON_INTERVAL_SECONDS) -> int:
    """Run one managed session and record its lifecycle without payload data.

    `beacon_interval` (seconds) paces the #33 liveness beacon; tests inject
    a short one. The beacon never gates or alters the lifecycle below.

    #41: stdout is captured for EVERY role (claude and codex children write
    their work to stdout) and streamed to this process's stdout unchanged;
    only the supervisor role's lines are parsed for broker requests. Each
    stdout/stderr chunk notes output liveness, a counter file that never
    carries content and never reaches a ledger."""
    root = Path(spec.project_root or spec.cwd).resolve()
    actor = lib.validate_agent_actor(actor or lib.default_agent_actor(spec.adapter, spec.role))
    if precreated_session_id is None:
        isolation = spec.reviewer_isolation
        if spec.role == "reviewer" and isolation is None:
            isolation = lib.reviewer_isolation_contract(spec.adapter, lib.load_config(root))
            if isolation["decision"] != "enforce":
                raise lib.HandsoffError(f"reviewer launch refused before session reservation: {isolation['reason']}")
        session = lib.create_agent_session(
            root, role=spec.role, actor=actor, adapter=spec.adapter,
            requested_model=spec.model, resolution_source=spec.resolution_source,
            id_factory=session_id_factory, packet_id=spec.packet_id, design_hash=spec.design_hash,
            tier=spec.tier, tier_reason=spec.tier_reason, amendment_id=spec.amendment_id,
            adaptive_routing=spec.adaptive_routing,
            budget_decision=spec.budget_decision,
            reviewer_isolation=isolation,
            reasoning_effort=spec.reasoning_effort,
            routing_contract=spec.routing_contract,
            owned_paths=list(spec.owned_paths) if spec.owned_paths else None,
        )
    else:
        session = lib.claim_precreated_agent_session(
            root, precreated_session_id, role=spec.role, adapter=spec.adapter,
            requested_model=spec.model, routing_contract=spec.routing_contract,
        )
        actor = session["actor"]
    session_id = session["session_id"]
    if spec.compact_scope:
        # #388: recorded before the child starts, so a compact verdict can
        # never be mistaken for a review that could have run the tests.
        try:
            _record_session_fields(root, session_id, {"compact": True}, event_kind="agent_session_compact",
                                   event_message="Compact reviewer: only its slices, no tests",
                                   ranges=len(spec.compact_scope))
        except lib.HandsoffError as exc:
            lib.transition_agent_session(root, session_id, "failed_to_start",
                                         failure=lib.classify_runtime_failure(exit_code=-1))
            raise AgentLaunchError(f"compact session could not be recorded: {str(exc)[:160]}",
                                   session_id) from exc
    cwd = spec.cwd
    if isinstance(session.get("workspace"), dict):
        # #359: a concurrent implementer works in its own worktree outside
        # the project root, so it cannot write another session's files.
        try:
            cwd = str(lib.create_implementer_workspace(root, session))
        except (lib.HandsoffError, OSError, subprocess.SubprocessError) as exc:
            lib.remove_implementer_workspace(root, session)
            lib.transition_agent_session(root, session_id, "failed_to_start",
                                         failure=lib.classify_runtime_failure(exit_code=-1))
            raise AgentLaunchError(f"implementer workspace could not be created: {str(exc)[:160]}",
                                   session_id) from exc
    try:
        return _execute_started_session(spec, root, session, cwd, timeout=timeout,
                                        popen_factory=popen_factory, beacon_interval=beacon_interval)
    finally:
        # #383: a quarantined workspace is the result itself; adoption applies it.
        if session_id not in _HELD_WORKSPACES:
            lib.remove_implementer_workspace(root, session)
        if isinstance(session.get("workspace"), dict):
            lib.live_beacon_path(root, session_id).unlink(missing_ok=True)


def _execute_started_session(spec: LaunchSpec, root: Path, session: dict, cwd: str, *, timeout: int,
                             popen_factory, beacon_interval: float) -> int:
    capture_supervisor = spec.role == "supervisor"
    session_id = session["session_id"]
    child_env = (_reviewer_environment(os.environ.copy(), spec.env_overrides)
                 if spec.role == "reviewer" else os.environ.copy())
    child_env[MANAGED_ROLE_ENV] = spec.role
    child_env[MANAGED_SESSION_ENV] = session_id
    if spec.env_overrides and spec.role != "reviewer":
        child_env.update(spec.env_overrides)
    portable_output = _PortableOutput(
        root, session_id, spec.role, spec.adapter, spec.stdin, child_env,
    )
    if spec.role == "reviewer":
        # #203: one scan, both views: the digest is derived from the same
        # entries the changed-path list is built from, so a file that
        # exists only between two scans cannot change one and not the other.
        entries_before = lib.repository_digest_entries(root, lib.load_config(root))
        repository_digest_before = {"entries": entries_before,
                                    "digest": lib.repository_digest_from_entries(entries_before)}
    else:
        repository_digest_before = None
    try:
        process = popen_factory(
            list(spec.argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
            env=child_env,
        )
    except OSError as exc:
        failure = lib.classify_runtime_failure(exit_code=-1)
        lib.transition_agent_session(root, session_id, "failed_to_start", failure=failure)
        portable_output.finish()
        raise AgentLaunchError(
            f"{spec.adapter} process failed to start: {type(exc).__name__}", session_id,
        ) from exc
    beacon = _LiveBeacon(root, session_id, spec.role, process, beacon_interval,
                         per_session=isinstance(session.get("workspace"), dict))
    beacon.start()
    try:
        result = _run_managed_process(
            spec, root, session_id, process, timeout, capture_supervisor, portable_output,
            repository_digest_before=repository_digest_before,
            beacon=beacon, workspace=isinstance(session.get("workspace"), dict),
        )
        if spec.env_overrides and spec.role == "reviewer":
            shutil.rmtree(spec.cwd, ignore_errors=True)
        return result
    finally:
        beacon.finish()
        portable_output.finish()


def _run_managed_process(spec: LaunchSpec, root: Path, session_id: str, process,
                         timeout: int, capture_supervisor: bool,
                         portable_output: _PortableOutput,
                         repository_digest_before: str | None = None,
                         beacon: _LiveBeacon | None = None, workspace: bool = False) -> int:
    """The lifecycle of an already-started child: exactly one terminal
    transition on every path, no payload data recorded."""
    supervisor_requests: list[dict] = []
    reviewer_results: list[dict] = []
    recovered_results: list[dict] = []  # #167: packets refused by a rule, adoptable
    architect_results: list[dict] = []
    architect_declines: list[dict] = []
    architect_requests: list[dict] = []
    protocol_errors: list[str] = []
    question_errors: list[str] = []
    question_lines = [0]
    raised_questions: set[str] = set()  # #345: one record per question per launch
    stdout_tail = [""]
    stderr_tail = [""]
    background = _BackgroundWork()  # #408
    progress_done = [0]  # #408: the Implementer's result is a criterion reported done
    # #168: the adapter's own usage, watched per streamed line on both
    # streams, independent of the bounded tails above.
    usage_watcher = lib.UsageWatcher(spec.adapter)
    #: #290: an adapter with no native meter is bounded here instead. The
    #: session is stopped at the FIRST observation past its provider limit,
    #: so the overrun is bounded by one reported usage step rather than
    #: unbounded. The step we could not prevent is recorded, never hidden.
    ceiling_stop: dict = {"tripped": False, "observed": None, "limit": spec.provider_limit}

    #: #309: the observation boundary. The adapter announces its model once,
    #: in its opening banner; this fires on the first feed that sets it, so a
    #: RUNNING session names its model instead of naming it only once it has
    #: ended. A failure to persist never disturbs the session: the model is
    #: telemetry, and losing it must not cost the work.
    model_recorded = {"done": False}

    def record_model() -> None:
        if model_recorded["done"]:
            return
        model = usage_watcher.reported_model
        if not model:
            return
        model_recorded["done"] = True
        try:
            lib.record_reported_model(root, session_id, model)
        except (lib.HandsoffError, OSError):
            pass

    def enforce_ceiling() -> None:
        if spec.ceiling_enforcement != "wrapper_enforced" or ceiling_stop["tripped"]:
            return
        limit = spec.provider_limit
        usage = usage_watcher.usage
        if not isinstance(limit, int) or not isinstance(usage, dict):
            return
        total = usage.get("tokens_total")
        if not isinstance(total, int) or total <= limit:
            return
        ceiling_stop.update(tripped=True, observed=total)
        sys.stdout.write(
            f"HANDSOFF_AGENT_WARNING: bounded termination at {total} tokens, past the "
            f"{limit}-token provider limit for this {spec.role} session\n"
        )
        sys.stdout.flush()
        _stop_process_group(process)
    usage_enabled = lib.feature_enabled(lib.load_config(root), "token_accounting")

    def end_session(state, **kw):
        # every terminal transition of this child carries the usage seen so
        # far (read at call time: after the streams drained, or as far as
        # they got when the child was stopped)
        return lib.transition_agent_session(root, session_id, state,
                                            usage=usage_watcher.result(usage_enabled),
                                            reported_model=usage_watcher.reported_model,
                                            **kw)

    def persist_new(items, kind):
        if items:
            if kind == "review" and spec.compact_scope:
                # #388: a compact reviewer had no suite to run, whatever it wrote.
                items[-1]["tests_executed"] = "no"
            lib.record_session_result(root, session_id, kind, items[-1])
    try:
        lib.transition_agent_session(root, session_id, "running")
    except Exception:
        _stop_process_group(process)
        raise
    lib.update_session_liveness(root, session_id)
    liveness_interval = lib.load_config(root).get("recovery", {}).get("liveness_seconds", 60)

    def publish_liveness() -> None:
        # Liveness pings are advisory. A process object without poll()
        # (a test double that only exposes wait()) simply publishes none.
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return
        while poll() is None:
            try:
                lib.update_session_liveness(root, session_id)
            except Exception:
                pass  # liveness is conservative telemetry; lifecycle remains authoritative
            threading.Event().wait(liveness_interval)

    threading.Thread(target=publish_liveness, daemon=True).start()

    def note_output(chunk: str) -> None:
        # #41: identifiers and counters only, rate limited inside lib, every
        # OSError swallowed there; a failure here must never reach the
        # reader thread, the child, or the session record.
        try:
            nbytes = len(chunk) if isinstance(chunk, bytes) else len(chunk.encode("utf-8", "replace"))
            lib.note_output_liveness(root, session_id, spec.role, nbytes)
        except Exception:
            pass

    reader = None
    stderr_reader = None
    reader_errors: list[BaseException] = []
    stream_label = "Supervisor output stream" if capture_supervisor else "agent output stream"
    if getattr(process, "stdout", None) is not None:
        def stream_and_parse() -> None:
            try:
                pending = ""
                discarding = False
                chunks = _ChunkReader(process.stdout)
                while True:
                    chunk = chunks.read()
                    if not chunk:
                        break
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    note_output(chunk)
                    portable_output.feed("stdout", chunk)
                    stdout_tail[0] = (stdout_tail[0] + chunk)[-8192:]
                    if discarding:
                        if "\n" in chunk:
                            _, pending = chunk.split("\n", 1)
                            discarding = False
                        else:
                            continue
                    else:
                        pending += chunk
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        usage_watcher.feed(line)
                        background.feed(line)
                        record_model()
                        enforce_ceiling()
                        if _raise_question_line(root, spec.role, session_id, line, question_errors, raised_questions):
                            question_lines[0] += 1
                        _parse_operation_line(line, root, session_id, spec.role)
                        progress = _parse_progress_line(line, root, session_id, spec.role)
                        if progress and progress.get("state") == "done":
                            progress_done[0] += 1
                        if capture_supervisor:
                            _parse_supervisor_line(line, supervisor_requests, protocol_errors)
                            persist_new(supervisor_requests, "supervisor_request")
                        if spec.role == "reviewer":
                            _parse_reviewer_line(line, reviewer_results, protocol_errors, recovered_results, root)
                            persist_new(reviewer_results, "review")
                        if spec.role == "architect":
                            _parse_architect_request_line(line, architect_requests, protocol_errors)
                            _parse_architect_line(line, architect_results, protocol_errors)
                            _parse_architect_decline_line(line, architect_declines, protocol_errors)
                            persist_new(architect_results, "design")
                    if len(pending.encode("utf-8")) > 65536:
                        if pending.startswith(SUPERVISOR_REQUEST_PREFIX):
                            protocol_errors.append("Supervisor broker request exceeded 65536 bytes")
                        pending = ""
                        discarding = True
                if pending and not discarding:
                    usage_watcher.feed(pending)
                    background.feed(pending)
                    record_model()
                    enforce_ceiling()
                    if _raise_question_line(root, spec.role, session_id, pending, question_errors, raised_questions):
                        question_lines[0] += 1
                    _parse_operation_line(pending, root, session_id, spec.role)
                    if capture_supervisor:
                        _parse_supervisor_line(pending, supervisor_requests, protocol_errors)
                        persist_new(supervisor_requests, "supervisor_request")
                    if spec.role == "reviewer":
                        _parse_reviewer_line(pending, reviewer_results, protocol_errors, recovered_results, root)
                        persist_new(reviewer_results, "review")
                    if spec.role == "architect":
                        _parse_architect_request_line(pending, architect_requests, protocol_errors)
                        _parse_architect_line(pending, architect_results, protocol_errors)
                        _parse_architect_decline_line(pending, architect_declines, protocol_errors)
                        persist_new(architect_results, "design")
            except BaseException as exc:
                reader_errors.append(exc)
            finally:
                portable_output.flush("stdout")
                # Drained to EOF: release the pipe instead of leaving it to GC.
                try:
                    process.stdout.close()
                except Exception:
                    pass

        try:
            reader = threading.Thread(target=stream_and_parse, daemon=True)
            reader.start()
        except Exception as exc:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(
                f"{stream_label} failed to start: {type(exc).__name__}", session_id,
            ) from exc
    stderr_pending = [""]
    stderr_review_lines: list[str] = []

    def stderr_protocol_line(line: str) -> None:
        # #345: a question on stderr is recorded the moment it is read, so
        # long diagnostics after it cannot push it out of the tail; a
        # reviewer verdict there is kept whole for the failure path.
        if _raise_question_line(root, spec.role, session_id, line, question_errors, raised_questions):
            question_lines[0] += 1
        if spec.role == "reviewer" and line.startswith(REVIEW_RESULT_PREFIX):
            stderr_review_lines.append(line)

    def collect_stderr_verdicts() -> None:
        # #345: the last unterminated stderr line, and a reviewer's stderr
        # verdicts, are read before EITHER failure path (a non-zero exit, or a
        # protocol error on a clean exit), so a #167 refused packet on stderr
        # blocks adoption and a valid stderr verdict can be adopted. #405:
        # the timeout path reads them too. Runs once.
        if stderr_pending[0]:
            stderr_protocol_line(stderr_pending[0])
            stderr_pending[0] = ""
        if spec.role == "reviewer":
            while stderr_review_lines:
                line = stderr_review_lines.pop(0)
                # #114: Codex can repeat its final message on stderr; an
                # identical verdict there is the same verdict, not a second.
                parsed: list[dict] = []
                _parse_reviewer_line(line, parsed, protocol_errors, recovered_results, root)
                reviewer_results.extend(item for item in parsed if item not in reviewer_results)
                persist_new(reviewer_results, "review")

    if getattr(process, "stderr", None) is not None:
        def stream_stderr() -> None:
            try:
                chunks = _ChunkReader(process.stderr)
                while True:
                    chunk = chunks.read()
                    if not chunk:
                        break
                    sys.stderr.write(chunk)
                    sys.stderr.flush()
                    note_output(chunk)
                    portable_output.feed("stderr", chunk)
                    stderr_tail[0] = (stderr_tail[0] + chunk)[-8192:]
                    stderr_pending[0] += chunk
                    while "\n" in stderr_pending[0]:
                        line, stderr_pending[0] = stderr_pending[0].split("\n", 1)
                        usage_watcher.feed(line)
                        record_model()
                        enforce_ceiling()
                        stderr_protocol_line(line)
            except BaseException as exc:
                reader_errors.append(exc)
            finally:
                portable_output.flush("stderr")
                # Drained to EOF: release the pipe instead of leaving it to GC.
                try:
                    process.stderr.close()
                except Exception:
                    pass
        try:
            stderr_reader = threading.Thread(target=stream_stderr, daemon=True)
            stderr_reader.start()
        except Exception as exc:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(
                f"agent error stream failed to start: {type(exc).__name__}", session_id,
            ) from exc
    try:
        if reader or stderr_reader:
            # A child that exits (or closes its stdin) before the task is
            # written raises EPIPE here. That is the child's outcome, not
            # the runner's: swallow it, close our end, and wait for the
            # real exit status, exactly as communicate() does on the other
            # branch. Reported as "agent runner I/O failed: BrokenPipeError"
            # it hid a fake child's exit 3 on three CI runs (#179).
            try:
                process.stdin.write(spec.stdin)
            except BrokenPipeError:
                pass
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            process.wait(timeout=timeout)
        else:
            process.communicate(input=spec.stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        deliver = False
        try:
            _stop_process_group(process)
        finally:
            try:
                if reader:
                    reader.join(timeout=5)
                if stderr_reader:
                    stderr_reader.join(timeout=5)
                # #405: a verdict printed before the timeout is delivered,
                # not discarded with the session (the tree check first).
                if spec.role == "reviewer" and not reader_errors:
                    collect_stderr_verdicts()
                    deliver = _one_valid_verdict(spec, reviewer_results, recovered_results) \
                        and _reviewer_tree_failure(spec, root, session_id, repository_digest_before) is None
            finally:
                if not deliver:
                    end_session(
                        "timed_out", exit_code=124,
                        failure=lib.classify_runtime_failure(timed_out=True),
                    )
        if deliver:
            return _deliver_reviewer_result(root, session_id, spec, reviewer_results[0], end_session, 124)
        raise AgentLaunchError(f"agent launch timed out after {timeout} seconds", session_id) from exc
    except KeyboardInterrupt as exc:
        try:
            _stop_process_group(process)
        finally:
            try:
                if reader:
                    reader.join(timeout=5)
                if stderr_reader:
                    stderr_reader.join(timeout=5)
            finally:
                end_session(
                    "cancelled", exit_code=130,
                    failure=lib.classify_runtime_failure(cancelled=True),
                )
        raise AgentLaunchError("agent launch cancelled", session_id) from exc
    except Exception as exc:
        _terminalize_runner_io_failure(
            root, session_id, process, stop_first=not isinstance(exc, BrokenPipeError),
        )
        raise AgentLaunchError(f"agent runner I/O failed: {type(exc).__name__}", session_id) from exc
    # #203: the child has exited, so what the readers are still doing is
    # draining a pipe the kernel already holds; on a loaded runner that
    # drain (tail writes, usage parsing) can outlast a 5 s join. The child's
    # exit is the outcome, so the drain gets a budget of its own; only a
    # grandchild that keeps the pipe open past it is "did not close".
    if reader:
        try:
            reader.join(timeout=READER_DRAIN_SECONDS)
            reader_alive = reader.is_alive()
        except KeyboardInterrupt as exc:
            try:
                _stop_process_group(process)
            finally:
                end_session(
                    "cancelled", exit_code=130,
                    failure=lib.classify_runtime_failure(cancelled=True),
                )
            raise AgentLaunchError("agent launch cancelled", session_id) from exc
        except Exception as exc:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(
                f"{stream_label} failed: {type(exc).__name__}", session_id,
            ) from exc
        if reader_alive:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(f"{stream_label} did not close", session_id)
        if reader_errors:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=False)
            raise AgentLaunchError(
                f"{stream_label} failed: {type(reader_errors[0]).__name__}", session_id,
            ) from reader_errors[0]
    if stderr_reader:
        try:
            stderr_reader.join(timeout=READER_DRAIN_SECONDS)
            stderr_alive = stderr_reader.is_alive()
        except Exception as exc:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(
                f"agent error stream failed: {type(exc).__name__}", session_id,
            ) from exc
        if stderr_alive:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError("agent error stream did not close", session_id)
        if reader_errors:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=False)
            raise AgentLaunchError(
                f"agent output stream failed: {type(reader_errors[0]).__name__}", session_id,
            ) from reader_errors[0]
    requested_identity = lib._canonical_provider_model(spec.model)
    reported_identity = lib._canonical_provider_model(usage_watcher.reported_model)
    if requested_identity not in {None, lib.DEFAULT_AGENT_MODEL} and reported_identity is not None \
            and reported_identity != requested_identity:
        # Provider identity is authoritative once supplied.  Refuse before
        # any structured result is dispatched so a denied or silently
        # substituted model can never contribute governed evidence.
        failure = {
            "category": "model_identity_mismatch",
            "reason": "provider reported a different model than requested",
            "tail_sha256": hashlib.sha256(b"").hexdigest(),
        }
        end_session("failed", exit_code=process.returncode or 1, failure=failure)
        raise AgentLaunchError(
            f"provider model identity mismatch: requested {requested_identity}, reported {reported_identity}",
            session_id,
        )
    if spec.role == "reviewer":
        # #345: before any ending can adopt a verdict, so a reviewer that
        # wrote to the tree never has its verdict recorded.
        failure = _reviewer_tree_failure(spec, root, session_id, repository_digest_before)
        if failure is not None:
            end_session("failed", exit_code=1, failure=failure)
            raise AgentLaunchError("Reviewer modified the project tree: " + ", ".join(
                f"{c['path']} {c['kind']}" for c in failure["changes"][:4]) or "Reviewer modified the project tree", session_id)
    collect_stderr_verdicts()
    if process.returncode:
        # #345: stderr questions were recorded as read; the tail rescan is a
        # fallback, and the launch's de-duplication keeps a question seen
        # on both streams, or twice here, to one record.
        tail_lines = stderr_tail[0].splitlines()
        if len(stderr_tail[0]) >= 8192 and tail_lines:
            tail_lines = tail_lines[1:]  # the first line of a full tail may be cut
        for line in tail_lines:
            if _raise_question_line(root, spec.role, session_id, line, question_errors, raised_questions):
                question_lines[0] += 1
        if spec.role == "architect" and not architect_results and not architect_declines:
            for line in stderr_tail[0].splitlines():
                if line.startswith(DESIGN_RESULT_PREFIX):
                    _parse_architect_line(line, architect_results, protocol_errors)
                    persist_new(architect_results, "design")
                elif spec.role == "architect" and line.startswith(DECLINE_RESULT_PREFIX):
                    _parse_architect_decline_line(line, architect_declines, protocol_errors)
        if spec.role == "architect":
            # #361: a refused proposal is named on this path too; a non-zero
            # exit after it must not hide the validation diagnostic.
            refused = next((error for error in protocol_errors
                            if error.startswith("invalid Architect design proposal")), None)
            if refused:
                _end_protocol_refused(end_session, refused, session_id)
        if beacon is not None and beacon.operation_terminated:
            operation = lib.current_operation(root, session_id) or {}
            failure = {"category": "external_timeout",
                       "reason": "external operation exceeded its declared timeout",
                       "dependency": operation.get("dependency"),
                       "operation": operation.get("operation"),
                       "tail_sha256": hashlib.sha256(b"").hexdigest()}
            end_session("failed", exit_code=process.returncode,
                                        failure=failure)
            raise AgentLaunchError(failure["reason"], session_id)
        failure = lib.classify_runtime_failure(
            exit_code=process.returncode, stderr_tail=stderr_tail[0], stdout_tail=stdout_tail[0],
        )
        complete_protocol = not protocol_errors and (
            (spec.role == "architect" and len(architect_results) + len(architect_declines) == 1)
            or (spec.role == "reviewer" and len(reviewer_results) == 1)
            or (capture_supervisor and bool(supervisor_requests))
            or question_lines[0] > 0
        )
        # #290: name why the budget ended, because the repairs differ. Most
        # specific first: our own bound, then output we could not parse, then
        # the provider's meter, then a session that never spoke at all.
        # #290: measure the bound's real cost, on every path. A per-turn
        # meter stops within one turn of the limit, and that turn's size is
        # the overshoot; recording it is what lets the reserve be sized from
        # evidence instead of assumption.
        failure["ceiling_overshoot_tokens"] = lib.ceiling_overshoot(
            usage_watcher.usage, spec.provider_limit)
        if ceiling_stop["tripped"]:
            failure["budget_cause"] = "wrapper_overrun"
        elif protocol_errors:
            failure["budget_cause"] = "model_noncompliance"
        elif failure["category"] == "token_budget_exhaustion":
            failure["budget_cause"] = "shared_budget_exhaustion"
        elif not complete_protocol:
            failure["budget_cause"] = "missing_protocol"
        if failure["category"] == "token_budget_exhaustion" and complete_protocol:
            # Codex may emit the complete final protocol line and then exit
            # non-zero while its rollout-budget wrapper accounts for the
            # just-finished turn. The validated transaction is the useful
            # terminal result; discarding it forces an identical paid retry.
            sys.stderr.write(
                "HANDSOFF_AGENT_WARNING: accepted complete structured result "
                "before trailing token-budget exhaustion\n"
            )
        elif _one_valid_verdict(spec, reviewer_results, recovered_results):
            # #405: a valid verdict is never lost to the exit status.
            return _deliver_reviewer_result(root, session_id, spec, reviewer_results[0], end_session,
                                            process.returncode)
        else:
            end_session(
                "failed", exit_code=process.returncode, failure=failure,
            )
            raise AgentLaunchError(f"{spec.adapter} exited with status {process.returncode}", session_id)
    if protocol_errors:
        if _one_valid_verdict(spec, reviewer_results, recovered_results):
            # #345: one valid verdict beside a malformed line is the
            # launcher's failure to finish cleanly, not a no-op; the
            # malformed line is kept on the transcript and never adopted.
            return _deliver_reviewer_result(root, session_id, spec, reviewer_results[0], end_session,
                                            process.returncode or 0)
        if recovered_results and not reviewer_results:
            # #167: keep the refused verdict on the session so
            # session-result-adopt can re-record it; the session still fails.
            try:
                lib.record_session_result(root, session_id, "review", recovered_results[-1]["payload"],
                                          recovered_from_rule=True, refused_text=recovered_results[-1]["text"])
            except lib.HandsoffError:
                pass
        refused = next((error for error in protocol_errors
                        if error.startswith("invalid Architect design proposal")), None)
        if spec.role == "architect" and refused:
            # #361: the proposal was written and refused by validation; the
            # reason names the refused field, its index and the limit.
            _end_protocol_refused(end_session, refused, session_id)
        end_session(
            "failed", exit_code=1,
            # A malformed structured request is a deterministic contract
            # failure, not an adapter/runtime outage.  Classifying it as a
            # generic non-zero exit made execute_with_recovery spend every
            # fallback on the same bad request and ultimately obscure the
            # still-valid workflow decision with a recovery hold.
            failure=lib.classify_runtime_failure(orchestration_noop=True, role=spec.role),
        )
        raise AgentLaunchError(protocol_errors[0], session_id)
    no_result = question_lines[0] == 0 and (
        (spec.role in {"reviewer", "architect", "supervisor"} and not reviewer_results and not architect_results
         and not architect_declines and not supervisor_requests)
        or (spec.role == "implementer" and progress_done[0] == 0))
    if background.outstanding and no_result:
        # #408: a clean exit with no result while background work it started
        # was still running: the turn ended the session. Recoverable, and no
        # review round is spent (the open attempt is reused on relaunch). An
        # Implementer's workspace is never applied as a completed session.
        failure = {"category": "background_abandoned",
                   "reason": lib._FAILURE_REASON_LABELS["background_abandoned"],
                   "tail_sha256": hashlib.sha256((stdout_tail[0] + stderr_tail[0]).encode()).hexdigest()}
        end_session("failed", exit_code=0, failure=failure)
        raise AgentLaunchError(failure["reason"], session_id)
    if capture_supervisor and not supervisor_requests and question_lines[0] == 0:
        end_session(
            "failed", exit_code=1,
            failure=lib.classify_runtime_failure(orchestration_noop=True),
        )
        raise AgentLaunchError(
            "Supervisor exited without a broker request or Pilot question", session_id,
        )
    if len(architect_results) > 1 or len(architect_declines) > 1 or (architect_results and architect_declines):
        _end_protocol_refused(end_session, "Architect emitted more than one structured outcome (proposal or decline)",
                              session_id)
    if spec.role == "architect" and not architect_results and not architect_declines and question_lines[0] == 0:
        end_session(
            "failed", exit_code=1,
            failure=lib.classify_runtime_failure(orchestration_noop=True, role=spec.role),
        )
        raise AgentLaunchError("Architect exited without a structured design proposal or Pilot question", session_id)
    if architect_requests:
        # #121: the Architect's criteria land through the host before its
        # proposal is bound to them.
        broker = __import__("handsoff_broker")
        for request in architect_requests:
            try:
                code = broker.execute_architect_criteria(root, request, capability=broker._SUPERVISOR_HOST_CAPABILITY)
            except lib.HandsoffError as exc:
                _end_protocol_refused(end_session, f"Architect criteria request rejected: {str(exc)[:160]}", session_id)
            if code != 0:
                _end_protocol_refused(end_session, "Architect criteria transaction was refused by criteria-apply",
                                      session_id)
    if architect_declines:
        # #177: the decline is recorded as a pending state the design
        # reviewer resolves; the session's actor is the architect of record.
        try:
            decline = architect_declines[0]
            cfg_now = lib.load_config(root)
            with lib.project_lock(root):
                record = (lib.load_unique_json(lib.status_path(root, cfg_now)).get("agent_sessions") or {}).get(session_id) or {}
            lib.record_design_decline(root, cfg_now, by=record.get("actor") or lib.default_agent_actor(spec.adapter, spec.role),
                                      reason=decline["reason"], evidence=decline.get("evidence"),
                                      alternative=decline.get("alternative"), except_session_id=session_id)
        except Exception as exc:
            end_session("failed", exit_code=1, failure=lib.classify_runtime_failure(exit_code=1))
            raise AgentLaunchError(f"Architect decline dispatch failed: {type(exc).__name__}: {str(exc)[:160]}", session_id) from exc
    if architect_results:
        try:
            lib.record_design_proposal(root, session_id, architect_results[0])
        except Exception as exc:
            end_session(
                "failed", exit_code=1,
                failure=lib.classify_runtime_failure(exit_code=1),
            )
            raise AgentLaunchError(
                f"Architect design proposal dispatch failed: {type(exc).__name__}", session_id,
            ) from exc
    if len(reviewer_results) > 1:
        end_session(
            "failed", exit_code=1,
            failure=lib.classify_runtime_failure(exit_code=1),
        )
        raise AgentLaunchError("Reviewer emitted more than one structured result", session_id)
    if reviewer_results:
        # #405: dispatch, then the session-result-adopt replay, before the end.
        return _deliver_reviewer_result(root, session_id, spec, reviewer_results[0], end_session, 0)
    if supervisor_requests:
        try:
            import handsoff_broker as broker
            for request in supervisor_requests:
                broker.dispatch_supervisor_request(Path(spec.project_root or spec.cwd), request)
        except KeyboardInterrupt as exc:
            end_session(
                "cancelled", exit_code=130,
                failure=lib.classify_runtime_failure(cancelled=True),
            )
            raise AgentLaunchError("agent launch cancelled", session_id) from exc
        except lib.HandsoffError as exc:
            # The host rejected a syntactically valid but semantically
            # invalid request. Re-running another model against unchanged
            # state cannot repair that contract violation safely, so this
            # stays orchestration_noop (the agent's fault), distinct from
            # dispatch_failed (#84), which names a host-side failure to
            # deliver an otherwise valid result.
            end_session(
                "failed", exit_code=1,
                failure=lib.classify_runtime_failure(orchestration_noop=True),
            )
            raise AgentLaunchError(
                f"Supervisor broker request rejected: {str(exc)[:200]}", session_id,
            ) from exc
        except Exception as exc:
            end_session(
                "failed", exit_code=1,
                failure={"category": "dispatch_failed", "reason": str(exc)[:200], "result_available": True,
                         "tail_sha256": hashlib.sha256(b"").hexdigest()},
            )
            raise AgentLaunchError(str(exc)[:200], session_id) from exc
    if spec.role in {"reviewer", "architect", "supervisor"} and not reviewer_results and not architect_results \
            and not architect_declines and not supervisor_requests and question_lines[0] == 0:
        failure = {"category": "no_artifact", "reason": "process exited 0 without a protocol result",
                   "tail_sha256": hashlib.sha256((stdout_tail[0] + stderr_tail[0]).encode()).hexdigest()}
        end_session("failed", exit_code=0, failure=failure)
        raise AgentLaunchError(failure["reason"], session_id)
    if workspace and _quarantine_late_result(root, session_id, spec.role, "implementer_workspace", None):
        end_session("completed", exit_code=0)
        return 0
    if workspace:
        # #359: only owned-path changes come back, all or nothing.
        try:
            applied = lib.apply_implementer_workspace(root, session_id)
        except (lib.HandsoffError, OSError, subprocess.SubprocessError) as exc:
            applied = {"state": "refused", "reason": "apply_failed", "paths": [],
                       "detail": str(exc)[:120]}
        if applied["state"] != "applied":
            reason = {"outside_ownership": "workspace changed paths outside its ownership: ",
                      "host_edit": "host changed owned paths since launch: ",
                      "type_transition": "workspace changed a file into a directory or back: ",
                      }.get(applied["reason"], "workspace apply failed: ")
            reason = (reason + (", ".join(applied["paths"]) or applied.get("detail", "")))[:200]
            end_session("failed", exit_code=1,
                        apply={"state": "refused", "paths": applied["paths"]},
                        failure={"category": "ownership_violation", "reason": reason,
                                 "changed_paths": applied["paths"],
                                 "tail_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest()})
            raise AgentLaunchError(f"implementer apply refused: {reason}", session_id)
        end_session("completed", exit_code=0, apply=applied)
        for problem in question_errors:
            sys.stderr.write(f"HANDSOFF_QUESTION_WARNING: {problem}\n")
        return 0
    end_session("completed", exit_code=0)
    for problem in question_errors:
        sys.stderr.write(f"HANDSOFF_QUESTION_WARNING: {problem}\n")
    return 0


def _end_protocol_refused(end_session, reason: str, session_id: str):
    """#361: end a session whose structured result the host refused, with
    the refusal as its reason, and print it: recovery replaces the
    AgentLaunchError message with its own pause reason, so stderr is the
    only place the launcher's caller reads why."""
    reason = reason.strip()[:200]
    end_session("failed", exit_code=1,
                failure={"category": "protocol_refused", "reason": reason,
                         "tail_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest()})
    sys.stderr.write(f"HANDSOFF_AGENT_REFUSED: {reason}\n")
    raise AgentLaunchError(reason, session_id)


def execute_with_recovery(spec: LaunchSpec | None, *, timeout: int = 3600,
                          from_session_id: str | None = None,
                          quality_finding_id: str | None = None,
                          actor: str | None = None,
                          popen_factory=subprocess.Popen, which=shutil.which,
                          snapshotter=lib.repository_snapshot) -> int:
    """Run and, only on authenticated recoverable outcomes, consume CAS reservations."""
    if spec is None and from_session_id is None:
        raise lib.HandsoffError("recovery requires a launch or a trusted completed session")
    # #80: a sandboxed Reviewer runs with its cwd in a scratch directory; the
    # project root travels on the spec so recovery never reads state there.
    root = Path(spec.project_root or spec.cwd).resolve() if spec else None
    if root is None:
        raise lib.HandsoffError("quality recovery requires an in-memory launch context")
    current_spec = spec
    original_input = spec.stdin
    trigger = "quality_finding" if quality_finding_id else "runtime_failure"
    source_id = from_session_id
    while True:
        if source_id is None:
            try:
                return execute_launch(current_spec, timeout=timeout, actor=actor,
                                      popen_factory=popen_factory)
            except AgentLaunchError as exc:
                source_id = exc.session_id
        prepared_spec = None

        def preflight_replacement(proposed: dict) -> None:
            nonlocal prepared_spec
            safe_handoff = json.dumps(
                proposed["handoff"], sort_keys=True, separators=(",", ":"),
            )
            completed = _completed_operations_section(root, source_id)
            fallback_input = original_input + "\n\n# Trusted replacement handoff\n\n" + safe_handoff
            if completed:
                fallback_input += "\n\n" + completed
            prepared_spec = build_profile_launch_spec(
                root, proposed["role"], fallback_input,
                proposed["selected_profile"], which=which,
                owned_paths=current_spec.owned_paths,  # #359
                compact_scope=current_spec.compact_scope,  # #388
            )

        replacement = lib.reserve_agent_replacement(
            root, from_session_id=source_id, trigger=trigger,
            finding_id=quality_finding_id, which=which, snapshotter=snapshotter,
            pre_reservation_check=preflight_replacement,
        )
        if replacement["action"] != "launch":
            raise lib.HandsoffError(f"agent replacement paused: {replacement['reason']}"
                                    + _kept_result_hint(root, source_id))
        if prepared_spec is None:
            raise lib.HandsoffError("replacement reservation completed without an exact launch preflight")
        current_spec = prepared_spec
        try:
            return execute_launch(
                current_spec, timeout=timeout, popen_factory=popen_factory,
                precreated_session_id=replacement["to_session_id"],
            )
        except AgentLaunchError as exc:
            source_id = exc.session_id
            trigger = "runtime_failure"
            quality_finding_id = None


def _reviewer_tree_failure(spec: LaunchSpec, root: Path, session_id: str,
                           repository_digest_before: dict | None) -> dict | None:
    """The reviewer's tree check: the failure record when an in-tree
    reviewer changed the project, else None. A sandboxed reviewer cannot
    write outside its scratch cwd, so a change there is attributed to the
    host (#92) and the review is kept."""
    before = repository_digest_before.get("entries", {}) if isinstance(repository_digest_before, dict) else {}
    after = lib.repository_digest_entries(root, lib.load_config(root))
    changed_paths = [path for path in sorted(set(before) | set(after))
                     if before.get(path) != after.get(path)][:64]
    digest_changed = isinstance(repository_digest_before, dict) \
        and repository_digest_before.get("digest") != lib.repository_digest_from_entries(after)
    if not (changed_paths or digest_changed):
        return None
    if Path(spec.cwd).resolve() != Path(root).resolve():
        lib.append_event(root, lib.load_config(root), "host_edited_during_review",
                         "Host edited project during scratch review", session_id=session_id,
                         paths=changed_paths or ["<repository-digest-changed>"])
        return None
    # The reviewer ran inside the tree, so a change is its own doing.
    # #203: say what was seen, so a sighting on a loaded runner can be
    # read: for each path, whether it appeared, vanished or changed, and
    # when the file on disk was last written relative to the session's start.
    return {"category": "reviewer_modified_project",
            "reason": "managed Reviewer modified the project tree",
            "tail_sha256": hashlib.sha256(b"").hexdigest(), "changed_paths": changed_paths or ["<repository-digest-changed>"],
            "changes": _describe_tree_changes(root, before, after, changed_paths, _session_started_at(root, session_id))}


AUTOADOPT_ACTOR = "handsoff-launcher"


def session_result_adopt_command(session_id: str) -> str:
    """#405: the exact command a Pilot runs to adopt a kept result."""
    return f"handsoff_supervisor.py session-result-adopt --session {session_id} --by ACTOR"


def _kept_result_hint(root: Path, session_id: str | None) -> str:
    """#405: a pause on a dispatch_failed session whose result is kept
    names the adoption command; any other pause says nothing more."""
    try:
        status = lib.load_unique_json(lib.status_path(root, lib.load_config(root)))
    except (lib.HandsoffError, OSError, ValueError):
        return ""
    failure = (status.get("agent_failures") or {}).get(session_id) if session_id else None
    if not isinstance(failure, dict) or failure.get("category") != "dispatch_failed" \
            or not failure.get("result_available") or failure.get("adopted"):
        return ""
    return f"; the result is kept: run {session_result_adopt_command(session_id)}"


def _one_valid_verdict(spec: LaunchSpec, reviewer_results: list[dict], recovered_results: list[dict]) -> bool:
    """#405: exactly one schema-valid reviewer verdict (implementation or
    design review) and no #167 refused packet beside it."""
    return spec.role == "reviewer" and len(reviewer_results) == 1 and not recovered_results


def _deliver_reviewer_result(root: Path, session_id: str, spec: LaunchSpec, result: dict,
                             end_session, process_exit: int) -> int:
    """#405 (#345): a reviewer's one schema-valid verdict is delivered
    whatever the process exit (0, non-zero, or a timeout as 124). The
    direct dispatch runs first, while the session is still live; when it
    raises, the same adoption session-result-adopt performs runs before
    the session ends, so every gate that refuses a recorded review refuses
    it here too. Either success ends the session completed. Only when both
    fail does it end failed as dispatch_failed with the result kept
    (result_available) and both reasons named; dispatch_failed is never
    replaced automatically, and the pause names the adoption command."""
    if _quarantine_late_result(root, session_id, spec.role, "review", result):
        end_session("completed", exit_code=process_exit)
        return 0  # #383: held for adoption after performance-resume
    try:
        import handsoff_broker as broker
        broker.dispatch_reviewer_result(root, session_id, result)
    except Exception as exc:
        dispatch_error = str(exc).strip() or type(exc).__name__
    else:
        end_session("completed", exit_code=process_exit)
        return 0
    supervisor = __import__("handsoff_supervisor")
    try:
        adopted, message = supervisor.adopt_session_result(root, session_id, AUTOADOPT_ACTOR, automatic=True)
    except lib.HandsoffError as exc:
        adopted, message = False, f"SESSION_RESULT_ADOPT_REFUSED: {exc}"
    if adopted:
        end_session("completed", exit_code=process_exit)
        sys.stdout.write(
            f"HANDSOFF_AGENT_WARNING: reviewer session {session_id} exited {process_exit} and its dispatch "
            f"failed ({dispatch_error[:120]}); the verdict was adopted automatically\n"
        )
        sys.stdout.flush()
        return 0
    adoption_error = message.split(":", 1)[-1].strip() or "refused"
    sys.stdout.write(f"SESSION_RESULT_AUTOADOPT_REFUSED: {adoption_error[:200]}\n")
    sys.stdout.flush()
    reason = f"dispatch: {dispatch_error[:90]}; adoption: {adoption_error[:90]}"[:200]
    try:
        lib.append_event(root, lib.load_config(root), "session_result_autoadopt_refused",
                         "Reviewer verdict was neither dispatched nor adopted", session_id=session_id,
                         reason=adoption_error[:200], dispatch_error=dispatch_error[:200],
                         process_exit=process_exit)
    except (lib.HandsoffError, OSError):
        pass
    end_session("failed", exit_code=process_exit,
                failure={"category": "dispatch_failed", "reason": reason, "result_available": True,
                         "tail_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest()})
    sys.stderr.write(f"HANDSOFF_AGENT_REFUSED: {reason}; the verdict is kept: run "
                     f"{session_result_adopt_command(session_id)}\n")
    raise AgentLaunchError(f"Reviewer result dispatch failed: {reason}", session_id)


_QUESTION_SEEN_LOCK = threading.Lock()


def _raise_question_line(root: Path, role: str, session_id: str, line: str, errors: list[str],
                         seen: set[str] | None = None) -> bool:
    """#46: `HANDSOFF_QUESTION: <text>` from any role is recorded the moment
    it is read, so the board alerts while the child is still running. A
    failure to record is remembered for the report and never reaches the
    child, the reader, or the session record. #345: a question inside a
    complete stream-json assistant event is unwrapped the same way, and
    `seen` keeps a question read twice in one launch to one record."""
    stripped = line.strip()
    if not stripped.startswith(lib.QUESTION_PREFIX):
        if not stripped.startswith("{"):
            return False
        try:
            event = json.loads(stripped)
        except ValueError:
            return False
        if not isinstance(event, dict) or event.get("type") != "assistant":
            return False
        raised = False
        for logical in _claude_logical_lines(stripped):
            if logical.strip().startswith(lib.QUESTION_PREFIX):
                raised = _raise_question_line(root, role, session_id, logical, errors, seen) or raised
        return raised
    if seen is not None:
        # Both stream readers share `seen`; check and add as one step.
        with _QUESTION_SEEN_LOCK:
            if stripped in seen:
                return False
            seen.add(stripped)
    try:
        lib.raise_question(root, role=role, session_id=session_id,
                           text=stripped[len(lib.QUESTION_PREFIX):])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"question not recorded: {type(exc).__name__}: {exc}")
    return True


def _parse_supervisor_line(line: str, requests: list[dict], errors: list[str]) -> None:
    if not line.startswith(SUPERVISOR_REQUEST_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_supervisor_line(logical, requests, errors)
        return
    payload = line[len(SUPERVISOR_REQUEST_PREFIX):].strip()
    if len(requests) >= MAX_SUPERVISOR_REQUESTS:
        errors.append(f"Supervisor emitted more than {MAX_SUPERVISOR_REQUESTS} broker requests")
        return
    try:
        requests.append(__import__("handsoff_broker").parse_request(payload))
    except lib.HandsoffError as exc:
        errors.append(str(exc))


def _parse_reviewer_line(line: str, results: list[dict], errors: list[str], recovered: list[dict] | None = None,
                         root: Path | None = None) -> None:
    if not line.startswith(REVIEW_RESULT_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_reviewer_line(logical, results, errors, recovered, root)
        return
    payload = line[len(REVIEW_RESULT_PREFIX):].strip()
    try:
        results.append(__import__("handsoff_broker").parse_reviewer_result(payload, root=root))
    except lib.PacketRuleViolation as exc:
        # #167: the packet is refused, the launch fails, and the verdict
        # stays recoverable: the raw text travels with the repaired copy so
        # the operator may adopt it deliberately from what was written.
        errors.append(str(exc))
        if recovered is not None and isinstance(exc.recovered, dict):
            recovered.append({"payload": exc.recovered, "text": payload})
    except lib.HandsoffError as exc:
        errors.append(str(exc))


def _claude_logical_lines(line: str) -> list[str]:
    """Extract assistant text from one Claude stream event.

    Claude's final event can be a tool result or tool call, so protocol
    parsing must consume assistant text as it arrives instead of waiting for
    a final-message event. Non-JSON output remains compatible with adapters
    and older fake processes that write protocol text directly.
    """
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return [line]
    if not isinstance(event, dict):
        return []
    texts = []
    def collect(value):
        if isinstance(value, dict):
            if value.get("type") in {"text", "text_delta"} and isinstance(value.get("text"), str):
                texts.append(value["text"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(event)
    return "\n".join(texts).splitlines() if texts else []


PROGRESS_PREFIX = "HANDSOFF_PROGRESS:"


def _parse_progress_line(line: str, root: Path, session_id: str, role: str) -> dict | None:
    """#215: persist a valid per-criterion progress claim from the
    Implementer on its session; a malformed line is a protocol warning and
    never a failure; another role's line is ignored. Returns the valid claim
    (#408: a `done` claim is the Implementer's result)."""
    if role != "implementer" or not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        record = lib.validate_progress_line(json.loads(line[len(PROGRESS_PREFIX):].strip()))
    except (ValueError, TypeError):
        record = None
    if record is None:
        lib.count_operation_warning(root, session_id, role, "protocol_warnings")
        return None
    try:
        lib.record_session_progress(root, session_id, record)
    except Exception:
        pass  # telemetry never turns a child outcome into a failure
    return record


def _parse_operation_line(line: str, root: Path, session_id: str, role: str) -> None:
    """Persist valid operation telemetry while isolating malformed or late child output."""
    if not line.startswith("HANDSOFF_OPERATION:"):
        return
    try:
        payload = json.loads(line[len("HANDSOFF_OPERATION:"):].strip())
        record = lib.validate_operation_line(payload)
    except (ValueError, TypeError):
        record = None
    if record is None:
        lib.count_operation_warning(root, session_id, role, "protocol_warnings")
        return
    try:
        status = lib.load_unique_json(lib.status_path(root, lib.load_config(root)))
        current = lib.current_agent_sessions(status).get(role)
        concurrent = role == "implementer" and any(  # #359: an earlier live implementer is not late
            item.get("session_id") == session_id for item in lib.live_implementer_sessions(status))
        if not concurrent and (not isinstance(current, dict) or current.get("session_id") != session_id):
            lib.count_operation_warning(root, session_id, role, "late_telemetry")
            return
        lib.record_operation(root, session_id, role, record, datetime.now(timezone.utc))
    except Exception:
        # Telemetry must never turn a managed child outcome into a failure.
        return


def _parse_architect_request_line(line: str, requests: list[dict], errors: list[str]) -> None:
    """#121: HANDSOFF_BROKER_REQUEST from the Architect is a criteria transaction."""
    if not line.startswith(SUPERVISOR_REQUEST_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_architect_request_line(logical, requests, errors)
        return
    payload = line[len(SUPERVISOR_REQUEST_PREFIX):].strip()
    if len(requests) >= 8:
        errors.append("Architect emitted more than 8 criteria requests")
        return
    try:
        requests.append(__import__("handsoff_broker").parse_architect_request(payload))
    except lib.HandsoffError as exc:
        errors.append(str(exc))


def _parse_architect_decline_line(line: str, results: list[dict], errors: list[str]) -> None:
    """#177: HANDSOFF_DESIGN_DECLINE: {"reason": ..., "evidence": [...], "alternative": ...|null}."""
    if not line.startswith(DECLINE_RESULT_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_architect_decline_line(logical, results, errors)
        return
    payload = line[len(DECLINE_RESULT_PREFIX):].strip()
    try:
        value = json.loads(payload)
    except ValueError as exc:
        errors.append(f"invalid Architect decline: {exc}")
        return
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str) or not 1 <= len(value["reason"].strip()) <= 512:
        errors.append("invalid Architect decline: reason must be 1 to 512 characters")
        return
    evidence = value.get("evidence") or []
    if not isinstance(evidence, list) or len(evidence) > lib.MAX_DECLINE_EVIDENCE \
            or any(not isinstance(e, str) or not 1 <= len(e.strip()) <= 512 for e in evidence):
        errors.append(f"invalid Architect decline: evidence is at most {lib.MAX_DECLINE_EVIDENCE} items of 1 to 512 characters")
        return
    alternative = value.get("alternative")
    if alternative is not None and (not isinstance(alternative, str) or not 1 <= len(alternative.strip()) <= 512):
        errors.append("invalid Architect decline: alternative must be 1 to 512 characters or null")
        return
    results.append({"reason": value["reason"].strip(), "evidence": [e.strip() for e in evidence],
                    "alternative": alternative.strip() if isinstance(alternative, str) else None})


def _parse_architect_line(line: str, results: list[dict], errors: list[str]) -> None:
    if not line.startswith(DESIGN_RESULT_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_architect_line(logical, results, errors)
        return
    payload = line[len(DESIGN_RESULT_PREFIX):].strip()
    try:
        value = json.loads(payload)
        results.append(lib.validate_design_proposal(value))
    except (ValueError, lib.HandsoffError) as exc:
        errors.append(f"invalid Architect design proposal: {exc}")


def _performance_refusal(root: Path, operation: str) -> str | None:
    """Ask the supervisor's own gate, imported late to avoid a cycle.

    A missing or unreadable performance record is not a refusal: the gate
    exists to stop work during a recorded pause, never to block a run that
    has no performance history yet.
    """
    try:
        import handsoff_supervisor
    except ImportError:
        return None
    try:
        return handsoff_supervisor.performance_mutation_refusal(root, operation)
    except (lib.HandsoffError, OSError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a configured Handsoff role")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "launch"):
        command = sub.add_parser(name)
        command.add_argument("role", choices=lib.SELECTABLE_AGENT_ROLES)
        command.add_argument("--task", required=True)
        command.add_argument("--topic", default=None,
                             help="one briefing topic to add to this launch")
        command.add_argument("--owns", action="append", default=None, metavar="PATH",
                             help="implementer only, repeatable: a project path this launch owns; "
                                  "with it a second implementer may run beside a live one whose "
                                  "owned paths do not overlap, in its own worktree")
        command.add_argument("--compact-scope", action="append", default=None, metavar="PATH:START-END",
                             help="reviewer only, repeatable: review only these line ranges; the "
                                  "reviewer sees nothing else and cannot run tests")
        if name == "launch":
            command.add_argument("--timeout", type=int, default=3600)
            command.add_argument(
                "--by", default=None,
                help="runtime actor identity (default: '<resolved-adapter>-<role>')",
            )
            command.add_argument("--skip-preflight", action="store_true")
            command.add_argument(
                "--amendment", default=None,
                help="review the OPEN amendment with this id (reviewer only): the verdict is "
                     "recorded as amendment-review and no phase review attempt is spent",
            )
    args = parser.parse_args()
    try:
        if args.command == "launch" and os.environ.get(MANAGED_ROLE_ENV):
            raise lib.HandsoffError(
                "managed roles cannot launch nested agents; return a structured request to the host Supervisor"
            )
        if args.command == "launch":
            # #295: this entry point had no performance gate at all, so a
            # paused run could still launch a model. The supervisor CLI and
            # the dashboard both refused; `handsoff agent launch` did not,
            # which is the path a host actually uses.
            refusal = _performance_refusal(lib.resolve_root(args.root), f"launch_{args.role}")
            if refusal:
                raise lib.HandsoffError(refusal)
        spec = build_launch_spec(lib.resolve_root(args.root), args.role, args.task, skip_preflight=getattr(args, "skip_preflight", False),
                                 inspection=args.command == "inspect", amendment=bool(getattr(args, "amendment", None)),
                                 topic=args.topic, owned_paths=args.owns,
                                 compact_scope=parse_compact_scope(args.compact_scope))
        if getattr(args, "amendment", None):
            spec = dataclasses.replace(spec, amendment_id=args.amendment)
        if args.command == "inspect":
            print(json.dumps({
                "role": spec.role,
                "adapter": spec.adapter,
                "model": spec.model,
                "tier": spec.tier,
                "tier_reason": spec.tier_reason,
                "argv": list(spec.argv),
                "cwd": spec.cwd,
                "stdin_bytes": len(spec.stdin.encode("utf-8")),
                "stdin_sha256": hashlib.sha256(spec.stdin.encode("utf-8")).hexdigest(),
                "token_budget": spec.token_budget,
                "owned_paths": list(spec.owned_paths) if spec.owned_paths else None,
                "compact_scope": list(spec.compact_scope) if spec.compact_scope else None,
                "fresh_session": True,
            }, indent=2))
            return 0
        if args.timeout <= 0:
            raise lib.HandsoffError("--timeout must be positive")
        return execute_with_recovery(spec, timeout=args.timeout, actor=args.by)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
