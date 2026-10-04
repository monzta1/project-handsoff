"""#347 REQ-005 and REQ-009: reasoning effort is pinned per role, not inherited.

Before this, a managed Codex role ran at whatever `model_reasoning_effort` the
operator's `~/.codex/config.toml` happened to set. Nothing in `handsoff.toml`
could pin it and nothing recorded what was used, so two runs of the same lane
on the same engine with the same project config could do different work for a
reason the ledger could not explain. One field report observed every implementer
running at `low` and only discovered it by reading the Codex session output.

The criteria these tests bind are deliberately specific about two things the
design review caught:

- **Both launch builders.** `build_launch_spec` and `build_profile_launch_spec`
  each construct their own argv, and the second is the failover path taken after
  a role's prior session failed. A flag threaded into one leaves every
  failed-over role running at the inherited effort, which is the original bug.
- **Archive compatibility.** `reasoning_effort` joins the CLOSED optional field
  set, so a session recorded before the field existed stays valid. The engine
  validates session records against exact field sets, so an unconditional field
  would invalidate every archive on disk.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT
from tests.guards import guard

sys.path.insert(0, str(BIN))
import handsoff_agent as agent  # noqa: E402
import handsoff_config as config  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_schema as schema  # noqa: E402


def project_with(models_line=None):
    """A project whose handsoff.toml is this repo's, plus one [models] line.

    The line is inserted INTO the existing table rather than appended as a new
    one: a second `[models]` is a duplicate-table TOML error, which the first
    draft of this fixture hit and which looks exactly like a validation refusal.
    """
    root = Path(tempfile.mkdtemp(prefix="handsoff-test-reasoning-"))
    text = (ROOT / "handsoff.toml").read_text(encoding="utf-8")
    if models_line:
        text = text.replace("[models]\n", f"[models]\n{models_line}\n", 1)
    (root / "handsoff.toml").write_text(text, encoding="utf-8")
    return root


class TheConfigPinsItPerRole(unittest.TestCase):
    """REQ-005, the config half."""

    def test_a_permitted_effort_is_accepted_for_the_named_role(self):
        cfg = lib.load_config(project_with('implementer_reasoning = "high"'))
        self.assertEqual(cfg["model_reasoning"], {"implementer": "high"})

    def test_every_permitted_value_is_accepted(self):
        for effort in config.REASONING_EFFORTS:
            with self.subTest(effort=effort):
                cfg = lib.load_config(project_with(f'reviewer_reasoning = "{effort}"'))
                self.assertEqual(cfg["model_reasoning"]["reviewer"], effort)

    def test_an_unpermitted_value_is_refused_by_name(self):
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.load_config(project_with('implementer_reasoning = "turbo"'))
        message = str(caught.exception)
        self.assertIn("models.implementer_reasoning", message,
                      "the refusal must name the key so it is fixable")
        self.assertIn("must be one of", message)

    def test_a_misspelled_role_is_refused_rather_than_ignored(self):
        """There is no unknown-key check on [models], so without this a typo
        reads as nothing at all and the operator believes it took effect."""
        with self.assertRaises(lib.HandsoffError) as caught:
            lib.load_config(project_with('implementr_reasoning = "high"'))
        self.assertIn("names no role", str(caught.exception))
        self.assertIn("implementr_reasoning", str(caught.exception))

    def test_absent_means_absent_not_a_default_effort(self):
        """A project that sets nothing must behave exactly as it did before
        this feature existed, which is the adapter deciding for itself."""
        self.assertEqual(lib.load_config(project_with())["model_reasoning"], {})

    def test_one_role_pinned_leaves_the_others_unpinned(self):
        cfg = lib.load_config(project_with('architect_reasoning = "medium"'))
        self.assertEqual(cfg["model_reasoning"], {"architect": "medium"})
        for role in ("implementer", "reviewer", "supervisor"):
            self.assertNotIn(role, cfg["model_reasoning"])


class TheArgvCarriesIt(unittest.TestCase):
    """REQ-005, the argv half, including the precedence claim."""

    def test_a_pinned_effort_appears_in_the_codex_argv(self):
        argv = lib.codex_argv("codex", "implementer", "gpt-6-astra", 50_000, reasoning="high")
        self.assertIn("model_reasoning_effort=high", argv)

    def test_no_pin_adds_no_flag_at_all(self):
        """Not `=default`, not an empty value: the flag is absent, so the
        adapter's own resolution is untouched."""
        argv = lib.codex_argv("codex", "implementer", "gpt-6-astra", 50_000)
        self.assertFalse([item for item in argv if "reasoning" in item])

    def test_the_project_value_is_what_the_argv_carries(self):
        """The precedence claim. The engine passes the project's value on the
        command line, which is what makes a managed launch reproducible: the
        operator's global ~/.codex/config.toml cannot reach past an explicit
        flag, and only an explicit flag can be asserted here."""
        for effort in config.REASONING_EFFORTS:
            with self.subTest(effort=effort):
                argv = lib.codex_argv("codex", "implementer", "m", 50_000, reasoning=effort)
                settings = [item for item in argv if item.startswith("model_reasoning_effort=")]
                self.assertEqual(settings, [f"model_reasoning_effort={effort}"],
                                 "exactly one effort reaches the adapter")

    @guard
    def test_both_launch_builders_read_the_same_config_key(self):
        """The failover path is the one that regresses silently. Asserted on
        the source because building a real failover spec needs a live adapter,
        and the defect is precisely that one of the two sites is forgotten."""
        source = (BIN / "handsoff_agent.py").read_text(encoding="utf-8")
        threaded = source.count('reasoning=cfg["model_reasoning"].get(role)')
        self.assertEqual(threaded, 2,
                         "both _codex_argv call sites must pass the pinned effort; "
                         f"found {threaded}. build_profile_launch_spec is the failover "
                         "path and leaving it out reproduces #347 for every role that "
                         "ever fails over.")
        specs = source.count('reasoning_effort=cfg["model_reasoning"].get(role)')
        self.assertEqual(specs, 2, "both LaunchSpec constructions must carry the effort")


