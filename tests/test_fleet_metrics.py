"""#153 and #156: the issue, commit and release collector behind the Fleet
Metrics tab, and the routes that serve it.

- IssueCollectorTests: REQ-002 and REQ-007, pagination, pull-request skip,
  field shapes, the commit window, failure keeping the whole previous entry,
  the unconfigured case, persistence.
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
import unittest
from datetime import datetime, timezone
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
        self.assertEqual(signals.issues_interval(), 900.0)
        os.environ.pop("HANDSOFF_FLEET_ISSUES_FILE")  # the env override wins; without it the file sits beside the registry
        self.assertEqual(signals.issues_path(self.base / "reg" / "projects.json"), (self.base / "reg" / "fleet-issues.json").resolve())


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
