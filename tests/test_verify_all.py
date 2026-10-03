#!/usr/bin/env python3
"""#363: one `verify --all` per round, and [digest] ignore in the config hashes.

The launch count is read from counter files the check scripts append to,
outside the fixture root so the counters never move the repository digest.
Each command appends one line per launch, so the line count is the number
of times the runner started it, whatever verify claims in its own output.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class VerifyAllFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.counters = Path(tempfile.mkdtemp(prefix="handsoff-verify-all-counters-"))
        self.addCleanup(shutil.rmtree, self.counters, True)

    def _command(self, name, fail=False):
        # [checks] refuses shell operators, so the append lives in a script
        script = self.counters / f"{name}.sh"
        script.write_text(f"echo launch >> '{self.counters / name}'\nexit {1 if fail else 0}\n")
        return f"sh {script}"

    def _launches(self, name):
        path = self.counters / name
        return len(path.read_text().splitlines()) if path.exists() else 0

    def _set_commands(self, commands):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", f"commands = {json.dumps(commands)}", 1))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _build(self, command_a, command_b):
        """REQ-001..004 name command A, REQ-005..007 name command B, and a
        manual REQ-008 that --all must leave alone."""
        self._set_commands([command_a, command_b])
        self.init("Issue 363 verify --all")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "#363 primary outcome",
                      "--test", command_a], cwd=self.tmp))
        for cid, command in (("REQ-002", command_a), ("REQ-003", command_a), ("REQ-004", command_a),
                             ("REQ-005", command_b), ("REQ-006", command_b), ("REQ-007", command_b)):
            self._ok(run(["criterion-add", cid, "--type", "supporting", "--requirement", f"#363 criterion {cid}",
                          "--verification", "automated", "--test", command], cwd=self.tmp))
        self._ok(run(["criterion-add", "REQ-008", "--type", "supporting", "--requirement", "#363 by hand",
                      "--verification", "manual", "--test", "walk through it"], cwd=self.tmp))

    def _records(self):
        records, problems = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        self.assertEqual(problems, [])
        return records


class TestVerifyAll(VerifyAllFixture):
    A_IDS = ("REQ-001", "REQ-002", "REQ-003", "REQ-004")
    B_IDS = ("REQ-005", "REQ-006", "REQ-007")

    def test_all_launches_each_distinct_command_once_and_records_each_criterion(self):
        self._build(self._command("a"), self._command("b"))
        result = self._ok(run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp))
        self.assertEqual((self._launches("a"), self._launches("b")), (1, 1))
        payload = json.loads(result.stdout)
        self.assertEqual(sorted(payload["criteria"]), sorted(self.A_IDS + self.B_IDS))
        self.assertEqual(len(payload["launched"]), 2)

        acceptance = self.read_acceptance()
        by_id = {c["id"]: c for c in acceptance["criteria"]}
        for cid in self.A_IDS + self.B_IDS:
            self.assertEqual(by_id[cid]["state"], "passing", cid)
        self.assertEqual(by_id["REQ-008"]["evidence"], [])

        records = {r["run_id"]: r for r in self._records()}
        for cid in self.A_IDS + self.B_IDS:
            record = records[payload["criteria"][cid]["run_id"]]
            self.assertEqual(record["criteria"], [cid])
            self.assertEqual(record["criterion_hashes"], {cid: lib.criterion_spec_hash(by_id[cid])})
            self.assertTrue(record["ok"])
        hashes = {records[payload["criteria"][cid]["run_id"]]["criterion_hashes"][cid]
                  for cid in self.A_IDS + self.B_IDS}
        self.assertEqual(len(hashes), 7, "each record carries its own criterion's spec hash")

    def test_a_failing_command_fails_only_the_criteria_that_name_it(self):
        self._build(self._command("a", fail=True), self._command("b"))
        result = run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self._launches("a"), self._launches("b")), (1, 1))
        by_id = {c["id"]: c for c in self.read_acceptance()["criteria"]}
        for cid in self.A_IDS:
            self.assertEqual(by_id[cid]["state"], "failing", cid)
        for cid in self.B_IDS:
            self.assertEqual(by_id[cid]["state"], "passing", cid)

    def test_all_and_criterion_are_mutually_exclusive_and_one_is_required(self):
        self._build(self._command("a"), self._command("b"))
        both = run(["verify", "--all", "--criterion", "REQ-001", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)
        self.assertIn("not allowed with argument", both.stderr)
        neither = run(["verify", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(neither.returncode, 2, neither.stdout + neither.stderr)
        self.assertEqual((self._launches("a"), self._launches("b")), (0, 0))

    def test_all_refuses_expect_fail(self):
        self._build(self._command("a"), self._command("b"))
        refused = run(["verify", "--all", "--expect-fail", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("--all does not apply to --expect-fail", refused.stdout)
        self.assertEqual((self._launches("a"), self._launches("b")), (0, 0))


class TestDigestIgnoreBinding(VerifyAllFixture):
    # A glob that matches nothing in the fixture: the repository digest is
    # unchanged by it, so only the config binding can stale anything.
    GLOB = "never-present-363/**"

    def _verified(self):
        self._set_commands(["true"])
        self.init("Issue 363 digest ignore")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "#363 bound ignore",
                      "--test", "true"], cwd=self.tmp))
        result = self._ok(run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp))
        return json.loads(result.stdout)

    def test_absent_or_empty_ignore_reproduces_the_legacy_hashes(self):
        cfg = lib.load_config(self.tmp)
        absent = {k: v for k, v in cfg.items() if k != "digest_ignore"}
        for name in ("config_hash", "verification_config_hash"):
            hasher = getattr(lib, name)
            legacy = hasher(absent)
            self.assertEqual(hasher({**cfg, "digest_ignore": []}), legacy, name)
            self.assertNotEqual(hasher({**cfg, "digest_ignore": [self.GLOB]}), legacy, name)
            self.assertEqual(hasher({**cfg, "digest_ignore": ["b/**", "a/**", "a/**"]}),
                             hasher({**cfg, "digest_ignore": ["a/**", "b/**"]}), name)

    def test_changing_ignore_after_verify_stales_the_evidence(self):
        self._verified()
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        records = self._records()
        self.assertEqual(lib.evidence_drift(self.tmp, cfg, acceptance, records)["stale"], [])
        self.assertEqual(lib.evidence_drift(self.tmp, {**cfg, "digest_ignore": []},
                                            acceptance, records)["stale"], [])
        changed = {**cfg, "digest_ignore": [self.GLOB]}
        self.assertEqual(lib.repository_digest(self.tmp, changed), lib.repository_digest(self.tmp, cfg))
        self.assertEqual(lib.evidence_drift(self.tmp, changed, acceptance, records)["stale"], ["REQ-001"])

    def test_changing_ignore_after_record_review_invalidates_the_review(self):
        self._verified()
        run_id = self.read_acceptance()["criteria"][0]["evidence"][-1]
        self._ok(run(["record-symptom-resolved", "--evidence", run_id, "--by", "test-implementer"], cwd=self.tmp))
        self.advance_to(5, implemented_by="impl")
        self._ok(run(["record-review", "--by", "reviewer", "--tests-executed", "yes"], cwd=self.tmp))
        self._ok(run(["advance", "6", "60"], cwd=self.tmp))
        toml = self.tmp / "handsoff.toml"
        original = toml.read_text()
        toml.write_text(original + "\n[digest]\nignore = []\n")
        empty = run(["validate"], cwd=self.tmp)
        self.assertNotIn("workflow policy changed since review", empty.stdout)
        toml.write_text(original + f"\n[digest]\nignore = [\"{self.GLOB}\"]\n")
        changed = run(["validate"], cwd=self.tmp)
        self.assertEqual(changed.returncode, 1, changed.stdout + changed.stderr)
        self.assertIn("workflow policy changed since review", changed.stdout)


class TestVerifyAllDocumented(unittest.TestCase):
    def test_lanes_and_reference_say_one_verify_all_per_round_after_bump_notes_and_docs(self):
        lanes = (ROOT / "playbook" / "lanes.md").read_text(encoding="utf-8")
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        flat_lanes = " ".join(lanes.split())
        flat_reference = " ".join(reference.split())
        self.assertIn("Run one `verify --all --by <you>` per round", flat_lanes)
        self.assertIn("only after the version bump, field notes and documentation are finished", flat_lanes)
        self.assertNotIn("always takes `--criterion", flat_lanes)
        self.assertNotIn("bare `verify` fails", flat_lanes)
        self.assertIn("Re-run `verify --all`", flat_lanes)
        self.assertIn("Run one `verify --all` per round", flat_reference)
        self.assertIn("only after the version bump, the field notes and the documentation are finished",
                      flat_reference)


if __name__ == "__main__":
    unittest.main()
