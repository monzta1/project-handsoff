"""#302: replay evaluation of a frozen cheaper routing policy.

Every fixture is built so the value it asserts can only come from the rule
under test: thirty late failures would sink the cheaper candidate's training
bound if they were split on session end, a fixture archive of failures would
do the same if it were counted, and a usage_partial record carrying a literal
zero would pull the input-token mean from 100 to 66.7 if it were averaged.
"""
import copy
import http.client
import json
import re
import random
import shutil
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_evidence as evidence  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_shadow as shadow  # noqa: E402
from tests.fixture_state import write_version_pin  # noqa: E402

UNKNOWN = evidence.UNKNOWN
BOUNDARY_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
BOUNDARY = BOUNDARY_AT.isoformat()
OPUS = {"adapter": "claude", "model": "claude-opus-5"}
HAIKU = {"adapter": "claude", "model": "claude-haiku-4-5-20251001"}
COSTS = [{**OPUS, "cost": 5.0}, {**HAIKU, "cost": 1.0}]
BASELINE = {"implementer": dict(OPUS)}
THRESHOLD = 0.85


def day(offset):
    """An ISO time `offset` days after the boundary (negative is before)."""
    return (BOUNDARY_AT + timedelta(days=offset)).isoformat()


def rec(n, *, profile=HAIKU, outcome="verified_phase8", available=-5.0, ended=None, role="implementer",
        task_class="bug", tokens=(100, 50, 150), flags=(), run_kind="product", repository="acme/app",
        policy="p1"):
    tokens_in, tokens_out, tokens_total = tokens
    record = {
        "derivation_version": evidence.DERIVATION_VERSION, "source_archive": f"run-{n}.json",
        "source_sha256": "a" * 64, "repository": repository, "engine_version": "0.4.9",
        "session_id": f"hs-{n}", "role": role, "task_class": task_class, "risk_class": "routine",
        "phase": 5, "adapter": profile["adapter"], "requested_model": profile["model"],
        "reported_model": UNKNOWN, "tier": UNKNOWN, "routed": False,
        "tokens": {"tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_total": tokens_total,
                   "source": "adapter"},
        "duration": {"wall_clock_ms": 60000, "active_ms": UNKNOWN},
        "retries": 0, "replacements": 0, "outcome": outcome, "policy_version": policy,
        "quality_flags": sorted(flags),
    }
    record["negative"] = outcome in evidence.NEGATIVE_OUTCOMES
    record["record_hash"] = evidence.record_hash(record)
    avail = None if available is None else day(available)
    return {"evidence": record, "run_kind": run_kind, "started_at": None,
            "ended_at": day(ended if ended is not None else (available if available is not None else -5)),
            "outcome_available_at": avail}


def many(count, start, **fields):
    return [rec(start + i, **fields) for i in range(count)]


def training_success(start=0, count=30, **fields):
    return many(count, start, **fields)


def evaluate(records, **kwargs):
    options = {"boundary": BOUNDARY, "threshold": THRESHOLD, "cost_table": COSTS, "baseline": BASELINE}
    options.update(kwargs)
    return shadow.evaluate(records, **options)


def cohort(report, key="acme/app|implementer|bug", variant="all_evidence"):
    return next(item for item in report["variants"][variant]["cohorts"] if item["cohort_key"] == key)


