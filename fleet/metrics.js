// #153: the Metrics tab. Every registered project's issues (created_at,
// closed_at) arrive from /api/metrics; the series below are computed here,
// in the page, so a filter change is a re-render and never a round trip.
//
// The first half is pure (dates in, numbers out) and is what
// tests/dashboard/fleet_metrics_series.test.js exercises through the
// module.exports guard at the bottom. The second half renders: hand-built
// SVG through createElementNS with attributes and classes only, because the
// Fleet server's CSP allows neither inline styles nor a chart library.

const DAY_MS = 86400000;

// --- Series (pure) ----------------------------------------------------------

// A day key is the viewer's local calendar date of an instant. The end of a
// day is the last millisecond before the NEXT local midnight, built from
// local year, month and day so a DST day is 23 or 25 hours long and still
// ends at midnight.
function dayKey(instant) {
  const at = instant instanceof Date ? instant : new Date(instant);
  const month = String(at.getMonth() + 1).padStart(2, "0");
  const day = String(at.getDate()).padStart(2, "0");
  return `${at.getFullYear()}-${month}-${day}`;
}

function parseKey(key) {
  const [year, month, day] = key.split("-").map(Number);
  return { year, month: month - 1, day };
}

function startOfDay(key) {
  const { year, month, day } = parseKey(key);
  return new Date(year, month, day);
}

function endOfDay(key) {
  const { year, month, day } = parseKey(key);
  return new Date(new Date(year, month, day + 1).getTime() - 1);
}

function addDays(key, count) {
  const { year, month, day } = parseKey(key);
  return dayKey(new Date(year, month, day + count));
}

// Every local day from `from` to `to`, both inclusive; empty when reversed.
function dayRange(from, to) {
  const days = [];
  if (!from || !to || from > to) return days;
  for (let key = from; key <= to; key = addDays(key, 1)) days.push(key);
  return days;
}

// A custom range in local dates: a missing `from` is the earliest
// created_at of the issues, a missing `to` is today, a reversed pair swaps.
function normalizeRange(from, to, issues, today) {
  let start = from || null;
  let end = to || dayKey(today || new Date());
  if (!start) {
    const earliest = issues.reduce((min, issue) => {
      const key = issue.created_at ? dayKey(issue.created_at) : null;
      return key && (!min || key < min) ? key : min;
    }, null);
    start = earliest || end;
  }
  if (start > end) [start, end] = [end, start];
  return { from: start, to: end };
}

function presetRange(preset, today) {
  const end = dayKey(today || new Date());
  if (preset === "today") return { from: end, to: end };
  if (preset === "7") return { from: addDays(end, -6), to: end };
  if (preset === "30") return { from: addDays(end, -29), to: end };
  if (preset === "90") return { from: addDays(end, -89), to: end };
  return { from: null, to: end };
}

function median(values) {
  if (!values.length) return null;
  const sorted = values.slice().sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

// Nearest-rank percentile: the value at rank ceil(p * n), 1-based.
function percentile(values, p) {
  if (!values.length) return null;
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)];
}

function daysToClose(issue) {
  return (new Date(issue.closed_at).getTime() - new Date(issue.created_at).getTime()) / DAY_MS;
}

// #156: a local day is known for commits only when it starts at or after the
// collector's window start; the day containing commits_since is unknown, so
// it never shows a partial count as if it were the whole day.
function commitSeries(commits, days, since) {
  const sinceMs = since ? new Date(since).getTime() : null;
  const known = days.map((key) => sinceMs !== null && startOfDay(key).getTime() >= sinceMs);
  const index = new Map(days.map((key, position) => [key, position]));
  const counts = days.map((_, position) => (known[position] ? 0 : null));
  for (const commit of commits) {
    if (!commit.date) continue;
    const key = dayKey(commit.date);
    if (index.has(key) && known[index.get(key)]) counts[index.get(key)] += 1;
  }
  const rolling = days.map((_, position) => {
    const start = Math.max(0, position - 6);
    const slice = counts.slice(start, position + 1).filter((value) => value !== null);
    return slice.length ? slice.reduce((sum, value) => sum + value, 0) / slice.length : null;
  });
  return { counts, rolling, known, total: counts.reduce((sum, value) => sum + (value || 0), 0) };
}

