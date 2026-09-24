"""#325: the shipped project template agrees with the engine's defaults.

`templates/handsoff.toml` is shipped (`pyproject.toml` installs it to
`share/handsoff/templates`) and `handsoff_cli.py` reads it when a project
is initialised, so its values are what every new project starts with.

#320 raised `DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS` to the measured p90 of
3 and the template kept `= 2`. That is worse than a stale default: an
explicit value is governance-bound, so the project was pinned to 2 and the
new default never applied to it, invisibly to anyone reading only the
engine constant.

The comparison runs the template through the real loader rather than
matching key names. A first version of this suite flattened the TOML and
compared only keys whose names appear in `DEFAULT_CONFIG`, which silently
skipped 25 of the template's 41 settings: everything the loader resolves
under a nested name, such as the per-role entries of
`agent_token_budgets`, the `features` switches, and `project.name`. It
would have caught the one key that drifted and claimed to cover the rest.
Loading the file means the engine decides what each setting means, so a
remapped or nested key is covered by construction.
"""
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

ROOT = BIN.parent
TEMPLATE = ROOT / "templates" / "handsoff.toml"

#: Keys whose loaded value legitimately differs from `DEFAULT_CONFIG`,
#: each with the reason. Declared rather than skipped by a filter, so a
#: third exception cannot appear without someone writing down why.
DECLARED_DIFFERENCES = {
    "adaptive_routing_profiles": (
        "the loader sorts each tier's capabilities, so the value differs from "
        "the constant by list order while holding the same set; compared as "
        "sets below instead of exempted"),
    "features": (
        "DEFAULT_CONFIG leaves the table empty, meaning every switch takes its "
        "FEATURES default; the template states all six explicitly, so they are "
        "compared against FEATURES below instead of exempted"),
}


#: Template settings the loader resolves under a different name, declared
#: so the reachability check below is auditable rather than approximate.
#: These are exactly the remappings that made a name-matching comparison
#: silently skip real settings.
TEMPLATE_KEY_PATHS = {
    "agent_budget.architect": ("agent_token_budgets", "architect"),
    "agent_budget.supervisor": ("agent_token_budgets", "supervisor"),
    "agent_budget.implementer": ("agent_token_budgets", "implementer"),
    "agent_budget.reviewer": ("agent_token_budgets", "reviewer"),
    "checks.timeout_seconds": ("check_timeout_seconds",),
    "execution.profile": ("execution_profile",),
}

#: Template settings that reach no loaded config key at all, with the
#: reason. `load_config` does not carry them, so this suite cannot compare
#: them and says so rather than implying it does.
TEMPLATE_KEYS_OUTSIDE_THE_CONFIG = {
    "project.name": (
        "a per-project placeholder read where the project is named, not a "
        "default load_config carries; there is no engine value to agree with"),
}


