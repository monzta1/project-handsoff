const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "dashboard/styles.css"), "utf8");

// #148: the headline and the phase rail show a running or failed live
// verification, and the Phase 7 card lists the failing command with its
// output tail until a later live run passes.
test("headline reads the live verification state from verification.live at Phase 7", () => {
  assert.match(app, /function liveVerificationHeadline\(verification\)/);
  assert.match(app, /return `LIVE VERIFICATION RUNNING · \$\{flight\.done\}\/\$\{flight\.total\}\$\{flight\.current \? ` · \$\{flight\.current\}` : ""\}`/);
  assert.match(app, /return `LIVE VERIFICATION FAILED · \$\{live\.last_failure\.command\} exit \$\{live\.last_failure\.exit_code\}`/);
  assert.match(app, /status\.phase_number === 7 \? liveVerificationHeadline\(snapshot\.verification\) : null/);
});

test("the phase rail takes the server's phase name and the Phase 7 card shows the failing command", () => {
  assert.match(app, /renderPhases\(snapshot\.phases, snapshot\.verification\)/);
  assert.match(app, /const failure = verification\?\.live\?\.last_failure/);
  assert.match(app, /card\.classList\.toggle\("hidden", !failure \|\| running\)/);
  assert.match(app, /head\.textContent = `LIVE VERIFICATION FAILED · \$\{failure\.command\} · exit \$\{failure\.exit_code\}`/);
  assert.match(app, /tail\.textContent = failure\.output_tail \|\| "\(no output captured\)"/);
  assert.match(html, /<div id="phase-7-card" class="phase-7-detail hidden" role="status" aria-live="polite"><\/div>/);
  assert.match(css, /\.phase-7-detail pre \{[^}]*white-space: pre-wrap/);
});
