import { dom } from "./dom.js";
import { state } from "./state.js";
import { showStatus } from "./status.js";
import { setAnswer, setActiveQuestion, areAllQuestionsAnswered } from "./render.js";

export function setupKeyboardShortcuts(onSubmit) {
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
        await onSubmit();
      } catch (error) {
        dom.submitBtn.disabled = false;
        showStatus(error.message || "Submission failed.", true);
      }
    }
  });
}
