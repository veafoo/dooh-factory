"""Preflight: everything that can be wrong before the first live run, checked.

`run.py --check` walks the whole chain - binaries, config, paths, credentials,
Sheets auth, spreadsheet access, write permission, per-tab header contract - and
reports each step with the action that fixes it.

The point is the diagnosis, not the check. Without this, the first live run
fails somewhere inside gspread and hands back a stack trace and a 403, which
says nothing about the actual cause: the sheet was shared with the wrong
address, or shared read-only, or the Sheets API was never enabled on the project.
Each of those has a different fix and they all look the same from the outside.

Every check returns (ok, message, fix). Nothing here writes anything the user
did not already have: the write-permission probe rewrites a cell with the value
it already holds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import sheet_model

OK, WARN, FAIL = "ok", "warn", "fail"


class Report:
    """Collected results. Warnings do not fail the run; failures do."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, level: str, step: str, detail: str, fix: str = "") -> None:
        self.rows.append((level, step, detail, fix))

    @property
    def failed(self) -> bool:
        return any(r[0] == FAIL for r in self.rows)

    def render(self) -> str:
        mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
        out = []
        for level, step, detail, fix in self.rows:
            out.append(f"[{mark[level]}] {step}: {detail}")
            if fix and level != OK:
                for line in fix.splitlines():
                    out.append(f"           -> {line}")
        counts = {lv: sum(1 for r in self.rows if r[0] == lv) for lv in (OK, WARN, FAIL)}
        out.append("")
        out.append(f"{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)")
        if not counts[FAIL]:
            out.append("ready: python run.py --job mode1 --dry-run")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# local environment
# --------------------------------------------------------------------------- #

def check_binaries(rep: Report, cfg: dict) -> None:
    ff = cfg.get("ffmpeg", {})
    for name, binary in (("ffmpeg", ff.get("bin", "ffmpeg")),
                         ("ffprobe", ff.get("ffprobe_bin", "ffprobe"))):
        path = shutil.which(binary)
        if not path:
            rep.add(FAIL, name, f"{binary!r} not on PATH",
                    "brew install ffmpeg   (or your platform's package)")
            continue
        try:
            v = subprocess.run([binary, "-version"], capture_output=True, text=True,
                               timeout=10).stdout.splitlines()[0]
        except Exception:
            v = "version unreadable"
        rep.add(OK, name, f"{path} - {v}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        rep.add(WARN, "playwright", "not installed - Mode 2 will not run",
                "pip install playwright && playwright install chromium")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            version = b.version
            b.close()
        rep.add(OK, "chromium", f"launches, version {version}")
    except Exception as e:
        rep.add(WARN, "chromium", f"cannot launch: {e}", "playwright install chromium")


def check_paths(rep: Report, cfg: dict) -> None:
    paths = cfg.get("paths") or {}
    for key in ("assets_dir", "templates_dir"):
        d = paths.get(key)
        if not d:
            rep.add(WARN, key, "not configured")
            continue
        if os.path.isdir(d):
            n = len([f for f in os.listdir(d) if not f.startswith(".")])
            rep.add(OK if n else WARN, key, f"{d} ({n} file(s))",
                    "" if n else f"empty - put your assets in {d}")
        else:
            rep.add(FAIL, key, f"{d} does not exist", f"mkdir -p {d}")

    out = paths.get("output_dir")
    if not out:
        rep.add(FAIL, "output_dir", "not configured", "add paths.output_dir to the config")
        return
    try:
        os.makedirs(out, exist_ok=True)
        probe = os.path.join(out, ".preflight")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        rep.add(OK, "output_dir", f"{out} is writable")
    except OSError as e:
        rep.add(FAIL, "output_dir", f"{out} not writable: {e}", "fix the permissions")


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def check_service_account(rep: Report, cfg: dict) -> str | None:
    """-> the service account's client_email, or None. Also flags loose perms."""
    path = ((cfg.get("google") or {}).get("service_account_json") or "").strip()
    if not path:
        rep.add(FAIL, "service account", "google.service_account_json not set in the config",
                "point it at the JSON key you downloaded from Google Cloud")
        return None
    if not os.path.isfile(path):
        rep.add(FAIL, "service account", f"{path} not found",
                "Google Cloud console -> IAM -> Service accounts -> Keys -> Add key (JSON)\n"
                f"save it as {path} (it is git-ignored)")
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        rep.add(FAIL, "service account", f"{path} is not readable JSON: {e}",
                "re-download the key")
        return None

    missing = [k for k in ("client_email", "private_key", "project_id") if not data.get(k)]
    if missing:
        rep.add(FAIL, "service account", f"key is missing {', '.join(missing)}",
                "this is probably an OAuth client file, not a service-account key")
        return None

    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        rep.add(WARN, "service account", f"{path} is mode {mode:o} - readable by others",
                f"chmod 600 {path}")
    else:
        rep.add(OK, "service account", f"{path} (mode {mode:o})")
    rep.add(OK, "service account identity", data["client_email"])
    return data["client_email"]


