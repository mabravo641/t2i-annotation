import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.2/firebase-app.js";
import {
  addDoc,
  collection,
  doc,
  getDoc,
  getDocs,
  getFirestore,
  query,
  serverTimestamp,
  setDoc,
  updateDoc,
  where,
} from "https://www.gstatic.com/firebasejs/10.13.2/firebase-firestore.js";

const DEFAULT_APP_CONFIG = {
  firebase: {
    apiKey: "REPLACE_WITH_FIREBASE_API_KEY",
    authDomain: "REPLACE_WITH_FIREBASE_AUTH_DOMAIN",
    projectId: "REPLACE_WITH_FIREBASE_PROJECT_ID",
    storageBucket: "REPLACE_WITH_FIREBASE_STORAGE_BUCKET",
    messagingSenderId: "REPLACE_WITH_FIREBASE_MESSAGING_SENDER_ID",
    appId: "REPLACE_WITH_FIREBASE_APP_ID",
  },
  targetAnnotationsPerDatapoint: 3,
  progressTarget: 20,
};

const APP_CONFIG = window.NEGGENEVAL_CONFIG || DEFAULT_APP_CONFIG;
const LOCAL_STORAGE_ANNOTATOR_KEY = "neggeneval_annotator_id";

const dom = {
  statusBanner: document.getElementById("statusBanner"),
  progressText: document.getElementById("progressText"),
  promptText: document.getElementById("promptText"),
  generatedImage: document.getElementById("generatedImage"),
  objectQuestions: document.getElementById("objectQuestions"),
  conditionQuestions: document.getElementById("conditionQuestions"),
  submitBtn: document.getElementById("submitBtn"),
  annotatorDialog: document.getElementById("annotatorDialog"),
  annotatorForm: document.getElementById("annotatorForm"),
  emailInput: document.getElementById("emailInput"),
  cancelAnnotatorBtn: document.getElementById("cancelAnnotatorBtn"),
};

const state = {
  annotator: null,
  assignment: null,
  datapoint: null,
  questions: [],
  responses: {},
  activeQuestionIndex: 0,
  startedAt: null,
  completedCount: 0,
  isSubmitting: false,
};

const adjectives = [
  "quiet",
  "brisk",
  "bright",
  "steady",
  "calm",
  "rapid",
  "mellow",
  "gentle",
  "clear",
  "nimble",
  "bold",
  "kind",
];
const animals = [
  "otter",
  "lynx",
  "sparrow",
  "badger",
  "heron",
  "fox",
  "seal",
  "falcon",
  "wolf",
  "beaver",
  "rabbit",
  "dolphin",
];

/* Firebase initialization: configure and create Firestore client for static GitHub Pages usage. */
let db = null;

function showStatus(message, isError = false) {
  dom.statusBanner.textContent = message;
  dom.statusBanner.style.color = isError ? "#b42318" : "#03543f";
}

function hasPlaceholderFirebaseConfig() {
  return Object.values(APP_CONFIG.firebase || {}).some((value) =>
    String(value || "").startsWith("REPLACE_WITH_")
  );
}

function generateNickname() {
  const adjective = adjectives[Math.floor(Math.random() * adjectives.length)];
  const animal = animals[Math.floor(Math.random() * animals.length)];
  const number = Math.floor(1000 + Math.random() * 9000);
  return `${adjective}-${animal}-${number}`;
}

