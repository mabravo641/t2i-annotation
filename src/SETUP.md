# Annotation data scripts

Run everything from the repo root with the shared virtual environment:

```bash
.venv/bin/python t2i-annotation/src/<script>.py
```

All scripts need the Firebase Admin SDK credentials at
`firebase/neg-gen-firebase-adminsdk-fbsvc-caafa15513.json` (outside this repo;
never commit it).

## Upload datapoints for annotation

Preview a balanced batch first, then upload for real. The default paired mode
selects prompt groups balanced across category and pos/neg setting, with one
image for every requested model. Image selection also balances each model's
automatic-evaluator correctness. Existing uploads count as fixed group members;
the script adds only missing models for those prompts and never uploads the same
source image twice.

```bash
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \
  --models flux2 flux2-4bit qwen sd35 --num-prompts 40 --seed 42 --dry-run
.venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \
  --models flux2 flux2-4bit qwen sd35 --num-prompts 40 --seed 42
```

`--num-prompts 40` means 40 matched prompt groups, not 40 images. With four
models that is 160 total datapoints before crediting images already uploaded.
All feasible existing prompt groups are completed first, even when there are
more than the requested number. Use an explicit `--models` list: including a
partially generated/unevaluated model restricts selection to prompts available
for that model (or requires `--include-unknown-correctness`).

Existing prompt groups are fixed inputs to balancing. When `--num-prompts` is
larger than their count, new prompts preferentially fill the least-represented
category and pos/neg setup buckets. Historical excesses cannot be reduced
without removing already uploaded datapoints, so the result is the closest
attainable marginal balance rather than necessarily equal counts.

Use `--unpaired --num-total N` only to reproduce the older behavior where each
image is selected independently. Paired uploads receive a stable
`promptGroupId` (`<setting>-<category>-<idx>`) for later grouped analysis or a
ranking interface.

## Initialize the three-annotator cap

Assignment creation reserves a slot in a Firestore transaction, enforcing the
`targetAnnotationsPerDatapoint` cap even when annotators request work at the
same time. Before deploying that website code, initialize existing datapoints:

```bash
.venv/bin/python t2i-annotation/src/backfill_assignment_slots.py
.venv/bin/python t2i-annotation/src/backfill_assignment_slots.py --apply
```

The first command is an audit-only dry run. Review any `OVER TARGET` or orphan
warnings before applying. Assigned slots remain reserved if an annotator walks
away; this preserves the strict cap but may require an administrator to cancel
stale assignments and decrement the corresponding counter in a future cleanup
workflow. Newly uploaded datapoints start with zero reserved slots.

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

## Verify or reset annotation data

Audit Firestore fields and source-image integrity (the first form is faster):

```bash
.venv/bin/python t2i-annotation/src/verify_datapoints.py --skip-image-check
.venv/bin/python t2i-annotation/src/verify_datapoints.py
```

Preview a test-data reset without changing Firebase:

```bash
.venv/bin/python t2i-annotation/src/reset_test_data.py --dry-run
```

`reset_test_data.py --yes` permanently deletes all datapoints, annotations,
assignments, and uploaded annotation images. It deliberately preserves
annotator identities. Use it only when starting a new annotation round.

## Shared logic

`datapoint_fields.py` builds the `objects`/`conditions` fields from GenEval
metadata and is imported by the upload script above — it isn't run directly.

## Older scripts

`graveyard/` holds superseded or one-off scripts (the original flux2-only
importer, the hand-written demo datapoint, and the structured-fields
migration) kept for reference. Not part of the normal workflow.
