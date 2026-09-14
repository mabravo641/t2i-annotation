"""Release reservation slots held by assignments that were opened and abandoned.

`reservedAnnotationSlots` counts assigned *and* completed work, and nothing in
the site ever moves an assignment out of `assigned`. An annotator who opened a
datapoint and closed the tab therefore holds that slot forever: the datapoint
can never reach its 3-annotator target, and it keeps occupying a priority tier
ahead of untouched datapoints. This sweep expires those stale rows and gives the
slot back.

No annotation is ever deleted, and no completed work is ever expired:

  * only rows whose status is exactly `assigned` are considered -- `completed`
    rows are never touched;
  * every candidate is checked against the `annotations` collection, and an
    assignment whose (annotatorId, datapointId) pair has an annotation is
    skipped and reported, because that pair represents submitted work whose
    status was merely never updated;
  * the check is repeated inside the transaction, so an annotation submitted
    between the scan and the write also blocks the expiry;
  * rows are marked `expired`, never deleted, so the audit trail survives.

Expiring is safe for the annotator too: `getAnnotatorSeenDatapointIds` in
js/assignment.js keys off assignments of any status, so an expired row still
prevents that annotator from being handed the same datapoint again.

Dry-run is the default. Nothing is written until you pass --apply.

    python src/expire_stale_assignments.py                 # show the plan
    python src/expire_stale_assignments.py --apply         # release the slots
    python src/expire_stale_assignments.py --hours 72      # be more patient
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_FILE = (
    REPO_ROOT / "firebase" / "neg-gen-firebase-adminsdk-fbsvc-caafa15513.json"
)

DEFAULT_STALE_HOURS = 48

# Why an `assigned` row was left alone, in output terms.
REASON_TEXT = {
    "still_fresh": "assigned within the window, annotator may still be working",
    "assigned_but_annotated": "annotation already submitted, slot genuinely used",
    "undated": "no assignedAt, age unknown; pass --include-undated to expire",
    "malformed": "no annotatorId/datapointId",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours", type=float, default=DEFAULT_STALE_HOURS,
        help=f"an assignment is stale this many hours after assignedAt "
             f"(default {DEFAULT_STALE_HOURS})",
    )
    parser.add_argument(
        "--include-undated", action="store_true",
        help="also expire assigned rows that have no assignedAt at all. Off by "
             "default: without a timestamp there is no evidence the row is stale.",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def annotated_pairs(db) -> set[tuple[str, str]]:
    """(annotatorId, datapointId) for every annotation that exists.

    This is the guard that makes the sweep non-destructive: any pair in here has
    submitted work, whatever its assignment row says.
    """
    pairs = set()
    for annotation in db.collection("annotations").stream():
        data = annotation.to_dict() or {}
        annotator_id, datapoint_id = data.get("annotatorId"), data.get("datapointId")
        if annotator_id and datapoint_id:
            pairs.add((annotator_id, datapoint_id))
    return pairs


def main():
    args = parse_args()
    if args.hours < 0:
        raise SystemExit("--hours must not be negative.")

    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_FILE))
    db = firestore.client()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    submitted = annotated_pairs(db)
    print(f"Annotations on record: {len(submitted)} (annotator, datapoint) pairs")
    print(f"Stale cutoff: assignedAt before {cutoff:%Y-%m-%d %H:%M} UTC\n")

    stale = []
    by_status = Counter()   # the status field as stored in Firestore
    skipped = Counter()     # why an `assigned` row was left alone
    for assignment in db.collection("assignments").stream():
        data = assignment.to_dict() or {}
        status = data.get("status")
        by_status[status] += 1
        if status != "assigned":
            continue  # completed and already-expired rows are never touched

        annotator_id = data.get("annotatorId")
        datapoint_id = data.get("datapointId")
        if not (annotator_id and datapoint_id):
            skipped["malformed"] += 1
            print(f"  SKIP malformed (no annotator/datapoint): {assignment.id}")
            continue

        # Submitted work whose status was never advanced. Expiring this would
        # release a slot that is genuinely used, letting a fourth annotator in.
        if (annotator_id, datapoint_id) in submitted:
            skipped["assigned_but_annotated"] += 1
            print(f"  SKIP has annotation (status stale, work intact): {assignment.id}")
            continue

        assigned_at = data.get("assignedAt")
        if assigned_at is None:
            if not args.include_undated:
                skipped["undated"] += 1
                continue
        elif assigned_at > cutoff:
            skipped["still_fresh"] += 1
            continue

        stale.append((assignment.reference, annotator_id, datapoint_id, assigned_at))

    # docIds are <model>-<posNeg>-<category>-<idx>-samples-<n> and model names
    # contain hyphens (hidream-4bit, blip3o-next), so split on the posNeg field
    # rather than on the first hyphen.
    by_model = defaultdict(int)
    for _ref, _annotator, datapoint_id, _at in stale:
        by_model[datapoint_id.split("-pos")[0]] += 1

    # Two separate tallies. by_status partitions every assignment row and sums to
    # the collection size; skipped explains what happened to the `assigned` ones
    # and is a breakdown *within* that status, not another slice of the total.
    total = sum(by_status.values())
    print(f"\nAssignment rows: {total}")
    for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {status}")
    print(f"\nOf the {by_status.get('assigned', 0)} still `assigned`:")
    print(f"  {len(stale):5d}  stale -- to expire")
    for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  kept ({REASON_TEXT.get(reason, reason)})")
    for model, n in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"         {n:5d} stale on {model}")

    if not stale:
        print("\nNothing to release.")
        return
    if not args.apply:
        print("\nDry run only. Re-run with --apply to release these slots.")
        return

    released = 0
    blocked = 0
    for reference, annotator_id, datapoint_id, _assigned_at in stale:
        datapoint_ref = db.collection("datapoints").document(datapoint_id)
        transaction = db.transaction()

        @firestore.transactional
        def expire(txn, reference=reference, datapoint_ref=datapoint_ref,
                   annotator_id=annotator_id, datapoint_id=datapoint_id):
            assignment_snap = reference.get(transaction=txn)
            if not assignment_snap.exists:
                return False
            current = (assignment_snap.to_dict() or {}).get("status")
            # Re-check under the transaction: the annotator may have submitted
            # between the scan and now, in which case the slot is legitimately
            # taken and must stay taken.
            if current != "assigned":
                return False
            fresh = list(
                db.collection("annotations")
                .where(filter=firestore.FieldFilter("annotatorId", "==", annotator_id))
                .where(filter=firestore.FieldFilter("datapointId", "==", datapoint_id))
                .limit(1)
                .stream()
            )
            if fresh:
                return False

            datapoint_snap = datapoint_ref.get(transaction=txn)
            if not datapoint_snap.exists:
                raise RuntimeError(f"Missing datapoint for assignment: {datapoint_id}")
            count = (datapoint_snap.to_dict() or {}).get("reservedAnnotationSlots")
            if not isinstance(count, int) or count < 1:
                raise RuntimeError(f"Invalid reservation count for {datapoint_id}: {count}")

            txn.update(datapoint_ref, {"reservedAnnotationSlots": count - 1})
            txn.update(reference, {
                "status": "expired",
                "expiredAt": firestore.SERVER_TIMESTAMP,
            })
            return True

        if expire(transaction):
            released += 1
        else:
            blocked += 1

    print(f"\nReleased {released} slots; {blocked} left alone (work landed first).")
    print("No annotations were read for deletion and none were removed.")


if __name__ == "__main__":
    main()
