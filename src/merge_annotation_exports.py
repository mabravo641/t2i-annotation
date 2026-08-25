"""Merge old/new annotation exports into deduplicated version-2 JSONL.

Duplicate annotation IDs are one annotation, not additional votes. A version-2
copy is preferred. If an annotation exists only in version 1, responses to
negative conditions are inverted using the source prompt metadata.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from datapoint_fields import build_conditions

REPO_ROOT = Path(__file__).resolve().parents[2]
OMAR_ROOT = REPO_ROOT / "omar_data"
DEFAULT_ANNOTATION_FILES = [
    REPO_ROOT / "t2i-annotation/samples/annotations/annotations.jsonl",
    REPO_ROOT / "t2i-annotation/samples/annotations/pre_positive_question_migration_2026-08-24/annotations.jsonl",
]
DEFAULT_MANIFEST_FILES = [
    REPO_ROOT / "t2i-annotation/samples/manifests/upload_manifest.jsonl",
]
DEFAULT_OUT_DIR = REPO_ROOT / "t2i-annotation/samples/annotations_merged"
VERSION = 2


def read_jsonl(path):
    with open(path) as jsonl_file:
        return [json.loads(line) for line in jsonl_file if line.strip()]


def negative_condition_ids(datapoint_id, manifests):
    manifest = manifests.get(datapoint_id)
    if manifest is None:
        raise ValueError(f"No manifest entry for datapoint {datapoint_id}")
    image_path = OMAR_ROOT / manifest["sourcePath"]
    metadata_path = image_path.parent.parent / "metadata.jsonl"
    with open(metadata_path) as metadata_file:
        metadata = json.loads(metadata_file.readline())
    return {
        condition["id"]
        for condition in build_conditions(metadata)
        if condition["expected"] is False
    }


def convert_to_v2(record, manifests):
    converted = dict(record)
    if converted.get("conditionResponseVersion", 1) >= VERSION:
        return converted, 0
    negative_ids = negative_condition_ids(converted["datapointId"], manifests)
    responses = []
    inversions = 0
    for response in converted.get("conditionResponses", []) or []:
        item = dict(response)
        if (
            item.get("conditionId") in negative_ids
            and isinstance(item.get("response"), bool)
        ):
            item["response"] = not item["response"]
            inversions += 1
        responses.append(item)
    converted["conditionResponses"] = responses
    converted["conditionResponseVersion"] = VERSION
    return converted, inversions


def response_signature(record):
    return {
        "annotatorId": record.get("annotatorId"),
        "datapointId": record.get("datapointId"),
        "objectResponses": record.get("objectResponses") or [],
        "conditionResponses": record.get("conditionResponses") or [],
        "objectFailure": record.get("objectFailure"),
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", nargs="+", type=Path,
                        default=DEFAULT_ANNOTATION_FILES)
    parser.add_argument("--manifests", nargs="+", type=Path,
                        default=DEFAULT_MANIFEST_FILES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser.parse_args()


def main():
    args = parse_args()

    manifests = {}
    manifest_duplicates = 0
    for path in args.manifests:
        for row in read_jsonl(path):
            doc_id = row["docId"]
            if doc_id in manifests:
                manifest_duplicates += 1
                if manifests[doc_id] != row:
                    raise ValueError(f"Conflicting manifest entries for {doc_id}")
            else:
                manifests[doc_id] = row

    grouped = defaultdict(list)
    input_rows = 0
    for path in args.annotations:
        for record in read_jsonl(path):
            grouped[record["annotationId"]].append(record)
            input_rows += 1

    merged = []
    converted_old_only = 0
    inverted_answers = 0
    duplicate_rows = 0
    for annotation_id, copies in grouped.items():
        converted_copies = []
        for copy in copies:
            converted, inversions = convert_to_v2(copy, manifests)
            converted_copies.append(converted)
            inverted_answers += inversions
        signatures = {json.dumps(response_signature(copy), sort_keys=True)
                      for copy in converted_copies}
        if len(signatures) != 1:
            raise ValueError(f"Conflicting responses for annotation {annotation_id}")
        v2_original = next(
            (copy for copy in copies if copy.get("conditionResponseVersion") == VERSION),
            None,
        )
        if v2_original is None:
            converted_old_only += 1
            chosen = converted_copies[0]
        else:
            chosen = dict(v2_original)
        chosen["conditionResponseVersion"] = VERSION
        merged.append(chosen)
        duplicate_rows += len(copies) - 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    annotation_path = args.out_dir / "annotations.jsonl"
    manifest_path = args.out_dir / "upload_manifest.jsonl"
    with open(annotation_path, "w") as output:
        for record in sorted(merged, key=lambda row: row["annotationId"]):
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(manifest_path, "w") as output:
        for record in sorted(manifests.values(), key=lambda row: row["docId"]):
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Annotation input rows: {input_rows}")
    print(f"Unique annotations: {len(merged)}")
    print(f"Duplicate annotation rows removed: {duplicate_rows}")
    print(f"Old-only annotations converted: {converted_old_only}")
    print(f"Old-copy answers inverted for validation: {inverted_answers}")
    print(f"Unique manifest datapoints: {len(manifests)}")
    print(f"Duplicate manifest rows removed: {manifest_duplicates}")
    print(f"Wrote {annotation_path}")
    print(f"Wrote {manifest_path}")


if __name__ == "__main__":
    main()
