import {
  addDoc,
  collection,
  doc,
  serverTimestamp,
  updateDoc,
} from "https://www.gstatic.com/firebasejs/10.13.2/firebase-firestore.js";

import { hasPlaceholderFirebaseConfig } from "./config.js";
import { initFirebase, db } from "./firebaseClient.js";
import { dom } from "./dom.js";
import { state } from "./state.js";
import { showStatus } from "./status.js";
import { initializeAnnotator, setupAnnotatorControls } from "./identity.js";
import { buildQuestions } from "./questions.js";
import {
  getEligibleCandidates,
  getOrCreateAssignment,
  loadDatapoint,
  refreshCompletedCount,
} from "./assignment.js";
import {
  renderDatapoint,
  updateProgressText,
  areAllQuestionsAnswered,
  hasObjectFailure,
} from "./render.js";
import { setupKeyboardShortcuts } from "./keyboard.js";

/* Next-datapoint assignment: refresh this annotator's progress, then fetch or
   create the next eligible assignment. */
async function loadNextDatapoint() {
  await refreshCompletedCount();
  const candidates = await getEligibleCandidates();
  state.remainingCount = candidates.length;
  updateProgressText();

  state.assignment = await getOrCreateAssignment(candidates);

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
    showStatus("Firebase configuration is missing. Update js/config.js values.", true);
    return;
  }

  try {
    initFirebase();
    await initializeAnnotator();
    setupAnnotatorControls(loadNextDatapoint);
    setupKeyboardShortcuts(submitCurrentAnnotation);
    await loadNextDatapoint();
  } catch (error) {
    showStatus(error.message || "Initialization failed.", true);
  }
}

bootstrap();
