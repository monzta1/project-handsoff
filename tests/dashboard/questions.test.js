const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const logic = require(path.join(root, "dashboard", "lib", "dashboard-logic.js"));

test("index.html carries the questions panel and app.js renders snapshot.questions", () => {
  const html = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
  const app = fs.readFileSync(path.join(root, "dashboard", "app.js"), "utf8");
  assert.match(html, /id="questions-panel"/);
  assert.match(html, /id="questions-list"/);
  assert.match(app, /function renderQuestions\(questions\)/);
  assert.match(app, /renderQuestions\(snapshot\.questions \|\| null\)/);
  assert.match(app, /fetch\("\/api\/question-answer"/);
  assert.match(app, /escapeHtml\(q\.text\)/);
});

test("question helpers describe open, blocking and answered questions", () => {
  assert.equal(logic.showQuestionsPanel(null), false);
  assert.equal(logic.showQuestionsPanel({ open: [], answered: [] }), false);
  const open = { question_id: "qn-1", role: "architect", text: "Path?", blocking: true, answer: null };
  const answered = { ...open, question_id: "qn-2", blocking: false, answer: "Concise" };
  assert.equal(logic.showQuestionsPanel({ open: [open], blocking: [open], answered: [] }), true);
  assert.equal(logic.questionHeadline({ open: [open], blocking: [open] }),
    "1 open question from architect (1 holding the run)");
  assert.equal(logic.questionHeadline({ open: [], blocking: [] }), "No open questions");
  assert.equal(logic.questionLabel(open), "Architect · blocking");
  assert.equal(logic.questionLabel(answered), "Architect · answered");
});
