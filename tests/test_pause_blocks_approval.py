"""#422: an open human pause names itself. `status` carries human_pause
while one is open; a run awaiting deployment approval behind it shows
approval_blocked_by_pause (who, since when, the command that ends it) with
no AUTHORIZE DEPLOYMENT control, and POST /api/deployment-approval answers
409 with the same text. Ending the pause brings the normal request back."""
import http.client
import json
import shutil
import sys
import threading
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402


class PauseBlocksApprovalTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)

    def _status(self):
        r = run(["status"], cwd=self.tmp)
        return json.loads(r.stdout)

    def _pause(self, note="checking the release notes"):
        r = run(["human-pause-start", "--by", "Sentinel", "--note", note], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.read_status()["human_pause"]

    def _end_pause(self):
        r = run(["human-pause-end", "--by", "Sentinel"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _phase7(self):
        self.init("Pause blocks approval")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="reviewer-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def _serve(self):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _post_approval(self):
        server = self._serve()
        try:
            host, port = server.server_address[:2]
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("POST", "/api/deployment-approval", body="{}",
                               headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
        return response.status, payload

    def test_status_shows_an_open_pause_and_hides_a_closed_one(self):
        self.init("Pause in status")
        self.assertIsNone(self._status()["human_pause"])
        pause = self._pause()
        self.assertEqual(self._status()["human_pause"],
                         {"by": "Sentinel", "at": pause["since"], "note": "checking the release notes"})
        self._end_pause()
        self.assertIsNone(self._status()["human_pause"])

    def test_a_pause_at_phase_7_blocks_approval_and_names_itself(self):
        self._phase7()
        pause = self._pause()
        request = dashboard.build_snapshot(self.tmp)["input_required"]
        self.assertTrue(request["required"])
        self.assertEqual(request["kind"], "approval_blocked_by_pause")
        message = request["message"]
        self.assertEqual(message, dashboard.approval_blocked_by_pause_message(pause))
        for part in ("Sentinel", pause["since"], "human-pause-end"):
            self.assertIn(part, message)
        # no AUTHORIZE DEPLOYMENT control: the button shows only for deployment_approval,
        # and the operator panel offers no deployment approval
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertNotIn("deployment_approve", [a["kind"] for a in snapshot["operator_actions"]])
        status, payload = self._post_approval()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"], message)
        self.assertIsNone(self.read_status()["deployment_approved"])

    def test_with_the_pause_ended_the_normal_approval_request_returns(self):
        self._phase7()
        self._pause()
        self._end_pause()
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual(snapshot["input_required"]["kind"], "deployment_approval")
        self.assertIn("deployment_approve", [a["kind"] for a in snapshot["operator_actions"]])
        status, payload = self._post_approval()
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_status()["deployment_approved"]["by"], "Mission Control Pilot")


if __name__ == "__main__":
    unittest.main()
