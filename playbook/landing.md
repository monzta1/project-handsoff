# Landing a lane

`main` is protected (required check `tests`, strict), so a lane lands
through a pull request, and the live checks compare the installed engine
with the checkout. The order, with the refusal each step prevents:

1. `record-symptom-resolved --by <you> --evidence <vr of REQ-001>`;
   `advance 5 50 --implemented-by <you>`; `record-review` if the managed
   verdict did not record itself; `advance 6 60 --implemented-by <you>`.
2. `git push -u origin <branch>`, `gh pr create`, `ci-watch --pr N --by
   <you>` (the run's board shows the checks), `gh pr merge N --auto --merge
   --delete-branch`; wait for MERGED. A red shard on a green tree: `gh run
   rerun <id> --failed` reruns only the red jobs; a real failure is a new
   commit and a full run by design. Stay in the worktree: its tree is what
   `main` now holds, so the evidence stands.
3. `advance 7 70`.
4. Cut the release from the merged commit: bump `pyproject` to the next
   free PATCH number read from `gh release list`. Patch only, in every
   repository of the house (Handsoff, the Miner, Sentinel, Beakon): a lane
   is never a minor or a major bump, whatever it adds; Moncy decides a
   minor, nobody else (2026-09-21, after two lanes went 0.1.0 to 0.2.0).
   Two hosts may release from
   one machine; never take a number from a brief), regenerate the manifest,
   an ANNOTATED tag on the merged sha (`git tag -a vX.Y.Z <sha> -m vX.Y.Z`; a
   lightweight tag fails `live_release_smoke`; deleting a tag under a
   published release turns it into a draft), `gh release create` with the
   wheel.
5. `handsoff install-check` first: it refuses while any registered run has
   a live managed session (root, role and session id named), because two
   sessions share one venv and a launch must never start against an engine
   half replaced; finish or cancel the session, or `--force --by <you>`,
   which is ledgered on each affected run (#216). Then install it
   (INSTALL.md "Clean patch upgrade") and kickstart the Fleet LaunchAgent. Until this step `verify-live` refuses: "installed engine is
   X, this checkout is Y: install the release first". Wait a minute for the
   Fleet signals cache.
6. `verify-live --by <pilot>`; a "latest release reads X, gh says Y" line
   means wait a minute and run it again; a metrics smoke that wants a "free
   pass" needs the board to be quiet for a minute.
7. `work-item-update issue-N --by <you> --implemented-by <you>` for every
   item; `advance 8 100`. If it refuses "the rules set changed since it was
   recorded (engine:version)", the review was recorded under the previous
   engine: `record-review --by <reviewer> --reaffirm ...` re-binds it.
8. Close the issue with the written result per acceptance line,
   `run-close --by <you> --reason`, copy the four ledgers plus
   `.handsoff-event-head.json` into `.handsoff-archive/<date>-lane-<x>/`,
   then `handsoff fleet unregister <worktree>` and only then
   `git worktree remove` and delete the branch. `run-close` always comes
   before the worktree goes: a root removed with its run still open is
   forgotten by Fleet after one pass, but the log line says the run was
   never closed (#207). The dashboard port is released by `advance 8`.

**Docs-only lane.** A lane that changes only Markdown (README.md, docs/,
the playbook) is criteria, the reviewer, a pull request and the merge:
no version bump, no release, no `verify-live`. CI classifies the pull
request from its diff and runs the `docs` job alone (the documentation
audit and the docs suites, about twenty seconds) in place of the shards;
the wheel does not carry the documentation, so the release smoke is not
in play. In that lane's worktree set `require_live_verification = false`
in `handsoff.toml` (never committed) so Phase 8 needs no live run;
`run-close` after the merge. A lane that touches one line of code with
its Markdown is not docs-only; send the Markdown alone (#227).
