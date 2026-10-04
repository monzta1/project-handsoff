"""Runner fixture: one guard passes and one is skipped; a skipped guard did not run."""
import unittest

from tests.guards import guard


class Guards(unittest.TestCase):
    @guard
    def test_passes(self):
        self.assertTrue(True)

    @guard
    @unittest.skip("not on this platform")
    def test_skipped_guard(self):
        self.fail("unreachable")
