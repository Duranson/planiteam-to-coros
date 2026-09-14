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

# In the cloud, RECIPIENTS_JSON is bound directly as an env var from Secret
# Manager (see main.py/DEPLOYMENT.md). Locally there's no secret manager, so
# a gitignored recipients.json file (same shape, see DEPLOYMENT.md "Build
# your recipients list") is read into the same env var here - mirroring how
# python-dotenv above transparently populates env vars from .env. This means
# every consumer (main.py, the CLI) can just read os.environ["RECIPIENTS_JSON"]
# without caring whether it came from Secret Manager or this local file.
_LOCAL_RECIPIENTS_FILE = Path("recipients.json")
if "RECIPIENTS_JSON" not in os.environ and _LOCAL_RECIPIENTS_FILE.exists():
    os.environ["RECIPIENTS_JSON"] = _LOCAL_RECIPIENTS_FILE.read_text(encoding="utf-8")


# The athlete's VMA (km/h) is not stored in the PDF: Planiteam prints a
# reference table for a range of VMA values and expects the athlete to read
# off their own row. This is that value for the repository owner.
DEFAULT_VMA = 17.5

INTERVALS_ICU_BASE_URL = "https://intervals.icu"

_DURATION_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
_REPEAT_RE = re.compile(r"^(\d+)x$", re.IGNORECASE)
_PACE_RE = re.compile(r"^(\d+)'(\d{2})/km$")
_ZONE_RE = re.compile(r"^Z\d+$")
_PERCENT_RANGE_RE = re.compile(r"^(\d+)-(\d+)%$")
_VMA_LABEL_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Known line-wrap artefact in Planiteam's fixed PDF template: long section
# titles get split across two text rows with no separating space.
_HEADER_JOIN_FIXUPS = {
    ("ÉCHAUFFEM", "ENT"): "ÉCHAUFFEMENT",
}


@dataclass
class Step:
    """One timed instruction inside a workout segment.

    ``cue`` is free text placed *before* the duration on the rendered line
    (e.g. "Effort 20s Z5 Pace"). Confirmed live that intervals.icu keeps this
    as a step-level text label (not a structured type, unlike
    ``Segment.role``'s warmup/cooldown flags - see CLAUDE.md), and that it
    shows up as that step's block name on the COROS device.
    """

    duration_s: int
    target: Optional[str] = None
    cue: Optional[str] = None

    def render(self) -> str:
        token = _format_duration(self.duration_s)
        # intervals.icu's structured-workout syntax requires an explicit
        # "Pace" suffix for running targets (zone or absolute pace) - a bare
        # "Z5" defaults to a *power* zone, and a bare pace value is ignored.
        parts = [self.cue] if self.cue else []
        parts.append(token)
        if self.target:
            parts.append(f"{self.target} Pace")
        return " ".join(parts)


