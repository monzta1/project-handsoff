# Handsoff playbook

Engine knowledge: how to run a lane, in any repository, on any machine
that installed Handsoff. `handsoff playbook` prints this index; `handsoff
playbook <topic>` prints one file. Managed sessions receive `lanes` with
every launch (the briefing, #175); a host reads `lanes.md` before `init`,
`landing.md` before `advance 6`, `lessons.md` once per session. Project
knowledge (a device's quirks, a repository's audits, accounts) is the
project's own knowledge base, declared in `handsoff.toml [briefing]`. A
`--topic` on a launch is looked up in both: a playbook topic rides from
here, a project topic from the project's KB, a name declared in both rides
from both. The playbook part of a launch is refused past 12 KB, never
trimmed, so a file that grows is shortened by its author.

| Topic | File | Read when |
|---|---|---|
| lanes | lanes.md | before `init`: board first, criteria, proposal, reviewers, drift, parallel lanes |
| landing | landing.md | before `advance 6`: the pull request, the release, the install, verify-live, Phase 8, the archive |
| reviewers | reviewers.md | before launching a reviewer: what a task names, delta attempts, declines |
| lessons | lessons.md | once per session: every rule that cost a round, and the refusal it prevents |
