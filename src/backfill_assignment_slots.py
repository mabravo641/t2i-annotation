"""Initialize strict per-datapoint assignment reservation counters.

Dry-run is the default. Pass --apply only after reviewing the summary.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)
TARGET = 3


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write counters (default: audit only)"
    )
    parser.add_argument(
        "--delete-orphans", action="store_true",
        help="Delete assignments whose datapoint no longer exists",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    annotators_by_datapoint = defaultdict(set)
    assignment_refs_by_datapoint = defaultdict(list)
    assignment_total = 0
    for assignment in db.collection("assignments").stream():
        data = assignment.to_dict() or {}
        if data.get("status") not in {"assigned", "completed"}:
            continue
        datapoint_id = data.get("datapointId")
        annotator_id = data.get("annotatorId")
        if datapoint_id and annotator_id:
            annotators_by_datapoint[datapoint_id].add(annotator_id)
            assignment_refs_by_datapoint[datapoint_id].append(assignment.reference)
            assignment_total += 1

    datapoints = list(db.collection("datapoints").stream())
    datapoint_ids = {datapoint.id for datapoint in datapoints}
    over_target = []
    changed = []
    for datapoint in datapoints:
        count = len(annotators_by_datapoint.get(datapoint.id, set()))
        current = (datapoint.to_dict() or {}).get("reservedAnnotationSlots")
        if count > TARGET:
            over_target.append((datapoint.id, count))
        if current != count:
            changed.append((datapoint.reference, current, count))

    missing_datapoints = sorted(set(annotators_by_datapoint) - datapoint_ids)
    print(f"Datapoints: {len(datapoints)}")
    print(f"Active/completed assignment rows: {assignment_total}")
    print(f"Counters requiring update: {len(changed)}")
    print(f"Datapoints already over target {TARGET}: {len(over_target)}")
    for datapoint_id, count in over_target:
        print(f"  OVER TARGET: {datapoint_id} has {count} distinct annotators")
    for datapoint_id in missing_datapoints:
        print(f"  ORPHAN ASSIGNMENTS: missing datapoint {datapoint_id}")

    if args.delete_orphans and missing_datapoints:
        orphan_refs = [
            ref
            for datapoint_id in missing_datapoints
            for ref in assignment_refs_by_datapoint[datapoint_id]
        ]
        for offset in range(0, len(orphan_refs), 400):
            batch = db.batch()
            for reference in orphan_refs[offset:offset + 400]:
                batch.delete(reference)
            batch.commit()
        print(f"Deleted {len(orphan_refs)} orphan assignment rows.")

    if not args.apply:
        if args.delete_orphans:
            print("Counters were audit-only; orphan deletion was applied as requested.")
        else:
            print("Dry run only. Re-run with --apply to initialize the counters.")
        return

    # Firestore batches allow at most 500 writes; leave some margin.
    for offset in range(0, len(changed), 400):
        batch = db.batch()
        for reference, _old, count in changed[offset:offset + 400]:
            batch.update(reference, {"reservedAnnotationSlots": count})
        batch.commit()
    print(f"Updated {len(changed)} datapoint counters.")


if __name__ == "__main__":
    main()
