#!/usr/bin/env python3
"""#45: benchmark the design phase with and without the #33 to #40 tranche.

Two arms run the same design task, the same seeded criteria, and the same
seeded structural defects in a temporary copy of a fixture repository at a
pinned revision, through this checkout's own bin/:

- baseline: no reviewer_followup, no [[design_evidence]], and the harness
  never calls design-review-packet (the --no-packets behaviour);
- tranche: the follow-up reviewer tier (#37), design evidence (#38), and
  delta review packets (#36) all on.

Every Architect and Reviewer session is a real managed session through
handsoff_agent.execute_launch, so the run's ledgers, the #35 budget, the #37
tier selection, and the #36 packet delivery are the product's own. With
--stub the adapters are a deterministic script installed by the harness;
with --live they are the real claude and codex executables (a paid step
that needs the Pilot's explicit yes; see docs/benchmark-33-40.md).

Token counts come only from the runner's structured usage output (claude
--output-format json, codex exec --json). When a runner reports none the
value is null, never an estimate.

Stdlib only. Never writes into the operator's Documents archive, never
touches the checkout it runs from, and never invokes an adapter outside the
directory it was told to use.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_lib as lib  # noqa: E402

SUPERVISOR = BIN / "handsoff_supervisor.py"
FIXTURE_DIR = TOOLS_DIR / "benchmark_fixture"
DEFAULT_FIXTURE = FIXTURE_DIR / "fixture.json"
HARNESS_ACTOR = "benchmark-harness"
PILOT_ACTOR = "benchmark-pilot"
ARMS = ("baseline", "tranche")
MODES = ("stub", "live")
WALL_CLOCK_THRESHOLD_PERCENT = 30.0
TOKENS_THRESHOLD_PERCENT = 40.0
SUMMARY_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
STATE_FILE_NAMES = (
    "handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl",
    "handsoff-verifications.jsonl", "handsoff-design-evidence.json",
    ".handsoff.lock", ".handsoff-event-head.json", ".handsoff-writeahead.json",
    ".handsoff-session-liveness.json", ".handsoff-live.json", ".handsoff-output-liveness.json",
    ".handsoff-dashboard-owner.json",
)
#: The nested-session variables a Claude Code child inherits from a parent
#: Claude Code session; a live launch from inside one must drop them or the
#: child refuses to start (the operator recipe for this machine).
NESTED_CLAUDE_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH",
)
STUB_ENV_ROLE = "HANDSOFF_BENCHMARK_ROLE"
STUB_ENV_ATTEMPT = "HANDSOFF_BENCHMARK_ATTEMPT"
STUB_ENV_DEFECTS = "HANDSOFF_BENCHMARK_DEFECTS"
STUB_ENV_FINDINGS = "HANDSOFF_BENCHMARK_FINDINGS"
STUB_ENV_SLEEP = "HANDSOFF_BENCHMARK_STUB_SLEEP"
FINDING_PREFIX = "FINDING:"
RESOLVED_PATTERN = re.compile(r"^\s*RESOLVED\s+(F\d+\.\d+)\b", re.MULTILINE)
DECISION_APPROVED = "DESIGN_APPROVED"
DECISION_CHANGES = "DESIGN_CHANGES_REQUESTED"

STUB_ADAPTER_SOURCE = '''#!{python}
"""Deterministic stand-in for the claude and codex executables (#45 --stub).

Reads the whole role prompt from stdin, sleeps a fixed short time, and
emits a fixed answer in the runner's structured shape: a single JSON
result object with a `usage` block when installed as `claude`, JSONL
events ending in a `turn.completed` usage event when installed as `codex`.
The Reviewer requests changes on attempt 1 (naming every seeded defect id)
and approves on any later attempt. The Architect marks every finding it was
handed as RESOLVED. Nothing here is a measurement.
"""
import json
import math
import os
import sys
import time

role = os.environ.get({role_env!r}, "")
attempt = int(os.environ.get({attempt_env!r}, "1") or "1")
defects = [d for d in os.environ.get({defects_env!r}, "").split(",") if d]
findings = [f for f in os.environ.get({findings_env!r}, "").split(",") if f]
prompt = sys.stdin.read()
time.sleep(float(os.environ.get({sleep_env!r}, "0.2") or "0.2"))
model = "stub-default"
argv = sys.argv[1:]
if "--model" in argv and argv.index("--model") + 1 < len(argv):
    model = argv[argv.index("--model") + 1]
if role == "reviewer":
    if attempt <= 1:
        lines = ["FINDING: " + d + " seeded structural defect is present in the proposed design" for d in defects]
        text = "\\n".join(lines + ["DESIGN_CHANGES_REQUESTED"])
    else:
        text = "Every prior finding is resolved in the revised design.\\nDESIGN_APPROVED"
else:
    lines = ["# Design proposal (stub attempt " + str(attempt) + ")",
             "Approach, data flow, and failure modes are described here."]
    lines += ["RESOLVED " + f + ": addressed in this revision" for f in findings]
    text = "\\n".join(lines)
usage = {{
    "input_tokens": math.ceil(len(prompt.encode("utf-8")) / 4),
    "output_tokens": math.ceil(len(text.encode("utf-8")) / 4),
}}
if os.path.basename(sys.argv[0]) == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": "stub-thread"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{**usage, "cached_input_tokens": 0}}}}))
else:
    print(json.dumps({{
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "usage": usage, "modelUsage": {{model: dict(usage)}},
    }}))
'''


class BenchmarkError(Exception):
    """A harness refusal; printed as BENCHMARK_BLOCKED and exits 1."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"BENCHMARK: {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Runner output parsing: the one place token counts come from.
# --------------------------------------------------------------------------

def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_runner_output(stdout: str) -> dict:
    """What the runner said and what it reported, never what it might have
    used. `claude -p --output-format json` prints one JSON object with
    `result` and `usage`; `codex exec --json` prints JSONL events where an
    `agent_message` item carries the text and `turn.completed` carries
    `usage`. Both shapes reduce to the same record here. Anything else
    (plain text, a runner without structured output) keeps the raw text as
    `text` with every token field null and `usage_source` "none"."""
    text_parts: list[str] = []
    input_tokens = output_tokens = None
    reported_model = None
    usage_source = "none"
    parsed_any = False
    records = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    if not records and stdout.strip().startswith("{"):
        # A pretty-printed single object spans lines; try it whole.
        try:
            whole = json.loads(stdout)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, dict):
            records.append(whole)
    for record in records:
        parsed_any = True
        usage = record.get("usage")
        if isinstance(usage, dict):
            usage_input = _int_or_none(usage.get("input_tokens"))
            usage_output = _int_or_none(usage.get("output_tokens"))
            if usage_input is not None or usage_output is not None:
                input_tokens = (input_tokens or 0) + (usage_input or 0) if usage_input is not None else input_tokens
                output_tokens = (output_tokens or 0) + (usage_output or 0) if usage_output is not None else output_tokens
                usage_source = "runner"
        if isinstance(record.get("result"), str):
            text_parts.append(record["result"])
        item = record.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
        model_usage = record.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            reported_model = ",".join(sorted(str(key) for key in model_usage))
        elif isinstance(record.get("model"), str) and reported_model is None:
            reported_model = record["model"]
    text = "\n".join(text_parts) if text_parts else stdout
    return {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reported_model": reported_model,
        "usage_source": usage_source,
        "structured": parsed_any,
    }


def review_decision(text: str) -> str | None:
    """The reviewer's verdict: the LAST decision token in the text wins, so a
    quoted mention earlier in the prose does not decide the review."""
    last = None
    for match in re.finditer(r"DESIGN_(APPROVED|CHANGES_REQUESTED)", text):
        last = "approved" if match.group(1) == "APPROVED" else "changes_requested"
    return last


def extract_findings(text: str, *, limit: int = 32, max_length: int = 512) -> list[str]:
    findings = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(FINDING_PREFIX):
            body = stripped[len(FINDING_PREFIX):].strip()
            if body:
                findings.append(body[:max_length])
    return findings[:limit]


def defects_found(text: str, defects: list[dict]) -> list[str]:
    """A seeded defect counts as found when the reviewer names its id or any
    of its keywords (case-insensitive). The keywords are the fixture's, so
    the same rule judges both arms."""
    lowered = text.lower()
    found = []
    for defect in defects:
        needles = [defect["id"].lower()] + [k.lower() for k in defect.get("keywords", [])]
        if any(needle in lowered for needle in needles):
            found.append(defect["id"])
    return found


# --------------------------------------------------------------------------
# Fixture and arm configuration
# --------------------------------------------------------------------------

def load_fixture(path: Path) -> dict:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot load fixture {path}: {exc}") from exc
    required = ("feature", "criteria_transaction", "seeded_defects", "profiles")
    missing = [key for key in required if key not in fixture]
    if missing:
        raise BenchmarkError(f"fixture {path} lacks: {', '.join(missing)}")
    defects = fixture["seeded_defects"]
    if not isinstance(defects, list) or not defects or not all(
            isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"] for d in defects):
        raise BenchmarkError("fixture seeded_defects must be a non-empty list of {id, reveals, keywords}")
    if len({d["id"] for d in defects}) != len(defects):
        raise BenchmarkError("fixture seeded_defect ids must be unique")
    profiles = fixture["profiles"]
    for role in ("architect", "reviewer", "reviewer_followup"):
        profile = profiles.get(role)
        if not isinstance(profile, dict) or set(profile) != {"adapter", "model"}:
            raise BenchmarkError(f"fixture profiles.{role} must be {{adapter, model}}")
    fixture.setdefault("max_autonomous_design_reviews", lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS)
    fixture.setdefault("check_commands", ["true"])
    fixture.setdefault("design_evidence", [])
    fixture["_path"] = str(path)
    return fixture


def arm_settings(arm: str, *, no_packets: bool) -> dict:
    if arm == "baseline":
        return {"reviewer_followup": False, "design_evidence": False, "packets": False}
    return {"reviewer_followup": True, "design_evidence": True, "packets": not no_packets}


def render_arm_toml(fixture: dict, arm: str, settings: dict) -> str:
    profiles = fixture["profiles"]
    lines = [
        "[project]", f'name = "benchmark-{arm}"', "",
        "[workflow]", "max_design_rounds = 3", "max_review_rounds = 3", "stall_minutes = 10",
        f"max_autonomous_design_reviews = {int(fixture['max_autonomous_design_reviews'])}",
        "require_live_verification = true", "deployment_requires_explicit_approval = true", "",
        "[agents]",
    ]
    roles = ["architect", "supervisor", "implementer", "reviewer"]
    for role in roles:
        profile = profiles.get(role) or profiles["architect"]
        lines.append(f"{role} = {json.dumps(profile['adapter'])}")
    if settings["reviewer_followup"]:
        lines.append(f"reviewer_followup = {json.dumps(profiles['reviewer_followup']['adapter'])}")
    lines += ["", "[models]"]
    for role in roles:
        profile = profiles.get(role) or profiles["architect"]
        lines.append(f"{role} = {json.dumps(profile['model'])}")
    if settings["reviewer_followup"]:
        lines.append(f"reviewer_followup = {json.dumps(profiles['reviewer_followup']['model'])}")
    lines += [
        "", "[checks]", f"commands = {json.dumps(list(fixture['check_commands']))}",
        "live_commands = []", "timeout_seconds = 120",
    ]
    if settings["design_evidence"]:
        for entry in fixture["design_evidence"]:
            lines += [
                "", "[[design_evidence]]", f"id = {json.dumps(entry['id'])}",
                f"command = {json.dumps(entry['command'])}", f"inputs = {json.dumps(list(entry['inputs']))}",
            ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# One arm
# --------------------------------------------------------------------------

@dataclasses.dataclass
class HarnessOptions:
    mode: str
    fixture_repo: Path
    fixture_revision: str
    fixture: dict
    task: str
    out_dir: Path
    max_attempts: int
    session_timeout: int
    no_packets: bool
    keep_workdir: bool
    stub_sleep: float
    which: object  # callable(name) -> path | None


def git(args: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode:
        raise BenchmarkError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def copy_fixture_repo(repo: Path, revision: str, destination: Path) -> str:
    """A fresh clone of the fixture repository checked out at `revision`,
    with any Handsoff state files that might have been tracked removed so
    `init` starts clean. Returns the resolved commit."""
    git(["clone", "--quiet", "--no-checkout", str(repo), str(destination)])
    git(["checkout", "--quiet", "--detach", revision], cwd=destination)
    head = git(["rev-parse", "HEAD"], cwd=destination).strip()
    for name in STATE_FILE_NAMES:
        path = destination / name
        if path.is_file():
            path.unlink()
    for name in (".handsoff-archive", ".handsoff-verify-inflight", ".handsoff-selfcheck"):
        shutil.rmtree(destination / name, ignore_errors=True)
    return head


def supervisor(root: Path, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SUPERVISOR), "--root", str(root), *args],
                          capture_output=True, text=True, timeout=timeout, check=False)


class ArmRun:
    """One repeat of one arm: init, seed, Phase 2, then Architect/Reviewer
    attempts until approval, the budget, or --max-attempts stops it."""

    def __init__(self, options: HarnessOptions, arm: str, repeat: int, workdir: Path, transcripts: Path):
        self.options = options
        self.arm = arm
        self.repeat = repeat
        self.settings = arm_settings(arm, no_packets=options.no_packets)
        self.root = workdir / f"{arm}-{repeat}"
        self.transcripts = transcripts
        self.commands: list[dict] = []
        self.sessions: list[dict] = []
        self.reviews: list[dict] = []
        self.authorized_attempt: int | None = None
        self.outcome = "incomplete"
        self.error: str | None = None
        self.fixture_head: str | None = None
        self.last_evidence_run: dict | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self._start_clock: float | None = None
        self._end_clock: float | None = None

    # -- helpers ---------------------------------------------------------

    def cfg(self) -> dict:
        return lib.load_config(self.root)

    def status(self) -> dict:
        return lib.load_unique_json(lib.status_path(self.root, self.cfg()))

    def run_supervisor(self, args: list[str], *, expect_ok: bool = True) -> subprocess.CompletedProcess:
        started = now_iso()
        clock = time.monotonic()
        proc = supervisor(self.root, args)
        self.commands.append({
            "command": ["handsoff_supervisor.py", "--root", "<arm-root>", *args],
            "started_at": started, "seconds": round(time.monotonic() - clock, 3),
            "exit_code": proc.returncode,
        })
        if expect_ok and proc.returncode:
            raise BenchmarkError(
                f"{self.arm}: handsoff_supervisor.py {args[0]} failed ({proc.returncode}): "
                f"{(proc.stdout + proc.stderr).strip()[-2000:]}"
            )
        return proc

    def design_evidence_state(self) -> dict | None:
        """Cache state as the product reports it: the ledger's count of
        executed measurements and the store's per-artifact states."""
        if not self.settings["design_evidence"]:
            return None
        cfg = self.cfg()
        events = lib.read_events(self.root, cfg)
        recorded = sum(1 for e in events if e.get("kind") == "design_evidence_recorded")
        view = lib.design_evidence_view(self.root, cfg)
        return {
            "configured": len(cfg.get("design_evidence") or []),
            "recorded_events": recorded,
            "states": {entry["id"]: entry["state"] for entry in view},
        }

    def run_design_evidence(self, attempt: int) -> dict | None:
        """#38: refresh the cached measurements before an Architect launch;
        the second attempt is expected to reuse every artifact."""
        if not self.settings["design_evidence"]:
            return None
        proc = self.run_supervisor(["design-evidence", "run", "--by", HARNESS_ACTOR], expect_ok=False)
        if proc.returncode:
            raise BenchmarkError(f"{self.arm}: design-evidence run failed: {(proc.stdout + proc.stderr).strip()[-2000:]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"{self.arm}: design-evidence run printed no JSON") from exc
        artifacts = payload.get("artifacts") or []
        return {
            "attempt": attempt,
            "executed": sum(1 for a in artifacts if not a.get("reused")),
            "reused": sum(1 for a in artifacts if a.get("reused")),
        }

    # -- sessions --------------------------------------------------------

    def launch(self, role: str, task: str, attempt: int, *, defects: list[str], findings: list[str]) -> dict:
        options = self.options
        env_before = {key: os.environ.get(key) for key in
                      (STUB_ENV_ROLE, STUB_ENV_ATTEMPT, STUB_ENV_DEFECTS, STUB_ENV_FINDINGS, STUB_ENV_SLEEP)}
        if options.mode == "stub":
            os.environ[STUB_ENV_ROLE] = role
            os.environ[STUB_ENV_ATTEMPT] = str(attempt)
            os.environ[STUB_ENV_DEFECTS] = ",".join(defects)
            os.environ[STUB_ENV_FINDINGS] = ",".join(findings)
            os.environ[STUB_ENV_SLEEP] = str(options.stub_sleep)
        evidence_before = self.design_evidence_state()
        record = {
            "role": role, "attempt": attempt, "adapter": None, "requested_model": None,
            "reported_model": None, "resolution_source": None, "tier": None, "tier_reason": None,
            "packet_id": None, "session_id": None, "actor": None, "state": None, "exit_code": None,
            "started_at": None, "ended_at": None, "seconds": None,
            "input_tokens": None, "output_tokens": None, "usage_source": "none",
            "design_evidence": evidence_before,
            "design_evidence_run": self.last_evidence_run if role == "architect" else None,
            "command": None, "cwd": "<arm-root>",
            "stdin_bytes": None, "stdout_bytes": None, "stdout_sha256": None, "transcript": None,
        }
        try:
            try:
                spec = agent.build_launch_spec(self.root, role, task, which=options.which)
            except lib.HandsoffError as exc:
                raise BenchmarkError(f"{self.arm}: {role} attempt {attempt} launch refused: {exc}") from exc
            spec = dataclasses.replace(spec, argv=structured_output_argv(spec))
            record.update({
                "adapter": spec.adapter, "requested_model": spec.model,
                "resolution_source": spec.resolution_source, "tier": spec.tier, "tier_reason": spec.tier_reason,
                "packet_id": spec.packet_id, "command": list(spec.argv),
                "stdin_bytes": len(spec.stdin.encode("utf-8")),
            })
            buffer = io.StringIO()
            record["started_at"] = now_iso()
            clock = time.monotonic()
            launch_error = None
            try:
                with contextlib.redirect_stdout(buffer):
                    agent.execute_launch(spec, timeout=options.session_timeout)
            except agent.AgentLaunchError as exc:
                launch_error = exc
            record["seconds"] = round(time.monotonic() - clock, 3)
            record["ended_at"] = now_iso()
            stdout = buffer.getvalue()
            record["stdout_bytes"] = len(stdout.encode("utf-8"))
            record["stdout_sha256"] = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
            transcript = self.transcripts / f"repeat-{self.repeat}-{role}-attempt-{attempt}.txt"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(stdout, encoding="utf-8")
            record["transcript"] = str(transcript.relative_to(options.out_dir))
            parsed = parse_runner_output(stdout)
            record.update({
                "input_tokens": parsed["input_tokens"], "output_tokens": parsed["output_tokens"],
                "reported_model": parsed["reported_model"], "usage_source": parsed["usage_source"],
            })
            session_id = launch_error.session_id if launch_error else None
            status = self.status()
            if session_id is None:
                session_id = (status.get("current_agent_sessions") or {}).get(role)
            session = (status.get("agent_sessions") or {}).get(session_id) if session_id else None
            if isinstance(session, dict):
                record.update({
                    "session_id": session_id, "actor": session.get("actor"), "state": session.get("state"),
                    "exit_code": session.get("exit_code"),
                    "tier": session.get("tier", record["tier"]),
                    "packet_id": session.get("packet_id", record["packet_id"]),
                })
            record["text"] = parsed["text"]
            if launch_error is not None:
                raise BenchmarkError(f"{self.arm}: {role} attempt {attempt} failed: {launch_error}")
            return record
        finally:
            self.sessions.append({k: v for k, v in record.items() if k != "text"})
            for key, value in env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # -- the run ---------------------------------------------------------

    def prepare(self) -> None:
        options = self.options
        log(f"{self.arm} repeat {self.repeat}: copying {options.fixture_repo} at {options.fixture_revision}")
        self.fixture_head = copy_fixture_repo(options.fixture_repo, options.fixture_revision, self.root)
        if not (self.root / "prompts" / "architect.md").is_file() or not (self.root / "prompts" / "reviewer.md").is_file():
            raise BenchmarkError(f"fixture revision has no prompts/architect.md and prompts/reviewer.md: {self.root}")
        (self.root / "handsoff.toml").write_text(render_arm_toml(options.fixture, self.arm, self.settings), encoding="utf-8")
        transaction = options.out_dir / self.arm / f"criteria-repeat-{self.repeat}.json"
        transaction.parent.mkdir(parents=True, exist_ok=True)
        transaction.write_text(json.dumps(options.fixture["criteria_transaction"], indent=2), encoding="utf-8")
        self.started_at = now_iso()
        self._start_clock = time.monotonic()
        self.run_supervisor(["init", options.fixture["feature"]])
        self.run_supervisor(["criteria-apply", "--file", str(transaction), "--by", HARNESS_ACTOR])
        self.run_supervisor(["advance", "2", "20"])

    def run(self) -> dict:
        options = self.options
        defects = options.fixture["seeded_defects"]
        defect_ids = [d["id"] for d in defects]
        try:
            self.prepare()
            architect_task = options.task
            last_findings: list[dict] = []
            architect_actor = None
            for attempt in range(1, options.max_attempts + 1):
                evidence = self.run_design_evidence(attempt)
                self.last_evidence_run = evidence
                if evidence:
                    log(f"{self.arm}: design evidence attempt {attempt}: executed {evidence['executed']}, reused {evidence['reused']}")
                if last_findings:
                    revision_task = architect_task + "\n\n# Prior review findings to address\n\n" + "\n".join(
                        f"{f['id']}: {f['text']}" for f in last_findings)
                else:
                    revision_task = architect_task
                architect = self.launch("architect", revision_task, attempt, defects=defect_ids,
                                        findings=[f["id"] for f in last_findings])
                architect_actor = architect["actor"]
                design_text = architect["text"]
                if last_findings and self.settings["packets"]:
                    resolved = set(RESOLVED_PATTERN.findall(design_text))
                    dispositions = []
                    for finding in last_findings:
                        state = "resolved" if finding["id"] in resolved else "unresolved"
                        dispositions += ["--disposition", f"{finding['id']}={state}"]
                    self.run_supervisor(["design-review-packet", "--by", HARNESS_ACTOR, *dispositions])
                budget = lib.design_review_budget(self.status(), self.cfg())
                if budget["exhausted"]:
                    if self.authorized_attempt is not None:
                        self.outcome = "budget_exhausted"
                        log(f"{self.arm}: budget exhausted again after one authorization; stopping")
                        break
                    self.run_supervisor(["design-review-authorize", "--by", PILOT_ACTOR,
                                         "--note", f"benchmark harness: one extra attempt for arm {self.arm}"])
                    self.authorized_attempt = budget["next_attempt"]
                    log(f"{self.arm}: budget exhausted at {budget['attempts']}/{budget['limit']}; authorized attempt {self.authorized_attempt}")
                reviewer_task = (options.task + "\n\n# Design under review (Architect attempt "
                                 f"{attempt})\n\n{design_text}")
                reviewer = self.launch("reviewer", reviewer_task, attempt, defects=defect_ids, findings=[])
                decision = review_decision(reviewer["text"])
                findings = extract_findings(reviewer["text"])
                found = defects_found(reviewer["text"], defects)
                if decision is None:
                    self.outcome = "no_decision"
                    self.reviews.append({"attempt": attempt, "decision": None, "findings": findings,
                                         "defects_found": found, "recorded": False})
                    log(f"{self.arm}: reviewer attempt {attempt} returned no decision token; stopping")
                    break
                args = ["record-design-review", "--by", reviewer["actor"], "--architect", architect_actor,
                        "--summary", f"Benchmark {self.arm} attempt {attempt}: {decision}",
                        "--approve" if decision == "approved" else "--request-changes"]
                for finding in findings:
                    args += ["--finding", finding]
                self.run_supervisor(args)
                recorded = self.status().get("design_review") or {}
                last_findings = list(recorded.get("findings") or [])
                self.reviews.append({
                    "attempt": attempt, "decision": decision, "findings": findings, "defects_found": found,
                    "recorded": True, "reviewer_profile": recorded.get("reviewer_profile"),
                    "design_review_attempt": recorded.get("attempt"),
                })
                log(f"{self.arm}: attempt {attempt} {decision}, defects found {found}")
                if decision == "approved":
                    self.outcome = "approved"
                    break
            else:
                self.outcome = "max_attempts"
        except BenchmarkError as exc:
            self.outcome = "error"
            self.error = str(exc)
            log(str(exc))
        finally:
            self._end_clock = time.monotonic()
            self.ended_at = now_iso()
        return self.record()

    def record(self) -> dict:
        wall = None
        if self._start_clock is not None and self._end_clock is not None:
            wall = round(self._end_clock - self._start_clock, 3)
        session_seconds = [s["seconds"] for s in self.sessions if isinstance(s.get("seconds"), (int, float))]
        inputs = [s["input_tokens"] for s in self.sessions]
        outputs = [s["output_tokens"] for s in self.sessions]
        complete = bool(self.sessions) and all(isinstance(v, int) for v in inputs) and all(isinstance(v, int) for v in outputs)
        tokens = (sum(inputs) + sum(outputs)) if complete else None
        found: list[str] = []
        for review in self.reviews:
            for defect in review["defects_found"]:
                if defect not in found:
                    found.append(defect)
        timing = None
        try:
            if self.root.is_dir() and lib.status_path(self.root, self.cfg()).is_file():
                timing = lib.summarize_design_timing(lib.read_events(self.root, self.cfg()))
        except Exception:
            timing = None
        return {
            "arm": self.arm, "repeat": self.repeat, "settings": self.settings, "outcome": self.outcome,
            "error": self.error, "fixture_head": self.fixture_head,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "wall_clock_seconds": wall, "session_seconds_total": round(sum(session_seconds), 3),
            "design_phase_tokens": tokens, "tokens_complete": complete,
            "input_tokens": sum(inputs) if complete else None,
            "output_tokens": sum(outputs) if complete else None,
            "attempts": len(self.reviews), "authorized_attempt": self.authorized_attempt,
            "defects_found": found, "sessions": self.sessions, "reviews": self.reviews,
            "commands": self.commands, "design_timing": timing,
        }


def structured_output_argv(spec: agent.LaunchSpec) -> tuple[str, ...]:
    """Ask the runner for its structured output so usage can be read from
    it: `--output-format json` for claude (verified in `claude --help`:
    "json (single result)"), `--json` for codex (verified in
    `codex exec --help`: "Print events to stdout as JSONL"). The codex
    prompt-from-stdin marker `-` stays last."""
    argv = list(spec.argv)
    if spec.adapter == "codex":
        if argv and argv[-1] == "-":
            argv.insert(len(argv) - 1, "--json")
        else:
            argv.append("--json")
    else:
        argv += ["--output-format", "json"]
    return tuple(argv)


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def _median(values: list) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return float(statistics.median(numbers)) if numbers else None


def _percent_delta(baseline, tranche) -> float | None:
    if baseline is None or tranche is None or baseline == 0:
        return None
    return round((tranche - baseline) / baseline * 100.0, 2)


def _union_found(repeats: list[dict]) -> list[str]:
    found: list[str] = []
    for repeat in repeats:
        for defect in repeat.get("defects_found", []):
            if defect not in found:
                found.append(defect)
    return found


def build_summary(mode: str, arms: dict, fixture: dict, options: dict) -> dict:
    """Medians over repeats, percent deltas (negative is a reduction),
    defect retention, and the thresholds. `thresholds_met` is null when the
    token comparison is impossible (any arm has null tokens or is missing)."""
    per_arm = {}
    for arm, repeats in arms.items():
        per_arm[arm] = {
            "repeats": len(repeats),
            "median_wall_clock_seconds": _median([r["wall_clock_seconds"] for r in repeats]),
            "median_session_seconds": _median([r["session_seconds_total"] for r in repeats]),
            "median_design_phase_tokens": (
                _median([r["design_phase_tokens"] for r in repeats])
                if repeats and all(r["tokens_complete"] for r in repeats) else None),
            "outcomes": [r["outcome"] for r in repeats],
            "attempts": [r["attempts"] for r in repeats],
            "authorized_attempts": [r["authorized_attempt"] for r in repeats],
            "defects_found": _union_found(repeats),
            "sessions": sum(len(r["sessions"]) for r in repeats),
        }
    baseline = per_arm.get("baseline")
    tranche = per_arm.get("tranche")
    wall_delta = tokens_delta = None
    retention = {"baseline_found": [], "tranche_found": [], "missing_in_tranche": [], "retained": None}
    if baseline and tranche:
        wall_delta = _percent_delta(baseline["median_wall_clock_seconds"], tranche["median_wall_clock_seconds"])
        tokens_delta = _percent_delta(baseline["median_design_phase_tokens"], tranche["median_design_phase_tokens"])
        missing = [d for d in baseline["defects_found"] if d not in tranche["defects_found"]]
        retention = {
            "baseline_found": baseline["defects_found"], "tranche_found": tranche["defects_found"],
            "missing_in_tranche": missing, "retained": not missing,
        }
    wall_met = None if wall_delta is None else bool(-wall_delta >= WALL_CLOCK_THRESHOLD_PERCENT)
    tokens_met = None if tokens_delta is None else bool(-tokens_delta >= TOKENS_THRESHOLD_PERCENT)
    if wall_met is None or tokens_met is None or retention["retained"] is None:
        thresholds_met = None
    else:
        thresholds_met = bool(wall_met and tokens_met and retention["retained"])
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "mode": mode,
        "measurement": mode == "live",
        "generated_at": now_iso(),
        "fixture": {
            "path": fixture.get("_path"), "repository": options["fixture_repo"],
            "revision": options["fixture_revision"], "feature": fixture["feature"],
            "seeded_defects": [d["id"] for d in fixture["seeded_defects"]],
            "max_autonomous_design_reviews": fixture["max_autonomous_design_reviews"],
        },
        "options": {k: v for k, v in options.items() if k not in ("fixture_repo", "fixture_revision")},
        "arms": per_arm,
        "deltas_percent": {"wall_clock": wall_delta, "tokens": tokens_delta},
        "thresholds": {
            "wall_clock_reduction_percent": WALL_CLOCK_THRESHOLD_PERCENT,
            "tokens_reduction_percent": TOKENS_THRESHOLD_PERCENT,
            "wall_clock_met": wall_met, "tokens_met": tokens_met,
        },
        "defect_retention": retention,
        "thresholds_met": thresholds_met,
        "commands": {
            "reproduce": [f"python3 tools/benchmark_design_phase.py --{mode} --fixture-revision "
                          f"{options['fixture_revision']} --out <benchmark dir>"],
            "harness": "tools/benchmark_design_phase.py",
        },
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def install_stub_adapters(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = STUB_ADAPTER_SOURCE.format(
        python=sys.executable, role_env=STUB_ENV_ROLE, attempt_env=STUB_ENV_ATTEMPT,
        defects_env=STUB_ENV_DEFECTS, findings_env=STUB_ENV_FINDINGS, sleep_env=STUB_ENV_SLEEP,
    )
    for name in lib.SELECTABLE_AGENT_ADAPTERS:
        path = directory / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    return directory


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the design phase: baseline versus the #33 to #40 tranche")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stub", action="store_true", help="deterministic stub adapters; not a measurement")
    mode.add_argument("--live", action="store_true",
                      help="real claude and codex sessions; a paid step that needs the Pilot's explicit yes")
    parser.add_argument("--fixture-repo", default=str(REPO_ROOT),
                        help="git repository to copy per arm (default: this checkout)")
    parser.add_argument("--fixture-revision", default="HEAD", help="git ref to check out in each copy (default HEAD)")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE),
                        help="fixture JSON: feature, criteria transaction, seeded defects, profiles, evidence")
    parser.add_argument("--task-file", default=None,
                        help="task text for the Architect and Reviewer (default: the fixture's task_file)")
    parser.add_argument("--out", default=None, help="output directory (default benchmark/<timestamp>/)")
    parser.add_argument("--arms", default=",".join(ARMS), help="comma-separated subset of baseline,tranche")
    parser.add_argument("--repeat", type=int, default=1, help="runs per arm; summary reports medians")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                        help="design-review attempts per run before the harness stops")
    parser.add_argument("--session-timeout", type=int, default=None,
                        help="seconds per managed session (default 60 with --stub, 3600 with --live)")
    parser.add_argument("--no-packets", action="store_true",
                        help="never call design-review-packet in any arm (the baseline already never does)")
    parser.add_argument("--adapter-path", action="append", default=[],
                        help="--live only: directory prepended to PATH so claude or codex can be found")
    parser.add_argument("--keep-workdir", action="store_true", help="leave the temporary repository copies in place")
    parser.add_argument("--stub-sleep", type=float, default=0.2, help="--stub only: seconds each stub session sleeps")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _main(args)
    except BenchmarkError as exc:
        print(f"BENCHMARK_BLOCKED: {exc}", file=sys.stderr)
        return 1


def _main(args: argparse.Namespace) -> int:
    mode = "stub" if args.stub else "live"
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if not arms or any(a not in ARMS for a in arms) or len(set(arms)) != len(arms):
        raise BenchmarkError(f"--arms must be a subset of {', '.join(ARMS)}")
    if args.repeat < 1 or args.max_attempts < 1:
        raise BenchmarkError("--repeat and --max-attempts must be positive")
    fixture_path = Path(args.fixture).resolve()
    fixture = load_fixture(fixture_path)
    task_path = Path(args.task_file) if args.task_file else fixture_path.parent / fixture.get("task_file", "task.md")
    if not task_path.is_file():
        raise BenchmarkError(f"task file not found: {task_path}")
    task = task_path.read_text(encoding="utf-8").strip()
    if not task:
        raise BenchmarkError(f"task file is empty: {task_path}")
    fixture_repo = Path(args.fixture_repo).resolve()
    if not (fixture_repo / ".git").exists():
        raise BenchmarkError(f"--fixture-repo is not a git repository: {fixture_repo}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out).resolve() if args.out else (REPO_ROOT / "benchmark" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Belt and braces: nothing here reaches Phase 8, but the archive writer
    # must never be able to land in the operator's Documents folder.
    os.environ.setdefault("HANDSOFF_ARCHIVE_DIR", str(out_dir / "archive"))
    session_timeout = args.session_timeout or (60 if mode == "stub" else 3600)
    workdir = Path(tempfile.mkdtemp(prefix="handsoff-benchmark-")).resolve()
    if mode == "stub":
        stub_dir = install_stub_adapters(workdir / "stub-bin")

        def which(name: str):
            candidate = stub_dir / name
            return str(candidate) if candidate.is_file() else None
    else:
        for directory in reversed(args.adapter_path):
            os.environ["PATH"] = f"{Path(directory).resolve()}{os.pathsep}{os.environ.get('PATH', '')}"
        for key in NESTED_CLAUDE_ENV:
            os.environ.pop(key, None)
        which = shutil.which
        missing = [name for name in lib.SELECTABLE_AGENT_ADAPTERS if not which(name)]
        if missing:
            raise BenchmarkError(f"--live needs these executables on PATH (use --adapter-path): {', '.join(missing)}")
    options = HarnessOptions(
        mode=mode, fixture_repo=fixture_repo, fixture_revision=args.fixture_revision, fixture=fixture,
        task=task, out_dir=out_dir, max_attempts=args.max_attempts, session_timeout=session_timeout,
        no_packets=args.no_packets, keep_workdir=args.keep_workdir, stub_sleep=args.stub_sleep, which=which,
    )
    log(f"mode {mode}; arms {', '.join(arms)}; out {out_dir}; workdir {workdir}")
    results: dict[str, list[dict]] = {}
    try:
        for arm in arms:
            repeats = []
            for repeat in range(1, args.repeat + 1):
                run = ArmRun(options, arm, repeat, workdir, out_dir / arm / "transcripts")
                repeats.append(run.run())
            results[arm] = repeats
            arm_record = {
                "arm": arm, "mode": mode, "settings": arm_settings(arm, no_packets=args.no_packets),
                "fixture_repo": str(fixture_repo), "fixture_revision": args.fixture_revision,
                "adapter_directory": str(stub_dir) if mode == "stub" else None,
                "repeats": repeats,
            }
            (out_dir / arm).mkdir(parents=True, exist_ok=True)
            (out_dir / arm / "run.json").write_text(json.dumps(arm_record, indent=2, sort_keys=True) + "\n",
                                                    encoding="utf-8")
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    summary = build_summary(mode, results, fixture, {
        "fixture_repo": str(fixture_repo), "fixture_revision": args.fixture_revision,
        "fixture": str(fixture_path), "task_file": str(task_path), "arms": arms, "repeat": args.repeat,
        "max_attempts": args.max_attempts, "session_timeout": session_timeout, "no_packets": args.no_packets,
        "stub_sleep": args.stub_sleep if mode == "stub" else None,
    })
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    failed = [arm for arm, repeats in results.items() if any(r["outcome"] == "error" for r in repeats)]
    if failed:
        log(f"arms with a harness error: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
