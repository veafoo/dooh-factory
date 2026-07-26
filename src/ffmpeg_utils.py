"""FFmpeg helpers. Every function builds an explicit command list and runs it
via subprocess, logging the exact command. No fragile wrapper lib.

DOOH output = MP4 (H.264) or a static image, per-creative resolution, muted.
Encoding parameters (CRF or CBR, profile, level, GOP, faststart) come from the
`ffmpeg` block of the config so a CMS with strict ingest specs can be satisfied
without touching code.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

log = logging.getLogger("dooh.ffmpeg")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


class FFmpegError(RuntimeError):
    """FFmpeg exited non-zero. Carries the tail of stderr - the useful part."""


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def run(cmd: list[str]) -> str:
    """Run a command, log it, raise FFmpegError with stderr context on failure.

    subprocess' own CalledProcessError message is just an exit code, which then
    lands in the sheet's error column and tells the operator nothing.
    """
    log.info("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = [ln for ln in (p.stderr or "").strip().splitlines() if ln.strip()][-6:]
        raise FFmpegError(f"{os.path.basename(cmd[0])} exit {p.returncode}: {' | '.join(tail)}")
    return p.stdout


def probe_duration(path: str, ffprobe_bin: str = "ffprobe") -> float:
    """Container duration, whatever the streams are.

    `probe` insists on a video stream because everything downstream of it needs
    dimensions; a radio spot has none, and only ever needs the duration.
    """
    out = run([ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path]).strip()
    try:
        return float(out)
    except ValueError:
        raise FFmpegError(f"could not read duration of {path}") from None


def probe(path: str, ffprobe_bin: str = "ffprobe") -> dict:
    """ffprobe a clip -> {width, height, fps, pix_fmt, duration, has_audio}."""
    cmd = [
        ffprobe_bin, "-v", "error",
        "-show_entries", "stream=index,codec_type,width,height,r_frame_rate,pix_fmt",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    data = json.loads(run(cmd))
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise FFmpegError(f"no video stream in {path}")
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": fps,
        "pix_fmt": v.get("pix_fmt"),
        "duration": float(data.get("format", {}).get("duration", 0.0) or 0.0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


# --------------------------------------------------------------------------- #
# encoding parameters
# --------------------------------------------------------------------------- #

def video_args(ff: dict | None = None) -> list[str]:
    """Output encoding flags from the `ffmpeg` config block.

    Rate control: `bitrate` set -> CBR-ish (what most DOOH CMS ingest specs
    ask for); otherwise CRF. x264/x265-only flags are skipped for other codecs.
    """
    ff = ff or {}
    codec = ff.get("video_codec", "libx264")
    args = ["-c:v", codec, "-pix_fmt", ff.get("pix_fmt", "yuv420p")]
    x26x = codec.startswith("libx26")

    bitrate = ff.get("bitrate")
    if bitrate:
        maxrate = ff.get("maxrate", bitrate)
        bufsize = ff.get("bufsize") or _double(maxrate)
        args += ["-b:v", str(bitrate), "-maxrate", str(maxrate), "-bufsize", str(bufsize)]
    elif x26x:
        args += ["-crf", str(ff.get("crf", 20))]

    if x26x:
        args += ["-preset", str(ff.get("preset", "medium"))]
        if ff.get("profile"):
            args += ["-profile:v", str(ff["profile"])]
        if ff.get("level"):
            args += ["-level:v", str(ff["level"])]

    gop = ff.get("gop")
    if gop:
        args += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]

    if ff.get("faststart", True):
        args += ["-movflags", "+faststart"]
    return args


def _double(rate) -> str:
    """'6M' -> '12M'. Default bufsize is 2x maxrate."""
    s = str(rate).strip()
    for suffix in ("M", "k", "K"):
        if s.endswith(suffix):
            try:
                return f"{float(s[:-1]) * 2:g}{suffix}"
            except ValueError:
                return s
    try:
        return str(int(float(s) * 2))
    except ValueError:
        return s


def audio_args(spec: dict | bool | None) -> list[str]:
    """Output audio flags.

    DOOH screens are muted, so `-an` stays the default. CTV and mobile ads carry
    sound, and a silent CTV rendition gets rejected or simply sounds broken, so
    a channel that wants audio gets AAC stereo at 48 kHz - the baseline every
    player accepts.
    """
    if not spec:
        return ["-an"]
    spec = {} if spec is True else spec
    return [
        "-c:a", spec.get("codec", "aac"),
        "-b:a", str(spec.get("bitrate", "128k")),
        "-ar", str(spec.get("sample_rate", 48000)),
        "-ac", str(spec.get("channels", 2)),
    ]


def _fit(w: int, h: int, fps: int) -> str:
    """Filter chain fitting any source into WxH: letterbox, square pixels, CFR."""
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    )


def _norm_chain(idx: int, w: int, h: int, fps: int) -> str:
    """Per-input labelled version of `_fit`, for filter_complex."""
    return f"[{idx}:v]{_fit(w, h, fps)},setpts=PTS-STARTPTS[v{idx}]"


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def effective_durations(
    clips: list[str], durations: list[float | None], still_duration: float,
    ffprobe_bin: str = "ffprobe",
) -> list[float]:
    """Actual on-timeline length of each clip, after trimming and image looping.

    Costs one ffprobe per video, so it is only called when something needs real
    durations - laying out transitions.
    """
    out = []
    for clip, dur in zip(clips, durations):
        if is_image(clip):
            out.append(float(dur or still_duration))
        else:
            # probe_duration, not probe: an audio rush has no video stream, and
            # the length is all this needs either way.
            actual = probe_duration(clip, ffprobe_bin)
            out.append(min(float(dur), actual) if dur else actual)
    return out


def _xfade_chain(n: int, clip_durations: list[float], transition: str, d: float) -> tuple[str, str]:
    """Cross-fade chain over the normalized inputs -> (filter, final label).

    Each xfade starts `d` seconds before the running output ends, so the total
    is sum(durations) - (n-1) * d.
    """
    if len(clip_durations) != n:
        raise ValueError("clip_durations must be parallel to clips")
    shortest = min(clip_durations)
    if d >= shortest:
        raise ValueError(
            f"transition duration {d}s must be shorter than the shortest clip ({shortest:.2f}s)")

    parts, prev, acc = [], "v0", clip_durations[0]
    for k in range(1, n):
        label = f"x{k}"
        parts.append(f"[{prev}][v{k}]xfade=transition={transition}:"
                     f"duration={d}:offset={acc - d:.4f}[{label}]")
        acc += clip_durations[k] - d
        prev = label
    return ";".join(parts), prev


def concat_normalized(
    clips: list[str],
    dst: str,
    *,
    width: int,
    height: int,
    fps: int = 25,
    ff: dict | None = None,
    durations: list[float | None] | None = None,
    still_duration: float = 4.0,
    max_duration: float | None = None,
    transition: str | None = None,
    transition_duration: float = 0.5,
    clip_durations: list[float] | None = None,
    audio: dict | bool | None = None,
) -> None:
    """Stitch heterogeneous clips into one creative in a single pass.

    Each input is scaled/padded to WxH, SAR normalized and fps-locked, then
    joined. Still images are looped for `durations[i]` (or `still_duration`)
    seconds. Videos are trimmed at the input when a duration is given.

    Audio is dropped by default - DOOH screens are muted. With `audio` set (CTV,
    mobile) the clips' audio is concatenated too; clips that carry no audio
    track, and still images, are filled with silence of the right length, since
    ffmpeg's concat filter refuses a stream count that varies between segments.

    With `transition` set (any ffmpeg xfade name: fade, wipeleft, dissolve...)
    clips cross-fade instead of hard-cutting; `clip_durations` is then required
    to lay out the offsets - see `effective_durations`. Audio is cross-faded to
    match, so picture and sound stay in step.
    """
    if not clips:
        raise ValueError("no clips to concat")
    ff = ff or {}
    durations = list(durations or [None] * len(clips))
    if len(durations) != len(clips):
        raise ValueError("durations must be parallel to clips")
    if audio and not clip_durations:
        # Silence padding needs to know how long each segment lasts.
        clip_durations = effective_durations(clips, durations, still_duration,
                                             ff.get("ffprobe_bin", "ffprobe"))

    cmd: list[str] = [ff.get("bin", "ffmpeg"), "-y"]
    for clip, dur in zip(clips, durations):
        if is_image(clip):
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(dur or still_duration)]
        elif dur:
            cmd += ["-t", str(dur)]
        cmd += ["-i", clip]

    chains = [_norm_chain(i, width, height, fps) for i in range(len(clips))]
    a_labels: list[str] = []
    if audio:
        rate = (audio if isinstance(audio, dict) else {}).get("sample_rate", 48000)
        silence_idx = len(clips)
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=stereo"]
        for i, clip in enumerate(clips):
            has_audio = (not is_image(clip)
                         and probe(clip, ff.get("ffprobe_bin", "ffprobe"))["has_audio"])
            if has_audio:
                chains.append(f"[{i}:a]aresample={rate},aformat=channel_layouts=stereo,"
                              f"asetpts=PTS-STARTPTS[a{i}]")
            else:
                chains.append(f"[{silence_idx}:a]atrim=duration={clip_durations[i]},"
                              f"aresample={rate},asetpts=PTS-STARTPTS[a{i}]")
            a_labels.append(f"[a{i}]")

    if transition and len(clips) > 1:
        if not clip_durations:
            raise ValueError("transitions need clip_durations")
        xfade, out_label = _xfade_chain(len(clips), clip_durations,
                                        transition, transition_duration)
        chains.append(xfade)
        maps = ["-map", f"[{out_label}]"]
        if audio:
            across, a_out = _acrossfade_chain(len(clips), transition_duration)
            chains.append(across)
            maps += ["-map", f"[{a_out}]"]
        filt = ";".join(chains)
    else:
        labels = "".join(f"[v{i}]" for i in range(len(clips)))
        n = len(clips)
        if audio:
            interleaved = "".join(f"[v{i}][a{i}]" for i in range(n))
            chains.append(f"{interleaved}concat=n={n}:v=1:a=1[vout][aout]")
            maps = ["-map", "[vout]", "-map", "[aout]"]
        else:
            chains.append(f"{labels}concat=n={n}:v=1:a=0[vout]")
            maps = ["-map", "[vout]"]
        filt = ";".join(chains)

    cmd += ["-filter_complex", filt] + maps
    cmd += video_args(ff) + audio_args(audio)
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd += [dst]
    run(cmd)


def _acrossfade_chain(n: int, d: float) -> tuple[str, str]:
    """Audio counterpart of the xfade chain, so sound follows the picture."""
    parts, prev = [], "a0"
    for k in range(1, n):
        label = f"ax{k}"
        parts.append(f"[{prev}][a{k}]acrossfade=d={d}:c1=tri:c2=tri[{label}]")
        prev = label
    return ";".join(parts), prev


def encode(
    src: str,
    dst: str,
    *,
    width: int | None = None,
    height: int | None = None,
    fps: int = 25,
    ff: dict | None = None,
    duration: float | None = None,
) -> None:
    """Re-encode a single asset (video or still) to DOOH spec, muted."""
    ff = ff or {}
    cmd: list[str] = [ff.get("bin", "ffmpeg"), "-y"]
    if is_image(src):
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(duration or 10)]
    elif duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", src]
    chain = _fit(width, height, fps) if width and height else f"fps={fps},format=yuv420p"
    cmd += ["-vf", chain] + video_args(ff) + ["-an", dst]
    run(cmd)


def frames_to_video(
    frames_glob: str, dst: str, fps: int, ff: dict | None = None,
    max_duration: float | None = None,
) -> None:
    """Assemble captured frames (Mode 2 `frames` capture) into an MP4."""
    ff = ff or {}
    cmd = [
        ff.get("bin", "ffmpeg"), "-y", "-framerate", str(fps),
        "-pattern_type", "glob", "-i", frames_glob,
        # browser viewports can be odd-sized; H.264 needs even dimensions
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
    ]
    cmd += video_args(ff) + ["-an"]
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd += [dst]
    run(cmd)


def transcode_capture(
    src: str, dst: str, *, width: int, height: int, fps: int,
    ff: dict | None = None, start: float = 0.0, duration: float | None = None,
) -> None:
    """Normalize a browser-recorded video (webm, variable fps) to DOOH spec."""
    ff = ff or {}
    cmd = [ff.get("bin", "ffmpeg"), "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vf", _fit(width, height, fps)] + video_args(ff) + ["-an", dst]
    run(cmd)


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}


def is_audio(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def concat_audio(
    clips: list[str], dst: str, *, ff: dict | None = None,
    durations: list[float | None] | None = None,
    audio: dict | bool | None = True,
    max_duration: float | None = None,
    crossfade: float = 0.0,
) -> None:
    """Stitch audio rushes into one radio / audio spot.

    Same spine as the video path, minus the picture: a radio ad is a sequence of
    beds and voice-overs, and DCO on radio means swapping the middle segment per
    market or per store. Inputs may be audio files or videos carrying audio -
    only the audio stream is taken either way.
    """
    if not clips:
        raise ValueError("no clips to concat")
    ff = ff or {}
    durations = list(durations or [None] * len(clips))
    if len(durations) != len(clips):
        raise ValueError("durations must be parallel to clips")
    rate = (audio if isinstance(audio, dict) else {}).get("sample_rate", 48000)

    cmd: list[str] = [ff.get("bin", "ffmpeg"), "-y"]
    for clip, dur in zip(clips, durations):
        if dur:
            cmd += ["-t", str(dur)]
        cmd += ["-i", clip]

    chains = [f"[{i}:a]aresample={rate},aformat=channel_layouts=stereo,"
              f"asetpts=PTS-STARTPTS[a{i}]" for i in range(len(clips))]
    if crossfade and len(clips) > 1:
        chain, out = _acrossfade_chain(len(clips), crossfade)
        chains.append(chain)
    else:
        labels = "".join(f"[a{i}]" for i in range(len(clips)))
        chains.append(f"{labels}concat=n={len(clips)}:v=0:a=1[aout]")
        out = "aout"

    cmd += ["-filter_complex", ";".join(chains), "-map", f"[{out}]", "-vn"]
    cmd += audio_args(audio or True)
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd += [dst]
    run(cmd)


def transcode_audio(src: str, dst: str, *, ff: dict | None = None,
                    audio: dict | bool | None = True) -> None:
    """Derive one audio delivery rendition from the master."""
    ff = ff or {}
    run([ff.get("bin", "ffmpeg"), "-y", "-i", src, "-vn"] + audio_args(audio or True) + [dst])


def transcode_rendition(
    src: str, dst: str, *, width: int, height: int, fps: int,
    ff: dict | None = None, audio: dict | bool | None = None,
    audio_track: str | None = None,
) -> None:
    """Derive one delivery rendition from the master.

    The master is encoded once at the highest quality of the ladder; every
    rendition is a transcode of it. `audio_track` muxes an external soundtrack -
    the browser capture path produces no audio at all, so a CTV variant built
    from an HTML5 template gets its sound this way.
    """
    ff = ff or {}
    cmd = [ff.get("bin", "ffmpeg"), "-y", "-i", src]
    if audio_track:
        cmd += ["-i", audio_track]
    cmd += ["-vf", _fit(width, height, fps)]
    if audio and audio_track:
        # Take video from the master, audio from the track, stop at the shorter.
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    elif audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += video_args(ff) + audio_args(audio) + [dst]
    run(cmd)


def convert_image(src: str, dst: str, *, width: int | None = None,
                  height: int | None = None, quality: int = 90,
                  ff: dict | None = None) -> None:
    """Convert/resize a captured screenshot to the delivered static format."""
    ff = ff or {}
    cmd = [ff.get("bin", "ffmpeg"), "-y", "-i", src]
    if width and height:
        cmd += ["-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"]
    if dst.lower().endswith((".jpg", ".jpeg")):
        # ffmpeg mjpeg -q:v is 2 (best) .. 31 (worst)
        cmd += ["-q:v", str(max(2, min(31, round(31 - (quality / 100) * 29))))]
    cmd += [dst]
    run(cmd)
