const $ = (id) => document.getElementById(id);
const state = {
  lastGenerated: null,
  failures: 0,
  offlineSince: null,
  lastRunStatus: null,
  lastStreamRefresh: 0,
  feature: "Handsoff",
  inputRequired: false,
  inputKind: null,
  pilotGate: false,
  alertSignature: null,
  titleFlip: false,
  refreshInFlight: false,
  refreshQueued: false,
  eventStream: null,
  settings: null,
  settingsDirty: false,
  actors: {},
  activeRole: null,
  latestEvent: null,
  runSessions: {},
  crew: [],
  replacements: [],
  regressionRequest: null,
  fallbackDraft: {},
  live: null,
  liveReceivedAt: null,
  agentOutputSession: null,
  agentOutputCursor: 0,
};
const AGENT_ROLES = ["architect", "supervisor", "implementer", "reviewer"];
// adapterLabel, effectiveProfileLabel, actorForRole, runProfileLabel,
// cleanKind, designReviewBudgetLabel, designReviewPacketLabel,
// designReviewerProfileLabel, eventMessage,
// runtime/crew/replacement
// labels, eventDetail, designEvidenceState, designEvidenceDetail,
// profileSourceLabel, autoDetectOptionLabel, resolveAgentSelectValue,
// ALLOWED_ADAPTERS, liveStatusView, liveAgeLabel, the amendment helpers
// (showAmendmentPanel, amendmentHeadline, amendmentDecisionLabel, ...), the verification
// execution helpers (verificationExecutionState, verificationExecutionLabel), pilotNoteText, and the fallback-list helpers come from
// lib/dashboard-logic.js (loaded before this file) so they stay testable
// with plain `node --test` and no DOM.

function runProfileLabelForRole(role) {
  return runProfileLabel(role, state);
}

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
    label.textContent = Array.isArray(info?.models) && info.models.length
      ? `${info?.label || id} (${info.models.join(", ")})`
      : (info?.label || id);
    const badge = document.createElement("span");
    badge.className = `provider-state ${meta.cssClass}`;
    badge.textContent = meta.text;
    item.append(label, badge);
    list.append(item);
  }
}

const ROUTING_PHASE_PURPOSES = {
  1: "ORIENTATION",
  2: "DESIGN CHALLENGE",
  3: "DESIGN APPROVAL",
  4: "IMPLEMENTATION",
  5: "IMPLEMENTATION AUDIT",
  6: "CHECKS & DOCUMENTATION",
  7: "DEPLOYMENT",
  8: "LIVE VERIFICATION",
};

function routingJourneyTime(value) {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : `${parsed.toISOString().slice(11, 19)}Z`;
}

function routingHandoff(previous, current) {
  const fromAdapter = String(previous?.adapter || "unknown").toUpperCase();
  const toAdapter = String(current?.adapter || "unknown").toUpperCase();
  if (fromAdapter !== toAdapter) return { kind: "handoff", label: "PROVIDER HANDOFF", detail: `${fromAdapter} → ${toAdapter}` };
  if (previous?.model && current?.model && previous.model !== current.model) {
    return { kind: "handoff", label: "MODEL SHIFT", detail: `${previous.model} → ${current.model}` };
  }
  if (!previous?.model && current?.model) return { kind: "resolved", label: "MODEL RESOLVED", detail: current.model };
  if (previous?.model && previous.model === current?.model) return { kind: "continued", label: "MODEL CONTINUES", detail: current.model };
  return { kind: "continued", label: "NEXT MISSION LEG", detail: toAdapter };
}

function renderAdaptiveRouting(routing) {
  const panel = $("adaptive-routing-panel");
  if (!panel) return;
  const view = adaptiveRoutingView(routing);
  panel.classList.toggle("is-unused", !view.used);
  $("routing-not-used")?.classList.toggle("hidden", view.used);
  $("routing-details")?.classList.toggle("hidden", !view.used);
  $("routing-assignments")?.classList.toggle("hidden", view.selections.length === 0);
  const set = (id, value) => { const element = $(id); if (element) element.textContent = value; };
  set("routing-tier", view.tier || "—");
  set("routing-model", view.model || "—");
  set("routing-tokens", view.token_usage.total.toLocaleString());
  set("routing-cost", view.estimated_cost == null ? "—" : `$${Number(view.estimated_cost).toFixed(4)}`);
  set("routing-duration", view.duration_ms == null ? "—" : `${view.duration_ms} ms`);
  set("routing-escalation", view.escalation_reason || "No escalation");
  set("routing-rounds", `${view.repair_rounds} repair · ${view.review_rounds} review`);
  set("routing-premium-scope", view.active_premium_scope || "None");
  set("routing-outcome", view.outcome || "Pending");
  for (const tier of ADAPTIVE_ROUTING_TIERS) set(`routing-calls-${tier.toLowerCase()}`, String(view.calls_by_tier[tier]));
  const selections = $("routing-selections");
  if (selections) {
    selections.replaceChildren();
    const journeyTrack = $("routing-journey-track");
    journeyTrack?.replaceChildren();
    journeyTrack?.style.setProperty("--journey-legs", String(view.selections.length));
    const providers = new Set(view.selections.map((item) => item.adapter).filter(Boolean));
    const transfers = view.selections.slice(1).filter((item, index) => {
      const previous = view.selections[index];
      return previous.adapter !== item.adapter || (previous.model && item.model && previous.model !== item.model);
    }).length;
    set("routing-journey-summary", `${view.selections.length} LEGS · ${providers.size} PROVIDER${providers.size === 1 ? "" : "S"} · ${transfers} HANDOFF${transfers === 1 ? "" : "S"}`);
    for (const [index, item] of view.selections.entries()) {
      if (journeyTrack) {
        const mapStop = document.createElement("div");
        const mapTransfer = index > 0 ? routingHandoff(view.selections[index - 1], item) : null;
        mapStop.className = `routing-map-stop adapter-${String(item.adapter || "unknown").toLowerCase()} state-${String(item.state || "unknown").replaceAll("_", "-")}${mapTransfer?.kind === "handoff" ? " is-handoff" : ""}`;
        mapStop.setAttribute("aria-label", `Leg ${item.journey_index}, ${item.actor || item.role}, ${item.purpose}, ${item.model || "model not reported"}`);
        mapStop.title = `${item.actor || item.role} · ${item.purpose} · ${item.model || "Not reported by provider"}`;
        const mapLeg = document.createElement("span");
        mapLeg.className = "routing-map-leg";
        mapLeg.textContent = String(item.journey_index).padStart(2, "0");
        const orbit = document.createElement("span");
        orbit.className = "routing-map-orbit";
        const core = document.createElement("span");
        core.className = "routing-map-core";
        core.textContent = String(item.adapter || "?").slice(0, 1).toUpperCase();
        orbit.append(core);
        const mapProvider = document.createElement("strong");
        mapProvider.textContent = String(item.adapter || "unknown").toUpperCase();
        const mapModel = document.createElement("small");
        mapModel.textContent = item.model || "UNREPORTED";
        if (mapTransfer?.kind === "handoff") {
          const marker = document.createElement("span");
          marker.className = "routing-map-transfer";
          marker.textContent = mapTransfer.label === "PROVIDER HANDOFF" ? `${String(view.selections[index - 1].adapter).toUpperCase()} → ${String(item.adapter).toUpperCase()}` : "MODEL SHIFT";
          mapStop.append(marker);
        }
        mapStop.append(mapLeg, orbit, mapProvider, mapModel);
        journeyTrack.append(mapStop);
      }
      if (index > 0) {
        const transfer = routingHandoff(view.selections[index - 1], item);
        const connector = document.createElement("div");
        connector.className = `routing-handoff is-${transfer.kind}`;
        connector.setAttribute("aria-label", `${transfer.label}: ${transfer.detail}`);
        const track = document.createElement("span");
        track.className = "routing-handoff-track";
        track.setAttribute("aria-hidden", "true");
        const copy = document.createElement("span");
        copy.className = "routing-handoff-copy";
        const label = document.createElement("strong");
        label.textContent = transfer.label;
        const detail = document.createElement("small");
        detail.textContent = transfer.detail;
        copy.append(label, detail);
        connector.append(track, copy);
        selections.append(connector);
      }
      const row = document.createElement("article");
      row.className = `routing-selection adapter-${String(item.adapter || "unknown").toLowerCase()} state-${String(item.state || "unknown").replaceAll("_", "-")}`;
      row.setAttribute("aria-label", `Journey leg ${item.journey_index}: ${item.actor || item.role}, ${item.purpose}, ${item.model || "model not reported"}, ${item.state}`);
      const node = (className, label) => {
        const element = document.createElement("div");
        element.className = `routing-selection-node ${className}`;
        const eyebrow = document.createElement("span");
        eyebrow.className = "routing-selection-eyebrow";
        eyebrow.textContent = label;
        element.append(eyebrow);
        return element;
      };
      const top = document.createElement("div");
      top.className = "routing-selection-top";
      const leg = document.createElement("span");
      leg.className = "routing-leg-number";
      leg.textContent = `LEG ${String(item.journey_index).padStart(2, "0")}`;
      const attempt = document.createElement("span");
      attempt.className = "routing-attempt";
      attempt.textContent = `PHASE ${item.phase_number || "—"} · ATTEMPT ${item.journey_attempt}`;
      const state = document.createElement("strong");
      state.className = `routing-selection-state state-${String(item.state || "unknown").replaceAll("_", "-")}`;
      state.textContent = String(item.state || "unknown").replaceAll("_", " ").toUpperCase();
      top.append(leg, attempt, state);
      const identity = document.createElement("div");
      identity.className = "routing-selection-agent";
      const avatar = document.createElement("span");
      avatar.className = `routing-agent-avatar adapter-${String(item.adapter || "unknown").toLowerCase()}`;
      avatar.textContent = String(item.adapter || "?").slice(0, 1).toUpperCase();
      const identityCopy = node("routing-selection-identity", "AGENT");
      const actor = document.createElement("strong");
      actor.textContent = item.actor || item.role || "Agent";
      const adapterName = document.createElement("small");
      adapterName.textContent = String(item.adapter || "unknown").toUpperCase();
      identityCopy.append(actor, adapterName);
      identity.append(avatar, identityCopy);
      const purpose = node("routing-selection-purpose", "MISSION");
      const phasePurpose = ROUTING_PHASE_PURPOSES[Number(item.phase_number)] || `PHASE ${item.phase_number || "—"}`;
      const purposeName = document.createElement("strong");
      purposeName.textContent = item.purpose || phasePurpose;
      const phase = document.createElement("small");
      phase.textContent = `PHASE ${item.phase_number || "—"}`;
      purpose.append(purposeName, phase);
      const model = node("routing-selection-model", "EXACT MODEL");
      const modelName = document.createElement("strong");
      modelName.textContent = item.model || "Not reported by provider";
      modelName.classList.toggle("is-unknown", !item.model);
      const modelDetail = document.createElement("small");
      const sourceLabels = {
        adapter_reported: "verified from runner telemetry",
        adaptive_selection: "exact adaptive selection",
        exact_request: "exact requested model",
        not_reported: `requested ${item.requested_model || "provider default"}`,
      };
      modelDetail.textContent = sourceLabels[item.model_source] || String(item.reason || "selected").replaceAll("_", " ");
      if (item.model_consistency === "matched") modelDetail.textContent += " · matches route";
      if (item.model_consistency === "pending_verification") modelDetail.textContent += " · awaiting provider confirmation";
      if (item.model_consistency === "mismatch") {
        modelDetail.textContent += ` · differs from routed ${item.requested_model}`;
        model.classList.add("is-mismatch");
      }
      model.append(modelName, modelDetail);
      const footer = document.createElement("div");
      footer.className = "routing-selection-footer";
      const tier = document.createElement("small");
      tier.className = `routing-selection-tier tier-${String(item.tier || "configured").toLowerCase()}`;
      tier.textContent = item.adaptive ? (item.tier || "—") : "CONFIGURED";
      const time = document.createElement("span");
      time.className = "routing-selection-time";
      const started = routingJourneyTime(item.started_at);
      const ended = routingJourneyTime(item.ended_at);
      time.textContent = `${started || "TIME UNAVAILABLE"} → ${ended || (item.state === "running" || item.state === "launching" ? "IN FLIGHT" : "END UNRECORDED")}`;
      if (item.started_at || item.ended_at) time.title = `${item.started_at || "unknown start"} → ${item.ended_at || "in flight"}`;
      footer.append(tier, time);
      row.append(top, identity, purpose, model, footer);
      selections.append(row);
    }
  }
  const state = $("routing-state");
  if (state) {
    state.textContent = adaptiveRoutingPauseLabel(routing);
    state.className = `adaptive-routing-state ${view.pause ? "is-warning" : view.used ? "is-active" : "is-unused"}`;
  }
  const detail = $("routing-pause-detail");
  if (detail) {
    detail.textContent = view.pause ? `Routing is paused: ${view.pause.reason.replaceAll("_", " ")}${view.pause.scope ? ` (${view.pause.scope})` : ""}.` : "";
    detail.classList.toggle("hidden", !view.pause);
  }
}

