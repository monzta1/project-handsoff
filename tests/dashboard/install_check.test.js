// #216: the Fleet engine badge reads the install block.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "..", "..", "fleet", "app.js"), "utf8");
const start = app.indexOf("function engineBadgeLabel("), end = app.indexOf("\nfunction projectCard(");
const { engineBadgeLabel, engineBadgeTitle } = new Function(`${app.slice(start, end)}\nreturn { engineBadgeLabel, engineBadgeTitle };`)();

test("the badge reads the version alone when nothing is live", () => {
  assert.equal(engineBadgeLabel({ version: "v0.3.70", source: "installed-engine", install_blocked: null }), "ENGINE v0.3.70");
  assert.equal(engineBadgeLabel({ version: "unknown" }), "ENGINE UNKNOWN");
});

test("the badge reads install blocked with the count and names the sessions in the title", () => {
  const engine = { version: "v0.3.70", source: "installed-engine",
    install_blocked: { count: 2, sessions: [{ root: "/a", role: "reviewer", session_id: "hs-1" }, { root: "/b", role: "implementer", session_id: "hs-2" }] } };
  assert.equal(engineBadgeLabel(engine), "ENGINE v0.3.70 (install blocked: 2 live sessions)");
  assert.equal(engineBadgeLabel({ ...engine, install_blocked: { count: 1, sessions: [engine.install_blocked.sessions[0]] } }), "ENGINE v0.3.70 (install blocked: 1 live session)");
  assert.match(engineBadgeTitle(engine), /install blocked by reviewer hs-1 on \/a, implementer hs-2 on \/b/);
});

test("renderEngineBadge uses the helpers and marks the badge", () => {
  assert.match(app, /badge\.textContent = engineBadgeLabel\(engine\)/);
  assert.match(app, /badge\.classList\.toggle\("install-blocked", Boolean\(engine\.install_blocked\)\)/);
});