def _resolve(cfg, path):
    node = cfg
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _loaded_template_config():
    """The effective config a new project gets from the shipped template."""
    tmp = Path(tempfile.mkdtemp(prefix="handsoff-template-"))
    shutil.copyfile(TEMPLATE, tmp / "handsoff.toml")
    try:
        return lib.load_config(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TheTemplateAgreesWithTheEngine(unittest.TestCase):
    """REQ-001: closed over what the loader resolves, not over key names."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = _loaded_template_config()

    def test_every_default_key_matches_except_those_declared(self):
        mismatched = {
            key: (self.cfg[key], default)
            for key, default in lib.DEFAULT_CONFIG.items()
            if key in self.cfg and self.cfg[key] != default
            and key not in DECLARED_DIFFERENCES
        }
        self.assertEqual(
            mismatched, {},
            "the shipped template pins values the engine no longer defaults to, "
            "so every new project would start governance-bound to the old ones "
            "(key: template, engine)")

    def test_the_comparison_covers_the_whole_default_config(self):
        """The guard the first version of this suite lacked.

        If the loader stopped returning most keys, or the template stopped
        being loadable, the assertion above would pass while comparing
        almost nothing.
        """
        compared = [key for key in lib.DEFAULT_CONFIG if key in self.cfg]
        self.assertEqual(
            sorted(set(lib.DEFAULT_CONFIG) - set(compared)), [],
            "a default the loaded template does not carry is a default this "
            "suite cannot check")
        self.assertGreaterEqual(len(compared), 30, f"only compared {len(compared)} keys")

    def test_every_template_setting_reaches_the_loaded_config(self):
        """Closes the gap the first version had, from the other side.

        Each scalar the template states must actually influence the loaded
        config, by its own name or nested inside a table. A setting that
        reaches nothing is either dead or silently renamed, and either way
        this suite would not be covering it.
        """
        parsed = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))
        unreachable = []
        for section, body in parsed.items():
            if not isinstance(body, dict):
                continue
            for key, value in body.items():
                if isinstance(value, (dict, list)):
                    continue
                dotted = f"{section}.{key}"
                if dotted in TEMPLATE_KEYS_OUTSIDE_THE_CONFIG:
                    continue
                if dotted in TEMPLATE_KEY_PATHS:
                    value_at, found = _resolve(self.cfg, TEMPLATE_KEY_PATHS[dotted])
                    if not found:
                        unreachable.append(f"{dotted} (declared path missing)")
                    elif value_at != value:
                        unreachable.append(f"{dotted} (declared path holds {value_at!r})")
                    continue
                reached = key in self.cfg or (
                    isinstance(self.cfg.get(section), dict) and key in self.cfg[section])
                if not reached:
                    unreachable.append(dotted)
        self.assertEqual(sorted(unreachable), [],
                         "these template settings reach no loaded config key; a "
                         "renamed or nested setting must be declared in "
                         "TEMPLATE_KEY_PATHS so the coverage claim stays true")

    def test_each_uncomparable_template_key_states_why(self):
        for key, reason in TEMPLATE_KEYS_OUTSIDE_THE_CONFIG.items():
            self.assertGreaterEqual(len(reason), 40, f"{key} has no stated reason")

    def test_each_declared_remapping_is_still_a_remapping(self):
        """A mapping kept after the loader stops needing it hides a rename."""
        for dotted, path in TEMPLATE_KEY_PATHS.items():
            section, key = dotted.split(".", 1)
            self.assertNotEqual(
                (section,) + (key,), path,
                f"{dotted} maps to itself; drop it from TEMPLATE_KEY_PATHS")
            _, found = _resolve(self.cfg, path)
            self.assertTrue(found, f"{dotted} maps to a path the loader does not produce")

    def test_each_declared_difference_states_a_reason(self):
        for key, reason in DECLARED_DIFFERENCES.items():
            self.assertIn(key, lib.DEFAULT_CONFIG, f"{key} is not a default at all")
            self.assertGreaterEqual(len(reason), 40, f"{key} has no stated reason")

    def test_the_declared_differences_are_still_real(self):
        """An exception kept after it stops applying is a hole."""
        for key in DECLARED_DIFFERENCES:
            self.assertNotEqual(
                self.cfg[key], lib.DEFAULT_CONFIG[key],
                f"{key} now matches the default, so remove its declared difference")


class TheDeclaredDifferencesAreCheckedNotWaived(unittest.TestCase):
    """REQ-001: the two exceptions are compared on their own terms."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = _loaded_template_config()

    def test_routing_capabilities_hold_the_same_set_per_tier(self):
        template = self.cfg["adaptive_routing_profiles"]
        default = lib.DEFAULT_CONFIG["adaptive_routing_profiles"]
        self.assertEqual(set(template), set(default))
        for tier in default:
            self.assertEqual(set(template[tier]["capabilities"]),
                             set(default[tier]["capabilities"]), tier)
            for field in ("adapter", "model", "limits", "pricing"):
                self.assertEqual(template[tier].get(field), default[tier].get(field),
                                 f"{tier}.{field}")

    def test_every_feature_switch_states_its_engine_default(self):
        switches = self.cfg["features"]
        self.assertEqual(set(switches), set(lib.FEATURES),
                         "the template must state exactly the known switches")
        for name, (default, _text) in lib.FEATURES.items():
            self.assertEqual(switches[name], default,
                             f"the template ships {name} flipped away from its default")


class TheDesignReviewLimitIsTheValueThatDrifted(unittest.TestCase):
    """REQ-001: names the specific regression, so the history stays legible."""

    def test_the_template_limit_matches_the_measured_percentile(self):
        cfg = _loaded_template_config()
        self.assertEqual(cfg["max_autonomous_design_reviews"],
                         lib.DEFAULT_MAX_AUTONOMOUS_DESIGN_REVIEWS)
        self.assertEqual(cfg["max_autonomous_design_reviews"], 3)


class TheTemplateIsReachableAsShipped(unittest.TestCase):
    """REQ-001: the file compared is the file a new project receives."""

    def test_the_template_is_declared_as_installed_data(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("templates/handsoff.toml", pyproject,
                      "the template must ship, or comparing it proves nothing")

    def test_the_template_is_covered_by_the_runtime_manifest(self):
        manifest_source = (BIN / "handsoff_manifest.py").read_text(encoding="utf-8")
        self.assertIn("templates/handsoff.toml", manifest_source,
                      "an unmanifested template can change without invalidating evidence")

    def test_the_cli_reads_it_on_init(self):
        cli = (BIN / "handsoff_cli.py").read_text(encoding="utf-8")
        self.assertIn("templates/handsoff.toml", cli,
                      "if init stops reading it, this suite guards a file nobody gets")


if __name__ == "__main__":
    unittest.main()
