// Pure display/logic helpers shared by dashboard/app.js and its tests
// (tests/dashboard/*.test.js). No DOM access here so this file loads
// unmodified in both a <script> tag (as globals) and plain Node via
// require() -- see the module.exports guard at the bottom.

// #215: one line for the replacement pause: what the previous Implementer
// session said it finished, from its ledgered account, never from its
// transcript. Empty when there is no such account.
function progressSummaryLabel(progress) {
  if (!progress || typeof progress !== "object" || !progress.summary) return "";
  const summary = progress.summary;
  const parts = [];
  const done = Array.isArray(summary.done) ? summary.done : [];
  const partial = Array.isArray(summary.partial) ? summary.partial : [];
  const untouched = Array.isArray(summary.untouched) ? summary.untouched : [];
  if (done.length) {
    const withTests = done.filter((c) => progress.tests && progress.tests[c]);
    parts.push(`${done.join(", ")} done${withTests.length === done.length ? " with tests" : withTests.length ? ` (${withTests.length} with tests)` : ""}`);
  }
  if (partial.length) parts.push(`${partial.join(", ")} partial`);
  if (untouched.length) parts.push(`${untouched.join(", ")} untouched`);
  return parts.join("; ");
}

// #164: one word per role chiclet naming the agent family at that station.
// The station's recorded actor wins (claude-* or codex-*, exactly, so a
// finished station keeps its word); the configured adapter is the fallback
// for a station not yet filled; nothing known reads nothing.
const ROLE_WORD_PREFIXES = ["claude-", "codex-"];

function roleStation(role, snapshot) {
  const crew = Array.isArray(snapshot?.crew) ? snapshot.crew : [];
  let key = role;
  if (role === "reviewer") {
    const phase = Number(snapshot?.status?.phase_number);
    key = phase === 1 || phase === 2 ? "design_reviewer" : "reviewer";
  }
  return crew.find((member) => member && member.key === key) || null;
}

function roleFamily(role, snapshot) {
  const actor = roleStation(role, snapshot)?.actor;
  if (typeof actor === "string") {
    const prefix = ROLE_WORD_PREFIXES.find((candidate) => actor.startsWith(candidate));
    if (prefix) return prefix.slice(0, -1);
  }
  const adapter = snapshot?.settings?.crew?.[role]?.adapter;
  // #186: a station filled by the host reads the host's family (claude,
  // codex) when the snapshot knows it, never the word "host".
  if (adapter === "host") {
    const family = snapshot?.host?.family;
    return typeof family === "string" && family && family !== "unknown" ? family : "host";
  }
  return typeof adapter === "string" && adapter.trim() ? adapter.trim() : null;
}

// #199: whether the station is the host at the keyboard or a managed
// session the host launched. "host" when its configured adapter is host;
// "managed" when it carries a recorded actor with a family prefix (the
// name a managed session is given); null when neither is known.
function roleStationKind(role, snapshot) {
  if (snapshot?.settings?.crew?.[role]?.adapter === "host") return "host";
  const actor = roleStation(role, snapshot)?.actor;
  if (typeof actor === "string" && ROLE_WORD_PREFIXES.some((candidate) => actor.startsWith(candidate))) return "managed";
  return null;
}

function roleWord(role, snapshot) {
  const family = roleFamily(role, snapshot);
  if (!family) return null;
  const kind = roleStationKind(role, snapshot);
  // an unknown host family already reads "host"; do not say it twice
  return kind && family !== kind ? `${family} · ${kind}` : family;
}

// #186: the topbar badge text for the host that drives the run.
function hostBadgeLabel(host) {
  const family = host && typeof host.family === "string" ? host.family : "unknown";
  return `HOST ${family === "claude" || family === "codex" ? family.toUpperCase() : "UNKNOWN"}`;
}

function hostBadgeTitle(host) {
  if (!host || !host.actor) return "Host driving this run: not recorded (init --by)";
  return `Host driving this run: ${host.actor} (${host.source})`;
}

function roleTitle(role, snapshot) {
  const profile = snapshot?.settings?.crew?.[role] || {};
  const parts = [String(role).toUpperCase(), roleWord(role, snapshot), profile.model, profile.adapter_source];
  return parts.filter((part) => typeof part === "string" && part.trim()).join(" · ");
}

function adapterLabel(adapter) {
  if (adapter === "codex") return "Codex";
  if (adapter === "claude") return "Claude Code";
  return adapter || "NONE DETECTED";
}

