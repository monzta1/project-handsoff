"""#153, #156 and #158: the issue, commit and release collector behind the
Fleet Metrics tab, its conditional and incremental reads, and the routes
that serve it.

- IssueCollectorTests: REQ-002 and REQ-007, pagination, pull-request skip,
  field shapes, the commit window, failure keeping the whole previous entry,
  the unconfigured case, persistence.
- ConditionalReaderTests (#158): the gh -i and token readers, 200 / 304 /
  404 / 500 and malformed output.
- ConditionalCollectorTests (#158): a scripted reader driving full,
  no-change and incremental passes, the daily full pass, a v0.3.39 entry,
  the atomic failure rule, the interval floor.
- MetricsServerTests: REQ-003, /api/metrics from the cache with started_at
  and refreshed_at, registry and repo identity rules, the /metrics and
  /metrics.js routes under the CSP, /api/fleet unchanged, the second thread.
"""
import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_fleet import _FleetFixture, fleet

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_fleet_signals as signals  # noqa: E402


def _issue(number, created, closed=None, pr=False):
    item = {"number": number, "title": f"Issue {number}", "html_url": f"https://github.com/o/r/issues/{number}",
            "created_at": created, "closed_at": closed, "state": "closed" if closed else "open",
            "labels": [{"name": "bug"}], "body": "ignored"}
    if pr:
        item["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/1"}
    return item


def _commit(sha, date, message="Do the thing\n\nLonger body"):
    return {"sha": sha, "commit": {"message": message, "committer": {"date": date}, "author": {"date": date}}}


def _release(tag, published, draft=False, name=None):
    return {"tag_name": tag, "name": name or tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}",
            "published_at": published, "draft": draft, "prerelease": False}


class _Client:
    """A GitHub reader answering the three list endpoints with configurable pages."""

    def __init__(self, issues=(), commits=(), releases=(), fail_on=None):
        self.issues, self.commits, self.releases, self.fail_on = list(issues), list(commits), list(releases), fail_on
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self.fail_on and self.fail_on in path:
            raise signals.GitHubUnavailable(f"GitHub HTTP 502 on {path.split('?')[0]}")
        page = int(path.split("page=")[-1])
        source = self.issues if "/issues?" in path else self.commits if "/commits?" in path else self.releases
        start = (page - 1) * signals.PAGE_SIZE
        return source[start:start + signals.PAGE_SIZE]


