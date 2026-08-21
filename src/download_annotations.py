"""Download all submitted annotations from Firestore and save them locally.

Joins each `annotations` document with its `datapoints` document (prompt,
model, category, pos/neg setting, automatic-evaluator verdict) and its
`annotators` document (nickname, email) so the export is self-contained and
readable without going back to Firestore.

Writes two files to t2i-annotation/src/annotations/ (default), overwriting
them each run so they always reflect the current contents of Firestore:

- annotations.jsonl : one JSON object per annotation, full fidelity
  (objectResponses/conditionResponses kept as nested lists).
- annotations.csv    : one row per individual object/condition response,
  flattened for quick spreadsheet review.

Usage:

    .venv/bin/python t2i-annotation/src/download_annotations.py
    .venv/bin/python t2i-annotation/src/download_annotations.py --out-dir some/other/dir
"""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "annotations"

CSV_FIELDS = [
    "annotationId", "annotatorId", "annotatorNickname", "annotatorEmail",
    "datapointId", "assignmentId", "model", "category", "posNeg", "tag",
    "autoCorrect", "prompt", "responseKind", "sourceId", "response",
    "objectFailure", "startedAt", "submittedAt", "durationMs",
]


def to_jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def fetch_lookup(db, collection, fields):
    lookup = {}
    for doc in db.collection(collection).stream():
        data = doc.to_dict()
        lookup[doc.id] = {field: data.get(field) for field in fields}
    return lookup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    datapoints = fetch_lookup(
        db, "datapoints",
        ["prompt", "model", "category", "posNeg", "tag", "autoCorrect"],
    )
    annotators = fetch_lookup(db, "annotators", ["nickname", "email"])

    jsonl_path = args.out_dir / "annotations.jsonl"
    csv_path = args.out_dir / "annotations.csv"

    enriched = []
    csv_rows = []

    for doc in db.collection("annotations").stream():
        annotation = doc.to_dict()
        datapoint = datapoints.get(annotation.get("datapointId"), {})
        annotator = annotators.get(annotation.get("annotatorId"), {})

        record = {
            "annotationId": doc.id,
            **{k: to_jsonable(v) for k, v in annotation.items()},
            "annotatorNickname": annotator.get("nickname"),
            "annotatorEmail": annotator.get("email"),
            "prompt": datapoint.get("prompt"),
            "model": datapoint.get("model"),
            "category": datapoint.get("category"),
            "posNeg": datapoint.get("posNeg"),
            "tag": datapoint.get("tag"),
            "autoCorrect": datapoint.get("autoCorrect"),
        }
        enriched.append(record)

        base_row = {
            "annotationId": doc.id,
            "annotatorId": annotation.get("annotatorId"),
            "annotatorNickname": annotator.get("nickname"),
            "annotatorEmail": annotator.get("email"),
            "datapointId": annotation.get("datapointId"),
            "assignmentId": annotation.get("assignmentId"),
            "model": datapoint.get("model"),
            "category": datapoint.get("category"),
            "posNeg": datapoint.get("posNeg"),
            "tag": datapoint.get("tag"),
            "autoCorrect": datapoint.get("autoCorrect"),
            "prompt": datapoint.get("prompt"),
            "objectFailure": annotation.get("objectFailure"),
            "startedAt": to_jsonable(annotation.get("startedAt")),
            "submittedAt": to_jsonable(annotation.get("submittedAt")),
            "durationMs": annotation.get("durationMs"),
        }

        for resp in annotation.get("objectResponses", []) or []:
            csv_rows.append({
                **base_row, "responseKind": "object",
                "sourceId": resp.get("object"), "response": resp.get("response"),
            })
        for resp in annotation.get("conditionResponses", []) or []:
            csv_rows.append({
                **base_row, "responseKind": "condition",
                "sourceId": resp.get("conditionId"), "response": resp.get("response"),
            })

    with open(jsonl_path, "w") as f:
        for record in enriched:
            f.write(json.dumps(record) + "\n")

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"Downloaded {len(enriched)} annotations ({len(csv_rows)} responses).")
    print(f"Wrote {jsonl_path}")
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    main()
