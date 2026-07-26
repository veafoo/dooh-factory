#!/usr/bin/env python3
"""Generate the rushes the example rows refer to, so a fresh clone renders.

The example rows in the sheet model name files (`intro_FR.mp4`,
`product_young.mp4`, ...) that obviously are not in the repo: rushes are
gitignored and they would be someone's footage anyway. This synthesises
stand-ins with FFmpeg's lavfi sources, which makes the documented first run
actually produce a creative instead of three "clip not found" errors.

    python scripts/make_demo_assets.py
    python run.py --job mode1 --csv templates/sheet_model/mode1.csv

Colour bars with a label burned in, deliberately at mismatched resolutions, so
the normalisation pass has something real to do.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# name -> (colour, width, height, seconds)
CLIPS = {
    "intro_FR.mp4": ("#1b2b4a", 1280, 720, 3),
    "intro_BE.mp4": ("#2b1b4a", 1280, 720, 3),
    "product_young.mp4": ("#c8442e", 1920, 1080, 3),
    "product_family.mp4": ("#2e8b57", 1920, 1080, 3),
    "cta_30.mp4": ("#ffd23f", 640, 480, 2),
    "cta_50.mp4": ("#ff8c00", 640, 480, 2),
    "cta_30pct.mp4": ("#ffd23f", 640, 480, 2),
    "cta_50pct.mp4": ("#ff8c00", 640, 480, 2),
}
STILLS = {"endframe.png": ("#0b1020", 1080, 1350)}
# A radio bed, a voice-over and a tail, plus the CTV/mobile soundtrack.
AUDIO = {"bed.m4a": (220, 4), "vo_fr.m4a": (660, 3), "tail.m4a": (440, 2)}


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-3:]
        raise SystemExit(f"ffmpeg failed: {' | '.join(tail)}")


def _drawtext_font() -> str | None:
    """A usable font, but only if this ffmpeg was built with drawtext at all.

    Homebrew ships builds without libfreetype, so checking that a .ttf exists on
    disk proves nothing. Ask ffmpeg instead.
    """
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True).stdout
    if " drawtext " not in filters:
        return None
    for font in ("/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.isfile(font):
            return font
    return None


_FONT: str | None = None


def _label(text: str, size: int) -> str:
    """Burn the clip name in, when this ffmpeg can. It is only a demo."""
    if not _FONT:
        return ""
    safe = text.replace(":", r"\:").replace("'", "")
    return (f",drawtext=fontfile={_FONT}:text='{safe}':fontcolor=white:"
            f"fontsize={size}:x=(w-text_w)/2:y=(h-text_h)/2")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate demo rushes")
    ap.add_argument("--assets-dir", default="assets")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    global _FONT
    _FONT = _drawtext_font()
    if not _FONT:
        print("note: this ffmpeg has no drawtext filter, clips will be plain colour")

    os.makedirs(args.assets_dir, exist_ok=True)
    made = skipped = 0

    for name, (colour, w, h, secs) in CLIPS.items():
        dst = os.path.join(args.assets_dir, name)
        if os.path.exists(dst) and not args.force:
            skipped += 1
            continue
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
              f"color=c={colour}:s={w}x{h}:d={secs}:r=25",
              "-vf", f"format=yuv420p{_label(name.rsplit('.', 1)[0], h // 12)}",
              "-pix_fmt", "yuv420p", dst])
        made += 1

    for name, (colour, w, h) in STILLS.items():
        dst = os.path.join(args.assets_dir, name)
        if os.path.exists(dst) and not args.force:
            skipped += 1
            continue
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}:d=1",
              "-vf", f"format=yuv420p{_label(name.rsplit('.', 1)[0], h // 12)}",
              "-frames:v", "1", dst])
        made += 1

    for name, (freq, secs) in AUDIO.items():
        dst = os.path.join(args.assets_dir, name)
        if os.path.exists(dst) and not args.force:
            skipped += 1
            continue
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
              f"sine=frequency={freq}:duration={secs}", "-c:a", "aac", dst])
        made += 1

    print(f"{made} demo asset(s) written to {args.assets_dir}/"
          + (f", {skipped} already there (use --force to rebuild)" if skipped else ""))
    print("\nnow:  python run.py --job mode1 --csv templates/sheet_model/mode1.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
