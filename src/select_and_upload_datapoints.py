"""Select and upload a balanced batch of image-text datapoints for annotation.

Supersedes `add_random_flux2_datapoints.py`. Differences:

- Works across every model under `geneval_data/` (flux2, flux2-4bit, qwen, sd35, ...),
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
  New prompts compensate for category/setting deficits in those existing groups.
  Fresh prompt selection balances categories evenly and weights settings so that
  every setting with at least one negated slot gets an equal target quota
  (`setting_weight`) -- NegGenEval is a negation benchmark, so `pos*_neg0` (the
  no-negation control) is deliberately under-sampled relative to any single
  negation setting, but `pos*_neg1` and `pos*_neg2` settings are not stacked
  against each other.
- Skips any image already present in Firestore `datapoints` (by deterministic
  document ID), so the script is safe to re-run as more evaluation results land.
- Records every upload to a line-oriented JSONL manifest
  (`t2i-annotation/samples/manifests/upload_manifest.jsonl`) for easy review, in
  addition to the fields already stored on the Firestore document.

`geneval_data` is read-only for this script; it is never modified.

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
import itertools
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

from datapoint_fields import build_conditions, build_objects

REPO_ROOT = Path(__file__).resolve().parents[2]
OMAR_ROOT = REPO_ROOT / "geneval_data"
STORAGE_PREFIX = "annotation-images"
STORAGE_BUCKET = "neg-gen.firebasestorage.app"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
MANIFEST_DIR = Path(__file__).resolve().parents[1] / "samples" / "manifests"
MANIFEST_PATH = MANIFEST_DIR / "upload_manifest.jsonl"

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
    """Any geneval_data subdirectory that has generated images under a known category.

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
    """geneval_data-relative path -> flat id, e.g. flux2-pos1_neg1-neg_attr_color-00002-samples-00000"""
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


def marginally_balanced_sample(candidates, dims, num_total, initial_items, rng):
    """Greedily fill marginal deficits across `dims`.

    Unlike joint-stratum round robin, this starts from mandatory existing prompt
    groups. Each next candidate minimizes the sum of its current marginal bucket
    counts, with the largest individual bucket as a tie-breaker. Random choice
    among equal candidates preserves seed-controlled variation.
    """
    counts = {dim: Counter(item[dim] for item in initial_items) for dim in dims}
    remaining = list(candidates)
    selected = []
    while remaining and len(selected) < num_total:
        scores = [
            (
                sum(counts[dim][item[dim]] for dim in dims),
                max(counts[dim][item[dim]] for dim in dims),
            )
            for item in remaining
        ]
        best_score = min(scores)
        best_indices = [i for i, score in enumerate(scores) if score == best_score]
        chosen_index = rng.choice(best_indices)
        chosen = remaining.pop(chosen_index)
        selected.append(chosen)
        for dim in dims:
            counts[dim][chosen[dim]] += 1
    return selected


def setting_neg_count(setting):
    """Return the number of negative slots encoded in a `pos<P>_neg<N>` setting."""
    try:
        return int(setting.split("_neg", 1)[1])
    except (IndexError, ValueError):
        return 0


def setting_weight(setting):
    """Sampling weight: 0 negatives -> 1, any negatives -> 2.

    NegGenEval is a negation benchmark; `pos*_neg0` is the no-negation control,
    not the thing being studied, so it should be under-represented relative to
    settings that actually exercise negation understanding. But every setting
    with at least one negated slot gets the *same* weight, so e.g. `pos0_neg1`,
    `pos2_neg1`, and `pos1_neg2` end up with equal quotas instead of `neg2`
    settings being stacked on top of `neg1` settings.
    """
    return 1 if setting_neg_count(setting) == 0 else 2