function markSettingsDirty() {
  state.settingsDirty = true;
  $("settings-result").textContent = "";
  updateSettingsSaveState();
}

function renderFallbackRole(role) {
  const container = $(`fallback-${role}`);
  const entries = state.fallbackDraft[role] || [];
  container.replaceChildren();
  const head = document.createElement("div");
  head.className = "fallback-role-head";
  const label = document.createElement("span");
  label.textContent = `${role.toUpperCase()} · ${entries.length}/8`;
  const add = document.createElement("button");
  add.type = "button";
  add.className = "fallback-add";
  add.textContent = "+ ADD FALLBACK";
  add.disabled = entries.length >= MAX_FALLBACK_ENTRIES;
  add.addEventListener("click", () => {
    if (addFallbackEntry(entries)) {
      renderFallbackRole(role);
      markSettingsDirty();
    }
  });
  head.append(label, add);
  container.append(head);
  entries.forEach((profile, index) => {
    const row = document.createElement("div");
    row.className = "fallback-row";
    const order = document.createElement("span");
    order.textContent = String(index + 1);
    const adapter = document.createElement("select");
    adapter.setAttribute("aria-label", `${role} fallback ${index + 1} adapter`);
    for (const value of ["codex", "claude"]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = adapterLabel(value);
      adapter.append(option);
    }
    adapter.value = profile.adapter;
    const model = document.createElement("input");
    model.setAttribute("aria-label", `${role} fallback ${index + 1} model`);
    model.setAttribute("list", "model-suggestions");
    model.maxLength = 128;
    model.value = profile.model;
    adapter.addEventListener("input", () => { profile.adapter = adapter.value; markSettingsDirty(); });
    model.addEventListener("input", () => { profile.model = model.value; markSettingsDirty(); });
    const control = (text, title, disabled, action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      button.title = title;
      button.setAttribute("aria-label", title);
      button.disabled = disabled;
      button.addEventListener("click", action);
      return button;
    };
    const move = (offset) => () => {
      if (moveFallbackEntry(entries, index, offset)) {
        renderFallbackRole(role);
        markSettingsDirty();
      }
    };
    const remove = () => {
      if (removeFallbackEntry(entries, index)) {
        renderFallbackRole(role);
        markSettingsDirty();
      }
    };
    row.append(order, adapter, model,
      control("↑", `Move ${role} fallback ${index + 1} up`, index === 0, move(-1)),
      control("↓", `Move ${role} fallback ${index + 1} down`, index === entries.length - 1, move(1)),
      control("×", `Remove ${role} fallback ${index + 1}`, false, remove));
    container.append(row);
  });
}

function populateAgentSettings() {
  if (!state.settings) return;
  state.fallbackDraft = buildFallbackDraft(state.settings.fallbacks, AGENT_ROLES);
  $("max-failovers").value = String(state.settings.max_failovers_per_role ?? 2);
  for (const role of AGENT_ROLES) {
    const select = $(`agent-${role}`);
    select.querySelectorAll("option[data-custom]").forEach((option) => option.remove());
    const profile = state.settings.profiles?.[role] || {
      adapter: state.settings.agents?.[role] || "auto",
      model: "default",
    };
    const value = resolveAgentSelectValue(profile.adapter);
    if (value) {
      select.value = value;
    } else {
      const custom = document.createElement("option");
      custom.value = "";
      custom.textContent = `Custom · ${profile.adapter}: choose replacement`;
      custom.dataset.custom = "true";
      custom.disabled = true;
      custom.selected = true;
      select.prepend(custom);
    }
    const autoOption = select.querySelector('option[value="auto"]');
    if (autoOption) {
      autoOption.textContent = autoDetectOptionLabel(state.settings.default_adapter);
    }
    select.title = select.options[select.selectedIndex]?.textContent || "";
    $(`agent-${role}-model`).value = profile.model || "default";
    const effective = state.settings.effective_profiles?.[role] || profile;
    const effectiveNode = $(`agent-${role}-effective`);
    // #39: name the provenance next to the next-launch profile so a
    // recommended default is never mistaken for a saved choice.
    const sources = state.settings.profile_sources?.[role];
    effectiveNode.textContent = `NEXT LAUNCH: ${effectiveProfileLabel(effective)} · ${profileSourceLabel(sources)}`;
    effectiveNode.title = effectiveNode.textContent;
    const runNode = $(`agent-${role}-run`);
    runNode.textContent = runProfileLabelForRole(role);
    runNode.title = runNode.textContent;
    renderFallbackRole(role);
  }
  const availability = state.settings.availability || {};
  $("adapter-availability").textContent = ALLOWED_ADAPTERS.filter((adapter) => adapter !== "auto").map((adapter) =>
    `${adapter === "claude" ? "Claude Code" : "Codex"}: ${availability[adapter]?.available ? "DETECTED" : "NOT DETECTED"}`
  ).join(" · ");
  renderProviderStatus(state.settings.providers || {});
  renderFeatureSwitches();
  updateSettingsSaveState();
}

function updateSettingsSaveState() {
  const cap = Number($("max-failovers").value);
  const invalidPrimary = AGENT_ROLES.some((role) => {
    const model = $(`agent-${role}-model`).value;
    return !ALLOWED_ADAPTERS.includes($(`agent-${role}`).value)
      || !model || model.trim() !== model || model.length > 128 || model.startsWith("-")
      || [...model].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127);
  });
  const invalidFallback = AGENT_ROLES.some((role) =>
    !Array.isArray(state.fallbackDraft[role]) || state.fallbackDraft[role].length > 8
    || state.fallbackDraft[role].some((profile) =>
      !["codex", "claude"].includes(profile.adapter)
      || !profile.model || profile.model.trim() !== profile.model || profile.model.length > 128
      || profile.model.startsWith("-")
      || [...profile.model].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127))
  );
  $("settings-save").disabled = invalidPrimary || invalidFallback
    || !Number.isInteger(cap) || cap < 0 || cap > 8;
}

// #165 #167 #166: the WORKFLOW FEATURES group, rendered from the snapshot
// and saved on its own button so a switch flip never rides on the matrix.
function renderFeatureSwitches() {
  const host = $("feature-switches");
  const rows = featureSwitchRows(state.settings);
  host.innerHTML = rows.map((row) => `<label class="feature-switch"><input type="checkbox" name="feature-${escapeHtml(row.name)}" data-feature="${escapeHtml(row.name)}"${row.enabled ? " checked" : ""}><span><strong>${escapeHtml(row.label)}</strong>${row.isDefault ? "" : ' <em class="feature-changed">changed from default</em>'}<small>${escapeHtml(row.description)}</small></span></label>`).join("");
  $("features-save").disabled = rows.length === 0;
}

