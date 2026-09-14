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
  assert.match(html, /id="input-alert-cards"/);
  assert.match(app, /function renderQuestions\(questions\)/);
  assert.match(app, /renderQuestions\(snapshot\.questions \|\| null\)/);
  assert.match(app, /fetch\("\/api\/question-answer"/);
  assert.match(app, /fetch\("\/api\/question-answers"/);
  assert.match(app, /renderQuestionForms\(questions\)/);
  assert.match(app, /questionBannerCards\(rendered\.banner\)/);
  assert.match(app, /collectQuestionFormAnswers\(entries\)/);
  assert.match(app, /classList\.toggle\("is-clamped"\)/);
  assert.match(app, /other\.hidden = !isOther/);
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

// #48 fixtures: two roles, a recommended form, a form without a
// recommendation (Other stays hidden), a plain-text question, a malformed
// form kept as text, and an answered question.
const recommended = {
  question_id: "qn-a1", role: "architect", text: "Which path?", asked_at: "2026-09-14T10:00:00+00:00",
  blocking: true, answer: null, options: ["Concise", "Full"], recommended: "Concise", form_error: null,
};
const noRecommendation = {
  question_id: "qn-a2", role: "architect", text: "Region?", asked_at: "2026-09-14T10:01:00+00:00",
  blocking: true, answer: null, options: ["us-east-1", "eu-west-1"], recommended: null, form_error: null,
};
const plainText = {
  question_id: "qn-r1", role: "reviewer", text: "Anything <else>?", asked_at: "2026-09-14T10:02:00+00:00",
  blocking: false, answer: null, options: [], recommended: null, form_error: null,
};
const malformed = {
  question_id: "qn-r2", role: "reviewer", text: '{"text": "oops"', asked_at: "2026-09-14T10:03:00+00:00",
  blocking: true, answer: null, options: [], recommended: null, form_error: "malformed_json",
};
const answeredForm = {
  question_id: "qn-a0", role: "architect", text: "Depth?", asked_at: "2026-09-14T09:00:00+00:00",
  blocking: false, answer: "Deep", answered_by: "moncy", options: ["Shallow", "Deep"], recommended: "Deep",
  form_error: null, chosen_option: "Deep", other_text: null,
};
const view = {
  open: [recommended, noRecommendation, plainText, malformed],
  blocking: [recommended, noRecommendation, malformed],
  answered: [answeredForm],
  by_role: [],
  total: 5,
};

function extract(html, pattern) {
  const found = html.match(pattern);
  assert.ok(found, `expected ${pattern} in ${html}`);
  return found;
}

test("renderQuestionForms groups open questions into one numbered form per role", () => {
  const rendered = logic.renderQuestionForms(view);
  assert.deepEqual(rendered.forms, [
    { role: "architect", question_ids: ["qn-a0", "qn-a1", "qn-a2"], open_count: 2 },
    { role: "reviewer", question_ids: ["qn-r1", "qn-r2"], open_count: 2 },
  ]);
  assert.deepEqual(rendered.banner, [{ role: "architect", count: 2 }, { role: "reviewer", count: 1 }]);
  const forms = rendered.html.match(/<form class="question-role-form" data-role="[a-z]+">/g);
  assert.deepEqual(forms, [
    '<form class="question-role-form" data-role="architect">',
    '<form class="question-role-form" data-role="reviewer">',
  ]);
  // One Send answers button per role form, never one per question.
  assert.equal((rendered.html.match(/class="ghost-button qf-send">Send answers<\/button>/g) || []).length, 2);
  // Rows are numbered in asked_at order within the role.
  const architect = rendered.html.slice(rendered.html.indexOf('data-role="architect"'),
    rendered.html.indexOf('data-role="reviewer"'));
  assert.deepEqual(architect.match(/<span class="qf-num">\d\.<\/span>/g),
    ['<span class="qf-num">1.</span>', '<span class="qf-num">2.</span>', '<span class="qf-num">3.</span>']);
  assert.ok(architect.indexOf('data-question-id="qn-a0"') < architect.indexOf('data-question-id="qn-a1"'));
  assert.ok(architect.indexOf('data-question-id="qn-a1"') < architect.indexOf('data-question-id="qn-a2"'));
  // No per-question boxes: rows are divs with a divider class, not panels.
  assert.doesNotMatch(rendered.html, /class="question-row/);
});

test("recommended option is preselected and labelled, Other field hidden until chosen", () => {
  const { html } = logic.renderQuestionForms(view);
  const row = html.slice(html.indexOf('data-question-id="qn-a1"'), html.indexOf('data-question-id="qn-a2"'));
  extract(row, /<input type="radio" name="qn-a1" value="Concise" checked>/);
  extract(row, /<input type="radio" name="qn-a1" value="Full">/);
  assert.equal((row.match(/checked/g) || []).length, 1);
  extract(row, /<label class="qf-choice is-recommended">.*?<em class="qf-recommended">recommended<\/em>/);
  extract(row, new RegExp(`<input type="radio" name="qn-a1" value="${logic.QUESTION_OTHER_VALUE}">`));
  extract(row, /<input type="text" class="qf-other" name="qn-a1-other"[^>]* hidden>/);
  extract(row, /<p class="qf-text is-clamped" data-expand title="Click to expand">Which path\?<\/p>/);
  extract(row, /<div class="qf-choices" role="radiogroup">/);
  assert.equal((row.match(/<div class="qf-choices"/g) || []).length, 1);
});

test("a form without a recommendation preselects nothing and keeps Other hidden", () => {
  const { html } = logic.renderQuestionForms(view);
  const row = html.slice(html.indexOf('data-question-id="qn-a2"'), html.indexOf('data-role="reviewer"'));
  assert.doesNotMatch(row, /checked/);
  assert.doesNotMatch(row, /qf-recommended/);
  extract(row, /<input type="text" class="qf-other" name="qn-a2-other"[^>]* hidden>/);
});

test("plain-text and malformed questions offer only Other with the field revealed", () => {
  const { html } = logic.renderQuestionForms(view);
  const plain = html.slice(html.indexOf('data-question-id="qn-r1"'), html.indexOf('data-question-id="qn-r2"'));
  assert.equal((plain.match(/type="radio"/g) || []).length, 1);
  extract(plain, new RegExp(`<input type="radio" name="qn-r1" value="${logic.QUESTION_OTHER_VALUE}" checked>`));
  extract(plain, /<input type="text" class="qf-other" name="qn-r1-other"[^>]*placeholder="Your answer">/);
  assert.doesNotMatch(plain, /hidden/);
  // Text is escaped, never injected.
  assert.match(plain, /Anything &lt;else&gt;\?/);
  assert.doesNotMatch(plain, /<else>/);
  const broken = html.slice(html.indexOf('data-question-id="qn-r2"'));
  assert.match(broken, /holding the run · form malformed json/);
  assert.match(broken, /&quot;text&quot;: &quot;oops&quot;/);
});

test("answered questions collapse to one muted line without controls", () => {
  const { html } = logic.renderQuestionForms(view);
  const row = html.slice(html.indexOf('data-question-id="qn-a0"'), html.indexOf('data-question-id="qn-a1"'));
  extract(html, /<div class="qf-row is-answered" data-question-id="qn-a0"><span class="qf-num">1\.<\/span><span class="qf-muted">Depth\? · moncy: Deep<\/span><\/div>/);
  assert.doesNotMatch(row, /type="radio"|qf-other|qf-choices|data-expand/);
  // A role with only answered questions renders no Send answers button.
  const settled = logic.renderQuestionForms({ open: [], blocking: [], answered: [answeredForm] });
  assert.deepEqual(settled.forms, [{ role: "architect", question_ids: ["qn-a0"], open_count: 0 }]);
  assert.deepEqual(settled.banner, []);
  assert.doesNotMatch(settled.html, /Send answers/);
});

test("banner cards name one card per role with the count", () => {
  const { banner } = logic.renderQuestionForms(view);
  const cards = logic.questionBannerCards(banner);
  assert.equal(cards,
    '<span class="qf-card">Architect: 2 questions waiting</span><span class="qf-card">Reviewer: 1 question waiting</span>');
  assert.equal(logic.questionBannerCards([]), "");
  assert.deepEqual(logic.renderQuestionForms(null), { html: "", forms: [], banner: [] });
});

test("collectQuestionFormAnswers builds the batch payload from the form controls", () => {
  const answers = logic.collectQuestionFormAnswers([
    { question_id: "qn-a1", choice: "Full", other: "" },
    { question_id: "qn-a2", choice: logic.QUESTION_OTHER_VALUE, other: "  ap-south-1 " },
    { question_id: "qn-r1", choice: logic.QUESTION_OTHER_VALUE, other: "   " },
    { question_id: "qn-r2", choice: null, other: "" },
  ]);
  assert.deepEqual(answers, [
    { question_id: "qn-a1", choice: "Full" },
    { question_id: "qn-a2", other: "ap-south-1" },
  ]);
});

test("styles keep the form to thin dividers with the existing palette", () => {
  const css = fs.readFileSync(path.join(root, "dashboard", "styles.css"), "utf8");
  assert.match(css, /\.qf-row \{[^}]*border-bottom: 1px solid/);
  assert.match(css, /\.qf-text\.is-clamped \{[^}]*-webkit-line-clamp: 2/);
  assert.match(css, /\.qf-row\.is-answered \{[^}]*color: var\(--muted/);
  const block = css.slice(css.indexOf("/* #48"));
  assert.doesNotMatch(block, /\.qf-row \{[^}]*border-radius/);
  const colors = new Set((block.match(/#[0-9a-f]{6}\b/gi) || []).map((c) => c.toLowerCase()));
  const before = new Set((css.slice(0, css.indexOf("/* #48")).match(/#[0-9a-f]{6}\b/gi) || []).map((c) => c.toLowerCase()));
  for (const color of colors) assert.ok(before.has(color), `new color ${color}`);
});
