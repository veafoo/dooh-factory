"""Mode 1: stitch rushes sequenced by column in the sheet into a creative.

Sheet contract (on top of the reserved columns in `contract.py`):

    clip_1 .. clip_N     ordered rushes; a path, or a key under assets_dir.
                         Empty cells are skipped, so a gap in the numbering
                         does not truncate the sequence.
    clip_N_dur           optional, seconds. Trims a video clip; sets the hold
                         duration of a still image.
    format               'WxH', e.g. 1080x1920. Falls back to config.
    max_duration_s       optional hard cap on the finished creative.
    still_duration_s     optional default hold for still images in this row.
    transition           optional ffmpeg xfade name (fade, wipeleft, dissolve,
                         circleopen...). Empty = hard cut.
    transition_dur       optional cross-fade length, seconds.

Stills (jpg/png) are first-class rushes - a lot of DOOH sequences are a still
endframe after two video shots.

`prepare`/`render` are the runner's two hooks; nothing here touches gspread,
so the whole mode is testable offline with plain dicts.
"""
from __future__ import annotations

import logging
import os
import re

from . import contract, ffmpeg_utils, runner

log = logging.getLogger("dooh.mode1")

_CLIP = re.compile(r"^clip_(\d+)$")
_CLIP_DUR = re.compile(r"^clip_(\d+)_dur(?:ation)?(?:_s)?$")


def _num(value, default=None):
    """Lenient sheet-cell -> float. Accepts '2,5' (FR locale) and blanks."""
    s = str(value or "").strip().replace(",", ".")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def collect_clips(row: dict, assets_dir: str) -> tuple[list[str], list[float | None]]:
    """Ordered clip paths and their per-clip durations.

    Reads clip_1, clip_2, ... in numeric order, skipping empty cells, and pairs
    each with its optional `clip_N_dur`. Resolves relative refs against
    assets_dir and verifies every file exists - the deterministic gate: fail
    loud before touching ffmpeg.
    """
    refs: dict[int, str] = {}
    durs: dict[int, float | None] = {}
    for k, v in row.items():
        m = _CLIP.match(k)
        if m and str(v).strip():
            refs[int(m.group(1))] = str(v).strip()
            continue
        m = _CLIP_DUR.match(k)
        if m:
            d = _num(v)
            if d:
                durs[int(m.group(1))] = d

    clips, durations, missing = [], [], []
    for idx in sorted(refs):
        path = contract.resolve_asset(refs[idx], assets_dir)
        if not os.path.isfile(path):
            missing.append(path)
        clips.append(path)
        durations.append(durs.get(idx))
    if missing:
        raise FileNotFoundError("clip not found: " + ", ".join(missing))
    if not clips:
        raise ValueError("row has no clips")
    return clips, durations


def prepare(row: dict, cfg: dict, job: dict | None = None) -> dict:
    """Resolve inputs, target spec and output name for one row."""
    job = job or {}
    paths, ff = cfg["paths"], cfg.get("ffmpeg", {})
    clips, durations = collect_clips(row, paths["assets_dir"])

    default_wh = (int(ff.get("width", 1080)), int(ff.get("height", 1920)))
    width, height = contract.parse_format(row.get("format"), default_wh)
    fps = int(_num(job.get("fps"), None) or ff.get("fps", 25))
    max_duration = _num(row.get("max_duration_s"), None) or _num(
        job.get("max_duration_s"), None) or _num(ff.get("max_duration_s"), None)
    still = (_num(row.get("still_duration_s"), None) or _num(job.get("still_duration_s"), None)
             or _num(ff.get("still_duration_s"), 4.0))

    # Per-format ingest spec, if this screen format declares one.
    enc = contract.encoding_for(ff, width, height)

    transition = str(row.get("transition", "") or job.get("transition", "") or "").strip().lower()
    trans_dur = (_num(row.get("transition_dur"), None) or _num(job.get("transition_dur"), None)
                 or _num(ff.get("transition_duration_s"), 0.5))
    # Transitions need real clip lengths to lay out the xfade offsets, which
    # costs one ffprobe per clip - so only pay for it when they are requested.
    clip_lengths = (ffmpeg_utils.effective_durations(
        clips, durations, still, enc.get("ffprobe_bin", "ffprobe")) if transition else None)

    stem = contract.output_stem(row, job.get("naming_fallback", "{creative_id}"))
    # The render spec is part of the fingerprint: change the codec or the
    # target format in config and every done row correctly goes stale.
    spec = "|".join(str(x) for x in [
        width, height, fps, max_duration, still, transition, trans_dur,
        enc.get("video_codec"), enc.get("crf"), enc.get("bitrate"), enc.get("maxrate"),
        enc.get("profile"), enc.get("level"), enc.get("gop"), enc.get("pix_fmt"),
    ])
    return {
        "stem": stem,
        "files": clips,
        "hash": contract.compute_input_hash(row, clips, spec),
        "clips": clips,
        "durations": durations,
        "clip_lengths": clip_lengths,
        "width": width,
        "height": height,
        "fps": fps,
        "max_duration": max_duration,
        "still_duration": still,
        "transition": transition or None,
        "transition_duration": trans_dur,
        "encoding": enc,
        "dst": os.path.join(paths["output_dir"], f"{stem}.mp4"),
    }


def render(plan: dict, cfg: dict, job: dict | None = None) -> str:
    """Stitch one prepared row into its finished creative."""
    os.makedirs(os.path.dirname(plan["dst"]) or ".", exist_ok=True)
    ffmpeg_utils.concat_normalized(
        plan["clips"], plan["dst"],
        width=plan["width"], height=plan["height"], fps=plan["fps"],
        ff=plan["encoding"], durations=plan["durations"],
        still_duration=plan["still_duration"], max_duration=plan["max_duration"],
        transition=plan["transition"], transition_duration=plan["transition_duration"],
        clip_durations=plan["clip_lengths"],
    )
    return plan["dst"]


def render_row(row: dict, cfg: dict, job: dict | None = None) -> str:
    """Build one creative from one row, return the output path (single-row helper)."""
    return render(prepare(row, cfg, job), cfg, job)


def run(cfg: dict, rows, write_back, job: dict | None = None, **opts) -> runner.Summary:
    """Render every pending row. See `runner.run_rows` for the options."""
    return runner.run_rows(rows, write_back, cfg, job or {},
                           prepare=prepare, render=render, **opts)