async function saveFeatureSettings() {
  const rows = featureSwitchRows(state.settings);
  const payload = featuresPayload(rows, (name) => $("feature-switches").querySelector(`input[data-feature="${name}"]`)?.checked);
  const button = $("features-save");
  button.disabled = true;
  $("features-result").textContent = "Transmitting workflow features…";
  try {
    const response = await fetch("/api/settings/features", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Settings returned ${response.status}`);
    state.settings = { ...state.settings, features: result.features };
    renderFeatureSwitches();
    $("features-result").textContent = "Workflow features confirmed. They apply from the next gate and launch.";
    refresh();
  } catch (error) {
    $("features-result").textContent = `Transmission rejected: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function closeSettings() {
  state.settingsDirty = false;
  $("settings-result").textContent = "";
  $("features-result").textContent = "";
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
  const payload = {
    profiles,
    fallbacks: serializeFallbackDraft(state.fallbackDraft, AGENT_ROLES),
    max_failovers_per_role: Number($("max-failovers").value),
  };
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
      body: JSON.stringify(payload),
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

function regressionRecordText(request) {
  if (!request) return "";
  const repo = request.repository || {};
  const pair = repo.commit_pair || {};
  const results = (request.results || []).map((result) =>
    `  exit ${result.exit_code}: ${result.command}`).join("\n");
  return [
    request.release_version ? `Release: ${request.release_version} · ${String(request.release_class || "unknown").toUpperCase()}` : null,
    request.policy_override_reason ? `Policy override: ${request.policy_override_reason}` : null,
    `Group: ${request.group}`,
    `State: ${String(request.state || "unknown").toUpperCase()}`,
    `Command hash: ${request.command_sha256}`,
    `Repository: ${repo.path || "unknown"}`,
    `Branch / HEAD: ${repo.branch || "unknown"} @ ${repo.head || "unknown"}`,
    `Dirty: ${Boolean(repo.dirty)} · Content digest: ${repo.content_sha256 || "unknown"}`,
    `Commit pair: ${pair.before || "unknown"} → ${pair.after || "unknown"}`,
    `Reason: ${request.reason || "not supplied"}`,
    `Requested by: ${request.requested_by || "unknown"}${request.requester_session_id ? ` (${request.requester_session_id})` : ""}`,
    `Requested: ${request.requested_at || "unknown"} · Expires: ${request.expires_at || "unknown"}${request.expires_at ? ` (${relativeTime(request.expires_at)})` : ""}`,
    request.decided_at ? `Decision: ${request.decided_by || "unknown"} at ${request.decided_at}` : null,
    request.launched_at ? `Launched: ${request.launched_at}` : null,
    request.completed_at ? `Completed: ${request.completed_at}` : null,
    `Scope: ${request.scope_hash || "unknown"} · Run: ${request.run_id || "unknown"}`,
    "Commands:",
    ...(request.commands || []).map((command) => `  ${command}`),
    results ? `Results:\n${results}` : null,
  ].filter(Boolean).join("\n");
}

function releasePlanText(plan) {
  if (!plan) return "";
  return [
    `Release: ${plan.version} · ${String(plan.release_class || "unknown").toUpperCase()}`,
    `Full regression eligible: ${plan.full_regression_eligible ? "YES" : "NO, TARGETED TESTS ONLY"}`,
    plan.full_regression_override_reason ? `Override: ${plan.full_regression_override_reason}` : null,
    `Planned by: ${plan.planned_by || "unknown"} · ${plan.planned_at || "unknown"}`,
    "Targeted checks:",
    ...(plan.targeted_checks || []).map((item) => `  ${item.command}: ${item.reason}`),
    `Full groups: ${(plan.regression_groups || []).join(", ") || "none"}`,
  ].filter(Boolean).join("\n");
}

function renderRegression(regression) {
  const current = regression?.current || null;
  const last = regression?.last || null;
  const plan = regression?.release_plan || null;
  const card = $("regression-alert");
  card.classList.toggle("hidden", !current && !last && !plan);
  $("regression-status-details").textContent = regressionRecordText(current || last) || releasePlanText(plan);
  $("regression-last").textContent = last
    ? `Last closed request: ${last.group} · ${String(last.state || "unknown").toUpperCase()} · ${last.completed_at || last.decided_at || last.requested_at}`
    : "";
}

function gateLabel(inputRequest) {
  if (!inputRequest?.required) return "AUTHORIZATION REQUIRED";
  if (inputRequest.turn === "pilot" && inputRequest.preauthorized) {
    const at = new Date(inputRequest.preauthorized.at);
    const stamp = isNaN(at.getTime()) ? "" : ` ${at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    return `PRE-AUTHORIZED BY PILOT NOTE${stamp}`;
  }
  if (inputRequest.turn === "reviewer") {
    const target = inputRequest.amendment_id ? ` · ${inputRequest.amendment_id} · ROUND ${inputRequest.amendment_round || 1}` : "";
    return `UNDER INDEPENDENT REVIEW${target}`;
  }
  if (inputRequest.turn === "architect") return "ARCHITECT REVISING";
  if (inputRequest.turn === "supervisor") return "SUPERVISOR WORKING";
  return "PILOT APPROVAL NEEDED";
}

function liveVerificationHeadline(verification) {
  const live = verification?.live || {};
  if (live.in_flight) {
    const flight = live.in_flight;
    return `LIVE VERIFICATION RUNNING · ${flight.done}/${flight.total}${flight.current ? ` · ${flight.current}` : ""}`;
  }
  if (live.last_failure) return `LIVE VERIFICATION FAILED · ${live.last_failure.command} exit ${live.last_failure.exit_code}`;
  return null;
}

function renderInputAlert(inputRequest, feature, regression) {
  const required = Boolean(inputRequest?.required);
  // #147: the card is labelled by whose turn it is; only the Pilot's own
  // turn, without a standing pre-authorization, is styled as a demand.
  const pilotTurn = required && inputRequest?.turn === "pilot" && !inputRequest?.preauthorized;
  const label = gateLabel(inputRequest);
  const message = inputRequest?.message || "Pilot authorization is required before the mission can continue.";
  const signature = required ? `${inputRequest.kind}:${message}` : null;
  state.feature = feature;
  state.inputRequired = required;
  state.pilotGate = pilotTurn;
  state.inputKind = required ? inputRequest?.kind : null;
  document.body.classList.toggle("input-is-required", pilotTurn);
  $("input-alert-label").textContent = label;
  $("input-alert").classList.toggle("hidden", !required);
  $("input-alert-message").textContent = message;
  const approvalButton = $("design-approve");
  const deploymentButton = $("deployment-approve");
  const regressionAccept = $("regression-accept");
  const regressionDecline = $("regression-decline");
  approvalButton.classList.toggle("hidden", state.inputKind !== "design_approval");
  deploymentButton.classList.toggle("hidden", state.inputKind !== "deployment_approval");
  const amendmentButton = $("amendment-approve");
  amendmentButton.classList.toggle("hidden", state.inputKind !== "amendment_approval");
  if (state.inputKind === "amendment_approval" && signature !== state.alertSignature) {
    amendmentButton.disabled = false;
    amendmentButton.textContent = "APPROVE AMENDMENT";
  }
  const budgetButton = $("design-review-authorize");
  budgetButton.classList.toggle("hidden", state.inputKind !== "design_review_budget");
  if (state.inputKind === "design_review_budget" && signature !== state.alertSignature) {
    budgetButton.disabled = false;
    budgetButton.textContent = "AUTHORIZE ONE MORE DESIGN REVIEW";
  }
  regressionAccept.classList.toggle("hidden", state.inputKind !== "regression_approval");
  regressionDecline.classList.toggle("hidden", state.inputKind !== "regression_approval");
  state.regressionRequest = regression?.pending || null;
  const regressionDetails = $("regression-details");
  regressionDetails.classList.toggle("hidden", state.inputKind !== "regression_approval");
  regressionDetails.textContent = regressionRecordText(state.regressionRequest);
  const blockers = Array.isArray(inputRequest?.blockers) ? inputRequest.blockers : [];
  state.inputBlockers = blockers;
  if (state.inputKind === "design_approval" && signature !== state.alertSignature) {
    approvalButton.disabled = false;
    approvalButton.textContent = "AUTHORIZE DESIGN";
  }
  // The gate would refuse: say why on the card and keep the button parked
  // until the Supervisor clears it, instead of inviting a click that fails.
  if (state.inputKind === "design_approval" && blockers.length) {
    approvalButton.disabled = true;
    approvalButton.textContent = "AUTHORIZATION BLOCKED";
    approvalButton.title = blockers.join("; ");
  } else if (state.inputKind === "design_approval") {
    approvalButton.title = "";
  }
  if (state.inputKind === "deployment_approval" && signature !== state.alertSignature) {
    deploymentButton.disabled = false;
    deploymentButton.textContent = "AUTHORIZE DEPLOYMENT";
  }
  if (state.inputKind === "regression_approval" && signature !== state.alertSignature) {
    regressionAccept.disabled = false;
    regressionDecline.disabled = false;
  }
  updateAlertButton();

  if (pilotTurn && signature !== state.alertSignature && "Notification" in window && Notification.permission === "granted") {
    new Notification("E.V.E. requests Pilot authorization", {
      body: message,
      tag: "handsoff-input-required",
    });
  }
  state.alertSignature = signature;
}

function renderOperations(operations = {}, actions = []) {
  const OP_CLASSES = ["op-actionable", "op-unavailable", "op-readonly"];
  const panel = $("operator-actions-panel");
  if (!panel) return;
  const inventory = operations.inventory || [];
  panel.classList.toggle("hidden", actions.length === 0 && inventory.length === 0);
  const actionKinds = new Set(actions.map((action) => action.kind));
  // Routine controls (pause, resume, close, reopen) are always offered, so
  // they live in the console; the decisions panel keeps only the calls that
  // actually gate the mission.
  const ROUTINE_KINDS = new Set(["pause", "resume", "run_close", "run_reopen"]);
  // The authorization card above already carries the control for the
  // decision the run is waiting on; listing it here too put two AUTHORIZE
  // DESIGN buttons on screen.
  const ALERT_KINDS = { design_approval: "design_approve", deployment_approval: "deployment_approve",
                        amendment_approval: "amendment_approve", design_review_budget: "design_review_authorize" };
  const carriedByAlert = state.inputRequired ? ALERT_KINDS[state.inputKind] : null;
  const decisions = actions.filter((action) => !ROUTINE_KINDS.has(action.kind) && action.kind !== carriedByAlert);
  const routine = actions.filter((action) => ROUTINE_KINDS.has(action.kind));
  $("operator-actions-count").textContent = `${decisions.length} PENDING`;
  const list = $("operator-actions-list");
  const routineList = $("operator-routine-list");
  // Snapshots arrive every few seconds; rebuilding the action cards each
  // time wiped whatever the Pilot was typing into a reason field. Keep the
  // cards when the bound action ids are unchanged, and carry typed drafts
  // across a rebuild otherwise.
  const shown = [...decisions, ...routine];
  const signature = shown.map((action) => action.action_id).join("|");
  state.reasonDrafts = state.reasonDrafts || {};
  document.querySelectorAll("[data-reason-for]").forEach((input) => { if (input.value) state.reasonDrafts[input.dataset.reasonFor] = input.value; });
  if (signature === state.actionSignature && list.childElementCount + (routineList ? routineList.childElementCount : 0) === shown.length) {
    renderInventory(inventory, actionKinds, OP_CLASSES);
    renderConsoleForms(inventory, operations, actions);
    return;
  }
  state.actionSignature = signature;
  list.replaceChildren();
  if (routineList) routineList.replaceChildren();
  for (const action of shown) {
    const card = document.createElement("article");
    card.className = `operator-action operator-action-${action.tone || "primary"}`;
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = action.label;
    const consequence = document.createElement("p");
    consequence.textContent = action.consequence;
    copy.append(title, consequence);
    const controls = document.createElement("div");
    controls.className = "operator-action-controls";
    let reason = null;
    if (action.requires_reason) {
      reason = document.createElement("input");
      reason.type = "text";
      reason.maxLength = 512;
      reason.placeholder = "Reason required";
      reason.setAttribute("aria-label", `Reason for ${action.label}`);
      reason.dataset.reasonFor = action.kind;
      reason.value = state.reasonDrafts[action.kind] || "";
      reason.addEventListener("input", () => { state.reasonDrafts[action.kind] = reason.value; });
      controls.append(reason);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label.toUpperCase();
    button.addEventListener("click", () => executeOperatorAction(action, reason?.value || "", button));
    controls.append(button);
    card.append(copy, controls);
    if (ROUTINE_KINDS.has(action.kind) && routineList) routineList.append(card);
    else list.append(card);
  }
  renderInventory(inventory, actionKinds, OP_CLASSES);
  renderConsoleForms(inventory, operations, actions);
}

function renderInventory(inventory, actionKinds, OP_CLASSES) {
  // Unavailable operations are folded into one closed disclosure with a
  // single line per reason. A complete run has eighteen of them and every
  // one says "run is complete"; listing each as a card buried the panel.
  $("operator-inventory").innerHTML = ["actionable", "read_only", "unavailable"].map((availability) => {
    const entries = inventory.filter((item) => item.availability === availability && !(availability === "actionable" && actionKinds.has(item.kind)));
    if (!entries.length) return "";
    const cls = OP_CLASSES[["actionable", "unavailable", "read_only"].indexOf(availability)];
    if (availability === "unavailable") {
      const byReason = new Map();
      for (const item of entries) {
        const reason = item.reason || "not currently applicable";
        if (!byReason.has(reason)) byReason.set(reason, []);
        byReason.get(reason).push(item.label || item.kind);
      }
      const rows = [...byReason.entries()].map(([reason, kinds]) => `<li class="${cls}"><span>${escapeHtml(reason)}</span><small>${escapeHtml(kinds.join(", "))}</small></li>`).join("");
      return `<details class="operator-inventory-group operator-inventory-collapsed"><summary>${entries.length} UNAVAILABLE</summary><ul>${rows}</ul></details>`;
    }
    const detail = (item) => escapeHtml(item.consequence);
    return `<div class="operator-inventory-group"><h3>${availability.replace("_", " ").toUpperCase()}</h3>${entries.map((item) => `<article class="operator-inventory-item ${cls}"><strong>${escapeHtml(item.label || item.kind)}</strong><p>${detail(item)}</p></article>`).join("")}</div>`;
  }).join("");
}

function renderConsoleForms(inventory, operations, actions) {
  const launch = inventory.find((item) => item.kind === "launch_role");
  const launchForm = $("launch-role-form");
  const launchSelect = $("launch-role-role");
  launchSelect.replaceChildren(...(launch?.launchable_roles || []).map((role) => new Option(role, role)));
  $("launch-role-consequence").innerHTML = escapeHtml(launch?.consequence || launch?.reason || "Launch role unavailable");
  const launchButton = launchForm.querySelector("button");
  launchButton.disabled = !launch || launch.availability !== "actionable";
  launchButton.title = launch?.reason || "";
  launchForm.onsubmit = (event) => { event.preventDefault(); if (launch) postOperation("/api/launch-role", { action_id: launch.action_id, role: launchSelect.value, task: $("launch-role-task").value }, $("launch-role-result")); };
  const verify = inventory.find((item) => item.kind === "verify_criterion");
  const live = inventory.find((item) => item.kind === "verify_live");
  const inFlight = operations.verification?.in_flight || [];
  $("verify-form").querySelectorAll("button").forEach((button) => { button.disabled = inFlight.length > 0; });
  $("verify-criteria").innerHTML = (verify?.criteria || []).map((id) => `<label><input type="checkbox" value="${escapeHtml(id)}"> ${escapeHtml(id)}</label>`).join("");
  $("verify-form").onsubmit = (event) => { event.preventDefault(); if (verify) postOperation("/api/verify", { action_id: verify.action_id, criteria: [...$("verify-criteria").querySelectorAll("input:checked")].map((input) => input.value) }, $("verify-status")); };
  $("verify-live").onclick = () => { if (live) postOperation("/api/verify-live", { action_id: live.action_id }, $("verify-status")); };
  $("verify-status").textContent = inFlight.length ? `CHECKS IN FLIGHT: ${inFlight.join(", ")}` : "";
  $("verification-latest").innerHTML = Object.entries(operations.verification?.latest || {}).map(([criterion, result]) => `<tr><td>${escapeHtml(criterion)}</td><td>${result.ok ? "YES" : "NO"}</td><td>${escapeHtml(result.at)}</td></tr>`).join("");
  const engine = operations.engine || {};
  // The engine pane is one identity line plus the copyable commands behind
  // a closed disclosure. #184: the upgrade and migrate previews and the
  // permanent "execution is not offered" line are gone; they said nothing
  // about the run. A reason (#185: the pin could not be read) shows once.
  const commandRows = Object.entries(engine.commands || {}).map(([name, command]) => `<div><code>${escapeHtml(command)}</code><button type="button" data-copy-command="${escapeHtml(command)}">COPY</button></div>`).join("");
  const engineReason = typeof engine.reason === "string" && engine.reason ? `<p class="engine-reason">${escapeHtml(engine.reason)}</p>` : "";
  $("engine-panel").innerHTML = `<p class="engine-summary">${escapeHtml(engine.version)} <span>${escapeHtml(engine.source)} · pin ${escapeHtml(engine.pin)} · ${escapeHtml(engine.compatibility)}</span></p>${engineReason}${commandRows ? `<details class="engine-details"><summary>${Object.keys(engine.commands || {}).length} COMMANDS</summary><div class="engine-commands">${commandRows}</div></details>` : ""}`;
  $("engine-panel").querySelectorAll("[data-copy-command]").forEach((button) => button.onclick = () => navigator.clipboard.writeText(button.dataset.copyCommand));
  const ROUTINE = new Set(["pause", "resume", "run_close", "run_reopen"]);
  const decisions = actions.filter((action) => !ROUTINE.has(action.kind));
  const clear = $("decisions-clear");
  if (clear) clear.classList.toggle("hidden", decisions.length > 0 || state.inputRequired);
}

// The Pilot console is tabbed: operations inventory, launch, verify and
// engine each get a pane so the rail never stacks four forms end to end.
function bindConsoleTabs() {
  const tabs = [...document.querySelectorAll(".console-tab")];
  tabs.forEach((tab) => tab.addEventListener("click", () => {
    tabs.forEach((other) => { other.classList.toggle("is-active", other === tab); other.setAttribute("aria-selected", other === tab ? "true" : "false"); });
    document.querySelectorAll(".console-pane").forEach((pane) => pane.classList.toggle("is-active", pane.id === tab.dataset.tab));
  }));
}

function postOperation(url, body, resultElement) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(async (response) => { const result = await response.json(); if (!response.ok) throw new Error(result.error || `Request returned ${response.status}`); resultElement.textContent = result.message || result.status || "Request accepted"; await refresh(); }).catch((error) => { resultElement.textContent = error.message; });
}

function renderOperatorActions(actions = []) { renderOperations({}, actions); }

async function executeOperatorAction(action, reason, button) {
  if (action.requires_reason && !reason.trim()) {
    showError("Command rejected: enter the required reason.");
    return;
  }
  if (action.requires_confirmation && !window.confirm(`${action.label}\n\n${action.consequence}\n\nThis action is audited and cannot delete source code or Git history.`)) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "TRANSMITTING…";
  try {
    const response = await fetch("/api/operator-action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: action.action_id, reason: reason.trim() || null }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Command returned ${response.status}`);
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showError(`Command rejected: ${error.message}`);
  }
}

async function initializeMission(event) {
  event.preventDefault();
  const button = $("mission-init-submit");
  const feature = $("mission-init-feature").value.trim();
  if (!feature) return;
  button.disabled = true;
  button.textContent = "INITIALIZING…";
  try {
    const response = await fetch("/api/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        feature,
        issue: $("mission-init-issue").value.trim() || null,
        lane: $("mission-init-lane").value,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Initialization returned ${response.status}`);
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "INITIALIZE MISSION";
    showError(`Mission initialization rejected: ${error.message}`);
  }
}

async function decideRegression(decision) {
  const request = state.regressionRequest;
  if (!request) return;
  const accept = $("regression-accept");
  const decline = $("regression-decline");
  accept.disabled = true;
  decline.disabled = true;
  try {
    const response = await fetch("/api/regression-decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: request.request_id, decision, command_hash: request.command_sha256 }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Decision returned ${response.status}`);
    await refresh();
  } catch (error) {
    accept.disabled = false;
    decline.disabled = false;
    showError(`Regression decision rejected: ${error.message}`);
  }
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

async function approveAmendment() {
  const button = $("amendment-approve");
  button.disabled = true;
  button.textContent = "TRANSMITTING APPROVAL…";
  try {
    const response = await fetch("/api/amendment-approval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Approval returned ${response.status}`);
    button.textContent = "AMENDMENT APPROVED";
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "APPROVE AMENDMENT";
    showError(`Approval rejected: ${error.message}`);
  }
}

async function authorizeDesignReviewAttempt() {
  const button = $("design-review-authorize");
  button.disabled = true;
  button.textContent = "TRANSMITTING AUTHORIZATION…";
  try {
    const response = await fetch("/api/design-review-authorize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Authorization returned ${response.status}`);
    button.textContent = "ATTEMPT AUTHORIZED";
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "AUTHORIZE ONE MORE DESIGN REVIEW";
    showError(`Authorization rejected: ${error.message}`);
  }
}

async function sendPilotNote(event) {
  // #49: the header note box. The text goes to the current run's ledger
  // as a pilot_note event; the next archive scan lists it as an R7 finding.
  event.preventDefault();
  const input = $("pilot-note-text");
  const button = $("pilot-note-send");
  const text = pilotNoteText(input.value);
  if (!text) {
    showError("Pilot note rejected: enter 1 to 512 characters");
    return;
  }
  button.disabled = true;
  button.textContent = "SENDING…";
  try {
    const response = await fetch("/api/pilot-note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Pilot note returned ${response.status}`);
    input.value = "";
    button.textContent = "SENT";
    await refresh();
  } catch (error) {
    showError(`Pilot note rejected: ${error.message}`);
  } finally {
    button.disabled = false;
    window.setTimeout(() => { button.textContent = "SEND"; }, 1500);
  }
}

async function authorizeDeployment() {
  const button = $("deployment-approve");
  button.disabled = true;
  button.textContent = "TRANSMITTING AUTHORIZATION…";
  try {
    const response = await fetch("/api/deployment-approval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Authorization returned ${response.status}`);
    button.textContent = "DEPLOYMENT AUTHORIZED";
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "AUTHORIZE DEPLOYMENT";
    showError(`Authorization rejected: ${error.message}`);
  }
}

function renderPhases(phases, verification = null) {
  // #148: the Phase 7 node carries the live verification state; the server
  // already names the current phase that way in phases[].name, so the rail
  // only adds the failing command's detail card below it.
  $("phase-rail").innerHTML = phases.map((phase) => `
    <div class="phase-node ${escapeHtml(phase.state)} lane-${escapeHtml(phase.lane_status)}">
      <strong>0${escapeHtml(phase.number)}</strong>
      <span>${escapeHtml(phase.name)}</span>
    </div>`).join("");
  const failure = verification?.live?.last_failure;
  const card = $("phase-7-card");
  const running = Boolean(verification?.live?.in_flight);
  card.classList.toggle("hidden", !failure || running);
  if (failure && !running) {
    card.replaceChildren();
    const head = document.createElement("strong");
    head.textContent = `LIVE VERIFICATION FAILED · ${failure.command} · exit ${failure.exit_code}`;
    const tail = document.createElement("pre");
    tail.textContent = failure.output_tail || "(no output captured)";
    card.append(head, tail);
  }
}

function renderDesignDocument(snapshot) {
  const link = $("design-document-link");
  const document = snapshot?.design_document;
  link.classList.toggle("hidden", !document);
  if (document) {
    link.href = document;
    link.textContent = "OPEN DESIGN DOCUMENT";
  }
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
          <span>${(criterion.evidence || []).length} evidence</span>${baselineLabel(criterion) ? `<span class="criterion-baseline">${escapeHtml(baselineLabel(criterion))}</span>` : ""}${repeatLabel(criterion) ? `<span class="criterion-repeat">${escapeHtml(repeatLabel(criterion))}</span>` : ""}
        </div>
      </div>
      <span class="criterion-state ${escapeHtml(criterion.state)}">${escapeHtml(String(criterion.state || "unknown").replaceAll("_", " "))}</span>
    </div>`).join("") : '<div class="empty-row">No acceptance criteria found.</div>';
}

function renderWorkItems(workItems) {
  const items = workItems?.items || [];
  const visible = showWorkItemTable(workItems);
  const panel = $("ticket-panel");
  panel.classList.toggle("hidden", !visible);
  $("ticket-total").textContent = `${items.length} ITEM${items.length === 1 ? "" : "S"}`;
  $("ticket-list").innerHTML = items.map((item) => `
    <tr data-item-id="${escapeHtml(item.id)}">
      <td data-label="Item"><code>${escapeHtml(item.id)}</code></td>
      <td data-label="Issue">${item.number ? `#${escapeHtml(item.number)}` : "n/a"}</td>
      <td data-label="Title">${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>` : escapeHtml(item.title)}${item.discrepancy ? `<span class="ticket-state blocked" title="${escapeHtml(item.discrepancy)}">DISCREPANT</span>` : ""}</td>
      <td data-label="Lane"><span class="ticket-state">${escapeHtml(workItemLaneLabel(item))}</span><small>${escapeHtml(workItemLaneDetail(item))}</small>${smallFixCanConfirm(item) ? `<button class="mini-action lane-confirm" data-item="${escapeHtml(item.id)}">CONFIRM</button>` : ""}</td>
      <td data-label="Progress"><strong>${escapeHtml(workItemProgressLabel(item))}</strong></td>
      <td data-label="Status"><span class="ticket-state ${escapeHtml(item.status)}">${escapeHtml(String(item.status).replaceAll("_", " "))}</span></td>
      <td data-label="Phase / next">${escapeHtml(item.phase_or_next || "n/a")}</td>
      <td data-label="Blocker">${escapeHtml(item.blocker || "n/a")}</td>
      <td data-label="Updated">${escapeHtml(relativeTime(item.updated_at))}</td>
    </tr>`).join("");
  document.querySelectorAll(".lane-confirm").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await fetch("/api/lane-confirm", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item: button.dataset.item }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Lane confirmation returned ${response.status}`);
        await refresh();
      } catch (error) {
        button.disabled = false;
        showError(`Lane confirmation rejected: ${error.message}`);
      }
    });
  });
}

function renderTranche(proposal) {
  const panel = $("tranche-panel");
  panel.classList.toggle("hidden", !proposal);
  if (!proposal) return;
  const byId = Object.fromEntries((proposal.issues || []).map((issue) => [`issue-${issue.number}`, issue]));
  const rows = proposal.proposed_order.map((id) => ({ id, issue: byId[id], dropped: false }));
  const paint = () => {
    $("tranche-list").innerHTML = rows.map((row, index) => `<article class="criterion tranche-row" data-id="${escapeHtml(row.id)}">
      <div><strong>${escapeHtml(row.id)} · ${escapeHtml(row.issue?.title || "Unknown issue")}</strong>
      <p>${escapeHtml(`${row.issue?.score || 0} pts · ${row.issue?.lane || "full"} · deps ${(row.issue?.dependencies || []).map((n) => `#${n}`).join(", ") || "none"} · cost ${row.issue?.cost_source || "unknown"}`)}</p>
      <small>${escapeHtml((row.issue?.rationale || []).join(" · "))}</small><small>${escapeHtml(trancheIssueDetail(row.issue))}</small></div>
      <div><button data-move="up" ${index === 0 ? "disabled" : ""}>↑</button><button data-move="down" ${index === rows.length - 1 ? "disabled" : ""}>↓</button><button data-drop>${row.dropped ? "RETAIN" : "DROP"}</button></div>
    </article>`).join("");
    document.querySelectorAll(".tranche-row").forEach((node, index) => {
      node.querySelector('[data-move="up"]').onclick = () => { [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]]; paint(); };
      node.querySelector('[data-move="down"]').onclick = () => { [rows[index + 1], rows[index]] = [rows[index], rows[index + 1]]; paint(); };
      node.querySelector("[data-drop]").onclick = () => { rows[index].dropped = !rows[index].dropped; paint(); };
    });
  };
  paint();
  $("tranche-approve").onclick = async () => {
    const button = $("tranche-approve");
    button.disabled = true;
    try {
      const response = await fetch("/api/tranche-approval", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(trancheDecisionPayload(proposal.proposal_hash, rows))});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Tranche approval returned ${response.status}`);
      await refresh();
    } catch (error) { showError(`Tranche approval rejected: ${error.message}`); }
    finally { button.disabled = false; }
  };
}

function renderDesignEvidence(artifacts) {
  const panel = $("design-evidence-panel");
  panel.classList.toggle("hidden", !artifacts.length);
  $("design-evidence-total").textContent = `${artifacts.length} ARTIFACT${artifacts.length === 1 ? "" : "S"}`;
  $("design-evidence-list").innerHTML = artifacts.map((artifact) => `
    <div class="design-evidence-item">
      <span class="evidence-pill ${escapeHtml(designEvidenceState(artifact))}">${escapeHtml(designEvidenceState(artifact))}</span>
      <div>
        <strong>${escapeHtml(artifact.id)}</strong>
        <p>${escapeHtml(designEvidenceDetail(artifact))}</p>
      </div>
      ${artifact.truncated ? '<span class="evidence-flag">truncated</span>' : ""}
    </div>`).join("");
}

function renderAmendment(amendment) {
  const panel = $("amendment-panel");
  const visible = showAmendmentPanel(amendment);
  panel.classList.toggle("hidden", !visible);
  if (!visible) {
    $("amendment-headline").textContent = amendmentHeadline(null);
    $("amendment-decision").textContent = amendmentDecisionLabel(null);
    $("amendment-list").innerHTML = "";
    return;
  }
  const pending = amendmentPendingDecision(amendment);
  $("amendment-state").textContent = `PENDING ${String(pending || "decision").replaceAll("_", " ").toUpperCase()}`;
  $("amendment-headline").textContent = amendmentHeadline(amendment);
  $("amendment-decision").textContent = amendmentDecisionLabel(amendment);
  const rows = [
    amendmentIdsLabel(amendment.changed_ids, "Changed criteria"),
    amendmentIdsLabel(amendment.dependent_ids, "Dependent criteria"),
    amendmentIdsLabel(amendment.affected_work_items, "Affected work items"),
    amendmentEvidenceLabel(amendment),
    ...amendmentDecisionsView(amendment).map((line) => `Decision · ${line}`),
    `Classification: ${String(amendment.classification || "scoped").replaceAll("_", " ")}`,
    ...amendmentReasonsView(amendment).map((reason) => `Reason · ${reason}`),
    `Base design ${String(amendment.base_design_hash || "").slice(0, 12)} · amended ${String(amendment.resulting_design_hash || "").slice(0, 12)} · amendment hash ${String(amendment.amendment_hash || "").slice(0, 12)}`,
  ];
  $("amendment-list").innerHTML = rows.map((line) => `<li>${escapeHtml(line)}</li>`).join("");
}

function renderQuestions(questions) {
  const panel = $("questions-panel");
  const visible = showQuestionsPanel(questions);
  panel.classList.toggle("hidden", !visible);
  $("questions-headline").textContent = questionHeadline(questions);
  const cards = $("input-alert-cards");
  if (!visible) {
    $("questions-list").innerHTML = "";
    $("questions-state").textContent = "NONE OPEN";
    cards.innerHTML = "";
    cards.classList.add("hidden");
    $("input-alert-message").classList.remove("hidden");
    return;
  }
  const open = questions.open || [];
  const blocking = questions.blocking || [];
  $("questions-state").textContent = blocking.length ? "PILOT ANSWER REQUIRED" : (open.length ? "OPEN" : "ANSWERED");
  // #48: one compact numbered form per role, rendered by the pure helper
  // in lib/dashboard-logic.js; the banner gets one card per role.
  const rendered = renderQuestionForms(questions);
  const list = $("questions-list");
  list.innerHTML = rendered.html;
  const showCards = state.inputKind === "question" && rendered.banner.length > 0;
  cards.innerHTML = questionBannerCards(rendered.banner);
  cards.classList.toggle("hidden", !showCards);
  $("input-alert-message").classList.toggle("hidden", showCards);
  list.querySelectorAll(".qf-text[data-expand]").forEach((node) => {
    node.addEventListener("click", () => node.classList.toggle("is-clamped"));
  });
  list.querySelectorAll(".qf-row.is-open").forEach((row) => {
    const other = row.querySelector(".qf-other");
    row.querySelectorAll('input[type="radio"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        const isOther = radio.value === QUESTION_OTHER_VALUE && radio.checked;
        other.hidden = !isOther;
        if (isOther) other.focus();
      });
    });
  });
  list.querySelectorAll(".question-role-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const entries = [...form.querySelectorAll(".qf-row.is-open")].map((row) => {
        const picked = row.querySelector('input[type="radio"]:checked');
        return {
          question_id: row.dataset.questionId,
          choice: picked ? picked.value : null,
          other: row.querySelector(".qf-other").value,
        };
      });
      answerQuestionsBatch(collectQuestionFormAnswers(entries), form);
    });
  });
}

