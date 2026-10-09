#!/usr/bin/env python3
"""P1.5: browser QA reports are untrusted until another actor maps them.

`qa-report add --file REPORT.json --target URL --by AGENT` stores a QA
agent's report (target, steps, findings, artifacts, generated tests) as a
side record under .handsoff-qa/ with an id, never as evidence. The target's
origin must be an exact [qa].targets entry (default empty, so nothing is
accepted until configured). `qa-report map --report ID --criterion CID --by
REVIEWER` records browser evidence citing the report, refused for the
report's author. Unmapped reports count toward no gate; status and the
dashboard list them as pending.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.guards import guard
from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_workflow as workflow  # noqa: E402

LOCAL = "http://localhost:5173"
REPORT = {
    "steps": ["open /login", "submit the empty form"],
    "findings": [{"severity": "minor", "text": "the error line is grey on grey"}],
    "artifacts": ["artifacts/login.png"],
    "generated_tests": ["tests/e2e/login.spec.ts"],
}


class QaFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(tempfile.mkdtemp(prefix="handsoff-qa-reports-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.report_file = self.scratch / "report.json"
        self.report_file.write_text(json.dumps(REPORT))
        self.init("P1.5 browser QA reports")
        self._ok(run(["criterion-update", "REQ-001", "--requirement", "P1.5 the login page is readable",
                      "--verification", "browser", "--test", "walk the login page"], cwd=self.tmp))

    def allow(self, *origins):
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + f"\n[qa]\ntargets = {json.dumps(list(origins))}\n")

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _refused(self, result, needle):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(needle, result.stdout + result.stderr)
        return result

    def add(self, target=LOCAL + "/login", by="qa-agent"):
        return run(["qa-report", "add", "--file", str(self.report_file), "--target", target, "--by", by],
                   cwd=self.tmp)

    def added_id(self):
        line = self._ok(self.add()).stdout.strip().splitlines()[-1]
        self.assertTrue(line.startswith("QA_REPORT_STORED: "), line)
        return line.split()[1]

    def _criterion(self, cid="REQ-001"):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def _verifications(self):
        path = self.tmp / "handsoff-verifications.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


class TestAddingReports(QaFixture):
    def test_an_allowlisted_report_is_stored_untrusted_and_counts_for_nothing(self):
        self.allow(LOCAL)
        records_before = self._verifications()
        report_id = self.added_id()
        stored = json.loads((self.tmp / ".handsoff-qa" / f"{report_id}.json").read_text())
        self.assertEqual((stored["id"], stored["author"], stored["target"], stored["trusted"]),
                         (report_id, "qa-agent", LOCAL + "/login", False))
        for field, value in REPORT.items():
            self.assertEqual(stored[field], value, field)
        # never evidence: no ledger record, the criterion untouched, the gate still unmet
        self.assertEqual(self._verifications(), records_before)
        criterion = self._criterion()
        self.assertEqual((criterion["state"], criterion["evidence"]), ("not_tested", []))
        self.assertFalse(lib.criterion_fully_evidenced(criterion, self._verifications()))
        self.assertEqual(workflow._evidence_errors([dict(criterion, state="passing")], self._verifications()),
                         ["evidence gate: passing criterion REQ-001 lacks valid browser evidence"])
        status = json.loads(self._ok(run(["status"], cwd=self.tmp)).stdout)
        self.assertEqual([(r["id"], r["author"], r["findings"], r["mapped"]) for r in status["qa_reports_pending"]],
                         [(report_id, "qa-agent", 1, [])])
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])
        self.assertIn('"qa_report_added"', (self.tmp / "handsoff-events.jsonl").read_text())

    def test_a_target_outside_the_allowlist_is_refused(self):
        # default: [qa].targets is empty, so nothing is accepted
        self.assertEqual(lib.load_config(self.tmp)["qa_targets"], [])
        self._refused(self.add(), "not an allowlisted [qa].targets origin (none configured)")
        self.allow(LOCAL)
        for target in ("https://app.example.com/login", "http://localhost:5174/login",
                       "https://localhost:5173/login", "http://localhost.evil.com:5173/", "not a url"):
            self._refused(self.add(target=target), "not an allowlisted [qa].targets origin")
        self.assertFalse((self.tmp / ".handsoff-qa").exists() and any((self.tmp / ".handsoff-qa").iterdir()))
        self.assertNotIn("qa_reports", self.read_status())

    def test_qa_targets_are_exact_origins(self):
        self.allow("http://localhost:5173/login")
        with self.assertRaises(lib.HandsoffError) as raised:
            lib.load_config(self.tmp)
        self.assertIn("qa.targets", str(raised.exception))

    def test_port_zero_never_matches_a_portless_origin(self):
        """E3 review: localhost:0 used to normalise to localhost."""
        self.allow("http://localhost")
        self._refused(self.add(target="http://localhost:0/login"), "not an allowlisted [qa].targets origin")
        with self.assertRaises(lib.HandsoffError) as raised:
            lib.normalize_public_origins(["http://localhost:0"], "handsoff.toml: qa.targets")
        self.assertIn("port 0", str(raised.exception))

    def test_a_malformed_report_is_refused(self):
        self.allow(LOCAL)
        self.report_file.write_text(json.dumps({"steps": "not a list"}))
        self._refused(self.add(), "'steps' must be a list")
        self.report_file.write_text("[1, 2]")
        self._refused(self.add(), "must be a JSON object")


class TestMappingReports(QaFixture):
    def test_mapping_by_the_author_is_refused(self):
        self.allow(LOCAL)
        report_id = self.added_id()
        for author in ("qa-agent", "QA-Agent", " qa-agent "):
            self._refused(run(["qa-report", "map", "--report", report_id, "--criterion", "REQ-001",
                               "--by", author], cwd=self.tmp), "a different actor must map it")
        self.assertEqual(self._verifications(), [])
        self.assertEqual(self._criterion()["state"], "not_tested")

    def test_mapping_by_another_actor_records_browser_evidence_citing_the_report(self):
        self.allow(LOCAL)
        report_id = self.added_id()
        out = self._ok(run(["qa-report", "map", "--report", report_id, "--criterion", "REQ-001",
                            "--by", "test-reviewer"], cwd=self.tmp)).stdout
        record = self._verifications()[-1]
        self.assertIn(f"EVIDENCE_RECORDED: {record['run_id']}", out)
        self.assertEqual((record["kind"], record["ok"], record["by"], record["criteria"]),
                         ("browser", True, "test-reviewer", ["REQ-001"]))
        self.assertEqual(record["results"][0]["qa_report"], report_id)
        self.assertIn(report_id, record["description"])
        criterion = self._criterion()
        self.assertEqual(criterion["state"], "passing")
        self.assertIn(record["run_id"], criterion["evidence"])
        entry = self.read_status()["qa_reports"][0]
        self.assertEqual([(m["criterion"], m["by"], m["run_id"]) for m in entry["mapped"]],
                         [("REQ-001", "test-reviewer", record["run_id"])])
        status = json.loads(self._ok(run(["status"], cwd=self.tmp)).stdout)
        self.assertEqual(status["qa_reports_pending"], [], "a mapped report is no longer pending")
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])

    def test_mapping_refuses_an_unknown_report_a_tampered_report_and_a_non_browser_criterion(self):
        self.allow(LOCAL)
        self._refused(run(["qa-report", "map", "--report", "qa-000000000000", "--criterion", "REQ-001",
                           "--by", "test-reviewer"], cwd=self.tmp), "unknown QA report")
        report_id = self.added_id()
        self._ok(run(["criterion-add", "REQ-002", "--type", "supporting", "--requirement", "P1.5 manual only",
                      "--verification", "manual", "--test", "read it"], cwd=self.tmp))
        self._refused(run(["qa-report", "map", "--report", report_id, "--criterion", "REQ-002",
                           "--by", "test-reviewer"], cwd=self.tmp), "does not accept browser evidence")
        path = self.tmp / ".handsoff-qa" / f"{report_id}.json"
        stored = json.loads(path.read_text())
        stored["findings"] = []
        path.write_text(json.dumps(stored))
        self._refused(run(["qa-report", "map", "--report", report_id, "--criterion", "REQ-001",
                           "--by", "test-reviewer"], cwd=self.tmp), "changed since it was stored")
        self.assertEqual(self._verifications(), [])


class TestLegacyBrowserEvidence(QaFixture):
    def test_legacy_browser_record_evidence_is_unchanged(self):
        self._ok(run(["record-evidence", "REQ-001", "--kind", "browser", "--description", "walked the login page",
                      "--by", "test-implementer"], cwd=self.tmp))
        record = self._verifications()[-1]
        self.assertEqual((record["kind"], record["by"], record["results"]), ("browser", "test-implementer", []))
        self.assertEqual(self._criterion()["state"], "passing")
        self.assertNotIn("qa_reports", self.read_status())


class TestDashboardListsPendingReports(unittest.TestCase):
    @guard
    def test_the_dashboard_renders_pending_reports_from_the_status(self):
        app = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        logic = (ROOT / "dashboard" / "lib" / "dashboard-logic.js").read_text(encoding="utf-8")
        page = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("renderQaReports(status);", app)
        self.assertIn("function pendingQaReports(status)", logic)
        self.assertIn("status?.qa_reports", logic)
        for element in ("qa-reports-panel", "qa-reports-total", "qa-reports-list"):
            self.assertIn(f'id="{element}"', page)
        schema = json.loads((ROOT / "schemas" / "status.schema.json").read_text())
        self.assertIn("qa_reports", schema["properties"])


if __name__ == "__main__":
    unittest.main()
