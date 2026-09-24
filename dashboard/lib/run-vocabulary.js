// #218 step one: the words and clocks every surface that shows a run
// shares, in one place. The run page, Fleet (both pages) and the Regression
// Console load this file before their own script; the node suites require
// it. No DOM access here, so it loads unmodified as globals in a <script>
// tag and via require() (the dashboard-logic.js pattern).

// Fleet card states, in the order the summary strip lists them.
const STATE_ORDER = ["waiting", "failed", "offline", "stalled", "running", "quiet", "released", "installed", "live_verified", "complete", "closed", "aborted", "idle", "orphaned"];
// #150/#154: only a run that is moving belongs in the grid; finished runs and
// projects with no run at all sit in the collapsed section.
// #296: "released" and "installed" are NOT finished. A published release
// whose artifact was never installed and verified is still in flight, and
// showing it as finished is what let v0.3.80 read as delivered at Phase 7.
const FINISHED_STATES = new Set(["complete", "closed", "aborted", "idle", "orphaned"]);
const STATE_LABELS = {
  waiting: "WAITING ON PILOT", failed: "FAILED", stalled: "STALLED", running: "RUNNING",
  quiet: "QUIET", offline: "DASHBOARD OFFLINE", complete: "COMPLETE", closed: "CLOSED", idle: "NO RUN", orphaned: "ORPHANED",
  // #296: the four things that are not the same as "done".
  released: "RELEASED, NOT INSTALLED", installed: "INSTALLED, NOT VERIFIED",
  live_verified: "LIVE VERIFIED", aborted: "ABORTED",
};

// #296: the closeout ladder, in order. A run may stop at any rung, but only
// the last one is a successful delivery. Each rung has evidence behind it:
// a published release, an installed artifact, a live verification id, and
// the Phase-8 advance that depends on all three.
const CLOSEOUT_LADDER = ["released", "installed", "live_verified", "complete"];
// The outcomes a run that never reached verified Phase 8 may record.
const UNVERIFIED_OUTCOMES = new Set(["aborted", "released_unverified"]);

// Which rung a run has actually reached. Never infers upward: an absent
// live verification id means not verified, whatever the phase claims.
function closeoutState(run) {
  const r = run || {};
  if (r.outcome === "aborted") return "aborted";
  if (r.outcome === "released_unverified") return "released";
  const verified = Boolean(r.live_verification_id);
  if (verified && Number(r.phase_number) >= 8 && Number(r.progress) >= 100) return "complete";
  if (verified) return "live_verified";
  if (r.installed) return "installed";
  if (r.released) return "released";
  return null;
}

// #296: a published release is not a delivered one.
function isSuccessfullyDelivered(run) {
  return closeoutState(run) === "complete";
}

// The LCD clock's text: hours, minutes and seconds, zero padded, never negative.
function lcdText(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600); const m = Math.floor((total % 3600) / 60); const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// The LCD markup every page draws: a ghost of eights under the live digits.
function lcdMarkup(extraClass, attributes) {
  return `<span class="lcd ${extraClass || ""}" ${attributes || ""}><span class="lcd-ghost" aria-hidden="true">88:88:88</span><span class="lcd-live">00:00:00</span></span>`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { STATE_ORDER, FINISHED_STATES, STATE_LABELS, lcdText, lcdMarkup,
    CLOSEOUT_LADDER, UNVERIFIED_OUTCOMES, closeoutState, isSuccessfullyDelivered };
}
