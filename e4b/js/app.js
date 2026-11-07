/* E4b static annotation client (no Firebase).
   - Annotator identity = the token in the URL (?a=<token>); assignment is
     pre-computed in data/a/<token>.json, so nothing is assigned live.
   - Every answer is kept in localStorage first, then POSTed to the Apps Script
     web app (config.SUBMIT_URL). Unsent answers are retried on the next load and
     after every submit. "Download my answers" exports everything as a fallback.
   - Same questions, wording, layout and shortcuts as the main study. */
import { SUBMIT_URL, CLIENT_VERSION } from "./config.js";
import { buildQuestions } from "./questions.js";

const $ = (id) => document.getElementById(id);
const dom = {
  status: $("statusBanner"), progress: $("progressText"), name: $("annotatorName"), pending: $("pendingText"),
  prompt: $("promptText"), image: $("generatedImage"), objectsSection: $("objectsSection"),
  objectQuestions: $("objectQuestions"), conditionQuestions: $("conditionQuestions"),
  submit: $("submitBtn"), download: $("downloadBtn"),
};

const state = {
  token: new URLSearchParams(location.search).get("a") || "",
  order: [], items: {}, answers: {}, pending: [],
  item: null, questions: [], responses: {}, active: 0, startedAt: null, busy: false,
};

function showStatus(msg, isError = false) {
  dom.status.textContent = msg;
  dom.status.style.color = isError ? "#b42318" : "#03543f";
}

/* ---------- local persistence (may be unavailable: private windows etc.) ---------- */
const kA = () => `e4b_answers_${state.token}`;
const kP = () => `e4b_pending_${state.token}`;
function loadLocal() {
  try {
    state.answers = JSON.parse(localStorage.getItem(kA()) || "{}");
    state.pending = JSON.parse(localStorage.getItem(kP()) || "[]");
  } catch { state.answers = {}; state.pending = []; }
}
function saveLocal() {
  try {
    localStorage.setItem(kA(), JSON.stringify(state.answers));
    localStorage.setItem(kP(), JSON.stringify(state.pending));
  } catch { /* the in-memory copy and the server still have it */ }
}

/* ---------- server ---------- */
async function fetchServerDone() {
  if (!SUBMIT_URL) return [];
  try {
    const r = await fetch(`${SUBMIT_URL}?token=${encodeURIComponent(state.token)}`);
    const j = await r.json();
    return Array.isArray(j.done) ? j.done : [];
  } catch { return []; }
}

async function send(payload) {
  if (!SUBMIT_URL) return false;
  // text/plain avoids a CORS preflight, which Apps Script does not answer.
  // With no-cors the response is opaque: a resolved fetch means it was delivered.
  await fetch(SUBMIT_URL, {
    method: "POST", mode: "no-cors",
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify(payload),
  });
  return true;
}

async function flushPending() {
  if (!SUBMIT_URL) { renderPending(); return; }
  for (const id of [...state.pending]) {
    try {
      if (await send(state.answers[id])) state.pending = state.pending.filter((x) => x !== id);
    } catch { break; } // offline: retry later
  }
  saveLocal();
  renderPending();
}

function renderPending() {
  const n = state.pending.length;
  dom.pending.hidden = n === 0;
  dom.pending.textContent = SUBMIT_URL
    ? `${n} answer(s) not sent yet — will retry automatically`
    : `Test mode: answers are stored in this browser only (${Object.keys(state.answers).length})`;
  if (!SUBMIT_URL) dom.pending.hidden = false;
}

/* ---------- rendering (as in the main study) ---------- */
function updateProgress() {
  const done = state.order.filter((id) => state.answers[id]).length;
  const total = state.order.length;
  dom.progress.textContent = done >= total ? `All ${total} done` : `Image ${done + 1} of ${total}`;
}

function render() {
  dom.prompt.textContent = state.item.prompt || "";
  dom.image.src = state.item.image;
  dom.image.alt = "Generated image for annotation";
  dom.objectQuestions.innerHTML = "";
  dom.conditionQuestions.innerHTML = "";
  dom.objectsSection.hidden = !state.questions.some((q) => q.kind === "object");

  state.questions.forEach((q, i) => {
    const card = document.createElement("article");
    card.className = "question-card";
    card.dataset.questionIndex = String(i);
    const title = document.createElement("p");
    title.className = "question-text";
    title.innerHTML = q.html;
    const row = document.createElement("div");
    row.className = "answer-row";
    row.append(answerButton(i, true, "[1] Yes"), answerButton(i, false, "[2] No"));
    card.append(title, row);
    (q.kind === "object" ? dom.objectQuestions : dom.conditionQuestions).appendChild(card);
  });
  highlight();
  dom.submit.disabled = !allAnswered();
}

