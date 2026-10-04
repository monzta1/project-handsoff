"""#285: the durable Fleet job queue.

Durability is tested across real processes: one python process submits, a
fresh one loads. Expiry is tested with an injected clock, never a sleep, and
the crash between journal and store is a real `os._exit` in a child process
between the journal fsync and the store write.
"""
import hmac
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_fleet import _FleetFixture
from tests.test_handsoff_supervisor import BIN, ROOT

sys.path.insert(0, str(BIN))

import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_queue as hq  # noqa: E402


class Clock:
    def __init__(self):
        self.moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


class QueueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.clock = Clock()
        self.queue = hq.Queue(self.base, now=self.clock, host="host-a")

    def fresh(self, **kwargs):
        """A second Queue object on the same directory, as a new caller sees it."""
        return hq.Queue(self.base, now=self.clock, host="host-a", **kwargs)

    def child(self, body):
        """Run python in a separate process against the same store."""
        env = dict(os.environ, PYTHONPATH=str(BIN),
                   HANDSOFF_FLEET_REGISTRY=str(self.base / "projects.json"))
        code = "import json, os\nfrom handsoff_queue import *\nq = Queue()\n" + textwrap.dedent(body)
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)

    def segment(self, number=1):
        return self.base / hq.JOURNAL_DIR / f"{number:06d}.jsonl"

    def raw_lines(self):
        lines = []
        for path in sorted((self.base / hq.JOURNAL_DIR).glob("*.jsonl")):
            lines.extend(json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines())
        return lines


class DurableSubmitTest(QueueCase):
    """REQ-001."""

    def test_submit_in_one_process_is_seen_by_a_fresh_process(self):
        submitted = self.child('print(q.submit("build-1", {"n": 1}))')
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        job_id = submitted.stdout.strip()
        self.assertRegex(job_id, hq.JOB_ID_PATTERN)
        loaded = self.child(f'print(json.dumps(q.load()["jobs"].get("{job_id}")))')
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        job = json.loads(loaded.stdout)
        self.assertEqual((job["key"], job["state"], job["payload"]), ("build-1", "queued", {"n": 1}))

    def test_same_idempotency_key_returns_the_existing_id_and_creates_nothing(self):
        first = self.child('print(q.submit("build-1", {"n": 1}))')
        second = self.child('print(q.submit("build-1", {"n": 2}))')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout.strip(), second.stdout.strip())
        loaded = self.child('print(json.dumps(q.load()))')
        store = json.loads(loaded.stdout)
        self.assertEqual(len(store["jobs"]), 1)
        self.assertEqual(store["jobs"][first.stdout.strip()]["payload"], {"n": 1})
        self.assertEqual(len(self.raw_lines()), 1)