function releasesInRange(releases, from, to) {
  return releases
    .filter((release) => release.published_at && dayKey(release.published_at) >= from && dayKey(release.published_at) <= to)
    .sort((a, b) => (a.published_at < b.published_at ? 1 : a.published_at > b.published_at ? -1 : 0));
}

// The whole computation for one slice: `issues` already filtered by project.
// `commits` (with `since`) and `releases` are optional so issue-only callers
// keep working.
function computeSeries(issues, from, to, commits = [], since = null, releases = []) {
  const days = dayRange(from, to);
  const index = new Map(days.map((key, position) => [key, position]));
  const opened = days.map(() => 0);
  const closed = days.map(() => 0);
  const closedByDay = days.map(() => []);
  const closures = [];
  for (const issue of issues) {
    if (!issue.created_at) continue;
    const createdKey = dayKey(issue.created_at);
    if (index.has(createdKey)) opened[index.get(createdKey)] += 1;
    if (issue.closed_at) {
      const closedKey = dayKey(issue.closed_at);
      if (index.has(closedKey)) {
        const position = index.get(closedKey);
        closed[position] += 1;
        const ttc = daysToClose(issue);
        closedByDay[position].push(ttc);
        closures.push({ issue, day: closedKey, ttc });
      }
    }
  }
  const rolling7 = days.map((_, position) => {
    const start = Math.max(0, position - 6);
    const slice = closed.slice(start, position + 1);
    return slice.reduce((sum, value) => sum + value, 0) / slice.length;
  });
  const backlog = days.map((key) => {
    const end = endOfDay(key).getTime();
    return issues.filter((issue) => issue.created_at && new Date(issue.created_at).getTime() <= end
      && (!issue.closed_at || new Date(issue.closed_at).getTime() > end)).length;
  });
  const rollingMedian = days.map((_, position) => {
    const start = Math.max(0, position - 6);
    const values = closedByDay.slice(start, position + 1).flat();
    return values.length ? median(values) : null;
  });
  const ttcValues = closures.map((item) => item.ttc);
  const commitsSeries = commitSeries(commits, days, since);
  const releaseList = releasesInRange(releases, from, to);
  return {
    days, opened, closed, rolling7, backlog, rollingMedian, closures,
    commits: commitsSeries.counts, commitsRolling: commitsSeries.rolling, commitsKnown: commitsSeries.known,
    releases: releaseList,
    totals: {
      opened: opened.reduce((sum, value) => sum + value, 0),
      closed: closed.reduce((sum, value) => sum + value, 0),
      openNow: issues.filter((issue) => issue.created_at && !issue.closed_at).length,
      medianTtc: median(ttcValues),
      p90Ttc: percentile(ttcValues, 0.9),
      commits: commitsSeries.total,
      releases: releaseList.length,
    },
  };
}

