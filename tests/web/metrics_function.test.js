const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

// #163: the Pages Function that feeds metrics.tonecommand.com. GitHub is a
// stubbed fetch; the edge cache is a stubbed caches.default.
const modulePath = path.resolve(__dirname, "../../web/functions/api/metrics.js");
const load = () => import(modulePath);

const NOW = new Date("2026-09-19T14:00:00Z");
const issue = (number, extra = {}) => ({ number, title: `Issue ${number}`, html_url: `https://x/${number}`, created_at: "2026-09-01T00:00:00Z",
  closed_at: null, updated_at: "2026-09-02T00:00:00Z", body: "ignored", labels: [], ...extra });
const commit = (sha, date) => ({ sha, commit: { message: "First line\n\nbody", committer: { date } } });
const release = (tag, published, draft = false) => ({ tag_name: tag, name: tag, html_url: `https://x/r/${tag}`, published_at: published, draft });

function stubFetch(routes, seen) {
  return async (url, init) => {
    seen.push({ url, auth: init.headers.Authorization });
    const u = new URL(url);
    const page = Number(u.searchParams.get("page"));
    const key = `${u.pathname}${u.searchParams.get("state") ? "?state=all" : ""}${u.searchParams.get("since") ? "?since" : ""}`;
    const route = routes[key];
    if (!route) return { status: 404, headers: new Map(), json: async () => ({}) };
    if (route.status) return { status: route.status, headers: new Map(), json: async () => ({}) };
    const start = (page - 1) * 100;
    const body = route.items.slice(start, start + 100);
    const headers = new Map([["x-ratelimit-remaining", "4321"], ["x-ratelimit-limit", "5000"], ["x-ratelimit-reset", "1790000000"]]);
    return { status: 200, headers, json: async () => body };
  };
}

test("the Function answers Fleet's shape from paged GitHub reads, skipping PRs and drafts", async () => {
  const { buildMetrics } = await load();
  const seen = [];
  const issues = Array.from({ length: 120 }, (_, i) => issue(i + 1));
  issues[5].pull_request = { url: "pr" };
  const fetchImpl = stubFetch({
    "/repos/o/alpha/issues?state=all": { items: issues },
    "/repos/o/alpha/commits?since": { items: [commit("a".repeat(40), "2026-09-10T00:00:00Z")] },
    "/repos/o/alpha/releases": { items: [release("v1", "2026-09-05T00:00:00Z"), release("v2", "2026-09-06T00:00:00Z", true)] },
  }, seen);
  const result = await buildMetrics({ GITHUB_TOKEN: "tok", METRICS_REPOS: "o/alpha" }, fetchImpl, NOW);
  assert.equal(result.status, 200);
  const body = result.body;
  assert.deepEqual(Object.keys(body), ["generated_at", "started_at", "refreshed_at", "rate_limit", "projects"]);
  assert.equal(body.generated_at, NOW.toISOString());
  assert.equal(body.rate_limit.remaining, 4321);
  const p = body.projects[0];
  assert.equal(p.root, null);
  assert.equal(p.name, "alpha");
  assert.equal(p.repo, "o/alpha");
  assert.equal(p.issues.length, 119);
  assert.deepEqual(Object.keys(p.issues[0]), ["number", "title", "html_url", "created_at", "closed_at", "updated_at"]);
  assert.deepEqual(p.commits, [{ sha: "a".repeat(40), date: "2026-09-10T00:00:00Z", message: "First line" }]);
  assert.deepEqual(p.releases.map((r) => r.tag_name), ["v1"]);
  assert.equal(p.commits_since, "2026-03-23T14:00:00Z");
  assert.equal(p.requests_total, 4);   // issues 2 pages, commits 1, releases 1
  // The token is an outbound header only.
  assert.ok(seen.every((call) => call.auth === "Bearer tok"));
  assert.doesNotMatch(JSON.stringify(body), /tok/);
  assert.match(seen[1].url, /page=2$/);
  assert.match(seen[2].url, /commits\?since=2026-03-23T14:00:00Z&per_page=100&page=1$/);
});

