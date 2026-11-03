# Architecture

How a Planiteam PDF becomes intervals.icu structured-workout text. For *why*
each rule exists (and the live API probing behind it), see
[CLAUDE.md](CLAUDE.md).

## Modules

| Module | Role |
|---|---|
| `planiteam_to_coros.py` | PDF -> VMA-independent `Workout` -> text for a given VMA. Also the CLI. |
| `intervals_icu.py` | Recipients list, event payload, push (one event per recipient). |
| `main.py` | Cloud Function: parse the PDF once, push to every recipient. |

## Pipeline

```mermaid
flowchart TD
    PDF["Planiteam PDF (single page)"] --> WORDS["pdfplumber extract_words()\nwords + x0/top/bottom"]
    WORDS --> GRID["_read_page(): locate the grid\n'==>' rest row, then the nearest 'Nx' row above it"]
    GRID --> TITLE["title: words above the first row's bottom"]
    GRID --> HEADERS["headers: words between title and grid\n(_parse_section_headers)"]
    GRID --> BLOCKS["blocks: 'Nx' + entries + nearest 'r = rest'\n(_parse_blocks)"]
    GRID --> BELOW["below the grid: Z# labels, %VMA bands,\npace-table columns (_parse_pace_columns)"]
    HEADERS --> PAIR["pair headers with blocks 1:1\n(count mismatch -> ValueError)\nsplit 'A + B' headers"]
    BLOCKS --> PAIR
    PAIR --> PLAN["_build_segments(): classify each entry"]
    BELOW --> PLAN
    PLAN --> WORKOUT["Workout(title, segments)\ntargets stored as %VMA"]
    WORKOUT --> TEXT["to_intervals_icu_text(vma)"]
    TEXT --> PUSH["intervals_icu.push_to_recipients()\none render + POST per recipient VMA"]
```

Only relative positions are used: the grid rows are found from their
content, and everything else is defined as above, between or below them.
Entries are matched to rests, zone labels, %VMA bands and pace columns by
**nearest x0** within `_X_TOL` (20pt), never by order or count.

## Entry classification

```mermaid
flowchart TD
    E(["block entries"]) --> LAP{"'Lap' entry?"}
    LAP -- yes --> LAPSEG["own segment: 'Gammes Press lap',\nkeeps the block's role"]
    LAP -- no --> ROLE{"header has CHAUFFEMENT / CALME?"}
    ROLE -- yes --> ROLESEG["Warmup / Cooldown segment\nno cue, no target"]
    ROLE -- no --> MULTI{"> 1 entry, or 1 entry\nwith an attached rest?"}
    MULTI -- no --> PLAIN["plain step, cue = header name\ntarget only if its %VMA says effort"]
    MULTI -- yes --> INT["interval steps"]
    INT --> INTENSITY["intensity = nearest Z# label +\nnearest %VMA band, else nearest pace column's %VMA"]
    INTENSITY --> KIND{"recovery?\nZ<=2, else %VMA < 70"}
    KIND -- "unknown" --> ERR["ValueError"]
    KIND -- recovery --> REC["'Récupération' [Zn on côtes]\nno target"]
    KIND -- effort --> EFF["'Effort' / 'Effort Cotes Zn'\ntarget = %VMA (none on côtes)"]
```

An attached `r = rest` always becomes an extra untargeted `Récupération` step.

## Key data shapes

- **`Step`**: `duration_s` or `distance_m`, `cue`, `pace_pct` (`(lo, hi)`
  %VMA; equal bounds = single pace), `until_lap_press`.
- **`Segment`**: `name`, `repeat`, `role` (`"warmup"`/`"cooldown"`/`None`), steps.
- **`Workout`**: `title` + segments; `to_intervals_icu_text(vma)` renders it.
- **`_PaceColumn`**: one pace-table column; `vma_pct(distance_m)` averages
  the %VMA implied by every row (split-time cells are rescaled by distance).

## When a new PDF breaks

1. Run it through the CLI; the `ValueError` message names what didn't fit.
2. Dump `page.extract_words()` sorted by `(top, x0)` and compare landmarks
   (`==>`, `r =`, `Nx`, `Z\d`, pace cells) with a working sample.
3. Add the PDF and a hand-checked `expected.workout.intervals.txt` under
   `example/<date>/`; `tests/test_examples.py` picks it up automatically.