// #156: one row per project for the All view, sorted by closed desc, then name.
function breakdownRows(projects, from, to) {
  return projects
    .filter((project) => project.repo)
    .map((project) => {
      const series = computeSeries(project.issues || [], from, to, project.commits || [], project.commits_since || null, project.releases || []);
      return {
        name: project.name, repo: project.repo, openNow: series.totals.openNow, opened: series.totals.opened,
        closed: series.totals.closed, medianTtc: series.totals.medianTtc, commits: series.totals.commits,
        releases: series.totals.releases,
      };
    })
    .sort((a, b) => b.closed - a.closed || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

// --- Rendering --------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
const CHART = { width: 720, height: 220, top: 14, right: 16, bottom: 28, left: 40 };
const MAX_BAR = 24;
const BAR_GAP = 2;

function svgElement(name, attributes = {}, parent = null) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (parent) parent.appendChild(element);
  return element;
}

function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

function formatDay(key) {
  const at = startOfDay(key);
  return at.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatDays(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  if (value < 1) return `${Math.round(value * 24)} h`;
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} d`;
}

function plotFrame(svg, days, yMax, options = {}) {
  const inner = { x: CHART.left, y: CHART.top, w: CHART.width - CHART.left - CHART.right, h: CHART.height - CHART.top - CHART.bottom };
  const x = (position) => inner.x + (days.length > 1 ? (position / (days.length - 1)) * inner.w : inner.w / 2);
  const band = days.length ? inner.w / days.length : inner.w;
  const xBand = (position) => inner.x + position * band;
  const y = (value) => inner.y + inner.h - (value / yMax) * inner.h;
  const grid = svgElement("g", { class: "grid" }, svg);
  const ticks = 4;
  for (let tick = 0; tick <= ticks; tick += 1) {
    const value = (yMax / ticks) * tick;
    svgElement("line", { x1: inner.x, x2: inner.x + inner.w, y1: y(value), y2: y(value), class: "gridline" }, grid);
    const label = svgElement("text", { x: inner.x - 6, y: y(value) + 3, class: "axis-label", "text-anchor": "end" }, grid);
    label.textContent = options.formatY ? options.formatY(value) : String(Math.round(value));
  }
  // Every Nth day is labelled; the last day only when it is at least half a
  // step past the previous label, so the two never collide.
  const labelEvery = Math.max(1, Math.ceil(days.length / 8));
  days.forEach((key, position) => {
    const last = position === days.length - 1;
    const onGrid = position % labelEvery === 0;
    if (!onGrid && !(last && position % labelEvery >= labelEvery / 2)) return;
    if (onGrid && !last && days.length - 1 - position < labelEvery / 2) return;
    const label = svgElement("text", { x: xBand(position) + band / 2, y: CHART.height - 8, class: "axis-label", "text-anchor": "middle" }, grid);
    label.textContent = formatDay(key);
  });
  return { inner, x, xBand, band, y };
}

function columns(svg, frame, values, className) {
  const width = Math.min(MAX_BAR, Math.max(1, frame.band - BAR_GAP));
  const group = svgElement("g", { class: `columns ${className}` }, svg);
  values.forEach((value, position) => {
    if (value <= 0) return;
    const x = frame.xBand(position) + (frame.band - width) / 2;
    const top = frame.y(value);
    const height = frame.inner.y + frame.inner.h - top;
    // 4px rounded data-end, square at the baseline: a path, not a rect.
    const r = Math.min(4, width / 2, height);
    const d = `M${x} ${top + r} a${r} ${r} 0 0 1 ${r} -${r} h${width - 2 * r} a${r} ${r} 0 0 1 ${r} ${r} v${height - r} h-${width} z`;
    svgElement("path", { d, class: "column", "data-position": position }, group);
  });
  return group;
}

function line(svg, frame, values, className) {
  const points = values.map((value, position) => (value === null ? null : `${frame.xBand(position) + frame.band / 2},${frame.y(value)}`));
  let d = "";
  let pen = false;
  for (const point of points) {
    if (point === null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${point} `;
    pen = true;
  }
  return svgElement("path", { d: d.trim(), class: `series-line ${className}`, fill: "none" }, svg);
}

function legend(panel, entries) {
  const list = document.createElement("ul");
  list.className = "legend";
  for (const entry of entries) {
    const item = document.createElement("li");
    const key = document.createElement("i");
    key.className = `legend-key ${entry.className}`;
    const label = document.createElement("span");
    label.textContent = entry.label;
    item.append(key, label);
    list.appendChild(item);
  }
  panel.appendChild(list);
  return list;
}

// One tooltip for the page; rows are [value, label] with the value in the
// strong slot (values lead, labels follow), every cell set with textContent.
function tooltip() {
  let box = document.getElementById("chart-tooltip");
  if (!box) {
    box = document.createElement("div");
    box.id = "chart-tooltip";
    box.className = "chart-tooltip hidden";
    document.body.appendChild(box);
  }
  return {
    show(rows, clientX, clientY) {
      box.replaceChildren();
      for (const [value, label] of rows) {
        const row = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = value;
        const span = document.createElement("span");
        span.textContent = label;
        row.append(strong, span);
        box.appendChild(row);
      }
      box.classList.remove("hidden");
      // CSSOM assignment is allowed under style-src 'self'; a style attribute is not.
      if (box.style) {
        box.style.left = `${clientX + 14}px`;
        box.style.top = `${clientY + 14}px`;
      }
    },
    hide() { box.classList.add("hidden"); },
  };
}

