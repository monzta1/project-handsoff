"""#298: costly shards wait behind fast gates, and superseded revisions die.

The v0.3.80 closeout launched several full CI matrices while earlier
revisions were already obsolete, and deterministic failures surfaced only
after the expensive jobs had started. These tests pin the workflow shape
that prevents both.

The workflow is parsed without PyYAML: this project ships with no runtime
dependencies and none of its suites add one. The parse is deliberately
narrow, covering only the `concurrency` mapping and each job's `needs`,
which is all these criteria assert.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: The five-way matrix and the dashboard suite: everything that costs runner
#: minutes and must never start for a revision that cannot pass.
COSTLY_JOBS = ("python", "dashboard", "docs")


def _lines():
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def parse_jobs() -> dict[str, dict]:
    """Return {job: {"needs": [...]}} for every job under `jobs:`.

    Job keys sit at exactly two spaces of indent inside the top-level
    `jobs:` block; their own keys sit at four. That is the whole grammar
    these tests need.
    """
    jobs: dict[str, dict] = {}
    in_jobs = False
    current = None
    for line in _lines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^\S", line):
            break  # a new top-level key ends the jobs block
        if not in_jobs:
            continue
        job = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if job:
            current = job.group(1)
            jobs[current] = {"needs": []}
            continue
        if current is None:
            continue
        needs = re.match(r"^    needs:\s*(.+?)\s*$", line)
        if needs:
            value = needs.group(1)
            if value.startswith("["):
                jobs[current]["needs"] = [
                    item.strip() for item in value.strip("[]").split(",") if item.strip()
                ]
            else:
                jobs[current]["needs"] = [value]
    return jobs


def parse_concurrency() -> dict[str, str]:
    """Return the top-level `concurrency:` mapping."""
    found: dict[str, str] = {}
    in_block = False
    for line in _lines():
        if re.match(r"^concurrency:\s*$", line):
            in_block = True
            continue
        if in_block:
            entry = re.match(r"^  ([a-z-]+):\s*(.+?)\s*$", line)
            if entry:
                found[entry.group(1)] = entry.group(2)
                continue
            if line.strip() and not line.startswith("  "):
                break
    return found


class TheWorkflowParses(unittest.TestCase):

    def test_the_workflow_exists_and_declares_the_expected_jobs(self):
        jobs = parse_jobs()
        for name in ("preflight", "changes", "tests", *COSTLY_JOBS):
            self.assertIn(name, jobs, f"{name} is missing from {WORKFLOW.name}")


class SupersededRevisionsAreCancelled(unittest.TestCase):

    def test_a_concurrency_group_is_declared(self):
        self.assertTrue(parse_concurrency(), "ci.yml declares no concurrency group")

    def test_the_group_is_bound_to_the_pull_request_head(self):
        group = parse_concurrency().get("group", "")
        self.assertIn("github.event.pull_request.number", group,
                      "the group must key on the pull request, so one PR's revisions share it")
        self.assertIn("github.ref", group,
                      "a push outside a pull request needs its own group rather than sharing one")

    def test_in_progress_runs_are_cancelled_for_a_pull_request(self):
        cancel = parse_concurrency().get("cancel-in-progress", "")
        self.assertIn("pull_request", cancel,
                      "cancellation is scoped to pull requests")

    def test_main_is_not_cancelled(self):
        """main's record of green runs is what a release reads; cancelling it
        would destroy that history to save nothing."""
        cancel = parse_concurrency().get("cancel-in-progress", "")
        self.assertNotEqual(cancel.strip().lower(), "true",
                            "unconditional cancellation would also cancel pushes to main")
        self.assertIn("github.event_name ==", cancel)


class CostlyJobsWaitForTheFastGate(unittest.TestCase):

    def test_the_preflight_gates_every_costly_job(self):
        jobs = parse_jobs()
        for name in COSTLY_JOBS:
            self.assertIn("preflight", jobs[name]["needs"],
                          f"{name} can start before the manifest is known good")

    def test_the_preflight_itself_waits_for_nothing(self):
        """It is the first thing that runs, and it is not gated on `changes`:
        playbook/*.md and prompts/*.md are covered runtime files that classify
        as docs-only, so a docs-only change can still stale the manifest."""
        self.assertEqual(parse_jobs()["preflight"]["needs"], [])

    def test_the_five_python_shards_are_still_five(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("shard: [0, 1, 2, 3, 4]", text,
                      "the matrix width changed; the staging assertions assume five")

    def test_the_preflight_runs_the_canonical_command(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("bin/handsoff_preflight.py", text,
                      "the fast gate must run the one canonical preflight, not a bespoke copy")


class TheRequiredCheckStaysHonest(unittest.TestCase):

    def test_the_aggregate_depends_on_the_preflight(self):
        self.assertIn("preflight", parse_jobs()["tests"]["needs"])

    def test_the_aggregate_fails_on_a_red_or_cancelled_preflight(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('test "${{ needs.preflight.result }}" = "success"', text,
                      "only an explicit success may pass; cancelled and skipped are not coverage")

    def test_cancelled_coverage_is_never_reported_as_passing(self):
        """Every result the aggregate consults is compared against "success"
        exactly, so `cancelled` and `skipped` can never satisfy it."""
        text = WORKFLOW.read_text(encoding="utf-8")
        for job in ("preflight", "docs", "python", "dashboard"):
            self.assertIn(f'needs.{job}.result }}}}" = "success"', text,
                          f"{job}'s result is not compared against success exactly")

    def test_the_aggregate_is_the_one_required_context(self):
        jobs = parse_jobs()
        self.assertEqual(
            sorted(jobs["tests"]["needs"]),
            sorted(["changes", "preflight", "python", "dashboard", "docs"]),
            "the required check must gather every gate, so its name stays stable",
        )


if __name__ == "__main__":
    unittest.main()
