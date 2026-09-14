"""Put datapoints retired by remove_wrong_prompt_datapoints.py back into the live corpus.

The counterpart to the removal script. Retirement is a move, not a delete -- the
datapoint, its annotations and its assignments are copied into the `deprecated_*`
Firestore collections and the image into the `deprecated-annotation-images/` storage
prefix -- so restoring is the same move in reverse.

Use it when a datapoint turns out to have been retired in error, or when a prompt
becomes usable again after its generation is fixed.

For each requested document ID this:

- copies `deprecated_datapoints/<id>` back to `datapoints/<id>`, along with every
  `deprecated_annotations` / `deprecated_assignments` row that references it, then
  deletes the deprecated copies;
- moves `deprecated-annotation-images/<id>.png` back to `annotation-images/<id>.png`
  if it is there (a removal that failed midway may never have moved it);
- rewrites the local `--backup-dir` JSONL files with those records dropped, keeping
  a `.bak` of each, and records the restore in `restored_manifest.csv`.

The local PNG under `<backup-dir>/images/` is deliberately left in place: it is the
only record of which picture an archived annotation judged, and it costs nothing.

Dry-run is the default; pass --apply to actually restore.

Usage:
    .venv/bin/python t2i-annotation/src/restore_removed_datapoints.py \\
        --datapoints negationclip-pos1_neg0-neg_attr_color-00025-samples-00002
    .venv/bin/python t2i-annotation/src/restore_removed_datapoints.py \\
        --datapoints <id> --apply
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
DEFAULT_BACKUP_DIR = REPO_ROOT / "removed_wrong_prompts"
STORAGE_PREFIX = "annotation-images"
DEPRECATED_STORAGE_PREFIX = "deprecated-annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--datapoints", nargs="+", required=True,
                        help="datapoint document IDs to restore")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR,
                        help=f"local backup directory to prune (default {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--reason", default="",
                        help="why this is being restored; recorded in restored_manifest.csv")
    parser.add_argument("--apply", action="store_true",
                        help="actually restore; default is dry-run")
    return parser.parse_args()


def prune_jsonl(path: Path, key_field: str, doc_ids: set, apply: bool) -> int:
    """Drop records for the restored datapoints, keeping a .bak of the original."""
    if not path.is_file():
        return 0
    kept, dropped = [], 0
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if rec.get(key_field) in doc_ids:
                dropped += 1
            else:
                kept.append(line)
    if dropped and apply:
        path.replace(path.with_suffix(path.suffix + ".bak"))
        path.write_text("".join(l + "\n" for l in kept))
    return dropped


def main():
    args = parse_args()
    doc_ids = list(dict.fromkeys(args.datapoints))

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    plans = []
    for doc_id in doc_ids:
        snap = db.collection("deprecated_datapoints").document(doc_id).get()
        if not snap.exists:
            print(f"  skip {doc_id}: not in deprecated_datapoints")
            continue
        if db.collection("datapoints").document(doc_id).get().exists:
            print(f"  skip {doc_id}: already live")
            continue
        anns = list(db.collection("deprecated_annotations")
                    .where(filter=firestore.FieldFilter("datapointId", "==", doc_id)).stream())
        asgs = list(db.collection("deprecated_assignments")
                    .where(filter=firestore.FieldFilter("datapointId", "==", doc_id)).stream())
        plans.append({"doc_id": doc_id, "data": snap.to_dict() or {},
                      "annotations": anns, "assignments": asgs})

    if not plans:
        print("Nothing to restore.")
        return

    print(f"\nrestoring {len(plans)} datapoints "
          f"({sum(len(p['annotations']) for p in plans)} annotations, "
          f"{sum(len(p['assignments']) for p in plans)} assignments):")
    for p in plans:
        print(f"  {p['doc_id']}")
        print(f"      prompt      : {p['data'].get('prompt', '')[:74]}")
        print(f"      annotations : {len(p['annotations'])}   "
              f"assignments: {len(p['assignments'])}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to restore.")
        return

    manifest_rows = []
    for p in plans:
        doc_id = p["doc_id"]
        batch = db.batch()
        batch.set(db.collection("datapoints").document(doc_id), p["data"])
        for doc in p["annotations"]:
            batch.set(db.collection("annotations").document(doc.id), doc.to_dict() or {})
        for doc in p["assignments"]:
            batch.set(db.collection("assignments").document(doc.id), doc.to_dict() or {})
        batch.delete(db.collection("deprecated_datapoints").document(doc_id))
        for doc in p["annotations"]:
            batch.delete(doc.reference)
        for doc in p["assignments"]:
            batch.delete(doc.reference)
        batch.commit()

        # the image may never have left annotation-images/ if the removal failed midway
        live_blob = bucket.blob(f"{STORAGE_PREFIX}/{doc_id}.png")
        dep_blob = bucket.blob(f"{DEPRECATED_STORAGE_PREFIX}/{doc_id}.png")
        image_state = "already in place"
        try:
            if not live_blob.exists() and dep_blob.exists():
                bucket.copy_blob(dep_blob, bucket, f"{STORAGE_PREFIX}/{doc_id}.png")
                dep_blob.delete()
                image_state = "moved back"
            elif dep_blob.exists():
                dep_blob.delete()
                image_state = "duplicate deprecated copy deleted"
        except Exception as exc:  # noqa: BLE001 - report, do not abort the restore
            image_state = f"storage move failed: {exc}"
            print(f"  Warning: {doc_id}: {image_state}")

        manifest_rows.append({
            "datapoint_id": doc_id,
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "annotations_restored": len(p["annotations"]),
            "assignments_restored": len(p["assignments"]),
            "source_path": p["data"].get("sourcePath", ""),
            "image_state": image_state,
            "reason": args.reason,
        })
        print(f"  restored datapoints/{doc_id} "
              f"({len(p['annotations'])} annotations, {len(p['assignments'])} assignments; "
              f"image {image_state})")

    restored = {p["doc_id"] for p in plans}
    n_dp = prune_jsonl(args.backup_dir / "datapoints.jsonl", "docId", restored, True)
    n_an = prune_jsonl(args.backup_dir / "annotations.jsonl", "datapointDocId", restored, True)
    n_as = prune_jsonl(args.backup_dir / "assignments.jsonl", "datapointDocId", restored, True)
    print(f"\npruned local backup: {n_dp} datapoint, {n_an} annotation, {n_as} assignment "
          f"records (.bak kept)")

    manifest = args.backup_dir / "restored_manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    write_header = not manifest.exists()
    with manifest.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(manifest_rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(manifest_rows)
    print(f"recorded in {manifest}")
    print(f"\nLocal PNG copies under {args.backup_dir}/images/ were left in place.")


if __name__ == "__main__":
    main()
