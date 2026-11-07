"""Rebuttal E4b: build the Firebase-free annotation site from a selection manifest.

Parallel to the Firebase workflow (select_and_upload_datapoints.py, js/, index.html),
which is left untouched. Everything this script writes for the public site goes to
`t2i-annotation/e4b/`; everything that would unblind annotators goes to the
git-ignored `samples/manifests/`.

What it does
------------
1. Reads the selection manifest written by select_e4b_deadzone_batch.py (dry run).
2. Gives every image an opaque id (`e4b-xxxxxx`), so file names and URLs reveal
   neither model, setting, category nor stratum.
3. Copies the images as JPEG (quality 95, native resolution) to `e4b/img/`.
4. Writes `e4b/data/items.json`: prompt, objects and positive-form conditions only
   (no polarity / expected / model / evaluator fields).
5. Pre-assigns every image to 3 of the N annotator slots (default 9): no slot sees
   an image twice, loads differ by at most one, and strata are spread evenly
   over slots. Each slot gets an unguessable token and its own randomised order in
   `e4b/data/a/<token>.json`. The site is opened as `.../e4b/?a=<token>`.
6. Writes the LOCAL key files:
     samples/manifests/e4b_key.jsonl          opaque id -> docId, stratum, statement, expected
     samples/manifests/e4b_annotator_links.csv slot, token, link, number of images
                                              (fill in who gets which slot; one person per slot)

Usage
-----
    .venv/bin/python t2i-annotation/src/build_e4b_static_site.py \\
        --manifest t2i-annotation/samples/manifests/e4b_selection_seed20261107.jsonl \\
        --base-url https://mabravo641.github.io/t2i-annotation/e4b/

Re-running with the same --seed gives the same ids, tokens and assignment.
Use --force to overwrite an existing build.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import string
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

from datapoint_fields import build_conditions, build_objects

ANN_ROOT = Path(__file__).resolve().parents[1]  # t2i-annotation/
SITE_DIR = ANN_ROOT / "e4b"
MANIFEST_DIR = ANN_ROOT / "samples" / "manifests"
PUBLIC_COND_FIELDS = ("id", "type", "category", "subject", "predicate", "target")


def token(rng: random.Random, n: int = 12) -> str:
    return "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def assign(items: list[dict], n_slots: int, per_item: int, rng: random.Random) -> dict[int, list[str]]:
    """Each item -> `per_item` distinct slots. Greedy on current load (ties random),
    items visited stratum by stratum so every slot gets a similar stratum mix."""
    load = Counter({s: 0 for s in range(n_slots)})
    strat_load = defaultdict(Counter)
    out: dict[int, list[str]] = defaultdict(list)
    by_stratum = defaultdict(list)
    for it in items:
        by_stratum[it["stratum"]].append(it)
    for stratum in sorted(by_stratum):
        group = by_stratum[stratum][:]
        rng.shuffle(group)
        for it in group:
            slots = sorted(range(n_slots), key=lambda s: (load[s], strat_load[stratum][s], rng.random()))[:per_item]
            for s in slots:
                out[s].append(it["id"])
                load[s] += 1
                strat_load[stratum][s] += 1
    for s in out:
        rng.shuffle(out[s])  # per-slot order: random, no stratum blocks
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--base-url", default="https://mabravo641.github.io/t2i-annotation/e4b/")
    ap.add_argument("--slots", type=int, default=9)
    ap.add_argument("--per-item", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument("--force", action="store_true", help="overwrite an existing e4b/img and e4b/data")
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.manifest) if l.strip()]
    assert len({r["docId"] for r in rows}) == len(rows), "duplicate docIds in manifest"
    rng = random.Random(a.seed)

    img_dir, data_dir = SITE_DIR / "img", SITE_DIR / "data"
    if (img_dir.exists() or data_dir.exists()) and not a.force:
        raise SystemExit(f"{SITE_DIR} already has img/ or data/; pass --force to rebuild.")
    for d in (img_dir, data_dir):
        shutil.rmtree(d, ignore_errors=True)
    (data_dir / "a").mkdir(parents=True)
    img_dir.mkdir(parents=True)

    used, items, key = set(), [], []
    for r in rows:
        while (oid := "e4b-" + "".join(rng.choice("0123456789abcdef") for _ in range(6))) in used:
            pass
        used.add(oid)
        meta = r["metadata"]
        conds = build_conditions(meta)
        stmt = r["statement"]
        match = [c for c in conds if c["id"] == stmt["condId"]]
        assert match and (match[0]["predicate"], match[0]["subject"], match[0]["target"]) == (
            stmt["predicate"], stmt["subject"], stmt["target"]), f"statement mismatch in {r['docId']}"

        with Image.open(r["imagePath"]) as im:
            im.convert("RGB").save(img_dir / f"{oid}.jpg", "JPEG", quality=a.jpeg_quality)

        items.append({"id": oid, "prompt": meta["prompt"], "image": f"img/{oid}.jpg",
                      "objects": build_objects(meta),
                      "conditions": [{k: c[k] for k in PUBLIC_COND_FIELDS if k in c} for c in conds]})
        key.append({"id": oid, "docId": r["docId"], "stratum": r["stratum"], "model": r["model"],
                    "setting": r["setting"], "category": r["category"], "tag": r["tag"], "idx": r["idx"],
                    "sample": r["sample"], "prompt": meta["prompt"], "sourcePath": r["sourcePath"],
                    "autoCorrect": r["autoCorrect"], "autoReason": r["autoReason"], "statement": stmt,
                    "objects": build_objects(meta),
                    "conditions": [{k: c[k] for k in ("id", "polarity", "expected", "question")} for c in conds]})

    rng.shuffle(items)  # file order carries no stratum information
    with open(data_dir / "items.json", "w") as f:
        json.dump(items, f, separators=(",", ":"))

    strata = {k["id"]: k["stratum"] for k in key}
    plan = assign([{"id": i["id"], "stratum": strata[i["id"]]} for i in items], a.slots, a.per_item, rng)
    links = []
    for s in range(a.slots):
        t = token(rng)
        with open(data_dir / "a" / f"{t}.json", "w") as f:
            json.dump({"items": plan[s]}, f)
        links.append({"slot": f"A{s + 1}", "person": "", "token": t, "link": f"{a.base_url}?a={t}",
                      "n_images": len(plan[s]),
                      **{st: sum(strata[i] == st for i in plan[s]) for st in sorted(set(strata.values()))}})

    with open(MANIFEST_DIR / "e4b_key.jsonl", "w") as f:
        for k in key:
            f.write(json.dumps(k) + "\n")
    with open(MANIFEST_DIR / "e4b_annotator_links.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(links[0]))
        w.writeheader()
        w.writerows(links)

    # sanity checks
    per_item = Counter(i for ids in plan.values() for i in ids)
    assert set(per_item.values()) == {a.per_item} and len(per_item) == len(items)
    assert all(len(ids) == len(set(ids)) for ids in plan.values())
    size = sum(p.stat().st_size for p in img_dir.iterdir()) / 1e6
    print(f"{len(items)} items, {size:.1f} MB of JPEGs in {img_dir}")
    for l in links:
        print(f"  {l['slot']}: {l['n_images']} images  " +
              "  ".join(f"{k}={v}" for k, v in l.items() if k not in ("slot", "person", "token", "link", "n_images")))
    print(f"Local key (do not publish): {MANIFEST_DIR / 'e4b_key.jsonl'}")
    print(f"Annotator links:            {MANIFEST_DIR / 'e4b_annotator_links.csv'}")


if __name__ == "__main__":
    main()
