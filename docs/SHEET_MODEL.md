# Sheet model: column reference

Generated from `src/sheet_model.py`, which is the source of truth, so this file
cannot drift from what the engine reads. Provision a real sheet with
`scripts/bootstrap_sheet.py`, or export the same model as CSV with `--csv-out`.

Columns marked **WRITTEN BACK** are owned by the tool. Everything else is input.
All written-back columns are optional: a sheet without one simply loses that
feature.

## `mode1`: stitched creatives

One row = one creative. Rushes are sequenced across `clip_1..clip_6`.

40 columns.

| Column | Meaning |
|---|---|
| `creative_id` | row id, and the naming fallback |
| `campaign` | DCO axis. Also the VAST AdTitle. |
| `advertiser` | DCO axis. Reporting only. |
| `market` | DCO axis |
| `language` | DCO axis |
| `audience` | DCO axis |
| `daypart` | DCO axis |
| `product` | DCO axis |
| `offer` | DCO axis |
| `price` | DCO axis |
| `cta` | DCO axis |
| `version` | DCO axis. Use it to break a naming collision. |
| `channel` | `dooh` / `ctv` / `mobile` / `radio` / `web`. Picks the ladder, the audio policy and the tracking event set. |
| `format` | `WxH` target, e.g. 1080x1920. Falls back to config. |
| `clip_1` | rush #1. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_1_dur` | trims rush #1; sets the hold if it is a still. |
| `clip_2` | rush #2. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_2_dur` | trims rush #2; sets the hold if it is a still. |
| `clip_3` | rush #3. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_3_dur` | trims rush #3; sets the hold if it is a still. |
| `clip_4` | rush #4. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_4_dur` | trims rush #4; sets the hold if it is a still. |
| `clip_5` | rush #5. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_5_dur` | trims rush #5; sets the hold if it is a still. |
| `clip_6` | rush #6. Empty cells are skipped, so a gap does not truncate the sequence. |
| `clip_6_dur` | trims rush #6; sets the hold if it is a still. |
| `transition` | ffmpeg xfade name (`fade`, `wipeleft`, ...). Empty = hard cut. |
| `transition_dur` | cross-fade length, seconds |
| `audio_track` | external soundtrack to mux. Required for a CTV/mobile variant built from mode2, since a browser capture has no audio. |
| `max_duration_s` | hard cap on the finished creative |
| `still_duration_s` | default hold for a still rush |
| `naming_template` | interpolated from the row, e.g. `{campaign}_{market}_{version}` |
| `output_name` | explicit name. Wins over `naming_template`. |
| `output_path` | **WRITTEN BACK**: the master |
| `status` | **WRITTEN BACK**: `pending` / `done` / `error` |
| `message` | **WRITTEN BACK**: error detail |
| `input_hash` | **WRITTEN BACK**: drives re-rendering |
| `rendered_at` | **WRITTEN BACK**: UTC timestamp |
| `vast_path` | **WRITTEN BACK**: the generated tag |
| `renditions` | **WRITTEN BACK**: the delivery ladder, comma-separated |

## `mode2`: HTML5 variants

One row = one variant. **Any column not listed as a control column below is injected into the template as a param**, so adding a creative variable is adding a column, with no code change.

39 columns.

| Column | Meaning |
|---|---|
| `variant_id` | row id, and the naming fallback |
| `campaign` | DCO axis. Also the VAST AdTitle. |
| `advertiser` | DCO axis. Reporting only. |
| `market` | DCO axis |
| `language` | DCO axis |
| `audience` | DCO axis |
| `daypart` | DCO axis |
| `product` | DCO axis |
| `offer` | DCO axis |
| `price` | DCO axis |
| `cta` | DCO axis |
| `claim` | DCO axis, injected (mode2) |
| `store_id` | DCO axis |
| `store_name` | injected param |
| `address` | injected param |
| `postal_code` | injected param |
| `city` | injected param |
| `phone` | injected param |
| `opening_hours` | injected param |
| `logo_url` | injected param, fills an img[data-dooh] |
| `image_url` | injected param, fills an img[data-dooh] |
| `template` | HTML5 creative under templates_dir, or an http(s) URL |
| `channel` | `dooh` / `ctv` / `mobile` / `radio` / `web`. Picks the ladder, the audio policy and the tracking event set. |
| `format` | `WxH` target, e.g. 1080x1920. Falls back to config. |
| `capture` | `static` / `video` / `frames` / `auto` |
| `inject` | `dom` / `query` / `window_var` / `postmessage` |
| `duration_s` | animation length. Empty means a still. |
| `audio_track` | external soundtrack to mux. Required for a CTV/mobile variant built from mode2, since a browser capture has no audio. |
| `wait_selector` | CSS selector to wait for before capturing |
| `wait_ms` | extra settle delay |
| `naming_template` | interpolated from the row, e.g. `{campaign}_{market}_{version}` |
| `output_name` | explicit name. Wins over `naming_template`. |
| `output_path` | **WRITTEN BACK**: the master |
| `status` | **WRITTEN BACK**: `pending` / `done` / `error` |
| `message` | **WRITTEN BACK**: error detail |
| `input_hash` | **WRITTEN BACK**: drives re-rendering |
| `rendered_at` | **WRITTEN BACK**: UTC timestamp |
| `vast_path` | **WRITTEN BACK**: the generated tag |
| `renditions` | **WRITTEN BACK**: the delivery ladder, comma-separated |

