"""#417 REQ-003: extra design-review rounds by policy.

A Pilot may authorize up to five more design-review rounds in one ledgered
action (design-review-authorize --rounds N), consumed one per attempt; and
[workflow] design_rounds_on_convergence = K authorizes one further round on
its own whenever the latest round recorded strictly fewer findings than the
round before it, up to a cumulative per-run allowance of K that neither a
new proposal nor a restart resets.
"""

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handsoff_supervisor import HandsoffTestCase, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import handsoff_lib  # noqa: E402
from tests.fixture_state import write_version_pin  # noqa: E402

LIMIT = handsoff_lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS


class _DesignRoundsCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        write_version_pin(self.tmp)

    def _start(self, allowance=None):
        self.init("Design rounds")
        if allowance is not None:
            self._set_allowance(allowance)
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "A criterion"],
                             self.tmp).returncode, 0)
        self.assertEqual(run(["advance", "2", "10"], self.tmp).returncode, 0)

    def _set_allowance(self, allowance):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text(encoding="utf-8")
        line = f"design_rounds_on_convergence = {allowance}"
        text, count = re.subn(r"^design_rounds_on_convergence\s*=.*$", line, text, flags=re.MULTILINE)
        if count == 0:
            text, count = re.subn(r"^\[workflow\]\s*$", "[workflow]\n" + line, text, count=1,
                                  flags=re.MULTILINE)
        self.assertEqual(count, 1, "could not set [workflow] design_rounds_on_convergence")
        toml.write_text(text, encoding="utf-8")

    def _review(self, findings=0, expect_ok=True):
        args = ["record-design-review", "--by", "reviewer", "--architect", "architect-1",
                "--request-changes", "--summary", f"Changes with {findings} findings"]
        for index in range(findings):
            args += ["--finding", f"finding {index + 1}"]
        result = run(args, self.tmp)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text(encoding="utf-8").splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def _assert_held(self):
        status = self.read_status()
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status.get("authorization_hold"), "design_review")
        refused = self._review(expect_ok=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("design review budget exhausted", refused.stdout)

    def _new_proposal(self):
        session = handsoff_lib.create_agent_session(
            self.tmp, role="architect", actor="architect-live", adapter="codex",
            requested_model="default", resolution_source="configured")
        proposal = {field: (["item"] if field in {"approach", "decisions", "verification"} else [])
                    for field in handsoff_lib.DESIGN_PROPOSAL_FIELDS}
        proposal["summary"] = "Revised proposal"
        handsoff_lib.record_design_proposal(self.tmp, session["session_id"], proposal)


class PilotAuthorizesSeveralRounds(_DesignRoundsCase):
    def test_rounds_three_permits_exactly_three_more_attempts(self):
        self._start()
        for _ in range(LIMIT):
            self._review()
        result = run(["design-review-authorize", "--by", "pilot", "--rounds", "3"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"DESIGN_REVIEW_ATTEMPT_AUTHORIZED: {LIMIT + 1}", result.stdout)
        grant = self.read_status()["design_review_authorization"]
        self.assertEqual((grant["rounds_remaining"], grant["attempt_permitted"]), (3, LIMIT + 1))
        event = self._events("design_review_attempt_authorized")[-1]
        self.assertEqual(event["rounds"], 3)

        for spent in range(1, 4):
            self._review()
            grant = self.read_status()["design_review_authorization"]
            self.assertEqual(grant["rounds_remaining"], 3 - spent)
            if spent < 3:
                self.assertIsNone(grant["consumed_at"])
                self.assertEqual(grant["attempt_permitted"], LIMIT + spent + 1)
                again = run(["design-review-authorize", "--by", "pilot"], self.tmp)
                self.assertEqual(again.returncode, 1, "a grant with rounds left is still open")
            else:
                self.assertIsNotNone(grant["consumed_at"])
        self.assertEqual(self.read_status()["design_review_attempts"], LIMIT + 3)
        self._assert_held()

    def test_without_rounds_one_attempt_is_permitted_as_before(self):
        self._start()
        for _ in range(LIMIT):
            self._review()
        self.assertEqual(run(["design-review-authorize", "--by", "pilot"], self.tmp).returncode, 0)
        self.assertEqual(self.read_status()["design_review_authorization"]["rounds_remaining"], 1)
        self._review()
        self.assertIsNotNone(self.read_status()["design_review_authorization"]["consumed_at"])
        self._assert_held()

    def test_rounds_outside_one_to_five_are_refused(self):
        self._start()
        for _ in range(LIMIT):
            self._review()
        for value in ("0", "6", "-1"):
            result = run(["design-review-authorize", "--by", "pilot", "--rounds", value], self.tmp)
            self.assertEqual(result.returncode, 1, value)
            self.assertIn("--rounds must be an integer from 1 to 5", result.stdout)
        self.assertIsNone(self.read_status()["design_review_authorization"])
        result = run(["design-review-authorize", "--by", "pilot", "--rounds", "5"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConvergenceAuthorizesRounds(_DesignRoundsCase):
    def test_shrinking_counts_authorize_up_to_k_and_stop_at_k(self):
        self._start(allowance=2)
        for count in (5, 5):
            self._review(count)
        self._review(4)  # exhausts the budget, 4 < 5: automatic round 1 of 2
        status = self.read_status()
        self.assertEqual(status["status"], "in_progress")
        self.assertNotIn("authorization_hold", status)
        self.assertEqual(status["design_rounds_auto_used"], 1)
        self.assertEqual(status["design_review_authorization"]["by"],
                         handsoff_lib.DESIGN_ROUNDS_ON_CONVERGENCE_ACTOR)
        self._review(3)  # 3 < 4: automatic round 2 of 2
        self.assertEqual(self.read_status()["design_rounds_auto_used"], 2)
        self._review(2)  # 2 < 3, but the allowance is spent
        self.assertEqual(self.read_status()["design_rounds_auto_used"], 2)
        self._assert_held()
        events = self._events("design_review_auto_authorized")
        self.assertEqual([(e["round"], e["findings"], e["previous_findings"]) for e in events],
                         [(LIMIT + 1, 4, 5), (LIMIT + 2, 3, 4)])

    def test_equal_counts_do_not_authorize(self):
        self._start(allowance=3)
        for count in (2, 3, 3):
            self._review(count)
        self.assertEqual(self._events("design_review_auto_authorized"), [])
        self.assertEqual(self.read_status().get("design_rounds_auto_used", 0), 0)
        self._assert_held()

    def test_larger_counts_do_not_authorize(self):
        self._start(allowance=3)
        for count in (2, 1, 4):
            self._review(count)
        self.assertEqual(self._events("design_review_auto_authorized"), [])
        self._assert_held()
        # After a Pilot round, a larger count still grants nothing.
        self.assertEqual(run(["design-review-authorize", "--by", "pilot"], self.tmp).returncode, 0)
        self._review(6)
        self.assertEqual(self._events("design_review_auto_authorized"), [])
        self._assert_held()

    def test_off_by_default(self):
        self._start()
        for count in (5, 4, 3):
            self._review(count)
        self.assertEqual(self._events("design_review_auto_authorized"), [])
        self._assert_held()

    def test_allowance_is_exhausted_after_a_reload_and_a_new_proposal(self):
        self._start(allowance=1)
        for count in (3, 3, 2):
            self._review(count)
        self.assertEqual(self.read_status()["design_rounds_auto_used"], 1)
        self._review(1)  # shrinking again, but K = 1 is spent
        self._assert_held()
        # A fresh load of config and status (a restart) and a new proposal
        # leave the cumulative count in the ledger untouched.
        self.assertEqual(handsoff_lib.load_config(self.tmp)["design_rounds_on_convergence"], 1)
        self._new_proposal()
        status = self.read_status()
        self.assertEqual(status["design_rounds_auto_used"], 1)
        self.assertEqual(status.get("authorization_hold"), "design_review")
        self.assertEqual(run(["design-review-authorize", "--by", "pilot"], self.tmp).returncode, 0)
        self._review(0)  # 0 < 1, still spent
        self.assertEqual(self.read_status()["design_rounds_auto_used"], 1)
        self.assertEqual(len(self._events("design_review_auto_authorized")), 1)
        self._assert_held()


class FindingCountsCompareCountOnly(unittest.TestCase):
    def test_legacy_and_current_entries_count_the_same_way(self):
        status = {"design_review_history": [
            {"findings": ["legacy text one", "legacy text two"]},
            {"findings": [{"id": "F2.1", "text": "current"}]},
        ]}
        self.assertEqual(handsoff_lib.design_review_finding_counts(status), (1, 2))
        self.assertIsNone(handsoff_lib.design_review_finding_counts({"design_review_history": [{}]}))
        self.assertEqual(handsoff_lib.design_review_finding_counts(
            {"design_review_history": [{"findings": None}, {}]}), (0, 0))


class ConvergenceConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsoff-design-rounds-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _load(self, body):
        (self.tmp / "handsoff.toml").write_text("[workflow]\n" + body, encoding="utf-8")
        return handsoff_lib.load_config(self.tmp)

    def test_default_is_zero(self):
        self.assertEqual(handsoff_lib.DEFAULT_CONFIG["design_rounds_on_convergence"], 0)
        self.assertEqual(self._load("")["design_rounds_on_convergence"], 0)

    def test_a_non_negative_integer_is_accepted(self):
        self.assertEqual(self._load("design_rounds_on_convergence = 4\n")["design_rounds_on_convergence"], 4)

    def test_negative_and_non_integer_values_are_refused(self):
        for value in ("-1", "true", '"2"', "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(handsoff_lib.HandsoffError):
                    self._load(f"design_rounds_on_convergence = {value}\n")


if __name__ == "__main__":
    unittest.main()
