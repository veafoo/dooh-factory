# dooh-factory: product documentation

**Audience:** a developer taking over this codebase, or an ops person running it.
**Prerequisite:** none. This document assumes no prior knowledge of the project.

If you read one section, read [§2 The mental model](#2-the-mental-model). Everything
else follows from it.

---

## 1. What this is, in one paragraph

An advertiser wants the same ad in 200 versions: one per store, per market, per
audience, per offer. A screen cannot run a template, whether it is a street panel, a TV, a phone or a
radio stream. It runs a finished file. This tool sits in between: it
reads a Google Sheet where each row describes one version, renders that version
to a flat asset with FFmpeg or a headless browser, wraps the result in a VAST tag
the buying platform can traffic, and writes the paths back into the sheet. It
runs on a schedule and it is idempotent, so re-running it costs nothing when
nothing changed.

The Sheet is the product. Everything else is machinery serving it.

---

## 2. The mental model

Four ideas. Once these are clear, the code reads itself.

### 2.1 One row = one creative

There is no hidden state. A row carries its inputs (which rushes, which text,
which format, which channel), and the tool writes the result back into the same
row. An ops person looks at the sheet and knows the state of the campaign. No
database, no dashboard, no separate job queue.

This is a deliberate constraint, and it has a cost: the Sheets API is slow and
rate-limited, so every read is one call and every batch of write-backs is one
call. It buys something worth more: the person running the campaign owns the
control surface and does not need us.

### 2.2 Two modes, one spine

|  | Mode 1 (`stitch`) | Mode 2 (`html5`) |
|---|---|---|
| Input | rushes sequenced across columns | an HTML5 template + params |
| Engine | FFmpeg | Playwright (headless Chromium) → FFmpeg |
| Varies by | which rush goes in which slot | which values are injected |
| Typical use | a campaign edit per market/audience/offer | drive-to-store, one variant per shop |

They share everything else: naming, idempotency, error reporting, scheduling,
delivery. In code, a mode provides exactly two functions and nothing more:

```python
prepare(row, cfg, job) -> plan   # resolve inputs, name, fingerprint. May fail loudly.
render(plan, cfg, job) -> path   # build the asset.
```

`src/runner.py` owns the rest. **If you are adding a third mode, you write those
two functions and you are done.**

### 2.3 The pipeline

```
Sheet row
   │
   ├─ prepare()      resolve rushes/template, pick format, compute the input hash
   │                 └─ fails here if an asset is missing: nothing expensive started
   │
   ├─ skip?          done + hash unchanged → stop. This is most rows, most runs.
   │
   ├─ render()       → the MASTER: one high-quality file
   │
   ├─ renditions     → the LADDER: one file per (size, bitrate) the channel needs
   │
   ├─ vast           → the TAG: one InLine referencing every rendition
   │
   └─ write-back     output_path, vast_path, renditions, status, input_hash, rendered_at
```

Master and renditions are separate on purpose. The master is encoded for quality;
a rendition is encoded for a delivery bitrate. Shipping the master as the "600k
mobile file" is how a 40 MB asset ends up in a mobile tag.

### 2.4 Do not render more often than the data changes

The scheduling rule, and it is enforced in code rather than written on a wiki:
`parse_cadence` refuses any interval below `scheduler.min_interval_seconds`
(60 by default). A typo cannot turn into a per-second hammering of the Sheets API.

The same idea drives idempotency: a row is rebuilt only when its inputs actually
moved. See [§5](#5-idempotency-the-input-hash).

---

## 3. Getting it running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
brew install ffmpeg                                  # or your platform's equivalent
cp config/config.example.yml config/config.yml
```

### Without Google credentials (do this first)

```bash
python scripts/bootstrap_sheet.py --csv-out templates/sheet_model
python run.py --job mode1 --csv templates/sheet_model/mode1.csv --dry-run
```

`--csv` drives a job from a local CSV with the same columns and the same
contract. A whole campaign can be validated before the sheet exists, and a bad
render can be reproduced from a single file.

### With Google credentials

1. Create a service account in Google Cloud, enable the Sheets and Drive APIs,
   download the JSON key to `config/service-account.json` (gitignored).
2. Provision a sheet:
   ```bash
   python scripts/bootstrap_sheet.py --create "dooh-factory" --share you@gmail.com
   ```
   `--share` is required: a spreadsheet created by a service account is owned by
   it and will **not** appear in your Drive otherwise.
   For an existing sheet, share it with the service account's `client_email` as
   **Editor**, then `--spreadsheet-id <id>`.
3. Put the id in `config/config.yml` under `google.spreadsheet_id`.
4. `python run.py --job mode1 --dry-run`, then without `--dry-run`.

---

## 4. The Sheet

Three tabs. Full column reference in [`SHEET_MODEL.md`](SHEET_MODEL.md).

| Tab | One row is | Owned by |
|---|---|---|
| `mode1` | one stitched creative | creative / production |
| `mode2` | one HTML5 variant | creative / production |
| `media_dv360` | one line item × creative | trading |

### The write-back contract

Seven columns the tool owns and fills in. All optional: a sheet with only
`output_path` and `status` works, it just loses the corresponding feature.

| Column | What lands in it |
|---|---|
| `output_path` | the master |
| `status` | `pending` / `done` / `error` |
| `message` | the error detail; without this column it rides along in `status` |
| `input_hash` | the fingerprint that drives re-rendering |
| `rendered_at` | UTC timestamp |
| `vast_path` | the generated tag |
| `renditions` | the delivery ladder, comma-separated |

### Why the creative tabs have so many columns

Because this is a DCO matrix, not a file list. A creative varies along axes:
market, language, audience, daypart, offer, store. Each axis must be a
column: it is both an input to the render and a component of the output name.
**A column you did not model is a variation you cannot produce.**

On `mode2`, any column that is not a control column is injected into the
template as a param. Adding a new creative variable is adding a column. No code
change.

### Generating the combinations

Filling 120 rows by hand is how mistakes get in. Describe the axes instead:

```yaml
# examples/campaign_soldes.yml
mode: mode1
base:
  campaign: "Soldes Été"
  channel: dooh
  format: "1080x1920"
  naming_template: "{campaign}_{market}_{audience}_{offer}_{version}"
axes:
  market: [FR, BE]
  audience: [young, family]
  offer: ["-30%", "-50%"]
slots:
  clip_1: "intro_{market}.mp4"
  clip_2: "product_{audience}.mp4"
  clip_3: "cta_{offer|slug}.mp4"
exclude:
  - {market: BE, offer: "-50%"}
```

```bash
python scripts/expand_dco.py examples/campaign_soldes.yml --dry-run   # count first
python scripts/expand_dco.py examples/campaign_soldes.yml -o rows.csv
```

`{axis}` interpolates the raw value, `{axis|slug}` a filename-safe one
(`-50%` → `50pct`). The combination count is printed before anything is written:
axes multiply fast, and 4 × 3 × 5 × 2 is 120 renders. That is a decision, not a detail.

---

## 5. Idempotency: the input hash

A row is skipped when `status = done`, **unless** `--force`, or the sheet has an
`input_hash` column whose stored value no longer matches what the inputs hash to
now.

The hash covers:

- every input cell of the row (a copywriter edits the claim → rebuild),
- the identity of every resolved input file: basename + size + mtime (a rush is
  replaced on disk → rebuild),
- the effective render spec: format, fps, codec, rate control, profile, level,
  GOP, channel, ladder (the CMS changes its ingest spec, you change the config →
  every affected row rebuilds).

It is **not** a content hash. Hashing a 200 MB rush on every tick is the correct
answer and the wrong cost; size + mtime is the pragmatic trade, and it is
documented as such rather than presented as cryptographically sound.

A sheet with no `input_hash` column keeps the old behaviour: done means done.

---

## 6. Delivery: renditions and VAST

### Channels

A channel decides the ladder, whether there is sound, and whether a mezzanine is
needed.

| Channel | Sound | Mezzanine | Ladder shape |
|---|---|---|---|
| `dooh` | no, public screens are muted | no | one or two high-bitrate renditions at panel size |
| `ctv` | yes | **yes** | 1080p and 720p at high bitrate |
| `mobile` | yes | no | 720p down to 360p; the low rung matters most |
| `radio` | audio only | no | AAC and MP3, no picture |
| `web` | yes | no | 1080p down to 360p |

Set it per row (`channel` column) or per job. **The ladders shipped in
`config.example.yml` are conventional starting points, not a quotation of a
specific IAB document.** Every network and SSP publishes its own ingest spec and
that spec wins. Tune `delivery.channels.<name>.ladder` to what the buyer asks
for.

### Why the VAST tag looks the way it does

Each of these is a known failure mode if skipped:

- **`<Error>` carrying `[ERRORCODE]`.** Without it, a player rejecting the
  creative is invisible. You see zero impressions and no reason.
- **`<Mezzanine>` on CTV.** Server-side ad insertion re-encodes from it. Missing
  it is VAST error 406: no ad served on SSAI inventory.
- **`<UniversalAdId>`** (required in VAST 4) and **`<AdServingId>`** (4.1+).
  `idRegistry` stays `unknown` until the creative is registered with Ad-ID or
  Clearcast. A fake registry value is worse than an honest absence.
- **`[CACHEBUSTING]` left in tracking URLs** for the player to substitute. Without
  it you either double-count or lose hits to caching.
- **URLs in `<![CDATA[…]]>`.** An unescaped `&` is invalid XML, and an escaped
  one (`&amp;`) is what the player then requests, which breaks the query string.
- **No `<VideoClicks>` on DOOH.** There is no click on a street panel, and
  advertising one distorts click reporting.
- **Exact event vocabulary** (`firstQuartile`, never `first_quartile`). A
  misspelled event is silently never fired. It shows up as a hole in completion
  reporting, not as an error.

DOOH emits `creativeView` / `start` / `complete`; the other channels emit the
full quartile set.

### Validating a tag

```bash
xmllint --noout output/spot.vast.xml                        # well-formed
xmllint --xpath 'count(//MediaFile)' output/spot.vast.xml    # renditions present
xmllint --schema vast4.xsd output/spot.vast.xml --noout      # bring your own XSD
```

**Not yet done:** validation against the official IAB XSD, and a pass through the
IAB VAST Validator. The generator follows the documented InLine element order and
every tag it produces is well-formed and passes the xpath assertions above, but
"well-formed" is not "schema-valid". Do this before trafficking anything real.

---

## 7. Modes in detail

### Mode 1: stitching

Rushes are sequenced across columns: `clip_1`, `clip_2`, … Empty cells are
**skipped**, so a gap in the numbering does not truncate the sequence. Stills
(jpg/png) are first-class rushes, held for `clip_N_dur` or `still_duration_s`
seconds. A still endframe after two shots is a common DOOH sequence.

All inputs are normalized in a single FFmpeg pass: scaled and letterboxed into
the target format, square pixels, fps-locked. Heterogeneous sources are the norm,
not the exception.

`transition` cross-fades instead of hard-cutting, using any FFmpeg xfade name.
Offsets are computed from the clips' real durations, so the total is
`sum(durations) − (n−1) × transition_dur`. A transition longer than the shortest
clip would produce garbage and is refused rather than rendered.

For `radio`, the same sequencing logic runs without a picture: bed, voice-over,
tail. The voice-over is the segment DCO swaps.

### Mode 2: HTML5 variant capture

**Injection**, how the params reach the creative:

| Mode | Mechanism | Use when |
|---|---|---|
| `dom` | fills every `[data-dooh="key"]` element | the designer handed over plain HTML with no JS hook. **The common case.** |
| `query` | URL query string | the creative already reads `URLSearchParams` |
| `window_var` | `window.__DATA__`, set **before any page script runs** | the creative needs the data synchronously at parse time |
| `postmessage` | posted after load | the creative listens for a `message` event |

**Capture**, how the pixels come out:

| Mode | Mechanism | Trade-off |
|---|---|---|
| `static` | one screenshot | deterministic. The drive-to-store case. |
| `video` | Chromium records in real time → FFmpeg | correct timing for **CSS** animation. Default for animated. |
| `frames` | screenshot loop → FFmpeg | frame-exact **only** if the template exposes `window.__seek(ms)`. Without it, capture follows the wall clock and timing is approximate, and the run warns rather than shipping it silently. |
| `auto` | `static` without a duration, else the job's animated engine | |

The seek hook is the contract for exact animated capture:

```js
window.__seek = ms => document.getAnimations().forEach(a => { a.pause(); a.currentTime = ms; });
```

A template implementing it renders identically regardless of machine load. See
`templates/example_store.html`, which supports all four injection modes and the
hook.

A browser capture has **no audio**. A CTV or mobile variant built from an HTML5
template gets its sound from the row's `audio_track`, muxed at rendition time.

---

## 8. Running it

```bash
python run.py --job mode1 --once            # what cron calls
python run.py --job mode1 --dry-run         # resolve and plan, render nothing
python run.py --job mode1 --force           # re-render rows already done
python run.py --job mode2 --limit 5         # stop after 5 rendered rows
python run.py --job mode2 --workers 4       # render 4 rows concurrently
python run.py --job mode1 --csv rows.csv    # local CSV instead of the Sheet
python run.py --daemon                      # every job on its cadence
python run.py --print-cron                  # crontab lines to review and install
```

Exit code is non-zero when any row errored, so cron and CI notice.

### On `--workers`

It pays off for Mode 2 and not for Mode 1. Measured on an M1:

| Workload | Serial | Parallel | Gain |
|---|---|---|---|
| Mode 2, 6 static captures | 3.9s | 2.1s (3 workers) | ~1.9× |
| Mode 1, 8 stitched creatives | 7.0s | 6.8s (4 workers) | none |

Mode 2 gains because each row is mostly browser latency (page load and settle
waits), which leaves the CPU idle in between. Mode 1 does not: x264 already threads
across every core, so concurrent encodes split the same CPU rather than adding
throughput. Leave Mode 1 at 1.

Planning stays serial whatever the worker count, so skip decisions, the
name-collision guard and error messages are identical and reproducible.

### Scheduling

```
once | hourly | daily@HH:MM | every_N_minutes|_seconds|_hours | cron:<5 fields>
```

Two paths, same job definitions. Cron for once/daily/hourly: `--print-cron`, review, install by hand, since it
deliberately does not edit your crontab. The daemon
(`--daemon`, APScheduler) for minute cadence: jobs run coalesced with
`max_instances=1`, so a tick that overruns its slot is skipped rather than queued
behind itself, and a failing job never takes the daemon down.

---

## 9. Failure policy

- **Deterministic gate first.** `prepare()` validates every input path before any
  encode starts. A missing rush or template fails that row at zero cost.
- **One row never takes down the batch.** Every failure is caught per row,
  written back as `error` + `message`, and the run continues.
- **Loud over silent.** FFmpeg's stderr is surfaced to the sheet, an approximate
  frame capture warns, and a duplicate output name is an error rather than an
  overwrite.
- **Retries only where retrying helps.** Transient Sheets failures (429, 5xx,
  connection errors) retry with exponential backoff and jitter. A 403 is *not*
  retried. It means the sheet is not shared with the service account, and
  delaying that error helps nobody.

---

## 10. Code map

| File | Responsibility |
|---|---|
| `run.py` | entrypoint |
| `src/main.py` | CLI, config loading, job dispatch |
| `src/contract.py` | **the sheet data contract**: reserved columns, naming, idempotency |
| `src/runner.py` | the shared batch loop: skip/force, collisions, errors, concurrency |
| `src/mode1_stitch.py` | rushes → stitched creative |
| `src/mode2_html5.py` | HTML5 → captured variants (Playwright) |
| `src/renditions.py` | channel ladders, master → delivery renditions |
| `src/vast.py` | VAST 4.2 InLine generation |
| `src/ffmpeg_utils.py` | every FFmpeg call, explicit and logged |
| `src/naming.py` | naming templates, ASCII transliteration |
| `src/sheets.py` | gspread I/O, buffered batch write-back, retries |
| `src/sheet_model.py` | the three-tab model, as data |
| `src/scheduler.py` | cadence grammar, crontab generation, daemon |
| `scripts/bootstrap_sheet.py` | provision the sheet (or export it as CSV) |
| `scripts/expand_dco.py` | axes → one row per combination |

**Two files deserve care.** `contract.py` defines what the sheet means; changing
it affects campaign tabs that already exist in someone's Drive. `runner.py` is
the only place batch semantics live; if you find yourself duplicating its logic
inside a mode, stop and extend the hook instead.

---

## 11. Extending it

**Add a creative variable (Mode 2).** Add a column. It is injected automatically.

**Add a channel.** Add an entry under `delivery.channels` in the config: a
ladder, `audio`, optionally `mezzanine` or `audio_only`. No code change.

**Add a third mode.** Write `prepare()` and `render()`, register it in
`MODES` in `src/main.py`. The runner gives you skip logic, force, collision
guarding, per-row error reporting, concurrency and write-back for free.

**Change the ingest spec for one format.** `ffmpeg.profiles["1920x1080"]` is
merged over the base encoding block. It is part of the input hash, so changing
it invalidates exactly the rows it affects.

---

## 12. Testing

```bash
pytest tests -q               # 115 offline tests: no ffmpeg, no browser, no credentials
python tests/smoke_mode1.py   # real FFmpeg: stitching, transitions, profiles, idempotency
python tests/smoke_mode2.py   # real Chromium: all capture modes, concurrency
```

The offline suite covers the places where a silent wrong answer is expensive:
the idempotency decision, the naming contract, cadence safety, sheet parsing,
VAST structure, ladder resolution and DCO expansion. The smoke tests cover what only a
real encoder and a real browser can prove.

---

## 13. Known gaps

Honest list. None of these are hidden in a backlog somewhere else.

1. **The live gspread path has never made a real API call.** There is no service
   account on the build machine. Row parsing and write-back buffering are
   unit-tested against raw values; the network round-trip is not. **This is the
   first thing to verify.**
2. **No IAB XSD validation.** Tags are well-formed and structurally checked; they
   have not been run through the official validator.
3. **DV360 SDF export is not implemented.** The `media_dv360` tab is a *planning*
   tab built on DV360 concepts. SDF headers are version-specific (v7, v8, …) and
   must be taken from the SDF template your own DV360 instance exports. They
   were deliberately not guessed.
4. **No remote asset fetch/push.** Assets are local. Drive/GCS in and CDN out is
   a product decision that has not been made.
5. **Mode 2 static renditions.** A static variant produces one image; there is no
   image ladder and no VAST (VAST is for video and audio).
6. **The rendition ladders are conventions, not quoted specs.** See §6.
