"""Mode-agnostic batch runner.

Both modes are the same loop: decide whether a row still needs rendering,
build it, report the result back to the sheet, keep the batch alive when one
row fails. Only two things differ per mode, so that is all a mode provides:

    prepare(row, cfg, job) -> plan dict   # resolve inputs, name, fingerprint
    render(plan, cfg, job) -> output path # actually build the asset

A plan must carry at least `stem` (output filename stem), `files` (resolved
input paths) and `hash` (input fingerprint). `prepare` is the deterministic
gate: it fails loud on a missing asset before anything expensive starts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import contract

log = logging.getLogger("dooh.runner")


@dataclass
class Summary:
    total: int = 0
    rendered: int = 0
    skipped: int = 0
    errors: int = 0
    outputs: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def __str__(self) -> str:
        return (f"{self.rendered} rendered, {self.skipped} skipped, "
                f"{self.errors} errors ({self.total} rows, {self.seconds:.1f}s)")


def run_rows(
    rows,
    write_back,
    cfg: dict,
    job: dict,
    *,
    prepare,
    render,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> Summary:
    """Render every row that needs it. One failing row never stops the batch."""
    started = time.monotonic()
    s = Summary()
    claimed: dict[str, int] = {}  # stem -> sheet row, guards silent overwrites

    for row in rows:
        s.total += 1
        if limit is not None and (s.rendered + s.errors) >= limit:
            s.skipped += 1
            continue

        # Cheap gate first: a done row in a sheet without an input_hash column
        # can be skipped without touching the filesystem.
        if not force and contract.is_settled(row):
            s.skipped += 1
            continue

        rid = row.get("creative_id") or row.get("variant_id") or f"row {row.get('_row')}"
        try:
            plan = prepare(row, cfg, job)
        except Exception as e:  # noqa: BLE001 - report per-row, keep going
            s.errors += 1
            write_back(row, "", "error", str(e))
            log.error("%s: prepare failed: %s", rid, e)
            continue

        if force:
            reason = "force"
        else:
            needed, reason = contract.needs_render(row, plan["hash"])
            if not needed:
                s.skipped += 1
                continue

        stem = plan["stem"]
        if stem in claimed:
            s.errors += 1
            msg = f"output name collision on '{stem}' (already claimed by sheet row {claimed[stem]})"
            write_back(row, "", "error", msg)
            log.error("%s: %s", rid, msg)
            continue
        claimed[stem] = row.get("_row", s.total)

        if dry_run:
            s.skipped += 1
            log.info("[dry-run] %s -> %s (%s), inputs: %s",
                     rid, stem, reason, ", ".join(plan.get("files", [])) or "-")
            continue

        try:
            path = render(plan, cfg, job)
            write_back(row, path, "done", "", input_hash=plan["hash"])
            s.rendered += 1
            s.outputs.append(path)
            log.info("%s: done (%s) -> %s", rid, reason, path)
        except Exception as e:  # noqa: BLE001
            s.errors += 1
            write_back(row, "", "error", str(e))
            log.error("%s: %s", rid, e)

    flush = getattr(write_back, "flush", None)
    if flush:
        try:
            flush()
        except Exception as e:  # noqa: BLE001 - a write-back failure must be loud
            log.error("write-back flush failed: %s", e)
            raise

    s.seconds = time.monotonic() - started
    log.info("summary: %s", s)
    return s
