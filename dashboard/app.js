const $ = (id) => document.getElementById(id);
const state = {
  lastGenerated: null,
  failures: 0,
  feature: "Handsoff",
  inputRequired: false,
  inputKind: null,
  alertSignature: null,
  titleFlip: false,
  refreshInFlight: false,
  refreshQueued: false,
  eventStream: null,
  settings: null,
  settingsDirty: false,
};
const AGENT_ROLES = ["architect", "supervisor", "implementer", "reviewer"];
const ALLOWED_ADAPTERS = ["codex", "claude"];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function relativeTime(value) {
  if (!value) return "Never";
  const then = new Date(value);
  if (Number.isNaN(then.getTime())) return "Unknown time";
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (Math.abs(seconds) < 10) return "just now";
  if (Math.abs(seconds) < 60) return `${Math.abs(seconds)}s ago`;
  const minutes = Math.round(Math.abs(seconds) / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric", year: then.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
}

function cleanKind(value) {
  return String(value || "event").replaceAll("_", " ");
}

function stateClass(element, value) {
  element.classList.remove("is-good", "is-warning", "is-bad");
  if (value) element.classList.add(value);
}

const PROVIDER_STATE_META = {
  detected: { text: "DETECTED", cssClass: "is-good" },
  requires_setup: { text: "REQUIRES SETUP", cssClass: "is-warning" },
  unavailable: { text: "UNAVAILABLE", cssClass: "is-bad" },
};

function renderProviderStatus(providers) {
  const list = $("provider-status");
  if (!list) return;
  list.replaceChildren();
  for (const [id, info] of Object.entries(providers)) {
    const meta = PROVIDER_STATE_META[info?.state] || { text: String(info?.state || "UNKNOWN").toUpperCase(), cssClass: "" };
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = info?.label || id;
    const badge = document.createElement("span");
    badge.className = `provider-state ${meta.cssClass}`;
    badge.textContent = meta.text;
    item.append(label, badge);
    list.append(item);
  }
}

function populateAgentSettings() {
  if (!state.settings) return;
  for (const role of AGENT_ROLES) {
    const select = $(`agent-${role}`);
    select.querySelectorAll("option[data-custom]").forEach((option) => option.remove());
    const profile = state.settings.profiles?.[role] || {
      adapter: state.settings.agents?.[role] || "configure-me",
      model: "default",
    };
    const value = profile.adapter;
    if (ALLOWED_ADAPTERS.includes(value)) {
      select.value = value;
    } else {
      const custom = document.createElement("option");
      custom.value = "";
      custom.textContent = `Custom · ${value} — choose replacement`;
      custom.dataset.custom = "true";
      custom.disabled = true;
      custom.selected = true;
      select.prepend(custom);
    }
    $(`agent-${role}-model`).value = profile.model || "default";
  }
  const availability = state.settings.availability || {};
  $("adapter-availability").textContent = ALLOWED_ADAPTERS.map((adapter) =>
    `${adapter === "claude" ? "Claude Code" : "Codex"}: ${availability[adapter]?.available ? "DETECTED" : "NOT DETECTED"}`
  ).join(" · ");
  renderProviderStatus(state.settings.providers || {});
  updateSettingsSaveState();
}

function updateSettingsSaveState() {
  $("settings-save").disabled = AGENT_ROLES.some((role) => {
    const model = $(`agent-${role}-model`).value;
    return !ALLOWED_ADAPTERS.includes($(`agent-${role}`).value)
      || !model || model.trim() !== model || model.length > 128 || model.startsWith("-")
      || [...model].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127);
  });
}

function closeSettings() {
  state.settingsDirty = false;
  $("settings-result").textContent = "";
  $("settings-dialog").close();
}

function openSettings() {
  populateAgentSettings();
  $("settings-dialog").showModal();
  $("agent-architect").focus();
}

