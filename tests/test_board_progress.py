"""#415 REQ-002: the board progresses on its own.

A work item becomes implemented only through an explicit binding (launch
implementer --item ID), credited when that session completes and its
workspace applies; it is done when every criterion tagged to it passes.
When no progress was set in the current phase, status, the board and the
first HTML show one derived value between this phase's default and the
next one's."""
import http.client
import io
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, run
from tests.fixture_state import write_version_pin

sys.path.insert(0, str(BIN))
import handsoff_agent as runtime  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class _Pipe:
    def write(self, _value):
        return None

    def close(self):
        return None


class _Process:
    """A fake adapter child that does `work` in its working directory and exits 0."""
    pid = 4243

    def __init__(self, cwd, work=None):
        if work:
            work(Path(cwd))
        self.returncode = 0
        self.stdin = _Pipe()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


ORIGINAL = {"a.txt": "a original\n", "b.txt": "b original\n"}


class BoardProgressTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        write_version_pin(self.tmp)
        r = run(["init", "Board progress", "--item", "#101", "--item", "#102"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for cid, kind, tag in (("REQ-001", "primary_fix", "#101"), ("REQ-002", "supporting", "#102")):
            r = run(["criterion-add", cid, "--type", kind, "--requirement", f"[{tag}] behaviour for {tag}",
                     "--verification", "automated", "--test", "true"], cwd=self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name, text in ORIGINAL.items():
            (self.tmp / name).write_text(text)
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, lib.implementer_workspace_dir(self.tmp), True)

    # helpers

    def items(self):
        return {item["id"]: item for item in self.read_acceptance()["work_items"]}

    def spec(self, owned=None, items=None):
        return runtime.LaunchSpec("implementer", "codex", "default", ("/bin/codex", "exec", "-"),
                                  str(self.tmp), "bounded task", project_root=str(self.tmp.resolve()),
                                  owned_paths=tuple(owned) if owned else None,
                                  work_items=tuple(items) if items else None)

    def launch(self, spec, work=None, actor=None):
        def factory(argv, **kwargs):
            return _Process(kwargs["cwd"], work=work)
        with mock.patch("sys.stdout", io.StringIO()):
            try:
                return runtime.execute_launch(spec, popen_factory=factory, beacon_interval=0.01,
                                              actor=actor), None
            except runtime.AgentLaunchError as exc:
                return None, exc

    def session_ids(self):
        return list(self.read_status()["agent_sessions"])

    def status_json(self):
        r = run(["status"], cwd=self.tmp)
        return json.loads(r.stdout)

    # binding and crediting

    def test_two_items_bound_to_two_sessions_are_each_credited_only_by_their_own(self):
        code, error = self.launch(self.spec(["a.txt"], ["issue-101"]),
                                  work=lambda cwd: (cwd / "a.txt").write_text("a done\n"), actor="impl-one")
        self.assertIsNone(error)
        self.assertEqual(code, 0)
        first = self.read_status()["agent_sessions"][self.session_ids()[-1]]
        self.assertEqual(first["work_items"], ["issue-101"])
        items = self.items()
        self.assertEqual(items["issue-101"]["implemented_by"], "impl-one")
        self.assertTrue(items["issue-101"]["implemented_at"])
        self.assertNotIn("implemented_at", items["issue-102"])
        credited_at = items["issue-101"]["implemented_at"]

        code, error = self.launch(self.spec(["b.txt"], ["issue-102"]),
                                  work=lambda cwd: (cwd / "b.txt").write_text("b done\n"), actor="impl-two")
        self.assertIsNone(error)
        items = self.items()
        self.assertEqual(items["issue-102"]["implemented_by"], "impl-two")
        # the second session never touched the first item
        self.assertEqual((items["issue-101"]["implemented_by"], items["issue-101"]["implemented_at"]),
                         ("impl-one", credited_at))
        self.assertEqual(lib.validate_acceptance_schema(self.read_acceptance()), [])
        self.assertEqual(lib.validate_status_schema(self.read_status()), [])
        # the board reads an implemented item as started, never not_started
        rows = {row["id"]: row for row in lib.derive_work_items(
            self.read_status(), self.read_acceptance(), lib.load_config(self.tmp))["items"]}
        self.assertEqual(rows["issue-101"]["status"], "in_progress")
        self.assertEqual(rows["issue-101"]["implemented_by"], "impl-one")

    def test_an_unbound_completion_credits_nothing_even_when_its_paths_match(self):
        code, error = self.launch(self.spec(["a.txt"]),
                                  work=lambda cwd: (cwd / "a.txt").write_text("a done\n"), actor="impl-one")
        self.assertIsNone(error)
        session = self.read_status()["agent_sessions"][self.session_ids()[-1]]
        self.assertEqual((session["state"], session["apply"]["state"]), ("completed", "applied"))
        self.assertNotIn("work_items", session)
        self.assertFalse(any("implemented_at" in item for item in self.items().values()))

    def test_a_failed_apply_credits_nothing(self):
        def outside(cwd):
            (cwd / "a.txt").write_text("a done\n")
            (cwd / "b.txt").write_text("outside ownership\n")
        code, error = self.launch(self.spec(["a.txt"], ["issue-101"]), work=outside, actor="impl-one")
        self.assertIsNotNone(error)
        session = self.read_status()["agent_sessions"][self.session_ids()[-1]]
        self.assertEqual((session["state"], session["apply"]["state"]), ("failed", "refused"))
        self.assertFalse(any("implemented_at" in item for item in self.items().values()))

    def test_a_bound_sole_implementer_without_a_workspace_is_credited_on_completion(self):
        code, error = self.launch(self.spec(None, ["issue-102"]), actor="impl-solo")
        self.assertIsNone(error)
        items = self.items()
        self.assertEqual(items["issue-102"]["implemented_by"], "impl-solo")
        self.assertNotIn("implemented_at", items["issue-101"])

    def test_item_must_name_an_existing_work_item_on_an_implementer(self):
        before = (self.tmp / "handsoff-status.json").read_bytes()
        with self.assertRaisesRegex(lib.HandsoffError, "unknown work item issue-999"):
            lib.create_agent_session(self.tmp, role="implementer", actor="impl", adapter="codex",
                                     requested_model="default", resolution_source="configured",
                                     work_items=["issue-999"])
        with self.assertRaisesRegex(lib.HandsoffError, "implementer"):
            lib.create_agent_session(self.tmp, role="reviewer", actor="rev", adapter="codex",
                                     requested_model="default", resolution_source="configured",
                                     work_items=["issue-101"])
        with self.assertRaisesRegex(lib.HandsoffError, "repeat"):
            lib.validate_bound_work_items(self.read_acceptance(), ["issue-101", "issue-101"])
        self.assertEqual((self.tmp / "handsoff-status.json").read_bytes(), before)
        # refused at the launch builder too, before any session or reservation
        with mock.patch.object(lib, "validate_runtime_integrity", return_value={}):
            with self.assertRaisesRegex(lib.HandsoffError, "unknown work item issue-7"):
                runtime.build_launch_spec(self.tmp, "implementer", "task", work_items=["issue-7"])
            with self.assertRaisesRegex(lib.HandsoffError, "--item applies to implementer launches only"):
                runtime.build_launch_spec(self.tmp, "reviewer", "task", work_items=["issue-101"])
        r = subprocess.run([sys.executable, str(BIN / "handsoff_agent.py"), "launch", "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertIn("--item ID", r.stdout)

    # done, and the 95% refusal

    def test_all_tagged_criteria_passing_marks_the_item_done(self):
        acceptance = self.read_acceptance()
        status = self.read_status()
        cfg = lib.load_config(self.tmp)
        rows = {row["id"]: row for row in lib.derive_work_items(status, acceptance, cfg)["items"]}
        self.assertEqual(rows["issue-101"]["status"], "not_started")
        self.assertEqual(rows["issue-101"]["unmet_criteria"], ["REQ-001"])
        for criterion in acceptance["criteria"]:
            if criterion["id"] == "REQ-001":
                criterion["state"] = "passing"
        rows = {row["id"]: row for row in lib.derive_work_items(status, acceptance, cfg)["items"]}
        self.assertEqual(rows["issue-101"]["status"], "done")
        self.assertEqual(rows["issue-101"]["unmet_criteria"], [])
        self.assertEqual(rows["issue-102"]["status"], "not_started")
        self.assertEqual(rows["issue-101"]["done_when"], lib.WORK_ITEM_DONE_CONDITION)
        self.assertEqual(lib.WORK_ITEM_DONE_CONDITION, "a work item is done when every criterion tagged to it passes")
        # status states the same condition, per item
        shown = self.status_json()
        self.assertEqual(shown["work_item_done_condition"], lib.WORK_ITEM_DONE_CONDITION)
        self.assertEqual({item["id"]: item["done_when"] for item in shown["work_items"]},
                         {"issue-101": lib.WORK_ITEM_DONE_CONDITION, "issue-102": lib.WORK_ITEM_DONE_CONDITION})

    def test_the_95_percent_refusal_names_the_unmet_items_and_the_condition(self):
        r = run(["advance", "2", "96"], cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0)
        for item, criterion in (("issue-101", "REQ-001"), ("issue-102", "REQ-002")):
            self.assertIn(f"progress gate: required work item {item} must be done before 95%+ "
                          f"({lib.WORK_ITEM_DONE_CONDITION}; not passing: {criterion})", r.stdout)
        self.assertEqual(self.read_status()["phase_number"], 1)

    # derived progress

    def view(self, phase, criteria=(), items=(), **status):
        acceptance = {"criteria": [{"id": f"REQ-{i:03d}", "state": state} for i, state in enumerate(criteria, 1)],
                      "work_items": [dict({"id": f"issue-{i}"}, **({"implemented_at": "t", "implemented_by": "x"}
                                                                   if done else {}))
                                     for i, done in enumerate(items, 1)]}
        return lib.progress_view({"phase_number": phase, "progress": status.pop("progress", 0), **status},
                                 acceptance, lib.DEFAULT_CONFIG)

    def test_derived_progress_values(self):
        self.assertEqual(lib.PHASE_DEFAULT_PROGRESS, {1: 0, 2: 20, 3: 30, 4: 40, 5: 50, 6: 75, 7: 90, 8: 95})
        # base + floor(fraction x (band - 1))
        self.assertEqual(self.view(3, ["passing", "failing"], [True, False])["progress"], 30 + 4)
        self.assertEqual(self.view(6, ["passing", "failing", "failing"], [True])["progress"], 75 + 7)
        self.assertEqual(self.view(2, ["passing"], [False])["progress"], 20 + 4)
        view = self.view(4, ["passing", "passing"], [True, False])
        self.assertEqual((view["progress"], view["source"], view["base"], view["next_phase_default"]),
                         (40 + 6, "derived", 40, 50))

    def test_boundaries_never_reach_the_next_phase_or_100(self):
        for phase in range(1, 9):
            # empty denominators read the phase's default
            self.assertEqual(self.view(phase)["progress"], lib.PHASE_DEFAULT_PROGRESS[phase], phase)
            # every criterion passing and every item implemented, landing gates unmet
            full = self.view(phase, ["passing"] * 3, [True, True])["progress"]
            self.assertEqual(full, lib.PHASE_DEFAULT_PROGRESS.get(phase + 1, 100) - 1, phase)
            self.assertLess(full, 100)
        self.assertEqual(self.view(8, ["passing"], [True])["progress"], 99)
        # the run's completion is what reaches 100
        self.assertEqual(self.view(8, ["passing"], [True], status="complete")["progress"], 100)

    def test_set_only_in_the_phase_it_was_recorded_in(self):
        view = self.view(4, ["failing"], [False], progress=47, progress_set_phase=4)
        self.assertEqual((view["progress"], view["source"]), (47, "set"))
        inherited = self.view(5, ["failing"], [False], progress=47, progress_set_phase=4)
        self.assertEqual((inherited["progress"], inherited["source"]), (50, "derived"))
        legacy = self.view(3, ["failing"], [False], progress=30)
        self.assertEqual(legacy["source"], "derived")

    # status, board and first HTML agree

    def serve(self):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def first_html(self, server):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/")
        html = connection.getresponse().read().decode("utf-8")
        connection.close()
        return html

    def test_status_board_and_first_html_show_one_value(self):
        r = run(["advance", "2"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        code, error = self.launch(self.spec(None, ["issue-101"]), actor="impl-one")
        self.assertIsNone(error)
        # 2 criteria none passing, 2 items one implemented: 20 + floor(0.25 x 9)
        expected = 22
        shown = self.status_json()
        self.assertEqual((shown["display_progress"], shown["progress_source"]), (expected, "derived"))
        board = dashboard.build_snapshot(self.tmp)["status"]
        self.assertEqual((board["progress"], board["progress_source"]), (expected, "derived"))
        self.assertEqual(dashboard.first_html_progress(self.tmp), expected)
        html = self.first_html(self.serve())
        self.assertIn(f'<strong id="topbar-overall">{expected}%</strong>', html)
        self.assertIn(f'<strong id="progress-value">{expected}</strong>', html)
        self.assertIn(f'aria-valuenow="{expected}"', html)
        self.assertNotIn('<strong id="topbar-overall">0%</strong>', html)

    def test_an_explicit_advance_value_is_set_for_its_phase(self):
        r = run(["advance", "2", "25"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_status()["progress_set_phase"], 2)
        shown = self.status_json()
        self.assertEqual((shown["display_progress"], shown["progress_source"]), (25, "set"))
        self.assertEqual(dashboard.build_snapshot(self.tmp)["status"]["progress"], 25)
        html = self.first_html(self.serve())
        self.assertIn('<strong id="topbar-overall">25%</strong>', html)
        self.assertTrue(re.search(r'<strong id="progress-value">25</strong>', html))
