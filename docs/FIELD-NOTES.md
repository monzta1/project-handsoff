# Handsoff field notes

One entry per release that taught something, newest last: the cause, the fix, and the lesson that went into the playbook. Moved from the README on 2026-09-21 (#224).

### v0.3.22 field notes: managed Claude launches, installed-engine permissions, reaffirmed reviews (field notes, 2026-09-18)

Eight defects observed on a real thin project during one run from `init` to Phase 8 with the v0.3.22 engine, each with its cause and fix:

1. **Every managed Claude role failed at launch.** Cause: the Claude CLI refuses `--output-format stream-json` under `--print` without `--verbose`, and the adapter pre-flight used a hand-written text-mode argv that never hit the flag. Fix: one shared `lib.claude_argv` (with `--verbose` next to `--output-format stream-json`) builds the argv for both launch paths, and pre-flight now probes each adapter with the launcher's own argv shape (`lib.claude_argv` / `lib.codex_argv`, read-only role, no model override).
2. **Generated implementer permissions used the drop-in script form on an installed-engine project**, which has no `bin/`, so `record-symptom-resolved` was refused. Fix: `lib.implementer_allowed_tools` names the forms that exist for the project (
<!-- handsoff-doc: intentional -->
`python3 bin/handsoff_supervisor.py ...` for a drop-in root; `handsoff supervisor ...` plus the resolved absolute console path otherwise), and the implementer's role input lists the exact permitted forms.
3. **A product-tree change after deployment approval voided the review, and re-adopting the same verdict was refused.** Fix, the safe half: `record-review --reaffirm --by REVIEWER` re-binds the latest approved attempt after an evidence-only refresh (unchanged criterion specs), opens no attempt, spends no budget, re-runs every gate, records `review_reaffirmed`, and is refused when the design hash changed, the reviewer differs, an attempt is open, or no approved attempt exists; `session-result-adopt` replays an already-adopted approved verdict as a reaffirmation when the review was revoked. A real post-approval product change still needs a fresh review; that half is deliberately refused as unsafe.
4. **The Phase 5 reviewer launch pre-check missed `automated_and_browser` criteria without browser evidence.** Fix: the pre-check reads the ledger and names every missing kind with the command that supplies it (`verify --criterion ID`, `record-evidence ID --kind browser|manual`).
5. **A `[checks].live_commands` entry with shell operators was accepted until `verify-live`, after deployment approval.** Fix: config load refuses it with verify-live's own message, so `validate`, `status`, `doctor` and every command surface it at configuration time.
6. Review budget accounting: a re-record of an adopted verdict no longer consumes an attempt (covered by 3). Documentation-only findings still count; the cap override remains the Pilot's lever.
7. **Run write-ups inside the project root staled the run's evidence and tripped the documentation audit.** Fix: documented below; put notes under `[digest] ignore` (and `[documentation] exclude` or a `handsoff-doc: intentional` marker for the audit), or keep them outside the root.
8. **Codex reviewers could not run tests that bind a loopback listener.** Fix: workspace-write Codex sessions (the Reviewer in its scratch directory and the Implementer) get `-c sandbox_workspace_write.network_access=true`; read-only Architect and Supervisor sessions do not.

#### Keeping write-ups out of the repository digest

`[digest] ignore` in `handsoff.toml` lists glob patterns excluded from the repository digest that evidence is bound to (a match applies to the full relative path and to any path component). Run notes, field write-ups and gap lists that live inside the project root belong there, so editing them between `verify` and `advance` does not read as evidence drift. The documentation audit is a separate filter: `[documentation] exclude` keeps a file out of the audit entirely, and a `handsoff-doc: intentional` marker on the line before a deliberate release reference suppresses just that finding. Anything not covered by either is simplest kept outside the project root.

### v0.3.25 field notes: the pre-flight probe, the version pin, and the release cadence (field notes, 2026-09-18)

Three defects observed on three thin projects right after the v0.3.22 to v0.3.25 upgrade, each with its cause and fix:

1. **`doctor` reported a working Codex as `unreachable, exit code 1` inside a real project, and a launch within 24 hours would have refused the adapter on that cached result.** Cause: the probe borrowed `MIN_AGENT_TOKEN_BUDGET` (8,000 tokens at prefill weight 1.0) and ran from the project root, where the reviewer-shaped prompt plus the project's context cost 13,008 tokens; Codex answered `OK` and then exited 1 on the rollout-budget error, and the probe judged by exit code alone. Fix: the probe has its own `PREFLIGHT_TOKEN_BUDGET` (24,000), runs from a throwaway scratch directory with the managed Reviewer's exact launch shape (`--skip-git-repo-check`, scratch sandbox), and records `reachable` with the reason `OK before trailing token-budget exhaustion` when `OK` precedes a budget error, the same acceptance the launcher already applies to a complete protocol line (#114). Any other non-zero exit is still `unreachable` with the bounded, redacted reason. `tests/live_doctor_smoke.py` now asserts the installed engine reports Codex reachable on a fresh thin project and on this repository's own root.
2. **An engine upgrade read as source drift on every completed run.** Cause: `upgrade --to` rewrites `.handsoff-version`, and the pin was the one `.handsoff*` file the repository digest deliberately kept. Fix: the pin is excluded from the digest like `handsoff.toml` (#93); the engine identity stays auditable through `engine_history` and the engine recorded on every `initialized` and `agent_session_launching` event. The documentation audit still flags an exact release reference in an instruction file, and editing that file is a product-tree change, so INSTALL.md now tells thin projects to name the compatible line `0.3.*` in `AGENTS.md`, `SKILL.md` and restart prompts, or to list those files under `[digest] ignore` (below).
3. **Three patch releases in nine minutes, two of them logo-only, each costing a pin bump per project.** Cause: the upgrade runbook showed the exact-pin bump as the routine path even though `init` defaults to `0.3.*`. Fix: INSTALL.md documents the compatible line as the default (an exact-pinned project is moved to `0.3.*` once), keeps the exact bump as the strict-reproducibility exception, `docs/FIELD-PROOF.md` follows, and the release procedure and cadence rule are written down below: a release is cut when behaviour, prompts, schemas or the runtime manifest change; a cosmetic-only change to the dashboard rides with the next such release.

### v0.3.31 field note: a revised criterion could never pass amendment review (#140, 2026-09-18)

`amendment-revise` records the cumulative delta as a history, so revising a criterion the open amendment already changed appends a second operation record for the same id. `recompute_amendment_hash` then compared EVERY record's resulting hash with the registry and refused the review with "no longer matches the reviewed amendment" although nothing had drifted. It now checks the last record per id; the full history still feeds `amendment_hash`, so a delta that drifted after review is still refused. Found on the first real revise against the same criterion (ToneCommand, HeadRush lane); the existing test revised twice and only asserted that approval fails for want of a review.

### v0.3.32 field notes: work-item tombstones, amendment reviewers, the phantom selection fault (#141 #142 #144 #145, 2026-09-18)

Four defects from ToneCommand's three runs of 2026-09-18, each with cause and fix:

1. **A removed work item came back on every criteria transaction (#141).** Cause: `derive_work_item_registry` seeds items from the feature title's `#N` refs and `work-item-remove` left no trace, so `criteria-apply`, `amendment-open` and `amendment-revise` all re-added it, three times in one run. Fix: the removal is a tombstone (`acceptance.removed_work_items`, `{id, by, at}`) that derivation honours; the two deliberate ways back, `work-items-sync --item` and a criterion tagged `[#N]`, delete the tombstone in the same commit that re-adds the item.
2. **A managed reviewer's verdict during an open amendment read as a failure (#142).** Cause: the broker only knew `record-review`, which the amendment freeze refuses, so the dispatch failed and the session was marked non-recoverable, with a phase attempt spent. Fix: `handsoff agent launch reviewer --amendment <id>` records the id on the session; the broker dispatches its verdict as `amendment-review` when, and only when, that amendment is open, its hash recomputes clean, the session started after it opened, and its criteria and work items are still in the run; no attempt is opened and no budget spent. A reviewer launched without the flag while an amendment is open is refused with the flag named.
3. **`test_fleet.ProjectLogoTests` broke when this repository declared its own logo (#144).** Cause: fixture projects copy the repo's `handsoff.toml`, logo line included. Fix: `normalize_fixture_config` drops `[project] logo`.
4. **Mission Control said "live reviewer session ... has no selection metadata" on every design review (#145).** Cause: `design_reviewer_selection_view` checked a status key nothing wrote. Fix: the Phase 2 launch persists `design_reviewer_selection.current` in the commit that logs `design_reviewer_selected`; a genuine mismatch (another session id or actor) still reports.

Also in this release: Fleet cards show `STARTED` and `FINISHED` wall-clock stamps beside the elapsed clock (#143), and the card's close control reads `CLOSE RUN` (cleanly was implicit).

### v0.3.33 field notes: amendment verdicts by binding, banners by turn, live verification in view (#146, #147, #148, #149, #150)

- **#146** A reviewer session launched for an open amendment dispatches `amendment-review` whatever `kind` the reviewer wrote, with every finding on a request-changes verdict; `session-result-adopt` takes the same path for a persisted verdict. `amendment-review` gained `--finding` (at most 32, 512 characters each; never on an approval) and `--adopted-session`/`--adopted-by`; the review record carries `findings`, `adopted_session` and `adopted_by`.
- **#147** `input_required` carries `turn` (pilot, reviewer, architect, supervisor), `preauthorized` (the newest human `pilot_note` whose text says pre-authoriz..., only for the Pilot's own turn) and `amendment_round`. The briefing label and headline are derived from them (Under independent review, Architect revising, Pilot approval needed, Pre-authorized by pilot note); the amber demand styling, title flip and browser notification fire only for the Pilot's own turn; Fleet reports `waiting` only then.
- **#148** `verify-live` keeps `.handsoff-live-inflight.json` (side state, outside the digest) while it runs, through `run_checks(on_progress=...)`. The snapshot's `verification.live` carries `in_flight` and `last_failure`; the Phase 7 display name reads LIVE VERIFICATION RUNNING or LIVE VERIFICATION FAILED instead of ready to ship; Fleet reports `running` or `failed` accordingly; the Phase 7 card lists the failing command with its output tail until a later live run passes.
- **#149** Fleet has its own inline SVG favicon (a constellation of mission dots in Fleet's accent); run dashboards keep the state-coloured canvas icon.
- **#150** Fleet lists ongoing runs in the grid and completed or closed runs in a collapsed section below, counted, remembered per browser. **#154** (v0.3.36): a registered project with no run reads `idle` and sits in that section too (COMPLETED, CLOSED AND IDLE); `quiet` is an initialized run with nothing moving.

### v0.3.34 field note: a dashboard whose server is gone, or whose run is closed, looks that way (#151)

When `/api/dashboard` (or `/api/fleet`) stops answering, the page sets `body.is-offline` within one poll interval: every CSS animation and transition stops, the mission dims, the stepper's active node is held, and a banner reads DASHBOARD OFFLINE since HH:MM: the server on this port is not answering; the first successful fetch clears it. A run with `run_closed` renders closed on the server side: the current phase's state is `closed` (never `active`), `status.phase` reads Run closed, the briefing reads Mission closed by WHO: REASON, no role is active. Fleet reports `offline` (counted, card dimmed, badge DASHBOARD OFFLINE) for a moving run whose owner record names a dashboard that does not answer. A completed or closed run whose dashboard goes away (advance 8 releases the owned port) is not an outage: the page keeps its celebration and a calm banner says the dashboard was released at HH:MM and this is the final snapshot (#155, v0.3.38). Fleet's own page polls every 5 seconds and refreshes once on a stream error, so a Fleet server dying under an operator is noticed the same way (v0.3.35). `tests/live_offline_smoke.py` proves both pages through headless Chrome's own DOM against the installed engine (`--tree` serves this checkout instead), and runs first in `live_commands`.

### v0.3.37 field note: Fleet cards carry GitHub and Beakon signals (#152)

Cause: Fleet showed a project's Handsoff state only; what was open on GitHub and which beams the Beakon worker had in flight for it lived on two other screens. Fix:
<!-- handsoff-doc: intentional -->
`bin/handsoff_fleet_signals.py` collects both per registered project on a daemon thread the Fleet server starts (first pass immediately, then every 300 s), swaps the results into one `SignalCache` under a lock and persists it beside the registry; `build_fleet` merges `github` and `beakon` from that cache and never fetches. GitHub is read through `gh api` (or `GITHUB_TOKEN`), three calls per project: open issues, open pull requests, latest release. Beakon is read from the worker's landing folder, so it needs neither the gateway nor a token. A per-project read failure keeps the previous good values with the error text; a first-ever failure shows the error with null counts. The card renders `GITHUB ...` and `BEAKON ...` lines with both cache ages; `tests/live_fleet_signals_smoke.py` proves the installed Fleet serves the strip and a release tag that matches `gh release view`, and runs first in `live_commands`. Daily throughput per project (issues closed per day, time to close) is #153.

### v0.3.39 field note: Fleet has a Metrics tab (#153, #156)

Cause: Fleet answered "what is happening now" and nothing about progress over time; how many tickets closed, how long they took, how many commits and releases landed lived in GitHub's own screens, one repository at a time. Fix:
<!-- handsoff-doc: intentional -->
`bin/handsoff_fleet_signals.py` gains `fetch_issues`, `fetch_commits` and `fetch_releases` plus an `IssueCache` (same lifecycle as the #152 signal cache: one file beside the registry, load once, swap whole under a lock, a failing project keeps its previous entry with the error; the three reads for a project succeed together or not at all), refreshed by a second daemon thread the Fleet server starts after it binds. `/api/metrics` serves the cache with `started_at` and `refreshed_at`, only for roots still registered and only when the cached repository is still the project's origin. `fleet/metrics.html` and `fleet/metrics.js` render the tab under the existing CSP with hand-built SVG (attributes and classes only, no library, no inline styles): local calendar days, DST-safe day ends, nearest-rank p90, commit days before the collector's window shown as unknown rather than zero. `tests/live_fleet_metrics_smoke.py` proves the installed Fleet serves the tab, that its own collector refreshed since it started, and that the project-handsoff issue count equals gh's open plus closed totals; it runs first in `live_commands`.

### v0.3.40 field note: the Metrics collector reads GitHub conditionally (#158)

Cause: every Metrics pass re-paged every issue, commit and release of every project, about twenty counted requests, which made a one-minute interval wasteful and anything faster unsafe. Fix:
<!-- handsoff-doc: intentional -->
`conditional_reader()` in `bin/handsoff_fleet_signals.py` reads through `gh api -i` with `If-None-Match` (a 304 exits 1 in gh but its status line and headers are parsed; the token path uses urllib), and `collect_project` keeps per-entry ETags, `updated_since` and `full_pass_at`: incremental issue reads merge by number, commit reads by sha inside the bounded window, releases stay one read behind their ETag, and a project's three reads still succeed together or its previous entry is kept whole. A pass with nothing changed is three 304s and zero counted requests (measured on the four registered repositories: pass three answered 12 requests, 0 counted, budget unchanged). The default `HANDSOFF_FLEET_ISSUES_INTERVAL` is 60 with a floor of 30; the entry carries `requests_total` and `requests_counted`, `/api/metrics` carries `rate_limit`, and the page shows the budget. `tests/live_fleet_metrics_smoke.py` now watches a second pass land and fails unless it counted nothing or names what changed.

### v0.3.41 field notes: the design click as config, prompt first collection, one Metrics row per repository (#159, #157, #160)

- **#159** Cause: every `init` flagged `requires_design_approval: true`, so Phase 3 always waited for a Pilot click after the independent design critique had already approved, which for a Pilot who trusts the critique is a bottleneck with no information in it. Fix: `[workflow] require_design_approval` (default true) in handsoff.toml; with false, `init` writes the flag false and logs `design_approval_waived`, Phase 3 advances on the approved design review alone, the review stays mandatory, and the key is hashed into the chain of trust only while non-default so existing approvals keep their hash.
- **#157** Cause: a newly registered project waited a full signals (300 s) or metrics (60 s, formerly 900 s) interval before its first collection. Fix: `start_refresh_thread` samples the registry's (mtime_ns, size) before each pass and every `HANDSOFF_FLEET_WAKE_SECONDS` while waiting; a change starts a pass within one wake period of the later of the change landing and the running pass ending.
- **#160** Cause: rows on the Metrics tab were per registered root, so a lane worktree beside its checkout showed the same repository twice and cost double reads. Fix: `IssueCache` collects each repository (owner/repo, case-insensitive) once per pass and serves equal entries to every root sharing it; `/api/metrics` stays one entry per root; the page groups by repository for the filter, the breakdown and the totals.

### v0.3.42 field notes: the engine badge, and one tab per dashboard (#161, #162)

- **#161** Cause: Mission Control named the engine only in a small grey eyebrow (UNKNOWN until the snapshot) and the ENGINE console pane; Fleet named it only inside each card's meta line and never for the Fleet server itself. Fix: one `ENGINE vX.Y.Z` badge in the topbar of both pages, UNKNOWN until data arrives; `/api/fleet` and the event stream carry `engine` (read once from the engine's own manifest); a card whose project engine differs from the Fleet engine is marked `engine-drift` and its meta reads `ENGINE vA (fleet vB)`.
- **#162** Cause: launchers opened the dashboard with macOS `open -a "Google Chrome" URL`, which always adds a tab even when one is already on that URL (measured 1, 2, 3 across three calls), so a run on a reused port showed twice. Fix: `lib.open_dashboard_url` runs one AppleScript that brings an existing tab forward or opens the location, answering `found` or `opened`; `webbrowser.open` is the fallback only when osascript could not start or answered neither word; both servers use it unless `--no-open`.

### v0.3.43 field note: the Metrics tab on the go (#163)

Cause: the Metrics tab needed the Fleet server on the Mac, although everything it shows comes from GitHub. Fix: `web/` holds a Cloudflare Pages site (`metrics.tonecommand.com`): one Pages Function (`web/functions/api/metrics.js`) reads the configured repositories with a token that lives only in a Pages secret and answers Fleet's `/api/metrics` shape, cached at the edge for 60 s; the page is `fleet/metrics.js` and `fleet/styles.css` copied unchanged plus a phone-first stylesheet; `.github/workflows/metrics-site.yml` deploys it on push; Cloudflare Access with the Pilot's email fronts the hostname because two repositories are private. `tests/live_metrics_site_smoke.py` runs first in `live_commands` and proves the deployed host redirects unauthenticated requests to Access.

### v0.3.44 field note: the role chiclets say who fills the station (#164)

Cause: the chiclets named the station only; which agent family held it was three panels away in CREW. Fix: `roleWord(role, snapshot)` in `dashboard/lib/dashboard-logic.js` (the recorded actor's `claude-`/`codex-` prefix, else the configured adapter, else nothing) and `roleTitle` for the hover; `renderRoleChiclets` appends one faint lowercase word; the reviewer chiclet reads the design reviewer in Phases 1 and 2 and the implementation reviewer otherwise. `tests/dashboard/role_words.test.js` runs the real renderer against a stub DOM for all four chiclets.

### v0.3.45 field note: the engine is named once

Cause: after #161 put the ENGINE badge in the topbar, the older grey eyebrow next to the project name still printed the same version and source, so the engine appeared twice on one screen. Fix: the eyebrow carries the project name only; `app.js` writes the version to the badge alone. Cosmetic, shipped on its own because the doubled line was in front of the operator.

### v0.3.46 field note: one logo, and the mission column keeps its name

Cause: on Handsoff's own runs the project artwork is the engine's brand mark, so the topbar showed the same logo twice; and between 1000 and 1240 px the mission column (`minmax(0, 1fr)`) collapsed to nothing, its project logo spilled under the phase pill and `MISSION COMPLETE` was drawn over it. Fix: `project_logo()` skips artwork whose bytes equal `dashboard/logo.png`; `.topbar-mission` clips its overflow and its copy keeps a 120 px floor, while below 1240 px the sync stamp hides and the pilot note input narrows first. Note for maintainers: after editing any runtime file, regenerate the manifest before running the Python suites; the fixtures recognise their engine copy by the manifest hash and otherwise fall back to the pin path with dozens of unrelated failures.

### v0.3.47 field notes: failing first, launch rules, one ticket one run (#165, #167, #166)

Cause: three losses from the 2026-09-19 runs. A green test with no red behind it was accepted as evidence; a reviewer launched at Phase 1 lost a whole Codex verdict to `dispatch_failed`, and a packet with a command line in `tests_executed` lost another to `orchestration_noop`; two sessions ran the same five tickets on two ports until the operator compared browser tabs. Fix: the three behaviours under "Workflow features" above, each behind a `[features]` switch that Mission Control edits. Two things learned while building: the fixtures recognise their engine copy by the runtime manifest hash, so every runtime edit is followed by
<!-- handsoff-doc: intentional -->
`python3 bin/handsoff_manifest.py --version vX.Y.Z` before the Python suites run; and `init` now registers the run itself, so every fixture sets `HANDSOFF_FLEET_REGISTRY` (the base test case does) or the operator's own register fills with temp roots.

### v0.3.48 field notes: the board closed (#168, #169, #170, #171, #172)

Cause: five open tickets, one filed by the Pilot during tranche 1 (a run that had adopted a failed session's verdict showed FAILED on Fleet). Fix: the five sections above, three more `[features]` switches, and one repair found on the way: the runner persists every reviewer result as kind `review`, so a design verdict recovered from a refused packet adopts through the design branch now. Learned: the design gate must not compare the rules set (a Phase 6 hook edit would demand a new design); it records, the review compares.

### v0.3.49 field notes: CI on the board (#181)

Cause: with `main` protected (#178) every lane waits about two minutes on
the pull request's checks (#180), and the run sat at Phase 6 looking idle
while the operator watched a GitHub tab. Fix: the section "CI is a step of
the run" above; `ci-watch`, the CI row, and the Phase 7 gate on a red
check. Found on the way and filed, not fixed here: a missing
`.handsoff-version` pin takes the whole page offline instead of one badge
(#185); the operations console prints 21 rows that cannot act (#183,
#184); the page says "host" where it could say which host (#186).

### v0.3.51 field notes: a lane cannot trip on the floor (#185, #190)

Cause: two runs on 2026-09-21 stopped four times for reasons that were
not the code. A worktree without `.handsoff-version` made every
`/api/dashboard` request raise, so the page read DASHBOARD OFFLINE while
the server was up; `live_offline_smoke` printed one word when the host's
`python3` lacked `websockets`; after the release was installed, `advance 8`
refused "the rules set changed (engine:version)" while `record-review
--reaffirm` refused "nothing to reaffirm"; and a `[checks]` test asserted a
literal engine version. Fix: the snapshot degrades the engine badge to
UNKNOWN with the reason on the audit strip and `init` writes the missing
pin; the smoke names the interpreter and the fix, and `websockets` is the
engine's `live` extra; reaffirm re-binds a current review whose rules set
changed and the ledger names what changed; the test reads the manifest.
The landing order below is the order this release was landed by.

### v0.3.52 field notes: the page says what matters (#186, #183, #184, #181)

Cause: two hosts ran lanes side by side and the page said "host" for both;
the console listed 21 rows that could not act; fixed copy, an empty card
and a grid of token dashes filled the rest; and the new CI row showed one
run's queue time as the estimate, a timer that jumped once a poll, and a
red watch a rerun could not clear. Fix: the three sections above.

### v0.3.53 field notes: the suite means the whole tree (#179, #176, #177)

Cause: CI ran one file of 54; the runner reported a broken pipe where a
child had simply exited (three red shards in five pull requests); five
modules were red on `main` for reasons no lane had touched (a shared fleet
register, the dogfood config inherited by a fixture, a review not
reaffirmed after a config edit, a README wording, the checkout's own run
refusing a fixture's launch); the Phase 8 implementer gate fired on one
run and not another; and the Architect had no way to say a change is not
needed. Fix: the sections "What CI runs", "One implementer rule for every
run" and "The Architect can decline" above. Open on #177: the runner does
not yet dispatch `HANDSOFF_DESIGN_DECLINE` from a managed Architect and the
design reviewer does not yet review a decline. Filed on the way: #193, the
clocks count the hours the Mac was asleep.

### v0.3.55 field notes: the board says who it waits on (#194, #198, #199)

Cause: a run whose host had stopped for eight hours after the Pilot
authorized a review attempt read "telemetry stalled"; the CI row had a bar
and no number; the crew chiclets named the family but not whether the
station was the host or a managed session. Fix: the host-wait line and
the Fleet tag, percent complete on the CI label, `host` or `managed` on
every chiclet.
### v0.3.56 field notes: the clocks know the Mac slept, and a decline is reviewed (#193, #177)

Cause: two runs read "design debate for 7 hours" while the machine was
asleep; #177's decline closed a run without the reviewer's word and a
managed Architect had no way to emit it. Fix: the sections "The clocks
know the Mac slept" and "The Architect can decline" above.

### v0.3.57 field note: the sleep log is read off the request path (#193)

Cause: `pmset -g log` is 33,000 lines and takes three seconds on this
Mac; v0.3.56 read it on the first snapshot, so every new dashboard's
first page waited that long and `live_offline_smoke` found the page not
yet rendered. Fix: the log is read on a background thread into the
per-process cache; a request before the first read lands sees no sleep
(wall clock) and the next one sees the intervals; nothing on the request
path waits for it.

### v0.3.58 field note: a stale manifest names its fix (#204)

Cause: three times in one day a host edited bin/ or prompts/ and launched
a reviewer or ran verify before regenerating the manifest, and read
"override not declared" or "runtime files do not match; reinstall the
engine", both pointing at the wrong place; a knowledge-base rule did not
stop the third. Fix: `stale_manifest_refusal` and the one line above,
from every path that reads the manifest.

### v0.3.59 field note: the badge reads the stale manifest too (#204)

Cause: v0.3.58 claimed every reader refused, and the dashboard's engine
badge did not: the check sat only on the missing-pin path, so with a pin
present (every initialised project) an edited runtime file left the badge
reading healthy; the acceptance named the engine view and no test
exercised it. Fix: the check runs on every read of the runtime identity;
the badge reads ENGINE UNKNOWN with the reason (#185) and Fleet reads
unknown; the test now covers both. Lesson, in the knowledge base: a claim
of "every reader" needs a test per reader, named in the criterion.

### v0.3.60 field note: the playbook ships with the engine (#208)

Cause: the rules for running a lane lived in one project's local,
gitignored knowledge base on one machine; a host on the Studio would start
without them. Fix: `playbook/` in the wheel, `handsoff playbook`, and the
briefing carrying it on every launch.

### v0.3.61 field notes: forgotten roots, and the reviewer blamed for a temp file (#207, #203)

#207. Cause: `init` registers the lane's worktree with Fleet; nothing
removed the entry when the worktree went, so nine ORPHANED cards stood
after one day of lanes. Fix: a root that is gone is ORPHANED for one Fleet
pass (`missing_since` on the entry), then forgotten with one
`fleet_entry_forgotten` line in `~/.handsoff/fleet.log` naming its last
state and whether the run was never closed; a transient absence clears
the mark; the card carries FORGET (`/api/forget`), never Close Run.

#203. Cause, found by running the module under an instrumented digest:
the runtime's own atomic writers leave `..handsoff-live.json.tmp-<pid>-<hex>`
on disk for a few milliseconds, and `..handsoff` escaped the `.handsoff`
exclusion. The reviewer's tree was scanned twice (paths, then digest);
a scan that landed in that window saw a file the other did not, and the
managed reviewer was blamed for a tree it never touched. Every CI sighting
named a sibling test's `PROBE.py` only because the runner's log interleaves
that test's output. Fix: in-flight Handsoff temp files are excluded, both
views come from one scan, the failure record says what was seen (path,
appeared/vanished/changed, mtime, seconds after session start), and the
post-exit reader drain has its own 60 s budget instead of the 5 s join.

### v0.3.62 field note: the Miner leaves the engine (#174, lane 1)

`bin/handsoff_analyzer.py` is gone; the archive scan is `monzta1/miner`
v0.1.0 (its release wheel, `MINER_RELEASE`), installed beside the engine. The Phase 8 trigger and
`analyze-archives` call `miner scan` / `miner propose-rules` and read the
report; without a Miner the trigger prints `HANDSOFF_ANALYSIS_SKIPPED` and
the command refuses with the install hint. The shim stays for one release.
Lesson: an extracted component's callers get a fake on PATH in the engine's
tests and a live smoke against the real install, never a copy of the code.

### v0.3.63 field note: the Miner beside a framework Python (#174)

`verify-live` on v0.3.62 refused the shim on the very machine it was built
on: inside the dedicated venv, macOS's framework build reports the
framework binary as `sys.executable`, so "beside the interpreter" was not
`venv/bin`. The lookup now reads `sys.prefix/bin/miner` first. Lesson: a
live smoke that runs the installed pair is the only proof of an install
path; the unit tests had a fake on PATH and could not see it.

### v0.3.65 field note: one command updates the house (#219)

Four tools, four INSTALL pages, two machines. `handsoff update` reads the
latest release per tool, installs or checks out what is behind, restarts
what runs here and prints one line per tool. The first dry run on the
build machine found the Beakon checkout eight commits ahead of v0.5.0 with
local work; the command leaves such a checkout alone (never a downgrade,
never over local changes), which the reviewer had not asked for and the
dry run did.

### v0.3.70 field notes: the evening set (#215, #216, #217, #218, #227)

Five lanes, five pull requests, one release. `handsoff install-check`
refuses the engine install under a live managed session; the Implementer
reports per-criterion progress and a relaunch reads it; the run-page
snapshot has a schema and eight fixture states, and the run vocabulary is
shared once; the managed-role protocol is one document derived from the
validators; a docs-only change is a merge and nothing more. Lesson, for the
playbook: five parallel lanes all touch `handsoff.toml [checks]` and the
manifest, so each landing rebases with the checks list as main's plus its
own lines, and a lane's Phase 4 advance is the first thing after the design
click, not the last thing before the reviewer.

### v0.3.70 field note: the first docs-only pull request (#227)

This entry is the proof: a Markdown-only pull request that ran the `docs`
job alone and merged without a version bump, a release or a live run.

### v0.4.1 field notes: the operator's budget, the reviewer's eyes, and a gate that mutates its own code (#342 #343 #347 #349, 2026-10-02)

Four defects from one real run of the v0.4.0 engine on another project
(Team Hub v2, October 1-2). The run finished and raised a green pull request;
roughly half its wall-clock went into working around these.

1. **An explicit `[agent_budget]` key was accepted and then ignored (#342).**
   Cause: `ceiling = min(configured, max(floor, calculated))` makes the
   configured number a cap and never an authority, so every value above the
   calculated one produces the same ceiling: `implementer = 500000` and
   `= 80000` were the same run. Managed Codex launches exhausted at 74k to 80k
   tokens, each exhaustion costing a half-written work item, and the operator
   abandoned the managed launcher for raw `codex exec`. Fix: `load_config`
   records WHICH roles the operator set, by key membership rather than by
   comparing the value against the built-in default, since an operator who
   writes the default value has still made a choice. An explicit key is the
   ceiling; the calculated figure is recorded beside it as `calculated_ceiling`
   with a signed `ceiling_divergence` the schema re-derives from the record's
   own numbers, and each Mission Control journey leg names which number was
   applied and by how much the two differ. An unset role behaves exactly as
   before. Measured on this repository: a managed implementer launch went from
   `limit_tokens=57952` to `117952` against a configured 120,000.

2. **The managed Claude reviewer could not read the project it was reviewing
   (#343).** Cause: the reviewer runs from an external scratch directory by
   design, and Claude Code refuses reads outside its working directory, so with
   no `--add-dir` every read came back "Path is outside allowed working
   directories". The first design review was packet-only and still consumed one
   of the two autonomous attempts. The operator's workaround was to add the
   project to `permissions.additionalDirectories` in their own
   `~/.claude/settings.json`, which changes every Claude session on the
   machine. Fix: `claude_argv` passes `--add-dir <absolute project root>` for
   the reviewer role only, with a read-only tool allowlist that contains no
   file-editing tool; both launch builders go through one shared
   `_claude_allowed_tools`, because the two had their own copy of that
   expression and #347 had just shipped with one of its two sites threaded and
   the other not. A read probe runs before the scratch directory, the session
   and the review attempt exist, gated on `role == "reviewer" and adapter ==
   "claude"`: `launch_preflight` takes no role, and architect and supervisor
   share the reviewer's empty allowlist, so a global probe would refuse
   launches #343 never asked to change. The adapter pre-flight probes with the
   new argv shape, since its whole purpose is that a flag the CLI refuses fails
   there rather than at launch.

3. **Reasoning effort was inherited silently from the operator's global
   `~/.codex/config.toml` (#347).** Every implementer ran at `low` and it was
   only discovered by reading the Codex session output. Fix: `[models]
   <role>_reasoning`, validated against the permitted values and refusing a
   misspelled role by name; the flag reaches both launch builders, the session
   records what it ran at, and the dashboard prints it beside the model.

4. **Nothing asked whether a passing test would notice the code breaking
   (#349).** This one was not in the field report. It is why the other three
   took a day each to find. `verify` records that a `[checks]` command exited
   zero; no gate anywhere asks whether that command would still exit zero with
   the implementation gutted. Four measurements of the same hole, in one day
   against v0.4.0: five design-critique rounds found eleven acceptance criteria
   a no-op implementation would have satisfied; a hand mutation of freshly
   written tests found two escapes in nine attempts; 115 of 542 recorded
   sessions were reviewer approvals carrying `tests_executed: no`, which the
   review gate accepted; and stubbing one refusal in `validate_status_schema`
   to `return []` passed the entire 1,727-test suite.

   Fix: `bin/handsoff_mutation.py` and a `mutation-proof` command. The engine
   replaces a named top-level function's body with `return None` inside a
   throwaway copy, runs the criterion's own configured tests before and after,
   and records a `mutation` verification only when they passed before and
   failed after. Each half rules out a different lie: passing before rules out
   a suite that was already red and would "detect" everything; failing after
   rules out a test that executes the code and asserts nothing. A failed proof
   records NOTHING, because a ledger entry reading "this test does not detect
   this" is a durable artifact that looks like evidence and means its opposite.
   The author never performs the mutation and never supplies the verdict, since
   "I mutation-tested it" is exactly the unverified claim this exists to stop,
   and the author cannot choose the command either: it is the criterion's own
   `tests`, joined with `&&` so every test must pass before and any may fail
   after. The new `automated_and_mutation` policy is opt-in per criterion, so
   no existing project changes.

   It found real gaps immediately, including in its own suite: `_source_digest`
   could be neutralised to `return None` and all 34 tests still passed, which
   meant the guard against a mutation escaping into a real checkout was
   covered by nothing. Three tests were added for the guard itself.

   An independent review then found four more, two of them blocking, and all
   four are now reproduced as tests. The proof was forgeable: a function called
   while the module loads, named by no test, raised when neutralised, so the
   command "failed after" and an irrelevant symbol reported `ok: true`. A
   differential import check fixes it, and the symbol is no longer chosen at
   proof time at all -- the criterion declares `mutation_target` and
   `mutation_symbol`, the `automated_and_mutation` policy requires them, and
   they sit inside `criterion_spec_hash`, so the design review sees the claim
   and changing it discards the proof. The review also found that pointing the
   target at a non-Python file raised SyntaxError out of the CLI as a traceback
   rather than a refusal; that the escape guard digested only `bin/` and
   `dashboard/` while nothing restricted the target, so an escape into a target
   elsewhere returned normally with the real file mutated on disk; and that
   `neutralize` and `where_defined` disagreed about what "defined" means, so
   the re-export hint was blind to methods while the mutation would happily
   change one.

   A second independent round then forged two more proofs, and both are now
   reproduced as tests. All five runs shared one copy of the tree, so a target
   that raises on a second import let the first run consume a one-shot resource
   and every later run fail for that reason, which read as detection: an
   arbitrary symbol reported `ok: true`. Every run now gets its own fresh copy,
   which removes the cause rather than detecting it, and also fixes the quieter
   version where a suite that writes into its own tree changed the conditions
   of the run after it. A control run was added for the part a fresh copy
   cannot cure: the command must pass on a SECOND clean copy, because a command
   whose result depends on state outside the tree cannot support a proof at
   all. The round's other finding is a real limit rather than a defect, and it
   is now disclosed in the docstring, in `docs/REFERENCE.md` and in a test that
   keeps it disclosed: a symbol used in a test file's module-level code is
   detected by crash rather than by assertion, and this proof does not tell the
   two apart, because doing so means parsing an arbitrary runner's output.

   Checking the review's question about `criteria-apply` then found a third
   path into the registry with its own copy of the merge:
   `CRITERION_UPDATE_FIELDS` listed the two new fields, so the key check
   accepted them and the apply loop dropped them, and the policy rule was
   judged on the fields being changed rather than on the resulting criterion,
   so a transaction could switch a criterion to `automated_and_mutation` with
   nothing declared. Both are fixed, with the same shape of test the other two
   paths have. One detail is worth stating because it would have been expensive
   to get wrong: `criterion_spec_hash` hashes the field SET, so a criterion
   carrying an explicit null hashes differently from one without the key. Every
   path writes these fields only when they have a value, and a test reads that
   off the source, because the cost of one path writing null is every recorded
   criterion in every project at once.

A third round, a third reviewer, found four more blocking defects. Two were
regressions this work introduced, and two were limits of the fix itself.

The regressions first, because they are the plainer lesson. Removing
`--target`/`--symbol` from `mutation-proof` left `reviewer_launch_evidence_gaps`
printing them, so the Phase 5 pre-check handed a reviewer a command that exits
2 on first use. The test that was supposed to catch this asserted the command
NAME and stopped there, so it kept passing; it now parses every flag out of the
message and asks argparse itself whether each one exists. And the whole proof
ran inside one `with project_lock`: three full suite runs holding a blocking
`fcntl.flock` with no timeout, while `heartbeat` takes the same lock. A slow
suite did not just make its own command slow, it silently blocked the liveness
signal the watchdog reads, which is the exact failure the watchdog exists to
catch. The proof now runs outside the lock, which it can because it writes no
engine state, and the lock is retaken to record; what the lock protected is
re-established then, by re-checking the criterion's spec hash and the source
digest, so a claim or a tree that changed during the proof is refused instead
of receiving the proof. A test takes the lock from inside the proof to prove
it is free, rather than reading the indentation of a `with` block.

The limits are the more interesting half, and they are now disclosed rather
than fixed, because they cannot be fixed.

`reproducible` was two unmutated runs. A reviewer showed that any fixed number
is defeated by a resource with one more strike: a command depending on state
outside the copied tree can be built to survive exactly N clean runs and fail
on the next, aligning a non-mutation failure with the mutated slot. Worse and
far more ordinary, a flaky suite forges a proof by chance with no adversary at
all; the same reviewer measured a 20%-flaky command producing `ok: true` on
trial 10 of 11. The command is now run three times clean and three times
mutated, and must pass every clean run and fail every mutated one, which took
the measured forgery rate to 0 in 14 trials against the same flaky command.
That is a confidence level, roughly 0.4% for that command, and it is reported
as one. Repetition cannot make an unsound command sound.

And the disclosed "detection by crash" limit was real but described too
narrowly. The first wording illustrated a symbol used in a test file's
module-level code, which makes it sound exotic. The reviewer showed
`10 / subject.get_divisor()` with nothing asserted about the divisor, which is
ordinary: `return None` keeps the module importable by design, but a None
flowing into arithmetic, indexing, iteration or attribute access raises at the
point of use, so any code the suite reaches that consumes the value without
asserting on it is enough. The wording now says that, and a test pins the
behaviour as it is rather than as one would like it.

Also from that round: `criteria-apply` refused a legitimate rename of just the
symbol on a criterion that already carried both fields, with "must be set
together", which was misleading because they already were; the single-criterion
CLI backfilled from the stored criterion before validating and the transaction
path did not. A commit failure after a successful proof now says so distinctly,
naming the ledger record, instead of printing a blocked message that reads as
"the proof failed" while a valid record for an expensive proof sits unconsumed.
And `--total-timeout` bounds the whole proof, because a per-run timeout bounds
one run and says nothing about a proof that makes up to eight of them.

Lessons, for the playbook: a green suite is not a tested suite, so neutralise
the function a criterion names and watch its test fail; mutation-prove the new
tests and not only the new code; a test whose evidence lives in gitignored
local state passes on the author's machine and nowhere else; key membership is
a fact no value comparison can recover; and when a rule names the strongest
member of a set, derive it from the set rather than naming it, or the next
member added above it is silently droppable.

### v0.4.2 field notes: six small defects left over from v0.4.1 (#348 #350 #351 #352 #353 #355, 2026-10-03)

Six defects filed while building v0.4.1 and left out of it because they were outside its criteria. Fixed directly, without a managed run, because each fix is small.

1. **`doctor` probed each adapter with the adapter's own default model (#355).** Cause: `adapter_preflight` passed `DEFAULT_AGENT_MODEL`, so no `--model` flag reached the CLI and `~/.codex/config.toml` chose the model. A project naming a usable model was reported unreachable, and one naming an unusable model was reported reachable. Fix: the probe runs once per distinct model the project assigns to each adapter, reports each one under `models`, and an adapter is reachable only when every one of its models is. An adapter no role uses is still probed on its own default.
2. **A stall-warning test sat exactly on a rounding boundary (#353).** Cause: an age of 1200 seconds is exactly 20 minutes, and the CLI and the dashboard sample `now` milliseconds apart, so one reading could floor to 19. Fix: the age is 1230 seconds.
3. **The failover launch path computed a follow-up design budget and discarded it (#352).** Cause: `_effective_token_budget` ran and its result was overwritten before anything read it. The ticket said the primary path still applied the reduction. It does not: that call was removed from the primary path in `d96349b` (2026-09-23), so the reduction and `followup_design_token_budget` have had no effect on either path since. Fix: the dead call is gone and both paths take the ceiling from `plan_role_token_budget` alone. No behaviour changes. Whether to bring the reduction back or delete the setting is still open on #352.
4. **`work-items-sync --item 349` created `ask-349` beside the derived `issue-349` (#351).** Fix: an explicit item spelled as a bare number, `#N` or `issue-N` resolves to `issue-N`, so naming an item derivation already found does nothing.
5. **A reviewer pre-flight failure left its scratch directory behind (#348).** Fix: both launch builders remove the scratch directory when anything after its creation refuses: pre-flight, or the failover path's budget refusal. The existing isolation-refusal test globbed a literal `/tmp`, which `mkdtemp` does not use on macOS, so it could not have seen a leak; it now globs `tempfile.gettempdir()`.
6. **`performance-resume` required `--evidence-hash`, stored it, and never checked it (#350).** Fix: the resume is bound to the paused episode it reopens. The engine computes that episode's hash and records it. The flag is optional, and a supplied value that does not match is refused with the expected hash named. Archived decisions keep the same schema.

Lesson: a test that checks for absence has to look where the thing would actually be, or it passes without testing anything.

### v0.4.3 field note: a budget setting that had done nothing since v0.3.77 (#352, 2026-10-03)

`[agent_budget] followup_design` was parsed, validated and stored, and nothing has read it since `d96349b` (v0.3.77), when the primary launch path moved to `plan_role_token_budget`. Projects kept setting it (fm9-tone sets `100000`), believing it applied. Fix: the setting, `_effective_token_budget` and `FOLLOWUP_DESIGN_TOKEN_BUDGET` are removed, so both launch paths take the ceiling from the planner alone. The key is still accepted so a project that sets it keeps loading on the `0.4.*` line, and `doctor` warns `inert-setting: agent_budget.followup_design has no effect since v0.3.77; delete it from handsoff.toml`. The template no longer suggests it.

### v0.4.4 field notes: a review that ran no tests, and a verdict thrown away (#341 #345, 2026-10-03)

Two defects found by projecting 542 recorded managed sessions (#300) and on a v0.4.0 run.

1. **A reviewer approval that said it ran no tests satisfied the review gate (#341).** Cause: `tests_executed` was collected, validated and stored, and no gate read it; 115 of 542 sessions were such approvals. Fix: `record-review` and `--reaffirm` refuse `tests_executed` `no`, and `unknown` (treated as `no`), while any criterion's verification policy requires `checks`, naming the field, its value and every such criterion, before anything is written. With no such criterion the approval is accepted and `status.review.tests_executed_waiver` records why. `compute_errors` is unchanged, so recorded reviews of past runs still validate. Pilot decision, recorded on the run.
2. **A valid verdict a reviewer printed was discarded when the session then failed (#345).** Cause: a non-zero exit, or a malformed line beside the verdict, ended the session as `orchestration_noop` and the verdict waited for a hand-run `session-result-adopt`. Fix: a managed reviewer's one schema-valid verdict is replayed through the same path `session-result-adopt` uses, marked `adopted_automatically`, so #341 and every other record-time refusal still applies. A #167 rule-refused packet on either stream, a reviewer that modified the tree (now checked before any adoption), or a refused record leaves the failed session and emits `session_result_autoadopt_refused`. Stderr verdicts are parsed before either failure path, and an identical verdict echoed on stderr (#114) counts once. Pilot decision: an automatically adopted verdict satisfies its gate while marked adopted. Architect proposals are out of scope; a refused architect proposal is #361.
3. **A question could be lost (#345).** Cause: `_raise_question_line` checked only the raw line, so a `HANDSOFF_QUESTION` inside a Claude stream-json assistant event was never recorded, and stderr was read for questions only from its last 8192 characters. Fix: complete assistant events are unwrapped, the stderr reader records questions as it reads them, and one launch records a question once.

The implementation review took three attempts; each found a real gap in stream handling (a stdout verdict hiding a stderr packet, a stderr question lost to long diagnostics, stderr verdicts read only on a non-zero exit), each now a test that fails without its fix.

Lesson: when a result can arrive on two streams, decide eligibility only after reading both, on every exit path, not just the failing one you were thinking about.

### v0.4.5 field notes: the same tests run over and over, and a refusal reported as nothing (#363 #361, 2026-10-03)

1. **The #341/#345 lane recorded 65 test runs to prove 7 criteria (#363).** Cause, mostly process: `verify` already ran the union of repeated `--criterion` flags once (#43), but the playbook said to pass one criterion per call, so a shared command ran once per criterion; and two edits after `verify` (a version and docs bump, a one-line test fix) each re-ran every criterion and the mutation proof. Fix: `verify --all` verifies every criterion whose policy includes checks in one call, so each distinct command runs once; `playbook/lanes.md` and `docs/REFERENCE.md` say one `verify --all` per round, after the bump, notes and docs. Also fixed: `[digest] ignore` was outside both hashes, so widening it silently un-staled evidence; it is now bound into `config_hash` and `verification_config_hash` while non-empty, and an absent or empty list keeps every existing hash.
2. **A refused architect proposal was recorded as `orchestration_noop` with the Supervisor's sentence (#361).** Cause: every architect failure path used the one no-op classification, whose reason label names the Supervisor, and the validation error never reached the record. Fix: a new `protocol_refused` category whose reason (and the launcher's stderr) names the refused field, its index and the limit, and an architect that emits nothing gets an architect reason.

Lesson: before filing an engine fix for a cost, read whether the engine already avoids it and the instructions are what spend it.

### v0.4.6 field notes: implementers in parallel, reviews per item, and a pre-flight that hung on stdin (#359 #360 #365, 2026-10-03)

1. **Parallel implementation ran outside the ledger (#359).** Cause: one live session per role, so four disjoint work items on a v0.4.0 run were built by direct `codex exec` sessions the ledger never saw. Fix: `--owns PATH` declares ownership; disjoint owners run at once, each in its own seeded git worktree, and only owned-path changes are applied back; an overlap is refused before anything is written. The first design review rejected end-of-session checking on a shared tree: a session could overwrite another's file, and attribution broke when sessions overlapped. Isolation answered both.
2. **Per-item review ran outside the ledger (#360).** Fix: `record-review --item` on a full-lane item records an advisory `work_item_review_advisory` event that never touches the Phase 5 gate or the review budget.
3. **`verify-live` for v0.4.5 failed on a pre-flight that waited on stdin (#365).** Cause: the login-status call inherited the caller's stdin, and a fake adapter that reads stdin blocked until the 15-second timeout, reported as `auth_failure`. Fix: pre-flight calls that send no prompt run with `stdin=DEVNULL`.

Lesson: when a check can only run after the fact, make the unsafe state impossible instead (a separate worktree) rather than detecting it later.

### v0.4.7 field notes: cohorts that refuse to overclaim, and a contract for a third provider (#301 #304, 2026-10-03)

1. **Routing evidence had no rules for when it counts (#301).** Fix: `bin/handsoff_cohorts.py` answers cohort queries with per-metric known counts, missing rates and a minimum sample, splits by model version after every widening, keeps `UNKNOWN` task class in its own bucket, and is a pure function of its inputs. Measured input from #300 shaped it: 98 percent of records lack full usage, so `mean_total_tokens` is usually null with its shortfall named, which is the honest answer.
2. **Adding a provider meant editing the router (#304).** Fix: `bin/handsoff_adapters.py` is a declared contract that Codex and Claude implement with byte-identical behaviour (a parity matrix holds it), and a third provider registers without touching `route_adaptive_profile`.

The two work items were built by two managed implementers at once, each in its own `--owns` worktree (#359, shipped in v0.4.6): the first lane to use it on itself.
### v0.4.8 field notes: a durable queue before Fleet leaves this machine (#285, 2026-10-03)

Fleet was useful local tooling with no durable queue, no leases and only loopback same-origin authorization, so extending it to a second host would have created ownership and recovery ambiguity. Fix: `bin/handsoff_queue.py` is a durable job queue (a locked JSON store and an append-only, hashed, seq-numbered journal written first) with leases fenced by epoch, expiry checked under the mutation lock, renewal, retries with attempt accounting, cancellation, and exact replay; only a torn tail (no newline and a seq beyond the store's last) is discarded, and any other corruption is refused by name. A job naming another host is refused, because no remote transport exists yet. Fleet serve refuses a non-loopback bind without a token, and with a token every API route answers 401 before any Origin check or mutation. The design took three reviews: the second introduced a tail rule that could truncate committed history, which the third closed.

Lesson: a recovery rule is part of the durability contract; review it as hard as the write path.

### v0.4.9 field notes: measure before activating, and a local model with read-only hands (#302 #305, 2026-10-04)

1. **Evidence-driven routing had no way to be judged before it ran (#302).** Fix: `bin/handsoff_shadow.py` replays cohorts against a fixed temporal holdout and records shadow choices without changing any route; applying a recommendation needs a Mission Control approval bound to the finding and the exact change. The first design review caught two leakage paths (a session ending before the boundary whose verified outcome arrived after it, and a boundary chosen after seeing results) and a forgeable CLI approval; all three are now tests.
2. **Local models could not run a managed role (#305).** Fix: an `ollama` adapter through the #304 contract, with a read-only tool loop over Ollama's native tool calling, for architect, supervisor and reviewer only, and every launch path held to the same capability and risk floors.

Both items were built by two managed implementers at once, each in its own `--owns` worktree.

Lesson: a temporal holdout is only as good as the timestamp it splits on; split on when the outcome became knowable, not when the work ended.

### v0.4.10 field notes: evidence-assisted routing, off until it earns its place (#303 #299, 2026-10-04)

The last step of the evidence line. `bin/handsoff_evidence_routing.py` selects a managed role's model by measured cost to a verified completion, but only after an approved activation bound to the scoring policy version; off by default, and off means routing is exactly what it was. Hard floors come first, unusable evidence is excluded with its reason, the cheapest candidate whose success lower bound meets the threshold wins, and otherwise the static choice stands as `static_fallback`. A Phase-8 rollback monitor switches assistance off when verified outcomes get worse, never because cost rose, and it stays off until a fresh approved activation.

The design took three reviews and every finding was about the contract, not the code: an undefined cost formula, version cohorts that could be pooled, a below-threshold static pick, activation not bound to a policy version, sparse rollback arms, and a `no_decision` record that would have silently disabled assistance. Each is now a test.

Epic #299 closes with this release: projection, cohorts, shadow evaluation, the provider contract, a local provider and opt-in selection, in that order.

Lesson: when the feature can change live behaviour, spend the review budget on the contract; each ambiguity there becomes either a silent regression or an argument later.

### v0.5.0 field notes: a new minor line, and guards that run before the push (#371, 2026-10-04)

v0.5.0 opens the second minor line, the Pilot's call after v0.4.4 to v0.4.10 added concurrent implementers, a durable Fleet queue, a provider contract, a local Ollama provider, shadow evaluation and opt-in evidence routing. Projects pinned `0.4.*` move once with `handsoff upgrade <project> --to 0.5.*`.

The release's own item (#371): about 100 test cases read the engine's source as text (line counts, registries, write inventories, re-export surfaces, call-site counts). Nothing ran them before a push, and across seven runs they failed CI about eight times, each a one-line fix that cost two CI rounds and a reaffirm, roughly 2 to 3 hours of one session. Fix: every such case carries `@guard`, `python3 -m tests.guards` runs exactly those cases in about a minute and records the repository digest it ran against (captured before, compared after, so an edit during the run leaves no record), a scanner fails on any unmarked source-reading case, and `ci-watch` refuses to start without a record for the current tree.

Lesson: a guard nobody runs before the push is a guard that only ever runs in CI; make the cheap check a gate, not a memory.

### v0.5.1 field notes: the wheel on the board, and a report that no longer dies on one closed issue (#373 #374, 2026-10-05)

1. **Nothing showed which engine wheel was installed or whether projects accepted it (#373).** After v0.5.0, beakon (`0.4.*`) and fridgeboard (exact `0.4.0`) were refusing every ledger command, found only by checking pins by hand. Fix: Fleet's snapshot and page carry an engine panel: the installed version and manifest status, the latest release with a behind flag, and each project's pin with an exact upgrade command when the engine does not satisfy it.
2. **One PR-closed issue stopped every report (#374).** On the ToneCommand thin-core lane the PR body said `Closes #209`, GitHub closed it at merge, and `run-close --post` refused to post anything to the other three items. Fix: an item closed by a merged PR whose head is the run's branch (read from `closingIssuesReferences`) counts as closed by the run; any other unpostable item is skipped and named without blocking the rest; an already-posted comment still has its close and tick checked. The landing playbook says `Refs #N`, never `Closes #N`.

Lesson: a batch operation that fails whole on one item turns one surprise into N silent misses; post each item and name what was skipped.

### v0.5.2 field notes: a whole-engine review, seven defects and the code nothing reached (2026-10-05)

A read of every engine module, in four slices, each finding confirmed against the source before it was acted on.

1. **Operation telemetry evicted the wrong session.** `record_operation` kept eight sessions and dropped `next(iter(sessions))`, but the store is saved with sorted keys, so that was the lowest session id, not the oldest. A replacement session could lose the succeeded-operation ids that tell it not to repeat a push or a release. Fix: evict the least recently active session, never the one being written.
2. **Usage parsing raced across two threads.** `UsageWatcher.feed` keeps two-line parse state and is fed by the stdout and the stderr reader threads at once. Fix: one lock around each line.
3. **`watch --interval 0` spun on the project lock, and a negative interval crashed in `time.sleep`.** Fix: at least one second, as `performance-watch` already did.
4. **`init --lane review --adopt` ran `git rev-parse` and `git show` with no timeout.** Fix: 60 seconds, and a timeout refuses the adoption.
5. **`doctor --docs-only --restore-state` silently skipped the restore.** Fix: the combination is refused.
6. **The archive analyzer matched rule ids as substrings**, so `"r1" in "r10"` dropped the review lane's own R10 finding and replaced it with a copy that had no run ids. Fix: whole rule ids.
7. **The turn-bound refusal offered a compact review scope as a remedy**, which no launch path accepts (#290 never wired it). Fix: the refusal names only the budget it can act on.

Also: the live gate's rules-binding check ran twice per validation and now runs once. Removed, each confirmed unreferenced or reached only by its own tests with production using another path: 17 unused imports, 12 dead locals and one unused parameter, six runtime-control aliases nothing called, `DRIFT_CLASSES`, `RELEASE_CLASSES`, `RECOMMENDATIONS`, `is_loopback_host`, `_count_unittest_total`, `append_agent_output` (production writes batches), `fetch_commits`, `fetch_releases` and `_paged` (Fleet builds the same rows through `_commit_row` and `_release_row`), and about 450 blank lines that earlier extractions left behind. Lesson: a static dead-code scan is a candidate list, not a verdict; one flagged tuple was a contract a guard test reads from source, and it is now used by the validator it declares.

### v0.5.3 field notes: installed command documentation and release adapter clarification (#377)

<!-- handsoff-doc: intentional -->
Documentation audit of installed command references (#377): every reference to the drop-in script form (`python3 bin/handsoff_supervisor.py`, `python3 bin/handsoff_agent.py`, `python3 bin/handsoff_fleet.py`) has been converted to the installed command form (`handsoff supervisor`, `handsoff agent`, `handsoff fleet`) in README.md, INSTALL.md, PACKAGE.md, and docs/REFERENCE.md. Maintainer helpers (`python3 bin/handsoff_manifest.py`, `python3 bin/handsoff_cohorts.py`, `python3 bin/handsoff_shadow.py`) retain their original form with intentional markers. Historical records (HANDOFF_GOVERNANCE_28_31.md, docs/FIELD-NOTES.md, docs/governance-design.md) keep their documented commands unchanged with markers. Added documentation for 13 previously undocumented supervisor subcommands (release-reconcile, regression-decide, regression-cancel, design-reject, review-attempt-start, recovery-acknowledge, evidence-routing-activate, run-reopen, monitor-poll, evidence-refresh-plan, performance-status, performance-watch, performance-resume) in docs/REFERENCE.md sections with their siblings. Updated bin/handsoff_release_transaction.py docstrings to clarify that every step reads before acting and adopts exact matches, the provider enforces uniqueness (tag, release per tag, asset name), and operation_key is advisory. Added comprehensive test coverage for documentation currency in tests/test_documentation_current.py.

### v0.5.4 field notes: a release record you can read without resuming it, a shadow route you can read, and archive findings only in their lane (#378 #379 #380, 2026-10-05)

1. **The only way to see a half-finished release was to resume it, and resuming publishes.** Fix: `release-reconcile --inspect` prints the record's presence and each step's state before any audit or gate, and leaves the file byte-identical (same sha256 and mtime).
2. **The shadow comparison for one cohort had no command.** `shadow_route` existed only as a library function. Fix: `shadow-route` prints the routed choice beside the frozen policy's pick for one cohort, with `handsoff.toml` hashed before and after.
3. **The Miner's findings were counted whatever lane their archive was in.** An implementation-review rule (R4) on a design-lane archive stayed in the report and in `archive_scan_completed`. The lane filter existed, but only the tests called it. Fix: each run id is kept only when its archive's lane still applies the rule, and a finding left with no run id is dropped. The Miner's own filing is not changed, and a dry run applies the same filter.

4. **What the live proofs found on real data.** Analysis and shadow evaluation read the flat JSON records `archive_run` writes to `~/Documents/Handsoff-Archive` (727 on this Mac), not the ledger folders under each repo's `.handsoff-archive`. All 727 are full-lane runs, so lane scope drops nothing there today. Only 56 of 640 replay records carry full token usage, below the shadow evaluator's default 0.5 coverage floor. Cohorts are keyed by worktree folder (`fm9-tone-lane-104`), which splits one project's history; that is the naming problem #389 tracks for Fleet cards.

5. **Two v0.5.3 slips, fixed on landing.** The documentation test handed the audit a fixed engine version (0.5.3), so the next version bump made its own README links look obsolete; it now reads the checkout's version. The v0.5.3 field-note entry had been inserted mid-file and is now in release order.

Lesson: a helper with tests is not a wired guard. Check that production calls it before counting it as the fix.

### v0.5.5 field notes: the analyzer ships in the wheel (#380, 2026-10-05)

1. **v0.5.4's installed `analyze-archives` crashed with `ModuleNotFoundError: handsoff_analyzer`.** #380 made `analyze-archives` call `handsoff_analyzer.applicable_findings`, the first production import of that module, but the module was in neither `pyproject.toml`'s `py-modules` nor the manifest's `RUNTIME_FILES`. The packaging test compared those two lists with each other, so a module missing from both passed. The drop-in checkout and every unit test ran from `bin/` and never saw it; the installed engine's live Miner check did. Fix: the module ships, and the packaging test now also compares the manifest with every `.py` file in `bin/`.

Lesson: a closed set is closed against the files on disk, not against another hand-kept list. Connecting dead code can expose packaging that only worked because nothing imported it.

### v0.5.6 field notes: the built-but-uncalled features connected, and Fleet made accurate and fast (#381-#389 #392 #393, 2026-10-05)

One run, eleven issues, three concurrent implementer lanes with disjoint owned paths.

1. **Code that was built, tested and never called (#381-#388).** The v0.5.2 review found the close transaction's per-item state machine, the config restore, the runtime-control pause rule and rediscovery, the performance history rebuild, the durable queue, the adaptive check records and the compact review scope all unreachable from any command. Each is now on a production path: per-item reconciliation with a write-once archive and an owner-checked dashboard release, `config-override` restored at close, the pause decided by operation class with late results quarantined and adoptable once, `fleet rediscover`, a clock rebuilt from its own timeline journal, `fleet queue` with `/api/queue`, review events carrying the check plan, and `--compact-scope` with the project path denied. `sign_legacy_record` was deleted instead: no released version wrote what it signs.
2. **Fleet misnamed runs and called a working host idle (#389).** Cards were named by folder, and `quiet` meant only "no managed session". Fix: the configured project name first, and `host_working` with an age while the ledger is being written. The live proof also found the summary strip had no tile for it, though the API counted it; the shared state vocabulary now lists it.
3. **`/api/fleet` rebuilt everything on every request (#393).** Fix: one config load per project per build, views cached by artifact signature (repository HEAD and index included), concurrent owner probes, single-flight builds.
4. **`ci-watch` demanded the engine's own guards module from every project (#392).** Fix: the record is required only where `tests/guards.py` exists.

Lessons: a managed Architect whose sandbox cannot read the issues designs from the brief alone, and here it rotated three issue tags; paste the issue bodies into its task. A lane plan must give every file the work needs an owner (`bin/handsoff_schema.py` had none, and the session-field writes it gates waited for a later launch). Anything written from refresh_performance_state runs before every command and on every snapshot: the first design put the timeline in the hash-chained event ledger, so read-only commands wrote it and CI failed about twenty tamper, doctor and Fleet tests. The timeline moved to its own journal.

### v0.5.7 field notes: implementation reviews get a bounded packet, and a review that cannot fit is refused (#397, 2026-10-05)

1. **The #381-#393 run's first implementation review ended with no verdict.** It spent 199K of its 200K tokens re-deriving every criterion's results from a 30-file diff and verbose test output, though the ledger already held them. Fix: a Phase-5 reviewer launch now carries `# Implementation review packet`: each criterion's latest result per test command (or `missing`), manual evidence, and the changed files' hunk ranges, at most 64 KiB, trimmed in a fixed recorded order that never drops a criterion or a command result. `handsoff supervisor implementation-review-packet` prints it, read-only.
2. **Nothing stopped a review too large for its budget from starting.** Fix: before any session or reservation, the launch estimates 1.5 times the diff's tokens plus the packet's and refuses above `[agent_budget] reviewer`, naming the remedies. A compact-scope launch is estimated from its slices.

Lesson: when the engine already holds the evidence, hand it over; a reviewer told to judge from it reads only the hunks it doubts.

### v0.5.8 field notes: the ticket lock is per repository, and a flaky wait waits on state (#399, #400, 2026-10-06)

1. **`init --item "#4"` in monzta1/sentinel was refused because a run in monzta1/sentinel-sandbox held `#4` (#400).** Cause: the lock compared bare numbers across every registered run. Fix: `init` captures the run's origin `owner/name` at claim time and records it on the run's own acceptance registry as `repository` (never on the shared register, which an older engine would refuse with an unknown key), so an owner whose origin changes later keeps its tickets; only a run initialised before #400 is read from its root's origin. Two runs whose repositories are both known and differ never share a ticket; an unknown repository on either side still compares by number, so no protection is lost. The refusal names the owner's repository.
2. **`--item monzta1/sentinel#4` minted `ask-monzta1-sentinel-4` beside `issue-4`, and the title minted one ask per `;` clause (#400).** Phase 8 refused until each was removed by hand. Fix: a qualified ref or issue URL of the run's own repository is `issue-N`, another repository's is refused at init (in the title as well as in `--item`), and a run initialised with `--item` records `work_items_explicit: true` so derivation never reads the title at all: no title asks and no title issue refs.
3. **`test_disjoint_concurrent_writers_both_apply_in_either_order` failed once on CI with "2 sessions never reached running" (#399).** Cause: a fixed 30 s wall-clock poll, and a fake gate that auto-completed after 30 s and reported success. Fix: each launch signals when its fake process starts, the test waits on that signal while the launch thread lives and fails at once with the launch's own error if it ends first; gates hold until released and fail the launch on the 300 s safety ceiling; a simulated slow runner covers the case that failed.

Lesson: an identifier is unique only inside the namespace that issued it; a lock that keys on it must carry the namespace, or say plainly that it compares without one. And a test that waits on a clock is measuring the runner, not the code: wait on the event the code emits.

### v0.5.9 field notes: the release wheel is rebuilt from the tag, not read from dist/ (#403, 2026-10-06)

1. **The release smoke compared the published wheel with whatever sat in the checkout's `dist/` (#403).** A wheel built from a dirty tree, a stale `build/` folder or another checkout passed or failed by accident, and `pip wheel .` stamped file times into the zip, so two honest builds of one commit differed. Fix: `scripts/build_release_wheel.py COMMIT [--out DIR]` exports exactly the commit's tracked tree with `git archive` into a temporary directory and runs `pip wheel --no-deps` there with `SOURCE_DATE_EPOCH` set to the commit's committer time; the release procedure builds with it, and `tests/live_release_smoke.py` rebuilds the tag the same way and requires the published asset to match through `compare_wheels`, which names both sha256 values and whether the unpacked contents match.

Lesson: a release check that compares with a local artifact proves only that two copies of the same mistake agree. Rebuild from the tagged commit and compare bytes.

### v0.5.10 field notes: evidence and gates, the manual steps removed (#405 #406 #418 #419 #422, 2026-10-07)

1. **A reviewer's schema-valid verdict was stored on the session and never applied when the session exited badly (#405).** Seen four times on a v0.5.8 full-lane run and again on monzta1/sentinel-sandbox#4 (`dispatch_failed`: 'implementation Reviewer result requires Phase 5', exit 0). Cause: only a non-zero exit or a protocol error went through the v0.4.4 adoption; a clean exit whose dispatch raised, and a timeout, ended the session with the verdict unread. Fix: whatever the exit (0, non-zero, a timeout as 124) one valid verdict is dispatched while the session is live, and when the dispatch raises the launcher runs the `session-result-adopt` replay before ending it; either success ends it `completed`. Only a double failure ends it `dispatch_failed` with `result_available`, both reasons named, no replacement, and the pause naming the exact adoption command. An approval delivered at Phase 7 reaffirms the recorded review when only engine or mechanics entries drifted.
2. **A run-mechanics edit revoked the review and the deployment approval (#406).** Adding `[checks].live_commands` after approval, or installing a new engine (`engine:version` alone, sentinel-sandbox#4), went stale and cost a fresh reviewer and a `review-cap-override` for code that had not moved. Fix: the rules set drops `engine:version` and hashes `handsoff.toml` over its policy subset (`handsoff.toml#policy`), without one documented constant of mechanics (`RULES_MECHANICS`: `[models]`, `[agent_budget]`, `[adapters]`, `[fallback_policy]`, `[dashboard]`, `[performance]`, `[routing_profiles]`, `[routing_budgets]`, `[checks].live_commands`, `[checks].timeout_seconds`). A decision recorded before carries the whole-file hash and fails closed except on an `engine:version`-only difference. `session-result-adopt` re-binds an adopted approval when only mechanics or the engine drifted, and refuses a policy change by name. `init` and `design-approve` warn when live verification is required and `live_commands` is empty.
3. **`init --item` still seeded the placeholder `REQ-001` (#418).** It had to be found and replaced on every run. Fix: with `--item` the registry starts empty, and `design-propose` and `advance 2` refuse it naming `criterion-add` and `criteria-apply`; without `--item` the placeholder stays.
4. **The reviewer launch was refused for `record-symptom-resolved` with no hint which of eight run ids to give it (#419).** Fix: a `verify` that leaves every `primary_fix` criterion passing on automated evidence records the symptom itself, bound to its run id and actor, and records it again after a rollback clears it; a manual primary still needs the command, and then `next_action` and the launch refusal name the exact run id.
5. **An open human pause silently blocked deployment approval in Mission Control (#422, monzta1/ToneCommand#216).** The Pilot's clicks were refused with 'not awaiting deployment authorization', and `status` had no `human_pause`, so Sentinel's resume concluded none was open. Fix: `status` carries `human_pause` (`by`, `at`, `note`); the snapshot shows `approval_blocked_by_pause` naming the actor, the time and `human-pause-end`, with no authorize control, and the endpoint answers 409 with the same text.

Lesson: a recorded decision should be bound to what it judged and nothing else. Binding the review to the engine version and the run's budgets made every routine change look like a policy change, and the cost landed on the Pilot as a manual step that proved nothing.
