#!/usr/bin/env python3
"""The ollama adapter (#305): a local model behind the #304 contract.

Ollama is not a CLI agent the way codex and claude are, so Handsoff brings
the agent loop itself. The managed launch path starts this file as an
ordinary child process (`python3 handsoff_ollama.py run ...`), writes the
role packet to its stdin and reads its stdout exactly as it reads codex or
claude: protocol lines are parsed, usage lines are metered, and the session
is recorded the same way. Nothing in the engine pretends this is codex or
claude; it is the `ollama` adapter, registered in `handsoff_adapters`.

The loop talks to Ollama's HTTP API (`/api/chat` with native tool calling)
and offers the model three tools, all read-only and all confined to the
project root: `read_file`, `list_files` and `search_text`. There is no
write tool and no command tool, which is why ollama never serves the
implementer role and why a reviewer's read-only boundary is enforced by
construction rather than by request.

Each model turn prints one JSON line the engine's usage watcher reads:

    {"type": "handsoff_ollama_turn", "model": "<reported>", "usage": {...}}

`usage` carries cumulative input and output token counts when Ollama
reports them, and is absent (`usage_available: false`) when it does not,
so an unmetered session is recorded as unreported rather than as zero.
The model Ollama reports is printed as reported (its `:latest` alias of an
untagged name is the same model); a different model than the one requested
stops the loop, and the engine refuses the session as
`model_identity_mismatch`.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

NAME = "ollama"

#: What an ollama pre-flight can say, in the order a launch meets them.
PREFLIGHT_STATES = ("service_unavailable", "model_not_pulled", "model_not_loadable", "ready")

#: The contract category each blocked state maps to, so the shared
#: `contract_preflight_state` names it without knowing about ollama.
PREFLIGHT_CATEGORIES = {"service_unavailable": "runtime_not_ready",
                        "model_not_pulled": "model_unavailable",
                        "model_not_loadable": "model_unavailable"}

TURN_EVENT = "handsoff_ollama_turn"
MAX_TURNS = 32
MAX_TOOL_OUTPUT_BYTES = 32 * 1024
MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_BYTES = 1024 * 1024
#: The most a single turn may generate; the remaining budget lowers it.
MAX_TURN_OUTPUT_TOKENS = 8192
SKIPPED_DIRECTORIES = {".git", "__pycache__", "node_modules"}

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file inside the project. Lines are numbered from 1.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Path relative to the project root."},
            "start_line": {"type": "integer", "description": "First line to return (default 1)."},
            "max_lines": {"type": "integer", "description": f"At most this many lines (default {MAX_READ_LINES})."},
        }},
    }},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files and directories inside the project.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to the project root (default '.')."},
            "recursive": {"type": "boolean", "description": "Walk subdirectories too (default false)."},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_text",
        "description": "Find lines containing a literal string in project files.",
        "parameters": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string", "description": "The literal text to find (case-sensitive)."},
            "path": {"type": "string", "description": "File or directory to search (default '.')."},
        }},
    }},
]

SYSTEM_PROMPT = (
    "You are the Handsoff {role}, running on a local model. You can see the project only "
    "through the tools read_file, list_files and search_text, which are read-only and confined "
    "to the project root. You cannot edit files or run commands. Use the tools to check what "
    "you need, then give your final answer as plain text, with any protocol line the task "
    "requires on a line of its own."
)


class OllamaUnavailable(Exception):
    """The service did not answer at all."""


class OllamaHTTPError(Exception):
    """The service answered with an HTTP error."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ToolError(Exception):
    """A tool call the loop refuses; the model is told why."""


