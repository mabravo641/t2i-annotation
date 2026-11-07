/* Question-text rendering always asks about the positive visual predicate.
   Negation remains internal in condition.expected/polarity and must not bias
   the annotator's low-level visual judgment. */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function highlightObject(value) {
  return `<strong class="hl-object">${escapeHtml(value)}</strong>`;
}

function highlightKey(value) {
  return `<strong class="hl-key">${escapeHtml(value)}</strong>`;
}

function buildObjectQuestionHtml(objectName) {
  return `Is there exactly one ${highlightObject(objectName)} in the image?`;
}

function buildConditionQuestionHtml(condition) {
  if (condition.type === "attribute" && condition.subject && condition.predicate) {
    if (condition.category === "material") {
      return `Does the ${highlightObject(condition.subject)} appear ${highlightKey(condition.predicate)}?`;
    }
    return `Is the ${highlightObject(condition.subject)} ${highlightKey(condition.predicate)}?`;
  }

  if (condition.type === "relation" && condition.subject && condition.predicate && condition.target) {
    return `Is the ${highlightObject(condition.subject)} ${highlightKey(condition.predicate)} the ${highlightObject(condition.target)}?`;
  }

  if (condition.type === "object" && condition.subject) {
    return buildObjectQuestionHtml(condition.subject);
  }

  return escapeHtml(condition.question || "");
}

export function buildQuestions(datapoint) {
  const questions = [];
  const conditionObjectIds = new Set(
    (datapoint.conditions || [])
      .map((condition) => String(condition.id || ""))
      .filter((conditionId) => conditionId.startsWith("obj_"))
      .map((conditionId) => conditionId.slice(4))
  );

  (datapoint.objects || []).forEach((objectName, index) => {
    const normalizedObject = String(objectName).trim();
    if (conditionObjectIds.has(normalizedObject)) return;
    questions.push({
      key: `object:${index}:${normalizedObject}`,
      kind: "object",
      sourceId: normalizedObject,
      html: buildObjectQuestionHtml(normalizedObject),
    });
  });

  (datapoint.conditions || []).forEach((condition, index) => {
    questions.push({
      key: `condition:${condition.id || index}`,
      kind: "condition",
      sourceId: condition.id || String(index),
      html: buildConditionQuestionHtml(condition),
    });
  });

  return questions;
}
