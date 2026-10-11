"""#446: doctor and init report a missing or logged-out gh before a lane can
reach landing.

Landing (Phase 6 -> 7) runs gh pr create, ci-watch and gh release create.
On a fresh Linux host a lane reached Phase 6 with gh unauthenticated and
stalled, because nothing checked gh before init. One test class per reader:
DoctorReportsGh (`handsoff doctor`) and InitWarnsGh (`handsoff supervisor
init`). shutil.which and subprocess.run are patched at the module level the
probe resolves them from, so no test reaches the real gh; every other
subprocess call (git) goes through to the real one.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_cli as cli  # noqa: E402
import handsoff_lib as lib  # noqa: E402
import handsoff_supervisor as supervisor  # noqa: E402

FAKE_GH = "/fake/bin/gh"
PROBE = [FAKE_GH, "auth", "status", "--hostname", "github.com"]
LOGGED_OUT = "You are not logged into any GitHub hosts. To log in, run: gh auth login\n"
LOGGED_IN = "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"
OFFLINE = "github.com\n  X Timeout trying to log in to github.com account octocat (keyring)\n"
REAL_RUN = subprocess.run
REAL_WHICH = shutil.which


class FakeGh:
    """A delegating subprocess.run: the gh probe is answered, everything else
    is the real call."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.answer = (returncode, stdout, stderr)
        self.raises = raises
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        if argv and argv[0] == FAKE_GH:
            self.calls.append(list(argv))
            if self.raises is not None:
                raise self.raises
            code, out, err = self.answer
            return subprocess.CompletedProcess(argv, code, out, err)
        return REAL_RUN(argv, *args, **kwargs)


def which_gh(present=True):
    return lambda name: (FAKE_GH if present else None) if name == "gh" else REAL_WHICH(name)


@contextlib.contextmanager
def gh(fake, present=True):
    with mock.patch.object(lib.shutil, "which", side_effect=which_gh(present)), \
            mock.patch.object(lib.subprocess, "run", side_effect=fake):
        yield


def git(root, *args):
    REAL_RUN(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def with_origin(root, url="https://github.com/acme/widgets.git"):
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", url)


class DoctorReportsGh(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("HANDSOFF_SKIP_PREFLIGHT")
        os.environ["HANDSOFF_SKIP_PREFLIGHT"] = "1"
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-gh-")).resolve()
        cli.init_project(self.root, None)
        with_origin(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        if self._env is None:
            os.environ.pop("HANDSOFF_SKIP_PREFLIGHT", None)
        else:
            os.environ["HANDSOFF_SKIP_PREFLIGHT"] = self._env

    def doctor(self, fake, present=True):
        with gh(fake, present):
            return cli.doctor(self.root, skip_preflight=True)

    def gh_warnings(self, report):
        return [w for w in report["warnings"] if w.startswith("gh-not-ready:")]

    def test_logged_out_gh_is_unauthenticated_and_the_warning_names_gh_auth_login(self):
        fake = FakeGh(1, "", LOGGED_OUT)
        report = self.doctor(fake)
        self.assertEqual(fake.calls, [PROBE])
        github = report["github_cli"]
        self.assertEqual(github["state"], "unauthenticated")
        self.assertTrue(github["required"])
        self.assertEqual(github["repository"], "acme/widgets")
        self.assertEqual(github["fix"], "gh auth login")
        [warning] = self.gh_warnings(report)
        self.assertIn("not logged in", warning)
        self.assertIn("gh pr create", warning)
        self.assertTrue(warning.endswith("fix: gh auth login"), warning)

    def test_missing_gh_names_the_install_and_login_fix(self):
        fake = FakeGh(0, LOGGED_IN)
        report = self.doctor(fake, present=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(report["github_cli"]["state"], "missing")
        self.assertIsNone(report["github_cli"]["executable"])
        [warning] = self.gh_warnings(report)
        self.assertIn("gh is not on PATH", warning)
        self.assertIn("https://cli.github.com", warning)
        self.assertIn("gh auth login", warning)

    def test_logged_in_gh_is_ready_and_warns_nothing(self):
        fake = FakeGh(0, LOGGED_IN)
        report = self.doctor(fake)
        self.assertEqual(fake.calls, [PROBE])
        self.assertEqual(report["github_cli"]["state"], "ready")
        self.assertIsNone(report["github_cli"]["fix"])
        self.assertEqual(self.gh_warnings(report), [])

    def test_an_offline_failure_is_unverified_never_a_login_request(self):
        fake = FakeGh(1, "", OFFLINE)
        report = self.doctor(fake)
        self.assertEqual(report["github_cli"]["state"], "unverified")
        self.assertEqual(report["github_cli"]["fix"], "gh auth status")
        [warning] = self.gh_warnings(report)
        self.assertNotIn("gh auth login", warning)

    def test_a_probe_timeout_is_unverified(self):
        fake = FakeGh(raises=subprocess.TimeoutExpired(PROBE, 20))
        report = self.doctor(fake)
        self.assertEqual(report["github_cli"]["state"], "unverified")
        self.assertIn("timed out", report["github_cli"]["detail"])

    def test_a_repository_without_a_github_origin_is_not_required_and_never_probes(self):
        git(self.root, "remote", "set-url", "origin", "https://gitlab.example/acme/widgets.git")
        fake = FakeGh(1, "", LOGGED_OUT)
        report = self.doctor(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(report["github_cli"]["state"], "not_required")
        self.assertFalse(report["github_cli"]["required"])
        self.assertEqual(self.gh_warnings(report), [])


class InitWarnsGh(HandsoffTestCase):
    def init(self, fake, present=True):
        argv = ["handsoff_supervisor.py", "--root", str(self.tmp), "init", "Feature", "--item", "#446"]
        out = io.StringIO()
        with gh(fake, present), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = supervisor.main()
        return code, out.getvalue()

    def warnings(self, output):
        return [line for line in output.splitlines() if line.startswith("HANDSOFF_PREFLIGHT_WARNING:")]

    def test_logged_out_gh_warns_with_the_fix_and_init_still_creates_the_run(self):
        with_origin(self.tmp)
        fake = FakeGh(1, "", LOGGED_OUT)
        code, output = self.init(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.calls, [PROBE])
        [warning] = self.warnings(output)
        self.assertIn("gh-not-ready: gh is not logged in", warning)
        self.assertTrue(warning.endswith("fix: gh auth login"), warning)
        self.assertTrue((self.tmp / "handsoff-status.json").is_file())

    def test_missing_gh_warns_with_the_install_fix(self):
        with_origin(self.tmp)
        code, output = self.init(FakeGh(0, LOGGED_IN), present=False)
        self.assertEqual(code, 0, output)
        [warning] = self.warnings(output)
        self.assertIn("https://cli.github.com", warning)

    def test_logged_in_gh_prints_no_warning(self):
        with_origin(self.tmp)
        fake = FakeGh(0, LOGGED_IN)
        code, output = self.init(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.calls, [PROBE])
        self.assertEqual(self.warnings(output), [])

    def test_a_repository_without_a_remote_prints_no_warning_and_never_probes(self):
        fake = FakeGh(1, "", LOGGED_OUT)
        code, output = self.init(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.warnings(output), [])


if __name__ == "__main__":
    unittest.main()
