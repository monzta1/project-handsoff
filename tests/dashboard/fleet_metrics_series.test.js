const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

// #153 and #156: the pure series math in fleet/metrics.js, loaded through
// its module.exports guard. Instants are built with the local Date
// constructor so the expectations hold in any zone the test runs in.
const m = require(path.resolve(__dirname, "../../fleet/metrics.js"));

const local = (y, mo, d, h = 12, mi = 0) => new Date(y, mo - 1, d, h, mi).toISOString();
const issue = (number, created, closed = null) => ({ number, title: `Issue ${number}`, created_at: created, closed_at: closed });

test("day keys and day ends are local, and DST days still end at midnight", () => {
  assert.equal(m.dayKey(local(2026, 9, 19, 0, 0)), "2026-09-19");
  assert.equal(m.dayKey(local(2026, 9, 19, 23, 59)), "2026-09-19");
  const end = m.endOfDay("2026-09-19");
  assert.equal(m.dayKey(end), "2026-09-19");
  assert.equal(m.dayKey(new Date(end.getTime() + 1)), "2026-09-20");
  // The day after a US spring-forward is 23 hours long; the end is still that day's last ms.
  assert.equal(m.dayKey(m.endOfDay("2026-03-08")), "2026-03-08");
  assert.equal(m.addDays("2026-02-28", 1), "2026-03-01");
  assert.deepEqual(m.dayRange("2026-09-17", "2026-09-19"), ["2026-09-17", "2026-09-18", "2026-09-19"]);
  assert.deepEqual(m.dayRange("2026-09-19", "2026-09-17"), []);
});

test("custom ranges fill missing ends and swap a reversed pair", () => {
  const today = new Date(2026, 8, 19, 15);
  const issues = [issue(1, local(2026, 8, 30)), issue(2, local(2026, 9, 2))];
  assert.deepEqual(m.normalizeRange("", "", issues, today), { from: "2026-08-30", to: "2026-09-19" });
  assert.deepEqual(m.normalizeRange("2026-09-10", "2026-09-01", issues, today), { from: "2026-09-01", to: "2026-09-10" });
  assert.deepEqual(m.normalizeRange("", "", [], today), { from: "2026-09-19", to: "2026-09-19" });
  assert.deepEqual(m.presetRange("7", today), { from: "2026-09-13", to: "2026-09-19" });
  assert.deepEqual(m.presetRange("today", today), { from: "2026-09-19", to: "2026-09-19" });
  assert.deepEqual(m.presetRange("all", today), { from: null, to: "2026-09-19" });
});

test("median and nearest-rank p90", () => {
  assert.equal(m.median([]), null);
  assert.equal(m.median([5]), 5);
  assert.equal(m.median([3, 1, 2]), 2);
  assert.equal(m.median([4, 1, 3, 2]), 2.5);
  assert.equal(m.percentile([], 0.9), null);
  assert.equal(m.percentile([10, 1, 2, 3, 4, 5, 6, 7, 8, 9], 0.9), 9);   // rank ceil(9) = 9th of 10
  assert.equal(m.percentile([1, 2, 3], 0.9), 3);                        // rank ceil(2.7) = 3
});