def distribute(targets, total):
    """Round fractional `targets` to integers summing exactly to `total`.

    Largest-remainder rounding: floor every target, then hand the leftover
    units to whichever keys had the biggest fractional part (ties broken by
    key so results are deterministic).
    """
    buckets = {key: int(value) for key, value in targets.items()}
    remainder = total - sum(buckets.values())
    if remainder <= 0:
        return buckets
    order = sorted(
        targets,
        key=lambda key: (targets[key] - buckets[key], key),
        reverse=True,
    )
    for key in order[:remainder]:
        buckets[key] += 1
    return buckets


def resolve_quotas(keys, total_target, weight_fn, overrides):
    """Per-key integer quotas summing to `total_target`.

    Keys in `overrides` get that exact count. The remaining keys split
    whatever's left over proportionally to `weight_fn`, same as before
    overrides existed. Lets a caller pin a handful of exact target counts
    (e.g. from a one-off rebalancing plan) while everything else still
    follows the general policy.
    """
    overrides = overrides or {}
    fixed = {key: overrides[key] for key in keys if key in overrides}
    free_keys = [key for key in keys if key not in overrides]
    remaining_target = total_target - sum(fixed.values())

    if not free_keys:
        return fixed

    weights = {key: weight_fn(key) for key in free_keys}
    weight_sum = sum(weights.values())
    targets = {key: remaining_target * weights[key] / weight_sum for key in free_keys}
    quotas = distribute(targets, remaining_target)
    quotas.update(fixed)
    return quotas


def weighted_setting_balanced_sample(
    candidates, num_total, initial_items, rng,
    category_target_overrides=None, setting_target_overrides=None,
):
    """Select fresh prompt groups with category balance and weighted settings.

    Existing prompt groups are fixed history. New prompt groups are chosen from
    the feasible pool by minimizing the combined deficit against:

    - equal category quotas across the whole batch, unless
      `category_target_overrides` pins specific categories to exact counts
    - equal global quotas for every setting with at least one negated slot,
      with the `pos*_neg0` control under-sampled relative to those
      (`setting_weight`), unless `setting_target_overrides` pins specific
      settings to exact counts

    This keeps the batch balanced in both dimensions while still allowing the
    available candidate pool to constrain the exact result.
    """
    if not candidates or num_total <= 0:
        return []

    # Quotas must reflect the balance of the FINAL batch (existing/mandatory
    # groups plus these fresh picks), not just these fresh picks in isolation.
    # `counts` below starts pre-loaded with `initial_items`, so targets have to
    # be computed against that same total or any skew already present in the
    # mandatory groups never gets compensated for.
    total_target = num_total + len(initial_items)

    categories = sorted({candidate["category"] for candidate in candidates})
    settings = sorted({candidate["setting"] for candidate in candidates})

    category_quotas = resolve_quotas(
        categories, total_target, lambda _category: 1, category_target_overrides
    )
    setting_quotas = resolve_quotas(
        settings, total_target, setting_weight, setting_target_overrides
    )

    counts = {
        "category": Counter(item["category"] for item in initial_items),
        "setting": Counter(item["setting"] for item in initial_items),
    }

    remaining = list(candidates)
    rng.shuffle(remaining)

    selected = []
    while remaining and len(selected) < num_total:
        scored = []
        for item in remaining:
            cat = item["category"]
            setting = item["setting"]
            score = (
                max(0, counts["category"][cat] - category_quotas[cat]),
                max(0, counts["setting"][setting] - setting_quotas[setting]),
                counts["category"][cat] / max(1, category_quotas[cat]),
                counts["setting"][setting] / max(1, setting_quotas[setting]),
            )
            scored.append((score, item))

        best_score = min(score for score, _ in scored)
        best_indices = [i for i, (score, _) in enumerate(scored) if score == best_score]
        chosen_index = rng.choice(best_indices)
        chosen = remaining.pop(chosen_index)
        selected.append(chosen)
        counts["category"][chosen["category"]] += 1
        counts["setting"][chosen["setting"]] += 1

    return selected


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
        # Use sourcePath for the corpus identity. Firestore's `category` field
        # stores the semantic value (e.g. "material"), while selection uses the
        # dataset directory name (e.g. "neg_attr_material"). Mixing those two
        # representations caused existing uploads to be missed.
        "model": parts[0],
        "setting": parts[1],
        "category": parts[2],
        "idx": parts[-3],
        "sample": parts[-1],
        "correct": data.get("autoCorrect"),
        "source_relpath": source_path,
    }


