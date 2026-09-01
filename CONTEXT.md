# NegGenEval Human Annotation Website — Context

Lightweight annotation site (GitHub Pages + Firebase Firestore) that reproduces NegGenEval's automatic-evaluator logic as human yes/no judgments, to validate it. Vanilla HTML/CSS/JS only — no React/Node/build tooling. Firestore stores metadata/results, not image binaries (served as static files from the repo, or Firebase Storage if the dataset later outgrows GitHub Pages).

Scale: ~500 image-prompt datapoints, ≥3 independent annotators each (~1,500 annotations).

## Data model

A prompt like *"A photo of a red car and a non-wooden bench."* decomposes into:
- **Objects** — one required instance per mentioned category. Negation never applies to object existence, only to attributes/relations (a missing bench still fails the item, even though "no bench" trivially satisfies "non-wooden bench").
- **Conditions** (attribute or relation) — stored as the underlying **positive** predicate + an `expected` boolean (`false` for negated conditions). Annotators are only ever asked the positive form ("Does the bench appear wooden?", never "Is the bench non-wooden?"); the evaluator later checks `humanAnswer == expected`.

Datapoint:
```json
{
  "id": "sample_0001", "prompt": "A photo of a red car and a non-wooden bench.",
  "image": "images/sample_0001.webp",
  "objects": [{"id": "car", "label": "car"}, {"id": "bench", "label": "bench"}],
  "conditions": [
    {"id": "condition_1", "type": "attribute", "category": "color", "subject": "car",
     "predicate": "red", "expected": true, "question": "Is the car red?"},
    {"id": "condition_2", "type": "attribute", "category": "material", "subject": "bench",
     "predicate": "wooden", "expected": false, "question": "Does the bench appear wooden?"}
  ]
}
```
Relation conditions add a `"target"` field (the second object).

Annotation — store raw responses only, never just the derived pass/fail:
```json
{
  "annotatorId": "...", "datapointId": "sample_0001",
  "objectResponses": {"car": true, "bench": true},
  "conditionResponses": {"condition_1": true, "condition_2": false},
  "startedAt": "...", "submittedAt": "...", "durationMs": 8421
}
```

## Question generation

| Kind | Question | Answer means |
|---|---|---|
| Object | "Is exactly one `[object]` present?" | 0 or 2+ instances → No, exactly 1 → Yes. One question per object — never separate presence/count questions. |
| Attribute | positive form, e.g. "Is the car red?" | compared against `expected` |
| Relation | positive form, e.g. "Is the dog left of the cat?" | compared against `expected` |

Attribute categories: color, material. Relation categories: directional (left/right of, above/below), proximity (near/far from), comparative size (bigger/smaller/taller/shorter than), depth (in front of/behind).

All answers are binary Yes/No (`true`/`false`) — no "unclear", "partially correct", or Likert scale.

## Image-level correctness

Derived later, not stored as the primary record. Passes iff every object appears exactly once AND every condition's human answer matches its `expected`. An object failure already fails the item; whether to stop asking its dependent conditions ("benchmark-efficient") or keep asking anyway ("evaluator-analysis") should stay configurable, not hard-coded.

## Hidden from annotators

Model identity, automatic-evaluator predictions/correctness, the `expected` field, other annotators' answers, current annotation counts — to avoid bias.

## Annotator identity & assignment

Each annotator: internal ID, random nickname (e.g. `quiet-otter-381`), optional email, creation timestamp. Identity persists across refreshes (e.g. localStorage); annotations reference the ID, not the email.

Assignment uses a Firestore transaction or other atomic mechanism, not client-side counting:
- Never re-assign a datapoint to an annotator who already completed it.
- Prefer datapoints closest to the target (2 completed before 1, before 0/brand-new) — completes partially-annotated items first rather than spreading coverage thin; stop once the target (3) is reached.
- Support concurrent annotators without over-assignment.

## Firestore collections

| Collection | Key fields |
|---|---|
| `annotators` | `nickname`, `email`, `createdAt` |
| `datapoints` | `prompt`, `image`, `objects[]`, `conditions[]`, `targetAnnotations` |
| `assignments` | `annotatorId`, `datapointId`, `status` (`assigned`/`completed`/`expired`), `assignedAt`, `completedAt` |
| `annotations` | `annotatorId`, `datapointId`, `objectResponses`, `conditionResponses`, `startedAt`, `submittedAt`, `durationMs` |

## UX

Keyboard-only workflow: `Y`/`1` = Yes, `N`/`2` = No, `J`/`Down` = next question, `K`/`Up` = previous, `Enter` = submit. Auto-advance focus after each answer; load the next assigned item immediately after submit, no confirmation dialogs.

## Constraints

- Exactly one instance required per object; absence and duplication both count as object failure.
- Negation always represented as `expected: false` on the positive predicate — never phrase a question in the negative.
- Raw per-annotator answers are the source of truth; derived pass/fail is computed later, never stored as the primary record.
- ≥3 independent annotators per datapoint; no annotator repeats a datapoint.
- Questions generated dynamically from datapoint metadata — no hard-coded prompts, object counts, or condition counts.
