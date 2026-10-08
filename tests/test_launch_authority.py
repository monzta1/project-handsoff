"""#407: launch authority. Under a host Supervisor automatic recovery and
orchestration never launch an Implementer: a lost or stopped one is reported
(not_applicable, host_supervised, naming the session) and waits; a Reviewer
is still recovered. No implementer launch is admitted without a task, nor,
once any implementer in the run declared owned paths, without its own; both
are refused before a session is reserved. Every dashboard-initiated launch
records a dashboard_launch event and prints one line."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase, _harness_env
from tests.engine_patch import patch_engine

sys.path.insert(0, str(BIN))
import handsoff_agent  # noqa: E402
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402

LOST_IMPLEMENTER = "hs-" + "1" * 32
LIVE_OWNED = "hs-" + "2" * 32
OLD_OWNED = "hs-" + "3" * 32
LOST_REVIEWER = "hs-" + "4" * 32


def session(sid, role, state, when, **extra):
    terminal = state not in lib.AGENT_SESSION_LIVE_STATES
    return {"session_id": sid, "role": role, "actor": f"codex-{role}", "adapter": "codex",
            "requested_model": "default", "reported_model": None, "resolution_source": "configured",
            "started_at": when, "running_at": when, "ended_at": when if terminal else None,
            "state": state, "exit_code": 1 if terminal else None, **extra}


def failure(sid, category="non_zero_exit"):
    return {"session_id": sid, "category": category, "reason": lib._FAILURE_REASON_LABELS[category],
            "tail_sha256": "0" * 64, "at": datetime.now(timezone.utc).isoformat()}


class LaunchAuthorityCase(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        # build_launch_spec checks the drop-in first; without the prompts every
        # admission test read the integrity refusal instead of its own
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts", copy_function=shutil.copyfile)
        self.init("Launch authority #407")
        self.now = datetime.now(timezone.utc)
        self.stale = (self.now - timedelta(minutes=30)).isoformat()

    def host_supervisor(self):
        import re
        toml = self.tmp / "handsoff.toml"
        # the first supervisor key is the [agents] one; [models] follows it
        toml.write_text(re.sub(r'(?m)^supervisor = "[^"]*"', 'supervisor = "host"', toml.read_text(), count=1))
        self.assertTrue(lib.host_supervised(lib.load_config(self.tmp)))

    def commit(self, phase, sessions, pointers, failures=None):
        status = self.read_status()
        status.update(phase_number=phase, phase=lib.PHASES[phase], updated_at=self.stale,
                      agent_sessions={item["session_id"]: item for item in sessions},
                      current_agent_sessions=pointers)
        status.pop("agent_failures", None)
        if failures:
            status["agent_failures"] = {item["session_id"]: item for item in failures}
        cfg = lib.load_config(self.tmp)
        with lib.project_lock(self.tmp):
            lib.commit(self.tmp, cfg, status=status, event_kind="test_setup", event_message="fixture")
        return status

    def ledger(self):
        return ((self.tmp / "handsoff-status.json").read_bytes(),
                (self.tmp / "handsoff-events.jsonl").read_bytes())

    def recover(self):
        launched = []

        def launcher(role, owned_paths=None):
            launched.append((role, owned_paths))
            return True
        result = lib.recover_run(self.tmp, actor="Mission Control Watchdog", launcher=launcher, now=self.now)
        return result, launched


class HostSupervisorRecovery(LaunchAuthorityCase):
    def test_a_stopped_implementer_under_a_host_supervisor_is_not_replaced_after_the_stall_window(self):
        self.host_supervisor()
        for state in ("failed", "running"):
            with self.subTest(state=state):
                status = self.commit(4, [session(LOST_IMPLEMENTER, "implementer", state, self.stale)],
                                     {"implementer": LOST_IMPLEMENTER} if state == "running" else {},
                                     [failure(LOST_IMPLEMENTER)] if state == "failed" else None)
                cfg = lib.load_config(self.tmp)
                assessment = lib.recovery_assessment(status, cfg, {}, [], self.now, root=self.tmp)
                self.assertEqual((assessment["state"], assessment["reason"], assessment["lost_session_id"]),
                                 ("not_applicable", "host_supervised", LOST_IMPLEMENTER), assessment)
                result, launched = self.recover()
                self.assertEqual(result["action"], "skipped")
                self.assertEqual(launched, [], "a host-supervised implementer was replaced")
                self.assertEqual(self.read_status()["agent_sessions"][LOST_IMPLEMENTER]["state"], state)

    def test_the_same_implementer_without_a_host_supervisor_is_still_recovered(self):
        # the control: the rule is the host Supervisor, not the implementer
        self.commit(4, [session(LOST_IMPLEMENTER, "implementer", "failed", self.stale)], {},
                    [failure(LOST_IMPLEMENTER)])
        result, launched = self.recover()
        self.assertEqual(launched, [("implementer", None)], result)

    def test_a_lost_reviewer_under_a_host_supervisor_is_still_recovered(self):
        self.host_supervisor()
        self.commit(5, [session(LOST_REVIEWER, "reviewer", "failed", self.stale)], {},
                    [failure(LOST_REVIEWER)])
        result, launched = self.recover()
        self.assertEqual(launched, [("reviewer", None)], result)

    def test_reviewer_recovery_beside_live_and_historical_owned_implementers_still_launches(self):
        for host in (False, True):
            with self.subTest(host_supervisor=host):
                self.tearDown()
                self.setUp()
                if host:
                    self.host_supervisor()
                self.commit(5, [
                    session(OLD_OWNED, "implementer", "completed", self.stale, owned_paths=["src/old"]),
                    session(LIVE_OWNED, "implementer", "running", self.stale, owned_paths=["src/live"]),
                    session(LOST_REVIEWER, "reviewer", "failed", self.stale),
                ], {"implementer": LIVE_OWNED}, [failure(LOST_REVIEWER)])
                lib.update_session_liveness(self.tmp, LIVE_OWNED, at=self.now.isoformat())
                result, launched = self.recover()
                self.assertEqual(launched, [("reviewer", None)], result)
                # while an undeclared implementer beside them is still refused
                status = self.read_status()
                with self.assertRaises(lib.HandsoffError) as refused:
                    lib.create_agent_session(self.tmp, role="implementer", actor="codex-implementer",
                                             adapter="codex", requested_model="default",
                                             resolution_source="configured")
                self.assertIn("declared owned paths", str(refused.exception))
                self.assertEqual(self.read_status(), status)

    def test_orchestration_never_hands_off_to_an_implementer_under_a_host_supervisor(self):
        self.host_supervisor()
        status = self.commit(4, [session(LOST_IMPLEMENTER, "implementer", "completed", self.stale)], {})
        status["pending_questions"] = [{"role": "implementer", "answer": "yes", "delivered_at": None}]
        self.assertIsNone(lib.managed_handoff_role(status, lib.load_config(self.tmp)))
        # and the dashboard loop refuses even if a handoff role were offered
        fake = mock.Mock(project_root=self.tmp)
        fake._take_first_launch.return_value = None
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("auto_handoff = false", "auto_handoff = true"))
        with patch_engine("managed_handoff_role", return_value="implementer"), \
                mock.patch.object(dashboard.supervisor, "advance_approved_design", return_value=False):
            dashboard.DashboardServer._orchestrate_once(fake)
        fake._launch_managed_role.assert_not_called()


class LaunchAdmission(LaunchAuthorityCase):
    def test_a_taskless_implementer_launch_is_refused_with_nothing_written(self):
        before = self.ledger()
        for task in ("", "   \n"):
            with self.subTest(task=repr(task)):
                with self.assertRaises(lib.HandsoffError) as refused:
                    handsoff_agent.build_launch_spec(self.tmp, "implementer", task)
                self.assertIn("task must be a non-empty string", str(refused.exception))
        env = {key: value for key, value in _harness_env().items()
               if key not in (handsoff_agent.MANAGED_ROLE_ENV, handsoff_agent.MANAGED_SESSION_ENV)}
        cli = subprocess.run([sys.executable, str(BIN / "handsoff_agent.py"), "--root", str(self.tmp),
                              "launch", "implementer", "--task", " "],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(cli.returncode, 1, cli.stdout + cli.stderr)
        self.assertIn("task must be a non-empty string", cli.stderr)
        self.assertEqual(self.ledger(), before)

    def test_an_undeclared_implementer_launch_beside_an_owns_session_is_refused_with_nothing_written(self):
        for state in ("running", "completed"):  # live and historical
            with self.subTest(owned_session=state):
                self.commit(4, [session(LIVE_OWNED, "implementer", state, self.stale, owned_paths=["src/a"])],
                            {"implementer": LIVE_OWNED} if state == "running" else {})
                before = self.ledger()
                with self.assertRaises(lib.HandsoffError) as spec_refused:
                    handsoff_agent.build_launch_spec(self.tmp, "implementer", "Build REQ-001")
                with self.assertRaises(lib.HandsoffError) as session_refused:
                    lib.create_agent_session(self.tmp, role="implementer", actor="codex-implementer",
                                             adapter="codex", requested_model="default",
                                             resolution_source="configured")
                for refused in (spec_refused, session_refused):
                    self.assertIn(f"implementer session {LIVE_OWNED} declared owned paths", str(refused.exception))
                self.assertEqual(self.ledger(), before)

    def test_a_precreated_replacement_of_an_undeclared_implementer_is_refused_before_reservation(self):
        undeclared = "hs-" + "5" * 32
        self.commit(4, [session(OLD_OWNED, "implementer", "completed", self.stale, owned_paths=["src/a"]),
                        session(undeclared, "implementer", "failed",
                                (self.now - timedelta(minutes=20)).isoformat())],
                    {}, [failure(undeclared)])
        before = self.ledger()
        with self.assertRaises(lib.HandsoffError) as refused:
            lib.reserve_agent_replacement(self.tmp, from_session_id=undeclared,
                                          snapshotter=lambda root: {"head": None, "branch": None, "dirty": False})
        self.assertIn("declared owned paths", str(refused.exception))
        self.assertEqual(self.ledger(), before)

    def test_reviewer_and_declared_implementer_launches_are_unaffected_by_ownership(self):
        status = self.commit(4, [session(OLD_OWNED, "implementer", "completed", self.stale,
                                         owned_paths=["src/a"])], {})
        self.assertIsNone(lib.implementer_ownership_refusal(status, ["src/b"]))
        with self.assertRaises(lib.HandsoffError) as owns:
            handsoff_agent.build_launch_spec(self.tmp, "reviewer", "Review", owned_paths=["src/b"])
        self.assertIn("--owns applies to implementer launches only", str(owns.exception))
        try:
            handsoff_agent.build_launch_spec(self.tmp, "reviewer", "Review", inspection=True)
        except lib.HandsoffError as exc:
            self.assertNotIn("declared owned paths", str(exc))


class DashboardLaunchEvent(LaunchAuthorityCase):
    def events(self, kind):
        lines = (self.tmp / "handsoff-events.jsonl").read_text().splitlines()
        return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("kind") == kind]

    def test_a_dashboard_launch_records_actor_reason_role_task_digest_and_owned_paths(self):
        task = "Resume trusted Handsoff state as implementer; secret-ish task text"
        out = io.StringIO()
        fake = mock.Mock(project_root=self.tmp)
        with mock.patch("sys.stdout", out), \
                mock.patch.object(dashboard.supervisor, "performance_mutation_refusal", return_value=None), \
                mock.patch.object(handsoff_agent, "build_launch_spec", return_value="spec") as build, \
                mock.patch.object(handsoff_agent, "execute_with_recovery", return_value=0):
            code = dashboard.DashboardServer._launch_managed_role(
                fake, "implementer", task, owned_paths=["src/a"],
                launched_by=(dashboard.WATCHDOG_ACTOR, "recovery"))
        self.assertEqual(code, 0)
        build.assert_called_once()
        import hashlib
        expected = {"actor": "Mission Control Watchdog", "reason": "recovery", "role": "implementer",
                    "task_sha256": hashlib.sha256(task.encode()).hexdigest(), "owned_paths": ["src/a"]}
        [event] = self.events("dashboard_launch")
        self.assertEqual({key: event[key] for key in expected}, expected)
        self.assertNotIn("secret-ish", (self.tmp / "handsoff-events.jsonl").read_text())
        line = next(item for item in out.getvalue().splitlines() if item.startswith("HANDSOFF_DASHBOARD_LAUNCH: "))
        self.assertEqual(json.loads(line.split(": ", 1)[1]), expected)
        self.assertEqual(dashboard.ORCHESTRATOR_ACTOR, "Mission Control Orchestrator")

    def test_the_watchdog_and_orchestrator_launches_pass_their_actor_and_reason(self):
        calls = []
        fake = mock.Mock(project_root=self.tmp)
        fake._launch_managed_role.side_effect = lambda role, task, **kw: calls.append((role, kw)) or 0
        fake._take_first_launch.return_value = None
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text().replace("auto_handoff = false", "auto_handoff = true"))
        with patch_engine("managed_handoff_role", return_value="reviewer"), \
                mock.patch.object(dashboard.supervisor, "advance_approved_design", return_value=False):
            dashboard.DashboardServer._orchestrate_once(fake)
        self.assertEqual(calls, [("reviewer", {"launched_by": ("Mission Control Orchestrator", "orchestration")})])
        self.commit(4, [session(LOST_IMPLEMENTER, "implementer", "failed", self.stale)], {},
                    [failure(LOST_IMPLEMENTER)])
        calls.clear()
        fake._watchdog_stop.wait.side_effect = [False, True]
        dashboard.DashboardServer._watchdog_loop(fake)
        self.assertEqual([(role, kw["launched_by"]) for role, kw in calls],
                         [("implementer", ("Mission Control Watchdog", "recovery"))])

    def test_the_record_refuses_an_unknown_actor_or_reason_and_bounds_owned_paths(self):
        with self.assertRaises(lib.HandsoffError):
            lib.dashboard_launch_record("someone", "recovery", "reviewer", "t", None)
        with self.assertRaises(lib.HandsoffError):
            lib.dashboard_launch_record("Mission Control Watchdog", "because", "reviewer", "t", None)
        record = lib.dashboard_launch_record("Mission Control Orchestrator", "orchestration", "reviewer", "t",
                                             ["p" * 300] * 40)
        self.assertEqual(len(record["owned_paths"]), 32)
        self.assertTrue(all(len(path) == 256 for path in record["owned_paths"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
