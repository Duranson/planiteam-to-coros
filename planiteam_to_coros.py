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
import unicodedata
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
# Distance-based entries ("500m", "1.0km") instead of a mm:ss duration - seen
# on VMA-split workouts (6x500m, 6x8x1000m...). Checked in this order so a
# "km" token is never mis-matched by the metres pattern.
_DISTANCE_KM_RE = re.compile(r"^(\d+(?:\.\d+)?)km$")
_DISTANCE_M_RE = re.compile(r"^(\d+)m$")

# Known line-wrap artefact in Planiteam's fixed PDF template: long section
# titles get split across two text rows with no separating space. This is no
# longer load-bearing for correctness (role detection strips whitespace and
# substring-matches instead, see parse_planiteam_pdf) - kept only for
# cosmetic Segment.name/JSON output on the splits it already knows about.
_HEADER_JOIN_FIXUPS = {
    ("ÉCHAUFFEM", "ENT"): "ÉCHAUFFEMENT",
}

# Tolerance (page points) used to match a word to the nearest other word by
# x-position - a duration/distance entry to its own "r = rest" group, to its
# nearest Z-zone/%VMA overlay token, or to its nearest pace-table column.
# Observed offsets across every sample PDF are 2-12pt; observed spacing
# between two distinct real entries is never under ~48pt - so this leaves a
# wide safety margin on both sides. See CLAUDE.md "Generalizing beyond the
# first sample PDF".
_X0_MATCH_TOL = 20.0


@dataclass
class Step:
    """One timed instruction inside a workout segment.

    Exactly one of ``duration_s`` (a time, e.g. 20 seconds) or ``distance_m``
    (a distance, e.g. 500 metres - some VMA-split workouts target a distance
    directly rather than a time) should be set.

    ``cue`` is free text placed *before* the duration on the rendered line
    (e.g. "Effort 20s Z5 Pace"). Confirmed live that intervals.icu keeps this
    as a step-level text label (not a structured type, unlike
    ``Segment.role``'s warmup/cooldown flags - see CLAUDE.md), and that it
    shows up as that step's block name on the COROS device.
    """

    duration_s: Optional[int] = None
    distance_m: Optional[float] = None
    distance_unit: str = "m"
    target: Optional[str] = None
    cue: Optional[str] = None

    def render(self) -> str:
        if self.distance_m is not None:
            token = _format_distance(self.distance_m, self.distance_unit)
        else:
            token = _format_duration(self.duration_s or 0)
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
                        {
                            "duration_s": step.duration_s,
                            "distance_m": step.distance_m,
                            "target": step.target,
                            "cue": step.cue,
                        }
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


def _format_distance(meters: float, unit: str) -> str:
    """Render a distance-based step using intervals.icu's distance syntax.

    Confirmed live (pushed a probe event, read back ``workout_doc``): the
    required suffix for metres is "mtr", *not* "m" - a bare "m" means
    *minutes* in intervals.icu's parser, so a literal "500m" step would be
    silently reinterpreted as 500 minutes. "km" works directly (and accepts
    a decimal, e.g. "1.0km" parses to a 1000m step). See CLAUDE.md.
    """

    if unit == "km":
        km = meters / 1000
        return f"{km:g}km"
    return f"{int(round(meters))}mtr"


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
    """Compute an absolute pace *range* target for a %VMA band.

    intervals.icu always pre-resolves pace *zone* targets (like "Z5 Pace")
    into an absolute pace using its own athlete-side zone config before a
    workout ever reaches a device - verified at the FIT byte level, see
    CLAUDE.md. Computing the pace here instead, directly from the %VMA the
    PDF actually prints and the athlete's own ``--vma``, means the main set
    no longer depends on intervals.icu's zone configuration at all - same as
    warm-up/cool-down already don't.

    This always renders as a two-sided range (e.g. "3:26-3:37/km"), even for
    short efforts - confirmed against the live API for the original sample
    and deliberately kept consistent for every %VMA-band target rather than
    collapsing short efforts to a single value (see CLAUDE.md).
    """

    speed_hi = vma * hi_pct / 100
    fast_s = 3600 / speed_hi
    if lo_pct <= 0:
        lo_pct = _OPEN_LOWER_BOUND_FLOOR_PCT
    speed_lo = vma * lo_pct / 100
    slow_s = 3600 / speed_lo
    return f"{_format_pace_value(fast_s)}-{_format_pace_value(slow_s)}/km"


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _mentions_cotes(name: str) -> bool:
    """True if a section name refers to a hill-repeats ("côtes") block."""

    return "COTE" in _strip_accents(name).upper()


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