async function answerQuestionsBatch(answers, form) {
  const button = form.querySelector(".qf-send");
  if (!answers.length) {
    showError("Pick an answer for at least one question before sending.");
    return;
  }
  button.disabled = true;
  button.textContent = "Transmitting…";
  try {
    const response = await fetch("/api/question-answers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Answers returned ${response.status}`);
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Send answers";
    showError(`Answers rejected: ${error.message}`);
  }
}

async function answerQuestion(questionId, text, form) {
  // #46 single-answer path, kept for one-off answers; the role forms use
  // answerQuestionsBatch.
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "TRANSMITTING…";
  try {
    const response = await fetch("/api/question-answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: questionId, text }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Answer returned ${response.status}`);
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "SEND ANSWER";
    showError(`Answer rejected: ${error.message}`);
  }
}

function renderAttention(items) {
  $("attention-count").textContent = items.length;
  $("attention-list").innerHTML = items.length
    ? items.map((item) => `<div class="attention-item"><i></i><span>${escapeHtml(item)}</span></div>`).join("")
    : '<div class="attention-clear">Threat scan clear. Trajectory stable.</div>';
}

// #33: the live status strip under the header. `renderLive` runs on every
// snapshot; `renderLiveAge` also runs from a 1 s local ticker so the
// "last activity N s ago" reading visibly moves between snapshots.
function renderLive(live) {
  const strip = $("live-status");
  if (!strip) return;
  state.live = live && typeof live === "object" ? live : null;
  state.liveReceivedAt = state.live ? Date.now() : null;
  if (!state.live) {
    strip.classList.add("hidden");
    return;
  }
  const view = liveStatusView(state.live);
  strip.classList.remove("hidden");
  strip.dataset.state = view.state;
  strip.dataset.tone = view.tone;
  strip.classList.toggle("is-pulsing", view.pulsing);
  $("live-state").textContent = view.label;
  $("live-role").textContent = view.role;
  $("live-detail").textContent = view.detail;
  renderLiveAge();
}

// #181: the CI row under the phase rail; hidden when no watch is recorded.
function renderCi(ci) {
  const strip = $("ci-status");
  if (!strip) return;
  if (!ci || typeof ci !== "object") {
    state.ci = null;
    strip.classList.add("hidden");
    return;
  }
  strip.classList.remove("hidden");
  strip.dataset.state = ci.state === "passed" || ci.state === "failed" ? ci.state : "running";
  $("ci-state").textContent = ciStateLabel(ci);
  const link = $("ci-link");
  link.textContent = ci.pr ? `PR #${ci.pr}` : "PR";
  if (typeof ci.url === "string" && ci.url) link.setAttribute("href", ci.url); else link.removeAttribute("href");
  // #181: the server's elapsed is the anchor; the label ticks from it
  // between snapshots (renderCiClock) instead of jumping once per poll.
  state.ci = { snapshot: ci, receivedAt: Date.now() };
  renderCiClock();
  $("ci-note").textContent = ciNote(ci);
  const cells = Array.isArray(ci.checks) ? ci.checks : [];
  $("ci-cells").innerHTML = cells.map((check) => {
    const href = typeof check.link === "string" && check.link ? ` href="${escapeHtml(check.link)}" target="_blank" rel="noopener"` : "";
    return `<a class="ci-cell" data-state="${escapeHtml(check.state || "PENDING")}" title="${escapeHtml(check.state || "PENDING")}"${href}>${escapeHtml(ciCellLabel(check))}</a>`;
  }).join("");
}

// #181: every second while the watch is running, the elapsed shown is the
// server's elapsed plus the wall time since that snapshot arrived.
function renderCiClock(now = Date.now()) {
  if (!state.ci || !state.ci.snapshot) return;
  const ci = ciTicked(state.ci.snapshot, (now - state.ci.receivedAt) / 1000);
  $("ci-progress-label").textContent = ciProgressLabel(ci);
  const percent = ciBarPercent(ci);
  const bar = $("ci-bar");
  bar.classList.toggle("is-indeterminate", percent === null);
  bar.setAttribute("aria-valuenow", String(percent === null ? 0 : percent));
  $("ci-bar-fill").style.width = percent === null ? "" : `${percent}%`;
}

function renderLiveAge() {
  if (!state.live) return;
  const elapsed = state.liveReceivedAt ? (Date.now() - state.liveReceivedAt) / 1000 : 0;
  $("live-age").textContent = liveAgeLabel(state.live, elapsed);
}

function renderAgentOutput(output) {
  const panel = $("agent-output-panel");
  if (!panel) return;
  if (!output || !output.session_id) {
    panel.classList.add("hidden");
    state.agentOutputSession = null;
    state.agentOutputCursor = 0;
    return;
  }
  panel.classList.remove("hidden");
  const labels = {
    connected_no_output: "CONNECTED · NO OUTPUT",
    active_output: "OUTPUT ACTIVE",
    stale_heartbeat: "STALE HEARTBEAT",
    transport_disconnected: "TRANSPORT DISCONNECTED",
    failed: "PROCESS FAILED",
    completed: "SESSION COMPLETE",
  };
  // Legacy payload names remain documented for clients that still inspect the
  // source, while the rendered state contract is the five names above.
  const legacyStateLabels = { running_output: "", running_quiet: "", failed: "" };
  $("agent-output-state").textContent = labels[output.state] || String(output.state || "UNKNOWN").replaceAll("_", " ").toUpperCase();
  $("agent-output-state").className = `section-meta output-state-${escapeHtml(output.state || "unknown")}`;
  $("agent-output-meta").textContent = `${String(output.role || "agent").toUpperCase()} · ${output.adapter || "unknown adapter"} · ${output.session_id}`;
  $("agent-output-timing").textContent = `Started ${relativeTime(output.started_at)} · Elapsed ${Math.round(output.elapsed_seconds || 0)}s · Heartbeat ${Math.round(output.last_heartbeat_age_seconds || 0)}s ago · Output ${relativeTime(output.last_output_at)}`;
  const entries = Array.isArray(output.entries) ? output.entries : [];
  $("agent-output-log").textContent = entries.length
    ? entries.map((entry) => `${entry.stream === "stderr" ? "ERR" : "OUT"} ${entry.text}`).join("\n")
    : (output.state === "transport_disconnected" ? "Signal transport unavailable. Managed process state remains authoritative." : "No output has been recorded yet.");
  state.agentOutputSession = output.session_id;
  state.agentOutputCursor = Number(output.cursor) || 0;
  $("agent-output-log").scrollTop = $("agent-output-log").scrollHeight;
}

// The closed set of operation states the panel can show. A class per state
// keeps unknown payload values from reaching the DOM as a class name.
const OPERATION_STATE_LABELS = {
  waiting: ["operation-state-waiting", "WAITING ON DEPENDENCY"],
  timed_out: ["operation-state-timed_out", "TIMED OUT"],
  stale: ["operation-state-stale", "STALE TELEMETRY"],
  failed: ["operation-state-failed", "FAILED"],
  cancelled: ["operation-state-cancelled", "CANCELLED"],
  succeeded: ["operation-state-succeeded", "SUCCEEDED"],
  unavailable: ["operation-state-unavailable", "NO OPERATION TELEMETRY"],
};

function renderOperation(operation) {
  const panel = $("operation-panel");
  if (!panel) return;
  const value = operation || { availability: "unavailable" };
  const raw = value.availability === "unavailable" ? "unavailable" : value.assessment;
  const assessment = Object.prototype.hasOwnProperty.call(OPERATION_STATE_LABELS, raw) ? raw : "unavailable";
  const state = $("operation-state");
  const [stateClass, label] = OPERATION_STATE_LABELS[assessment];
  state.className = `section-meta ${stateClass}`;
  state.textContent = label;
  const current = value.current || {};
  const show = (item, suffix = "") => (item == null ? "n/a" : `${item}${suffix}`);
  const details = assessment === "unavailable"
    ? "No operation telemetry reported by this session."
    : `${value.dependency_class || "engine"} · ${current.dependency || "unknown"} / ${current.operation || "unknown"}`
      + ` · ${show(value.elapsed_seconds, "s")} / ${show(value.timeout_seconds, "s")}`
      + ` · attempt ${show(value.attempt)} · retries ${value.retry_count ?? 0}`
      + ` · last success ${value.last_success_at || "never"}`;
  $("operation-meta").textContent = details;
  const history = Array.isArray(value.history) ? value.history : [];
  $("operation-history").textContent = history.length
    ? history.map((item) => `${item.operation_id} · ${item.state} · attempt ${item.attempt}`).join("\n")
    : "No operation history.";
}

function metricDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "n/a";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// #clock: LCD counters tick locally from the ledger anchors, so they move
// every second between snapshots and freeze once the run is complete.
// lcdText comes from /lib/run-vocabulary.js, loaded before this file (#218).

function renderClocks() {
  const mission = $("mission-clock"); const phase = $("phase-clock");
  if (!mission || !phase) return;
  const anchors = state.clocks || {};
  const now = Date.now();
  // #193: the LCDs count awake time. The snapshot's asleep seconds (run and
  // current phase) are subtracted from the wall clock since the anchor; a
  // sleep that happens between snapshots is taken off at the next one.
  if (anchors.startedAt) {
    const end = anchors.endedAt ? new Date(anchors.endedAt).getTime() : now;
    mission.querySelector(".lcd-live").textContent = lcdText((end - new Date(anchors.startedAt).getTime()) / 1000 - (anchors.asleepSeconds || 0));
    mission.dataset.frozen = anchors.endedAt ? "true" : "false";
  }
  if (anchors.phaseStartedAt) {
    const end = anchors.endedAt ? new Date(anchors.endedAt).getTime() : now;
    phase.querySelector(".lcd-live").textContent = lcdText((end - new Date(anchors.phaseStartedAt).getTime()) / 1000 - (anchors.phaseAsleepSeconds || 0));
    phase.dataset.frozen = anchors.endedAt ? "true" : "false";
  }
}

function renderMetrics(metrics) {
  if (!metrics) return;
  const currentPhase = String(state.phaseNumber ?? "");
  state.clocks = { startedAt: metrics.started_at || null, endedAt: metrics.ended_at || null, phaseStartedAt: metrics.phase_started_at || null,
    asleepSeconds: metrics.asleep_seconds || 0, phaseAsleepSeconds: (metrics.phase_asleep_seconds || {})[currentPhase] || 0 };
  renderClocks();
  // #193: awake time, with the sleep named beside it when there was any
  const asleep = asleepLabel(metrics.asleep_seconds);
  $("metrics-elapsed").textContent = metricDuration(metrics.elapsed_seconds) + (asleep ? ` (${asleep})` : "");
  const clockNote = $("mission-clock-asleep");
  if (clockNote) clockNote.textContent = asleep;
  const phaseNote = $("phase-clock-asleep");
  if (phaseNote) phaseNote.textContent = asleepLabel((metrics.phase_asleep_seconds || {})[currentPhase]);
  $("metrics-sessions").textContent = String(metrics.managed_sessions || 0);
  $("metrics-failures").textContent = `${metrics.failed_sessions || 0} / ${metrics.replacement_count || 0}`;
  $("metrics-reviews").textContent = `${metrics.design_review_attempts || 0} / ${metrics.implementation_review_attempts || 0}`;
  $("metrics-verification").textContent = metricDuration(metrics.verification_seconds);
  $("metrics-pilot-wait").textContent = metricDuration(metrics.pilot_wait_seconds);
  const total = metrics.tokens?.total;
  // #184: until a session reports usage the token cell is one quiet line
  // naming who did not report, not a grid of UNKNOWN.
  const reported = total != null;
  const adapters = [...new Set((metrics.sessions || []).map((session) => session.adapter).filter(Boolean))];
  $("metrics-tokens").textContent = reported ? Number(total).toLocaleString() : "";
  const tokensCell = $("metrics-tokens-cell");
  if (tokensCell) tokensCell.classList.toggle("hidden", !reported);
  const note = $("metrics-tokens-note");
  if (note) {
    note.classList.toggle("hidden", reported);
    note.textContent = reported ? "" : `tokens: not reported by ${adapters.length ? adapters.join(", ") : "any session yet"}`;
  }
  $("metrics-token-state").textContent = reported
    ? `TOKEN SIGNAL · ${metrics.tokens?.coverage || ""}`
    : `TOKENS NOT REPORTED · ${metrics.tokens?.coverage || "0/0 sessions"}`;
  $("metrics-phase-list").innerHTML = Object.entries(metrics.phase_seconds || {})
    .filter(([phase, seconds]) => Number(seconds) > 0 || Number((metrics.phase_asleep_seconds || {})[phase]) > 0)
    .map(([phase, seconds]) => {
      const slept = asleepLabel((metrics.phase_asleep_seconds || {})[phase]);
      return `<span>PHASE ${escapeHtml(phase)} <strong>${escapeHtml(metricDuration(seconds))}</strong>${slept ? ` <small>${escapeHtml(slept)}</small>` : ""}</span>`;
    })
    .join("");
  $("metrics-session-list").innerHTML = (metrics.largest_sessions || [])
    .map((session) => `<span>${escapeHtml(String(session.role || "agent").toUpperCase())} · ${escapeHtml(session.adapter || "unknown")} · ${escapeHtml(session.model || "default")} <strong>${escapeHtml(metricDuration(session.duration_seconds))}</strong></span>`)
    .join("");
}

function renderRoleChiclets(activeRole, snapshot) {
  document.querySelectorAll("#role-chiclets .chiclet").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.role === activeRole);
    // #164: one faint word after the role name says who fills the station.
    const word = snapshot ? roleWord(el.dataset.role, snapshot) : null;
    let small = el.querySelector("small.chiclet-word");
    if (word) {
      if (!small) {
        small = document.createElement("small");
        small.className = "chiclet-word";
        el.appendChild(small);
      }
      small.textContent = word;
    } else if (small) {
      small.remove();
    }
    el.title = snapshot ? roleTitle(el.dataset.role, snapshot) : String(el.dataset.role || "").toUpperCase();
  });
}

