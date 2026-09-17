"""#111: test fixtures never depend on files that only exist on one machine.

The version pin (.handsoff-version) is not tracked in the repository; a
fixture that copied it from the checkout root passed on a machine where a
run had been initialised there and failed with FileNotFoundError on a fresh
clone. Every fixture writes the pin itself, and this guard keeps it so."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

FORBIDDEN = (
    re.compile(r'ROOT\s*/\s*"\.handsoff-version"'),
    re.compile(r'BIN\.parent\s*/\s*"\.handsoff-version"'),
    re.compile(r'shutil\.copy\([^)]*"\.handsoff-version"'),
)


class FixtureHygieneTests(unittest.TestCase):
    def test_no_test_reads_the_pin_from_the_repository_root(self):
        offenders = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if any(pattern.search(line) for pattern in FORBIDDEN):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "fixtures must write the pin themselves:\n" + "\n".join(offenders))

    def test_fixtures_that_need_the_pin_write_it(self):
        # The five modules named in #111 each write the literal pin now.
        for name in ("test_design_convergence", "test_design_review_hold", "test_mission_control_ops",
                     "test_reviewer_sandbox", "test_handsoff_supervisor"):
            text = (ROOT / "tests" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn('".handsoff-version").write_text("0.3.*\\n")', text, name)


if __name__ == "__main__":
    unittest.main()