def _expand_combined_headers(pairs: Sequence[Tuple[str, dict]]) -> List[Tuple[str, dict]]:
    """Split a "+"-joined header over its block's entries, one-for-one.

    Seen on one PDF so far: Planiteam sometimes prints a single centred
    header like "ÉCHAUFFEMENT + GAMMES" over what is, on the duration row, two
    distinct entries (a 20:00 warm-up and a 5:00 drills block) sharing one
    "1x" repeat marker. Rendering that as one combined segment would wrongly
    apply the warm-up's role/pace to the drills entry too. Splitting is only
    attempted when the number of "+"-separated name parts exactly matches
    the block's own entry count, so this never fires on an unrelated header
    that happens to contain a "+" (e.g. a page title, which isn't in the
    header band at all) - see CLAUDE.md.
    """

    expanded: List[Tuple[str, dict]] = []
    for name, block in pairs:
        parts = [p.strip() for p in name.split(" + ")]
        if len(parts) > 1 and len(parts) == len(block["entries"]):
            for part, entry in zip(parts, block["entries"]):
                expanded.append((part, {"repeat": block["repeat"], "entries": [entry]}))
        else:
            expanded.append((name, block))
    return expanded


def _parse_rest_groups(rest_row: Sequence[dict]) -> List[dict]:
    """Parse a row of ``r = <value>`` triples, keeping each group's own x0.

    Each group is ``{"x0": float, "value_s": Optional[int], "used": False}``.
    Groups are matched to duration/distance entries by nearest x0 (see
    ``_parse_main_row``) rather than by flat sequential order: some PDFs
    print an extra free-text "Consigne" annotation on the duration row that
    carries its own (otherwise unused) "r = 0" placeholder beneath it, which
    would desync a purely sequential pairing - see CLAUDE.md "Generalizing
    beyond the first sample PDF".
    """

    tokens = sorted(rest_row, key=lambda w: w["x0"])
    groups: List[dict] = []
    i = 0
    while i < len(tokens):
        if tokens[i]["text"] != "r":
            i += 1
            continue
        anchor_x0 = tokens[i]["x0"]
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
        groups.append({"x0": anchor_x0, "value_s": value_s, "used": False})
    return groups


def _consume_nearest_rest(x0: float, rest_groups: Sequence[dict], tol: float = _X0_MATCH_TOL) -> Optional[int]:
    best = None
    best_dist = None
    for group in rest_groups:
        if group["used"]:
            continue
        dist = abs(group["x0"] - x0)
        if dist <= tol and (best_dist is None or dist < best_dist):
            best = group
            best_dist = dist
    if best is None:
        return None
    best["used"] = True
    return best["value_s"]


