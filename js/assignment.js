import {
  addDoc,
  collection,
  doc,
  getDoc,
  getDocs,
  query,
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

async function getCompletedAssignmentCountsMap() {
  const completedSnap = await getDocs(
    query(collection(db, "assignments"), where("status", "==", "completed"))
  );

  const perDatapointAnnotators = new Map();
  completedSnap.forEach((docSnap) => {
    const { annotatorId, datapointId } = docSnap.data();
    if (!annotatorId || !datapointId) return;

    if (!perDatapointAnnotators.has(datapointId)) {
      perDatapointAnnotators.set(datapointId, new Set());
    }
    perDatapointAnnotators.get(datapointId).add(annotatorId);
  });

  const counts = new Map();
  perDatapointAnnotators.forEach((annotators, datapointId) => {
    counts.set(datapointId, annotators.size);
  });
  return counts;
}

/* Every datapoint this annotator hasn't touched yet and that hasn't already
   reached its target annotation count. Used both to pick the next assignment
   and, via its length, as the "how many are left for me" progress count. */
export async function getEligibleCandidates() {
  const seenDatapoints = await getAnnotatorSeenDatapointIds();
  const completedCountsByDatapoint = await getCompletedAssignmentCountsMap();
  const datapointsSnapshot = await getDocs(collection(db, "datapoints"));
  const candidates = [];

  for (const datapointDoc of datapointsSnapshot.docs) {
    if (seenDatapoints.has(datapointDoc.id)) continue;
    const completedCount = completedCountsByDatapoint.get(datapointDoc.id) || 0;
    candidates.push({
      datapointId: datapointDoc.id,
      completedCount,
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

  const minCount = Math.min(...candidates.map((candidate) => candidate.completedCount));
  const prioritized = candidates.filter(
    (candidate) => candidate.completedCount === minCount
  );
  const underTarget = prioritized.filter(
    (candidate) => candidate.completedCount < (APP_CONFIG.targetAnnotationsPerDatapoint || 3)
  );

  const pool = underTarget.length ? underTarget : prioritized;
  const selected = pool[Math.floor(Math.random() * pool.length)];

  // TODO: replace this block with a Firestore transaction when moving to production multi-user scale.
  // Keep assignment creation isolated so this can be upgraded to a Firestore transaction safely.
  // Without a transaction, simultaneous clients can over-assign the same datapoint.
  const assignmentRef = await addDoc(collection(db, "assignments"), {
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
