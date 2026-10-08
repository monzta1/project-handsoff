# Handsoff playbook

Engine knowledge: how to run a lane, in any repository, on any machine
that installed Handsoff. `handsoff playbook` prints this index;
`handsoff playbook <topic>` prints one file. Managed sessions receive `lanes`
with every launch (#175); a host reads `lanes.md` before `init`, `landing.md`
before `advance 6`, and the fitting lessons topic once per session. Project
knowledge is the project's own KB, declared in `handsoff.toml [briefing]`. A
launch `--topic` is looked up in both. A launch's playbook part is refused
past 16 KB, never trimmed, so a growing file is shortened by its author.

| Topic | File | Read when |
|---|---|---|
| lanes | lanes.md | before `init`: board first, criteria, proposal, reviewers, drift, parallel lanes |
| crews | crews.md | choosing `[agents]`: recommended crews |
| landing | landing.md | before `advance 6`: PR, release, install, verify-live, Phase 8, archive |
| reviewers | reviewers.md | before a reviewer launch: tasks, delta attempts, declines, quota routing |
| lessons-lane | lessons-lane.md | phases, criteria, tickets, parallel lanes, releases |
| lessons-agents | lessons-agents.md | launching, waiting on, and bounding managed roles |
| lessons-evidence | lessons-evidence.md | manifests, tests, proofs and their claims |
| lessons-binding | lessons-binding.md | boundaries, bindings, tests that miss |
| lessons-proof | lessons-proof.md | would a passing test notice the code breaking |
| protocol | protocol.md | writing or reading a managed role's lines: every prefix and its fields |

Launch order: this playbook (plus one topic when asked), the project's KB,
the role's context (sandbox note, design packet), the role prompt, the task,
then evidence, resume scope and Pilot answers.
