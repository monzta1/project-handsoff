"""Runner fixture: a passing guard beside a module whose discovery fails."""
import unittest

from tests.guards import guard


class Fine(unittest.TestCase):
    @guard
    def test_passes(self):
        self.assertTrue(True)
