// #453: the Fleet page's Queued section, rendered from /api/fleet's
// sentinel_queue: per repository the run holding the slot (or SLOT FREE),
// the queued issues in the order the server sends (Sentinel's admission
// order) and the items held at the rig; escaped; hidden when nothing is
// queued or held.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");

// The page loaded whole, as fleet_sections.test.js does, with one element
// per id so the section's markup and hidden class can be read back.
function loadFleetPage() {
  const elements = {};
  const element = () => {
    const classes = new Set();
    return {
      textContent: "", innerHTML: "", value: "", dataset: {}, open: false, classes,
      classList: {
        toggle(name, on) { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); },
        add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      },
      addEventListener() {}, querySelector: element, showModal() {},
    };
  };
  const context = {
    document: { getElementById: (id) => (elements[id] ||= element()), querySelectorAll: () => [], body: element() },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    EventSource: class { addEventListener() {} },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {},
    Date, JSON, Math, Number, String, Array, Boolean, Set, Object, Promise, Error,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "dashboard/lib/run-vocabulary.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(root, "fleet/app.js"), "utf8"), context, { filename: "fleet/app.js" });
  context.elements = elements;
  return context;
}

const minutesAgo = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
const url = (number) => `https://github.com/monzta1/project-handsoff/issues/${number}`;
const flat = (markup) => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const panelOf = (page) => page.document.getElementById("sentinel-queue");

function view(repos) {
  return { fetched_at: new Date().toISOString(), note: null, repos };
}

test("#453: the page has a Queued section, hidden until there is something to show", () => {
  assert.match(html, /<section id="sentinel-queue" class="panel queue-panel hidden"/);
});

test("#453: the running item and each queued item render in the server's order", () => {
  const page = loadFleetPage();
  page.renderSentinelQueue(view({
    "monzta1/project-handsoff": {
      running: { number: 445, state: "running", phase: "host", url: url(445), title: "Managed architect" },
      slot_free: false,
      queued: [
        { number: 446, url: url(446), title: "First in", trigger: "user", queued_at: minutesAgo(30) },
        { number: 442, url: url(442), title: null, trigger: "maintainer", queued_at: minutesAgo(5) },
      ],
      rig: [],
    },
  }));
  const panel = panelOf(page);
  assert.equal(panel.classes.has("hidden"), false);
  const text = flat(panel.innerHTML);
  assert.match(text, /SENTINEL Queued 2 QUEUED monzta1\/project-handsoff/);
  assert.match(text, /RUNNING #445 Managed architect RUNNING · host/);
  assert.match(text, /1 #446 First in USER · WAITING 30 min 2 #442 title not cached MAINTAINER · WAITING 5 min/);
  assert.ok(text.indexOf("#446") < text.indexOf("#442"), "admission order is kept");
  assert.match(panel.innerHTML, /<a class="queue-number" href="https:\/\/github\.com\/monzta1\/project-handsoff\/issues\/446" target="_blank" rel="noopener">#446<\/a>/);
});

test("#453: a free slot reads SLOT FREE", () => {
  const page = loadFleetPage();
  page.renderSentinelQueue(view({
    "monzta1/ToneCommand": { running: null, slot_free: true, rig: [],
      queued: [{ number: 7, url: "https://github.com/monzta1/ToneCommand/issues/7", title: "x", trigger: "user", queued_at: minutesAgo(1) }] },
  }));
  assert.match(flat(panelOf(page).innerHTML), /monzta1\/ToneCommand SLOT FREE 1 #7 x USER/);
});

test("#453: every Sentinel string is escaped and a non-GitHub url is not linked", () => {
  const page = loadFleetPage();
  page.renderSentinelQueue(view({
    "monzta1/<x>": {
      running: { number: 1, state: "<b>paused</b>", phase: "<i>p</i>", url: url(1), title: "<script>alert(1)</script>" },
      queued: [{ number: 2, url: "javascript:alert(1)", title: "<img src=x onerror=alert(1)>", trigger: "<u>t</u>", queued_at: minutesAgo(2) }],
      rig: [{ number: 3, url: url(3), title: "a & b", reason: "<em>device</em>", held_at: minutesAgo(3) }],
    },
  }));
  const markup = panelOf(page).innerHTML;
  for (const raw of ["<script>", "<img", "<b>", "<i>", "<u>", "<em>", "<x>", "javascript:"]) {
    assert.equal(markup.includes(raw), false, `${raw} must not reach the page unescaped`);
  }
  assert.match(markup, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(markup, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(markup, /a &amp; b/);
  assert.match(markup, /data-repo="monzta1\/&lt;x&gt;"/);
  assert.match(markup, /<span class="queue-number">#2<\/span>/);
});

test("#453: the section is hidden when nothing is queued, even with a run holding the slot", () => {
  const page = loadFleetPage();
  const panel = panelOf(page);
  page.renderSentinelQueue(view({
    "monzta1/project-handsoff": { running: { number: 445, state: "running", phase: "host", url: url(445), title: "t" },
      slot_free: false, queued: [], rig: [] },
  }));
  assert.equal(panel.classes.has("hidden"), true);
  assert.equal(panel.innerHTML, "");
  for (const empty of [view({}), { fetched_at: null, note: "no Sentinel state on this machine", repos: {} }, undefined, null]) {
    page.renderSentinelQueue(empty);
    assert.equal(panel.classes.has("hidden"), true);
    assert.equal(panel.innerHTML, "");
  }
});

test("#453: a repository with only rig-held items still shows, with the reason and wait", () => {
  const page = loadFleetPage();
  page.renderSentinelQueue(view({
    "monzta1/project-handsoff": { running: null, slot_free: true, queued: [], rig: [] },
    "monzta1/ToneCommand": { running: null, slot_free: true, queued: [],
      rig: [{ number: 31, url: "https://github.com/monzta1/ToneCommand/issues/31", title: null, reason: "device label", held_at: minutesAgo(120) }] },
  }));
  const panel = panelOf(page);
  assert.equal(panel.classes.has("hidden"), false);
  const text = flat(panel.innerHTML);
  assert.match(text, /0 QUEUED monzta1\/ToneCommand SLOT FREE RIG #31 title not cached device label · WAITING 2 h/);
  assert.equal(panel.innerHTML.includes('data-repo="monzta1/project-handsoff"'), false, "an empty repository is not listed");
});

test("#453: render() draws the section from the fleet payload", () => {
  const page = loadFleetPage();
  page.render({ generated_at: new Date().toISOString(), engine: {}, counts: {}, decisions: [], projects: [],
    sentinel_queue: view({ "monzta1/project-handsoff": { running: null, slot_free: true, rig: [],
      queued: [{ number: 450, url: url(450), title: "Queued", trigger: "user", queued_at: minutesAgo(1) }] } }) });
  assert.equal(panelOf(page).classes.has("hidden"), false);
  assert.match(flat(panelOf(page).innerHTML), /#450 Queued USER/);
});