class OutcomeAvailabilitySplitTests(unittest.TestCase):
    """REQ-001: the split is on outcome availability, at a fixed boundary."""

    def test_a_session_ending_before_the_boundary_with_its_outcome_after_it_stays_out_of_training(self):
        # Thirty failures whose sessions ENDED well before the boundary but
        # whose Phase-8 outcome only became available after it. Split on
        # session end, they would join training and sink haiku's bound.
        late = many(30, 100, outcome="verification_failed", ended=-10, available=+3)
        report = evaluate(training_success() + late)
        item = cohort(report)
        self.assertEqual(item["n_training"], 30)
        self.assertEqual(item["n_holdout"], 30)
        self.assertEqual(item["recommendation"], "cheaper")
        self.assertEqual(item["training"]["n"], 30)
        self.assertEqual(item["holdout"]["policy"]["n"], 30)
        self.assertEqual(item["holdout"]["policy"]["successes"], 0)

    def test_records_with_unknown_availability_are_excluded_and_counted(self):
        unknown = many(7, 200, outcome="verification_failed", available=None)
        report = evaluate(training_success() + unknown)
        self.assertEqual(report["excluded"]["unknown_availability"], 7)
        self.assertEqual(report["included"], 30)
        self.assertEqual(cohort(report)["n"], 30)

    def test_the_boundary_is_a_recorded_input_and_an_offsetless_one_is_refused(self):
        report = evaluate(training_success())
        self.assertEqual(report["boundary"], BOUNDARY)
        self.assertEqual(report["inputs"]["boundary"], BOUNDARY)
        self.assertEqual(report["inputs"]["rule"], shadow.POLICY_RULE)
        with self.assertRaisesRegex(lib.HandsoffError, "boundary must be an ISO-8601 time with an offset"):
            evaluate(training_success(), boundary="2026-09-01T00:00:00")

    def test_changing_holdout_records_cannot_change_the_boundary_or_the_frozen_choice(self):
        training = training_success() + many(10, 300, profile=OPUS)
        good = many(20, 400, available=+2) + many(5, 500, profile=OPUS, available=+2)
        bad = many(20, 400, outcome="verification_failed", available=+2) \
            + many(9, 600, profile=OPUS, outcome="regression_failed", available=+4) \
            + many(12, 700, task_class="feature", available=+1)
        first, second = evaluate(training + good), evaluate(training + bad)
        self.assertEqual(first["boundary"], second["boundary"])
        self.assertEqual(first["inputs_sha256"], second["inputs_sha256"])
        for variant in shadow.VARIANTS:
            one, two = first["variants"][variant], second["variants"][variant]
            self.assertEqual(one["policy_sha256"], two["policy_sha256"])
            self.assertEqual(one["policy"], two["policy"])
        self.assertEqual(cohort(first)["choice"], HAIKU)
        self.assertEqual(cohort(second)["choice"], HAIKU)
        # the holdout really did differ, and was scored
        self.assertEqual(cohort(first)["holdout"]["policy"]["successes"], 20)
        self.assertEqual(cohort(second)["holdout"]["policy"]["successes"], 0)
        self.assertEqual(cohort(second)["holdout"]["baseline"]["n"], 9)

    def test_changing_training_records_can_change_the_frozen_choice(self):
        good = evaluate(training_success())
        worse = evaluate(training_success(count=20) + many(10, 50, outcome="review_changes_requested"))
        self.assertEqual(cohort(good)["choice"], HAIKU)
        self.assertEqual(cohort(worse)["choice"], OPUS)
        self.assertNotEqual(good["variants"]["all_evidence"]["policy_sha256"],
                            worse["variants"]["all_evidence"]["policy_sha256"])

    def test_holdout_reports_baseline_versus_policy(self):
        records = training_success() + many(8, 100, available=+1) \
            + many(6, 200, profile=OPUS, available=+1) + many(2, 300, profile=OPUS, outcome="failed_other",
                                                              available=+1)
        holdout = cohort(evaluate(records))["holdout"]
        self.assertEqual((holdout["policy"]["n"], holdout["policy"]["successes"]), (8, 8))
        self.assertEqual((holdout["baseline"]["n"], holdout["baseline"]["successes"]), (8, 6))
        self.assertEqual(holdout["baseline"]["success_rate"], {"value": 0.75, "value_state": "measured"})
        self.assertEqual(holdout["baseline"]["interval"],
                         {"value": list(shadow.wilson(6, 8)), "value_state": "measured"})
        self.assertLess(holdout["baseline"]["interval"]["value"][0], 0.75)


