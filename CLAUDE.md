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

Reference example: `example/cotes-courtes-2x6x20-.pdf`, matching the
screenshot the user provided when this was built (a "Côtes courtes : 2x6x20""
session — 20' warm-up, 10' drills, 2×[6×(20s effort/40s jog) with a 3'
recovery before the second set], 10' cool-down, total 56:40).

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

Not yet verified: whether this flag actually changes anything on the COROS
watch itself (only `workout_doc`'s shape on intervals.icu's side was
confirmed) — that requires the user to check a real sync. Also unconfirmed:
whether a `Recovery`/`Rest`-style keyword exists for tagging the easy steps
*inside* a repeat block the same way (not tested; the main set's `Z1`
recovery steps currently carry no such flag, only a pace-zone target).