## `media_dv360`: the trafficking plan

One row = one line item x creative. This is a **planning** tab built on DV360 concepts, not a Structured Data File: SDF headers are version-specific (v7, v8, ...) and must come from the SDF template your own DV360 instance exports. They were deliberately not guessed here.

37 columns.

| Column | Meaning |
|---|---|
| `line_id` | your own row id |
| `status` | **WRITTEN BACK**: `pending` / `done` / `error` |
| `advertiser_id` |  |
| `advertiser_name` |  |
| `campaign_id` |  |
| `campaign_name` |  |
| `io_id` |  |
| `io_name` |  |
| `line_item_id` |  |
| `line_item_name` |  |
| `line_item_type` |  |
| `channel` | `dooh` / `ctv` / `mobile` / `radio` / `web`. Picks the ladder, the audio policy and the tracking event set. |
| `environment` |  |
| `start_date` |  |
| `end_date` |  |
| `budget_type` |  |
| `budget_amount` |  |
| `pacing` |  |
| `pacing_rate` |  |
| `bid_strategy` |  |
| `bid_amount` |  |
| `frequency_cap` |  |
| `geo_targeting` |  |
| `audience_targeting` |  |
| `daypart_targeting` |  |
| `inventory_source` |  |
| `deal_id` |  |
| `creative_source` | `hosted` (upload the rendition into DV360) or `third_party` (traffic our VAST tag) |
| `creative_tab` | `mode1` or `mode2`: which tab the creative lives on |
| `creative_id` | row id, and the naming fallback |
| `creative_name` | name to give the creative in DV360 |
| `creative_type` | `video` / `audio` / `display` / `native` |
| `duration_s` | animation length. Empty means a still. |
| `dimensions` | `WxH` of the asset trafficked |
| `hosted_asset` | lookup: pulls `renditions` from the creative row |
| `third_party_vast_url` | lookup: pulls `vast_path` from the creative row |
| `notes` | free text |

### mode2 control columns (never injected)

`audio_track`, `capture`, `channel`, `creative_id`, `device_scale_factor`, `duration_s`, `format`, `fps`, `inject`, `naming_template`, `output_name`, `static_format`, `template`, `variant_id`, `wait_ms`, `wait_selector`

Plus every written-back column. Anything else becomes a creative param.

### Vocabularies

Columns with a fixed vocabulary get a dropdown when the sheet is provisioned.
The validation is deliberately non-strict: it warns rather than blocking an
ops person who knows what they are doing.

| Column | Values |
|---|---|
| `status` | pending, done, error |
| `channel` | dooh, ctv, mobile, radio, web |
| `format` | 1080x1920, 1920x1080, 3840x2160, 2160x3840, 1080x1080, 1280x720, 640x360 |
| `transition` | fade, dissolve, wipeleft, wiperight, slideup, slidedown, circleopen, smoothleft |
| `capture` | auto, static, video, frames |
| `inject` | dom, query, window_var, postmessage |
| `creative_source` | hosted, third_party |
| `creative_tab` | mode1, mode2 |
| `creative_type` | video, audio, display, native |
| `line_item_type` | Display, Video, Audio, Digital out of home |
| `environment` | dooh, ctv, mobile_app, mobile_web, desktop, audio |
| `budget_type` | Amount, Impressions |
| `pacing` | Flight, Daily |
| `pacing_rate` | ASAP, Even, Ahead |
| `bid_strategy` | Fixed, Maximize conversions, Maximize viewable impressions, Beat the deal |
