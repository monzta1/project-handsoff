"""Runner fixture: tests exist, but none is marked."""
import unittest


class Plain(unittest.TestCase):
    def test_not_a_guard(self):
        self.assertTrue(True)
