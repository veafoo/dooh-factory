"""Offline unit tests: no ffmpeg, no browser, no Google credentials.

Covers the parts where a silent wrong answer is expensive - the idempotency
decision, the naming contract, cadence safety, and sheet parsing.

    pytest tests -q
"""
from __future__ import annotations

import os

import pytest

from src import contract, ffmpeg_utils, mode1_stitch, mode2_html5, naming, runner, scheduler, sheets


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
    assert "collision" in calls[1]["msg"]


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


def test_runner_flushes_the_write_back_buffer():
    flushed = []
    rows = [{"_row": 2, "stem": "a", "status": "pending"}]

    def wb(row, path, status, msg="", input_hash=None):
        pass
    wb.flush = lambda: flushed.append(True)

    runner.run_rows(rows, wb, {}, {}, prepare=_fake_prepare, render=lambda p, c, j: "/o.mp4")
    assert flushed == [True]