function renderCrew(crew) {
  $("crew-list").innerHTML = crew.map((member) => `
    <div class="crew-member">
      <span>${escapeHtml(member.label)}</span>
      <div><strong class="${member.actor ? "" : "unassigned"}">${escapeHtml(member.actor || "Station vacant")}</strong><small>${escapeHtml(crewProfileLabel(member))}</small></div>
    </div>`).join("");
}

function renderReplacements(replacements, recoveries = [], implementerProgress = null) {
  const total = replacements.length + recoveries.length;
  // #184: the card exists once there is something in it.
  const panel = $("replacement-panel");
  if (panel) panel.classList.toggle("hidden", total === 0 && !implementerProgress);
  $("replacement-count").textContent = `${total} EVENT${total === 1 ? "" : "S"}`;
  // #215: the previous Implementer's ledgered account, so the pause says
  // what was finished before anyone reads a diff.
  const progressLine = progressSummaryLabel(implementerProgress);
  const progressRow = progressLine
    ? `<div class="replacement-item progress-item" data-progress-session="${escapeHtml(implementerProgress.session_id)}">
      <strong>IMPLEMENTER PROGRESS · ${escapeHtml(implementerProgress.session_id)}</strong>
      <p>${escapeHtml(progressLine)}</p>
    </div>`
    : "";
  const recoveryRows = recoveries.slice().reverse().map((item) => `
    <div class="replacement-item recovery-item" data-recovery-id="${escapeHtml(item.recovery_id)}">
      <strong>RECOVERY · ${escapeHtml(String(item.role || "agent").toUpperCase())} · ${escapeHtml(String(item.state || "unknown").replaceAll("_", " ").toUpperCase())} · ATTEMPT ${escapeHtml(item.attempt)}/${escapeHtml(item.cap)}</strong>
      <p>${escapeHtml(item.reason || "Continuity protocol")}</p>
    </div>`);
  const replacementRows = replacements.slice().reverse().map((replacement) => `
    <div class="replacement-item">
      <strong>${escapeHtml(replacementHeadline(replacement))}</strong>
      <p>${escapeHtml(replacementDetail(replacement))}</p>
    </div>`);
  $("replacement-list").innerHTML = total || progressRow
    ? [progressRow, ...recoveryRows, ...replacementRows].join("")
    : '<div class="attention-clear">No agent replacements or recoveries recorded.</div>';
}

