const $ = (id) => document.getElementById(id);
let fleet = null;
let pending = null;

const ENDPOINTS = { release: "/api/release-port", close: "/api/close-run", reopen: "/api/reopen-run", forget: "/api/forget" };
const STATE_ORDER = ["waiting", "failed", "offline", "stalled", "running", "quiet", "complete", "closed", "idle", "orphaned"];
// #150/#154: only a run that is moving belongs in the grid; finished runs and
// projects with no run at all sit in the collapsed section.
const FINISHED_STATES = new Set(["complete", "closed", "idle", "orphaned"]);
const STATE_LABELS = {
  waiting: "WAITING ON PILOT", failed: "FAILED", stalled: "STALLED", running: "RUNNING",
  quiet: "QUIET", offline: "DASHBOARD OFFLINE", complete: "COMPLETE", closed: "CLOSED", idle: "NO RUN", orphaned: "ORPHANED",
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

// #152: the GitHub and Beakon signals ride on each card as one strip. The
// server fills them from a cache on its own clock, so every line says how
// old it is; a collector error keeps the last good numbers beside its text.
function githubLine(github) {
  if (!github) return `<span class="signal signal-pending"><b>GITHUB</b> PENDING</span>`;
  if (github.open_issues === null || github.open_issues === undefined) {
    const label = github.error === "GitHub is not configured" ? "NOT CONFIGURED" : `ERROR: ${github.error || "no data"}`;
    return `<span class="signal signal-error"><b>GITHUB</b> ${esc(label)}</span>`;
  }
  const release = github.latest_release
    ? `${esc(github.latest_release.tag)} ${esc(relative(github.latest_release.published_at))}`
    : "NO RELEASE";
  const parts = [`${esc(github.open_issues)} ISSUES`, `${esc(github.open_prs)} PRS`, release, `CACHED ${esc(relative(github.fetched_at))}`];
  const error = github.error ? ` <em class="signal-error-text">ERROR: ${esc(github.error)}</em>` : "";
  return `<span class="signal${github.error ? " signal-stale" : ""}"><b>GITHUB</b> ${parts.join(" · ")}${error}</span>`;
}

function beakonLine(beakon) {
  if (!beakon) return "";
  const last = beakon.last
    ? `LAST ${esc(String(beakon.last.outcome || "unknown").toUpperCase())} ${esc(relative(beakon.last.finished_at))}`
    : "NO BEAMS YET";
  return `<span class="signal signal-beakon"><b>BEAKON</b> ${esc(beakon.in_flight)} IN FLIGHT · ${last} · CACHED ${esc(relative(beakon.fetched_at))}</span>`;
}

function signalsStrip(project) {
  return `<div class="signals">${githubLine(project.github)}${beakonLine(project.beakon)}</div>`;
}

// #161: the badge names the engine the Fleet server runs; a card whose
// project engine differs is marked so drift is visible where it matters.
function fleetEngineVersion() {
  return fleet?.engine?.version && fleet.engine.version !== "unknown" ? fleet.engine.version : null;
}

function renderEngineBadge(data) {
  const badge = $("engine-badge");
  if (!badge) return;
  const engine = data?.engine || {};
  badge.textContent = engineBadgeLabel(engine);
  badge.title = engineBadgeTitle(engine);
  badge.classList.toggle("install-blocked", Boolean(engine.install_blocked));
}

// #216: while any registered run has a live managed session, an engine
// install would land under it; the badge says so and names the sessions.
function engineBadgeLabel(engine) {
  const version = engine && engine.version && engine.version !== "unknown" ? engine.version : "UNKNOWN";
  const blocked = engine && engine.install_blocked;
  if (blocked && Number(blocked.count) > 0) {
    return `ENGINE ${version} (install blocked: ${blocked.count} live session${blocked.count === 1 ? "" : "s"})`;
  }
  return `ENGINE ${version}`;
}

function engineBadgeTitle(engine) {
  const base = engine && engine.source && engine.source !== "unknown" ? `Engine the Fleet server runs (${engine.source})` : "Engine the Fleet server runs";
  const blocked = engine && engine.install_blocked;
  if (blocked && Array.isArray(blocked.sessions) && blocked.sessions.length) {
    return base + "; install blocked by " + blocked.sessions.map((s) => `${s.role} ${s.session_id} on ${s.root}`).join(", ");
  }
  return base;
}

function engineMeta(project) {
  const own = project.engine_version || "unknown";
  const fleetVersion = fleetEngineVersion();
  const drift = fleetVersion && own !== "unknown" && own !== fleetVersion;
  return { text: `ENGINE ${esc(own)}${drift ? ` (fleet ${esc(fleetVersion)})` : ""}`, drift };
}

// #186: which host drives the run, beside the project name; only from an
// actor prefix the engine recorded, so two lanes from two hosts are told
// apart at a glance and an unrecorded host reads nothing rather than a guess.
function hostTag(project) {
  const family = project && (project.host === "claude" || project.host === "codex") ? project.host : null;
  return family ? ` <span class="host-tag" data-family="${esc(family)}">HOST ${esc(family.toUpperCase())}</span>` : "";
}

// #194: when the ball is with the host, the card says which host and how long.
function hostWaitTag(project) {
  const wait = project && project.host_wait;
  if (!wait || typeof wait !== "object") return "";
  const seconds = Number(wait.silent_seconds);
  const words = !Number.isFinite(seconds) || seconds < 0 ? "" : seconds < 3600 ? `${Math.floor(seconds / 60)} min` : `${Math.floor(seconds / 3600)} h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")} m`;
  return `<span class="host-wait">WAITING ON HOST ${esc(String(wait.family || "unknown").toUpperCase())}${words ? " " + esc(words) : ""}</span>`;
}

// #193: the card's phase line names how long the machine slept in this phase.
function asleepSuffix(project) {
  const seconds = Number(project && project.phase_asleep_seconds);
  if (!Number.isFinite(seconds) || seconds < 60) return "";
  const words = seconds < 3600 ? `${Math.floor(seconds / 60)} min` : `${Math.floor(seconds / 3600)} h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")} m`;
  return ` <span class="asleep">asleep ${esc(words)}</span>`;
}

function projectCard(project) {
  const state = project.state || "quiet";
  const engine = engineMeta(project);
  const decisions = project.decisions || [];
  const title = project.initialized ? (project.feature || "No active mission") : "No active run";
  const phase = project.initialized ? `${esc(project.phase || "Uninitialized")}` : esc(project.error || "Not initialized");
  // #207: a root that is gone says since when; the card is forgotten on the next pass.
  const missing = state === "orphaned" && project.missing_since
    ? ` · <span class="missing">MISSING since ${esc(stamp(project.missing_since))}, forgotten on the next pass</span>` : "";
  const link = project.dashboard_url
    ? `<a class="open-dashboard" href="${esc(project.dashboard_url)}" target="_blank" rel="noopener">OPEN DASHBOARD</a>`
    : `<span class="dashboard-note">${esc(project.dashboard_note)}</span>`;
  const ownerLabel = project.owner ? `PORT ${esc(project.owner.port)} · ${esc(project.owner.health)}` : "NO OWNED PORT";
  const crew = project.role ? `${esc(project.role.toUpperCase())} · ${esc(project.adapter || "-")}/${esc(project.model || "-")}` : "NO ACTIVE ROLE";
  return `<article class="project${engine.drift ? " engine-drift" : ""}" data-state="${esc(state)}">
    <div class="project-head">
      <span class="badge">${esc(STATE_LABELS[state] || state.toUpperCase())}</span>
      ${lcd(project)}
      <span class="progress">${esc(project.progress ?? 0)}%</span>
    </div>
    <div class="project-title">${project.logo_url ? `<img class="project-logo" src="${esc(project.logo_url)}" alt="" width="44" height="44">` : ""}<div><h3>${esc(title)}</h3>
    <p class="project-name">${esc(project.name)}${hostTag(project)}</p></div></div>
    ${phaseRail(project)}
    <p class="phase">${phase}${asleepSuffix(project)}${missing}</p>
    ${project.next_action ? `<p class="next">${esc(project.next_action)}</p>` : ""}
    ${Array.isArray(project.claimed_twice) && project.claimed_twice.length ? `<p class="claimed-twice">CLAIMED TWICE: ${esc(project.claimed_twice.map((n) => "#" + n).join(", "))} is also listed by another live run</p>` : ""}
    ${decisions.length ? `<p class="decisions-flag">${decisions.length} DECISION${decisions.length === 1 ? "" : "S"} WAITING: ${esc(decisions.map((item) => item.label).join(", "))}</p>` : ""}
    ${signalsStrip(project)}
    <div class="project-meta"><span>${crew}</span>${hostWaitTag(project)}<span>${ownerLabel}</span><span class="engine-meta">${engine.text}</span><span>UPDATED ${esc(relative(project.updated_at || project.registered_at))}</span>${timing(project)}</div>
    <p class="root">${esc(project.root)}</p>
    <div class="project-actions">
      ${link}
      <span class="spacer"></span>
      ${project.owner ? `<button data-op="release" data-root="${esc(project.root)}">RELEASE PORT</button>` : ""}
      ${state === "orphaned"
        ? `<button class="danger" data-op="forget" data-root="${esc(project.root)}">FORGET</button>`
        : state === "closed" && project.run_closed
        ? `<button data-op="reopen" data-root="${esc(project.root)}">REOPEN RUN</button>`
        : `<button class="danger" data-op="close" data-root="${esc(project.root)}">CLOSE RUN</button>`}
    </div>
  </article>`;
}

function render(data) {
  fleet = data;
  renderEngineBadge(data);
  $("synced").textContent = `SYNCED ${new Date(data.generated_at).toLocaleTimeString()}`;
  $("summary").innerHTML = STATE_ORDER.map((state) => `<article class="${data.counts[state] ? "has-some" : ""}" data-state="${state}"><span>${esc(STATE_LABELS[state] || state.toUpperCase())}</span><strong>${data.counts[state] || 0}</strong></article>`).join("");
  $("decisions").classList.toggle("hidden", !data.decisions.length);
  $("decision-count").textContent = data.decisions.length;
  $("decision-list").innerHTML = data.decisions.map((item) => `<div class="decision"><div><strong>${esc(item.label)}</strong><p>${esc(item.consequence)}</p></div><span>${esc(item.project)}</span></div>`).join("");
  const projects = data.projects.slice().sort((a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state));
  // #150: finished runs (complete, closed) sit in their own collapsed
  // section so the grid shows only what is ongoing.
  const ongoing = projects.filter((project) => !FINISHED_STATES.has(project.state));
  const finished = projects.filter((project) => FINISHED_STATES.has(project.state));
  $("fleet-count").textContent = `${ongoing.length} ONGOING · ${projects.length} REGISTERED`;
  $("projects").innerHTML = ongoing.length ? ongoing.map(projectCard).join("")
    : projects.length ? '<p class="empty">No ongoing mission. Every registered project is complete, closed or without a run.</p>'
    : '<p class="empty">No project is registered. Register one with <code>handsoff fleet register /path/to/project</code>.</p>';
  const finishedSection = $("finished-runs");
  finishedSection.classList.toggle("hidden", finished.length === 0);
  $("finished-count").textContent = `${finished.length} RUN${finished.length === 1 ? "" : "S"}`;
  $("finished-projects").innerHTML = finished.map(projectCard).join("");
  document.querySelectorAll("[data-op]").forEach((button) => button.addEventListener("click", () => openConfirm(button.dataset.op, button.dataset.root)));
  renderClocks();
}
window.setInterval(renderClocks, 1000);
(function rememberFinishedDisclosure() {
  const section = $("finished-runs");
  if (!section) return;
  try { section.open = localStorage.getItem("fleet.finished.open") === "1"; } catch (error) { /* per-browser convenience only */ }
  section.addEventListener("toggle", () => {
    try { localStorage.setItem("fleet.finished.open", section.open ? "1" : "0"); } catch (error) { /* ignore */ }
  });
})();

function openConfirm(op, root) {
  const project = fleet.projects.find((item) => item.root === root);
  pending = { op, project };
  const active = (project.sessions || []).filter((session) => ["launching", "running"].includes(session.state));
  $("confirm-title").textContent = op === "release" ? "Release owned dashboard port" : op === "reopen" ? "Reopen closed run" : op === "forget" ? "Forget a root that is gone" : "Close run";
  $("confirm-context").textContent = [
    `Project: ${project.name}`, `Feature: ${project.feature || "none"}`, `State: ${project.state}`,
    `Owned port: ${project.owner?.port || "none"}`,
    `Managed sessions: ${active.map((session) => `${session.role}/${session.session_id}`).join(", ") || "none"}`,
    op === "close"
      ? (active.length ? "Effect: owned live sessions will be cancelled, audited, then owned ports released." : "Effect: closure is audited and owned ports are released.")
      : op === "release" ? "Effect: only the verified run-owned dashboard listener is stopped."
      : op === "forget" ? "Effect: the register entry is removed and one line goes to the fleet log; nothing on disk is touched (there is nothing there)."
      : "Effect: workflow state reopens; durable history remains unchanged.",
  ].join("\n");
  $("reason-wrap").classList.toggle("hidden", op === "release" || op === "forget");
  $("reason").value = "";
  $("confirm").showModal();
}

$("confirm").addEventListener("close", async () => {
  if ($("confirm").returnValue !== "default" || !pending) return;
  const { op, project } = pending;
  const reason = $("reason").value.trim();
  if (op !== "release" && op !== "forget" && !reason) { toast("Reason required"); pending = null; return; }
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
    fleetOfflineSince = null;
    renderOffline();
  } catch (error) {
    if (!fleetOfflineSince) {
      fleetOfflineSince = new Date();
      toast(`Fleet telemetry interrupted: ${error.message}`);
    }
    renderOffline();
  }
}

let fleetOfflineSince = null;
function renderOffline() {
  document.body.classList.toggle("is-offline", Boolean(fleetOfflineSince));
  // Cancel what is already running too: the stylesheet rule stops new
  // animations, but Chrome keeps one alive inside a closed details element.
  if (fleetOfflineSince && typeof document.getAnimations === "function") document.getAnimations().forEach((animation) => animation.cancel());
  const banner = $("offline-banner");
  if (!banner) return;
  banner.textContent = fleetOfflineSince
    ? `DASHBOARD OFFLINE since ${fleetOfflineSince.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}: the server on this port is not answering`
    : "";
  banner.classList.toggle("hidden", !fleetOfflineSince);
}

refresh();
// #151: the poll is what notices a dead server; the stream only pushes
// changes while it is up, so its error also triggers one rate-limited
// refresh, exactly like the run dashboard.
let lastStreamRefresh = 0;
window.setInterval(refresh, 5000);
const stream = new EventSource("/api/events");
stream.addEventListener("fleet", (event) => { render(JSON.parse(event.data)); $("link").textContent = "LINK: LIVE"; });
stream.onerror = () => {
  $("link").textContent = "LINK: RECONNECTING";
  if (Date.now() - lastStreamRefresh >= 5000) {
    lastStreamRefresh = Date.now();
    refresh();
  }
};
