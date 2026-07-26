# dooh-factory

Automated DOOH creative factory. Reads a Google Sheet, renders flat creatives (MP4 / static) with FFmpeg, writes the output path back to the sheet. Runs on a configurable schedule.

- **Mode 1** - stitch a sequence of rushes (defined per column in the sheet) into a finished creative.
- **Mode 2** - inject variable params into an HTML5 creative and capture each variant (drive-to-store / address repiquage).

Full spec: [`docs/BRIEF.md`](docs/BRIEF.md).

## Install

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
brew install ffmpeg
cp config/config.example.yml config/config.yml   # sheet id + service account path
```

## Run

```
python run.py --job mode1 --once           # one job, once (this is what cron calls)
python run.py --job mode1 --dry-run        # resolve inputs + names, render nothing
python run.py --job mode1 --force          # re-render rows already marked done
python run.py --job mode2 --limit 5        # stop after 5 rendered rows
python run.py --daemon                     # every job on its cadence (APScheduler)
python run.py --print-cron                 # crontab lines to review and install
```

Exit code is non-zero when any row errored, so cron and CI notice.

### No Google credentials yet?

`--csv` drives a job from a local CSV with the same columns and the same
contract. Useful to validate a whole campaign before the sheet exists, and to
reproduce a bad render from a single file.

```
python run.py --job mode1 --csv rows.csv
```

## Sheet contract

One row = one creative. The tool owns five optional columns and writes them back:

| Column | Written back | Purpose |
|---|---|---|
| `output_path` | yes | path of the rendered asset |
| `status` | yes | `pending` / `done` / `error` |
| `message` | yes | error detail (falls back into `status` when absent) |
| `input_hash` | yes | fingerprint of the row's inputs |
| `rendered_at` | yes | UTC timestamp of the last render |

`input_hash` is what makes runs idempotent: a `done` row is rebuilt when a cell
changed, a rush was replaced on disk, or the target format/codec changed in
config. A sheet without the column keeps the plain behaviour - done means done.

**Mode 1 inputs:** `clip_1..clip_N` (rushes in order; empty cells are skipped, so
a gap does not truncate the sequence), optional `clip_N_dur` (trims a video,
sets the hold of a still), `format` (`1080x1920`), `max_duration_s`,
`naming_template` or `output_name`. Stills (jpg/png) are first-class rushes.

**Mode 2 inputs:** `template`, `format`, `capture`, `inject`, `duration_s`,
`wait_selector`, `wait_ms` - plus any other column, which is injected into the
creative as a param.

Injection modes: `dom` (fills `[data-dooh="key"]` elements - zero-JS templates),
`query`, `window_var` (set before any page script runs), `postmessage`.

Capture modes: `static` (screenshot), `video` (real-time Chromium recording -
correct for CSS animation), `frames` (screenshot loop, frame-exact only when the
template exposes `window.__seek(ms)`), `auto`.

See [`templates/example_store.html`](templates/example_store.html) for a
reference creative that supports all four injection modes and the seek hook.

## Tests

```
pytest tests -q               # offline: no ffmpeg, no browser, no credentials
python tests/smoke_mode1.py   # real ffmpeg stitch + idempotency contract
python tests/smoke_mode2.py   # real Chromium capture, all three capture modes
```

## Status

P0-P3 done and tested end-to-end: both modes, idempotency by input hash,
scheduling (cron + daemon), CLI. Remaining: live Google Sheet run (needs a
service account), then P4 hardening - see BRIEF §10.
