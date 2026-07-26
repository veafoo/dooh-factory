"""Delivery renditions: one master in, an IAB-shaped ladder out.

An ad is not one file. A player picks the rendition it can decode at the
bitrate the connection allows, which is why a VAST tag carries several
`<MediaFile>` entries. The same creative therefore ships as a ladder, and the
ladder differs by channel:

- **DOOH** - the screen plays what its CMS ingested. One or two high-bitrate
  renditions, muted, sized to the panel.
- **CTV** - big screen, good pipe, sound on. High bitrates, H.264 High profile,
  and a `<Mezzanine>` because server-side ad insertion needs one (a missing
  mezzanine is VAST error 406 and no ad served).
- **Mobile** - narrow and variable pipe. A low rung matters more than a high one.

The ladders in `config.example.yml` are conventional starting points, not a
quotation of a specific IAB document: every network and SSP publishes its own
ingest spec, and that spec wins. Tune `delivery.channels.<name>.ladder` to
whatever the buyer actually asks for.
"""
from __future__ import annotations

import logging
import os

from . import ffmpeg_utils

log = logging.getLogger("dooh.renditions")

DEFAULT_LADDERS = {
    "dooh": {"audio": False, "mezzanine": False,
             "ladder": [{"width": 1080, "height": 1920, "bitrate": "8M"}]},
    "ctv": {"audio": True, "mezzanine": True,
            "ladder": [{"width": 1920, "height": 1080, "bitrate": "8M"},
                       {"width": 1280, "height": 720, "bitrate": "4M"}]},
    "mobile": {"audio": True, "mezzanine": False,
               "ladder": [{"width": 1280, "height": 720, "bitrate": "2M"},
                          {"width": 854, "height": 480, "bitrate": "1M"},
                          {"width": 640, "height": 360, "bitrate": "600k"}]},
    "radio": {"audio": True, "mezzanine": False, "audio_only": True,
              "audio_spec": {"codec": "aac", "bitrate": "192k",
                             "sample_rate": 48000, "channels": 2},
              "ladder": [{"container": "m4a", "audio_bitrate": "192k"},
                         {"container": "mp3", "codec": "libmp3lame",
                          "audio_bitrate": "128k"}]},
    "web": {"audio": True, "mezzanine": False,
            "ladder": [{"width": 1920, "height": 1080, "bitrate": "4M"},
                       {"width": 1280, "height": 720, "bitrate": "2M"},
                       {"width": 640, "height": 360, "bitrate": "800k"}]},
}


def channel_config(cfg: dict, channel: str) -> dict:
    """Ladder + audio policy for a channel, config overriding the defaults."""
    channel = (channel or "dooh").strip().lower()
    delivery = cfg.get("delivery") or {}
    configured = (delivery.get("channels") or {}).get(channel)
    base = DEFAULT_LADDERS.get(channel)
    if configured is None and base is None:
        raise ValueError(
            f"unknown channel '{channel}'; configure delivery.channels.{channel} "
            f"or use one of: {', '.join(DEFAULT_LADDERS)}")
    merged = dict(base or {})
    merged.update(configured or {})
    if not merged.get("ladder"):
        raise ValueError(f"channel '{channel}' has an empty ladder")
    return merged


def audio_spec(channel_cfg: dict, rung: dict) -> dict | bool | None:
    """Audio settings for one rung; the rung may override the channel."""
    if not channel_cfg.get("audio"):
        return None
    spec = dict(channel_cfg.get("audio_spec") or {})
    for key in ("codec", "bitrate", "sample_rate", "channels"):
        if f"audio_{key}" in rung:
            spec[key] = rung[f"audio_{key}"]
    return spec or True


def build(master: str, stem: str, *, cfg: dict, channel: str, fps: int,
          out_dir: str, audio_track: str | None = None) -> list[dict]:
    """Transcode `master` into every rung. Returns MediaFile-ready descriptors.

    The rung whose size matches the master is still transcoded rather than
    copied: the master is encoded for quality, a rendition for a delivery
    bitrate, and shipping the former as the latter is how a 40 MB "600k mobile"
    file ends up in a tag.
    """
    ch = channel_config(cfg, channel)
    ff_base = cfg.get("ffmpeg", {})
    os.makedirs(out_dir, exist_ok=True)
    out = []

    if ch.get("audio_only"):
        return _audio_ladder(master, stem, ch, ff_base, out_dir)

    for rung in ch["ladder"]:
        width, height = int(rung["width"]), int(rung["height"])
        ff = {k: v for k, v in ff_base.items() if k != "profiles"}
        # A rung states a delivery bitrate, so it must win over the master's CRF.
        ff.pop("crf", None)
        for key in ("bitrate", "maxrate", "bufsize", "video_codec", "pix_fmt",
                    "profile", "level", "gop", "preset"):
            if key in rung:
                ff[key] = rung[key]
        audio = audio_spec(ch, rung)
        path = os.path.join(out_dir, f"{stem}_{width}x{height}.mp4")
        ffmpeg_utils.transcode_rendition(
            master, path, width=width, height=height, fps=fps, ff=ff,
            audio=audio, audio_track=audio_track)
        out.append({
            "path": path,
            "width": width,
            "height": height,
            "bitrate_kbps": _kbps(ff.get("bitrate")),
            "codec": ff.get("video_codec", "libx264"),
            "mime": "video/mp4",
            "bytes": os.path.getsize(path),
            "role": "media",
        })
        log.info("rendition %dx%d -> %s (%.1f MB)", width, height, path,
                 out[-1]["bytes"] / 1e6)

    if ch.get("mezzanine"):
        # VAST 4 wants the high-quality source alongside the delivery ladder;
        # SSAI stitchers re-encode from it. Missing it is error 406.
        out.append({
            "path": master,
            "width": 0,
            "height": 0,
            "bitrate_kbps": 0,
            "codec": ff_base.get("video_codec", "libx264"),
            "mime": "video/mp4",
            "bytes": os.path.getsize(master),
            "role": "mezzanine",
        })
    return out


