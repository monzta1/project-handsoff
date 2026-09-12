#!/usr/bin/env python3
"""Local dashboard for Project Handsoff.

Most workflow artifacts remain read-only. Two narrow same-origin endpoints
exist: Agent Settings atomically persists allowlisted role profiles, and the
design-approval action invokes the same guarded command as the supervisor CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402


ASSET_ROOT = Path(__file__).resolve().parent.parent / "dashboard"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_SETTINGS_BODY = 4096


def _settings_view(cfg: dict) -> dict:
    return {
        "agents": dict(cfg.get("agents", {})),
        "profiles": lib.agent_profiles(cfg),
        "allowed_adapters": list(lib.SELECTABLE_AGENT_ADAPTERS),
        "availability": lib.adapter_availability(),
        "availability_scope": (
            "Executable discovery only; it does not prove authentication, account entitlement, "
            "network access, or model validity."
        ),
        "providers": lib.provider_status(),
        "providers_scope": (
            "Detection only, shown for information: CLI providers are checked for an executable on "
            "PATH, credentialed providers for the presence of an environment variable name. Handsoff "
            "never reads, displays, or stores credential values -- you supply them yourself."
        ),
    }


def _strict_json_object(payload: bytes) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise lib.HandsoffError(f"invalid settings JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise lib.HandsoffError("settings payload must be a JSON object")
    return value


def _read_events(root: Path, cfg: dict) -> list[dict]:
    path = lib.event_log_path(root, cfg)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def _artifact_signature(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap, lock-consistent fingerprint of dashboard inputs.

    SSE carries only an invalidation signal. `/api/dashboard` remains the
    authoritative snapshot, which keeps event delivery small and avoids
    duplicating the gate calculations in two code paths.
    """
    with lib.project_lock(root):
        try:
            cfg = lib.load_config(root)
            paths = [
                root / "handsoff.toml",
                lib.status_path(root, cfg),
                lib.acceptance_path(root, cfg),
                lib.event_log_path(root, cfg),
                lib.verification_log_path(root, cfg),
            ]
        except lib.HandsoffError:
            paths = [root / "handsoff.toml"]
        signature = []
        for path in paths:
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(path), -1, -1))
        return tuple(signature)


def _phase_view(current: int, run_complete: bool) -> list[dict]:
    """The current phase renders "active" (the pulsing in-progress bar) only
    while the run is still moving. Once status is complete, phase 8 being
    "current" no longer means "in progress", so it renders solid-complete
    like every phase before it instead of blinking forever.
    """
    return [
        {
            "number": number,
            "name": name,
            "state": ("complete" if number < current or (number == current and run_complete)
                      else "active" if number == current else "upcoming"),
        }
        for number, name in lib.PHASES.items()
    ]


#: Which crew role is doing the work during each phase, for the dashboard's
#: chiclet row. Phase 2 is resolved dynamically below because a design
#: finding hands work back from the reviewer to the Architect.
ACTIVE_ROLE_BY_PHASE = {
    1: "architect", 3: "supervisor",
    4: "implementer", 5: "reviewer", 6: "implementer",
    7: "supervisor", 8: "supervisor",
}


def _active_role(status: dict, input_request: dict) -> str | None:
    """The crew role currently doing the work, or None when nobody is: the
    run is complete, or it is paused waiting on a human decision (the
    input-required banner already covers that case, so the chiclets go
    dark rather than falsely claiming the Supervisor is mid-task).
    """
    if status.get("status") == "complete":
        return None
    if input_request.get("required"):
        return None
    phase_number = int(status.get("phase_number", 1) or 1)
    if phase_number == 2:
        review = status.get("design_review") or {}
        return "architect" if review.get("decision") == "changes_requested" else "reviewer"
    return ACTIVE_ROLE_BY_PHASE.get(phase_number)