class LeaseTest(QueueCase):
    """REQ-002."""

    def test_heartbeat_extends_exclusivity_until_the_new_expiry(self):
        job_id = self.queue.submit("j", {})
        lease = self.queue.claim("worker-a")
        self.assertEqual((lease["id"], lease["epoch"]), (job_id, 1))
        self.clock.advance(50)
        self.fresh().heartbeat(job_id, "worker-a", 1)
        self.clock.advance(20)  # past the original expiry, before the renewed one
        self.assertIsNone(self.fresh().claim("worker-b"))
        self.clock.advance(39)
        self.assertIsNone(self.fresh().claim("worker-b"))
        self.clock.advance(1)  # exactly the renewed expiry
        regranted = self.fresh().claim("worker-b")
        self.assertEqual((regranted["id"], regranted["epoch"]), (job_id, 2))

    def test_current_owner_succeed_persists_in_a_fresh_process(self):
        job_id = self.queue.submit("j", {})
        lease = self.queue.claim("worker-a")
        self.clock.advance(30)
        self.queue.heartbeat(job_id, "worker-a", lease["epoch"])
        self.queue.succeed(job_id, "worker-a", lease["epoch"])
        loaded = self.child(f'print(q.load()["jobs"]["{job_id}"]["state"])')
        self.assertEqual(loaded.stdout.strip(), "succeeded", loaded.stderr)

    def test_expired_owner_is_refused_before_anyone_reclaims(self):
        job_id = self.queue.submit("j", {})
        self.queue.claim("worker-a")
        before = len(self.raw_lines())
        self.clock.advance(60)
        for call in (self.queue.heartbeat, self.queue.succeed, self.queue.fail):
            with self.subTest(call=call.__name__), self.assertRaises(hq.LeaseExpired):
                call(job_id, "worker-a", 1)
        job = self.fresh().job(job_id)
        self.assertEqual((job["state"], job["lease"]["worker"], job["epoch"]), ("leased", "worker-a", 1))
        self.assertEqual(len(self.raw_lines()), before)

    def test_reclaim_after_expiry_bumps_the_epoch_and_fences_old_epoch_calls(self):
        job_id = self.queue.submit("j", {})
        self.queue.claim("worker-a")
        self.clock.advance(61)
        regranted = self.fresh().claim("worker-b")
        self.assertEqual(regranted["epoch"], 2)
        for worker in ("worker-a", "worker-b"):
            for call in (self.queue.heartbeat, self.queue.succeed, self.queue.fail):
                with self.subTest(worker=worker, call=call.__name__), self.assertRaises(hq.StaleLease):
                    call(job_id, worker, 1)
        self.queue.succeed(job_id, "worker-b", 2)
        self.assertEqual(self.fresh().job(job_id)["state"], "succeeded")

    def test_a_live_lease_refuses_another_worker(self):
        job_id = self.queue.submit("j", {})
        self.queue.claim("worker-a")
        self.assertIsNone(self.queue.claim("worker-b"))
        with self.assertRaises(hq.StaleLease):
            self.queue.heartbeat(job_id, "worker-b", 1)

    def test_concurrent_claims_from_separate_processes_grant_one_lease(self):
        job_id = self.child('print(q.submit("j", {}))').stdout.strip()
        env = dict(os.environ, PYTHONPATH=str(BIN), HANDSOFF_FLEET_REGISTRY=str(self.base / "projects.json"))
        procs = [subprocess.Popen(
            [sys.executable, "-c", f"from handsoff_queue import *\nj = Queue().claim('w{i}')\n"
                                   "print(j['epoch'] if j else '-')"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
        outputs = [proc.communicate(timeout=60)[0].strip() for proc in procs]
        self.assertEqual(sorted(outputs), ["-"] * 5 + ["1"], outputs)
        self.assertEqual(hq.Queue(self.base).job(job_id)["epoch"], 1)


class RetryCancelJournalTest(QueueCase):
    """REQ-003."""

    def test_dead_worker_leaves_the_job_reclaimable_with_attempts_incremented(self):
        job_id = self.queue.submit("j", {})
        self.queue.claim("worker-a")  # worker-a dies without acknowledging
        self.assertIsNone(self.queue.claim("worker-b"))
        self.clock.advance(60)
        regranted = self.fresh().claim("worker-b")
        self.assertEqual((regranted["id"], regranted["attempts"], regranted["epoch"]), (job_id, 1, 2))
        expire = [line for line in self.raw_lines() if line["op"] == "expire"]
        self.assertEqual(len(expire), 1)
        self.assertEqual((expire[0]["worker"], expire[0]["epoch"]), ("worker-a", 1))

    def test_expiry_at_the_retry_limit_ends_failed(self):
        job_id = self.queue.submit("j", {})
        for worker in ("w1", "w2", "w3"):
            self.assertIsNotNone(self.queue.claim(worker))
            self.clock.advance(60)
        self.assertIsNone(self.queue.claim("w4"))
        job = self.fresh().job(job_id)
        self.assertEqual((job["state"], job["attempts"]), ("failed", 3))

    def test_owner_fail_requeues_while_attempts_remain_then_ends_failed(self):
        job_id = self.queue.submit("j", {})
        for expected_attempts, expected_state in ((1, "queued"), (2, "queued"), (3, "failed")):
            lease = self.queue.claim("worker-a")
            self.assertIsNotNone(lease)
            self.queue.fail(job_id, "worker-a", lease["epoch"])
            job = self.fresh().job(job_id)
            self.assertEqual((job["attempts"], job["state"], job["lease"]), (expected_attempts, expected_state, None))
        self.assertIsNone(self.queue.claim("worker-a"))
        self.assertEqual(hq.DEFAULT_MAX_ATTEMPTS, 3)

    def test_cancel_queued_and_leased_jobs_then_completion_is_refused(self):
        queued = self.queue.submit("queued", {})
        self.queue.cancel(queued)
        self.assertEqual(self.fresh().job(queued)["state"], "cancelled")
        self.assertIsNone(self.queue.claim("worker-a"))
        leased = self.queue.submit("leased", {})
        lease = self.queue.claim("worker-a")
        self.queue.cancel(leased)
        self.assertEqual(self.fresh().job(leased)["state"], "cancelled")
        for call in (self.queue.heartbeat, self.queue.succeed, self.queue.fail):
            with self.subTest(call=call.__name__), self.assertRaises(hq.LeaseRefused):
                call(leased, "worker-a", lease["epoch"])
        self.assertEqual(self.fresh().job(leased)["state"], "cancelled")

    def test_every_transition_and_heartbeat_is_journaled_and_replay_reproduces_the_store(self):
        first = self.queue.submit("a", {})
        self.clock.advance(1)
        second = self.queue.submit("b", {})
        self.queue.claim("w1")
        self.queue.heartbeat(first, "w1", 1)
        self.queue.fail(first, "w1", 1)
        self.queue.claim("w2")  # second is created later, so first is reclaimed first
        self.clock.advance(60)
        self.queue.claim("w3")  # first expires and is regranted
        self.queue.succeed(first, "w3", 3)
        self.queue.cancel(second)
        lines = self.raw_lines()
        self.assertEqual([line["op"] for line in lines],
                         ["submit", "submit", "claim", "heartbeat", "fail", "claim", "expire", "claim",
                          "succeed", "cancel"])
        self.assertEqual([line["seq"] for line in lines], list(range(1, len(lines) + 1)))
        for line in lines:
            self.assertTrue(line["job"] and line["at"])
            self.assertIsInstance(line["epoch"], int)
            if line["op"] not in ("submit", "cancel"):
                self.assertRegex(line["worker"], hq.WORKER_PATTERN)
        store = self.fresh().load()
        self.assertEqual(store["last_seq"], len(lines))
        self.assertEqual(self.fresh().replay(from_start=True), store)
        self.assertEqual(store["jobs"][first]["state"], "succeeded")

    def test_torn_tail_beyond_the_store_is_discarded_and_kept_aside(self):
        self.queue.submit("a", {"n": 1})
        good = self.segment().read_bytes()
        partial = b'{"at":"2026-01-01T00:00:00+00:00","epoch":0,"fields":{"key":"b"},"seq":2,"job":"jo'
        self.segment().write_bytes(good + partial)
        store = self.fresh().load()
        self.assertEqual(store["last_seq"], 1)
        self.assertEqual(self.segment().read_bytes(), good)
        self.assertEqual((self.base / hq.JOURNAL_DIR / "torn-2.bin").read_bytes(), partial)
        self.fresh().submit("b", {})
        self.assertEqual(len(self.fresh().journal()), 2)

    def test_unparseable_torn_tail_takes_the_next_seq_and_is_discarded(self):
        self.queue.submit("a", {})
        good = self.segment().read_bytes()
        self.segment().write_bytes(good + b"\x00\xffgarbage")
        self.assertEqual(self.fresh().load()["last_seq"], 1)
        self.assertEqual(self.segment().read_bytes(), good)
        self.assertTrue((self.base / hq.JOURNAL_DIR / "torn-2.bin").exists())

    def test_corrupt_last_acknowledged_record_is_refused_not_truncated(self):
        self.queue.submit("a", {"n": 1})
        self.queue.submit("b", {"n": 1})
        data = self.segment().read_bytes()
        cut = data.rstrip(b"\n").rfind(b"\n") + 1
        tampered = data[:cut] + data[cut:].replace(b'"n":1', b'"n":2')
        self.assertNotEqual(tampered, data)
        self.segment().write_bytes(tampered)
        with self.assertRaises(hq.QueueCorrupt) as caught:
            self.fresh().load()
        self.assertIn("segment 000001.jsonl line 2 seq 2", str(caught.exception))
        self.assertIn("hash", str(caught.exception))
        self.assertEqual(self.segment().read_bytes(), tampered)

    def test_a_forged_tail_seq_cannot_truncate_an_acknowledged_record(self):
        # implementation review attempt 1: the tail's own (unverified) seq
        # licensed truncation; raising it past the store's last seq and
        # removing the newline must still be refused, not truncated
        self.queue.submit("a", {"n": 1})
        self.queue.submit("b", {"n": 1})
        data = self.segment().read_bytes()
        cut = data.rstrip(b"\n").rfind(b"\n") + 1
        forged = data[:cut] + data[cut:].rstrip(b"\n").replace(b'"seq":2', b'"seq":3')
        self.assertNotEqual(forged, data)
        self.segment().write_bytes(forged)
        with self.assertRaises(hq.QueueCorrupt):
            self.fresh().load()
        self.assertEqual(self.segment().read_bytes(), forged, "nothing truncated")

    def test_committed_record_without_its_newline_is_refused_not_truncated(self):
        self.queue.submit("a", {})
        self.queue.submit("b", {})
        stripped = self.segment().read_bytes().rstrip(b"\n")
        self.segment().write_bytes(stripped)
        with self.assertRaises(hq.QueueCorrupt) as caught:
            self.fresh().load()
        self.assertIn("seq 2", str(caught.exception))
        self.assertEqual(self.segment().read_bytes(), stripped)
        self.assertEqual(list((self.base / hq.JOURNAL_DIR).glob("torn-*")), [])

    def test_crash_after_journal_append_before_store_write_is_recovered(self):
        self.assertEqual(self.child('q.submit("a", {})').returncode, 0)
        crashed = self.child('Queue(after_journal=lambda: os._exit(7)).submit("b", {})')
        self.assertEqual(crashed.returncode, 7, crashed.stderr)
        raw_store = json.loads((self.base / hq.STORE_FILE).read_text(encoding="utf-8"))
        self.assertEqual((raw_store["last_seq"], len(raw_store["jobs"])), (1, 1))
        loaded = self.child('print(json.dumps(sorted(j["key"] for j in q.load()["jobs"].values())))')
        self.assertEqual(json.loads(loaded.stdout), ["a", "b"], loaded.stderr)
        raw_store = json.loads((self.base / hq.STORE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(raw_store["last_seq"], 2)

    def test_rotation_keeps_every_record_and_replays_from_the_checkpoint(self):
        queue = self.fresh(segment_bytes=700)
        job_id = queue.submit("a", {})
        queue.claim("w1", lease_seconds=3600)
        for _ in range(12):
            self.clock.advance(1)
            queue.heartbeat(job_id, "w1", 1, lease_seconds=3600)
        queue.succeed(job_id, "w1", 1)
        segments = sorted((self.base / hq.JOURNAL_DIR).glob("*.jsonl"))
        self.assertGreater(len(segments), 2)
        lines = self.raw_lines()
        store = self.fresh().load()
        self.assertEqual([line["seq"] for line in lines], list(range(1, store["last_seq"] + 1)))
        self.assertEqual(sum(line["op"] == "heartbeat" for line in lines), 12)
        checkpoints = [line for line in lines if line["op"] == "checkpoint"]
        self.assertEqual(len(checkpoints), len(segments) - 1)
        self.assertEqual(len(list((self.base / hq.JOURNAL_DIR).glob("checkpoint-*.json"))), len(checkpoints))
        latest = self.fresh()._latest_verified_checkpoint(lines)
        self.assertEqual(latest["seq"], checkpoints[-1]["seq"])
        self.assertEqual(self.fresh().replay(), store)
        self.assertEqual(self.fresh().replay(from_start=True), store)

    def test_corruption_in_a_closed_segment_is_refused_by_name(self):
        queue = self.fresh(segment_bytes=400)
        job_id = queue.submit("a", {})
        queue.claim("w1", lease_seconds=3600)
        for _ in range(6):
            queue.heartbeat(job_id, "w1", 1, lease_seconds=3600)
        self.assertTrue(self.segment(2).exists())
        data = self.segment(1).read_bytes()
        tampered = data.replace(b'"key":"a"', b'"key":"z"', 1)
        self.segment(1).write_bytes(tampered)
        with self.assertRaises(hq.QueueCorrupt) as caught:
            self.fresh().load()
        self.assertIn("segment 000001.jsonl line 1 seq 1", str(caught.exception))


class LocalOnlyTest(QueueCase):
    """REQ-005, the queue half."""

    def test_submit_naming_another_host_is_refused(self):
        with self.assertRaises(hq.QueueError) as caught:
            self.queue.submit("j", {}, host="host-b")
        self.assertIn("no remote transport", str(caught.exception))
        self.assertEqual(self.fresh().load()["jobs"], {})
        self.assertEqual(self.raw_lines(), [])
        job_id = self.queue.submit("j", {}, host="host-a")
        self.assertEqual(self.fresh().job(job_id)["host"], "host-a")

    def test_reference_labels_local_only_capabilities_and_multi_host_prerequisites(self):
        """REQ-005, the documentation half."""
        text = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")

        def section(heading):
            self.assertIn(heading, text)
            body = text.split(heading, 1)[1]
            return re.split(r"\n#{2,4} ", body, maxsplit=1)[0]

        local = section("#### Local-only capabilities (#285)")
        self.assertIn("single-host", local)
        for capability in ("**Job execution.**", "no remote transport", "**The queue store and journal.**",
                           "**Run control.**", "**The registry and signal caches.**"):
            self.assertIn(capability, local)
        prerequisites = section("#### Multi-host prerequisites (#285)")
        for item in ("1. **Durable queue.**", "2. **Leases.**", "3. **Fencing.**",
                     "4. **Authenticated transport.**"):
            self.assertIn(item, prerequisites)
        self.assertIn("cross-host execution fails closed", prerequisites)


class FleetTokenTest(_FleetFixture):
    """REQ-004: the Bearer token on fleet serve, tested against a live server."""

    TOKEN = "s3cret-fleet-token"
    GET_ROUTES = ("/api/fleet", "/api/metrics", "/api/events", "/api/no-such-route")
    POST_ROUTES = ("/api/release-port", "/api/close-run", "/api/reopen-run", "/api/forget")

    def start(self, token):
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry, signals_interval=3600,
                                   issues_interval=3600, token=token)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()

        def stop():
            server.stopping = True
            server.signals_thread.stop_event.set()
            server.issues_thread.stop_event.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)
        self.port = server.server_address[1]
        return server

    def request(self, method, path, *, authorization=None, origin=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        if origin is not None:
            headers["Origin"] = origin
        payload = None if body is None else json.dumps(body)
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            if response.getheader("Content-Type") == "text/event-stream":
                return response.status, response.readline().decode()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def same_origin(self):
        return f"http://127.0.0.1:{self.port}"

    def orphaned_project(self):
        """A registered project whose root is gone: /api/forget would remove it."""
        root = self.project("gone").resolve()
        fleet.register_project(root, self.registry)
        shutil.rmtree(root)
        card = next(item for item in fleet.build_fleet(self.registry)["projects"] if item["root"] == str(root))
        self.assertEqual(card["state"], "orphaned")
        return root, {"root": str(root), "binding": card["binding"], "confirm": True, "reason": "test"}

    def test_serve_refuses_a_non_loopback_bind_without_a_token(self):
        bound = []

        class Bound(Exception):
            pass

        def fake_server(address, registry, token=None):
            bound.append((address, token))
            raise Bound

        # The fake server is in place for every call, so a refusal that is
        # removed shows up as a bind here rather than a real listening socket.
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(fleet, "FleetServer", fake_server):
            os.environ.pop(fleet.FLEET_TOKEN_ENV, None)
            with self.assertRaises(lib.HandsoffError) as caught:
                fleet.serve("0.0.0.0", 0, open_browser=False, registry=self.registry)
            self.assertIn("access token", str(caught.exception))
            os.environ[fleet.FLEET_TOKEN_ENV] = "   "
            with self.assertRaises(lib.HandsoffError):
                fleet.serve("0.0.0.0", 0, open_browser=False, registry=self.registry)
            self.assertEqual(bound, [])
            # loopback without a token: unchanged, no token on the server
            os.environ.pop(fleet.FLEET_TOKEN_ENV, None)
            with self.assertRaises(Bound):
                fleet.serve("127.0.0.1", 0, open_browser=False, registry=self.registry)
            # a token from the environment lifts the refusal
            os.environ[fleet.FLEET_TOKEN_ENV] = self.TOKEN
            with self.assertRaises(Bound):
                fleet.serve("0.0.0.0", 0, open_browser=False, registry=self.registry)
            # a token file wins over the environment
            token_file = self.base / "fleet.token"
            token_file.write_text("from-file\n", encoding="utf-8")
            with self.assertRaises(Bound):
                fleet.serve("0.0.0.0", 0, open_browser=False, registry=self.registry, token_file=token_file)
            token_file.write_text("\n", encoding="utf-8")
            with self.assertRaises(lib.HandsoffError):
                fleet.serve("0.0.0.0", 0, open_browser=False, registry=self.registry, token_file=token_file)
        self.assertEqual(bound, [(("127.0.0.1", 0), None), (("0.0.0.0", 0), self.TOKEN),
                                 (("0.0.0.0", 0), "from-file")])

    def test_every_route_answers_401_without_or_with_a_wrong_token_before_origin_or_mutation(self):
        root, forget_body = self.orphaned_project()
        before = fleet.load_registry(self.registry)
        self.start(self.TOKEN)
        refused = (None, "Bearer wrong-token", f"Bearer {self.TOKEN}x", "Bearer ", f"Basic {self.TOKEN}", self.TOKEN)
        for authorization in refused:
            for path in self.GET_ROUTES:
                with self.subTest(method="GET", path=path, authorization=authorization):
                    status, body = self.request("GET", path, authorization=authorization,
                                                origin="http://evil.example")
                    self.assertEqual(status, 401)
                    self.assertEqual(body, {"error": "Fleet access token required"})
            for path in self.POST_ROUTES:
                with self.subTest(method="POST", path=path, authorization=authorization):
                    # a foreign Origin would be 403 and this valid body would forget the
                    # project; 401 comes first and nothing changes
                    status, body = self.request("POST", path, authorization=authorization,
                                                origin="http://evil.example", body=forget_body)
                    self.assertEqual(status, 401)
                    status, body = self.request("POST", path, authorization=authorization,
                                                origin=self.same_origin(), body=forget_body)
                    self.assertEqual(status, 401)
                    self.assertEqual(body, {"error": "Fleet access token required"})
        self.assertEqual(fleet.load_registry(self.registry), before)
        self.assertEqual([item["root"] for item in before], [str(root)])
        log = fleet.fleet_log_path(self.registry)
        self.assertNotIn("fleet_entry_forgotten", log.read_text() if log.exists() else "")

    def test_the_right_token_reaches_normal_handling_on_every_route(self):
        root, forget_body = self.orphaned_project()
        self.start(self.TOKEN)
        right = f"Bearer {self.TOKEN}"
        with mock.patch.object(fleet.hmac, "compare_digest", wraps=hmac.compare_digest) as compared:
            status, body = self.request("GET", "/api/fleet", authorization=right)
        self.assertEqual(status, 200)
        self.assertEqual([item["root"] for item in body["projects"]], [str(root)])
        self.assertTrue(compared.called)  # constant-time comparison
        status, body = self.request("GET", "/api/metrics", authorization=right)
        self.assertEqual(status, 200)
        self.assertIn("projects", body)
        status, line = self.request("GET", "/api/events", authorization=right)
        self.assertEqual((status, line), (200, "event: fleet\n"))
        status, body = self.request("GET", "/api/no-such-route", authorization=right)
        self.assertEqual((status, body), (404, {"error": "Not found"}))
        unknown = {"root": str(self.base / "never-registered"), "binding": "x", "confirm": True, "reason": "test"}
        for path in self.POST_ROUTES:
            with self.subTest(path=path):
                status, body = self.request("POST", path, authorization=right, origin="http://evil.example",
                                            body=unknown)
                self.assertEqual((status, body), (403, {"error": "Same-origin Fleet request required"}))
                status, body = self.request("POST", path, authorization=right, origin=self.same_origin(),
                                            body=unknown)
                self.assertEqual(status, 400)
                self.assertIn("project is not registered", body["error"])
        status, body = self.request("POST", "/api/forget", authorization=right, origin=self.same_origin(),
                                    body=forget_body)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["forgotten"], str(root))
        self.assertEqual(fleet.load_registry(self.registry), [])

    def test_loopback_without_a_token_behaves_as_before(self):
        self.start(None)
        status, body = self.request("GET", "/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(body["projects"], [])
        status, body = self.request("GET", "/api/fleet", authorization="Bearer anything")
        self.assertEqual(status, 200)
        status, line = self.request("GET", "/api/events")
        self.assertEqual((status, line), (200, "event: fleet\n"))
        self.assertEqual(self.request("GET", "/api/no-such-route")[0], 404)
        unknown = {"root": str(self.base / "never-registered"), "binding": "x", "confirm": True}
        for path in self.POST_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, origin="http://evil.example", body=unknown)[0], 403)
                status, body = self.request("POST", path, origin=self.same_origin(), body=unknown)
                self.assertEqual(status, 400)
                self.assertIn("project is not registered", body["error"])


if __name__ == "__main__":
    unittest.main()
