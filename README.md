# dooh-factory

**Author:** Radwan Mezzi

Automated DOOH creative factory. Reads a Google Sheet, renders flat creatives (MP4 / static) with FFmpeg, writes the output path back to the sheet. Runs on a configurable schedule.

- **Mode 1** - stitch a sequence of rushes (defined per column in the sheet) into a finished creative.
- **Mode 2** - inject variable params into an HTML5 creative and capture each variant (drive-to-store / address repiquage).

- **Delivery** - every creative ships as a rendition ladder (DOOH / CTV / mobile / radio / web) wrapped in a VAST 4.2 tag.

**Start here:** [`docs/PRODUCT.md`](docs/PRODUCT.md) - full product documentation, written for someone who has never seen the project.
Column reference: [`docs/SHEET_MODEL.md`](docs/SHEET_MODEL.md). Original spec: [`docs/BRIEF.md`](docs/BRIEF.md).

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
python run.py --job mode2 --workers 4      # render 4 rows concurrently
python run.py --daemon                     # every job on its cadence (APScheduler)
python run.py --print-cron                 # crontab lines to review and install
```

Exit code is non-zero when any row errored, so cron and CI notice.

**On `--workers`:** it pays off for Mode 2 and not for Mode 1. Measured on an M1 —
6 static captures went 3.9s to 2.1s on 3 workers, because each row is mostly
browser latency; 8 stitched creatives went 7.0s to 6.8s on 4 workers, because
x264 already threads across every core and concurrent encodes just split the
same CPU. Leave Mode 1 at 1 unless the rushes are small enough that ffmpeg
process startup dominates.

### See it work in two commands

```
python scripts/make_demo_assets.py
python run.py --job mode1 --csv templates/sheet_model/mode1.csv
```

Renders three creatives, their rendition ladders and their VAST tags into `output/`.

### No Google credentials yet?

`--csv` drives a job from a local CSV with the same columns and the same
contract. Useful to validate a whole campaign before the sheet exists, and to
reproduce a bad render from a single file.

```
python run.py --job mode1 --csv rows.csv
```

## Sheet contract

Three tabs: `mode1` (stitched creatives), `mode2` (HTML5 variants), `media_dv360`
(the trafficking plan). Provision them with:

```
python scripts/bootstrap_sheet.py --create "dooh-factory" --share you@gmail.com
python scripts/bootstrap_sheet.py --csv-out templates/sheet_model   # or offline, as CSV
```

Generate the DCO combinations instead of typing them:

```
python scripts/expand_dco.py examples/campaign_soldes.yml --dry-run
```

One row = one creative. The tool owns seven optional columns and writes them back:

| Column | Written back | Purpose |
|---|---|---|
| `output_path` | yes | path of the rendered asset |
| `status` | yes | `pending` / `done` / `error` |
| `message` | yes | error detail (falls back into `status` when absent) |
| `input_hash` | yes | fingerprint of the row's inputs |
| `rendered_at` | yes | UTC timestamp of the last render |
| `vast_path` | yes | the generated VAST tag |
| `renditions` | yes | the delivery ladder, comma-separated |

`input_hash` is what makes runs idempotent: a `done` row is rebuilt when a cell
changed, a rush was replaced on disk, or the target format/codec changed in
config. A sheet without the column keeps the plain behaviour - done means done.

**Mode 1 inputs:** `clip_1..clip_N` (rushes in order; empty cells are skipped, so
a gap does not truncate the sequence), optional `clip_N_dur` (trims a video,
sets the hold of a still), `format` (`1080x1920`), `max_duration_s`,
`transition` + `transition_dur` (any ffmpeg xfade name; empty = hard cut),
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
pytest tests/test_vast_schema.py -q   # tags vs the official IAB VAST 4.2 XSD
python tests/smoke_mode1.py   # real ffmpeg stitch + idempotency contract
python tests/smoke_mode2.py   # real Chromium capture, all three capture modes
```

146 offline tests, including VAST schema validation per channel. Known gaps are
listed in [`docs/PRODUCT.md` §13](docs/PRODUCT.md).

## Encoding

CRF by default; setting `ffmpeg.bitrate` switches to the CBR-ish rate control
most DOOH CMS ingest specs ask for. `profile`, `level`, `gop` and `faststart`
are all configurable for the same reason. `ffmpeg.profiles` carries a per-format
override, so a 4K landscape network can demand 20 Mbit CBR while portrait city
panels stay on CRF:

```yaml
ffmpeg:
  crf: 20
  profiles:
    "3840x2160": {bitrate: "20M", gop: 50}
```

## Delivery and VAST

A creative is not one file. Each row produces a master, then a rendition ladder
sized for its `channel`, then a VAST 4.2 InLine referencing every rendition.

| Channel | Sound | Mezzanine | Ladder |
|---|---|---|---|
| `dooh` | no, screens are muted | no | one high-bitrate rendition at panel size |
| `ctv` | yes | yes, SSAI needs it | 1080p + 720p |
| `mobile` | yes | no | 720p down to 360p |
| `radio` | audio only | no | AAC + MP3, no picture |
| `web` | yes | no | 1080p down to 360p |

The ladders in `config.example.yml` are conventional starting points, not a
quotation of a specific IAB document. Every network publishes its own ingest
spec and that spec wins.

Every channel's tag is validated against the **official IAB VAST 4.2 XSD**
(vendored in `tests/schemas/`, so the suite runs offline). See
[`docs/PRODUCT.md` §6](docs/PRODUCT.md) for why the tag carries an `<Error>`
with `[ERRORCODE]`, a `<Mezzanine>` on CTV, no `<VideoClicks>` on DOOH, and why
a macro'd URL is deliberately not schema-valid.

## Status

P0-P11 done and tested end-to-end: both modes, idempotency by input hash,
scheduling (cron + daemon), xfade transitions, per-format encoding profiles,
retries on transient Sheets failures, concurrent rendering, multi-rendition
delivery with VAST 4.2 validated against the official IAB XSD, the three-tab DCO
sheet model with combination expansion, and the radio channel.

**Not yet verified: the live Google Sheet path.** There is no service account on
the build machine, so `open_worksheet` / `read_rows` / `make_write_back` have
never made a real API call. Row parsing and write-back buffering are unit-tested
against raw values; the network round-trip is not. That is the first thing to
run against a real sheet.

## Author

Radwan Mezzi - AdTech / adserving, DCO and server-side tracking.
Built from scratch as a clean-room implementation of the ad-stitching / DCO pattern.
