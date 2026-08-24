"""Audit every uploaded datapoint for prompt/image/question integrity.

Checks each Firestore `datapoints` document (that has a `sourcePath`, i.e. was
generated from omar_data metadata) against the source of truth on disk:

- IMAGE MISMATCH: the bytes actually stored in Firebase Storage differ from
  the source file at `sourcePath` in omar_data (would indicate an upload bug
  or an overwritten blob). Skipped with --skip-image-check for a faster run,
  since it downloads every image.
- STALE FIELDS: the stored `prompt`/`objects`/`conditions` no longer match
  what `datapoint_fields.build_objects`/`build_conditions` produce fresh from
  the source metadata.jsonl (would indicate a code change that should have
  been backfilled, à la backfill_condition_structure.py).
- MISSING SOURCE: sourcePath no longer resolves to a file in omar_data.

Also flags, for a human to look at (not a data-integrity bug, but worth eyes on):

- TOTAL MISMATCH: the automatic evaluator found ZERO of the EVERY required
  object for that image (all of them, not just one or two). A single
  undetected object among several is a normal detector limitation and is not
  flagged - only when the image plausibly contains none of the prompt's
  objects at all does this fire. In the two confirmed real examples so far,
  the image showed a completely different, coherent scene (unrelated to the
  prompt) repeated with variation across all 4 samples of that prompt index -
  consistent with either a severe model failure on long/complex prompts or an
  upstream indexing issue in the omar_data generation pipeline (outside this
  repo). Looked up from `autoCorrect`/`autoReason` if the datapoint has them,
  otherwise read live from the model's results/<category>.jsonl.

Usage:

    .venv/bin/python t2i-annotation/src/verify_datapoints.py
    .venv/bin/python t2i-annotation/src/verify_datapoints.py --skip-image-check
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

from datapoint_fields import build_conditions, build_objects
from select_and_upload_datapoints import load_results

REPO_ROOT = Path(__file__).resolve().parents[2]
OMAR_ROOT = REPO_ROOT / "omar_data"
STORAGE_PREFIX = "annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_metadata(local_image_path):
    metadata_path = local_image_path.parent.parent / "metadata.jsonl"
    if not metadata_path.is_file():
        return None
    with open(metadata_path) as f:
        line = f.readline().strip()
    return json.loads(line) if line else None


ZERO_COUNT_LINE = re.compile(r"^expected .*found 0$")


def check_total_mismatch(doc_id, data, source_path):
    """True only when EVERY required object was undetected (0 found for all of
    them) - i.e. the image plausibly depicts nothing from the prompt at all.

    Deliberately narrower than "any 'found 0' in the reason": a line like
    "expected red dog, found 0 (confusion set)" is an ATTRIBUTE check (the dog
    was detected fine, just not classified as red - a correct, valid negation
    result, not a pairing problem) and "expected exactly 1 X, found 2" is an
    over-detection, not an absence. Only bare "...found 0" lines (no
    parenthetical) count as an object actually being absent. A single object
    or two out of several missing is a normal detector limitation, worth
    keeping; only when the image contains recognizably none of the required
    objects is it worth a human look for a possible upstream pairing issue.
    """
    auto_correct = data.get("autoCorrect")
    auto_reason = data.get("autoReason")

    if auto_correct is None:
        parts = Path(source_path).parts
        if len(parts) < 6:
            return False
        model, setting, category, idx = parts[0], parts[1], parts[2], parts[3]
        sample = parts[-1]
        results = load_results(model, setting, category)
        auto_correct, auto_reason = results.get((idx, sample), (None, ""))

    if auto_correct is not False:
        return False

    auto_reason = auto_reason or ""
    zero_count_lines = [
        line for line in auto_reason.split("\n")
        if ZERO_COUNT_LINE.match(line.strip())
    ]
    # For a single-object prompt, "all objects undetected" is trivially true
    # whenever that one object misses - including a legitimate but stylized
    # image (an illustration a photo-trained detector can't recognize). Only
    # meaningful as a signal once there are several objects that would all
    # have to miss together.
    required_object_count = len(data.get("objects") or [])
    return required_object_count >= 2 and len(zero_count_lines) >= required_object_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-image-check", action="store_true",
                         help="Skip downloading+hashing every image (faster, "
                              "only checks fields against source metadata)")
    args = parser.parse_args()

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    image_mismatches = []
    stale_fields = []
    missing_source = []
    total_mismatches = []
    no_source_path = []
    checked = 0

    for doc in db.collection("datapoints").stream():
        data = doc.to_dict()
        source_path = data.get("sourcePath")
        if not source_path:
            no_source_path.append(doc.id)
            continue

        local_path = OMAR_ROOT / source_path
        if not local_path.is_file():
            missing_source.append(doc.id)
            continue

        checked += 1

        if not args.skip_image_check:
            blob = bucket.blob(f"{STORAGE_PREFIX}/{doc.id}.png")
            if not blob.exists():
                image_mismatches.append((doc.id, "storage blob missing"))
            else:
                remote_hash = hashlib.sha256(blob.download_as_bytes()).hexdigest()
                local_hash = sha256_of_file(local_path)
                if remote_hash != local_hash:
                    image_mismatches.append((doc.id, "uploaded image differs from omar_data source"))

        metadata = load_metadata(local_path)
        if metadata is not None:
            if data.get("prompt") != metadata.get("prompt"):
                stale_fields.append((doc.id, "prompt differs from source metadata"))

            expected_objects = build_objects(metadata)
            if data.get("objects") != expected_objects:
                stale_fields.append((doc.id, "objects differ from source metadata"))

            expected_conditions = build_conditions(metadata)
            if data.get("conditions") != expected_conditions:
                stale_fields.append((doc.id, "conditions differ from source metadata"))

        if check_total_mismatch(doc.id, data, source_path):
            total_mismatches.append(doc.id)

    print(f"Checked {checked} datapoints "
          f"({len(no_source_path)} skipped, no sourcePath e.g. demo_001).\n")

    def report(title, items, is_bug):
        label = "BUG" if is_bug else "info"
        print(f"--- {title}: {len(items)} [{label}] ---")
        for item in items:
            print(f"  {item}")
        print()

    report("Missing source files", missing_source, True)
    if not args.skip_image_check:
        report("Image mismatches", image_mismatches, True)
    report("Stale fields (out of sync with source metadata)", stale_fields, True)
    report("Total mismatch (every required object undetected - worth a human look)",
           total_mismatches, False)

    real_bugs = len(missing_source) + len(image_mismatches) + len(stale_fields)
    if real_bugs == 0:
        print("No integrity problems found.")
    else:
        print(f"{real_bugs} integrity problem(s) found — see BUG sections above.")


if __name__ == "__main__":
    main()
