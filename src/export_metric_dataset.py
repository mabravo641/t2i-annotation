"""Build a self-contained metric-evaluation dataset from Firebase and local exports.

The output contains one row per Firestore datapoint, points at the portable
image backup, preserves the exact condition questions stored in Firestore, and
attaches the available human answers and majority labels.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore


REPO_ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
DEFAULT_ANNOTATIONS = ANNOTATION_ROOT / "samples/annotations/annotations.jsonl"
DEFAULT_IMAGE_MANIFEST = ANNOTATION_ROOT / "samples/images/images_manifest.jsonl"
DEFAULT_OUTPUT = ANNOTATION_ROOT / "samples/metric_evaluation/input.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def majority(values: list[bool]) -> int | None:
    if not values:
        return None
    return int(sum(values) / len(values) > 0.5)


def build_elements(datapoint: dict, annotations: list[dict]) -> list[dict]:
    conditions = datapoint.get("conditions") or []
    condition_object_ids = {
        str(condition.get("id", ""))[4:]
        for condition in conditions
        if str(condition.get("id", "")).startswith("obj_")
    }
    elements = []

    for index, name in enumerate(datapoint.get("objects") or []):
        name = str(name).strip()
        if name in condition_object_ids:
            continue
        answers = [
            response["response"]
            for annotation in annotations
            for response in (annotation.get("objectResponses") or [])
            if response.get("object") == name and isinstance(response.get("response"), bool)
        ]
        label = majority(answers)
        elements.append({
            "element_id": f"object:{index}:{name}",
            "element_type": "object",
            "polarity": "affirmative",
            "question": f"Is there exactly one {name} in the image?",
            "source_id": name,
            "expected": True,
            "answers": ["Y" if value else "N" for value in answers],
            "human_element_label": label,
            "human_satisfied_label": label,
            "active": True,
        })

    object_majorities = {
        element["source_id"]: bool(element["human_element_label"])
        for element in elements
        if element["human_element_label"] is not None
    }
    for index, condition in enumerate(conditions):
        condition_id = condition.get("id") or str(index)
        answers = [
            response["response"]
            for annotation in annotations
            for response in (annotation.get("conditionResponses") or [])
            if response.get("conditionId") == condition_id
            and isinstance(response.get("response"), bool)
        ]
        label = majority(answers)
        required = [condition.get("subject")]
        if condition.get("target"):
            required.append(condition["target"])
        active = all(object_majorities.get(name, True) for name in required if name)
        expected = bool(condition.get("expected", True))
        satisfied = None if label is None else int(bool(label) == expected)
        category = condition.get("category")
        element_type = category if category in {"color", "material"} else condition.get("type", "condition")
        if element_type == "attribute":
            element_type = category or "attribute"
        elements.append({
            "element_id": f"condition:{condition_id}",
            "element_type": element_type,
            "polarity": "affirmative" if expected else "negated",
            "question": condition.get("question", ""),
            "source_id": condition_id,
            "expected": expected,
            "answers": ["Y" if value else "N" for value in answers],
            "human_element_label": label,
            "human_satisfied_label": satisfied,
            "active": active,
            "requires": [name for name in required if name],
        })
    return elements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--image-manifest", type=Path, default=DEFAULT_IMAGE_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    annotations_by_datapoint = defaultdict(list)
    for annotation in read_jsonl(args.annotations):
        annotations_by_datapoint[annotation["datapointId"]].append(annotation)
    images = {row["datapointId"]: row for row in read_jsonl(args.image_manifest)}

    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_FILE))
    db = firestore.client()
    rows = []
    for document in db.collection("datapoints").stream():
        datapoint = document.to_dict() or {}
        image = images.get(document.id)
        if image is None:
            raise ValueError(f"No downloaded image manifest entry for {document.id}")
        annotations = annotations_by_datapoint.get(document.id, [])
        elements = build_elements(datapoint, annotations)
        satisfied = [
            element["human_satisfied_label"]
            for element in elements
            if element["human_satisfied_label"] is not None
        ]
        rows.append({
            "image_id": document.id,
            "docId": document.id,
            "datapointId": document.id,
            "image_path": image["localPath"],
            "prompt": datapoint.get("prompt"),
            "setup": datapoint.get("posNeg"),
            "posNeg": datapoint.get("posNeg"),
            "category": datapoint.get("category"),
            "tag": datapoint.get("tag"),
            "model": datapoint.get("model"),
            "promptGroupId": datapoint.get("promptGroupId"),
            "sourcePath": datapoint.get("sourcePath"),
            "autoCorrect": datapoint.get("autoCorrect"),
            "autoReason": datapoint.get("autoReason"),
            "n_annotators": len(annotations),
            "human_score": sum(satisfied) / len(satisfied) if satisfied else None,
            "human_label": int(all(satisfied)) if satisfied else None,
            "elements": elements,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda item: item["image_id"]):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(args.output)
    print(f"Wrote {args.output} ({len(rows)} datapoints)")


if __name__ == "__main__":
    main()
