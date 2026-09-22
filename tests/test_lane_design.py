"""Coverage for the run-level design lane (REQ-001/REQ-002)."""

import hashlib
import json
import os
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handsoff_supervisor import HandsoffTestCase, run
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import handsoff_lib as lib


class DesignLaneTests(HandsoffTestCase):
    def _digests(self):
        return {name: hashlib.sha256((self.tmp / name).read_bytes() if (self.tmp / name).exists() else b"<missing>").hexdigest()
                for name in ("handsoff-status.json", "handsoff-acceptance.json",
                             "handsoff-verifications.jsonl", "handsoff-events.jsonl")}

    def test_init_records_design_lane_and_full_is_legacy(self):
        result = run(["init", "Design lane", "--lane", "design", "--by", "architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        self.assertEqual(status["lane"], "design")
        self.assertEqual(status["phases_run"], [1, 2, 3])
        self.assertEqual(status["phases_waived"], [4, 5, 6, 7, 8])
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        event = next(e for e in events if e.get("kind") == "lane_selected")
        self.assertEqual(event["lane"], "design")
        self.assertEqual(event["actor"], "architect")

        other = self.tmp / "other"
        other.mkdir()
        (other / "handsoff.toml").write_text((self.tmp / "handsoff.toml").read_text())
        full = run(["init", "Full lane"], cwd=other)
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        plain = json.loads((other / "handsoff-status.json").read_text())
        self.assertNotIn("lane", plain)
        self.assertNotIn("phases_run", plain)
        self.assertNotIn("phases_waived", plain)

    def test_design_lane_refusals_are_transactional(self):
        result = run(["init", "Design lane", "--lane", "design", "--by", "architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in (
            ["advance", "4", "40"],
            ["record-review", "--by", "reviewer"],
            ["deployment-gate", "--approve", "--by", "pilot"],
            ["verify-live", "--by", "operator"],
        ):
            before = self._digests()
            refused = run(command, cwd=self.tmp)
            self.assertNotEqual(refused.returncode, 0, command)
            self.assertIn("design lane", refused.stdout.lower())
            self.assertEqual(before, self._digests(), command)

    def _at_design_phase(self):
        config = self.tmp / "handsoff.toml"
        config.write_text(config.read_text().replace(
            'architect = "auto"', 'architect = "host"').replace(
            "require_design_approval = true", "require_design_approval = false"))
        result = run(["init", "Design gate", "--lane", "design", "--by", "architect"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = run(["advance", "2", "20"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _record_review(self, reviewer="reviewer", architect="architect"):
        result = run(["criterion-update", "REQ-001", "--requirement", "A real design-lane criterion"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proposal = self.tmp / "proposal.json"
        proposal.write_text(json.dumps({
            "summary": "A design-lane proposal",
            "approach": ["Data shape: retain the existing run state"],
            "tradeoffs": [],
            "decisions": ["Use the shared design gate"],
            "constraints": [],
            "verification": ["Run the lane tests"],
        }))
        recorded_reviewer = reviewer if reviewer.strip().casefold() != architect.strip().casefold() else "independent-reviewer"
        with mock.patch.dict(os.environ, {
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_COMPANION_SESSION_ID": "design-lane-host",
        }, clear=True):
            result = run(["design-propose", "--file", str(proposal), "--by", architect], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with mock.patch.dict(os.environ, {
            "CLAUDE_CODE_SESSION_ID": "design-lane-reviewer",
            "CODEX_COMPANION_SESSION_ID": "",
        }, clear=True):
            result = run(["record-design-review", "--approve", "--by", recorded_reviewer,
                          "--architect", architect, "--summary", "independent"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if recorded_reviewer != reviewer:
            status = self.read_status()
            status["design_review"]["by"] = reviewer
            lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                       event_kind="test_self_review", event_message="Test self-review")

    def _advance_three(self):
        return run(["advance", "3", "30"], cwd=self.tmp)

    def test_design_lane_phase_three_requires_independent_review_transactionally(self):
        self._at_design_phase()
        before = self._digests()
        refused = self._advance_three()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("design review gate: Phase 3+ requires an approved independent design review", refused.stdout)
        self.assertEqual(before["handsoff-status.json"], self._digests()["handsoff-status.json"])
        self.assertEqual(before["handsoff-acceptance.json"], self._digests()["handsoff-acceptance.json"])
        self.assertEqual(before["handsoff-verifications.jsonl"], self._digests()["handsoff-verifications.jsonl"])

    def test_design_lane_phase_three_rejects_stale_review_hash(self):
        self._at_design_phase()
        self._record_review()
        recorded_review = dict(self.read_status()["design_review"])
        old_hash = recorded_review["design_hash"]
        result = run(["criterion-update", "REQ-001", "--requirement", "Changed after review"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.read_status()
        status["design_review"] = dict(recorded_review, design_hash=old_hash)
        # The mutation is committed by criterion-update; retain the recorded
        # review in this fixture so the transition exercises hash binding,
        # rather than merely the missing-record branch.
        lib.commit(self.tmp, lib.load_config(self.tmp), status=status,
                   event_kind="test_review_rebound", event_message="Test rebound review")
        refused = self._advance_three()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("criteria changed since design review", refused.stdout)

    def test_design_lane_phase_three_rejects_architect_as_reviewer(self):
        self._at_design_phase()
        self._record_review(reviewer=" ARCHITECT ", architect="architect")
        refused = self._advance_three()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("reviewer must differ from the architect", refused.stdout)

    def test_design_lane_phase_three_accepts_current_independent_review(self):
        self._at_design_phase()
        self._record_review(reviewer="independent-reviewer")
        advanced = self._advance_three()
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["phase_number"], 3)

    def test_design_lane_terminal_document_round_trips_and_is_deterministic(self):
        self._at_design_phase()
        self._record_review(reviewer="independent-reviewer")
        advanced = self._advance_three()
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        self.assertEqual(self.read_status()["status"], "design_complete")
        self.assertEqual(self.read_status()["design_document"], "design.html")
        document = (self.tmp / "design.html").read_bytes()
        blocks = re.findall(rb'<script type="application/json" id="handsoff-design">(.*?)</script>', document, re.S)
        self.assertEqual(len(blocks), 1)
        payload = json.loads(blocks[0])
        self.assertEqual(payload["schema"], 1)
        acceptance = self.read_acceptance()
        status = self.read_status()
        self.assertEqual(json.dumps(payload["criteria"], sort_keys=True, separators=(",", ":")),
                         json.dumps(acceptance["criteria"], sort_keys=True, separators=(",", ":")))
        self.assertEqual(payload["design_hash"], status["design_review"]["design_hash"])
        self.assertEqual(payload["design_review"], status["design_review"])
        self.assertEqual(document, lib.render_design_document(
            self.tmp, lib.load_config(self.tmp), self.read_status(), acceptance).encode())
        self.assertEqual(document, lib.render_design_document(
            self.tmp, lib.load_config(self.tmp), self.read_status(), acceptance).encode())
        for text in ("A real design-lane criterion", "independent-reviewer", "approved"):
            self.assertIn(text, document.decode())
        for key in ("approach", "tradeoffs", "decisions", "constraints", "verification"):
            for item in status["design_proposal"][key]:
                self.assertIn(item, document.decode())

    def test_script_payload_escapes_html_terminator_and_remains_deterministic(self):
        self._at_design_phase()
        requirement = "Quote </script><b>x</b> and Unicode \u2028/\u2029"
        self.assertEqual(run(["criterion-update", "REQ-001", "--requirement", requirement], self.tmp).returncode, 0)
        self._record_review()
        self.assertEqual(self._advance_three().returncode, 0)
        document = (self.tmp / "design.html").read_bytes()
        self.assertEqual(document.count(b"</script>"), 1)
        block = re.search(rb'<script type="application/json" id="handsoff-design">(.*?)</script>', document, re.S)
        self.assertIsNotNone(block)
        payload = json.loads(block.group(1))
        self.assertEqual(payload["criteria"], self.read_acceptance()["criteria"])
        rendered = lib.render_design_document(self.tmp, lib.load_config(self.tmp), self.read_status(), self.read_acceptance()).encode()
        self.assertEqual(document, rendered)


if __name__ == "__main__":
    unittest.main()
