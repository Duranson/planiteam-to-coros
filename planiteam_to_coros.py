#!/usr/bin/env python3
"""Planiteam-to-COROS workout converter.

Planiteam's PDF export is a coordinate dump, not a text flow: section titles,
the "Nx ==> duration / r = rest" grid and the VMA pace table are independently
positioned text objects. This module reads word *positions* (``pdfplumber``)
to rebuild the structure, then produces a VMA-independent `Workout` that
renders to intervals.icu structured-workout text for any athlete's VMA.

Pipeline (see ARCHITECTURE.md / CLAUDE.md for the reasoning):

1. Page layout - find the grid rows relative to each other, then the title
   above, headers between, and the pace table / zone overlay below.
2. Plan - classify every entry (warm-up, cool-down, lap, effort, recovery,
   plain block) and attach its %VMA target, matching everything by nearest
   x-position.
3. Render - %VMA -> pace for a given VMA, in intervals.icu's line syntax.

Anything that doesn't fit the known structure raises ``ValueError`` rather
than silently producing a degraded session.
"""

from __future__ import annotations

import argparse
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import List, Optional, Sequence, Tuple, TypeVar, Union

import pdfplumber

import intervals_icu

# The athlete's VMA (km/h) is not in the PDF. This is the repository owner's.
DEFAULT_VMA = 17.5

_DURATION_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
_REPEAT_RE = re.compile(r"^(\d+)x$", re.IGNORECASE)
_DISTANCE_RE = re.compile(r"^(\d+(?:\.\d+)?)(km|m)$")
_PACE_RE = re.compile(r"^(\d+)'(\d{2})/km$")
_ZONE_RE = re.compile(r"^Z(\d+)$")
_PERCENT_RANGE_RE = re.compile(r"^(\d+)-(\d+)%$")
_VMA_LABEL_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Free-text annotation Planiteam prints on the duration row instead of a
# duration. It carries no step; its own "r = 0" is simply never matched.
_IGNORED_TOKENS = {"consigne"}

# A "Lap" entry is an open-ended drills block ended by the lap button. The
# duration only feeds intervals.icu's planned total, never the device; both
# values are the athlete's choice, not from the PDF.
_LAP_TOKEN = "lap"
_LAP_DEFAULT_DURATION_S = 10 * 60
_LAP_CUE = "Gammes"

# Matching tolerance (pt) between an entry and its rest group, zone label,
# %VMA band or pace-table column. Observed offsets are 2-12pt; two distinct
# entries are never closer than ~48pt.
_X_TOL = 20.0
_ROW_TOL = 1.5
# A pace-table row's VMA label sits up to ~2.2pt off its own cells.
_TABLE_ROW_TOL = 6.0
# Spread of the VMA-label column within one PDF (its absolute x varies).
_LABEL_COLUMN_SPREAD = 15.0

# Effort vs recovery: Z1/Z2 are recoveries, Z3+ efforts. Without a zone
# label, a %VMA below this is a recovery (pace columns seen: recoveries
# 55-60%, efforts 80-100%).
_MAX_RECOVERY_ZONE = 2
_MIN_EFFORT_PCT = 70

RECOVERY_CUE = "Récupération"

PctBand = Tuple[int, int]
T = TypeVar("T")


@dataclass
class Step:
    """One line of the workout: a duration *or* a distance.

    ``cue`` is free text before the duration; intervals.icu keeps it as the
    step's text, which COROS shows as the block name. ``pace_pct`` is the
    %VMA target as ``(lo, hi)`` - a range when ``lo != hi``, else a single
    pace - resolved against the athlete's VMA only at render time.
    ``until_lap_press`` makes the step open-ended on the device.
    """

    duration_s: Optional[int] = None
    distance_m: Optional[int] = None
    cue: Optional[str] = None
    pace_pct: Optional[PctBand] = None
    until_lap_press: bool = False

    def render(self, vma: float) -> str:
        parts = [self.cue] if self.cue else []
        if self.until_lap_press:
            parts.append("Press lap")
        parts.append(_format_distance(self.distance_m) if self.distance_m is not None else _format_duration(self.duration_s or 0))
        if self.pace_pct:
            # A bare pace is silently dropped by intervals.icu: "Pace" is required.
            parts.append(f"{_format_pace_target(self.pace_pct, vma)} Pace")
        return " ".join(parts)


@dataclass
class Segment:
    """A block of the workout. ``role`` ("warmup"/"cooldown"/None) renders as
    the literal "Warmup"/"Cooldown" header line - the only thing that makes
    intervals.icu (and then COROS) type the step as warm-up/cool-down."""

    name: str
    repeat: int = 1
    steps: List[Step] = field(default_factory=list)
    role: Optional[str] = None


