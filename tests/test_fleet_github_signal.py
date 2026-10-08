"""#433: Fleet's GitHub signal never uses the search API.

One GraphQL query per repository gives the open issue count, the open PR
count and the latest release; roots sharing a repository share one fetch
per refresh; a rate-limit answer keeps the last good values, backs off until
the reset GitHub reports, and the card reads a plain line instead of gh's
stderr.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_fleet_signals as signals  # noqa: E402

NOW = 1_800_000_000.0
RESET = NOW + 900


def _git_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    return path.resolve()


class _Client:
    """A fetch(path, body) spy answering the signal's GraphQL query."""

    def __init__(self, issues=4, prs=1, tag="v2.0.0"):
        self.calls = []
        self.answer = {"data": {"repository": {
            "issues": {"totalCount": issues}, "pullRequests": {"totalCount": prs},
            "latestRelease": {"tagName": tag, "publishedAt": "2026-10-01T09:00:00Z"}}}}
        self.raise_next = None

    def __call__(self, path, body=None):
        self.calls.append((path, body))
        if self.raise_next is not None:
            raise self.raise_next
        return self.answer


class GitHubSignalTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-github-signal-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.cache = signals.SignalCache(self.base / "fleet-signals.json")

    def refresh(self, roots, client, now=NOW):
        return self.cache.refresh(roots, beakon_scan=lambda w: None, work_root=self.base, client=client, now=now)

    def test_the_client_is_never_called_with_a_search_path(self):
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        client = _Client(issues=7, prs=2)
        signal = signals.fetch_github(root, client)
        self.assertEqual([path for path, _ in client.calls], ["graphql"])
        self.assertFalse(any(path.startswith("search/") for path, _ in client.calls))
        body = client.calls[0][1]
        self.assertEqual(body["variables"], {"owner": "o", "name": "r"})
        for field in ("issues(states: OPEN)", "pullRequests(states: OPEN)", "latestRelease"):
            self.assertIn(field, body["query"])
        self.assertEqual((signal["open_issues"], signal["open_prs"], signal["latest_release"]["tag"], signal["error"]),
                         (7, 2, "v2.0.0", None))

    def test_three_roots_on_one_repository_make_one_fetch(self):
        roots = [_git_repo(self.base / name, origin) for name, origin in (
            ("a", "https://github.com/o/r.git"), ("b", "git@github.com:o/r.git"), ("c", "https://github.com/O/R"))]
        client = _Client(issues=5)
        fresh = self.refresh(roots, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual({fresh[str(root)]["github"]["open_issues"] for root in roots}, {5})
        other = _git_repo(self.base / "d", "https://github.com/o/other.git")
        client = _Client()
        self.refresh([*roots, other], client)
        self.assertEqual(len(client.calls), 2, "one fetch per repository")

    def test_a_rate_limited_answer_keeps_values_backs_off_and_renders_the_plain_line(self):
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        client = _Client(issues=9)
        good = self.refresh([root], client)[str(root)]["github"]
        client.raise_next = signals.GitHubRateLimited(RESET)
        after = self.refresh([root], client, now=NOW + 60)[str(root)]["github"]
        stamp = datetime.fromisoformat(good["fetched_at"]).astimezone().strftime("%H:%M")
        self.assertEqual(after["error"], f"GitHub rate limited; showing values from {stamp}")
        self.assertEqual({k: after[k] for k in ("open_issues", "open_prs", "latest_release", "fetched_at")},
                         {k: good[k] for k in ("open_issues", "open_prs", "latest_release", "fetched_at")})
        self.assertEqual(self.cache.backoff["o/r"], RESET)
        self.assertEqual(after["rate_limited_until"], datetime.fromtimestamp(RESET, tz=timezone.utc).isoformat())
        # Before the reset: no fetch at all for that repository.
        client.raise_next = None
        calls = len(client.calls)
        held = self.refresh([root], client, now=RESET - 1)[str(root)]["github"]
        self.assertEqual(len(client.calls), calls)
        self.assertEqual(held["open_issues"], 9)
        self.assertTrue(held["error"].startswith("GitHub rate limited; showing values from "))
        # At the reset: one fetch, fresh values, the line and the backoff gone.
        fresh = self.refresh([root], client, now=RESET)[str(root)]["github"]
        self.assertEqual(len(client.calls), calls + 1)
        self.assertIsNone(fresh["error"])
        self.assertNotIn("rate_limited_until", fresh)
        self.assertNotIn("o/r", self.cache.backoff)

    def test_a_restart_before_the_reset_keeps_the_backoff(self):
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        client = _Client(issues=9)
        self.refresh([root], client)
        client.raise_next = signals.GitHubRateLimited(RESET)
        self.refresh([root], client, now=NOW + 60)
        # Fleet restarts: a new cache reads the persisted file.
        self.cache = signals.SignalCache(self.base / "fleet-signals.json")
        self.assertEqual(self.cache.backoff, {"o/r": RESET})
        client.raise_next = None
        calls = len(client.calls)
        held = self.refresh([root], client, now=RESET - 1)[str(root)]["github"]
        self.assertEqual(len(client.calls), calls, "no fetch before the reset after a restart")
        self.assertEqual(held["open_issues"], 9)
        self.assertTrue(held["error"].startswith("GitHub rate limited; showing values from "))
        self.refresh([root], client, now=RESET)
        self.assertEqual(len(client.calls), calls + 1)
        self.assertEqual(signals.SignalCache(self.base / "fleet-signals.json").backoff, {})

    def test_a_rate_limit_with_no_reset_backs_off_a_default_interval(self):
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        client = _Client()
        client.raise_next = signals.GitHubRateLimited(None)
        first = self.refresh([root], client)[str(root)]["github"]
        self.assertEqual(first["error"], "GitHub rate limited; no values yet")
        self.assertEqual(self.cache.backoff["o/r"], NOW + signals.DEFAULT_RATE_LIMIT_BACKOFF_SECONDS)

    def test_gh_rate_limit_stderr_never_reaches_the_card(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        reset = int(RESET)
        gh.write_text("#!/bin/sh\n"
                      "if [ \"$1\" = auth ]; then echo tok; exit 0; fi\n"
                      "cat >/dev/null\n"
                      f"printf 'HTTP/2.0 200 OK\\nX-Ratelimit-Remaining: 0\\nX-Ratelimit-Reset: {reset}\\n\\n"
                      "{\"errors\": [{\"type\": \"RATE_LIMITED\", \"message\": \"API rate limit exceeded for user ID 1.\"}]}'\n"
                      "echo 'gh: API rate limit exceeded for user ID 1.' >&2\nexit 1\n")
        gh.chmod(0o755)
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            client = signals._gh_client()
            with self.assertRaises(signals.GitHubRateLimited) as caught:
                client("graphql", {"query": "q"})
            self.assertEqual(caught.exception.reset_at, float(reset))
            github = self.refresh([root], client)[str(root)]["github"]
        self.assertNotIn("user ID", github["error"])
        self.assertNotIn("gh:", github["error"])
        self.assertTrue(github["error"].startswith("GitHub rate limited"))
        self.assertEqual(self.cache.backoff["o/r"], float(reset))

    def test_a_search_style_403_with_no_requests_left_is_a_rate_limit(self):
        with self.assertRaises(signals.GitHubRateLimited) as caught:
            signals._signal_answer(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(RESET))},
                                   json.dumps({"message": "API rate limit exceeded"}), "GitHub HTTP 403")
        self.assertEqual(caught.exception.reset_at, RESET)

    def test_a_non_rate_limit_failure_behaves_as_today(self):
        root = _git_repo(self.base / "one", "https://github.com/o/r.git")
        client = _Client(issues=3)
        good = self.refresh([root], client)[str(root)]["github"]
        client.raise_next = signals.GitHubUnavailable("GitHub HTTP 503")
        after = self.refresh([root], client, now=NOW + 1)[str(root)]["github"]
        self.assertEqual(after["error"], "GitHub HTTP 503")
        self.assertEqual((after["open_issues"], after["fetched_at"]), (3, good["fetched_at"]))
        self.assertEqual(self.cache.backoff, {}, "no backoff for an ordinary failure")
        client.raise_next = None
        self.refresh([root], client, now=NOW + 2)
        self.assertEqual(len(client.calls), 3, "the next refresh fetches again")
        with self.assertRaises(signals.GitHubUnavailable) as caught:
            signals._signal_answer(500, {}, "{}", "gh: boom (HTTP 500)")
        self.assertNotIsInstance(caught.exception, signals.GitHubRateLimited)
        self.assertEqual(str(caught.exception), "gh: boom (HTTP 500)")


if __name__ == "__main__":
    unittest.main()
