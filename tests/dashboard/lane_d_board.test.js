const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Lane D: percent complete on the CI row (#198), host or managed on every
// chiclet (#199), the host-wait line on the Fleet card (#194).
const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const logic = require(path.join(root, "dashboard/lib/dashboard-logic.js"));
const fleetApp = read("fleet/app.js");

const checks = (done, total) => Array.from({ length: total }, (_, i) => ({ name: `c${i}`, state: i < done ? "SUCCESS" : "IN_PROGRESS", elapsed_seconds: 10, link: null }));
const running = { state: "running", elapsed_seconds: 62, expected_seconds: 124, progress: 0.5, checks: checks(4, 8), note: null };

test("the label leads with a time-based percent, capped at 99 until every check is done", () => {
  assert.equal(logic.ciPercent(running), 50);
  assert.equal(logic.ciProgressLabel(running), "50% · 4 of 8 checks done · 1m 02s of about 2m 04s");
  assert.equal(logic.ciPercent({ ...running, progress: 1, elapsed_seconds: 130 }), 99, "past the estimate with checks still running: 99");
  assert.equal(logic.ciPercent({ ...running, progress: 1, checks: checks(8, 8) }), 100, "every check done and the time says 100: 100");
  assert.equal(logic.ciPercent({ ...running, expected_seconds: null, progress: null }), 50, "no estimate: the checks-done fraction");
  assert.equal(logic.ciProgressLabel({ ...running, expected_seconds: null, progress: null }), "50% · 4 of 8 checks done · 1m 02s elapsed");
  assert.equal(logic.ciPercent({ ...running, expected_seconds: null, progress: null, checks: checks(8, 8) }), 99, "no estimate, all done, not yet terminal: 99");
  assert.equal(logic.ciPercent({ ...running, expected_seconds: null, progress: null, checks: [] }), null, "nothing to count yet");
  assert.equal(logic.ciProgressLabel({ ...running, expected_seconds: null, progress: null, checks: [] }), "1m 02s elapsed");
  assert.equal(logic.ciPercent({ state: "passed", elapsed_seconds: 124, checks: checks(8, 8) }), 100);
  assert.equal(logic.ciProgressLabel({ state: "passed", elapsed_seconds: 124, expected_seconds: 124, checks: checks(8, 8) }), "100% · passed in 2m 04s (last run 2m 04s)");
  assert.equal(logic.ciProgressLabel({ state: "failed", failed_check: "tests", elapsed_seconds: 90, checks: checks(8, 8) }), "tests failed after 1m 30s");
  // the percent ticks with the timer
  const ticked = logic.ciTicked(running, 10);
  assert.equal(logic.ciPercent(ticked), 58);
  assert.equal(logic.ciProgressLabel(ticked), "58% · 4 of 8 checks done · 1m 12s of about 2m 04s");
  assert.equal(logic.ciNote(running), "", "the count lives in the label now");
});

test("a chiclet says host or managed after the family", () => {
  const member = (key, actor) => ({ key, actor });
  const snap = { status: { phase_number: 5 }, host: { family: "claude", actor: "claude-host", source: "initialized" },
    crew: [member("implementer", "codex-implementer"), member("reviewer", "codex-reviewer"), member("supervisor", null), member("architect", null)],
    settings: { crew: { supervisor: { adapter: "host" }, architect: { adapter: "host" }, implementer: { adapter: "codex" }, reviewer: { adapter: "codex" } } } };
  assert.deepEqual(["supervisor", "architect", "implementer", "reviewer"].map((r) => logic.roleWord(r, snap)),
    ["claude · host", "claude · host", "codex · managed", "codex · managed"]);
  assert.deepEqual(["supervisor", "implementer"].map((r) => logic.roleStationKind(r, snap)), ["host", "managed"]);
  assert.equal(logic.roleTitle("reviewer", snap), "REVIEWER · codex · managed");
  // unknown host family reads host once, never "host · host"
  assert.equal(logic.roleWord("supervisor", { ...snap, host: { family: "unknown" } }), "host");
  // a configured station with no session reads the adapter alone
  const empty = { status: {}, crew: [], settings: { crew: { implementer: { adapter: "codex" } } } };
  assert.equal(logic.roleWord("implementer", empty), "codex");
  assert.equal(logic.roleStationKind("implementer", empty), null);
  assert.equal(logic.roleWord("architect", { status: {}, crew: [], settings: {} }), null);
});

test("the Fleet card names the host it waits on and how long", () => {
  assert.match(fleetApp, /function hostWaitTag\(project\)/);
  assert.match(fleetApp, /<span>\$\{crew\}<\/span>\$\{hostWaitTag\(project\)\}/);
  const start = fleetApp.indexOf("function hostWaitTag("), end = fleetApp.indexOf("\nfunction projectCard(");
  const esc = (v) => String(v ?? "");
  const hostWaitTag = new Function("esc", `${fleetApp.slice(start, end)}\nreturn hostWaitTag;`)(esc);
  assert.equal(hostWaitTag({ host_wait: { family: "codex", silent_seconds: 8 * 3600 + 240, action: "launch design-review attempt 3" } }),
    '<span class="host-wait">WAITING ON HOST CODEX 8 h 04 m</span>');
  assert.equal(hostWaitTag({ host_wait: { family: "claude", silent_seconds: 900 } }), '<span class="host-wait">WAITING ON HOST CLAUDE 15 min</span>');
  assert.equal(hostWaitTag({ host_wait: null }), "");
  assert.equal(hostWaitTag({}), "");
  assert.match(read("fleet/styles.css"), /\.host-wait \{/);
});