function renderReviewAttempts(attempts = []) {
  const panel = $("review-attempts-panel");
  panel.classList.toggle("hidden", attempts.length === 0);
  $("review-attempt-count").textContent = `${attempts.length} ATTEMPT${attempts.length === 1 ? "" : "S"}`;
  $("review-attempt-list").innerHTML = attempts.length ? attempts.slice().reverse().map((attempt) => `
    <div class="replacement-item" data-review-attempt="${escapeHtml(attempt.attempt_id)}">
      <strong>ATTEMPT ${escapeHtml(attempt.attempt)} · ${escapeHtml(String(attempt.disposition || "unknown").replaceAll("_", " ").toUpperCase())}</strong>
      <p>${escapeHtml(String(attempt.trigger || "unknown").replaceAll("_", " "))} · ${escapeHtml(attempt.reviewer || "reviewer pending")} · ${escapeHtml(attempt.findings_count || 0)} finding(s)</p>
    </div>`).join("") : "";
}

// Real activity, not decoration: events per five-minute bucket over the
// last two hours, drawn as a HUD sparkline.
function renderActivitySpark(events) {
  const canvas = $("activity-spark");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  const width = canvas.width; const height = canvas.height;
  const buckets = new Array(24).fill(0);
  const now = Date.now();
  for (const event of events || []) {
    const age = now - new Date(event.at).getTime();
    if (!Number.isFinite(age) || age < 0 || age > 2 * 3600 * 1000) continue;
    buckets[23 - Math.min(23, Math.floor(age / (5 * 60 * 1000)))] += 1;
  }
  const peak = Math.max(1, ...buckets);
  ctx.clearRect(0, 0, width, height);
  const gap = 3; const bar = (width - gap * 23) / 24;
  buckets.forEach((count, index) => {
    const x = index * (bar + gap);
    const h = Math.max(2, Math.round((count / peak) * (height - 4)));
    ctx.fillStyle = count ? (index === 23 ? "#d7f6ff" : "rgba(95, 211, 255, .75)") : "rgba(148, 170, 190, .14)";
    ctx.fillRect(x, height - h, bar, h);
  });
  if (buckets[23]) { ctx.shadowColor = "#5fd3ff"; ctx.shadowBlur = 8; ctx.fillStyle = "#d7f6ff"; const x = 23 * (bar + gap); const h = Math.max(2, Math.round((buckets[23] / peak) * (height - 4))); ctx.fillRect(x, height - h, bar, h); ctx.shadowBlur = 0; }
}

