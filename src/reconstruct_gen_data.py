"""Reconstruct geneval_data evaluator-tree folders for annotated datapoints that
don't have one yet.

compare_human_metrics.py needs geneval_data/<model>/<posNeg>/<tag>/<idx>/metadata.jsonl
to build each datapoint's object/condition list (via datapoint_fields.py). Newer
datapoints downloaded through download_firebase_images.py never got one, so they
get silently skipped ("no metadata found") even though their annotations are real.

Nothing is actually missing, just not yet reconstructed:
  - The prompt metadata for (posNeg, tag, idx) already exists as line `idx` of
    neggeneval/prompts/neg/<posNeg>/<tag>.jsonl -- verified byte-for-byte
    equivalent (minus num_pos/num_neg, which the existing reconstructed copies
    never kept either) to the metadata.jsonl of an existing datapoint.
  - The image never needs downloading again: every existing geneval_data image is
    itself a symlink back to t2i-annotation/samples/images/<docId>.png, which we
    already have for every annotated datapoint.

Usage:
    .venv/bin/python t2i-annotation/src/reconstruct_gen_data.py --dry-run
    .venv/bin/python t2i-annotation/src/reconstruct_gen_data.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = REPO_ROOT / "neggeneval" / "prompts" / "neg"
OMAR_ROOT = REPO_ROOT / "geneval_data"
IMAGES_ROOT = ANNOTATION_ROOT / "samples" / "images"
DEFAULT_ANNOTATIONS = ANNOTATION_ROOT / "samples/annotations/annotations.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def split_datapoint_id(datapoint_id: str, model: str, pos_neg: str, tag: str) -> tuple[str, str]:
    prefix = f"{model}-{pos_neg}-{tag}-"
    if not datapoint_id.startswith(prefix):
        raise ValueError(f"docId {datapoint_id!r} does not start with {prefix!r}")
    rest = datapoint_id[len(prefix):]
    idx, _, sample = rest.partition("-samples-")
    if not sample:
        raise ValueError(f"docId {datapoint_id!r} has no -samples- suffix")
    return idx, sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing")
    args = parser.parse_args()

    # One representative row per datapoint: model/posNeg/tag/prompt are the
    # same across every annotator of the same datapoint.
    rows_by_dp: dict[str, dict] = {}
    for row in read_jsonl(args.annotations):
        rows_by_dp.setdefault(row["datapointId"], row)

    prompt_cache: dict[tuple[str, str], list[dict] | None] = {}
    created = already_present = 0
    failed: list[tuple[str, str]] = []

    for dp_id, row in sorted(rows_by_dp.items()):
        model, pos_neg, tag = row["model"], row["posNeg"], row["tag"]
        try:
            idx, sample = split_datapoint_id(dp_id, model, pos_neg, tag)
        except ValueError as exc:
            failed.append((dp_id, str(exc)))
            continue

        dp_dir = OMAR_ROOT / model / pos_neg / tag / idx
        metadata_path = dp_dir / "metadata.jsonl"
        link_path = dp_dir / "samples" / f"{sample}.png"
        image_path = IMAGES_ROOT / f"{dp_id}.png"

        did_something = False

        if not metadata_path.is_file():
            cache_key = (pos_neg, tag)
            if cache_key not in prompt_cache:
                prompt_file = PROMPTS_ROOT / pos_neg / f"{tag}.jsonl"
                prompt_cache[cache_key] = read_jsonl(prompt_file) if prompt_file.is_file() else None
            prompt_lines = prompt_cache[cache_key]
            if prompt_lines is None:
                failed.append((dp_id, f"no prompt file for {pos_neg}/{tag}"))
                continue
            try:
                record = prompt_lines[int(idx)]
            except (ValueError, IndexError):
                failed.append((dp_id, f"idx {idx} out of range for {pos_neg}/{tag} ({len(prompt_lines)} lines)"))
                continue
            if not args.dry_run:
                dp_dir.mkdir(parents=True, exist_ok=True)
                metadata = {
                    "tag": record["tag"], "prompt": record["prompt"], "category": record["category"],
                    "include": record["include"], "slots": record["slots"],
                }
                temporary = metadata_path.with_suffix(metadata_path.suffix + ".part")
                temporary.write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")
                temporary.replace(metadata_path)
            did_something = True

        if not link_path.exists():
            if not image_path.is_file():
                failed.append((dp_id, f"image not found: {image_path}"))
                continue
            if not args.dry_run:
                link_path.parent.mkdir(parents=True, exist_ok=True)
                link_path.symlink_to(image_path.resolve())
            did_something = True

        if did_something:
            created += 1
        else:
            already_present += 1

    verb = "Would touch" if args.dry_run else "Touched"
    print(f"Already present: {already_present}")
    print(f"{verb}: {created}")
    if failed:
        print(f"Failed: {len(failed)}")
        for dp_id, reason in failed[:20]:
            print(f"  {dp_id}: {reason}")


if __name__ == "__main__":
    main()
