# dooh-factory - Project Brief

**Status:** P0-P7 built and tested end-to-end (v1) - pending a live Sheet run
**Product doc:** `docs/PRODUCT.md` is the entry point for anyone new. This brief is the original spec plus its history.
**Owner:** Radwan Mezzi
**Created:** 2026-07-25
**Updated:** 2026-07-26
**Language note:** internal docs in English (HQ rule). Domain terms kept as-is (créa, rush, repiquage).

---

## 1. Purpose

Automated DOOH (Digital Out Of Home) creative factory. Generates rendered ad creatives at scale from a Google Sheet driver, in two modes, and runs on a configurable schedule (once / daily / hourly / minute).

This is the productized version of the ad-stitching / DCO pattern: **render creative variants server-side to flat video/image assets** (MP4 / JPG-PNG), the format DOOH screens actually play. No HTML is shipped to the screen - the screen plays the rendered output.

Two independent modes, one shared spine (Google Sheet in, FFmpeg out, naming convention, write-back of output path).

---

## 2. Mode 1 - FFmpeg stitching from a Google Sheet

**Goal:** stitch a sequence of rushes (video clips / assets) into a finished creative, per a sequencing defined in a spreadsheet.

**Input:** a Google Sheet tab where **each row = one creative to build**, and the **clip sequence is laid out by columns** (col = position in the sequence).

Example schema (columns):

| creative_id | clip_1 | clip_2 | clip_3 | ... | market | format | version | naming_template | output_path | status |
|---|---|---|---|---|---|---|---|---|---|---|
| 001 | intro.mp4 | product.mp4 | cta.mp4 | | FR | 1080x1920 | a | `{campaign}_{market}_{format}_{version}` | *(written back)* | *(written back)* |

- `clip_1..clip_N` - ordered rushes. Reference = local path, or an asset key resolved against a base assets dir (config). Empty cells are **skipped**, so a gap in the numbering does not truncate the sequence.
- `clip_N_dur` - optional, seconds. Trims a video clip; sets the hold duration of a still image.
- Still images (jpg/png/webp) are first-class rushes - a still endframe after two video shots is a common DOOH sequence.
- `max_duration_s` - optional hard cap on the finished creative.
- Naming convention: either explicit `output_name`, or a `naming_template` interpolated from the row's fields. `output_name` wins. Accents are transliterated (`Soldes Été` -> `Soldes-Ete`), not stripped.
- Two rows resolving to the same output name is an **error on the second row**, never a silent overwrite.
- `output_path` - **written back** by the tool once rendered.
- `status` - **written back**: `pending` / `done` / `error` (+ message).

**Process:**
1. Read the sheet (gspread / Sheets API, service account).
2. For each row with `status != done`: resolve clip paths, normalize (resolution / fps / SAR / pixel format), stitch with FFmpeg.
3. Encode to DOOH-spec output (see §5).
4. Write output file with the naming convention; write `output_path` + `status` back to the sheet.

**Stitching detail:** default = sequential concatenation. Use FFmpeg `concat` demuxer when clips share codec/params, else the `concat` filter with a normalization pass. Optional transitions (`xfade`) driven by an optional column.

---

## 3. Mode 2 - HTML5 → variant capture (DCO)

**Goal:** take an HTML5 creative (template), inject variable parameters, and capture each variant as a flat asset. Primary use case: **drive-to-store / repiquage d'adresse** (one variant per store/location, address + name injected).

**Input:** a Google Sheet tab where **each row = one variant**:

| variant_id | template | store_name | address | city | phone | ...params | naming_template | output_path | status |
|---|---|---|---|---|---|---|---|---|---|
| 001 | promo.html | Magasin Lyon 3 | 12 rue X | Lyon | ... | | `{campaign}_{store_id}` | *(written back)* | *(written back)* |

- `template` - path/URL of the HTML5 creative.
- Remaining columns = variable params injected into the creative.
- Same naming + write-back contract as Mode 1.

**Process:**
1. Read the sheet.
2. For each variant: load the HTML5 template in a headless browser (Playwright / Chromium), inject params (URL query string, `postMessage`, or a `window.__DATA__` object - configurable per template).
3. Capture:
   - **Static creative** → screenshot (PNG/JPG).
   - **Animated creative** → record to video (Playwright video or frame-by-frame screenshots at target fps) then FFmpeg-encode to MP4.
4. Same DOOH output spec + naming + write-back.

**Why not ship HTML to the screen:** DOOH players run flat assets (MP4/static) via their CMS, delivered programmatically over VAST. The dynamic layer is rendered server-side into those flat assets. This engine is that render layer.