class _Env(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-metrics-test-"))
        self.cache_file = self.base / "fleet-issues.json"
        self._env = mock.patch.dict(os.environ, {
            "HANDSOFF_FLEET_ISSUES_FILE": str(self.cache_file),
            "HANDSOFF_FLEET_SIGNALS_FILE": str(self.base / "fleet-signals.json"),
            "HANDSOFF_FLEET_ISSUES_INTERVAL": "3600",
            "HANDSOFF_FLEET_SIGNALS_INTERVAL": "3600",
            "BEAKON_WORK_ROOT": "",
        })
        self._env.start()
        os.environ.pop("GITHUB_TOKEN", None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def repo_dir(self, name, origin="https://github.com/o/r.git"):
        path = self.base / name
        path.mkdir()
        (path / "handsoff.toml").write_text(f"[project]\nname = '{name}'\n")  # registrable with Fleet
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
        return path.resolve()


class IssueCollectorTests(_Env):
    def test_issues_page_until_a_short_page_and_skip_pull_requests(self):
        items = [_issue(n, "2026-09-01T00:00:00Z") for n in range(1, 151)]
        items[3]["pull_request"] = {"url": "x"}
        client = _Client(issues=items)
        issues = signals.fetch_issues("o/r", client)
        self.assertEqual([c for c in client.calls if "/issues?" in c], [
            "repos/o/r/issues?state=all&per_page=100&page=1", "repos/o/r/issues?state=all&per_page=100&page=2"])
        self.assertEqual(len(issues), 149)
        self.assertEqual(set(issues[0]), set(signals.ISSUE_FIELDS))
        self.assertNotIn(4, [issue["number"] for issue in issues])

    def test_exactly_one_full_page_asks_for_a_second_empty_one(self):
        client = _Client(issues=[_issue(n, "2026-09-01T00:00:00Z") for n in range(1, 101)])
        self.assertEqual(len(signals.fetch_issues("o/r", client)), 100)
        self.assertEqual(len([c for c in client.calls if "/issues?" in c]), 2)

    def test_a_failed_page_raises_and_returns_nothing_partial(self):
        client = _Client(issues=[_issue(n, "2026-09-01T00:00:00Z") for n in range(1, 151)])
        original = client.__call__

        def failing(path):
            if "page=2" in path:
                raise signals.GitHubUnavailable("GitHub HTTP 502")
            return original(path)
        with self.assertRaises(signals.GitHubUnavailable):
            signals.fetch_issues("o/r", failing)

    def test_a_missing_repository_is_a_failure_not_an_empty_list(self):
        with self.assertRaises(signals.GitHubUnavailable):
            signals.fetch_issues("o/gone", lambda path: None)

    def test_commits_carry_sha_date_and_first_message_line_since_the_window(self):
        client = _Client(commits=[_commit("a" * 40, "2026-09-10T12:00:00Z"), _commit("b" * 40, "2026-09-11T12:00:00Z", "One line")])
        commits = signals.fetch_commits("o/r", client, "2026-03-23T13:20:42Z")
        self.assertEqual(client.calls, ["repos/o/r/commits?since=2026-03-23T13:20:42Z&per_page=100&page=1"])
        self.assertEqual(commits, [{"sha": "a" * 40, "date": "2026-09-10T12:00:00Z", "message": "Do the thing"},
                                   {"sha": "b" * 40, "date": "2026-09-11T12:00:00Z", "message": "One line"}])

    def test_commits_since_is_the_utc_instant_minus_the_window_in_iso_seconds(self):
        now = datetime(2026, 9, 19, 13, 20, 42, 123456, tzinfo=timezone.utc)
        self.assertEqual(signals.commits_since(now, 180), "2026-03-23T13:20:42Z")
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_COMMITS_DAYS": "10"}):
            self.assertEqual(signals.commits_since(now), "2026-09-09T13:20:42Z")
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_COMMITS_DAYS": "nonsense"}):
            self.assertEqual(signals.commits_days(), signals.DEFAULT_COMMITS_DAYS)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_COMMITS_DAYS": "0"}):
            self.assertEqual(signals.commits_days(), signals.DEFAULT_COMMITS_DAYS)

    def test_releases_skip_drafts_and_unpublished(self):
        client = _Client(releases=[_release("v1", "2026-09-01T00:00:00Z", name="First"), _release("v2", "2026-09-02T00:00:00Z", draft=True),
                                   {"tag_name": "v3", "name": "v3", "html_url": "u", "published_at": None, "draft": False}])
        releases = signals.fetch_releases("o/r", client)
        self.assertEqual(releases, [{"tag_name": "v1", "name": "First", "html_url": "https://github.com/o/r/releases/tag/v1",
                                     "published_at": "2026-09-01T00:00:00Z"}])

    def test_refresh_stores_the_three_lists_with_repo_and_window(self):
        repo = self.repo_dir("repo")
        cache = signals.IssueCache(self.cache_file)
        client = _Client(issues=[_issue(1, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")],
                         commits=[_commit("c" * 40, "2026-09-10T00:00:00Z")], releases=[_release("v1", "2026-09-03T00:00:00Z")])
        cache.refresh([repo], client=client, since="2026-03-23T13:20:42Z")
        entry = cache.get(repo)
        self.assertEqual(entry["repo"], "o/r")
        self.assertIsNone(entry["error"])
        self.assertEqual([i["number"] for i in entry["issues"]], [1])
        self.assertEqual(entry["commits"][0]["sha"], "c" * 40)
        self.assertEqual(entry["releases"][0]["tag_name"], "v1")
        self.assertEqual(entry["commits_since"], "2026-03-23T13:20:42Z")
        self.assertTrue(entry["fetched_at"])
        self.assertTrue(cache.refreshed_at)
        stored = json.loads(self.cache_file.read_text())
        self.assertEqual(stored["projects"][str(repo)]["commits_since"], "2026-03-23T13:20:42Z")
        restarted = signals.IssueCache(self.cache_file)
        self.assertIsNone(restarted.refreshed_at)
        self.assertEqual(restarted.get(repo)["fetched_at"], entry["fetched_at"])
        self.assertEqual(len(restarted.get(repo)["commits"]), 1)

    def test_a_commits_read_failure_keeps_the_whole_previous_entry(self):
        repo = self.repo_dir("repo")
        cache = signals.IssueCache(self.cache_file)
        good = _Client(issues=[_issue(1, "2026-09-01T00:00:00Z")], commits=[_commit("c" * 40, "2026-09-10T00:00:00Z")],
                       releases=[_release("v1", "2026-09-03T00:00:00Z")])
        cache.refresh([repo], client=good, since="2026-03-01T00:00:00Z")
        before = cache.get(repo)
        bad = _Client(issues=[_issue(1, "2026-09-01T00:00:00Z"), _issue(2, "2026-09-05T00:00:00Z")], fail_on="/commits?")
        cache.refresh([repo], client=bad, since="2026-03-02T00:00:00Z")
        after = cache.get(repo)
        self.assertEqual(after["error"], "GitHub HTTP 502 on repos/o/r/commits")
        self.assertEqual(after["issues"], before["issues"])  # the newer issue page is NOT taken
        self.assertEqual(after["commits"], before["commits"])
        self.assertEqual(after["releases"], before["releases"])
        self.assertEqual(after["fetched_at"], before["fetched_at"])
        self.assertEqual(after["commits_since"], "2026-03-01T00:00:00Z")

    def test_a_first_failure_reads_empty_lists_with_the_error(self):
        repo = self.repo_dir("repo")
        cache = signals.IssueCache(self.cache_file)
        cache.refresh([repo], client=_Client(fail_on="/issues?"), since="2026-03-01T00:00:00Z")
        entry = cache.get(repo)
        self.assertEqual(entry["issues"], [])
        self.assertIsNone(entry["fetched_at"])
        self.assertIn("GitHub HTTP 502", entry["error"])

    def test_unconfigured_github_and_no_origin_record_their_errors_with_empty_lists(self):
        repo = self.repo_dir("repo")
        bare = self.repo_dir("bare", origin=None)
        cache = signals.IssueCache(self.cache_file)
        cache.refresh([repo, bare], client=None, since="2026-03-01T00:00:00Z")
        self.assertEqual(cache.get(repo)["error"], signals.GITHUB_NOT_CONFIGURED)
        self.assertEqual(cache.get(repo)["issues"], [])
        self.assertEqual(cache.get(bare)["error"], signals.NO_ORIGIN)
        self.assertIsNone(cache.get(bare)["repo"])

    def test_a_malformed_cache_file_starts_empty_with_a_warning(self):
        self.cache_file.write_text("{oops")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            cache = signals.IssueCache(self.cache_file)
        self.assertEqual(cache.snapshot(), {})
        self.assertIn("HANDSOFF_FLEET_WARNING", err.getvalue())
        self.assertEqual(signals.issues_interval(), 3600.0)
        os.environ.pop("HANDSOFF_FLEET_ISSUES_INTERVAL")
        self.assertEqual(signals.issues_interval(), 60.0)   # #158 default
        os.environ.pop("HANDSOFF_FLEET_ISSUES_FILE")  # the env override wins; without it the file sits beside the registry
        self.assertEqual(signals.issues_path(self.base / "reg" / "projects.json"), (self.base / "reg" / "fleet-issues.json").resolve())


def _gh_script(cases: dict) -> str:
    """A fake gh whose `api -i` answers come from `cases`: path -> (status, etag, body)."""
    lines = ["#!/bin/sh", "if [ \"$1\" = auth ]; then echo tok; exit 0; fi", "path=\"$3\"", "etag=\"\"",
             "if [ \"$4\" = -H ]; then etag=\"${5#If-None-Match: }\"; fi", "case \"$path\" in"]
    for path, (status, etag, body) in cases.items():
        lines.append(f"  \"{path}\")")
        if status == 304:
            lines.append(f"    printf 'HTTP/2.0 304 Not Modified\\r\\nEtag: {etag}\\r\\nX-Ratelimit-Remaining: 4999\\r\\nX-Ratelimit-Limit: 5000\\r\\nX-Ratelimit-Reset: 1790000000\\r\\n\\r\\n'; exit 1;;")
        elif status == 200:
            lines.append(f"    printf 'HTTP/2.0 200 OK\\r\\nEtag: {etag}\\r\\nX-Ratelimit-Remaining: 4998\\r\\nX-Ratelimit-Limit: 5000\\r\\nX-Ratelimit-Reset: 1790000000\\r\\n\\r\\n{body}\\n';;")
        else:
            lines.append(f"    printf 'HTTP/2.0 {status} Nope\\r\\nX-Ratelimit-Remaining: 4997\\r\\n\\r\\n'; exit 1;;")
    lines += ["  *) echo garbage; exit 1;;", "esac"]
    return "\n".join(lines) + "\n"


class ConditionalReaderTests(_Env):
    def _gh(self, cases):
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(_gh_script(cases))
        gh.chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"})
        patch.start()
        self.addCleanup(patch.stop)
        return signals.conditional_reader()

    def test_gh_reader_parses_200_304_404_and_rate_headers(self):
        read = self._gh({"repos/o/r/releases?per_page=100&page=1": (200, 'W/"abc"', '[{"tag_name":"v1"}]'),
                         "repos/o/r/issues?state=all&since=x&per_page=100&page=1": (304, 'W/"def"', ""),
                         "repos/o/gone/releases?per_page=100&page=1": (404, "", "")})
        ok = read("repos/o/r/releases?per_page=100&page=1")
        self.assertEqual((ok.status, ok.etag, ok.payload), (200, 'W/"abc"', [{"tag_name": "v1"}]))
        self.assertEqual(ok.rate, {"remaining": 4998, "limit": 5000, "reset_at": "2026-09-21T14:13:20+00:00"})
        same = read("repos/o/r/issues?state=all&since=x&per_page=100&page=1", 'W/"def"')
        self.assertEqual((same.status, same.etag, same.payload), (304, 'W/"def"', None))
        self.assertEqual(same.rate["remaining"], 4999)
        self.assertEqual(read("repos/o/gone/releases?per_page=100&page=1").status, 404)

    def test_gh_reader_raises_on_server_errors_and_garbage(self):
        read = self._gh({"repos/o/r/releases?per_page=100&page=1": (500, "", "")})
        with self.assertRaises(signals.GitHubUnavailable):
            read("repos/o/r/releases?per_page=100&page=1")
        with self.assertRaises(signals.GitHubUnavailable):
            read("repos/o/r/unknown?per_page=100&page=1")

    def test_reader_falls_back_to_the_token_and_then_to_nothing(self):
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "gh").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "gh").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            self.assertIsNone(signals.conditional_reader())
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0k"}):
                read = signals.conditional_reader()
        self.assertIsNotNone(read)

    def test_token_reader_handles_200_304_and_errors(self):
        import email.message
        import urllib.error

        class _Response:
            def __init__(self, body, etag):
                self.headers = email.message.Message()
                self.headers["ETag"] = etag
                self.headers["X-RateLimit-Remaining"] = "4000"
                self.headers["X-RateLimit-Limit"] = "5000"
                self.headers["X-RateLimit-Reset"] = "1790000000"
                self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def opener(request, timeout=0):
            if request.get_header("If-none-match") == 'W/"same"':
                headers = email.message.Message()
                headers["ETag"] = 'W/"same"'
                headers["X-RateLimit-Remaining"] = "4000"
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", headers, None)
            if request.full_url.endswith("/boom?per_page=100&page=1"):
                raise urllib.error.HTTPError(request.full_url, 502, "Bad", email.message.Message(), None)
            return _Response(b'[{"sha": "a"}]', 'W/"new"')
        read = signals._token_conditional("t0k")
        with mock.patch.object(signals.urllib.request, "urlopen", opener):
            ok = read("repos/o/r/commits?per_page=100&page=1")
            self.assertEqual((ok.status, ok.etag, ok.payload, ok.rate["remaining"]), (200, 'W/"new"', [{"sha": "a"}], 4000))
            same = read("repos/o/r/commits?per_page=100&page=1", 'W/"same"')
            self.assertEqual((same.status, same.etag, same.payload), (304, 'W/"same"', None))
            with self.assertRaises(signals.GitHubUnavailable):
                read("repos/o/r/boom?per_page=100&page=1")


class _Scripted:
    """A reader answering by (path, etag): 304 when the etag matches the
    scripted one, 200 with the body otherwise. `fail` names a substring
    whose request raises."""

    def __init__(self, answers: dict, fail: str | None = None):
        self.answers, self.fail, self.calls = answers, fail, []

    def __call__(self, path, etag=None):
        self.calls.append((path, etag))
        if self.fail and self.fail in path:
            raise signals.GitHubUnavailable(f"GitHub HTTP 502 on {path.split('?')[0]}")
        if path not in self.answers:
            return signals.Answer(200, None, [], {"remaining": 4990, "limit": 5000, "reset_at": None})
        scripted_etag, body = self.answers[path]
        if etag and etag == scripted_etag:
            return signals.Answer(304, scripted_etag, None, {"remaining": 4990, "limit": 5000, "reset_at": None})
        return signals.Answer(200, scripted_etag, body, {"remaining": 4989, "limit": 5000, "reset_at": None})


NOW = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
WINDOW = "2026-03-23T14:00:00Z"


class ConditionalCollectorTests(_Env):
    def setUp(self):
        super().setUp()
        self.repo = self.repo_dir("repo")
        self.cache = signals.IssueCache(self.cache_file)
        self.full_issues = "repos/o/r/issues?state=all&per_page=100&page=1"
        self.releases = "repos/o/r/releases?per_page=100&page=1"

    def _issue(self, number, updated, closed=None):
        row = _issue(number, "2026-09-01T00:00:00Z", closed)
        row["updated_at"] = updated
        return row

    def _first_pass(self):
        reader = _Scripted({
            self.full_issues: ('W/"i1"', [self._issue(1, "2026-09-10T00:00:00Z"), self._issue(2, "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z"),
                                          {**self._issue(3, "2026-09-13T00:00:00Z"), "pull_request": {}}]),
            f"repos/o/r/commits?since={WINDOW}&per_page=100&page=1": ('W/"c1"', [_commit("a" * 40, "2026-09-11T00:00:00Z"), _commit("b" * 40, "2026-09-12T00:00:00Z")]),
            self.releases: ('W/"r1"', [_release("v1", "2026-09-05T00:00:00Z")]),
        })
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=NOW)
        return reader

    def test_first_pass_is_full_and_sets_the_incremental_state(self):
        reader = self._first_pass()
        entry = self.cache.get(self.repo)
        self.assertEqual([c[1] for c in reader.calls], [None, None, None])  # no ETags on a first pass
        self.assertEqual([i["number"] for i in entry["issues"]], [2, 1])   # the PR is skipped
        self.assertEqual(entry["updated_since"], "2026-09-12T00:00:00Z")
        self.assertEqual(entry["full_pass_at"], NOW.isoformat())
        self.assertEqual(entry["etags"], {"issues": None, "commits": 'W/"c1"', "releases": 'W/"r1"'})
        self.assertEqual((entry["requests_total"], entry["requests_counted"]), (3, 3))
        self.assertEqual(self.cache.last_rate["remaining"], 4989)
        self.assertEqual(entry["issues"][0]["updated_at"], "2026-09-12T00:00:00Z")

    def test_a_no_change_pass_is_three_304s_and_changes_nothing_else(self):
        self._first_pass()
        before = self.cache.get(self.repo)
        since_issues = "repos/o/r/issues?state=all&since=2026-09-12T00:00:00Z&per_page=100&page=1"
        since_commits = "repos/o/r/commits?since=2026-09-12T00:00:00Z&per_page=100&page=1"
        reader = _Scripted({since_issues: ('W/"i2"', [self._issue(2, "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z")]),
                            since_commits: ('W/"c2"', [_commit("b" * 40, "2026-09-12T00:00:00Z")]),
                            self.releases: ('W/"r1"', [_release("v1", "2026-09-05T00:00:00Z")])})
        # Second pass: the since URLs are new, so no ETag rides along (the window URL's commit ETag
        # was earned by another URL); issues and commits answer 200 once (the boundary items repeat
        # harmlessly), releases 304 on the known ETag of the same URL.
        later = NOW + timedelta(minutes=1)
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=later)
        middle = self.cache.get(self.repo)
        self.assertEqual([c[1] for c in reader.calls], [None, None, 'W/"r1"'])
        self.assertEqual(middle["issues"], before["issues"])
        self.assertEqual(middle["commits"], before["commits"])
        self.assertEqual((middle["requests_total"], middle["requests_counted"]), (3, 2))
        self.assertEqual(middle["etags"], {"issues": 'W/"i2"', "commits": 'W/"c2"', "releases": 'W/"r1"'})
        # Third pass: nothing changed on GitHub, every ETag matches: three 304s, zero counted.
        reader.calls.clear()
        latest = later + timedelta(minutes=1)
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=latest)
        after = self.cache.get(self.repo)
        self.assertEqual([c[1] for c in reader.calls], ['W/"i2"', 'W/"c2"', 'W/"r1"'])
        self.assertEqual((after["requests_total"], after["requests_counted"]), (3, 0))
        self.assertEqual(after["fetched_at"], latest.isoformat())
        for key in ("issues", "commits", "releases", "etags", "updated_since", "full_pass_at", "repo", "commits_since"):
            self.assertEqual(after[key], middle[key], key)
        self.assertIsNone(after["error"])
        self.assertEqual(self.cache.last_rate["remaining"], 4990)

    def test_an_incremental_pass_merges_updated_and_new_items(self):
        self._first_pass()
        since_issues = "repos/o/r/issues?state=all&since=2026-09-12T00:00:00Z&per_page=100&page=1"
        since_commits = "repos/o/r/commits?since=2026-09-12T00:00:00Z&per_page=100&page=1"
        reader = _Scripted({
            since_issues: ('W/"i3"', [self._issue(1, "2026-09-15T00:00:00Z", "2026-09-15T00:00:00Z"),   # #1 closed since
                                      self._issue(4, "2026-09-16T00:00:00Z"),                            # new
                                      {**self._issue(5, "2026-09-16T01:00:00Z"), "pull_request": {}}]),  # PR, skipped
            since_commits: ('W/"c3"', [_commit("b" * 40, "2026-09-12T00:00:00Z"), _commit("c" * 40, "2026-09-14T00:00:00Z")]),
            self.releases: ('W/"r2"', [_release("v2", "2026-09-15T00:00:00Z"), _release("v1", "2026-09-05T00:00:00Z")]),
        })
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=NOW + timedelta(minutes=1))
        entry = self.cache.get(self.repo)
        self.assertEqual([(i["number"], i["closed_at"]) for i in entry["issues"]],
                         [(4, None), (2, "2026-09-12T00:00:00Z"), (1, "2026-09-15T00:00:00Z")])
        self.assertEqual(entry["updated_since"], "2026-09-16T00:00:00Z")
        self.assertEqual([c["sha"][0] for c in entry["commits"]], ["c", "b", "a"])
        self.assertEqual([r["tag_name"] for r in entry["releases"]], ["v2", "v1"])
        self.assertEqual((entry["requests_total"], entry["requests_counted"]), (3, 3))
        self.assertEqual(entry["full_pass_at"], NOW.isoformat())  # not a full pass

    def test_commits_older_than_the_window_are_pruned_and_the_boundary_commit_is_kept(self):
        self._first_pass()
        newer_window = "2026-09-12T00:00:00Z"   # the window moved past commit a
        since_commits = "repos/o/r/commits?since=2026-09-12T00:00:00Z&per_page=100&page=1"
        reader = _Scripted({since_commits: ('W/"c2"', [_commit("b" * 40, "2026-09-12T00:00:00Z")])})
        self.cache.refresh([self.repo], reader=reader, since=newer_window, now=NOW + timedelta(minutes=1))
        entry = self.cache.get(self.repo)
        self.assertEqual([c["sha"][0] for c in entry["commits"]], ["b"])
        self.assertEqual(entry["commits_since"], newer_window)

    def test_a_stale_full_pass_at_forces_a_full_read_that_drops_deleted_issues(self):
        self._first_pass()
        reader = _Scripted({self.full_issues: ('W/"i9"', [self._issue(2, "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z")])})
        much_later = NOW + timedelta(hours=25)
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=much_later)
        entry = self.cache.get(self.repo)
        self.assertEqual(reader.calls[0], (self.full_issues, None))
        self.assertEqual([i["number"] for i in entry["issues"]], [2])   # #1 was deleted upstream
        self.assertEqual(entry["full_pass_at"], much_later.isoformat())
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_FULL_PASS_HOURS": "48"}):
            reader2 = _Scripted({})
            self.cache.refresh([self.repo], reader=reader2, since=WINDOW, now=much_later + timedelta(hours=30))
            self.assertIn("since=", reader2.calls[0][0])   # 30 h < 48 h: still incremental

    def test_a_v0_3_39_entry_loads_and_forces_a_full_pass(self):
        self.cache_file.write_text(json.dumps({"schema": 1, "projects": {str(self.repo): {
            "repo": "o/r", "fetched_at": "2026-09-19T13:00:00+00:00", "error": None,
            "issues": [{"number": 1, "title": "t", "html_url": "u", "created_at": "2026-09-01T00:00:00Z", "closed_at": None}],
            "commits": [], "releases": [], "commits_since": WINDOW}}}))
        cache = signals.IssueCache(self.cache_file)
        loaded = cache.get(self.repo)
        self.assertEqual(loaded["etags"], {})
        self.assertIsNone(loaded["full_pass_at"])
        reader = _Scripted({self.full_issues: ('W/"i1"', [self._issue(1, "2026-09-10T00:00:00Z")])})
        cache.refresh([self.repo], reader=reader, since=WINDOW, now=NOW)
        self.assertEqual(reader.calls[0], (self.full_issues, None))
        self.assertEqual(cache.get(self.repo)["full_pass_at"], NOW.isoformat())

    def test_a_failure_after_a_304_keeps_the_whole_previous_entry_and_forces_a_full_pass(self):
        self._first_pass()
        before = self.cache.get(self.repo)
        since_issues = "repos/o/r/issues?state=all&since=2026-09-12T00:00:00Z&per_page=100&page=1"
        reader = _Scripted({since_issues: ('W/"i2"', [])}, fail="/commits?")
        # Prime the issues ETag so the failing pass really starts with a 304 on issues.
        self.cache.refresh([self.repo], reader=_Scripted({since_issues: ('W/"i2"', [])}), since=WINDOW, now=NOW + timedelta(minutes=1))
        primed = self.cache.get(self.repo)
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=NOW + timedelta(minutes=2))
        after = self.cache.get(self.repo)
        self.assertEqual(reader.calls[0][1], 'W/"i2"')
        self.assertEqual(after["error"], "GitHub HTTP 502 on repos/o/r/commits")
        for key in ("issues", "commits", "releases", "etags", "updated_since", "full_pass_at", "fetched_at"):
            self.assertEqual(after[key], primed[key], key)
        self.assertEqual(after["issues"], before["issues"])
        # The error forces a full read next time.
        recovery = _Scripted({self.full_issues: ('W/"i5"', [])})
        self.cache.refresh([self.repo], reader=recovery, since=WINDOW, now=NOW + timedelta(minutes=3))
        self.assertEqual(recovery.calls[0], (self.full_issues, None))
        self.assertIsNone(self.cache.get(self.repo)["error"])

    def test_an_etag_is_never_sent_with_a_url_it_was_not_earned_by(self):
        self._first_pass()
        since_issues = "repos/o/r/issues?state=all&since=2026-09-12T00:00:00Z&per_page=100&page=1"
        since_commits = "repos/o/r/commits?since=2026-09-12T00:00:00Z&per_page=100&page=1"
        # Pass 2 advances both boundaries (an issue updated on the 16th, a commit on the 14th).
        reader = _Scripted({since_issues: ('W/"i2"', [self._issue(4, "2026-09-16T00:00:00Z")]),
                            since_commits: ('W/"c2"', [_commit("c" * 40, "2026-09-14T00:00:00Z")]),
                            self.releases: ('W/"r1"', [_release("v1", "2026-09-05T00:00:00Z")])})
        self.cache.refresh([self.repo], reader=reader, since=WINDOW, now=NOW + timedelta(minutes=1))
        entry = self.cache.get(self.repo)
        self.assertEqual(entry["etags"]["issues"], 'W/"i2"')
        self.assertEqual(entry["etag_urls"]["issues"], since_issues[:-len("&per_page=100&page=1")])
        # Pass 3 asks new since URLs: the old ETags must NOT ride along, releases' may.
        reader3 = _Scripted({self.releases: ('W/"r1"', [_release("v1", "2026-09-05T00:00:00Z")])})
        self.cache.refresh([self.repo], reader=reader3, since=WINDOW, now=NOW + timedelta(minutes=2))
        self.assertEqual([c for c in reader3.calls], [
            ("repos/o/r/issues?state=all&since=2026-09-16T00:00:00Z&per_page=100&page=1", None),
            ("repos/o/r/commits?since=2026-09-14T00:00:00Z&per_page=100&page=1", None),
            (self.releases, 'W/"r1"'),
        ])

    def test_the_interval_has_a_floor_and_a_new_default(self):
        os.environ.pop("HANDSOFF_FLEET_ISSUES_INTERVAL")
        self.assertEqual(signals.issues_interval(), 60.0)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_ISSUES_INTERVAL": "5"}):
            self.assertEqual(signals.issues_interval(), 30.0)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_ISSUES_INTERVAL": "45"}):
            self.assertEqual(signals.issues_interval(), 45.0)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_ISSUES_INTERVAL": "-3"}):
            self.assertEqual(signals.issues_interval(), 60.0)
        self.assertEqual(signals.full_pass_hours(), 24.0)

    def test_api_metrics_carries_rate_limit_and_counters(self):
        self._first_pass()
        registry = self.base / "reg.json"
        fleet.save_registry([{"root": str(self.repo), "registered_at": "2026-09-19T00:00:00+00:00"}], registry)
        payload = fleet.build_metrics(registry, self.cache, NOW.isoformat())
        self.assertEqual(payload["rate_limit"]["remaining"], 4989)
        project = payload["projects"][0]
        self.assertEqual((project["requests_total"], project["requests_counted"]), (3, 3))
        self.assertEqual(project["updated_since"], "2026-09-12T00:00:00Z")


