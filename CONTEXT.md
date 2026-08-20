# NegGenEval Human Annotation Website — Context

## Purpose

This repository contains a lightweight human annotation website for validating the automatic evaluation pipeline of **NegGenEval**.

NegGenEval is a benchmark for evaluating whether text-to-image models correctly understand and satisfy prompts containing both positive and negated attributes and relations.

The human annotation interface should mimic the logic of the automatic evaluator as closely as possible, while remaining fast and simple for annotators to use.

The website is intended to be hosted with **GitHub Pages**, while **Firebase Firestore** is used to store annotator information, assignments, and annotation results.

---

## Main Goal

The human evaluation should answer:

> Does the human judgment agree with the automatic NegGenEval evaluator?

The interface should therefore collect low-level binary visual judgments rather than asking annotators for a single subjective overall score.

The website must support:

* Required object checks.
* Object multiplicity checks.
* Attribute checks.
* Relation checks.
* Positive and negated conditions.
* Multiple independent annotators per image-text pair.
* Efficient keyboard-based annotation.

---

## Expected Scale

The initial experiment may contain approximately:

* 500 image-text datapoints.
* At least 3 independent annotators per datapoint.
* Approximately 1,500 completed image-level annotations.

The system should also remain usable if the dataset grows or includes generations from multiple text-to-image models.

---

## Technology

Keep the implementation simple.

Use:

* HTML.
* CSS.
* Vanilla JavaScript.
* GitHub Pages for hosting.
* Firebase Firestore for the database.

Do not introduce React, Node.js, npm, or build tooling unless there is a clear need later.

Images should normally be stored as static files in the GitHub repository and served through GitHub Pages.

Firestore should store metadata and annotation results, not image binaries.

Suggested repository structure:

```text
/
├── index.html
├── style.css
├── app.js
├── CONTEXT.md
└── images/
    ├── 000001.webp
    ├── 000002.webp
    └── ...
```

---

## NegGenEval Evaluation Philosophy

NegGenEval extends GenEval with explicit negation.

Prompts may contain:

* Required objects.
* Positive attributes.
* Negated attributes.
* Positive relations.
* Negated relations.
* Combinations of positive and negative conditions.

Negation applies to an attribute or relation, not to object existence.

Example:

```text
A photo of a red car and a non-wooden bench.
```

Both the car and the bench are required to appear.

The benchmark should fail if the bench is missing, even though the absence of a bench would also imply that no wooden bench is visible.

---

## Object Requirements

All prompts are constructed with one required instance per mentioned object category.

For every required object, the human evaluator should answer:

```text
Is exactly one [object] present in the image?
```

Examples:

```text
Is exactly one car present in the image?

Is exactly one bench present in the image?
```

This question is binary.

Possible responses:

```text
Yes
No
```

Interpretation:

```text
0 instances  -> No
1 instance   -> Yes
2+ instances -> No
```

Do not create separate questions for object presence and object count.

A required object is considered correct only when exactly one instance of that category appears.

---

## Attribute Evaluation

Current attribute categories include:

* Color.
* Material.

Example prompt:

```text
A photo of a red car and a non-wooden bench.
```

The interface should ask:

```text
Is the car red?

Does the bench appear wooden?
```

Important:

The human should always judge the underlying positive visual property.

Do not ask:

```text
Is the bench non-wooden?
```

Instead store negation internally.

Example datapoint representation:

```json
{
  "type": "attribute",
  "subject": "bench",
  "predicate": "wooden",
  "expected": false
}
```

The human answer describes whether `wooden` is visually true.

The evaluator later compares:

```text
humanAnswer == expected
```

to determine whether the prompt condition is satisfied.

---

## Relation Evaluation

Current relation categories include:

### Directional

* left of
* right of
* above
* below

### Proximity

* near
* far from

### Comparative size

* bigger than
* smaller than
* taller than
* shorter than

### Depth

* in front of
* behind

Example prompt:

```text
A photo of a dog, a cat, and a bench,
where the dog is left of the cat
and is not below the bench.
```

The interface should ask:

```text
Is the dog left of the cat?

Is the dog below the bench?
```

Internally:

```text
left_of(dog, cat)
expected = true

below(dog, bench)
expected = false
```

Again, annotators judge only the positive visual statement.

---

## Annotation Answers

