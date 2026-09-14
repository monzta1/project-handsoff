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
// execution helpers (verificationExecutionState, verificationExecutionLabel), and the fallback-list helpers come from
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

function renderRegression(regression) {
  const current = regression?.current || null;
  const last = regression?.last || null;
  const card = $("regression-alert");
  card.classList.toggle("hidden", !current && !last);
  $("regression-status-details").textContent = regressionRecordText(current || last);
  $("regression-last").textContent = last
    ? `Last closed request: ${last.group} · ${String(last.state || "unknown").toUpperCase()} · ${last.completed_at || last.decided_at || last.requested_at}`
    : "";
}

function renderInputAlert(inputRequest, feature, regression) {
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
  const deploymentButton = $("deployment-approve");
  const regressionAccept = $("regression-accept");
  const regressionDecline = $("regression-decline");
  approvalButton.classList.toggle("hidden", state.inputKind !== "design_approval");
  deploymentButton.classList.toggle("hidden", state.inputKind !== "deployment_approval");
  regressionAccept.classList.toggle("hidden", state.inputKind !== "regression_approval");
  regressionDecline.classList.toggle("hidden", state.inputKind !== "regression_approval");
  state.regressionRequest = regression?.pending || null;
  const regressionDetails = $("regression-details");
  regressionDetails.classList.toggle("hidden", state.inputKind !== "regression_approval");
  regressionDetails.textContent = regressionRecordText(state.regressionRequest);
  if (state.inputKind === "design_approval" && signature !== state.alertSignature) {
    approvalButton.disabled = false;
    approvalButton.textContent = "AUTHORIZE DESIGN";
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

  if (required && signature !== state.alertSignature && "Notification" in window && Notification.permission === "granted") {
    new Notification("E.V.E. requests Pilot authorization", {
      body: message,
      tag: "handsoff-input-required",
    });
  }
  state.alertSignature = signature;
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

function renderWorkItems(workItems) {
  const items = workItems?.items || [];
  const visible = Boolean(workItems?.multi);
  const panel = $("ticket-panel");
  panel.classList.toggle("hidden", !visible);
  $("ticket-total").textContent = `${items.length} ITEM${items.length === 1 ? "" : "S"}`;
  $("ticket-list").innerHTML = items.map((item) => `
    <tr data-item-id="${escapeHtml(item.id)}">
      <td><code>${escapeHtml(item.id)}</code></td>
      <td>${item.number ? `#${escapeHtml(item.number)}` : "—"}</td>
      <td>${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>` : escapeHtml(item.title)}${item.discrepancy ? `<span class="ticket-state blocked" title="${escapeHtml(item.discrepancy)}">DISCREPANT</span>` : ""}</td>
      <td><span class="ticket-state ${escapeHtml(item.status)}">${escapeHtml(String(item.status).replaceAll("_", " "))}</span></td>
      <td>${escapeHtml(item.phase_or_next || "—")}</td>
      <td>${escapeHtml(item.blocker || "—")}</td>
      <td>${escapeHtml(relativeTime(item.updated_at))}</td>
    </tr>`).join("");
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
  if (!visible) {
    $("questions-list").innerHTML = "";
    $("questions-state").textContent = "NONE OPEN";
    return;
  }
  const open = questions.open || [];
  const blocking = questions.blocking || [];
  $("questions-state").textContent = blocking.length ? "PILOT ANSWER REQUIRED" : (open.length ? "OPEN" : "ANSWERED");
  const rows = [...open, ...(questions.answered || []).slice().reverse()];
  $("questions-list").innerHTML = rows.map((q) => `
    <div class="question-row ${q.answer == null ? "is-open" : "is-answered"}">
      <div class="question-meta"><span class="question-pill">${escapeHtml(questionLabel(q))}</span><span class="question-time">${escapeHtml(relativeTime(q.asked_at))}</span></div>
      <p class="question-text">${escapeHtml(q.text)}${q.truncated ? " (truncated)" : ""}</p>
      ${q.answer == null
        ? `<form class="question-form" data-question-id="${escapeHtml(q.question_id)}"><textarea class="question-input" rows="2" placeholder="Answer for the ${escapeHtml(q.role)}" required></textarea><button type="submit" class="ghost-button">SEND ANSWER</button></form>`
        : `<p class="question-answer"><strong>${escapeHtml(q.answered_by)}:</strong> ${escapeHtml(q.answer)}</p>`}
    </div>`).join("");
  panel.querySelectorAll(".question-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      answerQuestion(form.dataset.questionId, form.querySelector(".question-input").value, form);
    });
  });
}

async function answerQuestion(questionId, text, form) {
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

function renderLiveAge() {
  if (!state.live) return;
  const elapsed = state.liveReceivedAt ? (Date.now() - state.liveReceivedAt) / 1000 : 0;
  $("live-age").textContent = liveAgeLabel(state.live, elapsed);
}

function renderRoleChiclets(activeRole) {
  document.querySelectorAll("#role-chiclets .chiclet").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.role === activeRole);
  });
}

function renderCrew(crew) {
  $("crew-list").innerHTML = crew.map((member) => `
    <div class="crew-member">
      <span>${escapeHtml(member.label)}</span>
      <div><strong class="${member.actor ? "" : "unassigned"}">${escapeHtml(member.actor || "Station vacant")}</strong><small>${escapeHtml(crewProfileLabel(member))}</small></div>
    </div>`).join("");
}

function renderReplacements(replacements, recoveries = []) {
  const total = replacements.length + recoveries.length;
  $("replacement-count").textContent = `${total} EVENT${total === 1 ? "" : "S"}`;
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
  $("replacement-list").innerHTML = total
    ? [...recoveryRows, ...replacementRows].join("")
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

  renderLive(snapshot.live);
  renderInputAlert(snapshot.input_required, snapshot.project.feature, snapshot.regression);
  renderRegression(snapshot.regression);
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
  $("review-rounds").textContent = reviewRoundLabel(policy);
  $("design-review-budget").textContent = designReviewBudgetLabel(policy);
  $("design-review-packet").textContent = designReviewPacketLabel(snapshot.design_review_packet);
  $("design-reviewer-profile").textContent = designReviewerProfileLabel(policy.design_reviewer_selection);
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
  renderWorkItems(snapshot.work_items || { items: [], multi: false });
  renderDesignEvidence(snapshot.design_evidence || []);
  renderAmendment(snapshot.amendment || null);
  renderQuestions(snapshot.questions || null);
  renderAttention(supervisor.attention);
  renderCrew(state.crew);
  renderReplacements(state.replacements, snapshot.recovery?.attempts || []);
  renderReviewAttempts(snapshot.review?.attempts || []);
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
$("deployment-approve").addEventListener("click", authorizeDeployment);
$("regression-accept").addEventListener("click", () => decideRegression("accept"));
$("regression-decline").addEventListener("click", () => decideRegression("decline"));
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
window.setInterval(() => {
  if (!state.inputRequired) return;
  state.titleFlip = !state.titleFlip;
  document.title = state.titleFlip ? "🔴 PILOT AUTHORIZATION REQUIRED" : `${state.feature} · Handsoff`;
}, 900);
