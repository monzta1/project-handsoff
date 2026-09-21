"""#218 step one: the run-page snapshot contract. schemas/snapshot.schema.json
is validated against build_snapshot's fresh output for a fixture run driven
to each of eight states, and each fresh snapshot is compared key-for-key at
the top level with its committed fixture. The validator is in-repo (type,
enum, required, properties, additionalProperties, items, minItems,
maxItems, minimum, maximum): no jsonschema dependency."""
import json
import os
import socket
import sys
import time
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, ROOT, HandsoffTestCase, approve_design_review, run

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402

SCHEMA = json.loads((ROOT / "schemas" / "snapshot.schema.json").read_text())
FIXTURES = ROOT / "tests" / "fixtures" / "snapshots"
STATES = ("in_progress", "waiting_on_pilot", "pre_authorized", "live_running", "live_failed", "complete", "closed", "offline")
REGENERATE = os.environ.get("HANDSOFF_REGENERATE_SNAPSHOT_FIXTURES") == "1"


def validate(value, schema, path="$"):
    """Return a list of violations; empty when the value fits the schema."""
    out = []
    if not isinstance(schema, dict) or not schema:
        return out
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} is not one of {schema['enum']}"]
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                  "string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                  "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None}
        if not any(checks[t](value) for t in types):
            return [f"{path}: expected {types}, got {type(value).__name__}"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                out.extend(validate(value[key], sub, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    out.append(f"{path}: unexpected key {key!r}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                out.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above {schema['maximum']}")
    return out


class SnapshotContractTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _init(self):
        r = run(["init", "Snapshot contract", "--item", "#218"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _to_phase_7(self):
        self.set_criterion_state("passing", resolved=True)
        reached = self.advance_to(7, implemented_by="impl-1", reviewed_by="rev-1")
        self.assertEqual(reached.returncode, 0, reached.stdout + reached.stderr)
        r = run(["deployment-gate", "--approve", "--by", "moncy"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def drive(self, state):
        """Drive the fixture run to `state` and return build_snapshot's output."""
        self._init()
        if state == "in_progress":
            r = run(["advance", "2", "20"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        elif state in ("waiting_on_pilot", "pre_authorized"):
            # a design the reviewer approved, awaiting the Pilot's click
            toml = self.tmp / "handsoff.toml"
            import re
            self.set_criterion_state("passing", resolved=False)  # a real criterion, as design review requires
            text = toml.read_text().replace("require_design_approval = false", "require_design_approval = true")
            text = re.sub(r'^architect = "[a-z-]+"$', 'architect = "host"', text, count=1, flags=re.M)
            toml.write_text(text)
            proposal = self.tmp / "proposal.json"
            proposal.write_text(json.dumps({"summary": "s", "approach": ["Data shape: none"], "tradeoffs": [], "decisions": ["d"],
                                            "constraints": [], "verification": ["v"]}))
            r = run(["design-propose", "--file", str(proposal), "--by", "host-architect"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run(["advance", "2", "20"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = approve_design_review(self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            if state == "pre_authorized":
                r = run(["pilot-note", "--by", "moncy", "--text", "Pre-authorized: approve the design and the deployment for this run"], cwd=self.tmp)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        elif state == "live_running":
            self._to_phase_7()
            (self.tmp / lib.LIVE_INFLIGHT_FILE).write_text(json.dumps({"total": 3, "done": 1, "current": "python3 tests/live_x.py",
                                                                        "started_at": lib.utc_now() if hasattr(lib, "utc_now") else "2026-09-21T00:00:00+00:00"}))
        elif state == "live_failed":
            self._to_phase_7()
            toml = self.tmp / "handsoff.toml"
            toml.write_text(toml.read_text().replace('live_commands = ["true"]', 'live_commands = ["false"]'))
            r = run(["verify-live", "--by", "moncy"], cwd=self.tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn('"ok": false', r.stdout)
        elif state in ("complete", "closed"):
            self._to_phase_7()
            r = run(["verify-live", "--by", "moncy"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            if state == "complete":
                r = run(["advance", "8", "100"], cwd=self.tmp)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            else:
                r = run(["run-close", "--by", "moncy", "--reason", "fixture"], cwd=self.tmp)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        elif state == "offline":
            r = run(["advance", "2", "20"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            (self.tmp / ".handsoff-dashboard-owner.json").write_text(json.dumps({
                "port": port, "host": "127.0.0.1", "run_token": "dead", "pid": 1, "feature": "x",
                "root_sha256": lib.dashboard_root_sha256(self.tmp), "started_at": "2026-09-19T00:00:00+00:00"}))
        return dashboard.build_snapshot(self.tmp)

    def check(self, state, expect, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.drive(state)
        problems = validate(snapshot, SCHEMA)
        self.assertEqual(problems, [], f"{state}: {problems[:6]}")
        for key, value in expect.items():
            self.assertEqual(json.loads(json.dumps(snapshot))[key] if not callable(value) else value(snapshot), value if not callable(value) else True, key)
        fixture = FIXTURES / f"{state}.json"
        scrubbed = self.scrub(snapshot)
        if REGENERATE or not fixture.exists():
            fixture.write_text(json.dumps(scrubbed, indent=1, sort_keys=True) + "\n")
        committed = json.loads(fixture.read_text())
        # the committed fixture fits the schema too, and its top-level keys are the live ones
        self.assertEqual(validate(committed, SCHEMA), [], state)
        self.assertEqual(sorted(committed), sorted(scrubbed), f"{state}: top-level keys drifted from the fixture")
        self.assertEqual(scrubbed["initialized"], committed["initialized"], state)
        self.assertEqual(scrubbed["phases"], committed["phases"], f"{state}: phases drifted from the fixture")
        self.assertEqual(scrubbed["supervisor"]["label"], committed["supervisor"]["label"], f"{state}: label drifted")
        for key in ("required", "kind", "turn"):
            self.assertEqual(scrubbed["input_required"][key], committed["input_required"][key], f"{state}: input_required.{key} drifted")

    def scrub(self, snapshot):
        """The snapshot as JSON with this machine's paths replaced, so a
        fixture carries no home directory or temp root."""
        text = json.dumps(snapshot, default=str)
        for real, placeholder in ((str(self.tmp.resolve()), "<root>"), (str(self.tmp), "<root>"), (str(Path.home()), "<home>")):
            text = text.replace(real, placeholder)
        return json.loads(text)

    def test_in_progress(self):
        snapshot = self.drive("in_progress")
        self.assertEqual([p["state"] for p in snapshot["phases"]][:3], ["complete", "active", "upcoming"])
        self.check("in_progress", {"initialized": True}, snapshot)

    def test_waiting_on_the_pilot(self):
        snapshot = self.drive("waiting_on_pilot")
        self.assertEqual(validate(snapshot, SCHEMA), [])
        self.assertEqual((snapshot["input_required"]["turn"], snapshot["input_required"]["kind"]), ("pilot", "design_approval"))
        self.assertIsNone(snapshot["input_required"]["preauthorized"])
        self.check("waiting_on_pilot", {}, snapshot)

    def test_pre_authorized(self):
        snapshot = self.drive("pre_authorized")
        self.assertEqual(validate(snapshot, SCHEMA), [])
        self.assertEqual(snapshot["input_required"]["turn"], "pilot")
        self.assertIsNotNone(snapshot["input_required"]["preauthorized"])
        self.check("pre_authorized", {}, snapshot)

    def test_live_verification_running_and_failed(self):
        running = self.drive("live_running")
        self.assertEqual(validate(running, SCHEMA), [])
        self.assertEqual(running["verification"]["live"]["in_flight"]["done"], 1)
        self.check("live_running", {}, running)
        self.tearDown(); self.setUp()
        failed = self.drive("live_failed")
        self.assertEqual(validate(failed, SCHEMA), [])
        self.assertIs(failed["verification"]["live"]["ok"], False)
        self.assertIsNotNone(failed["verification"]["live"]["last_failure"])
        self.check("live_failed", {}, failed)

    def test_complete_closed_and_offline(self):
        for state, label in (("complete", "Mission complete"), ("closed", "Mission closed"), ("offline", None)):
            snapshot = self.drive(state)
            self.assertEqual(validate(snapshot, SCHEMA), [], state)
            if label:
                self.assertEqual(snapshot["supervisor"]["label"], label)
            self.check(state, {}, snapshot)
            self.tearDown(); self.setUp()

    def test_the_schema_is_closed_at_the_top_and_names_its_open_objects(self):
        """[#218] F1.2: every emitted key listed and required; three open objects, marked."""
        self.assertIs(SCHEMA["additionalProperties"], False)
        self.assertEqual(sorted(SCHEMA["required"]), sorted(SCHEMA["properties"]))
        for key in ("runtime", "metrics"):
            self.assertIs(SCHEMA["properties"][key]["additionalProperties"], True)
            self.assertIn("intentionally open", SCHEMA["properties"][key]["description"])
        for key in ("input_required", "supervisor", "engine"):
            self.assertIs(SCHEMA["properties"][key]["additionalProperties"], False)
        self.assertIs(SCHEMA["properties"]["phases"]["items"]["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
