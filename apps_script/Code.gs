// PlaniTeam -> intervals.icu Gmail watcher.
//
// Runs on a time-driven trigger. Finds unread PlaniTeam workout
// notification emails, extracts the PDF attachment and the session date,
// and POSTs them to the Cloud Function that does the actual PDF parsing
// and intervals.icu push (see /main.py in the planiteam-to-coros repo).
// This script deliberately does no PDF parsing itself - Apps Script has no
// equivalent to pdfplumber's word-position extraction that the parser
// depends on (see CLAUDE.md), so all PDF handling stays in Python.
//
// One-time setup (see DEPLOYMENT.md for the full walkthrough):
//   1. Project Settings > Script Properties: add CLOUD_FUNCTION_URL and
//      SHARED_SECRET (the same secret configured on the Cloud Function).
//   2. Triggers > Add Trigger > checkForNewPlaniteamEmails > Time-driven >
//      Minutes timer > Every 30 minutes.

var ERROR_LABEL_NAME = 'PlaniTeam-Sync-Error';
var SENDER_ADDRESS = 'contact@planiteam.fr';

var FRENCH_MONTHS = {
  'janvier': 1, 'fevrier': 2, 'février': 2, 'mars': 3, 'avril': 4, 'mai': 5,
  'juin': 6, 'juillet': 7, 'aout': 8, 'août': 8, 'septembre': 9,
  'octobre': 10, 'novembre': 11, 'decembre': 12, 'décembre': 12
};

function checkForNewPlaniteamEmails() {
  var props = PropertiesService.getScriptProperties();
  var cloudFunctionUrl = props.getProperty('CLOUD_FUNCTION_URL');
  var sharedSecret = props.getProperty('SHARED_SECRET');
  if (!cloudFunctionUrl || !sharedSecret) {
    throw new Error('Set CLOUD_FUNCTION_URL and SHARED_SECRET in Script Properties first.');
  }

  var errorLabel = GmailApp.getUserLabelByName(ERROR_LABEL_NAME) ||
    GmailApp.createLabel(ERROR_LABEL_NAME);

  var threads = GmailApp.search(
    'from:' + SENDER_ADDRESS + ' is:unread -label:' + ERROR_LABEL_NAME
  );
  threads.forEach(function (thread) {
    thread.getMessages().forEach(function (message) {
      if (!message.isUnread()) return;
      processMessage_(message, thread, errorLabel, cloudFunctionUrl, sharedSecret);
    });
  });
}

function processMessage_(message, thread, errorLabel, cloudFunctionUrl, sharedSecret) {
  var subject = message.getSubject();
  var date = extractSessionDate_(message.getPlainBody());
  if (!date) {
    Logger.log('Could not find a session date in message: ' + subject);
    thread.addLabel(errorLabel);
    return;
  }

  var pdf = message.getAttachments().filter(function (a) {
    return a.getContentType() === 'application/pdf';
  })[0];
  if (!pdf) {
    Logger.log('No PDF attachment in message: ' + subject);
    thread.addLabel(errorLabel);
    return;
  }

  var payload = {
    date: date,
    pdf_base64: Utilities.base64Encode(pdf.getBytes())
  };

  var response = UrlFetchApp.fetch(cloudFunctionUrl, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Shared-Secret': sharedSecret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  if (response.getResponseCode() === 200) {
    message.markRead();
    Logger.log('Pushed: ' + subject + ' -> ' + response.getContentText());
  } else {
    Logger.log('Push failed (' + response.getResponseCode() + ') for ' + subject + ': ' + response.getContentText());
    thread.addLabel(errorLabel);
  }
}

// Parses "Date : jeudi 10 septembre 2026" into "2026-09-10".
// Returns null if the body doesn't match the expected pattern, so a
// format change on Planiteam's side fails loudly (labelled for review)
// instead of silently pushing a wrong date.
function extractSessionDate_(body) {
  var match = body.match(/Date\s*:\s*\S+\s+(\d{1,2})\s+(\S+)\s+(\d{4})/i);
  if (!match) return null;

  var day = match[1];
  var monthName = match[2].toLowerCase();
  var year = match[3];
  var month = FRENCH_MONTHS[monthName];
  if (!month) return null;

  return year + '-' + ('' + month).padStart(2, '0') + '-' + ('' + day).padStart(2, '0');
}