AUDIO_MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
              "ogg": "audio/ogg", "wav": "audio/wav"}


def _audio_ladder(master: str, stem: str, ch: dict, ff_base: dict,
                  out_dir: str) -> list[dict]:
    """Radio / audio spots: no picture, so the ladder varies codec and bitrate.

    VAST still wants width and height on a MediaFile; 0x0 is the accepted way to
    say "this ad has no picture".
    """
    out = []
    for rung in ch["ladder"]:
        container = str(rung.get("container", "m4a")).lstrip(".")
        spec = dict(ch.get("audio_spec") or {})
        for key in ("codec", "bitrate", "sample_rate", "channels"):
            if f"audio_{key}" in rung:
                spec[key] = rung[f"audio_{key}"]
            elif key in rung:
                spec[key] = rung[key]
        path = os.path.join(out_dir, f"{stem}_{spec.get('bitrate', 'audio')}.{container}")
        ffmpeg_utils.transcode_audio(master, path,
                                     ff={"bin": ff_base.get("bin", "ffmpeg")}, audio=spec)
        out.append({
            "path": path, "width": 0, "height": 0,
            "bitrate_kbps": _kbps(spec.get("bitrate")),
            "codec": spec.get("codec", "aac"),
            "mime": AUDIO_MIME.get(container, "audio/mp4"),
            "bytes": os.path.getsize(path), "role": "media",
        })
        log.info("audio rendition %s -> %s (%.2f MB)", spec.get("bitrate"), path,
                 out[-1]["bytes"] / 1e6)
    return out


def package(master: str, plan: dict, cfg: dict, job: dict | None = None) -> dict:
    """Turn a rendered master into what a buyer traffics.

    Builds the channel's rendition ladder and, when VAST is configured, the
    InLine tag that references them. Returns `{}` when the config declares no
    `delivery` block, so a plain "render an MP4" setup keeps working unchanged.

    The returned dict is written straight back to the sheet, so its keys are
    sheet column names.
    """
    if not (cfg.get("delivery") or {}):
        return {}
    job = job or {}
    channel = plan.get("channel", "dooh")
    delivery = cfg["delivery"]
    out_dir = plan.get("renditions_dir") or os.path.join(
        os.path.dirname(master) or ".", "renditions")

    files = build(master, plan["stem"], cfg=cfg, channel=channel, fps=plan["fps"],
                  out_dir=out_dir, audio_track=plan.get("audio_track"))
    extra = {"renditions": ", ".join(f["path"] for f in files if f["role"] == "media")}

    if delivery.get("vast", True):
        from . import ffmpeg_utils as _ff
        from . import vast as _vast

        duration = plan.get("duration") or _ff.probe_duration(
            master, cfg.get("ffmpeg", {}).get("ffprobe_bin", "ffprobe"))
        xml = _vast.build_inline(
            ad_id=plan["stem"],
            # Unique per ad response: the input hash makes it stable for an
            # unchanged creative and new as soon as anything moves.
            ad_serving_id=f"{plan['stem']}-{plan['hash']}",
            title=plan.get("title") or plan["stem"],
            duration_s=duration,
            renditions=files,
            vast_cfg=cfg.get("vast") or {},
            channel=channel,
            base_url=delivery.get("base_url", ""),
        )
        vast_path = os.path.join(os.path.dirname(master) or ".", f"{plan['stem']}.vast.xml")
        _vast.write(vast_path, xml)
        extra["vast_path"] = vast_path
        log.info("VAST %s -> %s (%d media file(s))", channel, vast_path,
                 sum(1 for f in files if f["role"] == "media"))
    return extra


def _kbps(bitrate) -> int:
    """'8M' -> 8000, '600k' -> 600. VAST wants kbps as an integer."""
    if not bitrate:
        return 0
    s = str(bitrate).strip()
    try:
        if s[-1] in "Mm":
            return int(float(s[:-1]) * 1000)
        if s[-1] in "Kk":
            return int(float(s[:-1]))
        return int(int(s) / 1000)  # plain bits per second
    except (ValueError, IndexError):
        return 0