class RegistryWakeTests(_Env):
    """#157: the refresh threads wake on a registry change."""

    def _registry(self, roots):
        path = self.base / "registry.json"
        fleet.save_registry([{"root": str(r), "registered_at": "2026-09-19T00:00:00+00:00"} for r in roots], path)
        return path

    def _wait(self, predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def test_a_project_registered_while_the_thread_idles_is_collected_within_seconds(self):
        alpha = self.repo_dir("alpha", "https://github.com/o/alpha.git")
        registry = self._registry([alpha])
        cache = signals.IssueCache(self.cache_file)
        passes = []
        reader = _Scripted({})

        def refresh(roots):
            cache.refresh(roots, reader=reader, since=WINDOW, now=NOW)
            passes.append([Path(r).name for r in roots])   # recorded once the pass has landed
        stub = type("Stub", (), {"refresh": staticmethod(refresh)})()
        thread = signals.start_refresh_thread(stub, lambda: [e["root"] for e in fleet.load_registry(registry)],
                                              interval=3600, wake_path=registry, wake=0.2, name="wake-test")
        try:
            self.assertTrue(self._wait(lambda: len(passes) == 1))
            time.sleep(0.5)
            self.assertEqual(len(passes), 1)   # idle: no pass without a change
            beta = self.repo_dir("beta", "https://github.com/o/beta.git")
            started = time.time()
            fleet.register_project(beta, registry)
            self.assertTrue(self._wait(lambda: len(passes) >= 2))
            self.assertLess(time.time() - started, 10)
            self.assertIn("beta", passes[-1])
            self.assertIsNotNone(cache.get(beta))
            time.sleep(0.6)
            self.assertEqual(len(passes), 2)   # the interval restarted; no extra pass
        finally:
            thread.stop_event.set()
            thread.join(5)

    def test_a_change_during_a_running_pass_is_seen_at_that_pass_end(self):
        alpha = self.repo_dir("alpha", "https://github.com/o/alpha.git")
        registry = self._registry([alpha])
        gate = threading.Event()
        passes = []

        def refresh(roots):
            passes.append([Path(r).name for r in roots])
            if len(passes) == 1:
                gate.wait(5)   # the first pass is blocked inside its fetch
        stub = type("Stub", (), {"refresh": staticmethod(refresh)})()
        thread = signals.start_refresh_thread(stub, lambda: [e["root"] for e in fleet.load_registry(registry)],
                                              interval=3600, wake_path=registry, wake=0.2, name="wake-test")
        try:
            self.assertTrue(self._wait(lambda: len(passes) == 1))
            beta = self.repo_dir("beta", "https://github.com/o/beta.git")
            fleet.register_project(beta, registry)   # lands while pass 1 is blocked
            time.sleep(0.5)
            self.assertEqual(len(passes), 1)
            gate.set()
            self.assertTrue(self._wait(lambda: len(passes) >= 2, 3))
            self.assertIn("beta", passes[1])
        finally:
            gate.set()
            thread.stop_event.set()
            thread.join(5)

    def test_a_missing_registry_never_raises_and_the_signature_is_stable(self):
        missing = self.base / "nowhere.json"
        self.assertEqual(signals.registry_signature(missing), (None, None))
        self.assertEqual(signals.registry_signature(None), (None, None))
        passes = []
        stub = type("Stub", (), {"refresh": staticmethod(lambda roots: passes.append(list(roots)))})()
        thread = signals.start_refresh_thread(stub, lambda: [], interval=3600, wake_path=missing, wake=0.1, name="wake-test")
        try:
            self.assertTrue(self._wait(lambda: len(passes) == 1))
            time.sleep(0.5)
            self.assertEqual(len(passes), 1)
        finally:
            thread.stop_event.set()
            thread.join(5)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_WAKE_SECONDS": "2.5"}):
            self.assertEqual(signals.wake_seconds(), 2.5)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_WAKE_SECONDS": "no"}):
            self.assertEqual(signals.wake_seconds(), 5.0)


class RepoDedupeTests(_Env):
    """#160: roots sharing one repository are collected once."""

    def test_two_roots_one_repo_share_one_set_of_reads_and_equal_entries(self):
        checkout = self.repo_dir("checkout", "https://github.com/o/Repo.git")
        lane = self.repo_dir("lane", "git@github.com:O/repo.git")
        cache = signals.IssueCache(self.cache_file)
        reader = _Scripted({
            "repos/o/Repo/issues?state=all&per_page=100&page=1": ('W/"i"', [_issue(1, "2026-09-01T00:00:00Z")]),
            f"repos/o/Repo/commits?since={WINDOW}&per_page=100&page=1": ('W/"c"', [_commit("a" * 40, "2026-09-11T00:00:00Z")]),
            "repos/o/Repo/releases?per_page=100&page=1": ('W/"r"', [_release("v1", "2026-09-05T00:00:00Z")]),
        })
        cache.refresh([checkout, lane], reader=reader, since=WINDOW, now=NOW)
        self.assertEqual(len(reader.calls), 3)   # one set of reads, not two
        first, second = cache.get(checkout), cache.get(lane)
        self.assertEqual(first, second)
        self.assertEqual(first["repo"], "o/Repo")
        self.assertEqual((first["requests_total"], first["requests_counted"]), (3, 3))
        self.assertEqual([i["number"] for i in second["issues"]], [1])
        registry = self.base / "reg.json"
        fleet.save_registry([{"root": str(r), "registered_at": "2026-09-19T00:00:00+00:00"} for r in (checkout, lane)], registry)
        payload = fleet.build_metrics(registry, cache, NOW.isoformat())
        self.assertEqual([(p["name"], p["repo"], len(p["issues"])) for p in payload["projects"]],
                         [("checkout", "o/Repo", 1), ("lane", "o/Repo", 1)])

    def test_a_failure_is_shared_too_and_unparsed_origins_never_group(self):
        checkout = self.repo_dir("checkout", "https://github.com/o/r.git")
        lane = self.repo_dir("lane", "https://github.com/o/r")
        bare_a = self.repo_dir("bare-a", origin=None)
        bare_b = self.repo_dir("bare-b", origin=None)
        cache = signals.IssueCache(self.cache_file)
        reader = _Scripted({}, fail="/issues?")
        cache.refresh([checkout, lane, bare_a, bare_b], reader=reader, since=WINDOW, now=NOW)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(cache.get(checkout)["error"], cache.get(lane)["error"])
        self.assertIn("GitHub HTTP 502", cache.get(lane)["error"])
        self.assertEqual(cache.get(bare_a)["error"], signals.NO_ORIGIN)
        self.assertIsNone(cache.get(bare_b)["repo"])
        self.assertEqual(signals.repo_identity("O/Repo"), "o/repo")
        self.assertIsNone(signals.repo_identity(None))


class MetricsServerTests(_FleetFixture, _Env):
    def setUp(self):
        _FleetFixture.setUp(self)
        _Env.setUp(self)
        # Pin every collector so no fixture server reaches GitHub or a worker.
        for patch in (mock.patch.object(signals, "github_client", return_value=None),
                      mock.patch.object(signals, "beakon_work_root", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        _Env.tearDown(self)
        _FleetFixture.tearDown(self)

    def _serve(self, **kwargs):
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals_interval=3600, issues_interval=3600, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.issues_thread.stop_event.set)
        self.addCleanup(server.signals_thread.stop_event.set)
        return server

    def _get(self, server, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, body

    def test_api_metrics_serves_the_cache_with_started_and_refreshed_at(self):
        alpha = self.project("alpha")
        subprocess.run(["git", "init", "-q"], cwd=alpha, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:o/alpha.git"], cwd=alpha, check=True)
        fleet.register_project(alpha, self.registry)
        cache = signals.IssueCache(self.cache_file)
        cache.refresh([alpha], client=_Client(issues=[_issue(7, "2026-09-01T00:00:00Z")], commits=[_commit("d" * 40, "2026-09-02T00:00:00Z")],
                                              releases=[_release("v9", "2026-09-03T00:00:00Z")]), since="2026-03-01T00:00:00Z")
        with mock.patch.object(signals, "fetch_issues", side_effect=AssertionError("must not fetch inline")):
            server = self._serve(issues=cache)
            status, headers, body = self._get(server, "/api/metrics")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["started_at"], server.started_at)
        self.assertEqual(payload["refreshed_at"], cache.refreshed_at)
        project = payload["projects"][0]
        self.assertEqual(project["repo"], "o/alpha")
        self.assertEqual(project["issues"][0]["number"], 7)
        self.assertEqual(project["commits"][0]["sha"], "d" * 40)
        self.assertEqual(project["releases"][0]["tag_name"], "v9")
        self.assertEqual(project["commits_since"], "2026-03-01T00:00:00Z")
        self.assertIn("Content-Security-Policy", headers)

    def test_unregistered_roots_and_foreign_repos_are_dropped(self):
        alpha = self.project("alpha")
        subprocess.run(["git", "init", "-q"], cwd=alpha, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/alpha"], cwd=alpha, check=True)
        gone = self.base / "gone"
        gone.mkdir()
        fleet.register_project(alpha, self.registry)
        cache = signals.IssueCache(self.cache_file)
        cache.refresh([alpha, gone], repo_of=lambda root: "o/old" if root == alpha.resolve() else "o/gone",
                      client=_Client(issues=[_issue(1, "2026-09-01T00:00:00Z")]), since="2026-03-01T00:00:00Z")
        payload = fleet.build_metrics(self.registry, cache, "2026-09-19T00:00:00+00:00")
        self.assertEqual([p["name"] for p in payload["projects"]], ["alpha"])
        alpha_row = payload["projects"][0]
        self.assertEqual(alpha_row["repo"], "o/alpha")
        self.assertEqual(alpha_row["issues"], [])
        self.assertIn("belong to o/old", alpha_row["error"])

    def test_a_project_without_a_cache_entry_reads_empty_and_the_fleet_snapshot_is_unchanged(self):
        alpha = self.project("alpha")
        fleet.register_project(alpha, self.registry)
        cache = signals.IssueCache(self.cache_file)
        payload = fleet.build_metrics(self.registry, cache, None)
        self.assertEqual(payload["projects"][0]["issues"], [])
        self.assertIsNone(payload["refreshed_at"])
        snapshot = fleet.build_fleet(self.registry)
        for key in ("issues", "commits", "releases"):
            self.assertNotIn(key, snapshot["projects"][0])

    def test_metrics_page_and_script_are_served_under_the_csp(self):
        server = self._serve()
        status, headers, body = self._get(server, "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b'<script src="/metrics.js" defer></script>', body)
        self.assertIn(b'href="/metrics" aria-current="page"', body)
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'")
        status, headers, body = self._get(server, "/metrics.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"function computeSeries", body)
        status, _, body = self._get(server, "/")
        self.assertIn(b'<a href="/metrics">METRICS</a>', body)

    def test_the_server_starts_the_issues_thread_after_binding(self):
        server = self._serve()
        self.assertTrue(server.issues_thread.is_alive())
        self.assertEqual(server.issues_thread.name, "fleet-issues")
        self.assertTrue(server.started_at)


if __name__ == "__main__":
    unittest.main()