@dataclass
class Segment:
    """A named block of the workout (warm-up, main set, cool-down, ...).

    ``role`` is ``"warmup"``, ``"cooldown"`` or ``None``. It drives the
    intervals.icu "Warmup"/"Cooldown" section-header keywords in
    ``to_intervals_icu_text()`` - confirmed live (see CLAUDE.md) to be the
    only thing that makes intervals.icu tag a step's ``workout_doc`` entry
    with ``"warmup": true`` / ``"cooldown": true``, which is what lets that
    step sync to the watch as the correct step type instead of a generic
    interval. The keyword must be the literal English word; Planiteam's
    French section names ("ÉCHAUFFEMENT", "RETOUR AU CALME") do not work.
    """

    name: str
    repeat: int = 1
    steps: List[Step] = field(default_factory=list)
    role: Optional[str] = None


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
                    "role": segment.role,
                    "steps": [
                        {"duration_s": step.duration_s, "target": step.target, "cue": step.cue}
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

        The "Nx" header is shown whenever a segment has more than one step -
        i.e. it's a real interval structure - even when ``repeat`` is 1
        (explicitly preferred over hiding a "1x" header: a lone
        multi-step "core" block, with no warm-up/cool-down around it, reads
        as a repeat of one rather than a bare list of steps). A segment with
        only one step (warm-up, cool-down, a plain drills block) never gets
        one, regardless of its repeat count - there's nothing to group.

        A segment whose ``role`` is "warmup"/"cooldown" gets that literal
        English keyword as its own header line (verified live: this, and
        only this, is what makes intervals.icu tag the step as warmup/
        cooldown for device sync - see the ``Segment.role`` docstring).
        """

        role_keywords = {"warmup": "Warmup", "cooldown": "Cooldown"}

        blocks: List[str] = []
        for segment in self.segments:
            step_lines = "\n".join(f"- {step.render()}" for step in segment.steps)
            if len(segment.steps) > 1:
                body = f"{segment.repeat}x\n{step_lines}"
            else:
                body = step_lines
            keyword = role_keywords.get(segment.role or "")
            blocks.append(f"{keyword}\n{body}" if keyword else body)
        return "\n\n".join(blocks)


def _format_duration(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    if seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m{seconds}s"


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


def _format_pace_value(total_seconds: float) -> str:
    minutes, seconds = divmod(round(total_seconds), 60)
    return f"{minutes}:{seconds:02d}"


def _format_pace(total_seconds: float) -> str:
    return f"{_format_pace_value(total_seconds)}/km"


# A %VMA band with a 0% lower bound (e.g. Planiteam's "0-65%" recovery zone)
# means "go as slow as you like" - not a literal dead stop. A single-value
# pace target doesn't convey that: confirmed live that COROS renders a bare
# absolute pace target with its own tight auto-generated tolerance band
# (a "5:16/km" target showed on the watch as "5'08/km - 5'24/km", ~2.5%
# either side), which reads as "hit close to this" - the opposite of the
# intended "no real floor" meaning, and actively counter-productive for a
# recovery interval. So an open (0%) lower bound is floored at this fixed
# %VMA instead, to get an explicit, wide, honestly-permissive range - not
# extracted from any PDF, just a reasonable "very easy jog" floor.
_OPEN_LOWER_BOUND_FLOOR_PCT = 40


def _vma_pace_target(lo_pct: int, hi_pct: int, vma: float) -> str:
    """Compute an absolute pace range target for a %VMA band.

    intervals.icu always pre-resolves pace *zone* targets (like "Z5 Pace")
    into an absolute pace using its own athlete-side zone config before a
    workout ever reaches a device - verified at the FIT byte level, see
    CLAUDE.md. Computing the pace here instead, directly from the %VMA the
    PDF actually prints and the athlete's own ``--vma``, means the main set
    no longer depends on intervals.icu's zone configuration at all - same as
    warm-up/cool-down already don't.
    """

    speed_hi = vma * hi_pct / 100
    fast_s = 3600 / speed_hi
    if lo_pct <= 0:
        lo_pct = _OPEN_LOWER_BOUND_FLOOR_PCT
    speed_lo = vma * lo_pct / 100
    slow_s = 3600 / speed_lo
    return f"{_format_pace_value(fast_s)}-{_format_pace_value(slow_s)}/km"


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


def _display_name(name: str) -> str:
    """Turn a PDF section name like "GAMMES" into a presentable cue ("Gammes")."""

    return " ".join(word.capitalize() for word in name.split())


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

    The two rows involved (durations, then "r = rest" below them) are found
    *relative to each other* - the "==>" row, then the nearest row above it
    that actually has "Nx" repeat markers on it - rather than at fixed page
    coordinates. An earlier version hardcoded absolute ``top`` bands
    reverse-engineered from one sample PDF; a second real PDF broke it
    immediately, because its section headers happened not to wrap onto a
    second line, shifting everything below them up by ~9pt. Coordinates
    drift between PDFs; the relationship between these two particular rows
    doesn't - though it isn't always the *immediately* preceding row: the
    pace table's own "VMA" column-header label sits in the gap between them
    on some PDFs, as its own single-word row, so this walks upward past any
    row without a repeat marker rather than assuming adjacency.
    """

    rows = _cluster_rows(words)
    rest_row_index = next((i for i, row in enumerate(rows) if any(w["text"] == "==>" for w in row)), None)
    if rest_row_index is None:
        return []
    duration_row_index = next(
        (i for i in range(rest_row_index - 1, -1, -1) if any(_REPEAT_RE.match(w["text"]) for w in rows[i])),
        None,
    )
    if duration_row_index is None:
        return []
    rest_row = rows[rest_row_index]
    duration_row = sorted(rows[duration_row_index], key=lambda w: w["x0"])
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


def _parse_main_set_zone_percents(words: Sequence[dict]) -> List[Tuple[int, int]]:
    """The "95-100%"/"0-65%" %VMA bands overlaid alongside the zone labels."""

    percent_words = sorted((w for w in words if _PERCENT_RANGE_RE.match(w["text"])), key=lambda w: w["x0"])
    return [tuple(int(v) for v in _PERCENT_RANGE_RE.match(w["text"]).groups()) for w in percent_words]


def _parse_pace_table(words: Sequence[dict]) -> Dict[float, List[int]]:
    """Parse the VMA reference table into ``{vma: [pace_s, pace_s, ...]}``.

    Column count varies by PDF: some workouts only print 2 columns (a
    warm-up and a cool-down pace); others (no %VMA zone overlay - see
    ``parse_planiteam_pdf``) print one column per session phase instead.
    Both are handled the same way here; it's up to the caller to decide
    which columns mean what.

    Rows are anchored on their VMA-value label (the leftmost column, e.g.
    "17.5") rather than a fixed row-clustering tolerance: the label's exact
    vertical offset from its own pace values varies by a couple of points
    between PDFs (different fonts/rendering for a longer table), enough to
    fall outside a tight tolerance and silently drop the row.
    """

    below_grid = [w for w in words if w["top"] > 150]
    labels = sorted(
        (w for w in below_grid if w["x0"] < 90 and _VMA_LABEL_RE.match(w["text"])),
        key=lambda w: w["top"],
    )

    table: Dict[float, List[int]] = {}
    for label in labels:
        paces = sorted(
            (w for w in below_grid if abs(w["top"] - label["top"]) <= 6 and _PACE_RE.match(w["text"])),
            key=lambda w: w["x0"],
        )
        if len(paces) < 2:
            continue
        table[float(label["text"])] = [_parse_pace(w["text"]) for w in paces]
    return table


def _lookup_pace(table: Dict[float, List[int]], vma: float, column: int) -> Optional[int]:
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


def _zone_number(zone: str) -> int:
    match = re.search(r"\d+", zone)
    return int(match.group()) if match else 0


def _zone_cue(zone: str, max_zone: int) -> str:
    # Generic effort/recovery cue, not tied to this workout's specific hill-
    # sprint content - the highest zone number used in the block is "Effort",
    # anything lower is "Récupération". Confirmed live that this text becomes
    # the step's block name on the COROS device (see Step.cue docstring).
    return "Effort" if _zone_number(zone) >= max_zone else "Récupération"


def _alternating_cue(index: int) -> str:
    # Used where there's no zone data to classify entries by (see
    # ``use_per_step_pace`` in parse_planiteam_pdf) - effort/recovery is
    # inferred purely by position instead: every workout seen so far starts
    # with an effort and alternates from there.
    return "Effort" if index % 2 == 0 else "Récupération"


def _plain_block_cue(role: Optional[str], name: str) -> Optional[str]:
    # A single-purpose block with no alternating effort/recovery structure.
    # Warm-up/cool-down already get a correctly-localised block name on the
    # device from the warmup/cooldown flag alone (confirmed live - see
    # CLAUDE.md), so no extra cue is added there. Anything else (e.g.
    # Planiteam's "GAMMES" drills) has no such flag, so its own section name
    # becomes the cue - generic to whatever a future PDF calls that block.
    return None if role else _display_name(name)


def _apply_zone_targets(
    entries: List[Tuple[int, Optional[int]]],
    zones: List[str],
    percents: List[Tuple[int, int]],
    vma: float,
) -> List[Step]:
    """Turn (duration, rest) entries into Steps, targeting each at a VMA-derived pace.

    The target is computed directly from the %VMA band the PDF prints (e.g.
    "95-100%") and ``vma`` (see ``_vma_pace_target``) rather than an
    intervals.icu zone reference like "Z5 Pace" - intervals.icu always
    resolves those itself using its own zone config before a workout reaches
    a device (verified at the FIT byte level, see CLAUDE.md), so computing
    the pace here instead removes that dependency entirely. The zone letters
    are still used, but only internally, to pick the "Effort"/"Récupération"
    cue text.

    A trailing rest value (e.g. the 3:00 recovery before the outer set
    repeats) is treated as an extra step in whichever zone is used by the
    *other* duration in this block (the recovery zone), since the block only
    alternates between an effort and a recovery duration.
    """

    zone_by_duration: Dict[int, str] = {}
    percent_by_duration: Dict[int, Tuple[int, int]] = {}
    for (duration_s, _rest_s), zone, pct in zip(entries, zones, percents):
        zone_by_duration.setdefault(duration_s, zone)
        percent_by_duration.setdefault(duration_s, pct)
    max_zone = max((_zone_number(z) for z in zones), default=0)

    steps: List[Step] = []
    for (duration_s, rest_s), zone, pct in zip(entries, zones, percents):
        target = _vma_pace_target(pct[0], pct[1], vma)
        steps.append(Step(duration_s=duration_s, target=target, cue=_zone_cue(zone, max_zone)))
        if rest_s:
            other = [d for d in zone_by_duration if d != duration_s]
            rest_zone = zone_by_duration[other[0]] if other else zone
            rest_pct = percent_by_duration[other[0]] if other else pct
            rest_target = _vma_pace_target(rest_pct[0], rest_pct[1], vma)
            steps.append(Step(duration_s=rest_s, target=rest_target, cue=_zone_cue(rest_zone, max_zone)))
    return steps


def parse_planiteam_pdf(path: Union[str, Path], vma: float = DEFAULT_VMA) -> Workout:
    """Parse a Planiteam PDF export into a Workout, using ``vma`` (km/h) to
    resolve step paces from the PDF's reference table.

    Two different pace-table layouts have been seen from Planiteam, and this
    dispatches between them (see CLAUDE.md for how each was found):

    - Zone-overlay PDFs: a 2-column table (warm-up pace, cool-down pace) plus
      a separate "Z5"/"95-100%"-style overlay for the main set, converted to
      an absolute pace via ``_vma_pace_target``.
    - Per-step PDFs: no zone overlay at all - instead one pace-table column
      per session phase (warm-up, each main-set entry, cool-down), looked up
      directly.

    Raises ``ValueError`` if nothing was parsed, rather than silently
    returning an empty workout - a differently-shaped PDF should fail loudly
    here, not push an empty session to a device with no error anywhere.
    """

    words, raw_text = _extract_page_words(path)

    title = _parse_title(words)
    section_names = _parse_section_headers(words)
    blocks = _parse_main_row(words)
    zones = _parse_main_set_zones(words)
    percents = _parse_main_set_zone_percents(words)
    pace_table = _parse_pace_table(words)

    warmup_pace = _lookup_pace(pace_table, vma, column=0)
    cooldown_pace = _lookup_pace(pace_table, vma, column=-1)

    pace_table_columns = len(next(iter(pace_table.values()), []))
    total_flat_entries = sum(len(block["entries"]) for block in blocks)
    use_per_step_pace = not zones and pace_table_columns > 2 and pace_table_columns == total_flat_entries

    segments: List[Segment] = []
    flat_index = 0
    for name, block in zip(section_names, blocks):
        is_zone_main_set = len(zones) > 1 and len(block["entries"]) == len(zones) == len(percents)
        # Whitespace-insensitive: "ÉCHAUFFEMENT" wraps onto two text rows at
        # a different character each time depending on the PDF (seen both
        # "ÉCHAUFFEM"/"ENT" and "ÉCHAUFFEME"/"NT" splits), and
        # _HEADER_JOIN_FIXUPS only knows the exact splits seen so far. This
        # only matters for role detection, not display - warmup/cooldown
        # segments never use their own name as a cue (see below), so an
        # imperfect join here is harmless as long as the role is still found.
        name_compact = name.upper().replace(" ", "")
        role = "warmup" if "CHAUFFEMENT" in name_compact else "cooldown" if "CALME" in name_compact else None

        if is_zone_main_set:
            steps = _apply_zone_targets(block["entries"], zones, percents, vma)
        elif use_per_step_pace:
            # A block with more than one entry and no warmup/cooldown role
            # is assumed to be the alternating effort/recovery main set -
            # see _alternating_cue for why "alternating" is a safe read here.
            is_alternating = role is None and len(block["entries"]) > 1
            steps = []
            for i, (duration_s, rest_s) in enumerate(block["entries"]):
                target = _format_pace(_lookup_pace(pace_table, vma, column=flat_index))
                flat_index += 1
                cue = _alternating_cue(i) if is_alternating else _plain_block_cue(role, name)
                steps.append(Step(duration_s=duration_s, target=target, cue=cue))
                if rest_s:
                    # Not observed in any per-step PDF so far (their rest
                    # values are always blank) - no pace-table column to
                    # attribute this to, so it's left untargeted rather than
                    # guessing.
                    steps.append(Step(duration_s=rest_s, target=None, cue=cue))
        else:
            cue = _plain_block_cue(role, name)
            steps = []
            for duration_s, rest_s in block["entries"]:
                if role == "warmup" and warmup_pace is not None:
                    target = _format_pace(warmup_pace)
                elif role == "cooldown" and cooldown_pace is not None:
                    target = _format_pace(cooldown_pace)
                else:
                    target = None
                steps.append(Step(duration_s=duration_s, target=target, cue=cue))
                if rest_s:
                    steps.append(Step(duration_s=rest_s, target=None, cue=cue))
        segments.append(Segment(name=name, repeat=block["repeat"], steps=steps, role=role))

    if not segments:
        raise ValueError(
            f"Parsed zero segments from {path} - its layout doesn't match what this parser expects "
            "(see CLAUDE.md for the structural assumptions this relies on)."
        )

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


@dataclass
class Recipient:
    """One club member to push a shared session to.

    Loaded from the ``RECIPIENTS_JSON`` env var - a JSON array, kept out of
    git the same way the single-athlete credentials always were (a
    gitignored ``.env`` locally, a Secret Manager secret in the cloud - see
    DEPLOYMENT.md/CLAUDE.md). ``vma`` is per-recipient because it directly
    changes the computed pace targets (see ``_vma_pace_target``); ``enabled``
    lets someone be paused (e.g. travelling) without losing their config.
    """

    name: str
    vma: float
    api_key: str
    athlete_id: str
    enabled: bool = True


def parse_recipients(raw: str) -> List[Recipient]:
    """Parse the ``RECIPIENTS_JSON`` env var into a list of Recipients.

    Raises ``ValueError`` on malformed JSON or a missing required field, so
    a typo in the list fails loudly at request time rather than silently
    dropping someone from the club-wide push.
    """

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
    """Outcome of pushing to one recipient - see ``push_to_recipients``."""

    name: str
    ok: bool
    event_id: Optional[int] = None
    title: Optional[str] = None
    error: Optional[str] = None


def push_to_recipients(
    pdf_path: Union[str, Path],
    *,
    date: str,
    recipients: Sequence[Recipient],
    sport: str = "Run",
    base_url: str = INTERVALS_ICU_BASE_URL,
) -> List[PushResult]:
    """Parse the PDF once per enabled recipient and push to each of their
    intervals.icu accounts independently.

    The PDF is re-parsed per recipient (not just re-pushed) because ``vma``
    changes the computed pace targets. One recipient's failure - a revoked
    key, a network hiccup - is caught and reported in that recipient's
    result rather than aborting everyone else's push; the caller decides
    what a partial failure means for the overall request (see main.py).
    """

    results = []
    for recipient in recipients:
        if not recipient.enabled:
            continue
        try:
            workout = parse_planiteam_pdf(pdf_path, vma=recipient.vma)
            event = push_event(
                workout,
                date=date,
                athlete_id=recipient.athlete_id,
                api_key=recipient.api_key,
                sport=sport,
                base_url=base_url,
            )
            results.append(PushResult(name=recipient.name, ok=True, event_id=event.get("id"), title=workout.title))
        except Exception as exc:  # boundary: one recipient's account/PDF edge case shouldn't sink the rest
            results.append(PushResult(name=recipient.name, ok=False, error=str(exc)))
    return results


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
        help="Also push the workout to intervals.icu as a planned event, via its API, to your own single account. "
        "Requires INTERVALS_ICU_API_KEY and INTERVALS_ICU_ATHLETE_ID (env vars or .env file) and --date.",
    )
    parser.add_argument(
        "--push-recipients",
        action="store_true",
        help="Push to every enabled recipient in RECIPIENTS_JSON instead of a single account - the same "
        "logic main.py runs in the cloud, useful for testing a recipients.json/RECIPIENTS_JSON change "
        "locally before deploying. Requires --date. Ignores --vma (each recipient's own vma is used).",
    )
    parser.add_argument("--date", type=str, default=None, help="Local date (YYYY-MM-DD) to schedule the event on. Required with --push/--push-recipients.")
    parser.add_argument("--sport", type=str, default="Run", help="intervals.icu sport type for the pushed event (default: %(default)s).")
    args = parser.parse_args(argv)

    if args.push and args.push_recipients:
        parser.error("--push and --push-recipients are mutually exclusive.")

    if args.push_recipients:
        if not args.date:
            parser.error("--push-recipients requires --date YYYY-MM-DD (the PDF has no session date).")
        raw = os.environ.get("RECIPIENTS_JSON")
        if not raw:
            parser.error(
                "--push-recipients requires RECIPIENTS_JSON to be set - either a recipients.json file "
                "in the current directory, or a RECIPIENTS_JSON env var/.env line (see DEPLOYMENT.md)."
            )
        try:
            recipients = parse_recipients(raw)
        except ValueError as exc:
            parser.error(str(exc))
        results = push_to_recipients(args.pdf, date=args.date, recipients=recipients, sport=args.sport)
        if not results:
            parser.error("No enabled recipients in RECIPIENTS_JSON.")
        for result in results:
            if result.ok:
                print(f"Pushed to {result.name}: event id {result.event_id} ({result.title})")
            else:
                print(f"FAILED to push to {result.name}: {result.error}")
        return 0 if any(r.ok for r in results) else 1

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
