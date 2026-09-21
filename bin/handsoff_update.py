"""`handsoff update` (#219): one command brings the house to its latest
releases on this machine. Handsoff, the Miner and Sentinel are wheels on
GitHub releases installed into the engine's venv; Beakon is a checkout
whose launchd services are rendered from it. The command reads the latest
tag per tool, installs or checks out what is behind, restarts what runs
here (the loaded com.beakon.* labels, then the Fleet service), prints one
line per tool and verifies each. It never downgrades, never touches a
token (gh holds the login), and one tool's failure never stops the rest.
Every external call goes through one runner, so tests put fakes on PATH."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path


class UpdateError(Exception):
    pass


DEFAULT_VENV = "~/.local/share/handsoff/venv"
DEFAULT_BEAKON_CHECKOUT = "~/Projects/beakon"
FLEET_LABEL = "com.moncy.handsoff-dashboard"
FLEET_URL = "http://127.0.0.1:8765/api/fleet"
BEAKON_LABEL_PREFIX = "com.beakon."
CONFIG_PATH = "~/.handsoff/update.toml"
DEFAULT_TOOLS = {
    "handsoff": {"repo": "monzta1/project-handsoff", "kind": "wheel", "package": "project-handsoff"},
    "miner": {"repo": "monzta1/miner", "kind": "wheel", "package": "miner"},
    "sentinel": {"repo": "monzta1/sentinel", "kind": "wheel", "package": "sentinel"},
    "beakon": {"repo": "monzta1/beakon", "kind": "checkout"},
}
TOOL_ORDER = ("handsoff", "miner", "sentinel", "beakon")
CONFIG_KEYS = {"venv", "beakon_checkout", "tools", "gh", "git", "launchctl", "fleet_url"}
#: a release version: an optional leading v, dot-separated integers, nothing
#: else. 1.2.0rc1, 1.2.0-beta or 1.2 (fewer than three parts is fine) are
#: read; a prerelease or a build suffix is refused as unsupported, never
#: guessed, and the tool is skipped with the reason.
VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def load_config(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("HANDSOFF_UPDATE_CONFIG") or CONFIG_PATH).expanduser()
    raw = {}
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise UpdateError(f"{path}: {exc}") from exc
    table = raw.get("update", raw) if isinstance(raw, dict) else {}
    if not isinstance(table, dict):
        raise UpdateError(f"{path}: [update] must be a table")
    unknown = set(table) - CONFIG_KEYS
    if unknown:
        raise UpdateError(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    cfg = {"venv": DEFAULT_VENV, "beakon_checkout": DEFAULT_BEAKON_CHECKOUT, "gh": "gh", "git": "git",
           "launchctl": "launchctl", "fleet_url": FLEET_URL, "tools": {name: dict(spec) for name, spec in DEFAULT_TOOLS.items()}}
    for key in ("venv", "beakon_checkout", "gh", "git", "launchctl", "fleet_url"):
        value = table.get(key, cfg[key])
        if not isinstance(value, str) or not value.strip():
            raise UpdateError(f"{path}: {key} must be a non-empty string")
        cfg[key] = value.strip()
    tools = table.get("tools", {})
    if not isinstance(tools, dict):
        raise UpdateError(f"{path}: tools must be a table")
    for name, spec in tools.items():
        if not isinstance(spec, dict) or spec.get("kind") not in ("wheel", "checkout") \
                or not isinstance(spec.get("repo"), str) or spec["repo"].count("/") != 1:
            raise UpdateError(f"{path}: tools.{name} needs repo = owner/name and kind = wheel or checkout")
        if spec["kind"] == "wheel" and not isinstance(spec.get("package"), str):
            raise UpdateError(f"{path}: tools.{name} (wheel) needs package")
        cfg["tools"][name] = dict(spec)
    cfg["venv"] = str(Path(cfg["venv"]).expanduser())
    cfg["beakon_checkout"] = str(Path(cfg["beakon_checkout"]).expanduser())
    return cfg


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    """(1, 2, 3) for 'v1.2.3' or '1.2.3'; None for anything else (a
    prerelease, a build suffix, a date, an empty string). Comparison is
    the tuple's: 0.3.10 > 0.3.9, and 1.2 == 1.2.0."""
    match = VERSION.fullmatch((text or "").strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _compare(result: dict, before: str | None, latest: str) -> str | None:
    """already, left_newer, or None when an update is due; a version that
    does not parse skips the tool with the reason."""
    want = version_tuple(latest)
    if want is None:
        result.update(action="skipped", after=before, reason=f"release tag {latest!r} is not a plain version (prerelease or build suffix)")
        return "skipped"
    have = version_tuple(before)
    if before is not None and have is None:
        result.update(action="skipped", after=before, reason=f"installed version {before!r} is not a plain version")
        return "skipped"
    if have is not None and have == want:
        result.update(action="already", after=before)
        return "already"
    if have is not None and have > want:
        result.update(action="left_newer", after=before, reason=f"installed {before} is newer than the release {latest}")
        return "left_newer"
    return None


class Runner:
    """Every external call. `dry_run` answers the writing calls without
    making them; `log` records every argv for tests and the printed trail."""

    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: str | None = None, timeout: int = 900, write: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if write and self.dry_run:
            return subprocess.CompletedProcess(argv, 0, "", "")
        try:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        except OSError as exc:
            raise UpdateError(f"{argv[0]} could not be started: {exc.strerror or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"{argv[0]} did not finish within {timeout} seconds") from exc


def _last(completed: subprocess.CompletedProcess) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return text.splitlines()[-1][:200] if text else "no output"


def gh_logged_in(cfg: dict, runner: Runner) -> bool:
    try:
        return runner.run([cfg["gh"], "auth", "status"], timeout=60).returncode == 0
    except UpdateError:
        return False


def latest_release(cfg: dict, runner: Runner, repo: str) -> str | None:
    completed = runner.run([cfg["gh"], "release", "list", "--repo", repo, "--limit", "1", "--json", "tagName"], timeout=120)
    if completed.returncode != 0:
        raise UpdateError(f"gh release list {repo}: {_last(completed)}")
    try:
        rows = json.loads(completed.stdout or "[]")
    except ValueError as exc:
        raise UpdateError(f"gh release list {repo} did not answer with JSON") from exc
    return str(rows[0]["tagName"]) if rows and isinstance(rows[0], dict) and rows[0].get("tagName") else None


def installed_version(cfg: dict, runner: Runner, package: str) -> str | None:
    pip = str(Path(cfg["venv"]) / "bin" / "pip")
    completed = runner.run([pip, "show", package], timeout=120)
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def update_wheel(cfg: dict, runner: Runner, name: str, spec: dict, *, logged_in: bool) -> dict:
    result = {"tool": name, "kind": "wheel", "before": None, "after": None, "action": None, "reason": ""}
    result["before"] = installed_version(cfg, runner, spec["package"])
    if not logged_in:
        result.update(action="skipped", reason="no gh login", after=result["before"])
        return result
    latest = latest_release(cfg, runner, spec["repo"])
    if latest is None:
        result.update(action="skipped", reason="no release", after=result["before"])
        return result
    result["latest"] = latest
    if _compare(result, result["before"], latest):
        return result
    want = version_tuple(latest)
    with tempfile.TemporaryDirectory(prefix=f"handsoff-update-{name}-") as tmp:
        downloaded = runner.run([cfg["gh"], "release", "download", latest, "--repo", spec["repo"],
                                 "--pattern", "*.whl", "--dir", tmp, "--clobber"], timeout=600, write=True)
        if downloaded.returncode != 0:
            raise UpdateError(f"download of {latest} failed: {_last(downloaded)}")
        wheels = sorted(Path(tmp).glob("*.whl"))
        wheel = str(wheels[0]) if wheels else str(Path(tmp) / "<wheel>")
        if not wheels and not runner.dry_run:
            raise UpdateError(f"release {latest} of {spec['repo']} carries no wheel")
        pip = str(Path(cfg["venv"]) / "bin" / "pip")
        installed = runner.run([pip, "install", "--force-reinstall", "--no-deps", wheel], timeout=900, write=True)
        if installed.returncode != 0:
            raise UpdateError(f"pip install failed: {_last(installed)}")
    result["after"] = latest if runner.dry_run else installed_version(cfg, runner, spec["package"])
    if not runner.dry_run and version_tuple(result["after"]) != want:
        raise UpdateError(f"installed {result['after']} after installing {latest}")
    result["action"] = "updated"
    return result


def loaded_labels(cfg: dict, runner: Runner, prefix: str) -> list[str]:
    completed = runner.run([cfg["launchctl"], "list"], timeout=60)
    labels = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[-1].startswith(prefix):
            labels.append(parts[-1])
    return sorted(labels)


def kickstart(cfg: dict, runner: Runner, label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    completed = runner.run([cfg["launchctl"], "kickstart", "-k", target], timeout=60, write=True)
    if completed.returncode != 0:
        raise UpdateError(f"launchctl kickstart {label}: {_last(completed)}")


def update_checkout(cfg: dict, runner: Runner, name: str, spec: dict, *, logged_in: bool) -> dict:
    result = {"tool": name, "kind": "checkout", "before": None, "after": None, "action": None, "reason": ""}
    path = Path(cfg["beakon_checkout"] if name == "beakon" else spec.get("path", "")).expanduser()
    if not (path / ".git").exists():
        result.update(action="skipped", reason=f"no checkout at {path}")
        return result
    git = cfg["git"]
    fetched = runner.run([git, "-C", str(path), "fetch", "--tags", "--quiet", "origin"], timeout=300, write=True)
    if fetched.returncode != 0:
        raise UpdateError(f"git fetch: {_last(fetched)}")
    described = runner.run([git, "-C", str(path), "describe", "--tags", "--exact-match", "HEAD"], timeout=60)
    result["before"] = (described.stdout or "").strip() or None
    # never over local work: a dirty tree is a failure named here, before
    # any comparison, so a checkout with local changes never reads already
    dirty = runner.run([git, "-C", str(path), "status", "--porcelain"], timeout=60)
    if (dirty.stdout or "").strip():
        raise UpdateError(f"checkout at {path} has local changes; commit or stash them first")
    latest = None
    if logged_in:
        latest = latest_release(cfg, runner, spec["repo"])
    if latest is None:
        tags = runner.run([git, "-C", str(path), "tag", "--sort=-v:refname"], timeout=60)
        latest = next((t.strip() for t in (tags.stdout or "").splitlines() if version_tuple(t)), None)
    if latest is None:
        result.update(action="skipped", reason="no release", after=result["before"])
        return result
    result["latest"] = latest
    if _compare(result, result["before"], latest):
        return result
    # the tag must exist in the checkout after the fetch: a release whose
    # tag was never pushed is a failure named here, not a checkout of nothing
    known = runner.run([git, "-C", str(path), "rev-parse", "--verify", "--quiet", f"refs/tags/{latest}"], timeout=60)
    if known.returncode != 0:
        raise UpdateError(f"release {latest} has no tag in the checkout after fetch")
    # never a downgrade: a checkout whose HEAD already contains the release
    # (a development checkout ahead of the tag) is left where it is
    ahead = runner.run([git, "-C", str(path), "merge-base", "--is-ancestor", f"refs/tags/{latest}", "HEAD"], timeout=60)
    if ahead.returncode == 0 and result["before"] is None:
        head = (runner.run([git, "-C", str(path), "rev-parse", "--short", "HEAD"], timeout=60).stdout or "").strip()
        result.update(action="left_newer", after=head or "HEAD", reason=f"checkout is ahead of the release {latest}")
        return result
    labels = loaded_labels(cfg, runner, BEAKON_LABEL_PREFIX) if name == "beakon" else []
    if runner.dry_run:
        result.update(action="updated", after=latest, restarted=labels)
        return result
    checked = runner.run([git, "-C", str(path), "checkout", "--quiet", latest], timeout=120, write=True)
    if checked.returncode != 0:
        raise UpdateError(f"git checkout {latest}: {_last(checked)}")
    for label in labels:
        kickstart(cfg, runner, label)
    result.update(action="updated", after=latest, restarted=labels)
    return result


def restart_fleet(cfg: dict, runner: Runner, *, wait_seconds: float = 30.0) -> dict:
    """Kickstart the Fleet service and wait for the page to answer with
    the engine version. Skipped when the label is not loaded here."""
    if FLEET_LABEL not in loaded_labels(cfg, runner, FLEET_LABEL):
        return {"tool": "fleet", "action": "skipped", "reason": "Fleet service not loaded on this machine"}
    if runner.dry_run:
        return {"tool": "fleet", "action": "would restart", "reason": f"kickstart {FLEET_LABEL}, then poll {cfg['fleet_url']}"}
    kickstart(cfg, runner, FLEET_LABEL)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(cfg["fleet_url"], timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            version = ((payload.get("engine") or {}).get("version"))
            if version:
                return {"tool": "fleet", "action": "restarted", "engine": version}
        except Exception:  # noqa: BLE001 - a service coming up answers with anything at first
            pass
        time.sleep(1)
    return {"tool": "fleet", "action": "failed", "reason": f"Fleet did not answer at {cfg['fleet_url']} within {int(wait_seconds)} s"}


def verify(cfg: dict, runner: Runner, name: str) -> str:
    venv = Path(cfg["venv"]) / "bin"
    argv = {"handsoff": [str(venv / "handsoff"), "version"], "miner": [str(venv / "miner"), "--help"],
            "sentinel": [str(venv / "sentinel"), "--version"],
            "beakon": [sys.executable, str(Path(cfg["beakon_checkout"]) / "client" / "beakon.py"), "--version"]}.get(name)
    if argv is None:
        return "no verify step"
    try:
        completed = runner.run(argv, timeout=120)
    except UpdateError as exc:
        return f"failed: {exc}"
    first = ((completed.stdout or "").strip() or (completed.stderr or "").strip()).splitlines()
    return (first[0][:120] if first else "no output") if completed.returncode == 0 else f"exit {completed.returncode}: {_last(completed)}"


def line_for(result: dict) -> str:
    tool, action = result["tool"], result.get("action")
    if action == "updated":
        extra = f" (restarted {', '.join(result['restarted'])})" if result.get("restarted") else ""
        return f"{tool} {result.get('before') or 'absent'} -> {result.get('after')}{extra}"
    if action == "would update":
        extra = f" and restart {', '.join(result['restarted'])}" if result.get("restarted") else ""
        return f"{tool} would update {result.get('before') or 'absent'} -> {result.get('latest')}{extra}"
    if action == "would restart":
        return f"{tool} would restart: {result.get('reason')}"
    if action == "already":
        return f"{tool} already {result.get('after')}"
    if action == "left_newer":
        return f"{tool} left at {result.get('after')}: {result.get('reason')}"
    if action == "skipped":
        return f"{tool} skipped: {result.get('reason')}"
    if action == "failed":
        return f"{tool} failed: {result.get('reason')}"
    return f"{tool} {action}" + (f": {result.get('reason')}" if result.get("reason") else "")


def install_check(*, force: bool = False, by: str | None = None, note: str | None = None,
                  registry: Path | None = None, out=print) -> dict:
    """#216: refuse to replace the shared engine while any registered run
    has a live managed session. Prints one 'blocked: <root> <role>
    <session_id>' line per live session. Returns {blocked: [...], forced:
    bool, exit_code}: 1 when blocked and not forced; 2 when --force lacks
    --by; 0 otherwise. --force records engine_install_forced on each
    affected run, once per root under that root's lock."""
    import handsoff_fleet as fleet
    sessions = fleet.live_managed_sessions(registry)
    for session in sessions:
        out(f"blocked: {session['root']} {session['role']} {session['session_id']}")
    if not sessions:
        out("INSTALL_CHECK_OK")
        return {"blocked": [], "forced": False, "exit_code": 0}
    if not force:
        out(f"INSTALL_CHECK_BLOCKED: {len(sessions)} live managed session(s); finish or cancel them, or --force --by <you>")
        return {"blocked": sessions, "forced": False, "exit_code": 1}
    if not by or not str(by).strip():
        out("INSTALL_CHECK_REFUSED: --force needs --by <actor>; the override is ledgered on each affected run")
        return {"blocked": sessions, "forced": False, "exit_code": 2}
    import handsoff_lib as lib
    by_root: dict[str, list[dict]] = {}
    for session in sessions:
        by_root.setdefault(session["root"], []).append(session)
    for root, group in by_root.items():
        root_path = Path(root)
        with lib.project_lock(root_path):
            cfg = lib.load_config(root_path)
            lib.commit(root_path, cfg, event_kind="engine_install_forced",
                       event_message=f"Engine install forced over {len(group)} live managed session(s) by {by}",
                       by=str(by).strip(), note=(note or "")[:512],
                       sessions=[{"role": s["role"], "session_id": s["session_id"]} for s in group])
    out("INSTALL_CHECK_FORCED")
    return {"blocked": sessions, "forced": True, "exit_code": 0}


def update(cfg: dict, *, only: list[str] | None = None, dry_run: bool = False, out=print,
           runner: Runner | None = None, fleet_wait: float = 30.0, force: bool = False,
           by: str | None = None, note: str | None = None, registry: Path | None = None) -> dict:
    """The whole run. Returns {results, fleet, verify, failed, exit_code}."""
    runner = runner or Runner(dry_run=dry_run)
    names = [n for n in TOOL_ORDER if n in cfg["tools"]] + sorted(n for n in cfg["tools"] if n not in TOOL_ORDER)
    if only:
        unknown = [n for n in only if n not in cfg["tools"]]
        if unknown:
            raise UpdateError(f"unknown tool(s): {', '.join(unknown)}; known: {', '.join(names)}")
        names = [n for n in names if n in only]
    logged_in = gh_logged_in(cfg, runner)
    # #216: never replace the shared engine under a live managed session
    if any(cfg["tools"][n]["kind"] == "wheel" for n in names):
        check = install_check(force=force and not dry_run, by=by, note=note, registry=registry, out=out)
        if check["exit_code"] and not (dry_run and check["exit_code"] == 1):
            out("UPDATE_FAILED: install blocked")
            return {"results": [], "fleet": None, "verify": {}, "failed": ["install-check"], "exit_code": 1}
        if dry_run and check["blocked"]:
            out("dry run: the install would be blocked; nothing else is checked")
            return {"results": [], "fleet": None, "verify": {}, "failed": ["install-check"], "exit_code": 1}
    results = []
    for name in names:
        spec = cfg["tools"][name]
        try:
            result = (update_wheel if spec["kind"] == "wheel" else update_checkout)(cfg, runner, name, spec, logged_in=logged_in)
            if dry_run and result["action"] == "updated":
                result["action"] = "would update"
        except UpdateError as exc:
            result = {"tool": name, "kind": spec["kind"], "action": "failed", "reason": str(exc)}
        results.append(result)
        out(line_for(result))
    fleet = None
    if not only or "handsoff" in (only or []):
        try:
            fleet = restart_fleet(cfg, runner, wait_seconds=fleet_wait)
        except UpdateError as exc:
            fleet = {"tool": "fleet", "action": "failed", "reason": str(exc)}
        out(line_for(fleet) if fleet["action"] != "restarted" else f"fleet restarted, engine {fleet['engine']}")
    verified = {}
    if not dry_run:
        for name in names:
            if next((r for r in results if r["tool"] == name), {}).get("action") in ("updated", "already", "left_newer"):
                verified[name] = verify(cfg, runner, name)
                out(f"verify: {name} {verified[name]}")
    failed = [r["tool"] for r in results if r["action"] == "failed"] + ([fleet["tool"]] if fleet and fleet["action"] == "failed" else [])
    if failed:
        out("UPDATE_FAILED: " + ", ".join(failed))
    else:
        out("UPDATE_OK" + (" (dry run)" if dry_run else ""))
    return {"results": results, "fleet": fleet, "verify": verified, "failed": failed, "exit_code": 1 if failed else 0}
