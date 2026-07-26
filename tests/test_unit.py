"""Offline unit tests: no ffmpeg, no browser, no Google credentials.

Covers the parts where a silent wrong answer is expensive - the idempotency
decision, the naming contract, cadence safety, and sheet parsing.

    pytest tests -q
"""
from __future__ import annotations

import os
import sys
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from src import (contract, ffmpeg_utils, mode1_stitch, mode2_html5, naming,
                 renditions, runner, scheduler, sheet_model, sheets, vast)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import expand_dco  # noqa: E402


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #

def test_render_name_interpolates_and_transliterates_accents():
    row = {"campaign": "Soldes Été", "market": "FR", "version": "a"}
    assert naming.render_name("{campaign}_{market}_{version}", row) == "Soldes-Ete_FR_a"


@pytest.mark.parametrize("raw,expected", [
    ("Café & Cie", "Cafe-et-Cie"),
    ("Magasin Lyon 3e", "Magasin-Lyon-3e"),
    ("L'Épicerie", "L-Epicerie"),
    ("-40% Nöel--", "40pct-Noel"),
    ("///", "creative"),
])
def test_sanitize_stays_ascii_readable_and_never_empty(raw, expected):
    assert naming.sanitize(raw) == expected


def test_render_name_missing_key_is_empty_not_crash():
    assert naming.render_name("{a}_{missing}", {"a": "x"}) == "x"


def test_render_name_never_returns_empty():
    assert naming.render_name("{missing}", {}) == "creative"


def test_output_name_wins_over_template_and_drops_extension():
    row = {"output_name": "explicit_v2.mp4", "naming_template": "{creative_id}", "creative_id": "1"}
    assert contract.output_stem(row) == "explicit_v2"


def test_naming_falls_back_when_both_columns_empty():
    assert contract.output_stem({"creative_id": "007"}) == "007"


# --------------------------------------------------------------------------- #
# contract: format, idempotency
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("1080x1920", (1080, 1920)),
    (" 1920 X 1080 ", (1920, 1080)),
    ("", (1080, 1920)),
    ("portrait", (1080, 1920)),
    (None, (1080, 1920)),
])
def test_parse_format(raw, expected):
    assert contract.parse_format(raw, (1080, 1920)) == expected


def test_settled_row_is_skipped_without_hash_column():
    assert contract.is_settled({"status": "done"})
    assert not contract.is_settled({"status": "done", "input_hash": "abc"})
    assert not contract.is_settled({"status": "pending"})


def test_needs_render_matrix():
    assert contract.needs_render({"status": "pending"}, "h1") == (True, "pending")
    assert contract.needs_render({"status": "error"}, "h1")[0] is True
    assert contract.needs_render({"status": "done"}, "h1") == (False, "done")
    assert contract.needs_render({"status": "done", "input_hash": "h1"}, "h1")[0] is False
    assert contract.needs_render({"status": "done", "input_hash": "h0"}, "h1")[0] is True
    assert contract.needs_render({"status": "done", "input_hash": ""}, "h1")[0] is True


def test_hash_ignores_write_back_columns():
    a = {"creative_id": "1", "clip_1": "a.mp4", "status": "done", "output_path": "x.mp4"}
    b = {"creative_id": "1", "clip_1": "a.mp4", "status": "pending", "output_path": ""}
    assert contract.compute_input_hash(a) == contract.compute_input_hash(b)


def test_hash_changes_with_input_cell_and_render_spec():
    row = {"creative_id": "1", "clip_1": "a.mp4"}
    base = contract.compute_input_hash(row, [], "1080x1920")
    assert base != contract.compute_input_hash({**row, "clip_1": "b.mp4"}, [], "1080x1920")
    assert base != contract.compute_input_hash(row, [], "1920x1080")


def test_hash_changes_when_a_rush_is_replaced_on_disk(tmp_path):
    f = tmp_path / "rush.mp4"
    f.write_bytes(b"a" * 100)
    before = contract.compute_input_hash({"clip_1": "rush.mp4"}, [str(f)])
    f.write_bytes(b"a" * 200)
    assert contract.compute_input_hash({"clip_1": "rush.mp4"}, [str(f)]) != before


