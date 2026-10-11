// #442: Mission Control shows the regression deadline in force, the launch
// deadline once a request is accepted, never the approval expiry alone.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const { regressionDeadline } = require(path.join(root, "dashboard", "lib", "dashboard-logic.js"));

const awaiting = {
  state: "awaiting_approval",
  expires_at: "2026-10-10T21:45:52+00:00",
};
const accepted = {
  ...awaiting,
  state: "accepted",
  decided_at: "2026-10-10T21:16:37+00:00",
  accept_expires_at: "2026-10-10T21:26:37+00:00",
};

test("an accepted request shows its launch deadline, not the approval expiry", () => {
  assert.deepEqual(regressionDeadline(accepted), { label: "Launch by", at: "2026-10-10T21:26:37+00:00" });
});

test("a request awaiting approval shows the approval expiry", () => {
  assert.deepEqual(regressionDeadline(awaiting), { label: "Expires", at: "2026-10-10T21:45:52+00:00" });
});

test("a legacy accepted request without the field and a missing request fall back safely", () => {
  const legacy = { ...accepted };
  delete legacy.accept_expires_at;
  assert.deepEqual(regressionDeadline(legacy), { label: "Expires", at: "2026-10-10T21:45:52+00:00" });
  assert.deepEqual(regressionDeadline(null), { label: "Expires", at: null });
  assert.deepEqual(regressionDeadline({}), { label: "Expires", at: null });
});

test("regressionRecordText renders the deadline in force", () => {
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  const start = app.indexOf("function regressionRecordText(request)");
  assert.notEqual(start, -1);
  const end = app.indexOf("\nfunction ", start + 1);
  const body = app.slice(start, end);
  const record = new Function("regressionDeadline", "relativeTime", `${body}\nreturn regressionRecordText;`)(
    regressionDeadline, (value) => `rel:${value}`);
  const text = record({ ...accepted, group: "python-full", commands: ["python3 tests/shard.py --all"] });
  assert.match(text, /Launch by: 2026-10-10T21:26:37\+00:00 \(rel:2026-10-10T21:26:37\+00:00\)/);
  assert.doesNotMatch(text, /21:45:52/);
  const pending = record({ ...awaiting, group: "python-full", commands: [] });
  assert.match(pending, /Expires: 2026-10-10T21:45:52\+00:00 \(rel:2026-10-10T21:45:52\+00:00\)/);
});