function renderEvents(events, total) {
  $("event-total").textContent = `${total} EVENT${total === 1 ? "" : "S"}`;
  $("event-list").innerHTML = events.length ? events.map((event) => `
    <div class="event">
      <time datetime="${escapeHtml(event.at)}">${escapeHtml(relativeTime(event.at))}</time>
      <div><strong>${escapeHtml(cleanKind(event.kind))}</strong><p>${escapeHtml(eventMessage(event))}</p>${eventDetail(event) ? `<small>${escapeHtml(eventDetail(event))}</small>` : ""}</div>
    </div>`).join("") : '<div class="empty-row">Flight Log clear. No events recorded.</div>';
}

function renderVerifications(records, total) {
  $("verification-total").textContent = `${total} RUN${total === 1 ? "" : "S"}`;
  $("verification-list").innerHTML = records.length ? records.map((record) => `
    <div class="verification">
      <time datetime="${escapeHtml(record.at)}">${escapeHtml(relativeTime(record.at))}</time>
      <div>
        <strong class="verification-status ${record.ok ? "" : "failed"}">${record.ok ? "PASS" : "FAIL"} · ${escapeHtml(cleanKind(record.kind))}${verificationExecutionState(record) ? ` <span class="execution-pill ${escapeHtml(verificationExecutionState(record))}">${escapeHtml(verificationExecutionLabel(record))}</span>` : ""}</strong>
        <p>${escapeHtml((record.criteria || []).join(", ") || "No criterion")} · ${escapeHtml(record.by || "Unknown actor")}</p>
      </div>
    </div>`).join("") : '<div class="empty-row">No diagnostic evidence recorded.</div>';
}

