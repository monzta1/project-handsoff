"""#224: the GitHub landing page is short and points at the reference; no
heading of the README at v0.3.68 was lost in the split."""
import json
import re
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import ROOT

OLD_HEADINGS = json.loads((Path(__file__).with_name("fixtures") / "readme_headings_v0.3.68.json").read_text())
FILES = ("README.md", "docs/REFERENCE.md", "docs/FIELD-NOTES.md")


def headings(text):
    out, fence = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
        if not fence and re.match(r"^#{2,3} ", line):
            out.append(line.rstrip())
    return out


class LandingPageTests(unittest.TestCase):
    def test_the_readme_is_a_landing_page(self):
        """[#224] acceptance 1."""
        text = (ROOT / "README.md").read_text()
        self.assertLessEqual(len(text.splitlines()), 160)
        self.assertIn('<img src="dashboard/logo.png"', text)
        self.assertRegex(text, r"python3 -m pip install https://github\.com/monzta1/project-handsoff/releases/download/v\d+\.\d+\.\d+/project_handsoff-\d+\.\d+\.\d+-py3-none-any\.whl")
        for needle in ("handsoff playbook lanes", "handsoff update", "docs/REFERENCE.md", "docs/FIELD-NOTES.md", "INSTALL.md",
                       "monzta1/miner", "monzta1/sentinel", "monzta1/beakon", "handsoff fleet serve"):
            self.assertIn(needle, text, needle)
        # no line long enough to scroll a GitHub render sideways outside a fence
        fence = False
        for line in text.splitlines():
            if line.startswith("```"):
                fence = not fence
            if not fence and not line.startswith("|") and not line.startswith("<"):
                self.assertLessEqual(len(line), 140, line)

    def test_every_old_heading_lives_in_exactly_one_file(self):
        """[#224] acceptance 2: nothing was lost in the split."""
        where = {name: headings((ROOT / name).read_text()) for name in FILES}
        self.assertGreater(len(OLD_HEADINGS), 80)
        missing, doubled = [], []
        for heading in OLD_HEADINGS:
            homes = [name for name, found in where.items() if heading in found]
            if not homes:
                missing.append(heading)
            elif len(homes) > 1:
                doubled.append((heading, homes))
        self.assertEqual(missing, [])
        self.assertEqual(doubled, [])
        reference = (ROOT / "docs" / "REFERENCE.md").read_text()
        self.assertIn("### Cutting a release", reference)
        notes = (ROOT / "docs" / "FIELD-NOTES.md").read_text()
        self.assertRegex(notes, r"(?m)^### v0\.3\.22 field notes")
        self.assertNotIn("field note", "\n".join(h for h in where["docs/REFERENCE.md"] if h.startswith("### v0.")))

    def test_the_audit_finds_nothing_new(self):
        """[#224] acceptance 3: doctor --docs-only reports nothing on the landing
        page, and on the split files only what it reported on the old README
        (the engine checkout's own bin/ commands, cited on purpose)."""
        import subprocess, sys
        from tests.test_handsoff_supervisor import BIN
        r = subprocess.run([sys.executable, str(BIN / "handsoff_cli.py"), "doctor", "--docs-only", str(ROOT)],
                           capture_output=True, text=True)
        findings = [line.split(":", 2) for line in r.stdout.splitlines() if ":" in line]
        by_file = {Path(f).name: code for f, code, _ in findings}
        self.assertNotIn("README.md", by_file)
        for name in ("REFERENCE.md", "FIELD-NOTES.md"):
            self.assertIn(by_file.get(name, "none"), ("none", "obsolete-command-path"), name)


if __name__ == "__main__":
    unittest.main()
