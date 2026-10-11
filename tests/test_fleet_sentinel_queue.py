"""#453: Fleet shows what Sentinel has queued.

The signal refresh reads Sentinel's run state (and the queue stores its
queued items name) read-only, and /api/fleet carries `sentinel_queue`: per
repository the run holding the slot, the queued issues in Sentinel's
admission order and the items held at the rig. Every fixture lives under a
temporary directory; the real ~/.sentinel is never read here.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_fleet_signals as signals  # noqa: E402

REPO = "monzta1/project-handsoff"
STORE = "monzta1__project-handsoff"


def _job(job_id, created_at, number):
    return {"id": job_id, "created_at": created_at, "state": "queued",
            "payload": {"number": number, "repo": REPO, "trigger": "user"}}


def _queued(job_id, queued_at, trigger="user", store=STORE):
    return {"job": job_id, "key": f"sentinel-{job_id}", "queue_store": store, "queued_at": queued_at, "trigger": trigger}


class SentinelQueueTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-sentinel-queue-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = self.base / "sentinel" / "state.json"
        self.queue = self.base / "sentinel" / "queue"
        self.state.parent.mkdir(parents=True)
        self.cache = signals.SignalCache(self.base / "fleet-signals.json")

    def write_state(self, entry, repo=REPO):
        self.state.write_text(json.dumps({"schema": 1, "repos": {repo: entry}}, indent=1), encoding="utf-8")

    def write_store(self, jobs, store=STORE):
        path = self.queue / store / signals.SENTINEL_STORE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": {job["id"]: job for job in jobs}, "last_seq": len(jobs)}), encoding="utf-8")
        return path

    def refresh(self):
        self.cache.refresh([], github_fetch=lambda root, client: {}, beakon_scan=lambda w: None,
                           work_root=self.base, client=None, sentinel=(self.state, self.queue))
        return self.cache.sentinel_queue()

    def ordering_fixture(self):
        # The state's key order (number order) is not the admission order:
        # the store created #446 and #447 first, then #443 and #442 at the
        # same instant (the job id breaks the tie: job-a before job-b).
        self.write_state({"queued": {
            "442": _queued("job-b", "2026-10-10T23:46:27.18+00:00", "maintainer"),
            "443": _queued("job-a", "2026-10-10T23:46:27.19+00:00", "maintainer"),
            "446": _queued("job-d", "2026-10-10T23:33:23.94+00:00"),
            "447": _queued("job-c", "2026-10-10T23:33:24.20+00:00"),
            "450": _queued("job-gone", "2026-10-10T23:00:00.00+00:00"),
        }, "runs": {}, "waiting_rig": {}})
        self.write_store([
            _job("job-d", "2026-10-10T23:33:23.90+00:00", 446),
            _job("job-c", "2026-10-10T23:33:24.10+00:00", 447),
            _job("job-b", "2026-10-10T23:46:27.00+00:00", 442),
            _job("job-a", "2026-10-10T23:46:27.00+00:00", 443),
        ])

    def test_queued_items_follow_the_stores_admission_order_with_ties_broken_by_job_id(self):
        self.ordering_fixture()
        repo = self.refresh()["repos"][REPO]
        # #450's job is not in the store: it sorts last even though its
        # queued_at is the earliest of all.
        self.assertEqual([item["number"] for item in repo["queued"]], [446, 447, 443, 442, 450])
        first = repo["queued"][0]
        self.assertEqual(first, {"number": 446, "url": f"https://github.com/{REPO}/issues/446", "title": None,
                                 "trigger": "user", "queued_at": "2026-10-10T23:33:23.94+00:00"})
        self.assertEqual(repo["queued"][2]["trigger"], "maintainer")

    def test_items_missing_from_the_store_sort_by_queued_at_then_number(self):
        self.write_state({"queued": {
            "9": _queued("job-x", "2026-10-10T02:00:00+00:00"),
            "8": _queued("job-y", "2026-10-10T02:00:00+00:00"),
            "7": _queued("job-z", "2026-10-10T03:00:00+00:00"),
            "5": _queued("job-known", "2026-10-10T09:00:00+00:00"),
        }})
        self.write_store([_job("job-known", "2026-10-10T09:00:00+00:00", 5)])
        self.assertEqual([item["number"] for item in self.refresh()["repos"][REPO]["queued"]], [5, 8, 9, 7])

    def test_a_paused_holder_beside_history_is_the_holder(self):
        self.write_state({"runs": {
            "448": {"state": "released", "phase": "outcome", "admitted_at": "2026-10-11T00:00:00+00:00"},
            "449": {"state": "closed", "phase": "closed", "admitted_at": "2026-10-11T01:00:00+00:00"},
            "445": {"state": "paused", "phase": "outcome", "admitted_at": "2026-10-10T20:00:00+00:00", "title": "T445"},
            "219": {"state": "withdrawn", "phase": "withdrawn", "admitted_at": "2026-10-11T02:00:00+00:00"},
        }, "queued": {}, "waiting_rig": {}})
        repo = self.refresh()["repos"][REPO]
        self.assertEqual(repo["running"], {"number": 445, "state": "paused", "phase": "outcome",
                                           "url": f"https://github.com/{REPO}/issues/445", "title": "T445"})
        self.assertFalse(repo["slot_free"])

    def test_every_slot_holding_state_holds_the_slot(self):
        for state in signals.SENTINEL_SLOT_STATES:
            with self.subTest(state=state):
                self.write_state({"runs": {"12": {"state": state, "phase": "host"}}})
                self.cache = signals.SignalCache(self.base / "fleet-signals.json")
                self.assertEqual(self.refresh()["repos"][REPO]["running"]["number"], 12)

    def test_no_holder_reads_slot_free_with_rig_held_items(self):
        self.write_state({"runs": {"448": {"state": "released", "phase": "outcome"},
                                   "449": {"state": "closed", "phase": "closed"}},
                          "queued": {},
                          "waiting_rig": {"31": {"reason": "device label", "held_at": "2026-10-10T01:00:00+00:00"},
                                          "30": {"reason": "device label", "held_at": "2026-10-10T00:00:00+00:00"}}})
        repo = self.refresh()["repos"][REPO]
        self.assertIsNone(repo["running"])
        self.assertTrue(repo["slot_free"])
        self.assertEqual(repo["queued"], [])
        self.assertEqual([item["number"] for item in repo["rig"]], [30, 31])
        self.assertEqual(repo["rig"][0], {"number": 30, "url": f"https://github.com/{REPO}/issues/30", "title": None,
                                          "reason": "device label", "held_at": "2026-10-10T00:00:00+00:00"})

    def test_titles_come_from_the_issue_cache_when_present(self):
        self.ordering_fixture()
        state = json.loads(self.state.read_text())
        state["repos"][REPO]["runs"] = {"445": {"state": "running", "phase": "host", "title": "from sentinel"}}
        state["repos"][REPO]["waiting_rig"] = {"31": {"reason": "device"}}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.refresh()
        issues_file = self.base / "fleet-issues.json"
        issues_file.write_text(json.dumps({"schema": 1, "projects": {str(self.base / "p"): {
            "repo": "Monzta1/Project-Handsoff",  # grouped case-insensitively, as #160 does
            "issues": [{"number": 446, "title": "Queued <b>title</b>"}, {"number": 445, "title": "Cached run title"},
                       {"number": 31, "title": "Rig title"}]}}}), encoding="utf-8")
        issues = signals.IssueCache(issues_file)
        repo = fleet.sentinel_queue_block(self.cache, issues)["repos"][REPO]
        by_number = {item["number"]: item["title"] for item in repo["queued"]}
        self.assertEqual(by_number[446], "Queued <b>title</b>")
        self.assertIsNone(by_number[447])  # not cached: the page shows a placeholder
        self.assertEqual(repo["running"]["title"], "Cached run title")
        self.assertEqual(repo["rig"][0]["title"], "Rig title")
        # The cache's own view is a copy: titling the payload never edits it.
        self.assertIsNone(self.cache.sentinel_queue()["repos"][REPO]["queued"][0]["title"])

    def test_missing_and_malformed_files_give_an_empty_view_with_a_note(self):
        view = self.refresh()
        self.assertEqual(view["repos"], {})
        self.assertEqual(view["note"], "no Sentinel state on this machine")
        for text in ("{not json", json.dumps(["a list"]), json.dumps({"repos": "nope"})):
            with self.subTest(text=text):
                self.state.write_text(text, encoding="utf-8")
                view = self.refresh()
                self.assertEqual(view["repos"], {})
                self.assertEqual(view["note"], "Sentinel state malformed")
        # A malformed repository entry or item is skipped, never fatal.
        self.state.write_text(json.dumps({"repos": {REPO: {"runs": [], "queued": {"x": {}, "4": "bad", "5": {}},
                                                           "waiting_rig": None}, "not a repo": {}}}), encoding="utf-8")
        view = self.refresh()
        self.assertEqual(list(view["repos"]), [REPO])
        self.assertEqual([item["number"] for item in view["repos"][REPO]["queued"]], [5])

    def test_an_unreadable_file_gives_an_empty_view_and_the_fleet_build_still_runs(self):
        self.write_state({"queued": {}})
        with mock.patch.object(signals, "_read_json_file", side_effect=PermissionError("denied")):
            view = self.refresh()
        self.assertEqual(view["repos"], {})
        self.assertEqual(view["note"], "Sentinel state unreadable (PermissionError)")
        self.state.write_text("{broken", encoding="utf-8")
        self.refresh()
        payload = fleet.build_fleet(self.base / "fleet.json", signals=self.cache,
                                    issues=signals.IssueCache(self.base / "fleet-issues.json"))
        self.assertEqual(payload["sentinel_queue"]["repos"], {})
        self.assertEqual(payload["sentinel_queue"]["note"], "Sentinel state malformed")
        self.assertEqual(payload["projects"], [])

    def test_the_fleet_payload_carries_the_view(self):
        self.ordering_fixture()
        self.refresh()
        payload = fleet.build_fleet(self.base / "fleet.json", signals=self.cache,
                                    issues=signals.IssueCache(self.base / "fleet-issues.json"))
        self.assertEqual([item["number"] for item in payload["sentinel_queue"]["repos"][REPO]["queued"]],
                         [446, 447, 443, 442, 450])

    def test_the_state_file_and_stores_are_never_written(self):
        self.ordering_fixture()
        store = self.queue / STORE / signals.SENTINEL_STORE_FILE

        def fingerprint():
            files = sorted(str(path.relative_to(self.state.parent)) for path in self.state.parent.rglob("*"))
            return files, [(hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                           for path in (self.state, store)]
        before = fingerprint()
        for _ in range(3):
            self.refresh()
        self.assertEqual(fingerprint(), before)

    def test_the_refresh_does_not_reread_an_unchanged_file(self):
        self.ordering_fixture()
        with mock.patch.object(signals, "_read_json_file", wraps=signals._read_json_file) as reads:
            self.refresh()
            first = reads.call_count
            self.assertEqual(first, 2)  # the state file and the one store it names
            self.refresh()
            self.refresh()
            self.assertEqual(reads.call_count, first)
            # A changed state file is read again on the next refresh.
            state = json.loads(self.state.read_text())
            del state["repos"][REPO]["queued"]["450"]
            self.state.write_text(json.dumps(state), encoding="utf-8")
            view = self.refresh()
            self.assertGreater(reads.call_count, first)
            self.assertEqual([item["number"] for item in view["repos"][REPO]["queued"]], [446, 447, 443, 442])
            # So is a changed store, with the state file untouched.
            settled = reads.call_count
            self.write_store([_job("job-a", "2026-10-10T20:00:00+00:00", 443), _job("job-b", "2026-10-10T21:00:00+00:00", 442),
                              _job("job-c", "2026-10-10T22:00:00+00:00", 447), _job("job-d", "2026-10-10T23:00:00+00:00", 446)])
            view = self.refresh()
            self.assertGreater(reads.call_count, settled)
            self.assertEqual([item["number"] for item in view["repos"][REPO]["queued"]], [443, 442, 447, 446])


class SentinelPathTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="handsoff-sentinel-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(signals.Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_environment_override_wins(self):
        with mock.patch.dict(os.environ, {"SENTINEL_STATE_FILE": str(self.home / "elsewhere" / "s.json")}):
            self.assertEqual(signals.sentinel_paths(), (self.home / "elsewhere" / "s.json", self.home / "elsewhere" / "queue"))
            self.assertEqual(signals.sentinel_state_path(), self.home / "elsewhere" / "s.json")

    def test_the_config_names_the_state_file_then_the_default_applies(self):
        env = {key: value for key, value in os.environ.items() if key != "SENTINEL_STATE_FILE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(signals.sentinel_paths(), (self.home / ".sentinel" / "state.json", self.home / ".sentinel" / "queue"))
            config = self.home / ".config" / "sentinel" / "sentinel.toml"
            config.parent.mkdir(parents=True)
            config.write_text(f'[sentinel]\nstate_file = "{self.home}/s/state.json"\nqueue_dir = "{self.home}/q"\n', encoding="utf-8")
            self.assertEqual(signals.sentinel_paths(), (self.home / "s" / "state.json", self.home / "q"))
            config.write_text('[sentinel]\nrepos = []\n', encoding="utf-8")
            self.assertEqual(signals.sentinel_state_path(), self.home / ".sentinel" / "state.json")


if __name__ == "__main__":
    unittest.main()