function uuid() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `annotator-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
}

/* Annotator initialization: load local annotator ID or collect email once, then persist annotator profile. */
async function initializeAnnotator() {
  let annotatorId = localStorage.getItem(LOCAL_STORAGE_ANNOTATOR_KEY);

  if (annotatorId) {
    const snap = await getDoc(doc(db, "annotators", annotatorId));
    if (snap.exists()) {
      state.annotator = snap.data();
      return;
    }
    localStorage.removeItem(LOCAL_STORAGE_ANNOTATOR_KEY);
    annotatorId = null;
  }

  const email = await promptForEmail();
  if (!email) throw new Error("Annotator setup cancelled.");

  const nickname = generateNickname();
  annotatorId = uuid();
  const annotator = {
    annotatorId,
    email,
    nickname,
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  };

  await setDoc(doc(db, "annotators", annotatorId), annotator);
  localStorage.setItem(LOCAL_STORAGE_ANNOTATOR_KEY, annotatorId);

  state.annotator = { ...annotator, createdAt: new Date(), updatedAt: new Date() };
}

function promptForEmail() {
  return new Promise((resolve) => {
    const form = dom.annotatorForm;

    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      dom.cancelAnnotatorBtn.removeEventListener("click", onCancel);
      dom.annotatorDialog.removeEventListener("cancel", onCancel);
    };

    const onSubmit = (event) => {
      event.preventDefault();
      if (!dom.emailInput.checkValidity()) {
        dom.emailInput.reportValidity();
        return;
      }
      const email = dom.emailInput.value.trim().toLowerCase();
      cleanup();
      dom.annotatorDialog.close();
      resolve(email);
    };

    const onCancel = () => {
      cleanup();
      dom.annotatorDialog.close();
      resolve("");
    };

    form.addEventListener("submit", onSubmit);
    dom.cancelAnnotatorBtn.addEventListener("click", onCancel);
    dom.annotatorDialog.addEventListener("cancel", onCancel);
    dom.annotatorDialog.showModal();
    dom.emailInput.focus();
  });
}

/* Loading datapoints and assignment selection: avoid duplicate annotator assignments and prefer lower completion counts. */
async function getOrCreateAssignment() {
  const openAssignmentQuery = query(
    collection(db, "assignments"),
    where("annotatorId", "==", state.annotator.annotatorId),
    where("status", "==", "assigned")
  );
  const openAssignments = await getDocs(openAssignmentQuery);
  if (!openAssignments.empty) {
    const first = openAssignments.docs[0];
    return { assignmentId: first.id, ...first.data() };
  }

  const seenDatapoints = await getAnnotatorSeenDatapointIds();
  const completedCountsByDatapoint = await getCompletedAssignmentCountsMap();
  const datapointsSnapshot = await getDocs(collection(db, "datapoints"));
  const candidates = [];

  for (const datapointDoc of datapointsSnapshot.docs) {
    if (seenDatapoints.has(datapointDoc.id)) continue;
    const completedCount = completedCountsByDatapoint.get(datapointDoc.id) || 0;
    candidates.push({
      datapointId: datapointDoc.id,
      completedCount,
    });
  }

  if (!candidates.length) return null;

  const minCount = Math.min(...candidates.map((candidate) => candidate.completedCount));
  const prioritized = candidates.filter(
    (candidate) => candidate.completedCount === minCount
  );
  const underTarget = prioritized.filter(
    (candidate) => candidate.completedCount < (APP_CONFIG.targetAnnotationsPerDatapoint || 3)
  );

  const pool = underTarget.length ? underTarget : prioritized;
  const selected = pool[Math.floor(Math.random() * pool.length)];

  // TODO: replace this block with a Firestore transaction when moving to production multi-user scale.
  // Keep assignment creation isolated so this can be upgraded to a Firestore transaction safely.
  // Without a transaction, simultaneous clients can over-assign the same datapoint.
  const assignmentRef = await addDoc(collection(db, "assignments"), {
    annotatorId: state.annotator.annotatorId,
    datapointId: selected.datapointId,
    status: "assigned",
    assignedAt: serverTimestamp(),
    completedAt: null,
  });

  return {
    assignmentId: assignmentRef.id,
    datapointId: selected.datapointId,
    status: "assigned",
  };
}

async function getAnnotatorSeenDatapointIds() {
  const seen = new Set();

  const assignmentSnap = await getDocs(
    query(collection(db, "assignments"), where("annotatorId", "==", state.annotator.annotatorId))
  );
  assignmentSnap.forEach((docSnap) => {
    const datapointId = docSnap.data().datapointId;
    if (datapointId) seen.add(datapointId);
  });

  const annotationSnap = await getDocs(
    query(collection(db, "annotations"), where("annotatorId", "==", state.annotator.annotatorId))
  );
  annotationSnap.forEach((docSnap) => {
    const datapointId = docSnap.data().datapointId;
    if (datapointId) seen.add(datapointId);
  });

  return seen;
}

async function getCompletedAssignmentCountsMap() {
  const completedSnap = await getDocs(
    query(collection(db, "assignments"), where("status", "==", "completed"))
  );

  const perDatapointAnnotators = new Map();
  completedSnap.forEach((docSnap) => {
    const { annotatorId, datapointId } = docSnap.data();
    if (!annotatorId || !datapointId) return;

    if (!perDatapointAnnotators.has(datapointId)) {
      perDatapointAnnotators.set(datapointId, new Set());
    }
    perDatapointAnnotators.get(datapointId).add(annotatorId);
  });

  const counts = new Map();
  perDatapointAnnotators.forEach((annotators, datapointId) => {
    counts.set(datapointId, annotators.size);
  });
  return counts;
}

async function loadDatapoint(datapointId) {
  const snap = await getDoc(doc(db, "datapoints", datapointId));
  if (!snap.exists()) throw new Error(`Datapoint not found: ${datapointId}`);
  return { id: snap.id, ...snap.data() };
}

function updateProgressText() {
  const target = APP_CONFIG.progressTarget || 20;
  const current = Math.min(state.completedCount + 1, target);
  dom.progressText.textContent = `Annotation ${current} of ${target}`;
}

async function refreshCompletedCount() {
  const completedSnap = await getDocs(
    query(
      collection(db, "assignments"),
      where("annotatorId", "==", state.annotator.annotatorId),
      where("status", "==", "completed")
    )
  );
  state.completedCount = completedSnap.size;
}

function buildQuestions(datapoint) {
  const questions = [];

  (datapoint.objects || []).forEach((objectName, index) => {
    const normalizedObject = String(objectName).trim();
    questions.push({
      key: `object:${index}:${normalizedObject}`,
      kind: "object",
      sourceId: normalizedObject,
      text: `Is exactly one ${normalizedObject} present in the image?`,
    });
  });

  (datapoint.conditions || []).forEach((condition, index) => {
    questions.push({
      key: `condition:${condition.id || index}`,
      kind: "condition",
      sourceId: condition.id || String(index),
      text: condition.question,
    });
  });

  return questions;
}

/* Dynamic question rendering: generate all object and condition questions from datapoint data only. */
function renderDatapoint() {
  dom.promptText.textContent = state.datapoint.prompt || "";
  dom.generatedImage.src = state.datapoint.imageUrl || "";
  dom.generatedImage.alt = `Generated image for datapoint ${state.datapoint.id}`;

  dom.objectQuestions.innerHTML = "";
  dom.conditionQuestions.innerHTML = "";

  state.questions.forEach((question, questionIndex) => {
    const card = document.createElement("article");
    card.className = "question-card";
    card.dataset.questionIndex = String(questionIndex);

    const title = document.createElement("p");
    title.className = "question-text";
    title.textContent = question.text;

    const answerRow = document.createElement("div");
    answerRow.className = "answer-row";

    const correctButton = createAnswerButton(question, questionIndex, true, "[1] ✓ Correct");
    const wrongButton = createAnswerButton(question, questionIndex, false, "[2] ✗ Wrong");

    answerRow.append(correctButton, wrongButton);
    card.append(title, answerRow);

    if (question.kind === "object") {
      dom.objectQuestions.appendChild(card);
    } else {
      dom.conditionQuestions.appendChild(card);
    }
  });

  applyActiveQuestionHighlight();
  updateSubmitEnabled();
}

function createAnswerButton(question, questionIndex, answerValue, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "answer-btn";
  button.dataset.questionKey = question.key;
  button.dataset.answer = String(answerValue);
  button.dataset.questionIndex = String(questionIndex);
  button.textContent = label;

  button.addEventListener("click", () => {
    setAnswer(questionIndex, answerValue);
  });

  return button;
}

function setAnswer(questionIndex, value) {
  const question = state.questions[questionIndex];
  if (!question) return;

  state.responses[question.key] = value;
  syncSelectedButtons(questionIndex);
  moveToNextUnanswered(questionIndex + 1);
  updateSubmitEnabled();
}

function syncSelectedButtons(questionIndex) {
  const question = state.questions[questionIndex];
  const selectedValue = state.responses[question.key];

  document
    .querySelectorAll(`.answer-btn[data-question-index="${questionIndex}"]`)
    .forEach((button) => {
      const buttonValue = button.dataset.answer === "true";
      button.classList.toggle("selected", buttonValue === selectedValue);
    });
}

function moveToNextUnanswered(startIndex) {
  const unansweredIndex = state.questions.findIndex(
    (question, index) => index >= startIndex && typeof state.responses[question.key] !== "boolean"
  );

  if (unansweredIndex >= 0) {
    state.activeQuestionIndex = unansweredIndex;
  } else {
    state.activeQuestionIndex = Math.min(startIndex, state.questions.length - 1);
  }

  applyActiveQuestionHighlight();
}

function setActiveQuestion(index) {
  if (!state.questions.length) return;
  const clamped = Math.max(0, Math.min(index, state.questions.length - 1));
  state.activeQuestionIndex = clamped;
  applyActiveQuestionHighlight();
}

function applyActiveQuestionHighlight() {
  document.querySelectorAll(".question-card").forEach((card) => {
    const index = Number(card.dataset.questionIndex);
    card.classList.toggle("active", index === state.activeQuestionIndex);
  });
}

function areAllQuestionsAnswered() {
  return state.questions.every((question) => typeof state.responses[question.key] === "boolean");
}

function updateSubmitEnabled() {
  dom.submitBtn.disabled = !areAllQuestionsAnswered();
}

function hasObjectFailure() {
  return state.questions
    .filter((question) => question.kind === "object")
    .some((question) => state.responses[question.key] === false);
}

/* Response collection and annotation submission: store raw boolean responses, times, duration, and assignment linkage. */
async function submitCurrentAnnotation() {
  if (!areAllQuestionsAnswered() || state.isSubmitting) return;
  state.isSubmitting = true;

  try {
    const submittedAt = new Date();
    const durationMs = submittedAt.getTime() - state.startedAt.getTime();

    const objectResponses = state.questions
      .filter((question) => question.kind === "object")
      .map((question) => ({
        object: question.sourceId,
        response: state.responses[question.key],
      }));

    const conditionResponses = state.questions
      .filter((question) => question.kind === "condition")
      .map((question) => ({
        conditionId: question.sourceId,
        response: state.responses[question.key],
      }));

    const annotationPayload = {
      annotatorId: state.annotator.annotatorId,
      datapointId: state.datapoint.id,
      assignmentId: state.assignment.assignmentId,
      objectResponses,
      conditionResponses,
      startedAt: state.startedAt,
      submittedAt,
      durationMs,
      timestamp: serverTimestamp(),
      objectFailure: hasObjectFailure(),
    };

    const annotationRef = await addDoc(collection(db, "annotations"), annotationPayload);

    await updateDoc(doc(db, "assignments", state.assignment.assignmentId), {
      status: "completed",
      completedAt: serverTimestamp(),
      submittedAnnotationId: annotationRef.id,
    });

    showStatus("Annotation saved. Loading next datapoint...");
    await loadNextDatapoint();
  } finally {
    state.isSubmitting = false;
  }
}

/* Next-datapoint assignment: complete one annotation, then fetch or create the next eligible assignment. */
async function loadNextDatapoint() {
  await refreshCompletedCount();
  updateProgressText();

  state.assignment = await getOrCreateAssignment();

  if (!state.assignment) {
    state.datapoint = null;
    state.questions = [];
    state.responses = {};
    dom.promptText.textContent = "No eligible datapoints remaining for this annotator.";
    dom.generatedImage.removeAttribute("src");
    dom.objectQuestions.innerHTML = "";
    dom.conditionQuestions.innerHTML = "";
    dom.submitBtn.disabled = true;
    showStatus("No assignments available right now.");
    return;
  }

  state.datapoint = await loadDatapoint(state.assignment.datapointId);
  state.questions = buildQuestions(state.datapoint);
  state.responses = {};
  state.activeQuestionIndex = 0;
  state.startedAt = new Date();

  renderDatapoint();
  showStatus(`Annotating as ${state.annotator.nickname}`);
}

function setupKeyboardShortcuts() {
  document.addEventListener("keydown", async (event) => {
    if (dom.annotatorDialog.open) return;
    if (event.target && ["INPUT", "TEXTAREA"].includes(event.target.tagName)) return;

    const key = event.key.toLowerCase();
    if (key === "enter" && event.target === dom.submitBtn) return;

    if (key === "1" || key === "y") {
      event.preventDefault();
      setAnswer(state.activeQuestionIndex, true);
      return;
    }

    if (key === "2" || key === "n") {
      event.preventDefault();
      setAnswer(state.activeQuestionIndex, false);
      return;
    }

    if (key === "arrowdown" || key === "j") {
      event.preventDefault();
      setActiveQuestion(state.activeQuestionIndex + 1);
      return;
    }

    if (key === "arrowup" || key === "k") {
      event.preventDefault();
      setActiveQuestion(state.activeQuestionIndex - 1);
      return;
    }

    if (key === "enter" && areAllQuestionsAnswered()) {
      event.preventDefault();
      dom.submitBtn.disabled = true;
      try {
        await submitCurrentAnnotation();
      } catch (error) {
        dom.submitBtn.disabled = false;
        showStatus(error.message || "Submission failed.", true);
      }
    }
  });
}

dom.submitBtn.addEventListener("click", async () => {
  dom.submitBtn.disabled = true;
  try {
    await submitCurrentAnnotation();
  } catch (error) {
    dom.submitBtn.disabled = false;
    showStatus(error.message || "Submission failed.", true);
  }
});

async function bootstrap() {
  if (hasPlaceholderFirebaseConfig()) {
    showStatus("Firebase configuration is missing. Update app.js configuration values.", true);
    return;
  }

  try {
    const firebaseApp = initializeApp(APP_CONFIG.firebase);
    db = getFirestore(firebaseApp);
    await initializeAnnotator();
    setupKeyboardShortcuts();
    await loadNextDatapoint();
  } catch (error) {
    showStatus(error.message || "Initialization failed.", true);
  }
}

bootstrap();
