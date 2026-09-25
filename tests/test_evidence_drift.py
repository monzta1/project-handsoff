"""Focused repository-digest evidence tests for REQ-001 and REQ-002."""
import json
import sys
import unittest

from tests.test_handsoff_supervisor import HandsoffTestCase, run

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "bin"))
import handsoff_lib as lib


class EvidenceDriftTests(HandsoffTestCase):
    def verify(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("commands = []", 'commands = ["true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test", "true"], self.tmp).returncode, 0)
        result = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["criteria"]["REQ-001"]["run_id"]

    def prepared_phase_six(self):
        run_id = self.verify()
        resolved = run(["record-symptom-resolved", "--evidence", run_id,
                        "--by", "test-implementer"], self.tmp)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        reached = self.advance_to(6, implemented_by="test-implementer",
                                  reviewed_by="test-reviewer")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)

    def drift(self):
        source = self.tmp / "bin" / "handsoff_lib.py"
        source.write_text(source.read_text() + "\n# evidence drift test\n")

    def test_fresh_and_source_edit_classification(self):
        self.verify()
        cfg = lib.load_config(self.tmp)
        acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        records, _ = lib.load_verifications(self.tmp, cfg)
        self.assertIn("REQ-001", lib.evidence_drift(self.tmp, cfg, acceptance, records)["current"])
        (self.tmp / "bin" / "handsoff_lib.py").write_text(
            (self.tmp / "bin" / "handsoff_lib.py").read_text() + "\n# drift test\n")
        drift = lib.evidence_drift(self.tmp, cfg, acceptance, records)
        self.assertEqual(drift["stale"], ["REQ-001"])

    def test_failed_check_tail_is_redacted_and_success_has_no_tail(self):
        """REQ-002: retain only redacted failure diagnosis, never success output."""
        toml = self.tmp / "handsoff.toml"
        (self.tmp / "fail_check.py").write_text(
            "print('token=ghp_abcdefghijklmnopqrstuvwxyz0123456789')\n"
            "raise SystemExit(1)\n")
        toml.write_text(toml.read_text().replace(
            "commands = []",
            'commands = ["python3 fail_check.py", "true"]'))
        self.init()
        self.assertEqual(run(["criterion-update", "REQ-001", "--test",
                              "python3 fail_check.py"], self.tmp).returncode, 0)
        self.assertEqual(run(["criterion-add", "REQ-002", "--type", "supporting",
                              "--requirement", "Successful checks remain tail-less", "--verification", "automated",
                              "--test", "true"], self.tmp).returncode, 0)
        failed = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], self.tmp)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        records, _ = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        failed_record = records[-1]["results"][0]
        self.assertIn("output_tail", failed_record)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", failed_record["output_tail"])
        passed = run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], self.tmp)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        records, _ = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        self.assertNotIn("output_tail", records[-1]["results"][0])

    def test_legacy_record_is_unknown(self):
        self.init()
        cfg = lib.load_config(self.tmp)
        acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        record = lib.append_verification(self.tmp, cfg, kind="checks", ok=True, by="legacy",
                                         criteria=[acceptance["criteria"][0]])
        drift = lib.evidence_drift(self.tmp, cfg, acceptance, [record])
        self.assertEqual(drift["unknown"], ["REQ-001"])

    def test_stale_evidence_blocks_gates(self):
        self.prepared_phase_six()
        self.drift()
        cfg = lib.load_config(self.tmp)
        acceptance = self.read_acceptance()
        records, _ = lib.load_verifications(self.tmp, cfg)

        validated = run(["validate"], self.tmp)
        self.assertNotEqual(validated.returncode, 0)
        self.assertIn("evidence drift: REQ-001", validated.stdout + validated.stderr)
        self.assertIn("handsoff_supervisor.py verify --criterion REQ-001 --by ACTOR",
                      validated.stdout + validated.stderr)

        advanced = run(["advance", "7", "70", "--implemented-by", "test-implementer"], self.tmp)
        self.assertNotEqual(advanced.returncode, 0)
        self.assertIn("REQ-001", advanced.stdout + advanced.stderr)

        review = run(["record-review", "--by", "other-reviewer"], self.tmp)
        self.assertNotEqual(review.returncode, 0)
        self.assertIn("evidence drift", (review.stdout + review.stderr).lower())

        gate = run(["deployment-gate", "--approve", "--by", "pilot"], self.tmp)
        self.assertNotEqual(gate.returncode, 0)
        self.assertIn("REQ-001", gate.stdout + gate.stderr)

        status = self.read_status()
        status.update({"phase_number": 7, "phase": lib.PHASES[7], "progress": 70})
        lib.commit(self.tmp, cfg, status=status, event_kind="test-phase",
                   event_message="Set fixture to Phase 7 for dashboard drift input")
        import handsoff_dashboard as dashboard
        request = dashboard._input_request(status, cfg, self.tmp, acceptance, records)
        self.assertEqual(request["kind"], "evidence_drift")

    def test_manifest_only_edit_is_stale(self):
        self.verify()
        runtime = self.tmp / "handsoff-runtime.json"
        runtime.write_text(runtime.read_text().replace("{", "{ ", 1))
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["stale"], ["REQ-001"])

    def test_gitignored_state_edit_is_current(self):
        self.verify()
        (self.tmp / ".handsoff-agent-output.json").write_text("{\"test\": true}\n")
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["current"], ["REQ-001"])

    def test_reverify_clears_drift(self):
        self.verify()
        self.drift()
        cfg = lib.load_config(self.tmp)
        before = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                    lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(before["stale"], ["REQ-001"])
        refreshed = run(["verify", "--criterion", "REQ-001", "--by", "test-implementer"], self.tmp)
        self.assertEqual(refreshed.returncode, 0, refreshed.stdout + refreshed.stderr)
        cfg = lib.load_config(self.tmp)
        after = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(after["current"], ["REQ-001"])
        self.assertEqual(after["stale"], [])

    def test_non_git_gitignore_ignores_runtime_log(self):
        (self.tmp / ".gitignore").write_text("*.log\n")
        (self.tmp / "run.log").write_text("start\n")
        self.verify()
        (self.tmp / "run.log").write_text("start\nchanged\n")
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertIn("REQ-001", drift["current"])
        (self.tmp / ".gitignore").write_text("")
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["stale"], ["REQ-001"])

    def test_digest_ignore_excludes_paths_but_not_source(self):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + '\n[digest]\nignore = ["build/**", "*.tmp"]\n')
        self.verify()
        (self.tmp / "build").mkdir()
        (self.tmp / "build" / "out.txt").write_text("generated")
        (self.tmp / "a.tmp").write_text("temporary")
        cfg = lib.load_config(self.tmp)
        self.assertIn("REQ-001", lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                                     lib.load_verifications(self.tmp, cfg)[0])["current"])
        (self.tmp / "product.txt").write_text("source")
        self.assertEqual(lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                            lib.load_verifications(self.tmp, cfg)[0])["stale"], ["REQ-001"])

    def test_version_pin_rewrite_is_not_drift_but_a_source_edit_still_is(self):
        """v0.3.25 field-note defect 2: `upgrade --to` rewrites .handsoff-version
        and every completed run's evidence read as stale. The pin is Handsoff
        configuration, excluded like handsoff.toml; product source still drifts."""
        self.verify()
        pin = self.tmp / lib.VERSION_PIN_FILE
        before = pin.read_text() if pin.is_file() else None
        pin.write_text("v9.9.9\n")
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["current"], ["REQ-001"], drift)
        self.assertEqual(drift["stale"], [])
        self.assertEqual(drift["changed_paths"], [])
        pin.write_text("0.3.*\n")
        self.assertIn("REQ-001", lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                                     lib.load_verifications(self.tmp, cfg)[0])["current"])
        self.drift()
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["stale"], ["REQ-001"])
        self.assertNotIn(lib.VERSION_PIN_FILE, drift["changed_paths"])
        if before is not None:
            pin.write_text(before)

    def test_changed_paths_are_reported_and_legacy_has_note(self):
        self.prepared_phase_six()
        (self.tmp / "product.txt").write_text("source")
        (self.tmp / "extra.txt").write_text("extra")
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(),
                                   lib.load_verifications(self.tmp, cfg)[0])
        self.assertEqual(drift["changed_paths"], ["extra.txt", "product.txt"])
        blocked = run(["advance", "7", "70", "--implemented-by", "test-implementer"], self.tmp)
        self.assertIn("changed paths: extra.txt, product.txt", blocked.stdout + blocked.stderr)
        legacy = lib.append_verification(self.tmp, cfg, kind="checks", ok=True, by="legacy",
                                         criteria=[self.read_acceptance()["criteria"][0]])
        legacy_drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), [legacy])
        self.assertEqual(legacy_drift["changed_paths_note"], "snapshot not recorded")


if __name__ == "__main__":
    unittest.main()
