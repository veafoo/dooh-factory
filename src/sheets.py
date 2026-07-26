"""Google Sheet I/O: read job rows, write back the render result.

Uses gspread + a service account. The sheet is the single source of truth for
what to render and where the result lands. Kept thin so mode1/mode2 stay
testable offline - they only ever see plain row dicts and a write_back callable.

Two things matter operationally here:

1. Rows are parsed from raw values, not `get_all_records()`, which throws on
   duplicate or blank header cells - and a real ops sheet always ends up with
   one of those.
2. Write-backs are buffered and flushed in a single `batch_update`. One cell at
   a time burns the Sheets quota (60 writes/min/user) after a few dozen rows.
"""
from __future__ import annotations

import logging
import random
import time

from . import contract

log = logging.getLogger("dooh.sheets")

# Sheets answers 429 on quota and 5xx on its own hiccups. Both are worth
# retrying; a 403 (sheet not shared with the service account) is not, and
# retrying it just delays the error the operator needs to see.
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5


def _is_transient(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in RETRY_STATUS:
        return True
    if status is not None:
        return False
    # No HTTP response at all: connection reset, DNS, timeout.
    return isinstance(exc, (OSError, TimeoutError))


def with_retry(fn, *args, what: str = "sheets call", attempts: int = MAX_ATTEMPTS, **kwargs):
    """Call `fn`, retrying transient Sheets/network failures with backoff.

    Exponential with jitter, so a batch of jobs hitting the quota at the same
    minute does not retry in lockstep and hit it again together.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - re-raised below unless transient
            if attempt == attempts or not _is_transient(e):
                raise
            delay = min(2 ** (attempt - 1), 16) * (1 + random.random() * 0.25)
            log.warning("%s failed (%s), retry %d/%d in %.1fs",
                        what, e.__class__.__name__, attempt, attempts - 1, delay)
            time.sleep(delay)


def open_worksheet(service_account_json: str, spreadsheet_id: str, worksheet: str):
    """Return a gspread worksheet handle."""
    import gspread  # local import: keeps offline tests dependency-free

    gc = gspread.service_account(filename=service_account_json)
    return with_retry(lambda: gc.open_by_key(spreadsheet_id).worksheet(worksheet),
                      what="open worksheet")


def parse_values(values: list[list[str]]) -> list[dict]:
    """Raw sheet values -> row dicts keyed by header, with a private `_row` index.

    Blank header cells are dropped, duplicate headers keep the leftmost column,
    fully empty rows are skipped. Pure function: this is what the offline tests
    exercise.
    """
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    rows = []
    for i, raw in enumerate(values[1:], start=2):  # row 1 = header
        rec: dict = {}
        for j, name in enumerate(header):
            if not name or name in rec:
                continue
            rec[name] = raw[j] if j < len(raw) else ""
        if not any(str(v).strip() for v in rec.values()):
            continue
        rec["_row"] = i
        rows.append(rec)
    return rows


def read_rows(ws) -> list[dict]:
    """All data rows as dicts keyed by header."""
    return parse_values(with_retry(ws.get_all_values, what="read rows"))


def make_write_back(ws, *, buffered: bool = True):
    """Return a `write_back(row, path, status, msg='', input_hash=None)`.

    Only writes columns the sheet actually has, so an existing sheet with just
    `output_path` + `status` keeps working unchanged. Call `.flush()` when done
    (the runner does it) - or pass `buffered=False` for immediate writes.
    """
    import gspread.utils as gu

    header = [str(h).strip() for h in with_retry(ws.row_values, 1, what="read header")]
    col = {}
    for i, name in enumerate(header):
        if name and name not in col:
            col[name] = i + 1

    missing = [c for c in ("output_path", "status") if c not in col]
    if missing:
        log.warning("worksheet has no %s column - those results will not be written back",
                    " / ".join(missing))

    pending: list[dict] = []

    def _set(row_idx: int, name: str, value) -> None:
        if name in col:
            pending.append({"range": gu.rowcol_to_a1(row_idx, col[name]),
                            "values": [[str(value)]]})

    def write_back(row: dict, path: str, status: str, msg: str = "",
                   input_hash: str | None = None, **extra) -> None:
        r = row["_row"]
        if path:
            _set(r, "output_path", path)
        # Without a `message` column the detail has to ride along in `status`,
        # otherwise the operator sees "error" and nothing else.
        _set(r, "status", status if ("message" in col or not msg) else f"{status}: {msg}")
        _set(r, "message", msg)
        if input_hash:
            _set(r, "input_hash", input_hash)
        if status == "done":
            _set(r, "rendered_at", contract.utcnow())
        # Delivery artefacts (vast_path, renditions) when the job produces them.
        for name, value in extra.items():
            if value:
                _set(r, name, value)
        if not buffered:
            flush()

    def flush() -> None:
        if not pending:
            return
        batch = list(pending)
        # Clear first: a retried-then-failed flush must not leave the buffer
        # armed to replay the same writes at the next flush.
        pending.clear()
        with_retry(ws.batch_update, batch, value_input_option="RAW", what="write back")
        log.info("wrote back %d cell(s)", len(batch))

    write_back.flush = flush  # type: ignore[attr-defined]
    return write_back


def null_write_back(*_a, **_kw) -> None:
    """No-op write_back for --dry-run and offline runs."""