// The crosshair finds the X: one transparent hit band per day carries the
// tooltip for every series at that day.
function hitBands(svg, frame, days, rowsFor, tip) {
  const group = svgElement("g", { class: "hit-bands" }, svg);
  const crosshair = svgElement("line", { class: "crosshair hidden", y1: frame.inner.y, y2: frame.inner.y + frame.inner.h }, svg);
  days.forEach((key, position) => {
    const band = svgElement("rect", {
      x: frame.xBand(position), y: frame.inner.y, width: Math.max(frame.band, 24), height: frame.inner.h,
      class: "hit-band", "data-day": key, tabindex: 0,
    }, group);
    const show = (event) => {
      const centre = frame.xBand(position) + frame.band / 2;
      crosshair.setAttribute("x1", centre);
      crosshair.setAttribute("x2", centre);
      crosshair.classList.remove("hidden");
      tip.show([[formatDay(key), ""], ...rowsFor(position)], event.clientX || 0, event.clientY || 0);
    };
    const hide = () => { crosshair.classList.add("hidden"); tip.hide(); };
    band.addEventListener("pointermove", show);
    band.addEventListener("focus", show);
    band.addEventListener("pointerleave", hide);
    band.addEventListener("blur", hide);
  });
}

function chartPanel(title, subtitle) {
  const panel = document.createElement("section");
  panel.className = "panel chart-panel";
  panel.id = `chart-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  const head = document.createElement("div");
  head.className = "section-head";
  const copy = document.createElement("div");
  const label = document.createElement("p");
  label.className = "panel-label";
  label.textContent = title;
  const h2 = document.createElement("h2");
  h2.textContent = subtitle;
  copy.append(label, h2);
  head.appendChild(copy);
  panel.appendChild(head);
  const svg = svgElement("svg", { viewBox: `0 0 ${CHART.width} ${CHART.height}`, class: "chart", role: "img", "aria-label": subtitle }, panel);
  return { panel, svg };
}

function renderClosedChart(series, tip) {
  const { panel, svg } = chartPanel("THROUGHPUT", "Closed per day");
  const frame = plotFrame(svg, series.days, niceMax(Math.max(...series.closed, 1)));
  columns(svg, frame, series.closed, "series-closed");
  line(svg, frame, series.rolling7, "series-rolling");
  legend(panel, [{ className: "key-closed", label: "Closed" }, { className: "key-rolling", label: "7-day rolling average" }]);
  hitBands(svg, frame, series.days, (position) => [
    [`${series.closed[position]}`, "closed"], [series.rolling7[position].toFixed(1), "7-day average"],
  ], tip);
  return panel;
}

function renderOpenedChart(series, tip) {
  const { panel, svg } = chartPanel("INTAKE", "Opened per day");
  const frame = plotFrame(svg, series.days, niceMax(Math.max(...series.opened, 1)));
  columns(svg, frame, series.opened, "series-opened");
  hitBands(svg, frame, series.days, (position) => [[`${series.opened[position]}`, "opened"]], tip);
  return panel;
}

function renderBacklogChart(series, tip) {
  const { panel, svg } = chartPanel("BACKLOG", "Open issues per day");
  const frame = plotFrame(svg, series.days, niceMax(Math.max(...series.backlog, 1)));
  line(svg, frame, series.backlog, "series-backlog");
  const last = series.backlog.length - 1;
  if (last >= 0) {
    svgElement("circle", { cx: frame.xBand(last) + frame.band / 2, cy: frame.y(series.backlog[last]), r: 4, class: "end-marker series-backlog" }, svg);
    const label = svgElement("text", { x: frame.xBand(last) + frame.band / 2 - 8, y: frame.y(series.backlog[last]) - 8, class: "value-label", "text-anchor": "end" }, svg);
    label.textContent = String(series.backlog[last]);
  }
  hitBands(svg, frame, series.days, (position) => [[`${series.backlog[position]}`, "open"]], tip);
  return panel;
}

function renderTtcChart(series, tip) {
  const { panel, svg } = chartPanel("TIME TO CLOSE", "Days to close, per closed issue");
  const values = series.closures.map((item) => item.ttc);
  const frame = plotFrame(svg, series.days, niceMax(Math.max(...values, ...series.rollingMedian.filter((v) => v !== null), 1)), { formatY: (v) => `${Math.round(v)}d` });
  line(svg, frame, series.rollingMedian, "series-rolling");
  const dots = svgElement("g", { class: "dots" }, svg);
  const index = new Map(series.days.map((key, position) => [key, position]));
  series.closures.forEach((item) => {
    const position = index.get(item.day);
    const cx = frame.xBand(position) + frame.band / 2;
    const cy = frame.y(item.ttc);
    const hit = svgElement("circle", { cx, cy, r: 12, class: "dot-hit", tabindex: 0 }, dots);
    svgElement("circle", { cx, cy, r: 4, class: "dot series-closed" }, dots);
    const show = (event) => tip.show([
      [formatDays(item.ttc), `#${item.issue.number} ${item.issue.title || ""}`.trim()],
      [formatDay(item.day), "closed"],
    ], event.clientX || 0, event.clientY || 0);
    hit.addEventListener("pointermove", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", () => tip.hide());
    hit.addEventListener("blur", () => tip.hide());
  });
  legend(panel, [{ className: "key-closed-dot", label: "Closed issue" }, { className: "key-rolling", label: "7-day rolling median" }]);
  return panel;
}

