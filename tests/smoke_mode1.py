"""Mode 1 smoke test: real ffmpeg, no Google Sheet.

Generates heterogeneous rushes (three resolutions + a still endframe), stitches
them through Mode 1, and checks the output against the DOOH spec. Also asserts
the idempotency contract: a second run must skip, and must re-render once a
rush changes on disk.

    python tests/smoke_mode1.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import ffmpeg_utils, mode1_stitch  # noqa: E402

TMP = os.path.join("output", "_smoke1")
ASSETS = os.path.join(TMP, "assets")
OUT = os.path.join(TMP, "out")

CFG = {
    "paths": {"assets_dir": ASSETS, "output_dir": OUT},
    "ffmpeg": {"bin": "ffmpeg", "video_codec": "libx264", "pix_fmt": "yuv420p",
               "fps": 25, "width": 1080, "height": 1920, "crf": 22,
               "preset": "veryfast", "gop": 25, "profile": "high", "level": "4.1"},
}


def gen_video(name: str, color: str, w: int, h: int, secs: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={secs}:r=25",
         "-pix_fmt", "yuv420p", os.path.join(ASSETS, name)],
        check=True, capture_output=True, text=True)


def gen_image(name: str, color: str, w: int, h: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d=1",
         "-frames:v", "1", os.path.join(ASSETS, name)],
        check=True, capture_output=True, text=True)


def build_rows() -> list[dict]:
    return [{
        "creative_id": "001",
        "clip_1": "intro.mp4", "clip_2": "product.mp4", "clip_3": "cta.mp4",
        "clip_4": "endframe.png", "clip_4_dur": "2",
        "format": "1080x1920", "campaign": "Soldes Été", "market": "FR", "version": "a",
        "naming_template": "{campaign}_{market}_{version}",
        "status": "pending", "input_hash": "", "output_path": "", "_row": 2,
    }]


def run_batch(rows, **opts):
    """Run Mode 1 and mirror the write-back into the rows, like the sheet does."""
    by_row = {r["_row"]: r for r in rows}

    def write_back(row, path, status, msg="", input_hash=None):
        r = by_row[row["_row"]]
        r["status"] = status
        r["output_path"] = path
        if input_hash:
            r["input_hash"] = input_hash
        if status == "error":
            print(f"   error: {msg}")

    return mode1_stitch.run(CFG, rows, write_back, {}, **opts)


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ASSETS, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    # deliberately mismatched sources: different resolutions, and a still
    gen_video("intro.mp4", "red", 640, 480, 2)
    gen_video("product.mp4", "green", 1280, 720, 2)
    gen_video("cta.mp4", "blue", 1920, 1080, 2)
    gen_image("endframe.png", "yellow", 800, 800)

    rows = build_rows()
    s = run_batch(rows)
    assert s.rendered == 1 and s.errors == 0, f"first run: {s}"

    path = rows[0]["output_path"]
    assert os.path.isfile(path), f"missing output: {path}"
    assert os.path.basename(path) == "Soldes-Ete_FR_a.mp4", f"bad name: {path}"

    info = ffmpeg_utils.probe(path)
    print(f"OK -> {path}")
    print(f"   {info['width']}x{info['height']} @ {info['fps']:.0f}fps, "
          f"{info['duration']:.1f}s, audio={info['has_audio']}")
    assert (info["width"], info["height"]) == (1080, 1920), "output not portrait 1080x1920"
    assert 7.5 < info["duration"] < 8.5, "expected ~8s (3x2s clips + 2s still)"
    assert not info["has_audio"], "DOOH output must be muted"

    # idempotency: nothing changed -> nothing re-rendered
    s2 = run_batch(rows)
    assert (s2.rendered, s2.skipped) == (0, 1), f"second run should skip: {s2}"
    print("OK -> second run skipped (idempotent)")

    # a rush changes on disk -> the row goes stale and is rebuilt
    gen_video("product.mp4", "magenta", 1280, 720, 3)
    s3 = run_batch(rows)
    assert s3.rendered == 1, f"changed rush should re-render: {s3}"
    assert 8.5 < ffmpeg_utils.probe(rows[0]["output_path"])["duration"] < 9.5
    print("OK -> changed rush re-rendered (input hash)")

    # --force ignores a clean state
    assert run_batch(rows, force=True).rendered == 1
    print("OK -> --force re-renders")

    # a missing rush is reported on its row, not raised at the batch
    bad = build_rows()
    bad[0]["clip_2"] = "does-not-exist.mp4"
    s4 = run_batch(bad)
    assert (s4.errors, s4.rendered) == (1, 0), f"missing asset should error the row: {s4}"
    print("OK -> missing rush reported per-row")

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
