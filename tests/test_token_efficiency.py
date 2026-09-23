import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import handsoff_lib as lib


class TokenEfficiencyTests(unittest.TestCase):
    def test_routine_small_work_is_lower_than_shared_infrastructure(self):
        routine = lib.plan_role_token_budget(
            configured_ceiling=120_000, role="implementer", risk_class="routine",
            packet_bytes=2_000, criteria_count=1,
        )
        shared = lib.plan_role_token_budget(
            configured_ceiling=120_000, role="implementer", risk_class="shared_infrastructure",
            packet_bytes=20_000, criteria_count=12, changed_files=8,
        )
        self.assertLess(routine["ceiling"], shared["ceiling"])
        self.assertGreaterEqual(routine["ceiling"], routine["floor"])

    def test_budget_never_exceeds_configured_ceiling_and_basis_is_explicit(self):
        decision = lib.plan_role_token_budget(
            configured_ceiling=30_000, role="reviewer", risk_class="irreversible",
            packet_bytes=100_000, criteria_count=64, changed_files=32,
        )
        self.assertEqual(decision["ceiling"], 30_000)
        self.assertEqual(decision["basis"], "risk_role_packet_scope")
        self.assertEqual(lib.validate_session_budget_decision(decision), decision)

    def test_followup_packet_receives_less_discovery_headroom(self):
        common = dict(configured_ceiling=120_000, role="reviewer", risk_class="elevated",
                      packet_bytes=8_000, criteria_count=4)
        self.assertLess(
            lib.plan_role_token_budget(**common, followup=True)["ceiling"],
            lib.plan_role_token_budget(**common, followup=False)["ceiling"],
        )

    def test_broad_high_risk_first_review_keeps_configured_ceiling(self):
        decision = lib.plan_role_token_budget(
            configured_ceiling=80_000, role="reviewer",
            risk_class="shared_infrastructure", packet_bytes=15_000,
            criteria_count=15, changed_files=12, followup=False,
        )
        self.assertEqual(decision["ceiling"], 80_000)
        followup = lib.plan_role_token_budget(
            configured_ceiling=80_000, role="reviewer",
            risk_class="shared_infrastructure", packet_bytes=5_000,
            criteria_count=15, changed_files=2, followup=True,
        )
        self.assertEqual(followup["ceiling"], 80_000)


if __name__ == "__main__":
    unittest.main()
