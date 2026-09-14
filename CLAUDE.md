# planiteam-to-coros

Converts a Planiteam workout PDF export into an intervals.icu planned
calendar event (pushed via the intervals.icu API), which intervals.icu then
syncs to a COROS watch — that device sync is configured on intervals.icu's
website, out of scope for this repo.

There is no Planiteam API; PDF export is the only available data source, and
it is a genuinely awkward one. The first half of this file documents what was
reverse-engineered about its structure. The second half documents the
intervals.icu push side, which turned out to have its own non-obvious traps —
**verified live against a real account**, not just read from docs (see
"Verify against the live API, not just the docs" below for why that mattered).

## The PDF is not a text document, it's a coordinate dump

Planiteam's PDF places section titles, the interval grid, and a pace
reference table as independently-positioned text objects. A naive linear text
extraction (`pypdf.extract_text()`, or `pdfplumber`'s own `.extract_text()`)
interleaves unrelated rows into garbage. **Always parse via word positions**
(`page.extract_words()` in `pdfplumber`, which gives `x0/x1/top` per word),
never via the flattened text string. `planiteam_to_coros.py` does this
throughout; `Workout.source_text` keeps the flattened text only for
debugging/JSON dumps, it is never parsed.

Reference example: `example/2026-09-10/cotes-courtes-2x6x20-.pdf`, matching
the screenshot the user provided when this was built (a "Côtes courtes :
2x6x20"" session — 20' warm-up, 10' drills, 2×[6×(20s effort/40s jog) with a
3' recovery before the second set], 10' cool-down, total 56:40). Two more
real PDFs arrived later with structurally different layouts
(`example/2026-09-15/`, `example/2026-09-17/`) - see "Generalizing beyond
the first sample PDF" near the end of this file for what they broke and how.
Each `example/<date>/` folder is named after the session's actual date (from
the notification email, not the PDF - it has no date field, see below).

## Layout of the page (single page, fixed template)

| Region | `top` range | Content |
|---|---|---|
| Title | ≤ 60 | `TITLE ... Édité le DD/MM/YYYY` — title words are left-aligned (small x0), the edit-date is far right (x0 > 700). No separating token; split by scanning until the word `"Édité"`. **The scheduled workout date is not in the PDF at all** — only the PDF's *export* date. |
| Section headers | ~95–120 | e.g. `ÉCHAUFFEMENT`, `GAMMES`, `2X6X20" CÔTES`, `RETOUR AU CALME` |
| Interval grid, duration row | ~128–136 | `1x`, `20:00`, `1x`, `10:00`, `2x`, `00:20`, `00:40`, ... |
| Interval grid, rest row | ~140–150 | `r = 0`, `r = `, `r = 3:00`, one under each duration cell |
| Zone-label overlay | coincides with pace-table rows 18/18.5 | `Z5 Z1 Z5 Z1 ...` and `95-100% 0-65% ...` |
| VMA pace reference table | > 150 | rows for VMA 12 → 23, two pace columns |
| Footer | far bottom | `© PlaniTeam` |

### Section headers vs. interval blocks: match by order, not by x-position

Headers are **centered** over their block, not left-aligned to it, so you
cannot line up a header's x0 with a block's x0. What *does* work: the number
of section headers always equals the number of `Nx ==>` blocks on the
interval row, and both are read left-to-right — zip them by index.

Headers can also wrap onto a second physical text row with **no space** at
the join, purely because the label is long (`ÉCHAUFFEM` / `ENT` →
`ÉCHAUFFEMENT`). Reconstructing this generically (word-wrap vs. two separate
words needing a space, e.g. `RETOUR`/`AU`/`CALME`) isn't reliably decidable
from position alone since the whole template is upper-case. Current approach:
cluster header words by horizontal proximity across rows (regardless of
y-position) to group multi-row headers, then join with spaces, with one
hardcoded fixup for the known no-space wrap (`_HEADER_JOIN_FIXUPS` in
`planiteam_to_coros.py`). If a new PDF has a different long header that wraps
mid-word, add its fixup there.

### Interval grid semantics

Each block is `Nx ==> duration₁ (r = rest₁) duration₂ (r = rest₂) ...`.
`rest` is the *additional* pause after that specific duration, before the
next one — usually `0` or blank (blank specifically appears after **effort**
durations, since the very next duration token, the recovery, already covers
that gap). The one meaningful non-zero `r` value in the sample
(`r = 3:00` after the 6th and last `00:20`) is the recovery **before the
outer `Nx` repeats**, not a 7th interval.

So: a block with a repeat count > 1 and alternating durations models a
"repeat this whole sequence N times" structure; the trailing non-zero rest on
the last entry becomes an extra step, tagged with whichever zone the *other*
duration value uses (see `_apply_zone_targets`) since the block only
alternates between one effort zone and one recovery zone.

Verification that this reconstruction is right: summing all step durations ×
repeat counts across the whole workout reproduces the exact total (`56:40`)
that Planiteam's own UI displays for this session. This is the cheapest
sanity check to rerun on any new sample PDF.

### Zone-label overlay row is a coincidence, not a real table row

The `Z5 Z1 Z5 Z1 ...` and `95-100% 0-65% ...` tokens are printed at the same
`top` as pace-table rows for VMA 18 and 18.5 purely because that's where they
land vertically on the page — they are **not** data about VMA 18. They are
metadata for the interval grid above (11 tokens, one per duration entry in
the main set: 6× `Z5` for the 6 efforts, 5× `Z1` for the 5 short recoveries).
Match them to the main block's entries by x-order / count, not by which pace
row they visually overlap. `_ZONE_RE` (`^Z\d+$`) finds these anywhere on the
page — filtering by regex is what keeps them from being mis-parsed as pace
table content.

### VMA pace reference table: what the two columns actually mean

Rows are the standard French "VMA test" reference speeds (12.0 → 23.0 km/h,
half-steps in the upper range), **not** the athlete's own VMA. Each row has
two pace columns. This was reverse-engineered empirically, not documented
anywhere:

- col1 (left, x0 ≈ 85) = pace at **60% of that row's VMA**
  (`pace_min_per_km = 100 / vma_row`)
- col2 (right, x0 ≈ 771) = pace at **55% of that row's VMA**
  (`pace_min_per_km ≈ 109.1 / vma_row`)

The way this was confirmed: the screenshot's warm-up pace (`5:43/km`) and
cool-down pace (`6:14/km`) exactly match row **17.5**'s col1 and col2. So:
**col1 is the warm-up pace, col2 is the cool-down pace, for the row matching
the athlete's own VMA.** The athlete's actual VMA is a personal setting that
does **not** appear anywhere in the PDF — it must be supplied externally
(`--vma`, default `17.5`, the repo owner's value — see `DEFAULT_VMA` in
`planiteam_to_coros.py`).

The main set's effort/recovery zones use the `Z5`/`Z1` labels directly
instead of being converted through this table (see below) — those two
columns are only used for the non-interval (warm-up/cool-down) blocks,
matched by section name containing `"CHAUFFEMENT"` or `"CALME"`.

Unresolved/unverified: what the 60%/55% figures actually correspond to
semantically (e.g. "EF" pace, "Récup" pace) — the screenshot's own badges say
`EF 40%` and `Récup 55%`, which only half-matches (55% does, 40% doesn't).
Those badge percentages are plausibly %HRmax rather than %VMA, i.e. a
different axis entirely. This was left unresolved since the *numeric pace
values* are independently verified correct (they match the screenshot's own
computed output), regardless of what the "40%"/"55%" badges mean.

### Why Z5/Z1 are kept as literal intervals.icu zone tokens

The PDF's main-set zone labels (`Z5`, `Z1`) are reused verbatim as
intervals.icu zone shorthand rather than converted to explicit VMA-derived
paces. This assumes the athlete's intervals.icu pace-zone configuration
roughly matches Planiteam's zone numbering (Z5 ≈ 95-100% VMA effort, Z1 ≈
0-65% VMA recovery). This is an assumption, not verified against the user's
actual intervals.icu zone config — flag it if a converted session looks
wrong on intervals.icu/COROS, and convert to computed pace ranges instead
(`vma × %` bounds are easy to derive from the same `--vma` value already
being plumbed through).

## The "mojibake" that isn't

Printing extracted text to a Windows terminal shows accented characters
(É, Ô, é...) as `�`. **This is a terminal/console-encoding display artifact,
not corrupted data** — `sys.stdout.encoding` in this environment is `cp1252`
and the console can't render the actual glyph, but the underlying Python
string holds the correct Unicode codepoint (verified: `ord(char)` gives the
right value, e.g. `0xD4` = `Ô`, and round-tripping through a UTF-8 file and
reading it back with `encoding="utf-8"` preserves it correctly). Don't chase
this as a bug — just make sure output is always written with
`encoding="utf-8"` (already done throughout `planiteam_to_coros.py`), and use
a file round-trip rather than `print()` if you need to eyeball extracted
accented text while debugging in this shell.

## Practical notes for extending the parser

- `pdfplumber` was chosen over `pypdf` specifically for `extract_words()`'s
  positional data; don't regress back to plain text extraction.
- The whole page-region `top` bands above (title/headers/grid/rest/table)
  are specific to this one fixed PDF template. If a differently-shaped
  Planiteam workout (e.g. more/fewer sections, no repeat block, multiple
  interval blocks) is supplied, re-derive the bands the same way this file
  describes: dump `page.extract_words()` sorted by `(top, x0)` and look for
  the same structural landmarks (`==>`, `r =`, `Nx`, `Z\d`, `\d+'\d{2}/km`).
- Cross-check any new sample against its app screenshot the same way this one
  was: total duration must match the UI's stated duration, and any pace
  shown in the UI should match a value derivable from the parsed data. Don't
  trust a structurally-plausible parse without this numeric cross-check —
  it's what caught that the original hardcoded draft was wrong.
- If a new PDF's date field is needed, `Workout.date` is currently always
  `None` — the PDF only has an "edited on" date, not the scheduled session
  date, and there is nowhere else in this repo's inputs to source it from
  (would need to come from a CLI flag or Planiteam calendar export instead).

## Pushing to intervals.icu

`push_event()` / `build_event_payload()` create a planned calendar event via
`POST /api/v1/athlete/{id}/events` (`--push --date YYYY-MM-DD` on the CLI).
Credentials come from `INTERVALS_ICU_API_KEY` / `INTERVALS_ICU_ATHLETE_ID`
(env vars, auto-loaded from a gitignored `.env` via `python-dotenv`) — get
them from `https://intervals.icu/settings` → Developer Settings.

### Verify against the live API, not just the docs

The schema was pulled from the real OpenAPI spec at `https://intervals.icu/api/v1/docs`
(fetch it with `curl`, not `WebFetch` — the tool's summarizing pass truncates
a spec this large; `curl -s .../api/v1/docs -o spec.json` then `json.load`
and index into it directly). That confirmed the endpoint, field names, and
auth scheme (HTTP Basic, username literally `API_KEY`, password = the API
key) — see `INTERVALS_ICU_BASE_URL`/`push_event` in `planiteam_to_coros.py`.

That was necessary but **not sufficient**. The schema tells you a field is
accepted; it doesn't tell you the request will be understood the way you
expect. The first live push here returned `200 OK` with no `push_errors`,
and still silently produced a wrong workout — nearly a full missed repeat
and every zone mapped to the wrong axis (power instead of pace). **A
successful-looking response is not confirmation the workout was parsed as
intended** — always `GET` the event back afterwards and inspect
`workout_doc` (the server's own parse of your `description` text) and
`moving_time` (cross-check it against the total you computed locally, the
same way the PDF parsing itself was verified). Don't trust the request that
sent the data; trust the one that reads back what the server actually stored.

Two traps this caught, both invisible from the API schema alone (the schema
only says `description` is a `string` — the actual grammar of that string is
undocumented in the OpenAPI spec and lives only in intervals.icu's forum:
https://forum.intervals.icu/t/workout-builder-syntax-quick-guide/123701):

- **Repeat blocks**: the header line is a *bare* `2x` (no leading `- `), and
  its child steps are plain `- ` bullets at the **same** indentation as
  everything else (not nested/indented under the `2x` line). The forum guide
  also asks for a blank line before and after each repeat block. Getting this
  wrong doesn't error — intervals.icu just silently treats the whole thing as
  flat, unrepeated steps, so a "2x(...)" block quietly executes once instead
  of twice. `Workout.to_intervals_icu_text()` joins every top-level segment
  with a blank line (`"\n\n".join(...)`), which satisfies the blank-line
  convention as a side effect.
- **Pace/zone targets need an explicit `Pace` suffix.** A bare zone like
  `Z5` defaults to a **power** zone (bike-oriented default, wrong for a
  running workout) unless written `Z5 Pace`. A bare absolute pace like
  `5:43/km` is **silently dropped** (not an error, just absent from the
  parsed `workout_doc`) unless written `5:43/km Pace`. `Step.render()` always
  appends `" Pace"` after a non-empty target for this reason — if this repo
  ever needs a non-running sport, that suffix needs to become conditional.

### `uid`/`upsertOnUid` does not provide idempotent re-push

An earlier version of this code tried to make repeated pushes idempotent by
sending a deterministic client-generated `uid` (from title+date) with
`upsertOnUid=true`, expecting a second push for the same session to update
the first event instead of duplicating it. **This does not work**: verified
live that intervals.icu ignores any client-supplied `uid` on creation and
always assigns its own server-generated UUID — so a client-side "the uid
should already match" strategy can never find anything to update, since
nothing with that uid was ever actually stored. This was caught by pushing
twice and diffing the event IDs (got two different ones), then confirming
via `GET` that the stored `uid` field didn't match what was sent.

Current behavior (deliberately simple, matching what the API actually does):
every `push_event()` call creates a **new** event; `upsertOnUid` is always
sent as `false`. Re-running `--push` for the same PDF/date will duplicate the
event — clean up stray ones by hand, or `DELETE
/api/v1/athlete/{id}/events/{eventId}`. If real idempotency is wanted later,
it would need the *server-returned* `id`/`uid` cached locally after a
successful push (e.g. a sidecar file keyed by PDF+date) and reused as a `PUT`
target on the next run — this was consciously not built since nobody asked
for it yet and it adds real state-management complexity.

### Getting a step recognised as Warmup/Cooldown on the device

intervals.icu's structured-workout text supports a bare `Warmup` or
`Cooldown` line as a section header immediately before a block. This is not
just a readability label — **confirmed live** (push a probe event, `GET` it
back, inspect `workout_doc.steps`) that it is the only thing that makes
intervals.icu tag that step `"warmup": true` / `"cooldown": true` in
`workout_doc`, which is what should let it sync to the watch as the correct
step type instead of a generic interval/active step. A step with no such
header, or headed by anything else (including the *French* label Planiteam
itself uses, `ÉCHAUFFEMENT` — tested, does **not** work), gets no such flag.
It must be the literal English keyword.

This is implemented as `Segment.role` (`"warmup"` / `"cooldown"` / `None`),
set once in `parse_planiteam_pdf` from the same name-matching already used
for pace lookup, then consumed by `to_intervals_icu_text()` to prepend the
keyword line. If a new PDF template uses different section names, the
matching there (`"CHAUFFEMENT" in name.upper()` / `"CALME" in name.upper()`)
is the place to extend.

Confirmed on a real watch: after pushing a corrected event, the user saw it
auto-sync into the COROS app's Training Calendar with the warm-up/cool-down
correctly typed. So this mechanism is verified end-to-end, not just on
intervals.icu's side.

### There is no equivalent keyword for "Recovery" steps inside a repeat

Tried to get the main set's `Z1` jog steps tagged the same way (COROS calls
this step type "Récupération"). **Exhaustively tested and it doesn't exist**
— four live probes, all confirmed via `GET`+`workout_doc` inspection, then
cleaned up (events deleted):

1. `Recovery`/`Interval` as a **leading word on the same line** as a step
   (`- Recovery 40s Z1 Pace`): parsed, but only becomes a free-text `"text"`
   cue on that step (e.g. `{"text": "Recovery", "pace": {...}, "duration": 40}`)
   — a label, not a structured type flag. No `"recovery": true` equivalent
   appears anywhere.
2. `Recovery` as its **own header line between the two child steps inside a
   repeat** (mirroring how `Warmup`/`Cooldown` work): silently dropped
   entirely — doesn't even survive as a text cue, the step comes back with
   no trace of it.
3. `Recovery` as a **standalone top-level segment header** (same shape as
   the working `Warmup`/`Cooldown` test, just not nested in a repeat):
   also dropped entirely, same as `Rest`.
4. `Rest` as a standalone top-level header: same result as `Recovery` — dropped.

Conclusion: **`Warmup` and `Cooldown` are the only two special step-type
keywords intervals.icu's text format recognises.** There's no text-syntax
equivalent for `Recovery`/`Rest`/`Active`/`Interval` — those FIT-style step
types (if COROS's own display logic distinguishes them at all) are most
likely inferred device-side from the step's pace/power **zone number**
itself (e.g. zone 1 conventionally means "easy/recovery" across most
platforms' zone models), not from anything intervals.icu's API exposes as a
settable field. This repo doesn't control that — the main set's `Z1 Pace`
target is already the most it can do; if COROS shows "Récupération" for
those steps, it's reading that from the low zone number, not from a flag
this code could set differently.

The one real (if partial) lever available: option 1 above (`Recovery` as
leading same-line text) genuinely works as a text cue. **This is now wired
up** (see next section) — it doesn't change the step's *type*, but it does
become the step's displayed block name on the device, which is what was
actually being asked for.

### Every block gets a device-visible name via `Step.cue`

`Step.cue` is free text rendered before the duration on a step's line (e.g.
`Effort 20s Z5 Pace`). Confirmed live (push, `GET`, inspect `workout_doc`)
that it lands as that step's `"text"` field, and confirmed by the user on a
real device that this is what shows as the block's name in the COROS app
(their app showed unlabeled numbered blocks for every step without one -
`Warmup`/`Cooldown` were already named correctly via the role flag, but the
drills block and every individual interval/recovery step were just numbers).

Rules implemented in `parse_planiteam_pdf`/`_apply_zone_targets` (kept
deliberately generic - not specific to hill-sprint workouts, since any
Planiteam PDF this parses could be a different session shape):

- Warm-up/cool-down: **no cue** - the `warmup`/`cooldown` flag alone already
  produces a correctly-localised device label (confirmed: shows as
  "Echauffement"/"Retour au calme" in French even though the flag is set via
  the literal English keyword - COROS localises the *flag*, not any text we
  send). Adding a redundant cue here was deliberately not attempted; it's
  unverified whether a cue would coexist with the flag's label or clobber it,
  and there was no reason to risk the thing that already works correctly.
- Any other single-step block (e.g. Planiteam's "GAMMES" drills, `role is
  None` and not the main interval set): cue = that segment's own section
  name, title-cased (`_display_name`) - e.g. `GAMMES` → `Gammes`. This reuses
  whatever the PDF itself calls the block rather than hardcoding a label, so
  it stays meaningful for a differently-shaped workout.
- Main-set steps (the `Z5`/`Z1`-style alternating block): cue = `"Effort"` for
  whichever zone number is highest in that block, `"Récupération"` for any
  lower zone (`_zone_cue`/`_zone_number`). This is a **binary** effort-vs-
  recovery heuristic - fine for the simple two-zone alternating pattern seen
  so far, but would mislabel a block with more than two distinct zones (e.g.
  a three-step pyramid) since everything below the single highest zone would
  be called "Récupération" even if it's still a moderately hard effort. No
  such PDF has been seen yet to design against; revisit if one shows up.
  (Note: the zone *letters* `Z5`/`Z1` are still parsed and still drive this
  cue internally - they're just never sent to intervals.icu as a target any
  more, see next section.)

### Main-set targets are computed from %VMA directly, not sent as intervals.icu zones

Originally the main set's targets were `Z5 Pace`/`Z1 Pace` (an intervals.icu
zone reference). Asked whether that could stay a *reference* all the way to
the watch, so the watch's own threshold-pace estimate decides the actual
pace - avoiding keeping intervals.icu's `threshold_pace` manually in sync
with whatever COROS estimates on its own. Checked at the actual byte level
rather than guessed: **it can't, so this was changed instead.**

FIT's `workout_step` message *does* have a mechanism for a device-resolved
target: a `target_speed_zone` field that (per the Garmin FIT SDK convention,
same idea as `target_hr_zone` for heart rate) can hold a small zone-index
integer and leave the device to resolve it against its own configured zones,
instead of a pre-computed range. intervals.icu just never uses it. Downloaded
the actual `.fit` export intervals.icu hands to a device
(`GET /api/v1/athlete/{id}/events/{eventId}/download.fit`, parsed with the
`fitdecode` package — a one-off diagnostic, not added to `requirements.txt`)
for the `Z5`/`Z1`-targeted version of this workout and inspected every
`workout_step`:

```
target_type: 'speed', target_speed_zone: 0,
custom_target_speed_low: 3.802, custom_target_speed_high: 3.932   # Effort (Z5)
target_type: 'speed', target_speed_zone: 0,
custom_target_speed_low: 2.357, custom_target_speed_high: 2.947   # Récupération (Z1)
```

`target_speed_zone` was `0` (unused) on **every single step**, including the
ones written as bare `Z5 Pace`/`Z1 Pace`. intervals.icu always resolves a
pace zone into an absolute `custom_target_speed_low/high` range (m/s)
server-side, using the athlete's *own intervals.icu* pace-zone configuration,
before it ever reaches the FIT file. This also explained the earlier
`null-null` symptom cleanly: there was nothing downstream to fall back to
when `threshold_pace` was unset — resolution happens once, here, or not at
all. So the double source of truth (COROS's own pace estimate vs.
intervals.icu's `threshold_pace`) would be unavoidable for as long as pace
*zones* are what gets sent for the main set - no config flag or alternate
text syntax defers that resolution to the device.

**Fix implemented**: `_vma_pace_target` (called from `_apply_zone_targets`,
using the %VMA bands parsed by `_parse_main_set_zone_percents` - the
"95-100%"/"0-65%" text overlaid next to the `Z5`/`Z1` labels, see the zone-
label-overlay section above) computes an absolute pace directly from the
PDF's own %VMA figures and `--vma`, the same way `_lookup_pace` already did
for warm-up/cool-down. E.g. for VMA 17.5: `95-100%` → `3:26-3:37/km`,
`0-65%` → `5:16/km` (a single value, not a range - see below). Verified this
target syntax works the same way as the earlier syntax fixes: pushed a probe
with `20s 3:26-3:37/km Pace`, `GET` it back, confirmed
`workout_doc` shows `{"pace": {"units": "secs/km", "start": 206, "end": 217}}`
- a real absolute range, no `pace_zone` unit anywhere. Then re-pushed the
real event and confirmed the same on it (`moving_time` still 3400, correct
`secs/km` values on every step).

This does **not** eliminate a second number to maintain - the athlete's VMA
in `--vma` still needs updating by hand occasionally - but it does eliminate
intervals.icu's zone config specifically as a dependency: the main set no
longer cares whether `threshold_pace` is set at all, same as warm-up/
cool-down already didn't.

**0% lower bound**: a %VMA band's lower bound of 0 (the actual PDF value for
the recovery zone, `0-65%`) has no finite pace - 0% VMA is a dead stop, and
means "go as slow as you like." The first attempt collapsed that case to a
single pace value (just the band's faster/upper-% edge, e.g. `5:16/km` for
`0-65%`) instead of an undefined open range. **That backfired in practice**:
confirmed on the real watch that COROS renders a bare single-value pace
target with its own tight auto-generated tolerance band - the user saw
`5:16/km` show up on-device as `5'08/km - 5'24/km` (~2.5% either side),
which reads as "hit close to this pace," the opposite of the intended "no
real floor" meaning, and actively counter-productive for what's supposed to
be a recovery interval.

Fixed by flooring an open (`lo_pct <= 0`) lower bound at a fixed
`_OPEN_LOWER_BOUND_FLOOR_PCT = 40` (%VMA) instead of leaving it as a single
value, producing an explicit, honestly-wide range (`5:16-8:34/km` for
VMA 17.5's `0-65%`) that a device renders as given, no auto-banding. This
constant is a deliberate product choice ("a very easy jog"), not extracted
from any PDF or derived from anything - the user explicitly preferred a
fixed %VMA floor over the alternative that was considered (reusing the
warm-up segment's own pace as the floor, which was rejected for silently
assuming every workout has a warm-up to borrow from). Verified server-side
after re-pushing: `workout_doc` shows `{"pace": {"start": 316, "end": 514,
"units": "secs/km"}}` (5:16 → 8:34) on the recovery steps, `moving_time`
still 3400.

## Generalizing beyond the first sample PDF

The first PDF (hill sprints, `example/2026-09-10/`) shaped every design
decision above. Two more real PDFs arrived once the pipeline was live
(`example/2026-09-15/` stairs, `example/2026-09-17/` a threshold pyramid),
and both broke assumptions baked in from having only ever seen one sample.
Worth internalizing: **this PDF template has more variation between
individual workouts than it first appeared, and a fixed-coordinate approach
will keep breaking on new ones.** What follows is what actually generalizes
and what still doesn't.

### Absolute pixel coordinates don't generalize - use relative structure instead

The original `_parse_main_row` filtered words by hardcoded `top` bands
(`128 <= top <= 136` for durations, `140 <= top <= 150` for the rest row),
reverse-engineered from the one sample PDF available at the time. The stairs
PDF's section headers happen not to wrap onto a second line, which shifts
every row below them up by ~9pt - so the hardcoded bands missed the grid
entirely. Worse: this failed **silently**. `_parse_main_row` returned an
empty list, which cascaded to zero segments, an empty `to_intervals_icu_text()`
string, and - because nothing anywhere checked for this - an actual empty
session pushed to the athlete's COROS watch, with a real email notification,
and no error at any layer. This is why `parse_planiteam_pdf` now raises
`ValueError` when it parses zero segments (see below) - that incident is
exactly the failure mode that check exists to prevent.

The fix: find these two rows **relative to each other**, not at fixed
coordinates. Every observed PDF has exactly one row containing `==>` tokens
(the "rest" row), so `_parse_main_row` now locates that row via
`_cluster_rows` (which does relative y-clustering already, unaffected by
this bug) and walks upward from it to find the duration row. That walk isn't
simply "one row up", either: the pace table's own `VMA` column-header label
sits, on some PDFs, as its own single-word row squeezed into the gap between
the duration row and the rest row - so the code walks upward past any row
that doesn't contain an `Nx` repeat marker, rather than assuming adjacency.
Both quirks were only found by actually diffing what broke against what
worked, not by inspecting one PDF in isolation.

The pace table had the same class of bug, for a different reason: its row
grouping used a fixed clustering tolerance (`tol=1.5`) to merge a row's VMA
label with its pace values, but the vertical offset between them varies by
PDF (0.4-2.2pt observed) - tight enough that the stairs PDF's ~2.2pt offset
fell outside the old tolerance and silently dropped every row. Fixed by
anchoring differently: find each VMA-label word (leftmost column, `x0 < 90`,
matching the numeric label pattern) directly, then gather pace-shaped tokens
within a generous `±6pt` vertical window of *that specific label* - decoupled
from the generic row-clustering primitive entirely, since this relationship
(a label and its own row's values) needed a wider tolerance than everything
else on the page.

### `ÉCHAUFFEMENT` wraps at a different letter on every PDF - don't chase exact splits

Three PDFs, three different line-wrap points for the same French word:
`ÉCHAUFFEM`/`ENT` (first sample), `ÉCHAUFFEME`/`NT` (pyramid). Chasing exact
fragment pairs in `_HEADER_JOIN_FIXUPS` doesn't scale - there's no way to
predict where Planiteam's renderer will break a word next. Since warm-up/
cool-down role detection is the only thing that actually depends on getting
this right (the section's *name* is never shown - see below), the fix was
to make that specific check tolerant instead of trying to perfectly
reconstruct the joined text: strip whitespace from the header text before
substring-matching `CHAUFFEMENT`/`CALME`. `ÉCHAUFFEME NT` (imperfectly
joined, extra space) still matches `CHAUFFEMENT` once spaces are stripped,
regardless of exactly where the PDF wrapped the word. `_HEADER_JOIN_FIXUPS`
is left in place for the cases it already handles (cosmetic `Segment.name`
values, e.g. in JSON output) but is no longer load-bearing for correctness.

### A second, structurally different pace-table layout exists

The pyramid PDF (`seuil-pyramide-2-4-6-4-3-2-min.pdf`) has **no `Z\d` zone
labels and no `%VMA` overlay anywhere on the page.** Instead its pace table
has one column *per session phase* - warm-up, each of the 6 efforts, each of
the 5 recoveries, cool-down: 13 columns total, each independently computed
per VMA row (confirmed by checking column values against x-position
alignment with each phase's block above, the same technique used to
originally confirm the 2-column layout against a screenshot). The two
zone-based PDFs only ever print 2 pace columns (warm-up, cool-down) plus a
separate zone/percent overlay for the main set.

`parse_planiteam_pdf` now dispatches between the two modes:

```python
pace_table_columns = len(next(iter(pace_table.values()), []))
total_flat_entries = sum(len(block["entries"]) for block in blocks)
use_per_step_pace = not zones and pace_table_columns > 2 and pace_table_columns == total_flat_entries
```

i.e.: no zone overlay was found, *and* the pace table's column count exactly
matches the total number of entries across every block on the page (13 for
the pyramid: 1 warm-up + 11 main-set + 1 cool-down). When true, each entry's
target is looked up directly from its corresponding pace-table column via a
single running `flat_index` cursor that increments across the whole
workout, in page order - not just within the main block, since the count
check only holds when warm-up and cool-down's own entries are included in
the flattened total too.

Effort/recovery classification in this mode has no zone data to lean on, so
it falls back to positional alternation: the first entry of a multi-entry,
non-warmup/cooldown block is `"Effort"`, then alternates. This held for
every workout seen so far (every one starts with an effort), but is a weaker
assumption than the zone-based path's `_zone_cue` (which compares actual
zone numbers) - a workout that opened with a recovery would get this wrong,
and there'd be no way to detect that from a per-step-mode PDF alone.

**Known gap, not yet built**: every per-step-mode PDF seen so far has all
`r = 0` (blank) rest values within its main block - i.e. no analogue to the
hill-sprint PDF's trailing `r = 3:00`. If a future per-step PDF combines
both a nonzero trailing rest *and* per-step pace columns, there's currently
no code path attributing a pace-table column to that extra step - it's
appended with `target=None` rather than guessing. Revisit if that PDF shows up.

### Duration formatting needed a combined "1m15s" form

The pyramid's recovery durations (1:15, 1:45, 1:30) aren't whole minutes or
under 60 seconds, so `_format_duration`'s old two-case logic (`"Nm"` or
`"Ns"`) had no representation for them - it would have emitted raw seconds
(`"75s"`) instead. Extended to a third case (`"NmNs"`) and verified live the
same way every other syntax question in this file was: pushed a probe with
`1m15s`/`1m45s` targets, read `workout_doc` back, confirmed exact 75s/105s
durations and a correct `moving_time` total. Confirmed working, not assumed.

### Verifying against real data beats guessing, even for "obvious" assumptions

The stairs PDF's warm-up and cool-down initially looked ambiguous: the
athlete's hand-typed expected file had both at the same pace (`5:43/km`),
while the parser computed two different values (`5:16/km` / `5:43/km`).
Rather than trust either blindly, the pace table's *column x-positions* were
checked directly against the page: the left pace column sits at the same x
as the warm-up block's `20:00` duration (under the `ÉCHAUFFEMENT` header),
and the right column sits at the same x as the cool-down block's `10:00`
(under `RETOUR AU CALME`). That's independent structural evidence for
"left column = warm-up, right column = cool-down" beyond the one screenshot
cross-check the original 2-column assumption was based on - and it disagreed
with the hand-typed expectation, which turned out to be an unverified guess
rather than a checked value. Worth remembering: a human-provided "expected"
file is a hypothesis to check against the source data, not automatically
ground truth, even when it's the more conservative-looking answer (both
values equal felt like the "safe" guess here, but wasn't the correct one).

### A multi-step segment gets its "Nx" header even at 1x

Product decision (the athlete's, not derived from any PDF data): a segment
gets its repeat header (`"1x"`, `"2x"`, ...) whenever it has **more than one
step**, not only when `repeat > 1`. Originally `to_intervals_icu_text()`
hid the header whenever `repeat == 1` (the pyramid's own main-set marker,
since it's a single pass), rendering it as a bare list of steps with no
grouping at all. The preference, after seeing that rendered: a "core"
block - the pyramid's main set has no warm-up/cool-down immediately
recognisable around each rep the way the hill-sprints' `2x` block does -
should still visually read as one repeated group, "repeated once", rather
than an undifferentiated list. A single-step segment (warm-up, cool-down, a
plain drills block like `GAMMES`) never gets a header regardless of its own
repeat count - there's nothing to group in a single instruction.

Implemented as `len(segment.steps) > 1` replacing the old `segment.repeat > 1`
condition. Verified live the same way as every other syntax question here:
pushed a probe with a bare `1x` header, confirmed `workout_doc` shows
`{"reps": 1, ...}` (not silently dropped or misparsed), then re-pushed the
real pyramid event with the corrected format and deleted the stale one.