async function saveAgentSettings(event) {
  event.preventDefault();
  const profiles = Object.fromEntries(AGENT_ROLES.map((role) => [role, {
    adapter: $(`agent-${role}`).value,
    model: $(`agent-${role}-model`).value,
  }]));
  updateSettingsSaveState();
  if ($("settings-save").disabled) {
    $("settings-result").textContent = "Select a valid adapter and model for every station, Pilot.";
    return;
  }
  const button = $("settings-save");
  button.disabled = true;
  $("settings-result").textContent = "Transmitting agent matrix…";
  try {
    const response = await fetch("/api/settings/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profiles),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Settings returned ${response.status}`);
    state.settings = result;
    state.settingsDirty = false;
    populateAgentSettings();
    $("settings-result").textContent = "Agent matrix confirmed. Future launches will use these exact profiles.";
    refresh();
  } catch (error) {
    $("settings-result").textContent = `Transmission rejected: ${error.message}`;
  } finally {
    updateSettingsSaveState();
  }
}

function updateAlertButton() {
  const button = $("enable-alerts");
  if (!("Notification" in window)) {
    button.classList.add("hidden");
    return;
  }
  button.classList.remove("hidden");
  if (Notification.permission === "granted") {
    button.textContent = "Tactical alerts enabled";
    button.disabled = true;
  } else if (Notification.permission === "denied") {
    button.textContent = "Tactical alerts blocked";
    button.disabled = true;
  } else {
    button.textContent = "Enable tactical alerts";
    button.disabled = false;
  }
}

async function enableDesktopAlerts() {
  if (!("Notification" in window)) return;
  await Notification.requestPermission();
  updateAlertButton();
  if (Notification.permission === "granted" && state.inputRequired) {
    new Notification("E.V.E. requests Pilot authorization", {
      body: $("input-alert-message").textContent,
      tag: "handsoff-input-required",
    });
  }
}

function renderInputAlert(inputRequest, feature) {
  const required = Boolean(inputRequest?.required);
  const message = inputRequest?.message || "Pilot authorization is required before the mission can continue.";
  const signature = required ? `${inputRequest.kind}:${message}` : null;
  state.feature = feature;
  state.inputRequired = required;
  state.inputKind = required ? inputRequest?.kind : null;
  document.body.classList.toggle("input-is-required", required);
  $("input-alert").classList.toggle("hidden", !required);
  $("input-alert-message").textContent = message;
  const approvalButton = $("design-approve");
  approvalButton.classList.toggle("hidden", state.inputKind !== "design_approval");
  if (state.inputKind === "design_approval" && signature !== state.alertSignature) {
    approvalButton.disabled = false;
    approvalButton.textContent = "AUTHORIZE DESIGN";
  }
  updateAlertButton();

  if (required && signature !== state.alertSignature && "Notification" in window && Notification.permission === "granted") {
    new Notification("E.V.E. requests Pilot authorization", {
      body: message,
      tag: "handsoff-input-required",
    });
  }
  state.alertSignature = signature;
}

async function authorizeDesign() {
  const button = $("design-approve");
  button.disabled = true;
  button.textContent = "TRANSMITTING AUTHORIZATION…";
  try {
    const response = await fetch("/api/design-approval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Authorization returned ${response.status}`);
    button.textContent = "DESIGN AUTHORIZED";
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "AUTHORIZE DESIGN";
    showError(`Authorization rejected: ${error.message}`);
  }
}

function renderPhases(phases) {
  $("phase-rail").innerHTML = phases.map((phase) => `
    <div class="phase-node ${escapeHtml(phase.state)}">
      <strong>0${escapeHtml(phase.number)}</strong>
      <span>${escapeHtml(phase.name)}</span>
    </div>`).join("");
}

function renderCriteria(criteria) {
  $("criteria-list").innerHTML = criteria.length ? criteria.map((criterion, index) => `
    <div class="criterion">
      <span class="criterion-index">${String(index + 1).padStart(2, "0")}</span>
      <div>
        <h3>${escapeHtml(criterion.requirement)}</h3>
        <div class="criterion-meta">
          <span>${escapeHtml(criterion.id)}</span>
          <span>${escapeHtml(String(criterion.type || "").replaceAll("_", " "))}</span>
          <span>${escapeHtml(String(criterion.verification || "").replaceAll("_", " + "))}</span>
          <span>${(criterion.evidence || []).length} evidence</span>
        </div>
      </div>
      <span class="criterion-state ${escapeHtml(criterion.state)}">${escapeHtml(String(criterion.state || "unknown").replaceAll("_", " "))}</span>
    </div>`).join("") : '<div class="empty-row">No acceptance criteria found.</div>';
}

function renderAttention(items) {
  $("attention-count").textContent = items.length;
  $("attention-list").innerHTML = items.length
    ? items.map((item) => `<div class="attention-item"><i></i><span>${escapeHtml(item)}</span></div>`).join("")
    : '<div class="attention-clear">Threat scan clear. Trajectory stable.</div>';
}

function renderRoleChiclets(activeRole) {
  document.querySelectorAll("#role-chiclets .chiclet").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.role === activeRole);
  });
}