@dataclass
class Workout:
    title: str
    segments: List[Segment] = field(default_factory=list)

    def to_intervals_icu_text(self, vma: float) -> str:
        """Render in intervals.icu's structured-workout syntax.

        A multi-step segment gets a bare "Nx" header line (even at 1x) with its
        steps as plain "- " bullets at the same indentation; segments are
        separated by a blank line, as the syntax requires around repeats.
        """

        role_keywords = {"warmup": "Warmup", "cooldown": "Cooldown"}
        blocks: List[str] = []
        for segment in self.segments:
            lines = [f"- {step.render(vma)}" for step in segment.steps]
            if len(segment.steps) > 1:
                lines.insert(0, f"{segment.repeat}x")
            if segment.role:
                lines.insert(0, role_keywords[segment.role])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


# --- Formatting -------------------------------------------------------------


def _format_duration(total_seconds: int) -> str:
    minutes, seconds = divmod(max(total_seconds, 0), 60)
    if minutes == 0:
        return f"{seconds}s"
    if seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m{seconds}s"


def _format_distance(meters: int) -> str:
    # intervals.icu reads a bare "m" as *minutes*: metres must be "mtr".
    if meters % 1000 == 0:
        return f"{meters // 1000}km"
    return f"{meters}mtr"


def _format_pace(pct: int, vma: float) -> str:
    minutes, seconds = divmod(round(3600 / (vma * pct / 100)), 60)
    return f"{minutes}:{seconds:02d}"


def _format_pace_target(band: PctBand, vma: float) -> str:
    lo, hi = band
    if lo == hi:
        return f"{_format_pace(hi, vma)}/km"
    if lo <= 0:
        raise ValueError(f"Cannot target an open-ended {lo}-{hi}% VMA band as a pace range")
    return f"{_format_pace(hi, vma)}-{_format_pace(lo, vma)}/km"


def _display_name(name: str) -> str:
    return " ".join(word.capitalize() for word in name.split())


def _mentions_cotes(name: str) -> bool:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return "COTE" in stripped.upper()


def _role(name: str) -> Optional[str]:
    # Spaces stripped: the PDF wraps ÉCHAUFFEMENT at an unpredictable letter.
    compact = name.upper().replace(" ", "")
    if "CHAUFFEMENT" in compact:
        return "warmup"
    if "CALME" in compact:
        return "cooldown"
    return None


# --- Page layout ------------------------------------------------------------


@dataclass
class _Entry:
    """One cell of the duration row, with its own "r = rest" below it."""

    x0: float
    duration_s: Optional[int] = None
    distance_m: Optional[int] = None
    rest_s: int = 0
    lap: bool = False


@dataclass
class _Block:
    name: str
    repeat: int
    entries: List[_Entry]


@dataclass
class _PaceColumn:
    """One pace-table column: ``(vma_row, seconds, is_split)`` per row.

    Every column is a constant %VMA across rows. A "7'42/km" cell is a pace;
    a "05:41" cell is the time to cover the entry's own distance (a split).
    """

    x0: float
    cells: List[Tuple[float, int, bool]] = field(default_factory=list)

    def vma_pct(self, distance_m: Optional[int]) -> Optional[int]:
        pcts = []
        for vma_row, seconds, is_split in self.cells:
            if is_split:
                if not distance_m:
                    return None
                seconds = seconds * 1000 / distance_m
            pcts.append(360000 / (seconds * vma_row))
        return round(mean(pcts)) if pcts else None


@dataclass
class _Page:
    title: str
    blocks: List[_Block]
    zones: List[Tuple[float, int]]
    bands: List[Tuple[float, PctBand]]
    columns: List[_PaceColumn]


def _parse_mmss(value: str) -> int:
    minutes, seconds = _DURATION_RE.match(value).groups()
    return int(minutes) * 60 + int(seconds)


def _nearest(x0: float, candidates: Sequence[Tuple[float, T]]) -> Optional[T]:
    best = min(candidates, key=lambda c: abs(c[0] - x0), default=None)
    return best[1] if best is not None and abs(best[0] - x0) <= _X_TOL else None