def test_hash_survives_a_missing_file():
    contract.compute_input_hash({"clip_1": "x"}, ["/nope/missing.mp4"])  # must not raise


# --------------------------------------------------------------------------- #
# mode 1: clip collection
# --------------------------------------------------------------------------- #

def _touch(d, name):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"\0")
    return p


def test_collect_clips_orders_numerically_and_skips_gaps(tmp_path):
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        _touch(tmp_path, n)
    row = {"clip_1": "a.mp4", "clip_2": "", "clip_3": "b.mp4", "clip_10": "c.mp4"}
    clips, durs = mode1_stitch.collect_clips(row, str(tmp_path))
    assert [os.path.basename(c) for c in clips] == ["a.mp4", "b.mp4", "c.mp4"]
    assert durs == [None, None, None]


def test_collect_clips_reads_per_clip_durations(tmp_path):
    _touch(tmp_path, "a.mp4")
    _touch(tmp_path, "end.png")
    row = {"clip_1": "a.mp4", "clip_1_dur": "2,5", "clip_2": "end.png", "clip_2_dur": "3"}
    clips, durs = mode1_stitch.collect_clips(row, str(tmp_path))
    assert durs == [2.5, 3.0]
    assert ffmpeg_utils.is_image(clips[1])


def test_collect_clips_fails_loud_on_missing_asset(tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        mode1_stitch.collect_clips({"clip_1": "nope.mp4"}, str(tmp_path))
    assert "nope.mp4" in str(e.value)


def test_collect_clips_rejects_an_empty_row(tmp_path):
    with pytest.raises(ValueError):
        mode1_stitch.collect_clips({"creative_id": "1"}, str(tmp_path))


def test_prepare_uses_row_format_over_config(tmp_path):
    _touch(tmp_path, "a.mp4")
    cfg = {"paths": {"assets_dir": str(tmp_path), "output_dir": str(tmp_path / "out")},
           "ffmpeg": {"width": 1080, "height": 1920, "fps": 25}}
    plan = mode1_stitch.prepare({"creative_id": "1", "clip_1": "a.mp4", "format": "1920x1080"}, cfg)
    assert (plan["width"], plan["height"]) == (1920, 1080)
    assert plan["dst"].endswith("1.mp4")


# --------------------------------------------------------------------------- #
# ffmpeg argument construction (no ffmpeg run)
# --------------------------------------------------------------------------- #

def test_video_args_default_to_crf():
    args = ffmpeg_utils.video_args({})
    assert "-crf" in args and "-b:v" not in args
    assert args[args.index("-movflags") + 1] == "+faststart"


def test_video_args_switch_to_cbr_when_bitrate_is_set():
    args = ffmpeg_utils.video_args({"bitrate": "8M"})
    assert "-crf" not in args
    assert args[args.index("-b:v") + 1] == "8M"
    assert args[args.index("-bufsize") + 1] == "16M"


def test_video_args_skip_x264_only_flags_for_other_codecs():
    args = ffmpeg_utils.video_args({"video_codec": "mpeg2video", "crf": 18, "profile": "high"})
    assert "-crf" not in args and "-profile:v" not in args and "-preset" not in args


def test_video_args_emit_gop_lock():
    args = ffmpeg_utils.video_args({"gop": 25})
    assert args[args.index("-g") + 1] == "25"
    assert args[args.index("-keyint_min") + 1] == "25"


def test_concat_rejects_mismatched_durations():
    with pytest.raises(ValueError):
        ffmpeg_utils.concat_normalized(["a.mp4"], "o.mp4", width=10, height=10, durations=[1, 2])


# --------------------------------------------------------------------------- #
# transitions (P4)
# --------------------------------------------------------------------------- #

def test_xfade_offsets_accumulate_correctly():
    filt, out = ffmpeg_utils._xfade_chain(3, [4.0, 3.0, 2.0], "fade", 0.5)
    parts = filt.split(";")
    # first xfade starts 0.5s before clip 0 ends
    assert "offset=3.5000" in parts[0]
    # running length after the first join is 4 + 3 - 0.5 = 6.5
    assert "offset=6.0000" in parts[1]
    assert out == "x2"


def test_xfade_total_duration_is_sum_minus_overlaps():
    durations, d = [4.0, 3.0, 2.0], 0.5
    filt, _ = ffmpeg_utils._xfade_chain(3, durations, "fade", d)
    last_offset = float(filt.split("offset=")[-1].split("[")[0])
    assert round(last_offset + durations[-1], 3) == round(sum(durations) - 2 * d, 3)


def test_transition_longer_than_the_shortest_clip_is_refused():
    with pytest.raises(ValueError) as e:
        ffmpeg_utils._xfade_chain(2, [4.0, 0.3], "fade", 0.5)
    assert "shorter than the shortest clip" in str(e.value)


def test_transition_without_clip_durations_is_refused():
    with pytest.raises(ValueError) as e:
        ffmpeg_utils.concat_normalized(["a.mp4", "b.mp4"], "o.mp4", width=10, height=10,
                                       transition="fade")
    assert "clip_durations" in str(e.value)


def test_effective_durations_uses_still_hold_for_images(tmp_path):
    img = tmp_path / "end.png"
    img.write_bytes(b"\0")
    assert ffmpeg_utils.effective_durations([str(img)], [None], 3.0) == [3.0]
    assert ffmpeg_utils.effective_durations([str(img)], [1.5], 3.0) == [1.5]


# --------------------------------------------------------------------------- #
# per-format encoding profiles (P4)
# --------------------------------------------------------------------------- #

def test_encoding_profile_overrides_only_what_it_states():
    ff = {"crf": 20, "gop": 25, "video_codec": "libx264",
          "profiles": {"3840x2160": {"bitrate": "20M", "gop": 50}}}
    enc = contract.encoding_for(ff, 3840, 2160)
    assert enc["bitrate"] == "20M" and enc["gop"] == 50
    assert enc["video_codec"] == "libx264"      # inherited
    assert "profiles" not in enc                # not leaked into the encoder config
    assert ff["gop"] == 25                      # base config untouched


def test_unmatched_format_keeps_the_base_encoding():
    ff = {"crf": 20, "profiles": {"3840x2160": {"bitrate": "20M"}}}
    assert contract.encoding_for(ff, 1080, 1920) is ff


def test_profile_switches_rate_control_end_to_end():
    ff = {"crf": 20, "profiles": {"1920x1080": {"bitrate": "10M"}}}
    args = ffmpeg_utils.video_args(contract.encoding_for(ff, 1920, 1080))
    assert "-crf" not in args and args[args.index("-b:v") + 1] == "10M"


# --------------------------------------------------------------------------- #
# transient-failure retries (P4)
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _ApiError(Exception):
    def __init__(self, status):
        super().__init__(f"api error {status}")
        self.response = _Resp(status)


def test_retry_recovers_from_a_transient_status(monkeypatch):
    monkeypatch.setattr(sheets.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _ApiError(429)
        return "ok"

    assert sheets.with_retry(flaky, what="test") == "ok"
    assert calls["n"] == 3


def test_retry_does_not_mask_a_permission_error(monkeypatch):
    monkeypatch.setattr(sheets.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise _ApiError(403)

    with pytest.raises(_ApiError):
        sheets.with_retry(forbidden, what="test")
    assert calls["n"] == 1, "a 403 is the operator's problem, not a retry candidate"


def test_retry_gives_up_and_reraises(monkeypatch):
    monkeypatch.setattr(sheets.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_503():
        calls["n"] += 1
        raise _ApiError(503)

    with pytest.raises(_ApiError):
        sheets.with_retry(always_503, what="test", attempts=4)
    assert calls["n"] == 4


def test_retry_covers_connection_errors_without_a_response(monkeypatch):
    monkeypatch.setattr(sheets.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("reset by peer")
        return "ok"

    assert sheets.with_retry(flaky, what="test") == "ok"


# --------------------------------------------------------------------------- #
# mode 2: prepare / injection (no browser)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def m2cfg(tmp_path):
    tpl = tmp_path / "t.html"
    tpl.write_text("<html></html>")
    return {"paths": {"templates_dir": str(tmp_path), "output_dir": str(tmp_path / "out"),
                      "assets_dir": str(tmp_path)},
            "ffmpeg": {"width": 1080, "height": 1920, "fps": 25, "default_duration_s": 10}}


def test_extract_params_drops_control_and_reserved_columns():
    row = {"variant_id": "1", "template": "t.html", "format": "1080x1920", "status": "pending",
           "output_path": "", "_row": 2, "store_name": "Lyon 3", "address": "12 rue X", "empty": ""}
    assert mode2_html5.extract_params(row) == {"store_name": "Lyon 3", "address": "12 rue X"}


def test_auto_capture_is_static_without_duration(m2cfg):
    plan = mode2_html5.prepare({"variant_id": "1", "template": "t.html"}, m2cfg, {})
    assert plan["capture"] == "static" and plan["dst"].endswith(".jpg")


def test_auto_capture_becomes_video_with_a_duration(m2cfg):
    plan = mode2_html5.prepare({"variant_id": "1", "template": "t.html", "duration_s": "8"},
                               m2cfg, {"animated_engine": "video"})
    assert plan["capture"] == "video" and plan["duration"] == 8.0
    assert plan["dst"].endswith(".mp4")


def test_explicit_capture_without_duration_falls_back_to_config(m2cfg):
    plan = mode2_html5.prepare({"variant_id": "1", "template": "t.html", "capture": "frames"},
                               m2cfg, {})
    assert plan["duration"] == 10.0


def test_row_overrides_job_settings(m2cfg):
    plan = mode2_html5.prepare({"variant_id": "1", "template": "t.html", "inject": "dom"},
                               m2cfg, {"inject": "query"})
    assert plan["inject"] == "dom"


def test_query_injection_appends_params_and_keeps_existing_query():
    url = mode2_html5.build_url("https://x/c.html?a=1", {"store": "Lyon 3"}, "query")
    assert url.startswith("https://x/c.html?a=1&") and "store=Lyon+3" in url


def test_non_query_injection_leaves_the_url_alone():
    assert mode2_html5.build_url("https://x/c.html", {"a": "1"}, "dom") == "https://x/c.html"


def test_missing_template_fails_loud(m2cfg):
    with pytest.raises(FileNotFoundError):
        mode2_html5.prepare({"variant_id": "1", "template": "nope.html"}, m2cfg, {})


def test_http_template_needs_no_local_file(m2cfg):
    plan = mode2_html5.prepare({"variant_id": "1", "template": "https://x/c.html"}, m2cfg, {})
    assert plan["files"] == [] and plan["url"].startswith("https://")


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cadence,expected", [
    ("once", {"type": "once"}),
    ("", {"type": "once"}),
    ("hourly", {"type": "cron", "minute": 0}),
    ("daily@08:00", {"type": "cron", "hour": 8, "minute": 0}),
    ("every_5_minutes", {"type": "interval", "seconds": 300}),
    ("every_2_hours", {"type": "interval", "seconds": 7200}),
])
def test_parse_cadence(cadence, expected):
    assert scheduler.parse_cadence(cadence) == expected


def test_parse_cadence_accepts_raw_cron():
    assert scheduler.parse_cadence("cron:*/5 9-19 * * 1-5") == {
        "type": "cron", "minute": "*/5", "hour": "9-19", "day": "*",
        "month": "*", "day_of_week": "1-5"}


def test_cadence_below_the_floor_is_refused():
    with pytest.raises(scheduler.CadenceError):
        scheduler.parse_cadence("every_10_seconds")


def test_cadence_floor_is_configurable():
    assert scheduler.parse_cadence("every_10_seconds", min_interval_seconds=5) == {
        "type": "interval", "seconds": 10}


@pytest.mark.parametrize("bad", ["daily@99:00", "every_0_minutes", "weekly", "cron:* *"])
def test_unparseable_cadences_raise(bad):
    with pytest.raises(ValueError):
        scheduler.parse_cadence(bad)


def test_crontab_lines_cover_scheduled_jobs_only():
    cfg = {"jobs": {"a": {"cadence": "daily@08:00"}, "b": {"cadence": "once"},
                    "c": {"cadence": "every_15_minutes"}}}
    lines = scheduler.crontab_lines(cfg, project_dir="/tmp/p")
    body = [ln for ln in lines if not ln.startswith("#")]
    assert len(body) == 2
    assert any(ln.startswith("0 8 * * *") and "--job a" in ln for ln in body)
    assert any(ln.startswith("*/15 * * * *") for ln in body)


# --------------------------------------------------------------------------- #
# sheets parsing
# --------------------------------------------------------------------------- #

def test_parse_values_indexes_rows_from_two():
    rows = sheets.parse_values([["creative_id", "status"], ["1", "done"], ["2", ""]])
    assert [r["_row"] for r in rows] == [2, 3]
    assert rows[0]["status"] == "done"


def test_parse_values_survives_blank_and_duplicate_headers():
    rows = sheets.parse_values([["id", "", "id", "market"], ["1", "junk", "2", "FR"]])
    assert rows[0]["id"] == "1" and rows[0]["market"] == "FR"


def test_parse_values_pads_short_rows_and_drops_empty_ones():
    rows = sheets.parse_values([["a", "b", "c"], ["1"], ["", "", ""], ["2", "x", "y"]])
    assert len(rows) == 2
    assert rows[0]["c"] == ""


# --------------------------------------------------------------------------- #
# runner behaviour
# --------------------------------------------------------------------------- #

def _fake_prepare(row, cfg, job):
    return {"stem": row["stem"], "files": [], "hash": row.get("h", "h1")}


def _collect():
    calls = []

    def write_back(row, path, status, msg="", input_hash=None):
        calls.append({"row": row.get("_row"), "path": path, "status": status,
                      "msg": msg, "hash": input_hash})
    return calls, write_back


def test_runner_renders_pending_skips_done_and_keeps_going_after_an_error():
    rows = [
        {"_row": 2, "stem": "a", "status": "pending"},
        {"_row": 3, "stem": "b", "status": "done"},
        {"_row": 4, "stem": "boom", "status": "pending"},
        {"_row": 5, "stem": "d", "status": "error"},
    ]
    calls, wb = _collect()

    def render(plan, cfg, job):
        if plan["stem"] == "boom":
            raise RuntimeError("ffmpeg exit 1: no such file")
        return f"/out/{plan['stem']}.mp4"

    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=render)
    assert (s.rendered, s.skipped, s.errors, s.total) == (2, 1, 1, 4)
    assert [c["status"] for c in calls] == ["done", "error", "done"]
    assert "no such file" in calls[1]["msg"]
    assert calls[0]["hash"] == "h1"


def test_runner_force_rerenders_done_rows():
    rows = [{"_row": 2, "stem": "a", "status": "done"}]
    calls, wb = _collect()
    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare,
                        render=lambda p, c, j: "/out/a.mp4", force=True)
    assert s.rendered == 1


def test_runner_rerenders_a_done_row_whose_hash_moved():
    rows = [{"_row": 2, "stem": "a", "status": "done", "input_hash": "old", "h": "new"}]
    _calls, wb = _collect()
    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare,
                        render=lambda p, c, j: "/out/a.mp4")
    assert s.rendered == 1


