"""Select and upload a balanced batch of image-text datapoints for annotation.

Supersedes `add_random_flux2_datapoints.py`. Differences:

- Works across every model under `omar_data/` (flux2, flux2-4bit, qwen, sd35, ...),
  not just flux2, so the same prompt can be uploaded again with a different
  model's generated image.
- Reads each model's automatic-evaluator `results/<category>.jsonl` (written by
  NegGenEval's `evaluate_images.py`) to know, per generated image, whether the
  automatic system judged it correct or incorrect. Evaluation results are
  produced incrementally, so a model/category/setting with no results.jsonl yet
  is simply skipped unless --include-unknown-correctness is passed.
- Selects a balanced sample across model / category / pos-neg setting / automatic
  correctness using round-robin stratified sampling, so no single combination
  dominates the ~500-image annotation set.
- Skips any image already present in Firestore `datapoints` (by deterministic
  document ID), so the script is safe to re-run as more evaluation results land.
- Records every upload to a local CSV manifest
  (`t2i-annotation/src/manifests/upload_manifest.csv`) for easy review, in
  addition to the fields already stored on the Firestore document.

`omar_data` is read-only for this script; it is never modified.

Usage examples
--------------
Preview a balanced batch of 40 images across every model/category/setting,
using only images with a known automatic-evaluator verdict, without touching
Firebase:

    .venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py --num-total 40 --dry-run

Upload 500 images, balancing only by model and automatic correctness (let
category/setting vary naturally):

    .venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \\
        --num-total 500 --balance-by model correct

Restrict to specific models/categories:

    .venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \\
        --models flux2 sd35 --categories neg_attr_color neg_rel_depth --num-total 100
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

from datapoint_fields import build_conditions, build_objects

REPO_ROOT = Path(__file__).resolve().parents[2]
OMAR_ROOT = REPO_ROOT / "omar_data"
STORAGE_PREFIX = "annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
MANIFEST_PATH = Path(__file__).resolve().parent / "manifests" / "upload_manifest.csv"
MANIFEST_FIELDS = [
    "docId", "uploadedAt", "model", "category", "posNeg", "tag",
    "idx", "sample", "prompt", "autoCorrect", "autoReason",
    "storagePath", "sourcePath",
]

ALL_CATEGORIES = [
    "neg_attr_color",
    "neg_attr_material",
    "neg_rel_comparative",
    "neg_rel_depth",
    "neg_rel_directional",
    "neg_rel_proximity",
]
BALANCE_DIMENSIONS = ["model", "category", "setting", "correct"]


def discover_models():
    """Any omar_data subdirectory that has generated images under a known category.

    Excludes sibling directories like `prompts/` and `plots/` that happen to
    also contain pos<P>_neg<N>-named subdirectories but hold no images.
    """
    models = []
    for entry in sorted(OMAR_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        has_images = any(
            next(entry.glob(f"pos*_neg*/{category}/*/samples/*.png"), None) is not None
            for category in ALL_CATEGORIES
        )
        if has_images:
            models.append(entry.name)
    return models


def discover_settings(model):
    return sorted(p.name for p in (OMAR_ROOT / model).glob("pos*_neg*"))


def load_results(model, setting, category):
    """Map (idx, sample_filename) -> (correct, reason) from results/<category>.jsonl.

    Returns an empty dict if the evaluator hasn't produced results yet for this
    model/setting/category combination.
    """
    results_path = OMAR_ROOT / model / setting / "results" / f"{category}.jsonl"
    lookup = {}
    if not results_path.is_file():
        return lookup

    with open(results_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            filename_parts = Path(record["filename"]).parts
            idx, sample = filename_parts[-3], filename_parts[-1]
            lookup[(idx, sample)] = (record.get("correct"), record.get("reason", ""))
    return lookup


def sanitize_ref(relative_path):
    """omar_data-relative path -> flat id, e.g. flux2-pos1_neg1-neg_attr_color-00002-samples-00000"""
    return relative_path.replace("/", "-").rsplit(".", 1)[0]


def discover_candidates(models, settings, categories, include_unknown_correctness):
    """Every (model, setting, category, prompt, sample-image) combination on disk."""
    candidates = []
    for model in models:
        model_settings = [s for s in discover_settings(model) if s in settings]
        for setting in model_settings:
            for category in categories:
                category_dir = OMAR_ROOT / model / setting / category
                if not category_dir.is_dir():
                    continue

                results_lookup = load_results(model, setting, category)

                for metadata_path in sorted(category_dir.glob("*/metadata.jsonl")):
                    idx = metadata_path.parent.name
                    samples_dir = metadata_path.parent / "samples"
                    if not samples_dir.is_dir():
                        continue

                    with open(metadata_path) as f:
                        line = f.readline().strip()
                    if not line:
                        continue
                    metadata = json.loads(line)

                    for image_path in sorted(samples_dir.glob("*.png")):
                        correct, reason = results_lookup.get(
                            (idx, image_path.name), (None, "")
                        )
                        if correct is None and not include_unknown_correctness:
                            continue

                        relative_source = image_path.relative_to(OMAR_ROOT).as_posix()
                        candidates.append(
                            {
                                "model": model,
                                "setting": setting,
                                "category": category,
                                "idx": idx,
                                "sample": image_path.name,
                                "image_path": image_path,
                                "metadata": metadata,
                                "correct": correct,
                                "reason": reason,
                                "source_relpath": relative_source,
                                "doc_id": sanitize_ref(relative_source),
                            }
                        )
    return candidates


def stratified_sample(candidates, dims, num_total, rng):
    """Round-robin sample across every distinct combination of `dims`.

    Cycles through strata in random order, taking one candidate from each in
    turn, so no single (model, category, setting, correctness) combination can
    dominate the batch while pools with fewer items are exhausted gracefully.
    """
    groups = defaultdict(list)
    for candidate in candidates:
        key = tuple(candidate[d] for d in dims)
        groups[key].append(candidate)
    for group in groups.values():
        rng.shuffle(group)

    active_keys = list(groups.keys())
    rng.shuffle(active_keys)

    selected = []
    while active_keys and len(selected) < num_total:
        next_round = []
        for key in active_keys:
            if len(selected) >= num_total:
                break
            group = groups[key]
            if group:
                selected.append(group.pop())
            if group:
                next_round.append(key)
        active_keys = next_round
        rng.shuffle(active_keys)

    exhausted = [key for key, group in groups.items() if not group]
    return selected, exhausted


def print_summary(label, items, dims):
    print(f"\n{label} ({len(items)} items):")
    for dim in dims:
        counts = Counter(item[dim] for item in items)
        breakdown = ", ".join(f"{key}={count}" for key, count in sorted(
            counts.items(), key=lambda kv: str(kv[0])
        ))
        print(f"  by {dim}: {breakdown}")


def append_manifest(rows):
    is_new = not MANIFEST_PATH.exists()
    with open(MANIFEST_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", default=None,
                         help="Model keys under omar_data/ (default: all discovered)")
    parser.add_argument("--categories", nargs="+", default=ALL_CATEGORIES,
                         choices=ALL_CATEGORIES)
    parser.add_argument("--settings", nargs="+", default=None,
                         help="pos<P>_neg<N> dirs to include (default: all discovered)")
    parser.add_argument("--num-total", type=int, default=40,
                         help="How many datapoints to select (default: 40)")
    parser.add_argument("--balance-by", nargs="+", default=BALANCE_DIMENSIONS,
                         choices=BALANCE_DIMENSIONS,
                         help="Dimensions to stratify the sample on (default: all)")
    parser.add_argument("--include-unknown-correctness", action="store_true",
                         help="Also consider images with no automatic-evaluator "
                              "result yet (their 'correct' bucket is None)")
    parser.add_argument("--seed", type=int, default=None,
                         help="Random seed for reproducible selection")
    parser.add_argument("--dry-run", action="store_true",
                         help="Select and print the batch, but do not upload "
                              "anything to Firebase or write the manifest")
    args = parser.parse_args()
    args.models = args.models or discover_models()
    return args


def main():
    args = parse_args()
    rng = random.Random(args.seed)

    all_settings = sorted({s for m in args.models for s in discover_settings(m)})
    settings = args.settings or all_settings

    print(f"Models: {args.models}")
    print(f"Settings: {settings}")
    print(f"Categories: {args.categories}")

    candidates = discover_candidates(
        args.models, set(settings), args.categories, args.include_unknown_correctness
    )
    print_summary("Candidate pool", candidates, ["model", "category", "setting", "correct"])

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    db = firestore.client()
    bucket = storage.bucket()

    existing_ids = {ref.id for ref in db.collection("datapoints").list_documents()}
    candidates = [c for c in candidates if c["doc_id"] not in existing_ids]
    print(f"\n{len(candidates)} candidates remain after excluding "
          f"{len(existing_ids)} already-uploaded datapoints.")

    selected, exhausted_strata = stratified_sample(
        candidates, args.balance_by, args.num_total, rng
    )
    if exhausted_strata:
        print(f"\nNote: {len(exhausted_strata)} of the requested strata ran out of "
              f"candidates before reaching an even split: {exhausted_strata}")
    if len(selected) < args.num_total:
        print(f"\nWarning: only {len(selected)} of {args.num_total} requested "
              f"datapoints could be selected from the available pool.")

    print_summary("Selected batch", selected, ["model", "category", "setting", "correct"])

    if args.dry_run:
        print("\n--dry-run set: not uploading anything.")
        for c in selected:
            print(f"  would upload {c['doc_id']}  (correct={c['correct']})")
        return

    manifest_rows = []
    uploaded_at = datetime.now(timezone.utc).isoformat()

    for c in selected:
        doc_id = c["doc_id"]
        storage_path = f"{STORAGE_PREFIX}/{doc_id}.png"
        blob = bucket.blob(storage_path)
        blob.upload_from_filename(str(c["image_path"]), content_type="image/png")
        blob.make_public()

        metadata = c["metadata"]
        datapoint = {
            "prompt": metadata["prompt"],
            "imageUrl": blob.public_url,
            "objects": build_objects(metadata),
            "conditions": build_conditions(metadata),
            "tag": metadata.get("tag"),
            "category": metadata.get("category"),
            "model": c["model"],
            "posNeg": c["setting"],
            "sourcePath": c["source_relpath"],
            "autoCorrect": c["correct"],
            "autoReason": c["reason"],
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
        db.collection("datapoints").document(doc_id).set(datapoint)
        print(f"Inserted datapoints/{doc_id}  <-  {c['source_relpath']}")

        manifest_rows.append({
            "docId": doc_id,
            "uploadedAt": uploaded_at,
            "model": c["model"],
            "category": c["category"],
            "posNeg": c["setting"],
            "tag": metadata.get("tag"),
            "idx": c["idx"],
            "sample": c["sample"],
            "prompt": metadata["prompt"],
            "autoCorrect": c["correct"],
            "autoReason": c["reason"],
            "storagePath": storage_path,
            "sourcePath": c["source_relpath"],
        })

    append_manifest(manifest_rows)
    print(f"\nDone. Inserted {len(selected)} datapoints. "
          f"Manifest updated at {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
