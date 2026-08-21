import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.2/firebase-app.js";
import { getFirestore } from "https://www.gstatic.com/firebasejs/10.13.2/firebase-firestore.js";

import { APP_CONFIG } from "./config.js";

export let db = null;

export function initFirebase() {
  const app = initializeApp(APP_CONFIG.firebase);
  db = getFirestore(app);
  return db;
}
