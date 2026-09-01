"""Retire surplus, low-annotation prompt groups from an over-represented
pos*_neg* setting, to bring the corpus toward a target group count per
setting without discarding annotator work on the datapoints closest to
completion.

A prompt group ((setting, category, idx) bundle of up to 8 model images) is
"fully annotated" once at least `--full-min-models` of its models have
reached `--full-min-annotations` completed annotations each -- these are
never retired even if the setting is still over target, since they
represent completed human-labeling effort worth keeping. Among the
remaining groups, the ones with the fewest total annotations across all
their models are retired first, so whatever work is set aside is the least
possible.

Nothing is deleted outright. Retiring a group *moves* each of its
datapoints, their annotations, and their assignments:

- In Firestore: copied into `deprecated_datapoints`/`deprecated_annotations`/
  `deprecated_assignments` collections (same document IDs), then removed
  from the live `datapoints`/`annotations`/`assignments` collections so they
  stop counting toward setting quotas and eligible-datapoint queries.
- In Storage: the generated image is copied to the `deprecated-annotation-
  images/` prefix in the same bucket, then removed from the live
  `annotation-images/` prefix.
- Locally: the same three Firestore payloads are appended (one JSON object
  per line) to datapoints.jsonl/annotations.jsonl/assignments.jsonl under
  --deprecated-dir (default /home/mbravo/data/t2i-generation/deprecated_annotations).

Re-run download_annotations.py and select_and_upload_datapoints.py's
manifest refresh afterward to bring the local snapshots back in sync with
the now-smaller live Firestore collections.

Dry-run is the default; pass --apply to actually move anything.

Usage:
    .venv/bin/python t2i-annotation/src/retire_prompt_groups.py \\
        --setting pos1_neg0 --target-count 9
    .venv/bin/python t2i-annotation/src/retire_prompt_groups.py \\
        --setting pos1_neg0 --target-count 9 --apply
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
STORAGE_PREFIX = "annotation-images"
DEPRECATED_STORAGE_PREFIX = "deprecated-annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
DEFAULT_DEPRECATED_DIR = REPO_ROOT / "deprecated_annotations"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--setting", required=True, help="pos<P>_neg<N> setting to shrink")
    parser.add_argument("--target-count", type=int, required=True,
                         help="Desired number of prompt groups left in this setting")
    parser.add_argument("--full-min-models", type=int, default=5,
                         help="A group counts as fully annotated once this many of its "
                              "models reach --full-min-annotations (default 6)")
    parser.add_argument("--full-min-annotations", type=int, default=3,
                         help="Annotations a single model-datapoint needs to count as "
                              "done (default 3, matches targetAnnotationsPerDatapoint)")
    parser.add_argument("--deprecated-dir", type=Path, default=DEFAULT_DEPRECATED_DIR,
                         help="Local directory to append moved records to")
    parser.add_argument("--apply", action="store_true",
                         help="Actually move data; default is dry-run")
    return parser.parse_args()


def group_key(datapoint):
    """(setting, category, idx) for a datapoint, or None if sourcePath is non-standard."""
    source_path = datapoint.get("sourcePath", "")
    parts = Path(source_path).parts
    if len(parts) < 6 or parts[-2] != "samples":
        return None
    return (datapoint["posNeg"], datapoint["category"], parts[-3])


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main():
    args = parse_args()

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    datapoint_docs = list(
        db.collection("datapoints")
        .where(filter=firestore.FieldFilter("posNeg", "==", args.setting))
        .stream()
    )
    if not datapoint_docs:
        print(f"No datapoints found for setting {args.setting!r}.")
        return

    groups = defaultdict(dict)  # (setting, category, idx) -> {model: doc_id}
    datapoint_data_by_id = {}
    for doc in datapoint_docs:
        data = doc.to_dict() or {}
        key = group_key(data)
        if key is None:
            print(f"Skipping datapoint with unexpected sourcePath: {doc.id}")
            continue
        groups[key][data.get("model")] = doc.id
        datapoint_data_by_id[doc.id] = data

    ann_count_by_datapoint = defaultdict(int)
    for ann_doc in db.collection("annotations").stream():
        datapoint_id = (ann_doc.to_dict() or {}).get("datapointId")
        if datapoint_id in datapoint_data_by_id:
            ann_count_by_datapoint[datapoint_id] += 1

    rows = []
    for key, model_docs in groups.items():
        counts = {model: ann_count_by_datapoint.get(doc_id, 0) for model, doc_id in model_docs.items()}
        n_full_models = sum(1 for c in counts.values() if c >= args.full_min_annotations)
        rows.append({
            "key": key,
            "model_docs": model_docs,
            "counts": counts,
            "total_ann": sum(counts.values()),
            "fully_annotated": n_full_models >= args.full_min_models,
        })

    current_count = len(rows)
    excess = current_count - args.target_count
    print(f"{args.setting}: {current_count} prompt groups, target {args.target_count} "
          f"({'no change needed' if excess <= 0 else f'need to retire {excess}'}).")
    if excess <= 0:
        return

    removable = sorted(
        (row for row in rows if not row["fully_annotated"]),
        key=lambda row: (row["total_ann"], row["key"]),
    )
    fully_annotated_count = current_count - len(removable)
    if fully_annotated_count > args.target_count:
        print(f"Note: {fully_annotated_count} groups are already fully annotated "
              f"(>= {args.full_min_models}/8 models with >= {args.full_min_annotations} "
              "annotations each). Fully annotated groups are never retired, so the "
              f"final count will stay at {fully_annotated_count}, above the requested "
              f"target of {args.target_count}.")

    if len(removable) < excess:
        print(f"Warning: only {len(removable)} non-fully-annotated groups are available "
              f"to retire; retiring all of them leaves {current_count - len(removable)} "
              f"groups instead of the requested {args.target_count}.")
    to_retire = removable[:excess]

    total_ann_moved = sum(row["total_ann"] for row in to_retire)
    print(f"\nRetiring {len(to_retire)} groups ({total_ann_moved} annotation rows) into "
          f"deprecated_* Firestore collections and {args.deprecated_dir}:")
    for row in to_retire:
        _setting, category, idx = row["key"]
        n_full = sum(1 for c in row["counts"].values() if c >= args.full_min_annotations)
        print(f"  {category}/{idx}: total_ann={row['total_ann']} "
              f"models_at_full={n_full}/{len(row['counts'])}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to actually move this data.")
        return

    datapoints_path = args.deprecated_dir / "datapoints.jsonl"
    annotations_path = args.deprecated_dir / "annotations.jsonl"
    assignments_path = args.deprecated_dir / "assignments.jsonl"

    for row in to_retire:
        for _model, doc_id in row["model_docs"].items():
            datapoint_data = datapoint_data_by_id[doc_id]
            annotation_docs = list(
                db.collection("annotations")
                .where(filter=firestore.FieldFilter("datapointId", "==", doc_id))
                .stream()
            )
            assignment_docs = list(
                db.collection("assignments")
                .where(filter=firestore.FieldFilter("datapointId", "==", doc_id))
                .stream()
            )

            batch = db.batch()
            batch.set(db.collection("deprecated_datapoints").document(doc_id), datapoint_data)
            for ann_doc in annotation_docs:
                batch.set(db.collection("deprecated_annotations").document(ann_doc.id), ann_doc.to_dict() or {})
            for assignment_doc in assignment_docs:
                batch.set(db.collection("deprecated_assignments").document(assignment_doc.id), assignment_doc.to_dict() or {})
            batch.delete(db.collection("datapoints").document(doc_id))
            for ann_doc in annotation_docs:
                batch.delete(ann_doc.reference)
            for assignment_doc in assignment_docs:
                batch.delete(assignment_doc.reference)
            batch.commit()

            append_jsonl(datapoints_path, {"docId": doc_id, **datapoint_data})
            for ann_doc in annotation_docs:
                append_jsonl(annotations_path, {"annotationId": ann_doc.id, **(ann_doc.to_dict() or {})})
            for assignment_doc in assignment_docs:
                append_jsonl(assignments_path, {"assignmentId": assignment_doc.id, **(assignment_doc.to_dict() or {})})

            source_blob = bucket.blob(f"{STORAGE_PREFIX}/{doc_id}.png")
            try:
                if source_blob.exists():
                    bucket.copy_blob(source_blob, bucket, f"{DEPRECATED_STORAGE_PREFIX}/{doc_id}.png")
                    source_blob.delete()
            except Exception as exc:  # noqa: BLE001 - best-effort image move
                print(f"  Warning: could not move storage image for {doc_id}: {exc}")

    print(f"\nRetired {len(to_retire)} groups. Firestore copies live under deprecated_* "
          f"collections; local copies appended under {args.deprecated_dir}.")
    print("Re-run download_annotations.py and select_and_upload_datapoints.py's "
          "manifest refresh to sync local snapshots.")


if __name__ == "__main__":
    main()
