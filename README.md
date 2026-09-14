# planiteam-to-coros

Gets a training session created by a coach in [Planiteam](https://planiteam.fr)
onto a COROS watch, without typing it in by hand.

There's no native bridge between the two products. Planiteam can only export
a session as a PDF, and that PDF's layout is awkward to parse (see
[CLAUDE.md](CLAUDE.md) for the gory details). This repository parses it
properly, turns it into a structured workout, and pushes it to
[intervals.icu](https://intervals.icu), which already has a working COROS
sync built in.

## How it works

```
Planiteam (coach creates a session)
  -> PDF export (emailed to the athlete, or downloaded manually)
  -> planiteam_to_coros.py parses the PDF's actual layout
     (section headers, interval grid, VMA pace table - not just its text)
  -> pushed to intervals.icu as a planned calendar event
  -> intervals.icu syncs it to COROS (configured once, on intervals.icu's
     own website - not part of this repo)
  -> shows up on the watch
```

The PDF has no field for the athlete's actual VMA (only a reference table
for a range of VMA values), so it's passed in as a parameter and used to
compute real paces - warm-up/cool-down get an absolute pace from the PDF's
own table, and the main set's effort/recovery targets are computed directly
from the %VMA the PDF prints, rather than relying on intervals.icu's own
pace-zone configuration (which would otherwise have to be kept in sync with
whatever COROS estimates on its own - see CLAUDE.md for why that matters).

## Quick start

```bash
pip install -r requirements.txt
python planiteam_to_coros.py path/to/session.pdf --vma 17.5
```

That prints the parsed workout as intervals.icu structured-workout text.
Useful flags:

- `--format json` - dump the parsed workout as JSON instead.
- `--output FILE` - write to a file instead of stdout.
- `--vma FLOAT` - your VMA in km/h (default `17.5`, tuned to this repo's
  owner - override it).
- `--push --date YYYY-MM-DD` - also push the workout to intervals.icu as a
  planned event on that date (the PDF has no session date, only the
  notification email does).
- `--sport` - intervals.icu sport type for a pushed event (default `Run`).

Pushing requires two credentials, read from environment variables (or a
local `.env` file, auto-loaded, **never commit this file**):

```
INTERVALS_ICU_API_KEY=...
INTERVALS_ICU_ATHLETE_ID=...
```

Both are available from your intervals.icu account under Settings ->
Developer Settings.

## Automatic sync from a Gmail notification

Planiteam can email a PDF notification whenever a coach publishes a new
session. `main.py` + `apps_script/Code.gs` turn that into a fully-automated,
cloud-hosted pipeline: a Google Apps Script watches Gmail on a timer,
extracts the PDF and session date, and hands them to a small Cloud Function
that runs the same parsing/push logic. Nothing depends on any local machine
being on. See [DEPLOYMENT.md](DEPLOYMENT.md) for the full setup walkthrough.

## Repository layout

| Path | What |
|---|---|
| `planiteam_to_coros.py` | The parser and intervals.icu client - the core of this repo. |
| `main.py` | Cloud Function entry point for the Gmail automation. |
| `apps_script/Code.gs` | The Gmail-watching side of that automation. |
| `tests/` | Tests against the real sample PDF, including cross-checks against the app's own displayed totals. |
| `example/` | A real (anonymised) sample PDF and its expected parsed output. |
| `CLAUDE.md` | Everything reverse-engineered about the PDF's layout and intervals.icu's API/text-format quirks - written for continuing this work, but a useful read for anyone curious how it works. |
| `DEPLOYMENT.md` | Step-by-step guide for the Gmail -> Cloud Function automation. |

## Limitations

This was built and tested against one specific Planiteam PDF template
(interval-style running sessions with a warm-up, drills, a repeated
effort/recovery block, and a cool-down). A differently-shaped session -
a continuous tempo run, a session with more than two intensity zones,
swimming or cycling - will likely need the parser extended; it's written to
fail loudly (an exception, or a mismatched structure) rather than silently
produce a wrong workout, but it hasn't been validated against anything but
the sample in `example/`.

VMA-based pace computation is specific to how *this* Planiteam account's
sessions are set up. If your coach expresses intensity differently (HR
zones, %FTP, etc.), the parser will need adapting.

## Contributing

This is a personal tool built for one athlete's specific setup, not
maintained as a general-purpose product. Not looking for contributions at
this time, but feel free to fork it if your own Planiteam/COROS/intervals.icu
setup is close enough to be useful as a starting point.
