# Annotation data scripts

Run from repo root: `.venv/bin/python t2i-annotation/src/<script>.py`
Needs Firebase Admin SDK credentials at `firebase/neg-gen-firebase-adminsdk-fbsvc-caafa15513.json` (outside this repo, never commit it).

## Upload datapoints

```bash
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \
  --models flux2 flux2-4bit qwen sd35 --num-prompts 40 --seed 42 --dry-run
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \
  --models flux2 flux2-4bit qwen sd35 --num-prompts 40 --seed 42
```

`--num-prompts N` = N prompt groups (one image per requested model each), not N images. Existing prompt groups are completed first (missing models only, never re-uploads a source image); new prompts fill the least-represented category/setup buckets. `--models` restricts selection to prompts available for that model (or pass `--include-unknown-correctness`). `--unpaired --num-total N` selects images independently instead (legacy mode). All options: top of `select_and_upload_datapoints.py`.

Every upload is logged to `samples/manifests/upload_manifest.jsonl`, rewritten from live Firestore after each run.

## Initialize the 3-annotator cap

Run once before deploying assignment code, so existing datapoints get their reserved-slot counters:

```bash
.venv/bin/python t2i-annotation/src/backfill_assignment_slots.py           # audit (dry run)
.venv/bin/python t2i-annotation/src/backfill_assignment_slots.py --apply
```

Review `OVER TARGET`/orphan warnings before applying. Slots stay reserved if an annotator abandons a datapoint — needs manual cleanup later. New datapoints start with zero reserved slots.

## Download annotations

```bash
.venv/bin/python t2i-annotation/src/download_annotations.py
```

Writes `samples/annotations/annotations.jsonl` (full fidelity) + `.csv` (one row per response), joined with each datapoint's prompt/model/category and annotator nickname/email. Overwrites every run.

## Download images

```bash
.venv/bin/python t2i-annotation/src/download_firebase_images.py   # --overwrite to redownload all
```

Saves to `samples/images/` + `images_manifest.jsonl` (local file ↔ Firebase Storage path). Skips existing files, so an interrupted download resumes. `samples/` is gitignored.

## Verify / reset

```bash
.venv/bin/python t2i-annotation/src/verify_datapoints.py --skip-image-check   # faster
.venv/bin/python t2i-annotation/src/verify_datapoints.py                     # + image integrity

.venv/bin/python t2i-annotation/src/reset_test_data.py --dry-run
.venv/bin/python t2i-annotation/src/reset_test_data.py --yes
```

`reset_test_data.py --yes` permanently deletes all datapoints/annotations/assignments/uploaded images (keeps annotator identities). Only for starting a fresh annotation round.

## Migrate old negation-question format

Version 2 asks annotators only positive predicates, stores negation as `expected: false`.

```bash
.venv/bin/python t2i-annotation/src/migrate_positive_condition_questions.py           # audit
.venv/bin/python t2i-annotation/src/migrate_positive_condition_questions.py --apply
```

Inverts the historical boolean for negative conditions so its meaning is preserved (old "Yes, not wooden" → new "No, it is wooden"). Version-marked, safe to rerun.

```bash
.venv/bin/python t2i-annotation/src/merge_annotation_exports.py
```

Merges `samples/annotations` + its pre-migration backup + the current upload manifest into deduplicated version-2 JSONL at `samples/annotations_merged/`. Duplicate `annotationId`s count once; v2 copies preferred.

## Shared logic

`datapoint_fields.py` builds `objects`/`conditions` from GenEval metadata; imported by the upload script above, not run directly.
