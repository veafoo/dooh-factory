"""The sheet data contract, in one place.

Both modes share the same spine: a row describes one creative to build, and
the tool writes the result back into reserved columns. Everything about which
columns are inputs, which are written back, how a row is named, and when a row
is considered up to date lives here so the two modes cannot drift apart.

Reserved columns (all optional except `status` in practice):

    output_path   written back - path of the rendered asset
    status        written back - pending | done | error
    message       written back - error detail (falls back into `status` if absent)
    input_hash    written back - fingerprint of the row's inputs
    rendered_at   written back - UTC ISO timestamp of the last render

`input_hash` is what makes runs idempotent beyond a plain `status = done`:
a row already done is re-rendered when its inputs changed (a cell was edited,
a rush was replaced on disk, the target format changed). Sheets without the
column keep the old behaviour - done means done.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone

# Columns the tool owns. Never injected as creative params, never hashed.
WRITE_BACK_COLS = ("output_path", "status", "message", "input_hash", "rendered_at")
# Internal bookkeeping added by sheets.read_rows().
META_COLS = ("_row",)
RESERVED = set(WRITE_BACK_COLS) | set(META_COLS)

_UNSAFE_EXT = re.compile(r"\.(mp4|mov|png|jpe?g|webp|html?)$", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_format(fmt, default: tuple[int, int]) -> tuple[int, int]:
    """'1080x1920' -> (1080, 1920). Anything unparseable falls back to default."""
    if fmt:
        m = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(fmt))
        if m:
            return int(m.group(1)), int(m.group(2))
    return default


def resolve_asset(ref: str, base_dir: str) -> str:
    """Resolve a sheet cell reference against a base dir (absolute refs pass through)."""
    ref = str(ref).strip()
    return ref if os.path.isabs(ref) else os.path.join(base_dir, ref)


def output_stem(row: dict, fallback: str = "{creative_id}") -> str:
    """Filename stem for a row.

    Explicit `output_name` wins over `naming_template` (BRIEF §2); both go
    through the same interpolation + sanitize pass, so an explicit name may
    still contain {placeholders}. A trailing extension is dropped - the caller
    owns the container.
    """
    from . import naming

    template = str(row.get("output_name") or row.get("naming_template") or fallback).strip()
    stem = naming.render_name(_UNSAFE_EXT.sub("", template), row)
    return stem


def input_fields(row: dict) -> dict:
    """The row without the columns the tool writes back."""
    return {k: v for k, v in row.items() if k not in RESERVED}


def compute_input_hash(row: dict, files: list[str] | None = None, extra: str = "") -> str:
    """Fingerprint of everything that can change the rendered output.

    Covers the row's input cells, the identity of each resolved input file
    (name + size + mtime - content hashing a 200 MB rush every tick is not
    worth it), and an `extra` string for the effective render spec (format,
    fps, codec) so a config change also invalidates.
    """
    h = hashlib.sha256()
    for k in sorted(input_fields(row)):
        h.update(f"{k}={row[k]}\x1f".encode())
    for f in files or []:
        try:
            st = os.stat(f)
            h.update(f"{os.path.basename(f)}:{st.st_size}:{int(st.st_mtime)}\x1f".encode())
        except OSError:
            h.update(f"{os.path.basename(f)}:missing\x1f".encode())
    h.update(extra.encode())
    return h.hexdigest()[:16]


def status_of(row: dict) -> str:
    return str(row.get("status", "")).strip().lower()


def is_settled(row: dict) -> bool:
    """True if the row is done AND the sheet has no hash column to check against.

    Cheap pre-gate: lets the runner skip a finished row without touching disk.
    """
    return status_of(row) == "done" and "input_hash" not in row


def needs_render(row: dict, new_hash: str) -> tuple[bool, str]:
    """Decide whether a `done` row must be rebuilt. Returns (render?, reason)."""
    if status_of(row) != "done":
        return True, status_of(row) or "pending"
    stored = str(row.get("input_hash", "")).strip()
    if "input_hash" not in row:
        return False, "done"
    if not stored:
        return True, "no stored hash"
    if stored != new_hash:
        return True, "inputs changed"
    return False, "done"