def select_paired_by_prompt(
    candidates, existing, models, num_prompts, rng,
    category_target_overrides=None, setting_target_overrides=None,
):
    """Select missing images for prompt-matched, all-model bundles.

    Every returned prompt has either an existing or newly selected datapoint for
    each requested model. Existing prompt groups are completed first. New prompt
    groups are round-robin balanced over category and setting -- or pinned to
    exact counts via `category_target_overrides`/`setting_target_overrides`.
    Within each model, image choice favors the currently underrepresented
    evaluator-correctness bucket, including existing uploads in those counts.
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

    def missing_models(key):
        return [model for model in models
                if not (existing_by_prompt[key].get(model) or pool[key].get(model))]

    def is_feasible(key):
        return not missing_models(key)

    existing_keys = set(existing_by_prompt)
    mandatory = sorted(key for key in existing_keys if is_feasible(key))
    unresolved = sorted(key for key in existing_keys if not is_feasible(key))
    unresolved_missing = {key: missing_models(key) for key in unresolved}

    # --num-prompts is the desired total number of prompt groups. Completing all
    # feasible pre-existing groups is a stronger invariant and may exceed it.
    selected_keys = list(mandatory)
    target = max(num_prompts, len(selected_keys))
    mandatory_items = [
        {"setting": key[0], "category": key[1], "idx": key[2], "key": key}
        for key in selected_keys
    ]
    fresh = [
        {"setting": key[0], "category": key[1], "idx": key[2], "key": key}
        for key in pool
        if key not in existing_keys and is_feasible(key)
    ]
    chosen_fresh = weighted_setting_balanced_sample(
        fresh,
        target - len(selected_keys),
        mandatory_items,
        rng,
        category_target_overrides=category_target_overrides,
        setting_target_overrides=setting_target_overrides,
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

    return selected, bundle_rows, unresolved, [], unresolved_missing


def print_summary(label, items, dims):
    print(f"\n{label} ({len(items)} items):")
    for dim in dims:
        counts = Counter(item[dim] for item in items)
        breakdown = ", ".join(f"{key}={count}" for key, count in sorted(
            counts.items(), key=lambda kv: str(kv[0])
        ))
        print(f"  by {dim}: {breakdown}")


def write_manifest_snapshot(db):
    """Rewrite JSONL from live Firestore datapoints, one document per line."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for doc_snap in db.collection("datapoints").stream():
        data = doc_snap.to_dict() or {}
        source_path = data.get("sourcePath", "")
        parts = Path(source_path).parts
        has_standard_path = len(parts) >= 6 and parts[-2] == "samples"
        created_at = data.get("createdAt")
        records.append({
            "docId": doc_snap.id,
            "uploadedAt": created_at.isoformat() if created_at else None,
            "model": parts[0] if has_standard_path else data.get("model"),
            "category": parts[2] if has_standard_path else data.get("category"),
            "posNeg": parts[1] if has_standard_path else data.get("posNeg"),
            "tag": data.get("tag"),
            "idx": parts[-3] if has_standard_path else None,
            "sample": parts[-1] if has_standard_path else None,
            "prompt": data.get("prompt"),
            "autoCorrect": data.get("autoCorrect"),
            "autoReason": data.get("autoReason", ""),
            "storagePath": f"{STORAGE_PREFIX}/{doc_snap.id}.png",
            "sourcePath": source_path,
        })

    temp_path = MANIFEST_PATH.with_suffix(".jsonl.tmp")
    with open(temp_path, "w") as manifest_file:
        for record in sorted(records, key=lambda row: row["docId"]):
            manifest_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    temp_path.replace(MANIFEST_PATH)
    return len(records)


