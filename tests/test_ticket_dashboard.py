import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import handsoff_lib as lib  # noqa: E402


class TicketConfigTests(unittest.TestCase):
    def test_ticket_rows_are_loaded_in_declared_order(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "handsoff.toml").write_text(
                """
[[tickets]]
number = 74
title = "Catalogue"
status = "done"
url = "https://example.test/74"

[[tickets]]
number = 79
title = "Cab gate"
status = "in_progress"
url = "https://example.test/79"
""",
                encoding="utf-8",
            )
            self.assertEqual(
                lib.load_config(root)["tickets"],
                [
                    {"number": 74, "title": "Catalogue", "status": "done", "url": "https://example.test/74"},
                    {"number": 79, "title": "Cab gate", "status": "in_progress", "url": "https://example.test/79"},
                ],
            )

    def test_duplicate_ticket_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "handsoff.toml").write_text(
                """
[[tickets]]
number = 74
title = "First"
status = "done"

[[tickets]]
number = 74
title = "Duplicate"
status = "not_started"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(lib.HandsoffError, "must be unique"):
                lib.load_config(root)


if __name__ == "__main__":
    unittest.main()
