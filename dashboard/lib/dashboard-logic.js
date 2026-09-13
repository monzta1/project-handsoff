// Pure display/logic helpers shared by dashboard/app.js and its tests
// (tests/dashboard/*.test.js). No DOM access here so this file loads
// unmodified in both a <script> tag (as globals) and plain Node via
// require() -- see the module.exports guard at the bottom.

function adapterLabel(adapter) {
  if (adapter === "codex") return "Codex";
  if (adapter === "claude") return "Claude Code";
  return adapter || "NONE DETECTED";
}

function effectiveProfileLabel(profile) {
  const adapter = adapterLabel(profile?.adapter);
  const model = profile?.model || "default";
  if (model === "default") {
    return `${adapter} · runner default (exact model not exposed)`;
  }
  return `${adapter} · ${model}`;
}

function actorForRole(role, { actors, runSessions, activeRole, latestEvent } = {}) {
  const sessionActor = runSessions?.[role]?.actor;
  if (sessionActor) return sessionActor;
  const recorded = {
    architect: actors?.architect,
    implementer: actors?.implemented_by,
    reviewer: actors?.reviewed_by,
  }[role];
  if (recorded) return recorded;
  if (role === activeRole && latestEvent?.kind === "heartbeat") {
    return latestEvent.by || null;
  }
  return null;
}

function runProfileLabel(role, context = {}) {
  const actor = actorForRole(role, context);
  const session = context.runSessions?.[role];
  if (session) {
    const requested = session.requested_model === "default"
      ? "requested runner default"
      : `requested ${session.requested_model}`;
    const reported = session.reported_model
      ? `reported ${session.reported_model}`
      : "exact model not reported";
    return `THIS RUN: ${adapterLabel(session.adapter)} · ${requested} · ${reported} · ${session.actor} · ${session.session_id} · ${String(session.state || "unknown").replaceAll("_", " ").toUpperCase()}`;
  }
  if (!actor) return "THIS RUN: station not assigned · no managed session recorded";
  return `THIS RUN: external/manual launch · provider, model, and session not recorded · ${actor}`;
}

function cleanKind(value) {
  return String(value || "event").replaceAll("_", " ");
}

function eventMessage(event) {
  return event?.message || "Recorded state transition";
}

function runtimeProfileLabel(session) {
  if (!session) return "External/manual launch · provider, model, and session not recorded";
  const requested = session.requested_model === "default"
    ? "runner default"
    : (session.requested_model || "requested model not recorded");
  const reported = session.reported_model || "exact model not reported";
  return `${adapterLabel(session.adapter)} · ${requested} · ${reported} · ${session.session_id} · ${String(session.state || "unknown").replaceAll("_", " ").toUpperCase()}`;
}

function crewProfileLabel(member) {
  if (member?.key === "approver") return member?.actor ? "Human authorization" : "Authorization not recorded";
  if (!member?.actor) return "Station vacant · no managed session recorded";
  return runtimeProfileLabel(member.session);
}

function replacementHeadline(replacement) {
  const role = String(replacement?.role || "agent").toUpperCase();
  const state = String(replacement?.state || "unknown").replaceAll("_", " ").toUpperCase();
  return `${role} · ${state} · ATTEMPT ${replacement?.attempt ?? "?"}/${replacement?.cap ?? "?"}`;
}

function replacementDetail(replacement) {
  const from = replacement?.from_profile
    ? `${adapterLabel(replacement.from_profile.adapter)} ${replacement.from_profile.requested_model || "default"} (${replacement.from_session_id})`
    : (replacement?.from_session_id || "source session not recorded");
  const targetProfile = replacement?.to_profile || replacement?.selected_profile;
  const to = targetProfile
    ? `${adapterLabel(targetProfile.adapter)} ${targetProfile.requested_model || targetProfile.model || "default"}${replacement?.to_session_id ? ` (${replacement.to_session_id})` : ""}`
    : "Pilot review";
  const trigger = String(replacement?.trigger || "unknown trigger").replaceAll("_", " ");
  const category = String(replacement?.category || "unknown category").replaceAll("_", " ");
  return `${from} → ${to} · ${trigger} / ${category}`;
}

function eventDetail(event) {
  if (!String(event?.kind || "").startsWith("agent_replacement_")) return "";
  const transition = [event.from_session_id, event.to_session_id].filter(Boolean).join(" → ");
  const parts = [
    event.role,
    event.category,
    Number.isInteger(event.attempt) ? `attempt ${event.attempt}` : null,
    transition || null,
    event.replacement_state,
  ];
  return parts.filter(Boolean).map((part) => String(part).replaceAll("_", " ")).join(" · ");
}

// REQ-002: the Auto-detect <option> label always names the live resolved
// adapter, but the stored/selected value must stay "auto" -- never get
// silently rewritten to that resolved adapter.
function autoDetectOptionLabel(defaultAdapter) {
  return `Auto-detect (currently ${adapterLabel(defaultAdapter)})`;
}

const ALLOWED_ADAPTERS = ["auto", "codex", "claude"];

// Returns the value the role's <select> should hold: one of
// ALLOWED_ADAPTERS, or null when the stored adapter is a custom value the
// dropdown must present as a distinct disabled placeholder instead of
// silently coercing it to a listed option.
function resolveAgentSelectValue(storedAdapter) {
  const value = storedAdapter === "configure-me" ? "auto" : storedAdapter;
  return ALLOWED_ADAPTERS.includes(value) ? value : null;
}

// REQ-007: pure, boundary-safe manipulation of a per-role fallback list.
// Each function mutates `entries` in place (matching the array-by-reference
// style the dashboard's render loop already relies on) and returns whether
// it actually changed anything, so callers can skip a re-render on a no-op.
const MAX_FALLBACK_ENTRIES = 8;

function addFallbackEntry(entries) {
  if (entries.length >= MAX_FALLBACK_ENTRIES) return false;
  entries.push({ adapter: "codex", model: "default" });
  return true;
}

function moveFallbackEntry(entries, index, offset) {
  const target = index + offset;
  if (index < 0 || index >= entries.length || target < 0 || target >= entries.length) {
    return false;
  }
  const [item] = entries.splice(index, 1);
  entries.splice(target, 0, item);
  return true;
}

function removeFallbackEntry(entries, index) {
  if (index < 0 || index >= entries.length) return false;
  entries.splice(index, 1);
  return true;
}

function buildFallbackDraft(fallbacks, roles) {
  return Object.fromEntries(roles.map((role) => [
    role, (fallbacks?.[role] || []).map((profile) => ({ ...profile })),
  ]));
}

function serializeFallbackDraft(fallbackDraft, roles) {
  return Object.fromEntries(roles.map((role) => [
    role, (fallbackDraft[role] || []).map((profile) => ({ ...profile })),
  ]));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    adapterLabel,
    effectiveProfileLabel,
    actorForRole,
    runProfileLabel,
    cleanKind,
    eventMessage,
    runtimeProfileLabel,
    crewProfileLabel,
    replacementHeadline,
    replacementDetail,
    eventDetail,
    autoDetectOptionLabel,
    resolveAgentSelectValue,
    ALLOWED_ADAPTERS,
    MAX_FALLBACK_ENTRIES,
    addFallbackEntry,
    moveFallbackEntry,
    removeFallbackEntry,
    buildFallbackDraft,
    serializeFallbackDraft,
  };
}