def test_runner_refuses_to_silently_overwrite_a_duplicate_output_name():
    rows = [{"_row": 2, "stem": "same", "status": "pending"},
            {"_row": 3, "stem": "same", "status": "pending"}]
    calls, wb = _collect()
    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare,
                        render=lambda p, c, j: "/out/same.mp4")
    assert (s.rendered, s.errors) == (1, 1)
    # The first row to claim the name wins; write-back order is not part of the
    # contract (collisions surface during planning, renders land afterwards),
    # so assert per row rather than positionally.
    by_row = {c["row"]: c for c in calls}
    assert by_row[2]["status"] == "done"
    assert by_row[3]["status"] == "error" and "collision" in by_row[3]["msg"]


def test_runner_dry_run_renders_nothing():
    rows = [{"_row": 2, "stem": "a", "status": "pending"}]
    calls, wb = _collect()

    def render(plan, cfg, job):
        raise AssertionError("dry-run must not render")

    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=render, dry_run=True)
    assert (s.rendered, s.skipped) == (0, 1) and calls == []


def test_runner_limit_caps_rendered_rows():
    rows = [{"_row": i, "stem": f"s{i}", "status": "pending"} for i in range(2, 8)]
    _calls, wb = _collect()
    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare,
                        render=lambda p, c, j: "/out/x.mp4", limit=2)
    assert s.rendered == 2 and s.skipped == 4