All annotation questions are boolean.

Use:

```text
Yes
No
```

Internally store:

```text
true
false
```

Do not use:

* Partially correct.
* Unclear.
* Cannot determine.
* Likert scales.
* Free-form correctness ratings.

The goal is to reproduce the binary decisions made by the automatic evaluator.

---

## Datapoint Structure

The annotation interface must be data-driven.

Different prompts contain different numbers of objects and conditions, so questions must be generated dynamically.

A datapoint may look like:

```json
{
  "id": "sample_0001",
  "promptId": "prompt_0001",
  "prompt": "A photo of a red car and a non-wooden bench.",
  "image": "images/sample_0001.webp",

  "objects": [
    {
      "id": "car",
      "label": "car"
    },
    {
      "id": "bench",
      "label": "bench"
    }
  ],

  "conditions": [
    {
      "id": "condition_1",
      "type": "attribute",
      "category": "color",
      "subject": "car",
      "predicate": "red",
      "expected": true,
      "question": "Is the car red?"
    },
    {
      "id": "condition_2",
      "type": "attribute",
      "category": "material",
      "subject": "bench",
      "predicate": "wooden",
      "expected": false,
      "question": "Does the bench appear wooden?"
    }
  ]
}
```

For relation datapoints, a condition can also contain:

```json
{
  "target": "cat"
}
```

---

## What Annotators Should See

The annotation screen should show:

1. The full prompt.
2. The generated image.
3. Required-object questions.
4. Attribute or relation questions.
5. A submit/next action.
6. Progress information when available.

Example:

```text
Prompt

A photo of a red car and a non-wooden bench.


[ IMAGE ]


Required objects

Is exactly one car present?
[Y] Yes     [N] No

Is exactly one bench present?
[Y] Yes     [N] No


Conditions

Is the car red?
[Y] Yes     [N] No

Does the bench appear wooden?
[Y] Yes     [N] No
```

---

## Information Hidden From Annotators

Do not show annotators:

* The text-to-image model name.
* Automatic evaluator predictions.
* Automatic benchmark correctness.
* The `expected` field.
* Whether a condition is internally positive or negative beyond what is naturally visible in the original prompt.
* Other annotators' answers.
* Current annotation counts for the datapoint.

This is intended to reduce bias.

---

## Annotator Identity

Each annotator should have:

* A unique internal annotator ID.
* A random human-readable nickname.
* An email address if collected.
* A creation timestamp.

Example nickname:

```text
quiet-otter-381
```

The nickname is for convenient tracking only.

Annotations should reference the internal annotator ID rather than duplicating the email address.

The annotator identity should persist across page refreshes, for example using browser local storage.

The same annotator must never annotate the same datapoint twice.

---

## Assignment Requirements

Each datapoint should receive at least:

```text
3 independent annotators
```

The assignment system should:

1. Never assign a datapoint to an annotator who has already completed it.
2. Prefer datapoints with the fewest completed annotations.
3. Stop assigning a datapoint once the target number of annotations has been reached.
4. Support multiple annotators using the website simultaneously.
5. Avoid race conditions where too many annotators are assigned to the same item.

Assignments should be stored separately from completed annotations.

Suggested collections:

```text
annotators
datapoints
assignments
annotations
```

The final implementation should preferably use a Firestore transaction or another atomic mechanism for assignment.

Do not rely only on client-side counting if simultaneous annotation can cause over-assignment.

---

## Annotation Storage

Store raw human responses.

Do not store only the derived overall pass/fail result.

Example:

```json
{
  "annotatorId": "annotator_abc123",
  "datapointId": "sample_0001",

  "objectResponses": {
    "car": true,
    "bench": true
  },

  "conditionResponses": {
    "condition_1": true,
    "condition_2": false
  },

  "startedAt": "...",
  "submittedAt": "...",
  "durationMs": 8421
}
```

The expected answers belong to the datapoint definition, not to the annotation document.

This allows later analysis such as:

```text
Human judgment vs automatic evaluator
Human majority vote
Per-condition agreement
Positive vs negated condition agreement
Object-detection agreement
Attribute agreement
Relation agreement
Inter-annotator agreement
```

---

## Image-Level Correctness

Image-level correctness can be derived later.

Conceptually, an image passes only when:

