#!/usr/bin/env python3
"""P1.2: dependency-aware evidence invalidation.

A criterion that declares `paths` binds its evidence to a scoped digest over
the repository entries matching them (tracked and untracked non-ignored),
so a change outside its paths keeps its evidence current and a change inside
stales it. The verify cache key of a command carries, for every criterion it
covers, that criterion's spec hash and scoped digest (the whole-repository
digest when it has none). Launch counts are read from counter files the
check scripts append to, outside the fixture root.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402


class ScopedFixture(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        self.counters = Path(tempfile.mkdtemp(prefix="handsoff-scoped-counters-"))
        self.addCleanup(shutil.rmtree, self.counters, True)
        for name in ("a", "b", "c"):
            self._write(f"src/{name}/mod.py", f"VALUE = '{name}'\n")

    def _write(self, relative, text):
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _command(self, name):
        script = self.counters / f"{name}.sh"
        script.write_text(f"echo launch >> '{self.counters / name}'\nexit 0\n")
        return f"sh {script}"

    def _launches(self, name):
        path = self.counters / name
        return len(path.read_text().splitlines()) if path.exists() else 0

    def _set_commands(self, commands):
        toml = self.tmp / "handsoff.toml"
        text = toml.read_text()
        current = next(line for line in text.splitlines() if line.startswith("commands = "))
        toml.write_text(text.replace(current, f"commands = {json.dumps(commands)}", 1))

    def _ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _criterion(self, cid):
        return next(c for c in self.read_acceptance()["criteria"] if c["id"] == cid)

    def _verify_all(self):
        return json.loads(self._ok(run(["verify", "--all", "--by", "test-implementer"], cwd=self.tmp)).stdout)

    def _drift(self):
        cfg = lib.load_config(self.tmp)
        records, problems = lib.load_verifications(self.tmp, cfg)
        self.assertEqual(problems, [])
        acceptance = json.loads((self.tmp / "handsoff-acceptance.json").read_text())
        return lib.evidence_drift(self.tmp, cfg, acceptance, records)

    def _build(self, plan, commands):
        """`plan`: {criterion id: (command, paths or None)}; REQ-001 is the primary."""
        self._set_commands(commands)
        self.init("P1.2 scoped invalidation")
        for cid, (command, paths) in plan.items():
            path_args = [arg for path in paths or [] for arg in ("--path", path)]
            if cid == "REQ-001":
                self._ok(run(["criterion-update", "REQ-001", "--requirement", "P1.2 primary",
                              "--test", command] + path_args, cwd=self.tmp))
            else:
                self._ok(run(["criterion-add", cid, "--type", "supporting", "--requirement", f"P1.2 {cid}",
                              "--verification", "automated", "--test", command] + path_args, cwd=self.tmp))


class TestDisjointPaths(ScopedFixture):
    def test_only_the_touched_criterion_is_invalidated_and_only_its_command_reruns(self):
        a, b, c = self._command("a"), self._command("b"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, ["src/a/*"]), "REQ-003": (b, ["src/b"])},
                    [a, b, c])
        self._verify_all()
        self.assertEqual((self._launches("a"), self._launches("b"), self._launches("c")), (1, 1, 1))
        self.assertEqual(sorted(self._drift()["current"]), ["REQ-001", "REQ-002", "REQ-003"])

        self._write("src/b/mod.py", "VALUE = 'changed'\n")
        drift = self._drift()
        self.assertEqual(drift["stale"], ["REQ-003"])
        self.assertEqual(sorted(drift["current"]), ["REQ-001", "REQ-002"])
        self.assertEqual(drift["invalidated"], [{"criterion": "REQ-003", "changed_paths": ["src/b/mod.py"]}])
        status = json.loads(run(["status"], cwd=self.tmp).stdout)
        self.assertEqual(status["evidence_drift"]["invalidated"],
                         [{"criterion": "REQ-003", "changed_paths": ["src/b/mod.py"]}])

        payload = self._verify_all()
        self.assertEqual((self._launches("a"), self._launches("b"), self._launches("c")), (1, 2, 1))
        self.assertEqual(payload["launched"], [b])
        self.assertEqual(sorted(payload["reused"]), sorted([a, c]))
        self.assertEqual(self._drift()["stale"], [])

    def test_a_change_outside_every_path_keeps_all_evidence_current(self):
        a, c = self._command("a"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, ["src/a"])}, [a, c])
        self._verify_all()
        self._write("docs/notes.md", "unrelated\n")
        drift = self._drift()
        self.assertEqual(drift["stale"], [])
        self._verify_all()
        self.assertEqual((self._launches("a"), self._launches("c")), (1, 1))


class TestSharedCommand(ScopedFixture):
    def test_a_change_under_either_path_reruns_the_shared_command(self):
        shared, c = self._command("shared"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (shared, ["src/a"]), "REQ-003": (shared, ["src/b"])},
                    [shared, c])
        self._verify_all()
        self.assertEqual(self._launches("shared"), 1)
        self._verify_all()
        self.assertEqual(self._launches("shared"), 1, "nothing changed: the shared command is reused")

        self._write("src/a/mod.py", "VALUE = 'a2'\n")
        self.assertEqual(self._drift()["stale"], ["REQ-002"])
        self._verify_all()
        self.assertEqual(self._launches("shared"), 2)

        self._write("src/b/mod.py", "VALUE = 'b2'\n")
        self.assertEqual(self._drift()["stale"], ["REQ-003"])
        self._verify_all()
        self.assertEqual((self._launches("shared"), self._launches("c")), (3, 1))

    def test_reuse_guidance_checks_every_criterion_the_shared_command_covers(self):
        """verify writes one record per criterion, REQ-003's last; a change
        under REQ-002's path must still make the shared command not
        reusable (review finding 3)."""
        shared, c = self._command("shared"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (shared, ["src/a"]), "REQ-003": (shared, ["src/b"])},
                    [shared, c])
        self._verify_all()

        def standing():
            return {row["command"]: (row["reusable"], row["reason"])
                    for row in lib.verified_commands(self.tmp, lib.load_config(self.tmp))}

        self.assertEqual(standing(), {shared: (True, None), c: (True, None)})
        self._write("src/a/mod.py", "VALUE = 'a2'\n")
        self.assertEqual(self._drift()["stale"], ["REQ-002"])
        self.assertEqual(standing(), {shared: (False, "stale tree (REQ-002)"), c: (True, None)})
        self._verify_all()
        self.assertEqual(standing(), {shared: (True, None), c: (True, None)})


class TestPathsEditedAfterEvidence(ScopedFixture):
    def test_editing_paths_resets_the_criterion_until_reverified(self):
        a, c = self._command("a"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, ["src/a"])}, [a, c])
        self._verify_all()
        before = self._criterion("REQ-002")
        self.assertEqual(before["state"], "passing")
        self._ok(run(["criterion-update", "REQ-002", "--path", "src/b"], cwd=self.tmp))
        after = self._criterion("REQ-002")
        self.assertEqual((after["state"], after["evidence"], after["paths"]), ("not_tested", [], ["src/b"]))
        self.assertNotEqual(lib.criterion_spec_hash(before), lib.criterion_spec_hash(after))
        records, _ = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        self.assertFalse(lib.criterion_fully_evidenced(after, records))
        self.assertNotIn("REQ-002", self._drift()["current"])
        payload = self._verify_all()
        self.assertIn(a, payload["launched"])
        self.assertEqual(self._criterion("REQ-002")["state"], "passing")


class TestUntrackedFiles(ScopedFixture):
    def _git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=self.tmp, check=True, capture_output=True)

    def test_a_new_untracked_file_under_a_path_stales_it_and_an_ignored_one_does_not(self):
        a, c = self._command("a"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, ["src/a"])}, [a, c])
        self._write(".gitignore", (self.tmp / ".gitignore").read_text() + "\nsrc/a/*.log\n"
                    if (self.tmp / ".gitignore").exists() else "src/a/*.log\n")
        self._git("init", "-q")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "fixture")
        self._verify_all()
        self.assertEqual(self._drift()["stale"], [])
        self._write("src/a/build.log", "ignored\n")
        self.assertEqual(self._drift()["stale"], [], "a gitignored file is not repository content")
        self._write("src/a/new_module.py", "NEW = 1\n")
        drift = self._drift()
        self.assertEqual(drift["stale"], ["REQ-002"])
        self.assertEqual(drift["invalidated"], [{"criterion": "REQ-002", "changed_paths": ["src/a/new_module.py"]}])


class TestCriterionWithoutPaths(ScopedFixture):
    def test_a_criterion_without_paths_keeps_whole_repository_behavior(self):
        a, c = self._command("a"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, None)}, [a, c])
        self._verify_all()
        self._write("docs/notes.md", "anywhere\n")
        drift = self._drift()
        self.assertEqual(drift["stale"], ["REQ-002"])
        self.assertIn("docs/notes.md", drift["invalidated"][0]["changed_paths"])
        self._verify_all()
        self.assertEqual((self._launches("a"), self._launches("c")), (2, 1))

    def test_legacy_binding_is_unchanged_without_paths(self):
        legacy = lib.verification_binding("cmd", "d" * 64, "e" * 64, ["s"])
        self.assertEqual(legacy, lib.verification_binding("cmd", "d" * 64, "e" * 64, ["s"], scoped=None))
        self.assertNotEqual(legacy, lib.verification_binding("cmd", "d" * 64, "e" * 64, ["s"],
                                                             scoped=[("s", "f" * 64)]))


class TestConfigChange(ScopedFixture):
    def test_a_verification_config_change_invalidates_every_criterion(self):
        a, b, c = self._command("a"), self._command("b"), self._command("c")
        self._build({"REQ-001": (c, ["src/c"]), "REQ-002": (a, ["src/a"]), "REQ-003": (b, ["src/b"])},
                    [a, b, c])
        self._verify_all()
        self._set_commands([a, b, c, self._command("extra")])
        drift = self._drift()
        self.assertEqual(sorted(drift["stale"]), ["REQ-001", "REQ-002", "REQ-003"])
        self._verify_all()
        self.assertEqual((self._launches("a"), self._launches("b"), self._launches("c")), (2, 2, 2))


class TestPathMatching(unittest.TestCase):
    def test_globs_and_directory_prefixes(self):
        self.assertTrue(lib.path_in_scope("src/a/mod.py", ["src/a"]))
        self.assertTrue(lib.path_in_scope("src/a/mod.py", ["src/a/"]))
        self.assertTrue(lib.path_in_scope("src/a/deep/mod.py", ["src/a/*"]))
        self.assertTrue(lib.path_in_scope("src/a/mod.py", ["src/*/mod.py"]))
        self.assertFalse(lib.path_in_scope("src/ab/mod.py", ["src/a"]))
        self.assertFalse(lib.path_in_scope("docs/x.md", ["src/a", "src/b/*.py"]))

    def test_scoped_digest_binds_its_patterns_and_matching_entries_only(self):
        entries = {"src/a/x.py": "1", "src/b/y.py": "2"}
        base = lib.scoped_digest(entries, ["src/a"])
        self.assertEqual(base, lib.scoped_digest({**entries, "src/b/y.py": "3"}, ["src/a"]))
        self.assertNotEqual(base, lib.scoped_digest({**entries, "src/a/x.py": "3"}, ["src/a"]))
        self.assertNotEqual(base, lib.scoped_digest(entries, ["src/a", "src/c"]))


if __name__ == "__main__":
    unittest.main()