function renderCommitsChart(series, tip, since) {
  const { panel, svg } = chartPanel("DELIVERY", "Commits per day");
  const known = series.commits.filter((value) => value !== null);
  const frame = plotFrame(svg, series.days, niceMax(Math.max(...known, 1)));
  columns(svg, frame, series.commits.map((value) => value || 0), "series-commits");
  line(svg, frame, series.commitsRolling, "series-rolling");
  legend(panel, [{ className: "key-closed", label: "Commits" }, { className: "key-rolling", label: "7-day rolling average" }]);
  if (series.commitsKnown.some((flag) => !flag)) {
    const note = document.createElement("p");
    note.className = "chart-note";
    note.textContent = since ? `Commits known since ${formatDay(dayKey(since))}; earlier days are blank, not zero.` : "Commits not collected yet.";
    panel.appendChild(note);
  }
  hitBands(svg, frame, series.days, (position) => [
    [series.commits[position] === null ? "unknown" : `${series.commits[position]}`, "commits"],
    [series.commitsRolling[position] === null ? "unknown" : series.commitsRolling[position].toFixed(1), "7-day average"],
  ], tip);
  return panel;
}

function tableInto(container, headers, rows, className) {
  const table = document.createElement("table");
  table.className = className;
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of headers) {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  for (const values of rows) {
    const row = document.createElement("tr");
    for (const value of values) {
      const cell = document.createElement("td");
      if (value && typeof value === "object" && value.href) {
        const link = document.createElement("a");
        link.href = value.href;
        link.textContent = value.text;
        link.target = "_blank";
        link.rel = "noopener";
        cell.appendChild(link);
      } else {
        cell.textContent = String(value);
      }
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
  table.appendChild(body);
  container.appendChild(table);
  return table;
}

function renderReleasesPanel(series, projectsById) {
  const panel = document.createElement("section");
  panel.className = "panel releases-panel";
  const head = document.createElement("div");
  head.className = "section-head";
  const copy = document.createElement("div");
  const label = document.createElement("p");
  label.className = "panel-label";
  label.textContent = "RELEASES";
  const h2 = document.createElement("h2");
  h2.textContent = `${series.releases.length} release${series.releases.length === 1 ? "" : "s"} in range`;
  copy.append(label, h2);
  head.appendChild(copy);
  panel.appendChild(head);
  if (!series.releases.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No release published in this range.";
    panel.appendChild(empty);
    return panel;
  }
  const scroll = document.createElement("div");
  scroll.className = "scroll-wrap";
  tableInto(scroll, ["PROJECT", "TAG", "NAME", "PUBLISHED"], series.releases.map((release) => [
    release.project || "", { href: release.html_url || "#", text: release.tag_name || "" }, release.name || "",
    formatDay(dayKey(release.published_at)),
  ]), "metrics-table releases-table");
  panel.appendChild(scroll);
  return panel;
}

function renderBreakdown(container, rows) {
  container.replaceChildren();
  const head = document.createElement("div");
  head.className = "section-head";
  const copy = document.createElement("div");
  const label = document.createElement("p");
  label.className = "panel-label";
  label.textContent = "PER PROJECT";
  const h2 = document.createElement("h2");
  h2.textContent = "Every repository, this range";
  copy.append(label, h2);
  head.appendChild(copy);
  container.appendChild(head);
  tableInto(container, ["PROJECT", "REPO", "OPEN NOW", "OPENED", "CLOSED", "MEDIAN TTC", "COMMITS", "RELEASES"], rows.map((row) => [
    row.name, row.repo, row.openNow, row.opened, row.closed, formatDays(row.medianTtc), row.commits, row.releases,
  ]), "metrics-table breakdown-table");
}

function renderKpis(container, series) {
  container.replaceChildren();
  const tiles = [
    ["CLOSED IN RANGE", String(series.totals.closed), "closed"],
    ["OPENED IN RANGE", String(series.totals.opened), "opened"],
    ["OPEN NOW", String(series.totals.openNow), "open"],
    ["MEDIAN TIME TO CLOSE", formatDays(series.totals.medianTtc), "median"],
    ["P90 TIME TO CLOSE", formatDays(series.totals.p90Ttc), "p90"],
    ["COMMITS IN RANGE", String(series.totals.commits), "commits"],
    ["RELEASES IN RANGE", String(series.totals.releases), "releases"],
  ];
  for (const [label, value, key] of tiles) {
    const tile = document.createElement("article");
    tile.className = "has-some";
    tile.setAttribute("data-kpi", key);
    const span = document.createElement("span");
    span.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    tile.append(span, strong);
    container.appendChild(tile);
  }
}

function renderTable(container, series) {
  container.replaceChildren();
  const table = document.createElement("table");
  table.className = "metrics-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["DATE", "OPENED", "CLOSED", "BACKLOG", "COMMITS"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  series.days.forEach((key, position) => {
    const row = document.createElement("tr");
    const commits = series.commits[position] === null ? "" : series.commits[position];
    for (const value of [key, series.opened[position], series.closed[position], series.backlog[position], commits]) {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.appendChild(cell);
    }
    body.appendChild(row);
  });
  table.appendChild(body);
  container.appendChild(table);
}