class TheSessionRecordsWhatItRanAt(unittest.TestCase):
    """REQ-009, the record half."""

    def test_the_field_is_optional_so_older_archives_stay_valid(self):
        self.assertIn("reasoning_effort", schema.AGENT_SESSION_OPTIONAL_FIELDS)
        self.assertNotIn("reasoning_effort", schema.AGENT_SESSION_FIELDS - schema.AGENT_SESSION_OPTIONAL_FIELDS)

    def test_a_real_archived_session_without_the_field_still_validates(self):
        """The compatibility claim, against a status the engine really wrote."""
        baseline = ROOT / "tests" / "fixtures" / "status_governance_baseline.json"
        if not baseline.is_file():
            self.skipTest("the governance baseline fixture is not in this checkout")
        status = json.loads(baseline.read_text(encoding="utf-8"))
        sessions = status.get("agent_sessions") or {}
        self.assertTrue(sessions, "the fixture carries no sessions to prove the claim")
        for session in sessions.values():
            self.assertNotIn("reasoning_effort", session)
        self.assertEqual(schema.validate_status_schema(status), [],
                         "a pre-field archive stopped validating when the field was added")

    def test_the_journey_leg_carries_it_when_the_session_recorded_one(self):
        leg = lib._agent_assignment({
            "session_id": "hs-" + "1" * 32, "role": "implementer", "adapter": "codex",
            "reported_model": "gpt-6-astra", "reasoning_effort": "high",
            "phase_number": 4, "state": "completed"})
        self.assertEqual(leg["reasoning_effort"], "high")

    def test_a_leg_from_before_the_field_keeps_its_shape(self):
        leg = lib._agent_assignment({
            "session_id": "hs-" + "2" * 32, "role": "implementer", "adapter": "codex",
            "phase_number": 4, "state": "completed"})
        self.assertNotIn("reasoning_effort", leg,
                         "the key appeared on a session that never recorded one")

    @guard
    def test_the_snapshot_session_view_exposes_it(self):
        """The closed field tuple in handsoff_dashboard decides what reaches
        the page at all, however faithfully the record carries it."""
        source = (BIN / "handsoff_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('"reasoning_effort"', source,
                      "the snapshot session view drops the field before the page sees it")


if __name__ == "__main__":
    unittest.main()
