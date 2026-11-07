"""Rebuttal E4b: collect answers from the static site and compute the E4b numbers.

Inputs (any mix):
  --sheet-csv   CSV downloaded from the Google Sheet (File > Download > CSV)
  --json        files from the site's "Download my answers" button (fallback)

For every (token, item) the latest submission wins. Output, in the same shape as
neggeneval/output/human_eval/human_scores.jsonl (raw answers per annotator), plus
the E4b fields (stratum, audited statement):

  samples/annotations/e4b_human_annotations.jsonl   (git-ignored)

Then prints, per stratum, how often the human majority confirms that the denied
relation is ABSENT, on active statements only (majority says both objects are
present exactly once), with Wilson 95% CIs. That is the E4b confirmation rate.

    .venv/bin/python t2i-annotation/src/collect_e4b_annotations.py --sheet-csv answers.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

ANN_ROOT = Path(__file__).resolve().parents[1]
KEY = ANN_ROOT / "samples" / "manifests" / "e4b_key.jsonl"
LINKS = ANN_ROOT / "samples" / "manifests" / "e4b_annotator_links.csv"
OUT = ANN_ROOT / "samples" / "annotations" / "e4b_human_annotations.jsonl"


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def majority(votes):
    return None if not votes else sum(votes) > len(votes) / 2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet-csv", type=Path, nargs="*", default=[])
    ap.add_argument("--json", type=Path, nargs="*", default=[])
    a = ap.parse_args()

    key = {k["id"]: k for k in map(json.loads, open(KEY))}
    slot = {r["token"]: r["slot"] for r in csv.DictReader(open(LINKS))}

    latest = {}
    def add(rec):
        if rec["itemId"] not in key or rec["token"] not in slot:
            return
        k = (rec["token"], rec["itemId"])
        if k not in latest or rec["submittedAt"] >= latest[k]["submittedAt"]:
            latest[k] = rec
    for p in a.sheet_csv:
        for r in csv.DictReader(open(p)):
            add({"token": r["token"], "itemId": r["itemId"], "submittedAt": r["submittedAt"],
                 "objectResponses": json.loads(r["objectResponses"]),
                 "conditionResponses": json.loads(r["conditionResponses"]),
                 "durationMs": int(float(r["durationMs"] or 0))})
    for p in a.json:
        d = json.load(open(p))
        for r in d["answers"]:
            if "objectResponses" in r:
                add({**r, "token": r.get("token", d["token"])})

    per_item = defaultdict(list)
    for (tok, iid), r in latest.items():
        per_item[iid].append({"annotatorId": slot[tok], "objectResponses": r["objectResponses"],
                              "conditionResponses": r["conditionResponses"], "durationMs": r["durationMs"],
                              "submittedAt": r["submittedAt"]})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stats = defaultdict(lambda: {"n": 0, "confirmed": 0, "inactive": 0, "tie": 0})
    with open(OUT, "w") as f:
        for iid, anns in sorted(per_item.items()):
            k = key[iid]
            f.write(json.dumps({"docId": k["docId"], "datapointId": k["docId"], "e4bId": iid, "prompt": k["prompt"],
                                "model": k["model"], "posNeg": k["setting"], "category": k["category"],
                                "tag": k["tag"], "stratum": k["stratum"], "statement": k["statement"],
                                "n_annotators": len(anns), "annotators": anns}) + "\n")
            if len(anns) < 3:
                continue
            st = stats[k["stratum"]]
            stmt = k["statement"]
            obj_ok = all(majority([o["response"] for an in anns for o in an["objectResponses"] if o["object"] == ob])
                         for ob in (stmt["subject"], stmt["target"]))
            if not obj_ok:
                st["inactive"] += 1
                continue
            votes = [c["response"] for an in anns for c in an["conditionResponses"] if c["conditionId"] == stmt["condId"]]
            st["n"] += 1
            st["confirmed"] += int(majority(votes) is False)  # relation judged absent -> the pass is right

    print(f"{len(latest)} answers, {len(per_item)} images, "
          f"{sum(len(v) >= 3 for v in per_item.values())} with 3 annotators -> {OUT}")
    print("\nE4b: humans confirm the denied relation is absent (active statements, 3-annotator majority)")
    for s, st in sorted(stats.items()):
        lo, hi = wilson(st["confirmed"], st["n"])
        rate = st["confirmed"] / st["n"] if st["n"] else float("nan")
        print(f"  {s:15s} {st['confirmed']:3d}/{st['n']:3d} = {rate:5.1%}  [{lo:.0%}, {hi:.0%}]"
              f"   (inactive: {st['inactive']})")


if __name__ == "__main__":
    main()