def test_runner_reports_a_prepare_failure_per_row():
    rows = [{"_row": 2, "stem": "a", "status": "pending"}]
    calls, wb = _collect()

    def bad_prepare(row, cfg, job):
        raise FileNotFoundError("clip not found: assets/x.mp4")

    s = runner.run_rows(rows, wb, {}, {}, prepare=bad_prepare, render=lambda *a: "")
    assert s.errors == 1 and "clip not found" in calls[0]["msg"]


def test_runner_renders_in_parallel_without_losing_or_duplicating_rows():
    rows = [{"_row": i, "stem": f"s{i}", "status": "pending"} for i in range(2, 22)]
    seen, seen_lock = [], threading.Lock()
    _calls, wb = _collect()

    def render(plan, cfg, job):
        time.sleep(0.005)
        with seen_lock:
            seen.append(plan["stem"])
        return f"/out/{plan['stem']}.mp4"

    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=render, workers=4)
    assert s.rendered == 20 and s.errors == 0
    assert sorted(seen) == sorted(f"s{i}" for i in range(2, 22))


def test_parallel_workers_each_get_their_own_render_context():
    rows = [{"_row": i, "stem": f"s{i}", "status": "pending"} for i in range(2, 12)]
    _calls, wb = _collect()
    opened, closed, threads = [], [], set()
    guard = threading.Lock()

    class Ctx:
        def __enter__(self):
            with guard:
                opened.append(threading.current_thread().name)
            return self

        def __exit__(self, *exc):
            with guard:
                closed.append(threading.current_thread().name)

        def render(self, plan, cfg, job):
            time.sleep(0.005)
            with guard:
                threads.add(threading.current_thread().name)
            return f"/out/{plan['stem']}.mp4"

    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=None,
                        render_context=Ctx, workers=3)
    assert s.rendered == 10 and s.errors == 0
    assert len(opened) == 3 and sorted(opened) == sorted(closed)
    # a context is entered and exited by the same thread - Playwright requires it
    assert threads <= set(opened)


