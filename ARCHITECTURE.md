# Architecture of `planiteam_to_coros.py`

This is a map of how the script turns a Planiteam PDF into intervals.icu
structured-workout text. For *why* each design choice was made (and the live
API probing that justified it), see [CLAUDE.md](CLAUDE.md) - this file is
just the shape of the logic.

## 1. End-to-end pipeline

```mermaid
flowchart TD
    PDF["Planiteam PDF\n(single page)"] --> EXTRACT["_extract_page_words()\npdfplumber: words + x0/x1/top positions"]

    EXTRACT --> TITLE["_parse_title()"]
    EXTRACT --> HEADERS["_parse_section_headers()\n(ÉCHAUFFEMENT, GAMMES, ...)"]
    EXTRACT --> MAINROW["_parse_main_row()\nNx ==> duration/distance, r = rest"]
    EXTRACT --> ZONES["_parse_main_set_zones()\n+ _parse_main_set_zone_percents()\n(Z5 / 95-100% overlay, if any)"]
    EXTRACT --> PACETABLE["_parse_pace_table()\nVMA reference table"]

    HEADERS --> EXPAND["_expand_combined_headers()\nsplit a '+'-joined header\nacross its block's entries"]
    MAINROW --> EXPAND

    EXPAND --> DISPATCH{{"parse_planiteam_pdf()\nper-block dispatch\n- see diagram 2 -"}}
    ZONES --> DISPATCH
    PACETABLE --> DISPATCH

    DISPATCH --> SEGMENTS["list[Segment]\n(each holding list[Step])"]
    TITLE --> WORKOUT["Workout"]
    SEGMENTS --> WORKOUT

    WORKOUT --> TEXT["to_intervals_icu_text()\nintervals.icu line syntax"]
    WORKOUT --> JSONOUT["to_coros_json()\ndebug/alternate output"]

    TEXT --> PUSH["push_event() / push_to_recipients()\nPOST .../events (HTTP Basic, API_KEY)"]
    PUSH --> ICU["intervals.icu calendar\n(-> synced to COROS watch,\nconfigured on intervals.icu's site)"]
```

## 2. Per-block target/cue dispatch

This is the core of `parse_planiteam_pdf()`: for every `(name, block)` pair
(after `+`-header expansion), decide **how to label and target** each entry.
Matching between an entry and its zone/percent/pace-table data is always
done by **nearest x-position** (`_nearest_payload` / `_nearest_column_index`,
tolerance `_X0_MATCH_TOL`), never by a flat count or sequence index - that
was the main source of silent breakage across PDF layouts (see
`CLAUDE.md`).

```mermaid
flowchart TD
    START(["(name, block) pair"]) --> EMPTY{"block has\nentries?"}
    EMPTY -- "no (a 'Consigne'\nplaceholder block)" --> SKIP["skip - nothing to render"]
    EMPTY -- yes --> ROLE{"name contains\nCHAUFFEMENT / CALME?"}

    ROLE -- "warmup / cooldown" --> ROLESTEPS["_build_role_steps()\nno cue (role flag alone\nnames the step on-device);\npace = nearest pace-table\ncolumn to the entry's x0"]

    ROLE -- "no role" --> MAINSET{"is_main_set?\n(>1 entry, OR\n1 entry + attached rest)"}

    MAINSET -- no --> PLAIN["_build_plain_steps()\ncue = _display_name(name)\n(e.g. 'Gammes'); never targeted"]

    MAINSET -- yes --> HASZONE{"any Z#/%VMA\noverlay anywhere\non the page?"}

    HASZONE -- yes --> MATCHZONE{"nearest zone + percent\nfound for EVERY entry\nin this block?"}
    MATCHZONE -- yes --> ZONESTEPS["_build_zone_steps()\ncue: Effort[ Cotes]/Récupération\nby zone number vs block max;\ntarget: _vma_pace_target(lo,hi,vma)\n(always a range, e.g. 3:26-3:37/km);\na rest's target borrows the block's\nOTHER zone if one exists, else None"]
    MATCHZONE -- no --> UNTARGETED

    HASZONE -- no --> PERSTEP{"pace table has\n>2 columns?"}
    PERSTEP -- yes --> PERSTEPSTEPS["_build_per_step_main_set_steps()\ncue alternates Effort/Récupération\nby position (no zone data to\nclassify by); target = nearest\npace-table column to entry's x0\n(rescaled if the column is a raw\nsplit-time, e.g. distance entries)"]
    PERSTEP -- no --> UNTARGETED["_build_untargeted_main_set_steps()\ncue alternates Effort/Récupération\nby position; no target available"]

    ROLESTEPS --> SEGMENT["Segment(name, repeat, steps, role)"]
    PLAIN --> SEGMENT
    ZONESTEPS --> SEGMENT
    PERSTEPSTEPS --> SEGMENT
    UNTARGETED --> SEGMENT
```

## Key data shapes

- **`Step`** - one rendered line (`- [cue] duration-or-distance [target Pace]`).
  Exactly one of `duration_s` / `distance_m` is set.
- **`Segment`** - a named block (warm-up, main set, ...): `repeat` count,
  `role` (`"warmup"`/`"cooldown"`/`None`, drives the device step-type flag),
  and its `Step`s.
- **`Workout`** - `title` + ordered `Segment`s; renders to either
  intervals.icu's line syntax or a debug JSON doc.
- **entry** (internal, not a public type) - one parsed cell from the
  duration/distance row: `{x0, duration_s, distance_m, distance_unit,
  rest_s}`. `rest_s` is the attached "r = ..." gap *after* this entry,
  matched by nearest x-position rather than by sequence order (see
  `_parse_rest_groups` / `_consume_nearest_rest` in CLAUDE.md).

## Where to look when a new PDF breaks this

1. Dump `page.extract_words()` sorted by `(top, x0)` for the new PDF and
   compare its structural landmarks (`==>`, `r =`, `Nx`, `Z\d`, pace-table
   column x0s) against a working sample - see CLAUDE.md's "Practical notes
   for extending the parser".
2. Everything in diagram 2 is decided *per block*, by x-position matching,
   not by any fixed page coordinate or flat count - if something doesn't
   line up, the fix is almost always "match this by nearest x0 instead of
   assuming a fixed position/order", the same pattern used throughout this
   file.
3. Add the new PDF + a hand-checked `expected.workout.intervals.txt` under
   `example/<date>/` and wire it into `tests/test_planiteam_to_coros.py`.
