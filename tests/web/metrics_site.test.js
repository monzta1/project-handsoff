const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

// #163: the site assembly (web/build.py), the page's contract with
// fleet/metrics.js, the phone stylesheet and the deploy workflow.
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const indexHtml = read("web/public/index.html");
const phoneCss = read("web/public/phone.css");
const metricsJs = read("fleet/metrics.js");
const workflow = read(".github/workflows/metrics-site.yml");

test("index.html carries every id fleet/metrics.js reads, loads both sheets in order, and says ENGINE WEB", () => {
  const ids = ["kpis", "charts", "releases", "breakdown", "table-wrap", "range-label", "source-note", "project", "custom-range", "from", "to", "toggle-table", "synced", "link", "engine-badge"];
  for (const id of ids) assert.match(indexHtml, new RegExp(`id="${id}"`), id);
  // Every $("...") the page code asks for is present.
  for (const match of metricsJs.matchAll(/\$\("([a-z-]+)"\)/g)) assert.ok(indexHtml.includes(`id="${match[1]}"`), match[1]);
  assert.ok(indexHtml.indexOf('href="/styles.css"') < indexHtml.indexOf('href="/phone.css"'));
  assert.match(indexHtml, /<script src="\/metrics.js" defer><\/script>/);
  assert.match(indexHtml, /id="engine-badge" class="engine-badge"[^>]*>ENGINE WEB<\/span>/);
  assert.doesNotMatch(indexHtml, /MISSIONS/);
  assert.doesNotMatch(indexHtml, /style="/);
});

test("phone.css makes the page phone-first below 760 px with touch-sized controls", () => {
  assert.match(phoneCss, /\.preset-row button, \.filters \.secondary \{ min-height: 44px; min-width: 44px;/);
  const narrow = phoneCss.slice(phoneCss.indexOf("@media (max-width: 760px)"));
  assert.match(narrow, /\.charts \{ grid-template-columns: 1fr; \}/);
  assert.match(narrow, /\.kpis \{ grid-template-columns: repeat\(2, minmax\(0, 1fr\)\); \}/);
  assert.match(narrow, /\.preset-row \{ display: grid; grid-template-columns: repeat\(3, minmax\(0, 1fr\)\); width: 100%; \}/);
  assert.match(narrow, /\.scroll-wrap, #breakdown, #table-wrap \{ overflow-x: auto;/);
  assert.match(narrow, /\.topbar-meta \{ grid-column: 1 \/ -1; display: flex; flex-wrap: wrap;/);
});

test("build.py assembles dist from the public files and the Fleet code, byte for byte", () => {
  const dist = fs.mkdtempSync(path.join(os.tmpdir(), "metrics-dist-"));
  const script = `import sys; sys.path.insert(0, ${JSON.stringify(path.join(root, "web"))}); import build; from pathlib import Path; print("\\n".join(str(p) for p in build.build(Path(${JSON.stringify(dist)}))))`;
  const out = execFileSync("python3", ["-c", script], { cwd: root, encoding: "utf8" });
  const written = out.trim().split("\n").map((p) => path.basename(p)).sort();
  assert.deepEqual(written, ["index.html", "metrics.js", "phone.css", "styles.css"]);
  assert.equal(fs.readFileSync(path.join(dist, "metrics.js"), "utf8"), metricsJs);
  assert.equal(fs.readFileSync(path.join(dist, "styles.css"), "utf8"), read("fleet/styles.css"));
  assert.equal(fs.readFileSync(path.join(dist, "index.html"), "utf8"), indexHtml);
  assert.ok(!fs.existsSync(path.join(dist, "functions")), "functions stay beside the deploy directory, not inside it");
  assert.ok(fs.existsSync(path.join(root, "web/functions/api/metrics.js")));
  fs.rmSync(dist, { recursive: true, force: true });
});

test("the workflow deploys web/dist to handsoff-metrics on the right triggers with the right secrets", () => {
  assert.match(workflow, /^on:\n  push:\n    branches: \[main\]\n    paths:\n      - "web\/\*\*"\n      - fleet\/metrics\.js\n      - fleet\/styles\.css\n      - "\.github\/workflows\/metrics-site\.yml"\n  workflow_dispatch:/m);
  assert.match(workflow, /run: python3 web\/build\.py/);
  assert.match(workflow, /pages project create handsoff-metrics --production-branch main \|\| echo "project exists"/);
  assert.match(workflow, /printf '%s' "\$METRICS_GITHUB_TOKEN" \| npx --yes wrangler@3 pages secret put GITHUB_TOKEN --project-name handsoff-metrics/);
  assert.match(workflow, /uses: cloudflare\/wrangler-action@v3\n        with:\n          apiToken: \$\{\{ secrets\.CLOUDFLARE_API_TOKEN \}\}\n          accountId: \$\{\{ secrets\.CLOUDFLARE_ACCOUNT_ID \}\}\n          workingDirectory: web\n          command: pages deploy dist --project-name handsoff-metrics --branch main/);
  for (const secret of ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "METRICS_GITHUB_TOKEN"]) assert.match(workflow, new RegExp(`secrets\\.${secret}`));
  assert.match(workflow, /printf '%s' "\$METRICS_REPOS" \| npx --yes wrangler@3 pages secret put METRICS_REPOS --project-name handsoff-metrics/);
  assert.match(workflow, /METRICS_REPOS: \$\{\{ vars\.METRICS_REPOS \}\}/);
  assert.doesNotMatch(workflow, /ghp_|github_pat_/);
});
