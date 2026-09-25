# Lessons: running and landing a lane

Phases, criteria, tickets, parallel lanes and releases.

Each one cost a round. When a refusal, a wasted CI round or a stalled
session was caused by process (not the code), add one bullet to the
fitting file here, with the refusal line it prevents; a bullet that gets
broken twice becomes a refusal in the engine. Add, do not evict: if a
file no longer fits its budget, tests/test_playbook_budget.py says so
with the number.

- Landing order is in `landing.md`; follow it rather than remembering it.
- One `[#N]` tag per criterion scopes one item.
- `init --by <actor>` so the page names the host.
- Two hosts release from one machine: read the next patch number from `gh
  release list` at bump time; the second lane to land rebases, checks every
  merged file parses, regenerates the manifest, re-verifies, reaffirms.
- A criterion that says "every reader" gets one test per reader, named in
  the criterion; a claim in a commit message is not evidence.
- The board sees nothing while a host implements; `advance 4` before
  building.
- `init --item` takes `#174` or `issue-174`, never a bare `174`: a bare
  number becomes an "ask" item with no ticket, the run is never registered
  with Fleet, and the card is missing until `fleet register` by hand.
- The board moves only when the host records: criteria and the proposal go
  in before the first line of code, or the Pilot watches Orient for the
  whole build.
- Versions are patch only, in every repository: 0.1.0 becomes 0.1.1, not
  0.2.0, whatever the lane added. A minor is Moncy's call.
- Parallel lanes: `advance 3` and `advance 4` the moment the design is
  approved, before the first line of code, or the Pilot watches five boards
  read "Design approved" for half an hour. Every lane touches
  `handsoff.toml [checks]` and the manifest, so each landing rebases with
  the checks list as main's plus its own lines and the manifest regenerated.
- After a fix mid-review, `verify` pulls the run back to Phase 4; `advance
  5` again before the reviewer's result can dispatch, or adopt it after.
- The last merge comes before the live runs: a tests-only merge that lands
  between `verify` and `verify-live` invalidates every lane's evidence and
  costs the whole live pass again. Order: last merge, re-verify, reaffirm,
  `advance 6`, `advance 7`, `verify-live`, `advance 8`.
- A set of parallel lanes is one release, not one per lane: land them in
  sequence, bump once, install once, run `verify-live` once per lane against
  that one install.
- Several open tickets that will land together are ONE run with N items
  (`init --item "#1" --item "#2" ...`, one `[#N]` tag per criterion), one
  worktree, one pull request, one reviewer pass per phase, one live pass,
  one release. Five separate runs for five tickets on 2026-09-21 cost five
  of everything; a separate run is for a ticket that must land or release
  on its own.
- Amending a criterion revokes design approval and resets the phase; budget a
  fresh review first.
- A run that has started is carried to completion in the same session.
  A stop-time finding, a refused reviewer, a red shard or an answered
  question is work inside the run, not the end of it. Stop only on an
  explicit instruction or a Pilot gate the engine will not pass, and put
  that gate on Mission Control rather than in chat.
