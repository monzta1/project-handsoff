"""Field-note defect 4: the Phase 5 reviewer launch pre-check names every missing evidence kind."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest import TestCase

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class EvidenceGapTests(TestCase):
    def _criterion(self, cid, verification):
        return {"id": cid, "type": "supporting", "requirement": "r", "verification": verification, "tests": ["true"], "evidence": [], "state": "not_tested"}

    def _record(self, criterion, kind):
        return {"ok": True, "kind": kind, "criteria": [criterion["id"]], "criterion_hashes": {criterion["id"]: lib.criterion_spec_hash(criterion)}}

    def test_each_policy_names_its_missing_kinds(self):
        a = self._criterion("REQ-001", "automated")
        b = self._criterion("REQ-002", "browser")
        m = self._criterion("REQ-003", "manual")
        ab = self._criterion("REQ-004", "automated_and_browser")
        gaps = lib.reviewer_launch_evidence_gaps([a, b, m, ab], [])
        self.assertEqual(gaps, [
            "REQ-001: run handsoff_supervisor.py verify --criterion REQ-001 --by ACTOR",
            "REQ-002: run handsoff_supervisor.py record-evidence REQ-002 --kind browser --description ... --by ACTOR",
            "REQ-003: run handsoff_supervisor.py record-evidence REQ-003 --kind manual --description ... --by ACTOR",
            "REQ-004: run handsoff_supervisor.py record-evidence REQ-004 --kind browser --description ... --by ACTOR",
            "REQ-004: run handsoff_supervisor.py verify --criterion REQ-004 --by ACTOR",
        ])

    def test_combined_criterion_with_only_checks_is_a_gap_and_full_registry_passes(self):
        ab = self._criterion("REQ-004", "automated_and_browser")
        only_checks = [self._record(ab, "checks")]
        self.assertEqual(lib.reviewer_launch_evidence_gaps([ab], only_checks),
                         ["REQ-004: run handsoff_supervisor.py record-evidence REQ-004 --kind browser --description ... --by ACTOR"])
        both = only_checks + [self._record(ab, "browser")]
        self.assertEqual(lib.reviewer_launch_evidence_gaps([ab], both), [])
        # a stale-spec record does not count
        stale = dict(self._record(ab, "browser")); stale["criterion_hashes"] = {"REQ-004": "0" * 64}
        self.assertEqual(len(lib.reviewer_launch_evidence_gaps([ab], only_checks + [stale])), 1)


class ReviewerLaunchPrecheckTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def test_phase5_launch_refuses_until_every_kind_is_recorded(self):
        self.init("Precheck fixture")
        self.set_criterion_state("passing", resolved=True)
        added = run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "The page renders",
                     "--verification", "automated_and_browser", "--test", "true"], cwd=self.tmp)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        verified = run(["verify", "--criterion", "REQ-002", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        advanced = self.advance_to(5, implemented_by="test-implementer")
        self.assertEqual(advanced.returncode, 0, advanced.stdout + advanced.stderr)
        cfg = lib.load_config(self.tmp)
        cfg["agents"]["reviewer"] = "codex"
        with self.assertRaisesRegex(lib.HandsoffError, "record-evidence REQ-002 --kind browser"):
            runtime.build_launch_spec(self.tmp, "reviewer", "review", which=lambda x: "/bin/codex", skip_preflight=True)
        recorded = run(["record-evidence", "REQ-002", "--kind", "browser", "--description", "screenshot at docs/x.png", "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        spec = runtime.build_launch_spec(self.tmp, "reviewer", "review", which=lambda x: "/bin/codex", skip_preflight=True)
        self.assertEqual(spec.role, "reviewer")
