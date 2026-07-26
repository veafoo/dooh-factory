# Sheet model: column reference

Generated from `src/sheet_model.py`, which is the source of truth. Provision a
real sheet with `scripts/bootstrap_sheet.py`, or export the same model as CSV
with `--csv-out`.

Columns marked **WRITTEN BACK** are owned by the tool. Everything else is input.

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
| `channel` | dooh | ctv | mobile | radio | web. Picks the ladder, the audio policy and the tracking event set. |
| `format` | WxH target, e.g. 1080x1920. Falls back to config. |
| `clip_1` | rush #1. Empty cells are skipped, not treated as the end. |
| `clip_1_dur` | trims rush #1; sets the hold if it is a still. |
| `clip_2` | rush #2. Empty cells are skipped, not treated as the end. |
| `clip_2_dur` | trims rush #2; sets the hold if it is a still. |
| `clip_3` | rush #3. Empty cells are skipped, not treated as the end. |
| `clip_3_dur` | trims rush #3; sets the hold if it is a still. |
| `clip_4` | rush #4. Empty cells are skipped, not treated as the end. |
| `clip_4_dur` | trims rush #4; sets the hold if it is a still. |
| `clip_5` | rush #5. Empty cells are skipped, not treated as the end. |
| `clip_5_dur` | trims rush #5; sets the hold if it is a still. |
| `clip_6` | rush #6. Empty cells are skipped, not treated as the end. |
| `clip_6_dur` | trims rush #6; sets the hold if it is a still. |
| `transition` | ffmpeg xfade name. Empty = hard cut. |
| `transition_dur` | cross-fade length, seconds |
| `audio_track` | external soundtrack to mux (required for CTV/mobile from mode2) |
| `max_duration_s` | hard cap on the finished creative |
| `still_duration_s` | default hold for a still rush |
| `naming_template` | interpolated from the row, e.g. {campaign}_{market}_{version} |
| `output_name` | explicit name. Wins over naming_template. |
| `output_path` | WRITTEN BACK: the master |
| `status` | WRITTEN BACK: pending | done | error |
| `message` | WRITTEN BACK: error detail |
| `input_hash` | WRITTEN BACK: drives re-rendering |
| `rendered_at` | WRITTEN BACK: UTC timestamp |
| `vast_path` | WRITTEN BACK: the generated tag |
| `renditions` | WRITTEN BACK: the delivery ladder, comma-separated |

## `mode2`: HTML5 variants

One row = one variant. **Any column not listed as a control column below is injected into the template as a param**, so adding a creative variable is adding a column.

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
| `channel` | dooh | ctv | mobile | radio | web. Picks the ladder, the audio policy and the tracking event set. |
| `format` | WxH target, e.g. 1080x1920. Falls back to config. |
| `capture` | static | video | frames | auto |
| `inject` | dom | query | window_var | postmessage |
| `duration_s` | animation length. Empty means a still. |
| `audio_track` | external soundtrack to mux (required for CTV/mobile from mode2) |
| `wait_selector` | CSS selector to wait for before capturing |
| `wait_ms` | extra settle delay |
| `naming_template` | interpolated from the row, e.g. {campaign}_{market}_{version} |
| `output_name` | explicit name. Wins over naming_template. |
| `output_path` | WRITTEN BACK: the master |
| `status` | WRITTEN BACK: pending | done | error |
| `message` | WRITTEN BACK: error detail |
| `input_hash` | WRITTEN BACK: drives re-rendering |
| `rendered_at` | WRITTEN BACK: UTC timestamp |
| `vast_path` | WRITTEN BACK: the generated tag |
| `renditions` | WRITTEN BACK: the delivery ladder, comma-separated |

## `media_dv360`: the trafficking plan

One row = one line item x creative. This is a **planning** tab built on DV360 concepts, not a Structured Data File: SDF headers are version-specific and must come from your own DV360 export.

37 columns.

| Column | Meaning |
|---|---|
| `line_id` |  |
| `status` | WRITTEN BACK: pending | done | error |
| `advertiser_id` |  |
| `advertiser_name` |  |
| `campaign_id` |  |
| `campaign_name` |  |
| `io_id` |  |
| `io_name` |  |
| `line_item_id` |  |
| `line_item_name` |  |
| `line_item_type` |  |
| `channel` | dooh | ctv | mobile | radio | web. Picks the ladder, the audio policy and the tracking event set. |
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
| `creative_source` |  |
| `creative_tab` |  |
| `creative_id` | row id, and the naming fallback |
| `creative_name` |  |
| `creative_type` |  |
| `duration_s` | animation length. Empty means a still. |
| `dimensions` |  |
| `hosted_asset` |  |
| `third_party_vast_url` |  |
| `notes` |  |

### mode2 control columns (never injected)

`audio_track`, `capture`, `channel`, `creative_id`, `device_scale_factor`, `duration_s`, `format`, `fps`, `inject`, `naming_template`, `output_name`, `static_format`, `template`, `variant_id`, `wait_ms`, `wait_selector`

Plus every written-back column. Anything else becomes a creative param.