def test_parallel_render_failures_stay_per_row():
    rows = [{"_row": i, "stem": f"s{i}", "status": "pending"} for i in range(2, 12)]
    calls, wb = _collect()

    def render(plan, cfg, job):
        if plan["stem"] in ("s3", "s7"):
            raise RuntimeError("boom " + plan["stem"])
        return f"/out/{plan['stem']}.mp4"

    s = runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=render, workers=4)
    assert (s.rendered, s.errors) == (8, 2)
    assert sum(1 for c in calls if c["status"] == "error") == 2


def test_runner_flushes_the_write_back_buffer():
    flushed = []
    rows = [{"_row": 2, "stem": "a", "status": "pending"}]

    def wb(row, path, status, msg="", input_hash=None):
        pass
    wb.flush = lambda: flushed.append(True)

    runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=lambda p, c, j: "/o.mp4")
    assert flushed == [True]


# --------------------------------------------------------------------------- #
# delivery renditions (P5)
# --------------------------------------------------------------------------- #

def test_channel_config_merges_over_the_defaults():
    cfg = {"delivery": {"channels": {"ctv": {"ladder": [{"width": 1920, "height": 1080}]}}}}
    ch = renditions.channel_config(cfg, "ctv")
    assert ch["audio"] is True and ch["mezzanine"] is True   # from the default
    assert len(ch["ladder"]) == 1                            # overridden


