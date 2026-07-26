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

    # transitions: each xfade overlaps two clips, so the total shrinks by
    # (n-1) * transition_dur against the same hard-cut sequence
    cut = build_rows()
    cut[0].update(naming_template="cut", status="pending", input_hash="")
    run_batch(cut)
    cut_len = ffmpeg_utils.probe(cut[0]["output_path"])["duration"]

    xf = build_rows()
    xf[0].update(naming_template="xfade", transition="fade", transition_dur="0.5",
                 status="pending", input_hash="")
    run_batch(xf)
    xf_len = ffmpeg_utils.probe(xf[0]["output_path"])["duration"]
    expected = cut_len - 3 * 0.5  # 4 clips -> 3 transitions
    assert abs(xf_len - expected) < 0.15, f"xfade: got {xf_len:.2f}s, expected {expected:.2f}s"
    print(f"OK -> xfade: {cut_len:.1f}s hard cut -> {xf_len:.1f}s crossfaded")

    # a transition longer than the shortest clip would produce garbage
    over = build_rows()
    over[0].update(naming_template="over", transition="fade", transition_dur="9",
                   status="pending", input_hash="")
    s5 = run_batch(over)
    assert s5.errors == 1, "an over-long transition must be refused, not rendered"
    print("OK -> over-long transition refused")

    # per-format ingest profile switches rate control for that format only
    CFG["ffmpeg"]["profiles"] = {"1920x1080": {"bitrate": "10M", "gop": 50}}
    try:
        land = build_rows()
        land[0].update(format="1920x1080", naming_template="landscape",
                       status="pending", input_hash="")
        plan = mode1_stitch.prepare(land[0], CFG, {})
        assert plan["encoding"]["bitrate"] == "10M" and plan["encoding"]["gop"] == 50
        assert run_batch(land).rendered == 1
        info = ffmpeg_utils.probe(land[0]["output_path"])
        assert (info["width"], info["height"]) == (1920, 1080)
        # portrait creatives must be untouched by the landscape profile
        assert "bitrate" not in mode1_stitch.prepare(build_rows()[0], CFG, {})["encoding"]
        print("OK -> per-format profile applied (CBR 10M) and scoped to its format")
    finally:
        CFG["ffmpeg"].pop("profiles")

    # concurrency must not lose, duplicate or corrupt rows
    many = []
    for i in range(6):
        r = build_rows()[0]
        r.update(creative_id=f"p{i}", naming_template=f"par_{i}", _row=i + 10,
                 status="pending", input_hash="", output_path="")
        many.append(r)
    s6 = run_batch(many, workers=3)
    assert (s6.rendered, s6.errors) == (6, 0), f"parallel run: {s6}"
    assert len({r["output_path"] for r in many}) == 6
    for r in many:
        assert ffmpeg_utils.probe(r["output_path"])["duration"] > 0
    print("OK -> 6 rows rendered on 3 workers, all outputs valid")

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
