"""#284: the extracted routing boundary is enforced, not merely drawn.

`bin/handsoff_routing.py` is the first bounded subsystem out of a 15,013-line
module. Because `handsoff_lib` re-exports every moved symbol, no caller
changed, and the line count barely moved (15,013 to 14,475 for 36 symbols).
Line count is the wrong measure of this change. What is different is that
the boundary can be checked, which is what this module does:

- the re-export surface is exactly the moved set, so a symbol cannot be
  dropped from it or added to it silently;
- the module imports only a declared allowlist;
- nothing it defines is unreachable from the routing concern;
- it does not import the monolith at module level, which would close the
  cycle the deferred imports exist to avoid;
- every routing function still returns what it returned before, checked
  against values captured from the pre-extraction module rather than
  against this module's own output.
"""
import ast
import json
import sys
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402
import handsoff_routing as routing  # noqa: E402

ROUTING_SOURCE = BIN / "handsoff_routing.py"
LIB_SOURCE = BIN / "handsoff_lib.py"
CONTRACT = Path(__file__).parent / "fixtures" / "routing_contract_v0386.json"

#: What `handsoff_routing` may import. Anything else is a new responsibility
#: arriving inside the boundary, which is the thing this rule exists to stop.
#: `handsoff_core` is allowed at module level because it has zero outbound
#: dependencies on the engine, so importing it cannot close a cycle. That is
#: the whole point of stage 1: primitives it owns are ordinary imports here,
#: not deferred ones.
ALLOWED_MODULE_IMPORTS = {"__future__", "json", "os", "re", "copy", "datetime", "pathlib",
                          "handsoff_core"}

#: The one module it may import from inside a function body, and only by
#: naming the primitives it needs. Declared so the deferred-import escape
#: hatch cannot quietly widen into "import whatever you like at call time".
ALLOWED_DEFERRED_IMPORTS = {"handsoff_lib"}

#: The primitives the extraction left behind, named in
#: docs/ARCHITECTURE-MIGRATION.md as what must move into a shared core
#: before the deferred imports can become ordinary ones.
DECLARED_DEFERRED_PRIMITIVES = {"_agent_assignment", "_canonical_provider_model"}

#: Deferred imports that now name the LAYER that defines the symbol rather than
#: the monolith. Still deferred, because config sits above routing (DEFAULT_CONFIG
#: embeds routing defaults) and a module-level import would close that cycle --
#: but the dependency is on one layer, not on 8,346 lines. #284 criterion 2 asks
#: for a subsystem extracted "without importing the entire monolith", and this is
#: what closed the gap between that wording and the code: eleven monolith
#: primitives became two.
DECLARED_DEFERRED_BY_LAYER = {
    "handsoff_config": {"DEFAULT_AGENT_MODEL", "DEFAULT_MODEL_POLICY", "load_config",
                        "model_policy_allows", "validate_model_policy",
                        "SELECTABLE_AGENT_ADAPTERS"},
    "handsoff_schema": {"AGENT_SESSION_LIVE_STATES", "PHASES"},
    "handsoff_projection": {"actor_family"},
}

#: Primitives stage 1 moved into the core, which routing now imports at module
#: level. They must NOT reappear as deferred monolith imports: that would be a
#: regression to the workaround the core exists to remove.
CORE_OWNED_PRIMITIVES = {"HandsoffError", "load_unique_json", "status_path"}


def _routing_tree():
    return ast.parse(ROUTING_SOURCE.read_text(encoding="utf-8"))


def _defined_symbols(tree):
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return out