// --- Page wiring ------------------------------------------------------------

const state = { data: null, project: "all", preset: "30", from: "", to: "" };

function selectedProjects() {
  return (state.data?.projects || []).filter((project) => state.project === "all" || project.root === state.project);
}

function selectedIssues() {
  return selectedProjects().flatMap((project) => project.issues || []);
}

// Commits are only comparable across projects on days every selected project
// knows; the earliest commits_since among them is the shared window start.
function selectedCommits(projects) {
  const sinces = projects.map((project) => project.commits_since).filter(Boolean).sort();
  const since = sinces.length ? sinces[sinces.length - 1] : null;
  return { commits: projects.flatMap((project) => project.commits || []), since };
}

function currentRange(issues) {
  if (state.preset === "custom") return normalizeRange(state.from, state.to, issues);
  const preset = presetRange(state.preset);
  return preset.from ? preset : normalizeRange(null, preset.to, issues);
}

function renderAll() {
  const $ = (id) => document.getElementById(id);
  if (!state.data) return;
  const projects = selectedProjects();
  const issues = selectedIssues();
  const range = currentRange(issues);
  const { commits, since } = selectedCommits(projects);
  const releases = projects.flatMap((project) => (project.releases || []).map((release) => ({ ...release, project: project.name })));
  const series = computeSeries(issues, range.from, range.to, commits, since, releases);
  const tip = tooltip();
  renderKpis($("kpis"), series);
  const charts = $("charts");
  charts.replaceChildren(renderClosedChart(series, tip), renderOpenedChart(series, tip), renderBacklogChart(series, tip),
    renderTtcChart(series, tip), renderCommitsChart(series, tip, since));
  $("releases").replaceChildren(...renderReleasesPanel(series).childNodes);
  const breakdown = $("breakdown");
  breakdown.classList.toggle("hidden", state.project !== "all");
  if (state.project === "all") renderBreakdown(breakdown, breakdownRows(state.data.projects || [], range.from, range.to));
  renderTable($("table-wrap"), series);
  $("range-label").textContent = `${formatDay(range.from)} to ${formatDay(range.to)} · ${series.days.length} DAYS`;
  const stale = (state.data.projects || []).filter((project) => project.error);
  // #158: the GitHub budget beside the refresh time, when the collector has seen the headers.
  const rate = state.data.rate_limit;
  const budget = rate && Number.isInteger(rate.remaining) ? ` · GITHUB BUDGET ${rate.remaining} OF ${rate.limit}` : "";
  $("source-note").textContent = (stale.length
    ? `${issues.length} ISSUES · ${stale.length} PROJECT${stale.length === 1 ? "" : "S"} WITH ERRORS: ${stale.map((p) => `${p.name} (${p.error})`).join("; ")}`
    : `${issues.length} ISSUES · REFRESHED ${state.data.refreshed_at ? new Date(state.data.refreshed_at).toLocaleTimeString() : "FROM CACHE"}`) + budget;
  return series;
}

