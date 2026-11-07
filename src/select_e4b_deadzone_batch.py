"""Rebuttal E4b: select (and optionally upload) a balanced, randomised batch of
dead-zone passes for human annotation.

What is sampled
---------------
Negated relation statements that the PAPER evaluator (`results_rfdetr_siglip`)
passed, from the full benchmark (11 models, every setting with >= 1 negation):

  stratum          definition                                           default n
  depth_deadzone   negated depth stmt passed, NO depth predicate fired       150
  dir_deadzone     negated directional stmt passed, NO directional fired      50
  depth_earned     negated depth stmt passed, ANOTHER depth predicate fired   50
                   (control: the 89% "earned" rate rests on n = 18 so far)

Same definition as Table 19 / `05_free_pass.py` / `count_600.py`:
"passed" = `computed` is not None and the denied predicate is not in it;
"dead-zone" = additionally no predicate of the category's own vocabulary in it.
`computed is None` (object not detected) is never sampled.

How it is balanced and still random
-----------------------------------
1. One statement per image (chosen at random if an image has several).
2. At most one image per prompt (setting, category, prompt idx) across the
   whole batch, so no prompt is seen twice with different models.
3. Per stratum, equal quotas per model (largest remainder). If a model's pool is
   too small, its shortfall is redistributed to the other models and reported.
4. Within a (stratum, model) cell, settings are visited round-robin in random
   order, and the image inside a setting is drawn uniformly at random.
   -> balanced over model and setting, random everywhere else (prompt, seed,
   objects, image). Everything is driven by --seed, so the batch is reproducible.
5. Excluded: the 600 already-annotated images, anything already in the upload
   manifest, and (with --upload) anything already in Firestore `datapoints`.

Blinding: the stratum / evaluator verdict is written only to the LOCAL manifest.
The Firestore datapoint carries the same fields as earlier uploads plus a
`batch` tag; the annotation UI hides model and evaluator fields as before, and the
annotator answers every question of the image, as in the main study, so
dead-zone, earned and other statements are mixed.

Usage
-----
Dry run (default; reads results only, writes the local selection manifest):

    python t2i-annotation/src/select_e4b_deadzone_batch.py --seed 20261107

Upload the exact selection from a previous dry run (needs firebase_admin and the
service-account file; run with the annotation venv):

    .venv/bin/python t2i-annotation/src/select_e4b_deadzone_batch.py \\
        --from-manifest t2i-annotation/samples/manifests/e4b_selection_seed20261107.jsonl --upload
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from datapoint_fields import build_conditions, build_objects

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path("/data2/oelfatairy/data/geneval")
RESULTS_DIR = "results_rfdetr_siglip"  # paper evaluator (relation files identical to ct0.5/_final)
ANNOTATED_600 = REPO_ROOT / "t2i-annotation" / "samples" / "metric_evaluation" / "input.jsonl"
MANIFEST_DIR = REPO_ROOT / "t2i-annotation" / "samples" / "manifests"
UPLOAD_MANIFEST = MANIFEST_DIR / "upload_manifest.jsonl"

MODELS = ["blip3o-next", "flux2", "flux2-4bit", "hidream-4bit", "hunyuanimage-distilled", "janus-pro",
          "negationclip", "qwen", "sd15", "sd35", "zimage"]
# Category vocabularies, as in neg_ext.vocab.RELATION_CATEGORIES (copied so this
# script has no dependency on the evaluator package; checked in --self-check).
OWN_PREDICATES = {
    "depth": {"in front of", "behind"},
    "directional": {"left of", "right of", "above", "below"},
}
STRATA = {  # name -> (category, pass type)
    "depth_deadzone": ("depth", "deadzone"),
    "dir_deadzone": ("directional", "deadzone"),
    "depth_earned": ("depth", "earned"),
}


def neg_count(setting: str) -> int:
    return int(setting.split("_neg", 1)[1])


def loads(x):
    return json.loads(x) if isinstance(x, str) else (x or {})


def excluded_ids() -> set[str]:
    ids = {json.loads(l)["docId"] for l in open(ANNOTATED_600)}
    if UPLOAD_MANIFEST.is_file():
        ids |= {json.loads(l)["docId"] for l in open(UPLOAD_MANIFEST) if l.strip()}
    return ids


def scan(exclude: set[str]) -> dict[str, list[dict]]:
    """All eligible images per stratum; one candidate statement chosen later."""
    pools: dict[str, list[dict]] = defaultdict(list)
    n_mismatch = 0
    for model in MODELS:
        for sdir in sorted((DATA_ROOT / model).glob("pos*_neg*")):
            setting = sdir.name
            if neg_count(setting) == 0:
                continue
            for category in ("depth", "directional"):
                tag = f"neg_rel_{category}"
                f = sdir / RESULTS_DIR / f"{tag}.jsonl"
                if not f.is_file():
                    print(f"  [missing] {f}")
                    continue
                for line in open(f):
                    d = json.loads(line)
                    img = Path(d["filename"])
                    rel = img.relative_to(DATA_ROOT).as_posix()
                    doc_id = rel.replace("/", "-").rsplit(".", 1)[0]
                    if doc_id in exclude or not img.is_file():
                        continue
                    meta, det = loads(d["metadata"]), loads(d["details"])
                    conds = build_conditions(meta)
                    per_type = defaultdict(list)
                    for i, e in enumerate(det.get("edges") or []):
                        comp = e.get("computed")
                        if e.get("polarity") != "neg" or comp is None or e["predicate"] in comp:
                            continue
                        # edge i must be condition cond_rel_i (same order as the prompt's slots)
                        c = conds[i] if i < len(conds) else None
                        if not c or (c["predicate"], c["subject"], c["target"]) != (e["predicate"], e["subject"], e["target"]):
                            n_mismatch += 1
                            continue
                        kind = "deadzone" if not (set(comp) & OWN_PREDICATES[category]) else "earned"
                        per_type[kind].append({"condId": c["id"], "question": c["question"], "subject": e["subject"],
                                               "predicate": e["predicate"], "target": e["target"],
                                               "computed": sorted(comp)})
                    parts = rel.split("/")  # model/setting/tag/idx/samples/xxxxx.png
                    base = {"docId": doc_id, "model": model, "setting": setting, "category": category, "tag": tag,
                            "idx": parts[3], "sample": parts[5], "prompt": d["prompt"], "imagePath": str(img),
                            "sourcePath": rel, "autoCorrect": bool(d["correct"]), "autoReason": d.get("reason", ""),
                            "metadata": meta}
                    for stratum, (cat, kind) in STRATA.items():
                        if cat == category and per_type[kind]:
                            pools[stratum].append({**base, "stratum": stratum, "candidates": per_type[kind]})
    if n_mismatch:
        print(f"  [warn] {n_mismatch} passed edges skipped: edge/condition order mismatch")
    return pools


def quotas(total: int, keys: list[str], capacity: dict[str, int]) -> dict[str, int]:
    """Equal split with largest remainder; shortfalls are redistributed to keys with spare capacity."""
    q = {k: 0 for k in keys}
    left, open_keys = total, [k for k in keys if capacity[k] > 0]
    while left > 0 and open_keys:
        share, extra = divmod(left, len(open_keys))
        order = sorted(open_keys)
        for j, k in enumerate(order):
            want = share + (1 if j < extra else 0)
            take = min(want, capacity[k] - q[k])
            q[k] += take
            left -= take
        open_keys = [k for k in open_keys if q[k] < capacity[k]]
    return q


def select(pools, n_per_stratum, rng):
    used_prompts: set[tuple] = set()
    used_docs: set[str] = set()
    chosen = []
    for stratum, n in n_per_stratum.items():
        pool = pools.get(stratum, [])
        by_model = defaultdict(lambda: defaultdict(list))
        for it in pool:
            by_model[it["model"]][it["setting"]].append(it)
        for m in by_model.values():
            for lst in m.values():
                rng.shuffle(lst)
        cap = {m: sum(len(v) for v in by_model[m].values()) for m in MODELS}
        q = quotas(n, MODELS, cap)
        for model in MODELS:
            settings = list(by_model[model].keys())
            rng.shuffle(settings)
            got, si = 0, 0
            while got < q[model] and any(by_model[model][s] for s in settings):
                s = settings[si % len(settings)]; si += 1
                lst = by_model[model][s]
                while lst:
                    it = lst.pop()
                    pkey = (it["setting"], it["tag"], it["idx"])
                    if pkey in used_prompts or it["docId"] in used_docs:
                        continue
                    stmt = rng.choice(it["candidates"])
                    chosen.append({**{k: v for k, v in it.items() if k != "candidates"}, "statement": stmt})
                    used_prompts.add(pkey); used_docs.add(it["docId"]); got += 1
                    break
            if got < q[model]:
                print(f"  [short] {stratum} {model}: {got}/{q[model]} (pool exhausted after prompt de-dup)")
    rng.shuffle(chosen)  # upload order carries no stratum information
    return chosen


def summarize(pools, chosen):
    print("\nPool sizes (eligible images, excluding the 600 and earlier uploads):")
    for s, p in pools.items():
        print(f"  {s:15s} {len(p):6d}   by model: " + ", ".join(f"{m}={c}" for m, c in sorted(Counter(i['model'] for i in p).items())))
    print(f"\nSelected {len(chosen)} images")
    for s in STRATA:
        sel = [c for c in chosen if c["stratum"] == s]
        print(f"\n  {s}: {len(sel)}")
        print("    model:   " + ", ".join(f"{m}={c}" for m, c in sorted(Counter(i['model'] for i in sel).items())))
        print("    setting: " + ", ".join(f"{m}={c}" for m, c in sorted(Counter(i['setting'] for i in sel).items())))
        print("    image-level autoCorrect: " + str(dict(Counter(i['autoCorrect'] for i in sel))))


def upload(chosen, batch):
    import firebase_admin
    from firebase_admin import credentials, firestore, storage
    from select_and_upload_datapoints import SERVICE_ACCOUNT_FILE, STORAGE_BUCKET, STORAGE_PREFIX

    firebase_admin.initialize_app(credentials.Certificate(str(SERVICE_ACCOUNT_FILE)), {"storageBucket": STORAGE_BUCKET})
    db, bucket = firestore.client(), storage.bucket()
    existing = {d.id for d in db.collection("datapoints").select([]).stream()}
    todo = [c for c in chosen if c["docId"] not in existing]
    print(f"{len(chosen) - len(todo)} already in Firestore, uploading {len(todo)}")
    with open(UPLOAD_MANIFEST, "a") as man:
        for c in todo:
            blob = bucket.blob(f"{STORAGE_PREFIX}/{c['docId']}.png")
            blob.upload_from_filename(c["imagePath"], content_type="image/png")
            blob.make_public()
            meta = c["metadata"]
            db.collection("datapoints").document(c["docId"]).set({
                "prompt": meta["prompt"], "imageUrl": blob.public_url,
                "objects": build_objects(meta), "conditions": build_conditions(meta),
                "conditionQuestionVersion": 2, "tag": meta.get("tag"), "category": meta.get("category"),
                "model": c["model"], "posNeg": c["setting"], "sourcePath": c["sourcePath"],
                "autoCorrect": c["autoCorrect"], "autoReason": c["autoReason"],
                "promptGroupId": f"{c['setting']}-{c['tag']}-{c['idx']}",
                "reservedAnnotationSlots": 0, "batch": batch,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            man.write(json.dumps({"docId": c["docId"], "model": c["model"], "category": c["category"],
                                  "posNeg": c["setting"], "tag": c["tag"], "idx": c["idx"], "sample": c["sample"],
                                  "prompt": c["prompt"], "autoCorrect": c["autoCorrect"], "batch": batch,
                                  "storagePath": f"{STORAGE_PREFIX}/{c['docId']}.png",
                                  "sourcePath": c["sourcePath"]}) + "\n")
            print("  uploaded", c["docId"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--n-depth-deadzone", type=int, default=150)
    ap.add_argument("--n-dir-deadzone", type=int, default=50)
    ap.add_argument("--n-depth-earned", type=int, default=50)
    ap.add_argument("--batch", default="e4b_deadzone_2026-11")
    ap.add_argument("--from-manifest", type=Path, help="re-use an existing selection instead of sampling")
    ap.add_argument("--upload", action="store_true", help="actually upload to Firebase (default: dry run)")
    a = ap.parse_args()

    if a.from_manifest:
        chosen = [json.loads(l) for l in open(a.from_manifest)]
        print(f"Loaded {len(chosen)} selected images from {a.from_manifest}")
    else:
        rng = random.Random(a.seed)
        pools = scan(excluded_ids())
        chosen = select(pools, {"depth_deadzone": a.n_depth_deadzone, "dir_deadzone": a.n_dir_deadzone,
                                "depth_earned": a.n_depth_earned}, rng)
        summarize(pools, chosen)
        out = MANIFEST_DIR / f"e4b_selection_seed{a.seed}.jsonl"
        with open(out, "w") as f:
            for c in chosen:
                f.write(json.dumps(c) + "\n")
        print(f"\nSelection manifest (LOCAL ONLY, contains strata -> keep away from annotators): {out}")

    if a.upload:
        upload(chosen, a.batch)
    else:
        print("Dry run: nothing uploaded. Re-run with --from-manifest <file> --upload to upload this exact selection.")


if __name__ == "__main__":
    main()
