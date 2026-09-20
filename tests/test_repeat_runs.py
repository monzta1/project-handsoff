"""#169: repeat runs as evidence. A criterion may demand N green runs in a
row; the record keeps every attempt, and the failing one keeps its exit
code, output and seed."""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run, set_fixture_check_commands

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

FLAKY = "sh tests/flaky.sh"   # fails on the third call (a counter file), passes otherwise
SEEDED = "sh tests/seeded.sh"  # fails when the seed ends with a digit the fixture chooses


class RepeatRunTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        set_fixture_check_commands(self.tmp / "handsoff.toml", [FLAKY, SEEDED])
        tests = self.tmp / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "flaky.sh").write_text(
            'n=$(cat .calls 2>/dev/null || echo 0); n=$((n+1)); echo $n > .calls; echo "call $n"; [ "$n" -ne 3 ]\n')
        (tests / "seeded.sh").write_text('echo "seed=$RUN_SEED"; [ -n "$RUN_SEED" ] && [ "$RUN_SEED" != "$BAD_SEED" ]\n')
        self.init()
        run(["criterion-update", "REQ-001", "--requirement", "the store survives a race", "--verification", "automated",
             "--test", FLAKY], cwd=self.tmp)

    def _records(self):
        return [json.loads(line) for line in (self.tmp / "handsoff-verifications.jsonl").read_text().splitlines()]

    def test_repeat_records_every_attempt_and_names_the_failing_one(self):
        r = run(["criterion-update", "REQ-001", "--repeat", "5"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        criterion = next(c for c in self.read_acceptance()["criteria"])
        self.assertEqual(criterion["repeat"], 5)
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["criteria"]["REQ-001"]["attempts"], out["criteria"]["REQ-001"]["repeat"]), (3, 5))
        record = self._records()[-1]
        self.assertFalse(record["ok"])
        self.assertEqual([a["ok"] for a in record["attempts"]], [True, True, False])
        self.assertEqual(record["attempts"][2]["exit_codes"], [1])
        self.assertIn("call 3", record["attempts"][2]["output_tail"])
        self.assertEqual(record["description"], "repeat 5: failed at attempt 3")
        self.assertEqual(next(c for c in self.read_acceptance()["criteria"])["state"], "failing")
        # a fresh run where every attempt passes turns it green with 5/5
        (self.tmp / ".calls").write_text("10")
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self._records()[-1]
        self.assertTrue(record["ok"])
        self.assertEqual(len(record["attempts"]), 5)
        self.assertEqual(record["description"], "repeat 5: 5/5 passed")
        self.assertEqual(next(c for c in self.read_acceptance()["criteria"])["state"], "passing")
        self.assertEqual(json.loads(r.stdout)["reused"], {}, "a repeat run never reuses a cached record")

    def test_seeds_are_distinct_reproducible_and_the_failing_seed_is_kept(self):
        run(["criterion-update", "REQ-001", "--test", SEEDED, "--repeat", "4", "--seed-env", "RUN_SEED"], cwd=self.tmp)
        criterion = next(c for c in self.read_acceptance()["criteria"])
        self.assertEqual((criterion["repeat"], criterion["seed_env"]), (4, "RUN_SEED"))
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self._records()[-1]
        seeds = [a["seed"] for a in record["attempts"]]
        self.assertEqual(len(seeds), 4)
        self.assertEqual(len(set(seeds)), 4, "every attempt gets its own seed")
        self.assertTrue(all(len(s) == 8 for s in seeds))
        # make the third seed the bad one: the record names it
        import os
        bad = seeds[2]
        (self.tmp / "tests" / "seeded.sh").write_text(f'echo "seed=$RUN_SEED"; [ "$RUN_SEED" != "{bad}" ]\n')
        r = run(["verify", "--criterion", "REQ-001", "--by", "tester", "--no-cache"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        record = self._records()[-1]
        self.assertEqual(record["attempts"][-1]["seed"], bad)
        self.assertEqual(record["description"], f"repeat 4: failed at attempt 3 (seed {bad})")
        # reproducible: the same run hash yields the same seeds
        self.assertEqual(lib.repeat_seed("abc", 3), lib.repeat_seed("abc", 3))
        self.assertNotEqual(lib.repeat_seed("abc", 3), lib.repeat_seed("abd", 3))

    def test_bounds_and_the_regression_gate_are_untouched(self):
        for bad in (["--repeat", "0"], ["--repeat", "51"], ["--seed-env", "lower"]):
            r = run(["criterion-update", "REQ-001", *bad], cwd=self.tmp)
            self.assertEqual(r.returncode, 1, bad)
        r = run(["criterion-update", "REQ-001", "--repeat", "1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("repeat", next(c for c in self.read_acceptance()["criteria"]))
        r = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "x", "--verification", "automated",
                 "--test", SEEDED, "--repeat", "3", "--seed-env", "RUN_SEED"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(next(c for c in self.read_acceptance()["criteria"] if c["id"] == "REQ-002")["repeat"], 3)
        # --expect-fail and repeat do not mix
        r = run(["verify", "--criterion", "REQ-002", "--expect-fail", "--by", "tester"], cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not apply to a repeat criterion", r.stdout)
        # run_repeated_checks never bypasses the regression gate
        with self.assertRaisesRegex(lib.HandsoffError, "full regression blocked"):
            cfg = lib.load_config(self.tmp)
            cfg["regressions"] = [{"name": "full", "commands": ["sh tests/flaky.sh"]}]
            lib.run_repeated_checks(cfg, self.tmp, ["sh tests/"], 2, None, "h")


if __name__ == "__main__":
    unittest.main()
