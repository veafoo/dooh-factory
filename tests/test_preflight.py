"""Preflight diagnostics, exercised on the failures they exist to explain.

Each of these produces the same opaque symptom in the wild - the first live run
dies inside gspread - and each has a different fix. The test is that the report
names the right one.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from src import preflight

FAIL, WARN, OK = preflight.FAIL, preflight.WARN, preflight.OK


def _levels(rep, step_contains):
    return [(lv, detail, fix) for lv, step, detail, fix in rep.rows if step_contains in step]


def _sa(tmp_path, **over):
    data = {"client_email": "bot@proj.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
            "project_id": "proj"}
    data.update(over)
    p = tmp_path / "sa.json"
    p.write_text(json.dumps(data))
    os.chmod(p, 0o600)
    return str(p)


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def test_missing_service_account_says_where_to_get_one(tmp_path):
    rep = preflight.Report()
    email = preflight.check_service_account(
        rep, {"google": {"service_account_json": str(tmp_path / "nope.json")}})
    assert email is None
    lv, detail, fix = _levels(rep, "service account")[0]
    assert lv == FAIL and "not found" in detail
    assert "Service accounts" in fix and "JSON" in fix


def test_unset_service_account_path_is_a_failure_not_a_crash():
    rep = preflight.Report()
    assert preflight.check_service_account(rep, {}) is None
    assert _levels(rep, "service account")[0][0] == FAIL


def test_an_oauth_client_file_is_named_as_such(tmp_path):
    """The commonest wrong download: it is valid JSON, it just is not a key."""
    p = tmp_path / "client.json"
    p.write_text(json.dumps({"installed": {"client_id": "x"}}))
    rep = preflight.Report()
    assert preflight.check_service_account(rep, {"google": {"service_account_json": str(p)}}) is None
    lv, detail, fix = _levels(rep, "service account")[0]
    assert lv == FAIL and "missing" in detail
    assert "OAuth client" in fix


def test_unreadable_json_is_reported_as_such(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    rep = preflight.Report()
    assert preflight.check_service_account(rep, {"google": {"service_account_json": str(p)}}) is None
    assert "not readable JSON" in _levels(rep, "service account")[0][1]


def test_world_readable_key_warns_without_blocking(tmp_path):
    p = _sa(tmp_path)
    os.chmod(p, 0o644)
    rep = preflight.Report()
    email = preflight.check_service_account(rep, {"google": {"service_account_json": p}})
    assert email == "bot@proj.iam.gserviceaccount.com"
    warn = [r for r in _levels(rep, "service account") if r[0] == WARN]
    assert warn and "chmod 600" in warn[0][2]
    assert not rep.failed, "loose permissions are a warning, not a blocker"


def test_a_good_key_surfaces_the_identity_to_share_the_sheet_with(tmp_path):
    rep = preflight.Report()
    email = preflight.check_service_account(rep, {"google": {"service_account_json": _sa(tmp_path)}})
    assert email == "bot@proj.iam.gserviceaccount.com"
    assert any("bot@proj" in d for _lv, d, _f in _levels(rep, "service account identity"))


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def test_missing_asset_dir_fails_with_the_mkdir(tmp_path):
    rep = preflight.Report()
    preflight.check_paths(rep, {"paths": {"assets_dir": str(tmp_path / "nope"),
                                          "output_dir": str(tmp_path / "out")}})
    lv, detail, fix = _levels(rep, "assets_dir")[0]
    assert lv == FAIL and "mkdir" in fix


def test_empty_asset_dir_only_warns(tmp_path):
    (tmp_path / "a").mkdir()
    rep = preflight.Report()
    preflight.check_paths(rep, {"paths": {"assets_dir": str(tmp_path / "a"),
                                          "output_dir": str(tmp_path / "out")}})
    assert _levels(rep, "assets_dir")[0][0] == WARN
    assert not rep.failed


def test_output_dir_is_created_and_probed(tmp_path):
    out = tmp_path / "deep" / "out"
    rep = preflight.Report()
    preflight.check_paths(rep, {"paths": {"output_dir": str(out)}})
    assert _levels(rep, "output_dir")[0][0] == OK
    assert out.is_dir()
    assert not (out / ".preflight").exists(), "the probe file must be cleaned up"


# --------------------------------------------------------------------------- #
# the spreadsheet
# --------------------------------------------------------------------------- #

def test_placeholder_spreadsheet_id_points_at_the_bootstrap_script():
    rep = preflight.Report()
    preflight.check_spreadsheet(rep, {"google": {"spreadsheet_id": "PUT_SHEET_ID_HERE"}},
                                "bot@proj.iam", [])
    lv, _detail, fix = _levels(rep, "spreadsheet_id")[0]
    assert lv == FAIL and "bootstrap_sheet.py" in fix


class _WS:
    def __init__(self, title, header, rows=3, writable=True):
        self.title = title
        self._header = header
        self._rows = rows
        self.writable = writable

    def row_values(self, n):
        return self._header if n == 1 else []

    def col_values(self, n):
        return ["h"] + ["x"] * self._rows

    def acell(self, a1):
        return type("C", (), {"value": "creative_id"})()

    def update(self, values=None, range_name=None, value_input_option=None):
        if not self.writable:
            raise RuntimeError("APIError: [403]: The caller does not have permission")


class _SH:
    def __init__(self, tabs, writable=True):
        self.title = "campaign sheet"
        self._tabs = tabs
        for t in tabs:
            t.writable = writable

    def worksheets(self):
        return self._tabs

    def worksheet(self, name):
        return next(t for t in self._tabs if t.title == name)

    @property
    def sheet1(self):
        return self._tabs[0]


def test_read_only_share_is_caught_before_the_first_render():
    """The failure that otherwise appears at the end of a batch, after the
    renders have already been paid for."""
    rep = preflight.Report()
    sh = _SH([_WS("mode1", ["creative_id", "status", "output_path"])], writable=False)
    preflight._check_write_permission(rep, sh, "bot@proj.iam")
    lv, detail, fix = _levels(rep, "write access")[0]
    assert lv == FAIL and "403" in detail
    assert "Editor" in fix and "bot@proj.iam" in fix


def test_writable_sheet_passes_without_changing_anything():
    rep = preflight.Report()
    preflight._check_write_permission(rep, _SH([_WS("mode1", ["creative_id"])]), "bot@x")
    assert _levels(rep, "write access")[0][0] == OK


def test_tab_without_the_write_back_columns_fails():
    rep = preflight.Report()
    preflight._check_header(rep, _WS("mode1", ["creative_id", "clip_1"]), "mode1", "ID")
    lv, detail, _fix = _levels(rep, "tab 'mode1'")[0]
    assert lv == FAIL and "output_path" in detail and "status" in detail


def test_tab_missing_only_the_optional_columns_warns_and_says_what_each_buys():
    rep = preflight.Report()
    preflight._check_header(rep, _WS("mode1", ["creative_id", "clip_1", "status", "output_path"]),
                            "mode1", "ID")
    assert not rep.failed
    warn = [r for r in _levels(rep, "tab 'mode1' optional") if r[0] == WARN]
    assert warn and "input_hash" in warn[0][1]
    assert "re-rendering" in warn[0][2]


def test_a_full_model_header_reports_clean():
    from src import sheet_model

    rep = preflight.Report()
    preflight._check_header(rep, _WS("mode1", sheet_model.MODE1_HEADER), "mode1", "ID")
    assert not rep.failed
    assert not [r for r in rep.rows if r[0] == WARN]


# --------------------------------------------------------------------------- #
# the report itself
# --------------------------------------------------------------------------- #

def test_report_fails_on_failures_only():
    rep = preflight.Report()
    rep.add(OK, "a", "fine")
    rep.add(WARN, "b", "meh")
    assert not rep.failed
    rep.add(FAIL, "c", "broken")
    assert rep.failed


def test_report_prints_the_fix_for_anything_not_ok():
    rep = preflight.Report()
    rep.add(OK, "a", "fine", "unused fix")
    rep.add(FAIL, "b", "broken", "do this")
    out = rep.render()
    assert "do this" in out and "unused fix" not in out
    assert "1 ok, 0 warning(s), 1 failure(s)" in out
    assert "ready:" not in out


def test_a_clean_report_ends_with_the_next_command():
    rep = preflight.Report()
    rep.add(OK, "a", "fine")
    assert "python run.py --job mode1 --dry-run" in rep.render()


def test_offline_mode_skips_the_network_checks(tmp_path):
    cfg = {"paths": {"output_dir": str(tmp_path / "o")},
           "google": {"service_account_json": _sa(tmp_path)},
           "ffmpeg": {}}
    rep = preflight.run(cfg, [], offline=True)
    assert any("skipped" in d for _lv, d, _f in _levels(rep, "spreadsheet"))