def test_unknown_channel_is_refused():
    with pytest.raises(ValueError) as e:
        renditions.channel_config({}, "cinema")
    assert "cinema" in str(e.value)


def test_dooh_channel_is_muted_and_ctv_is_not():
    assert renditions.audio_spec(renditions.channel_config({}, "dooh"), {}) is None
    assert renditions.audio_spec(renditions.channel_config({}, "ctv"), {}) is not None


def test_rung_overrides_the_channel_audio_bitrate():
    ch = {"audio": True, "audio_spec": {"bitrate": "128k"}}
    assert renditions.audio_spec(ch, {"audio_bitrate": "256k"})["bitrate"] == "256k"


@pytest.mark.parametrize("raw,expected", [
    ("8M", 8000), ("600k", 600), ("1.5M", 1500), ("", 0), (None, 0), ("nonsense", 0),
])
def test_bitrate_to_kbps(raw, expected):
    assert renditions._kbps(raw) == expected


def test_package_is_a_no_op_without_a_delivery_block():
    assert renditions.package("/o/x.mp4", {"stem": "x"}, {}, {}) == {}


def test_radio_channel_is_audio_only():
    ch = renditions.channel_config({}, "radio")
    assert ch["audio_only"] is True
    assert all("width" not in rung for rung in ch["ladder"])


