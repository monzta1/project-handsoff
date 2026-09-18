#!/usr/bin/env python3
"""Build and launch least-privilege Codex or Claude Code role sessions."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402


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
    # Host-enforced native rollout ceiling when the selected adapter
    # supports it.  It is configuration, never inferred from output.
    token_budget: int | None = None
    env_overrides: dict[str, str] | None = None
    project_root: str | None = None


SUPERVISOR_REQUEST_PREFIX = "HANDSOFF_BROKER_REQUEST:"
REVIEW_RESULT_PREFIX = "HANDSOFF_REVIEW_RESULT:"
DESIGN_RESULT_PREFIX = "HANDSOFF_DESIGN_PROPOSAL:"
MANAGED_ROLE_ENV = "HANDSOFF_MANAGED_SESSION_ROLE"
MANAGED_SESSION_ENV = "HANDSOFF_MANAGED_SESSION_ID"
MAX_AGENT_TASK_BYTES = 16 * 1024
MAX_REPLACEMENT_INPUT_BYTES = 64 * 1024
MAX_SUPERVISOR_REQUESTS = 8
FOLLOWUP_DESIGN_TOKEN_BUDGET = 16_000

# Managed coding roles need the repository shell, not the user's entire
# interactive Codex plugin/app/tool catalogue.  Disabling those optional
# surfaces materially reduces the fixed prompt paid again on every tool
# turn while leaving the OS sandbox and core shell/edit tools intact.
CODEX_DISABLED_FEATURES = lib.CODEX_DISABLED_FEATURES


def _effective_token_budget(configured: int, role: str, context: dict | None) -> int:
    """Follow-up design turns receive the packet, not another discovery budget."""
    if role in {"architect", "reviewer"} and isinstance(context, dict) \
            and int(context.get("review_attempts", 0) or 0) > 0:
        packet_bytes = int(context.get("packet_stdin_bytes", 0) or 0) or len(str(context.get("packet_stdin", "")).encode())
        if not packet_bytes:
            # #114: a host Architect's proposal carries no managed packet, but
            # build_launch_spec has already derived a budget from the full
            # role input; the 16k constant is only for callers that give
            # neither.
            return min(configured, int(context.get("followup_design_token_budget") or FOLLOWUP_DESIGN_TOKEN_BUDGET))
        default = max(40_000, packet_bytes // 3 + 30_000)
        return min(configured, context.get("followup_design_token_budget", default))
    return configured


def _codex_argv(executable: str, role: str, model: str, token_budget: int,
                *, reviewer_sandbox: bool = False) -> list[str]:
    return lib.codex_argv(executable, role, model, token_budget, reviewer_sandbox=reviewer_sandbox)


def _reviewer_scratch(root: Path, adapter: str, role: str, *, create: bool = True) -> Path | None:
    """Create the per-session boundary required by a Codex Reviewer."""
    if role != "reviewer" or adapter != "codex":
        return None
    root = root.resolve()
    if not create:
        return root / ".handsoff-reviewer-inspect"
    scratch = Path(tempfile.mkdtemp(prefix="handsoff-reviewer-")).resolve()
    assert not (scratch == root or root in scratch.parents)
    return scratch


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


def build_role_input(root: Path, role: str, task: str) -> str:
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
    text = f"{_role_prompt(root, role)}\n\n# Assigned task\n\n{task}"
    if role == "reviewer":
        text = (f"# Project root (read-only)\n\n{root}: use git as `git -C {root} ...` and tests as `cd {root} && python3 -m unittest ...`. "
                "Write only inside the current directory.\n\n" + text)
    if role == "implementer":
        text = f"{lib.implementer_permissions_section(root)}\n\n{text}"
    if role in {"architect", "reviewer"}:
        # #121: the sandbox cannot take the project lock; the host records
        # every protocol line, so supervisor commands are never the answer.
        text = ("# Sandbox\n\nYour sandbox is read-only for the project. Do not run `handsoff supervisor` "
                "commands; they will fail on the project lock. The host records your protocol lines "
                "(HANDSOFF_BROKER_REQUEST criteria transactions and HANDSOFF_DESIGN_PROPOSAL for the Architect, "
                "HANDSOFF_REVIEW_RESULT for the Reviewer).\n\n" + text)
    if role in DESIGN_EVIDENCE_ROLES:
        cfg = lib.load_config(root)
        context = lib.managed_design_context(root, role)
        if context is not None:
            context_json = json.dumps(context, sort_keys=True, separators=(",", ":"))
            text = f"# Managed design context\n\n{context_json}\n\n{text}"
        packet = applicable_design_review_packet(root, cfg, role)
        if packet is not None:
            packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
            text = f"{DESIGN_REVIEW_PACKET_HEADING}\n\n{packet_json}\n\n{text}"
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
    return text


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


def build_launch_spec(root: Path, role: str, task: str, *, which=shutil.which, skip_preflight: bool = False, inspection: bool = False) -> LaunchSpec:
    root = root.resolve()
    lib.validate_runtime_integrity(root)
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise lib.HandsoffError("role must be architect, implementer, or reviewer")
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    cfg = lib.load_config(root)
    if role == "reviewer":
        status_file = lib.status_path(root, cfg)
        if status_file.is_file():
            status = lib.load_unique_json(status_file)
            if status.get("phase_number") == 2 and not status.get("design_proposal"):
                raise lib.HandsoffError("reviewer launch refused: no design proposal is recorded; run design-propose or an Architect session first")
            if status.get("phase_number") == 5:
                if not status.get("original_symptom_evidence_id"):
                    raise lib.HandsoffError("reviewer launch refused: run handsoff_supervisor.py record-symptom-resolved --evidence <run_id> --by ACTOR first")
                acceptance = lib.load_unique_json(lib.acceptance_path(root, lib.load_config(root)))
                records, _problems = lib.load_verifications(root, cfg)
                gaps = lib.reviewer_launch_evidence_gaps(acceptance.get("criteria", []), records)
                if gaps:
                    raise lib.HandsoffError("reviewer launch refused, evidence still missing: " + "; ".join(gaps))
    if lib.agent_profiles(cfg)[role]["adapter"] == lib.HOST_AGENT_ADAPTER:
        raise lib.HandsoffError(f"{role} is host-driven; run the Supervisor CLI directly instead of launching a managed session")
    context = lib.managed_design_context(root, role) or {}
    context["followup_design_token_budget"] = cfg.get("followup_design_token_budget") or max(40_000, len(build_role_input(root, role, task).encode()) // 3 + 30_000)
    token_budget = _effective_token_budget(cfg["agent_token_budgets"][role], role, context)
    _refuse_reviewer_launch_over_budget(root, cfg, role)
    selection = _phase2_design_reviewer_selection(root, cfg, role, which=which)
    if selection is not None:
        adapter = selection["adapter"]
        model = lib.validate_agent_model(selection["model"])
        resolution_source = selection["resolution_source"]
        tier, tier_reason = selection["tier"], selection["reason"]
    else:
        tier = tier_reason = None
        configured_adapter = lib.agent_profiles(cfg)[role]["adapter"]
        profile = lib.resolved_agent_profiles(cfg, which=which, require_available=True)[role]
        adapter = profile["adapter"]
        if adapter not in lib.SELECTABLE_AGENT_ADAPTERS:
            raise lib.HandsoffError(f"role {role} uses unsupported adapter: {adapter}")
        model = lib.validate_agent_model(profile["model"])
        # #39: "recommended" only when the adapter itself came from
        # RECOMMENDED_CREW; an explicit "auto" keeps the older auto-detect
        # label, and any other explicit value is "configured" (load_config
        # already folds the legacy "configure-me" placeholder into the
        # recommended crew, so it never reaches here). Nothing here ever
        # records the recommended profile as launched when it was not: the
        # executable check below refuses first.
        adapter_source = profile["source"]["adapter"]
        if configured_adapter == lib.AUTO_AGENT_ADAPTER:
            resolution_source = "auto_detected"
        elif adapter_source == lib.RECOMMENDED_PROFILE_SOURCE:
            resolution_source = "recommended"
        else:
            resolution_source = "configured"
    stdin = build_role_input(root, role, task)
    packet = applicable_design_review_packet(root, cfg, role)
    executable = cfg.get("adapters", {}).get(adapter) or which(adapter)
    if not executable:
        # An auto-detected adapter is by construction installed, so only a
        # recommended default or an explicit selection can land here.
        raise lib.HandsoffError(lib.unavailable_adapter_message(role, adapter, model, resolution_source))
    executable = str(Path(executable).resolve())
    if not skip_preflight:
        try:
            preflight = json.loads((root / lib.PREFLIGHT_FILE).read_text(encoding="utf-8"))
            item = preflight.get(adapter, {})
            checked = datetime.fromisoformat(item.get("checked_at", "")).astimezone(timezone.utc)
            if item.get("state") == "unreachable" and datetime.now(timezone.utc) - checked < timedelta(hours=24):
                raise lib.HandsoffError(f"{adapter} pre-flight unreachable: {item.get('reason')}")
        except FileNotFoundError:
            pass

    scratch = _reviewer_scratch(root, adapter, role, create=not inspection)
    if adapter == "codex":
        argv = _codex_argv(executable, role, model, token_budget, reviewer_sandbox=scratch is not None)
    else:
        allowed = [] if role in {"reviewer", "supervisor", "architect"} else lib.implementer_allowed_tools(cfg, root, which)
        argv = lib.claude_argv(executable, role, allowed, model)

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
        env_overrides={"TMPDIR": str(scratch)} if scratch else None,
        project_root=str(root),
    )


def build_profile_launch_spec(root: Path, role: str, task: str, profile: dict,
                              *, which=shutil.which) -> LaunchSpec:
    """Build a launch only from the exact fallback profile reserved by the host."""
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
    if adapter not in lib.SELECTABLE_AGENT_ADAPTERS:
        raise lib.HandsoffError("reserved fallback adapter is invalid")
    executable = which(adapter)
    if not executable:
        raise lib.HandsoffError(f"reserved {adapter} executable is no longer available")
    executable = str(Path(executable).resolve())
    configured_budget = lib.load_config(root)["agent_token_budgets"][role]
    token_budget = _effective_token_budget(
        configured_budget, role, lib.managed_design_context(root, role),
    )
    scratch = _reviewer_scratch(root, adapter, role)
    if adapter == "codex":
        argv = _codex_argv(executable, role, model, token_budget, reviewer_sandbox=scratch is not None)
    else:
        allowed = [] if role in {"reviewer", "supervisor", "architect"} else lib.implementer_allowed_tools(lib.load_config(root), root)
        argv = lib.claude_argv(executable, role, allowed, model)
    return LaunchSpec(
        role, adapter, model, tuple(argv), str(scratch or root.resolve()), task, "fallback",
        token_budget=token_budget,
        env_overrides={"TMPDIR": str(scratch)} if scratch else None,
        project_root=str(root.resolve()),
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

    def __init__(self, root: Path, session_id: str, role: str, process, interval: float):
        self.root = root
        self.session_id = session_id
        self.role = role
        self.process = process
        self.pid = _process_pid(process)
        self.interval = max(float(interval), 0.01)
        self.stop = threading.Event()
        self.thread = None
        self.operation_terminated = False

    def _beat(self) -> None:
        while not self.stop.is_set():
            lib.write_live_beacon(self.root, session_id=self.session_id, role=self.role,
                                  state="running", pid=self.pid)
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
        lib.write_live_beacon(
            self.root, session_id=self.session_id, role=self.role, state=record["state"],
            pid=self.pid, ended_at=record.get("ended_at"), exit_code=record.get("exit_code"),
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
    capture_supervisor = spec.role == "supervisor"
    root = Path(spec.project_root or spec.cwd).resolve()
    actor = lib.validate_agent_actor(actor or lib.default_agent_actor(spec.adapter, spec.role))
    if precreated_session_id is None:
        session = lib.create_agent_session(
            root, role=spec.role, actor=actor, adapter=spec.adapter,
            requested_model=spec.model, resolution_source=spec.resolution_source,
            id_factory=session_id_factory, packet_id=spec.packet_id, design_hash=spec.design_hash,
            tier=spec.tier, tier_reason=spec.tier_reason,
        )
    else:
        session = lib.claim_precreated_agent_session(
            root, precreated_session_id, role=spec.role, adapter=spec.adapter,
            requested_model=spec.model,
        )
        actor = session["actor"]
    session_id = session["session_id"]
    child_env = os.environ.copy()
    child_env[MANAGED_ROLE_ENV] = spec.role
    child_env[MANAGED_SESSION_ENV] = session_id
    if spec.env_overrides:
        child_env.update(spec.env_overrides)
    portable_output = _PortableOutput(
        root, session_id, spec.role, spec.adapter, spec.stdin, child_env,
    )
    repository_digest_before = ({"entries": lib.repository_digest_entries(root, lib.load_config(root)),
                                 "digest": lib.repository_digest(root, lib.load_config(root))}
                                if spec.role == "reviewer" else None)
    try:
        process = popen_factory(
            list(spec.argv),
            cwd=spec.cwd,
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
    beacon = _LiveBeacon(root, session_id, spec.role, process, beacon_interval)
    beacon.start()
    try:
        result = _run_managed_process(
            spec, root, session_id, process, timeout, capture_supervisor, portable_output,
            repository_digest_before=repository_digest_before,
            beacon=beacon,
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
                         beacon: _LiveBeacon | None = None) -> int:
    """The lifecycle of an already-started child: exactly one terminal
    transition on every path, no payload data recorded."""
    supervisor_requests: list[dict] = []
    reviewer_results: list[dict] = []
    architect_results: list[dict] = []
    architect_requests: list[dict] = []
    protocol_errors: list[str] = []
    question_errors: list[str] = []
    question_lines = [0]
    stdout_tail = [""]
    stderr_tail = [""]
    def persist_new(items, kind):
        if items:
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
                        if _raise_question_line(root, spec.role, session_id, line, question_errors):
                            question_lines[0] += 1
                        _parse_operation_line(line, root, session_id, spec.role)
                        if capture_supervisor:
                            _parse_supervisor_line(line, supervisor_requests, protocol_errors)
                            persist_new(supervisor_requests, "supervisor_request")
                        if spec.role == "reviewer":
                            _parse_reviewer_line(line, reviewer_results, protocol_errors)
                            persist_new(reviewer_results, "review")
                        if spec.role == "architect":
                            _parse_architect_request_line(line, architect_requests, protocol_errors)
                            _parse_architect_line(line, architect_results, protocol_errors)
                            persist_new(architect_results, "design")
                    if len(pending.encode("utf-8")) > 65536:
                        if pending.startswith(SUPERVISOR_REQUEST_PREFIX):
                            protocol_errors.append("Supervisor broker request exceeded 65536 bytes")
                        pending = ""
                        discarding = True
                if pending and not discarding:
                    if _raise_question_line(root, spec.role, session_id, pending, question_errors):
                        question_lines[0] += 1
                    _parse_operation_line(pending, root, session_id, spec.role)
                    if capture_supervisor:
                        _parse_supervisor_line(pending, supervisor_requests, protocol_errors)
                        persist_new(supervisor_requests, "supervisor_request")
                    if spec.role == "reviewer":
                        _parse_reviewer_line(pending, reviewer_results, protocol_errors)
                        persist_new(reviewer_results, "review")
                    if spec.role == "architect":
                        _parse_architect_request_line(pending, architect_requests, protocol_errors)
                        _parse_architect_line(pending, architect_results, protocol_errors)
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
            process.stdin.write(spec.stdin)
            process.stdin.close()
            process.wait(timeout=timeout)
        else:
            process.communicate(input=spec.stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            _stop_process_group(process)
        finally:
            try:
                if reader:
                    reader.join(timeout=5)
                if stderr_reader:
                    stderr_reader.join(timeout=5)
            finally:
                lib.transition_agent_session(
                    root, session_id, "timed_out", exit_code=124,
                    failure=lib.classify_runtime_failure(timed_out=True),
                )
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
                lib.transition_agent_session(
                    root, session_id, "cancelled", exit_code=130,
                    failure=lib.classify_runtime_failure(cancelled=True),
                )
        raise AgentLaunchError("agent launch cancelled", session_id) from exc
    except Exception as exc:
        _terminalize_runner_io_failure(
            root, session_id, process, stop_first=not isinstance(exc, BrokenPipeError),
        )
        raise AgentLaunchError(f"agent runner I/O failed: {type(exc).__name__}", session_id) from exc
    if reader:
        try:
            reader.join(timeout=5)
            reader_alive = reader.is_alive()
        except KeyboardInterrupt as exc:
            try:
                _stop_process_group(process)
            finally:
                lib.transition_agent_session(
                    root, session_id, "cancelled", exit_code=130,
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
            stderr_reader.join(timeout=5)
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
    if process.returncode:
        # #114: on a budget error Codex can route the final message to
        # stderr. A complete protocol line there is still the result, so
        # scan the tail once before classifying the failure.
        if spec.role in {"reviewer", "architect"} and not reviewer_results and not architect_results:
            for line in stderr_tail[0].splitlines():
                if spec.role == "reviewer" and line.startswith(REVIEW_RESULT_PREFIX):
                    _parse_reviewer_line(line, reviewer_results, protocol_errors)
                    persist_new(reviewer_results, "review")
                elif spec.role == "architect" and line.startswith(DESIGN_RESULT_PREFIX):
                    _parse_architect_line(line, architect_results, protocol_errors)
                    persist_new(architect_results, "design")
        if beacon is not None and beacon.operation_terminated:
            operation = lib.current_operation(root, session_id) or {}
            failure = {"category": "external_timeout",
                       "reason": "external operation exceeded its declared timeout",
                       "dependency": operation.get("dependency"),
                       "operation": operation.get("operation"),
                       "tail_sha256": hashlib.sha256(b"").hexdigest()}
            lib.transition_agent_session(root, session_id, "failed", exit_code=process.returncode,
                                        failure=failure)
            raise AgentLaunchError(failure["reason"], session_id)
        failure = lib.classify_runtime_failure(
            exit_code=process.returncode, stderr_tail=stderr_tail[0], stdout_tail=stdout_tail[0],
        )
        complete_protocol = not protocol_errors and (
            (spec.role == "architect" and len(architect_results) == 1)
            or (spec.role == "reviewer" and len(reviewer_results) == 1)
            or (capture_supervisor and bool(supervisor_requests))
            or question_lines[0] > 0
        )
        if failure["category"] == "token_budget_exhaustion" and complete_protocol:
            # Codex may emit the complete final protocol line and then exit
            # non-zero while its rollout-budget wrapper accounts for the
            # just-finished turn. The validated transaction is the useful
            # terminal result; discarding it forces an identical paid retry.
            sys.stderr.write(
                "HANDSOFF_AGENT_WARNING: accepted complete structured result "
                "before trailing token-budget exhaustion\n"
            )
        else:
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=process.returncode, failure=failure,
            )
            raise AgentLaunchError(f"{spec.adapter} exited with status {process.returncode}", session_id)
    if protocol_errors:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            # A malformed structured request is a deterministic contract
            # failure, not an adapter/runtime outage.  Classifying it as a
            # generic non-zero exit made execute_with_recovery spend every
            # fallback on the same bad request and ultimately obscure the
            # still-valid workflow decision with a recovery hold.
            failure=lib.classify_runtime_failure(orchestration_noop=True),
        )
        raise AgentLaunchError(protocol_errors[0], session_id)
    if capture_supervisor and not supervisor_requests and question_lines[0] == 0:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            failure=lib.classify_runtime_failure(orchestration_noop=True),
        )
        raise AgentLaunchError(
            "Supervisor exited without a broker request or Pilot question", session_id,
        )
    if len(architect_results) > 1:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            failure=lib.classify_runtime_failure(orchestration_noop=True),
        )
        raise AgentLaunchError("Architect emitted more than one structured design proposal", session_id)
    if spec.role == "architect" and not architect_results and question_lines[0] == 0:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            failure=lib.classify_runtime_failure(orchestration_noop=True),
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
                lib.transition_agent_session(root, session_id, "failed", exit_code=1,
                                            failure=lib.classify_runtime_failure(orchestration_noop=True))
                raise AgentLaunchError(f"Architect criteria request rejected: {str(exc)[:200]}", session_id) from exc
            if code != 0:
                lib.transition_agent_session(root, session_id, "failed", exit_code=1,
                                            failure=lib.classify_runtime_failure(orchestration_noop=True))
                raise AgentLaunchError("Architect criteria transaction was refused by criteria-apply", session_id)
    if architect_results:
        try:
            lib.record_design_proposal(root, session_id, architect_results[0])
        except Exception as exc:
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=1,
                failure=lib.classify_runtime_failure(exit_code=1),
            )
            raise AgentLaunchError(
                f"Architect design proposal dispatch failed: {type(exc).__name__}", session_id,
            ) from exc
    if spec.role == "reviewer":
        before = repository_digest_before.get("entries", {}) if isinstance(repository_digest_before, dict) else {}
        after = lib.repository_digest_entries(root, lib.load_config(root))
        changed_paths = [path for path in sorted(set(before) | set(after))
                         if before.get(path) != after.get(path)][:64]
        digest_changed = isinstance(repository_digest_before, dict) and repository_digest_before.get("digest") != lib.repository_digest(root, lib.load_config(root))
        sandboxed = Path(spec.cwd).resolve() != Path(root).resolve()
        if (changed_paths or digest_changed) and not sandboxed:
            # The reviewer ran inside the tree, so a change is its own doing.
            failure = {"category": "reviewer_modified_project",
                       "reason": "managed Reviewer modified the project tree",
                       "tail_sha256": hashlib.sha256(b"").hexdigest(), "changed_paths": changed_paths or ["<repository-digest-changed>"]}
            lib.transition_agent_session(root, session_id, "failed", exit_code=1, failure=failure)
            raise AgentLaunchError("Reviewer modified the project tree", session_id)
        if changed_paths or digest_changed:
            # #92: a sandboxed reviewer cannot write outside its scratch cwd,
            # so a changed tree is the host's doing; attribute it and keep
            # the review rather than blaming the reviewer.
            lib.append_event(root, lib.load_config(root), "host_edited_during_review",
                             "Host edited project during scratch review", session_id=session_id,
                             paths=changed_paths or ["<repository-digest-changed>"])
    if len(reviewer_results) > 1:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            failure=lib.classify_runtime_failure(exit_code=1),
        )
        raise AgentLaunchError("Reviewer emitted more than one structured result", session_id)
    if reviewer_results:
        try:
            import handsoff_broker as broker
            broker.dispatch_reviewer_result(root, session_id, reviewer_results[0])
        except Exception as exc:
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=1,
                failure={"category": "dispatch_failed", "reason": str(exc)[:200], "result_available": True,
                         "tail_sha256": hashlib.sha256(b"").hexdigest()},
            )
            raise AgentLaunchError(
                f"Reviewer result dispatch failed: {str(exc)[:200]}", session_id,
            ) from exc
    if supervisor_requests:
        try:
            import handsoff_broker as broker
            for request in supervisor_requests:
                broker.dispatch_supervisor_request(Path(spec.project_root or spec.cwd), request)
        except KeyboardInterrupt as exc:
            lib.transition_agent_session(
                root, session_id, "cancelled", exit_code=130,
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
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=1,
                failure=lib.classify_runtime_failure(orchestration_noop=True),
            )
            raise AgentLaunchError(
                f"Supervisor broker request rejected: {str(exc)[:200]}", session_id,
            ) from exc
        except Exception as exc:
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=1,
                failure={"category": "dispatch_failed", "reason": str(exc)[:200], "result_available": True,
                         "tail_sha256": hashlib.sha256(b"").hexdigest()},
            )
            raise AgentLaunchError(str(exc)[:200], session_id) from exc
    if spec.role in {"reviewer", "architect", "supervisor"} and not reviewer_results and not architect_results and not supervisor_requests and question_lines[0] == 0:
        failure = {"category": "no_artifact", "reason": "process exited 0 without a protocol result",
                   "tail_sha256": hashlib.sha256((stdout_tail[0] + stderr_tail[0]).encode()).hexdigest()}
        lib.transition_agent_session(root, session_id, "failed", exit_code=0, failure=failure)
        raise AgentLaunchError(failure["reason"], session_id)
    lib.transition_agent_session(root, session_id, "completed", exit_code=0)
    for problem in question_errors:
        sys.stderr.write(f"HANDSOFF_QUESTION_WARNING: {problem}\n")
    return 0


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
        replacement = lib.reserve_agent_replacement(
            root, from_session_id=source_id, trigger=trigger,
            finding_id=quality_finding_id, which=which, snapshotter=snapshotter,
        )
        if replacement["action"] != "launch":
            raise lib.HandsoffError(f"agent replacement paused: {replacement['reason']}")
        safe_handoff = json.dumps(replacement["handoff"], sort_keys=True, separators=(",", ":"))
        completed = _completed_operations_section(root, source_id)
        fallback_input = original_input + "\n\n# Trusted replacement handoff\n\n" + safe_handoff
        if completed:
            fallback_input += "\n\n" + completed
        try:
            current_spec = build_profile_launch_spec(
                root, replacement["role"], fallback_input,
                replacement["selected_profile"], which=which,
            )
        except lib.HandsoffError:
            selected = replacement["selected_profile"]
            lib.claim_precreated_agent_session(
                root, replacement["to_session_id"], role=replacement["role"],
                adapter=selected["adapter"], requested_model=selected["model"],
            )
            lib.transition_agent_session(
                root, replacement["to_session_id"], "failed_to_start",
                failure=lib.classify_runtime_failure(exit_code=-1),
            )
            source_id = replacement["to_session_id"]
            trigger = "runtime_failure"
            quality_finding_id = None
            continue
        try:
            return execute_launch(
                current_spec, timeout=timeout, popen_factory=popen_factory,
                precreated_session_id=replacement["to_session_id"],
            )
        except AgentLaunchError as exc:
            source_id = exc.session_id
            trigger = "runtime_failure"
            quality_finding_id = None


def _raise_question_line(root: Path, role: str, session_id: str, line: str, errors: list[str]) -> bool:
    """#46: `HANDSOFF_QUESTION: <text>` from any role is recorded the moment
    it is read, so the board alerts while the child is still running. A
    failure to record is remembered for the report and never reaches the
    child, the reader, or the session record."""
    stripped = line.strip()
    if not stripped.startswith(lib.QUESTION_PREFIX):
        return False
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


def _parse_reviewer_line(line: str, results: list[dict], errors: list[str]) -> None:
    if not line.startswith(REVIEW_RESULT_PREFIX):
        for logical in _claude_logical_lines(line):
            if logical != line:
                _parse_reviewer_line(logical, results, errors)
        return
    payload = line[len(REVIEW_RESULT_PREFIX):].strip()
    try:
        results.append(__import__("handsoff_broker").parse_reviewer_result(payload))
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
        if not isinstance(current, dict) or current.get("session_id") != session_id:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a configured Handsoff role")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "launch"):
        command = sub.add_parser(name)
        command.add_argument("role", choices=lib.SELECTABLE_AGENT_ROLES)
        command.add_argument("--task", required=True)
        if name == "launch":
            command.add_argument("--timeout", type=int, default=3600)
            command.add_argument(
                "--by", default=None,
                help="runtime actor identity (default: '<resolved-adapter>-<role>')",
            )
            command.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "launch" and os.environ.get(MANAGED_ROLE_ENV):
            raise lib.HandsoffError(
                "managed roles cannot launch nested agents; return a structured request to the host Supervisor"
            )
        spec = build_launch_spec(lib.resolve_root(args.root), args.role, args.task, skip_preflight=getattr(args, "skip_preflight", False), inspection=args.command == "inspect")
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
