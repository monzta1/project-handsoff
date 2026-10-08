const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");

// P2.4: fleet/app.js is loaded whole into a vm context, as in
// fleet_signals.test.js, and the real projectCard renders a closed run for
// each close outcome. The assertions are on the text a Pilot would read.
function loadFleetPage() {
  const element = () => ({
    textContent: "", innerHTML: "", value: "", dataset: {}, open: false,
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, querySelector: element, showModal() {},
  });
  const context = {
    document: { getElementById: element, querySelectorAll: () => [], body: element() },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {},
    Date, JSON, Math, Number, String, Array, Boolean, Set, Object, Promise, Error,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "dashboard/lib/run-vocabulary.js"), "utf8"), context, { filename: "dashboard/lib/run-vocabulary.js" });
  vm.runInContext(fs.readFileSync(path.join(root, "fleet/app.js"), "utf8"), context, { filename: "fleet/app.js" });
  return context;
}

const now = Date.now();
const iso = (secondsAgo) => new Date(now - secondsAgo * 1000).toISOString();
const closedCard = (name, runClosed) => ({
  root: `/tmp/${name}`, name, registered_at: iso(86400), initialized: true, feature: `Feature ${name}`,
  phase: "Release", phase_number: 8, progress: 100, state: "closed", decisions: [],
  engine_version: "v0.5.14", updated_at: iso(30), dashboard_url: null, dashboard_note: "no run-owned dashboard",
  run_closed: { by: "pilot", at: iso(60), cancelled_active: false, session_ids: [], ...runClosed },
});
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

test("a closed card names each new outcome and its reason", () => {
  const page = loadFleetPage();
  for (const outcome of ["qa_pending", "blocked_environment"]) {
    const html = page.projectCard(closedCard(outcome, { outcome, reason: `waiting on ${outcome}` }));
    assert.match(html, new RegExp(`<p class="close-outcome" data-outcome="${outcome}">`));
    assert.match(text(html), new RegExp(`CLOSED AS ${outcome.toUpperCase()} · waiting on ${outcome}`));
    assert.doesNotMatch(text(html), /KNOWN RISK/);
  }
});

test("verified_with_known_risk shows its reason and the known risk", () => {
  const page = loadFleetPage();
  const html = page.projectCard(closedCard("risk", {
    outcome: "verified_with_known_risk", reason: "shipped", known_risk: "cache may serve stale counts",
  }));
  assert.match(text(html), /CLOSED AS VERIFIED_WITH_KNOWN_RISK · shipped · KNOWN RISK: cache may serve stale counts/);
});

test("the outcome, reason and known risk are escaped", () => {
  const page = loadFleetPage();
  const html = page.projectCard(closedCard("xss", {
    outcome: "verified_with_known_risk", reason: "<script>a</script>", known_risk: "<img src=x onerror=1>",
  }));
  assert.doesNotMatch(html, /<script>|<img src=x/);
  assert.match(html, /&lt;script&gt;a&lt;\/script&gt;/);
  assert.match(html, /KNOWN RISK: &lt;img src=x onerror=1&gt;/);
});

test("a run that is not closed shows no close outcome", () => {
  const page = loadFleetPage();
  const card = { ...closedCard("open", {}), state: "running" };
  delete card.run_closed;
  assert.doesNotMatch(page.projectCard(card), /close-outcome/);
});
