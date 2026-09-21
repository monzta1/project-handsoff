// #218 step one: the words and clocks every surface that shows a run
// shares, in one place. The run page, Fleet (both pages) and the Regression
// Console load this file before their own script; the node suites require
// it. No DOM access here, so it loads unmodified as globals in a <script>
// tag and via require() (the dashboard-logic.js pattern).

// Fleet card states, in the order the summary strip lists them.
const STATE_ORDER = ["waiting", "failed", "offline", "stalled", "running", "quiet", "complete", "closed", "idle", "orphaned"];
// #150/#154: only a run that is moving belongs in the grid; finished runs and
// projects with no run at all sit in the collapsed section.
const FINISHED_STATES = new Set(["complete", "closed", "idle", "orphaned"]);
const STATE_LABELS = {
  waiting: "WAITING ON PILOT", failed: "FAILED", stalled: "STALLED", running: "RUNNING",
  quiet: "QUIET", offline: "DASHBOARD OFFLINE", complete: "COMPLETE", closed: "CLOSED", idle: "NO RUN", orphaned: "ORPHANED",
};

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
  module.exports = { STATE_ORDER, FINISHED_STATES, STATE_LABELS, lcdText, lcdMarkup };
}
