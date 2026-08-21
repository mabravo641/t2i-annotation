# Annotation data scripts

Run everything from the repo root with the shared virtual environment:

```bash
.venv/bin/python t2i-annotation/src/<script>.py
```

All scripts need the Firebase Admin SDK credentials at
`firebase/neg-gen-firebase-adminsdk-fbsvc-caafa15513.json` (outside this repo;
never commit it).

## Upload datapoints for annotation

Preview a balanced batch first, then upload for real. Balances across model,
category, pos/neg setting, and automatic-evaluator correctness; skips anything
already uploaded, so it's safe to re-run as more evaluation results land.

```bash
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py --num-total 40 --dry-run
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py --num-total 40
```

See the top of `select_and_upload_datapoints.py` for all options
(`--models`, `--categories`, `--settings`, `--balance-by`, ...). Every upload
is logged to `manifests/upload_manifest.csv`.

## Download submitted annotations

```bash
.venv/bin/python t2i-annotation/src/download_annotations.py
```

Writes `annotations/annotations.jsonl` (full fidelity) and
`annotations/annotations.csv` (one row per response), joined with each
datapoint's prompt/model/category and each annotator's nickname/email.
Overwrites on every run to reflect the current state of Firestore.

## Shared logic

`datapoint_fields.py` builds the `objects`/`conditions` fields from GenEval
metadata and is imported by the upload script above — it isn't run directly.

## Older scripts

`graveyard/` holds superseded or one-off scripts (the original flux2-only
importer, the hand-written demo datapoint, and the structured-fields
migration) kept for reference. Not part of the normal workflow.
