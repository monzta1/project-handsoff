const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// #164: one word per role chiclet naming the agent family at the station.
const root = path.resolve(__dirname, "../..");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");

const snapshot = (phase, crew, settings) => ({ status: { phase_number: phase }, crew, settings: settings ? { crew: settings } : undefined });
const member = (key, actor) => ({ key, actor });

test("roleWord: actor prefix first, exactly claude- or codex-, else the configured adapter, else null; #199 adds host or managed", () => {
  const snap = snapshot(5, [member("implementer", "claude-implementer"), member("reviewer", "codex-reviewer"), member("design_reviewer", "codex-design"),
    member("supervisor", null), member("architect", "claude-architect")], { supervisor: { adapter: "host" }, architect: { adapter: "codex" } });
  assert.equal(logic.roleWord("implementer", snap), "claude · managed");
  assert.equal(logic.roleWord("reviewer", snap), "codex · managed");
  assert.equal(logic.roleWord("supervisor", snap), "host");     // no actor: configured adapter, family unknown
  assert.equal(logic.roleWord("architect", snap), "claude · managed");    // actor beats the configured codex
  assert.equal(logic.roleFamily("architect", snap), "claude");
  assert.equal(logic.roleStationKind("implementer", snap), "managed");
  assert.equal(logic.roleStationKind("supervisor", snap), "host");
  for (const bad of ["claude", "Claude-x", "claudex-y", "codex", "CODEX-r", "", null, 42]) {
    const s = snapshot(5, [member("implementer", bad)], { implementer: { adapter: "auto" } });
    assert.equal(logic.roleWord("implementer", s), "auto", String(bad));
  }
  assert.equal(logic.roleWord("implementer", snapshot(5, [member("implementer", "Claude-x")], { implementer: { adapter: " " } })), null);
  assert.equal(logic.roleWord("implementer", {}), null);
  assert.equal(logic.roleWord("implementer", null), null);
});

test("the reviewer station is the design reviewer in Phases 1 and 2 and the implementation reviewer otherwise", () => {
  const crew = [member("design_reviewer", "codex-design"), member("reviewer", "claude-impl-review")];
  assert.equal(logic.roleWord("reviewer", snapshot(1, crew)), "codex · managed");
  assert.equal(logic.roleWord("reviewer", snapshot(2, crew)), "codex · managed");
  assert.equal(logic.roleWord("reviewer", snapshot(3, crew)), "claude · managed");
  assert.equal(logic.roleWord("reviewer", snapshot(4, crew)), "claude · managed");
  assert.equal(logic.roleWord("reviewer", snapshot(5, crew)), "claude · managed");
  assert.equal(logic.roleWord("reviewer", snapshot(undefined, crew)), "claude · managed");
  assert.equal(logic.roleWord("reviewer", snapshot("two", crew)), "claude · managed");
  assert.equal(logic.roleStation("reviewer", snapshot(2, crew)).key, "design_reviewer");
});

test("roleTitle joins ROLE, word, model and source, omitting what is missing", () => {
  const full = snapshot(5, [member("supervisor", null)], { supervisor: { adapter: "host", model: "default", adapter_source: "explicit" } });
  assert.equal(logic.roleTitle("supervisor", full), "SUPERVISOR · host · default · explicit");
  const partial = snapshot(5, [member("implementer", "codex-implementer")], { implementer: { adapter: "codex", model: "", adapter_source: "recommended" } });
  assert.equal(logic.roleTitle("implementer", partial), "IMPLEMENTER · codex · managed · recommended");
  assert.equal(logic.roleTitle("architect", {}), "ARCHITECT");
  assert.equal(logic.roleTitle("architect", null), "ARCHITECT");
});

// The real renderRoleChiclets from dashboard/app.js, run against a stub DOM
// with the logic helpers in scope, exactly as the page has them.
function renderWith(activeRole, snap) {
  const start = app.indexOf("function renderRoleChiclets(");
  const end = app.indexOf("\nfunction renderCrew(");
  assert.ok(start > 0 && end > start, "renderRoleChiclets is defined before renderCrew in app.js");
  const source = app.slice(start, end);
  const chiclets = ["supervisor", "architect", "implementer", "reviewer"].map((role) => {
    const el = { dataset: { role }, title: "", children: [], classes: new Set(),
      classList: { toggle(cls, on) { on ? el.classes.add(cls) : el.classes.delete(cls); } },
      querySelector(selector) { return selector === "small.chiclet-word" ? (el.children[0] || null) : null; },
      appendChild(child) { el.children.push(child); child.parent = el; },
      text() { return `${role.toUpperCase()}${el.children.map((c) => " " + c.textContent).join("")}`; } };
    return el;
  });
  const context = { document: { querySelectorAll: () => chiclets, createElement: () => ({ className: "", textContent: "", remove() { const p = this.parent; if (p) p.children = p.children.filter((c) => c !== this); } }) },
    roleWord: logic.roleWord, roleTitle: logic.roleTitle, String, Number, Array };
  vm.createContext(context);
  vm.runInContext(`${source}\nrenderRoleChiclets(${JSON.stringify(activeRole)}, ${JSON.stringify(snap)});`, context);
  return chiclets;
}

test("the rendered chiclets carry the word and the title for a full snapshot, and the role alone for an empty one", () => {
  const full = snapshot(5, [member("supervisor", null), member("architect", "claude-architect"), member("implementer", "claude-implementer"),
    member("reviewer", "codex-reviewer"), member("design_reviewer", "codex-design")],
    { supervisor: { adapter: "host", model: "default", adapter_source: "explicit" }, architect: { adapter: "host" }, implementer: { adapter: "codex" }, reviewer: { adapter: "codex", model: "default", adapter_source: "explicit" } });
  const rendered = renderWith("implementer", full);
  assert.deepEqual(rendered.map((el) => el.text()), ["SUPERVISOR host", "ARCHITECT claude · host", "IMPLEMENTER claude · managed", "REVIEWER codex · managed"]);
  assert.deepEqual(rendered.map((el) => el.title), ["SUPERVISOR · host · default · explicit", "ARCHITECT · claude · host", "IMPLEMENTER · claude · managed", "REVIEWER · codex · managed · default · explicit"]);
  assert.deepEqual(rendered.map((el) => el.classes.has("is-active")), [false, false, true, false]);
  assert.ok(rendered.every((el) => el.children.length === 1 && el.children[0].className === "chiclet-word"));
  const empty = renderWith(null, { status: {}, crew: [], settings: {} });
  assert.deepEqual(empty.map((el) => el.text()), ["SUPERVISOR", "ARCHITECT", "IMPLEMENTER", "REVIEWER"]);
  assert.deepEqual(empty.map((el) => el.title), ["SUPERVISOR", "ARCHITECT", "IMPLEMENTER", "REVIEWER"]);
  assert.ok(empty.every((el) => el.children.length === 0));
});

test("app.js hands the snapshot to the renderer and the word has its faint style", () => {
  assert.match(app, /renderRoleChiclets\(status\.status === "closed" \? null : snapshot\.actors\.active_role, snapshot\);/);
  assert.match(css, /\.chiclet \.chiclet-word \{ margin-left: 2px; color: var\(--faint\); font: 500 9px\/1 var\(--mono\); letter-spacing: 0; text-transform: lowercase;/);
});