class ResultQualityTests(unittest.TestCase):
    """REQ-002: measured versus unavailable, fixtures, coverage, low quality."""

    def test_fixture_records_are_excluded_and_counted(self):
        fixtures = many(30, 100, outcome="verification_failed", run_kind="test")
        report = evaluate(training_success() + fixtures)
        self.assertEqual(report["excluded"]["fixture"], 30)
        self.assertEqual(cohort(report)["recommendation"], "cheaper")

    def test_values_without_data_are_unavailable_never_zero(self):
        item = cohort(evaluate(training_success() + many(4, 100, available=+1)))
        baseline = item["holdout"]["baseline"]
        self.assertEqual(baseline["n"], 0)
        self.assertEqual(baseline["success_rate"], {"value": None, "value_state": "unavailable"})
        self.assertEqual(baseline["interval"], {"value": None, "value_state": "unavailable"})
        self.assertEqual(baseline["tokens"]["mean_tokens_total"], {"value": None, "value_state": "unavailable"})
        self.assertEqual(item["holdout"]["policy"]["success_rate"], {"value": 1.0, "value_state": "measured"})

    def test_reports_size_coverage_interval_versions_and_missing_data(self):
        records = training_success(count=28) + many(2, 100, tokens=(UNKNOWN, UNKNOWN, UNKNOWN),
                                                     flags=("usage_not_reported",), outcome=UNKNOWN)
        report = evaluate(records)
        item = cohort(report)
        self.assertEqual(item["n"], 30)
        self.assertEqual(item["coverage"]["coverage"], {"value": 28 / 30, "value_state": "measured"})
        self.assertEqual(item["policy_version"], shadow.SHADOW_POLICY_VERSION)
        self.assertEqual(item["routing_policy_versions"], ["p1"])
        self.assertEqual(item["model_versions"], [HAIKU["model"]])
        self.assertAlmostEqual(item["missing_data"]["outcome"]["value"], 2 / 30)
        self.assertAlmostEqual(item["missing_data"]["full_usage"]["value"], 2 / 30)
        self.assertEqual(item["missing_data"]["duration"], {"value": 0.0, "value_state": "measured"})
        lower = shadow.wilson(28, 28)[0]
        self.assertEqual(item["training"]["lower_bound"], lower)
        self.assertLess(lower, 1.0)
        self.assertEqual(report["policy_version"], shadow.SHADOW_POLICY_VERSION)

    def test_results_are_reported_with_and_without_low_quality_evidence(self):
        legacy = many(15, 100, outcome="verification_failed", flags=("legacy_archive",))
        report = evaluate(training_success() + legacy)
        self.assertEqual(set(report["variants"]), set(shadow.VARIANTS))
        self.assertEqual(cohort(report, variant="all_evidence")["recommendation"], "refused")
        self.assertEqual(cohort(report, variant="all_evidence")["n"], 45)
        self.assertEqual(cohort(report, variant="excluding_low_quality")["recommendation"], "cheaper")
        self.assertEqual(cohort(report, variant="excluding_low_quality")["n"], 30)

    def test_the_scorer_refuses_below_the_full_usage_coverage_floor_naming_the_shortfall(self):
        partial = many(30, 100, tokens=(UNKNOWN, UNKNOWN, 900), flags=("usage_partial",))
        records = many(10, 0) + partial
        with self.assertRaises(shadow.ShadowRefusal) as caught:
            evaluate(records)
        message = str(caught.exception)
        self.assertIn("full-usage coverage 0.2500 is below the declared floor 0.5000", message)
        self.assertIn("10 of 40 records", message)
        self.assertIn("10 more full-usage record(s) needed", message)
        self.assertEqual(shadow.DEFAULT_MIN_FULL_USAGE_COVERAGE, 0.5)
        # the floor is the declared input, not a constant
        self.assertEqual(evaluate(records, min_full_usage_coverage=0.25)["included"], 40)

    def test_usage_partial_never_contributes_zero_for_a_missing_half(self):
        records = training_success() + many(2, 100, available=+1, tokens=(100, 50, 150)) + [
            rec(200, available=+1, tokens=(0, 50, 900), flags=("usage_partial",)),
            rec(201, available=+1, tokens=(UNKNOWN, 0, 900), flags=("usage_partial",)),
        ]
        tokens = cohort(evaluate(records, min_full_usage_coverage=0.0))["holdout"]["policy"]["tokens"]
        self.assertEqual(tokens["n_known"], 2)
        self.assertEqual(tokens["mean_tokens_in"], {"value": 100.0, "value_state": "measured"})
        self.assertEqual(tokens["mean_tokens_out"], {"value": 50.0, "value_state": "measured"})
        self.assertEqual(tokens["mean_tokens_total"], {"value": 150.0, "value_state": "measured"})
        self.assertFalse(shadow.full_usage(records[-2]["evidence"]))


