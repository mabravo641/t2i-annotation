"""Remove a duplicate annotator identity without losing the canonical history.

Dry-run is the default. The duplicate's annotations must overlap datapoints
already completed by the canonical identity; otherwise cleanup refuses to run.
Reserved slots are released transactionally with assignment deletion.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", required=True)
    parser.add_argument("--duplicate", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def documents_for(db, collection_name, annotator_id):
    return list(
        db.collection(collection_name)
        .where(filter=firestore.FieldFilter("annotatorId", "==", annotator_id))
        .stream()
    )


def main():
    args = parse_args()
    if args.canonical == args.duplicate:
        raise SystemExit("Canonical and duplicate annotator IDs must differ.")

    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_FILE))
    db = firestore.client()
    canonical_ref = db.collection("annotators").document(args.canonical)
    duplicate_ref = db.collection("annotators").document(args.duplicate)
    canonical_snap = canonical_ref.get()
    duplicate_snap = duplicate_ref.get()
    if not canonical_snap.exists:
        raise SystemExit(f"Canonical annotator does not exist: {args.canonical}")
    if not duplicate_snap.exists:
        print("Duplicate annotator is already absent; nothing to do.")
        return

    canonical_annotations = documents_for(db, "annotations", args.canonical)
    duplicate_annotations = documents_for(db, "annotations", args.duplicate)
    duplicate_assignments = documents_for(db, "assignments", args.duplicate)
    canonical_datapoints = {
        (doc.to_dict() or {}).get("datapointId") for doc in canonical_annotations
    }
    duplicate_annotation_datapoints = {
        (doc.to_dict() or {}).get("datapointId") for doc in duplicate_annotations
    }
    nonoverlap = sorted(duplicate_annotation_datapoints - canonical_datapoints)

    assignment_rows = []
    for assignment in duplicate_assignments:
        data = assignment.to_dict() or {}
        if data.get("status") not in {"assigned", "completed"}:
            raise SystemExit(
                f"Refusing unexpected assignment status {data.get('status')!r}: "
                f"{assignment.id}"
            )
        assignment_rows.append((assignment, data.get("datapointId"), data.get("status")))

    print(f"Canonical annotations retained: {len(canonical_annotations)}")
    print(f"Duplicate annotations to delete: {len(duplicate_annotations)}")
    print(f"Duplicate assignments to delete/release: {len(assignment_rows)}")
    print(f"Duplicate annotations without canonical coverage: {len(nonoverlap)}")
    for assignment, datapoint_id, status in assignment_rows:
        print(f"  {status}: {assignment.id} -> {datapoint_id}")

    if nonoverlap:
        for datapoint_id in nonoverlap:
            print(f"  NONOVERLAP: {datapoint_id}")
        raise SystemExit("Refusing cleanup: canonical identity would lose completed work.")
    if not args.apply:
        print("Dry run only. Re-run with --apply to clean the duplicate identity.")
        return

    for assignment, datapoint_id, _status in assignment_rows:
        if not datapoint_id:
            raise SystemExit(f"Assignment has no datapointId: {assignment.id}")
        datapoint_ref = db.collection("datapoints").document(datapoint_id)
        transaction = db.transaction()

        @firestore.transactional
        def release_slot(txn):
            assignment_snap = assignment.reference.get(transaction=txn)
            if not assignment_snap.exists:
                return
            datapoint_snap = datapoint_ref.get(transaction=txn)
            if not datapoint_snap.exists:
                raise RuntimeError(f"Missing datapoint for assignment: {datapoint_id}")
            count = (datapoint_snap.to_dict() or {}).get("reservedAnnotationSlots")
            if not isinstance(count, int) or count < 1:
                raise RuntimeError(f"Invalid reservation count for {datapoint_id}: {count}")
            txn.update(datapoint_ref, {"reservedAnnotationSlots": count - 1})
            txn.delete(assignment.reference)

        release_slot(transaction)

    batch = db.batch()
    for annotation in duplicate_annotations:
        batch.delete(annotation.reference)
    batch.delete(duplicate_ref)
    batch.commit()
    print("Duplicate annotations, assignments, reservations, and identity removed.")


if __name__ == "__main__":
    main()
