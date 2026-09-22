# Handsoff field notes

One entry per release that taught something, newest last: the cause, the fix, and the lesson that went into the playbook. Moved from the README on 2026-09-21 (#224).

### v0.3.22 field notes: managed Claude launches, installed-engine permissions, reaffirmed reviews (field notes, 2026-09-18)

Eight defects observed on a real thin project during one run from `init` to Phase 8 with the v0.3.22 engine, each with its cause and fix:

1. **Every managed Claude role failed at launch.** Cause: the Claude CLI refuses `--output-format stream-json` under `--print` without `--verbose`, and the adapter pre-flight used a hand-written text-mode argv that never hit the flag. Fix: one shared `lib.claude_argv` (with `--verbose` next to `--output-format stream-json`) builds the argv for both launch paths, and pre-flight now probes each adapter with the launcher's own argv shape (`lib.claude_argv` / `lib.codex_argv`, read-only role, no model override).
2. **Generated implementer permissions used the drop-in script form on an installed-engine project**, which has no `bin/`, so `record-symptom-resolved` was refused. Fix: `lib.implementer_allowed_tools` names the forms that exist for the project (`python3 bin/handsoff_supervisor.py ...` for a drop-in root; `handsoff supervisor ...` plus the resolved absolute console path otherwise), and the implementer's role input lists the exact permitted forms.
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

Cause: Fleet showed a project's Handsoff state only; what was open on GitHub and which beams the Beakon worker had in flight for it lived on two other screens. Fix: `bin/handsoff_fleet_signals.py` collects both per registered project on a daemon thread the Fleet server starts (first pass immediately, then every 300 s), swaps the results into one `SignalCache` under a lock and persists it beside the registry; `build_fleet` merges `github` and `beakon` from that cache and never fetches. GitHub is read through `gh api` (or `GITHUB_TOKEN`), three calls per project: open issues, open pull requests, latest release. Beakon is read from the worker's landing folder, so it needs neither the gateway nor a token. A per-project read failure keeps the previous good values with the error text; a first-ever failure shows the error with null counts. The card renders `GITHUB ...` and `BEAKON ...` lines with both cache ages; `tests/live_fleet_signals_smoke.py` proves the installed Fleet serves the strip and a release tag that matches `gh release view`, and runs first in `live_commands`. Daily throughput per project (issues closed per day, time to close) is #153.

### v0.3.39 field note: Fleet has a Metrics tab (#153, #156)

Cause: Fleet answered "what is happening now" and nothing about progress over time; how many tickets closed, how long they took, how many commits and releases landed lived in GitHub's own screens, one repository at a time. Fix: `bin/handsoff_fleet_signals.py` gains `fetch_issues`, `fetch_commits` and `fetch_releases` plus an `IssueCache` (same lifecycle as the #152 signal cache: one file beside the registry, load once, swap whole under a lock, a failing project keeps its previous entry with the error; the three reads for a project succeed together or not at all), refreshed by a second daemon thread the Fleet server starts after it binds. `/api/metrics` serves the cache with `started_at` and `refreshed_at`, only for roots still registered and only when the cached repository is still the project's origin. `fleet/metrics.html` and `fleet/metrics.js` render the tab under the existing CSP with hand-built SVG (attributes and classes only, no library, no inline styles): local calendar days, DST-safe day ends, nearest-rank p90, commit days before the collector's window shown as unknown rather than zero. `tests/live_fleet_metrics_smoke.py` proves the installed Fleet serves the tab, that its own collector refreshed since it started, and that the project-handsoff issue count equals gh's open plus closed totals; it runs first in `live_commands`.

### v0.3.40 field note: the Metrics collector reads GitHub conditionally (#158)

Cause: every Metrics pass re-paged every issue, commit and release of every project, about twenty counted requests, which made a one-minute interval wasteful and anything faster unsafe. Fix: `conditional_reader()` in `bin/handsoff_fleet_signals.py` reads through `gh api -i` with `If-None-Match` (a 304 exits 1 in gh but its status line and headers are parsed; the token path uses urllib), and `collect_project` keeps per-entry ETags, `updated_since` and `full_pass_at`: incremental issue reads merge by number, commit reads by sha inside the bounded window, releases stay one read behind their ETag, and a project's three reads still succeed together or its previous entry is kept whole. A pass with nothing changed is three 304s and zero counted requests (measured on the four registered repositories: pass three answered 12 requests, 0 counted, budget unchanged). The default `HANDSOFF_FLEET_ISSUES_INTERVAL` is 60 with a floor of 30; the entry carries `requests_total` and `requests_counted`, `/api/metrics` carries `rate_limit`, and the page shows the budget. `tests/live_fleet_metrics_smoke.py` now watches a second pass land and fails unless it counted nothing or names what changed.

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

Cause: three losses from the 2026-09-19 runs. A green test with no red behind it was accepted as evidence; a reviewer launched at Phase 1 lost a whole Codex verdict to `dispatch_failed`, and a packet with a command line in `tests_executed` lost another to `orchestration_noop`; two sessions ran the same five tickets on two ports until the operator compared browser tabs. Fix: the three behaviours under "Workflow features" above, each behind a `[features]` switch that Mission Control edits. Two things learned while building: the fixtures recognise their engine copy by the runtime manifest hash, so every runtime edit is followed by `python3 bin/handsoff_manifest.py --version vX.Y.Z` before the Python suites run; and `init` now registers the run itself, so every fixture sets `HANDSOFF_FLEET_REGISTRY` (the base test case does) or the operator's own register fills with temp roots.

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
