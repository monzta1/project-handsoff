// #218 step one: the run vocabulary exists once and every page draws from it.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vocab = require("../../dashboard/lib/run-vocabulary.js");

const read = (p) => fs.readFileSync(path.join(__dirname, "..", "..", p), "utf8");

test("the module exports the states, the labels and the clock", () => {
  assert.deepEqual(vocab.STATE_ORDER, ["waiting", "failed", "offline", "stalled", "running", "quiet", "complete", "closed", "idle", "orphaned"]);
  assert.equal(vocab.STATE_LABELS.waiting, "WAITING ON PILOT");
  assert.equal(vocab.STATE_LABELS.orphaned, "ORPHANED");
  assert.ok(vocab.FINISHED_STATES.has("closed") && !vocab.FINISHED_STATES.has("running"));
  assert.equal(vocab.lcdText(0), "00:00:00");
  assert.equal(vocab.lcdText(3661.9), "01:01:01");
  assert.equal(vocab.lcdText(-5), "00:00:00");
  assert.equal(vocab.lcdText(90000), "25:00:00");
  assert.match(vocab.lcdMarkup("lcd-small", 'data-x="1"'), /^<span class="lcd lcd-small" data-x="1"><span class="lcd-ghost"/);
});

test("no page carries a second copy of the vocabulary", () => {
  for (const name of ["fleet/app.js", "dashboard/app.js", "dashboard/regression.js", "fleet/metrics.js"]) {
    const source = read(name);
    assert.ok(!/function lcdText\(/.test(source), `${name} carries lcdText`);
    assert.ok(!/const STATE_LABELS = \{/.test(source), `${name} carries STATE_LABELS`);
  }
  assert.match(read("fleet/app.js"), /from\n\/\/ \/lib\/run-vocabulary\.js/);
});

test("every page loads the module before its own script", () => {
  for (const [page, own] of [["dashboard/index.html", "/app.js"], ["fleet/index.html", "/app.js"],
                             ["fleet/metrics.html", "/metrics.js"], ["dashboard/regression.html", "/regression.js"]]) {
    const html = read(page);
    const vocabAt = html.indexOf('<script src="/lib/run-vocabulary.js" defer></script>');
    const ownAt = html.indexOf(`<script src="${own}" defer></script>`);
    assert.ok(vocabAt >= 0, `${page} loads the vocabulary`);
    assert.ok(vocabAt < ownAt, `${page} loads the vocabulary first`);
  }
});