### 3.1 Injection modes (implemented)

| Mode | How | Use it when |
|---|---|---|
| `dom` | fills every `[data-dooh="<key>"]` element (textContent, or `src`/`href` for img/a) | the designer handed over plain HTML with no JS hook - the common case |
| `query` | params in the URL query string | the creative already reads `URLSearchParams` |
| `window_var` | `window.__DATA__` set via `add_init_script`, **before any page script runs** | the creative needs the data synchronously at parse time |
| `postmessage` | posted to the page after load | the creative listens for a `message` event |

### 3.2 Capture modes (implemented)

| Mode | How | Trade-off |
|---|---|---|
| `static` | one screenshot -> jpg/png | deterministic. The drive-to-store / repiquage case |
| `video` | Chromium records in real time -> ffmpeg normalizes | correct timing for **CSS** animation. Default for animated |
| `frames` | screenshot loop -> ffmpeg | frame-exact **only** if the template exposes `window.__seek(ms)`; otherwise wall-clock and approximate - the runner warns rather than shipping it silently |
| `auto` | `static` with no `duration_s`, else the job's `animated_engine` | |

The seek hook is the contract for exact animated capture: a template that
implements `window.__seek(ms)` (pause all animations, set `currentTime`) is
rendered deterministically regardless of machine load. See
`templates/example_store.html`.

---

## 4. Scheduling

Configurable cadence per job (a "job" = one mode + one sheet + config). Two run models:

- **Cron** (recommended for once/daily/hourly): OS cron or a scheduled runner invoking `main.py --job <name>`.
- **Daemon** (for minute-level near-real-time): long-running process with an internal scheduler (APScheduler or a simple loop), re-reading the sheet each tick.

Cadence options: `once`, `hourly`, `daily@HH:MM`, `every_N_minutes` (also `_seconds`/`_hours`), `cron:<5-field expression>`.