# --------------------------------------------------------------------------- #
# VAST (P5)
# --------------------------------------------------------------------------- #

def _rends(**over):
    base = {"path": "/o/spot_1920x1080.mp4", "width": 1920, "height": 1080,
            "bitrate_kbps": 8000, "codec": "libx264", "mime": "video/mp4",
            "bytes": 1, "role": "media"}
    base.update(over)
    return [base]


def _tag(channel="dooh", renditions_=None, **cfg):
    # `is None` on purpose: an empty list is a case under test, not a default.
    return vast.build_inline(ad_id="spot", ad_serving_id="spot-abc", title="Camp",
                             duration_s=10.0,
                             renditions=_rends() if renditions_ is None else renditions_,
                             vast_cfg=cfg, channel=channel, base_url="https://cdn.test/c")


@pytest.mark.parametrize("seconds,expected", [
    (10, "00:00:10"), (9.5, "00:00:09.500"), (65, "00:01:05"), (3661, "01:01:01"), (0, "00:00:00"),
])
def test_vast_duration_format(seconds, expected):
    assert vast.duration_hms(seconds) == expected


def test_vast_is_well_formed_and_carries_the_required_vast4_elements():
    root = ET.fromstring(_tag())
    assert root.get("version") == "4.2"
    assert root.find(".//AdServingId") is not None, "VAST 4.1+ requires AdServingId"
    assert root.find(".//UniversalAdId") is not None, "VAST 4 requires UniversalAdId"
    assert root.find(".//UniversalAdId").get("idRegistry") == "unknown"


def test_tracking_urls_keep_their_macros_and_ampersands():
    xml = _tag(impression_url="https://ad.test/i?cb=[CACHEBUSTING]&pid=1&x=2",
               error_url="https://ad.test/e?code=[ERRORCODE]")
    assert "[CACHEBUSTING]" in xml and "[ERRORCODE]" in xml
    assert "&amp;" not in xml, "an escaped & would break the query string the player calls"
    assert "<![CDATA[" in xml
    root = ET.fromstring(xml)
    assert root.findtext(".//Impression").endswith("pid=1&x=2")


def test_dooh_gets_no_clickthrough_but_ctv_does():
    assert ET.fromstring(_tag("dooh", click_through="https://b.test")).find(".//VideoClicks") is None
    assert ET.fromstring(_tag("ctv", click_through="https://b.test")).find(".//VideoClicks") is not None


def test_dooh_uses_its_own_event_set():
    xml = _tag("dooh", tracking={e: f"https://t/{e}" for e in
                                 ("start", "complete", "firstQuartile")})
    events = {t.get("event") for t in ET.fromstring(xml).findall(".//Tracking")}
    assert "firstQuartile" not in events, "a street panel has no meaningful quartile"
    assert {"start", "complete"} <= events


def test_ctv_emits_the_full_quartile_set():
    xml = _tag("ctv", tracking={e: f"https://t/{e}" for e in vast.QUARTILE_EVENTS})
    events = {t.get("event") for t in ET.fromstring(xml).findall(".//Tracking")}
    assert events == set(vast.QUARTILE_EVENTS)


def test_every_rendition_becomes_a_mediafile_with_its_bitrate():
    r = _rends() + [{"path": "/o/spot_1280x720.mp4", "width": 1280, "height": 720,
                     "bitrate_kbps": 4000, "codec": "libx264", "mime": "video/mp4",
                     "bytes": 1, "role": "media"}]
    files = ET.fromstring(_tag("ctv", renditions_=r)).findall(".//MediaFile")
    assert [(f.get("width"), f.get("bitrate")) for f in files] == \
           [("1920", "8000"), ("1280", "4000")]
    assert all(f.get("codec") == "H.264" for f in files)


