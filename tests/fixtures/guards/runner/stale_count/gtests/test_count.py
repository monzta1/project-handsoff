"""Runner fixture: a count written when the engine had two handlers; it has three now."""
import re
import unittest
from pathlib import Path

from tests.guards import guard

ENGINE = Path(__file__).resolve().parents[1] / "bin" / "engine.py"


class HandlerCount(unittest.TestCase):
    @guard
    def test_the_engine_has_two_handlers(self):
        self.assertEqual(len(re.findall(r"^def handle_", ENGINE.read_text(), re.M)), 2)
