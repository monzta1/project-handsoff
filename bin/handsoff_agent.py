#!/usr/bin/env python3
"""Build and launch least-privilege Codex or Claude Code role sessions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
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


SUPERVISOR_REQUEST_PREFIX = "HANDSOFF_BROKER_REQUEST:"


class AgentLaunchError(lib.HandsoffError):
    """A managed child failed after its host-authenticated session existed."""
    def __init__(self, message: str, session_id: str):
        super().__init__(message)
        self.session_id = session_id


def _role_prompt(root: Path, role: str) -> str:
    path = root / "prompts" / f"{role}.md"
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
    root = root.resolve()
    text = f"{_role_prompt(root, role)}\n\n# Assigned task\n\n{task}"
    if role in DESIGN_EVIDENCE_ROLES:
        cfg = lib.load_config(root)
        packet = applicable_design_review_packet(root, cfg, role)
        if packet is not None:
            packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
            text = f"{DESIGN_REVIEW_PACKET_HEADING}\n\n{packet_json}\n\n{text}"
        evidence = lib.design_evidence_prompt_section(root, cfg)
        if evidence:
            text = f"{text}\n\n{evidence}"
    return text


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


def build_launch_spec(root: Path, role: str, task: str, *, which=shutil.which) -> LaunchSpec:
    root = root.resolve()
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise lib.HandsoffError("role must be architect, implementer, or reviewer")
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    cfg = lib.load_config(root)
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
    executable = which(adapter)
    if not executable:
        # An auto-detected adapter is by construction installed, so only a
        # recommended default or an explicit selection can land here.
        raise lib.HandsoffError(lib.unavailable_adapter_message(role, adapter, model, resolution_source))
    executable = str(Path(executable).resolve())

    if adapter == "codex":
        sandbox = "read-only" if role in {"reviewer", "supervisor"} else "workspace-write"
        argv = [executable, "exec", "--ephemeral", "--sandbox", sandbox]
        if model != lib.DEFAULT_AGENT_MODEL:
            argv.extend(["--model", model])
        argv.append("-")
    else:
        permission_mode = "plan" if role in {"reviewer", "supervisor"} else "acceptEdits"
        argv = [executable, "-p", "--permission-mode", permission_mode]
        if model != lib.DEFAULT_AGENT_MODEL:
            argv.extend(["--model", model])

    return LaunchSpec(
        role=role,
        adapter=adapter,
        model=model,
        argv=tuple(argv),
        cwd=str(root),
        stdin=stdin,
        resolution_source=resolution_source,
        packet_id=packet["packet_id"] if packet else None,
        design_hash=packet["design_hash"] if packet else None,
        tier=tier,
        tier_reason=tier_reason,
    )


def build_profile_launch_spec(root: Path, role: str, task: str, profile: dict,
                              *, which=shutil.which) -> LaunchSpec:
    """Build a launch only from the exact fallback profile reserved by the host."""
    if role not in lib.SELECTABLE_AGENT_ROLES or not isinstance(profile, dict) \
            or set(profile) != {"adapter", "model"}:
        raise lib.HandsoffError("reserved fallback profile is invalid")
    adapter = profile.get("adapter")
    model = lib.validate_agent_model(profile.get("model"))
    if adapter not in lib.SELECTABLE_AGENT_ADAPTERS:
        raise lib.HandsoffError("reserved fallback adapter is invalid")
    executable = which(adapter)
    if not executable:
        raise lib.HandsoffError(f"reserved {adapter} executable is no longer available")
    executable = str(Path(executable).resolve())
    if adapter == "codex":
        sandbox = "read-only" if role in {"reviewer", "supervisor"} else "workspace-write"
        argv = [executable, "exec", "--ephemeral", "--sandbox", sandbox]
        if model != lib.DEFAULT_AGENT_MODEL:
            argv.extend(["--model", model])
        argv.append("-")
    else:
        mode = "plan" if role in {"reviewer", "supervisor"} else "acceptEdits"
        argv = [executable, "-p", "--permission-mode", mode]
        if model != lib.DEFAULT_AGENT_MODEL:
            argv.extend(["--model", model])
    return LaunchSpec(role, adapter, model, tuple(argv), str(root.resolve()), task, "fallback")


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
        self.pid = _process_pid(process)
        self.interval = max(float(interval), 0.01)
        self.stop = threading.Event()
        self.thread = None

    def _beat(self) -> None:
        while not self.stop.is_set():
            lib.write_live_beacon(self.root, session_id=self.session_id, role=self.role,
                                  state="running", pid=self.pid)
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


def execute_launch(spec: LaunchSpec, *, timeout: int = 3600, actor: str | None = None,
                   popen_factory=subprocess.Popen, session_id_factory=None,
                   precreated_session_id: str | None = None,
                   beacon_interval: float = lib.LIVE_BEACON_INTERVAL_SECONDS) -> int:
    """Run one managed session and record its lifecycle without payload data.

    `beacon_interval` (seconds) paces the #33 liveness beacon; tests inject
    a short one. The beacon never gates or alters the lifecycle below."""
    capture_supervisor = spec.role == "supervisor"
    root = Path(spec.cwd).resolve()
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
    try:
        process = popen_factory(
            list(spec.argv),
            cwd=spec.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_supervisor else None,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        failure = lib.classify_runtime_failure(exit_code=-1)
        lib.transition_agent_session(root, session_id, "failed_to_start", failure=failure)
        raise AgentLaunchError(
            f"{spec.adapter} process failed to start: {type(exc).__name__}", session_id,
        ) from exc
    beacon = _LiveBeacon(root, session_id, spec.role, process, beacon_interval)
    beacon.start()
    try:
        return _run_managed_process(spec, root, session_id, process, timeout, capture_supervisor)
    finally:
        beacon.finish()


def _run_managed_process(spec: LaunchSpec, root: Path, session_id: str, process,
                         timeout: int, capture_supervisor: bool) -> int:
    """The lifecycle of an already-started child: exactly one terminal
    transition on every path, no payload data recorded."""
    supervisor_requests: list[dict] = []
    protocol_errors: list[str] = []
    stdout_tail = [""]
    stderr_tail = [""]
    try:
        lib.transition_agent_session(root, session_id, "running")
    except Exception:
        _stop_process_group(process)
        raise
    reader = None
    stderr_reader = None
    reader_errors: list[BaseException] = []
    if capture_supervisor:
        def stream_and_parse() -> None:
            try:
                pending = ""
                discarding = False
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
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
                        _parse_supervisor_line(line, supervisor_requests, protocol_errors)
                    if len(pending.encode("utf-8")) > 65536:
                        if pending.startswith(SUPERVISOR_REQUEST_PREFIX):
                            protocol_errors.append("Supervisor broker request exceeded 65536 bytes")
                        pending = ""
                        discarding = True
                if pending and not discarding:
                    _parse_supervisor_line(pending, supervisor_requests, protocol_errors)
            except BaseException as exc:
                reader_errors.append(exc)

        try:
            reader = threading.Thread(target=stream_and_parse, daemon=True)
            reader.start()
        except Exception as exc:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError(
                f"Supervisor output stream failed to start: {type(exc).__name__}", session_id,
            ) from exc
    if getattr(process, "stderr", None) is not None:
        def stream_stderr() -> None:
            try:
                while True:
                    chunk = process.stderr.read(4096)
                    if not chunk:
                        break
                    sys.stderr.write(chunk)
                    sys.stderr.flush()
                    stderr_tail[0] = (stderr_tail[0] + chunk)[-8192:]
            except BaseException as exc:
                reader_errors.append(exc)
            finally:
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
        if capture_supervisor or stderr_reader:
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
                f"Supervisor output stream failed: {type(exc).__name__}", session_id,
            ) from exc
        if reader_alive:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=True)
            raise AgentLaunchError("Supervisor output stream did not close", session_id)
        if reader_errors:
            _terminalize_runner_io_failure(root, session_id, process, stop_first=False)
            raise AgentLaunchError(
                f"Supervisor output stream failed: {type(reader_errors[0]).__name__}", session_id,
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
        failure = lib.classify_runtime_failure(
            exit_code=process.returncode, stderr_tail=stderr_tail[0], stdout_tail=stdout_tail[0],
        )
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=process.returncode, failure=failure,
        )
        raise AgentLaunchError(f"{spec.adapter} exited with status {process.returncode}", session_id)
    if protocol_errors:
        lib.transition_agent_session(
            root, session_id, "failed", exit_code=1,
            failure=lib.classify_runtime_failure(exit_code=1),
        )
        raise AgentLaunchError(protocol_errors[0], session_id)
    if supervisor_requests:
        try:
            import handsoff_broker as broker
            for request in supervisor_requests:
                broker.dispatch_supervisor_request(Path(spec.cwd), request)
        except KeyboardInterrupt as exc:
            lib.transition_agent_session(
                root, session_id, "cancelled", exit_code=130,
                failure=lib.classify_runtime_failure(cancelled=True),
            )
            raise AgentLaunchError("agent launch cancelled", session_id) from exc
        except Exception as exc:
            lib.transition_agent_session(
                root, session_id, "failed", exit_code=1,
                failure=lib.classify_runtime_failure(exit_code=1),
            )
            raise AgentLaunchError(f"Supervisor broker request failed: {type(exc).__name__}", session_id) from exc
    lib.transition_agent_session(root, session_id, "completed", exit_code=0)
    return 0


def execute_with_recovery(spec: LaunchSpec | None, *, timeout: int = 3600,
                          from_session_id: str | None = None,
                          quality_finding_id: str | None = None,
                          popen_factory=subprocess.Popen, which=shutil.which,
                          snapshotter=lib.repository_snapshot) -> int:
    """Run and, only on authenticated recoverable outcomes, consume CAS reservations."""
    if spec is None and from_session_id is None:
        raise lib.HandsoffError("recovery requires a launch or a trusted completed session")
    root = Path(spec.cwd).resolve() if spec else None
    if root is None:
        raise lib.HandsoffError("quality recovery requires an in-memory launch context")
    current_spec = spec
    original_input = spec.stdin
    trigger = "quality_finding" if quality_finding_id else "runtime_failure"
    source_id = from_session_id
    while True:
        if source_id is None:
            try:
                return execute_launch(current_spec, timeout=timeout, popen_factory=popen_factory)
            except AgentLaunchError as exc:
                source_id = exc.session_id
        replacement = lib.reserve_agent_replacement(
            root, from_session_id=source_id, trigger=trigger,
            finding_id=quality_finding_id, which=which, snapshotter=snapshotter,
        )
        if replacement["action"] != "launch":
            raise lib.HandsoffError(f"agent replacement paused: {replacement['reason']}")
        safe_handoff = json.dumps(replacement["handoff"], sort_keys=True, separators=(",", ":"))
        fallback_input = original_input + "\n\n# Trusted replacement handoff\n\n" + safe_handoff
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


def _parse_supervisor_line(line: str, requests: list[dict], errors: list[str]) -> None:
    if not line.startswith(SUPERVISOR_REQUEST_PREFIX):
        return
    payload = line[len(SUPERVISOR_REQUEST_PREFIX):].strip()
    try:
        requests.append(__import__("handsoff_broker").parse_request(payload))
    except lib.HandsoffError as exc:
        errors.append(str(exc))


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
    args = parser.parse_args()
    try:
        spec = build_launch_spec(lib.resolve_root(args.root), args.role, args.task)
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
                "fresh_session": True,
            }, indent=2))
            return 0
        if args.timeout <= 0:
            raise lib.HandsoffError("--timeout must be positive")
        return execute_launch(spec, timeout=args.timeout, actor=args.by)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
