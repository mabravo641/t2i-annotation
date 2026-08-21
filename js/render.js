import { dom } from "./dom.js";
import { state } from "./state.js";

/* Progress reflects this annotator's own completed count against everything
   still eligible for them right now (state.remainingCount, refreshed in
   main.js's loadNextDatapoint), not a fixed constant. */
export function updateProgressText() {
  const remaining = state.remainingCount || 0;
  const total = state.completedCount + remaining;

  if (total === 0) {
    dom.progressText.textContent = "No datapoints available";
    return;
  }

  const current = Math.min(state.completedCount + 1, total);
  dom.progressText.textContent = `Annotation ${current} of ${total}`;
}

/* Dynamic question rendering: generate all object and condition questions from datapoint data only. */
export function renderDatapoint() {
  dom.annotatorName.textContent = `Annotating as ${state.annotator.nickname}`;
  dom.promptText.textContent = state.datapoint.prompt || "";
  dom.generatedImage.src = state.datapoint.imageUrl || "";
  dom.generatedImage.alt = `Generated image for datapoint ${state.datapoint.id}`;

  dom.objectQuestions.innerHTML = "";
  dom.conditionQuestions.innerHTML = "";
  dom.objectsSection.hidden = !state.questions.some(
    (question) => question.kind === "object"
  );

  state.questions.forEach((question, questionIndex) => {
    const card = document.createElement("article");
    card.className = "question-card";
    card.dataset.questionIndex = String(questionIndex);

    const title = document.createElement("p");
    title.className = "question-text";
    title.innerHTML = question.html;

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

export function setAnswer(questionIndex, value) {
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

export function setActiveQuestion(index) {
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

export function areAllQuestionsAnswered() {
  return state.questions.every((question) => typeof state.responses[question.key] === "boolean");
}

export function updateSubmitEnabled() {
  dom.submitBtn.disabled = !areAllQuestionsAnswered();
}

export function hasObjectFailure() {
  return state.questions
    .filter((question) => question.kind === "object")
    .some((question) => state.responses[question.key] === false);
}