**Design principle (Rad's rule), enforced in code:** do not render more often than the source data changes. `parse_cadence` refuses any interval below `scheduler.min_interval_seconds` (default 60), so a typo cannot become a per-second hammering of the Sheets API. Daemon jobs run with `coalesce=True, max_instances=1`: a tick that overruns its slot is skipped, not queued behind itself, and a failing job never takes the daemon down.

**Idempotency (implemented):** a row is skipped when `status = done`, unless
`--force`, or the sheet has an `input_hash` column whose stored value no longer
matches. The hash covers the row's input cells, the identity of every resolved
input file (name + size + mtime), and the effective render spec (format, fps,
codec, rate control) - so replacing a rush on disk or changing the codec in
config correctly invalidates every affected row.

### 4.1 Reserved columns (the write-back contract)

| Column | Purpose |
|---|---|
| `output_path` | path of the rendered asset |
| `status` | `pending` / `done` / `error` |
| `message` | error detail; when the column is absent the detail rides along in `status` |
| `input_hash` | input fingerprint, drives re-render |
| `rendered_at` | UTC ISO timestamp of the last render |

All optional and additive: a sheet with only `output_path` + `status` keeps
working unchanged. Write-backs are buffered and flushed in one `batch_update` -
one cell at a time burns the 60 writes/min/user quota after a few dozen rows.

---

## 5. DOOH output spec (defaults, configurable)

- Container: **MP4 (H.264)**, or **JPG/PNG** for static.
- Resolution / aspect ratio: **per-creative** (portrait 1080x1920, landscape 1920x1080, custom). Driven by a `format` field. Sources are letterboxed into the target, SAR normalized, fps-locked.
- Audio: **none**. Public screens are muted; `-an` on every output.
- Loop / duration: typically **6-15s** (default 10s), configurable; `max_duration_s` caps any creative.
- Rate control: **CRF** by default, **CBR-ish** (`-b:v` / `-maxrate` / `-bufsize`) as soon as `ffmpeg.bitrate` is set - which is what most DOOH CMS ingest specs ask for.
- Also configurable because CMS ingest specs demand them: `profile` (default high), `level` (4.1), `gop` (keyframe interval), `faststart` (moov atom up front).
- No click (OOH). Measurement is out of scope here.

---

## 6. Tech stack

- **Python** - orchestration, sheet I/O, job runner.
- **FFmpeg** - stitching (Mode 1), encoding both modes. Called via subprocess (explicit, logged commands) - not a fragile wrapper lib.
- **Google Sheets API** - `gspread` + service account (read + write-back).
- **Playwright (headless Chromium)** - Mode 2 HTML5 render + capture.
- **Scheduler** - cron (external) and/or APScheduler (daemon).

---

## 7. Directory structure

```
dooh-factory/
├── README.md
├── requirements.txt
├── docs/
│   └── BRIEF.md         # this file
├── config/
│   └── config.example.yml
├── run.py               # entrypoint (python run.py --help)
├── src/
│   ├── main.py          # CLI: --job/--daemon/--print-cron/--csv/--force/--dry-run
│   ├── contract.py      # THE sheet data contract: reserved cols, naming, idempotency
│   ├── runner.py        # mode-agnostic batch loop (skip/force/collision/summary)
│   ├── sheets.py        # Google Sheet read + buffered batch write-back
│   ├── naming.py        # naming convention interpolation + ASCII transliteration
│   ├── ffmpeg_utils.py  # normalize / concat / encode / capture-transcode helpers
│   ├── mode1_stitch.py  # rushes -> stitched creative
│   ├── mode2_html5.py   # HTML5 -> injected variants capture (Playwright)
│   └── scheduler.py     # cadence parsing, crontab generation, APScheduler daemon
├── tests/
│   ├── test_unit.py     # offline: no ffmpeg, no browser, no credentials
│   ├── smoke_mode1.py   # real ffmpeg stitch + idempotency contract
│   └── smoke_mode2.py   # real Chromium capture, all three capture modes
├── assets/              # input rushes (gitignored)
├── templates/           # HTML5 creative templates (+ example_store.html)
└── output/              # rendered creatives (gitignored)
```

Both modes expose the same two hooks to `runner.py` - `prepare(row, cfg, job)`
resolves inputs/name/fingerprint (the deterministic gate: it fails loud on a
missing asset before anything expensive starts), `render(plan, cfg, job)` builds
the asset. Everything else - skip logic, force, name-collision guard, per-row
error reporting, batch summary, write-back flush - lives once in the runner, so
the two modes cannot drift apart.

---

## 8. Failure policy

- **Deterministic gate first.** `prepare()` resolves and validates every input path before any encode starts. A missing rush or template fails that row loudly, at zero cost.
- **One row never takes down the batch.** Every failure is caught per row, written back as `error` + `message`, and the run continues.
- **Loud over silent.** ffmpeg stderr is surfaced to the sheet. An approximate frame capture warns. A duplicate output name is an error, not an overwrite.
- **Non-zero exit** when any row errored, so cron and CI notice.

---

## 9. Assumptions

Settled:

1. **"resh" = rushes** (raw video clips) - confirmed by Rad. Still images are supported as rushes too.
2. **Mode 1 stitching = sequential concat.** Overlay/PiP composition remains out of scope.
3. **Naming** = `output_name` (explicit) or `naming_template` interpolated from row fields. Implemented, `output_name` wins.
4. **Mode 2 injection** - `dom` is the default (fills `[data-dooh]`, no JS required from the designer); the other three are available per row or per job.
5. **Sheet auth** = Google service account with the sheet shared to it.

Still open (do not block anything already built):

6. **Assets location** - local paths today. Do rushes / templates ultimately live on Drive or GCS? Affects a fetch step in P4.
7. **Output destination** - local `output/` today, then push to Drive/GCS/CDN? The sheet gets the path either way.

---

## 10. Build phases

- **P0 (foundations)** - done: brief, scaffold, config, deps.
- **P1 (Mode 1)** - done + tested: heterogeneous rushes and stills -> stitched DOOH creative, per-clip trim, duration cap, CMS-grade encoding params.
- **P2 (Mode 2)** - done + tested: Playwright capture, 4 injection modes, 3 capture modes, one browser per batch.
- **P3 (scheduling + idempotency)** - done + tested: cadence grammar with a minimum-interval floor, crontab generation, APScheduler daemon, input-hash idempotency, `--force` / `--dry-run` / `--limit` / `--csv`.
- **P5 (delivery)** - done + tested: rendition ladder per channel (dooh / ctv / mobile / web), audio as a channel policy rather than a hardcoded `-an`, VAST 4.2 InLine generation. See §11.
- **P6 (DCO model)** - done + tested: three-tab sheet model with the campaign axes as columns, sheet provisioning, and cartesian expansion of axes into rows. See `docs/SHEET_MODEL.md`.
- **P7 (radio)** - done + tested: audio-only channel, `concat_audio`, codec/container ladder, audio MediaFiles.
- **P4 (hardening)** - done + tested: `xfade` transitions with offset layout and a guard against a transition longer than the shortest clip; per-format encoding profiles (`ffmpeg.profiles`); retries with jittered backoff on transient Sheets/network failures (429/5xx and connection errors, never on 403); concurrent rendering with a serial planning phase.
  - **Still open:** the live Sheet run (needs a service account), and remote asset fetch/push, which is blocked on assumptions 6-7 above - the destination is a product decision, not a technical one.

### Concurrency, measured (M1, 2026-07-26)

| Workload | Serial | Parallel | Gain |
|---|---|---|---|
| Mode 2, 6 static captures | 3.9s | 2.1s (3 workers) | ~1.9x |
| Mode 1, 8 stitched creatives | 7.0s | 6.8s (4 workers) | none |

Mode 2 gains because each row is dominated by browser latency - page load and
settle waits - leaving the CPU idle in between. Mode 1 does not: x264 already
threads across every core, so concurrent encodes split the same CPU rather than
adding throughput. `--workers` is a Mode 2 tool; Mode 1 stays at 1 unless the
rushes are small enough that ffmpeg process startup dominates.

Planning stays serial in both cases, so skip decisions, the name-collision guard
and error messages are identical whatever the worker count.

### Verified (2026-07-26)

- 80 offline unit tests pass (`pytest tests -q`).
- `smoke_mode1.py`: 3 heterogeneous rushes + still endframe -> 1080x1920 @ 25fps, muted, H.264 High/4.1, duration cap honoured; second run skips; a rush changed on disk re-renders; `--force` re-renders; a missing rush errors that row only; xfade shortens 9.0s to 7.5s across 3 transitions; an over-long transition is refused; a per-format profile applies CBR 10M to 1920x1080 and leaves portrait untouched; 6 rows on 3 workers all render valid outputs.
- `smoke_mode2.py`: static repiquage (two stores, verified visually and by output divergence), real-time video capture (~3s), deterministic frame capture via the seek hook (2.0s), query injection, idempotent second run, missing template errors that row only; 5 variants across 3 concurrent browsers with a bad row contained.
- CLI: `--dry-run`, `--csv`, `--force`, `--limit`, `--print-cron`, exit code 1 when any row errored. Daemon ticks, skips `once` jobs, survives a failing job.
- **Not yet verified:** the live gspread path (`open_worksheet` / `read_rows` / `make_write_back`) - no service account on this machine. Parsing and write-back buffering are unit-tested against raw values; the network call is not.


---

## 11. Delivery: renditions and VAST (P5)

A creative is not one file. Each row produces a master, then a rendition ladder
sized for its `channel`, then a VAST 4.2 InLine referencing every rendition.

| Channel | Sound | Mezzanine | Ladder |
|---|---|---|---|
| `dooh` | no, screens are muted | no | one high-bitrate rendition at panel size |
| `ctv` | yes | yes, SSAI re-encodes from it | 1080p + 720p |
| `mobile` | yes | no | 720p down to 360p |
| `radio` | audio only | no | AAC + MP3, no picture |
| `web` | yes | no | 1080p down to 360p |

**The ladders shipped in `config.example.yml` are conventional starting points,
not a quotation of a specific IAB document.** Every network and SSP publishes its
own ingest spec and that spec wins.

Master and renditions stay separate on purpose: the master is encoded for
quality, a rendition for a delivery bitrate. Shipping the master as the "600k
mobile file" is how a 40 MB asset ends up in a mobile tag.

### What the tag carries, and why

Each item below is a known failure mode when it is missing.

- `<Error>` with `[ERRORCODE]`: without it, a player rejecting the creative is
  invisible. Zero impressions and no reason.
- `<Mezzanine>` on CTV: server-side ad insertion re-encodes from it. Missing is
  VAST error 406, so no ad served on SSAI inventory.
- `<UniversalAdId>` (VAST 4) and `<AdServingId>` (4.1+). `idRegistry` stays
  `unknown` until the creative is registered with Ad-ID or Clearcast.
- `[CACHEBUSTING]` left in tracking URLs for the player to substitute.
- URLs in CDATA: an unescaped `&` is invalid XML, an escaped one breaks the
  query string the player calls.
- No `<VideoClicks>` on DOOH: there is no click on a street panel.
- Exact event vocabulary. A misspelled event is silently never fired.

**Not done:** validation against the official IAB XSD, and a pass through the IAB
VAST Validator. Tags are well-formed and pass xpath assertions on the critical
nodes, but well-formed is not schema-valid. Do this before trafficking anything.

### Audio (P5/P7)

The pipeline forced `-an` everywhere because DOOH screens are muted. Audio is now
a channel policy. Concatenating it means clips without an audio track, and
stills, are filled with silence of the right length, because ffmpeg's concat
filter refuses a stream count that varies between segments. Transitions
cross-fade the audio to match. A browser capture carries no audio at all, which
is what the `audio_track` column is for.
