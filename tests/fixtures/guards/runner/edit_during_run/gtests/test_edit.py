"""Runner fixture: the guard passes, but edits the engine while the run is in flight."""
import unittest
from pathlib import Path

from tests.guards import guard

ENGINE = Path(__file__).resolve().parents[1] / "bin" / "engine.py"


class Guards(unittest.TestCase):
    @guard
    def test_the_engine_defines_a_handler_and_then_changes(self):
        text = ENGINE.read_text()
        self.assertIn("def handle_", text)
        ENGINE.write_text(text + "\n# edited while the guards ran\n")
