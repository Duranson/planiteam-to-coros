# Deploying the Gmail -> intervals.icu automation

Two pieces, both cloud-hosted (nothing depends on any local machine being on):

- **Apps Script** (`apps_script/Code.gs`): runs on a time trigger inside Google's
  infrastructure, watches Gmail for new Planiteam emails, extracts the PDF +
  session date, and POSTs them to the Cloud Function below. No OAuth setup
  needed - Apps Script gets native Gmail access via its own built-in
  authorization.
- **Cloud Function** (`main.py`, repo root): receives that POST, runs the
  existing `parse_planiteam_pdf`/`push_event` unchanged, pushes the event to
  intervals.icu.

Why split it this way, and why polling instead of Gmail push notifications:
see the chat history / commit message - short version is that Apps Script's
native Gmail access avoids an OAuth refresh-token dance entirely, and a
30-minute poll is more than fast enough for a coach's weekly upload, avoiding
Pub/Sub setup and the 7-day `watch()` renewal chore that real-time push would
need.

## 0. What you need before starting

- A GCP project with billing enabled (required for Cloud Functions Gen2,
  even within the free tier).
- `gcloud` CLI, authenticated to that project - either install it locally, or
  use **Cloud Shell** in the GCP Console (browser-based, `gcloud` preinstalled,
  no local install needed): https://console.cloud.google.com -> the `>_` icon
  top right.
- Your `INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID` (already in your
  local `.env`).

Fill these in for yourself before running anything below:

```
PROJECT_ID=<your GCP project id>
REGION=europe-west1          # or your preferred region
```

## 1. Store secrets in Secret Manager

Never put real credentials in `--set-env-vars` (those are visible in the
Cloud Console/`gcloud functions describe` output in plain text) - use
`--set-secrets` instead, which references Secret Manager.

```bash
gcloud config set project "$PROJECT_ID"
gcloud services enable secretmanager.googleapis.com cloudfunctions.googleapis.com cloudbuild.googleapis.com run.googleapis.com

printf '%s' 'PASTE_YOUR_INTERVALS_ICU_API_KEY' | gcloud secrets create INTERVALS_ICU_API_KEY --data-file=-
printf '%s' 'PASTE_YOUR_INTERVALS_ICU_ATHLETE_ID' | gcloud secrets create INTERVALS_ICU_ATHLETE_ID --data-file=-
printf '%s' '17.5' | gcloud secrets create VMA --data-file=-

# Shared secret that authenticates Apps Script's requests to this function.
# Generate your own - never commit the actual value to git, even to a
# private repo (visibility can change, and it stays in history forever).
# Kept as a shell variable (not a file) so step 3's smoke test can reuse it -
# it only lives in this Cloud Shell session, gone when the session ends.
SHARED_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
printf '%s' "$SHARED_SECRET" | gcloud secrets create SHARED_SECRET --data-file=-
```

(If a secret already exists from a previous attempt, use
`gcloud secrets versions add NAME --data-file=-` instead of `create`.)

## 2. Deploy the Cloud Function

Run this from the repo root (`.gcloudignore` already excludes `.env`,
`.venv/`, `tests/`, `example/`, etc. from the upload):

```bash
gcloud functions deploy planiteam-to-coros \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=handle_planiteam_email \
  --trigger-http \
  --allow-unauthenticated \
  --set-secrets="INTERVALS_ICU_API_KEY=INTERVALS_ICU_API_KEY:latest,INTERVALS_ICU_ATHLETE_ID=INTERVALS_ICU_ATHLETE_ID:latest,VMA=VMA:latest,SHARED_SECRET=SHARED_SECRET:latest"
```

`--allow-unauthenticated` is intentional: Apps Script can't easily attach a
Google-signed IAM identity token, so the function is public but gated by the
`X-Shared-Secret` header check in `main.py` instead - anyone without that
secret gets a 401, and the worst a leaked secret enables is someone pushing
garbage workouts to your own intervals.icu account, not any data exposure.

Capture the URL (also shown as `httpsTrigger.url` in the deploy output) -
you'll need it again for Apps Script's `CLOUD_FUNCTION_URL` property in step 4:

```bash
FUNCTION_URL=$(gcloud functions describe planiteam-to-coros --gen2 --region="$REGION" --format='value(serviceConfig.uri)')
echo "$FUNCTION_URL"
```

## 3. Smoke-test the deployed function

```bash
python3 -c "
import base64, json
print(json.dumps({'date': '2026-09-17', 'pdf_base64': base64.b64encode(open('example/cotes-courtes-2x6x20-.pdf','rb').read()).decode()}))
" > /tmp/probe_payload.json

curl -s -X POST "$FUNCTION_URL" \
  -H "X-Shared-Secret: $SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d @/tmp/probe_payload.json
```

Expect `{"status": "ok", "title": "...", "event_id": ...}`. Delete the test
event from intervals.icu afterwards (calendar view, or
`DELETE /api/v1/athlete/{id}/events/{eventId}` the same way earlier testing
in this repo did).

## 4. Set up Apps Script

1. Go to https://script.google.com -> New project.
2. Replace the default `Code.gs` content with this repo's
   `apps_script/Code.gs`.
3. Project Settings (gear icon) -> Script Properties -> add:
   - `CLOUD_FUNCTION_URL` = the URL from step 2
   - `SHARED_SECRET` = the same value stored in Secret Manager above (if
     your Cloud Shell session ended and you lost the `$SHARED_SECRET`
     variable, retrieve it again with
     `gcloud secrets versions access latest --secret=SHARED_SECRET`)
4. Run `checkForNewPlaniteamEmails` once manually from the editor (Run button)
   to trigger Gmail's OAuth consent screen - approve it (it's your own
   script asking for your own Gmail access, first-party, no external review
   needed since it's unpublished/private to you).
5. Triggers (clock icon, left sidebar) -> Add Trigger:
   - Function: `checkForNewPlaniteamEmails`
   - Event source: Time-driven
   - Type: Minutes timer -> Every 30 minutes

## 5. End-to-end test

Find a real Planiteam notification email in Gmail (or wait for the next
one), mark it unread, and either wait for the next 30-minute trigger or run
`checkForNewPlaniteamEmails` manually from the Apps Script editor. Check:

- The email gets marked read on success, or labelled `PlaniTeam-Sync-Error`
  on failure (check Apps Script's Executions log, left sidebar, for why).
- The workout shows up on your intervals.icu calendar on the right date,
  then syncs to COROS the same way every manual push in this repo already
  has.

## Failure behaviour

A message that fails to parse or push gets labelled `PlaniTeam-Sync-Error`
and left unread - it won't be retried automatically (the search query
excludes that label), so it stays visible in the inbox until you notice,
investigate via the Execution log, and remove the label to let it retry.
This is deliberate: a PDF whose layout has changed enough to break the
parser should fail loudly, not silently push wrong data or retry forever.