function renderCrew(actors) {
  const crew = [
    ["ARCHITECT", actors.architect],
    ["DESIGN REVIEWER", actors.design_reviewed_by],
    ["IMPLEMENTER", actors.implemented_by],
    ["REVIEWER", actors.reviewed_by],
    ["APPROVER", actors.approved_by],
  ];
  $("crew-list").innerHTML = crew.map(([role, person]) => `
    <div class="crew-member"><span>${role}</span><strong class="${person ? "" : "unassigned"}">${escapeHtml(person || "Station vacant")}</strong></div>`).join("");
}

function renderEvents(events, total) {
  $("event-total").textContent = `${total} EVENT${total === 1 ? "" : "S"}`;
  $("event-list").innerHTML = events.length ? events.map((event) => `
    <div class="event">
      <time datetime="${escapeHtml(event.at)}">${escapeHtml(relativeTime(event.at))}</time>
      <div><strong>${escapeHtml(cleanKind(event.kind))}</strong><p>${escapeHtml(event.message || "Recorded state transition")}</p></div>
    </div>`).join("") : '<div class="empty-row">Flight Log clear. No events recorded.</div>';
}

function renderVerifications(records, total) {
  $("verification-total").textContent = `${total} RUN${total === 1 ? "" : "S"}`;
  $("verification-list").innerHTML = records.length ? records.map((record) => `
    <div class="verification">
      <time datetime="${escapeHtml(record.at)}">${escapeHtml(relativeTime(record.at))}</time>
      <div>
        <strong class="verification-status ${record.ok ? "" : "failed"}">${record.ok ? "PASS" : "FAIL"} · ${escapeHtml(cleanKind(record.kind))}</strong>
        <p>${escapeHtml((record.criteria || []).join(", ") || "No criterion")} · ${escapeHtml(record.by || "Unknown actor")}</p>
      </div>
    </div>`).join("") : '<div class="empty-row">No diagnostic evidence recorded.</div>';
}