test("a repository that fails reads as an error row, the others still answer, and no token is a 500", async () => {
  const { buildMetrics } = await load();
  const fetchImpl = stubFetch({
    "/repos/o/good/issues?state=all": { items: [issue(1)] },
    "/repos/o/good/commits?since": { items: [] },
    "/repos/o/good/releases": { items: [] },
    "/repos/o/bad/issues?state=all": { status: 502 },
  }, []);
  const result = await buildMetrics({ GITHUB_TOKEN: "tok", METRICS_REPOS: "o/good, o/bad, o/missing" }, fetchImpl, NOW);
  assert.equal(result.status, 200);
  const [good, bad, missing] = result.body.projects;
  assert.equal(good.error, null);
  assert.equal(bad.error, "GitHub HTTP 502");
  assert.deepEqual([bad.issues, bad.commits, bad.releases], [[], [], []]);
  assert.equal(missing.error, "GitHub answered 404 for repos/o/missing/issues");
  const none = await buildMetrics({ METRICS_REPOS: "o/good" }, fetchImpl, NOW);
  assert.deepEqual(none, { status: 500, body: { error: "GitHub is not configured" } });
});

test("onRequestGet caches a good answer for 60 s under the request URL and serves the hit", async () => {
  const { onRequestGet } = await load();
  const store = new Map();
  globalThis.caches = { default: { match: async (req) => store.get(req.url) || null, put: async (req, res) => { store.set(req.url, res); } } };
  globalThis.fetch = stubFetch({ "/repos/o/a/issues?state=all": { items: [issue(1)] }, "/repos/o/a/commits?since": { items: [] }, "/repos/o/a/releases": { items: [] } }, []);
  const waits = [];
  const context = { request: new Request("https://metrics.example/api/metrics"), env: { GITHUB_TOKEN: "tok", METRICS_REPOS: "o/a" }, waitUntil: (p) => waits.push(p) };
  const first = await onRequestGet(context);
  await Promise.all(waits);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("Cache-Control"), "public, max-age=60");
  assert.equal(first.headers.get("Content-Type"), "application/json; charset=utf-8");
  assert.ok(store.has("https://metrics.example/api/metrics"));
  globalThis.fetch = async () => { throw new Error("must not reach GitHub on a cache hit"); };
  const second = await onRequestGet(context);
  assert.equal((await second.json()).projects[0].repo, "o/a");
  // A 500 (no token) is never cached.
  store.clear();
  const bad = await onRequestGet({ ...context, env: { METRICS_REPOS: "o/a" } });
  assert.equal(bad.status, 500);
  assert.equal(store.size, 0);
  delete globalThis.caches;
});

// --- which repositories the site shows -------------------------------------
//
// METRICS_REPOS was set once in September and four repositories later the site
// was showing a third of the work, with nothing to say it had gone stale. The
// list is now the token's own repositories, filtered by a window, so a new
// repository appears by itself and a dead one drops off.

const repoRow = (name, pushed) => ({ full_name: `o/${name}`, pushed_at: pushed });

const emptyRepo = (name) => ({
  [`/repos/o/${name}/issues?state=all`]: { items: [] },
  [`/repos/o/${name}/commits?since`]: { items: [] },
  [`/repos/o/${name}/releases`]: { items: [] },
});

