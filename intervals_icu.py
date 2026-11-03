"""intervals.icu client: push a parsed Workout as a planned calendar event,
once per club member, each with their own VMA.

Auth is HTTP Basic with the literal username ``API_KEY``. Every push creates
a *new* event: intervals.icu ignores client-supplied uids, so re-pushing the
same session duplicates it (see CLAUDE.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence

import requests

if TYPE_CHECKING:
    from planiteam_to_coros import Workout

BASE_URL = "https://intervals.icu"


def build_event_payload(workout: Workout, *, vma: float, date: str) -> dict:
    """EventEx body for the create-event endpoint; ``date`` is local YYYY-MM-DD."""

    return {
        "start_date_local": f"{date}T00:00:00",
        "category": "WORKOUT",
        "type": "Run",
        "name": workout.title,
        "description": workout.to_intervals_icu_text(vma),
    }


def push_event(workout: Workout, *, vma: float, date: str, athlete_id: str, api_key: str, base_url: str = BASE_URL) -> dict:
    response = requests.post(
        f"{base_url}/api/v1/athlete/{athlete_id}/events",
        json=build_event_payload(workout, vma=vma, date=date),
        auth=("API_KEY", api_key),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


@dataclass
class Recipient:
    """One club member. ``enabled`` pauses someone without losing their config."""

    name: str
    vma: float
    api_key: str
    athlete_id: str
    enabled: bool = True


def parse_recipients(raw: str) -> List[Recipient]:
    """Parse the ``RECIPIENTS_JSON`` list. Raises ``ValueError`` on malformed
    JSON or a missing field, so a typo never silently drops someone."""

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"RECIPIENTS_JSON is not valid JSON: {exc}") from exc

    recipients = []
    for i, entry in enumerate(entries):
        try:
            recipients.append(
                Recipient(
                    name=entry["name"],
                    vma=float(entry["vma"]),
                    api_key=entry["api_key"],
                    athlete_id=str(entry["athlete_id"]),
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        except KeyError as exc:
            raise ValueError(f"RECIPIENTS_JSON entry #{i} is missing required field {exc}") from exc
    return recipients


@dataclass
class PushResult:
    name: str
    ok: bool
    event_id: Optional[int] = None
    error: Optional[str] = None


def push_to_recipients(workout: Workout, *, date: str, recipients: Sequence[Recipient], base_url: str = BASE_URL) -> List[PushResult]:
    """Push to every enabled recipient. One recipient's failure (revoked key,
    network hiccup) is captured in its own result instead of aborting the rest."""

    results = []
    for recipient in recipients:
        if not recipient.enabled:
            continue
        try:
            event = push_event(
                workout,
                vma=recipient.vma,
                date=date,
                athlete_id=recipient.athlete_id,
                api_key=recipient.api_key,
                base_url=base_url,
            )
            results.append(PushResult(name=recipient.name, ok=True, event_id=event.get("id")))
        except Exception as exc:  # boundary: one recipient's account shouldn't sink the rest
            results.append(PushResult(name=recipient.name, ok=False, error=str(exc)))
    return results