function render(snapshot) {
  state.lastGenerated = snapshot.generated_at;
  $("last-sync").textContent = `SYNCED ${relativeTime(snapshot.generated_at).toUpperCase()}`;
  if (snapshot.settings) {
    state.settings = snapshot.settings;
    if (!$("settings-dialog").open || !state.settingsDirty) populateAgentSettings();
  }
  if (!snapshot.initialized) {
    state.inputRequired = false;
    state.alertSignature = null;
    document.body.classList.remove("input-is-required");
    document.title = "Handsoff // E.V.E. Mission Control";
    $("active-state").classList.add("hidden");
    $("empty-state").classList.remove("hidden");
    $("empty-message").textContent = snapshot.error || "No Mission Objective detected. Initialize a Ship Feature to begin.";
    setFaviconState("idle");
    return;
  }

  $("empty-state").classList.add("hidden");
  $("active-state").classList.remove("hidden");
  const status = snapshot.status;
  const acceptance = snapshot.acceptance;
  const supervisor = snapshot.supervisor;
  const policy = snapshot.policy;
  const progress = Math.max(0, Math.min(100, Number(status.progress) || 0));

  renderInputAlert(snapshot.input_required, snapshot.project.feature);
  if (!state.inputRequired) document.title = `${snapshot.project.feature} · Handsoff`;
  setFaviconState(status.status === "complete" ? "complete"
    : state.inputRequired ? "blocked" : "in_progress");
  $("project-name").textContent = snapshot.project.name.toUpperCase();
  $("feature-name").textContent = snapshot.project.feature;
  $("project-root").textContent = snapshot.root;
  $("mission-state").textContent = String(status.status || "unknown").replaceAll("_", " ").toUpperCase();
  const complete = progress >= 100;
  $("progress-value").textContent = Math.round(progress);
  $("progress-ring").style.setProperty("--progress", `${progress * 3.6}deg`);
  $("progress-ring").classList.toggle("is-complete", complete);
  $("phase-kicker").textContent = complete ? "OBJECTIVES NEUTRALIZED · MISSION COMPLETE" : `PHASE ${status.phase_number} OF 8`;
  $("phase-name").textContent = status.phase;
  $("status-updated").textContent = `State updated ${relativeTime(status.updated_at)}`;
  $("design-rounds").textContent = `${policy.design_round} / ${policy.max_design_rounds}`;
  $("review-rounds").textContent = `${policy.review_round} / ${policy.max_review_rounds}`;
  $("evidence-runs").textContent = snapshot.audit.verification_runs;

  renderPhases(snapshot.phases);

  $("supervisor-panel").className = `supervisor-panel panel ${supervisor.tone}`;
  $("briefing-state").textContent = supervisor.label.toUpperCase();
  $("supervisor-headline").textContent = supervisor.headline;
  $("supervisor-summary").textContent = supervisor.summary;
  $("supervisor-next").textContent = supervisor.next_action;
  $("supervisor-reassurance").textContent = supervisor.reassurance;

  $("acceptance-score").textContent = `${acceptance.passing} / ${acceptance.total}`;
  stateClass($("acceptance-score"), acceptance.passing === acceptance.total && acceptance.total ? "is-good" : acceptance.failing || acceptance.blocked ? "is-bad" : "is-warning");
  $("symptom-state").textContent = acceptance.original_symptom_resolved ? "RESOLVED" : "OPEN";
  $("symptom-detail").textContent = acceptance.original_symptom_resolved ? "neutralization confirmed" : "neutralization unconfirmed";
  stateClass($("symptom-state"), acceptance.original_symptom_resolved ? "is-good" : "is-warning");
  $("audit-state").textContent = snapshot.audit.healthy ? "INTACT" : "BLOCKED";
  $("audit-detail").textContent = snapshot.audit.healthy ? `${snapshot.audit.event_count} chained events verified` : `${snapshot.audit.gate_errors.length + snapshot.audit.chain_errors.length} integrity issue(s)`;
  stateClass($("audit-state"), snapshot.audit.healthy ? "is-good" : "is-bad");
  $("approval-state").textContent = status.deployment_approved ? "APPROVED" : "PENDING";
  $("approval-detail").textContent = status.deployment_approved ? `by ${status.deployment_approved.by}` : policy.explicit_approval ? "Pilot authorization required" : "approval interlock disabled";
  stateClass($("approval-state"), status.deployment_approved ? "is-good" : "is-warning");

  renderCriteria(acceptance.criteria);
  renderAttention(supervisor.attention);
  renderCrew(snapshot.actors);
  renderRoleChiclets(snapshot.actors.active_role);
  renderEvents(snapshot.events, snapshot.audit.event_count);
  renderVerifications(snapshot.verifications, snapshot.audit.verification_runs);
}

