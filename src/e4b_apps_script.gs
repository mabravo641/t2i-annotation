/**
 * Rebuttal E4b: answer collector for the static annotation site (t2i-annotation/e4b/).
 *
 * Setup (once, ~5 min):
 *  1. Create a new Google Sheet. Extensions > Apps Script. Replace Code.gs with this file.
 *  2. Deploy > New deployment > type "Web app".
 *       Execute as: Me.   Who has access: Anyone.
 *  3. Copy the web-app URL (ends in /exec) into t2i-annotation/e4b/js/config.js (SUBMIT_URL).
 *  4. Optional: Project Settings > Script properties > add TOKENS = comma-separated list of the
 *     tokens in samples/manifests/e4b_annotator_links.csv. Answers from other tokens are rejected.
 *
 * One row per submitted image; a re-submission adds a new row (the collector keeps the latest).
 * Export: File > Download > CSV, then run src/collect_e4b_annotations.py on it.
 */
const SHEET_NAME = "answers";
const HEADER = ["receivedAt", "token", "itemId", "objectResponses", "conditionResponses",
                "startedAt", "submittedAt", "durationMs", "clientVersion"];

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(HEADER);
  }
  return sh;
}

function tokenAllowed_(token) {
  if (!/^[a-z0-9]{8,}$/.test(token || "")) return false;
  const list = PropertiesService.getScriptProperties().getProperty("TOKENS");
  return !list || list.split(",").map((s) => s.trim()).indexOf(token) >= 0;
}

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (!tokenAllowed_(d.token) || !/^e4b-[0-9a-f]{6}$/.test(d.itemId || "")) {
    return ContentService.createTextOutput("rejected");
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    sheet_().appendRow([new Date(), d.token, d.itemId, JSON.stringify(d.objectResponses),
                        JSON.stringify(d.conditionResponses), d.startedAt, d.submittedAt,
                        d.durationMs, d.clientVersion || ""]);
  } finally {
    lock.releaseLock();
  }
  return ContentService.createTextOutput("ok");
}

/** GET ?token=... -> {"done": [itemIds]}; lets an annotator resume on another device. */
function doGet(e) {
  const token = (e.parameter || {}).token;
  let done = [];
  if (tokenAllowed_(token)) {
    const values = sheet_().getDataRange().getValues();
    done = values.slice(1).filter((r) => r[1] === token).map((r) => r[2]);
  }
  return ContentService.createTextOutput(JSON.stringify({ done: done }))
    .setMimeType(ContentService.MimeType.JSON);
}