class RecommendationFixtureTests(unittest.TestCase):
    """REQ-003: the cheaper model only when its training bound meets the threshold."""

    def test_cheaper_model_recommended_when_its_bound_meets_the_threshold(self):
        report = evaluate(training_success() + many(10, 100, profile=OPUS))
        findings = report["variants"]["all_evidence"]["findings"]
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertGreaterEqual(finding["training"]["lower_bound"], THRESHOLD)
        self.assertEqual(finding["recommendation"], "cheaper")
        self.assertEqual(finding["cohort_key"], "acme/app|implementer|bug")
        self.assertEqual(finding["policy_version"], shadow.SHADOW_POLICY_VERSION)
        self.assertRegex(finding["finding_id"], r"^shf-[a-z0-9]{12}$")
        self.assertEqual(finding["change"], {"kind": "agent_profile", "role": "implementer",
                                             "from": OPUS, "to": HAIKU})
        self.assertEqual(finding["change_sha256"], shadow._sha256(finding["change"]))

    def test_cheaper_model_refused_below_the_threshold_with_the_gap_named(self):
        report = evaluate(training_success(count=24) + many(6, 100, outcome="verification_failed"))
        finding = report["variants"]["all_evidence"]["findings"][0]
        lower = shadow.wilson(24, 30)[0]
        self.assertEqual(finding["recommendation"], "refused")
        self.assertIsNone(finding["change"])
        self.assertIsNone(finding["change_sha256"])
        self.assertEqual(finding["cohort_key"], "acme/app|implementer|bug")
        self.assertEqual(finding["policy_version"], shadow.SHADOW_POLICY_VERSION)
        self.assertAlmostEqual(finding["gap"]["shortfall"], THRESHOLD - lower)
        self.assertIn(f"{HAIKU['adapter']}/{HAIKU['model']}", finding["gap"]["reason"])
        self.assertIn("below the threshold", finding["gap"]["reason"])
        self.assertEqual(cohort(report)["choice"], OPUS)

    def test_the_fixture_is_reproducible_in_any_record_order(self):
        records = training_success() + many(10, 100, profile=OPUS) + many(5, 200, available=+2)
        shuffled = list(records)
        random.Random(302).shuffle(shuffled)
        self.assertEqual(evaluate(records)["report_sha256"], evaluate(shuffled)["report_sha256"])

    def test_the_baseline_alone_yields_no_finding(self):
        report = evaluate(many(30, 0, profile=OPUS))
        self.assertEqual(cohort(report)["recommendation"], "baseline")
        self.assertEqual(report["variants"]["all_evidence"]["findings"], [])


