"""Migrate condition questions and historical answers to positive predicates.

Old negative questions asked, for example, "Is the bench not wooden?". The new
schema asks "Does the bench appear wooden?" and stores `expected = false`.
Consequently, an old boolean response to a negative question must be inverted.

Dry-run is the default. Pass --apply after reviewing the counts. Version fields
make the migration idempotent.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from datapoint_fields import build_conditions

REPO_ROOT = Path(__file__).resolve().parents[2]
OMAR_ROOT = REPO_ROOT / "geneval_data"
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
QUESTION_VERSION = 2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the migration")
    return parser.parse_args()


def load_metadata(source_path):
    image_path = OMAR_ROOT / source_path
    metadata_path = image_path.parent.parent / "metadata.jsonl"
    if not metadata_path.is_file():
        return None
    with open(metadata_path) as metadata_file:
        line = metadata_file.readline().strip()
    return json.loads(line) if line else None


def main():
    args = parse_args()
    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_FILE))
    db = firestore.client()

    datapoints = list(db.collection("datapoints").stream())
    annotations = list(db.collection("annotations").stream())
    annotations_by_datapoint = defaultdict(list)
    for annotation in annotations:
        data = annotation.to_dict() or {}
        annotations_by_datapoint[data.get("datapointId")].append(annotation)

    writes = []
    migrated_datapoints = 0
    migrated_annotations = 0
    inverted_answers = 0
    missing_metadata = []

    for datapoint in datapoints:
        data = datapoint.to_dict() or {}
        if data.get("conditionQuestionVersion", 1) >= QUESTION_VERSION:
            continue
        metadata = load_metadata(data.get("sourcePath", ""))
        if metadata is None:
            missing_metadata.append(datapoint.id)
            continue

        old_conditions = data.get("conditions") or []
        negative_ids = {
            condition.get("id")
            for condition in old_conditions
            if condition.get("polarity") == "neg"
            or condition.get("expected") is False
        }
        writes.append((datapoint.reference, {
            "conditions": build_conditions(metadata),
            "conditionQuestionVersion": QUESTION_VERSION,
        }))
        migrated_datapoints += 1

        for annotation in annotations_by_datapoint.get(datapoint.id, []):
            annotation_data = annotation.to_dict() or {}
            if annotation_data.get("conditionResponseVersion", 1) >= QUESTION_VERSION:
                continue
            converted = []
            for response in annotation_data.get("conditionResponses", []) or []:
                converted_response = dict(response)
                if (
                    response.get("conditionId") in negative_ids
                    and isinstance(response.get("response"), bool)
                ):
                    converted_response["response"] = not response["response"]
                    inverted_answers += 1
                converted.append(converted_response)
            writes.append((annotation.reference, {
                "conditionResponses": converted,
                "conditionResponseVersion": QUESTION_VERSION,
            }))
            migrated_annotations += 1

    print(f"Datapoints to migrate: {migrated_datapoints}")
    print(f"Annotations to migrate: {migrated_annotations}")
    print(f"Negative-condition answers to invert: {inverted_answers}")
    print(f"Datapoints missing source metadata: {len(missing_metadata)}")
    for datapoint_id in missing_metadata:
        print(f"  MISSING METADATA: {datapoint_id}")

    if not args.apply:
        print("Dry run only. Re-run with --apply to migrate Firestore.")
        return

    for offset in range(0, len(writes), 400):
        batch = db.batch()
        for reference, fields in writes[offset:offset + 400]:
            batch.update(reference, fields)
        batch.commit()
    print(f"Applied {len(writes)} Firestore document updates.")


if __name__ == "__main__":
    main()
