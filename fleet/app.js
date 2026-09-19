const $ = (id) => document.getElementById(id);
let fleet = null;
let pending = null;

const ENDPOINTS = { release: "/api/release-port", close: "/api/close-run", reopen: "/api/reopen-run" };
const STATE_ORDER = ["waiting", "failed", "stalled", "running", "quiet", "complete", "closed", "orphaned"];
const STATE_LABELS = {
  waiting: "WAITING ON PILOT", failed: "FAILED", stalled: "STALLED", running: "RUNNING",
  quiet: "QUIET", complete: "COMPLETE", closed: "CLOSED", orphaned: "ORPHANED",
};

const esc = (value) => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#39;");

function relative(value) {
  if (!value) return "unknown";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

// A wall-clock stamp in the viewer's locale: the time alone when it is
// today, the date as well when it is not. #143: a frozen elapsed clock says
// how long a run took; it never said when it finished.
function stamp(value) {
  if (!value) return "";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "";
  const now = new Date();
  const sameDay = at.getFullYear() === now.getFullYear() && at.getMonth() === now.getMonth() && at.getDate() === now.getDate();
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${at.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function timing(project) {
  if (!project.started_at) return "";
  const started = `<span class="stamp" data-started-stamp="${esc(project.started_at)}">STARTED ${esc(stamp(project.started_at))}</span>`;
  const finished = project.ended_at
    ? `<span class="stamp stamp-finished" data-ended-stamp="${esc(project.ended_at)}">FINISHED ${esc(stamp(project.ended_at))}</span>`
    : "";
  return started + finished;
}

function lcdText(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600); const m = Math.floor((total % 3600) / 60); const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// LCD elapsed counters tick locally from each project's ledger anchors and
// freeze when the run is complete.
function renderClocks() {
  const now = Date.now();
  document.querySelectorAll("[data-started-at]").forEach((element) => {
    const started = new Date(element.dataset.startedAt).getTime();
    const ended = element.dataset.endedAt ? new Date(element.dataset.endedAt).getTime() : now;
    if (Number.isFinite(started)) element.querySelector(".lcd-live").textContent = lcdText((ended - started) / 1000);
  });
  const fleetClock = $("fleet-clock");
  if (fleetClock && fleet) {
    const active = fleet.projects.filter((project) => project.started_at && !project.ended_at);
    const oldest = active.length ? Math.min(...active.map((project) => new Date(project.started_at).getTime())) : null;
    fleetClock.querySelector(".lcd-live").textContent = oldest ? lcdText((now - oldest) / 1000) : "00:00:00";
    fleetClock.dataset.frozen = oldest ? "false" : "true";
  }
}

function lcd(project) {
  if (!project.started_at) return "";
  return `<span class="lcd lcd-small" data-started-at="${esc(project.started_at)}" ${project.ended_at ? `data-ended-at="${esc(project.ended_at)}" data-frozen="true"` : 'data-frozen="false"'}><span class="lcd-ghost" aria-hidden="true">88:88:88</span><span class="lcd-live">00:00:00</span></span>`;
}

function phaseRail(project) {
  const current = Number(project.phase_number) || 0;
  return `<div class="mini-rail" aria-label="Phase ${esc(current)} of 8">${Array.from({ length: 8 }, (_, index) => {
    const number = index + 1;
    const cls = number < current ? "complete" : number === current ? "active" : "";
    return `<i class="${cls}"></i>`;
  }).join("")}</div>`;
}

function projectCard(project) {
  const state = project.state || "quiet";
  const decisions = project.decisions || [];
  const title = project.initialized ? (project.feature || "No active mission") : "No active run";
  const phase = project.initialized ? `${esc(project.phase || "Uninitialized")}` : esc(project.error || "Not initialized");
  const link = project.dashboard_url
    ? `<a class="open-dashboard" href="${esc(project.dashboard_url)}" target="_blank" rel="noopener">OPEN DASHBOARD</a>`
    : `<span class="dashboard-note">${esc(project.dashboard_note)}</span>`;
  const ownerLabel = project.owner ? `PORT ${esc(project.owner.port)} · ${esc(project.owner.health)}` : "NO OWNED PORT";
  const crew = project.role ? `${esc(project.role.toUpperCase())} · ${esc(project.adapter || "-")}/${esc(project.model || "-")}` : "NO ACTIVE ROLE";
  return `<article class="project" data-state="${esc(state)}">
    <div class="project-head">
      <span class="badge">${esc(STATE_LABELS[state] || state.toUpperCase())}</span>
      ${lcd(project)}
      <span class="progress">${esc(project.progress ?? 0)}%</span>
    </div>
    <div class="project-title">${project.logo_url ? `<img class="project-logo" src="${esc(project.logo_url)}" alt="" width="44" height="44">` : ""}<div><h3>${esc(title)}</h3>
    <p class="project-name">${esc(project.name)}</p></div></div>
    ${phaseRail(project)}
    <p class="phase">${phase}</p>
    ${project.next_action ? `<p class="next">${esc(project.next_action)}</p>` : ""}
    ${decisions.length ? `<p class="decisions-flag">${decisions.length} DECISION${decisions.length === 1 ? "" : "S"} WAITING: ${esc(decisions.map((item) => item.label).join(", "))}</p>` : ""}
    <div class="project-meta"><span>${crew}</span><span>${ownerLabel}</span><span>ENGINE ${esc(project.engine_version)}</span><span>UPDATED ${esc(relative(project.updated_at || project.registered_at))}</span>${timing(project)}</div>
    <p class="root">${esc(project.root)}</p>
    <div class="project-actions">
      ${link}
      <span class="spacer"></span>
      ${project.owner ? `<button data-op="release" data-root="${esc(project.root)}">RELEASE PORT</button>` : ""}
      ${state === "closed" && project.run_closed
        ? `<button data-op="reopen" data-root="${esc(project.root)}">REOPEN RUN</button>`
        : `<button class="danger" data-op="close" data-root="${esc(project.root)}">CLOSE RUN</button>`}
    </div>
  </article>`;
}

function render(data) {
  fleet = data;
  $("synced").textContent = `SYNCED ${new Date(data.generated_at).toLocaleTimeString()}`;
  $("summary").innerHTML = STATE_ORDER.map((state) => `<article class="${data.counts[state] ? "has-some" : ""}" data-state="${state}"><span>${esc(STATE_LABELS[state] || state.toUpperCase())}</span><strong>${data.counts[state] || 0}</strong></article>`).join("");
  $("decisions").classList.toggle("hidden", !data.decisions.length);
  $("decision-count").textContent = data.decisions.length;
  $("decision-list").innerHTML = data.decisions.map((item) => `<div class="decision"><div><strong>${esc(item.label)}</strong><p>${esc(item.consequence)}</p></div><span>${esc(item.project)}</span></div>`).join("");
  const projects = data.projects.slice().sort((a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state));
  $("fleet-count").textContent = `${projects.length} PROJECT${projects.length === 1 ? "" : "S"}`;
  $("projects").innerHTML = projects.length ? projects.map(projectCard).join("")
    : '<p class="empty">No project is registered. Register one with <code>handsoff fleet register /path/to/project</code>.</p>';
  document.querySelectorAll("[data-op]").forEach((button) => button.addEventListener("click", () => openConfirm(button.dataset.op, button.dataset.root)));
  renderClocks();
}
window.setInterval(renderClocks, 1000);

function openConfirm(op, root) {
  const project = fleet.projects.find((item) => item.root === root);
  pending = { op, project };
  const active = (project.sessions || []).filter((session) => ["launching", "running"].includes(session.state));
  $("confirm-title").textContent = op === "release" ? "Release owned dashboard port" : op === "reopen" ? "Reopen closed run" : "Close run";
  $("confirm-context").textContent = [
    `Project: ${project.name}`, `Feature: ${project.feature || "none"}`, `State: ${project.state}`,
    `Owned port: ${project.owner?.port || "none"}`,
    `Managed sessions: ${active.map((session) => `${session.role}/${session.session_id}`).join(", ") || "none"}`,
    op === "close"
      ? (active.length ? "Effect: owned live sessions will be cancelled, audited, then owned ports released." : "Effect: closure is audited and owned ports are released.")
      : op === "release" ? "Effect: only the verified run-owned dashboard listener is stopped."
      : "Effect: workflow state reopens; durable history remains unchanged.",
  ].join("\n");
  $("reason-wrap").classList.toggle("hidden", op === "release");
  $("reason").value = "";
  $("confirm").showModal();
}

$("confirm").addEventListener("close", async () => {
  if ($("confirm").returnValue !== "default" || !pending) return;
  const { op, project } = pending;
  const reason = $("reason").value.trim();
  if (op !== "release" && !reason) { toast("Reason required"); pending = null; return; }
  const active = (project.sessions || []).some((session) => ["launching", "running"].includes(session.state));
  try {
    const response = await fetch(ENDPOINTS[op], {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: project.root, binding: project.binding, confirm: true, reason: reason || null, cancel_active: active }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    pending = null;
  }
});

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 5000);
}

async function refresh() {
  try {
    const response = await fetch("/api/fleet", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    toast(`Fleet telemetry interrupted: ${error.message}`);
  }
}

refresh();
const stream = new EventSource("/api/events");
stream.addEventListener("fleet", (event) => { render(JSON.parse(event.data)); $("link").textContent = "LINK: LIVE"; });
stream.onerror = () => { $("link").textContent = "LINK: RECONNECTING"; };