// --- favicon: a small colored dot standing in for run state at a glance,
// visible in the browser tab without the dashboard needing focus. Blue and
// blinking while work is moving, solid red while blocked on a decision,
// solid green once the run is complete. Drawn on a canvas rather than a
// static file so it needs no image asset and can change color live.
const FAVICON_COLORS = { in_progress: "#6fb3f5", blocked: "#ff8f88", complete: "#3ddc84", idle: "#75858f" };
const faviconCanvas = document.createElement("canvas");
faviconCanvas.width = 32;
faviconCanvas.height = 32;
const faviconCtx = faviconCanvas.getContext("2d");
let faviconBlinkTimer = null;
let faviconState = null;

function paintFavicon(color, alpha) {
  const ctx = faviconCtx;
  ctx.clearRect(0, 0, 32, 32);
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  ctx.arc(16, 16, 13, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  $("favicon").href = faviconCanvas.toDataURL("image/png");
}

function setFaviconState(state) {
  if (state === faviconState) return;
  faviconState = state;
  if (faviconBlinkTimer) {
    window.clearInterval(faviconBlinkTimer);
    faviconBlinkTimer = null;
  }
  const color = FAVICON_COLORS[state] || FAVICON_COLORS.in_progress;
  if (state === "in_progress") {
    let bright = true;
    paintFavicon(color, 1);
    faviconBlinkTimer = window.setInterval(() => {
      bright = !bright;
      paintFavicon(color, bright ? 1 : 0.25);
    }, 600);
  } else {
    paintFavicon(color, 1);
  }
}

function showError(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 5000);
}

async function refresh() {
  if (state.refreshInFlight) {
    state.refreshQueued = true;
    return;
  }
  state.refreshInFlight = true;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`Dashboard returned ${response.status}`);
    render(await response.json());
    state.failures = 0;
  } catch (error) {
    state.failures += 1;
    if (state.failures === 1) showError(`Tactical telemetry interrupted: ${error.message}`);
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      window.queueMicrotask(refresh);
    }
  }
}

function setConnectionStatus(label) {
  $("connection-status").textContent = label;
}

function connectEventStream() {
  if (!("EventSource" in window)) {
    setConnectionStatus("TACTICAL LINK: POLLING");
    return;
  }
  const events = new EventSource("/api/events");
  state.eventStream = events;
  events.addEventListener("ready", () => {
    setConnectionStatus("TACTICAL LINK: LIVE");
    refresh();
  });
  events.addEventListener("invalidate", refresh);
  events.onopen = () => setConnectionStatus("TACTICAL LINK: LIVE");
  events.onerror = () => setConnectionStatus("TACTICAL LINK: RECONNECTING");
}

refresh();
connectEventStream();
$("enable-alerts").addEventListener("click", enableDesktopAlerts);
$("design-approve").addEventListener("click", authorizeDesign);
$("settings-toggle").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", closeSettings);
$("settings-cancel").addEventListener("click", closeSettings);
$("settings-form").addEventListener("submit", saveAgentSettings);
$("settings-dialog").addEventListener("cancel", () => {
  state.settingsDirty = false;
  $("settings-result").textContent = "";
});
for (const role of AGENT_ROLES) {
  for (const id of [`agent-${role}`, `agent-${role}-model`]) {
    $(id).addEventListener("input", () => {
      state.settingsDirty = true;
      $("settings-result").textContent = "";
      updateSettingsSaveState();
    });
  }
}
window.setInterval(refresh, 5000);
window.addEventListener("focus", refresh);
window.addEventListener("pageshow", refresh);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});
window.setInterval(() => {
  if (state.lastGenerated) $("last-sync").textContent = `SYNCED ${relativeTime(state.lastGenerated).toUpperCase()}`;
}, 1000);
window.setInterval(() => {
  if (!state.inputRequired) return;
  state.titleFlip = !state.titleFlip;
  document.title = state.titleFlip ? "🔴 PILOT AUTHORIZATION REQUIRED" : `${state.feature} · Handsoff`;
}, 900);
