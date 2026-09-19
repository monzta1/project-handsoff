#!/usr/bin/env python3
"""Cached GitHub and Beakon signals for Fleet project rows (#152).

Fleet's snapshot is rebuilt once a second for the event stream, so nothing
here runs on that path: a SignalCache is filled by a background refresh and
`build_fleet` only reads it. Two signals per registered project:

- `github`: open issues, open pull requests and the latest release of the
  project's `origin` remote, read through `gh api` when gh is authenticated,
  else GITHUB_TOKEN over HTTPS. Without either the signal carries the error
  "GitHub is not configured" and null counts: unauthenticated access is
  refused on purpose (60 requests an hour would be gone in minutes).
- `beakon`: beams for the project in the local Beakon worker's landing
  folder. One `bk-*` folder is one beam; `task.md` frontmatter `workdir`
  attributes it, no `result.json` means in flight, and the newest finished
  beam is `last`. No worker on this machine means the signal is None.

Failure semantics are per project and atomic: a refresh that fails for one
project keeps that project's previous good values, sets `error`, and leaves
`fetched_at` at the last success. The cache persists to
`~/.handsoff/fleet-signals.json` so a restarted Fleet serves the last values
(with their original `fetched_at`) until the first refresh lands.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handsoff_lib as lib  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

GITHUB_NOT_CONFIGURED = "GitHub is not configured"
NO_ORIGIN = "no GitHub origin remote"
DEFAULT_INTERVAL_SECONDS = 300.0
GITHUB_API = "https://api.github.com"
_REMOTE_FORMS = (
    re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
)
_FRONTMATTER_WORKDIR = re.compile(r"^\s*workdir\s*:\s*(?P<value>.+?)\s*$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def signals_path(registry: Path | None = None) -> Path:
    """HANDSOFF_FLEET_SIGNALS_FILE, else fleet-signals.json beside the Fleet
    registry (~/.handsoff/projects.json by default), so a server on a test
    registry never writes the real cache."""
    override = os.environ.get("HANDSOFF_FLEET_SIGNALS_FILE")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(registry).expanduser().resolve() if registry else Path.home() / ".handsoff" / "projects.json"
    return base.with_name("fleet-signals.json")


def signals_interval() -> float:
    raw = os.environ.get("HANDSOFF_FLEET_SIGNALS_INTERVAL")
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        value = DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


# --- GitHub -----------------------------------------------------------------

def parse_github_remote(url: str) -> str | None:
    """owner/repo for the https and ssh remote forms GitHub hands out; None otherwise."""
    for form in _REMOTE_FORMS:
        match = form.match(url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def origin_repo(root: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=root,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_github_remote(proc.stdout) if proc.returncode == 0 else None


class GitHubUnavailable(Exception):
    """A GitHub read failed; the caller keeps its last good values."""


def _gh_client():
    """A fetch(path) over the gh CLI, or None when gh is absent or logged out."""
    executable = shutil.which("gh")
    if not executable:
        return None
    try:
        probe = subprocess.run([executable, "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0 or not probe.stdout.strip():
        return None

    def fetch(path: str):
        try:
            proc = subprocess.run([executable, "api", path], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubUnavailable(f"gh api failed: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            if "HTTP 404" in proc.stderr:
                return None
            raise GitHubUnavailable((proc.stderr.strip().splitlines() or ["gh api failed"])[-1][:200])
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise GitHubUnavailable("gh api returned invalid JSON") from exc
    return fetch


def _token_client(token: str):
    def fetch(path: str):
        request = urllib.request.Request(f"{GITHUB_API}/{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "User-Agent": "handsoff-fleet",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise GitHubUnavailable(f"GitHub HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise GitHubUnavailable(f"GitHub unreachable: {type(exc).__name__}") from exc
    return fetch


def github_client():
    """The configured reader: gh when authenticated, else GITHUB_TOKEN, else None."""
    client = _gh_client()
    if client is not None:
        return client
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return _token_client(token) if token else None


def _count(payload) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("total_count"), int):
        raise GitHubUnavailable("GitHub search answer had no total_count")
    return payload["total_count"]


_ASK = object()


def fetch_github(root: Path, client=_ASK) -> dict:
    """One project's GitHub signal. `client` is fetch(path) -> parsed JSON or
    None for 404, or None when GitHub is not configured; left out, it is
    `github_client()`. Raises GitHubUnavailable on a failed read so the
    cache can keep the previous good values."""
    repo = origin_repo(root)
    if repo is None:
        return {"repo": None, "open_issues": None, "open_prs": None, "latest_release": None,
                "fetched_at": utcnow(), "error": NO_ORIGIN}
    fetch = github_client() if client is _ASK else client
    if fetch is None:
        return {"repo": repo, "open_issues": None, "open_prs": None, "latest_release": None,
                "fetched_at": utcnow(), "error": GITHUB_NOT_CONFIGURED}
    issues = _count(fetch(f"search/issues?q=repo:{repo}+is:issue+is:open&per_page=1"))
    prs = _count(fetch(f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1"))
    release = fetch(f"repos/{repo}/releases/latest")
    latest = None
    if isinstance(release, dict) and release.get("tag_name"):
        latest = {"tag": release["tag_name"], "published_at": release.get("published_at")}
    return {"repo": repo, "open_issues": issues, "open_prs": prs, "latest_release": latest,
            "fetched_at": utcnow(), "error": None}


# --- Beakon -----------------------------------------------------------------

def beakon_work_root() -> Path | None:
    """The local worker's landing folder: BEAKON_WORK_ROOT, else `work_root`
    in ~/.config/beakon/worker.toml; None when neither names one."""
    if "BEAKON_WORK_ROOT" in os.environ:
        override = os.environ["BEAKON_WORK_ROOT"].strip()
        return Path(override).expanduser().resolve() if override else None
    config = Path.home() / ".config" / "beakon" / "worker.toml"
    if tomllib is None:
        return None
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8")).get("work_root")
    except (OSError, ValueError):
        return None
    return Path(value).expanduser().resolve() if isinstance(value, str) and value.strip() else None


def _frontmatter_workdir(task: Path) -> Path | None:
    try:
        lines = task.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FRONTMATTER_WORKDIR.match(line)
        if match:
            value = match.group("value").strip().strip("'\"")
            return Path(value).expanduser().resolve() if value else None
    return None


def scan_beakon(work_root: Path) -> dict:
    """Every beam in the landing folder grouped by its resolved workdir:
    {"fetched_at", "beams": {workdir: {"in_flight": n, "finished": [...]}}}.
    A beam without a parsable workdir is skipped; nothing here raises."""
    beams: dict[str, dict] = {}
    try:
        folders = sorted(path for path in work_root.iterdir() if path.is_dir() and path.name.startswith("bk-"))
    except OSError:
        folders = []
    for folder in folders:
        task = folder / "task.md"
        if not task.is_file():
            continue
        workdir = _frontmatter_workdir(task)
        if workdir is None:
            continue
        entry = beams.setdefault(str(workdir), {"in_flight": 0, "finished": []})
        result = folder / "result.json"
        if not result.exists():
            entry["in_flight"] += 1
            continue
        outcome, finished_at = "unknown", None
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("status") in {"done", "failed"}:
                outcome = payload["status"]
            if isinstance(payload.get("finished_at"), str):
                finished_at = payload["finished_at"]
        entry["finished"].append({"receipt": folder.name, "outcome": outcome, "finished_at": finished_at})
    for entry in beams.values():
        entry["finished"].sort(key=lambda item: (item["finished_at"] or "", item["receipt"]))
    return {"fetched_at": utcnow(), "beams": beams}


def beakon_signal(root: Path, scan: dict | None) -> dict | None:
    """One project's slice of a scan; None when there is no worker (scan is None)."""
    if scan is None:
        return None
    entry = (scan.get("beams") or {}).get(str(Path(root).resolve()))
    finished = entry["finished"] if entry else []
    return {"in_flight": entry["in_flight"] if entry else 0,
            "last": finished[-1] if finished else None,
            "fetched_at": scan.get("fetched_at")}


