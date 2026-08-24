"""Wipe test-phase datapoints/annotations/assignments and their Storage images.

Deletes every document in the `datapoints`, `annotations`, and `assignments`
Firestore collections, plus every blob under the `annotation-images/` prefix
in Storage. Deliberately leaves the `annotators` collection untouched, so
existing nicknames/emails carry over into the real annotation round.

This is destructive and cannot be undone from within this script. Always
run --dry-run first.

Usage:

    .venv/bin/python t2i-annotation/src/reset_test_data.py --dry-run
    .venv/bin/python t2i-annotation/src/reset_test_data.py --yes
"""
import argparse
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE_PREFIX = "annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
COLLECTIONS_TO_WIPE = ["datapoints", "annotations", "assignments"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be deleted without deleting anything")
    parser.add_argument("--yes", action="store_true",
                         help="Actually perform the deletion (required unless --dry-run)")
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        raise SystemExit("Refusing to delete without --yes (or use --dry-run to preview).")

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    for collection_name in COLLECTIONS_TO_WIPE:
        docs = list(db.collection(collection_name).stream())
        print(f"{collection_name}: {len(docs)} document(s)"
              f"{' would be deleted' if args.dry_run else ' deleting...'}")
        if not args.dry_run:
            for doc in docs:
                doc.reference.delete()

    blobs = list(bucket.list_blobs(prefix=f"{STORAGE_PREFIX}/"))
    print(f"Storage blobs under {STORAGE_PREFIX}/: {len(blobs)}"
          f"{' would be deleted' if args.dry_run else ' deleting...'}")
    if not args.dry_run:
        for blob in blobs:
            blob.delete()

    annotator_count = len(list(db.collection("annotators").stream()))
    print(f"\nannotators: {annotator_count} document(s) left untouched.")

    if args.dry_run:
        print("\n--dry-run set: nothing was actually deleted.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
