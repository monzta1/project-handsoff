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


SUPERVISOR_REQUEST_PREFIX = "HANDSOFF_BROKER_REQUEST:"


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


def build_launch_spec(root: Path, role: str, task: str, *, which=shutil.which) -> LaunchSpec:
    root = root.resolve()
    if role not in lib.SELECTABLE_AGENT_ROLES:
        raise lib.HandsoffError("role must be architect, implementer, or reviewer")
    if not isinstance(task, str) or not task.strip():
        raise lib.HandsoffError("task must be a non-empty string")
    cfg = lib.load_config(root)
    profile = lib.agent_profiles(cfg)[role]
    adapter = profile["adapter"]
    if adapter not in lib.SELECTABLE_AGENT_ADAPTERS:
        raise lib.HandsoffError(f"role {role} uses unsupported adapter: {adapter}")
    model = lib.validate_agent_model(profile["model"])
    prompt = _role_prompt(root, role)
    executable = which(adapter)
    if not executable:
        raise lib.HandsoffError(
            f"{adapter} executable is not available on PATH; availability does not prove authentication, "
            "entitlement, network access, or model validity"
        )
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

    stdin = f"{prompt}\n\n# Assigned task\n\n{task}"
    return LaunchSpec(
        role=role,
        adapter=adapter,
        model=model,
        argv=tuple(argv),
        cwd=str(root),
        stdin=stdin,
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


def execute_launch(spec: LaunchSpec, *, timeout: int = 3600, popen_factory=subprocess.Popen) -> int:
    """Stream runner output directly and propagate failures without capture."""
    supervisor_requests: list[dict] = []
    protocol_errors: list[str] = []
    capture_supervisor = spec.role == "supervisor"
    process = popen_factory(
        list(spec.argv),
        cwd=spec.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture_supervisor else None,
        stderr=None,
        text=True,
        shell=False,
        start_new_session=True,
    )
    reader = None
    if capture_supervisor:
        def stream_and_parse() -> None:
            pending = ""
            discarding = False
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                sys.stdout.write(chunk)
                sys.stdout.flush()
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

        reader = threading.Thread(target=stream_and_parse, daemon=True)
        reader.start()
    try:
        if capture_supervisor:
            process.stdin.write(spec.stdin)
            process.stdin.close()
            process.wait(timeout=timeout)
        else:
            process.communicate(input=spec.stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _stop_process_group(process)
        if reader:
            reader.join(timeout=5)
        raise lib.HandsoffError(f"agent launch timed out after {timeout} seconds") from exc
    except KeyboardInterrupt as exc:
        _stop_process_group(process)
        if reader:
            reader.join(timeout=5)
        raise lib.HandsoffError("agent launch cancelled") from exc
    if reader:
        reader.join(timeout=5)
        if reader.is_alive():
            raise lib.HandsoffError("Supervisor output stream did not close")
    if process.returncode:
        raise lib.HandsoffError(f"{spec.adapter} exited with status {process.returncode}")
    if protocol_errors:
        raise lib.HandsoffError(protocol_errors[0])
    if supervisor_requests:
        import handsoff_broker as broker
        for request in supervisor_requests:
            broker.dispatch_supervisor_request(Path(spec.cwd), request)
    return 0


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
    args = parser.parse_args()
    try:
        spec = build_launch_spec(lib.resolve_root(args.root), args.role, args.task)
        if args.command == "inspect":
            print(json.dumps({
                "role": spec.role,
                "adapter": spec.adapter,
                "model": spec.model,
                "argv": list(spec.argv),
                "cwd": spec.cwd,
                "stdin_bytes": len(spec.stdin.encode("utf-8")),
                "stdin_sha256": hashlib.sha256(spec.stdin.encode("utf-8")).hexdigest(),
                "fresh_session": True,
            }, indent=2))
            return 0
        if args.timeout <= 0:
            raise lib.HandsoffError("--timeout must be positive")
        return execute_launch(spec, timeout=args.timeout)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
