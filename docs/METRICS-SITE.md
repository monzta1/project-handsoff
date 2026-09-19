# metrics.tonecommand.com: the Metrics tab on the go

The Fleet Metrics tab (issues, commits and releases per repository over
time) served from Cloudflare Pages, fed straight from GitHub by one Pages
Function, behind Cloudflare Access. Nothing on the Mac has to be running.
The Missions tab (Handsoff run state, Beakon beams) lives on the Mac and is
not part of this; see `REMOTE-ACCESS.md` for that.

## What deploys

`.github/workflows/metrics-site.yml` runs on every push to `main` that
touches `web/`, `fleet/metrics.js`, `fleet/styles.css` or the workflow. It
runs `web/build.py` (copies the page, the phone stylesheet and the two Fleet
files into `web/dist`), creates the Pages project `handsoff-metrics` if it
does not exist, puts the GitHub token into the project's secrets, and
deploys `web/dist` with `web/functions` beside it.

## One-time setup

### Repository secrets (Settings, Secrets and variables, Actions)

| secret | value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | an API token with "Cloudflare Pages: Edit" (the one tonecommand.com uses works) |
| `CLOUDFLARE_ACCOUNT_ID` | the account that owns tonecommand.com |
| `METRICS_GITHUB_TOKEN` | a read-only GitHub token for the repositories the site shows (fine-grained: Issues read, Contents read, Metadata read on those repositories; private repositories need it, public ones work without) |

### Repository variable (Settings, Secrets and variables, Actions, Variables)

`METRICS_REPOS`: comma-separated `owner/repo` list, for example
`monzta1/project-handsoff,monzta1/ToneCommand,monzta1/beakon,monzta1/ircommand`.
The workflow copies it into the Pages project on every deploy (as a Pages
secret, which the Function reads like any variable). Optional
`METRICS_COMMITS_DAYS` (default 180) can be set the same way if wanted.

### Custom domain (Workers & Pages, handsoff-metrics, Custom domains)

Add `metrics.tonecommand.com`; Cloudflare creates the DNS record on the
zone it already hosts.

### Access (Zero Trust, Access, Applications, Add, Self-hosted)

Application domain `metrics.tonecommand.com`, one policy `Allow` with
`Emails` = the Pilot's address, one-time PIN as the login method. Without
this the page is public, and two of the repositories are private.

## Verify

- `python3 tests/live_metrics_site_smoke.py`: `/` and `/api/metrics` answer
  a redirect to `cloudflareaccess.com` when not logged in.
- On the phone: open the URL, enter the emailed code, and the KPI row, the
  charts, the releases, the per-repository breakdown and the table render.
- GitHub budget: the Function reads about 20 requests per cache miss (one
  miss a minute at most), well under the 5,000 an hour of the token.

## Where the code is

`web/public/index.html` and `web/public/phone.css` (the page and its
phone layout), `web/functions/api/metrics.js` (the Function),
`web/build.py`, and the reused `fleet/metrics.js` and `fleet/styles.css`.
