"""#152: the GitHub and Beakon signals on Fleet project rows.

Four classes, one per acceptance criterion, so `[checks].commands` can bind
each criterion to the class that proves it:

- FleetSnapshotTests: REQ-001, the snapshot carries both keys from the cache
  and never fetches on its own.
- GitHubSignalTests: REQ-002, remote parsing, the client paths, the
  unconfigured case, and failure keeping the last good values.
- BeakonSignalTests: REQ-003, the landing-folder scan and attribution.
- SignalCacheTests: REQ-004, persistence, malformed files, the refresh
  thread returning before its first pass, the interval override.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_fleet import _FleetFixture, fleet

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_fleet_signals as signals  # noqa: E402


def _fake_client(issues=3, prs=2, release=("v1.2.3", "2026-09-18T12:00:00Z"), calls=None):
    """A GitHub reader with the three answers `fetch_github` asks for."""
    def fetch(path):
        if calls is not None:
            calls.append(path)
        if path.startswith("search/issues") and "is:issue" in path:
            return {"total_count": issues}
        if path.startswith("search/issues") and "is:pr" in path:
            return {"total_count": prs}
        if path.endswith("/releases/latest"):
            return None if release is None else {"tag_name": release[0], "published_at": release[1]}
        raise AssertionError(f"unexpected GitHub path {path}")
    return fetch


def _git_repo(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    return path


class _SignalsEnv(unittest.TestCase):
    """Every test runs with the cache file and the Beakon landing folder
    pointed into a temp dir, so nothing touches ~/.handsoff or a real worker."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-signals-test-"))
        self.cache_file = self.base / "fleet-signals.json"
        self.landing = self.base / "beakon"
        self.landing.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "HANDSOFF_FLEET_SIGNALS_FILE": str(self.cache_file),
            "BEAKON_WORK_ROOT": str(self.landing),
            "HANDSOFF_FLEET_SIGNALS_INTERVAL": "3600",
        })
        self._env.start()
        os.environ.pop("GITHUB_TOKEN", None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def beam(self, receipt: str, workdir: str | None, result: dict | str | None = None, task: str | None = None):
        folder = self.landing / receipt
        folder.mkdir()
        if task is None:
            task = f"---\nworkdir: {workdir}\ntimeout_seconds: 300\n---\n# Task\n" if workdir else "# Task without frontmatter\n"
        (folder / "task.md").write_text(task, encoding="utf-8")
        if result is not None:
            (folder / "result.json").write_text(result if isinstance(result, str) else json.dumps(result), encoding="utf-8")
        return folder


class FleetSnapshotTests(_FleetFixture, _SignalsEnv):
    def setUp(self):
        _FleetFixture.setUp(self)
        _SignalsEnv.setUp(self)

    def tearDown(self):
        _SignalsEnv.tearDown(self)
        _FleetFixture.tearDown(self)

    def test_snapshot_carries_both_signals_from_the_cache_and_fetches_nothing(self):
        alpha = self.project("alpha")
        fleet.register_project(alpha, self.registry)
        cache = signals.SignalCache(self.cache_file)
        fetches = []

        def github_fetch(root, client):
            fetches.append(str(root))
            return {"repo": "o/alpha", "open_issues": 4, "open_prs": 1,
                    "latest_release": {"tag": "v9", "published_at": "2026-09-01T00:00:00Z"},
                    "fetched_at": "2026-09-19T10:00:00+00:00", "error": None}
        scan_calls = []

        def beakon_scan(work_root):
            scan_calls.append(work_root)
            return {"fetched_at": "2026-09-19T10:00:01+00:00",
                    "beams": {str(alpha.resolve()): {"in_flight": 2, "finished": [
                        {"receipt": "bk-1", "outcome": "done", "finished_at": "2026-09-18T00:00:00+00:00"}]}}}
        cache.refresh([alpha], github_fetch=github_fetch, beakon_scan=beakon_scan, work_root=self.landing, client=None)
        self.assertEqual(fetches, [str(alpha.resolve())])
        self.assertEqual(scan_calls, [self.landing])
        for _ in range(3):
            snapshot = fleet.build_fleet(self.registry, signals=cache)
        project = snapshot["projects"][0]
        self.assertEqual(project["github"]["open_issues"], 4)
        self.assertEqual(project["github"]["open_prs"], 1)
        self.assertEqual(project["github"]["latest_release"]["tag"], "v9")
        self.assertEqual(project["github"]["fetched_at"], "2026-09-19T10:00:00+00:00")
        self.assertEqual(project["beakon"], {"in_flight": 2, "fetched_at": "2026-09-19T10:00:01+00:00",
                                             "last": {"receipt": "bk-1", "outcome": "done",
                                                      "finished_at": "2026-09-18T00:00:00+00:00"}})
        # Three snapshots, still exactly one fetch and one scan: build_fleet reads the cache only.
        self.assertEqual(len(fetches), 1)
        self.assertEqual(len(scan_calls), 1)

    def test_snapshot_without_a_cache_carries_null_signals(self):
        alpha = self.project("alpha")
        fleet.register_project(alpha, self.registry)
        with mock.patch.object(signals, "fetch_github", side_effect=AssertionError("must not fetch")), \
                mock.patch.object(signals, "scan_beakon", side_effect=AssertionError("must not scan")):
            project = fleet.build_fleet(self.registry)["projects"][0]
        self.assertIsNone(project["github"])
        self.assertIsNone(project["beakon"])

    def test_uninitialized_project_row_still_carries_the_signals(self):
        broken = self.base / "broken"
        broken.mkdir()
        (broken / "handsoff.toml").write_text("[project]\nname='broken'\n")
        fleet.register_project(broken, self.registry)
        cache = signals.SignalCache(self.cache_file)
        cache.refresh([broken], github_fetch=lambda root, client: {
            "repo": None, "open_issues": None, "open_prs": None, "latest_release": None,
            "fetched_at": "2026-09-19T10:00:00+00:00", "error": signals.NO_ORIGIN},
            beakon_scan=lambda work_root: None, work_root=None, client=None)
        project = fleet.build_fleet(self.registry, signals=cache)["projects"][0]
        self.assertFalse(project["initialized"])
        self.assertEqual(project["github"]["error"], signals.NO_ORIGIN)
        self.assertIsNone(project["beakon"])

    def test_the_event_stream_and_api_serve_the_servers_cache(self):
        alpha = self.project("alpha")
        fleet.register_project(alpha, self.registry)
        cache = signals.SignalCache(self.cache_file)
        cache.refresh([alpha], github_fetch=_ready("v3"), beakon_scan=lambda w: None, work_root=None, client=None)
        # The server's own refresh thread runs at once; pin its reads so the
        # test never reaches gh or a real landing folder and the value holds.
        patches = (mock.patch.object(signals, "fetch_github", _ready("v3")),
                   mock.patch.object(signals, "github_client", return_value=None),
                   mock.patch.object(signals, "beakon_work_root", return_value=None))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals=cache, signals_interval=3600)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", "/api/fleet")
            payload = json.loads(conn.getresponse().read())
            conn.close()
            self.assertEqual(payload["projects"][0]["github"]["latest_release"]["tag"], "v3")
        finally:
            server.stopping = True
            server.signals_thread.stop_event.set()
            server.shutdown()
            server.server_close()


