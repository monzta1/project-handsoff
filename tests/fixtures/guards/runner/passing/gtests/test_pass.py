"""Runner fixture: two marked readers pass; the unmarked case fails if it is ever run."""
import unittest
from pathlib import Path

from tests.guards import guard

ENGINE = Path(__file__).resolve().parents[1] / "bin" / "engine.py"


class Guards(unittest.TestCase):
    @guard
    def test_the_engine_defines_a_handler(self):
        self.assertIn("def handle_", ENGINE.read_text())

    def test_not_a_guard(self):
        self.fail("an unmarked case ran in the guard run")


@guard
class MarkedClass(unittest.TestCase):
    def test_the_engine_is_not_empty(self):
        self.assertTrue(ENGINE.read_text().strip())