class TheReExportSurfaceIsExactlyTheMovedSet(unittest.TestCase):
    """REQ-001. A symbol cannot leave the surface or join it unnoticed."""

    def setUp(self):
        self.defined = _defined_symbols(_routing_tree())
        # The private helper is not part of the concern's public surface.
        self.public = {s for s in self.defined if not s.startswith("_") or s.startswith("_adaptive")}

    def _lib_reexports(self):
        tree = ast.parse(LIB_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_routing":
                return {a.asname or a.name for a in node.names}
        self.fail("handsoff_lib no longer re-exports from handsoff_routing")

    def test_the_lib_reexports_every_public_routing_symbol(self):
        missing = sorted(self.public - self._lib_reexports())
        self.assertEqual(missing, [],
                         "these routing symbols are no longer reachable from handsoff_lib, "
                         "so a caller that imported them from there is broken")

    def test_the_lib_reexports_nothing_that_routing_does_not_define(self):
        extra = sorted(self._lib_reexports() - self.defined)
        self.assertEqual(extra, [],
                         "handsoff_lib re-exports names handsoff_routing does not define")

    def test_every_reexported_symbol_actually_resolves(self):
        for name in sorted(self._lib_reexports()):
            self.assertTrue(hasattr(lib, name), f"handsoff_lib.{name} does not resolve")
            self.assertTrue(hasattr(routing, name), f"handsoff_routing.{name} does not resolve")

    def test_the_moved_set_is_not_trivially_small(self):
        """A refactor that moved two symbols would pass every other check."""
        self.assertGreaterEqual(len(self.defined), 30,
                                f"only {len(self.defined)} symbols; this is not the measured module")


class NothingUnrelatedEntersTheBoundary(unittest.TestCase):
    """REQ-003. The architectural rule, derived from the source."""

    def setUp(self):
        self.tree = _routing_tree()

    def test_module_level_imports_are_on_the_allowlist(self):
        offenders = []
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                offenders += [a.name.split(".")[0] for a in node.names
                              if a.name.split(".")[0] not in ALLOWED_MODULE_IMPORTS]
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root not in ALLOWED_MODULE_IMPORTS:
                    offenders.append(root)
        self.assertEqual(sorted(set(offenders)), [],
                         "these module-level imports are outside the declared allowlist; "
                         "a new dependency inside the boundary needs declaring")

    def test_the_monolith_is_never_imported_at_module_level(self):
        """That would close the cycle the deferred imports exist to avoid."""
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "handsoff_lib",
                                    "a module-level lib import closes the re-export cycle")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "handsoff_lib",
                                        "a module-level lib import closes the re-export cycle")

    def test_deferred_imports_name_only_declared_primitives(self):
        undeclared = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in ALLOWED_DEFERRED_IMPORTS:
                continue
            for alias in node.names:
                if alias.name == "*":
                    self.fail("a star import from the monolith defeats the boundary")
                if alias.name not in DECLARED_DEFERRED_PRIMITIVES:
                    undeclared.add(alias.name)
        self.assertEqual(sorted(undeclared), [],
                         "these primitives are pulled from the monolith without being declared "
                         "in DECLARED_DEFERRED_PRIMITIVES or in the migration document")

    def test_the_declared_primitives_are_exactly_the_imported_ones(self):
        """Closed both ways, because a one-directional list grew a ghost.

        The first version of this list carried `acceptance_hash`, which the
        moved code only ever uses as a local name. It was never imported, so
        the plan and this list both claimed fifteen remaining primitives
        where there are fourteen. A subset check would not have caught it:
        an entry that names nothing is invisible to a rule that only asks
        whether every import is declared.
        """
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_lib":
                imported |= {a.name for a in node.names}
        self.assertEqual(
            sorted(DECLARED_DEFERRED_PRIMITIVES - imported), [],
            "these primitives are declared as remaining migration work but are not "
            "imported anywhere, so the declared coupling is larger than the real coupling")
        self.assertEqual(
            sorted(imported - DECLARED_DEFERRED_PRIMITIVES), [],
            "these primitives are imported from the monolith without being declared")


    def test_every_deferred_layer_import_is_declared(self):
        """Closed both ways, like the monolith list above. A deferred import
        that names a layer is still coupling; it is just honest coupling."""
        found = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module in DECLARED_DEFERRED_BY_LAYER:
                found.setdefault(node.module, set()).update(a.name for a in node.names)
        self.assertEqual(found, DECLARED_DEFERRED_BY_LAYER,
                         "the per-layer deferred imports and their declaration disagree")

    def test_no_deferred_import_names_a_symbol_the_monolith_no_longer_owns(self):
        """The point of the change: importing `load_config` from the monolith
        worked only because the monolith re-exports it. Naming the layer says
        where it lives, so a later move surfaces here instead of silently
        resolving through a re-export."""
        lib_defined = set()
        for node in ast.parse((BIN / "handsoff_lib.py").read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                lib_defined.add(node.name)
            elif isinstance(node, ast.Assign):
                lib_defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_lib":
                for alias in node.names:
                    self.assertIn(alias.name, lib_defined,
                                  f"line {node.lineno} imports {alias.name} from the monolith, "
                                  "which only re-exports it; name the module that defines it")

    def test_core_primitives_are_imported_at_module_level_not_deferred(self):
        """Stage 1's payoff, asserted rather than assumed.

        The core has no outbound engine dependencies, so importing it cannot
        close a cycle and there is no reason to defer it. If one of these
        reappears as a `from handsoff_lib import ...` inside a function, the
        workaround has crept back.
        """
        module_level = set()
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_core":
                module_level |= {a.name for a in node.names}
        self.assertEqual(sorted(CORE_OWNED_PRIMITIVES - module_level), [],
                         "these core primitives are not imported at module level")
        deferred = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_lib":
                deferred |= {a.name for a in node.names}
        self.assertEqual(sorted(CORE_OWNED_PRIMITIVES & deferred), [],
                         "these primitives moved to the core but are still pulled from "
                         "the monolith inside a function body")

    def test_no_definition_is_unreachable_from_the_concern(self):
        """A definition nothing reaches is a responsibility that drifted in."""
        defined = _defined_symbols(self.tree)
        referenced = set()
        for node in self.tree.body:
            body_names = {c.id for c in ast.walk(node) if isinstance(c, ast.Name)}
            own = {node.name} if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else set()
            referenced |= (body_names - own)
        exported = self._lib_reexport_names()
        orphans = sorted(defined - referenced - exported)
        self.assertEqual(orphans, [],
                         "these definitions are referenced by no routing symbol and exported "
                         "to no caller, so nothing in the concern needs them")

    def _lib_reexport_names(self):
        tree = ast.parse(LIB_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handsoff_routing":
                return {a.asname or a.name for a in node.names}
        return set()


class BehaviourIsUnchangedAcrossTheExtraction(unittest.TestCase):
    """REQ-004: checked against the OLD module, not the new one.

    Values were captured from `handsoff_lib` before any symbol moved and
    committed as a fixture. Comparing the new module against its own output
    would pass whatever it did.
    """

    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def _now(self):
        return {
            "route_routine": lib.route_adaptive_profile(
                risk_class="routine", deterministic_checks_complete=True),
            "route_premium": lib.route_adaptive_profile(
                risk_class="irreversible", deterministic_checks_complete=True),
            "route_checks_in_flight": lib.route_adaptive_profile(),
            "route_no_tiers": lib.route_adaptive_profile(
                available_tiers=[], deterministic_checks_complete=True),
            "profiles": lib.adaptive_routing_profiles(),
            "budgets": lib.adaptive_routing_budgets(),
            "risk_policy": lib.adaptive_risk_policy(),
            "classify_routine": lib.classify_adaptive_risk("routine"),
            "tiers": list(lib.ADAPTIVE_ROUTING_TIERS),
            "risk_classes": list(lib.ADAPTIVE_RISK_CLASSES),
        }

    def test_the_fixture_predates_the_extraction(self):
        """If this file were regenerated after the move it would prove nothing."""
        self.assertTrue(CONTRACT.is_file())
        self.assertGreaterEqual(len(self.baseline), 10, "the baseline lost cases")

    def test_every_recorded_case_is_unchanged(self):
        now = json.loads(json.dumps(self._now(), sort_keys=True, default=str))
        differing = {k: (self.baseline[k], now.get(k))
                     for k in self.baseline if self.baseline[k] != now.get(k)}
        self.assertEqual(differing, {},
                         "the extraction changed behaviour (case: before, after)")

    def test_the_routed_profile_still_reaches_a_real_model(self):
        """Guards against a baseline that captured an error state on both sides."""
        routed = lib.route_adaptive_profile(risk_class="routine",
                                            deterministic_checks_complete=True)
        self.assertEqual(routed["state"], "selected")
        self.assertIn("model", routed["profile"])


class TheModuleIsRegisteredWhereItMustBe(unittest.TestCase):
    """#325's lesson: the file set lives in more than one registry."""

    def test_the_runtime_manifest_covers_it(self):
        source = (BIN / "handsoff_manifest.py").read_text(encoding="utf-8")
        self.assertIn("bin/handsoff_routing.py", source,
                      "an unmanifested module can change without invalidating evidence")

    def test_the_wheel_packages_it(self):
        pyproject = (BIN.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("handsoff_routing", pyproject,
                      "the wheel would install without it while the manifest expects it")


if __name__ == "__main__":
    unittest.main()