def validate_host(value: object) -> str:
    """scheme://host[:port], nothing else, so a host is never a path or a URL with a query."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ollama host must be a non-empty http(s) origin")
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"} \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("ollama host must be scheme://host[:port] with no path")
    return f"{parts.scheme}://{parts.netloc}"


def _call(host: str, path: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        host.rstrip("/") + path, data=data, method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8", "replace")).get("error") or ""
        except (ValueError, AttributeError, OSError):
            message = ""
        raise OllamaHTTPError(exc.code, str(message)[:200]) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnavailable(type(exc).__name__) from exc
    try:
        decoded = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise OllamaUnavailable("response was not JSON") from exc
    if not isinstance(decoded, dict):
        raise OllamaUnavailable("response was not a JSON object")
    return decoded


def same_model(requested: str, reported: object) -> bool:
    """Ollama names an untagged model `<name>:latest`; that alias is the same model."""
    if not isinstance(reported, str) or not reported:
        return False
    def canonical(name: str) -> str:
        return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"
    return canonical(requested) == canonical(reported)


def probe(host: str, model: str, *, timeout: float = 10.0, load_timeout: float = 120.0) -> dict:
    """Name one of PREFLIGHT_STATES for this host and model, with a reason.

    `/api/tags` answers whether the service is up and the model is pulled;
    `/api/show` whether it can call tools natively (a model that cannot is
    refused rather than emulated by parsing text); an empty `/api/generate`
    loads it, which is the only honest test that it fits in memory.
    """
    try:
        tags = _call(host, "/api/tags", None, timeout)
    except (OllamaUnavailable, OllamaHTTPError) as exc:
        return {"state": "service_unavailable", "reason": f"ollama service did not answer: {exc}"[:200]}
    names = set()
    for entry in tags.get("models") or []:
        if isinstance(entry, dict):
            names.update(value for value in (entry.get("name"), entry.get("model")) if isinstance(value, str))
    if not any(same_model(model, name) for name in names):
        return {"state": "model_not_pulled", "reason": f"model {model} is not pulled; run ollama pull {model}"}
    try:
        shown = _call(host, "/api/show", {"model": model}, timeout)
        capabilities = shown.get("capabilities")
        if isinstance(capabilities, list) and "tools" not in capabilities:
            return {"state": "model_not_loadable",
                    "reason": f"model {model} does not support native tool calling"}
        _call(host, "/api/generate", {"model": model, "prompt": "", "stream": False}, load_timeout)
    except OllamaHTTPError as exc:
        return {"state": "model_not_loadable", "reason": f"model {model} could not be loaded: {exc}"[:200]}
    except OllamaUnavailable as exc:
        return {"state": "service_unavailable", "reason": f"ollama service stopped answering: {exc}"[:200]}
    return {"state": "ready", "reason": f"model {model} is pulled, tool-capable and loaded"}


def _project_settings(project_root) -> dict:
    from handsoff_config import load_config
    return load_config(Path(project_root))["ollama"]


def preflight(root, *, model: str, executable: str, argv, cwd: str, host: str | None = None,
              **_kwargs) -> dict:
    """The contract pre-flight: `launch_preflight`-shaped, plus the ollama state."""
    host = host or _project_settings(root)["host"]
    result = probe(host, model)
    state = result["state"]
    return {"state": "ready" if state == "ready" else "blocked",
            "category": PREFLIGHT_CATEGORIES.get(state), "reason": result["reason"],
            "ollama_state": state}


def build_argv(executable: str, role: str, model: str, *, provider_limit: int, reasoning=None,
               reviewer_sandbox: bool = False, allowed_tools=None, project_root=None,
               owned_paths=None) -> list[str]:
    """The argv for one managed ollama session: this file, run by the interpreter."""
    from handsoff_config import adapter_serves_role
    from handsoff_core import HandsoffError
    if not adapter_serves_role(NAME, role):
        raise HandsoffError(f"ollama does not serve the {role} role")
    if project_root is None:
        raise HandsoffError("an ollama launch needs the project root its tools are confined to")
    settings = _project_settings(project_root)
    return [executable, str(Path(__file__).resolve()), "run", "--host", settings["host"],
            "--model", model, "--role", role, "--project-root", str(Path(project_root).resolve()),
            "--provider-limit", str(int(provider_limit)),
            "--timeout", str(settings["request_timeout_seconds"])]


# -- the read-only tools ---------------------------------------------------

def _confined(root: Path, relative: object) -> Path:
    """Resolve a model-supplied path, refusing anything outside the root.

    Resolution follows symlinks, so a link inside the project that points
    outside it is refused like `../` or an absolute path."""
    if relative is None or relative == "":
        relative = "."
    if not isinstance(relative, str) or "\x00" in relative:
        raise ToolError("path must be a string")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"{relative} is outside the project root")
    return candidate


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() or "."


def _bounded(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    return data[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", "ignore") + "\n[output truncated]"


def _int_argument(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, number))


def read_file(root: Path, arguments: dict) -> str:
    path = _confined(root, arguments.get("path"))
    if not path.is_file():
        raise ToolError(f"{arguments.get('path')} is not a file")
    start = _int_argument(arguments.get("start_line"), 1, 1, 10_000_000)
    count = _int_argument(arguments.get("max_lines"), MAX_READ_LINES, 1, MAX_READ_LINES)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ToolError(f"cannot read {arguments.get('path')}: {type(exc).__name__}") from exc
    selected = lines[start - 1:start - 1 + count]
    body = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start))
    return _bounded(body or "(no lines in range)")


def _walk(root: Path, base: Path, recursive: bool):
    entries = sorted(base.iterdir(), key=lambda item: item.name)
    for entry in entries:
        if entry.name in SKIPPED_DIRECTORIES:
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved != root and root not in resolved.parents:
            continue  # a symlink out of the project is not listed
        yield entry
        if recursive and entry.is_dir() and not entry.is_symlink():
            yield from _walk(root, entry, recursive)


def list_files(root: Path, arguments: dict) -> str:
    base = _confined(root, arguments.get("path"))
    if not base.is_dir():
        raise ToolError(f"{arguments.get('path')} is not a directory")
    lines = []
    for entry in _walk(root, base, bool(arguments.get("recursive"))):
        if len(lines) >= MAX_LIST_ENTRIES:
            lines.append(f"[listing stopped at {MAX_LIST_ENTRIES} entries]")
            break
        lines.append(entry.relative_to(root).as_posix() + ("/" if entry.is_dir() else ""))
    return _bounded("\n".join(lines) or "(empty directory)")


def search_text(root: Path, arguments: dict) -> str:
    needle = arguments.get("text")
    if not isinstance(needle, str) or not needle:
        raise ToolError("text must be a non-empty string")
    base = _confined(root, arguments.get("path"))
    files = [base] if base.is_file() else [entry for entry in _walk(root, base, True) if entry.is_file()] \
        if base.is_dir() else []
    matches = []
    for path in files:
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line:
                matches.append(f"{_relative(root, path.resolve())}:{number}: {line[:200]}")
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return _bounded("\n".join(matches) + f"\n[search stopped at {MAX_SEARCH_MATCHES} matches]")
    return _bounded("\n".join(matches) or "(no matches)")


TOOL_FUNCTIONS = {"read_file": read_file, "list_files": list_files, "search_text": search_text}


def run_tool(root: Path, name: object, arguments: object) -> str:
    """Run one tool call. A refusal is returned to the model, never raised."""
    function = TOOL_FUNCTIONS.get(name) if isinstance(name, str) else None
    if function is None:
        return f"error: no tool named {name!r}; the tools are read_file, list_files and search_text"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return "error: tool arguments must be a JSON object"
    if not isinstance(arguments, dict):
        return "error: tool arguments must be a JSON object"
    try:
        return function(root, arguments)
    except ToolError as exc:
        return f"error: {exc}"


# -- the session loop -------------------------------------------------------

def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def run_session(*, host: str, model: str, role: str, project_root: str, prompt: str,
                provider_limit: int, timeout: float, out=sys.stdout, err=sys.stderr,
                max_turns: int = MAX_TURNS) -> int:
    """Drive one managed role to its final answer. Returns the exit status."""
    root = Path(project_root).resolve()
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(role=role)},
                {"role": "user", "content": prompt}]
    tokens_in = tokens_out = 0
    metered = True
    # Implementation review attempt 1: one unmetered turn used to switch the
    # ceiling off. `charged` is what the ceiling is enforced against: the
    # reported counts when Ollama gives them, otherwise a deliberately high
    # estimate (one token per three characters of what was sent and
    # received), so a session without usage still stops at its limit.
    charged = 0
    for _turn in range(max_turns):
        remaining = provider_limit - charged
        options = {"num_predict": max(1, min(MAX_TURN_OUTPUT_TOKENS, remaining))}
        try:
            response = _call(host, "/api/chat", {"model": model, "messages": messages, "tools": TOOLS,
                                                 "stream": False, "options": options}, timeout)
        except OllamaUnavailable as exc:
            err.write(f"HANDSOFF_OLLAMA_ERROR: ollama service unavailable ({exc})\n")
            return 1
        except OllamaHTTPError as exc:
            err.write(f"HANDSOFF_OLLAMA_ERROR: ollama refused the chat request: {exc}\n")
            return 1
        reported = response.get("model")
        if same_model(model, reported):
            # Ollama's own `:latest` alias is spelled as requested, so the
            # engine's exact identity check sees the same model; any other
            # name is printed as Ollama reported it.
            reported = model
        prompt_count, eval_count = _count(response.get("prompt_eval_count")), _count(response.get("eval_count"))
        event = {"type": TURN_EVENT, "model": reported if isinstance(reported, str) else None}
        if prompt_count is not None and eval_count is not None:
            charged += prompt_count + eval_count
        else:
            sent = len(json.dumps(messages, ensure_ascii=False))
            received = len(json.dumps(response.get("message") or {}, ensure_ascii=False))
            charged += sent // 3 + received // 3 + 2
        if metered and prompt_count is not None and eval_count is not None:
            tokens_in += prompt_count
            tokens_out += eval_count
            event["usage"] = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        else:
            # One unmetered turn makes every later total a guess, so no
            # total is printed again: unreported, never a partial count.
            metered = False
            event["usage_available"] = False
        out.write(json.dumps(event, sort_keys=True) + "\n")
        out.flush()
        if not same_model(model, reported):
            err.write("HANDSOFF_OLLAMA_ERROR: ollama answered with a different model than requested\n")
            return 1
        if charged > provider_limit:
            err.write("HANDSOFF_OLLAMA_ERROR: token budget exhausted for this local session\n")
            return 3
        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            out.write((message.get("content") or "").rstrip("\n") + "\n")
            out.flush()
            return 0
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            function = function if isinstance(function, dict) else {}
            name = function.get("name")
            messages.append({"role": "tool", "tool_name": name if isinstance(name, str) else "",
                             "content": run_tool(root, name, function.get("arguments"))})
    err.write(f"HANDSOFF_OLLAMA_ERROR: no final answer after {max_turns} model turns\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one managed Handsoff role on a local Ollama model.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--host", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--role", required=True, choices=("architect", "supervisor", "reviewer"))
    run.add_argument("--project-root", required=True)
    run.add_argument("--provider-limit", required=True, type=int)
    run.add_argument("--timeout", type=float, default=300.0)
    probe_parser = sub.add_parser("probe")
    probe_parser.add_argument("--host", required=True)
    probe_parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        host = validate_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "probe":
        print(json.dumps(probe(host, args.model), sort_keys=True))
        return 0
    prompt = sys.stdin.read()
    return run_session(host=host, model=args.model, role=args.role, project_root=args.project_root,
                       prompt=prompt, provider_limit=args.provider_limit, timeout=args.timeout)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
