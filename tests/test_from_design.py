"""Coverage for taking up a completed design document."""

import hashlib
import json
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from test_handsoff_supervisor import HandsoffTestCase, run
import handsoff_lib as lib


class FromDesignTests(HandsoffTestCase):
    def block(self, **changes):
        criterion = {"id": "REQ-001", "type": "primary_fix",
                     "requirement": "A valid design criterion",
                     "verification": "automated", "tests": ["true"],
                     "evidence": [], "state": "failing"}
        block = {"schema": 1, "criteria": [criterion],
                 "design_hash": lib.design_hash([criterion]),
                 "criterion_hashes": {"REQ-001": lib.criterion_spec_hash(criterion)},
                 "design_review": None, "items": [], "lane": "design",
                 "phases_run": [1, 2, 3], "phases_waived": [4, 5, 6, 7, 8]}
        block.update(changes)
        return block

    def document(self, block):
        path = self.tmp / "design.html"
        payload = json.dumps(block, sort_keys=True, separators=(",", ":"))
        path.write_text(f"<html>prose may not be parsed<script type=\"application/json\" id=\"handsoff-design\">{payload}</script></html>")
        return path

    def test_valid_take_up_enters_phase_two_and_records_provenance(self):
        path = self.document(self.block())
        result = run(["init", "Taken up", "--from-design", str(path), "--by", "host"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 2)
        taken = status["taken_up_from"]
        self.assertEqual(taken["path"], str(path.resolve()))
        self.assertEqual(taken["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(taken["source_design_hash"], self.block()["design_hash"])
        events = lib.read_events(self.tmp, lib.load_config(self.tmp))
        self.assertEqual(sum(event.get("kind") == "design_taken_up" for event in events), 1)

    def test_take_up_derives_registry_when_design_omits_or_nulls_items(self):
        for items in ("omitted", None):
            with self.subTest(items=items):
                block = self.block()
                if items == "omitted":
                    del block["items"]
                else:
                    block["items"] = None
                path = self.document(block)
                result = run(["init", "Taken up", "--from-design", str(path)], self.tmp)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
                self.assertIsInstance(acceptance.get("work_items"), list)
                self.assertTrue((self.tmp / "handsoff-status.json").exists())
                for artifact in ("handsoff-status.json", "handsoff-acceptance.json",
                                 "handsoff-events.jsonl", "handsoff-verifications.jsonl",
                                 ".handsoff-event-head.json"):
                    (self.tmp / artifact).unlink(missing_ok=True)

    def test_take_up_without_lane_is_full_and_can_advance(self):
        cfg = lib.load_config(self.tmp)
        criterion = self.block()["criteria"][0]
        digest = self.block()["design_hash"]
        items = lib.derive_work_item_registry(
            {"feature": "Taken up", "criteria": [criterion]}, cfg, explicit_items=[]
        )
        binding = {"config_hash": lib.config_hash(cfg),
                   "scope_hash": lib.work_item_scope_hash(items, [criterion])}
        review = {"decision": "approved", "by": "reviewer", "architect": "architect",
                  "at": "2026-01-01T00:00:00+00:00", "summary": "approved",
                  "design_hash": digest, **binding}
        approval = {"by": "pilot", "architect": "architect", "at": "2026-01-01T00:00:00+00:00",
                    "design_hash": digest, **binding}
        path = self.document(self.block(items=items, design_review=review, design_approved=approval))
        result = run(["init", "Taken up", "--from-design", str(path)], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        self.assertEqual(status["phase_number"], 3, json.dumps(status, sort_keys=True))
        self.assertEqual(status.get("lane", "full"), "full")
        self.assertEqual(status.get("phases_waived", []), [])
        advanced = run(["advance", "4", "40"], self.tmp)
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["phase_number"], 4)
        self.assertEqual(self.read_status()["taken_up_from"]["inherited_waived"], [4, 5, 6, 7, 8])

    def test_completed_design_lane_round_trips_into_phase_three(self):
        source = self.tmp / "source"
        source.mkdir()
        shutil.copy(self.tmp / "handsoff.toml", source / "handsoff.toml")
        config = source / "handsoff.toml"
        config.write_text(config.read_text().replace('architect = "auto"', 'architect = "host"')
                          .replace("require_design_approval = true", "require_design_approval = false"))
        self.assertEqual(run(["init", "Source design", "--lane", "design", "--by", "architect"], source).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], source).returncode, 0)
        criterion = source / "criterion.json"
        criterion.write_text(json.dumps({"summary": "summary", "approach": ["Data shape: preserve the design record"],
                                         "tradeoffs": [], "decisions": ["Keep the review binding"],
                                         "constraints": [], "verification": ["Run the round trip test"]}))
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", "A round-trip criterion"], source).returncode, 0)
        self.assertEqual(run(["design-propose", "--file", str(criterion), "--by", "architect"], source).returncode, 0)
        self.assertEqual(run(["record-design-review", "--approve", "--by", "reviewer", "--architect", "architect",
                              "--summary", "independent"], source).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], source).returncode, 0)
        source_status = json.loads((source / "handsoff-status.json").read_text())
        source_acceptance = json.loads((source / "handsoff-acceptance.json").read_text())
        design = source / "design.html"
        destination = self.tmp / "destination"
        destination.mkdir()
        shutil.copy(config, destination / "handsoff.toml")
        result = run(["init", "Taken up", "--from-design", str(design)], destination)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        taken_status = json.loads((destination / "handsoff-status.json").read_text())
        taken_acceptance = json.loads((destination / "handsoff-acceptance.json").read_text())
        self.assertEqual(taken_status["phase_number"], 3)
        self.assertEqual(lib.design_hash(taken_acceptance["criteria"]), source_status["design_review"]["design_hash"])
        self.assertEqual(taken_status["taken_up_from"], {
            "path": str(design.resolve()), "sha256": hashlib.sha256(design.read_bytes()).hexdigest(),
            "source_design_hash": source_status["design_review"]["design_hash"],
            "inherited_waived": source_status["phases_waived"],
        })

    def test_escaped_script_payload_round_trips_criteria_through_take_up(self):
        source = self.tmp / "source-escaped"
        source.mkdir()
        shutil.copy(self.tmp / "handsoff.toml", source / "handsoff.toml")
        config = source / "handsoff.toml"
        config.write_text(config.read_text().replace('architect = "auto"', 'architect = "host"')
                          .replace("require_design_approval = true", "require_design_approval = false"))
        self.assertEqual(run(["init", "Escaped source", "--lane", "design", "--by", "architect"], source).returncode, 0)
        self.assertEqual(run(["advance", "2", "20"], source).returncode, 0)
        requirement = "Quote </script><b>x</b>"
        criterion = source / "criterion.json"
        criterion.write_text(json.dumps({"summary": "summary", "approach": ["Data shape: preserve the design record"],
                                         "tradeoffs": [], "decisions": ["Keep the review binding"],
                                         "constraints": [], "verification": ["Run the round trip test"]}))
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", requirement], source).returncode, 0)
        self.assertEqual(run(["design-propose", "--file", str(criterion), "--by", "architect"], source).returncode, 0)
        self.assertEqual(run(["record-design-review", "--by", "reviewer", "--architect", "architect",
                              "--approve", "--summary", "independent"], source).returncode, 0)
        self.assertEqual(run(["advance", "3", "30"], source).returncode, 0)
        destination = self.tmp / "destination-escaped"
        destination.mkdir()
        shutil.copy(config, destination / "handsoff.toml")
        self.assertEqual(run(["init", "Taken up", "--from-design", str(source / "design.html")], destination).returncode, 0)
        self.assertEqual(json.loads((destination / "handsoff-acceptance.json").read_text())["criteria"],
                         json.loads((source / "handsoff-acceptance.json").read_text())["criteria"])

    def test_explicit_take_up_item_keeps_phase_two_scope_gate(self):
        cfg = lib.load_config(self.tmp)
        criterion = self.block()["criteria"][0]
        items = []
        binding = {"config_hash": lib.config_hash(cfg),
                   "scope_hash": lib.work_item_scope_hash(items, [criterion])}
        digest = self.block()["design_hash"]
        review = {"decision": "approved", "by": "reviewer", "architect": "architect",
                  "at": "2026-01-01T00:00:00+00:00", "summary": "approved",
                  "design_hash": digest, **binding}
        path = self.document(self.block(items=items, design_review=review,
                                        design_approved={"by": "pilot", "architect": "architect",
                                                         "at": "2026-01-01T00:00:00+00:00", "design_hash": digest,
                                                         **binding}))
        result = run(["init", "Different scope", "--from-design", str(path), "--item", "new work"], self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read_status()["phase_number"], 2)
        refused = run(["advance", "3", "30"], self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("work-item scope changed since design review", refused.stdout)

    def test_refusals_leave_no_artifacts(self):
        cases = [
            ("no block", "<html>handsoff-design in prose</html>", "no handsoff-design block"),
            ("two", "<script type=\"application/json\" id=\"handsoff-design\">{}</script>" * 2, "more than one"),
            ("bad json", "<script type=\"application/json\" id=\"handsoff-design\">{</script>", "invalid JSON"),
        ]
        for name, text, reason in cases:
            with self.subTest(name=name):
                path = self.tmp / f"{name}.html"
                path.write_text(text)
                result = run(["init", name, "--from-design", str(path)], self.tmp)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stdout)
                self.assertFalse((self.tmp / "handsoff-status.json").exists())
                self.assertFalse((self.tmp / "handsoff-acceptance.json").exists())

    def test_hash_mismatch_names_criterion(self):
        block = self.block(design_hash="0" * 64,
                           criterion_hashes={"REQ-001": "0" * 64})
        path = self.document(block)
        result = run(["init", "Mismatch", "--from-design", str(path)], self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REQ-001", result.stdout)
        self.assertFalse((self.tmp / "handsoff-status.json").exists())


if __name__ == "__main__":
    unittest.main()
