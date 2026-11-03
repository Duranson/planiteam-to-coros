# planiteam-to-coros

Converts a Planiteam workout PDF export into an intervals.icu planned
calendar event (pushed via the intervals.icu API), which intervals.icu then
syncs to a COROS watch - that device sync is configured on intervals.icu's
website, out of scope for this repo.

There is no Planiteam API; the PDF export is the only data source. The code
map is in [ARCHITECTURE.md](ARCHITECTURE.md). This file records what was
reverse-engineered or **verified live** and the product decisions behind
the rules - read it before changing behaviour.

## Working rules

- **Verify against real data, not assumptions.** Every intervals.icu syntax
  fact below was confirmed by pushing a probe event, `GET`ting it back and
  inspecting `workout_doc` (the server's own parse of `description`) and
  `moving_time`. A `200 OK` with no errors once still produced a wrong
  workout (missed repeat, power instead of pace). Clean up probe events
  (`DELETE /api/v1/athlete/{id}/events/{eventId}`).
- **Hand-typed `expected.workout.intervals.txt` files are hypotheses.**
  Several had errors (a duration copied from another session, `mt` instead
  of `mtr`, a single pace instead of a range, a pace read from the wrong
  column). Check them against the PDF's own words before bending the parser.
- **Fail loudly.** An unknown layout must raise `ValueError` (the Cloud
  Function then returns 502 and Apps Script labels the email
  `PlaniTeam-Sync-Error`). A silently degraded parse once pushed an empty
  session to the watch.
- **Never use absolute page coordinates.** Every one tried so far broke on
  the next PDF. Locate things relative to the grid rows and match by nearest
  x0.
- Accented text printed to this Windows terminal shows as `�`: a cp1252
  console artefact, not corrupted data. Always read/write files as UTF-8.

## The PDF

A coordinate dump: section titles, the interval grid and a VMA pace table
are independent text objects. Linear text extraction interleaves them into
garbage - always parse via `page.extract_words()` positions.

Layout, top to bottom (positions vary between PDFs, the order doesn't):

1. **Title** (left) and `Édité le DD/MM/YYYY` (right) on the first row. The
   PDF has **no session date** - it comes from the notification email
   (`--date`, or the Apps Script payload).
2. **Section headers**, centred over their block (not aligned), possibly
   wrapped onto a second row - sometimes mid-word, at an unpredictable
   letter (`ÉCHAUFFEM`/`ENT`, `ÉCHAUFFEME`/`NT`). Grouped by horizontal
   overlap and paired 1:1, in order, with the grid's `Nx` blocks. Planiteam
   itself clips long headers (`... / MARCH` for `MARCHE`).
   A header like `ÉCHAUFFEMENT + GAMMES` over a block with exactly two
   entries is split into two blocks.
3. **Duration row**: `Nx` repeat markers followed by entries: `mm:ss`,
   distances (`500m`, `1.0km`), `Lap`, or a free-text `Consigne` (ignored;
   a `Nx` followed only by `Consigne` is an empty block, skipped).
4. Sometimes a lone `VMA` word row.
5. **Rest row**: `==> r = <value>` under each entry. `<value>` is the extra
   rest after that entry (`0`/blank = none). A non-zero rest on the last
   entry is the recovery before the outer repeat. Rests are matched to
   entries by nearest x0 - `Consigne` carries an orphan `r = 0` that would
   desync sequential pairing.
6. **Below the grid**: the VMA reference table, plus (on some PDFs) a zone
   overlay - `Z5`/`Z1` labels and `95-100%`/`0-65%` bands placed under the
   main-set entries. The overlay just happens to share `top` with some table
   rows; it is metadata for the grid, matched to entries by nearest x0.

### The pace table: every column is a constant %VMA

Rows are reference VMAs (12 -> 23 km/h, not the athlete's own; the label
column's x varies between PDFs). Each column is one session phase and is a
**constant %VMA** across all rows (measured: 55, 60, 65, 68, 80, 83, 85, 86,
88, 90, 100%) - so `_PaceColumn.vma_pct()` derives the % and the athlete's
pace is computed directly from their VMA. Cell kinds:

- `7'42/km` - a pace per km.
- `05:41` - a **split time for the entry's own distance** (e.g. 300m), to be
  rescaled by that distance. For a 1000m entry it equals the pace, which
  made this easy to miss.
- `-` - no target for that phase.

Two-column tables (warm-up, cool-down) appear on PDFs with a zone overlay;
others have a column per phase. The column-derived pace can differ by 1s
from the printed cell (cells are rounded; the 300m split showed 3:27 for an
exactly-100% target computed as 3:26) - the computed value is used.

## Step rules (product decisions)

- **Warm-up / cool-down** (header contains `CHAUFFEMENT` / `CALME`, spaces
  stripped): `Warmup`/`Cooldown` header line, no cue, **no pace target**
  (pace alerts during those phases were more distracting than useful).
- **Effort vs recovery**: `Z1`/`Z2` recovery, `Z3+` effort
  (`_MAX_RECOVERY_ZONE`; one PDF mixes Z4 and Z5 efforts). Without a zone,
  %VMA below 70 is a recovery. No intensity source for a step in an
  interval block -> `ValueError`.
- **Recoveries never get a pace target** (the athlete wants them free of
  alerts). Every attached `r = rest` is an untargeted `Récupération` step.
- **Efforts** get the %VMA target: a zone-overlay band renders as a range
  (`3:26-3:37/km`, kept as a range even for short efforts - confirmed with
  the athlete), a pace column as a single pace.
- **Côtes** (header mentions `côte`, accent-insensitive): no pace target at
  all - a flat-ground pace is meaningless on a slope, and COROS's
  grade-adjusted "Allure d'effort" can't be set via intervals.icu/FIT (the
  FIT target-type enum has no grade-adjusted variant; intervals.icu's `GAP`
  is analysis-only). The zone goes in the cue instead, **in parentheses**:
  `Côtes (Z5)`, `Récupération (Z1)`; a rest with no zone label of its
  own is a bare `Récupération`. A bare `Z5` anywhere on a step line is read
  as a power-zone target (rendered `(0-0w)` without FTP, which crashed the
  COROS app).
- **Plain single-entry blocks** (drills, an inter-block jog, a tempo): cue =
  header name, title-cased (`Gammes`, `Récup Footing Avant Tempo`); a target
  only when its own pace column says effort (e.g. an 80% tempo).
- **`Lap` entries**: open-ended drills, own segment with cue `Gammes`
  (`_LAP_CUE`), placeholder 10 min (`_LAP_DEFAULT_DURATION_S`), keeping the
  enclosing block's role.
- **Repeat header**: a segment with more than one step gets `Nx` even at
  `1x` (so a lone core block reads as a group); a single-step segment never
  does.

## intervals.icu: verified facts

Endpoint and schema from the OpenAPI spec (`curl -s
https://intervals.icu/api/v1/docs -o spec.json`; don't use a summarising
fetch tool, the spec is too large). The `description` text grammar is only
documented in the forum guide
(https://forum.intervals.icu/t/workout-builder-syntax-quick-guide/123701 -
fetch `<url>.json`, the HTML is an empty app shell). All verified live:

- `POST /api/v1/athlete/{id}/events`, HTTP Basic auth, username literally
  `API_KEY`, password = the API key.
- **Repeats**: a bare `2x` line (no `- `), child steps as plain `- ` bullets
  at the same indentation, blank line before/after. Getting this wrong
  silently runs the block once. `1x` is accepted (`{"reps": 1}`).
- **Pace targets need a `Pace` suffix**: bare `Z5` is a *power* zone; a
  bare `5:43/km` is silently dropped.
- **Zones are resolved server-side**: `Z5 Pace` is converted to an absolute
  speed range from the athlete's intervals.icu zone config before the FIT
  export (`target_speed_zone` is always 0 in the `.fit` from
  `.../events/{eventId}/download.fit`). So zones can't defer to the watch's
  own threshold; targets are computed here from %VMA instead, which also
  removes any dependency on intervals.icu's zone settings.
- **A single-value pace target** gets a tight auto-band on COROS (`5:16/km`
  showed as `5'08-5'24`). A `0-x%` band has no finite slow bound and is
  refused at render time (only Z1 recoveries have one, and they're untargeted).
- **`Warmup` / `Cooldown`** header lines are the only way to get
  `"warmup": true` / `"cooldown": true`; must be the English keyword (the
  French `ÉCHAUFFEMENT` doesn't work). COROS then shows them localised
  ("Echauffement", "Retour au calme") - confirmed on a real watch.
- **No equivalent for recovery**: `Recovery`/`Rest`/`Interval` as a header
  line are dropped, inside or outside repeats. As a leading word on a step
  line it becomes the step's `"text"`.
- **Cue text** (leading words on a step line) becomes the step's `"text"`,
  shown by COROS as the block name - confirmed on the device. **The COROS
  app only shows about two words** of it, so keep cues to two words
  (`Côtes (Z4)`, not `Effort Côtes (Z4)`).
- **Durations**: `20s`, `10m`, `1m15s`. **Distances**: `500mtr` (a bare `m`
  means *minutes*; `mt` is invalid), `1km` / `1.0km`.
- **`Press lap`** on a step line -> `"until_lap_press": true`, exported to
  FIT as `duration_type: 'open'`, `target_type: 'open'`; the duration is
  still required but only feeds `moving_time`. Placed **after the
  duration** (`- Gammes 10m Press lap`) it stays out of the step name
  (`"text": "Gammes"`); placed before it, intervals.icu kept it in the name
  (`Gammes Press lap`).
- **No idempotent upsert**: client-supplied `uid` is ignored (the server
  assigns its own), so every push creates a new event and re-pushing
  duplicates it. Real idempotency would need the returned event id cached
  and reused as a `PUT` target - not built, nobody asked for it.

## Club push (multiple recipients)

`intervals_icu.Recipient` (`name`, `vma`, `api_key`, `athlete_id`,
`enabled`) comes from one `RECIPIENTS_JSON` value: a Secret Manager secret
in the cloud, `.env` or a gitignored `recipients.json` locally.
`parse_recipients` raises on malformed JSON or a missing field rather than
silently dropping someone.

One JSON secret instead of one per person: `gcloud functions deploy
--set-secrets` names every binding explicitly, so per-person secrets would
need a deploy-config change to add someone. A `:latest` secret is resolved
at instance start, so a new version applies on the next cold start without
redeploying (Google-documented, not verified live here).

The PDF is parsed once; `push_to_recipients` renders it with each
recipient's VMA and pushes, isolating failures per recipient (the athlete's
explicit preference: other people's accounts can break independently).
`main.py` returns 502 only when parsing fails or **every** push fails;
partial failures return 200 with a per-recipient `results` array and one
log line per recipient.

## Tests

- `tests/test_examples.py`: one test per `example/<date>/` folder (named
  after the session date) comparing the rendered text at `DEFAULT_VMA` with
  `expected.workout.intervals.txt`. It always writes
  `actual.workout.intervals.txt` (gitignored) next to it for diffing. Adding
  a folder with a PDF and an expected file is enough to add a test. Uses
  `unittest` (VSCode Testing tab via `.vscode/settings.json`), not pytest.
- `tests/test_planiteam_to_coros.py`: what the examples can't show - other
  VMAs, %VMA extraction, fail-loudly cases, render syntax, recipients/push.
- Cheapest sanity check on a new PDF: the sum of step durations × repeats
  should equal the total Planiteam's UI shows (56:40 for `2026-09-10`).