test("computeSeries counts opened, closed, backlog, rolling and time to close on a known fixture", () => {
  const issues = [
    issue(1, local(2026, 9, 10), local(2026, 9, 12)),          // ttc 2 d, closed the 12th
    issue(2, local(2026, 9, 12, 9), local(2026, 9, 12, 18)),   // same-day close, ttc 0.375 d
    issue(3, local(2026, 9, 13)),                               // still open
    issue(4, local(2026, 9, 5), local(2026, 9, 20)),           // closed after the range
    issue(5, local(2026, 9, 1), local(2026, 9, 14)),           // created before, closed in range: ttc 13 d
    { number: 6, title: "no dates", created_at: null, closed_at: null },
  ];
  const s = m.computeSeries(issues, "2026-09-11", "2026-09-14");
  assert.deepEqual(s.days, ["2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]);
  assert.deepEqual(s.opened, [0, 1, 1, 0]);
  assert.deepEqual(s.closed, [0, 2, 0, 1]);
  assert.deepEqual(s.rolling7, [0, 1, 2 / 3, 3 / 4]);
  // Backlog at each day's end: issues created by then and not yet closed.
  // 11th: #1, #4, #5 open. 12th: #1 and #2 closed, #4, #5 open. 13th: +#3. 14th: #5 closes.
  assert.deepEqual(s.backlog, [3, 2, 3, 2]);
  assert.deepEqual(s.rollingMedian.map((v) => (v === null ? null : Number(v.toFixed(3)))), [null, 1.188, 1.188, 2]);
  assert.equal(s.totals.opened, 2);
  assert.equal(s.totals.closed, 3);
  assert.equal(s.totals.openNow, 1);   // only #3 has no closed_at; #4 is closed, just after the range
  assert.equal(Number(s.totals.medianTtc.toFixed(3)), 2);
  assert.equal(s.totals.p90Ttc, 13);
  assert.equal(s.closures.length, 3);
});

test("an empty range or no closures yields zeros and nulls, never NaN", () => {
  const s = m.computeSeries([issue(1, local(2026, 9, 1))], "2026-09-19", "2026-09-10");
  assert.deepEqual(s.days, []);
  assert.equal(s.totals.closed, 0);
  assert.equal(s.totals.medianTtc, null);
  assert.equal(s.totals.p90Ttc, null);
  const t = m.computeSeries([issue(1, local(2026, 9, 1))], "2026-09-18", "2026-09-19");
  assert.deepEqual(t.closed, [0, 0]);
  assert.deepEqual(t.rolling7, [0, 0]);
  assert.deepEqual(t.rollingMedian, [null, null]);
  assert.equal(Number.isNaN(t.totals.medianTtc), false);
});

test("commits are null before the window, counted after it, and the boundary day is unknown", () => {
  const since = local(2026, 9, 12, 8);   // the window starts mid-morning on the 12th
  const commits = [
    { sha: "a", date: local(2026, 9, 12, 9) },   // boundary day: unknown, not counted
    { sha: "b", date: local(2026, 9, 13, 9) },
    { sha: "c", date: local(2026, 9, 13, 17) },
    { sha: "d", date: local(2026, 9, 15, 1) },
    { sha: "e", date: null },
  ];
  const s = m.commitSeries(commits, m.dayRange("2026-09-11", "2026-09-15"), since);
  assert.deepEqual(s.known, [false, false, true, true, true]);
  assert.deepEqual(s.counts, [null, null, 2, 0, 1]);
  assert.deepEqual(s.rolling, [null, null, 2, 1, 1]);
  assert.equal(s.total, 3);
  const none = m.commitSeries(commits, m.dayRange("2026-09-11", "2026-09-12"), null);
  assert.deepEqual(none.counts, [null, null]);
  assert.deepEqual(none.rolling, [null, null]);
});

test("releases in range are filtered by local publish day and sorted newest first", () => {
  const releases = [
    { tag_name: "v1", published_at: local(2026, 9, 10) },
    { tag_name: "v2", published_at: local(2026, 9, 12, 23, 30) },
    { tag_name: "v3", published_at: local(2026, 9, 12, 8) },
    { tag_name: "v4", published_at: local(2026, 9, 16) },
    { tag_name: "v5", published_at: null },
  ];
  assert.deepEqual(m.releasesInRange(releases, "2026-09-11", "2026-09-15").map((r) => r.tag_name), ["v2", "v3"]);
});

test("breakdown rows carry every measure per project and sort by closed desc then name", () => {
  const since = local(2026, 9, 1);
  const projects = [
    { name: "zeta", repo: "o/zeta", issues: [issue(1, local(2026, 9, 10), local(2026, 9, 12)), issue(2, local(2026, 9, 11))],
      commits: [{ sha: "a", date: local(2026, 9, 12) }], commits_since: since, releases: [{ tag_name: "v1", published_at: local(2026, 9, 12) }] },
    { name: "alpha", repo: "o/alpha", issues: [issue(3, local(2026, 9, 10), local(2026, 9, 13))], commits: [], commits_since: since, releases: [] },
    { name: "beta", repo: "o/beta", issues: [issue(4, local(2026, 9, 10), local(2026, 9, 11)), issue(5, local(2026, 9, 10), local(2026, 9, 14))],
      commits: [{ sha: "b", date: local(2026, 9, 12) }, { sha: "c", date: local(2026, 9, 13) }], commits_since: since, releases: [] },
    { name: "no-origin", repo: null, issues: [] },
  ];
  const rows = m.breakdownRows(projects, "2026-09-11", "2026-09-14");
  assert.deepEqual(rows.map((r) => r.name), ["beta", "alpha", "zeta"]);
  const zeta = rows[2];
  assert.equal(zeta.openNow, 1);
  assert.equal(zeta.opened, 1);
  assert.equal(zeta.closed, 1);
  assert.equal(zeta.medianTtc, 2);
  assert.equal(zeta.commits, 1);
  assert.equal(zeta.releases, 1);
  assert.equal(rows[0].commits, 2);
});