function render(snapshot) {
  state.lastGenerated = snapshot.generated_at;
  $("last-sync").textContent = `SYNCED ${relativeTime(snapshot.generated_at).toUpperCase()}`;
  state.actors = snapshot.actors || {};
  state.activeRole = snapshot.actors?.active_role || null;
  state.latestEvent = snapshot.events?.[0] || null;
  state.runSessions = snapshot.runtime?.current_sessions || {};
  state.crew = snapshot.crew || [];
  state.replacements = snapshot.runtime?.replacements || [];
  if (snapshot.settings) {
    state.settings = snapshot.settings;
    if (!$("settings-dialog").open || !state.settingsDirty) populateAgentSettings();
  }
  if (!snapshot.initialized) {
    renderLive(null);
    state.ci = null;
    renderCi(null);
    renderAgentOutput(null);
    renderOperation(null);
    state.inputRequired = false;
    state.alertSignature = null;
    document.body.classList.remove("input-is-required");
    document.title = "Handsoff // E.V.E. Mission Control";
    $("active-state").classList.add("hidden");
    $("empty-state").classList.remove("hidden");
    $("empty-message").textContent = snapshot.error || "No Mission Objective detected. Initialize a Ship Feature to begin.";
    renderOperatorActions([]);
    setFaviconState("idle");
    return;
  }

  $("empty-state").classList.add("hidden");
  $("active-state").classList.remove("hidden");
  const status = snapshot.status;
  state.lastRunStatus = status.status;
  document.body.classList.toggle("is-closed", status.status === "closed");
  const acceptance = snapshot.acceptance;
  const supervisor = snapshot.supervisor;
  const policy = snapshot.policy;
  const progress = Math.max(0, Math.min(100, Number(status.progress) || 0));

  renderLive(snapshot.live);
  renderAdaptiveRouting(snapshot.adaptive_routing || null);
  renderCi(snapshot.ci || null);
  const consistency = snapshot.status?.consistency_errors || [];
  $("consistency-fault").classList.toggle("hidden", !consistency.length);
  $("consistency-fault-message").textContent = consistency.join("; ");
  renderAgentOutput(snapshot.runtime?.agent_output || null);
  renderOperation(snapshot.runtime?.operation || null);
  state.phaseNumber = snapshot.status?.phase_number ?? null;
  renderMetrics(snapshot.metrics || null);
  renderInputAlert(snapshot.input_required, snapshot.project.feature, snapshot.regression);
  renderOperations(snapshot.operations || {}, snapshot.operator_actions || []);
  renderRegression(snapshot.regression);
  if (!state.inputRequired) document.title = `${snapshot.project.feature} · Handsoff`;
  const pilotGate = snapshot.input_required?.turn === "pilot" && !snapshot.input_required?.preauthorized;
  setFaviconState(status.status === "complete" ? "complete"
    : pilotGate ? "blocked" : "in_progress");
  $("project-name").textContent = snapshot.project.name.toUpperCase();
  const projectLogo = $("project-logo");
  if (snapshot.project.logo_url) {
    if (projectLogo.getAttribute("src") !== snapshot.project.logo_url) projectLogo.src = snapshot.project.logo_url;
    projectLogo.alt = snapshot.project.name;
  }
  projectLogo.classList.toggle("hidden", !snapshot.project.logo_url);
  $("feature-name").textContent = snapshot.project.feature;
  $("project-root").textContent = snapshot.root;
  // #161: the engine is shown once, in the ENGINE badge where the operator
  // looks first; the old eyebrow copy next to the project name is gone.
  const badge = $("engine-badge");
  if (badge) {
    const version = snapshot.engine?.version;
    badge.textContent = `ENGINE ${version && version !== "unknown" ? version : "UNKNOWN"}`;
    badge.title = snapshot.engine?.source ? `Engine this run uses (${snapshot.engine.source})` : "Engine this run uses";
  }
  // #186: which host drives the run, from an actor prefix, never a guess.
  const hostBadge = $("host-badge");
  if (hostBadge) {
    hostBadge.textContent = hostBadgeLabel(snapshot.host);
    hostBadge.title = hostBadgeTitle(snapshot.host);
    hostBadge.dataset.family = snapshot.host?.family || "unknown";
  }
  $("mission-state").textContent = String(status.status || "unknown").replaceAll("_", " ").toUpperCase();
  const complete = progress >= 100;
  $("progress-value").textContent = Math.round(progress);
  $("progress-ring").style.setProperty("--progress", `${progress * 3.6}deg`);
  $("progress-ring").classList.toggle("is-complete", complete);
  $("phase-kicker").textContent = complete ? "MISSION COMPLETE" : `PHASE ${status.phase_number} OF 8`;
  $("phase-name").textContent = status.phase;
  $("status-updated").textContent = `State updated ${relativeTime(status.updated_at)}`;
  $("design-rounds").textContent = `${policy.design_round} / ${policy.max_design_rounds}`;
  $("review-rounds").textContent = reviewRoundLabel(policy);
  $("design-review-budget").textContent = designReviewBudgetLabel(policy);
  $("design-review-packet").textContent = designReviewPacketLabel(snapshot.design_review_packet);
  $("design-reviewer-profile").textContent = designReviewerProfileLabel(policy.design_reviewer_selection);
  $("evidence-runs").textContent = snapshot.audit.verification_runs;

  renderPhases(snapshot.phases, snapshot.verification);
  renderDesignDocument(snapshot);

  $("supervisor-panel").className = `briefing ${supervisor.tone}`;
  $("briefing-state").textContent = supervisor.label.toUpperCase();
  // #147/#148: the briefing label and headline come from the server (one
  // wording for CLI and dashboard); a live verification in flight or just
  // failed outranks every other headline while the run is at Phase 7.
  const verificationHeadline = status.phase_number === 7 ? liveVerificationHeadline(snapshot.verification) : null;
  $("supervisor-headline").textContent = verificationHeadline || supervisor.headline;
  $("supervisor-summary").textContent = supervisor.summary;
  $("supervisor-next").textContent = supervisor.next_action;
  // #184: the fixed reassurance copy is gone, and the label only shows
  // when it says something ("Trajectory stable" on a steady run does not).
  $("briefing-state").classList.toggle("hidden", supervisor.tone === "steady");

  $("acceptance-score").textContent = `${acceptance.passing} / ${acceptance.total}`;
  stateClass($("acceptance-score"), acceptance.passing === acceptance.total && acceptance.total ? "is-good" : acceptance.failing || acceptance.blocked ? "is-bad" : "is-warning");
  $("symptom-state").textContent = acceptance.original_symptom_resolved ? "RESOLVED" : "OPEN";
  $("symptom-detail").textContent = acceptance.original_symptom_resolved ? "neutralization confirmed" : "neutralization unconfirmed";
  stateClass($("symptom-state"), acceptance.original_symptom_resolved ? "is-good" : "is-warning");
  $("audit-state").textContent = snapshot.audit.healthy ? "INTACT" : "BLOCKED";
  // #185: an engine identity that could not be read is one line here, never a dead page.
  const engineError = typeof snapshot.audit.engine_error === "string" && snapshot.audit.engine_error ? ` · engine: ${snapshot.audit.engine_error}` : "";
  $("audit-detail").textContent = (snapshot.audit.healthy ? `${snapshot.audit.event_count} chained events verified` : `${snapshot.audit.gate_errors.length + snapshot.audit.chain_errors.length} integrity issue(s)`) + engineError;
  stateClass($("audit-state"), snapshot.audit.healthy ? "is-good" : "is-bad");
  $("approval-state").textContent = status.deployment_approved ? "APPROVED" : "PENDING";
  $("approval-detail").textContent = status.deployment_approved ? `by ${status.deployment_approved.by}` : policy.explicit_approval ? "Pilot authorization required" : "approval interlock disabled";
  stateClass($("approval-state"), status.deployment_approved ? "is-good" : "is-warning");

  renderCriteria(acceptance.criteria);
  renderWorkItems(snapshot.work_items || { items: [], multi: false });
  renderTranche(snapshot.tranche || null);
  renderDesignEvidence(snapshot.design_evidence || []);
  renderAmendment(snapshot.amendment || null);
  renderQuestions(snapshot.questions || null);
  renderAttention(supervisor.attention);
  renderCrew(state.crew);
  renderReplacements(state.replacements, snapshot.recovery?.attempts || [], snapshot.recovery?.implementer_progress || null);
  renderReviewAttempts(snapshot.review?.attempts || []);
  renderRoleChiclets(status.status === "closed" ? null : snapshot.actors.active_role, snapshot);
  renderEvents(snapshot.events, snapshot.audit.event_count);
  renderActivitySpark(snapshot.events);
  renderVerifications(snapshot.verifications, snapshot.audit.verification_runs);
  if (!document.body.classList.contains("is-booted")) window.setTimeout(() => document.body.classList.add("is-booted"), 30);
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
    state.offlineSince = null;
    renderOffline();
  } catch (error) {
    state.failures += 1;
    if (!state.offlineSince) state.offlineSince = new Date();
    renderOffline();
    if (state.failures === 1) showError(`Tactical telemetry interrupted: ${error.message}`);
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      window.queueMicrotask(refresh);
    }
  }
}

function renderOffline() {
  // A completed or closed run releases its own dashboard (advance 8 frees
  // the owned port), so losing the server afterwards is the expected end,
  // not an outage: the celebration stays and the page is the final
  // snapshot. Anything else going dark is an outage.
  const final = Boolean(state.offlineSince) && ["complete", "closed"].includes(state.lastRunStatus);
  const outage = Boolean(state.offlineSince) && !final;
  document.body.classList.toggle("is-offline", outage);
  document.body.classList.toggle("is-final", final);
  // Cancel what is already running too: the stylesheet rule stops new
  // animations, but Chrome keeps one alive inside a closed details element.
  if (outage && typeof document.getAnimations === "function") document.getAnimations().forEach((animation) => animation.cancel());
  // A stepper node cannot be "active" while nobody is serving the run; the
  // next successful snapshot re-renders the rail from the server.
  if (outage) {
    document.querySelectorAll("#phase-rail .phase-node.active").forEach((node) => {
      node.classList.remove("active");
      node.classList.add("held");
    });
  }
  const banner = $("offline-banner");
  if (!banner) return;
  const stamp = state.offlineSince ? state.offlineSince.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  banner.textContent = final
    ? `MISSION ${state.lastRunStatus === "closed" ? "CLOSED" : "COMPLETE"} · this run's dashboard was released at ${stamp}; this page is the final snapshot`
    : outage ? `DASHBOARD OFFLINE since ${stamp}: the server on this port is not answering` : "";
  banner.classList.toggle("is-final", final);
  banner.classList.toggle("hidden", !state.offlineSince);
}

function setConnectionStatus(label) {
  $("connection-status").textContent = label;
}

function connectEventStream() {
  if (!("EventSource" in window)) {
    setConnectionStatus("LINK: POLLING");
    return;
  }
  const events = new EventSource("/api/events");
  state.eventStream = events;
  events.addEventListener("ready", () => {
    setConnectionStatus("LINK: LIVE");
    refresh();
  });
  events.addEventListener("invalidate", refresh);
  events.onopen = () => setConnectionStatus("LINK: LIVE");
  events.onerror = () => {
    setConnectionStatus("LINK: RECONNECTING");
    if (Date.now() - state.lastStreamRefresh >= 5000) {
      state.lastStreamRefresh = Date.now();
      refresh();
    }
  };
}

refresh();
connectEventStream();
$("enable-alerts").addEventListener("click", enableDesktopAlerts);
$("design-approve").addEventListener("click", authorizeDesign);
$("deployment-approve").addEventListener("click", authorizeDeployment);
$("design-review-authorize").addEventListener("click", authorizeDesignReviewAttempt);
$("amendment-approve").addEventListener("click", approveAmendment);
$("pilot-note-form").addEventListener("submit", sendPilotNote);
$("mission-init-form").addEventListener("submit", initializeMission);
bindConsoleTabs();
$("regression-accept").addEventListener("click", () => decideRegression("accept"));
$("regression-decline").addEventListener("click", () => decideRegression("decline"));
$("settings-toggle").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", closeSettings);
$("settings-cancel").addEventListener("click", closeSettings);
$("settings-form").addEventListener("submit", saveAgentSettings);
$("features-save").addEventListener("click", saveFeatureSettings);
$("settings-dialog").addEventListener("cancel", () => {
  state.settingsDirty = false;
  $("settings-result").textContent = "";
});
for (const role of AGENT_ROLES) {
  for (const id of [`agent-${role}`, `agent-${role}-model`]) {
    $(id).addEventListener("input", () => {
      markSettingsDirty();
    });
  }
}
$("max-failovers").addEventListener("input", markSettingsDirty);
window.setInterval(refresh, 5000);
window.addEventListener("focus", refresh);
window.addEventListener("pageshow", refresh);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});
window.setInterval(() => {
  if (state.lastGenerated) $("last-sync").textContent = `SYNCED ${relativeTime(state.lastGenerated).toUpperCase()}`;
}, 1000);
window.setInterval(renderLiveAge, 1000);
window.setInterval(renderCiClock, 1000);
window.setInterval(renderClocks, 1000);
window.setInterval(() => {
  if (!state.inputRequired) return;
  state.titleFlip = !state.titleFlip;
  document.title = state.pilotGate && state.titleFlip ? "🔴 PILOT AUTHORIZATION REQUIRED" : `${state.feature} · Handsoff`;
}, 900);
