"""#165: failing first. A criterion's own commands must be seen to fail on
the tree before the feature; the later green run is judged against that
recorded red when [features].failing_first is on."""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run, set_fixture_check_commands

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402

RED = "sh tests/red.sh"      # exits 1 until the "feature" lands
GREEN = "sh tests/green.sh"  # always passes: a test that proves nothing


class FailingFirstTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        set_fixture_check_commands(self.tmp / "handsoff.toml", [RED, GREEN])
        tests = self.tmp / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "red.sh").write_text("test -f feature.built\n")
        (tests / "green.sh").write_text("exit 0\n")
        self.init()
        run(["criterion-update", "REQ-001", "--requirement", "the feature exists", "--verification", "automated",
             "--test", RED], cwd=self.tmp)

    def _features(self, **switches):
        toml = self.tmp / "handsoff.toml"
        lines = "".join(f"{k} = {'true' if v else 'false'}\n" for k, v in switches.items())
        toml.write_text(toml.read_text() + "\n[features]\n" + lines)

    def _records(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines()]

    def _gate_errors(self, phase=6, progress=60):
        status = self.read_status()
        status["phase_number"] = phase
        status["phase"] = lib.PHASES[phase]
        status["progress"] = progress
        cfg = lib.load_config(self.tmp)
        records, _ = lib.load_verifications(self.tmp, cfg)
        return lib.compute_errors(status, self.read_acceptance(), cfg, verifications=records)

    def test_expect_fail_records_a_valid_baseline_when_the_command_fails(self):
        r = run(["verify", "--criterion", "REQ-001", "--expect-fail", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["expected"], "fail")
        self.assertTrue(out["criteria"]["REQ-001"]["baseline"])
        record = self._records()[-1]
        self.assertEqual(record["kind"], "baseline")
        self.assertTrue(record["ok"])
        self.assertEqual(record["commands"], [RED])
        self.assertEqual(record["results"][0]["exit_code"], 1)
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(criterion["state"], "not_tested", "a baseline never changes the state")
        self.assertIn(record["run_id"], criterion["evidence"])
        self.assertIsNotNone(lib.criterion_baseline(criterion, self._records()))
        events = [json.loads(l)["kind"] for l in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertIn("baseline_recorded", events)

    def test_a_command_that_passes_before_the_feature_is_baseline_invalid(self):
        run(["criterion-update", "REQ-001", "--test", GREEN], cwd=self.tmp)
        r = run(["verify", "--criterion", "REQ-001", "--expect-fail", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertTrue(out["criteria"]["REQ-001"]["baseline_invalid"])
        record = self._records()[-1]
        self.assertEqual(record["kind"], "baseline")
        self.assertFalse(record["ok"])
        self.assertIn("baseline_invalid", record["description"])
        self.assertEqual(record["results"][0]["exit_code"], 0)
        criterion = next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(criterion["state"], "not_tested")
        self.assertIsNone(lib.criterion_baseline(criterion, self._records()), "an invalid baseline does not count")
        # and a baseline never satisfies the checks requirement
        self.assertNotIn("checks", lib.valid_evidence_kinds(criterion, self._records()))

    def test_gate_refuses_a_green_with_no_red_behind_it_only_when_the_switch_is_on(self):
        (self.tmp / "feature.built").write_text("x")
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(next(c for c in self.read_acceptance()["criteria"])["state"], "passing")
        self.assertFalse([e for e in self._gate_errors() if "baseline gate" in e], "switch off: nothing asked")
        self._features(failing_first=True)
        errors = [e for e in self._gate_errors() if "baseline gate" in e]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("REQ-001 passed without a recorded failing run", errors[0])
        self.assertIn("--expect-fail", errors[0])
        # phases before 6 and progress under 95 do not ask
        self.assertFalse([e for e in self._gate_errors(phase=4, progress=40) if "baseline gate" in e])
        self.assertTrue([e for e in self._gate_errors(phase=4, progress=95) if "baseline gate" in e])

    def test_a_red_recorded_first_satisfies_the_gate_and_an_older_criterion_hash_does_not(self):
        self._features(failing_first=True)
        run(["verify", "--criterion", "REQ-001", "--expect-fail", "--by", "tester"], cwd=self.tmp)
        (self.tmp / "feature.built").write_text("x")
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([e for e in self._gate_errors() if "baseline gate" in e])
        # the dashboard shows the red beside the pass
        row = next(c for c in dashboard.build_snapshot(self.tmp)["acceptance"]["criteria"] if c["id"] == "REQ-001")
        self.assertEqual(row["baseline_view"]["kind"], "recorded")
        self.assertTrue(row["baseline_view"]["run_id"].startswith("vr-"))
        # reword the criterion: the old red no longer describes this claim
        run(["criterion-update", "REQ-001", "--requirement", "the feature exists and is documented"], cwd=self.tmp)
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        errors = [e for e in self._gate_errors() if "baseline gate" in e]
        self.assertEqual(len(errors), 1, "a baseline bound to the old wording does not count")

    def test_not_applicable_with_a_reason_is_accepted_and_without_one_refused(self):
        self._features(failing_first=True)
        (self.tmp / "feature.built").write_text("x")
        r = run(["criterion-update", "REQ-001", "--baseline", "not_applicable"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("baseline_reason", r.stdout)
        r = run(["criterion-update", "REQ-001", "--baseline", "not_applicable",
                 "--baseline-reason", "the test is born with the feature in the same commit"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        criterion = next(c for c in self.read_acceptance()["criteria"])
        self.assertEqual(criterion["baseline"], "not_applicable")
        run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertFalse([e for e in self._gate_errors() if "baseline gate" in e])
        row = next(c for c in dashboard.build_snapshot(self.tmp)["acceptance"]["criteria"])
        self.assertEqual(row["baseline_view"], {"kind": "not_applicable", "reason": "the test is born with the feature in the same commit"})
        # clearing the declaration asks for the red again
        r = run(["criterion-update", "REQ-001", "--baseline", "none"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("baseline", next(c for c in self.read_acceptance()["criteria"]))
        run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertTrue([e for e in self._gate_errors() if "baseline gate" in e])
        # criterion-add takes the declaration too
        r = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "x", "--verification",
                 "automated", "--test", GREEN, "--baseline", "not_applicable", "--baseline-reason", "new test"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-002")["baseline_reason"], "new test")

    def test_a_baseline_never_reuses_a_cached_record(self):
        (self.tmp / "feature.built").write_text("x")
        run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        (self.tmp / "feature.built").unlink()
        r = run(["verify", "--criterion", "REQ-001", "--expect-fail", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["launched"], [RED], "the baseline ran the command again on this tree")
        self.assertEqual(out["reused"], {})


if __name__ == "__main__":
    unittest.main()