def _input_request(status: dict, cfg: dict) -> dict:
    """Translate an explicit workflow pause into a dashboard alert.

    Supervisors record user-dependent pauses as status=blocked with the exact
    request in next_action. Phase 7's explicit approval wait is inherently a
    user pause, so it is surfaced even without a separate blocked transition.
    A narrow phrase check supports older state written before that convention.
    """
    workflow_status = str(status.get("status") or "")
    phase = int(status.get("phase_number", 1) or 1)
    next_action = str(status.get("next_action") or "Pilot authorization is required before the mission can continue.")
    approval_missing = (
        cfg.get("deployment_requires_explicit_approval", True)
        and phase == 7
        and not status.get("deployment_approved")
    )
    design_review = status.get("design_review") or {}
    design_approval_missing = (
        status.get("requires_design_approval") is True
        and phase == 2
        and not status.get("design_approved")
        and design_review.get("decision") == "approved"
    )
    older_signal = any(phrase in next_action.lower() for phrase in (
        "waiting for user", "waiting on user", "your input", "need your decision",
        "need your approval", "provide credentials", "grant permission", "authorize",
    ))
    required = workflow_status == "blocked" or approval_missing or design_approval_missing or older_signal
    if approval_missing:
        kind = "deployment_approval"
        message = "Pilot authorization required: grant explicit deployment approval before live verification can continue."
    elif design_approval_missing:
        kind = "design_approval"
        message = "Independent design review is approved. Authorize this exact design to open Phase 3."
    elif workflow_status == "blocked":
        kind = "blocked"
        message = next_action
    else:
        kind = "decision"
        message = next_action
    return {"required": required, "kind": kind if required else None, "message": message if required else None}


def _supervisor_briefing(status: dict, criteria: list[dict], errors: list[str],
                         audit_errors: list[str], stall: str | None, activity: str | None,
                         latest_event: dict | None, input_request: dict) -> dict:
    phase_number = int(status.get("phase_number", 1) or 1)
    phase = status.get("phase") or lib.PHASES.get(phase_number, "Unknown phase")
    progress = status.get("progress", 0)
    passing = sum(c.get("state") == "passing" for c in criteria)
    total = len(criteria)
    resolved = status.get("requirement_coverage", {}).get("original_symptom_resolved") is True
    all_errors = [*audit_errors, *errors]
    blocked = [c for c in criteria if c.get("state") == "blocked"]
    failing = [c for c in criteria if c.get("state") == "failing"]

    if input_request["required"]:
        tone = "critical"
        label = "Pilot authorization required"
        headline = "Holding position. Awaiting your command, Pilot."
        summary = input_request["message"]
    elif all_errors:
        tone = "critical"
        label = "Safety interlock"
        headline = "Tactical advance suspended, Pilot."
        summary = (f"The feature is at Phase {phase_number}, {phase}, with {progress}% reported progress. "
                   f"I found {len(all_errors)} condition{'s' if len(all_errors) != 1 else ''} that must be resolved before advancement.")
    elif stall:
        tone = "warning"
        label = "Telemetry interruption"
        headline = "Mission telemetry has gone silent, Pilot."
        summary = (f"No unsafe transition has occurred. Work remains at Phase {phase_number}, {phase}, "
                   f"with {passing} of {total} acceptance criteria verified.")
    elif activity:
        tone = "steady"
        label = "Background operation"
        headline = "Systems active. Background sequence in progress, Pilot."
        summary = (f"{activity}. Work remains at Phase {phase_number}, {phase}, "
                   f"with {passing} of {total} acceptance criteria verified.")
    elif blocked or failing:
        tone = "warning"
        label = "Objectives unresolved"
        headline = "Holding trajectory until Mission Objectives are verified, Pilot."
        summary = (f"We are in Phase {phase_number}, {phase}, at {progress}%. "
                   f"{passing} of {total} criteria are passing; {len(failing)} are failing and {len(blocked)} are blocked.")
    elif phase_number == 8 and status.get("status") == "complete":
        tone = "success"
        label = "Mission complete"
        headline = "Objectives neutralized. Mission complete, Pilot."
        summary = (f"All {total} acceptance criteria are evidenced, the original symptom is resolved, "
                   "and the live verification gate has passed.")
    else:
        tone = "steady"
        label = "Trajectory stable"
        headline = f"System online. {phase} sequence active, Pilot."
        summary = (f"Progress is {progress}% with {passing} of {total} acceptance criteria verified. "
                   f"The original symptom is {'confirmed resolved' if resolved else 'not yet confirmed resolved'}.")

    attention = []
    if stall:
        attention.append(stall)
    attention.extend(all_errors[:4])
    for criterion in [*blocked, *failing]:
        if len(attention) >= 6:
            break
        attention.append(f"{criterion.get('id', 'Criterion')}: {criterion.get('requirement', 'Needs attention')}")
    if not attention and not resolved:
        attention.append("Original symptom still needs a successful evidence record.")

    completed = []
    if passing:
        completed.append(f"{passing} acceptance criterion{'s' if passing != 1 else ''} verified")
    if resolved:
        completed.append("Original symptom verified as resolved")
    if status.get("review"):
        completed.append("Independent review recorded")
    if status.get("deployment_approved"):
        completed.append("Deployment approval recorded")
    if status.get("live_verification_id"):
        completed.append("Live verification passed")

    return {
        "tone": tone,
        "label": label,
        "headline": headline,
        "summary": status.get("summary") or summary,
        "reassurance": status.get("reassurance") or "Reactor core stable. Safety interlocks active. I will advance only when every required gate is satisfied.",
        "next_action": input_request["message"] or status.get("next_action") or lib.NEXT_ACTION_DEFAULTS.get(phase_number, "Review the current state."),
        "attention": attention,
        "completed": completed,
        "latest_event": ({"kind": latest_event.get("kind"), "message": latest_event.get("message"),
                          "at": latest_event.get("at")} if latest_event else None),
    }