def test_mezzanine_is_emitted_when_present():
    r = _rends() + [{"path": "/o/spot.mp4", "width": 0, "height": 0, "bitrate_kbps": 0,
                     "codec": "libx264", "mime": "video/mp4", "bytes": 1,
                     "role": "mezzanine"}]
    root = ET.fromstring(_tag("ctv", renditions_=r))
    assert root.find(".//Mezzanine") is not None, "SSAI without a mezzanine is VAST error 406"
    assert len(root.findall(".//MediaFile")) == 1, "the mezzanine is not a MediaFile"


def test_audio_rendition_drops_the_meaningless_scaling_attributes():
    r = _rends(path="/o/spot.m4a", width=0, height=0, mime="audio/mp4",
               codec="aac", bitrate_kbps=192)
    f = ET.fromstring(_tag("radio", renditions_=r)).find(".//MediaFile")
    assert f.get("type") == "audio/mp4" and f.get("codec") == "AAC"
    assert (f.get("width"), f.get("height")) == ("0", "0")
    assert f.get("scalable") is None and f.get("maintainAspectRatio") is None


def test_a_tag_needs_at_least_one_playable_rendition():
    with pytest.raises(ValueError):
        _tag(renditions_=[])
    with pytest.raises(ValueError):
        _tag(renditions_=[dict(_rends()[0], role="mezzanine")])


def test_asset_url_uses_the_cdn_base_or_falls_back_to_file():
    assert vast.asset_url("/o/a.mp4", "https://cdn.test/c/") == "https://cdn.test/c/a.mp4"
    assert vast.asset_url("/o/a.mp4", "").startswith("file:///")


# --------------------------------------------------------------------------- #
# sheet model + DCO expansion (P6)
# --------------------------------------------------------------------------- #

def test_sheet_model_matches_what_the_engine_reads():
    sheet_model.check_model_matches_code()


def test_media_tab_bridges_to_the_creative_tabs():
    header = sheet_model.MEDIA_HEADER
    for col in ("creative_tab", "creative_id", "creative_source",
                "hosted_asset", "third_party_vast_url"):
        assert col in header
    rows = sheet_model.media_examples()
    vast_col = header.index("third_party_vast_url")
    assert all(r[vast_col].startswith("=") for r in rows), "the bridge must be a live lookup"


def test_dco_axes_produce_the_cartesian_product():
    rows = expand_dco.combinations({"market": ["FR", "BE"], "offer": ["-30%", "-50%"]})
    assert len(rows) == 4
    assert {"market": "FR", "offer": "-50%"} in rows


def test_dco_exclusions_are_applied():
    rows = expand_dco.combinations({"market": ["FR", "BE"], "offer": ["-30%", "-50%"]},
                                   exclude=[{"market": "BE", "offer": "-50%"}])
    assert len(rows) == 3
    assert {"market": "BE", "offer": "-50%"} not in rows


def test_slug_interpolation_makes_filename_safe_values():
    values = {"market": "FR", "offer": "-50%"}
    assert expand_dco.interpolate("cta_{offer|slug}.mp4", values) == "cta_50pct.mp4"
    assert expand_dco.interpolate("intro_{market}.mp4", values) == "intro_FR.mp4"


def test_expand_builds_rows_in_model_order_with_slots_resolved():
    header, rows = expand_dco.expand({
        "mode": "mode1",
        "base": {"campaign": "Soldes", "channel": "dooh"},
        "axes": {"market": ["FR", "BE"], "audience": ["young"]},
        "slots": {"clip_1": "intro_{market}.mp4"},
    })
    assert len(rows) == 2
    by_col = dict(zip(header, rows[0]))
    assert by_col["clip_1"] == "intro_FR.mp4"
    assert by_col["campaign"] == "Soldes"
    assert by_col["status"] == "pending"
    assert by_col["creative_id"] == "FR-young"


def test_expand_refuses_two_combinations_with_the_same_id():
    with pytest.raises(ValueError) as e:
        expand_dco.expand({"mode": "mode1", "base": {"creative_id": "fixed"},
                           "axes": {"market": ["FR", "BE"]}})
    assert "same creative_id" in str(e.value)


def test_expand_rejects_a_column_the_mode1_model_does_not_have():
    with pytest.raises(ValueError):
        expand_dco.expand({"mode": "mode1", "base": {"not_a_column": "x"},
                           "axes": {"market": ["FR"]}})
