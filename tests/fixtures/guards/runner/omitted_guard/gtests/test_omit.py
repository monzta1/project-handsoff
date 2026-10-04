"""Runner fixture: load_tests leaves out a marked guard, which fails if it is ever run."""
import unittest

from tests.guards import guard


class Guards(unittest.TestCase):
    @guard
    def test_kept_guard(self):
        self.assertTrue(True)

    @guard
    def test_omitted_guard(self):
        self.fail("the stale count this guard would have caught")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([Guards("test_kept_guard")])