function answerButton(i, value, label) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "answer-btn";
  b.dataset.questionIndex = String(i);
  b.dataset.answer = String(value);
  b.textContent = label;
  b.addEventListener("click", () => setAnswer(i, value));
  return b;
}

function setAnswer(i, value) {
  const q = state.questions[i];
  if (!q) return;
  state.responses[q.key] = value;
  document.querySelectorAll(`.answer-btn[data-question-index="${i}"]`).forEach((b) =>
    b.classList.toggle("selected", (b.dataset.answer === "true") === value));
  const next = state.questions.findIndex((qq, j) => j > i && typeof state.responses[qq.key] !== "boolean");
  state.active = next >= 0 ? next : Math.min(i + 1, state.questions.length - 1);
  highlight();
  dom.submit.disabled = !allAnswered();
}

function setActive(i) {
  if (!state.questions.length) return;
  state.active = Math.max(0, Math.min(i, state.questions.length - 1));
  highlight();
}

function highlight() {
  document.querySelectorAll(".question-card").forEach((c) =>
    c.classList.toggle("active", Number(c.dataset.questionIndex) === state.active));
}

const allAnswered = () => state.questions.every((q) => typeof state.responses[q.key] === "boolean");

/* ---------- flow ---------- */
function loadNext() {
  updateProgress();
  const nextId = state.order.find((id) => !state.answers[id]);
  if (!nextId) {
    state.item = null;
    dom.prompt.textContent = "All images are done. Thank you!";
    dom.image.removeAttribute("src");
    dom.objectQuestions.innerHTML = "";
    dom.conditionQuestions.innerHTML = "";
    dom.submit.disabled = true;
    showStatus(state.pending.length
      ? "Finished, but some answers are not sent yet. Keep this page open while online, or download your answers."
      : "Finished. All answers are saved.");
    return;
  }
  state.item = state.items[nextId];
  state.questions = buildQuestions(state.item);
  state.responses = {};
  state.active = 0;
  state.startedAt = new Date();
  render();
}

async function submit() {
  if (!state.item || !allAnswered() || state.busy) return;
  state.busy = true;
  dom.submit.disabled = true;
  try {
    const submittedAt = new Date();
    const payload = {
      token: state.token,
      itemId: state.item.id,
      objectResponses: state.questions.filter((q) => q.kind === "object")
        .map((q) => ({ object: q.sourceId, response: state.responses[q.key] })),
      conditionResponses: state.questions.filter((q) => q.kind === "condition")
        .map((q) => ({ conditionId: q.sourceId, response: state.responses[q.key] })),
      startedAt: state.startedAt.toISOString(),
      submittedAt: submittedAt.toISOString(),
      durationMs: submittedAt - state.startedAt,
      clientVersion: CLIENT_VERSION,
    };
    state.answers[payload.itemId] = payload;
    if (!state.pending.includes(payload.itemId)) state.pending.push(payload.itemId);
    saveLocal();
    showStatus("Saved.");
    loadNext();
    await flushPending();
  } finally {
    state.busy = false;
  }
}

function download() {
  const blob = new Blob([JSON.stringify({ token: state.token, answers: Object.values(state.answers) }, null, 1)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `e4b_answers_${state.token}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

document.addEventListener("keydown", (e) => {
  if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if (k === "enter" && e.target === dom.submit) return;
  if (k === "1" || k === "y") { e.preventDefault(); setAnswer(state.active, true); }
  else if (k === "2" || k === "n") { e.preventDefault(); setAnswer(state.active, false); }
  else if (k === "arrowdown" || k === "j") { e.preventDefault(); setActive(state.active + 1); }
  else if (k === "arrowup" || k === "k") { e.preventDefault(); setActive(state.active - 1); }
  else if (k === "enter" && allAnswered()) { e.preventDefault(); submit(); }
});
dom.submit.addEventListener("click", submit);
dom.download.addEventListener("click", download);

async function bootstrap() {
  if (!/^[a-z0-9]{8,}$/.test(state.token)) {
    showStatus("This link is incomplete. Please use the personal link you received.", true);
    return;
  }
  try {
    const [items, mine] = await Promise.all([
      fetch("data/items.json").then((r) => r.json()),
      fetch(`data/a/${state.token}.json`).then((r) => { if (!r.ok) throw new Error("unknown link"); return r.json(); }),
    ]);
    items.forEach((it) => { state.items[it.id] = it; });
    state.order = mine.items;
    loadLocal();
    // Items the server already has (e.g. answered on another device) count as done.
    for (const id of await fetchServerDone()) {
      if (!state.answers[id]) state.answers[id] = { itemId: id, fromServer: true };
    }
    dom.name.textContent = `Link ${state.token.slice(0, 4)}…`;
    renderPending();
    loadNext();
    flushPending();
  } catch (err) {
    showStatus(`Could not load your images (${err.message}). Please check the link.`, true);
  }
}

bootstrap();
