#!/usr/bin/env python3
"""Provision a Google Sheet with the dooh-factory model.

Creates or refreshes three tabs - `mode1`, `mode2`, `media_dv360` - with the
exact header contract, a frozen header, dropdowns on the columns that have a
fixed vocabulary, status colouring, sensible widths and example rows to
overwrite. The model itself lives in `src/sheet_model.py`.

    # brand new spreadsheet, shared back to you
    python scripts/bootstrap_sheet.py --create "dooh-factory" --share you@gmail.com

    # provision the tabs of a sheet you already own
    python scripts/bootstrap_sheet.py --spreadsheet-id <id>

    # refresh header + dropdowns on one tab, keep your rows
    python scripts/bootstrap_sheet.py --spreadsheet-id <id> --tabs mode1

    # wipe a tab back to the model
    python scripts/bootstrap_sheet.py --spreadsheet-id <id> --tabs mode1 --force

    # no Google account yet: write the model as CSV instead
    python scripts/bootstrap_sheet.py --csv-out templates/sheet_model

Re-running is safe: an existing tab keeps its data rows and only gets its
header, dropdowns and formatting refreshed. `--force` is the destructive path.

A spreadsheet created by a service account is owned by that account and will
NOT appear in your Drive, which is why `--share` is required with `--create`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import contract, sheet_model  # noqa: E402

GREY = {"red": 0.95, "green": 0.95, "blue": 0.95}
HEADER_BG = {"red": 0.16, "green": 0.20, "blue": 0.29}
STATUS_COLORS = {
    "done": {"red": 0.85, "green": 0.93, "blue": 0.83},
    "error": {"red": 0.98, "green": 0.83, "blue": 0.82},
    "pending": {"red": 1.0, "green": 0.95, "blue": 0.80},
}


def _grid(sheet_id: int, header: list[str], column: str,
          start_row: int = 1, end_row: int = 5000) -> dict:
    idx = header.index(column)
    return {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row,
            "startColumnIndex": idx, "endColumnIndex": idx + 1}


def style_requests(ws, header: list[str]) -> list[dict]:
    """Dropdowns, status colouring, greyed write-back block, column widths."""
    sid = ws.id
    reqs: list[dict] = []

    for column, values in sheet_model.DROPDOWNS.items():
        if column not in header:
            continue
        reqs.append({"setDataValidation": {
            "range": _grid(sid, header, column),
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": v} for v in values if v]},
                "showCustomUi": True,
                # Not strict: a warning beats blocking an ops person who knows
                # what they are doing (a new xfade name, a new channel).
                "strict": False,
            },
        }})

    if "status" in header:
        for value, color in STATUS_COLORS.items():
            reqs.append({"addConditionalFormatRule": {
                "index": 0,
                "rule": {"ranges": [_grid(sid, header, "status")],
                         "booleanRule": {
                             "condition": {"type": "TEXT_EQ",
                                           "values": [{"userEnteredValue": value}]},
                             "format": {"backgroundColor": color}}},
            }})

    # The tool owns these columns; grey them so nobody types in them by hand.
    owned = [c for c in contract.WRITE_BACK_COLS if c in header]
    if owned:
        first = min(header.index(c) for c in owned)
        last = max(header.index(c) for c in owned)
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 5000,
                      "startColumnIndex": first, "endColumnIndex": last + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": GREY,
                                           "textFormat": {"italic": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat.italic)",
        }})

    for i, column in enumerate(header):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": sheet_model.WIDTHS.get(
                column, sheet_model.DEFAULT_WIDTH)},
            "fields": "pixelSize",
        }})

    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {
            "backgroundColor": HEADER_BG,
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "verticalAlignment": "MIDDLE"}},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})
    return reqs


def provision(sh, name: str, *, examples: bool, force: bool) -> str:
    spec = sheet_model.tab(name)
    header, rows = spec["header"], spec["examples"]
    try:
        ws = sh.worksheet(name)
        existing = True
    except Exception:  # WorksheetNotFound - any lookup failure means "create it"
        ws = sh.add_worksheet(name, rows=2000, cols=max(len(header), 26))
        existing = False

    if existing and force:
        ws.clear()
        existing = False
    if ws.col_count < len(header):
        ws.resize(cols=len(header))

    ws.update(values=[header], range_name="A1", value_input_option="RAW")

    action = "refreshed"
    if not existing:
        if examples and rows:
            # The media tab carries formulas that pull the rendered asset from
            # the creative tabs, so it has to be entered, not stored as text.
            option = "USER_ENTERED" if spec["formulas"] else "RAW"
            ws.update(values=rows, range_name="A2", value_input_option=option)
        action = "created"
    else:
        unknown = [c for c in ws.row_values(1) if c and c not in header]
        if unknown:
            print(f"    note: extra columns kept as-is: {', '.join(unknown)}")

    ws.freeze(rows=1)
    sh.batch_update({"requests": style_requests(ws, header)})
    try:
        ws.set_basic_filter()
    except Exception:  # a filter may already exist; not worth failing over
        pass
    return action


def write_csv(out_dir: str, tabs: list[str]) -> None:
    """Offline path: the same model as CSV, usable with `run.py --csv`."""
    os.makedirs(out_dir, exist_ok=True)
    for name in tabs:
        spec = sheet_model.tab(name)
        path = os.path.join(out_dir, f"{name}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(spec["header"])
            w.writerows(spec["examples"])
        print(f"  {path} ({len(spec['header'])} columns, {len(spec['examples'])} example rows)")


def main() -> int:
    sheet_model.check_model_matches_code()

    ap = argparse.ArgumentParser(description="Provision a Sheet with the dooh-factory model")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--create", metavar="TITLE", help="create a new spreadsheet")
    src.add_argument("--spreadsheet-id", help="provision an existing spreadsheet")
    src.add_argument("--csv-out", metavar="DIR", help="write the model as CSV, no Google call")
    ap.add_argument("--service-account", help="path to the service account JSON "
                                              "(default: read from the config)")
    ap.add_argument("--config", default="config/config.yml")
    ap.add_argument("--share", metavar="EMAIL",
                    help="give this address write access (required with --create)")
    ap.add_argument("--tabs", default=",".join(sheet_model.TABS))
    ap.add_argument("--no-examples", action="store_true", help="header only, no example rows")
    ap.add_argument("--force", action="store_true",
                    help="WIPE the tab and rewrite it from the model")
    args = ap.parse_args()

    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    unknown = [t for t in tabs if t not in sheet_model.TABS]
    if unknown:
        ap.error(f"unknown tab(s) {unknown}; available: {', '.join(sheet_model.TABS)}")

    if args.csv_out:
        write_csv(args.csv_out, tabs)
        return 0

    if args.create and not args.share:
        ap.error("--create needs --share EMAIL: a spreadsheet created by the service "
                 "account is owned by it and will not appear in your Drive")

    sa = args.service_account
    if not sa:
        import yaml
        if not os.path.isfile(args.config):
            ap.error(f"no --service-account and no config at {args.config}")
        with open(args.config) as f:
            sa = ((yaml.safe_load(f) or {}).get("google") or {}).get("service_account_json")
    if not sa or not os.path.isfile(sa):
        ap.error(f"service account JSON not found: {sa}")

    import gspread

    gc = gspread.service_account(filename=sa)
    if args.create:
        sh = gc.create(args.create)
        print(f"created spreadsheet '{args.create}'")
    else:
        sh = gc.open_by_key(args.spreadsheet_id)
        print(f"opened spreadsheet '{sh.title}'")

    if args.force:
        print(f"--force: tabs {', '.join(tabs)} will be wiped and rewritten")

    for name in tabs:
        print(f"  {name}: {provision(sh, name, examples=not args.no_examples, force=args.force)}")

    # Drop the default empty tab only once ours exist, never before.
    for ws in sh.worksheets():
        if ws.title in ("Sheet1", "Feuille 1") and len(sh.worksheets()) > 1:
            sh.del_worksheet(ws)
            print("  removed the default empty tab")

    if args.share:
        sh.share(args.share, perm_type="user", role="writer", notify=False)
        print(f"  shared with {args.share} (writer)")

    sa_email = ""
    try:
        with open(sa) as f:
            sa_email = json.load(f).get("client_email", "")
    except Exception:
        pass

    print(f"\nspreadsheet_id: {sh.id}\nurl:            {sh.url}")
    if sa_email:
        print(f"service account: {sa_email}")
        if args.spreadsheet_id:
            print("  (this address needs Editor access on the sheet)")
    print("\nnext:")
    print(f"  1. put the id in {args.config} under google.spreadsheet_id")
    print("  2. drop your rushes in assets/ and your templates in templates/")
    print("  3. python run.py --job mode1 --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