# --------------------------------------------------------------------------- #
# the sheet
# --------------------------------------------------------------------------- #

def check_spreadsheet(rep: Report, cfg: dict, sa_email: str | None, jobs: list[str]):
    """Auth, open, write permission, then the header contract of each job's tab."""
    google = cfg.get("google") or {}
    sheet_id = (google.get("spreadsheet_id") or "").strip()
    if not sheet_id or sheet_id.startswith("PUT_"):
        rep.add(FAIL, "spreadsheet_id", "not set in the config",
                "python scripts/bootstrap_sheet.py --create \"dooh-factory\" --share <you@gmail.com>\n"
                "then copy the printed id into google.spreadsheet_id")
        return

    try:
        import gspread
    except ImportError:
        rep.add(FAIL, "gspread", "not installed", "pip install -r requirements.txt")
        return

    try:
        gc = gspread.service_account(filename=google["service_account_json"])
    except Exception as e:
        rep.add(FAIL, "Sheets auth", str(e).splitlines()[0],
                "check the key file; make sure the Sheets AND Drive APIs are enabled "
                "on the Google Cloud project")
        return
    rep.add(OK, "Sheets auth", "credentials accepted")

    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        detail = str(e).splitlines()[0]
        fix = (f"share the spreadsheet with {sa_email} as Editor"
               if sa_email else "share the spreadsheet with the service account as Editor")
        if "404" in detail or "not found" in detail.lower():
            fix = "the id is wrong, or the sheet was never shared with the service account:\n" + fix
        elif "403" in detail:
            fix = ("the service account cannot see this sheet:\n" + fix +
                   "\nalso check the Sheets API is enabled on the Cloud project")
        rep.add(FAIL, "spreadsheet", detail, fix)
        return
    rep.add(OK, "spreadsheet", f"{sh.title!r} ({len(sh.worksheets())} tab(s))")

    _check_write_permission(rep, sh, sa_email)

    tabs = {ws.title for ws in sh.worksheets()}
    for job_name, worksheet in jobs:
        if worksheet not in tabs:
            rep.add(FAIL, f"tab {worksheet!r}", f"missing (job {job_name!r})",
                    f"python scripts/bootstrap_sheet.py --spreadsheet-id {sheet_id} "
                    f"--tabs {worksheet}")
            continue
        _check_header(rep, sh.worksheet(worksheet), worksheet, sheet_id)


def _check_write_permission(rep: Report, sh, sa_email: str | None) -> None:
    """Rewrite one cell with the value it already holds.

    A real write, so a read-only share is caught here rather than at the end of
    the first batch - and nothing in the sheet changes.
    """
    try:
        ws = sh.sheet1
        current = ws.acell("A1").value or ""
        ws.update(values=[[current]], range_name="A1", value_input_option="RAW")
        rep.add(OK, "write access", "the service account can write")
    except Exception as e:
        detail = str(e).splitlines()[0]
        rep.add(FAIL, "write access", detail,
                f"the sheet is shared read-only. Re-share it with {sa_email or 'the service account'} "
                "as **Editor** (Viewer is not enough: the tool writes output_path and status back)")


def _check_header(rep: Report, ws, name: str, sheet_id: str) -> None:
    header = [h.strip() for h in ws.row_values(1)]
    if not header:
        rep.add(FAIL, f"tab {name!r}", "no header row",
                f"python scripts/bootstrap_sheet.py --spreadsheet-id {sheet_id} --tabs {name}")
        return

    model = sheet_model.TABS.get(name, {}).get("header", [])
    from . import contract

    missing_wb = [c for c in ("output_path", "status") if c not in header]
    if missing_wb:
        rep.add(FAIL, f"tab {name!r}", f"missing {', '.join(missing_wb)}",
                "the tool has nowhere to write the result back")
        return

    optional = [c for c in contract.WRITE_BACK_COLS if c not in header]
    unknown = [c for c in header if model and c not in model]
    rows = max(0, len(ws.col_values(1)) - 1)
    rep.add(OK, f"tab {name!r}", f"{len(header)} columns, {rows} data row(s)")
    if optional:
        rep.add(WARN, f"tab {name!r} optional columns", f"absent: {', '.join(optional)}",
                "each one you add enables a feature: input_hash drives re-rendering, "
                "message carries the error detail, vast_path and renditions record delivery")
    if unknown:
        rep.add(WARN, f"tab {name!r} extra columns", ", ".join(unknown),
                "harmless on mode1; on mode2 every extra column is injected into the "
                "creative as a param")


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #

def run(cfg: dict, jobs: list[tuple[str, str]], *, offline: bool = False) -> Report:
    rep = Report()
    check_binaries(rep, cfg)
    check_paths(rep, cfg)
    sa_email = check_service_account(rep, cfg)
    if offline:
        rep.add(WARN, "spreadsheet", "skipped (--offline)")
    elif sa_email:
        check_spreadsheet(rep, cfg, sa_email, jobs)
    else:
        rep.add(FAIL, "spreadsheet", "skipped: no usable service account")
    return rep
