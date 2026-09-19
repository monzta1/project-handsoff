// #163: the Metrics tab's data, served from Cloudflare Pages without the
// Fleet server. One Function reads the configured repositories from GitHub
// with a token that lives only in a Pages secret and answers the same shape
// as Fleet's /api/metrics, so fleet/metrics.js renders it unchanged.
//
// Runs on the Workers runtime (ES module, fetch, caches.default). Tested in
// node by stubbing globalThis.fetch and caches.

const PAGE_SIZE = 100;
const ISSUE_FIELDS = ["number", "title", "html_url", "created_at", "closed_at", "updated_at"];
const GITHUB_API = "https://api.github.com";
const CACHE_SECONDS = 60;
const DEFAULT_COMMITS_DAYS = 180;

class GitHubUnavailable extends Error {}

function commitsSince(now, days) {
  const since = new Date(now.getTime() - days * 86400000);
  since.setUTCMilliseconds(0);
  return since.toISOString().replace(".000Z", "Z");
}

async function readPaged(fetchImpl, token, path, tally) {
  const items = [];
  let page = 1;
  for (;;) {
    const joiner = path.includes("?") ? "&" : "?";
    const response = await fetchImpl(`${GITHUB_API}/${path}${joiner}per_page=${PAGE_SIZE}&page=${page}`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "handsoff-metrics-site" },
    });
    tally.total += 1;
    const remaining = response.headers.get("x-ratelimit-remaining");
    if (remaining !== null) {
      const reset = Number(response.headers.get("x-ratelimit-reset") || 0);
      tally.rate = { remaining: Number(remaining), limit: Number(response.headers.get("x-ratelimit-limit") || 0),
        reset_at: reset ? new Date(reset * 1000).toISOString() : null };
    }
    if (response.status === 404) throw new GitHubUnavailable(`GitHub answered 404 for ${path.split("?")[0]}`);
    if (response.status !== 200) throw new GitHubUnavailable(`GitHub HTTP ${response.status}`);
    tally.counted += 1;
    const payload = await response.json();
    if (!Array.isArray(payload)) throw new GitHubUnavailable("GitHub list answer was not a list");
    items.push(...payload.filter((item) => item && typeof item === "object"));
    if (payload.length < PAGE_SIZE) return items;
    page += 1;
  }
}

function issueRow(item) {
  const row = {};
  for (const field of ISSUE_FIELDS) row[field] = item[field] ?? null;
  return row;
}

function commitRow(item) {
  const commit = item.commit && typeof item.commit === "object" ? item.commit : {};
  const committer = commit.committer && typeof commit.committer === "object" ? commit.committer : {};
  const message = typeof commit.message === "string" ? commit.message : "";
  return { sha: item.sha ?? null, date: committer.date ?? null, message: message.split("\n")[0] };
}

function releaseRow(item) {
  if (item.draft || typeof item.published_at !== "string") return null;
  return { tag_name: item.tag_name ?? null, name: item.name ?? null, html_url: item.html_url ?? null, published_at: item.published_at };
}

async function collectRepo(fetchImpl, token, repo, since, tally) {
  const rawIssues = await readPaged(fetchImpl, token, `repos/${repo}/issues?state=all`, tally);
  const issues = rawIssues.filter((item) => !("pull_request" in item)).map(issueRow);
  const commits = (await readPaged(fetchImpl, token, `repos/${repo}/commits?since=${since}`, tally)).map(commitRow);
  const releases = (await readPaged(fetchImpl, token, `repos/${repo}/releases`, tally)).map(releaseRow).filter(Boolean);
  return { issues, commits, releases };
}

export async function buildMetrics(env, fetchImpl = globalThis.fetch, now = new Date()) {
  const token = (env.GITHUB_TOKEN || "").trim();
  if (!token) {
    return { status: 500, body: { error: "GitHub is not configured" } };
  }
  const repos = String(env.METRICS_REPOS || "").split(",").map((item) => item.trim()).filter(Boolean);
  const days = Number(env.METRICS_COMMITS_DAYS) > 0 ? Number(env.METRICS_COMMITS_DAYS) : DEFAULT_COMMITS_DAYS;
  const since = commitsSince(now, days);
  const generated = now.toISOString();
  let rate = null;
  const projects = [];
  for (const repo of repos) {
    const tally = { total: 0, counted: 0, rate: null };
    const name = repo.split("/")[1] || repo;
    try {
      const lists = await collectRepo(fetchImpl, token, repo, since, tally);
      projects.push({ root: null, name, repo, fetched_at: generated, error: null, ...lists, commits_since: since,
        requests_total: tally.total, requests_counted: tally.counted });
    } catch (error) {
      const message = error instanceof GitHubUnavailable ? error.message : `GitHub unreachable: ${error.name || "Error"}`;
      projects.push({ root: null, name, repo, fetched_at: null, error: message, issues: [], commits: [], releases: [],
        commits_since: since, requests_total: tally.total, requests_counted: tally.counted });
    }
    if (tally.rate) rate = tally.rate;
  }
  return { status: 200, body: { generated_at: generated, started_at: null, refreshed_at: generated, rate_limit: rate, projects } };
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const key = new Request(new URL(request.url).toString(), { method: "GET" });
  if (cache) {
    const hit = await cache.match(key);
    if (hit) return hit;
  }
  const result = await buildMetrics(env);
  const response = new Response(JSON.stringify(result.body), {
    status: result.status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
  });
  if (cache && result.status === 200) {
    const stored = response.clone();
    if (context.waitUntil) context.waitUntil(cache.put(key, stored)); else await cache.put(key, stored);
  }
  return response;
}