1. Every required object appears exactly once.
2. Every positive condition is judged true.
3. Every negated condition is judged false.

Example:

```text
human answer: wooden(bench) = false
expected:     wooden(bench) = false

condition passes
```

Do not make derived benchmark correctness the primary annotation stored in Firestore.

Always keep the raw decisions.

---

## Object Failure and Early Submission

If any required-object question is answered `No`, the image already fails the NegGenEval image-level evaluation.

The implementation should keep early submission possible.

However, this behavior should remain configurable.

Two useful modes are:

```text
Mode A — Benchmark-efficient
Stop evaluating conditions after an object failure.

Mode B — Evaluator-analysis
Continue evaluating any conditions that remain visually meaningful.
```

The first version may use either mode, but the code should not make this impossible to change later.

---

## Annotation Efficiency

Efficiency is important because annotators may label hundreds of images.

The entire workflow should be usable without a mouse.

Recommended shortcuts:

```text
Y or 1     -> Yes
N or 2     -> No

J or Down  -> Next question
K or Up    -> Previous question

Enter      -> Submit when complete
```

After answering a question:

* Save the local response.
* Automatically move focus to the next unanswered question.
* Visually highlight the currently active question.

Buttons should display shortcuts when useful:

```text
[Y / 1] Yes

[N / 2] No
```

Avoid unnecessary confirmation dialogs between datapoints.

After successful submission, immediately load the next assigned item.

---

## UI Principles

The interface should be:

* Minimal.
* Fast.
* Clear.
* Suitable for a scientific annotation study.
* Usable on normal laptop screens.
* Keyboard friendly.

The image should be large enough to judge:

* Object count.
* Color.
* Material.
* Direction.
* Proximity.
* Relative size.
* Depth.

Questions should be visually separated and easy to scan.

Avoid decorative UI that slows annotation.

---

## Firestore Responsibilities

Firestore should store:

```text
annotators
datapoints
assignments
annotations
```

Optionally it may also store:

```text
experiment configuration
annotation target per datapoint
dataset version
```

Firestore should not normally store image binaries.

Images should be served as static files from GitHub Pages unless the dataset later becomes too large, in which case Firebase Storage or another object-storage service can be considered.

---

## Suggested Firestore Collections

### `annotators`

```json
{
  "nickname": "quiet-otter-381",
  "email": "example@example.com",
  "createdAt": "..."
}
```

### `datapoints`

```json
{
  "promptId": "prompt_0001",
  "prompt": "...",
  "image": "images/sample_0001.webp",
  "objects": [],
  "conditions": [],
  "targetAnnotations": 3
}
```

### `assignments`

```json
{
  "annotatorId": "...",
  "datapointId": "...",
  "status": "assigned",
  "assignedAt": "...",
  "completedAt": null
}
```

Possible statuses:

```text
assigned
completed
expired
```

### `annotations`

```json
{
  "annotatorId": "...",
  "datapointId": "...",
  "objectResponses": {},
  "conditionResponses": {},
  "startedAt": "...",
  "submittedAt": "...",
  "durationMs": 0
}
```

---

## Important Constraints

The implementation must preserve these constraints:

1. Exactly one required object instance is considered correct.
2. Object absence and object duplication both count as object failure.
3. Human condition questions describe positive visual predicates.
4. Negation is represented internally using `expected = false`.
5. Human answers are binary.
6. Raw answers must be preserved.
7. At least three independent annotators should evaluate each datapoint.
8. No annotator should label the same datapoint twice.
9. Annotators should not see model identity or automatic evaluator results.
10. Annotation should be optimized for speed and keyboard use.
11. Questions must be generated dynamically from datapoint metadata.
12. The website should remain simple enough to host on GitHub Pages.

---

## Development Philosophy

Prefer simple, readable code over unnecessary abstractions.

When modifying the website:

* Do not hard-code specific prompts.
* Do not hard-code a fixed number of objects.
* Do not hard-code a fixed number of conditions.
* Keep dataset-specific information in datapoint metadata.
* Keep annotation rendering generic.
* Keep Firestore logic separated from UI logic where practical.
* Preserve the distinction between raw human judgments and derived benchmark scores.

The website is a research annotation tool, not a general-purpose survey platform.

Its main purpose is to produce reliable human labels that can be directly compared with the NegGenEval automatic evaluation pipeline.
