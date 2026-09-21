"""#219: `handsoff update`. Every external call (gh, pip, git, launchctl)
is a fake on PATH that records its argv in a log and answers a fixture;
the Beakon checkout is a real local git repository with a tag; nothing
reaches the network, the real venv or launchd."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT

sys.path.insert(0, str(BIN))
import handsoff_update as update  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
"""One fake for gh, pip and launchctl: answers FAKE_FIXTURE, logs to FAKE_LOG."""
import json, os, sys
name = os.path.basename(sys.argv[0]); args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name] + args) + "\n")
fx = json.load(open(os.environ["FAKE_FIXTURE"]))
def fail(msg, code=1):
    print(msg, file=sys.stderr); sys.exit(code)
if name == "gh":
    if args[:2] == ["auth", "status"]:
        sys.exit(0 if fx.get("logged_in", True) else 1)
    if args[:2] == ["release", "list"]:
        repo = args[args.index("--repo") + 1]
        tag = fx["latest"].get(repo)
        print(json.dumps([{"tagName": tag}] if tag else [])); sys.exit(0)
    if args[:2] == ["release", "download"]:
        repo = args[args.index("--repo") + 1]
        if repo in fx.get("download_fails", []):
            fail("HTTP 504 gateway time-out")
        d = args[args.index("--dir") + 1]
        open(os.path.join(d, f"{repo.split('/')[1]}-{args[2].lstrip('v')}-py3-none-any.whl"), "w").write("wheel")
        sys.exit(0)
    fail("fake gh: unexpected " + " ".join(args), 2)
if name == "pip":
    state = fx.setdefault("installed", {})
    if args[:1] == ["show"]:
        v = json.load(open(os.environ["FAKE_STATE"])).get(args[1])
        if not v: fail("WARNING: Package(s) not found: " + args[1])
        print(f"Name: {args[1]}\nVersion: {v}\n"); sys.exit(0)
    if args[:1] == ["install"]:
        wheel = os.path.basename(args[-1]); pkg, ver = wheel.split("-py3")[0].rsplit("-", 1)
        if pkg in fx.get("install_fails", []):
            fail("ERROR: could not install " + wheel)
        st = json.load(open(os.environ["FAKE_STATE"])); st[pkg.replace("_", "-")] = ver
        json.dump(st, open(os.environ["FAKE_STATE"], "w")); sys.exit(0)
    fail("fake pip: unexpected " + " ".join(args), 2)
if name == "launchctl":
    if args[:1] == ["list"]:
        print("\n".join(f"123\t0\t{l}" for l in fx.get("labels", []))); sys.exit(0)
    if args[:2] == ["kickstart", "-k"]:
        sys.exit(0)
    fail("fake launchctl: unexpected " + " ".join(args), 2)
fail("unexpected fake " + name, 2)
'''


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="handsoff-update-test-")).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("gh", "pip", "launchctl"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        venv = self.base / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "pip").symlink_to(self.bin / "pip")
        for tool, line in (("handsoff", "v0.3.99"), ("miner", "usage: miner"), ("sentinel", "sentinel 9.9.9")):
            (venv / tool).write_text(f"#!/bin/sh\necho '{line}'\n")
            (venv / tool).chmod(0o755)
        self.fixture = self.base / "fixture.json"
        self.state = self.base / "installed.json"
        self.log = self.base / "calls.log"
        # a Beakon origin with a tag, and a clean checkout behind it
        self.origin = self.base / "beakon-origin.git"
        self.origin.mkdir()
        git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        seed = self.base / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        git("config", "user.email", "t@t", cwd=seed)
        git("config", "user.name", "t", cwd=seed)
        (seed / "client").mkdir()
        (seed / "client" / "beakon.py").write_text("print('beakon 0.4.0')\n")
        git("add", "-A", cwd=seed)
        git("commit", "-qm", "0.4.0", cwd=seed)
        git("tag", "-a", "v0.4.0", "-m", "v0.4.0", cwd=seed)
        (seed / "client" / "beakon.py").write_text("print('beakon 0.5.0')\n")
        git("commit", "-qam", "0.5.0", cwd=seed)
        git("tag", "-a", "v0.5.0", "-m", "v0.5.0", cwd=seed)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "-q", "origin", "main", "--tags", cwd=seed)
        self.checkout = self.base / "beakon"
        git("clone", "-q", str(self.origin), str(self.checkout), cwd=self.base)
        git("config", "user.email", "t@t", cwd=self.checkout)
        git("config", "user.name", "t", cwd=self.checkout)
        git("checkout", "-q", "v0.4.0", cwd=self.checkout)
        self.config = self.base / "update.toml"
        self.config.write_text(f'[update]\nvenv = "{self.base / "venv"}"\nbeakon_checkout = "{self.checkout}"\n')
        self._old = {k: os.environ.get(k) for k in ("PATH", "FAKE_FIXTURE", "FAKE_STATE", "FAKE_LOG", "HANDSOFF_UPDATE_CONFIG", "GH_TOKEN")}
        os.environ.update({"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin:/opt/homebrew/bin", "FAKE_FIXTURE": str(self.fixture),
                           "FAKE_STATE": str(self.state), "FAKE_LOG": str(self.log), "HANDSOFF_UPDATE_CONFIG": str(self.config)})
        self.set(latest={"monzta1/project-handsoff": "v0.3.99", "monzta1/miner": "v0.9.0", "monzta1/sentinel": "v9.9.9", "monzta1/beakon": "v0.5.0"},
                 installed={"project-handsoff": "0.3.64", "miner": "0.9.0", "sentinel": "0.2.1"},
                 labels=["com.beakon.snapshots", "com.moncy.handsoff-dashboard", "com.apple.other"])

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.base, ignore_errors=True)

    def set(self, *, installed=None, **fixture):
        current = json.loads(self.fixture.read_text()) if self.fixture.exists() else {}
        current.update(fixture)
        self.fixture.write_text(json.dumps(current))
        if installed is not None:
            self.state.write_text(json.dumps(installed))

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def writes(self):
        return [c for c in self.calls() if c[:3] == ["gh", "release", "download"] or c[:2] == ["pip", "install"]
                or c[:2] == ["launchctl", "kickstart"] or (c[0] == "git" and "checkout" in c)]

    def run_update(self, *args, fleet_wait=0.0):
        lines = []
        cfg = update.load_config(self.config)
        cfg["fleet_url"] = "http://127.0.0.1:9/api/fleet"  # nothing listens: the poll times out at once
        outcome = update.update(cfg, only=[a for a in args if not a.startswith("--")] or None,
                                dry_run="--dry-run" in args, out=lines.append, fleet_wait=fleet_wait,
                                registry=self.base / "empty-register.json")  # #216: no live session on an empty register
        # #216: the install check speaks first; these tests read the tool lines after it
        self.assertEqual(lines[0], "INSTALL_CHECK_OK")
        return outcome, lines[1:]

    def test_dry_run_prints_every_would_line_and_writes_nothing(self):
        """[#219] acceptance 1, the dry run."""
        outcome, lines = self.run_update("--dry-run")
        self.assertEqual(lines, [
            "handsoff would update 0.3.64 -> v0.3.99",
            "miner already 0.9.0",
            "sentinel would update 0.2.1 -> v9.9.9",
            "beakon would update v0.4.0 -> v0.5.0 and restart com.beakon.snapshots",
            "fleet would restart: kickstart com.moncy.handsoff-dashboard, then poll http://127.0.0.1:9/api/fleet",
            "UPDATE_OK (dry run)"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertEqual(self.writes(), [])
        self.assertEqual(git("describe", "--tags", cwd=self.checkout), "v0.4.0")
        self.assertEqual(json.loads(self.state.read_text())["project-handsoff"], "0.3.64")

    def test_a_full_run_updates_each_tool_restarts_what_is_loaded_and_verifies(self):
        """[#219] acceptance 1, the run."""
        outcome, lines = self.run_update()
        self.assertEqual(lines[:4], [
            "handsoff 0.3.64 -> 0.3.99",
            "miner already 0.9.0",
            "sentinel 0.2.1 -> 9.9.9",
            "beakon v0.4.0 -> v0.5.0 (restarted com.beakon.snapshots)"])
        self.assertIn("fleet failed: Fleet did not answer at http://127.0.0.1:9/api/fleet within 0 s", lines)
        self.assertIn("verify: handsoff v0.3.99", lines)
        self.assertIn("verify: miner usage: miner", lines)
        self.assertIn("verify: sentinel sentinel 9.9.9", lines)
        self.assertIn("verify: beakon beakon 0.5.0", lines)
        self.assertEqual(lines[-1], "UPDATE_FAILED: fleet")
        self.assertEqual(outcome["exit_code"], 1)
        calls = self.calls()
        downloads = [c for c in calls if c[:3] == ["gh", "release", "download"]]
        self.assertEqual([(c[3], c[c.index("--repo") + 1]) for c in downloads], [("v0.3.99", "monzta1/project-handsoff"), ("v9.9.9", "monzta1/sentinel")])
        installs = [c for c in calls if c[:2] == ["pip", "install"]]
        self.assertEqual([c[2:4] for c in installs], [["--force-reinstall", "--no-deps"]] * 2)
        self.assertEqual([c[3] for c in calls if c[:2] == ["launchctl", "kickstart"]],
                         [f"gui/{os.getuid()}/com.beakon.snapshots", f"gui/{os.getuid()}/com.moncy.handsoff-dashboard"])
        self.assertEqual(git("describe", "--tags", cwd=self.checkout), "v0.5.0")
        self.assertEqual(json.loads(self.state.read_text()), {"project-handsoff": "0.3.99", "miner": "0.9.0", "sentinel": "9.9.9"})

    def test_already_and_never_a_downgrade(self):
        """[#219] acceptance 2."""
        self.set(installed={"project-handsoff": "0.3.99", "miner": "1.2.0", "sentinel": "9.9.9"})
        git("checkout", "-q", "main", cwd=self.checkout)  # main is at v0.5.0 exactly
        outcome, lines = self.run_update("handsoff", "miner", "sentinel", "beakon")
        self.assertEqual(lines[:4], ["handsoff already 0.3.99", "miner left at 1.2.0: installed 1.2.0 is newer than the release v0.9.0",
                                     "sentinel already 9.9.9", "beakon already v0.5.0"])
        self.assertEqual(self.writes(), [c for c in self.writes() if c[:2] == ["launchctl", "kickstart"]])  # only the fleet restart
        # a checkout ahead of the tag with local work: left, then failed when the tag is behind a dirty tree
        (self.checkout / "client" / "beakon.py").write_text("print('beakon dev')\n")
        git("commit", "-qam", "dev work", cwd=self.checkout)
        outcome, lines = self.run_update("beakon")
        self.assertTrue(lines[0].startswith("beakon left at ") and lines[0].endswith(": checkout is ahead of the release v0.5.0"), lines)
        git("checkout", "-q", "v0.4.0", cwd=self.checkout)
        (self.checkout / "scratch.txt").write_text("local\n")
        outcome, lines = self.run_update("beakon")
        self.assertIn("beakon failed: checkout at", lines[0])
        self.assertIn("has local changes", lines[0])
        self.assertEqual(git("describe", "--tags", cwd=self.checkout), "v0.4.0")
        # F1.1 (attempt 1): a dirty checkout at exactly the release fails too, never already
        git("checkout", "-q", "v0.5.0", cwd=self.checkout)
        outcome, lines = self.run_update("beakon")
        self.assertIn("beakon failed: checkout at", lines[0])
        self.assertIn("has local changes", lines[0])
        (self.checkout / "scratch.txt").unlink()
        outcome, lines = self.run_update("beakon")
        self.assertEqual(lines[0], "beakon already v0.5.0")

    def test_skipped_without_gh_login_without_a_checkout_and_on_odd_versions(self):
        """[#219] acceptance 2, the skips."""
        self.set(logged_in=False)
        outcome, lines = self.run_update("handsoff", "miner", "beakon")
        self.assertEqual(lines[:2], ["handsoff skipped: no gh login", "miner skipped: no gh login"])
        self.assertTrue(lines[2].startswith("beakon v0.4.0 -> v0.5.0"), lines)  # the local tags still say what is latest
        self.assertEqual([c for c in self.writes() if c[:3] == ["gh", "release", "download"] or c[:2] == ["pip", "install"]], [])
        self.set(logged_in=True, latest={"monzta1/project-handsoff": None, "monzta1/miner": "v1.0.0rc1", "monzta1/sentinel": "v9.9.9", "monzta1/beakon": "v0.5.0"})
        self.config.write_text(f'[update]\nvenv = "{self.base / "venv"}"\nbeakon_checkout = "{self.base / "nowhere"}"\n')
        outcome, lines = self.run_update("handsoff", "miner", "beakon")
        self.assertEqual(lines[:3], ["handsoff skipped: no release",
                                     "miner skipped: release tag 'v1.0.0rc1' is not a plain version (prerelease or build suffix)",
                                     f"beakon skipped: no checkout at {self.base / 'nowhere'}"])
        self.assertEqual(outcome["exit_code"], 1)  # the fleet poll on port 9 fails; the tools themselves did not
        self.assertEqual(outcome["failed"], ["fleet"])

    def test_one_failure_leaves_the_rest_updated_with_exit_one_and_the_summary(self):
        """[#219] acceptance 3."""
        self.set(download_fails=["monzta1/project-handsoff"])
        outcome, lines = self.run_update("handsoff", "miner", "sentinel", "beakon")
        self.assertEqual(lines[0], "handsoff failed: download of v0.3.99 failed: HTTP 504 gateway time-out")
        self.assertEqual(lines[2], "sentinel 0.2.1 -> 9.9.9")
        self.assertEqual(lines[3], "beakon v0.4.0 -> v0.5.0 (restarted com.beakon.snapshots)")
        self.assertEqual(lines[-1], "UPDATE_FAILED: handsoff, fleet")
        self.assertEqual(outcome["exit_code"], 1)
        self.assertEqual(json.loads(self.state.read_text())["sentinel"], "9.9.9")
        self.set(download_fails=[], install_fails=["sentinel"], installed={"project-handsoff": "0.3.64", "miner": "0.9.0", "sentinel": "0.2.1"})
        outcome, lines = self.run_update("sentinel", "miner")
        self.assertTrue(lines[1].startswith("sentinel failed: pip install failed: ERROR: could not install"), lines)
        self.assertEqual(outcome["failed"], ["sentinel"])  # --only without handsoff: no fleet step

    def test_no_token_anywhere_and_the_config_is_checked(self):
        """[#219] acceptance 3, the token and the config."""
        os.environ["GH_TOKEN"] = "ghp_SECRET_TOKEN"
        outcome, lines = self.run_update()
        self.assertNotIn("SECRET", "\n".join(lines))
        self.assertNotIn("SECRET", json.dumps(self.calls()))
        cfg = update.load_config(Path(self.base / "missing.toml"))
        self.assertEqual(cfg["venv"], str(Path(update.DEFAULT_VENV).expanduser()))
        self.assertEqual(set(cfg["tools"]), {"handsoff", "miner", "sentinel", "beakon"})
        bad = self.base / "bad.toml"
        bad.write_text('[update]\ntoken = "x"\n')
        with self.assertRaisesRegex(update.UpdateError, "unknown keys: token"):
            update.load_config(bad)
        bad.write_text('[update]\ntools = { extra = { repo = "nope", kind = "wheel" } }\n')
        with self.assertRaisesRegex(update.UpdateError, "tools.extra needs repo"):
            update.load_config(bad)
        with self.assertRaisesRegex(update.UpdateError, "unknown tool"):
            update.update(update.load_config(self.config), only=["nope"], out=lambda _: None)

    def test_the_cli_lists_update_and_runs_the_dry_run(self):
        """[#219] acceptance 5."""
        commands = subprocess.run([sys.executable, str(BIN / "handsoff_cli.py"), "commands"], capture_output=True, text=True, env=os.environ).stdout
        self.assertIn("update", commands)
        r = subprocess.run([sys.executable, str(BIN / "handsoff_cli.py"), "update", "--dry-run", "--only", "miner,beakon",
                            "--config", str(self.config)], capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("miner already 0.9.0", r.stdout)
        self.assertIn("beakon would update v0.4.0 -> v0.5.0", r.stdout)
        self.assertNotIn("fleet", r.stdout)  # --only without handsoff: no fleet step
        self.assertTrue(r.stdout.strip().endswith("UPDATE_OK (dry run)"))


if __name__ == "__main__":
    unittest.main()
