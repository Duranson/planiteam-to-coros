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
# cloudresourcemanager is easy to miss: an *interactive* `gcloud functions
# deploy` silently offers to enable it for you on the fly ("enable this API?
# (y/N)"), so a manual first deploy can work without it ever being enabled
# explicitly - then a non-interactive one (CI) hits that same prompt with no
# terminal to answer it, and just fails instead.
gcloud services enable secretmanager.googleapis.com cloudfunctions.googleapis.com cloudbuild.googleapis.com run.googleapis.com cloudresourcemanager.googleapis.com

printf '%s' 'PASTE_YOUR_INTERVALS_ICU_API_KEY' | gcloud secrets create INTERVALS_ICU_API_KEY --data-file=-
printf '%s' 'PASTE_YOUR_INTERVALS_ICU_ATHLETE_ID' | gcloud secrets create INTERVALS_ICU_ATHLETE_ID --data-file=-
printf '%s' '17.5' | gcloud secrets create VMA --data-file=-

# Shared secret that authenticates Apps Script's requests to this function.
# Generate your own - never commit the actual value to git, even to a
# private repo (visibility can change, and it stays in history forever).
# This only needs to exist in Secret Manager, not as a shell variable - later
# steps re-fetch it with `gcloud secrets versions access` rather than relying
# on a variable surviving between commands (Cloud Shell sessions don't).
printf '%s' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" | gcloud secrets create SHARED_SECRET --data-file=-
```

(If a secret already exists from a previous attempt, use
`gcloud secrets versions add NAME --data-file=-` instead of `create`.)

## 2. Grant the function's service account access to those secrets

On newer GCP projects the default compute service account no longer gets
broad project access automatically, so it has to be given explicit
permission to read each secret - otherwise the deploy in the next step
fails with `Permission denied on secret: ... The service account used must
be granted the 'Secret Manager Secret Accessor' role`.

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for SECRET in INTERVALS_ICU_API_KEY INTERVALS_ICU_ATHLETE_ID VMA SHARED_SECRET; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 3. Deploy the Cloud Function

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
you'll need it again for Apps Script's `CLOUD_FUNCTION_URL` property in step 5:

```bash
FUNCTION_URL=$(gcloud functions describe planiteam-to-coros --gen2 --region="$REGION" --format='value(serviceConfig.uri)')
echo "$FUNCTION_URL"
```

## 4. Smoke-test the deployed function

Reads both values straight from GCP rather than assuming a shell variable
from an earlier step is still around - `FUNCTION_URL` and `SHARED_SECRET`
only exist for the life of one Cloud Shell session, and step 1 sets
`SHARED_SECRET` as a secret in Secret Manager, not as a shell variable, so
it doesn't carry over between commands unless you set it yourself first:

```bash
FUNCTION_URL=$(gcloud functions describe planiteam-to-coros --gen2 --region="$REGION" --format='value(serviceConfig.uri)')
SHARED_SECRET=$(gcloud secrets versions access latest --secret=SHARED_SECRET)

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

## 5. Set up Apps Script

1. Go to https://script.google.com -> New project.
2. Replace the default `Code.gs` content with this repo's
   `apps_script/Code.gs`.
3. Project Settings (gear icon) -> Script Properties -> add:
   - `CLOUD_FUNCTION_URL` = the URL from step 3
   - `SHARED_SECRET` = fetch the value with
     `gcloud secrets versions access latest --secret=SHARED_SECRET` and
     paste it in
4. Run `checkForNewPlaniteamEmails` once manually from the editor (Run button)
   to trigger Gmail's OAuth consent screen - approve it (it's your own
   script asking for your own Gmail access, first-party, no external review
   needed since it's unpublished/private to you).
5. Triggers (clock icon, left sidebar) -> Add Trigger:
   - Function: `checkForNewPlaniteamEmails`
   - Event source: Time-driven
   - Type: Minutes timer -> Every 30 minutes

## 6. End-to-end test

Find a real Planiteam notification email in Gmail (or wait for the next
one), mark it unread, and either wait for the next 30-minute trigger or run
`checkForNewPlaniteamEmails` manually from the Apps Script editor. Check:

- The email gets marked read on success, or labelled `PlaniTeam-Sync-Error`
  on failure (check Apps Script's Executions log, left sidebar, for why).
- The workout shows up on your intervals.icu calendar on the right date,
  then syncs to COROS the same way every manual push in this repo already
  has.

## 7. Keep the Cloud Function in sync with `main` (continuous deployment)

Steps 1-4 deploy whatever was in the working directory at that moment -
nothing about the running function watches the repo afterwards. Every code
change from here on needs a fresh `gcloud functions deploy`, or it silently
keeps running the old version. `.github/workflows/deploy.yml` automates
that: on every push to `main` that touches `main.py`, `planiteam_to_coros.py`,
`requirements.txt` or `.gcloudignore`, it runs the test suite and then the
exact same deploy command from step 3.

Authentication uses **Workload Identity Federation**: GitHub's own per-run
OIDC token is exchanged for short-lived GCP access, scoped to this one repo
- no long-lived key stored anywhere, nothing to leak or rotate. One-time
setup, run in Cloud Shell:

```bash
GITHUB_REPO="Duranson/planiteam-to-coros"   # owner/repo, exactly as on GitHub
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# A dedicated service account for the deploy step, not the default compute
# one - keeps the CI credential's permissions to exactly what it needs.
gcloud iam service-accounts create github-deployer \
  --display-name="GitHub Actions deployer for planiteam-to-coros"
DEPLOYER_SA="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# Everything a Cloud Functions Gen2 deploy actually touches under the hood:
# Cloud Run (what Gen2 functions run on), Cloud Build (compiles the source
# into a container image), Artifact Registry (stores that image), and
# permission to hand the function its own runtime service account.
for ROLE in roles/cloudfunctions.developer roles/run.developer \
            roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_SA}" \
    --role="$ROLE" \
    --condition=None
done

# A Workload Identity Pool + an OIDC provider that trusts GitHub Actions
# tokens - but only ones asserting they came from this exact repo.
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions pool"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '${GITHUB_REPO}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Let tokens from that provider/repo impersonate the deployer service
# account - and only that repo; nothing else can mint a token this trusts.
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${GITHUB_REPO}"

# The full provider resource name the GitHub Actions workflow needs.
gcloud iam workload-identity-pools providers describe "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
```

Then, in the GitHub repo -> Settings -> Secrets and variables -> Actions ->
**Variables** tab (none of these are secret - WIF is the whole point of not
needing a stored credential), add:

| Name | Value |
|---|---|
| `GCP_PROJECT_ID` | `$PROJECT_ID` |
| `GCP_REGION` | `$REGION` (e.g. `europe-west1`) |
| `GCP_DEPLOYER_SA` | `github-deployer@<project-id>.iam.gserviceaccount.com` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | the full resource name printed by the last command above |

Push to `main` and check the Actions tab - the workflow should run the
tests, then redeploy. From here on, `gcloud functions deploy` by hand
(steps 1-4) is only needed for the very first deploy or if Secret Manager
values themselves change; ordinary code changes just need a push.

## Failure behaviour

A message that fails to parse or push gets labelled `PlaniTeam-Sync-Error`
and left unread - it won't be retried automatically (the search query
excludes that label), so it stays visible in the inbox until you notice,
investigate via the Execution log, and remove the label to let it retry.
This is deliberate: a PDF whose layout has changed enough to break the
parser should fail loudly, not silently push wrong data or retry forever.
