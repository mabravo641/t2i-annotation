import {
  collection,
  doc,
  getDoc,
  getDocs,
  query,
  runTransaction,
  serverTimestamp,
  where,
} from "https://www.gstatic.com/firebasejs/10.13.2/firebase-firestore.js";

import { db } from "./firebaseClient.js";
import { state } from "./state.js";
import { APP_CONFIG } from "./config.js";

async function getAnnotatorSeenDatapointIds() {
  const seen = new Set();

  const assignmentSnap = await getDocs(
    query(collection(db, "assignments"), where("annotatorId", "==", state.annotator.annotatorId))
  );
  assignmentSnap.forEach((docSnap) => {
    const datapointId = docSnap.data().datapointId;
    if (datapointId) seen.add(datapointId);
  });

  const annotationSnap = await getDocs(
    query(collection(db, "annotations"), where("annotatorId", "==", state.annotator.annotatorId))
  );
  annotationSnap.forEach((docSnap) => {
    const datapointId = docSnap.data().datapointId;
    if (datapointId) seen.add(datapointId);
  });

  return seen;
}

/* Every datapoint this annotator hasn't touched yet and that hasn't already
   reserved three annotation slots. The reservation count includes assigned and
   completed work, so simultaneous clients cannot all target the same last slot. */
export async function getEligibleCandidates() {
  const seenDatapoints = await getAnnotatorSeenDatapointIds();
  const datapointsSnapshot = await getDocs(collection(db, "datapoints"));
  const candidates = [];
  const target = APP_CONFIG.targetAnnotationsPerDatapoint || 3;

  for (const datapointDoc of datapointsSnapshot.docs) {
    if (seenDatapoints.has(datapointDoc.id)) continue;
    const reservedCount = datapointDoc.data().reservedAnnotationSlots;
    // Refuse to assign uninitialized datapoints. Treating a missing counter as
    // zero would violate the cap when historical assignments already exist.
    if (!Number.isInteger(reservedCount)) {
      console.warn(`Datapoint ${datapointDoc.id} has no reservedAnnotationSlots; run the backfill.`);
      continue;
    }
    if (reservedCount >= target) continue;
    candidates.push({
      datapointId: datapointDoc.id,
      reservedCount,
    });
  }

  return candidates;
}

export async function getOrCreateAssignment(candidates) {
  const openAssignmentQuery = query(
    collection(db, "assignments"),
    where("annotatorId", "==", state.annotator.annotatorId),
    where("status", "==", "assigned")
  );
  const openAssignments = await getDocs(openAssignmentQuery);
  if (!openAssignments.empty) {
    const first = openAssignments.docs[0];
    return { assignmentId: first.id, ...first.data() };
  }

  if (!candidates.length) return null;

  // Randomize within each reservation-count tier while retaining most-covered
  // first priority: datapoints closer to the 3-annotator target are assigned
  // before brand-new (0-annotation) ones, so partially-done items reach a
  // complete, human-ceiling-eligible set sooner instead of spreading coverage
  // thin across the full pool. If concurrent clients fill a tier, continue to
  // the next.
  const pool = [];
  const counts = [...new Set(candidates.map((candidate) => candidate.reservedCount))].sort(
    (a, b) => b - a
  );
  for (const count of counts) {
    const tier = candidates.filter((candidate) => candidate.reservedCount === count);
    while (tier.length) {
      pool.push(tier.splice(Math.floor(Math.random() * tier.length), 1)[0]);
    }
  }
  const target = APP_CONFIG.targetAnnotationsPerDatapoint || 3;
  for (const selected of pool) {
    // Deterministic per annotator+datapoint ID prevents two tabs/devices for the
    // same annotator from consuming two slots in a concurrent race.
    const assignmentId = `${selected.datapointId}__${state.annotator.annotatorId}`;
    const assignmentRef = doc(db, "assignments", assignmentId);
    const datapointRef = doc(db, "datapoints", selected.datapointId);
    const assignment = await runTransaction(db, async (transaction) => {
      const datapointSnap = await transaction.get(datapointRef);
      const assignmentSnap = await transaction.get(assignmentRef);
      if (assignmentSnap.exists()) {
        const prior = assignmentSnap.data();
        return prior.status === "assigned"
          ? { assignmentId: assignmentRef.id, ...prior }
          : null;
      }
      if (!datapointSnap.exists()) return null;
      const reservedCount = datapointSnap.data().reservedAnnotationSlots;
      if (!Number.isInteger(reservedCount)) {
        throw new Error(`Datapoint ${selected.datapointId} is not assignment-counter initialized.`);
      }
      if (reservedCount >= target) return null;

      transaction.update(datapointRef, {
        reservedAnnotationSlots: reservedCount + 1,
      });
      transaction.set(assignmentRef, {
        annotatorId: state.annotator.annotatorId,
        datapointId: selected.datapointId,
        status: "assigned",
        assignedAt: serverTimestamp(),
        completedAt: null,
      });
      return {
        assignmentId: assignmentRef.id,
        datapointId: selected.datapointId,
        status: "assigned",
      };
    });
    if (assignment) return assignment;
  }

  // Every candidate in this priority tier was filled by concurrent clients.
  // The caller will refresh the pool on its next load attempt.
  return null;
}

export async function loadDatapoint(datapointId) {
  const snap = await getDoc(doc(db, "datapoints", datapointId));
  if (!snap.exists()) throw new Error(`Datapoint not found: ${datapointId}`);
  return { id: snap.id, ...snap.data() };
}

export async function refreshCompletedCount() {
  const completedSnap = await getDocs(
    query(
      collection(db, "assignments"),
      where("annotatorId", "==", state.annotator.annotatorId),
      where("status", "==", "completed")
    )
  );
  state.completedCount = completedSnap.size;
}