class RoutingUntouchedAndApprovalTests(HandsoffTestCase):
    """REQ-001 shadow mode, and REQ-003 apply only with an endpoint approval."""

    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        cfg = lib.load_config(self.tmp)
        assignments = {role: {"adapter": cfg["agents"][role], "model": cfg["models"][role]}
                       for role in lib.agent_profiles(cfg)}
        assignments["implementer"] = dict(OPUS)
        lib.update_agent_config(self.tmp, assignments)

    def toml(self):
        return (self.tmp / "handsoff.toml").read_bytes()

    def report(self):
        records = training_success() + many(30, 100, task_class="feature") + many(10, 200, profile=OPUS)
        return shadow.evaluate_project(self.tmp, records, boundary=BOUNDARY, threshold=THRESHOLD,
                                       cost_table=COSTS)

    def finding(self, report, task_class="bug"):
        return next(item for item in report["variants"]["all_evidence"]["findings"]
                    if item["cohort_key"] == f"acme/app|implementer|{task_class}")

    def write_report(self, report, name="report.json"):
        path = self.tmp / name
        path.write_text(json.dumps(report))
        return path

    def apply(self, report_path, finding_id, approval_id, *extra):
        return run(["shadow-apply", "--report", str(report_path), "--finding", finding_id,
                    "--approval", approval_id, *extra], cwd=self.tmp)

    def serve(self):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, method, path, body=None):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"}
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def page_token(self, server):
        """The Pilot token as Mission Control gets it: from its own page."""
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/")
        html = connection.getresponse().read().decode("utf-8")
        connection.close()
        found = re.search(r'<meta name="handsoff-pilot-token" content="([0-9a-f]+)">', html)
        self.assertIsNotNone(found, "the page carries the Pilot token")
        return found.group(1)

    def approve(self, server, finding, token=None):
        if token is None:
            token = self.page_token(server)
        return self.request(server, "POST", "/api/shadow-approval", {"pilot_token": token, "finding": finding})

    def test_no_api_hands_out_the_pilot_token(self):
        # implementation review attempt 1: GET /api/pilot-token returned the
        # token to any caller, who could then mint approvals
        server = self.serve()
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/api/pilot-token", headers={"Origin": "http://evil.example"})
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        connection.close()
        self.assertNotEqual(response.status, 200)
        self.assertNotIn(server.pilot_token, body)

    def test_a_cross_origin_approval_is_refused_even_with_the_token(self):
        server = self.serve()
        finding = self.finding(self.report())
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("POST", "/api/shadow-approval",
                           body=json.dumps({"pilot_token": self.page_token(server), "finding": finding}),
                           headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertFalse(list(shadow.approvals_dir(self.tmp).glob("apr-*.json")))

    def test_evaluation_and_shadow_mode_leave_routing_config_and_the_routed_choice_unchanged(self):
        before = (self.toml(), shadow.routing_config_sha256(self.tmp), shadow.routed_choice(self.tmp))
        report = self.report()
        self.assertEqual(report["inputs"]["baseline"]["implementer"], OPUS)
        view = shadow.shadow_route(self.tmp, report, role="implementer", repository="acme/app", task_class="bug")
        self.assertEqual(view["routed"], OPUS)
        self.assertEqual(view["shadow"], HAIKU)
        self.assertFalse(view["agrees"])
        after = (self.toml(), shadow.routing_config_sha256(self.tmp), shadow.routed_choice(self.tmp))
        self.assertEqual(before, after)

    def test_the_routing_guard_fails_an_action_that_changes_routing(self):
        def mutate():
            cfg = lib.load_config(self.tmp)
            assignments = {role: {"adapter": cfg["agents"][role], "model": cfg["models"][role]}
                           for role in lib.agent_profiles(cfg)}
            assignments["implementer"] = dict(HAIKU)
            lib.update_agent_config(self.tmp, assignments)
        with self.assertRaisesRegex(shadow.ShadowRefusal, "routing configuration changed"):
            shadow.routing_unchanged(self.tmp, mutate)

    def test_a_wrong_pilot_token_records_nothing(self):
        server = self.serve()
        finding = self.finding(self.report())
        status, payload = self.approve(server, finding, token="0" * 32)
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertFalse(list(shadow.approvals_dir(self.tmp).glob("apr-*.json")))

    def test_an_endpoint_created_approval_applies_once(self):
        server = self.serve()
        report = self.report()
        finding = self.finding(report)
        status, payload = self.approve(server, finding)
        self.assertEqual(status, 200, payload)
        approval = payload["approval"]
        self.assertEqual(approval["finding_id"], finding["finding_id"])
        self.assertEqual(approval["change_sha256"], finding["change_sha256"])
        self.assertEqual(approval["source"], "mission_control")
        path = self.write_report(report)
        before = lib.agent_profiles(lib.load_config(self.tmp))
        applied = self.apply(path, finding["finding_id"], approval["approval_id"])
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("SHADOW_APPLIED", applied.stdout)
        after = lib.agent_profiles(lib.load_config(self.tmp))
        self.assertEqual(after["implementer"], HAIKU)
        self.assertEqual({r: p for r, p in after.items() if r != "implementer"},
                         {r: p for r, p in before.items() if r != "implementer"})
        again = self.apply(path, finding["finding_id"], approval["approval_id"])
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already used", again.stdout)

    def test_a_forged_cli_approval_is_refused(self):
        report = self.report()
        finding = self.finding(report)
        path = self.write_report(report)
        before = self.toml()
        # An actor name on the command line is not an approval: there is no such flag.
        named = self.apply(path, finding["finding_id"], "apr-aaaaaaaaaaaa", "--by", "Mission Control Pilot")
        self.assertNotEqual(named.returncode, 0)
        self.assertIn("unrecognized arguments", named.stderr)
        # A well-formed approval file written by hand was never recorded by Mission Control.
        forged = {"approval_id": "apr-aaaaaaaaaaaa", "finding_id": finding["finding_id"],
                  "change_sha256": finding["change_sha256"], "policy_version": finding["policy_version"],
                  "created_at": datetime.now(timezone.utc).isoformat(), "source": "mission_control"}
        shadow.approvals_dir(self.tmp).mkdir(parents=True, exist_ok=True)
        (shadow.approvals_dir(self.tmp) / "apr-aaaaaaaaaaaa.json").write_text(json.dumps(forged))
        written = self.apply(path, finding["finding_id"], "apr-aaaaaaaaaaaa")
        self.assertNotEqual(written.returncode, 0)
        self.assertIn("was not recorded by Mission Control", written.stdout)
        self.assertEqual(self.toml(), before)

    def test_a_missing_approval_is_refused(self):
        report = self.report()
        finding = self.finding(report)
        before = self.toml()
        result = self.apply(self.write_report(report), finding["finding_id"], "apr-bbbbbbbbbbbb")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no Mission Control approval apr-bbbbbbbbbbbb exists", result.stdout)
        self.assertEqual(self.toml(), before)

    def test_an_approval_reused_for_a_different_finding_or_change_is_refused(self):
        server = self.serve()
        report = self.report()
        bug, feature = self.finding(report, "bug"), self.finding(report, "feature")
        self.assertNotEqual(bug["finding_id"], feature["finding_id"])
        status, payload = self.approve(server, bug)
        self.assertEqual(status, 200, payload)
        approval_id = payload["approval"]["approval_id"]
        before = self.toml()
        other_finding = self.apply(self.write_report(report), feature["finding_id"], approval_id)
        self.assertNotEqual(other_finding.returncode, 0)
        self.assertIn(f"is bound to finding {bug['finding_id']}", other_finding.stdout)
        tampered = copy.deepcopy(report)
        changed = self.finding(tampered, "bug")
        changed["change"]["to"] = {"adapter": "claude", "model": "claude-sonnet-5"}
        changed["change_sha256"] = shadow._sha256(changed["change"])
        other_change = self.apply(self.write_report(tampered, "tampered.json"), bug["finding_id"], approval_id)
        self.assertNotEqual(other_change.returncode, 0)
        self.assertIn("is bound to a different change", other_change.stdout)
        self.assertEqual(self.toml(), before)


if __name__ == "__main__":
    unittest.main()
