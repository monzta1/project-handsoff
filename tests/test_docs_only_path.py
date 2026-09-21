"""#227: a docs-only change is a merge and nothing more. The wheel does not
carry README.md or docs/, so a Markdown edit never changes it; ci.yml runs
one docs job for a Markdown-only pull request and the full matrix otherwise."""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_handsoff_supervisor import ROOT

CI = ROOT / ".github" / "workflows" / "ci.yml"


def _wheel_fingerprint(tree: Path) -> list:
    out = tree / "dist"
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(tree)],
                   capture_output=True, text=True, check=True)
    wheel = next(out.glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        return sorted((i.filename, i.file_size, i.CRC) for i in zf.infolist()) + [("METADATA", zf.read(next(n for n in zf.namelist() if n.endswith("METADATA"))))]


class DocsOnlyPathTests(unittest.TestCase):
    def test_a_readme_edit_leaves_the_wheel_byte_identical(self):
        """[#227] acceptance 1."""
        base = Path(tempfile.mkdtemp(prefix="handsoff-wheel-")).resolve()
        try:
            tree = base / "tree"
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "dist", "build", "*.egg-info", "__pycache__",
                                                                     ".handsoff*", "handsoff-status.json", "handsoff-acceptance.json", "handsoff-events.jsonl", "handsoff-verifications.jsonl", "node_modules"))
            before = _wheel_fingerprint(tree)
            (tree / "README.md").write_text((tree / "README.md").read_text() + "\nAn edit that must not reach the wheel.\n")
            (tree / "docs" / "REFERENCE.md").write_text((tree / "docs" / "REFERENCE.md").read_text() + "\nAnother.\n")
            after = _wheel_fingerprint(tree)
            self.assertEqual(before, after)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_package_md_is_the_readme_and_carries_no_version(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertRegex(pyproject, r'(?m)^readme = "PACKAGE\.md"$')
        text = (ROOT / "PACKAGE.md").read_text()
        self.assertNotRegex(text, r"\d+\.\d+\.\d+")
        self.assertIn("README.md", text)

    def test_ci_runs_the_docs_job_alone_on_a_docs_only_pull_request(self):
        """[#227] acceptance 2: read off ci.yml."""
        text = CI.read_text()
        text = text[text.index("\njobs:\n"):]
        jobs = re.findall(r"(?m)^  ([a-z]+):\n", text)
        self.assertEqual(jobs, ["changes", "docs", "python", "modules", "dashboard", "tests"])

        def block(name):
            start = text.index(f"\n  {name}:\n")
            rest = text[start + 1:]
            end = re.search(r"(?m)^  [a-z]+:\n", rest[rest.index("\n") + 1:])
            return rest[: rest.index("\n") + 1 + end.start()] if end else rest

        self.assertIn("if: needs.changes.outputs.docs_only == 'true'", block("docs"))
        for name in ("python", "modules", "dashboard"):
            self.assertIn("if: needs.changes.outputs.docs_only != 'true'", block(name), name)
            self.assertIn("needs: changes", block(name), name)
        gather = block("tests")
        self.assertIn("needs: [changes, python, modules, dashboard, docs]", gather)
        self.assertIn("if: always()", gather)
        self.assertIn('if [ "${{ needs.changes.outputs.docs_only }}" = "true" ]', gather)
        self.assertIn('test "${{ needs.docs.result }}" = "success"', gather)
        self.assertIn('test "${{ needs.python.result }}" = "success" && test "${{ needs.modules.result }}" = "success" && test "${{ needs.dashboard.result }}" = "success"', gather)
        classify = block("changes")
        self.assertIn("*.md|docs/*) ;;", classify)
        self.assertIn('if [ "${{ github.event_name }}" != "pull_request" ]', classify)  # a push to main is never docs-only
        self.assertIn('[ -n "$files" ] || docs_only=false', classify)
        # the docs job runs the audit and the docs suites, nothing that needs node
        docs = block("docs")
        self.assertIn("tests.test_landing_page tests.test_governance_docs tests.test_docs_only_path", docs)
        self.assertNotIn("setup-node", docs)
        self.assertNotIn("|| true", docs)  # nothing in the docs job is non-gating (review F1.1)
        # the audit gates through the suite: the test that runs it fails on a finding
        landing = (ROOT / "tests" / "test_landing_page.py").read_text()
        self.assertIn('"doctor", "--docs-only"', landing)
        self.assertIn('self.assertNotIn("README.md", by_file)', landing)

    def test_the_playbook_names_the_docs_only_path(self):
        text = (ROOT / "playbook" / "landing.md").read_text()
        self.assertIn("Docs-only lane", text)
        self.assertIn("require_live_verification = false", text)
        self.assertIn("no version bump, no release", text)


if __name__ == "__main__":
    unittest.main()