def parse_target_overrides(pairs):
    """["pos1_neg0=9", "pos2_neg0=7"] -> {"pos1_neg0": 9, "pos2_neg0": 7}."""
    if not pairs:
        return None
    overrides = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not value.isdigit():
            raise argparse.ArgumentTypeError(
                f"Expected KEY=COUNT (e.g. pos1_neg0=9), got {pair!r}"
            )
        overrides[key] = int(value)
    return overrides


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", default=None,
                         help="Model keys under geneval_data/ (default: all discovered)")
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
    parser.add_argument("--setting-target", nargs="+", default=None, metavar="SETTING=COUNT",
                         help="Paired mode: pin exact final prompt-group counts for "
                              "specific settings (e.g. pos1_neg0=9 pos2_neg0=7). "
                              "Settings not listed still follow --num-prompts and the "
                              "usual negation-count weighting.")
    parser.add_argument("--category-target", nargs="+", default=None, metavar="CATEGORY=COUNT",
                         help="Paired mode: pin exact final prompt-group counts for "
                              "specific categories. Categories not listed still split "
                              "the remaining total evenly.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Select and print the batch, but do not upload "
                              "anything to Firebase or write the manifest")
    args = parser.parse_args()
    args.models = args.models or discover_models()
    args.setting_target = parse_target_overrides(args.setting_target)
    args.category_target = parse_target_overrides(args.category_target)
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

    if not args.unpaired:
        initial_prompt_groups = list({prompt_key(item): item for item in existing}.values())
        print_summary(
            "Initial prompt groups (already in Firestore)",
            initial_prompt_groups, ["category", "setting"],
        )

    if args.unpaired:
        selected, exhausted_strata = stratified_sample(
            candidates, args.balance_by, args.num_total, rng
        )
        bundle_rows = []
        unresolved = []
        requested = args.num_total
    else:
        requested = args.num_prompts if args.num_prompts is not None else args.num_total
        selected, bundle_rows, unresolved, exhausted_strata, unresolved_missing = select_paired_by_prompt(
            candidates, existing, args.models, requested, rng,
            category_target_overrides=args.category_target,
            setting_target_overrides=args.setting_target,
        )
        print(f"\nPaired selection: {len(bundle_rows)} prompt groups, "
              f"{len(selected)} new datapoints, {len(args.models)} requested models/group.")
        for row in bundle_rows:
            print(f"  {row['setting']}/{row['category']}/{row['idx']}: "
                  f"existing={row['existing_models']} add={row['added_models']}")
        if unresolved:
            print(f"\nWarning: {len(unresolved)} existing prompt groups cannot yet "
                  "be completed for every requested model because images/results "
                  "are unavailable:")
            for key in unresolved:
                setting, category, idx = key
                print(f"  {setting}/{category}/{idx}: missing={unresolved_missing[key]}")
        print_summary(
            "Final prompt groups (existing + newly selected)", bundle_rows, ["category", "setting"]
        )
    if exhausted_strata:
        print(f"\nNote: {len(exhausted_strata)} of the requested strata ran out of "
              f"candidates before reaching an even split: {exhausted_strata}")
    if args.unpaired and len(selected) < requested:
        print(f"\nWarning: only {len(selected)} of {requested} requested "
              "datapoints could be selected from the available pool.")
    if not args.unpaired and len(bundle_rows) < requested:
        print(f"\nWarning: only {len(bundle_rows)} of {requested} requested "
              "all-model prompt groups could be formed from the available pool.")

    image_summary_label = (
        "New image datapoints to upload" if not args.unpaired else "Selected batch"
    )
    print_summary(
        image_summary_label,
        selected,
        ["model", "category", "setting", "correct"],
    )

    if args.dry_run:
        print("\n--dry-run set: not uploading anything.")
        for c in selected:
            print(f"  would upload {c['doc_id']}  (correct={c['correct']})")
        return

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
            "conditionQuestionVersion": 2,
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

    manifest_count = write_manifest_snapshot(db)
    print(f"\nDone. Inserted {len(selected)} datapoints. "
          f"Manifest refreshed with {manifest_count} live datapoints at {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
