// Regression Console: renders the battery progress bin/handsoff_regress.py
// writes to .handsoff-regression.json, polled every second through
// /api/regression. Everything shown comes from that file; nothing is
// estimated here except the percentage, which is done / total.
const $ = (id) => document.getElementById(id);
const state = { data: null, receivedAt: null };

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function lcdText(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600); const m = Math.floor((total % 3600) / 60); const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function renderClock() {
  const data = state.data;
  const clock = $("battery-clock");
  if (!data || !data.started_at) { clock.dataset.frozen = "true"; return; }
  const end = data.finished_at ? new Date(data.finished_at).getTime() : Date.now();
  clock.querySelector(".lcd-live").textContent = lcdText((end - new Date(data.started_at).getTime()) / 1000);
  clock.dataset.frozen = data.finished_at ? "true" : "false";
}

function render(payload) {
  const data = payload.regression;
  state.data = data;
  if (!data) {
    $("battery-state").textContent = "IDLE";
    return;
  }
  const totals = data.totals || {};
  const total = totals.total ?? null;
  const done = totals.done || 0;
  const failed = (totals.failed || 0) + (totals.errors || 0);
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const running = !data.finished_at;
  $("battery-label").textContent = String(data.label || "battery").toUpperCase();
  $("battery-root").textContent = data.root || "";
  $("battery-command").textContent = data.current_command || (running ? "starting" : `finished with exit ${data.exit_code}`);
  $("battery-state").textContent = running ? "RUNNING" : (data.exit_code === 0 ? "GREEN" : `RED · ${failed} NOT PASSING`);
  $("battery-state").className = `phase-pill ${running ? "" : (data.exit_code === 0 ? "is-good" : "is-bad")}`;
  $("progress-value").textContent = total ? percent : done;
  $("progress-ring").style.setProperty("--progress", `${(total ? percent : 0) * 3.6}deg`);
  $("progress-ring").classList.toggle("is-complete", !running && data.exit_code === 0);
  $("battery-counts").textContent = total ? `${done} / ${total}` : `${done} run`;
  const current = (data.commands || []).find((c) => !c.finished_at)?.current;
  $("battery-current").textContent = running ? (current ? `now: ${current}` : "waiting for the first test") : `finished ${new Date(data.finished_at).toLocaleTimeString()}`;
  $("count-pass").textContent = totals.passed || 0;
  $("count-fail").textContent = totals.failed || 0;
  $("count-error").textContent = totals.errors || 0;
  $("count-skip").textContent = totals.skipped || 0;
  const denominator = Math.max(1, total || done);
  $("bar-pass").style.width = `${((totals.passed || 0) / denominator) * 100}%`;
  $("bar-fail").style.width = `${(failed / denominator) * 100}%`;
  $("bar-skip").style.width = `${((totals.skipped || 0) / denominator) * 100}%`;
  const commands = data.commands || [];
  $("command-count").textContent = `${commands.length} COMMAND${commands.length === 1 ? "" : "S"}`;
  $("command-list").innerHTML = commands.map((c) => {
    const bad = (c.failed || 0) + (c.errors || 0);
    const status = !c.finished_at ? "running" : (c.exit_code === 0 ? "green" : "red");
    return `<div class="command-row is-${status}"><code>${escapeHtml(c.command)}</code><span>${c.done}${c.total ? ` / ${c.total}` : ""} · ${c.passed} ok${bad ? ` · ${bad} bad` : ""}${c.skipped ? ` · ${c.skipped} skipped` : ""}</span></div>`;
  }).join("") || '<p class="attention-clear">No commands recorded.</p>';
  const failures = commands.flatMap((c) => (c.failures || []).map((f) => ({ ...f, command: c.command })));
  $("failure-count").textContent = failures.length;
  $("failure-list").innerHTML = failures.length
    ? failures.slice().reverse().map((f) => `<div class="failure-row"><span class="failure-kind ${f.kind === "ERROR" ? "is-error" : ""}">${escapeHtml(f.kind)}</span><code>${escapeHtml(f.name)}</code></div>`).join("")
    : '<p class="attention-clear">Nothing has failed.</p>';
  renderClock();
  document.title = running ? `${percent}% · Regression Console` : `${data.exit_code === 0 ? "GREEN" : "RED"} · Regression Console`;
  if (!document.body.classList.contains("is-booted")) window.setTimeout(() => document.body.classList.add("is-booted"), 30);
}

async function refresh() {
  try {
    const response = await fetch("/api/regression", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    $("battery-state").textContent = "LINK LOST";
  }
}

refresh();
window.setInterval(refresh, 1000);
window.setInterval(renderClock, 1000);
