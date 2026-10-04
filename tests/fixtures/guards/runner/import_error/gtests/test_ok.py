"""Runner fixture: a passing guard beside a module that cannot be imported."""
import unittest

from tests.guards import guard


class Fine(unittest.TestCase):
    @guard
    def test_passes(self):
        self.assertTrue(True)
