"""P2.4: run-close outcomes that say what is uncertain: verified_with_known_risk,
qa_pending and blocked_environment."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_fleet as fleet  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as sup  # noqa: E402

LIVE = {"phase_number": 8, "progress": 100, "live_verification_id": "ver-1"}


class CloseOutcomeRules(unittest.TestCase):
    def test_the_new_outcomes_exist_and_two_count_as_not_verified(self):
        for outcome in ("verified_with_known_risk", "qa_pending", "blocked_environment"):
            self.assertIn(outcome, lib.RUN_OUTCOMES)
        self.assertEqual(set(lib.UNVERIFIED_RUN_OUTCOMES),
                         {"aborted", "released_unverified", "qa_pending", "blocked_environment"})
        self.assertNotIn("verified_with_known_risk", lib.UNVERIFIED_RUN_OUTCOMES)

    def test_verified_with_known_risk_requires_the_risk_text(self):
        for risk in (None, "", "   "):
            with self.assertRaisesRegex(lib.HandsoffError, "requires --known-risk"):
                lib.validate_close_outcome(LIVE, {}, "verified_with_known_risk", "shipped", risk)
        self.assertEqual(lib.validate_close_outcome(LIVE, {}, "verified_with_known_risk", "shipped",
                                                    " flaky  on Windows "), "flaky on Windows")
        with self.assertRaisesRegex(lib.HandsoffError, "at most"):
            lib.validate_close_outcome(LIVE, {}, "verified_with_known_risk", "shipped", "x" * 2001)

    def test_verified_with_known_risk_refused_before_phase_8(self):
        with self.assertRaisesRegex(lib.HandsoffError, "live-verified run"):
            lib.validate_close_outcome({"phase_number": 7, "progress": 70, "live_verification_id": "ver-1"},
                                       {}, "verified_with_known_risk", "early", "risk")

    def test_verified_with_known_risk_refused_at_phase_8_without_live_evidence(self):
        with self.assertRaisesRegex(lib.HandsoffError, "live verification absent"):
            lib.validate_close_outcome({"phase_number": 8, "progress": 100}, {"require_live_verification": True},
                                       "verified_with_known_risk", "no evidence", "risk")

    def test_qa_pending_and_blocked_environment_require_a_reason(self):
        for outcome in ("qa_pending", "blocked_environment"):
            for reason in (None, "", "  "):
                with self.assertRaisesRegex(lib.HandsoffError, f"--outcome {outcome} requires --reason"):
                    lib.validate_close_outcome({"phase_number": 7}, {}, outcome, reason, None)
            self.assertIsNone(lib.validate_close_outcome({"phase_number": 7}, {}, outcome, "QA owns it", None))

    def test_known_risk_belongs_to_one_outcome_only(self):
        with self.assertRaisesRegex(lib.HandsoffError, "applies only to"):
            lib.validate_close_outcome({"phase_number": 7}, {}, "qa_pending", "waiting", "risk")

    def test_run_close_requires_reason_at_the_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sup.build_parser().parse_args(["run-close", "--by", "pilot", "--outcome", "qa_pending"])


class CloseStatesEndToEnd(HandsoffTestCase):
    def close(self, *extra):
        status = json.loads((self.tmp / "handsoff-status.json").read_text())
        return run(["run-close", "--by", "pilot", "--expected-updated-at", status["updated_at"], *extra],
                   cwd=self.tmp)

    def make_live_verified(self):
        status = json.loads((self.tmp / "handsoff-status.json").read_text())
        status.update(LIVE, phase=lib.PHASES[8])
        lib.atomic_write_json(self.tmp / "handsoff-status.json", status)

    def assert_shown(self, outcome, text, known_risk=None):
        status = json.loads((self.tmp / "handsoff-status.json").read_text())
        closed = status["run_closed"]
        self.assertEqual((closed["outcome"], closed["reason"]), (outcome, text))
        self.assertEqual(closed.get("known_risk"), known_risk)
        self.assertEqual(lib.validate_status_schema(status), [])
        # status
        shown = run(["status"], cwd=self.tmp)
        self.assertEqual(json.loads(shown.stdout)["run_closed"]["outcome"], outcome)
        self.assertEqual(json.loads(shown.stdout)["run_closed"]["reason"], text)
        # the run report
        cfg = lib.load_config(self.tmp)
        acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        report = lib.render_final_report(self.tmp, cfg, status, acceptance, [], [], ["SHIP_FEATURE_VALID"],
                                         runner=lambda *a, **k: None)
        self.assertIn("### Outcome", report)
        self.assertIn(f"- {outcome}: {text}", report)
        if known_risk:
            self.assertIn(f"- known risk: {known_risk}", report)
        # Fleet
        card = fleet.project_view({"root": str(self.tmp), "registered_at": "now"})
        self.assertEqual(card["state"], "closed")
        self.assertEqual(card["run_closed"]["outcome"], outcome)
        self.assertEqual(card["run_closed"]["reason"], text)
        self.assertEqual(card["run_closed"].get("known_risk"), known_risk)

    def test_qa_pending_close_records_and_shows_its_reason(self):
        self.init("QA pending close")
        result = self.close("--outcome", "qa_pending", "--reason", "waiting on the QA pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.tmp / json.loads(result.stdout)["transaction"]["path"]).read_text())
        self.assertEqual(record["close_outcome"], {"outcome": "qa_pending", "reason": "waiting on the QA pass",
                                                   "known_risk": None})
        self.assert_shown("qa_pending", "waiting on the QA pass")

    def test_blocked_environment_close_records_and_shows_its_reason(self):
        self.init("Blocked close")
        result = self.close("--outcome", "blocked_environment", "--reason", "device lab offline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_shown("blocked_environment", "device lab offline")

    def test_verified_with_known_risk_refused_before_phase_8_end_to_end(self):
        self.init("Too early")
        result = self.close("--outcome", "verified_with_known_risk", "--reason", "early",
                            "--known-risk", "untested on Linux")
        self.assertEqual(result.returncode, 1)
        self.assertIn("live-verified run", result.stdout)
        self.assertIsNone(json.loads((self.tmp / "handsoff-status.json").read_text()).get("run_closed"))
        self.assertFalse((self.tmp / ".handsoff-archive" / "close-transactions").is_dir()
                         and any((self.tmp / ".handsoff-archive" / "close-transactions").iterdir()))

    def test_verified_with_known_risk_without_the_risk_is_refused(self):
        self.init("No risk named")
        self.make_live_verified()
        result = self.close("--outcome", "verified_with_known_risk", "--reason", "shipped")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires --known-risk", result.stdout)

    def test_verified_with_known_risk_close_at_live_verified_phase_8(self):
        self.init("Shipped with a risk")
        self.make_live_verified()
        result = lib.close_run(self.tmp, by="pilot", reason="shipped", outcome="verified_with_known_risk",
                               known_risk="cold start is slow on old laptops", release_dashboard=False)
        self.assertTrue(result["closed"])
        self.assert_shown("verified_with_known_risk", "shipped", "cold start is slow on old laptops")


if __name__ == "__main__":
    unittest.main()
