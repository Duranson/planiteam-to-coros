"""Cloud Function entry point: receives a Planiteam PDF + session date from
the Apps Script Gmail watcher and pushes it to intervals.icu.

This is the receiving end of the two-part cloud automation described in
DEPLOYMENT.md:

    Gmail (new Planiteam email)
      -> Apps Script (time-driven trigger, extracts PDF + date, no PDF
         parsing - just Gmail access and an HTTPS POST)
      -> this Cloud Function (does the actual PDF parsing via
         planiteam_to_coros.parse_planiteam_pdf, then push_event)

Credentials (INTERVALS_ICU_API_KEY, INTERVALS_ICU_ATHLETE_ID, VMA,
SHARED_SECRET) are bound as plain environment variables at deploy time via
`gcloud functions deploy --set-secrets=...` (Secret Manager) - never read
from a .env file or baked into the deployed source.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import functions_framework
from flask import Request, jsonify

from planiteam_to_coros import parse_planiteam_pdf, push_event

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
        vma = float(os.environ.get("VMA", "17.5"))
        api_key = os.environ["INTERVALS_ICU_API_KEY"]
        athlete_id = os.environ["INTERVALS_ICU_ATHLETE_ID"]
    except KeyError as exc:
        return jsonify({"error": f"missing server config: {exc}"}), 500

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = Path(tmp_dir) / "workout.pdf"
        pdf_path.write_bytes(base64.b64decode(pdf_b64))

        try:
            workout = parse_planiteam_pdf(pdf_path, vma=vma)
        except Exception as exc:  # boundary: arbitrary PDF content from Gmail
            return jsonify({"error": f"failed to parse PDF: {exc}"}), 422

    try:
        event = push_event(
            workout,
            date=date,
            athlete_id=athlete_id,
            api_key=api_key,
            sport=payload.get("sport", DEFAULT_SPORT),
        )
    except Exception as exc:  # boundary: network call to intervals.icu
        return jsonify({"error": f"failed to push to intervals.icu: {exc}"}), 502

    return jsonify({"status": "ok", "title": workout.title, "event_id": event.get("id")}), 200
