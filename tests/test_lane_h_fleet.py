"""Lane H (#207, #203): a lane whose worktree is gone leaves the fleet
register on its own, and a managed reviewer is never blamed for the
runtime's own temp files."""
import http.client
import json
import shutil
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_fleet import _FleetFixture
from tests.test_handsoff_supervisor import BIN, ROOT

sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class ForgottenRootTests(_FleetFixture):
    def _log(self):
        path = fleet.fleet_log_path(self.registry)
        return path.read_text() if path.exists() else ""

    def _states(self):
        return {item["root"]: item["state"] for item in fleet.build_fleet(self.registry)["projects"]}

    def test_a_removed_root_is_orphaned_once_then_gone_with_the_log_line(self):
        """[#207] acceptance 1: ORPHANED on the first pass, gone from the
        register and the page on the second, one fleet_entry_forgotten line."""
        root = self.project("gone").resolve()
        fleet.register_project(root, self.registry)
        fleet.note_registry_state(root, {207}, "open", path=self.registry)  # what init with a ticket writes
        shutil.rmtree(root)
        first = self._states()
        self.assertEqual(first, {str(root): "orphaned"})
        entry = fleet.load_registry(self.registry)[0]
        self.assertIsInstance(entry["missing_since"], str)
        self.assertEqual(self._log(), "")
        # the next pass, one collector interval later
        since_at = datetime.fromisoformat(entry["missing_since"])
        # one second short of the interval: still ORPHANED
        self.assertEqual(fleet.forget_missing_roots(
            self.registry, now=since_at + timedelta(seconds=fleet.FORGET_AFTER_SECONDS - 1)), [])
        self.assertEqual(self._states(), {str(root): "orphaned"})
        # exactly one interval after the mark: this pass removes it
        later = since_at + timedelta(seconds=fleet.FORGET_AFTER_SECONDS)
        forgotten = fleet.forget_missing_roots(self.registry, now=later)
        self.assertEqual([item["root"] for item in forgotten], [str(root)])
        self.assertTrue(forgotten[0]["run_never_closed"])  # init'd, never closed
        self.assertEqual(fleet.load_registry(self.registry), [])
        self.assertEqual(self._states(), {})
        lines = [line for line in self._log().splitlines() if "fleet_entry_forgotten" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"root={root}", lines[0])
        self.assertIn("last_state=open", lines[0])
        self.assertIn("run_never_closed=true", lines[0])
        self.assertIn(f"missing_since={entry['missing_since']}", lines[0])

    def test_a_closed_run_whose_root_is_gone_is_forgotten_without_the_never_closed_flag(self):
        root = self.project("closed").resolve()
        fleet.register_project(root, self.registry)
        fleet.note_registry_state(root, None, "closed", path=self.registry)  # what run-close writes
        shutil.rmtree(root)
        fleet.forget_missing_roots(self.registry)
        later = datetime.now(timezone.utc) + timedelta(seconds=fleet.FORGET_AFTER_SECONDS + 1)
        forgotten = fleet.forget_missing_roots(self.registry, now=later)
        self.assertEqual(len(forgotten), 1)
        self.assertFalse(forgotten[0]["run_never_closed"])
        line = self._log().splitlines()[-1]
        self.assertIn("last_state=closed", line)
        self.assertNotIn("run_never_closed", line)

    def test_a_transient_absence_forgets_nothing(self):
        """[#207] acceptance 2: the root is back before the next pass."""
        root = self.project("blip").resolve()
        fleet.register_project(root, self.registry)
        parked = self.base.resolve() / "parked"
        root.rename(parked)
        self.assertEqual(self._states(), {str(root): "orphaned"})
        parked.rename(root)
        later = datetime.now(timezone.utc) + timedelta(seconds=fleet.FORGET_AFTER_SECONDS * 10)
        self.assertEqual(fleet.forget_missing_roots(self.registry, now=later), [])
        entries = fleet.load_registry(self.registry)
        self.assertEqual([item["root"] for item in entries], [str(root)])
        self.assertNotIn("missing_since", entries[0])
        self.assertEqual(self._states(), {str(root): "quiet"})
        self.assertEqual(self._log(), "")

    def test_forget_removes_exactly_one_entry_and_refuses_a_root_that_exists(self):
        """[#207] acceptance 3: the FORGET button's endpoint."""
        gone = self.project("gone").resolve()
        kept = self.project("kept").resolve()
        fleet.register_project(gone, self.registry)
        fleet.register_project(kept, self.registry)
        fleet.note_registry_state(gone, {207}, "open", path=self.registry)
        shutil.rmtree(gone)
        server = fleet.FleetServer(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            def post(body):
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/api/forget", json.dumps(body),
                                   {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"})
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                return response.status, payload
            cards = {item["root"]: item for item in fleet.build_fleet(self.registry)["projects"]}
            self.assertEqual(cards[str(gone)]["state"], "orphaned")
            code, body = post({"root": str(kept), "binding": cards[str(kept)]["binding"], "confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("the root exists", body["error"])
            code, body = post({"root": str(gone), "binding": cards[str(gone)]["binding"], "confirm": True})
            self.assertEqual(code, 200, body)
            self.assertEqual(body["result"]["forgotten"], str(gone))
            self.assertEqual(body["result"]["last_state"], "open")
            self.assertTrue(body["result"]["run_never_closed"])
            since = body["result"]["missing_since"]
            self.assertIsInstance(since, str)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual([item["root"] for item in fleet.load_registry(self.registry)], [str(kept)])
        self.assertEqual(self._states(), {str(kept): "quiet"})
        line = self._log().splitlines()[-1]
        self.assertIn(f"fleet_entry_forgotten root={gone}", line)
        self.assertIn("last_state=open", line)
        self.assertIn("run_never_closed=true", line)
        self.assertIn(f"missing_since={since}", line)
        self.assertIn("by=Fleet Mission Control Pilot", line)

    def test_an_orphaned_card_offers_forget_and_not_close_run(self):
        """[#207] the page: FORGET on an orphaned card, no Cleanly Close Run."""
        script = (ROOT / "fleet" / "app.js").read_text()
        self.assertIn('"/api/forget"', script)
        self.assertIn('state === "orphaned"\n        ? `<button class="danger" data-op="forget"', script)
        self.assertIn('op === "release" || op === "forget"', script)  # no reason asked
        self.assertIn("MISSING", script)

    def test_teardown_is_in_the_playbook(self):
        """[#207] the lane playbook says run-close first, then unregister."""
        text = (ROOT / "playbook" / "landing.md").read_text()
        self.assertIn("run-close", text)
        self.assertIn("handsoff fleet unregister", text)
        self.assertIn("git worktree remove", text)


class ReviewerTreeBlameTests(unittest.TestCase):
    """[#203] the runtime's own in-flight temp files are not repository
    content, and the digest a reviewer is judged by comes from one scan."""

    def test_in_flight_handsoff_temp_files_are_not_repository_content(self):
        for name in ("..handsoff-live.json.tmp-7400-10c5b3be86cb41ce9910041440a62782",
                     "handsoff-status.json.tmp7400",
                     ".handsoff-session-liveness.json.tmp-1-ab",
                     "sub/..handsoff-live.json.tmp-1-2"):
            self.assertTrue(lib._digest_excluded(name, set()), name)
        for name in ("handsoff_lib.py", "src/handsoff-tmp-notes.md", "notes.tmp1"):
            self.assertFalse(lib._digest_excluded(name, set()), name)

    def test_digest_from_entries_matches_a_fresh_scan(self):
        root = Path(__import__("tempfile").mkdtemp(prefix="handsoff-digest-"))
        try:
            (root / "a.py").write_text("print(1)\n")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("b\n")
            cfg = lib.DEFAULT_CONFIG
            entries = lib.repository_digest_entries(root, cfg)
            self.assertEqual(lib.repository_digest_from_entries(entries), lib.repository_digest(root, cfg))
            (root / "a.py").write_text("print(2)\n")
            self.assertNotEqual(lib.repository_digest_from_entries(entries), lib.repository_digest(root, cfg))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_runtime_judges_a_reviewer_by_one_scan(self):
        source = Path(agent.__file__).read_text()
        self.assertNotIn('repository_digest_before.get("digest") != lib.repository_digest(root', source)
        self.assertIn("lib.repository_digest_from_entries(after)", source)
        self.assertIn("lib.repository_digest_from_entries(entries_before)", source)
        self.assertGreaterEqual(agent.READER_DRAIN_SECONDS, 30)


if __name__ == "__main__":
    unittest.main()