function bindControls() {
  const $ = (id) => document.getElementById(id);
  const select = $("project");
  select.addEventListener("change", () => { state.project = select.value; renderAll(); });
  document.querySelectorAll("[data-preset]").forEach((button) => button.addEventListener("click", () => {
    state.preset = button.dataset.preset;
    document.querySelectorAll("[data-preset]").forEach((other) => other.classList.toggle("active", other === button));
    $("custom-range").classList.toggle("hidden", state.preset !== "custom");
    renderAll();
  }));
  $("from").addEventListener("change", () => { state.from = $("from").value; renderAll(); });
  $("to").addEventListener("change", () => { state.to = $("to").value; renderAll(); });
  $("toggle-table").addEventListener("click", () => {
    const wrap = $("table-wrap");
    wrap.classList.toggle("hidden");
    $("toggle-table").textContent = wrap.classList.contains("hidden") ? "TABLE" : "HIDE TABLE";
  });
}

function fillProjects(data) {
  const select = document.getElementById("project");
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "All projects";
  select.appendChild(all);
  for (const project of data.projects.filter((item) => item.repo)) {
    const option = document.createElement("option");
    option.value = project.root;
    option.textContent = `${project.name} (${project.repo})`;
    select.appendChild(option);
  }
  select.value = state.project;
}

async function load() {
  const $ = (id) => document.getElementById(id);
  try {
    const response = await fetch("/api/metrics", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    fillProjects(state.data);
    $("synced").textContent = `SYNCED ${new Date(state.data.generated_at).toLocaleTimeString()}`;
    $("link").textContent = "LINK: LIVE";
    renderAll();
  } catch (error) {
    $("link").textContent = "LINK: OFFLINE";
    $("source-note").textContent = `Metrics unavailable: ${error.message}`;
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    dayKey, startOfDay, endOfDay, addDays, dayRange, normalizeRange, presetRange, median, percentile, daysToClose,
    computeSeries, commitSeries, releasesInRange, breakdownRows,
    renderAll, renderKpis, renderTable, renderClosedChart, renderOpenedChart, renderBacklogChart, renderTtcChart,
    renderCommitsChart, renderReleasesPanel, renderBreakdown, state, load, bindControls, fillProjects, MAX_BAR, BAR_GAP,
  };
} else {
  bindControls();
  load();
  window.setInterval(load, 60000);
}
