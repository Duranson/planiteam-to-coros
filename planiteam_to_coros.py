#!/usr/bin/env python3
"""Planiteam-to-COROS workout converter.

Planiteam's PDF export does not use a readable text flow: section titles,
the "Nx ==> duration / r = rest" grid, and a VMA pace reference table are all
positioned independently on the page and get scrambled by naive text
extraction. This module reads the PDF's word *positions* (via ``pdfplumber``)
to reconstruct the actual structure, then exposes a normalised `Workout`
object that can be rendered as intervals.icu structured-workout text or JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover - import error surfaced clearly
    raise ImportError("Install dependencies from requirements.txt: pip install -r requirements.txt") from exc

try:
    import requests
except ImportError as exc:  # pragma: no cover - import error surfaced clearly
    raise ImportError("Install dependencies from requirements.txt: pip install -r requirements.txt") from exc

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a convenience, not a hard requirement
    pass


# The athlete's VMA (km/h) is not stored in the PDF: Planiteam prints a
# reference table for a range of VMA values and expects the athlete to read
# off their own row. This is that value for the repository owner.
DEFAULT_VMA = 17.5

INTERVALS_ICU_BASE_URL = "https://intervals.icu"

_DURATION_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
_REPEAT_RE = re.compile(r"^(\d+)x$", re.IGNORECASE)
_PACE_RE = re.compile(r"^(\d+)'(\d{2})/km$")
_ZONE_RE = re.compile(r"^Z\d+$")
_VMA_LABEL_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Known line-wrap artefact in Planiteam's fixed PDF template: long section
# titles get split across two text rows with no separating space.
_HEADER_JOIN_FIXUPS = {
    ("ÉCHAUFFEM", "ENT"): "ÉCHAUFFEMENT",
}


@dataclass
class Step:
    """One timed instruction inside a workout segment."""

    duration_s: int
    target: Optional[str] = None

    def render(self) -> str:
        token = _format_duration(self.duration_s)
        if not self.target:
            return token
        # intervals.icu's structured-workout syntax requires an explicit
        # "Pace" suffix for running targets (zone or absolute pace) - a bare
        # "Z5" defaults to a *power* zone, and a bare pace value is ignored.
        return f"{token} {self.target} Pace"


@dataclass
class Segment:
    """A named block of the workout (warm-up, main set, cool-down, ...)."""

    name: str
    repeat: int = 1
    steps: List[Step] = field(default_factory=list)


@dataclass
class Workout:
    """A canonical in-memory representation of a Planiteam workout."""

    title: str
    date: Optional[str] = None
    segments: List[Segment] = field(default_factory=list)
    source_text: str = ""

    def to_coros_json(self) -> str:
        """Return a JSON document that can be handed to a COROS adapter later."""

        payload = {
            "title": self.title,
            "date": self.date,
            "segments": [
                {
                    "name": segment.name,
                    "repeat": segment.repeat,
                    "steps": [
                        {"duration_s": step.duration_s, "target": step.target}
                        for step in segment.steps
                    ],
                }
                for segment in self.segments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def to_intervals_icu_text(self) -> str:
        """Render structured workout text in the intervals.icu line syntax.

        A repeat block's header is a bare "Nx" line (no leading "- ", no
        extra indentation on its child steps), and intervals.icu's own style
        guide asks for a blank line before and after each repeat block -
        joining every segment with a blank line satisfies that generally.
        """

        blocks: List[str] = []
        for segment in self.segments:
            step_lines = "\n".join(f"- {step.render()}" for step in segment.steps)
            if segment.repeat > 1:
                blocks.append(f"{segment.repeat}x\n{step_lines}")
            else:
                blocks.append(step_lines)
        return "\n\n".join(blocks)


def _format_duration(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0s"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"


def _parse_mmss(value: str) -> int:
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"Not a mm:ss duration: {value!r}")
    minutes, seconds = match.groups()
    return int(minutes) * 60 + int(seconds)


def _parse_pace(value: str) -> int:
    match = _PACE_RE.match(value.strip())
    if not match:
        raise ValueError(f"Not a pace token: {value!r}")
    minutes, seconds = match.groups()
    return int(minutes) * 60 + int(seconds)


def _format_pace(total_seconds: int) -> str:
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}/km"


def _extract_page_words(path: Union[str, Path]) -> Tuple[List[dict], str]:
    """Return the positioned words and raw text of the PDF's first page."""

    with pdfplumber.open(str(path)) as pdf:
        if not pdf.pages:
            raise ValueError(f"No pages found in PDF: {path}")
        page = pdf.pages[0]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        text = page.extract_text() or ""
    return words, text


