"""Cloud Function entry point: receives a Planiteam PDF + session date from
the Apps Script Gmail watcher and pushes it, per club member, to intervals.icu.

This is the receiving end of the two-part cloud automation described in
DEPLOYMENT.md:

    Gmail (new Planiteam email)
      -> Apps Script (time-driven trigger, extracts PDF + date, no PDF
         parsing - just Gmail access and an HTTPS POST)
      -> this Cloud Function (does the actual PDF parsing via
         planiteam_to_coros.parse_planiteam_pdf, then push_event, once per
         recipient in RECIPIENTS_JSON)

Credentials (RECIPIENTS_JSON, SHARED_SECRET) are bound as plain environment
variables at deploy time via `gcloud functions deploy --set-secrets=...`
(Secret Manager) - never read from a .env file or baked into the deployed
source. RECIPIENTS_JSON holds the whole club's list (name/vma/api_key/
athlete_id per member, see planiteam_to_coros.Recipient) as a single secret,
so adding or removing someone only changes that secret's value, not the
deploy command.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import tempfile
from pathlib import Path

import functions_framework
from flask import Request, jsonify

from planiteam_to_coros import parse_recipients, push_to_recipients

DEFAULT_SPORT = "Run"


@functions_framework.http
def handle_planiteam_email(request: Request):
    shared_secret = os.environ.get("SHARED_SECRET")
    if not shared_secret or request.headers.get("X-Shared-Secret") != shared_secret:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    pdf_b64 = payload.get("pdf_base64")
    date = payload.get("date")
    if not pdf_b64 or not date:
        return jsonify({"error": "pdf_base64 and date are required"}), 400

    try:
        recipients = parse_recipients(os.environ["RECIPIENTS_JSON"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": f"missing/invalid server config: {exc}"}), 500

    enabled = [r for r in recipients if r.enabled]
    if not enabled:
        return jsonify({"error": "RECIPIENTS_JSON has no enabled recipients"}), 500

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = Path(tmp_dir) / "workout.pdf"
        pdf_path.write_bytes(base64.b64decode(pdf_b64))

        try:
            results = push_to_recipients(
                pdf_path,
                date=date,
                recipients=recipients,
                sport=payload.get("sport", DEFAULT_SPORT),
            )
        except Exception as exc:  # boundary: failure outside the per-recipient loop (e.g. bad temp file)
            return jsonify({"error": f"failed to process workout: {exc}"}), 502

    # Logged per recipient (by name, per their request) so a broken account
    # is identifiable from the Cloud Function's logs without blocking
    # everyone else's push - see CLAUDE.md on isolating per-recipient failures.
    for result in results:
        if result.ok:
            print(f"Pushed to {result.name}: event id {result.event_id}")
        else:
            print(f"FAILED to push to {result.name}: {result.error}")

    title = next((r.title for r in results if r.title), None)
    response_body = {
        "status": "ok" if any(r.ok for r in results) else "error",
        "title": title,
        "results": [dataclasses.asdict(r) for r in results],
    }
    # A total failure (nobody got pushed) is treated as a failed request so
    # Apps Script labels the email for retry instead of marking it read; a
    # partial failure (some recipients pushed, others didn't) still counts
    # as success overall - it's already visible in the logs above.
    status_code = 200 if any(r.ok for r in results) else 502
    return jsonify(response_body), status_code