def _parse_main_row(words: Sequence[dict]) -> List[dict]:
    """Return the ordered ``Nx ==> duration/distance / rest`` blocks on the page.

    Each block is ``{"repeat": int, "entries": [entry, ...]}`` where an
    entry is ``{"x0": float, "duration_s": Optional[int],
    "distance_m": Optional[float], "distance_unit": Optional[str],
    "rest_s": Optional[int]}`` - exactly one of ``duration_s``/``distance_m``
    is set, depending on whether this entry is a time or a distance.

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
    rest_groups = _parse_rest_groups(rest_row)

    blocks: List[dict] = []
    current: Optional[dict] = None
    for word in duration_row:
        text = word["text"]
        repeat_match = _REPEAT_RE.match(text)
        if repeat_match:
            current = {"repeat": int(repeat_match.group(1)), "entries": []}
            blocks.append(current)
            continue
        if current is None:
            continue

        duration_s: Optional[int] = None
        distance_m: Optional[float] = None
        distance_unit: Optional[str] = None
        if _DURATION_RE.match(text):
            duration_s = _parse_mmss(text)
        elif _DISTANCE_KM_RE.match(text):
            distance_m = float(_DISTANCE_KM_RE.match(text).group(1)) * 1000
            distance_unit = "km"
        elif _DISTANCE_M_RE.match(text):
            distance_m = float(_DISTANCE_M_RE.match(text).group(1))
            distance_unit = "m"
        else:
            # Not a duration/distance token - e.g. a "Consigne" free-text
            # annotation Planiteam sometimes prints inline on this row (see
            # CLAUDE.md). Skipped; its own "r = ..." placeholder (if any)
            # simply won't be within range of any real entry.
            continue

        rest_s = _consume_nearest_rest(word["x0"], rest_groups)
        current["entries"].append(
            {
                "x0": word["x0"],
                "duration_s": duration_s,
                "distance_m": distance_m,
                "distance_unit": distance_unit,
                "rest_s": rest_s,
            }
        )
    return blocks


def _parse_main_set_zones(words: Sequence[dict]) -> List[Tuple[float, str]]:
    """Zone labels (e.g. "Z5"/"Z1") overlaid above the main interval block(s),
    kept with their own x0 so they can be matched to the nearest entry."""

    zone_words = sorted((w for w in words if _ZONE_RE.match(w["text"])), key=lambda w: w["x0"])
    return [(w["x0"], w["text"]) for w in zone_words]


def _parse_main_set_zone_percents(words: Sequence[dict]) -> List[Tuple[float, Tuple[int, int]]]:
    """The "95-100%"/"0-65%" %VMA bands overlaid alongside the zone labels,
    kept with their own x0 so they can be matched to the nearest entry."""

    percent_words = sorted((w for w in words if _PERCENT_RANGE_RE.match(w["text"])), key=lambda w: w["x0"])
    return [(w["x0"], tuple(int(v) for v in _PERCENT_RANGE_RE.match(w["text"]).groups())) for w in percent_words]


def _parse_pace_table(words: Sequence[dict]) -> Dict[float, List[Tuple[float, int, str]]]:
    """Parse the VMA reference table into ``{vma: [(x0, seconds, kind), ...]}``.

    Column count and kind vary by PDF:

    - A 2-column table (a warm-up and a cool-down pace) on most workouts.
    - One column per session phase (warm-up, each main-set entry,
      cool-down) on workouts with no Z-zone/%VMA overlay - see
      ``parse_planiteam_pdf``.

    ``kind`` is ``"pace"`` for an apostrophe-formatted column (e.g.
    "7'42/km" - already a direct pace/km) or ``"raw"`` for a colon-formatted
    column (e.g. "05:41" - the time to cover *this entry's own distance* at
    that VMA row, not a pace/km; needs rescaling by the entry's own distance
    before use, see ``_pace_cell_to_target``). Both kinds are seen within the
    same table on VMA-split-distance workouts (warm-up/cool-down are
    apostrophe, the distance entries' own split time is colon).

    Rows are anchored on their VMA-value label (the leftmost column, e.g.
    "17.5") rather than a fixed row-clustering tolerance: the label's exact
    vertical offset from its own pace values varies by a couple of points
    between PDFs (different fonts/rendering for a longer table), enough to
    fall outside a tight tolerance and silently drop the row.

    The label column's own x0 is *not* a fixed page coordinate either - most
    PDFs print it around x0 35-65, but one sample (a longer title needing
    different centring) shifted the whole table right, to x0 ~123. Labels
    are instead found as whichever VMA-shaped words sit closest to the
    leftmost such word on the page (small spread within one PDF, e.g. 4-10pt
    - the absolute position is what varies between PDFs). An earlier version
    hardcoded "x0 < 90", which silently found zero rows - and from there,
    zero pace anywhere on that workout - on that shifted PDF. See CLAUDE.md.
    """

    below_grid = [w for w in words if w["top"] > 150]
    label_candidates = [w for w in below_grid if _VMA_LABEL_RE.match(w["text"])]
    if not label_candidates:
        return {}
    min_label_x0 = min(w["x0"] for w in label_candidates)
    labels = sorted(
        (w for w in label_candidates if w["x0"] <= min_label_x0 + 15),
        key=lambda w: w["top"],
    )

    table: Dict[float, List[Tuple[float, int, str]]] = {}
    for label in labels:
        cells: List[Tuple[float, int, str]] = []
        for w in below_grid:
            if abs(w["top"] - label["top"]) > 6:
                continue
            if _PACE_RE.match(w["text"]):
                cells.append((w["x0"], _parse_pace(w["text"]), "pace"))
            elif _DURATION_RE.match(w["text"]):
                cells.append((w["x0"], _parse_mmss(w["text"]), "raw"))
        cells.sort(key=lambda c: c[0])
        if len(cells) < 2:
            continue
        table[float(label["text"])] = cells
    return table


def _lookup_pace(table: Dict[float, List[Tuple[float, int, str]]], vma: float, column: int) -> Optional[Tuple[int, str]]:
    """Look up (or linearly interpolate) the (seconds, kind) cell for a given VMA/column."""

    if not table:
        return None

    def cell(v: float) -> Tuple[int, str]:
        _, seconds, kind = table[v][column]
        return seconds, kind

    if vma in table:
        return cell(vma)

    keys = sorted(table)
    if vma <= keys[0]:
        return cell(keys[0])
    if vma >= keys[-1]:
        return cell(keys[-1])

    lower = max(k for k in keys if k < vma)
    upper = min(k for k in keys if k > vma)
    lower_s, kind = cell(lower)
    upper_s, _ = cell(upper)
    ratio = (vma - lower) / (upper - lower)
    return round(lower_s + (upper_s - lower_s) * ratio), kind


def _pace_cell_to_target(seconds: float, kind: str, distance_m: Optional[float]) -> str:
    """Turn a looked-up pace-table cell into a "/km" target string.

    A "raw" cell is the PDF's own split time for *this entry's own distance*
    (e.g. a 1000m-repeat row), not already a pace/km - rescale it. A "pace"
    cell is already a direct pace/km value. See ``_parse_pace_table``.
    """

    if kind == "raw" and distance_m:
        seconds = seconds * 1000 / distance_m
    return _format_pace(seconds)


def _nearest_payload(x0: float, candidates: Sequence[Tuple[float, object]], tol: float = _X0_MATCH_TOL):
    best = None
    best_dist = None
    for cx0, payload in candidates:
        dist = abs(cx0 - x0)
        if dist <= tol and (best_dist is None or dist < best_dist):
            best = payload
            best_dist = dist
    return best


def _nearest_column_index(x0: float, column_x0s: Sequence[float], tol: float = _X0_MATCH_TOL) -> Optional[int]:
    best = None
    best_dist = None
    for i, cx0 in enumerate(column_x0s):
        dist = abs(cx0 - x0)
        if dist <= tol and (best_dist is None or dist < best_dist):
            best = i
            best_dist = dist
    return best


def _zone_number(zone: str) -> int:
    match = re.search(r"\d+", zone)
    return int(match.group()) if match else 0


def _zone_cue(zone: str, max_zone: int, is_cotes: bool) -> str:
    # Generic effort/recovery cue, not tied to any one workout's content -
    # the highest zone number used in the block is "Effort", anything lower
    # is "Récupération". Confirmed live that this text becomes the step's
    # block name on the COROS device (see Step.cue docstring). "Cotes" is
    # appended to an effort cue when the block's own section name mentions
    # hill repeats ("côtes"), per the athlete's own labelling convention.
    if _zone_number(zone) >= max_zone:
        return "Effort Cotes" if is_cotes else "Effort"
    return "Récupération"


def _make_step(entry: dict, target: Optional[str], cue: Optional[str]) -> Step:
    if entry["distance_m"] is not None:
        return Step(distance_m=entry["distance_m"], distance_unit=entry["distance_unit"], target=target, cue=cue)
    return Step(duration_s=entry["duration_s"], target=target, cue=cue)


def _build_role_steps(
    entries: Sequence[dict],
    column_x0s: Sequence[float],
    pace_table: Dict[float, List[Tuple[float, int, str]]],
    vma: float,
) -> List[Step]:
    """Warm-up/cool-down steps: no cue (the role flag alone names the block
    correctly on-device, see Segment.role), pace from the nearest pace-table
    column to this entry's own x-position."""

    steps: List[Step] = []
    for entry in entries:
        target = None
        if pace_table:
            col = _nearest_column_index(entry["x0"], column_x0s)
            if col is not None:
                seconds, kind = _lookup_pace(pace_table, vma, col)
                target = _pace_cell_to_target(seconds, kind, entry["distance_m"])
        steps.append(_make_step(entry, target=target, cue=None))
        if entry["rest_s"]:
            steps.append(Step(duration_s=entry["rest_s"], target=None, cue=None))
    return steps


def _build_zone_steps(entries: Sequence[dict], matched: Sequence[Tuple[str, Tuple[int, int]]], vma: float, is_cotes: bool) -> List[Step]:
    """Main-set steps targeted via the Z-zone/%VMA overlay (see
    parse_planiteam_pdf). Each entry has already been matched to its own
    nearest zone+percent band by x-position.

    A trailing rest attached to an entry (e.g. the long recovery before an
    outer repeat, or the jog between distance reps) is targeted using
    whichever *other* zone is used elsewhere in this same block, if any -
    this is what the block alternates between. When the block only ever uses
    one zone (no second zone/percent token nearby for any entry - common on
    the single-effort-distance blocks), there is nothing to borrow from and
    the rest is left untargeted rather than reusing the effort's own pace
    (which would be wrong - see CLAUDE.md).
    """

    zone_numbers = [_zone_number(zone) for zone, _ in matched]
    max_zone = max(zone_numbers)
    distinct: List[Tuple[str, Tuple[int, int]]] = []
    for zone, pct in matched:
        if not any(zone == dzone for dzone, _ in distinct):
            distinct.append((zone, pct))

    steps: List[Step] = []
    for entry, (zone, pct) in zip(entries, matched):
        cue = _zone_cue(zone, max_zone, is_cotes)
        target = _vma_pace_target(pct[0], pct[1], vma)
        steps.append(_make_step(entry, target=target, cue=cue))
        if entry["rest_s"]:
            alt = next((dzone_pct for dzone_pct in distinct if dzone_pct[0] != zone), None)
            rest_target = _vma_pace_target(alt[1][0], alt[1][1], vma) if alt is not None else None
            steps.append(Step(duration_s=entry["rest_s"], target=rest_target, cue="Récupération"))
    return steps


def _build_per_step_main_set_steps(
    entries: Sequence[dict],
    column_x0s: Sequence[float],
    pace_table: Dict[float, List[Tuple[float, int, str]]],
    vma: float,
    is_cotes: bool,
) -> List[Step]:
    """Main-set steps targeted via a per-entry pace-table column (no zone
    data on this PDF at all - see ``parse_planiteam_pdf``). Effort/recovery
    is inferred purely by position (every workout seen so far starts with an
    effort and alternates), since there's no zone data to classify by."""

    steps: List[Step] = []
    for i, entry in enumerate(entries):
        cue = ("Effort Cotes" if is_cotes else "Effort") if i % 2 == 0 else "Récupération"
        target = None
        col = _nearest_column_index(entry["x0"], column_x0s)
        if col is not None:
            seconds, kind = _lookup_pace(pace_table, vma, col)
            target = _pace_cell_to_target(seconds, kind, entry["distance_m"])
        steps.append(_make_step(entry, target=target, cue=cue))
        if entry["rest_s"]:
            # Not observed in any per-step PDF so far (their rest values are
            # always blank/zero, or - when present - there's no pace-table
            # column to attribute to this extra step) - left untargeted
            # rather than guessing.
            steps.append(Step(duration_s=entry["rest_s"], target=None, cue="Récupération"))
    return steps


def _build_untargeted_main_set_steps(entries: Sequence[dict], is_cotes: bool) -> List[Step]:
    """Fallback for a block that looks like a main set (role-less, alternating
    or repeat+rest shaped) but has no pace source available at all - keeps
    the Effort/Récupération cue text without inventing a target."""

    steps: List[Step] = []
    for i, entry in enumerate(entries):
        cue = ("Effort Cotes" if is_cotes else "Effort") if i % 2 == 0 else "Récupération"
        steps.append(_make_step(entry, target=None, cue=cue))
        if entry["rest_s"]:
            steps.append(Step(duration_s=entry["rest_s"], target=None, cue="Récupération"))
    return steps


def _build_plain_steps(
    entries: Sequence[dict],
    cue: Optional[str],
    column_x0s: Sequence[float] = (),
    pace_table: Optional[Dict[float, List[Tuple[float, int, str]]]] = None,
    vma: float = DEFAULT_VMA,
) -> List[Step]:
    """A single-purpose block with no effort/recovery structure (e.g.
    Planiteam's "GAMMES" drills, or an inter-block rest) - reuses whatever
    the PDF itself calls the block as the cue.

    Usually untargeted - except a block can legitimately sit in the "warm-up
    .. cool-down" pace-table slot without carrying a warmup/cooldown *role*
    (e.g. a workout that ends on a plain tempo block instead of a cool-down -
    see CLAUDE.md). Only the table's *first or last* column is ever tried
    here (never a middle one): a middle column can sit close enough, by sheer
    page-layout coincidence, to an unrelated plain block (an inter-block
    rest) to look like a match by x-position alone without actually being
    one - the first/last slots are the only ones this template consistently
    reserves for "a block with no explicit role".
    """

    target = None
    if entries and column_x0s and pace_table:
        col = _nearest_column_index(entries[0]["x0"], column_x0s)
        if col in (0, len(column_x0s) - 1):
            seconds, kind = _lookup_pace(pace_table, vma, col)
            target = _pace_cell_to_target(seconds, kind, entries[0]["distance_m"])

    steps: List[Step] = []
    for entry in entries:
        steps.append(_make_step(entry, target=target, cue=cue))
        if entry["rest_s"]:
            steps.append(Step(duration_s=entry["rest_s"], target=None, cue=cue))
    return steps


def parse_planiteam_pdf(path: Union[str, Path], vma: float = DEFAULT_VMA) -> Workout:
    """Parse a Planiteam PDF export into a Workout, using ``vma`` (km/h) to
    resolve step paces from the PDF's reference table.

    Two different pace sources have been seen from Planiteam, and this
    dispatches between them per-block (see CLAUDE.md for how each was
    found):

    - Zone-overlay blocks: entries matched (by x-position) to a nearby
      "Z5"/"95-100%"-style overlay, converted to an absolute pace range via
      ``_vma_pace_target``.
    - Per-step blocks: no zone overlay anywhere on the page - instead one
      pace-table column per session phase (warm-up, each main-set entry,
      cool-down), matched by x-position and looked up directly.

    A block that looks like a main set (more than one entry, or a single
    entry with an attached rest - the "effort + jog between reps" shape)
    but has no pace source nearby falls back to an untargeted Effort/
    Récupération rendering rather than guessing.

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

    name_block_pairs = _expand_combined_headers(list(zip(section_names, blocks)))

    page_has_zone_data = bool(zones)
    column_x0s = [x0 for x0, _, _ in next(iter(pace_table.values()))] if pace_table else []
    use_per_step = not page_has_zone_data and len(column_x0s) > 2

    segments: List[Segment] = []
    for name, block in name_block_pairs:
        entries = block["entries"]
        if not entries:
            # A placeholder block - Planiteam sometimes prints a free-text
            # "Consigne" note instead of a measurable duration/distance here
            # (seen on two PDFs, always under a "GAMMES"-style header with no
            # fixed duration of its own) - nothing to render. See CLAUDE.md.
            continue

        name_compact = name.upper().replace(" ", "")
        role = "warmup" if "CHAUFFEMENT" in name_compact else "cooldown" if "CALME" in name_compact else None
        is_cotes = _mentions_cotes(name)
        is_main_set = role is None and (len(entries) > 1 or bool(entries[0]["rest_s"]))

        if role is not None:
            steps = _build_role_steps(entries, column_x0s, pace_table, vma)
        elif is_main_set and page_has_zone_data:
            matched = [(_nearest_payload(e["x0"], zones), _nearest_payload(e["x0"], percents)) for e in entries]
            if all(zone is not None and pct is not None for zone, pct in matched):
                steps = _build_zone_steps(entries, matched, vma, is_cotes)
            else:
                steps = _build_untargeted_main_set_steps(entries, is_cotes)
        elif is_main_set and use_per_step:
            steps = _build_per_step_main_set_steps(entries, column_x0s, pace_table, vma, is_cotes)
        elif is_main_set:
            steps = _build_untargeted_main_set_steps(entries, is_cotes)
        else:
            steps = _build_plain_steps(entries, cue=_display_name(name), column_x0s=column_x0s, pace_table=pace_table, vma=vma)

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
