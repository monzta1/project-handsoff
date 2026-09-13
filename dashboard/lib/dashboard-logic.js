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
  if (!actor) return "THIS RUN: station not assigned · profile not recorded";
  return `THIS RUN: profile not recorded · ${actor}`;
}

function cleanKind(value) {
  return String(value || "event").replaceAll("_", " ");
}

function eventMessage(event) {
  return event?.message || "Recorded state transition";
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
