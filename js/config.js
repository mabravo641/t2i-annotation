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
  // Which end of the coverage range to serve first.
  //   "least-covered" -- 0-annotation datapoints before partially-done ones.
  //     Maximises how many datapoints (and models) have any human label at all.
  //   "most-covered"  -- the original: 2 before 1 before 0, so partially-done
  //     items reach the 3-annotator target sooner. Maximises the number of
  //     ceiling-eligible datapoints, at the cost of starving newly uploaded
  //     batches for as long as any older datapoint is still short of target.
  assignmentPriority: "least-covered",
};

export const LOCAL_STORAGE_ANNOTATOR_KEY = "neggeneval_annotator_id";

export function hasPlaceholderFirebaseConfig() {
  return Object.values(APP_CONFIG.firebase || {}).some((value) =>
    String(value || "").startsWith("REPLACE_WITH_")
  );
}