def _cluster_rows(words: Sequence[dict], tol: float = 1.5) -> List[List[dict]]:
    """Group words into visual rows, tolerant of small baseline jitter."""

    rows: List[dict] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for row in rows:
            if abs(row["top"] - word["top"]) <= tol:
                row["words"].append(word)
                break
        else:
            rows.append({"top": word["top"], "words": [word]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
    rows.sort(key=lambda r: r["top"])
    return [row["words"] for row in rows]


def _parse_title(words: Sequence[dict]) -> str:
    """The title sits on the page's first line, left of the "Édité le" stamp."""

    title_words = sorted((w for w in words if w["top"] <= 60), key=lambda w: w["x0"])
    parts: List[str] = []
    for word in title_words:
        if word["text"] in ("Édité", "Edité"):
            break
        parts.append(word["text"])
    return " ".join(parts).strip() or "Planiteam Workout"


def _join_header_words(words: Sequence[dict]) -> str:
    parts = [w["text"] for w in words]
    for i in range(len(parts) - 1):
        pair = (parts[i], parts[i + 1])
        if pair in _HEADER_JOIN_FIXUPS:
            parts[i] = _HEADER_JOIN_FIXUPS[pair]
            parts[i + 1] = ""
    return " ".join(p for p in parts if p)


def _parse_section_headers(words: Sequence[dict]) -> List[str]:
    """Section titles ("ÉCHAUFFEMENT", "GAMMES", ...) sit above each block.

    They may be centred over their block and wrap onto a second text row, so
    words are merged by horizontal proximity (any y-position) rather than by
    row membership.
    """

    header_words = sorted((w for w in words if 95 <= w["top"] <= 120), key=lambda w: w["x0"])

    groups: List[dict] = []
    for word in header_words:
        target = next((g for g in groups if word["x0"] <= g["x1"] + 6), None)
        if target is None:
            groups.append({"x1": word["x1"], "words": [word]})
        else:
            target["words"].append(word)
            target["x1"] = max(target["x1"], word["x1"])

    return [_join_header_words(sorted(g["words"], key=lambda w: (w["top"], w["x0"]))) for g in groups]


def _pair_rest_values(rest_row: Sequence[dict]) -> List[Optional[int]]:
    """Parse a row of ``r = <value>`` triples (value may be blank) in x-order."""

    tokens = sorted(rest_row, key=lambda w: w["x0"])
    values: List[Optional[int]] = []
    i = 0
    while i < len(tokens):
        if tokens[i]["text"] != "r":
            i += 1
            continue
        i += 1
        if i < len(tokens) and tokens[i]["text"] == "=":
            i += 1
        value_s: Optional[int] = None
        if i < len(tokens) and tokens[i]["text"] != "r":
            text = tokens[i]["text"]
            if text.isdigit():
                value_s = int(text)
                i += 1
            elif _DURATION_RE.match(text):
                value_s = _parse_mmss(text)
                i += 1
        values.append(value_s)
    return values


def _parse_main_row(words: Sequence[dict]) -> List[dict]:
    """Return the ordered ``Nx ==> duration / rest`` blocks along the page.

    Each block is ``{"repeat": int, "entries": [(duration_s, rest_s | None), ...]}``.
    """

    duration_row = sorted((w for w in words if 128 <= w["top"] <= 136), key=lambda w: w["x0"])
    rest_row = [w for w in words if 140 <= w["top"] <= 150]
    rest_values = iter(_pair_rest_values(rest_row))

    blocks: List[dict] = []
    current: Optional[dict] = None
    for word in duration_row:
        repeat_match = _REPEAT_RE.match(word["text"])
        if repeat_match:
            current = {"repeat": int(repeat_match.group(1)), "entries": []}
            blocks.append(current)
            continue
        if current is None or not _DURATION_RE.match(word["text"]):
            continue
        duration_s = _parse_mmss(word["text"])
        current["entries"].append((duration_s, next(rest_values, None)))
    return blocks


def _parse_main_set_zones(words: Sequence[dict]) -> List[str]:
    """Zone labels (e.g. "Z5"/"Z1") overlaid above the main interval block."""

    zone_words = sorted((w for w in words if _ZONE_RE.match(w["text"])), key=lambda w: w["x0"])
    return [w["text"] for w in zone_words]


def _parse_pace_table(words: Sequence[dict]) -> Dict[float, Tuple[int, int]]:
    """Parse the VMA reference table into ``{vma: (warmup_pace_s, cooldown_pace_s)}``."""

    rows = _cluster_rows([w for w in words if w["top"] > 150], tol=1.5)

    table: Dict[float, Tuple[int, int]] = {}
    for row in rows:
        if not row or not _VMA_LABEL_RE.match(row[0]["text"]):
            continue
        paces = sorted((w for w in row if _PACE_RE.match(w["text"])), key=lambda w: w["x0"])
        if len(paces) < 2:
            continue
        table[float(row[0]["text"])] = (_parse_pace(paces[0]["text"]), _parse_pace(paces[-1]["text"]))
    return table


def _lookup_pace(table: Dict[float, Tuple[int, int]], vma: float, column: int) -> Optional[int]:
    """Look up (or linearly interpolate) the pace for a given VMA and column."""

    if not table:
        return None
    if vma in table:
        return table[vma][column]

    keys = sorted(table)
    if vma <= keys[0]:
        return table[keys[0]][column]
    if vma >= keys[-1]:
        return table[keys[-1]][column]

    lower = max(k for k in keys if k < vma)
    upper = min(k for k in keys if k > vma)
    lower_v, upper_v = table[lower][column], table[upper][column]
    ratio = (vma - lower) / (upper - lower)
    return round(lower_v + (upper_v - lower_v) * ratio)


def _apply_zone_targets(entries: List[Tuple[int, Optional[int]]], zones: List[str]) -> List[Step]:
    """Turn (duration, rest) entries into Steps, tagging each with its zone.

    A trailing rest value (e.g. the 3:00 recovery before the outer set
    repeats) is treated as an extra step in whichever zone is used by the
    *other* duration in this block (the recovery zone), since the block only
    alternates between an effort and a recovery duration.
    """

    zone_by_duration: Dict[int, str] = {}
    for (duration_s, _rest_s), zone in zip(entries, zones):
        zone_by_duration.setdefault(duration_s, zone)

    steps: List[Step] = []
    for (duration_s, rest_s), zone in zip(entries, zones):
        steps.append(Step(duration_s=duration_s, target=zone))
        if rest_s:
            other = [d for d in zone_by_duration if d != duration_s]
            rest_zone = zone_by_duration[other[0]] if other else zone
            steps.append(Step(duration_s=rest_s, target=rest_zone))
    return steps


def parse_planiteam_pdf(path: Union[str, Path], vma: float = DEFAULT_VMA) -> Workout:
    """Parse a Planiteam PDF export into a Workout, using ``vma`` (km/h) to
    resolve the warm-up/cool-down paces from the PDF's reference table."""

    words, raw_text = _extract_page_words(path)

    title = _parse_title(words)
    section_names = _parse_section_headers(words)
    blocks = _parse_main_row(words)
    zones = _parse_main_set_zones(words)
    pace_table = _parse_pace_table(words)

    warmup_pace = _lookup_pace(pace_table, vma, column=0)
    cooldown_pace = _lookup_pace(pace_table, vma, column=1)

    segments: List[Segment] = []
    for name, block in zip(section_names, blocks):
        is_main_set = len(zones) > 1 and len(block["entries"]) == len(zones)
        if is_main_set:
            steps = _apply_zone_targets(block["entries"], zones)
        else:
            steps = []
            for duration_s, rest_s in block["entries"]:
                if "CHAUFFEMENT" in name.upper() and warmup_pace is not None:
                    target = _format_pace(warmup_pace)
                elif "CALME" in name.upper() and cooldown_pace is not None:
                    target = _format_pace(cooldown_pace)
                else:
                    target = None
                steps.append(Step(duration_s=duration_s, target=target))
                if rest_s:
                    steps.append(Step(duration_s=rest_s, target=None))
        segments.append(Segment(name=name, repeat=block["repeat"], steps=steps))

    return Workout(title=title, date=None, segments=segments, source_text=raw_text)


def build_event_payload(workout: Workout, *, date: str, sport: str = "Run") -> dict:
    """Build the EventEx JSON body for intervals.icu's create-event endpoint.

    ``date`` is a local ``YYYY-MM-DD`` string (the PDF has no session date,
    see CLAUDE.md).
    """

    return {
        "start_date_local": f"{date}T00:00:00",
        "category": "WORKOUT",
        "type": sport,
        "name": workout.title,
        "description": workout.to_intervals_icu_text(),
    }


def push_event(
    workout: Workout,
    *,
    date: str,
    athlete_id: str,
    api_key: str,
    sport: str = "Run",
    base_url: str = INTERVALS_ICU_BASE_URL,
) -> dict:
    """Create a planned event on intervals.icu's calendar.

    Uses HTTP Basic Auth with the literal username ``API_KEY`` and the
    athlete's API key as password, per intervals.icu's documented API.

    Every call creates a *new* event: intervals.icu assigns its own ``uid``
    on creation and ignores any client-supplied one, so there is no working
    upsert-by-uid available here. Re-pushing the same PDF/date will create a
    duplicate event rather than updating the previous one - delete the stale
    one by hand (or via the API's DELETE .../events/{eventId}) if that
    happens.
    """

    payload = build_event_payload(workout, date=date, sport=sport)
    response = requests.post(
        f"{base_url}/api/v1/athlete/{athlete_id}/events",
        params={"upsertOnUid": "false"},
        json=payload,
        auth=("API_KEY", api_key),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def cli(argv: Optional[Sequence[str]] = None) -> int:
    """CLI wrapper for invoking the converter from the command line."""

    parser = argparse.ArgumentParser(description="Convert a Planiteam PDF into a workout recipe for intervals.icu or JSON.")
    parser.add_argument("pdf", type=Path, help="Path to the Planiteam PDF file.")
    parser.add_argument("--format", choices=("intervals", "json"), default="intervals", help="Output format. Defaults to intervals.icu text structure.")
    parser.add_argument("--output", type=Path, default=None, help="Optional output file path.")
    parser.add_argument(
        "--vma",
        type=float,
        default=DEFAULT_VMA,
        help="Athlete's VMA in km/h, used to resolve warm-up/cool-down paces from the PDF's reference table (default: %(default)s).",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Also push the workout to intervals.icu as a planned event, via its API. "
        "Requires INTERVALS_ICU_API_KEY and INTERVALS_ICU_ATHLETE_ID (env vars or .env file) and --date.",
    )
    parser.add_argument("--date", type=str, default=None, help="Local date (YYYY-MM-DD) to schedule the event on. Required with --push.")
    parser.add_argument("--sport", type=str, default="Run", help="intervals.icu sport type for the pushed event (default: %(default)s).")
    args = parser.parse_args(argv)

    workout = parse_planiteam_pdf(args.pdf, vma=args.vma)
    payload = workout.to_coros_json() if args.format == "json" else workout.to_intervals_icu_text()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)

    if args.push:
        if not args.date:
            parser.error("--push requires --date YYYY-MM-DD (the PDF has no session date).")
        api_key = os.environ.get("INTERVALS_ICU_API_KEY")
        athlete_id = os.environ.get("INTERVALS_ICU_ATHLETE_ID")
        if not api_key or not athlete_id:
            parser.error("--push requires INTERVALS_ICU_API_KEY and INTERVALS_ICU_ATHLETE_ID to be set (env vars or .env file).")
        event = push_event(workout, date=args.date, athlete_id=athlete_id, api_key=api_key, sport=args.sport)
        print(f"Pushed to intervals.icu as event id {event.get('id')}, scheduled on {args.date}.")
        print(f"View it on your calendar: {INTERVALS_ICU_BASE_URL}/calendar")

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
