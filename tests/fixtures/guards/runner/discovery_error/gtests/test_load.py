"""Runner fixture: the module imports, but loading its tests raises."""
import unittest

from tests.guards import guard


class Fine(unittest.TestCase):
    @guard
    def test_passes(self):
        self.assertTrue(True)


def load_tests(loader, tests, pattern):
    raise RuntimeError("discovery broke")