test("with no METRICS_REPOS it lists the token's own repositories", async () => {
  const { buildMetrics } = await load();
  const seen = [];
  const fetchImpl = stubFetch({
    "/user/repos": { items: [repoRow("zebra", "2026-09-01T00:00:00Z"),
                             repoRow("alpha", "2026-08-01T00:00:00Z")] },
    ...emptyRepo("zebra"), ...emptyRepo("alpha"),
  }, seen);
  const body = (await buildMetrics({ GITHUB_TOKEN: "tok" }, fetchImpl, NOW)).body;
  assert.deepEqual(body.projects.map((p) => p.repo), ["o/alpha", "o/zebra"],
    "it did not list the account's repositories, or did not sort them");
  // Owned, not every repository this account can see: a collaborator invite
  // must not silently put somebody else's project on the board.
  assert.ok(seen.some((r) => r.url.includes("affiliation=owner")),
    "it asked for every repository the token can see, not the owned ones");
});

test("a repository nobody has pushed to for two years is left out", async () => {
  const { buildMetrics } = await load();
  const fetchImpl = stubFetch({
    "/user/repos": { items: [
      repoRow("live", "2026-09-01T00:00:00Z"),
      repoRow("quiet", "2025-06-01T00:00:00Z"),      // inside the window
      repoRow("dead", "2020-06-07T00:00:00Z"),       // the 2018-2020 ones
      repoRow("ancient", "2018-03-17T00:00:00Z"),
    ] },
    ...emptyRepo("live"), ...emptyRepo("quiet"),
    ...emptyRepo("dead"), ...emptyRepo("ancient"),
  }, []);
  const body = (await buildMetrics({ GITHUB_TOKEN: "tok" }, fetchImpl, NOW)).body;
  assert.deepEqual(body.projects.map((p) => p.repo), ["o/live", "o/quiet"],
    "a board carrying repositories last touched in 2018 is one people scroll past");
});

test("the window is a setting, not a number buried in the code", async () => {
  const { buildMetrics } = await load();
  const fetchImpl = stubFetch({
    "/user/repos": { items: [repoRow("live", "2026-09-01T00:00:00Z"),
                             repoRow("quiet", "2025-06-01T00:00:00Z")] },
    ...emptyRepo("live"), ...emptyRepo("quiet"),
  }, []);
  const body = (await buildMetrics({ GITHUB_TOKEN: "tok", METRICS_ACTIVE_MONTHS: "6" },
                                   fetchImpl, NOW)).body;
  assert.deepEqual(body.projects.map((p) => p.repo), ["o/live"]);
});

test("a repository with no commits at all is not counted as quiet", async () => {
  const { buildMetrics } = await load();
  const fetchImpl = stubFetch({
    "/user/repos": { items: [repoRow("live", "2026-09-01T00:00:00Z"),
                             { full_name: "o/empty", pushed_at: null }] },
    ...emptyRepo("live"),
  }, []);
  const body = (await buildMetrics({ GITHUB_TOKEN: "tok" }, fetchImpl, NOW)).body;
  assert.deepEqual(body.projects.map((p) => p.repo), ["o/live"]);
});

test("METRICS_REPOS still wins when somebody wants an explicit subset", async () => {
  const { buildMetrics } = await load();
  const seen = [];
  const fetchImpl = stubFetch({
    "/user/repos": { items: [repoRow("everything", "2026-09-01T00:00:00Z")] },
    ...emptyRepo("named"),
  }, seen);
  const body = (await buildMetrics({ GITHUB_TOKEN: "tok", METRICS_REPOS: "o/named" },
                                   fetchImpl, NOW)).body;
  assert.deepEqual(body.projects.map((p) => p.repo), ["o/named"]);
  assert.ok(!seen.some((r) => r.url.includes("/user/repos")),
    "it listed the account even though it was told exactly what to show");
});

test("GitHub being unreachable while listing says so rather than showing nothing", async () => {
  const { buildMetrics } = await load();
  const fetchImpl = stubFetch({ "/user/repos": { status: 503 } }, []);
  const result = await buildMetrics({ GITHUB_TOKEN: "tok" }, fetchImpl, NOW);
  assert.equal(result.status, 200);
  assert.deepEqual(result.body.projects, []);
  assert.match(result.body.error, /GitHub HTTP 503/,
    "an empty board with no explanation reads as 'you have no repositories'");
});
