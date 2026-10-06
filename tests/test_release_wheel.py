"""#403: the release wheel is built reproducibly from a commit, and the live
release smoke rebuilds the tag and compares bytes instead of reading dist/."""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from tests.guards import guard

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release_wheel.py"
SMOKE = ROOT / "tests" / "live_release_smoke.py"

_spec = importlib.util.spec_from_file_location("build_release_wheel", SCRIPT)
build_release_wheel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_release_wheel)
compare_wheels = build_release_wheel.compare_wheels


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def run_script(checkout: Path, commit: str, out: Path) -> Path:
    """Run the CLI from CHECKOUT; it prints the wheel path, then its sha256."""
    result = subprocess.run([sys.executable, str(SCRIPT), commit, "--out", str(out)], cwd=checkout,
                            capture_output=True, text=True, check=True)
    path, digest = result.stdout.strip().splitlines()
    wheel = Path(path)
    if not wheel.is_absolute():
        wheel = checkout / wheel
    assert build_release_wheel.sha256(wheel.read_bytes()) == digest, (path, digest)
    return wheel


class ReproducibleBuildTest(unittest.TestCase):
    """REQ-001: two checkouts of one commit build byte-identical wheels, whatever
    their file times, stray files or stale build/ folder; another commit differs;
    the wheel carries exactly the commit's tracked runtime files."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="release-wheel-test-")
        base = Path(cls.scratch.name)
        cls.commit = git(ROOT, "rev-parse", "HEAD")
        clean, dirty = base / "clean", base / "dirty"
        for checkout in (clean, dirty):
            subprocess.run(["git", "clone", "--quiet", str(ROOT), str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "checkout", "--quiet", "--detach", cls.commit], cwd=checkout, check=True,
                           capture_output=True)
        for path in dirty.rglob("*"):
            if ".git" not in path.relative_to(dirty).parts and path.is_file():
                os.utime(path, (1_000_000_000, 1_000_000_000))
        (dirty / "stray_untracked.py").write_text("STRAY = True\n")
        (dirty / "bin" / "handsoff_lib.py").write_text("# modified, never committed\n")
        stale = dirty / "build" / "lib"
        stale.mkdir(parents=True)
        (stale / "handsoff_lib.py").write_text("# stale build output\n")
        egg = dirty / "bin" / "project_handsoff.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text("Version: 0.0.0\n")
        cls.clean_wheel = run_script(clean, cls.commit, base / "out-clean")
        cls.dirty_wheel = run_script(dirty, cls.commit, base / "out-dirty")
        # a second commit made here, not HEAD~1: CI checks out one commit
        lib_file = clean / "bin" / "handsoff_lib.py"
        lib_file.write_text(lib_file.read_text() + "# a later commit\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-am", "later"],
                       cwd=clean, check=True, capture_output=True)
        cls.other = git(clean, "rev-parse", "HEAD")
        cls.other_wheel = run_script(clean, cls.other, base / "out-other")

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_two_checkouts_of_one_commit_build_identical_bytes(self):
        self.assertEqual(self.clean_wheel.name, self.dirty_wheel.name)
        self.assertEqual(self.clean_wheel.read_bytes(), self.dirty_wheel.read_bytes())

    def test_a_different_commit_builds_a_different_wheel(self):
        self.assertNotEqual(self.clean_wheel.read_bytes(), self.other_wheel.read_bytes())

    def test_the_wheel_carries_exactly_the_commits_tracked_runtime_files(self):
        pyproject = tomllib.loads(git(ROOT, "show", f"{self.commit}:pyproject.toml"))
        version = pyproject["project"]["version"]
        tool = pyproject["tool"]["setuptools"]
        expected = {f"{name}.py": f"bin/{name}.py" for name in tool["py-modules"]}
        data_prefix = f"project_handsoff-{version}.data/data/"
        for target, sources in tool["data-files"].items():
            for source in sources:
                expected[f"{data_prefix}{target}/{Path(source).name}"] = source
        with zipfile.ZipFile(self.dirty_wheel) as wheel:
            members = [name for name in wheel.namelist() if ".dist-info/" not in name and not name.endswith("/")]
            self.assertEqual(sorted(members), sorted(expected))
            for member, source in expected.items():
                committed = subprocess.run(["git", "show", f"{self.commit}:{source}"], cwd=ROOT,
                                           capture_output=True, check=True).stdout
                self.assertEqual(wheel.read(member), committed, member)


def make_wheel(members: dict[str, bytes], date=(2026, 1, 1, 0, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=date), data)
    return buffer.getvalue()


class CompareWheelsTest(unittest.TestCase):
    """REQ-002: compare_wheels passes identical bytes and otherwise names both
    hashes and whether the unpacked contents match."""

    members = {"handsoff_lib.py": b"print('x')\n", "project_handsoff-0.5.9.dist-info/RECORD": b"r\n"}

    def test_identical_wheels_pass(self):
        wheel = make_wheel(self.members)
        self.assertIsNone(compare_wheels(wheel, bytes(wheel)))

    def test_same_contents_with_different_zip_times_fail_saying_contents_match(self):
        published, built = make_wheel(self.members), make_wheel(self.members, date=(2025, 6, 1, 12, 0, 0))
        self.assertNotEqual(published, built)
        with self.assertRaises(AssertionError) as caught:
            compare_wheels(published, built)
        message = str(caught.exception)
        self.assertIn(build_release_wheel.sha256(published), message)
        self.assertIn(build_release_wheel.sha256(built), message)
        self.assertIn("contents match", message)

    def test_a_changed_member_fails_saying_contents_differ(self):
        published = make_wheel(self.members)
        built = make_wheel({**self.members, "handsoff_lib.py": b"print('y')\n"})
        with self.assertRaises(AssertionError) as caught:
            compare_wheels(published, built)
        message = str(caught.exception)
        self.assertIn(build_release_wheel.sha256(published), message)
        self.assertIn(build_release_wheel.sha256(built), message)
        self.assertIn("contents differ", message)


@guard
class SourceGuardsTest(unittest.TestCase):
    """REQ-002 and REQ-003: the smoke rebuilds the tag commit with the script and
    compares through compare_wheels; the release procedure builds with the script."""

    def test_the_smoke_builds_the_tag_commit_and_never_reads_dist(self):
        smoke = SMOKE.read_text(encoding="utf-8")
        self.assertIn("build_release_wheel.py", smoke)
        self.assertRegex(smoke, r'build_release_wheel\.py"?\)?,\s*tag_commit')
        self.assertIn("compare_wheels(published, built)", smoke)
        self.assertNotIn("dist/", smoke)
        self.assertNotIn('"dist"', smoke)

    def test_the_release_procedure_builds_with_the_script_before_gh_release_create(self):
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        procedure = reference.split("### Cutting a release", 1)[1].split("#### What CI runs", 1)[0]
        build_at = procedure.find("python3 scripts/build_release_wheel.py")
        self.assertNotEqual(build_at, -1, "the procedure does not name the build script")
        self.assertIn("--out dist", procedure)
        self.assertLess(build_at, procedure.index("gh release create"))
        self.assertNotIn("pip wheel", procedure)
        self.assertIn("reproducib", procedure)
        self.assertIn("docs/REFERENCE.md", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
