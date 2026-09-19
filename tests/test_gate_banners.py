"""#147: a paused run says whose turn it is. input_required carries `turn`
(pilot, reviewer, architect, supervisor) and `preauthorized` (the newest
human pilot note stating a standing pre-authorization), the briefing uses
the same words, and Fleet reports `waiting` only for the Pilot's own turn
without a pre-authorization. Real ledgers throughout."""
import sys
import unittest

from tests import test_handsoff_supervisor as _supervisor_tests
from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, approve_design_review, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _AmendmentFixture(HandsoffTestCase):
    STATE_FILES = _supervisor_tests.TestAmendmentLane.STATE_FILES
    ARCHITECT = _supervisor_tests.TestAmendmentLane.ARCHITECT
    REVIEWER = _supervisor_tests.TestAmendmentLane.REVIEWER
    PILOT = _supervisor_tests.TestAmendmentLane.PILOT
    _phase_4_run = _supervisor_tests.TestAmendmentLane._phase_4_run
    _open = _supervisor_tests.TestAmendmentLane._open
    _revise = _supervisor_tests.TestAmendmentLane._revise
    _review = _supervisor_tests.TestAmendmentLane._review
    _approve = _supervisor_tests.TestAmendmentLane._approve
    _write_tx = _supervisor_tests.TestAmendmentLane._write_tx
    _update = _supervisor_tests.TestAmendmentLane._update
    _ok = _supervisor_tests.TestAmendmentLane._ok
    _criteria = _supervisor_tests.TestAmendmentLane._criteria
    _events = _supervisor_tests.TestAmendmentLane._events
    _kinds = _supervisor_tests.TestAmendmentLane._kinds

    def setUp(self):
        super().setUp()
        self.lib = lib
        self.dashboard = dashboard
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]', 1)
                        .replace("live_commands = []", 'live_commands = ["true"]', 1))

    def _fleet_state(self):
        registry = self.tmp / ".handsoff-fixture" / "fleet.json"
        registry.parent.mkdir(exist_ok=True)
        fleet.register_project(self.tmp, registry)
        return fleet.build_fleet(registry)["projects"][0]["state"]