def build_snapshot(root: Path) -> dict:
    """Build one coherent dashboard snapshot while holding the project lock."""
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        cfg = lib.load_config(root)
    except lib.HandsoffError as exc:
        return {"initialized": False, "generated_at": generated_at, "root": str(root), "error": str(exc)}

    status_file = lib.status_path(root, cfg)
    acceptance_file = lib.acceptance_path(root, cfg)
    if not status_file.exists() or not acceptance_file.exists():
        return {
            "initialized": False,
            "generated_at": generated_at,
            "root": str(root),
            "error": "System online. No active Mission Objective was found in this project, Pilot.",
            "settings": _settings_view(cfg),
        }

    try:
        with lib.project_lock(root):
            status = lib.load_unique_json(status_file)
            acceptance = lib.load_unique_json(acceptance_file)
            verifications, verification_problems = lib.load_verifications(root, cfg)
            events = _read_events(root, cfg)
            gate_errors = lib.compute_errors(status, acceptance, cfg, verifications=verifications,
                                             verification_problems=verification_problems)
            event_errors = lib.verify_event_log(root, cfg)
            actual_head = verifications[-1].get("hash") if verifications else "GENESIS"
            audit_errors = [*event_errors]
            if status.get("verification_head") != actual_head:
                audit_errors.append("Verification ledger tail does not match its anchored head.")
            stall = lib.stall_warning(status, cfg)
            activity = lib.activity_note(status, cfg)
    except (lib.HandsoffError, OSError) as exc:
        return {"initialized": False, "generated_at": generated_at, "root": str(root), "error": str(exc)}

    criteria = acceptance.get("criteria", [])
    latest_event = events[-1] if events else None
    coverage = status.get("requirement_coverage", {})
    audit_healthy = not gate_errors and not audit_errors
    input_request = _input_request(status, cfg)
    return {
        "initialized": True,
        "generated_at": generated_at,
        "root": str(root),
        "project": {"name": root.name, "feature": status.get("feature", acceptance.get("feature", "Untitled feature"))},
        "status": status,
        "phases": _phase_view(int(status.get("phase_number", 1) or 1), status.get("status") == "complete"),
        "acceptance": {
            "criteria": criteria,
            "passing": coverage.get("passing", 0),
            "failing": coverage.get("failing", 0),
            "not_tested": coverage.get("not_tested", 0),
            "blocked": coverage.get("blocked", 0),
            "total": len(criteria),
            "original_symptom_resolved": coverage.get("original_symptom_resolved") is True,
        },
        "actors": {
            "architect": ((status.get("design_review") or {}).get("architect")
                          or (status.get("design_approved") or {}).get("architect")),
            "design_reviewed_by": (status.get("design_review") or {}).get("by"),
            "implemented_by": status.get("implemented_by"),
            "reviewed_by": status.get("reviewed_by"),
            "approved_by": (status.get("deployment_approved") or {}).get("by"),
            "active_role": _active_role(status, input_request),
        },
        "audit": {
            "healthy": audit_healthy,
            "gate_errors": gate_errors,
            "chain_errors": audit_errors,
            "verification_runs": len(verifications),
            "event_count": len(events),
            "verification_head": status.get("verification_head"),
        },
        "policy": {
            "review_round": status.get("review_round", 0),
            "max_review_rounds": cfg.get("max_review_rounds"),
            "design_round": status.get("design_round", 0),
            "max_design_rounds": cfg.get("max_design_rounds"),
            "explicit_approval": cfg.get("deployment_requires_explicit_approval", True),
            "live_verification": cfg.get("require_live_verification", True),
            "configured_checks": len(cfg.get("check_commands", [])),
            "configured_live_checks": len(cfg.get("live_check_commands", [])),
        },
        "settings": _settings_view(cfg),
        "input_required": input_request,
        "activity_note": activity,
        "supervisor": _supervisor_briefing(status, criteria, gate_errors, audit_errors, stall, activity,
                                            latest_event, input_request),
        "events": list(reversed(events[-12:])),
        "verifications": list(reversed(verifications[-8:])),
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.project_root = root
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        self.end_headers()

    def _sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        self.end_headers()

    def _json_response(self, status: HTTPStatus, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _same_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and port == self.server.server_port
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )

    def _send_event(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    def _serve_events(self) -> None:
        self._sse_headers()
        signature = _artifact_signature(self.server.project_root)
        last_keepalive = time.monotonic()
        self.wfile.write(b"retry: 1000\n")
        self._send_event("ready", {"connected": True})
        while True:
            time.sleep(0.2)
            current = _artifact_signature(self.server.project_root)
            if current != signature:
                signature = current
                self._send_event("invalidate", {"changed": True})
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 15:
                self.wfile.write(b": telemetry keepalive\n\n")
                self.wfile.flush()
                last_keepalive = time.monotonic()

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/dashboard":
            payload = json.dumps(build_snapshot(self.server.project_root), separators=(",", ":")).encode("utf-8")
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if path == "/api/events":
            try:
                self._serve_events()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/healthz":
            payload = b'{"ok":true}'
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        asset = ASSETS.get(path)
        if asset:
            asset_path = ASSET_ROOT / asset[0]
            try:
                payload = asset_path.read_bytes()
            except OSError:
                payload = b"Dashboard assets are missing. Copy the dashboard/ directory beside bin/."
                self._headers(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            self._headers(HTTPStatus.OK, asset[1], len(payload))
            self.wfile.write(payload)
            return
        payload = b"Not found"
        self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/api/settings/agents", "/api/design-approval"}:
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if not self._same_origin_allowed():
            self._json_response(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Same-origin dashboard request required"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 1:
            self._json_response(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "A settings request body is required"})
            return
        if length > MAX_SETTINGS_BODY:
            self._json_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Settings request is too large"})
            return
        try:
            requested = _strict_json_object(self.rfile.read(length))
            if path == "/api/design-approval":
                if requested:
                    raise lib.HandsoffError("design approval payload must be empty")
                snapshot = build_snapshot(self.server.project_root)
                input_request = snapshot.get("input_required") or {}
                review = (snapshot.get("status") or {}).get("design_review") or {}
                architect = review.get("architect")
                if input_request.get("kind") != "design_approval" or not architect:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The current mission is not awaiting design authorization"},
                    )
                    return
                command = argparse.Namespace(
                    root=str(self.server.project_root),
                    by="Mission Control Pilot",
                    architect=architect,
                    summary="Pilot authorized the independently reviewed design in Mission Control.",
                    redesigns_settled_work=None,
                )
                if supervisor.cmd_design_approve(command) != 0:
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "The design approval gate rejected this authorization"},
                    )
                    return
                approved = lib.load_unique_json(lib.status_path(
                    self.server.project_root, lib.load_config(self.server.project_root)
                )).get("design_approved")
                self._json_response(HTTPStatus.OK, {"ok": True, "design_approved": approved})
                return
            lib.update_agent_config(self.server.project_root, requested)
            effective_cfg = lib.load_config(self.server.project_root)
        except lib.HandsoffError as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except OSError:
            self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Agent settings could not be saved"})
            return
        response = _settings_view(effective_cfg)
        response["agents"] = {role: effective_cfg["agents"][role] for role in requested}
        self._json_response(HTTPStatus.OK, {"ok": True, **response})


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise lib.HandsoffError("dashboard binds to localhost only; use an authenticated reverse proxy for remote access")
    if not ASSET_ROOT.is_dir():
        raise lib.HandsoffError(f"dashboard assets are missing at {ASSET_ROOT}")
    server = DashboardServer((host, port), root)
    actual_host, actual_port = server.server_address[:2]
    browser_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    url = f"http://{browser_host}:{actual_port}/"
    print(f"HANDSOFF_DASHBOARD: {url}")
    print(f"PROJECT_ROOT: {root}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nHANDSOFF_DASHBOARD_STOPPED")
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Handsoff local dashboard")
    parser.add_argument("--root", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        return serve(lib.resolve_root(args.root), args.host, args.port, not args.no_open)
    except lib.HandsoffError as exc:
        print(f"SHIP_FEATURE_BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
