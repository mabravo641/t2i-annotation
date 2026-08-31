"""Download every annotation datapoint image from Firebase Storage.

Images are written to ``t2i-annotation/samples/images/`` by default, one file
per Firestore datapoint ID. A JSONL manifest in the same directory records the
Firebase Storage path and local filename, making the backup portable without
depending on the original ``geneval_data`` tree.

Existing files are skipped by default so interrupted downloads can be resumed.
Use ``--overwrite`` to download every image again.

Usage:

    .venv/bin/python t2i-annotation/src/download_firebase_images.py
    .venv/bin/python t2i-annotation/src/download_firebase_images.py --overwrite
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
STORAGE_PREFIX = "annotation-images"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Download images even when their local files already exist.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    firebase_admin.initialize_app(
        credentials.Certificate(SERVICE_ACCOUNT_FILE),
        {"storageBucket": STORAGE_BUCKET},
    )
    db = firestore.client()
    bucket = storage.bucket()

    datapoints = sorted(db.collection("datapoints").stream(), key=lambda doc: doc.id)
    manifest_rows = []
    downloaded = 0
    skipped = 0
    missing = []

    for doc in datapoints:
        data = doc.to_dict() or {}
        storage_path = data.get("storagePath") or f"{STORAGE_PREFIX}/{doc.id}.png"
        suffix = Path(storage_path).suffix or ".png"
        local_path = args.out_dir / f"{doc.id}{suffix}"
        blob = bucket.blob(storage_path)

        if not blob.exists():
            missing.append((doc.id, storage_path))
            continue

        if local_path.exists() and not args.overwrite:
            skipped += 1
        else:
            temporary_path = local_path.with_suffix(local_path.suffix + ".part")
            blob.download_to_filename(temporary_path)
            temporary_path.replace(local_path)
            downloaded += 1

        manifest_rows.append(
            {
                "datapointId": doc.id,
                "storagePath": storage_path,
                "localPath": local_path.relative_to(args.out_dir.parent).as_posix(),
                "model": data.get("model"),
                "category": data.get("category"),
                "posNeg": data.get("posNeg"),
                "promptGroupId": data.get("promptGroupId"),
                "sourcePath": data.get("sourcePath"),
            }
        )

    manifest_path = args.out_dir / "images_manifest.jsonl"
    temporary_manifest = manifest_path.with_suffix(".jsonl.part")
    with open(temporary_manifest, "w") as output:
        for row in manifest_rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary_manifest.replace(manifest_path)

    print(f"Firebase datapoints: {len(datapoints)}")
    print(f"Images downloaded: {downloaded}")
    print(f"Existing images skipped: {skipped}")
    print(f"Missing Storage images: {len(missing)}")
    for datapoint_id, storage_path in missing:
        print(f"  MISSING: {datapoint_id} -> {storage_path}")
    print(f"Wrote {manifest_path} ({len(manifest_rows)} entries)")

    if missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
