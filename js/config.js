export const APP_CONFIG = {
  firebase: {
    apiKey: "AIzaSyD3Ut1Z5eoDXBwGK8yqdqP3zRd7TGP5Wvc",
    authDomain: "neg-gen.firebaseapp.com",
    projectId: "neg-gen",
    storageBucket: "neg-gen.firebasestorage.app",
    messagingSenderId: "1057258679978",
    appId: "1:1057258679978:web:29acc54cdc3ec269ee2ad3",
    measurementId: "G-YJLQZ793E6",
  },
  targetAnnotationsPerDatapoint: 3,
};

export const LOCAL_STORAGE_ANNOTATOR_KEY = "neggeneval_annotator_id";

export function hasPlaceholderFirebaseConfig() {
  return Object.values(APP_CONFIG.firebase || {}).some((value) =>
    String(value || "").startsWith("REPLACE_WITH_")
  );
}
