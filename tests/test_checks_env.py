"""#410: [checks].env reaches every verification command and is recorded; bounded concurrency."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_lib as lib  # noqa: E402

PROBE = (
    'printf "%s|%s" "${HANDSOFF_PROBE-unset}" "${HANDSOFF_INHERITED_PROBE-unset}" > "{out}/$1"\n'
    'if [ -n "$2" ]; then\n'
    '  python3 -c "import time; print(time.time())" > "{out}/$1.start"\n'
    '  sleep "$2"\n'
    '  python3 -c "import time; print(time.time())" > "{out}/$1.end"\n'
    'fi\n'
    'exit "${3:-0}"\n'
)


class ChecksEnvTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        self.out = Path(tempfile.mkdtemp(prefix="handsoff-checks-env-"))
        self.addCleanup(shutil.rmtree, self.out, True)
        self.probe = self.out / "probe.sh"
        self.probe.write_text(PROBE.replace("{out}", str(self.out)))

    # -- fixture ----------------------------------------------------------

    def command(self, name, sleep=None, exit_code=0, script=None):
        parts = ["sh", str(script or self.probe), name]
        if sleep is not None:
            parts.append(str(sleep))
            if exit_code:
                parts.append(str(exit_code))
        return " ".join(parts)

    def configure(self, commands, *, env=None, concurrency=None, live=None, extra=""):
        toml = self.tmp / "handsoff.toml"
        text = re.sub(r"(?m)^commands = \[\]", lambda _: "commands = " + json.dumps(commands), toml.read_text())
        if live is not None:
            text = re.sub(r"(?m)^live_commands = \[\]", lambda _: "live_commands = " + json.dumps(live), text)
        lines = []
        if env is not None:
            lines.append("env = { " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items()) + " }")
        if concurrency is not None:
            lines.append(f"concurrency = {concurrency}")
        if lines:
            text = text.replace("[checks]\n", "[checks]\n" + "\n".join(lines) + "\n", 1)
        toml.write_text(text + extra)

    def criterion(self, cid, command, **extra):
        if cid == "REQ-001":
            result = run(["criterion-update", "REQ-001", "--test", command], cwd=self.tmp)
        else:
            args = ["criterion-add", cid, "--type", "supporting", "--requirement", f"Fixture {cid}",
                    "--verification", "automated", "--test", command]
            for key, value in extra.items():
                args += [f"--{key.replace('_', '-')}", str(value)]
            result = run(args, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def records(self):
        path = self.tmp / "handsoff-verifications.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def seen(self, name):
        return (self.out / name).read_text().split("|")

    def verify(self, *args, expect=0):
        result = run(["verify", *args, "--by", "test-implementer"], cwd=self.tmp)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return json.loads(result.stdout)

    # -- configuration ----------------------------------------------------

    def test_credential_like_and_malformed_tables_are_refused_at_load(self):
        self.init("Env fixture")
        for env, message in (({"GH_TOKEN": "x"}, "looks like a credential (TOKEN)"),
                             ({"APP_SECRET": "x"}, "looks like a credential (SECRET)"),
                             ({"DB_PASSWORD": "x"}, "looks like a credential (PASSWORD)"),
                             ({"API_KEY": "x"}, "looks like a credential (KEY)"),
                             ({"lower": "x"}, "must match"),
                             ({"LONG": "x" * 1025}, "at most 1024 characters"),
                             ({f"V{i}": "x" for i in range(33)}, "more than 32 variables")):
            with self.subTest(env=sorted(env)[:2]):
                with self.assertRaisesRegex(lib.HandsoffError, re.escape(message)):
                    lib.validate_check_env(env)
        toml = self.tmp / "handsoff.toml"
        original = toml.read_text()
        self.configure(["true"], env={"GH_TOKEN": "x"})
        refused = run(["status"], cwd=self.tmp)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("checks.env.GH_TOKEN looks like a credential", refused.stdout + refused.stderr)
        for value in (0, 9):
            toml.write_text(original)
            self.configure(["true"], concurrency=value)
            with self.assertRaisesRegex(lib.HandsoffError, "checks.concurrency must be an integer from 1 to 8"):
                lib.load_config(self.tmp)
        toml.write_text(original)
        self.configure(["true"], env={"HANDSOFF_PROBE": "on"}, concurrency=3)
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["check_env"], {"HANDSOFF_PROBE": "on"})
        self.assertEqual(cfg["check_concurrency"], 3)

    # -- one test per reader ------------------------------------------------

    def test_verify_passes_the_table_to_the_command_and_records_it(self):
        command = self.command("verify")
        self.configure([command], env={"HANDSOFF_PROBE": "on"})
        self.init("Env fixture")
        self.criterion("REQ-001", command)
        self.verify("--criterion", "REQ-001")
        self.assertEqual(self.seen("verify")[0], "on")
        record = self.records()[-1]
        self.assertEqual(record["env"], {"HANDSOFF_PROBE": "on"})
        self.assertEqual(record["concurrency"], 1)

    def test_verify_all_passes_the_table_to_every_command_and_records_it(self):
        first, second = self.command("all-a"), self.command("all-b")
        self.configure([first, second], env={"HANDSOFF_PROBE": "all"})
        self.init("Env fixture")
        self.criterion("REQ-001", first)
        self.criterion("REQ-002", second)
        self.verify("--all")
        self.assertEqual(self.seen("all-a")[0], "all")
        self.assertEqual(self.seen("all-b")[0], "all")
        self.assertEqual([r["env"] for r in self.records()[-2:]], [{"HANDSOFF_PROBE": "all"}] * 2)

    def test_verify_live_passes_the_table_to_the_command_and_records_it(self):
        self.configure(["true"], env={"HANDSOFF_PROBE": "live"}, live=[self.command("live")])
        self.init("Env fixture")
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        approved = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        live = run(["verify-live", "--by", "monitor"], cwd=self.tmp)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertEqual(self.seen("live")[0], "live")
        record = self.records()[-1]
        self.assertEqual(record["kind"], "live")
        self.assertEqual(record["env"], {"HANDSOFF_PROBE": "live"})

    def test_an_accepted_regression_run_passes_the_table_to_the_command_and_records_it(self):
        # named test paths, so the gate can tell the focused check from the group
        (self.tmp / "tests").mkdir(exist_ok=True)
        for name in ("regress_probe.sh", "focused_probe.sh"):
            shutil.copyfile(self.probe, self.tmp / "tests" / name)
        regression = self.command("regress", script="tests/regress_probe.sh")
        self.configure([self.command("focused", script="tests/focused_probe.sh")],
                       env={"HANDSOFF_PROBE": "regress"}, extra=(
            f'\n[[regressions]]\nname = "probe"\ncommands = {json.dumps([regression])}\n'))
        shutil.copy(BIN.parent / ".gitignore", self.tmp / ".gitignore")
        for args in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.com"],
                     ["git", "config", "user.name", "Env Test"], ["git", "add", "."],
                     ["git", "commit", "-qm", "fixture"]):
            subprocess.run(args, cwd=self.tmp, check=True, capture_output=True)
        self.init("Env fixture")
        planned = run(["release-plan", "--version", "v1.0.0", "--by", "Pilot"], cwd=self.tmp)
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        requested = run(["regression-request", "--group", "probe", "--by", "codex-supervisor"], cwd=self.tmp)
        self.assertEqual(requested.returncode, 0, requested.stdout + requested.stderr)
        item = self.read_status()["regression_requests"][-1]
        accepted = run(["regression-decide", "--request-id", item["request_id"], "--accept",
                        "--by", "Mission-Control-Pilot"], cwd=self.tmp)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        ran = run(["regression-run", "--request-id", item["request_id"], "--by", "runner"], cwd=self.tmp)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(self.seen("regress")[0], "regress")
        final = self.read_status()["regression_requests"][-1]
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["env"], {"HANDSOFF_PROBE": "regress"})

    # -- precedence and binding ----------------------------------------------

    def test_a_readers_fixed_variable_beats_the_table_and_the_inherited_environment_is_never_recorded(self):
        self.assertEqual(lib.check_environment({"check_env": {"A": "table", "B": "table"}}, {"A": "fixed"}),
                         {"A": "fixed", "B": "table"})
        self.assertIsNone(lib.check_environment({"check_env": {}}, None))
        command = self.command("seeded")
        self.configure([command], env={"HANDSOFF_PROBE": "table"})
        self.init("Env fixture")
        self.criterion("REQ-002", command, repeat=2, seed_env="HANDSOFF_PROBE")
        os.environ["HANDSOFF_INHERITED_PROBE"] = "inherited"
        self.addCleanup(os.environ.pop, "HANDSOFF_INHERITED_PROBE", None)
        self.verify("--criterion", "REQ-002")
        probe, inherited = self.seen("seeded")
        self.assertNotEqual(probe, "table", "the reader's seed wins over the table")
        self.assertRegex(probe, r"^[0-9a-f]{8}$")
        self.assertEqual(inherited, "inherited", "the inherited environment still reaches the command")
        self.assertEqual(self.records()[-1]["env"], {"HANDSOFF_PROBE": "table"})
        self.assertNotIn("HANDSOFF_INHERITED_PROBE", json.dumps(self.records()))

    def test_changing_the_table_stales_recorded_evidence(self):
        command = self.command("stale")
        self.configure([command], env={"HANDSOFF_PROBE": "one"})
        self.init("Env fixture")
        self.criterion("REQ-001", command)
        self.verify("--criterion", "REQ-001")
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), self.records())
        self.assertEqual(drift["current"], ["REQ-001"])
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace('HANDSOFF_PROBE = "one"', 'HANDSOFF_PROBE = "two"'))
        cfg = lib.load_config(self.tmp)
        drift = lib.evidence_drift(self.tmp, cfg, self.read_acceptance(), self.records())
        self.assertEqual(drift["stale"], ["REQ-001"])
        again = self.verify("--criterion", "REQ-001")
        self.assertEqual(again["launched"], [command], "evidence under the old table is not reused")
        self.assertEqual(self.seen("stale")[0], "two")

    def test_a_project_without_a_table_keeps_its_verification_config_hash(self):
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["check_env"], {})
        without = lib.verification_config_hash(cfg)
        self.assertEqual(lib.verification_config_hash({**cfg, "check_env": {}}), without)
        self.assertNotEqual(lib.verification_config_hash({**cfg, "check_env": {"A": "1"}}), without)

    # -- concurrency ----------------------------------------------------------

    def _two_slow(self, *, concurrency, second_exit=0):
        first, second = self.command("slow-a", 2), self.command("slow-b", 2, second_exit)
        self.configure([first, second], concurrency=concurrency)
        self.init("Concurrency fixture")
        self.criterion("REQ-001", first)
        self.criterion("REQ-002", second)
        started = time.monotonic()
        result = self.verify("--all", expect=0 if second_exit == 0 else 1)
        return result, time.monotonic() - started

    def _window(self, name):
        start = float((self.out / f"{name}.start").read_text().strip())
        end = float((self.out / f"{name}.end").read_text().strip())
        return start, end

    def test_concurrency_two_runs_two_slow_commands_together(self):
        result, elapsed = self._two_slow(concurrency=2)
        a_start, a_end = self._window("slow-a")
        b_start, b_end = self._window("slow-b")
        self.assertLess(max(a_start, b_start), min(a_end, b_end), "the two commands overlapped")
        self.assertLess(elapsed, 4 + 6, "two 2-second commands did not run back to back")
        self.assertEqual(set(result["criteria"]), {"REQ-001", "REQ-002"})
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        recorded = [e for e in events if e.get("kind") == "criterion_checks_recorded"]
        self.assertEqual(sorted(cid for e in recorded for cid in e["criteria"]), ["REQ-001", "REQ-002"])
        self.assertTrue(all(r["concurrency"] == 2 for r in self.records()[-2:]))

    def test_two_commands_finishing_together_one_failing_leave_a_valid_chain_and_correct_references(self):
        result, _ = self._two_slow(concurrency=2, second_exit=1)
        records, problems = lib.load_verifications(self.tmp, lib.load_config(self.tmp))
        self.assertEqual(problems, [])
        previous = "GENESIS"
        for record in records:
            self.assertEqual(record["prev_hash"], previous)
            previous = record["hash"]
        self.assertEqual(self.read_status()["verification_head"], previous)
        criteria = {c["id"]: c for c in self.read_acceptance()["criteria"]}
        by_id = {r["run_id"]: r for r in records}
        self.assertEqual(criteria["REQ-001"]["state"], "passing")
        self.assertEqual(criteria["REQ-002"]["state"], "failing")
        for cid, ok in (("REQ-001", True), ("REQ-002", False)):
            own = by_id[criteria[cid]["evidence"][-1]]
            self.assertEqual(own["criteria"], [cid])
            self.assertIs(own["ok"], ok)
            self.assertEqual(result["criteria"][cid]["run_id"], own["run_id"])
        audit = run(["validate"], cwd=self.tmp)
        self.assertNotIn("chain", audit.stdout + audit.stderr)

    def test_concurrency_one_keeps_todays_order(self):
        result, _ = self._two_slow(concurrency=1)
        _, a_end = self._window("slow-a")
        b_start, _ = self._window("slow-b")
        self.assertLessEqual(a_end, b_start + 0.5, "the second command started after the first ended")
        events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
        self.assertNotIn("criterion_checks_recorded", [e.get("kind") for e in events])
        self.assertEqual([r["criteria"] for r in self.records()[-2:]], [["REQ-001"], ["REQ-002"]])
        self.assertEqual(list(result["launched"]), [self.command("slow-a", 2), self.command("slow-b", 2)])