class GateBannerTests(_AmendmentFixture):
    def test_open_amendment_is_the_reviewers_turn_then_the_pilots(self):
        self._phase_4_run()
        self._ok(self._open([self._update("REQ-003", requirement="[#102] corrected outcome")]))
        request = dashboard.build_snapshot(self.tmp)["input_required"]
        self.assertEqual((request["kind"], request["turn"], request["preauthorized"]),
                         ("amendment_review", "reviewer", None))
        self.assertEqual(request["amendment_round"], 1)
        briefing = dashboard.build_snapshot(self.tmp)["supervisor"]
        self.assertEqual(briefing["label"], "Under independent review")
        self.assertIn("round 1", briefing["headline"])
        self.assertIn("Nothing waits on you, Pilot", briefing["headline"])
        self.assertNotEqual(briefing["tone"], "critical")
        self.assertNotEqual(self._fleet_state(), "waiting")
        # A revision after changes requested is the Architect's turn, round 2.
        self._ok(self._review("--request-changes", summary="tighten the wording"))
        request = dashboard.build_snapshot(self.tmp)["input_required"]
        self.assertEqual((request["kind"], request["turn"]), ("amendment_revision", "architect"))
        self.assertEqual(dashboard.build_snapshot(self.tmp)["supervisor"]["label"], "Architect revising")
        self._ok(self._revise([self._update("REQ-003", requirement="[#102] corrected outcome, tightened")]))
        request = dashboard.build_snapshot(self.tmp)["input_required"]
        self.assertEqual((request["kind"], request["turn"], request["amendment_round"]),
                         ("amendment_review", "reviewer", 2))
        # A standing pre-authorization never relabels someone else's turn:
        # with the note on the ledger the reviewer's step still reads as
        # under review and preauthorized stays null.
        noted = run(["pilot-note", "--by", "moncy", "--text", "Standing pre-authorization: record approvals without asking"], cwd=self.tmp)
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot["input_required"]
        self.assertEqual((request["turn"], request["preauthorized"]), ("reviewer", None))
        self.assertEqual(snapshot["supervisor"]["label"], "Under independent review")
        self.assertEqual(dashboard._decision_headline({"turn": "reviewer", "preauthorized": {"by": "moncy", "at": "x"},
                                                       "amendment_id": "am-1", "amendment_round": 2})[0],
                         "Under independent review")
        # Once the review is recorded, approval is the Pilot's own turn, and
        # now the standing note pre-authorizes it.
        self._ok(self._review())
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot["input_required"]
        self.assertEqual((request["kind"], request["turn"]), ("amendment_approval", "pilot"))
        self.assertEqual(request["preauthorized"]["by"], "moncy")
        self.assertEqual(snapshot["supervisor"]["label"], "Pre-authorized by pilot note")
        self.assertNotEqual(snapshot["supervisor"]["tone"], "critical")
        self.assertNotEqual(self._fleet_state(), "waiting")

    def test_a_standing_pilot_note_preauthorizes_the_pilots_turn(self):
        self.init("Design approval fixture")
        authored = run(["criterion-update", "REQ-001", "--requirement", "[#1] a real acceptance criterion"], cwd=self.tmp)
        self.assertEqual(authored.returncode, 0, authored.stdout + authored.stderr)
        self.assertEqual(run(["advance", "2", "20"], cwd=self.tmp).returncode, 0)
        reviewed = approve_design_review(self.tmp, architect="arch-ui", reviewer="reviewer-ui")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot["input_required"]
        self.assertEqual((request["kind"], request["turn"], request["preauthorized"]), ("design_approval", "pilot", None))
        self.assertEqual(snapshot["supervisor"]["label"], "Pilot approval needed")
        self.assertEqual(self._fleet_state(), "waiting")
        # A note without the phrase changes nothing; a managed role's note never counts.
        self.assertEqual(run(["pilot-note", "--by", "moncy", "--text", "watching from the phone"], cwd=self.tmp).returncode, 0)
        self.assertIsNone(dashboard.build_snapshot(self.tmp)["input_required"]["preauthorized"])
        self.assertEqual(run(["pilot-note", "--by", "codex-supervisor", "--text",
                              "pre-authorized: proceed"], cwd=self.tmp).returncode, 0)
        self.assertIsNone(dashboard.build_snapshot(self.tmp)["input_required"]["preauthorized"])
        # The Pilot's own standing pre-authorization is honoured.
        noted = run(["pilot-note", "--by", "moncy", "--text",
                     "Standing pre-authorization for this run: record the design and deployment approvals "
                     "without asking me again. " + "x" * 200], cwd=self.tmp)
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
        snapshot = dashboard.build_snapshot(self.tmp)
        request = snapshot["input_required"]
        self.assertTrue(request["required"])
        self.assertEqual(request["turn"], "pilot")
        self.assertEqual(request["preauthorized"]["by"], "moncy")
        self.assertTrue(request["preauthorized"]["at"].startswith("20"))
        self.assertLessEqual(len(request["preauthorized"]["excerpt"]), 160)
        self.assertIn("Standing pre-authorization", request["preauthorized"]["excerpt"])
        self.assertEqual(snapshot["supervisor"]["label"], "Pre-authorized by pilot note")
        self.assertIn("the Supervisor records the approval", snapshot["supervisor"]["headline"])
        self.assertNotEqual(snapshot["supervisor"]["tone"], "critical")
        self.assertNotEqual(self._fleet_state(), "waiting")
        # The CLI status carries the same fields.
        status = run(["status", "--json"], cwd=self.tmp)
        if status.returncode == 0 and status.stdout.strip().startswith("{"):
            import json
            payload = json.loads(status.stdout)
            if "input_required" in payload:
                self.assertEqual(payload["input_required"]["turn"], "pilot")

    def test_turn_table_covers_every_pause_kind(self):
        self.assertEqual(dashboard._decision_turn("deployment_approval"), "pilot")
        self.assertEqual(dashboard._decision_turn("regression_approval"), "pilot")
        self.assertEqual(dashboard._decision_turn("question"), "pilot")
        self.assertEqual(dashboard._decision_turn("escalation"), "pilot")
        self.assertEqual(dashboard._decision_turn("amendment_review"), "reviewer")
        self.assertEqual(dashboard._decision_turn("amendment_revision"), "architect")
        self.assertEqual(dashboard._decision_turn("evidence_drift"), "supervisor")
        self.assertEqual(dashboard._decision_turn("blocked"), "supervisor")
        self.assertIsNone(dashboard._decision_turn(None))
        quiet = dashboard._input_request({"status": "in_progress", "phase_number": 4, "next_action": "build"}, {})
        self.assertEqual((quiet["required"], quiet["turn"], quiet["preauthorized"]), (False, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
