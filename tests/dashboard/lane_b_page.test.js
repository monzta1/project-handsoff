const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Lane B (#186, #183, #184): the host badge and the chiclet word, the Fleet
// card's host tag, the fixed copy gone, the label hidden on a steady run,
// the FAILOVER card hidden until it has an event, the token cells folded
// into one line until a session reports usage.
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const app = read("dashboard/app.js");
const html = read("dashboard/index.html");
const css = read("dashboard/styles.css");
const fleetApp = read("fleet/app.js");
const fleetCss = read("fleet/styles.css");

test("the host badge and title read the family from the snapshot, never a guess", () => {
  assert.equal(logic.hostBadgeLabel({ family: "claude", actor: "claude-host", source: "initialized" }), "HOST CLAUDE");
  assert.equal(logic.hostBadgeLabel({ family: "codex", actor: "codex-implementer", source: "ledger" }), "HOST CODEX");
  assert.equal(logic.hostBadgeLabel({ family: "unknown", actor: null, source: "none" }), "HOST UNKNOWN");
  assert.equal(logic.hostBadgeLabel(null), "HOST UNKNOWN");
  assert.equal(logic.hostBadgeLabel({ family: "moncy" }), "HOST UNKNOWN", "only claude and codex are families");
  assert.equal(logic.hostBadgeTitle({ family: "codex", actor: "codex-implementer", source: "ledger" }), "Host driving this run: codex-implementer (ledger)");
  assert.equal(logic.hostBadgeTitle({ family: "unknown", actor: null, source: "none" }), "Host driving this run: not recorded (init --by)");
  assert.match(html, /<span id="host-badge" class="engine-badge host-badge" title="Host driving this run">HOST UNKNOWN<\/span>/);
  const engine = html.indexOf('id="engine-badge"'), host = html.indexOf('id="host-badge"');
  assert.ok(engine > 0 && host > engine, "the host badge sits beside the engine badge");
  assert.match(app, /hostBadge\.textContent = hostBadgeLabel\(snapshot\.host\);/);
  assert.match(app, /hostBadge\.dataset\.family = snapshot\.host\?\.family \|\| "unknown";/);
  assert.match(css, /\.host-badge\[data-family="claude"\]/);
  assert.match(css, /\.host-badge\[data-family="codex"\]/);
});

test("a host-adapter station reads the family instead of the word host when it is known", () => {
  const snapshot = (family) => ({ status: {}, crew: [], host: { family, actor: null, source: "x" },
    settings: { crew: { supervisor: { adapter: "host" }, architect: { adapter: "host" }, implementer: { adapter: "codex" } } } });
  assert.equal(logic.roleWord("supervisor", snapshot("claude")), "claude");
  assert.equal(logic.roleWord("architect", snapshot("codex")), "codex");
  assert.equal(logic.roleWord("supervisor", snapshot("unknown")), "host", "unknown keeps the word host");
  assert.equal(logic.roleWord("implementer", snapshot("claude")), "codex", "a non-host station is unchanged");
  assert.equal(logic.roleTitle("supervisor", snapshot("claude")), "SUPERVISOR · claude");
  // a recorded actor on the station still wins (#164)
  const recorded = { ...snapshot("codex"), crew: [{ key: "supervisor", actor: "claude-supervisor" }] };
  assert.equal(logic.roleWord("supervisor", recorded), "claude");
});

test("the Fleet card carries a host tag for a known family and nothing for unknown", () => {
  assert.match(fleetApp, /function hostTag\(project\)/);
  assert.match(fleetApp, /<p class="project-name">\$\{esc\(project\.name\)\}\$\{hostTag\(project\)\}<\/p>/);
  const start = fleetApp.indexOf("function hostTag("), end = fleetApp.indexOf("\nfunction projectCard(");
  const esc = (v) => String(v ?? "");
  const hostTag = new Function("esc", `${fleetApp.slice(start, end)}\nreturn hostTag;`)(esc);
  assert.equal(hostTag({ host: "claude" }), ' <span class="host-tag" data-family="claude">HOST CLAUDE</span>');
  assert.equal(hostTag({ host: "codex" }), ' <span class="host-tag" data-family="codex">HOST CODEX</span>');
  assert.equal(hostTag({ host: "unknown" }), "");
  assert.equal(hostTag({}), "");
  assert.match(fleetCss, /\.host-tag\[data-family="claude"\]/);
});

test("the fixed copy is gone, the label hides on a steady run, FAILOVER hides at zero, tokens fold to one line", () => {
  assert.ok(!html.includes("supervisor-reassurance"), "no reassurance node");
  assert.ok(!app.includes("supervisor.reassurance"), "no reassurance rendering");
  assert.match(app, /\$\("briefing-state"\)\.classList\.toggle\("hidden", supervisor\.tone === "steady"\);/);
  assert.match(html, /<article id="replacement-panel" class="panel replacement-panel hidden">/);
  assert.match(app, /panel\.classList\.toggle\("hidden", total === 0\);/);
  assert.match(html, /<div id="metrics-tokens-cell">/);
  assert.match(html, /<p id="metrics-tokens-note" class="metrics-note hidden"><\/p>/);
  assert.match(app, /tokensCell\.classList\.toggle\("hidden", !reported\);/);
  assert.match(app, /note\.textContent = reported \? "" : `tokens: not reported by \$\{adapters\.length \? adapters\.join\(", "\) : "any session yet"\}`;/);
  assert.match(app, /TOKENS NOT REPORTED/);
  assert.ok(!app.includes("engine.previews"), "no preview rendering");
  assert.ok(!app.includes("engine.execution_reason"), "no permanent execution line");
});

test("a Phase 2 snapshot renders fewer fixed rows than before", () => {
  // Rows that were always rendered on a fresh run and are now conditional or gone.
  const gone = ["supervisor-reassurance", "engine.previews", "engine.execution_reason"];
  for (const marker of gone) assert.ok(!app.includes(marker), marker);
  const conditional = [/replacement-panel" class="panel replacement-panel hidden"/, /metrics-tokens-note" class="metrics-note hidden"/];
  for (const pattern of conditional) assert.match(html, pattern);
  // Before Lane B: the three READ ONLY rows, the reason line, two preview
  // rows, the reassurance line, the FAILOVER card's empty body and six
  // token dashes; twelve rows that said nothing. Now none of them render
  // on a Phase 2 snapshot with no sessions reporting usage.
  assert.equal(gone.length + conditional.length + 3 /* engine rows */ + 2 /* previews */, 10);
});
