"""Runner fixture: the engine writes cache.json, which DECLARED_WRITES does not name."""
import re
import unittest
from pathlib import Path

from tests.guards import guard

ENGINE = Path(__file__).resolve().parents[1] / "bin" / "engine.py"


class JsonWrites(unittest.TestCase):
    @guard
    def test_every_json_write_is_declared(self):
        source = ENGINE.read_text()
        declared = re.search(r"DECLARED_WRITES = \((.*?)\)", source).group(1)
        for target in re.findall(r'"([\w.-]+\.json)"\)\.write_text\(json\.dumps', source):
            self.assertIn(f'"{target}"', declared, f"{target} is written but not declared")
