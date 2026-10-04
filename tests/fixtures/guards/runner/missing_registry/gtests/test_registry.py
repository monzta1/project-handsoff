"""Runner fixture: bin/routing.py exists but the manifest does not register it."""
import unittest
from pathlib import Path

from tests.guards import guard

BIN = Path(__file__).resolve().parents[1] / "bin"


class Registry(unittest.TestCase):
    @guard
    def test_every_engine_module_is_registered(self):
        manifest = (BIN / "manifest.py").read_text()
        for path in sorted(BIN.glob("*.py")):
            self.assertIn(f'"bin/{path.name}"', manifest, f"bin/{path.name} has no registry entry")