def _ready(tag):
    def github_fetch(root, client):
        return {"repo": "o/r", "open_issues": 1, "open_prs": 0,
                "latest_release": {"tag": tag, "published_at": "2026-09-01T00:00:00Z"},
                "fetched_at": "2026-09-19T10:00:00+00:00", "error": None}
    return github_fetch


class GitHubSignalTests(_SignalsEnv):
    def test_remote_forms_resolve_to_owner_repo(self):
        for url in ("https://github.com/monzta1/project-handsoff.git", "https://github.com/monzta1/project-handsoff",
                    "https://github.com/monzta1/project-handsoff/", "git@github.com:monzta1/project-handsoff.git",
                    "git@github.com:monzta1/project-handsoff", "ssh://git@github.com/monzta1/project-handsoff.git"):
            self.assertEqual(signals.parse_github_remote(url), "monzta1/project-handsoff", url)
        for url in ("https://gitlab.com/o/r.git", "file:///tmp/r", "", "monzta1/project-handsoff"):
            self.assertIsNone(signals.parse_github_remote(url), url)

    def test_origin_remote_is_read_from_the_project(self):
        repo = _git_repo(self.base / "repo", "git@github.com:o/r.git")
        self.assertEqual(signals.origin_repo(repo), "o/r")
        self.assertIsNone(signals.origin_repo(_git_repo(self.base / "bare", None)))
        self.assertIsNone(signals.origin_repo(self.base / "missing"))

    def test_fetch_asks_the_three_reads_and_shapes_the_signal(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")
        calls = []
        signal = signals.fetch_github(repo, _fake_client(issues=7, prs=2, calls=calls))
        self.assertEqual(calls, ["search/issues?q=repo:o/r+is:issue+is:open&per_page=1",
                                 "search/issues?q=repo:o/r+is:pr+is:open&per_page=1",
                                 "repos/o/r/releases/latest"])
        self.assertEqual({k: signal[k] for k in ("repo", "open_issues", "open_prs", "latest_release", "error")}, {
            "repo": "o/r", "open_issues": 7, "open_prs": 2,
            "latest_release": {"tag": "v1.2.3", "published_at": "2026-09-18T12:00:00Z"}, "error": None})
        self.assertTrue(signal["fetched_at"])

    def test_a_repository_without_a_release_reads_null_release(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")
        signal = signals.fetch_github(repo, _fake_client(release=None))
        self.assertIsNone(signal["latest_release"])
        self.assertIsNone(signal["error"])

    def test_unconfigured_github_reads_the_exact_error_with_null_counts(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")
        with mock.patch.object(signals, "github_client", return_value=None):
            signal = signals.fetch_github(repo)
        self.assertEqual(signal["error"], "GitHub is not configured")
        self.assertEqual(signal["repo"], "o/r")
        self.assertIsNone(signal["open_issues"])
        self.assertIsNone(signal["open_prs"])
        self.assertIsNone(signal["latest_release"])

    def test_no_origin_reads_its_own_error_without_touching_github(self):
        repo = _git_repo(self.base / "repo", None)
        signal = signals.fetch_github(repo, lambda path: self.fail("must not fetch"))
        self.assertEqual(signal["error"], signals.NO_ORIGIN)
        self.assertIsNone(signal["repo"])

    def test_client_resolution_prefers_gh_then_token_then_nothing(self):
        with mock.patch.object(signals, "_gh_client", return_value=None):
            self.assertIsNone(signals.github_client())
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0k"}):
                client = signals.github_client()
            self.assertIsNotNone(client)
        marker = object()
        with mock.patch.object(signals, "_gh_client", return_value=marker):
            self.assertIs(signals.github_client(), marker)

    def test_gh_client_uses_gh_api_and_treats_404_as_none(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\n"
                      "if [ \"$1\" = auth ]; then echo tok; exit 0; fi\n"
                      "case \"$2\" in\n"
                      "  *releases/latest) echo 'gh: Not Found (HTTP 404)' >&2; exit 1;;\n"
                      "  *is:issue*) echo '{\"total_count\": 5}';;\n"
                      "  *is:pr*) echo '{\"total_count\": 1}';;\n"
                      "  *) echo 'gh: boom (HTTP 500)' >&2; exit 1;;\n"
                      "esac\n")
        gh.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            client = signals._gh_client()
            self.assertIsNotNone(client)
            self.assertEqual(client("search/issues?q=repo:o/r+is:issue+is:open&per_page=1"), {"total_count": 5})
            self.assertIsNone(client("repos/o/r/releases/latest"))
            with self.assertRaises(signals.GitHubUnavailable):
                client("repos/o/r/other")

    def test_a_logged_out_gh_is_not_a_client(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n")
        gh.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            self.assertIsNone(signals._gh_client())

    def test_a_failed_fetch_keeps_the_last_good_values_and_records_the_error(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")
        cache = signals.SignalCache(self.cache_file)
        cache.refresh([repo], github_fetch=lambda root, client: signals.fetch_github(root, _fake_client(issues=9)),
                      beakon_scan=lambda w: None, work_root=None, client=None)
        good = cache.get(repo)["github"]
        self.assertEqual(good["open_issues"], 9)

        def failing(root, client):
            raise signals.GitHubUnavailable("GitHub HTTP 503")
        cache.refresh([repo], github_fetch=failing, beakon_scan=lambda w: None, work_root=None, client=None)
        after = cache.get(repo)["github"]
        self.assertEqual(after["open_issues"], 9)
        self.assertEqual(after["latest_release"], good["latest_release"])
        self.assertEqual(after["fetched_at"], good["fetched_at"])
        self.assertEqual(after["error"], "GitHub HTTP 503")
        # A later success clears the error and moves fetched_at.
        cache.refresh([repo], github_fetch=lambda root, client: signals.fetch_github(root, _fake_client(issues=2)),
                      beakon_scan=lambda w: None, work_root=None, client=None)
        self.assertEqual(cache.get(repo)["github"]["open_issues"], 2)
        self.assertIsNone(cache.get(repo)["github"]["error"])

    def test_a_first_fetch_failure_reads_null_counts_with_the_error(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")
        cache = signals.SignalCache(self.cache_file)

        def failing(root, client):
            raise signals.GitHubUnavailable("GitHub unreachable: TimeoutError")
        cache.refresh([repo], github_fetch=failing, beakon_scan=lambda w: None, work_root=None, client=None)
        github = cache.get(repo)["github"]
        self.assertEqual(github["error"], "GitHub unreachable: TimeoutError")
        self.assertIsNone(github["open_issues"])
        self.assertIsNone(github["fetched_at"])

    def test_a_partial_failure_is_a_whole_failure(self):
        repo = _git_repo(self.base / "repo", "https://github.com/o/r.git")

        def client(path):
            if path.endswith("/releases/latest"):
                raise signals.GitHubUnavailable("GitHub HTTP 502")
            return {"total_count": 1}
        with self.assertRaises(signals.GitHubUnavailable):
            signals.fetch_github(repo, client)


class BeakonSignalTests(_SignalsEnv):
    def test_beams_are_attributed_by_frontmatter_workdir(self):
        alpha = (self.base / "alpha").resolve()
        beta = (self.base / "beta").resolve()
        alpha.mkdir()
        beta.mkdir()
        self.beam("bk-20260918T120000Z-aaa", str(alpha), {"status": "done", "finished_at": "2026-09-18T12:10:00+00:00"})
        self.beam("bk-20260918T130000Z-bbb", str(alpha), {"status": "failed", "finished_at": "2026-09-18T13:10:00+00:00"})
        self.beam("bk-20260918T140000Z-ccc", str(alpha))  # in flight
        self.beam("bk-20260918T150000Z-ddd", str(beta))
        self.beam("bk-20260918T160000Z-eee", str(beta))
        scan = signals.scan_beakon(self.landing)
        self.assertTrue(scan["fetched_at"])
        self.assertEqual(signals.beakon_signal(alpha, scan), {
            "in_flight": 1, "fetched_at": scan["fetched_at"],
            "last": {"receipt": "bk-20260918T130000Z-bbb", "outcome": "failed", "finished_at": "2026-09-18T13:10:00+00:00"}})
        self.assertEqual(signals.beakon_signal(beta, scan), {"in_flight": 2, "last": None, "fetched_at": scan["fetched_at"]})
        untouched = (self.base / "gamma").resolve()
        self.assertEqual(signals.beakon_signal(untouched, scan), {"in_flight": 0, "last": None, "fetched_at": scan["fetched_at"]})

    def test_workdir_is_expanded_and_resolved(self):
        home_project = Path.home() / ".handsoff-signals-test-home-project"
        self.beam("bk-20260918T120000Z-aaa", "~/.handsoff-signals-test-home-project",
                  {"status": "done", "finished_at": "2026-09-18T12:10:00+00:00"})
        scan = signals.scan_beakon(self.landing)
        self.assertIn(str(home_project.resolve()), scan["beams"])
        self.assertEqual(signals.beakon_signal(home_project, scan)["last"]["outcome"], "done")

    def test_result_fields_map_to_outcome_and_finished_at_with_unknown_fallbacks(self):
        alpha = (self.base / "alpha").resolve()
        alpha.mkdir()
        self.beam("bk-20260918T120000Z-aaa", str(alpha), {"status": "cancelled"})
        self.beam("bk-20260918T130000Z-bbb", str(alpha), "{not json")
        self.beam("bk-20260918T140000Z-ccc", str(alpha), {"finished_at": 42})
        scan = signals.scan_beakon(self.landing)
        finished = scan["beams"][str(alpha)]["finished"]
        self.assertEqual([(item["outcome"], item["finished_at"]) for item in finished],
                         [("unknown", None)] * 3)
        # Without finished_at the receipt (which embeds the send time) orders them.
        self.assertEqual([item["receipt"] for item in finished],
                         ["bk-20260918T120000Z-aaa", "bk-20260918T130000Z-bbb", "bk-20260918T140000Z-ccc"])
        self.assertEqual(signals.beakon_signal(alpha, scan)["last"]["receipt"], "bk-20260918T140000Z-ccc")

    def test_unparsable_frontmatter_and_foreign_folders_never_fail_the_scan(self):
        alpha = (self.base / "alpha").resolve()
        alpha.mkdir()
        self.beam("bk-20260918T120000Z-aaa", None)  # no frontmatter
        self.beam("bk-20260918T130000Z-bbb", None, task="---\ntimeout_seconds: 5\n---\nno workdir\n")
        self.beam("bk-20260918T140000Z-ccc", None, task="---\nworkdir: ''\n---\n")
        (self.landing / "bk-20260918T150000Z-ddd").mkdir()  # no task.md at all
        (self.landing / "log").mkdir()  # not a beam
        (self.landing / "bk-not-a-dir").write_text("x")
        self.beam("bk-20260918T160000Z-eee", str(alpha), {"status": "done", "finished_at": "2026-09-18T16:10:00+00:00"})
        scan = signals.scan_beakon(self.landing)
        self.assertEqual(list(scan["beams"]), [str(alpha)])
        self.assertEqual(signals.beakon_signal(alpha, scan)["last"]["outcome"], "done")

    def test_a_missing_landing_folder_is_an_empty_scan(self):
        scan = signals.scan_beakon(self.base / "nowhere")
        self.assertEqual(scan["beams"], {})

    def test_no_worker_means_null_signals_not_errors(self):
        with mock.patch.dict(os.environ, {"BEAKON_WORK_ROOT": ""}):
            self.assertIsNone(signals.beakon_work_root())
        self.assertIsNone(signals.beakon_signal(self.base, None))
        alpha = (self.base / "alpha").resolve()
        alpha.mkdir()
        cache = signals.SignalCache(self.cache_file)
        with mock.patch.dict(os.environ, {"BEAKON_WORK_ROOT": ""}):
            cache.refresh([alpha], github_fetch=_ready("v1"), client=None)
        self.assertIsNone(cache.get(alpha)["beakon"])

    def test_work_root_comes_from_the_env_or_the_worker_config(self):
        self.assertEqual(signals.beakon_work_root(), self.landing.resolve())
        config_home = self.base / "home"
        (config_home / ".config" / "beakon").mkdir(parents=True)
        (config_home / ".config" / "beakon" / "worker.toml").write_text('work_root = "~/beams"\n')
        with mock.patch.dict(os.environ, {"HOME": str(config_home)}), \
                mock.patch.object(Path, "home", return_value=config_home):
            os.environ.pop("BEAKON_WORK_ROOT")
            self.assertEqual(signals.beakon_work_root(), (config_home / "beams").resolve())
            (config_home / ".config" / "beakon" / "worker.toml").write_text('work_root = [1\n')
            self.assertIsNone(signals.beakon_work_root())
            (config_home / ".config" / "beakon" / "worker.toml").unlink()
            self.assertIsNone(signals.beakon_work_root())


class SignalCacheTests(_SignalsEnv):
    def test_refresh_persists_and_a_new_cache_serves_the_persisted_values(self):
        alpha = (self.base / "alpha").resolve()
        alpha.mkdir()
        cache = signals.SignalCache(self.cache_file)
        cache.refresh([alpha], github_fetch=_ready("v7"), beakon_scan=lambda w: {
            "fetched_at": "2026-09-19T09:00:00+00:00", "beams": {}}, work_root=self.landing, client=None)
        self.assertTrue(self.cache_file.is_file())
        stored = json.loads(self.cache_file.read_text())
        self.assertEqual(stored["schema"], 1)
        self.assertEqual(stored["projects"][str(alpha)]["github"]["latest_release"]["tag"], "v7")
        # A fresh cache (a restarted server) reads the file: same values, same fetched_at, before any refresh.
        restarted = signals.SignalCache(self.cache_file)
        self.assertIsNone(restarted.refreshed_at)
        self.assertEqual(restarted.get(alpha)["github"]["fetched_at"], "2026-09-19T10:00:00+00:00")
        self.assertEqual(restarted.get(alpha)["beakon"]["fetched_at"], "2026-09-19T09:00:00+00:00")

    def test_a_malformed_or_unreadable_cache_starts_empty_with_a_warning(self):
        import io
        for body in ("{not json", '{"schema": 2, "projects": {}}', '{"schema": 1, "projects": []}', "[]"):
            self.cache_file.write_text(body)
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                cache = signals.SignalCache(self.cache_file)
            self.assertEqual(cache.snapshot(), {}, body)
            self.assertIn("HANDSOFF_FLEET_WARNING", err.getvalue(), body)
        self.cache_file.unlink()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            cache = signals.SignalCache(self.cache_file)
        self.assertEqual(cache.snapshot(), {})
        self.assertEqual(err.getvalue(), "")
        # A partly valid file keeps the well-formed rows only.
        self.cache_file.write_text(json.dumps({"schema": 1, "projects": {
            "/a": {"github": {"open_issues": 1}, "beakon": "nope"}, "/b": "junk"}}))
        cache = signals.SignalCache(self.cache_file)
        self.assertEqual(cache.snapshot(), {"/a": {"github": {"open_issues": 1}, "beakon": None}})

    def test_the_refresh_thread_returns_before_its_first_pass_and_serves_persisted_values_meanwhile(self):
        alpha = (self.base / "alpha").resolve()
        alpha.mkdir()
        self.cache_file.write_text(json.dumps({"schema": 1, "projects": {str(alpha): {
            "github": {"repo": "o/r", "open_issues": 5, "open_prs": 0, "latest_release": None,
                       "fetched_at": "2026-09-19T08:00:00+00:00", "error": None}, "beakon": None}}}))
        cache = signals.SignalCache(self.cache_file)
        gate = threading.Event()
        started = threading.Event()

        def slow_fetch(root, client):
            started.set()
            gate.wait(5)
            return _ready("v8")(root, client)
        with mock.patch.object(signals, "fetch_github", slow_fetch), \
                mock.patch.object(signals, "github_client", return_value=None), \
                mock.patch.object(signals, "beakon_work_root", return_value=None):
            thread = signals.start_refresh_thread(cache, lambda: [alpha], interval=3600)
            try:
                self.assertTrue(started.wait(5))
                # The first pass is blocked inside the fetch; the cache still answers with the persisted row.
                self.assertEqual(cache.get(alpha)["github"]["open_issues"], 5)
                self.assertEqual(cache.get(alpha)["github"]["fetched_at"], "2026-09-19T08:00:00+00:00")
                gate.set()
                for _ in range(100):
                    if cache.refreshed_at:
                        break
                    threading.Event().wait(.05)
                self.assertEqual(cache.get(alpha)["github"]["latest_release"]["tag"], "v8")
            finally:
                thread.stop_event.set()
                thread.join(5)

    def test_a_fleet_server_starts_its_own_refresh_thread_and_returns_at_once(self):
        gate = threading.Event()
        with mock.patch.object(signals, "fetch_github", side_effect=lambda root, client: gate.wait(5) or _ready("v1")(root, client)), \
                mock.patch.object(signals, "github_client", return_value=None), \
                mock.patch.object(signals, "beakon_work_root", return_value=None):
            registry = self.base / "registry.json"
            server = fleet.FleetServer(("127.0.0.1", 0), registry, signals_interval=3600)
            try:
                self.assertTrue(server.signals_thread.is_alive())
                self.assertEqual(fleet.build_fleet(registry, signals=server.signals)["projects"], [])
            finally:
                gate.set()
                server.signals_thread.stop_event.set()
                server.server_close()

    def test_the_interval_and_the_cache_path_honour_their_overrides(self):
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_SIGNALS_INTERVAL": "45"}):
            self.assertEqual(signals.signals_interval(), 45.0)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_SIGNALS_INTERVAL": "nonsense"}):
            self.assertEqual(signals.signals_interval(), signals.DEFAULT_INTERVAL_SECONDS)
        with mock.patch.dict(os.environ, {"HANDSOFF_FLEET_SIGNALS_INTERVAL": "-1"}):
            self.assertEqual(signals.signals_interval(), signals.DEFAULT_INTERVAL_SECONDS)
        os.environ.pop("HANDSOFF_FLEET_SIGNALS_INTERVAL")
        self.assertEqual(signals.signals_interval(), 300.0)
        self.assertEqual(signals.signals_path(), self.cache_file.resolve())
        os.environ.pop("HANDSOFF_FLEET_SIGNALS_FILE")
        self.assertEqual(signals.signals_path(), Path.home() / ".handsoff" / "fleet-signals.json")
        self.assertEqual(signals.signals_path(self.base / "reg" / "projects.json"),
                         (self.base / "reg" / "fleet-signals.json").resolve())

    def test_a_refresh_cycle_error_never_stops_the_thread(self):
        import io
        cache = signals.SignalCache(self.cache_file)
        calls = []

        def roots():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("registry hiccup")
            return []
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            thread = signals.start_refresh_thread(cache, roots, interval=.05)
            try:
                for _ in range(100):
                    if len(calls) >= 2:
                        break
                    threading.Event().wait(.02)
            finally:
                thread.stop_event.set()
                thread.join(5)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("registry hiccup", err.getvalue())


if __name__ == "__main__":
    unittest.main()