def _cluster_rows(words: Sequence[dict]) -> List[List[dict]]:
    """Group words into visual rows, tolerant of small baseline jitter."""

    rows: List[List[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        row = next((r for r in rows if abs(r[0]["top"] - word["top"]) <= _ROW_TOL), None)
        if row is None:
            rows.append([word])
        else:
            row.append(word)
    return [sorted(row, key=lambda w: w["x0"]) for row in rows]


def _parse_title(words: Sequence[dict]) -> str:
    parts: List[str] = []
    for word in sorted(words, key=lambda w: w["x0"]):
        if word["text"] in ("Édité", "Edité"):
            break
        parts.append(word["text"])
    return " ".join(parts) or "Planiteam Workout"


def _parse_section_headers(words: Sequence[dict]) -> List[str]:
    """Headers are centred over their block and may wrap onto a second row,
    so words are grouped by horizontal overlap regardless of row."""

    groups: List[dict] = []
    for word in sorted(words, key=lambda w: w["x0"]):
        group = next((g for g in groups if word["x0"] <= g["x1"] + 6), None)
        if group is None:
            groups.append({"x1": word["x1"], "words": [word]})
        else:
            group["words"].append(word)
            group["x1"] = max(group["x1"], word["x1"])
    return [" ".join(w["text"] for w in sorted(g["words"], key=lambda w: (w["top"], w["x0"]))) for g in groups]


def _parse_rests(rest_row: Sequence[dict]) -> List[Tuple[float, int]]:
    """``(x0, seconds)`` for every "r = <value>" group; a blank value is 0."""

    tokens = [w["text"] for w in rest_row]
    rests = []
    for i, word in enumerate(rest_row):
        if word["text"] != "r":
            continue
        value = tokens[i + 2] if i + 2 < len(tokens) and tokens[i + 1] == "=" else ""
        seconds = int(value) if value.isdigit() else _parse_mmss(value) if _DURATION_RE.match(value) else 0
        rests.append((word["x0"], seconds))
    return rests


def _parse_entry(word: dict) -> _Entry:
    text = word["text"]
    if text.lower() == _LAP_TOKEN:
        return _Entry(word["x0"], duration_s=_LAP_DEFAULT_DURATION_S, lap=True)
    if _DURATION_RE.match(text):
        return _Entry(word["x0"], duration_s=_parse_mmss(text))
    distance = _DISTANCE_RE.match(text)
    if distance:
        factor = 1000 if distance.group(2) == "km" else 1
        return _Entry(word["x0"], distance_m=round(float(distance.group(1)) * factor))
    raise ValueError(f"Unrecognised token {text!r} on the duration row")


def _parse_blocks(duration_row: Sequence[dict], rest_row: Sequence[dict]) -> List[Tuple[int, List[_Entry]]]:
    """``(repeat, entries)`` per "Nx" marker, each entry carrying the rest
    nearest to it by x0 - never by sequence, which "Consigne" would desync."""

    rests = _parse_rests(rest_row)
    blocks: List[Tuple[int, List[_Entry]]] = []
    for word in duration_row:
        repeat = _REPEAT_RE.match(word["text"])
        if repeat:
            blocks.append((int(repeat.group(1)), []))
            continue
        if word["text"].lower() in _IGNORED_TOKENS:
            continue
        if not blocks:
            raise ValueError(f"Token {word['text']!r} appears before the first repeat marker")
        entry = _parse_entry(word)
        rest_index = _nearest(entry.x0, [(x0, i) for i, (x0, _) in enumerate(rests)])
        if rest_index is not None:
            entry.rest_s = rests.pop(rest_index)[1]
        blocks[-1][1].append(entry)
    return blocks


def _parse_pace_columns(table_words: Sequence[dict]) -> List[_PaceColumn]:
    """Read the VMA reference table into columns.

    Rows are anchored on their VMA label (the leftmost numeric column, whose
    absolute x varies between PDFs); cells within a few points vertically of
    a label belong to that row. Cells are then grouped into columns by x.
    """

    labels = [w for w in table_words if _VMA_LABEL_RE.match(w["text"])]
    if not labels:
        return []
    label_x0 = min(w["x0"] for w in labels)
    labels = [w for w in labels if w["x0"] <= label_x0 + _LABEL_COLUMN_SPREAD]

    cells = []
    for label in labels:
        for word in table_words:
            if abs(word["top"] - label["top"]) > _TABLE_ROW_TOL:
                continue
            pace = _PACE_RE.match(word["text"])
            if pace:
                cells.append((word["x0"], float(label["text"]), int(pace.group(1)) * 60 + int(pace.group(2)), False))
            elif _DURATION_RE.match(word["text"]):
                cells.append((word["x0"], float(label["text"]), _parse_mmss(word["text"]), True))

    columns: List[_PaceColumn] = []
    for x0, *cell in sorted(cells):
        if not columns or x0 - columns[-1].x0 > _X_TOL:
            columns.append(_PaceColumn(x0))
        columns[-1].cells.append(tuple(cell))
    return columns


def _expand_combined_headers(blocks: Sequence[_Block]) -> List[_Block]:
    """Split a "ÉCHAUFFEMENT + GAMMES"-style header over its block's entries,
    one name per entry - only when the counts match exactly."""

    expanded: List[_Block] = []
    for block in blocks:
        parts = [p.strip() for p in block.name.split(" + ")]
        if len(parts) > 1 and len(parts) == len(block.entries):
            expanded.extend(_Block(part, block.repeat, [entry]) for part, entry in zip(parts, block.entries))
        else:
            expanded.append(block)
    return expanded


def _read_page(path: Union[str, Path]) -> _Page:
    with pdfplumber.open(str(path)) as pdf:
        if not pdf.pages:
            raise ValueError(f"No pages found in PDF: {path}")
        words = pdf.pages[0].extract_words()
    if not words:
        raise ValueError(f"No text found in PDF: {path}")

    # Everything is located relative to the interval grid: the "==>" rest row,
    # and the nearest row above it with "Nx" markers (a lone "VMA" label can
    # sit between them). Absolute coordinates drift between PDFs.
    rows = _cluster_rows(words)
    rest_index = next((i for i, row in enumerate(rows) if any(w["text"] == "==>" for w in row)), None)
    if rest_index is None:
        raise ValueError(f"{path}: no '==>' interval row found")
    duration_index = next((i for i in range(rest_index - 1, -1, -1) if any(_REPEAT_RE.match(w["text"]) for w in rows[i])), None)
    if duration_index is None:
        raise ValueError(f"{path}: no 'Nx' duration row found above the '==>' row")

    title_bottom = max(w["bottom"] for w in rows[0])
    grid_top = min(w["top"] for w in rows[duration_index])
    grid_bottom = max(w["bottom"] for w in rows[rest_index])
    title_words = [w for w in words if w["top"] < title_bottom]
    header_words = [w for w in words if title_bottom <= w["top"] < grid_top - _ROW_TOL]
    below_grid = [w for w in words if w["top"] > grid_bottom]

    headers = _parse_section_headers(header_words)
    raw_blocks = _parse_blocks(rows[duration_index], rows[rest_index])
    if len(headers) != len(raw_blocks):
        raise ValueError(f"{path}: {len(headers)} section headers {headers} but {len(raw_blocks)} 'Nx' blocks")
    blocks = [_Block(name, repeat, entries) for name, (repeat, entries) in zip(headers, raw_blocks)]

    zones, bands = [], []
    for word in below_grid:
        zone = _ZONE_RE.match(word["text"])
        band = _PERCENT_RANGE_RE.match(word["text"])
        if zone:
            zones.append((word["x0"], int(zone.group(1))))
        elif band:
            bands.append((word["x0"], (int(band.group(1)), int(band.group(2)))))

    return _Page(
        title=_parse_title(title_words),
        blocks=_expand_combined_headers(blocks),
        zones=zones,
        bands=bands,
        columns=_parse_pace_columns(below_grid),
    )


# --- Plan -------------------------------------------------------------------


def _intensity(entry: _Entry, page: _Page) -> Tuple[Optional[int], Optional[PctBand]]:
    """The entry's zone label and %VMA band: from the Z/% overlay above the
    table when there is one, else from the pace-table column under it."""

    zone = _nearest(entry.x0, page.zones)
    band = _nearest(entry.x0, page.bands)
    if band is None:
        column = _nearest(entry.x0, [(c.x0, c) for c in page.columns])
        pct = column.vma_pct(entry.distance_m) if column else None
        band = (pct, pct) if pct is not None else None
    return zone, band


def _is_recovery(zone: Optional[int], band: Optional[PctBand]) -> Optional[bool]:
    if zone is not None:
        return zone <= _MAX_RECOVERY_ZONE
    if band is not None:
        return band[1] < _MIN_EFFORT_PCT
    return None


def _step(entry: _Entry, **kwargs) -> Step:
    return Step(duration_s=entry.duration_s, distance_m=entry.distance_m, **kwargs)


def _interval_steps(block: _Block, entries: Sequence[_Entry], page: _Page) -> List[Step]:
    """Effort/recovery steps. Recoveries and côtes get no pace target (a
    flat pace is meaningless on a slope); côtes carry their zone in the cue."""

    is_cotes = _mentions_cotes(block.name)
    steps: List[Step] = []
    for entry in entries:
        zone, band = _intensity(entry, page)
        recovery = _is_recovery(zone, band)
        if recovery is None:
            raise ValueError(f"{block.name}: no zone label or pace column next to the step at x={entry.x0:.0f}")
        cue = RECOVERY_CUE if recovery else "Effort Cotes" if is_cotes else "Effort"
        if is_cotes and zone is not None:
            cue += f" Z{zone}"
        steps.append(_step(entry, cue=cue, pace_pct=None if recovery or is_cotes else band))
        if entry.rest_s:
            steps.append(Step(duration_s=entry.rest_s, cue=RECOVERY_CUE))
    return steps


def _plain_step(block: _Block, entry: _Entry, page: _Page) -> Step:
    """A single-entry block (drills, an inter-block jog, a tempo): named after
    its header, targeted only when its own %VMA marks it as an effort."""

    zone, band = _intensity(entry, page)
    is_effort = _is_recovery(zone, band) is False and not _mentions_cotes(block.name)
    return _step(entry, cue=_display_name(block.name), pace_pct=band if is_effort else None)


def _block_segments(block: _Block, entries: Sequence[_Entry], page: _Page) -> Segment:
    role = _role(block.name)
    if role:
        # The role flag alone names the step on the device; warm-up and
        # cool-down get no pace target (athlete's choice).
        steps = []
        for entry in entries:
            steps.append(_step(entry))
            if entry.rest_s:
                steps.append(Step(duration_s=entry.rest_s))
    elif len(entries) > 1 or entries[0].rest_s:
        steps = _interval_steps(block, entries, page)
    else:
        steps = [_plain_step(block, entries[0], page)]
    return Segment(name=block.name, repeat=block.repeat, steps=steps, role=role)


def _build_segments(page: _Page) -> List[Segment]:
    segments: List[Segment] = []
    for block in page.blocks:
        # A "Lap" entry becomes its own segment (it needs its own role header;
        # sharing one would turn the block into a "1x" group).
        run: List[_Entry] = []
        for entry in block.entries + [None]:
            if entry is not None and not entry.lap:
                run.append(entry)
                continue
            if run:
                segments.append(_block_segments(block, run, page))
                run = []
            if entry is not None:
                lap_step = Step(duration_s=entry.duration_s, cue=_LAP_CUE, until_lap_press=True)
                segments.append(Segment(name=_LAP_CUE, steps=[lap_step], role=_role(block.name)))
    return segments


def parse_planiteam_pdf(path: Union[str, Path]) -> Workout:
    """Parse a Planiteam PDF export into a VMA-independent Workout.

    Raises ``ValueError`` whenever the page doesn't fit the known structure,
    so a new layout fails loudly instead of pushing a wrong or empty session.
    """

    page = _read_page(path)
    segments = _build_segments(page)
    if not segments:
        raise ValueError(f"{path}: parsed zero segments")
    return Workout(title=page.title, segments=segments)


# --- CLI --------------------------------------------------------------------


def cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a Planiteam PDF into intervals.icu workout text, optionally pushing it to every recipient.")
    parser.add_argument("pdf", type=Path, help="Path to the Planiteam PDF file.")
    parser.add_argument("--vma", type=float, default=DEFAULT_VMA, help="VMA in km/h used for the printed paces (default: %(default)s).")
    parser.add_argument("--output", type=Path, default=None, help="Write the text to this file instead of stdout.")
    parser.add_argument(
        "--push-recipients",
        action="store_true",
        help="Push to every enabled recipient in RECIPIENTS_JSON (env var, .env, or a local recipients.json), "
        "each with their own VMA - the same logic main.py runs in the cloud. Requires --date.",
    )
    parser.add_argument("--date", type=str, default=None, help="Local date (YYYY-MM-DD) to schedule the event on.")
    args = parser.parse_args(argv)

    workout = parse_planiteam_pdf(args.pdf)

    if not args.push_recipients:
        text = workout.to_intervals_icu_text(args.vma)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    if not args.date:
        parser.error("--push-recipients requires --date YYYY-MM-DD (the PDF has no session date).")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raw = os.environ.get("RECIPIENTS_JSON")
    local_file = Path("recipients.json")
    if not raw and local_file.exists():
        raw = local_file.read_text(encoding="utf-8")
    if not raw:
        parser.error("Set RECIPIENTS_JSON or create recipients.json (see DEPLOYMENT.md).")
    try:
        recipients = intervals_icu.parse_recipients(raw)
    except ValueError as exc:
        parser.error(str(exc))

    results = intervals_icu.push_to_recipients(workout, date=args.date, recipients=recipients)
    if not results:
        parser.error("No enabled recipients.")
    for result in results:
        print(f"Pushed to {result.name}: event id {result.event_id}" if result.ok else f"FAILED to push to {result.name}: {result.error}")
    return 0 if any(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(cli())