# --- The cache ---------------------------------------------------------------

EMPTY = {"github": None, "beakon": None}


class SignalCache:
    """The one per-server store Fleet reads from. Loaded once; every refresh
    swaps a whole new dict under the lock and persists it atomically."""

    def __init__(self, path: Path | None = None, *, registry: Path | None = None):
        self.path = path or signals_path(registry)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self.loaded_at: str | None = None
        self.refreshed_at: str | None = None
        self.load()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"schema": 1, "projects": {}}
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet signals cache {self.path} is unreadable "
                             f"({type(exc).__name__}); starting empty\n")
            payload = {"schema": 1, "projects": {}}
        projects = payload.get("projects") if isinstance(payload, dict) and payload.get("schema") == 1 else None
        if not isinstance(projects, dict):
            if payload != {"schema": 1, "projects": {}}:
                sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet signals cache {self.path} is malformed; starting empty\n")
            projects = {}
        clean = {}
        for root, value in projects.items():
            if isinstance(root, str) and isinstance(value, dict):
                clean[root] = {"github": value.get("github") if isinstance(value.get("github"), dict) else None,
                               "beakon": value.get("beakon") if isinstance(value.get("beakon"), dict) else None}
        with self._lock:
            self._data = clean
            self.loaded_at = utcnow()

    def get(self, root: Path | str) -> dict:
        with self._lock:
            found = self._data.get(str(Path(root).resolve()))
        return dict(found) if found else dict(EMPTY)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {root: dict(value) for root, value in self._data.items()}

    def refresh(self, roots, *, github_fetch=None, beakon_scan=None,
                work_root: Path | None = None, client=_ASK) -> dict[str, dict]:
        """Recompute every registered project's signals and swap them in.
        `github_fetch(root, client)` and `beakon_scan(work_root)` are the
        seams tests inject (the module functions when left out, looked up
        at call time); `work_root` None means "ask beakon_work_root()"."""
        github_fetch = github_fetch or fetch_github
        beakon_scan = beakon_scan or scan_beakon
        roots = [Path(root).resolve() for root in roots]
        previous = self.snapshot()
        if work_root is None:
            work_root = beakon_work_root()
        scan = beakon_scan(work_root) if work_root is not None else None
        if client is _ASK:
            client = github_client()  # resolved once per refresh, not per project
        fresh: dict[str, dict] = {}
        for root in roots:
            key = str(root)
            old = previous.get(key) or dict(EMPTY)
            try:
                github = github_fetch(root, client)
            except GitHubUnavailable as exc:
                github = dict(old["github"]) if old.get("github") else {
                    "repo": None, "open_issues": None, "open_prs": None, "latest_release": None, "fetched_at": None,
                }
                github["error"] = str(exc)
            fresh[key] = {"github": github, "beakon": beakon_signal(root, scan)}
        with self._lock:
            self._data = fresh
            self.refreshed_at = utcnow()
        self._persist(fresh)
        return fresh

    def _persist(self, data: dict[str, dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lib.atomic_write_json(self.path, {"schema": 1, "projects": data})
        except OSError as exc:
            sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet signals cache {self.path} was not written "
                             f"({type(exc).__name__})\n")


def start_refresh_thread(cache, roots_provider, *, interval: float | None = None,
                         stop_event: threading.Event | None = None, name: str = "fleet-signals") -> threading.Thread:
    """Refresh now, then every `interval` seconds, on a daemon thread. The
    caller's constructor returns before the first refresh completes. Works
    for any cache with refresh(roots): SignalCache and IssueCache (#153)."""
    interval = signals_interval() if interval is None else interval
    stop_event = stop_event or threading.Event()

    def loop():
        while not stop_event.is_set():
            try:
                cache.refresh(roots_provider())
            except Exception as exc:  # the loop must outlive any one bad cycle
                sys.stderr.write(f"HANDSOFF_FLEET_WARNING: {name} refresh failed ({type(exc).__name__}: {exc})\n")
            stop_event.wait(interval)

    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread


# --- Issues (#153): the Metrics tab's data ----------------------------------

# #158: a no-change pass is three 304s, free of rate limit, so a minute is the
# default and half a minute the floor; a full re-page once a day catches
# deletions and transfers the incremental read cannot see.
DEFAULT_ISSUES_INTERVAL_SECONDS = 60.0
MIN_ISSUES_INTERVAL_SECONDS = 30.0
DEFAULT_FULL_PASS_HOURS = 24.0
DEFAULT_COMMITS_DAYS = 180
ISSUE_FIELDS = ("number", "title", "html_url", "created_at", "closed_at", "updated_at")
PAGE_SIZE = 100


def issues_path(registry: Path | None = None) -> Path:
    """HANDSOFF_FLEET_ISSUES_FILE, else fleet-issues.json beside the registry."""
    override = os.environ.get("HANDSOFF_FLEET_ISSUES_FILE")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(registry).expanduser().resolve() if registry else Path.home() / ".handsoff" / "projects.json"
    return base.with_name("fleet-issues.json")


def issues_interval() -> float:
    raw = os.environ.get("HANDSOFF_FLEET_ISSUES_INTERVAL")
    try:
        value = float(raw) if raw else DEFAULT_ISSUES_INTERVAL_SECONDS
    except ValueError:
        value = DEFAULT_ISSUES_INTERVAL_SECONDS
    if value <= 0:
        value = DEFAULT_ISSUES_INTERVAL_SECONDS
    return max(value, MIN_ISSUES_INTERVAL_SECONDS)


def full_pass_hours() -> float:
    raw = os.environ.get("HANDSOFF_FLEET_FULL_PASS_HOURS")
    try:
        value = float(raw) if raw else DEFAULT_FULL_PASS_HOURS
    except ValueError:
        value = DEFAULT_FULL_PASS_HOURS
    return value if value > 0 else DEFAULT_FULL_PASS_HOURS


def commits_days() -> int:
    raw = os.environ.get("HANDSOFF_FLEET_COMMITS_DAYS")
    try:
        value = int(raw) if raw else DEFAULT_COMMITS_DAYS
    except ValueError:
        value = DEFAULT_COMMITS_DAYS
    return value if value > 0 else DEFAULT_COMMITS_DAYS


def commits_since(now: datetime | None = None, days: int | None = None) -> str:
    """#156: the inclusive lower bound of the commit window, ISO seconds Z."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=commits_days() if days is None else days)
    return since.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paged(client, base: str) -> list:
    """Every item of a paged GitHub list endpoint; raises on any failed page."""
    items: list = []
    page = 1
    while True:
        joiner = "&" if "?" in base else "?"
        payload = client(f"{base}{joiner}per_page={PAGE_SIZE}&page={page}")
        if payload is None:
            raise GitHubUnavailable(f"GitHub answered 404 for {base.split('?')[0]}")
        if not isinstance(payload, list):
            raise GitHubUnavailable("GitHub list answer was not a list")
        items.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < PAGE_SIZE:
            return items
        page += 1


def fetch_commits(repo: str, client, since: str) -> list[dict]:
    """#156: commits on the default branch dated at or after `since`
    (GitHub's since is inclusive): sha, committer date, first message line."""
    commits = []
    for item in _paged(client, f"repos/{repo}/commits?since={since}"):
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
        message = commit.get("message") if isinstance(commit.get("message"), str) else ""
        commits.append({"sha": item.get("sha"), "date": committer.get("date"),
                        "message": message.splitlines()[0] if message else ""})
    return commits


def fetch_releases(repo: str, client) -> list[dict]:
    """#156: every published release: tag, name, link, published_at; drafts
    and entries without a published_at are skipped."""
    releases = []
    for item in _paged(client, f"repos/{repo}/releases"):
        if item.get("draft") or not isinstance(item.get("published_at"), str):
            continue
        releases.append({"tag_name": item.get("tag_name"), "name": item.get("name"),
                         "html_url": item.get("html_url"), "published_at": item["published_at"]})
    return releases


def fetch_issues(repo: str, client) -> list[dict]:
    """Every issue of `repo` (pull requests skipped), newest first as GitHub
    lists them, with just ISSUE_FIELDS. Pages until a short page; a failure
    on any page raises GitHubUnavailable and nothing partial is returned."""
    issues: list[dict] = []
    page = 1
    while True:
        payload = client(f"repos/{repo}/issues?state=all&per_page={PAGE_SIZE}&page={page}")
        if payload is None:
            raise GitHubUnavailable(f"GitHub repository {repo} was not found")
        if not isinstance(payload, list):
            raise GitHubUnavailable("GitHub issues answer was not a list")
        for item in payload:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            issues.append({field: item.get(field) for field in ISSUE_FIELDS})
        if len(payload) < PAGE_SIZE:
            return issues
        page += 1


# --- Conditional reads (#158) -------------------------------------------------

class Answer(NamedTuple):
    """One GitHub answer: HTTP status (200 or 304), the ETag to send next
    time, the parsed body (None on 304) and the rate-limit headers, when
    the answer carried them, as {remaining, limit, reset_at}."""
    status: int
    etag: str | None
    payload: object
    rate: dict | None


def _rate_from_headers(headers: dict) -> dict | None:
    lower = {str(k).lower(): v for k, v in headers.items()}
    if "x-ratelimit-remaining" not in lower:
        return None
    try:
        reset = int(lower.get("x-ratelimit-reset", "0"))
        reset_at = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat() if reset else None
        return {"remaining": int(lower["x-ratelimit-remaining"]), "limit": int(lower.get("x-ratelimit-limit", "0")),
                "reset_at": reset_at}
    except ValueError:
        return None


def _parse_gh_include(text: str) -> tuple[int, dict, str]:
    """The status, headers and body of `gh api -i` output. gh prints the
    status line, the headers, a blank line, then the body (none on 304)."""
    head, sep, body = text.partition("\n\n")
    if not sep:
        head, sep, body = text.partition("\r\n\r\n")
    lines = head.replace("\r", "").splitlines()
    if not lines or not lines[0].upper().startswith("HTTP/"):
        raise GitHubUnavailable("gh api -i printed no HTTP status line")
    parts = lines[0].split()
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise GitHubUnavailable(f"gh api -i status line unreadable: {lines[0][:60]}") from exc
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


def _gh_conditional(executable: str):
    def read(path: str, etag: str | None = None) -> Answer:
        argv = [executable, "api", "-i", path]
        if etag:
            argv += ["-H", f"If-None-Match: {etag}"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubUnavailable(f"gh api failed: {type(exc).__name__}") from exc
        # gh exits 1 on a 304 but still prints the status line and headers.
        text = proc.stdout if proc.stdout.strip() else proc.stderr
        status, headers, body = _parse_gh_include(text)
        rate = _rate_from_headers(headers)
        if status == 304:
            return Answer(304, headers.get("etag") or etag, None, rate)
        if status == 404:
            return Answer(404, None, None, rate)
        if status != 200:
            raise GitHubUnavailable(f"GitHub HTTP {status}")
        try:
            return Answer(200, headers.get("etag"), json.loads(body), rate)
        except ValueError as exc:
            raise GitHubUnavailable("gh api returned invalid JSON") from exc
    return read


def _token_conditional(token: str):
    def read(path: str, etag: str | None = None) -> Answer:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "handsoff-fleet"}
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(f"{GITHUB_API}/{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_headers = dict(response.headers.items())
                return Answer(200, response_headers.get("ETag") or response_headers.get("Etag"),
                              json.loads(response.read().decode("utf-8")), _rate_from_headers(response_headers))
        except urllib.error.HTTPError as exc:
            rate = _rate_from_headers(dict(exc.headers.items())) if exc.headers else None
            if exc.code == 304:
                return Answer(304, (exc.headers.get("ETag") if exc.headers else None) or etag, None, rate)
            if exc.code == 404:
                return Answer(404, None, None, rate)
            raise GitHubUnavailable(f"GitHub HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise GitHubUnavailable(f"GitHub unreachable: {type(exc).__name__}") from exc
    return read


def conditional_reader():
    """read(path, etag) -> Answer through gh when it is authenticated, else
    GITHUB_TOKEN, else None (GitHub is not configured)."""
    executable = shutil.which("gh")
    if executable:
        try:
            probe = subprocess.run([executable, "auth", "token"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if probe is not None and probe.returncode == 0 and probe.stdout.strip():
            return _gh_conditional(executable)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return _token_conditional(token) if token else None


def plain_reader(client):
    """Wrap a fetch(path) client (no headers) as a reader: every answer is a
    counted 200 with no ETag, so every pass is a full read. Tests and the
    signals client use it."""
    def read(path: str, etag: str | None = None) -> Answer:
        payload = client(path)
        return Answer(404 if payload is None else 200, None, payload, None)
    return read


class _Tally:
    """Requests sent and requests that counted (answered 200) in one pass,
    plus the last rate-limit headers seen."""
    def __init__(self):
        self.total = 0
        self.counted = 0
        self.rate = None

    def note(self, answer: Answer) -> Answer:
        self.total += 1
        if answer.status == 200:
            self.counted += 1
        if answer.rate is not None:
            self.rate = answer.rate
        return answer


def _etag_for(old: dict | None, name: str, url: str) -> str | None:
    """The cached ETag for `name`, only when it was earned by this exact URL:
    a since boundary that advanced makes a new URL, and an ETag from the old
    one must never be sent with it."""
    if not old:
        return None
    if (old.get("etag_urls") or {}).get(name) != url:
        return None
    return (old.get("etags") or {}).get(name)


def _read_list(read, tally: _Tally, base: str, etag: str | None, *, what: str) -> tuple[list | None, str | None]:
    """A paged list endpoint, page 1 conditional. Returns (items, etag);
    items is None when page 1 answered 304 (nothing changed). Every page
    after the first is an unconditional 200."""
    joiner = "&" if "?" in base else "?"
    first = tally.note(read(f"{base}{joiner}per_page={PAGE_SIZE}&page=1", etag))
    if first.status == 304:
        return None, first.etag
    if first.status == 404:
        raise GitHubUnavailable(f"GitHub answered 404 for {what}")
    if not isinstance(first.payload, list):
        raise GitHubUnavailable(f"GitHub {what} answer was not a list")
    items = [item for item in first.payload if isinstance(item, dict)]
    page = 1
    payload = first.payload
    while len(payload) >= PAGE_SIZE:
        page += 1
        answer = tally.note(read(f"{base}{joiner}per_page={PAGE_SIZE}&page={page}", None))
        if answer.status != 200 or not isinstance(answer.payload, list):
            raise GitHubUnavailable(f"GitHub {what} page {page} failed")
        payload = answer.payload
        items.extend(item for item in payload if isinstance(item, dict))
    return items, first.etag


def _issue_row(item: dict) -> dict:
    return {field: item.get(field) for field in ISSUE_FIELDS}


def _commit_row(item: dict) -> dict:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    message = commit.get("message") if isinstance(commit.get("message"), str) else ""
    return {"sha": item.get("sha"), "date": committer.get("date"), "message": message.splitlines()[0] if message else ""}


def _release_row(item: dict) -> dict | None:
    if item.get("draft") or not isinstance(item.get("published_at"), str):
        return None
    return {"tag_name": item.get("tag_name"), "name": item.get("name"), "html_url": item.get("html_url"),
            "published_at": item["published_at"]}


def _newest(values) -> str | None:
    dated = [value for value in values if isinstance(value, str) and value]
    return max(dated) if dated else None


def _is_full_pass(old: dict | None, repo: str, now: datetime) -> bool:
    if not old or old.get("repo") != repo or old.get("error") or not old.get("full_pass_at"):
        return True
    try:
        last = datetime.fromisoformat(old["full_pass_at"])
    except (TypeError, ValueError):
        return True
    return (now - last) > timedelta(hours=full_pass_hours())


def collect_project(old: dict | None, repo: str, read, *, now: datetime, window_since: str) -> dict:
    """One project's pass: issues (full or incremental), commits (bounded
    window, incremental after the first pass) and releases (one read behind
    its ETag). Raises GitHubUnavailable on any failed read; the caller keeps
    the previous entry whole in that case."""
    tally = _Tally()
    etags: dict = {}
    etag_urls: dict = {}
    full = _is_full_pass(old, repo, now)
    cached_issues = list((old or {}).get("issues") or []) if not full else []
    if full:
        base = f"repos/{repo}/issues?state=all"
        items, etag = _read_list(read, tally, base, None, what="issues")
        issues = [_issue_row(item) for item in items if "pull_request" not in item]
        etags["issues"], etag_urls["issues"] = None, None  # the next pass reads the since URL, which earns its own ETag
        full_pass_at = now.isoformat()
    else:
        since = old.get("updated_since") or _newest(issue.get("updated_at") for issue in cached_issues) or window_since
        base = f"repos/{repo}/issues?state=all&since={since}"
        items, etag = _read_list(read, tally, base, _etag_for(old, "issues", base), what="issues")
        issues = cached_issues
        if items is not None:
            by_number = {issue.get("number"): issue for issue in cached_issues}
            for item in items:
                if "pull_request" in item:
                    continue
                by_number[item.get("number")] = _issue_row(item)
            issues = list(by_number.values())
        etags["issues"], etag_urls["issues"] = etag, base
        full_pass_at = old.get("full_pass_at")
    issues.sort(key=lambda issue: -(issue.get("number") or 0))
    updated_since = _newest(issue.get("updated_at") for issue in issues) or window_since

    cached_commits = list((old or {}).get("commits") or []) if old and old.get("repo") == repo else []
    newest_commit = _newest(commit.get("date") for commit in cached_commits)
    commit_since = newest_commit or window_since
    base = f"repos/{repo}/commits?since={commit_since}"
    items, etag = _read_list(read, tally, base, _etag_for(old, "commits", base), what="commits")
    commits = cached_commits
    if items is not None:
        by_sha = {commit.get("sha"): commit for commit in cached_commits}
        for item in items:
            by_sha[item.get("sha")] = _commit_row(item)
        commits = list(by_sha.values())
    commits = [commit for commit in commits if not commit.get("date") or commit["date"] >= window_since]
    commits.sort(key=lambda commit: commit.get("date") or "", reverse=True)
    etags["commits"], etag_urls["commits"] = etag, base

    base = f"repos/{repo}/releases"
    items, etag = _read_list(read, tally, base, _etag_for(old, "releases", base), what="releases")
    releases = list((old or {}).get("releases") or []) if old and old.get("repo") == repo else []
    if items is not None:
        releases = [row for row in (_release_row(item) for item in items) if row]
    etags["releases"], etag_urls["releases"] = etag, base

    return {"repo": repo, "fetched_at": now.isoformat(), "error": None, "issues": issues, "commits": commits,
            "releases": releases, "commits_since": window_since, "etags": etags, "etag_urls": etag_urls,
            "updated_since": updated_since,
            "full_pass_at": full_pass_at, "requests_total": tally.total, "requests_counted": tally.counted,
            "_rate": tally.rate}


class IssueCache:
    """Per registered project: repo, its complete issue list, fetched_at and
    error. Same lifecycle as SignalCache: loaded once, swapped whole under
    the lock, persisted atomically; a project whose read fails keeps its
    previous list and gets the error beside it."""

    def __init__(self, path: Path | None = None, *, registry: Path | None = None):
        self.path = path or issues_path(registry)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self.refreshed_at: str | None = None
        self.last_rate: dict | None = None
        self.load()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"schema": 1, "projects": {}}
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet issues cache {self.path} is unreadable "
                             f"({type(exc).__name__}); starting empty\n")
            payload = {"schema": 1, "projects": {}}
        projects = payload.get("projects") if isinstance(payload, dict) and payload.get("schema") == 1 else None
        if not isinstance(projects, dict):
            if payload != {"schema": 1, "projects": {}}:
                sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet issues cache {self.path} is malformed; starting empty\n")
            projects = {}
        clean = {}
        for root, value in projects.items():
            if isinstance(root, str) and isinstance(value, dict) and isinstance(value.get("issues"), list):
                clean[root] = {"repo": value.get("repo"), "fetched_at": value.get("fetched_at"),
                               "error": value.get("error"),
                               "issues": [item for item in value["issues"] if isinstance(item, dict)],
                               "commits": [item for item in (value.get("commits") or []) if isinstance(item, dict)],
                               "releases": [item for item in (value.get("releases") or []) if isinstance(item, dict)],
                               "commits_since": value.get("commits_since"),
                               # #158: absent in a v0.3.39 file; None here forces a full pass.
                               "etags": value.get("etags") if isinstance(value.get("etags"), dict) else {},
                               "etag_urls": value.get("etag_urls") if isinstance(value.get("etag_urls"), dict) else {},
                               "updated_since": value.get("updated_since"), "full_pass_at": value.get("full_pass_at"),
                               "requests_total": value.get("requests_total"), "requests_counted": value.get("requests_counted")}
        with self._lock:
            self._data = clean

    def get(self, root: Path | str) -> dict | None:
        with self._lock:
            found = self._data.get(str(Path(root).resolve()))
        return dict(found) if found else None

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {root: dict(value) for root, value in self._data.items()}

    def refresh(self, roots, *, reader=_ASK, client=_ASK, repo_of=None, since: str | None = None,
                now: datetime | None = None) -> dict[str, dict]:
        """Recompute every project's entry and swap them in. `reader(path,
        etag) -> Answer` is the seam tests inject (a plain `client(path)` is
        wrapped for the older tests); `repo_of(root)`, `since` (the commit
        window start) and `now` likewise. The three reads for a project
        succeed together or its previous entry is kept whole, with the error,
        which also forces a full pass next time."""
        repo_of = repo_of or origin_repo
        roots = [Path(root).resolve() for root in roots]
        previous = self.snapshot()
        now = now or datetime.now(timezone.utc)
        since = since or commits_since(now)
        if reader is _ASK:
            reader = plain_reader(client) if client is not _ASK and client is not None else (None if client is None else conditional_reader())
        fresh: dict[str, dict] = {}
        rate = None

        def empty(repo, error):
            return {"repo": repo, "fetched_at": utcnow(), "error": error, "issues": [], "commits": [],
                    "releases": [], "commits_since": None, "etags": {}, "etag_urls": {}, "updated_since": None, "full_pass_at": None,
                    "requests_total": 0, "requests_counted": 0}
        for root in roots:
            key = str(root)
            repo = repo_of(root)
            old = previous.get(key)
            if repo is None:
                fresh[key] = empty(None, NO_ORIGIN)
                continue
            if reader is None:
                fresh[key] = empty(repo, GITHUB_NOT_CONFIGURED)
                continue
            try:
                entry = collect_project(old, repo, reader, now=now, window_since=since)
            except GitHubUnavailable as exc:
                kept = old if old and old.get("repo") == repo else None
                fresh[key] = {**(kept or empty(repo, None)), "repo": repo, "error": str(exc),
                              "fetched_at": kept["fetched_at"] if kept else None}
                continue
            seen = entry.pop("_rate", None)
            if seen is not None:
                rate = seen
            fresh[key] = entry
        with self._lock:
            self._data = fresh
            self.refreshed_at = utcnow()
            if rate is not None:
                self.last_rate = rate
        self._persist(fresh)
        return fresh

    def _persist(self, data: dict[str, dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lib.atomic_write_json(self.path, {"schema": 1, "projects": data})
        except OSError as exc:
            sys.stderr.write(f"HANDSOFF_FLEET_WARNING: fleet issues cache {self.path} was not written "
                             f"({type(exc).__name__})\n")

