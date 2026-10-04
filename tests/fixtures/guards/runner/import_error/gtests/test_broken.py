"""Runner fixture: this module cannot be imported."""
import unittest

import handsoff_module_that_does_not_exist  # noqa: F401

from tests.guards import guard


class Broken(unittest.TestCase):
    @guard
    def test_never_collected(self):
        pass
