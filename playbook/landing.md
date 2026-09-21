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
   free patch number read from `gh release list` (two hosts may release from
   one machine; never take a number from a brief), regenerate the manifest,
   an ANNOTATED tag on the merged sha (`git tag -a vX.Y.Z <sha> -m vX.Y.Z`; a
   lightweight tag fails `live_release_smoke`; deleting a tag under a
   published release turns it into a draft), `gh release create` with the
   wheel.
5. Install it (INSTALL.md "Clean patch upgrade") and kickstart the Fleet
   LaunchAgent. Until this step `verify-live` refuses: "installed engine is
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
   remove the worktree and branch. The dashboard port is released by
   `advance 8`.
