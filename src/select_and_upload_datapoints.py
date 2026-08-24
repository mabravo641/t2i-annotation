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
- By default selects prompt-matched bundles: one generated image for every
  requested model, balanced across category, pos-neg setting, and automatic
  correctness. Existing uploads are treated as fixed bundle members and every
  feasible incomplete existing bundle is completed before new prompts are added.
- Skips any image already present in Firestore `datapoints` (by deterministic
  document ID), so the script is safe to re-run as more evaluation results land.
- Records every upload to a local CSV manifest
  (`t2i-annotation/src/manifests/upload_manifest.csv`) for easy review, in
  addition to the fields already stored on the Firestore document.

`omar_data` is read-only for this script; it is never modified.

Usage examples
--------------
Preview 40 prompt groups across every model/category/setting,
using only images with a known automatic-evaluator verdict, without touching
Firebase:

    .venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py --num-prompts 40 --dry-run

Use the legacy independent-image mode to upload 500 images balanced only by
model and automatic correctness (letting category/setting vary naturally):

    .venv/bin/python t2i-annotation/src/select_and_upload_datapoints.py \\
        --unpaired --num-total 500 --balance-by model correct

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
PAIRED_BALANCE_DIMENSIONS = ["category", "setting"]


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


def prompt_key(item):
    """Stable prompt identity shared by every model's rendering."""
    return item["setting"], item["category"], item["idx"]


def existing_record(doc):
    """Convert a Firestore datapoint to the fields needed by paired selection."""
    data = doc.to_dict() or {}
    source_path = data.get("sourcePath", "")
    parts = Path(source_path).parts
    if len(parts) < 6 or parts[-2] != "samples":
        return None
    return {
        "doc_id": doc.id,
        "model": data.get("model") or parts[0],
        "setting": data.get("posNeg") or parts[1],
        "category": data.get("category") or parts[2],
        "idx": parts[-3],
        "sample": parts[-1],
        "correct": data.get("autoCorrect"),
        "source_relpath": source_path,
    }


def select_paired_by_prompt(candidates, existing, models, num_prompts, rng):
    """Select missing images for prompt-matched, all-model bundles.

    Every returned prompt has either an existing or newly selected datapoint for
    each requested model. Existing prompt groups are completed first. New prompt
    groups are round-robin balanced over category and setting. Within each model,
    image choice favors the currently underrepresented evaluator-correctness
    bucket, including existing uploads in those counts.
    """
    pool = defaultdict(lambda: defaultdict(list))
    for candidate in candidates:
        pool[prompt_key(candidate)][candidate["model"]].append(candidate)

    existing_by_prompt = defaultdict(lambda: defaultdict(list))
    correctness_counts = {model: Counter() for model in models}
    for item in existing:
        if item["model"] not in models:
            continue
        existing_by_prompt[prompt_key(item)][item["model"]].append(item)
        correctness_counts[item["model"]][item["correct"]] += 1

    def is_feasible(key):
        return all(existing_by_prompt[key].get(model) or pool[key].get(model)
                   for model in models)

    existing_keys = set(existing_by_prompt)
    mandatory = sorted(key for key in existing_keys if is_feasible(key))
    unresolved = sorted(key for key in existing_keys if not is_feasible(key))

    # --num-prompts is the desired total number of prompt groups. Completing all
    # feasible pre-existing groups is a stronger invariant and may exceed it.
    selected_keys = list(mandatory)
    target = max(num_prompts, len(selected_keys))
    fresh = [
        {"setting": key[0], "category": key[1], "idx": key[2], "key": key}
        for key in pool
        if key not in existing_keys and is_feasible(key)
    ]
    chosen_fresh, exhausted = stratified_sample(
        fresh, PAIRED_BALANCE_DIMENSIONS, target - len(selected_keys), rng
    )
    selected_keys.extend(item["key"] for item in chosen_fresh)

    selected = []
    bundle_rows = []
    for key in selected_keys:
        setting, category, idx = key
        existing_models = sorted(
            model for model in models if existing_by_prompt[key].get(model)
        )
        added_models = []
        for model in models:
            if existing_by_prompt[key].get(model):
                continue
            options = pool[key][model]
            by_correct = defaultdict(list)
            for option in options:
                by_correct[option["correct"]].append(option)
            # Choose a correctness bucket least represented for this model, then
            # one random rendering inside it. This balances True/False (and None
            # when explicitly enabled) without breaking prompt matching.
            min_count = min(correctness_counts[model][value] for value in by_correct)
            values = [value for value in by_correct
                      if correctness_counts[model][value] == min_count]
            value = rng.choice(values)
            choice = rng.choice(by_correct[value])
            choice["prompt_group_id"] = f"{setting}-{category}-{idx}"
            selected.append(choice)
            added_models.append(model)
            correctness_counts[model][value] += 1
        bundle_rows.append({
            "setting": setting,
            "category": category,
            "idx": idx,
            "existing_models": existing_models,
            "added_models": added_models,
        })

    return selected, bundle_rows, unresolved, exhausted


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
                         help="Legacy unpaired mode: datapoints to select; paired "
                              "mode: alias for --num-prompts (default: 40)")
    parser.add_argument("--num-prompts", type=int, default=None,
                         help="Desired total prompt groups in paired mode; each "
                              "group has one datapoint per requested model")
    parser.add_argument("--unpaired", action="store_true",
                         help="Use the legacy independent-datapoint sampler instead "
                              "of selecting one image per model for each prompt")
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

    existing_docs = list(db.collection("datapoints").stream())
    existing_ids = {doc.id for doc in existing_docs}
    existing = [
        item for doc in existing_docs
        if (item := existing_record(doc))
        and item["model"] in args.models
        and item["setting"] in settings
        and item["category"] in args.categories
    ]
    candidates = [c for c in candidates if c["doc_id"] not in existing_ids]
    print(f"\n{len(candidates)} candidates remain after excluding "
          f"{len(existing_ids)} already-uploaded datapoints.")

    if args.unpaired:
        selected, exhausted_strata = stratified_sample(
            candidates, args.balance_by, args.num_total, rng
        )
        bundle_rows = []
        unresolved = []
        requested = args.num_total
    else:
        requested = args.num_prompts if args.num_prompts is not None else args.num_total
        selected, bundle_rows, unresolved, exhausted_strata = select_paired_by_prompt(
            candidates, existing, args.models, requested, rng
        )
        print(f"\nPaired selection: {len(bundle_rows)} prompt groups, "
              f"{len(selected)} new datapoints, {len(args.models)} requested models/group.")
        for row in bundle_rows:
            print(f"  {row['setting']}/{row['category']}/{row['idx']}: "
                  f"existing={row['existing_models']} add={row['added_models']}")
        if unresolved:
            print(f"\nWarning: {len(unresolved)} existing prompt groups cannot yet "
                  "be completed for every requested model because images/results "
                  f"are unavailable: {unresolved}")
    if exhausted_strata:
        print(f"\nNote: {len(exhausted_strata)} of the requested strata ran out of "
              f"candidates before reaching an even split: {exhausted_strata}")
    if args.unpaired and len(selected) < requested:
        print(f"\nWarning: only {len(selected)} of {requested} requested "
              "datapoints could be selected from the available pool.")
    if not args.unpaired and len(bundle_rows) < requested:
        print(f"\nWarning: only {len(bundle_rows)} of {requested} requested "
              "all-model prompt groups could be formed from the available pool.")

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
            "promptGroupId": c.get("prompt_group_id"),
            "reservedAnnotationSlots": 0,
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
