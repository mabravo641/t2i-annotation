"""Pull datapoints whose generated images are broken out of the live annotation corpus.

Reads the generation-failure audit written by
`neggeneval/metric_evaluation/check_generation_failures.py`
(default `geneval_data/wrong_prompts.csv`), finds the live `datapoints` whose
images come from one of those failed (model, setting, tag, prompt) groups, and
moves them -- together with any annotations and assignments already collected on
them -- out of the live collections so annotators stop being served images that
cannot be judged.

Nothing is destroyed. Exactly like retire_prompt_groups.py, each affected record is
copied before it is deleted:

- Firestore: into `deprecated_datapoints`/`deprecated_annotations`/
  `deprecated_assignments` (same document IDs), then deleted from
  `datapoints`/`annotations`/`assignments`.
- Storage: the image is copied to the `deprecated-annotation-images/` prefix,
  then deleted from `annotation-images/`.
- Locally: appended as JSON lines to datapoints.jsonl / annotations.jsonl /
  assignments.jsonl under --backup-dir, plus a `removed_manifest.csv` recording
  which audit row caused each removal and how many annotations were set aside,
  plus a copy of each removed PNG under `<backup-dir>/images/<doc_id>.png`.
  That image copy matters: `doc_id` is derived from the image path, so a
  regenerated image is re-uploaded under the *same* id, and without the copy
  there is no way to tell which picture an archived annotation actually judged.
  The local copies are the recovery path if a prompt turns out to be fine after
  the generation bug is fixed.

What belongs out of the corpus, and what does not
-------------------------------------------------
Remove a datapoint only when the annotation would measure nothing about this model
on this prompt:

  * the images belong to a DIFFERENT prompt -- a corrupted generation shard or a
    wrong-folder mix-up. The human judgement is meaningless for the recorded prompt.
  * the images are degenerate -- washed-out haze, blank, corrupt. Nothing to judge.

Do NOT remove a datapoint because the model rendered the prompt badly: a missing
object, a street lamp where a traffic light was asked for, an orange "non-orange"
skis. Those are TRUE NEGATIVES and they are the whole point of the benchmark.
Deleting them strips each model's failures out of the corpus and biases every score
upward -- the opposite of what the human study is for.

The audit cannot make this distinction on its own. `off_prompt_generation` covers
both cases, so a low-coverage row needs eyes on the image before it is retired; use
neggeneval/notebooks/annotation_generation_triage.ipynb for that.

Which audit rows count
----------------------
The audit grades every failed prompt `high`/`medium`/`low`:

  high    blank, white, washed-out, corrupt or missing images -- the image itself
          is unusable, no judgement call needed.
  medium  the detector found too few of the prompt's objects while other models
          reached coverage. Mostly real, but it also catches detector class
          confusion: "a photo of a purple bear" rendered as a purple *teddy* bear is
          a perfectly good image that RF-DETR labels `teddy bear`, not `bear`.
          The audit grades this by object *coverage*, not mere presence -- an earlier
          "at least one object present" rule silently passed images that rendered one
          of five requested objects, e.g. flux2/pos2_neg2/neg_rel_depth/00043, whose
          samples show skis and skateboards for a prompt asking for an umbrella, a
          suitcase, a toaster and a baseball glove. Use --max-object-coverage to
          target those.
  low     no model gets a detection on this prompt -- almost always a detector
          blind spot (`hair drier`, `skis`, `book`), with fine images.

So confidence alone is not enough, and the default selection is deliberately
narrow: a row is removed only if it is `high` confidence, **or** its four images
between them yielded no detection at all (`detected_objects` empty), which means
nothing recognisable was rendered. That keeps genuinely broken images out of the
corpus without throwing away annotator work on images that are only awkward for
the detector -- exactly the disagreement cases the human study exists to measure.

Widen or narrow it with --min-confidence / --reasons / --no-empty-detections, or
audit what a filter would touch with --list-only.

Dry-run is the default; pass --apply to actually move anything.

Usage:
    .venv/bin/python t2i-annotation/src/remove_wrong_prompt_datapoints.py
    .venv/bin/python t2i-annotation/src/remove_wrong_prompt_datapoints.py --apply

    # everything the audit flagged at medium or better, including detector confusion
    .venv/bin/python t2i-annotation/src/remove_wrong_prompt_datapoints.py \\
        --min-confidence medium --apply

    # just see what is live for a given failure mode, change nothing
    .venv/bin/python t2i-annotation/src/remove_wrong_prompt_datapoints.py \\
        --reasons washed_out blank_white --list-only
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
DEFAULT_AUDIT_CSV = REPO_ROOT / "geneval_data" / "wrong_prompts.csv"
DEFAULT_BACKUP_DIR = REPO_ROOT / "removed_wrong_prompts"
GENEVAL_ROOT = REPO_ROOT / "geneval_data"
STORAGE_PREFIX = "annotation-images"
DEPRECATED_STORAGE_PREFIX = "deprecated-annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT_CSV,
                        help=f"failure audit to read (default {DEFAULT_AUDIT_CSV})")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR,
                        help=f"local directory for the recovery copies "
                             f"(default {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="high",
                        help="lowest audit confidence to act on (default high); rows with no "
                             "detections at all are included regardless, see "
                             "--no-empty-detections")
    parser.add_argument("--no-empty-detections", action="store_true",
                        help="do NOT auto-include rows whose four images yielded no detection "
                             "at all; go strictly by --min-confidence")
    parser.add_argument("--max-object-coverage", type=float, default=None,
                        help="also select rows whose object_coverage is below this (e.g. 0.25 "
                             "= fewer than a quarter of the prompt's objects rendered). Eyeball "
                             "these before applying: a purple teddy bear scores 0.00 on "
                             "'a photo of a purple bear' and is a perfectly good image.")
    parser.add_argument("--shards", nargs="*", default=None, metavar="MODEL/SETTING/TAG",
                        help="retire every live datapoint under these generation directories "
                             "(e.g. flux2/pos2_neg2/neg_rel_depth), whether or not the audit "
                             "flagged its prompt. Use when a whole shard is untrustworthy -- "
                             "a directory whose failures pack into one contiguous index block "
                             "was generated against a misaligned prompt list, so even the "
                             "prompts that pass the coverage check are suspect.")
    parser.add_argument("--only-datapoints", nargs="*", default=None,
                        help="restrict to these exact datapoint document IDs, for removing a "
                             "specific image you have already inspected")
    parser.add_argument("--reasons", nargs="*", default=None,
                        help="only act on rows whose failure_reason mentions one of these "
                             "labels (e.g. washed_out off_prompt_generation)")
    parser.add_argument("--models", nargs="*", default=None, help="restrict to these models")
    parser.add_argument("--settings", nargs="*", default=None, help="restrict to these settings")
    parser.add_argument("--no-image-copies", action="store_true",
                        help="skip copying the removed PNGs into <backup-dir>/images/. By "
                             "default they are copied, because a regenerated image reuses the "
                             "same doc_id (it is derived from the path) -- without a copy there "
                             "is no way to tell which picture an old annotation actually judged")
    parser.add_argument("--list-only", action="store_true",
                        help="report what matches and exit without writing backups or asking "
                             "to apply")
    parser.add_argument("--apply", action="store_true",
                        help="actually move the data; default is dry-run")
    return parser.parse_args()


# ------------------------------------------------------------------ audit rows


def row_selected(row, args) -> tuple[bool, str]:
    """Should this audit row be acted on? Returns (selected, why-or-why-not)."""
    if args.models and row["model"] not in args.models:
        return False, "model not requested"
    if args.settings and row["setting"] not in args.settings:
        return False, "setting not requested"

    labels = {part.split("x")[0] for part in row["failure_reason"].split(";") if part}
    if args.reasons and not (labels & set(args.reasons)):
        return False, "reason not requested"

    confidence = row.get("confidence", "low")
    if CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK[args.min_confidence]:
        return True, f"{confidence} confidence ({row['failure_reason']})"
    if not args.no_empty_detections and not row.get("detected_objects", "").strip():
        return True, f"nothing detected in any sample ({row['failure_reason']})"
    if args.max_object_coverage is not None and row.get("object_coverage", ""):
        coverage = float(row["object_coverage"])
        if coverage < args.max_object_coverage:
            return True, (f"only {coverage:.0%} of the prompt's objects rendered "
                          f"({row['failure_reason']})")
    return False, f"{confidence} confidence, below --min-confidence"


def load_audit(args):
    """(model, setting, tag, idx) -> (row, reason) for every selected audit row."""
    if not args.audit_csv.is_file():
        raise SystemExit(f"audit CSV not found: {args.audit_csv}")
    selected, skipped = {}, Counter()
    with open(args.audit_csv, newline="") as f:
        for row in csv.DictReader(f):
            keep, why = row_selected(row, args)
            key = (row["model"], row["setting"], row["tag"], row["prompt_index"])
            if keep:
                selected[key] = (row, why)
            else:
                skipped[why] += 1
    return selected, skipped


# ---------------------------------------------------------------------- helpers


def source_key(data):
    """(model, setting, tag, idx) for a datapoint, or None if sourcePath is non-standard.

    `sourcePath` is the corpus identity -- the Firestore `category` field holds the
    semantic value ("material") while the dataset uses the directory name
    ("neg_attr_material"), so matching on `category` would silently miss rows.
    """
    parts = Path(data.get("sourcePath", "")).parts
    if len(parts) < 6 or parts[-2] != "samples":
        return None
    return (parts[0], parts[1], parts[2], parts[-3])


def shard_row_stub(key, data):
    """Manifest fields for a datapoint the audit never flagged, retired by shard."""
    model, setting, tag, idx = key
    return {
        "model": model, "setting": setting, "tag": tag, "prompt_index": idx,
        "prompt": data.get("prompt", ""), "failure_reason": "shard_retired",
        "confidence": "n/a", "required_objects": "|".join(data.get("objects") or []),
        "detected_objects": "", "object_coverage": "",
    }


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main():
    args = parse_args()

    shards = {tuple(s.strip("/").split("/")) for s in (args.shards or [])}
    for shard in shards:
        if len(shard) != 3:
            raise SystemExit(f"--shards wants MODEL/SETTING/TAG, got {'/'.join(shard)!r}")

    selected, skipped = load_audit(args)
    print(f"audit          : {args.audit_csv}")
    print(f"selected rows  : {len(selected)} failed prompt groups")
    if skipped:
        print("not selected   : " + ", ".join(f"{n} {why}" for why, n in skipped.most_common()))
    if shards:
        print("shards         : " + ", ".join("/".join(s) for s in sorted(shards))
              + "  (all datapoints under these, audit ignored)")
    # An explicit target list REPLACES the audit-driven selection rather than adding to
    # it. Passing --shards used to leave --min-confidence active underneath, so a shard
    # retirement also swept up unrelated high-confidence rows -- which is how a chmod on
    # 30 unreadable directories once turned into an auto-removal.
    if shards or args.only_datapoints:
        if selected:
            print(f"note           : ignoring {len(selected)} audit-selected prompt groups -- "
                  "--shards/--only-datapoints name the targets explicitly")
        selected = {}
    if not selected and not shards and not args.only_datapoints:
        print("\nNothing selected by the current filters; nothing to do.")
        return

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    # live datapoints whose image belongs to one of the selected prompt groups
    targets = []
    n_live = 0
    for doc in db.collection("datapoints").stream():
        n_live += 1
        data = doc.to_dict() or {}
        key = source_key(data)
        if key is None:
            print(f"  note: datapoint {doc.id} has a non-standard sourcePath, skipping")
            continue
        if args.only_datapoints and doc.id not in args.only_datapoints:
            continue
        if key[:3] in shards:
            targets.append({
                "doc_id": doc.id, "data": data, "key": key,
                "row": shard_row_stub(key, data),
                "why": f"whole shard {'/'.join(key[:3])} retired",
            })
        elif args.only_datapoints:
            targets.append({
                "doc_id": doc.id, "data": data, "key": key,
                "row": shard_row_stub(key, data),
                "why": "named explicitly with --only-datapoints",
            })
        elif key in selected:
            row, why = selected[key]
            targets.append({"doc_id": doc.id, "data": data, "key": key, "row": row, "why": why})

    if args.only_datapoints:
        missing = set(args.only_datapoints) - {t["doc_id"] for t in targets}
        for doc_id in sorted(missing):
            print(f"  note: {doc_id} is not live, or its prompt is not in the selected audit rows")
    print(f"live datapoints: {n_live}")
    print(f"matched        : {len(targets)} datapoints from {len({t['key'] for t in targets})} "
          f"prompt groups")
    if not targets:
        print("\nNone of the failed prompts are in the annotation corpus; nothing to do.")
        return

    # annotations / assignments attached to them -- one pass each, not a query per doc
    target_ids = {t["doc_id"] for t in targets}
    annotations = defaultdict(list)
    assignments = defaultdict(list)
    for doc in db.collection("annotations").stream():
        dp = (doc.to_dict() or {}).get("datapointId")
        if dp in target_ids:
            annotations[dp].append(doc)
    for doc in db.collection("assignments").stream():
        dp = (doc.to_dict() or {}).get("datapointId")
        if dp in target_ids:
            assignments[dp].append(doc)

    n_ann = sum(len(v) for v in annotations.values())
    n_asg = sum(len(v) for v in assignments.values())
    print(f"annotations set aside: {n_ann}   assignments set aside: {n_asg}")

    targets.sort(key=lambda t: t["doc_id"])
    print("\nwould remove:" if not args.apply else "\nremoving:")
    for t in targets:
        n = len(annotations.get(t["doc_id"], []))
        print(f"  {t['doc_id']}")
        print(f"      prompt      : {t['row'].get('prompt', '')[:78]}")
        print(f"      why         : {t['why']}")
        print(f"      annotations : {n}   assignments: {len(assignments.get(t['doc_id'], []))}")

    if args.list_only:
        print("\n--list-only: nothing written.")
        return
    if not args.apply:
        print(f"\nDry run only. Backups would go to {args.backup_dir}.")
        print("Re-run with --apply to move this data.")
        return

    datapoints_path = args.backup_dir / "datapoints.jsonl"
    annotations_path = args.backup_dir / "annotations.jsonl"
    assignments_path = args.backup_dir / "assignments.jsonl"
    manifest_path = args.backup_dir / "removed_manifest.csv"
    manifest_rows = []

    for t in targets:
        doc_id, data = t["doc_id"], t["data"]
        ann_docs = annotations.get(doc_id, [])
        asg_docs = assignments.get(doc_id, [])

        # local backup first: if the Firestore batch fails we still hold the data
        append_jsonl(datapoints_path, {"docId": doc_id, "removalReason": t["why"], **data})
        for doc in ann_docs:
            append_jsonl(annotations_path,
                         {"annotationId": doc.id, "datapointDocId": doc_id,
                          **(doc.to_dict() or {})})
        for doc in asg_docs:
            append_jsonl(assignments_path,
                         {"assignmentId": doc.id, "datapointDocId": doc_id,
                          **(doc.to_dict() or {})})

        # Copy the picture first: once the Firestore rows are gone, a failure here would
        # leave an archived annotation with no record of what it actually judged.
        image_copy = ""
        if not args.no_image_copies:
            source_image = GENEVAL_ROOT / data.get("sourcePath", "")
            try:
                if source_image.is_file():
                    dest = args.backup_dir / "images" / f"{doc_id}.png"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_image, dest)
                    image_copy = str(dest.relative_to(args.backup_dir))
                else:
                    print(f"  Warning: source image not found for {doc_id}: {source_image}")
            except OSError as exc:  # unreadable directory, full disk, vanished mid-copy
                print(f"  Warning: could not copy image for {doc_id}: {exc}")

        batch = db.batch()
        batch.set(db.collection("deprecated_datapoints").document(doc_id), data)
        for doc in ann_docs:
            batch.set(db.collection("deprecated_annotations").document(doc.id),
                      doc.to_dict() or {})
        for doc in asg_docs:
            batch.set(db.collection("deprecated_assignments").document(doc.id),
                      doc.to_dict() or {})
        batch.delete(db.collection("datapoints").document(doc_id))
        for doc in ann_docs:
            batch.delete(doc.reference)
        for doc in asg_docs:
            batch.delete(doc.reference)
        batch.commit()

        source_blob = bucket.blob(f"{STORAGE_PREFIX}/{doc_id}.png")
        image_moved = False
        try:
            if source_blob.exists():
                bucket.copy_blob(source_blob, bucket,
                                 f"{DEPRECATED_STORAGE_PREFIX}/{doc_id}.png")
                source_blob.delete()
                image_moved = True
        except Exception as exc:  # noqa: BLE001 - best-effort image move
            print(f"  Warning: could not move storage image for {doc_id}: {exc}")

        row = t["row"]
        manifest_rows.append({
            "datapoint_id": doc_id,
            "model": row.get("model", ""), "setting": row.get("setting", ""),
            "tag": row.get("tag", ""),
            "prompt_index": row.get("prompt_index", ""), "prompt": row.get("prompt", ""),
            "failure_reason": row.get("failure_reason", ""),
            "confidence": row.get("confidence", ""),
            "removal_reason": t["why"],
            "required_objects": row.get("required_objects", ""),
            "detected_objects": row.get("detected_objects", ""),
            "annotations_saved": len(ann_docs),
            "assignments_saved": len(asg_docs),
            "source_path": data.get("sourcePath", ""),
            "image_moved_to": (f"{DEPRECATED_STORAGE_PREFIX}/{doc_id}.png"
                               if image_moved else ""),
            "local_image_copy": image_copy,
        })
        print(f"  removed datapoints/{doc_id} "
              f"({len(ann_docs)} annotations, {len(asg_docs)} assignments saved)")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not manifest_path.exists()
    with open(manifest_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(manifest_rows)

    print(f"\nRemoved {len(targets)} datapoints "
          f"({n_ann} annotations, {n_asg} assignments preserved).")
    print(f"Recovery copies : {args.backup_dir}/"
          "{datapoints,annotations,assignments}.jsonl")
    if not args.no_image_copies:
        print(f"Image copies    : {args.backup_dir}/images/<doc_id>.png  "
              "(the exact picture each saved annotation judged)")
    print(f"Manifest        : {manifest_path}")
    print("Firestore copies: deprecated_datapoints / deprecated_annotations / "
          "deprecated_assignments")
    print("\nRe-run download_annotations.py and select_and_upload_datapoints.py's manifest "
          "refresh to sync local snapshots.")


if __name__ == "__main__":
    main()
