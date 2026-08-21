import {
  collection,
  doc,
  getDoc,
  getDocs,
  query,
  serverTimestamp,
  setDoc,
  updateDoc,
  where,
} from "https://www.gstatic.com/firebasejs/10.13.2/firebase-firestore.js";

import { db } from "./firebaseClient.js";
import { dom } from "./dom.js";
import { state } from "./state.js";
import { showStatus } from "./status.js";
import { LOCAL_STORAGE_ANNOTATOR_KEY } from "./config.js";

const adjectives = [
  "quiet", "brisk", "bright", "steady", "calm", "rapid",
  "mellow", "gentle", "clear", "nimble", "bold", "kind",
];
const animals = [
  "otter", "lynx", "sparrow", "badger", "heron", "fox",
  "seal", "falcon", "wolf", "beaver", "rabbit", "dolphin",
];

function generateNickname() {
  const adjective = adjectives[Math.floor(Math.random() * adjectives.length)];
  const animal = animals[Math.floor(Math.random() * animals.length)];
  const number = Math.floor(1000 + Math.random() * 9000);
  return `${adjective}-${animal}-${number}`;
}

function uuid() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `annotator-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
}

/* Reused by both first-time setup and "change email", so entering an email
   that's already registered always reconnects to that same annotator record
   (nickname + history) instead of spawning a new one per browser/device. */
async function findAnnotatorByEmail(email) {
  const snap = await getDocs(query(collection(db, "annotators"), where("email", "==", email)));
  if (snap.empty) return null;
  const found = snap.docs[0];
  return { annotatorId: found.id, ...found.data() };
}

export function promptForEmail(initialEmail = "") {
  return new Promise((resolve) => {
    const form = dom.annotatorForm;

    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      dom.cancelAnnotatorBtn.removeEventListener("click", onCancel);
      dom.annotatorDialog.removeEventListener("cancel", onCancel);
    };

    const onSubmit = (event) => {
      event.preventDefault();
      if (!dom.emailInput.checkValidity()) {
        dom.emailInput.reportValidity();
        return;
      }
      const email = dom.emailInput.value.trim().toLowerCase();
      cleanup();
      dom.annotatorDialog.close();
      resolve(email);
    };

    const onCancel = () => {
      cleanup();
      dom.annotatorDialog.close();
      resolve("");
    };

    form.addEventListener("submit", onSubmit);
    dom.cancelAnnotatorBtn.addEventListener("click", onCancel);
    dom.annotatorDialog.addEventListener("cancel", onCancel);
    dom.emailInput.value = initialEmail;
    dom.annotatorDialog.showModal();
    dom.emailInput.focus();
  });
}

/* Annotator initialization: reuse the browser-local ID if it still resolves;
   otherwise collect an email and reconnect to an existing annotator with that
   email if one exists, or create a fresh one. */
export async function initializeAnnotator() {
  let annotatorId = localStorage.getItem(LOCAL_STORAGE_ANNOTATOR_KEY);

  if (annotatorId) {
    const snap = await getDoc(doc(db, "annotators", annotatorId));
    if (snap.exists()) {
      state.annotator = snap.data();
      return;
    }
    localStorage.removeItem(LOCAL_STORAGE_ANNOTATOR_KEY);
  }

  const email = await promptForEmail();
  if (!email) throw new Error("Annotator setup cancelled.");

  const existing = await findAnnotatorByEmail(email);
  if (existing) {
    state.annotator = existing;
    localStorage.setItem(LOCAL_STORAGE_ANNOTATOR_KEY, existing.annotatorId);
    return;
  }

  const nickname = generateNickname();
  annotatorId = uuid();
  const annotator = {
    annotatorId,
    email,
    nickname,
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  };

  await setDoc(doc(db, "annotators", annotatorId), annotator);
  localStorage.setItem(LOCAL_STORAGE_ANNOTATOR_KEY, annotatorId);

  state.annotator = { ...annotator, createdAt: new Date(), updatedAt: new Date() };
}

/* Returns "switched" if the entered email matched a different existing
   annotator (identity + history are adopted for this browser), "updated" if
   the current annotator's email field was changed, or null if unchanged/cancelled. */
export async function changeAnnotatorEmail() {
  const email = await promptForEmail(state.annotator.email || "");
  if (!email || email === state.annotator.email) return null;

  const existing = await findAnnotatorByEmail(email);
  if (existing && existing.annotatorId !== state.annotator.annotatorId) {
    state.annotator = existing;
    localStorage.setItem(LOCAL_STORAGE_ANNOTATOR_KEY, existing.annotatorId);
    showStatus(`Switched to existing annotator ${existing.nickname}.`);
    return "switched";
  }

  await updateDoc(doc(db, "annotators", state.annotator.annotatorId), {
    email,
    updatedAt: serverTimestamp(),
  });

  state.annotator.email = email;
  showStatus("Email updated.");
  return "updated";
}

/* onIdentitySwitch lets the caller (main.js) reload the assignment/progress
   after adopting a different annotator identity, without this module needing
   to import the assignment/render logic. */
export function setupAnnotatorControls(onIdentitySwitch) {
  dom.changeAnnotatorBtn.addEventListener("click", async () => {
    dom.changeAnnotatorBtn.disabled = true;
    try {
      const result = await changeAnnotatorEmail();
      if (result === "switched" && onIdentitySwitch) {
        await onIdentitySwitch();
      }
    } catch (error) {
      showStatus(error.message || "Email update failed.", true);
    } finally {
      dom.changeAnnotatorBtn.disabled = false;
    }
  });
}
