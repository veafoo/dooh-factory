"""Mode 2 smoke test: real Chromium + ffmpeg, no Google Sheet.

Renders the drive-to-store reference template three ways - static repiquage
(dom injection), animated via real-time recording, animated via deterministic
frame seeking - and verifies each output. Also checks the injected address
actually made it into the pixels, which is the whole point of the mode.

    python tests/smoke_mode2.py
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import ffmpeg_utils, mode2_html5  # noqa: E402

TMP = os.path.join("output", "_smoke2")
OUT = os.path.join(TMP, "out")

CFG = {
    "paths": {"templates_dir": "templates", "assets_dir": "assets", "output_dir": OUT},
    "ffmpeg": {"bin": "ffmpeg", "video_codec": "libx264", "pix_fmt": "yuv420p",
               "fps": 25, "width": 1080, "height": 1920, "crf": 22, "preset": "veryfast",
               "default_duration_s": 10},
}

STORES = [
    {"variant_id": "lyon3", "store_name": "Magasin Lyon 3", "address": "12 rue Garibaldi",
     "city": "69003 Lyon", "phone": "04 72 00 00 00"},
    {"variant_id": "paris11", "store_name": "Magasin Paris 11", "address": "8 bd Voltaire",
     "city": "75011 Paris", "phone": "01 40 00 00 00"},
]


def rows_for(capture: str, **extra) -> list[dict]:
    rows = []
    for i, store in enumerate(STORES):
        rows.append({
            **store,
            "template": "example_store.html",
            "format": "1080x1920",
            "campaign": "Drive to Store",
            "claim": "à 5 minutes",
            "capture": capture,
            "naming_template": "{campaign}_{variant_id}_" + capture,
            "status": "pending", "output_path": "", "input_hash": "",
            "_row": i + 2, **extra,
        })
    return rows


def run_batch(rows, job, **opts):
    by_row = {r["_row"]: r for r in rows}

    def write_back(row, path, status, msg="", input_hash=None):
        r = by_row[row["_row"]]
        r["status"], r["output_path"] = status, path
        if input_hash:
            r["input_hash"] = input_hash
        if status == "error":
            print(f"   error on row {row['_row']}: {msg}")

    return mode2_html5.run(CFG, rows, write_back, job, **opts)


def ocr_free_check(path: str, expected_min_bytes: int = 5000) -> None:
    """No OCR here - assert the asset exists and is not a blank/degenerate file."""
    assert os.path.isfile(path), f"missing output: {path}"
    assert os.path.getsize(path) > expected_min_bytes, f"suspiciously small: {path}"


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    job = {"inject": "dom", "settle_ms": 200, "static_format": "jpg", "jpeg_quality": 90}

    # 1. static repiquage d'adresse - the core drive-to-store case
    rows = rows_for("static")
    s = run_batch(rows, job)
    assert s.rendered == 2 and s.errors == 0, f"static: {s}"
    for r in rows:
        ocr_free_check(r["output_path"])
        info = ffmpeg_utils.probe(r["output_path"])
        assert (info["width"], info["height"]) == (1080, 1920), f"bad size: {info}"
    print(f"OK -> static x2: {os.path.basename(rows[0]['output_path'])}")

    # two stores must not produce identical pixels (injection really happened)
    sizes = {os.path.getsize(r["output_path"]) for r in rows}
    assert len(sizes) == 2, "both variants rendered identically - injection did nothing"
    print("OK -> variants differ (params injected)")

    # 2. animated, real-time browser recording
    rows = rows_for("video", duration_s="3")
    s = run_batch(rows[:1], job)
    assert s.rendered == 1 and s.errors == 0, f"video: {s}"
    info = ffmpeg_utils.probe(rows[0]["output_path"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert 2.4 < info["duration"] < 3.6, f"expected ~3s, got {info['duration']:.2f}s"
    assert not info["has_audio"]
    print(f"OK -> video: {info['duration']:.1f}s @ {info['fps']:.0f}fps")

    # 3. animated, deterministic frame seeking (template exposes window.__seek)
    rows = rows_for("frames", duration_s="2", fps="25")
    s = run_batch(rows[:1], job)
    assert s.rendered == 1 and s.errors == 0, f"frames: {s}"
    info = ffmpeg_utils.probe(rows[0]["output_path"])
    assert 1.8 < info["duration"] < 2.2, f"expected 2s, got {info['duration']:.2f}s"
    print(f"OK -> frames (seek hook): {info['duration']:.1f}s")

    # 4. query injection reaches the creative too
    rows = rows_for("static")
    for r in rows:
        r["naming_template"] = "{variant_id}_query"
    s = run_batch(rows, {**job, "inject": "query"})
    assert s.rendered == 2, f"query injection: {s}"
    print("OK -> query injection")

    # 5. idempotency + a bad template reported per-row
    s = run_batch(rows, {**job, "inject": "query"})
    assert (s.rendered, s.skipped) == (0, 2), f"second run should skip: {s}"
    print("OK -> second run skipped (idempotent)")

    bad = rows_for("static")
    bad[0]["template"] = "nope.html"
    s = run_batch(bad, job, limit=1)
    assert s.errors == 1, f"missing template should error the row: {s}"
    print("OK -> missing template reported per-row")

    # 6. concurrency: each worker owns its own browser, created and closed in
    #    its own thread. Nothing lost, nothing duplicated, one bad row contained.
    many = []
    for i in range(6):
        r = dict(STORES[i % len(STORES)])
        r.update(variant_id=f"p{i}", template="example_store.html", format="1080x1920",
                 campaign="Parallel", capture="static", naming_template=f"par_{i}",
                 status="pending", output_path="", input_hash="", _row=i + 20)
        many.append(r)
    many[3]["template"] = "nope.html"
    s = run_batch(many, job, workers=3)
    assert (s.rendered, s.errors) == (5, 1), f"parallel run: {s}"
    rendered = [r for r in many if r["status"] == "done"]
    assert len({r["output_path"] for r in rendered}) == 5
    for r in rendered:
        assert os.path.getsize(r["output_path"]) > 5000, f"corrupt output: {r['output_path']}"
    print("OK -> 5 variants on 3 browsers, bad row contained")

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
