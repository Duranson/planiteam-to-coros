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
compute real paces: every effort target is a %VMA read from the PDF (its
zone overlay or its pace-table column) turned into an absolute pace, rather
than relying on intervals.icu's own pace-zone configuration (which would
otherwise have to be kept in sync with whatever COROS estimates on its own -
see CLAUDE.md for why that matters). Warm-ups, cool-downs, recoveries and
hill repeats get no pace target on purpose.

## Quick start

```bash
pip install -r requirements.txt
python planiteam_to_coros.py path/to/session.pdf --vma 17.5
```

That prints the parsed workout as intervals.icu structured-workout text.
Useful flags:

- `--output FILE` - write to a file instead of stdout.
- `--vma FLOAT` - your VMA in km/h (default `17.5`, tuned to this repo's
  owner - override it).
- `--push-recipients --date YYYY-MM-DD` - push the workout as a planned
  event on that date (the PDF has no session date, only the notification
  email does) to every enabled recipient, each with their own VMA - the same
  thing the Cloud Function does.

Recipients are read from `RECIPIENTS_JSON` (env var or a local `.env` line)
or a local `recipients.json` (**never commit either**) - see "Build your
recipients list" in [DEPLOYMENT.md](DEPLOYMENT.md). Each entry needs an
intervals.icu API key and athlete id, from Settings -> Developer Settings.

## Automatic sync from a Gmail notification

Planiteam can email a PDF notification whenever a coach publishes a new
session. `main.py` + `apps_script/Code.gs` turn that into a fully-automated,
cloud-hosted pipeline: a Google Apps Script watches Gmail on a timer,
extracts the PDF and session date, and hands them to a small Cloud Function
that runs the same parsing/push logic. Nothing depends on any local machine
being on. See [DEPLOYMENT.md](DEPLOYMENT.md) for the full setup walkthrough.

The Cloud Function can push the same session to more than one intervals.icu
account - useful for sharing a coach's session with a training club, since
each member typically has their own VMA. Recipients (name, VMA, their own
intervals.icu API key + athlete id) are an explicit list kept in a
gitignored `recipients.json`, stored as a single Secret Manager secret so it
never reaches the public repo - see "Build your recipients list" in
[DEPLOYMENT.md](DEPLOYMENT.md). Each recipient is parsed (with their own
VMA) and pushed independently, so one person's revoked key doesn't block
the rest of the club.

## Repository layout

| Path | What |
|---|---|
| `planiteam_to_coros.py` | The PDF parser and the CLI - the core of this repo. |
| `intervals_icu.py` | intervals.icu client: recipients list and event push. |
| `ARCHITECTURE.md` | Map of the parsing pipeline. |
| `main.py` | Cloud Function entry point for the Gmail automation. |
| `manage_recipients.py` | Publishes `recipients.json` to the `RECIPIENTS_JSON` Secret Manager secret. |
| `apps_script/Code.gs` | The Gmail-watching side of that automation. |
| `tests/` | Tests against the real sample PDF, including cross-checks against the app's own displayed totals. |
| `example/` | A real (anonymised) sample PDF and its expected parsed output. |
| `CLAUDE.md` | Everything reverse-engineered about the PDF's layout and intervals.icu's API/text-format quirks - written for continuing this work, but a useful read for anyone curious how it works. |
| `DEPLOYMENT.md` | Step-by-step guide for the Gmail -> Cloud Function automation. |

## Limitations

This was built and tested against the Planiteam PDFs in `example/`
(running sessions: warm-up, drills, repeated effort/recovery blocks by time
or distance, hill repeats, pyramids, cool-down). A differently-shaped
session will likely need the parser extended; it's written to fail loudly
(a `ValueError`) rather than silently produce a wrong workout.

VMA-based pace computation is specific to how *this* Planiteam account's
sessions are set up. If your coach expresses intensity differently (HR
zones, %FTP, etc.), the parser will need adapting.

## Contributing

This is a personal tool built for one athlete's specific setup, not
maintained as a general-purpose product. Not looking for contributions at
this time, but feel free to fork it if your own Planiteam/COROS/intervals.icu
setup is close enough to be useful as a starting point.
