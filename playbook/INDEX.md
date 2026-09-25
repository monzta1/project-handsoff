# Handsoff playbook

Engine knowledge: how to run a lane, in any repository, on any machine
that installed Handsoff. `handsoff playbook` prints this index; `handsoff
playbook <topic>` prints one file. Managed sessions receive `lanes` with
every launch (the briefing, #175); a host reads `lanes.md` before `init`,
`landing.md` before `advance 6`, and the lessons topic that fits the work
once per session. Project knowledge (a device's quirks, a repository's
audits, accounts) is the
project's own knowledge base, declared in `handsoff.toml [briefing]`. A
`--topic` on a launch is looked up in both: a playbook topic rides from
here, a project topic from the project's KB, a name declared in both rides
from both. The playbook part of a launch is refused past 16 KB, never
trimmed, so a file that grows is shortened by its author.

| Topic | File | Read when |
|---|---|---|
| lanes | lanes.md | before `init`: board first, criteria, proposal, reviewers, drift, parallel lanes |
| landing | landing.md | before `advance 6`: PR, release, install, verify-live, Phase 8, archive |
| reviewers | reviewers.md | before a reviewer launch: tasks, delta attempts, declines, quota routing |
| lessons-lane | lessons-lane.md | phases, criteria, tickets, parallel lanes, releases |
| lessons-agents | lessons-agents.md | launching, waiting on, and bounding managed roles |
| lessons-evidence | lessons-evidence.md | manifests, tests, proofs and their claims |
| lessons-binding | lessons-binding.md | boundaries, bindings, tests that miss |
| protocol | protocol.md | writing or reading a managed role's lines: every prefix and its fields |

A managed launch's text is assembled in this order: the playbook (this
index and `lanes.md`, plus one topic when asked), the project's knowledge
base when `[briefing]` declares one, the role's context (sandbox note,
design packet, managed design context), the role prompt, the assigned task,
then evidence, resume scope and Pilot answers. The playbook is first so it
is read first; the role prompt's own contract stays where the engine has
always put it.