function friendlyActorLabel({ role, purpose, actor } = {}) {
  const key = String(role || "").toLowerCase();
  const mission = String(purpose || "").toLowerCase();
  if (key.includes("reviewer") || mission.includes("review")) {
    return mission.includes("design") || key === "design_reviewer" ? "Design Reviewer" : "Implementation Reviewer";
  }
  if (key === "architect") return "Solution Architect";
  if (key === "supervisor") return "Mission Supervisor";
  if (key === "implementer") return "Implementation Engineer";
  if (key === "approver" || /pilot/i.test(String(actor || ""))) return "Mission Pilot";
  if (typeof actor === "string" && actor.trim()) {
    return actor.trim().replace(/^(claude|codex)[-_]/i, "").replaceAll(/[-_]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  return "Agent";
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
    return `THIS RUN: ${adapterLabel(session.adapter)} · ${requested} · ${reported} · ${friendlyActorLabel({ role, actor: session.actor })} · ${session.session_id} · ${String(session.state || "unknown").replaceAll("_", " ").toUpperCase()}`;
  }
  if (!actor) return "THIS RUN: station not assigned · no managed session recorded";
  return `THIS RUN: external/manual launch · provider, model, and session not recorded · ${friendlyActorLabel({ role, actor })}`;
}

// #35: the design-review attempt budget, derived from the structured
// policy fields only (never parsed out of next_action or any prose), so
// the label can never disagree with what the supervisor enforces.
function designReviewBudgetLabel(policy) {
  const attempts = Number.isInteger(policy?.design_review_attempts) ? policy.design_review_attempts : 0;
  const limit = Number.isInteger(policy?.max_autonomous_design_reviews) ? policy.max_autonomous_design_reviews : 0;
  const authorization = policy?.design_review_authorization;
  const authorized = authorization && typeof authorization === "object" && authorization.consumed_at === null;
  const suffix = authorized ? ` · attempt ${authorization.attempt_permitted} authorized` : "";
  return `Design review ${attempts}/${limit}${suffix}`;
}

// #36: one compact line for the latest delta review packet, derived from
// the snapshot's counts only (the packet's finding text never reaches the
// dashboard). Null or a legacy snapshot without the field reads as no packet.
function designReviewPacketLabel(packet) {
  if (!packet || typeof packet !== "object" || !Number.isInteger(packet.attempt)) {
    return "Delta packet: none (first review gets the full task)";
  }
  const findings = packet.findings && typeof packet.findings === "object" ? packet.findings : {};
  const count = (value) => (Number.isInteger(value) ? value : 0);
  const delta = packet.criteria_delta && typeof packet.criteria_delta === "object" ? packet.criteria_delta : {};
  const parts = [
    `Delta packet for attempt ${packet.attempt}`,
    `${count(findings.total)} finding${count(findings.total) === 1 ? "" : "s"} (${count(findings.resolved)} resolved, ${count(findings.rejected)} rejected, ${count(findings.unresolved)} unresolved)`,
    `criteria +${count(delta.added)} -${count(delta.removed)} ~${count(delta.changed)}`,
  ];
  if (packet.stale) parts.push("STALE");
  if (packet.truncated) parts.push("truncated");
  return parts.join(" · ");
}

// #37: one line naming the reviewer tier the NEXT design-review launch
// selects, derived from policy.design_reviewer_selection only (never from
// prose). The reason token is shown with spaces; a post-selection refusal
// (independence or availability) is appended so a missing follow-up
// executable is visible instead of silently substituted. The latest
// recorded review's tier follows as "last review" when there is one.
function designReviewerProfileLabel(selection) {
  const next = selection && typeof selection === "object" ? selection.next : null;
  if (!next || typeof next !== "object" || typeof next.tier !== "string" || !next.tier) {
    return "Review profile: not selected";
  }
  const reason = String(next.reason || "unknown").replaceAll("_", " ");
  const profile = `${next.adapter || "unresolved"}/${next.model || "default"}`;
  let label = `Review profile: ${next.tier} ${profile} (${reason})`;
  if (typeof next.error === "string" && next.error) label += ` · BLOCKED: ${next.error}`;
  const current = selection.current;
  if (current && typeof current === "object" && typeof current.tier === "string" && current.tier) {
    const currentReason = String(current.reason || "unknown").replaceAll("_", " ");
    label += ` · last review: ${current.tier} ${current.adapter || "unresolved"}/${current.model || "default"} (${currentReason})`;
  }
  return label;
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

// #38: one pill per configured [[design_evidence]] artifact. The snapshot
// carries states and hashes only, never command output.
const DESIGN_EVIDENCE_STATES = ["current", "stale", "failed", "missing"];

function designEvidenceState(artifact) {
  const state = String(artifact?.state || "");
  return DESIGN_EVIDENCE_STATES.includes(state) ? state : "missing";
}

function designEvidenceDetail(artifact) {
  const parts = [];
  const reasons = Array.isArray(artifact?.reasons) ? artifact.reasons.filter(Boolean) : [];
  if (reasons.length) parts.push(reasons.join("; "));
  if (Number.isInteger(artifact?.matched_files)) {
    parts.push(`${artifact.matched_files} input file${artifact.matched_files === 1 ? "" : "s"}`);
  }
  if (artifact?.head) {
    parts.push(`commit ${String(artifact.head).slice(0, 12)}${artifact.commit_matches_head ? " (HEAD)" : " (HEAD has moved)"}`);
  }
  if (artifact?.at) parts.push(`measured ${artifact.at}${artifact.by ? ` by ${artifact.by}` : ""}`);
  return parts.join(" · ") || "No measurement recorded.";
}

function eventDetail(event) {
  if (String(event?.kind || "") === "design_evidence_recorded") {
    return [
      event.artifact_id,
      Number.isInteger(event.exit_code) ? `exit ${event.exit_code}` : null,
      event.truncated ? "truncated" : null,
      event.head ? `commit ${String(event.head).slice(0, 12)}` : null,
    ].filter(Boolean).join(" · ");
  }
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

// #39: a role whose adapter or model came from the recommended crew
// (the handsoff.toml key was absent, or held the legacy placeholder) is
// labelled as such, so a default is never displayed as an operator choice.
// `sources` is the settings view's profile_sources[role]: each of adapter
// and model is "explicit", "recommended", or "runner_default" (adapter
// overridden with no model named, so the runner's own default applies).
function profileSourceLabel(sources) {
  if (!sources) return "explicit";
  if (sources.adapter === "recommended" && sources.model === "recommended") {
    return "recommended default";
  }
  const part = (kind, source) => {
    if (source === "recommended") return `recommended default ${kind}`;
    if (source === "runner_default") return `runner default ${kind}`;
    return `explicit ${kind}`;
  };
  return `${part("adapter", sources.adapter)} · ${part("model", sources.model)}`;
}

// #33: the live ship-feature status strip. Every state the server can
// derive (handsoff_lib.LIVE_STATES) maps to a fixed pill label, a tone
// class, and whether the pill pulses; an unknown state reads as such
// rather than being coerced into a healthy-looking one.
const LIVE_STATES = ["idle", "started", "running", "waiting", "stalled", "stopped", "failed", "complete"];

const LIVE_STATE_META = {
  idle: { label: "IDLE", tone: "muted", pulsing: false },
  started: { label: "STARTING", tone: "good", pulsing: true },
  running: { label: "RUNNING", tone: "good", pulsing: true },
  waiting: { label: "WAITING", tone: "warning", pulsing: false },
  stalled: { label: "STALLED", tone: "bad", pulsing: false },
  stopped: { label: "STOPPED", tone: "muted", pulsing: false },
  failed: { label: "FAILED", tone: "bad", pulsing: false },
  complete: { label: "COMPLETE", tone: "good", pulsing: false },
};

function liveStateLabel(state) {
  const meta = LIVE_STATE_META[String(state || "")];
  return meta ? meta.label : "UNKNOWN";
}

function liveStatusView(live) {
  const state = LIVE_STATES.includes(live?.state) ? live.state : "unknown";
  const meta = LIVE_STATE_META[state] || { label: "UNKNOWN", tone: "muted", pulsing: false };
  const role = typeof live?.role === "string" && live.role ? live.role.toUpperCase() : "NO ROLE";
  const detail = typeof live?.detail === "string" && live.detail ? live.detail : "no managed process is running";
  return { state, label: meta.label, tone: meta.tone, pulsing: meta.pulsing, role, detail };
}

// The age line ticks locally between snapshots: the server's
// seconds_since_activity (measured at generated_at) plus the seconds that
// have elapsed on this page since the snapshot arrived.
function liveAgeLabel(live, elapsedSeconds = 0) {
  const base = live?.seconds_since_activity;
  if (!Number.isFinite(base)) return "last activity unknown";
  const elapsed = Number.isFinite(elapsedSeconds) && elapsedSeconds > 0 ? elapsedSeconds : 0;
  const seconds = Math.max(0, Math.floor(base + elapsed));
  if (seconds < 120) return `last activity ${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 120) return `last activity ${minutes} min ago`;
  return `last activity ${Math.floor(minutes / 60)} h ago`;
}

function reviewRoundLabel(policy = {}) {
  const round = Number(policy.review_round) || 0;
  const cap = Number(policy.effective_max_review_rounds ?? policy.max_review_rounds) || 0;
  const overrides = Number(policy.review_cap_overrides) || 0;
  return `${round} / ${cap}${overrides ? ` (${overrides} override${overrides === 1 ? "" : "s"})` : ""}`;
}

function workItemStateLabel(state) {
  return String(state || "unknown").replaceAll("_", " ").toUpperCase();
}

function workItemBlockerText(item = {}) {
  return item.blocker || (item.status === "done" ? "Complete" : "No blocker recorded");
}

function discrepancyLabel(item = {}) {
  return item.discrepancy ? `DISCREPANT: ${item.discrepancy}` : "";
}

function showWorkItemTable(workItems = {}) {
  const items = workItems.items || [];
  return Boolean(items.length > 1 || items.some((item) => item.lane !== "full"));
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
// silently coercing it to a listed option. Since #39 the server folds the
// legacy "configure-me" placeholder into the recommended crew before it
// reaches the UI; the mapping below only matters for an older server.
function resolveAgentSelectValue(storedAdapter) {
  const value = storedAdapter === "configure-me" ? "auto" : storedAdapter;
  return ALLOWED_ADAPTERS.includes(value) ? value : null;
}

// The server's adaptive_routing object is the one canonical source. An
// absent/legacy value is explicitly NOT USED, never healthy-looking zeroes.
const ADAPTIVE_ROUTING_TIERS = ["FAST", "STANDARD", "PREMIUM"];

function adaptiveJourneySelections(selections) {
  const ordered = (Array.isArray(selections) ? selections : [])
    .filter((item) => item && typeof item === "object")
    .map((item) => ({ ...item }))
    .sort((left, right) => {
      const leftStarted = typeof left.started_at === "string" && left.started_at ? left.started_at : null;
      const rightStarted = typeof right.started_at === "string" && right.started_at ? right.started_at : null;
      if (leftStarted === null && rightStarted !== null) return 1;
      if (leftStarted !== null && rightStarted === null) return -1;
      const byStart = leftStarted === rightStarted ? 0 : String(leftStarted).localeCompare(String(rightStarted));
      return byStart || String(left.session_id || "").localeCompare(String(right.session_id || ""));
    });
  const attempts = new Map();
  return ordered.map((item, index) => {
    const attemptKey = `${item.role || "agent"}:${item.phase_number ?? "unknown"}`;
    const attempt = (attempts.get(attemptKey) || 0) + 1;
    attempts.set(attemptKey, attempt);
    return { ...item, journey_index: index + 1, journey_attempt: attempt };
  });
}

function adaptiveRoutingView(routing) {
  const source = routing && typeof routing === "object" ? routing : {};
  const usage = source.token_usage && typeof source.token_usage === "object" ? source.token_usage : {};
  const calls = source.calls_by_tier && typeof source.calls_by_tier === "object" ? source.calls_by_tier : {};
  const pause = source.pause && typeof source.pause === "object" ? source.pause : null;
  return {
    used: source.used === true,
    tier: source.tier || null,
    model: source.model || null,
    token_usage: {
      input: Number(usage.input) || 0,
      output: Number(usage.output) || 0,
      total: Number(usage.total) || ((Number(usage.input) || 0) + (Number(usage.output) || 0)),
    },
    estimated_cost: source.estimated_cost ?? null,
    duration_ms: source.duration_ms ?? null,
    escalation_reason: source.escalation_reason || null,
    repair_rounds: Number(source.repair_rounds) || 0,
    review_rounds: Number(source.review_rounds) || 0,
    active_premium_scope: source.active_premium_scope || null,
    outcome: source.outcome || null,
    // #333: the header used to show a model computed at read time as the model
    // that ran. header_source says which it is, so the label can say so too:
    // a recorded routing decision, a model a session reported, or a projection.
    header_source: source.header_source || null,
    would_route_to: source.would_route_to && typeof source.would_route_to === "object"
      ? { tier: source.would_route_to.tier || null, adapter: source.would_route_to.adapter || null,
          model: source.would_route_to.model || null }
      : null,
    calls_by_tier: Object.fromEntries(ADAPTIVE_ROUTING_TIERS.map((tier) => [tier, Number(calls[tier]) || 0])),
    selections: adaptiveJourneySelections(source.selections),
    pause: pause ? { state: "paused", reason: pause.reason || "unavailable", scope: pause.scope || null } : null,
  };
}

function adaptiveRoutingModelLabel(routing) {
  const view = adaptiveRoutingView(routing);
  if (view.header_source === "routed") return "SELECTED MODEL";
  if (view.header_source === "reported") return "MODEL THAT RAN";
  if (view.would_route_to) return "WOULD ROUTE TO";
  return "SELECTED MODEL";
}

function adaptiveRoutingPauseLabel(routing) {
  const view = adaptiveRoutingView(routing);
  if (!view.used) return "NOT USED";
  return view.pause ? `PAUSED · ${String(view.pause.reason).replaceAll("_", " ")}` : "ACTIVE";
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

// #42: the open post-approval amendment, derived from snapshot.amendment
// only (ids, counts, hashes; the snapshot never carries criterion text).
// A null or legacy snapshot without the field reads as no amendment.
const AMENDMENT_DECISIONS = ["review", "revision", "pilot_approval"];

function amendmentPendingDecision(amendment) {
  if (!amendment || typeof amendment !== "object" || amendment.state !== "open") return null;
  if (AMENDMENT_DECISIONS.includes(amendment.pending_decision)) return amendment.pending_decision;
  const review = Array.isArray(amendment.required_decisions)
    ? amendment.required_decisions.find((item) => item && item.decision === "review")
    : null;
  if (!review || review.status === "pending") return "review";
  return review.status === "approved" ? "pilot_approval" : "revision";
}

function showAmendmentPanel(amendment) {
  return Boolean(amendment && typeof amendment === "object" && amendment.state === "open");
}

function amendmentHeadline(amendment) {
  if (!showAmendmentPanel(amendment)) return "No amendment open";
  const changed = Array.isArray(amendment.changed_ids) ? amendment.changed_ids.length : 0;
  const items = Array.isArray(amendment.affected_work_items) ? amendment.affected_work_items : [];
  const classification = String(amendment.classification || "scoped").replaceAll("_", " ").toUpperCase();
  const phase = Number.isInteger(amendment.frozen_phase) ? ` · frozen at Phase ${amendment.frozen_phase}` : "";
  const scope = items.length ? ` · ${items.join(", ")}` : "";
  return `${amendment.amendment_id || "amendment"} · ${classification} · ${changed} criteri${changed === 1 ? "on" : "a"} changed${scope}${phase}`;
}

function amendmentDecisionLabel(amendment) {
  const pending = amendmentPendingDecision(amendment);
  if (pending === "review") return "PENDING: independent amendment review, then Pilot approval";
  if (pending === "revision") return "PENDING: reviewer requested changes; Architect revises or escalates";
  if (pending === "pilot_approval") return "PENDING: Pilot approval (amendment-approve)";
  return "No decision pending";
}

function amendmentDecisionsView(amendment) {
  const rows = Array.isArray(amendment?.required_decisions) ? amendment.required_decisions : [];
  return rows.map((row) => {
    const decision = String(row?.decision || "decision").replaceAll("_", " ");
    const status = String(row?.status || "pending").replaceAll("_", " ");
    const by = row?.by ? ` by ${row.by}` : "";
    return `${decision}: ${status}${by}`;
  });
}

function amendmentIdsLabel(ids, noun) {
  const list = Array.isArray(ids) ? ids.filter((id) => typeof id === "string" && id) : [];
  if (!list.length) return `${noun}: none`;
  return `${noun}: ${list.join(", ")}`;
}

function amendmentEvidenceLabel(amendment) {
  const retained = Number.isInteger(amendment?.retained_evidence_count) ? amendment.retained_evidence_count : 0;
  const changed = Array.isArray(amendment?.changed_ids) ? amendment.changed_ids.length : 0;
  return `${retained} criteri${retained === 1 ? "on" : "a"} outside the change keep valid evidence · ${changed} reset to not tested`;
}

function amendmentReasonsView(amendment) {
  const reasons = Array.isArray(amendment?.classification_reasons) ? amendment.classification_reasons : [];
  return reasons.filter((reason) => typeof reason === "string" && reason);
}

// #43: a verification record is either executed (its commands were
// launched for it) or reused (its results were copied from an earlier
// executed record of the same binding within the run, named by
// reused_from). A legacy record without the field gets no pill.
function verificationExecutionState(record) {
  if (record?.executed === true) return "executed";
  if (record?.executed === false) return "reused";
  return null;
}

function verificationExecutionLabel(record) {
  const state = verificationExecutionState(record);
  if (state === "executed") return "EXECUTED";
  if (state === "reused") {
    const source = typeof record.reused_from === "string" && record.reused_from
      ? ` · from ${record.reused_from.slice(0, 11)}` : "";
    return `REUSED${source}`;
  }
  return "";
}


// #46: role questions. Pure helpers so the panel logic is testable without a DOM.
function showQuestionsPanel(questions) {
  return Boolean(questions && ((questions.open || []).length || (questions.answered || []).length));
}

function questionHeadline(questions) {
  const open = (questions && questions.open) || [];
  const blocking = (questions && questions.blocking) || [];
  if (!open.length) return "No open questions";
  const roles = [...new Set(open.map((q) => String(q.role || "role")))].join(", ");
  const hold = blocking.length ? `${blocking.length} holding the run` : "none holding the run";
  return `${open.length} open question${open.length === 1 ? "" : "s"} from ${roles} (${hold})`;
}

function questionLabel(question) {
  const role = String((question && question.role) || "role");
  const state = question && question.answer != null ? "answered" : (question && question.blocking ? "blocking" : "open");
  return `${role.charAt(0).toUpperCase()}${role.slice(1)} · ${state}`;
}

// #48: structured question forms. A pure renderer (no DOM) so the node
// tests can assert on the HTML it returns; app.js injects the HTML and wires
// the Other reveal, the click-to-expand and the per-role batch submit.
const QUESTION_OTHER_VALUE = "__other__";

function questionEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function questionRoleTitle(role) {
  const text = String(role || "role");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function questionFormErrorLabel(code) {
  return code ? `form ${String(code).replaceAll("_", " ")}` : "";
}

function renderQuestionChoices(question) {
  const id = questionEscape(question.question_id);
  const options = Array.isArray(question.options) ? question.options : [];
  const recommended = question.recommended != null ? String(question.recommended) : null;
  const plain = options.length === 0;
  const choices = options.map((option) => {
    const value = String(option);
    const isRecommended = recommended === value;
    return `<label class="qf-choice${isRecommended ? " is-recommended" : ""}">`
      + `<input type="radio" name="${id}" value="${questionEscape(value)}"${isRecommended ? " checked" : ""}>`
      + `<span>${questionEscape(value)}</span>`
      + (isRecommended ? '<em class="qf-recommended">recommended</em>' : "")
      + "</label>";
  });
  choices.push(`<label class="qf-choice qf-choice-other">`
    + `<input type="radio" name="${id}" value="${QUESTION_OTHER_VALUE}"${plain ? " checked" : ""}>`
    + "<span>Other</span></label>");
  const field = `<input type="text" class="qf-other" name="${id}-other" maxlength="1024" `
    + `placeholder="${plain ? "Your answer" : "Your own answer"}"${plain ? "" : " hidden"}>`;
  return `<div class="qf-choices" role="radiogroup">${choices.join("")}${field}</div>`;
}

function renderQuestionRow(question, number) {
  const id = questionEscape(question.question_id);
  const answered = question.answer != null;
  const text = `${question.text || ""}${question.truncated ? " (truncated)" : ""}`;
  if (answered) {
    const by = question.answered_by || "Pilot";
    return `<div class="qf-row is-answered" data-question-id="${id}">`
      + `<span class="qf-num">${number}.</span>`
      + `<span class="qf-muted">${questionEscape(text)} · ${questionEscape(by)}: ${questionEscape(question.answer)}</span>`
      + "</div>";
  }
  const flags = [question.blocking ? "holding the run" : "", questionFormErrorLabel(question.form_error)]
    .filter(Boolean).join(" · ");
  return `<div class="qf-row is-open" data-question-id="${id}">`
    + `<div class="qf-line"><span class="qf-num">${number}.</span>`
    + `<p class="qf-text is-clamped" data-expand title="Click to expand">${questionEscape(text)}</p>`
    + (flags ? `<span class="qf-flags">${questionEscape(flags)}</span>` : "")
    + "</div>"
    + renderQuestionChoices(question)
    + "</div>";
}

function renderQuestionForms(questions) {
  const open = (questions && questions.open) || [];
  const answered = (questions && questions.answered) || [];
  const byRole = new Map();
  const asked = (q) => String((q && q.asked_at) || "");
  [...open, ...answered]
    .filter((q) => q && q.question_id)
    .sort((a, b) => (asked(a) < asked(b) ? -1 : asked(a) > asked(b) ? 1 : 0))
    .forEach((q) => {
      const role = String(q.role || "role");
      if (!byRole.has(role)) byRole.set(role, []);
      byRole.get(role).push(q);
    });
  const forms = [];
  const banner = [];
  const html = [];
  byRole.forEach((rows, role) => {
    const openRows = rows.filter((q) => q.answer == null);
    const blocking = openRows.filter((q) => q.blocking).length;
    forms.push({ role, question_ids: rows.map((q) => q.question_id), open_count: openRows.length });
    if (blocking) banner.push({ role, count: blocking });
    const body = rows.map((q, index) => renderQuestionRow(q, index + 1)).join("");
    const send = openRows.length
      ? `<div class="qf-actions"><button type="submit" class="ghost-button qf-send">Send answers</button></div>`
      : "";
    html.push(`<form class="question-role-form" data-role="${questionEscape(role)}">`
      + `<div class="qf-head"><span class="question-pill">${questionEscape(questionRoleTitle(role))}</span>`
      + `<span class="qf-count">${openRows.length} open</span></div>`
      + body + send + "</form>");
  });
  return { html: html.join(""), forms, banner };
}

function questionBannerCards(cards) {
  return (cards || []).map((card) => {
    const count = Number(card.count || 0);
    return `<span class="qf-card">${questionEscape(questionRoleTitle(card.role))}: ${count} question${count === 1 ? "" : "s"} waiting</span>`;
  }).join("");
}

function collectQuestionFormAnswers(entries) {
  // entries: [{question_id, choice, other}] straight from the form controls;
  // a row with no selection is skipped, an Other row with no text is skipped.
  const answers = [];
  (entries || []).forEach((entry) => {
    if (!entry || !entry.question_id) return;
    if (entry.choice === QUESTION_OTHER_VALUE) {
      const other = String(entry.other || "").trim();
      if (other) answers.push({ question_id: entry.question_id, other });
      return;
    }
    if (entry.choice != null && entry.choice !== "") {
      answers.push({ question_id: entry.question_id, choice: entry.choice });
    }
  });
  return answers;
}

const PILOT_NOTE_MAX_LENGTH = 512;

function workItemLaneLabel(item) {
  const lane = String(item?.lane || "full").replaceAll("-", " ").toUpperCase();
  return item?.lane === "small-fix" && !item?.lane_confirmed ? `${lane} · UNCONFIRMED` : lane;
}

function workItemProgressLabel(item) {
  const value = Number(item?.progress);
  return `${Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : 0}%`;
}

function smallFixCanConfirm(item) {
  return item?.lane === "small-fix" && !item?.lane_confirmed && !item?.lane_escalation;
}

function workItemLaneDetail(item) {
  const facts = item?.lane_facts;
  if (!facts) return item?.lane_escalation || "Measurements pending";
  const caps = facts.caps || {};
  const detail = `${facts.criteria ?? "?"}/${caps.criteria ?? "?"} criteria · ${facts.changed_lines ?? "?"}/${caps.changed_lines ?? "?"} lines · ${facts.changed_files ?? "?"}/${caps.changed_files ?? "?"} files`;
  return item?.lane_escalation ? `${detail} · ${item.lane_escalation}` : detail;
}

function trancheIssueDetail(issue) {
  const score = Object.entries(issue?.score_inputs || {}).map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`).join(" · ");
  const blockers = (issue?.blockers || []).join(", ") || "none";
  const cost = issue?.cost_shape ? JSON.stringify(issue.cost_shape.median_launched_sessions || {}) : "unknown";
  return `${score || "score inputs unavailable"} · blockers ${blockers} · cost ${cost}`;
}

function trancheDecisionPayload(proposalHash, rows) {
  const order = rows.filter((row) => !row.dropped).map((row) => row.id);
  const drops = rows.filter((row) => row.dropped).map((row) => row.id);
  return { proposal_hash: String(proposalHash || ""), order, drops };
}

function pilotNoteText(value) {
  // #49: whitespace-collapsed note text, or "" when it is empty or over
  // the 512-character bound the supervisor enforces.
  const text = String(value == null ? "" : value).split(/\s+/).filter(Boolean).join(" ");
  if (!text || text.length > PILOT_NOTE_MAX_LENGTH) return "";
  return text;
}

// #165: the RED beside the PASS on a criterion row. A recorded baseline
// reads "red <date>", a declared exception reads "no red: <reason>", and a
// criterion the snapshot knows nothing about (older engine) reads nothing.
function baselineLabel(criterion) {
  const view = criterion && criterion.baseline_view;
  if (!view || typeof view !== "object") return "";
  if (view.kind === "recorded") {
    const at = typeof view.at === "string" && view.at ? view.at.slice(0, 10) : "";
    return at ? `red ${at}` : "red recorded";
  }
  if (view.kind === "not_applicable") return `no red: ${view.reason || "no reason"}`;
  return "";
}

// #169: the repeat count beside the pass: "5/5", "failed at attempt 3 (seed x)",
// or "repeat 5" while nothing has run yet.
function repeatLabel(criterion) {
  const view = criterion && criterion.repeat_view;
  if (!view || typeof view !== "object") return "";
  if (view.kind === "passed") return `${view.attempts}/${view.repeat}`;
  if (view.kind === "failed") return `failed at attempt ${view.attempt} of ${view.repeat}${view.seed ? ` (seed ${view.seed})` : ""}`;
  if (view.kind === "pending") return `repeat ${view.repeat}`;
  return "";
}

// #165 #167 #166: the [features] switches. One row per known feature from
// the snapshot's settings.features, in the engine's own order; a snapshot
// without the table (an older engine) renders no rows and no save.
function featureSwitchRows(settings) {
  const features = settings && settings.features && typeof settings.features === "object" ? settings.features : {};
  return Object.keys(features).map((name) => {
    const item = features[name] || {};
    return {
      name,
      label: name.replace(/_/g, " ").toUpperCase(),
      enabled: item.enabled === true,
      isDefault: item.enabled === item.default,
      description: typeof item.description === "string" ? item.description : "",
    };
  });
}

// The exact body POST /api/settings/features accepts: every known feature,
// a literal boolean each, nothing else.
function featuresPayload(rows, checked) {
  const payload = {};
  for (const row of rows) payload[row.name] = checked(row.name) === true;
  return payload;
}


// #193: "asleep 7 h 03 m" beside a clock, or "" when the machine did not sleep.
function asleepLabel(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 60) return "";
  const total = Math.round(seconds);
  if (total < 3600) return `asleep ${Math.floor(total / 60)} min`;
  return `asleep ${Math.floor(total / 3600)} h ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")} m`;
}

// #181: text for the CI row. Pure, so tests/dashboard/ci_row.test.js
// exercises them without a DOM.
function ciSeconds(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  const total = Math.round(value);
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

function ciStateLabel(ci) {
  const state = ci && typeof ci.state === "string" ? ci.state : "running";
  if (state === "passed") return "CI PASSED";
  if (state === "failed") return "CI FAILED";
  return "CI RUNNING";
}

// the same finished set the engine uses (CI_CHECK_DONE in handsoff_lib.py)
const CI_DONE_STATES = ["SUCCESS", "FAILURE", "CANCELLED", "SKIPPED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "NEUTRAL"];

function ciChecksDone(ci) {
  const checks = ci && Array.isArray(ci.checks) ? ci.checks : [];
  return { done: checks.filter((c) => CI_DONE_STATES.includes(c.state)).length, total: checks.length };
}

// #198: the percent leads the label. Time-based like the bar when there is
// an estimate, capped at 99 until every check is done; the checks-done
// fraction without one; 100 on passed.
function ciPercent(ci) {
  if (!ci) return null;
  if (ci.state === "passed") return 100;
  const { done, total } = ciChecksDone(ci);
  if (ci.state === "failed") return total ? Math.round((done / total) * 100) : null;
  if (typeof ci.progress === "number" && Number.isFinite(ci.progress)) {
    const timed = Math.max(0, Math.min(100, Math.round(ci.progress * 100)));
    return total && done === total ? timed : Math.min(99, timed);
  }
  return total ? Math.min(99, Math.round((done / total) * 100)) : null;
}

function ciProgressLabel(ci) {
  if (!ci) return "";
  const elapsed = ciSeconds(ci.elapsed_seconds);
  const expected = ciSeconds(ci.expected_seconds);
  const { done, total } = ciChecksDone(ci);
  const percent = ciPercent(ci);
  const lead = percent === null ? "" : `${percent}% · `;
  const checks = total ? `${done} of ${total} checks done · ` : "";
  if (ci.state === "passed") return `${lead}passed in ${elapsed || "?"}${expected ? ` (last run ${expected})` : ""}`;
  if (ci.state === "failed") return `${ci.failed_check || "a check"} failed after ${elapsed || "?"}`;
  if (!expected) return `${lead}${checks}${elapsed || "0s"} elapsed`;
  return `${lead}${checks}${elapsed || "0s"} of about ${expected}`;
}

function ciBarPercent(ci) {
  if (!ci) return null;
  if (ci.state === "passed" || ci.state === "failed") return 100;
  if (typeof ci.progress !== "number" || !Number.isFinite(ci.progress)) return null;
  return Math.max(0, Math.min(100, Math.round(ci.progress * 100)));
}

// #181: the snapshot's ci block advanced by `seconds` of wall time: elapsed
// and progress move while the watch runs; a terminal watch is frozen.
function ciTicked(ci, seconds) {
  if (!ci || ci.state !== "running" || typeof ci.elapsed_seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return ci;
  const elapsed = ci.elapsed_seconds + seconds;
  const expected = ci.expected_seconds;
  const progress = typeof expected === "number" && expected > 0 ? Math.min(elapsed / expected, 1) : ci.progress;
  return { ...ci, elapsed_seconds: elapsed, progress };
}

function ciCellLabel(check) {
  if (check && check.queued && typeof check.name === "string") return `${check.name} queued`;
  if (!check || typeof check.name !== "string") return "";
  const elapsed = ciSeconds(check.elapsed_seconds);
  return elapsed ? `${check.name} ${elapsed}` : check.name;
}

function ciNote(ci) {
  // #198: the checks-done count moved into the label; the note keeps the server's words
  if (!ci) return "";
  return typeof ci.note === "string" && ci.note ? ci.note : "";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    baselineLabel,
    repeatLabel,
    featureSwitchRows,
    featuresPayload,
    roleStation,
    roleWord,
    roleTitle,
    PILOT_NOTE_MAX_LENGTH,
    pilotNoteText,
    workItemLaneLabel,
    workItemProgressLabel,
    smallFixCanConfirm,
    workItemLaneDetail,
    trancheIssueDetail,
    trancheDecisionPayload,
    showQuestionsPanel,
    questionHeadline,
    questionLabel,
    QUESTION_OTHER_VALUE,
    renderQuestionForms,
    questionBannerCards,
    collectQuestionFormAnswers,
    adapterLabel,
    friendlyActorLabel,
    effectiveProfileLabel,
    actorForRole,
    runProfileLabel,
    cleanKind,
    designReviewBudgetLabel,
    designReviewPacketLabel,
    designReviewerProfileLabel,
    eventMessage,
    runtimeProfileLabel,
    crewProfileLabel,
    replacementHeadline,
    replacementDetail,
    eventDetail,
    DESIGN_EVIDENCE_STATES,
    designEvidenceState,
    designEvidenceDetail,
    profileSourceLabel,
    LIVE_STATES,
    liveStateLabel,
    liveStatusView,
    liveAgeLabel,
    reviewRoundLabel,
    workItemStateLabel,
    workItemBlockerText,
    discrepancyLabel,
    showWorkItemTable,
    autoDetectOptionLabel,
    resolveAgentSelectValue,
    ALLOWED_ADAPTERS,
    MAX_FALLBACK_ENTRIES,
    addFallbackEntry,
    moveFallbackEntry,
    removeFallbackEntry,
    buildFallbackDraft,
    serializeFallbackDraft,
    AMENDMENT_DECISIONS,
    amendmentPendingDecision,
    showAmendmentPanel,
    amendmentHeadline,
    amendmentDecisionLabel,
    amendmentDecisionsView,
    amendmentIdsLabel,
    amendmentEvidenceLabel,
    amendmentReasonsView,
    verificationExecutionState,
    verificationExecutionLabel,
    hostBadgeLabel,
    hostBadgeTitle,
    roleFamily,
    roleStationKind,
    ciChecksDone,
    ciPercent,
    asleepLabel,
    ciSeconds,
    ciStateLabel,
    ciProgressLabel,
    ciBarPercent,
    ciCellLabel,
    ciTicked,
    ciNote,
    progressSummaryLabel,
    adaptiveRoutingView,
    adaptiveRoutingModelLabel,
    adaptiveJourneySelections,
  };
}
